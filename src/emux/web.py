"""emux web — a persistent local daemon with monitoring + chat views.

`emux web` starts a long-running HTTP server (the daemon) that exposes the
same registry + tmux operations as the MCP server, plus a browser UI with
several views over the sessions emux knows about:

- chat     — one session as a live screen you can type into. The pane updates
             in place (it is the rendered terminal, not a growing transcript);
             your keystrokes are logged as a chat above it.
- grid     — every session as a live mini-pane tile, all streaming at once.
- groups   — the same tiles sectioned by registry tag.
- activity — change-detection strips per session: which panes moved, when.
- flow     — agent topology: a layered hierarchy built from registry `manages`
             edges (orchestrators on top, the agents they drive below);
             sessions with no relationships sit in an "unconnected" row.

Design principles (same as the MCP server):
- Operates on EXISTING tmux sessions only. Never spawns, never kills.
- The registry is metadata; live truth comes from `tmux list-sessions`.
- Binds 127.0.0.1 by default. Localhost is NOT a security boundary — any web
  page in your browser can reach a localhost port — so the API rejects
  foreign Host headers (DNS-rebind defense) and cross-origin POSTs. There is
  still no authentication; only widen `--host` on a network you trust.

Performance: a single background thread captures every live pane on a timer
into a shared cache, so N browser tabs watching M sessions cost one capture
sweep, not N×M. The cache also evicts sessions once tmux reaps them.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import server as _server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8689

# Background capture loop cadence, activity ring-buffer size, and how long a
# cached pane frame is considered fresh (covers the loop missing a tick).
_POLL_INTERVAL = 1.5
_SAMPLE_WINDOW = 60
_CACHE_TTL = 5.0

_LOCALHOSTS = {"127.0.0.1", "localhost", "::1", ""}

# Spinner / progress glyphs that animate without representing real work:
# braille (Claude Code's thinking spinner), block/quadrant spinners, and a
# handful of common dot/arc spinners. Stripped before change-detection so an
# idle session with a spinning cursor doesn't read as perpetually "active".
_SPINNER_RE = re.compile(
    r"[⠀-⣿▀-▟●○◐◑◒◓"
    r"◜◝◞◟◢◣◤◥"
    r"⠋⠹⠙⠸⠦⠇⠏]"
)

# Daemon-side state, shared across all clients and guarded by one lock.
_ACTIVITY: dict[str, dict[str, Any]] = {}   # session -> {norm, changed, last_change, samples}
_CACHE: dict[str, dict[str, Any]] = {}      # session -> {content, ts, lines}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# session listing
# ---------------------------------------------------------------------------

def sessions_payload() -> dict[str, Any]:
    """Merged registry + live view: registered entries first, then live unregistered."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed", "sessions": []}
    live = _server._live_sessions()
    registry = _server._load_registry()
    live_by_name = {s["name"]: s for s in live}
    sessions = []
    for name, entry in sorted(registry.items()):
        target = entry.get("session")
        cwd = live_by_name.get(target, {}).get("cwd") or entry.get("cwd")
        sessions.append({
            "name": name,
            "session": target,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "manages": entry.get("manages") or [],
            "registered": True,
            "live": target in live_by_name,
            "attached": live_by_name.get(target, {}).get("attached", False),
            "created_unix": live_by_name.get(target, {}).get("created_unix"),
            "cwd": cwd,
            "company": _detect_company(cwd),
        })
    registered_targets = {e.get("session") for e in registry.values()}
    for s in live:
        if s["name"] in registered_targets:
            continue
        sessions.append({
            "name": s["name"],
            "session": s["name"],
            "description": None,
            "tags": [],
            "manages": [],
            "registered": False,
            "live": True,
            "attached": s.get("attached", False),
            "created_unix": s.get("created_unix"),
            "cwd": s.get("cwd"),
            "company": _detect_company(s.get("cwd")),
        })
    return {"ok": True, "sessions": sessions}


# ---------------------------------------------------------------------------
# capture + change detection
# ---------------------------------------------------------------------------

def capture_payload(session: str, lines: int = 300) -> dict[str, Any]:
    """Capture the active pane of `session` (raw tmux session name). Always live
    — the chat view wants fresh, deep scrollback for one session, which is cheap."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    code, out, err = _server._run_tmux([
        "capture-pane", "-t", session, "-p", "-S", f"-{lines}",
    ])
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed", "stderr": err}
    return {"ok": True, "session": session, "content": out}


# Which AI/tool is running in a pane. Primary signal is tmux's live foreground
# process (`pane_current_command`); content signatures catch node-wrapped CLIs
# that all report as "node". Glyphs are monochrome Unicode to match the theme.
# (key, label, glyph, command-name substrings, content substrings)
_AGENT_TABLE = [
    ("claude", "Claude Code", "✳", ("claude",),
     ("claude code", "anthropic", "esc to interrupt", "? for shortcuts", "bypass permissions")),
    ("codex", "Codex", "◇", ("codex",), ("openai codex", "codex cli")),
    ("gemini", "Gemini", "♊", ("gemini",), ("gemini cli", "google gemini")),
    ("grok", "Grok", "⚡", ("grok",), ("grok", "xai")),
    ("opencode", "opencode", "❖", ("opencode",), ("opencode",)),
    ("hermes", "Hermes", "☿", ("hermes",), ("hermes", " nous ")),
    ("aider", "Aider", "✦", ("aider",), ("aider ",)),
]
# Which company/context a session belongs to, from its cwd.
# (key, label, color, roots, keywords). `roots` are the repos-<x>/ trees and are
# authoritative; `keywords` catch a company's repos that live in the generic
# ~/repos/ tree (e.g. repos/greenmark-claude-toolkit). Roots win over keywords.
_COMPANY_TABLE = [
    ("eidos", "Eidos", "#7dd3fc", ("repos-eidos-agi", "repos-eidos-capital"), ("eidos",)),
    ("greenmark", "Greenmark Waste", "#7bd88f", ("repos-greenmark",), ("greenmark",)),
    ("aic", "AIC", "#c4a3ff", ("repos-aic", "repos-aic-holdings"), ("aic-",)),
    ("jetta", "Jetta", "#ffb27d", ("repos-jetta",), ("jetta",)),
    ("momentito", "Momentito", "#ff9ecf", ("repos-momentito",), ("momentito",)),
    ("rhea", "Rhea Impact", "#9ae6e6", ("repos-rheaimpact",), ("rheaimpact", "rhea-impact")),
    ("asmp", "ASMP", "#d0c0a0", ("repos-asmp",), ("asmp",)),
    ("boone", "Boone Voyage", "#4db6c9", ("repos-bv",), ("boonevoyage", "boone-voyage")),
    ("personal", "Personal", "#f0d060", ("repos-personal", "repos-local"), ()),
]


def _detect_company(cwd: str | None) -> dict[str, str]:
    """Best-effort: which company/context owns a session, from its working dir.
    Match the repos-<x>/ root first (authoritative); fall back to a company
    keyword anywhere in the path, so e.g. repos/greenmark-claude-toolkit lands."""
    low = (cwd or "").lower()
    if not low:
        return {"company": "", "label": "", "color": ""}
    for key, label, color, roots, _kw in _COMPANY_TABLE:
        if any(r in low for r in roots):
            return {"company": key, "label": label, "color": color}
    for key, label, color, _roots, kw in _COMPANY_TABLE:
        if any(k in low for k in kw):
            return {"company": key, "label": label, "color": color}
    return {"company": "", "label": "", "color": ""}


def _known_hosts() -> list[str]:
    """Machines you can spawn on: local + ~/.ssh/config aliases + hosts already
    used by registered sessions."""
    hosts: list[str] = []
    cfg = Path(os.path.expanduser("~/.ssh/config"))
    if cfg.is_file():
        try:
            for line in cfg.read_text(errors="replace").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() == "host":
                    hosts.extend(h for h in parts[1:] if "*" not in h and "?" not in h)
        except OSError:
            pass
    try:
        for e in _server._load_registry().values():
            h = e.get("host")
            if isinstance(h, str) and h:
                hosts.append(h)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), ["local"]
    for h in hosts:
        if h not in seen and h != "local":
            seen.add(h)
            out.append(h)
    return out


_DIRS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DIRS_TTL = 120.0


def _candidate_dirs(host: str | None = None, limit: int = 200) -> list[str]:
    """Working directories that ACTUALLY EXIST on `host` (None/local = this box).

    The choices cascade: which directories you can start a session in depends on
    which machine you picked, so this is always resolved against a real machine —
    never a local list handed to a remote spawn."""
    key = host or "local"
    hit = _DIRS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _DIRS_TTL:
        return hit[1]
    dirs: list[str] = []
    if key == "local":
        for base in sorted(Path.home().glob("repos*")):
            if base.is_dir():
                dirs.extend(str(d) for d in sorted(base.iterdir())
                            if d.is_dir() and not d.name.startswith("."))
    else:
        import subprocess
        try:
            # one ssh round-trip; list the repo trees one level deep
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", key,
                 "for b in ~/repos*; do [ -d \"$b\" ] && "
                 "for d in \"$b\"/*/; do [ -d \"$d\" ] && echo \"${d%/}\"; done; done"],
                capture_output=True, text=True, timeout=25,
            )
            if proc.returncode == 0:
                dirs = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        except (subprocess.TimeoutExpired, OSError):
            dirs = []
    dirs = dirs[:limit]
    _DIRS_CACHE[key] = (time.time(), dirs)
    return dirs


def _claude_json(prompt: str, timeout: int = 90) -> dict[str, Any]:
    """One fixed-cost `claude -p` call that must answer with a JSON object.
    Never the API — the CLI only."""
    import shutil
    import subprocess
    claude = shutil.which("claude")
    if claude is None:
        return {"_error": "claude CLI not on PATH"}
    try:
        proc = subprocess.run(
            [claude, "-p", prompt, "--model",
             os.environ.get("EMUX_NAV_MODEL", "claude-haiku-4-5-20251001")],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"_error": f"claude -p failed: {e}"}
    m = re.search(r"\{.*\}", proc.stdout or "", re.DOTALL)
    if not m:
        return {"_error": "no JSON in model reply", "raw": (proc.stdout or "")[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"_error": "unparseable JSON", "raw": m.group(0)[:300]}


def _suggest_session(intent: str, host: str | None = None) -> dict[str, Any]:
    """Turn plain English into a session, by CLASSIFYING it down the cascade of
    real choices — never by inventing values.

    The choices depend on each other: the machine determines which directories
    exist, so we resolve the machine FIRST, enumerate that machine's real repo
    dirs, and only then choose among THOSE. The model picks by index from a list
    of things that actually exist, so it cannot hallucinate a path onto a box.

    `host` pins the machine (the user already chose it) and skips step 1.
    """
    hosts = _known_hosts()

    # --- step 1: which machine? (skipped when the user already picked one) ---
    if host:
        chosen_host, host_why = host, ""
    else:
        r1 = _claude_json(
            "Pick the MACHINE to start a terminal session on.\n\n"
            f"WHAT THE USER WANTS TO DO: {intent}\n\n"
            "MACHINES (choose exactly one of these names):\n"
            + "\n".join(f"  {h}" for h in hosts) + "\n\n"
            'Reply with ONE line of JSON: {"host": "<one of the names above>", '
            '"why": "<short reason>"}\n'
            'Choose "local" unless the intent clearly names a remote machine or '
            "needs one (heavy compute, a server-side service, a box by name)."
        )
        if "_error" in r1:
            return r1
        chosen_host = r1.get("host") or "local"
        host_why = r1.get("why") or ""
        if chosen_host not in hosts:          # constrain to a real machine
            chosen_host = "local"

    # --- step 2: what actually exists ON that machine ---
    dirs = _candidate_dirs(None if chosen_host == "local" else chosen_host)
    if not dirs:
        return {"host": chosen_host, "name": "", "cwd": "", "command": "",
                "why": (host_why + " — could not list directories on "
                        f"{chosen_host}; enter one manually.").strip(" —"),
                "dirs": [], "verified": False}

    # --- step 3: choose among THOSE directories (a constrained choice, by index) ---
    listing = "\n".join(f"  [{i}] {d}" for i, d in enumerate(dirs))
    r2 = _claude_json(
        f"Place a terminal session on machine '{chosen_host}'.\n\n"
        f"WHAT THE USER WANTS TO DO: {intent}\n\n"
        "DIRECTORIES THAT EXIST ON THIS MACHINE — choose ONE by its index:\n"
        f"{listing}\n\n"
        'Reply with ONE line of JSON: {"dir_index": <int>, '
        '"name": "<short-kebab-session-name>", '
        '"command": "<command to run, e.g. claude; empty string for a plain shell>", '
        '"why": "<one short sentence: why this directory>"}\n'
        "Prefer a cockpit directory for planning/strategy work and the relevant "
        "repo for code work. dir_index MUST be one of the indices listed above."
    )
    if "_error" in r2:
        return r2
    idx = r2.get("dir_index")
    cwd, verified = "", False
    if isinstance(idx, int) and 0 <= idx < len(dirs):
        cwd, verified = dirs[idx], True   # a real dir on that machine, not invented
    why = " ".join(x for x in (host_why, r2.get("why") or "") if x).strip()
    return {
        "host": chosen_host,
        "name": r2.get("name") or "",
        "cwd": cwd,
        "command": r2.get("command") or "",
        "why": why,
        "dirs": dirs,          # so the UI's directory choices match the machine
        "verified": verified,  # the path was CHOSEN from real dirs, not invented
    }


def _spawn_session(data: dict[str, Any]) -> dict[str, Any]:
    """Create a new session (local or remote) via the server's spawn primitive."""
    import asyncio
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing_name"}
    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    cwd = (data.get("cwd") or "").strip() or None
    command = (data.get("command") or "").strip() or None
    try:
        return asyncio.run(_server.tmux_spawn(
            name=name, command=command, host=host, cwd=cwd,
            gui=bool(data.get("gui", False)),
            description=(data.get("description") or "").strip() or None,
            tags=data.get("tags") or None,
        ))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _iterm_attach(session: str) -> tuple[bool, str | None]:
    """Open a NEW iTerm2 window attached to `session`, driven by AppleScript —
    no `.command` file, so macOS Gatekeeper doesn't throw a quarantine prompt."""
    import platform
    import shlex
    import subprocess
    if platform.system() != "Darwin":
        return False, "macOS/iTerm2 only"
    attach = f"tmux attach -t {shlex.quote(session)}"
    script = (
        'tell application "iTerm2"\n'
        " create window with default profile\n"
        " tell current session of current window to write text "
        f'"{attach}"\n'
        " activate\n"
        "end tell"
    )
    try:
        g = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if g.returncode != 0:
        return False, (g.stderr or g.stdout or "osascript failed").strip()
    return True, None


