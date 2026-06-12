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


def test_http_unknown_route_404(daemon):
    status, body = _get(daemon + "/api/nope")
    assert status == 404
    assert body["error"] == "not_found"
