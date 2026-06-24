"""Regression tests for MiniMax prompt-cache wiring.

Covers the two MiniMax API surfaces hosted at ``api.minimax.io``:

* ``/anthropic/v1/messages`` — Anthropic-compatible. Caching uses the same
  explicit ``cache_control: {type: ephemeral}`` markers on the system + tools
  blocks that we already attach for native Anthropic (see
  ``test_llm_core_anthropic_cache.py``). The Anthropic streaming SSE parser
  in ``stream_llm`` is reused to surface ``cache_read_input_tokens`` /
  ``cache_creation_input_tokens`` in usage events.
* ``/v1/chat/completions`` — OpenAI-compatible. Caching is passive (prefix
  matching) but MiniMax honours the OpenAI-standard ``prompt_cache_key``
  field to route the same conversation to the same cache bucket. We attach a
  ``prompt_cache_key`` derived from the Odysseus session id when one is
  available.

Prior to this wiring every chat turn re-billed the full system+tools prefix
(roughly 10–30 KB) on MiniMax M2.x (which has explicit-cache pricing on its
Anthropic surface) and was vulnerable to LRU eviction on MiniMax M3 (which
caches passively on its OpenAI surface). These tests pin the payload-level
behaviour so a future refactor doesn't silently regress either surface.
"""
import httpx
import pytest

from src import llm_core


# ---------------------------------------------------------------------------
# Provider / URL plumbing
# ---------------------------------------------------------------------------

class TestDetectProvider:
    @pytest.mark.parametrize("url,expected", [
        # OpenAI-compat surface
        ("https://api.minimax.io", "minimax"),
        ("https://api.minimax.io/v1", "minimax"),
        ("https://api.minimax.io/v1/chat/completions", "minimax"),
        # Anthropic-compat surface (same host, different path)
        ("https://api.minimax.io/anthropic", "minimax"),
        ("https://api.minimax.io/anthropic/v1", "minimax"),
        ("https://api.minimax.io/anthropic/v1/messages", "minimax"),
    ])
    def test_minimax_host_is_recognized(self, url, expected):
        assert llm_core._detect_provider(url) == expected

    def test_lookalike_host_is_not_minimax(self):
        # Path- or label-only matches must not be promoted to a provider
        # classification. Otherwise a custom domain that happens to contain
        # "minimax" in its label would get the cache_control builder attached
        # to the wrong request shape.
        assert llm_core._detect_provider("https://minimax.io.evil.example/v1") == "openai"


class TestIsMiniMaxAnthropicCompat:
    @pytest.mark.parametrize("url,expected", [
        ("https://api.minimax.io", False),
        ("https://api.minimax.io/v1", False),
        ("https://api.minimax.io/v1/chat/completions", False),
        ("https://api.minimax.io/anthropic", True),
        ("https://api.minimax.io/anthropic/v1", True),
        ("https://api.minimax.io/anthropic/v1/messages", True),
    ])
    def test_path_based(self, url, expected):
        assert llm_core._is_minimax_anthropic_compat_url(url) is expected

    def test_non_minimax_host_is_never_anthropic_compat(self):
        # Even if the path happens to contain /anthropic, a non-MiniMax host
        # is not the MiniMax Anthropic-compat surface.
        assert llm_core._is_minimax_anthropic_compat_url("https://example.com/anthropic/v1") is False


class TestNormalizeMiniMaxUrls:
    @pytest.mark.parametrize("base,expected", [
        # OpenAI-compat
        ("https://api.minimax.io", "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/", "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/v1", "https://api.minimax.io/v1/chat/completions"),
        ("https://api.minimax.io/v1/chat/completions",
         "https://api.minimax.io/v1/chat/completions"),
        # Anthropic-compat
        ("https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.io/anthropic/v1",
         "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.io/anthropic/v1/messages",
         "https://api.minimax.io/anthropic/v1/messages"),
    ])
    def test_url_collapse(self, base, expected):
        # /anthropic/* goes to the Anthropic builder; everything else to OpenAI.
        if "/anthropic" in base:
            assert llm_core._normalize_minimax_anthropic_url(base) == expected
        else:
            assert llm_core._normalize_minimax_openai_url(base) == expected


class TestProviderLabel:
    @pytest.mark.parametrize("url,expected", [
        ("https://api.minimax.io/v1", "MiniMax"),
        ("https://api.minimax.io/v1/chat/completions", "MiniMax"),
        ("https://api.minimax.io/anthropic/v1/messages", "MiniMax (Anthropic-compat)"),
    ])
    def test_label_mentions_minimax(self, url, expected):
        assert llm_core._provider_label(url) == expected


# ---------------------------------------------------------------------------
# Cache-directive injection (OpenAI-compat)
# ---------------------------------------------------------------------------

