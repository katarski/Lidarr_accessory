"""
Album assembly (Torrenting requirement e).

The needs-attention list is full of compilations, box sets and best-ofs that
Lidarr has no album record for, so nothing can ever import them -- yet the SONGS
inside them are often exactly the tracks missing from albums Lidarr *does* want.
This module works out, per missing album, which of those songs are available and
where, so the album can be assembled from them.

Design notes
------------
* Matching is by SONG, not by folder name. Sources are indexed from their ID
  TAGS first (title/artist/album), falling back to the filename -- compilation
  file naming is wildly inconsistent ("01 - Artist - Title.mp3",
  "03. Title.flac", "Artist-Title-cd1.mp3"), so both halves of a
  "something - something" filename are considered as (artist, title).
* The target album's ARTIST must agree, otherwise a same-titled song by another
  act (a cover on a tribute compilation) would look like a match. This is the
  single most important guard here.
* A source file is NEVER consumed exclusively: one compilation legitimately
  feeds several assemblies (a hits CD can supply tracks for three albums), so
  matches are recorded per album and the "keep this file" set is the UNION over
  all assemblies -- which is what the torrent deselect needs.
* State is a single JSON file so the WebUI tab serves it as-is (the same
  file-backed pattern as held_store: filled when planned, updated in place,
  never rebuilt per page load).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("assembly")

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape",
    ".wv", ".alac", ".aiff", ".aif", ".wma", ".dsf", ".dff", ".mpc",
}

# Bracketed/trailing noise that shows up in song titles and must not defeat a
# match: "(Remastered 2011)", "[Live]", "- feat. X", "(Radio Edit)".
_PAREN_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT_RE = re.compile(r"(?i)\s*(?:feat\.?|featuring|ft\.?|with)\s+.*$")
_NOISE_TAIL_RE = re.compile(
    r"(?i)\s*[-–—]?\s*(?:remaster(?:ed)?(?:\s*\d{4})?|\d{4}\s*remaster|"
    r"radio\s*edit|album\s*version|single\s*version|mono|stereo|live|"
    r"explicit|clean|bonus\s*track|remix|edit|version)\s*$")
# Leading track-number / disc noise on filenames: "01 - ", "03.", "1-04 ",
# "a1 ", "cd2 05 ".
_LEAD_NUM_RE = re.compile(
    r"(?i)^\s*(?:cd\s*\d+\s*[-_. ]*)?"          # optional "cd2 " / "cd2-"
    r"(?:\d{1,2}\s*[-_.]\s*)?"                  # optional disc prefix "1-04"
    r"(?:[a-d]?\d{1,3})\s*[-_.)\]]+\s*")        # the track number itself
# Trailing disc/format tokens glued onto a filename: "-cd1", " cd 2", "_disc3".
_TRAIL_DISC_RE = re.compile(
    r"(?i)[\s\-_.]*(?:cd|disc|disk)\s*\d{1,2}\s*$")


def _fold(s: str) -> str:
    """Accent-fold + lowercase."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def norm_title(s: str) -> str:
    """
    Normalize a song title for comparison: fold accents, drop bracketed and
    trailing noise, drop a 'feat.' clause, collapse everything non-alphanumeric.
    "Bullet For Narcissus (Remastered 2020)" -> "bullet for narcissus".
    """
    t = _fold(s)
    t = _PAREN_RE.sub(" ", t)
    t = _FEAT_RE.sub(" ", t)
    for _ in range(2):                      # e.g. "... - live - remastered"
        t = _NOISE_TAIL_RE.sub("", t)
    # A leading track number survives in sloppy TAGS too ("03. Tonite"), and a
    # trailing disc token in filename-derived candidates ("Searchin-cd1").
    t = _LEAD_NUM_RE.sub("", t)
    t = _TRAIL_DISC_RE.sub("", t)
    # Keep ALL word characters, not just a-z: a Latin-only class deletes every
    # Cyrillic / Greek / CJK character, turning a non-Latin title into "" --
    # which then compares as garbage instead of not matching.
    t = re.sub(r"[\W_]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s{2,}", " ", t).strip()


