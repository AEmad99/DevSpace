"""Cross-platform pseudo-terminal (PTY) sessions for the in-app terminal.

This backs the interactive Terminal panel: a real shell attached to a real
pseudo-terminal, so REPLs, progress bars, full-screen TUIs (``vim``, ``htop``),
and long-running interactive agents work exactly as they do in a native
terminal — unlike the old one-shot ``POST /shell`` runner which spawned a fresh
process per command and had no input channel.

Platform backends, picked at import:
  * Windows: **ConPTY** via ``pywinpty``. We tried raw ``ctypes`` against the
    in-box ``kernel32!CreatePseudoConsole`` first (to keep the no-native-deps
    spirit of ``core/platform_compat.py``), but on current Windows builds the
    in-box ConPTY failed to attach the child to the pseudoconsole — the shell's
    stdin came through as a plain pipe and its output leaked to the host
    console. pywinpty bundles a known-good ConPTY (the same approach VS Code's
    node-pty and Jupyter's terminado take) and works reliably, so we depend on
    it on Windows.
  * POSIX: the stdlib ``pty`` module + ``termios``/``fcntl`` ioctls (no deps).

Both expose the same async-friendly :class:`_PtyBase` surface: a background
reader thread pumps raw bytes into an :class:`asyncio.Queue`, while writes and
resizes are offloaded to the loop's executor so the event loop never blocks.
Output is delivered as raw UTF-8 bytes (the websocket forwards them verbatim and
xterm.js does its own decoding across chunk boundaries).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from typing import List, Optional

from core.platform_compat import IS_WINDOWS, pid_alive, which_tool

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
_READ_CHUNK = 65536
_IDLE_POLL_S = 0.01  # Windows: poll interval while the pty has no output


# ── Shell / environment selection ────────────────────────────────────────────
def _default_shell_argv() -> List[str]:
    """argv for the interactive shell to attach to the PTY.

    Override with ``DEVSPACE_TERMINAL_SHELL`` (an executable path/name). Default:
    PowerShell 7 (``pwsh``) → Windows PowerShell on Windows; ``$SHELL`` → bash →
    sh on POSIX.
    """
    override = (os.environ.get("DEVSPACE_TERMINAL_SHELL") or "").strip()
    if override:
        return [override]
    if IS_WINDOWS:
        pwsh = which_tool("pwsh")
        if pwsh:
            return [pwsh, "-NoLogo"]
        return [which_tool("powershell") or "powershell.exe", "-NoLogo"]
    shell = (os.environ.get("SHELL") or "").strip()
    if not shell:
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    return [shell]


def _default_env() -> dict:
    env = dict(os.environ)
    if not IS_WINDOWS:
        # Advertise a capable terminal so curses/colour apps behave. ConPTY does
        # not use TERM, so this is POSIX-only.
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
    return env


# ── Shared async surface ──────────────────────────────────────────────────────
class _PtyBase:
    """Common orchestration: a reader thread feeds an asyncio.Queue; writes and
    resizes run in the loop executor. Subclasses implement the OS primitives."""

    def __init__(self, argv: List[str], cwd: Optional[str], env: dict, cols: int, rows: int):
        self.argv = argv
        self.cwd = cwd if (cwd and os.path.isdir(cwd)) else None
        self.env = env
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.pid: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: "asyncio.Queue[Optional[bytes]]" = None  # type: ignore[assignment]
        self._reader: Optional[threading.Thread] = None
        self._closed = False

    # -- lifecycle --
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._spawn()  # OS-specific; sets self.pid
        self._reader = threading.Thread(target=self._read_loop, name="pty-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                data = self._read_blocking()  # bytes, or b"" on EOF
                if not data:
                    break
                self._post(data)
        except Exception:
            pass
        finally:
            self._post(None)  # EOF sentinel

    def _post(self, item: Optional[bytes]) -> None:
        loop, q = self._loop, self._queue
        if loop is None or q is None:
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:
            pass  # loop already closed

    async def read(self) -> Optional[bytes]:
        """Next chunk of PTY output, or None at EOF (shell exited / closed)."""
        return await self._queue.get()

    async def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            await self._loop.run_in_executor(None, self._write_blocking, data)
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(1, int(cols)), max(1, int(rows))
        if not self._closed:
            try:
                self._resize(self.cols, self.rows)
            except Exception:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._terminate()
        except Exception:
            pass
        try:
            self._cleanup()
        except Exception:
            pass

    def exit_code(self) -> Optional[int]:
        try:
            return self._get_exit_code()
        except Exception:
            return None

    # -- OS primitives (subclass) --
    def _spawn(self) -> None: raise NotImplementedError
    def _read_blocking(self) -> bytes: raise NotImplementedError
    def _write_blocking(self, data: bytes) -> None: raise NotImplementedError
    def _resize(self, cols: int, rows: int) -> None: raise NotImplementedError
    def _terminate(self) -> None: raise NotImplementedError
    def _cleanup(self) -> None: raise NotImplementedError
    def _get_exit_code(self) -> Optional[int]: raise NotImplementedError


# ════════════════════════════════════════════════════════════════════════════
# Windows backend — ConPTY via pywinpty
# ════════════════════════════════════════════════════════════════════════════
if IS_WINDOWS:
    try:
        from winpty import PtyProcess as _WinPtyProcess  # type: ignore
        _WINPTY_ERR: Optional[str] = None
    except Exception as exc:  # pragma: no cover - import-time env issue
        _WinPtyProcess = None  # type: ignore
        _WINPTY_ERR = f"{type(exc).__name__}: {exc}"

    class _WindowsPty(_PtyBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._p = None  # winpty.PtyProcess

        def _spawn(self) -> None:
            if _WinPtyProcess is None:
                raise OSError(
                    "The terminal needs the 'pywinpty' package on Windows "
                    f"(pip install pywinpty). Import failed: {_WINPTY_ERR}"
                )
            self._p = _WinPtyProcess.spawn(
                self.argv, cwd=self.cwd, env=self.env,
                dimensions=(self.rows, self.cols),
            )
            self.pid = getattr(self._p, "pid", None)

        def _read_blocking(self) -> bytes:
            # pywinpty's read is non-blocking and yields str (incrementally
            # UTF-8 decoded). Poll until data or EOF; return raw UTF-8 bytes.
            # pywinpty's own isalive() can lag ~7s after the shell exits, which
            # would freeze the terminal on `exit`; use the fast Win32 pid check
            # instead and drain once more before reporting EOF.
            while True:
                try:
                    s = self._p.read(_READ_CHUNK)
                except EOFError:
                    return b""
                except Exception:
                    return b""
                if s:
                    return s.encode("utf-8", "replace")
                if self.pid and not pid_alive(self.pid):
                    try:
                        s = self._p.read(_READ_CHUNK)
                    except Exception:
                        s = ""
                    return s.encode("utf-8", "replace") if s else b""
                time.sleep(_IDLE_POLL_S)

        def _write_blocking(self, data: bytes) -> None:
            self._p.write(data.decode("utf-8", "replace"))

        def _resize(self, cols: int, rows: int) -> None:
            self._p.setwinsize(rows, cols)

        def _terminate(self) -> None:
            try:
                if self._p and self._p.isalive():
                    self._p.terminate(force=True)
            except Exception:
                pass

        def _cleanup(self) -> None:
            self._p = None

        def _get_exit_code(self) -> Optional[int]:
            return getattr(self._p, "exitstatus", None) if self._p else None

    _PtyImpl = _WindowsPty

# ════════════════════════════════════════════════════════════════════════════
# POSIX backend — stdlib pty
# ════════════════════════════════════════════════════════════════════════════
else:
    import fcntl
    import pty
    import signal
    import struct
    import termios

    class _PosixPty(_PtyBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._master: Optional[int] = None
            self._proc: Optional[subprocess.Popen] = None

        def _spawn(self) -> None:
            master, slave = pty.openpty()
            try:
                self._set_size(master, self.cols, self.rows)
                self._proc = subprocess.Popen(
                    self.argv,
                    stdin=slave, stdout=slave, stderr=slave,
                    cwd=self.cwd, env=self.env,
                    start_new_session=True, close_fds=True,
                )
            finally:
                os.close(slave)  # parent keeps only the master side
            self._master = master
            self.pid = self._proc.pid

        @staticmethod
        def _set_size(fd: int, cols: int, rows: int) -> None:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def _read_blocking(self) -> bytes:
            try:
                return os.read(self._master, _READ_CHUNK)
            except OSError:
                return b""  # EIO on the master == child exited

        def _write_blocking(self, data: bytes) -> None:
            os.write(self._master, data)

        def _resize(self, cols: int, rows: int) -> None:
            if self._master is not None:
                self._set_size(self._master, cols, rows)

        def _terminate(self) -> None:
            if self._proc and self._proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass

        def _cleanup(self) -> None:
            if self._master is not None:
                try:
                    os.close(self._master)
                except OSError:
                    pass
                self._master = None

        def _get_exit_code(self) -> Optional[int]:
            return self._proc.poll() if self._proc else None

    _PtyImpl = _PosixPty


# ── Public factory ────────────────────────────────────────────────────────────
async def open_pty_session(
    cwd: Optional[str] = None,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    argv: Optional[List[str]] = None,
    env: Optional[dict] = None,
) -> _PtyBase:
    """Spawn an interactive shell attached to a fresh PTY and return the session.

    Raises ``OSError`` if the PTY cannot be created (e.g. pywinpty missing).
    """
    sess = _PtyImpl(argv or _default_shell_argv(), cwd, env or _default_env(), cols, rows)
    await sess.start()
    return sess
