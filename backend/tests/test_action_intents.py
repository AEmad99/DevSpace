from src.action_intents import classify_tool_intent, message_needs_tools


def test_calendar_entry_request_promotes_to_agent():
    assert message_needs_tools("Can you add an entry to my calendar?")
    intent = classify_tool_intent("Can you add an entry to my calendar?")
    assert intent.needs_tools
    assert intent.category == "calendar"


def test_calendar_imperative_variants_promote_to_agent():
    assert message_needs_tools("add lunch with Sam to my calendar tomorrow at noon")
    assert message_needs_tools("schedule a call with Mina next Friday")
    assert message_needs_tools("put dentist appointment on my calendar")
    assert message_needs_tools("Alright. Recreate that same appointment")
    assert message_needs_tools("Okay delete that doctor appointment from the calendar")
    assert message_needs_tools("have another go at adding a test entry to the calendar")
    assert message_needs_tools(
        "Okay so you should be able to create that calendar event for tomorrow at 1:30 p.m. right for me to go to the hardware store"
    )
    assert message_needs_tools(
        "make it an appointment at 12pm for me to visit the doctor it's tomorrow the 2nd of June 2026"
    )


def test_calendar_read_requests_promote_to_agent():
    assert message_needs_tools("What upcoming events do I have?")
    assert message_needs_tools("Can you show my next appointments?")
    assert message_needs_tools("Do I have upcoming Taekwondo classes this week?")
    assert message_needs_tools("What's on my calendar tomorrow?")
    assert message_needs_tools("When is my next meeting?")


def test_note_todo_and_reminder_actions_promote_to_agent():
    assert message_needs_tools("add milk to my todo list")
    assert message_needs_tools("take a note that the server needs checking")
    assert message_needs_tools("set a reminder to call Pat at 4pm")


def test_email_and_ui_actions_promote_to_agent():
    assert message_needs_tools("reply to that email")
    assert message_needs_tools("mark those emails as read")
    assert message_needs_tools("open my calendar")
    assert message_needs_tools("turn off web search")


def test_research_action_promotes_to_agent():
    assert message_needs_tools("research cost effective local models")
    assert message_needs_tools("can you look into GPU hosting options")


def test_explicit_web_search_promotes_to_agent():
    assert message_needs_tools("use web search and find a recipe for chocolate chip cookies")
    assert message_needs_tools("do a web search for the best chocolate chip cookies")
    assert message_needs_tools("search the web for current RTX 3090 prices")
    assert classify_tool_intent("use web search and find a recipe").category == "web"


def test_explanatory_calendar_questions_stay_plain_chat():
    assert not message_needs_tools("How do I add an entry to my calendar?")
    assert not message_needs_tools("What about the built-in Odysseus calendar, is that linked to email?")
    assert not message_needs_tools("Can you explain how calendar reminders work?")
    intent = classify_tool_intent("How do I add an entry to my calendar?")
    assert not intent.needs_tools
    assert intent.reason == "explanatory feature question"


def test_router_reports_non_calendar_categories():
    assert classify_tool_intent("reply to that email").category == "email"
    assert classify_tool_intent("open my calendar").category == "ui"
    assert classify_tool_intent("research cost effective local models").category == "research"


# ---------------------------------------------------------------------------
# Implicit web-research intent (entity + financial/news metric)
# ---------------------------------------------------------------------------
# The bug report: a chat titled "MiniMax Revenue and Profitability" was
# stuck in chat mode. The model had no tools, fell back to writing
# Anthropic-style JSON, and the turn terminated. The fix auto-escalates
# these "I want current info about X" requests to agent mode so the
# model can actually call web_search.

class TestImplicitWebResearchIntent:
    """Chat-list title shape: `<Entity> <metric>`. Screenshot case."""

    def test_screenshot_chat_title_promotes_to_agent(self):
        assert message_needs_tools("MiniMax Revenue and Profitability")
        assert classify_tool_intent("MiniMax Revenue and Profitability").category == "web"

    def test_short_entity_plus_metric_promotes(self):
        assert message_needs_tools("OpenAI valuation")
        assert message_needs_tools("Anthropic funding")
        assert message_needs_tools("Apple earnings")
        assert message_needs_tools("Tesla stock price")
        assert message_needs_tools("Nvidia market cap")

    def test_single_char_ticker_plus_metric_promotes(self):
        """X (formerly Twitter), G (Google), T (AT&T) — real single-char tickers."""
        assert message_needs_tools("X users 2026")
        assert message_needs_tools("G revenue")

    def test_metric_then_entity_order_promotes(self):
        """'Latest news on MiniMax' — metric comes BEFORE the entity."""
        assert message_needs_tools("Latest news on MiniMax")
        assert message_needs_tools("Recent funding for OpenAI")
        assert message_needs_tools("This week's earnings for Apple")

    def test_time_sensitive_year_promotes(self):
        """Fresh year reference + financial metric."""
        assert message_needs_tools("SpaceX 2026 funding round")

    def test_imperative_lookup_phrases_promote(self):
        """'look up X', 'check on X', 'give me the latest on X'."""
        assert message_needs_tools("look up Tesla stock")
        assert message_needs_tools("check on Nvidia")
        assert message_needs_tools("give me the latest on SpaceX")
        assert message_needs_tools("what's the latest on OpenAI?")
        assert message_needs_tools("status of SpaceX")

    def test_vague_research_phrases_promote_to_research(self):
        """'what's going on with X', 'tell me about X' — research not just lookup."""
        assert message_needs_tools("tell me about Anthropic")
        assert message_needs_tools("what's the deal with X")
        assert message_needs_tools("give me the rundown on X")
        assert classify_tool_intent("tell me about Anthropic").category == "research"


class TestImplicitWebResearchFalsePositives:
    """Common English phrasings that look similar but must NOT promote."""

    def test_explanatory_questions_stay_plain_chat(self):
        assert not message_needs_tools("How does OpenAI work?")
        assert not message_needs_tools("What is revenue recognition?")
        assert not message_needs_tools("How to do my taxes")
        assert not message_needs_tools("Can you explain how calendars work?")
        assert not message_needs_tools("What about the weather?")

    def test_bare_concept_stays_plain_chat(self):
        """'revenue growth', 'revenue recognition' — no entity to research."""
        assert not message_needs_tools("revenue growth")
        assert not message_needs_tools("revenue recognition")
        assert not message_needs_tools("stock price")
        assert not message_needs_tools("market cap")

    def test_lowercase_tell_me_about_stays_plain_chat(self):
        """'tell me about a good restaurant' — no real entity (lowercase noun)."""
        assert not message_needs_tools("tell me about a good restaurant")
        assert not message_needs_tools("what's going on with my schedule")
