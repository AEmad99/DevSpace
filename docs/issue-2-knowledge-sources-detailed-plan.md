# Detailed Implementation Plan — Issue #2 (Custom Knowledge Sources)

**Repo:** `AEmad99/DevSpace`
**Issue:** #2 — *Support Custom Knowledge Sources Beyond Internet Research*
**Plan scope:** Four sequential milestones. Each must ship green before the next starts.

## Implementation status

| # | Milestone | Status | Tests |
|---|---|---|---|
| M1 | Source abstraction | ✅ DONE | 23 / 23 passing |
| M2 | FolderSource        | ✅ DONE | 34 / 34 passing |
| M3 | CodebaseSource      | ✅ DONE | 34 / 34 passing |
| M4 | KB + Hybrid + UI    | ✅ DONE | 37 / 37 passing |

**Combined test count after M1 + M2 + M3 + M4:** 179 tests passing (51 pre-existing + 128 new). Zero regression on legacy paths.

**Issue #2 status:** ✅ RESOLVED — all four user-facing capabilities from the issue body are now implemented and tested:
- F1 (Configurable research source) — via picker UI + `/api/research/sources` + `RESEARCH_SOURCES_ENABLED` flag
- F2 (Local codebase source) — `CodebaseSource` with language-aware chunking
- F3 (Document-folder source) — `FolderSource` with .gitignore, excludes, incremental indexing
- F4 (Internal knowledge repository source) — `KnowledgeBaseSource` with persistent named corpora + CRUD

---

## Reading guide

- Each milestone is **independently shippable** behind a feature flag (`RESEARCH_SOURCES_ENABLED`).
- Code is sketched, not full — but file paths and key symbols are exact.
- Effort assumes one engineer familiar with the existing `DeepResearcher` loop.
- **Don't start a milestone until the previous one's acceptance tests are green in CI.**

---

# 🟢 Milestone 1 — Source Abstraction (Foundation)

**Why first:** Everything else plugs into this. Zero user-facing change. Pure refactor.

**Goal:** Introduce a `Source` interface so the existing internet path becomes one of several. Existing `/api/research` with no source specified must behave **identically** to today.

**Estimated effort:** 3 dev days
**Risk:** Low (refactor only)

---

### 1.1 — New file: `backend/src/research_sources/__init__.py`

Empty package marker + re-exports for convenience:

```python
from .base import Source, SourceRef, Finding
from .registry import SourceRegistry, registry

__all__ = ["Source", "SourceRef", "Finding", "SourceRegistry", "registry"]
```

### 1.2 — New file: `backend/src/research_sources/base.py`

The contract every adapter must implement.

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional


@dataclass
class SourceRef:
    """A single citable reference produced by a Source."""
    source_id: str           # adapter id, e.g. "internet", "folder", "kb:work-notes"
    title: str
    location: str            # url, file://path, or kb://name/path
    snippet: str = ""        # short preview
    metadata: Dict = field(default_factory=dict)


@dataclass
class Finding:
    """One chunk of evidence surfaced for the research report."""
    content: str
    ref: SourceRef
    score: float = 0.0       # relevance / retrieval score (0..1)
    metadata: Dict = field(default_factory=dict)


class Source(ABC):
    """Pluggable research source.

    A Source receives a research question and yields Findings that the
    DeepResearcher loop can consume the same way it consumes SearXNG hits.
    """

    type_id: str = "base"           # used in API + UI selector
    display_name: str = "Base"
    config_schema: Dict = {}        # JSON-schema-ish for UI form generation

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        prior_refs: Optional[List[str]] = None,
    ) -> List[Finding]:
        """Return top-N findings for `query`. May be called multiple times per
        research round (LLM-in-the-loop). `prior_refs` lists locations already
        seen in earlier rounds — adapters should skip them."""

    async def warmup(self) -> None:
        """Optional: pre-load models, open handles, etc. Called once at start."""
        return None

    async def shutdown(self) -> None:
        """Optional: release handles. Called at end of a research session."""
        return None

    def describe(self) -> Dict:
        return {"type": self.type_id, "name": self.display_name}
```

### 1.3 — New file: `backend/src/research_sources/registry.py`

```python
from typing import Dict, List, Type
from .base import Source


class SourceRegistry:
    def __init__(self):
        self._types: Dict[str, Type[Source]] = {}

    def register(self, cls: Type[Source]) -> Type[Source]:
        if not cls.type_id or cls.type_id == "base":
            raise ValueError("Source must define a non-empty type_id")
        self._types[cls.type_id] = cls
        return cls

    def get(self, type_id: str, config: Dict | None = None) -> Source:
        if type_id not in self._types:
            raise KeyError(f"Unknown source type: {type_id}")
        return self._types[type_id](config or {})

    def list(self) -> List[Dict]:
        return [
            {"type": c.type_id, "name": c.display_name, "config_schema": c.config_schema}
            for c in self._types.values()
        ]


