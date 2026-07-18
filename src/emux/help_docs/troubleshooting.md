---
title: Troubleshooting
summary: Diagnose missing sessions, stale panes, and browser connection problems.
order: 40
---

# Troubleshooting

If the control room reports that tmux is unavailable, confirm tmux is installed on the machine running Emux.

If a registered session is shown as gone, confirm the underlying tmux session still exists and that its registry target is correct. Remote sessions also require the configured SSH host to be reachable from the Emux server.

If the browser cannot load API data, check the daemon health at `/healthz`, then verify the requested Host matches the configured local or public origin. A foreign Host is rejected to prevent DNS rebinding.

If this assistant cannot find an answer, use the linked source pages or open the project issue tracker. It will not invent a command that is absent from the documentation.
