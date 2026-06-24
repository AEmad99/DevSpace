"""Git tools for the coding agent — first-class `git_status`, `git_diff`,
`git_log`, `git_blame`, `git_commit`, `git_branch` instead of forcing the model
to shell out.

Why a named tool set instead of `bash git …`?
  • The agent loop can show real-time git state in the prompt (branch /
    uncommitted-count) and use it to make decisions (revert a file, rebase,
    see what changed). With shell-out those calls cost a full round each.
  • Output is structured (status → list, log → list, blame → lines) so the
    model gets a clean JSON-ish shape and the UI can render it nicely.
  • Each call is confined to the active workspace (same as the file tools)
    and run with a 30s timeout so a misbehaving `git blame` on a giant file
    doesn't lock the agent loop.
  • The model still has `bash` for one-off git commands (interactive rebase,
    stash, custom flags). These tools are the common case, not a replacement.

The implementation runs `git -C <workspace> …` directly via subprocess.run
(rather than spawning a shell) so no user input ever reaches a shell parser.
Confinement is enforced by `_resolve_tool_path_in_workspace` on every path arg
the model supplies — same gate the file tools use.
"""
import asyncio
import json
import os
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from src.constants import MAX_OUTPUT_CHARS
from src.tool_execution import _resolve_tool_path_in_workspace, _truncate

# Per-call timeout. Git operations are usually fast; the only slow ones are
# blame on huge files and log --all on huge repos, both of which the model
# can scope with a path / max_count arg.
GIT_TIMEOUT = 30


# --- shared runner ---------------------------------------------------------

def _git_workspace() -> Optional[str]:
    """The agent's current workspace, or None when no workspace is set. Git
    commands are workspace-scoped; without one, the tools refuse to run
    (the model can still fall back to `bash git -C <path> ...`)."""
    from src.tool_execution import get_active_workspace
    return get_active_workspace()


def _run_git(args: List[str], cwd: str, *, timeout: int = GIT_TIMEOUT) -> Dict[str, Any]:
    """Run `git -C <cwd> <args>…` and return {ok, returncode, stdout, stderr}.

    Never raises on non-zero exit — the model should see the error and decide.
    Only raises on truly unexpected failures (git not on PATH, etc.)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Force deterministic, parseable English output.
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0"},
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": "git is not installed or not on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "stdout": "", "stderr": f"git timed out after {timeout}s"}


def _resolve_in_workspace(cwd: str, raw_path: str) -> Optional[str]:
    """Resolve `raw_path` inside the workspace, raising if it escapes. Returns
    None when the path is invalid (caller turns that into a clean error)."""
    if not raw_path:
        return None
    try:
        return _resolve_tool_path_in_workspace(cwd, raw_path)
    except ValueError:
        return None


def _not_in_repo(cwd: str) -> bool:
    """True when cwd is not inside a git working tree (or git isn't installed)."""
    r = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return not r["ok"]


# --- per-tool argument parsing ---------------------------------------------

def _parse_args(content: str) -> Dict[str, Any]:
    """Parse the tool's JSON arg blob. Returns {} on parse failure (tools
    then fall back to their own defaults)."""
    s = (content or "").strip()
    if not s.startswith("{"):
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}


# --- tool classes ----------------------------------------------------------

