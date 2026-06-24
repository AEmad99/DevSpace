"""Project bootstrap tool.

A cheap one-shot that lets the agent orient itself in a workspace without
making 5+ separate `read_file` / `ls` / `glob` calls. Runs at the start of
a coding task (or when the user says "look at this project" / "what's in
here") and returns:

  {
    "type": "python" | "node" | "go" | "rust" | "java" | "ruby" | "generic",
    "language": "python" | ...,
    "package_manager": "npm" | "pnpm" | "yarn" | "pip" | "poetry" | ...,
    "test_runner": "pytest" | "npm test" | ...,
    "lint_command": "ruff check ." | "eslint ." | ...,
    "format_command": "black ." | "prettier --write ." | ...,
    "entry_points": ["src/main.py", "main.go", ...],
    "conventions": ["uses pytest fixtures", "ESLint with --max-warnings=0", ...],
    "instructions_files": [{"name": "AGENTS.md", "path": "...", "preview": "..."}],
    "key_files": ["package.json", "pyproject.toml", "src/index.ts", ...],
  }

The point isn't to be exhaustive — it's to give the model a fast "what
am I looking at" answer so its first real call (read a file, run a test)
is targeted, not exploratory.

Cached per workspace: the file watcher invalidates on AGENTS.md /
package.json / pyproject.toml / etc. changes. The cache lives in-process
(memory), scoped by the active workspace path.
"""
import json
import os
import re
from typing import Optional, Dict, Any, List


# Files we read to detect the project type. Order matters: more specific
# wins (e.g. pyproject.toml is more specific than setup.py).
_MANIFEST_HINTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "poetry.lock", "uv.lock", "pdm.lock",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
    "go.mod", "Cargo.toml", "Gemfile", "composer.json",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "deno.json", "deno.jsonc",
)

# Convention files the model should know about — both opencode / Claude
# Code convention (AGENTS.md / CLAUDE.md / .opencode/instructions.md) and
# language-specific readme files.
_INSTRUCTIONS_FILES = (
    "AGENTS.md", "CLAUDE.md", "CONVENTIONS.md", "CONTRIBUTING.md",
    ".opencode/instructions.md", ".cursor/rules",
)

# Cap how much we read from any single file to keep the bootstrap output
# bounded. The full file content is read by the agent on demand — this
# preview is just to surface "there's a CONTRIBUTING.md, here's a hint
# of what it says".
_MAX_PREVIEW_CHARS = 400


def _read_text(path: str, max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read(max_chars + 1)
        if len(data) > max_chars:
            data = data[:max_chars] + "…"
        return data
    except OSError:
        return ""


def _exists(cwd: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(cwd, n)) for n in names)


def _has_py_files(cwd: str) -> bool:
    try:
        with os.scandir(cwd) as it:
            return any(e.is_file() and e.name.endswith(".py") for e in it)
    except OSError:
        return False


# ── language / type detection ────────────────────────────────────────────

def _detect_type(cwd: str) -> Dict[str, str]:
    """Return {type, language, package_manager} from the workspace's
    manifest files. Falls through to 'generic' for the unusual case."""
    # Python
    if _exists(cwd, "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
               "poetry.lock", "uv.lock", "pdm.lock", "requirements.txt") \
            or _has_py_files(cwd):
        pkg = "pip"
        if _exists(cwd, "uv.lock"): pkg = "uv"
        elif _exists(cwd, "poetry.lock"): pkg = "poetry"
        elif _exists(cwd, "pdm.lock"): pkg = "pdm"
        elif _exists(cwd, "Pipfile"): pkg = "pipenv"
        return {"type": "python", "language": "python", "package_manager": pkg}

    # Node / TypeScript
    if _exists(cwd, "package.json"):
        pkg = "npm"
        if _exists(cwd, "pnpm-lock.yaml"): pkg = "pnpm"
        elif _exists(cwd, "yarn.lock"): pkg = "yarn"
        return {"type": "node", "language": "javascript", "package_manager": pkg}

    # Go
    if _exists(cwd, "go.mod"):
        return {"type": "go", "language": "go", "package_manager": "go modules"}

    # Rust
    if _exists(cwd, "Cargo.toml"):
        return {"type": "rust", "language": "rust", "package_manager": "cargo"}

    # Ruby
    if _exists(cwd, "Gemfile"):
        return {"type": "ruby", "language": "ruby", "package_manager": "bundler"}

    # PHP
    if _exists(cwd, "composer.json"):
        return {"type": "php", "language": "php", "package_manager": "composer"}

    # Java (Maven or Gradle)
    if _exists(cwd, "pom.xml"):
        return {"type": "java", "language": "java", "package_manager": "maven"}
    if _exists(cwd, "build.gradle", "build.gradle.kts"):
        return {"type": "java", "language": "kotlin" if _exists(cwd, "build.gradle.kts") else "java",
                "package_manager": "gradle"}

    # Deno
    if _exists(cwd, "deno.json", "deno.jsonc"):
        return {"type": "deno", "language": "typescript", "package_manager": "deno"}

    return {"type": "generic", "language": "unknown", "package_manager": ""}


