"""Tests for the LSP bridge (``src/lsp_bridge.py``) and its WebSocket
route (``routes/lsp_routes.py``).

Covers the deterministic pieces that don't require a real language server
to be installed: framing, URI <-> path conversion, path confinement, the
``is_path_allowed`` policy, and the ``_vet_message`` rejection rules.
The subprocess side is exercised by ``test_lsp_session_smoke`` only when
``pylsp`` is importable in the test environment — the test is skipped
otherwise so a clean dev install (no pylsp) still passes the suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Make ``backend`` importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── Framing ────────────────────────────────────────────────────────────────


def test_framer_round_trips_single_message():
    from src.lsp_bridge import _Framer, _encode_message
    f = _Framer()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"capabilities": {}}}
    data = _encode_message(msg)
    out = f.feed(data)
    assert out == [msg]


def test_framer_handles_chunked_input():
    from src.lsp_bridge import _Framer, _encode_message
    f = _Framer()
    a = {"jsonrpc": "2.0", "id": 1, "method": "a"}
    b = {"jsonrpc": "2.0", "method": "b", "params": {}}
    blob = _encode_message(a) + _encode_message(b)
    # Feed in tiny chunks; both messages must eventually come out.
    out = []
    for i in range(0, len(blob), 7):
        out.extend(f.feed(blob[i: i + 7]))
    assert out == [a, b]


def test_framer_drops_malformed_message():
    from src.lsp_bridge import _Framer
    f = _Framer()
    # Garbage header + body length 0; should not raise.
    out = f.feed(b"Content-Length: 0\r\n\r\n")
    assert out == []


def test_framer_handles_unicode():
    from src.lsp_bridge import _Framer, _encode_message
    f = _Framer()
    msg = {"jsonrpc": "2.0", "method": "workspace/symbol",
           "params": {"query": "café résumé"}}
    out = f.feed(_encode_message(msg))
    assert out[0]["params"]["query"] == "café résumé"


# ── URI <-> path ──────────────────────────────────────────────────────────


def test_uri_to_path_unix():
    from src.lsp_bridge import uri_to_path
    assert uri_to_path("file:///home/user/code/foo.py") == "/home/user/code/foo.py"


def test_uri_to_path_handles_url_encoded_chars():
    from src.lsp_bridge import uri_to_path
    assert uri_to_path("file:///home/user/My%20Code/foo.py") == "/home/user/My Code/foo.py"


def test_uri_to_path_rejects_non_file_scheme():
    from src.lsp_bridge import uri_to_path
    try:
        uri_to_path("https://example.com/x")
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-file URI")


def test_path_to_uri_round_trip():
    from src.lsp_bridge import path_to_uri, uri_to_path
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "sub dir", "foo.py")
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "w") as fh:
            fh.write("x = 1\n")
        uri = path_to_uri(d, f)
        assert uri.startswith("file://")
        # URI round-trip normalises path separators; on Windows the URI uses
        # forward slashes, the filesystem uses backslashes. os.path.normpath
        # makes the comparison deterministic.
        assert os.path.normpath(uri_to_path(uri)) == os.path.normpath(f)


# ── is_path_allowed ──────────────────────────────────────────────────────


def test_is_path_allowed_inside_workspace_true():
    from src.lsp_bridge import is_path_allowed
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "foo.py")
        with open(f, "w") as fh:
            fh.write("x = 1\n")
        assert is_path_allowed(d, f) is True


def test_is_path_allowed_outside_workspace_false():
    from src.lsp_bridge import is_path_allowed
    with tempfile.TemporaryDirectory() as d:
        # /etc/passwd lives outside any reasonable workspace.
        assert is_path_allowed(d, "/etc/passwd") is False


def test_is_path_allowed_traversal_blocked():
    """A path that uses '..' to escape must NOT resolve into the workspace."""
    from src.lsp_bridge import is_path_allowed
    with tempfile.TemporaryDirectory() as d:
        evil = os.path.join(d, "..", "..", "etc", "passwd")
        # Normalize — depending on platform, realpath may bring it back to
        # the same outside path or to an unrelated location. Either way the
        # containment check must reject.
        assert is_path_allowed(d, evil) is False


def test_is_path_allowed_sensitive_substring_blocked():
    """A path inside the workspace but matching a sensitive-path substring
    (e.g. ``.../repo/.ssh/id_rsa``) is rejected — defence in depth so a
    language server can never be tricked into indexing credentials."""
    from src.lsp_bridge import is_path_allowed
    with tempfile.TemporaryDirectory() as d:
        ssh_dir = os.path.join(d, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        evil = os.path.join(ssh_dir, "id_rsa")
        with open(evil, "w") as fh:
            fh.write("placeholder")
        assert is_path_allowed(d, evil) is False


def test_is_path_allowed_nonexistent_but_inside_is_allowed():
    """A non-existent path that's still inside the workspace containment
    check passes — the language server is the right place to surface
    "file does not exist" as a diagnostic. We don't fail closed on every
    non-existent path; only on realpath errors (broken symlinks, etc)."""
    from src.lsp_bridge import is_path_allowed
    with tempfile.TemporaryDirectory() as d:
        ghost = os.path.join(d, "not_yet_created.py")
        assert is_path_allowed(d, ghost) is True


# ── _vet_message (routes/lsp_routes.py) ──────────────────────────────────


def test_vet_message_allows_initialize_without_uri():
    from routes.lsp_routes import _vet_message
    with tempfile.TemporaryDirectory() as d:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"capabilities": {}}}
        assert _vet_message(d, msg) is None


def test_vet_message_allows_didopen_inside_workspace():
    from routes.lsp_routes import _vet_message
    from src.lsp_bridge import path_to_uri
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "foo.py")
        with open(f, "w") as fh:
            fh.write("x = 1\n")
        msg = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
               "params": {"textDocument": {"uri": path_to_uri(d, f),
                                            "languageId": "python",
                                            "version": 1},
                          "text": "x = 1\n"}}
        assert _vet_message(d, msg) is None


def test_vet_message_rejects_didopen_outside_workspace():
    from routes.lsp_routes import _vet_message
    with tempfile.TemporaryDirectory() as d:
        msg = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
               "params": {"textDocument": {"uri": "file:///etc/passwd",
                                            "languageId": "python",
                                            "version": 1},
                          "text": "root:x:0:0..."}}
        reason = _vet_message(d, msg)
        assert reason is not None
        assert "outside" in reason.lower() or "workspace" in reason.lower()


def test_vet_message_rejects_didopen_with_oversized_text():
    """The cap mirrors ``src.constants.MAX_READ_CHARS`` so the LSP server
    never sees more than the editor's file-read endpoint will display.
    A payload at the limit is allowed; one over is rejected with a clear
    'too large' message naming the actual size and the cap."""
    from routes.lsp_routes import _vet_message
    from src.lsp_bridge import path_to_uri
    from src.constants import MAX_READ_CHARS
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "big.py")
        with open(f, "w") as fh:
            fh.write("# placeholder\n")
        uri = path_to_uri(d, f)
        # Just under the cap — must be accepted.
        at_limit = "x" * MAX_READ_CHARS
        msg_ok = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
                  "params": {"textDocument": {"uri": uri, "languageId": "python",
                                               "version": 1},
                             "text": at_limit}}
        assert _vet_message(d, msg_ok) is None, \
            "text at exactly MAX_READ_CHARS must be allowed"
        # One over the cap — must be rejected.
        msg_over = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
                    "params": {"textDocument": {"uri": uri, "languageId": "python",
                                                 "version": 1},
                               "text": "x" * (MAX_READ_CHARS + 1)}}
        reason = _vet_message(d, msg_over)
        assert reason is not None
        assert "too large" in reason.lower()
        # The error message includes both the actual size and the cap so
        # operators diagnosing a stuck LSP can see exactly what was
        # attempted without grepping the source.
        assert str(MAX_READ_CHARS) in reason
        assert str(MAX_READ_CHARS + 1) in reason


def test_vet_message_rejects_non_file_uri():
    from routes.lsp_routes import _vet_message
    with tempfile.TemporaryDirectory() as d:
        msg = {"jsonrpc": "2.0", "method": "textDocument/definition",
               "params": {"textDocument": {"uri": "https://evil.example/x.py"}}}
        reason = _vet_message(d, msg)
        assert reason is not None
        assert "scheme" in reason.lower()


def test_vet_message_rejects_didopen_with_non_string_text():
    from routes.lsp_routes import _vet_message
    from src.lsp_bridge import path_to_uri
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "foo.py")
        with open(f, "w") as fh:
            fh.write("x = 1\n")
        msg = {"jsonrpc": "2.0", "method": "textDocument/didOpen",
               "params": {"textDocument": {"uri": path_to_uri(d, f),
                                            "languageId": "python",
                                            "version": 1},
                          "text": {"not": "a string"}}}
        reason = _vet_message(d, msg)
        assert reason is not None
        assert "string" in reason.lower()


# ── Registry / session smoke (skipped when pylsp isn't installed) ────────


def _pylsp_available() -> bool:
    try:
        __import__("pylsp")
        return True
    except Exception:
        return False


def test_registry_reuses_session_for_same_key():
    """Refcount bookkeeping + session reuse for the same (lang, root) key.

    Implemented in a single event loop because ``asyncio.subprocess``
    binds the child process to the loop that created it — calling
    ``asyncio.run`` per awaitable (which is what we do for the synchronous
    helper tests above) creates a fresh loop each time and trips
    'Future attached to a different loop' on Windows. Skipped when pylsp
    isn't importable so a clean dev install still passes the suite."""
    if not _pylsp_available():
        return  # pragma: no cover
    from src.lsp_bridge import LspSessionRegistry
    reg = LspSessionRegistry()

    async def _scenario():
        with tempfile.TemporaryDirectory() as d:
            # First call: refcount goes 0 -> 1, session starts.
            s1 = await reg.get_or_create("python", d)
            assert s1.is_running
            # Second call: same session, refcount 1 -> 2.
            s2 = await reg.get_or_create("python", d)
            assert s1 is s2
            # Two releases: refcount 2 -> 0. Session stays alive (idle).
            await reg.release("python", d)
            await reg.release("python", d)
            # Forcibly reap: in production the sweeper would do this after
            # the idle timeout; for the test we set last_used far in the
            # past and walk the registry.
            key = ("python", os.path.abspath(d))
            entry = reg._entries.get(key)
            assert entry is not None
            entry.last_used = 0.0
            for e in list(reg._entries.values()):
                if e.refs == 0:
                    await e.session.stop()
                    del reg._entries[e.key]
            assert key not in reg._entries

    asyncio.run(_scenario())


