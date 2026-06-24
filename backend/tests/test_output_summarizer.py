"""Tests for the bash / python output summarizer.

Long tool outputs (a 60-second pip install, a verbose pytest run, a large
build) are the most common case where the model loses the *actual* error
— the head-only truncate drops the tail, and the tail is where the failure
lives. The summarizer keeps the head, the tail, and any "interesting" line
in the middle (errors / warnings / tracebacks)."""
import pytest

from src.output_summarizer import (
    SUMMARIZE_THRESHOLD_CHARS,
    HEAD_LINES,
    TAIL_LINES,
    summarize_output,
)


# ── short inputs are unchanged ────────────────────────────────────────────

def test_empty():
    out, stats = summarize_output("")
    assert out == ""
    assert stats["summarized"] is False


def test_short_output_unchanged():
    text = "line1\nline2\nline3"
    out, stats = summarize_output(text)
    assert out == text
    assert stats["summarized"] is False
    assert stats["reason"] == "short"


def test_output_at_threshold_unchanged():
    # Exactly threshold chars: should still be passed through.
    text = "x" * SUMMARIZE_THRESHOLD_CHARS
    out, stats = summarize_output(text)
    assert out == text
    assert stats["summarized"] is False


# ── long inputs are summarized ────────────────────────────────────────────

def test_long_output_is_summarized():
    # 600 lines × ~80 chars/line ≈ 48 KB — well over the 10K threshold.
    body = "\n".join(f"noise line {i} — {'x' * 60}" for i in range(600))
    out, stats = summarize_output(body)
    assert stats["summarized"] is True
    assert stats["reason"] == "long"
    # Must be shorter than the input (and by a healthy margin).
    assert len(out) < len(body) // 2
    # Head: first HEAD_LINES must survive.
    for i in range(HEAD_LINES):
        assert f"noise line {i}" in out
    # Tail: last TAIL_LINES must survive.
    for i in range(600 - TAIL_LINES, 600):
        assert f"noise line {i}" in out
    # Some indicator that the middle was dropped.
    assert "omitted" in out


def test_long_output_keeps_error_lines_from_middle():
    """A 5 MB pip install with one real ImportError somewhere in the
    middle: the summarizer must keep that line, or the model has no idea
    what went wrong."""
    lines = [f"line {i} — {'x' * 60}" for i in range(600)]
    lines[300] = "ModuleNotFoundError: No module named 'totally_real_pkg'"
    body = "\n".join(lines)
    out, stats = summarize_output(body)
    assert "ModuleNotFoundError" in out, "the error line must survive"
    assert "No module named" in out
    assert stats["kept_interesting_middle"] >= 1


def test_long_output_keeps_traceback_from_middle():
    lines = [f"line {i} — {'x' * 60}" for i in range(600)]
    lines[200] = "Traceback (most recent call last):"
    lines[201] = "  File '/srv/app/x.py', line 42 in main"
    lines[202] = "    raise ValueError('boom')"
    body = "\n".join(lines)
    out, stats = summarize_output(body)
    assert "Traceback" in out
    assert "ValueError" in out


def test_long_output_keeps_pytest_failure_marker():
    lines = [f"line {i} — {'x' * 60}" for i in range(600)]
    lines[350] = "FAILED tests/test_foo.py::test_bar - AssertionError"
    body = "\n".join(lines)
    out, stats = summarize_output(body)
    assert "FAILED" in out or "AssertionError" in out


def test_long_output_caps_interesting_middle():
    """A noisy `pip install` that flags every line as 'warning' must not
    drown out the head + tail."""
    body = "\n".join(["WARNING: skipping dep — " + "x" * 70] * 600)
    out, stats = summarize_output(body)
    # The interesting-middle cap protects the head + tail.
    assert stats["kept_interesting_middle"] <= 80
    # Head and tail should still be intact.
    for i in range(HEAD_LINES):
        assert "WARNING" in out
    # The summary marker should be present.
    assert "omitted" in out


def test_long_output_with_no_interesting_lines():
    """A `cat /var/log/syslog`-style run with thousands of 'INFO' lines —
    no errors, no warnings. The model doesn't need any of it; just the
    head + tail (so the user can see 'tail of log' context) is enough."""
    body = "\n".join(["INFO: routine update — " + "x" * 70] * 600)
    out, stats = summarize_output(body)
    assert stats["kept_interesting_middle"] == 0
    assert "omitted" in out


def test_custom_threshold():
    """Callers can lower the threshold for tests / unit tests."""
    text = "x" * 200
    out, stats = summarize_output(text, threshold=100)
    # 200 > 100 → summarized (if the line count is high enough).
    # We need > HEAD+TAIL lines to actually trigger; with 200 chars on
    # one line, it's still under HEAD+TAIL lines so stats.reason is 'few_lines'.
    assert stats["summarized"] is False
    # Bump to many lines.
    body = "\n".join(["x" * 80] * 200)
    out, stats = summarize_output(body, threshold=100)
    assert stats["summarized"] is True


def test_summary_is_pure():
    """The summarizer must not mutate its input."""
    body = "\n".join(f"line {i} — {'x' * 70}" for i in range(600))
    snapshot = body
    out, _ = summarize_output(body)
    assert body == snapshot  # unchanged
    assert out != body        # and the output IS different
