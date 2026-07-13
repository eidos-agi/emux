"""One adapter per AI agent — the contract for driving it through a terminal.

emux drives agents that live in a TUI, and every one of them differs in the four
places that matter:

    DETECT   what does this agent look like in a pane?
    DRIVE    how do I type into it without the input being swallowed?
    READ     how do I tell whether it is working, blocked, or done?
    LIVE     how do I launch it, resume it, and get a completion signal?

Before this file, emux only really knew ONE agent. Claude-specific facts were
smeared through general code: the semver pane-title hack in web, `esc to
interrupt` inside the judge's regexes, `settle=0.4` in tmux_send (a workaround
for *Claude's* paste detection), and a Stop hook as *the* way a worker reports
done. Every one of those is a per-agent contract wearing a general rule's
clothes. They belong here, named and per-agent.

Honesty rule: a field we have not established is left empty and marked unknown.
An adapter that lies is worse than one that admits it doesn't know — a wrong
`busy` regex makes the judge confidently mislabel a session.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Adapter:
    """How to detect, drive, read, and live-cycle one agent."""

    key: str
    label: str
    glyph: str
    color: str
    cmd: str
    access: str = "unknown"          # subscription | metered | unknown

    # ---- DETECT -----------------------------------------------------------
    pane_cmds: tuple[str, ...] = ()      # matches #{pane_current_command}
    pane_regex: str | None = None        # …when the pane title isn't the binary name
    content_sigs: tuple[str, ...] = ()   # substrings that betray it on screen

    # ---- DRIVE ------------------------------------------------------------
    # seconds to wait between typing text and sending Enter. A fast text+Enter
    # trips a TUI's paste detection and lands as an unsubmitted multi-line blob.
    send_settle: float = 0.0

    # ---- READ -------------------------------------------------------------
    busy_sigs: tuple[str, ...] = ()       # on screen ⇒ actively generating
    approval_sigs: tuple[str, ...] = ()   # on screen ⇒ blocked on a human

    # ---- LIVE -------------------------------------------------------------
    launch_flags: tuple[str, ...] = ()      # flags for an unattended session
    resume_fmt: str | None = None           # e.g. "claude --resume {id}"
    resume_last: str | None = None          # resume the most recent session
    oneshot_fmt: str | None = None          # non-interactive: prompt in, text out
    # how this agent can be made to emit a completion signal. Two agents, two
    # mechanisms, same idea: the harness fires at a real turn boundary, so
    # nothing is scraped and the worker can't forget to report.
    done_hook: str | None = None
    done_hook_install: dict[str, Any] = field(default_factory=dict)

    # ---- derived ----------------------------------------------------------
    def installed(self) -> bool:
        return shutil.which(self.cmd) is not None

    def launch(self, prompt: str | None = None, unattended: bool = False) -> str:
        parts = [self.cmd, *(self.launch_flags if unattended else ())]
        if prompt:
            parts.append(prompt)
        return " ".join(shlex.quote(p) if " " in p else p for p in parts)

    def resume(self, session_id: str | None = None) -> str | None:
        if session_id and self.resume_fmt:
            return self.resume_fmt.format(id=shlex.quote(session_id))
        return self.resume_last

    def matches(self, pane_cmd: str, content: str = "") -> bool:
        cmd, low = (pane_cmd or "").lower(), (content or "").lower()
        if any(c in cmd for c in self.pane_cmds):
            return True
        if self.pane_regex and re.match(self.pane_regex, cmd):
            return True
        return any(s in low for s in self.content_sigs)


# --------------------------------------------------------------------------- #
# the agents emux actually knows how to drive
# --------------------------------------------------------------------------- #

CLAUDE = Adapter(
    key="claude", label="Claude Code", glyph="✳", color="#d97757",
    cmd="claude", access="subscription",
    pane_cmds=("claude",),
    # Claude Code retitles its pane to a bare version string ("2.1.207"), so the
    # binary name is NOT what tmux reports. Verified live.
    pane_regex=r"^\d+\.\d+\.\d+",
    content_sigs=("claude code", "anthropic", "esc to interrupt",
                  "? for shortcuts", "bypass permissions"),
    # text+Enter too fast trips paste detection; 0.4s lands it. Verified live.
    send_settle=0.4,
    busy_sigs=("esc to interrupt",),
    approval_sigs=("do you want to proceed", "❯ 1. yes", "1. yes"),
    launch_flags=("--dangerously-skip-permissions",),
    resume_fmt="claude --resume {id}",
    oneshot_fmt="claude -p {prompt}",
    done_hook="Stop hook → `emux signal IDLE`; Notification → `emux signal NEED`",
    done_hook_install={
        "file": "~/.claude/settings.json",
        "path": ["hooks", "Stop"],
        "run": "emux signal IDLE",
        "note": "fires at a real turn boundary — no scraping, worker can't forget",
    },
)

CODEX = Adapter(
    key="codex", label="Codex", glyph="◇", color="#10a37f",
    cmd="codex", access="subscription",
    pane_cmds=("codex",),
    content_sigs=("openai codex", "codex cli"),
    # UNKNOWN: not yet measured against codex's TUI. 0.4 is Claude's number and
    # borrowing it would be guessing; 0 means "type and submit" until measured.
    send_settle=0.0,
    busy_sigs=(),          # unknown — do NOT invent, the judge would mislabel
    approval_sigs=(),      # unknown
    launch_flags=(),       # its config already sets approval_policy = "never"
    resume_fmt="codex resume {id}",
    resume_last="codex resume --last",
    oneshot_fmt="codex exec {prompt}",
    # Codex's answer to Claude's Stop hook: a `notify` program invoked on
    # turn-ended. Same shape — the harness fires at the boundary.
    done_hook="notify → turn-ended → `emux signal IDLE`",
    done_hook_install={
        "file": "~/.codex/config.toml",
        "key": "notify",
        "value": ["emux", "signal", "IDLE"],
        "note": "codex appends a JSON payload arg; `emux signal` ignores extras",
    },
)

# Installed on this box but NOT established. Detection only — we do not claim to
# know how to drive, read, or signal these. Fill them in when you measure one.
GEMINI = Adapter(key="gemini", label="Gemini", glyph="♊", color="#5a8dff",
                 cmd="gemini", pane_cmds=("gemini",),
                 content_sigs=("gemini cli", "google gemini"))
GROK = Adapter(key="grok", label="Grok", glyph="⚡", color="#e8e8e8",
               cmd="grok", pane_cmds=("grok",), content_sigs=("grok", "xai"))
OPENCODE = Adapter(key="opencode", label="opencode", glyph="❖", color="#b57bff",
                   cmd="opencode", pane_cmds=("opencode",), content_sigs=("opencode",))
AIDER = Adapter(key="aider", label="Aider", glyph="✦", color="#f0a020",
                cmd="aider", pane_cmds=("aider",), content_sigs=("aider ",))

# order matters: most specific first
ADAPTERS: tuple[Adapter, ...] = (CLAUDE, CODEX, GEMINI, GROK, OPENCODE, AIDER)
BY_KEY: dict[str, Adapter] = {a.key: a for a in ADAPTERS}

# things that are not agents at all
SHELLS = {"zsh", "-zsh", "bash", "-bash", "fish", "sh", "-sh"}
EDITORS = {"vim", "nvim", "vi", "nano", "emacs"}


def detect(pane_cmd: str, content: str = "") -> Adapter | None:
    """Which agent is running in this pane? None if it isn't one we know."""
    cmd = (pane_cmd or "").lower()
    for a in ADAPTERS:                      # binary/pane-title first (cheap, exact)
        if any(c in cmd for c in a.pane_cmds):
            return a
        if a.pane_regex and re.match(a.pane_regex, cmd):
            return a
    low = (content or "").lower()           # then what's on screen (fuzzy)
    for a in ADAPTERS:
        if any(s in low for s in a.content_sigs):
            return a
    return None


