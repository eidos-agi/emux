"""Native handoff installer (src/emux/handoff.py)."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_handoff_install_writes_knowledge_and_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from emux import handoff as h

    product = "testhandoff"
    repo = tmp_path / "prod"
    repo.mkdir()
    knowledge = repo / "KNOWLEDGE.md"
    knowledge.write_text(
        textwrap.dedent(
            """\
            # Test knowledge
            Compass: prove handoff.
            Line three for length.
            Line four.
            Line five.
            """
        ),
        encoding="utf-8",
    )
    seat = f"test-handoff-{os.getpid()}"
    state = tmp_path / "state"
    monkeypatch.setenv("EMUX_HANDOFF_STATE_ROOT", str(state))

    try:
        code = h.cmd_install(
            product,
            repo=repo,
            knowledge=knowledge,
            seat=seat,
            source_session="test-session-id",
        )
        assert code == 0
        assert (repo / "KNOWLEDGE.md").is_file()
        assert (repo / "this-chat-handoff.md").is_file()
        assert "test-session-id" in (repo / "this-chat-handoff.md").read_text(encoding="utf-8")
        assert (state / seat / "KNOWLEDGE.md").is_file()
        # Thin install: never clobber product agent briefs
        assert not (repo / "CLAUDE.md").exists()
        assert not (repo / "AGENTS.md").exists()

        vcode = h.cmd_verify(product, repo=repo, seat=seat)
        assert vcode == 0
        assert (state / seat / "verify-status").read_text(encoding="utf-8").strip() == "pass"

        scode = h.cmd_status(product, repo=repo, seat=seat)
        assert scode == 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", seat], capture_output=True)


def test_handoff_verify_fails_without_seat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from emux import handoff as h

    product = "nofake"
    repo = tmp_path / "prod"
    repo.mkdir()
    (repo / "KNOWLEDGE.md").write_text(
        "# k\n1\n2\n3\n4\n5\n",
        encoding="utf-8",
    )
    seat = f"missing-seat-{os.getpid()}"
    state = tmp_path / "state"
    monkeypatch.setenv("EMUX_HANDOFF_STATE_ROOT", str(state))
    # state knowledge present but no tmux
    (state / seat).mkdir(parents=True)
    (state / seat / "KNOWLEDGE.md").write_text(
        "# k\n1\n2\n3\n4\n5\n",
        encoding="utf-8",
    )
    code = h.cmd_verify(product, repo=repo, seat=seat)
    assert code == 3


def test_emux_handoff_help() -> None:
    r = subprocess.run(
        ["uv", "run", "emux", "handoff", "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "install" in out
    assert "verify" in out
    assert "doctor" in out


def test_emux_handoff_init_template(tmp_path: Path) -> None:
    from emux import handoff as h

    repo = tmp_path / "newprod"
    repo.mkdir()
    code = h.cmd_init("newprod", repo=repo)
    assert code == 0
    assert (repo / "KNOWLEDGE.md").is_file()
    text = (repo / "KNOWLEDGE.md").read_text(encoding="utf-8")
    assert "Compass" in text
    # second init without force leaves it
    code2 = h.cmd_init("newprod", repo=repo)
    assert code2 == 0


def test_handoff_install_archives_prior_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from emux import handoff as h

    product = "archivetest"
    repo = tmp_path / "prod"
    repo.mkdir()
    old = textwrap.dedent(
        """\
        # Old mission
        Finance pack line 2
        Finance pack line 3
        Finance pack line 4
        Finance pack line 5
        """
    )
    (repo / "KNOWLEDGE.md").write_text(old, encoding="utf-8")
    new_k = repo / "KNOWLEDGE-new.md"
    new_k.write_text(
        textwrap.dedent(
            """\
            # New mission
            Kids pack line 2
            Kids pack line 3
            Kids pack line 4
            Kids pack line 5
            """
        ),
        encoding="utf-8",
    )
    seat = f"test-archive-{os.getpid()}"
    state = tmp_path / "state"
    monkeypatch.setenv("EMUX_HANDOFF_STATE_ROOT", str(state))
    # seed prior state knowledge too
    (state / seat).mkdir(parents=True)
    (state / seat / "KNOWLEDGE.md").write_text(old, encoding="utf-8")

    try:
        code = h.cmd_install(product, repo=repo, knowledge=new_k, seat=seat)
        assert code == 0
        assert "New mission" in (repo / "KNOWLEDGE.md").read_text(encoding="utf-8")
        archives = list((repo / "KNOWLEDGE-archive").glob("*.md"))
        assert archives, "expected repo KNOWLEDGE-archive"
        assert "Old mission" in archives[0].read_text(encoding="utf-8")
        state_archives = list((state / seat / "KNOWLEDGE-archive").glob("*.md"))
        assert state_archives, "expected state KNOWLEDGE-archive"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", seat], capture_output=True)


def test_resolve_boot_runtime_flag_and_product_json(tmp_path: Path) -> None:
    from emux import handoff as h

    repo = tmp_path / "prod"
    repo.mkdir()
    (repo / "product.json").write_text('{"runtime": "grok"}\n', encoding="utf-8")
    assert h._resolve_boot_runtime("x", repo=repo, runtime=None) == "grok"
    assert h._resolve_boot_runtime("x", repo=repo, runtime="claude") == "claude"
    with pytest.raises(ValueError):
        h._resolve_boot_runtime("x", repo=repo, runtime="codex")
