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

import hashlib
import json
import os
import re
import shlex
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
# The Gist, cached server-side by content hash so it's not recomputed while the
# pane is unchanged; warmed proactively the moment a session stops (see
# _capture_and_observe) so its result is ready before you open it.
_GIST_CACHE: dict[str, dict[str, Any]] = {}   # session -> {hash, digest, suggestions, ts}
_GIST_INFLIGHT: set[str] = set()              # sessions being warmed right now (dedupe)
_SETTLED_STATES = frozenset({"idle", "asking", "waiting_human", "error"})
# how long a settled session must sit STILL (no pane change) before we treat it as
# "clearly stopped" and warm its gist — long enough to skip the brief running↔idle
# flicker between an agent's turns. Override with EMUX_GIST_PAUSE.
_GIST_PAUSE_SECS = float(os.environ.get("EMUX_GIST_PAUSE", "10"))


def _gist_hash(pane: str) -> str:
    """Cache key = a hash of the exact pane slice the gist model reads. When the
    pane changes, the hash changes, so the cache self-busts."""
    return hashlib.sha1(pane[-3500:].encode("utf-8", "ignore")).hexdigest()[:16]


def _should_warm_gist(state: str, age: float | None, norm: str | None,
                      prev_warm_norm: str | None, inflight: bool) -> bool:
    """Warm the gist iff the session is settled, has sat STILL for a real pause
    (age ≥ threshold — skips the brief between-turns flicker), its content is new
    since the last warm (norm changed), and no warm is already running."""
    return (state in _SETTLED_STATES and age is not None and age >= _GIST_PAUSE_SECS
            and norm is not None and norm != prev_warm_norm and not inflight)


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
    # a registered session may live on another machine — probe each distinct
    # remote host once so it shows LIVE, not "gone".
    remote_live: dict[str, set[str]] = {}
    for entry in registry.values():
        h = entry.get("host")
        if h and h not in remote_live:
            remote_live[h] = _remote_live_names(h)
    sessions = []
    for name, entry in sorted(registry.items()):
        target = entry.get("session")
        host = entry.get("host")
        if host:
            is_live = target in remote_live.get(host, set())
        else:
            is_live = target in live_by_name
        cwd = live_by_name.get(target, {}).get("cwd") or entry.get("cwd")
        sessions.append({
            "name": name,
            "session": target,
            "host": host,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "manages": entry.get("manages") or [],
            "registered": True,
            "live": is_live,
            "attached": live_by_name.get(target, {}).get("attached", False),
            "created_unix": live_by_name.get(target, {}).get("created_unix"),
            "cwd": cwd,
            # explicit override (remote worker / manager) wins over cwd-derivation
            "company": _company_by_key(entry.get("company")) or _detect_company(cwd),
            "_co_explicit": bool(entry.get("company")),
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
    # A manager belongs to the company of what it SUPERVISES, not where its
    # process runs. So if it manages workers that agree on one company, adopt it
    # — overriding a cwd-derived guess, but never an explicit override.
    by_name = {s["name"]: s for s in sessions}
    for s in sessions:
        if s.get("_co_explicit") or not s.get("manages"):
            continue
        managed_cos = {}
        for m in s["manages"]:
            c = (by_name.get(m) or {}).get("company") or {}
            if c.get("company"):
                managed_cos[c["company"]] = c
        if len(managed_cos) == 1:
            s["company"] = next(iter(managed_cos.values()))
    for s in sessions:
        s.pop("_co_explicit", None)
    return {"ok": True, "sessions": sessions}


# ---------------------------------------------------------------------------
# capture + change detection
# ---------------------------------------------------------------------------

_RLIVE_CACHE: dict[str, tuple[float, set[str]]] = {}
_RLIVE_TTL = 5.0


def _remote_live_names(host: str) -> set[str]:
    """tmux session names live on a REMOTE host (cached briefly). This is what
    lets the control room show a rentamac worker as LIVE instead of 'gone' — the
    daemon only sees local tmux, so a remote session needs an ssh probe."""
    hit = _RLIVE_CACHE.get(host)
    if hit and (time.time() - hit[0]) < _RLIVE_TTL:
        return hit[1]
    names: set[str] = set()
    try:
        code, out, _ = _server._run_tmux(["ls", "-F", "#{session_name}"],
                                         host=host, timeout=15)
        if code == 0:
            names = {ln.strip() for ln in out.splitlines() if ln.strip()}
    except Exception:  # noqa: BLE001
        names = set()
    _RLIVE_CACHE[host] = (time.time(), names)
    return names


def _session_host(session: str) -> str | None:
    """The registered host for a session id (None = local). Lets host-unaware
    callers (the modal's capture/send/classify by session id) reach remote."""
    for e in _server._load_registry().values():
        if e.get("session") == session:
            return e.get("host")
    return None


def capture_payload(session: str, lines: int = 300,
                    host: str | None = None) -> dict[str, Any]:
    """Capture the active pane of `session` (raw tmux session name), local or —
    when `host` is set — over ssh. Always live; the chat/modal want fresh, deep
    scrollback for one session, which is cheap."""
    if host is None and _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    code, out, err = _server._run_tmux(
        ["capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        host=host, timeout=20)
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed", "stderr": err}
    return {"ok": True, "session": session, "host": host, "content": out,
            "options": _parse_options(out),      # clickable menu bubbles, if a menu is up
            "thinking": _thinking(out)}          # is it generating, and for how long


_THINK_TIME_RE = re.compile(r"(\d+m\s?\d+s|\d+m|\d+s)")


def _thinking(content: str) -> dict[str, Any]:
    """Is the agent GENERATING right now, and for how long? 'esc to interrupt' is
    the universal 'I'm working' tell (Claude & Codex), and its line carries the
    live elapsed timer ('Working (12s …)', 'Herding… (2m 47s …)')."""
    for ln in content.splitlines():
        if "esc to interrupt" in ln.lower():
            m = _THINK_TIME_RE.search(ln)
            return {"active": True, "for": m.group(1).replace(" ", "") if m else ""}
    return {"active": False, "for": ""}


# (What each agent looks like in a pane now lives in emux/adapters.py — that is
# part of the agent's contract, not a private table here.)

# Which company/context a session belongs to, from its cwd.
# (key, label, color, roots, keywords). `roots` are the repos-<x>/ trees and are
# authoritative; `keywords` catch a company's repos that live in the generic
# ~/repos/ tree (e.g. repos/greenmark-claude-toolkit). Roots win over keywords.
_COMPANY_TABLE = [
    ("eidos", "Eidos", "#7dd3fc", ("repos-eidos-agi", "repos-eidos-capital"), ("eidos",)),
    ("greenmark", "Greenmark Waste", "#7bd88f", ("repos-greenmark",), ("greenmark",)),
    # Reeves — Daniel's PERSONAL ecosystem (reeves-wealth/lens/cockpit/operator/…).
    # Lives under repos-personal/, so it must be listed BEFORE `personal` (and `aic`,
    # for repos-aic/reeves-view) so the "reeves" match wins over the generic root.
    ("reeves", "Reeves", "#8ea0ff", ("reeves",), ("reeves",)),
    ("aic", "AIC", "#c4a3ff", ("repos-aic", "repos-aic-holdings"), ("aic-",)),
    ("jetta", "Jetta", "#ffb27d", ("repos-jetta",), ("jetta",)),
    ("momentito", "Momentito", "#ff9ecf", ("repos-momentito",), ("momentito",)),
    ("rhea", "Rhea Impact", "#9ae6e6", ("repos-rheaimpact",), ("rheaimpact", "rhea-impact")),
    ("asmp", "ASMP", "#d0c0a0", ("repos-asmp",), ("asmp",)),
    ("boone", "Boone Voyage", "#4db6c9", ("repos-bv",), ("boonevoyage", "boone-voyage")),
    ("personal", "Personal", "#f0d060", ("repos-personal", "repos-local"), ()),
]


# Standing routing preferences — state a rule ONCE, it sticks. You should never
# have to re-explain "Eidos runs on the mac-mini" per session. Defaults live here;
# override/extend at ~/.config/emux/routing.json:
#   {"company_host": {"eidos": "daniels-mac-mini", "greenmark": "some-host"}}
_COMPANY_HOST_DEFAULT = {"eidos": "daniels-mac-mini", "reeves": "daniels-mac-mini"}


def _routing_prefs() -> dict[str, Any]:
    prefs: dict[str, Any] = {"company_host": dict(_COMPANY_HOST_DEFAULT)}
    p = Path(os.environ.get("EMUX_ROUTING")
             or Path.home() / ".config" / "emux" / "routing.json")
    if p.is_file():
        try:
            user = json.loads(p.read_text())
            prefs["company_host"].update(user.get("company_host") or {})
        except (OSError, json.JSONDecodeError):
            pass
    return prefs


def _company_by_key(key: str | None) -> dict[str, str] | None:
    """An explicit company override on a registry entry (e.g. a remote worker
    whose cwd we can't see locally, or a manager). Maps the key to its pill."""
    if not key:
        return None
    for k, label, color, _roots, _kw in _COMPANY_TABLE:
        if k == key:
            return {"company": k, "label": label, "color": color}
    return None


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


def _running_sessions(host: str | None = None) -> list[dict[str, Any]]:
    """tmux sessions ALREADY RUNNING on `host` — the things you can RESUME.

    The other half of "what exists on that machine": you either start fresh in a
    directory, or you pick up something that's already there."""
    fmt = "#{session_name}|#{session_created}|#{session_attached}|#{session_windows}|#{session_path}"
    try:
        code, out, _ = _server._run_tmux(["ls", "-F", fmt], host=host, timeout=20)
    except (FileNotFoundError, Exception):  # noqa: BLE001
        return []
    if code != 0:
        return []
    known = {e.get("session") for e in _server._load_registry().values()}
    rows: list[dict[str, Any]] = []
    now = time.time()
    for ln in (out or "").splitlines():
        p = ln.strip().split("|")
        if len(p) < 5:
            continue
        try:
            created = int(p[1])
        except ValueError:
            continue
        rows.append({
            "name": p[0],
            "created_unix": created,
            "age_sec": max(0, int(now - created)),
            "attached": p[2] == "1",
            "windows": p[3],
            "path": p[4],
            "adopted": p[0] in known,   # already in the emux registry
        })
    rows.sort(key=lambda r: r["created_unix"], reverse=True)   # most recent first
    return rows


_UNSENT_RE = re.compile(r"^[❯>]\s+\S", re.MULTILINE)


def _peek_session(session: str, host: str | None = None, lines: int = 12) -> dict[str, Any]:
    """Look INSIDE a running session before you touch it.

    Resuming is not creating: the thing already has state. You need to see which
    one it is, whether it is holding an unsent prompt someone typed, and whether
    a terminal is already attached — all of which attaching could disturb."""
    try:
        code, out, err = _server._run_tmux(
            ["capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
            host=host, timeout=20)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if code != 0:
        return {"ok": False, "error": (err or "capture failed").strip()}
    text = out or ""
    body = [ln for ln in text.splitlines() if ln.strip()]
    return {
        "ok": True, "session": session, "host": host,
        "content": "\n".join(body[-lines:]),
        # a line like "❯ do the thing" is a prompt typed but never submitted
        "unsent": bool(_UNSENT_RE.search(text)),
    }


def _adopt_session(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an ALREADY-RUNNING session (local or remote) into emux."""
    import asyncio
    session = (data.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "missing_session"}
    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    name = (data.get("name") or "").strip() or session
    try:
        r = asyncio.run(_server.tmux_register(
            name=name, session=session, host=host,
            description=(data.get("description") or "").strip() or None,
            tags=(data.get("tags") or ["adopted"]),
        ))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if data.get("gui"):
        _iterm_attach(session, host)   # a real terminal on it — over ssh when remote
    return {"ok": True, "name": name, "session": session, "host": host, **(r or {})}


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {"_error": "no JSON in model reply", "raw": (text or "")[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"_error": "unparseable JSON", "raw": m.group(0)[:300]}


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
    return _extract_json(proc.stdout or "")


# --- model routing: send cheap high-volume tasks (The Gist, menu/reply) to a
# self-hosted NIM to spare the Claude subscription. NIM MUST be fixed-cost
# (local/self-hosted) — never a metered cloud endpoint. Falls back to claude -p
# whenever NIM is unset or unreachable, so nothing breaks. ---
_MODELS_PATH = Path.home() / ".config" / "emux" / "models.json"
_TASKS = ("gist", "placement")   # model-backed tasks that can be routed


def _model_config() -> dict[str, Any]:
    cfg = {"nim": {"base_url": "", "model": "", "api_key": ""},
           "routes": dict.fromkeys(_TASKS, "claude")}
    try:
        disk = json.loads(_MODELS_PATH.read_text())
        if isinstance(disk.get("nim"), dict):
            cfg["nim"].update({k: disk["nim"].get(k, "") for k in cfg["nim"]})
        if isinstance(disk.get("routes"), dict):
            for t in _TASKS:
                if disk["routes"].get(t) in ("claude", "nim"):
                    cfg["routes"][t] = disk["routes"][t]
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _save_model_config(data: dict[str, Any]) -> dict[str, Any]:
    cfg = _model_config()
    nim = data.get("nim") or {}
    for k in ("base_url", "model", "api_key"):
        if isinstance(nim.get(k), str):
            cfg["nim"][k] = nim[k].strip()
    routes = data.get("routes") or {}
    for t in _TASKS:
        if routes.get(t) in ("claude", "nim"):
            cfg["routes"][t] = routes[t]
    _MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODELS_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


def _nim_json(prompt: str, timeout: int = 60) -> dict[str, Any]:
    """OpenAI-compatible chat call to a self-hosted NIM. Stdlib only."""
    import urllib.error
    import urllib.request
    nim = _model_config()["nim"]
    base, model = nim.get("base_url", ""), nim.get("model", "")
    if not base or not model:
        return {"_error": "nim_not_configured"}
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "temperature": 0.2,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    if nim.get("api_key"):
        headers["Authorization"] = "Bearer " + nim["api_key"]
    try:
        req = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310 (user-configured local endpoint)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read())
        content = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
        return {"_error": f"nim_failed: {e}"}
    return _extract_json(content)


def _llm_json(prompt: str, task: str = "", timeout: int = 90) -> dict[str, Any]:
    """Dispatch a JSON model call by the task's configured route. NIM for routed
    tasks (fixed-cost), else claude -p; NIM failures fall back to claude -p."""
    if task and _model_config()["routes"].get(task) == "nim":
        r = _nim_json(prompt, timeout=min(timeout, 60))
        if "_error" not in r:
            return r
        # NIM down/misconfigured → don't fail the feature, use the subscription CLI
    return _claude_json(prompt, timeout=timeout)


def _nim_ping() -> dict[str, Any]:
    """Check a configured NIM is reachable (GET /models). For the settings 'Test'."""
    import urllib.error
    import urllib.request
    nim = _model_config()["nim"]
    base = nim.get("base_url", "")
    if not base:
        return {"ok": False, "error": "no base_url set"}
    url = base.rstrip("/") + "/models"
    headers = {}
    if nim.get("api_key"):
        headers["Authorization"] = "Bearer " + nim["api_key"]
    try:
        req = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310
            data = json.loads(resp.read())
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        return {"ok": True, "models": [i for i in ids if i][:20]}
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"ok": False, "error": str(e)}


_NEW_VERBS = ("create", "new ", "start a new", "start new", "spin up", "spin-up",
              "build a", "build me", "set up", "set-up", "make a", "make me",
              "i want a", "i want an", "i need a", "i need an", "launch a",
              "fresh", "from scratch", "stand up", "spawn")
_RESUME_VERBS = ("resume", "pick up", "pick back up", "continue", "reattach",
                 "re-attach", "reconnect", "get back to", "back to the",
                 "the one i was", "that i had", "already running", "existing",
                 "still running", "left off")


def _bm25_rank(intent: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank running sessions by lexical relevance to the intent (BM25 over
    name+path+description+tags). Cheap, stdlib, no API — the resume path presents
    the LLM a RELEVANCE-ORDERED list instead of raw registration order, so
    "the greenmark reconcile work" floats the reconcile session to the top.

    Deliberately lexical-only for now: at this fleet size the LLM reads the whole
    (ranked) list, so dense embeddings + RRF would be scale-work for no gain.
    When the fleet outgrows what fits in one prompt, add a dense ranker and fuse
    with this via RRF — this function is the bm25 leg of that."""
    import math
    from collections import Counter

    def tok(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", (s or "").lower())
    docs = [tok(" ".join([s.get("name", ""), s.get("path", ""),
                          s.get("description", ""), " ".join(s.get("tags") or [])]))
            for s in sessions]
    if not any(docs):
        return sessions
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    q = tok(intent)
    k1, b = 1.5, 0.75
    scored = []
    for s, d in zip(docs, sessions):  # noqa: B905 (py3.9 compat, equal-length)
        tf = Counter(s)
        score = sum(
            math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * len(s) / avgdl))
            for t in q if t in tf)
        scored.append((score, d))
    order = sorted(range(len(scored)), key=lambda i: scored[i][0], reverse=True)
    return [dict(scored[i][1], _relevance=round(scored[i][0], 2)) for i in order]


def _intent_company_hint(intent: str) -> tuple[str, str] | None:
    """If the wording names a company (by name or a company keyword), return it.
    Used to bias the directory choice — "an Eidos digest of the org" must not
    land in an AIC repo just because the model liked a repo name there."""
    low = f" {(intent or '').lower()} "
    for key, label, _color, _roots, kw in _COMPANY_TABLE:
        if f" {key} " in low or any(k in low for k in kw if len(k) > 3):
            return key, label
    return None


def _new_vs_resume_lean(intent: str) -> str:
    """A deterministic keyword lean, fed to the model as a HINT (not an override).

    The AI over-weighted a name match ("...manager" → resume the running
    ggo-manager) against an explicit "I want to create". This lifts the wording
    signal so the model weighs it, without hard-coding the decision."""
    low = f" {(intent or '').lower()} "
    n = sum(1 for v in _NEW_VERBS if v in low)
    r = sum(1 for v in _RESUME_VERBS if v in low)
    if n > r:
        return ('the wording leans NEW — they used create/new/build/"I want a" '
                "language, so treat a similarly-named running session as a "
                "coincidence, not a resume target")
    if r > n:
        return "the wording leans RESUME — they pointed at an existing session"
    return "the wording is neutral — decide from what they actually describe"


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

    # a company named in the wording routes BOTH the machine and the directory.
    hint = _intent_company_hint(intent)
    # --- step 1: which machine? (skipped when the user already picked one) ---
    if host:
        chosen_host, host_why = host, ""
    else:
        # per-company home machine (a standing preference, not a per-session ask).
        pref = _routing_prefs()["company_host"].get(hint[0]) if hint else None
        pref = pref if pref in hosts else None
        mhint = (f"\nMACHINE PREFERENCE: {hint[1]} work runs on '{pref}' — pick it "
                 f"unless the intent explicitly names another machine.\n"
                 if (pref and hint) else "")
        r1 = _llm_json(
            "Pick the MACHINE to start a terminal session on.\n\n"
            f"WHAT THE USER WANTS TO DO: {intent}\n"
            f"{mhint}\n"
            "MACHINES (choose exactly one of these names):\n"
            + "\n".join(f"  {h}" for h in hosts) + "\n\n"
            'Reply with ONE line of JSON: {"host": "<one of the names above>", '
            '"why": "<short reason>"}\n'
            'Choose "local" unless a machine preference above applies, or the '
            "intent clearly names a remote machine or needs one."
        )
        if "_error" in r1:
            return r1
        chosen_host = r1.get("host") or "local"
        host_why = r1.get("why") or ""
        if chosen_host not in hosts:          # constrain to a real machine
            chosen_host = "local"

    # --- step 2: what actually exists ON that machine — BOTH kinds ---
    rhost = None if chosen_host == "local" else chosen_host
    dirs = _candidate_dirs(rhost)
    running = _running_sessions(rhost)      # things you could RESUME instead
    base = {"host": chosen_host, "whyHost": host_why,
            "dirs": dirs, "running": running}
    if not dirs and not running:
        return {**base, "action": "new", "name": "", "cwd": "", "command": "",
                "why": f"could not list anything on {chosen_host}", "verified": False}

    # --- step 3: resume something already there, or start fresh in a directory? ---
    lean = _new_vs_resume_lean(intent)
    # enrich with registry description/tags, then rank by BM25 relevance so the
    # session the intent describes floats to the top of what the LLM picks from.
    reg = _server._load_registry()
    reg_by_session = {e.get("session"): e for e in reg.values()}
    for s in running:
        e = reg_by_session.get(s["name"]) or {}
        s.setdefault("description", e.get("description") or "")
        s.setdefault("tags", e.get("tags") or [])
    running = _bm25_rank(intent, running)
    sess_list = "\n".join(
        f"  [{i}] {s['name']} — {_ago(s['age_sec'])} old, {s['path']}"
        f"{' (attached)' if s['attached'] else ''}"
        f"{'  [most relevant]' if i == 0 and s.get('_relevance', 0) > 0 else ''}"
        for i, s in enumerate(running)) or "  (none running)"
    # If the wording names a company, float ITS directories to the top and tell
    # the model — so "an Eidos org digest" doesn't land in an AIC repo.
    hint = _intent_company_hint(intent)
    co_hint = ""
    if hint:
        hk, hl = hint
        dirs = sorted(dirs, key=lambda d: 0 if _detect_company(d).get("company") == hk else 1)
        co_hint = (f"\nCOMPANY: the wording names {hl}. Prefer a {hl} directory "
                   f"(they are listed FIRST). Do NOT pick another company's repo "
                   f"unless the intent clearly points there.\n")
    dir_list = "\n".join(f"  [{i}] {d}" for i, d in enumerate(dirs)) or "  (none)"
    r2 = _llm_json(
        f"A developer wants a terminal session on machine '{chosen_host}'.\n\n"
        f"WHAT THEY SAID: {intent}\n\n"
        "They can either RESUME a session already running on that machine, or "
        "start a NEW one in a directory. Choose the one that matches what they said.\n\n"
        f"PHRASING HINT (from their wording, weigh it — do not obey blindly): {lean}\n"
        f"{co_hint}\n"
        f"ALREADY RUNNING ON {chosen_host} (resume one of these):\n{sess_list}\n\n"
        f"DIRECTORIES ON {chosen_host} (start a new session in one of these):\n{dir_list}\n\n"
        'Reply with ONE line of JSON, nothing else:\n'
        '{"action": "resume" | "new", "session_index": <int, only if resume>, '
        '"dir_index": <int, only if new>, "name": "<short-kebab-session-name>", '
        '"command": "<command for a NEW session; empty string for a plain shell>", '
        '"why": "<one short sentence>"}\n'
        'Resume ONLY when they point at an EXISTING session ("resume", "pick up", '
        '"continue", "reattach", "the one I was working on"). If they say CREATE / '
        'NEW / START / BUILD / SET UP / "I want a ___", it is a NEW session EVEN IF '
        'a running session has a similar name — naming the KIND of thing to make '
        '(a manager, a worker, a bot) is NOT a request to resume something. '
        'Indices MUST come from the '
        "lists above. If they ask for the most recent, note the lists are "
        "ordered newest-first."
    )
    if "_error" in r2:
        return {**base, **r2}

    action = "resume" if str(r2.get("action")) == "resume" else "new"
    why = r2.get("why") or ""

    if action == "resume":
        si = r2.get("session_index")
        if isinstance(si, int) and 0 <= si < len(running):
            s = running[si]
            return {**base, "action": "resume", "verified": True,
                    "session": s["name"], "cwd": s["path"],
                    "name": r2.get("name") or s["name"],
                    "command": "", "why": why}
        return {**base, "action": "resume", "verified": False,
                "session": "", "cwd": "", "name": "", "command": "",
                "why": why or "could not identify which session to resume"}

    di = r2.get("dir_index")
    cwd, verified = "", False
    if isinstance(di, int) and 0 <= di < len(dirs):
        cwd, verified = dirs[di], True   # a real dir on that machine, not invented

    # WHICH AGENT runs here is a routing decision with a registry behind it —
    # not something the model should free-style. Registry wins; the model's
    # command is only a fallback when nothing routes.
    from . import agents as _agents
    adv = _agents.advise(intent)
    command = r2.get("command") or ""
    agent_why = ""
    if adv.get("matched") and adv.get("command"):
        command = adv["command"]
        agent_why = f"{adv['agent']}: {adv['why']}"
    return {**base, "action": "new", "verified": verified,
            "session": "", "cwd": cwd,
            "name": r2.get("name") or "",
            "command": command, "why": why,
            "agent": adv.get("agent"), "agentWhy": agent_why}


def _ago(sec: int) -> str:
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _reply_suggestions(session: str, host: str | None, *, force: bool = False) -> dict[str, Any]:
    """The reader's-digest + 'what do I say' for a session you're looking at.

    Reads the recent pane and asks a model (claude -p / a self-hosted NIM, never a
    metered API) for a 1-2 sentence digest plus a few ready-to-send replies with
    confidences, so a human staring at a wall of output knows the gist AND what to
    do next. CACHED by content hash: while the pane is unchanged the cached result
    is returned instantly (no model call); the poll loop warms it the moment a
    session stops, so it's ready before you open the modal."""
    cap = capture_payload(session, 45, host=host)
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error", "capture_failed")}
    pane = cap.get("content", "")
    h = _gist_hash(pane)
    if not force:
        with _LOCK:
            c = _GIST_CACHE.get(session)
        if c and c.get("hash") == h:   # pane unchanged since we last summarized → serve cache
            return {"ok": True, "digest": c["digest"], "suggestions": c["suggestions"],
                    "cached": True}
    out = _compute_gist(pane)
    if out.get("ok"):
        with _LOCK:
            _GIST_CACHE[session] = {"hash": h, "digest": out["digest"],
                                    "suggestions": out["suggestions"], "ts": time.time()}
    return out


def _warm_gist(session: str, host: str | None) -> None:
    """Populate the gist cache for a session in the background (dedupe concurrent
    warms). Fired when a session stops, so its digest is ready on open."""
    with _LOCK:
        if session in _GIST_INFLIGHT:
            return
        _GIST_INFLIGHT.add(session)
    try:
        _reply_suggestions(session, host)
    except Exception:  # noqa: BLE001, S110  (best-effort warm; never crash the poll)
        pass
    finally:
        with _LOCK:
            _GIST_INFLIGHT.discard(session)


def _compute_gist(pane: str) -> dict[str, Any]:
    """The actual model call + parse for the gist (no caching)."""
    r = _llm_json(
        "A human is looking at an AI agent's terminal session and may not know how "
        "to respond. Here is its recent output:\n<<<\n" + pane[-3500:] + "\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else:\n"
        '{"digest": "<1-2 plain sentences: what the agent is doing, or the '
        'decision/question it is waiting on>", "suggestions": [{"text": "<a short, '
        'ready-to-send reply>", "confidence": <0-100>}, ...]}\n'
        "Give 0-4 suggestions — each a concrete message the human could send RIGHT "
        "NOW to move it forward (approve, redirect, answer, ask a clarifier). Keep "
        "each under ~14 words, phrased as the human talking TO the agent. "
        "For each, set `confidence` = how likely THIS reply is the right move for the "
        "human right now (0-100). Make them genuinely comparative — the best option "
        "high, weaker ones lower; they need not sum to 100. Order best-first. "
        "IMPORTANT: phrase each as a DECISIVE instruction that authorises the agent "
        "to PROCEED and finish autonomously without coming back to ask again — e.g. "
        "'yes, switch it and proceed — don't re-confirm' rather than a bare 'yes'. "
        "The human clicking this wants it handled, not another round of questions. "
        "(A 'cancel/reverse/hold off' option is the one exception that may stop it.) "
        "If the agent is just working and needs nothing, return an empty suggestions "
        "list and say so in the digest.", task="gist", timeout=45)
    if "_error" in r:
        return {"ok": False, "error": r["_error"]}
    sugg = []
    for s in (r.get("suggestions") or [])[:4]:
        if isinstance(s, dict) and str(s.get("text", "")).strip():
            try:
                conf = max(0, min(100, int(round(float(s.get("confidence", 50))))))
            except (TypeError, ValueError):
                conf = 50
            sugg.append({"text": str(s["text"]).strip(), "confidence": conf})
        elif isinstance(s, str) and s.strip():   # tolerate a plain-string reply
            sugg.append({"text": s.strip(), "confidence": 50})
    return {"ok": True, "digest": (r.get("digest") or "").strip(), "suggestions": sugg}


def _spawn_session(data: dict[str, Any]) -> dict[str, Any]:
    """Create a new session (local or remote) — and KICKSTART it.

    A session created from "say what you want to do" should start DOING it, not
    boot to a blank composer. So when the command is a known agent and there's an
    intent, we hand that intent to the agent as its opening prompt (`claude
    <prompt>` / `codex <prompt>`) — the session comes up already working."""
    import asyncio
    import shlex
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing_name"}
    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    cwd = (data.get("cwd") or "").strip() or None
    command = (data.get("command") or "").strip() or None
    intent = (data.get("prompt") or data.get("description") or "").strip()

    kicked = False
    if command and intent:
        from . import adapters
        a = adapters.detect(command.split()[0])
        if a is not None:      # it's a real agent → launch it ON the task
            command = f"{command} {shlex.quote(intent)}"
            kicked = True
    try:
        r = asyncio.run(_server.tmux_spawn(
            name=name, command=command, host=host, cwd=cwd,
            gui=bool(data.get("gui", False)),
            description=(data.get("description") or "").strip() or None,
            tags=data.get("tags") or None,
        ))
        if isinstance(r, dict):
            r["kickstarted"] = kicked
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# which tool-calls are worth showing in the live feed — the fleet's meaningful
# moves, not the capture/poll/classify read-noise that runs every tick.
_FEED_OPS = {"tmux_spawn", "tmux_send", "tmux_register", "tmux_unregister",
             "move_to_emux", "tmux_signals", "tmux_wait"}


def _events(limit: int = 60) -> list[dict[str, Any]]:
    """Merge the fleet's recent activity into one time-ordered feed: up-channel
    signals (IDLE/DONE/NEED/ERROR/PROGRESS) from every session inbox, plus the
    meaningful tool-calls from the audit trail. This is what a human watches to
    see what the agents are doing as they do it."""
    ev: list[dict[str, Any]] = []
    inbox = getattr(_server, "_INBOX_DIR", None)
    if inbox and inbox.is_dir():
        for f in inbox.glob("*.jsonl"):
            try:
                lines = f.read_text(errors="replace").splitlines()[-25:]
            except OSError:
                continue
            for ln in lines:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ev.append({
                    "ts": r.get("t", 0), "kind": "signal",
                    "tag": r.get("kind", "?"),
                    "session": r.get("session") or f.stem,
                    "text": r.get("payload") or "",
                })
    audit = getattr(_server, "_AUDIT_PATH", None)
    if audit and audit.is_file():
        try:
            alines = audit.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            alines = []
        for ln in alines:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            op, ok = r.get("op", ""), r.get("ok", True)
            if op not in _FEED_OPS and ok:
                continue          # skip read-noise; always keep errors
            ev.append({
                "ts": r.get("t", 0), "kind": "error" if not ok else "op",
                "tag": op,
                "session": r.get("target") or r.get("name") or "",
                "text": ("" if ok else f"error: {r.get('error', '')}"),
            })
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:limit]


def _iterm_run(command: str, focus: bool = False) -> tuple[bool, str | None]:
    """Open a NEW iTerm2 window running `command`, driven by AppleScript — no
    `.command` file, so macOS Gatekeeper doesn't throw a quarantine prompt.

    `focus` is OFF by default and MUST stay off for anything that attaches to a
    session you did not just create. Activating steals OS keyboard focus, so a
    window that pops up mid-keystroke swallows what you were typing straight into
    a live agent's prompt — observed for real: adopting a 2-day-old Claude session
    running with bypass-permissions captured stray keys into its input box.
    """
    import platform
    import subprocess
    if platform.system() != "Darwin":
        return False, "macOS/iTerm2 only"
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "iTerm2"\n'
        " create window with default profile\n"
        " tell current session of current window to write text "
        f'"{esc}"\n'
        + (" activate\n" if focus else "")
        + "end tell"
    )
    try:
        g = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if g.returncode != 0:
        return False, (g.stderr or g.stdout or "osascript failed").strip()
    return True, None


def _iterm_attach(session: str, host: str | None = None) -> tuple[bool, str | None]:
    """Open an iTerm2 window attached to `session` — over ssh when it's remote.

    Never takes focus: you are attaching to something that is ALREADY RUNNING and
    may hold an unsent prompt. The window appears; you click into it when you mean
    to type there."""
    if host:
        inner = (f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
                 f"tmux attach -t {shlex.quote(session)}")
        return _iterm_run(f"ssh -t {shlex.quote(host)} {shlex.quote(inner)}", focus=False)
    return _iterm_run(f"tmux attach -t {shlex.quote(session)}", focus=False)




def _pane_command(session: str) -> str:
    """The live foreground process name in the session's active pane."""
    code, out, _ = _server._run_tmux(
        ["display-message", "-p", "-t", session, "#{pane_current_command}"]
    )
    return out.strip() if code == 0 else ""


def _detect_agent(session: str, content: str) -> dict[str, str]:
    """Which AI agent is running in this pane.

    The ADAPTERS own this. What an agent looks like is part of that agent's
    contract — the semver-pane-title trick is a Claude fact and belongs with
    Claude, not in a private table in the web daemon."""
    from . import adapters
    cmd = _pane_command(session).lower()
    a = adapters.detect(cmd, content)
    if a is not None:
        return {"agent": a.key, "label": a.label, "glyph": a.glyph}
    if cmd in adapters.SHELLS:
        return {"agent": "shell", "label": "shell", "glyph": "$"}
    if cmd in adapters.EDITORS:
        return {"agent": "editor", "label": cmd, "glyph": "✎"}
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


_ESCALATED: dict[str, str] = {}   # session -> the gate we've already escalated


def _escalate_if_gated(session: str, agent_key: str, content: str) -> None:
    """A blocked worker must never be SILENT.

    An agent sitting on an approval gate is the fleet's worst failure mode: it
    looks alive, it changes nothing, and it waits forever for a human who does
    not know it is waiting. A human manager who is stuck for three hours and
    says nothing has failed at the job, whatever the reason.

    So the moment a session is gated, it escalates ITSELF up the same `NEED`
    channel a worker uses to ask for help — carrying the exact decision needed,
    not a vague "stuck". A parent blocked in `tmux_wait` wakes immediately; the
    control room shows it. Escalated once per gate (not per poll), and rearmed
    when the gate clears.
    """
    from . import adapters
    gate = adapters.gated(agent_key, content)
    if not gate:
        _ESCALATED.pop(session, None)      # gate cleared — rearm
        return
    if _ESCALATED.get(session) == gate:
        return                             # already escalated THIS gate
    _ESCALATED[session] = gate
    # (1) the up-channel: a parent blocked in tmux_wait wakes immediately.
    try:
        _server.inject_signal(
            session, "NEED",
            f"blocked on a {agent_key} gate: {gate!r} — needs a human decision")
    except Exception:  # noqa: BLE001, S110  — escalation must never break the poll
        pass
    # (2) the HUMAN's tray: file a Hancock request so the block lands where the
    # human actually looks, with the specific ask — not a jsonl nobody watches.
    _file_hancock_escalation(session, agent_key, gate)


def _hancock_db() -> Path:
    return Path(os.environ.get("HANCOCK_DB")
                or Path.home() / ".local" / "state" / "hancock" / "hancock.db")


def _hancock_pending() -> list[dict[str, Any]]:
    """Read Hancock's pending signing tray straight from its SQLite db (read-only,
    no CLI guard). This is what the human must approve/deny."""
    import sqlite3
    db = _hancock_db()
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, command, cwd, reason, risk, created_at "
            "FROM request WHERE status='pending' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 50").fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "command": r["command"], "cwd": r["cwd"],
             "why": r["reason"] or "", "risk": r["risk"], "created_at": r["created_at"]}
            for r in rows]


