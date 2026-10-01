"""
SearXNG web search, for evidence the catalogue cannot give.

Reached at http://SearXNG:8080 over the private `searxng_link` network that
tools/deploy.sh creates (see HANDOFF). SearXNG's engines are scrapers on a
residential line and are SHARED with Home Assistant's voice lookup: queried
hard they answer 429s and captchas and suspend themselves. So every query is
spaced (`min_interval`), capped per hour, and its answer kept on disk for a
month; a blocked or failed search returns None and is never stored -- "could
not search" is not "nothing found".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import List, Optional

logger = logging.getLogger(__name__)


class WebSearch:
    def __init__(self, base_url: str, cache_file: Optional[str] = None,
                 min_interval: float = 20.0, max_per_hour: int = 30,
                 timeout: float = 25.0, max_age_days: float = 30.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.cache_file = cache_file
        self.min_interval = float(min_interval)
        self.max_per_hour = int(max_per_hour)
        self.timeout = float(timeout)
        self.max_age = max_age_days * 86400
        self._lock = threading.Lock()
        self._last = 0.0
        self._recent: List[float] = []
        self._cache: dict = {}
        if cache_file:
            try:
                with open(cache_file, encoding="utf-8") as fh:
                    rows = json.load(fh)
                cutoff = time.time() - self.max_age
                self._cache = {q: v for q, v in rows.items()
                               if float(v.get("at") or 0) >= cutoff}
            except (OSError, ValueError, AttributeError):
                self._cache = {}

    def titles(self, query: str, limit: int = 8) -> Optional[List[str]]:
        """'Title -- site' of the top results for `query`, or None when the
        search could not be made (unreachable, blocked, over the cap)."""
        query = " ".join((query or "").split())
        if not query or not self.base_url:
            return None
        hit = self._cache.get(query)
        if hit is not None:
            return list(hit.get("titles") or [])[:limit]
        with self._lock:
            now = time.time()
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= self.max_per_hour:
                logger.info("web search: %d searches this hour -- %r waits",
                            len(self._recent), query[:60])
                return None
            wait = self._last + self.min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self._recent.append(self._last)
        url = "%s/search?%s" % (self.base_url, urllib.parse.urlencode(
            {"q": query, "format": "json"}))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cue_pipeline"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            logger.info("web search %r failed: %s", query[:60], exc)
            return None
        results = data.get("results") or []
        if not results:
            # Engines blocked or suspended is not "the web has nothing".
            logger.info("web search %r: no results (unresponsive: %s)",
                        query[:60], data.get("unresponsive_engines"))
            return None
        out = []
        for r in results:
            title = " ".join(str(r.get("title") or "").split())
            host = urllib.parse.urlparse(str(r.get("url") or "")).netloc
            if title:
                out.append("%s -- %s" % (title, host.replace("www.", "")))
        self._cache[query] = {"at": time.time(), "titles": out}
        self._save()
        return out[:limit]

    def _save(self) -> None:
        if not self.cache_file:
            return
        tmp = "%s.tmp" % self.cache_file
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False)
            os.replace(tmp, self.cache_file)
        except OSError as exc:
            logger.debug("web search cache not written: %s", exc)
