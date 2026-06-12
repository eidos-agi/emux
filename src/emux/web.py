"""emux web — a persistent local daemon with a chat-style monitor UI.

`emux web` starts a long-running HTTP server (the daemon) that exposes the
same registry + tmux operations as the MCP server, plus a browser UI with
five views over the sessions emux knows about:

- chat     — one session rendered like a chatbot: the live pane is the bot's
             side of the chat, the input bar sends keys into the session.
- grid     — every session as a live mini-pane tile, all streaming at once.
- groups   — the same tiles sectioned by registry tag.
- activity — change-detection strips per session: which panes moved, when.
- flow     — topology graph with the emux daemon at the center, sessions
             around it, and directed edges for agent→agent `manages` links
             from the registry.

Design principles (same as the MCP server):
- Operates on EXISTING tmux sessions only. Never spawns, never kills.
- The registry is metadata; live truth comes from `tmux list-sessions`.
- Binds 127.0.0.1 by default. There is no auth — anything that can reach the
  port can type into your tmux sessions. Only widen the bind address on a
  network you trust end to end.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import server as _server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8689

# Activity tracking: per tmux session, the daemon remembers the last pane
# hash, when it last changed, and a ring buffer of changed/unchanged samples.
# Shared across clients (it lives in the daemon, not the browser).
_ACTIVITY: dict[str, dict[str, Any]] = {}
_ACTIVITY_LOCK = threading.Lock()
_SAMPLE_MIN_INTERVAL = 1.0  # seconds; multiple clients don't double-count
_SAMPLE_WINDOW = 60


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
            "manages": entry.get("manages") or [],
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
            "manages": [],
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


def _record_activity(session: str, content: str) -> dict[str, Any]:
    """Update the daemon's activity record for one pane capture; return meta."""
    now = time.time()
    digest = hashlib.sha1(content.encode()).hexdigest()
    with _ACTIVITY_LOCK:
        st = _ACTIVITY.setdefault(session, {
            "hash": None, "last_change": None, "last_sample": 0.0,
            "samples": deque(maxlen=_SAMPLE_WINDOW),
        })
        changed = st["hash"] is not None and st["hash"] != digest
        if changed:
            st["last_change"] = now
        st["hash"] = digest
        if now - st["last_sample"] >= _SAMPLE_MIN_INTERVAL:
            st["samples"].append(1 if changed else 0)
            st["last_sample"] = now
        return {
            "changed": changed,
            "last_change_age": (now - st["last_change"]) if st["last_change"] else None,
            "activity": list(st["samples"]),
        }


