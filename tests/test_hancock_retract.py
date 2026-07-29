import sqlite3, pathlib, sys, tempfile, importlib.util
src = pathlib.Path("src/emux/web.py")
spec = importlib.util.spec_from_file_location("w", src)
# don't import the whole module (heavy) — exec just the two funcs
code = src.read_text()
import re
start = code.index("def _hancock_retract")
end   = code.index("def _file_hancock_escalation")
ns = {"Any": object}
tmp = pathlib.Path(tempfile.mkdtemp())/"h.db"
con = sqlite3.connect(tmp)
con.executescript("""
CREATE TABLE request(id TEXT PRIMARY KEY, command TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','skipped','expired','executed','failed')),
  updated_at TEXT);
CREATE TABLE decision(id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES request(id),
  approver_id TEXT, verdict TEXT NOT NULL CHECK (verdict IN ('approve','deny','skip')),
  reason TEXT, decided_at TEXT NOT NULL DEFAULT (datetime('now')));
INSERT INTO request(id,command) VALUES ('r1','emux head redash-multitenant');
""")
con.commit(); con.close()
ns["_hancock_db"] = lambda: tmp
exec(code[start:end], ns)

r = ns["_hancock_retract"]("r1", "retracted: session no longer gated")
assert r["ok"], r
con = sqlite3.connect(tmp)
st = con.execute("select status from request where id='r1'").fetchone()[0]
vd, rs = con.execute("select verdict, reason from decision where request_id='r1'").fetchone()
print(f"status={st}  verdict={vd}  reason={rs}")
assert st == "skipped",  f"status is {st}, must not be 'denied'"
assert vd == "skip",     f"verdict is {vd}, must not be 'deny'"
assert "retracted" in rs
# terminal-state contract from hancock cli/mcp.go:120
TERMINAL = {"executed","failed","skipped","denied","expired"}
assert st in TERMINAL, "status not terminal — a waiting agent would hang"
print("retract check: ok — terminal, not a denial")
