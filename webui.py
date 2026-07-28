"""
Minimal manual-attention WebUI (backlog #11).

A tiny stdlib http.server dashboard (no extra dependencies) that surfaces the
items the pipeline couldn't finish on its own (see held_store.HeldStore) and
lets the user resolve each one:

  * Copy path   -- copy the stuck folder's path to the clipboard.
  * Keep existing -- discard the held files; trust Lidarr's current library.
  * Move held   -- move the held files into the library and rescan Lidarr.

The two actions call back into the orchestrator through the `actions` object
(so the HTTP layer stays ignorant of Lidarr / the filesystem). Each callback
returns (ok: bool, message: str).

Runs on its own daemon thread; `stop_event` + server.shutdown() ends it.
"""

from __future__ import annotations

import html
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


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cue_pipeline &mdash; needs attention</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 1.5rem; background: #0f1115; color: #e6e6e6; }}
  @media (prefers-color-scheme: light) {{ body {{ background:#f5f6f8; color:#1a1a1a; }} }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  .sub {{ opacity: .65; font-size: .85rem; margin-bottom: 1.25rem; }}
  .empty {{ padding: 3rem; text-align: center; opacity: .6; border: 1px dashed #444;
    border-radius: 12px; }}
  .card {{ background: #1a1d24; border: 1px solid #2a2e37; border-radius: 12px;
    padding: 1rem 1.15rem; margin-bottom: 1rem; }}
  @media (prefers-color-scheme: light) {{ .card {{ background:#fff; border-color:#e2e4e8; }} }}
  .title {{ font-weight: 600; font-size: 1.05rem; }}
  .meta {{ font-size: .8rem; opacity: .7; margin: .15rem 0 .5rem; }}
  .reason {{ font-size: .85rem; margin: .35rem 0 .6rem; }}
  .badge {{ display:inline-block; font-size:.7rem; padding:.1rem .45rem; border-radius:999px;
    background:#3a2a12; color:#f0b45e; margin-left:.4rem; vertical-align:middle; }}
  .path {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .78rem;
    background:#0d0f13; padding:.45rem .6rem; border-radius:8px; overflow-x:auto;
    white-space:nowrap; border:1px solid #23262e; }}
  @media (prefers-color-scheme: light) {{ .path {{ background:#f0f1f4; border-color:#dcdee3; }} }}
  .row {{ display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.7rem; align-items:center; }}
  button {{ font: inherit; border:0; border-radius:8px; padding:.5rem .9rem; cursor:pointer; }}
  .b-copy {{ background:#2a2e37; color:#e6e6e6; }}
  .b-keep {{ background:#334155; color:#fff; }}
  .b-move {{ background:#7c3aed; color:#fff; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  .toast {{ position:fixed; bottom:1rem; left:50%; transform:translateX(-50%);
    background:#111; color:#fff; padding:.6rem 1rem; border-radius:10px; opacity:0;
    transition:opacity .2s; border:1px solid #333; max-width:90vw; }}
  .toast.show {{ opacity:1; }}
</style></head>
<body>
  <h1>cue_pipeline &mdash; needs manual attention</h1>
  <div class="sub">{count} item(s) the pipeline couldn't finish. Refreshed on load.</div>
  <div id="list">{cards}</div>
  <div id="toast" class="toast"></div>
<script>
function toast(m) {{ var t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(function(){{t.classList.remove('show');}}, 3000); }}
function copyPath(p) {{
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(p).then(function(){{toast('Path copied');}},
      function(){{toast('Copy failed — select it manually');}});
  }} else {{ toast('Clipboard unavailable — select it manually'); }}
}}
function act(id, kind, label) {{
  var msg = kind === 'move'
    ? 'Move the held files into the Lidarr library and rescan? This overwrites what Lidarr has for this album.'
    : 'Discard the held files and keep what Lidarr already has? The stuck folder will be deleted.';
  if (!confirm(msg)) return;
  document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){{b.disabled=true;}});
  fetch('/api/held/'+kind, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{id:id}})}})
    .then(function(r){{return r.json();}})
    .then(function(j){{ toast(j.message || (j.ok?'Done':'Failed'));
      if (j.ok) {{ var el=document.querySelector('[data-id="'+id+'"]'); if(el) el.remove(); }}
      else {{ document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){{b.disabled=false;}}); }}
    }})
    .catch(function(e){{ toast('Error: '+e);
      document.querySelectorAll('[data-id="'+id+'"] button').forEach(function(b){{b.disabled=false;}}); }});
}}
</script>
</body></html>"""

_CARD = """<div class="card" data-id="{id}">
  <div class="title">{title}<span class="badge">{outcome}</span></div>
  <div class="meta">{tracks} track(s) &middot; first seen {created} &middot; seen {seen}&times;</div>
  <div class="reason">{reason}</div>
  <div class="path" id="p-{id}">{path}</div>
  <div class="row">
    <button class="b-copy" onclick="copyPath(document.getElementById('p-{id}').textContent)">Copy path</button>
    <button class="b-keep" onclick="act('{id}','keep')">Keep existing</button>
    <button class="b-move" onclick="act('{id}','move')">Move held &rarr; library</button>
  </div>
</div>"""


def _render(items) -> str:
    if not items:
        cards = '<div class="empty">Nothing needs attention right now. 🎉</div>'
    else:
        cards = "".join(
            _CARD.format(
                id=html.escape(e["id"]),
                title=html.escape((f'{e.get("artist","")} — {e.get("album","")}').strip(" —")
                                  or e.get("source_path", "").split("/")[-1] or "(unknown)"),
                outcome=html.escape(e.get("outcome", "") or "held"),
                tracks=int(e.get("tracks", 0) or 0),
                created=_fmt_ts(e.get("created")),
                seen=int(e.get("seen_count", 1) or 1),
                reason=html.escape(e.get("reason", "") or ""),
                path=html.escape(e.get("source_path", "")),
            )
            for e in items
        )
    return _PAGE.format(count=len(items), cards=cards)


def _fmt_ts(ts) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # noqa: BLE001
        return "?"


def make_handler(store, actions: HeldActions):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cue_pipeline-webui"

        def log_message(self, fmt, *args):  # quieter than default stderr spam
            logger.debug("webui: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                store.prune_missing()  # hide items whose folder is already gone
                self._send(200, _render(store.list()).encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/api/held":
                store.prune_missing()
                self._json(200, {"items": store.list()})
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
        target=lambda: (stop_event.wait(), httpd.shutdown()),
        daemon=True,
    )
    watcher.start()
    try:
        httpd.serve_forever(poll_interval=1.0)
    finally:
        httpd.server_close()
        logger.info("WebUI: stopped")
