"""
ffmpeg-driven splitter.

Takes a parsed Cue + source audio file, emits one FLAC per track into a
staging folder. Lossless: FLAC -> FLAC is a re-encode but the codec is
lossless. APE/WV are decoded and re-encoded to FLAC.

No shntool involvement anywhere -- ffmpeg handles FLAC, APE, WV, WAV natively.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from cue_parser import Cue, Track

logger = logging.getLogger(__name__)


@dataclass
class SplitResult:
    track: Track
    output_path: Path


def probe_duration(ffmpeg_binary: str, audio_path: Path) -> Optional[float]:
    """Return audio duration in seconds via ffprobe (assumed next to ffmpeg)."""
    ffprobe = str(Path(ffmpeg_binary).with_name("ffprobe"))
    if not shutil.which(ffprobe) and ffmpeg_binary != "ffmpeg":
        ffprobe = "ffprobe"
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout or "{}")
        dur = payload.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, KeyError) as exc:
        logger.warning("ffprobe failed for %s: %s", audio_path, exc)
        return None


# Block signatures for the container formats that carry their audio in a
# self-synchronising block stream. Some rips prepend junk (a fake header,
# a release-group watermark) before the first block -- tolerant decoders
# (libwavpack/libmac, i.e. real media players) scan to the first block, but
# ffmpeg demands the signature at byte 0 and rejects the file ("Invalid data").
_LEADIN_MAGIC = {".wv": b"wvpk", ".ape": b"MAC "}


def repair_leadin(audio_path: Path, ffmpeg_binary: str = "ffmpeg") -> Optional[Path]:
    """
    If `audio_path` is a WavPack/APE file whose block signature isn't at offset
    0 (junk prepended), write a trimmed copy that starts at the first signature
    and return it IF ffmpeg can then decode it. Returns None when no repair
    applies (not WV/APE, signature already at 0, not found, or still unreadable
    after trimming). The trimmed copy is a sibling named '<name>.cuefix.<ext>'.
    """
    magic = _LEADIN_MAGIC.get(audio_path.suffix.lower())
    if not magic:
        return None
    try:
        with open(audio_path, "rb") as fh:
            head = fh.read(8_000_000)  # junk lead-ins are tiny; 8 MB is ample
        off = head.find(magic)
        if off <= 0:
            return None  # already at start, or no signature in the head
        out = audio_path.with_name(audio_path.stem + ".cuefix" + audio_path.suffix)
        with open(audio_path, "rb") as src, open(out, "wb") as dst:
            src.seek(off)
            shutil.copyfileobj(src, dst, length=4_000_000)
        dur = probe_duration(ffmpeg_binary, out)
        if dur and dur > 1.0:
            logger.info(
                "repair_leadin: trimmed %d junk byte(s) before the %s stream in "
                "%s -> now decodable (%.0fs)",
                off, audio_path.suffix, audio_path.name, dur,
            )
            return out
        try:
            out.unlink()
        except OSError:
            pass
        logger.warning("repair_leadin: trimmed %s but ffmpeg still can't read it",
                       audio_path.name)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair_leadin failed for %s: %s", audio_path, exc)
        return None


_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str, fallback: str = "track") -> str:
    cleaned = _INVALID_FS.sub("_", name).strip().rstrip(".")
    return cleaned or fallback


def _output_name(
    cue: Cue, track: Track, template: Optional[str] = None
) -> str:
    title = _sanitize(track.title or f"Track {track.number:02d}")
    if template:
        try:
            return _sanitize(
                template.format(
                    artist=_sanitize(track.performer or cue.performer or ""),
                    album=_sanitize(cue.title or ""),
                    albumartist=_sanitize(cue.performer or track.performer or ""),
                    number=track.number,
                    title=title,
                    ext="flac",
                ),
                fallback=f"{track.number:02d} - {title}.flac",
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("filename_template error %r, using default: %s", template, exc)
    return f"{track.number:02d} - {title}.flac"


def split_cue(
    cue: Cue,
    audio_path: Path,
    staging_dir: Path,
    ffmpeg_binary: str = "ffmpeg",
    flac_compression_level: int = 8,
    extra_args: Optional[List[str]] = None,
    filename_template: Optional[str] = None,
) -> List[SplitResult]:
    """
    Split `audio_path` into per-track FLAC files using the timings in `cue`.
    Files are written into `staging_dir` which will be created if missing.
    """
    extra_args = extra_args or []
    staging_dir.mkdir(parents=True, exist_ok=True)

    results: List[SplitResult] = []

    for track in cue.tracks:
        out_name = _output_name(cue, track, template=filename_template)
        out_path = staging_dir / out_name

        # Defensive: refuse to call ffmpeg with a non-progressing range.
        if (
            track.end_seconds is not None
            and track.end_seconds <= track.start_seconds + 0.001
        ):
            raise ValueError(
                f"Track {track.number}: end ({track.end_seconds:.3f}) is not "
                f"after start ({track.start_seconds:.3f}). CUE is malformed or "
                f"points to already-split audio -- this should have been "
                f"caught by is_disc_image()."
            )

        cmd: List[str] = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel", "warning",
            "-y",
            "-i", str(audio_path),
            "-ss", f"{track.start_seconds:.6f}",
        ]
        if track.end_seconds is not None:
            cmd += ["-to", f"{track.end_seconds:.6f}"]

        # Lossless FLAC encode. Do NOT set -sample_fmt -- preserve source bit depth.
        cmd += [
            "-map", "0:a:0",
            "-c:a", "flac",
            "-compression_level", str(flac_compression_level),
            # Strip container metadata; tagger.py writes clean tags afterwards.
            "-map_metadata", "-1",
            "-vn",
        ]
        cmd += extra_args
        cmd.append(str(out_path))

        logger.info(
            "Splitting track %02d: %.3fs -> %s -> %s",
            track.number,
            track.start_seconds,
            f"{track.end_seconds:.3f}" if track.end_seconds else "EOF",
            out_path.name,
        )

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            logger.error(
                "ffmpeg failed for track %s (%s):\n%s",
                track.number,
                out_name,
                exc.stderr,
            )
            # Clean up partial file if present.
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            raise

        # ffmpeg exited 0 -- but on SMB shares the directory listing can lag
        # behind the write for a few hundred ms. If we hand the Path to
        # mutagen before the share catches up, we get a spurious
        # FileNotFoundError. Poll briefly to let the share settle before
        # declaring success.
        _wait_for_write(out_path, staging_dir)

        results.append(SplitResult(track=track, output_path=out_path))

    return results


def _wait_for_write(
    out_path: Path,
    staging_dir: Path,
    timeout: float = 10.0,
    initial_delay: float = 0.05,
) -> None:
    """
    Block until `out_path` is visible via both exists() and a size>0 stat.
    Necessary for SMB/UNC shares where the local client may not see a
    freshly-written file immediately after the writer process exits.
    Raises FileNotFoundError with directory context if the file never shows.
    """
    deadline = time.monotonic() + timeout
    delay = initial_delay
    while True:
        try:
            st = out_path.stat()
            if st.st_size > 0:
                return
        except (FileNotFoundError, OSError):
            pass
        if time.monotonic() >= deadline:
            # Give the user useful context -- what IS in the folder we
            # just told ffmpeg to write into?
            try:
                listing = sorted(os.listdir(staging_dir))
            except OSError:
                listing = []
            raise FileNotFoundError(
                f"ffmpeg reported success but {out_path.name} did not "
                f"appear in {staging_dir} within {timeout:.1f}s. "
                f"Folder contains: {listing[:20]}"
            )
        time.sleep(delay)
        delay = min(delay * 1.5, 0.5)