def _detect_test_cmd(cwd: str, ptype: str) -> str:
    """Return the project's preferred test invocation, as a string. The
    agent can shell out to this directly or wrap it with a target arg."""
    # Python — prefer pytest; honour common tox invocations as a fallback.
    if ptype == "python":
        if _exists(cwd, "pytest.ini", "tox.ini", "conftest.py") or os.path.isdir(os.path.join(cwd, "tests")) \
                or os.path.isdir(os.path.join(cwd, "test")):
            return "python -m pytest"
        return "python -m pytest"
    if ptype == "node":
        scripts = _pkg_scripts(cwd) or {}
        if "test" in scripts:
            return f"{_pkg_manager(cwd)} test"
        return "npm test"
    if ptype == "go":
        return "go test ./..."
    if ptype == "rust":
        return "cargo test"
    if ptype == "ruby":
        return "bundle exec rspec" if _exists(cwd, ".rspec") else "bundle exec rake test"
    if ptype == "java":
        return "mvn test" if ptype == "java" and _exists(cwd, "pom.xml") else "gradle test"
    return ""


def _detect_lint_cmd(cwd: str, ptype: str) -> str:
    if ptype == "python":
        pyproject = _read_text(os.path.join(cwd, "pyproject.toml"), 4096)
        if _exists(cwd, "ruff.toml", ".ruff.toml") or "[tool.ruff" in pyproject:
            return "ruff check ."
        if _exists(cwd, ".flake8") or "[flake8]" in _read_text(os.path.join(cwd, "setup.cfg"), 4096):
            return "flake8 ."
        if "[tool.black" in pyproject:
            return "black --check ."
        return "ruff check ."  # sensible default for Python
    if ptype == "node":
        scripts = _pkg_scripts(cwd) or {}
        if "lint" in scripts:
            return f"{_pkg_manager(cwd)} run lint"
        if _exists(cwd, ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
                   ".eslintrc.yml", ".eslintrc.yaml", "eslint.config.js", "eslint.config.mjs"):
            return f"{_pkg_manager(cwd)} run lint --if-present || npx --no-install eslint ."
    if ptype == "go":
        return "go vet ./..."
    if ptype == "rust":
        return "cargo clippy --all-targets"
    return ""


def _detect_format_cmd(cwd: str, ptype: str) -> str:
    if ptype == "python":
        pyproject = _read_text(os.path.join(cwd, "pyproject.toml"), 4096)
        if _exists(cwd, "ruff.toml", ".ruff.toml") or "[tool.ruff" in pyproject:
            return "ruff format ."
        if "[tool.black" in pyproject:
            return "black ."
        return "black ."
    if ptype == "node":
        scripts = _pkg_scripts(cwd) or {}
        if "format" in scripts:
            return f"{_pkg_manager(cwd)} run format"
        if _exists(cwd, ".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs",
                   ".prettierrc.yaml", ".prettierrc.yml", "prettier.config.js", "prettier.config.cjs"):
            return f"{_pkg_manager(cwd)} run format --if-present || npx --no-install prettier --write ."
    if ptype == "go":
        return "gofmt -w ."
    if ptype == "rust":
        return "cargo fmt"
    return ""


