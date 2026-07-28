"""Permanent handoff-seat installer (scripts/handoff-seat.sh)."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "handoff-seat.sh"


@pytest.mark.skipif(not SCRIPT.is_file(), reason="handoff-seat.sh missing")
def test_handoff_install_writes_knowledge_and_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    product = "testhandoff"
    repo = tmp_path / "prod"
    repo.mkdir()
    knowledge = repo / "KNOWLEDGE.md"
    knowledge.write_text(
        textwrap.dedent(
            """\
            # Test knowledge
            Compass: prove handoff.
            READY_FOR_HANDOFF protocol applies.
            """
        ),
        encoding="utf-8",
    )
    seat = f"test-handoff-{os.getpid()}"
    state = tmp_path / "state"
    monkeypatch.setenv("EMUX_HANDOFF_STATE_ROOT", str(state))
    env = os.environ.copy()
    env["EMUX_HANDOFF_STATE_ROOT"] = str(state)
    env.pop("EMUX_HANDOFF_STATE", None)
    r = subprocess.run(
        [
            str(SCRIPT),
            "install",
            "--product",
            product,
            "--repo",
            str(repo),
            "--knowledge",
            str(knowledge),
            "--seat",
            seat,
            "--source-session",
            "test-session-id",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    try:
        assert r.returncode == 0, r.stderr + r.stdout
        assert (repo / "KNOWLEDGE.md").is_file()
        assert (repo / "CLAUDE.md").is_file()
        assert (repo / "this-chat-handoff.md").is_file()
        assert "test-session-id" in (repo / "this-chat-handoff.md").read_text(encoding="utf-8")
        assert (state / seat / "KNOWLEDGE.md").is_file()
        # status
        r2 = subprocess.run(
            [str(SCRIPT), "status", "--product", product, "--seat", seat, "--repo", str(repo)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert r2.returncode == 0
        assert "seat:" in r2.stdout
    finally:
        subprocess.run(["tmux", "kill-session", "-t", seat], capture_output=True)


def test_emux_handoff_help() -> None:
    r = subprocess.run(
        ["uv", "run", "emux", "handoff", "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0
    assert "install" in r.stdout or "install" in r.stderr
