"""Tests for the pluggable research-sources system (issue #2 / M1).

What M1 promises:
  - A `Source` ABC + `Finding` + `SourceRef` contract exists.
  - A `SourceRegistry` can register and instantiate adapters.
  - `InternetSource` is the default and only registered source today.
  - `DeepResearcher` accepts a `sources=` kwarg; when omitted it builds a
    single `InternetSource(delegate=self)` so the legacy `_search_and_extract`
    path keeps running unchanged.
  - `_retrieve_via_sources` routes per-round retrieval through the sources
    list and flattens results back to the legacy dict shape.
  - A failing source logs a warning but does NOT abort the whole session.
  - `GET /api/research/sources` lists registered sources (gated by
    `RESEARCH_SOURCES_ENABLED`).
"""
import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.research_sources import (
    Finding,
    Source,
    SourceRef,
    SourceRegistry,
    registry,
)
from src.research_sources.internet import InternetSource
from src.deep_research import DeepResearcher


# ----------------------------------------------------------------------
# Registry tests
# ----------------------------------------------------------------------


def test_registry_has_internet_source_by_default():
    """The built-in InternetSource registers itself on package import."""
    assert "internet" in registry.types()


def test_registry_get_instantiates_internet_source():
    src = registry.get("internet")
    assert isinstance(src, InternetSource)
    assert src.type_id == "internet"


def test_registry_get_with_config():
    src = registry.get("internet", {"provider": "brave", "max_urls_per_round": 7})
    assert src.config["provider"] == "brave"
    assert src.max_urls_per_round == 7


def test_registry_get_unknown_type_raises():
    with pytest.raises(KeyError):
        registry.get("does_not_exist")


def test_registry_rejects_blank_type_id():
    class BadSource(Source):
        type_id = ""            # illegal: must be non-empty
        async def retrieve(self, *a, **kw):
            return []

    with pytest.raises(ValueError, match="type_id"):
        registry.register(BadSource)


def test_registry_rejects_default_base_type_id():
    class AlsoBadSource(Source):  # type_id inherits "base" from Source
        async def retrieve(self, *a, **kw):
            return []

    with pytest.raises(ValueError, match="type_id"):
        registry.register(AlsoBadSource)


def test_registry_rejects_duplicate_type_id():
    class DupSource(Source):
        type_id = "internet"    # already registered by InternetSource
        async def retrieve(self, *a, **kw):
            return []

    with pytest.raises(ValueError, match="already registered"):
        registry.register(DupSource)


def test_registry_register_is_idempotent_for_same_class():
    """Re-registering the SAME class (not a duplicate registration) is a no-op."""
    # InternetSource is already registered. Doing it again should be fine.
    registry.register(InternetSource)  # must not raise
    assert "internet" in registry.types()


def test_registry_list_returns_ui_shape():
    out = registry.list()
    assert all(set(s) == {"type", "name", "config_schema"} for s in out)
    internet = next(s for s in out if s["type"] == "internet")
    assert internet["name"] == "Internet"
    assert "provider" in internet["config_schema"]


# ----------------------------------------------------------------------
# Finding / SourceRef bridge tests
# ----------------------------------------------------------------------


def test_finding_to_legacy_dict_internet_shape():
    """An internet-source Finding converts back to the legacy dict shape."""
    f = Finding(
        content="long evidence text…",
        ref=SourceRef(
            source_id="internet",
            title="Example page",
            location="https://example.test/page",
            snippet="summary text",
        ),
        # InternetSource stores og_image / rational on the Finding's own
        # metadata (not on the SourceRef's) — matches the wire format.
        metadata={"og_image": "https://img", "rational": "r"},
    )
    d = f.to_legacy_dict()
    assert d["url"] == "https://example.test/page"
    assert d["title"] == "Example page"
    assert d["summary"] == "summary text"
    assert d["evidence"] == "long evidence text…"
    assert d["og_image"] == "https://img"
    assert d["rational"] == "r"


def test_finding_to_legacy_dict_falls_back_to_content_when_no_snippet():
    """Local sources (M2) won't have a separate snippet — use content head."""
    f = Finding(
        content="some file content line 1\nline 2\nline 3",
        ref=SourceRef(source_id="folder", title="foo.md", location="file:///x/foo.md#L1-L3"),
    )
    d = f.to_legacy_dict()
    assert d["url"] == "file:///x/foo.md#L1-L3"
    # snippet empty → summary falls back to first 500 chars of content
    assert d["summary"] == f.content[:500]
    assert d["evidence"] == f.content