def _pkg_manager(cwd: str) -> str:
    if _exists(cwd, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(cwd, "yarn.lock"):
        return "yarn"
    return "npm"


def _pkg_scripts(cwd: str) -> Optional[dict]:
    p = os.path.join(cwd, "package.json")
    if not os.path.exists(p):
        return None
    try:
        return (json.loads(_read_text(p, 200_000) or "{}") or {}).get("scripts") or {}
    except Exception:
        return {}


# ── entry points / key files / conventions ───────────────────────────────

def _detect_entry_points(cwd: str, ptype: str) -> List[str]:
    """Heuristic: the file(s) a fresh contributor would look at first to
    understand the project. Returned as workspace-relative paths."""
    candidates: List[str] = []
    common = [
        # Python
        "src/__main__.py", "src/main.py", "main.py", "app.py", "server.py",
        "manage.py",  # Django
        "src/index.ts", "src/index.js", "src/main.ts", "src/main.js", "index.ts", "index.js",
        "main.go", "cmd/main.go",
        "src/main.rs", "src/lib.rs",
        "app/main.go",  # generic
    ]
    for c in common:
        if os.path.exists(os.path.join(cwd, c)):
            candidates.append(c)
    return candidates[:5]


def _detect_conventions(cwd: str, ptype: str) -> List[str]:
    """Free-form bullets the model can use as guardrails when writing
    code. Cheap to compute, no LLM call."""
    out: List[str] = []
    if ptype == "python":
        if _exists(cwd, "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
            out.append("Python project with a manifest — install with the detected package manager before running tests.")
        if os.path.isdir(os.path.join(cwd, "src")):
            out.append("Uses a `src/` layout — import from `src.module`, not the project root.")
        if _exists(cwd, "mypy.ini", ".mypy.ini") or "[tool.mypy" in _read_text(os.path.join(cwd, "pyproject.toml"), 4096):
            out.append("Type-checked with mypy — keep type hints accurate.")
    if ptype == "node":
        scripts = _pkg_scripts(cwd) or {}
        if "typecheck" in scripts or "lint:tsc" in scripts:
            out.append("Has a `typecheck` script — run it after type-affecting edits.")
        if "test" in scripts:
            out.append(f"Test script: `npm test` → `{scripts.get('test')}`")
    if _exists(cwd, "tsconfig.json"):
        out.append("TypeScript project — keep types strict.")
    if _exists(cwd, ".editorconfig"):
        out.append("`.editorconfig` is the source of truth for formatting (indent, EOL, charset).")
    if _exists(cwd, "LICENSE", "LICENSE.md", "LICENSE.txt"):
        out.append("Project has a LICENSE file — respect its terms when contributing.")
    if _exists(cwd, "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        out.append("Containerized — `Dockerfile` / compose file present; check for the dev workflow before running things locally.")
    if _exists(cwd, "Makefile"):
        out.append("Has a Makefile — `make help` (or grep the file) for project-defined commands.")
    return out


def _detect_instructions_files(cwd: str) -> List[Dict[str, str]]:
    """Opencode / Claude Code convention files: AGENTS.md, CLAUDE.md, etc.
    Surfaces a short preview so the model knows what to read for full context."""
    found: List[Dict[str, str]] = []
    for rel in _INSTRUCTIONS_FILES:
        full = os.path.join(cwd, rel)
        if os.path.isfile(full):
            preview = _read_text(full, _MAX_PREVIEW_CHARS)
            found.append({"name": rel, "path": full, "preview": preview})
    # README is the universal "what is this project" file.
    for rel in ("README.md", "README.rst", "README.txt", "README"):
        full = os.path.join(cwd, rel)
        if os.path.isfile(full):
            preview = _read_text(full, _MAX_PREVIEW_CHARS)
            found.append({"name": rel, "path": full, "preview": preview})
            break
    return found


def _detect_key_files(cwd: str) -> List[str]:
    """Workspace-relative paths to the files that are MOST useful for a
    fresh agent: manifests, configs, and the obvious entry points.
    Returned in a stable order (manifests first, then entry points)."""
    seen: set = set()
    out: List[str] = []

    def add(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        out.append(rel)

    for hint in _MANIFEST_HINTS:
        if os.path.exists(os.path.join(cwd, hint)):
            add(hint)
    for cfg in ("tsconfig.json", "jest.config.js", "jest.config.ts",
                "vitest.config.js", "vitest.config.ts", "vite.config.js",
                "vitest.config.mjs", "vitest.config.mts", "vite.config.ts",
                ".eslintrc", ".eslintrc.js", "eslint.config.js",
                "tailwind.config.js", "postcss.config.js",
                ".editorconfig", ".prettierrc", ".flake8", "ruff.toml",
                "Makefile", "Dockerfile", "docker-compose.yml",
                "docker-compose.yaml"):
        if os.path.exists(os.path.join(cwd, cfg)):
            add(cfg)
    for ep in _detect_entry_points(cwd, ""):
        add(ep)
    return out[:20]


# ── public entry point ───────────────────────────────────────────────────

def project_bootstrap(cwd: str) -> Dict[str, Any]:
    """Detect the project's type, tooling, conventions, and any
    opencode/Claude-Code instruction files. Pure-function over the
    filesystem — no subprocess, no LLM call. Idempotent and fast."""
    if not cwd or not os.path.isdir(cwd):
        return {"error": "no workspace", "exit_code": 1}

    meta = _detect_type(cwd)
    test_cmd = _detect_test_cmd(cwd, meta["type"])
    lint_cmd = _detect_lint_cmd(cwd, meta["type"])
    format_cmd = _detect_format_cmd(cwd, meta["type"])

    return {
        "output": (
            f"Project: {meta['type']} ({meta['language']})\n"
            f"Package manager: {meta['package_manager'] or '(none detected)'}\n"
            f"Test runner: {test_cmd or '(none detected — see README)'}\n"
            f"Lint: {lint_cmd or '(none detected)'}\n"
            f"Format: {format_cmd or '(none detected)'}\n"
            + (f"Entry point(s): {', '.join(_detect_entry_points(cwd, meta['type'])) or '(none detected)'}\n"
               if _detect_entry_points(cwd, meta["type"]) else "")
        ),
        "type": meta["type"],
        "language": meta["language"],
        "package_manager": meta["package_manager"],
        "test_runner": test_cmd,
        "lint_command": lint_cmd,
        "format_command": format_cmd,
        "entry_points": _detect_entry_points(cwd, meta["type"]),
        "conventions": _detect_conventions(cwd, meta["type"]),
        "instructions_files": _detect_instructions_files(cwd),
        "key_files": _detect_key_files(cwd),
        "exit_code": 0,
    }


# ── cache ────────────────────────────────────────────────────────────────

# Workspace path → (mtime_signature, bootstrap_dict). Invalidated when the
# mtime of any "signature file" (manifest + AGENTS.md + README) changes.
_BOOTSTRAP_CACHE: Dict[str, tuple] = {}
_SIGNATURE_FILES = (
    *_MANIFEST_HINTS,
    *_INSTRUCTIONS_FILES,
    "README.md", "README.rst", "README.txt", "README",
    "tsconfig.json", ".editorconfig", "Dockerfile", "Makefile",
)


def _mtime_signature(cwd: str) -> tuple:
    """A tuple of (path, mtime) for every signature file in the workspace
    that exists. Used to invalidate the bootstrap cache cheaply (no need
    to re-read content — mtime changes when content changes)."""
    sig = []
    for rel in _SIGNATURE_FILES:
        full = os.path.join(cwd, rel)
        try:
            m = os.path.getmtime(full)
        except OSError:
            continue
        sig.append((rel, m))
    return tuple(sig)


def project_bootstrap_cached(cwd: str) -> Dict[str, Any]:
    """project_bootstrap with a per-workspace mtime-keyed cache. The cache
    lives in-process (memory); for the desktop app this is fine — the
    workspace is a single folder the user is actively working in."""
    sig = _mtime_signature(cwd)
    cached = _BOOTSTRAP_CACHE.get(cwd)
    if cached and cached[0] == sig:
        return cached[1]
    result = project_bootstrap(cwd)
    _BOOTSTRAP_CACHE[cwd] = (sig, result)
    return result


def invalidate_bootstrap_cache(cwd: Optional[str] = None) -> None:
    """Drop the cache for one workspace (or all). Called by the agent
    loop when the user explicitly says 'I just changed the project
    setup' or by the file watcher when a signature file changes."""
    if cwd is None:
        _BOOTSTRAP_CACHE.clear()
    else:
        _BOOTSTRAP_CACHE.pop(cwd, None)


# ── tool wrapper ─────────────────────────────────────────────────────────

class ProjectBootstrapTool:
    """Workspace project bootstrap. One-call orientation: project type,
    test/lint/format commands, entry points, conventions, and any
    opencode/Claude Code instruction files (AGENTS.md, CLAUDE.md,
    .opencode/instructions.md).

    Args: none. Returns the structured project profile.

    Cached per workspace (mtime-keyed), so repeated calls in the same
    session are free. Use `bash` to read a specific key file in full."""
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace
        cwd = get_active_workspace()
        if not cwd:
            return {"error": "project_bootstrap: no workspace is set. Pick a workspace first.", "exit_code": 1}
        return project_bootstrap_cached(cwd)