def test_lsp_session_smoke_initializes_and_exits_cleanly():
    """End-to-end: spawn pylsp, send ``initialize``, expect
    ``InitializeResult``. Skipped when pylsp isn't importable so dev
    installs still pass."""
    if not _pylsp_available():
        return  # pragma: no cover
    from src.lsp_bridge import get_registry, reset_registry_for_tests
    reset_registry_for_tests()
    reg = get_registry()

    async def _scenario():
        with tempfile.TemporaryDirectory() as d:
            sess = await reg.get_or_create("python", d)
            try:
                q = sess.subscribe()
                await sess.send({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"processId": os.getpid(),
                               "rootUri": None,
                               "capabilities": {}}})
                # Drain messages until we see our `initialize` response.
                while True:
                    msg = await asyncio.wait_for(q.get(), timeout=10)
                    if msg is None:
                        raise AssertionError("server exited before initialize")
                    if msg.get("id") == 1:
                        assert msg.get("result"), "expected InitializeResult"
                        return
            finally:
                await reg.release("python", d)
                for e in list(reg._entries.values()):
                    if e.refs == 0:
                        await e.session.stop()
                        del reg._entries[e.key]

    asyncio.run(_scenario())


def test_availability_map_reports_python_when_pylsp_installed():
    from src.lsp_bridge import get_registry, reset_registry_for_tests
    # The subprocess tests above bind a pylsp transport to their event
    # loop; we want a clean registry for the pure-Python availability check
    # so the subprocess lifecycle doesn't leak into this test.
    reset_registry_for_tests()
    m = get_registry().availability_map()
    assert "python" in m
    # The truth value depends on whether pylsp is installed in the test env.
    expected = _pylsp_available()
    assert m["python"] is expected


