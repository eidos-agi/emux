"""emux web — a persistent local daemon with a chat-style monitor UI.

`emux web` starts a long-running HTTP server (the daemon) that exposes the
same registry + tmux operations as the MCP server, plus a browser UI that
renders any session like a chatbot conversation: the live pane is the bot's
side of the chat, the input bar sends keys into the session.

Design principles (same as the MCP server):
- Operates on EXISTING tmux sessions only. Never spawns, never kills.
- The registry is metadata; live truth comes from `tmux list-sessions`.
- Binds 127.0.0.1 by default. There is no auth — anything that can reach the
  port can type into your tmux sessions. Only widen the bind address on a
  network you trust end to end.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import server as _server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8689


def sessions_payload() -> dict[str, Any]:
    """Merged registry + live view: registered entries first, then live unregistered."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed", "sessions": []}
    live = _server._live_sessions()
    registry = _server._load_registry()
    live_by_name = {s["name"]: s for s in live}
    sessions = []
    for name, entry in sorted(registry.items()):
        target = entry.get("session")
        sessions.append({
            "name": name,
            "session": target,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "registered": True,
            "live": target in live_by_name,
            "attached": live_by_name.get(target, {}).get("attached", False),
        })
    registered_targets = {e.get("session") for e in registry.values()}
    for s in live:
        if s["name"] in registered_targets:
            continue
        sessions.append({
            "name": s["name"],
            "session": s["name"],
            "description": None,
            "tags": [],
            "registered": False,
            "live": True,
            "attached": s.get("attached", False),
        })
    return {"ok": True, "sessions": sessions}


def capture_payload(session: str, lines: int = 300) -> dict[str, Any]:
    """Capture the active pane of `session` (raw tmux session name)."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    code, out, err = _server._run_tmux([
        "capture-pane", "-t", session, "-p", "-S", f"-{lines}",
    ])
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed", "stderr": err}
    return {"ok": True, "session": session, "content": out}


def send_payload(session: str, keys: str, literal: bool = True, enter: bool = True) -> dict[str, Any]:
    """Send keys to `session`. literal=True sends text verbatim (`send-keys -l`),
    so chat input like "C-c" types those characters; literal=False interprets
    tmux key names (used by the UI's control-key chips)."""
    if _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    if literal:
        if keys:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "-l", keys])
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
        if enter:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "Enter"])
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    else:
        args = ["send-keys", "-t", session, keys]
        if enter:
            args.append("Enter")
        code, _, err = _server._run_tmux(args)
        if code != 0:
            return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    return {"ok": True, "session": session, "sent": keys, "literal": literal, "enter": enter}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>emux — control room</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0c0a07;
  --bg-raise:#141008;
  --bg-card:#191307;
  --amber:#ffb000;
  --amber-dim:#b87d00;
  --amber-faint:#3d2e0a;
  --text:#e8d5a3;
  --text-dim:#8a774d;
  --live:#7dff8a;
  --stale:#ff5d5d;
  --line:#2a2113;
  --user:#ffd569;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);
  color:var(--text);
  font:14px/1.5 "IBM Plex Mono",ui-monospace,monospace;
  display:flex;
  overflow:hidden;
}
/* scanlines + vignette over everything */
body::after{
  content:"";position:fixed;inset:0;pointer-events:none;z-index:99;
  background:
    repeating-linear-gradient(0deg, rgba(0,0,0,.16) 0 1px, transparent 1px 3px),
    radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.45) 100%);
}
/* ---------- sidebar ---------- */
#side{
  width:280px;flex:none;height:100%;display:flex;flex-direction:column;
  background:var(--bg-raise);border-right:1px solid var(--line);
}
#brand{padding:18px 18px 10px}
#brand h1{
  font-family:"VT323",monospace;font-size:44px;font-weight:400;letter-spacing:2px;
  color:var(--amber);text-shadow:0 0 18px rgba(255,176,0,.45),0 0 2px rgba(255,176,0,.9);
}
#brand small{color:var(--text-dim);font-size:11px;letter-spacing:3px;text-transform:uppercase}
#sessions{flex:1;overflow-y:auto;padding:8px}
.card{
  border:1px solid var(--line);border-left:3px solid var(--amber-faint);
  background:var(--bg-card);padding:10px 12px;margin-bottom:8px;cursor:pointer;
  transition:border-color .15s, transform .15s;
}
.card:hover{border-color:var(--amber-dim);transform:translateX(2px)}
.card.active{border-left-color:var(--amber);box-shadow:0 0 14px rgba(255,176,0,.12) inset}
.card .nm{color:var(--amber);font-weight:600}
.card .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.dot.live{background:var(--live);box-shadow:0 0 6px var(--live)}
.dot.stale{background:var(--stale);box-shadow:0 0 6px var(--stale)}
.card .sub{color:var(--text-dim);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .badges{margin-top:4px;font-size:10px}
.card .badges span{border:1px solid var(--line);color:var(--text-dim);padding:0 5px;margin-right:4px}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--text-dim);font-size:10px;letter-spacing:1px}
/* ---------- main ---------- */
#main{flex:1;height:100%;display:flex;flex-direction:column;min-width:0}
#topbar{
  flex:none;display:flex;align-items:baseline;gap:14px;
  padding:14px 22px;border-bottom:1px solid var(--line);background:var(--bg-raise);
}
#topbar #title{font-family:"VT323",monospace;font-size:26px;color:var(--amber);letter-spacing:1px}
#topbar #status{font-size:11px;color:var(--text-dim);letter-spacing:2px;text-transform:uppercase}
#topbar #status.err{color:var(--stale)}
#chat{flex:1;overflow-y:auto;padding:22px;display:flex;flex-direction:column;gap:12px}
.bubble{max-width:88%;padding:10px 14px;border:1px solid var(--line);position:relative}
.bubble .who{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim);margin-bottom:5px}
.bubble.user{
  align-self:flex-end;background:#1d1605;border-color:var(--amber-faint);
  color:var(--user);border-radius:10px 10px 0 10px;
}
.bubble.sys{align-self:center;color:var(--text-dim);font-size:11px;border:none;font-style:italic}
#screen-bubble{
  align-self:flex-start;width:100%;max-width:100%;
  background:#080705;border:1px solid var(--line);border-radius:10px 10px 10px 0;
}
#screen{
  font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;
  max-height:none;padding:4px 2px 2px;
}
#screen.dimmed{opacity:.35}
.cursorblock{display:inline-block;width:8px;height:14px;background:var(--amber);
  vertical-align:-2px;animation:blink 1.1s steps(1) infinite;box-shadow:0 0 8px rgba(255,176,0,.8)}