def grid_payload(lines: int = 14) -> dict[str, Any]:
    """Everything the grid/groups/activity/flow views need, in one call:
    the merged session list, a mini capture per live pane, and activity meta."""
    base = sessions_payload()
    if not base["ok"]:
        return base
    for item in base["sessions"]:
        if item["live"]:
            cap = capture_payload(item["session"], lines)
            content = cap.get("content", "") if cap.get("ok") else ""
            item["content"] = content
            item.update(_record_activity(item["session"], content))
        else:
            item["content"] = ""
            item["changed"] = False
            item["last_change_age"] = None
            item["activity"] = []
    return base


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
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.dot.live{background:var(--live);box-shadow:0 0 6px var(--live)}
.dot.stale{background:var(--stale);box-shadow:0 0 6px var(--stale)}
.card .sub{color:var(--text-dim);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .badges{margin-top:4px;font-size:10px}
.card .badges span{border:1px solid var(--line);color:var(--text-dim);padding:0 5px;margin-right:4px}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--text-dim);font-size:10px;letter-spacing:1px}
/* ---------- main ---------- */
#main{flex:1;height:100%;display:flex;flex-direction:column;min-width:0}
#topbar{
  flex:none;display:flex;align-items:center;gap:14px;
  padding:10px 22px;border-bottom:1px solid var(--line);background:var(--bg-raise);
}
#topbar #title{font-family:"VT323",monospace;font-size:26px;color:var(--amber);letter-spacing:1px}
#topbar #status{font-size:11px;color:var(--text-dim);letter-spacing:2px;text-transform:uppercase}
#topbar #status.err{color:var(--stale)}
#tabs{margin-left:auto;display:flex;gap:6px}
.tab{
  font-family:"VT323",monospace;font-size:17px;letter-spacing:2px;padding:3px 14px;
  background:transparent;color:var(--text-dim);border:1px solid var(--line);cursor:pointer;
}
.tab:hover{color:var(--amber);border-color:var(--amber-dim)}
.tab.on{background:var(--amber);color:#160f00;border-color:var(--amber)}
/* ---------- views ---------- */
#views{flex:1;overflow-y:auto;padding:18px}
/* grid of live tiles */
.tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.tile{
  border:1px solid var(--line);background:#080705;cursor:pointer;overflow:hidden;
  display:flex;flex-direction:column;transition:border-color .2s, box-shadow .4s;
}
.tile:hover{border-color:var(--amber-dim)}
.tile.hot{border-color:var(--amber);box-shadow:0 0 16px rgba(255,176,0,.25)}
.tile.dead{opacity:.45}
.tile header{
  display:flex;align-items:baseline;gap:8px;padding:6px 10px;
  background:var(--bg-card);border-bottom:1px solid var(--line);
}
.tile header .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile header .age{margin-left:auto;font-size:10px;color:var(--text-dim);letter-spacing:1px;white-space:nowrap}
.tile pre{
  flex:1;font:9.5px/1.35 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:8px 10px;height:190px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-end;
}
.tile pre.empty{color:var(--text-dim);font-style:italic;justify-content:center;text-align:center}
/* grouped grids */
.group{margin-bottom:26px}
.group h2{
  font-family:"VT323",monospace;font-size:22px;letter-spacing:2px;color:var(--amber-dim);
  border-bottom:1px solid var(--line);margin-bottom:12px;padding-bottom:4px;
}
.group h2 .cnt{color:var(--text-dim);font-size:14px}
/* activity grid */
.actrows{display:flex;flex-direction:column;gap:10px;max-width:980px}
.actrow{
  display:flex;align-items:center;gap:14px;border:1px solid var(--line);
  background:var(--bg-card);padding:10px 14px;cursor:pointer;
}
.actrow:hover{border-color:var(--amber-dim)}
.actrow .nm{color:var(--amber);font-weight:600;width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:none}
.cells{display:flex;gap:2px;flex:1;min-width:0}
.cell{width:9px;height:18px;background:#1a140a;flex:none}
.cell.on{background:var(--amber);box-shadow:0 0 5px rgba(255,176,0,.6)}
.cell.recent{background:var(--user)}
.actrow .age{font-size:11px;color:var(--text-dim);width:120px;text-align:right;flex:none;letter-spacing:1px}
/* flow view */
#flowwrap{width:100%;height:100%;min-height:520px}
#flowwrap svg{width:100%;height:100%}
.fnode{cursor:pointer}
.fnode rect{fill:var(--bg-card);stroke:var(--line);stroke-width:1.5;rx:4}
.fnode:hover rect{stroke:var(--amber-dim)}
.fnode.hot rect{stroke:var(--amber);filter:drop-shadow(0 0 8px rgba(255,176,0,.5))}
.fnode.dead{opacity:.4}
.fnode text{fill:var(--amber);font:600 12px "IBM Plex Mono",monospace}
.fnode text.sub{fill:var(--text-dim);font:9px "IBM Plex Mono",monospace}
.hub circle{fill:#1d1605;stroke:var(--amber);stroke-width:2;filter:drop-shadow(0 0 18px rgba(255,176,0,.4))}
.hub text{fill:var(--amber);font:28px "VT323",monospace;letter-spacing:2px}
.edge{stroke:var(--amber-faint);stroke-width:1.2;fill:none;stroke-dasharray:5 7;animation:flow 1.6s linear infinite}
.edge.manage{stroke:var(--amber);stroke-width:2;stroke-dasharray:7 5;animation:flow .8s linear infinite;
  filter:drop-shadow(0 0 4px rgba(255,176,0,.5))}
@keyframes flow{to{stroke-dashoffset:-12}}
.elabel{fill:var(--amber-dim);font:9px "IBM Plex Mono",monospace;letter-spacing:1px}
#flowhint{color:var(--text-dim);font-size:11px;font-style:italic;margin-top:6px}
#flowhint code{color:var(--amber-dim);font-style:normal}
/* ---------- chat ---------- */
#chat{flex:1;overflow-y:auto;padding:22px;display:none;flex-direction:column;gap:12px}
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
#empty{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--text-dim);height:100%}
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
    <span id="title">grid</span>
    <span id="status">connecting…</span>
    <div id="tabs">
      <button class="tab" data-mode="grid">GRID</button>
      <button class="tab" data-mode="groups">GROUPS</button>
      <button class="tab" data-mode="activity">ACTIVITY</button>
      <button class="tab" data-mode="flow">FLOW</button>
    </div>
  </div>
  <div id="views"></div>
  <div id="chat"></div>
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
const SVGNS="http://www.w3.org/2000/svg";
let mode="grid", current=null, grid=[], chatTimer=null, gridTimer=null, screenEl=null;

async function api(path,opts){const r=await fetch(path,opts);return r.json();}

function ageLabel(a){
  if(a===null||a===undefined)return "—";
  if(a<2)return "now";
  if(a<60)return Math.round(a)+"s ago";
  if(a<3600)return Math.round(a/60)+"m ago";
  return Math.round(a/3600)+"h ago";
}

/* ---------- mode switching ---------- */
function setMode(m){
  mode=m;current=(m==="chat")?current:null;
  $("#chat").style.display=(m==="chat")?"flex":"none";
  $("#views").style.display=(m==="chat")?"none":"";
  $("#composer").style.display=(m==="chat")?"":"none";
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.mode===m));
  document.querySelectorAll(".card").forEach(el=>el.classList.toggle("active",!!(current&&el.dataset.name===current.name)));
  clearInterval(chatTimer);chatTimer=null;
  if(m!=="chat"){$("#title").textContent=m;$("#views").innerHTML="";render();}
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setMode(t.dataset.mode));

/* ---------- shared poll ---------- */
async function poll(){
  try{
    const r=await api("/api/grid?lines=14");
    if(!r.ok){$("#status").textContent=r.error||"error";$("#status").className="err";return;}
    grid=r.sessions;
    $("#status").textContent=grid.filter(s=>s.live).length+" live · polling";$("#status").className="";
    renderSidebar();
    if(mode!=="chat")render();
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function renderSidebar(){
  const box=$("#sessions");box.innerHTML="";
  grid.forEach(s=>{
    const d=document.createElement("div");
    d.className="card"+(current&&current.name===s.name?" active":"");
    d.dataset.name=s.name;
    const badges=(s.registered?"<span>registered</span>":"<span>unregistered</span>")
      +(s.attached?"<span>attached</span>":"")
      +(s.tags||[]).map(t=>"<span>#"+t+"</span>").join("");
    d.innerHTML='<div class="nm"><span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name+'</div>'
      +'<div class="sub">→ '+s.session+(s.description?" — "+s.description:"")+'</div>'
      +'<div class="badges">'+badges+'</div>';
    d.onclick=()=>openChat(s);
    box.appendChild(d);
  });
}

/* ---------- tiles (grid + groups) ---------- */
function makeTile(s){
  const t=document.createElement("div");
  t.className="tile"+((s.last_change_age!==null&&s.last_change_age<6)?" hot":"")+(s.live?"":" dead");
  const h=document.createElement("header");
  h.innerHTML='<span class="dot '+(s.live?"live":"stale")+'"></span><span class="nm">'+s.name
    +'</span><span class="age">'+(s.live?ageLabel(s.last_change_age):"gone")+'</span>';
  const p=document.createElement("pre");
  if(s.live&&s.content.trim()){
    const lines=s.content.replace(/\s+$/,"").split("\n");
    p.textContent=lines.slice(-14).join("\n");
  }else{
    p.className="empty";p.textContent=s.live?"(blank pane)":"tmux session gone";
  }
  t.appendChild(h);t.appendChild(p);
  t.onclick=()=>openChat(s);
  return t;
}

function renderGrid(){
  const v=$("#views");v.innerHTML="";
  const g=document.createElement("div");g.className="tilegrid";
  grid.forEach(s=>g.appendChild(makeTile(s)));
  if(!grid.length)v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';
  else v.appendChild(g);
}

function renderGroups(){
  const v=$("#views");v.innerHTML="";
  const groups=new Map();
  const put=(k,s)=>{if(!groups.has(k))groups.set(k,[]);groups.get(k).push(s);};
  grid.forEach(s=>{
    if(!s.registered)put("unregistered",s);
    else if(!(s.tags||[]).length)put("untagged",s);
    else s.tags.forEach(t=>put("#"+t,s));
  });
  if(!groups.size){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';return;}
  const order=[...groups.keys()].sort((a,b)=>{
    const w=k=>k==="unregistered"?2:(k==="untagged"?1:0);
    return w(a)-w(b)||a.localeCompare(b);
  });
  order.forEach(k=>{
    const sec=document.createElement("div");sec.className="group";
    const h=document.createElement("h2");
    h.innerHTML=k+' <span class="cnt">· '+groups.get(k).length+'</span>';
    const g=document.createElement("div");g.className="tilegrid";
    groups.get(k).forEach(s=>g.appendChild(makeTile(s)));
    sec.appendChild(h);sec.appendChild(g);v.appendChild(sec);
  });
}

/* ---------- activity grid ---------- */
function renderActivity(){
  const v=$("#views");v.innerHTML="";
  const wrap=document.createElement("div");wrap.className="actrows";
  grid.forEach(s=>{
    const row=document.createElement("div");row.className="actrow";
    const nm=document.createElement("div");nm.className="nm";
    nm.innerHTML='<span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name;
    const cells=document.createElement("div");cells.className="cells";
    const samples=s.activity||[];
    const pad=Math.max(0,60-samples.length);
    for(let i=0;i<pad;i++){const c=document.createElement("div");c.className="cell";cells.appendChild(c);}
    samples.forEach((on,i)=>{
      const c=document.createElement("div");
      c.className="cell"+(on?(i>=samples.length-5?" recent":" on"):"");
      cells.appendChild(c);
    });
    const age=document.createElement("div");age.className="age";
    age.textContent=s.live?("active "+ageLabel(s.last_change_age)):"gone";
    row.appendChild(nm);row.appendChild(cells);row.appendChild(age);
    row.onclick=()=>openChat(s);
    wrap.appendChild(row);
  });
  if(!grid.length)v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';
  else v.appendChild(wrap);
}

/* ---------- flow view ---------- */
function el(tag,attrs){const e=document.createElementNS(SVGNS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}

function renderFlow(){
  const v=$("#views");v.innerHTML="";
  const wrap=document.createElement("div");wrap.id="flowwrap";
  const W=1100,H=640,CX=W/2,CY=H/2;
  const svg=el("svg",{viewBox:"0 0 "+W+" "+H});
  const defs=el("defs",{});
  const marker=el("marker",{id:"arrow",viewBox:"0 0 10 10",refX:"9",refY:"5",
    markerWidth:"7",markerHeight:"7",orient:"auto-start-reverse"});
  marker.appendChild(el("path",{d:"M0,0 L10,5 L0,10 z",fill:"#ffb000"}));
  defs.appendChild(marker);svg.appendChild(defs);

  const n=grid.length;
  const pos={};
  grid.forEach((s,i)=>{
    const a=-Math.PI/2+(2*Math.PI*i)/Math.max(1,n);
    pos[s.name]={x:CX+Math.cos(a)*(W/2-160),y:CY+Math.sin(a)*(H/2-90),s:s};
  });
  // resolve manage targets by registry name OR underlying tmux session name
  const byKey={};grid.forEach(s=>{byKey[s.name]=s;byKey[s.session]=byKey[s.session]||s;});

  // monitor edges: hub → every session (under nodes)
  grid.forEach(s=>{
    const p=pos[s.name];
    svg.appendChild(el("line",{x1:CX,y1:CY,x2:p.x,y2:p.y,class:"edge"}));
  });
  // manage edges: agent → agent
  grid.forEach(s=>{
    (s.manages||[]).forEach(t=>{
      const target=byKey[t];if(!target||target.name===s.name)return;
      const a=pos[s.name],b=pos[target.name];
      const mx=(a.x+b.x)/2+(CY-(a.y+b.y)/2)*.25, my=(a.y+b.y)/2+((a.x+b.x)/2-CX)*.25;
      svg.appendChild(el("path",{d:"M"+a.x+","+a.y+" Q"+mx+","+my+" "+b.x+","+b.y,
        class:"edge manage","marker-end":"url(#arrow)"}));
      const lt=el("text",{x:mx,y:my-6,class:"elabel","text-anchor":"middle"});
      lt.textContent="manages";svg.appendChild(lt);
    });
  });
  // hub
  const hub=el("g",{class:"hub"});
  hub.appendChild(el("circle",{cx:CX,cy:CY,r:46}));
  const ht=el("text",{x:CX,y:CY+9,"text-anchor":"middle"});ht.textContent="EMUX";
  hub.appendChild(ht);svg.appendChild(hub);
  // session nodes
  grid.forEach(s=>{
    const p=pos[s.name];
    const g=el("g",{class:"fnode"+((s.last_change_age!==null&&s.last_change_age<6)?" hot":"")+(s.live?"":" dead")});
    const bw=Math.max(120,s.name.length*8+30);
    g.appendChild(el("rect",{x:p.x-bw/2,y:p.y-24,width:bw,height:48,rx:4}));
    const t1=el("text",{x:p.x,y:p.y-2,"text-anchor":"middle"});t1.textContent=s.name;
    const t2=el("text",{x:p.x,y:p.y+14,"text-anchor":"middle",class:"sub"});
    t2.textContent=s.live?("active "+ageLabel(s.last_change_age)):"gone";
    g.appendChild(t1);g.appendChild(t2);
    g.onclick=()=>openChat(s);
    svg.appendChild(g);
  });
  wrap.appendChild(svg);
  v.appendChild(wrap);
  const hint=document.createElement("div");hint.id="flowhint";
  hint.innerHTML="dim flows = emux monitoring · bright arrows = agent manages agent — declare with <code>emux register &lt;name&gt; &lt;session&gt; --manages &lt;other&gt;</code>";
  v.appendChild(hint);
  if(!grid.length)v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';
}

function render(){
  if(mode==="grid")renderGrid();
  else if(mode==="groups")renderGroups();
  else if(mode==="activity")renderActivity();
  else if(mode==="flow")renderFlow();
}

/* ---------- chat ---------- */
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

async function refreshScreen(){
  if(!current)return;
  const wasPinned=pinned();
  try{
    const r=await api("/api/capture?session="+encodeURIComponent(current.session)+"&lines=400");
    const s=$("#screen");if(!s)return;
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

function openChat(sess){
  current=sess;
  setMode("chat");
  $("#title").textContent=sess.name;
  $("#status").textContent="connecting…";$("#status").className="";
  const c=$("#chat");c.innerHTML="";screenEl=null;
  screenEl=document.createElement("div");screenEl.id="screen-bubble";screenEl.className="bubble";
  screenEl.innerHTML='<div class="who"></div><div id="screen"></div>';
  screenEl.querySelector(".who").textContent=sess.name+" · live pane";
  c.appendChild(screenEl);
  addBubble("sys",null,"monitoring tmux session “"+sess.session+"”"+(sess.description?" — "+sess.description:""));
  document.querySelectorAll(".card").forEach(el2=>el2.classList.toggle("active",el2.dataset.name===sess.name));
  refreshScreen();chatTimer=setInterval(refreshScreen,1500);
  $("#input").focus();
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

setMode("grid");
poll();gridTimer=setInterval(poll,2000);
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
        if url.path == "/api/grid":
            q = parse_qs(url.query)
            try:
                lines = max(1, min(100, int((q.get("lines") or ["14"])[0])))
            except ValueError:
                lines = 14
            self._json(grid_payload(lines))
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
