"""Find abandoned / nonoperative agent chats (Claude Code + Grok Build).

Complements getcontrol (live panes) and greenmux registry (tmux): this scans
*transcript stores on disk* for work that still has a conversation but no
running process — the common Greenmark failure mode of "we started a chat on
rentamac / laptop and it died mid-issue."

Classification (per row):
  live       — process still running (Grok active_sessions; Claude: claude/node
               pid with session id in cmdline when detectable)
  recent     — not live, mtime within --recent-hours (default 24)
  stale      — not live, older than recent window
  abandoned  — stale (alias used when --abandoned-only; same as stale for v1)

Greenmark filter: cwd/project path matches greenmark|gmw|cerebro|gms|greenmux
(or --match regex).

Personal / Reeves filter (reevux): repos-personal, reeves-*, tally/dally, conduit
personal roots — never repos-aic / greenmark workspaces (or --match regex).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


_GREENMARK_RE = re.compile(
    r"greenmark|gmw|cerebro|gms|greenmux|rentamac|neutrino",
    re.I,
)

# Personal / Reeves lane — positive match only (repos-aic & greenmark paths fall out).
# Keep this tighter than a bare "personal" word match so work trees never bleed in.
_PERSONAL_RE = re.compile(
    r"repos-personal|"
    r"MacMiniStorage/personal|"
    r"reeves(?:-|_|\b)|"
    r"reevux|"
    r"(?:^|/)(?:tally|dally)(?:-|_|\b|/)|"
    r"dallyd|"
    r"(?:^|/)conduit(?:-|_|\b|/)|"
    r"personal-mgr|"
    r"reeves-apps|reeves-store|reeves-cockpit",
    re.I,
)

# Noise that pollutes titles when the last "user" event is a system envelope.
_NOISE_RE = re.compile(
    r"^\s*<(task-notification|task-id|local-command|system-reminder|command-name)\b",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class ChatHit:
    tool: str  # claude | grok
    session_id: str
    cwd: str
    title: str
    summary: str
    path: str  # transcript / session dir
    mtime: float
    age_hours: float
    status: str  # live | recent | stale
    resume: str  # copy-paste command
    messages: int | None = None
    model: str | None = None
    branch: str | None = None
    priority: int = 0  # higher = more urgent to resume
    greenmark: bool = False
    session_kind: str | None = None
    agent_name: str | None = None
    parent_session_id: str | None = None
    src_summary_mtime: float = 0.0
    src_updates_mtime: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_hours"] = round(self.age_hours, 2)
        d["mtime_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.mtime))
        return d


def clean_text(raw: str, *, max_len: int = 240) -> str:
    """Strip task-notification XML / tags; collapse whitespace for UI titles."""
    if not raw:
        return ""
    s = raw.strip()
    if _NOISE_RE.match(s) or s.startswith("<task-notification"):
        # pull inner human-ish text if present
        m = re.search(
            r"<(?:summary|description|text|message)[^>]*>([^<]+)",
            s,
            re.I,
        )
        s = m.group(1).strip() if m else _TAG_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _is_noise_prompt(text: str) -> bool:
    if not text or not text.strip():
        return True
    t = text.strip()
    if _NOISE_RE.match(t) or t.startswith("<task-notification"):
        return True
    # pure UUID / session ids as "title"
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        t,
        re.I,
    ):
        return True
    low = t.lower()
    if low.startswith("stop at the requested boundary"):
        return True
    if low in ("say hi", "hi", "hello", "test"):
        return False  # still real, just trivial — keep but low priority
    return False


def _priority(status: str, greenmark: bool, age_hours: float, title: str) -> int:
    """Higher = more urgent to surface (stale greenmark missions first)."""
    score = 0
    if status == "stale":
        score += 40
    elif status == "recent":
        score += 25
    elif status == "live":
        score += 5
    if greenmark:
        score += 30
    if status == "stale" and age_hours > 72:
        score += 10
    if status == "stale" and age_hours > 168:
        score += 5
    if title and not _is_noise_prompt(title) and len(title) > 12:
        score += 8
    if title and _is_noise_prompt(title):
        score -= 15
    return score


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _grok_live_ids() -> set[str]:
    """Session ids with a live Grok process (from active_sessions.json)."""
    path = Path.home() / ".grok" / "active_sessions.json"
    live: set[str] = set()
    if not path.exists():
        return live
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return live
    rows = data if isinstance(data, list) else data.get("sessions") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("session_id") or row.get("id")
        pid = row.get("pid")
        if sid and isinstance(pid, int) and _pid_alive(pid):
            live.add(str(sid))
    return live


def _claude_live_ids() -> set[str]:
    """Best-effort: session UUIDs from *CLI* claude processes only.

    Ignores Claude Desktop (Electron), helpers, and remote bridges — those
    otherwise false-positive "already_live" and block RESUME IN FLEET.
    """
    live: set[str] = set()
    try:
        import subprocess

        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return live
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.I,
    )
    # CLI: …/claude --resume <uuid>  or  claude <args with uuid>
    # Not: Claude.app, Helper, remote/srv, chrome_crashpad, etc.
    skip_frags = (
        "claude.app/",
        "claude helper",
        "claude/remote/",
        "crashpad",
        "electron",
        "gpu-process",
        "type=renderer",
    )
    for line in out.splitlines():
        low = line.lower()
        if "claude" not in low:
            continue
        if any(s in low for s in skip_frags):
            continue
        # Prefer lines that look like the CLI binary (path or bare name + resume)
        if not re.search(r"(?:^|\s)(?:\S*/)?claude(?:\s|$)", line, re.I):
            continue
        if "claude.app" in low:
            continue
        for m in uuid_re.findall(line):
            live.add(m.lower())
    return live


def _match_path(path: str, pattern: re.Pattern[str] | None) -> bool:
    if pattern is None:
        return True
    return bool(pattern.search(path or ""))


def _match_pattern(match: str | None) -> re.Pattern[str] | None:
    if match is None or match == "" or match == "all":
        return None
    key = match.strip().lower()
    if key in ("greenmark", "gm", "gmw"):
        return _GREENMARK_RE
    if key in ("personal", "reeves", "reevux", "rvs"):
        return _PERSONAL_RE
    return re.compile(match, re.I)


def _extract_text_block(c: Any) -> str:
    if isinstance(c, list):
        parts = []
        for block in c:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    if isinstance(c, dict):
        return str(c.get("text") or c.get("content") or "")
    return str(c or "")


def scan_grok(
    *,
    match: re.Pattern[str] | None = None,
    recent_hours: float = 24.0,
    now: float | None = None,
    include_subagents: bool | None = None,
) -> list[ChatHit]:
    """Scan ~/.grok/sessions via grok_control enrichment when available.

    Subagent sessions are skipped by default (fleet noise); set
    include_subagents=True or EMUX_CHATS_INCLUDE_SUBAGENTS=1 to keep them.
    """
    now = now if now is not None else time.time()
    live_ids = _grok_live_ids()
    hits: list[ChatHit] = []
    if include_subagents is None:
        include_subagents = (os.environ.get("EMUX_CHATS_INCLUDE_SUBAGENTS") or "").strip() in (
            "1",
            "true",
            "yes",
        )

    try:
        from . import grok_control as gc
    except ImportError:
        gc = None  # type: ignore[assignment]

    if gc is not None:
        root = gc.sessions_root()
        grok_bin = gc.resolve_grok_bin()
        if not root.is_dir():
            return hits
        for session_dir in gc.iter_session_dirs(root):
            idx = gc.enrich_session_dir(session_dir, deep=True)
            if idx is None:
                continue
            if not include_subagents and idx.is_subagent:
                continue
            sid = idx.session_id
            resume_cwd = idx.cwd or idx.project_cwd or "~"
            blob = (
                f"{idx.cwd} {idx.project_cwd} {idx.summary} {idx.title} "
                f"{idx.last_user_snippet} {idx.branch or ''} {idx.agent_name or ''}"
            )
            if not _match_path(blob, match):
                continue
            mtime = gc.mtime_from_index(idx, fallback_stat=session_dir / "summary.json")
            age_h = max(0.0, (now - mtime) / 3600.0)
            is_live = sid in live_ids
            if is_live:
                status = "live"
            elif age_h <= recent_hours:
                status = "recent"
            else:
                status = "stale"
            title = clean_text(idx.title, max_len=100) or sid[:12]
            # Prefer last human prompt for summary when present
            summary_src = idx.last_user_snippet or idx.summary or idx.title
            summary_txt = clean_text(summary_src, max_len=240)
            try:
                resume = gc.resume_shell_command(
                    sid, bin_path=grok_bin, cwd=resume_cwd, use_exec=False
                )
            except ValueError:
                resume = f"grok --resume {sid}"
            gm = bool(_GREENMARK_RE.search(blob))
            hits.append(
                ChatHit(
                    tool="grok",
                    session_id=sid,
                    cwd=resume_cwd,
                    title=title,
                    summary=summary_txt,
                    path=idx.path,
                    mtime=mtime,
                    age_hours=age_h,
                    status=status,
                    resume=resume,
                    messages=idx.chat_messages or idx.messages,
                    model=idx.model,
                    branch=idx.branch,
                    priority=_priority(status, gm, age_h, title),
                    greenmark=gm,
                    session_kind=idx.session_kind,
                    agent_name=idx.agent_name,
                    parent_session_id=idx.parent_session_id,
                    src_summary_mtime=idx.summary_mtime,
                    src_updates_mtime=idx.updates_mtime,
                )
            )
        return hits

    # Fallback if grok_control missing (should not happen in-tree).
    root = Path.home() / ".grok" / "sessions"
    if not root.is_dir():
        return hits
    for summary in root.rglob("summary.json"):
        try:
            data = json.loads(summary.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        info = data.get("info") or {}
        sid = str(info.get("id") or summary.parent.name)
        cwd = str(info.get("cwd") or "")
        project_key = summary.parent.parent.name
        try:
            from urllib.parse import unquote

            project_cwd = unquote(project_key)
        except Exception:
            project_cwd = project_key
        blob = (
            f"{cwd} {project_cwd} {data.get('session_summary') or ''} "
            f"{data.get('generated_title') or ''}"
        )
        if not _match_path(blob, match):
            continue
        mtime = summary.stat().st_mtime
        for key in ("last_active_at", "updated_at"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.endswith("Z"):
                try:
                    from datetime import datetime

                    mtime = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                    break
                except Exception:
                    pass
        age_h = max(0.0, (now - mtime) / 3600.0)
        is_live = sid in live_ids
        if is_live:
            status = "live"
        elif age_h <= recent_hours:
            status = "recent"
        else:
            status = "stale"
        raw_title = (
            data.get("generated_title")
            or data.get("session_summary")
            or sid[:12]
        )
        title = clean_text(str(raw_title), max_len=100) or sid[:12]
        summary_txt = clean_text(str(data.get("session_summary") or raw_title), max_len=240)
        resume_cwd = cwd or project_cwd or "~"
        resume = f"cd {json.dumps(resume_cwd)} && grok --resume {sid}"
        gm = bool(_GREENMARK_RE.search(blob))
        hits.append(
            ChatHit(
                tool="grok",
                session_id=sid,
                cwd=resume_cwd,
                title=title,
                summary=summary_txt,
                path=str(summary.parent),
                mtime=mtime,
                age_hours=age_h,
                status=status,
                resume=resume,
                messages=data.get("num_chat_messages") or data.get("num_messages"),
                model=data.get("current_model_id"),
                branch=data.get("head_branch"),
                priority=_priority(status, gm, age_h, title),
                greenmark=gm,
            )
        )
    return hits


def _claude_project_cwd(project_dir_name: str) -> str:
    """Decode Claude's project folder name to a path guess."""
    name = project_dir_name
    if name.startswith("-"):
        name = name[1:]
    if name.startswith("Users-"):
        return "/" + name.replace("-", "/")
    if name.startswith("private-tmp-"):
        return "/private/tmp/" + name[len("private-tmp-") :]
    return name.replace("-", "/")


