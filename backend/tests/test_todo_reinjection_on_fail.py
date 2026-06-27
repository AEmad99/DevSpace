"""HARNESS: TODO checklist re-injection on failure.

When the agent loop encounters a failure, re-injecting the open TODO
list gives the model its plan back so it can recover against the
checklist instead of inventing a new path. Three failure modes:

  1. Completion verifier FAIL (``_run_verifier_subagent`` returns issues).
  2. Edit tool failure (``edit_file`` / ``write_file`` returns exit != 0).
  3. Verify tool failure (``run_tests` / `lint` / `format`` returns exit != 0).

The helper functions in ``src.agent_harness`` produce the system-prompt
content for each mode; this test pins the shape so a future refactor
can't silently drop the checklist tail.
"""
from src.agent_harness import tool_fail_message, verifier_fail_message


# ── verifier_fail_message ─────────────────────────────────────────────────


def test_verifier_fail_lists_each_issue_with_dash():
    issues = ["tests didn't actually run", "missing error handling", "wrong file"]
    msg = verifier_fail_message(issues)
    for issue in issues:
        assert issue in msg
    assert msg.count("- ") >= len(issues)


def test_verifier_fail_starts_with_context_directive():
    msg = verifier_fail_message(["issue A"])
    # The directive must come BEFORE the issues so the model reads context first.
    head = msg.split("- ")[0]
    assert "verifier" in head.lower()
    assert "fixed" in head.lower() or "fix" in head.lower()


def test_verifier_fail_no_todos_omits_checklist_tail():
    msg = verifier_fail_message(["x"], todos_md="")
    assert "TODO list" not in msg
    assert "current TODO" not in msg


def test_verifier_fail_with_todos_includes_checklist_tail():
    todos = "- [x] done\n- [~] in progress\n- [ ] pending"
    msg = verifier_fail_message(["x"], todos_md=todos)
    assert "TODO list" in msg or "current TODO" in msg
    assert "- [x] done" in msg
    assert "- [ ] pending" in msg


def test_verifier_fail_with_whitespace_only_todos_omits_tail():
    msg = verifier_fail_message(["x"], todos_md="   \n  \n")
    assert "TODO list" not in msg


def test_verifier_fail_empty_issues_still_has_directive():
    msg = verifier_fail_message([])
    assert "verifier" in msg.lower()
    assert "Fix these now" in msg or "fix" in msg.lower()


# ── tool_fail_message ─────────────────────────────────────────────────────


def test_tool_fail_message_includes_tool_name_and_exit_code():
    msg = tool_fail_message("edit_file", 1)
    assert "edit_file" in msg
    assert "exit_code=1" in msg


def test_tool_fail_message_with_todos_includes_checklist():
    todos = "- [ ] step one\n- [ ] step two"
    msg = tool_fail_message("run_tests", 2, todos_md=todos)
    assert "run_tests" in msg
    assert "exit_code=2" in msg
    assert "- [ ] step one" in msg
    assert "- [ ] step two" in msg


def test_tool_fail_message_without_todos_omits_checklist():
    msg = tool_fail_message("lint", 1)
    assert "TODO" not in msg


def test_tool_fail_message_accepts_none_exit_code():
    msg = tool_fail_message("edit_file", None)
    assert "exit_code=None" in msg
    assert "edit_file" in msg


def test_tool_fail_message_does_not_double_separate_with_empty_todos():
    # Whitespace-only todos must NOT add a "TODOs below" tail (would look broken).
    msg = tool_fail_message("write_file", 1, todos_md="   \n")
    assert "TODO" not in msg
    assert "write_file" in msg
    assert "exit_code=1" in msg
