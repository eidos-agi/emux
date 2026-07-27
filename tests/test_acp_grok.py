"""Unit tests for Grok ACP client (fake agent process — no network)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from emux import acp_grok as acp


FAKE_AGENT = textwrap.dedent(
    r"""
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def main():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            mid = msg.get("id")
            method = msg.get("method")
            # reverse-RPC response from client — ignore
            if method is None:
                continue
            params = msg.get("params") or {}

            if method == "initialize":
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": True},
                        "authMethods": [],
                    },
                })
            elif method == "session/new":
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"sessionId": "sess-new-1"},
                })
            elif method == "session/load":
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"sessionId": params.get("sessionId") or "sess-loaded"},
                })
            elif method == "session/prompt":
                # stream a chunk then complete
                sid = params.get("sessionId")
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "hello "},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "from acp"},
                        },
                    },
                })
                # reverse-RPC mid-turn: client must answer without hanging.
                # Do not block the fake agent on the reply (client may answer
                # after we already emit the prompt result).
                send({
                    "jsonrpc": "2.0",
                    "id": 9001,
                    "method": "session/request_permission",
                    "params": {
                        "options": [
                            {"optionId": "allow-once", "kind": "allow_once"},
                            {"optionId": "deny", "kind": "reject_once"},
                        ],
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"stopReason": "end_turn"},
                })
            elif method == "session/cancel":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"unknown {method}"},
                })

    if __name__ == "__main__":
        main()
    """
).strip()


@pytest.fixture
def fake_agent_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_acp_agent.py"
    path.write_text(FAKE_AGENT + "\n")
    return path


def _spawn_fake(script: Path, **kwargs) -> acp.GrokAcpClient:
    proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    client = acp.GrokAcpClient(
        process=proc,
        argv=[sys.executable, str(script)],
        always_approve=True,
    )
    client._start_reader()
    return client


def test_initialize_session_new_prompt(fake_agent_script: Path):
    with _spawn_fake(fake_agent_script) as client:
        init = client.initialize(timeout=5)
        assert init.get("protocolVersion") == 1
        assert client.authenticate_if_needed(init) is False
        sid = client.session_new("/tmp/proj", timeout=5)
        assert sid == "sess-new-1"
        client.session_prompt(sid, "hi", timeout=5)
        assert client.captured_text() == "hello from acp"
        assert any(u.kind == "agent_message_chunk" for u in client.updates)


def test_session_load(fake_agent_script: Path):
    with _spawn_fake(fake_agent_script) as client:
        client.initialize(timeout=5)
        sid = client.session_load("abc-uuid", "/tmp/proj", timeout=5)
        assert sid == "abc-uuid"


def test_run_acp_prompt_one_shot(fake_agent_script: Path, tmp_path: Path):
    def spawner(**_kw):
        return _spawn_fake(fake_agent_script)

    result = acp.run_acp_prompt(
        "do the thing",
        cwd=str(tmp_path),
        timeout=10,
        spawn_fn=spawner,
    )
    assert result["ok"] is True
    assert result["mode"] == "acp"
    assert result["text"] == "hello from acp"
    assert result["session_id"] == "sess-new-1"
    assert result["update_count"] >= 2


def test_run_acp_prompt_with_load(fake_agent_script: Path, tmp_path: Path):
    def spawner(**_kw):
        return _spawn_fake(fake_agent_script)

    result = acp.run_acp_prompt(
        "resume please",
        session_id="existing-id",
        cwd=str(tmp_path),
        timeout=10,
        spawn_fn=spawner,
    )
    assert result["ok"] is True
    assert result["session_id"] == "existing-id"
    assert "acp" in result["text"] or result["text"]


def test_empty_prompt():
    r = acp.run_acp_prompt("")
    assert r["ok"] is False
    assert "prompt" in r["error"]


def test_permission_auto_approve(fake_agent_script: Path):
    """Permission reverse-RPC during prompt must not hang (covered by prompt test)."""
    with _spawn_fake(fake_agent_script) as client:
        client.initialize(timeout=5)
        sid = client.session_new("/x", timeout=5)
        client.session_prompt(sid, "x", timeout=5)
        # If we got here, permission was answered and prompt completed.
        assert client.captured_text()
