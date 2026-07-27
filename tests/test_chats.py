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
    assert any("/resume" in h.resume for h in hits)
    assert all(h.greenmark for h in hits)
    assert all(h.priority > 0 for h in hits)


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
