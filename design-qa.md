# Emux docs assistant design QA

- Source visual truth: the five Claude documentation assistant references supplied by the user:
  - `/home/eidos/.codex/attachments/18c91631-ed0e-4643-a82e-9f70059bd732/codex-clipboard-260de5f4-471b-4917-8673-36c7c8b90351.png`
  - `/home/eidos/.codex/attachments/a50b94cf-7945-4419-a57c-0feb263d8037/codex-clipboard-adf36d19-8ea5-4135-9761-f01eac3daa8c.png`
  - `/home/eidos/.codex/attachments/43b5d66a-fec5-4f6e-a78e-35bb1fa433b9/codex-clipboard-98d72d1d-1c89-4144-ba37-33e17e3a42c6.png`
  - `/home/eidos/.codex/attachments/d5eb2682-59e2-4011-bf12-61989024071a/codex-clipboard-8884f587-ede6-4924-9482-b83a43ea02eb.png`
  - `/home/eidos/.codex/attachments/13cfc972-6ed3-4d8c-94f6-800f2ab120ec/codex-clipboard-2839d603-95e9-48f5-93c1-35c13e0c4f1b.png`
- Browser-rendered implementation screenshots:
  - `docs-desktop.png` — desktop docs surface
  - `docs-assistant-answer.png` — desktop answer state
  - `docs-mobile-open.png` — 390 × 844 docs assistant open state
  - `control-room-mobile-help.png` — 390 × 844 shared control-room assistant
- Viewports: browser default desktop; 390 × 844 mobile.
- States exercised: closed, open, answer with sources, no-answer, forced network error, clear, persisted history across docs/control-room, Escape close.
- Primary interactions tested: open, submit by Enter, source links present, feedback, copy control present, retry, clear, expand control present, close by Escape.
- Browser console/errors: no console messages or page errors after desktop and mobile flows.

## Findings

- [P1] Visual comparison input is unavailable.
  - Location: final screenshot-to-reference comparison.
  - Evidence: browser screenshots were captured successfully, but the image-view sandbox helper fails with `bwrap: loopback: Failed RTM_NEWADDR`, so source and implementation images could not be opened together in one comparison input.
  - Impact: interaction, dimensions, overflow, accessibility tree, and browser errors are verified, but precise typography, spacing, colors, and visible polish cannot honestly be certified against the Claude references.
  - Fix: reopen the saved implementation screenshots and supplied source references in a working visual comparison surface, record mismatches, and iterate before promotion.

## Required fidelity surfaces

- Fonts and typography: implementation uses the existing Emux IBM Plex Mono and VT323 system while preserving the reference's large assistant heading and restrained hierarchy; final pixel-level comparison is blocked.
- Spacing and layout rhythm: right panel is 480px desktop, full-width 390px mobile; measured mobile panel is exactly 390 × 844 with no docs overflow. Final visible comparison is blocked.
- Colors and visual tokens: implementation reuses Emux amber, cream, line, text, and raised-surface tokens. Final sampled comparison is blocked.
- Image quality and asset fidelity: no raster imagery, logos, illustrations, or custom image assets appear in the target component; controls use text rather than fabricated icon art.
- Copy and content: Assistant, grounded-result label, composer, disclaimer, sources, feedback/copy/retry, and empty/loading/no-answer/error semantics are implemented. Product-memory boundaries are explicit.

## Comparison history

- Functional browser iteration: initial server launched from the installed checkout and returned the old 404 route; restarted with the isolated worktree explicitly selected and captured all routes/states.
- Responsive iteration: 390px docs surface measured `innerWidth=390`, `scrollWidth=390`, and a 390px assistant panel. The control room retains its pre-existing 1286px mobile document overflow, owned by EID-789; the assistant itself remains 390px.
- No visual mismatch iteration could be performed because the required combined image comparison could not be opened.

## Implementation checklist

- Re-run combined source/implementation visual comparison when image viewing works.
- Resolve any P0/P1/P2 visual differences found.
- Re-capture desktop and 390px states and change final result to passed only after comparison.

final result: blocked
