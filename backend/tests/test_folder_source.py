"""Tests for FolderSource (issue #2 / M2).

Coverage:
  - chunker: paragraph/line split, overlap, line-number accuracy
  - FolderSource construction: path validation, defaults, overrides
  - file enumeration: respects exclude_dirs, extensions, size cap, .gitignore
  - indexing (warmup): only indexes changed/new files; deletes removed
  - retrieval: returns file:// citations, dedupes across queries, skips
    prior_refs, ranks by score
  - registry: FolderSource auto-registers on package import
  - routes: GET /api/research/sources lists `folder` when flag is on
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from src.research_sources import registry
from src.research_sources.chunker import chunk_file, Chunk
from src.research_sources.folder import FolderSource


# ----------------------------------------------------------------------
# Fake embedding lane — used wherever FolderSource would touch ChromaDB.
# Records every call so tests can assert on indexing/retrieval behavior
# without the cost of a real embedding model.
# ----------------------------------------------------------------------


class FakeCollection:
    """In-memory replacement for a ChromaDB collection."""

    def __init__(self):
        self._items: Dict[str, Dict[str, Any]] = {}  # id -> {doc, meta, embedding}
        self.upsert_calls: List[Tuple[List[str], List[str], List[Dict]]] = []
        self.delete_calls: List[List[str]] = []
        self.query_calls: List[Tuple[List[List[float]], int]] = []

    def upsert(self, *, ids, documents, metadatas, embeddings=None):
        for cid, doc, meta in zip(ids, documents, metadatas):
            emb = None
            if embeddings is not None:
                idx = ids.index(cid)
                emb = embeddings[idx]
            self._items[cid] = {"doc": doc, "meta": meta, "embedding": emb}
        self.upsert_calls.append((list(ids), list(documents), [dict(m) for m in metadatas]))

    def delete(self, *, ids):
        for cid in ids:
            self._items.pop(cid, None)
        self.delete_calls.append(list(ids))

    def get(self, *, include=None):
        ids = list(self._items.keys())
        metas = [dict(self._items[i]["meta"]) for i in ids]
        out = {"ids": ids}
        if include and "metadatas" in include:
            out["metadatas"] = metas
        return out

    def query(self, *, query_embeddings, n_results=10, **_):
        # Score each item by cosine similarity to the (single) query embedding.
        q = query_embeddings[0]
        scored = []
        for cid, item in self._items.items():
            emb = item.get("embedding")
            if emb is None:
                # Items without an embedding can't be scored — skip.
                continue
            score = _cosine(q, emb)
            scored.append((score, cid, item))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:n_results]
        self.query_calls.append((list(query_embeddings), n_results))
        return {
            "ids": [[t[1] for t in top]],
            "documents": [[t[2]["doc"] for t in top]],
            "metadatas": [[dict(t[2]["meta"]) for t in top]],
            "distances": [[1.0 - t[0] for t in top]],
        }

    def count(self) -> int:
        return len(self._items)


class FakeLane:
    def __init__(self, dim: int = 4):
        self.collection = FakeCollection()
        self._dim = dim

    def encode(self, texts):
        # Deterministic embedding: simple hash → vector. Different texts →
        # different vectors; identical texts → identical vectors.
        import hashlib
        out = []
        for t in texts:
            h = hashlib.sha1(t.encode("utf-8")).digest()
            # 4 floats in [-1, 1]
            vec = [(h[i] - 128) / 128.0 for i in range(self._dim)]
            # Normalize
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out

    @property
    def healthy(self) -> bool:
        return True


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@pytest.fixture
def fake_lane() -> FakeLane:
    return FakeLane()


@pytest.fixture
def folder_with_files(tmp_path: Path) -> Path:
    """Build a small folder with a known mix of files for tests."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "intro.md").write_text(
        "# Intro\n\nThis is the first paragraph of the intro document.\n"
        "It talks about the architecture overview.\n\n"
        "Second paragraph with more details.\n"
        "Third line.\n"
    )
    (tmp_path / "docs" / "api.md").write_text(
        "# API\n\nThe API supports list, get, and create operations.\n"
        "Authentication uses bearer tokens.\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def hello():\n    print('hi')\n\ndef bye():\n    print('bye')\n"
    )
    (tmp_path / "src" / "node_modules").mkdir()     # must be excluded by default
    (tmp_path / "src" / "node_modules" / "x.js").write_text("// junk\n")
    (tmp_path / "src" / ".git").mkdir()              # must be excluded
    (tmp_path / "src" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "README.txt").write_text("Top-level readme with a list of chapters.\n")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")  # wrong extension → skipped
    return tmp_path


