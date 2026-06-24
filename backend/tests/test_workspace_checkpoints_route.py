"""Tests for the new GET /api/workspace/checkpoints endpoint.

This backs the strict-mode edit review UX: the input-bar pending chip and
the "View session changes" affordance on the assistant message. The endpoint
is read-only — resolve via /apply, /discard, /revert — and the data it returns
must match what's on disk in the checkpoint store.
"""
import os

import pytest
from fastapi.testclient import TestClient

import src.workspace_checkpoints as wc


@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    """Redirect DATA_DIR to a temp dir so checkpoint files don't pollute
    the real data dir, and so each test starts from a clean state."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(wc, "DATA_DIR", str(d))
    monkeypatch.setattr(wc, "_CP_DIR", os.path.join(str(d), "checkpoints"))
    return d


@pytest.fixture
def client(monkeypatch):
    """Build a FastAPI TestClient with the code workspace router mounted.
    Single-user mode (AUTH_ENABLED=false) so we don't have to forge cookies."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from fastapi import FastAPI
    from routes.code_workspace_routes import setup_code_workspace_routes
    app = FastAPI()
    app.include_router(setup_code_workspace_routes())
    return TestClient(app)


def test_list_checkpoints_empty(tmp_data_dir, client):
    r = client.get("/api/workspace/checkpoints", params={"session_id": "sess1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["pending_count"] == 0
    assert body["checkpoints"] == []


def test_list_checkpoints_returns_staged_only_by_default(tmp_data_dir, client):
    # Stage one pending edit, apply another, and capture an auto-mode edit.
    cp_pending = wc.stage_checkpoint("sess1", "/tmp/a.py", "old1", "new1")
    cp_staged = wc.stage_checkpoint("sess1", "/tmp/b.py", "old2", "new2")
    wc.apply_checkpoint(cp_staged)  # now applied (no longer pending)
    wc.capture_checkpoint("sess1", "/tmp/c.py", "old3", "new3")  # auto-mode

    r = client.get("/api/workspace/checkpoints", params={"session_id": "sess1"})
    assert r.status_code == 200
    body = r.json()
    # Only staged+unresolved show by default.
    assert body["pending_count"] == 1
    assert len(body["checkpoints"]) == 1
    assert body["checkpoints"][0]["checkpoint_id"] == cp_pending
    assert body["checkpoints"][0]["staged"] is True
    assert body["checkpoints"][0]["path"] == "/tmp/a.py"


def test_list_checkpoints_include_resolved(tmp_data_dir, client):
    cp_pending = wc.stage_checkpoint("sess1", "/tmp/a.py", "old1", "new1")
    cp_staged = wc.stage_checkpoint("sess1", "/tmp/b.py", "old2", "new2")
    wc.apply_checkpoint(cp_staged)  # applied-staged: cleaned up from disk
    wc.capture_checkpoint("sess1", "/tmp/c.py", "old3", "new3")  # auto-mode, kept

    r = client.get(
        "/api/workspace/checkpoints",
        params={"session_id": "sess1", "include_resolved": "1"},
    )
    assert r.status_code == 200
    body = r.json()
    # 1 pending (a.py) + 1 captured (c.py). The applied-staged one is gone
    # (apply cleans up its files), so total = 2.
    assert body["pending_count"] == 1  # count is always pending-only
    assert len(body["checkpoints"]) == 2
    staged_flags = {c["staged"] for c in body["checkpoints"]}
    assert staged_flags == {True, False}


def test_list_checkpoints_scoped_to_session(tmp_data_dir, client):
    """Other sessions' checkpoints must never leak in."""
    wc.stage_checkpoint("sess1", "/tmp/a.py", "old1", "new1")
    wc.stage_checkpoint("sess2", "/tmp/b.py", "old2", "new2")
    wc.stage_checkpoint("sess3", "/tmp/c.py", "old3", "new3")

    r = client.get("/api/workspace/checkpoints", params={"session_id": "sess2"})
    body = r.json()
    assert body["pending_count"] == 1
    assert len(body["checkpoints"]) == 1
    assert body["checkpoints"][0]["path"] == "/tmp/b.py"


def test_list_checkpoints_requires_session_id(tmp_data_dir, client):
    r = client.get("/api/workspace/checkpoints")
    # FastAPI returns 422 for missing required query param.
    assert r.status_code == 422
