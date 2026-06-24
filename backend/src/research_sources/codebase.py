"""`CodebaseSource` — language-aware research over a local codebase (M3).

Subclass of `FolderSource` that swaps in a code-aware chunker so research
over a repo returns whole-function / whole-class context instead of the
mid-statement cuts the prose chunker would produce.

Everything else (file enumeration, ChromaDB indexing, retrieval,
citation format, prior_refs handling) is inherited from `FolderSource`
unchanged — we only override:
  1. `type_id` / `display_name` / `config_schema` for the registry.
  2. The default extensions / exclude_dirs — tighter code defaults.
  3. The chunker selection — Regex by default; TreeSitter when installed
     AND `use_tree_sitter=True`.

Citation format is the same as `FolderSource` (`file://abs/path#Lstart-Lend`),
so the existing report renderer just works. Range citations mean the user
sees the whole function context in the citation chip.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .base import Finding, Source, SourceRef
from .chunker import Chunk
from .code_chunker import CodeChunker, RegexCodeChunker, TreeSitterCodeChunker
from .folder import _DEFAULT_EXCLUDE_DIRS, FolderSource
from .registry import registry

logger = logging.getLogger(__name__)


# Code-focused defaults — overrides the prose-leaning defaults of FolderSource.
_CODE_EXTS: Set[str] = {
    ".py", ".pyi", ".pyx",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx", ".d.ts",
    ".go", ".rs",
    ".java", ".kt", ".scala",
    ".c", ".h", ".cpp", ".cxx", ".hpp", ".hxx", ".cc",
    ".rb", ".php",
    ".cs", ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh",
    ".lua", ".pl", ".r",
    ".sql", ".html", ".htm", ".css", ".scss", ".less",
    ".json", ".yaml", ".yml", ".toml", ".xml",
    ".md", ".rst",            # docs alongside code are valuable context
}

# Code repos have even more junk dirs than generic folders.
_CODE_EXCLUDE_DIRS: Set[str] = _DEFAULT_EXCLUDE_DIRS | {
    "vendor", "third_party", "Pods", "DerivedData",
    "cmake-build-debug", "cmake-build-release", "build",
    "obj", "bin", "lib",
    "coverage", ".nyc_output",
    ".terraform", ".serverless",
    ".gradle", ".idea",
    "node_modules",          # already in FolderSource defaults, listed again for clarity
}


@registry.register
class CodebaseSource(FolderSource):
    """Research over a local codebase with language-aware chunking.

    Extends `FolderSource` with:
      - Code-focused default extensions / exclude dirs.
      - A pluggable chunker (`regex` or `tree_sitter`).
      - Same `file://path#Lstart-Lend` citation format (range citations
        mean the chip shows the whole function/class).

    Config keys (all optional except `path`):
        path              (str, required)         folder root
        use_tree_sitter   (bool, default False)   opt-in AST chunker
        extensions        (list[str])             override the default code extensions
        exclude_dirs      (list[str])             override the default excludes
        max_file_bytes    (int, default 1 MB)
        respect_gitignore (bool, default True)
        collection_name   (str)                   override the auto collection name
        max_chunks        (int, default 50_000)
    """
    type_id = "codebase"
    display_name = "Local Codebase"
    config_schema = {
        **FolderSource.config_schema,
        "use_tree_sitter": {"type": "boolean", "default": False},
        # Override the inherited defaults with code-focused ones.
        "extensions": {"type": "array", "items": {"type": "string"},
                       "default": sorted(_CODE_EXTS)},
        "exclude_dirs": {"type": "array", "items": {"type": "string"},
                         "default": sorted(_CODE_EXCLUDE_DIRS)},
    }

    # ------------------------------------------------------------------
    # Override defaults inherited from FolderSource when not user-supplied.
    # ------------------------------------------------------------------

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        # If the user didn't override `extensions` / `exclude_dirs`, swap in
        # the code-focused defaults AFTER FolderSource.__init__ ran (which
        # already populated self.exts / self.exclude_dirs from the prose
        # defaults). Without this, CodebaseSource would inherit
        # `.csv`/`.tex`/etc. as indexed extensions and miss `.go`/`.rs`.
        cfg = config or {}
        if "extensions" not in cfg:
            self.exts = set(_CODE_EXTS)
        if "exclude_dirs" not in cfg:
            self.exclude_dirs = set(_CODE_EXCLUDE_DIRS)
        # Pick chunker once; both implementations are stateless w.r.t. files.
        self._chunker: CodeChunker = self._make_chunker()

    def _make_chunker(self) -> CodeChunker:
        if self.config.get("use_tree_sitter"):
            try:
                ts = TreeSitterCodeChunker()
                if ts._available_langs:  # at least one language loaded
                    return ts
                logger.info(
                    "Tree-sitter chunker selected but no languages loaded; "
                    "falling back to regex."
                )
            except Exception as e:
                logger.warning(f"TreeSitterCodeChunker init failed: {e}")
        return RegexCodeChunker()

    # ------------------------------------------------------------------
    # Override the chunker selection point used by FolderSource.warmup().
    #
    # FolderSource.warmup() does `for ch in chunk_file(p, text):` — we want
    # to insert `self._chunker.chunk(p, text)` instead. The cleanest way
    # without changing FolderSource's signature is to override the entire
    # warmup() here, calling the same indexing helpers but using our
    # chunker. This duplicates a little code but keeps the contract
    # simple.
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Mirror FolderSource.warmup() but use the code chunker."""
        try:
            lane = self._resolve_lane()
        except Exception as e:
            logger.warning(f"CodebaseSource warmup skipped (no embedding lane): {e}")
            return

        coll = lane.collection
        files = self._iter_files()
        if not files:
            return

        try:
            existing = coll.get(include=["metadatas"])
        except Exception as e:
            logger.warning(f"Chroma get() failed during codebase warmup: {e}")
            existing = {"ids": [], "metadatas": []}

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
                continue
            to_upsert.append(p)

        to_delete_paths = [k for k in existing_by_path if k and k not in current_paths]

        if not to_upsert and not to_delete_paths:
            logger.info(f"CodebaseSource warmup: {self.root} already up to date "
                        f"({len(existing.get('ids', []))} chunks)")
            return

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
                logger.warning(f"CodebaseSource delete failed (continuing): {e}")

        if to_upsert:
            docs: List[str] = []
            metas: List[Dict[str, Any]] = []
            ids: List[str] = []
            for p in to_upsert:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for ch in self._chunker.chunk(p, text):
                    docs.append(ch.text)
                    metas.append({
                        "path": str(p),
                        "start_line": ch.start_line,
                        "end_line": ch.end_line,
                        "mtime": p.stat().st_mtime,
                        "size": p.stat().st_size,
                        "language": self._language_of(p),
                    })
                    ids.append(self._chunk_id(p, ch.start_line, ch.end_line))
            if docs:
                try:
                    embeddings = lane.encode(docs)
                    coll.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
                    logger.info(f"CodebaseSource indexed {len(docs)} chunks from "
                                f"{len(to_upsert)} file(s) in {self.root}")
                except Exception as e:
                    logger.error(f"CodebaseSource upsert failed: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Inherited Finding helpers — override so citations say `codebase`,
    # not `folder`. The shape is otherwise identical to FolderSource.
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap(doc: str, meta: Dict[str, Any], score: float) -> Finding:
        path = meta.get("path", "")
        title = Path(path).name if path else "unknown"
        # Same range-citation format as FolderSource (file://...#Lstart-Lend)
        loc = f"file://{path}#L{meta.get('start_line', 1)}-L{meta.get('end_line', 1)}"
        return Finding(
            content=doc,
            ref=SourceRef(
                source_id="codebase",
                title=title,
                location=loc,
                snippet=doc[:200],
                metadata={
                    "path": path,
                    "start_line": meta.get("start_line"),
                    "end_line": meta.get("end_line"),
                    "language": meta.get("language"),
                },
            ),
            score=score,
            metadata={"path": path, "language": meta.get("language")},
        )

    @staticmethod
    def _language_of(p: Path) -> str:
        ext = p.suffix.lower()
        # Tiny static map — enough for the UI badge; full mapping would be
        # the linguist-style lookup table (out of scope).
        return {
            ".py": "python", ".pyi": "python", ".pyx": "python",
            ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".go": "go", ".rs": "rust",
            ".java": "java", ".kt": "kotlin",
            ".rb": "ruby", ".php": "php",
            ".cs": "csharp",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
            ".sh": "shell", ".bash": "shell", ".zsh": "shell",
            ".md": "markdown", ".rst": "rst",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
        }.get(ext, "unknown")
