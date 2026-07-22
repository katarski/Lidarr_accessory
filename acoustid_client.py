"""
AcoustID acoustic-fingerprint identification.

Identifies audio by its SOUND -- Chromaprint fingerprint -> AcoustID -> the
MusicBrainz recording -- so a download with garbage tags/filenames can still be
recognized (artist / album / track title) and imported correctly. This is the
"identify music I don't have yet, at move time" tool (NOT dedup).

Requirements:
  * fpcalc (Chromaprint) on PATH.
      Docker: apt-get install -y libchromaprint-tools
  * a FREE AcoustID application API key: https://acoustid.org/new-application

Design goals ("won't hit a wall"):
  * Throttled to AcoustID's ~3 req/s guideline (global, thread-safe).
  * Exponential backoff + retry on HTTP 429 / 5xx, then give up gracefully.
  * Per-(file,size,mtime) result cache so re-runs never re-query.
  * NEVER raises into the caller -- every failure path returns None and logs.
  * If fpcalc is missing it disables itself once (logged), rather than erroring
    on every call.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("acoustid")

ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"


class _RateLimiter:
    """Global minimum-interval throttle, shared across threads."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            gap = now - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class AcoustIDClient:
    def __init__(
        self,
        api_key: str,
        fpcalc: str = "fpcalc",
        enabled: bool = True,
        max_rps: float = 3.0,
        timeout: int = 20,
        cache: Optional[dict] = None,
    ):
        self.api_key = (api_key or "").strip()
        self.fpcalc = fpcalc or "fpcalc"
        self.enabled = bool(enabled and self.api_key)
        self.timeout = timeout
        self._rl = _RateLimiter(1.0 / max_rps if max_rps and max_rps > 0 else 0.0)
        self._cache: dict = cache if cache is not None else {}
        self._fpcalc_ok: Optional[bool] = None
        self._session = requests.Session()

    # ---- fpcalc -------------------------------------------------------

    def _have_fpcalc(self) -> bool:
        if self._fpcalc_ok is None:
            try:
                subprocess.run([self.fpcalc, "-version"],
                               capture_output=True, timeout=10)
                self._fpcalc_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fpcalc not available (%s) -- AcoustID identification "
                    "disabled. Install libchromaprint-tools.", exc,
                )
                self._fpcalc_ok = False
        return self._fpcalc_ok

    def _fingerprint(self, path: str) -> Optional[tuple]:
        """Return (duration:int, fingerprint:str) for a file, or None."""
        try:
            r = subprocess.run(
                [self.fpcalc, "-json", "-length", "120", path],
                capture_output=True, timeout=60, text=True,
            )
            if r.returncode != 0 or not r.stdout:
                logger.debug("fpcalc rc=%s for %s", r.returncode, path)
                return None
            d = json.loads(r.stdout)
            return int(round(float(d["duration"]))), d["fingerprint"]
        except Exception as exc:  # noqa: BLE001
            logger.debug("fpcalc failed for %s: %s", path, exc)
            return None

    # ---- AcoustID lookup ----------------------------------------------

    def _lookup(self, duration: int, fingerprint: str) -> Optional[dict]:
        params = {
            "client": self.api_key,
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups",  # requests encodes spaces -> +
        }
        backoff = 1.0
        for attempt in range(4):
            self._rl.wait()
            try:
                resp = self._session.post(ACOUSTID_URL, data=params,
                                          timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning("AcoustID HTTP %s -- backing off %.1fs",
                                   resp.status_code, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") != "ok":
                    logger.warning("AcoustID status=%s error=%s",
                                   data.get("status"),
                                   (data.get("error") or {}))
                    return None
                return data
            except Exception as exc:  # noqa: BLE001
                logger.warning("AcoustID lookup failed (attempt %d/4): %s",
                               attempt + 1, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        return None

    @staticmethod
    def _best(data: dict) -> Optional[Dict[str, Any]]:
        results = data.get("results") or []
        if not results:
            return None
        top = max(results, key=lambda r: r.get("score", 0) or 0)
        recs = top.get("recordings") or []
        if not recs:
            return {"artist": "", "title": "", "album": "",
                    "recording_id": top.get("id"), "score": top.get("score", 0)}
        rec = recs[0]
        artist = ""
        for a in (rec.get("artists") or []):
            artist += (a.get("name") or "") + (a.get("joinphrase") or "")
        rgs = rec.get("releasegroups") or []
        return {
            "artist": artist.strip(),
            "title": rec.get("title", ""),
            "album": (rgs[0].get("title", "") if rgs else ""),
            "recording_id": rec.get("id"),
            "score": top.get("score", 0),
        }

    def identify_file(self, path: str) -> Optional[Dict[str, Any]]:
        """Identify one audio file. Returns {artist,title,album,recording_id,
        score} or None. Cached by (path,size,mtime)."""
        if not self.enabled or not self._have_fpcalc():
            return None
        key = None
        try:
            st = os.stat(path)
            key = (path, st.st_size, int(st.st_mtime))
        except OSError:
            pass
        if key is not None and key in self._cache:
            return self._cache[key]
        result = None
        fp = self._fingerprint(path)
        if fp:
            data = self._lookup(fp[0], fp[1])
            if data:
                result = self._best(data)
        if key is not None:
            self._cache[key] = result
        return result

    def identify_folder(self, files: List[str]) -> Dict[str, Any]:
        """
        Identify a list of audio files and aggregate to an album-level guess:
        the most common artist/album across the tracks (robust to a few
        per-track misses). Returns {artist, album, identified, total, per_file}.
        """
        per: List[Dict[str, Any]] = []
        for f in files:
            r = self.identify_file(f)
            per.append({"file": f, **(r or {})})
        artists = Counter(p["artist"] for p in per if p.get("artist"))
        albums = Counter(p["album"] for p in per if p.get("album"))
        return {
            "artist": artists.most_common(1)[0][0] if artists else "",
            "album": albums.most_common(1)[0][0] if albums else "",
            "identified": sum(1 for p in per if p.get("recording_id")),
            "total": len(files),
            "per_file": per,
        }


# ---- standalone test CLI ----------------------------------------------
# python acoustid_client.py --key <APIKEY> --file song.flac
# python acoustid_client.py --key <APIKEY> --folder "/path/to/album"
def main() -> int:
    import argparse
    import sys

    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="AcoustID application API key")
    ap.add_argument("--file", help="single audio file to identify")
    ap.add_argument("--folder", help="folder of audio files to identify")
    ap.add_argument("--fpcalc", default="fpcalc")
    args = ap.parse_args()

    client = AcoustIDClient(api_key=args.key, fpcalc=args.fpcalc, enabled=True)
    if not client._have_fpcalc():
        print("fpcalc not found on PATH. Install Chromaprint (libchromaprint-tools).")
        return 1

    if args.file:
        print(json.dumps(client.identify_file(args.file), indent=2, ensure_ascii=False))
    elif args.folder:
        exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".ape",
                ".wv", ".alac", ".aiff", ".aif"}
        files = []
        for dp, _dn, fn in os.walk(args.folder):
            for x in fn:
                if os.path.splitext(x)[1].lower() in exts:
                    files.append(os.path.join(dp, x))
        files.sort()
        res = client.identify_folder(files)
        print(f"\nAlbum guess: artist={res['artist']!r} album={res['album']!r} "
              f"({res['identified']}/{res['total']} tracks identified)\n")
        for p in res["per_file"]:
            print(f"  {os.path.basename(p['file'])[:45]:45} -> "
                  f"{p.get('artist','')!r} / {p.get('title','')!r} "
                  f"(score {p.get('score',0):.2f})")
    else:
        print("Pass --file or --folder")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
