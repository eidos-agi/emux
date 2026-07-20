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
import re
import time
from pathlib import Path
from typing import Any

DECISION_LOG_PATH = Path(
    os.environ.get("EMUX_DELEGATION_LOG")
    or (Path.home() / ".local" / "state" / "emux" / "delegation.jsonl")
)


def _san(value: Any) -> str:
    """Sanitize a field before it enters the durable decision log — screen text
    (workspace names, gate types) must never smuggle control chars into the
    ledger that a learner or auditor reads."""
    return re.sub(r"[^A-Za-z0-9_.:@/+-]", "_", str(value))[:160]


def log_decision(
    decision: str,
    reason: str | None,
    identity: str,
    server: str,
    workspace: str,
    gate_type: str,
    session: str | None = None,
) -> None:
    """Append one line for EVERY grant-answer outcome — allowed or denied, with
    the reason. This is the success/failure record for learning (which grants
    actually get used, which attempts get refused and why) and for security
    audit (an attempt to answer a gate without authority is a signal, not a
    silent no-op). Best-effort: logging must never block or break the decision."""
    rec = {
        "t": int(time.time()),
        "decision": decision if decision in ("allowed", "denied") else "unknown",
        "reason": _san(reason) if reason else None,
        "identity": _san(identity),
        "server": _san(server),
        "workspace": _san(workspace),
        "gate_type": _san(gate_type),
        "session": _san(session) if session else None,
    }
    try:
        DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DECISION_LOG_PATH.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass

GRANTS_PATH = Path(
    os.environ.get("EMUX_DELEGATIONS")
    or (Path.home() / ".config" / "emux" / "delegations.json")
)

# A HARD backstop independent of any grant: only these gate types may EVER be
# answered by delegated authority, no matter what a grant lists. A grant is
# necessary but not sufficient — the gate type must also be in here. Start with
# only the trust-folder gate (accepting your own directory is not a consequential
# act). Command approvals, MCP-trust, hook-review, and software-update are
# deliberately absent and stay human-only. Widening this set is a founder
# decision (EID-874), not a config change.
GRANTABLE_GATE_TYPES = frozenset({"trusted_workspace"})


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


def _read_store() -> dict[str, Any]:
    """Full grant store with both active `grants` and a `revoked` audit trail.
    `_load_grants` reads only `grants`, so a revoked grant never authorizes."""
    try:
        data = json.loads(GRANTS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["version"] = 1
    if not isinstance(data.get("grants"), list):
        data["grants"] = []
    if not isinstance(data.get("revoked"), list):
        data["revoked"] = []
    return data


def _write_store(store: dict[str, Any]) -> None:
    GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRANTS_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(store, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, GRANTS_PATH)


def add_grant(
    identity: str,
    server: str,
    workspace: str,
    gate_types: list[str],
    ttl_seconds: float,
    now: float | None = None,
) -> dict[str, Any]:
    """Create a scoped grant. SAFETY: refuses to even persist a grant for a
    gate type not in GRANTABLE_GATE_TYPES — you can't stage authority for a
    gate the answer path would never honor anyway (defense in depth)."""
    if not all(isinstance(x, str) and x for x in (identity, server, workspace)):
        raise ValueError("identity, server, and workspace are required")
    types = sorted({g for g in gate_types if isinstance(g, str) and g})
    if not types:
        raise ValueError("at least one gate_type is required")
    ungrantable = [g for g in types if g not in GRANTABLE_GATE_TYPES]
    if ungrantable:
        raise ValueError(f"gate types are not grantable: {ungrantable}")
    if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive number")
    clock = time.time() if now is None else now
    grant = {
        "identity": identity,
        "server": server,
        "workspace": workspace,
        "gate_types": types,
        "created_at": clock,
        "expires_at": clock + ttl_seconds,
    }
    store = _read_store()
    store["grants"].append(grant)
    _write_store(store)
    return grant


def revoke_grants(
    identity: str | None = None,
    server: str | None = None,
    workspace: str | None = None,
    now: float | None = None,
) -> int:
    """Revoke all active grants matching the given filters (None = any). Moves
    them to the `revoked` audit list with a timestamp; they stop authorizing
    immediately. Returns the count revoked."""
    clock = time.time() if now is None else now
    store = _read_store()
    keep, revoked = [], []
    for g in store["grants"]:
        matches = (
            (identity is None or g.get("identity") == identity)
            and (server is None or g.get("server") == server)
            and (workspace is None or g.get("workspace") == workspace)
        )
        (revoked if matches else keep).append({**g, "revoked_at": clock} if matches else g)
    if revoked:
        store["grants"] = keep
        store["revoked"] = store["revoked"] + revoked
        _write_store(store)
    return len(revoked)


def list_grants() -> dict[str, Any]:
    """The full store (active + revoked) for display/audit. Never authorizes."""
    return _read_store()


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
