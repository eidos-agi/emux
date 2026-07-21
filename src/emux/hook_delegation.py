#!/usr/bin/env python3
"""PreToolUse permission hook — enforce scoped delegation at Claude Code's OWN
permission boundary (EID-874, pivoted from screen-scraping).

Two adversarial audits proved you cannot safely decide "is this action allowed?"
from agent-controlled terminal text — the agent writes the screen. Claude Code's
PreToolUse hook instead hands us the REAL structured call on stdin:

    {"tool_name": "Edit", "tool_input": {"file_path": ".../secrets.py"}, "cwd": ...}

The model cannot forge the tool name or input, so every laundering attack from
the scraping approach is structurally impossible here. We return, on stdout:

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "allow"|"deny"|"ask",
                            "permissionDecisionReason": "..."}}

Contract verified against the installed Claude Code 2.1.215 binary + the bundled
hook-development validate-bash example (2026-07-20).

POLICY (deny-by-default, the sound half of today's work, reused verbatim):
  - a decision comes from a delegation grant for {identity, server, workspace},
    keyed by an ALLOWLIST of tool patterns a grant may ever cover;
  - anything not explicitly allowed → "ask" (hand back to the human — NEVER a
    silent allow). We never emit "deny" for un-granted actions: that would break
    the session; "ask" keeps the human in the loop, which is the whole point;
  - every decision is written to the delegation decision log (learning + audit).

This never presses a key and never reads the screen. It is consulted by Claude
Code itself, before the tool runs.
"""

from __future__ import annotations

import json
import os
import sys

# Import policy from the installed emux package if available; fall back to a
# vendored deny-by-default so the hook is safe even standalone.
try:
    from emux import delegation  # type: ignore
except Exception:  # noqa: BLE001
    delegation = None  # type: ignore


def _identity() -> str:
    """Who this session acts as — from the environment the operator set when
    launching the worker (never from anything the model can influence)."""
    return os.environ.get("EMUX_IDENTITY") or os.environ.get("USER") or "unknown"


def _server() -> str:
    return os.environ.get("EMUX_SERVER_ID") or os.uname().nodename


def _decision(kind: str, reason: str, tool: str, workspace: str, identity: str, server: str):
    if delegation is not None:
        try:
            delegation.log_decision(
                "allowed" if kind == "allow" else "denied",
                f"{reason}:{tool}", identity, server, workspace, tool,
            )
        except Exception:  # noqa: BLE001 — logging must never break the decision
            pass
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))
    # exit 0 with allow/ask on stdout; exit 2 is reserved for a hard deny.
    sys.exit(2 if kind == "deny" else 0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed input → don't guess. Ask the human. (No stdout body needed;
        # exit 0 falls through to Claude Code's normal permission flow.)
        sys.exit(0)

    tool = str(payload.get("tool_name") or "")
    cwd = str(payload.get("cwd") or "")
    workspace = os.path.realpath(cwd) if cwd else ""
    identity, server = _identity(), _server()

    if delegation is None or not tool:
        # No policy engine or no tool name → fall through to the app's own
        # permission flow (which prompts the human). Safe default.
        sys.exit(0)

    # Consult the grant policy at the tool-pattern granularity. A grant lists
    # tool patterns (e.g. "Read", "Edit", "Bash(git *)") it authorizes for this
    # {identity, server, workspace}. may_answer_gate is reused as the exact,
    # unexpired, deny-by-default matcher — here the "gate_type" is the tool.
    if delegation.may_answer_gate(identity, server, workspace, tool):
        _decision("allow", "granted", tool, workspace, identity, server)
    # Not covered by a grant → hand to the human. Never a silent deny/allow.
    _decision("ask", "no_grant", tool, workspace, identity, server)


if __name__ == "__main__":
    main()
