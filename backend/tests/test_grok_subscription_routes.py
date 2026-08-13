"""DB-backed Grok Subscription endpoint provisioning tests."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ModelEndpoint, ProviderAuthSession
import routes.grok_subscription_routes as gsr


def _mem_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(gsr, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def test_provision_creates_owner_scoped_auth_session_and_endpoint(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    monkeypatch.setattr(gsr.grok_subscription, "fetch_available_models", lambda token: ["grok-4.6", "grok-4.5"])

    res = gsr._provision_endpoint({"access_token": "AT", "refresh_token": "RT"}, "alice")

    assert res["name"] == "Grok Subscription"
    assert res["base_url"] == gsr.grok_subscription.DEFAULT_GROK_SUBSCRIPTION_BASE_URL
    assert res["models"] == ["grok-4.6", "grok-4.5"]

    db = TestSessionLocal()
    try:
        auth = db.query(ProviderAuthSession).first()
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == res["id"]).first()
        assert auth is not None
        assert auth.owner == "alice"
        assert auth.provider == gsr.grok_subscription.GROK_SUBSCRIPTION_PROVIDER
        assert auth.access_token == "AT"
        assert auth.refresh_token == "RT"
        assert auth.auth_mode == "grok"
        assert ep is not None
        assert ep.owner == "alice"
        assert ep.api_key is None
        assert ep.provider_auth_id == auth.id
        assert ep.endpoint_kind == "api"
        assert ep.model_refresh_mode == "manual"
        assert ep.supports_tools is True
        assert json.loads(ep.cached_models) == ["grok-4.6", "grok-4.5"]
    finally:
        db.close()


def test_provision_refreshes_existing_auth_session_and_endpoint(monkeypatch):
    TestSessionLocal = _mem_db(monkeypatch)
    monkeypatch.setattr(gsr.grok_subscription, "fetch_available_models", lambda token: ["grok-4.6"])

    first = gsr._provision_endpoint({"access_token": "OLD", "refresh_token": "OLD-RT"}, "bob")
    second = gsr._provision_endpoint({"access_token": "NEW", "refresh_token": "NEW-RT"}, "bob")

    assert first["id"] == second["id"]
    db = TestSessionLocal()
    try:
        auth_rows = db.query(ProviderAuthSession).filter(ProviderAuthSession.owner == "bob").all()
        ep_rows = db.query(ModelEndpoint).filter(ModelEndpoint.owner == "bob").all()
        assert len(auth_rows) == 1
        assert len(ep_rows) == 1
        assert auth_rows[0].access_token == "NEW"
        assert auth_rows[0].refresh_token == "NEW-RT"
        assert ep_rows[0].provider_auth_id == auth_rows[0].id
    finally:
        db.close()


def test_provision_rejects_missing_tokens(monkeypatch):
    _mem_db(monkeypatch)
    with pytest.raises(ValueError, match="missing access_token or refresh_token"):
        gsr._provision_endpoint({"access_token": "AT"}, "alice")


def test_provision_rejects_accounts_without_usable_models(monkeypatch):
    _mem_db(monkeypatch)
    monkeypatch.setattr(gsr.grok_subscription, "fetch_available_models", lambda token: [])

    with pytest.raises(ValueError, match="no usable Grok models"):
        gsr._provision_endpoint({"access_token": "AT", "refresh_token": "RT"}, "alice")
