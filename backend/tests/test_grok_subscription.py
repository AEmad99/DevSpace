"""Unit tests for Grok Subscription OAuth helpers."""

import time

import pytest

from src import grok_subscription as gs


def test_is_grok_subscription_base_matches_virtual_path():
    assert gs.is_grok_subscription_base("https://api.x.ai/v1/grok-subscription")
    assert gs.is_grok_subscription_base("https://api.x.ai/v1/grok-subscription/chat/completions")
    assert gs.is_grok_subscription_base("https://api.x.ai/v1/grok-subscription/models")


def test_is_grok_subscription_base_ignores_api_key_xai():
    assert not gs.is_grok_subscription_base("https://api.x.ai/v1")
    assert not gs.is_grok_subscription_base("https://api.x.ai/v1/chat/completions")
    assert not gs.is_grok_subscription_base("https://api.openai.com/v1/grok-subscription")


def test_request_url_rewrites_virtual_paths():
    assert gs.grok_subscription_request_url(
        "https://api.x.ai/v1/grok-subscription/chat/completions"
    ) == "https://api.x.ai/v1/chat/completions"
    assert gs.grok_subscription_request_url(
        "https://api.x.ai/v1/grok-subscription/models"
    ) == "https://api.x.ai/v1/models"
    assert gs.grok_subscription_request_url("https://api.x.ai/v1") == "https://api.x.ai/v1"


def test_fetch_available_models_filters_non_chat(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [
                {"id": "grok-4.6"},
                {"id": "grok-imagine-image"},
                {"id": "text-embedding-3"},
                {"id": "grok-4.5"},
            ]}

    monkeypatch.setattr(gs.httpx, "get", lambda *a, **k: _Resp())
    assert gs.fetch_available_models("token") == ["grok-4.6", "grok-4.5"]


def test_fetch_available_models_falls_back_on_error(monkeypatch):
    monkeypatch.setattr(gs.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert gs.fetch_available_models("token") == list(gs.GROK_FALLBACK_MODELS)


def test_access_token_is_expiring_uses_jwt_exp():
    import base64
    import json

    def _jwt(exp):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"aaa.{payload}.sig"

    assert gs.access_token_is_expiring(_jwt(int(time.time()) + 30))
    assert not gs.access_token_is_expiring(_jwt(int(time.time()) + 3600))


def test_poll_device_auth_treats_pending_as_soft(monkeypatch):
    class _Resp:
        status_code = 400

        def json(self):
            return {"error": "authorization_pending"}

    monkeypatch.setattr(gs.httpx, "post", lambda *a, **k: _Resp())
    assert gs.poll_device_auth("dc")["error"] == "authorization_pending"


def test_resolve_endpoint_runtime_dispatches_to_grok(monkeypatch):
    from src import endpoint_resolver as er

    monkeypatch.setattr(er, "_provider_auth_row_provider", lambda auth_id, owner=None: "grok-subscription")
    monkeypatch.setattr(
        gs,
        "resolve_runtime_credentials",
        lambda auth_id, owner=None, force_refresh=False: {
            "base_url": gs.DEFAULT_GROK_SUBSCRIPTION_BASE_URL,
            "api_key": "oauth-token",
        },
    )
    ep = type("EP", (), {
        "base_url": gs.DEFAULT_GROK_SUBSCRIPTION_BASE_URL,
        "api_key": None,
        "provider_auth_id": "auth1",
    })()
    base, key = er.resolve_endpoint_runtime(ep, owner="alice")
    assert base == gs.DEFAULT_GROK_SUBSCRIPTION_BASE_URL
    assert key == "oauth-token"


def test_poll_device_auth_raises_on_hard_failure(monkeypatch):
    class _Resp:
        status_code = 401

        def json(self):
            return {"error": "invalid_grant", "error_description": "expired"}

    monkeypatch.setattr(gs.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(gs.GrokSubscriptionReauthRequired):
        gs.poll_device_auth("dc")
