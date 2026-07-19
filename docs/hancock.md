# Hancock integration

Emux pins the complete [Hancock](../integrations/hancock) product as a git
submodule. Hancock remains independently buildable and releasable; emux does
not copy, wrap, or reduce it to the small approval surface in `web.py`.
It is a product with CLI, MCP, API, device, and UI surfaces; the bundled agent
skill teaches callers how to use those surfaces, rather than being the product.

## Product map

| Surface | Hancock authority | Role |
| --- | --- | --- |
| CLI, signing TUI, policy, audit | `cli/` | Local SQLite-backed command requests, delegation-license decisions, signed execution, audit, and `hancock mcp`. |
| MCP and agent skill | `cli/mcp.go`, `cli/skills/` | Blocking `request`/`wait` path plus usage/verification instructions for agents; register `hancock mcp` as the MCP server. |
| API, Postgres, SSE | `packages/server/`, `packages/registry/`, `docker-compose.yml` | Hono approval broker, durable Postgres request/event storage, trusted-device routes, and live event stream. |
| Dashboard and client | `packages/dashboard/`, `packages/client/` | Next.js approval UI and TypeScript client/request-and-wait integration. |
| Delegation, capabilities, registry | `packages/delegation/`, `packages/capabilities/`, `packages/registry/` | Capability taxonomy, scoped delegation tokens and constraints, verification, and persisted identities/requests/events. |
| Touch ID and menu bar | `apps/mac-touchid-helper/` | Swift biometric approval helper, device signing/keychain support, and macOS menu-bar approver. |
| Telegram | `bridge/` | Outbound long-poll bridge that exposes approve/deny to one allowlisted Telegram chat without an inbound public endpoint. |
| Product, security, and operations | `docs/`, `CHARTER.md` | North star, infrastructure/security model, PRDs, launch checklist, support runbook, and audit expectations. |

Emux's existing `src/emux/web.py` integration is only an operator convenience:
it reads the local Hancock SQLite queue, invokes the installed `hancock` CLI
for approval, records denial, and files `emux head <session>` escalations. It is
not Hancock's API, policy engine, dashboard, device client, or product boundary.

## WCS integration truth

The broker request contract already requires `requesterId`, `targetSystem`, and
a non-empty structured or string `scope`. Delegation tokens also support scoped
capabilities and constraints. Those are the correct inputs for a future WCS
adapter.

Hancock does **not** currently ship WCS admin policy evaluation by endpoint,
user, group, and project context. WCS must map that context into the request and
delegation contract, evaluate its admin rules (or call a future Hancock policy
engine), and fail closed when no rule grants the requested scope. Do not present
the current CLI command classifier or generic delegation verification as that
WCS-specific policy layer.

## Checkout and verification

```bash
git clone --recurse-submodules https://github.com/eidos-agi/emux.git
git submodule update --init --recursive

# Hancock terminal tier
cd integrations/hancock/cli
go test ./...

# Hancock TypeScript packages
cd ..
corepack pnpm install --frozen-lockfile
corepack pnpm test

# Hancock Mac helper
corepack pnpm apps:mac:test

# Emux
cd ../..
uv run pytest -q
```

Update the pin only after Hancock's target commit is pushed and verified:

```bash
git -C integrations/hancock fetch origin main
git -C integrations/hancock checkout <verified-commit>
git add integrations/hancock
```
