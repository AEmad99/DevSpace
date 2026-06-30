"""Pin the agent system prompt's action-bias framing.

The bug this guards against: the model self-restricts and refuses to make
file edits because the system prompt over-prescribes the edit->verify
discipline as a mandatory gate. Concretely:

  * `_AGENT_RULES` and `_API_AGENT_RULES` must carry the "BIAS TOWARD
    ACTION on edit requests" and "don't second-guess a successful edit"
    framing — the operative blocks, not dead code.
  * The "files" domain rule must NOT contain the old "The harness will
    block further edits until a verify tool succeeds" wording, which
    previously caused the model to refuse to make consecutive edits on
    projects without a test runner. The new wording makes verify a
    recommendation and explicitly allows edits in one turn.
  * There must be exactly one operative definition of each rule block
    (no silent redefinition that overwrites the action-bias copy).
"""
from src.agent_loop import (
    _AGENT_PREAMBLE,
    _AGENT_RULES,
    _API_AGENT_RULES,
    _DOMAIN_RULES,
)


def test_agent_rules_carries_action_bias_framing():
    text = _AGENT_RULES
    assert "BIAS TOWARD ACTION" in text, (
        "_AGENT_RULES must include the 'BIAS TOWARD ACTION on edit "
        "requests' rule so the model doesn't refuse to edit"
    )
    assert "second-guess" in text, (
        "_AGENT_RULES must tell the model not to second-guess a "
        "successful edit (one-sentence confirmation, not re-verification)"
    )
    assert "MAKE AS MANY EDITS" in text, (
        "_AGENT_RULES must tell the model to make as many edits as the "
        "task needs in one turn (multi-file refactor is one turn, not "
        "five separate ones)"
    )


def test_api_agent_rules_carries_action_bias_framing():
    text = _API_AGENT_RULES
    assert "BIAS TOWARD ACTION" in text, (
        "_API_AGENT_RULES (used for native function-calling models) must "
        "carry the action-bias framing"
    )
    assert "second-guess" in text
    assert "MAKE AS MANY EDITS" in text


def test_files_domain_rule_drops_hard_verify_gate_wording():
    files_rule = _DOMAIN_RULES["files"]
    # The old wording made the model self-enforce a verify-before-next-edit
    # gate. That contradicts the auto-approve-by-default design and was the
    # root cause of the "agent refuses to edit" bug. The replacement wording
    # must NOT contain that sentence.
    assert "block further edits until a verify tool succeeds" not in files_rule, (
        "The 'files' domain rule must not tell the model the harness will "
        "block further edits until a verify tool succeeds. The model reads "
        "this as a hard self-gate and refuses to make consecutive edits on "
        "projects without a test runner."
    )


def test_files_domain_rule_keeps_recommendation_and_action_bias():
    files_rule = _DOMAIN_RULES["files"]
    # The new wording should:
    #  - state the agent is in auto-approve mode by default
    #  - include the "BIAS TOWARD ACTION" framing
    #  - call out that non-code files / projects without a test runner
    #    don't need verification
    assert "auto-approve" in files_rule, (
        "files rule should explicitly state the agent is auto-approve by default"
    )
    assert "BIAS TOWARD ACTION" in files_rule, (
        "files rule should include the BIAS TOWARD ACTION framing"
    )
    assert "no test runner" in files_rule or "non-code" in files_rule, (
        "files rule should explicitly carve out non-code files / projects "
        "without a test runner as not requiring verification"
    )


def test_no_duplicate_rule_definitions():
    """Pin that there's exactly ONE operative _AGENT_RULES / _API_AGENT_RULES.

    A previous iteration defined longer versions and then redefined the
    same module-level names a few lines later — the long copies were
    dead code (Python rebinds the names) and shipped ZERO of the
    action-bias framing the model actually needed. This test fails the
    moment anyone re-introduces that anti-pattern.
    """
    import src.agent_loop as mod
    # Each name should appear exactly once in the module source. Multiple
    # top-level assignments indicate a duplicate that silently overwrites
    # the first copy.
    src = open(mod.__file__, encoding="utf-8").read()
    for name in ("_AGENT_RULES", "_API_AGENT_RULES", "_AGENT_PREAMBLE"):
        # Count top-level assignments only: lines that start with `<name> =`.
        n = sum(1 for line in src.splitlines()
                if line.startswith(f"{name} =") and not line.lstrip().startswith("#"))
        assert n == 1, (
            f"{name} is defined {n} times in agent_loop.py — duplicates "
            f"silently overwrite the first copy and ship dead code."
        )


def test_preamble_is_short_and_singular():
    import src.agent_loop as mod
    assert len(_AGENT_PREAMBLE) < 600
    src = open(mod.__file__, encoding="utf-8").read()
    n = sum(1 for line in src.splitlines()
            if line.startswith("_AGENT_PREAMBLE =") and not line.lstrip().startswith("#"))
    assert n == 1, "_AGENT_PREAMBLE must be defined exactly once"
