"""Code-quality tools — run_tests, lint, format.

Each tool auto-detects the right command for the active workspace (from manifest
files / config) and runs it through the shell with cwd=agent_cwd() — the same
posture as the `bash` tool (owner-authed, single-user, not sandboxed). Output is
streamed via the shared subprocess helper and truncated like every other tool.

Detection is best-effort: if nothing is recognised the tool returns a clear
error telling the model to fall back to `bash`. The model may pass an optional
`target` (a path or test-node filter); shell metacharacters in it are rejected.

Handler signature matches every other tool: async execute(content, ctx) -> dict.
"""
import json
import os

from src.constants import MAX_OUTPUT_CHARS
from .subprocess_tools import _run_subprocess_streaming, spawn_shell

# Tests can be slow; linters/formatters less so.
TEST_TIMEOUT = 30 * 60
QUALITY_TIMEOUT = 10 * 60

# Shell metacharacters / control chars that must not appear in a `target` — they
# would let a malformed target chain commands. (The agent can still run anything
# via `bash`; this just keeps these convenience wrappers predictable.)
_BAD_TARGET_CHARS = set(";|&`$<>\n\r\"'")


# --- small fs helpers ------------------------------------------------------

def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _has(cwd: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(cwd, n)) for n in names)


def _has_py_files(cwd: str) -> bool:
    try:
        with os.scandir(cwd) as it:
            return any(e.is_file() and e.name.endswith(".py") for e in it)
    except OSError:
        return False


def _pkg_manager(cwd: str) -> str:
    if os.path.exists(os.path.join(cwd, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(cwd, "yarn.lock")):
        return "yarn"
    return "npm"


def _pkg_scripts(cwd: str):
    """Return package.json scripts dict, {} if no scripts, or None if no package.json."""
    p = os.path.join(cwd, "package.json")
    if not os.path.exists(p):
        return None
    try:
        return (json.loads(_read_text(p)) or {}).get("scripts") or {}
    except Exception:
        return {}


# --- input parsing ---------------------------------------------------------

def _parse_target(content: str):
    """Pull `target` from the tool args. Returns the cleaned string, or None if
    it contains disallowed characters (caller turns that into an error)."""
    data = {}
    if content and content.strip():
        try:
            data = json.loads(content)
        except Exception:
            data = {}
    target = data.get("target", "") if isinstance(data, dict) else (data if isinstance(data, str) else "")
    target = (target or "").strip()
    if not target:
        return ""
    if any(c in target for c in _BAD_TARGET_CHARS):
        return None
    return target


# --- detection -------------------------------------------------------------

def _detect_test_cmd(cwd: str, target: str):
    scripts = _pkg_scripts(cwd)
    if scripts is not None and "test" in scripts:
        return f"{_pkg_manager(cwd)} test"
    pyproject = _read_text(os.path.join(cwd, "pyproject.toml"))
    pytest_markers = (
        _has(cwd, "pytest.ini", "tox.ini", "conftest.py")
        or "[tool.pytest" in pyproject
        or "[pytest]" in _read_text(os.path.join(cwd, "setup.cfg"))
        or os.path.isdir(os.path.join(cwd, "tests"))
        or os.path.isdir(os.path.join(cwd, "test"))
    )
    if pytest_markers or (_has(cwd, "pyproject.toml", "setup.py") and _has_py_files(cwd)):
        return "python -m pytest" + (f" {target}" if target else "")
    if _has(cwd, "go.mod"):
        return "go test " + (target or "./...")
    if _has(cwd, "Cargo.toml"):
        return "cargo test" + (f" {target}" if target else "")
    return None


def _detect_lint_cmd(cwd: str, target: str):
    tgt = target or "."
    pyproject = _read_text(os.path.join(cwd, "pyproject.toml"))
    if _has(cwd, "ruff.toml", ".ruff.toml") or "[tool.ruff" in pyproject:
        return f"ruff check {tgt}"
    scripts = _pkg_scripts(cwd)
    if scripts is not None and "lint" in scripts:
        return f"{_pkg_manager(cwd)} run lint"
    if _has(cwd, ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
            ".eslintrc.yml", ".eslintrc.yaml", "eslint.config.js", "eslint.config.mjs"):
        return f"npx --no-install eslint {tgt}"
    if _has(cwd, ".flake8") or "[flake8]" in _read_text(os.path.join(cwd, "setup.cfg")):
        return f"flake8 {tgt}"
    if _has(cwd, "pyproject.toml", "setup.py", "setup.cfg") or _has_py_files(cwd):
        return f"ruff check {tgt}"  # fast, common default for Python
    return None


def _detect_format_cmd(cwd: str, target: str):
    tgt = target or "."
    pyproject = _read_text(os.path.join(cwd, "pyproject.toml"))
    if "[tool.black" in pyproject:
        return f"black {tgt}"
    if _has(cwd, ".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs",
            ".prettierrc.yaml", ".prettierrc.yml", "prettier.config.js", "prettier.config.cjs"):
        return f"npx --no-install prettier --write {tgt}"
    scripts = _pkg_scripts(cwd)
    if scripts is not None and "format" in scripts:
        return f"{_pkg_manager(cwd)} run format"
    if "[tool.ruff" in pyproject or _has(cwd, "ruff.toml", ".ruff.toml"):
        return f"ruff format {tgt}"
    if _has(cwd, "pyproject.toml", "setup.py", "setup.cfg") or _has_py_files(cwd):
        return f"black {tgt}"
    return None


# --- shared runner ---------------------------------------------------------

async def _run_cmd(cmd: str, cwd: str, ctx: dict, *, timeout: int) -> dict:
    from src.tool_execution import _truncate
    proc = await spawn_shell(cmd, env=ctx.get("subproc_env"), cwd=cwd)
    stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
        proc, timeout=timeout, progress_cb=ctx.get("progress_cb"),
    )
    if timed_out:
        return {
            "error": f"`{cmd}` timed out after {timeout}s — process killed.",
            "command": cmd, "exit_code": 124,
            "stdout": _truncate(stdout, MAX_OUTPUT_CHARS),
            "stderr": _truncate(stderr, MAX_OUTPUT_CHARS),
        }
    out = stdout.rstrip()
    err = stderr.rstrip()
    combined = (out + "\nSTDERR: " + err).strip() if err else out
    return {
        "command": cmd,
        "output": _truncate(combined, MAX_OUTPUT_CHARS) or "(no output)",
        "exit_code": rc if rc is not None else 0,
    }


async def _execute(detector, content: str, ctx: dict, *, timeout: int, what: str) -> dict:
    from src.tool_execution import agent_cwd
    target = _parse_target(content)
    if target is None:
        return {"error": "Invalid `target` — shell metacharacters/quotes/newlines are not allowed. "
                         "Use the `bash` tool for custom commands."}
    cwd = agent_cwd()
    if not cwd or not os.path.isdir(cwd):
        return {"error": f"No usable workspace directory ({cwd!r}). Set a workspace first."}
    cmd = detector(cwd, target)
    if not cmd:
        return {"error": f"Could not auto-detect a {what} for this workspace ({cwd}). "
                         f"Run the exact command via the `bash` tool instead."}
    return await _run_cmd(cmd, cwd, ctx, timeout=timeout)


class RunTestsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _execute(_detect_test_cmd, content, ctx, timeout=TEST_TIMEOUT, what="test runner")


class LintTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _execute(_detect_lint_cmd, content, ctx, timeout=QUALITY_TIMEOUT, what="linter")


class FormatTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        return await _execute(_detect_format_cmd, content, ctx, timeout=QUALITY_TIMEOUT, what="formatter")
