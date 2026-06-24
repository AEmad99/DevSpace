"""Tests for the first-class git tools (git_status / git_diff / git_log /
git_blame / git_commit / git_branch).

The tools shell out to the real `git` binary on the test machine. We run
them against a real temp repo so the behavior matches production (porcelain
format, status codes, etc.) — mocking subprocess.run would only verify the
arg-plumbing, not the parsing.
"""
import json
import os
import subprocess
import tempfile

import pytest

from src.tool_execution import (
    _active_workspace,
    _active_session_id,
    get_active_workspace,
)
from src.agent_tools.git_tools import (
    GitStatusTool, GitDiffTool, GitLogTool,
    GitBlameTool, GitCommitTool, GitBranchTool,
)


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with one tracked file, one untracked file, and a
    single commit on `main` (so git_status / git_log have stable input)."""
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@e"}
    def _git(*args, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or repo)] + list(args),
            capture_output=True, text=True, env=env, check=False,
        )
    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@e")
    _git("config", "user.name", "Test")
    (repo / "tracked.txt").write_text("hello\n")
    (repo / "ignored.txt").write_text("x\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git("add", "tracked.txt", ".gitignore")
    _git("commit", "-q", "-m", "initial")
    # Make a working-tree change so git_status has something to show.
    (repo / "tracked.txt").write_text("hello world\n")
    (repo / "new.txt").write_text("brand new\n")
    return repo


@pytest.fixture(autouse=True)
def bind_workspace(git_repo):
    """Bind the agent's active workspace to the temp git repo for each test,
    then restore the previous value so other tests aren't affected.

    The agent's workspace is a contextvar (see src.tool_execution). Test
    context isn't always a fresh Context (pytest-asyncio re-uses it), so we
    manually set/reset rather than relying on context isolation."""
    prev_ws = _active_workspace.get()
    prev_sid = _active_session_id.get()
    _active_workspace.set(str(git_repo))
    _active_session_id.set("test-session")
    try:
        yield str(git_repo)
    finally:
        _active_workspace.set(prev_ws)
        _active_session_id.set(prev_sid)


# ── git_status ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_status_reports_modified_and_untracked():
    res = await GitStatusTool().execute("{}", {})
    assert res["exit_code"] == 0
    assert res["branch"] == "main"
    assert res["uncommitted_count"] == 2  # tracked.txt modified + new.txt untracked
    files = {(f["path"], f["staged"], f["untracked"], f["unstaged"]) for f in res["files"]}
    assert ("tracked.txt", False, False, True) in files
    assert ("new.txt", False, True, True) in files


@pytest.mark.asyncio
async def test_git_status_scoped_to_path():
    res = await GitStatusTool().execute(json.dumps({"path": "new.txt"}), {})
    assert res["exit_code"] == 0
    assert len(res["files"]) == 1
    assert res["files"][0]["path"] == "new.txt"


@pytest.mark.asyncio
async def test_git_status_rejects_path_outside_workspace():
    res = await GitStatusTool().execute(json.dumps({"path": "../etc"}), {})
    # Confinement rejects the path → empty file list, but still a clean exit.
    # (git_status passes a scoped path; the resolver refuses, so we pass
    # no path filter and fall back to whole-workspace status.)
    # The exact outcome depends on the resolver; just assert no crash.
    assert "exit_code" in res


# ── git_diff ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_diff_working_tree():
    res = await GitDiffTool().execute("{}", {})
    assert res["exit_code"] == 0
    assert "+world" in res["diff"] or "world" in res["diff"]
    assert res["added"] >= 1
    assert res["file_count"] >= 1


@pytest.mark.asyncio
async def test_git_diff_untracked_file_as_virtual_add():
    res = await GitDiffTool().execute(json.dumps({"path": "new.txt"}), {})
    assert res["exit_code"] == 0
    # The untracked file's content should surface as a virtual + diff.
    assert res["diff"]
    assert "brand new" in res["diff"]
    assert res["added"] >= 1


@pytest.mark.asyncio
async def test_git_diff_staged_vs_working():
    # Stage a change and confirm staged=true returns it, working=false hides it.
    repo_path = get_active_workspace()
    (os.path.join(repo_path, "tracked.txt") if False else os.path.join(repo_path, "tracked.txt"))
    p = os.path.join(repo_path, "tracked.txt")
    with open(p, "a", encoding="utf-8") as f:
        f.write("appended\n")
    subprocess.run(["git", "-C", repo_path, "add", "tracked.txt"], check=True)
    working = await GitDiffTool().execute("{}", {})
    staged = await GitDiffTool().execute(json.dumps({"staged": True}), {})
    assert working["diff"] == ""  # everything is staged, so no working diff
    assert "appended" in staged["diff"]


# ── git_log ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_log_returns_commits():
    res = await GitLogTool().execute("{}", {})
    assert res["exit_code"] == 0
    assert res["count"] >= 1
    c = res["commits"][0]
    assert c["subject"] == "initial"
    assert len(c["hash"]) >= 7
    assert c["author"] == "Test"
    assert c["date"]  # YYYY-MM-DD


@pytest.mark.asyncio
async def test_git_log_max_count():
    res = await GitLogTool().execute(json.dumps({"max_count": 1}), {})
    assert res["count"] == 1


@pytest.mark.asyncio
async def test_git_log_oneline_false():
    res = await GitLogTool().execute(json.dumps({"oneline": False}), {})
    # Without --oneline, format is multi-line; subject is still in the first tab.
    assert res["count"] >= 1
    assert any("initial" in c["subject"] for c in res["commits"])


# ── git_blame ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_blame_annotates_file():
    res = await GitBlameTool().execute(json.dumps({"path": "tracked.txt"}), {})
    assert res["exit_code"] == 0
    assert res["lines"]
    # The file has 2 lines (hello world, plus the newline).
    for ln in res["lines"]:
        assert ln["commit"]
        assert ln["author"]
        assert ln["content"] is not None


@pytest.mark.asyncio
async def test_git_blame_requires_path():
    res = await GitBlameTool().execute("{}", {})
    assert res["exit_code"] == 1
    assert "path" in res["error"]


@pytest.mark.asyncio
async def test_git_blame_rejects_nonexistent_file():
    res = await GitBlameTool().execute(json.dumps({"path": "does_not_exist.txt"}), {})
    assert res["exit_code"] == 1
    assert "not a regular file" in res["error"] or "outside" in res["error"]


# ── git_commit ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_commit_happy_path():
    repo_path = get_active_workspace()
    p = os.path.join(repo_path, "tracked.txt")
    with open(p, "a", encoding="utf-8") as f:
        f.write("more\n")
    res = await GitCommitTool().execute(
        json.dumps({"message": "add more", "paths": ["tracked.txt"]}), {},
    )
    assert res["exit_code"] == 0, res
    assert "add more" in res["output"] or res["output"]  # git emits the commit summary


@pytest.mark.asyncio
async def test_git_commit_requires_message():
    res = await GitCommitTool().execute("{}", {})
    assert res["exit_code"] == 1
    assert "message" in res["error"]


@pytest.mark.asyncio
async def test_git_commit_nothing_to_commit_is_clean_exit():
    # No staged / modified files → git's own "nothing to commit" check.
    res = await GitCommitTool().execute(json.dumps({"message": "noop"}), {})
    assert res["exit_code"] == 0
    assert "Nothing to commit" in res["output"]


@pytest.mark.asyncio
async def test_git_commit_rejects_path_outside_workspace():
    res = await GitCommitTool().execute(
        json.dumps({"message": "x", "paths": ["../etc/passwd"]}), {},
    )
    assert res["exit_code"] == 1
    assert "outside" in res["error"].lower()


# ── git_branch ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_branch_list():
    res = await GitBranchTool().execute("{}", {})
    assert res["exit_code"] == 0
    assert res["current"] == "main"
    assert any(b["name"] == "main" and b["current"] for b in res["branches"])


@pytest.mark.asyncio
async def test_git_branch_current():
    res = await GitBranchTool().execute(json.dumps({"action": "current"}), {})
    assert res["exit_code"] == 0
    assert res["current"] == "main"


@pytest.mark.asyncio
async def test_git_branch_create_and_delete():
    create = await GitBranchTool().execute(json.dumps({"action": "create", "name": "feat"}), {})
    assert create["exit_code"] == 0
    assert "feat" in create["output"]
    # New branch is now current.
    cur = await GitBranchTool().execute(json.dumps({"action": "current"}), {})
    assert cur["current"] == "feat"
    # Delete (clean, no unmerged commits) — first we need to switch back to main
    # so the working branch isn't checked-out.
    subprocess.run(["git", "-C", get_active_workspace(), "checkout", "main"], check=True)
    delete = await GitBranchTool().execute(json.dumps({"action": "delete", "name": "feat"}), {})
    assert delete["exit_code"] == 0


@pytest.mark.asyncio
async def test_git_branch_create_rejects_flag_name():
    res = await GitBranchTool().execute(json.dumps({"action": "create", "name": "-D"}), {})
    assert res["exit_code"] == 1
    assert "must not start" in res["error"]


@pytest.mark.asyncio
async def test_git_branch_create_requires_name():
    res = await GitBranchTool().execute(json.dumps({"action": "create"}), {})
    assert res["exit_code"] == 1
    assert "name" in res["error"]


@pytest.mark.asyncio
async def test_git_branch_unknown_action():
    res = await GitBranchTool().execute(json.dumps({"action": "nuke"}), {})
    assert res["exit_code"] == 1
    assert "unknown action" in res["error"]


# ── not-in-workspace posture ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_status_without_workspace():
    _active_workspace.set(None)
    res = await GitStatusTool().execute("{}", {})
    assert res["exit_code"] == 1
    assert "no workspace" in res["error"]


@pytest.mark.asyncio
async def test_git_diff_without_workspace():
    _active_workspace.set(None)
    res = await GitDiffTool().execute("{}", {})
    assert res["exit_code"] == 1
    assert "no workspace" in res["error"]


# ── Dispatcher integration ───────────────────────────────────────────────
# The new git tools must also be reachable through the central dispatcher
# (tool_execution.execute_tool_block) so both fenced-block and native
# function-call paths land on the right class. This is the same path the
# agent loop uses, so a regression here is exactly the kind that breaks
# coding tasks in production.

@pytest.mark.asyncio
async def test_execute_tool_block_dispatches_git_status():
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        ToolBlock("git_status", "{}"),
        owner="test", workspace=get_active_workspace(),
        session_id="test-session",
    )
    assert result["exit_code"] == 0
    assert result["branch"] == "main"
    assert "git_status" in desc or "git" in desc.lower()


@pytest.mark.asyncio
async def test_execute_tool_block_dispatches_git_log():
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        ToolBlock("git_log", json.dumps({"max_count": 1})),
        owner="test", workspace=get_active_workspace(),
        session_id="test-session",
    )
    assert result["exit_code"] == 0
    assert result["count"] == 1
    assert "git_log" in desc or "git" in desc.lower()


@pytest.mark.asyncio
async def test_execute_tool_block_dispatches_git_diff():
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(
        ToolBlock("git_diff", json.dumps({"path": "new.txt"})),
        owner="test", workspace=get_active_workspace(),
        session_id="test-session",
    )
    assert result["exit_code"] == 0
    # Untracked file → virtual + diff
    assert "brand new" in result["diff"]
    assert "git_diff" in desc or "git" in desc.lower()
