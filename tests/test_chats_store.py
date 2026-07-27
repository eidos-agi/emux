"""Durable chats.db index — remember missions so we do not re-find them cold."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from emux import chats, chats_store


def test_store_upsert_query_and_fresh(tmp_path, monkeypatch):
    db = tmp_path / "chats.db"
    store = chats_store.ChatStore(path=str(db))

    old = time.time() - 48 * 3600
    hits = [
        chats.ChatHit(
            tool="claude",
            session_id="sid-aaa",
            cwd="/Users/x/repos-greenmark-waste-solutions",
            title="GMW outage",
            summary="continue outage work",
            path="/tmp/a.jsonl",
            mtime=old,
            age_hours=48,
            status="stale",
            resume='cd "..." && claude --resume sid-aaa',
            messages=12,
            priority=80,
            greenmark=True,
        ),
        chats.ChatHit(
            tool="grok",
            session_id="sid-bbb",
            cwd="/Users/x/other",
            title="unrelated",
            summary="noise",
            path="/tmp/b",
            mtime=old,
            age_hours=48,
            status="stale",
            resume="grok",
            messages=2,
            priority=10,
            greenmark=False,
        ),
    ]
    n = store.upsert_hits(hits)
    assert n == 2
    assert store.row_count() == 2
    assert store.is_fresh(max_age_secs=60)

    # greenmark filter drops the non-GM row
    bundle = store.query(match="greenmark", statuses=["stale"], limit=20)
    assert bundle["matched"] == 1
    assert bundle["hits"][0]["session_id"] == "sid-aaa"
    assert bundle["source"] == "store"
    assert bundle["scan_ms"] >= 0

    # second query is pure store — no need to scan
    bundle2 = store.query(match="all", limit=20)
    assert bundle2["matched"] == 2

    store.mark_resumed("claude", "sid-aaa", "chat-claude-sid-aaa")
    resumed = store.query(match="all", statuses=["resumed"], limit=20)
    assert resumed["matched"] == 1
    assert resumed["hits"][0]["fleet_name"] == "chat-claude-sid-aaa"

    # missing from later scan → on_disk=0 but still remembered
    store.mark_missing_not_in([("grok", "sid-bbb")])
    off = store.query(match="all", include_off_disk=True, limit=20)
    claude_row = next(h for h in off["hits"] if h["session_id"] == "sid-aaa")
    assert claude_row["on_disk"] is False
    store.close()


def test_list_or_sync_uses_store(tmp_path, monkeypatch):
    db = tmp_path / "chats.db"
    # plant a store row without going through disk scan
    store = chats_store.ChatStore(path=str(db))
    store.upsert_hits(
        [
            chats.ChatHit(
                tool="claude",
                session_id="planted",
                cwd="/Users/x/repos-greenmark-waste-solutions/foo",
                title="planted mission",
                summary="remember me",
                path="/tmp/p.jsonl",
                mtime=time.time() - 100 * 3600,
                age_hours=100,
                status="stale",
                resume="claude --resume planted",
                greenmark=True,
                priority=90,
            )
        ]
    )
    store.close()

    # Force no disk scan by claiming fresh — list_or_sync with refresh=False
    # will still sync if not fresh; we just wrote so it is fresh.
    bundle = chats_store.list_or_sync(
        refresh=False,
        match="greenmark",
        limit=10,
        store_path=str(db),
    )
    assert bundle["did_sync"] is False
    assert bundle["source"] == "store"
    assert any(h["session_id"] == "planted" for h in bundle["hits"])
