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

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_hours"] = round(self.age_hours, 2)
        d["mtime_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.mtime))
        return d


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
    """Best-effort: session UUIDs appearing in running claude-related cmdlines."""
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
    # Claude session ids look like uuid
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.I,
    )
    for line in out.splitlines():
        if "claude" not in line.lower() and "anthropic" not in line.lower():
            continue
        for m in uuid_re.findall(line):
            live.add(m.lower())
    return live


def _match_path(path: str, pattern: re.Pattern[str] | None) -> bool:
    if pattern is None:
        return True
    return bool(pattern.search(path or ""))


def scan_grok(
    *,
    match: re.Pattern[str] | None = None,
    recent_hours: float = 24.0,
    now: float | None = None,
) -> list[ChatHit]:
    now = now if now is not None else time.time()
    live_ids = _grok_live_ids()
    root = Path.home() / ".grok" / "sessions"
    hits: list[ChatHit] = []
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
        # project path is parent of session uuid dir
        project_key = summary.parent.parent.name
        try:
            from urllib.parse import unquote

            project_cwd = unquote(project_key)
        except Exception:
            project_cwd = project_key
        blob = f"{cwd} {project_cwd} {data.get('session_summary') or ''} {data.get('generated_title') or ''}"
        if not _match_path(blob, match):
            continue
        mtime = summary.stat().st_mtime
        # prefer last_active_at / updated_at if present
        for key in ("last_active_at", "updated_at"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.endswith("Z"):
                try:
                    # minimal parse
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
        title = (
            data.get("generated_title")
            or data.get("session_summary")
            or sid[:12]
        )
        resume_cwd = cwd or project_cwd or "~"
        # Grok resume: open in cwd; slash /resume <id> inside session
        resume = f'cd {json.dumps(resume_cwd)} && grok  # then /resume {sid}'
        hits.append(
            ChatHit(
                tool="grok",
                session_id=sid,
                cwd=resume_cwd,
                title=str(title)[:120],
                summary=str(data.get("session_summary") or "")[:240],
                path=str(summary.parent),
                mtime=mtime,
                age_hours=age_h,
                status=status,
                resume=resume,
                messages=data.get("num_chat_messages") or data.get("num_messages"),
                model=data.get("current_model_id"),
                branch=data.get("head_branch"),
            )
        )
    return hits


def _claude_project_cwd(project_dir_name: str) -> str:
    """Decode Claude's project folder name to a path guess."""
    # e.g. -Users-dshanklinbv-repos-greenmark-waste-solutions
    name = project_dir_name
    if name.startswith("-"):
        name = name[1:]
    # private-tmp and seats prefixes keep hyphens; best-effort
    if name.startswith("Users-"):
        return "/" + name.replace("-", "/")
    if name.startswith("private-tmp-"):
        return "/private/tmp/" + name[len("private-tmp-") :]
    return name.replace("-", "/")


def _claude_last_prompt(path: Path, max_lines: int = 80) -> str:
    """Pull last user-ish text from a Claude jsonl transcript."""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-max_lines:]):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = o.get("type") or o.get("role") or ""
        if t in ("user", "human", "last-prompt"):
            c = o.get("content") or o.get("message") or o.get("text") or ""
            if isinstance(c, list):
                parts = []
                for block in c:
                    if isinstance(block, dict) and block.get("text"):
                        parts.append(str(block["text"]))
                    elif isinstance(block, str):
                        parts.append(block)
                c = " ".join(parts)
            if isinstance(c, str) and c.strip():
                return c.strip()[:240]
        if t == "queue-operation" and o.get("content"):
            return str(o["content"])[:240]
    return ""


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
            # still allow if any session content matches later — skip whole project first for speed
            if match is not None and not match.search(proj.name):
                continue
        for transcript in proj.glob("*.jsonl"):
            # skip agent runner side files if any
            if transcript.name.startswith("."):
                continue
            sid = transcript.stem
            mtime = transcript.stat().st_mtime
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
            # count lines as rough message volume
            try:
                nlines = sum(1 for _ in transcript.open(errors="ignore"))
            except OSError:
                nlines = None
            title = (last[:80] + "…") if len(last) > 80 else (last or sid[:12])
            resume = f'cd {json.dumps(proj_cwd)} && claude --resume {sid}'
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
) -> list[ChatHit]:
    """Scan local agent stores. match=None means all paths; 'greenmark' uses built-in regex."""
    if match is None or match == "" or match == "all":
        pat = None
    elif match in ("greenmark", "gm", "gmw"):
        pat = _GREENMARK_RE
    else:
        pat = re.compile(match, re.I)

    tools_set = {t.lower() for t in tools}
    hits: list[ChatHit] = []
    if "grok" in tools_set:
        hits.extend(scan_grok(match=pat, recent_hours=recent_hours))
    if "claude" in tools_set:
        hits.extend(scan_claude(match=pat, recent_hours=recent_hours))

    want = set(statuses) if statuses else None
    if want:
        # abandoned ≡ stale
        if "abandoned" in want:
            want = (want - {"abandoned"}) | {"stale"}
        hits = [h for h in hits if h.status in want]

    hits.sort(key=lambda h: h.mtime, reverse=True)
    return hits[: max(1, limit)]


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
        "3. Resume with the command in the last column (Claude: `--resume`; Grok: open cwd then `/resume <id>`).",
        "4. Cross-check greenmux registry ghosts (`greenmux ls` / gmux status `?all=1`) — tmux dead ≠ chat dead.",
        "",
    ]
    return "\n".join(lines)