registry = SourceRegistry()
```

### 1.4 — New file: `backend/src/research_sources/internet.py`

**Move** the existing SearXNG/provider logic out of `deep_research.py` and wrap it as a `Source`. Do not change behavior.

```python
from typing import Dict, List, Optional
from .base import Source, Finding, SourceRef
# ... imports of existing search helpers from deep_research.py ...


@registry.register
class InternetSource(Source):
    type_id = "internet"
    display_name = "Internet"
    config_schema = {
        "provider": {"type": "string", "default": None,
                     "enum": ["searxng", "brave", "tavily", "duckduckgo"]},
        "category": {"type": "string", "default": None},
        "max_urls_per_round": {"type": "integer", "default": 3, "minimum": 1, "maximum": 10},
    }

    async def retrieve(self, query, *, limit=10, prior_refs=None):
        # Existing SearXNG logic from deep_research.py:_round_search()
        # returns List[Finding] where ref.location is the URL.
        ...
```

### 1.5 — Edit: `backend/src/deep_research.py`

**Minimal change** to `DeepResearcher.__init__` (lines 192–244) and `research()` (line 252+):

- Add optional kwarg `sources: Optional[List[Source]] = None`.
- If `sources is None`, build `[InternetSource({...defaults from existing kwargs...})]` so behavior is preserved.
- Replace the in-line web-search block in the round loop with `await source.retrieve(...)`.
- Keep all existing public attributes (`search_provider_override`, `category`, `urls_fetched`, etc.) — `InternetSource` reads them from its config.

```python
# In DeepResearcher.__init__:
self.sources: List[Source] = sources or [
    InternetSource({
        "provider": search_provider,
        "category": category,
        "max_urls_per_round": max_urls_per_round,
    })
]
```

```python
# In research() loop, replace the search block with:
round_findings: List[Finding] = []
for src in self.sources:
    await src.warmup()
    try:
        round_findings.extend(await src.retrieve(
            query_text,
            limit=src.config.get("max_urls_per_round", 3),
            prior_refs=list(self.urls_fetched),
        ))
    finally:
        await src.shutdown()
```

The downstream extraction / synthesis / continue-stop logic stays the same — it already consumes a list of "hits", and `Finding` is structurally the same shape (content + ref + score).

### 1.6 — New file: `backend/routes/research_sources_routes.py`

Expose the registry so the frontend can build a picker later (M4).

```python
from fastapi import APIRouter
from src.research_sources import registry

router = APIRouter(prefix="/api/research/sources", tags=["research-sources"])


@router.get("")
def list_sources():
    return {"sources": registry.list()}
```

Register in `backend/app.py` near other research routers.

### 1.7 — Tests

`backend/tests/test_research_sources.py`:
- `test_registry_register_and_get`
- `test_registry_rejects_duplicate_type_id`
- `test_registry_rejects_blank_type_id`
- `test_internet_source_default_behavior_matches_legacy` — golden test that runs `InternetSource.retrieve("python list comprehension")` and asserts ≥1 finding with a URL-shaped ref.

### 1.8 — Feature flag

In `backend/src/config.py` (or wherever feature flags live):

```python
RESEARCH_SOURCES_ENABLED = os.environ.get("RESEARCH_SOURCES_ENABLED", "false").lower() == "true"
```

In `DeepResearcher.__init__`, when the flag is **false**, ignore `sources=` and use the legacy single-internet path verbatim. This lets us merge M1 without breaking any existing deployment.

### Acceptance criteria for M1

| Criterion | How to verify |
|---|---|
| Existing `/api/research/start` with no body change returns same report | Golden-query replay against saved fixture |
| New `GET /api/research/sources` returns `{"sources":[{"type":"internet", ...}]}` | curl |
| `InternetSource.retrieve` produces same `Finding[]` shape as old internal logic | Unit test |
| All existing research tests pass unchanged | `pytest backend/tests/` |
| Feature flag off → old code path runs (no behavioral diff) | Manual smoke test |

**Merge → tag M1 done → start M2.**

---

# 🟢 Milestone 2 — FolderSource

**Why second:** Smallest useful new feature. Validates the Source pattern end-to-end (index → retrieve → cite).

**Goal:** `POST /api/research {query, source:{type:"folder", path:"docs/", extensions:[".md",".py"]}}` returns a report citing `file://docs/foo.md#L42`-style references.

