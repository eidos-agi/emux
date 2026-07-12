#!/usr/bin/env python3
"""Executable property checks for emux, driven by complex-test-cases.
Each exits 0 iff the property holds. The JUDGE (these) is authored here; emux is
the artifact under test — author != judge."""
import asyncio
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "src")
from emux import server as s  # noqa: E402


def run(c):
    return asyncio.run(c)


RT = "rentamac"


def _iterm_ids():
    """The REAL window manager's truth: the set of iTerm2 window ids right now."""
    r = subprocess.run(
        ["osascript", "-e", 'tell application "iTerm2" to get id of windows'],
        capture_output=True, text=True,
    )
    return {int(x) for x in r.stdout.replace(",", " ").split() if x.strip().isdigit()}


def _iterm_close(wid):
    subprocess.run(
        ["osascript", "-e", f'tell application "iTerm2" to close (first window whose id is {wid})'],
        capture_output=True, text=True,
    )


def _live(name):
    return s._run_tmux(["has-session", "-t", name])[0] == 0


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


def visible_manager_worker():         # real-surface GUI — manager + worker each in a real iTerm2 window on THIS mac
    """Prove the nested-manager pattern is VISIBLE, on the real machine. Open the
    manager and the worker each in a real iTerm2 window, then verify against the
    REAL window manager (not a mock) that exactly two new windows appeared, the
    worker left its green mark, the manager->worker edge is first-class, both
    sessions are live, AND the manager's command actually RAN TO COMPLETION.

    That last check is the lesson real life taught: a manager whose command dies
    (e.g. a shell parse error from a stray apostrophe) still leaves a live idle
    shell — "live" is not "worked." So the manager must leave its own mark; if
    its command broke before writing it, the case goes RED. Real life is where
    the code is proven. macOS + iTerm2 only. EMUX_CTC_HOLD=<seconds> to watch."""
    if sys.platform != "darwin":
        print("visible_manager_worker requires macOS + iTerm2", file=sys.stderr)
        sys.exit(1)
    token = f"GREENMARK-{os.getpid()}-{int(time.time())}"
    mgr_token = f"MGR-OK-{os.getpid()}-{int(time.time())}"
    tmp = pathlib.Path(tempfile.gettempdir())
    proof = tmp / f"emux-visible-{os.getpid()}.txt"
    mgr_proof = tmp / f"emux-visible-mgr-{os.getpid()}.txt"
    proof.unlink(missing_ok=True)
    mgr_proof.unlink(missing_ok=True)
    qp, qmp = shlex.quote(str(proof)), shlex.quote(str(mgr_proof))
    before = _iterm_ids()
    # WORKER window: leaves the green mark, then stays up so it stays visible.
    run(s.tmux_spawn(name="ctc-wrk", gui=True, command=(
        f"printf '=== WORKER ===\\nleaving green mark...\\n'; "
        f"echo {token} > {qp}; printf 'done: %s\\n' {token}; sleep 3600")))
    # MANAGER window: manages the worker (edge first-class), reads its mark, then
    # leaves ITS OWN mark last — proof the whole command ran, not just that a
    # shell is alive. If the command dies early, mgr_proof never appears -> RED.
    run(s.tmux_spawn(name="ctc-mgr", gui=True, manages=["ctc-wrk"], command=(
        "printf '=== MANAGER (drives ctc-wrk) ===\\n'; sleep 1; "
        f"printf 'worker left: '; cat {qp}; echo {mgr_token} > {qmp}; sleep 3600")))
    time.sleep(3)                     # let both windows open + both marks land
    opened = _iterm_ids() - before
    got = proof.read_text().strip() if proof.exists() else ""
    mgr_got = mgr_proof.read_text().strip() if mgr_proof.exists() else ""
    edge = s._load_registry().get("ctc-mgr", {}).get("manages") == ["ctc-wrk"]
    live = _live("ctc-mgr") and _live("ctc-wrk")
    ok = len(opened) == 2 and got == token and mgr_got == mgr_token and edge and live
    hold = int(os.environ.get("EMUX_CTC_HOLD", "0"))
    if hold:
        print(f"holding {hold}s — two iTerm2 windows are open (MANAGER + WORKER); look now")
        time.sleep(hold)
    for wid in opened:                # close exactly the windows we opened, by id
        _iterm_close(wid)
    for sess in ("ctc-mgr", "ctc-wrk"):
        s._run_tmux(["kill-session", "-t", sess])
    proof.unlink(missing_ok=True)
    mgr_proof.unlink(missing_ok=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    {"happy": happy_search_running, "ended": search_finds_ended,
     "ghost": adopt_ghost_not_live, "refusal": drive_dead_refuses,
     "greenmark": manager_worker_greenmark,
     "visible": visible_manager_worker}[sys.argv[1]]()
