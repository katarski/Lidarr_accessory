"""
Song harvest -- salvage individual tracks that are already on disk.

WHY THIS EXISTS
---------------
Lidarr imports album-at-a-time. A compilation disc is not an album in anyone's
metadata, so a 10-CD box lands in needs-attention as ten entries reading "no
monitored album target" and stays there forever -- while the songs inside it are
tracks Lidarr is actively missing.

Measured on this library: `Billie Holiday - The Complete On Verve (1945-1959)
FLAC` sat 100% downloaded with 256 tracks parked, re-checked up to 20 times,
holding 49 songs wanted by ~12 DIFFERENT albums (Billie Holiday Sings, Lover
Man, On The Sentimental Side, The Love Songs, Body & Soul, ...). One disc
scatters across a dozen destinations, so no single album target exists and none
ever will. Meanwhile the assembly hunt was downloading MORE compilations
looking for those same songs.

So matching has to be per-SONG, not per-album:

  wanted index   every missing track of every monitored incomplete album
  source scan    real tags off disk (title/artist/duration), not filenames
  match          normalized title + DURATION + artist, with a variant guard
  import         Lidarr ManualImport with an explicit trackId per file

Filename matching (what assembly used) is not good enough here: a compilation
names files "05 - Solitude.flac" and a box set repeats the same song across
discs as different takes. Duration is what separates the 1941 master from a
1956 live version of the same title.

SAFETY
------
* Default is DRY RUN. Nothing is written until explicitly enabled.
* Import mode is COPY, never move -- the source is usually a SEEDING torrent
  and moving its files out breaks the seed.
* A file is only offered for a track whose duration agrees; unknown durations
  are reported separately rather than silently trusted.
* Alternate takes / live / remixes are refused unless the wanted track says so
  too, because a wrong track file is worse than a missing one (it has to be
  found and deleted by hand afterwards).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("song_harvest")

AUDIO_EXTS = {".flac", ".ape", ".wv", ".wav", ".mp3", ".m4a", ".ogg", ".opus",
              ".aac", ".alac"}

# Markers that make a recording a DIFFERENT performance of the same title.
# If a candidate file advertises one and the wanted track does not (or vice
# versa), they are not the same recording however well the titles match.
_VARIANT_RE = re.compile(
    r"(?i)\b(take\s*\d+|alt(?:ernate|\.)?(?:\s+take)?|live|instrumental|"
    r"remix|rehearsal|demo|karaoke|acappella|a\s*cappella|reprise|"
    r"radio\s+edit|edit|mono|stereo\s+version|interview)\b")


def norm_title(s: Any) -> str:
    """Aggressive title key: drop bracketed asides, punctuation and case.

    "I Don't Want To Cry Any More (take 2)" -> "idontwanttocryanymore"
    so the bracket content does NOT defeat the match -- the variant guard
    handles that separately and deliberately.
    """
    t = re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", " ", str(s or ""))
    t = t.lower().replace("&", " and ")
    t = re.sub(r"^\s*the\s+", "", t)
    return re.sub(r"[^a-z0-9]", "", t)


def artist_key(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower().replace("&", "and"))


def variant_markers(s: Any) -> frozenset:
    """Variant words present in the FULL string (brackets included)."""
    return frozenset(m.group(1).lower().strip()
                     for m in _VARIANT_RE.finditer(str(s or "")))


@dataclass(frozen=True)
class WantedTrack:
    """A track Lidarr is missing, and where it belongs."""
    title: str
    track_id: int
    album_id: int
    album_title: str
    artist_id: int
    artist_name: str
    release_id: Optional[int]
    duration_ms: int = 0
    track_number: int = 0


@dataclass
class SourceFile:
    """An audio file on disk, as its own tags describe it."""
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()


@dataclass
class Match:
    src: SourceFile
    want: WantedTrack
    delta_ms: int
    confidence: str            # "exact" | "duration-unknown"
    reason: str = ""


@dataclass
class HarvestReport:
    matches: List[Match] = field(default_factory=list)
    rejected: List[Tuple[SourceFile, str]] = field(default_factory=list)
    scanned: int = 0
    untagged: int = 0

    def by_album(self) -> Dict[Tuple[int, str], List[Match]]:
        out: Dict[Tuple[int, str], List[Match]] = {}
        for m in self.matches:
            out.setdefault((m.want.album_id, m.want.album_title), []).append(m)
        return out

    def summary(self) -> str:
        alb = self.by_album()
        return ("scanned %d file(s): %d match(es) across %d album(s), "
                "%d rejected, %d untagged"
                % (self.scanned, len(self.matches), len(alb),
                   len(self.rejected), self.untagged))


# ---------------------------------------------------------------- wanted index

def build_wanted_index(lidarr, artist_id: Optional[int] = None,
                       ) -> Dict[str, List[WantedTrack]]:
    """
    {normalized title: [WantedTrack, ...]} for every missing track of every
    MONITORED, INCOMPLETE album. Scoped to one artist when `artist_id` is given
    (much cheaper), else the whole library.

    A title can map to several wanted tracks -- the same song is missing from
    more than one album -- so the caller picks per-file; that is exactly the
    cross-album case that makes this feature necessary.
    """
    index: Dict[str, List[WantedTrack]] = {}
    artists = ([{"id": artist_id}] if artist_id
               else (lidarr.list_artists() or []))
    for a in artists:
        aid = int(a.get("id"))
        try:
            albums = lidarr.list_albums_for_artist(aid) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("harvest: albums for artist %s failed: %s", aid, exc)
            continue
        for alb in albums:
            if not alb.get("monitored"):
                continue
            st = alb.get("statistics") or {}
            have = int(st.get("trackFileCount") or 0)
            total = int(st.get("totalTrackCount") or 0)
            if total and have >= total:
                continue                      # already complete
            art = (alb.get("artist") or {})
            aname = art.get("artistName") or a.get("artistName") or ""
            rid = _primary_release_id(alb)
            try:
                tracks = lidarr.list_tracks_for_album(int(alb["id"])) or []
            except Exception as exc:  # noqa: BLE001
                logger.debug("harvest: tracks for album %s failed: %s",
                             alb.get("id"), exc)
                continue
            for t in tracks:
                if t.get("hasFile"):
                    continue
                if not t.get("monitored", True):
                    continue
                key = norm_title(t.get("title"))
                if not key:
                    continue
                index.setdefault(key, []).append(WantedTrack(
                    title=str(t.get("title") or ""),
                    track_id=int(t.get("id")),
                    album_id=int(alb["id"]),
                    album_title=str(alb.get("title") or ""),
                    artist_id=int(art.get("id") or aid),
                    artist_name=str(aname),
                    release_id=rid,
                    duration_ms=int(t.get("duration") or 0),
                    track_number=int(t.get("absoluteTrackNumber") or 0),
                ))
    return index


def _primary_release_id(album: Dict[str, Any]) -> Optional[int]:
    """The monitored release of an album, which is what an import must target."""
    for rel in (album.get("releases") or []):
        if rel.get("monitored"):
            return int(rel.get("id"))
    rels = album.get("releases") or []
    return int(rels[0]["id"]) if rels and rels[0].get("id") else None


# ---------------------------------------------------------------- source scan

def read_tags(path: str) -> SourceFile:
    """Tags + duration for one file. Never raises."""
    sf = SourceFile(path=path)
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path, easy=True)
        if mf is None:
            return sf
        def first(k: str) -> str:
            v = mf.get(k) or []
            return str(v[0]) if v else ""
        sf.title = first("title")
        sf.artist = first("artist") or first("albumartist")
        sf.album = first("album")
        info = getattr(mf, "info", None)
        if info is not None and getattr(info, "length", None):
            sf.duration_ms = int(float(info.length) * 1000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("harvest: tag read failed for %s: %s", path, exc)
    return sf


def scan_folder(root: str, limit: int = 0) -> List[SourceFile]:
    """Every audio file under `root`, with tags read. Sorted for stable runs."""
    found: List[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            if os.path.splitext(n)[1].lower() in AUDIO_EXTS:
                found.append(os.path.join(dirpath, n))
                if limit and len(found) >= limit:
                    break
        if limit and len(found) >= limit:
            break
    return [read_tags(p) for p in sorted(found)]


# ---------------------------------------------------------------- matching

def match_files(
    files: Iterable[SourceFile],
    index: Dict[str, List[WantedTrack]],
    tolerance_seconds: float = 10.0,
    require_artist: bool = True,
    allow_unknown_duration: bool = False,
) -> HarvestReport:
    """
    Pair on-disk files with wanted tracks.

    Every candidate must clear FOUR gates. Duration is the one that matters
    most in practice: a box set repeats a title across discs as different
    takes, and only length tells the 1941 master from a 1956 live cut.
    """
    rep = HarvestReport()
    tol_ms = int(max(0.0, tolerance_seconds) * 1000)
    for sf in files:
        rep.scanned += 1
        if not sf.title:
            rep.untagged += 1
            rep.rejected.append((sf, "no title tag"))
            continue
        key = norm_title(sf.title)
        cands = index.get(key) or []
        if not cands:
            rep.rejected.append((sf, "title not wanted by any album"))
            continue

        src_variants = variant_markers(sf.title)
        best: Optional[Match] = None
        why: List[str] = []
        for w in cands:
            # (1) artist must agree -- a compilation is credited to someone
            # else, but the TRACK's artist tag still names the performer.
            if require_artist and sf.artist:
                ak, wk = artist_key(sf.artist), artist_key(w.artist_name)
                if wk and ak and wk not in ak and ak not in wk:
                    why.append("artist %r != %r" % (sf.artist, w.artist_name))
                    continue
            # (2) same performance? A "(take 2)" is not the master.
            if variant_markers(w.title) != src_variants:
                why.append("variant mismatch (%s vs %s)"
                           % (sorted(src_variants) or "-",
                              sorted(variant_markers(w.title)) or "-"))
                continue
            # (3) duration
            if not w.duration_ms or not sf.duration_ms:
                if not allow_unknown_duration:
                    why.append("duration unknown (%s/%s)"
                               % (w.duration_ms, sf.duration_ms))
                    continue
                cand = Match(sf, w, 0, "duration-unknown",
                             "accepted without duration check")
            else:
                delta = abs(sf.duration_ms - w.duration_ms)
                if delta > tol_ms:
                    why.append("duration off by %.1fs (%s)"
                               % (delta / 1000.0, w.album_title[:24]))
                    continue
                cand = Match(sf, w, delta, "exact")
            # (4) prefer the closest duration when several albums want it
            if best is None or abs(cand.delta_ms) < abs(best.delta_ms):
                best = cand
        if best is None:
            rep.rejected.append((sf, "; ".join(why[:3]) or "no acceptable track"))
        else:
            rep.matches.append(best)

    # ONE file per wanted track. A box set legitimately carries the same song
    # several times (different sessions/takes), and on real data three separate
    # "He's Funny That Way" files all cleared the gates for one track slot
    # (+1.3s, +18.7s, +26.9s). Importing all three would stack duplicates onto
    # one track, so keep only the closest by duration and report the rest.
    best_per_track: Dict[int, Match] = {}
    for m in rep.matches:
        cur = best_per_track.get(m.want.track_id)
        if cur is None or abs(m.delta_ms) < abs(cur.delta_ms):
            best_per_track[m.want.track_id] = m
    if len(best_per_track) != len(rep.matches):
        for m in rep.matches:
            keep = best_per_track.get(m.want.track_id)
            if keep is not m:
                rep.rejected.append((
                    m.src,
                    "duplicate for %r -- kept %s (%+.1fs vs %+.1fs)"
                    % (m.want.title[:30],
                       os.path.basename(keep.src.path)[:28],
                       keep.delta_ms / 1000.0, m.delta_ms / 1000.0)))
        rep.matches = list(best_per_track.values())
    return rep


# ---------------------------------------------------------------- import plan

def quality_map(lidarr, folders: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """
    {file path: Lidarr quality dict} by asking Lidarr's own /manualimport about
    each source folder.

    REQUIRED, not optional. An import entry without `quality` makes Lidarr
    throw while building the destination filename and the file silently never
    lands:

        System.NullReferenceException
          at NzbDrone.Core.Organizer.FileNameBuilder.BuildTrackFileName(...)
          at TrackFileMovingService.CopyTrackFile(...)

    Lidarr logs only "Couldn't import track" at Warn, so this failure is easy
    to mistake for a permissions problem. Only Lidarr can name the quality it
    will accept, so it is fetched rather than constructed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for folder in sorted(set(folders)):
        try:
            cands = lidarr.manual_import_candidates(folder) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("harvest: quality lookup failed for %s: %s",
                           folder, exc)
            continue
        for c in cands:
            p, q = c.get("path"), c.get("quality")
            if p and q:
                out[str(p)] = q
    return out


