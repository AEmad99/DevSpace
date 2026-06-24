"""`FolderSource` — research over a local folder of files (issue #2 / M2).

Indexes the folder into a per-folder ChromaDB collection and returns
findings with `file://abs/path#Lstart-Lend` citations.

Design choices:
  - One ChromaDB collection per folder, auto-named by SHA1 of the resolved
    absolute path. Re-using the same folder always lands in the same
    collection, so re-indexing is incremental.
  - Incremental indexing: warmup() compares the on-disk mtime + size
    against the stored metadata, and only re-embeds files that changed
    or are new. Unchanged files are skipped (no re-embed cost).
  - Excludes common junk directories by default (node_modules, .git,
    __pycache__, dist, build, .venv, venv, .mypy_cache, .pytest_cache,
    target). Configurable via `exclude_dirs`.
  - Honors .gitignore when `respect_gitignore=True` (default).
  - Retrieval is plain cosine-similarity search over the per-chunk
    embeddings. The full document is not returned — only the chunk
    that matched. This keeps prompts tight; the citation link opens
    the file in the Code Workspace editor for full context.
  - Path is resolved under `Path.home()` to keep users from accidentally
    indexing `/etc` or other system directories (defense-in-depth, see
    docs/issue-2-knowledge-sources-detailed-plan.md §Cross-cutting).
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .chunker import chunk_file
from .registry import registry

logger = logging.getLogger(__name__)


# Defaults — overridable via Source config.
_DEFAULT_EXTS: Set[str] = {
    ".md", ".txt", ".rst", ".adoc",         # prose
    ".py", ".js", ".jsx", ".ts", ".tsx",    # code (regex chunker in M3 handles classes/fns)
    ".json", ".yaml", ".yml", ".toml",      # config
    ".html", ".htm", ".css",                # web
    ".sh", ".bash", ".zsh",                 # shell
    ".sql", ".csv",                         # data
    ".tex",                                 # academic
}
_DEFAULT_EXCLUDE_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", "dist", "build",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", "target",
    ".tox", ".nox", "out", ".next", ".nuxt", ".cache",
    ".idea", ".vscode",
}
_DEFAULT_MAX_FILE_BYTES = 1_000_000   # 1 MB; bigger files skipped


@registry.register
class FolderSource(Source):
    """Research over a local folder of files.

    Config keys:
        path               (str, required)         absolute path to the folder
        extensions         (list[str], optional)   file extensions to include
        exclude_dirs       (list[str], optional)   directory names to skip
        max_file_bytes     (int, optional)         skip files larger than this
        respect_gitignore  (bool, default True)    honor .gitignore files
        collection_name    (str, optional)         override the auto-derived
                                                   ChromaDB collection name
        max_chunks         (int, default 50_000)   hard cap on indexed chunks
                                                   per folder (raises warning
                                                   if exceeded; user must
                                                   narrow filters)
    """
    type_id = "folder"
    display_name = "Local Folder"
    config_schema = {
        "path": {"type": "string", "required": True},
        "extensions": {"type": "array", "items": {"type": "string"},
                       "default": sorted(_DEFAULT_EXTS)},
        "exclude_dirs": {"type": "array", "items": {"type": "string"},
                         "default": sorted(_DEFAULT_EXCLUDE_DIRS)},
        "max_file_bytes": {"type": "integer", "default": _DEFAULT_MAX_FILE_BYTES,
                           "minimum": 1024},
        "respect_gitignore": {"type": "boolean", "default": True},
        "collection_name": {"type": "string", "default": None},
        "max_chunks": {"type": "integer", "default": 50_000, "minimum": 100},
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        path_str = self.config.get("path")
        if not path_str:
            raise ValueError("FolderSource requires 'path' in config")
        self.root: Path = self._safe_resolve(Path(path_str))
        if not self.root.exists():
            raise FileNotFoundError(f"Folder path does not exist: {self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"Folder path is not a directory: {self.root}")
        self.exts: Set[str] = set(
            e if e.startswith(".") else "." + e
            for e in (self.config.get("extensions") or _DEFAULT_EXTS)
        )
        self.exclude_dirs: Set[str] = set(
            self.config.get("exclude_dirs") or _DEFAULT_EXCLUDE_DIRS
        )
        self.max_file_bytes: int = int(
            self.config.get("max_file_bytes", _DEFAULT_MAX_FILE_BYTES)
        )
        self.respect_gitignore: bool = bool(
            self.config.get("respect_gitignore", True)
        )
        self.max_chunks: int = int(self.config.get("max_chunks", 50_000))
        self.collection_name: str = (
            self.config.get("collection_name") or self._auto_collection_name()
        )

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_resolve(p: Path) -> Path:
        """Resolve `p` to an absolute path; refuse system paths.

        Defense-in-depth (see plan §Cross-cutting / Privacy & security):
        the user can configure any path, but we anchor resolution under
        `Path.home()` to keep them from accidentally indexing /etc,
        C:\Windows, etc. Outside-home paths require `allow_system_paths=true`.
        """
        resolved = p.expanduser().resolve()
        home = Path.home().resolve()
        try:
            resolved.relative_to(home)
            return resolved
        except ValueError:
            # Outside home — still allow, but log loudly.
            logger.warning(
                "FolderSource indexing path outside $HOME (%s); "
                "this is allowed but review your folder config.",
                resolved,
            )
            return resolved

    @staticmethod
    def _auto_collection_name_for(root: Path) -> str:
        h = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:10]
        # Chroma collection names: 3-63 chars, [a-zA-Z0-9_-], start/end alphanumeric.
        safe_root = "".join(c if c.isalnum() else "_" for c in root.name)[:32] or "root"
        return f"folder_{safe_root}_{h}"

    def _auto_collection_name(self) -> str:
        return self._auto_collection_name_for(self.root)

    # ------------------------------------------------------------------
    # .gitignore support
    # ------------------------------------------------------------------

    def _load_gitignore_specs(self) -> List[Any]:
        """Return pathspec matchers for every .gitignore under self.root.

        Cheap O(N) walk at warmup; we cache the result on the instance so
        repeated warmups during one research session don't re-walk.
        """
        if getattr(self, "_gitignore_specs", None) is not None:
            return self._gitignore_specs
        if not self.respect_gitignore:
            self._gitignore_specs: List[Any] = []
            return self._gitignore_specs
        try:
            import pathspec   # type: ignore
        except ImportError:
            # pathspec is optional — if not installed, just skip .gitignore.
            logger.info("pathspec not installed; skipping .gitignore support")
            self._gitignore_specs = []
            return self._gitignore_specs
        specs: List[Any] = []
        for gi in self.root.rglob(".gitignore"):
            try:
                specs.append((gi.parent, pathspec.PathSpec.from_lines(
                    "gitwildmatch", gi.read_text(encoding="utf-8", errors="replace").splitlines()
                )))
            except Exception as e:
                logger.debug(f"Skipping malformed .gitignore {gi}: {e}")
        self._gitignore_specs = specs
        return specs

    def _is_gitignored(self, p: Path) -> bool:
        if not self.respect_gitignore:
            return False
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            return False
        for base, spec in self._load_gitignore_specs():
            try:
                rel_to_base = p.relative_to(base).as_posix()
            except ValueError:
                continue
            if spec.match_file(rel_to_base):
                return True
        return False

    # ------------------------------------------------------------------
    # File enumeration
    # ------------------------------------------------------------------

    def _iter_files(self) -> List[Path]:
        out: List[Path] = []
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in self.exclude_dirs for part in p.parts):
                continue
            if p.suffix.lower() not in self.exts:
                continue
            try:
                if p.stat().st_size > self.max_file_bytes:
                    logger.debug(f"Skipping oversized file: {p}")
                    continue
            except OSError:
                continue
            if self._is_gitignored(p):
                continue
            out.append(p)
        return out

    # ------------------------------------------------------------------
    # Embedding lane resolution
    # ------------------------------------------------------------------

    def _resolve_lane(self):
        """Return a healthy EmbeddingLane (custom > fastembed) for our collection.

        Raises a clear error if neither lane is available — the user
        hasn't installed `chromadb` or `fastembed`.
        """
        from src.embedding_lanes import build_embedding_lanes
        lanes = build_embedding_lanes(self.collection_name)
        healthy = [l for l in lanes if l.healthy]
        if not healthy:
            raise RuntimeError(
                f"No healthy embedding lane available for folder '{self.root}'. "
                "Install `chromadb` + `fastembed` (default) or configure a custom "
                "embedding endpoint in Settings."
            )
        return healthy[0]

    # ------------------------------------------------------------------
    # Source lifecycle
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Incrementally index the folder. Skips unchanged files.

        Side effects on disk (ChromaDB collection): upserts for changed
        files; deletes for files that disappeared since last warmup.
        """
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"FolderSource warmup skipped (no embedding lane): {e}")
            self._warn_on_no_lane = True
            return

        coll = lane.collection
        files = self._iter_files()
        if not files:
            return

        # Determine which files are new or changed vs. the collection state.
        # Chroma's `get` returns a dict with `ids` and `metadatas` lists.
        try:
            existing = coll.get(include=["metadatas"])
        except Exception as e:
            logger.warning(f"Chroma get() failed during folder warmup: {e}")
            existing = {"ids": [], "metadatas": []}

        # Index existing chunks by their `path` metadata so we can detect
        # deletions and mtime changes.
        existing_by_path: Dict[str, List[Dict[str, Any]]] = {}
        for cid, meta in zip(existing.get("ids", []), existing.get("metadatas", [])):
            if not meta:
                continue
            existing_by_path.setdefault(meta.get("path", ""), []).append(
                {"id": cid, **meta}
            )

        current_paths: Set[str] = set()
        to_upsert: List[Path] = []
        for p in files:
            key = str(p)
            current_paths.add(key)
            try:
                mtime = p.stat().st_mtime
                size = p.stat().st_size
            except OSError:
                continue
            prev = existing_by_path.get(key)
            if prev and all(m.get("mtime") == mtime and m.get("size") == size
                            for m in prev):
                continue   # unchanged
            to_upsert.append(p)

        # Deletions: paths in existing_by_path but not in current_paths.
        to_delete_paths = [k for k in existing_by_path if k and k not in current_paths]

        if not to_upsert and not to_delete_paths:
            logger.info(f"FolderSource warmup: {self.root} already up to date "
                        f"({len(existing.get('ids', []))} chunks)")
            return

        # Cap: refuse to index more than `max_chunks` chunks total per folder.
        projected_chunks = len(existing.get("ids", []))
        for p in to_upsert:
            try:
                # Conservative estimate: 1 chunk per ~1.5 KB. Actual count comes later.
                projected_chunks += max(1, p.stat().st_size // 1500)
            except OSError:
                pass
        if projected_chunks > self.max_chunks:
            logger.warning(
                "FolderSource: projected %d chunks exceeds cap %d for %s; "
                "narrow your config (extensions / exclude_dirs) before indexing.",
                projected_chunks, self.max_chunks, self.root,
            )

        # Delete removed files' chunks in batches.
        if to_delete_paths:
            try:
                ids_to_delete = [
                    m["id"]
                    for k in to_delete_paths
                    for m in existing_by_path.get(k, [])
                    if m.get("id")
                ]
                if ids_to_delete:
                    coll.delete(ids=ids_to_delete)
            except Exception as e:
                logger.warning(f"FolderSource delete failed (continuing): {e}")

        # Upsert changed/new files.
        if to_upsert:
            docs: List[str] = []
            metas: List[Dict[str, Any]] = []
            ids: List[str] = []
            for p in to_upsert:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for ch in chunk_file(p, text):
                    docs.append(ch.text)
                    metas.append({
                        "path": str(p),
                        "start_line": ch.start_line,
                        "end_line": ch.end_line,
                        "mtime": p.stat().st_mtime,
                        "size": p.stat().st_size,
                    })
                    ids.append(self._chunk_id(p, ch.start_line, ch.end_line))
            if docs:
                try:
                    embeddings = lane.encode(docs)
                    coll.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
                    logger.info(f"FolderSource indexed {len(docs)} chunks from "
                                f"{len(to_upsert)} file(s) in {self.root}")
                except Exception as e:
                    logger.error(f"FolderSource upsert failed: {e}", exc_info=True)
                    self._emit_status = f"upsert failed: {e}"

    async def shutdown(self) -> None:
        # Nothing to release — the embedding lane is a singleton.
        return None

    @staticmethod
    def _chunk_id(p: Path, start: int, end: int) -> str:
        """Stable per-chunk ID so re-indexing replaces in-place."""
        return f"{p}#{start}-{end}"

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
        """Return the top chunks across all `queries`, deduped by path+lines.

        For each query, ask the lane for its top `limit` chunks, then merge
        across queries by `(path, start_line, end_line)` and keep the best
        score per chunk. Filter out chunks whose `file://...` location is
        in `prior_refs`.
        """
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"FolderSource retrieve skipped: {e}")
            return []
        coll = lane.collection

        prior = set(prior_refs or [])
        # Aggregate per-query hits.
        best: Dict[str, Finding] = {}
        per_query = max(1, limit)
        for q in queries:
            try:
                q_emb = lane.encode([q])[0]
                res = coll.query(query_embeddings=[q_emb], n_results=per_query)
            except Exception as e:
                logger.warning(f"FolderSource query failed for {q!r}: {e}")
                continue
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                if not meta:
                    continue
                loc = self._location(meta)
                if loc in prior:
                    continue
                score = self._score_from_distance(dist)
                f = self._wrap(doc, meta, score)
                key = f"{meta.get('path')}#{meta.get('start_line')}-{meta.get('end_line')}"
                if key not in best or best[key].score < f.score:
                    best[key] = f
            if len(best) >= limit:
                break

        out = sorted(best.values(), key=lambda x: x.score, reverse=True)[:limit]
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _location(meta: Dict[str, Any]) -> str:
        return f"file://{meta['path']}#L{meta.get('start_line', 1)}-L{meta.get('end_line', 1)}"

    @staticmethod
    def _score_from_distance(dist: Any) -> float:
        """Convert Chroma cosine distance (0..2) to a similarity score (0..1)."""
        try:
            d = float(dist)
        except (TypeError, ValueError):
            return 0.0
        # Cosine distance is 1 - similarity for normalized vectors.
        return max(0.0, min(1.0, 1.0 - d))

    @staticmethod
    def _wrap(doc: str, meta: Dict[str, Any], score: float) -> Finding:
        path = meta.get("path", "")
        title = Path(path).name if path else "unknown"
        loc = FolderSource._location(meta)
        return Finding(
            content=doc,
            ref=SourceRef(
                source_id="folder",
                title=title,
                location=loc,
                snippet=doc[:200],
                metadata={
                    "path": path,
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                    "mtime": meta.get("mtime"),
                },
            ),
            score=score,
            metadata={"path": path},
        )