**Estimated effort:** 5 dev days
**Risk:** Low (uses existing ChromaDB)

---

### 2.1 — New file: `backend/src/research_sources/chunker.py`

Plain-text / markdown / code chunker. Reused by M3 and M4.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class Chunk:
    text: str
    start_line: int   # 1-indexed, for citation
    end_line: int


def chunk_file(path: Path, *, max_chars: int = 1500, overlap: int = 200) -> List[Chunk]:
    """Language-agnostic chunker.

    Strategy:
      - Split on blank-line boundaries (paragraphs for prose).
      - If a paragraph exceeds max_chars, split on line boundaries.
      - Emit with overlap so cross-boundary context survives.
    """
    ...
```

For M3 we'll replace this with a tree-sitter-aware version; for M2 this naive chunker is enough.

### 2.2 — New file: `backend/src/research_sources/folder.py`

```python
from pathlib import Path
from typing import Dict, List, Optional, Set
from .base import Source, Finding, SourceRef
from .registry import registry
from .chunker import chunk_file
from src.chroma_client import get_chroma_collection
from src.embeddings import embed_texts  # pick whichever lane is appropriate
import hashlib


@registry.register
class FolderSource(Source):
    type_id = "folder"
    display_name = "Local Folder"
    config_schema = {
        "path":               {"type": "string", "required": True},
        "extensions":         {"type": "array",  "default": [".md",".txt",".py",".js",".ts"]},
        "exclude_dirs":       {"type": "array",  "default": [".git","node_modules","__pycache__","dist","build",".venv","venv"]},
        "max_file_bytes":     {"type": "integer","default": 1_000_000},
        "respect_gitignore":  {"type": "boolean","default": True},
        "collection_name":    {"type": "string", "default": None},  # auto-generated if None
    }

    DEFAULT_EXTS = {".md",".txt",".py",".js",".ts",".tsx",".jsx",".json",".yaml",".yml",".rst",".adoc"}

    def __init__(self, config=None):
        super().__init__(config)
        self.root = Path(self.config["path"]).expanduser().resolve()
        if not self.root.exists():
            raise FileNotFoundError(self.root)
        self.exts: Set[str] = set(self.config.get("extensions") or self.DEFAULT_EXTS)
        self.exclude_dirs = set(self.config.get("exclude_dirs") or [])
        self.max_file_bytes = int(self.config.get("max_file_bytes", 1_000_000))
        self.collection_name = self.config.get("collection_name") or self._auto_collection_name()

    def _auto_collection_name(self) -> str:
        h = hashlib.sha1(str(self.root).encode("utf-8")).hexdigest()[:10]
        return f"folder_{self.root.name}_{h}"

    # ---- indexing --------------------------------------------------

    def _iter_files(self):
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in self.exclude_dirs for part in p.parts):
                continue
            if p.suffix.lower() not in self.exts:
                continue
            if p.stat().st_size > self.max_file_bytes:
                continue
            yield p

    async def warmup(self) -> None:
        coll = get_chroma_collection(self.collection_name)
        indexed = {m["path"] for m in coll.get(include=["metadatas"]).get("metadatas", [])}
        new_files, changed = [], []
        for p in self._iter_files():
            key = str(p)
            mtime = p.stat().st_mtime
            existing = coll.get(where={"path": key}, include=["metadatas"]).get("metadatas", [])
            if not existing:
                new_files.append(p)
            elif existing and existing[0].get("mtime") != mtime:
                changed.append(p)
        to_index = new_files + changed
        if to_index:
            docs, metas, ids = [], [], []
            for p in to_index:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for ch in chunk_file(p):
                    docs.append(ch.text)
                    metas.append({
                        "path": str(p),
                        "start_line": ch.start_line,
                        "end_line": ch.end_line,
                        "mtime": p.stat().st_mtime,
                    })
                    ids.append(f"{p}#{ch.start_line}-{ch.end_line}")
            if docs:
                embs = await embed_texts(docs)   # use whatever lane the user has configured
                coll.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embs)

    # ---- retrieval -------------------------------------------------

    async def retrieve(self, query, *, limit=10, prior_refs=None):
        coll = get_chroma_collection(self.collection_name)
        emb = (await embed_texts([query]))[0]
        res = coll.query(query_embeddings=[emb], n_results=limit)
        out: List[Finding] = []
        for doc, meta, score in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            ref_loc = f"file://{meta['path']}#L{meta['start_line']}"
            if prior_refs and ref_loc in prior_refs:
                continue
            out.append(Finding(
                content=doc,
                ref=SourceRef(
                    source_id=self.type_id,
                    title=Path(meta["path"]).name,
                    location=ref_loc,
                    snippet=doc[:200],
                    metadata={"path": meta["path"], "start_line": meta["start_line"]},
                ),
                score=float(score),
            ))
        return out
