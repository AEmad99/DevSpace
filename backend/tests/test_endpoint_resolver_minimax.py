"""Regression tests for MiniMax URL building in endpoint_resolver.

Before this fix, the generic OpenAI-compat fallthrough in ``build_chat_url`` /
``build_models_url`` (and the bearer-only header logic in ``build_headers``)
broke MiniMax endpoints in three ways:

1. A user who configured the bare origin ``https://api.minimax.io`` got
   ``https://api.minimax.io/chat/completions`` — MiniMax only exposes
   ``/v1/chat/completions`` on that surface and 404s without the ``/v1``
   segment. The chat symptom was a generic
   "api.minimax.io returned 404 — check the base URL and model name."
2. A user who configured ``https://api.minimax.io/anthropic`` got
   ``https://api.minimax.io/anthropic/chat/completions`` — the Anthropic-compat
   surface only exposes ``/anthropic/v1/messages``, so the chat call
   similarly 404'd and the prompt-cache markers we inject never reached the
   wire. Without the cache, every MiniMax turn re-billed the full system+tools
   prefix on M2.x (explicit-cache pricing) and was LRU-evictable on M3
   (passive cache).
3. The Anthropic-compat surface requires ``x-api-key`` + ``anthropic-version``
   auth headers (not just ``Authorization: Bearer``); sending bearer alone
   silently worked on the chat path but 401'd on the models probe and
   occasionally on the chat too.

These tests pin the resolver output so future refactors can't silently
regress MiniMax back into the OpenAI-generic path.
"""
import pytest

from src.endpoint_resolver import (
    build_chat_url,
    build_headers,
    build_models_url,
    normalize_base,
)


class TestBuildChatUrlMiniMax:
    @pytest.mark.parametrize("base,expected", [
        # OpenAI-compat surface — must land on /v1/chat/completions.
        ("https://api.minimax.io",
         "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/",
         "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/v1",
         "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/v1/chat/completions",
         "https://api.minimax.io/v1/chat/completions"),
        # Anthropic-compat surface — must land on /anthropic/v1/messages,
        # NOT /anthropic/chat/completions.
        ("https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.io/anthropic/v1",
         "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.io/anthropic/v1/messages",
         "https://api.minimax.io/anthropic/v1/messages"),
    ])
    def test_chat_url_lands_on_real_endpoint(self, base, expected):
        assert build_chat_url(base) == expected


class TestBuildModelsUrlMiniMax:
    @pytest.mark.parametrize("base,expected", [
        # OpenAI-compat — /v1/models.
        ("https://api.minimax.io",
         "https://api.minimax.io/v1/models"),
        ("https://api.minimax.io/v1",
         "https://api.minimax.io/v1/models"),
        ("https://api.minimax.io/v1/chat/completions",
         "https://api.minimax.io/v1/models"),
        # Anthropic-compat — /anthropic/v1/models (NOT /anthropic/v1/v1/models,
        # which is what the naive _append_endpoint_path('/v1/models') produces).
        ("https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/models"),
        ("https://api.minimax.io/anthropic/v1",
         "https://api.minimax.io/anthropic/v1/models"),
        ("https://api.minimax.io/anthropic/v1/messages",
         "https://api.minimax.io/anthropic/v1/models"),
    ])
    def test_models_url_lands_on_real_endpoint(self, base, expected):
        assert build_models_url(base) == expected


class TestBuildHeadersMiniMax:
    def test_openai_compat_uses_bearer(self):
        # The OpenAI-compat /v1/models listing is bearer-only — x-api-key
        # returns 401. The chat path silently accepts bearer too, so always
        # sending Bearer on this surface is the safe default.
        h = build_headers("sk-test", "https://api.minimax.io")
        assert h.get("Authorization") == "Bearer sk-test"
        assert "x-api-key" not in h

    def test_anthropic_compat_uses_x_api_key(self):
        # The Anthropic-compat /anthropic/v1/messages endpoint expects
        # x-api-key + anthropic-version, mirroring native Anthropic.
        h = build_headers("sk-test", "https://api.minimax.io/anthropic")
        assert h.get("x-api-key") == "sk-test"
        assert h.get("anthropic-version") == "2023-06-01"
        # Bearer is also accepted but redundant; we don't send it.
        assert "Authorization" not in h

    def test_anthropic_compat_with_full_messages_path(self):
        h = build_headers("sk-test", "https://api.minimax.io/anthropic/v1/messages")
        assert h.get("x-api-key") == "sk-test"
        assert h.get("anthropic-version") == "2023-06-01"

    def test_missing_key_returns_only_version_header(self):
        h = build_headers(None, "https://api.minimax.io/anthropic")
        assert h.get("anthropic-version") == "2023-06-01"
        assert "x-api-key" not in h
        assert "Authorization" not in h


class TestNormalizeBaseMiniMax:
    # normalize_base() is the chat-route layer that strips known chat-endpoint
    # suffixes so endpoint lookups still match by base URL. Make sure it
    # still collapses the same suffixes it always has — independent of the
    # new chat/models header logic above.
    @pytest.mark.parametrize("base,expected", [
        ("https://api.minimax.io/v1/chat/completions", "https://api.minimax.io/v1"),
        ("https://api.minimax.io/anthropic/v1/messages", "https://api.minimax.io/anthropic"),
        ("https://api.minimax.io", "https://api.minimax.io"),
    ])
    def test_normalize_preserves_surface_segment(self, base, expected):
        assert normalize_base(base) == expected
