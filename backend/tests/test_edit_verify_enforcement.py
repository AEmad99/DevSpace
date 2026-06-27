"""HARNESS: edit-then-verify enforcement state machine.

Extracted to ``src.agent_harness`` so the policy is unit-testable. The
agent loop wires the hooks (``record_edit`` / ``record_verify`` /
``nudge_round`` / ``block_round`` / ``filter_schemas``) at the right
points; this test pins the transitions so a future refactor can't
collapse the escalation back into a soft-prompt-only behaviour.
"""
from src.agent_harness import (
    EDIT_TOOLS,
    VERIFY_TOOLS,
    EditVerifyHarness,
    MAX_VERIFY_NUDGES,
    block_message,
    nudge_message,
)


def _edit_fn(name="edit_file"):
    return {"function": {"name": name}}


def _verify_fn(name="run_tests"):
    return {"function": {"name": name}}


def test_initially_no_nudge_no_block():
    h = EditVerifyHarness()
    assert h.edits_since_verify == 0
    assert h.verify_nudge_count == 0
    assert h.verify_block_active is False
    assert h.nudge_round() is False
    assert h.block_round() is False


def test_single_edit_triggers_one_nudge():
    h = EditVerifyHarness()
    h.record_edit()
    assert h.edits_since_verify == 1
    assert h.nudge_round() is True
    # Already nudged; second call this round returns False
    assert h.nudge_round() is False


def test_nudge_message_present():
    msg = nudge_message()
    assert "run_tests" in msg
    assert "lint" in msg or "format" in msg


def test_repeated_edits_escalate_to_block():
    h = EditVerifyHarness()
    # Round 1: edit fires the nudge.
    h.record_edit()
    assert h.nudge_round() is True
    # The model ignores it; round 2 starts with edits_since_verify still 1
    # (no verify ran). The loop's per-round check fires the block.
    assert h.block_round() is True
    assert h.verify_block_active is True


def test_block_message_present():
    msg = block_message()
    assert "edit_file" in msg
    assert "write_file" in msg
    assert "run_tests" in msg


def test_block_round_idempotent_within_round():
    h = EditVerifyHarness()
    h.record_edit()
    h.nudge_round()  # first time
    assert h.block_round() is True
    # Second call in the same round must NOT re-arm (the block is one-shot).
    assert h.block_round() is False


def test_filter_schemas_drops_edit_tools_when_blocked():
    h = EditVerifyHarness()
    schemas = [_edit_fn("edit_file"), _edit_fn("write_file"),
               _verify_fn("run_tests"), _verify_fn("lint")]
    assert len(h.filter_schemas(schemas)) == 4  # no block yet
    h.record_edit()
    h.nudge_round()
    h.block_round()
    filt = h.filter_schemas(schemas)
    names = {(s.get("function") or {}).get("name") for s in filt}
    assert "edit_file" not in names
    assert "write_file" not in names
    assert "run_tests" in names
    assert "lint" in names


def test_verify_resets_block_and_counter():
    h = EditVerifyHarness()
    h.record_edit()
    h.record_edit()
    h.record_edit()
    h.nudge_round()
    h.block_round()
    assert h.verify_block_active is True
    # A verify call clears everything.
    h.record_verify()
    assert h.edits_since_verify == 0
    assert h.verify_nudge_count == 0
    assert h.verify_block_active is False
    # Schemas reappear.
    schemas = [_edit_fn("edit_file"), _verify_fn("run_tests")]
    assert len(h.filter_schemas(schemas)) == 2


def test_verify_tools_constant_includes_run_tests_lint_format():
    assert "run_tests" in VERIFY_TOOLS
    assert "lint" in VERIFY_TOOLS
    assert "format" in VERIFY_TOOLS


def test_edit_tools_constant_includes_only_edit_and_write():
    assert EDIT_TOOLS == frozenset({"edit_file", "write_file"})


def test_max_verify_nudges_is_one():
    # The escalation policy: ONE soft nudge, then the block. Increasing
    # this would re-introduce the "nudge forever without verifying" bug.
    assert MAX_VERIFY_NUDGES == 1


def test_filter_schemas_passthrough_when_no_block():
    h = EditVerifyHarness()
    schemas = [_edit_fn("edit_file"), _verify_fn("run_tests")]
    # No edits, no block -> all schemas pass through unchanged.
    out = h.filter_schemas(schemas)
    assert out == schemas


def test_filter_schemas_tolerates_non_dict_schemas():
    h = EditVerifyHarness()
    h.record_edit()
    h.nudge_round()
    h.block_round()
    # Mixed input: dict, plain string, dict missing 'function' key.
    schemas = [_edit_fn(), "not a dict", {"name": "write_file"}, _verify_fn()]
    filt = h.filter_schemas(schemas)
    names = {(s.get("function") or {}).get("name") if isinstance(s, dict) else None
             for s in filt}
    assert "edit_file" not in names
    assert "write_file" not in names  # dropped via top-level "name" key
    assert "run_tests" in names