def norm_artist(s: str) -> str:
    """
    Normalize an artist name: fold, &->and, drop a leading 'the'.

    Unicode-safe on purpose. A Latin-only character class reduced "Белослава"
    to an EMPTY string, and an empty target artist used to switch the artist
    guard OFF -- so every non-Latin artist matched any song with a similar
    title (a real false positive: Белослава / Красотата "matched" Quincy Jones'
    "Walkin'").
    """
    a = _fold(s).replace("&", " and ")
    a = re.sub(r"[\W_]+", " ", a, flags=re.UNICODE).strip()
    a = re.sub(r"^the\s+", "", a)
    return re.sub(r"\s{2,}", " ", a).strip()


def title_from_filename(name: str) -> List[str]:
    """
    Candidate (title) strings parsed out of a file name, best first. Handles
    "01 - Artist - Title", "Artist - Title", "03. Title", "01 Title".
    Returns the raw candidates; caller normalizes.
    """
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = stem.replace("_", " ").strip()
    stem = _LEAD_NUM_RE.sub("", stem)
    stem = _TRAIL_DISC_RE.sub("", stem)
    out = [stem]
    # A track number separated by nothing but a SPACE ("1-04 Never 2 Far",
    # "04 Never 2 Far") can't be stripped unconditionally -- real titles start
    # with numbers ("99 Problems", "24K Magic"). So offer BOTH forms as
    # candidates and let the best-scoring one win.
    bare = re.sub(r"^\s*\d{1,3}\s+", "", stem)
    if bare and bare != stem:
        out.append(bare)
    # Prefer the spaced " - " separator; fall back to bare hyphens for rips
    # named "Eminem-Searchin-cd1.mp3" that have no spaces at all.
    seps = [" - "] if " - " in stem else (["-"] if "-" in stem else [])
    for sep in seps:
        parts = [p.strip() for p in stem.split(sep) if p.strip()]
        if len(parts) >= 2:
            # "Artist - Title" -> title is the LAST part; "A - B - Title" too.
            out.append(parts[-1])
            # ...but some rips are "Title - Artist", so offer the first too.
            out.append(parts[0])
            # And the same number-stripped variant for "1-04 Artist - Title".
            last_bare = re.sub(r"^\s*\d{1,3}\s+", "", parts[-1])
            if last_bare and last_bare != parts[-1]:
                out.append(last_bare)
    return [o for o in out if o]


def artists_from_filename(name: str) -> List[str]:
    """Candidate artist strings from a compilation-style file name."""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = _LEAD_NUM_RE.sub("", stem.replace("_", " ").strip())
    stem = _TRAIL_DISC_RE.sub("", stem)
    sep = " - " if " - " in stem else ("-" if "-" in stem else "")
    if not sep:
        return []
    parts = [p.strip() for p in stem.split(sep) if p.strip()]
    if len(parts) < 2:
        return []
    out: List[str] = []
    for cand in (parts[0], parts[-1]):
        out.append(cand)
        # "1-04 Eminem" -> also offer "Eminem": a disc/track prefix glued to the
        # artist would otherwise never clear the artist-agreement threshold.
        bare = re.sub(r"^\s*\d{1,3}\s*[-_.]?\s*\d{0,3}\s+", "", cand)
        if bare and bare != cand:
            out.append(bare)
    return out


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalized strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # Containment is strong but not exact -- scale by length ratio so
        # "love" inside "love will tear us apart" doesn't score 0.9.
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        return 0.90 * (len(short) / float(len(long))) + 0.05
    return SequenceMatcher(None, a, b).ratio()


