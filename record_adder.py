"""
Which record a library folder IS, when Lidarr does not list it -- proven by
its songs, so the record can be given to Lidarr and the folder owned.

The owner's metadata profile ("Standard": Album; Studio + Soundtrack) leaves
out live albums, compilations, EPs and singles, so a folder holding one stays
"album not in Lidarr" for good: 199 such folders on 1 Oct 2026. Lidarr can
hold any single record by its MusicBrainz id: added by hand it survives
refreshes, and the refresh's rescan links the files already in the artist
folder. Donny Hathaway / ' In Performance (1980)' (a live album) was added
unmonitored on 1 Oct and read 6/6 after one refresh.

Identity comes from the songs, never from a name alone. Candidates are found
the cheap way first -- Lidarr's album lookup by the folder's names, then
MusicBrainz by the songs (their recordings' release groups, all of them read
or, past 3, none), then a web search -- and a candidate is taken only when one of its releases carries at
least 90% of the folder's titled songs AND the folder holds at least half of
that release. The second test keeps a few shared songs from turning a folder
into a compilation that also has them: on 1 Oct a song vote alone put two of
Barbara Cook's 'No One Is Alone' into 'Oscar Winners', and one track of The
Matrix score into 'Warriors of Virtue'. A record Lidarr does not list must
also be nearly all in the folder (90%), or carry the folder's name: half let
the Beatles' 'Twist and Shout' and 'Abbey Road' be 'Perfect Collection,
Volume 1' and 'The Alternate Abbey Road'. Two different records that fit
equally well are no answer.
"""

from __future__ import annotations

import collections
import difflib
import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# What a web result title wraps around a record's name.
_WEB_NOISE_RE = re.compile(
    r"\b(?:album|lp|ep|full album|lyrics|discogs|spotify|apple music|youtube"
    r"(?: music)?|allmusic|deezer|tidal|bandcamp|wikipedia|vinyl|cd|mp3|flac"
    r"|songs?|tracklist|by)\b", re.I)


def covered(folder: Sequence[str], release: Sequence[str]) -> int:
    """How many of the folder's song keys are on the release. Each release
    title answers for one song: the exact key, else the closest at >= 0.88."""
    pool = list(release)
    hit = 0
    for t in folder:
        if t in pool:
            pool.remove(t)
            hit += 1
            continue
        best_i, best = -1, 0.0
        for i, r in enumerate(pool):
            s = difflib.SequenceMatcher(None, t, r).ratio()
            if s > best:
                best_i, best = i, s
        if best_i >= 0 and best >= 0.88:
            pool.pop(best_i)
            hit += 1
    return hit


def fits(folder: Sequence[str], release: Sequence[str]) -> int:
    """The songs the release explains (0 = it is not this record): >= 90% of
    the folder's titled songs on it, the folder >= half of it. Fewer than 3
    titled songs prove nothing unless they are the whole release."""
    if not folder or not release:
        return 0
    if len(folder) < 3 and len(folder) != len(release):
        return 0
    hit = covered(folder, release)
    if hit >= 0.9 * len(folder) and hit >= 0.5 * len(release):
        return hit
    return 0


