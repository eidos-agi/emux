# Emux handoff (thin)

**Write KNOWLEDGE → install seat → structural verify → attach and work there.**

Optional: `boot` + `quiz`. Do not thrash the LLM for green.

## Commands

```bash
# 1. Write KNOWLEDGE.md in the product repo (one page is enough)

# 2. Park it on a seat
emux handoff install \
  --product directmux \
  --repo ~/repos-aic/directmux \
  --knowledge ~/repos-aic/directmux/KNOWLEDGE.md \
  --source-session <chat-id>

# 3. Ship gate (no LLM)
emux handoff verify --product directmux

# 4. Optional
emux handoff boot --product directmux
emux handoff quiz --product directmux   # one-shot; sole-line READY_FOR_HANDOFF=yes
```

Product wrapper: `bin/handoff` (same subcommands).

## What install does

- Copies **only** `KNOWLEDGE.md` (+ `this-chat-handoff.md`)
- Creates/registers `<product>-this-chat` if needed  
- **Does not** overwrite product `CLAUDE.md` / `ALWAYS.md` / `AGENTS.md`

## Structural verify (default gate)

- tmux seat exists  
- `KNOWLEDGE.md` in state + repo, ≥5 lines  

That means **data landed**, not “agent took ownership.” Ownership = someone works in the seat.

## Dual seats (when chat must stay responsive)

| Seat | Role |
|------|------|
| `<product>-this-chat` | Memory / light steering |
| `<product>-build` (optional) | Write-heavy work |

## Anti-patterns

- Paste without `KNOWLEDGE.md`  
- Claim handoff complete with only structural green and no successor work  
- Overwrite product agent briefs with a mission pack  
- Quiz thrash until exit 0  
