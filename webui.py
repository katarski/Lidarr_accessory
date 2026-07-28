"""
Compact manual-attention dashboard (backlog #11).

A dependency-free stdlib http.server dashboard the user drives to resolve items
the pipeline couldn't finish. It is a thin JSON API + a single client-rendered
page:

  GET  /                 the dashboard (HTML shell; renders client-side)
  GET  /api/held         [{id, artist, album, source_path, outcome, reason,
                           tracks, created, seen_count, details{...}}]
  GET  /api/activity     ongoing conversions (SACD/DVD-Audio/DTS/DSD/archive)
  GET  /healthz          {ok, held}
  POST /api/held/keep    {id}  -> discard held files (keep Lidarr's library)
  POST /api/held/move    {id}  -> move held files into the library + rescan

`details` carries the audio profile (formats, lossless/lossy, channels,
sample-rate/bits, size) so the user can decide from the table alone. The page
has tabs (Needs attention / In progress), a text filter, condition chips
(lossless / has-lossy / multichannel / stereo / by-outcome) and column toggles
("add/exclude details"). Actions call back through the `actions` object
(keep_existing / move_held), which returns (ok, message).

Runs on its own daemon thread; stop_event + server.shutdown() ends it.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Protocol
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class HeldActions(Protocol):
    def keep_existing(self, entry: Dict[str, Any]) -> "tuple[bool, str]": ...
    def move_held(self, entry: Dict[str, Any]) -> "tuple[bool, str]": ...


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cue_pipeline</title>
<style>
  :root{color-scheme:light dark;--bg:#0f1115;--fg:#e6e6e6;--card:#1a1d24;--bd:#2a2e37;--mut:#8a93a2;--acc:#7c3aed;--chip:#232733;--chipOn:#3b2d63;--ok:#2f9e57;--warn:#f0b45e;--danger:#c0453b}
  @media(prefers-color-scheme:light){:root{--bg:#f5f6f8;--fg:#1a1a1a;--card:#fff;--bd:#e2e4e8;--mut:#5a6270;--chip:#eceef2;--chipOn:#e0d6fb}}
  *{box-sizing:border-box}
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--fg);font-size:13px}
  header{padding:.7rem 1rem;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:.8rem;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
  h1{font-size:1rem;margin:0;font-weight:650}
  .tabs{display:flex;gap:.3rem}
  .tab{padding:.3rem .7rem;border-radius:7px;background:var(--chip);cursor:pointer;border:1px solid transparent}
  .tab.on{background:var(--chipOn);border-color:var(--acc)}
  .tab .n{opacity:.7;font-size:.8em;margin-left:.3rem}
  .grow{flex:1}
  .toolbar{padding:.55rem 1rem;border-bottom:1px solid var(--bd);display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
  input[type=search]{background:var(--card);border:1px solid var(--bd);color:var(--fg);border-radius:7px;padding:.35rem .6rem;min-width:190px}
  .chip{padding:.25rem .55rem;border-radius:999px;background:var(--chip);cursor:pointer;font-size:.8rem;border:1px solid transparent;user-select:none}
  .chip.on{background:var(--chipOn);border-color:var(--acc)}
  .cols{position:relative}
  .colmenu{position:absolute;right:0;top:110%;background:var(--card);border:1px solid var(--bd);border-radius:9px;padding:.5rem .7rem;display:none;z-index:9;min-width:150px}
  .colmenu.on{display:block}
  .colmenu label{display:block;padding:.15rem 0;white-space:nowrap;cursor:pointer}
  main{padding:.5rem 1rem 3rem}
  table{border-collapse:collapse;width:100%}
  th,td{text-align:left;padding:.4rem .5rem;border-bottom:1px solid var(--bd);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;cursor:pointer;white-space:nowrap}
  td.path{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.72rem;color:var(--mut);max-width:360px;overflow-wrap:anywhere}
  .title{font-weight:600}
  .badge{display:inline-block;font-size:.68rem;padding:.05rem .4rem;border-radius:999px;background:var(--chip);margin-right:.25rem;white-space:nowrap}
  .b-mc{background:#173a2a;color:#5fe0a0}.b-lossy{background:#3a2a12;color:var(--warn)}.b-ll{background:#14243a;color:#6bb6ff}
  .b-out{background:#3a1e1c;color:#f0857a}
  .acts{display:flex;gap:.3rem;flex-wrap:wrap}
  button{font:inherit;border:0;border-radius:7px;padding:.32rem .6rem;cursor:pointer;white-space:nowrap}
  .b-copy{background:var(--chip);color:var(--fg)}.b-keep{background:#334155;color:#fff}.b-move{background:var(--acc);color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .empty{padding:3rem;text-align:center;color:var(--mut)}
  .muted{color:var(--mut)}
  .toast{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:#111;color:#fff;padding:.55rem 1rem;border-radius:10px;opacity:0;transition:opacity .2s;border:1px solid #333;max-width:92vw;z-index:20}
  .toast.show{opacity:1}
  #ctx{position:fixed;display:none;background:var(--card);border:1px solid var(--bd);border-radius:9px;padding:.25rem;z-index:30;min-width:170px;box-shadow:0 6px 24px rgba(0,0,0,.4)}
  #ctx.on{display:block}
  #ctx .mi{padding:.4rem .6rem;border-radius:6px;cursor:pointer;white-space:nowrap;font-size:.82rem}
  #ctx .mi:hover{background:var(--chipOn)}
  #ctx .mh{padding:.3rem .6rem;color:var(--mut);font-size:.72rem;border-bottom:1px solid var(--bd);margin-bottom:.2rem;max-width:260px;overflow:hidden;text-overflow:ellipsis}
  #vfilters{display:flex;gap:.35rem;flex-wrap:wrap}
  .vf{padding:.2rem .5rem;border-radius:999px;background:var(--chipOn);border:1px solid var(--acc);font-size:.75rem;cursor:pointer}
  .vf.exc{background:#3a1e1c;border-color:var(--danger)}
  td .ex{color:var(--mut)}
  .up{color:#5fe0a0}.dn{color:var(--warn)}
  .spin{color:var(--mut);font-size:.8rem}
  .prog{display:flex;flex-direction:column;gap:.5rem}
  .procard{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.6rem .8rem;display:flex;justify-content:space-between;gap:1rem;align-items:center}
</style></head>
<body>
<header>
  <h1>cue_pipeline</h1>
  <div class="tabs">
    <div class="tab on" data-tab="attention" onclick="setTab('attention')">Needs attention <span class="n" id="n-att">0</span></div>
    <div class="tab" data-tab="progress" onclick="setTab('progress')">In progress <span class="n" id="n-prog">0</span></div>
  </div>
  <div class="grow"></div>
  <span class="spin" id="updated"></span>
  <button class="b-copy" onclick="refresh()">Refresh</button>
</header>

<div class="toolbar" id="toolbar">
  <input type="search" id="q" placeholder="filter by artist / album / path / reason…" oninput="render()">
  <span class="chip" data-f="lossless" onclick="toggleFilter(this)">Lossless</span>
  <span class="chip" data-f="lossy" onclick="toggleFilter(this)">Has lossy</span>
  <span class="chip" data-f="multichannel" onclick="toggleFilter(this)">Multichannel</span>
  <span class="chip" data-f="stereo" onclick="toggleFilter(this)">Stereo</span>
  <span id="outchips"></span>
  <div id="vfilters"></div>
  <div class="grow"></div>
  <div class="cols">
    <button class="b-copy" onclick="document.getElementById('cm').classList.toggle('on')">Columns ▾</button>
    <div class="colmenu" id="cm"></div>
  </div>
</div>

<main>
  <div id="attention"></div>
  <div id="progress" style="display:none"></div>
</main>
<div id="toast" class="toast"></div>
<div id="ctx"></div>

<script>
var HELD=[], ACT=[], TAB='attention';
var FILT={lossless:false,lossy:false,multichannel:false,stereo:false,outcomes:{}};
var COLS=[
 {k:'title',   name:'Album',            on:true},
 {k:'quality', name:'Held quality',     on:true},
 {k:'existing',name:'Existing (library)',on:true},
 {k:'verdict', name:'Compare',          on:true},
 {k:'formats', name:'Formats',          on:false},
 {k:'ch',      name:'Channels',         on:true},
 {k:'tracks',  name:'Tracks',           on:true},
 {k:'size',    name:'Size',             on:true},
 {k:'outcome', name:'Status',           on:true},
 {k:'reason',  name:'Reason',           on:false},
 {k:'path',    name:'Path',             on:true},
];
// ServiceNow-style per-value filters set from the right-click menu:
// INC[col] = set of values a row's cell MUST match; EXC[col] = must NOT match.
var INC={}, EXC={};
function score(d){d=d||{};return (d.lossless?2:0)+(d.multichannel?2:0)+(d.max_channels||0)/10+(d.bits||0)/100+(d.sample_rate||0)/1e7;}
function h(s){return (s==null?'':''+s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function human(b){b=b||0;var u=['B','KB','MB','GB','TB'],i=0;while(b>=1024&&i<u.length-1){b/=1024;i++;}return b.toFixed(b<10&&i>0?1:0)+u[i];}
function ago(t){if(!t)return'';var s=Math.max(0,Date.now()/1000-t);if(s<90)return Math.round(s)+'s';if(s<5400)return Math.round(s/60)+'m';if(s<172800)return Math.round(s/3600)+'h';return Math.round(s/86400)+'d';}
function toast(m){var t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(function(){t.classList.remove('show');},3200);}
function setTab(t){TAB=t;document.querySelectorAll('.tab').forEach(function(e){e.classList.toggle('on',e.dataset.tab===t);});
  document.getElementById('attention').style.display=t==='attention'?'':'none';
  document.getElementById('progress').style.display=t==='progress'?'':'none';
  document.getElementById('toolbar').style.display=t==='attention'?'':'none';}
function toggleFilter(el){var f=el.dataset.f;FILT[f]=!FILT[f];el.classList.toggle('on',FILT[f]);render();}
function fmtStr(d){if(!d||!d.formats)return'';return Object.keys(d.formats).map(function(k){return k.replace('.','')+'×'+d.formats[k];}).join(' ');}
function buildColMenu(){var cm=document.getElementById('cm');cm.innerHTML=COLS.map(function(c,i){return '<label><input type="checkbox" '+(c.on?'checked':'')+' onchange="COLS['+i+'].on=this.checked;render()"> '+h(c.name)+'</label>';}).join('');}
function buildOutChips(){var outs={};HELD.forEach(function(x){outs[x.outcome||'held']=(outs[x.outcome||'held']||0)+1;});
  document.getElementById('outchips').innerHTML=Object.keys(outs).sort().map(function(o){return '<span class="chip'+(FILT.outcomes[o]?' on':'')+'" onclick="FILT.outcomes[\''+o+'\']=!FILT.outcomes[\''+o+'\'];this.classList.toggle(\'on\');render()">'+h(o)+' '+outs[o]+'</span>';}).join('');}
function matches(x){var d=x.details||{};
  if(FILT.lossless&&!d.lossless)return false;
  if(FILT.lossy&&!d.has_lossy)return false;
  if(FILT.multichannel&&!d.multichannel)return false;
  if(FILT.stereo&&!(d.max_channels&&d.max_channels<=2))return false;
  var anyOut=Object.keys(FILT.outcomes).some(function(k){return FILT.outcomes[k];});
  if(anyOut&&!FILT.outcomes[x.outcome||'held'])return false;
  // ServiceNow-style per-value include/exclude (from the right-click menu).
  for(var ck in INC){if(INC[ck]&&INC[ck].size&&!INC[ck].has(cellText(ck,x)))return false;}
  for(var ek in EXC){if(EXC[ek]&&EXC[ek].has(cellText(ek,x)))return false;}
  var q=(document.getElementById('q').value||'').toLowerCase().trim();
  if(q){var hay=((x.artist||'')+' '+(x.album||'')+' '+(x.source_path||'')+' '+(x.reason||'')).toLowerCase();if(hay.indexOf(q)<0)return false;}
  return true;}
function colName(k){var c=COLS.filter(function(z){return z.k===k;})[0];return c?c.name:k;}
function addValueFilter(kind,col,val){var M=kind==='inc'?INC:EXC;(M[col]=M[col]||new Set()).add(val);
  // include+exclude of the same value is contradictory -> drop the opposite
  var O=kind==='inc'?EXC:INC;if(O[col])O[col].delete(val);render();}
function clearValueFilter(kind,col,val){var M=kind==='inc'?INC:EXC;if(M[col]){M[col].delete(val);if(!M[col].size)delete M[col];}render();}
function renderVFilters(){var out='';['inc','exc'].forEach(function(kind){var M=kind==='inc'?INC:EXC;for(var col in M){M[col].forEach(function(v){
  out+='<span class="vf'+(kind==='exc'?' exc':'')+'" title="click to remove" onclick="clearValueFilter(\''+kind+'\',\''+col+'\','+JSON.stringify(v).replace(/"/g,'&quot;')+')">'+(kind==='exc'?'≠ ':'= ')+h(colName(col))+': '+h(v)+' ✕</span>';});}});
  document.getElementById('vfilters').innerHTML=out;}
function chLabel(n){return n===6?'5.1':n===8?'7.1':n===2?'stereo':n===4?'quad':n?(n+'ch'):'';}
function cell(c,x){var d=x.details||{},ex=x.existing||{};switch(c.k){
  case'title':var t=((x.artist||'')+' — '+(x.album||'')).replace(/^ — | — $/,'')||(x.source_path||'').split('/').pop();return '<span class="title">'+h(t)+'</span>';
  case'quality':return h(d.quality||'');
  case'existing':
    if(!ex||ex.in_library!==true)return '<span class="ex">not in library</span>';
    return '<span class="ex">'+h(ex.quality||'')+' · '+(ex.n_audio||0)+'trk · '+human(ex.total_bytes)+'</span>';
  case'verdict':
    if(!ex||ex.in_library!==true)return '<span class="badge b-mc">held is new</span>';
    var sh=score(d),se=score(ex);
    if(sh>se+0.01)return '<span class="badge b-mc">held better ↑</span>';
    if(se>sh+0.01)return '<span class="badge b-lossy">library better ↓</span>';
    return '<span class="badge">≈ same</span>';
  case'formats':return '<span class="muted">'+h(fmtStr(d))+'</span>';
  case'ch':var b='';if(d.multichannel)b='<span class="badge b-mc">'+h(chLabel(d.max_channels))+'</span>';else if(d.max_channels)b='<span class="badge">stereo</span>';var q=d.lossless?'<span class="badge b-ll">lossless</span>':'';if(d.has_lossy)q+='<span class="badge b-lossy">lossy</span>';return b+q;
  case'tracks':return d.n_audio||x.tracks||0;
  case'size':return '<span class="muted">'+human(d.total_bytes)+'</span>';
  case'outcome':return '<span class="badge b-out">'+h(x.outcome||'held')+'</span>';
  case'reason':return '<span class="muted">'+h(x.reason||'')+'</span>';
  case'path':return '<span class="path" id="p-'+x.id+'">'+h(x.source_path||'')+'</span>';
  default:return '';}}
// Plain-text value of a cell, used for the right-click value filters.
function cellText(k,x){var d=x.details||{},ex=x.existing||{};switch(k){
  case'title':return ((x.artist||'')+' — '+(x.album||'')).replace(/^ — | — $/,'')||(x.source_path||'').split('/').pop();
  case'quality':return d.quality||'';
  case'existing':return (ex&&ex.in_library)?(ex.quality||''):'not in library';
  case'verdict':if(!ex||ex.in_library!==true)return 'held is new';var a=score(d),b2=score(ex);return a>b2+0.01?'held better':(b2>a+0.01?'library better':'same');
  case'formats':return fmtStr(d);
  case'ch':return d.multichannel?chLabel(d.max_channels):(d.max_channels?'stereo':'');
  case'tracks':return ''+(d.n_audio||x.tracks||0);
  case'size':return human(d.total_bytes);
  case'outcome':return x.outcome||'held';
  case'reason':return x.reason||'';
  case'path':return x.source_path||'';
  default:return '';}}
function render(){
  buildOutChips();renderVFilters();
  var cols=COLS.filter(function(c){return c.on;});
  var rows=HELD.filter(matches);
  document.getElementById('n-att').textContent=HELD.length;
  var head='<tr>'+cols.map(function(c){return '<th>'+h(c.name)+'</th>';}).join('')+'<th>Actions</th></tr>';
  var body=rows.map(function(x){
    var tds=cols.map(function(c){return '<td data-col="'+c.k+'" data-val="'+h(cellText(c.k,x))+'">'+cell(c,x)+'</td>';}).join('');
    var acts='<div class="acts" data-id="'+x.id+'">'
      +'<button class="b-copy" onclick="copyPath(\''+x.id+'\')">Copy</button>'
      +'<button class="b-keep" onclick="act(\''+x.id+'\',\'keep\')">Keep existing</button>'
      +'<button class="b-move" onclick="act(\''+x.id+'\',\'move\')">Move held</button></div>';
    return '<tr>'+tds+'<td>'+acts+'</td></tr>';
  }).join('');
  document.getElementById('attention').innerHTML=rows.length
    ? '<table>'+head+body+'</table>'
    : (HELD.length?'<div class="empty">No items match the current filters.</div>':'<div class="empty">Nothing needs attention right now. 🎉</div>');
  // progress tab
  document.getElementById('n-prog').textContent=ACT.length;
  document.getElementById('progress').innerHTML=ACT.length
    ? '<div class="prog">'+ACT.map(function(a){return '<div class="procard"><div><span class="badge b-out">'+h(a.stage)+'</span> <span class="title">'+h(a.name)+'</span>'+(a.detail?' <span class="muted">'+h(a.detail)+'</span>':'')+'</div><span class="muted">'+ago(a.started)+'</span></div>';}).join('')+'</div>'
    : '<div class="empty">No conversions running.</div>';
}
function copyPath(id){var el=document.getElementById('p-'+id);var p=el?el.textContent:(HELD.find(function(x){return x.id===id;})||{}).source_path;
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(p).then(function(){toast('Path copied');},function(){toast('Copy failed');});}else toast('Clipboard unavailable');}
function act(id,kind){
  var msg=kind==='move'?'Move the held files into the Lidarr library and rescan? This overwrites what Lidarr has for this album.':'Discard the held files and keep what Lidarr already has? The stuck folder will be deleted.';
  if(!confirm(msg))return;
  document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){b.disabled=true;});
  fetch('/api/held/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})})
   .then(function(r){return r.json();}).then(function(j){toast(j.message||(j.ok?'Done':'Failed'));
     if(j.ok){HELD=HELD.filter(function(x){return x.id!==id;});render();}
     else document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){b.disabled=false;});})
   .catch(function(e){toast('Error: '+e);document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){b.disabled=false;});});
}
function refresh(){
  Promise.all([fetch('/api/held').then(function(r){return r.json();}),fetch('/api/activity').then(function(r){return r.json();}).catch(function(){return{items:[]};})])
   .then(function(res){HELD=res[0].items||[];ACT=(res[1]||{}).items||[];document.getElementById('updated').textContent='updated '+new Date().toLocaleTimeString();render();});
}
// Right-click a table cell -> ServiceNow-style "Show matching" / "Filter out".
var ctx=document.getElementById('ctx');
document.getElementById('attention').addEventListener('contextmenu',function(e){
  var td=e.target.closest('td[data-col]');if(!td)return;e.preventDefault();
  var col=td.getAttribute('data-col'),val=td.getAttribute('data-val')||'';
  ctx.innerHTML='<div class="mh">'+h(colName(col))+': <b>'+h(val||'(empty)')+'</b></div>'
   +'<div class="mi" data-a="inc">Show matching</div>'
   +'<div class="mi" data-a="exc">Filter out</div>'
   +'<div class="mi" data-a="clear">Clear this column\'s filters</div>';
  ctx.style.left=Math.min(e.clientX,window.innerWidth-190)+'px';
  ctx.style.top=Math.min(e.clientY,window.innerHeight-120)+'px';ctx.classList.add('on');
  ctx.querySelectorAll('.mi').forEach(function(mi){mi.onclick=function(){
    var a=mi.getAttribute('data-a');
    if(a==='inc')addValueFilter('inc',col,val);
    else if(a==='exc')addValueFilter('exc',col,val);
    else{delete INC[col];delete EXC[col];render();}
    ctx.classList.remove('on');};});
});
document.addEventListener('click',function(){ctx.classList.remove('on');});
document.addEventListener('scroll',function(){ctx.classList.remove('on');},true);
buildColMenu();setTab('attention');refresh();setInterval(refresh,15000);
</script>
</body></html>"""


