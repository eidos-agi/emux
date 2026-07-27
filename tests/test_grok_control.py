"""Unit tests for Grok Build control-plane helpers (no network)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from emux import grok_control as gc


def test_resolve_grok_bin_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "grok"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("EMUX_GROK_BIN", str(fake))
    assert gc.resolve_grok_bin(home=tmp_path / "home") == str(fake.resolve())


def test_resolve_grok_bin_home_dot_grok(tmp_path, monkeypatch):
    monkeypatch.delenv("EMUX_GROK_BIN", raising=False)
    home = tmp_path / "home"
    fake = home / ".grok" / "bin" / "grok"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    # Empty PATH so we do not pick up a real install.
    found = gc.resolve_grok_bin(env={"PATH": "/nonexistent"}, home=home)
    assert found == str(fake.resolve())


def test_enrich_session_dir_summary_and_history(tmp_path):
    sid = "019fa1e8-764b-73a1-8310-fa308fe1b924"
    proj = tmp_path / "sessions" / "%2FUsers%2Fx%2Frepos-greenmark" / sid
    proj.mkdir(parents=True)
    (proj / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": sid, "cwd": "/Users/x/repos-greenmark"},
                "session_summary": "Ship greenmux path fix",
                "generated_title": "Greenmux path",
                "updated_at": "2020-01-02T00:00:00Z",
                "last_active_at": "2020-01-02T01:00:00Z",
                "num_chat_messages": 4,
                "num_messages": 10,
                "current_model_id": "grok-4.5",
                "head_branch": "feat/x",
                "agent_name": "grok-build",
            }
        )
    )
    (proj / "chat_history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "content": "sys"}),
                json.dumps(
                    {
                        "type": "user",
                        "content": [{"type": "text", "text": "please fix the Caddy path"}],
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "content": [{"type": "text", "text": "looking at greenmark.Caddyfile"}],
                    }
                ),
            ]
        )
        + "\n"
    )
    (proj / "updates.jsonl").write_text(
        json.dumps(
            {
                "method": "_x.ai/session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "done with path"},
                    }
                },
            }
        )
        + "\n"
    )

    idx = gc.enrich_session_dir(proj)
    assert idx is not None
    assert idx.session_id == sid
    assert idx.cwd == "/Users/x/repos-greenmark"
    assert "Greenmux" in idx.title
    assert "greenmux" in idx.summary.lower() or "Ship" in idx.summary
    assert idx.model == "grok-4.5"
    assert idx.branch == "feat/x"
    assert idx.chat_messages == 4
    assert "Caddy" in idx.last_user_snippet
    assert "summary.json" in idx.source_files
    assert "chat_history.jsonl" in idx.source_files
    assert "updates.jsonl" in idx.source_files
    assert idx.project_cwd.endswith("repos-greenmark") or "greenmark" in idx.project_cwd
    ts = gc.mtime_from_index(idx)
    assert ts > 0


def test_enrich_falls_back_to_chat_history_when_title_missing(tmp_path):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    d = tmp_path / sid
    d.mkdir()
    (d / "summary.json").write_text(
        json.dumps({"info": {"id": sid, "cwd": "/tmp/p"}, "num_messages": 1})
    )
    (d / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "resume the outage work"}) + "\n"
    )
    idx = gc.enrich_session_dir(d)
    assert idx is not None
    assert "outage" in idx.title.lower() or "outage" in idx.summary.lower()


def test_resume_and_headless_argv(tmp_path, monkeypatch):
    fake = tmp_path / "grok"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("EMUX_GROK_BIN", str(fake))

    sid = "019fa1e8-764b-73a1-8310-fa308fe1b924"
    argv = gc.resume_argv(sid, bin_path=str(fake))
    assert argv == [str(fake), "--resume", sid]

    shell = gc.resume_shell_command(sid, bin_path=str(fake), cwd="/tmp/work", use_exec=True)
    assert "exec" in shell
    assert "--resume" in shell
    assert sid in shell
    assert "/tmp/work" in shell

    h = gc.headless_steer_argv(
        "continue the mission",
        session_id=sid,
        bin_path=str(fake),
        output_format="json",
    )
    assert h[0] == str(fake)
    assert "-p" in h
    assert "continue the mission" in h
    assert "-r" in h and sid in h
    assert "--output-format" in h and "json" in h

    cont = gc.headless_steer_argv("ping", continue_recent=True, bin_path=str(fake))
    assert "-c" in cont


def test_acp_stdio_argv_shape(tmp_path, monkeypatch):
    fake = tmp_path / "grok"
    fake.write_text("x")
    fake.chmod(0o755)
    monkeypatch.setenv("EMUX_GROK_BIN", str(fake))

    basic = gc.acp_stdio_argv(bin_path=str(fake))
    assert basic == [str(fake), "agent", "stdio"]

    rich = gc.acp_stdio_argv(
        bin_path=str(fake),
        model="grok-4.5",
        always_approve=True,
        leader=False,
        leader_socket="/tmp/leader.sock",
    )
    assert rich[0] == str(fake)
    assert rich[1] == "agent"
    assert "-m" in rich and "grok-4.5" in rich
    assert "--always-approve" in rich
    assert "--no-leader" in rich
    assert rich[rich.index("stdio")] == "stdio"
    assert "--leader-socket" in rich


def test_hooks_bridge_dry_run_and_write(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    plan = gc.write_hooks_bridge(scope="global", home=home, dry_run=True)
    assert plan["ok"] is True
    assert plan["written"] is False
    assert "PreToolUse" in plan["payload"]["hooks"]
    assert "Stop" in plan["payload"]["hooks"]
    assert "python3 -m emux.hook_delegation" in json.dumps(plan["payload"])

    written = gc.write_hooks_bridge(scope="global", home=home, dry_run=False)
    assert written["written"] is True
    path = Path(written["path"])
    assert path.is_file()
    body = json.loads(path.read_text())
    assert "PreToolUse" in body["hooks"]

    proj = tmp_path / "proj"
    proj.mkdir()
    p2 = gc.write_hooks_bridge(scope="project", project_dir=proj, dry_run=False, name="emux.json")
    assert Path(p2["path"]).is_file()
    assert ".grok/hooks" in p2["path"]


def test_is_session_id():
    assert gc.is_session_id("019fa1e8-764b-73a1-8310-fa308fe1b924")
    assert not gc.is_session_id("not-a-uuid")
    assert not gc.is_session_id("")


def test_scan_grok_uses_control_resume(tmp_path, monkeypatch):
    """chats.scan_grok should emit grok --resume, not bare grok + TUI /resume."""
    from emux import chats

    home = tmp_path / "home"
    sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    d = home / ".grok" / "sessions" / "%2FUsers%2Fx%2Fgreenmark" / sid
    d.mkdir(parents=True)
    (d / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": sid, "cwd": "/Users/x/greenmark"},
                "generated_title": "GMW path",
                "session_summary": "greenmark work",
                "updated_at": "2020-01-01T00:00:00Z",
            }
        )
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(chats, "_grok_live_ids", lambda: set())
    # Point sessions_root at fake home without needing GROK_HOME if home() is patched.
    monkeypatch.setenv("GROK_HOME", str(home / ".grok"))

    hits = chats.scan_grok(match=None, recent_hours=24)
    assert len(hits) >= 1
    h = hits[0]
    assert h.session_id == sid
    assert "--resume" in h.resume
    assert sid in h.resume
    assert h.greenmark is True or "greenmark" in h.cwd.lower()
