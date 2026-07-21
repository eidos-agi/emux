"""EID-875 — goal loop mechanics. Ground truth is the verifier, not the model's
self-report; the loop stops on goal_met / stalled / budget / max_iters."""

from __future__ import annotations

from emux import goal_loop as gl
from emux import structured_driver as sd


def _driver(monkeypatch, results):
    calls = {"n": 0}
    def fake_drive(prompt, cwd, **kw):
        i = calls["n"]; calls["n"] += 1
        r = results[min(i, len(results) - 1)]
        return sd.DriveResult(ok=True, result="ok", stop_reason="end_turn",
                              session_id="s", cost_usd=r.get("cost", 0.01),
                              raw={"usage": {"input_tokens": r.get("tok", 100)}})
    monkeypatch.setattr(sd, "drive", fake_drive)
    return calls


def _verifier(monkeypatch, passes_on, tail_seq=None):
    state = {"n": 0}
    def fake_verify(cmd, cwd, timeout=120.0):
        i = state["n"]; state["n"] += 1
        tail = (tail_seq[min(i, len(tail_seq)-1)] if tail_seq else f"fail-{i}")
        return (i + 1 >= passes_on, tail)
    monkeypatch.setattr(gl, "_verify", fake_verify)


def test_stops_on_verified_goal(monkeypatch):
    _driver(monkeypatch, [{"cost": 0.02, "tok": 100}, {"cost": 0.03, "tok": 250}, {"cost": 0.04, "tok": 500}])
    _verifier(monkeypatch, passes_on=3)   # verifier passes on the 3rd check
    run = gl.run_goal("g", "/w", "pytest", identity="d", provision=False)
    assert run.reason == "goal_met" and run.verified
    assert len(run.iterations) == 3
    assert round(run.total_cost_usd, 2) == 0.09
    # context-growth signal is captured and climbs
    toks = [it.input_tokens for it in run.iterations]
    assert toks == [100, 250, 500]


def test_stops_on_stall(monkeypatch):
    _driver(monkeypatch, [{"cost": 0.01}])
    _verifier(monkeypatch, passes_on=99, tail_seq=["same-error"])  # identical failure forever
    run = gl.run_goal("g", "/w", "pytest", identity="d", provision=False, stall_limit=3)
    assert run.reason == "stalled" and not run.verified
    # iter1 sets baseline; iters 2,3,4 are identical-to-previous → stall on the 4th
    assert len(run.iterations) == 4


def test_stops_on_budget(monkeypatch):
    _driver(monkeypatch, [{"cost": 3.0}])   # each turn costs 3
    _verifier(monkeypatch, passes_on=99, tail_seq=["e1", "e2", "e3", "e4"])
    run = gl.run_goal("g", "/w", "pytest", identity="d", provision=False, max_cost_usd=5.0, stall_limit=99)
    assert run.reason == "budget"
    assert run.total_cost_usd >= 5.0


def test_stops_on_max_iters(monkeypatch):
    _driver(monkeypatch, [{"cost": 0.01}])
    # never passes, never stalls (unique tail each time), cheap
    _verifier(monkeypatch, passes_on=99, tail_seq=[f"e{i}" for i in range(50)])
    run = gl.run_goal("g", "/w", "pytest", identity="d", provision=False, max_iters=5, stall_limit=99)
    assert run.reason == "max_iters" and len(run.iterations) == 5
