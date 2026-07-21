"""Durable management ledger — the manager's authority lives here, not in context (EID-877).

Corrected orchestration doctrine (EID-860 → decision 2026-07-20): a manager is a
*controller over durable state*, not a conversationalist and not a fire-and-forget
dispatcher. Fan-out with no central state is the highest-error topology. This module is
the load-bearing core that everything else reads from.

The claim under test (shadow mode, EID-877): an append-only, idempotent event ledger
lets a manager reconstruct exact task state — last-confirmed stage + first-missing
receipt — after restart, compaction, and duplicate/reordered delivery, where a
context/transcript-only manager cannot. Context is a lossy many-to-one projection; the
ledger is the authority.

Scope of THIS cut (deliberately minimal — prove the core before adding the rest):
  * append-only SQLite event log, idempotent by event_id (duplicate = no-op);
  * receipt-chain projection: contiguous-prefix last-confirmed + first-missing;
  * safety invariant in code: a missed lease → `stale`, NEVER `failed`; intervention
    needs an explicit `failed` event OR the two-proof gate (2 missed-lease proofs).
Deferred until the core proves out: authority/delegation envelopes, retry-owner
budgets, topology admission, central acceptance gate. Those are separate reducers over
this same ledger — do not build them here (YAGNI until the shadow trial clears).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

# The monotonic receipt chain. "last confirmed stage" = end of the longest gap-free
# prefix; "first missing receipt" = the first stage in this order with no receipt.
CHAIN: tuple[str, ...] = (
    "dispatch",
    "route_accepted",
    "task_read",
    "worker_started",
    "progress",         # may repeat; pass a distinct event_id per progress receipt
    "outcome_verified",
)

# Non-chain event kinds the reducer understands.
FAILED = "failed"           # explicit worker/plane failure — the only thing that authorizes intervention on its own
LEASE = "lease"             # liveness renewal; carries lease_deadline
MISSED_LEASE = "missed_lease"  # an observer's proof that a lease deadline passed unrenewed


@dataclass
class TaskState:
    task_id: str
    stages: set[str] = field(default_factory=set)   # receipt stages seen (chain only)
    last_confirmed: str | None = None               # end of contiguous chain prefix
    first_missing: str | None = None                # first gap in the chain, None if complete
    failed: bool = False
    attempts: int = 0                               # max attempt # seen
    lease_deadline: float | None = None             # latest lease deadline
    missed_leases: int = 0                          # count of distinct missed-lease proofs

    def is_stale(self, now: float) -> bool:
        """Freshness only. Stale is an OBSERVATION state — it is NOT failure and never
        authorizes interruption on its own."""
        return self.lease_deadline is not None and now > self.lease_deadline

    def intervention_allowed(self) -> bool:
        """The safety invariant. Absence of liveness evidence is not evidence of failure.
        Interruption/retry/recovery is permitted ONLY on an explicit failure, or the
        two-proof gate (two missed-lease proofs). Silence alone → no."""
        return self.failed or self.missed_leases >= 2


class Ledger:
    """Append-only, idempotent event store + reducer. One SQLite file is the authority.

    ponytail: single-table event log + full-scan-per-task reducer. O(events for a task)
    per read — fine at management scale (tens of tasks, hundreds of events). If a single
    task ever carries 10k+ events, materialize a snapshot row and fold only the tail.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   event_id      TEXT PRIMARY KEY,   -- idempotency key; re-insert is a no-op
                   task_id       TEXT NOT NULL,
                   stage         TEXT NOT NULL,
                   ts            REAL NOT NULL,
                   host          TEXT DEFAULT '',
                   component     TEXT DEFAULT '',
                   attempt       INTEGER DEFAULT 0,
                   task_version  TEXT DEFAULT '',
                   lease_deadline REAL,
                   payload       TEXT
               )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id)")
        self.db.commit()

    def record(
        self,
        task_id: str,
        stage: str,
        *,
        event_id: str | None = None,
        ts: float | None = None,
        host: str = "",
        component: str = "",
        attempt: int = 0,
        task_version: str = "",
        lease_deadline: float | None = None,
        payload: Any = None,
    ) -> bool:
        """Append one receipt/event. Returns True if newly written, False if the
        event_id already existed (idempotent no-op). Default event_id is
        `task:stage:attempt` — right for once-per-attempt stages; pass an explicit
        event_id for repeatable stages (progress) and observation proofs (missed_lease)."""
        eid = event_id or f"{task_id}:{stage}:{attempt}"
        cur = self.db.execute(
            """INSERT OR IGNORE INTO events
               (event_id, task_id, stage, ts, host, component, attempt, task_version, lease_deadline, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                eid, task_id, stage, ts if ts is not None else time.time(),
                host, component, attempt, task_version, lease_deadline,
                json.dumps(payload) if payload is not None else None,
            ),
        )
        self.db.commit()
        return cur.rowcount > 0

    def state(self, task_id: str) -> TaskState:
        """Fold all events for a task into current state. Deterministic and
        order-independent: the result is identical regardless of insertion order,
        duplicate delivery, or a process restart on the same file."""
        rows = self.db.execute(
            "SELECT stage, attempt, lease_deadline FROM events WHERE task_id=?",
            (task_id,),
        ).fetchall()
        st = TaskState(task_id=task_id)
        for stage, attempt, lease_deadline in rows:
            if stage in CHAIN:
                st.stages.add(stage)
            elif stage == FAILED:
                st.failed = True
            elif stage == MISSED_LEASE:
                st.missed_leases += 1
            elif stage == LEASE and lease_deadline is not None:
                if st.lease_deadline is None or lease_deadline > st.lease_deadline:
                    st.lease_deadline = lease_deadline
            if attempt and attempt > st.attempts:
                st.attempts = attempt

        # Contiguous-prefix projection: last-confirmed = end of the gap-free run from the
        # start of the chain; first-missing = the first stage not yet confirmed. Honest
        # about gaps — a receipt for a later stage does NOT confirm an unseen earlier one.
        st.first_missing = None
        for s in CHAIN:
            if s in st.stages:
                st.last_confirmed = s
            else:
                st.first_missing = s
                break
        return st

    def tasks(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT task_id FROM events").fetchall()]

    def close(self) -> None:
        self.db.close()
