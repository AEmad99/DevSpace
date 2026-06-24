"""Tests for the _looks_like_coding_turn heuristic.

The agent's verifier subagent is gated by default-on for coding tasks
(workspace + code-ish message), default-off for personal-assistant use.
The heuristic is a small regex on the user message; the tests cover
the common shapes we care about and the shapes that must NOT trip it.
"""
from src.agent_loop import _looks_like_coding_turn


# ── coding-shape messages ──────────────────────────────────────────────────

def test_bug_fix_with_workspace_is_coding():
    assert _looks_like_coding_turn("fix the bug in foo.py", workspace="/repo") is True


def test_feature_request_with_workspace_is_coding():
    assert _looks_like_coding_turn("add a /search endpoint", workspace="/repo") is True


def test_refactor_with_workspace_is_coding():
    assert _looks_like_coding_turn("refactor the user model", workspace="/repo") is True


def test_test_request_with_workspace_is_coding():
    assert _looks_like_coding_turn("write tests for the new flow", workspace="/repo") is True


def test_deploy_request_with_workspace_is_coding():
    assert _looks_like_coding_turn("set up the docker build", workspace="/repo") is True


# ── no-workspace: only file extensions trip the heuristic ──────────────────

def test_workspace_file_extension_alone_is_coding():
    # Even without a workspace, a file-extension mention is a strong coding signal.
    assert _looks_like_coding_turn("edit src/foo.py", workspace=None) is True


def test_unknown_extension_alone_is_not_coding():
    # No workspace, no recognized extension — the heuristic stays conservative.
    assert _looks_like_coding_turn("open the foo.docx", workspace=None) is False


# ── personal-assistant shapes must NOT trip the heuristic ─────────────────

def test_email_request_is_not_coding():
    assert _looks_like_coding_turn("send an email to alice", workspace="/repo") is False


def test_calendar_request_is_not_coding():
    assert _looks_like_coding_turn("schedule a meeting for tomorrow", workspace="/repo") is False


def test_notes_request_is_not_coding():
    assert _looks_like_coding_turn("add a note about the lunch", workspace="/repo") is False


def test_weather_request_is_not_coding():
    assert _looks_like_coding_turn("what's the weather today?", workspace="/repo") is False


def test_chitchat_is_not_coding():
    assert _looks_like_coding_turn("thanks!", workspace="/repo") is False
    assert _looks_like_coding_turn("hello", workspace="/repo") is False


# ── edge cases ────────────────────────────────────────────────────────────

def test_empty_message_returns_false():
    assert _looks_like_coding_turn("", workspace="/repo") is False
    assert _looks_like_coding_turn("", workspace=None) is False


def test_no_workspace_no_extension_returns_false():
    # A message with a code-ish word but no workspace AND no extension is
    # not enough — the heuristic wants one of the two anchors.
    assert _looks_like_coding_turn("add a function", workspace=None) is False


def test_message_with_action_word_and_extension_is_coding():
    # Both anchors present — definitely a coding task.
    assert _looks_like_coding_turn("add a function in main.ts", workspace=None) is True
