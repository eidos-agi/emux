"""Local Emux control-plane gateway.

The controller is deliberately separate from Emux's execution plane: it never
imports or drives tmux and never reads a remote Emux database.
"""

from .protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
