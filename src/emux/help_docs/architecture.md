---
title: Product boundaries
summary: Understand the difference between Emux operational state, Fleet learning, and assistant history.
order: 35
---

# Product boundaries

Emux owns operational state needed to observe and control active sessions. It is not a semantic or product-memory store.

Fleet owns durable learning from Emux usage. Eidos Omni is Fleet's memory authority. Emux must not create a parallel learned-memory system.

The help assistant performs deterministic lookup over this versioned documentation corpus. Its browser-local conversation history is only temporary UI state: it is not training data, user memory, operational state, or Fleet memory. Clear removes that history from the current browser.
