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
import os
import re
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Protocol
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class HeldActions(Protocol):
    def keep_existing(self, entry: Dict[str, Any]) -> "tuple[bool, str]": ...
    def move_held(self, entry: Dict[str, Any]) -> "tuple[bool, str]": ...


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cue_pipeline</title>
<link rel="icon" type="image/png" href="https://raw.githubusercontent.com/lidarr/Lidarr/develop/Logo/256.png">
<link rel="shortcut icon" href="https://raw.githubusercontent.com/lidarr/Lidarr/develop/Logo/256.png">
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
  tbody tr.mainrow td{transition:background .1s}
  tbody tr.zebra td{background:rgba(255,255,255,.030)}
  @media(prefers-color-scheme:light){tbody tr.zebra td{background:rgba(0,0,0,.028)}}
  tbody tr.mainrow:hover td{background:rgba(124,58,237,.10)}
  tbody tr.sel-on td{background:rgba(124,58,237,.20)!important}
  th{color:var(--mut);font-weight:600;font-size:.75rem;text-transform:uppercase;letter-spacing:.03em;cursor:pointer;white-space:nowrap}
  td.path{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.72rem;color:var(--mut);max-width:360px;overflow-wrap:anywhere}
  .title{font-weight:600}
  .badge{display:inline-block;font-size:.68rem;padding:.05rem .4rem;border-radius:999px;background:var(--chip);margin-right:.25rem;white-space:nowrap}
  .b-mc{background:#173a2a;color:#5fe0a0}.b-lossy{background:#3a2a12;color:var(--warn)}.b-ll{background:#14243a;color:#6bb6ff}
  .b-out{background:#3a1e1c;color:#f0857a}
  .acts{display:flex;gap:.3rem;flex-wrap:wrap}
  button{font:inherit;border:0;border-radius:7px;padding:.32rem .6rem;cursor:pointer;white-space:nowrap}
  .b-copy{background:var(--chip);color:var(--fg)}.b-keep{background:#334155;color:#fff}.b-move{background:var(--acc);color:#fff}.b-disc{background:var(--danger);color:#fff}
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
  .ttog{cursor:pointer;color:var(--acc);font-weight:700;margin-right:.3rem;user-select:none;display:inline-block;width:1ch}
  tr.det>td{background:var(--bg);border-bottom:2px solid var(--acc);padding:.6rem 1rem}
  .cmp{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
  @media(max-width:720px){.cmp{grid-template-columns:1fr}}
  .tcol h4{margin:.1rem 0 .4rem;font-size:.8rem;color:var(--mut)}
  .tcol{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:.5rem .7rem;max-height:340px;overflow:auto}
  .td>summary{cursor:pointer;list-style:revert}
  .tind{padding-left:1rem;border-left:1px dotted var(--bd);margin-left:.3rem}
  .tf{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.75rem;padding:.05rem 0}
  .tf .muted{font-size:.9em}
  .spin{color:var(--mut);font-size:.8rem}
  .prog{display:flex;flex-direction:column;gap:.5rem}
  .procard{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.6rem .8rem;display:flex;justify-content:space-between;gap:1rem;align-items:center}
  #bulkn{font-weight:600}
  .toolbar select{font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:7px;padding:.35rem .55rem}
  .cols button{font-size:1rem;line-height:1;padding:.35rem .55rem}
  td.sel,th.sel{width:1.6rem;text-align:center;padding-left:.6rem}
  /* Converter tab */
  .cvsec{background:var(--card);border:1px solid var(--bd);border-radius:10px;margin-bottom:.55rem;padding:.15rem .7rem}
  .cvsec>summary{cursor:pointer;font-weight:600;padding:.4rem 0;user-select:none}
  .cvbody{padding:.3rem 0 .55rem;display:flex;flex-direction:column;gap:.4rem}
  .cvbar{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.5rem .7rem;margin-bottom:.55rem}
  .cvbar label{display:flex;flex-direction:column;font-size:.68rem;color:var(--mut);gap:.15rem}
  .cvbar select{font:inherit;font-size:.78rem;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:7px;padding:.28rem .45rem}
  .cvtree{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:.4rem .6rem;max-height:62vh;overflow:auto;font-size:.78rem}
  .cvrow{display:flex;align-items:center;gap:.45rem;padding:.12rem .25rem;border-radius:6px;white-space:nowrap}
  .cvrow:hover{background:rgba(125,125,125,.12)}
  .cvrow .nm{overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:0}
  .cvrow .grow{flex:1}
  .cvrow .meta{color:var(--mut);font-size:.72rem;flex:0 0 auto}
  .cvcaret{cursor:pointer;width:1rem;display:inline-block;text-align:center;color:var(--mut)}
  .cvkids{margin-left:1.25rem;border-left:1px dotted var(--bd);padding-left:.45rem}
  .pbar{height:.55rem;background:rgba(125,125,125,.18);border-radius:5px;overflow:hidden;flex:1;min-width:8rem}
  .pfill{height:100%;background:#4f8ef7;border-radius:5px;transition:width .6s}
  .pline{display:flex;align-items:center;gap:.6rem}
  .pline .plab{flex:0 0 22rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.74rem}
  .pline .ppct{flex:0 0 3.2rem;text-align:right;font-size:.72rem;color:var(--mut)}
  #cv-modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:60}
  #cv-modal .mbox{background:var(--card);border:1px solid var(--bd);border-radius:12px;max-width:44rem;max-height:80vh;overflow:auto;padding:1rem 1.2rem;min-width:24rem}
  #cv-modal table{width:100%;font-size:.75rem}
  #cv-modal td{padding:.15rem .4rem;vertical-align:top}
  #cv-modal td:first-child{color:var(--mut);white-space:nowrap}
  /* ---- web player ---------------------------------------------------- */
  .playable{cursor:pointer}
  .playable:hover{text-decoration:underline}
  .playable.on{color:#58a6ff;font-weight:600}
  #pl{position:fixed;left:1rem;top:1rem;width:24rem;max-width:94vw;z-index:60;
      background:var(--pan,#161b22);border:1px solid var(--bd,#30363d);
      border-radius:8px;box-shadow:0 8px 28px #000a;display:none}
  #pl.on{display:block}
  #pl .plhead{display:flex;align-items:center;gap:.5rem;padding:.5rem .6rem;
      border-bottom:1px solid var(--bd,#30363d)}
  #pl .plname{font-weight:600;overflow:hidden;text-overflow:ellipsis;
      white-space:nowrap;flex:1}
  #pl .plbody{padding:.5rem .6rem}
  #pl .plbtns{display:flex;gap:.35rem;align-items:center;margin:.35rem 0}
  #pl .plbtns button{min-width:2.4rem}
  #pl #pl-vol{width:5.5rem;flex:0 0 auto;accent-color:#58a6ff;cursor:pointer}
  #pl audio{width:100%;margin-top:.25rem}
  #pl details{margin-top:.4rem}
  #pl details summary{cursor:pointer;color:var(--mut,#8b949e)}
  #pl table{border-collapse:collapse;font-size:.82rem;width:100%;
      max-height:11rem;display:block;overflow:auto}
  #pl td{padding:.1rem .35rem;vertical-align:top;border-bottom:1px solid #21262d}
  #pl td:first-child{color:var(--mut,#8b949e);white-space:nowrap}
</style></head>
<body>
<header>
  <h1>cue_pipeline</h1>
  <div class="tabs">
    <div class="tab on" data-tab="attention" onclick="setTab('attention')">Needs attention <span class="n" id="n-att">0</span></div>
    <div class="tab" data-tab="assembly" onclick="setTab('assembly')">Assembly <span class="n" id="n-asm">0</span></div>
    <div class="tab" data-tab="progress" onclick="setTab('progress')">Converter <span class="n" id="n-prog">0</span></div>
    <div class="tab" data-tab="log" onclick="setTab('log')">Log</div>
    <div class="tab" data-tab="settings" onclick="setTab('settings')">Settings</div>
  </div>
  <div class="grow"></div>
  <span class="spin" id="updated"></span>
  <button class="b-copy" onclick="refresh()">Refresh</button>
  <button class="b-copy" title="Restart the cue_pipeline container" onclick="ctrl('restart')">⟳ Restart</button>
  <button class="b-copy" title="Stop the cue_pipeline container" onclick="ctrl('shutdown')">⏻ Shutdown</button>
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
  <span id="bulkn" class="muted">0 selected</span>
  <select id="bulkact" onchange="if(this.value){bulkAct(this.value);this.value='';}">
    <option value="">Actions on selected rows…</option>
    <option value="keep">Add to library</option>
    <option value="move">Overwrite</option>
    <option value="discard">Discard</option>
  </select>
  <button class="b-copy" onclick="clearSel()" title="Clear selection">✕</button>
  <div class="cols">
    <button class="b-copy" title="Columns" aria-label="Columns" onclick="document.getElementById('cm').classList.toggle('on')">⚙</button>
    <div class="colmenu" id="cm"></div>
  </div>
</div>
<main>
  <div id="attention"></div>
  <div id="assembly" style="display:none">
    <div class="procard" style="justify-content:flex-start;gap:1rem;flex-wrap:wrap">
      <input type="search" id="asm-q" placeholder="filter by artist / album / song…" oninput="asmRender()" style="min-width:18rem">
      <label><input type="checkbox" id="asm-complete" onchange="asmRender()"> only 100% assembled</label>
      <span class="grow"></span>
      <span class="muted" id="asm-sum"></span>
      <span class="muted" id="asm-seln">0 selected</span>
      <button class="b-copy" onclick="asmAll(true)">Select all</button>
      <button class="b-copy" onclick="asmAll(false)">Clear</button>
      <select id="asmact" onchange="if(this.value){asmAct(this.value);this.value='';}">
        <option value="">Actions on selected albums…</option>
        <option value="find">Find missing songs</option>
        <option value="add">Add to library (assemble + import)</option>
        <option value="remove">Remove (dismiss row, keep files)</option>
      </select>
      <button class="b-copy" onclick="asmLoad()">Reload</button>
    </div>
    <div id="asm-list"></div>
  </div>
  <div id="progress" style="display:none">
    <details id="cv-sec-prog" class="cvsec" open><summary>Progress</summary><div id="cv-prog" class="cvbody"><span class="muted">nothing running</span></div></details>
    <details id="cv-sec-conv" class="cvsec"><summary>Conversions <span class="n" id="cv-nconv">0</span></summary><div id="cv-convlist" class="cvbody"></div></details>
    <details id="cv-sec-split" class="cvsec"><summary>Cue splits <span class="n" id="cv-nsplit">0</span></summary><div id="cv-splitlist" class="cvbody"></div></details>
    <div class="cvbar">
      <label>Codec <select id="cv-codec" onchange="cvCodecChanged()"></select></label>
      <label>Mode <select id="cv-mode" onchange="cvModeChanged()"></select></label>
      <label>Bitrate <select id="cv-bitrate"></select></label>
      <label>Quality <select id="cv-quality"></select></label>
      <label>Sample rate <select id="cv-sr"></select></label>
      <label>Channels <select id="cv-ch"></select></label>
      <label>Files at a time <select id="cv-conc"></select></label>
      <label title="Replace each source file with the converted one: the original (e.g. FLAC) is DELETED after a verified encode, sidecar .xml files are repointed at the new filename, and Lidarr is asked to rescan the album folder."><input type="checkbox" id="cv-overwrite"> Overwrite existing (delete original)</label>
      <span class="muted" id="cv-codechelp"></span>
      <div class="grow"></div>
      <span id="cv-seln" class="muted">0 selected</span>
      <button class="b-move" onclick="cvConvertSel()">Convert selected</button>
      <button class="b-disc" onclick="cvDeleteSel()">Delete selected</button>
    </div>
    <div id="cv-tree" class="cvtree"><div class="empty">Loading library…</div></div>
  </div>
  <div id="log" style="display:none">
    <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem">
      <select id="logwhich" onchange="loadLog()">
        <option value="pipeline">pipeline.log</option>
        <option value="flac2mp3">flac2mp3 (downsampler)</option>
      </select>
      <select id="loglines" onchange="loadLog()">
        <option value="400">last 400 lines</option>
        <option value="4000">last 4,000 lines</option>
        <option value="40000">last 40,000 lines</option>
        <option value="0">entire file</option>
      </select>
      <button class="b-copy" onclick="loadLog()">Refresh log</button>
      <label class="muted"><input type="checkbox" id="logtail" checked> auto-refresh</label>
    </div>
    <pre id="logbox" style="background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:.6rem;max-height:70vh;overflow:auto;font-size:.72rem;white-space:pre-wrap"></pre>
  </div>
  <div id="settings" style="display:none">
    <div class="muted" style="margin-bottom:.6rem">Change grab/behaviour settings here instead of editing the container. Saved settings win over the template and apply on <b>Restart</b>.</div>
    <div id="setform"></div>
    <div style="display:flex;gap:.5rem;margin-top:1rem">
      <button class="b-move" onclick="saveSettings(false)">Save</button>
      <button class="b-keep" onclick="saveSettings(true)">Save &amp; Restart</button>
      <span id="setmsg" class="muted"></span>
    </div>
  </div>
</main>
<div id="pl">
  <div class="plhead">
    <span class="plname" id="pl-name">—</span>
    <button class="b-copy" title="Close player" onclick="plClose()">✕</button>
  </div>
  <div class="plbody">
    <div class="plbtns">
      <button class="b-copy" title="Rewind 10s" onclick="plSeek(-10)">⏪</button>
      <button class="b-copy" title="Play / pause" onclick="plToggle()" id="pl-play">▶</button>
      <button class="b-copy" title="Stop" onclick="plStop()">⏹</button>
      <button class="b-copy" title="Forward 10s" onclick="plSeek(10)">⏩</button>
      <input type="range" id="pl-vol" min="0" max="100" step="1" value="100"
             title="Volume" oninput="plVol(this.value)">
      <span class="muted" id="pl-time" style="margin-left:auto">0:00</span>
    </div>
    <audio id="pl-audio" preload="none" controls></audio>
    <details id="pl-tags"><summary>ID tags</summary><div id="pl-tagbody" class="muted">—</div></details>
    <details id="pl-specs"><summary>Specs</summary><div id="pl-specbody" class="muted">—</div></details>
  </div>
</div>
<div id="toast" class="toast"></div>
<div id="ctx"></div>

<script>
var HELD=[], ACT=[], TAB='attention', SEL=new Set(), VISIBLE=[], OPEN=new Set(), SORT={col:'detected',dir:-1}, SETTINGS=[];
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
 {k:'detected',name:'Detected',         on:true},
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
// ---- Web player -----------------------------------------------------------
// One shared <audio> + popup for every tab. Playback starts IMMEDIATELY on click
// (the stream endpoint supports Range, so the browser buffers as it goes); the
// ID-tag and Specs dropdowns are filled by a separate request that never blocks
// the audio. Any element carrying data-audio="<abs path>" is clickable, so the
// same handler serves Needs attention, Assembly and Converter.
var PLCUR='';
function plEl(){return document.getElementById('pl-audio');}
function plPlace(x,y){
  // Put the popup right under the pointer so it can be confirmed and dismissed
  // without travelling to a corner -- clamped so it never hangs off-screen.
  var el=document.getElementById('pl');
  el.classList.add('on');                       // must be visible to measure
  var w=el.offsetWidth||384, h=el.offsetHeight||220, m=8;
  var left=Math.min(Math.max(m,(x||0)+12), Math.max(m,window.innerWidth-w-m));
  var top =Math.min(Math.max(m,(y||0)+12), Math.max(m,window.innerHeight-h-m));
  el.style.left=left+'px'; el.style.top=top+'px';
}
function plOpen(path,label,x,y){
  if(!path)return;
  var a=plEl();
  if(x!==undefined&&x!==null)plPlace(x,y);
  else document.getElementById('pl').classList.add('on');
  document.getElementById('pl-name').textContent=label||path.split('/').pop();
  if(PLCUR!==path){
    PLCUR=path;
    a.src='/api/audio/stream?path='+encodeURIComponent(path);
    document.getElementById('pl-tagbody').innerHTML='<span class="muted">loading…</span>';
    document.getElementById('pl-specbody').innerHTML='<span class="muted">loading…</span>';
    plInfo(path);
  }
  a.play().catch(function(){/* browser may need a second click; controls still work */});
  document.querySelectorAll('.playable.on').forEach(function(e){e.classList.remove('on');});
  var cur=document.querySelector('.playable[data-audio="'+path.replace(/"/g,'\\"')+'"]');
  if(cur)cur.classList.add('on');
  plSyncBtn();
}
function plInfo(path){
  fetch('/api/audio/info?path='+encodeURIComponent(path))
    .then(function(r){return r.json();}).then(function(j){
      if(PLCUR!==path)return;                       // user moved on already
      var t=j.tags||{},s=j.specs||{};
      function tbl(o){
        var ks=Object.keys(o);
        if(!ks.length)return '<span class="muted">none</span>';
        return '<table>'+ks.map(function(k){
          return '<tr><td>'+h(k)+'</td><td>'+h(String(o[k]))+'</td></tr>';}).join('')+'</table>';
      }
      if(s.duration_s)s.duration=fmtTime(s.duration_s);
      document.getElementById('pl-tagbody').innerHTML=tbl(t);
      document.getElementById('pl-specbody').innerHTML=tbl(s);
    }).catch(function(){
      document.getElementById('pl-tagbody').innerHTML='<span class="muted">unavailable</span>';});
}
function fmtTime(s){s=Math.max(0,Math.floor(s||0));
  return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2);}
function plVol(v){
  var a=plEl(); a.volume=Math.max(0,Math.min(1,(parseFloat(v)||0)/100));
  a.muted=(a.volume===0);
  try{localStorage.setItem('plvol',String(a.volume));}catch(e){}
}
function plVolInit(){
  var v=1; try{var st=localStorage.getItem('plvol'); if(st!==null)v=parseFloat(st);}catch(e){}
  if(!(v>=0&&v<=1))v=1;
  var sl=document.getElementById('pl-vol'); if(sl)sl.value=Math.round(v*100);
  plEl().volume=v; plEl().muted=(v===0);
}
function plToggle(){var a=plEl();if(a.paused)a.play().catch(function(){});else a.pause();plSyncBtn();}
function plStop(){var a=plEl();a.pause();a.currentTime=0;plSyncBtn();}
function plSeek(d){var a=plEl();a.currentTime=Math.max(0,(a.currentTime||0)+d);}
function plClose(){plStop();document.getElementById('pl').classList.remove('on');
  document.querySelectorAll('.playable.on').forEach(function(e){e.classList.remove('on');});}
function plSyncBtn(){document.getElementById('pl-play').textContent=plEl().paused?'▶':'⏸';}
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&document.getElementById('pl').classList.contains('on'))plClose();
});
document.addEventListener('DOMContentLoaded',function(){
  var a=plEl();if(!a)return;
  plVolInit();                                   // restore last-used level
  a.addEventListener('timeupdate',function(){
    document.getElementById('pl-time').textContent=
      fmtTime(a.currentTime)+(a.duration?(' / '+fmtTime(a.duration)):'');});
  ['play','pause','ended'].forEach(function(e){a.addEventListener(e,plSyncBtn);});
});
// Single delegated listener: cheap, and works for rows rendered later.
document.addEventListener('click',function(ev){
  var el=ev.target.closest?ev.target.closest('.playable[data-audio]'):null;
  if(!el)return;
  ev.preventDefault();ev.stopPropagation();
  plOpen(el.getAttribute('data-audio'),el.getAttribute('data-label')||el.textContent.trim(),
         ev.clientX,ev.clientY);
});

function setTab(t){TAB=t;document.querySelectorAll('.tab').forEach(function(e){e.classList.toggle('on',e.dataset.tab===t);});
  document.getElementById('attention').style.display=t==='attention'?'':'none';
  document.getElementById('assembly').style.display=t==='assembly'?'':'none';
  document.getElementById('progress').style.display=t==='progress'?'':'none';
  document.getElementById('log').style.display=t==='log'?'':'none';
  document.getElementById('settings').style.display=t==='settings'?'':'none';
  document.getElementById('toolbar').style.display=t==='attention'?'':'none';
  updateBulkBar();
  if(t==='log')loadLog();
  if(t==='settings')loadSettings();
  if(t==='assembly')asmLoad();
  if(t==='progress')cvEnter();}

// ---- Assembly tab: % assembled per missing album + the songs still missing.
var ASM=[], ASMSEL=new Set();
function asmSel(ev,id,on){
  if(ev)ev.stopPropagation();          // don't toggle the <details> open/closed
  if(on)ASMSEL.add(id);else ASMSEL.delete(id);
  asmSelCount();
}
function asmSelCount(){
  document.getElementById('asm-seln').textContent=ASMSEL.size+' selected';
}
function asmAll(on){
  ASMSEL.clear();
  if(on)(ASM||[]).forEach(function(a){ASMSEL.add(String(a.id));});
  document.querySelectorAll('.asmsel').forEach(function(cb){cb.checked=on;});
  asmSelCount();
}
function asmAct(kind){
  var ids=Array.from(ASMSEL);
  if(!ids.length){toast('Tick one or more albums first');return;}
  var what={find:'Search for the missing songs of',
            add:'Assemble and import into the library',
            remove:'Dismiss (files untouched)'}[kind];
  var extra=kind==='add'
    ? '\n\nMatched songs are COPIED and retagged to the target album. A source '
      +'file is deleted afterwards only if no other assembly still needs it.'
    : '';
  if(!confirm(what+' '+ids.length+' album(s)?'+extra))return;
  toast('working…');
  fetch('/api/assembly/'+kind,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ids:ids})})
   .then(function(r){return r.json();}).then(function(j){
      var res=j.results||[];
      var bad=res.filter(function(r){return !r.ok;});
      toast('Done: '+(j.done||0)+' ok'+(bad.length?(', '+bad.length+' failed: '
            +h(bad[0].message||'')):''));
      ASMSEL.clear();asmLoad();
   }).catch(function(e){toast('failed: '+e);});
}
function asmLoad(){
  fetch('/api/assembly').then(function(r){return r.json();}).then(function(j){
    ASM=j.items||[];
    document.getElementById('n-asm').textContent=ASM.length;
    ASMSEL.forEach(function(id){if(!ASM.some(function(a){return String(a.id)===id;}))ASMSEL.delete(id);});
    asmSelCount();
    var don=ASM.filter(function(a){return (a.pct||0)>=100;}).length;
    document.getElementById('asm-sum').textContent=
      ASM.length+' album(s) partly available · '+don+' fully assembled'
      +(j.sources?(' · '+j.sources+' source song(s) shared across assemblies'):'');
    asmRender();
  }).catch(function(){document.getElementById('asm-list').innerHTML='<p class="muted">assembly unavailable</p>';});
}
function asmRender(){
  var q=(document.getElementById('asm-q').value||'').toLowerCase();
  var onlyC=document.getElementById('asm-complete').checked;
  var rows=ASM.filter(function(a){
    if(onlyC&&(a.pct||0)<100)return false;
    if(!q)return true;
    var hay=((a.artist||'')+' '+(a.album||'')).toLowerCase();
    if(hay.indexOf(q)>=0)return true;
    var inSong=(a.matched||[]).concat(a.missing||[]).some(function(m){
      return String(m.track||'').toLowerCase().indexOf(q)>=0;});
    return inSong;
  });
  if(!rows.length){document.getElementById('asm-list').innerHTML=
    '<p class="muted">No album can be assembled from the current needs-attention sources.</p>';return;}
  document.getElementById('asm-list').innerHTML=rows.map(function(a){
    var pct=a.pct||0;
    var col=pct>=100?'#3fb950':(pct>=50?'#d29922':'#8b949e');
    var bar='<div style="background:#30363d;border-radius:4px;height:10px;width:12rem;overflow:hidden">'
      +'<div style="width:'+Math.min(100,pct)+'%;height:100%;background:'+col+'"></div></div>';
    var miss=(a.missing||[]).map(function(m){return '<li>'+h(m.track)+'</li>';}).join('');
    var got=(a.matched||[]).map(function(m){
      var src=m.source||'';
      return '<li>'+h(m.track)+' <span class="muted">← </span>'
        +'<span class="playable" data-audio="'+h(src)+'" data-label="'+h(src.split('/').pop())
        +'" title="Click to play">'+h(src.split('/').pop())+'</span>'
        +' <span class="muted">('+(m.score||0)+' via '+h(m.via||'?')+')</span></li>';}).join('');
    return '<details class="cvsec"><summary>'
      +'<input type="checkbox" class="asmsel" data-id="'+h(String(a.id))+'" '
      +(ASMSEL.has(String(a.id))?'checked':'')
      +' onclick="asmSel(event,\''+h(String(a.id))+'\',this.checked)"> '
      +'<b>'+h(a.artist)+' / '+h(a.album)+'</b> '
      +'<span style="color:'+col+'">'+pct.toFixed(0)+'%</span> '
      +'<span class="muted">'+(a.n_matched||0)+'/'+(a.total||0)+' songs</span> '
      +bar+'</summary><div class="cvbody" style="display:flex;gap:2rem;flex-wrap:wrap">'
      +'<div><b>Assembled ('+(a.n_matched||0)+')</b><ul>'+(got||'<li class="muted">none</li>')+'</ul></div>'
      +'<div><b>Still missing ('+(a.n_missing||0)+')</b><ul>'+(miss||'<li class="muted">none — complete</li>')+'</ul></div>'
      +'</div></details>';
  }).join('');
}
function loadSettings(){fetch('/api/settings').then(function(r){return r.json();}).then(function(j){
  var s=j.settings||[];SETTINGS=s;
  document.getElementById('setform').innerHTML=s.map(function(o){
    var ctl=o.type==='bool'
      ? '<input type="checkbox" data-sid="'+h(o.id)+'" data-type="bool" '+(o.value?'checked':'')+'>'
      : '<input type="'+(o.type==='int'?'number':'text')+'" data-sid="'+h(o.id)+'" data-type="'+h(o.type)+'" value="'+h(o.value)+'" style="width:8rem">';
    return '<div class="procard" style="justify-content:flex-start;gap:1rem"><label style="min-width:15rem"><b>'+h(o.label)+'</b></label>'+ctl+'<span class="muted" style="flex:1">'+h(o.help)+'</span></div>';
  }).join('');});}
function saveSettings(restart){
  var changes={};
  document.querySelectorAll('#setform [data-sid]').forEach(function(el){
    changes[el.getAttribute('data-sid')] = el.getAttribute('data-type')==='bool'? el.checked : el.value;
  });
  document.getElementById('setmsg').textContent='saving…';
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)})
   .then(function(r){return r.json();}).then(function(j){
     document.getElementById('setmsg').textContent=j.message||'';
     if(j.ok&&restart){if(confirm('Saved. Restart the container now to apply?'))ctrl('restart');}
   }).catch(function(e){document.getElementById('setmsg').textContent='error: '+e;});}
