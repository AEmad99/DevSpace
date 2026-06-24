"""LSP (Language Server Protocol) bridge for the in-app Code editor.

The Code editor (backend/static/js/codeWorkspace.js) is a vanilla
``<textarea>`` with a highlight.js overlay. There is no Monaco, no
CodeMirror, no process-spawn capability through the Tauri shell plugin, and
no npm/bundler — the frontend is raw ESM served as static files.

This module is the **server-side** half of a thin WebSocket bridge: a
FastAPI route (``routes/lsp_routes.py``) opens one WebSocket per
(workspace_root, language) pair, and this module manages the lifetime of
one language-server subprocess per (workspace_root, language) and proxies
JSON-RPC frames between the WebSocket and the server's stdio.

Why stdio: LSP servers are stdio-only by design (``vscode-langservers-
extracted``, ``pylsp``, ``rust-analyzer``, ``typescript-language-server``,
``gopls``, etc. all speak JSON-RPC over Content-Length framed stdio). A
WebSocket is a natural transport to the browser; the bridge translates
the two.

Lifecycle:
  * ``LspSessionRegistry.get_or_create(lang, root, owner)`` — refcounted
    registry; one subprocess per ``(lang, root)`` pair, shared across
    multiple concurrent WebSocket clients (rare in practice but possible
    if the user opens two Code panels side-by-side).
  * Idle sessions (no clients, no in-flight messages) are reaped after
    ``_IDLE_TIMEOUT_SECS`` by an async sweeper task started lazily on
    first use.
  * Each WebSocket caller must call ``release()`` on disconnect so the
    refcount drops; the session is killed when the count hits zero AND
    the idle timer fires.

Path confinement: every ``textDocument/didOpen`` URI is mapped back to a
filesystem path via ``uri_to_path`` and checked against the workspace
root + sensitive-path denylist (``_resolve_tool_path_in_workspace``).
URIs outside the root or matching a denylisted substring are dropped —
we never forward them to the server, so a malicious client can't
trick the language server into reading ``/etc/passwd``.

Tested with ``python-lsp-server`` (``pylsp``) v1.7+. Adding a new
language is one entry in ``LSP_SERVERS`` plus a vendored Monaco language
worker on the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# Language → server launch descriptor. ``cmd`` is argv to exec; ``languages``
# is the set of LSP language ids the server handles. ``availability_check``
# runs at startup and from /api/lsp/availability to decide whether to expose
# the language at all.
#
# v1.1: Python (built-in via python-lsp-server), TypeScript/JavaScript
# (requires `npm install -g typescript-language-server`), and Rust
# (requires `rustup component add rust-analyzer`). Each non-Python
# server is opt-in: if the binary is missing, /api/lsp/availability
# reports `false` for that language and the frontend renders the LSP
# status pill as "off" (yellow) instead of attempting a WS upgrade.
#
# Adding more languages is a config-only change: add an entry here, a
# `_lspLanguageFor` branch in `codeWorkspace.js`, and (if you want
# custom syntax highlighting) a Monaco basic-languages contribution.


def _module_available(mod_name: str) -> bool:
    """Return True when ``mod_name`` can be imported in the current Python.

    Used as the availability check for language servers that are shipped as
    Python modules (e.g. ``pylsp``). Importing is more reliable than
    ``shutil.which`` because venvs often don't put module entry points on
    PATH for unrelated shells.
    """
    try:
        __import__(mod_name)
        return True
    except Exception:
        return False


# ── Non-Python language-server helpers ─────────────────────────────────────
# `which_tool` from `core.platform_compat` already handles Windows
# PATHEXT suffixes (`.cmd` / `.exe` / `.bat`) so the same code works
# on dev machines, CI runners, and the Tauri desktop build without
# per-platform branching. The helpers below lazily import which_tool
# so the LSP bridge can be imported on systems that don't yet have
# `core.platform_compat` on the import path (tests run in a stripped
# environment).


def _which_tool(name: str):
    """Wrapper around ``core.platform_compat.which_tool`` that defers
    the import so this module stays importable in test environments
    that strip ``core``. Returns the path string or None."""
    try:
        from core.platform_compat import which_tool
        return which_tool(name)
    except Exception:
        return None


def _ts_available() -> bool:
    """True when ``typescript-language-server`` (or its ``ts_ls`` alias)
    is on PATH. The bridge always uses whichever the user has installed;
    the two names exist because the npm package has been republished
    under different names across versions."""
    return _which_tool("typescript-language-server") is not None \
        or _which_tool("ts_ls") is not None


def _ts_cmd() -> list:
    """Return argv for the TS language server. Falls back to the bare
    name if the binary isn't on PATH *now* — the availability check
    catches that case earlier in the request lifecycle, so the bare
    name here only matters if availability flipped between the check
    and the spawn (rare)."""
    path = (_which_tool("typescript-language-server")
            or _which_tool("ts_ls")
            or "typescript-language-server")
    return [path, "--stdio"]


def _rust_available() -> bool:
    return _which_tool("rust-analyzer") is not None


def _rust_cmd() -> list:
    path = _which_tool("rust-analyzer") or "rust-analyzer"
    # rust-analyzer speaks JSON-RPC directly on stdio; no flag needed.
    return [path]


LSP_SERVERS: Dict[str, Dict] = {
    "python": {
        "cmd": [sys.executable, "-m", "pylsp"],
        "languages": {"python"},
        # `python -m pylsp` works whenever python-lsp-server is importable.
        # The check imports the module rather than shutil.which-ing a binary
        # because the module is the canonical install method in venvs.
        "availability_check": lambda: _module_available("pylsp"),
    },
    "typescript": {
        # typescript-language-server handles both TS and JS. We resolve
        # the binary at import time so that the cmd list is fully-formed
        # by the time asyncio.create_subprocess_exec runs. If the binary
        # is uninstalled, the availability_check returns False and the
        # WS route refuses the connection before the subprocess is
        # launched — see lsp_routes._ws_available / is_available.
        "cmd": _ts_cmd(),
        "languages": {"typescript", "javascript",
                      "typescriptreact", "javascriptreact"},
        "availability_check": _ts_available,
    },
    "rust": {
        "cmd": _rust_cmd(),
        "languages": {"rust"},
        "availability_check": _rust_available,
    },
}


# ── Path confinement (mirrors ``code_workspace_routes._confine``) ──────────
# We don't import the workspace-confine helper directly to avoid a circular
# import (tool_execution.py is heavy and pulls in a lot of agent deps). The
# policy here is intentionally a *subset* of that helper's policy: a path
# is rejected when (a) it doesn't resolve to a real file/dir, (b) it lives
# outside the workspace root, or (c) it matches a sensitive-path substring.
_SENSITIVE_PATH_SUBSTRINGS = (
    os.sep + ".ssh",
    os.sep + ".gnupg",
    os.sep + ".aws" + os.sep,
    "id_rsa",
    "id_ed25519",
    os.sep + ".config" + os.sep + "git",
)


def path_to_uri(workspace_root: str, file_path: str) -> str:
    """Convert a filesystem path (relative to ``workspace_root`` or absolute
    inside it) to a ``file://`` URI. Both forward and backward slashes are
    accepted; the URI is canonical with forward slashes per RFC 8089."""
    abs_path = os.path.abspath(file_path)
    if not abs_path.startswith(os.path.abspath(workspace_root)):
        # Caller is responsible for confining first; we just stringify.
        pass
    # urlparse on a Windows path needs explicit 'file://' scheme handling.
    canonical = abs_path.replace("\\", "/")
    if not canonical.startswith("/"):
        canonical = "/" + canonical
    # Encode each path segment but keep '/' as a literal.
    from urllib.parse import quote
    return "file://" + quote(canonical, safe="/")


def uri_to_path(uri: str) -> str:
    """Inverse of ``path_to_uri``. Returns the local filesystem path."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported URI scheme: {parsed.scheme!r}")
    raw = unquote(parsed.path or "")
    # On Windows, urlparse leaves a leading '/' before the drive letter
    # ('/C:/...'); strip it so os.path understands the path.
    if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return raw


