"""Provision a bounded worker environment so an agent runs free INSIDE a
boundary instead of stopping at trust/permission gates (EID-874, pivoted).

The autonomy construction (per the roadmap: EID-839 isolated shadow, EID-836
least-privilege, GMW-864 ssh sandbox, Dally sandbox-only): don't answer gates,
CONFIGURE them away and bound what the worker can reach. Two moves:

1. Establish folder trust by config so the "Do you trust this folder?" gate
   never appears. Verified empirically (2026-07-20): Claude Code stores trust
   as `projects[<abspath>].hasTrustDialogAccepted` in ~/.claude.json.

2. Point the session's PreToolUse hook at the delegation policy (hook_delegation)
   via settings.local.json, so consequential tools the grant does not cover
   escalate to a human ('ask') at the STRUCTURED boundary — never a screen scrape.

This module only writes config a human/operator authorized; it never presses a
key and never runs the agent. Pairs with an operator who launches the worker
with EMUX_IDENTITY / EMUX_SERVER_ID / EMUX_DELEGATIONS set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOOK_MODULE = "emux.hook_delegation"


def mark_trusted(project_dir: str, claude_json: Path | None = None) -> None:
    """Pre-accept the folder-trust dialog for one project, so it never prompts.
    Idempotent; touches only that project's entry."""
    path = claude_json or (Path.home() / ".claude.json")
    abspath = os.path.realpath(project_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = data["projects"] = {}
    entry = projects.setdefault(abspath, {})
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    tmp = path.with_suffix(".json.emux-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def write_settings_local(project_dir: str, python: str | None = None) -> Path:
    """Write <project>/.claude/settings.local.json wiring the PreToolUse
    delegation hook. Personal/gitignored, so it does not trigger the trust
    dialog and does not travel with the repo."""
    py = python or "python3"
    settings_dir = Path(project_dir) / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"
    hook_cmd = f"{py} -m {HOOK_MODULE}"
    config: dict[str, Any] = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}
            ]
        }
    }
    settings_path.write_text(json.dumps(config, indent=2) + "\n")
    return settings_path


def provision(project_dir: str, python: str | None = None,
              claude_json: Path | None = None) -> dict[str, Any]:
    """Full bounded-worker provision: trust the folder + wire the delegation
    hook. Returns what was written so the caller can log/verify it."""
    mark_trusted(project_dir, claude_json=claude_json)
    settings = write_settings_local(project_dir, python=python)
    return {
        "project_dir": os.path.realpath(project_dir),
        "trusted": True,
        "settings_local": str(settings),
        "hook": f"{python or 'python3'} -m {HOOK_MODULE}",
    }