function ctrl(kind){
  var m=kind==='restart'?'Restart the cue_pipeline container now?':'Stop the cue_pipeline container now? (you\'ll start it again from the Unraid Docker page)';
  if(!confirm(m))return;
  fetch('/api/'+kind,{method:'POST'}).then(function(r){return r.json();})
   .then(function(j){toast(j.message||(j.ok?'requested':'failed'));})
   .catch(function(){toast(kind+' requested (connection dropped, expected)');});
}
function loadLog(){
  var n=document.getElementById('loglines').value;
  var w=document.getElementById('logwhich').value;
  fetch('/api/log?lines='+n+'&which='+w).then(function(r){return r.text();}).then(function(t){
    var b=document.getElementById('logbox');b.textContent=t;b.scrollTop=b.scrollHeight;});}
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
  case'quality':return h((d.quality||'')+(d.n_audio?' · '+d.n_audio+'trk':'')+(d.total_bytes?' · '+human(d.total_bytes):''));
  case'existing':
    if(ex&&ex.in_library===true)
      return '<span class="ex">'+h(ex.quality||'')+' · '+(ex.n_audio||0)+'trk · '+human(ex.total_bytes)+'</span>';
    var L=(ex&&ex.lidarr)||{};
    if(L.present)
      return '<span class="ex">not on disk · <span class="badge b-ll">Lidarr'+(L.monitored?' ●':' ○')+' '+(L.have||0)+'/'+(L.total||0)+'</span></span>';
    return '<span class="ex">not in library / not in Lidarr</span>';
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
  case'detected':return '<span class="muted">'+h(fmtDate(x.created))+'</span>';
  case'reason':return '<span class="muted">'+h(x.reason||'')+'</span>';
  case'path':return '<span class="ttog" title="show folder tree" onclick="toggleTree(event,\''+x.id+'\')">&#9656;</span><span class="path" id="p-'+x.id+'">'+h(x.source_path||'')+'</span>';
  default:return '';}}