def _hancock_approve(req_id: str) -> dict[str, Any]:
    """Sign + RUN a request via the real hancock path (`hancock approve <id>`),
    with a scrubbed env so hancock's Claude-Code guard doesn't reject the daemon."""
    import shutil
    import subprocess
    hancock = shutil.which("hancock")
    if not hancock:
        return {"ok": False, "error": "hancock_not_found"}
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY")}
    try:
        p = subprocess.run([hancock, "approve", req_id], env=env,
                           capture_output=True, text=True, timeout=120, check=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()[:600]}


def _hancock_deny(req_id: str, reason: str = "denied from emux") -> dict[str, Any]:
    """Deny a request. Hancock has no CLI deny, so record the decision + mark the
    request denied directly (so the agent blocked in `wait` sees the verdict)."""
    import sqlite3
    import uuid
    db = _hancock_db()
    if not db.is_file():
        return {"ok": False, "error": "no_db"}
    try:
        con = sqlite3.connect(str(db), timeout=5)
        con.execute(
            "INSERT INTO decision (id, request_id, verdict, reason) VALUES (?,?,?,?)",
            (uuid.uuid4().hex[:16], req_id, "deny", reason))
        con.execute(
            "UPDATE request SET status='denied', updated_at=datetime('now') "
            "WHERE id=? AND status='pending'", (req_id,))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _file_hancock_escalation(session: str, agent_key: str, gate: str) -> None:
    """Put a gated worker in the operator's Hancock tray. The queued command
    opens the worker's terminal (`emux head`) so ONE signature brings it up to
    resolve; the `why` carries the exact block. High risk ⇒ always waits.

    Best-effort and isolated: if hancock isn't installed or this fails, the NEED
    signal already fired — escalation degrades, it never breaks the poll."""
    import shutil
    import subprocess
    hancock = shutil.which("hancock")
    if not hancock:
        return
    # a clean env: the daemon may have inherited CLAUDECODE from its spawner,
    # which trips hancock's "don't drive the queue from Claude Code" guard.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY")}
    try:
        subprocess.run(
            [hancock, "add", f"emux head {session}",
             "-why", f"{session} blocked on a {agent_key} gate: {gate} — "
                     "approve to open its terminal and resolve, or deny to leave it",
             "-risk", "high", "--source", f"emux:{session}"],
            env=env, capture_output=True, text=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001, S110
        pass


def _capture_and_observe(session: str, lines: int, host: str | None = None) -> str:
    """Capture a pane (local or remote), fold it into activity state, detect the
    running agent, and refresh the cache."""
    cap = capture_payload(session, lines, host=host)
    content = cap.get("content", "") if cap.get("ok") else ""
    meta = _observe(session, content)
    agent = _detect_agent(session, content)
    agent_key = agent.get("agent", "")
    _escalate_if_gated(session, agent_key, content)
    # is this session WAITING ON THE HUMAN? A real gate on screen — an approval
    # menu or a y/n it can't answer itself. NOT merely "there's a ❯ prompt line"
    # (every Claude pane has those in scrollback); that over-fires on everything.
    # The UI marches ants around a genuinely-blocked session so you can't miss it.
    from . import adapters
    # a real gate/menu lives at the LIVE BOTTOM — the last NON-EMPTY lines (a
    # Claude dialog leaves blank space below it), so echoed menu text up in
    # scrollback isn't flagged, but a real modal dialog still is.
    tail = "\n".join([ln for ln in content.splitlines() if ln.strip()][-10:])
    needs_human = bool(adapters.gated(agent_key, tail))
    state = _quick_state(agent_key, content, needs_human)
    summary = _summarize(agent.get("label", ""), state, content)
    # "clearly stopped" = settled AND the pane has sat STILL for a real pause (not
    # the brief flicker between turns). Warm the gist once per genuine stop, deduped
    # by content-norm so a still session doesn't re-warm every poll.
    age = meta.get("last_change_age")
    norm = (_ACTIVITY.get(session) or {}).get("norm")
    with _LOCK:
        prev = _CACHE.get(session, {})
        warm = _should_warm_gist(state, age, norm, prev.get("gist_warm_norm"),
                                 session in _GIST_INFLIGHT)
        _CACHE[session] = {"content": content, "ts": time.time(), "lines": lines,
                           "agent": agent, "needs_human": needs_human,
                           "state": state, "summary": summary,
                           "gist_warm_norm": norm if warm else prev.get("gist_warm_norm")}
    if warm:
        threading.Thread(target=_warm_gist, args=(session, host), daemon=True).start()
    return content


_SUM_VERB = {"running": "working", "asking": "asks you", "error": "hit an error",
             "idle": "idle", "waiting_human": "needs you", "dead": "gone"}


# UI notices / status meters / tips that are NOT what the agent is doing
_NOISE_RE = re.compile(
    r"update available|brew upgrade|new task\?|/clear to save|/mcp\b|^tip:|"
    r"enter to select|to navigate|esc to (?:cancel|close)|for shortcuts|"
    r"bypass permissions|auto mode on|mcp server|need(?:s)? auth|paste images|"
    r"·\s*[↓↑].*token|\(\d+[ms].*token|\bfor \d+[ms]\b|ctrl\+b|shift\+tab|"   # spinner meters / tmux help
    r"^\s*\d+\.\s|^\s*[▘▝▗▖▀▄█░▚▞◜◝◞◟]|…\s*\+\d+ (?:completed|tool)|"          # menu items / box spinners
    r"^\S+…\s*(?:\(.*\))?$|@[\w.-]+\s.*[%$#]\s*$", re.I)                       # single-word spinner / shell prompt
_ACTION_RE = re.compile(r"^\s*⏺\s*(\S.*)")   # Claude marks its own actions with ⏺


def _headline(content: str) -> str:
    """The cheapest possible 'what is it doing' — read the bottom of the screen,
    no model, no GPU. Prefer the agent's last ACTION line (Claude marks these
    with ⏺); else the last line that isn't chrome/notice/spinner."""
    lines = content.splitlines()
    for ln in reversed(lines):        # 1st choice: the agent's last ⏺ action
        m = _ACTION_RE.match(ln)
        if m and not _NOISE_RE.search(ln):
            s = re.sub(r"\s+", " ", m.group(1).strip())
            if len(s) >= 6:
                return s
    for ln in reversed(lines):        # else: last real, non-noise line
        if not ln.strip() or _CHROME_RE.search(ln) or _NOISE_RE.search(ln):
            continue
        s = re.sub(r"\s+", " ", ln.strip().lstrip("⏺✻✢◇◆•·⎿│>❯▶▸ ").strip())
        if len(s) >= 6:
            return s
    return ""


_OPT_RE = re.compile(r"^\s*(❯|>|›|▶)?\s*(\d{1,2})[.)]\s+(\S.*?)\s*$")


def _parse_options(content: str) -> list[dict[str, Any]]:
    """Pull a numbered selection menu out of the live pane, so the UI can offer
    it as CLICKABLE BUBBLES. Only fires when a menu is genuinely on screen — a
    navigate/select hint near the bottom, or a ❯-cursored numbered list — not any
    stray numbered list in the agent's prose."""
    # NON-EMPTY lines only — a Claude dialog renders with blank space below it,
    # so the last raw lines are empty and the menu sits higher up.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    tail = "\n".join(lines[-12:]).lower()
    has_menu_hint = ("to navigate" in tail or "enter to select" in tail
                     or "esc to cancel" in tail or "enter to confirm" in tail)
    opts, cursor = [], False
    for ln in lines[-14:]:
        m = _OPT_RE.match(ln)
        if m:
            sel = bool(m.group(1))
            cursor = cursor or sel
            opts.append({"n": int(m.group(2)), "label": m.group(3).strip()[:70],
                         "selected": sel})
    # need ≥2 options AND (a menu hint OR a ❯ cursor) to be sure it's a real menu
    if len(opts) >= 2 and (has_menu_hint or cursor):
        # de-dupe by number, keep order
        seen, out = set(), []
        for o in opts:
            if o["n"] not in seen:
                seen.add(o["n"])
                out.append(o)
        return out
    return []


def _summarize(agent_label: str, state: str, content: str) -> str:
    """A super-cheap, LOCAL, always-on one-liner of what a session is doing right
    now — the 'thin rail' text. Deterministic (state verb + the last real output
    line); this is what a model WOULD say, without a model."""
    head = _headline(content)
    verb = _SUM_VERB.get(state, state)
    if head:
        return f"{verb} — {head}"[:200]
    return verb


_QUESTION_PHRASES = re.compile(
    r"\b(do you want|would you like|should i|shall i|which (?:one|option|of)|"
    r"let me know|your call|say the word|approve|amend|confirm|"
    r"waiting (?:on|for) (?:you|your)|awaiting your|need(?:s)? (?:you|your)|"
    r"want me to|proceed\?|go ahead\?|"
    # requests that aren't literal questions but still need the human:
    r"decision needed|needs? (?:a |your )?decision|your (?:input|approval|sign-?off|go-?ahead)|"
    r"blocking the fleet|which do you (?:want|prefer)|pick (?:one|an option)|"
    r"enter to select|↑/↓ to navigate)\b", re.I)
# tmux chrome / composer lines to ignore when looking for the agent's own words
_CHROME_RE = re.compile(r"^\s*(❯|>|⏵|›|»|─+|\[PONYTAIL\]|▶▶|▸▸)|esc to interrupt|shift\+tab",
                        re.I)


def _looks_like_question(content: str) -> bool:
    """Is an IDLE agent asking the human something? Look at its last few real
    output lines (dropping the composer/chrome) for a trailing '?' or a question
    phrase — this catches "say the word on the proposal", "should I…?", etc.,
    which are NOT formal gates but still need you."""
    body = [ln.rstrip() for ln in content.splitlines()
            if ln.strip() and not _CHROME_RE.search(ln)]
    if not body:
        return False
    tail = " ".join(body[-6:]).strip()
    return tail.endswith("?") or bool(_QUESTION_PHRASES.search(tail))


def _quick_state(agent_key: str, content: str, needs_human: bool) -> str:
    """A cheap, per-poll status for at-a-glance fleet tracking — running /
    waiting_human / asking / error / idle. Shown on every tile and flow box so a
    manager (or human) sees each agent's status AND its subs' without opening any.
    The full judge (`/api/classify`) is the precise on-demand read."""
    from . import adapters
    if needs_human:
        return "waiting_human"
    low = content.lower()
    if "traceback (most recent call last)" in low or "\nerror:" in low or " error:" in low:
        return "error"
    if any(s in low for s in adapters.busy_sigs_for(agent_key)):
        return "running"
    # idle — but is it asking YOU something? then it needs you, not resting.
    if _looks_like_question(content):
        return "asking"
    return "idle"


def _capture_many(items: list[tuple[str, str | None]], lines: int) -> None:
    """Capture (session, host) pairs CONCURRENTLY. A remote capture is an ssh
    round-trip (~1-2s); serial was O(n) latency — 7 remotes = 14s. In parallel
    it collapses to roughly one hop. I/O-bound, so threads are the right tool."""
    from concurrent.futures import ThreadPoolExecutor
    if not items:
        return
    with ThreadPoolExecutor(max_workers=min(10, len(items))) as ex:
        list(ex.map(lambda it: _capture_and_observe(it[0], lines, host=it[1]), items))


def poll_once(lines: int = 14) -> None:
    """One capture sweep over all live sessions (local AND remote), in parallel;
    evicts state for dead ones. Called on a timer by the background loop."""
    if _server._resolve_tmux() is None:
        return
    live = _server._live_sessions()
    live_names = {s["name"] for s in live}
    items: list[tuple[str, str | None]] = [(s["name"], None) for s in live]
    # registered REMOTE sessions never appear in the local list — poll them too,
    # so a rentamac worker is cached and the grid serves it fast (not re-ssh'd).
    rhosts = {h for e in _server._load_registry().values() if (h := e.get("host"))}
    for host in rhosts:
        for name in _remote_live_names(host):
            items.append((name, host))
            live_names.add(name)
    _capture_many(items, lines)
    with _LOCK:
        for dead in [k for k in _ACTIVITY if k not in live_names]:
            _ACTIVITY.pop(dead, None)
            _CACHE.pop(dead, None)
            _GIST_CACHE.pop(dead, None)      # cache-bust: a dead session's gist is void
            _GIST_INFLIGHT.discard(dead)


def grid_payload(lines: int = 14) -> dict[str, Any]:
    """Session list with a mini pane capture + activity meta per live session.
    Serves from the daemon cache when fresh; captures on miss (cold start, or
    when the poll loop isn't running, e.g. under tests)."""
    base = sessions_payload()
    if not base["ok"]:
        return base
    now = time.time()
    # capture every stale/missing live pane IN PARALLEL (remotes are ssh hops).
    misses = []
    for item in base["sessions"]:
        if not item["live"]:
            continue
        with _LOCK:
            ce = _CACHE.get(item["session"])
        if ce is None or (now - ce["ts"]) >= _CACHE_TTL:
            misses.append((item["session"], item.get("host")))
    _capture_many(misses, lines)
    for item in base["sessions"]:
        if item["live"]:
            with _LOCK:
                ce = _CACHE.get(item["session"])
            content = (ce or {}).get("content", "")
            item["content"] = content
            item["agent"] = (ce or {}).get("agent") or {"agent": "unknown", "label": "—", "glyph": "·"}
            item["needs_human"] = bool((ce or {}).get("needs_human"))
            item["state"] = (ce or {}).get("state") or "idle"
            item["summary"] = (ce or {}).get("summary") or ""
            item.update(_meta(item["session"]))
        else:
            item["content"] = ""
            item["needs_human"] = False
            item["state"] = "dead"
            item["summary"] = ""
            item["changed"] = False
            item["last_change_age"] = None
            item["activity"] = []
            item["agent"] = {"agent": "gone", "label": "", "glyph": ""}
    return base


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def send_payload(session: str, keys: str, literal: bool = True, enter: bool = True,
                 host: str | None = None) -> dict[str, Any]:
    """Send keys to `session`, local or — when `host` is set — over ssh. literal=
    True sends text verbatim (`send-keys -l`), so chat input like "C-c" types
    those characters; literal=False interprets tmux key names (UI control chips)."""
    if host is None and _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    if literal:
        if keys:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "-l", keys], host=host)
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
        if enter:
            # a paste-detecting TUI (Claude) needs text and Enter as SEPARATE
            # events with a beat between, or the Enter is swallowed into the paste
            # and the message never submits. Use the agent's measured settle.
            settle = _server._pane_settle(session, host)
            if keys and settle > 0:
                time.sleep(settle)
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "Enter"], host=host)
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    else:
        args = ["send-keys", "-t", session, keys]
        if enter:
            args.append("Enter")
        code, _, err = _server._run_tmux(args, host=host)
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
:root{   /* Eidos light — the default skin; JS may swap to another via a company pill */
  --bg:#f0ebe4;
  --bg-raise:#e9e3db;
  --bg-card:#e4ded6;
  --amber:#8e6129;
  --amber-dim:#a9853f;
  --amber-faint:#d8cdba;
  --text:#1e1a17;
  --text-dim:#6b6159;
  --live:#4a6a3a;
  --stale:#ab5036;
  --line:#cabfae;
  --user:#8e6129;
  --on-accent:#f5efe6;
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
.tagchip.on{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.tagchip .cnt{opacity:.6;margin-left:3px}
.tagchip.clr{color:var(--text-dim)}
.cochip{font-size:10px;letter-spacing:.3px;padding:2px 7px;border:1px solid;border-radius:10px;
  cursor:pointer;user-select:none;white-space:nowrap;font-weight:700;opacity:.9}
.cochip:hover{opacity:1}
.cochip.on{color:var(--on-accent)}
.cochip .cnt{opacity:.7;margin-left:3px;font-weight:400}
.cco{font-size:9px;font-weight:700;letter-spacing:.3px;padding:1px 6px;border-radius:8px;
  color:var(--on-accent);white-space:nowrap}
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
.dot.warn{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.dot.stale{background:var(--stale);box-shadow:0 0 6px var(--stale)}
.card .sub{color:var(--text-dim);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .badges{margin-top:4px;font-size:10px}
.card .badges span{border:1px solid var(--line);color:var(--text-dim);padding:0 5px;margin-right:4px}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--text-dim);font-size:10px;letter-spacing:1px}
#main{flex:1;height:100%;display:flex;flex-direction:column;min-width:0}
/* live fleet feed — a collapsible right rail showing what agents do as they do it */
#feed{width:300px;flex:none;height:100%;border-left:1px solid var(--line);
  background:var(--bg-raise);display:flex;flex-direction:column;transition:width .18s ease}
#feed:not(.open){width:0;border-left:none;overflow:hidden}
#feedhead{flex:none;display:flex;align-items:center;gap:8px;padding:11px 14px;
  border-bottom:1px solid var(--line);font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--amber-dim)}
