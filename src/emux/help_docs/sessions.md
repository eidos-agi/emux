---
title: Sessions and registration
summary: Register existing tmux sessions and describe their relationships.
order: 20
---

# Sessions and registration

Emux separates live tmux state from descriptive registry metadata. A registered session may be local or remote, and a live unregistered tmux session can still appear in the control room.

Register an existing session with `emux register NAME SESSION`. Add `--tags TAG` to organize it. Use `--manages OTHER` to describe an agent that manages another registered agent; Flow renders these relationships.

Registration does not start, stop, or take ownership of the underlying tmux session. Live truth continues to come from tmux.