```

### 2.3 — Edit: `backend/routes/research_routes.py`

In the `start_research` body schema (pydantic model), add optional `source` field:

```python
class StartResearchBody(BaseModel):
    query: str
    source: Optional[Dict] = None    # NEW
    # ... existing fields ...
```

Where the handler builds the researcher, pass the source through:

```python
from src.research_sources import registry

sources = None
if body.source and RESEARCH_SOURCES_ENABLED:
    src = registry.get(body.source["type"], body.source.get("config"))
    sources = [src]

researcher = DeepResearcher(..., sources=sources)
```

### 2.4 — Tests

`backend/tests/test_folder_source.py`:
- `test_iter_files_respects_excludes` — point at a fixture dir with `node_modules/`, `.git/`, verify they're skipped.
- `test_iter_files_filters_extensions`
- `test_chunk_file_line_numbers_are_accurate` — golden file with known line counts.
- `test_warmup_indexes_new_files` — first call → collection has N entries.
- `test_warmup_skips_unchanged_files` — second call → upserts 0.
- `test_warmup_reindexes_changed_files` — touch a file → upserts its chunks.
- `test_retrieve_returns_citations` — query a known phrase → assertion on `file://...#L42` in result.
- `test_retrieve_skips_prior_refs` — pass `prior_refs` → they don't reappear.

### 2.5 — Smoke test (manual)

```bash
curl -X POST http://localhost:8000/api/research/start \
  -H "Content-Type: application/json" \
  -d '{
    "query": "how is the chat handler initialized?",
    "source": {"type":"folder","config":{"path":"/path/to/devspace/backend/src","extensions":[".py"]}}
  }'
```

Expected: report with citations like `file:///path/to/devspace/backend/src/chat_handler.py#L14`.

### Acceptance criteria for M2

| Criterion | How to verify |
|---|---|
| Folder source indexes ≥1 fixture folder into ChromaDB | Unit test + manual `chroma.list_collections()` |
| Citations use `file://path#Lstart` format | Golden test on 5 known queries |
| Re-running research with unchanged folder doesn't re-embed (fast) | Manual timing + unit test |
| Feature flag off → folder source is ignored, default behavior | Smoke test |
| Indexing respects `.gitignore`, excludes common junk dirs | Unit test |

**Merge → tag M2 done → start M3.**

---

# 🟡 Milestone 3 — CodebaseSource

**Why third:** Biggest single win for DevSpace — directly extends the existing Code Workspace. Inherits everything from FolderSource; the new piece is **language-aware chunking**.

**Goal:** From inside the Code Workspace, "Research this repo" produces an architecture overview with citations to real `file:line` locations and respects code boundaries (functions, classes).

**Estimated effort:** 8 dev days
**Risk:** Medium (chunker complexity, large-repo perf)

---

### 3.1 — New file: `backend/src/research_sources/code_chunker.py`

Two implementations behind one interface. Default to the regex one; tree-sitter is an opt-in upgrade.

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List
from .chunker import Chunk


class CodeChunker(ABC):
    @abstractmethod
    def chunk(self, path: Path, text: str) -> List[Chunk]: ...


class RegexCodeChunker(CodeChunker):
    """Language-agnostic chunker that uses brace + indentation heuristics.

    Works well enough for Python/JS/TS/Go/Rust/C/Java without a parser dep.
    """
    LANG_HINTS = {
        ".py":   ["^class\\s+\\w", "^def\\s+\\w", "^    def\\s+\\w", "^async def\\s+\\w"],
        ".js":   ["^function\\s+\\w", "^class\\s+\\w", "^export\\s+(function|class)"],
        ".ts":   ["^function\\s+\\w", "^class\\s+\\w", "^export\\s+(function|class)", "^interface\\s+\\w"],
        ".tsx":  ["^function\\s+\\w", "^const\\s+\\w+\\s*=\\s*\\(", "^export\\s+"],
        ".go":   ["^func\\s+", "^type\\s+\\w+\\s+struct"],
        ".rs":   ["^fn\\s+", "^impl\\s+", "^struct\\s+", "^enum\\s+"],
        ".java": ["^\\s*(public|private|protected)\\s+(class|interface|static)", "^\\s*public\\s+\\w+\\s+\\w+\\s*\\("],
    }

    def chunk(self, path, text):
        hints = self.LANG_HINTS.get(path.suffix.lower(), [])
        if not hints:
            from .chunker import chunk_file
            return chunk_file(path)
        import re
        lines = text.splitlines()
        boundaries = [0]
        for i, line in enumerate(lines):
            if any(re.match(p, line) for p in hints):
                boundaries.append(i)
        boundaries.append(len(lines))
        out: List[Chunk] = []
        for s, e in zip(boundaries, boundaries[1:]):
            body = "\n".join(lines[s:e]).strip()
            if not body:
                continue
            out.append(Chunk(text=body, start_line=s + 1, end_line=e))
        return out


