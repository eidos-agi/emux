# Emux permanent handoff + verify procedure

**Status:** active · 2026-07-28  
**Applies to:** every emux product (amux, gmux, reevux, directmux/directrux, bare emux, hostkey seats)  
**Related:** hostkey dual-seat [EID-1146](https://linear.app/eidos-agi/issue/EID-1146)

## Problem

AI chats are ephemeral. “I told the other agent” is not a handoff.  
A handoff is **durable knowledge + a live seat + a deterministic structural check**.

Optional: an LLM quiz for confidence. **Do not thrash the LLM to get green.**

## Dual-seat model

| Seat | Name pattern | Role |
|------|----------------|------|
| **Source** | Grok Build / Claude Code / Slack chat seat | Writes `KNOWLEDGE.md` |
| **Home** | `<product>-this-chat` | Owns the mission after handoff |
| **Builder** (optional) | `<product>-build` or product-specific | Write-heavy work; keep chat responsive |

Rules:

1. Source **writes** `KNOWLEDGE.md` (authoritative).
2. **Structural verify** must pass (files + tmux seat). That is the ship gate.
3. **Quiz** is optional; source agent grades substance by eye if needed.
4. Rescue of a chat seat must not wipe builder state.

## Artifacts

| File | Purpose |
|------|---------|
| `KNOWLEDGE.md` | Mission memory (compass, proofs, open gaps, dogfood) |
| `STANDING.md` / `CLAUDE.md` / `AGENTS.md` | Copies of knowledge for agents in the seat cwd |
| `this-chat-handoff.md` | Human attach pointer |
| Registry row | Room visibility |

## CLI

```bash
# 1) Write KNOWLEDGE.md, then install seat
emux handoff install \
  --product directmux \
  --repo ~/repos-aic/directmux \
  --knowledge ~/repos-aic/directmux/KNOWLEDGE.md \
  --source-session <chat-id>

# 2) Structural verify (default gate — no LLM)
emux handoff verify --product directmux

# 3) Optional: boot Claude + one-shot quiz
emux handoff boot --product directmux
emux handoff quiz --product directmux

# 4) Status
emux handoff status --product directmux
```

Product wrapper: `bin/handoff` → same subcommands.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | structural verify pass / quiz pass |
| 2 | missing product/seat/knowledge |
| 3 | structural or quiz fail |

### Structural verify checks

- tmux session `<seat>` exists  
- `$STATE/KNOWLEDGE.md` and `$REPO/KNOWLEDGE.md` exist and are ≥5 lines  
- `$REPO/this-chat-handoff.md` exists  

No Claude UI sniffing. No retries. No multi-capture settle loops.

### Quiz (optional)

One `emux ask`. Success only if the reply contains a **sole line**  
`READY_FOR_HANDOFF=yes` (not mid-sentence, not from the prompt blob).  
If flaky: fix ask/capture, or grade by hand — **do not** add thrash retries.

## What is not a handoff

- Paste without `KNOWLEDGE.md`
- Register a session with no knowledge file
- “Claude was in the directory once”
- Claiming green by running quiz until it passes under load

## Propagation

- [ ] Product has `bin/handoff`
- [ ] `ALWAYS.md` points here
- [ ] `emux handoff install` + `verify` succeed
