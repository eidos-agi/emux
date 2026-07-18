---
title: Getting started
summary: Start the local control room and understand what Emux can safely operate.
order: 10
---

# Getting started

Emux is a control room for tmux sessions that already exist. It observes registered sessions, shows their live state, and lets an authorized operator steer them.

Start the local web control room with `emux web`. By default it binds to `127.0.0.1:8689`. Open `http://127.0.0.1:8689` on the same computer.

Use Grid for an overview, Groups to organize sessions by tags, Activity for recent changes, and Flow to see manager-to-worker relationships. Select a session to inspect and steer it.

Emux does not treat localhost as authentication. Keep the default loopback binding unless a trusted reverse proxy provides authentication and authorization.
