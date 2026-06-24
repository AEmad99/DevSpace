"""Hybrid orchestration helpers for multi-source research (issue #2 / M4).

A "hybrid" research session runs multiple `Source` adapters per round and
merges their findings. The merge rules are:

  1. **De-duplicate** by `(source_id, location)` — a chunk from a KB and
     a chunk from a folder that point to the same file:line are the same
     evidence; keep the higher-scoring one.
  2. **Re-rank** by score descending.
  3. **Lightly boost local hits** when the user's query signals local
     intent (contains "this codebase", "this repo", "our docs", or
     "internal"). The boost is intentionally small (1.2x) so we don't
     drown out higher-quality internet hits when the user actually wants
     both.

The orchestrator in `deep_research.py` already iterates `self.sources`;
this module is a thin helper that does the merge+boost step so the
research loop stays small.
"""
from __future__ import annotations

from typing import Iterable, List

from .base import Finding


# Substrings that, when present (case-insensitive) in the original research
# question, indicate the user wants local content prioritized. Kept short
# and obvious — anything fancier would be brittle.
_LOCAL_INTENT_KEYWORDS = (
    "this codebase",
    "this repo",
    "this repository",
    "this project",
    "this code",
    "our docs",
    "our code",
    "our codebase",
    "internal",
    "in our",
)


def merge_findings(
    source_results: Iterable[List[Finding]],
    *,
    question: str = "",
    limit: int = 20,
    local_boost: float = 1.2,
) -> List[Finding]:
    """De-duplicate and re-rank findings from multiple sources.

    Args:
        source_results: one list per Source (in priority order). Order
            matters for ties — earlier sources win.
        question: the user's original research question; used to decide
            whether to apply the local boost.
        limit: maximum number of findings to return.
        local_boost: score multiplier for local sources (folder / codebase
            / kb) when the question indicates local intent.

    Returns a flat list of `Finding` sorted by score descending, with the
    best representative kept per `(source_id, location)`.
    """
    q_lower = (question or "").lower()
    boost_local = any(kw in q_lower for kw in _LOCAL_INTENT_KEYWORDS)
    local_ids = {"folder", "codebase", "kb"}

    best: dict = {}   # (source_id, location) -> Finding
    for results in source_results:
        for f in results or []:
            key = (f.ref.source_id, f.ref.location)
            score = f.score
            if boost_local and f.ref.source_id in local_ids:
                score *= local_boost
            # Mutate a copy of the finding so the original is untouched
            # (consumers should not see per-call boost values leaking).
            if key not in best or best[key].score < score:
                # Cheap clone with new score; deepcopy is overkill.
                boosted = Finding(
                    content=f.content,
                    ref=f.ref,
                    score=score,
                    metadata=dict(f.metadata),
                )
                best[key] = boosted

    return sorted(best.values(), key=lambda x: x.score, reverse=True)[:limit]


def to_legacy_dicts(findings: List[Finding]) -> List[dict]:
    """Convert hybrid-merged Findings back to legacy dicts for the rest of the pipeline."""
    return [f.to_legacy_dict() for f in findings]
