"""EID-877 — the durable management ledger's load-bearing claim.

The whole point over a context/transcript-only manager: state is reconstructed from the
ledger, so it survives restart, and is identical under duplicate + reordered delivery.
Plus the safety invariant: silence (a missed lease) is `stale`, never `failed`.
"""

from __future__ import annotations

from emux import mgmt_ledger as ml


def test_happy_chain_projects_last_confirmed_and_complete():
    lg = ml.Ledger()
    for s in ml.CHAIN:
        lg.record("t1", s, ts=1.0)
    st = lg.state("t1")
    assert st.last_confirmed == "outcome_verified"
    assert st.first_missing is None            # complete chain → no gap


def test_gap_is_honest_not_papered_over():
    # worker_started present but task_read LOST — a later receipt must NOT confirm an
    # unseen earlier one. Report last-confirmed = route_accepted, first-missing = task_read.
    lg = ml.Ledger()
    for s in ("dispatch", "route_accepted", "worker_started", "progress"):
        lg.record("t1", s, ts=1.0)
    st = lg.state("t1")
    assert st.last_confirmed == "route_accepted"
    assert st.first_missing == "task_read"


def test_idempotent_duplicate_delivery_is_a_noop():
    lg = ml.Ledger()
    assert lg.record("t1", "dispatch", ts=1.0) is True
    assert lg.record("t1", "dispatch", ts=1.0) is False   # same default event_id → ignored
    # duplicate did not corrupt the fold
    assert lg.state("t1").last_confirmed == "dispatch"


def test_out_of_order_arrival_matches_in_order():
    in_order = ml.Ledger()
    for s in ml.CHAIN:
        in_order.record("t1", s, ts=1.0)

    shuffled = ml.Ledger()
    for s in ["progress", "dispatch", "outcome_verified", "task_read", "worker_started", "route_accepted"]:
        shuffled.record("t1", s, ts=1.0)

    a, b = in_order.state("t1"), shuffled.state("t1")
    assert (a.last_confirmed, a.first_missing) == (b.last_confirmed, b.first_missing)
    assert a.stages == b.stages


def test_survives_process_restart(tmp_path):
    path = str(tmp_path / "ledger.db")
    lg = ml.Ledger(path)
    lg.record("t1", "dispatch", ts=1.0)
    lg.record("t1", "route_accepted", ts=1.0)
    lg.record("t1", "task_read", ts=1.0)
    lg.close()                              # process dies

    reopened = ml.Ledger(path)              # fresh instance, same file — no transcript, no memory
    st = reopened.state("t1")
    assert st.last_confirmed == "task_read"
    assert st.first_missing == "worker_started"


def test_silence_is_stale_never_failed():
    lg = ml.Ledger()
    lg.record("t1", "worker_started", ts=1.0)
    lg.record("t1", ml.LEASE, ts=1.0, lease_deadline=10.0)
    st = lg.state("t1")

    assert st.is_stale(now=20.0) is True     # deadline passed
    assert st.is_stale(now=5.0) is False
    assert st.failed is False
    assert st.intervention_allowed() is False  # stale alone NEVER authorizes intervention


def test_two_proof_gate_authorizes_intervention():
    lg = ml.Ledger()
    lg.record("t1", "worker_started", ts=1.0)
    lg.record("t1", ml.MISSED_LEASE, event_id="miss-1", ts=11.0)
    assert lg.state("t1").intervention_allowed() is False   # one proof is not enough
    lg.record("t1", ml.MISSED_LEASE, event_id="miss-2", ts=12.0)
    assert lg.state("t1").intervention_allowed() is True     # two-proof gate met


def test_explicit_failure_authorizes_intervention():
    lg = ml.Ledger()
    lg.record("t1", "worker_started", ts=1.0)
    lg.record("t1", ml.FAILED, ts=2.0, component="worker")
    st = lg.state("t1")
    assert st.failed is True
    assert st.intervention_allowed() is True
