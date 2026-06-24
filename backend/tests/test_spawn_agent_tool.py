"""`spawn_agent` — delegate a self-contained sub-task to a focused nested agent.

The handler depth-guards (no sub-agents of sub-agents), refuses when disabled or
when no parent endpoint/model is available, and otherwise runs a nested
`stream_agent_loop` and returns its final report as the tool output.
"""
import asyncio
import json

from src.agent_tools import ToolBlock, TOOL_TAGS
from src.tool_execution import execute_tool_block
import src.tool_execution as te


def _run(content, **kw):
    return asyncio.run(execute_tool_block(ToolBlock("spawn_agent", content), **kw))


def test_depth_guard_blocks_nested_spawn():
    _, result = _run(json.dumps({"prompt": "do x"}),
                     agent_endpoint="http://x/v1", agent_model="m",
                     subagent_depth=1)
    assert result.get("exit_code") == 1
    assert "sub-agent" in result["error"].lower()


def test_missing_endpoint_is_graceful():
    _, result = _run(json.dumps({"prompt": "do x"}))
    assert result.get("exit_code") == 1
    assert "unavailable" in result["error"].lower()


def test_missing_prompt_rejected():
    _, result = _run(json.dumps({"description": "no prompt"}),
                     agent_endpoint="http://x/v1", agent_model="m")
    assert result.get("exit_code") == 1


def test_disabled_setting_refuses(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: False if key == "agent_subagents_enabled" else default,
                        raising=False)
    _, result = _run(json.dumps({"prompt": "do x"}),
                     agent_endpoint="http://x/v1", agent_model="m")
    assert result.get("exit_code") == 1
    assert "disabled" in result["error"].lower()


def test_successful_subagent_returns_report(monkeypatch):
    # Stub the nested loop so we don't hit a real model.
    async def _fake_loop(endpoint, model, messages, **kwargs):
        # The sub-agent must run at depth+1 and with a constrained toolset.
        assert kwargs.get("subagent_depth") == 1
        assert "spawn_agent" not in (kwargs.get("relevant_tools") or set())
        yield 'data: ' + json.dumps({"type": "tool_start", "tool": "grep"}) + '\n\n'
        yield 'data: ' + json.dumps({"delta": "Found it in auth.py."}) + '\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _fake_loop, raising=False)
    desc, result = _run(
        json.dumps({"description": "find auth", "prompt": "where is auth wired?", "agent_type": "explore"}),
        agent_endpoint="http://x/v1", agent_model="m",
    )
    assert result.get("exit_code") == 0
    assert "Found it in auth.py." in result["output"]
    assert result.get("subagent_report") is True
    assert "find auth" in desc


def test_registered_everywhere():
    assert "spawn_agent" in TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "spawn_agent" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
