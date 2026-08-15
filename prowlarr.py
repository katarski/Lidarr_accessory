"""
Free-text indexer search via Prowlarr, for the find-the-compilation workflow.

Lidarr cannot do this. Its interactive-search endpoint (`GET /api/v1/release`)
only accepts an `albumId` or an `artistId` -- it builds the query from its own
metadata, so there is no way to ask it for an arbitrary phrase. But the whole
point of the compilation hunt is to search for a title Lidarr has never heard
of ("The Quintessential Billie Holiday, Volume 9"), which is precisely the query
Lidarr won't accept. So this talks to Prowlarr directly.

Prowlarr is used strictly as the free-text TRANSPORT, not as a wider net.
Prowlarr here has 13 indexers enabled while Lidarr has 3 with music categories,
and that gap is deliberate curation -- the others return noise (a search for the
compilation "The Lady" came back topped by a Lady GaGa release from a tracker
Lidarr does not use). So `indexer_ids_from_lidarr()` reads which Torznab
indexers Lidarr actually has switched on and pins every search to exactly that
set. Same indexers Lidarr would have queried; only the query shape is new.

Results are normalized to the same release dict the assembly ranker already
consumes (guid / indexerId / title / seeders / size / protocol), so the existing
rank -> grab -> harvest path takes them unchanged.

Grabbing is NOT done through Lidarr either: `POST /api/v1/release` refuses any
release it cannot attribute to a library artist (404 "Unable to find matching
artist and albums"), which is every cross-artist compilation. So each result
carries a bare `magnet` for `qbt.add_magnet()`. Note that Prowlarr's own
`magnetUrl` field is a PROXY REDIRECT through Prowlarr (with the api key in the
query string), not a magnet URI -- the bare magnet is in `guid` for the trackers
that publish one, and otherwise gets rebuilt from `infoHash`. Results with
neither are dropped: without a magnet there is nothing we could hand to
qBittorrent, and Lidarr won't take them.

This module only READS from Prowlarr. Nothing is added, enabled or configured.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Torznab/Newznab music category. 3000 is "Audio" and covers the 30xx children
# (3010 MP3, 3040 Lossless, ...), so one number is enough and indexers that
# only map some children still answer.
AUDIO_CATEGORIES: Tuple[int, ...] = (3000,)

_BTIH_RE = re.compile(r"(?i)\bxt=urn:btih:([0-9a-f]{40}|[2-7a-z]{32})\b")

_BRACKETS_RE = re.compile(r"\(.*?\)|\[.*?\]")
_DASHES_RE = re.compile(r"[‐-―]")
_PUNCT_RE = re.compile(r"[^\w\s&'-]", re.UNICODE)
# A trailing year or year-range: MusicBrainz titles carry them ("The Complete
# Masters 1933-1959") and torrent names almost never do.
_TRAIL_YEARS_RE = re.compile(
    r"[\s,-]*\b(1[89]\d\d|20\d\d)\s*(?:-\s*(?:1[89]\d\d|20\d\d|\d\d))?\s*$")
_SUBTITLE_RE = re.compile(r"\s*[:;]\s*|\s+-\s+")


def _clean_term(s: str) -> str:
    """Flatten a MusicBrainz title to what an indexer's search will tolerate:
    bracketed asides gone, en/em dashes folded to '-', other punctuation to
    spaces. "Lady Day: The Complete Billie Holiday on Columbia (1933-1944)"
    becomes "Lady Day The Complete Billie Holiday on Columbia"."""
    s = _BRACKETS_RE.sub(" ", str(s or ""))
    s = _DASHES_RE.sub("-", s)
    return " ".join(_PUNCT_RE.sub(" ", s).split())


def search_terms(title: str, artist: str = "") -> List[str]:
    """
    Query strings to try for one compilation title, most specific first.

    THE ARTIST NAME IS PREFIXED TO EVERY VARIANT (unless the title already
    contains it), and that is not cosmetic. Shortening a title to its
    distinctive head is what makes these searches land -- "The Quintessential
    Billie Holiday, Volume 9: 1940-1942" finds nothing but drop the year range
    and it hits exactly -- yet an unanchored head is actively dangerous: the
    compilation "The Lady: Complete Collection" shortens to "The Lady", which
    returned 72 results headed by a Lady GaGa album. With the artist prefixed,
    the same shortening returned 3 results, all Billie Holiday.

    Variants, in order: the full cleaned title, the title minus a trailing
    year/year-range, and the part before a subtitle separator. Duplicates and
    anything that would name the artist twice are dropped, so a self-titled or
    artist-named release yields one sane term rather than four.
    """
    artist = " ".join(str(artist or "").split())
    base = _clean_term(title)
    if not base:
        return []
    shapes = [base]
    trimmed = _TRAIL_YEARS_RE.sub("", base).strip()
    if trimmed and trimmed != base:
        shapes.append(trimmed)
    # Split the subtitle off BEFORE cleaning: _clean_term removes ':' and ';',
    # so splitting the cleaned string would never find a separator and the head
    # variant -- the one that actually lands on the indexers -- was never built.
    raw = _DASHES_RE.sub("-", _BRACKETS_RE.sub(" ", str(title or "")))
    head = _clean_term(_SUBTITLE_RE.split(raw, 1)[0])
    head = _TRAIL_YEARS_RE.sub("", head).strip()
    if head and head not in shapes:
        shapes.append(head)

    akey = artist.lower()
    out: List[str] = []
    seen = set()
    for shape in shapes:
        term = shape if (akey and akey in shape.lower()) else (
            ("%s %s" % (artist, shape)).strip() if artist else shape)
        term = " ".join(term.split())
        # A head that is only the artist name is the blind artist-level search
        # this whole workflow exists to replace -- never emit it.
        if not term or (akey and term.lower() == akey):
            continue
        # Two words is not evidence; it invites the Lady GaGa problem even with
        # a prefix, because indexers match loosely.
        if len(term.split()) < 2:
            continue
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        out.append(term)
    return out


def _bare_magnet(rec: Dict[str, Any]) -> Optional[str]:
    """
    A magnet URI for a Prowlarr result, or None if it publishes neither a
    magnet nor an infohash.

    `magnetUrl` is deliberately NOT used as-is: Prowlarr hands back a redirect
    through its own /download endpoint (api key included), which tells us
    nothing about the infohash -- and the infohash is how the grab is verified.
    """
    for key in ("guid", "magnetUrl", "downloadUrl"):
        cand = str(rec.get(key) or "")
        i = cand.find("magnet:")
        if i >= 0:
            return cand[i:]
    ih = str(rec.get("infoHash") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}|[2-7a-z]{32}", ih):
        # Rebuilt magnet: trackers are unknown, so this relies on DHT/PEX. Fine
        # for the public trackers that omit the magnet, useless for a private
        # one -- which is why the caller treats a failed add as "try the next".
        dn = urllib.parse.quote(str(rec.get("title") or "")[:120])
        return "magnet:?xt=urn:btih:%s&dn=%s" % (ih, dn)
    return None


# Words that carry no identity: grammar, and the format/edition furniture that
# every torrent name is padded with. Removed from both sides before comparing,
# so "The Essential Billie Holiday 3 cd boxset[flac]" and "Billie Holiday - The
# Essential" reduce to the same single token.
_NOISE_WORDS = frozenset("""
a an and the of on in at to for with by or from de le la les el
flac ape wav wavpack wv alac mp3 aac m4a ogg opus lossless dsd
cd cds disc discs disk cdrip rip vinyl lp ep box boxset set
remaster remastered reissue edition deluxe expanded anniversary
bit kbps khz vbr v0 v2 320 256 192 128 24 16 96 44 48 192khz
web webrip scene proper repack retail promo va various
""".split())

_YEAR_TOKEN_RE = re.compile(r"^(1[89]\d\d|20\d\d)$")


def _identity_tokens(title: str, artist: str = "") -> frozenset:
    """
    The tokens that actually identify a record: no grammar, no format/edition
    padding, no years, and NOT the artist name (every candidate has that, so it
    can only inflate a comparison). Single characters and bare numbers go too.
    """
    toks = set(_clean_term(title).lower().replace("-", " ").split())
    toks -= {t for t in _clean_term(artist).lower().replace("-", " ").split()}
    return frozenset(t for t in toks
                     if len(t) > 1 and t not in _NOISE_WORDS
                     and not _YEAR_TOKEN_RE.match(t) and not t.isdigit())


def title_plausible(release_title: str, comp_title: str,
                    artist: str = "") -> float:
    """
    How plausibly an indexer result IS the compilation we searched for, 0..1.

    Needed because the shortened search terms that make these searches land are
    also what makes them lie. "The Lady: Complete Collection" has to be queried
    as "Billie Holiday The Lady" to return anything, and that matches
    "Lady Sings The Blues" -- an album already complete at 12/12 in the library,
    and the exact release a previous song hunt wasted its grabs on.

    Scored BOTH WAYS on identity tokens (an F1), because one direction alone is
    fooled: "The Great Billie Holiday" reduces to the single token {great},
    which "The Great American Songbook" contains in full -- perfect recall,
    poor precision. Requiring both keeps it out.

    This is a pre-filter for grabs, not proof. The authoritative check stays
    what it always was: fetch the metadata paused and look for the missing song
    titles in the file list.
    """
    want = _identity_tokens(comp_title, artist)
    got = _identity_tokens(release_title, artist)
    if not want or not got:
        # Nothing distinctive on one side (a self-titled comp, or a release
        # named only after the artist) -- no evidence either way, so don't
        # pretend there is any.
        return 0.0
    overlap = len(want & got)
    if not overlap:
        return 0.0
    recall = overlap / float(len(want))
    precision = overlap / float(len(got))
    return 2.0 * recall * precision / (recall + precision)


class ProwlarrClient:
    """Read-only Prowlarr client: free-text search across enabled indexers."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120,
                 indexer_ids: Iterable[int] = ()) -> None:
        self.base = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = int(timeout)
        # Empty means "every enabled Prowlarr indexer". Callers should normally
        # pass indexer_ids_from_lidarr() instead, to keep the search inside the
        # set the user curated in Lidarr.
        self.indexer_ids = [int(i) for i in (indexer_ids or [])]
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.api_key,
                                     "Accept": "application/json"})

    # ---- discovery -------------------------------------------------------

    @staticmethod
    def base_url_from_lidarr(lidarr) -> Optional[str]:
        """
        Prowlarr's address as read off Lidarr's own indexer definitions.

        Prowlarr's app-sync writes each indexer into Lidarr as a Torznab entry
        whose baseUrl is `http://<prowlarr>:9696/<indexer-id>/`, so the host and
        port need not be configured twice. The api key CANNOT be recovered this
        way -- Lidarr masks it as '********' when reading indexers back -- so
        that still has to be supplied.

        Returns an origin like "http://192.168.1.200:9696", or None.
        """
        try:
            rows = lidarr._get("/api/v1/indexer")   # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.debug("prowlarr: Lidarr indexer lookup failed: %s", exc)
            return None
        for row in (rows or []):
            if str(row.get("implementation") or "").lower() != "torznab":
                continue
            for field in (row.get("fields") or []):
                if str(field.get("name") or "") != "baseUrl":
                    continue
                url = str(field.get("value") or "")
                if not url:
                    continue
                parts = urllib.parse.urlsplit(url)
                if parts.scheme and parts.netloc:
                    return "%s://%s" % (parts.scheme, parts.netloc)
        return None

    @staticmethod
    def indexer_ids_from_lidarr(lidarr) -> List[int]:
        """
        The Prowlarr indexer ids for the indexers LIDARR HAS ENABLED for music.

        This is what keeps the compilation hunt inside the user's curated set
        rather than blasting every tracker Prowlarr knows. Prowlarr's app-sync
        writes each indexer into Lidarr as Torznab with baseUrl
        `http://<prowlarr>/<indexer-id>/`, and Lidarr records both whether the
        indexer is enabled for search and which Torznab categories it carries --
        so the id, the enabled flag and the music test all come from one call.

        An indexer counts when interactive OR automatic search is on and it maps
        at least one 3xxx (Audio) category; EZTV is enabled here but is TV-only,
        so it is correctly left out. Returns [] on failure, which the search
        treats as "no filter" rather than "no indexers".
        """
        try:
            rows = lidarr._get("/api/v1/indexer")   # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.debug("prowlarr: Lidarr indexer lookup failed: %s", exc)
            return []
        out: List[int] = []
        for row in (rows or []):
            if str(row.get("implementation") or "").lower() != "torznab":
                continue
            if not (row.get("enableInteractiveSearch")
                    or row.get("enableAutomaticSearch")):
                continue
            fields = {str(f.get("name") or ""): f.get("value")
                      for f in (row.get("fields") or [])}
            cats = fields.get("categories") or []
            if not any(3000 <= int(c) < 4000 for c in cats
                       if isinstance(c, (int, float))):
                continue
            path = urllib.parse.urlsplit(
                str(fields.get("baseUrl") or "")).path.strip("/")
            head = path.split("/")[0] if path else ""
            if head.isdigit():
                out.append(int(head))
        return sorted(set(out))

    def ping(self) -> bool:
        try:
            r = self.session.get("%s/api/v1/system/status" % self.base,
                                 timeout=20)
            return r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            logger.debug("prowlarr ping failed: %s", exc)
            return False

    # ---- search ----------------------------------------------------------

    def search(
        self, term: str, categories: Iterable[int] = AUDIO_CATEGORIES,
        min_seeders: int = 1, limit: int = 0, require_magnet: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Free-text search, restricted to `self.indexer_ids` when set (i.e. to the
        indexers Lidarr has enabled) and across every enabled Prowlarr indexer
        otherwise.

        Returns release dicts shaped like Lidarr's interactive-search results --
        guid, indexerId, indexer, title, seeders, leechers, size, protocol --
        plus a bare `magnet` and `infoHash`, and `_source="prowlarr"` so the
        grab path knows not to route it through Lidarr. Torrents only (a usenet
        result has no magnet to add), highest seeders first.

        [] on any failure, and callers must read that as "couldn't search",
        never as "no such compilation exists".
        """
        term = " ".join(str(term or "").split())
        if not term or not self.base or not self.api_key:
            return []
        params = [("query", term), ("type", "search")]
        params += [("categories", int(c)) for c in categories]
        params += [("indexerIds", int(i)) for i in self.indexer_ids]
        try:
            r = self.session.get("%s/api/v1/search" % self.base,
                                 params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("prowlarr search %r failed: %s", term[:60], exc)
            return []
        if not isinstance(data, list):
            return []
        out: List[Dict[str, Any]] = []
        no_magnet = 0
        seen: set = set()
        for rec in data:
            if str(rec.get("protocol") or "torrent").lower() != "torrent":
                continue
            seeders = int(rec.get("seeders") or 0)
            if seeders < int(min_seeders):
                continue
            magnet = _bare_magnet(rec)
            # Some trackers publish NEITHER a magnet nor an infohash -- every
            # RuTracker music result is like this, carrying only Prowlarr's own
            # /download proxy link. Dropping those silently made a whole
            # indexer invisible: the exact `Tiesto - In My Memory ... FLAC
            # (tracks+.cue), lossless` at 11 seeders was discarded this way.
            # qBittorrent's add endpoint takes an http .torrent URL in the very
            # same `urls` field as a magnet, so with require_magnet=False the
            # caller can still grab it -- it just has to learn the infohash
            # after the add instead of parsing it from the URI.
            grab_url = magnet or (str(rec.get("downloadUrl") or "")
                                  if not require_magnet else "")
            if not grab_url:
                no_magnet += 1
                continue
            ih = (_BTIH_RE.search(magnet).group(1).lower()
                  if magnet and _BTIH_RE.search(magnet) else "")
            if ih and ih in seen:
                continue
            if ih:
                seen.add(ih)
            out.append({
                "guid": grab_url,        # the assembly path reads magnets here
                "grab_url": grab_url,
                "is_magnet": bool(magnet),
                "indexerId": rec.get("indexerId"),
                "indexer": rec.get("indexer"),
                "title": str(rec.get("title") or ""),
                "seeders": seeders,
                "leechers": int(rec.get("leechers") or 0),
                "size": int(rec.get("size") or 0),
                "protocol": "torrent",
                "magnet": magnet or "",
                "infoHash": ih,
                "_source": "prowlarr",
            })
        if no_magnet:
            logger.debug("prowlarr %r: skipped %d result(s) with no magnet or "
                         "infohash (private trackers)", term[:50], no_magnet)
        out.sort(key=lambda r: -int(r.get("seeders") or 0))
        if limit and limit > 0:
            out = out[:int(limit)]
        return out
