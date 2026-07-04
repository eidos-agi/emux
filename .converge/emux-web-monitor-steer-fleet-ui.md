# emux web — monitor + steer fleet UI

Generated: 2026-06-15T18:52:27Z
Scope: Record the target, proof envelope, known gaps, and next repair command.

## Canonical Contracts

- none recorded yet

## Proof Artifacts

- none recorded yet

## Current Status

Record the current pass, partial, blocked, and gap rows here. Do not mark a
browser/API full loop as real-surface pass unless its proof envelope records the
actual surface or `base_origin` exercised.

## Next Command

```bash
uv run pytest -q && emux web (manual real-surface checks at http://127.0.0.1:8689)
```

## Secret Hygiene

Do not commit raw recovery links, token hashes, access tokens, refresh tokens,
passwords, TOTP secrets, TOTP codes, otpauth URIs, JWTs, or provider secrets.