_SHELLS = {"zsh", "-zsh", "bash", "-bash", "fish", "sh", "-sh"}
_EDITORS = {"vim", "nvim", "vi", "nano", "emacs"}


def _pane_command(session: str) -> str:
    """The live foreground process name in the session's active pane."""
    code, out, _ = _server._run_tmux(
        ["display-message", "-p", "-t", session, "#{pane_current_command}"]
    )
    return out.strip() if code == 0 else ""


def _detect_agent(session: str, content: str) -> dict[str, str]:
    """Best-effort: which AI system / tool is currently running in the pane."""
    cmd = _pane_command(session).lower()
    low = (content or "").lower()
    for key, label, glyph, cmds, _sigs in _AGENT_TABLE:
        if any(c in cmd for c in cmds):
            return {"agent": key, "label": label, "glyph": glyph}
    for key, label, glyph, _cmds, sigs in _AGENT_TABLE:
        if any(s in low for s in sigs):
            return {"agent": key, "label": label, "glyph": glyph}
    if cmd in _SHELLS:
        return {"agent": "shell", "label": "shell", "glyph": "$"}
    if cmd in _EDITORS:
        return {"agent": "editor", "label": cmd, "glyph": "✎"}
    # Claude Code retitles its pane to a bare version string (e.g. "2.1.207").
    if re.match(r"^\d+\.\d+\.\d+", cmd):
        return {"agent": "claude", "label": "Claude Code", "glyph": "✳"}
    if cmd:
        return {"agent": cmd, "label": cmd, "glyph": "▸"}
    return {"agent": "unknown", "label": "—", "glyph": "·"}


def _normalize(content: str) -> str:
    """Reduce a pane capture to its meaningful content for change detection:
    drop per-line trailing whitespace, trailing blank lines, and spinner glyphs.
    Two captures that differ only by a cursor blink or a spinner frame normalize
    to the same string."""
    lines = [ln.rstrip() for ln in content.split("\n")]
    text = "\n".join(lines).rstrip("\n")
    return _SPINNER_RE.sub("", text)


def _observe(session: str, content: str) -> dict[str, Any]:
    """Fold one capture into the session's activity record. Returns its meta."""
    now = time.time()
    norm = _normalize(content)
    with _LOCK:
        st = _ACTIVITY.setdefault(session, {
            "norm": None, "changed": False, "last_change": None,
            "samples": deque(maxlen=_SAMPLE_WINDOW),
        })
        changed = st["norm"] is not None and st["norm"] != norm
        if changed:
            st["last_change"] = now
        st["norm"] = norm
        st["changed"] = changed
        st["samples"].append(1 if changed else 0)
        return _meta_locked(st, now)


def _meta_locked(st: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "changed": st["changed"],
        "last_change_age": (now - st["last_change"]) if st["last_change"] else None,
        "activity": list(st["samples"]),
    }


def _meta(session: str) -> dict[str, Any]:
    """Read the current activity meta for a session without re-observing."""
    with _LOCK:
        st = _ACTIVITY.get(session)
        if not st:
            return {"changed": False, "last_change_age": None, "activity": []}
        return _meta_locked(st, time.time())


def _capture_and_observe(session: str, lines: int) -> str:
    """Capture a pane, fold it into activity state, detect the running agent,
    and refresh the cache."""
    cap = capture_payload(session, lines)
    content = cap.get("content", "") if cap.get("ok") else ""
    _observe(session, content)
    agent = _detect_agent(session, content)
    with _LOCK:
        _CACHE[session] = {"content": content, "ts": time.time(), "lines": lines, "agent": agent}
    return content


def poll_once(lines: int = 14) -> None:
    """One capture sweep over all live sessions; evicts state for dead ones.
    Called on a timer by the background loop, and directly by tests."""
    if _server._resolve_tmux() is None:
        return
    live = _server._live_sessions()
    live_names = {s["name"] for s in live}
    for s in live:
        _capture_and_observe(s["name"], lines)
    with _LOCK:
        for dead in [k for k in _ACTIVITY if k not in live_names]:
            _ACTIVITY.pop(dead, None)
            _CACHE.pop(dead, None)


