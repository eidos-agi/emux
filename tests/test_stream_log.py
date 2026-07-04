"""emux's durable character log: pipe-pane writes it, _read_log reads it back.
strip=True must produce clean text for grep/consideration; strip=False keeps the
raw stream for exact replay. Path names must be filesystem-safe."""
import emux.server as s


def test_read_log_strips_ansi_for_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_LOG_DIR", tmp_path)
    (tmp_path / "x.log").write_text("hi \x1b[31mred\x1b[0m\rthere")
    assert s._read_log("x", strip=True) == "hi redthere"      # ANSI + \r gone
    assert "\x1b[31m" in s._read_log("x", strip=False)          # raw kept for replay


def test_read_log_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_LOG_DIR", tmp_path)
    assert s._read_log("nope") == ""


def test_log_path_is_filesystem_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_LOG_DIR", tmp_path)
    assert s._log_path("dir/name:weird session").name == "dir_name_weird_session.log"
