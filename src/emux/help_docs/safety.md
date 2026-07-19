---
title: Safety and remote access
summary: Keep control actions private and publish Emux only through a trusted identity boundary.
order: 30
---

# Safety and remote access

The help assistant is read-only. It searches this documentation corpus and cannot send keys, start sessions, approve requests, or invoke any control endpoint.

For remote access, keep the Emux service on a loopback or private socket. Put authentication and authorization at a trusted reverse proxy, strip spoofed identity headers, and authorize immutable user identifiers in the application boundary.

State-changing requests require same-origin protection. Streaming control paths must enforce the same identity as ordinary HTTP requests.
