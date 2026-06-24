"""Workspace edit checkpoints — journal of file contents for diff approval / undo.

Two flavours, both keyed by a unique checkpoint id and stored under
DATA_DIR/checkpoints/:

  • captured (auto mode): the edit is already written to disk; we keep the OLD
    text so a later "Reject" can restore it, plus the new content's hash so we
    can refuse to revert if the file changed again since (stale guard).
  • staged (strict mode): the edit is NOT written; we keep both OLD and proposed
    NEW text so "Accept" can apply it or "Reject" can discard it.

Everything is stored and compared as TEXT normalised to "\n" line endings — the
file tools read/write in text mode (so on Windows on-disk bytes use "\r\n"),
and hashing/restoring as text keeps the stale-guard from tripping on the
platform's newline translation. Each checkpoint is <id>.json (metadata),
<id>.old (old text) and, for staged ones, <id>.new (proposed text).

All operations are best-effort and never raise into the edit path — a None id
means "no checkpoint" to callers.
"""
import hashlib
import json
import os
import time
import uuid
from typing import Optional

from src.constants import DATA_DIR

_CP_DIR = os.path.join(DATA_DIR, "checkpoints")


def _ensure_dir() -> None:
    os.makedirs(_CP_DIR, exist_ok=True)


def _meta_path(cid: str) -> str:
    return os.path.join(_CP_DIR, cid + ".json")


def _old_path(cid: str) -> str:
    return os.path.join(_CP_DIR, cid + ".old")