@keyframes blink{50%{opacity:0}}
#empty{flex:1;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--text-dim)}
#empty .glyph{font-family:"VT323",monospace;font-size:80px;color:var(--amber-faint);text-shadow:0 0 30px rgba(255,176,0,.15)}
/* ---------- composer ---------- */
#composer{flex:none;border-top:1px solid var(--line);background:var(--bg-raise);padding:12px 22px 16px}
#chips{display:flex;gap:8px;margin-bottom:10px}
.chip{
  font:11px "IBM Plex Mono",monospace;color:var(--amber-dim);background:transparent;
  border:1px solid var(--line);padding:3px 10px;cursor:pointer;letter-spacing:1px;
}
.chip:hover{color:var(--amber);border-color:var(--amber-dim)}
#row{display:flex;gap:10px}
#input{
  flex:1;background:#080705;border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber);
}
#input:focus{border-color:var(--amber-dim);box-shadow:0 0 12px rgba(255,176,0,.1)}
#send{
  font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 26px;
  background:var(--amber);color:#160f00;border:none;cursor:pointer;
}
#send:hover{box-shadow:0 0 18px rgba(255,176,0,.5)}
#send:disabled{background:var(--amber-faint);color:var(--text-dim);cursor:default;box-shadow:none}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--amber-faint)}
::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>
<aside id="side">
  <div id="brand"><h1>EMUX</h1><small>control room</small></div>
  <div id="sessions"></div>
  <footer>daemon · v__VERSION__</footer>
</aside>
<main id="main">
  <div id="topbar">
    <span id="title">no session</span>
    <span id="status">pick a session to monitor</span>
  </div>
  <div id="chat">
    <div id="empty"><div class="glyph">▚▞</div><div>select a session on the left — its pane becomes the chat</div></div>
  </div>
  <div id="composer" style="display:none">
    <div id="chips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
    </div>
    <div id="row">
      <input id="input" placeholder="type into the session… (Enter sends)" autocomplete="off" spellcheck="false">
      <button id="send">SEND</button>
    </div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s);
let current=null, pollTimer=null, sessTimer=null, screenEl=null;

async function api(path,opts){const r=await fetch(path,opts);return r.json();}

function pinned(){const c=$("#chat");return c.scrollHeight-c.scrollTop-c.clientHeight<60;}
function scrollBottom(){const c=$("#chat");c.scrollTop=c.scrollHeight;}

function addBubble(cls,who,text){
  const b=document.createElement("div");b.className="bubble "+cls;
  if(who){const w=document.createElement("div");w.className="who";w.textContent=who;b.appendChild(w);}
  const t=document.createElement("div");t.textContent=text;b.appendChild(t);
  const c=$("#chat");
  if(screenEl&&screenEl.parentElement===c){c.insertBefore(b,screenEl);}else{c.appendChild(b);}
  scrollBottom();
}

function ensureScreen(){
  if(screenEl)return;
  screenEl=document.createElement("div");screenEl.id="screen-bubble";screenEl.className="bubble";
  screenEl.innerHTML='<div class="who">'+current.name+' · live pane</div><div id="screen"></div>';
  $("#chat").appendChild(screenEl);
}

