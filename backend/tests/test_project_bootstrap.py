"""Tests for the project_bootstrap tool.

A one-call orientation for a workspace: project type, tooling, conventions,
and any opencode / Claude-Code instruction files. Cached per-workspace.
"""
import json
import os
import time

import pytest

from src.tool_execution import _active_workspace
from src.agent_tools.project_tools import (
    ProjectBootstrapTool,
    project_bootstrap,
    project_bootstrap_cached,
    invalidate_bootstrap_cache,
)


# ── python project ────────────────────────────────────────────────────────

def test_python_project_with_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest]\naddopts = '-ra'\n\n[tool.ruff]\nline-length = 100\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("# entry\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("# tests\n")
    res = project_bootstrap(str(tmp_path))
    assert res["exit_code"] == 0
    assert res["type"] == "python"
    assert res["language"] == "python"
    assert "pytest" in res["test_runner"]
    assert "ruff" in res["lint_command"]
    # The src/main.py file should appear somewhere in entry_points.
    assert any("main.py" in ep for ep in res["entry_points"])


def test_python_project_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'x'\n")
    (tmp_path / "poetry.lock").write_text("")
    res = project_bootstrap(str(tmp_path))
    assert res["package_manager"] == "poetry"


def test_python_project_uv(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "uv.lock").write_text("")
    res = project_bootstrap(str(tmp_path))
    assert res["package_manager"] == "uv"


def test_python_project_with_agents_md(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "AGENTS.md").write_text("# Project rules\n\nAlways run pytest before committing.\n")
    res = project_bootstrap(str(tmp_path))
    found = {f["name"] for f in res["instructions_files"]}
    assert "AGENTS.md" in found
    preview = next(f for f in res["instructions_files"] if f["name"] == "AGENTS.md")
    assert "pytest" in preview["preview"]


def test_python_project_with_claude_md(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "CLAUDE.md").write_text("# Claude notes\nUse the dev compose file.\n")
    res = project_bootstrap(str(tmp_path))
    found = {f["name"] for f in res["instructions_files"]}
    assert "CLAUDE.md" in found


def test_python_conventions_detect_src_layout(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.py").write_text("")
    res = project_bootstrap(str(tmp_path))
    joined = " ".join(res["conventions"])
    assert "src/" in joined


# ── node project ──────────────────────────────────────────────────────────

def test_node_project_with_pnpm(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "scripts": {"test": "jest", "lint": "eslint ."},
    }))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    res = project_bootstrap(str(tmp_path))
    assert res["type"] == "node"
    assert res["package_manager"] == "pnpm"
    # test_runner is the invocation (e.g. "pnpm test"), not the underlying
    # script command — the model wraps it with a target arg as needed.
    assert "test" in res["test_runner"]
    assert "lint" in res["lint_command"]


def test_node_project_with_yarn(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": "mocha"}}))
    (tmp_path / "yarn.lock").write_text("")
    res = project_bootstrap(str(tmp_path))
    assert res["package_manager"] == "yarn"


# ── other languages ──────────────────────────────────────────────────────

def test_go_project(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n")
    (tmp_path / "main.go").write_text("package main\n")
    res = project_bootstrap(str(tmp_path))
    assert res["type"] == "go"
    assert res["test_runner"] == "go test ./..."
    assert "main.go" in res["entry_points"]


def test_rust_project(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    res = project_bootstrap(str(tmp_path))
    assert res["type"] == "rust"
    assert res["test_runner"] == "cargo test"


def test_java_maven_project(tmp_path):
    (tmp_path / "pom.xml").write_text("<project></project>\n")
    res = project_bootstrap(str(tmp_path))
    assert res["type"] == "java"
    assert res["package_manager"] == "maven"


# ── generic / no manifest ────────────────────────────────────────────────

def test_generic_workspace(tmp_path):
    # No manifests, no code files.
    (tmp_path / "stuff.txt").write_text("random")
    res = project_bootstrap(str(tmp_path))
    assert res["type"] == "generic"
    assert res["test_runner"] == ""
    assert res["lint_command"] == ""


def test_workspace_with_dockerfile(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    res = project_bootstrap(str(tmp_path))
    joined = " ".join(res["conventions"])
    assert "Dockerfile" in joined or "containerized" in joined.lower()


def test_workspace_with_makefile(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "Makefile").write_text("help:\n\t@echo help\n")
    res = project_bootstrap(str(tmp_path))
    joined = " ".join(res["conventions"])
    assert "Makefile" in joined


# ── key files list ───────────────────────────────────────────────────────

def test_key_files_includes_manifests_and_entry_points(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "Dockerfile").write_text("")
    (tmp_path / "main.py").write_text("")
    res = project_bootstrap(str(tmp_path))
    assert "pyproject.toml" in res["key_files"]
    assert "Dockerfile" in res["key_files"]
    assert "main.py" in res["key_files"]


# ── caching ──────────────────────────────────────────────────────────────

def test_bootstrap_is_cached(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    invalidate_bootstrap_cache()  # start clean
    r1 = project_bootstrap_cached(str(tmp_path))
    r2 = project_bootstrap_cached(str(tmp_path))
    # Same workspace, same mtime → same dict object (cached).
    assert r1 is r2


def test_bootstrap_cache_invalidates_on_signature_change(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    invalidate_bootstrap_cache()
    r1 = project_bootstrap_cached(str(tmp_path))
    # Bump mtime on a signature file (mtime resolution is ~1s on some FS).
    time.sleep(1.1)
    p = tmp_path / "pyproject.toml"
    p.write_text("[project]\nname = 'changed'\n")
    r2 = project_bootstrap_cached(str(tmp_path))
    assert r1 is not r2


def test_bootstrap_cache_can_be_manually_invalidated(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    invalidate_bootstrap_cache()
    r1 = project_bootstrap_cached(str(tmp_path))
    invalidate_bootstrap_cache(str(tmp_path))
    r2 = project_bootstrap_cached(str(tmp_path))
    assert r1 is not r2


# ── no workspace ──────────────────────────────────────────────────────────

def test_bootstrap_without_workspace_returns_error():
    res = project_bootstrap("")
    assert res["exit_code"] == 1
    assert "no workspace" in res["error"]


def test_bootstrap_with_nonexistent_path_returns_error():
    res = project_bootstrap("/nonexistent/path/that/does/not/exist")
    assert res["exit_code"] == 1


# ── tool wrapper integration ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_uses_active_workspace():
    (tmp_path := os.path.join(os.environ.get("TEMP", "/tmp"), "odysseus-bootstrap-test-XXXX"))
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # Set the active workspace via contextvar.
        from src.tool_execution import _active_workspace as _aw
        prev = _aw.get()
        _aw.set(td)
        try:
            # Create a project under the temp dir.
            with open(os.path.join(td, "pyproject.toml"), "w") as f:
                f.write("[project]\nname = 'x'\n")
            res = await ProjectBootstrapTool().execute("{}", {})
            assert res["exit_code"] == 0
            assert res["type"] == "python"
        finally:
            _aw.set(prev)


@pytest.mark.asyncio
async def test_tool_without_workspace_returns_error():
    from src.tool_execution import _active_workspace as _aw
    prev = _aw.get()
    _aw.set(None)
    try:
        res = await ProjectBootstrapTool().execute("{}", {})
        assert res["exit_code"] == 1
        assert "no workspace" in res["error"]
    finally:
        _aw.set(prev)