def grid_payload(lines: int = 14) -> dict[str, Any]:
    """Session list with a mini pane capture + activity meta per live session.
    Serves from the daemon cache when fresh; captures on miss (cold start, or
    when the poll loop isn't running, e.g. under tests)."""
    base = sessions_payload()
    if not base["ok"]:
        return base
    now = time.time()
    for item in base["sessions"]:
        if item["live"]:
            with _LOCK:
                ce = _CACHE.get(item["session"])
                content = ce["content"] if (ce is not None and (now - ce["ts"]) < _CACHE_TTL) else None
            if content is None:
                content = _capture_and_observe(item["session"], lines)
            with _LOCK:
                ce = _CACHE.get(item["session"])
            item["content"] = content
            item["agent"] = (ce or {}).get("agent") or {"agent": "unknown", "label": "—", "glyph": "·"}
            item.update(_meta(item["session"]))
        else:
            item["content"] = ""
            item["changed"] = False
            item["last_change_age"] = None
            item["activity"] = []
            item["agent"] = {"agent": "gone", "label": "", "glyph": ""}
    return base


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def send_payload(session: str, keys: str, literal: bool = True, enter: bool = True) -> dict[str, Any]:
    """Send keys to `session`. literal=True sends text verbatim (`send-keys -l`),
    so chat input like "C-c" types those characters; literal=False interprets
    tmux key names (used by the UI's control-key chips)."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    if literal:
        if keys:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "-l", keys])
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
        if enter:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "Enter"])
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    else:
        args = ["send-keys", "-t", session, keys]
        if enter:
            args.append("Enter")
        code, _, err = _server._run_tmux(args)
        if code != 0:
            return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    return {"ok": True, "session": session, "sent": keys, "literal": literal, "enter": enter}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>emux — control room</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><rect width='16' height='16' fill='%230c0a07'/><rect x='3' y='3' width='10' height='10' rx='2' fill='%23ffb000'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0c0a07;
  --bg-raise:#141008;
  --bg-card:#191307;
  --amber:#ffb000;
  --amber-dim:#b87d00;
  --amber-faint:#3d2e0a;
  --text:#e8d5a3;
  --text-dim:#8a774d;
  --live:#7dff8a;
  --stale:#ff5d5d;
  --line:#2a2113;
  --user:#ffd569;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);
  color:var(--text);
  font:14px/1.5 "IBM Plex Mono",ui-monospace,monospace;
  display:flex;
  overflow:hidden;
}
body::after{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:99;
  background:
    repeating-linear-gradient(0deg, rgba(0,0,0,.16) 0 1px, transparent 1px 3px),
    radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.45) 100%);
}
#side{
  width:280px;flex:none;height:100%;display:flex;flex-direction:column;
  background:var(--bg-raise);border-right:1px solid var(--line);
}
#brand{padding:18px 18px 10px}
#brand h1{
  font-family:"VT323",monospace;font-size:44px;font-weight:400;letter-spacing:2px;
  color:var(--amber);text-shadow:0 0 18px rgba(255,176,0,.45),0 0 2px rgba(255,176,0,.9);
}
#brand small{color:var(--text-dim);font-size:11px;letter-spacing:3px;text-transform:uppercase}
#tagbar{display:flex;flex-wrap:wrap;gap:4px;padding:4px 8px 0}
#tagbar:empty{display:none}
.tagchip{font-size:10px;letter-spacing:.5px;padding:2px 7px;border:1px solid var(--line);
  border-radius:10px;color:var(--text-dim);cursor:pointer;user-select:none;white-space:nowrap}
.tagchip:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.tagchip.on{background:var(--amber);border-color:var(--amber);color:#1a1200;font-weight:700}
.tagchip .cnt{opacity:.6;margin-left:3px}
.tagchip.clr{color:var(--text-dim)}
.cochip{font-size:10px;letter-spacing:.3px;padding:2px 7px;border:1px solid;border-radius:10px;
  cursor:pointer;user-select:none;white-space:nowrap;font-weight:700;opacity:.9}
.cochip:hover{opacity:1}
.cochip.on{color:#151005}
.cochip .cnt{opacity:.7;margin-left:3px;font-weight:400}
.cco{font-size:9px;font-weight:700;letter-spacing:.3px;padding:1px 6px;border-radius:8px;
  color:#151005;white-space:nowrap}
#sessions{flex:1;overflow-y:auto;padding:8px}
.card{
  border:1px solid var(--line);border-left:3px solid var(--amber-faint);
  background:var(--bg-card);padding:10px 12px;margin-bottom:8px;cursor:pointer;
  transition:border-color .15s, transform .15s;
}
.card:hover{border-color:var(--amber-dim);transform:translateX(2px)}
.card.active{border-left-color:var(--amber);box-shadow:0 0 14px rgba(255,176,0,.12) inset}
.card .nm{color:var(--amber);font-weight:600}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.dot.live{background:var(--live);box-shadow:0 0 6px var(--live)}
.dot.stale{background:var(--stale);box-shadow:0 0 6px var(--stale)}
.card .sub{color:var(--text-dim);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .badges{margin-top:4px;font-size:10px}
.card .badges span{border:1px solid var(--line);color:var(--text-dim);padding:0 5px;margin-right:4px}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--text-dim);font-size:10px;letter-spacing:1px}
#main{flex:1;height:100%;display:flex;flex-direction:column;min-width:0}
#topbar{
  flex:none;display:flex;align-items:center;gap:14px;
  padding:10px 22px;border-bottom:1px solid var(--line);background:var(--bg-raise);
}
#topbar #title{font-family:"VT323",monospace;font-size:26px;color:var(--amber);letter-spacing:1px}
#topbar #status{font-size:11px;color:var(--text-dim);letter-spacing:2px;text-transform:uppercase}
#topbar #status.err{color:var(--stale)}
#tabs{margin-left:auto;display:flex;gap:6px}
.tab{
  font-family:"VT323",monospace;font-size:17px;letter-spacing:2px;padding:3px 14px;
  background:transparent;color:var(--text-dim);border:1px solid var(--line);cursor:pointer;
}
.tab:hover{color:var(--amber);border-color:var(--amber-dim)}
.tab.on{background:var(--amber);color:#160f00;border-color:var(--amber)}
#views{flex:1;overflow-y:auto;padding:18px}
.tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.tile{
  border:1px solid var(--line);background:#080705;cursor:pointer;overflow:hidden;
  display:flex;flex-direction:column;transition:border-color .2s, box-shadow .4s;
}
.tile:hover{border-color:var(--amber-dim)}
.tile.hot{border-color:var(--amber);box-shadow:0 0 16px rgba(255,176,0,.25)}
.tile.dead{opacity:.45}
.tile header{
  display:flex;align-items:baseline;gap:8px;padding:6px 10px;
  background:var(--bg-card);border-bottom:1px solid var(--line);
}
.tile header .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile header .age{margin-left:auto;font-size:10px;color:var(--text-dim);letter-spacing:1px;white-space:nowrap}
.tile pre{
  flex:1;font:9.5px/1.35 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:8px 10px;height:190px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-end;
}
.tile pre.empty{color:var(--text-dim);font-style:italic;justify-content:center;text-align:center}
.group{margin-bottom:26px}
.group h2{
  font-family:"VT323",monospace;font-size:22px;letter-spacing:2px;color:var(--amber-dim);
  border-bottom:1px solid var(--line);margin-bottom:12px;padding-bottom:4px;
}
.group h2 .cnt{color:var(--text-dim);font-size:14px}
.actrows{display:flex;flex-direction:column;gap:10px;max-width:980px}
.actrow{
  display:flex;align-items:center;gap:14px;border:1px solid var(--line);
  background:var(--bg-card);padding:10px 14px;cursor:pointer;
}
.actrow:hover{border-color:var(--amber-dim)}
.actrow .nm{color:var(--amber);font-weight:600;width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:none}
.cells{display:flex;gap:2px;flex:1;min-width:0}
.cell{width:9px;height:18px;background:#1a140a;flex:none}
.cell.on{background:var(--amber);box-shadow:0 0 5px rgba(255,176,0,.6)}
.cell.recent{background:var(--user)}
.actrow .age{font-size:11px;color:var(--text-dim);width:120px;text-align:right;flex:none;letter-spacing:1px}
/* flow view — live mini-pane boxes over an SVG edge layer */
#flowwrap{position:relative;margin:0 auto}
#flowsvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.fbox{
  position:absolute;transform:translate(-50%,-50%);z-index:1;
  border:1px solid var(--line);background:#080705;cursor:pointer;overflow:hidden;
  transition:border-color .2s, box-shadow .4s;
}
.fbox:hover{border-color:var(--amber-dim)}
.fbox.hot{border-color:var(--amber);box-shadow:0 0 16px rgba(255,176,0,.25)}
.fbox.dead{opacity:.45}
.fbox .ftitle{display:flex;align-items:center;gap:6px;padding:5px 9px;background:var(--bg-card);border-bottom:1px solid var(--line)}
.fbox .ftitle .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fbox .ftitle .ag{margin-left:auto;color:var(--amber-dim);font-size:10px;letter-spacing:.5px;white-space:nowrap}
.fbox pre{
  font:8.5px/1.32 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:6px 9px;height:96px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-end;
}
.fbox pre.empty{color:var(--text-dim);font-style:italic;justify-content:center;text-align:center}
.agentbadge{color:var(--amber-dim);font-size:10px;letter-spacing:1px;margin-left:6px}
.agi{font-weight:700}
.ag-claude{color:#d97757}.ag-codex{color:#10a37f}.ag-gemini{color:#5a8dff}
.ag-grok{color:#e8e8e8}.ag-opencode{color:#b57bff}.ag-aider{color:#f0a020}
.ag-hermes{color:#b07de0}.ag-shell{color:#8a8a72}.ag-editor{color:#8a8a72}
.edge{stroke:var(--amber);stroke-width:2;fill:none;stroke-dasharray:7 5;animation:flow 1.1s linear infinite;
  filter:drop-shadow(0 0 4px rgba(255,176,0,.4))}
@keyframes flow{to{stroke-dashoffset:-12}}
.rowlabel{fill:var(--text-dim);font:10px "IBM Plex Mono",monospace;letter-spacing:2px;text-transform:uppercase}
.sep{stroke:var(--line);stroke-width:1;stroke-dasharray:3 5}
#flowhint{color:var(--text-dim);font-size:11px;font-style:italic;margin-top:6px}
#flowhint code{color:var(--amber-dim);font-style:normal}
#chat{flex:1;overflow-y:auto;padding:22px;display:none;flex-direction:column;gap:12px}
.bubble{max-width:88%;padding:10px 14px;border:1px solid var(--line);position:relative}
.bubble .who{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim);margin-bottom:5px}
.bubble.user{
  align-self:flex-end;background:#1d1605;border-color:var(--amber-faint);
  color:var(--user);border-radius:10px 10px 0 10px;
}
.bubble.sys{align-self:center;color:var(--text-dim);font-size:11px;border:none;font-style:italic}
#screen-bubble{
  align-self:flex-start;width:100%;max-width:100%;
  background:#080705;border:1px solid var(--line);border-radius:10px 10px 10px 0;
}
#screen{
  font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:4px 2px 2px;
}
#screen.dimmed{opacity:.35}
.cursorblock{display:inline-block;width:8px;height:14px;background:var(--amber);
  vertical-align:-2px;animation:blink 1.1s steps(1) infinite;box-shadow:0 0 8px rgba(255,176,0,.8)}
@keyframes blink{50%{opacity:0}}
#empty{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--text-dim);height:100%}
#empty .glyph{font-family:"VT323",monospace;font-size:80px;color:var(--amber-faint);text-shadow:0 0 30px rgba(255,176,0,.15)}
#composer{flex:none;border-top:1px solid var(--line);background:var(--bg-raise);padding:12px 22px 16px}
#chips{display:flex;gap:8px;margin-bottom:10px}
.chip{
  font:11px "IBM Plex Mono",monospace;color:var(--amber-dim);background:transparent;
  border:1px solid var(--line);padding:3px 10px;cursor:pointer;letter-spacing:1px;
}
.chip:hover{color:var(--amber);border-color:var(--amber-dim)}
#row{display:flex;gap:10px}
#input{
  flex:1;background:#080705;border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber);
}
#input:focus{border-color:var(--amber-dim);box-shadow:0 0 12px rgba(255,176,0,.1)}
#send{
  font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 26px;
  background:var(--amber);color:#160f00;border:none;cursor:pointer;
}
#send:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
/* recency-tiered age coloring (#17) */
.age.t-now{color:var(--amber)}
.age.t-min{color:var(--amber-dim)}
.age.t-old{color:var(--text-dim)}
/* stale sessions in the sidebar (#19) */
.card.gone{opacity:.5}
.card.gone .nm{text-decoration:line-through}
/* attached marker (#18) */
.att{color:var(--live);font-size:10px;letter-spacing:1px;margin-left:6px}
/* sidebar filter (#7) */
#filter{
  margin:0 8px 8px;width:calc(100% - 16px);background:#080705;border:1px solid var(--line);
  color:var(--text);font:11px "IBM Plex Mono",monospace;padding:6px 9px;outline:none;
}
#filter:focus{border-color:var(--amber-dim)}
/* topbar action buttons (#12 #15) */
.act{
  font:11px "IBM Plex Mono",monospace;color:var(--amber-dim);background:transparent;
  border:1px solid var(--line);padding:3px 9px;cursor:pointer;letter-spacing:1px;
}
.act:hover{color:var(--amber);border-color:var(--amber-dim)}
/* word-wrap toggle off → horizontal scroll (#9) */
#screen.nowrap{white-space:pre;overflow-x:auto}
/* jump-to-bottom pill (#11) */
#jump{
  position:absolute;left:50%;transform:translateX(-50%);bottom:96px;display:none;
  font-family:"VT323",monospace;font-size:16px;letter-spacing:1px;
  background:var(--amber);color:#160f00;border:none;padding:4px 16px;cursor:pointer;
  box-shadow:0 0 14px rgba(255,176,0,.5);z-index:5;
}
/* zoom-in steer modal */
#modal{position:fixed;inset:0;z-index:200;display:none;align-items:center;justify-content:center}
#modal.open{display:flex}
#modalback{position:absolute;inset:0;background:rgba(6,4,2,.72);backdrop-filter:blur(2px)}
#modalpanel{
  position:relative;width:min(900px,86vw);height:min(620px,82vh);display:flex;flex-direction:column;
  background:var(--bg-raise);border:1px solid var(--amber-dim);box-shadow:0 0 50px rgba(255,176,0,.18);
  animation:zoomin .16s ease-out;
}
@keyframes zoomin{from{transform:scale(.92);opacity:.4}to{transform:scale(1);opacity:1}}
#modalhead{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line);background:var(--bg-card)}
#modalhead .nm{font-family:"VT323",monospace;font-size:24px;color:var(--amber);letter-spacing:1px}
#modalhead .ag{color:var(--amber-dim);font-size:12px;letter-spacing:1px}
#modalhead .st{margin-left:auto;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
#modalclose{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:14px;cursor:pointer;padding:2px 11px;margin-left:10px}
#modalclose:hover{color:var(--amber);border-color:var(--amber-dim)}
/* --- new-session modal --- */
#newmodal{display:none;position:fixed;inset:0;z-index:60}
#newmodal.open{display:block}
#newback{position:absolute;inset:0;background:rgba(0,0,0,.72)}
#newpanel{position:relative;margin:6vh auto;width:min(720px,92vw);background:var(--bg-card);
  border:1px solid var(--amber-faint);box-shadow:0 0 60px rgba(0,0,0,.7);display:flex;flex-direction:column}
#newhead{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line)}
#newhead .nm{font-family:"VT323",monospace;font-size:24px;color:var(--amber);letter-spacing:1px}
#newhead .st{margin-left:auto;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
#newclose{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:14px;cursor:pointer;padding:2px 11px;margin-left:10px}
#newclose:hover{color:var(--amber);border-color:var(--amber-dim)}
#newbody{padding:16px;display:flex;flex-direction:column;gap:6px;max-height:66vh;overflow-y:auto}
#newbody label{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim);margin-top:8px}
#newbody label small{text-transform:none;letter-spacing:0;margin-left:8px;opacity:.7}
#newbody input[type=text],#newbody input:not([type]),#newbody select{
  background:var(--bg);border:1px solid var(--line);color:var(--amber);font-family:inherit;
  font-size:13px;padding:7px 10px;width:100%}
#newbody input:focus,#newbody select:focus{outline:none;border-color:var(--amber-dim)}
#newbody .introw{display:flex;gap:8px}
#newbody .fgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
#newbody .chk{display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:12px;color:var(--amber-dim)}
#newbody .chk input{width:auto}
#newbody .chk.pin{margin-top:6px;font-size:10px;opacity:.75}
#newbody input:disabled{opacity:.5}
#dircount{opacity:.7}
#newsuggest{background:transparent;border:1px solid var(--amber-faint);color:var(--amber-dim);
  font-family:inherit;font-size:12px;cursor:pointer;padding:0 14px;white-space:nowrap}
#newsuggest:hover{color:var(--amber);border-color:var(--amber-dim)}
#newsuggest:disabled{opacity:.5;cursor:default}
#newwhy{font-size:11px;color:var(--amber-dim);font-style:italic;min-height:0}
#newwhy:empty{display:none}
#newerr{color:#ff5f56;font-size:11px}
#newerr:empty{display:none}
#newfoot{padding:12px 16px;border-top:1px solid var(--line);display:flex;justify-content:flex-end}
#newcreate{font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:5px 28px;
  background:var(--amber);border:none;color:#151005;cursor:pointer;font-weight:700}
#newcreate:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
#newcreate:disabled{opacity:.5;cursor:default;box-shadow:none}
#modaliterm{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:13px;cursor:pointer;padding:2px 11px;margin-left:10px}
#modaliterm:hover{color:var(--amber);border-color:var(--amber-dim)}
#modaliterm:disabled{opacity:.6;cursor:default}
#modalscreen{
  flex:1;overflow-y:auto;font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:14px 16px;background:#080705;
}
/* live classifier strip (emux judge) */
#modaljudge{display:none;align-items:center;gap:12px;padding:8px 16px;flex:none;
  border-bottom:1px solid var(--line);background:#0c0a06;font:11px "IBM Plex Mono",monospace}
#modaljudge.on{display:flex}
#modaljudge .jstate{font-weight:700;letter-spacing:1px;text-transform:uppercase;font-size:12px;white-space:nowrap}
#modaljudge .jconf{color:var(--text-dim);font-size:10px;white-space:nowrap}
#modaljudge .jsum{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modaljudge .jflag{border:1px solid #7a3a1a;color:#ff9f43;padding:0 5px;font-size:10px;
  text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.s-running{color:#8fd88f}.s-done_idle{color:#8a8a72}.s-error{color:#ff5f56}
.s-thrashing{color:#ff9f43}.s-stuck{color:#ffb000}.s-waiting_human{color:#38d9ff}
.s-waiting_external{color:#7a8fd8}.s-planning{color:#d0b24a}.s-editing{color:#d0b24a}.s-dead{color:#666}
#modalchips{display:flex;gap:8px;padding:10px 16px 0}
#modalrow{display:flex;gap:10px;padding:10px 16px 14px}
#modalinput{flex:1;background:#080705;border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber)}
#modalinput:focus{border-color:var(--amber-dim);box-shadow:0 0 12px rgba(255,176,0,.1)}
#modalsend{font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 24px;
  background:var(--amber);color:#160f00;border:none;cursor:pointer}
#modalsend:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--amber-faint)}
::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>
<aside id="side">
  <div id="brand"><h1>EMUX</h1><small>control room</small></div>
  <input id="filter" placeholder="filter sessions…" autocomplete="off" spellcheck="false">
  <div id="tagbar"></div>
  <div id="sessions"></div>
  <footer id="footer">daemon · v__VERSION__</footer>
</aside>
<main id="main">
  <div id="topbar">
    <span id="title">grid</span>
    <span id="status">connecting…</span>
    <button id="attachbtn" class="act" style="display:none">⧉ copy attach</button>
    <button id="newbtn" class="act">+ NEW SESSION</button>
    <button id="refreshbtn" class="act">↻ refresh</button>
    <div id="tabs">
      <button class="tab" data-mode="grid">GRID</button>
      <button class="tab" data-mode="groups">GROUPS</button>
      <button class="tab" data-mode="activity">ACTIVITY</button>
      <button class="tab" data-mode="flow">FLOW</button>
    </div>
  </div>
  <div id="views"></div>
  <div id="chat"></div>
  <button id="jump">↓ jump to bottom</button>
  <div id="composer" style="display:none">
    <div id="chips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
      <button class="chip" id="wrapchip">WRAP: ON</button>
    </div>
    <div id="row">
      <input id="input" placeholder="type into the session… (Enter sends)" autocomplete="off" spellcheck="false">
      <button id="send">SEND</button>
    </div>
  </div>
</main>
<div id="modal">
  <div id="modalback"></div>
  <div id="modalpanel">
    <div id="modalhead">
      <span class="dot live"></span>
      <span class="nm" id="modalname"></span>
      <span class="ag" id="modalagent"></span>
      <span class="st" id="modalstatus">live</span>
      <button id="modaliterm" title="open this session in a new iTerm2 window (attached tmux)">⧉ iTerm2</button>
      <button id="modalclose">✕ close</button>
    </div>
    <div id="modaljudge"></div>
    <div id="modalscreen"></div>
    <div id="modalchips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
    </div>
    <div id="modalrow">
      <input id="modalinput" placeholder="prompt / steer this session… (Enter sends)" autocomplete="off" spellcheck="false">
      <button id="modalsend">SEND</button>
    </div>
  </div>
</div>

<div id="newmodal">
  <div id="newback"></div>
  <div id="newpanel">
    <div id="newhead">
      <span class="nm">+ new session</span>
      <span class="st" id="newstatus"></span>
      <button id="newclose">✕ close</button>
    </div>
    <div id="newbody">
      <label>what do you want to do?<small>optional — Claude picks the machine, folder, and command</small></label>
      <div class="introw">
        <input id="newintent" placeholder="e.g. plan the next Greenmark sprint / fix the helios auth bug" autocomplete="off">
        <button id="newsuggest">✦ suggest</button>
      </div>
      <div id="newwhy"></div>
      <div class="fgrid">
        <div>
          <label>machine<small>changes what directories exist below</small></label>
          <select id="newhost"></select>
          <label class="chk pin"><input type="checkbox" id="newpin"> pin this machine when suggesting</label>
        </div>
        <div><label>name</label><input id="newname" placeholder="session name" autocomplete="off"></div>
      </div>
      <label>directory<small id="dircount"></small></label>
      <input id="newcwd" list="dirlist" placeholder="/Users/…" autocomplete="off">
      <datalist id="dirlist"></datalist>
      <label>command<small>empty = plain shell</small></label>
      <input id="newcmd" placeholder="e.g. claude" autocomplete="off">
      <label class="chk"><input type="checkbox" id="newgui" checked> open an iTerm2 window attached to it</label>
      <div id="newerr"></div>
    </div>
    <div id="newfoot"><button id="newcreate">CREATE SESSION</button></div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const SVGNS="http://www.w3.org/2000/svg";
let mode="grid", current=null, grid=[], chatTimer=null, gridTimer=null, screenEl=null;
let filterStr="", flashOn=false, activeTag="", activeCompany="";
let flowSig=null, flowPre={}, flowBox={};   // flow view: rebuild only on topology change, else update panes in place
const BASE_TAB={grid:"GRID",groups:"GROUPS",activity:"ACTIVITY",flow:"FLOW"};

async function api(path,opts){const r=await fetch(path,opts);return r.json();}

function ageLabel(a){
  if(a===null||a===undefined)return "—";
  if(a<2)return "now";
  if(a<60)return Math.round(a)+"s ago";
  if(a<3600)return Math.round(a/60)+"m ago";
  return Math.round(a/3600)+"h ago";
}
function ageClass(a){            // recency tiers (#17)
  if(a===null||a===undefined)return "t-old";
  if(a<6)return "t-now";
  if(a<120)return "t-min";
  return "t-old";
}
function uptime(created){        // session age from tmux created_unix (#16)
  if(!created)return "";
  const s=Math.max(0,Math.floor(Date.now()/1000)-created);
  if(s<3600)return "up "+Math.round(s/60)+"m";
  if(s<86400)return "up "+Math.round(s/3600)+"h";
  return "up "+Math.round(s/86400)+"d";
}
function hot(s){return s.last_change_age!==null&&s.last_change_age!==undefined&&s.last_change_age<6;}
function shown(){return grid.filter(s=>
  (!filterStr||s.name.toLowerCase().includes(filterStr))
  &&(!activeTag||(s.tags||[]).includes(activeTag))
  &&(!activeCompany||(s.company||{}).company===activeCompany));}

function applyFilters(){renderTagbar();renderSidebar();if(mode!=="chat")render();}

function renderTagbar(){
  const box=$("#tagbar");if(!box)return;
  // companies (colored, from cwd) then tags — both filter the whole view
  const comp=new Map();   // key -> {label,color,n}
  grid.forEach(s=>{const c=s.company||{};if(c.company){
    const e=comp.get(c.company)||{label:c.label,color:c.color,n:0};e.n++;comp.set(c.company,e);}});
  const counts=new Map();
  grid.forEach(s=>(s.tags||[]).forEach(t=>counts.set(t,(counts.get(t)||0)+1)));
  if(!comp.size&&!counts.size&&!activeTag&&!activeCompany){box.innerHTML="";return;}
  let html="";
  if(activeTag||activeCompany)html+='<span class="tagchip clr" data-clear="1">✕ all</span>';
  [...comp.keys()].sort().forEach(k=>{const e=comp.get(k);
    const on=k===activeCompany;
    html+='<span class="cochip'+(on?" on":"")+'" data-co="'+k+'" '
      +'style="'+(on?'background:'+e.color+';border-color:'+e.color:'color:'+e.color+';border-color:'+e.color)+'">'
      +e.label+'<span class="cnt">'+e.n+'</span></span>';
  });
  [...counts.keys()].sort().forEach(t=>{
    html+='<span class="tagchip'+(t===activeTag?" on":"")+'" data-tag="'+t+'">#'+t
      +'<span class="cnt">'+counts.get(t)+'</span></span>';
  });
  box.innerHTML=html;
  box.querySelectorAll("[data-clear]").forEach(el=>el.onclick=()=>{activeTag="";activeCompany="";applyFilters();});
  box.querySelectorAll(".cochip").forEach(el=>el.onclick=()=>{
    activeCompany=el.dataset.co===activeCompany?"":el.dataset.co;applyFilters();});
  box.querySelectorAll(".tagchip[data-tag]").forEach(el=>el.onclick=()=>{
    activeTag=el.dataset.tag===activeTag?"":el.dataset.tag;applyFilters();});
}

function setMode(m){
  mode=m;current=(m==="chat")?current:null;
  if(m!=="chat")localStorage.setItem("emux_view",m);   // remember last view (#6)
  $("#chat").style.display=(m==="chat")?"flex":"none";
  $("#views").style.display=(m==="chat")?"none":"";
  $("#composer").style.display=(m==="chat")?"":"none";
  $("#attachbtn").style.display=(m==="chat")?"":"none";
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.mode===m));
  document.querySelectorAll(".card").forEach(el=>el.classList.toggle("active",!!(current&&el.dataset.name===current.name)));
  clearInterval(chatTimer);chatTimer=null;
  if(m!=="chat"){$("#title").textContent=m;$("#views").innerHTML="";flowSig=null;render();}
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setMode(t.dataset.mode));

function updateChrome(){         // title, footer, tab counts (#1 #3 #4)
  const liveN=grid.filter(s=>s.live).length;
  const actN=grid.filter(s=>s.live&&hot(s)).length;
  if(!flashOn)document.title="emux · "+liveN+" live";
  $("#footer").textContent="daemon · v__VERSION__ · "+grid.length+" sessions";
  document.querySelectorAll(".tab").forEach(t=>{
    const m=t.dataset.mode;
    t.textContent=(m==="activity"&&actN)?BASE_TAB[m]+" · "+actN:BASE_TAB[m];
  });
}

async function poll(){
  if(document.hidden&&grid.length)return;  // pause steady-state polling on a backgrounded tab, but still do the first load (#13)
  try{
    const r=await api("/api/grid?lines=14");
    if(!r.ok){$("#status").textContent=r.error||"error";$("#status").className="err";return;}
    grid=r.sessions;
    $("#status").textContent=grid.filter(s=>s.live).length+" live · polling";$("#status").className="";
    updateChrome();renderTagbar();renderSidebar();
    if(mode!=="chat")render();
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function renderSidebar(){
  const box=$("#sessions");box.innerHTML="";
  shown().forEach(s=>{
    const d=document.createElement("div");
    d.className="card"+(current&&current.name===s.name?" active":"")+(s.live?"":" gone");
    d.dataset.name=s.name;
    const att=s.attached?'<span class="att">●attached</span>':"";
    const up=s.live?uptime(s.created_unix):"";
    const ag=s.agent||{glyph:"",label:""};
    const agspan=(s.live&&ag.label&&ag.label!=="—")?'<span>'+agentHTML(s)+'</span>':"";
    const tagspans=(s.tags||[]).map(t=>'<span class="tagjump" data-tag="'+t+'">#'+t+'</span>').join("");
    const cobadge=companyHTML(s);
    const badges=(s.registered?"<span>registered</span>":"<span>unregistered</span>")
      +cobadge+agspan+(s.attached?"<span>attached</span>":"")+tagspans;
    d.innerHTML='<div class="nm"><span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name+att+'</div>'
      +'<div class="sub">→ '+s.session+(up?" · "+up:"")+(s.description?" — "+s.description:"")+'</div>'
      +'<div class="badges">'+badges+'</div>';
    d.onclick=()=>openModal(s);
    box.appendChild(d);
  });
  document.querySelectorAll(".tagjump").forEach(el=>el.onclick=ev=>{   // click a card's tag → filter to it
    ev.stopPropagation();const tag=el.dataset.tag;
    activeTag=tag===activeTag?"":tag;
    renderTagbar();renderSidebar();if(mode!=="chat")render();
  });
}

function makeTile(s){
  const t=document.createElement("div");
  t.className="tile"+(hot(s)?" hot":"")+(s.live?"":" dead");
  const h=document.createElement("header");
  const att=s.attached?'<span class="att">●</span>':"";
  const ag=s.agent||{glyph:"",label:""};
  const agbadge=(s.live&&ag.label&&ag.label!=="—")?'<span class="agentbadge">'+agentHTML(s)+'</span>':"";
  h.innerHTML='<span class="dot '+(s.live?"live":"stale")+'"></span><span class="nm">'+s.name+att+'</span>'
    +companyHTML(s)+agbadge
    +'<span class="age '+(s.live?ageClass(s.last_change_age):"t-old")+'">'+(s.live?ageLabel(s.last_change_age):"gone")+'</span>';
  const p=document.createElement("pre");
  if(s.live&&s.content.trim()){
    p.textContent=s.content.replace(/\s+$/,"").split("\n").slice(-14).join("\n");
  }else{
    p.className="empty";p.textContent=s.live?"(blank pane)":"tmux session gone";
  }
  t.appendChild(h);t.appendChild(p);
  t.onclick=()=>openModal(s);
  return t;
}

function renderGrid(){
  const v=$("#views");v.innerHTML="";
  const list=shown();
  if(!list.length){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const g=document.createElement("div");g.className="tilegrid";
  list.forEach(s=>g.appendChild(makeTile(s)));
  v.appendChild(g);
}

function renderGroups(){
  const v=$("#views");v.innerHTML="";
  const groups=new Map();
  const put=(k,s)=>{if(!groups.has(k))groups.set(k,[]);groups.get(k).push(s);};
  shown().forEach(s=>{
    if(!s.registered)put("unregistered",s);
    else if(!(s.tags||[]).length)put("untagged",s);
    else s.tags.forEach(t=>put("#"+t,s));
  });
  if(!groups.size){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const order=[...groups.keys()].sort((a,b)=>{
    const w=k=>k==="unregistered"?2:(k==="untagged"?1:0);
    return w(a)-w(b)||a.localeCompare(b);
  });
  order.forEach(k=>{
    const sec=document.createElement("div");sec.className="group";
    const h=document.createElement("h2");
    if(k[0]==="#")h.id="grp-"+k.slice(1);   // anchor for clickable-tag jump (#8)
    h.innerHTML=k+' <span class="cnt">· '+groups.get(k).length+'</span>';
    const g=document.createElement("div");g.className="tilegrid";
    groups.get(k).forEach(s=>g.appendChild(makeTile(s)));
    sec.appendChild(h);sec.appendChild(g);v.appendChild(sec);
  });
}

function renderActivity(){
  const v=$("#views");v.innerHTML="";
  const list=shown();
  if(!list.length){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const wrap=document.createElement("div");wrap.className="actrows";
  list.forEach(s=>{
    const row=document.createElement("div");row.className="actrow";
    const nm=document.createElement("div");nm.className="nm";
    nm.innerHTML='<span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name;
    const cells=document.createElement("div");cells.className="cells";
    const samples=s.activity||[];
    for(let i=0;i<Math.max(0,60-samples.length);i++){const c=document.createElement("div");c.className="cell";cells.appendChild(c);}
    samples.forEach((on,i)=>{
      const c=document.createElement("div");
      c.className="cell"+(on?(i>=samples.length-5?" recent":" on"):"");
      cells.appendChild(c);
    });
    const age=document.createElement("div");age.className="age "+(s.live?ageClass(s.last_change_age):"t-old");
    age.textContent=s.live?("active "+ageLabel(s.last_change_age)):"gone";
    row.appendChild(nm);row.appendChild(cells);row.appendChild(age);
    row.onclick=()=>openModal(s);
    wrap.appendChild(row);
  });
  v.appendChild(wrap);
}

function el(tag,attrs){const e=document.createElementNS(SVGNS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}

function paneText(s){return (s.live&&s.content.trim())?s.content.replace(/\s+$/,"").split("\n").slice(-9).join("\n"):"";}
function agentHTML(s){const ag=s.agent||{};if(!s.live)return "gone";
  const g=ag.glyph?'<span class="agi ag-'+(ag.agent||"x")+'">'+ag.glyph+'</span> ':'';
  return g+(ag.label||"—");}
function companyHTML(s){const c=s.company||{};if(!c.company)return "";
  return '<span class="cco" style="background:'+c.color+'">'+c.label+'</span>';}

// Update flow boxes in place (no DOM teardown) — keeps the panes LIVE and smooth.
function updateFlowPanes(){
  grid.forEach(s=>{
    const d=flowBox[s.name], pre=flowPre[s.name];
    if(!d||!pre)return;
    d.className="fbox"+(hot(s)?" hot":"")+(s.live?"":" dead");
    const ag=d.querySelector(".ag");if(ag)ag.innerHTML=agentHTML(s);
    const txt=paneText(s);
    if(txt){pre.className="";if(pre.textContent!==txt)pre.textContent=txt;}
    else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
  });
}

function renderFlow(){
  const v=$("#views");
  if(!grid.length){flowSig=null;v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';return;}
  // resolve manage targets by registry name OR underlying tmux session name
  const byKey={};grid.forEach(s=>{byKey[s.name]=s;if(!(s.session in byKey))byKey[s.session]=s;});
  const children=new Map(),indeg=new Map();
  grid.forEach(s=>indeg.set(s.name,0));
  const edges=[];
  grid.forEach(s=>(s.manages||[]).forEach(t=>{
    const tg=byKey[t];if(!tg||tg.name===s.name)return;
    edges.push([s.name,tg.name]);
    if(!children.has(s.name))children.set(s.name,[]);
    children.get(s.name).push(tg.name);
    indeg.set(tg.name,(indeg.get(tg.name)||0)+1);
  }));
  const connected=new Set();edges.forEach(([a,b])=>{connected.add(a);connected.add(b);});
  // level assignment via BFS from roots; guard against cycles
  const level=new Map();
  [...connected].filter(n=>(indeg.get(n)||0)===0).forEach(r=>level.set(r,0));
  let q=[...level.keys()],guard=0,MAX=grid.length*grid.length+20;
  while(q.length&&guard++<MAX){
    const n=q.shift(),l=level.get(n);
    (children.get(n)||[]).forEach(c=>{
      if(l+1<grid.length&&(!level.has(c)||level.get(c)<l+1)){level.set(c,l+1);q.push(c);}
    });
  }
  [...connected].forEach(n=>{if(!level.has(n))level.set(n,0);});
  const maxLvl=Math.max(0,...[...level.values()]);
  const unconnected=grid.filter(s=>!connected.has(s.name));

  // Topology signature: rebuild the DOM only when the structure changes;
  // otherwise just stream new pane content into the existing boxes.
  const sig=JSON.stringify([
    [...connected].sort(), unconnected.map(s=>s.name).sort(),
    edges.map(e=>e.join(">")).sort(), [...level.entries()].sort(),
  ]);
  if(sig===flowSig && document.getElementById("flowwrap")){updateFlowPanes();return;}
  flowSig=sig;flowPre={};flowBox={};
  v.innerHTML="";

  // Each node is a live mini-pane box (title + tiny tmux preview), laid out by
  // level. Boxes are HTML positioned over an SVG edge layer.
  const BW=236,BH=150,COLGAP=40,ROWGAP=64,PAD=30;
  const W=1200;
  // Unconnected boxes wrap into a left-aligned GRID that never overlaps — the old
  // layout crammed every unconnected box onto a single fixed-width row (x =
  // W*(i+1)/(n+1)), so past ~4 boxes they printed on top of each other.
  const UNCW=BW+COLGAP;
  const uncCols=Math.max(1,Math.floor((W-2*PAD+COLGAP)/UNCW));
  const uncRows=unconnected.length?Math.ceil(unconnected.length/uncCols):0;
  const H=PAD*2+(maxLvl+1)*(BH+ROWGAP)+(uncRows?ROWGAP+uncRows*(BH+ROWGAP):0);
  const pos={};
  const rowY=lv=>PAD+BH/2+lv*(BH+ROWGAP);
  for(let lv=0;lv<=maxLvl;lv++){
    const row=[...connected].filter(n=>level.get(n)===lv);
    row.forEach((n,i)=>{pos[n]={x:W*(i+1)/(row.length+1),y:rowY(lv)};});
  }
  const uncY=rowY(maxLvl+1);
  unconnected.forEach((s,i)=>{
    const c=i%uncCols, r=Math.floor(i/uncCols);
    pos[s.name]={x:PAD+BW/2+c*UNCW, y:uncY+r*(BH+ROWGAP)};
  });

  // edge layer (SVG, behind the boxes)
  const svg=el("svg",{id:"flowsvg",viewBox:"0 0 "+W+" "+H});
  const defs=el("defs",{});
  const marker=el("marker",{id:"arrow",viewBox:"0 0 10 10",refX:"9",refY:"5",markerWidth:"7",markerHeight:"7",orient:"auto-start-reverse"});
  marker.appendChild(el("path",{d:"M0,0 L10,5 L0,10 z",fill:"#ffb000"}));
  defs.appendChild(marker);svg.appendChild(defs);
  edges.forEach(([a,b])=>{
    const pa=pos[a],pb=pos[b];if(!pa||!pb)return;
    const y1=pa.y+BH/2,y2=pb.y-BH/2,my=(y1+y2)/2;
    svg.appendChild(el("path",{d:"M"+pa.x+","+y1+" C"+pa.x+","+my+" "+pb.x+","+my+" "+pb.x+","+y2,
      class:"edge","marker-end":"url(#arrow)"}));
  });
  if(unconnected.length){
    const sy=uncY-BH/2-ROWGAP/2;
    svg.appendChild(el("line",{x1:PAD,y1:sy,x2:W-PAD,y2:sy,class:"sep"}));
    const lab=el("text",{x:PAD+6,y:sy-7,class:"rowlabel"});lab.textContent="unconnected · not in any manages relationship";
    svg.appendChild(lab);
  }

  const wrap=document.createElement("div");wrap.id="flowwrap";
  wrap.style.width=W+"px";wrap.style.height=H+"px";
  wrap.appendChild(svg);

  // node boxes: title (name + agent icon) + tiny live pane
  function box(s){
    const p=pos[s.name];
    const d=document.createElement("div");
    d.className="fbox"+(hot(s)?" hot":"")+(s.live?"":" dead");
    d.style.left=p.x+"px";d.style.top=p.y+"px";d.style.width=BW+"px";
    const title='<div class="ftitle"><span class="dot '+(s.live?"live":"stale")+'"></span>'
      +'<span class="nm">'+s.name+'</span><span class="ag">'+agentHTML(s)+'</span></div>';
    const pre=document.createElement("pre");
    const txt=paneText(s);
    if(txt)pre.textContent=txt;else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
    d.innerHTML=title;d.appendChild(pre);
    d.onclick=()=>openModal(s);                 // click a box → zoom-in modal to steer it
    flowBox[s.name]=d;flowPre[s.name]=pre;
    wrap.appendChild(d);
  }
  [...connected].forEach(n=>box(byKey[n]));
  unconnected.forEach(box);

  v.appendChild(wrap);
  const hint=document.createElement("div");hint.id="flowhint";
  hint.innerHTML="each box is a live tmux pane (click to zoom in & steer); title shows the session + detected AI. arrows = agent manages agent (<code>emux register &lt;name&gt; &lt;session&gt; --manages &lt;other&gt;</code>) — orchestrators on top, the agents they drive below.";
  v.appendChild(hint);
}

function render(){
  if(mode==="grid")renderGrid();
  else if(mode==="groups")renderGroups();
  else if(mode==="activity")renderActivity();
  else if(mode==="flow")renderFlow();
}

function pinned(){const c=$("#chat");return c.scrollHeight-c.scrollTop-c.clientHeight<60;}
function scrollBottom(){const c=$("#chat");c.scrollTop=c.scrollHeight;$("#jump").style.display="none";}
function clockNow(){const d=new Date();return d.toTimeString().slice(0,5);}  // HH:MM (#10)

function addBubble(cls,who,text){
  const b=document.createElement("div");b.className="bubble "+cls;
  if(who){
    const w=document.createElement("div");w.className="who";
    w.textContent=(cls==="user")?who+" · "+clockNow():who;   // timestamp user bubbles (#10)
    b.appendChild(w);
  }
  const t=document.createElement("div");t.textContent=text;b.appendChild(t);
  const c=$("#chat");
  if(screenEl&&screenEl.parentElement===c){c.insertBefore(b,screenEl);}else{c.appendChild(b);}
  scrollBottom();
}

async function refreshScreen(){
  if(!current)return;
  const wasPinned=pinned();
  try{
    const r=await api("/api/capture?session="+encodeURIComponent(current.session)+"&lines=400");
    const s=$("#screen");if(!s)return;
    if(r.ok){
      $("#status").textContent="live · polling";$("#status").className="";
      s.classList.remove("dimmed");
      if(s.dataset.last!==r.content){
        s.dataset.last=r.content;
        s.textContent=r.content.replace(/\s+$/,"")+"\n";
        const cur=document.createElement("span");cur.className="cursorblock";s.appendChild(cur);
        if(document.hidden){flashOn=true;document.title="● emux — "+current.name;}  // title flash (#20)
        if(wasPinned)scrollBottom();else $("#jump").style.display="block";          // jump pill (#11)
      }
    }else{
      $("#status").textContent=r.error||"capture failed";$("#status").className="err";
      s.classList.add("dimmed");
    }
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function openChat(sess){
  current=sess;
  setMode("chat");
  $("#title").textContent=sess.name;
  $("#status").textContent="connecting…";$("#status").className="";
  const c=$("#chat");c.innerHTML="";screenEl=null;
  screenEl=document.createElement("div");screenEl.id="screen-bubble";screenEl.className="bubble";
  screenEl.innerHTML='<div class="who"></div><div id="screen"></div>';
  screenEl.querySelector(".who").textContent=sess.name+" · live screen (updates in place)";
  c.appendChild(screenEl);
  applyWrap();
  addBubble("sys",null,"monitoring tmux session “"+sess.session+"”"+(sess.description?" — "+sess.description:""));
  document.querySelectorAll(".card").forEach(el2=>el2.classList.toggle("active",el2.dataset.name===sess.name));
  refreshScreen();chatTimer=setInterval(refreshScreen,1500);
  $("#input").focus();
}

async function sendText(){
  const inp=$("#input");const text=inp.value;
  if(!current||!text)return;
  inp.value="";addBubble("user","you",text);
  let ok=false;
  try{const r=await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:current.session,keys:text,literal:true,enter:true})});ok=!!(r&&r.ok);}catch(e){}
  if(!ok){addBubble("sys",null,"send failed — draft restored");if(!inp.value)inp.value=text;}
  setTimeout(refreshScreen,300);
}

// wrap toggle (#9)
function applyWrap(){
  const s=$("#screen");if(!s)return;
  const off=localStorage.getItem("emux_wrap")==="off";
  s.classList.toggle("nowrap",off);
  $("#wrapchip").textContent="WRAP: "+(off?"OFF":"ON");
}
$("#wrapchip").onclick=()=>{
  localStorage.setItem("emux_wrap",localStorage.getItem("emux_wrap")==="off"?"on":"off");applyWrap();
};

$("#send").onclick=sendText;
$("#input").addEventListener("keydown",e=>{if(e.key==="Enter")sendText();});
document.querySelectorAll("#chips .chip").forEach(ch=>{
  if(ch.id==="wrapchip")return;
  ch.onclick=async()=>{
    if(!current)return;
    addBubble("user","key",ch.textContent);
    await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:current.session,keys:ch.dataset.keys,literal:false,enter:false})});
    setTimeout(refreshScreen,300);
  };
});

// copy attach command (#12)
$("#attachbtn").onclick=()=>{
  if(!current)return;
  navigator.clipboard?.writeText("tmux attach -t "+current.session);
  const b=$("#attachbtn");const o=b.textContent;b.textContent="⧉ copied!";setTimeout(()=>b.textContent=o,1200);
};
// manual refresh (#15)
$("#refreshbtn").onclick=()=>{poll();if(current)refreshScreen();};
// jump to bottom pill (#11)
$("#jump").onclick=scrollBottom;
$("#chat").addEventListener("scroll",()=>{if(pinned())$("#jump").style.display="none";});
// sidebar filter (#7)
$("#filter").addEventListener("input",e=>{filterStr=e.target.value.toLowerCase();renderSidebar();if(mode!=="chat")render();});
// ---------- zoom-in steer modal ----------
let modalSession=null, modalTimer=null;
function openModal(s){
  modalSession=s;
  $("#modalname").textContent=s.name;
  $("#modalagent").innerHTML=agentHTML(s);
  $("#modalstatus").textContent="connecting…";$("#modalstatus").style.color="";
  const sc=$("#modalscreen");sc.textContent="";sc.dataset.last="";
  $("#modal").classList.add("open");
  modalRefresh();clearInterval(modalTimer);modalTimer=setInterval(modalRefresh,1200);
  setTimeout(()=>$("#modalinput").focus(),40);
}
function closeModal(){
  $("#modal").classList.remove("open");
  clearInterval(modalTimer);modalTimer=null;modalSession=null;
}
async function modalRefresh(){
  if(!modalSession)return;
  const sc=$("#modalscreen");const atBottom=sc.scrollHeight-sc.scrollTop-sc.clientHeight<60;
  try{
    const r=await api("/api/capture?session="+encodeURIComponent(modalSession.session)+"&lines=400");
    if(r.ok){
      $("#modalstatus").textContent="live";$("#modalstatus").style.color="";
      if(sc.dataset.last!==r.content){
        sc.dataset.last=r.content;sc.textContent=r.content.replace(/\s+$/,"")+"\n";
        const cur=document.createElement("span");cur.className="cursorblock";sc.appendChild(cur);
        if(atBottom)sc.scrollTop=sc.scrollHeight;
      }
    }else{$("#modalstatus").textContent=r.error||"error";$("#modalstatus").style.color="var(--stale)";}
  }catch(e){$("#modalstatus").textContent="unreachable";$("#modalstatus").style.color="var(--stale)";}
  modalJudge();
}
async function modalJudge(){
  if(!modalSession)return;
  const el=$("#modaljudge");
  try{
    const r=await api("/api/classify?name="+encodeURIComponent(modalSession.name));
    if(r&&r.ok&&r.state){
      const flags=(r.flags||[]).map(f=>'<span class="jflag">'+f.replace(/_/g," ")+'</span>').join("");
      el.innerHTML='<span class="jstate s-'+r.state+'">'+r.state.replace(/_/g,"·")+'</span>'
        +'<span class="jconf">'+Math.round((r.confidence||0)*100)+'% · '+(r.recommended_action||"")+'</span>'
        +'<span class="jsum">'+(r.summary||"")+'</span>'+flags;
      el.className="on";
    }else{el.className="";el.innerHTML="";}
  }catch(e){el.className="";}
}
async function modalKeys(keys,literal,enter){
  if(!modalSession)return false;
  try{
    const r=await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:modalSession.session,keys,literal,enter})});
    setTimeout(modalRefresh,250);
    return !!(r&&r.ok);
  }catch(e){return false;}   // daemon down / network error
}
function modalSubmit(){
  const i=$("#modalinput");const text=i.value;if(!text)return;
  i.value="";                                   // optimistic clear for snappy UX…
  modalKeys(text,true,true).then(ok=>{
    if(!ok){                                    // …but if it didn't land, give the draft back
      if(!i.value)i.value=text;
      const st=$("#modalstatus");st.textContent="send failed — draft kept";st.style.color="var(--stale)";
    }
  });
}
$("#modalsend").onclick=modalSubmit;
$("#modalinput").addEventListener("keydown",e=>{if(e.key==="Enter")modalSubmit();});
$("#modaliterm").onclick=async()=>{
  if(!modalSession)return;
  const b=$("#modaliterm");const was=b.textContent;b.disabled=true;b.textContent="opening…";
  const r=await api("/api/head",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:modalSession.session})});
  b.textContent=r.ok?"⧉ opened":("✕ "+(r.error||"failed"));
  setTimeout(()=>{b.textContent=was;b.disabled=false;},r.ok?1400:3000);
};
// ---- new session (a CASCADE: machine → its real dirs → command) ----
let hostsLoaded=false;
function setDirs(dirs){
  $("#dirlist").innerHTML=(dirs||[]).map(d=>'<option value="'+d+'">').join("");
  $("#dircount").textContent=(dirs&&dirs.length)?(dirs.length+" dirs on this machine"):"no dirs found";
}
async function loadHosts(){
  if(hostsLoaded)return;
  const r=await api("/api/hosts");
  if(!r.ok)return;
  $("#newhost").innerHTML=(r.hosts||[]).map(h=>'<option value="'+h+'">'+h+'</option>').join("");
  hostsLoaded=true;
}
// the machine determines which directories exist — re-derive them on every change
async function loadDirsFor(host){
  $("#dircount").textContent="listing "+host+"…";
  setDirsBusy(true);
  const r=await api("/api/dirs?host="+encodeURIComponent(host));
  setDirsBusy(false);
  if(!r.ok){$("#dircount").textContent="could not list "+host;setDirs([]);return;}
  setDirs(r.dirs);
}
function setDirsBusy(b){$("#newcwd").disabled=b;}
async function onHostChange(){
  const h=$("#newhost").value;
  $("#newcwd").value="";          // a path from the old machine is meaningless here
  $("#newwhy").textContent="";
  await loadDirsFor(h);
}
function openNew(){
  $("#newmodal").classList.add("open");
  $("#newerr").textContent="";$("#newwhy").textContent="";$("#newstatus").textContent="";
  (async()=>{await loadHosts();await loadDirsFor($("#newhost").value||"local");})();
  setTimeout(()=>$("#newintent").focus(),40);
}
function closeNew(){$("#newmodal").classList.remove("open");}
async function doSuggest(){
  const intent=$("#newintent").value.trim();
  if(!intent){$("#newerr").textContent="describe what you want to do first";return;}
  const b=$("#newsuggest");b.disabled=true;b.textContent="thinking…";
  $("#newerr").textContent="";
  // if the user already picked a machine, that pins the cascade; else claude picks it
  const pinned=$("#newpin").checked?$("#newhost").value:"";
  $("#newstatus").textContent=pinned?("choosing a dir on "+pinned):"choosing machine → dir";
  const r=await api("/api/suggest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({intent,host:pinned})});
  b.disabled=false;b.textContent="✦ suggest";$("#newstatus").textContent="";
  if(!r.ok){$("#newerr").textContent=r.error||"suggest failed";return;}
  if(r.host){$("#newhost").value=r.host;}
  if(r.dirs)setDirs(r.dirs);             // the dirs it actually chose from
  if(r.name)$("#newname").value=r.name;
  if(r.cwd)$("#newcwd").value=r.cwd;
  if(r.command!==undefined)$("#newcmd").value=r.command||"";
  $("#newwhy").textContent=r.why?((r.verified?"✦ ":"⚠ unverified path — ")+r.why):"";
}
async function doCreate(){
  const name=$("#newname").value.trim();
  if(!name){$("#newerr").textContent="a session needs a name";return;}
  const b=$("#newcreate");b.disabled=true;b.textContent="CREATING…";$("#newerr").textContent="";
  const r=await api("/api/spawn",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name,host:$("#newhost").value,cwd:$("#newcwd").value.trim(),
      command:$("#newcmd").value.trim(),gui:$("#newgui").checked,
      description:$("#newintent").value.trim()||null})});
  b.disabled=false;b.textContent="CREATE SESSION";
  if(!r.ok){$("#newerr").textContent=r.error||"spawn failed";return;}
  closeNew();$("#newintent").value="";$("#newname").value="";$("#newcmd").value="";
  refresh();
}
$("#newhost").onchange=onHostChange;   // the cascade: machine → its real dirs
$("#newbtn").onclick=openNew;
$("#newclose").onclick=closeNew;
$("#newback").onclick=closeNew;
$("#newsuggest").onclick=doSuggest;
$("#newcreate").onclick=doCreate;
$("#newintent").addEventListener("keydown",e=>{if(e.key==="Enter")doSuggest();});

$("#modalclose").onclick=closeModal;
$("#modalback").onclick=closeModal;
document.querySelectorAll("#modalchips .chip").forEach(ch=>ch.onclick=()=>modalKeys(ch.dataset.keys,false,false));

// keyboard: Esc closes the modal first; otherwise 1-4 switch views
document.addEventListener("keydown",e=>{
  if($("#newmodal").classList.contains("open")){if(e.key==="Escape")closeNew();return;}
  if($("#modal").classList.contains("open")){if(e.key==="Escape")closeModal();return;}
  if(e.target.id==="filter"||e.target.id==="input"||e.target.id==="modalinput")return;
  if(e.target.closest&&e.target.closest("#newmodal"))return;
  const map={"1":"grid","2":"groups","3":"activity","4":"flow"};
  if(map[e.key])setMode(map[e.key]);
});
// resume + clear title flash when tab refocuses (#13 #20)
document.addEventListener("visibilitychange",()=>{
  if(!document.hidden){flashOn=false;poll();if(modalSession)modalRefresh();}
});

setMode(localStorage.getItem("emux_view")||"grid");   // restore last view (#6)
poll();gridTimer=setInterval(poll,2000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class EmuxWebHandler(BaseHTTPRequestHandler):
    server_version = f"emux/{__version__}"
    # The host the daemon was bound to, when non-localhost; lets a deliberate
    # --host 0.0.0.0 accept its own LAN address while still blocking foreign ones.
    extra_host: str | None = None

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_allowed(self) -> bool:
        """Defeat DNS-rebinding: only serve requests whose Host header is a
        loopback name (or the explicit bind host). A rebound attacker domain
        resolving to 127.0.0.1 carries its own name in Host and is rejected."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in _LOCALHOSTS or (self.extra_host is not None and host == self.extra_host)

    def _origin_allowed(self) -> bool:
        """Block cross-site writes: a POST carrying an Origin from any non-local
        site is a forged request from another tab. Same-origin and non-browser
        (no Origin) requests pass."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            host = urlparse(origin).hostname
        except ValueError:
            return False
        return host in _LOCALHOSTS or (self.extra_host is not None and host == self.extra_host)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            body = PAGE.replace("__VERSION__", __version__).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/healthz":
            # Unguarded on purpose: leaks nothing, lets launchd/monitoring probe liveness.
            live = sessions_payload()
            n = len([s for s in live.get("sessions", []) if s.get("live")]) if live.get("ok") else 0
            self._json({"ok": True, "version": __version__, "live_sessions": n})
            return
        if url.path.startswith("/api/"):
            if not self._host_allowed():
                self._json({"ok": False, "error": "forbidden_host"}, 403)
                return
        if url.path == "/api/sessions":
            self._json(sessions_payload())
            return
        if url.path == "/api/grid":
            q = parse_qs(url.query)
            try:
                lines = max(1, min(100, int((q.get("lines") or ["14"])[0])))
            except ValueError:
                lines = 14
            self._json(grid_payload(lines))
            return
        if url.path == "/api/capture":
            q = parse_qs(url.query)
            session = (q.get("session") or [""])[0]
            if not session:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            try:
                lines = max(1, min(5000, int((q.get("lines") or ["300"])[0])))
            except ValueError:
                lines = 300
            self._json(capture_payload(session, lines))
            return
        if url.path == "/api/classify":
            q = parse_qs(url.query)
            name = (q.get("name") or [""])[0]
            if not name:
                self._json({"ok": False, "error": "missing_name"}, 400)
                return
            from . import judge
            try:
                self._json({"ok": True, **judge.classify_session(name)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return
        if url.path == "/api/hosts":
            self._json({"ok": True, "hosts": _known_hosts()})
            return
        if url.path == "/api/dirs":
            # directories that exist ON the chosen machine — the cascade's 2nd level
            h = (parse_qs(url.query).get("host") or ["local"])[0]
            dirs = _candidate_dirs(None if h in ("", "local") else h)
            self._json({"ok": True, "host": h, "dirs": dirs})
            return
        self._json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path not in ("/api/send", "/api/head", "/api/spawn", "/api/suggest"):
            self._json({"ok": False, "error": "not_found"}, 404)
            return
        if not self._host_allowed():
            self._json({"ok": False, "error": "forbidden_host"}, 403)
            return
        if not self._origin_allowed():
            self._json({"ok": False, "error": "forbidden_origin"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad_json"}, 400)
            return
        if url.path == "/api/suggest":
            intent = (data.get("intent") or "").strip()
            if not intent:
                self._json({"ok": False, "error": "missing_intent"}, 400)
                return
            # a machine the user already picked pins the cascade; else the model picks it
            pinned = (data.get("host") or "").strip()
            r = _suggest_session(intent, host=pinned or None)
            if "_error" in r:
                self._json({"ok": False, "error": r["_error"]})
            else:
                self._json({"ok": True, **r})
            return
        if url.path == "/api/spawn":
            self._json(_spawn_session(data))
            return
        session = data.get("session")
        if not isinstance(session, str) or not session:
            self._json({"ok": False, "error": "missing_session"}, 400)
            return
        if url.path == "/api/head":
            ok, err = _iterm_attach(session)
            self._json({"ok": ok, "error": err} if not ok else {"ok": True, "session": session})
            return
        keys = data.get("keys")
        if not isinstance(keys, str):
            self._json({"ok": False, "error": "missing_keys"}, 400)
            return
        self._json(send_payload(
            session,
            keys,
            literal=bool(data.get("literal", True)),
            enter=bool(data.get("enter", True)),
        ))

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default; the poll traffic would otherwise flood the terminal.
        pass


def launchd_plist(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    """A ready-to-install launchd plist that keeps `emux web` running and
    restarts it on crash / login. Print with `emux web --print-launchd`."""
    emux = sys.argv[0]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.eidos.emux-web</string>
  <key>ProgramArguments</key>
  <array>
    <string>{emux}</string>
    <string>web</string>
    <string>--host</string><string>{host}</string>
    <string>--port</string><string>{port}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/emux-web.log</string>
  <key>StandardErrorPath</key><string>/tmp/emux-web.err.log</string>
</dict>
</plist>
"""