def build_import_files(matches: Iterable[Match],
                       quality_by_path: Optional[Dict[str, Dict[str, Any]]] = None,
                       quality: Optional[Dict[str, Any]] = None,
                       ) -> Tuple[List[Dict[str, Any]], List[Tuple[Match, str]]]:
    """
    ManualImport entries for `LidarrClient.manual_import_apply_files`, plus the
    matches that had to be skipped.

    One entry per file with an explicit trackId, so Lidarr never guesses the
    mapping -- guessing is what produced "Unable to find matching artist and
    albums" and the album-level dead end in the first place.

    A match with no known quality is SKIPPED rather than sent, because sending
    it makes Lidarr fail on the filename build and drop the file on the floor.
    """
    out: List[Dict[str, Any]] = []
    skipped: List[Tuple[Match, str]] = []
    qbp = quality_by_path or {}
    for m in matches:
        q = qbp.get(m.src.path) or quality
        if not q:
            skipped.append((m, "no quality from Lidarr for this path"))
            continue
        entry: Dict[str, Any] = {
            "path": m.src.path,
            "artistId": m.want.artist_id,
            "albumId": m.want.album_id,
            "trackIds": [m.want.track_id],
            "quality": q,
            "disableReleaseSwitching": True,
            "additionalFile": False,
        }
        if m.want.release_id:
            entry["albumReleaseId"] = m.want.release_id
        out.append(entry)
    return out, skipped