#feedcount{color:var(--text-dim);font-size:10px}
#feedclose{margin-left:auto;background:transparent;border:none;color:var(--text-dim);
  font-size:18px;cursor:pointer;line-height:1;padding:0 4px}
#feedclose:hover{color:var(--amber)}
#feedlist{flex:1;overflow-y:auto;padding:6px 0}
.fev{display:flex;gap:8px;padding:5px 12px;border-bottom:1px solid rgba(255,176,0,.05);font-size:11px;align-items:baseline}
.fev .fage{color:var(--text-dim);font-size:9px;white-space:nowrap;min-width:26px}
.fev .ftag{font-weight:700;white-space:nowrap}
.fev .fsess{color:var(--amber-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:90px}
.fev .ftext{color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.fev.k-NEED .ftag,.fev.k-ERROR .ftag,.fev.k-error .ftag{color:var(--stale)}
.fev.k-IDLE .ftag,.fev.k-DONE .ftag,.fev.k-READY .ftag{color:var(--live)}
.fev.k-PROGRESS .ftag{color:var(--user)}
.fev.k-op .ftag{color:var(--amber-dim)}
.fev.fresh{animation:fevin .5s ease}
@keyframes fevin{from{background:rgba(255,176,0,.18)}to{background:transparent}}
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
.tab.on{background:var(--amber);color:var(--on-accent);border-color:var(--amber)}
#views{flex:1;overflow-y:auto;padding:18px}
.tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.tile{
  border:1px solid var(--line);background:var(--bg-card);cursor:pointer;overflow:hidden;
  display:flex;flex-direction:column;transition:border-color .2s, box-shadow .4s;
}
.tile:hover{border-color:var(--amber-dim)}
.tile.hot{border-color:var(--amber);box-shadow:0 0 16px rgba(255,176,0,.25)}
.tile.dead{opacity:.45}
/* WAITING ON YOU: marching ants around the edge + a slow breathing orb glow, so a
   session that needs your decision is impossible to miss on a wall of tiles. */
.needy{position:relative;animation:orb 1.6s ease-in-out infinite}
/* canonical marching ants: four dashed edges, each scrolling one dash-period.
   Reliable (moves visibly) where the masked-border trick was not. */
.needy::before{content:"";position:absolute;inset:0;pointer-events:none;z-index:3;
  background-image:
    linear-gradient(90deg, var(--amber) 50%, transparent 50%),
    linear-gradient(90deg, var(--amber) 50%, transparent 50%),
    linear-gradient(0deg,  var(--amber) 50%, transparent 50%),
    linear-gradient(0deg,  var(--amber) 50%, transparent 50%);
  background-size:14px 2px, 14px 2px, 2px 14px, 2px 14px;
  background-position:0 0, 0 100%, 0 0, 100% 0;
  background-repeat:repeat-x, repeat-x, repeat-y, repeat-y;
  animation:ants .55s infinite linear}
@keyframes ants{to{background-position:14px 0, -14px 100%, 0 -14px, 100% 14px}}
@keyframes orb{0%,100%{box-shadow:0 0 6px rgba(255,176,0,.35)}50%{box-shadow:0 0 22px 3px rgba(255,176,0,.6)}}
.card.needy{border-left-color:var(--amber)}
/* LOUD needs-you: a red ring + glow + a pulsing corner badge — red reads as
   "attention" against the amber theme, which amber-on-amber ants did not. */
.tile.needy{border:2px solid #e0483a;animation:needpulse 1.3s ease-in-out infinite}
.tile.needy::before{background-image:
  linear-gradient(90deg,#e0483a 50%,transparent 50%),linear-gradient(90deg,#e0483a 50%,transparent 50%),
  linear-gradient(0deg,#e0483a 50%,transparent 50%),linear-gradient(0deg,#e0483a 50%,transparent 50%)}
@keyframes needpulse{0%,100%{box-shadow:0 0 0 0 rgba(224,72,58,.55),0 0 14px rgba(224,72,58,.4)}
  50%{box-shadow:0 0 0 4px rgba(224,72,58,0),0 0 22px 3px rgba(224,72,58,.65)}}
.needbadge{position:absolute;top:7px;right:7px;z-index:5;background:#c0392b;color:#fff;
  font-size:9.5px;font-weight:800;letter-spacing:.6px;padding:3px 7px;border-radius:5px;
  box-shadow:0 1px 5px rgba(0,0,0,.35);animation:needpulse 1.3s ease-in-out infinite}
.card.needy{border-left:4px solid #e0483a !important;background:rgba(224,72,58,.06)}
pre.gonecache{color:var(--text-dim);font-style:italic;opacity:.85;white-space:pre-wrap}
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
.cell{width:9px;height:18px;background:var(--bg-card);flex:none}
.cell.on{background:var(--amber);box-shadow:0 0 5px rgba(255,176,0,.6)}
.cell.recent{background:var(--user)}
.actrow .age{font-size:11px;color:var(--text-dim);width:120px;text-align:right;flex:none;letter-spacing:1px}
/* flow view — live mini-pane boxes over an SVG edge layer */
#flowwrap{position:relative;margin:0 auto}
#flowsvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.fbox{
  position:absolute;transform:translate(-50%,-50%);z-index:1;
  border:1px solid var(--line);background:var(--bg-card);cursor:pointer;overflow:hidden;
  transition:border-color .2s, box-shadow .4s;
}
.fbox:hover{border-color:var(--amber-dim)}
.fbox.hot{border-color:var(--amber);box-shadow:0 0 16px rgba(255,176,0,.25)}
.fbox.dead{opacity:.45}
.fbox .ftitle{display:flex;align-items:center;gap:6px;padding:5px 9px;background:var(--bg-card);border-bottom:1px solid var(--line)}
.fbox .ftitle .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fbox .ftitle .ag{margin-left:auto;color:var(--amber-dim);font-size:10px;letter-spacing:.5px;white-space:nowrap}
/* per-agent status pip — at-a-glance state at EVERY level of the hierarchy, so
   the manager (and human) tracks all agents + their subs without opening them. */
.spip{font-size:8.5px;letter-spacing:.5px;text-transform:uppercase;padding:1px 5px;border-radius:7px;
  white-space:nowrap;font-weight:700;border:1px solid currentColor}
.spip.st-running{color:var(--live)}
.spip.st-idle{color:var(--text-dim)}
.spip.st-error{color:var(--stale)}
.spip.st-waiting_human{color:var(--amber);animation:orb 1.8s ease-in-out infinite}
.spip.st-dead{color:var(--text-dim);opacity:.5}
.spip.st-asking{color:var(--amber);animation:orb 1.6s ease-in-out infinite}
/* it's asking YOU — a pulsing question mark in place of the heartbeat */
.qmark{display:inline-flex;align-items:center;justify-content:center;
  width:14px;height:14px;margin-right:6px;border-radius:50%;
  background:var(--amber);color:var(--on-accent);font-weight:800;font-size:10px;
  vertical-align:middle;animation:qpulse 1.1s ease-in-out infinite}
@keyframes qpulse{0%,100%{box-shadow:0 0 0 0 rgba(255,176,0,.5)}50%{box-shadow:0 0 0 4px rgba(255,176,0,0)}}
/* the summary rail — a thin always-on "what's happening", hover for the full text */
.rail{position:relative;font-size:9.5px;line-height:1.5;padding:2px 10px;
  background:var(--bg-raise);border-bottom:1px solid var(--line);color:var(--text-dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:default;flex:none}
.rail .rv{font-weight:700;color:var(--amber-dim);text-transform:uppercase;letter-spacing:.5px;font-size:8.5px}
.rail.st-running .rv{color:var(--live)}
.rail.st-error .rv{color:var(--stale)}
.rail.st-asking .rv,.rail.st-waiting_human .rv{color:var(--amber)}
.rfull{display:none;position:absolute;left:0;right:0;top:100%;z-index:30;
  background:var(--bg-card);border:1px solid var(--amber-faint);border-radius:0 0 3px 3px;
  padding:7px 11px;white-space:normal;font-size:11px;line-height:1.4;color:var(--text);
  box-shadow:0 6px 16px rgba(0,0,0,.45)}
.rail:hover{overflow:visible}
.rail:hover .rfull{display:block}
/* a RUNNING agent shows a hospital-monitor heartbeat instead of a static dot */
.ekg{width:30px;height:12px;flex:none;vertical-align:middle}
.ekg polyline{fill:none;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.ekg .base{stroke:var(--live);opacity:.20}
.ekg .beat{stroke:var(--live);stroke-dasharray:8 48;animation:ekg 1.25s linear infinite}
@keyframes ekg{from{stroke-dashoffset:56}to{stroke-dashoffset:0}}
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
  align-self:flex-end;background:var(--bg-raise);border-color:var(--amber-faint);
  color:var(--user);border-radius:10px 10px 0 10px;
}
.bubble.sys{align-self:center;color:var(--text-dim);font-size:11px;border:none;font-style:italic}
#screen-bubble{
  align-self:flex-start;width:100%;max-width:100%;
  background:var(--bg-card);border:1px solid var(--line);border-radius:10px 10px 10px 0;
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
  flex:1;background:var(--bg-card);border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber);
}
#input:focus{border-color:var(--amber-dim);box-shadow:0 0 12px rgba(255,176,0,.1)}
#send{
  font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 26px;
  background:var(--amber);color:var(--on-accent);border:none;cursor:pointer;
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
  margin:0 8px 8px;width:calc(100% - 16px);background:var(--bg-card);border:1px solid var(--line);
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
  background:var(--amber);color:var(--on-accent);border:none;padding:4px 16px;cursor:pointer;
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
#newbody{padding:16px;display:flex;flex-direction:column;gap:4px;max-height:68vh;overflow-y:auto}
#newbody input{background:var(--bg);border:1px solid var(--line);color:var(--amber);
  font-family:inherit;font-size:13px;padding:7px 10px;width:100%}
#newbody input:focus{outline:none;border-color:var(--amber-dim)}
#newbody input:disabled{opacity:.45}
.askrow{display:flex;gap:8px;margin-bottom:6px}
.askrow input{font-size:15px;padding:11px 12px}
/* THE ANSWER — the only thing you should have to read */
#result{display:none;padding:14px 2px 4px}
#result.show{display:block}
#rverb{font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--text-dim);margin-bottom:2px}
#rwhat{font-family:"VT323",monospace;font-size:30px;line-height:1.1;color:var(--amber);letter-spacing:1px}
#rwhere{font-size:12px;color:var(--amber-dim);margin-top:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#rflags{margin-top:6px}
#rflags .flag{font-size:9.5px;padding:2px 7px;border-radius:9px;margin-right:5px;
  background:rgba(255,95,86,.16);color:#ff8a80;border:1px solid rgba(255,95,86,.4)}
#rwhy{font-size:11px;font-style:italic;color:var(--text-dim);margin-top:8px}
#rpeek{margin-top:8px}
#rpeek summary{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);cursor:pointer}
#rpeek[hidden]{display:none}
#peek2{margin:6px 0 0;padding:7px 9px;border:1px solid var(--line);background:var(--bg);
  font-size:10.5px;line-height:1.35;color:var(--amber-dim);max-height:120px;overflow:auto;white-space:pre;opacity:.85}
/* the machinery — only when you ask for it */
#tree{display:none}
#tree.show{display:block}
.linkish{background:transparent;border:none;color:var(--text-dim);font-family:inherit;
  font-size:11px;cursor:pointer;margin-right:auto;text-decoration:underline;padding:0}
.linkish:hover{color:var(--amber-dim)}
/* a step is dependent on the one above it — locked until that one resolves */
.step{border-left:2px solid var(--line);padding:8px 0 10px 12px;margin-left:3px}
.step.locked{opacity:.38;pointer-events:none}
.step.done{border-left-color:var(--amber)}
.steph{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.steph .num{width:16px;height:16px;border-radius:50%;border:1px solid var(--amber-faint);
  color:var(--amber-dim);font-size:10px;display:flex;align-items:center;justify-content:center}
.step.done .steph .num{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.steph .lbl{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
.steph .sub{font-size:10px;color:var(--text-dim);opacity:.65}
.steph .why{margin-left:auto;font-size:10px;font-style:italic;color:var(--amber-dim);
  max-width:52%;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.hchip{font-size:11px;padding:3px 10px;border:1px solid var(--line);border-radius:11px;
  color:var(--text-dim);cursor:pointer;user-select:none}
.hchip:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.hchip.on{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.hchip.ai::after{content:" ✦";opacity:.8}
.lanes{display:flex;gap:6px;margin-bottom:5px}
.lane{font-size:10px;letter-spacing:.5px;padding:3px 10px;border:1px solid var(--line);
  color:var(--text-dim);cursor:pointer;user-select:none;border-radius:3px}
.lane:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.lane.on{border-color:var(--amber);color:var(--amber);background:rgba(255,176,0,.09)}
.lane b{font-weight:700;opacity:.7;margin-left:5px}
.dirchoices{max-height:150px;overflow-y:auto;border:1px solid var(--line);margin-top:5px}
.dirrow .meta{opacity:.55;margin-left:8px;font-size:10px}
.dirrow.on .meta{opacity:.8}
/* resuming is not creating — you're touching something that already has state, so look at it */
#peekwrap{display:none;margin-top:7px;border:1px solid var(--line);background:var(--bg)}
#peekwrap.show{display:block}
#peekhead{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);
  padding:4px 8px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#peekflags .flag{font-size:9px;padding:1px 6px;border-radius:8px;margin-left:4px;
  background:rgba(255,95,86,.16);color:#ff8a80;border:1px solid rgba(255,95,86,.4)}
#peek{margin:0;padding:7px 9px;font-size:10.5px;line-height:1.35;color:var(--amber-dim);
  max-height:112px;overflow:auto;white-space:pre;opacity:.85}
#guinote{font-size:10px;color:#ffb27d;margin-top:4px}
#guinote:empty{display:none}
.dirrow{padding:5px 9px;font-size:12px;color:var(--text-dim);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid rgba(255,176,0,.06)}
.dirrow:hover{background:rgba(255,176,0,.07);color:var(--amber-dim)}
.dirrow.on{background:var(--amber);color:var(--on-accent);font-weight:700}
.dirrow .ai{opacity:.75;margin-left:6px}
#newbody .chk{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--amber-dim);margin-top:8px}
#newbody .chk input{width:auto}
#newsummary{margin-right:auto;font-size:11px;color:var(--text-dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:75%}
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
  background:var(--amber);border:none;color:var(--on-accent);cursor:pointer;font-weight:700}
#newcreate:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
#newcreate:disabled{opacity:.5;cursor:default;box-shadow:none}
#modaliterm{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:13px;cursor:pointer;padding:2px 11px;margin-left:10px}
#modaliterm:hover{color:var(--amber);border-color:var(--amber-dim)}
#modaliterm:disabled{opacity:.6;cursor:default}
#modalscreen{
  flex:1;overflow-y:auto;font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:14px 16px;background:var(--bg-card);
}
/* live classifier strip (emux judge) */
#modaljudge{display:none;align-items:center;gap:12px;padding:8px 16px;flex:none;
  border-bottom:1px solid var(--line);background:var(--bg-raise);font:11px "IBM Plex Mono",monospace}
#modaljudge.on{display:flex}
#modaljudge .jstate{font-weight:700;letter-spacing:1px;text-transform:uppercase;font-size:12px;white-space:nowrap}
#modaljudge .jconf{color:var(--text-dim);font-size:10px;white-space:nowrap}
#modaljudge .jsum{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modaljudge .jflag{border:1px solid #7a3a1a;color:#ff9f43;padding:0 5px;font-size:10px;
  text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.s-running{color:#8fd88f}.s-done_idle{color:#8a8a72}.s-error{color:#ff5f56}
.s-thrashing{color:#ff9f43}.s-stuck{color:#ffb000}.s-waiting_human{color:#38d9ff}
.s-waiting_external{color:#7a8fd8}.s-planning{color:#d0b24a}.s-editing{color:#d0b24a}.s-dead{color:#666}
/* thinking indicator — movement + a live timer while the agent is generating */
#modalthink{display:none;align-items:center;gap:7px;margin-left:14px;font-size:11px;
  color:var(--live);letter-spacing:.5px}
#modalthink.on{display:inline-flex}
#modalthink b{color:var(--text);font-variant-numeric:tabular-nums}
.tdots{display:inline-flex;gap:3px}
.tdots i{width:4px;height:4px;border-radius:50%;background:var(--live);display:block;
  animation:tbounce 1s ease-in-out infinite}
.tdots i:nth-child(2){animation-delay:.16s}
.tdots i:nth-child(3){animation-delay:.32s}
@keyframes tbounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-4px);opacity:1}}
/* pending send — a message received by emux but not yet reflected in the chat */
#modalpending{display:none}
#modalpending.on{display:block;margin:0 16px 8px;padding:7px 11px;font-size:12px;
  background:var(--bg-card);border:1px dashed var(--amber-faint);border-radius:6px;color:var(--text-dim)}
#modalpending .plabel{color:var(--amber-dim);font-size:9px;letter-spacing:1.5px;text-transform:uppercase;margin-right:7px}
#modalpending .ptext{color:var(--text)}
/* the gist — reader's-digest + suggested replies, so you know what to do */
#modaldigest{display:none}
#modaldigest.on{display:block;padding:11px 16px;background:var(--bg-raise);
  border-bottom:1px solid var(--amber-faint)}
#modaldigest .dghead{display:flex;align-items:center;font-size:10px;letter-spacing:2px;
  text-transform:uppercase;color:var(--amber-dim);margin-bottom:5px}
