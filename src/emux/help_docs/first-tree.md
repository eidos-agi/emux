---
title: Your first AI tree
summary: Register a manager and worker, view their relationship in Flow, and steer the tree safely.
order: 5
---

# Your first AI tree

Start two AI sessions in tmux: one manager and one worker. Emux does not start or own those processes; it observes and steers sessions that already exist.

Register both nodes with `emux register MANAGER MANAGER_SESSION` and `emux register WORKER WORKER_SESSION`. Describe the directed relationship by registering or updating the manager with `--manages WORKER`. Use tags to place both nodes in the same channel or body of work.

Open the control room with `emux web`, then choose Flow. The manager should appear above the worker with a directed edge between them. Select either node to inspect its live pane, state, detected AI, and recent operational activity.

Steer the manager with a bounded instruction. Let the manager delegate a concrete subtask to the worker, then watch both nodes rather than treating the sessions as unrelated terminals. Use Activity and the Feed to follow changes across the tree.

If a node reaches an approval gate, do not bypass it with raw terminal input. Inspect the exact gate, route consequential authorization through Hancock, and execute only when the live target still matches the approved fingerprint. The resulting request, decision, execution, and outcome receipts should identify the same action without storing prompt or secret content.

The goal is not merely to multiplex terminals. The goal is to let one human understand and direct a living hierarchy of AI work.