def _claude_content_from_obj(o: dict[str, Any]) -> str:
    t = o.get("type") or o.get("role") or ""
    if t in ("user", "human", "last-prompt"):
        c = o.get("content") or o.get("message") or o.get("text") or ""
        if isinstance(c, dict) and "content" in c:
            c = c.get("content")
        return _extract_text_block(c).strip()
    if t == "queue-operation" and o.get("content"):
        return str(o["content"]).strip()
    # assistant
    if t in ("assistant", "ai", "model"):
        msg = o.get("message") or o.get("content") or o.get("text") or ""
        if isinstance(msg, dict):
            msg = msg.get("content") or msg.get("text") or ""
        return _extract_text_block(msg).strip()
    return ""


def _claude_last_prompt(path: Path, max_lines: int = 120) -> str:
    """Pull last *useful* user text from a Claude jsonl transcript."""
    try:
        # Read only the tail for large transcripts
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 200_000:
                f.seek(max(0, size - 180_000))
                f.readline()  # drop partial first line
            text = f.read().decode("utf-8", errors="ignore")
        lines = text.splitlines()
    except OSError:
        return ""
    candidates: list[str] = []
    for line in reversed(lines[-max_lines:]):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get("type") or o.get("role") or ""
        if t not in ("user", "human", "last-prompt", "queue-operation"):
            continue
        c = _claude_content_from_obj(o)
        if not c:
            continue
        cleaned = clean_text(c, max_len=240)
        if cleaned and not _is_noise_prompt(c):
            return cleaned
        if cleaned:
            candidates.append(cleaned)
    return candidates[0] if candidates else ""


