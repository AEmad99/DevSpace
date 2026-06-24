"""`InternetSource` — wraps the existing SearXNG/Brave/Tavily search + LLM
extraction pipeline as a `Source` adapter.

This is the M1 refactor target: it moves the per-round search + extract logic
out of `DeepResearcher._search_and_extract` and into a pluggable adapter,
WITHOUT changing any behavior. Findings produced here are byte-for-byte
equivalent to what the old code returned (same {url, title, summary,
evidence, og_image, rational} shape via `Finding.to_legacy_dict()`).

Future sources (folder, codebase, knowledge base) will live alongside this
file in `src/research_sources/` and will be registered into the same
`SourceRegistry`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .base import Finding, LLMFn, Source, SourceRef
from .registry import registry

logger = logging.getLogger(__name__)


# Default caps lifted from the original DeepResearcher.__init__ parameters.
_DEFAULTS = {
    "max_urls_per_round": 3,
    "max_content_chars": 15000,
    "extraction_concurrency": 3,
    "extraction_timeout": 90,
}


@registry.register
class InternetSource(Source):
    """Search the web (via configured provider) and LLM-extract evidence.

    Config keys:
        provider              — "searxng" | "brave" | "tavily" | "duckduckgo" | "disabled" | None
                                None  → use the user's settings.json
                                "disabled" → return no findings
        category              — optional category hint forwarded to the provider
        max_urls_per_round    — total URL budget per round (default 3)
        max_content_chars     — page-content cap before LLM extraction (default 15000)
        extraction_concurrency — page-fetch concurrency (default 3)
        extraction_timeout    — LLM extraction call timeout, seconds (default 90)
        emit                  — optional progress callback (called with kwargs)
        set_last_error        — optional callable(str) to record last provider error
    """
    type_id = "internet"
    display_name = "Internet"
    config_schema = {
        "provider": {"type": "string", "default": None,
                     "enum": ["searxng", "brave", "tavily", "duckduckgo", "disabled", None]},
        "category": {"type": "string", "default": None},
        "max_urls_per_round": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
        "max_content_chars": {"type": "integer", "default": 15000, "minimum": 1000},
        "extraction_concurrency": {"type": "integer", "default": 3, "minimum": 1, "maximum": 12},
        "extraction_timeout": {"type": "integer", "default": 90, "minimum": 15, "maximum": 3600},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None, *, delegate=None):
        super().__init__(config)
        # The orchestrator (DeepResearcher) passes itself as `delegate` so
        # `retrieve()` can call back into the existing _search_and_extract /
        # _search / _fetch_and_extract methods. This is what lets the M1
        # refactor preserve byte-for-byte behavior: subclass overrides of
        # those methods (see tests/test_deep_research_extraction_controls.py)
        # continue to be honored, and all the existing side effects on the
        # parent (urls_fetched, analyzed_urls, providers_used,
        # _last_search_error) keep happening on the parent where the rest of
        # the pipeline reads them.
        self.delegate = delegate
        # Normalize numeric caps the same way DeepResearcher did:
        #   max_urls_per_round → no clamp (kept as-is for behavior parity)
        #   max_content_chars  → no clamp
        #   extraction_concurrency → clamp(1, 12, x)
        #   extraction_timeout    → clamp(15, 3600, x)
        self.provider: Optional[str] = (self.config.get("provider") or None)
        self.category: Optional[str] = self.config.get("category") or None
        self.max_urls_per_round: int = int(self.config.get("max_urls_per_round", _DEFAULTS["max_urls_per_round"]))
        self.max_content_chars: int = int(self.config.get("max_content_chars", _DEFAULTS["max_content_chars"]))
        self.extraction_concurrency: int = min(
            12, max(1, int(self.config.get("extraction_concurrency", _DEFAULTS["extraction_concurrency"])))
        )
        self.extraction_timeout: int = min(
            3600, max(15, int(self.config.get("extraction_timeout", _DEFAULTS["extraction_timeout"])))
        )
        # Injectable side-channels so the orchestrator can keep its existing
        # telemetry hooks (SSE emit, _last_search_error, providers_used).
        # Set by DeepResearcher.__init__ when wrapping in this adapter.
        self._emit = self.config.get("emit") or (lambda **_kw: None)
        self._set_last_error = self.config.get("set_last_error") or (lambda _msg: None)
        self._on_provider_used = self.config.get("on_provider_used") or (lambda _name: None)
        # The LLM function the source uses for per-page evidence extraction.
        # Resolved lazily so importing this module doesn't pull llm_core
        # (tests that never extract evidence shouldn't pay the cost).
        self._llm_fn: Optional[LLMFn] = self.config.get("llm_fn")

    async def _llm(self, messages, **kwargs) -> str:
        """Resolve the configured LLM callable, falling back to llm_call_async.

        The orchestrator (DeepResearcher) injects its bound `_llm` via
        config["llm_fn"] so headers / endpoint / model stay consistent.
        This fallback is only used by direct unit tests of the source.
        """
        if self._llm_fn is not None:
            return await self._llm_fn(messages, **kwargs)
        from src.llm_core import llm_call_async
        from src.research_utils import strip_thinking
        resp = await llm_call_async(
            url="", model="", messages=messages,
            headers=None,
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 2048),
            timeout=kwargs.get("timeout", 90),
        )
        return strip_thinking(resp)

    # ------------------------------------------------------------------
    # Source lifecycle
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        # Nothing to preload — search provider is lazy and the LLM is per-call.
        return None

    async def shutdown(self) -> None:
        return None

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        queries: List[str],
        *,
        question: str,
        limit: int = 10,
        prior_refs: Optional[List[str]] = None,
    ) -> List[Finding]:
        """Run all queries in parallel, fetch top URLs, LLM-extract evidence.

        Returns a flat list of `Finding` objects whose `.to_legacy_dict()` is
        the same dict shape the legacy `_search_and_extract` produced.

        When `delegate` is set (the M1 path inside DeepResearcher), we call
        `delegate._search_and_extract()` so subclass overrides of `_search`
        and `_fetch_and_extract` keep working unchanged. When `delegate` is
        None (e.g. direct use from tests or a future standalone mode), we
        run the self-contained search + extract pipeline defined below.
        """
        # ----- Delegate path (M1 inside DeepResearcher) ----------------
        if self.delegate is not None:
            legacy = await self.delegate._search_and_extract(queries, question)
            return [self._wrap(d) for d in (legacy or [])]

        # ----- Self-contained path (direct / future use) --------------
        prior_refs = set(prior_refs or [])
        # The legacy cap was: max_urls_per_round * len(queries). Preserve it.
        cap = self.max_urls_per_round * len(queries)

        # ---- 1. search all queries in parallel ----
        search_tasks = [self._search(q) for q in queries]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        urls_to_fetch: List[Dict[str, Any]] = []
        seen: set = set()
        for result in search_results:
            if isinstance(result, Exception):
                logger.warning(f"Search error: {result}")
                continue
            if not result:
                continue
            for r in result:
                url = r.get("url", "")
                if not url or url in seen or url in prior_refs:
                    continue
                seen.add(url)
                urls_to_fetch.append(r)
                if len(urls_to_fetch) >= cap:
                    break
            if len(urls_to_fetch) >= cap:
                break

        if not urls_to_fetch:
            return []

        # ---- 2. fetch + LLM-extract with backpressure ----
        semaphore = asyncio.Semaphore(self.extraction_concurrency)

        async def _bounded_extract(result: Dict[str, Any]):
            async with semaphore:
                return await self._fetch_and_extract(
                    result["url"], question, result.get("title", "")
                )

        extracted = await asyncio.gather(
            *[_bounded_extract(r) for r in urls_to_fetch],
            return_exceptions=True,
        )

        out: List[Finding] = []
        for r in extracted:
            if isinstance(r, Exception):
                logger.warning(f"Extraction error: {r}")
                continue
            if r is None:
                continue
            out.append(self._wrap(r))
        return out

    # ------------------------------------------------------------------
    # Internals — preserve existing DeepResearcher behavior exactly
    # ------------------------------------------------------------------

    async def _search(self, query: str) -> List[Dict[str, Any]]:
        """Run a search query using the configured research search provider."""
        try:
            from src.search.providers import _get_search_settings
            from src.search.core import _call_provider, _build_provider_chain

            settings = _get_search_settings()
            provider = (self.provider or "").strip()
            if not provider:
                provider = (settings.get("research_search_provider") or "").strip()
            if not provider:
                provider = settings.get("search_provider", "searxng")

            if provider == "disabled":
                logger.info("Search is disabled for research")
                return []

            chain = _build_provider_chain(provider)
            raised = False
            for prov in chain:
                try:
                    results = await asyncio.to_thread(_call_provider, prov, query, 10)
                    if results:
                        logger.info(f"Research search: {prov} returned {len(results)} results")
                        self._on_provider_used(prov)
                        return results
                except Exception as e:
                    raised = True
                    logger.warning(f"Research search: {prov} failed: {e}")
                    self._set_last_error(f"{prov}: {e}")
            if not raised:
                self._set_last_error(
                    f"no results from search provider(s): "
                    f"{', '.join(chain) if chain else provider}"
                )
            return []
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            self._set_last_error(str(e))
            return []

    async def _fetch_and_extract(
        self, url: str, question: str, title: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a URL's content and use LLM to extract relevant info.

        Identical to the old DeepResearcher._fetch_and_extract — preserved
        bit-for-bit so M1 has zero behavior diff.
        """
        display = title or url
        self._emit(phase="reading", url=url, title=display)
        try:
            from src.search import fetch_webpage_content
            page = await asyncio.to_thread(fetch_webpage_content, url, 10)
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            return None

        if not page.get("success") or not page.get("content"):
            return None

        content = page["content"]
        # Truncate at paragraph boundary when possible — match legacy logic.
        if len(content) > self.max_content_chars:
            truncated = content[:self.max_content_chars]
            last_para = truncated.rfind("\n\n")
            if last_para > self.max_content_chars * 0.8:
                content = truncated[:last_para]
            else:
                content = truncated

        try:
            from src.goal_based_extractor import EXTRACTOR_SYSTEM
            from src.prompt_security import untrusted_context_message
            from src.research_utils import is_low_quality

            response = await self._llm(
                [
                    {"role": "user", "content": EXTRACTOR_SYSTEM.format(goal=question)},
                    untrusted_context_message("webpage", content),
                ],
                temperature=0.2,
                max_tokens=2048,
                timeout=self.extraction_timeout,
            )
            from src.deep_research import DeepResearcher  # for _parse_json_object
            parsed = DeepResearcher._parse_json_object(response)  # type: ignore[arg-type]
            if parsed:
                parsed["url"] = url
                parsed["title"] = title or page.get("title", "")
                parsed["og_image"] = page.get("og_image", "")
                if is_low_quality(parsed.get("summary", "")):
                    logger.info(f"Skipping low-quality extraction from {url}")
                    return None
                return parsed
            return {
                "url": url,
                "title": title or page.get("title", ""),
                "og_image": page.get("og_image", ""),
                "rational": "LLM extraction (raw)",
                "evidence": response[:3000],
                "summary": response[:500],
            }
        except Exception as e:
            logger.warning(f"LLM extraction failed for {url}: {e}")
            return None

    @staticmethod
    def _wrap(d: Dict[str, Any]) -> Finding:
        """Convert legacy finding dict → Finding object."""
        url = d.get("url", "")
        return Finding(
            content=d.get("evidence") or d.get("summary", ""),
            ref=SourceRef(
                source_id="internet",
                title=d.get("title", "") or url,
                location=url,
                snippet=d.get("summary", "") or "",
                metadata={
                    "og_image": d.get("og_image", ""),
                    "rational": d.get("rational", ""),
                },
            ),
            metadata={},
        )
