"""Tests for the Anthropic-style bare-JSON tool-call parser.

A model trained on the Anthropic `tool_use` block shape (e.g. MiniMax-M3)
emits tool calls as plain text when no native `tool_calls` channel is
available. The text format looks like:

    {
      "name": "web_search",
      "input": {
        "query": "..."
      }
    }

`tool_parsing.parse_tool_blocks` should recover that as a ToolBlock and
`strip_tool_blocks` should drop the raw JSON from the user-visible text,
so the user sees the tool result rather than the JSON that produced it.
"""
import json

import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import (
    _anthropic_object_to_tool_block,
    _scan_anthropic_tool_objects,
    parse_tool_blocks,
    strip_tool_blocks,
)


# ---------------------------------------------------------------------------
# _anthropic_object_to_tool_block
# ---------------------------------------------------------------------------

def test_anthropic_web_search_with_input_key_converts_to_tool_block():
    block = _anthropic_object_to_tool_block(
        {"name": "web_search", "input": {"query": "MiniMax revenue 2025 2026"}}
    )
    assert block is not None
    assert block.tool_type == "web_search"
    # Bare query string when no time_filter — matches function_call_to_tool_block
    # convention for the simpler web_search shape.
    assert "MiniMax revenue 2025 2026" in block.content


def test_anthropic_web_search_with_time_filter_serialises_as_json():
    """time_filter forces a JSON content payload so the executor parses it."""
    block = _anthropic_object_to_tool_block(
        {
            "name": "web_search",
            "input": {"query": "OpenAI valuation", "time_filter": "week"},
        }
    )
    assert block is not None
    assert block.tool_type == "web_search"
    payload = json.loads(block.content)
    assert payload["query"] == "OpenAI valuation"
    assert payload["time_filter"] == "week"


def test_anthropic_arguments_alias_is_accepted():
    """Some emitters use `arguments` (OpenAI-style) instead of `input`."""
    block = _anthropic_object_to_tool_block(
        {"name": "web_search", "arguments": {"query": "anything"}}
    )
    assert block is not None
    assert block.tool_type == "web_search"


def test_anthropic_manage_session_call_uses_canonical_converter():
    """Non-trivial tools must round-trip through function_call_to_tool_block."""
    block = _anthropic_object_to_tool_block(
        {"name": "manage_session", "input": {"action": "list"}}
    )
    assert block is not None
    assert block.tool_type == "manage_session"
    # `list` is the only action that takes an optional keyword filter; the
    # canonical converter produces a 2-line content "list\n<filter>".
    assert block.content.startswith("list")


def test_anthropic_unknown_tool_name_returns_none():
    block = _anthropic_object_to_tool_block(
        {"name": "not_a_real_tool", "input": {"query": "x"}}
    )
    assert block is None


def test_anthropic_extra_keys_rejected():
    """A normal JSON object with `name` + unrelated keys must not match."""
    block = _anthropic_object_to_tool_block(
        {"name": "John", "age": 30, "city": "NYC"}
    )
    assert block is None


def test_anthropic_missing_input_or_arguments_returns_none():
    block = _anthropic_object_to_tool_block({"name": "web_search"})
    assert block is None


def test_anthropic_input_must_be_dict():
    block = _anthropic_object_to_tool_block(
        {"name": "web_search", "input": "raw string not a dict"}
    )
    assert block is None


def test_anthropic_empty_input_dict_returns_none():
    block = _anthropic_object_to_tool_block({"name": "web_search", "input": {}})
    assert block is None


def test_anthropic_non_string_name_returns_none():
    block = _anthropic_object_to_tool_block({"name": 123, "input": {"query": "x"}})
    assert block is None


# ---------------------------------------------------------------------------
# parse_tool_blocks (the public entry point)
# ---------------------------------------------------------------------------