class TestApplyMiniMaxOpenAICache:
    def test_attaches_prompt_cache_key_with_session_id(self):
        payload = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
        llm_core._apply_minimax_openai_cache(
            payload, "https://api.minimax.io/v1/chat/completions", "session-abc",
        )
        assert payload["prompt_cache_key"] == "session-abc"

    def test_noop_without_session_id(self):
        # A random per-prompt key would defeat the purpose (every turn lands
        # in a fresh cache bucket). Silently skip rather than emit a junk key.
        payload = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
        llm_core._apply_minimax_openai_cache(
            payload, "https://api.minimax.io/v1/chat/completions", None,
        )
        assert "prompt_cache_key" not in payload

    def test_noop_for_anthropic_compat_url(self):
        # /anthropic/v1/messages is handled by the Anthropic builder, which
        # already attaches cache_control. A second marker layer here would
        # be ignored at best, JSON-validation-rejected at worst.
        payload = {"model": "MiniMax-M2.7", "messages": [{"role": "user", "content": "hi"}]}
        llm_core._apply_minimax_openai_cache(
            payload, "https://api.minimax.io/anthropic/v1/messages", "session-abc",
        )
        assert "prompt_cache_key" not in payload

    def test_noop_for_non_minimax_host(self):
        payload = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        llm_core._apply_minimax_openai_cache(
            payload, "https://api.openai.com/v1/chat/completions", "session-abc",
        )
        # OpenAI ignores the field anyway, but we shouldn't claim ownership
        # of an upstream-specific extension on a different provider.
        assert "prompt_cache_key" not in payload

    def test_disabled_via_env_var(self, monkeypatch):
        # Single escape hatch if an upstream ever rejects the field.
        monkeypatch.setenv("LLM_DISABLE_PROMPT_CACHE", "1")
        payload = {"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hi"}]}
        llm_core._apply_minimax_openai_cache(
            payload, "https://api.minimax.io/v1/chat/completions", "session-abc",
        )
        assert "prompt_cache_key" not in payload

    def test_disabled_env_var_accepts_truthy_values(self, monkeypatch):
        for truthy in ("1", "true", "yes", "on", "TRUE", " yes "):
            monkeypatch.setenv("LLM_DISABLE_PROMPT_CACHE", truthy)
            assert llm_core._prompt_cache_enabled() is False

    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("LLM_DISABLE_PROMPT_CACHE", raising=False)
        assert llm_core._prompt_cache_enabled() is True


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------

class TestLLMCallMiniMaxOpenAI:
    def test_openai_compat_url_uses_prompt_cache_key(self, monkeypatch):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "OK"}}]},
            )

        monkeypatch.setattr(llm_core.httpx, "post", fake_post)

        result = llm_core.llm_call(
            "https://api.minimax.io/v1",
            "MiniMax-M3",
            [{"role": "user", "content": "Say OK"}],
            temperature=0.5,
            max_tokens=10,
            headers={"Authorization": "Bearer sk-test"},
            timeout=11,
            # llm_call is sync-only; session_id is not a parameter, but the
            # directive should still NOT be injected (no session context
            # means no stable bucket — the helper short-circuits).
        )

        assert result == "OK"
        assert seen["url"] == "https://api.minimax.io/v1/chat/completions"
        # No session_id available → no prompt_cache_key leaked.
        assert "prompt_cache_key" not in seen["json"]
        # Plain OpenAI-compat shape, not Anthropic.
        assert "messages" in seen["json"]
        assert "system" not in seen["json"]


class TestLLMCallAsyncMiniMaxAnthropic:
    @pytest.mark.asyncio
    async def test_anthropic_compat_url_uses_cache_control(self, monkeypatch):
        seen = {}

        # Patch the async POST shim so we don't need a live HTTP client.
        # The shim is awaited inside llm_call_async, so the replacement must
        # itself be a coroutine function — returning a plain Response from a
        # sync function makes the caller raise "object Response can't be used
        # in 'await' expression".
        async def fake_async_post(client, target_url, headers, json, timeout):
            seen["url"] = target_url
            seen["headers"] = headers
            seen["json"] = json
            request = httpx.Request("POST", target_url)
            return httpx.Response(
                200,
                request=request,
                json={
                    "content": [
                        {"type": "text", "text": "OK"},
                    ],
                },
            )

        monkeypatch.setattr(
            llm_core, "httpx_post_kimi_aware_async", fake_async_post,
        )

        result = await llm_core.llm_call_async(
            "https://api.minimax.io/anthropic/v1",
            "MiniMax-M2.7",
            [
                # > 4000 chars so _build_anthropic_payload attaches the
                # explicit cache_control breakpoint on the system block
                # (the threshold lives in llm_core: `tools or
                # len(system_text) > 4000`).
                {"role": "system", "content": "SYS " * 1500},
                {"role": "user", "content": "hi"},
            ],
            temperature=0.0,
            max_tokens=64,
            headers={"Authorization": "Bearer sk-test"},
            session_id="session-abc",
        )

        assert result == "OK"
        # /anthropic/v1/messages path; Bearer header rewritten to x-api-key.
        assert seen["url"] == "https://api.minimax.io/anthropic/v1/messages"
        assert "x-api-key" in seen["headers"]
        assert seen["headers"]["x-api-key"] == "sk-test"
        assert "Authorization" not in seen["headers"]
        # Anthropic-shape payload: system as a list with cache_control,
        # messages without a system entry, no temperature (out of [0,1]).
        assert isinstance(seen["json"]["system"], list)
        assert seen["json"]["system"][0].get("cache_control") == {"type": "ephemeral"}
        assert seen["json"]["messages"] == [{"role": "user", "content": "hi"}]
        # 0.0 sits inside the [0, 1] clamp so the field is kept.
        assert seen["json"]["temperature"] == 0.0