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

import dataclasses
import logging
import os
import re
import time
import unicodedata

import claims
import titlematch
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
    t = titlematch.fold(t)
    t = re.sub(r"^\s*the\s+", "", t)
    # Expand the abbreviations a tagger and a metadata source disagree on.
    # Punctuation is stripped below, so "Pt. 1" and "Part 1" collapse to
    # DIFFERENT keys ("pt1" vs "part1") and a track that is plainly the same
    # song never matches: `The Sweet Inspirations / Sweet Sweet Soul` sat at
    # 8/10 with both halves of "(Gotta Find) A Brand New Lover" already on
    # disk, invisible to the assembly because the files say "Pt." and Lidarr
    # says "Part". Token-bounded, so a word merely starting with these letters
    # is untouched.
    t = re.sub(r"\bpts\b\.?", "parts", t)
    t = re.sub(r"\bpt\b\.?", "part", t)
    t = re.sub(r"\bvols\b\.?", "volumes", t)
    t = re.sub(r"\bvol\b\.?", "volume", t)
    # Letters of any script: [^a-z0-9] turned every CJK/Greek/Arabic title
    # into "" and so into the same key.
    return "".join(titlematch._runs(t))


def artist_key(s: Any) -> str:
    """
    Comparison key for an artist name.

    Diacritics are FOLDED, not dropped. Stripping [^a-z0-9] deleted them
    outright, so Lidarr's "Tiësto" keyed to "tisto" while the file's "Tiesto"
    keyed to "tiesto" -- the same artist, never equal, and every track of
    `In My Memory` was skipped with "artist 'DJ Tiesto' != 'Tiësto'". Folding
    first also lets the caller's substring test do its job, since "tiesto" IS
    contained in "djtiesto".
    """
    return titlematch.key(str(s or ""))


def artist_tokens(s: Any) -> frozenset:
    """
    Artist name as a set of WORDS, diacritics folded.

    artist_key() strips spaces, so a substring test on it treats any name that
    merely STARTS with the wanted one as a match: 'Frida' passed as
    'Frida Leider' (the Wagner soprano) and 'Frida Boccara', because 'frida' is
    a prefix of 'fridaleider'. Words cannot do that.
    """
    return frozenset(titlematch.words(str(s or "")))


def _credit_names(s: Any) -> List[frozenset]:
    """
    An artist string split into the individual acts it credits, each as a word
    set with the decoration removed.

    Parentheticals are aliases of the same act ("Frida (Anni-Frid Lyngstad)"),
    anything after feat/with/presents is a guest, "&" and "/" join separate
    acts, and a leading DJ/MC is a stage prefix.
    """
    raw = str(s or "")
    raw = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", raw)            # alias in parens
    for _sep in (" feat", " featuring", " ft", " with", " presents", " pres",
                 " vs"):
        _i = raw.lower().find(_sep)
        if _i > 0 and (len(raw) == _i + len(_sep)
                       or not raw[_i + len(_sep)].isalpha()):
            raw = raw[:_i]                                      # drop guests
            break
    out: List[frozenset] = []
    for part in re.split(r"[&/,;]| and ", raw):
        toks = artist_tokens(part)
        toks = frozenset(w for w in toks if w not in _CREDIT_NOISE)
        if toks:
            out.append(toks)
    return out


def artists_agree(a: Any, b: Any) -> bool:
    """
    Do these two artist names refer to the same act?

    EQUALITY per credited act, not containment. A substring/subset test made
    'Frida' agree with 'Frida Leider' (a Wagner soprano) and 'Frida Boccara',
    'Allred' with 'David Allred', 'Bellini' with 'Vincenzo Bellini' and 'Cher'
    with 'Cher Lloyd' -- a shared first or last name is not the same person.
    Decoration is removed first, so the real equivalences still hold:
    'Frida (Anni-Frid Lyngstad)', 'DJ Tiesto feat. Kirsty Hawkshaw', and
    'Cissy Drinkard & The Sweet Inspirations' matching one of its credits.
    """
    ca, cb = _credit_names(a), _credit_names(b)
    if not ca or not cb:
        return False
    return any(x == y for x in ca for y in cb)


