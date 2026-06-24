"""Code Workspace API - file tree, editor read/write, search, git, diff revert.

Phase 1: workspace backbone.
  - GET/POST /api/workspace/current   - get/set the server-persisted root
  - GET  /api/workspace/tree          - one-level dir listing (lazy-expand UI)
  - GET  /api/workspace/file          - read a file (truncated at MAX_READ_CHARS)
  - POST /api/workspace/file          - write a file (returns a unified diff)
  - GET  /api/workspace/search?q=     - recursive fuzzy filename search

Phase 3 will add: POST /revert, POST /revert_all/{session_id}
Phase 6 will add: /git/* endpoints

Auth posture (plan decision 4): owner-authed, NOT admin-gated — but every path
is confined to the chosen workspace root via _resolve_tool_path_in_workspace(),
which also enforces the sensitive-path denylist (.ssh, .gnupg, id_rsa, ...).
In single-user mode (AUTH_ENABLED=false) get_current_user() returns the sole
owner, so this is equivalent to admin access for the desktop user.
"""
import asyncio
import json
import os
import subprocess

from fastapi import APIRouter, Request, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src.tool_execution import vet_workspace, _resolve_tool_path_in_workspace

# Reuse the codenav skip-list + caps so the tree/search behave like the agent's
# ls/glob tools (no .git/node_modules/__pycache__ noise, bounded results).
from src.agent_tools.filesystem_tools import _CODENAV_SKIP_DIRS, _unified_diff
from src.constants import MAX_READ_CHARS

_MAX_TREE_ENTRIES = 1000
_MAX_SEARCH_HITS = 200


def _pty_ws_allowed(ws: WebSocket) -> bool:
    """Authorization gate for the terminal WebSocket.

    The HTTP auth middleware is BaseHTTPMiddleware, so it never runs for a WS
    upgrade and ``request.state.current_user`` is unset here — we gate directly.
    The in-app terminal is owner-authed / single-user (same posture as the file
    endpoints): when AUTH_ENABLED=false (the desktop default) we allow it.
    Otherwise — a multi-user/server deployment — we require a loopback client so
    this shell (an RCE surface) stays off the network; the desktop shell binds
    uvicorn to 127.0.0.1, so the real UI always qualifies."""
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return True
    client = getattr(ws, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _root_or_409() -> str:
    """Load the persisted workspace root or 409 if none chosen yet."""
    from src.settings import get_setting
    root = (get_setting("code_workspace_root") or "").strip()
    if not root:
        raise HTTPException(
            status_code=409,
            detail="No code workspace root is set. POST /api/workspace/current to choose one.",
        )
    # The saved root was vetted at set-time, but re-vet to defend against
    # deletion/symlink-swap since: vet_workspace returns None for a path
    # that is no longer a real, non-sensitive, non-root directory.
    resolved = vet_workspace(root)
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail=f"Saved workspace root is no longer usable: {root!r}. Choose a new one.",
        )
    return resolved