#dgrefresh{margin-left:auto;background:transparent;border:none;color:var(--text-dim);
  font-size:13px;cursor:pointer}
#dgrefresh:hover{color:var(--amber)}
#modaldigest .dgtext{font-size:13px;line-height:1.5;color:var(--text)}
#modaldigest.loading .dgtext{color:var(--text-dim);font-style:italic}
#modaldigest .dgsugg{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
#modaldigest .dgsugg:empty{display:none}
.sgg{background:var(--amber);border:1.5px solid var(--amber);color:var(--on-accent);font-weight:650;
  font-family:inherit;font-size:12.5px;padding:7px 13px;border-radius:15px;cursor:pointer;
  transition:filter .12s,transform .08s;text-align:left;box-shadow:0 2px 6px rgba(0,0,0,.22)}
.sgg:hover{filter:brightness(1.12);transform:translateY(-1px)}
.sgg:active{transform:translateY(0)}
.sgg:disabled{opacity:.45;cursor:default;box-shadow:none}
/* clickable answer bubbles — overlay the chat when the agent shows a menu */
#modalopts{display:none}
#modalopts.on{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px 2px;
  border-top:1px solid var(--amber-faint);background:var(--bg-raise)}
#modalopts .ohint{flex-basis:100%;font-size:10px;letter-spacing:1px;text-transform:uppercase;
  color:var(--amber-dim);margin-bottom:2px}
