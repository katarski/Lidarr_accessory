"""
CUE sheet parser.

Tries deterministic parsing first (handles the 95% case: properly formatted
.cue files in UTF-8, Latin-1, Shift-JIS, CP1251, etc.). If parsing fails or
the result looks malformed, falls back to Ollama to repair the file.

Public entrypoint: parse_cue(cue_path, audio_duration_seconds, ollama_client)
Returns a Cue dataclass with the disc-level fields and a list of Track.
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import chardet

from ollama_client import UNAVAILABLE

logger = logging.getLogger(__name__)


class LLMUnavailableError(ValueError):
    """The CUE needs an LLM repair and the LLM could not be asked. Not a
    verdict on the CUE: the caller must retry it once the LLM can answer."""


# --- Data shapes --------------------------------------------------------


@dataclass
class Track:
    number: int
    title: str = ""
    performer: str = ""
    start_seconds: float = 0.0
    end_seconds: Optional[float] = None  # None = until EOF
    isrc: str = ""
    # Which FILE of the sheet the track's INDEX 01 sits in (0-based), and --
    # once the orchestrator has resolved it -- that image on disk. Only a
    # multi-image sheet needs either.
    file_index: int = 0
    source: Optional[Path] = None


@dataclass
class Cue:
    performer: str = ""          # album artist
    title: str = ""              # album title
    date: str = ""
    genre: str = ""
    comment: str = ""
    audio_files: List[str] = field(default_factory=list)  # all FILE refs, in order
    tracks: List[Track] = field(default_factory=list)

    @property
    def audio_file(self) -> str:
        """First FILE reference (backwards-compat helper)."""
        return self.audio_files[0] if self.audio_files else ""

    @property
    def is_multi_image(self) -> bool:
        """Several FILEs, each holding two or more tracks: one sheet for both
        sides of a vinyl rip (Ray Parker Jr. - After Dark: FILE '(Side 1)'
        with tracks 1-5, FILE '(Side 2)' with 6-10, each side timed from
        00:00). An EAC per-track sheet has one track per FILE (its pregap
        may sit at the end of the previous FILE) and is not one."""
        if len(self.audio_files) < 2 or not self.tracks:
            return False
        per: dict = {}
        for t in self.tracks:
            per[t.file_index] = per.get(t.file_index, 0) + 1
        return (set(per) == set(range(len(self.audio_files)))
                and all(n >= 2 for n in per.values()))

    def is_valid(self) -> bool:
        """
        Structural validity only: did the parser understand enough of the
        CUE to know what it contains? A multi-FILE CUE is structurally
        valid even though its per-track end/start times overlap at 0 --
        that's the orchestrator's is_disc_image() gate to worry about.
        """
        if not self.tracks:
            return False
        if not self.title:
            return False
        # Track starts must be monotonically non-decreasing WITHIN each FILE.
        # Multi-FILE CUEs legitimately have all starts at 0, and a
        # multi-image sheet starts each image again at 0.
        last: dict = {}
        for t in self.tracks:
            if t.start_seconds < last.get(t.file_index, -1.0):
                return False
            last[t.file_index] = t.start_seconds
        return True

    def is_disc_image(self, audio_duration: Optional[float] = None) -> tuple[bool, str]:
        """
        Return (True, '') if this CUE describes ONE big audio file with
        multiple tracks at strictly increasing positions (i.e. a real
        disc image -- the only thing we want to split). Otherwise
        return (False, reason).

        Multi-FILE CUEs (one FILE per track, already-split audio) and
        single-track CUEs (per-song) are rejected here so the pipeline
        leaves the files completely alone.

        If `audio_duration` is provided, also reject cases where the
        companion audio is too short to contain the tracks the CUE
        describes (catches the "CUE for full disc but audio is just
        track 1" scenario).
        """
        if not self.audio_files:
            return False, "CUE has no FILE reference"
        if self.is_multi_image:
            # Each image is a disc image of its own: positions strictly
            # increasing inside it. (No length check: `audio_duration` is
            # the length of one image.)
            prev: dict = {}
            for t in self.tracks:
                if t.start_seconds <= prev.get(t.file_index, -1.0):
                    return False, (
                        f"Track {t.number} start ({t.start_seconds:.3f}s) is "
                        f"not after the previous track of its FILE -- corrupt")
                prev[t.file_index] = t.start_seconds
            return True, ""
        if len(self.audio_files) > 1:
            return False, (
                f"CUE references {len(self.audio_files)} separate audio files "
                f"(already-split tracks, not a disc image)"
            )
        # If any FILE reference contains a path separator, the CUE is pointing
        # at audio in a subfolder -- typically the hallmark of a pre-split
        # album described by a .cue TOC. Not a disc image.
        for f in self.audio_files:
            if "/" in f or "\\" in f:
                return False, (
                    f"CUE FILE reference '{f}' contains a path separator "
                    f"(points into a subfolder -- pre-split album)"
                )
        if len(self.tracks) < 2:
            return False, (
                f"CUE has only {len(self.tracks)} track(s) -- "
                f"looks like a single-song CUE"
            )
        # One FILE, 2+ tracks: positions must be strictly increasing.
        prev = -1.0
        for t in self.tracks:
            if t.start_seconds <= prev:
                return False, (
                    f"Track {t.number} start ({t.start_seconds:.3f}s) is not "
                    f"after the previous track ({prev:.3f}s) -- CUE points to "
                    f"already-split audio or is corrupt"
                )
            prev = t.start_seconds
        # Duration sanity: if we know the audio length and the last track's
        # start is past the end of the file, the audio is not the full disc
        # image the CUE describes (e.g. someone left a 4-minute track 1
        # behind but the CUE still describes a full 55-minute disc).
        if audio_duration is not None and self.tracks:
            last = self.tracks[-1]
            # Give 1 second of slack for rounding.
            if last.start_seconds + 1.0 > audio_duration:
                return False, (
                    f"Audio is {audio_duration:.1f}s but CUE's last track "
                    f"starts at {last.start_seconds:.1f}s -- audio doesn't "
                    f"match the CUE (likely already-split or truncated)"
                )
        return True, ""


# --- Encoding detection -------------------------------------------------


# The legacy encodings a CUE that is not UTF-8 may be in. This order is
# only the LAST tie-break (see _choose_decoding). It used to be the whole
# rule -- "the first that decodes wins" -- and cp1251 decodes every byte but
# 0x98, so after UTF-8 it almost always won: 'Tiempo De Soleá' became
# 'Tiempo De Soleб' and 'Calé Barí' 'Calй Barн' in the split file names and
# tags that ManualImport put in the library (Ojos De Brujo, 29 Sep); a
# Shift-JIS 'ドラゴンの歌' became 'ѓhѓ‰ѓSѓ“‚М‰М'.
_ENCODINGS_TO_TRY = [
    "cp1251",        # Cyrillic Windows
    "cp1252",        # Western Windows
    "shift_jis",
    "cp932",         # Windows Shift-JIS (NEC/IBM extensions)
    "gb18030",       # superset of GB2312/GBK
    "big5",
    "euc_kr",
    "iso-8859-1",    # decodes anything; C1 controls make it lose to cp1252
]

# Punctuation that really does sit between two letters of one word:
# apostrophes (Don't, Don´t), the Catalan middle dot (col·lecció), dashes.
_INNER_PUNCT_OK = set("\u2019\u2018\u00b4\u02bc\u00b7\u2010\u2011\u2013\u2014")

_CJK_NAMES = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "HALFWIDTH",
              "FULLWIDTH", "BOPOMOFO", "IDEOGRAPHIC")

# Words are split at white space and ASCII punctuation (not the apostrophe),
# so 'крови.flac' in a FILE line is two words, not one mixing two scripts.
_WORD_SPLIT = re.compile(r"[\s\x21-\x26\x28-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]+")


def _script(ch: str) -> str:
    """The writing system of a letter, from its Unicode name: 'Latin',
    'CJK' (Han, kana, Hangul, full/half-width forms), or the name's first
    word (CYRILLIC, GREEK, ARABIC, ...). '' for letters that belong to no
    script in particular (modifier letters, the micro sign)."""
    word = unicodedata.name(ch, "").split(" ", 1)[0]
    if word in ("LATIN", "MASCULINE", "FEMININE"):
        return "Latin"
    if word in _CJK_NAMES:
        return "CJK"
    if word in ("MODIFIER", "MICRO", ""):
        return ""
    return word


def _cue_values(text: str) -> List[str]:
    """The human-written part of each line: the line without its CUE
    keyword (and without the REM field name). Keywords are ASCII in every
    encoding, so they would only dilute the per-line script checks."""
    out: List[str] = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        rest = parts[1]
        if parts[0].upper() == "REM":
            sub = rest.split(None, 1)
            rest = sub[1] if len(sub) > 1 else ""
        if rest.strip():
            out.append(rest)
    return out


def _implausibility(text: str) -> int:
    """How many things in `text` real writing does not do. A wrong 8-bit
    decode is recognisable by itself: cp1251 read of Western text puts a
    Cyrillic letter inside a Latin word ('Beyoncй'); a cp1252 read of
    Cyrillic gives words made only of accented letters ('Èâàíîâà') or with
    a symbol inside ('Àë¸íà'); a Shift-JIS read as cp1251 mixes scripts and
    symbols ('ѓhѓ‰ѓSѓ“'); a multi-byte read of Western text glues one
    ideograph onto a Latin word. Counted once per word and rule."""
    bad = 0
    # Kana or kanji anywhere: the text reads as Japanese (see rule 6).
    kana_kanji = any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff"
                     for c in text)
    for ch in text:
        if ch in "\t\r\n":
            continue
        if unicodedata.category(ch) in ("Cc", "Co", "Cn", "Cs"):
            bad += 1                      # controls, private use, unassigned
    for value in _cue_values(text):
        words = []
        for tok in _WORD_SPLIT.split(value):
            # Trim quotes/brackets/digits around the word, keep what is inside.
            i, j = 0, len(tok)
            while i < j and not tok[i].isalpha():
                i += 1
            while j > i and not tok[j - 1].isalpha():
                j -= 1
            core = tok[i:j]
            letters = [c for c in core if c.isalpha()]
            if not letters:
                continue
            scripts = {_script(c) for c in letters} - {""}
            words.append((len(letters), scripts))
            if not any(ord(c) > 127 for c in core):
                continue
            # 1. One word, two scripts. Latin next to CJK is normal ("CDの").
            if len(scripts) > 1 and not scripts <= {"Latin", "CJK"}:
                bad += 1
            # 2. A lone ideograph glued onto Latin letters: a multi-byte codec
            #    swallowed an accented letter and the ASCII letter after it.
            if "CJK" in scripts and "Latin" in scripts:
                kinds = [_script(c) if c.isalpha() else "" for c in core]
                for k in range(len(kinds)):
                    if kinds[k] != "CJK":
                        continue
                    if (k > 0 and kinds[k - 1] == "CJK") or \
                            (k + 1 < len(kinds) and kinds[k + 1] == "CJK"):
                        continue
                    if (k > 0 and kinds[k - 1] == "Latin") or \
                            (k + 1 < len(kinds) and kinds[k + 1] == "Latin"):
                        bad += 1
                        break
            # 3. Symbols between two letters of one word: 'Àë¸íà', 'ѓ‰ѓ', and
            #    'Queen¡¯s' -- a GBK apostrophe (A1 AF) read as cp1252, in the
            #    Game of Thrones Season 7 CUE in the library.
            k = 1
            while k < len(core) - 1:
                m = k
                while m < len(core) - 1 and ord(core[m]) > 127 \
                        and not core[m].isalnum() and core[m] not in _INNER_PUNCT_OK \
                        and not unicodedata.category(core[m]).startswith(("M", "Cf")):
                    m += 1
                if m > k and core[k - 1].isalpha() and core[m].isalpha():
                    bad += 1
                    break
                k = m + 1
            # 4. A Latin word made (almost) only of accented letters: what an
            #    8-bit Cyrillic, Greek or CJK text looks like read as cp1252.
            #    Western words are mostly plain letters ('été', 'Ñu', 'poème').
            if scripts == {"Latin"}:
                accented = sum(1 for c in letters if ord(c) > 127)
                n = len(letters)
                if (n >= 2 and accented == n) or (n >= 4 and 2 * accented > n):
                    bad += 1
            # 5. An accented capital straight after a small letter: KOI8-R
            #    read as cp1251 (or the reverse) swaps the case of every word.
            for k in range(1, len(core)):
                if ord(core[k]) > 127 and core[k].isupper() and core[k - 1].islower():
                    bad += 1
                    break
            # 6. Half-width katakana with no kana or kanji anywhere. cp1251
            #    capitals (0xC0-0xDF) ARE Shift-JIS half-width katakana byte
            #    for byte, so a Russian title in capitals ('КИНО') reads as
            #    half-width katakana and nothing else ('ﾊﾈﾍﾎ'). Real Japanese
            #    that uses half-width katakana has kanji or kana beside it.
            if not kana_kanji and any("\uff61" <= c <= "\uff9f" for c in core):
                bad += 1
        # 7. A lone Cyrillic/Greek/... letter among Latin words: 'Voyage à
        #    Paris' read as cp1251 is 'Voyage а Paris'. A Russian one-letter
        #    word (в, и, а) only ever stands between Russian words.
        for idx, (n, scripts) in enumerate(words):
            if n != 1 or len(scripts) != 1 or scripts & {"Latin", "CJK"}:
                continue
            others = [s for k, (_n, s) in enumerate(words) if k != idx]
            if len(others) >= 2 and all(s == {"Latin"} for s in others):
                bad += 1
    return bad


def _letter_bigrams(text: str) -> set:
    """Adjacent character pairs that contain a non-ASCII letter, NFC and
    case-folded: what a correct decode shares with the names on disk."""
    t = unicodedata.normalize("NFC", text).casefold()
    return {t[k:k + 2] for k in range(len(t) - 1)
            if not t[k].isspace() and not t[k + 1].isspace()
            and ((ord(t[k]) > 127 and t[k].isalpha())
                 or (ord(t[k + 1]) > 127 and t[k + 1].isalpha()))}


_TAG_AUDIO_EXTS = {".flac", ".ape", ".wv", ".wav", ".tta", ".m4a", ".mp3",
                   ".ogg", ".opus", ".dsf", ".aiff", ".aif", ".wma"}


def _disk_evidence(cue_path: Path) -> str:
    """Text the filesystem and the companion audio already hold as real
    Unicode: the .cue's own name, its folder and the folder above, every
    name in the folder, and the tags of (up to three of) its audio files.
    A correct decode of the CUE usually repeats some of it."""
    parts = [cue_path.name, cue_path.parent.name, cue_path.parent.parent.name]
    try:
        names = sorted(os.listdir(cue_path.parent))[:500]
    except OSError:
        names = []
    parts.extend(names)
    audio = [n for n in names if os.path.splitext(n)[1].lower() in _TAG_AUDIO_EXTS][:3]
    if audio:
        try:
            import audio_open            # mutagen: absent in a bare checkout
        except Exception:  # noqa: BLE001
            audio = []
        for n in audio:
            try:
                f = audio_open.File(str(cue_path.parent / n))
                tags = getattr(f, "tags", None)
                items = list(tags.items()) if tags is not None else []
            except Exception:  # noqa: BLE001
                continue
            for _key, val in items:
                vals = getattr(val, "text", val)          # ID3 frames
                if not isinstance(vals, (list, tuple)):
                    vals = [vals]
                for v in vals:
                    if not isinstance(v, str) and getattr(v, "kind", None) == 0:
                        v = str(v)                          # APEv2 text value
                    if isinstance(v, str) and len(v) < 4096:
                        parts.append(v)
    return "\n".join(parts)


def _choose_decoding(raw: bytes, cue_path: Optional[Path]) -> Tuple[str, str, str]:
    """Decode a CUE that is not UTF-8. Every candidate codec that decodes
    the bytes is judged, in this order, and the first rule that separates
    them decides:
      1. fewest implausible words (_implausibility) -- a wrong 8-bit decode
         gives itself away;
      2. most letter pairs shared with the names and tags on disk -- decides
         between readings that all look like real text (Shift-JIS vs GBK vs
         Big5, where a wrong reading is just other ideographs);
      3. charset detection, asked to choose among the tied codecs only
         (unrestricted, chardet 7 answers cp1250 'sr' for Western, Cyrillic,
         GBK, Big5 and EUC-KR alike: all 19 non-UTF-8 CUEs in the library);
      4. the order of _ENCODINGS_TO_TRY.
    Returns (text, codec, why)."""
    cands: List[Tuple[str, str]] = []           # (codec, text), distinct texts
    for enc in _ENCODINGS_TO_TRY:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if all(text != t for _e, t in cands):
            cands.append((enc, text))
    if not cands:
        return raw.decode("utf-8", errors="replace"), "utf-8", "undecodable"
    scored = [(_implausibility(t), e, t) for e, t in cands]
    low = min(s for s, _e, _t in scored)
    tied = [(e, t) for s, e, t in scored if s == low]
    why = "; ".join("%s=%d" % (e, s) for s, e, _t in scored)
    if len(tied) == 1:
        return tied[0][1], tied[0][0], "implausible words " + why
    if cue_path is not None:
        disk = _letter_bigrams(_disk_evidence(cue_path))
        if disk:
            hits = [(len(_letter_bigrams(t) & disk), e, t) for e, t in tied]
            best = max(h for h, _e, _t in hits)
            if best > 0:
                tied = [(e, t) for h, e, t in hits if h == best]
                if len(tied) == 1:
                    return tied[0][1], tied[0][0], "matches the names on disk; " + why
    try:
        try:
            # cp1252 is chardet's no-match answer; excluding it makes chardet
            # warn and answer None. A cp1252 answer outside `tied` is no vote.
            guess = chardet.detect(
                raw, include_encodings=[e for e, _t in tied] + ["cp1252"])
        except TypeError:                        # chardet < 7: no restriction
            guess = chardet.detect(raw)
        name = (guess or {}).get("encoding")
        if name:
            gtext = raw.decode(codecs.lookup(name).name)
            for e, t in tied:
                if t == gtext:
                    return t, e, "charset detection; " + why
    except Exception:  # noqa: BLE001 -- unknown codec, undecodable guess
        pass
    return tied[0][1], tied[0][0], "first plausible; " + why


def read_cue_text(cue_path: Path) -> Tuple[str, str, str]:
    """The text of a .cue and the codec it was read with: UTF-8 (with or
    without BOM), UTF-16 by its BOM, else the most plausible legacy codec
    (_choose_decoding). Returns (text, codec, why). The one decoder for
    every reader of a .cue, so the parser, the companion-audio lookup and
    the FILE-reference repair all see the same text."""
    raw = cue_path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16"), "utf-16", "BOM"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc, ""
        except UnicodeDecodeError:
            continue
    return _choose_decoding(raw, cue_path)


def _read_cue_text(cue_path: Path) -> str:
    text, enc, why = read_cue_text(cue_path)
    if why:
        logger.info("CUE %s is not UTF-8: read as %s (%s)", cue_path.name, enc, why)
    return text


# --- Deterministic parser ----------------------------------------------


_INDEX_RE = re.compile(r"INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", re.IGNORECASE)
_QUOTED_RE = re.compile(r'"([^"]*)"')


def _msf_to_seconds(mm: int, ss: int, ff: int) -> float:
    """CUE frames are 1/75 second."""
    return mm * 60 + ss + ff / 75.0


def _strip_value(line: str, keyword: str) -> str:
    body = line.strip()[len(keyword):].strip()
    m = _QUOTED_RE.search(body)
    return m.group(1) if m else body.strip().strip('"')


def parse_cue_text(text: str) -> Cue:
    cue = Cue()
    current: Optional[Track] = None
    in_track_block = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("FILE "):
            # FILE "source.flac" WAVE
            m = _QUOTED_RE.search(line)
            if m:
                cue.audio_files.append(m.group(1))
            continue

        if upper.startswith("TRACK "):
            # TRACK 01 AUDIO
            parts = line.split()
            try:
                num = int(parts[1])
            except (IndexError, ValueError):
                continue
            current = Track(number=num)
            cue.tracks.append(current)
            in_track_block = True
            continue

        if upper.startswith("TITLE "):
            value = _strip_value(line, line[:5])
            if in_track_block and current is not None:
                current.title = value
            else:
                cue.title = value
            continue

        if upper.startswith("PERFORMER "):
            value = _strip_value(line, line[:9])
            if in_track_block and current is not None:
                current.performer = value
            else:
                cue.performer = value
            continue

        if upper.startswith("ISRC "):
            if current is not None:
                current.isrc = line.split(None, 1)[1].strip()
            continue

        if upper.startswith("REM "):
            body = line[4:].strip()
            if body.upper().startswith("DATE "):
                cue.date = body.split(None, 1)[1].strip().strip('"')
            elif body.upper().startswith("GENRE "):
                cue.genre = body.split(None, 1)[1].strip().strip('"')
            elif body.upper().startswith("COMMENT "):
                cue.comment = body.split(None, 1)[1].strip().strip('"')
            continue

        if upper.startswith("INDEX "):
            m = _INDEX_RE.search(line)
            if not m or current is None:
                continue
            idx_num = int(m.group(1))
            secs = _msf_to_seconds(int(m.group(2)), int(m.group(3)), int(m.group(4)))
            # INDEX 01 is the track start; INDEX 00 is the pregap (ignore).
            if idx_num == 1:
                current.start_seconds = secs
                current.file_index = max(0, len(cue.audio_files) - 1)
            continue

    return cue


def _fill_end_times(cue: Cue, audio_duration_seconds: Optional[float]) -> None:
    # A multi-image sheet: a track ends where the next one of ITS image
    # starts, and the last of each image at that image's end (None = EOF;
    # the given duration is one image's).
    multi = cue.is_multi_image
    for i, track in enumerate(cue.tracks):
        nxt = cue.tracks[i + 1] if i + 1 < len(cue.tracks) else None
        if nxt is not None and (not multi or nxt.file_index == track.file_index):
            track.end_seconds = nxt.start_seconds
        elif nxt is None and not multi:
            track.end_seconds = audio_duration_seconds  # may be None
        else:
            track.end_seconds = None


def _promote_album_fields(cue: Cue) -> None:
    """If track-level performer is set but album-level is not, copy one up."""
    if not cue.performer and cue.tracks:
        # Pick the most common track performer.
        performers = [t.performer for t in cue.tracks if t.performer]
        if performers:
            cue.performer = max(set(performers), key=performers.count)


def _fallback_identity_from_folder(cue: Cue, cue_path: Path) -> None:
    """
    Many disc-image CUEs -- DTS-CD rips especially -- carry per-track TITLEs
    but NO album-level TITLE or PERFORMER. That's a perfectly splittable cue,
    but is_valid() needs a title. Derive the missing album/artist from the
    containing folder name ('[DTSCD][UP] Elton John - The Big Picture' ->
    artist 'Elton John', album 'The Big Picture'), stripping [tag]/(year)
    noise. Only fills fields the CUE left empty.
    """
    if cue.title and cue.performer:
        return
    name = re.sub(r"[(\[\{][^)\]\}]*[)\]\}]", " ", cue_path.parent.name or "")
    name = re.sub(r"\s{2,}", " ", name).strip(" -_.")
    if not name:
        return
    artist, album = "", name
    if " - " in name:
        artist, album = (p.strip(" -_.") for p in name.split(" - ", 1))
    if not cue.title and album:
        cue.title = album
    if not cue.performer and artist:
        cue.performer = artist


# --- Public entrypoint --------------------------------------------------


def parse_cue(
    cue_path: Path,
    audio_duration_seconds: Optional[float],
    ollama=None,
) -> Cue:
    """
    Parse a .cue file. If the deterministic parser fails, fall back to Ollama.

    Parameters
    ----------
    cue_path : Path
    audio_duration_seconds : duration of the companion audio file (sec), if known.
        Used to fill in the end time of the last track.
    ollama : optional OllamaClient; if provided and parse fails, we ask it to repair.
    """
    text = _read_cue_text(cue_path)
    cue = parse_cue_text(text)
    _fill_end_times(cue, audio_duration_seconds)
    _promote_album_fields(cue)
    _fallback_identity_from_folder(cue, cue_path)

    if cue.is_valid():
        logger.info(
            "Parsed %s deterministically: %d tracks",
            cue_path.name,
            len(cue.tracks),
        )
        return cue

    logger.warning("Deterministic parse of %s failed or looks incomplete", cue_path.name)

    if ollama is None:
        raise ValueError(f"Could not parse CUE file {cue_path} and no Ollama fallback")

    repaired_text = ollama.repair_cue(text)
    if not repaired_text:
        if repaired_text is UNAVAILABLE:
            raise LLMUnavailableError(
                f"CUE {cue_path} needs the LLM to repair it and the LLM cannot "
                f"be asked right now")
        raise ValueError(f"Ollama returned empty repair for {cue_path}")

    cue = parse_cue_text(repaired_text)
    _fill_end_times(cue, audio_duration_seconds)
    _promote_album_fields(cue)
    _fallback_identity_from_folder(cue, cue_path)

    if not cue.is_valid():
        raise ValueError(f"Even after Ollama repair, CUE {cue_path} is invalid")

    logger.info(
        "Parsed %s via Ollama repair: %d tracks",
        cue_path.name,
        len(cue.tracks),
    )
    return cue
