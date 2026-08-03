"""
Minimal MusicBrainz client for the "Lidarr may not list every album" check
(Torrenting requirement c).

Lidarr's own metadata comes from MusicBrainz via its proxy, but its library only
holds what it decided to add -- constrained by the artist's metadata profile
(which typically excludes compilations, live records, singles) and by whatever it
knew at the time the artist was added. So an album can genuinely exist for an
artist and simply not be listed in Lidarr. This asks MusicBrainz directly and
reports the difference.

Two things make this cheap and safe:

  * No API key and no artist SEARCH is needed -- Lidarr already stores the
    MusicBrainz artist id in `foreignArtistId`, so we query release-groups by
    mbid directly (exact, no fuzzy artist matching).
  * MusicBrainz asks for at most ONE request per second and a descriptive
    User-Agent; both are enforced here (a process-wide lock serialises callers,
    so several pipeline threads can't burst past the limit).

This module only READS. Deciding what to do with a reported gap is the caller's
business -- nothing is added to Lidarr automatically.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MB_BASE = "https://musicbrainz.org/ws/2"
# MusicBrainz requires a real, contactable User-Agent; a generic one gets 403.
_UA = ("cue_pipeline/1.0 (Lidarr_accessory; "
       "https://github.com/katarski/Lidarr_accessory)")

# Secondary release-group types that mean "not a plain studio album". Kept in
# sync with the grab-side filter so both sides agree on what counts as the
# artist's official work.
NON_STUDIO_SECONDARY = frozenset({
    "compilation", "live", "remix", "dj-mix", "mixtape/street", "demo",
    "interview", "spokenword", "audiobook", "audio drama", "soundtrack",
})


class MusicBrainzClient:
    """Read-only MusicBrainz WS/2 client, rate-limited to <=1 request/second."""

    _rate_lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, min_interval: float = 1.1, timeout: int = 30,
                 user_agent: str = _UA, retries: int = 1) -> None:
        self.min_interval = max(1.0, float(min_interval))
        self.timeout = int(timeout)
        self.user_agent = user_agent or _UA
        self.retries = max(0, int(retries))

    def _get(self, path: str, **params: Any) -> Optional[Dict[str, Any]]:
        params.setdefault("fmt", "json")
        url = f"{_MB_BASE}{path}?{urllib.parse.urlencode(params)}"
        # Serialise ALL callers so the 1 req/s courtesy limit holds even when
        # several pipeline threads ask at once. One retry covers the odd slow
        # response; a still-failing call returns None and the caller must treat
        # that as "couldn't check", never as "the artist has no albums".
        last: Optional[str] = None
        for attempt in range(self.retries + 1):
            with MusicBrainzClient._rate_lock:
                wait = self.min_interval - (
                    time.time() - MusicBrainzClient._last_call)
                if wait > 0:
                    time.sleep(wait)
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": self.user_agent,
                                      "Accept": "application/json"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return json.loads(resp.read().decode("utf-8", "replace"))
                except Exception as exc:  # noqa: BLE001
                    last = str(exc)
                finally:
                    MusicBrainzClient._last_call = time.time()
            if attempt < self.retries:
                time.sleep(2.0)
        logger.warning("musicbrainz GET %s failed: %s", path, last)
        return None

    def release_groups(
        self, artist_mbid: str, studio_only: bool = True,
        include_eps: bool = False, page_limit: int = 100,
        max_pages: int = 6,
    ) -> List[Dict[str, Any]]:
        """
        Every release-group (album-level record) MusicBrainz has for an artist.

        `studio_only` drops release-groups carrying a non-studio secondary type
        (compilation / live / remix / soundtrack / demo ...), matching the
        pipeline's "official releases only" rule. `include_eps` also keeps EPs.

        Returns [{title, type, secondary_types, first_release_date, mbid}],
        oldest first. Empty on any failure -- callers must treat empty as
        "couldn't check", never as "the artist has no albums".
        """
        if not artist_mbid:
            return []
        wanted_primary = {"album"} | ({"ep"} if include_eps else set())
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, int(max_pages))):
            data = self._get("/release-group", artist=artist_mbid,
                             limit=int(page_limit), offset=offset)
            if not data:
                break
            groups = data.get("release-groups") or []
            for g in groups:
                primary = str(g.get("primary-type") or "").strip().lower()
                secondary = [str(s or "").strip().lower()
                             for s in (g.get("secondary-types") or [])]
                if primary and primary not in wanted_primary:
                    continue
                if studio_only and any(
                        s in NON_STUDIO_SECONDARY for s in secondary):
                    continue
                title = str(g.get("title") or "").strip()
                if not title:
                    continue
                out.append({
                    "title": title,
                    "type": primary or "album",
                    "secondary_types": secondary,
                    "first_release_date": str(
                        g.get("first-release-date") or "")[:10],
                    "mbid": g.get("id"),
                })
            total = int(data.get("release-group-count") or 0)
            offset += len(groups)
            if not groups or offset >= total:
                break
        out.sort(key=lambda r: r.get("first_release_date") or "")
        return out