def format_report(rep: HarvestReport, source: str = "",
                  max_rejected: int = 8) -> List[str]:
    """Human-readable dry-run lines."""
    lines: List[str] = []
    if source:
        lines.append("harvest %s" % source)
    lines.append("  " + rep.summary())
    for (aid, atitle), ms in sorted(rep.by_album().items(),
                                    key=lambda kv: -len(kv[1])):
        lines.append("  -> %s (album %s): %d track(s)"
                     % (atitle[:44], aid, len(ms)))
        for m in ms[:6]:
            lines.append("       %-34s %+.1fs  %s"
                         % (m.want.title[:34], m.delta_ms / 1000.0,
                            os.path.basename(m.src.path)[:36]))
    shown = 0
    for sf, why in rep.rejected:
        if why == "title not wanted by any album":
            continue                       # the overwhelming majority; noise
        if shown >= max_rejected:
            break
        lines.append("  skip %-34s %s"
                     % (os.path.basename(sf.path)[:34], why[:60]))
        shown += 1
    return lines


# ---------------------------------------------------------------- change gate

def folder_signature(root: str) -> str:
    """
    Cheap fingerprint of a folder's audio content: count, total size, newest
    mtime. Changes when files are added, removed, replaced or retagged.
    """
    n = 0
    total = 0
    newest = 0.0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            n += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return "%d:%d:%d" % (n, total, int(newest))


