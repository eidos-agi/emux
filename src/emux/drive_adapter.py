"""Drive-adapter registry — how emux drives an agent toward a task.

A drive adapter is keyed by MECHANISM, belongs to an AGENT, and one agent has
MANY (EID-875, founder correction 2026-07-20). Claude alone exposes several:

    claude-structured   `claude -p --output-format json` (+ PreToolUse hook)
    claude-sdk          Claude Agent SDK (persistent, long runs)
    claude-terminal     tmux capture/send of the interactive TUI
    claude-mcp          ... etc

and Codex / other agents have their own sets. NONE is "the way": the orchestrator
picks per {agent, task, need}. This module is only the seam that keeps any one
mechanism from being privileged — it does not build all backends (YAGNI). New
adapters register themselves.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DriveAdapter(Protocol):
    """One way to drive one agent. `name` is the mechanism id (e.g.
    'claude-structured'); `agent` is what it drives (e.g. 'claude'); `traits`
    advertise fit so the orchestrator can choose among an agent's several
    adapters (e.g. {'structured', 'resumable', 'permission_hook'} vs
    {'persistent', 'long_run'} vs {'observe', 'universal_fallback'})."""

    name: str
    agent: str
    traits: frozenset[str]

    def drive(self, task: str, cwd: str, **kwargs: Any) -> Any: ...


_REGISTRY: dict[str, DriveAdapter] = {}


def register(adapter: DriveAdapter) -> DriveAdapter:
    """Register a drive adapter by its mechanism name. Idempotent per name."""
    _REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> DriveAdapter | None:
    return _REGISTRY.get(name)


def adapters_for(agent: str) -> list[DriveAdapter]:
    """Every registered adapter that drives `agent`. Returns a LIST because an
    agent (Claude included) has many — this is the load-bearing shape."""
    return [a for a in _REGISTRY.values() if a.agent == agent]


def select(agent: str, need: str | None = None) -> DriveAdapter | None:
    """Pick an adapter for `agent`, optionally requiring a trait. Deliberately
    dumb for now (first match); a real chooser weighs traits vs the task."""
    candidates = adapters_for(agent)
    if need:
        candidates = [a for a in candidates if need in a.traits]
    return candidates[0] if candidates else None


def registry() -> dict[str, dict[str, Any]]:
    """The full adapter table, for display/audit."""
    return {
        a.name: {"agent": a.agent, "traits": sorted(a.traits)}
        for a in _REGISTRY.values()
    }