class SourceIndex:
    """
    Index of candidate songs found on disk: for each audio file, its tag-derived
    (title, artist, album) plus filename-derived fallbacks, so an album's track
    list can be matched against it.
    """

    def __init__(self, tag_reader=None) -> None:
        # tag_reader(path) -> (title, artist, album); injected so this module
        # stays independent of mutagen and is easy to test.
        self._tag_reader = tag_reader
        self.entries: List[Dict[str, Any]] = []

    def add_folder(self, folder: Path, max_files: int = 5000) -> int:
        """Index every audio file under `folder`. Returns files added."""
        added = 0
        try:
            for dp, _dn, fns in os.walk(folder):
                for fn in fns:
                    if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                        continue
                    if added >= max_files:
                        return added
                    p = Path(dp) / fn
                    title = artist = album = ""
                    if self._tag_reader is not None:
                        try:
                            title, artist, album = self._tag_reader(p)
                        except Exception:  # noqa: BLE001
                            title = artist = album = ""
                    self.entries.append({
                        "path": str(p),
                        "tag_title": title or "",
                        "tag_artist": artist or "",
                        "tag_album": album or "",
                        "n_titles": [norm_title(t)
                                     for t in ([title] if title else [])
                                     + title_from_filename(fn)],
                        # STRONG artist evidence -- the tag and the file name.
                        # These describe the SONG, so if they name a different
                        # artist the file really is someone else's recording.
                        "n_artists": [
                            norm_artist(a) for a in
                            ([artist] if artist else [])
                            + artists_from_filename(fn)
                        ],
                        # WEAK evidence -- the containing folder names. A
                        # single-artist album folder names the artist even when
                        # the files don't, so it can CONFIRM a match; but a
                        # compilation folder ("VA", "Hip Hop Gold 3CD") names no
                        # artist, so it must never be able to REJECT one.
                        "n_artists_weak": [
                            norm_artist(a) for a in
                            (Path(dp).name, Path(dp).parent.name)
                        ],
                        "size": (p.stat().st_size
                                 if p.exists() else 0),
                    })
                    added += 1
        except OSError as exc:
            logger.debug("assembly: cannot walk %s: %s", folder, exc)
        return added

    def __len__(self) -> int:
        return len(self.entries)

    def build_lookup(self) -> None:
        """
        Build an inverted index so planning is not O(albums x tracks x sources).

        A naive scan over every source song for every track of every missing
        album is billions of comparisons on a real library (2700 gap albums x
        ~12 tracks x thousands of songs) -- it simply never finishes. Instead:
          * `by_title`  exact normalized title -> entries (an O(1) hit, which is
            what the overwhelming majority of real matches are once titles are
            normalized),
          * `by_token`  word -> entry indexes, so a fuzzy fallback only has to
            look at songs sharing at least one word with the track.
        """
        self.by_title: Dict[str, List[Dict[str, Any]]] = {}
        self.by_token: Dict[str, set] = {}
        for i, e in enumerate(self.entries):
            for t in e.get("n_titles") or []:
                if not t:
                    continue
                self.by_title.setdefault(t, []).append(e)
                for tok in t.split():
                    if len(tok) >= 3:      # skip "a"/"of"-style noise
                        self.by_token.setdefault(tok, set()).add(i)

    def artists(self) -> set:
        """Every normalized artist name the indexed songs point at (tags, file
        names and containing folders). Used to skip planning for albums whose
        artist has no presence in the sources at all -- which is what makes a
        library-wide pass affordable: without it, every one of thousands of
        missing albums costs a Lidarr track-list request."""
        out: set = set()
        for e in self.entries:
            for a in (e.get("n_artists") or []) + (e.get("n_artists_weak") or []):
                if a:
                    out.add(a)
        return out

    def candidates_for(self, n_title: str, cap: int = 400
                       ) -> List[Dict[str, Any]]:
        """Source songs worth comparing against this normalized track title:
        exact-title hits first, then songs sharing its rarest words."""
        if not hasattr(self, "by_title"):
            self.build_lookup()
        out: List[Dict[str, Any]] = list(self.by_title.get(n_title) or [])
        if out:
            return out
        toks = [t for t in n_title.split() if len(t) >= 3]
        if not toks:
            return []
        # Rarest words first -- they discriminate best and keep the set small.
        toks.sort(key=lambda t: len(self.by_token.get(t) or ()))
        idxs: set = set()
        for t in toks:
            idxs |= (self.by_token.get(t) or set())
            if len(idxs) >= cap:
                break
        return [self.entries[i] for i in list(idxs)[:cap]]