function fmtDate(ts){if(!ts)return'';try{var d=new Date(ts*1000);return d.toLocaleDateString()+' '+d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}catch(e){return'';}}
// Plain-text value of a cell, used for the right-click value filters.
function cellText(k,x){var d=x.details||{},ex=x.existing||{};switch(k){
  case'title':return ((x.artist||'')+' — '+(x.album||'')).replace(/^ — | — $/,'')||(x.source_path||'').split('/').pop();
  case'quality':return d.quality||'';
  case'existing':return (ex&&ex.in_library)?(ex.quality||''):((ex&&ex.lidarr&&ex.lidarr.present)?('lidarr '+(ex.lidarr.have||0)+'/'+(ex.lidarr.total||0)):'not in lidarr');
  case'verdict':if(!ex||ex.in_library!==true)return 'held is new';var a=score(d),b2=score(ex);return a>b2+0.01?'held better':(b2>a+0.01?'library better':'same');
  case'formats':return fmtStr(d);
  case'ch':return d.multichannel?chLabel(d.max_channels):(d.max_channels?'stereo':'');
  case'tracks':return ''+(d.n_audio||x.tracks||0);
  case'size':return human(d.total_bytes);
  case'outcome':return x.outcome||'held';
  case'detected':return ''+(x.created||0);
  case'reason':return x.reason||'';
  case'path':return x.source_path||'';
  default:return '';}}
