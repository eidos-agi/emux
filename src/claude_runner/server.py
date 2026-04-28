"""claude-runner MCP server.

Exposes a single tool, `claude_run`, that runs `claude <args>` as a subprocess
with optional pre-flight `claude plugins marketplace update <name>`. Captures
stdout, stderr, returncode, and elapsed time. Returns structured JSON.

Tier 1 only: subprocess wrapper, no PTY, no tmux, no interactivity. For
interactive driving of another Claude Code session, see the Tier 2 spec
(deferred — only ship if a use case demands it).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("claude-runner")


def _resolve_claude() -> str | None:
    """Locate the `claude` CLI on PATH. Returns None if not found."""
    return shutil.which("claude")


def _run_subprocess(
    cmd: list[str],
    timeout: int,
    cwd: str | None,
) -> dict[str, Any]:
    """Run a subprocess synchronously, capture output, return structured result."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": "timeout",
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "command": " ".join(cmd),
            "timeout_seconds": timeout,
            "stdout_partial": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            "stderr_partial": (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
            "hint": (
                f"`{cmd[0]}` did not finish within {timeout}s. The process was killed. "
                "If this was a slow first-time uvx fetch, retry with a higher timeout."
            ),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "command_not_found",
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "command": " ".join(cmd),
            "hint": (
                f"`{cmd[0]}` was not found on PATH. If you meant the Claude Code CLI, "
                "ensure it's installed and discoverable (try `which claude`)."
            ),
        }
    except OSError as e:
        return {
            "ok": False,
            "error": "os_error",
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "command": " ".join(cmd),
            "stderr": str(e),
        }

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.monotonic() - start, 2),
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


@mcp.tool()
async def claude_run(
    args: list[str],
    timeout: int = 300,
    refresh_marketplace: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run `claude <args>` as a subprocess and return the result.

    Use this when you need to autonomously run a Claude Code CLI command and
    observe its output — for example, to verify a marketplace plugin install
    works end-to-end without a human in the loop, or to script a sequence of
    Claude invocations.

    Args:
        args: Argument list passed to `claude` (e.g. `["plugins", "install",
            "cept@eidos-marketplace"]`). Do NOT include `claude` itself; it is
            prepended.
        timeout: Maximum seconds to wait for the command. Default 300. The
            first `uvx` fetch of a new package can take 30–90s; allow headroom.
        refresh_marketplace: If set, runs `claude plugins marketplace update
            <name>` BEFORE the main command. Use this for plugin install tests
            to ensure the local marketplace cache reflects the latest GitHub
            state. (See LEARNINGS.md in eidos-marketplace for context.)
        cwd: Working directory for the subprocess. Defaults to the parent
            process's cwd. Useful when the Claude command is sensitive to
            project context (e.g. session discovery).

    Returns:
        A dict with at minimum `ok` (bool) and either `{returncode, stdout,
        stderr, elapsed_seconds}` on success/failure with output, or
        `{error, hint}` on environmental failures (timeout, command not found).
        The dict is JSON-serializable.

    Failure modes:
        - `error: "command_not_found"` — `claude` is not on PATH.
        - `error: "timeout"` — the command exceeded `timeout` seconds; the
          process was killed and any captured partial output is returned.
        - `error: "os_error"` — the OS rejected the spawn (rare).

    This tool does NOT run interactive Claude Code sessions. It is for
    one-shot, capturable commands. For interactive driving (sending slash
    commands, reading streaming responses), use a future Tier 2 tool — not
    yet implemented.
    """
    claude_path = _resolve_claude()
    if claude_path is None:
        return {
            "ok": False,
            "error": "command_not_found",
            "command": "claude " + " ".join(args),
            "hint": "`claude` is not on PATH. Install Claude Code first.",
        }

    refresh_result: dict[str, Any] | None = None
    if refresh_marketplace:
        refresh_cmd = [claude_path, "plugins", "marketplace", "update", refresh_marketplace]
        refresh_result = await asyncio.to_thread(
            _run_subprocess, refresh_cmd, min(timeout, 60), cwd
        )
        if not refresh_result["ok"]:
            return {
                "ok": False,
                "error": "marketplace_refresh_failed",
                "refresh_result": refresh_result,
                "hint": (
                    f"`claude plugins marketplace update {refresh_marketplace}` failed. "
                    "Verify the marketplace name is correct and that the marketplace "
                    "has been added (`claude plugins marketplace add <source>`)."
                ),
            }

    main_cmd = [claude_path] + list(args)
    result = await asyncio.to_thread(_run_subprocess, main_cmd, timeout, cwd)

    if refresh_result is not None:
        result["refresh"] = {
            "ok": refresh_result["ok"],
            "elapsed_seconds": refresh_result["elapsed_seconds"],
        }

    return result


def main() -> None:
    """Entry point for the `claude-runner` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
