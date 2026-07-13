# emux login — manager-operable login gates

## The problem

When a managed Claude Code session hits a login gate (logged out, `/login`
mid-sequence, wrong account, OAuth URL + paste-code prompt), supervision
dead-ends: the manager can see the pane but has no way to change the login or
navigate the sequence. A human has to find the pane by hand.

## Detect: the `login_gate` flag

The Tier-0 classifier (`emux.judge`) recognises a login/auth sequence on
screen — logged-out banners ("Invalid API key - Please run /login"), the
"Select login method" picker, `claude.ai/oauth` / `console.anthropic.com/oauth`
URLs, the "Paste code" prompt, "logged out" confirmations. Any of these
classify as `waiting_human` with flag **`login_gate`** and the summary "Needs
login", so `tmux_classify` surfaces it as actionable, not as a generic
approval prompt. "Login successful" is deliberately NOT a gate — that means
the sequence is over.

## Navigate: `emux login` / `tmux_login`

The login TUI is a fixed sequence, so the driver is deterministic keystrokes —
no model calls. The only part emux cannot do is the browser sign-in, so the
flow is two calls with the OAuth URL handed to you in between:

```
emux login <target> [-n]            # start: sends /login (or resumes a
                                    # mid-sequence screen), picks the default
                                    # login method, prints the OAuth URL
# ... open the URL in a browser, sign in, copy the authorization code ...
emux login <target> [-n] --code <c> # finish: pastes the code, verifies
                                    # "Login successful" on screen
emux login <target> [-n] --switch   # change account: /logout first, then start
```

MCP equivalent: `tmux_login(target, code=None, switch=False,
by_registry_name=False)`. Same two-call shape; the start call returns
`{ok, logged_in: false, url, next}`, the finish call returns
`{ok, logged_in: true}`.

Notes:

- Works by registry name across hosts (`-n`): every keystroke and capture runs
  through the same ssh-aware tmux transport as the rest of emux.
- The pane captures with `-J` (join wrapped lines) so the OAuth URL survives
  narrow panes.
- The authorization code is sent as keystrokes only — never stored, and
  redacted from the emux audit trail.
- Refuses panes not running claude (`not_a_claude_pane`) — if claude exited,
  restart it in the session first.
- Unrecognised screens bail with `unrecognized_screen` and the screen tail —
  fall back to `emux navigate` / `tmux_navigate` for anything exotic.

## Checks

`tests/test_login.py` exercises the pure step-decision (`_login_step`) and the
whole flow against a scripted fake tmux (zero real sessions, zero tokens);
`tests/test_classify.py` covers the `login_gate` classifier cases.
