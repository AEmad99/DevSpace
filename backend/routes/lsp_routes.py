"""LSP (Language Server Protocol) WebSocket bridge for the Code editor.

Exposes two endpoints on the existing FastAPI app:

  * ``GET  /api/lsp/availability``   — JSON ``{lang: bool}`` for every
    language the bridge knows about. The frontend reads this to decide
    whether to show an "LSP" indicator on a file open.

  * ``WS   /api/lsp/{lang}?path=<root>`` — opens (or reuses) a
    language-server subprocess for ``lang`` rooted at the workspace
    directory ``<root>`` and proxies JSON-RPC frames both ways. Frames
    are full JSON-RPC objects (``{jsonrpc, id, method, params}`` etc.)
    sent as WebSocket text messages. The session is reference-counted;
    closing the WebSocket decrements the count and the subprocess is
    reaped after ``lsp_bridge._IDLE_TIMEOUT_SECS`` of idleness so
    re-opening a file is instant (no rescan of the workspace).

Auth posture: owner-authed, NOT admin-gated, matching the rest of the
Code Workspace surface. In single-user mode (``AUTH_ENABLED=false``,
the desktop default) the loopback cookie authenticates. In multi-user
mode the WebSocket auth gate mirrors the terminal WS gate
(``_pty_ws_allowed`` in ``code_workspace_routes``): we require the
client host to be loopback. The Tauri desktop UI binds uvicorn to
``127.0.0.1`` so it always qualifies.

Path confinement: every incoming message is inspected; ``textDocument/
didOpen`` carries a URI, which we map back to a filesystem path and
verify lives inside the workspace root. Out-of-bounds URIs are dropped
silently (the language server is never asked to read them). This
defends against a malicious client that crafts a URI pointing at
``/etc/passwd`` or a symlink escape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from src.lsp_bridge import (
    LSP_SERVERS,
    LspSession,
    get_registry,
    is_path_allowed,
    uri_to_path,
)

logger = logging.getLogger(__name__)


# Methods whose ``params`` carry a textDocument URI we must vet before
# forwarding to the language server. Anything not in this list is
# forwarded as-is (initialize/shutdown, workspace/*, completion, etc.
# don't reference user files).
_URI_BEARING_METHODS = {
    "textDocument/didOpen",
    "textDocument/didChange",
    "textDocument/didSave",
    "textDocument/didClose",
    "textDocument/definition",
    "textDocument/references",
    "textDocument/hover",
    "textDocument/documentSymbol",
    "textDocument/codeAction",
    "textDocument/formatting",
    "textDocument/rangeFormatting",
    "textDocument/signatureHelp",
    "textDocument/completion",
    "textDocument/implementation",
    "textDocument/typeDefinition",
    "textDocument/declaration",
}


def _vet_message(workspace_root: str, msg: dict) -> Optional[str]:
    """Return None when the message is safe to forward, or a string
    reason when it should be dropped. We do not raise — the WS pump
    just logs and skips the offending frame.

    Confinement targets the *textDocument.uri* field on the params.
    For didOpen we also verify the ``text`` body length is sane so a
    client can't ask the server to slurp a multi-GB file.
    """
    method = msg.get("method") or ""
    if method not in _URI_BEARING_METHODS:
        return None
    params = msg.get("params") or {}
    doc = params.get("textDocument") or {}
    uri = doc.get("uri")
    if not uri:
        # Some methods (definition, references) put the uri at params.uri
        # when called from the older "textDocument/x-at-position" shape.
        uri = params.get("uri")
    if not isinstance(uri, str):
        return "missing uri on textDocument request"
    if not uri.startswith("file://"):
        return f"unsupported uri scheme: {uri.split(':', 1)[0]}"
    try:
        path = uri_to_path(uri)
    except ValueError as e:
        return f"could not decode uri: {e}"
    if not is_path_allowed(workspace_root, path):
        return f"path is outside the workspace: {path}"
    # Sanity-check didOpen body size. Aligned with `MAX_READ_CHARS` from
    # `src.constants` — the editor's file-read endpoint truncates to that
    # many characters before sending to the client, so the LSP server
    # never needs more than that. Letting the server see more than the
    # editor shows would create a misleading analysis surface.
    if method == "textDocument/didOpen":
        from src.constants import MAX_READ_CHARS
        text = (params.get("text") or "")
        if not isinstance(text, str):
            return "didOpen text is not a string"
        if len(text) > MAX_READ_CHARS:
            return (f"didOpen text too large: {len(text)} chars "
                    f"(max {MAX_READ_CHARS})")
    return None


def _ws_allowed(ws: WebSocket) -> bool:
    """Mirror ``_pty_ws_allowed`` for the LSP endpoint. Owner-authed in
    single-user mode; loopback-only in multi-user mode (defence in
    depth — the LSP server can read every file in the workspace)."""
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return True
    client = getattr(ws, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def setup_lsp_routes() -> APIRouter:
    router = APIRouter(prefix="/api/lsp", tags=["lsp"])

    @router.get("/availability")
    def availability(request: Request):
        """Return ``{lang: bool}`` for every language we know about. The
        frontend uses this to dim the LSP indicator on file open when
        the server isn't installed."""
        return get_registry().availability_map()

    @router.websocket("/{lang}")
    async def lsp_socket(ws: WebSocket, lang: str):
        if not _ws_allowed(ws):
            await ws.close(code=1008)
            return
        if lang not in LSP_SERVERS:
            await ws.close(code=1008)
            return

        root = (ws.query_params.get("path") or "").strip()
        if not root or not os.path.isdir(root):
            try:
                await ws.send_text(json.dumps(
                    {"type": "error", "message": "missing or invalid ?path="}))
                await ws.close(code=1008)
            except Exception:
                pass
            return

        # Reject languages whose server isn't installed BEFORE accepting —
        # saves a roundtrip and surfaces a clear error message.
        if not get_registry().is_available(lang):
            await ws.accept()
            try:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": (f"{lang!r} language server is not installed. "
                                f"Install it (e.g. `pip install python-lsp-server[all]` "
                                f"for python) and reload."),
                }))
                await ws.close(code=1011)
            except Exception:
                pass
            return

        await ws.accept()
        registry = get_registry()
        try:
            session = await registry.get_or_create(lang, root)
        except Exception as e:
            logger.warning("lsp: failed to start %s server: %s", lang, e)
            try:
                await ws.send_text(json.dumps(
                    {"type": "error", "message": f"failed to start server: {e}"}))
                await ws.close(code=1011)
            except Exception:
                pass
            return

        subscriber_queue = session.subscribe()

        async def pump_server_to_client():
            try:
                while True:
                    msg = await subscriber_queue.get()
                    if msg is None:
                        # EOF / server exited.
                        break
                    try:
                        await ws.send_text(json.dumps(msg))
                    except Exception:
                        return
            except (asyncio.CancelledError, WebSocketDisconnect):
                return
            except Exception as e:
                logger.warning("lsp: server->client pump failed: %s", e)

        async def pump_client_to_server():
            try:
                while True:
                    raw = await ws.receive_text()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    reject = _vet_message(root, msg)
                    if reject:
                        logger.info("lsp: dropped %s from client: %s",
                                    msg.get("method"), reject)
                        continue
                    try:
                        await session.send(msg)
                    except Exception as e:
                        logger.warning("lsp: send to server failed: %s", e)
                        return
            except (asyncio.CancelledError, WebSocketDisconnect):
                return
            except Exception as e:
                logger.warning("lsp: client->server pump failed: %s", e)

        out_task = asyncio.create_task(pump_server_to_client())
        in_task = asyncio.create_task(pump_client_to_server())
        try:
            await asyncio.wait(
                {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (out_task, in_task):
                t.cancel()
            session.unsubscribe(subscriber_queue)
            await registry.release(lang, root)
            try:
                await ws.close()
            except Exception:
                pass

    return router
