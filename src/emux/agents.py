"""Which agent to use in which scenario — a rudimentary, updatable registry.

emux spawns sessions that run AI coding agents. Which one should run in a given
session is a real decision, and right now it lives only in someone's head. This
is the smallest thing that fixes that: a table you can read, query, and correct
as evidence arrives.

THE SELECTION AXIS IS CAPABILITY, NOT PRICE.
The operator is a SUBSCRIBER to both Claude Code and Codex — flat fee, not
per-token. So the whole genre of "route cheap tokens to a cheap tier to cut your
bill" is a non-question here, and the hard constraint (never the metered API,
only `claude -p` / the Agent SDK / a subscribed CLI) already forbids the path it
optimises. Route on what an agent is GOOD at, not what it costs.

Correct this file when you learn something. Every route carries `evidence` and
`updated` so a claim can be traced, and `notes` records claims we EVALUATED AND
REJECTED, so they don't get re-litigated.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# the agents
# --------------------------------------------------------------------------- #

# access: "subscription" = flat fee, safe to burn tokens on.
#         "metered"      = per-token API. FORBIDDEN as a build target.
#         "unknown"      = installed, but we haven't established how it bills.
AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "cmd": "claude",
        "label": "Claude Code",
        "access": "subscription",
        "good_at": [
            "judgment calls and design decisions",
            "large multi-file refactors",
            "long multi-step tool chains that must not shortcut",
            "writing: docs, briefs, commit messages, prose",
            "reading an unfamiliar codebase and explaining it",
            "adversarial review of someone else's work",
        ],
        "weak_at": ["nothing established yet — record it when you find it"],
    },
    "codex": {
        "cmd": "codex",
        "label": "Codex",
        "access": "subscription",
        "good_at": [
            "long autonomous coding loops you leave running",
            "agentic coding against a test suite (its published strength)",
            "a second, independent implementation to diff against Claude's",
        ],
        "weak_at": ["unestablished — we have not measured it head-to-head"],
    },
    "gemini": {"cmd": "gemini", "label": "Gemini", "access": "unknown",
               "good_at": ["very large context dumps (claimed, unverified here)"],
               "weak_at": []},
    "grok": {"cmd": "grok", "label": "Grok", "access": "unknown",
             "good_at": [], "weak_at": []},
    "opencode": {"cmd": "opencode", "label": "opencode", "access": "unknown",
                 "good_at": ["open-model routing when you want a local/cheap backend"],
                 "weak_at": []},
    "aider": {"cmd": "aider", "label": "Aider", "access": "unknown",
              "good_at": ["tight edit-commit loops on a small diff"], "weak_at": []},
}

# --------------------------------------------------------------------------- #
# the routes — scenario → agent. Rudimentary on purpose. Correct as you learn.
# --------------------------------------------------------------------------- #

ROUTES: list[dict[str, Any]] = [
    {
        "scenario": "write, refactor, or debug code",
        "triggers": ["refactor", "debug", "fix", "bug", "implement", "feature",
                     "build", "code", "test", "failing"],
        "use": "claude",
        "why": "default for anything needing judgment; strongest at multi-step chains",
        "evidence": "operator's standing practice; not a measured head-to-head",
        "updated": "2026-07-12",
    },
    {
        "scenario": "plan, strategise, decide, or write prose",
        "triggers": ["plan", "strategy", "decide", "decision", "brief", "write",
                     "doc", "docs", "research", "explain", "review"],
        "use": "claude",
        "why": "reasoning + writing; this is what a cockpit session is usually for",
        "evidence": "operator's standing practice",
        "updated": "2026-07-12",
    },
    {
        "scenario": "a long autonomous build you leave running unattended",
        "triggers": ["autonomous", "unattended", "overnight", "long run", "loop",
                     "agentic", "let it run", "grind"],
        "use": "codex",
        "why": "built for the unattended agentic-coding loop; subscribed, so let it burn",
        "evidence": "vendor benchmark (Terminal-Bench) — NOT independently verified here",
        "updated": "2026-07-12",
    },
    {
        "scenario": "get an independent second opinion / cross-check",
        "triggers": ["second opinion", "cross-check", "verify", "adversarial",
                     "another model", "compare", "disagree", "sanity check"],
        "use": "codex",
        "why": "a DIFFERENT model is the point — diff two independent attempts. "
               "Use whichever agent did NOT do the original work.",
        "evidence": "principle, not measurement: independence is the value",
        "updated": "2026-07-12",
    },
]

# Claims we looked at and REJECTED — so nobody re-litigates them.
NOTES: list[dict[str, str]] = [
    {
        "claim": "Run a 3-tier model stack (cheap/mid/premium) to cut agent cost ~80%",
        "verdict": "IRRELEVANT HERE, and the number is wrong anyway",
        "why": "The operator is a SUBSCRIBER to Claude Code and Codex — flat fee, so "
               "per-token tiering saves nothing. The source (a vendor's subreddit, "
               "r/better_claw → BetterClaw.io) also mis-derives its headline: it books "
               "85% of tokens on the cheap tier at ~9x less than its own baseline "
               "implies. Corrected, its own figures give ~52%, not 80%. Its 'testing' "
               "also predates the model's GA. Treat as content marketing.",
        "source": "reddit.com/r/better_claw — GPT-5.6 three price tiers, 2026-07-07",
        "updated": "2026-07-12",
    },
    {
        "claim": "Route bulk/background tokens to a metered cheap API model",
        "verdict": "FORBIDDEN",
        "why": "Hard constraint: never the metered API. Fixed-cost tools only "
               "(`claude -p`, the Agent SDK, a subscribed CLI). Unpredictable bills "
               "are the thing being avoided; a cheaper per-token rate is still metered.",
        "source": "operator hard constraint",
        "updated": "2026-07-12",
    },
]


def _override_path() -> Path:
    return Path(os.environ.get("EMUX_AGENTS")
                or Path.home() / ".config" / "emux" / "agents.json")


def _load() -> dict[str, Any]:
    """Defaults, with a user override file merged over the top (if present)."""
    data: dict[str, Any] = {"agents": dict(AGENTS), "routes": list(ROUTES),
                            "notes": list(NOTES)}
    p = _override_path()
    if p.is_file():
        try:
            user = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return data
        for a, spec in (user.get("agents") or {}).items():
            data["agents"].setdefault(a, {}).update(spec)
        # user routes take precedence: they're checked first
        data["routes"] = list(user.get("routes") or []) + data["routes"]
        data["notes"] = list(user.get("notes") or []) + data["notes"]
    return data


def installed() -> dict[str, str]:
    """Which agent CLIs actually exist on this machine."""
    out = {}
    for key, spec in _load()["agents"].items():
        path = shutil.which(spec.get("cmd") or key)
        if path:
            out[key] = path
    return out


def advise(scenario: str) -> dict[str, Any]:
    """Which agent should run this? Rudimentary keyword match over the routes.

    Returns the matched route plus the runnable command, or the default (claude)
    when nothing matches — with `matched: False` so the caller knows it guessed.
    """
    data = _load()
    low = (scenario or "").lower()
    best, score = None, 0
    for route in data["routes"]:
        hits = sum(1 for t in route.get("triggers", []) if t in low)
        if hits > score:
            best, score = route, hits
    have = installed()
    if best is None:
        agent = "claude"
        return {
            "agent": agent, "matched": False,
            "why": "no route matched — defaulting to Claude Code",
            "command": data["agents"][agent]["cmd"] if agent in have else "",
            "installed": agent in have,
            "access": data["agents"][agent]["access"],
        }
    agent = best["use"]
    spec = data["agents"].get(agent, {})
    return {
        "agent": agent, "matched": True, "scenario": best["scenario"],
        "why": best["why"], "evidence": best.get("evidence"),
        "updated": best.get("updated"),
        "command": spec.get("cmd", "") if agent in have else "",
        "installed": agent in have,
        "access": spec.get("access", "unknown"),
    }


def table() -> dict[str, Any]:
    """The whole registry + what's actually installed — for `emux agents` / the web."""
    data = _load()
    have = installed()
    for key, spec in data["agents"].items():
        spec["installed"] = key in have
        spec["path"] = have.get(key)
    data["override_file"] = str(_override_path())
    return data
