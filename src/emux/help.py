"""Versioned, read-only Emux help corpus and shared browser assistant."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

CORPUS_VERSION = "2026.07.18"
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class HelpPage:
    slug: str
    title: str
    summary: str
    order: int
    body: str

    @property
    def url(self) -> str:
        return f"/docs#{self.slug}"


def _parse_page(name: str, raw: str) -> HelpPage:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"help page {name} requires frontmatter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    title = meta.get("title", "")
    summary = meta.get("summary", "")
    if not title or not summary:
        raise ValueError(f"help page {name} requires title and summary")
    return HelpPage(name.removesuffix(".md"), title, summary,
                    int(meta.get("order", 999)), match.group(2).strip())


def pages() -> tuple[HelpPage, ...]:
    root = files("emux").joinpath("help_docs")
    result = [_parse_page(item.name, item.read_text(encoding="utf-8"))
              for item in root.iterdir() if item.name.endswith(".md")]
    return tuple(sorted(result, key=lambda page: (page.order, page.title)))


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.lower()))


def answer(query: str) -> dict[str, Any]:
    """Return a deterministic answer sourced only from the packaged corpus."""
    clean = " ".join(query.split())[:500]
    if not clean:
        return {"ok": False, "error": "missing_query"}
    wanted = _tokens(clean)
    ranked: list[tuple[float, HelpPage]] = []
    for page in pages():
        score = (5 * len(wanted & _tokens(page.title))
                 + 3 * len(wanted & _tokens(page.summary))
                 + len(wanted & _tokens(page.body)))
        if score:
            ranked.append((score / max(1, len(wanted)), page))
    ranked.sort(key=lambda row: (-row[0], row[1].order))
    if not ranked or ranked[0][0] < 0.34:
        return {"ok": True, "found": False, "query": clean, "answer": "",
                "sources": [], "corpus_version": CORPUS_VERSION}
    selected = [page for _, page in ranked[:2]]
    excerpts = []
    for page_index, page in enumerate(selected):
        paragraphs = [part.strip() for part in page.body.split("\n\n")
                      if part.strip() and not part.startswith("#")]
        ranked_paragraphs = sorted(
            paragraphs,
            key=lambda part: len(wanted & _tokens(part)),
            reverse=True,
        )
        # The primary source gets enough adjacent context to answer boundary
        # questions without turning retrieval into a learned-summary system.
        excerpts.extend((ranked_paragraphs or [page.summary])[:2 if page_index == 0 else 1])
    return {"ok": True, "found": True, "query": clean,
            "answer": "\n\n".join(excerpts),
            "sources": [{"title": page.title, "url": page.url,
                         "summary": page.summary} for page in selected],
            "corpus_version": CORPUS_VERSION}


def _inline(text: str) -> str:
    safe = html.escape(text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)


def _render_markdown(body: str) -> str:
    blocks: list[str] = []
    for part in body.split("\n\n"):
        part = part.strip()
        if part.startswith("# "):
            blocks.append(f"<h1>{_inline(part[2:])}</h1>")
        elif part:
            blocks.append(f"<p>{_inline(part).replace(chr(10), '<br>')}</p>")
    return "".join(blocks)


def assistant_fragment() -> str:
    """One client for control room and docs. It can only call GET /api/help."""
    return r'''
<button id="help-launch" type="button" aria-haspopup="dialog" aria-controls="help-panel" aria-expanded="false">Ask Emux</button>
<aside id="help-panel" role="dialog" aria-modal="false" aria-labelledby="help-title" aria-hidden="true">
  <header><span class="help-mark">EMUX</span><h2 id="help-title">Assistant</h2><div class="help-actions"><button id="help-expand" type="button">Expand</button><button id="help-clear" type="button">Clear</button><button id="help-close" type="button" aria-label="Close assistant">Close</button></div></header>
  <div id="help-thread" role="log" aria-live="polite"></div>
  <form id="help-form"><label class="sr-only" for="help-input">Ask a question about Emux</label><textarea id="help-input" rows="2" maxlength="500" placeholder="Ask a question…"></textarea><div class="help-compose-row"><span>docs · read only · browser history</span><button id="help-send" type="submit">Send</button></div></form>
</aside>
<style>
#help-launch{position:fixed;right:18px;bottom:18px;z-index:130;border:1px solid var(--line,#cabfae);background:var(--bg-raise,#e9e3db);color:var(--amber,#8e6129);padding:10px 15px;font:600 12px "IBM Plex Mono",monospace;cursor:pointer;box-shadow:0 6px 24px rgba(30,26,23,.14)}
#help-panel{position:fixed;z-index:140;inset:0 0 0 auto;width:clamp(360px,32vw,480px);background:var(--bg,#f0ebe4);border-left:1px solid var(--line,#cabfae);box-shadow:-12px 0 36px rgba(30,26,23,.18);display:flex;flex-direction:column;transform:translateX(102%);transition:transform .18s ease;color:var(--text,#1e1a17);font:14px/1.55 "IBM Plex Mono",monospace}
#help-panel[aria-hidden="false"]{transform:translateX(0)}#help-panel.expanded{width:min(760px,50vw)}
body{width:100%;transition:width .18s ease}body.help-open{width:calc(100% - clamp(360px,32vw,480px))}body.help-open.help-expanded{width:calc(100% - min(760px,50vw))}
#help-panel header{height:72px;display:flex;align-items:center;gap:12px;padding:0 28px;border-bottom:1px solid var(--line,#cabfae);background:var(--bg-raise,#e9e3db)}
.help-mark{font:22px "VT323",monospace;color:var(--amber,#8e6129)}#help-panel h2{font:400 29px "VT323",monospace;letter-spacing:1px}.help-actions{margin-left:auto;display:flex;gap:4px}.help-actions button{border:0;background:transparent;color:var(--text-dim,#6b6159);font:11px "IBM Plex Mono",monospace;cursor:pointer;padding:7px}.help-actions button:hover,.help-actions button:focus-visible{color:var(--amber,#8e6129)}
#help-thread{flex:1;overflow:auto;padding:28px;display:flex;flex-direction:column;gap:22px}.help-empty{margin:auto;max-width:320px;text-align:center;color:var(--text-dim,#6b6159);display:flex;flex-direction:column;gap:10px}.help-empty strong{color:var(--text,#1e1a17);font-weight:500}.help-q{align-self:flex-end;max-width:88%;background:var(--bg-card,#e4ded6);border-radius:19px;padding:10px 15px}.help-a{max-width:100%;white-space:pre-wrap}.help-found{color:var(--text-dim,#6b6159);font-size:12px;margin-bottom:12px}.help-sources{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}.help-sources a{color:var(--amber,#8e6129);text-underline-offset:3px}.help-tools{display:flex;gap:7px;margin-top:12px}.help-tools button,.help-error button{border:0;background:transparent;color:var(--text-dim,#6b6159);font:11px "IBM Plex Mono",monospace;cursor:pointer;padding:5px}.help-error,.help-noanswer{border-left:2px solid var(--amber,#8e6129);padding-left:12px;color:var(--text-dim,#6b6159)}.help-loading{color:var(--text-dim,#6b6159);animation:help-pulse 1s infinite alternate}@keyframes help-pulse{to{opacity:.45}}
#help-form{margin:18px 28px 28px;border:1px solid var(--amber-dim,#a9853f);padding:13px 15px 10px;background:var(--bg-card,#e4ded6)}#help-input{display:block;width:100%;resize:none;border:0;outline:0;background:transparent;color:var(--text,#1e1a17);font:14px/1.5 "IBM Plex Mono",monospace;min-height:52px}.help-compose-row{display:flex;align-items:center;color:var(--text-dim,#6b6159);font-size:10px;letter-spacing:1px}#help-send{margin-left:auto;border:0;background:var(--amber,#8e6129);color:var(--on-accent,#f5efe6);font:11px "IBM Plex Mono",monospace;padding:9px 13px;cursor:pointer}#help-send:disabled{opacity:.4}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:800px){body.help-open,body.help-open.help-expanded{width:100%;overflow:hidden}body.help-open> :not(#help-panel):not(#help-launch):not(script):not(style){visibility:hidden}#help-launch{right:12px;bottom:12px}#help-panel,#help-panel.expanded{width:100%;border-left:0}#help-panel header{height:60px;padding:0 16px}#help-thread{padding:20px 16px}#help-form{margin:10px 12px 12px}.help-actions{gap:0}#help-expand{display:none}}
@media(prefers-reduced-motion:reduce){#help-panel{transition:none}.help-loading{animation:none}}
</style>
<script>
(()=>{const $=s=>document.querySelector(s),panel=$("#help-panel"),launch=$("#help-launch"),thread=$("#help-thread"),input=$("#help-input"),store="emux.help.v1";let history=[];try{history=JSON.parse(localStorage.getItem(store)||"[]")}catch(_){history=[]}const esc=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
function open(){document.body.classList.add("help-open");panel.setAttribute("aria-hidden","false");launch.setAttribute("aria-expanded","true");setTimeout(()=>input.focus(),0)}function close(){document.body.classList.remove("help-open","help-expanded");panel.classList.remove("expanded");panel.setAttribute("aria-hidden","true");launch.setAttribute("aria-expanded","false");launch.focus()}function sources(items){return '<div class="help-sources">'+items.map(x=>'<a href="'+esc(x.url)+'">'+esc(x.title)+'</a>').join("")+"</div>"}
function render(){if(!history.length){thread.innerHTML='<div class="help-empty"><strong>Ask how to use Emux</strong><span>Answers come from versioned Emux docs. This read-only assistant cannot control sessions or create product memory.</span></div>';return}thread.innerHTML=history.map((m,i)=>m.role==="user"?'<div class="help-q">'+esc(m.text)+'</div>':m.kind==="error"?'<div class="help-error">'+esc(m.text)+' <button data-retry="'+i+'">Retry</button></div>':m.kind==="none"?'<div class="help-noanswer">I could not find that in the Emux documentation.'+sources(m.sources||[])+'<div class="help-tools"><button data-copy="'+i+'">Copy</button><button data-retry="'+i+'">Retry</button></div></div>':'<div class="help-a"><div class="help-found">Found in Emux docs</div>'+esc(m.text)+sources(m.sources||[])+'<div class="help-tools"><button data-feedback="up">Helpful</button><button data-feedback="down">Not helpful</button><button data-copy="'+i+'">Copy</button><button data-retry="'+i+'">Retry</button></div></div>').join("");thread.scrollTop=thread.scrollHeight}function save(){localStorage.setItem(store,JSON.stringify(history.slice(-20)))}
async function ask(q){history.push({role:"user",text:q});render();const loading=document.createElement("div");loading.className="help-loading";loading.textContent="Searching Emux documentation…";thread.appendChild(loading);$("#help-send").disabled=true;try{const r=await fetch("/api/help?q="+encodeURIComponent(q),{method:"GET",headers:{Accept:"application/json"}});const data=await r.json();loading.remove();if(!r.ok||!data.ok)throw Error();history.push({role:"assistant",kind:data.found?"answer":"none",text:data.answer||"",sources:data.sources||[],query:q})}catch(e){loading.remove();history.push({role:"assistant",kind:"error",text:"The help service is unavailable.",query:q})}finally{$("#help-send").disabled=false;save();render()}}
launch.addEventListener("click",open);$("#help-close").addEventListener("click",close);$("#help-expand").addEventListener("click",()=>{panel.classList.toggle("expanded");document.body.classList.toggle("help-expanded",panel.classList.contains("expanded"))});$("#help-clear").addEventListener("click",()=>{history=[];save();render();input.focus()});$("#help-form").addEventListener("submit",e=>{e.preventDefault();const q=input.value.trim();if(q){input.value="";ask(q)}});input.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("#help-form").requestSubmit()}if(e.key==="Escape")close()});document.addEventListener("keydown",e=>{if(e.key==="Escape"&&panel.getAttribute("aria-hidden")==="false")close()});thread.addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;if(b.dataset.copy!==undefined){navigator.clipboard&&navigator.clipboard.writeText(history[+b.dataset.copy].text||"");b.textContent="Copied"}if(b.dataset.retry!==undefined){const m=history[+b.dataset.retry];if(m&&m.query)ask(m.query)}if(b.dataset.feedback)b.textContent="Saved"});render()})();
</script>'''


def docs_page(version: str) -> str:
    nav = "".join(f'<a href="#{page.slug}">{html.escape(page.title)}</a>' for page in pages())
    articles = "".join(f'<article id="{page.slug}">{_render_markdown(page.body)}</article>' for page in pages())
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Emux documentation</title><link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet"><style>:root{{--bg:#f0ebe4;--bg-raise:#e9e3db;--bg-card:#e4ded6;--amber:#8e6129;--amber-dim:#a9853f;--line:#cabfae;--text:#1e1a17;--text-dim:#6b6159;--on-accent:#f5efe6}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 "IBM Plex Mono",monospace}}header.docs-top{{position:sticky;top:0;z-index:10;height:70px;display:flex;align-items:center;padding:0 28px;background:var(--bg-raise);border-bottom:1px solid var(--line)}}.docs-brand{{font:36px "VT323",monospace;color:var(--amber);letter-spacing:2px}}.docs-top span{{margin-left:14px;color:var(--text-dim);font-size:11px;letter-spacing:2px}}.docs-top a{{margin-left:auto;color:var(--amber)}}.docs-shell{{max-width:1180px;margin:auto;display:grid;grid-template-columns:250px minmax(0,720px);gap:56px;padding:42px 28px 120px}}nav{{position:sticky;top:105px;align-self:start;display:flex;flex-direction:column;border-left:1px solid var(--line)}}nav a{{padding:9px 16px;color:var(--text-dim);text-decoration:none}}nav a:hover,nav a:focus{{color:var(--amber)}}article{{padding-bottom:55px;margin-bottom:48px;border-bottom:1px solid var(--line);scroll-margin-top:100px}}h1{{font:42px/1.1 "VT323",monospace;color:var(--amber);letter-spacing:1px}}p{{margin:18px 0}}code{{background:var(--bg-card);padding:2px 5px;color:var(--amber)}}@media(max-width:720px){{.docs-shell{{display:block;padding:24px 18px 100px}}nav{{position:static;overflow-x:auto;flex-direction:row;border-left:0;border-bottom:1px solid var(--line);margin-bottom:35px}}nav a{{white-space:nowrap}}h1{{font-size:35px}}.docs-top{{padding:0 16px}}.docs-top span{{display:none}}}}</style></head><body><header class="docs-top"><strong class="docs-brand">EMUX</strong><span>DOCUMENTATION · {html.escape(version)}</span><a href="/">control room</a></header><div class="docs-shell"><nav aria-label="Documentation sections">{nav}</nav><main>{articles}</main></div>{assistant_fragment()}</body></html>'''


def control_room_page(page: str, version: str) -> str:
    return page.replace("__VERSION__", version).replace("</body>", assistant_fragment() + "</body>")
