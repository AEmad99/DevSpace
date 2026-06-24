"""`manage_todos` — the agent's own working checklist for a multi-step task.

Pure UI + state marker: `execute_tool_block` returns a `todo_update` payload the
agent loop turns into a `todo_update` SSE event (and persists for re-injection at
auto-continue checkpoints). No I/O, does not end the turn.
"""
import asyncio
import json

from src.agent_tools import ToolBlock, TOOL_TAGS  # import first to avoid circular
from src.tool_execution import execute_tool_block
from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
from src.tool_security import is_public_blocked_tool


def _run(content):
    return asyncio.run(execute_tool_block(ToolBlock("manage_todos", content)))


def test_structured_list_returns_marker_and_counts():
    todos = [
        {"text": "explore the loop", "status": "done"},
        {"text": "edit the breaker", "status": "in_progress"},
        {"text": "run tests", "status": "pending"},
    ]
    desc, result = _run(json.dumps({"todos": todos}))
    assert result.get("exit_code") == 0
    out = result["todo_update"]["todos"]
    assert [t["status"] for t in out] == ["done", "in_progress", "pending"]
    assert "1/3" in result["output"]
    md = result["todo_update"]["markdown"]
    assert "- [x] explore the loop" in md
    assert "- [~] edit the breaker" in md
    assert "- [ ] run tests" in md


def test_markdown_checklist_accepted():
    md = "- [x] a\n- [~] b\n- [ ] c"
    _, result = _run(md)
    statuses = [t["status"] for t in result["todo_update"]["todos"]]
    assert statuses == ["done", "in_progress", "pending"]


def test_empty_rejected():
    _, result = _run(json.dumps({"todos": []}))
    assert "error" in result and result.get("exit_code") == 1


def test_capped_at_40_items():
    todos = [{"text": f"step {i}", "status": "pending"} for i in range(60)]
    _, result = _run(json.dumps({"todos": todos}))
    assert len(result["todo_update"]["todos"]) == 40


def test_registered_everywhere():
    assert "manage_todos" in TOOL_TAGS
    assert "manage_todos" in ALWAYS_AVAILABLE
    assert "manage_todos" in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "manage_todos" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert is_public_blocked_tool("manage_todos") is False
