"""Durable index of agent chats (Claude Code + Grok Build).

Disk transcripts are the source of *discovery*. This SQLite DB is the source of
*memory* — so the control room does not re-discover the same abandoned mission
on every page load.

Authority:
  * path / mtime / title from last successful scan (and never forget a row just
    because a later scan missed it — set on_disk=0 instead)
  * live/recent/stale recomputed cheaply on read from mtime + process liveness
  * fleet_name / resumed_at when POST /api/chats/resume succeeds

Default path: ~/.config/emux/chats.db (or ~/.config/greenmux/chats.db when the
active skin is gmux / EMUX_SKIN=gmux).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .chats import (
    ChatHit,
    _GREENMARK_RE,
    _claude_live_ids,
    _grok_live_ids,
    _priority,
    clean_text,
    find_chats_bundle as _scan_bundle,
)


_DEFAULT_FRESH_SECS = 15 * 60  # re-scan disk only when store older than this


def default_chats_db_path() -> str:
    """Prefer greenmux config when that product is the active skin."""
    skin = (os.environ.get("EMUX_SKIN") or "").strip().lower()
    if skin in ("gmux", "greenmux", "greenmark"):
        root = Path.home() / ".config" / "greenmux"
    else:
        # if greenmux db already exists and emux does not, use it (rentamac)
        g = Path.home() / ".config" / "greenmux" / "chats.db"
        e = Path.home() / ".config" / "emux" / "chats.db"
        if g.is_file() and not e.is_file():
            return str(g)
        # skin module may not be importable in bare CLI; check EMUX_PRODUCT
        prod = (os.environ.get("EMUX_PRODUCT") or "").strip().lower()
        if "green" in prod or prod == "gmux":
            root = Path.home() / ".config" / "greenmux"
        else:
            try:
                from . import skin as _skin

                if _skin.active_skin().id == "gmux":
                    root = Path.home() / ".config" / "greenmux"
                else:
                    root = Path.home() / ".config" / "emux"
            except Exception:
                root = Path.home() / ".config" / "emux"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / "chats.db")


class ChatStore:
    """SQLite index of discovered agent chats. Soft-delete via deleted_at."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_chats_db_path()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                tool          TEXT NOT NULL,
                session_id    TEXT NOT NULL,
                host          TEXT NOT NULL DEFAULT 'local',
                cwd           TEXT DEFAULT '',
                title         TEXT DEFAULT '',
                summary       TEXT DEFAULT '',
                path          TEXT DEFAULT '',
                mtime         REAL DEFAULT 0,
                status        TEXT DEFAULT 'stale',
                resume        TEXT DEFAULT '',
                messages      INTEGER,
                model         TEXT,
                branch        TEXT,
                priority      INTEGER DEFAULT 0,
                greenmark     INTEGER DEFAULT 0,
                on_disk       INTEGER DEFAULT 1,
                fleet_name    TEXT,
                first_seen    REAL NOT NULL,
                last_seen     REAL NOT NULL,
                last_scan     REAL NOT NULL,
                resumed_at    REAL,
                notes         TEXT,
                deleted_at    REAL,
                PRIMARY KEY (tool, session_id, host)
            );
            CREATE INDEX IF NOT EXISTS ix_chats_status ON chats(status);
            CREATE INDEX IF NOT EXISTS ix_chats_mtime ON chats(mtime DESC);
            CREATE INDEX IF NOT EXISTS ix_chats_priority ON chats(priority DESC);
            CREATE INDEX IF NOT EXISTS ix_chats_greenmark ON chats(greenmark);
            CREATE INDEX IF NOT EXISTS ix_chats_last_scan ON chats(last_scan);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def last_full_scan(self) -> float | None:
        raw = self._meta_get("last_full_scan")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def row_count(self) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE deleted_at IS NULL"
            ).fetchone()["n"]
        )

    def is_fresh(self, max_age_secs: float = _DEFAULT_FRESH_SECS) -> bool:
        ts = self.last_full_scan()
        if ts is None:
            return False
        return (time.time() - ts) < max_age_secs and self.row_count() > 0

    def upsert_hits(
        self,
        hits: Iterable[ChatHit],
        *,
        host: str = "local",
        now: float | None = None,
        scan_token: str | None = None,
    ) -> int:
        """Insert or refresh rows from a disk scan. Returns rows written."""
        now = now if now is not None else time.time()
        token = scan_token or str(now)
        n = 0
        for h in hits:
            title = clean_text(h.title or "", max_len=120) or h.session_id[:12]
            summary = clean_text(h.summary or "", max_len=400)
            gm = 1 if h.greenmark else 0
            self.db.execute(
                """
                INSERT INTO chats(
                    tool, session_id, host, cwd, title, summary, path, mtime,
                    status, resume, messages, model, branch, priority, greenmark,
                    on_disk, first_seen, last_seen, last_scan, deleted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,NULL)
                ON CONFLICT(tool, session_id, host) DO UPDATE SET
                    cwd=excluded.cwd,
                    title=excluded.title,
                    summary=excluded.summary,
                    path=excluded.path,
                    mtime=excluded.mtime,
                    status=CASE
                        WHEN chats.fleet_name IS NOT NULL AND chats.resumed_at IS NOT NULL
                        THEN chats.status
                        ELSE excluded.status
                    END,
                    resume=excluded.resume,
                    messages=excluded.messages,
                    model=excluded.model,
                    branch=excluded.branch,
                    priority=excluded.priority,
                    greenmark=excluded.greenmark,
                    on_disk=1,
                    last_seen=excluded.last_seen,
                    last_scan=excluded.last_scan,
                    deleted_at=NULL
                """,
                (
                    h.tool,
                    h.session_id,
                    host,
                    h.cwd or "",
                    title,
                    summary,
                    h.path or "",
                    h.mtime,
                    h.status,
                    h.resume or "",
                    h.messages,
                    h.model,
                    h.branch,
                    h.priority,
                    gm,
                    now,
                    now,
                    now,
                ),
            )
            n += 1
        self._meta_set("last_full_scan", str(now))
        self._meta_set("last_scan_token", token)
        self.db.commit()
        return n

    def mark_missing_not_in(
        self,
        seen: Iterable[tuple[str, str]],
        *,
        host: str = "local",
        now: float | None = None,
    ) -> int:
        """Rows not returned by this scan still exist in memory; flag on_disk=0."""
        now = now if now is not None else time.time()
        seen_set = {(t, s) for t, s in seen}
        rows = self.db.execute(
            "SELECT tool, session_id FROM chats WHERE host=? AND deleted_at IS NULL AND on_disk=1",
            (host,),
        ).fetchall()
        n = 0
        for r in rows:
            if (r["tool"], r["session_id"]) not in seen_set:
                self.db.execute(
                    "UPDATE chats SET on_disk=0, last_scan=? "
                    "WHERE tool=? AND session_id=? AND host=?",
                    (now, r["tool"], r["session_id"], host),
                )
                n += 1
        self.db.commit()
        return n

    def mark_resumed(
        self,
        tool: str,
        session_id: str,
        fleet_name: str,
        *,
        host: str = "local",
        notes: str | None = None,
    ) -> bool:
        now = time.time()
        cur = self.db.execute(
            """
            UPDATE chats SET
                status='resumed',
                fleet_name=?,
                resumed_at=?,
                notes=COALESCE(?, notes),
                last_scan=?
            WHERE tool=? AND session_id=? AND host=? AND deleted_at IS NULL
            """,
            (fleet_name, now, notes, now, tool, session_id, host),
        )
        self.db.commit()
        return cur.rowcount > 0

    def soft_delete(self, tool: str, session_id: str, *, host: str = "local") -> bool:
        cur = self.db.execute(
            "UPDATE chats SET deleted_at=? WHERE tool=? AND session_id=? AND host=? AND deleted_at IS NULL",
            (time.time(), tool, session_id, host),
        )
        self.db.commit()
        return cur.rowcount > 0

    def _recompute_live_status(
        self,
        row: sqlite3.Row,
        now: float,
        recent_hours: float,
        *,
        claude_live: set[str] | None = None,
        grok_live: set[str] | None = None,
    ) -> str:
        """Cheap status without re-reading transcripts: process liveness + mtime."""
        # Durable "resumed into fleet" wins until the human archives it
        if row["status"] == "resumed" and row["fleet_name"]:
            return "resumed"
        if row["status"] == "archived":
            return "archived"
        tool = row["tool"]
        sid = row["session_id"]
        if tool == "claude":
            live_ids = claude_live if claude_live is not None else _claude_live_ids()
            live = sid.lower() in live_ids
        elif tool == "grok":
            live_ids = grok_live if grok_live is not None else _grok_live_ids()
            live = sid in live_ids
        else:
            live = False
        if live:
            return "live"
        age_h = max(0.0, (now - float(row["mtime"] or 0)) / 3600.0)
        if age_h <= recent_hours:
            return "recent"
        return "stale"

    def query(
        self,
        *,
        tools: Iterable[str] | None = None,
        match: str | None = "greenmark",
        statuses: Iterable[str] | None = None,
        limit: int = 50,
        q: str | None = None,
        sort: str = "priority",
        recent_hours: float = 24.0,
        include_off_disk: bool = True,
        host: str = "local",
    ) -> dict[str, Any]:
        """List from durable index. Recomputes live/recent/stale on the fly."""
        t0 = time.time()
        now = t0
        tools_set = {t.lower() for t in tools} if tools else {"claude", "grok"}
        want = set(statuses) if statuses else None
        if want and "abandoned" in want:
            want = (want - {"abandoned"}) | {"stale"}

        sql = "SELECT * FROM chats WHERE host=? AND deleted_at IS NULL"
        args: list[Any] = [host]
        if not include_off_disk:
            sql += " AND on_disk=1"
        rows = self.db.execute(sql, args).fetchall()

        # match filter
        if match is None or match == "" or match == "all":
            pat = None
        elif match in ("greenmark", "gm", "gmw"):
            pat = _GREENMARK_RE
        else:
            pat = re.compile(match, re.I)

        # one process table walk per query — not per row
        claude_live = _claude_live_ids() if "claude" in tools_set else set()
        grok_live = _grok_live_ids() if "grok" in tools_set else set()

        qn = (q or "").strip().lower()
        hits: list[dict[str, Any]] = []
        counts = {"live": 0, "recent": 0, "stale": 0, "resumed": 0, "archived": 0, "total": 0}
        tools_counts = {"claude": 0, "grok": 0}

        for row in rows:
            if row["tool"] not in tools_set:
                continue
            blob = f"{row['cwd']} {row['title']} {row['summary']} {row['path']}"
            if match in ("greenmark", "gm", "gmw"):
                if not row["greenmark"] and not _GREENMARK_RE.search(blob):
                    continue
            elif pat is not None and not pat.search(blob):
                continue
            status = self._recompute_live_status(
                row, now, recent_hours, claude_live=claude_live, grok_live=grok_live
            )
            age_h = max(0.0, (now - float(row["mtime"] or 0)) / 3600.0)
            gm = bool(row["greenmark"])
            prio = int(row["priority"] or 0)
            if status in ("live", "recent", "stale"):
                prio = _priority(status, gm, age_h, row["title"] or "")

            counts["total"] += 1
            if status in counts:
                counts[status] += 1
            if row["tool"] in tools_counts:
                tools_counts[row["tool"]] += 1

            if want and status not in want:
                continue
            if qn:
                hay = " ".join(
                    str(row[k] or "")
                    for k in ("title", "summary", "cwd", "session_id", "branch", "tool", "fleet_name")
                ).lower()
                if qn not in hay:
                    continue

            hits.append(
                {
                    "tool": row["tool"],
                    "session_id": row["session_id"],
                    "cwd": row["cwd"] or "",
                    "title": row["title"] or "",
                    "summary": row["summary"] or "",
                    "path": row["path"] or "",
                    "mtime": float(row["mtime"] or 0),
                    "age_hours": round(age_h, 2),
                    "mtime_iso": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(row["mtime"] or 0))
                    ),
                    "status": status,
                    "resume": row["resume"] or "",
                    "messages": row["messages"],
                    "model": row["model"],
                    "branch": row["branch"],
                    "priority": prio,
                    "greenmark": gm,
                    "on_disk": bool(row["on_disk"]),
                    "fleet_name": row["fleet_name"],
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "last_scan": row["last_scan"],
                    "resumed_at": row["resumed_at"],
                    "host": row["host"],
                    "source": "store",
                }
            )

        if sort == "mtime" or sort == "age":
            hits.sort(key=lambda h: h["mtime"], reverse=True)
        else:
            hits.sort(key=lambda h: (h["priority"], h["mtime"]), reverse=True)

        limited = hits[: max(1, limit)]
        return {
            "hits": limited,
            "counts": counts,
            "tools_counts": tools_counts,
            "returned": len(limited),
            "matched": len(hits),
            "scan_ms": int((time.time() - t0) * 1000),
            "match": match or "all",
            "q": q or "",
            "source": "store",
            "store_path": self.path,
            "last_full_scan": self.last_full_scan(),
            "store_rows": self.row_count(),
        }

    def sync_from_disk(
        self,
        *,
        tools: Iterable[str] = ("claude", "grok"),
        recent_hours: float = 24.0,
        limit: int = 5000,
        host: str = "local",
    ) -> dict[str, Any]:
        """Full disk scan → upsert. Always match=all; greenmark is a read filter."""
        t0 = time.time()
        bundle = _scan_bundle(
            tools=tools,
            match="all",
            recent_hours=recent_hours,
            statuses=None,
            limit=limit,
            q=None,
            sort="mtime",
        )
        hits: list[ChatHit] = bundle["hits"]
        now = time.time()
        written = self.upsert_hits(hits, host=host, now=now)
        missing = self.mark_missing_not_in(
            ((h.tool, h.session_id) for h in hits), host=host, now=now
        )
        return {
            "ok": True,
            "written": written,
            "missing_flagged": missing,
            "scanned": len(hits),
            "scan_ms": int((time.time() - t0) * 1000),
            "store_path": self.path,
            "store_rows": self.row_count(),
            "last_full_scan": self.last_full_scan(),
        }


def open_store(path: str | None = None) -> ChatStore:
    return ChatStore(path=path)


def list_or_sync(
    *,
    refresh: bool = False,
    max_age_secs: float = _DEFAULT_FRESH_SECS,
    tools: Iterable[str] = ("claude", "grok"),
    match: str | None = "greenmark",
    statuses: Iterable[str] | None = None,
    limit: int = 50,
    q: str | None = None,
    sort: str = "priority",
    recent_hours: float = 24.0,
    store_path: str | None = None,
) -> dict[str, Any]:
    """Room/API entry: serve from durable index; rescan disk only when stale/forced."""
    store = ChatStore(path=store_path)
    try:
        did_sync = False
        sync_info: dict[str, Any] = {}
        if refresh or not store.is_fresh(max_age_secs):
            sync_info = store.sync_from_disk(
                tools=tools,
                recent_hours=recent_hours,
                limit=max(limit, 5000),
            )
            did_sync = True
        bundle = store.query(
            tools=tools,
            match=match,
            statuses=statuses,
            limit=limit,
            q=q,
            sort=sort,
            recent_hours=recent_hours,
        )
        bundle["did_sync"] = did_sync
        bundle["sync"] = sync_info if did_sync else None
        return bundle
    finally:
        store.close()
