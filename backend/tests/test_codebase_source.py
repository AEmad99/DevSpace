"""Tests for CodebaseSource and language-aware chunkers (issue #2 / M3).

Coverage:
  - RegexCodeChunker: Python, JS, TS, Go, Rust function/class detection
  - RegexCodeChunker: line numbers accurate, no infinite loops on malformed code
  - TreeSitterCodeChunker: falls back gracefully when tree-sitter absent
  - CodebaseSource construction: code defaults, overrides, validation
  - CodebaseSource.warmup: uses code chunker (whole-function chunks)
  - CodebaseSource.retrieve: file:// citations with range markers (Lstart-Lend)
  - CodebaseSource inherits FolderSource behavior (excludes, gitignore,
    size cap, incremental indexing)
  - Registry: auto-registered, listed in routes when flag is on
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.research_sources import registry
from src.research_sources.chunker import Chunk
from src.research_sources.code_chunker import (
    CodeChunker,
    RegexCodeChunker,
    TreeSitterCodeChunker,
)
from src.research_sources.codebase import CodebaseSource
from src.research_sources.folder import FolderSource


# ----------------------------------------------------------------------
# Fake embedding lane (mirrors test_folder_source.py)
# ----------------------------------------------------------------------


class FakeCollection:
    def __init__(self):
        self._items: Dict[str, Dict[str, Any]] = {}

    def upsert(self, *, ids, documents, metadatas, embeddings=None):
        for i, cid in enumerate(ids):
            emb = embeddings[i] if embeddings is not None else None
            self._items[cid] = {"doc": documents[i], "meta": dict(metadatas[i]), "emb": emb}

    def delete(self, *, ids):
        for cid in ids:
            self._items.pop(cid, None)

    def get(self, *, include=None):
        ids = list(self._items.keys())
        metas = [dict(self._items[i]["meta"]) for i in ids]
        return {"ids": ids, "metadatas": metas}

    def query(self, *, query_embeddings, n_results=10, **_):
        q = query_embeddings[0]
        scored = []
        for cid, item in self._items.items():
            if item["emb"] is None:
                continue
            score = _cosine(q, item["emb"])
            scored.append((score, cid, item))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:n_results]
        return {
            "ids": [[t[1] for t in top]],
            "documents": [[t[2]["doc"] for t in top]],
            "metadatas": [[dict(t[2]["meta"]) for t in top]],
            "distances": [[1.0 - t[0] for t in top]],
        }


class FakeLane:
    def __init__(self):
        self.collection = FakeCollection()

    def encode(self, texts):
        import hashlib
        out = []
        for t in texts:
            h = hashlib.sha1(t.encode("utf-8")).digest()
            vec = [(h[i] - 128) / 128.0 for i in range(4)]
            n = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / n for x in vec])
        return out

    @property
    def healthy(self):
        return True


def _cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@pytest.fixture
def fake_lane():
    return FakeLane()


# ----------------------------------------------------------------------
# Sample codebases for tests
# ----------------------------------------------------------------------


PYTHON_SAMPLE = '''\
"""Module docstring."""
import os


def helper(x):
    """Return x + 1."""
    return x + 1


class Calculator:
    """A simple calculator."""

    def __init__(self, base=0):
        self.base = base

    def add(self, n):
        return self.base + n

    def multiply(self, n):
        return self.base * n


def main():
    c = Calculator(10)
    return c.multiply(3)
'''


JS_SAMPLE = '''\
// Entry point.
const express = require("express");

function greet(name) {
    return `Hello, ${name}!`;
}

class UserService {
    constructor(db) {
        this.db = db;
    }

    async findById(id) {
        return this.db.users.find({ id });
    }
}

const app = express();
app.get("/", (req, res) => res.send(greet("world")));
'''


GO_SAMPLE = '''\
package main

import "fmt"

type Point struct {
    X, Y int
}

func (p Point) String() string {
    return fmt.Sprintf("(%d,%d)", p.X, p.Y)
}

func add(a, b int) int {
    return a + b
}

func main() {
    fmt.Println(add(2, 3))
}
'''


@pytest.fixture
def python_codebase(tmp_path: Path) -> Path:
    f = tmp_path / "calc.py"
    f.write_text(PYTHON_SAMPLE)
    return tmp_path


@pytest.fixture
def polyglot_codebase(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(PYTHON_SAMPLE)
    (tmp_path / "server.js").write_text(JS_SAMPLE)
    (tmp_path / "main.go").write_text(GO_SAMPLE)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("# junk\n")
    return tmp_path


# ----------------------------------------------------------------------
# RegexCodeChunker tests
# ----------------------------------------------------------------------


def test_regex_chunker_python_finds_functions_and_classes():
    p = Path("calc.py")
    chs = RegexCodeChunker().chunk(p, PYTHON_SAMPLE)
    # Expect: header, helper, class Calculator, __init__, add, multiply, main
    assert len(chs) >= 4, f"got {len(chs)} chunks: {[c.text[:30] for c in chs]}"
    # At minimum: the file's three top-level functions must each be in some chunk.
    text_blob = "\n".join(c.text for c in chs)
    assert "def helper" in text_blob
    assert "class Calculator" in text_blob
    assert "def main" in text_blob


def test_regex_chunker_python_method_chunks_have_whole_methods():
    """Class methods should be in their own chunk with the full body."""
    p = Path("calc.py")
    chs = RegexCodeChunker().chunk(p, PYTHON_SAMPLE)
    init_chunks = [c for c in chs if "def __init__" in c.text]
    add_chunks = [c for c in chs if "def add" in c.text and "multiply" not in c.text]
    assert init_chunks, "__init__ should be its own chunk"
    assert add_chunks, "add should be its own chunk separate from multiply"


def test_regex_chunker_javascript_finds_classes_and_functions():
    chs = RegexCodeChunker().chunk(Path("server.js"), JS_SAMPLE)
    text_blob = "\n".join(c.text for c in chs)
    assert "function greet" in text_blob
    assert "class UserService" in text_blob


def test_regex_chunker_go_finds_funcs_and_structs():
    chs = RegexCodeChunker().chunk(Path("main.go"), GO_SAMPLE)
    text_blob = "\n".join(c.text for c in chs)
    assert "type Point" in text_blob
    assert "func (p Point)" in text_blob or "func (p" in text_blob
    assert "func add" in text_blob
    assert "func main" in text_blob


def test_regex_chunker_unknown_extension_falls_back_to_prose():
    """A .xyz file should use the prose chunker."""
    chs = RegexCodeChunker().chunk(Path("readme.xyz"), "Para 1.\n\nPara 2.\n\nPara 3.\n")
    assert len(chs) >= 1
    assert all(isinstance(c, Chunk) for c in chs)


def test_regex_chunker_line_numbers_accurate():
    chs = RegexCodeChunker().chunk(Path("calc.py"), PYTHON_SAMPLE)
    lines = PYTHON_SAMPLE.splitlines()
    for c in chs:
        # Each chunk's text must equal exactly those source lines.
        original = "\n".join(lines[c.start_line - 1:c.end_line]).rstrip()
        assert c.text == original, (
            f"chunk L{c.start_line}-L{c.end_line} doesn't match source"
        )


def test_regex_chunker_handles_empty_file():
    assert RegexCodeChunker().chunk(Path("empty.py"), "") == []
    assert RegexCodeChunker().chunk(Path("ws.py"), "   \n\n   \n") == []


def test_regex_chunker_handles_malformed_code_without_crashing():
    """Bizarre inputs must not raise — emit reasonable chunks."""
    bad = "def (((\nclass @@@\n{ not valid"
    chs = RegexCodeChunker().chunk(Path("bad.py"), bad)
    assert isinstance(chs, list)


def test_regex_chunker_handles_single_function_file():
    """If the whole file is one function, return it as one chunk."""
    one = "def only():\n    return 42\n"
    chs = RegexCodeChunker().chunk(Path("one.py"), one)
    assert len(chs) == 1
    assert "def only" in chs[0].text
    assert chs[0].start_line == 1
    assert chs[0].end_line == 2


def test_regex_chunker_preserves_decorators():
    """Decorators should be part of the next function's chunk."""
    code = (
        "@property\n"
        "def x(self):\n"
        "    return 1\n"
        "\n"
        "@staticmethod\n"
        "def y():\n"
        "    return 2\n"
    )
    chs = RegexCodeChunker().chunk(Path("d.py"), code)
    text_blob = "\n".join(c.text for c in chs)
    # Both decorators must appear somewhere in the chunks.
    assert "@property" in text_blob
    assert "@staticmethod" in text_blob
    # Decorator should be in the same chunk as its function.
    for c in chs:
        if "def x" in c.text:
            assert "@property" in c.text
        if "def y" in c.text:
            assert "@staticmethod" in c.text