async function refreshScreen(){
  if(!current)return;
  const wasPinned=pinned();
  try{
    const r=await api("/api/capture?session="+encodeURIComponent(current.session)+"&lines=400");
    const s=$("#screen");
    if(r.ok){
      $("#status").textContent="live · polling";$("#status").className="";
      s.classList.remove("dimmed");
      if(s.dataset.last!==r.content){
        s.dataset.last=r.content;
        s.textContent=r.content.replace(/\s+$/,"")+"\n";
        const cur=document.createElement("span");cur.className="cursorblock";s.appendChild(cur);
        if(wasPinned)scrollBottom();
      }
    }else{
      $("#status").textContent=r.error||"capture failed";$("#status").className="err";
      s.classList.add("dimmed");
    }
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function select(sess){
  current=sess;
  $("#title").textContent=sess.name;
  $("#status").textContent="connecting…";$("#status").className="";
  $("#composer").style.display="";
  const c=$("#chat");c.innerHTML="";screenEl=null;
  ensureScreen();
  addBubble("sys",null,"monitoring tmux session “"+sess.session+"”"+(sess.description?" — "+sess.description:""));
  document.querySelectorAll(".card").forEach(el=>el.classList.toggle("active",el.dataset.name===sess.name));
  clearInterval(pollTimer);refreshScreen();pollTimer=setInterval(refreshScreen,1500);
  $("#input").focus();
}

async function refreshSessions(){
  try{
    const r=await api("/api/sessions");
    const box=$("#sessions");box.innerHTML="";
    if(!r.ok){box.innerHTML='<div class="card"><div class="nm">tmux unavailable</div><div class="sub">'+(r.error||"")+'</div></div>';return;}
    r.sessions.forEach(s=>{
      const d=document.createElement("div");d.className="card"+(current&&current.name===s.name?" active":"");
      d.dataset.name=s.name;
      const dot=s.live?"live":"stale";
      const badges=(s.registered?"<span>registered</span>":"<span>unregistered</span>")
        +(s.attached?"<span>attached</span>":"")
        +(s.tags||[]).map(t=>"<span>#"+t+"</span>").join("");
      d.innerHTML='<div class="nm"><span class="dot '+dot+'"></span>'+s.name+'</div>'
        +'<div class="sub">→ '+s.session+(s.description?" — "+s.description:"")+'</div>'
        +'<div class="badges">'+badges+'</div>';
      d.onclick=()=>select(s);
      box.appendChild(d);
    });
  }catch(e){/* daemon hiccup; next tick retries */}
}

async function sendText(){
  const inp=$("#input");const text=inp.value;
  if(!current||!text)return;
  inp.value="";addBubble("user","you",text);
  const r=await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:current.session,keys:text,literal:true,enter:true})});
  if(!r.ok)addBubble("sys",null,"send failed: "+(r.error||"unknown"));
  setTimeout(refreshScreen,300);
}

$("#send").onclick=sendText;
$("#input").addEventListener("keydown",e=>{if(e.key==="Enter")sendText();});
document.querySelectorAll(".chip").forEach(ch=>{
  ch.onclick=async()=>{
    if(!current)return;
    addBubble("user","key",ch.textContent);
    await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:current.session,keys:ch.dataset.keys,literal:false,enter:false})});
    setTimeout(refreshScreen,300);
  };
});

refreshSessions();sessTimer=setInterval(refreshSessions,5000);
</script>
</body>
</html>
"""


class EmuxWebHandler(BaseHTTPRequestHandler):
    server_version = f"emux/{__version__}"

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            body = PAGE.replace("__VERSION__", __version__).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/sessions":
            self._json(sessions_payload())
            return
        if url.path == "/api/capture":
            q = parse_qs(url.query)
            session = (q.get("session") or [""])[0]
            if not session:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            try:
                lines = max(1, min(5000, int((q.get("lines") or ["300"])[0])))
            except ValueError:
                lines = 300
            self._json(capture_payload(session, lines))
            return
        self._json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/api/send":
            self._json({"ok": False, "error": "not_found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad_json"}, 400)
            return
        session = data.get("session")
        keys = data.get("keys")
        if not isinstance(session, str) or not session or not isinstance(keys, str):
            self._json({"ok": False, "error": "missing_session_or_keys"}, 400)
            return
        self._json(send_payload(
            session,
            keys,
            literal=bool(data.get("literal", True)),
            enter=bool(data.get("enter", True)),
        ))

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default; the poll traffic would otherwise flood the terminal.
        pass


def run_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = False) -> int:
    """Start the emux web daemon. Blocks until Ctrl-C."""
    if _server._resolve_tmux() is None:
        print("emux web: tmux not found on PATH — the UI will load but show nothing.", file=sys.stderr)
    try:
        server = ThreadingHTTPServer((host, port), EmuxWebHandler)
    except OSError as e:
        if "address already in use" in str(e).lower():
            print(f"emux web: port {port} is already in use — try `emux web --port {port + 1}`.", file=sys.stderr)
            return 2
        raise
    url = f"http://{host}:{port}"
    print(f"emux web daemon → {url}  (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost"):
        print("  WARNING: bound beyond localhost with no auth — anything that can", file=sys.stderr)
        print("  reach this port can type into your tmux sessions.", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nemux web: stopped.")
    finally:
        server.server_close()
    return 0