def run_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
    """Start the emux web daemon. Blocks until Ctrl-C."""
    if _server._resolve_tmux() is None:
        print("emux web: tmux not found on PATH — the UI will load but show nothing.", file=sys.stderr)
    EmuxWebHandler.extra_host = host if host not in _LOCALHOSTS else None
    try:
        server = ThreadingHTTPServer((host, port), EmuxWebHandler)
    except OSError as e:
        if "address already in use" in str(e).lower():
            print(f"emux web: port {port} is already in use — try `emux web --port {port + 1}`.", file=sys.stderr)
            return 2
        raise

    # Single background capture loop feeds the cache that every browser tab
    # reads from. One sweep per tick regardless of how many tabs are open.
    stop = threading.Event()

    def poll_loop() -> None:
        while not stop.is_set():
            try:
                poll_once(14)
            except Exception:  # noqa: BLE001 — a transient tmux error must not kill the loop
                pass
            stop.wait(_POLL_INTERVAL)

    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    url = f"http://{host}:{port}"
    print(f"emux web daemon → {url}  (Ctrl-C to stop)")
    if host not in _LOCALHOSTS:
        print("  WARNING: bound beyond localhost. The API blocks foreign Host/Origin", file=sys.stderr)
        print("  requests, but there is still NO authentication — anyone who can reach", file=sys.stderr)
        print("  this port and forge a matching Host header can type into your sessions.", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nemux web: stopped.")
    finally:
        stop.set()
        server.server_close()
    return 0
