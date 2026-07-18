"""Thin Linear contract bridge for managed emux sessions.

Linear remains the task system of record. Emux stores only the issue link,
acceptance contract, and append-only manager evidence needed to supervise the
worker. Nothing here writes to Linear or marks an issue complete.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_STATE_DIR = Path(os.environ.get("EMUX_STATE") or (Path.home() / ".local" / "state" / "emux"))
EVIDENCE_PATH = _STATE_DIR / "linear_evidence.jsonl"


def metadata(
    issue: str,
    project: str | None = None,
    team: str | None = None,
    acceptance: list[str] | None = None,
) -> dict[str, Any]:
    issue = issue.strip()
    if not issue:
        raise ValueError("Linear issue is required")
    criteria = list(dict.fromkeys(x.strip() for x in (acceptance or []) if x.strip()))
    return {
        "issue": issue,
        "project": project.strip() if project else None,
        "team": team.strip() if team else None,
        "acceptance": criteria,
        "linked_at": int(time.time()),
    }


def link_session(
    registry: dict[str, dict[str, Any]],
    name: str,
    issue: str,
    project: str | None = None,
    team: str | None = None,
    acceptance: list[str] | None = None,
) -> dict[str, Any]:
    if name not in registry:
        raise ValueError(f"not registered: {name}")
    previous = registry[name].get("linear") or {}
    if project is None:
        project = previous.get("project")
    if team is None:
        team = previous.get("team")
    if acceptance is None:
        acceptance = previous.get("acceptance") or []
    registry[name]["linear"] = metadata(issue, project, team, acceptance)
    return registry[name]["linear"]


def _evidence() -> list[dict[str, Any]]:
    if not EVIDENCE_PATH.is_file():
        return []
    records = []
    for line in EVIDENCE_PATH.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def record_evidence(
    registry: dict[str, dict[str, Any]],
    name: str,
    criterion: int,
    proof: str,
    source: str | None = None,
) -> dict[str, Any]:
    entry = registry.get(name)
    if not entry:
        raise ValueError(f"not registered: {name}")
    contract = entry.get("linear") or {}
    criteria = contract.get("acceptance") or []
    if not contract.get("issue"):
        raise ValueError(f"session is not linked to a Linear issue: {name}")
    if criterion < 1 or criterion > len(criteria):
        raise ValueError(f"criterion must be between 1 and {len(criteria)}")
    proof = proof.strip()
    if not proof:
        raise ValueError("proof is required")
    record = {
        "t": int(time.time()),
        "session": name,
        "issue": contract["issue"],
        "criterion": criterion,
        "criterion_text": criteria[criterion - 1],
        "proof": proof[:4000],
        "source": source,
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVIDENCE_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def reconcile(
    registry: dict[str, dict[str, Any]],
    signals: list[dict[str, Any]],
    is_live: Callable[[str, dict[str, Any]], bool],
    names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return manager state; the strongest recommendation is review, never close."""
    evidence = _evidence()
    rows = []
    for name, entry in registry.items():
        contract = entry.get("linear") or {}
        if not contract.get("issue") or (names is not None and name not in names):
            continue
        session_signals = [x for x in signals if x.get("session") == name]
        latest = (
            max(
                enumerate(session_signals),
                key=lambda pair: (pair[1].get("t") or 0, pair[0]),
            )[1]
            if session_signals
            else None
        )
        issue_evidence = [
            x for x in evidence if x.get("session") == name and x.get("issue") == contract["issue"]
        ]
        criteria = contract.get("acceptance") or []
        proved = {x.get("criterion") for x in issue_evidence}
        missing = [
            {"criterion": i, "text": text} for i, text in enumerate(criteria, 1) if i not in proved
        ]
        kind = (latest or {}).get("kind")
        if kind == "ERROR":
            state = "blocked"
        elif kind == "NEED":
            state = "needs_input"
        elif kind == "DONE" and criteria and not missing:
            state = "ready_for_review"
        elif kind == "DONE":
            state = "evidence_missing" if criteria else "acceptance_missing"
        elif is_live(name, entry):
            state = "in_progress"
        else:
            state = "stale"
        rows.append(
            {
                "session": name,
                "issue": contract["issue"],
                "project": contract.get("project"),
                "team": contract.get("team"),
                "state": state,
                "latest_signal": latest,
                "acceptance": criteria,
                "evidence": issue_evidence,
                "missing_evidence": missing,
                "recommended_linear_status": "In Review" if state == "ready_for_review" else None,
            }
        )
    return sorted(rows, key=lambda x: (str(x.get("team") or ""), str(x["issue"])))