_CREDIT_NOISE = frozenset(
    "dj mc vj the a an of feat feats featuring ft with vs presents pres".split())



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
    recording_id: str = ""        # MusicBrainz recording (foreignRecordingId)
    artist_mbid: str = ""         # MusicBrainz artist (foreignArtistId)


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
                    recording_id=str(t.get("foreignRecordingId") or ""),
                    artist_mbid=str(art.get("foreignArtistId")
                                    or a.get("foreignArtistId") or ""),
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

# path -> (size, mtime_ns, SourceFile): a file unchanged since it was read is
# not read again. Every harvest pass reads the same seeding folders.
_TAG_CACHE: Dict[str, Tuple[int, int, "SourceFile"]] = {}
_TAG_CACHE_MAX = 50000


def read_tags(path: str) -> SourceFile:
    """Tags + duration for one file. Never raises.

    A TYPED open (audio_open.File without easy=True, audio_open.basic_tags):
    easy=True bypassed the typed open and sniffed every parser, 631 ms vs
    264 ms per FLAC, over ~2,200 files a pass (loops F9). Cached per (path,
    size, mtime)."""
    try:
        st = os.stat(path)
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = None
    if stamp is not None:
        hit = _TAG_CACHE.get(path)
        if hit is not None and hit[:2] == stamp:
            return dataclasses.replace(hit[2])
    sf = SourceFile(path=path)
    try:
        import audio_open
        mf = audio_open.File(path)
        if mf is None:
            return sf
        sf.title, sf.artist, sf.album = audio_open.basic_tags(mf)
        info = getattr(mf, "info", None)
        if info is not None and getattr(info, "length", None):
            sf.duration_ms = int(float(info.length) * 1000)
    except Exception as exc:  # noqa: BLE001
        logger.debug("harvest: tag read failed for %s: %s", path, exc)
        return sf                             # a failed read is not cached
    if stamp is not None:
        if len(_TAG_CACHE) >= _TAG_CACHE_MAX:
            _TAG_CACHE.clear()
        _TAG_CACHE[path] = stamp + (dataclasses.replace(sf),)
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
                if not artists_agree(sf.artist, w.artist_name):
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


# Spoken forms a tag and a database spell differently: "Cheatin'" /
# "Cheating", "Gonna" / "Going to".
_COLLOQUIAL = (
    (re.compile(r"(?i)\b(\w+?)in['\u2019]"), r"\1ing"),
    (re.compile(r"(?i)\bgonna\b"), "going to"),
    (re.compile(r"(?i)\bwanna\b"), "want to"),
    (re.compile(r"(?i)\bgotta\b"), "got to"),
)


def _same_song_title(a: Any, b: Any) -> bool:
    if norm_title(a) == norm_title(b):
        return True

    def spoken(s):
        s = str(s or "")
        for rx, rep in _COLLOQUIAL:
            s = rx.sub(rep, s)
        return norm_title(s)
    return spoken(a) == spoken(b)