def _claude_line_count_fast(path: Path) -> int | None:
    """Rough message volume without full UTF-8 parse — newline count."""
    try:
        n = 0
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                n += chunk.count(b"\n")
        return n or None
    except OSError:
        return None


def scan_claude(
    *,
    match: re.Pattern[str] | None = None,
    recent_hours: float = 24.0,
    now: float | None = None,
) -> list[ChatHit]:
    now = now if now is not None else time.time()
    live_ids = _claude_live_ids()
    root = Path.home() / ".claude" / "projects"
    hits: list[ChatHit] = []
    if not root.is_dir():
        return hits
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        proj_cwd = _claude_project_cwd(proj.name)
        if not _match_path(f"{proj.name} {proj_cwd}", match):
            if match is not None and not match.search(proj.name):
                continue
        for transcript in proj.glob("*.jsonl"):
            if transcript.name.startswith("."):
                continue
            # skip agent subagent side channels (often tiny / noisy)
            if transcript.name.endswith(".jsonl") and "subagents" in str(transcript.parent):
                continue
            sid = transcript.stem
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            age_h = max(0.0, (now - mtime) / 3600.0)
            last = _claude_last_prompt(transcript)
            blob = f"{proj.name} {proj_cwd} {last}"
            if match is not None and not match.search(blob):
                continue
            is_live = sid.lower() in live_ids
            if is_live:
                status = "live"
            elif age_h <= recent_hours:
                status = "recent"
            else:
                status = "stale"
            nlines = _claude_line_count_fast(transcript)
            title = last if last else sid[:12]
            if len(title) > 80:
                title = title[:79] + "…"
            resume = f'cd {json.dumps(proj_cwd)} && claude --resume {sid}'
            gm = bool(_GREENMARK_RE.search(blob))
            hits.append(
                ChatHit(
                    tool="claude",
                    session_id=sid,
                    cwd=proj_cwd,
                    title=title,
                    summary=last,
                    path=str(transcript),
                    mtime=mtime,
                    age_hours=age_h,
                    status=status,
                    resume=resume,
                    messages=nlines,
                    model=None,
                    branch=None,
                    priority=_priority(status, gm, age_h, title),
                    greenmark=gm,
                )
            )
    return hits


