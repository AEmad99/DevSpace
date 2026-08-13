"""Grok subscription (SuperGrok / X Premium+) OAuth helpers.

Separate from the xAI API-key endpoint. Uses xAI's public Grok CLI OAuth
client over RFC 8628 device authorization, stores refresh tokens server-side,
and resolves a fresh bearer at request time.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

DEFAULT_GROK_SUBSCRIPTION_BASE_URL = (
    os.getenv("GROK_SUBSCRIPTION_BASE_URL", "").strip().rstrip("/")
    or "https://api.x.ai/v1/grok-subscription"
)
GROK_SUBSCRIPTION_PROVIDER = "grok-subscription"
GROK_INFERENCE_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_OAUTH_TOKEN_URL = f"{XAI_OAUTH_ISSUER}/oauth2/token"
GROK_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
GROK_FALLBACK_MODELS = (
    "grok-4.6",
    "grok-4.5",
    "grok-4.3",
    "grok-build-0.1",
)
_NON_CHAT_MODEL = (
    "embedding",
    "tts",
    "whisper",
    "imagine",
    "image",
    "video",
    "moderation",
    "rerank",
)
_AUTH_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_AUTH_REFRESH_LOCKS_GUARD = threading.Lock()


def _database_handles():
    from core.database import ProviderAuthSession, SessionLocal, utcnow_naive
    return ProviderAuthSession, SessionLocal, utcnow_naive


def _refresh_lock_for(auth_id: str) -> threading.Lock:
    with _AUTH_REFRESH_LOCKS_GUARD:
        lock = _AUTH_REFRESH_LOCKS.get(auth_id)
        if lock is None:
            lock = threading.Lock()
            _AUTH_REFRESH_LOCKS[auth_id] = lock
        return lock


class GrokSubscriptionError(RuntimeError):
    """Base error for Grok subscription provider failures."""


class GrokSubscriptionReauthRequired(GrokSubscriptionError):
    """Stored OAuth credentials are invalid or expired beyond refresh."""


class GrokSubscriptionRateLimited(GrokSubscriptionError):
    """Upstream quota/rate limit; reconnecting will not fix it."""


class GrokSubscriptionAuthNotFound(GrokSubscriptionError):
    """No matching owner-scoped auth session exists."""


def is_grok_subscription_base(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").rstrip("/")
    except Exception:
        return False
    if host != "api.x.ai" and host != "x.ai" and not host.endswith(".x.ai"):
        return False
    return path == "/v1/grok-subscription" or path.startswith("/v1/grok-subscription/")


def grok_subscription_request_url(url: str) -> str:
    """Map a stored grok-subscription URL onto the real xAI inference path."""
    if not is_grok_subscription_base(url):
        return url
    try:
        path = (urlparse(url or "").path or "").rstrip("/")
    except Exception:
        path = ""
    if path.endswith("/models"):
        return f"{GROK_INFERENCE_BASE_URL}/models"
    return f"{GROK_INFERENCE_BASE_URL}/chat/completions"


def grok_headers(access_token: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def fetch_available_models(access_token: str, timeout: float = 10.0) -> list[str]:
    if not access_token:
        return list(GROK_FALLBACK_MODELS)
    try:
        response = httpx.get(
            f"{GROK_INFERENCE_BASE_URL}/models",
            headers=grok_headers(access_token),
            timeout=timeout,
        )
        if response.status_code != 200:
            return list(GROK_FALLBACK_MODELS)
        data = response.json()
    except Exception:
        return list(GROK_FALLBACK_MODELS)
    entries = data.get("data", []) if isinstance(data, dict) else []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        slug = mid.strip()
        lower = slug.lower()
        if any(part in lower for part in _NON_CHAT_MODEL):
            continue
        if slug not in seen:
            ordered.append(slug)
            seen.add(slug)
    return ordered or list(GROK_FALLBACK_MODELS)


def _raise_for_oauth_response(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    code = ""
    message = f"Grok Subscription {action} failed with HTTP {response.status_code}."
    try:
        payload = response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "").strip()
            msg = err.get("message")
            if msg:
                message = f"Grok Subscription {action} failed: {msg}"
        elif isinstance(err, str):
            code = err.strip()
            desc = payload.get("error_description") or payload.get("message")
            if desc:
                message = f"Grok Subscription {action} failed: {desc}"
    except Exception:
        pass
    if response.status_code == 429:
        raise GrokSubscriptionRateLimited(
            "Grok Subscription quota or rate limit was reached. Credentials are still valid."
        )
    if response.status_code in (401, 403) or code in {
        "invalid_grant",
        "invalid_token",
        "invalid_request",
        "refresh_token_reused",
        "access_denied",
        "expired_token",
    }:
        raise GrokSubscriptionReauthRequired(message)
    raise GrokSubscriptionError(message)


def _json_or_error(response: httpx.Response, action: str) -> Dict[str, Any]:
    _raise_for_oauth_response(response, action)
    try:
        data = response.json()
    except Exception as exc:
        raise GrokSubscriptionError(f"Grok Subscription {action} returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise GrokSubscriptionError(f"Grok Subscription {action} returned an unexpected response.")
    return data


def request_device_code(timeout: float = 15.0) -> Dict[str, Any]:
    response = httpx.post(
        XAI_OAUTH_DEVICE_CODE_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "client_id": XAI_OAUTH_CLIENT_ID,
            "scope": XAI_OAUTH_SCOPE,
        },
        timeout=timeout,
    )
    data = _json_or_error(response, "device-code request")
    if not data.get("device_code") or not data.get("user_code"):
        raise GrokSubscriptionError("xAI device-code response was missing required fields.")
    data.setdefault("verification_uri", f"{XAI_OAUTH_ISSUER}/oauth2/device")
    data.setdefault("interval", 5)
    data.setdefault("expires_in", 900)
    return data


def poll_device_auth(device_code: str, timeout: float = 15.0) -> Dict[str, Any]:
    response = httpx.post(
        XAI_OAUTH_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "device_code": device_code,
        },
        timeout=timeout,
    )
    if response.status_code == 400:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        err = str((payload or {}).get("error") or "")
        if err in {"authorization_pending", "slow_down"}:
            return payload if isinstance(payload, dict) else {"error": err}
    if response.status_code in (403, 404):
        return {"status": "pending", "error": "authorization_pending"}
    return _json_or_error(response, "device-code poll")


def refresh_oauth_tokens(access_token: str, refresh_token: str, timeout: float = 20.0) -> Dict[str, Any]:
    del access_token
    if not refresh_token:
        raise GrokSubscriptionReauthRequired(
            "Grok Subscription is missing a refresh token. Reconnect the provider."
        )
    response = httpx.post(
        XAI_OAUTH_TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        timeout=timeout,
    )
    data = _json_or_error(response, "token refresh")
    if not data.get("access_token"):
        raise GrokSubscriptionReauthRequired("Grok token refresh did not return an access token.")
    return data


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def access_token_is_expiring(access_token: str, skew_seconds: int = GROK_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    try:
        exp = int(_decode_jwt_payload(access_token).get("exp") or 0)
    except Exception:
        return True
    return exp <= int(time.time()) + int(skew_seconds)


def resolve_runtime_credentials(auth_id: str, owner: Optional[str] = None, *, force_refresh: bool = False) -> Dict[str, Any]:
    ProviderAuthSession, SessionLocal, utcnow_naive = _database_handles()
    db = SessionLocal()
    try:
        q = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.id == auth_id,
            ProviderAuthSession.provider == GROK_SUBSCRIPTION_PROVIDER,
        )
        if owner:
            q = q.filter(ProviderAuthSession.owner == owner)
        row = q.first()
        if row is None:
            raise GrokSubscriptionAuthNotFound("Grok Subscription credentials were not found for this user.")

        access_token = row.access_token or ""
        if force_refresh or access_token_is_expiring(access_token):
            with _refresh_lock_for(auth_id):
                db.refresh(row)
                access_token = row.access_token or ""
                refresh_token = row.refresh_token or ""
                if force_refresh or access_token_is_expiring(access_token):
                    refreshed = refresh_oauth_tokens(access_token, refresh_token)
                    row.access_token = refreshed["access_token"]
                    if refreshed.get("refresh_token"):
                        row.refresh_token = refreshed["refresh_token"]
                    row.last_refresh = utcnow_naive()
                    db.commit()
                    db.refresh(row)
            access_token = row.access_token or ""

        return {
            "provider": GROK_SUBSCRIPTION_PROVIDER,
            "base_url": (row.base_url or DEFAULT_GROK_SUBSCRIPTION_BASE_URL).rstrip("/"),
            "api_key": access_token,
            "auth_mode": row.auth_mode or "grok",
        }
    finally:
        db.close()


def to_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, GrokSubscriptionRateLimited):
        return HTTPException(429, str(exc))
    if isinstance(exc, (GrokSubscriptionReauthRequired, GrokSubscriptionAuthNotFound)):
        return HTTPException(401, f"{exc} Reconnect the provider.")
    return HTTPException(502, str(exc))