def acoustid_verify(
    matches: Iterable[Match],
    acoustid,
    min_score: float = 0.5,
    require: bool = False,
) -> Tuple[List[Match], List[Tuple[Match, str]]]:
    """
    Final gate before anything moves: confirm the FINGERPRINT agrees.

    Tags and duration can both be right and the recording still wrong -- two
    different performances of a standard run to the same length, and a
    compilation's tags are often copied from a tracklist rather than the audio.
    AcoustID identifies the actual audio, so it is the only check that can catch
    that. Duration stays the primary gate because fingerprinting needs fpcalc, a
    network round trip and a rate limit (3 req/s here), so it runs LAST and only
    on what already passed everything else.

    `require=False` keeps a match whose fingerprint is simply unknown (no
    fpcalc, no network, nothing in the database -- common for obscure 1930s
    sides). `require=True` drops anything unproven.

    Returns (verified, rejected_with_reason).
    """
    ok: List[Match] = []
    bad: List[Tuple[Match, str]] = []
    if acoustid is None or not getattr(acoustid, "enabled", False):
        if require:
            return [], [(m, "AcoustID required but unavailable") for m in matches]
        return list(matches), []
    for m in matches:
        try:
            res = acoustid.identify_file(m.src.path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("harvest: acoustid failed for %s: %s", m.src.path, exc)
            res = None
        if not res:
            if require:
                bad.append((m, "no AcoustID match (unproven)"))
            else:
                ok.append(m)
            continue
        score = float(res.get("score") or 0.0)
        got_t, got_a = res.get("title"), res.get("artist")
        if score < min_score:
            bad.append((m, "AcoustID score %.2f below %.2f" % (score, min_score)))
            continue
        # The fingerprint IS the wanted recording (MusicBrainz id): proven,
        # whatever either side spells.
        if (m.want.recording_id
                and m.want.recording_id in (res.get("recording_ids") or ())):
            ok.append(m)
            continue
        # The fingerprint must name the SAME song. A mismatch here means the
        # tags lied -- exactly the garbage this gate exists to stop.
        if got_t and not _same_song_title(got_t, m.want.title):
            bad.append((m, "AcoustID says %r, not %r"
                        % (str(got_t)[:34], m.want.title[:34])))
            continue
        if (m.want.artist_mbid
                and m.want.artist_mbid in (res.get("artist_ids") or ())):
            ok.append(m)          # the same artist, under any credited name
            continue
        if got_a and m.want.artist_name:
            ak, wk = artist_key(got_a), artist_key(m.want.artist_name)
            if wk and ak and wk not in ak and ak not in wk:
                bad.append((m, "AcoustID artist %r != %r"
                            % (str(got_a)[:26], m.want.artist_name[:26])))
                continue
        ok.append(m)
    return ok, bad


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


def wanted_signature(index: Dict[str, List[WantedTrack]],
                     titles: Optional[Iterable[str]] = None) -> str:
    """
    Fingerprint of what Lidarr currently WANTS -- of the wanted tracks under
    `titles` (normalized titles) when given, else of all of them.

    The gate must cover this, not just the folder. A verdict of "no monitored
    album target" is a statement about Lidarr's library, so if only the folder
    were fingerprinted, adding an artist or unmonitoring an album would never
    re-open a parked item -- it would stay stuck forever.

    A source is gated on the wanted tracks ITS titles can match (match_files
    looks a file up by its normalized title and nothing else). Gated on the
    whole library's, any import anywhere re-opened every source: every pass on
    29 Sep said "0 skipped unchanged" and re-read ~2,200 files (loops F9).
    """
    if titles is None:
        ids = sorted(w.track_id for ws in index.values() for w in ws)
    else:
        ids = sorted(w.track_id for k in set(titles) for w in index.get(k, ()))
    import hashlib
    h = hashlib.sha1(repr(ids).encode("utf-8")).hexdigest()[:12]
    return "v%d:%d:%s" % (JUDGE_VERSION, len(ids), h)


# Bumped when what a pass would conclude about an unchanged folder changes
# (the matching or verification rules): every recorded verdict then differs
# once and each source is judged again under the new rules. 2: AcoustID by
# MusicBrainz id (1 Oct) -- songs it had wrongly rejected sat in sources the
# ledger skipped as unchanged.
JUDGE_VERSION = 2


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
        # source -> {"cid", "paths", "tracks": {albumId: [trackId]}, "mode"}:
        # imports submitted but not yet seen to land. The purge of a source
        # waits here until Lidarr's own state shows every track filed.
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._saved_at = time.monotonic()
        if path and os.path.exists(path):
            try:
                import json
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self._seen = dict(data.get("checked") or {})
                self.pending = dict(data.get("pending") or {})
            except Exception as exc:  # noqa: BLE001
                logger.debug("harvest ledger unreadable (%s): %s", path, exc)

    def add_pending(self, source: str, rec: Dict[str, Any]) -> None:
        self.pending[source] = rec
        self._dirty = True
        self.save()            # a deploy between submit and settle loses nothing

    def drop_pending(self, source: str) -> None:
        if self.pending.pop(source, None) is not None:
            self._dirty = True

    def unchanged(self, source: str, folder_sig: str,
                  index: Dict[str, List[WantedTrack]]) -> bool:
        """The folder is as it was judged, and so is what Lidarr wants under
        the titles found in it. (An entry in the older "folder|wanted"
        string form is judged again once.)"""
        rec = self._seen.get(source)
        if not isinstance(rec, dict) or rec.get("folder") != folder_sig:
            return False
        return rec.get("wanted") == wanted_signature(index, rec.get("titles") or [])

    def mark(self, source: str, folder_sig: str, titles: Iterable[str],
             index: Dict[str, List[WantedTrack]]) -> None:
        keys = sorted({t for t in titles if t})
        self._seen[source] = {"folder": folder_sig, "titles": keys,
                              "wanted": wanted_signature(index, keys)}
        self._dirty = True

    def save_every(self, seconds: float = 30.0) -> None:
        """save(), at most every `seconds`: a pass is minutes long, and a
        restart in the middle used to lose every verdict of the pass (they
        were saved only at its end)."""
        if time.monotonic() - self._saved_at >= seconds:
            self.save()

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
                json.dump({"checked": self._seen, "pending": self.pending}, fh)
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved_at = time.monotonic()
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
    purge_leftovers_enabled: bool = False,
    leftovers_dir: Optional[str] = None,
    qbt=None,
    keep_dir: Optional[str] = None,
    acoustid=None,
    acoustid_min_score: float = 0.5,
    acoustid_required: bool = False,
    category: str = "",
) -> Dict[str, Any]:
    """
    Walk each source folder, harvest whatever songs Lidarr is missing, and
    (unless dry_run) import them track-by-track.

    import_mode is COPY on purpose: these sources are usually SEEDING torrents,
    and moving files out from under qBittorrent breaks the seed.
    """
    gen = getattr(lidarr, "failure_generation", 0)
    index = build_wanted_index(lidarr)
    # A Lidarr failure while the index was built makes "not wanted" unknown,
    # and the purge deletes on "not wanted". No purge this pass, and no
    # source is recorded as judged: its "nothing wanted here" could be a
    # failed call (list_tracks_for_album answers [] on failure).
    index_ok = getattr(lidarr, "failure_generation", 0) == gen
    if ledger is not None and index_ok and purge_leftovers_enabled:
        settle_pending_purges(
            lidarr, ledger, index, tolerance_seconds=tolerance_seconds,
            staging_dir=leftovers_dir, qbt=qbt, keep_dir=keep_dir,
            category=category)
    # The consolidated keep-folder is a source in its own right: songs parked
    # there are exactly the ones a later pass should be able to take.
    sources = list(sources)
    if keep_dir and os.path.isdir(keep_dir) and keep_dir not in sources:
        sources.append(keep_dir)
    stats = {"sources": 0, "skipped_unchanged": 0, "scanned": 0,
             "matched": 0, "imported": 0, "albums": 0, "no_quality": 0,
             "busy": 0}
    albums: set = set()
    budget = max(0, int(max_files))
    for src_dir in sources:
        if not src_dir or not os.path.isdir(src_dir):
            continue
        # One worker per folder: the CUE job, the sweep or a WebUI action
        # may be importing from it or deleting it right now. Skipped, it
        # records no verdict (the ledger is not marked) and is taken on a
        # later pass.
        if not claims.claim(src_dir):
            stats["busy"] += 1
            continue
        try:
            stats["sources"] += 1
            fsig = folder_signature(src_dir)
            if (skip_unchanged and ledger is not None
                    and ledger.unchanged(src_dir, fsig, index)):
                stats["skipped_unchanged"] += 1
                continue
            files = scan_folder(src_dir, limit=budget)
            budget = max(0, budget - len(files))
            # The file cap may have cut the scan short: then the folder was
            # not judged whole (folder_signature starts with its file count).
            whole = len(files) >= int(fsig.split(":", 1)[0])
            rep = match_files(files, index, tolerance_seconds=tolerance_seconds)
            stats["scanned"] += rep.scanned
            stats["matched"] += len(rep.matches)
            for line in format_report(rep, source=src_dir):
                logger.info("%s", line)
            if rep.matches and acoustid is not None:
                # LAST gate before anything moves: the fingerprint must agree.
                verified, unproven = acoustid_verify(
                    rep.matches, acoustid, min_score=acoustid_min_score,
                    require=acoustid_required)
                for m, why in unproven:
                    logger.warning("harvest: AcoustID rejected %s -- %s",
                                   os.path.basename(m.src.path)[:40], why)
                stats["acoustid_rejected"] = (stats.get("acoustid_rejected", 0)
                                              + len(unproven))
                rep.matches = verified
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
                        # A submitted command is not an import: Lidarr's queue
                        # runs minutes behind, and purging now raced it (and won).
                        # The purge is recorded and settled on a later pass, once
                        # every submitted track has its file.
                        if purge_leftovers_enabled and ledger is not None:
                            tracks: Dict[str, List[int]] = {}
                            for e in entries:
                                tracks.setdefault(str(e["albumId"]), []).extend(
                                    int(x) for x in e["trackIds"])
                            ledger.add_pending(src_dir, {
                                "cid": int(cid), "mode": import_mode,
                                "paths": [e["path"] for e in entries],
                                "tracks": tracks})
                            logger.info("harvest: purge of %s waits until Lidarr "
                                        "has filed all %d track(s)",
                                        os.path.basename(src_dir.rstrip("/"))[:44],
                                        len(entries))
                    else:
                        logger.warning("harvest: Lidarr refused the import of "
                                       "%d track(s) from %s", len(entries), src_dir)
            if ledger is not None and index_ok and whole:
                # Only remember a source once it has been fully judged, against
                # an index Lidarr answered in full. An import CHANGES the wanted
                # set under these titles, so the next pass re-derives anyway --
                # that is correct, not waste.
                ledger.mark(src_dir, fsig,
                            (norm_title(f.title) for f in files), index)
                ledger.save_every()
            if budget <= 0:
                logger.info("harvest: hit the %d-file cap for this pass", max_files)
                break
        finally:
            claims.release(src_dir)
    stats["albums"] = len(albums)
    if ledger is not None:
        ledger.save()
    logger.info(
        "harvest pass: %d source(s), %d skipped unchanged, %d file(s) scanned, "
        "%d match(es), %d imported into %d album(s)%s%s",
        stats["sources"], stats["skipped_unchanged"], stats["scanned"],
        stats["matched"], stats["imported"], stats["albums"],
        ", %d busy (next pass)" % stats["busy"] if stats["busy"] else "",
        "  [DRY RUN -- nothing written]" if dry_run else "")
    return stats


