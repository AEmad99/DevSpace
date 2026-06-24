"""Tests for /api/skills/generate — the AI skill-creator endpoint.

Covers the parts that don't require a live LLM:

  * ``_extract_skill_md`` tolerates the common shapes a model can reply with
    (raw markdown, prose + markdown, fenced ```markdown``` block, stray
     think  blocks).
  * POST /api/skills/generate parses the model output, drops a SKILL.md on
    disk under the configured category, and exposes it through the
    SkillsManager on the next load (so /api/skills + the slash catalog see
    the new skill immediately).

The LLM call itself is mocked — only the wire-up between the parser, the
SkillsManager, and disk is exercised here. Live end-to-end with a real
endpoint is a separate manual smoke test.
"""
import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request
from fastapi.datastructures import State

from routes.skills_routes import (
    SkillGenerateRequest,
    _extract_skill_md,
    setup_skills_routes,
)
from services.memory.skill_format import slugify
from services.memory.skills import SkillsManager


# ── Helpers ────────────────────────────────────────────────────────

SAMPLE_SKILL_MD = textwrap.dedent("""\
    ---
    name: tidy-mixed-files
    description: Sort a mixed directory into subfolders by file type.
    version: 1.0.0
    category: filesystem
    tags: [files, organization]
    status: draft
    confidence: 0.7
    source: learned
    ---

    ## When to Use
    When the user asks to tidy a messy folder with mixed file types.

    ## Procedure
    1. List the folder contents.
    2. Group by extension.
    3. Move each file into a category-named subfolder.
    4. Report what was moved.

    ## Pitfalls
    - Don't recurse into subfolders the user didn't ask about.
    - Never move hidden files without asking.

    ## Verification
    - Re-list the folder and confirm every file now lives in a subfolder.
""")


def _request(user, body):
    class _App:
        state = State()

    async def _receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request(scope={
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
        "app": _App(),
        "state": {"current_user": user},
    }, receive=_receive)


def _handler(router, path, method):
    return next(r.endpoint for r in router.routes
                if r.path == path and method in r.methods)


# ── _extract_skill_md ──────────────────────────────────────────────


def test_extract_skill_md_accepts_raw():
    assert _extract_skill_md(SAMPLE_SKILL_MD) == SAMPLE_SKILL_MD.strip()


def test_extract_skill_md_strips_think_blocks():
    wrapped = (
        "Let me think about this carefully...\n"
        "```\nSome scratch reasoning here.\n```\n"
        "<think>still thinking</think>\n"
        + SAMPLE_SKILL_MD
    )
    out = _extract_skill_md(wrapped)
    assert out is not None
    assert out.startswith("---")
    assert "name: tidy-mixed-files" in out
    # The think block must not have leaked into the saved body.
    assert "still thinking" not in out


def test_extract_skill_md_keeps_last_doc_when_prose_prepends():
    wrapped = "Sure! Here is the SKILL.md you asked for:\n\n" + SAMPLE_SKILL_MD + "\n\nLet me know if you'd like any changes."
    out = _extract_skill_md(wrapped)
    assert out is not None
    assert out.startswith("---")
    assert out.rstrip().endswith(
        "Re-list the folder and confirm every file now lives in a subfolder."
    )
    assert "Let me know" not in out


def test_extract_skill_md_strips_outer_fence():
    # Some models still wrap the whole thing in a markdown fence.
    wrapped = "```markdown\n" + SAMPLE_SKILL_MD + "\n```"
    out = _extract_skill_md(wrapped)
    assert out is not None
    assert out.startswith("---")
    assert "```" not in out


def test_extract_skill_md_returns_none_when_no_frontmatter():
    assert _extract_skill_md("just some prose, no frontmatter here") is None
    assert _extract_skill_md("") is None
    assert _extract_skill_md(None) is None


# ── POST /api/skills/generate ──────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_skill_saves_to_disk_and_returns_skill(tmp_path):
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    handler = _handler(router, "/api/skills/generate", "POST")

    with patch("routes.skills_routes._resolve_generate_models",
               return_value=("http://example.invalid/v1", "test-model", {})), \
         patch("src.llm_core.llm_call_async",
               new=AsyncMock(return_value=SAMPLE_SKILL_MD)):
        result = await handler(
            _request("alice", {"description": "Sort files into subfolders by type",
                               "category": "filesystem"}),
            SkillGenerateRequest(**{"description": "Sort files into subfolders by type",
                                    "category": "filesystem"}),
        )

    assert result["ok"] is True
    skill = result["skill"]
    assert skill["name"] == "tidy-mixed-files"
    assert skill["status"] == "published"
    assert skill["source"] == "user"
    assert skill["owner"] == "alice"
    assert skill["category"] == "filesystem"
    assert result["command"] == "/tidy-mixed-files"
    assert result["markdown"].startswith("---")

    # On-disk shape: <root>/skills/<category>/<name>/SKILL.md
    skill_path = tmp_path / "skills" / slugify("filesystem") / "tidy-mixed-files" / "SKILL.md"
    assert skill_path.is_file(), f"expected {skill_path} to exist"
    on_disk = skill_path.read_text(encoding="utf-8")
    assert "name: tidy-mixed-files" in on_disk
    assert "status: published" in on_disk
    assert "owner: alice" in on_disk
    # The body should round-trip through the SkillsManager.
    loaded = sm.load(owner="alice")
    names = [s.get("name") for s in loaded]
    assert "tidy-mixed-files" in names
    # And the slash catalog (index_for) should see it as published —
    # that's what lets /<skill-name> work right away in the chat UI.
    catalog = sm.index_for(owner="alice")
    assert any(s.get("name") == "tidy-mixed-files" for s in catalog)


@pytest.mark.asyncio
async def test_generate_skill_returns_400_when_no_model_configured(tmp_path):
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    handler = _handler(router, "/api/skills/generate", "POST")

    with patch("routes.skills_routes._resolve_generate_models",
               side_effect=ValueError("No model configured — set a Default or Utility model.")):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await handler(
                _request("alice", {"description": "anything", "category": "general"}),
                SkillGenerateRequest(**{"description": "anything", "category": "general"}),
            )
    assert ei.value.status_code == 400
    assert "No model configured" in ei.value.detail


@pytest.mark.asyncio
async def test_generate_skill_returns_502_when_model_output_unparseable(tmp_path):
    sm = SkillsManager(str(tmp_path))
    router = setup_skills_routes(sm)
    handler = _handler(router, "/api/skills/generate", "POST")

    with patch("routes.skills_routes._resolve_generate_models",
               return_value=("http://example.invalid/v1", "test-model", {})), \
         patch("src.llm_core.llm_call_async",
               new=AsyncMock(return_value="sorry, I can't write that")):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await handler(
                _request("alice", {"description": "anything"}),
                SkillGenerateRequest(**{"description": "anything"}),
            )
    assert ei.value.status_code == 502
    assert "SKILL.md" in ei.value.detail

    # Nothing should have been written to disk if the parser rejected the model.
    assert not (tmp_path / "skills").exists() or not any(
        (tmp_path / "skills").rglob("SKILL.md")
    )
