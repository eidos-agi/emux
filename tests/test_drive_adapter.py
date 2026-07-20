"""EID-875 — an agent has MANY drive adapters; structured -p is just one of
Claude's. The load-bearing property: the registry is keyed by mechanism and
adapters_for(agent) returns a list, so one agent can hold several."""

from __future__ import annotations

from typing import Any

from emux import drive_adapter as da
from emux import structured_driver as sd


def test_structured_is_registered_as_one_claude_adapter():
    a = da.get("claude-structured")
    assert a is not None and a.agent == "claude"
    assert "structured" in a.traits and "resumable" in a.traits
    # it is A claude adapter, in a LIST — the shape supports many
    claude_adapters = da.adapters_for("claude")
    assert any(x.name == "claude-structured" for x in claude_adapters)
    assert isinstance(claude_adapters, list)


def test_registry_is_keyed_by_mechanism_so_claude_can_have_many():
    # Register two more Claude adapters (stubs) and prove they coexist — Claude
    # is not limited to one adapter; none is privileged.
    class _Stub:
        def __init__(self, name, traits):
            self.name, self.agent, self.traits = name, "claude", frozenset(traits)
        def drive(self, task: str, cwd: str, **kw: Any):  # pragma: no cover
            return None

    da.register(_Stub("claude-sdk", {"persistent", "long_run"}))
    da.register(_Stub("claude-terminal", {"observe", "universal_fallback"}))
    names = {a.name for a in da.adapters_for("claude")}
    assert {"claude-structured", "claude-sdk", "claude-terminal"} <= names
    # select can pick by trait among Claude's several adapters
    assert da.select("claude", need="persistent").name == "claude-sdk"
    assert da.select("claude", need="observe").name == "claude-terminal"


def test_conforms_to_protocol():
    assert isinstance(sd.StructuredClaudeAdapter(), da.DriveAdapter)