def find_chats(
    *,
    tools: Iterable[str] = ("claude", "grok"),
    match: str | None = "greenmark",
    recent_hours: float = 24.0,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
    q: str | None = None,
    sort: str = "priority",  # priority | mtime | age
) -> list[ChatHit]:
    """Scan local agent stores. match=None means all paths; 'greenmark' uses built-in regex."""
    bundle = find_chats_bundle(
        tools=tools,
        match=match,
        recent_hours=recent_hours,
        statuses=statuses,
        limit=limit,
        q=q,
        sort=sort,
    )
    return bundle["hits"]


def find_chats_bundle(
    *,
    tools: Iterable[str] = ("claude", "grok"),
    match: str | None = "greenmark",
    recent_hours: float = 24.0,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
    q: str | None = None,
    sort: str = "priority",
) -> dict[str, Any]:
    """Single-pass scan: hits + counts + tools_counts + scan_ms."""
    t0 = time.time()
    pat = _match_pattern(match)
    tools_set = {t.lower() for t in tools}
    hits: list[ChatHit] = []
    if "grok" in tools_set:
        hits.extend(scan_grok(match=pat, recent_hours=recent_hours))
    if "claude" in tools_set:
        hits.extend(scan_claude(match=pat, recent_hours=recent_hours))

    counts = {"live": 0, "recent": 0, "stale": 0, "total": 0}
    tools_counts = {"claude": 0, "grok": 0}
    for h in hits:
        counts["total"] += 1
        if h.status in counts:
            counts[h.status] += 1
        if h.tool in tools_counts:
            tools_counts[h.tool] += 1

    want = set(statuses) if statuses else None
    if want:
        if "abandoned" in want:
            want = (want - {"abandoned"}) | {"stale"}
        hits = [h for h in hits if h.status in want]

    qn = (q or "").strip().lower()
    if qn:
        hits = [
            h
            for h in hits
            if qn in (h.title or "").lower()
            or qn in (h.summary or "").lower()
            or qn in (h.cwd or "").lower()
            or qn in (h.session_id or "").lower()
            or qn in (h.branch or "").lower()
            or qn in (h.tool or "").lower()
        ]

    if sort == "mtime" or sort == "age":
        hits.sort(key=lambda h: h.mtime, reverse=True)
    else:
        # priority desc, then mtime desc
        hits.sort(key=lambda h: (h.priority, h.mtime), reverse=True)

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
    }