def _web_names(titles: Iterable[str], artist: str) -> List[str]:
    """Record names out of web result titles ('X - Album by Y -- host')."""
    out: List[str] = []
    art = re.escape(artist.strip())
    for t in titles:
        t = str(t).split(" -- ")[0]
        if art:
            t = re.sub(art, " ", t, flags=re.I)
        t = _WEB_NOISE_RE.sub(" ", t)
        t = re.sub(r"[|:\-–—·]+", " ", t)
        t = " ".join(t.split())
        if len(t) >= 2 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def identify_record(
    lidarr: Any, artist: Dict[str, Any], names: Sequence[str],
    songs: Sequence[Tuple[str, float]], n_files: int,
    norm: Callable[[Any], str], mb: Any = None, web: Any = None,
    max_candidates: int = 8,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """(the record, how it was found) or (None, why not).

    `songs` are the folder's titled files as (title, seconds); `norm` the
    song-title key. The record is a Lidarr album lookup resource plus the
    fitting release: {"album", "release", "hit", "tracks", "via"}. Its "id"
    is set when Lidarr already lists it (under a name the folder does not
    carry). Needs MusicBrainz: tracklists come from there."""
    if mb is None:
        return None, "no MusicBrainz to read tracklists from"
    want = [k for k in (norm(t) for t, _ in songs) if k]
    if not want:
        return None, "its files carry no song titles"
    artist_id = str(artist.get("foreignArtistId") or "")
    artist_name = str(artist.get("artistName") or "")
    tried: set = set()
    fitted: List[Tuple[int, int, Dict[str, Any], Dict[str, Any], int, str]] = []
    unchecked = [0]
    own_names = {k for k in (norm(n) for n in names if n) if k}

    def whole_or_named(c: Dict[str, Any], hit: int, n_rel: int) -> bool:
        # A record Lidarr does not list is added whole, so the folder must be
        # nearly all of it, or carry its name ('Paint The Sky with Stars' is
        # 11 of 'Paint the Sky With Stars: The Best of Enya'). Lidarr's lookup
        # never offered the Beatles' own 'Magical Mystery Tour': its 11 songs
        # are also 11 of the 17 on 'The Beatles Collection, Volume 7'.
        if c.get("id") or hit >= 0.9 * n_rel:
            return True
        title = str(c.get("title") or "")
        return bool({norm(title), norm(title.split(":")[0])} & own_names)

    def consider(cands: Iterable[Dict[str, Any]], via: str,
                 limit: int = max_candidates) -> None:
        for c in cands:
            fid = str(c.get("foreignAlbumId") or "")
            if not fid or fid in tried or len(tried) >= limit:
                continue
            if str((c.get("artist") or {}).get("foreignArtistId") or "") != artist_id:
                continue                 # another artist's record (a VA soundtrack)
            tried.add(fid)
            rels = sorted((r for r in (c.get("releases") or [])
                           if r.get("foreignReleaseId")),
                          key=lambda r: abs(int(r.get("trackCount") or 0) - n_files))
            for r in rels[:3]:
                titles = mb.release_track_titles(str(r["foreignReleaseId"]))
                if titles is None:
                    unchecked[0] += 1
                    continue
                keys = [k for k in (norm(t) for t in titles) if k]
                hit = fits(want, keys)
                if hit and whole_or_named(c, hit, len(keys)):
                    fitted.append((hit, -abs(len(keys) - n_files), c, r,
                                   len(keys), via))
                    break

    def lookup(term: str) -> List[Dict[str, Any]]:
        try:
            return list(lidarr.lookup_albums(term) or [])[:5]
        except Exception as exc:  # noqa: BLE001
            logger.debug("album lookup %r failed: %s", term, exc)
            return []

    for name in dict.fromkeys(n for n in names if n and n.strip()):
        consider(lookup("%s %s" % (artist_name, name)), "its name")
    crowded = 0
    if not fitted:
        votes: collections.Counter = collections.Counter()
        asked = 0
        for title, secs in list(songs)[:5]:
            rels = mb.recording_releases_or_none(title, artist_id, duration=secs)
            if rels is None:
                unchecked[0] += 1
                continue
            asked += 1
            for rg in {x.get("rg") for x in rels if x.get("rg")}:
                votes[rg] += 1
        need = max(2, (asked + 1) // 2)
        voted = sorted((rg for rg, n in votes.items() if n >= need),
                       key=lambda rg: (-votes[rg], rg))
        if len(voted) > 3:
            # Songs on many records name none of them: Édith Piaf's voted for
            # 9 to 29 release groups, up to 12 tied, and which 3 were read
            # decided it -- 'je sais comment' was 'Milord' in one run and
            # 'L'Intégrale' in the next. Every one voted for is read, or none.
            crowded = len(voted)
        else:
            limit = len(tried) + len(voted)
            for rg in voted:
                consider(lookup("lidarr:%s" % rg), "its songs on MusicBrainz",
                         limit)
    if not fitted and web is not None:
        for name in [n for n in names if n and n.strip()][:1]:
            found = web.titles("%s %s album" % (artist_name, name)) or []
            for wn in _web_names(found, artist_name)[:3]:
                consider(lookup("%s %s" % (artist_name, wn)), "a web search")
    if not fitted:
        if unchecked[0]:
            return None, "MusicBrainz could not be asked -- not judged"
        if crowded:
            return None, ("its songs are on %d records of %s -- not sure which"
                          % (crowded, artist_name))
        return None, ("no record of %s carries its songs (%d looked at)"
                      % (artist_name, len(tried)))
    fitted.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = fitted[0]
    rival = next((f for f in fitted[1:] if f[2].get("foreignAlbumId")
                  != best[2].get("foreignAlbumId")), None)
    if rival is not None and (rival[0], rival[1]) == (best[0], best[1]):
        return None, ("two records fit as well: %r and %r"
                      % (best[2].get("title"), rival[2].get("title")))
    return ({"album": best[2], "release": best[3], "hit": best[0],
             "tracks": best[4], "via": best[5]},
            "found by %s" % best[5])