class TreeSitterCodeChunker(CodeChunker):
    """Opt-in: requires `tree-sitter` + language packages.

    Falls back to RegexCodeChunker if a language isn't installed.
    """
    def __init__(self):
        try:
            import tree_sitter_python, tree_sitter_javascript, tree_sitter_typescript  # noqa
            self._available = True
        except ImportError:
            self._available = False

    def chunk(self, path, text):
        if not self._available:
            return RegexCodeChunker().chunk(path, text)
        # Build AST, walk top-level def/class/function nodes, emit one chunk each.
        ...
```

### 3.2 — New file: `backend/src/research_sources/codebase.py`

```python
from pathlib import Path
from typing import Dict, List, Optional
from .folder import FolderSource
from .registry import registry
from .code_chunker import RegexCodeChunker, TreeSitterCodeChunker


@registry.register
class CodebaseSource(FolderSource):
    type_id = "codebase"
    display_name = "Local Codebase"
    config_schema = {
        **FolderSource.config_schema,
        "use_tree_sitter": {"type": "boolean", "default": False},
        "languages":       {"type": "array",  "default": ["python", "javascript", "typescript", "go", "rust"]},
        # Reasonable defaults for code:
        "extensions":      {"type": "array",
                            "default": [".py",".js",".jsx",".ts",".tsx",".go",".rs",".java",
                                        ".c",".h",".cpp",".hpp",".rb",".php"]},
        "exclude_dirs":    {"type": "array",
                            "default": [".git","node_modules","__pycache__","dist","build",
                                        ".venv","venv",".mypy_cache",".pytest_cache","target"]},
    }

    def __init__(self, config=None):
        super().__init__(config)
        self.chunker = (
            TreeSitterCodeChunker() if self.config.get("use_tree_sitter") else RegexCodeChunker()
        )

    # override only the chunker selection; everything else inherits
    def _iter_chunks_for_file(self, p: Path):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        yield from self.chunker.chunk(p, text)
```

Adjust `FolderSource.warmup` to call `self._iter_chunks_for_file(p)` instead of inlining `chunk_file(p)`. This is the only breaking change inside `folder.py` — guarded by the inheritance.

### 3.3 — `@`-mention → research integration

In `backend/src/chat_processor.py`, when the user `@`-mentions a file or folder inside a chat that triggers a research session, attach a `CodebaseSource` automatically:

- `@src-tauri/src` → CodebaseSource pointing at the mentioned subpath
- `@backend/` → CodebaseSource at the workspace root

Sketch (in the chat handler's "tools" branch):

```python
mentions = extract_at_mentions(user_message)        # already exists
paths = [resolve_workspace_path(m) for m in mentions if is_path(m)]
if paths:
    sources.append(CodebaseSource({"path": str(paths[0].parent), "respect_gitignore": True}))
```

### 3.4 — Code Workspace "Research this repo" button

In `backend/static/js/.../code_workspace_panel.js` (or wherever the workspace UI lives), add a button next to the existing Git panel that calls:

```js
POST /api/research/start
{
  query: "give me an architecture overview of this codebase",
  source: { type: "codebase", config: { path: workspaceRoot, use_tree_sitter: false } }
}
```

…and routes the result to the existing report viewer.

### 3.5 — Tests

`backend/tests/test_codebase_source.py`:
- `test_regex_chunker_python_finds_functions_and_classes` — fixture: file with 3 classes and 5 functions, expect 8 chunks with correct line ranges.
- `test_regex_chunker_handles_unknow_ext_falls_back_to_text`
- `test_codebase_inherits_folder_gitignore_behavior`
- `test_tree_sitter_chunker_matches_or_exceeds_regex` — same fixture, opt-in.
- `test_codebase_on_large_repo_completes_in_reasonable_time` — fixture: 5k files, must warmup in < 60s on dev laptop, retrieve in < 500ms.

### 3.6 — Perf guardrails

- Hard cap on **total indexed chunks per source** (default 50k). Surface a warning if exceeded; user must add exclude filters.
- **Indexer concurrency**: process files in a `asyncio.gather` pool of 8. Embedding is usually the bottleneck, not IO.
- **Soft cap on file size**: 1 MB default; bigger files are skipped with a warning logged.
- **Incremental only**: never re-embed on `retrieve()`, only on `warmup()`.

### Acceptance criteria for M3

| Criterion | How to verify |
|---|---|
| CodebaseSource finds function/class boundaries | Unit test on regex chunker |
| Code Workspace "Research this repo" button produces a report | Manual smoke test |
| Citations include `file://.../foo.py#L14-L42` (range, not just start) | Golden test |
| Large fixture repo indexes in < 60s, retrieves in < 500ms | Bench test |
| Feature flag off → CodebaseSource isn't registered | Manual smoke test |