def peek_chat(
    tool: str,
    session_id: str,
    *,
    max_turns: int = 8,
) -> dict[str, Any]:
    """Last user/assistant turns for a transcript (room tile expand)."""
    tool = (tool or "").lower().strip()
    sid = (session_id or "").strip()
    if not tool or not sid:
        return {"ok": False, "error": "missing_tool_or_session"}
    turns: list[dict[str, str]] = []
    path: Path | None = None

    if tool == "claude":
        root = Path.home() / ".claude" / "projects"
        if root.is_dir():
            for p in root.rglob(f"{sid}.jsonl"):
                path = p
                break
        if path is None or not path.is_file():
            return {"ok": False, "error": "not_found", "tool": tool, "session_id": sid}
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > 250_000:
                    f.seek(max(0, size - 220_000))
                    f.readline()
                lines = f.read().decode("utf-8", errors="ignore").splitlines()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        for line in lines:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type") or o.get("role") or ""
            if t not in ("user", "human", "assistant", "ai", "model"):
                continue
            text = clean_text(_claude_content_from_obj(o), max_len=400)
            if not text or _is_noise_prompt(text):
                continue
            role = "user" if t in ("user", "human") else "assistant"
            turns.append({"role": role, "text": text})
        turns = turns[-max_turns:]
        return {
            "ok": True,
            "tool": tool,
            "session_id": sid,
            "path": str(path),
            "turns": turns,
            "count": len(turns),
        }

    if tool == "grok":
        try:
            from . import grok_control as gc
        except ImportError:
            gc = None  # type: ignore[assignment]
        root = gc.sessions_root() if gc is not None else (Path.home() / ".grok" / "sessions")
        # session id is often the directory name
        if root.is_dir():
            for summary in root.rglob("summary.json"):
                if summary.parent.name == sid:
                    path = summary.parent
                    break
                try:
                    data = json.loads(summary.read_text())
                    info = data.get("info") or {}
                    if str(info.get("id") or "") == sid:
                        path = summary.parent
                        break
                except (json.JSONDecodeError, OSError):
                    continue
        if path is None:
            return {"ok": False, "error": "not_found", "tool": tool, "session_id": sid}
        # Prefer chat_history.jsonl (canonical Grok transcript), then aliases.
        for name in (
            "chat_history.jsonl",
            "transcript.jsonl",
            "messages.jsonl",
            "chat.jsonl",
        ):
            tp = path / name
            if tp.is_file():
                try:
                    for line in tp.read_text(errors="ignore").splitlines()[-200:]:
                        try:
                            o = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(o, dict):
                            continue
                        role = str(o.get("role") or o.get("type") or "").lower()
                        if role not in ("user", "assistant", "human", "model", "ai"):
                            continue
                        text = clean_text(
                            _extract_text_block(
                                o.get("content") or o.get("text") or o.get("message") or ""
                            ),
                            max_len=400,
                        )
                        if not text:
                            continue
                        r = "user" if role in ("user", "human") else "assistant"
                        turns.append({"role": r, "text": text})
                except OSError:
                    pass
                break
        if not turns:
            # fall back to enriched summary / last user snippet
            if gc is not None:
                idx = gc.enrich_session_dir(path)
                if idx is not None:
                    if idx.summary:
                        turns.append(
                            {"role": "summary", "text": clean_text(idx.summary, max_len=400)}
                        )
                    if idx.last_user_snippet:
                        turns.append(
                            {
                                "role": "user",
                                "text": clean_text(idx.last_user_snippet, max_len=400),
                            }
                        )
            if not turns:
                try:
                    data = json.loads((path / "summary.json").read_text())
                    for key in ("session_summary", "generated_title", "last_message"):
                        if data.get(key):
                            turns.append(
                                {
                                    "role": "summary",
                                    "text": clean_text(str(data[key]), max_len=400),
                                }
                            )
                except (json.JSONDecodeError, OSError):
                    pass
        turns = turns[-max_turns:]
        return {
            "ok": True,
            "tool": tool,
            "session_id": sid,
            "path": str(path),
            "turns": turns,
            "count": len(turns),
        }

    return {"ok": False, "error": "unknown_tool", "tool": tool}