def wanted_signature(index: Dict[str, List[WantedTrack]]) -> str:
    """
    Fingerprint of what Lidarr currently WANTS.

    The gate must cover this, not just the folder. A verdict of "no monitored
    album target" is a statement about Lidarr's library, so if only the folder
    were fingerprinted, adding an artist or unmonitoring an album would never
    re-open a parked item -- it would stay stuck forever.
    """
    ids = sorted(w.track_id for ws in index.values() for w in ws)
    import hashlib
    h = hashlib.sha1(repr(ids).encode("utf-8")).hexdigest()[:12]
    return "%d:%s" % (len(ids), h)


class HarvestLedger:
    """
    Remembers the (folder, wanted) fingerprint each source was last judged
    against, so an unchanged source is not re-examined every pass.

    Needs-attention entries here had `seen_count` up to 20 -- the same verdict
    recomputed twenty times over one folder, tag-reading hundreds of files off
    a FUSE share each round for nothing.
    """

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._seen: Dict[str, str] = {}
        self._dirty = False
        if path and os.path.exists(path):
            try:
                import json
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self._seen = dict(data.get("checked") or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug("harvest ledger unreadable (%s): %s", path, exc)

    def unchanged(self, source: str, folder_sig: str, want_sig: str) -> bool:
        return self._seen.get(source) == "%s|%s" % (folder_sig, want_sig)

    def mark(self, source: str, folder_sig: str, want_sig: str) -> None:
        self._seen[source] = "%s|%s" % (folder_sig, want_sig)
        self._dirty = True

    def forget(self, source: str) -> None:
        if self._seen.pop(source, None) is not None:
            self._dirty = True

    def save(self) -> None:
        if not (self.path and self._dirty):
            return
        try:
            import json
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"checked": self._seen}, fh)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as exc:
            logger.debug("harvest ledger save failed: %s", exc)


