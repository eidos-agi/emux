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

import json
import os
import re
import shlex
import shutil
import urllib.request
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
    # Can the one-shot mode call MCP tools? If False, a one-shot of this agent
    # can only THINK — it cannot act, so it is useless as a manager.
    oneshot_can_use_tools: bool = True
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
    # NOTE: "esc to interrupt" is deliberately NOT here. Codex prints the same
    # string ("• Working (1s • esc to interrupt)"), so using it as a Claude
    # content-signature misdetects a node-wrapped Codex AS Claude. Measured.
    content_sigs=("claude code", "anthropic", "? for shortcuts", "bypass permissions"),
    # text+Enter too fast trips paste detection; 0.4s lands it. Verified live.
    send_settle=0.4,
    busy_sigs=("esc to interrupt",),
    # a y/n confirm OR a selection menu ("Enter to select · ↑/↓ to navigate")
    # is the agent waiting on YOU to choose — a gate.
    approval_sigs=("do you want to proceed", "❯ 1. yes", "1. yes",
                   "enter to select", "↑/↓ to navigate", "to navigate · esc",
                   "allow the"),  # per-tool MCP approval, same modal shape as Codex
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
    # tmux reports the platform binary, e.g. "codex-aarch64-a" — not "codex".
    # The substring match catches it; measured, don't "fix" it to an exact match.
    pane_cmds=("codex",),
    content_sigs=("openai codex", "codex cli", "• working (", "/model to change"),
    # MEASURED, not borrowed: 0.2s does NOT submit, 0.4s does. Like Claude, a
    # single text+Enter is swallowed as a paste and the prompt sits unsent.
    send_settle=0.4,
    busy_sigs=("• working (", "esc to interrupt"),
    # Codex's startup is GATE-LADEN, and every gate eats keystrokes (see the
    # module docstring warning). All three block an unattended spawn.
    approval_sigs=(
        "do you trust the contents of this directory",   # 1st launch in a dir
        "hooks need review",                             # hook hashes changed
        "enter to review hooks",                         # …its 2nd presentation
        "update available",                              # ← default is BREW UPGRADE
        # Codex asks approval PER MCP TOOL CALL, with a menu. This is why a
        # `codex exec` manager is useless: headless has no approver, so every
        # MCP call auto-cancels ("user cancelled MCP tool call") — measured
        # against emux AND a known-good server, so it is not an emux fault.
        "allow the",                                     # "Allow the X MCP server to run tool …"
        "press enter to continue",
        "press enter to confirm",
    ),
    # Clears the hook-trust gate. The directory-trust gate is cleared by
    # pre-trusting the cwd: -c 'projects."<cwd>".trust_level="trusted"'.
    # NOT --dangerously-bypass-approvals-and-sandbox: that also removes the
    # sandbox, which is a bigger concession than the gate requires.
    launch_flags=("--dangerously-bypass-hook-trust",),
    resume_fmt="codex resume {id}",
    resume_last="codex resume --last",
    # `codex exec` is TEXT-ONLY. It can reason, but it CANNOT call MCP tools:
    # every call is auto-cancelled because headless has no one to answer Codex's
    # per-tool approval menu. Measured against emux and a known-good server, so
    # it is not an emux fault. A Codex MANAGER must therefore be an INTERACTIVE
    # session (which is the right shape anyway — a manager should be warm).
    oneshot_fmt="codex exec {prompt}",
    oneshot_can_use_tools=False,
    # Codex has a NATIVE Stop hook, same JSON shape as Claude's — better than the
    # `notify` path. PROVEN LIVE: hook fired `emux signal IDLE` into the inbox
    # ~9s after the turn ended, and the judge read the session as done_idle.
    done_hook="Stop hook (hooks.json) → `emux signal IDLE`",
    done_hook_install={
        "file": "~/.codex/hooks.json",           # or <project>/.codex/hooks.json
        "path": ["hooks", "Stop"],
        "run": "emux signal IDLE",
        "note": "identical shape to Claude's Stop hook; project-local file avoids "
                "touching the user's global config",
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


def gated(agent_key: str | None, content: str) -> str | None:
    """Is a modal GATE on screen right now? Returns the gate text, else None.

    THIS IS A SAFETY CHECK, not a nicety. An agent's startup gate is a menu that
    eats keystrokes and PERSISTS the answer. Typing a prompt into a gated Codex
    pane is not a no-op — it is a config write. Measured, the hard way:

      • Sending "what is 2+2?" while the hook-review gate was up fed the `2` to
        the menu, which selected "2. Trust all and continue" and wrote
        trusted_hash entries into ~/.codex/config.toml.
      • The update gate defaults to "1. Update now (runs `brew upgrade`)", so a
        blind Enter upgrades the user's Codex.

    So: before sending anything, ask whether a gate is up. If it is, the caller
    must resolve it deliberately — never type through it.
    """
    a = BY_KEY.get(agent_key or "")
    low = (content or "").lower()
    if a and a.approval_sigs:
        for sig in a.approval_sigs:
            if sig in low:
                return sig
        return None
    # Agent unidentified (e.g. codex/claude running under a `node`/`python`
    # wrapper, so pane_current_command isn't the agent name). A gate is a
    # safety-critical thing to miss, so fall back to matching EVERY known
    # agent's signatures against the screen. Better a false "gated" (send
    # refused, human retries) than typing a prompt through a live modal.
    return any_gate(low)


def any_gate(content: str) -> str | None:
    """Agent-agnostic gate scan: does the screen show ANY known agent's gate?"""
    low = (content or "").lower()
    for adapter in ADAPTERS:
        for sig in adapter.approval_sigs:
            if sig in low:
                return sig
    return None


# --------------------------------------------------------------------------- #
# ML gate detection — the net under the signature list
# --------------------------------------------------------------------------- #
#
# `gated()`/`any_gate()` match a hardcoded signature list. That list will ALWAYS
# lag: new agents, reworded prompts, locales, partial redraws. And here the
# failure direction is dangerous — a missed gate means a prompt gets typed into
# a live modal that persists the answer (measured: codex under a `node` wrapper
# went unidentified and a real trust gate was nearly typed through — EID-871).
#
# So: a learned fallback. Signatures stay the instant first check. Only when a
# screen is *suspicious but unmatched* (unidentified agent, or structurally
# gate-shaped) do we pay for a local-model classification. Fail CLOSED: a gate,
# an uncertain answer, or an unreachable model all count as gated. A local model
# only (Ollama / claude -p) — never a raw cloud API (fixed-cost tools only).

# Structural pre-filter: strong decision phrases, or a numbered menu behind an
# interactive selector glyph. Deliberately tighter than "any numbered list" so
# ordinary output doesn't pay for inference.
_GATE_LIKE = re.compile(
    r"do you (?:want|trust)"
    r"|press enter to (?:continue|confirm|select)"
    r"|\(y/n\)|\[y/n\]|\byes/no\b"
    r"|need review"
    r"|update available"
    r"|type ['\"]?yes['\"]? to confirm"
    # a numbered option BEHIND a selector glyph — the shape of a menu, not a
    # bare ❯/› composer or shell prompt (those use the same glyphs).
    r"|[❯›▶]\s*\d+[.)]\s+\S",
    re.IGNORECASE | re.MULTILINE,
)

_GATE_ML_URL = os.environ.get("EMUX_GATE_ML_URL", "http://127.0.0.1:11434/api/generate")
# llama3.2:3b scored 12/12 on the gate/clear eval at ~1s warm; the 1.5b model
# over-triggered (fail-safe but useless). Few-shot is load-bearing — the same
# models are near-random zero-shot (EID-871 eval, 2026-07-20).
_GATE_ML_MODEL = os.environ.get("EMUX_GATE_ML_MODEL", "llama3.2:3b")

_GATE_ML_PROMPT = (
    "You are a strict binary classifier for terminal screens. Reply with exactly "
    "one word: GATE or CLEAR.\n"
    "GATE = the screen is BLOCKED waiting for a human to choose/confirm before "
    "anything else can happen (permission, trust, update, y/n, numbered options "
    "behind a > selector, press enter to continue).\n"
    "CLEAR = ordinary output, a shell prompt ready for input, a running/finished "
    "program, or a text composer. Not blocked on a decision.\n\n"
    "Examples:\n"
    "SCREEN: Do you trust this directory?\n> 1. Yes  2. No\nANSWER: GATE\n"
    "SCREEN: eidos@host:~$ ls\nfile.txt  build/\nANSWER: CLEAR\n"
    "SCREEN: 393 passed in 49s\neidos@host:~$\nANSWER: CLEAR\n"
    "SCREEN: Overwrite file? (y/n)\nANSWER: GATE\n"
    "SCREEN: * Building wheel... done\nANSWER: CLEAR\n\n"
)


def looks_gate_like(content: str) -> bool:
    """Cheap structural pre-filter: does this screen have the SHAPE of a modal
    awaiting a human decision? Decides whether an unmatched screen is worth an
    ML classification — keeps inference off the ordinary-output fast path."""
    return bool(_GATE_LIKE.search(content or ""))


def ml_is_gate(content: str, timeout: float = 3.0) -> bool | None:
    """Local-model classification of whether a pane shows a gate awaiting a human.

    Returns True (gate), False (clear), or None (unreachable/unparseable). Local
    Ollama only — never a raw cloud API. The caller fails closed on True/None."""
    screen = "\n".join((content or "").splitlines()[-20:])[:2000]
    prompt = _GATE_ML_PROMPT + f"SCREEN: {screen}\nANSWER:"
    body = json.dumps(
        {
            "model": _GATE_ML_MODEL,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "10m",  # keep the model resident so escalations stay ~1s, not cold-load
            "options": {"temperature": 0, "num_predict": 3},
        }
    ).encode()
    try:
        req = urllib.request.Request(
            _GATE_ML_URL, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = str(json.load(resp).get("response", ""))
    except Exception:  # noqa: BLE001 — any failure ⇒ None ⇒ caller fails closed
        return None
    up = out.strip().upper()
    if "GATE" in up:
        return True
    if "CLEAR" in up:
        return False
    return None


def detect_gate(agent_key: str | None, content: str, ml: Any = ml_is_gate) -> tuple[str | None, str | None]:
    """Layered gate detection. Returns (kind, detail):
    kind == "signature" — a known signature matched (instant path);
    kind == "ml"        — ML/uncertain fallback fired (fail-closed);
    kind is None        — clear, safe to send.

    ML is skipped entirely when EMUX_GATE_ML is off (signatures-only, the legacy
    behavior) — an escape hatch if the local model is unavailable/unreliable."""
    sig = gated(agent_key, content)
    if sig:
        return ("signature", sig)
    if os.environ.get("EMUX_GATE_ML", "on").strip().lower() in ("off", "0", "false", "no"):
        return (None, None)
    # Escalate only when we have reason to distrust the fast path: an
    # unidentified agent (signatures can't be trusted at all) or a gate-shaped
    # screen. Everything else is taken as clear without paying for inference.
    if agent_key and not looks_gate_like(content):
        return (None, None)
    verdict = ml(content)
    if verdict is False:
        return (None, None)  # the model confidently cleared it
    return ("ml", "ml-gate" if verdict is True else "ml-uncertain")


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
