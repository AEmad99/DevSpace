"""Tests for LibrarySource and PreviousChatsSource.

Covers:
  - Construction: required config keys, default values
  - Registry: both source types are registered
  - Empty report_ids → no findings (Library)
  - retrieve() pulls relevant chunks from the collection
  - User scoping: report/chat ownership filter

The tests stub out ChromaDB with an in-memory FakeCollection and
short-circuit the embedding lane so the suite runs without chromadb /
fastembed installed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.research_sources import registry
from src.research_sources.base import Finding, SourceRef
from src.research_sources.library import LibrarySource
from src.research_sources.previous_chats import PreviousChatsSource


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeCollection:
    def __init__(self):
        self._items: Dict[str, Dict[str, Any]] = {}

    def upsert(self, *, ids, documents, metadatas, embeddings=None):
        for i, cid in enumerate(ids):
            self._items[cid] = {
                "doc": documents[i],
                "meta": dict(metadatas[i]),
                "emb": embeddings[i] if embeddings is not None else None,
            }

    def delete(self, *, ids):
        for cid in ids:
            self._items.pop(cid, None)

    def get(self, *, include=None):
        ids = list(self._items.keys())
        metas = [self._items[i]["meta"] for i in ids]
        out = {"ids": ids, "metadatas": metas}
        if include and "documents" in include:
            out["documents"] = [self._items[i]["doc"] for i in ids]
        return out

    def query(self, *, query_embeddings, n_results, where=None):
        if not query_embeddings:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        q = query_embeddings[0]
        scored: List[tuple] = []
        for cid, item in self._items.items():
            meta = item["meta"]
            if where:
                if "report_id" in where and meta.get("report_id") not in where["report_id"].get("$in", []):
                    continue
                if "session_id" in where and meta.get("session_id") not in where["session_id"].get("$in", []):
                    continue
            emb = item["emb"] or [0.0] * len(q)
            # Cosine distance (1 - similarity) for normalized vectors.
            d = 1.0 - sum(a * b for a, b in zip(q, emb))
            scored.append((d, cid, item))
        scored.sort(key=lambda x: x[0])
        top = scored[:n_results]
        return {
            "documents": [[t[2]["doc"] for t in top]],
            "metadatas": [[t[2]["meta"] for t in top]],
            "distances": [[t[0] for t in top]],
        }


class FakeLane:
    def __init__(self):
        self.collection = FakeCollection()
        # 4096-dim bag-of-words hashing — large enough that hash
        # collisions between unrelated words are vanishingly rare, so
        # the test ordering reflects actual word overlap.
        import hashlib
        self._dim = 4096

    def encode(self, texts):
        import hashlib
        out = []
        for t in texts:
            v = [0.0] * self._dim
            for word in t.lower().split():
                h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
                v[h % self._dim] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1
            out.append([x / norm for x in v])
        return out


# ----------------------------------------------------------------------
# LibrarySource
# ----------------------------------------------------------------------


def test_library_source_registers_in_registry():
    """LibrarySource is registered via @registry.register on import."""
    assert "library" in registry.types()
    src = registry.get("library", {"owner": "alice"})
    assert isinstance(src, LibrarySource)
    assert src.type_id == "library"


def test_library_source_default_config():
    src = LibrarySource({"owner": "alice"})
    assert src.owner == "alice"
    assert src.report_ids == []
    assert src.limit_per_report == 3
    assert src.collection_name == "library_alice"


def test_library_source_empty_collection_name_for_single_user():
    src = LibrarySource({"owner": ""})
    assert src.collection_name == "library_default"


def test_library_source_empty_report_ids_returns_no_findings(tmp_path):
    """UX contract: empty multi-select = no findings."""
    src = LibrarySource({"owner": "alice", "report_ids": []})
    # Even if the collection has data, the empty filter should short-circuit.
    lane = FakeLane()
    with patch.object(src, "_resolve_lane", return_value=lane):
        out = asyncio_run(src.retrieve(["test query"], question="q"))
    assert out == []


def test_library_source_warmup_indexes_reports(tmp_path):
    """Warmup reads research JSONs from disk, chunks, and embeds."""
    from src.constants import DEEP_RESEARCH_DIR
    data_dir = tmp_path / "deep_research"
    data_dir.mkdir()
    # Two reports owned by alice; one owned by bob (must be excluded).
    (data_dir / "rep1.json").write_text(json.dumps({
        "query": "Question about cats",
        "result": "Cats knead with their paws because of kittenhood instincts.",
        "status": "done",
        "owner": "alice",
        "completed_at": 1000.0,
    }))
    (data_dir / "rep2.json").write_text(json.dumps({
        "query": "Best SSDs",
        "result": "Top SSDs: Samsung 990 Pro, WD Black SN850X.",
        "status": "done",
        "owner": "alice",
        "completed_at": 1100.0,
    }))
    (data_dir / "rep3.json").write_text(json.dumps({
        "query": "Bob's secret",
        "result": "Should never appear in alice's library.",
        "status": "done",
        "owner": "bob",
        "completed_at": 1200.0,
    }))

    src = LibrarySource({"owner": "alice", "report_ids": ["rep1", "rep2"]})
    lane = FakeLane()
    with patch("src.constants.DEEP_RESEARCH_DIR", str(data_dir)), \
         patch.object(src, "_resolve_lane", return_value=lane):
        asyncio_run(src.warmup())

    # Two of alice's reports indexed; bob's excluded.
    ids = list(lane.collection._items.keys())
    rep_ids = {lane.collection._items[i]["meta"].get("report_id") for i in ids}
    assert "rep1" in rep_ids
    assert "rep2" in rep_ids
    assert "rep3" not in rep_ids


def test_library_source_retrieve_returns_relevant_chunks(tmp_path):
    """retrieve() returns top chunks from the indexed reports."""
    from src.constants import DEEP_RESEARCH_DIR
    data_dir = tmp_path / "deep_research"
    data_dir.mkdir()
    (data_dir / "rep1.json").write_text(json.dumps({
        "query": "knead",
        "result": "Cats knead with their paws to mark territory. " * 30,
        "status": "done",
        "owner": "alice",
        "completed_at": 1000.0,
    }))
    (data_dir / "rep2.json").write_text(json.dumps({
        "query": "ssd",
        "result": "The Samsung 990 Pro is a fast NVMe drive. " * 30,
        "status": "done",
        "owner": "alice",
        "completed_at": 1100.0,
    }))
    src = LibrarySource({"owner": "alice", "report_ids": ["rep1", "rep2"]})
    lane = FakeLane()
    with patch("src.constants.DEEP_RESEARCH_DIR", str(data_dir)), \
         patch.object(src, "_resolve_lane", return_value=lane):
        asyncio_run(src.warmup())
        findings = asyncio_run(src.retrieve(
            ["knead paws"], question="why cats knead", limit=3
        ))
    assert findings
    # All returned findings should belong to the knead report.
    for f in findings:
        assert f.ref.source_id == "library"
        assert "rep1" in f.ref.location or "rep2" in f.ref.location
    # The top hit should be from rep1 (knead matches).
    assert "rep1" in findings[0].ref.location


def test_library_source_filter_restricts_to_chosen_reports(tmp_path):
    """When report_ids is non-empty, retrieve() must not return findings from
    reports the user did not select."""
    from src.constants import DEEP_RESEARCH_DIR
    data_dir = tmp_path / "deep_research"
    data_dir.mkdir()
    for i, body in enumerate([
        "Alpha bravo charlie delta echo foxtrot golf hotel india juliet",
        "Kilo lima mike november oscar papa quebec romeo sierra tango",
    ]):
        (data_dir / f"r{i}.json").write_text(json.dumps({
            "query": f"q{i}",
            "result": body,
            "status": "done",
            "owner": "alice",
            "completed_at": 1000.0 + i,
        }))
    src = LibrarySource({"owner": "alice", "report_ids": ["r0"]})  # only r0 selected
    lane = FakeLane()
    with patch("src.constants.DEEP_RESEARCH_DIR", str(data_dir)), \
         patch.object(src, "_resolve_lane", return_value=lane):
        asyncio_run(src.warmup())
        # Ask about "kilo" — that's only in r1, which is NOT selected.
        # retrieve() should return only r0 chunks (or nothing, but never r1).
        findings = asyncio_run(src.retrieve(["kilo"], question="k", limit=5))
    # No finding may come from the unselected r1.
    for f in findings:
        assert "r1" not in f.ref.location, f"r1 leaked into findings: {f.ref.location}"


# ----------------------------------------------------------------------
# PreviousChatsSource
# ----------------------------------------------------------------------


def test_chats_source_registers_in_registry():
    assert "chats" in registry.types()
    src = registry.get("chats", {"owner": "alice"})
    assert isinstance(src, PreviousChatsSource)
    assert src.type_id == "chats"


def test_chats_source_default_config():
    src = PreviousChatsSource({"owner": "alice"})
    assert src.owner == "alice"
    assert src.collection_name == "chats_alice"


def test_chats_source_warmup_indexes_sessions():
    """Warmup walks the DB and embeds every non-archived session."""
    # Hand-rolled fakes — MagicMock breaks ``.message_count > 0`` (which
    # raises TypeError when message_count is a MagicMock) and
    # ``.last_message_at.desc().nullslast()`` (which returns a MagicMock
    # that the production code expects to behave like a SQLAlchemy
    # expression). A real class with real attributes sidesteps both.
    fake_db_session = type("S", (), {})()
    fake_db_session.id = "s1"
    fake_db_session.name = "Cooking"
    fake_db_session.last_message_at = None
    fake_db_session.archived = False
    fake_db_session.owner = "alice"
    fake_db_session.message_count = 2

    fake_msg_1 = type("M", (), {})()
    fake_msg_1.role = "user"
    fake_msg_1.content = "How do I make pasta?"
    fake_msg_1.meta_data = None
    fake_msg_1.session_id = "s1"
    fake_msg_2 = type("M", (), {})()
    fake_msg_2.role = "assistant"
    fake_msg_2.content = "Boil water, add salt, cook 8 minutes."
    fake_msg_2.meta_data = None
    fake_msg_2.session_id = "s1"

    # A no-op "column descriptor" so .desc() / .nullslast() chain cleanly.
    class _Expr:
        """Generic SQLAlchemy-expression stand-in. Carries an opaque tag."""
        def __init__(self, tag): self.tag = tag
        def __repr__(self): return f"Expr({self.tag!r})"
        def __or__(self, other): return _Expr(("or", self.tag, other if isinstance(other, _Expr) else other))
        def __and__(self, other): return _Expr(("and", self.tag, other if isinstance(other, _Expr) else other))
        def __ror__(self, other): return _Expr(("or", other, self.tag))
        def __rand__(self, other): return _Expr(("and", other, self.tag))

    class _Col:
        def __init__(self, name): self.name = name
        def desc(self): return _ColDesc(self.name)
        def asc(self): return _ColAsc(self.name)
        def is_(self, other): return _Expr(("is", self.name, other))
        def in_(self, other): return _Expr(("in", self.name, list(other)))
        def __eq__(self, other): return _Expr(("eq", self.name, other))
        def __ne__(self, other): return _Expr(("ne", self.name, other))
        def __gt__(self, other): return _Expr(("gt", self.name, other))
        def __lt__(self, other): return _Expr(("lt", self.name, other))
        def __or__(self, other): return _Expr(("or", self.name, other))
        def __and__(self, other): return _Expr(("and", self.name, other))
    class _ColDesc(_Expr):
        def __init__(self, name): super().__init__(("desc", name))
        def nullslast(self): return _Expr(("nullslast", self.tag))
        def nullsfirst(self): return _Expr(("nullsfirst", self.tag))
    class _ColAsc(_Expr):
        def __init__(self, name): super().__init__(("asc", name))
        def nullslast(self): return _Expr(("nullslast", self.tag))
        def nullsfirst(self): return _Expr(("nullsfirst", self.tag))

    class FakeSession:
        message_count = _Col("message_count")
        last_message_at = _Col("last_message_at")
        archived = _Col("archived")
        owner = _Col("owner")
    class FakeMsg:
        session_id = _Col("session_id")
        timestamp = _Col("timestamp")

    class FakeQuery:
        def filter(self, *a, **kw): return self
        def order_by(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
        def all(self): return [fake_db_session]
    class FakeMsgQuery:
        def filter(self, *a, **kw): return self
        def order_by(self, *a, **kw): return self
        def all(self): return [fake_msg_1, fake_msg_2]

    fake_db = MagicMock()
    fake_db.query.side_effect = lambda M: FakeQuery() if M is FakeSession else FakeMsgQuery()

    src = PreviousChatsSource({"owner": "alice"})
    lane = FakeLane()
    with patch.dict("sys.modules", {
        "core": MagicMock(),
        "core.database": MagicMock(
            Session=FakeSession,
            ChatMessage=FakeMsg,
            SessionLocal=MagicMock(return_value=fake_db),
        ),
    }), patch.object(src, "_resolve_lane", return_value=lane):
        asyncio_run(src.warmup())
    ids = list(lane.collection._items.keys())
    assert any("s1" in cid for cid in ids)


def test_chats_source_retrieve_returns_findings():
    """retrieve() pulls top chunks across all sessions for the user."""
    fake_db_session = type("S", (), {})()
    fake_db_session.id = "s1"
    fake_db_session.name = "Pasta"
    fake_db_session.last_message_at = None
    fake_db_session.archived = False
    fake_db_session.owner = "alice"
    fake_db_session.message_count = 1
    fake_msg = type("M", (), {})()
    fake_msg.role = "user"
    fake_msg.content = "Tell me about fresh pasta making " * 20
    fake_msg.meta_data = None
    fake_msg.session_id = "s1"

    class _Expr:
        def __init__(self, tag): self.tag = tag
        def __repr__(self): return f"Expr({self.tag!r})"
        def __or__(self, other): return _Expr(("or", self.tag, other if isinstance(other, _Expr) else other))
        def __and__(self, other): return _Expr(("and", self.tag, other if isinstance(other, _Expr) else other))
        def __ror__(self, other): return _Expr(("or", other, self.tag))
        def __rand__(self, other): return _Expr(("and", other, self.tag))

    class _Col:
        def __init__(self, name): self.name = name
        def desc(self): return _ColDesc(self.name)
        def asc(self): return _ColAsc(self.name)
        def is_(self, other): return _Expr(("is", self.name, other))
        def in_(self, other): return _Expr(("in", self.name, list(other)))
        def __eq__(self, other): return _Expr(("eq", self.name, other))
        def __ne__(self, other): return _Expr(("ne", self.name, other))
        def __gt__(self, other): return _Expr(("gt", self.name, other))
        def __lt__(self, other): return _Expr(("lt", self.name, other))
        def __or__(self, other): return _Expr(("or", self.name, other))
        def __and__(self, other): return _Expr(("and", self.name, other))
    class _ColDesc(_Expr):
        def __init__(self, name): super().__init__(("desc", name))
        def nullslast(self): return _Expr(("nullslast", self.tag))
        def nullsfirst(self): return _Expr(("nullsfirst", self.tag))
    class _ColAsc(_Expr):
        def __init__(self, name): super().__init__(("asc", name))
        def nullslast(self): return _Expr(("nullslast", self.tag))
        def nullsfirst(self): return _Expr(("nullsfirst", self.tag))

    class FakeSession:
        message_count = _Col("message_count")
        last_message_at = _Col("last_message_at")
        archived = _Col("archived")
        owner = _Col("owner")
    class FakeMsg:
        session_id = _Col("session_id")
        timestamp = _Col("timestamp")

    class FakeQ:
        def filter(self, *a, **kw): return self
        def order_by(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
        def all(self): return [fake_db_session]
    class FakeMQ:
        def filter(self, *a, **kw): return self
        def order_by(self, *a, **kw): return self
        def all(self): return [fake_msg]
    fake_db = MagicMock()
    fake_db.query.side_effect = lambda M: FakeQ() if M is FakeSession else FakeMQ()

    src = PreviousChatsSource({"owner": "alice"})
    lane = FakeLane()
    with patch.dict("sys.modules", {
        "core": MagicMock(),
        "core.database": MagicMock(
            Session=FakeSession,
            ChatMessage=FakeMsg,
            SessionLocal=MagicMock(return_value=fake_db),
        ),
    }), patch.object(src, "_resolve_lane", return_value=lane):
        asyncio_run(src.warmup())
        findings = asyncio_run(src.retrieve(["pasta"], question="how do I", limit=3))
    assert findings
    assert all(f.ref.source_id == "chats" for f in findings)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def asyncio_run(coro):
    """Run a coroutine in a fresh loop — works on all Python versions."""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)
    finally:
        loop.close()
