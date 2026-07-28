"""Unit tests for READY_FOR_HANDOFF sole-line acceptance (handoff verify)."""
from __future__ import annotations

import re


def _last_token_line(text: str, token: str) -> int | None:
    """1-based line number of last sole-line token, or None."""
    pat = re.compile(rf"^[\t ]*{re.escape(token)}[\t ]*$")
    last = None
    for i, line in enumerate(text.splitlines(), 1):
        if pat.match(line):
            last = i
    return last


def _verdict(combined: str) -> str:
    """Mirror scripts/handoff-seat.sh sole-line logic: pass|fail_no|fail_missing."""
    yes = _last_token_line(combined, "READY_FOR_HANDOFF=yes")
    no = _last_token_line(combined, "READY_FOR_HANDOFF=no")
    if no is not None and (yes is None or no > yes):
        return "fail_no"
    if yes is not None:
        return "pass"
    return "fail_missing"


def test_prompt_midline_does_not_pass():
    prompt = (
        "End with token READY_FOR_HANDOFF=yes alone on its own final line "
        "if ready; else READY_FOR_HANDOFF=no alone."
    )
    assert _verdict(prompt) == "fail_missing"


def test_shell_echo_of_prompt_does_not_pass():
    blob = """
$ emux ask seat '.... put READY_FOR_HANDOFF=yes alone ...'
zsh: command not found: put
"""
    assert _verdict(blob) == "fail_missing"


def test_sole_line_yes_passes():
    text = """
## Compass
- a
- b
READY_FOR_HANDOFF=yes
"""
    assert _verdict(text) == "pass"


def test_sole_line_no_fails():
    text = """
I am not ready.
READY_FOR_HANDOFF=no
"""
    assert _verdict(text) == "fail_no"


def test_last_token_wins_no_after_yes():
    text = """
READY_FOR_HANDOFF=yes
wait actually not
READY_FOR_HANDOFF=no
"""
    assert _verdict(text) == "fail_no"


def test_last_token_wins_yes_after_no():
    text = """
READY_FOR_HANDOFF=no
changed mind after reading
READY_FOR_HANDOFF=yes
"""
    assert _verdict(text) == "pass"


def test_indented_sole_line_yes_passes():
    text = "  READY_FOR_HANDOFF=yes  \n"
    assert _verdict(text) == "pass"


def test_yes_in_sentence_no_pass():
    text = "I think READY_FOR_HANDOFF=yes is correct overall.\n"
    assert _verdict(text) == "fail_missing"
