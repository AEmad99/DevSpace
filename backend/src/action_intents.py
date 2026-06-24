"""Lightweight routing hints for chat requests that need tools.

These patterns are intentionally conservative. They only promote plain chat
to agent mode when the user asks the assistant to take an action, not when the
user asks how a feature works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Pattern


@dataclass(frozen=True)
class ToolIntent:
    """A cheap, deterministic chat-to-agent routing decision."""

    needs_tools: bool
    category: str = ""
    reason: str = ""


_ACTION_QUESTION = r"\b(?:can|could|would|will)\s+you\s+"
_ACTION_FOLLOWUP = (
    r"\b(?:you\s+should\s+be\s+able\s+to|"
    r"(?:can|could|would|will|should)\s+you|"
    r"you\s+(?:can|could|would|will|should|need\s+to|have\s+to))\s+"
)
_PLEASE = r"^\s*(?:(?:please|ok(?:ay)?|alright|right|sure|cool|great|thanks)[\s,.!-]+)*"

_CALENDAR_ACTION = (
    r"(?:add|adding|create|creating|recreate|recreating|schedule|scheduling|"
    r"reschedule|rescheduling|book|booking|put|set\s+up|make|making|"
    r"delete|deleting|remove|removing|cancel|cancelling|canceling)"
)
_CALENDAR_THING = r"(?:calendar|calendar\s+(?:entry|item)|event|meeting|appointment|entry|call)"
_CALENDAR_READ_THING = r"(?:calendar|schedule|events?|meetings?|appointments?|classes?)"
_EXPLANATORY_PREFIX = re.compile(
    r"^\s*(?:how\s+(?:do|can)\s+i|can\s+you\s+explain|what\s+about|tell\s+me\s+how|show\s+me\s+how)\b",
    re.I,
)

_PANEL = (
    r"(?:calendar|notes?|inbox|email|mail|documents?|docs|library|gallery|"
    r"settings|cookbook|sessions?|chats?|skills|memories|memory|brain)"
)

_ROUTING_PATTERNS: tuple[tuple[str, str, Pattern[str]], ...] = tuple(
    (category, reason, re.compile(pattern, re.I))
    for category, reason, pattern in (
        # Calendar/event creation. Covers "Can you add an entry to my
        # calendar?", imperatives like "add lunch to my calendar", and
        # follow-ups such as "you should be able to create that event now".
        ("calendar", "assistant calendar action request", rf"{_ACTION_QUESTION}{_CALENDAR_ACTION}\b.{{0,120}}\b{_CALENDAR_THING}\b"),
        ("calendar", "calendar follow-up action request", rf"{_ACTION_FOLLOWUP}{_CALENDAR_ACTION}\b.{{0,120}}\b{_CALENDAR_THING}\b"),
        ("calendar", "calendar imperative action request", rf"{_PLEASE}{_CALENDAR_ACTION}\b.{{0,120}}\b{_CALENDAR_THING}\b"),
        ("calendar", "calendar target action request", rf"{_PLEASE}{_CALENDAR_ACTION}\b.{{0,120}}\b(?:to|on|in|into|for)\s+(?:my\s+|the\s+|this\s+)?calendar\b"),
        ("calendar", "calendar item action request", rf"{_PLEASE}{_CALENDAR_ACTION}\s+(?:it\s+)?(?:a\s+|an\s+)?(?:calendar\s+)?(?:event|meeting|appointment|entry|item|call)\b"),
        ("calendar", "calendar target action request", rf"\b{_CALENDAR_ACTION}\b.{{0,120}}\b(?:to|on|in|into|for)\s+(?:my\s+|the\s+|this\s+)?calendar\b"),
        ("calendar", "put item on calendar request", r"\bput\s+.+\bon\s+(?:my\s+)?calendar\b"),

        # Calendar/event lookup. A question such as "Do I have Taekwondo
        # classes this week?" needs the calendar tool; plain chat cannot know.
        ("calendar", "calendar lookup request", rf"\b(?:list|show|check|find)\b.{{0,120}}\b(?:my\s+|the\s+)?(?:upcoming|next|today'?s?|tomorrow'?s?|this\s+week'?s?)\b.{{0,120}}\b{_CALENDAR_READ_THING}\b"),
        ("calendar", "calendar lookup question", rf"\b(?:what|which)\b.{{0,120}}\b(?:upcoming|next|today'?s?|tomorrow'?s?|this\s+week'?s?)\b.{{0,120}}\b{_CALENDAR_READ_THING}\b"),
        ("calendar", "calendar availability question", rf"\bdo\s+i\s+have\b.{{0,120}}\b(?:upcoming|next|today|tomorrow|this\s+week)\b.{{0,120}}\b{_CALENDAR_READ_THING}\b"),
        ("calendar", "calendar agenda question", r"\bwhat(?:'s| is)\s+on\s+(?:my\s+)?calendar\b"),
        ("calendar", "next calendar item question", r"\bwhen\s+(?:is|are)\s+(?:my\s+)?next\s+(?:event|meeting|appointment|class)\b"),

        # Notes, todos, checklists, and reminders.
        ("notes", "reminder request", r"\bremind\s+me\b"),
        ("notes", "assistant note/todo action request", rf"{_ACTION_QUESTION}(?:add|create|make|take|jot|write\s+down|set)\b.{{0,120}}\b(?:note|todo|task|checklist|reminder)\b"),
        ("notes", "note/todo imperative request", rf"{_PLEASE}(?:add|create|make)\s+(?:a\s+|an\s+)?(?:todo|task|reminder|note|checklist)\b"),
        ("notes", "take note request", rf"{_PLEASE}(?:take|jot|write\s+down)\s+(?:a\s+|an\s+)?note\b"),
        ("notes", "add item to notes/todo request", rf"{_PLEASE}(?:add|jot|write\s+down)\b.{{0,120}}\b(?:to|in|into)\s+(?:my\s+|the\s+)?(?:todo(?:\s+list)?|task\s+list|notes?|checklist)\b"),
        ("notes", "set reminder request", rf"{_PLEASE}set\s+(?:a\s+)?reminder\b"),
        ("notes", "assistant reminder request", rf"{_ACTION_QUESTION}set\s+(?:a\s+)?reminder\b"),

        # Email actions.
        ("email", "assistant email action request", rf"{_ACTION_QUESTION}(?:send|write|reply|email|message|archive|delete|mark)\b.{{0,120}}\b(?:emails?|mail|messages?|inbox|unread|read)\b"),
        ("email", "send/write/reply email request", rf"{_PLEASE}(?:send|write|reply)\b.{{0,120}}\b(?:emails?|mail|messages?)\b"),
        ("email", "archive/delete/mark email request", rf"{_PLEASE}(?:archive|delete|mark)\b.{{0,120}}\b(?:emails?|mail|messages?|inbox)\b"),
        ("email", "email composition request", r"\b(?:send|write|reply)\s+(?:an?\s+)?(?:email|message|mail)\b"),
        ("email", "email contact request", r"\bemail\s+\w+\b"),
        ("email", "check inbox request", r"\bcheck\s+(?:my\s+)?(?:email|inbox|mail)\b"),
        ("email", "unread email request", r"\bunread\s+(?:email|mail)s?\b"),

        # UI/control-plane actions that should open panels or flip toggles.
        ("ui", "open/show panel request", rf"{_PLEASE}(?:open|show|bring\s+up)\s+(?:me\s+)?(?:my\s+|the\s+)?{_PANEL}\b"),
        ("ui", "tool or feature toggle request", r"\b(?:disable|enable|turn\s+(?:on|off))\s+(?:the\s+)?(?:shell|search|web|browser|documents?|memory|skills|images?|calendar|email|mail|research|incognito)\b"),

        # Deep research jobs, not quick conceptual mentions of research.
        ("web", "explicit web search request", rf"{_PLEASE}(?:do|run|use|perform|make)\s+(?:a\s+)?(?:web\s+search|search\s+the\s+web)\b.+"),
        ("web", "web lookup imperative request", rf"{_PLEASE}(?:web\s+search|search\s+the\s+web|search\s+online|look\s+up|google)\b.+"),
        ("web", "assistant web lookup request", rf"{_ACTION_QUESTION}(?:web\s+search|search\s+the\s+web|search\s+online|look\s+up|google)\b.+"),
        # Topic + financial/news metric. The "topic" must be a proper-noun-
        # like entity — NOT a question word, NOT the metric word itself, NOT
        # an English preamble word. Anchored to the START of the message so
        # a mid-sentence phrase like "What is revenue recognition?" (where
        # "is revenue" would otherwise look like topic+metric) doesn't
        # false-positive. Single-char topics are allowed (e.g. "X users
        # 2026" for the X / formerly-Twitter ticker) — the negative
        # lookahead keeps articles ("A revenue ...") from matching. The
        # screenshot's exact chat-list title ("MiniMax Revenue and
        # Profitability") and the more conversational "OpenAI valuation" /
        # "Anthropic funding round" both fall through this gate.
        ("web", "topic plus financial/news metric",
         rf"^\s*(?!(?:What|How|When|Why|Where|Who|Which|"
         rf"Latest|Current|Recent|Today|Now|New|First|Last|"
         rf"Revenue|Profitability|Profit|Loss|Funding|Valuation|Earnings|"
         rf"Stock|Share|Market|Users?|Growth|Launch|Announcement|"
         rf"Pricing|Price|Cost|News|Update|Status|Story|Report|Article)s?\b)"
         rf"(?!(?:A|An|The|This|That|It|Is|Of|On|At|To|By|For|From|In|With)\b)"
         rf"[A-Z][\w&]{{0,30}}"
         rf"(?:\s+(?:[A-Z][\w&]{{0,30}}|\d{{2,4}})){{0,2}}"
         rf"\s+(?:revenue|profitability|profit|loss|funding|valuation|"
         rf"earnings|stock\s+price|share\s+price|market\s+cap|users?|growth|"
         rf"launch|announcement|pricing|price|news|update|status)\b"),
        # "freshness word + metric + preposition + entity" — e.g. "Latest
        # news on MiniMax", "recent funding for OpenAI". Catches the case
        # where the metric precedes the entity (the other patterns assume
        # entity-then-metric order).
        ("web", "freshness then metric then entity",
         rf"^\s*(?:latest|current|recent|new|this\s+week(?:'s)?|2026|2025)\s+"
         rf"(?:news|update|launch|announcement|earnings|funding|valuation|"
         rf"report|growth|pricing|users?)\s+"
         rf"(?:on|about|for|from|re|regarding)\s+[A-Z]\w+"),
        # Time-sensitive fact requests: a freshness word near a financial
        # / market / launch / news word. E.g. "latest OpenAI valuation",
        # "current stock price of X", "X earnings this week". The
        # "current/now" words appear BEFORE the topic, so we don't pin the
        # order — the two just need to be within ~80 chars of each other.
        # The freshness word is also a negative-list match so "Give me
        # the latest on X" doesn't get caught by this rule (it has its own
        # pattern below).
        ("web", "time-sensitive fact request",
         rf"\b(?:this\s+week|this\s+month|2026|2025|2024|right\s+now)\b"
         rf".{{0,80}}\b"
         rf"(?:revenue|funding|valuation|earnings|stock|users?|price|"
         rf"launch|announcement|news|update)\b"),
        # Imperative lookup phrases that don't need the _PLEASE preamble —
        # these are unambiguous ("give me the latest on X", "check on X",
        # "status of X", "what's happening with X"). Strictly imperative
        # so an explanatory "how do I look up users" doesn't get promoted.
        # `\w*` (not `\w+`) and `(?-i:[A-Z])` so a single-character ticker
        # like "X" still matches but lowercase words ("my", "schedule")
        # don't slip through the case-insensitive parent pattern.
        ("web", "imperative lookup phrase",
         rf"(?:look\s+up|find\s+out\s+(?:about|on)|check\s+(?:on|out)|"
         rf"give\s+me\s+(?:the|an)\s+update\s+on|give\s+me\s+the\s+latest\s+on|"
         rf"what(?:'s|\s+is)\s+the\s+latest\s+on|what(?:'s|\s+is)\s+happening\s+with|"
         rf"status\s+of|state\s+of)\s+(?-i:[A-Z])\w*"),
        ("research", "deep research imperative request", rf"{_PLEASE}(?:research|deep\s+dive|look\s+into|investigate)\s+.+"),
        ("research", "assistant deep research request", rf"{_ACTION_QUESTION}(?:research|do\s+research|deep\s+dive|look\s+into|investigate)\s+.+"),
        # Vague research-shaped prompts: "what's the deal with X",
        # "give me the rundown on X", "what's going on with X" — the user
        # wants a researched report, not a single fact. Require a
        # capitalized entity after the phrase so "tell me about a good
        # restaurant" doesn't get promoted (lowercase topic = not a
        # researchable entity, more likely a memory/notes question).
        # `(?-i:[A-Z])` ensures the entity is genuinely capitalized, not
        # a lowercase word sneaking through the case-insensitive parent
        # pattern (e.g. "what is going on with my schedule" must NOT
        # match "my" as an entity).
        ("research", "vague research request",
         rf"(?:what's\s+going\s+on\s+with|what's\s+the\s+deal\s+with|"
         rf"give\s+me\s+(?:a|the)\s+rundown\s+on|give\s+me\s+(?:a|the)\s+lowdown\s+on|"
         rf"summary\s+of|tell\s+me\s+about|what\s+is\s+going\s+on\s+with)\s+"
         rf"(?-i:[A-Z])\w*"),

        # Shell / remote-host intent.
        ("shell", "ssh request", r"\bssh\s+(?:in)?to\b"),
        ("shell", "ssh target request", r"\bssh\s+\w+"),
        ("shell", "remote command request", r"\b(run|execute)\s+.{1,40}\bon\s+\w+"),
        ("shell", "assistant command execution request", r"\b(can|could|please|would)\s+you\s+(run|execute|exec)\b"),
        # Shell verbs only count in imperative position (start of message,
        # optionally after "please") or as a "can you ..." request. A bare
        # word match promoted informational questions ("What does the grep
        # command do?") and incidental uses ("My cat ate my homework").
        ("shell", "imperative shell command request", rf"{_PLEASE}(deploy|build|install|restart|reboot|kill|tail|grep|cat|ls|cd|cp|mv|rm)\b\s+\S+"),
        ("shell", "assistant shell command request", rf"{_ACTION_QUESTION}(deploy|build|install|restart|reboot|kill|tail|grep|cat|ls|cd|cp|mv|rm)\b\s+\S+"),
        ("shell", "system/file check request", r"\b(check|see)\s+(if|whether|what)\s+.{1,40}\b(running|process|service|port|file|exists?)\b"),
    )
)

_TOOL_INTENT_PATTERNS: tuple[Pattern[str], ...] = tuple(
    pattern for _, _, pattern in _ROUTING_PATTERNS
)


def classify_tool_intent(text: str) -> ToolIntent:
    """Classify whether a chat message should be promoted to agent mode."""
    if not text:
        return ToolIntent(False, reason="empty message")
    if _EXPLANATORY_PREFIX.search(text):
        return ToolIntent(False, reason="explanatory feature question")
    for category, reason, pattern in _ROUTING_PATTERNS:
        if pattern.search(text):
            return ToolIntent(True, category=category, reason=reason)
    return ToolIntent(False, reason="no tool-action pattern matched")


def message_needs_tools(text: str, patterns: Iterable[Pattern[str]] = _TOOL_INTENT_PATTERNS) -> bool:
    """Return True when a plain chat message should be promoted to agent mode."""
    if not text:
        return False
    if _EXPLANATORY_PREFIX.search(text):
        return False
    if patterns is _TOOL_INTENT_PATTERNS:
        return classify_tool_intent(text).needs_tools
    return any(pattern.search(text) for pattern in patterns)
