#!/usr/bin/env python3
"""Executable property checks for emux, driven by complex-test-cases.
Each exits 0 iff the property holds. The JUDGE (these) is authored here; emux is
the artifact under test — author != judge."""
import asyncio
import os
import pathlib
import shlex
import sys
import tempfile
import time

sys.path.insert(0, "src")
from emux import server as s  # noqa: E402


def run(c):
    return asyncio.run(c)


RT = "rentamac"


def happy_search_running():           # happy
    run(s.tmux_spawn(name="ctc-run", command="sleep 30"))
    run(s.tmux_sessions())
    r = run(s.tmux_search(query="ctc-run", status="running"))
    ok = any(x["name"] == "ctc-run" for x in r["results"])
    s._run_tmux(["kill-session", "-t", "ctc-run"])
    sys.exit(0 if ok else 1)


def search_finds_ended():             # integration + ordering (the flagship claim)
    run(s.tmux_spawn(name="ctc-end", command="true"))
    run(s.tmux_sessions())            # track it
    s._run_tmux(["kill-session", "-t", "ctc-end"])
    r = run(s.tmux_search(query="ctc-end", status="ended"))
    ok = any(x["name"] == "ctc-end" and x["status"] == "ended" for x in r["results"])
    sys.exit(0 if ok else 1)


def adopt_ghost_not_live():           # negative — must correctly say NO
    r = run(s.tmux_register(name="ctc-ghost", session=f"nope-{int(time.time())}", host=RT))
    sys.exit(0 if r["session_live"] is False else 1)


def drive_dead_refuses():             # refusal — the correct outcome is failure, not fake success
    r = run(s.tmux_send(target=f"no-such-{int(time.time())}", keys="echo x"))
    sys.exit(0 if not r["ok"] else 1)


def manager_worker_greenmark():       # delegation — the nested-manager pattern, proven end-to-end, harmlessly
    """A MANAGER session spawns a WORKER session that leaves a harmless green
    mark (a unique token in a temp file). Proves the nested-manager claim: a
    session managing a session, with the manager->worker edge first-class in the
    registry — while THIS process only checks the top-level result, never
    touching the worker. Harmless: one echo into a throwaway temp file, two
    throwaway tmux sessions, both killed."""
    token = f"GREENMARK-{os.getpid()}-{int(time.time())}"
    proof = pathlib.Path(tempfile.gettempdir()) / f"emux-greenmark-{os.getpid()}.txt"
    proof.unlink(missing_ok=True)
    # The manager's job (its launch command) is to spawn the worker, which
    # writes the green mark. `tmux new-session` from inside the manager session
    # IS a session spawning a session — the nesting, done for real.
    worker_cmd = f"echo {token} > {shlex.quote(str(proof))}"
    manager_cmd = f"tmux new-session -d -s ctc-wrk {shlex.quote(worker_cmd)}"
    run(s.tmux_spawn(name="ctc-mgr", command=manager_cmd, manages=["ctc-wrk"]))
    got = ""
    for _ in range(50):               # up to ~5s for the worker to leave its mark
        if proof.exists():
            got = proof.read_text().strip()
            if got == token:
                break
        time.sleep(0.1)
    # The tree is first-class: the manager's registry entry declares the worker.
    edge = s._load_registry().get("ctc-mgr", {}).get("manages") == ["ctc-wrk"]
    for sess in ("ctc-wrk", "ctc-mgr"):
        s._run_tmux(["kill-session", "-t", sess])
    proof.unlink(missing_ok=True)
    sys.exit(0 if (got == token and edge) else 1)


if __name__ == "__main__":
    {"happy": happy_search_running, "ended": search_finds_ended,
     "ghost": adopt_ghost_not_live, "refusal": drive_dead_refuses,
     "greenmark": manager_worker_greenmark}[sys.argv[1]]()