# ---------------------------------------------------------------- the pass

def harvest_pass(
    lidarr,
    sources: Iterable[str],
    ledger: Optional[HarvestLedger] = None,
    tolerance_seconds: float = 10.0,
    dry_run: bool = True,
    max_files: int = 4000,
    skip_unchanged: bool = True,
    import_mode: str = "copy",
) -> Dict[str, Any]:
    """
    Walk each source folder, harvest whatever songs Lidarr is missing, and
    (unless dry_run) import them track-by-track.

    import_mode is COPY on purpose: these sources are usually SEEDING torrents,
    and moving files out from under qBittorrent breaks the seed.
    """
    index = build_wanted_index(lidarr)
    wsig = wanted_signature(index)
    stats = {"sources": 0, "skipped_unchanged": 0, "scanned": 0,
             "matched": 0, "imported": 0, "albums": 0, "no_quality": 0}
    albums: set = set()
    budget = max(0, int(max_files))
    for src_dir in sources:
        if not src_dir or not os.path.isdir(src_dir):
            continue
        stats["sources"] += 1
        fsig = folder_signature(src_dir)
        if (skip_unchanged and ledger is not None
                and ledger.unchanged(src_dir, fsig, wsig)):
            stats["skipped_unchanged"] += 1
            continue
        files = scan_folder(src_dir, limit=budget)
        budget = max(0, budget - len(files))
        rep = match_files(files, index, tolerance_seconds=tolerance_seconds)
        stats["scanned"] += rep.scanned
        stats["matched"] += len(rep.matches)
        for line in format_report(rep, source=src_dir):
            logger.info("%s", line)
        if rep.matches and not dry_run:
            qmap = quality_map(lidarr, {os.path.dirname(m.src.path)
                                        for m in rep.matches})
            entries, skipped = build_import_files(rep.matches,
                                                 quality_by_path=qmap)
            stats["no_quality"] += len(skipped)
            for m, why in skipped:
                logger.warning("harvest: skipped %s -- %s",
                               os.path.basename(m.src.path), why)
            if entries:
                cid = lidarr.manual_import_apply_files(
                    entries, import_mode=import_mode)
                if cid:
                    stats["imported"] += len(entries)
                    albums |= {e["albumId"] for e in entries}
                    logger.info("harvest: submitted %d track(s) to Lidarr "
                                "(command %s, mode=%s)",
                                len(entries), cid, import_mode)
                else:
                    logger.warning("harvest: Lidarr refused the import of "
                                   "%d track(s) from %s", len(entries), src_dir)
        if ledger is not None:
            # Only remember a source once it has been fully judged. An import
            # CHANGES the wanted set, so the next pass re-derives anyway --
            # that is correct, not waste.
            ledger.mark(src_dir, fsig, wsig)
        if budget <= 0:
            logger.info("harvest: hit the %d-file cap for this pass", max_files)
            break
    stats["albums"] = len(albums)
    if ledger is not None:
        ledger.save()
    logger.info(
        "harvest pass: %d source(s), %d skipped unchanged, %d file(s) scanned, "
        "%d match(es), %d imported into %d album(s)%s",
        stats["sources"], stats["skipped_unchanged"], stats["scanned"],
        stats["matched"], stats["imported"], stats["albums"],
        "  [DRY RUN -- nothing written]" if dry_run else "")
    return stats