class GitStatusTool:
    """Workspace git status: branch, ahead/behind, uncommitted files.

    Args (all optional): {path} (a sub-path of the workspace to scope to —
    reads `git status` for just that subdir; default = whole workspace)."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_status: no workspace is set. Pick a workspace first or use `bash` with `git -C <path> …`.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"output": f"{cwd}: not a git repository", "exit_code": 0}

        args = _parse_args(content)
        scope = _resolve_in_workspace(cwd, args.get("path", "") or "")

        # Porcelain v1 with branch line; --untracked-files=all so the model
        # can see new files it just wrote.
        cmd = ["status", "--porcelain=v1", "-b", "--untracked-files=all"]
        if scope:
            cmd += ["--", scope]
        r = _run_git(cmd, cwd)
        if not r["ok"]:
            return {"error": (r["stderr"] or r["stdout"]).strip() or "git status failed", "exit_code": 1}

        branch = None
        files: List[Dict[str, str]] = []
        for line in r["stdout"].splitlines():
            if not line:
                continue
            if line.startswith("## "):
                # Format: "## main...origin/main [ahead 2, behind 1]" or "## main"
                header = line[3:]
                branch = header.split("...")[0].strip() or None
                continue
            if len(line) < 4:
                continue
            x, y, p = line[0], line[1], line[3:]
            if " -> " in p:  # rename: surface the new name (model cares about that)
                p = p.split(" -> ", 1)[1]
            files.append({
                "path": p,
                "x": x, "y": y,
                "staged": x not in (" ", "?"),
                "unstaged": (y != " ") or x == "?",
                "untracked": x == "?",
            })

        # Truncate the file list if absurdly long (a clean status is ~0 lines;
        # a fresh checkout can be 10k+). Keep the first + a "+N more" note.
        TRUNC = 200
        truncated = len(files) > TRUNC
        out_files = files[:TRUNC]

        return {
            "output": (
                f"branch: {branch or '(detached)'}\n"
                f"uncommitted files: {len(files)}\n"
                + ("\n".join(
                    f"  {f['x']}{f['y']} {f['path']}"
                    + ("  (untracked)" if f['untracked'] else "")
                    for f in out_files
                ) or "  (working tree clean)")
                + (f"\n  … ({len(files) - TRUNC} more)" if truncated else "")
            ),
            "branch": branch,
            "uncommitted_count": len(files),
            "files": out_files,
            "truncated": truncated,
            "exit_code": 0,
        }


class GitDiffTool:
    """Workspace git diff. Returns a unified diff text plus a structured
    summary (added/removed line counts, file count). For an untracked file,
    surfaces its content as a virtual `+` diff so the model can see what it
    just wrote.

    Args: {path} (optional, scope to a sub-path), {staged} (bool, default
    false → working-tree diff; true → --cached)."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_diff: no workspace is set. Use `bash` with `git -C <path> …` for an arbitrary path.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"output": f"{cwd}: not a git repository", "exit_code": 0}

        args = _parse_args(content)
        scope = _resolve_in_workspace(cwd, args.get("path", "") or "")
        staged = bool(args.get("staged"))

        cmd = ["diff", "--no-color", "--no-ext-diff"]
        if staged:
            cmd.append("--cached")
        if scope:
            cmd += ["--", scope]
        r = _run_git(cmd, cwd)

        diff = r["stdout"]
        # Untracked files have no diff target. If the user asked about a single
        # path and it's untracked, show its content as a virtual `+` diff —
        # the model uses git_diff to "see what changed" right after write_file.
        if not diff.strip() and scope and not staged and os.path.isfile(scope):
            r2 = _run_git(["status", "--porcelain=v1", "--untracked-files=all", "--", scope], cwd)
            if r2["ok"] and r2["stdout"].lstrip().startswith("??"):
                try:
                    with open(scope, "r", encoding="utf-8", errors="replace") as f:
                        body = f.read()
                except OSError as e:
                    return {"error": f"git_diff: cannot read {scope}: {e}", "exit_code": 1}
                # Cap the untracked-file virtual diff so a freshly-written 10k-line
                # file doesn't blow the tool output budget — same idea as
                # write_file's own truncation.
                if len(body) > MAX_OUTPUT_CHARS:
                    body = body[:MAX_OUTPUT_CHARS] + f"\n… [truncated at {MAX_OUTPUT_CHARS} chars]"
                diff = f"--- /dev/null\n+++ b/{scope}\n" + "\n".join(
                    f"+{line}" for line in body.splitlines()
                )

        # Cheap stat: count + and - lines in the diff body.
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        file_count = sum(1 for ln in diff.splitlines() if ln.startswith("--- ") and not ln.startswith("--- /"))

        return {
            "output": _truncate(diff) if diff else "(no changes)",
            "diff": diff,
            "added": added,
            "removed": removed,
            "file_count": file_count,
            "staged": staged,
            "exit_code": 0,
        }


