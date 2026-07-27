"""Tests for abandoned-chat finder (Claude + Grok)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from emux import chats


def test_find_chats_greenmark_filter(tmp_path, monkeypatch):
    # Isolate homes
    fake_home = tmp_path / "home"
    (fake_home / ".grok" / "sessions" / "proj" / "sid1").mkdir(parents=True)
    (fake_home / ".claude" / "projects" / "-Users-x-repos-greenmark-waste-solutions").mkdir(
        parents=True
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Grok summary — greenmark-ish cwd
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
    # Claude transcript — greenmark project dir
    cpath = (
        fake_home
        / ".claude"
        / "projects"
        / "-Users-x-repos-greenmark-waste-solutions"
        / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    )
    cpath.write_text(
        json.dumps(
            {
                "type": "user",
                "content": "continue GMW-1020 outage",
            }
        )
        + "\n"
    )
    # old mtime for stale
    old = time.time() - 48 * 3600
    import os

    os.utime(gsum, (old, old))
    os.utime(cpath, (old, old))

    hits = chats.find_chats(match="greenmark", recent_hours=24, limit=20)
    tools = {h.tool for h in hits}
    assert "grok" in tools
    assert "claude" in tools
    assert all(h.status == "stale" for h in hits)
    assert any("claude --resume" in h.resume for h in hits)
    assert any("/resume" in h.resume for h in hits)


def test_format_text_empty():
    text = chats.format_text([])
    assert "No matching chats" in text
