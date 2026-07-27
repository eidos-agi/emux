"""Tests for abandoned-chat finder (Claude + Grok)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from emux import chats


def _fake_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".grok" / "sessions" / "proj" / "sid1").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-x-repos-greenmark-waste-solutions").mkdir(
        parents=True
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


def test_find_chats_greenmark_filter(tmp_path, monkeypatch):
    fake_home = _fake_home(tmp_path, monkeypatch)

    gsum = fake_home / ".grok" / "sessions" / "proj" / "sid1" / "summary.json"
    gsum.write_text(
        json.dumps(
            {
                "info": {"id": "sid1", "cwd": "/Users/x/repos-greenmark-waste-solutions/foo"},
                "session_summary": "GMW-950 greenmux work",
                "generated_title": "Greenmux ship",
                "updated_at": "2020-01-01T00:00:00Z",
                "num_chat_messages": 3,
            }
        )
    )
    cpath = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-greenmark-waste-solutions"
        / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    cpath.write_text(
        json.dumps({"type": "user", "content": "continue GMW-1020 outage"}) + "\n"
    )
    old = time.time() - 48 * 3600
    os.utime(gsum, (old, old))
    os.utime(cpath, (old, old))

    hits = chats.find_chats(match="greenmark", recent_hours=24, limit=20)
    tools = {h.tool for h in hits}
    assert "grok" in tools
    assert "claude" in tools
    assert all(h.status == "stale" for h in hits)
    assert any("claude --resume" in h.resume for h in hits)
    assert any("grok" in h.resume and "--resume" in h.resume for h in hits)
    assert all(h.greenmark for h in hits)
    assert all(h.priority > 0 for h in hits)


def test_find_chats_personal_filter_excludes_work(tmp_path, monkeypatch):
    """reevux match=personal: personal roots only — never repos-aic / greenmark."""
    fake_home = tmp_path / "home"
    (fake_home / ".grok" / "sessions" / "p" / "pers").mkdir(parents=True)
    (fake_home / ".grok" / "sessions" / "a" / "aic").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-x-repos-personal-reeves-3").mkdir(
        parents=True
    )
    (fake_home / ".claude" / "projects" / "-Users-x-repos-aic-cockpit").mkdir(
        parents=True
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    def _write_grok(sid: str, cwd: str, title: str) -> Path:
        d = fake_home / ".grok" / "sessions" / sid[0] / sid
        d.mkdir(parents=True, exist_ok=True)
        p = d / "summary.json"
        p.write_text(
            json.dumps(
                {
                    "info": {"id": sid, "cwd": cwd},
                    "session_summary": title,
                    "generated_title": title,
                    "updated_at": "2020-01-01T00:00:00Z",
                    "num_chat_messages": 2,
                }
            )
        )
        return p

    g_pers = _write_grok(
        "pers1", "/Users/x/repos-personal/reeves-3", "personal reeves mission"
    )
    g_aic = _write_grok(
        "aic1", "/Users/x/repos-aic/aic-software-engineer-cockpit", "AIC work"
    )
    c_pers = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-personal-reeves-3"
        / "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    c_pers.write_text(json.dumps({"type": "user", "content": "reeves cockpit task"}) + "\n")
    c_aic = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-aic-cockpit"
        / "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    c_aic.write_text(json.dumps({"type": "user", "content": "ship AIC epic"}) + "\n")
    old = time.time() - 48 * 3600
    for p in (g_pers, g_aic, c_pers, c_aic):
        os.utime(p, (old, old))

    hits = chats.find_chats(match="personal", recent_hours=24, limit=50)
    cwds = " ".join((h.cwd or "") + " " + (h.path or "") for h in hits)
    assert hits, "expected at least one personal hit"
    assert "repos-personal" in cwds or "reeves" in cwds.lower()
    assert "repos-aic" not in cwds
    assert all(
        chats._PERSONAL_RE.search((h.cwd or "") + " " + (h.path or "")) for h in hits
    )
    # greenmark alias still works and must not return pure personal-only set
    gm = chats.find_chats(match="greenmark", recent_hours=24, limit=50)
    assert all(h.greenmark for h in gm) if gm else True


def test_find_chats_aic_filter_excludes_personal(tmp_path, monkeypatch):
    """amux match=aic: repos-aic only — never repos-personal / greenmark."""
    fake_home = tmp_path / "home"
    (fake_home / ".grok" / "sessions" / "p" / "pers").mkdir(parents=True)
    (fake_home / ".grok" / "sessions" / "a" / "aic").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    def _write_grok(sid: str, cwd: str, title: str) -> Path:
        d = fake_home / ".grok" / "sessions" / sid[0] / sid
        d.mkdir(parents=True, exist_ok=True)
        p = d / "summary.json"
        p.write_text(
            json.dumps(
                {
                    "info": {"id": sid, "cwd": cwd},
                    "session_summary": title,
                    "generated_title": title,
                    "updated_at": "2020-01-01T00:00:00Z",
                    "num_chat_messages": 2,
                }
            )
        )
        return p

    g_pers = _write_grok(
        "pers1", "/Users/x/repos-personal/reeves-3", "personal reeves mission"
    )
    g_aic = _write_grok(
        "aic1", "/Users/x/repos-aic/aic-software-engineer-cockpit", "AIC work"
    )
    old = time.time() - 48 * 3600
    for p in (g_pers, g_aic):
        os.utime(p, (old, old))

    hits = chats.find_chats(match="aic", recent_hours=24, limit=50)
    cwds = " ".join((h.cwd or "") + " " + (h.path or "") for h in hits)
    assert hits, "expected at least one AIC hit"
    assert "repos-aic" in cwds
    assert "repos-personal" not in cwds
    assert all(
        chats._AIC_RE.search((h.cwd or "") + " " + (h.path or "")) for h in hits
    )


def test_clean_text_strips_task_notification():
    raw = (
        "<task-notification>\n<task-id>abc</task-id>\n"
        "<summary>Monitor event: new file in /tmp</summary>\n</task-notification>"
    )
    cleaned = chats.clean_text(raw)
    assert "task-notification" not in cleaned.lower()
    assert "Monitor event" in cleaned


def test_find_chats_bundle_q_and_counts(tmp_path, monkeypatch):
    fake_home = _fake_home(tmp_path, monkeypatch)
    gsum = fake_home / ".grok" / "sessions" / "proj" / "sid1" / "summary.json"
    gsum.write_text(
        json.dumps(
            {
                "info": {"id": "sid1", "cwd": "/Users/x/repos-greenmark-waste-solutions/foo"},
                "session_summary": "GMW-950 greenmux work",
                "generated_title": "Greenmux ship",
                "updated_at": "2020-01-01T00:00:00Z",
            }
        )
    )
    cpath = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-greenmark-waste-solutions"
        / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    cpath.write_text(
        json.dumps({"type": "user", "content": "continue GMW-1020 outage"}) + "\n"
        + json.dumps({"type": "assistant", "content": "working on outage"}) + "\n"
    )
    old = time.time() - 48 * 3600
    os.utime(gsum, (old, old))
    os.utime(cpath, (old, old))

    bundle = chats.find_chats_bundle(match="greenmark", recent_hours=24, limit=20)
    assert bundle["counts"]["stale"] >= 2
    assert bundle["tools_counts"]["claude"] >= 1
    assert bundle["scan_ms"] >= 0

    only = chats.find_chats(match="greenmark", q="GMW-1020", limit=20)
    assert len(only) >= 1
    assert all("1020" in (h.title + h.summary) for h in only)


def test_peek_chat_claude(tmp_path, monkeypatch):
    fake_home = _fake_home(tmp_path, monkeypatch)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cpath = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-greenmark-waste-solutions"
        / f"{sid}.jsonl"
    )
    cpath.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "content": "fix the Caddy path"}),
                json.dumps({"type": "assistant", "content": "Looking at greenmark.Caddyfile"}),
                json.dumps(
                    {
                        "type": "user",
                        "content": "<task-notification><summary>noise</summary></task-notification>",
                    }
                ),
            ]
        )
        + "\n"
    )
    peek = chats.peek_chat("claude", sid, max_turns=8)
    assert peek["ok"] is True
    assert peek["count"] >= 1
    roles = {t["role"] for t in peek["turns"]}
    assert "user" in roles or "assistant" in roles
    # noise-only last turn should not be the only content if cleaner works
    texts = " ".join(t["text"] for t in peek["turns"])
    assert "Caddy" in texts or "greenmark" in texts.lower()


def test_format_text_empty():
    text = chats.format_text([])
    assert "No matching chats" in text


def test_normalize_chat_cwd_and_resume_fleet(monkeypatch):
    from emux import web

    assert web._normalize_chat_cwd("Volumes/GREENMARK/foo") == "/Volumes/GREENMARK/foo"
    assert web._normalize_chat_cwd("/Users/x/y") == "/Users/x/y"
    assert web._normalize_chat_cwd(None) is None

    calls = {}

    async def fake_spawn(**kwargs):
        calls.update(kwargs)
        return {"ok": True, "name": kwargs["name"], "session": kwargs["name"], "launched": True}

    monkeypatch.setattr(web._server, "tmux_spawn", fake_spawn)

    import emux.chats as chat_find

    monkeypatch.setattr(chat_find, "_claude_live_ids", lambda: set())
    monkeypatch.setattr(chat_find, "_grok_live_ids", lambda: set())

    r = web._resume_chat_in_fleet(
        {
            "tool": "claude",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "cwd": "Volumes/GREENMARK/Clouds/x",
            "title": "fix the outage",
            "greenmark": True,
        }
    )
    assert r["ok"] is True
    assert r["name"].startswith("chat-claude-")
    assert "claude --resume" in r["command"]
    assert calls["cwd"] == "/Volumes/GREENMARK/Clouds/x"
    assert "chat-resume" in (calls.get("tags") or [])
    assert "greenmark" in (calls.get("tags") or [])

    # refuse remote host
    bad = web._resume_chat_in_fleet(
        {"tool": "claude", "session_id": "x", "host": "otherbox"}
    )
    assert bad["ok"] is False
    assert bad["error"] == "chat_resume_local_only"

    # refuse already live
    monkeypatch.setattr(chat_find, "_claude_live_ids", lambda: {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})
    live = web._resume_chat_in_fleet(
        {"tool": "claude", "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    )
    assert live["ok"] is False
    assert live["error"] == "already_live"
