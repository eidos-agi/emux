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
    calls: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (calls.append(args), (0, "", ""))[1])
    result = web.send_payload("main", "C-c looks like text", literal=True, enter=True)
    assert result["ok"]
    assert calls[0] == ["send-keys", "-t", "main", "-l", "C-c looks like text"]
    assert calls[1] == ["send-keys", "-t", "main", "Enter"]


def test_send_payload_named_key(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    calls: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (calls.append(args), (0, "", ""))[1])
    result = web.send_payload("main", "C-c", literal=False, enter=False)
    assert result["ok"]
    assert calls == [["send-keys", "-t", "main", "C-c"]]


def test_capture_payload_reports_failure(monkeypatch):
    from emux import server, web
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (1, "", "no such session"))
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
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, next(outputs), ""))
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
        "/Users/x/repos-bv/aic-dashboard": "personal",
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
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "x\n", ""))
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
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "pane content here\n", ""))

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
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "x\n", ""))
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
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "x\n", ""))
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