# ----------------------------------------------------------------------
# TreeSitterCodeChunker tests
# ----------------------------------------------------------------------


def test_tree_sitter_chunker_handles_missing_dependency(caplog):
    """When tree_sitter isn't installed, init must NOT raise; _available_langs is empty."""
    import logging
    with caplog.at_level(logging.WARNING):
        ts = TreeSitterCodeChunker()
    assert ts._available_langs == set()
    # Fallback path works for any file.
    chs = ts.chunk(Path("calc.py"), PYTHON_SAMPLE)
    assert len(chs) >= 1


def test_tree_sitter_chunker_falls_back_to_regex_for_unknown_ext():
    ts = TreeSitterCodeChunker()
    chs = ts.chunk(Path("readme.xyz"), "Paragraph 1.\n\nParagraph 2.\n")
    assert len(chs) >= 1


# ----------------------------------------------------------------------
# CodebaseSource construction
# ----------------------------------------------------------------------


def test_codebase_requires_path(tmp_path: Path):
    with pytest.raises(ValueError, match="path"):
        CodebaseSource({})


def test_codebase_uses_code_focused_defaults(tmp_path: Path):
    src = CodebaseSource({"path": str(tmp_path)})
    # Code extensions
    assert ".py" in src.exts
    assert ".go" in src.exts
    assert ".rs" in src.exts
    # Code excludes
    assert "node_modules" in src.exclude_dirs
    assert "vendor" in src.exclude_dirs
    # Prose-only files are NOT in code defaults
    assert ".csv" not in src.exts
    assert ".tex" not in src.exts


