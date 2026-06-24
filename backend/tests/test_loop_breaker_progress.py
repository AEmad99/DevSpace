"""Progress-aware loop-breaker: a repeated identical tool call must NOT be read
as "stuck" when the round is making progress — i.e. an effectful tool ran
(file edit) or the tool output changed. This is what lets a real coding loop
(edit→test→edit→test, reusing identical signatures) run to completion instead
of being force-stopped after 4 "repeats".

When the breaker DOES trip it forces a tool-free round, which discards the tool
call and ends the turn early (no `rounds_exhausted`). So: breaker tripped ⇒ no
`rounds_exhausted`; breaker stayed quiet ⇒ loop runs to the cap ⇒ `rounds_exhausted`.
"""
import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch(monkeypatch, exec_fn):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", exec_fn, raising=False)


def _run_loop(monkeypatch, tool_text, exec_fn, max_rounds=8, tools=None):
    async def _fake_stream(_candidates, messages, **kwargs):
        # No answer text — just the repeated tool call each round.
        yield f'data: {json.dumps({"delta": tool_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _patch(monkeypatch, exec_fn)
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "iterate until the tests pass"}],
        max_rounds=max_rounds,
        relevant_tools=tools or {"edit_file", "bash"},
    )
    return _types(_collect(gen))


def test_repeated_effectful_edit_is_not_stuck(monkeypatch):
    # Same edit_file call every round (identical signature) — but edit_file is
    # effectful, so the loop keeps going to the cap instead of force-stopping.
    async def _fake_exec(block, *a, **k):
        return ("edit_file", {"output": "edited", "exit_code": 0, "diff": "x"})
    events = _run_loop(monkeypatch, "```edit_file\n{\"path\":\"a.py\",\"old_string\":\"x\",\"new_string\":\"y\"}\n```",
                       _fake_exec, max_rounds=8, tools={"edit_file"})
    assert any(e.get("type") == "rounds_exhausted" for e in events), events


def test_repeated_call_with_changing_output_is_not_stuck(monkeypatch):
    # Same read-only signature every round, but output changes each time → progress.
    _n = {"i": 0}
    async def _fake_exec(block, *a, **k):
        _n["i"] += 1
        return ("bash", {"output": f"run {_n['i']}", "exit_code": 0})
    events = _run_loop(monkeypatch, "```bash\npytest\n```", _fake_exec, max_rounds=8, tools={"bash"})
    assert any(e.get("type") == "rounds_exhausted" for e in events), events


def test_true_noop_repeat_still_breaks(monkeypatch):
    # Same read-only call (read_file — not effectful), identical output, no text:
    # a genuine spin. The 4-round stall rule should trip and force a stop, so the
    # loop ends early with NO rounds_exhausted.
    async def _fake_exec(block, *a, **k):
        return ("read_file", {"output": "same contents", "exit_code": 0})
    events = _run_loop(monkeypatch, "```read_file\na.py\n```", _fake_exec, max_rounds=12, tools={"read_file"})
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events
