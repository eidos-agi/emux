# Emux permanent handoff + verify procedure

**Status:** active · 2026-07-28  
**Applies to:** every emux product (amux, gmux, reevux, directmux/directrux, bare emux, hostkey seats)  
**Related:** hostkey dual-seat [EID-1146](https://linear.app/eidos-agi/issue/EID-1146) · Directmux rehome

## Problem this solves

AI chats are ephemeral. “I told the other agent” is not a handoff.  
A handoff is **durable knowledge + a live seat + a quiz the source agent grades**.

## Dual-seat model (product-wide)

| Seat | Name pattern | Role |
|------|----------------|------|
| **Source** | wherever work happened (Grok Build, Claude Code, Slack chat seat) | Produces KNOWLEDGE.md |
| **Home** | `<product>-this-chat` | Memory seat that owns the mission after handoff |
| **Builder** (optional) | `<product>-build` or product-specific (e.g. `hostkey-kai-handoff`) | Write-heavy work that must not steal the chat pane |

Rules:

1. Source **writes** `KNOWLEDGE.md` (authoritative). Never “trust the chat transcript alone.”
2. Home seat **reads** KNOWLEDGE; source **verifies** with a quiz until `READY_FOR_HANDOFF=yes`.
3. Builder seats stay separate when chat must stay responsive (Slack/hostkey pattern).
4. Rescue/recreate of a chat seat must **not** wipe builder state.

## Artifacts (required)

| File | Owner | Purpose |
|------|--------|---------|
| `KNOWLEDGE.md` | product repo root + seat state dir | Full mission memory |
| `STANDING.md` | seat state | Short “who you are / what you own” |
| `this-chat-handoff.md` | product repo | Human attach pointer + session ids |
| Registry row | `emux register` | Room visibility + tags |

## Compass lines every KNOWLEDGE should carry

1. What the product **is** (role, allowlist, room URL, brand vs on-disk id).
2. What was **proved green** (commands + PASS counts / screenshots).
3. What is **not** fixed (honest open gaps).
4. How to **dogfood** (ensure seats → headed tester → Linear).
5. **Do not** list (anti-patterns).

## CLI (engine)

```bash
# 1) Install knowledge into product home + create/register seat
emux handoff install \
  --product directmux \
  --repo ~/repos-aic/directmux \
  --knowledge ~/repos-aic/directmux/KNOWLEDGE.md \
  --source-session 019fa3d6-7e9b-7020-922c-21312f61cea5

# 2) Boot Claude in the seat (if shell-only)
emux handoff boot --product directmux

# 3) Verify: source agent grades the recap (blocks until READY or fail)
emux handoff verify --product directmux

# 4) Status
emux handoff status --product directmux
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | verify passed (`READY_FOR_HANDOFF=yes`) |
| 2 | seat missing / knowledge missing |
| 3 | verify failed (quiz incomplete) |
| 4 | AI not ready in pane |

## Verify protocol (non-negotiable)

Source agent **must** ask the home seat to:

1. Read `KNOWLEDGE.md` fully.
2. Recap: compass, product shape, proofs, open gaps, confidence.
3. Answer adversarial detail questions (IDs, numbers, what is NOT fixed).
4. Emit exactly: `READY_FOR_HANDOFF=yes` only when it can operate without the source thread.

Source agent **must not** mark handoff complete until those answers match known truth.

Reference quiz (adapt per mission):

- A key constant (e.g. backlog 5→256)
- A UI/product footgun (e.g. `#newmodal` not `#modal`)
- Ship status (PR number + what blocks merge)
- One “not fixed” that people mis-claim as fixed

## Product install

Every product package should ship:

```text
bin/handoff          # thin wrapper → emux handoff
briefs/ALWAYS.md     # pointer: handoffs use emux handoff procedure
docs/ or README      # link to this file
```

Directmux brand: UI says DIRECTMUX; config paths may still be `directrux` until EID-1147.

## Propagation checklist (new emux product)

- [ ] `bin/handoff` wrapper
- [ ] `ALWAYS.md` mentions handoff + verify
- [ ] `emux handoff install` dry-run succeeds
- [ ] One live verify pass on `<product>-this-chat`
- [ ] Room registry shows the seat

## What is not a handoff

- Pasting a paragraph into Slack without KNOWLEDGE.md
- Registering a session with no quiz
- “Claude was in the directory once”
- Claiming green without the product’s headed tester (Mafia for Directmux)