# ---------------------------------------------------------------- leftovers

def settle_pending_purges(lidarr, ledger, index, tolerance_seconds: float = 10.0,
                          staging_dir: Optional[str] = None, qbt=None,
                          keep_dir: Optional[str] = None,
                          category: str = "") -> int:
    """Purge each source whose submitted import has provably landed: the
    command completed, every submitted track now has a file, and in move mode
    every submitted path has left the source. A command still running waits;
    one that failed, or left a track without a file, purges nothing.
    Returns the number of sources purged."""
    done = 0
    for src, rec in list(ledger.pending.items()):
        name = os.path.basename(src.rstrip("/"))[:44]
        status = lidarr.command_status(int(rec.get("cid") or 0))
        if status is None or status in ("queued", "started"):
            continue                              # not yet, or no answer
        if status != "completed":
            logger.warning("harvest purge %s: import command %s ended %r -- "
                           "nothing purged", name, rec.get("cid"), status)
            ledger.drop_pending(src)
            continue
        gen = getattr(lidarr, "failure_generation", 0)
        filed = True
        for aid, tids in (rec.get("tracks") or {}).items():
            have = {int(t.get("id")) for t in
                    (lidarr.list_tracks_for_album(int(aid)) or [])
                    if t.get("hasFile") or t.get("trackFileId")}
            if not set(int(x) for x in tids) <= have:
                filed = False
                break
        if getattr(lidarr, "failure_generation", 0) != gen:
            continue                              # an outage is not an answer
        paths = list(rec.get("paths") or [])
        if filed and rec.get("mode") == "move":
            filed = not any(os.path.exists(p) for p in paths)
        if not filed:
            logger.warning("harvest purge %s: Lidarr did not file every "
                           "submitted track -- nothing purged", name)
            ledger.drop_pending(src)
            continue
        if os.path.isdir(src):
            with claims.held(src) as ok:
                if not ok:
                    # a worker is in it right now: the purge stays pending
                    logger.info("harvest purge %s: being processed right now "
                                "-- settled on a later pass", name)
                    continue
                try:
                    purge_leftovers(src, imported_paths=paths, index=index,
                                    tolerance_seconds=tolerance_seconds,
                                    staging_dir=staging_dir, delete=True,
                                    qbt=qbt, keep_dir=keep_dir,
                                    category=category)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("harvest: purge failed for %s: %s", src, exc)
                    continue
        ledger.drop_pending(src)
    ledger.save()
    return done