def is_path_allowed(workspace_root: str, file_path: str) -> bool:
    """Return True when ``file_path`` lives inside ``workspace_root`` and
    doesn't match a sensitive-path substring. Used to gate every URI the
    frontend sends to the language server."""
    try:
        abs_root = os.path.abspath(workspace_root)
        abs_path = os.path.abspath(file_path)
    except (ValueError, OSError):
        return False
    # Containment: realpath catches symlink escapes. Fail closed when the
    # path doesn't exist (LSP shouldn't try to open non-existent files
    # anyway, but we don't want to expose existence info either).
    try:
        real_root = os.path.realpath(abs_root)
        real_path = os.path.realpath(abs_path)
    except OSError:
        return False
    if not real_path.startswith(real_root + os.sep) and real_path != real_root:
        return False
    lowered = real_path.lower()
    for needle in _SENSITIVE_PATH_SUBSTRINGS:
        if needle.lower() in lowered:
            return False
    return True


# ── JSON-RPC framing ─────────────────────────────────────────────────────
# LSP uses HTTP-style headers on stdio: each message is
#   Content-Length: N\r\n
#   \r\n
#   <N bytes of JSON>
# A single read can contain multiple messages (or partial messages), so we
# buffer and re-parse on every chunk.


def _encode_message(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class _Framer:
    """Accumulates bytes and yields one JSON-RPC message at a time."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list:
        """Append ``chunk`` to the buffer; return any complete messages."""
        self._buf.extend(chunk)
        out = []
        while True:
            idx = self._buf.find(b"\r\n\r\n")
            if idx < 0:
                break
            header = self._buf[:idx].decode("ascii", errors="replace")
            length = 0
            for line in header.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = 0
                    break
            if length <= 0:
                # Malformed; drop the header and try again.
                del self._buf[: idx + 4]
                continue
            total = idx + 4 + length
            if len(self._buf) < total:
                break  # wait for more
            body = bytes(self._buf[idx + 4: total])
            del self._buf[:total]
            try:
                out.append(json.loads(body))
            except json.JSONDecodeError:
                # Drop malformed messages silently — the server shouldn't
                # produce any, and if it does we don't want to die.
                continue
        return out


# ── Subprocess wrapper ────────────────────────────────────────────────────


class LspSession:
    """A single language-server subprocess plus its JSON-RPC stream.

    Owned by ``LspSessionRegistry``. Do not construct directly — use the
    registry's ``get_or_create`` so refcounting stays correct.
    """

    def __init__(self, lang: str, cmd: list, cwd: str) -> None:
        self.lang = lang
        self.cmd = cmd
        self.cwd = cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._framer = _Framer()
        self._reader_task: Optional[asyncio.Task] = None
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.is_running:
            return
        logger.info("lsp: starting %s for cwd=%s", self.cmd, self.cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._framer = _Framer()
        self._reader_task = asyncio.create_task(
            self._pump_stdout(), name=f"lsp-{self.lang}-stdout")
        # stderr is read into the void (logged at WARNING when non-empty
        # so misconfigured servers are visible without spamming logs).
        asyncio.create_task(
            self._pump_stderr(), name=f"lsp-{self.lang}-stderr")

    async def stop(self) -> None:
        async with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        self._proc.kill()
                        await self._proc.wait()
                except ProcessLookupError:
                    pass
            # Drain subscribers so any pending await unblocks.
            for q in list(self._subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def send(self, message: dict) -> None:
        if not self.is_running:
            raise RuntimeError(f"LSP session for {self.lang!r} is not running")
        data = _encode_message(message)
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _pump_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                for msg in self._framer.feed(chunk):
                    for q in list(self._subscribers):
                        try:
                            q.put_nowait(msg)
                        except asyncio.QueueFull:
                            # Slow consumer — drop the oldest to make room.
                            try:
                                q.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            try:
                                q.put_nowait(msg)
                            except asyncio.QueueFull:
                                pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("lsp %s: stdout pump failed: %s", self.lang, e)
        finally:
            # EOF: notify subscribers with None so they can break out.
            for q in list(self._subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.warning("lsp %s stderr: %s", self.lang, text)
        except asyncio.CancelledError:
            return
        except Exception:
            return


# ── Registry ──────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    session: LspSession
    refs: int = 0
    last_used: float = 0.0  # monotonic seconds; set in get_or_create / release
    key: Tuple[str, str] = field(default_factory=tuple)  # (lang, root)


_IDLE_TIMEOUT_SECS = 600  # 10 min — match the plan


class LspSessionRegistry:
    """One registry per process. Holds (lang, root) → ``_Entry``."""

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], _Entry] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None

    async def get_or_create(self, lang: str, root: str) -> LspSession:
        """Return a running ``LspSession`` for ``(lang, root)``, starting one
        if needed. Increments the refcount — caller MUST call ``release``
        when the WebSocket disconnects."""
        if lang not in LSP_SERVERS:
            raise ValueError(f"unsupported language: {lang!r}")
        cfg = LSP_SERVERS[lang]
        key = (lang, os.path.abspath(root))
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                session = LspSession(lang, cfg["cmd"], cwd=key[1])
                await session.start()
                entry = _Entry(session=session, key=key)
                self._entries[key] = entry
            entry.refs += 1
            entry.last_used = asyncio.get_event_loop().time()
        self._ensure_sweeper()
        return entry.session

    async def release(self, lang: str, root: str) -> None:
        """Decrement the refcount for ``(lang, root)``. The session stays
        alive for ``_IDLE_TIMEOUT_SECS`` after the last release so a
        reconnect reuses the same subprocess (preserves indexed state)."""
        key = (lang, os.path.abspath(root))
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refs = max(0, entry.refs - 1)
            entry.last_used = asyncio.get_event_loop().time()

    def is_available(self, lang: str) -> bool:
        """True when the language's server is configured AND its
        availability check passes (e.g. pylsp importable)."""
        cfg = LSP_SERVERS.get(lang)
        if not cfg:
            return False
        check = cfg.get("availability_check")
        try:
            return bool(check() if check else True)
        except Exception:
            return False

    def availability_map(self) -> Dict[str, bool]:
        return {lang: self.is_available(lang) for lang in LSP_SERVERS}

    def _ensure_sweeper(self) -> None:
        if self._sweeper_task and not self._sweeper_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        self._sweeper_task = loop.create_task(
            self._sweeper(), name="lsp-sweeper")

    async def _sweeper(self) -> None:
        """Periodically reap sessions that have no refs AND have been idle
        longer than ``_IDLE_TIMEOUT_SECS``."""
        try:
            while True:
                await asyncio.sleep(60)
                now = asyncio.get_event_loop().time()
                async with self._lock:
                    to_kill = [
                        e for e in self._entries.values()
                        if e.refs == 0 and (now - e.last_used) > _IDLE_TIMEOUT_SECS
                    ]
                    for entry in to_kill:
                        logger.info(
                            "lsp: reaping idle session %s (refs=0, idle=%.0fs)",
                            entry.key, now - entry.last_used)
                        try:
                            await entry.session.stop()
                        except Exception as e:
                            logger.warning("lsp: reap error: %s", e)
                        del self._entries[entry.key]
                if not self._entries:
                    # No work to do; let the next get_or_create restart us.
                    return
        except asyncio.CancelledError:
            return


# Single global registry. Importers should use ``get_registry()`` so tests
# can monkey-patch.
_REGISTRY: Optional[LspSessionRegistry] = None


def get_registry() -> LspSessionRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = LspSessionRegistry()
    return _REGISTRY


def reset_registry_for_tests() -> None:
    """Drop the cached registry so test setup can install a fresh one."""
    global _REGISTRY
    _REGISTRY = None
