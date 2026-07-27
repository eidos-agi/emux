# Grok control plane (emux slice)

Emux used to treat Grok Build as **disk scan + bare `grok` spawn**. This slice
adds a small pure-Python module (`emux.grok_control`) that maps onto Grok's
*documented* control surfaces so fleet resume, headless steer, and future ACP
clients share one source of truth.

Reference tree (when present): `/tmp/grok-build` (open-source crates under
`crates/codegen/`).

## Surfaces → emux helpers

| Grok surface | Where it lives | emux helper |
|---|---|---|
| Session index | `~/.grok/sessions/**/summary.json` | `enrich_session_dir`, `iter_session_dirs` |
| Conversation log | `updates.jsonl` (authoritative restore) | `last_agent_snippet_from_updates` |
| Chat transcript | `chat_history.jsonl` | `last_user_from_chat_history`, `chats.peek_chat` |
| Live processes | `~/.grok/active_sessions.json` | `chats._grok_live_ids` (unchanged) |
| CLI binary | `~/.grok/bin/grok`, `EMUX_GROK_BIN` | `resolve_grok_bin` |
| Interactive resume | `grok --resume <id>` (`-r`) | `resume_argv`, `resume_shell_command` |
| Headless single-turn | `grok -p "…"` (`--single`) | `headless_steer_argv` |
| Headless resume | `grok -p "…" -r <id>` or `-c` | `headless_steer_argv(session_id=…)` |
| ACP / stdio agent | `grok agent stdio` | `acp_stdio_argv`, `emux.acp_grok` (Phase C client) |
| Hooks (file-based) | `~/.grok/hooks/*.json`, `<proj>/.grok/hooks/` | `write_hooks_bridge`, `hook_bridge_payload` |

## Open-source crate map

| Concern | Crate / docs (under grok-build) |
|---|---|
| TUI + CLI flags | `xai-grok-pager` — user guide sessions (`17-sessions.md`), hooks (`10-hooks.md`) |
| Session lifecycle | `xai-agent-lifecycle` — session contributors |
| Hooks runtime | `xai-grok-hooks` — event types, examples (`PreToolUse`, `Stop`, …) |
| ACP transport | `xai-grok-mcp` (`acp_transport`), pager `app/acp_handler` |
| Stdio test client | `xai-grok-test-support` — `GrokStdioClient` / `agent-client-protocol` |
| Shell session types | `cli-chat-proxy-types` / pager session modules |

## What landed in this slice

1. **Richer scan** — `chats.scan_grok` uses `enrich_session_dir` (title/summary/model/branch + optional history snippets) and copy-paste resume as `grok --resume <id>` (absolute bin when resolved).
2. **Fleet resume** — `web._resume_chat_in_fleet` for tool=grok uses absolute `grok` + `--resume <id>` (same pattern as Claude). No more “spawn bare TUI then inject `/resume`”.
3. **Headless / ACP builders** — pure argv helpers; nothing shells out unless a caller does.
4. **Hooks bridge sketch** — writes Grok-format JSON that points at `python3 -m emux.hook_delegation` for `PreToolUse` + `Stop`. **Not wired for Grok stdin semantics yet** (delegation module still speaks Claude Code hook JSON).

### Phase A (session memory v2)

5. **Subagent filter** — skip `session_kind` starting with `subagent` (and `hidden`) by default; `include_subagents=True` or `EMUX_CHATS_INCLUDE_SUBAGENTS=1` to keep them.
6. **Last-user summary** — tail `updates.jsonl` `user_message_chunk` (and chat_history) preferred over title echo for CHATS triage.
7. **Watermarks** — `summary_mtime` / `updates_mtime` on enrich; stored as `src_*_mtime` in chats.db for dirty checks; optional fields `session_kind`, `agent_name`, `parent_session_id`.

### Phase B (headless steer)

8. **`run_headless_steer`** — subprocess `grok -p … -r <id> --output-format json --always-approve`.
9. **`POST /api/steer`** — `mode=auto|headless|pty`; auto picks headless for Grok (tags `grok` / `gsid:…`).
10. **Room modal** — Grok sessions SEND via `/api/steer` first, PTY fallback on failure.
11. **Resume tags** — `gsid:<uuid>` / `csid:<uuid>` so steer can resume the right transcript.

### Phase C (ACP client — first vertical)

12. **`emux.acp_grok.GrokAcpClient`** — NDJSON JSON-RPC over `grok agent --always-approve stdio`.
13. **`run_acp_prompt`** — one-shot initialize → session/load|new → session/prompt → close.
14. **`POST /api/steer` `mode=acp`** — same body as headless; returns `{mode: acp, text, session_id, …}`.
15. **Reverse-RPC** — auto-approve permission requests; refuse unsupported fs/terminal methods so turns never hang.

## Remaining gaps for full ACP

- **Long-lived process manager** — keep ACP child registered in emux registry (`tags: acp,grok`); multi-turn without re-spawn.
- **Streaming UI** — pipe `session/update` into fleet feed / room modal (today one-shot collects text then returns).
- **Permission / hook adapter** — map Hancock grants into ACP permission outcomes (beyond always-approve).
- **Leader mode** — multi-client share via `grok agent --leader` / `~/.grok/leader.sock`.
- **Remote hosts** — chat resume remains local-only (transcript stores on the daemon host).

## Operator knobs

```bash
export EMUX_GROK_BIN=$HOME/.grok/bin/grok   # launchd-safe absolute path
# optional: GROK_HOME=/custom/grok-state
```

Dry-run hooks bridge:

```python
from emux.grok_control import write_hooks_bridge
print(write_hooks_bridge(dry_run=True))
```