class AssemblyPlanner:
    """Works out which songs of a missing album are available on disk."""

    def __init__(self, min_score: float = 0.87,
                 require_artist: bool = True) -> None:
        self.min_score = float(min_score)
        self.require_artist = bool(require_artist)

    def _artist_ok(self, entry: Dict[str, Any], want_artist: str,
                   album_is_various: bool) -> Tuple[bool, float]:
        """
        Does this source file plausibly belong to `want_artist`? Returns
        (ok, bonus). A compilation's per-track artist tag is the reliable
        signal; its ALBUM tag is not (it's the compilation's name).
        """
        if not self.require_artist or album_is_various:
            return True, 0.0
        if not want_artist:
            # We cannot verify the artist at all, so we must NOT fall through to
            # "title only" -- that is exactly how a Bulgarian album ended up
            # claiming a Quincy Jones track. Refuse; the album shows 0% instead
            # of a wrong match.
            return False, 0.0
        strong = [a for a in (entry.get("n_artists") or []) if a]
        for a in strong:
            if similarity(a, want_artist) >= 0.90:
                return True, 0.05          # the song itself names our artist
        weak = [a for a in (entry.get("n_artists_weak") or []) if a]
        for a in weak:
            if similarity(a, want_artist) >= 0.90:
                return True, 0.05          # its folder names our artist
        if strong:
            # The song's own tag/name names SOMEONE ELSE -> genuinely a different
            # recording (a cover on a tribute compilation). Reject.
            return False, 0.0
        # No strong evidence either way (untagged file whose name carries no
        # artist). Absence of evidence is not evidence of a mismatch, so allow it
        # on title alone -- rejecting here makes untagged compilations unusable.
        return True, 0.0

    def plan_album(
        self, artist: str, album: str, tracks: Sequence[Dict[str, Any]],
        index: SourceIndex, album_is_various: bool = False,
    ) -> Dict[str, Any]:
        """
        Match one album's track list against the source index.

        `tracks` is Lidarr's track rows [{id, title, trackNumber, ...}].
        Returns a plan dict: {artist, album, total, matched:[...],
        missing:[...], pct, sources:{path: [track titles]}}.
        """
        want_artist = norm_artist(artist)
        matched: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        # Tracks the LIBRARY already holds. Previously every track was either
        # sourced from a download or declared missing, with no third state, so
        # a song already sitting in the library was reported as "still missing"
        # purely because no download folder happened to contain it. Odetta's
        # `Sings Ballads and Blues` read 18/20 with "Deep River" and "Chilly
        # Winds" listed as missing while Lidarr had both -- the album is
        # completable, and the UI said it was not.
        present: List[Dict[str, Any]] = []
        for t in tracks:
            title = str(t.get("title") or "").strip()
            nt = norm_title(title)
            if not nt:
                continue
            if t.get("hasFile"):
                present.append({
                    "track_id": t.get("id"), "track": title,
                    "number": (t.get("absoluteTrackNumber")
                               or t.get("trackNumber")),
                })
                continue        # nothing to source; it is already in place
            best: Optional[Tuple[float, Dict[str, Any], str]] = None
            # Only compare against songs the inverted index says could match.
            for e in index.candidates_for(nt):
                ok, bonus = self._artist_ok(e, want_artist, album_is_various)
                if not ok:
                    continue
                for cand in e.get("n_titles") or []:
                    if not cand:
                        continue
                    s = similarity(nt, cand) + bonus
                    if best is None or s > best[0]:
                        best = (s, e, cand)
                        if s >= 1.0:       # can't beat an exact hit
                            break
                if best is not None and best[0] >= 1.0:
                    break
            if best and best[0] >= self.min_score:
                score, e, _c = best
                matched.append({
                    "track_id": t.get("id"),
                    "track": title,
                    "number": t.get("absoluteTrackNumber") or t.get("trackNumber"),
                    "medium": t.get("mediumNumber") or 1,
                    "source": e["path"],
                    "score": round(min(1.0, score), 3),
                    "via": "tag" if e.get("tag_title") else "filename",
                })
            else:
                missing.append({
                    "track_id": t.get("id"),
                    "track": title,
                    "number": t.get("absoluteTrackNumber") or t.get("trackNumber"),
                    "best_score": round(best[0], 3) if best else 0.0,
                })
        total = len(matched) + len(missing) + len(present)
        sources: Dict[str, List[str]] = {}
        for m in matched:
            sources.setdefault(m["source"], []).append(m["track"])
        # Completeness is what the album will look like AFTER the import, so
        # already-present tracks count towards it. Odetta: 18 sourced + 2
        # already in the library = 100%, not 90% with two "missing".
        have = len(matched) + len(present)
        return {
            "artist": artist, "album": album, "total": total,
            "matched": matched, "missing": missing, "present": present,
            "n_matched": len(matched), "n_missing": len(missing),
            "n_present": len(present),
            "pct": round(100.0 * have / total, 1) if total else 0.0,
            "sources": sources,
            "updated": time.time(),
        }