function render(){
  buildOutChips();renderVFilters();
  var cols=COLS.filter(function(c){return c.on;});
  var rows=HELD.filter(matches);
  // Sort by the active column (numeric-aware for tracks/size/detected).
  var numeric={tracks:1,size:1,detected:1};
  rows.sort(function(a,b){
    var va=cellText(SORT.col,a),vb=cellText(SORT.col,b);
    if(SORT.col==='size'){va=(a.details||{}).total_bytes||0;vb=(b.details||{}).total_bytes||0;}
    else if(numeric[SORT.col]){va=parseFloat(va)||0;vb=parseFloat(vb)||0;}
    else{va=(''+va).toLowerCase();vb=(''+vb).toLowerCase();}
    return (va<vb?-1:va>vb?1:0)*SORT.dir;
  });
  document.getElementById('n-att').textContent=HELD.length;
  VISIBLE=rows.map(function(r){return r.id;});
  var allSel=rows.length>0&&rows.every(function(r){return SEL.has(r.id);});
  var caret=function(k){return SORT.col===k?(SORT.dir>0?' ▲':' ▼'):'';};
  var head='<tr><th class="sel"><input type="checkbox" id="selall" '+(allSel?'checked':'')+' onclick="toggleAll(this.checked)"></th>'
    +cols.map(function(c){return '<th onclick="sortBy(\''+c.k+'\')">'+h(c.name)+caret(c.k)+'</th>';}).join('')+'<th>Actions</th></tr>';
  var body=rows.map(function(x,i){
    var tds=cols.map(function(c){return '<td data-col="'+c.k+'" data-val="'+h(cellText(c.k,x))+'">'+cell(c,x)+'</td>';}).join('');
    var sel='<td class="sel"><input type="checkbox" class="rowsel" data-id="'+x.id+'" '+(SEL.has(x.id)?'checked':'')+' onclick="toggleSel(\''+x.id+'\',this.checked)"></td>';
    var acts='<div class="acts" data-id="'+x.id+'">'
      +'<button class="b-copy" onclick="copyPath(\''+x.id+'\')">Copy</button>'
      +'<button class="b-copy" onclick="toggleTree(event,\''+x.id+'\')">Tree ▸</button>'
      +'<button class="b-keep" title="Copy the held tracks into the library, keeping existing files (adds only what is missing)" onclick="act(\''+x.id+'\',\'keep\')">Add to library</button>'
      +'<button class="b-move" title="Copy the held tracks into the library, OVERWRITING existing files" onclick="act(\''+x.id+'\',\'move\')">Overwrite</button>'
      +'<button class="b-disc" title="Throw the held download away (delete the torrent + its folder); keep the library as-is" onclick="act(\''+x.id+'\',\'discard\')">Discard</button></div>';
    var op=OPEN.has(x.id);
    var det='<tr class="det" id="det-'+x.id+'" style="display:'+(op?'table-row':'none')+'"><td colspan="'+(cols.length+2)+'">'
      +'<div class="cmp"><div class="tcol"><h4>Held (download)</h4><div id="t-held-'+x.id+'" class="muted">…</div></div>'
      +'<div class="tcol"><h4>Library (existing)</h4><div id="t-lib-'+x.id+'" class="muted">…</div></div></div></td></tr>';
    var cls='mainrow'+(i%2?' zebra':'')+(SEL.has(x.id)?' sel-on':'');
    return '<tr id="row-'+x.id+'" class="'+cls+'">'+sel+tds+'<td>'+acts+'</td></tr>'+det;
  }).join('');
  document.getElementById('attention').innerHTML=rows.length
    ? '<table>'+head+body+'</table>'
    : (HELD.length?'<div class="empty">No items match the current filters.</div>':'<div class="empty">Nothing needs attention right now. 🎉</div>');
  // Re-load trees for rows that were expanded before this re-render.
  OPEN.forEach(function(id){if(VISIBLE.indexOf(id)>=0){loadTree(id,'held','t-held-'+id);loadTree(id,'library','t-lib-'+id);}});
  updateBulkBar();
}
function _rowHL(id,on){var r=document.getElementById('row-'+id);if(r)r.classList.toggle('sel-on',on);}
function toggleSel(id,on){if(on)SEL.add(id);else SEL.delete(id);_rowHL(id,on);updateBulkBar();
  var sa=document.getElementById('selall');if(sa)sa.checked=VISIBLE.length>0&&VISIBLE.every(function(v){return SEL.has(v);});}
