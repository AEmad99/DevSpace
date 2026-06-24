"""Output summarizer for long bash / python tool results.

Without this, a 60-second pip install can dump 5 MB of output and the model
sees only the first 10 KB — losing the actual error at the end. With this,
when the output exceeds SUMMARIZE_THRESHOLD_CHARS we keep:

  1. The first HEAD_LINES lines (early context: command echoes, banners).
  2. The last TAIL_LINES lines (the final result / actual error / exit status).
  3. Any line that matches an "interesting" pattern (error / exception /
     warning / traceback / fail / panic / deprecated / etc.) anywhere in
     the middle — these are the gold and dropping them turns a useful
     failure into a silent "exit code 1" the model has to guess about.
  4. A one-line "... N middle lines omitted" marker between head and tail.

The result is then truncated to the standard tool-output cap. The point
isn't to be pretty — it's to make sure the model still sees (a) what went
wrong and (b) the last few lines of context, even on a 100 MB compile.

The summarizer is a pure function over text. It's a soft transform — it
never throws, never mutates the input, and falls back to the head-only
truncate when the input is so noisy that no summary is better than a
losing one (e.g. every line is a warning).
"""
import re
from typing import Tuple

# Soft cap (in chars) above which we start summarizing instead of head-only
# truncation. Below this the model sees the full output — useful for normal
# commands. The hard cap (MAX_OUTPUT_CHARS) is applied AFTER summarization
# by the tool's own _truncate, so even pathological output stays bounded.
SUMMARIZE_THRESHOLD_CHARS = 10_000

# How many lines to keep at the head/tail of a long output. The tail matters
# more than the head because errors almost always live at the end of a
# failing install / build / test run.
HEAD_LINES = 30
TAIL_LINES = 60

# Patterns whose line should be PRESERVED in the middle even when it would
# otherwise be dropped. These are the lines the model actually needs to see
# to debug — "ModuleNotFoundError: No module named 'X'", "ERROR: failed
# building wheel", "FAIL tests/test_foo.py::test_bar", etc. Case-insensitive
# substring match (the regex anchors are loose; this is a heuristic).
#
# Loose anchors: "error" without `\b` so it also matches inside camelcase
# identifiers like ValueError / TypeError / RuntimeError. "warning" similarly
# catches the cargo / pip / eslint "warning:" line shapes.
_INTERESTING_RE = re.compile(
    r"(error|errors|err\b|exception|traceback|failed|fail\b|"
    r"fatal|panic|warning|deprecated|"
    r"assertion|assert\b|"
    r"\bE\s+\b|\bF\s+\b|\bW\s+\b)"  # pytest E / F / W markers
    r"|"
    r"(npm err|error\[|error:|err!|"
    r"errno|segfault|abort\(\))",
    re.IGNORECASE,
)

# Cap the number of "interesting" middle lines we keep, even if every line
# matches. Without this, a `pip install` that prints "WARNING: ignoring" on
# every dep would dominate the summary and push out the real failure.
MAX_INTERESTING_MIDDLE = 80


def summarize_output(text: str, *, threshold: int = SUMMARIZE_THRESHOLD_CHARS) -> Tuple[str, dict]:
    """If `text` is short, return it unchanged. Otherwise return a
    head + interesting-middle + tail view, plus a stats dict describing
    what was kept / dropped (useful for the metric event).

    Pure function; never raises. Empty / whitespace-only input is a no-op."""
    if not text:
        return text, {"summarized": False, "reason": "empty"}

    if len(text) <= threshold:
        return text, {"summarized": False, "reason": "short", "len": len(text)}

    lines = text.splitlines()
    total_lines = len(lines)

    # Cheap path: the line count is small enough that head + tail already
    # cover everything. No need to scan for "interesting" lines.
    if total_lines <= HEAD_LINES + TAIL_LINES:
        return text, {
            "summarized": False,
            "reason": "few_lines",
            "len": len(text),
            "lines": total_lines,
        }

    head = lines[:HEAD_LINES]
    tail = lines[-TAIL_LINES:]
    # Lines that fall in the gap (strictly after head, strictly before tail).
    gap = lines[HEAD_LINES:total_lines - TAIL_LINES]

    interesting: list[str] = []
    for ln in gap:
        if len(interesting) >= MAX_INTERESTING_MIDDLE:
            break
        if _INTERESTING_RE.search(ln):
            interesting.append(ln)

    omitted = len(gap) - len(interesting)
    parts: list[str] = []
    parts.extend(head)
    if interesting:
        parts.append(f"\n… [{omitted} middle lines omitted; {len(interesting)} "
                     f"error/warning lines kept]\n")
        parts.extend(interesting)
    elif omitted > 0:
        parts.append(f"\n… [{omitted} middle lines omitted]\n")
    parts.extend(tail)

    summary = "\n".join(parts)
    return summary, {
        "summarized": True,
        "reason": "long",
        "len": len(text),
        "lines": total_lines,
        "kept_head": len(head),
        "kept_tail": len(tail),
        "kept_interesting_middle": len(interesting),
        "omitted_middle": omitted,
    }
