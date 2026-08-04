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

import collections
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

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

# Release-group secondary types that make a release a plausible home for a
# stray song -- the INVERSE of the audit's filter above. A best-of, a box set
# and a soundtrack all carry other artists' recordings; a plain studio album
# does not, so it is no use to the compilation hunt.
COLLECTION_SECONDARY = frozenset({"compilation", "soundtrack", "live"})

# Lucene metacharacters that would change the meaning of a quoted phrase.
_LUCENE_ESCAPE_RE = re.compile(r'(["\\])')
_NORM_BRACKETS_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NORM_KEEP_RE = re.compile(r"[^a-z0-9]+")


def norm_release_title(s: Any) -> str:
    """
    Comparison key for a release title: lowercased, bracketed asides dropped,
    punctuation flattened. "Lady Day: The Best of Billie Holiday (Remastered)"
    and "lady day - the best of billie holiday" collapse to the same key, so
    the same compilation reached via twenty different recordings is counted
    once.
    """
    s = _NORM_BRACKETS_RE.sub(" ", str(s or "").lower())
    return " ".join(_NORM_KEEP_RE.sub(" ", s).split())


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

    # ---- find-the-compilation -------------------------------------------

    def recording_releases(
        self, title: str, artist_mbid: str, duration: float = 0.0,
        tolerance: float = 15.0, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Every release MusicBrainz knows that carries this artist's recording of
        this song title.

        Deliberately a SEARCH, not a browse by recording id. Lidarr stores a
        recording mbid per track (`foreignRecordingId`), which looks like the
        exact answer -- but MusicBrainz holds a separate recording entry per
        release for these old sides, so browsing `/release?recording=<mbid>`
        returns only the one album Lidarr already got the id from. Verified on
        `Gloomy Sunday`: the browse found 1 release, this search found 125.

        `duration` (seconds) is the discriminator that keeps a 1930s studio side
        apart from a later live take of the same standard; recordings whose
        length differs by more than `tolerance` are dropped. MusicBrainz does
        not know every length, and a recording with no length is KEPT -- these
        rarities are exactly where the metadata is thinnest.

        Returns [{title, norm, type, secondary_types, date, mbid}], one entry
        per (release, recording) pair -- callers dedupe on `norm`. Empty on any
        failure, which must be read as "couldn't check".
        """
        title = str(title or "").strip()
        if not title or not artist_mbid:
            return []
        query = 'recording:"%s" AND arid:%s' % (
            _LUCENE_ESCAPE_RE.sub(r"\\\1", title), artist_mbid)
        data = self._get("/recording", query=query, limit=int(limit))
        if not data:
            return []
        want = norm_release_title(title)
        out: List[Dict[str, Any]] = []
        for rec in (data.get("recordings") or []):
            # The search is fuzzy and scores partial matches -- "Sugar" pulls in
            # "Sugar Blues". Only an exact normalized title is this song.
            if norm_release_title(rec.get("title")) != want:
                continue
            length = rec.get("length")
            if length and duration and tolerance >= 0:
                if abs(float(length) / 1000.0 - float(duration)) > tolerance:
                    continue
            for rel in (rec.get("releases") or []):
                rg = rel.get("release-group") or {}
                rtitle = str(rel.get("title") or "").strip()
                key = norm_release_title(rtitle)
                if not key:
                    continue
                out.append({
                    "title": rtitle,
                    "norm": key,
                    "type": str(rg.get("primary-type") or "").lower(),
                    "secondary_types": [str(s or "").lower()
                                        for s in (rg.get("secondary-types") or [])],
                    "date": str(rel.get("date") or "")[:10],
                    "mbid": rel.get("id"),
                })
        return out

    def compilations_for_tracks(
        self, tracks: Iterable[Dict[str, Any]], artist_mbid: str,
        artist_name: str = "", tolerance: float = 15.0,
        collections_only: bool = True, exclude: Iterable[str] = (),
        max_tracks: int = 40, on_progress=None,
    ) -> List[Dict[str, Any]]:
        """
        Which releases carry the most of a set of wanted tracks -- the heart of
        the find-the-compilation workflow.

        `tracks` is [{title, duration}] (duration in seconds; 0 to skip the
        length check). Each is looked up separately and the releases are pooled,
        so a release is ranked by HOW MANY of the wanted songs it holds. That
        ordering is what makes the hunt worth running: for `Billie Holiday /
        The Love Songs` the top result covers 30 of the 34 missing sides, so one
        grab can finish most of the album instead of one song at a time.

        `exclude` drops releases by title -- pass the album being filled, or the
        hunt's best answer is the record we already know we can't get. Titles
        matching the ARTIST NAME are dropped too: a self-titled compilation
        ("Billie Holiday") makes a search term that degenerates into exactly the
        blind artist-level search this workflow replaces.

        `max_tracks` bounds the pass: MusicBrainz allows one request a second,
        so this is also the pass length in seconds. `on_progress(done, total,
        title)` is called per track if given.

        Returns [{title, norm, coverage, tracks, type, secondary_types, date,
        mbid}] sorted by coverage descending. Empty on total failure.
        """
        wanted = [t for t in tracks if str(t.get("title") or "").strip()]
        wanted = wanted[:max(1, int(max_tracks))]
        if not wanted or not artist_mbid:
            return []
        skip = {norm_release_title(x) for x in exclude}
        skip.discard("")
        artist_key = norm_release_title(artist_name)
        if artist_key:
            skip.add(artist_key)

        cover: Dict[str, set] = collections.defaultdict(set)
        meta: Dict[str, Dict[str, Any]] = {}
        for i, trk in enumerate(wanted, 1):
            title = str(trk.get("title") or "").strip()
            rels = self.recording_releases(
                title, artist_mbid, duration=float(trk.get("duration") or 0.0),
                tolerance=tolerance)
            for rel in rels:
                key = rel["norm"]
                if key in skip:
                    continue
                if collections_only and not (
                        set(rel["secondary_types"]) & COLLECTION_SECONDARY):
                    continue
                cover[key].add(title)
                prev = meta.get(key)
                # Keep the SHORTEST spelling of a title seen across releases --
                # the plainest form ("The Best of Billie Holiday" rather than
                # "The Best of Billie Holiday [Columbia Legacy Remaster]") is
                # the one an indexer search is most likely to match.
                if prev is None or len(rel["title"]) < len(prev["title"]):
                    meta[key] = rel
            if on_progress:
                try:
                    on_progress(i, len(wanted), title)
                except Exception:  # noqa: BLE001
                    pass

        out: List[Dict[str, Any]] = []
        for key, titles in cover.items():
            rel = dict(meta[key])
            rel["coverage"] = len(titles)
            rel["tracks"] = sorted(titles)
            out.append(rel)
        # Coverage first, then the older release -- an original-issue box set is
        # a likelier torrent than a 2019 streaming-era repackage of it.
        out.sort(key=lambda r: (-r["coverage"], r.get("date") or "9999"))
        return out