# ----------------------------------------------------------------------
# DeepResearcher integration tests
# ----------------------------------------------------------------------


def _make_researcher(**kwargs) -> DeepResearcher:
    defaults = dict(
        llm_endpoint="http://local.test/v1",
        llm_model="local-model",
        max_time=5,
        max_rounds=1,
    )
    defaults.update(kwargs)
    return DeepResearcher(**defaults)


def test_deep_researcher_defaults_to_internet_source():
    r = _make_researcher()
    assert len(r.sources) == 1
    assert isinstance(r.sources[0], InternetSource)
    # The default InternetSource delegates back to the parent so subclass
    # overrides of _search / _fetch_and_extract keep working.
    assert r.sources[0].delegate is r


def test_deep_researcher_accepts_explicit_sources_list():
    fake = _make_fake_source([Finding(
        content="evidence",
        ref=SourceRef(source_id="fake", title="t", location="file:///x"),
    )])
    r = _make_researcher(sources=[fake])
    assert r.sources == [fake]


def test_researcher_attribute_aliases_for_back_compat():
    """Existing callers read these attributes; they must remain on DeepResearcher."""
    r = _make_researcher()
    # The legacy attribute names must still exist and be set to sane defaults.
    assert r.search_provider_override is None
    assert r.category is None
    assert r.max_urls_per_round == 3
    assert r.extraction_concurrency >= 1


# ----------------------------------------------------------------------
# _retrieve_via_sources behavior
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_via_sources_flattens_findings_to_legacy_dicts():
    r = _make_researcher()

    f1 = Finding(content="a", ref=SourceRef(source_id="fake", title="t1", location="loc1"))
    f2 = Finding(content="b", ref=SourceRef(source_id="fake", title="t2", location="loc2"))
    src = _make_fake_source([f1, f2])
    r.sources = [src]

    out = await r._retrieve_via_sources(["q1", "q2"], question="what?")
    assert len(out) == 2
    # Legacy shape keys are all present.
    for d in out:
        assert set(d.keys()) >= {"url", "title", "summary", "evidence", "og_image", "rational"}
    assert {d["url"] for d in out} == {"loc1", "loc2"}


@pytest.mark.asyncio
async def test_retrieve_via_sources_aggregates_multiple_sources():
    r = _make_researcher()
    src_a = _make_fake_source([Finding(content="a", ref=SourceRef("a", "ta", "la"))])
    src_b = _make_fake_source([Finding(content="b", ref=SourceRef("b", "tb", "lb"))])
    r.sources = [src_a, src_b]

    out = await r._retrieve_via_sources(["q"], question="x")
    assert {d["url"] for d in out} == {"la", "lb"}


@pytest.mark.asyncio
async def test_retrieve_via_sources_failing_source_does_not_abort():
    """A throwing source logs + continues; the other source's findings still come through."""
    r = _make_researcher()

    class BoomSource(Source):
        type_id = "boom"
        async def retrieve(self, *a, **kw):
            raise RuntimeError("kaboom")

    good = _make_fake_source([Finding(content="ok", ref=SourceRef("g", "t", "loc"))])
    r.sources = [BoomSource(), good]

    out = await r._retrieve_via_sources(["q"], question="x")
    assert len(out) == 1
    assert out[0]["url"] == "loc"


@pytest.mark.asyncio
async def test_retrieve_via_sources_passes_queries_and_question_to_source():
    r = _make_researcher()
    captured: Dict[str, Any] = {}

    class SpySource(Source):
        type_id = "spy"
        async def retrieve(self, queries, *, question, limit, prior_refs):
            captured["queries"] = list(queries)
            captured["question"] = question
            captured["limit"] = limit
            captured["prior_refs"] = list(prior_refs or [])
            return []

    r.sources = [SpySource()]
    await r._retrieve_via_sources(["q1", "q2"], question="the Q")
    assert captured["queries"] == ["q1", "q2"]
    assert captured["question"] == "the Q"
    # limit defaults to max_urls_per_round * len(queries) (3 * 2 = 6)
    assert captured["limit"] == 6


@pytest.mark.asyncio
async def test_retrieve_via_sources_returns_empty_when_no_sources():
    r = _make_researcher()
    r.sources = []
    out = await r._retrieve_via_sources(["q"], question="x")
    assert out == []