def get(key: str) -> Adapter | None:
    return BY_KEY.get(key)


def settle_for(agent_key: str | None) -> float:
    """How long to wait before Enter, for THIS agent. 0 when we haven't measured."""
    a = BY_KEY.get(agent_key or "")
    return a.send_settle if a else 0.0


def busy_sigs_for(agent_key: str | None) -> tuple[str, ...]:
    """Screen tells that this agent is actively generating. Empty = unknown, and
    the judge must fall back to change-detection rather than guess."""
    a = BY_KEY.get(agent_key or "")
    return a.busy_sigs if a else ()


def table() -> list[dict[str, Any]]:
    """The adapter matrix — including, honestly, what we don't know yet."""
    rows = []
    for a in ADAPTERS:
        rows.append({
            "agent": a.key, "label": a.label, "access": a.access,
            "installed": a.installed(),
            "detect": bool(a.pane_cmds or a.pane_regex or a.content_sigs),
            "drive": a.send_settle > 0 or a.key == "codex",
            "read": bool(a.busy_sigs),          # can the judge read its state?
            "resume": bool(a.resume_fmt or a.resume_last),
            "done_signal": a.done_hook,
            "unknowns": [k for k, v in (("send_settle", a.send_settle),
                                        ("busy_sigs", a.busy_sigs),
                                        ("approval_sigs", a.approval_sigs))
                         if not v],
        })
    return rows