class GitLogTool:
    """Recent commits. Args: {path} (optional), {max_count} (default 10),
    {oneline} (default true — one line per commit, otherwise full format)."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_log: no workspace is set. Use `bash` with `git -C <path> log …` for an arbitrary path.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"output": f"{cwd}: not a git repository", "exit_code": 0}

        args = _parse_args(content)
        scope = _resolve_in_workspace(cwd, args.get("path", "") or "")
        try:
            max_count = max(1, min(int(args.get("max_count") or 10), 100))
        except (TypeError, ValueError):
            max_count = 10
        oneline = bool(args.get("oneline", True))

        if oneline:
            fmt = ["--pretty=format:%h%x09%an%x09%ad%x09%s", "--date=short"]
        else:
            fmt = ["--pretty=format:%H%x09%an%x09%ad%x09%s%n%b%n--"]
        cmd = ["log", f"--max-count={max_count}", "--no-color"] + fmt
        if scope:
            cmd += ["--", scope]
        r = _run_git(cmd, cwd)
        if not r["ok"]:
            return {"error": (r["stderr"] or r["stdout"]).strip() or "git log failed", "exit_code": 1}

        commits: List[Dict[str, str]] = []
        for line in r["stdout"].splitlines():
            if not line or line == "--":
                continue
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            })

        out = "\n".join(
            f"{c['hash']}  {c['date']}  {c['author']}  {c['subject']}"
            for c in commits
        ) or "(no commits)"

        return {"output": _truncate(out), "commits": commits, "count": len(commits), "exit_code": 0}


class GitBlameTool:
    """Annotate lines of a file with the commit that last touched them.
    Args: {path} (required), {start_line} (1-based; default 1), {end_line}
    (default = whole file). The model uses this to answer "who wrote this
    line / when was it last changed" without a full grep across history."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_blame: no workspace is set. Use `bash` with `git -C <path> blame …` for an arbitrary path.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"output": f"{cwd}: not a git repository", "exit_code": 0}

        args = _parse_args(content)
        scope = _resolve_in_workspace(cwd, args.get("path", "") or "")
        if not scope or not os.path.isfile(scope):
            return {"error": f"git_blame: path {args.get('path', '')!r} is not a regular file inside the workspace", "exit_code": 1}

        try:
            start = max(1, int(args.get("start_line") or 1))
            end = max(start, int(args.get("end_line") or 0))  # 0 → whole file
        except (TypeError, ValueError):
            start, end = 1, 0

        cmd = ["blame", "--line-porcelain"]
        if end > 0:
            cmd.append(f"-L{start},{end}")
        cmd += ["--", scope]
        r = _run_git(cmd, cwd, timeout=GIT_TIMEOUT)
        if not r["ok"]:
            return {"error": (r["stderr"] or r["stdout"]).strip() or "git blame failed", "exit_code": 1}

        # `git blame --line-porcelain` emits per-line header+content records.
        # Each line starts with "<sha> <orig-line> <final-line> [<count>]", then
        # key/value lines (author, author-mail, author-time, summary, …), then
        # the line content prefixed with a tab. Walk it into a flat list.
        lines: List[Dict[str, str]] = []
        current: Dict[str, str] = {}
        for raw in r["stdout"].splitlines():
            if not raw:
                continue
            if raw.startswith("\t"):
                # Line content — store on the current record + finalize.
                current["content"] = raw[1:]
                lines.append(current)
                current = {}
                continue
            # Header line: "<sha> <orig-line> <final-line> [<count>]"
            head = raw.split(" ", 3)
            if len(head) >= 3 and len(head[0]) >= 8 and all(c in "0123456789abcdef" for c in head[0][:8].lower()):
                current = {
                    "commit": head[0],
                    "orig_line": head[1],
                    "final_line": head[2],
                }
                continue
            # Key/value (author, author-mail, author-time, summary, …).
            if " " in raw and current:
                k, _, v = raw.partition(" ")
                if k in ("author", "author-mail", "author-time", "summary", "committer", "filename"):
                    current[k.replace("-", "_")] = v

        out_lines = [
            f"{ln.get('final_line', '?'):>5}  {ln.get('commit', '?')[:8]}  "
            f"{(ln.get('author') or '?')[:24]:<24}  {ln.get('content', '')}"
            for ln in lines
        ]
        return {
            "output": _truncate("\n".join(out_lines)) or "(no lines)",
            "lines": [{"line": l.get("final_line"), "commit": l.get("commit"),
                       "author": l.get("author"), "content": l.get("content")}
                      for l in lines],
            "exit_code": 0,
        }


