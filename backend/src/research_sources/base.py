"""Source abstraction for the Deep Research engine.

Every research source (internet search, local folder, codebase, knowledge
base, …) implements the `Source` interface and yields `Finding` objects. The
`DeepResearcher` loop consumes `Finding`s the same way it consumed SearXNG
hits — this is the M1 refactor that lets us plug new sources in without
touching the iterative loop.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class SourceRef:
    """A single citable reference produced by a Source.

    `location` is the canonical handle for the reference:
      - "https://…"        for web pages
      - "file:///abs/path#L14-L42"  for local files
      - "kb://name/relative/path#L14"  for knowledge base entries
    """
    source_id: str               # adapter id, e.g. "internet", "folder", "codebase", "kb"
    title: str
    location: str
    snippet: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """One chunk of evidence surfaced by a Source.

    `content` carries the evidence text (already LLM-extracted for the
    internet source; raw chunk for local sources). `to_legacy_dict()` returns
    the same `{url, title, summary, evidence, og_image, rational}` shape the
    existing pipeline consumes — so M1's `InternetSource` is behaviorally a
    drop-in replacement for the old `_search_and_extract`.
    """
    content: str
    ref: SourceRef
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # --- legacy dict shape (consumed by DeepResearcher._format_findings,
    # ResearchHandler._extract_sources, _save_result, _fallback_report) ---
    def to_legacy_dict(self) -> Dict[str, Any]:
        """Return the legacy finding dict shape.

        For internet sources: `location` is the URL, `snippet` is summary.
        For local sources (M2+): URL slot becomes file:// ref; the rest still
        flows through the same synthesis pipeline.
        """
        return {
            "url": self.ref.location,
            "title": self.ref.title,
            "summary": self.ref.snippet or self.content[:500],
            "evidence": self.content,
            "og_image": self.metadata.get("og_image", ""),
            "rational": self.metadata.get("rational", ""),
        }


# Callable signature for the LLM function a Source may need. The internet
# source uses it to extract evidence from a fetched page; local sources do
# NOT need it (their chunks are already structured). We type it loosely so
# adapter authors don't have to import the full DeepResearcher just to type
# their constructor.
LLMFn = Callable[..., Awaitable[str]]


class Source(ABC):
    """Pluggable research source.

    Lifecycle:
      1. `warmup()`   — called once before the research loop starts; do any
                         one-time work (open collections, preload models).
      2. `retrieve()` — called every research round; returns up to `limit`
                         findings for the question/queries. `prior_refs` lists
                         locations already cited earlier in the run so
                         adapters can skip them.
      3. `shutdown()` — called once after the research loop ends; release
                         handles.

    Subclasses MUST set `type_id` (non-empty, unique across the registry)
    and SHOULD set `display_name` and `config_schema` for the UI.
    """
    type_id: str = "base"
    display_name: str = "Base"
    config_schema: Dict[str, Any] = {}

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(config or {})

    @abstractmethod
    async def retrieve(
        self,
        queries: List[str],
        *,
        question: str,
        limit: int = 10,
        prior_refs: Optional[List[str]] = None,
    ) -> List[Finding]:
        """Return up to `limit` findings for this round."""

    async def warmup(self) -> None:
        """Optional: pre-load models, open handles, etc."""
        return None

    async def shutdown(self) -> None:
        """Optional: release handles."""
        return None

    def describe(self) -> Dict[str, Any]:
        return {
            "type": self.type_id,
            "name": self.display_name,
            "config_schema": self.config_schema,
        }
