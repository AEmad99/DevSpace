"""HARNESS: sub-agent prompts must differ by agent_type.

The ``spawn_agent`` tool dispatches to a focused nested agent loop with a
toolset constrained by ``agent_type`` (explore / code / general). Each type
should also get a tailored system prompt so the sub-agent knows what kind
of report to return:

  - ``explore``  — READ-ONLY investigator. No write tools; structured
    findings report (files inspected, key findings, suggested next steps).
  - ``code``     — IMPLEMENTATION agent. Edit→test→fix until green;
    structured implementation report (files changed, test results,
    follow-ups).
  - ``general``  — generic single-purpose sub-agent (original wording).

This test pins the lookup table and the lookup helper so a refactor can't
collapse the three prompts back into one generic block.
"""
from src.tool_execution import (
    _SUBAGENT_SYSTEMS,
    _subagent_system_for,
)


def test_three_distinct_agent_types():
    assert set(_SUBAGENT_SYSTEMS.keys()) == {"explore", "code", "general"}


def test_explore_prompt_is_read_only_investigator():
    p = _subagent_system_for("explore")
    assert "READ-ONLY" in p
    assert "findings report" in p.lower()
    # Explicitly NOT instructing the explore agent to write code (the
    # toolset already enforces it; the prompt should match).
    assert "implementation" not in p.lower()


def test_code_prompt_is_implementation_with_verify_loop():
    p = _subagent_system_for("code")
    assert "IMPLEMENTATION" in p
    assert "run_tests" in p
    assert "implementation report" in p.lower()


def test_general_prompt_is_generic():
    p = _subagent_system_for("general")
    assert "READ-ONLY" not in p
    assert "IMPLEMENTATION" not in p


def test_unknown_type_falls_back_to_general():
    assert _subagent_system_for("unknown") == _subagent_system_for("general")
    assert _subagent_system_for("") == _subagent_system_for("general")
    assert _subagent_system_for(None) == _subagent_system_for("general")


def test_case_insensitive_lookup():
    # Case-insensitive: spawn_agent callers sometimes pass capitalised
    # agent_type from hand-written JSON.
    assert _subagent_system_for("EXPLORE") == _subagent_system_for("explore")
    assert _subagent_system_for("Code") == _subagent_system_for("code")


def test_each_prompt_instructs_report_format():
    # All three prompts must end with a directive to deliver a self-contained
    # final report — that's the contract between the sub-agent and its caller.
    for agent_type, prompt in _SUBAGENT_SYSTEMS.items():
        assert "report" in prompt.lower(), (
            f"{agent_type} prompt is missing the 'report' directive"
        )
        assert "ONLY thing returned" in prompt or "self-contained" in prompt, (
            f"{agent_type} prompt is missing the self-contained-report directive"
        )