class AssemblyStore:
    """
    Thread-safe, JSON-backed set of assembly plans (one per missing album).

    Same contract as HeldStore: the WebUI reads this file AS-IS so the tab is
    instant, a background pass keeps it current, and an entry disappears when
    its album stops being a gap.
    """

    def __init__(self, path: Optional[Path], clock=time.time) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._clock = clock
        self._items: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                self._items = {str(e.get("id")): e for e in items
                               if isinstance(e, dict) and e.get("id")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("AssemblyStore: could not load %s: %s",
                           self.path, exc)

    def _save_locked(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"items": list(self._items.values())}, indent=2),
                encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("AssemblyStore: could not save %s: %s",
                           self.path, exc)

    def upsert(self, album_id: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(plan)
        entry["id"] = str(album_id)
        with self._lock:
            prev = self._items.get(entry["id"]) or {}
            entry["created"] = prev.get("created") or self._clock()
            self._items[entry["id"]] = entry
            self._save_locked()
        return entry

    def remove(self, album_id: Any) -> bool:
        with self._lock:
            gone = self._items.pop(str(album_id), None) is not None
            if gone:
                self._save_locked()
        return gone

    def keep_only(self, album_ids: Iterable[Any]) -> int:
        """Drop plans for albums that are no longer gaps. Returns removed."""
        keep = {str(a) for a in album_ids}
        with self._lock:
            drop = [k for k in self._items if k not in keep]
            for k in drop:
                del self._items[k]
            if drop:
                self._save_locked()
        return len(drop)

    def get(self, album_id: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            e = self._items.get(str(album_id))
            return dict(e) if e else None

    def list(self, verify_sources: bool = True) -> List[Dict[str, Any]]:
        """
        Every plan. With `verify_sources` (the default) a matched song whose
        SOURCE FILE NO LONGER EXISTS is dropped and the counts recomputed, so
        the tab never advertises an assembly it cannot perform.

        This is not hypothetical: `Counting Crows / Saturday Nights & Sunday
        Mornings` showed 14/14 "completely assembled" while all fourteen source
        files were gone -- consumed by the harvest, which imports in MOVE mode,
        or swept up by a purge. Pressing Add to library then failed with "no file
        could be prepared", because there was nothing left to copy. The plan is
        only ever as good as the files it points at, and those move underneath it.
        """
        with self._lock:
            out = [dict(e) for e in self._items.values()]
        if verify_sources:
            for e in out:
                matched = e.get("matched") or []
                if not matched:
                    continue
                alive = [m for m in matched
                         if m.get("source") and os.path.isfile(str(m["source"]))]
                if len(alive) == len(matched):
                    continue
                gone = len(matched) - len(alive)
                e["matched"] = alive
                e["n_matched"] = len(alive)
                total = int(e.get("total") or 0)
                e["pct"] = (round(100.0 * len(alive) / total, 1) if total else 0.0)
                e["sources_missing"] = gone
                e["sources"] = {p: v for p, v in (e.get("sources") or {}).items()
                                if os.path.isfile(str(p))}
        out.sort(key=lambda e: (-float(e.get("pct") or 0),
                                str(e.get("artist") or "")))
        return out

    def needed_files(self) -> Dict[str, List[str]]:
        """
        UNION of source files needed across ALL assemblies -> the albums each
        one feeds. This is what the torrent deselect keeps: one compilation can
        serve several assemblies, so a file is needed if ANY plan wants it.
        """
        out: Dict[str, List[str]] = {}
        with self._lock:
            for e in self._items.values():
                label = f"{e.get('artist')} - {e.get('album')}"
                for path in (e.get("sources") or {}):
                    out.setdefault(path, [])
                    if label not in out[path]:
                        out[path].append(label)
        return out