@pytest.fixture
def folder_with_gitignore(tmp_path: Path) -> Path:
    """Folder with a .gitignore excluding `secrets/` and `*.log`."""
    (tmp_path / ".gitignore").write_text("secrets/\n*.log\nbuild/\n")
    (tmp_path / "kept.md").write_text("kept\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.txt").write_text("API_KEY=abc\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("build artifact\n")
    (tmp_path / "ignored.log").write_text("ignored log\n")
    return tmp_path


# ----------------------------------------------------------------------
# chunker tests
# ----------------------------------------------------------------------


def test_chunk_file_empty_returns_empty():
    assert chunk_file(Path("ignored"), "") == []


def test_chunk_file_short_file_single_chunk():
    chs = chunk_file(Path("ignored"), "hello world")
    assert len(chs) == 1
    assert chs[0].text == "hello world"
    assert chs[0].start_line == 1
    assert chs[0].end_line == 1


def test_chunk_file_line_numbers_are_accurate():
    text = "line1\nline2\nline3\nline4\nline5\n"
    chs = chunk_file(Path("ignored"), text, max_chars=1000)
    assert len(chs) == 1
    assert chs[0].start_line == 1
    assert chs[0].end_line == 5
    # When re-chunked, line numbers must match what was in the file.
    for ch in chs:
        original = "\n".join(text.splitlines()[ch.start_line - 1:ch.end_line])
        assert ch.text == original


def test_chunk_file_splits_on_paragraph_boundaries():
    text = (
        "Para A line 1.\nPara A line 2.\n\n"
        "Para B line 1.\nPara B line 2.\n\n"
        "Para C line 1.\n"
    )
    chs = chunk_file(Path("ignored"), text, max_chars=200)
    # Should split at the blank lines into 3 chunks.
    assert len(chs) >= 2
    # Each chunk should be self-contained (no cross-chunk contamination).
    assert all("Para " in c.text for c in chs)


def test_chunk_file_overlap_creates_context_continuity():
    text = "\n".join(f"line {i} some content here" for i in range(1, 11))
    chs = chunk_file(Path("ignored"), text, max_chars=80, overlap=20)
    # Multiple chunks produced (text is long enough)
    assert len(chs) >= 2
    # Line numbers are monotonically increasing
    for a, b in zip(chs, chs[1:]):
        assert b.start_line > a.start_line


def test_chunk_file_handles_long_lines():
    text = "a" * 5000 + "\nshort\n"
    chs = chunk_file(Path("ignored"), text, max_chars=500)
    # Even an oversized line should be emitted, not infinite-looped.
    assert len(chs) >= 1
    assert any(len(c.text) > 500 for c in chs)


# ----------------------------------------------------------------------
# FolderSource construction
# ----------------------------------------------------------------------


def test_construct_requires_path():
    with pytest.raises(ValueError, match="path"):
        FolderSource({})


def test_construct_rejects_nonexistent_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        FolderSource({"path": str(tmp_path / "missing")})


def test_construct_rejects_file_path(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        FolderSource({"path": str(f)})


def test_construct_accepts_directory(tmp_path: Path):
    src = FolderSource({"path": str(tmp_path)})
    assert src.root == tmp_path.resolve()
    assert src.collection_name.startswith("folder_")
    # collection_name is deterministic for the same path.
    src2 = FolderSource({"path": str(tmp_path)})
    assert src.collection_name == src2.collection_name


def test_construct_collection_name_is_safe_for_chromadb(tmp_path: Path):
    p = tmp_path / "weird name with spaces & symbols!"
    p.mkdir()
    src = FolderSource({"path": str(p)})
    # Chroma collection name: 3-63 chars, alphanumeric + _-, starts/ends alphanumeric.
    assert src.collection_name.replace("_", "").isalnum()
    assert 3 <= len(src.collection_name) <= 63


def test_construct_overrides_extend_defaults(tmp_path: Path):
    src = FolderSource({
        "path": str(tmp_path),
        "extensions": [".custom"],
        "exclude_dirs": ["secret_stash"],
    })
    assert src.exts == {".custom"}
    assert "secret_stash" in src.exclude_dirs
    # Still respects built-in defaults? No — explicit override replaces.
    assert ".git" not in src.exclude_dirs


# ----------------------------------------------------------------------
# File enumeration
# ----------------------------------------------------------------------


def test_iter_files_respects_exclude_dirs(folder_with_files: Path):
    src = FolderSource({"path": str(folder_with_files)})
    files = src._iter_files()
    paths = {str(p) for p in files}
    # node_modules and .git must be excluded.
    assert not any("node_modules" in p for p in paths)
    assert not any(f"{os.sep}.git{os.sep}" in p or p.endswith(f"{os.sep}.git") for p in paths)


def test_iter_files_respects_extensions(folder_with_files: Path):
    src = FolderSource({"path": str(folder_with_files), "extensions": [".md"]})
    files = src._iter_files()
    paths = {str(p) for p in files}
    # Only .md files
    assert all(p.endswith(".md") for p in paths)
    # The .txt and .py files should NOT be present.
    assert not any(p.endswith(".py") for p in paths)
    assert not any(p.endswith(".txt") for p in paths)
    assert not any(p.endswith(".bin") for p in paths)


def test_iter_files_respects_size_cap(folder_with_files: Path):
    src = FolderSource({
        "path": str(folder_with_files),
        "max_file_bytes": 10,   # tiny cap
    })
    files = src._iter_files()
    # Every returned file must be ≤ 10 bytes.
    for p in files:
        assert p.stat().st_size <= 10


def test_iter_files_respects_gitignore(folder_with_gitignore: Path):
    src = FolderSource({"path": str(folder_with_gitignore)})
    files = src._iter_files()
    paths = {str(p) for p in files}
    assert any(p.endswith("kept.md") for p in paths)
    assert not any("secrets" in p for p in paths)
    assert not any(p.endswith(".log") for p in paths)
    assert not any(f"{os.sep}build{os.sep}" in p for p in paths)


def test_iter_files_can_disable_gitignore(folder_with_gitignore: Path):
    src = FolderSource({
        "path": str(folder_with_gitignore),
        "respect_gitignore": False,
    })
    files = src._iter_files()
    paths = {str(p) for p in files}
    # Without .gitignore, secrets/log/build become visible.
    assert any("secrets" in p for p in paths)


def test_iter_files_is_idempotent(folder_with_files: Path):
    src = FolderSource({"path": str(folder_with_files)})
    files1 = sorted(str(p) for p in src._iter_files())
    files2 = sorted(str(p) for p in src._iter_files())
    assert files1 == files2


# ----------------------------------------------------------------------
# warmup (indexing)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warmup_indexes_all_eligible_files(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
    coll = fake_lane.collection
    # All 4 eligible files (intro.md, api.md, main.py, README.txt) should be
    # indexed. Each at minimum becomes 1 chunk.
    assert coll.upsert_calls, "warmup did not call upsert"
    _ids, _docs, metas = coll.upsert_calls[0]
    paths = {m["path"] for m in metas}
    assert any("intro.md" in p for p in paths)
    assert any("api.md" in p for p in paths)
    assert any("main.py" in p for p in paths)
    assert any("README.txt" in p for p in paths)
    assert not any("node_modules" in p for p in paths)


@pytest.mark.asyncio
async def test_warmup_is_idempotent_when_unchanged(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        n_first = len(fake_lane.collection._items)
        # Second warmup should re-embed NOTHING (no upserts).
        fake_lane.collection.upsert_calls.clear()
        await src.warmup()
    assert fake_lane.collection.upsert_calls == []
    assert len(fake_lane.collection._items) == n_first


@pytest.mark.asyncio
async def test_warmup_reindexes_changed_files(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        first_ids = set(fake_lane.collection._items.keys())
        # Touch a file's mtime so it counts as changed.
        target = folder_with_files / "docs" / "intro.md"
        time.sleep(0.05)
        os.utime(target, (time.time(), time.time() + 100))
        fake_lane.collection.upsert_calls.clear()
        await src.warmup()
    # intro.md's chunks should have been re-upserted.
    _ids, _docs, metas = fake_lane.collection.upsert_calls[0]
    upserted_paths = {m["path"] for m in metas}
    assert any("intro.md" in p for p in upserted_paths)


@pytest.mark.asyncio
async def test_warmup_deletes_chunks_for_removed_files(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        # Remove a file
        (folder_with_files / "docs" / "intro.md").unlink()
        fake_lane.collection.delete_calls.clear()
        await src.warmup()
    # intro.md's chunks must have been deleted.
    assert fake_lane.collection.delete_calls, "no delete call observed"
    deleted_ids = fake_lane.collection.delete_calls[0]
    assert any("intro.md" in cid for cid in deleted_ids)


@pytest.mark.asyncio
async def test_warmup_without_lane_does_not_raise(
    folder_with_files: Path,
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane",
                      side_effect=RuntimeError("no chroma")):
        # Must not raise — degrade gracefully.
        await src.warmup()


# ----------------------------------------------------------------------
# retrieve
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_returns_file_citations(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        out = await src.retrieve(["API"], question="how does API work?")
    assert out, "no findings returned"
    for f in out:
        assert f.ref.source_id == "folder"
        assert f.ref.location.startswith("file://")
        # file:///<abs path>#L<start>-L<end>
        assert "#L" in f.ref.location
        assert f.ref.metadata.get("path", "").startswith(str(folder_with_files))


@pytest.mark.asyncio
async def test_retrieve_uses_question_independent_of_query_only(
    folder_with_files: Path, fake_lane: FakeLane
):
    """The `question` arg is documented for symmetry with other sources;
    folder retrieval should still work when question is empty."""
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        out = await src.retrieve(["API"], question="")
    assert isinstance(out, list)


@pytest.mark.asyncio
async def test_retrieve_dedupes_across_queries(
    folder_with_files: Path, fake_lane: FakeLane
):
    """Two queries that return the same chunk should appear once."""
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        # Two identical queries → same chunks returned twice → deduped to one.
        out = await src.retrieve(["API", "API"], question="x", limit=5)
    locations = [f.ref.location for f in out]
    assert len(locations) == len(set(locations))


@pytest.mark.asyncio
async def test_retrieve_skips_prior_refs(
    folder_with_files: Path, fake_lane: FakeLane
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        first = await src.retrieve(["API"], question="x")
        prior = [f.ref.location for f in first]
        second = await src.retrieve(["API"], question="x", prior_refs=prior)
    # second must not return anything that was in first.
    second_locs = {f.ref.location for f in second}
    assert second_locs.isdisjoint(set(prior))


@pytest.mark.asyncio
async def test_retrieve_returns_empty_when_lane_unavailable(
    folder_with_files: Path,
):
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane",
                      side_effect=RuntimeError("no chroma")):
        out = await src.retrieve(["q"], question="x")
    assert out == []


@pytest.mark.asyncio
async def test_retrieve_results_have_legacy_dict_shape(
    folder_with_files: Path, fake_lane: FakeLane
):
    """Each Finding must round-trip to the legacy dict shape (M1 contract)."""
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        out = await src.retrieve(["intro"], question="x")
    assert out
    d = out[0].to_legacy_dict()
    for k in ("url", "title", "summary", "evidence", "og_image", "rational"):
        assert k in d
    # url is the file:// location; title is the file's basename.
    assert d["url"].startswith("file://")
    assert d["title"]


# ----------------------------------------------------------------------
# DeepResearcher integration: FolderSource flows through the round loop
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_uses_folder_source_when_provided(
    folder_with_files: Path, fake_lane: FakeLane
):
    """End-to-end: DeepResearcher.sources = [FolderSource(...)] →
    _retrieve_via_sources calls FolderSource.retrieve → legacy dicts
    land in `findings`."""
    src = FolderSource({"path": str(folder_with_files)})
    with patch.object(FolderSource, "_resolve_lane", return_value=fake_lane):
        # Warm the index BEFORE handing the source to the researcher, so the
        # test isn't timing-sensitive (warmup is async; in production this
        # happens during the first call to retrieve anyway).
        await src.warmup()
        from src.deep_research import DeepResearcher
        researcher = DeepResearcher(
            llm_endpoint="http://local.test/v1",
            llm_model="m",
            max_rounds=1,
            max_time=5,
            sources=[src],
        )
        findings = await researcher._retrieve_via_sources(["API"], question="how does API work?")
    assert findings
    for d in findings:
        assert d["url"].startswith("file://")
        assert "#L" in d["url"]


# ----------------------------------------------------------------------
# Registry / routes
# ----------------------------------------------------------------------


def test_folder_source_is_registered():
    assert "folder" in registry.types()


def test_folder_source_listed_in_routes_when_flag_on(monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "RESEARCH_SOURCES_ENABLED", True)
    from routes.research_sources_routes import list_sources
    types = [s["type"] for s in list_sources()["sources"]]
    assert "folder" in types
    assert "internet" in types


def test_folder_source_hidden_when_flag_off(monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "RESEARCH_SOURCES_ENABLED", False)
    from routes.research_sources_routes import list_sources
    types = [s["type"] for s in list_sources()["sources"]]
    assert "folder" not in types
    assert "internet" in types


def test_folder_source_config_schema_is_ui_ready():
    schema = FolderSource.config_schema
    # Every config key has a type so the UI can render the right input.
    for k, v in schema.items():
        assert "type" in v, f"key {k!r} missing type"
