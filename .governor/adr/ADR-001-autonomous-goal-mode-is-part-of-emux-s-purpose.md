---
id: "ADR-001"
type: "decision"
title: "Autonomous goal mode is part of emux's purpose"
status: "accepted"
date: "2026-07-03"
---

emux expands from session picker/driver to autonomous operator: goal mode (observe->act->judge loop, pursue()) drives whole tasks through a TUI. The never-spawn/never-kill invariant is preserved and is the telos metric (ns_1271c383bf3e). navigate/goal require the claude CLI on PATH (fixed-cost subscription tool, never the Anthropic API). Destructive-action gating is a deliberate non-goal. Full text: docs/decisions/0001-autonomous-goal-mode.md