# ----------------------------------------------------------------------
# End-to-end: legacy behavior preserved via InternetSource(delegate=self)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_search_and_extract_still_callable_directly():
    """The existing tests subclass DeepResearcher and override _search /
    _fetch_and_extract. Calling _search_and_extract directly must keep
    using the subclass overrides — i.e., the refactor didn't move the
    real logic out from under the subclass.
    """
    import time as _time

    class Controlled(DeepResearcher):
        def __init__(self, *a, **kw):
            super().__init__(
                llm_endpoint="http://local.test/v1",
                llm_model="m",
                max_urls_per_round=2,
                extraction_concurrency=2,
                *a, **kw,
            )

        async def _search(self, q):
            return [{"url": f"https://e/{q}/{i}", "title": f"{q}{i}"} for i in range(2)]

        async def _fetch_and_extract(self, url, question, title):
            await asyncio.sleep(0)
            return {"url": url, "title": title, "summary": "ok"}

    r = Controlled()
    # _search_and_extract short-circuits on _time_exceeded() when
    # _start_time is 0 (pre-existing quirk; existing tests set it manually).
    r._start_time = _time.time()
    findings = await r._search_and_extract(["a", "b"], "Q")
    # 2 URLs per query × 2 queries = 4 findings.
    assert len(findings) == 4
    assert all(f["summary"] == "ok" for f in findings)


@pytest.mark.asyncio
async def test_round_loop_routes_through_internet_source_with_delegate():
    """End-to-end: _retrieve_via_sources → InternetSource(delegate)
    → delegate._search_and_extract → subclass _search + _fetch_and_extract.
    Verifies the whole chain produces findings.
    """
    import time as _time

    class Controlled(DeepResearcher):
        async def _search(self, q):
            return [{"url": f"https://e/{q}", "title": q}]

        async def _fetch_and_extract(self, url, question, title):
            await asyncio.sleep(0)
            return {"url": url, "title": title, "summary": f"summary-{title}"}

    r = Controlled(llm_endpoint="http://local.test/v1", llm_model="m",
                   max_rounds=1, max_time=5)
    # _search_and_extract on the delegate short-circuits on _time_exceeded()
    # when _start_time is 0 (pre-existing quirk; the real research() loop
    # sets it in `research()` before the first round).
    r._start_time = _time.time()
    out = await r._retrieve_via_sources(["foo"], question="Q")
    assert len(out) == 1
    assert out[0]["url"] == "https://e/foo"
    assert out[0]["summary"] == "summary-foo"


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def test_list_sources_endpoint_returns_internet(monkeypatch):
    """The /api/research/sources route lists the built-in internet source."""
    from routes.research_sources_routes import list_sources

    # Default: flag off, only internet listed.
    from src import constants as c
    monkeypatch.setattr(c, "RESEARCH_SOURCES_ENABLED", False)
    out = list_sources()
    assert out["feature_enabled"] is False
    assert [s["type"] for s in out["sources"]] == ["internet"]

    # Flag on: same result today (no other sources registered yet), but
    # feature_enabled flips.
    monkeypatch.setattr(c, "RESEARCH_SOURCES_ENABLED", True)
    out = list_sources()
    assert out["feature_enabled"] is True
    assert any(s["type"] == "internet" for s in out["sources"])


def test_list_sources_hides_non_internet_when_flag_off():
    """Register a temp source, confirm it's hidden when the flag is off."""
    from src import constants as c
    saved_flag = c.RESEARCH_SOURCES_ENABLED

    class TempSource(Source):
        type_id = "temp_test_only"
        async def retrieve(self, *a, **kw):
            return []

    try:
        registry.register(TempSource)
        c.RESEARCH_SOURCES_ENABLED = False
        from routes.research_sources_routes import list_sources
        types = [s["type"] for s in list_sources()["sources"]]
        assert "temp_test_only" not in types
        assert "internet" in types

        c.RESEARCH_SOURCES_ENABLED = True
        types = [s["type"] for s in list_sources()["sources"]]
        assert "temp_test_only" in types
    finally:
        c.RESEARCH_SOURCES_ENABLED = saved_flag
        registry.unregister("temp_test_only")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _make_fake_source(findings: List[Finding]) -> Source:
    """Build a minimal Source subclass that returns the given findings."""
    captured: Dict[str, Any] = {}

    class FakeSource(Source):
        type_id = f"fake_{id(findings)}"   # unique per call
        async def retrieve(self, queries, *, question, limit, prior_refs):
            captured["calls"] = captured.get("calls", 0) + 1
            return list(findings)

    return FakeSource()