class GitCommitTool:
    """Stage + commit. Args: {message} (required), {paths} (optional list of
    paths to stage; default = stage everything that's currently modified).
    Uses -m for the message; for multi-line / heredoc messages the model
    can use `bash git -C <workspace> commit -m "..." -m "..."`.

    Refuses to commit when no `message` is supplied, when paths escape the
    workspace, or when there's nothing to commit (git's own check; surfaced
    as a clean error)."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_commit: no workspace is set. Use `bash` with `git -C <path> commit …` for an arbitrary path.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"error": f"{cwd}: not a git repository", "exit_code": 1}

        args = _parse_args(content)
        msg = (args.get("message") or "").strip()
        if not msg:
            return {"error": "git_commit: `message` is required", "exit_code": 1}

        paths_arg = args.get("paths")
        if isinstance(paths_arg, str):
            paths: List[str] = [paths_arg]
        elif isinstance(paths_arg, list):
            paths = [str(p) for p in paths_arg if p]
        else:
            paths = []

        # If specific paths were given, resolve + stage them; else `git commit
        # -a` picks up all tracked changes.
        if paths:
            resolved = []
            for p in paths:
                rp = _resolve_in_workspace(cwd, p)
                if not rp:
                    return {"error": f"git_commit: path {p!r} is outside the workspace", "exit_code": 1}
                resolved.append(os.path.relpath(rp, cwd))
            cmd = ["add", "--"] + resolved
            r = _run_git(cmd, cwd)
            if not r["ok"]:
                return {"error": (r["stderr"] or r["stdout"]).strip() or "git add failed", "exit_code": 1}

        # -m once for single-line. Multi-line messages via `bash` are still
        # available; this tool is the common case.
        cmd = ["commit", "-m", msg]
        r = _run_git(cmd, cwd, timeout=GIT_TIMEOUT)
        out = (r["stdout"] or "").strip()
        err = (r["stderr"] or "").strip()
        if r["ok"]:
            return {"output": out or "(committed)", "exit_code": 0}
        # Nothing-to-commit is a legitimate outcome the model should see, not
        # an error to retry. Surface as a clean exit_code=0 + a clear message.
        # Git emits either "nothing to commit" (older releases) or
        # "no changes added to commit" (newer, 2.30+) on the same path.
        if "nothing to commit" in err.lower() or "nothing to commit" in out.lower() \
                or "no changes added to commit" in err.lower() \
                or "no changes added to commit" in out.lower():
            return {"output": "Nothing to commit (working tree clean or only unstaged changes).", "exit_code": 0}
        return {"error": err or out or "git commit failed", "exit_code": 1}


class GitBranchTool:
    """Branch ops. Args: {action} ('list' | 'current' | 'create' | 'delete',
    default 'list'), {name} (required for create/delete). create/delete use
    git's standard safety checks (-d refuses to delete unmerged branches)."""
    async def execute(self, content: str, ctx: dict) -> dict:
        cwd = _git_workspace()
        if not cwd:
            return {"error": "git_branch: no workspace is set. Use `bash` with `git -C <path> branch …` for an arbitrary path.", "exit_code": 1}
        if _not_in_repo(cwd):
            return {"output": f"{cwd}: not a git repository", "exit_code": 0}

        args = _parse_args(content)
        action = (args.get("action") or "list").strip().lower()
        name = (args.get("name") or "").strip()

        if action in ("list", "current"):
            cmd = ["branch", "--no-color"]
            if action == "current":
                cmd += ["--show-current"]
            r = _run_git(cmd, cwd)
            if not r["ok"]:
                return {"error": (r["stderr"] or r["stdout"]).strip() or "git branch failed", "exit_code": 1}
            out = r["stdout"].strip()
            if action == "current":
                return {"output": out or "(detached HEAD)", "current": out, "exit_code": 0}
            branches: List[Dict[str, str]] = []
            current_branch = None
            for ln in out.splitlines():
                if ln.startswith("*"):
                    current_branch = ln[2:].strip()
                    branches.append({"name": current_branch, "current": True})
                elif ln.strip():
                    branches.append({"name": ln.strip(), "current": False})
            return {
                "output": _truncate(out) or "(no branches)",
                "branches": branches,
                "current": current_branch,
                "exit_code": 0,
            }

        if action in ("create", "delete"):
            if not name:
                return {"error": f"git_branch {action}: `name` is required", "exit_code": 1}
            # Refuse anything that smells like a git ref flag — prevents the
            # model from doing `git branch -D main` by passing name='-D main'.
            if name.startswith("-"):
                return {"error": f"git_branch {action}: branch name must not start with '-'", "exit_code": 1}
            if action == "create":
                r = _run_git(["checkout", "-b", name], cwd)
            else:
                # -d refuses unmerged; -D forces. Use -d for safety; the model
                # can always run `bash git -C … branch -D …` for the forceful form.
                r = _run_git(["branch", "-d", name], cwd)
            if r["ok"]:
                verb = "Created and checked out" if action == "create" else "Deleted"
                return {"output": f"{verb} branch {name!r}", "exit_code": 0}
            err = (r["stderr"] or r["stdout"]).strip()
            # "not fully merged" → surface cleanly; the model decides whether
            # to retry with `bash` for a forced delete.
            if "not fully merged" in err.lower():
                return {"error": f"git_branch: {name!r} has unmerged commits. Use `bash git -C <workspace> branch -D {shlex.quote(name)}` to force-delete.", "exit_code": 1}
            return {"error": err or f"git branch {action} failed", "exit_code": 1}

        return {"error": f"git_branch: unknown action {action!r}. Use 'list' | 'current' | 'create' | 'delete'.", "exit_code": 1}
