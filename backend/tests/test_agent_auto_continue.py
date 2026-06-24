"""Auto-continue: when `auto_continue=True`, the per-message `max_rounds` becomes
a SOFT checkpoint. A still-working agent keeps going past it (emitting
`agent_continue` markers) up to the hard ceiling instead of stopping — this is the
"don't quit mid coding task" behaviour. With `auto_continue` off (the default for
internal callers), behaviour is unchanged: it stops at `max_rounds`.
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


def _patch_common(monkeypatch, ceiling=5):
    def _get_setting(key, default=None):
        if key == "agent_max_rounds_ceiling":
            return ceiling
        if key == "agent_auto_continue":
            return True
        return default
    monkeypatch.setattr(al, "get_setting", _get_setting, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2, auto_continue=False):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
        auto_continue=auto_continue,
    )
    return _types(_collect(gen))


def test_auto_continue_runs_past_soft_cap_to_ceiling(monkeypatch):
    _patch_common(monkeypatch, ceiling=5)
    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=2, auto_continue=True)
    # Crossed the soft cap → emitted at least one auto-continue marker.
    conts = [e for e in events if e.get("type") == "agent_continue"]
    assert conts, events
    # Ran all the way to the ceiling before reporting exhaustion (not max_rounds=2).
    exhausted = [e for e in events if e.get("type") == "rounds_exhausted"]
    assert exhausted and exhausted[0]["rounds"] == 5, events


def test_no_auto_continue_stops_at_soft_cap(monkeypatch):
    _patch_common(monkeypatch, ceiling=5)
    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=2, auto_continue=False)
    # No auto-continue markers, and it stops at max_rounds, not the ceiling.
    assert not [e for e in events if e.get("type") == "agent_continue"], events
    exhausted = [e for e in events if e.get("type") == "rounds_exhausted"]
    assert exhausted and exhausted[0]["rounds"] == 2, events


def test_auto_continue_still_finishes_on_done(monkeypatch):
    _patch_common(monkeypatch, ceiling=50)
    # A plain answer (no tool block) finishes on round 1 even with auto-continue on.
    events = _run_loop(monkeypatch, "All done.", max_rounds=2, auto_continue=True)
    assert not [e for e in events if e.get("type") == "rounds_exhausted"], events
    assert not [e for e in events if e.get("type") == "agent_continue"], events
