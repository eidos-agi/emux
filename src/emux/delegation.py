"""Scoped delegation grants — the bridge from per-action approval to
pre-granted authority (EID-874; Vybhav's question, 2026-07-20).

The autonomy blocker is not the decision and not capability; it is that
authority is checked per-action, inline, with NO MEMORY, so every consequential
step falls to a human. A grant moves the decision OUT of the execution path and
UP to a durable, scoped authorization a human makes once, up front:

    "identity X may answer gate types {..} on server/workspace, until expiry."

Then an agent accepts its own next steps *within scope* — no human per step.

SAFETY BOUNDARY: this module is the SUBSTRATE only. `may_answer_gate` computes
whether an identity is pre-authorized; it does NOT answer any gate. Wiring a
grant to the live gate-answer path (`tmux_approve_gate`) is a separate,
deliberate, founder-scoped step (EID-874 stage 2). Deny by default — any
missing field, malformed grant, unknown gate type, or expiry ⇒ no authority.
No wildcards: gate types are matched exactly, so a grant can never silently
widen to a gate it did not name.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

GRANTS_PATH = Path(
    os.environ.get("EMUX_DELEGATIONS")
    or (Path.home() / ".config" / "emux" / "delegations.json")
)


def _load_grants() -> list[dict[str, Any]]:
    """Read the grant store fresh (so minting/revoking takes effect at once).
    A malformed or missing store yields no grants — deny by default."""
    try:
        data = json.loads(GRANTS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("grants"), list)
    ):
        return []
    return [g for g in data["grants"] if isinstance(g, dict)]


def may_answer_gate(
    identity: str,
    server: str,
    workspace: str,
    gate_type: str,
    now: float | None = None,
) -> bool:
    """True ONLY on an exact, unexpired grant matching all of
    {identity, server, workspace} with gate_type in the grant's explicit
    gate_types. Everything else — including any malformed grant — is False."""
    if not all(isinstance(x, str) and x for x in (identity, server, workspace, gate_type)):
        return False
    clock = time.time() if now is None else now
    for g in _load_grants():
        gate_types = g.get("gate_types")
        expires = g.get("expires_at")
        if (
            g.get("identity") == identity
            and g.get("server") == server
            and g.get("workspace") == workspace
            and isinstance(gate_types, list)
            and gate_type in gate_types
            and isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and clock < expires
        ):
            return True
    return False


def active_grants(identity: str, now: float | None = None) -> list[dict[str, Any]]:
    """Non-expired, well-formed grants for one identity — for display/audit.
    Never used to authorize; `may_answer_gate` is the only decision path."""
    clock = time.time() if now is None else now
    out = []
    for g in _load_grants():
        expires = g.get("expires_at")
        if (
            g.get("identity") == identity
            and isinstance(g.get("server"), str)
            and isinstance(g.get("workspace"), str)
            and isinstance(g.get("gate_types"), list)
            and isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and clock < expires
        ):
            out.append(g)
    return out