def test_parse_tool_blocks_recovers_anthropic_call_from_screenshot():
    """The exact pattern from the bug report: model emits two web_search
    calls as bare JSON in chat mode. Both must convert to ToolBlocks."""
    text = (
        "I'll search the web for recent reporting on MiniMax's revenue and "
        "financial position.\n\n"
        '{\n  "name": "web_search",\n  "input": {\n    "query": "MiniMax AI lab revenue 2025 2026"\n  }\n}\n\n'
        '{\n  "name": "web_search",\n  "input": {\n    "query": "MiniMax AI company funding valuation profitability"\n  }\n}\n'
    )
    blocks = parse_tool_blocks(text)
    assert len(blocks) == 2
    assert all(b.tool_type == "web_search" for b in blocks)
    assert "MiniMax AI lab revenue 2025 2026" in blocks[0].content
    assert "MiniMax AI company funding valuation profitability" in blocks[1].content


def test_parse_tool_blocks_anthropic_call_active_even_with_skip_fenced():
    """Anthropic-shape recovery is NOT gated by `skip_fenced` — the JSON
    shape isn't an illustrative example a native model would write into
    prose. Even when the agent loop trusts native calls over fenced code
    blocks, an Anthropic JSON tool call must still convert."""
    text = '{"name": "web_search", "input": {"query": "x"}}'
    blocks = parse_tool_blocks(text, skip_fenced=True)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "web_search"


def test_parse_tool_blocks_anthropic_call_lower_priority_than_fenced():
    """If a fenced block already converted, the bare JSON sibling is dropped
    (no double execution). Mirrors the gating of Patterns 2-6."""
    text = (
        '```web_search\nfoo bar\n```\n\n'
        '{"name": "web_search", "input": {"query": "from json"}}'
    )
    blocks = parse_tool_blocks(text)
    # Fenced block matches first; the JSON sibling must NOT also fire.
    assert len(blocks) == 1
    assert "foo bar" in blocks[0].content


def test_parse_tool_blocks_no_match_for_unrelated_json():
    """A JSON object with unrelated keys should be ignored — it's not a tool call."""
    text = '{"status": "ok", "data": [1, 2, 3]}'
    assert parse_tool_blocks(text) == []


# ---------------------------------------------------------------------------
# strip_tool_blocks
# ---------------------------------------------------------------------------

def test_strip_tool_blocks_removes_anthropic_json_from_visible_text():
    """The user should see the prose, not the raw JSON that became a tool call."""
    text = (
        "I'll search the web for recent reporting on MiniMax's revenue and "
        'financial position.\n\n'
        '{\n  "name": "web_search",\n  "input": {\n    "query": "MiniMax AI lab revenue 2025 2026"\n  }\n}\n'
    )
    cleaned = strip_tool_blocks(text)
    assert "MiniMax revenue" in cleaned or "financial position" in cleaned
    assert '"name": "web_search"' not in cleaned
    assert '"input"' not in cleaned


def test_strip_tool_blocks_removes_multiple_anthropic_calls():
    text = (
        "Some prose.\n"
        '{"name": "web_search", "input": {"query": "first"}}\n'
        "More prose.\n"
        '{"name": "web_search", "input": {"query": "second"}}\n'
    )
    cleaned = strip_tool_blocks(text)
    assert "Some prose" in cleaned
    assert "More prose" in cleaned
    assert '"name"' not in cleaned
    assert "first" not in cleaned
    assert "second" not in cleaned


def test_strip_tool_blocks_keeps_unrelated_json():
    """Don't accidentally strip normal JSON the user wants to see."""
    text = 'Server returned: {"status": "ok"}'
    cleaned = strip_tool_blocks(text)
    assert '"status": "ok"' in cleaned


# ---------------------------------------------------------------------------
# _scan_anthropic_tool_objects
# ---------------------------------------------------------------------------

def test_scan_anthropic_returns_empty_for_unrelated_text():
    assert _scan_anthropic_tool_objects("just some text, no tools") == []


def test_scan_anthropic_returns_offsets_correctly():
    """Stripper uses the offsets to splice. Verify they cover the JSON exactly."""
    text = 'before {"name": "web_search", "input": {"query": "q"}} after'
    matches = _scan_anthropic_tool_objects(text)
    assert len(matches) == 1
    block, (start, end) = matches[0]
    assert block.tool_type == "web_search"
    assert text[start:end].startswith("{")
    assert text[start:end].endswith("}")
    assert text[:start].strip() == "before"
    assert text[end:].strip() == "after"
