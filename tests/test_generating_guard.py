"""The _looks_generating guard decides whether converse may send Escape to clear
a stale composer draft. Escape mid-generation interrupts the AI, so a false
"idle" here is a real bug — this locks the guard down."""
from emux.server import _looks_generating


def test_idle_screens_are_clearable():
    # Plain prompt / no activity → safe to Escape.
    assert not _looks_generating("❯ \n[PONYTAIL]\n⏵⏵ auto mode on")
    assert not _looks_generating("")
    assert not _looks_generating("some finished reply text\n❯ ")


def test_generating_screens_are_protected():
    # Live timer, interrupt hint, or a busy marker → never Escape.
    assert _looks_generating("· Gesticulating… (9s · thinking)")
    assert _looks_generating("✻ Whirring… (24s · ↓ 1.4k tokens)")
    assert _looks_generating("Working… esc to interrupt")
    assert _looks_generating("thinking hard", busy_markers=["thinking"])


def test_marker_case_insensitive():
    assert _looks_generating("THINKING…", busy_markers=["thinking"])
