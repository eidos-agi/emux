"""EID-875 — structured driver. Parse Claude Code's JSON result into an
authoritative completion (no screen scrape, no keystroke, no sleep-and-guess)."""

from __future__ import annotations

import json
import subprocess

from emux import structured_driver as sd

RESULT_OK = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "STRUCTURED-OK", "stop_reason": "end_turn",
    "session_id": "sess-1", "total_cost_usd": 0.01, "num_turns": 1,
}


def _fake_run(stdout: str, returncode: int = 0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
    return run


def test_parses_authoritative_success(monkeypatch):
    captured = {}
    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        captured["identity"] = kw.get("env", {}).get("EMUX_IDENTITY")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(RESULT_OK), stderr="")
    monkeypatch.setattr(subprocess, "run", run)

    r = sd.drive("do a thing", "/work", identity="daniel", server_id="e1")
    assert r.ok and r.result == "STRUCTURED-OK"
    assert r.stop_reason == "end_turn" and r.session_id == "sess-1" and r.cost_usd == 0.01
    # structured surface, not a screen: json output + hook settings + as identity
    assert "--output-format" in captured["cmd"] and "json" in captured["cmd"]
    assert "--settings" in captured["cmd"]
    assert captured["identity"] == "daniel" and captured["cwd"] == "/work"


def test_hook_settings_wires_delegation_pretooluse(monkeypatch):
    captured = {}
    def run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(RESULT_OK), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    sd.drive("x", "/w", identity="d", python="/usr/bin/python3")
    settings = json.loads(captured["cmd"][captured["cmd"].index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook == "/usr/bin/python3 -m emux.hook_delegation"


def test_with_hook_false_omits_settings(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (captured.__setitem__("cmd", cmd) or
        subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(RESULT_OK), stderr="")))
    sd.drive("x", "/w", identity="d", with_hook=False)
    assert "--settings" not in captured["cmd"]


def test_resume_threads_the_session(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (captured.__setitem__("cmd", cmd) or
        subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(RESULT_OK), stderr="")))
    sd.drive("next", "/w", identity="d", resume_session="sess-1")
    assert "--resume" in captured["cmd"] and "sess-1" in captured["cmd"]


def test_error_result_is_not_ok(monkeypatch):
    err = {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": None}
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(err)))
    r = sd.drive("x", "/w", identity="d")
    assert not r.ok and r.is_error and r.error == "error_max_turns"


def test_timeout_and_empty_and_unparseable_fail_closed(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(subprocess, "run", boom)
    assert sd.drive("x", "/w", identity="d").error == "timeout"

    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    assert sd.drive("x", "/w", identity="d").error == "no_output"

    monkeypatch.setattr(subprocess, "run", _fake_run("not json"))
    assert sd.drive("x", "/w", identity="d").error == "unparseable_output"


# EID-881 — the drive wrapper emits lifecycle receipts to the ledger (opt-in).

def test_drive_emits_lifecycle_receipts(monkeypatch):
    from emux import mgmt_ledger as ml
    lg = ml.Ledger()
    monkeypatch.setattr(sd, "_drive_impl", lambda *a, **k: sd.DriveResult(ok=True, result="done"))
    sd.drive("task", "/w", identity="d", ledger=lg, receipt_task="sess-1")
    st = lg.state("sess-1")
    assert {"dispatch", "worker_started", "outcome_verified"} <= st.stages and not st.failed
    assert ml.ui_state(st) is None                       # complete → defer to classifier


def test_drive_records_failure_receipt(monkeypatch):
    from emux import mgmt_ledger as ml
    lg = ml.Ledger()
    monkeypatch.setattr(sd, "_drive_impl", lambda *a, **k: sd.DriveResult(ok=False, error="boom"))
    sd.drive("task", "/w", identity="d", ledger=lg, receipt_task="sess-2")
    st = lg.state("sess-2")
    assert st.failed and ml.ui_state(st) == "failed"


def test_drive_without_ledger_is_transparent(monkeypatch):
    monkeypatch.setattr(sd, "_drive_impl", lambda *a, **k: sd.DriveResult(ok=True, result="ok"))
    r = sd.drive("task", "/w", identity="d")             # no ledger → no receipts, unchanged
    assert r.ok and r.result == "ok"
