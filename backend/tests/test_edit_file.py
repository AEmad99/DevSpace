"""edit_file: filesystem-write permission policy + behavior."""
import json
import os
import tempfile

import pytest

from src import tool_security
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    is_public_blocked_tool,
    blocked_tools_for_owner,
)
from src.agent_tools.filesystem_tools import EditFileTool, _edit_review_mode
from src.agent_tools import ToolBlock


# ── Permission policy ─────────────────────────────────────────────────────
def test_edit_file_is_sensitive_write_tool():
    # Must be blocked for non-admins exactly like write_file.
    assert "edit_file" in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool("edit_file") is True


def test_blocked_tools_for_owner_includes_edit_file_for_non_admin(monkeypatch):
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    blocked = blocked_tools_for_owner("bob")
    assert "edit_file" in blocked and "write_file" in blocked
    # Admin / single-user gets nothing blocked.
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    assert blocked_tools_for_owner("admin") == set()


@pytest.mark.asyncio
async def test_edit_file_blocked_at_execution_for_non_admin(monkeypatch):
    # Execution-level gate: a non-admin owner must be refused even if the tool
    # reaches execute_tool_block. edit_file stays admin-gated by tool_security
    # after #2684 (ALWAYS_AVAILABLE only changed advertisement, not execution).
    #
    # Resolve execute_tool_block from the live module object (te) rather than a
    # top-level import: other test modules pop src.tool_execution from
    # sys.modules and re-import it, so a stale top-level reference would call a
    # different module's function than the one monkeypatch targets — silently
    # bypassing the admin gate.
    import src.tool_execution as te
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: False)
    ws = tempfile.mkdtemp()
    p = os.path.join("/tmp", "ef_block.txt")
    open(p, "w").write("a\n")
    _desc, result = await te.execute_tool_block(
        ToolBlock("edit_file", json.dumps({"path": p, "old_string": "a", "new_string": "b"})),
        owner="bob",
    )
    assert result.get("exit_code") == 1 and "admin" in result.get("error", "").lower()
    os.unlink(p)


# ── Review-mode default ───────────────────────────────────────────────────
def test_default_review_mode_is_auto(monkeypatch):
    # The default value in src.settings.DEFAULT_SETTINGS drives _edit_review_mode
    # when no user override is present. Fresh installs get auto (silent apply)
    # so the agent's edits land on disk without per-step approval prompts;
    # existing installs that set it in data/settings.json keep their saved
    # value. Reset the settings cache to make sure we read the default.
    import src.settings as s
    monkeypatch.setattr(s, "_settings_cache", None)
    # Point load_settings at a non-existent file so the default wins.
    monkeypatch.setattr(s, "SETTINGS_FILE", "/nonexistent/devspace-test-settings.json")
    assert _edit_review_mode() == "auto"


def test_review_mode_honours_explicit_strict(monkeypatch, tmp_path):
    # An explicit "strict" in data/settings.json wins over the default —
    # this is the escape hatch for users who want the diff-then-approve flow.
    import src.settings as s
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "strict"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)
    assert _edit_review_mode() == "strict"


# ── Behavior (auto mode) ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_edit_file_success_auto_mode(monkeypatch, tmp_path):
    # Auto mode (the default) applies the edit directly to disk. The diff
    # payload carries review="auto" + checkpoint_id so the UI can render a
    # breadcrumb + Open-in-editor link instead of the strict-mode Approve bar.
    import src.settings as s
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "auto"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)

    fp = os.path.join("/tmp", "ef_ok.py")
    open(fp, "w").write("def f():\n    return 1\n")
    res = await EditFileTool().execute(json.dumps({"path": fp, "old_string": "return 1", "new_string": "return 2"}), {})
    assert res["exit_code"] == 0
    assert open(fp).read() == "def f():\n    return 2\n"
    diff = res.get("diff") or {}
    assert diff.get("review") == "auto"
    assert diff.get("staged") is False
    assert diff.get("added") == 1 and diff.get("removed") == 1 and diff.get("file") == "ef_ok.py"
    os.unlink(fp)


@pytest.mark.asyncio
async def test_edit_file_success_strict_mode(monkeypatch, tmp_path):
    # Strict mode: the edit is STAGED — nothing is written to disk — and the
    # returned diff carries staged=True + a checkpoint id so the UI can render
    # the Apply/Discard bar. We write an explicit settings file because the
    # default is now "auto".
    import src.settings as s
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "strict"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)

    fp = os.path.join("/tmp", "ef_strict.py")
    open(fp, "w").write("def f():\n    return 1\n")
    res = None
    try:
        res = await EditFileTool().execute(
            json.dumps({"path": fp, "old_string": "return 1", "new_string": "return 2"}),
            {},
        )
        assert res["exit_code"] == 0
        # Disk unchanged — the edit is staged, not applied.
        assert open(fp).read() == "def f():\n    return 1\n"
        # Output tells the agent the edit is pending; diff carries the
        # approval metadata the frontend needs.
        assert "STAGED" in res.get("output", "")
        diff = res.get("diff") or {}
        assert diff.get("staged") is True
        assert diff.get("checkpoint_id"), "strict mode must return a checkpoint id"
    finally:
        # Clean up the temp file and any staged checkpoint.
        try:
            os.unlink(fp)
        except OSError:
            pass
        if res is not None:
            cid = (res.get("diff") or {}).get("checkpoint_id")
            if cid:
                try:
                    from src.workspace_checkpoints import discard_checkpoint
                    discard_checkpoint(cid)
                except Exception:
                    pass


@pytest.mark.asyncio
async def test_edit_file_not_found():
    p = os.path.join("/tmp", "ef_nf.txt")
    open(p, "w").write("hello\n")
    res = await EditFileTool().execute(json.dumps({"path": p, "old_string": "nope", "new_string": "x"}), {})
    assert res["exit_code"] == 1 and "not found" in res["error"]
    os.unlink(p)


@pytest.mark.asyncio
async def test_edit_file_non_unique(monkeypatch, tmp_path):
    # Force auto mode so the second call (replace_all=True) actually writes
    # and we can read the result back from disk.
    import src.settings as s
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "auto"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)

    fp = os.path.join("/tmp", "ef_dup.txt")
    open(fp, "w").write("x\nx\n")
    res = await EditFileTool().execute(json.dumps({"path": fp, "old_string": "x", "new_string": "y"}), {})
    assert res["exit_code"] == 1 and "not unique" in res["error"]
    # replace_all resolves it
    res = await EditFileTool().execute(json.dumps({"path": fp, "old_string": "x", "new_string": "y", "replace_all": True}), {})
    assert res["exit_code"] == 0 and open(fp).read() == "y\ny\n"
    os.unlink(fp)


@pytest.mark.asyncio
async def test_edit_file_outside_allowed_roots():
    res = await EditFileTool().execute(json.dumps({"path": "/etc/hosts", "old_string": "x", "new_string": "y"}), {})
    assert res["exit_code"] == 1 and ("outside the allowed roots" in res["error"] or "sensitive" in res["error"])
