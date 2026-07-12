#!/usr/bin/env python3
"""Executable property checks for emux, driven by complex-test-cases.
Each exits 0 iff the property holds. The JUDGE (these) is authored here; emux is
the artifact under test — author != judge."""
import asyncio, sys, time, subprocess
sys.path.insert(0, "src")
from emux import server as s
run = lambda c: asyncio.run(c)
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

if __name__ == "__main__":
    {"happy": happy_search_running, "ended": search_finds_ended,
     "ghost": adopt_ghost_not_live, "refusal": drive_dead_refuses}[sys.argv[1]]()