.obub{background:var(--amber);border:1.5px solid var(--amber);color:var(--on-accent);font-weight:650;
  font-family:inherit;font-size:13px;padding:8px 14px;border-radius:16px;cursor:pointer;
  transition:filter .12s,transform .08s;max-width:100%;text-align:left;box-shadow:0 2px 6px rgba(0,0,0,.22)}
.obub:hover{filter:brightness(1.12);transform:translateY(-1px)}
.obub:active{transform:translateY(0)}
.obub b{color:var(--on-accent);opacity:.75;margin-right:6px;font-weight:800}
.obub.sel{box-shadow:0 0 0 3px var(--bg),0 0 0 5px var(--amber)}
.obub:disabled{opacity:.45;cursor:default;box-shadow:none}
.obub:disabled{opacity:.5;cursor:default}
#modalchips{display:flex;gap:8px;padding:10px 16px 0}
#modalrow{display:flex;gap:10px;padding:10px 16px 14px}
#modalinput{flex:1;background:var(--bg-card);border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber)}
#modalinput:focus{border-color:var(--amber-dim);box-shadow:0 0 12px rgba(255,176,0,.1)}
#modalsend{font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 24px;
  background:var(--amber);color:var(--on-accent);border:none;cursor:pointer}
#modalsend:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--amber-faint)}
::-webkit-scrollbar-track{background:transparent}
/* --- Hancock approvals --- */
#hbtn.hot{border-color:var(--amber);color:var(--amber);font-weight:700}
#hbadge{background:#c0392b;color:#fff;border-radius:9px;padding:0 6px;margin-left:5px;font-size:11px;font-weight:700}
#hbanner{display:none;position:fixed;top:0;left:0;right:0;z-index:120;
  background:#c0392b;color:#fff;text-align:center;padding:9px 12px;font-weight:600;cursor:pointer;
  box-shadow:0 2px 14px rgba(192,57,43,.5)}
#hbanner u{text-underline-offset:3px}
.hbell{display:inline-block}
body.hneedy #hbanner{display:block;animation:hpulse 1.1s ease-in-out infinite}
body.hneedy #hbanner .hbell{display:inline-block;animation:hshake .9s ease-in-out infinite;transform-origin:50% 0}
body.hneedy #main,body.hneedy #side{outline:2px solid #c0392b;outline-offset:-2px}
@keyframes hpulse{0%,100%{background:#c0392b}50%{background:#e05545}}
@keyframes hshake{0%,100%{transform:rotate(0)}20%{transform:rotate(-16deg)}40%{transform:rotate(14deg)}60%{transform:rotate(-8deg)}80%{transform:rotate(6deg)}}
#htray{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:92vw;z-index:130;
  background:var(--bg-raise);border-left:2px solid var(--amber);box-shadow:-8px 0 24px rgba(0,0,0,.35);
  display:flex;flex-direction:column;transform:translateX(102%);transition:transform .28s cubic-bezier(.22,1,.36,1)}
#htray.open{transform:translateX(0)}
#htrayhead{padding:12px 14px;border-bottom:1px solid var(--line);color:var(--text);font-size:13px;letter-spacing:.5px}
#htrayhead b{color:var(--amber)}
#htrayclose{float:right;cursor:pointer;color:var(--text-dim);font-size:15px}
#htoast{max-height:0;overflow:hidden;opacity:0;margin:0 10px;border-radius:6px;
  font-size:12px;font-weight:600;transition:max-height .2s,opacity .2s,padding .2s;padding:0 12px}
#htoast.show{max-height:44px;opacity:1;padding:9px 12px;margin-top:8px}
#htoast.ok{background:rgba(46,160,67,.16);color:#2ea043;border:1px solid rgba(46,160,67,.4)}
#htoast.err{background:rgba(192,57,43,.16);color:#e05545;border:1px solid rgba(192,57,43,.45)}
.hreq.working{opacity:.55}
#htraylist{overflow:auto;padding:10px;display:flex;flex-direction:column;gap:10px}
.hempty{color:var(--text-dim);text-align:center;padding:30px 10px;font-size:13px}
.hreq{background:var(--bg-card);border:1px solid var(--line);border-left:3px solid var(--stale);border-radius:8px;padding:10px}
.hreq.r-high,.hreq.r-critical{border-left-color:#c0392b}
.hreq.r-medium{border-left-color:var(--amber)}
.hrisk{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text-dim);margin-bottom:4px}
.hreq.r-high .hrisk,.hreq.r-critical .hrisk{color:#e05545}
.hcmd{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--text);word-break:break-all;margin-bottom:5px}
.hwhy{font-size:12px;color:var(--text-dim);margin-bottom:3px}
.hcwd{font-size:11px;color:var(--text-dim);opacity:.7;font-family:ui-monospace,monospace;margin-bottom:7px}
.hact{display:flex;gap:8px}
.happrove,.hdeny{flex:1;padding:7px;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px;border:1px solid}
.happrove{background:var(--amber);color:var(--on-accent);border-color:var(--amber)}
.hdeny{background:var(--bg-raise);color:var(--text);border-color:var(--text-dim);font-weight:600}
.hdeny:hover{background:#c0392b;color:#fff;border-color:#c0392b}
/* --- settings modal --- */
#setmodal{display:none;position:fixed;inset:0;z-index:140;background:rgba(0,0,0,.45);
  align-items:center;justify-content:center}
#setmodal.open{display:flex}
#setcard{background:var(--bg-raise);border:1px solid var(--line);border-radius:12px;width:560px;max-width:94vw;
  max-height:88vh;overflow:auto;padding:0 0 16px;box-shadow:0 12px 40px rgba(0,0,0,.4)}
#sethead{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;
  border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg-raise)}
#sethead b{color:var(--amber);letter-spacing:.5px}
.setx{cursor:pointer;color:var(--text-dim);font-size:16px}
.setnote{font-size:12px;color:var(--text-dim);padding:12px 18px 4px;line-height:1.5}
.setnote code{background:var(--bg-card);padding:1px 5px;border-radius:4px}
.setsec{padding:10px 18px}
.setsec h4{margin:6px 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--text)}
.setpill{font-size:10px;background:var(--amber-faint);color:var(--amber-dim);padding:2px 7px;border-radius:8px;
  text-transform:none;letter-spacing:0;margin-left:6px;font-weight:600}
.setroute{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:7px 0;
  border-bottom:1px solid var(--line);font-size:13px;color:var(--text)}
.setroute span{flex:1}
.setroute select,#setcard input{background:var(--bg-card);color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:6px 9px;font-family:inherit;font-size:12.5px}
#setcard label{display:block;font-size:12px;color:var(--text-dim);margin:8px 0}
#setcard label input{display:block;width:100%;margin-top:4px;box-sizing:border-box}
.setdim{opacity:.7}
.setrow{display:flex;align-items:center;gap:10px;margin-top:10px}
.setfoot{display:flex;align-items:center;gap:12px;padding:12px 18px 0}
#setsave,#nimtest{background:var(--amber);color:var(--on-accent);border:none;font-weight:650;
  padding:8px 18px;border-radius:7px;cursor:pointer;font-size:13px}
#nimtest{background:var(--bg-card);color:var(--text);border:1px solid var(--amber)}
#nimtestout.ok,#setsaveout.ok{color:#2ea043;font-size:12px}
#nimtestout.err,#setsaveout.err{color:#e05545;font-size:12px}
/* --- floating per-terminal chat: choices as chips + type-your-own, NCDMV-style --- */
#tchat{position:fixed;right:26px;bottom:26px;z-index:60;width:340px;max-width:90vw}
#tchat.collapsed #tchatpanel{display:none}
#tchat:not(.collapsed) #tchatlauncher{display:none}
#tchatlauncher{width:54px;height:54px;border-radius:50%;background:var(--amber);color:var(--on-accent);
  border:none;font-size:23px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4);position:relative}
#tchatbadge{position:absolute;top:-3px;right:-3px;background:#c0392b;color:#fff;font-size:11px;
  min-width:19px;height:19px;border-radius:10px;display:none;align-items:center;justify-content:center;
  padding:0 4px;font-weight:800;box-shadow:0 1px 4px rgba(0,0,0,.35)}
#tchatpanel{background:var(--bg-raise);border:1px solid var(--amber);border-radius:14px;overflow:hidden;
  box-shadow:0 12px 44px rgba(0,0,0,.5);display:flex;flex-direction:column;max-height:64vh}
#tchathead{background:var(--amber);color:var(--on-accent);padding:9px 13px;font-size:13px;font-weight:650;
  display:flex;justify-content:space-between;align-items:center}
#tchathead b{font-weight:800}
#tchatmin{background:transparent;border:none;color:var(--on-accent);font-size:15px;cursor:pointer}
#tchatlog{padding:9px 11px 0;display:flex;flex-direction:column;gap:6px;overflow:auto;max-height:26vh}
#tchatlog:empty{display:none}
.tcmsg{max-width:86%;padding:6px 10px;border-radius:11px;font-size:12px;line-height:1.35;white-space:pre-wrap;word-break:break-word}
.tcmsg.bot{align-self:flex-start;background:var(--bg-card);color:var(--text-dim);border:1px solid var(--line)}
.tcmsg.you{align-self:flex-end;background:var(--amber);color:var(--on-accent);font-weight:600}
#tchatchips{padding:11px;display:flex;flex-direction:column;gap:7px;overflow:auto}
.tc-opt,.tc-sug{border:none;border-radius:12px;padding:9px 13px;font-family:inherit;font-size:12.5px;
  font-weight:600;cursor:pointer;text-align:left;box-shadow:0 2px 5px rgba(0,0,0,.18);transition:filter .12s;
  display:flex;align-items:center;gap:8px}
.tc-opt{background:var(--amber);color:var(--on-accent)}
.tc-opt b{opacity:.8;font-weight:800;margin-right:6px}
.tc-sug{background:var(--bg-card);color:var(--text);border:1.5px solid var(--amber)}
.tc-txt{flex:1}
.cpie{flex:0 0 auto;width:16px;height:16px;border-radius:50%;box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
.cpct{flex:0 0 auto;font-size:11px;font-weight:800;color:#2ea043;min-width:30px}
.tc-opt:hover,.tc-sug:hover{filter:brightness(1.1)}
.tc-opt:disabled,.tc-sug:disabled{opacity:.5;cursor:default}
.tc-opt.sel{box-shadow:0 0 0 2px var(--bg-raise),0 0 0 4px var(--amber)}
.tc-empty{color:var(--text-dim);font-size:12px;font-style:italic;padding:4px 2px}
#tchatrow{display:flex;gap:6px;padding:10px 11px;border-top:1px solid var(--line);background:var(--bg-card)}
#tchatinput{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:9px;
  padding:8px 11px;font-family:inherit;font-size:13px}
#tchatsend{background:var(--amber);color:var(--on-accent);border:none;border-radius:9px;width:40px;
  font-size:15px;cursor:pointer}
.happrove:disabled,.hdeny:disabled{opacity:.4;cursor:default}
</style>
</head>
<body>
<div id="hbanner" onclick="openHancock()"><span class="hbell">🔔</span> <b id="hbannern">0</b> need your signature — <u>review &amp; approve</u></div>
<div id="htray">
  <div id="htrayhead"><b>HANCOCK</b> · pending approvals <span id="htrayclose" onclick="closeHancock()">✕</span></div>
  <div id="htoast"></div>
  <div id="htraylist"></div>
</div>
<div id="setmodal" onclick="if(event.target===this)closeSettings()">
  <div id="setcard">
    <div id="sethead"><b>⚙ MODEL ROUTING</b><span onclick="closeSettings()" class="setx">✕</span></div>
    <p class="setnote">Route cheap, high-volume tasks to a <b>self-hosted, fixed-cost</b> NIM to spare the Claude subscription. If NIM is unset or unreachable, emux falls back to <code>claude -p</code> automatically — nothing breaks. <b>Do not point this at a metered cloud API.</b></p>
    <div class="setsec"><h4>Per-task backend</h4><div id="setroutes"></div></div>
    <div class="setsec"><h4>NIM endpoint <span class="setpill">self-hosted / local only</span></h4>
      <label>Base URL <input id="nimurl" placeholder="http://localhost:8000/v1" autocomplete="off" spellcheck="false"></label>
      <label>Model <input id="nimmodel" placeholder="meta/llama-3.1-8b-instruct" autocomplete="off" spellcheck="false"></label>
      <label>API key <span class="setdim">(only if your local NIM needs one)</span> <input id="nimkey" type="password" placeholder="(usually blank for local)" autocomplete="off"></label>
      <div class="setrow"><button id="nimtest" class="act" onclick="testNim()">Test connection</button><span id="nimtestout"></span></div>
    </div>
    <div class="setfoot"><button id="setsave" onclick="saveSettings()">Save</button><span id="setsaveout"></span></div>
  </div>
</div>
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
    <button id="feedbtn" class="act" title="live fleet activity">◫ FEED</button>
    <button id="hbtn" class="act" title="Hancock approvals" onclick="openHancock()">⧉ HANCOCK<span id="hbadge" style="display:none">0</span></button>
    <button id="refreshbtn" class="act">↻ refresh</button>
    <button id="setbtn" class="act" title="model routing settings" onclick="openSettings()">⚙ SETTINGS</button>
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
<aside id="feed" class="open">
  <div id="feedhead"><span>◫ fleet</span><span id="feedcount"></span>
    <button id="feedclose" title="hide">›</button></div>
  <div id="feedlist"></div>
