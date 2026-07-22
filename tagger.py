"""
Vorbis-comment tagger for split FLACs.

Writes album/track metadata derived from the CUE onto each output file so
Lidarr can match the release cleanly. Optionally asks Ollama to normalize
artist/title strings before tagging.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from mutagen import MutagenError
from mutagen.flac import FLAC

from cue_parser import Cue
from splitter import SplitResult

logger = logging.getLogger(__name__)


@dataclass
class TagPlan:
    """What we're about to write, per track. Exposed so Ollama can rewrite it."""
    tracknumber: str
    tracktotal: str
    title: str
    artist: str
    albumartist: str
    album: str
    date: str
    genre: str
    comment: str
    isrc: str


def _build_plans(cue: Cue, splits: List[SplitResult]) -> List[TagPlan]:
    total = len(splits)
    album_artist = cue.performer or (splits[0].track.performer if splits else "")
    album = cue.title

    plans: List[TagPlan] = []
    for s in splits:
        t = s.track
        plans.append(
            TagPlan(
                tracknumber=str(t.number),
                tracktotal=str(total),
                title=t.title or f"Track {t.number:02d}",
                artist=t.performer or album_artist,
                albumartist=album_artist,
                album=album,
                date=cue.date,
                genre=cue.genre,
                comment=cue.comment,
                isrc=t.isrc,
            )
        )
    return plans


def _open_flac_with_retry(
    flac_path: Path, attempts: int = 8, initial_delay: float = 0.1
) -> FLAC:
    """
    Open a FLAC with exponential backoff. Tolerates the brief window on
    SMB shares where a fresh file can raise FileNotFoundError even
    though the write has completed, and guards against AV software
    holding an open handle for a moment post-write.
    """
    delay = initial_delay
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return FLAC(str(flac_path))
        except (FileNotFoundError, MutagenError, OSError) as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay)
            delay = min(delay * 1.7, 1.5)
    assert last_exc is not None
    raise last_exc


def _apply(flac_path: Path, plan: TagPlan) -> None:
    audio = _open_flac_with_retry(flac_path)
    audio.delete()  # wipe anything that survived -map_metadata -1

    def set_if(key: str, value: str) -> None:
        if value:
            audio[key] = value

    set_if("tracknumber", plan.tracknumber)
    set_if("tracktotal", plan.tracktotal)
    set_if("totaltracks", plan.tracktotal)  # some apps read this variant
    set_if("title", plan.title)
    set_if("artist", plan.artist)
    set_if("albumartist", plan.albumartist)
    set_if("album", plan.album)
    set_if("date", plan.date)
    set_if("genre", plan.genre)
    set_if("comment", plan.comment)
    set_if("isrc", plan.isrc)
    audio.save()


def tag_splits(
    cue: Cue,
    splits: List[SplitResult],
    ollama=None,
) -> List[TagPlan]:
    """
    Tag every split file. If `ollama` is given, it may normalize the plan
    (cleaner capitalization, remove junk like "[320k]", unify featured artists).
    Returns the plans actually written -- useful for downstream folder naming.
    """
    plans = _build_plans(cue, splits)

    if ollama is not None:
        try:
            plans = ollama.normalize_tags(plans) or plans
        except Exception as exc:  # never let LLM failure break tagging
            logger.warning("Ollama tag normalization failed: %s", exc)

    for split, plan in zip(splits, plans):
        try:
            _apply(split.output_path, plan)
        except (FileNotFoundError, MutagenError, OSError) as exc:
            # One split file vanished/unreadable (SMB drop, AV, or an
            # external process moved it) between split and tag. Don't let
            # it crash the whole album -- skip it and carry on. The album
            # will be incomplete, and the source-cleanup gate
            # (_tracks_present_in_library) will refuse to delete the source
            # because fewer files than expected land in the library.
            logger.error(
                "Skipping tag for %s -- split file missing/unreadable; "
                "album will be incomplete and source preserved: %s",
                split.output_path.name, exc,
            )
            continue
        logger.info(
            "Tagged %s: %s - %s / %s",
            split.output_path.name,
            plan.artist,
            plan.title,
            plan.album,
        )

    return plans


_FOLDER_SANITIZE_RE = None  # lazy import


def album_folder_name(
    plans: List[TagPlan],
    template: str = "{album} ({year})",
) -> str:
    """
    Build a folder name from a template.

    Placeholders: {album}, {year}, {artist}, {albumartist}.
    Missing values are substituted with an empty string. Bracketed or
    parenthesised groups that would be left empty (e.g. "Album ()")
    are cleaned up so the output stays tidy.

    Default "{album} ({year})" -> "Forever Blue (1995)".
    """
    global _FOLDER_SANITIZE_RE
    import re
    if _FOLDER_SANITIZE_RE is None:
        _FOLDER_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    if not plans:
        return "Unknown Album"
    p = plans[0]
    fields = {
        "album": p.album or "",
        "artist": p.artist or "",
        "albumartist": p.albumartist or p.artist or "",
        "year": (p.date or "").strip().split("-")[0].strip(),
    }
    try:
        name = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        # Fall back to a sane default if the user typo'd the template.
        name = f"{fields['album']} ({fields['year']})" if fields["year"] else fields["album"]

    # Tidy up empty brackets/parens left when a placeholder was blank.
    # "Forever Blue ()"   -> "Forever Blue"
    # "Forever Blue [ - ]"-> "Forever Blue"
    name = re.sub(r"\s*[\(\[]\s*[-\s,]*\s*[\)\]]", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -_.,")

    # Windows-invalid chars.
    name = _FOLDER_SANITIZE_RE.sub("_", name).strip().rstrip(". ")
    return name or (p.album or "Unknown Album")