def _confine(root: str, raw_path: str) -> str:
    """Resolve `raw_path` inside `root`, applying the sensitive-path denylist
    and containment check. Raises HTTPException(400) on rejection."""
    try:
        return _resolve_tool_path_in_workspace(root, raw_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def setup_code_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["code-workspace"])

    # ------------------------------------------------------------------
    # Current workspace root (server-persisted)
    # ------------------------------------------------------------------
    @router.get("/current")
    def get_current(request: Request):
        owner = get_current_user(request)  # owner-authed; not admin-gated
        from src.settings import get_setting
        root = (get_setting("code_workspace_root") or "").strip()
        resolved = vet_workspace(root) if root else None
        return {"path": resolved or root or None, "ok": resolved is not None, "owner": owner}

    class CurrentBody(BaseModel):
        path: str

    @router.post("/current")
    def set_current(request: Request, body: CurrentBody):
        get_current_user(request)  # owner-authed
        resolved = vet_workspace(body.path)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"Not a usable workspace folder: {body.path!r} "
                       "(must be an existing directory, not a filesystem root, "
                       "not a sensitive path like .ssh).",
            )
        from src.settings import load_settings, save_settings
        settings = load_settings()
        settings["code_workspace_root"] = resolved
        save_settings(settings)
        return {"path": resolved, "ok": True}

    # ------------------------------------------------------------------
    # File tree (one level — frontend expands lazily on click)
    # ------------------------------------------------------------------
    @router.get("/tree")
    def get_tree(request: Request, path: str = Query(default="")):
        get_current_user(request)
        root = _root_or_409()
        # Empty path = the root itself (already vetted by _root_or_409); only
        # non-empty subpaths need confinement, since _resolve_tool_path_in_workspace
        # rejects "" as "path is required".
        target = root if not path.strip() else _confine(root, path)
        if not os.path.isdir(target):
            raise HTTPException(status_code=400, detail=f"Not a directory: {path or root}")

        entries = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        # Skip noisy/non-project dirs at the top level so the
                        # tree stays focused (matches the agent's ls/glob).
                        if is_dir and name in _CODENAV_SKIP_DIRS:
                            continue
                        size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    entries.append({
                        "name": name,
                        "path": os.path.join(target, name),
                        "is_dir": is_dir,
                        "size": size,
                    })
        except (PermissionError, OSError) as e:
            raise HTTPException(status_code=403, detail=f"Cannot read directory: {e}")

        # Directories first, then files; both alphabetical (case-insensitive).
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        truncated = len(entries) > _MAX_TREE_ENTRIES
        return {
            "root": root,
            "path": target,
            "parent": os.path.dirname(target) if os.path.dirname(target) != target else None,
            "entries": entries[:_MAX_TREE_ENTRIES],
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # File read
    # ------------------------------------------------------------------
    @router.get("/file")
    def get_file(request: Request, path: str = Query(...)):
        get_current_user(request)
        root = _root_or_409()
        full = _confine(root, path)
        if not os.path.isfile(full):
            raise HTTPException(status_code=400, detail=f"Not a file: {path}")
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_READ_CHARS + 1)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        truncated = len(content) > MAX_READ_CHARS
        if truncated:
            content = content[:MAX_READ_CHARS] + f"\n... [truncated at {MAX_READ_CHARS} chars]"
        # Best-effort language hint from extension for the editor's hljs dropdown.
        ext = os.path.splitext(full)[1].lstrip(".").lower()
        return {
            "path": full,
            "content": content,
            "size": os.path.getsize(full),
            "truncated": truncated,
            "language": ext,
        }

    # ------------------------------------------------------------------
    # File write
    # ------------------------------------------------------------------
    class FileWriteBody(BaseModel):
        path: str
        content: str

    @router.post("/file")
    def post_file(request: Request, body: FileWriteBody):
        get_current_user(request)
        root = _root_or_409()
        full = _confine(root, body.path)
        try:
            def _write():
                old = ""
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        old = f.read()
                except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, OSError):
                    old = ""
                d = os.path.dirname(full)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(body.content)
                return old, len(body.content)
            # Sync handler runs in FastAPI's threadpool, so a blocking call is fine.
            old_content, size = _write()
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        diff = _unified_diff(old_content, body.content, full)
        return {"path": full, "size": size, "diff": diff}

    # ------------------------------------------------------------------
    # Fuzzy filename search (recursive, case-insensitive substring)
    # ------------------------------------------------------------------
    @router.get("/search")
    def search(request: Request, q: str = Query(..., min_length=1), path: str = Query(default="")):
        get_current_user(request)
        root = _root_or_409()
        base = _confine(root, path) if path else root
        if not os.path.isdir(base):
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

        needle = q.lower()
        hits = []

        def _walk():
            from pathlib import Path
            for p in Path(base).rglob("*"):
                # Skip noisy dirs entirely (rglob still descends into them,
                # so filter on relative parts — matches GlobTool's approach).
                rel_parts = p.relative_to(base).parts
                if set(rel_parts[:-1]) & _CODENAV_SKIP_DIRS:
                    continue
                name = p.name
                if name.startswith(".") and not needle:
                    continue
                if needle in name.lower():
                    try:
                        is_dir = p.is_dir()
                        size = 0 if is_dir else p.stat().st_size
                    except OSError:
                        continue
                    hits.append({
                        "name": name,
                        "path": str(p),
                        "is_dir": is_dir,
                        "size": size,
                    })
                    if len(hits) >= _MAX_SEARCH_HITS:
                        return
        try:
            _walk()
        except (PermissionError, OSError) as e:
            raise HTTPException(status_code=403, detail=f"Search failed: {e}")
        # Closest matches (name starts with needle) first, then alphabetical.
        hits.sort(key=lambda h: (not h["name"].lower().startswith(needle), h["name"].lower()))
        return {"root": root, "base": base, "q": q, "hits": hits, "truncated": len(hits) >= _MAX_SEARCH_HITS}

    # ------------------------------------------------------------------
    # Diff approval — revert / apply / discard / revert-all (Phase 3)
    # Checkpoints store the absolute path that the agent already wrote to (each
    # was confined when the edit ran), so these act only on paths we recorded —
    # an unknown checkpoint_id is rejected. Return 200 with {ok:false,...} for
    # expected failures (not found / stale) so the chat UI can show them inline.
    # ------------------------------------------------------------------
    class CheckpointBody(BaseModel):
        checkpoint_id: str

    @router.post("/revert")
    def revert(request: Request, body: CheckpointBody):
        get_current_user(request)
        from src.workspace_checkpoints import revert_checkpoint
        return revert_checkpoint(body.checkpoint_id)

    @router.post("/apply")
    def apply(request: Request, body: CheckpointBody):
        get_current_user(request)
        from src.workspace_checkpoints import apply_checkpoint
        return apply_checkpoint(body.checkpoint_id)

    @router.post("/discard")
    def discard(request: Request, body: CheckpointBody):
        get_current_user(request)
        from src.workspace_checkpoints import discard_checkpoint
        return discard_checkpoint(body.checkpoint_id)

    @router.post("/revert_all/{session_id}")
    def revert_all(request: Request, session_id: str):
        get_current_user(request)
        from src.workspace_checkpoints import revert_all_for_session
        return {"ok": True, "reverted": revert_all_for_session(session_id)}

    # ------------------------------------------------------------------
    # List checkpoints for a session (Phase 1 — strict-mode edit review).
    # Backs the "View session changes" affordance on the assistant message
    # and the "1 edit awaiting approval" chip on the input bar. Read-only;
    # resolve via /apply, /discard, /revert (which all stale-guard against
    # the file changing since the checkpoint was created).
    #
    # ?session_id=... is required. ?include_resolved=1 also returns the
    # already-applied (auto-mode) checkpoints so the chip can show e.g.
    # "1 pending, 3 applied" for context.
    # ------------------------------------------------------------------
    @router.get("/checkpoints")
    def list_checkpoints(
        request: Request,
        session_id: str = Query(..., min_length=1),
        include_resolved: bool = Query(default=False),
    ):
        get_current_user(request)
        from src.workspace_checkpoints import (
            count_pending_for_session,
            list_for_session,
        )
        return {
            "ok": True,
            "session_id": session_id,
            "pending_count": count_pending_for_session(session_id),
            "checkpoints": list_for_session(session_id, include_resolved=include_resolved),
        }

    # ------------------------------------------------------------------
    # Git panel (Phase 6) — shell `git` in the workspace root. The agent can
    # already drive git via `bash`; this is the human UI. Paths are confined to
    # the root before being handed to git as pathspecs.
    # ------------------------------------------------------------------
    def _git(root: str, args, timeout: int = 30):
        try:
            p = subprocess.run(["git", "-C", root] + args, capture_output=True,
                               text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", "git is not installed or not on PATH"
        except subprocess.TimeoutExpired:
            return 124, "", "git timed out"

    def _rel_in_root(root: str, path: str) -> str:
        return os.path.relpath(_confine(root, path), root)

    @router.get("/git/status")
    def git_status(request: Request):
        get_current_user(request)
        root = _root_or_409()
        rc, out, err = _git(root, ["status", "--porcelain=v1", "-b", "--untracked-files=all"])
        if rc == 127:
            raise HTTPException(status_code=500, detail=err)
        if rc != 0 and "not a git repository" in (err or "").lower():
            return {"is_repo": False, "branch": None, "files": []}
        branch, files = None, []
        for line in out.splitlines():
            if line.startswith("## "):
                branch = line[3:].split("...")[0].strip()
                continue
            if len(line) < 4:
                continue
            x, y, path = line[0], line[1], line[3:]
            if " -> " in path:  # rename: show the new name
                path = path.split(" -> ", 1)[1]
            files.append({
                "path": path, "x": x, "y": y,
                "staged": x not in (" ", "?"),
                "unstaged": (y != " ") or x == "?",
                "untracked": x == "?",
            })
        files.sort(key=lambda f: f["path"].lower())
        return {"is_repo": True, "branch": branch, "files": files}

    @router.get("/git/diff")
    def git_diff(request: Request, path: str = Query(default=""), staged: bool = Query(default=False)):
        get_current_user(request)
        root = _root_or_409()
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        if path.strip():
            args += ["--", _rel_in_root(root, path)]
        rc, out, err = _git(root, args)
        # Untracked files have no diff target; surface their content as an add.
        if not out.strip() and path.strip() and not staged:
            full = _confine(root, path)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        body = f.read(MAX_READ_CHARS)
                    out = "\n".join("+" + ln for ln in body.splitlines())
                except OSError:
                    pass
        return {"diff": out, "error": (err.strip() or None) if rc not in (0,) else None}

    class GitPathBody(BaseModel):
        path: str = ""

    @router.post("/git/stage")
    def git_stage(request: Request, body: GitPathBody):
        get_current_user(request)
        root = _root_or_409()
        args = ["add", "--", _rel_in_root(root, body.path)] if body.path.strip() else ["add", "-A"]
        rc, out, err = _git(root, args)
        return {"ok": rc == 0, "error": (err or out).strip() or None if rc != 0 else None}

    @router.post("/git/unstage")
    def git_unstage(request: Request, body: GitPathBody):
        get_current_user(request)
        root = _root_or_409()
        args = ["reset", "-q", "HEAD", "--", _rel_in_root(root, body.path)] if body.path.strip() else ["reset", "-q"]
        rc, out, err = _git(root, args)
        return {"ok": rc == 0, "error": (err or out).strip() or None if rc != 0 else None}

    class GitCommitBody(BaseModel):
        message: str

    @router.post("/git/commit")
    def git_commit(request: Request, body: GitCommitBody):
        get_current_user(request)
        root = _root_or_409()
        msg = (body.message or "").strip()
        if not msg:
            return {"ok": False, "error": "Commit message is required."}
        rc, out, err = _git(root, ["commit", "-m", msg])
        if rc != 0:
            return {"ok": False, "error": (err or out).strip() or "commit failed"}
        return {"ok": True, "output": out.strip()}

    # ------------------------------------------------------------------
    # Terminal (Phase 5) — owner-authed streaming shell, cwd = workspace root.
    # The existing /api/shell/stream is ADMIN-gated, which 403s in single-user
    # desktop mode (no auth middleware). Per plan decision 4 the in-app terminal
    # is owner-authed (single-user trusted) instead — same posture as the file
    # endpoints above — and the shell always starts in the chosen workspace.
    # ------------------------------------------------------------------
    class ShellBody(BaseModel):
        command: str
        timeout: int | None = None

    @router.post("/shell")
    async def workspace_shell(request: Request, body: ShellBody):
        get_current_user(request)  # owner-authed (NOT admin); confined cwd below
        root = _root_or_409()
        cmd = (body.command or "").strip()
        if not cmd:
            raise HTTPException(status_code=400, detail="command is required")
        timeout = body.timeout if (body.timeout and body.timeout > 0) else 300

        async def gen():
            from routes.shell_routes import _create_shell
            try:
                proc = await _create_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=root,
                )
            except Exception as e:  # spawn failure
                yield f"data: {json.dumps({'stream': 'stderr', 'data': str(e)})}\n\n"
                yield f"data: {json.dumps({'exit_code': -1})}\n\n"
                return
            q: asyncio.Queue = asyncio.Queue()

            async def _reader(stream, label):
                try:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        await q.put(("line", label, line.decode("utf-8", "replace").rstrip("\r\n")))
                finally:
                    await q.put(("eof", label, None))

            readers = [asyncio.create_task(_reader(proc.stdout, "stdout")),
                       asyncio.create_task(_reader(proc.stderr, "stderr"))]
            rc, eofs = None, 0
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            try:
                while eofs < 2:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        yield f"data: {json.dumps({'stream': 'stderr', 'data': f'Command timed out after {timeout}s'})}\n\n"
                        break
                    try:
                        kind, label, data = await asyncio.wait_for(q.get(), timeout=min(remaining, 30))
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        continue
                    if kind == "eof":
                        eofs += 1
                    else:
                        yield f"data: {json.dumps({'stream': label, 'data': data})}\n\n"
                        if await request.is_disconnected():
                            break
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=2)
                except (asyncio.TimeoutError, Exception):
                    rc = proc.returncode
            finally:
                for t in readers:
                    t.cancel()
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            yield f"data: {json.dumps({'exit_code': rc if rc is not None else -1})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # Interactive terminal — a real PTY over a WebSocket. This is what the
    # Terminal panel uses; it supersedes the one-shot POST /shell runner above
    # (kept for API back-compat). A persistent shell is attached to a
    # pseudo-terminal, so REPLs, full-screen TUIs, and long-running interactive
    # agents work exactly as in a native terminal.
    #
    # Wire protocol:
    #   client -> server  binary frame : raw keystroke bytes -> PTY stdin
    #                     text frame   : JSON control, {"type":"resize","cols":N,"rows":M}
    #                                    (or {"type":"input","data":"..."} as a text fallback)
    #   server -> client  binary frame : raw PTY output bytes
    #                     text frame   : JSON status, {"type":"exit","code":N|null}
    #                                    or {"type":"error","message":"..."}
    # The shell starts in the Code Workspace root (else the user's home).
    # ------------------------------------------------------------------
    @router.websocket("/pty")
    async def workspace_pty(ws: WebSocket):
        if not _pty_ws_allowed(ws):
            await ws.close(code=1008)  # policy violation
            return
        await ws.accept()

        def _qint(name: str, default: int) -> int:
            try:
                return max(1, min(1000, int(ws.query_params.get(name, default))))
            except (TypeError, ValueError):
                return default
        cols, rows = _qint("cols", 80), _qint("rows", 24)

        from core.pty_session import open_pty_session
        from routes.shell_routes import _shell_cwd
        try:
            sess = await open_pty_session(cwd=_shell_cwd(), cols=cols, rows=rows)
        except Exception as e:  # PTY spawn failed (e.g. pywinpty missing)
            try:
                await ws.send_text(json.dumps({"type": "error", "message": f"Could not start terminal: {e}"}))
                await ws.close(code=1011)
            except Exception:
                pass
            return

        async def pump_out():
            """PTY output -> client, until the shell exits or the socket drops."""
            try:
                while True:
                    data = await sess.read()
                    if data is None:
                        break
                    await ws.send_bytes(data)
            except Exception:
                pass
            try:
                await ws.send_text(json.dumps({"type": "exit", "code": sess.exit_code()}))
            except Exception:
                pass

        async def pump_in():
            """Client -> PTY: binary = keystrokes, text = JSON control message."""
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    data = msg.get("bytes")
                    if data is not None:
                        await sess.write(data)
                        continue
                    text = msg.get("text")
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except (ValueError, TypeError):
                        continue
                    kind = obj.get("type")
                    if kind == "resize":
                        sess.resize(obj.get("cols", cols), obj.get("rows", rows))
                    elif kind == "input":
                        await sess.write(str(obj.get("data", "")).encode("utf-8"))
            except (WebSocketDisconnect, RuntimeError):
                pass

        out_task = asyncio.create_task(pump_out())
        in_task = asyncio.create_task(pump_in())
        try:
            await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (out_task, in_task):
                t.cancel()
            sess.close()
            try:
                await ws.close()
            except Exception:
                pass

    return router
