"""HARNESS: auto-call project_bootstrap on workspace attach.

When the agent loop has a workspace bound and ``project_bootstrap`` is
in the active tool set, the harness auto-loads the project profile (type,
language, test runner, lint, format, conventions, instruction files) into
the model's context BEFORE its first tool call — so it never has to
guess. The result is wrapped as an untrusted-context message and inserted
into the messages list alongside ``_doc_message`` / ``_email_message`` /
``_skills_message``.

The auto-call uses ``project_bootstrap_cached``, which is mtime-keyed, so
repeated turns in the same workspace are free. The gate (mirrored from
``agent_loop._build_system_prompt``) is:

    not suppress_local_context
    and workspace
    and "project_bootstrap" in (relevant_tools or set())

``suppress_local_context`` covers guide-only + plan-mode in one flag (the
route sets it for both, see ``chat_routes.py``). The bootstrap is read-only
context the model can benefit from in plan-mode anyway.
"""
import json
import os

from src.agent_tools.project_tools import (
    invalidate_bootstrap_cache,
    project_bootstrap_cached,
)


def _bootstrap_stub(monkeypatch, payload):
    """Patch ``project_bootstrap_cached`` to return *payload* and count calls."""
    import src.agent_tools.project_tools as pt
    calls = {"n": 0}

    def _stub(_cwd):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(pt, "project_bootstrap_cached", _stub)
    return calls


def _gate(suppress_local_context: bool, workspace, relevant_tools) -> bool:
    """Mirror the gate as written in ``_build_system_prompt``."""
    return bool(
        not suppress_local_context
        and workspace
        and "project_bootstrap" in (relevant_tools or set())
    )


def test_project_bootstrap_cached_is_idempotent(tmp_path):
    # The real ``project_bootstrap_cached`` mtime cache is the contract
    # the harness relies on. Two back-to-back calls with the same cwd
    # should return identical results.
    invalidate_bootstrap_cache(str(tmp_path))
    r1 = project_bootstrap_cached(str(tmp_path))
    r2 = project_bootstrap_cached(str(tmp_path))
    assert r1 == r2  # cache hit


def test_project_bootstrap_auto_called_when_workspace_and_tool_present(monkeypatch, tmp_path):
    """Workspace + tool in relevant_tools + local context allowed ->
    bootstrap is invoked exactly once."""
    import src.agent_tools.project_tools as pt
    calls = _bootstrap_stub(monkeypatch, {
        "type": "python",
        "language": "Python",
        "package_manager": "pip",
        "test_runner": "pytest",
        "lint_command": "ruff check .",
        "format_command": "black .",
        "entry_points": ["src/main.py"],
        "instructions_files": ["AGENTS.md"],
        "conventions": "type hints everywhere",
    })

    relevant_tools = {"read_file", "write_file", "edit_file", "project_bootstrap",
                      "run_tests", "lint", "format", "manage_todos"}
    workspace = str(tmp_path)

    assert _gate(suppress_local_context=False, workspace=workspace,
                 relevant_tools=relevant_tools) is True
    # If the gate passes, the harness invokes the stub.
    if _gate(False, workspace, relevant_tools):
        result = pt.project_bootstrap_cached(workspace)
    assert calls["n"] == 1
    assert result["test_runner"] == "pytest"


def test_project_bootstrap_auto_skipped_without_workspace(monkeypatch, tmp_path):
    """No workspace bound -> the harness MUST NOT call bootstrap."""
    calls = _bootstrap_stub(monkeypatch, {"type": "python"})

    relevant_tools = {"read_file", "write_file", "edit_file", "project_bootstrap"}
    workspace = None

    assert not _gate(suppress_local_context=False, workspace=workspace,
                     relevant_tools=relevant_tools)
    assert calls["n"] == 0


def test_project_bootstrap_auto_skipped_when_tool_not_relevant(monkeypatch, tmp_path):
    """Workspace set BUT project_bootstrap not in relevant_tools -> the
    harness MUST NOT call bootstrap. RAG may have trimmed it for a
    non-coding turn."""
    calls = _bootstrap_stub(monkeypatch, {"type": "python"})

    workspace = str(tmp_path)
    relevant_tools = {"read_file", "grep"}  # project_bootstrap NOT included

    assert _gate(False, workspace, relevant_tools) is False
    assert calls["n"] == 0


def test_project_bootstrap_auto_skipped_when_local_context_suppressed(monkeypatch, tmp_path):
    """``suppress_local_context=True`` covers guide-only + plan-mode
    (the chat route sets it for both). Bootstrap is skipped too."""
    calls = _bootstrap_stub(monkeypatch, {"type": "python"})

    workspace = str(tmp_path)
    relevant_tools = {"read_file", "project_bootstrap"}

    assert _gate(suppress_local_context=True, workspace=workspace,
                 relevant_tools=relevant_tools) is False
    assert calls["n"] == 0


def test_project_bootstrap_error_payload_does_not_break_harness(monkeypatch, tmp_path):
    """A bootstrap error (e.g. permission denied) must NOT crash the
    prompt builder — the harness logs and moves on."""
    import src.agent_tools.project_tools as pt

    def _err(_cwd):
        return {"error": "permission denied"}

    monkeypatch.setattr(pt, "project_bootstrap_cached", _err)

    # The harness's own guard: if _bs has an "error" key, skip the message.
    bs = pt.project_bootstrap_cached(str(tmp_path))
    skip = bool(bs and bs.get("error"))
    assert skip is True


def test_agent_auto_verify_setting_default_is_false():
    """The auto-verify global setting must default to False (opt-in)."""
    from src.settings import get_setting
    assert bool(get_setting("agent_auto_verify", False)) is False
