"""Routes for the pluggable research-sources system (issue #2).

GET /api/research/sources
    List registered Source adapters so the UI can build a picker (M4).
    Always returns the built-in `internet` source. M2+ (folder, codebase,
    knowledge_base) only appear when RESEARCH_SOURCES_ENABLED is true.

GET /api/research/sources/health
    Cheap liveness probe — answers "would the registry serve anything?" —
    used by the UI to decide whether to render the picker at all.
"""
from fastapi import APIRouter

# Import the constants module (not the names) so the route re-reads the
# current value on every call. monkeypatch / test toggles of
# constants.RESEARCH_SOURCES_ENABLED take effect immediately.
from src import constants
from src.research_sources import registry

router = APIRouter(prefix="/api/research/sources", tags=["research-sources"])


@router.get("")
def list_sources() -> dict:
    """Return every registered Source the user is allowed to pick from.

    When the feature flag is off, only `internet` is listed (same behavior
    as before this feature existed). When the flag is on, every registered
    adapter (folder, codebase, kb — added in M2+) is listed.
    """
    flag = bool(getattr(constants, "RESEARCH_SOURCES_ENABLED", False))
    sources = registry.list()
    if not flag:
        sources = [s for s in sources if s["type"] == "internet"]
    return {"sources": sources, "feature_enabled": flag}


@router.get("/health")
def health() -> dict:
    """Trivial probe for the UI; helps debugging in dev."""
    flag = bool(getattr(constants, "RESEARCH_SOURCES_ENABLED", False))
    return {
        "ok": True,
        "registered": registry.types(),
        "feature_enabled": flag,
    }
