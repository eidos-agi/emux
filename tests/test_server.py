

def test_resolve_tmux_falls_back_when_path_is_bare(monkeypatch):
    """A launchd daemon gets PATH=/usr/bin:/bin:/usr/sbin:/sbin — `which` fails
    and the daemon silently went blind to every local session. Known install
    locations must be checked before giving up."""
    from emux import server
    monkeypatch.setattr(server.shutil, "which", lambda _: None)
    monkeypatch.setattr(server.os, "access",
                        lambda p, m: p == "/opt/homebrew/bin/tmux")
    assert server._resolve_tmux() == "/opt/homebrew/bin/tmux"
    monkeypatch.setattr(server.os, "access", lambda p, m: False)
    assert server._resolve_tmux() is None
