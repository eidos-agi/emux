---
title: Getting started
summary: Open the control room and begin observing and steering a live AI tree.
order: 10
---

# Getting started

Emux is the human control room for trees of AIs. It observes AI sessions that already exist, renders manager-to-agent relationships, shows their live state, and lets an authorized operator steer any node in the tree.

Start the local web control room with `emux web`. By default it binds to `127.0.0.1:8689`. Open `http://127.0.0.1:8689` on the same computer.

Begin with Flow to understand the manager-to-agent tree. Use Grid for a dense overview, Groups and channels to organize persistent bodies of work, and Activity for recent changes. Select any node to inspect and steer it.

Emux does not treat localhost as authentication. Keep the default loopback binding unless a trusted reverse proxy provides authentication and authorization.