def _new_path(cid: str) -> str:
    return os.path.join(_CP_DIR, cid + ".new")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_text(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_meta(cid: str, meta: dict) -> None:
    with open(_meta_path(cid), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _read_meta(cid: str) -> Optional[dict]:
    # Guard the id so a caller-supplied value can't escape the checkpoint dir.
    if not cid or not cid.isalnum():
        return None
    try:
        with open(_meta_path(cid), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _str(x) -> str:
    return x if isinstance(x, str) else (x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or ""))


# --- capture / stage -------------------------------------------------------

def capture_checkpoint(session_id, path, old_content, new_content) -> Optional[str]:
    """Auto mode: the new content is already on disk. Persist the old text (to
    restore on Reject) and the new content hash (stale guard). Returns the id,
    or None if anything went wrong (the edit itself is unaffected)."""
    try:
        _ensure_dir()
        cid = _new_id()
        _write_text(_old_path(cid), _str(old_content))
        _write_meta(cid, {
            "id": cid, "session_id": session_id or None, "path": path,
            "staged": False, "created": time.time(),
            "old_sha": _sha(_str(old_content)), "new_sha": _sha(_str(new_content)),
        })
        return cid
    except OSError:
        return None


def stage_checkpoint(session_id, path, old_content, new_content) -> Optional[str]:
    """Strict mode: nothing is written to `path`. Persist both old and proposed
    new text so Accept can apply / Reject can discard. Returns the id or None."""
    try:
        _ensure_dir()
        cid = _new_id()
        _write_text(_old_path(cid), _str(old_content))
        _write_text(_new_path(cid), _str(new_content))
        _write_meta(cid, {
            "id": cid, "session_id": session_id or None, "path": path,
            "staged": True, "created": time.time(),
            "old_sha": _sha(_str(old_content)), "new_sha": _sha(_str(new_content)),
        })
        return cid
    except OSError:
        return None


def get_checkpoint(cid: str) -> Optional[dict]:
    return _read_meta(cid)


# --- resolve (Accept / Reject) --------------------------------------------

def _cleanup(cid: str) -> None:
    for p in (_meta_path(cid), _old_path(cid), _new_path(cid)):
        try:
            os.remove(p)
        except OSError:
            pass


def _current_text(path: str) -> str:
    try:
        return _read_text(path)
    except OSError:
        return ""


def revert_checkpoint(checkpoint_id: str) -> dict:
    """Reject an auto-applied edit: restore the old text. Stale-guarded — if the
    file no longer matches the content we wrote, refuse (it changed since)."""
    meta = _read_meta(checkpoint_id)
    if not meta:
        return {"ok": False, "error": "Checkpoint not found (already resolved?)."}
    if meta.get("staged"):
        return {"ok": False, "error": "This edit is staged, not applied — discard it instead."}
    path = meta.get("path")
    try:
        old = _read_text(_old_path(checkpoint_id))
    except OSError:
        return {"ok": False, "error": "Original content is missing; cannot revert."}
    if meta.get("new_sha") and _sha(_current_text(path)) != meta["new_sha"]:
        return {"ok": False, "stale": True,
                "error": "File changed since this edit — revert skipped to avoid losing newer changes."}
    try:
        _write_text(path, old)
    except OSError as e:
        return {"ok": False, "error": f"Could not restore file: {e}"}
    _cleanup(checkpoint_id)
    return {"ok": True, "path": path, "action": "reverted"}


def apply_checkpoint(checkpoint_id: str) -> dict:
    """Accept a staged (strict-mode) edit: write the proposed new text to disk."""
    meta = _read_meta(checkpoint_id)
    if not meta:
        return {"ok": False, "error": "Checkpoint not found (already resolved?)."}
    if not meta.get("staged"):
        return {"ok": False, "error": "This edit was already applied."}
    path = meta.get("path")
    try:
        new = _read_text(_new_path(checkpoint_id))
    except OSError:
        return {"ok": False, "error": "Staged content is missing; cannot apply."}
    try:
        _write_text(path, new)
    except OSError as e:
        return {"ok": False, "error": f"Could not write file: {e}"}
    _cleanup(checkpoint_id)
    return {"ok": True, "path": path, "action": "applied"}


def discard_checkpoint(checkpoint_id: str) -> dict:
    """Reject a staged (strict-mode) edit: drop it without touching the file."""
    meta = _read_meta(checkpoint_id)
    if not meta:
        return {"ok": False, "error": "Checkpoint not found (already resolved?)."}
    path = meta.get("path")
    _cleanup(checkpoint_id)
    return {"ok": True, "path": path, "action": "discarded"}


def revert_all_for_session(session_id: str) -> int:
    """Roll a whole session back: for every file the session edited (auto mode),
    restore the EARLIEST captured text (its pre-session state). Returns the
    number of files restored, then drops those checkpoints."""
    if not session_id:
        return 0
    try:
        ids = [n[:-5] for n in os.listdir(_CP_DIR) if n.endswith(".json")]
    except OSError:
        return 0
    metas = []
    for cid in ids:
        m = _read_meta(cid)
        if m and not m.get("staged") and m.get("session_id") == session_id:
            metas.append(m)
    metas.sort(key=lambda m: m.get("created", 0))  # oldest first
    earliest_by_path = {}
    for m in metas:
        earliest_by_path.setdefault(m["path"], m)  # first seen = earliest
    restored = 0
    for path, m in earliest_by_path.items():
        try:
            _write_text(path, _read_text(_old_path(m["id"])))
            restored += 1
        except OSError:
            continue
    for m in metas:
        _cleanup(m["id"])
    return restored


# --- enumeration helpers (used by the agent loop and the workspace API) ----

def _all_metas() -> list:
    """Read every checkpoint meta dict currently on disk. Returns [] on
    failure (best-effort; never raises). Skips unparseable files."""
    try:
        ids = [n[:-5] for n in os.listdir(_CP_DIR) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for cid in ids:
        m = _read_meta(cid)
        if m:
            out.append(m)
    return out


def list_for_session(session_id: str, include_resolved: bool = False) -> list:
    """Return checkpoints for a session. By default only STAGED (pending) ones
    are returned — that's what the UI needs to render "1 edit awaiting
    approval" indicators. Pass include_resolved=True to also get the auto-mode
    checkpoints that the agent already applied (used by the workspace "View
    session changes" affordance)."""
    if not session_id:
        return []
    out = []
    for m in _all_metas():
        if m.get("session_id") != session_id:
            continue
        if not include_resolved and not m.get("staged"):
            continue
        out.append({
            "checkpoint_id": m.get("id"),
            "path": m.get("path", ""),
            "staged": bool(m.get("staged")),
            "created": m.get("created", 0),
        })
    out.sort(key=lambda d: d.get("created", 0))
    return out


def count_pending_for_session(session_id: str) -> int:
    """Number of staged (unresolved) edits this session is currently waiting
    on the user to approve / discard. 0 when there are none."""
    if not session_id:
        return 0
    return sum(1 for m in _all_metas() if m.get("staged") and m.get("session_id") == session_id)


def list_pending_for_session(session_id: str) -> list:
    """Compact list of pending edits for the session — only the fields the
    input-bar chip needs (checkpoint_id, path, file basename)."""
    if not session_id:
        return []
    out = []
    for m in _all_metas():
        if not m.get("staged") or m.get("session_id") != session_id:
            continue
        path = m.get("path", "")
        out.append({
            "checkpoint_id": m.get("id"),
            "path": path,
            "file": os.path.basename(path) if path else "",
        })
    out.sort(key=lambda d: d.get("path", ""))
    return out


# --- one-shot drain --------------------------------------------------------

_DRAIN_MARKER = os.path.join(_CP_DIR, ".drained_for_auto_mode")


def drain_staged_for_auto_mode() -> int:
    """Best-effort one-shot migration for users who have switched to auto mode
    while a strict-mode staged edit is still waiting for approval.

    When ``agent_edit_review != "strict"``, the user has signalled that they
    want the agent's edits to land on disk immediately without per-step
    prompts — which is exactly what was staged for the previous strict-mode
    edit. We apply those staged edits on their behalf and remove the journal
    files. A marker file (``_DRAIN_MARKER``) makes this idempotent: we never
    drain more than once per install, even across reboots. Returns the number
    of checkpoints applied (0 if nothing to do or already drained).

    Auto-mode checkpoints (staged=False) are skipped — those edits are
    already on disk, so calling ``apply_checkpoint`` on them would just fail
    with "This edit was already applied."
    """
    try:
        from src.settings import get_setting
        if (get_setting("agent_edit_review") or "auto").strip().lower() == "strict":
            return 0
    except Exception:
        return 0
    try:
        if os.path.exists(_DRAIN_MARKER):
            return 0
        _ensure_dir()
    except OSError:
        return 0

    applied = 0
    for m in _all_metas():
        if not m.get("staged"):
            continue
        cid = m.get("id")
        if not cid:
            continue
        try:
            res = apply_checkpoint(cid)
            if res.get("ok"):
                applied += 1
        except Exception:
            continue

    try:
        with open(_DRAIN_MARKER, "w", encoding="utf-8") as f:
            f.write(f"drained {applied} staged checkpoint(s)\n")
    except OSError:
        pass
    if applied:
        try:
            import logging
            logging.getLogger(__name__).info(
                "workspace_checkpoints: drained %d staged edit(s) on auto-mode migration",
                applied,
            )
        except Exception:
            pass
    return applied


try:
    drain_staged_for_auto_mode()
except Exception:
    pass
