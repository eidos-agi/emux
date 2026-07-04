"""Isolate emux's durable log/state dir during tests.

_start_stream_log now arms on register/drive, so any test that registers a real
tmux session would otherwise write pipe-pane logs into the user's real
~/.local/state/emux/logs. Redirect state to a throwaway dir for the whole run."""
import pathlib
import tempfile

_STATE = pathlib.Path(tempfile.mkdtemp(prefix="emux-test-state-"))

import emux.server as _s  # noqa: E402

_s._STATE_DIR = _STATE
_s._LOG_DIR = _STATE / "logs"