def format_text(hits: list[ChatHit], *, title: str = "Abandoned / nonoperative agent chats") -> str:
    lines = [
        f"# {title}",
        "",
        f"- scanned: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"- count: {len(hits)}",
        "",
        "| status | tool | age_h | title | cwd | resume |",
        "|---|---|---:|---|---|---|",
    ]
    for h in hits:
        title_s = h.title.replace("|", "\\|").replace("\n", " ")[:60]
        cwd_s = h.cwd.replace("|", "\\|")[:40]
        resume_s = h.resume.replace("|", "\\|")
        lines.append(
            f"| {h.status} | {h.tool} | {h.age_hours:.1f} | {title_s} | `{cwd_s}` | `{resume_s}` |"
        )
    if not hits:
        lines.append("")
        lines.append("_No matching chats._")
    lines += [
        "",
        "## How to use",
        "",
        "1. Prefer **stale** rows with Greenmark cwd — those are likely dropped missions.",
        "2. **live** means a process still holds the session — attach/boss instead of re-opening.",
        "3. Resume with the command in the last column (Claude: `claude --resume`; Grok: `grok --resume <id>`).",
        "4. Cross-check greenmux registry ghosts (`greenmux ls` / gmux status `?all=1`) — tmux dead ≠ chat dead.",
        "",
    ]
    return "\n".join(lines)