def make_handler(store, actions: HeldActions):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cue_pipeline-webui"

        def log_message(self, fmt, *args):
            logger.debug("webui: " + fmt, *args)

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/held":
                store.prune_missing()
                items = store.list()
                # Enrich each item with the library's EXISTING album (for the
                # held-vs-existing compare), computed once and cached.
                if hasattr(actions, "existing_album_summary"):
                    for it in items:
                        if "existing" not in it:
                            try:
                                ex = actions.existing_album_summary(it)
                            except Exception:  # noqa: BLE001
                                ex = {}
                            store.update(it["id"], existing=ex)
                            it["existing"] = ex
                self._json(200, {"items": items})
            elif path == "/api/activity":
                acts = actions.list_activity() if hasattr(actions, "list_activity") else []
                self._json(200, {"items": acts})
            elif path == "/healthz":
                self._json(200, {"ok": True, "held": len(store.list())})
            else:
                self._json(404, {"ok": False, "message": "not found"})

        def _read_id(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return (json.loads(raw or b"{}") or {}).get("id", "")
            except Exception:  # noqa: BLE001
                return (parse_qs(raw.decode("utf-8", "replace")).get("id", [""]) or [""])[0]

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/api/held/keep", "/api/held/move"):
                self._json(404, {"ok": False, "message": "not found"})
                return
            eid = self._read_id()
            entry = store.get(eid) if eid else None
            if not entry:
                self._json(404, {"ok": False, "message": "item not found (already resolved?)"})
                return
            try:
                if path.endswith("/keep"):
                    ok, msg = actions.keep_existing(entry)
                else:
                    ok, msg = actions.move_held(entry)
            except Exception as exc:  # noqa: BLE001
                logger.exception("webui: action failed for %s", eid)
                self._json(500, {"ok": False, "message": f"error: {exc}"})
                return
            if ok:
                store.remove(eid)
            self._json(200 if ok else 409, {"ok": ok, "message": msg})

    return Handler


def run_webui(store, actions: HeldActions, host: str, port: int,
              stop_event: threading.Event) -> None:
    """Serve the dashboard until stop_event is set. Blocks; run on a thread."""
    httpd = ThreadingHTTPServer((host, port), make_handler(store, actions))
    httpd.daemon_threads = True
    logger.info("WebUI: listening on http://%s:%d", host, port)
    watcher = threading.Thread(
        target=lambda: (stop_event.wait(), httpd.shutdown()), daemon=True)
    watcher.start()
    try:
        httpd.serve_forever(poll_interval=1.0)
    finally:
        httpd.server_close()
        logger.info("WebUI: stopped")
