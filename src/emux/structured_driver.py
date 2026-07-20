"""Drive Claude Code through its STRUCTURED surface, not its terminal screen
(EID-875 — the fix for the scrape+keystroke transport).

The capture/send transport reads a terminal screenshot and injects keystrokes —
the same forgeable-pixel anti-pattern the EID-874 audit killed. Claude Code
already exposes a structured, non-forgeable surface:

    claude -p "<task>" --output-format json --permission-mode ... --settings <json>

which returns a single JSON object:

    {"type":"result","subtype":"success","is_error":false,
     "result":"<final assistant text>","stop_reason":"end_turn",
     "session_id":"...","total_cost_usd":..., "usage":{...}, ...}

vs. the screen path this gives us, for free:
  - a REAL completion signal (no sleep-and-guess): the process exits, is_error
    and stop_reason are authoritative;
  - the actual result text, not ANSI box-drawing scraped off a pane;
  - session_id to resume the SAME conversation (real continuity, not "type into
    whatever pane is there");
  - cost/usage accounting;
  - permissions enforced at the structured PreToolUse boundary via --settings
    (the delegation hook, EID-874) — the tool_name/input can't be forged.

Fixed-cost tooling only: this shells out to the `claude` CLI (subscription),
never the raw Anthropic API.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from . import worker_env


@dataclass
class DriveResult:
    ok: bool
    result: str | None = None
    is_error: bool = False
    stop_reason: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _hook_settings(python: str) -> str:
    """Inline --settings JSON that wires the PreToolUse delegation hook, so tool
    permissions are enforced at the structured boundary (EID-874)."""
    return json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": f"{python} -m emux.hook_delegation"}
                        ],
                    }
                ]
            }
        }
    )


def drive(
    task: str,
    cwd: str,
    *,
    identity: str,
    server_id: str | None = None,
    resume_session: str | None = None,
    permission_mode: str = "acceptEdits",
    with_hook: bool = True,
    claude_bin: str = "claude",
    python: str = "python3",
    timeout: float = 600.0,
    env: dict[str, str] | None = None,
) -> DriveResult:
    """Run one structured turn against Claude Code in `cwd`, as `identity`.

    Returns a DriveResult with the authoritative completion — no screen capture,
    no keystroke injection, no timing guess. `resume_session` continues an
    existing session_id (real conversational continuity)."""
    cmd = [claude_bin, "-p", task, "--output-format", "json", "--permission-mode", permission_mode]
    if with_hook:
        cmd += ["--settings", _hook_settings(python)]
    if resume_session:
        cmd += ["--resume", resume_session]

    run_env = {**os.environ, **(env or {})}
    run_env["EMUX_IDENTITY"] = identity
    if server_id:
        run_env["EMUX_SERVER_ID"] = server_id
    # Bound the temp dir explicitly — the root-only /tmp bites otherwise (EID-873).
    run_env.setdefault("TMPDIR", os.path.join(os.path.expanduser("~"), "tmp-claude"))

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, input="", capture_output=True, text=True,
            env=run_env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DriveResult(ok=False, error="timeout")
    except OSError as exc:
        return DriveResult(ok=False, error=f"spawn_failed:{exc}")

    if not proc.stdout.strip():
        return DriveResult(ok=False, error="no_output", raw={"stderr": proc.stderr[-500:]})
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return DriveResult(ok=False, error="unparseable_output", raw={"stdout": proc.stdout[-500:]})

    is_error = bool(obj.get("is_error"))
    return DriveResult(
        ok=not is_error and obj.get("subtype") == "success",
        result=obj.get("result"),
        is_error=is_error,
        stop_reason=obj.get("stop_reason"),
        session_id=obj.get("session_id"),
        cost_usd=obj.get("total_cost_usd"),
        num_turns=obj.get("num_turns"),
        error=obj.get("subtype") if is_error else None,
        raw=obj,
    )


def drive_in_bounded_worker(
    task: str,
    project_dir: str,
    *,
    identity: str,
    server_id: str | None = None,
    python: str = "python3",
    provision: bool = True,
    **kwargs: Any,
) -> DriveResult:
    """Provision the bounded worker env (trust-by-config + delegation hook wiring)
    then drive one structured turn. The gate is CONFIGURED AWAY, not answered."""
    if provision:
        worker_env.provision(project_dir, python=python)
    return drive(
        task, project_dir, identity=identity, server_id=server_id,
        python=python, **kwargs,
    )


class StructuredClaudeAdapter:
    """ONE of Claude's several drive adapters (EID-875): the `claude -p` JSON
    surface with a PreToolUse permission hook. Good for bounded, resumable
    task-steps. NOT the way Claude is driven — a sibling to claude-sdk (long
    runs), claude-terminal (observe / universal), etc."""

    name = "claude-structured"
    agent = "claude"
    traits = frozenset({"structured", "resumable", "permission_hook", "bounded_step"})

    def drive(self, task: str, cwd: str, **kwargs: Any) -> DriveResult:
        return drive(task, cwd, **kwargs)


# Register as one Claude adapter among many; adapters_for("claude") returns a
# LIST, so adding claude-sdk / claude-terminal later needs no change here.
try:  # keep import-time failures from taking down the module
    from . import drive_adapter as _da

    _da.register(StructuredClaudeAdapter())
except Exception:  # noqa: BLE001
    pass