function toggleAll(on){VISIBLE.forEach(function(v){if(on)SEL.add(v);else SEL.delete(v);_rowHL(v,on);});
  document.querySelectorAll('.rowsel').forEach(function(cb){cb.checked=on;});updateBulkBar();}
function clearSel(){VISIBLE.forEach(function(v){_rowHL(v,false);});SEL.clear();document.querySelectorAll('.rowsel').forEach(function(cb){cb.checked=false;});var sa=document.getElementById('selall');if(sa)sa.checked=false;updateBulkBar();}
function updateBulkBar(){var n=VISIBLE.filter(function(v){return SEL.has(v);}).length;
  document.getElementById('bulkn').textContent=n+' selected';}
function bulkAct(kind){
  var ids=VISIBLE.filter(function(v){return SEL.has(v);});
  if(!ids.length){toast('Select one or more rows first (checkboxes on the left)');return;}
  var verb=kind==='discard'?'DISCARD (delete torrent + folder, keep library)':kind==='move'?'OVERWRITE library files with the held tracks':'ADD the held tracks to the library (keeping existing)';
  if(!confirm('This will '+verb+' for '+ids.length+' selected item(s). Continue?'))return;
  toast('Working on '+ids.length+'…');
  var ok=0,fail=0;
  (function next(i){
    if(i>=ids.length){toast('Done: '+ok+' ok'+(fail?', '+fail+' failed':''));SEL.clear();refresh();return;}
    fetch('/api/held/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:ids[i]})})
     .then(function(r){return r.json();}).then(function(j){if(j.ok){ok++;HELD=HELD.filter(function(x){return x.id!==ids[i];});}else fail++;})
     .catch(function(){fail++;}).finally(function(){next(i+1);});
  })(0);
}
function treeHtml(node){
  if(!node||!node.name&&!node.children)return '<span class="muted">empty / not available</span>';
  function rec(n){
    if(!n)return '';
    if(n.type==='file'){
      var pth=n.path||'';
      var nm=pth?('<span class="playable" data-audio="'+h(pth)+'" data-label="'+h(n.name)+'" title="Click to play">'+h(n.name)+'</span>'):h(n.name);
      return '<div class="tf">📄 '+nm+' <span class="muted">('+human(n.size)+')</span></div>';
    }
    var kids=(n.children||[]).map(rec).join('')+(n.truncated?'<div class="muted">… (truncated)</div>':'');
    return '<details class="td"><summary>📁 '+h(n.name)+' <span class="muted">('+(n.children||[]).length+')</span></summary><div class="tind">'+kids+'</div></details>';
  }
  // Show the root's children directly (the column header already names the side).
  if(node.type==='dir')return (node.children||[]).map(rec).join('')+(node.truncated?'<div class="muted">… (truncated)</div>':'')||'<span class="muted">(empty)</span>';
  return rec(node);
}
function loadTree(id,which,elId){
  fetch('/api/tree?id='+encodeURIComponent(id)+'&which='+which)
   .then(function(r){return r.json();}).then(function(j){
     var el=document.getElementById(elId);if(!el)return;
     el.className='';el.innerHTML=(j.tree&&Object.keys(j.tree).length)?treeHtml(j.tree):'<span class="muted">not in library / no files</span>';})
   .catch(function(){var el=document.getElementById(elId);if(el)el.innerHTML='<span class="muted">error loading</span>';});
}
function sortBy(col){if(SORT.col===col)SORT.dir=-SORT.dir;else{SORT.col=col;SORT.dir=1;}render();}
function toggleTree(ev,id){if(ev){ev.stopPropagation();ev.preventDefault();}
  var row=document.getElementById('det-'+id);if(!row)return;
  var open=row.style.display==='none';
  row.style.display=open?'table-row':'none';
  if(open){OPEN.add(id);loadTree(id,'held','t-held-'+id);loadTree(id,'library','t-lib-'+id);}
  else OPEN.delete(id);
}
function copyText(p){
  // navigator.clipboard only works in a secure context (https/localhost); over
  // plain http://<lan-ip>:8830 it's undefined -> fall back to execCommand.
  if(navigator.clipboard&&navigator.clipboard.writeText&&window.isSecureContext){
    navigator.clipboard.writeText(p).then(function(){toast('Copied');},function(){legacyCopy(p);});
  }else legacyCopy(p);
}
function legacyCopy(p){
  try{var ta=document.createElement('textarea');ta.value=p;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.focus();ta.select();
    var ok=document.execCommand('copy');document.body.removeChild(ta);
    toast(ok?'Copied':'Press Ctrl+C to copy');}
  catch(e){toast('Press Ctrl+C to copy');}
}
function copyPath(id){var el=document.getElementById('p-'+id);var p=el?el.textContent:(HELD.find(function(x){return x.id===id;})||{}).source_path;copyText(p);}
function act(id,kind){
  var msg=kind==='discard'?'DISCARD: delete this album\'s folder and deselect it in the torrent (keeping the rest of a discography). If it\'s the only album left, the whole torrent + folder are removed. Library kept as-is. Continue?'
    :kind==='move'?'OVERWRITE: copy the held tracks into the library, replacing colliding files, rescan Lidarr, then delete the source torrent + folder. Continue?'
    :'ADD TO LIBRARY: copy the held tracks into the library (keeping existing files, adding the rest), rescan Lidarr, then delete the source torrent + folder. Continue?';
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
   .then(function(res){HELD=res[0].items||[];ACT=(res[1]||{}).items||[];document.getElementById('updated').textContent='updated '+new Date().toLocaleTimeString();render();
     if(TAB==='log'&&document.getElementById('logtail').checked)loadLog();});
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

/* ===================== Converter tab ===================== */
var CVOPT=null, CVSEL=new Set(), CVINIT=false, CVPOLL=null, CVROOT='', CVDONE=new Set();
function cvEnter(){
  if(!CVINIT){CVINIT=true;
    fetch('/api/convert/options').then(function(r){return r.json();}).then(function(j){
      CVOPT=j.codecs||{};CVROOT=(j.root||'').replace(/\/+$/,'');
      var cs=document.getElementById('cv-codec');
      cs.innerHTML=Object.keys(CVOPT).map(function(k){return '<option value="'+k+'">'+h(CVOPT[k].label)+'</option>';}).join('');
      var cc=document.getElementById('cv-conc');
      cc.innerHTML=(j.concurrency||[1,2,3,4,5,6,7,8,9,10]).map(function(n){return '<option '+(n===2?'selected':'')+'>'+n+'</option>';}).join('');
      cvCodecChanged();
    });
    cvLoadDir('', 'cv-tree');
  }
  cvPollOnce();
  if(CVPOLL)clearInterval(CVPOLL);
  CVPOLL=setInterval(function(){if(TAB==='progress')cvPollOnce();},3000);
}
function cvCodecChanged(){
  var k=document.getElementById('cv-codec').value,o=CVOPT[k];if(!o)return;
  document.getElementById('cv-mode').innerHTML=o.modes.map(function(m){return '<option>'+m+'</option>';}).join('');
  document.getElementById('cv-bitrate').innerHTML=o.bitrates.map(function(b){return '<option value="'+b+'">'+b+' kbps</option>';}).join('');
  document.getElementById('cv-quality').innerHTML=o.quality.map(function(q2){return '<option>'+h(q2)+'</option>';}).join('');
  document.getElementById('cv-sr').innerHTML=o.sample_rates.map(function(s){return '<option value="'+s+'">'+(s==='keep'?'keep source':h(''+s))+'</option>';}).join('');
  document.getElementById('cv-ch').innerHTML=o.channels.map(function(c){return '<option>'+h(c)+'</option>';}).join('');
  document.getElementById('cv-codechelp').textContent=o.help||'';
  cvModeChanged();
}
function cvModeChanged(){
  var k=document.getElementById('cv-codec').value,m=document.getElementById('cv-mode').value;
  var isVbr=(m==='VBR'&&k!=='opus');
  document.getElementById('cv-bitrate').disabled=isVbr;
  document.getElementById('cv-quality').disabled=(k==='mp3'||k==='aac')&&m==='CBR';
}
function cvLoadDir(rel,elId){
  fetch('/api/library/ls?path='+encodeURIComponent(rel)).then(function(r){return r.json();}).then(function(j){
    var el=document.getElementById(elId);if(!el)return;
    if(j.scanning){el.innerHTML='<div class="empty">First library scan running… this can take a few minutes. The tree loads from cache afterwards.</div>';
      setTimeout(function(){cvLoadDir(rel,elId);},8000);return;}
    var out='';
    (j.folders||[]).forEach(function(f){
      var id='cvk-'+btoa(unescape(encodeURIComponent(f.rel))).replace(/[^a-zA-Z0-9]/g,'');
      out+='<div class="cvrow" data-rel="'+h(f.rel)+'" data-dir="1">'
        +'<span class="cvcaret" onclick="cvToggleDir(this,\''+h(f.rel).replace(/'/g,"\\'")+'\',\''+id+'\')">▸</span>'
        +'<input type="checkbox" '+(cvIsCovered(f.rel)?'checked':'')+' onclick="cvSel(\''+h(f.rel).replace(/'/g,"\\'")+'\',this.checked,true)">'
        +'<span class="nm">📁 '+h(f.name)+'</span><span class="grow"></span>'
        +'<span class="meta">'+f.files+' files · '+h(f.size_h)+'</span></div>'
        +'<div class="cvkids" id="'+id+'" style="display:none"></div>';
    });
    (j.files||[]).forEach(function(f){
      var meta=[f.format,f.bitrate,chLabel(f.channels)||'',(f.sample_rate?(f.sample_rate/1000)+'k':'')+(f.bits?'/'+f.bits:''),h(f.size_h)].filter(function(x){return x;}).join(' · ');
      out+='<div class="cvrow" data-rel="'+h(f.rel)+'">'
        +'<span class="cvcaret"></span>'
        +'<input type="checkbox" '+(cvIsCovered(f.rel)?'checked':'')+' onclick="cvSel(\''+h(f.rel).replace(/'/g,"\\'")+'\',this.checked)">'
        +'<span class="nm playable" data-audio="'+h(CVROOT+'/'+f.rel)+'" data-label="'+h(f.name)+'" title="Click to play">'+h(f.name)+'</span><span class="grow"></span>'
        +'<span class="meta">'+meta+'</span></div>';
    });
    el.innerHTML=out||'<div class="empty">(no audio here)</div>';
  }).catch(function(e){var el=document.getElementById(elId);if(el)el.innerHTML='<div class="empty">error: '+h(''+e)+'</div>';});
}
function cvToggleDir(caret,rel,kidsId){
  var kids=document.getElementById(kidsId);if(!kids)return;
  var open=kids.style.display==='none';
  kids.style.display=open?'':'none';
  caret.textContent=open?'▾':'▸';
  if(open&&!kids.dataset.loaded){kids.dataset.loaded='1';kids.innerHTML='<span class="muted">loading…</span>';cvLoadDir(rel,kidsId);}
}
function cvSel(rel,on,isDir){
  if(on)CVSEL.add(rel);else CVSEL.delete(rel);
  // Ticking a FOLDER means "everything under it", so cascade to every
  // descendant that is currently rendered: tick their boxes and drop them from
  // the selection (the folder already covers them, so counting both would
  // double up). Individual FILES are unaffected -- tick them one by one as
  // before. The server expands a selected folder recursively anyway
  // (_expand_audio), so this keeps the UI honest about what will be converted.
  if(isDir){
    var pref=rel.replace(/\/+$/,'')+'/';
    document.querySelectorAll('.cvrow').forEach(function(row){
      var r=row.getAttribute('data-rel')||'';
      if(r!==rel&&r.indexOf(pref)===0){
        var cb=row.querySelector('input[type=checkbox]');
        if(cb)cb.checked=on;
        CVSEL.delete(r);
      }
    });
  }
  cvSelCount();
}
// Re-read ONE folder from disk and re-render just that node -- never the whole
// tree. Used after a conversion or a delete so the change shows at once.
function cvRefreshDir(rel){
  rel=(rel||'').replace(/^\/+|\/+$/g,'');
  fetch('/api/library/refresh?path='+encodeURIComponent(rel)).catch(function(){})
   .then(function(){
     if(!rel){cvLoadDir('','cv-tree');return;}
     var id='cvk-'+btoa(unescape(encodeURIComponent(rel))).replace(/[^a-zA-Z0-9]/g,'');
     var box=document.getElementById(id);
     if(box&&box.dataset.loaded){cvLoadDir(rel,id);}      // node is open -> redraw it
   });
}
function cvSelCount(){
  document.getElementById('cv-seln').textContent=CVSEL.size+' selected';
}
// Is this row covered by the selection -- either ticked itself, or sitting
// under a ticked ANCESTOR folder? Used when rendering (children are loaded
// lazily, long after their parent folder was ticked) so the tree always shows
// the truth: ticking a folder means everything inside it.
function cvIsCovered(rel){
  if(CVSEL.has(rel))return true;
  var covered=false;
  CVSEL.forEach(function(sel){
    if(!covered&&rel.indexOf(sel.replace(/\/+$/,'')+'/')===0)covered=true;
  });
  return covered;
}
function cvSettingsSummary(){
  var k=document.getElementById('cv-codec').value,o=CVOPT[k]||{};
  var m=document.getElementById('cv-mode').value;
  var parts=[o.label||k,m];
  if(!document.getElementById('cv-bitrate').disabled)parts.push(document.getElementById('cv-bitrate').value+' kbps');
  if(!document.getElementById('cv-quality').disabled)parts.push(document.getElementById('cv-quality').value);
  parts.push('sr '+document.getElementById('cv-sr').value);
  parts.push('ch '+document.getElementById('cv-ch').value);
  return parts.join(', ');
}
function cvConvertSel(){cvConvert(Array.from(CVSEL));}
function cvConvert(rels){
  if(!rels.length){toast('Nothing selected');return;}
  var ow=document.getElementById('cv-overwrite').checked;
  var where=ow
    ? 'OVERWRITE MODE: each ORIGINAL FILE WILL BE DELETED and replaced by the converted one.\nSidecar .xml files are repointed and Lidarr rescans the folder.\nThis cannot be undone.'
    : 'Converted files are written NEXT TO the originals.';
  if(!confirm('Convert '+rels.length+' item(s) (folders expand to their audio files) to:\n'+cvSettingsSummary()+'\n\n'+where+'\n\nContinue?'))return;
  if(ow&&!confirm('Really DELETE '+rels.length+' original file(s) after converting?'))return;
  var body={files:rels,codec:document.getElementById('cv-codec').value,
    opts:{mode:document.getElementById('cv-mode').value,
      bitrate:document.getElementById('cv-bitrate').value,
      quality:document.getElementById('cv-quality').value,
      sample_rate:document.getElementById('cv-sr').value,
      channels:document.getElementById('cv-ch').value,
      overwrite:ow},
    concurrency:parseInt(document.getElementById('cv-conc').value||'2',10)};
  fetch('/api/convert/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(function(r){return r.json();}).then(function(j){
     toast(j.ok?('Queued '+j.queued+' conversion(s)'):(j.message||'failed'));
     document.getElementById('cv-sec-prog').open=true;cvPollOnce();})
   .catch(function(e){toast('Error: '+e);});
}
function cvDeleteSel(){cvDelete(Array.from(CVSEL));}
function cvDelete(rels){
  if(!rels.length){toast('Nothing selected');return;}
  if(!confirm('DELETE '+rels.length+' selected item(s) from the LIBRARY?\n\n'+rels.slice(0,12).join('\n')+(rels.length>12?'\n… and '+(rels.length-12)+' more':'')+'\n\nThis cannot be undone.'))return;
  fetch('/api/library/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:rels})})
   .then(function(r){return r.json();}).then(function(j){
     toast(j.ok?('Deleted '+j.deleted+' item(s)'):(j.errors||[]).join('; ')||'failed');
     var dirs={};(rels||[]).forEach(function(r){
       var d=r.indexOf('/')>=0?r.replace(/\/[^/]*$/,''):'';dirs[d]=1;});
     CVSEL.clear();cvSelCount();
     Object.keys(dirs).forEach(function(d){cvRefreshDir(d);});})
   .catch(function(e){toast('Error: '+e);});
}
function cvInfo(rel){
  fetch('/api/library/info?path='+encodeURIComponent(rel)).then(function(r){return r.json();}).then(function(j){
    var i=j.info||{},d=i.detail||{},t=i.tags||{};
    var specs=[['File',i.name],['Format',d.format+(d.lossless?' (lossless)':'')],
      ['Bitrate',d.bitrate?Math.round(d.bitrate/1000)+' kbps':''],['Channels',chLabel(d.channels)||d.channels],
      ['Sample rate',d.sample_rate?d.sample_rate+' Hz':''],['Bit depth',d.bits||''],
      ['Length',d.length?Math.floor(d.length/60)+':'+('0'+Math.round(d.length%60)).slice(-2):''],['Size',human(d.size)]];
    var html='<div class="mbox"><h3 style="margin:.2rem 0 .6rem">'+h(i.name||rel)+'</h3><table>'
      +specs.filter(function(s){return s[1];}).map(function(s){return '<tr><td>'+h(s[0])+'</td><td>'+h(''+s[1])+'</td></tr>';}).join('')
      +'<tr><td colspan="2" style="border-top:1px solid var(--bd);padding-top:.4rem"><b>ID tags</b></td></tr>'
      +Object.keys(t).map(function(k){return '<tr><td>'+h(k)+'</td><td>'+h(t[k])+'</td></tr>';}).join('')
      +'</table><div style="text-align:right;margin-top:.6rem"><button class="b-copy" onclick="document.getElementById(\'cv-modal\').remove()">Close</button></div></div>';
    var m=document.createElement('div');m.id='cv-modal';m.innerHTML=html;
    m.onclick=function(e){if(e.target===m)m.remove();};
    document.body.appendChild(m);
  });
}
function cvPollOnce(){
  fetch('/api/progress').then(function(r){return r.json();}).then(function(j){
    var conv=j.conversions||{},act=j.activity||[],splitq=j.split_queue||[];
    // A job that just finished changed its folder on disk -- refresh ONLY that
    // folder so the new (or replaced) file appears at once. Each job is handled
    // once, tracked by id.
    (conv.done||[]).forEach(function(jb){
      if(!jb||!jb.id||CVDONE.has(jb.id))return;
      CVDONE.add(jb.id);
      var d=(jb.refreshed!==undefined&&jb.refreshed!==null)?jb.refreshed
            :String(jb.rel||'').replace(/\/[^/]*$/,'');
      if(d!==undefined&&d!==null)cvRefreshDir(d);
    });
    var running=(conv.active||[]);
    document.getElementById('n-prog').textContent=(running.length+act.length)||0;
    // Progress section: total bar + each running conversion + each activity.
    var out='';
    if(running.length){
      out+='<div class="pline"><span class="plab"><b>Conversion total ('+running.length+' file(s))</b></span><div class="pbar"><div class="pfill" style="width:'+(conv.total_pct||0)+'%"></div></div><span class="ppct">'+(conv.total_pct||0)+'%</span></div>';
      running.forEach(function(jb){
        out+='<div class="pline"><span class="plab">'+h(jb.name)+'</span><div class="pbar"><div class="pfill" style="width:'+(jb.pct||0)+'%"></div></div><span class="ppct">'+(jb.state==='queued'?'queued':(jb.pct||0)+'%')+'</span></div>';
      });
    }
    act.forEach(function(a){
      var pct=(typeof a.pct==='number')?a.pct:null;
      out+='<div class="pline"><span class="plab"><span class="badge b-out">'+h(a.stage)+'</span> '+h(a.name)+(a.detail?' <span class="muted">'+h(a.detail)+'</span>':'')+'</span>'
        +(pct!==null?'<div class="pbar"><div class="pfill" style="width:'+pct+'%"></div></div><span class="ppct">'+pct+'%</span>':'<span class="ppct">'+ago(a.started)+'</span>')+'</div>';
    });
    document.getElementById('cv-prog').innerHTML=out||'<span class="muted">nothing running</span>';
    // Conversions section: queue + recent results.
    document.getElementById('cv-nconv').textContent=running.length;
    var cl='';
    running.forEach(function(jb){cl+='<div class="pline"><span class="plab">'+h(jb.rel)+'</span><span class="muted">'+h(jb.state)+'</span></div>';});
    (conv.done||[]).slice().reverse().forEach(function(jb){cl+='<div class="pline"><span class="plab">'+h(jb.rel)+'</span><span class="'+(jb.state==='done'?'muted':'badge b-lossy')+'">'+h(jb.state)+' '+h(jb.msg||'')+'</span></div>';});
    document.getElementById('cv-convlist').innerHTML=cl||'<span class="muted">no conversions yet</span>';
    // Cue splits section.
    document.getElementById('cv-nsplit').textContent=splitq.length;
    document.getElementById('cv-splitlist').innerHTML=splitq.length
      ? splitq.map(function(p){return '<div class="pline"><span class="plab">'+h(p)+'</span><span class="muted">queued</span></div>';}).join('')
      : '<span class="muted">no cue files queued</span>';
  }).catch(function(){});
}
// Right-click on a converter row -> Show info / Convert / Delete.
document.getElementById('cv-tree').addEventListener('contextmenu',function(e){
  var row=e.target.closest('.cvrow');if(!row)return;e.preventDefault();
  var rel=row.getAttribute('data-rel'),isDir=row.getAttribute('data-dir')==='1';
  if(!CVSEL.has(rel)){CVSEL.add(rel);var cb=row.querySelector('input[type=checkbox]');if(cb)cb.checked=true;cvSel(rel,true);}
  var sel=Array.from(CVSEL);
  ctx.innerHTML='<div class="mh">'+h(rel.split('/').pop())+(sel.length>1?' <span class="muted">(+'+(sel.length-1)+' more selected)</span>':'')+'</div>'
   +(!isDir?'<div class="mi" data-a="info">Show info</div>':'')
   +'<div class="mi" data-a="convert">Convert selection…</div>'
   +'<div class="mi" data-a="delete">Delete selection…</div>';
  ctx.style.left=Math.min(e.clientX,window.innerWidth-190)+'px';
  ctx.style.top=Math.min(e.clientY,window.innerHeight-140)+'px';ctx.classList.add('on');
  ctx.querySelectorAll('.mi').forEach(function(mi){mi.onclick=function(){
    var a=mi.getAttribute('data-a');ctx.classList.remove('on');
    if(a==='info')cvInfo(rel);
    else if(a==='convert')cvConvert(sel);
    else if(a==='delete')cvDelete(sel);};});
});
</script>
</body></html>"""


def _expand_audio(lt, rels, cap: int = 1000):
    """Expand selected folders to their audio files (recursive), pass files
    through, keep everything inside the library root. Capped for sanity."""
    try:
        from converter import AUDIO_EXTS
    except Exception:  # noqa: BLE001
        return []
    out = []
    for rel in rels:
        p = lt._safe_rel(rel)
        if p is None:
            continue
        if p.is_dir():
            for dirpath, _d, fns in os.walk(p):
                for fn in sorted(fns):
                    if os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                        r = os.path.relpath(os.path.join(dirpath, fn),
                                            lt.root).replace("\\", "/")
                        out.append(r)
                        if len(out) >= cap:
                            return out
        else:
            out.append(rel.strip("/"))
        if len(out) >= cap:
            break
    return out


_PLAYER_MIME = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".aiff": "audio/aiff", ".aif": "audio/aiff",
    ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape", ".wv": "audio/x-wavpack",
}
# Roots the player may read from. Everything the WebUI can show lives under one
# of these two mounts, and nothing outside them is ever served.
_PLAYER_ROOTS = ("/music", "/downloads")


def _player_path(raw: str) -> Optional[Path]:
    """
    Resolve a requested audio path, refusing anything outside the allowed mounts.
    Guards against traversal ('..') because this endpoint streams raw bytes.
    """
    if not raw:
        return None
    try:
        p = Path(raw).resolve(strict=False)
    except OSError:
        return None
    for root in _PLAYER_ROOTS:
        try:
            rp = Path(root).resolve(strict=False)
        except OSError:
            continue
        if p == rp or rp in p.parents:
            return p if p.is_file() else None
    return None


def _player_info(p: Path) -> Dict[str, Any]:
    """ID tags + codec specs for the player's two dropdowns."""
    out: Dict[str, Any] = {"name": p.name, "path": str(p), "tags": {}, "specs": {}}
    try:
        out["specs"]["size"] = p.stat().st_size
    except OSError:
        pass
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(p))
        if mf is not None:
            info = getattr(mf, "info", None)
            if info is not None:
                for k, label in (("length", "duration_s"), ("bitrate", "bitrate"),
                                 ("sample_rate", "sample_rate"),
                                 ("channels", "channels"),
                                 ("bits_per_sample", "bits")):
                    v = getattr(info, k, None)
                    if v:
                        out["specs"][label] = (round(v, 1) if k == "length" else v)
                out["specs"]["codec"] = type(info).__module__.split(".")[-1]
            if mf.tags:
                for k, v in list(mf.tags.items())[:60]:
                    if isinstance(v, (list, tuple)):
                        v = ", ".join(str(x) for x in v)
                    s = str(v)
                    out["tags"][str(k)] = s[:300]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


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
                # FAST PATH: serve straight from the file-backed store. Every
                # heavy step that used to run here per page load -- pruning
                # vanished folders, box-set expansion, the library compare,
                # identity backfill, auto-dismissing greens -- now runs in the
                # background curator thread (Orchestrator.curate_held_pass),
                # which keeps the store file itself fresh. The page load is
                # just this in-memory list dump.
                self._json(200, {"items": store.list()})
            elif path == "/api/audio/info":
                qs = parse_qs(urlparse(self.path).query)
                p = _player_path((qs.get("path", [""]) or [""])[0])
                if p is None:
                    self._json(404, {"error": "not found or not allowed"})
                    return
                self._json(200, _player_info(p))

            elif path == "/api/audio/stream":
                qs = parse_qs(urlparse(self.path).query)
                p = _player_path((qs.get("path", [""]) or [""])[0])
                if p is None:
                    self._send(404, b"not found", "text/plain")
                    return
                ctype = _PLAYER_MIME.get(p.suffix.lower(), "application/octet-stream")
                try:
                    size = p.stat().st_size
                except OSError:
                    self._send(404, b"not found", "text/plain")
                    return
                # Honour Range: without it the browser can't seek, so fast-forward
                # and rewind would be dead on a long lossless track.
                rng = self.headers.get("Range") or ""
                start, end = 0, size - 1
                partial = False
                m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    else:                       # suffix range: last N bytes
                        start = max(0, size - int(m.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    partial = True
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(p, "rb") as fh:
                        fh.seek(start)
                        left = length
                        while left > 0:
                            chunk = fh.read(min(262144, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass                        # listener seeked away / closed
                except OSError as exc:
                    logger.debug("stream %s failed: %s", p, exc)

            elif path == "/api/assembly":
                # NOTE: must NOT be named `store` -- that name is the held-store
                # CLOSURE variable used by /api/held above; assigning it here
                # would make it a local for the whole method and break that
                # handler with UnboundLocalError.
                asm_store = getattr(actions, "assembly", None)
                if asm_store is None:
                    self._json(200, {"items": [], "sources": 0,
                                     "note": "assembly disabled"})
                    return
                # Served AS-IS from the plan store (same contract as /api/held):
                # the background planner keeps it current, the page never
                # recomputes.
                self._json(200, {"items": asm_store.list(),
                                 "sources": len(asm_store.needed_files())})

            elif path == "/api/activity":
                acts = actions.list_activity() if hasattr(actions, "list_activity") else []
                self._json(200, {"items": acts})
            elif path == "/api/convert/options":
                conv = getattr(actions, "converter", None)
                if conv is None:
                    self._json(503, {"ok": False, "message": "converter unavailable"})
                else:
                    self._json(200, conv.options())
            elif path == "/api/progress":
                conv = getattr(actions, "converter", None)
                acts = (actions.list_activity()
                        if hasattr(actions, "list_activity") else [])
                split_q: list = []
                wq = getattr(actions, "work_queue", None)
                if wq is not None:
                    try:
                        split_q = [str(p) for p in list(wq.queue)[:50]]
                    except Exception:  # noqa: BLE001
                        split_q = []
                self._json(200, {
                    "conversions": conv.status() if conv else {},
                    "activity": acts,
                    "split_queue": split_q,
                })
            elif path == "/api/library/ls":
                lt = getattr(actions, "library_tree", None)
                if lt is None:
                    self._json(503, {"ok": False, "message": "library tree unavailable"})
                    return
                if lt._scanned_ts == 0:
                    # First scan not done yet: kick it off and tell the UI.
                    if not lt._scanning:
                        threading.Thread(target=lt.scan, daemon=True,
                                         name="lib-scan").start()
                    self._json(200, {"scanning": True})
                    return
                qs = parse_qs(urlparse(self.path).query)
                rel = (qs.get("path", [""]) or [""])[0]
                out = lt.list_dir(rel)
                if out is None:
                    self._json(400, {"ok": False, "message": "bad path"})
                else:
                    self._json(200, out)
            elif path == "/api/library/refresh":
                lt = getattr(actions, "library_tree", None)
                qs = parse_qs(urlparse(self.path).query)
                rel = (qs.get("path", [""]) or [""])[0]
                if lt is None or not hasattr(lt, "refresh_dir"):
                    self._json(503, {"ok": False})
                    return
                try:
                    self._json(200, {"ok": True, "info": lt.refresh_dir(rel)})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "message": str(exc)[:200]})

            elif path == "/api/library/info":
                lt = getattr(actions, "library_tree", None)
                qs = parse_qs(urlparse(self.path).query)
                rel = (qs.get("path", [""]) or [""])[0]
                info = lt.file_info(rel) if lt is not None else None
                self._json(200 if info else 404, {"info": info or {}})
            elif path == "/api/tree":
                qs = parse_qs(urlparse(self.path).query)
                eid = (qs.get("id", [""]) or [""])[0]
                which = (qs.get("which", ["held"]) or ["held"])[0]
                entry = store.get(eid) if eid else None
                tree = {}
                if entry and hasattr(actions, "folder_tree"):
                    try:
                        tree = actions.folder_tree(entry, which)
                    except Exception:  # noqa: BLE001
                        tree = {}
                self._json(200, {"tree": tree})
            elif path == "/api/settings":
                s = actions.get_settings() if hasattr(actions, "get_settings") else []
                self._json(200, {"settings": s})
            elif path == "/api/log":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    n = int((qs.get("lines", ["400"]) or ["400"])[0])
                except ValueError:
                    n = 400
                which = (qs.get("which", ["pipeline"]) or ["pipeline"])[0]
                try:
                    text = (actions.read_log(n, which)
                            if hasattr(actions, "read_log") else "(unavailable)")
                except TypeError:
                    text = actions.read_log(n)
                self._send(200, text.encode("utf-8", "replace"), "text/plain; charset=utf-8")
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
            if path == "/api/settings":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    changes = json.loads(raw or b"{}") or {}
                except Exception:  # noqa: BLE001
                    changes = {}
                if not hasattr(actions, "save_settings"):
                    self._json(501, {"ok": False, "message": "not supported"})
                    return
                ok, msg = actions.save_settings(changes)
                self._json(200 if ok else 500, {"ok": ok, "message": msg})
                return
            if path in ("/api/assembly/remove", "/api/assembly/find",
                        "/api/assembly/add"):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                except Exception:  # noqa: BLE001
                    body = {}
                ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
                fn = {
                    "/api/assembly/remove": "assembly_remove",
                    "/api/assembly/find": "assembly_find_missing",
                    "/api/assembly/add": "assembly_add_to_library",
                }[path]
                act = getattr(actions, fn, None)
                if act is None:
                    self._json(503, {"ok": False, "message": "assembly disabled"})
                    return
                results = []
                for aid in ids:
                    try:
                        ok, msg = act(aid)
                    except Exception as exc:  # noqa: BLE001
                        ok, msg = False, str(exc)[:200]
                    results.append({"id": aid, "ok": bool(ok), "message": msg})
                good = sum(1 for r in results if r["ok"])
                self._json(200, {"ok": good > 0, "done": good,
                                 "results": results})
                return

            if path == "/api/convert/start":
                conv = getattr(actions, "converter", None)
                lt = getattr(actions, "library_tree", None)
                if conv is None or lt is None:
                    self._json(503, {"ok": False, "message": "converter unavailable"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                except Exception:  # noqa: BLE001
                    body = {}
                rels = _expand_audio(lt, list(body.get("files") or []))
                queued, errs = conv.start(
                    rels, str(body.get("codec") or "mp3"),
                    dict(body.get("opts") or {}),
                    int(body.get("concurrency") or 2))
                self._json(200, {"ok": queued > 0, "queued": queued,
                                 "errors": errs,
                                 "message": "; ".join(errs)[:300]})
                return
            if path == "/api/library/delete":
                lt = getattr(actions, "library_tree", None)
                if lt is None:
                    self._json(503, {"ok": False, "message": "library tree unavailable"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") or {}
                except Exception:  # noqa: BLE001
                    body = {}
                deleted, errs = lt.delete(list(body.get("paths") or []))
                self._json(200, {"ok": deleted > 0 and not errs,
                                 "deleted": deleted, "errors": errs})
                return
            if path in ("/api/restart", "/api/shutdown"):
                if not hasattr(actions, "container_action"):
                    self._json(501, {"ok": False, "message": "not supported"})
                    return
                act = "restart" if path.endswith("restart") else "stop"
                ok, msg = actions.container_action(act)
                self._json(200 if ok else 500, {"ok": ok, "message": msg})
                return
            if path not in ("/api/held/keep", "/api/held/move", "/api/held/discard"):
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
                elif path.endswith("/discard"):
                    ok, msg = actions.discard(entry)
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