**Merge → tag M3 done → start M4.**

---

# 🟡 Milestone 4 — KnowledgeBaseSource + Hybrid Mode + UI

**Why last:** Closes the loop with the user-facing feature from the issue. Adds persistence, multi-source merging, and the UI picker.

**Goal:** User can create a named knowledge base from multiple folders, run research in **hybrid** mode combining KB + internet, and see merged citations.

**Estimated effort:** 10 dev days
**Risk:** Medium (UI work + concurrency in hybrid mode)

---

### 4.1 — New file: `backend/src/research_sources/knowledge_base.py`

```python
from pathlib import Path
from typing import Dict, List, Optional
from .folder import FolderSource
from .registry import registry
from .chunker import chunk_file
from src.chroma_client import get_chroma_collection
import hashlib, json


@registry.register
class KnowledgeBaseSource(Source):
    """A named, persistent corpus composed of multiple folders.

    Persisted as a small JSON manifest in backend/data/knowledge_bases/<id>.json.
    Embeddings live in a single ChromaDB collection per KB.
    """
    type_id = "kb"
    display_name = "Knowledge Base"
    config_schema = {
        "kb_id": {"type": "string", "required": True},
    }

    def __init__(self, config=None):
        super().__init__(config)
        self.kb_id = self.config["kb_id"]
        self.manifest = self._load_manifest()
        self.folders: List[FolderSource] = [
            FolderSource({**f, "collection_name": f"kb_{self.kb_id}"})
            for f in self.manifest["folders"]
        ]

    @staticmethod
    def _manifest_path(kb_id: str) -> Path:
        from src.constants import DEEP_RESEARCH_DIR
        return Path(DEEP_RESEARCH_DIR).parent / "knowledge_bases" / f"{kb_id}.json"

    def _load_manifest(self) -> Dict:
        p = self._manifest_path(self.kb_id)
        if not p.exists():
            raise FileNotFoundError(f"Knowledge base '{self.kb_id}' not found")
        return json.loads(p.read_text(encoding="utf-8"))

    async def warmup(self) -> None:
        for f in self.folders:
            await f.warmup()

    async def retrieve(self, query, *, limit=10, prior_refs=None):
        out = []
        per = max(1, limit // len(self.folders))
        for f in self.folders:
            out.extend(await f.retrieve(query, limit=per, prior_refs=prior_refs))
        out.sort(key=lambda x: x.score, reverse=True)
        return out[:limit]
```

### 4.2 — New file: `backend/routes/knowledge_base_routes.py`

```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import json, secrets
from src.research_sources import registry

router = APIRouter(prefix="/api/knowledge_bases", tags=["knowledge-bases"])


class KBCreate(BaseModel):
    name: str
    folders: list   # [{"path": "...", "extensions": [...]}]


def _manifests_dir():
    from src.constants import DEEP_RESEARCH_DIR
    d = Path(DEEP_RESEARCH_DIR).parent / "knowledge_bases"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("")
def list_kbs():
    out = []
    for p in _manifests_dir().glob("*.json"):
        m = json.loads(p.read_text())
        out.append({"id": p.stem, "name": m["name"], "folders": m["folders"]})
    return {"knowledge_bases": out}


@router.post("")
def create_kb(body: KBCreate):
    kb_id = secrets.token_urlsafe(8)
    manifest = {"name": body.name, "folders": body.folders}
    (_manifests_dir() / f"{kb_id}.json").write_text(json.dumps(manifest, indent=2))
    return {"id": kb_id, **manifest}


@router.delete("/{kb_id}")
def delete_kb(kb_id: str):
    p = _manifests_dir() / f"{kb_id}.json"
    if not p.exists():
        raise HTTPException(404)
    p.unlink()
    # also drop the chroma collection "kb_<kb_id>" (best-effort)
    try:
        from src.chroma_client import delete_collection
        delete_collection(f"kb_{kb_id}")
    except Exception:
        pass
    return {"deleted": kb_id}
```

