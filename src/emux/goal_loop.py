"""Goal loop — drive an adapter toward a VERIFIED objective over many turns,
and instrument exactly where it breaks (EID-875/EID-876).

The point is not to ship autonomy; it's to convert "the substrate chains for 19
seconds" into a real number: how many turns / how much cost / how much context
growth before a genuine multi-step task completes or dies, and WHY it stops.

Ground truth is a VERIFIER COMMAND (e.g. `pytest -q`) run after each turn — not
the model's self-report. Done = the verifier passes. The loop resumes the same
session (real continuity) and stops on: goal_met | stalled | budget | max_iters.
Every iteration records {stop_reason, cost, input_tokens (context growth proxy),
wall, verifier pass}. Local `claude` CLI only (fixed-cost).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from . import structured_driver as sd


@dataclass
class Iteration:
    n: int
    ok: bool
    stop_reason: str | None
    cost_usd: float | None
    input_tokens: int | None   # grows each turn — the context-growth signal
    wall_s: float
    verified: bool
    verify_tail: str = ""


@dataclass
class GoalRun:
    reason: str                 # goal_met | stalled | budget | max_iters | error
    verified: bool
    iterations: list[Iteration] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0
    session_id: str | None = None


def _verify(cmd: str, cwd: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Run the ground-truth check. Passing = exit 0. Returns (passed, tail).

    shell=True is deliberate and safe here: `verify_cmd` is OPERATOR config (the
    human writes the check, e.g. `pytest -q && ruff check .`), never agent- or
    end-user input. It needs a shell for pipes/&&. Not a command-injection
    surface — the agent under test cannot set it."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)  # noqa: S602
    except subprocess.TimeoutExpired:
        return False, "verify_timeout"
    out = (p.stdout + p.stderr)[-400:]
    return p.returncode == 0, out


def run_goal(
    goal: str,
    cwd: str,
    verify_cmd: str,
    *,
    identity: str,
    server_id: str | None = None,
    max_iters: int = 15,
    max_cost_usd: float = 5.0,
    stall_limit: int = 3,
    provision: bool = True,
    python: str = "python3",
    per_turn_timeout: float = 300.0,
    now: Any = time.time,
) -> GoalRun:
    """Drive `identity` toward `goal` in `cwd` until `verify_cmd` passes or a
    budget stops it. Resumes the same session each turn (context threads)."""
    if provision:
        from . import worker_env
        worker_env.provision(cwd, python=python)

    run = GoalRun(reason="max_iters", verified=False)
    sid: str | None = None
    last_tail = None
    stalled_for = 0
    t_start = now()

    for i in range(1, max_iters + 1):
        if i == 1:
            prompt = (
                f"{goal}\n\nWork toward this. When you believe it's done, run "
                f"`{verify_cmd}` yourself to confirm. Be concise."
            )
        else:
            prompt = (
                f"Continue toward the goal. `{verify_cmd}` is still failing:\n"
                f"{last_tail}\n\nFix it and re-run the check. Keep going until it passes."
            )

        t0 = now()
        r = sd.drive(
            prompt, cwd, identity=identity, server_id=server_id, python=python,
            resume_session=sid, timeout=per_turn_timeout,
        )
        sid = r.session_id or sid
        wall = now() - t0

        verified, tail = _verify(verify_cmd, cwd)
        # Real context size the model saw = fresh input + cache-read (prompt
        # caching parks the growing conversation in cache_read, so input_tokens
        # alone reads ~0 and hides growth — measure the sum).
        u = r.raw.get("usage") or {}
        ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        run.iterations.append(Iteration(
            n=i, ok=r.ok, stop_reason=r.stop_reason, cost_usd=r.cost_usd,
            input_tokens=ctx or None,
            wall_s=round(wall, 1), verified=verified, verify_tail=tail[-160:],
        ))
        run.total_cost_usd += r.cost_usd or 0.0
        run.session_id = sid

        if verified:
            run.reason, run.verified = "goal_met", True
            break
        # stall: identical failing check output N times in a row
        if tail == last_tail:
            stalled_for += 1
        else:
            stalled_for = 0
        last_tail = tail
        if stalled_for >= stall_limit:
            run.reason = "stalled"
            break
        if run.total_cost_usd >= max_cost_usd:
            run.reason = "budget"
            break
        if not r.ok and r.error == "timeout":
            run.reason = "error"
            break

    run.total_wall_s = round(now() - t_start, 1)
    return run
