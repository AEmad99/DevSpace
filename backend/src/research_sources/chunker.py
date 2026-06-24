"""Text chunker for local research sources (FolderSource, CodebaseSource).

Splits a file's text into overlapping chunks with accurate line numbers so
we can cite `file://path#Lstart-Lend` in the report. Language-agnostic
(intentionally — language-aware chunking lives in `code_chunker.py` for
M3's `CodebaseSource`).

Strategy:
  - Split on blank-line boundaries (paragraphs for prose).
  - If a paragraph exceeds `max_chars`, split on single line boundaries.
  - Emit with `overlap` so cross-boundary context survives.
  - Always preserve the original line numbers (1-indexed) so citations are
    exact even after re-chunking the same file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Chunk:
    """A single text chunk with source-line attribution.

    `start_line` and `end_line` are 1-indexed inclusive line numbers into
    the original file. `text` is the chunk's content WITHOUT line numbers
    (the LLM doesn't want them; we add them only when synthesizing citations).
    """
    text: str
    start_line: int
    end_line: int


def chunk_file(
    path: Path,
    text: Optional[str] = None,
    *,
    max_chars: int = 1500,
    overlap: int = 200,
) -> List[Chunk]:
    """Split `path`'s content (or pre-read `text`) into overlapping chunks.

    `max_chars` is a soft target — a single chunk may be a bit smaller
    (we never split mid-paragraph unless necessary). `overlap` is the
    number of characters carried over from the previous chunk's tail to
    preserve cross-boundary context (default 200).

    Returns at least one Chunk for any non-empty input. An empty file
    returns [].
    """
    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

    if not text:
        return []

    lines = text.splitlines(keepends=False)
    if not lines:
        return []

    # Build (line_no, line_text) pairs so we can attribute line ranges.
    line_pairs = [(i + 1, line) for i, line in enumerate(lines)]

    chunks: List[Chunk] = []
    cursor = 0   # index into line_pairs for the start of the next chunk
    n = len(line_pairs)

    while cursor < n:
        # Collect lines until we cross max_chars (counting newlines too).
        end = cursor
        char_count = 0
        while end < n and char_count + len(line_pairs[end][1]) + 1 <= max_chars:
            char_count += len(line_pairs[end][1]) + 1
            end += 1

        if end == cursor:
            # A single line exceeds max_chars — emit it on its own rather
            # than loop forever. The downstream consumer truncates anyway.
            end = cursor + 1

        # Prefer paragraph boundaries: if the chunk ends mid-paragraph and
        # the next line is blank, we trim back to that blank so chunks are
        # semantic paragraphs when possible.
        end = _trim_to_paragraph_break(line_pairs, cursor, end)

        chunk_text = "\n".join(t for _, t in line_pairs[cursor:end])
        start_line = line_pairs[cursor][0]
        end_line = line_pairs[end - 1][0]
        if chunk_text.strip():
            chunks.append(Chunk(text=chunk_text, start_line=start_line, end_line=end_line))

        if end >= n:
            break

        # Advance cursor, applying overlap. Overlap counts back from `end`
        # in characters; we step back at most `overlap` chars but at least
        # one line so we always make progress.
        next_cursor = end
        back_chars = 0
        while next_cursor > cursor + 1 and back_chars < overlap:
            next_cursor -= 1
            back_chars += len(line_pairs[next_cursor][1]) + 1
        cursor = max(cursor + 1, next_cursor)

    return chunks


def _trim_to_paragraph_break(line_pairs, start: int, end: int) -> int:
    """Trim `end` back to the nearest blank-line boundary inside [start+1, end).

    A "paragraph break" is a blank line — we want to end chunks at blank
    lines when possible. If there is no blank line, return `end` unchanged.
    """
    for i in range(end - 1, start, -1):
        if not line_pairs[i][1].strip():
            return i
    return end
