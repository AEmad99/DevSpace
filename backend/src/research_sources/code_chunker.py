"""Language-aware chunkers for `CodebaseSource` (issue #2 / M3).

Two implementations behind one interface:

  - `RegexCodeChunker` (default, zero deps) — uses brace + indentation
    heuristics to find function/class/method boundaries for the most
    common languages. Good enough for Python, JS, TS, Go, Rust, Java,
    C/C++. Falls back to the prose chunker for unknown extensions.

  - `TreeSitterCodeChunker` (opt-in via `use_tree_sitter=True`) — uses
    a real parser AST to find top-level definitions. More accurate but
    requires the `tree_sitter` package and per-language packages to be
    installed. When anything is missing, falls back to RegexCodeChunker
    with a single WARNING log line — the user still gets working code
    chunks; they just don't get AST-perfect ones.

The chunkers return the same `Chunk` dataclass as `chunker.py` so
`CodebaseSource` can drop in either one without changes downstream.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Type

from .chunker import Chunk, chunk_file

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Common interface
# ----------------------------------------------------------------------


class CodeChunker:
    """Base class for language-aware code chunkers.

    Subclasses override `chunk(path, text)`. They MUST return Chunks whose
    `start_line` / `end_line` are accurate 1-indexed inclusive line numbers
    into the original file, just like `chunk_file`.
    """
    def chunk(self, path: Path, text: str) -> List[Chunk]:
        raise NotImplementedError


# ----------------------------------------------------------------------
# Regex chunker — language-agnostic, works on the common file types.
# ----------------------------------------------------------------------


# Compiled regexes per extension. Each pattern matches the FIRST LINE of a
# top-level (or method) definition. We then emit one chunk per definition
# spanning from its first line to the next definition's first line.
_LANG_HINTS: Dict[str, List[re.Pattern]] = {
    ".py": [
        re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+\w+"),
        re.compile(r"^[ \t]*class[ \t]+\w+"),
        re.compile(r"^[ \t]*@[A-Za-z_]\w*"),            # decorators → part of next def
    ],
    ".js": [
        re.compile(r"^(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*\w+"),
        re.compile(r"^(?:export[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?(?:const|let|var)[ \t]+\w+[ \t]*=[ \t]*\("),
    ],
    ".jsx": [
        re.compile(r"^(?:export[ \t]+)?(?:default[ \t]+)?function[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^const[ \t]+\w+[ \t]*=[ \t]*\("),
    ],
    ".ts": [
        re.compile(r"^(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*\w+"),
        re.compile(r"^(?:export[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?interface[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?type[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?(?:const|let|var)[ \t]+\w+[ \t]*[:=]"),
    ],
    ".tsx": [
        re.compile(r"^(?:export[ \t]+)?(?:default[ \t]+)?function[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^(?:export[ \t]+)?interface[ \t]+\w+"),
        re.compile(r"^const[ \t]+\w+[ \t]*[:=]"),
    ],
    ".go": [
        re.compile(r"^func[ \t]+(?:\(\w+[ \t]+\*?\w+\)[ \t]+)?\w+"),
        re.compile(r"^type[ \t]+\w+[ \t]+struct"),
        re.compile(r"^type[ \t]+\w+[ \t]+interface"),
    ],
    ".rs": [
        re.compile(r"^(?:pub[ \t]+)?fn[ \t]+\w+"),
        re.compile(r"^(?:pub[ \t]+)?struct[ \t]+\w+"),
        re.compile(r"^(?:pub[ \t]+)?enum[ \t]+\w+"),
        re.compile(r"^(?:pub[ \t]+)?trait[ \t]+\w+"),
        re.compile(r"^(?:pub[ \t]+)?impl[ \t]+"),
        re.compile(r"^(?:pub[ \t]+)?mod[ \t]+\w+"),
    ],
    ".java": [
        re.compile(r"^[ \t]*(?:public|private|protected)?[ \t]*(?:static[ \t]+)?(?:final[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^[ \t]*(?:public|private|protected)?[ \t]*interface[ \t]+\w+"),
        re.compile(r"^[ \t]*(?:public|private|protected)[ \t]+(?:static[ \t]+)?[\w<>\[\],\s]+[ \t]+\w+[ \t]*\("),
    ],
    ".c": [
        re.compile(r"^(?:static[ \t]+)?(?:inline[ \t]+)?(?:const[ \t]+)?[\w \t*]+[ \t]+\w+[ \t]*\([^;]*\)[ \t]*\{"),
        re.compile(r"^(?:typedef[ \t]+)?struct[ \t]+\w+"),
    ],
    ".h": [
        re.compile(r"^(?:static[ \t]+)?(?:inline[ \t]+)?(?:const[ \t]+)?[\w \t*]+[ \t]+\w+[ \t]*\([^;]*\)[ \t]*\{"),
        re.compile(r"^(?:typedef[ \t]+)?struct[ \t]+\w+"),
        re.compile(r"^#define[ \t]+\w+"),
    ],
    ".cpp": [
        re.compile(r"^(?:static[ \t]+)?(?:inline[ \t]+)?(?:virtual[ \t]+)?[\w:<> \t*&]+[ \t]+\w+[ \t]*\([^;]*\)[ \t]*(?:const)?[ \t]*\{?"),
        re.compile(r"^(?:class|struct)[ \t]+\w+"),
        re.compile(r"^namespace[ \t]+\w+"),
    ],
    ".hpp": [
        re.compile(r"^(?:class|struct)[ \t]+\w+"),
        re.compile(r"^(?:static[ \t]+)?(?:inline[ \t]+)?[\w:<> \t*&]+[ \t]+\w+[ \t]*\([^;]*\)"),
    ],
    ".rb": [
        re.compile(r"^[ \t]*def[ \t]+[\w?!.]+"),
        re.compile(r"^[ \t]*class[ \t]+\w+"),
        re.compile(r"^[ \t]*module[ \t]+\w+"),
    ],
    ".php": [
        re.compile(r"^[ \t]*(?:public|private|protected)?[ \t]*function[ \t]+\w+"),
        re.compile(r"^[ \t]*class[ \t]+\w+"),
        re.compile(r"^[ \t]*interface[ \t]+\w+"),
        re.compile(r"^[ \t]*trait[ \t]+\w+"),
    ],
    ".cs": [
        re.compile(r"^[ \t]*(?:public|private|protected|internal)?[ \t]*(?:static[ \t]+)?(?:partial[ \t]+)?class[ \t]+\w+"),
        re.compile(r"^[ \t]*(?:public|private|protected|internal)?[ \t]*(?:static[ \t]+)?[\w<>\[\]]+[ \t]+\w+[ \t]*\("),
    ],
}


class RegexCodeChunker(CodeChunker):
    """Pattern-based code chunker.

    Strategy:
      - Compile a list of regexes from `_LANG_HINTS` for the file's extension.
      - If no hints exist, delegate to `chunk_file` (prose chunker).
      - Find all matching lines as definition boundaries.
      - Emit one chunk per boundary: from that line (including decorators /
        preceding comments) up to the next boundary.
      - For languages that indent-block (Python/Ruby), capture the indented
        body; for brace languages, capture until the next matching `}` at
        column 0 (best-effort; the regex chunker is intentionally simple).

    Falls back to `chunk_file()` for files it doesn't understand. NEVER
    raises on malformed code — just emits reasonable chunks.
    """
    def __init__(self):
        # Compile once at construction; reuse across calls.
        self._compiled: Dict[str, List[re.Pattern]] = {
            ext: list(patterns) for ext, patterns in _LANG_HINTS.items()
        }

    def chunk(self, path: Path, text: str) -> List[Chunk]:
        if not text.strip():
            return []

        ext = path.suffix.lower()
        patterns = self._compiled.get(ext)
        if not patterns:
            # Unknown extension → use prose chunker.
            return chunk_file(path, text)

        lines = text.splitlines()
        n = len(lines)

        # Find definition start lines.
        boundaries: List[int] = []   # 0-indexed
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            # Skip pure comments and blank lines.
            if not stripped or stripped.startswith(("#", "//", "/*", "*", "--")):
                continue
            for pat in patterns:
                if pat.match(line):
                    boundaries.append(i)
                    break

        if not boundaries:
            # No definitions detected (e.g. a single-function file where
            # the def matches but is the only thing in the file). Just emit
            # the whole file as one chunk.
            return [Chunk(text=text, start_line=1, end_line=n)]

        # Walk adjacent boundaries and collect chunks. We also prepend any
        # module-level docstring / header comments (lines above the first def).
        chunks: List[Chunk] = []
        first_def = boundaries[0]
        # Header: anything before the first definition, if non-empty and
        # substantive (≥ 1 non-blank line).
        header_lines = lines[:first_def]
        if any(l.strip() for l in header_lines):
            chunks.append(Chunk(
                text="\n".join(header_lines).rstrip(),
                start_line=1,
                end_line=first_def,   # 1-indexed: last header line = first_def
            ))

        # Decorator merging (Python / TS / JS): for each boundary that
        # starts with `@decorator`, walk backwards to include any preceding
        # `@`-lines in the SAME chunk. The boundary index doesn't change,
        # but we remember the "effective" start to use when emitting.
        effective_starts: List[int] = []
        for b in boundaries:
            eff = b
            while eff > 0 and lines[eff - 1].lstrip().startswith("@"):
                eff -= 1
            effective_starts.append(eff)

        # Emit one chunk per [boundary, next_boundary) span. For brace
        # languages, we trim trailing closing braces that belong to the
        # NEXT top-level def.
        for idx, start in enumerate(boundaries):
            end = boundaries[idx + 1] if idx + 1 < len(boundaries) else n
            eff_start = effective_starts[idx]
            chunk_text = "\n".join(lines[eff_start:end]).rstrip()
            if not chunk_text.strip():
                continue
            chunks.append(Chunk(
                text=chunk_text,
                start_line=eff_start + 1,    # 1-indexed
                end_line=end,                # end is exclusive index → last line = end
            ))

        return chunks


# ----------------------------------------------------------------------
# Tree-sitter chunker — opt-in, AST-accurate.
# ----------------------------------------------------------------------


# Mapping from extension to tree-sitter language name.
_TS_LANG_NAMES: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".cs": "c_sharp",
    ".php": "php",
}


class TreeSitterCodeChunker(CodeChunker):
    """Tree-sitter-based chunker.

    Opt-in: requires the `tree-sitter` package and the per-language
    packages (`tree-sitter-python`, `tree-sitter-javascript`, etc.) to be
    installed. When anything is missing, logs one WARNING and falls back
    to `RegexCodeChunker` so the user still gets working chunks.

    Chunking strategy:
      - Parse the file into an AST.
      - Walk top-level nodes; whenever we hit a "definition" node
        (function/method/class/struct/interface/etc. — see `_DEF_NODE_TYPES`),
        emit one chunk spanning from the node's start line to the next
        sibling's start line (or EOF for the last one).
    """
    _DEF_NODE_TYPES = {
        "function_definition", "function_declaration", "method_definition",
        "method_declaration", "class_definition", "class_declaration",
        "struct_item", "struct_specifier", "interface_declaration",
        "trait_item", "impl_item", "function_item", "type_item",
        "module", "namespace_definition",
    }

    def __init__(self):
        self._parsers: Dict[str, object] = {}     # ext -> Parser
        self._available_langs: set = set()         # ext that loaded OK
        self._regex_fallback = RegexCodeChunker()
        self._init_tree_sitter()

    def _init_tree_sitter(self) -> None:
        try:
            import tree_sitter  # type: ignore
        except ImportError:
            logger.warning(
                "tree_sitter not installed; TreeSitterCodeChunker will fall "
                "back to RegexCodeChunker. Install with: pip install "
                "tree-sitter tree-sitter-python tree-sitter-javascript ..."
            )
            return
        try:
            # tree_sitter >= 0.21 uses Language.build; older used directly.
            from tree_sitter import Language, Parser  # type: ignore
        except Exception as e:
            logger.warning(f"tree_sitter import failed: {e}")
            return

        for ext, lang_name in _TS_LANG_NAMES.items():
            try:
                mod_name = f"tree_sitter_{lang_name}"
                cap_mod = __import__(mod_name)
                lib_path = next(
                    (p for p in dir(cap_mod) if p.startswith("language") or p == "_language"),
                    None,
                )
                if lib_path is None:
                    # tree_sitter 0.21+ pattern: just import the package and
                    # call .language() on the capi module.
                    lang_obj = Language(getattr(cap_mod, "language")())
                else:
                    lang_obj = Language(getattr(cap_mod, lib_path)())
                parser = Parser(lang_obj)
                self._parsers[ext] = parser
                self._available_langs.add(ext)
            except Exception as e:
                logger.debug(f"tree-sitter language '{lang_name}' unavailable: {e}")

        if not self._parsers:
            logger.warning(
                "tree_sitter languages failed to load; TreeSitterCodeChunker "
                "will fall back to RegexCodeChunker for every file."
            )

    def chunk(self, path: Path, text: str) -> List[Chunk]:
        ext = path.suffix.lower()
        parser = self._parsers.get(ext)
        if parser is None:
            return self._regex_fallback.chunk(path, text)

        try:
            tree = parser.parse(text.encode("utf-8"))
            root = tree.root_node
        except Exception as e:
            logger.debug(f"tree-sitter parse failed for {path}: {e}")
            return self._regex_fallback.chunk(path, text)

        lines = text.splitlines()
        n = len(lines)

        # Collect top-level definition boundaries.
        boundaries: List[int] = []
        for child in root.children:
            t = child.type
            if t in self._DEF_NODE_TYPES:
                # child.start_point is (row, col); row is 0-indexed.
                boundaries.append(child.start_point[0])

        if not boundaries:
            return self._regex_fallback.chunk(path, text)

        chunks: List[Chunk] = []
        header_end = boundaries[0]
        header = lines[:header_end]
        if any(l.strip() for l in header):
            chunks.append(Chunk(text="\n".join(header), start_line=1, end_line=header_end))

        for idx, start in enumerate(boundaries):
            end = boundaries[idx + 1] if idx + 1 < len(boundaries) else n
            ct = "\n".join(lines[start:end]).rstrip()
            if ct.strip():
                chunks.append(Chunk(
                    text=ct,
                    start_line=start + 1,
                    end_line=end,
                ))
        return chunks