</aside>
<div id="modal">
  <div id="modalback"></div>
  <div id="modalpanel">
    <div id="modalhead">
      <span class="dot live"></span>
      <span class="nm" id="modalname"></span>
      <span class="ag" id="modalagent"></span>
      <span id="modalthink"><span class="tdots"><i></i><i></i><i></i></span>thinking <b>0s</b></span>
      <span class="st" id="modalstatus">live</span>
      <button id="modaliterm" title="open this session in a new iTerm2 window (attached tmux)">⧉ iTerm2</button>
      <button id="modalclose">✕ close</button>
    </div>
    <div id="modaljudge"></div>
    <div id="modaldigest">
      <div class="dghead"><span>◆ the gist</span><button id="dgrefresh" title="re-read">↻</button></div>
      <div class="dgtext"></div>
      <div class="dgsugg"></div>
    </div>
    <div id="modalscreen"></div>
    <div id="tchat" class="collapsed">
      <button id="tchatlauncher" onclick="tchatToggle()">💬<span id="tchatbadge"></span></button>
      <div id="tchatpanel">
        <div id="tchathead"><span>💬 reply to <b id="tchatsess"></b></span><button id="tchatmin" onclick="tchatToggle()" title="minimize">▾</button></div>
        <div id="tchatlog"></div>
        <div id="tchatchips"></div>
        <div id="tchatrow"><input id="tchatinput" placeholder="choose one, or type your reply…" autocomplete="off" spellcheck="false"><button id="tchatsend" onclick="tchatSend()">➤</button></div>
      </div>
    </div>
    <div id="modalopts"></div>
    <div id="modalchips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
    </div>
    <div id="modalpending"></div>
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
      <div class="askrow">
        <input id="newintent" placeholder="say what you want to do…" autocomplete="off">
        <button id="newsuggest">✦ go</button>
      </div>

      <!-- THE ANSWER: one readable card. The tree below is the machinery, hidden. -->
      <div id="result">
        <div id="rverb"></div>
        <div id="rwhat"></div>
        <div id="rwhere"></div>
        <div id="rflags"></div>
        <details id="rpeek"><summary>what's in it</summary><pre id="peek2"></pre></details>
        <div id="rwhy"></div>
      </div>

      <div id="tree">
      <div class="step" id="s-host">
        <div class="steph"><span class="num">1</span><span class="lbl">machine</span>
          <span class="sub">everything below depends on this</span>
          <span class="why" id="why-host"></span></div>
        <div class="chips" id="hostchips"></div>
      </div>

      <div class="step locked" id="s-dir">
        <div class="steph"><span class="num">2</span><span class="lbl">what's on it</span>
          <span class="sub" id="dirsub">pick a machine first</span>
          <span class="why" id="why-dir"></span></div>
        <div class="lanes">
          <span class="lane" data-lane="resume">↺ resume something running <b id="nrun">0</b></span>
          <span class="lane" data-lane="new">+ start fresh in a directory <b id="ndir">0</b></span>
        </div>
        <input id="dirfilter" placeholder="filter…" autocomplete="off">
        <div class="dirchoices" id="dirchoices"></div>
        <div id="peekwrap">
          <div id="peekhead">what's in it right now <span id="peekflags"></span></div>
          <pre id="peek"></pre>
        </div>
      </div>

      <div class="step locked" id="s-cmd">
        <div class="steph"><span class="num">3</span><span class="lbl">what runs there</span>
          <span class="sub" id="cmdsub"></span></div>
        <div class="chips" id="cmdchips"></div>
        <input id="newcmd" placeholder="custom command… (empty = plain shell)" autocomplete="off">
      </div>

      <div class="step locked" id="s-name">
        <div class="steph"><span class="num">4</span><span class="lbl" id="namelbl">name it</span>
          <span class="sub" id="namesub"></span></div>
        <input id="newname" placeholder="session name" autocomplete="off">
        <label class="chk"><input type="checkbox" id="newgui" checked>
          <span id="guilbl">open an iTerm2 window attached to it</span></label>
        <div id="guinote"></div>
      </div>
      </div><!-- /tree -->
      <div id="newerr"></div>
    </div>
    <div id="newfoot">
      <button id="newchange" class="linkish">change…</button>
      <button id="newcreate" disabled>CREATE SESSION</button>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const SVGNS="http://www.w3.org/2000/svg";
let mode="grid", current=null, grid=[], chatTimer=null, gridTimer=null, screenEl=null;
let filterStr="", flashOn=false, activeTag="";
const metaCache={};   // last-live summary/agent per session name, so a GONE session still shows what it last was
function cacheMeta(){grid.forEach(s=>{if(s.live&&s.summary)metaCache[s.name]={summary:s.summary,agent:s.agent,state:s.state,ts:Date.now()};});}
function lastSummary(s){return s.summary||(metaCache[s.name]&&metaCache[s.name].summary)||"";}
function goneAge(s){const m=metaCache[s.name];if(!m)return"";const sec=Math.round((Date.now()-m.ts)/1000);return sec<60?sec+"s":Math.round(sec/60)+"m";}
let activeCompany=localStorage.getItem("emux_company")||"";   // restore the skin you were in
let flowSig=null, flowPre={}, flowBox={};   // flow view: rebuild only on topology change, else update panes in place
const BASE_TAB={grid:"GRID",groups:"GROUPS",activity:"ACTIVITY",flow:"FLOW"};

// ---- deep links: the view, filters, and open session live in the URL, so any
// state is bookmarkable/shareable and survives reload/back-forward. ----
let urlBooting=false;   // suppress syncURL while we APPLY a url (avoid loops)
function syncURL(){
  if(urlBooting)return;
  const p=new URLSearchParams();
  if(mode&&mode!=="grid")p.set("view",mode);
  if(activeCompany)p.set("company",activeCompany);
  if(activeTag)p.set("tag",activeTag);
  if(filterStr)p.set("q",filterStr);
  if(modalSession)p.set("session",modalSession.name);
  const h=p.toString();
  if((location.hash.slice(1))!==h)
    history.replaceState(null,"",h?("#"+h):location.pathname+location.search);
}
function applyURL(){
  urlBooting=true;
  const p=new URLSearchParams(location.hash.slice(1));
  activeCompany=p.get("company")||"";
  activeTag=p.get("tag")||"";
  filterStr=(p.get("q")||"").toLowerCase();
  const f=$("#filter");if(f)f.value=p.get("q")||"";
  skinForCompany();
  setMode(p.get("view")||localStorage.getItem("emux_view")||"grid");
  renderTagbar();renderSidebar();
  urlBooting=false;
  // deep-link to an open session modal — once the grid is loaded
  const sess=p.get("session");
  if(sess){const open=()=>{const s=grid.find(x=>x.name===sess);
    if(s)openModal(s);else if(!grid.length)setTimeout(open,300);};open();}
  else if(!modalSession){/* leave modal closed */}
}
window.addEventListener("hashchange",()=>{if(!urlBooting)applyURL();});

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
// does a session directly match the active filter (name / tag / company)?
function baseMatch(s){
  // filter now searches CONTENT too — name, target, description, and the live/last
  // gist — so typing "cloudflare" finds the session whose gist mentions it.
  const hay=filterStr?((s.name||"")+" "+(s.session||"")+" "+(s.description||"")+" "
    +lastSummary(s)+" "+(s.path||s.cwd||"")+" "+(s.tags||[]).join(" ")).toLowerCase():"";
  return (!filterStr||hay.includes(filterStr))
  &&(!activeTag||(s.tags||[]).includes(activeTag))
  &&(!activeCompany||(s.company||{}).company===activeCompany);}

// TERMINALS grouped into connected components by the `manages` edges (undirected).
// Returns name -> component id. A "group of terminals" is one component.
function components(){
  const parent={};grid.forEach(s=>parent[s.name]=s.name);
  const find=x=>{while(parent[x]!==x){parent[x]=parent[parent[x]];x=parent[x];}return x;};
  const uni=(a,b)=>{if(parent[a]!==undefined&&parent[b]!==undefined)parent[find(a)]=find(b);};
  const byKey={};grid.forEach(s=>{byKey[s.name]=s.name;if(!(s.session in byKey))byKey[s.session]=s.name;});
  grid.forEach(s=>(s.manages||[]).forEach(t=>{const tn=byKey[t];if(tn)uni(s.name,tn);}));
  const comp={};grid.forEach(s=>comp[s.name]=find(s.name));return comp;
}

// shown: the filter, but CONNECTION-AWARE. If any terminal in a group matches,
// the WHOLE group shows — even members that aren't in the filtered set — so a
// manager and everything it manages stay together on screen.
function shown(){
  if(!(filterStr||activeTag||activeCompany))return grid.slice();
  const comp=components(),hot=new Set();
  grid.forEach(s=>{if(baseMatch(s))hot.add(comp[s.name]);});
  return grid.filter(s=>baseMatch(s)||hot.has(comp[s.name]));
}

// ---- SKINS: the whole UI recolors to what you're working on ----
// default = Eidos light; the Eidos pill = Eidos dark; the Greenmark pill = the
// Greenmark Waste forest-green brand. Each theme just remaps the 12 CSS vars.
const THEMES={
  "eidos-light":{"--bg":"#f0ebe4","--bg-raise":"#e9e3db","--bg-card":"#e4ded6",
    "--amber":"#8e6129","--amber-dim":"#a9853f","--amber-faint":"#d8cdba",
    "--text":"#1e1a17","--text-dim":"#6b6159","--live":"#4a6a3a","--stale":"#ab5036",
    "--line":"#cabfae","--user":"#8e6129","--on-accent":"#f5efe6"},
  "eidos-dark":{"--bg":"#15110f","--bg-raise":"#1a1613","--bg-card":"#1e1a17",
    "--amber":"#c4935a","--amber-dim":"#9a6d35","--amber-faint":"#3a2f22",
    "--text":"#dcd5cb","--text-dim":"#8b8179","--live":"#7a8c72","--stale":"#c4694f",
    "--line":"#332a20","--user":"#d4a870","--on-accent":"#1a1207"},
  // Greenmark Waste — its actual brand: forest-green ink (#2d4a3e) on warm cream,
  // gold as the secondary pop. Light, per the brand's own palette.json.
  "greenmark":{"--bg":"#f5f0e8","--bg-raise":"#efe8da","--bg-card":"#e8e0d0",
    "--amber":"#2d4a3e","--amber-dim":"#3d6b56","--amber-faint":"#d7e0d3",
    "--text":"#1f2937","--text-dim":"#6b7280","--live":"#3d6b56","--stale":"#b3261e",
    "--line":"#d3ccbb","--user":"#2d4a3e","--on-accent":"#f5f0e8"},
  // Reeves — the PERSONAL context. A cool slate/navy skin, deliberately unlike the
  // Eidos amber and Greenmark green, so switching to Reeves signals "personal mode".
  "reeves":{"--bg":"#eef1f6","--bg-raise":"#e6ebf2","--bg-card":"#dee4ee",
    "--amber":"#3b5ba5","--amber-dim":"#5a76bd","--amber-faint":"#ccd6e8",
    "--text":"#182030","--text-dim":"#5c6678","--live":"#3d7a5a","--stale":"#b3503a",
    "--line":"#c5cddd","--user":"#3b5ba5","--on-accent":"#f4f7fc"},
};
// company key → skin. Anything unmapped falls back to the default light Eidos.
const CO_THEME={"":"eidos-light","eidos":"eidos-dark","greenmark":"greenmark","reeves":"reeves"};
function applyTheme(name){
  const t=THEMES[name]||THEMES["eidos-light"];
  const r=document.documentElement;
  for(const k in t) r.style.setProperty(k,t[k]);
  r.dataset.theme=name;
  localStorage.setItem("emux_theme",name);
}
function skinForCompany(){applyTheme(CO_THEME[activeCompany]||"eidos-light");}

