"""apply_patch — Codex-style multi-file patches with exact context matching."""

import os

import pytest

from src.agent_tools import ToolBlock
from src.tool_execution import execute_tool_block
from src.tool_parsing import parse_tool_blocks
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user", lambda owner: True
    )
    monkeypatch.setattr(
        "src.tool_execution._owner_is_admin", lambda owner: True
    )


@pytest.mark.asyncio
async def test_apply_patch_updates_file(tmp_path, admin):
    target = tmp_path / "hello.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: hello.txt
@@
 alpha
-beta
+gamma
*** End Patch
"""
    desc, result = await execute_tool_block(
        ToolBlock("apply_patch", patch), owner="a", workspace=str(tmp_path)
    )
    assert result.get("exit_code") == 0, result
    assert target.read_text(encoding="utf-8") == "alpha\ngamma\n"
    assert result.get("diff")


@pytest.mark.asyncio
async def test_apply_patch_rejects_ambiguous_context(tmp_path, admin):
    target = tmp_path / "dup.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: dup.txt
@@
-same
+other
*** End Patch
"""
    desc, result = await execute_tool_block(
        ToolBlock("apply_patch", patch), owner="a", workspace=str(tmp_path)
    )
    assert result.get("exit_code") == 1
    assert "matched" in (result.get("error") or "").lower()
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


@pytest.mark.asyncio
async def test_apply_patch_is_workspace_confined(tmp_path, admin):
    outside_dir = tmp_path.parent / "apply-patch-escape"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "escape.txt"
    outside.write_text("x\n", encoding="utf-8")
    patch = f"""*** Begin Patch
*** Update File: {outside}
@@
-x
+y
*** End Patch
"""
    desc, result = await execute_tool_block(
        ToolBlock("apply_patch", patch), owner="a", workspace=str(tmp_path)
    )
    assert result.get("exit_code") == 1
    assert "outside" in (result.get("error") or "").lower()
    assert outside.read_text(encoding="utf-8") == "x\n"


def test_apply_patch_schema_and_parser():
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "apply_patch" in names
    assert "manage_bg_jobs" in names
    block = function_call_to_tool_block("apply_patch", '{"patch_text": "*** Begin Patch\\n*** End Patch"}')
    assert block is not None
    assert block.tool_type == "apply_patch"
    blocks = parse_tool_blocks("```apply_patch\n*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch\n```")
    assert blocks and blocks[0].tool_type == "apply_patch"


def test_todowrite_fence_aliases_to_manage_todos():
    from src.tool_parsing import _TOOL_NAME_MAP
    assert _TOOL_NAME_MAP["todowrite"] == "manage_todos"
    block = function_call_to_tool_block(
        "todowrite",
        '{"todos": [{"content": "ship it", "status": "in_progress"}]}',
    )
    assert block is not None
    assert block.tool_type == "manage_todos"
