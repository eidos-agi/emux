# Emux vocabulary — Sessions · Heads · CHATS

**Linear:** [RVS-371 MASTER PLAN](https://linear.app/eidos-agi/issue/RVS-371) (Directrux)

## Frozen line

> **Sessions** run. **Heads** are how you attach. **CHATS** are what you recover when nothing is live.

“Terminal” is chrome (“open head in iTerm”), not the fleet noun.

## Dictionary

| Word | Meaning | Operator action |
|------|---------|-----------------|
| **Session** | Named live unit (tmux + emux registry) | Register, list, drive, poll |
| **Head** | Interactive attach surface on a session (iTerm / Terminal / room head mode) | `emux head <name>` or **OPEN HEAD** in the room |
| **CHATS** | Past transcripts on disk (Claude / Grok) | Browse archive; resume into a session |
| **Pane** | Raw tmux surface (capture/send) | Technical; prefer *session* / *head* in product copy |
| **Mission** | Intent of work (plan / Linear / channel) | Not the I/O surface |
| **Channel** | Durable domain memory over sessions | Notes + matchers, not a head |

## Flows

```
CHATS (pick past) → resume into a session → OPEN HEAD (type/drive)
register / resume session → OPEN HEAD in amux/gmux/reevux room
```

| Wrong | Right |
|-------|--------|
| “Move this into CHATS” (when you want a live window) | Register/resume **session**, **open a head** |
| Live room mode called **chat** | Live mode is **head** (legacy URL `view=chat` still maps to head) |
| “Terminal” as the registry object | **Session**; open a **head** |

## UI mapping (room)

| Surface | Name | Contents |
|---------|------|----------|
| Past | **CHATS** | Disk transcripts; resume spawns/adopts a **session** |
| Active grid | **GRID** (sessions) | Live tiles |
| Live full view | **HEAD** (`view=head`) | One session, type into pane |
| Modal button | **OPEN HEAD** | iTerm / `emux head` attach |
| Orphans | **OPEN HEAD** | Adopt live tmux into a head |

## Skins

amux / gmux / reevux inherit this dictionary from emux. Directrux uses it as the meta-plane language for steering all four.

## Compatibility

- URL/localStorage `view=chat` → treated as **head**
- DOM for CHATS tiles may still use CSS class `.tile.chat` (archive cards)
- API paths `/api/chats*` remain (past transcripts store)