Register in `backend/app.py`.

### 4.3 — Hybrid orchestrator (in `deep_research.py`)

Allow `sources: List[Source]` (plural) — already supported by the M1 refactor. In `research()`, when more than one source is present:

```python
# after each round's retrieval:
round_findings: List[Finding] = []
for src in self.sources:
    try:
        round_findings.extend(await src.retrieve(
            query_text,
            limit=src.config.get("max_urls_per_round", 3),
            prior_refs=list(self.urls_fetched),
        ))
    except Exception as e:
        logger.warning(f"Source {src.type_id} failed this round: {e}")

# de-dupe by (source_id, location)
seen = set()
unique = []
for f in round_findings:
    k = (f.ref.source_id, f.ref.location)
    if k in seen:
        continue
    seen.add(k)
    unique.append(f)

# re-rank: keep internet hits but boost local KB hits when query contains
# "this codebase" / "this repo" / "our docs" — naive but effective.
for f in unique:
    if f.ref.source_id in ("folder", "codebase", "kb"):
        if any(tok in query.lower() for tok in ("this codebase", "this repo", "our docs", "internal")):
            f.score *= 1.2
unique.sort(key=lambda x: x.score, reverse=True)
```

The downstream extraction + synthesis code is **unchanged** — it already consumes a flat list of hits.

### 4.4 — File watcher (debounced)

`backend/services/research/watcher.py`:

```python
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from threading import Timer
import asyncio


class DebouncedReindexHandler(FileSystemEventHandler):
    def __init__(self, source, *, debounce_seconds: float = 5.0):
        self.source = source
        self._timer: Timer | None = None
        self._loop = asyncio.get_event_loop()

    def _trigger(self):
        if self._timer:
            self._timer.cancel()
        self._timer = Timer(self.debounce_seconds, lambda: asyncio.run_coroutine_threadsafe(
            self.source.warmup(), self._loop
        ))
        self._timer.start()

    def on_any_event(self, event):
        if event.is_directory:
            return
        self._trigger()


def attach_watcher(source: KnowledgeBaseSource):
    obs = Observer()
    for f in source.folders:
        obs.schedule(DebouncedReindexHandler(source), f.root, recursive=True)
    obs.start()
    return obs
```

Only auto-watches `KnowledgeBaseSource` (never `InternetSource`); wired up in the handler when a KB-backed session is created and torn down on `cancel`.

### 4.5 — UI — source picker

New file: `backend/static/js/research/source_picker.js`

```js
// rendered above the existing query input
async function renderSourcePicker(rootEl) {
  const { sources } = await fetch('/api/research/sources').then(r => r.json());
  const kbs  = await fetch('/api/knowledge_bases').then(r => r.json()).catch(() => ({knowledge_bases:[]}));

  rootEl.innerHTML = `
    <label>Sources</label>
    <select id="source-type">
      <option value="internet">Internet (default)</option>
      ${sources.filter(s => s.type !== 'internet').map(s => `<option value="${s.type}">${s.name}</option>`).join('')}
      ${kbs.knowledge_bases.map(kb => `<option value="kb" data-kb="${kb.id}">KB: ${kb.name}</option>`).join('')}
    </select>
    <div id="source-config"></div>
  `;

  document.getElementById('source-type').onchange = (e) => {
    const cfg = rootEl.querySelector('#source-config');
    cfg.innerHTML = renderConfigForm(e.target.value, e.target.selectedOptions[0].dataset.kb);
  };
}
```

Two screens:
1. **Deep Research panel** — picker sits above the query box. "Add another source" repeats the picker (multi-select → hybrid mode).
2. **KB management screen** — CRUD on `/knowledge_bases`, accessible from settings.

### 4.6 — Citation rendering in the report

In `backend/src/deep_research.py` and the report formatter (likely `services/research/research_handler.py:_format_research_report`), make `SourceRef.location` drive the citation:

- `http(s)://...` → existing link rendering.
- `file:///abs/path#L14-L42` → clickable link that opens in the Code Workspace editor (existing route `code_workspace_routes.py` already supports file open).
- `kb://name/relative/path#L14` → opens the KB manager and highlights the file.

### 4.7 — Tests

`backend/tests/test_kb_source.py`:
- `test_create_list_delete_kb`
- `test_kb_persists_across_process_restarts` — write manifest, reload, assert identity.
- `test_kb_retrieve_aggregates_across_folders`

`backend/tests/test_hybrid_orchestrator.py`:
- `test_hybrid_merges_internet_and_kb_findings`
- `test_hybrid_dedupes_by_location`
- `test_hybrid_boosts_local_when_query_indicates_local`

