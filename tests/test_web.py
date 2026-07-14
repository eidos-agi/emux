"""Tests for the emux web daemon.

Does NOT exercise live tmux — tmux calls are monkeypatched. The HTTP tests
start a real ThreadingHTTPServer on an ephemeral port and hit it with urllib,
so routing, JSON encoding, and error paths are tested end to end.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

# ---------- payload helpers ----------

def test_sessions_payload_merges_registry_and_live(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 0, "attached": True},
        {"name": "scratch", "windows": 1, "created_unix": 0, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {
        "claude-prod": {"session": "main", "description": "prod", "tags": ["prod"], "registered_at": 0},
        "old-build": {"session": "gone", "description": None, "tags": [], "registered_at": 0},
    })
    result = web.sessions_payload()
    assert result["ok"]
    by_name = {s["name"]: s for s in result["sessions"]}
    # registered + live
    assert by_name["claude-prod"]["live"] and by_name["claude-prod"]["registered"]
    assert by_name["claude-prod"]["attached"]
    # registered + stale
    assert not by_name["old-build"]["live"]
    # live + unregistered appears under its tmux name
    assert by_name["scratch"]["registered"] is False and by_name["scratch"]["live"]
    # live session already covered by a registry entry is not duplicated
    assert "main" not in by_name


def test_sessions_payload_without_tmux(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)
    result = web.sessions_payload()
    assert result["ok"] is False
    assert result["error"] == "tmux_not_installed"


def test_send_payload_literal_sends_text_then_enter(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_pane_settle", lambda s, h=None: 0.0)   # no paste-settle wait
    calls: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (calls.append(args), (0, "", ""))[1])
    result = web.send_payload("main", "C-c looks like text", literal=True, enter=True)
    assert result["ok"]
    # text and Enter go as SEPARATE send-keys events (so a paste-detecting TUI submits)
    sends = [c for c in calls if c[0] == "send-keys"]
    assert sends == [["send-keys", "-t", "main", "-l", "C-c looks like text"],
                     ["send-keys", "-t", "main", "Enter"]]


def test_send_payload_named_key(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    calls: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (calls.append(args), (0, "", ""))[1])
    result = web.send_payload("main", "C-c", literal=False, enter=False)
    assert result["ok"]
    assert calls == [["send-keys", "-t", "main", "C-c"]]


def test_capture_payload_reports_failure(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (1, "", "no such session"))
    result = web.capture_payload("nope")
    assert result["ok"] is False
    assert result["error"] == "tmux_capture_failed"


def test_poll_once_tracks_activity_then_grid_reads_cache(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 0, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {
        "boss": {"session": "main", "description": None, "tags": ["agents"],
                 "manages": ["worker-1"], "registered_at": 0},
    })
    outputs = iter(["pane v1\n", "pane v1\n", "pane v2\n"])
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (0, next(outputs), ""))
    monkeypatch.setattr(web, "_pane_command", lambda s: "")  # don't consume the capture iterator
    web._ACTIVITY.clear()
    web._CACHE.clear()

    # The daemon's background loop drives activity sampling, one sample per tick.
    web.poll_once()   # baseline (v1)
    web.poll_once()   # unchanged (v1)
    web.poll_once()   # changed (v2)

    item = web.grid_payload()["sessions"][0]  # served from cache, no extra sample
    assert item["content"] == "pane v2\n"
    assert item["manages"] == ["worker-1"]
    assert item["changed"] is True
    assert item["last_change_age"] is not None and item["last_change_age"] < 2
    assert item["activity"] == [0, 0, 1]


def test_grid_payload_stale_session_has_no_capture(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [])
    monkeypatch.setattr(server, "_load_registry", lambda: {
        "old": {"session": "gone", "description": None, "tags": [], "registered_at": 0},
    })
    web._ACTIVITY.clear()
    web._CACHE.clear()
    item = web.grid_payload()["sessions"][0]
    assert item["live"] is False
    assert item["content"] == ""
    assert item["activity"] == []


def test_detect_company_maps_cwd_to_company():
    from emux import web
    cases = {
        "/Users/x/repos-eidos-agi/helios": "eidos",
        "/Users/x/repos-eidos-capital/v0": "eidos",
        "/Users/x/repos-greenmark/university": "greenmark",
        "/Users/x/repos-aic-holdings/foo": "aic",
        "/Users/x/repos-personal/notes": "personal",
        "/Users/x/repos-bv/aic-dashboard": "boone",   # root beats the aic- keyword
        # company repos that live in the GENERIC ~/repos/ tree — keyword fallback
        "/Users/x/repos/greenmark-claude-toolkit": "greenmark",
        "/Users/x/repos/eidos-scratch": "eidos",
        "/Users/x/some/other/path": "",
        None: "",
    }
    for cwd, want in cases.items():
        assert web._detect_company(cwd)["company"] == want, cwd


def test_normalize_collapses_spinner_and_cursor_noise():
    from emux import web
    # A braille thinking spinner and trailing whitespace are not real change.
    assert web._normalize("working ⠋ \nrest") == web._normalize("working ⠙\nrest")
    assert web._normalize("line   \n\n\n") == web._normalize("line\n")


def test_observe_ignores_spinner_frame_change():
    from emux import web
    web._ACTIVITY.clear()
    web._observe("s", "Thinking ⠋\nidle")
    meta = web._observe("s", "Thinking ⠹\nidle")  # only the spinner advanced
    assert meta["changed"] is False
    real = web._observe("s", "Done.\nidle")
    assert real["changed"] is True


def test_poll_once_evicts_dead_sessions(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (0, "x\n", ""))
    web._ACTIVITY.clear()
    web._CACHE.clear()

    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 0, "attached": False},
    ])
    web.poll_once()
    assert "main" in web._ACTIVITY and "main" in web._CACHE

    monkeypatch.setattr(server, "_live_sessions", lambda: [])  # tmux reaped it
    web.poll_once()
    assert "main" not in web._ACTIVITY and "main" not in web._CACHE


# ---------- HTTP round trip ----------

@pytest.fixture()
def daemon(monkeypatch):
    """Run the web handler on an ephemeral localhost port; tmux is mocked out."""
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 0, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (0, "pane content here\n", ""))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.EmuxWebHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(url) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if "json" in r.headers.get("Content-Type", "") else body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_serves_ui(daemon):
    status, body = _get(daemon + "/")
    assert status == 200
    assert "EMUX" in body and "control room" in body


def test_http_sessions(daemon):
    status, body = _get(daemon + "/api/sessions")
    assert status == 200
    assert body["ok"] and body["sessions"][0]["name"] == "main"


def test_http_capture(daemon):
    status, body = _get(daemon + "/api/capture?session=main&lines=50")
    assert status == 200
    assert body["ok"] and "pane content here" in body["content"]


def test_http_capture_requires_session(daemon):
    status, body = _get(daemon + "/api/capture")
    assert status == 400
    assert body["error"] == "missing_session"


def test_http_send(daemon):
    req = urllib.request.Request(
        daemon + "/api/send",
        data=json.dumps({"session": "main", "keys": "echo hi"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        body = json.loads(r.read().decode())
    assert body["ok"] and body["sent"] == "echo hi"


def test_http_send_rejects_bad_body(daemon):
    req = urllib.request.Request(
        daemon + "/api/send",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read().decode())["error"] == "bad_json"


def test_http_grid(daemon):
    status, body = _get(daemon + "/api/grid?lines=10")
    assert status == 200
    assert body["ok"]
    first = body["sessions"][0]
    assert "content" in first and "activity" in first and "manages" in first


def test_http_unknown_route_404(daemon):
    status, body = _get(daemon + "/api/nope")
    assert status == 404
    assert body["error"] == "not_found"


def test_http_rejects_foreign_host(daemon):
    # DNS-rebind defense: a request whose Host isn't loopback is refused.
    req = urllib.request.Request(daemon + "/api/sessions", headers={"Host": "attacker.example"})
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403
        assert json.loads(e.read().decode())["error"] == "forbidden_host"


def test_http_send_rejects_cross_origin(daemon):
    # CSRF defense: a POST carrying a foreign Origin (forged by another site) is refused.
    req = urllib.request.Request(
        daemon + "/api/send",
        data=json.dumps({"session": "main", "keys": "rm -rf ~"}).encode(),
        headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403
        assert json.loads(e.read().decode())["error"] == "forbidden_origin"


def test_http_send_allows_same_origin(daemon):
    # A same-origin Origin header (the emux UI itself) is allowed through.
    base = daemon
    req = urllib.request.Request(
        base + "/api/send",
        data=json.dumps({"session": "main", "keys": "echo hi"}).encode(),
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read().decode())["ok"]


def test_http_healthz_is_unguarded(daemon):
    # /healthz must answer even with a foreign Host (it leaks nothing).
    req = urllib.request.Request(daemon + "/healthz", headers={"Host": "monitoring.example"})
    with urllib.request.urlopen(req) as r:
        body = json.loads(r.read().decode())
    assert body["ok"] and "version" in body and "live_sessions" in body


def test_sessions_payload_includes_created_unix(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 1700000000, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    item = web.sessions_payload()["sessions"][0]
    assert item["created_unix"] == 1700000000


def test_detect_agent_from_pane_command(monkeypatch):
    from emux import web
    monkeypatch.setattr(web, "_pane_command", lambda s: "claude")
    assert _agent_key(web._detect_agent("s", "")) == "claude"
    monkeypatch.setattr(web, "_pane_command", lambda s: "codex")
    assert _agent_key(web._detect_agent("s", "")) == "codex"
    monkeypatch.setattr(web, "_pane_command", lambda s: "zsh")
    assert _agent_key(web._detect_agent("s", "")) == "shell"


def test_detect_agent_falls_back_to_content_signature(monkeypatch):
    from emux import web
    # A node-wrapped CLI reports "node" as the process; the content disambiguates.
    monkeypatch.setattr(web, "_pane_command", lambda s: "node")
    a = web._detect_agent("s", "Welcome to Gemini CLI — type a prompt")
    assert _agent_key(a) == "gemini" and a["glyph"]


def test_grid_payload_attaches_agent(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "main", "windows": 1, "created_unix": 0, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (0, "x\n", ""))
    monkeypatch.setattr(web, "_pane_command", lambda s: "claude")
    web._ACTIVITY.clear()
    web._CACHE.clear()
    item = web.grid_payload()["sessions"][0]
    assert item["agent"]["agent"] == "claude" and item["agent"]["label"] == "Claude Code"


def _agent_key(a):
    return a["agent"]


def test_flow_handles_recursive_manages_cycle(monkeypatch):
    """The simplest recursion: two agents that manage each other (A→B→A).

    The flow view's level-assignment BFS guards against exactly this — a cycle
    has no in-degree-0 root, so naive layering would loop forever. This test
    pins the data layer that feeds the view: grid_payload must carry BOTH
    directions of the loop so the flow renderer can draw (and cycle-guard) it.
    Live-verified separately that the browser renders the loop without hanging.
    """
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [
        {"name": "a", "windows": 1, "created_unix": 0, "attached": False},
        {"name": "b", "windows": 1, "created_unix": 0, "attached": False},
    ])
    monkeypatch.setattr(server, "_load_registry", lambda: {
        "a": {"session": "a", "description": None, "tags": [], "manages": ["b"], "registered_at": 0},
        "b": {"session": "b", "description": None, "tags": [], "manages": ["a"], "registered_at": 0},
    })
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10, host=None: (0, "x\n", ""))
    web._ACTIVITY.clear()
    web._CACHE.clear()

    by_name = {s["name"]: s for s in web.grid_payload()["sessions"]}
    # Both halves of the cycle are present → the renderer sees a true loop.
    assert by_name["a"]["manages"] == ["b"]
    assert by_name["b"]["manages"] == ["a"]
    # Every node is both a manager and managed → no in-degree-0 root exists,
    # which is precisely the case the flow BFS must guard against.
    targets = {t for s in by_name.values() for t in s["manages"]}
    assert targets == {"a", "b"}


def test_launchd_plist_is_well_formed():
    from emux import web
    plist = web.launchd_plist(port=9999)
    assert "com.eidos.emux-web" in plist
    assert "<string>9999</string>" in plist
    assert plist.strip().startswith("<?xml")


def test_agent_registry_routes_on_capability_not_price():
    from emux import agents
    # long unattended coding loop -> codex; judgment/refactor -> claude
    assert agents.advise("leave a long autonomous build running overnight")["agent"] == "codex"
    assert agents.advise("refactor the auth module and fix the failing tests")["agent"] == "claude"
    assert agents.advise("plan the next sprint")["agent"] == "claude"
    # a second opinion should NOT come from the same model that did the work
    assert agents.advise("get a second opinion / cross-check this design")["agent"] == "codex"
    # unknown scenario still answers, but says it guessed
    d = agents.advise("xyzzy")
    assert d["agent"] == "claude" and d["matched"] is False
    # both defaults are flat-fee: price is not a routing axis here
    t = agents.table()
    assert t["agents"]["claude"]["access"] == "subscription"
    assert t["agents"]["codex"]["access"] == "subscription"
    # and the metered-API route is recorded as forbidden, so it can't be re-litigated
    assert any(n["verdict"] == "FORBIDDEN" for n in t["notes"])


def test_manager_inherits_company_from_the_worker_it_manages(monkeypatch):
    # A manager is defined by what it supervises, not where its process runs.
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [])
    monkeypatch.setattr(server, "_load_registry", lambda: {
        # manager's cwd would derive Eidos, but it manages a Greenmark worker
        "mgr": {"session": "mgr", "cwd": "/Users/x/repos-eidos-agi/emux",
                "manages": ["wrk"], "tags": [], "registered_at": 0},
        "wrk": {"session": "wrk", "company": "greenmark",  # explicit (remote, no cwd)
                "manages": [], "tags": [], "registered_at": 0},
    })
    by = {s["name"]: s for s in web.sessions_payload()["sessions"]}
    assert by["wrk"]["company"]["company"] == "greenmark"
    assert by["mgr"]["company"]["company"] == "greenmark"   # inherited
    assert "_co_explicit" not in by["mgr"]                  # temp flag cleaned up


def test_gated_worker_escalates_once_per_gate_low_risk_head(monkeypatch, tmp_path):
    """A blocked worker escalates once per gate (rearmed on clear): the NEED
    signal fires, and a Hancock request queuing `emux head` is filed at LOW
    risk — so hancock's `emux head` allow rule auto-runs it and the terminal
    just opens, instead of a high-risk request waiting for a signature."""
    import shutil
    import subprocess

    from emux import server, web
    signals = []
    monkeypatch.setattr(server, "inject_signal",
                        lambda session, kind, payload="", **kw:
                        signals.append({"session": session, "kind": kind,
                                        "payload": payload}))
    calls = []
    fake_hancock = tmp_path / "hancock"
    fake_hancock.write_text("#!/bin/sh\n")
    fake_hancock.chmod(0o755)
    monkeypatch.setattr(shutil, "which",
                        lambda n: str(fake_hancock) if n == "hancock" else None)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, env=None, **kw):
        calls.append({"cmd": cmd, "env": env})
        return _R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDECODE", "1")   # the daemon may inherit this
    monkeypatch.setattr(web, "_GATE_LOG_PATH", tmp_path / "gates.jsonl")
    monkeypatch.setattr(web, "_GATE_POLICY_PATH", tmp_path / "gatepolicy.json")

    gate = "Update available!\n1. Update now (runs brew upgrade)\n2. Skip"
    web._ESCALATED.clear()
    web._AUTO_ANSWERED.clear()
    web._escalate_if_gated("wrk", "codex", gate)
    web._escalate_if_gated("wrk", "codex", gate)   # same gate again → no repeat

    assert len(signals) == 1 and signals[0]["kind"] == "NEED"
    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert "emux head wrk" in cmd
    assert cmd[cmd.index("-risk") + 1] == "low"    # low ⇒ allow rule auto-runs it
    assert "CLAUDECODE" not in (calls[0]["env"] or {})   # CC guard env scrubbed

    # gate clears → rearm → a new gate escalates again
    web._escalate_if_gated("wrk", "codex", "› write some code")   # not a gate
    web._escalate_if_gated("wrk", "codex", gate)
    assert len(signals) == 2 and len(calls) == 2


def _wire_gate_policy(monkeypatch, tmp_path, rules):
    import json as _json

    from emux import server, web
    policy = tmp_path / "gatepolicy.json"
    policy.write_text(_json.dumps({"rules": rules}))
    monkeypatch.setattr(web, "_GATE_POLICY_PATH", policy)
    monkeypatch.setattr(web, "_GATE_LOG_PATH", tmp_path / "gates.jsonl")
    signals = []
    monkeypatch.setattr(server, "inject_signal",
                        lambda session, kind, payload="", **kw:
                        signals.append({"kind": kind, "payload": payload}))
    sent = []
    monkeypatch.setattr(server, "_run_tmux",
                        lambda args, timeout=10, host=None:
                        (sent.append({"args": list(args), "host": host}), (0, "", ""))[1])
    monkeypatch.setattr(web, "_file_hancock_escalation", lambda *a, **k: None)
    web._ESCALATED.clear()
    web._AUTO_ANSWERED.clear()
    return signals, sent


def test_gate_policy_auto_answers_matching_gate(monkeypatch, tmp_path):
    """A policy rule answers a known gate with deterministic keystrokes — no
    NEED signal, no hancock, no model, no human."""
    from emux import web
    signals, sent = _wire_gate_policy(
        monkeypatch, tmp_path,
        [{"pattern": r"trust this folder", "keys": ["Enter"], "note": "trust gate"}])
    gate_screen = ("Quick safety check: Is this a project you created?\n"
                   "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
                   "Do you want to proceed?\n")
    web._escalate_if_gated("wrk", "claude", gate_screen, host="box-1")
    assert sent and sent[0]["args"][:3] == ["send-keys", "-t", "wrk"]
    assert sent[0]["args"][3:] == ["Enter"]
    assert sent[0]["host"] == "box-1"      # host-aware answering
    assert signals == []                    # answered, not escalated
    ledger = (tmp_path / "gates.jsonl").read_text()
    assert '"action": "auto"' in ledger


def test_gate_policy_one_attempt_then_escalates(monkeypatch, tmp_path):
    """If the auto-answer didn't clear the gate, the SAME gate next poll goes
    to a human — no answer-retry loops."""
    from emux import web
    signals, sent = _wire_gate_policy(
        monkeypatch, tmp_path,
        [{"pattern": r"trust this folder", "keys": ["Enter"]}])
    gate_screen = "❯ 1. Yes, I trust this folder\nDo you want to proceed?\n"
    web._escalate_if_gated("wrk", "claude", gate_screen)
    assert len(sent) == 1 and signals == []
    web._escalate_if_gated("wrk", "claude", gate_screen)   # gate still up
    assert len([s for s in sent if s["args"][0] == "send-keys"]) == 1  # no re-answer
    assert len(signals) == 1 and signals[0]["kind"] == "NEED"


def test_answered_gate_in_scrollback_does_not_reescalate(monkeypatch, tmp_path):
    """After an answer, the dialog's text lingers in the capture ABOVE fresh
    output. Detection judges the live bottom only — an answered gate must
    clear, not re-escalate (found live by the fraude gate test)."""
    from emux import web
    signals, sent = _wire_gate_policy(
        monkeypatch, tmp_path,
        [{"pattern": r"fraude-marker", "keys": ["Enter"]}])
    gate_screen = "Tool use: fraude-marker\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
    web._escalate_if_gated("wrk", "claude", gate_screen)
    assert len(sent) == 1 and signals == []          # auto-answered
    # the gate text is still in scrollback, but 10+ fresh lines follow it
    after = gate_screen + "\n".join(f"working on step {i}" for i in range(12)) + "\n"
    web._escalate_if_gated("wrk", "claude", after)
    assert signals == []                              # cleared, not escalated
    assert "wrk" not in web._ESCALATED and "wrk" not in web._AUTO_ANSWERED  # rearmed


def test_gate_policy_never_answers_destructive(monkeypatch, tmp_path):
    """Destructive text on the gate always goes to a human, whatever the rules."""
    from emux import web
    signals, sent = _wire_gate_policy(
        monkeypatch, tmp_path,
        [{"pattern": r"do you want to proceed", "keys": ["Enter"]}])
    gate_screen = ("About to run: rm -rf /var/lib/data\n"
                   "Do you want to proceed?\n❯ 1. Yes\n")
    web._escalate_if_gated("wrk", "claude", gate_screen)
    assert not [s for s in sent if s["args"][0] == "send-keys"]
    assert len(signals) == 1 and signals[0]["kind"] == "NEED"


def test_remote_session_reads_live_and_captures_over_ssh(monkeypatch):
    """A registered session on another host must show LIVE (not 'gone') and
    capture over ssh — the daemon only sees local tmux otherwise."""
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda: [])   # nothing LOCAL
    monkeypatch.setattr(server, "_load_registry", lambda: {
        "remote-wrk": {"session": "wrk", "host": "rentamac",
                       "manages": [], "tags": [], "registered_at": 0},
    })
    calls = []
    def fake_tmux(args, timeout=10, host=None):
        calls.append((args[0], host))
        if args[0] == "ls":
            return (0, "wrk\nother\n", "") if host == "rentamac" else (0, "", "")
        if args[0] == "capture-pane":
            return (0, "remote pane content\n", "")
        return (0, "", "")
    monkeypatch.setattr(server, "_run_tmux", fake_tmux)
    web._RLIVE_CACHE.clear()

    # liveness: the remote ls was consulted, and the session reads LIVE
    s = next(x for x in web.sessions_payload()["sessions"] if x["name"] == "remote-wrk")
    assert s["live"] is True and s["host"] == "rentamac"
    assert ("ls", "rentamac") in calls

    # capture routes over ssh with the resolved host
    cap = web.capture_payload("wrk", 10, host=web._session_host("wrk"))
    assert cap["ok"] and cap["host"] == "rentamac" and "remote pane" in cap["content"]
    assert ("capture-pane", "rentamac") in calls


def test_bm25_ranks_the_session_the_intent_describes_to_the_top():
    from emux import web
    sessions = [
        {"name": "ggo-build", "path": "/Volumes/GREENMARK/eidos-spreadsheet-explorer"},
        {"name": "cerebro-claude", "path": "/Volumes/GREENMARK/cerebro-registry-workbench/reconcile"},
        {"name": "api-server", "path": "/Users/x/repos/api"},
    ]
    ranked = web._bm25_rank("pick up the greenmark reconcile work", sessions)
    assert ranked[0]["name"] == "cerebro-claude"          # reconcile in the path
    assert ranked[0]["_relevance"] > ranked[1]["_relevance"]
    assert ranked[-1]["name"] == "api-server"             # irrelevant → last
    # description + tags feed the ranking too
    s2 = [{"name": "w1", "path": "/x", "description": "hancock auth work", "tags": []},
          {"name": "w2", "path": "/y", "description": "", "tags": ["greenmark"]}]
    assert web._bm25_rank("the hancock auth session", s2)[0]["name"] == "w1"


def test_intent_routing_and_kickstart():
    from emux import web
    # a company named in the wording is detected → routes machine + dir
    assert web._intent_company_hint("an Eidos digest of the org")[0] == "eidos"
    assert web._intent_company_hint("fix the greenmark reconcile")[0] == "greenmark"
    assert web._intent_company_hint("just a scratch shell") is None
    # standing routing preference (durable, overridable) — Eidos → the mac-mini
    assert web._routing_prefs()["company_host"]["eidos"] == "daniels-mac-mini"


def test_spawn_kickstarts_the_agent_with_the_intent(monkeypatch):
    from emux import server, web
    seen = {}
    async def fake_spawn(**kw):
        seen.update(kw)
        return {"ok": True, "name": kw["name"]}
    monkeypatch.setattr(server, "tmux_spawn", fake_spawn)
    # an agent command + an intent → the intent becomes the agent's opening prompt
    r = web._spawn_session({"name": "x", "command": "claude", "prompt": "build the thing"})
    assert r["kickstarted"] is True
    assert seen["command"] == "claude 'build the thing'"
    # a PLAIN SHELL is not an agent → no kickstart, command untouched
    seen.clear()
    r = web._spawn_session({"name": "y", "command": "", "prompt": "build the thing"})
    assert r.get("kickstarted") is False


def test_cheap_summarizer_reads_the_agent_not_the_chrome():
    from emux import web
    # prefers the agent's ⏺ action line over UI notices/spinners below it
    pane = ("⏺ Refactored the auth module and added the missing test\n"
            "✻ Cooked for 13m 59s\n"
            "  Update available! Run: brew upgrade claude-code@latest\n"
            "─────\n❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n")
    assert web._headline(pane) == "Refactored the auth module and added the missing test"
    s = web._summarize("Claude Code", "idle", pane)
    assert s.startswith("idle — Refactored the auth")
    # spinner meters, tips, menu items, shell prompts are all noise
    for noise in ["Brewed for 18s", "Tip: paste images with control+v",
                  "  3. Skip until next version", "user@box repo %",
                  "  Update available! Run: brew upgrade"]:
        assert web._headline(noise) == "", noise


def test_asking_state_summary_and_question_detection():
    from emux import web
    pane = "⏺ Say the word on the routing proposal and I'll implement it.\n───\n❯ \n"
    assert web._looks_like_question(pane) is True
    assert web._quick_state("claude", pane, False) == "asking"


def test_menu_parses_to_clickable_options_not_prose():
    from emux import web
    menu = ("❯ 1. Expose behind SSO\n  2. Expose open\n  3. Don't expose\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel")
    opts = web._parse_options(menu)
    assert [o["n"] for o in opts] == [1, 2, 3]
    assert opts[0]["selected"] is True and opts[1]["selected"] is False
    # a numbered list in prose (no menu hint, no cursor) must NOT become bubbles
    assert web._parse_options("Steps:\n1. a\n2. b\n3. c\nDone.") == []
    # a claude selection menu is now a gate (marches ants)
    assert web._detect_agent  # sanity


def test_gist_cache_hits_and_busts(monkeypatch):
    """The gist is cached by pane hash: an unchanged pane serves the cache
    (no recompute); a changed pane recomputes; content-hash is the bust key."""
    from emux import web

    web._GIST_CACHE.clear()
    calls = {"n": 0}
    pane = {"content": "AGENT: doing work\nline2\n"}
    monkeypatch.setattr(web, "capture_payload",
                        lambda s, n, host=None: {"ok": True, "content": pane["content"]})

    def fake_compute(_pane):
        calls["n"] += 1
        return {"ok": True, "digest": "d",
                "suggestions": [{"text": "go", "confidence": 80}]}
    monkeypatch.setattr(web, "_compute_gist", fake_compute)

    r1 = web._reply_suggestions("s1", None)          # cold → compute
    r2 = web._reply_suggestions("s1", None)          # same pane → cache
    pane["content"] = "AGENT: DIFFERENT now\n"        # pane changed → bust
    r3 = web._reply_suggestions("s1", None)

    assert calls["n"] == 2                            # the identical 2nd call did NOT recompute
    assert r2.get("cached") is True
    assert r1.get("cached") is None and r3.get("ok") is True
    # force=True always recomputes even on a cache hit
    web._reply_suggestions("s1", None, force=True)
    assert calls["n"] == 3


def test_should_warm_gist_pause_and_dedup():
    """Warm only when settled AND still for the pause AND content is new."""
    from emux import web
    P = web._GIST_PAUSE_SECS
    # settled but not still long enough -> no warm (skips between-turns flicker)
    assert web._should_warm_gist("idle", P - 1, "n1", None, False) is False
    # settled + still past the pause + new content -> warm
    assert web._should_warm_gist("idle", P + 1, "n1", None, False) is True
    # same content already warmed -> no re-warm
    assert web._should_warm_gist("idle", P + 5, "n1", "n1", False) is False
    # new content after a change -> warm again
    assert web._should_warm_gist("idle", P + 1, "n2", "n1", False) is True
    # still running (not settled) -> never warm
    assert web._should_warm_gist("running", P + 9, "n3", None, False) is False
    # a warm already in flight -> don't double-fire
    assert web._should_warm_gist("idle", P + 1, "n4", None, True) is False
    # no recorded change age -> can't judge stillness -> no proactive warm
    assert web._should_warm_gist("idle", None, "n5", None, False) is False


def test_cost_overrun_detection():
    """Detects usage/rate/quota/cost limits at the live bottom; ignores benign text."""
    from emux import web
    hit = [
        "Claude usage limit reached. Your limit will reset at 5pm.",
        "Error: 429 Too Many Requests",
        "You've reached your plan limit — upgrade to increase your usage.",
        "rate_limit_error: too many requests",
        "Insufficient credits remaining.",
    ]
    miss = [
        "Running tests, all green. No limit issues.",       # 'limit' but benign
        "def clamp(x): return max(0, min(100, x))",
        "The rate of progress is good; committing now.",     # 'rate' but not rate-limit
        "",
    ]
    for c in hit:
        assert web._cost_overrun("work\n" + c) is True, c
    for c in miss:
        assert web._cost_overrun("work\n" + c) is False, c


def test_plan_failover_facade(monkeypatch):
    """Round-robin to the next account, skip cooling-down ones, dry-run yields the
    exact relaunch command, and a single account can't fail over."""
    import time

    from emux import web

    PLANS = [{"name": "acct-1", "config_dir": "/x/.claude"},
             {"name": "acct-2", "config_dir": "/x/.claude-2"},
             {"name": "acct-3", "config_dir": "/x/.claude-3"}]
    monkeypatch.setattr(web, "_plans", lambda: PLANS)
    web._SESSION_PLAN.clear()
    web._PLAN_EXHAUSTED.clear()
    now = time.time()

    assert web._next_plan("acct-1", now)["name"] == "acct-2"
    web._PLAN_EXHAUSTED["acct-2"] = now + 9999          # cooling down → skip it
    assert web._next_plan("acct-1", now)["name"] == "acct-3"

    r = web._switch_plan("mgr", dry_run=True)
    assert r["ok"] and r["to"] == "acct-3"
    assert r["relaunch"] == "CLAUDE_CONFIG_DIR=/x/.claude-3 claude -c"
    assert web._switch_plan("mgr", to="acct-2", dry_run=True)["to"] == "acct-2"

    # a single configured account can't fail over
    monkeypatch.setattr(web, "_plans", lambda: [{"name": "d", "config_dir": "/x/.claude"}])
    assert web._switch_plan("s", dry_run=True)["ok"] is False