function applyFilters(){localStorage.setItem("emux_company",activeCompany);
  skinForCompany();renderTagbar();renderSidebar();if(mode!=="chat")render();syncURL();}

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
  syncURL();
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
    grid=r.sessions;cacheMeta();
    $("#status").textContent=grid.filter(s=>s.live).length+" live · polling";$("#status").className="";
    updateChrome();renderTagbar();renderSidebar();
    if(mode!=="chat")render();
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function renderSidebar(){
  const box=$("#sessions");box.innerHTML="";
  shown().forEach(s=>{
    const d=document.createElement("div");
    d.className="card"+(current&&current.name===s.name?" active":"")+(needsYou(s)?" needy":"")+(s.live?"":" gone");
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

const STLABEL={running:"run",idle:"idle",error:"err",asking:"asks you",waiting_human:"needs you",dead:"gone"};
function statePip(s){const st=s.live?(s.state||"idle"):"dead";
  return '<span class="spip st-'+st+'">'+(STLABEL[st]||st)+'</span>';}
// a session waiting on YOU — a formal gate, it asked a question, OR its gist reads
// like it's parked on a human action (authorize / approve / on your desk / until you…).
// deliberately CONSERVATIVE — only phrases that mean "parked, waiting on the human",
// which don't show up in ordinary working output. (Broad words like verify/paste/login
// false-positived on normal agent chatter.)
const _NEEDY_GIST=/(on your desk|await(ing)? your|waiting (on|for) (you|your)|needs? your (approval|sign|decision|input|go)|your (approval|sign-?off|authoriz)|until you (authoriz|approv|confirm|sign|respond))/i;
function needsYou(s){return s.live&&(s.needs_human||s.state==="asking"||s.state==="waiting_human"
  ||_NEEDY_GIST.test(lastSummary(s)));}
// the live indicator: heartbeat EKG when running, a QUESTION MARK when it's
// asking you something, else a colored dot by state.
const EKG='<svg class="ekg" viewBox="0 0 44 16" preserveAspectRatio="none">'
  +'<polyline class="base" points="0,8 15,8 18,8 20,3 23,13 26,8 44,8"/>'
  +'<polyline class="beat" points="0,8 15,8 18,8 20,3 23,13 26,8 44,8"/></svg>';
function liveDot(s){
  if(!s.live)return '<span class="dot stale"></span>';
  if(s.state==="running")return EKG;
  if(s.state==="asking")return '<span class="qmark">?</span>';   // it's asking YOU
  const cls=s.state==="error"?"stale":(s.state==="waiting_human"?"warn":"live");
  return '<span class="dot '+cls+'"></span>';
}
// the thin summary rail — a super-cheap always-on "what's happening now", with a
// hover overlay that shows it in full.
function railHTML(s){
  const sum=s.summary||(s.live?"…":"gone");
  const i=sum.indexOf(" — ");
  const verb=i>0?sum.slice(0,i):sum, rest=i>0?sum.slice(i+3):"";
  return '<div class="rail st-'+(s.state||"idle")+'"><span class="rv">'+esc(verb)+'</span>'
    +(rest?' <span class="rt">'+esc(rest)+'</span>':'')
    +'<div class="rfull"><b>'+esc(verb)+'</b>'+(rest?' — '+esc(rest):'')+'</div></div>';
}

function makeTile(s){
  const t=document.createElement("div");
  t.className="tile"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(s.live?"":" dead");
  const h=document.createElement("header");
  const att=s.attached?'<span class="att">●</span>':"";
  const ag=s.agent||{glyph:"",label:""};
  const agbadge=(s.live&&ag.label&&ag.label!=="—")?'<span class="agentbadge">'+agentHTML(s)+'</span>':"";
  h.innerHTML='<span class="lind">'+liveDot(s)+'</span><span class="nm">'+s.name+att+'</span>'
    +companyHTML(s)+agbadge
    +'<span class="age '+(s.live?ageClass(s.last_change_age):"t-old")+'">'+(s.live?ageLabel(s.last_change_age):"gone")+'</span>'
    +statePip(s);
  if(needsYou(s)){const b=document.createElement("div");b.className="needbadge";b.textContent="⚠ NEEDS YOU";t.appendChild(b);}
  const p=document.createElement("pre");
  if(s.live&&s.content.trim()){
    p.textContent=s.content.replace(/\s+$/,"").split("\n").slice(-14).join("\n");
  }else if(!s.live&&lastSummary(s)){
    // GONE, but we remember what it was doing — show it instead of a blank ghost
    p.className="empty gonecache";
    p.textContent="⏹ session ended"+(goneAge(s)?" · "+goneAge(s)+" ago":"")+"\nlast: "+lastSummary(s);
  }else{
    p.className="empty";p.textContent=s.live?"(blank pane)":"tmux session gone";
  }
  const rail=document.createElement("div");rail.className="railwrap";rail.innerHTML=railHTML(s);
  t.appendChild(h);t.appendChild(rail.firstChild);t.appendChild(p);
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
    d.className="fbox"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(s.live?"":" dead");
    const ag=d.querySelector(".ag");if(ag)ag.innerHTML=agentHTML(s);
    const sp=d.querySelector(".spip");if(sp){const st=s.live?(s.state||"idle"):"dead";
      sp.className="spip st-"+st;sp.textContent=STLABEL[st]||st;}
    const li=d.querySelector(".lind");if(li)li.innerHTML=liveDot(s);   // idle↔run swaps dot↔heartbeat
    const rl=d.querySelector(".rail");if(rl)rl.outerHTML=railHTML(s);   // refresh the summary rail
    const txt=paneText(s);
    if(txt){pre.className="";if(pre.textContent!==txt)pre.textContent=txt;}
    else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
  });
}

function renderFlow(){
  const v=$("#views");
  if(!grid.length){flowSig=null;v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';return;}
  // work on the connection-aware filtered set: whole chains stay together
  const G=shown();
  if(!G.length){flowSig=null;v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  // resolve manage targets by registry name OR underlying tmux session name
  const byKey={};G.forEach(s=>{byKey[s.name]=s;if(!(s.session in byKey))byKey[s.session]=s;});
  const children=new Map(),indeg=new Map();
  G.forEach(s=>indeg.set(s.name,0));
  const edges=[];
  G.forEach(s=>(s.manages||[]).forEach(t=>{
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
  let q=[...level.keys()],guard=0,MAX=G.length*G.length+20;
  while(q.length&&guard++<MAX){
    const n=q.shift(),l=level.get(n);
    (children.get(n)||[]).forEach(c=>{
      if(l+1<G.length&&(!level.has(c)||level.get(c)<l+1)){level.set(c,l+1);q.push(c);}
    });
  }
  [...connected].forEach(n=>{if(!level.has(n))level.set(n,0);});
  const maxLvl=Math.max(0,...[...level.values()]);
  const unconnected=G.filter(s=>!connected.has(s.name));

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
  marker.appendChild(el("path",{d:"M0,0 L10,5 L0,10 z",fill:"var(--amber)"}));
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
    d.className="fbox"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(s.live?"":" dead");
    d.style.left=p.x+"px";d.style.top=p.y+"px";d.style.width=BW+"px";
    const title='<div class="ftitle"><span class="lind">'+liveDot(s)+'</span>'
      +'<span class="nm">'+s.name+'</span>'+statePip(s)+'<span class="ag">'+agentHTML(s)+'</span></div>';
    const pre=document.createElement("pre");
    const txt=paneText(s);
    if(txt)pre.textContent=txt;else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
    d.innerHTML=title+railHTML(s);d.appendChild(pre);
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
$("#filter").addEventListener("input",e=>{filterStr=e.target.value.toLowerCase();renderSidebar();if(mode!=="chat")render();syncURL();});
// ---------- zoom-in steer modal ----------
let modalSession=null, modalTimer=null;
let digestErr=false, digestRetries=0;   // The Gist: recover from a failed summarize when the pane changes, capped at 10
function openModal(s){
  modalSession=s;
  digestErr=false;digestRetries=0;
  $("#modalname").textContent=s.name;
  $("#modalagent").innerHTML=agentHTML(s);
  $("#modalstatus").textContent="connecting…";$("#modalstatus").style.color="";
  const sc=$("#modalscreen");sc.textContent="";sc.dataset.last="";
  $("#modaldigest").className="";$("#modaldigest .dgtext").textContent="";$("#modaldigest .dgsugg").innerHTML="";
  setPending("");$("#modalthink").className="";
  tOpts=[];tSugg=[];tchatCollapsed=false;tLoggedDigest="";$("#tchatlog").innerHTML="";
  $("#tchatsess").textContent=s.name;$("#tchat").className="collapsed";renderTChat();
  $("#modal").classList.add("open");
  modalRefresh();clearInterval(modalTimer);modalTimer=setInterval(modalRefresh,1200);
  loadDigest();                                  // the gist + suggested replies, up front
  syncURL();                                      // deep-link the open session
  setTimeout(()=>$("#modalinput").focus(),40);
}
function closeModal(){
  $("#modal").classList.remove("open");
  clearInterval(modalTimer);modalTimer=null;modalSession=null;
  syncURL();                                      // drop the session from the URL
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
        // The Gist failed but the web changed — ok to try to recover, capped at 10
        if(digestErr&&digestRetries<10){digestRetries++;loadDigest();}
      }
      renderOptions(r.options);   // a menu on screen → clickable bubbles
      updateThinking(r.thinking); // movement + timer while it generates
      // pending send has landed once the pane echoes it → clear the holding bubble
      if(pendingText&&r.content&&r.content.indexOf(pendingText.trim().slice(0,40))>=0)setPending("");
    }else{$("#modalstatus").textContent=r.error||"error";$("#modalstatus").style.color="var(--stale)";}
  }catch(e){$("#modalstatus").textContent="unreachable";$("#modalstatus").style.color="var(--stale)";}
  modalJudge();
}
// the gist: a reader's-digest of the session + clickable suggested replies, so
// you don't have to read a wall of text and invent a response.
async function loadDigest(){
  if(!modalSession)return;
  const el=$("#modaldigest");el.className="on loading";
  $("#modaldigest .dgtext").textContent="reading the session…";
  $("#modaldigest .dgsugg").innerHTML="";
  const sess=modalSession.session;
  const r=await api("/api/reply",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:sess})});
  if(!modalSession||modalSession.session!==sess)return;   // modal changed while thinking
  el.className="on";
  if(!r.ok){
    digestErr=true;   // stuck — modalRefresh will retry (≤10x) when the pane content next changes
    const more=digestRetries<10?" · will retry when the session moves":"";
    $("#modaldigest .dgtext").textContent="(couldn't summarize — "+(r.error||"")+")"+more;return;}
  digestErr=false;digestRetries=0;   // recovered
  $("#modaldigest .dgtext").textContent=r.digest||"(nothing notable)";
  if(r.digest&&r.digest!==tLoggedDigest){tchatLog("bot",r.digest);tLoggedDigest=r.digest;}  // the gist opens the chat
  setSuggestions(r.suggestions||[]);   // gist replies become chips (with confidence pies)
}
// turn an on-screen numbered menu into clickable bubbles that answer it for you.
// The choices for a terminal live in a floating chat LOCAL to that terminal:
// on-screen menu options + the gist's suggested replies become quick chips, and
// there's a box to type your own — choose one, or chat. (NCDMV-style.)
let tOpts=[], tSugg=[], tchatCollapsed=false, tLoggedDigest="";
// a green pie/wedge filled to the confidence %, shown on each suggested reply so you
// can see at a glance how strong emux thinks that choice is.
function pieHTML(pct){const p=Math.round(pct);
  return '<span class="cpie" title="'+p+'% confidence" style="background:conic-gradient(#2ea043 '+p+'%,rgba(127,127,127,.26) 0)"></span>'
    +'<span class="cpct">'+p+'%</span>';}
function tchatLog(who,text){const log=$("#tchatlog");if(!log||!text)return;
  const b=document.createElement("div");b.className="tcmsg "+who;b.textContent=text;
  log.appendChild(b);log.scrollTop=log.scrollHeight;}
function renderOptions(opts){ tOpts=opts||[]; renderTChat(); }   // a menu on screen → chips
function setSuggestions(sugg){ tSugg=sugg||[]; renderTChat(); }  // gist replies → chips
async function sendOption(target){
  // walk the ❯ cursor to the target then confirm — works for any cursor menu
  // (Claude/Codex), no reliance on digit-select.
  const cur=((tOpts.find(o=>o.selected))||tOpts[0]||{n:target}).n;
  const send=k=>api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:modalSession.session,keys:k,literal:false,enter:false})});
  const arrow=target>=cur?"Down":"Up";
  for(let i=0;i<Math.abs(target-cur);i++){await send(arrow);await new Promise(r=>setTimeout(r,90));}
  await send("Enter");
  setTimeout(()=>{modalRefresh();loadDigest();},600);
}
function renderTChat(){
  const chips=$("#tchatchips"); if(!chips)return;
  const parts=[];
  tOpts.forEach(o=>parts.push('<button class="tc-opt'+(o.selected?" sel":"")+'" data-n="'+o.n+'"><b>'+o.n+'</b> '+esc(o.label)+'</button>'));
  tSugg.forEach((s,i)=>{
    const c=Math.max(0,Math.min(100,s.confidence==null?50:s.confidence));
    parts.push('<button class="tc-sug" data-i="'+i+'">'+pieHTML(c)+'<span class="tc-txt">'+esc(s.text||"")+'</span></button>');
  });
  chips.innerHTML=parts.join("")||'<div class="tc-empty">no suggestions right now — type your reply below</div>';
  const n=tOpts.length+tSugg.length;
  const badge=$("#tchatbadge");badge.textContent=n||"";badge.style.display=n?"inline-block":"none";
  chips.querySelectorAll(".tc-opt").forEach(b=>b.onclick=()=>{
    chips.querySelectorAll("button").forEach(x=>x.disabled=true);sendOption(+b.dataset.n);});
  chips.querySelectorAll(".tc-sug").forEach(b=>b.onclick=()=>{
    const t=(tSugg[+b.dataset.i]||{}).text||"";if(!t)return;
    setPending(t);tchatLog("you",t);modalKeys(t,true,true);
    tSugg=[];renderTChat();setTimeout(()=>{modalRefresh();loadDigest();},1500);});
  // when a real choice lands, float the chat open (unless the user minimized it)
  if(n&&!tchatCollapsed)$("#tchat").className="";
}
function tchatToggle(){
  tchatCollapsed=!tchatCollapsed;
  $("#tchat").className=tchatCollapsed?"collapsed":"";
  if(!tchatCollapsed)setTimeout(()=>$("#tchatinput").focus(),50);
}
function tchatSend(){
  const i=$("#tchatinput");const t=i.value.trim();if(!t)return;
  i.value="";setPending(t);tchatLog("you",t);
  modalKeys(t,true,true).then(ok=>{if(!ok){if(!i.value)i.value=t;setPending("");}});
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
  setPending(text);                             // …and hold it above the box until it lands
  modalKeys(text,true,true).then(ok=>{
    if(!ok){                                    // …but if it didn't land, give the draft back
      if(!i.value)i.value=text;setPending("");
      const st=$("#modalstatus");st.textContent="send failed — draft kept";st.style.color="var(--stale)";
    }
  });
}
// a message received by emux but not yet echoed by the (maybe ssh-laggy) session
let pendingText="";
function setPending(txt){pendingText=txt;const el=$("#modalpending");
  if(txt){el.className="on";el.innerHTML='<span class="plabel">received · sending</span><span class="ptext">'+esc(txt)+'</span>';}
  else{el.className="";el.innerHTML="";}}
// thinking indicator: movement + how long the agent has been generating
function updateThinking(t){const el=$("#modalthink");
  if(t&&t.active){el.className="on";if(t.for)el.querySelector("b").textContent=t.for;}
  else el.className="";}
$("#modalsend").onclick=modalSubmit;
$("#modalinput").addEventListener("keydown",e=>{if(e.key==="Enter")modalSubmit();});
$("#tchatinput").addEventListener("keydown",e=>{if(e.key==="Enter")tchatSend();});
$("#modaliterm").onclick=async()=>{
  if(!modalSession)return;
  const b=$("#modaliterm");const was=b.textContent;b.disabled=true;b.textContent="opening…";
  const r=await api("/api/head",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:modalSession.session})});
  b.textContent=r.ok?"⧉ opened":("✕ "+(r.error||"failed"));
  setTimeout(()=>{b.textContent=was;b.disabled=false;},r.ok?1400:3000);
};
// ---- new session: a CASCADE. Each choice constrains the ones below it, so
// changing an upper choice INVALIDATES everything under it and re-derives. ----
// The tree: machine → (resume a running session | start new in a dir) → command → name.
// Its nodes are enumerated from REALITY; the LLM only classifies your sentence into
// a path through it and pre-fills the picks (marked ✦), which you can override.
const NS={hosts:[],host:"",dirs:[],running:[],lane:"new",session:"",cwd:"",cmd:"",name:"",
          aiHost:"",aiPick:"",whyHost:"",whyDir:"",loading:false,manual:false};

function nsReset(level){        // wipe every choice BELOW `level` (1=machine, 2=target)
  if(level<2){NS.dirs=[];NS.running=[];NS.session="";NS.cwd="";NS.whyDir="";NS.aiPick="";}
  if(level<3){NS.cmd="";}
  if(level<4){NS.name="";}
}
const isResume=()=>NS.lane==="resume";
const hasTarget=()=>isResume()?!!NS.session:!!NS.cwd;

