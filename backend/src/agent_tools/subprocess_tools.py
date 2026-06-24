import asyncio
import os
import sys
import time
import collections
import logging
from typing import Optional, Callable, Awaitable, Tuple, Dict
from src.constants import MAX_OUTPUT_CHARS
from src.output_summarizer import summarize_output

logger = logging.getLogger(__name__)

DEFAULT_BASH_TIMEOUT = 60 * 60     # 1 hour
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12

async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
) -> Tuple[str, str, Optional[int], bool]:
    started = time.time()
    stdout_full: list[str] = []
    stderr_full: list[str] = []
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            full_buf.append(decoded)
            if label == "err":
                tail.append(f"! {decoded}")
            else:
                tail.append(decoded)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        "\n".join(stdout_full),
        "\n".join(stderr_full),
        proc.returncode,
        timed_out,
    )

async def spawn_shell(command: str, *, env: Optional[dict], cwd: Optional[str]) -> asyncio.subprocess.Process:
    """Spawn a shell command on the OS-correct shell.

    Routes through Git Bash (as a login shell) on Windows when available, else
    cmd.exe; on POSIX it uses the system shell. See
    :func:`core.platform_compat.agent_shell_invocation` for the rationale.
    Shared by the ``bash`` tool and the code-quality tools so every agent shell
    command runs through the same selection.
    """
    from core.platform_compat import agent_shell_invocation
    spec = agent_shell_invocation(command)
    if spec["env_extra"]:
        env = {**(env if env is not None else os.environ), **spec["env_extra"]}
    pipe = asyncio.subprocess.PIPE
    if spec["argv"]:
        return await asyncio.create_subprocess_exec(
            *spec["argv"], stdout=pipe, stderr=pipe, env=env, cwd=cwd,
        )
    return await asyncio.create_subprocess_shell(
        spec["shell_command"], stdout=pipe, stderr=pipe, env=env, cwd=cwd,
    )


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        proc = await spawn_shell(content, env=ctx.get("subproc_env"), cwd=agent_cwd())
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_BASH_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            # For a timeout we keep the full head/tail summary — the failure
            # reason is the timeout, not something in the output.
            return {"error": f"bash: timed out after {DEFAULT_BASH_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        # Long outputs (a 5 MB pip install) lose their last 10K chars to the
        # old head-only truncate. Summarize first — keep the head, the tail
        # (where errors live), and any "interesting" line in the middle —
        # THEN truncate. This is the single change that makes long-running
        # builds useful for the model again.
        summarized, stats = summarize_output(output)
        if stats.get("summarized"):
            logger.info(
                "[bash] summarized long output: %d lines / %d chars → %d chars "
                "(kept head=%d tail=%d interesting=%d omitted=%d)",
                stats.get("lines", 0), stats.get("len", 0), len(summarized),
                stats.get("kept_head", 0), stats.get("kept_tail", 0),
                stats.get("kept_interesting_middle", 0),
                stats.get("omitted_middle", 0),
            )
        output = _truncate(summarized, MAX_OUTPUT_CHARS)
        result = {"output": output or "(no output)", "exit_code": rc or 0}
        if stats.get("summarized"):
            result["output_summarized"] = stats
        return result

class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        proc = await asyncio.create_subprocess_exec(
            (sys.executable or "python"), "-I", "-c", content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subproc_env,
            cwd=agent_cwd(),
        )
        stdout, stderr, rc, timed_out = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS)}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        # Same summarizer as bash — Python's pip / pytest output can be
        # equally noisy on a slow build, and the actual error is at the tail.
        summarized, stats = summarize_output(output)
        if stats.get("summarized"):
            logger.info(
                "[python] summarized long output: %d lines / %d chars → %d chars "
                "(kept head=%d tail=%d interesting=%d omitted=%d)",
                stats.get("lines", 0), stats.get("len", 0), len(summarized),
                stats.get("kept_head", 0), stats.get("kept_tail", 0),
                stats.get("kept_interesting_middle", 0),
                stats.get("omitted_middle", 0),
            )
        output = _truncate(summarized, MAX_OUTPUT_CHARS)
        result = {"output": output or "(no output)", "exit_code": rc or 0}
        if stats.get("summarized"):
            result["output_summarized"] = stats
        return result
