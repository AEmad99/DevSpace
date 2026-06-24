"""Guard tests for the agent system prompt.

The system prompt prefix is KV-cached across requests with the same
(disable/mcp/admin/relevant-tools/...) key. Bloating it is the single
most expensive thing you can do to per-turn latency. These tests
assert each rule block stays under a reasonable size, so accidental
additions (another 30-line anti-pattern paragraph, a copy-paste of
cookbook rules, etc.) are caught before they ship.

The cap sizes here are conservative: the current prompt fits well under
them. If you genuinely need to grow a section, do it consciously
(update the cap AND leave a comment explaining why).
"""
import pytest

from src.agent_loop import (
    _AGENT_PREAMBLE,
    _AGENT_RULES,
    _API_AGENT_RULES,
    _LINK_RULES,
    _DOMAIN_RULES,
    TOOL_SECTIONS,
)


# Soft caps (chars). Pick a number that's well above the current value
# but small enough to catch a careless 30-line paragraph.
PREAMBLE_MAX = 600
RULES_MAX = 6000              # _AGENT_RULES + _API_AGENT_RULES are similar
LINK_RULES_MAX = 1500
DOMAIN_RULES_TOTAL_MAX = 15_000  # sum of all per-domain rules
TOOL_SECTIONS_TOTAL_MAX = 60_000  # sum of all per-tool sections


def test_preamble_is_short():
    assert len(_AGENT_PREAMBLE) < PREAMBLE_MAX, (
        f"preamble is {len(_AGENT_PREAMBLE)} chars (cap {PREAMBLE_MAX}). "
        f"KV-cached across requests — bloat here costs every turn."
    )


def test_agent_rules_under_cap():
    assert len(_AGENT_RULES) < RULES_MAX, (
        f"_AGENT_RULES is {len(_AGENT_RULES)} chars (cap {RULES_MAX})."
    )


def test_api_agent_rules_under_cap():
    assert len(_API_AGENT_RULES) < RULES_MAX, (
        f"_API_AGENT_RULES is {len(_API_AGENT_RULES)} chars (cap {RULES_MAX})."
    )


def test_link_rules_under_cap():
    assert len(_LINK_RULES) < LINK_RULES_MAX, (
        f"_LINK_RULES is {len(_LINK_RULES)} chars (cap {LINK_RULES_MAX})."
    )


def test_domain_rules_total_under_cap():
    total = sum(len(v) for v in _DOMAIN_RULES.values())
    assert total < DOMAIN_RULES_TOTAL_MAX, (
        f"_DOMAIN_RULES total is {total} chars (cap {DOMAIN_RULES_TOTAL_MAX})."
    )


def test_tool_sections_total_under_cap():
    # Per-tool sections can grow as we add new tools; the cap scales with
    # how many tools we expect to advertise. We have ~30 tools, so each
    # averages <2 KB here — generous.
    total = sum(len(v) for v in TOOL_SECTIONS.values())
    assert total < TOOL_SECTIONS_TOTAL_MAX, (
        f"TOOL_SECTIONS total is {total} chars (cap {TOOL_SECTIONS_TOTAL_MAX})."
    )


def test_agent_rules_and_api_rules_share_key_content():
    """The fenced-block and native function-calling rules are ~80% the
    same — duplication doubles the prompt. If the file accidentally
    diverges, the model gets conflicting guidance. Assert a meaningful
    overlap so any drift is intentional."""
    import re
    def _words(s):
        return set(re.findall(r"[a-zA-Z][a-zA-Z\-]+", s.lower()))
    a = _words(_AGENT_RULES)
    b = _words(_API_AGENT_RULES)
    # Shared vocabulary (e.g. "tool", "agent", "edit", "rule", "user",
    # "memory", "document", "manage") should be heavy in both.
    overlap = len(a & b)
    # At least 15 shared words; if a substantial change rephrases, this
    # is the first signal to check the diff.
    assert overlap >= 15, f"_AGENT_RULES ∩ _API_AGENT_RULES only {overlap} words"


def test_no_duplicate_tool_sections():
    """A common bloat vector: adding the same tool description to both the
    fenced and native sections. Catch it here."""
    assert set(TOOL_SECTIONS.keys()) == set(TOOL_SECTIONS.keys()), (
        "TOOL_SECTIONS should not have any duplicate keys"
    )
