"""refresh_session_endpoint_url self-heals stale MiniMax session URLs.

When the endpoint_resolver used to fall through to the generic
OpenAI-compat branch, sessions created against MiniMax endpoints got
broken ``endpoint_url`` values persisted to the sessions table:

    https://api.minimax.io/chat/completions           (404)
    https://api.minimax.io/anthropic/chat/completions  (404)

Even after the resolver fix in endpoint_resolver.py, those sessions
keep using the broken URL verbatim on every chat, surfacing as the
same generic ``api.minimax.io returned 404`` error. refresh_session_endpoint_url
runs before the LLM call and rewrites the session row (URL + headers)
to whatever ``build_chat_url(ep.base_url)`` returns today.
"""

import types

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.chat_helpers as chat_helpers
import src.endpoint_resolver as endpoint_resolver
from core.database import Base, ModelEndpoint, Session as DbSession


def _mem_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(chat_helpers, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def test_refreshes_bare_minimax_openai_compat_url(monkeypatch):
    """A session saved with the bare origin should land on /v1/chat/completions
    after the fix lands."""
    TestSessionLocal = _mem_db(monkeypatch)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="MiniMax", base_url="https://api.minimax.io",
            owner="alice", is_enabled=True, api_key="sk-test",
        ))
        # Simulate a session created BEFORE the fix landed: the resolver
        # used to build https://api.minimax.io/chat/completions here, which
        # is a 404. That stale URL is what's persisted.
        db.add(DbSession(
            id="sess1", name="chat",
            endpoint_url="https://api.minimax.io/chat/completions",
            model="MiniMax-M3", owner="alice",
            headers={"Authorization": "Bearer sk-test"},
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: ("https://api.minimax.io", "sk-test"),
    )

    sess = types.SimpleNamespace(
        id="sess1",
        endpoint_url="https://api.minimax.io/chat/completions",
        model="MiniMax-M3", owner="alice",
        headers={"Authorization": "Bearer sk-test"},
    )
    changed = chat_helpers.refresh_session_endpoint_url(sess, "sess1", owner="alice")
    assert changed is True, "refresh should report the URL was rewritten"
    assert sess.endpoint_url == "https://api.minimax.io/v1/chat/completions"
    assert sess.headers == {"Authorization": "Bearer sk-test"}

    db = TestSessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == "sess1").first()
        assert row.endpoint_url == "https://api.minimax.io/v1/chat/completions"
        assert row.headers == {"Authorization": "Bearer sk-test"}
    finally:
        db.close()


def test_refreshes_anthropic_compat_url_and_swaps_auth_header(monkeypatch):
    """The Anthropic-compat surface rebuilds to /anthropic/v1/messages AND
    the auth pair must change from Bearer to x-api-key."""
    TestSessionLocal = _mem_db(monkeypatch)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="MiniMax (Anthropic-compat)",
            base_url="https://api.minimax.io/anthropic",
            owner="alice", is_enabled=True, api_key="sk-test",
        ))
        # Stale URL from the pre-fix resolver. Note the wrong auth header too
        # — the bug was BOTH the path AND the auth shape.
        db.add(DbSession(
            id="sess1", name="chat",
            endpoint_url="https://api.minimax.io/anthropic/chat/completions",
            model="MiniMax-M2.7", owner="alice",
            headers={"Authorization": "Bearer sk-test"},
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: ("https://api.minimax.io/anthropic", "sk-test"),
    )

    sess = types.SimpleNamespace(
        id="sess1",
        endpoint_url="https://api.minimax.io/anthropic/chat/completions",
        model="MiniMax-M2.7", owner="alice",
        headers={"Authorization": "Bearer sk-test"},
    )
    changed = chat_helpers.refresh_session_endpoint_url(sess, "sess1", owner="alice")
    assert changed is True
    assert sess.endpoint_url == "https://api.minimax.io/anthropic/v1/messages"
    # Headers rebuilt for the Anthropic surface — x-api-key, not Bearer.
    assert sess.headers.get("x-api-key") == "sk-test"
    assert sess.headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in sess.headers

    db = TestSessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == "sess1").first()
        assert row.endpoint_url == "https://api.minimax.io/anthropic/v1/messages"
        assert row.headers.get("x-api-key") == "sk-test"
        assert "Authorization" not in (row.headers or {})
    finally:
        db.close()


def test_noop_when_url_already_matches(monkeypatch):
    """A session that was created AFTER the fix has the right URL — refresh
    must be a no-op so we don't churn the DB row on every chat."""
    TestSessionLocal = _mem_db(monkeypatch)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="MiniMax", base_url="https://api.minimax.io/anthropic",
            owner="alice", is_enabled=True, api_key="sk-test",
        ))
        db.add(DbSession(
            id="sess1", name="chat",
            endpoint_url="https://api.minimax.io/anthropic/v1/messages",
            model="MiniMax-M2.7", owner="alice",
            headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: ("https://api.minimax.io/anthropic", "sk-test"),
    )

    sess = types.SimpleNamespace(
        id="sess1",
        endpoint_url="https://api.minimax.io/anthropic/v1/messages",
        model="MiniMax-M2.7", owner="alice",
        headers={"x-api-key": "sk-test", "anthropic-version": "2023-06-01"},
    )
    changed = chat_helpers.refresh_session_endpoint_url(sess, "sess1", owner="alice")
    assert changed is False
    # Headers untouched on no-op.
    assert sess.headers == {"x-api-key": "sk-test", "anthropic-version": "2023-06-01"}


def test_noop_when_no_matching_endpoint(monkeypatch):
    """A session pointing at an unknown / deleted endpoint must not be
    rewritten to something arbitrary."""
    TestSessionLocal = _mem_db(monkeypatch)
    db = TestSessionLocal()
    try:
        # Different endpoint that won't match the session URL.
        db.add(ModelEndpoint(
            id="ep1", name="OpenAI", base_url="https://api.openai.com/v1",
            owner="alice", is_enabled=True, api_key="sk-openai",
        ))
        db.add(DbSession(
            id="sess1", name="chat",
            endpoint_url="https://api.example.com/v1/chat/completions",
            model="gpt-x", owner="alice",
            headers={"Authorization": "Bearer stale"},
        ))
        db.commit()
    finally:
        db.close()

    sess = types.SimpleNamespace(
        id="sess1",
        endpoint_url="https://api.example.com/v1/chat/completions",
        model="gpt-x", owner="alice",
        headers={"Authorization": "Bearer stale"},
    )
    changed = chat_helpers.refresh_session_endpoint_url(sess, "sess1", owner="alice")
    assert changed is False
    # URL and headers untouched when no endpoint matches.
    assert sess.endpoint_url == "https://api.example.com/v1/chat/completions"
    assert sess.headers == {"Authorization": "Bearer stale"}
