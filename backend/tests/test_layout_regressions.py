"""Regression coverage for the ask-user (multiple-choice question) card.

The question card used to render wider than the assistant's message bubble
above it because `.ask-user-card` had `max-width: 680px` while `.msg-ai`
capped at `max-width: 85%`. On viewports wider than ~800px the card's
right edge sat past the message's right edge and visually "overlapped"
the message text. This test reads style.css directly so it catches a
regression in either of those rules without needing jsdom.

It also asserts the question-text de-duplication branch in chat.js: when
the backend streams the question as a `delta` (the default), the JS
renders only an "Agent asked" label inside the card; when the assistant
narrated nothing about the question, the JS falls back to rendering the
full question text. The renderer can't be exercised in isolation here
without jsdom — covered by inspection of chat.js's renderTodoPanel flow.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CSS = _REPO / "static" / "style.css"
_JS = _REPO / "static" / "js" / "chat.js"


def _rule_block(css_text: str, selector: str) -> str:
    """Return the body of the first CSS rule whose selector matches `selector`.
    Handles the `selector {` opener, nested braces via depth counter, and the
    closing `}`. Returns an empty string if no match is found."""
    pattern = re.compile(rf"(^|\s){re.escape(selector)}\s*\{{", re.MULTILINE)
    m = pattern.search(css_text)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    while i < len(css_text) and depth > 0:
        ch = css_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return css_text[start:i - 1]


def _prop(rule_body: str, prop: str) -> str:
    m = re.search(rf"\b{re.escape(prop)}\s*:\s*([^;]+);", rule_body)
    return m.group(1).strip() if m else ""


def test_ask_user_card_max_width_matches_msg_ai():
    """`.ask-user-card` must cap at the same right edge as `.msg-ai` so the
    card never staggers past the assistant's message bubble above it on
    wider viewports. Both should read `max-width: 85%;`."""
    css = _CSS.read_text(encoding="utf-8")
    ai = _rule_block(css, ".msg-ai")
    card = _rule_block(css, ".ask-user-card")
    assert ai, ".msg-ai rule must exist"
    assert card, ".ask-user-card rule must exist"
    ai_max = _prop(ai, "max-width")
    card_max = _prop(card, "max-width")
    assert ai_max, ".msg-ai must declare max-width"
    assert card_max, ".ask-user-card must declare max-width"
    assert ai_max == card_max, (
        f".ask-user-card max-width ({card_max}) must equal "
        f".msg-ai max-width ({ai_max}) — otherwise the card staggers past "
        "the assistant's message on wider viewports."
    )
    # Sanity check — both should be percentage-based.
    assert "85%" in ai_max


def test_ask_user_card_drops_negative_question_margin():
    """`.ask-user-question` previously had `margin: -2px 0 10px;` which
    pulled the question text up into the close-button header above it.
    The label is now small (uppercase "Agent asked") so the negative
    top margin is no longer needed and the new rule should be `0 0 10px;`."""
    css = _CSS.read_text(encoding="utf-8")
    q = _rule_block(css, ".ask-user-question")
    assert q, ".ask-user-question rule must exist"
    margin = _prop(q, "margin")
    assert margin, ".ask-user-question must declare margin"
    assert "margin-top" not in q, (
        "margin-top should be folded into the margin shorthand"
    )
    # Should NOT start with a negative number — the old `-2px` bug.
    assert not re.match(r"-\d", margin.strip()), (
        f".ask-user-question margin must not start with a negative value "
        f"(was '{margin}'). The old `-2px` top pulled text into the header."
    )


def test_chat_js_question_text_dedup_branch_present():
    """chat.js must skip rendering the question text inside the card when
    the question was already streamed into the assistant's .msg-ai body
    above. The fallback to rendering the full question only fires when
    no prior assistant narration was found — keeps the UI from showing
    the same text twice in a row."""
    src = _JS.read_text(encoding="utf-8")
    assert "_alreadyStreamed" in src, (
        "chat.js must track the _alreadyStreamed flag so it doesn't double-"
        "render the question text inside the .ask-user-question element."
    )
    assert "ask-user-question-text" in src, (
        "chat.js must have a separate .ask-user-question-text element for "
        "the fallback (question wasn't streamed)."
    )


def test_chat_js_renders_into_todo_panel_not_chat_history():
    """The agent's todo_update SSE event used to render an inline .agent-todos
    bubble inside #chat-history. Now it should render into the left-of-chat
    #todo-panel-list so the user has a dedicated checklist panel."""
    src = _JS.read_text(encoding="utf-8")
    # The SSE handler should call renderTodoPanel(...) and NOT touch
    # chat-history for todos.
    assert "renderTodoPanel(_todos, _streamSessionId)" in src, (
        "todo_update SSE handler must call renderTodoPanel(...)"
    )
    # The old "create a .agent-todos bubble in chat-history" code path
    # should be gone from the live SSE branch.
    assert "renderTodoPanel" in src


def test_index_html_has_todo_panel_between_sidebar_and_chat():
    """The todo panel must be a DOM sibling of #sidebar and #chat-container
    so the existing horizontal flex layout in body places it between them.
    No extra wrapper that would interfere with the flex order."""
    from pathlib import Path as P
    html = (P(_REPO) / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="todo-panel"' in html
    # Order check: #sidebar closes before #todo-panel opens, which closes
    # before #chat-container opens.
    sidebar_end = html.find("</nav>")
    panel_start = html.find('id="todo-panel"')
    chat_start = html.find('id="chat-container"')
    assert sidebar_end < panel_start < chat_start, (
        "DOM order must be: #sidebar → #todo-panel → #chat-container"
    )


def test_todo_panel_css_uses_flex_basis_for_fixed_width():
    """The panel uses flex: 0 0 280px so it can't squeeze the chat on narrow
    viewports (overridden by the @media (max-width: 900px) rule that turns
    it into a slide-in drawer)."""
    css = _CSS.read_text(encoding="utf-8")
    panel = _rule_block(css, ".todo-panel")
    assert panel, ".todo-panel rule must exist"
    assert "flex" in panel
    assert "280px" in panel, ".todo-panel should have a fixed flex-basis of 280px"