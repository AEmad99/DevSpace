"""
agent_harness.py

The coding-agent harness's edit-then-verify enforcement state, extracted
from ``agent_loop.py`` so the loop body stays focused on streaming + tool
dispatch and the harness policy is unit-testable in isolation.

The policy is:
  - Track consecutive ``edit_file``/``write_file`` calls without an
    intervening ``run_tests``/``lint``/``format`` call.
  - When that count crosses 1, inject a one-time soft nudge into the
    model's context the next time it tries to act.
  - If the model ignores the nudge AND attempts another edit without
    verifying, escalate: hide ``edit_file``/``write_file`` from the active
    tool schemas for one round. The model can only respond by running a
    verify tool or by ending the turn.
  - When any verify tool runs (success or fail), reset the counter and
    clear the block.

A separate ``should_auto_verify`` helper checks the opt-in setting that
queues a synthetic ``run_tests`` call after every edit.
"""
import json
import os
from typing import Any, Callable, Dict, FrozenSet, Optional


EDIT_TOOLS: FrozenSet[str] = frozenset({"edit_file", "write_file"})
VERIFY_TOOLS: FrozenSet[str] = frozenset({"run_tests", "lint", "format"})
MAX_VERIFY_NUDGES = 1  # one soft nudge before escalation


class EditVerifyHarness:
    """Tracks the edit→verify cycle and decides, per round, whether the
    harness should (a) inject a soft nudge, (b) block further edits, or
    (c) stay out of the way.

    The agent loop instantiates one per stream_agent_loop call and calls
    the four hooks (record_edit / record_verify / nudge_round /
    block_round) at the appropriate points. The state is intentionally
    pure (no I/O) so tests can drive it deterministically.
    """

    def __init__(self) -> None:
        self.edits_since_verify = 0
        self.verify_nudge_count = 0
        self.verify_block_active = False

    def record_edit(self) -> None:
        """Call after a successful edit_file/write_file tool call."""
        self.edits_since_verify += 1

    def record_verify(self) -> None:
        """Call after any verify tool call (run_tests / lint / format).
        Resets both the counter and the block flag so the edit tools
        reappear on the next round."""
        self.edits_since_verify = 0
        self.verify_nudge_count = 0
        self.verify_block_active = False

    def nudge_round(self) -> bool:
        """Decide whether to inject a soft nudge this round. Returns True
        iff this round should carry the nudge (and increments the cap)."""
        if (
            self.edits_since_verify >= 1
            and self.verify_nudge_count < MAX_VERIFY_NUDGES
        ):
            self.verify_nudge_count += 1
            return True
        return False

    def block_round(self) -> bool:
        """Decide whether to escalate to the hard block this round.
        Returns True iff the edit tools should be hidden for one round.
        Idempotent within a single round (the loop calls once per round)."""
        if (
            self.edits_since_verify >= 1
            and self.verify_nudge_count >= MAX_VERIFY_NUDGES
            and not self.verify_block_active
        ):
            self.verify_block_active = True
            return True
        return False

    def filter_schemas(self, schemas: list) -> list:
        """When the block is active, drop the edit tools from a schemas
        list (API-model native function-calling shape). Returns the
        (possibly shorter) list. The agent loop calls this on the
        assembled ``all_tool_schemas``. Tolerates non-dict entries
        (the loop's schema assembly can include strings or other shapes
        that the loop's own typecheck would catch later — we just pass
        them through)."""
        if not self.verify_block_active:
            return schemas
        out = []
        for s in schemas:
            if not isinstance(s, dict):
                out.append(s)
                continue
            fname = (s.get("function") or {}).get("name")
            tname = s.get("name")
            if fname in EDIT_TOOLS or tname in EDIT_TOOLS:
                continue
            out.append(s)
        return out


def should_auto_verify(workspace: Optional[str] = None,
                       setting_reader: Optional[Callable[[str, Any], Any]] = None) -> bool:
    """HARNESS auto-verify gate. Returns True iff a synthetic ``run_tests``
    call should be auto-queued after every edit_file/write_file in the
    current turn.

    Resolution order:
      1. ``<workspace>/.devspace/auto-verify.json`` ``{"enabled": <bool>}``
         if present (per-workspace override; wins over the global).
      2. The global ``agent_auto_verify`` setting (default ``False``).

    ``setting_reader`` lets tests inject a stub for ``get_setting`` without
    monkeypatching src.settings at the module level.
    """
    reader = setting_reader
    if reader is None:
        try:
            from src.settings import get_setting as _gs
            reader = _gs
        except Exception:
            return False

    global_on = False
    try:
        global_on = bool(reader("agent_auto_verify", False))
    except Exception:
        global_on = False

    if workspace:
        try:
            with open(os.path.join(workspace, ".devspace", "auto-verify.json"),
                      "r", encoding="utf-8") as _f:
                override = json.load(_f)
            if isinstance(override, dict) and "enabled" in override:
                return bool(override["enabled"])
        except (OSError, ValueError):
            pass
    return bool(global_on)


def nudge_message() -> str:
    """The soft nudge text injected when ``nudge_round()`` returns True."""
    return (
        "You've made a file edit without running the project's tests or "
        "linter yet. For a coding task the edit→test→fix loop is the whole "
        "point — a green test run is what proves the change actually works. "
        "Run `run_tests` (or `lint`/`format`) on the affected files now, then "
        "iterate. If the change is trivially obvious (typo / comment / "
        "single-line) and doesn't warrant a test run, say so in one sentence "
        "and end the turn."
    )


def block_message() -> str:
    """The hard-block text injected when ``block_round()`` returns True."""
    return (
        "The harness is temporarily removing `edit_file` and `write_file` "
        "from your tools this round. You've edited without verifying, and "
        "the previous nudge was ignored. Run `run_tests` (or `lint`/`format`) "
        "on the affected files now; once a verify tool succeeds the edit "
        "tools come back automatically. If the change truly doesn't need a "
        "test run, explain why in one sentence and end the turn — that's "
        "also acceptable."
    )


def verifier_fail_message(issues: list, todos_md: str = "") -> str:
    """The system-prompt message injected after a verifier FAIL. Includes
    the open TODO checklist (if any) so the model has its plan visible
    while fixing the issues. ``todos_md`` is the markdown checklist
    string from ``_todos_md`` in agent_loop; empty string is allowed (the
    tail is omitted in that case)."""
    issues_block = "\n- " + "\n- ".join(issues) if issues else ""
    tail = (
        "\n\nYour current TODO list (use these to focus the fix):\n" + todos_md
        if todos_md and todos_md.strip() else ""
    )
    return (
        "An independent verifier reviewed your work against the "
        "original request and found issues that must be fixed before "
        "this is actually done:" + issues_block +
        "\n\nFix these now using tools, then finish." + tail
    )


def tool_fail_message(tool_name: str, exit_code, todos_md: str = "") -> str:
    """The system-prompt message injected after an edit/verify tool
    failure. Includes the open TODO checklist so the model can recover
    against its own plan. Empty ``todos_md`` skips the checklist tail."""
    tail = (
        "\n\nYour open TODOs below — use them to recover:\n\n" + todos_md.strip()
        if todos_md and todos_md.strip() else ""
    )
    return (
        f"`{tool_name}` failed (exit_code={exit_code}). " + tail.lstrip()
    )
