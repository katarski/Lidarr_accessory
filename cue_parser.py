"""
CUE sheet parser.

Tries deterministic parsing first (handles the 95% case: properly formatted
.cue files in UTF-8, Latin-1, Shift-JIS, CP1251, etc.). If parsing fails or
the result looks malformed, falls back to Ollama to repair the file.

Public entrypoint: parse_cue(cue_path, audio_duration_seconds, ollama_client)
Returns a Cue dataclass with the disc-level fields and a list of Track.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import chardet

logger = logging.getLogger(__name__)


# --- Data shapes --------------------------------------------------------


@dataclass
class Track:
    number: int
    title: str = ""
    performer: str = ""
    start_seconds: float = 0.0
    end_seconds: Optional[float] = None  # None = until EOF
    isrc: str = ""


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
        # Track starts must be monotonically non-decreasing. Multi-FILE
        # CUEs legitimately have all starts at 0; that's fine here.
        last = -1.0
        for t in self.tracks:
            if t.start_seconds < last:
                return False
            last = t.start_seconds
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


_ENCODINGS_TO_TRY = [
    "utf-8-sig",
    "utf-8",
    "cp1251",        # Cyrillic Windows
    "cp1252",        # Western Windows
    "shift_jis",
    "gb18030",
    "iso-8859-1",
]


def _read_cue_text(cue_path: Path) -> str:
    raw = cue_path.read_bytes()
    # First, try the common encodings in order; they usually win.
    for enc in _ENCODINGS_TO_TRY:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort: chardet guess.
    guess = chardet.detect(raw)
    if guess and guess.get("encoding"):
        try:
            return raw.decode(guess["encoding"], errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


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
            continue

    return cue


def _fill_end_times(cue: Cue, audio_duration_seconds: Optional[float]) -> None:
    for i, track in enumerate(cue.tracks):
        if i + 1 < len(cue.tracks):
            track.end_seconds = cue.tracks[i + 1].start_seconds
        else:
            track.end_seconds = audio_duration_seconds  # may be None


def _promote_album_fields(cue: Cue) -> None:
    """If track-level performer is set but album-level is not, copy one up."""
    if not cue.performer and cue.tracks:
        # Pick the most common track performer.
        performers = [t.performer for t in cue.tracks if t.performer]
        if performers:
            cue.performer = max(set(performers), key=performers.count)


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
        raise ValueError(f"Ollama returned empty repair for {cue_path}")

    cue = parse_cue_text(repaired_text)
    _fill_end_times(cue, audio_duration_seconds)
    _promote_album_fields(cue)

    if not cue.is_valid():
        raise ValueError(f"Even after Ollama repair, CUE {cue_path} is invalid")

    logger.info(
        "Parsed %s via Ollama repair: %d tracks",
        cue_path.name,
        len(cue.tracks),
    )
    return cue