function nsRender(){
  // ── node 1: machine
  $("#hostchips").innerHTML=NS.hosts.map(h=>
    '<span class="hchip'+(h===NS.host?" on":"")+(h===NS.aiHost&&h!==NS.host?" ai":"")+'" data-h="'+h+'">'+h+'</span>').join("");
  $("#hostchips").querySelectorAll(".hchip").forEach(el=>el.onclick=()=>pickHost(el.dataset.h));
  $("#why-host").textContent=NS.whyHost?("✦ "+NS.whyHost):"";
  $("#s-host").classList.toggle("done",!!NS.host);

  // ── node 2: what's ON that machine — resume something running, or start fresh
  const dl=$("#s-dir");
  dl.classList.toggle("locked",!NS.host);
  dl.classList.toggle("done",hasTarget());
  $("#nrun").textContent=NS.running.length;
  $("#ndir").textContent=NS.dirs.length;
  $("#dirsub").textContent=!NS.host?"pick a machine first"
    :NS.loading?("looking at "+NS.host+"…"):("on "+NS.host);
  $("#s-dir").querySelectorAll(".lane").forEach(el=>{
    el.classList.toggle("on",el.dataset.lane===NS.lane);
    el.onclick=()=>{NS.lane=el.dataset.lane;NS.session="";NS.cwd="";nsReset(2);
                    $("#dirfilter").value="";$("#newname").value="";nsRender();};
  });
  const f=($("#dirfilter").value||"").toLowerCase();
  let html="";
  if(isResume()){
    const rows=NS.running.filter(s=>!f||(s.name+" "+s.path).toLowerCase().includes(f));
    html=rows.map(s=>
      '<div class="dirrow'+(s.name===NS.session?" on":"")+'" data-s="'+s.name+'">'+s.name
      +'<span class="meta">'+ago(s.age_sec)+' · '+s.path+(s.attached?' · attached':'')
      +(s.adopted?' · in emux':'')+'</span>'
      +(s.name===NS.aiPick?'<span class="ai">✦</span>':'')+'</div>').join("");
  }else{
    const rows=NS.dirs.filter(d=>!f||d.toLowerCase().includes(f));
    html=rows.map(d=>
      '<div class="dirrow'+(d===NS.cwd?" on":"")+'" data-d="'+d+'">'+d
      +(d===NS.aiPick?'<span class="ai">✦</span>':"")+'</div>').join("");
  }
  $("#dirchoices").innerHTML=html||'<div class="dirrow" style="cursor:default;opacity:.5">— nothing here —</div>';
  $("#dirchoices").querySelectorAll(".dirrow[data-d]").forEach(el=>el.onclick=()=>pickDir(el.dataset.d));
  $("#dirchoices").querySelectorAll(".dirrow[data-s]").forEach(el=>el.onclick=()=>pickSession(el.dataset.s));
  $("#why-dir").textContent=NS.whyDir?((NS.aiPick?"✦ ":"")+NS.whyDir):"";

  // ── node 3: what runs there — only a NEW session needs one; a resumed one is already running
  const cs=$("#s-cmd");
  cs.classList.toggle("locked",!hasTarget());
  cs.classList.toggle("done",hasTarget());
  cs.style.display=isResume()?"none":"";
  const presets=[["","plain shell"],["claude","claude"],["claude --dangerously-skip-permissions","claude (skip perms)"]];
  $("#cmdchips").innerHTML=presets.map(([v,l])=>
    '<span class="hchip'+(NS.cmd===v?" on":"")+'" data-c="'+v+'">'+l+'</span>').join("");
  $("#cmdchips").querySelectorAll(".hchip").forEach(el=>el.onclick=()=>{NS.cmd=el.dataset.c;$("#newcmd").value=NS.cmd;nsRender();});

  // ── the preview: only meaningful when RESUMING (a new session has no state yet)
  $("#peekwrap").classList.toggle("show",isResume()&&!!NS.session);

  // ── node 4: naming means different things. New: christen a thing that doesn't
  //    exist. Resume: it already HAS a name — you're choosing its emux alias.
  const ns=$("#s-name");
  ns.classList.toggle("locked",!hasTarget());
  ns.classList.toggle("done",!!NS.name);
  $("#namelbl").textContent=isResume()?"adopt into emux as":"name it";
  $("#namesub").textContent=isResume()
    ? ("it is already called “"+NS.session+"” on "+NS.host)
    : "";
  $("#guilbl").textContent=isResume()
    ? "also open a terminal attached to it"
    : "open an iTerm2 window attached to it";
  // attaching to a LIVE session is not free — say so
  $("#guinote").textContent=(isResume()&&$("#newgui").checked)
    ? "⚠ it is already running; the window opens WITHOUT taking focus so it can't swallow your keystrokes"
    : "";

  // ── the answer card: one line you can actually read
  const ready=!!(NS.host&&hasTarget()&&NS.name);
  $("#result").classList.toggle("show",ready&&!NS.manual);
  if(ready){
    $("#rverb").textContent=isResume()?"↺ resume":"+ new session";
    $("#rwhat").textContent=isResume()?NS.session:NS.name;
    $("#rwhere").textContent=(isResume()?"on ":"on ")+NS.host+" · "+NS.cwd
      +(isResume()?"":(NS.cmd?" · "+NS.cmd:" · shell"));
    $("#rwhy").textContent=NS.whyDir||"";
    $("#rpeek").hidden=!isResume();
  }
  // the tree is the machinery — only on demand. Empty state = just the question.
  $("#tree").classList.toggle("show",NS.manual);
  $("#newchange").textContent=NS.manual?"← done"
    :(ready?"change…":"set it up by hand");

  const b=$("#newcreate");
  b.disabled=!ready;
  b.textContent=isResume()?"RESUME":"CREATE";
}
function ago(s){return s<3600?Math.floor(s/60)+"m":(s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d");}

async function pickHost(h){       // node 1 changed ⇒ everything under it is invalid
  NS.host=h;NS.whyHost="";nsReset(1);
  $("#newcmd").value="";$("#newname").value="";$("#dirfilter").value="";
  NS.loading=true;nsRender();
  const r=await api("/api/dirs?host="+encodeURIComponent(h));
  NS.loading=false;
  NS.dirs=(r.ok&&r.dirs)?r.dirs:[];
  NS.running=(r.ok&&r.running)?r.running:[];
  nsRender();
}
function pickDir(d){NS.cwd=d;NS.session="";if(!NS.name)NS.name=autoName(d);
  $("#newname").value=NS.name;nsRender();}
async function pickSession(s){
  const row=NS.running.find(x=>x.name===s)||{};
  NS.session=s;NS.cwd=row.path||"";NS.name=s;   // it already has a name — default to it
  $("#newname").value=NS.name;
  $("#peek").textContent="reading "+s+"…";$("#peekflags").innerHTML="";
  nsRender();
  // LOOK INSIDE before touching it: which one is this, and does it hold unsent input?
  const r=await api("/api/peek?session="+encodeURIComponent(s)
                    +"&host="+encodeURIComponent(NS.host));
  if(NS.session!==s)return;                     // selection moved on while we read
  const body=r.ok?(r.content||"(blank)"):("could not read: "+(r.error||""));
  $("#peek").textContent=body;
  $("#peek2").textContent=body;
  let flags="";
  if(r.ok&&r.unsent)flags+='<span class="flag">holds an unsent prompt</span>';
  if(row.attached)flags+='<span class="flag">already attached elsewhere</span>';
  $("#peekflags").innerHTML=flags;
  $("#rflags").innerHTML=flags;
  nsRender();
}
function autoName(d){const base=(d||"").split("/").filter(Boolean).pop()||"session";
  return base.toLowerCase().replace(/[^a-z0-9]+/g,"-").slice(0,28);}

async function openNew(){
  $("#newmodal").classList.add("open");
  $("#newerr").textContent="";$("#newstatus").textContent="";
  Object.assign(NS,{host:"",dirs:[],running:[],lane:"new",session:"",cwd:"",cmd:"",name:"",
                    aiHost:"",aiPick:"",whyHost:"",whyDir:"",manual:false});
  $("#rflags").innerHTML="";$("#peek2").textContent="";
  $("#newintent").value="";$("#newcmd").value="";$("#newname").value="";$("#dirfilter").value="";
  if(!NS.hosts.length){const r=await api("/api/hosts");if(r.ok)NS.hosts=r.hosts||[];}
  nsRender();
  setTimeout(()=>$("#newintent").focus(),40);
}
function closeNew(){$("#newmodal").classList.remove("open");}

// Plain English → the LLM CLASSIFIES it into a path through the tree and PRE-FILLS
// the nodes. Every pick is marked ✦ and stays overridable.
async function doSuggest(){
  const intent=$("#newintent").value.trim();
  if(!intent){$("#newerr").textContent="say what you want to do first";return;}
  const b=$("#newsuggest");b.disabled=true;b.textContent="thinking…";$("#newerr").textContent="";
  $("#newstatus").textContent=NS.host?("reading "+NS.host):"machine → what's on it";
  const r=await api("/api/suggest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({intent,host:NS.host||""})});   // a machine you already picked pins node 1
  b.disabled=false;b.textContent="✦ figure it out";$("#newstatus").textContent="";
  if(!r.ok){$("#newerr").textContent=r.error||"suggest failed";return;}
  NS.host=r.host||NS.host;NS.aiHost=r.host||"";
  NS.dirs=r.dirs||[];NS.running=r.running||[];
  NS.whyHost=r.whyHost||"";NS.whyDir=r.why||"";
  NS.lane=(r.action==="resume")?"resume":"new";
  if(NS.lane==="resume"){
    NS.aiPick=r.session||"";NS.cmd="";
    nsRender();
    if(r.session){await pickSession(r.session);return;}   // selects it AND peeks inside
    NS.session="";NS.cwd="";
  }else{
    NS.cwd=r.cwd||"";NS.aiPick=r.cwd||"";NS.session="";
    NS.cmd=r.command||"";$("#newcmd").value=NS.cmd;
    if(!r.verified&&r.cwd)NS.whyDir="⚠ unverified — "+NS.whyDir;
  }
  NS.name=r.name||(NS.session||(r.cwd?autoName(r.cwd):""));
  $("#newname").value=NS.name;
  nsRender();
}
async function doCreate(){
  if(!NS.name){$("#newerr").textContent="a session needs a name";return;}
  const b=$("#newcreate");const was=b.textContent;
  b.disabled=true;b.textContent=isResume()?"RESUMING…":"CREATING…";$("#newerr").textContent="";
  const resume=isResume();
  const r=await api(resume?"/api/adopt":"/api/spawn",
    {method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify(resume
       ? {session:NS.session,host:NS.host,name:NS.name,gui:$("#newgui").checked,
          description:$("#newintent").value.trim()||("resumed on "+NS.host),
          tags:["adopted",NS.host]}
       : {name:NS.name,host:NS.host,cwd:NS.cwd,command:NS.cmd,
          gui:$("#newgui").checked,description:$("#newintent").value.trim()||null,
          prompt:$("#newintent").value.trim()||null})});   // kickstart: the agent starts ON this
  b.disabled=false;b.textContent=was;
  if(!r.ok){$("#newerr").textContent=r.error||(resume?"resume failed":"spawn failed");return;}
  closeNew();refresh();
}
$("#newbtn").onclick=openNew;
$("#newclose").onclick=closeNew;
$("#newback").onclick=closeNew;
$("#newsuggest").onclick=doSuggest;
$("#newcreate").onclick=doCreate;
$("#dirfilter").addEventListener("input",nsRender);
$("#newgui").addEventListener("change",nsRender);
$("#newchange").onclick=()=>{NS.manual=!NS.manual;nsRender();};
$("#newcmd").addEventListener("input",()=>{NS.cmd=$("#newcmd").value;nsRender();});
$("#newname").addEventListener("input",()=>{NS.name=$("#newname").value.trim();nsRender();});
$("#newintent").addEventListener("keydown",e=>{if(e.key==="Enter")doSuggest();});

$("#dgrefresh").onclick=loadDigest;
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

// ---- live fleet feed ----
let feedSeen="";   // signature of the newest event, to flash only what's new
function fage(ts){const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  return s<60?s+"s":(s<3600?Math.floor(s/60)+"m":(s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d"));}
function esc(x){return (x||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
async function pollFeed(){
  if(!$("#feed").classList.contains("open"))return;
  const r=await api("/api/events?limit=60");
  if(!r.ok)return;
  const ev=r.events||[];
  const newest=ev.length?(ev[0].ts+"|"+ev[0].tag+"|"+ev[0].session):"";
  $("#feedcount").textContent=ev.length?(ev.length+" recent"):"quiet";
  $("#feedlist").innerHTML=ev.map((e,i)=>{
    const fresh=(i===0&&newest!==feedSeen)?" fresh":"";
    const label=e.kind==="signal"?e.tag:(e.kind==="error"?e.tag:e.tag);
    return '<div class="fev k-'+e.tag+(e.kind==="error"?" k-error":"")+fresh+'">'
      +'<span class="fage">'+fage(e.ts)+'</span>'
      +'<span class="ftag">'+esc(label)+'</span>'
      +(e.session?'<span class="fsess">'+esc(e.session)+'</span>':'')
      +'<span class="ftext">'+esc(e.text)+'</span></div>';
  }).join("")||'<div class="fev"><span class="ftext">no activity yet</span></div>';
  feedSeen=newest;
}
function setFeed(open){
  $("#feed").classList.toggle("open",open);
  localStorage.setItem("emux_feed",open?"1":"0");
  if(open)pollFeed();
}
$("#feedbtn").onclick=()=>setFeed(!$("#feed").classList.contains("open"));
$("#feedclose").onclick=()=>setFeed(false);
setFeed(localStorage.getItem("emux_feed")!=="0");   // open by default
setInterval(pollFeed,2000);

// --- Hancock: pending approvals surfaced loud, opened async, cleared in-app ---
let hancock=[], hDismissed=new Set();  // ids the user closed the tray on — stay closed until a NEW one arrives
async function pollHancock(){
  let r; try{ r=await api("/api/hancock"); }catch(e){ return; }
  if(!r||!r.ok)return;
  hancock=r.pending||[];
  const ids=new Set(hancock.map(h=>h.id));
  hDismissed.forEach(id=>{ if(!ids.has(id))hDismissed.delete(id); });  // forget cleared requests
  const n=hancock.length, open=$("#htray").classList.contains("open");
  $("#hbadge").textContent=n;$("#hbadge").style.display=n?"inline-block":"none";
  $("#hbtn").classList.toggle("hot",n>0);
  $("#hbannern").textContent=n;
  if(open&&n===0){ $("#htray").classList.remove("open"); }        // queue cleared → slide away
  else if(!open&&hancock.some(h=>!hDismissed.has(h.id))){ openHancock(); return; }  // fresh request → throw it open, don't wait
  document.body.classList.toggle("hneedy",n>0&&!$("#htray").classList.contains("open"));
  if($("#htray").classList.contains("open"))renderHancock();
}
function renderHancock(){
  const box=$("#htraylist");
  if(!hancock.length){box.innerHTML='<div class="hempty">nothing waiting — the fleet is clear ✓</div>';return;}
  box.innerHTML=hancock.map(h=>{
    const risk=(h.risk||"medium");
    return '<div class="hreq r-'+risk+'" data-id="'+h.id+'">'
      +'<div class="hrisk">'+risk+'</div>'
      +'<div class="hcmd">'+esc(h.command)+'</div>'
      +(h.why?'<div class="hwhy">'+esc(h.why)+'</div>':'')
      +(h.cwd?'<div class="hcwd">'+esc(h.cwd)+'</div>':'')
      +'<div class="hact"><button class="happrove" onclick="hancockDo(\''+h.id+'\',1)">approve &amp; run</button>'
      +'<button class="hdeny" onclick="hancockDo(\''+h.id+'\',0)">deny</button></div></div>';
  }).join("");
}
let htoastTimer=null;
function htoast(msg,kind){
  const t=$("#htoast");t.textContent=msg;t.className="show "+(kind||"");
  clearTimeout(htoastTimer);htoastTimer=setTimeout(()=>{t.className="";},kind==="err"?5000:2200);
}
async function hancockDo(id,ok){
  const card=document.querySelector('#htraylist .hreq[data-id="'+id+'"]');
  const btns=card?card.querySelectorAll('button'):[];btns.forEach(b=>b.disabled=true);
  if(card)card.classList.add("working");
  const path=ok?"/api/hancock/approve":"/api/hancock/deny";
  let r; try{ r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}); }
  catch(e){ r={ok:false,error:"unreachable"}; }
  if(!r.ok){ btns.forEach(b=>b.disabled=false); if(card)card.classList.remove("working");
    htoast((ok?"Approve":"Deny")+" failed — "+(r.error||"unknown"),"err"); return; }
  htoast(ok?"Approved & ran ✓":"Denied ✓","ok");
  hancock=hancock.filter(h=>h.id!==id);
  renderHancock();pollHancock();
}
function openHancock(){ $("#htray").classList.add("open");document.body.classList.remove("hneedy");renderHancock(); }
function closeHancock(){ $("#htray").classList.remove("open");hancock.forEach(h=>hDismissed.add(h.id)); }  // stay closed for THESE; a new id re-opens
pollHancock();setInterval(pollHancock,3000);

// --- model routing settings ---
const TASK_LABELS={gist:"The Gist (session digest + suggested replies)",placement:"Session placement (new-session machine + name)"};
async function openSettings(){
  const r=await api("/api/models");
  if(!r.ok)return;
  const c=r.config;
  $("#nimurl").value=c.nim.base_url||"";$("#nimmodel").value=c.nim.model||"";$("#nimkey").value=c.nim.api_key||"";
  $("#setroutes").innerHTML=(r.tasks||[]).map(t=>{
    const cur=c.routes[t]||"claude";
    return '<label class="setroute"><span>'+esc(TASK_LABELS[t]||t)+'</span>'
      +'<select data-task="'+t+'">'
      +'<option value="claude"'+(cur==="claude"?" selected":"")+'>claude -p (subscription)</option>'
      +'<option value="nim"'+(cur==="nim"?" selected":"")+'>NIM (local)</option>'
      +'</select></label>';
  }).join("");
  $("#nimtestout").textContent="";$("#setsaveout").textContent="";
  $("#setmodal").classList.add("open");
}
function closeSettings(){$("#setmodal").classList.remove("open");}
async function testNim(){
  const out=$("#nimtestout");out.textContent="testing…";out.className="";
  await saveSettings(true);   // persist first so the server tests what you typed
  const r=await api("/api/models/test",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  if(r.ok){out.textContent="✓ reachable"+(r.models&&r.models.length?" · "+r.models.slice(0,3).join(", "):"");out.className="ok";}
  else{out.textContent="✗ "+(r.error||"unreachable");out.className="err";}
}
async function saveSettings(quiet){
  const routes={};document.querySelectorAll("#setroutes select").forEach(s=>routes[s.dataset.task]=s.value);
  const body={nim:{base_url:$("#nimurl").value.trim(),model:$("#nimmodel").value.trim(),api_key:$("#nimkey").value},routes};
  const r=await api("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!quiet){const o=$("#setsaveout");o.textContent=r.ok?"saved ✓":"save failed";o.className=r.ok?"ok":"err";
    setTimeout(()=>{o.textContent="";},1800);}
  return r.ok;
}

applyURL();   // restore view + filters + open session from the URL (falls back to localStorage)
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
            # a registered remote session captures over ssh
            self._json(capture_payload(session, lines, host=_session_host(session)))
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
        if url.path == "/api/events":
            q = parse_qs(url.query)
            try:
                lim = max(1, min(200, int((q.get("limit") or ["60"])[0])))
            except ValueError:
                lim = 60
            self._json({"ok": True, "events": _events(lim)})
            return
        if url.path == "/api/hosts":
            self._json({"ok": True, "hosts": _known_hosts()})
            return
        if url.path == "/api/hancock":
            pend = _hancock_pending()
            self._json({"ok": True, "pending": pend, "count": len(pend)})
            return
        if url.path == "/api/models":
            self._json({"ok": True, "config": _model_config(), "tasks": list(_TASKS)})
            return
        if url.path == "/api/agents":
            from . import agents as _agents
            q = (parse_qs(url.query).get("scenario") or [""])[0]
            self._json({"ok": True, **(_agents.advise(q) if q else _agents.table())})
            return
        if url.path == "/api/peek":
            q = parse_qs(url.query)
            sess = (q.get("session") or [""])[0]
            h = (q.get("host") or ["local"])[0]
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            self._json(_peek_session(sess, None if h in ("", "local") else h))
            return
        if url.path == "/api/dirs":
            # what exists ON the chosen machine — the cascade's 2nd level.
            # Two kinds: sessions you can RESUME, and dirs you can start NEW in.
            h = (parse_qs(url.query).get("host") or ["local"])[0]
            rh = None if h in ("", "local") else h
            self._json({"ok": True, "host": h,
                        "dirs": _candidate_dirs(rh),
                        "running": _running_sessions(rh)})
            return
        self._json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path not in ("/api/send", "/api/head", "/api/spawn", "/api/suggest",
                            "/api/adopt", "/api/reply",
                            "/api/hancock/approve", "/api/hancock/deny",
                            "/api/models", "/api/models/test"):
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
        if url.path == "/api/reply":
            sess = (data.get("session") or "").strip()
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            self._json(_reply_suggestions(sess, _session_host(sess)))
            return
        if url.path == "/api/adopt":
            self._json(_adopt_session(data))
            return
        if url.path == "/api/models":
            self._json({"ok": True, "config": _save_model_config(data)})
            return
        if url.path == "/api/models/test":
            self._json(_nim_ping())
            return
        if url.path in ("/api/hancock/approve", "/api/hancock/deny"):
            rid = (data.get("id") or "").strip()
            if not rid:
                self._json({"ok": False, "error": "missing_id"}, 400)
                return
            if url.path.endswith("approve"):
                self._json(_hancock_approve(rid))
            else:
                self._json(_hancock_deny(rid))
            return
        session = data.get("session")
        if not isinstance(session, str) or not session:
            self._json({"ok": False, "error": "missing_session"}, 400)
            return
        if url.path == "/api/head":
            # a registered session may live on a remote box — attach over ssh
            reg = _server._load_registry()
            entry = next((e for e in reg.values() if e.get("session") == session), {})
            ok, err = _iterm_attach(session, entry.get("host"))
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
            host=_session_host(session),   # steer a remote worker over ssh
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