def _is_within(path: str, root: str) -> bool:
    p, r = os.path.abspath(path), os.path.abspath(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _unrepeat_keep_name(full: str, keep_dir: str) -> Optional[str]:
    """Undo the names the keep-folder loop inflated: a file already in the
    keep folder was "consolidated" onto itself every pass, and the collision
    branch added " (<keep folder>)" each time -- '10. Going To The River
    (_harvest_pending) (_harvest_pending) ...' up to 255 characters, then
    [Errno 36] (loops F12). Renames it back to the name without those
    suffixes when that name is free; returns the new path, else None."""
    tag = " (%s)" % os.path.basename(keep_dir.rstrip("/\\"))
    stem, ext = os.path.splitext(os.path.basename(full))
    if not stem.endswith(tag):
        return None
    while stem.endswith(tag):
        stem = stem[: -len(tag)]
    dest = os.path.join(os.path.dirname(full), stem + ext)
    if not stem or os.path.exists(dest):
        return None
    os.replace(full, dest)
    return dest


def purge_leftovers(
    src_dir: str,
    imported_paths: Iterable[str],
    index: Dict[str, List[WantedTrack]],
    tolerance_seconds: float = 10.0,
    staging_dir: Optional[str] = None,
    delete: bool = False,
    qbt=None,
    keep_dir: Optional[str] = None,
    acoustid=None,
    acoustid_min_score: float = 0.5,
    acoustid_required: bool = False,
    category: str = "",
) -> Dict[str, Any]:
    """
    Deal with what a harvested source still holds once its wanted tracks are in
    the library.

    A file survives ONLY if it is still wanted -- i.e. it matches a track Lidarr
    is missing. Everything else (other performances, non-wanted songs, artwork,
    logs, cue sheets) is leftover.

    `staging_dir`  move leftovers there first, so a pass can be inspected
                   before anything is destroyed
    `delete`       actually remove them (default OFF)
    `qbt`          if given, the torrent covering this folder is removed from
                   the client once its files are gone -- seeding is already
                   broken by the move, and the registration is not wanted
    `category`     the ONLY qBittorrent category whose torrents may be removed.
                   The client is shared with other apps, so an empty value here
                   means "touch no torrent at all" rather than "touch any"

    IMPORTANT: "not wanted" means not wanted RIGHT NOW, and "not in the wanted
    index" is not "in the library": a song Lidarr has no record of is neither.
    So leftover AUDIO is never deleted here -- it is moved to `staging_dir`
    when one is set, otherwise left in place -- and the torrent is removed
    only when no such song is left in the folder. `imported_paths` must be
    files Lidarr has already filed (settle_pending_purges checks).
    """
    stats = {"kept_wanted": 0, "leftover": 0, "moved": 0, "deleted": 0,
             "bytes_freed": 0, "torrent_removed": 0, "errors": 0}
    done = {os.path.abspath(p) for p in imported_paths}
    leftovers: List[str] = []
    for dirpath, _dirs, names in os.walk(src_dir):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            if os.path.abspath(full) in done:
                continue                      # already moved into the library
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                # Still wanted? Then it is NOT leftover -- a later pass takes it.
                sf = read_tags(full)
                rep = match_files([sf], index,
                                  tolerance_seconds=tolerance_seconds)
                if rep.matches:
                    # Still wanted -- a later pass takes it. Consolidate it into
                    # the harvest folder so the original box folder can be
                    # dissolved entirely and its torrent dropped, instead of
                    # leaving one wanted file behind pinning the whole thing.
                    stats["kept_wanted"] += 1
                    if keep_dir and _is_within(dirpath, keep_dir):
                        # Already consolidated: it stays as it is. It used to
                        # be "moved" onto itself, and the collision branch
                        # below renamed it every pass (loops F12).
                        try:
                            if _unrepeat_keep_name(full, keep_dir):
                                stats["kept_renamed_back"] = stats.get(
                                    "kept_renamed_back", 0) + 1
                        except OSError as exc:
                            logger.debug("harvest keep: %s -- %s", full, exc)
                    elif keep_dir:
                        try:
                            dest = os.path.join(keep_dir, os.path.basename(full))
                            os.makedirs(keep_dir, exist_ok=True)
                            # Never clobber a same-named song from another box.
                            if os.path.exists(dest):
                                stem, ext = os.path.splitext(dest)
                                dest = "%s (%s)%s" % (
                                    stem,
                                    os.path.basename(dirpath.rstrip("/"))[:24],
                                    ext)
                            if not os.path.exists(dest):
                                os.replace(full, dest)
                                stats["kept_moved"] = stats.get(
                                    "kept_moved", 0) + 1
                        except OSError as exc:
                            stats["errors"] += 1
                            logger.warning("harvest keep: %s -- %s", full, exc)
                    continue
            leftovers.append(full)
    stats["leftover"] = len(leftovers)
    unproven = 0
    for full in leftovers:
        is_audio = os.path.splitext(full)[1].lower() in AUDIO_EXTS
        if is_audio and not staging_dir:
            unproven += 1                 # nobody proved it is in the library
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        try:
            if staging_dir:
                rel = os.path.relpath(full, src_dir)
                dest = os.path.join(staging_dir,
                                    os.path.basename(src_dir.rstrip("/")), rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                os.replace(full, dest)
                stats["moved"] += 1
                full = dest
            if delete and not is_audio:
                os.remove(full)
                stats["deleted"] += 1
                stats["bytes_freed"] += size
        except OSError as exc:
            stats["errors"] += 1
            logger.warning("harvest purge: %s -- %s", full, exc)
    stats["kept_unproven"] = unproven
    if unproven:
        logger.info("harvest purge %s: %d song(s) are neither wanted nor "
                    "proven in the library -- kept, and the torrent with them",
                    os.path.basename(src_dir.rstrip("/"))[:44], unproven)
    # Any audio still here that Lidarr has not filed (kept-wanted that could
    # not be moved, unproven songs) goes down with a removed torrent's data.
    remaining = [os.path.join(dp, n) for dp, _d, ns in os.walk(src_dir)
                 for n in ns if os.path.splitext(n)[1].lower() in AUDIO_EXTS
                 and os.path.abspath(os.path.join(dp, n)) not in done]
    if (qbt is not None and delete and not remaining
            and (stats["deleted"] or stats["moved"] or done)):
        # The files are gone from where the torrent expects them, so the
        # torrent can only error from here on. Remove it WITH data so any
        # remaining pieces go too.
        # ONLY our own category. This used to enumerate EVERY torrent in a shared
        # client and then delete with data on a substring match -- one loose
        # folder name away from destroying another app's download. An empty
        # category matches nothing (fail closed): skipping a cleanup is always
        # cheaper than removing a stranger's torrent.
        want = str(category or "").strip().lower()
        if not want:
            logger.debug("harvest purge: no torrent category configured -- "
                         "leaving every torrent alone")
        else:
            try:
                base = os.path.basename(src_dir.rstrip("/"))
                for t in (qbt.torrents(category=category) or []):
                    if str(t.get("category") or "").strip().lower() != want:
                        continue                  # belt and braces
                    cp = str(t.get("content_path") or "")
                    nm = str(t.get("name") or "")
                    if not nm:
                        continue
                    # The torrent whose content IS this folder, and no other:
                    # a substring test also matched a discography containing
                    # it (its sibling albums went with it) or any torrent
                    # whose path happened to be a prefix.
                    top = os.path.basename(cp.replace("\\", "/").rstrip("/"))
                    if base and (top == base or (not cp and nm == base)):
                        if qbt.remove(str(t.get("hash")), delete_files=True):
                            stats["torrent_removed"] += 1
                            logger.info("harvest purge: removed torrent %r "
                                        "(with data) -- it was harvested",
                                        nm[:60])
            except Exception as exc:  # noqa: BLE001
                logger.warning("harvest purge: torrent removal failed: %s", exc)
    logger.info(
        "harvest purge %s: %d still-wanted kept, %d leftover (%d staged, "
        "%d deleted, %.2f GB freed), %d torrent(s) removed%s",
        os.path.basename(src_dir.rstrip("/"))[:44], stats["kept_wanted"],
        stats["leftover"], stats["moved"], stats["deleted"],
        stats["bytes_freed"] / 1e9, stats["torrent_removed"],
        "" if delete else "  [REPORT ONLY -- nothing deleted]")
    return stats