def test_codebase_chunker_default_is_regex(tmp_path: Path):
    src = CodebaseSource({"path": str(tmp_path)})
    assert isinstance(src._chunker, RegexCodeChunker)


def test_codebase_chunker_with_use_tree_sitter_falls_back_when_missing(tmp_path: Path):
    """If tree_sitter isn't installed, use_tree_sitter=True must still work (via regex)."""
    src = CodebaseSource({"path": str(tmp_path), "use_tree_sitter": True})
    # Should NOT raise; falls back to regex.
    assert isinstance(src._chunker, RegexCodeChunker)


def test_codebase_inherits_folder_path_validation(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        CodebaseSource({"path": str(tmp_path / "missing")})


def test_codebase_collection_name_starts_with_folder_for_chromadb_compat(tmp_path: Path):
    """Codebase collections still use the same `folder_*` prefix so they
    share ChromaDB's collection-name character set and lookups work."""
    src = CodebaseSource({"path": str(tmp_path)})
    assert src.collection_name.startswith("folder_")


def test_codebase_can_override_extensions_and_excludes(tmp_path: Path):
    src = CodebaseSource({
        "path": str(tmp_path),
        "extensions": [".py"],
        "exclude_dirs": ["secret_stash"],
    })
    assert src.exts == {".py"}
    # When overridden, code defaults are NOT also applied.
    assert "node_modules" not in src.exclude_dirs


# ----------------------------------------------------------------------
# CodebaseSource warmup (uses code chunker, not prose chunker)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warmup_indexes_whole_functions_as_chunks(python_codebase: Path, fake_lane):
    """The most important behavior: a multi-function file should produce
    one chunk per function (NOT mid-statement cuts)."""
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
    items = fake_lane.collection._items
    assert items
    # Find the chunk that contains the helper function — its Lstart should
    # point at the `def helper` line, not some line inside it.
    helper_chunk = next(
        (item for item in items.values() if "def helper" in item["doc"]),
        None,
    )
    assert helper_chunk is not None
    assert helper_chunk["meta"]["path"].endswith("calc.py")
    assert helper_chunk["meta"]["start_line"] == PYTHON_SAMPLE.splitlines().index("def helper(x):") + 1


@pytest.mark.asyncio
async def test_warmup_stamps_language_metadata(polyglot_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(polyglot_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
    items = fake_lane.collection._items
    languages = {item["meta"].get("language") for item in items.values()}
    assert "python" in languages
    assert "javascript" in languages
    assert "go" in languages


@pytest.mark.asyncio
async def test_warmup_excludes_node_modules(polyglot_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(polyglot_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
    paths = {item["meta"]["path"] for item in fake_lane.collection._items.values()}
    assert not any("node_modules" in p for p in paths)


@pytest.mark.asyncio
async def test_warmup_is_incremental(python_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        n_first = len(fake_lane.collection._items)
        # No changes → no re-index.
        await src.warmup()
    assert len(fake_lane.collection._items) == n_first


@pytest.mark.asyncio
async def test_warmup_reindexes_changed_file(python_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        target = python_codebase / "calc.py"
        time.sleep(0.05)
        os.utime(target, (time.time(), time.time() + 100))
        await src.warmup()
    items = fake_lane.collection._items
    # After re-warmup, calc.py should still be indexed (its chunks replaced).
    assert any("calc.py" in item["meta"]["path"] for item in items.values())


# ----------------------------------------------------------------------
# CodebaseSource retrieve
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_returns_file_citations_with_range(python_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        out = await src.retrieve(["class Calculator"], question="how does Calculator work?")
    assert out
    for f in out:
        # Range citation format: file:///abs/path#L<start>-L<end>
        assert f.ref.location.startswith("file://")
        assert "#L" in f.ref.location
        assert f.ref.source_id == "codebase"
        # Title is the file's basename.
        assert f.ref.title == "calc.py"


@pytest.mark.asyncio
async def test_retrieve_dedupes_across_queries(python_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        out = await src.retrieve(["foo", "foo"], question="x", limit=5)
    locs = [f.ref.location for f in out]
    assert len(locs) == len(set(locs))


@pytest.mark.asyncio
async def test_retrieve_skips_prior_refs(python_codebase: Path, fake_lane):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        first = await src.retrieve(["foo"], question="x")
        prior = [f.ref.location for f in first]
        second = await src.retrieve(["foo"], question="x", prior_refs=prior)
    assert {f.ref.location for f in second}.isdisjoint(set(prior))


# ----------------------------------------------------------------------
# CodebaseSource works end-to-end through DeepResearcher
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_routes_codebase_findings_to_legacy_dicts(
    python_codebase: Path, fake_lane
):
    src = CodebaseSource({"path": str(python_codebase)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        await src.warmup()
        from src.deep_research import DeepResearcher
        r = DeepResearcher(
            llm_endpoint="http://local.test/v1",
            llm_model="m",
            max_rounds=1,
            max_time=5,
            sources=[src],
        )
        findings = await r._retrieve_via_sources(["Calculator"], question="explain class")
    assert findings
    for d in findings:
        # Round-trips through the legacy dict shape.
        assert d["url"].startswith("file://")
        assert "#L" in d["url"]
        # title + summary + evidence + og_image + rational all present.
        for k in ("url", "title", "summary", "evidence", "og_image", "rational"):
            assert k in d


# ----------------------------------------------------------------------
# Registry / routes
# ----------------------------------------------------------------------


def test_codebase_is_registered():
    assert "codebase" in registry.types()


def test_codebase_inherits_folder_type_id_naming():
    """Sanity: CodebaseSource's collection name must follow the same rules
    as FolderSource (same prefix) so the underlying Chroma collection can
    in principle be migrated between the two."""
    f1 = FolderSource.type_id
    f2 = CodebaseSource.type_id
    assert f1 != f2
    # Both must be short, alphanumeric-ish for Chroma.
    assert f1.replace("_", "").isalnum()
    assert f2.replace("_", "").isalnum()


def test_codebase_listed_in_routes_when_flag_on(monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "RESEARCH_SOURCES_ENABLED", True)
    from routes.research_sources_routes import list_sources
    types = [s["type"] for s in list_sources()["sources"]]
    assert "codebase" in types
    assert "folder" in types
    assert "internet" in types


def test_codebase_hidden_when_flag_off(monkeypatch):
    from src import constants
    monkeypatch.setattr(constants, "RESEARCH_SOURCES_ENABLED", False)
    from routes.research_sources_routes import list_sources
    types = [s["type"] for s in list_sources()["sources"]]
    assert "codebase" not in types


def test_codebase_config_schema_is_ui_ready():
    schema = CodebaseSource.config_schema
    assert "use_tree_sitter" in schema
    assert schema["use_tree_sitter"]["type"] == "boolean"
    # Should inherit FolderSource keys too.
    assert "path" in schema
    assert "extensions" in schema
    assert "exclude_dirs" in schema


# ----------------------------------------------------------------------
# Perf guardrails
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_codebase_indexing_completes_quickly(tmp_path: Path, fake_lane):
    """Smoke test that a moderately sized repo can be indexed in seconds
    with the fake (instant) encoder. The real FastEmbed lane will be slower,
    but this catches O(N²) regressions in our chunker / enumeration."""
    # Create 50 Python files, each ~100 lines.
    for i in range(50):
        (tmp_path / f"mod_{i:02d}.py").write_text(
            f"# Module {i}\n"
            + "\n".join(
                f"def func_{i}_{j}(x):\n    return x + {j}\n\n" for j in range(5)
            )
        )
    src = CodebaseSource({"path": str(tmp_path)})
    with patch.object(CodebaseSource, "_resolve_lane", return_value=fake_lane):
        t0 = time.time()
        await src.warmup()
        elapsed = time.time() - t0
    assert elapsed < 10.0, f"indexing 50 files took {elapsed:.2f}s — too slow"
    # 50 files × (1 header + 5 funcs) = ≥ 300 chunks
    assert len(fake_lane.collection._items) >= 300
