"""Workspace checkpoints: enumeration helpers (count/list pending, list all).

These back the new `edit_pending` SSE event and the "View session changes"
affordance in the UI. They read the same on-disk files that the apply /
discard / revert operations touch, so we exercise them end-to-end against a
real DATA_DIR by monkeypatching the module-level path.
"""
import json
import os
import tempfile

import pytest

import src.workspace_checkpoints as wc
from src.workspace_checkpoints import (
    capture_checkpoint,
    discard_checkpoint,
    stage_checkpoint,
    count_pending_for_session,
    list_for_session,
    list_pending_for_session,
)


@pytest.fixture
def tmp_cp_dir(monkeypatch, tmp_path):
    """Redirect DATA_DIR to a temp dir for the duration of the test."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(wc, "DATA_DIR", str(d))
    monkeypatch.setattr(wc, "_CP_DIR", os.path.join(str(d), "checkpoints"))
    return d


def test_count_pending_empty(tmp_cp_dir):
    assert count_pending_for_session("sess1") == 0
    assert list_pending_for_session("sess1") == []


def test_count_pending_ignores_other_sessions(tmp_cp_dir):
    stage_checkpoint("sess1", "/tmp/a.py", "old1", "new1")
    stage_checkpoint("sess2", "/tmp/b.py", "old2", "new2")
    assert count_pending_for_session("sess1") == 1
    assert count_pending_for_session("sess2") == 1
    assert count_pending_for_session("sess3") == 0


def test_count_pending_ignores_applied(tmp_cp_dir):
    # Applied (auto-mode) checkpoints are NOT pending; only staged ones count.
    capture_checkpoint("sess1", "/tmp/a.py", "old", "new")
    stage_checkpoint("sess1", "/tmp/b.py", "old", "new")
    assert count_pending_for_session("sess1") == 1
    assert len(list_for_session("sess1", include_resolved=True)) == 2
    assert len(list_for_session("sess1")) == 1


def test_list_pending_returns_files_only_for_session(tmp_cp_dir):
    stage_checkpoint("sess1", "/tmp/a.py", "old1", "new1")
    stage_checkpoint("sess1", "/tmp/b.py", "old2", "new2")
    stage_checkpoint("sess2", "/tmp/c.py", "old3", "new3")
    pending = list_pending_for_session("sess1")
    assert {p["file"] for p in pending} == {"a.py", "b.py"}
    for p in pending:
        assert p["checkpoint_id"]
        assert p["path"].endswith(p["file"])


def test_discard_removes_pending(tmp_cp_dir):
    cp = stage_checkpoint("sess1", "/tmp/a.py", "old", "new")
    assert count_pending_for_session("sess1") == 1
    assert discard_checkpoint(cp)["ok"] is True
    assert count_pending_for_session("sess1") == 0


# ── Auto-mode drain migration ─────────────────────────────────────────────
def test_drain_skips_when_strict(monkeypatch, tmp_cp_dir):
    # When the user is in strict mode, the drain must NOT touch staged edits
    # — those are awaiting their explicit Apply/Discard decision.
    import src.settings as s
    p = tmp_cp_dir / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "strict"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)
    cp = stage_checkpoint("sess1", "/tmp/a.py", "old", "new")
    # Reset the drain marker that the module-import side effect may have
    # already written — we want to drive the drain explicitly here.
    import src.workspace_checkpoints as wc
    if os.path.exists(wc._DRAIN_MARKER):
        os.remove(wc._DRAIN_MARKER)
    assert wc.drain_staged_for_auto_mode() == 0
    assert count_pending_for_session("sess1") == 1


def test_drain_applies_staged_when_auto(monkeypatch, tmp_cp_dir):
    # Auto mode (the default): the drain applies every staged checkpoint so
    # the previous strict-mode backlog doesn't linger forever once the user
    # opts back into auto. The actual write happens via apply_checkpoint().
    import src.settings as s
    p = tmp_cp_dir / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "auto"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)
    import src.workspace_checkpoints as wc
    if os.path.exists(wc._DRAIN_MARKER):
        os.remove(wc._DRAIN_MARKER)
    target = "/tmp/drain_target.py"
    open(target, "w").write("original\n")
    try:
        stage_checkpoint("sess1", target, "original", "drained-content")
        assert count_pending_for_session("sess1") == 1
        applied = wc.drain_staged_for_auto_mode()
        assert applied == 1
        # Edit landed on disk.
        assert open(target).read() == "drained-content"
        # Pending list now empty.
        assert count_pending_for_session("sess1") == 0
        # Marker file written so we never re-drain.
        assert os.path.exists(wc._DRAIN_MARKER)
        # Second call is a no-op (marker present).
        assert wc.drain_staged_for_auto_mode() == 0
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass


def test_drain_ignores_already_applied(monkeypatch, tmp_cp_dir):
    # Auto-mode checkpoints (capture_checkpoint) are NOT touched by the drain
    # — those edits are already on disk, so calling apply on them is a no-op
    # that would just fail with "This edit was already applied." The drain
    # should skip them entirely (count_pending_for_session only counts
    # staged=True metas).
    import src.settings as s
    p = tmp_cp_dir / "settings.json"
    p.write_text(json.dumps({"agent_edit_review": "auto"}))
    monkeypatch.setattr(s, "SETTINGS_FILE", str(p))
    monkeypatch.setattr(s, "_settings_cache", None)
    import src.workspace_checkpoints as wc
    if os.path.exists(wc._DRAIN_MARKER):
        os.remove(wc._DRAIN_MARKER)
    capture_checkpoint("sess1", "/tmp/applied.py", "old", "new")
    applied = wc.drain_staged_for_auto_mode()
    assert applied == 0  # nothing staged → nothing to apply