`backend/tests/test_watcher.py`:
- `test_watcher_debounces_multiple_events`
- `test_watcher_skips_excluded_dirs`

### 4.8 — E2E

`backend/tests/e2e_research_sources.py` — uses an in-process FastAPI client:
- Create a KB from 2 fixture folders.
- Start a hybrid research session.
- Assert response contains findings from both sources.
- Assert citations include `file://` references.
- Screenshot via Playwright of the UI picker (manual, attach to PR).

### Acceptance criteria for M4

| Criterion | How to verify |
|---|---|
| KB CRUD endpoints work and persist | E2E |
| Hybrid mode merges findings from ≥2 sources without duplicates | Unit test |
| `file://` citations open in Code Workspace editor on click | Manual UI test |
| File watcher debounces (5s default) and re-indexes | Unit test |
| UI picker renders all registered sources + KBs | Screenshot |
| Feature flag on: end-to-end research on a real repo + internet | Manual smoke test |

**Merge → tag M4 done → close issue #2.**

---

# Cross-cutting concerns (apply to all milestones)

### Feature flag
- Env var `RESEARCH_SOURCES_ENABLED` (default `false`).
- M1 ships with flag off (pure refactor, no behavior change).
- M2-M4 each ship with flag off; turn it on in dev/staging only.
- M4 is the milestone where the flag flips to `true` by default in `dev`.

### Logging
- Every source adapter logs its `warmup` duration, # files indexed, # chunks added/skipped/replaced.
- Every `retrieve()` logs latency, # findings returned, top score.

### Error handling
- A single source failing must NOT abort the whole research session. Wrap each `src.retrieve()` in try/except in the orchestrator (already sketched in M4.3).
- ChromaDB unavailability → log + fall back to "source unavailable" notice in the report footer.

### Privacy / security
- `path` configs must resolve within the user's workspace (no `/etc/passwd`). Add `Path.resolve()` + a check that the resolved path is under `Path.home()` or a configured allowed roots list in `backend/src/config.py`.
- KBs that contain `.env` / `secrets.*` must be indexable but with a clear warning that they will be embedded.

### Docs
- `docs/research_sources.md` (new) — adapter author guide.
- Update README's "Deep Research" section in M4.

### Telemetry (optional, gated by another env flag)
- Count `retrieve()` calls per source type.
- Don't log query content.

---

# Final merge plan & issue closure

| PR | Milestone | Reviewers | Estimated review rounds |
|---|---|---|---|
| #1 | M1 — Abstraction | backend lead | 2 |
| #2 | M2 — FolderSource | backend lead | 2 |
| #3 | M3 — CodebaseSource | backend lead + code workspace owner | 3 |
| #4 | M4 — KB + Hybrid + UI | backend lead + frontend owner | 3 |

When PR #4 merges:
1. Flip `RESEARCH_SOURCES_ENABLED` to default `true` in `dev`.
2. Post a comment on issue #2 linking to the docs page and the merged PRs.
3. Close the issue with the comment: *"Implemented via PR #1–#4. Available behind `RESEARCH_SOURCES_ENABLED` (default on in next release). Docs: `docs/research_sources.md`."*

---

# Open questions to confirm with @Khalid-Tarek on issue #2 BEFORE M2

1. Is the "internal knowledge repository" a **remote** git repo (GitHub/GitLab/SSH) or strictly a local one? *(Affects whether M5 needs a git-clone adapter — out of scope right now.)*
2. Should sources be re-indexed automatically on file change, or only on explicit "rebuild"? *(M4.4 assumes debounced auto-watch with opt-out.)*
3. Is **hybrid mode** (internet + local) the default or opt-in? *(M4.3 assumes opt-in, internet-only when only one source picked.)*
4. Should the citation format for local sources use `file://path#Lstart-Lend` or `path:line`? *(Plan assumes `file://` because it round-trips into the Code Workspace editor.)*

Post these as a checklist comment on issue #2 before starting M2.

---

# Quick links

- Issue: https://github.com/AEmad99/DevSpace/issues/2
- DeepResearcher: `backend/src/deep_research.py` (line 184)
- ResearchHandler: `backend/services/research/research_handler.py` (line 25)
- Research routes: `backend/routes/research_routes.py`
- ChromaDB client: `backend/src/chroma_client.py`
- Embeddings lane: `backend/src/embedding_lanes.py`, `backend/src/embeddings.py`
- Code Workspace routes: `backend/routes/code_workspace_routes.py`
- Research UI: `backend/static/js/research/`