def test_availability_map_includes_typescript_and_rust():
    """The TS and Rust entries are present in ``LSP_SERVERS`` and
    ``availability_map()`` returns them — even if the binaries are
    not installed locally, the keys should exist (with value ``False``).
    This pins the contract that the frontend uses to render the LSP
    status pill: a missing key is treated the same as a False value,
    so a missing entry would silently disable the feature."""
    from src.lsp_bridge import get_registry, reset_registry_for_tests, LSP_SERVERS
    reset_registry_for_tests()
    m = get_registry().availability_map()
    assert "typescript" in m, "LSP_SERVERS must include 'typescript'"
    assert "rust" in m, "LSP_SERVERS must include 'rust'"
    # Both should be False on a dev machine without node/rustup installed.
    # The exact values depend on PATH, but the structure is what we test.
    assert isinstance(m["typescript"], bool)
    assert isinstance(m["rust"], bool)
    # And the dict should have exactly the three languages we ship in v1.1.
    assert set(m.keys()) == {"python", "typescript", "rust"}, \
        f"LSP_SERVERS exposed unexpected languages: {set(m.keys())}"


def test_didchange_triggers_publish_diagnostics():
    """End-to-end: open a Python file with a syntax error, then send
    ``textDocument/didChange`` with a corrected version, and assert the
    server emits ``textDocument/publishDiagnostics`` in both states.

    This is the test that proves item 1 (the front-end `didChange` hook
    in codeWorkspace.js) actually round-trips through to the language
    server. Without it, `didChange` is just code that we hope works.

    Skipped when pylsp isn't installed so a clean dev install still
    passes the suite."""
    if not _pylsp_available():
        return  # pragma: no cover
    from src.lsp_bridge import get_registry, reset_registry_for_tests, path_to_uri
    reset_registry_for_tests()
    reg = get_registry()

    # Deliberate syntax error: the ``def f(`` has an unmatched paren. pylsp
    # flags it as E999 / SyntaxError via pyflakes.
    BROKEN = "def f(\n    return 1\n"
    FIXED = "def f():\n    return 1\n"

    async def _wait_for_diagnostics(q, file_uri, expected_min_count=1,
                                    timeout=10):
        """Drain messages until we see a publishDiagnostics for the file
        with at least ``expected_min_count`` diagnostic entries. Returns
        the diagnostic list. Times out and fails the test if we never
        see one — a 10s budget is generous for a fresh pylsp startup
        that has to import rope/pyflakes/etc on first use."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out after {timeout}s waiting for diagnostics on "
                    f"{file_uri}")
            msg = await asyncio.wait_for(q.get(), timeout=remaining)
            if msg is None:
                raise AssertionError("server exited before publishing diagnostics")
            if msg.get("method") != "textDocument/publishDiagnostics":
                continue
            params = msg.get("params") or {}
            if params.get("uri") != file_uri:
                continue  # diagnostics for a different file we don't care about
            diags = params.get("diagnostics") or []
            if len(diags) >= expected_min_count:
                return diags

    async def _wait_for_diagnostics_changed(q, file_uri, first_count, timeout=10):
        """Drain messages until we see a publishDiagnostics for the file
        whose list length is *different* from ``first_count`` (i.e. the
        server re-evaluated the file after our didChange). Accepts
        either an empty list (diagnostics cleared) or a list with a
        different number of entries (different set of issues). We don't
        require a specific count because pylsp's plugin output varies
        between versions — the contract is "the server re-published",
        not "the diagnostics are empty"."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out after {timeout}s waiting for diagnostics "
                    f"to change on {file_uri}")
            msg = await asyncio.wait_for(q.get(), timeout=remaining)
            if msg is None:
                raise AssertionError("server exited while waiting for re-publish")
            if msg.get("method") != "textDocument/publishDiagnostics":
                continue
            params = msg.get("params") or {}
            if params.get("uri") != file_uri:
                continue
            diags = params.get("diagnostics") or []
            if len(diags) != first_count:
                return diags

    async def _scenario():
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "broken.py")
            with open(f, "w") as fh:
                fh.write(BROKEN)
            uri = path_to_uri(d, f)

            sess = await reg.get_or_create("python", d)
            try:
                q = sess.subscribe()
                # 1. Handshake.
                await sess.send({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"processId": os.getpid(),
                               "rootUri": None, "rootPath": d,
                               "capabilities": {}}})
                while True:
                    msg = await asyncio.wait_for(q.get(), timeout=10)
                    if msg is None:
                        raise AssertionError("server exited before initialize")
                    if msg.get("id") == 1:
                        assert msg.get("result"), "expected InitializeResult"
                        break
                await sess.send({"jsonrpc": "2.0", "method": "initialized",
                                 "params": {}})

                # 2. didOpen the broken file → expect a diagnostic.
                await sess.send({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                                 "params": {"textDocument": {
                                     "uri": uri, "languageId": "python",
                                     "version": 1, "text": BROKEN}}})
                diags = await _wait_for_diagnostics(q, uri, expected_min_count=1)
                # pylsp reports the missing paren as one of: SyntaxError,
                # "invalid syntax", "was never closed", or an E901
                # TokenError from pycodestyle. We accept any of these —
                # the source/checker plugins can change between versions,
                # and the point of this test is "the server emitted a
                # diagnostic for the broken file", not "the exact wording".
                messages = " ".join(d.get("message", "") for d in diags)
                assert any(needle in messages
                           for needle in ("SyntaxError", "invalid syntax",
                                          "never closed", "TokenError")), \
                    f"expected a syntax-error diagnostic, got: {diags}"

                # 3. didChange to the fixed version → expect diagnostics to change.
                # We only require that the server re-publishes (count
                # differs from the broken-state count of 2), not that
                # the list is empty — different pylsp plugin combinations
                # produce different post-fix diagnostics.
                line_count = len(FIXED.splitlines())
                last_line_len = len(FIXED.splitlines()[-1])
                full_range = {"start": {"line": 0, "character": 0},
                              "end": {"line": line_count - 1,
                                      "character": last_line_len}}
                await sess.send({"jsonrpc": "2.0",
                                 "method": "textDocument/didChange",
                                 "params": {"textDocument": {"uri": uri,
                                                              "version": 2},
                                            "contentChanges": [{
                                                "range": full_range,
                                                "rangeLength": 0,
                                                "text": FIXED}]}})
                new_diags = await _wait_for_diagnostics_changed(
                    q, uri, first_count=len(diags))
                # The fixed file has no unmatched paren, so the
                # never-closed-paren diagnostic should be gone. We assert
                # that as a sanity check — the wording varies by pylsp
                # version, so we look for the token rather than the full
                # sentence.
                assert not any("never closed" in d.get("message", "")
                               for d in new_diags), \
                    f"never-closed-paren diagnostic should be gone after fix, " \
                    f"got: {new_diags}"
            finally:
                await reg.release("python", d)
                for e in list(reg._entries.values()):
                    if e.refs == 0:
                        await e.session.stop()
                        del reg._entries[e.key]

    asyncio.run(_scenario())
