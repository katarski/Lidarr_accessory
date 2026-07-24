"""
Per-CUE state machine.

Called from main.py every time a new .cue file appears. One instance handles
one disc image end-to-end:

    detect -> wait for stability
           -> find companion audio
           -> probe duration
           -> parse CUE (Ollama fallback if broken)
           -> (is_disc_image? else skip)
           -> ffmpeg split to staging (in-place next to source, or separate)
           -> tag (Ollama normalize optional)
           -> Lidarr API: DownloadedAlbumsScan
           -> poll command; if staging still has files, probe ManualImport
              and apply when match% >= floor (override Lidarr threshold)
           -> else: manual move into library + RefreshArtist + clear queue
           -> record outcome in the ledger
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cue_parser import Cue, parse_cue
from lidarr import LidarrClient
from ollama_client import OllamaClient
from splitter import SplitResult, probe_duration, repair_leadin, split_cue
from tagger import TagPlan, album_folder_name, tag_splits

logger = logging.getLogger(__name__)


_FS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# For fuzzy matching folder names vs Lidarr titles we need to ignore
# cosmetic punctuation differences (e.g. disk must use "-" where Lidarr
# stores ":", trailing year suffixes, "The "/"A " prefixes, commas,
# ampersands vs "and", etc.).
_MATCH_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MATCH_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")
# Only strip year when it's bracketed at the end -- "Album (2006)" or
# "Album [2006]". A bare trailing "3121" must NOT be eaten, because
# that's Prince's album name, not a year. Likewise Weezer's "2000",
# Metric's "1986", Taylor Swift's "1989", etc.
_MATCH_YEAR_SUFFIX = re.compile(r"\s+[\(\[]\s*\d{4}\s*[\)\]]\s*$")


def _sanitize_fs(value: str) -> str:
    """Make a string safe for use in filenames. Collapses forbidden chars."""
    return _FS_INVALID.sub("_", (value or "")).strip().rstrip(". ")


class _AuditSkip(Exception):
    """Sentinel raised to short-circuit the audit act-block for an album."""


def _album_track_file_count(album_rec: Dict[str, Any]) -> int:
    """
    How many tracks Lidarr has actual files for. Reads
    statistics.trackFileCount (preferred) or a top-level trackFileCount.
    Returns 0 if neither is present.
    """
    stats = album_rec.get("statistics") or {}
    for k in ("trackFileCount", "trackFilesCount"):
        v = stats.get(k)
        if isinstance(v, int):
            return v
        v = album_rec.get(k)
        if isinstance(v, int):
            return v
    return 0


# Cyrillic -> Latin transliteration (Bulgarian/Russian/Ukrainian covered).
# Needed so folder "Azis" matches Lidarr artist "Азис", folder "Bolka"
# matches Lidarr album "Болка", etc. NFKD alone won't do this -- Cyrillic
# letters are distinct code points, not Latin-with-diacritics.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
    "ы": "y", "э": "e", "ё": "yo",
    # Ukrainian extras
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
}


def _translit_cyrillic(value: str) -> str:
    """
    Fold any Cyrillic characters in `value` to a rough Latin
    transliteration. ASCII letters pass through untouched. Used as a
    best-effort equalizer for cross-script name matching: the folder on
    disk is often Latin ("Azis") while Lidarr stores the canonical
    Cyrillic ("Азис"), or vice-versa.
    """
    if not value:
        return value
    # Fast path: no Cyrillic, nothing to do.
    if not any("\u0400" <= ch <= "\u04ff" for ch in value):
        return value
    out = []
    for ch in value:
        lower = ch.lower()
        rep = _CYRILLIC_TO_LATIN.get(lower)
        if rep is None:
            out.append(ch)
        elif ch == lower:
            out.append(rep)
        else:
            out.append(rep.capitalize() if len(rep) > 1 else rep.upper())
    return "".join(out)


def _match_key(value: str) -> str:
    """
    Aggressive normalization for fuzzy equality of artist/album names.

    Handles the common reasons an on-disk folder and a Lidarr title
    disagree despite naming the same thing:

      * Windows-illegal `:` in Lidarr -> `-` on disk
      * Commas, periods, parentheses, square brackets, ampersands
      * "&" vs "and"
      * Trailing "(2019)" year suffix on disk
      * Leading "The "/"A "/"An " articles
      * Accented characters -> ascii
      * Cyrillic -> Latin (Азис == Azis, Болка == Bolka)
      * Double/extra whitespace
    """
    s = (value or "").strip().lower()
    if not s:
        return ""
    # Fold accents: "Beyoncé" -> "beyonce", "Motörhead" -> "motorhead".
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Transliterate Cyrillic to Latin so cross-script names collide.
    s = _translit_cyrillic(s)
    # Normalize "&" to "and" before stripping punctuation so they match.
    s = s.replace("&", " and ")
    # Strip trailing (YYYY) / [YYYY] / YYYY album-folder year suffix.
    s = _MATCH_YEAR_SUFFIX.sub("", s)
    # Collapse everything non-alphanumeric to single spaces.
    s = _MATCH_NON_ALNUM.sub(" ", s).strip()
    # Drop leading article.
    s = _MATCH_LEADING_ARTICLE.sub("", s).strip()
    # Collapse whitespace to single spaces.
    s = " ".join(s.split())
    return s


# These substrings identify an `is_disc_image(...)` reason as "definitely
# already split," meaning the .cue is orphan metadata and can be deleted
# without losing anything. Ambiguous reasons ("CUE points to already-split
# audio OR is corrupt") also match on 'already-split'. Corrupt/malformed
# cases ("no FILE reference") are intentionally NOT matched -- those are
# kept so Ollama's CUE-repair path can retry them later.
_PRE_SPLIT_REASON_MARKERS = (
    "separate audio files",   # multi-FILE CUE
    "pre-split album",        # FILE ref points to a subfolder
    "single-song CUE",        # per-song CUE
    "already-split",          # increasing-position failure / audio-too-short
)


# Superset of audio extensions used ONLY for "is this folder already
# split?" detection. Deliberately broader than the splitter's
# `audio_extensions` config (which lists only the disc-image formats
# ffmpeg should decode). ALAC/.m4a, MP3, OGG/Opus, DSF/DFF etc. still
# make the folder "pre-split" for our purposes -- we just never try to
# decode them ourselves; Lidarr handles the import.
_ALL_AUDIO_EXTS = frozenset({
    ".flac", ".ape", ".wv", ".wav", ".aiff", ".aif",
    ".m4a", ".mp4", ".m4b", ".alac",
    ".mp3", ".ogg", ".opus", ".oga",
    ".wma",
    ".dsf", ".dff",
    ".tak", ".tta",
    ".shn",
})


def _is_pre_split_reason(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return any(m in r for m in _PRE_SPLIT_REASON_MARKERS)


@dataclass
class OrchestratorConfig:
    audio_extensions: List[str]
    stable_seconds: int
    staging_root: Path
    lidarr_grace_seconds: int
    ffmpeg_binary: str
    flac_compression_level: int
    ffmpeg_extra_args: List[str]
    library_root_windows: Path
    album_folder_template: str = "{album} ({year})"
    # New options -------------------------------------------------------
    # "in_place": split next to the source CUE, delete originals on success.
    # "separate": split into staging_root (legacy behaviour).
    staging_mode: str = "in_place"
    # Output filename template. Placeholders:
    #   {artist} {album} {albumartist} {number:02d} {title} {ext}
    filename_template: str = "{artist} - {album} - {number:02d} - {title}.{ext}"
    # Lidarr policy overrides.
    min_match_percent: float = 60.0  # force-accept if match >= this
    cleanup_lidarr_queue: bool = True
    # How long to wait for a ManualImport command to drain the staging
    # folder. Lidarr's internal scheduler can sit on a command for a
    # while before picking it up, especially during RefreshArtist runs.
    manual_import_timeout_seconds: int = 300
    delete_originals_on_success: bool = True
    # After a successful import, recursively remove the folder that
    # contained the source CUE -- scans/, .log, .nfo, .m3u, cover art,
    # empty subfolders, everything. Guarded so it never touches the
    # watch root itself. Set False to keep the parent folder (empty).
    delete_source_folder_on_success: bool = True
    # Before processing a CUE, query Lidarr. If Lidarr already has a
    # matching album with its files, skip splitting entirely and just
    # delete the source folder -- there's no point re-importing what's
    # already in the library. Set False to always process.
    pre_check_lidarr_library: bool = True
    # Discography decision logic ("fill monitored gaps only"): only hand a
    # pre-split folder to Lidarr if it maps to an album Lidarr MONITORS and is
    # INCOMPLETE (missing tracks). Already-complete albums are skipped as
    # redundant (see pre_check_lidarr_library); folders with no monitored
    # target -- compilations / live / best-of that the metadata profile
    # excludes -- are skipped cleanly instead of being handed to Lidarr,
    # rejected, and left thrashing on disk. This is what stops a messy
    # discography torrent from wasting hours on albums that can never match.
    # When multiple editions of the SAME album are present, the sweep keeps the
    # one whose track count is closest to Lidarr's release and skips the rest.
    pre_split_monitored_gap_only: bool = True
    # DTS-CD support: a DTS audio CD rips to raw .dts surround streams (no tags,
    # no cue) that Lidarr can't ingest. When on, the cueless sweep transcodes
    # each .dts to a channel-preserving (5.1) FLAC, tags it (track/title from
    # the filename; artist/album from the folder name, LLM-assisted when there's
    # no 'Artist - Album' separator), and hands the FLACs to the normal import.
    transcode_dts_cd: bool = True
    # When Lidarr rejects a pre-split folder ONLY on title-mismatch grounds
    # ("unmatched tracks" / "missing tracks" / "not close enough") but the
    # on-disk file count EXACTLY equals a Lidarr release's track count, force
    # the import by pairing files to tracks by sorted position, then clean up
    # the source. This is the "23 files == 23-track release, one file is
    # named (Untitled) so Lidarr balks" rescue. Exact count match is required.
    force_import_on_count_match: bool = True
    # Tolerance for the force-import rescue: if the download has at least
    # (100 - this)% of a release's tracks, import the files that match and
    # accept the few absent tracks. 0 = require an exact count match.
    force_import_max_missing_percent: int = 10
    # Partial import: when a download matches a Lidarr album but is MISSING
    # tracks (fewer than the release has), import the tracks that DO match
    # rather than skipping the whole album. The source is then KEPT -- never
    # deleted -- because the album is incomplete; only exact/superset imports
    # (where everything landed) delete the source. Guarded by a coverage floor
    # so a single disc of a box set (well under the album's track count) isn't
    # crammed in: the download must cover at least this % of the release.
    force_import_partial: bool = True
    force_import_partial_min_percent: int = 50
    # Superset cap: how many EXTRA tracks (beyond a release's track count) a
    # download may carry and still be treated as a deluxe edition rather than
    # a compilation. If extras exceed max(2, this% of the release track count)
    # it's almost certainly a compilation spanning many albums -- refuse the
    # superset and leave it in place instead of cramming it into one folder.
    force_import_max_extra_percent: int = 25
    # When a CUE is detected as already-split metadata (multi-FILE CUE,
    # FILE references pointing into a subfolder, or per-song CUE), delete
    # the .cue file on first sight. Without this, the startup scan finds
    # it on every restart and re-enqueues the same skip decision forever.
    # Does NOT touch the audio files -- Lidarr handles those directly.
    delete_cue_if_pre_split: bool = True
    # Watch root -- needed so we can refuse to recursively delete it
    # or anything at/above it, no matter what cue_path.parent looks like.
    watch_root: Optional[Path] = None
    # Ledger path; None to disable.
    ledger_file: Optional[Path] = None
    # If True, NEVER do the manual-move fallback. Every successful outcome
    # must go through Lidarr's own DownloadedAlbumsScan or ManualImport so
    # that Lidarr's "Import Using Script" / Connect hooks fire. If both
    # Lidarr strategies fail, the CUE is marked failed and the source is
    # preserved for you to resolve manually. Recommended if you have a
    # post-import encode script wired up on the Lidarr side.
    strict_import_only: bool = False
    # If True (and Lidarr was unreachable at startup OR goes down mid-run),
    # wait for Lidarr to come back before processing a CUE, up to
    # lidarr_availability_wait_seconds. 0 = don't wait; negative =
    # wait indefinitely.
    wait_for_lidarr: bool = True
    lidarr_availability_wait_seconds: int = 10800  # 3 hours
    # Sweep for pre-split folders that have NO .cue file (the watcher
    # only triggers on .cue files, so these are invisible to it). On
    # startup (and periodically if sweep_interval_seconds > 0) walk the
    # watch root looking for folders that look pre-split and hand them
    # off to Lidarr via ManualImport. Off by default so it doesn't
    # surprise existing users on the next restart.
    sweep_cueless_pre_split: bool = False
    # Interval for the periodic sweep. 0 = startup only. Minimum 60s to
    # avoid thrashing the SMB share.
    sweep_interval_seconds: int = 0
    # A pre-split folder is only handed off if every file in it has
    # been untouched for at least this many seconds (guards against
    # grabbing an in-progress download). Defaults to 5 minutes.
    sweep_min_stable_seconds: int = 300
    # After Lidarr claims the import succeeded, open the music library
    # on disk (library_root_windows, e.g. \\PARK\Audio\Music) and confirm
    # the album folder is actually there. If it's on disk but Lidarr's
    # album record doesn't reflect the tracks as imported (hasFile=true),
    # keep nudging Lidarr (Rescan -> Refresh -> DownloadedAlbumsScan on
    # the library path) until it picks them up or we hit the timeout.
    # Disable to restore the old behavior (trust Lidarr's command status
    # alone).
    verify_library_after_import: bool = True
    # Total budget for the post-import verification loop (seconds). 0 to
    # disable waiting (just do a single check). Negative = wait forever.
    lidarr_verify_timeout_seconds: int = 1800
    # Periodically walk the music library on disk and compare to Lidarr's
    # DB. Folders found on disk that Lidarr has no artist/album record
    # for are written to a CSV report. On the first run (report file
    # absent) we ONLY report -- it's a dry run. On subsequent runs
    # (report + marker both present) we also trigger a
    # DownloadedAlbumsScan on each missing album folder so Lidarr picks
    # it up. A per-process set prevents us hitting the same folder twice
    # in one service lifetime.
    # Enable the periodic disk-vs-Lidarr audit. It runs on a schedule
    # (every library_audit_interval_seconds), NOT tied to startup. Kept as
    # a distinct toggle so turning it on doesn't couple it to anything else.
    library_audit_enabled: bool = False
    # Back-compat alias: old configs used library_audit_on_startup. Still
    # honored as "enable the audit", but it now means scheduled, not startup.
    library_audit_on_startup: bool = False
    # Interval for the periodic audit sweep. Minimum 300s so we don't hammer
    # the library share.
    library_audit_interval_seconds: int = 0
    # Only walk the library when it has actually changed since the last
    # audit (cheap artist/album dir-signature check). Avoids re-scanning the
    # whole library every cycle for nothing.
    library_audit_skip_unchanged: bool = True
    # CSV report of audit findings. Created on first run; read on
    # subsequent runs to determine "first run" vs "act" mode.
    library_audit_report_file: Optional[Path] = None

    # ---- Queue reaper --------------------------------------------------
    # Periodically remove "stuck" rows from Lidarr's download queue:
    # torrents that are FULLY downloaded but that Lidarr will never import
    # (unmatched artist=None grabs, best-hits compilations, title
    # mismatches the pipeline didn't force). Lidarr's own "remove
    # completed" only fires on a SUCCESSFUL import, so these leftovers
    # accumulate -- brutal at discography/mass-import scale.
    #
    # Safety model: a torrent is grouped by downloadId; the reaper only
    # acts once EVERY row for that torrent is finished-and-stuck (nothing
    # still downloading or actively importing) AND it has stayed that way
    # for at least queue_reaper_grace_minutes (persisted across restarts,
    # so a transient "waiting to import" blip never gets reaped).
    queue_reaper_enabled: bool = False
    # How often to sweep the queue (seconds). Minimum 60.
    queue_reaper_interval_seconds: int = 600
    # A torrent must sit finished-and-stuck this long before it's reaped,
    # giving Lidarr and the pipeline time to import it first.
    queue_reaper_grace_minutes: int = 30
    # Also delete the torrent + its data from the download client (qBit).
    # Required for the reap to actually stick -- otherwise Lidarr re-adds
    # the row on its next sync. WARNING: stops seeding (hit-and-run risk
    # on private trackers). True = delete from client + data.
    queue_reaper_remove_from_client: bool = True
    # Blocklist the release when reaping so Lidarr won't re-grab the exact
    # same release. Off by default (we reap junk, not "bad" releases).
    queue_reaper_blocklist: bool = False
    # JSON state file remembering when each torrent was first seen stuck,
    # so the grace clock survives restarts. Lives in /config.
    queue_reaper_state_file: Optional[Path] = None


class Orchestrator:
    def __init__(
        self,
        cfg: OrchestratorConfig,
        lidarr: LidarrClient,
        ollama: Optional[OllamaClient],
        acoustid=None,
    ):
        self.cfg = cfg
        self.lidarr = lidarr
        self.ollama = ollama
        # Optional AcoustID fingerprint identifier (best-effort). Used to
        # recover artist/album for a pre-split folder whose tags can't
        # identify it. None = disabled.
        self.acoustid = acoustid
        # Remember CUEs we've already classified as non-disc-images so we
        # don't spam INFO logs every time the watcher re-enqueues them on
        # startup. In-memory only; a restart re-parses (cheap).
        self._skip_seen: set = set()
        self._ledger_lock = threading.Lock()
        # Album IDs we've already release-cycled in this process lifetime,
        # so we don't hammer Lidarr with PUT+Refresh on every audit pass.
        self._audit_cycled_album_ids: set = set()
        # Trimmed "lead-in repair" temp files created while processing the
        # current CUE; deleted in process()'s finally so they never linger.
        self._repair_temps: set = set()

    # ---- Public entry --------------------------------------------------

    def process(self, cue_path: Path) -> None:
        staging_dir: Optional[Path] = None
        self._repair_temps = set()
        try:
            staging_dir = self._process(cue_path)
        except Exception as exc:
            logger.exception("Pipeline failed for %s: %s", cue_path, exc)
            self._record(
                cue_path=cue_path,
                outcome="failed",
                pre_split=False,
                reason=str(exc),
            )
            # Roll back any partial staging directory so it doesn't linger.
            if staging_dir is not None:
                self._rollback_staging(staging_dir)
        finally:
            # Remove any lead-in-repair temp copies we made for this CUE.
            for tmp in list(self._repair_temps):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError as exc:  # noqa: BLE001
                    logger.debug("could not remove repair temp %s: %s", tmp, exc)
            self._repair_temps = set()

    def _process(self, cue_path: Path) -> Optional[Path]:
        """
        Run the pipeline for one CUE. Returns the staging directory if one
        was created (so process() can roll it back on error), else None.
        """
        # Don't spam logs for CUEs we already decided to skip this run.
        if cue_path in self._skip_seen:
            return None

        logger.info("=== Processing %s ===", cue_path)

        # Hold the job until Lidarr is actually reachable. This avoids
        # silently falling through to the "manual copy" path when Lidarr
        # is restarting / stuck on a big RefreshArtist. The import job
        # for Lidarr's Connect scripts only fires via Lidarr itself, so
        # we want the ManualImport strategy to have a real shot.
        if self.cfg.wait_for_lidarr and not self._wait_for_lidarr_available():
            logger.error(
                "Lidarr never became reachable within the configured window; "
                "leaving %s for a later pass.",
                cue_path.name,
            )
            return None

        if not self._wait_for_stability(cue_path):
            logger.warning("CUE %s never stabilized, skipping", cue_path)
            return None

        # --- Pre-flight: is this folder already pre-split? ----------------
        # Folders containing several similarly-sized audio files (no single
        # big disc-image dominating the others) are already split. Any
        # .cue sitting there is orphan metadata. This catches the case
        # where `_find_companion_audio` would otherwise accidentally match
        # the CUE against one of the split tracks (via stem fallback or
        # extension drift) and try to process it as a "disc image".
        if self._looks_pre_split(cue_path.parent):
            audios = self._sibling_audio_files(cue_path.parent)
            logger.info(
                "Folder %s looks pre-split (%d audio files, no dominant "
                "disc image) -- handing off to Lidarr.",
                cue_path.parent, len(audios),
            )
            self._handoff_pre_split_to_lidarr(
                cue_path, cue_path.parent,
                reason=f"{len(audios)} similarly-sized audio files (no disc image)",
            )
            return None

        candidates = self._find_companion_candidates(cue_path)
        if not candidates:
            # A CUE with no matching disc-image audio is almost always
            # orphan metadata left sitting next to already-split tracks.
            # If the folder contains ANY audio files at all, hand the
            # folder off to Lidarr so it can import those tracks via its
            # own DownloadedAlbumsScan path (which fires Connect events).
            sibling_audio = self._sibling_audio_files(cue_path.parent)
            if sibling_audio:
                logger.info(
                    "No disc-image companion for %s, but folder has %d split "
                    "audio file(s) -- handing off to Lidarr.",
                    cue_path.name, len(sibling_audio),
                )
                self._handoff_pre_split_to_lidarr(
                    cue_path, cue_path.parent,
                    reason="orphan cue + split audio (no disc image)",
                )
                return None
            logger.error("No companion audio next to %s", cue_path)
            self._record(cue_path, outcome="failed", pre_split=False,
                         reason="no companion audio")
            return None

        # Probe every candidate. Pick the first that ffprobe accepts --
        # this is the "CUE referenced .wav but .wv exists / .wv is broken
        # so try .flac" repair path. Without this, a corrupt disc image
        # crashes the whole run in ffmpeg with no fallback.
        audio_path, duration, probe_errors = self._pick_decodable_companion(
            cue_path, candidates,
        )
        if audio_path is None:
            detail = "; ".join(probe_errors) or "no candidates decodable"
            logger.error(
                "No decodable companion audio next to %s (tried %d): %s",
                cue_path, len(candidates), detail,
            )
            # If the folder also contains already-split siblings beyond
            # the broken disc image, fall through to the Lidarr handoff
            # rather than leaving the user with a "failed" ledger entry
            # and no forward motion.
            sibling_audio = self._sibling_audio_files(cue_path.parent)
            if sibling_audio and len(sibling_audio) >= 2:
                logger.info(
                    "All disc-image candidates undecodable for %s, but "
                    "folder has %d audio files -- handing off to Lidarr.",
                    cue_path.name, len(sibling_audio),
                )
                self._handoff_pre_split_to_lidarr(
                    cue_path, cue_path.parent,
                    reason="disc image undecodable; folder has split audio",
                )
                return None
            self._record(
                cue_path, outcome="failed", pre_split=False,
                reason=f"all companion candidates failed probe: {detail[:400]}",
            )
            self._skip_seen.add(cue_path)
            return None
        logger.info(
            "Companion audio: %s (%.1fs, chose from %d candidate%s)",
            audio_path.name, duration or 0.0,
            len(candidates), "" if len(candidates) == 1 else "s",
        )
        # If the cue pointed at a different extension of this same file
        # (foo.wav vs foo.flac), correct the FILE line on disk to match.
        self._heal_cue_file_reference(cue_path, audio_path)
        try:
            cue = parse_cue(cue_path, duration, ollama=self.ollama)
        except ValueError as exc:
            # Unparseable CUE and no (working) Ollama to repair it.
            # Skip gracefully instead of raising a traceback.
            logger.warning("Cannot parse %s: %s", cue_path.name, exc)
            self._skip_seen.add(cue_path)
            self._record(cue_path, outcome="skipped", pre_split=False,
                         reason=f"unparseable: {exc}")
            return None

        # --- Pre-flight: is it already in Lidarr with files? -----------
        # Cheapest skip possible. Covers two cases:
        #   1. Backlog: CUEs we split before but never deleted the source.
        #   2. New download for an album the user already has.
        # In both cases there's no reason to split again; just erase the
        # source folder and move on.
        if self.cfg.pre_check_lidarr_library:
            matched = self._already_in_library(cue)
            if matched:
                artist_name = cue.performer or ""
                album_name = cue.title or ""
                stats = matched.get("statistics") or {}
                logger.info(
                    "Already in Lidarr library: %s / %s "
                    "(%s/%s tracks present). Deleting source folder.",
                    artist_name, album_name,
                    stats.get("trackFileCount"), stats.get("totalTrackCount"),
                )
                # Remove the source CUE + audio as well (belt-and-braces
                # for the case where the CUE sits at watch_root with no
                # enclosing folder and _delete_source_folder bails out).
                if self.cfg.delete_originals_on_success:
                    self._delete_originals(cue_path, audio_path)
                if self.cfg.delete_source_folder_on_success:
                    self._delete_source_folder(cue_path)
                self._skip_seen.add(cue_path)
                self._record(
                    cue_path, outcome="already_in_lidarr", pre_split=False,
                    artist=artist_name, album=album_name,
                    reason=f"trackFileCount={stats.get('trackFileCount')} "
                           f"totalTrackCount={stats.get('totalTrackCount')}",
                )
                return None

        # --- Gatekeeper: only process disc-image CUEs ------------------
        ok, reason = cue.is_disc_image(audio_duration=duration)
        if not ok:
            logger.info("Not a disc image, skipping %s: %s", cue_path.name, reason)
            # For UNAMBIGUOUS pre-split signals -- multi-FILE CUEs, CUEs
            # whose FILE ref points into a subfolder, per-song CUEs, and
            # increasing-position failures -- hand the folder to Lidarr so
            # it can import the existing split tracks (firing OnReleaseImport
            # for post-import scripts). "Corrupt" or "no FILE reference"
            # reasons are kept so Ollama's repair path can retry later.
            if _is_pre_split_reason(reason):
                self._handoff_pre_split_to_lidarr(
                    cue_path, cue_path.parent, reason=reason,
                )
                return None
            self._skip_seen.add(cue_path)
            self._record(
                cue_path, outcome="skipped_pre_split", pre_split=True,
                reason=reason, artist=cue.performer, album=cue.title,
            )
            return None

        # DTS-in-WAV disc image? ffmpeg decodes it to SILENCE, so a normal split
        # would emit silent tracks. Decode it to real 5.1 PCM with libdca first
        # and split THAT. No-op for ordinary audio.
        _dts_decoded = self._decode_dts_wav_source(audio_path)
        if _dts_decoded is not None:
            audio_path = _dts_decoded

        staging_dir = self._make_staging_dir(cue, cue_path, audio_path)
        logger.info("Staging to %s", staging_dir)

        # ffmpeg can still blow up on a candidate that ffprobe cleared --
        # e.g. a .wv WavPack file where only the header is valid. Retry
        # with the next decodable candidate in that case so one corrupt
        # companion file doesn't fail the whole album.
        split_errors: List[str] = []
        tried_audio: set = {audio_path}
        splits = None
        while True:
            try:
                splits = split_cue(
                    cue=cue,
                    audio_path=audio_path,
                    staging_dir=staging_dir,
                    ffmpeg_binary=self.cfg.ffmpeg_binary,
                    flac_compression_level=self.cfg.flac_compression_level,
                    extra_args=self.cfg.ffmpeg_extra_args,
                    filename_template=self.cfg.filename_template,
                )
                break
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip().splitlines()
                stderr_tail = " | ".join(stderr[-3:]) if stderr else ""
                msg = (
                    f"{audio_path.name}: ffmpeg exit={exc.returncode}"
                    + (f" :: {stderr_tail}" if stderr_tail else "")
                )
                logger.error("Split failed -- %s", msg)
                split_errors.append(msg)
                # Nuke whatever ffmpeg half-wrote before retrying; in
                # in_place mode this dir lives next to the CUE and
                # _rollback_staging refuses to touch it, so we clean
                # it up inline.
                self._clean_staging_dir(staging_dir)
            except (FileNotFoundError, ValueError) as exc:
                # FileNotFoundError: ffmpeg reported success but the
                # output never showed up on the SMB share.
                # ValueError: malformed cue with non-progressing ranges.
                msg = f"{audio_path.name}: {type(exc).__name__}: {exc}"
                logger.error("Split aborted -- %s", msg)
                split_errors.append(msg)
                self._clean_staging_dir(staging_dir)

            # Pick the next candidate that's not already been tried.
            remaining = [c for c in candidates if c not in tried_audio]
            next_audio, next_duration, next_errors = (
                self._pick_decodable_companion(cue_path, remaining)
            )
            if next_audio is None:
                detail = "; ".join(split_errors + next_errors) or "no alternatives"
                logger.error(
                    "All companion audio candidates failed to split for %s: %s",
                    cue_path, detail,
                )
                # If the folder looks pre-split after all, hand off to
                # Lidarr rather than flagging a hard failure.
                siblings = self._sibling_audio_files(cue_path.parent)
                if siblings and len(siblings) >= 2:
                    logger.info(
                        "All disc-image attempts failed for %s but folder "
                        "has %d audio files -- handing off to Lidarr.",
                        cue_path.name, len(siblings),
                    )
                    self._handoff_pre_split_to_lidarr(
                        cue_path, cue_path.parent,
                        reason="all companion audio failed to split",
                    )
                    return None
                self._record(
                    cue_path, outcome="failed", pre_split=False,
                    reason=f"split failed on all candidates: {detail[:400]}",
                    artist=cue.performer, album=cue.title,
                )
                self._skip_seen.add(cue_path)
                return staging_dir if staging_dir.exists() else None
            logger.info(
                "Retrying split with next candidate: %s (%.1fs)",
                next_audio.name, next_duration or 0.0,
            )
            audio_path = next_audio
            duration = next_duration
            tried_audio.add(audio_path)

        plans = tag_splits(cue, splits, ollama=self.ollama)
        # Re-name files using the final (post-Ollama) plan so filenames
        # reflect any title cleanup the LLM did.
        splits = self._rename_to_plan(splits, plans, cue)

        # Extraction + tagging succeeded, and the split files now live in
        # staging. We deliberately do NOT delete or park the source disc
        # image + .cue here. Nothing about the source is touched until the
        # import is verified to have actually landed in the library (see
        # the post-verification block below). This is the guarantee:
        # a source is only ever removed once its tracks are confirmed in
        # the library -- whether the move was done by Lidarr or by our own
        # manual-move fallback. If the import fails, the originals stay put
        # so the album can be recovered or retried.

        artist_name = (plans[0].albumartist if plans else cue.performer) or ""
        album_name = (plans[0].album if plans else cue.title) or ""

        # --- DTS-CD: import EXACTLY like the working Enigma case --------
        # Lidarr's import mishandles the decoded 5.1 FLACs when they sit in a
        # split-staging SUBfolder nested inside the tracked qBit download: it
        # copies each file into the library then rolls it back out, leaving the
        # album at 0/N ("Manually imported N files" but nothing lands). Enigma
        # imported cleanly only because its FLACs were a FLAT folder at the
        # download root with no source disc-image beside them. So mirror that:
        # move the 5.1 tracks into a CLEAN top-level folder (not nested, no
        # source .wav, no download-client tie), drop the source, and hand off
        # via the same pre-split ManualImport path that landed Enigma.
        if _dts_decoded is not None:
            try:
                base = cue_path.parent.parent
                if not base or not base.exists():
                    base = cue_path.parent
                label = _FS_INVALID.sub("_", f"{artist_name} - {album_name}").strip(" ._-") or "dts_album"
                clean = base / label
                if clean.resolve() == cue_path.parent.resolve():
                    clean = base / f"{label} (5.1)"
                clean.mkdir(parents=True, exist_ok=True)
                moved = 0
                for sp in splits:
                    try:
                        shutil.move(str(sp.output_path), str(clean / sp.output_path.name))
                        moved += 1
                    except OSError as exc:
                        logger.warning("DTS-CD: could not move %s: %s",
                                       sp.output_path.name, exc)
                logger.info(
                    "DTS-CD: relocated %d/%d 5.1 FLAC(s) to clean folder %s "
                    "(Enigma-style flat import).", moved, len(splits), clean,
                )
                # The source download (.wav disc image + .cue + empty staging)
                # is no longer needed -- the tracks are safe in `clean`.
                self._delete_source_folder(cue_path)
                # Import via the proven pre-split ManualImport handoff.
                self._handoff_pre_split_to_lidarr(None, clean, reason="dts-cd 5.1")
                return clean
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "DTS-CD clean-folder import failed (%s); falling through to "
                    "normal strategies.", exc,
                )

        # --- Find the matching queue entry (if any) --------------------
        # Unpackerr-style correlation: when we tell Lidarr to scan, pass
        # the downloadClientId of the stuck queue row so Lidarr ties the
        # import to that specific torrent/nzb entry and clears it cleanly.
        queue_entry = self.lidarr.queue_find_for(artist_name, album_name)
        download_client_id = queue_entry.get("downloadId") if queue_entry else None
        if queue_entry:
            logger.info(
                "Matched Lidarr queue entry id=%s downloadId=%s title=%r",
                queue_entry.get("id"),
                download_client_id,
                queue_entry.get("title"),
            )

        # --- Strategy 1: ask Lidarr to scan the staging folder ---------
        outcome: Optional[str] = None
        # Track any artistId we learn during import so the post-success
        # RefreshArtist can target it directly without a second lookup.
        imported_artist_id: Optional[int] = None
        # DTS-CD decode? Its 5.1 FLACs must NOT go through DownloadedAlbumsScan.
        # Lidarr's download-tied folder scan fails on them ("Failed to import"),
        # copying each file into the library then rolling it back out, and that
        # leaves the follow-up import unable to land them. Skip straight to the
        # explicit ManualImport path (Strategy 2) -- the exact path that cleanly
        # imported the 6-channel Enigma DTS-CD. Applies to every DTS-CD, so no
        # manual handling at scale.
        skip_scan = _dts_decoded is not None
        if skip_scan:
            logger.info(
                "DTS-CD: skipping DownloadedAlbumsScan (it rejects 5.1 folders); "
                "importing via ManualImport directly."
            )
        command_id = (
            None if skip_scan
            else self.lidarr.downloaded_albums_scan(
                staging_dir, download_client_id=download_client_id
            )
        )
        if command_id is not None:
            record = self.lidarr.wait_for_command(
                command_id, timeout_seconds=self.cfg.lidarr_grace_seconds
            )
            self._log_command_record("DownloadedAlbumsScan", record)

            if self._staging_cleared(staging_dir):
                outcome = "imported_via_scan"

        # --- Strategy 2: probe ManualImport + match% override ----------
        if outcome is None:
            lidarr_path = self.lidarr.windows_to_lidarr(staging_dir)
            candidates = self.lidarr.manual_import_candidates(lidarr_path)
            if candidates:
                self._log_rejections(candidates)
                acceptable = self._filter_acceptable(candidates)
                if acceptable:
                    # Before calling ManualImport, hydrate any missing
                    # artist/album/release/track IDs ourselves. Lidarr
                    # returns these as null when it refused to pick a
                    # release (the same reason we're overriding its
                    # refusal), so we fill them in by looking up the
                    # artist/album we already know from the CUE.
                    hydrated = self._hydrate_candidates(
                        acceptable, artist_name, album_name, len(splits)
                    )
                    logger.info(
                        "ManualImport: %d/%d files accepted "
                        "(match floor = %.0f%%, %d hydrated) -- committing",
                        len(acceptable), len(candidates),
                        self.cfg.min_match_percent,
                        sum(1 for h in hydrated if h is not None),
                    )
                    committable = [h for h in hydrated if h is not None]
                    mi_cmd = self.lidarr.manual_import_apply(committable)
                    if mi_cmd is not None:
                        # Give Lidarr time to both run the command AND
                        # actually move the files. We watch BOTH the
                        # command record (for status/message logging)
                        # and the staging folder (as ground truth),
                        # because Lidarr can sit on a queued command
                        # for a while if it's busy with other jobs.
                        if self._wait_for_manual_import(
                            mi_cmd, staging_dir,
                            timeout=self.cfg.manual_import_timeout_seconds,
                        ):
                            outcome = "imported_via_manual"
                            for h in committable:
                                aid = h.get("artistId")
                                if aid:
                                    imported_artist_id = int(aid)
                                    break
                else:
                    logger.warning(
                        "No ManualImport candidate met the %.0f%% match floor; "
                        "falling back to file move.",
                        self.cfg.min_match_percent,
                    )
            else:
                logger.warning(
                    "ManualImport probe returned no candidates for %s "
                    "(Lidarr can't see the folder? check path_mapping)",
                    lidarr_path,
                )

        # --- Strategy 3: manual move + refresh -------------------------
        # Skipped when strict_import_only is set: we'd rather leave the
        # files in staging (and flag the CUE as failed) than bypass
        # Lidarr's own import event, which is what fires the user's
        # "Import Using Script" / Connect hooks. A manual move puts
        # files on disk, but Lidarr never raises OnReleaseImport for
        # files placed out-of-band, so post-import encoders never run.
        if outcome is None and self.cfg.strict_import_only:
            logger.error(
                "Both Lidarr strategies failed for %s and strict_import_only "
                "is enabled -- NOT falling back to manual move. Source folder "
                "and split files are preserved under %s so you can import them "
                "by hand (Lidarr -> Wanted -> Manual Import).",
                cue_path.name, staging_dir,
            )
            self._record(
                cue_path, outcome="failed", pre_split=False,
                reason="lidarr import failed; strict_import_only",
                artist=artist_name, album=album_name,
            )
            return staging_dir

        if outcome is None:
            logger.warning("Falling back to manual file move.")
            target_dir = self._manual_move_to_library(plans, splits)
            if target_dir is None:
                logger.error("Manual move failed; files remain in %s", staging_dir)
                self._record(cue_path, outcome="failed", pre_split=False,
                             reason="manual move failed",
                             artist=artist_name, album=album_name)
                return staging_dir

            artist = self.lidarr.find_artist(artist_name) if artist_name else None
            if artist:
                self.lidarr.refresh_artist(artist["id"])
                outcome = "moved_then_refreshed"
            else:
                logger.warning(
                    "Artist '%s' not found in Lidarr -- no auto-refresh. "
                    "Files are in place; Lidarr will see them on its next scan.",
                    artist_name,
                )
                outcome = "moved_no_refresh"
            # Kick Lidarr so any stuck queue item referring to this
            # download gets re-examined immediately instead of waiting
            # for the next scheduled sweep. Same as unpackerr's finale.
            self.lidarr.process_monitored_downloads()

        # --- Post-success housekeeping --------------------------------
        if self.cfg.cleanup_lidarr_queue and artist_name:
            self._clear_lidarr_queue(artist_name, album_name)

        # Remove the staging subfolder if the import emptied it (a real
        # "move" leaves it empty). Source originals are handled later, only
        # after verification -- see the post-verification cleanup block.
        self._cleanup_empty(staging_dir)

        # Lidarr accepts the import command and inserts the track rows,
        # but the artist page (cached) often doesn't reflect the new
        # album until an explicit RefreshArtist. Fire one now so the UI
        # is consistent with disk by the time the user looks.
        # moved_then_refreshed already refreshed above; skip it.
        if outcome in ("imported_via_scan", "imported_via_manual") and artist_name:
            self._trigger_artist_refresh(artist_name, artist_id=imported_artist_id)

        # --- Post-import verification -------------------------------------
        # Lidarr can happily report `completed / successful` without the
        # new album ever appearing under the artist -- usually because it
        # imported the tracks against the wrong release mapping, or
        # because the artist-page cache is stale, or because the release
        # metadata simply hadn't landed yet. Ground truth is the music
        # library folder on disk. If the files are there, we keep
        # nudging Lidarr (Rescan -> Refresh -> DownloadedAlbumsScan on
        # the library album folder) until the album record shows the
        # tracks, or we hit the verify timeout.
        #
        # This happens BEFORE source-folder cleanup on purpose -- if
        # verification fails we leave the source folder on disk as a
        # breadcrumb so the user can diagnose (or re-run).
        verified = True
        if (
            self.cfg.verify_library_after_import
            and outcome in ("imported_via_scan", "imported_via_manual",
                            "moved_then_refreshed", "moved_no_refresh")
            and artist_name and album_name
        ):
            verified = self._verify_library_reflects_album(
                artist_name, album_name, len(splits),
                imported_artist_id=imported_artist_id,
            )
            if not verified:
                logger.warning(
                    "Import '%s' for %s / %s did not verify in Lidarr; "
                    "marking outcome as imported_unverified and preserving "
                    "the source folder for inspection.",
                    outcome, artist_name, album_name,
                )
                outcome = "imported_unverified"

        # Source cleanup. Reaching this point means the import/move
        # SUCCEEDED -- every failure path returned earlier (failed manual
        # move, strict_import_only with no Lidarr success, etc.), and a
        # success outcome is only set once Lidarr cleared staging (files
        # moved out) or our own manual move placed the files in the library.
        # So the files ARE in the library: delete/park the source now.
        # We do NOT gate on a library name re-match -- Lidarr stores albums
        # under its own canonical names ("The X" -> "X", curly apostrophes,
        # &/and), which made the disk check fail and wrongly retain folders
        # whose tracks had, in fact, been imported. The `verified` flag above
        # is advisory only (it labels the ledger + nudges Lidarr's view).
        if self.cfg.delete_source_folder_on_success:
            # Nuke the whole folder that held the CUE + all siblings
            # (scans/, .log, .nfo, cover art, the disc image, etc.).
            self._delete_source_folder(cue_path)
        elif self.cfg.delete_originals_on_success:
            # Keep the folder, but remove the now-redundant disc image + .cue.
            self._delete_originals(cue_path, audio_path)
        else:
            # Neither delete flag set: park the originals out of the watch
            # tree (non-destructive) so we don't re-process them.
            self._cleanup_source(cue_path, audio_path)

        self._record(
            cue_path, outcome=outcome, pre_split=False,
            artist=artist_name, album=album_name,
            track_count=len(splits),
        )
        return None

    # ---- Helpers -------------------------------------------------------

    def _wait_for_stability(self, path: Path) -> bool:
        """Return True when file size stops changing for `stable_seconds`."""
        deadline = time.monotonic() + max(self.cfg.stable_seconds * 6, 60)
        last_size = -1
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            if not path.exists():
                time.sleep(1)
                continue
            size = path.stat().st_size
            now = time.monotonic()
            if size != last_size:
                last_size = size
                last_change = now
            elif now - last_change >= self.cfg.stable_seconds:
                return True
            time.sleep(1)
        return False

    def _sibling_audio_files(self, folder: Path) -> List[Path]:
        """
        List audio files in a folder. Uses the broad `_ALL_AUDIO_EXTS` set
        (ALAC/.m4a, MP3, Opus, DSD, etc.) -- NOT `self.cfg.audio_extensions`,
        which is the narrower ffmpeg-decode list for disc images.
        A folder full of .m4a ALAC files is still a pre-split folder even
        though our splitter would never try to decode them.
        Used to distinguish "orphan .cue next to pre-split tracks" from
        "truly empty folder with just a stray .cue".
        """
        out: List[Path] = []
        try:
            for p in folder.iterdir():
                try:
                    if p.is_file() and p.suffix.lower() in _ALL_AUDIO_EXTS:
                        out.append(p)
                except OSError:
                    continue
        except OSError:
            return []
        return out

    _DISC_SUBDIR_RE = re.compile(r"(?i)^(?:cd|disc|disk|dvd)\s*[.\-_]?\s*\d{1,2}\b")
    _DISC_NUM_RE = re.compile(r"(?i)^(?:cd|disc|disk|dvd)\s*[.\-_]?\s*(\d{1,2})\b")

    def _disc_number_from_folder(self, folder: Path) -> Optional[int]:
        """'CD1'/'Disc 2' -> 1/2, else None."""
        m = self._DISC_NUM_RE.match(folder.name or "")
        return int(m.group(1)) if m else None

    def _album_folder_identity(self, folder: Path) -> tuple[str, str]:
        """
        Parse (artist, album) from the album FOLDER name, climbing past a disc
        subfolder ('CD1' / 'Disc 2'). More reliable than track-1 tags for
        various-artists or multi-disc compilations, where each track lists a
        different collaboration as its 'artist'. Returns ("","") when the
        folder isn't in 'Artist - Album ...' form.
        """
        p = folder
        if self._DISC_SUBDIR_RE.match(p.name or ""):
            p = p.parent
        name = re.sub(r"[\(\[\{][^)\]\}]*[)\]\}]", " ", p.name or "")
        name = re.sub(r"\s{2,}", " ", name).strip(" -_.")
        if " - " not in name:
            return "", ""
        artist, album = name.split(" - ", 1)
        album = re.sub(r"(?i)\bCD\s*\d+\b", "", album).strip(" -_.")
        return artist.strip(" -_."), album.strip(" -_.")

    def _read_audio_tags(self, path: Path) -> tuple[str, str]:
        """
        Read (artist, album) from an audio file using mutagen's format-
        agnostic File() loader. Falls back to parsing the filename if the
        file has no usable tags. Empty strings on total failure.

        Works for FLAC, ALAC/.m4a, MP3, Ogg, Opus, WMA, etc. -- i.e. any
        file a pre-split download folder might contain.
        """
        try:
            from mutagen import File as MutagenFile  # lazy import
        except Exception:  # noqa: BLE001
            MutagenFile = None  # type: ignore
        artist = album = ""
        if MutagenFile is not None:
            try:
                mf = MutagenFile(str(path))
                if mf is not None:
                    def _first(keys: tuple) -> str:
                        for k in keys:
                            val = mf.tags.get(k) if mf.tags else None
                            if not val:
                                continue
                            # mutagen can return list-of-str or MP4FreeForm etc.
                            if isinstance(val, list) and val:
                                val = val[0]
                            s = str(val).strip()
                            if s:
                                return s
                        return ""
                    # Vorbis/FLAC uses uppercase, MP4/ALAC uses ©ART/©alb,
                    # ID3 uses TPE1/TALB, etc. Try them all.
                    artist = _first((
                        "albumartist", "ALBUMARTIST", "artist", "ARTIST",
                        "\xa9ART", "aART", "TPE1", "TPE2",
                    ))
                    album = _first((
                        "album", "ALBUM", "\xa9alb", "TALB",
                    ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("mutagen read failed for %s: %s", path.name, exc)
        # Filename fallback: "<Artist> - <Album> - NN - <Title>.<ext>"
        if not artist or not album:
            stem = path.stem
            parts = [p.strip() for p in stem.split(" - ")]
            if len(parts) >= 2:
                if not artist:
                    artist = parts[0]
                if not album:
                    album = parts[1]
        return artist, album

    def _try_positional_force_import(
        self, cue_path, folder, key_path, artist_name, album_name, audios, reason,
    ) -> bool:
        """
        Rescue import for a folder Lidarr rejected only on title-mismatch
        grounds. Three modes:

          * EXACT    -- file count == a release's track count: pair files to
            tracks by sorted position and force ALL of them in (the "one file
            named (Untitled) breaks the batch" case).
          * SUPERSET -- download has MORE files than a release AND covers
            EVERY track of it: import the release's tracks, then relocate the
            leftover files into the same library album folder so the whole
            download moves and nothing is lost.
          * PARTIAL  -- file count is within `force_import_max_missing_percent`
            below a release's track count: import the files Lidarr matched
            (by title, so gaps don't misalign) and accept the few absent
            tracks.

        On a verified import, cleans up the source (only once the source no
        longer holds un-relocated audio). Returns True only when the import
        lands. Never raises into the caller.
        """
        if not (artist_name and album_name):
            return False
        n = len(audios)
        tol = max(0, int(self.cfg.force_import_max_missing_percent))
        # Partial-import coverage floor: the download must cover at least this
        # % of a release to import its matching tracks. When partial import is
        # off, fall back to the old (100 - tol)% "near-complete" behaviour.
        pmin = (max(1, int(self.cfg.force_import_partial_min_percent))
                if self.cfg.force_import_partial else (100 - tol))
        try:
            arec = self.lidarr.find_artist(artist_name)
            if not arec:
                return False
            aid = int(arec["id"])
            alb = self.lidarr.find_album(aid, album_name)
            if not alb:
                return False
            # Strict: matched album title must normalize-equal ours, so a
            # fuzzy substring match can't force the WRONG album.
            if _match_key(alb.get("title")) != _match_key(album_name):
                return False
            album_id = int(alb["id"])

            # Choose a target release. Preference:
            #   exact    -- n == T          (positional; grabs every file)
            #   superset -- T < n           (download has MORE files than the
            #               release; import its tracks, relocate the extras)
            #   partial  -- n < T within tol% (a few tracks missing)
            full = self.lidarr.get_album(album_id) or alb
            releases = full.get("releases") or []
            exact_rid = None
            superset = None  # (rid, T): largest release smaller than the folder
            partial = None   # (rid, T): smallest release just above the folder
            for r in releases:
                T = int(r.get("trackCount") or 0)
                if T <= 0:
                    continue
                if n == T:
                    exact_rid = r.get("id")
                    break
                if T < n:
                    # Guard against compilations: only a SMALL number of
                    # extras counts as a deluxe-edition superset. 25 files
                    # vs an 8-track release (17 extras) is a compilation,
                    # not a superset -- skip it.
                    allowed_extra = max(
                        2, (T * self.cfg.force_import_max_extra_percent) // 100
                    )
                    if (n - T) <= allowed_extra and (
                        superset is None or T > superset[1]
                    ):
                        superset = (r.get("id"), T)
                elif 100 * n >= pmin * T:   # n < T, download covers >= pmin% of release
                    if partial is None or T < partial[1]:
                        partial = (r.get("id"), T)
            if exact_rid is not None:
                target_rid, T, mode = exact_rid, n, "exact"
            elif superset is not None:
                target_rid, T, mode = superset[0], superset[1], "superset"
            elif partial is not None:
                target_rid, T, mode = partial[0], partial[1], "partial"
            else:
                logger.info(
                    "Force-import: no release of %s / %s fits %d files "
                    "(exact / superset / within %d%% missing) -- not forcing.",
                    artist_name, album_name, n, tol,
                )
                return False

            logger.info(
                "Force-import (%s): %s / %s -- %d files vs release %s "
                "(%d tracks); flipping release and importing.",
                mode, artist_name, album_name, n, target_rid, T,
            )
            self.lidarr.set_album_monitored_release(album_id, target_rid)
            rcmd = self.lidarr.refresh_artist(aid)
            if rcmd:
                self.lidarr.wait_for_command(
                    rcmd, timeout_seconds=45, poll_interval=1.5,
                )
            lidarr_path = self.lidarr.windows_to_lidarr(folder)
            cands = self.lidarr.manual_import_candidates(lidarr_path)

            if mode == "exact":
                tracks_all = self.lidarr.list_tracks_for_album(album_id)
                tracks_rel = [
                    t for t in tracks_all
                    if t.get("albumReleaseId") == target_rid
                    or not t.get("albumReleaseId")
                ]
                cmd = self.lidarr.manual_import_positional(
                    cands, tracks_rel, album_id, target_rid, aid,
                )
            else:
                # superset / partial: keep Lidarr's own per-file title matches
                # (so gaps/extras don't misalign), force the release id, drop
                # files that don't map to a track.
                importable = []
                matched_track_ids = set()
                for c in cands:
                    tids = [t["id"] for t in (c.get("tracks") or []) if t.get("id")]
                    if not tids:
                        continue
                    item = dict(c)
                    item["artist"] = arec
                    item["album"] = alb
                    item["albumRelease"] = {"id": target_rid}
                    importable.append(item)
                    matched_track_ids.update(tids)
                if mode == "superset":
                    # Only proceed if the download covers EVERY release track.
                    if len(matched_track_ids) < T:
                        logger.info(
                            "Force-import (superset): download covers only "
                            "%d/%d release tracks -- not a true superset; "
                            "not forcing.",
                            len(matched_track_ids), T,
                        )
                        return False
                    cmd = self.lidarr.manual_import_apply(importable)
                elif 100 * len(importable) >= pmin * T:   # partial, enough matched
                    cmd = self.lidarr.manual_import_apply(importable)
                else:
                    # Partial, but Lidarr mapped too few files -- common for a
                    # various-artists disc it can't identify. Disc-aware
                    # positional rescue: if this folder is disc N of a multi-disc
                    # release and that medium's track count equals the file
                    # count, pair them by position (safe -- the disc's tracks are
                    # a known contiguous block). Otherwise give up.
                    disc = self._disc_number_from_folder(folder)
                    disc_tracks = []
                    if disc is not None:
                        disc_tracks = [
                            t for t in self.lidarr.list_tracks_for_album(album_id)
                            if (t.get("albumReleaseId") == target_rid
                                or not t.get("albumReleaseId"))
                            and t.get("mediumNumber") == disc
                        ]
                    if disc_tracks and len(disc_tracks) == n:
                        logger.info(
                            "Force-import (partial): Lidarr mapped only %d/%d; "
                            "pairing %d files positionally to disc %d (%d "
                            "tracks) of %s / %s.",
                            len(importable), T, n, disc, len(disc_tracks),
                            artist_name, album_name,
                        )
                        cmd = self.lidarr.manual_import_positional(
                            cands, disc_tracks, album_id, target_rid, aid,
                        )
                    else:
                        logger.info(
                            "Force-import (partial): only %d/%d files matched a "
                            "track -- below %d%% coverage floor and no "
                            "disc-positional fit; not forcing.",
                            len(importable), T, pmin,
                        )
                        return False

            if not cmd:
                return False
            # Only EXACT should fully clear staging. Superset and PARTIAL both
            # legitimately leave files behind (extras / unmatched), so requiring
            # a cleared staging would reject every partial as "not trusted" --
            # which made compilation folders re-import in an endless loop.
            if not self._wait_for_manual_import(
                cmd, folder, timeout=self.cfg.manual_import_timeout_seconds,
                require_cleared=(mode == "exact"),
                staging_exts=_ALL_AUDIO_EXTS,
            ):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Force-import failed for %s: %s", folder, exc)
            return False

        # Import landed. In superset mode Lidarr moved the matched tracks out
        # but EXTRA files remain -- relocate them into the library album
        # folder so the whole download moves and nothing is lost. If we can't
        # find the library folder or a move fails, keep the source rather than
        # risk losing the extras.
        ok_to_delete = True
        remaining = self._sibling_audio_files(folder)
        if mode == "partial":
            # Lidarr MOVES the tracks it imports out of the folder, so whatever
            # audio is still here is exactly what Lidarr did NOT take -- extras
            # it isn't using. Keep the source only for those; if nothing is
            # left, every file the download had was imported and the source is
            # redundant (the "missing" tracks were never in the download), so
            # delete it. Don't relocate leftovers -- they stay in the download.
            if remaining:
                logger.info(
                    "Force-import (partial): imported the matching tracks for "
                    "%s / %s; keeping %d un-imported extra file(s) in the "
                    "source.", artist_name, album_name, len(remaining),
                )
                ok_to_delete = False
            else:
                logger.info(
                    "Force-import (partial): every file the download had was "
                    "imported for %s / %s -- source is redundant, deleting.",
                    artist_name, album_name,
                )
                ok_to_delete = True
        elif remaining:
            album_dir, _existing = self._find_album_on_disk(artist_name, album_name)
            if album_dir is None:
                logger.warning(
                    "Force-import: imported %s / %s but could not locate its "
                    "library folder to relocate %d extra file(s); leaving "
                    "source in place.",
                    artist_name, album_name, len(remaining),
                )
                ok_to_delete = False
            else:
                moved = 0
                for f in remaining:
                    try:
                        shutil.move(str(f), str(album_dir / f.name))
                        moved += 1
                    except OSError as exc:
                        logger.warning(
                            "Force-import: could not relocate extra %s: %s",
                            f.name, exc,
                        )
                if moved == len(remaining):
                    logger.info(
                        "Force-import: relocated %d extra file(s) into %s",
                        moved, album_dir,
                    )
                else:
                    logger.warning(
                        "Force-import: relocated %d/%d extras; leaving source "
                        "in place.", moved, len(remaining),
                    )
                    ok_to_delete = False

        logger.info("Force-import succeeded for %s / %s.", artist_name, album_name)
        if self.cfg.cleanup_lidarr_queue:
            self._clear_lidarr_queue(artist_name, album_name)
        self._trigger_artist_refresh(artist_name, artist_id=aid)
        self._record(
            key_path, outcome="imported_via_manual", pre_split=True,
            artist=artist_name, album=album_name,
            reason=f"force-import {mode} ({reason})",
        )
        if ok_to_delete:
            if self.cfg.delete_source_folder_on_success:
                sentinel = (
                    cue_path if cue_path is not None else folder / ".cueless_sweep"
                )
                self._delete_source_folder(sentinel)
            elif self.cfg.delete_originals_on_success:
                for a in audios:
                    try:
                        if a.exists():
                            a.unlink()
                    except OSError as exc:
                        logger.warning("Could not delete %s: %s", a, exc)
        return True

    _DTS_MARKER_RE = re.compile(
        r"(?i)[\s\-_.\(\[]*\bdts(?:[\s\-_.]?cd)?\b[\s\-_.\)\]]*"
    )

    def _dts_identity(self, folder: Path) -> Tuple[str, str]:
        """
        (artist, album) for a DTS-CD folder. Prefer the 'Artist - Album' folder
        parser; when the name has no separator (e.g. 'Enigma A Posteriori
        DTS_CD') and an LLM is enabled, ask it to split -- guarded so it can
        only use words already in the folder name. ("","") if undetermined.
        """
        artist, album = self._album_folder_identity(folder)
        if artist and album:
            # Strip the DTS/DTS_CD marker the folder parser leaves on the album
            # ("A Posteriori DTS_CD" -> "A Posteriori").
            album = re.sub(
                r"\s{2,}", " ", self._DTS_MARKER_RE.sub(" ", album)
            ).strip(" -_.")
            if album:
                return artist, album
        raw = self._DTS_MARKER_RE.sub(" ", folder.name or "")
        raw = re.sub(r"\s{2,}", " ", raw).strip(" -_.")
        if self.ollama is not None and getattr(self.ollama, "enabled", False):
            try:
                a, b = self.ollama.parse_artist_album(raw)
                if a and b:
                    logger.info(
                        "DTS-CD: LLM parsed identity from %r -> artist=%r album=%r",
                        folder.name, a, b,
                    )
                    return a, b
            except Exception as exc:  # noqa: BLE001
                logger.warning("DTS-CD: LLM identity parse failed: %s", exc)
        return "", ""

    # DTS frame sync word (first 4 bytes) in the four orderings a DTS-CD may
    # store it: 16-bit BE, 16-bit LE, 14-bit BE, 14-bit LE. Used to recognise a
    # WAV whose "PCM" is really a DTS bitstream -- the same check VLC does.
    _DTS_SYNCS = (
        b"\x7f\xfe\x80\x01", b"\xfe\x7f\x01\x80",
        b"\x1f\xff\xe8\x00", b"\xff\x1f\x00\xe8",
    )

    def _wav_dts_data_range(self, wav_path: Path):
        """
        If `wav_path` is a WAV whose audio data is actually a DTS bitstream (a
        DTS-CD rip), return (data_offset, data_size); else None. Walks the RIFF
        chunks to find 'data' (never assumes offset 44), then checks its first
        4 bytes against the DTS sync words.
        """
        try:
            with open(wav_path, "rb") as f:
                hdr = f.read(12)
                if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                    return None
                while True:
                    ch = f.read(8)
                    if len(ch) < 8:
                        return None
                    size = int.from_bytes(ch[4:8], "little")
                    if ch[:4] == b"data":
                        off = f.tell()
                        first4 = f.read(4)
                        if any(first4 == s for s in self._DTS_SYNCS):
                            return (off, size)
                        return None
                    f.seek(size + (size & 1), 1)  # chunks are word-aligned
        except OSError:
            return None

    def _dcadec_to_wav(self, es_path: Path, out_wav: Path) -> bool:
        """
        Decode a raw DTS elementary stream to a 5.1 WAV using libdca's `dcadec`
        (-o wav6). ffmpeg's built-in dca decoder can't frame 14-bit DTS-CD
        streams, which is why we shell out to libdca (the decoder VLC uses).
        Returns True if a real (non-empty) WAV was produced.
        """
        try:
            with open(out_wav, "wb") as w:
                subprocess.run(
                    ["dcadec", "-o", "wav6", str(es_path)],
                    stdout=w, stderr=subprocess.DEVNULL, timeout=3600,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dcadec failed for %s: %s", es_path.name, exc)
        try:
            ok = out_wav.exists() and out_wav.stat().st_size > 1024
        except OSError:
            ok = False
        if not ok and out_wav.exists():
            out_wav.unlink(missing_ok=True)
        return ok

    def _decode_dts_wav_source(self, audio_path: Path):
        """
        If `audio_path` is a DTS-in-WAV disc image, decode it to a real 5.1 PCM
        WAV (via dcadec) and return that path; else None. MUST run before the
        split: ffmpeg reads a DTS-in-WAV as SILENCE, so splitting the original
        would yield silent tracks. The DTS bytes are pulled straight out of the
        data chunk (ffmpeg mis-decodes them). Temp files are registered in
        self._repair_temps so process()'s finally cleans them up.
        """
        if not getattr(self.cfg, "transcode_dts_cd", True):
            return None
        if audio_path.suffix.lower() != ".wav":
            return None
        rng = self._wav_dts_data_range(audio_path)
        if not rng:
            return None
        offset, size = rng
        es = audio_path.with_suffix(".dtses.tmp")
        dec = audio_path.with_suffix(".dts5p1.wav")
        try:
            logger.info(
                "DTS-CD: %s is a DTS-in-WAV disc image -- decoding to 5.1 with "
                "libdca before split.", audio_path.name,
            )
            with open(audio_path, "rb") as src, open(es, "wb") as dst:
                src.seek(offset)
                remaining = size
                while remaining > 0:
                    block = src.read(min(4_000_000, remaining))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)
            self._repair_temps.add(es)
            if self._dcadec_to_wav(es, dec):
                self._repair_temps.add(dec)
                logger.info("DTS-CD: decoded %s -> %s (5.1 PCM)",
                            audio_path.name, dec.name)
                return dec
            logger.warning("DTS-CD: dcadec could not decode %s", audio_path.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DTS-CD decode failed for %s: %s", audio_path, exc)
        for p in (es, dec):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        return None

    def _transcode_dts_folder(self, folder: Path) -> List[Path]:
        """
        Transcode a DTS-CD folder's raw .dts surround streams into
        channel-preserving (5.1) FLAC so Lidarr can import them, tagging each
        from the filename + folder identity. Idempotent (skips a .dts whose
        .flac already exists) and best-effort (any failure logs, returns what
        succeeded). Returns the FLAC paths created/present.

        Decoding goes through libdca's dcadec (ffmpeg can't frame 14-bit DTS),
        producing a 5.1 WAV that ffmpeg then losslessly re-encodes to FLAC.
        """
        try:
            from tagger import _apply  # lazy: needs mutagen (present in image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DTS-CD: tagger unavailable (%s) -- skipping.", exc)
            return []
        try:
            dts = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() == ".dts"
            )
        except OSError:
            return []
        if not dts:
            return []
        artist, album = self._dts_identity(folder)
        total = str(len(dts))
        made: List[Path] = []
        for p in dts:
            out = p.with_suffix(".flac")
            if out.exists():
                made.append(out)
                continue
            # libdca decode (.dts -> 5.1 wav), then ffmpeg lossless wav -> flac.
            tmpwav = p.with_suffix(".dec.wav")
            if not self._dcadec_to_wav(p, tmpwav):
                logger.warning("DTS-CD: dcadec couldn't decode %s -- skipping.",
                               p.name)
                continue
            cmd = [
                self.cfg.ffmpeg_binary, "-hide_banner", "-loglevel", "warning",
                "-y", "-i", str(tmpwav),
                "-c:a", "flac",
                "-compression_level", str(self.cfg.flac_compression_level),
                "-map_metadata", "-1",
                str(out),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DTS-CD: flac encode failed on %s: %s", p.name, exc)
                if out.exists():
                    out.unlink(missing_ok=True)
                continue
            finally:
                if tmpwav.exists():
                    tmpwav.unlink(missing_ok=True)
            if not ((probe_duration(self.cfg.ffmpeg_binary, out) or 0) > 1.0):
                logger.warning("DTS-CD: %s transcoded but is unreadable.", p.name)
                out.unlink(missing_ok=True)
                continue
            m = re.match(r"\s*(\d+)\s*[-.]?\s*(.*)", p.stem)
            tno = (m.group(1).lstrip("0") or m.group(1)) if m else ""
            title = (m.group(2).strip(" -_.") if m and m.group(2) else p.stem)
            try:
                _apply(out, TagPlan(
                    tracknumber=tno, tracktotal=total, title=title,
                    artist=artist, albumartist=artist, album=album,
                    date="", genre="", comment="DTS-CD 5.1 (transcoded)", isrc="",
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("DTS-CD: tag write failed for %s: %s", out.name, exc)
            made.append(out)
        if made:
            logger.info(
                "DTS-CD: %s -> %d FLAC (5.1) (artist=%r album=%r)%s",
                folder.name, len(made), artist, album,
                "" if (artist and album) else
                " -- identity unknown; rename folder to 'Artist - Album' to import",
            )
        return made

    def _monitored_album_status(self, artist_name: str, album_name: str):
        """
        Decide whether a pre-split download fills a MONITORED GAP in Lidarr.

        Matches the download's artist/album to a Lidarr album by normalized-
        title EQUALITY (via _match_key) -- NOT the fuzzy both-direction
        find_album, which over-matches a different album. Returns:

          ("import",    have, total)  monitored album, incomplete -> import it
          ("redundant", have, total)  monitored album, already complete
          ("skip",      0, 0)         no monitored matching album -> skip clean
          ("unknown",   0, 0)         Lidarr lookup failed -> caller shouldn't
                                      skip (fall through to a normal attempt)

        "skip" is the important one: compilations / live / best-of that the
        metadata profile excludes have no monitored album, so we return early
        instead of handing them to Lidarr just to be rejected.
        """
        artist_name = (artist_name or "").strip()
        album_name = (album_name or "").strip()
        if not artist_name or not album_name:
            return ("unknown", 0, 0)
        try:
            artist = self.lidarr.find_artist(artist_name)
            if not artist:
                return ("skip", 0, 0)
            albums = self.lidarr.list_albums_for_artist(artist["id"]) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gap check: Lidarr lookup failed for %s: %s",
                           artist_name, exc)
            return ("unknown", 0, 0)
        target = _match_key(album_name)
        match = None
        for a in albums:
            if _match_key(a.get("title")) == target:
                match = a
                break
        if match is None:
            return ("skip", 0, 0)
        if not match.get("monitored", True):
            return ("skip", 0, 0)
        aid = match.get("id")
        try:
            full = (self.lidarr.get_album(aid) or {}) if aid else {}
        except Exception:  # noqa: BLE001
            full = {}
        stats = full.get("statistics") or match.get("statistics") or {}
        have = int(stats.get("trackFileCount") or 0)
        total = int(stats.get("totalTrackCount") or 0)
        if total > 0 and have >= total:
            return ("redundant", have, total)
        return ("import", have, total)

    def _drop_duplicate_editions(
        self, eligible: List[Tuple[Path, List[Path]]]
    ) -> set:
        """
        Find folders in the sweep's eligible list that map to the SAME album
        (normalized artist+album from tags) and return the ones to SKIP,
        keeping only the edition whose track count is CLOSEST to Lidarr's
        release. When Lidarr has no count for the album, keep the edition with
        the most tracks (the most complete). Folders whose tags can't be read
        are never grouped -- they pass through untouched for the handoff to
        sort out. Returns a set of folders to skip.
        """
        groups: Dict[str, list] = {}
        for folder, audios in eligible:
            artist = album = ""
            for a in audios:
                artist, album = self._read_audio_tags(a)
                if artist and album:
                    break
            if not (artist and album):
                continue
            key = _match_key(artist) + "\x00" + _match_key(album)
            groups.setdefault(key, []).append(
                (folder, len(audios), artist, album)
            )

        to_skip: set = set()
        for members in groups.values():
            if len(members) < 2:
                continue
            artist, album = members[0][2], members[0][3]
            try:
                _verdict, _have, total = self._monitored_album_status(
                    artist, album
                )
            except Exception:  # noqa: BLE001
                total = 0
            if total and total > 0:
                best = min(members, key=lambda m: abs(m[1] - total))
                crit = f"closest to Lidarr's {total} tracks"
            else:
                best = max(members, key=lambda m: m[1])
                crit = "most tracks (Lidarr count unknown)"
            for folder, n, _ar, _al in members:
                if folder == best[0]:
                    continue
                to_skip.add(folder)
                logger.info(
                    "cueless sweep: %s / %s has %d editions -- keeping %r "
                    "(%d tracks, %s), skipping %r (%d tracks).",
                    artist, album, len(members), best[0].name, best[1],
                    crit, folder.name, n,
                )
                self._record(
                    folder, outcome="skipped_duplicate_edition",
                    pre_split=True, artist=artist, album=album,
                    reason=f"duplicate edition; kept {best[0].name}",
                )
        return to_skip

    def _handoff_pre_split_to_lidarr(
        self,
        cue_path: Optional[Path],
        folder: Path,
        reason: str,
    ) -> None:
        """
        Already-split folder. Drop the orphan .cue (if any), then ask
        Lidarr to import the existing audio files via ManualImport -- NOT
        DownloadedAlbumsScan, because that path parses the folder name
        to find the artist, and pre-split folders are usually named after
        the album, not the artist (so Lidarr logs "Unknown Artist" and
        bails). ManualImport takes explicit artist/album/track IDs, which
        we hydrate from tags + Lidarr lookup.

        `cue_path` is None when this is called from the CUE-less sweep
        (folders without a .cue). In that case we skip the .cue-delete
        step and use the folder itself as the record/skip key.

        Lidarr still fires OnReleaseImport for ManualImport, so your
        post-import encode script runs for these tracks too.

        On successful import we delete the now-empty source folder.
        On failure (Lidarr can't match artist, or no candidates) we leave
        the audio files alone so you can import them manually; the .cue
        (if any) is gone either way.
        """
        # `key_path` stands in for cue_path in ledger + seen-tracking so
        # the CUE-less sweep path has something stable to key off of.
        key_path = cue_path if cue_path is not None else folder

        # 1) Delete the orphan .cue (if any) so it doesn't re-enqueue on restart.
        if cue_path is not None and self.cfg.delete_cue_if_pre_split:
            try:
                if cue_path.exists():
                    cue_path.unlink()
                    logger.info(
                        "Deleted orphan pre-split CUE %s (%s)",
                        cue_path.name, reason,
                    )
            except OSError as exc:
                logger.warning("Could not delete orphan CUE %s: %s", cue_path, exc)
        self._skip_seen.add(key_path)

        # 2) Read artist/album from the audio tags. Folder name is
        #    unreliable (it's usually the album, not the artist).
        audios = self._sibling_audio_files(folder)
        if not audios:
            logger.warning(
                "Pre-split handoff: folder %s has no audio files after .cue "
                "removal -- nothing to import.",
                folder,
            )
            self._record(
                key_path, outcome="skipped_pre_split", pre_split=True,
                reason=f"no audio files in folder ({reason})",
            )
            return
        artist_name, album_name = "", ""
        for a in audios:
            artist_name, album_name = self._read_audio_tags(a)
            if artist_name and album_name:
                break
        # Fingerprint fallback: tags couldn't identify it -> ask AcoustID what
        # the audio actually is (content-based, so garbage/absent tags don't
        # matter). Best-effort: any failure falls through to the give-up below,
        # exactly as before. This is the "import music I don't have, by sound".
        if not (artist_name and album_name) and self.acoustid is not None:
            try:
                ident = self.acoustid.identify_folder([str(a) for a in audios])
            except Exception as exc:  # noqa: BLE001
                logger.warning("AcoustID identify failed for %s: %s", folder, exc)
                ident = None
            if ident and ident.get("artist") and ident.get("album"):
                artist_name = artist_name or ident["artist"]
                album_name = album_name or ident["album"]
                logger.info(
                    "Pre-split handoff: identified via AcoustID artist=%r "
                    "album=%r (%d/%d tracks) for %s",
                    ident["artist"], ident["album"],
                    ident.get("identified", 0), ident.get("total", 0), folder,
                )
        if not (artist_name and album_name):
            logger.warning(
                "Pre-split handoff: could not determine artist/album from "
                "tags for any file in %s -- leaving audio in place.",
                folder,
            )
            self._record(
                key_path, outcome="failed", pre_split=True,
                reason=f"pre-split handoff: tags unreadable ({reason})",
            )
            return
        logger.info(
            "Pre-split handoff: artist=%r album=%r folder=%s",
            artist_name, album_name, folder,
        )

        # 2a) GAP GATE ("fill monitored gaps only"): the decision logic for
        #     messy discography torrents. Only proceed if this folder maps to a
        #     MONITORED album in Lidarr. A folder with no monitored target --
        #     a compilation / live / best-of that the metadata profile excludes
        #     -- is skipped cleanly here, instead of being handed to Lidarr,
        #     rejected ("couldn't find a similar album" / weak match), and left
        #     thrashing on disk. Complete albums fall through to the redundancy
        #     delete (2b); incomplete monitored albums are the gap we import.
        if self.cfg.pre_split_monitored_gap_only:
            verdict, _have, _total = self._monitored_album_status(
                artist_name, album_name
            )
            if verdict == "skip":
                logger.info(
                    "Pre-split: %s / %s has no monitored album in Lidarr "
                    "(compilation/live/best-of, or not in your metadata "
                    "profile) -- skipping handoff, leaving audio in place. (%s)",
                    artist_name, album_name, reason,
                )
                self._record(
                    key_path, outcome="skipped_unmonitored", pre_split=True,
                    artist=artist_name, album=album_name,
                    reason=f"no monitored album target ({reason})",
                )
                return
            # "import" -> fall through and import the gap.
            # "redundant" -> handled by 2b just below (deletes the download).
            # "unknown" (Lidarr lookup failed) -> fall through and attempt as
            #           before, so a transient Lidarr hiccup never drops audio.

        # 2b) Space-saving dedup: if Lidarr already has this album fully in
        #     the library, this download is redundant -- delete it instead
        #     of re-importing. Guarded by a track-count check so we never
        #     delete a LARGER edition (e.g. a 14-track deluxe download) just
        #     because a smaller standard album is complete in the library.
        if self.cfg.pre_check_lidarr_library:
            existing = self._album_already_in_library(artist_name, album_name)
            if existing:
                stats = existing.get("statistics") or {}
                have = int(stats.get("trackFileCount") or 0)
                total = int(stats.get("totalTrackCount") or 0)
                if total >= len(audios):
                    logger.info(
                        "Pre-split: %s / %s already fully in library "
                        "(%d/%d tracks; download has %d) -- deleting download "
                        "to reclaim space.",
                        artist_name, album_name, have, total, len(audios),
                    )
                    self._record(
                        key_path, outcome="already_in_lidarr", pre_split=True,
                        artist=artist_name, album=album_name,
                        reason=f"already in library ({have}/{total} tracks)",
                    )
                    self._skip_seen.add(key_path)
                    if self.cfg.delete_source_folder_on_success:
                        sentinel = (
                            cue_path if cue_path is not None
                            else folder / ".cueless_sweep"
                        )
                        self._delete_source_folder(sentinel)
                    elif self.cfg.delete_originals_on_success:
                        for a in audios:
                            try:
                                a.unlink()
                            except OSError as exc:
                                logger.warning("Could not delete %s: %s", a, exc)
                    return
                logger.info(
                    "Pre-split: %s / %s is in library but with fewer tracks "
                    "(%d) than this download (%d) -- NOT deleting (likely a "
                    "larger edition); proceeding with import.",
                    artist_name, album_name, total, len(audios),
                )

        # 3) Queue-correlation (best-effort).
        download_client_id: Optional[str] = None
        try:
            queue_entry = self.lidarr.queue_find_for(artist_name, album_name)
            if queue_entry:
                download_client_id = queue_entry.get("downloadId")
                logger.info(
                    "Matched Lidarr queue entry id=%s downloadId=%s",
                    queue_entry.get("id"), download_client_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("pre-split queue lookup failed: %s", exc)

        # 4) Probe ManualImport candidates for the folder.
        lidarr_path = self.lidarr.windows_to_lidarr(folder)
        try:
            candidates = self.lidarr.manual_import_candidates(lidarr_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ManualImport probe failed for %s: %s", folder, exc)
            candidates = []
        if not candidates:
            logger.warning(
                "Pre-split handoff: Lidarr returned no ManualImport candidates "
                "for %s -- leaving audio in place.",
                folder,
            )
            self._record(
                key_path, outcome="failed", pre_split=True,
                reason=f"pre-split handoff: no candidates ({reason})",
            )
            return
        self._log_rejections(candidates)
        acceptable = self._filter_acceptable(candidates)
        if not acceptable:
            # Last-resort rescue: Lidarr rejected everything, but if the
            # file count exactly equals a release's track count it's almost
            # certainly the right album (e.g. one file named "(Untitled)"
            # tripping "unmatched tracks"). Force-import by position.
            if self.cfg.force_import_on_count_match:
                if self._try_positional_force_import(
                    cue_path, folder, key_path, artist_name, album_name, audios, reason,
                ):
                    return
                # Tag-based identity failed (common for various-artists / multi-
                # disc comps where track tags list per-track collaborations, so
                # Lidarr can't resolve the album -- e.g. a ".../CD1" whose tracks
                # are each a different artist). Retry with the ALBUM-level
                # artist/name parsed from the folder ('Adamski - Revolt / CD1'
                # -> Adamski / Revolt).
                f_artist, f_album = self._album_folder_identity(folder)
                if (f_artist and f_album
                        and (f_artist, f_album) != (artist_name, album_name)):
                    logger.info(
                        "Pre-split handoff: retrying force-import with "
                        "folder-derived identity artist=%r album=%r for %s",
                        f_artist, f_album, folder,
                    )
                    if self._try_positional_force_import(
                        cue_path, folder, key_path, f_artist, f_album, audios, reason,
                    ):
                        return
            logger.warning(
                "Pre-split handoff: no acceptable candidates (floor=%.0f%%) "
                "for %s -- leaving audio in place.",
                self.cfg.min_match_percent, folder,
            )
            self._record(
                key_path, outcome="failed", pre_split=True,
                reason=f"pre-split handoff: rejections ({reason})",
            )
            return

        # 5) Hydrate IDs from the tags-derived artist/album, then commit.
        hydrated = self._hydrate_candidates(
            acceptable, artist_name, album_name, len(audios)
        )
        committable = [h for h in hydrated if h is not None]
        if not committable:
            logger.warning(
                "Pre-split handoff: could not hydrate any candidates for %s "
                "-- leaving audio in place.",
                folder,
            )
            self._record(
                key_path, outcome="failed", pre_split=True,
                reason=f"pre-split handoff: hydration failed ({reason})",
            )
            return
        logger.info(
            "Pre-split handoff: committing ManualImport for %d/%d files",
            len(committable), len(candidates),
        )
        mi_cmd = self.lidarr.manual_import_apply(committable)
        if mi_cmd is None:
            logger.warning(
                "Pre-split handoff: ManualImport apply returned no command id "
                "for %s.",
                folder,
            )
            self._record(
                key_path, outcome="failed", pre_split=True,
                reason=f"pre-split handoff: apply failed ({reason})",
            )
            return

        # 6) Wait for Lidarr to reach terminal state. We treat its
        #    status=completed + result=successful as ground truth, same
        #    as the main split flow.
        if self._wait_for_manual_import(
            mi_cmd, folder,
            timeout=self.cfg.manual_import_timeout_seconds,
            staging_exts=_ALL_AUDIO_EXTS,
        ):
            remaining = self._sibling_audio_files(folder)
            logger.info(
                "Pre-split handoff succeeded for %s (%d audio files remaining)",
                folder, len(remaining),
            )
            # Queue + folder cleanup (mirrors the main-split post-success path).
            if self.cfg.cleanup_lidarr_queue:
                self._clear_lidarr_queue(artist_name, album_name)
            # Ask Lidarr to refresh its cached view of this artist. Without
            # this, the web UI often shows an empty discography even though
            # the trackfiles are inserted on disk and in the DB -- same
            # reason we do it after the main-split ManualImport path.
            aid: Optional[int] = None
            for h in committable:
                cand_aid = h.get("artistId")
                if cand_aid:
                    aid = int(cand_aid)
                    break
            self._trigger_artist_refresh(artist_name, artist_id=aid)
            # Lidarr reported completed+successful AND the source folder's
            # audio was moved out (staging cleared) -- that IS "the files
            # were moved to the library", so we clean up the source now.
            # The library-reflection check runs only as an ADVISORY nudge
            # (it refreshes Lidarr's cached view and labels the ledger); it
            # no longer BLOCKS cleanup, because Lidarr stores albums under
            # canonical names ("The X" -> "X", curly apostrophes, &/and)
            # that our disk re-match can't always reproduce -- which was
            # wrongly retaining folders whose tracks had really been imported.
            final_outcome = "imported_via_manual"
            if self.cfg.verify_library_after_import:
                if not self._verify_library_reflects_album(
                    artist_name, album_name, len(audios),
                    imported_artist_id=aid,
                ):
                    final_outcome = "imported_unverified"
                    logger.info(
                        "Pre-split handoff for %s / %s: Lidarr moved the files "
                        "but its library view didn't confirm by name -- cleaning "
                        "up the source anyway (files were moved).",
                        artist_name, album_name,
                    )
            self._record(
                key_path, outcome=final_outcome, pre_split=True,
                artist=artist_name, album=album_name,
                reason=f"pre-split handoff ({reason})",
            )
            if self.cfg.delete_source_folder_on_success:
                # _delete_source_folder takes a cue_path and removes its
                # .parent -- for the CUE-less sweep, synthesize a fake
                # child path so the correct folder is targeted.
                sentinel = cue_path if cue_path is not None else folder / ".cueless_sweep"
                self._delete_source_folder(sentinel)
            return

        # ManualImport didn't report clean success within the window. That
        # does NOT mean it failed -- Lidarr frequently completes a queued
        # command shortly after our wait elapses. If we blindly "leave audio
        # in place", the next sweep re-imports the same folder => duplicates.
        # So verify against the library before giving up.
        aid: Optional[int] = None
        for h in committable:
            cand_aid = h.get("artistId") or (h.get("artist") or {}).get("id")
            if cand_aid:
                aid = int(cand_aid)
                break
        if self.cfg.verify_library_after_import and self._verify_library_reflects_album(
            artist_name, album_name, len(audios), imported_artist_id=aid,
        ):
            logger.info(
                "Pre-split handoff: wait window elapsed but Lidarr DID import "
                "%s / %s -- treating as success (avoids duplicate re-import).",
                artist_name, album_name,
            )
            if self.cfg.cleanup_lidarr_queue:
                self._clear_lidarr_queue(artist_name, album_name)
            self._trigger_artist_refresh(artist_name, artist_id=aid)
            self._record(
                key_path, outcome="imported_via_manual", pre_split=True,
                artist=artist_name, album=album_name,
                reason=f"pre-split handoff (verified after wait timeout) ({reason})",
            )
            if self.cfg.delete_source_folder_on_success:
                sentinel = cue_path if cue_path is not None else folder / ".cueless_sweep"
                self._delete_source_folder(sentinel)
            return

        logger.warning(
            "Pre-split handoff: Lidarr did not report clean success for %s "
            "-- audio files left in place for manual import.",
            folder,
        )
        self._record(
            key_path, outcome="failed", pre_split=True,
            artist=artist_name, album=album_name,
            reason=f"pre-split handoff: Lidarr terminal state not ok ({reason})",
        )

    def _looks_pre_split(self, folder: Path) -> bool:
        """
        Heuristic: does this folder already contain split tracks (i.e. no
        disc-image CUE work to do here)?

        A real disc-image folder has ONE big audio file (hundreds of MB)
        and optionally a .cue beside it. A pre-split folder has many
        audio files of similar size, one per track.

        Returns True if there are >= 2 audio files AND the largest is not
        dominant (< 3x the median size). "Dominant" = the classic
        big-disc-image-plus-some-sample-files pattern; if the largest file
        is 10x bigger than the others, treat the folder as containing a
        real disc image and defer to the normal pipeline.
        """
        audios = self._sibling_audio_files(folder)
        if len(audios) < 2:
            return False
        try:
            sizes = sorted((p.stat().st_size for p in audios), reverse=True)
        except OSError:
            return False
        if not sizes or sizes[0] == 0:
            return False
        # Median of all non-zero sizes (skip empty partial downloads).
        non_zero = [s for s in sizes if s > 0]
        if not non_zero:
            return False
        mid = non_zero[len(non_zero) // 2]
        if mid <= 0:
            return False
        # If the biggest file is >= 3x the median, there's a plausible
        # disc-image dominating the folder; let the normal pipeline handle it.
        if sizes[0] >= 3 * mid:
            return False
        return True

    @staticmethod
    def _extract_embedded_cuesheet(path: Path) -> Optional[str]:
        """
        Return an embedded cuesheet's TEXT from an audio file, or None.
        Handles APEv2 'Cuesheet' (WavPack/Monkey's Audio) and the FLAC/Ogg
        Vorbis 'CUESHEET' comment -- the common ways a single-file disc image
        carries its own cue. Only returns text that actually looks like a cue
        (has TRACK + INDEX), so a stray tag can't produce a bogus split.
        """
        try:
            from mutagen import File as MutagenFile
        except Exception:  # noqa: BLE001
            return None
        try:
            mf = MutagenFile(str(path))
            tags = getattr(mf, "tags", None)
            if not tags:
                return None
            for key in list(tags.keys()):
                if str(key).strip().lower() != "cuesheet":
                    continue
                val = tags[key]
                if isinstance(val, list):
                    val = val[0] if val else ""
                text = str(val)
                if "TRACK" in text.upper() and "INDEX" in text.upper():
                    return text.replace("\r\n", "\n").replace("\r", "\n")
        except Exception as exc:  # noqa: BLE001
            logger.debug("embedded cuesheet read failed for %s: %s", path, exc)
        return None

    def _materialize_embedded_cues(
        self, audio_files: List[Path]
    ) -> List[Path]:
        """
        For each audio file that has NO sidecar .cue but DOES carry an embedded
        cuesheet, write '<file>.cue' next to it (with a FILE line pointing at
        the real audio if the embedded cue lacks one -- the orchestrator heals
        the reference otherwise). Returns the .cue paths written.
        """
        written: List[Path] = []
        for af in audio_files:
            sidecar = af.with_suffix(".cue")
            if sidecar.exists():
                continue
            text = self._extract_embedded_cuesheet(af)
            if not text:
                continue
            if not re.search(r'(?im)^\s*FILE\s+".*"', text):
                text = f'FILE "{af.name}" WAVE\n' + text
            try:
                sidecar.write_text(text, encoding="utf-8")
                written.append(sidecar)
                logger.info(
                    "Extracted embedded cuesheet from %s -> %s",
                    af.name, sidecar.name,
                )
            except OSError as exc:
                logger.warning(
                    "Could not write extracted cue for %s: %s", af, exc
                )
        return written

    def sweep_cueless_pre_split_folders(
        self,
        watch_root: Path,
        excluded: Optional[List[Path]] = None,
    ) -> int:
        """
        Walk `watch_root` looking for folders that contain pre-split audio
        (many similarly-sized files) but NO .cue file. The watchdog only
        fires on .cue arrivals, so these folders are invisible to it --
        they sit forever unless Lidarr picks them up through some other
        mechanism. This sweep hands them off to Lidarr via ManualImport.

        Guards:
          * Folder must have no .cue anywhere in it (we don't want to
            double-handle something the normal pipeline will see).
          * Folder must look pre-split per `_looks_pre_split`.
          * Every audio file's mtime must be older than
            `self.cfg.sweep_min_stable_seconds` (so we don't grab an
            in-progress download).
          * Folder must not be in `excluded` or under any excluded path.
          * Folder must not already be in `_skip_seen` (either from an
            earlier sweep this run or from a prior handoff attempt).

        Returns the number of folders handed off (successful or not).
        """
        if not watch_root or not watch_root.exists():
            logger.debug("cueless sweep: watch_root %s doesn't exist", watch_root)
            return 0

        excluded_resolved: List[Path] = []
        for ex in (excluded or []):
            try:
                excluded_resolved.append(Path(ex).resolve(strict=False))
            except OSError:
                continue

        def _is_excluded(folder_r: Path) -> bool:
            for ex in excluded_resolved:
                if folder_r == ex or ex in folder_r.parents:
                    return True
            return False

        min_stable = max(0, int(self.cfg.sweep_min_stable_seconds))
        now_ts = time.time()

        handed_off = 0
        # Folders that pass every guard, collected first so duplicate editions
        # can be de-duplicated before any handoff (see _drop_duplicate_editions).
        eligible: List[Tuple[Path, List[Path]]] = []
        # Directories that contain a .cue (top-down): the .cue path owns their
        # whole subtree, so the sweep won't grab split-staging subfolders in it.
        cue_dirs: set = set()
        logger.info(
            "cueless sweep: scanning %s (min_stable=%ss)",
            watch_root, min_stable,
        )

        # os.walk is friendlier than iterdir recursion on SMB.
        try:
            walker = os.walk(watch_root, topdown=True, followlinks=False)
        except OSError as exc:
            logger.warning("cueless sweep: cannot walk %s: %s", watch_root, exc)
            return 0

        for dirpath, dirnames, filenames in walker:
            folder = Path(dirpath)
            try:
                folder_r = folder.resolve(strict=False)
            except OSError:
                folder_r = folder

            # Prune excluded subtrees early.
            if _is_excluded(folder_r):
                dirnames[:] = []
                continue

            # Skip if we already dealt with this folder this run.
            if folder in self._skip_seen or folder_r in self._skip_seen:
                continue

            # If this folder has any .cue, the normal watcher path owns it --
            # remember the whole subtree so we never grab a split-STAGING
            # subfolder the .cue path creates inside it (that caused a second,
            # concurrent import that collided with the main one and left the
            # album stuck: "Lidarr imported 11 files" but moved zero).
            has_cue = any(
                fn.lower().endswith(".cue") for fn in filenames
            )
            if has_cue:
                cue_dirs.add(folder)
                continue

            # Inside a .cue-owned tree (e.g. the main path's split staging)?
            # Leave it to that path; os.walk is top-down so the ancestor's .cue
            # was already seen.
            if any(anc in cue_dirs for anc in folder.parents):
                continue

            # A lone disc-image file (e.g. .wv/.ape/.flac) may carry its
            # cuesheet EMBEDDED in itself with no sidecar .cue. Extract it to a
            # .cue so the normal split path handles it; the polling watcher then
            # picks the new .cue up. Without this the folder sits unimportable.
            audio_here = [
                folder / fn for fn in filenames
                if Path(fn).suffix.lower() in set(self.cfg.audio_extensions)
            ]
            extracted = self._materialize_embedded_cues(audio_here)
            if extracted:
                logger.info(
                    "cueless sweep: wrote %d .cue(s) from embedded cuesheets in "
                    "%s -- watcher will split them",
                    len(extracted), folder,
                )
                self._skip_seen.add(folder)
                continue

            # DTS-CD: a folder of raw .dts surround streams (no recognized
            # audio yet). Transcode them to channel-preserving FLAC in place;
            # once written (and stable), the normal pre-split path below picks
            # up the FLACs and imports them. Idempotent, so re-running is cheap.
            if (
                self.cfg.transcode_dts_cd
                and not audio_here
                and any(fn.lower().endswith(".dts") for fn in filenames)
            ):
                self._transcode_dts_folder(folder)
                # Fall through: the just-written FLACs are usually caught by the
                # stability guard and imported on the next sweep.

            # Needs to look pre-split.
            if not self._looks_pre_split(folder):
                continue

            # Stability check: every audio file's mtime must be old enough.
            audios = self._sibling_audio_files(folder)
            if not audios:
                continue
            unstable = False
            for a in audios:
                try:
                    age = now_ts - a.stat().st_mtime
                except OSError:
                    unstable = True
                    break
                if age < min_stable:
                    unstable = True
                    break
            if unstable:
                logger.debug(
                    "cueless sweep: %s still has files newer than %ss; skipping",
                    folder, min_stable,
                )
                continue

            logger.info(
                "cueless sweep: found pre-split folder with no .cue: %s (%d audio files)",
                folder, len(audios),
            )
            eligible.append((folder, audios))

        # Edition dedup ("best track-count match"): when several eligible
        # folders in THIS sweep map to the same album (multiple pressings/
        # editions of a discography grab), only hand off the one whose track
        # count is closest to Lidarr's release; skip the rest as redundant.
        # Done before handoff so we never import two editions into one album.
        for folder in self._drop_duplicate_editions(eligible):
            self._skip_seen.add(folder)

        surviving = [f for (f, _a) in eligible if f not in self._skip_seen]
        for folder in surviving:
            try:
                self._handoff_pre_split_to_lidarr(
                    None, folder, reason="cueless sweep",
                )
                handed_off += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "cueless sweep: handoff failed for %s: %s", folder, exc,
                )
                # Mark as seen so we don't retry in a tight loop.
                self._skip_seen.add(folder)

        logger.info(
            "cueless sweep: finished; handed off %d folder(s)", handed_off,
        )
        return handed_off

    def _find_companion_audio(self, cue_path: Path) -> Optional[Path]:
        """Back-compat wrapper: return the top-priority companion audio."""
        cands = self._find_companion_candidates(cue_path)
        return cands[0] if cands else None

    def _find_companion_candidates(self, cue_path: Path) -> List[Path]:
        """
        Locate every plausible disc-image companion for this CUE, in
        priority order (highest priority first, no duplicates).

        We need a LIST (not a single Path) because some disc-image files
        on disk are corrupt or use a codec variant ffmpeg can't decode.
        The caller will probe each candidate in order and fall back to
        the next one when ffprobe/ffmpeg rejects it -- this is the CUE
        auto-repair behaviour for "CUE says foo.wav but foo.wv is
        actually there / foo.wv turned out to be broken so try foo.flac".

        Priority order:
          1. Exact CUE FILE reference (subfolder-aware).
          2. Extension-drift on the CUE FILE stem, ordered by the
             config's `audio_extensions` list so FLAC > APE > WV > WAV.
          3. Audio file named after the CUE's own stem.
          4. Any other decode-able audio sibling in the folder.

        Robust to:
          * CUE FILE refs with backslash or forward-slash subpaths.
          * Multi-FILE CUEs (we collect candidates from every FILE line).
          * CUEs whose declared extension doesn't match what's on disk.
        """
        # Preserve config order so the priority is deterministic across runs.
        exts_order: List[str] = []
        seen_ext: set = set()
        for raw in self.cfg.audio_extensions:
            e = raw.lower()
            if e and e not in seen_ext:
                exts_order.append(e)
                seen_ext.add(e)
        exts = set(exts_order)

        try:
            cue_text = cue_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            cue_text = ""

        file_refs: List[str] = []
        for line in cue_text.splitlines():
            s = line.strip()
            if not s.upper().startswith("FILE "):
                continue
            a = s.find('"')
            b = s.rfind('"')
            if a != -1 and b > a:
                file_refs.append(s[a + 1:b])

        parent = cue_path.parent
        ordered: List[Path] = []
        seen: set = set()

        def _add(p: Path) -> None:
            try:
                key = str(p.resolve(strict=False)).lower()
            except OSError:
                key = str(p).lower()
            if key in seen:
                return
            try:
                if p.is_file():
                    seen.add(key)
                    ordered.append(p)
            except OSError:
                return

        # 1 + 2: honour every FILE reference, then extension-drift on each.
        for ref in file_refs:
            norm = ref.replace("\\", "/")
            basename = norm.rsplit("/", 1)[-1] or norm
            # Literal path (subfolder-aware) first, then same-folder basename.
            for candidate in (parent / norm, parent / basename):
                try:
                    if (
                        candidate.is_file()
                        and candidate.suffix.lower() in exts
                    ):
                        _add(candidate)
                except OSError:
                    continue
            stem = Path(basename).stem
            for ext in exts_order:
                _add(parent / f"{stem}{ext}")

        # 3: audio file named after the CUE itself.
        cue_stem = cue_path.stem
        for ext in exts_order:
            _add(cue_path.with_name(cue_stem + ext))

        # 4: any other audio sibling (single-disc images dropped into the
        # folder with a mismatched stem). Honour the config ext order so
        # FLAC/APE come before WV/WAV even in this last-resort bucket.
        try:
            siblings = list(parent.iterdir())
        except OSError:
            siblings = []
        siblings_by_ext: Dict[str, List[Path]] = {e: [] for e in exts_order}
        for sib in siblings:
            try:
                if not sib.is_file():
                    continue
            except OSError:
                continue
            suf = sib.suffix.lower()
            if suf in siblings_by_ext:
                siblings_by_ext[suf].append(sib)
        for ext in exts_order:
            for sib in sorted(siblings_by_ext[ext], key=lambda p: p.name.lower()):
                _add(sib)

        return ordered

    def _heal_cue_file_reference(self, cue_path: Path, audio_path: Path) -> None:
        """
        Rewrite the CUE's FILE line(s) to the audio we actually resolved,
        when the cue points at a different extension of the SAME name
        (e.g. FILE "foo.wav" but foo.flac is what's on disk). Corrects the
        on-disk .cue so it matches reality. Best-effort; never raises.

        Guards (so we only ever make a favorable, safe repoint):
          * audio must be in the SAME folder as the cue (bare basename);
          * the FILE ref must be a bare name (no sub-path) -- we don't touch
            multi-FILE / subfolder refs;
          * the resolved audio must share the FILE ref's STEM (same name,
            only the extension differs). We never repoint to an unrelated
            file.
        """
        if audio_path.parent != cue_path.parent:
            return
        try:
            text = cue_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        lines = text.splitlines(keepends=True)
        real_name = audio_path.name
        changed = False
        for i, line in enumerate(lines):
            if not line.strip().upper().startswith("FILE "):
                continue
            a = line.find('"')
            b = line.rfind('"')
            if a == -1 or b <= a:
                continue
            current = line[a + 1:b]
            if ("/" in current) or ("\\" in current) or current == real_name:
                continue
            # Favorable-only: same base name, different extension.
            if Path(current).stem.lower() != audio_path.stem.lower():
                continue
            lines[i] = line[:a + 1] + real_name + line[b:]
            changed = True
        if not changed:
            return
        try:
            cue_path.write_text("".join(lines), encoding="utf-8")
            logger.info(
                "Healed CUE FILE reference in %s -> %s (extension drift)",
                cue_path.name, real_name,
            )
        except OSError as exc:
            logger.warning("Could not rewrite CUE %s: %s", cue_path.name, exc)

    def _pick_decodable_companion(
        self, cue_path: Path, candidates: List[Path]
    ) -> tuple[Optional[Path], Optional[float], List[str]]:
        """
        Walk `candidates` in order, probing each with ffprobe. Returns
        (audio_path, duration_seconds, probe_errors). The first candidate
        that reports a positive duration wins. Stability-wait is done
        inline so we don't kick off a probe against a half-written file.
        """
        errors: List[str] = []
        for cand in candidates:
            try:
                if not cand.is_file():
                    errors.append(f"{cand.name}: missing")
                    continue
            except OSError as exc:
                errors.append(f"{cand.name}: stat failed ({exc})")
                continue
            if not self._wait_for_stability(cand):
                errors.append(f"{cand.name}: never stabilized")
                continue
            try:
                duration = probe_duration(self.cfg.ffmpeg_binary, cand)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cand.name}: probe crashed ({exc})")
                continue
            if duration is None or duration < 1.0:
                # ffmpeg couldn't read it. Some rips prepend junk before the
                # real WavPack/APE stream -- trim to the first block and retry.
                repaired = repair_leadin(cand, self.cfg.ffmpeg_binary)
                if repaired is not None:
                    self._repair_temps.add(repaired)
                    rdur = probe_duration(self.cfg.ffmpeg_binary, repaired)
                    if rdur and rdur >= 1.0:
                        logger.info(
                            "Using lead-in-repaired copy of %s for split",
                            cand.name,
                        )
                        return repaired, rdur, errors
                errors.append(
                    f"{cand.name}: ffprobe returned no usable duration "
                    f"({duration!r})"
                )
                continue
            return cand, duration, errors
        return None, None, errors

    def _trigger_artist_refresh(
        self,
        artist_name: str,
        artist_id: Optional[int] = None,
    ) -> None:
        """
        Kick Lidarr to re-scan an artist after ManualImport. Lidarr's
        artist page can otherwise stay stale until its next scheduled
        rescan -- user-visible symptom is "the files are on disk and
        Lidarr says import succeeded, but the album appears empty under
        the artist". RefreshArtist reconciles the DB against disk.

        Best-effort: errors are logged and swallowed.
        """
        try:
            aid = artist_id
            if aid is None and artist_name:
                artist = self.lidarr.find_artist(artist_name)
                if artist:
                    aid = artist.get("id")
            if aid:
                self.lidarr.refresh_artist(aid)
            else:
                logger.debug(
                    "RefreshArtist skipped: no id for artist_name=%r",
                    artist_name,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RefreshArtist post-import failed for %r: %s", artist_name, exc,
            )

    # ---- On-disk ground truth + Lidarr reconciliation ----------------

    def _find_album_on_disk(
        self, artist_name: str, album_name: str,
    ) -> tuple[Optional[Path], List[Path]]:
        """
        Walk `library_root_windows` (e.g. \\PARK\Audio\Music) looking for
        the album folder. Returns (album_folder, audio_files) or
        (None, []) if nothing plausible found.

        Matching is permissive: exact-fold first, then substring either
        direction, then first artist-folder that starts with the same
        first word. Same-ish for albums. This matters because Lidarr /
        our tagger / the folder template can each settle on slightly
        different forms (e.g. "Ebbhead" vs "Ebbhead (2CD Remaster)",
        "AC_DC" vs "AC/DC", "The Beatles" vs "Beatles").
        """
        root = self.cfg.library_root_windows
        if not root:
            return None, []
        try:
            if not root.exists():
                logger.debug("library root %s doesn't exist / not reachable", root)
                return None, []
        except OSError as exc:
            logger.debug("library root %s stat failed: %s", root, exc)
            return None, []

        target_artist = _sanitize_fs(artist_name).strip().lower()
        target_album = _sanitize_fs(album_name).strip().lower()
        if not target_artist or not target_album:
            return None, []

        def _match_folder(folder: Path, target: str) -> Optional[Path]:
            exact: Optional[Path] = None
            substring: Optional[Path] = None
            try:
                children = list(folder.iterdir())
            except OSError:
                return None
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                n = _sanitize_fs(child.name).strip().lower()
                if not n:
                    continue
                if n == target:
                    exact = child
                    break
                if target in n or n in target:
                    if substring is None:
                        substring = child
            return exact or substring

        artist_dir = _match_folder(root, target_artist)
        if artist_dir is None:
            return None, []

        album_dir = _match_folder(artist_dir, target_album)
        if album_dir is None:
            return None, []

        audio_files: List[Path] = []
        try:
            for p in album_dir.rglob("*"):
                try:
                    if p.is_file() and p.suffix.lower() in _ALL_AUDIO_EXTS:
                        audio_files.append(p)
                except OSError:
                    continue
        except OSError:
            pass
        return album_dir, audio_files

    def _nudge_positional_force_import(
        self,
        album_id: int,
        artist_id: int,
        album_dir: Path,
        disk_file_count: int,
    ) -> bool:
        """
        Force Lidarr to recognize files on disk when the normal nudges
        (Rescan / Refresh / DownloadedAlbumsScan) can't bridge a
        count/title mismatch. Finds a release whose trackCount matches
        disk_file_count (+/- 1), flips to it if it isn't already
        monitored, then positional-force-imports: each file pinned to
        one track by sorted order.

        Same logic as the audit Step 3, adapted for the post-split
        post-move path. Returns True if a ManualImport command was
        issued, False otherwise.
        """
        try:
            target_rid = self.lidarr.find_release_matching_track_count(
                album_id, disk_file_count,
            )
            if not target_rid:
                logger.info(
                    "Positional nudge: no release in album id=%s has "
                    "trackCount matching disk=%d; skipping",
                    album_id, disk_file_count,
                )
                return False

            live_alb = self.lidarr.get_album(album_id)
            monitored_rid = None
            for r in (live_alb or {}).get("releases") or []:
                if r.get("monitored"):
                    monitored_rid = r.get("id")
                    break
            if monitored_rid != target_rid:
                logger.info(
                    "Positional nudge: flipping album id=%s to release "
                    "id=%s (trackCount matches disk=%d)",
                    album_id, target_rid, disk_file_count,
                )
                self.lidarr.set_album_monitored_release(album_id, target_rid)
                rcmd = self.lidarr.refresh_artist(artist_id)
                if rcmd:
                    self.lidarr.wait_for_command(
                        rcmd, timeout_seconds=45, poll_interval=1.5,
                    )

            lidarr_path = self.lidarr.library_windows_to_lidarr(album_dir)
            cands = self.lidarr.manual_import_candidates(lidarr_path)
            tracks_all = self.lidarr.list_tracks_for_album(album_id)
            tracks_rel = [
                t for t in tracks_all
                if (t.get("albumReleaseId") == target_rid
                    or not t.get("albumReleaseId"))
            ]
            pos_cmd = self.lidarr.manual_import_positional(
                cands, tracks_rel, album_id, target_rid, artist_id,
            )
            if pos_cmd:
                logger.info(
                    "Positional nudge: dispatched ManualImport cmd=%s "
                    "for album id=%s (%d files)",
                    pos_cmd, album_id, len(tracks_rel),
                )
                return True
            logger.info(
                "Positional nudge: manual_import_positional returned None "
                "for album id=%s (count mismatch or no track ids)",
                album_id,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Positional nudge failed for album id=%s: %s", album_id, exc,
            )
            return False

    def _lidarr_album_is_imported(
        self,
        artist_name: str,
        album_name: str,
        artist_id: Optional[int] = None,
    ) -> tuple[bool, int, int, Optional[int], Optional[int]]:
        """
        Ask Lidarr whether its DB reflects the album as imported.
        Returns (is_imported, have, want, artist_id, album_id) where:
          * is_imported: True if every monitored track hasFile
          * have/want:   imported-track / monitored-track counts
          * artist_id / album_id: useful for follow-up commands

        is_imported is False when we can't even find the artist/album --
        the caller is expected to use that as a signal to rescan/refresh.
        """
        try:
            if artist_id is None and artist_name:
                a = self.lidarr.find_artist(artist_name)
                if a:
                    artist_id = int(a.get("id")) if a.get("id") is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("verify: find_artist %r failed: %s", artist_name, exc)
        if not artist_id:
            return False, 0, 0, None, None

        album_rec: Optional[Dict[str, Any]] = None
        try:
            album_rec = self.lidarr.find_album(artist_id, album_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("verify: find_album %r failed: %s", album_name, exc)
        if not album_rec:
            return False, 0, 0, artist_id, None

        album_id = album_rec.get("id")
        if not album_id:
            return False, 0, 0, artist_id, None

        tracks: List[Dict[str, Any]] = []
        try:
            tracks = self.lidarr.list_tracks_for_album(album_id) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("verify: list_tracks_for_album(%s) failed: %s", album_id, exc)

        want = 0
        have = 0
        for t in tracks:
            if not t.get("monitored", True):
                continue
            want += 1
            if t.get("hasFile"):
                have += 1
        # Fallback: if there are NO monitored tracks in the track list (which
        # happens on partial-release records), trust statistics on the album.
        if want == 0:
            stats = album_rec.get("statistics") or {}
            want = int(stats.get("totalTrackCount") or 0)
            have = int(stats.get("trackFileCount") or 0)

        is_imported = want > 0 and have >= want
        return is_imported, have, want, artist_id, album_id

    def _verify_library_reflects_album(
        self,
        artist_name: str,
        album_name: str,
        expected_tracks: int,
        imported_artist_id: Optional[int] = None,
    ) -> bool:
        """
        Don't declare victory until disk AND Lidarr agree.

        Phase 1: confirm the album folder is physically on disk under
        `library_root_windows`. If it's NOT there, the "successful" import
        put the files somewhere else (or nowhere). Return False so the
        caller can fall through to its own recovery path.

        Phase 2: check Lidarr's DB -- does it show every monitored track
        for that album as hasFile=true? If yes, great, we're done.

        Phase 3: if disk says yes and Lidarr says no, keep nudging:
          * RescanArtist  -- tells Lidarr to re-read disk for THIS artist
          * RefreshArtist -- re-fetches metadata; picks up new albums
          * DownloadedAlbumsScan on the library album folder -- forces
            Lidarr to parse+import anything sitting under that path

        We poll between each nudge; total budget is
        `cfg.lidarr_verify_timeout_seconds`. Negative = wait forever.
        """
        budget = self.cfg.lidarr_verify_timeout_seconds
        infinite = budget < 0
        deadline = None if infinite else time.monotonic() + max(0, budget)

        album_dir, disk_files = self._find_album_on_disk(artist_name, album_name)
        if album_dir is None or not disk_files:
            logger.warning(
                "Post-import check: could NOT find '%s / %s' under library "
                "root %s -- Lidarr's 'success' didn't actually land files "
                "where we expect them.",
                artist_name, album_name, self.cfg.library_root_windows,
            )
            return False
        logger.info(
            "Post-import check: %d file(s) on disk at %s",
            len(disk_files), album_dir,
        )

        artist_id = imported_artist_id
        last_report = 0.0
        refresh_tried = False
        scan_tried = False
        positional_tried = False
        attempt = 0

        while True:
            attempt += 1
            imported, have, want, aid, album_id = self._lidarr_album_is_imported(
                artist_name, album_name, artist_id=artist_id,
            )
            if aid:
                artist_id = aid
            if imported:
                logger.info(
                    "Post-import check: Lidarr reflects %d/%d monitored tracks "
                    "for %s / %s (attempt %d).",
                    have, want, artist_name, album_name, attempt,
                )
                return True

            # Report progress + pick the next nudge.
            logger.info(
                "Post-import check: Lidarr has have=%s want=%s for %s / %s "
                "(artistId=%s albumId=%s) -- files are on disk; nudging Lidarr.",
                have, want, artist_name, album_name, artist_id, album_id,
            )

            # 1) RefreshArtist first (metadata refresh; picks up new
            #    releases and re-scans the artist folder as a side effect).
            #    Previously we tried RescanArtist before this -- that
            #    command doesn't exist in Lidarr and just 500'd.
            if artist_id and not refresh_tried:
                self.lidarr.refresh_artist(artist_id)
                refresh_tried = True
            # 2) Then DownloadedAlbumsScan targeted at the album folder on
            #    disk. This is the "re-import what's here" hammer -- Lidarr
            #    treats the folder as a finished download and parses it.
            elif not scan_tried:
                lidarr_album_path = self.lidarr.library_windows_to_lidarr(album_dir)
                self.lidarr.downloaded_albums_scan_rescan(lidarr_album_path)
                scan_tried = True
            # 3) Positional force-import: when the nudges fail because disk
            #    count != monitored release's trackCount (Modern Talking's
            #    14 disk tracks vs Lidarr wanting 10), look for a release
            #    with matching trackCount, flip to it, and force-map files
            #    to tracks by sorted position. Same mechanism as the audit
            #    Step 3 -- works for albums where Lidarr's fuzzy matcher
            #    can't resolve track titles but counts align.
            elif not positional_tried and album_id and artist_id:
                positional_tried = True
                self._nudge_positional_force_import(
                    album_id=album_id,
                    artist_id=artist_id,
                    album_dir=album_dir,
                    disk_file_count=len(disk_files),
                )
            else:
                # All nudges fired; we're now just waiting for Lidarr to
                # finish digesting them. Loop and keep polling.
                pass

            now = time.monotonic()
            if now - last_report > 30:
                if infinite:
                    logger.info(
                        "Still waiting for Lidarr to reflect %s / %s (no deadline)...",
                        artist_name, album_name,
                    )
                else:
                    remaining = max(0, int(deadline - now))
                    logger.info(
                        "Still waiting for Lidarr to reflect %s / %s (%ds left)...",
                        artist_name, album_name, remaining,
                    )
                last_report = now

            if not infinite and time.monotonic() >= deadline:
                break
            # Gap between checks. Keep it tight early (Lidarr runs rescan
            # fast), stretch as we go on.
            time.sleep(min(30, 5 + attempt * 2))

        logger.warning(
            "Post-import check: Lidarr did NOT reflect %s / %s within %ds "
            "(files are on disk at %s). Final state: have=%s want=%s.",
            artist_name, album_name, budget, album_dir, have, want,
        )
        return False

    # ---- Full-library audit: disk vs Lidarr --------------------------

    _AUDIT_MARKER_SENTINEL = "__AUDIT_FIRST_RUN_COMPLETE__"
    _AUDIT_CSV_HEADER = [
        "timestamp_utc", "artist_folder", "album_folder",
        "artist_name_guess", "album_name_guess",
        "audio_file_count", "discrepancy", "action_taken",
    ]

    def _strip_year_suffix(self, name: str) -> str:
        """'Ebbhead (1991)' -> 'Ebbhead'. Leaves non-matching names alone."""
        return re.sub(r"\s*\(\s*(?:19|20)\d{2}\s*\)\s*$", "", name).strip()

    def _build_lidarr_artist_index(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch every artist from Lidarr ONCE and key them by a normalized
        name so the audit walk can do O(1) lookups instead of calling
        /api/v1/artist per folder. Keys are lower-cased + sanitize_fs'd.
        Artists with empty names are skipped.
        """
        try:
            artists = self.lidarr._get("/api/v1/artist")  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: /api/v1/artist fetch failed: %s", exc)
            return {}
        idx: Dict[str, Dict[str, Any]] = {}
        for a in artists or []:
            # Index under every name we can get: artistName (canonical),
            # sortName (alphabetized form, e.g. "Beatles, The"), and the
            # disambiguation field (rarely useful but cheap). This gives
            # us multiple keys pointing at the same record so cross-script
            # or punctuation quirks can still hit.
            names = [
                a.get("artistName") or "",
                a.get("sortName") or "",
                a.get("disambiguation") or "",
            ]
            if not any(n.strip() for n in names):
                continue
            for nm in names:
                nm = (nm or "").strip()
                if not nm:
                    continue
                key = _match_key(nm)
                if key and key not in idx:
                    idx[key] = a
        return idx

    def _lidarr_lookup_artist(
        self,
        folder_name: str,
        index: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        Find the Lidarr artist record matching a library folder name.
        Same fuzzy rules as `_find_album_on_disk`: exact, then substring
        either direction.
        """
        target = _match_key(folder_name)
        if not target:
            return None
        if target in index:
            return index[target]
        # Substring match either direction (handles "Beatles" vs "The Beatles"
        # after leading-article stripping still differs, or folder-abbreviations).
        # Gate by a minimum length so 2-char noise doesn't spuriously match.
        if len(target) >= 4:
            for key, rec in index.items():
                if len(key) < 4:
                    continue
                if target in key or key in target:
                    return rec
        return None

    def _audit_load_report(self, path: Path) -> tuple[bool, set]:
        """
        Returns (first_run_complete, folders_already_acted).
        first_run_complete is True if the report CSV exists and contains
        the marker row. folders_already_acted is always empty -- we
        retry failed actions on every restart rather than trusting that
        a past row with a non-empty action column actually worked.
        In-process dedup still prevents hammering within a single run.
        """
        first_done = False
        acted: set = set()  # intentionally left empty across restarts
        try:
            if not path.exists():
                return False, acted
        except OSError:
            return False, acted
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rdr = csv.reader(fh)
                for row in rdr:
                    if not row:
                        continue
                    if row[0] == self._AUDIT_MARKER_SENTINEL:
                        first_done = True
                        continue
                    # Historic rows are ignored for dedup -- we retry on
                    # every process start so failed actions (e.g. from an
                    # earlier, buggy act strategy) get a fresh attempt.
        except OSError as exc:
            logger.debug("audit: could not read report %s: %s", path, exc)
        return first_done, acted

    def _audit_append_rows(self, path: Path, rows: List[List[str]]) -> None:
        """Append rows to the audit CSV, creating it (with header) if needed."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with path.open("a", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                if new_file:
                    w.writerow(self._AUDIT_CSV_HEADER)
                for row in rows:
                    w.writerow(row)
        except OSError as exc:
            logger.warning("audit: could not write report %s: %s", path, exc)

    def _audit_write_marker(self, path: Path) -> None:
        """Write the 'first run complete' sentinel row to the report CSV."""
        marker_row = [self._AUDIT_MARKER_SENTINEL] + [""] * (len(self._AUDIT_CSV_HEADER) - 1)
        self._audit_append_rows(path, [marker_row])

    def _library_signature(self) -> str:
        """
        Cheap fingerprint of the library's structure: every artist/album
        folder name + its mtime. Adding a new album, or new files landing in
        an existing album (which bumps that dir's mtime), changes the hash.
        Only stats directories -- no file reads, no Lidarr calls -- so it's
        far cheaper than a full audit. Empty string if the root is
        unreachable (treated as "unknown", never skips).
        """
        root = self.cfg.library_root_windows
        if not root:
            return ""
        try:
            if not root.exists():
                return ""
        except OSError:
            return ""
        h = hashlib.sha1()
        try:
            for artist in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if not artist.is_dir():
                        continue
                    for album in sorted(artist.iterdir(), key=lambda p: p.name.lower()):
                        try:
                            if not album.is_dir():
                                continue
                            st = album.stat()
                            line = f"{artist.name}/{album.name}|{int(st.st_mtime)}\n"
                            h.update(line.encode("utf-8", "replace"))
                        except OSError:
                            continue
                except OSError:
                    continue
        except OSError:
            return ""
        return h.hexdigest()

    def _audit_sig_path(self) -> Optional[Path]:
        rf = self.cfg.library_audit_report_file
        return rf.with_suffix(".sig") if rf else None

    def maybe_audit_library(self) -> int:
        """
        Scheduled entry point: run the disk-vs-Lidarr audit ONLY when the
        library changed since the last run. Compares a cheap dir-signature
        against the stored one; if identical, skips the whole walk. After a
        real audit, stores the post-audit signature so the next cycle
        compares against the settled state.
        """
        if not self.cfg.library_audit_report_file:
            return 0
        sig_path = self._audit_sig_path()
        if self.cfg.library_audit_skip_unchanged:
            sig = self._library_signature()
            last = ""
            try:
                if sig and sig_path and sig_path.exists():
                    last = sig_path.read_text(encoding="utf-8").strip()
            except OSError:
                last = ""
            if sig and sig == last:
                logger.info(
                    "Library audit: no changes since last run "
                    "(signature unchanged) -- skipping walk.",
                )
                return 0
        result = self.audit_library_vs_lidarr()
        # Store the POST-audit signature (the audit may have added albums),
        # so a follow-up cycle with nothing new correctly skips.
        try:
            post = self._library_signature()
            if post and sig_path:
                sig_path.write_text(post, encoding="utf-8")
        except OSError as exc:
            logger.debug("audit: could not persist signature: %s", exc)
        return result

    def audit_library_vs_lidarr(self) -> int:
        """
        Walk `library_root_windows` and flag album folders that Lidarr
        doesn't know about. On the first run (report CSV missing or no
        marker row yet) we ONLY write the report -- it's a dry run. On
        every subsequent run, for each newly-discovered missing folder,
        we trigger a DownloadedAlbumsScan on the album folder (using the
        library_root_lidarr path mapping) and record the action in the
        report.

        The library layout we assume is:

            <library_root_windows>/
                <Artist folder>/
                    <Album folder>/
                        01 - Track.flac
                        02 - Track.flac
                        ...

        Returns the count of discrepancies found this pass.
        """
        root = self.cfg.library_root_windows
        if not root:
            logger.warning(
                "Library audit: library_root_windows not configured; skipping"
            )
            return 0
        try:
            if not root.exists():
                logger.warning(
                    "Library audit: library_root_windows %s is not reachable; "
                    "skipping",
                    root,
                )
                return 0
        except OSError as exc:
            logger.warning("Library audit: stat of %s failed: %s", root, exc)
            return 0

        report_file = self.cfg.library_audit_report_file
        if not report_file:
            logger.warning(
                "Library audit: no report file configured (library_audit_report_file "
                "is null); skipping. Set a path in config.yaml to enable."
            )
            return 0

        first_run_done, already_acted = self._audit_load_report(report_file)
        in_act_mode = first_run_done

        logger.info(
            "Library audit starting: root=%s mode=%s report=%s acted_so_far=%d",
            root, "act" if in_act_mode else "dry-run (first run)",
            report_file, len(already_acted),
        )

        # Reset per-run cycling dedup so every audit run gets a fresh
        # shot at fixing a stubborn album. We still dedup within a
        # single run (so one album isn't cycled twice in the same pass),
        # but we don't carry the dedup across hourly passes -- otherwise
        # a one-time fluke (Lidarr refresh racing, temporary 500, etc.)
        # permanently poisons the album for the rest of the process's
        # lifetime.
        self._audit_cycled_album_ids = set()

        # One bulk fetch of Lidarr's artist list, indexed by sanitized name.
        artist_index = self._build_lidarr_artist_index()
        if not artist_index:
            logger.warning(
                "audit: Lidarr returned no artists (or fetch failed); "
                "every on-disk album will look like a discrepancy. "
                "Aborting this pass."
            )
            return 0

        # Per-artist album index, lazily built when we first need it.
        album_index_by_artist: Dict[int, Dict[str, Dict[str, Any]]] = {}

        def _album_index(artist_id: int) -> Dict[str, Dict[str, Any]]:
            cached = album_index_by_artist.get(artist_id)
            if cached is not None:
                return cached
            try:
                albums = self.lidarr.list_albums_for_artist(artist_id) or []
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "audit: list_albums_for_artist(%s) failed: %s",
                    artist_id, exc,
                )
                albums = []
            built: Dict[str, Dict[str, Any]] = {}
            for a in albums:
                t = (a.get("title") or "").strip()
                if not t:
                    continue
                key = _match_key(t)
                if key and key not in built:
                    built[key] = a
                # Also index by disambig'd title ("Album (Deluxe Edition)")
                # stripped of the parenthetical, so a disk folder without
                # the edition tag still matches.
                bare = re.sub(r"\s*[\(\[][^)\]]+[\)\]]\s*$", "", t).strip()
                if bare and bare != t:
                    bare_key = _match_key(bare)
                    if bare_key and bare_key not in built:
                        built[bare_key] = a
            album_index_by_artist[artist_id] = built
            return built

        def _album_lookup(
            album_name: str, artist_id: int
        ) -> Optional[Dict[str, Any]]:
            """
            Fuzzy match against the per-artist album index. Returns the
            matched album record (Lidarr dict) or None.
            """
            target = _match_key(album_name)
            if not target:
                return None
            idx = _album_index(artist_id)
            rec = idx.get(target)
            if rec is not None:
                return rec
            # Gate substring match by length so trivially-short keys
            # (e.g. single-word eps "Fire") don't over-match.
            if len(target) >= 4:
                for key, candidate in idx.items():
                    if len(key) < 4:
                        continue
                    if target in key or key in target:
                        return candidate
            return None

        discrepancies: List[List[str]] = []
        new_actions: List[tuple] = []  # (album_dir, row_index)
        scanned_artists = scanned_albums = 0
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            artist_children = sorted(root.iterdir())
        except OSError as exc:
            logger.warning("Library audit: cannot iterate %s: %s", root, exc)
            return 0

        # Pre-filter to directories only (excluding hidden/system) so the
        # progress denominator reflects actual artist-folder candidates,
        # not loose files at the library root.
        candidate_artists: List[Path] = []
        for d in artist_children:
            if d.name.startswith(".") or d.name.startswith("$"):
                continue
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            candidate_artists.append(d)
        total_artists = len(candidate_artists)
        logger.info(
            "Library audit: walking %d top-level folders under %s "
            "(Lidarr knows %d artists)",
            total_artists, root, len(artist_index),
        )

        heartbeat_every = 30.0  # seconds
        last_heartbeat = time.time()
        idx = 0

        for artist_dir in candidate_artists:
            idx += 1
            try:
                if not artist_dir.is_dir():
                    continue
            except OSError:
                continue
            scanned_artists += 1
            logger.debug(
                "Library audit: [%d/%d] scanning artist folder %s",
                idx, total_artists, artist_dir.name,
            )

            now = time.time()
            if now - last_heartbeat >= heartbeat_every:
                logger.info(
                    "Library audit progress: %d/%d artist folders, "
                    "%d albums checked, %d discrepancies so far",
                    idx, total_artists, scanned_albums, len(discrepancies),
                )
                last_heartbeat = now

            artist_rec = self._lidarr_lookup_artist(artist_dir.name, artist_index)

            try:
                album_children = sorted(artist_dir.iterdir())
            except OSError as exc:
                logger.debug("audit: cannot iterate %s: %s", artist_dir, exc)
                continue

            for album_dir in album_children:
                try:
                    if not album_dir.is_dir():
                        continue
                except OSError:
                    continue

                # Quick audio file count (non-recursive; album folders
                # are usually flat, occasionally CD1/CD2 subfolders --
                # we fall back to rglob if flat is empty).
                audios = [
                    p for p in album_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in _ALL_AUDIO_EXTS
                ] if album_dir.exists() else []
                if not audios:
                    try:
                        audios = [
                            p for p in album_dir.rglob("*")
                            if p.is_file() and p.suffix.lower() in _ALL_AUDIO_EXTS
                        ]
                    except OSError:
                        audios = []
                if not audios:
                    continue
                scanned_albums += 1

                album_name_guess = self._strip_year_suffix(album_dir.name)
                artist_name_guess = artist_dir.name

                reason: Optional[str] = None
                if artist_rec is None:
                    reason = "artist not in Lidarr"
                else:
                    album_rec = _album_lookup(
                        album_name_guess, int(artist_rec["id"])
                    )
                    if album_rec is None:
                        reason = "album not in Lidarr"
                    else:
                        # Album metadata exists. Do tracks actually exist
                        # in Lidarr's DB? Only flag if Lidarr has ZERO files.
                        # If Lidarr has ANY files for the album, trust it --
                        # don't touch a working album over a small track-count
                        # mismatch (Lidarr is the source of truth for what
                        # belongs; disk may legitimately have extras like
                        # bonus tracks, hidden tracks, or rip artifacts).
                        files_in_lidarr = _album_track_file_count(album_rec)
                        if files_in_lidarr <= 0:
                            # One more live check -- the cached album_rec
                            # may be stale from the index snapshot. Confirm
                            # against Lidarr RIGHT NOW before flagging.
                            try:
                                live = self.lidarr.get_album(int(album_rec["id"]))
                            except Exception:  # noqa: BLE001
                                live = None
                            live_count = _album_track_file_count(live) if live else 0
                            if live_count > 0:
                                # Lidarr actually has it -- index was stale.
                                # Trust Lidarr. No discrepancy.
                                logger.debug(
                                    "audit: %r cached trackFileCount=0 but live=%d; "
                                    "trusting live and skipping",
                                    album_name_guess, live_count,
                                )
                            else:
                                reason = "album in Lidarr but no tracks imported"
                if reason is None:
                    continue

                # Discrepancy! Decide whether to act.
                album_key = str(album_dir)
                action_taken = ""
                if in_act_mode and album_key not in already_acted:
                    try:
                        if artist_rec is not None:
                            aid = int(artist_rec["id"])
                            # Re-check album state LIVE before acting.
                            # The cached index may be stale; if Lidarr now
                            # shows tracks imported, skip -- don't touch
                            # a working album.
                            if album_rec is not None:
                                live = self.lidarr.get_album(int(album_rec["id"]))
                                live_count = (
                                    _album_track_file_count(live) if live else 0
                                )
                                logger.info(
                                    "audit: acting on %r (id=%s) -- "
                                    "live trackFileCount=%d, disk files=%d",
                                    album_rec.get("title"), album_rec.get("id"),
                                    live_count, len(audios),
                                )
                                if live_count > 0:
                                    action_taken = "already-imported-skip"
                                    cmd_id = None
                                    already_acted.add(album_key)
                                    logger.info(
                                        "audit: %r now shows trackFileCount>0 in "
                                        "Lidarr; skipping action",
                                        album_rec.get("title"),
                                    )
                                    raise _AuditSkip()
                            # Green-gate helper. Before ANY mutation (refresh,
                            # release-flip, auto-switch toggle, import), we
                            # re-poll the live album. If Lidarr reports even
                            # one track file, the album is effectively fine
                            # and we must stop -- the user has seen repeated
                            # cases where a working album gets broken by a
                            # follow-up PUT that races with Lidarr's internal
                            # state machine. Raises _AuditSkip to jump out.
                            def _bail_if_green(reason: str) -> None:
                                nonlocal action_taken
                                if album_rec is None:
                                    return
                                try:
                                    l = self.lidarr.get_album(int(album_rec["id"]))
                                except Exception:  # noqa: BLE001
                                    return
                                if l and _album_track_file_count(l) > 0:
                                    already_acted.add(album_key)
                                    action_taken = f"green-skip ({reason})"
                                    logger.info(
                                        "audit: %r is green now (%s); stopping "
                                        "to avoid breaking the artist",
                                        album_rec.get("title"), reason,
                                    )
                                    raise _AuditSkip()

                            _bail_if_green("pre-refresh")
                            self.lidarr.refresh_artist(aid)
                            lidarr_path = self.lidarr.library_windows_to_lidarr(album_dir)
                            # Ask Lidarr what IT would import from this folder.
                            def _pick_importable(cs):
                                return [
                                    c for c in cs
                                    if c.get("path")
                                    and (c.get("album") or {}).get("id")
                                    and (c.get("albumRelease") or {}).get("id")
                                    and any(t.get("id") for t in (c.get("tracks") or []))
                                ]

                            # Step 1: enable "Automatically Switch Release"
                                # (anyReleaseOk=true). Solves most cases -- Lidarr
                                # picks the best-matching release automatically.
                            if album_rec is not None:
                                _bail_if_green("pre-auto-switch")
                                self.lidarr.set_album_auto_switch(
                                    int(album_rec["id"]), True,
                                )
                                _bail_if_green("post-auto-switch")
                                self.lidarr.refresh_artist(aid)

                            _bail_if_green("pre-candidates")
                            cands = self.lidarr.manual_import_candidates(lidarr_path)
                            importable = _pick_importable(cands)

                            # NOTE: "release-cycling" is intentionally gone.
                            # It tried dozens of releases hoping for a
                            # perfect fuzzy match, spammed RefreshArtist,
                            # and broke working artists ("Unable to load
                            # albums"). The positional force-import below
                            # handles the common case (disk-count ==
                            # release-count) without any of that churn.

                            # Final green-gate before ManualImport. If
                            # Lidarr became happy during Step 1, the
                            # import is redundant and may trigger the
                            # "green then red" flapping.
                            _bail_if_green("pre-manual-import")

                            # Step 3: FORCE-MATCH BY POSITION. If we still
                            # have no importable items but the disk file
                            # count matches some release's trackCount, pair
                            # files to tracks by sorted position and force
                            # the import. Handles the "Voces 8 - Twenty"
                            # case where 32 files match 32 tracks but
                            # Lidarr's fuzzy matcher rejects all pairings
                            # (different title spellings, durations, etc.).
                            if not importable and album_rec is not None:
                                album_id = int(album_rec["id"])
                                target_rid = (
                                    self.lidarr.find_release_matching_track_count(
                                        album_id, len(audios),
                                    )
                                )
                                if target_rid:
                                    _bail_if_green("pre-force-release-set")
                                    # Only flip if not already monitored, so
                                    # we don't re-trigger refresh churn.
                                    live_alb = self.lidarr.get_album(album_id)
                                    monitored_rid = None
                                    for r in (live_alb or {}).get("releases") or []:
                                        if r.get("monitored"):
                                            monitored_rid = r.get("id")
                                            break
                                    if monitored_rid != target_rid:
                                        logger.info(
                                            "audit: positional fallback: flipping "
                                            "album %s (%r) to release %s (trackCount "
                                            "matches disk=%d)",
                                            album_id, album_rec.get("title"),
                                            target_rid, len(audios),
                                        )
                                        self.lidarr.set_album_monitored_release(
                                            album_id, target_rid,
                                        )
                                        # Wait for refresh so tracks for the
                                        # newly-monitored release are visible
                                        # to /api/v1/track before we query.
                                        rcmd = self.lidarr.refresh_artist(aid)
                                        if rcmd:
                                            self.lidarr.wait_for_command(
                                                rcmd, timeout_seconds=45,
                                                poll_interval=1.5,
                                            )
                                        _bail_if_green("post-force-release-set")
                                        cands = self.lidarr.manual_import_candidates(
                                            lidarr_path,
                                        )
                                    tracks_all = self.lidarr.list_tracks_for_album(
                                        album_id,
                                    )
                                    # Keep only tracks belonging to the
                                    # target release.
                                    tracks_rel = [
                                        t for t in tracks_all
                                        if (t.get("albumReleaseId") == target_rid
                                            or not t.get("albumReleaseId"))
                                    ]
                                    _bail_if_green("pre-positional-import")
                                    pos_cmd = self.lidarr.manual_import_positional(
                                        cands, tracks_rel, album_id,
                                        target_rid, aid,
                                    )
                                    if pos_cmd:
                                        cmd_id = pos_cmd
                                        action_taken = (
                                            f"ManualImport-positional "
                                            f"cmd={pos_cmd} "
                                            f"files={len(tracks_rel)}"
                                        )
                                        already_acted.add(album_key)
                                        new_actions.append(
                                            (album_dir, len(discrepancies)),
                                        )
                                        raise _AuditSkip()

                            if not importable:
                                # No usable candidates -- log rejections so
                                # the user can see WHY Lidarr refused.
                                rej = []
                                for c in cands:
                                    for r in (c.get("rejections") or []):
                                        rej.append(r.get("reason", ""))
                                action_taken = (
                                    f"no-importable (rejections={rej[:3]})"
                                    if rej else "no-candidates"
                                )
                                cmd_id = None
                            else:
                                cmd_id = self.lidarr.manual_import_apply(importable)
                                action_taken = (
                                    f"ManualImport cmd={cmd_id} files={len(importable)}"
                                    if cmd_id else "manualimport-failed"
                                )
                        else:
                            action_taken = "artist-missing-no-auto-add"
                            cmd_id = None
                        if cmd_id:
                            already_acted.add(album_key)
                        new_actions.append((album_dir, len(discrepancies)))
                    except _AuditSkip:
                        pass  # already-imported short-circuit, action_taken set
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "audit: import of %s failed: %s", album_dir, exc,
                        )
                        action_taken = f"exception: {exc}"

                row = [
                    now_iso,
                    str(artist_dir),
                    str(album_dir),
                    artist_name_guess,
                    album_name_guess,
                    str(len(audios)),
                    reason,
                    action_taken,
                ]
                discrepancies.append(row)
                # Log + persist each discrepancy the moment we find it so
                # the user sees progress in real time and a killed process
                # doesn't lose already-found rows.
                logger.info(
                    "Library audit: discrepancy [%d/%d] artist=%r album=%r "
                    "reason=%s action=%s",
                    idx, total_artists, artist_name_guess, album_name_guess,
                    reason, action_taken or "(dry-run)",
                )
                self._audit_append_rows(report_file, [row])
        if not in_act_mode:
            # First run: record the marker so the NEXT pass enters act mode.
            self._audit_write_marker(report_file)
            logger.info(
                "Library audit (dry run): scanned %d artist folders, %d album "
                "folders; %d discrepancies written to %s. Next pass will act "
                "on new/unacted discrepancies.",
                scanned_artists, scanned_albums, len(discrepancies), report_file,
            )
        else:
            acted_this_pass = sum(1 for d in discrepancies if d[-1] and not d[-1].startswith("exception"))
            logger.info(
                "Library audit: scanned %d artist folders, %d album folders; "
                "%d discrepancies found, %d DownloadedAlbumsScan actions "
                "triggered. Report: %s",
                scanned_artists, scanned_albums, len(discrepancies),
                acted_this_pass, report_file,
            )
        return len(discrepancies)

    def _make_staging_dir(
        self, cue: Cue, cue_path: Path, audio_path: Path
    ) -> Path:
        """
        Build the staging folder.

        * `staging_mode == "in_place"`: split right next to the source CUE.
          We use a temporary "_split" sub-folder while ffmpeg is running
          to keep the directory tidy; it's flattened into the parent once
          we either delete the originals or move to the library.

        * `staging_mode == "separate"`: use the configured staging_root
          with an "Album (Year)" sub-folder name.
        """
        year = (cue.date or "").strip().split("-")[0].strip()
        fields = {
            "album": cue.title or cue_path.stem,
            "artist": cue.performer or "",
            "albumartist": cue.performer or "",
            "year": year,
        }
        try:
            folder_name = self.cfg.album_folder_template.format(**fields)
        except (KeyError, IndexError, ValueError):
            folder_name = fields["album"]

        folder_name = re.sub(r"\s*[\(\[]\s*[-\s,]*\s*[\)\]]", "", folder_name)
        folder_name = re.sub(r"\s{2,}", " ", folder_name).strip(" -_.,")
        safe = "".join(c if c.isalnum() or c in " -._()[]" else "_" for c in folder_name)
        safe = safe.strip().rstrip(". ") or "album"

        if self.cfg.staging_mode == "in_place":
            dirpath = cue_path.parent / safe
        else:
            dirpath = self.cfg.staging_root / safe
        dirpath.mkdir(parents=True, exist_ok=True)
        return dirpath

    def _rename_to_plan(
        self,
        splits: List[SplitResult],
        plans: List[TagPlan],
        cue: Cue,
    ) -> List[SplitResult]:
        """
        Rename split files so they reflect the final TagPlan (important if
        Ollama cleaned up titles). Uses `cfg.filename_template`.
        """
        if len(splits) != len(plans):
            return splits
        new_splits: List[SplitResult] = []
        for split, plan in zip(splits, plans):
            ext = split.output_path.suffix.lstrip(".") or "flac"
            try:
                new_name = self.cfg.filename_template.format(
                    artist=_sanitize_fs(plan.artist),
                    album=_sanitize_fs(plan.album),
                    albumartist=_sanitize_fs(plan.albumartist),
                    number=int(plan.tracknumber) if plan.tracknumber.isdigit() else 0,
                    title=_sanitize_fs(plan.title),
                    ext=ext,
                )
            except (KeyError, IndexError, ValueError) as exc:
                logger.warning("filename_template error: %s -- keeping %s",
                               exc, split.output_path.name)
                new_splits.append(split)
                continue
            new_name = new_name.strip().rstrip(". ")
            new_path = split.output_path.with_name(new_name)
            if new_path == split.output_path:
                new_splits.append(split)
                continue
            try:
                split.output_path.rename(new_path)
                new_splits.append(SplitResult(track=split.track, output_path=new_path))
            except OSError as exc:
                logger.warning("Rename %s -> %s failed: %s",
                               split.output_path.name, new_name, exc)
                new_splits.append(split)
        return new_splits

    def _rollback_staging(self, staging_dir: Path) -> None:
        """Delete a staging folder we created if the pipeline aborted."""
        try:
            if not staging_dir.exists():
                return
            # Only remove if it's inside our staging root -- paranoid safety.
            if self.cfg.staging_root.resolve() not in staging_dir.resolve().parents:
                return
            for entry in staging_dir.iterdir():
                try:
                    if entry.is_file():
                        entry.unlink()
                except OSError:
                    pass
            staging_dir.rmdir()
            logger.info("Rolled back staging folder %s", staging_dir.name)
        except OSError as exc:
            logger.debug("Rollback of %s failed: %s", staging_dir, exc)

    def _clean_staging_dir(self, staging_dir: Path) -> None:
        """
        Delete any files we left inside `staging_dir` (usually partial
        .flac writes from a failed ffmpeg invocation). Unlike
        `_rollback_staging`, this works in BOTH staging modes -- in
        in_place mode the staging folder sits next to the source CUE
        and _rollback_staging refuses to touch it. Keeps the folder
        itself so the caller can reuse it for the next split attempt.
        """
        try:
            if not staging_dir.exists():
                return
            for entry in staging_dir.iterdir():
                try:
                    if entry.is_file():
                        entry.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug(
                        "Could not remove %s during staging cleanup: %s",
                        entry, exc,
                    )
        except OSError as exc:
            logger.debug("Staging cleanup scan of %s failed: %s", staging_dir, exc)

    def _wait_for_lidarr_to_clear(self, staging_dir: Path) -> bool:
        """Poll the staging folder; return True when it's empty."""
        deadline = time.monotonic() + self.cfg.lidarr_grace_seconds
        while time.monotonic() < deadline:
            remaining = [p for p in staging_dir.glob("*.flac")]
            if not remaining:
                return True
            time.sleep(3)
        return False

    def _staging_cleared(
        self, staging_dir: Path, exts: Optional[frozenset] = None
    ) -> bool:
        """
        True if none of the audio files we handed to Lidarr remain in the dir.

        Default (`exts=None`) checks `*.flac` -- correct for the MAIN split
        flow, where staging holds our split .flac output and we must ignore
        any source disc-image (.wv/.ape) sitting beside it in `in_place` mode.

        The pre-split / force-import flows hand Lidarr already-split audio of
        arbitrary format (.mp3/.m4a/.ape/.wv/...), so they pass the broad
        `_ALL_AUDIO_EXTS` set: globbing only `*.flac` there would ALWAYS report
        "cleared" (no .flac present) and defeat the `require_cleared` safety,
        letting us delete a source whose tracks never actually moved.
        """
        try:
            if exts is None:
                return not any(staging_dir.glob("*.flac"))
            for p in staging_dir.iterdir():
                if p.is_file() and p.suffix.lower() in exts:
                    return False
            return True
        except OSError:
            return False

    def _wait_for_lidarr_available(self) -> bool:
        """
        Block until Lidarr answers /api/v1/system/status, up to the
        configured budget. Returns True if reachable (possibly after a
        wait), False if we gave up.

        First ping is immediate -- no sleep -- so the happy path costs
        a single HTTP round-trip. On failure we log once, then poll
        quietly every 15s. A progress heartbeat every 60s keeps long
        waits visibly alive in the log.
        """
        if self.lidarr.ping():
            return True
        budget = self.cfg.lidarr_availability_wait_seconds
        infinite = budget < 0
        if budget == 0 and not infinite:
            logger.warning(
                "Lidarr not reachable and wait-budget is 0; skipping CUE."
            )
            return False
        deadline = None if infinite else time.monotonic() + budget
        logger.warning(
            "Lidarr not reachable at %s -- waiting for it to come back "
            "(%s)",
            self.lidarr.cfg.base_url,
            "no deadline" if infinite else f"up to {budget}s",
        )
        last_report = time.monotonic()
        while infinite or time.monotonic() < deadline:
            time.sleep(15)
            if self.lidarr.ping():
                logger.info("Lidarr is reachable again; resuming.")
                return True
            now = time.monotonic()
            if now - last_report > 60:
                if infinite:
                    logger.info("Still waiting for Lidarr (no deadline)...")
                else:
                    logger.info(
                        "Still waiting for Lidarr (%ds remaining)...",
                        max(0, int(deadline - now)),
                    )
                last_report = now
        logger.error(
            "Gave up waiting for Lidarr after %ds.", budget,
        )
        return False

    def _wait_for_manual_import(
        self, command_id: int, staging_dir: Path, timeout: int,
        require_cleared: bool = True,
        staging_exts: Optional[frozenset] = None,
    ) -> bool:
        """
        Wait for Lidarr's ManualImport command to reach a terminal state
        and report genuine success.

        History: an earlier version returned True as soon as the staging
        folder emptied. That's unsafe -- Lidarr can move files out of
        staging before its command transitions to `completed`, and if
        the import ultimately fails (result=unsuccessful), the files
        don't end up in the library. We then deleted the source folder
        and lost everything. So: we trust Lidarr's own terminal signal.

        Returns True only if status=="completed" AND result=="successful"
        AND the staging folder is actually empty. Anything else is
        treated as a failure and the caller falls through to the manual
        move path, which puts files in the library ourselves.

        `timeout <= 0` means "wait indefinitely" -- useful when Lidarr
        is stuck on a big RefreshArtist and you'd rather hold the job
        than give up.
        """
        terminal = {"completed", "failed", "aborted"}
        infinite = timeout <= 0
        deadline = None if infinite else time.monotonic() + timeout
        if infinite:
            logger.info(
                "ManualImport cmd=%s: waiting indefinitely for Lidarr to reach terminal state",
                command_id,
            )
        last_status = None
        last_report = 0.0
        staging_empty_noted = False
        final_record: dict = {}
        while infinite or time.monotonic() < deadline:
            rec = self.lidarr.command_record(command_id)
            if rec:
                final_record = rec
                status = (rec.get("status") or "").lower()
                if status != last_status:
                    logger.info(
                        "ManualImport cmd=%s status: %s -> %s",
                        command_id, last_status, status,
                    )
                    last_status = status
                if status in terminal:
                    # Command reached a terminal state. Lidarr's own
                    # status+result is ground truth: if it says
                    # completed/successful we trust it, even if the SMB
                    # directory listing for staging hasn't caught up yet.
                    # Over SMB, Lidarr can report the move as done a few
                    # seconds before the remote listing reflects it; we
                    # used to mis-call that a failure and mangle the
                    # source folder.
                    self._log_command_record("ManualImport", rec)
                    result = (rec.get("result") or "").lower()
                    if status == "completed" and result == "successful":
                        cleared = self._staging_cleared(staging_dir, staging_exts)
                        if not cleared:
                            # Grace window for SMB listing lag before we judge.
                            grace_deadline = time.monotonic() + 30
                            while time.monotonic() < grace_deadline:
                                if self._staging_cleared(staging_dir, staging_exts):
                                    cleared = True
                                    break
                                time.sleep(3)
                        # SAFETY: unless the caller expects leftovers (superset
                        # relocation), Lidarr saying "successful" is NOT enough
                        # to delete the source -- the files must actually be
                        # gone from staging. If Lidarr reported success but the
                        # audio is still sitting there, treat it as a failure so
                        # we never delete a source whose tracks didn't move.
                        if require_cleared and not cleared:
                            logger.warning(
                                "ManualImport cmd=%s reported successful but "
                                "staging %s still holds files -- NOT trusting it "
                                "(source will be preserved).",
                                command_id, staging_dir,
                            )
                            return False
                        logger.info(
                            "ManualImport cmd=%s succeeded (staging_cleared=%s)",
                            command_id, cleared,
                        )
                        return True
                    logger.warning(
                        "ManualImport cmd=%s did not succeed (status=%s, "
                        "result=%s) -- treating as failure; falling through "
                        "to manual move",
                        command_id, status, result,
                    )
                    return False

            # Staging going empty while Lidarr is still "started" is
            # informational only -- it does NOT short-circuit the wait.
            # We log it once so you can see Lidarr is making progress,
            # but we keep waiting for the terminal signal.
            if not staging_empty_noted and self._staging_cleared(staging_dir, staging_exts):
                logger.info(
                    "ManualImport cmd=%s: staging folder emptied (Lidarr status=%s) -- "
                    "waiting for command to reach terminal state",
                    command_id, last_status,
                )
                staging_empty_noted = True

            # Progress heartbeat every 30s so long waits don't look frozen.
            now = time.monotonic()
            if now - last_report > 30:
                staging_count = sum(1 for _ in staging_dir.glob("*.flac"))
                if infinite:
                    logger.info(
                        "Waiting on ManualImport cmd=%s (status=%s, staging has %d files, "
                        "no deadline)",
                        command_id, last_status, staging_count,
                    )
                else:
                    remaining = int(deadline - now)
                    logger.info(
                        "Waiting on ManualImport cmd=%s (status=%s, staging has %d files, "
                        "%ds remaining)",
                        command_id, last_status, staging_count, max(0, remaining),
                    )
                last_report = now
            time.sleep(3)
        # Timed out (only reachable when not infinite).
        self._log_command_record("ManualImport", final_record)
        logger.warning(
            "ManualImport cmd=%s did not reach a terminal state within %ds",
            command_id, timeout,
        )
        return False

    def _log_command_record(self, label: str, rec: dict) -> None:
        """
        Log the interesting fields of a Lidarr command record. Lidarr
        doesn't raise HTTP errors for 'I refused to import' cases -- the
        command completes and the reason is buried in message/exception.
        """
        if not rec:
            logger.warning("%s: no command record returned (timed out?)", label)
            return
        status = (rec.get("status") or "").lower()
        msg = rec.get("message") or ""
        exc = rec.get("exception") or ""
        result = rec.get("result") or ""
        logger.info("%s: status=%s result=%s message=%r", label, status, result, msg)
        if exc:
            logger.warning("%s exception: %s", label, exc)
        body = rec.get("body") or {}
        # `completionMessage` sometimes carries the real reason.
        if isinstance(body, dict) and body.get("completionMessage"):
            logger.info("%s completionMessage: %s", label, body["completionMessage"])

    def _log_rejections(self, candidates: list) -> int:
        """
        Log per-file rejections returned by /api/v1/manualimport.
        Returns the number of files that had at least one rejection.
        """
        rejected_count = 0
        for c in candidates:
            rej = c.get("rejections") or []
            name = Path(c.get("path") or "").name or "?"
            if not rej:
                # Show what Lidarr matched so you can sanity-check it.
                artist = (c.get("artist") or {}).get("artistName") or "?"
                album = (c.get("album") or {}).get("title") or "?"
                tracks = c.get("tracks") or []
                logger.info(
                    "  [OK]  %s -> %s / %s (%d track match%s)",
                    name, artist, album, len(tracks), "es" if len(tracks) != 1 else "",
                )
                continue
            rejected_count += 1
            reasons = "; ".join(
                (r.get("reason") or "").strip() for r in rej if r.get("reason")
            )
            logger.warning("  [NO]  %s  reason: %s", name, reasons or rej)
        return rejected_count

    def _manual_move_to_library(
        self, plans: List[TagPlan], splits: List[SplitResult]
    ) -> Optional[Path]:
        if not plans:
            return None
        artist = _sanitize_fs(
            plans[0].albumartist or plans[0].artist or "Unknown Artist"
        ) or "Unknown Artist"
        album_dir = album_folder_name(plans, template=self.cfg.album_folder_template)

        target = self.cfg.library_root_windows / artist / album_dir
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create library folder %s: %s", target, exc)
            return None

        moved = 0
        missing = 0
        for split in splits:
            src = split.output_path
            if not src.exists():
                missing += 1
                continue
            dst = target / src.name
            try:
                shutil.move(str(src), str(dst))
                moved += 1
            except OSError as exc:
                logger.error("Move %s -> %s failed: %s", src, dst, exc)
                return None
        if moved == 0:
            # Typical cause: Lidarr's ManualImport already pulled the
            # files out of staging (somewhere we can't see) and then
            # reported an unsuccessful result. We must NOT declare this
            # a success -- otherwise we'd nuke the source folder and
            # leave an empty shell in the library.
            logger.error(
                "Manual move found 0/%d source files in staging "
                "(Lidarr likely moved them out before failing). "
                "Refusing to claim success -- source folder preserved "
                "so you can recover the originals.",
                missing,
            )
            return None
        if missing:
            logger.warning(
                "Manual move: %d of %d source files were missing "
                "(Lidarr may have moved some); moved the remaining %d to %s",
                missing, len(splits), moved, target,
            )
        else:
            logger.info("Moved %d tracks to %s", moved, target)
        return target

    # ---- ManualImport: match-% override -------------------------------

    _MATCH_RE = re.compile(
        r"(\d+(?:\.\d+)?)\s*%\s*vs\s*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
    )

    # Rejections we'll forgive when the album match % clears the floor. These
    # all mean "right album, wrong track count" -- a superset (extra tracks) or
    # a partial (a few missing). Anything else (missing artist, no album match,
    # permissions) stays a hard fail.
    _OVERRIDABLE_REJECTIONS = (
        "not close enough",
        "unmatched tracks",
        "missing tracks",
    )

    def _filter_acceptable(self, candidates: list) -> list:
        """
        Decide which manual-import candidates we'll commit.
        A candidate is 'acceptable' if:
          - no rejections at all, OR
          - every rejection is an "overridable" one (not-close-enough / has
            unmatched tracks / has missing tracks), AND the album match % (when
            Lidarr reports one) is >= cfg.min_match_percent. When no % is given
            we only override if Lidarr still resolved the album, so an extra- or
            missing-track batch imports its matches instead of being dropped
            wholesale on the 80% rule.
        Anything else (missing artist, no album match, permissions, etc.) is a
        hard fail.
        """
        floor = self.cfg.min_match_percent
        ok: list = []
        for c in candidates:
            rej = c.get("rejections") or []
            if not rej:
                ok.append(c)
                continue
            override = True
            seen_pct = None
            for r in rej:
                reason = (r.get("reason") or "")
                low = reason.lower()
                if not any(k in low for k in self._OVERRIDABLE_REJECTIONS):
                    override = False
                    break
                m = self._MATCH_RE.search(reason)
                if m:
                    pct = float(m.group(1))
                    seen_pct = pct if seen_pct is None else min(seen_pct, pct)
                    if pct < floor:
                        override = False
                        break
            # Guard: if Lidarr never reported a match %, only override when it
            # actually resolved the album (so we don't blind-cram a compilation).
            if override and seen_pct is None and not (c.get("album") or {}).get("id"):
                override = False
            if override:
                path_name = Path(c.get("path") or "").name or "?"
                # Pull the actual % from the last rejection message so we
                # log "match 63% passes floor 60%" rather than hiding it.
                actual_str = "?"
                for r in rej:
                    m = self._MATCH_RE.search(r.get("reason") or "")
                    if m:
                        actual_str = m.group(1)
                        break
                logger.info(
                    "  [OVERRIDE] %s -- match %s%% passes floor %.0f%%, "
                    "forcing import despite Lidarr's 80%% rule",
                    path_name, actual_str, floor,
                )
                ok.append(c)
        return ok

    # ---- ManualImport hydration --------------------------------------

    @staticmethod
    def _candidate_release_id(c: dict) -> Optional[int]:
        """
        Pull the album-release id from a probe candidate, tolerating both
        Lidarr shapes: a nested `albumRelease: {id}` object or a flat
        `albumReleaseId` int.
        """
        rid = (c.get("albumRelease") or {}).get("id")
        return rid or c.get("albumReleaseId")

    def _candidate_is_complete(self, c: dict) -> bool:
        """
        True when Lidarr's own probe already resolved every field that
        manual_import_apply requires (artist/album/release IDs + at least
        one track ID). These are clean matches -- we must never discard or
        re-derive them.
        """
        if not (c.get("artist") or {}).get("id"):
            return False
        if not (c.get("album") or {}).get("id"):
            return False
        if not self._candidate_release_id(c):
            return False
        return any(t.get("id") for t in (c.get("tracks") or []))

    @staticmethod
    def _title_key(s: str) -> str:
        """Normalize a title for fuzzy comparison: lowercase, alnum only."""
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    def _read_audio_title(self, path: Path) -> str:
        """
        Best-effort track title from tags, falling back to the trailing
        segment of a '<Artist> - <Album> - NN - <Title>' filename.
        """
        try:
            from mutagen import File as MutagenFile  # lazy import
        except Exception:  # noqa: BLE001
            MutagenFile = None  # type: ignore
        title = ""
        if MutagenFile is not None:
            try:
                mf = MutagenFile(str(path))
                if mf is not None and mf.tags:
                    for k in ("title", "TITLE", "\xa9nam", "TIT2"):
                        val = mf.tags.get(k)
                        if not val:
                            continue
                        if isinstance(val, list) and val:
                            val = val[0]
                        title = str(val).strip()
                        if title:
                            break
            except Exception as exc:  # noqa: BLE001
                logger.debug("title read failed for %s: %s", path.name, exc)
        if not title:
            parts = [p.strip() for p in path.stem.split(" - ")]
            title = parts[-1] if parts else path.stem
        return title

    def _match_track_by_title(
        self, path: Optional[str], tracks_rec: list, floor: float = 0.6
    ) -> Optional[dict]:
        """
        Map a file to its Lidarr track by fuzzy TITLE similarity, not by
        position. The ManualImport probe returns files in arbitrary order
        (see the out-of-order candidate logs), so positional mapping can
        silently mis-file tracks. Returns None if nothing clears `floor`
        -- we'd rather drop a file than tag it as the wrong song.
        """
        if not path:
            return None
        file_key = self._title_key(self._read_audio_title(Path(path)))
        if not file_key:
            return None
        best: Optional[dict] = None
        best_score = 0.0
        for t in tracks_rec:
            tk = self._title_key(t.get("title") or "")
            if not tk:
                continue
            score = difflib.SequenceMatcher(None, file_key, tk).ratio()
            if score > best_score:
                best, best_score = t, score
        if best is not None and best_score >= floor:
            return best
        return None

    def _hydrate_candidates(
        self,
        candidates: list,
        artist_name: str,
        album_name: str,
        cue_track_count: int,
    ) -> list:
        """
        Return a list the same length as `candidates`. Each entry is:
          * the candidate untouched, if Lidarr's probe already resolved it
            (a clean match -- see _candidate_is_complete); OR
          * the candidate with artist/album/release/track IDs filled in
            from a Lidarr lookup, for the match-% override path where
            Lidarr returned null IDs on purpose; OR
          * None, if it's incomplete and we can't confidently rescue it.

        Fixes two historical bugs:
          1. A failed batch lookup used to return [None]*N, discarding even
             the clean matches Lidarr had already resolved. Now clean
             matches always pass through untouched.
          2. Rescued files used to be mapped to tracks by list position,
             but the probe returns files out of order -- so tracks got
             mis-filed. Now we map by fuzzy title match and drop anything
             we can't match, rather than guessing.
        """
        # 1) Split clean matches (pass-through) from ones needing rescue.
        def _passthrough(c: dict) -> dict:
            # Clean match: keep as-is, but normalize a flat albumReleaseId
            # into the albumRelease{id} shape manual_import_apply expects.
            item = dict(c)
            if not (item.get("albumRelease") or {}).get("id"):
                rid = self._candidate_release_id(item)
                if rid:
                    item["albumRelease"] = {"id": rid}
            return item

        needs_rescue = [
            i for i, c in enumerate(candidates)
            if not self._candidate_is_complete(c)
        ]
        if not needs_rescue:
            return [_passthrough(c) for c in candidates]

        # 2) Only now do the (fragile, folder-name-based) Lidarr lookup --
        #    and only to rescue the incomplete candidates. Its failure must
        #    NOT affect the clean matches.
        artist_rec = self.lidarr.find_artist(artist_name) if artist_name else None
        album_rec = (
            self.lidarr.find_album(artist_rec["id"], album_name)
            if (artist_rec and album_name) else None
        )
        tracks_rec: list = []
        release_id: Optional[int] = None
        if album_rec:
            releases = album_rec.get("releases") or []
            if not releases:
                full = self.lidarr.get_album(album_rec["id"])
                if full:
                    album_rec = full
                    releases = full.get("releases") or []
            chosen = None
            for r in releases:
                if r.get("trackCount") == cue_track_count:
                    chosen = r
                    break
            if chosen is None:
                for r in releases:
                    if r.get("monitored"):
                        chosen = r
                        break
            if chosen is None and releases:
                chosen = releases[0]
            if chosen:
                release_id = chosen.get("id")
            tracks_rec = self.lidarr.list_tracks_for_album(album_rec["id"])
            if release_id:
                same_rel = [
                    t for t in tracks_rec
                    if t.get("albumReleaseId") == release_id
                ]
                if same_rel:
                    tracks_rec = same_rel

        rescue_possible = bool(artist_rec and album_rec and release_id and tracks_rec)
        if not rescue_possible:
            logger.warning(
                "Could not hydrate override candidates: artist=%s album=%s "
                "release_id=%s tracks=%d (clean matches unaffected)",
                bool(artist_rec), bool(album_rec), release_id, len(tracks_rec),
            )

        hydrated: list = []
        for c in candidates:
            item = dict(c)  # shallow copy so we don't mutate the probe result
            if self._candidate_is_complete(item):
                hydrated.append(_passthrough(c))  # clean match -- never discard
                continue
            if not rescue_possible:
                hydrated.append(None)
                continue
            if not (item.get("artist") or {}).get("id"):
                item["artist"] = artist_rec
            if not (item.get("album") or {}).get("id"):
                item["album"] = album_rec
            if not (item.get("albumRelease") or {}).get("id"):
                item["albumRelease"] = {"id": release_id}
            if not any(t.get("id") for t in (item.get("tracks") or [])):
                matched = self._match_track_by_title(item.get("path"), tracks_rec)
                if matched is None:
                    logger.warning(
                        "Hydrate: no confident title match for %s -- refusing "
                        "to force a positional guess; dropping this file.",
                        Path(item.get("path") or "").name or "?",
                    )
                    hydrated.append(None)
                    continue
                item["tracks"] = [matched]
            hydrated.append(item)
        return hydrated

    # ---- Lidarr queue cleanup ----------------------------------------

    def _clear_lidarr_queue(self, artist_name: str, album_name: str) -> None:
        """
        Remove 'Completed item still waiting' style entries from Lidarr's
        download-client queue. Matching is conservative: artist name or
        folder contains the album name. We never blocklist.
        """
        try:
            entries = self.lidarr.queue_list()
        except Exception as exc:
            logger.debug("queue_list failed: %s", exc)
            return
        if not entries:
            return
        target_artist = (artist_name or "").strip().lower()
        target_album = (album_name or "").strip().lower()
        removed = 0
        for e in entries:
            title = (e.get("title") or "").lower()
            output_path = (e.get("outputPath") or "").lower()
            e_artist = ((e.get("artist") or {}).get("artistName") or "").lower()
            match_artist = target_artist and (
                target_artist == e_artist or target_artist in title
            )
            match_album = target_album and (
                target_album in title or target_album in output_path
            )
            # Require BOTH: matching artist alone would sweep away sibling
            # albums of the same artist that are still downloading (e.g. a
            # discography grab) -- removing their Lidarr queue entry breaks
            # their auto-import. Only clear the entry for THIS album.
            if match_artist and match_album:
                qid = e.get("id")
                if qid is None:
                    continue
                if self.lidarr.queue_remove(qid, blocklist=False):
                    removed += 1
                    logger.info("Cleared queue item %s: %s", qid, e.get("title"))
        if removed:
            logger.info("Removed %d entries from Lidarr queue", removed)

    # ---- Queue reaper -------------------------------------------------

    @staticmethod
    def _classify_queue_row(row: Dict[str, Any]) -> str:
        """
        Classify one Lidarr queue row for reaping purposes:

          "active"   -- still downloading, or Lidarr is actively (and
                        healthily) importing it. NEVER reap; blocks the
                        whole torrent from being reaped.
          "stuck"    -- fully downloaded but Lidarr will not import it
                        (warning/error, import blocked/failed, or the
                        grab never mapped to an artist/album). Reapable.
          "imported" -- Lidarr imported it (native cleanup handles it).
        """
        status = (row.get("status") or "").lower()
        state = (row.get("trackedDownloadState") or "").lower()
        tstat = (row.get("trackedDownloadStatus") or "").lower()
        sizeleft = row.get("sizeleft") or 0

        # Not finished downloading yet -> always active (never touch an
        # in-progress download; this is what protected the 9%-downloaded
        # Thalia grab).
        finished = sizeleft == 0 or status in ("completed", "warning", "failed")
        if not finished:
            return "active"

        if state == "imported":
            return "imported"
        # Lidarr flags a problem it will not resolve on its own.
        if tstat in ("warning", "error"):
            return "stuck"
        if state in ("importblocked", "importfailed", "failedpending", "failed"):
            return "stuck"
        # Grab that never mapped to a library artist/album -- Lidarr can't
        # import what it can't identify.
        if row.get("artist") is None and row.get("album") is None:
            return "stuck"
        # Finished + healthy + Lidarr still working on it (pending/importing).
        if state in ("importpending", "importing", "downloading"):
            return "active"
        # Finished, healthy, identified, but idle: be conservative and let
        # Lidarr's own completed-download handling take it.
        return "active"

    def _load_reaper_state(self) -> Dict[str, float]:
        path = self.cfg.queue_reaper_state_file
        if not path:
            return {}
        try:
            import json
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {str(k): float(v) for k, v in (data or {}).items()}
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("reaper state load failed: %s", exc)
            return {}

    def _save_reaper_state(self, state: Dict[str, float]) -> None:
        path = self.cfg.queue_reaper_state_file
        if not path:
            return
        try:
            import json
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reaper state save failed: %s", exc)

    def reap_lidarr_queue(self) -> int:
        """
        Remove fully-downloaded-but-stuck torrents from Lidarr's queue (and,
        per config, from the download client). This is the scale valve for
        mass discography imports: Lidarr's native cleanup only reaps a
        SUCCESSFUL import, leaving compilations / unmatched grabs / title
        mismatches to pile up forever. Returns the number of queue rows
        removed.

        Grouped by downloadId (one torrent -> many queue rows). A torrent is
        reaped only when:
          * no row is still active (downloading or healthily importing), and
          * at least one row is stuck, and
          * it has been continuously stuck for >= grace minutes.
        """
        from collections import defaultdict

        try:
            entries = self.lidarr.queue_list()
        except Exception as exc:  # noqa: BLE001
            logger.debug("reaper: queue_list failed: %s", exc)
            return 0
        if not entries:
            # Nothing queued -- clear any stale grace clocks.
            self._save_reaper_state({})
            return 0

        groups: Dict[str, list] = defaultdict(list)
        for r in entries:
            dlid = r.get("downloadId") or f"__row_{r.get('id')}"
            groups[str(dlid)].append(r)

        now = time.time()
        grace = max(0, int(self.cfg.queue_reaper_grace_minutes)) * 60
        state = self._load_reaper_state()
        new_state: Dict[str, float] = {}
        to_reap: list = []

        for dlid, rows in groups.items():
            classes = [self._classify_queue_row(r) for r in rows]
            if "active" in classes or "stuck" not in classes:
                # In progress, or nothing stuck -- reset this torrent's clock.
                continue
            first_seen = state.get(dlid, now)
            new_state[dlid] = first_seen  # carry the clock forward
            waited = now - first_seen
            if waited >= grace:
                to_reap.append((dlid, rows))
            else:
                logger.debug(
                    "reaper: %s stuck %ds/%ds -- waiting out grace",
                    dlid[:16], int(waited), grace,
                )

        removed = 0
        for dlid, rows in to_reap:
            label = (rows[0].get("title") or "?")
            logger.info(
                "reaper: reaping stuck torrent %s (%d queue row(s)): %r "
                "[remove_from_client=%s]",
                dlid[:16], len(rows), label,
                self.cfg.queue_reaper_remove_from_client,
            )
            for r in rows:
                qid = r.get("id")
                if qid is None:
                    continue
                if self.lidarr.queue_remove(
                    qid,
                    remove_from_client=self.cfg.queue_reaper_remove_from_client,
                    blocklist=self.cfg.queue_reaper_blocklist,
                ):
                    removed += 1
            new_state.pop(dlid, None)  # gone -- drop its clock

        self._save_reaper_state(new_state)
        if removed:
            logger.info("reaper: removed %d stuck queue row(s)", removed)
        return removed

    # ---- Originals / ledger -------------------------------------------

    def _tracks_present_in_library(
        self, artist_name: str, album_name: str, expected_tracks: int
    ) -> bool:
        """
        Ground-truth check that an album's tracks are physically present in
        the library folder on disk -- independent of whether Lidarr's DB
        knows about them. This is the gate for source deletion: we only
        remove a source once its tracks are confirmed moved into the
        library, with or without Lidarr.

        Requires at least `expected_tracks` audio files in the matched
        library folder, so a partial move (some tracks failed to land)
        never authorizes deleting the source.
        """
        if not (artist_name and album_name):
            return False
        album_dir, disk_files = self._find_album_on_disk(artist_name, album_name)
        if album_dir is None or not disk_files:
            return False
        if expected_tracks > 0 and len(disk_files) < expected_tracks:
            logger.info(
                "Library holds only %d/%d expected tracks for %s / %s -- "
                "treating as incomplete; source NOT cleaned.",
                len(disk_files), expected_tracks, artist_name, album_name,
            )
            return False
        logger.info(
            "Library confirmed: %d/%d tracks on disk for %s / %s -- source "
            "cleanup authorized.",
            len(disk_files), expected_tracks, artist_name, album_name,
        )
        return True

    def _delete_originals(self, cue_path: Path, audio_path: Path) -> None:
        """
        Permanently remove the source CUE + the big disc image after a
        successful import. Only called after we're sure the split files
        are either in the library or accepted by Lidarr.
        """
        for src in (cue_path, audio_path):
            if not src or not src.exists():
                continue
            try:
                src.unlink()
                logger.info("Deleted original %s", src.name)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", src, exc)

    def _record(
        self,
        cue_path: Path,
        outcome: str,
        pre_split: bool,
        reason: str = "",
        artist: str = "",
        album: str = "",
        track_count: int = 0,
    ) -> None:
        """Append one row to the ledger CSV. Safe if ledger is disabled."""
        path = self.cfg.ledger_file
        if not path:
            return
        row = [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            str(cue_path),
            artist,
            album,
            str(track_count),
            "pre_split" if pre_split else "split_by_us",
            outcome,
            reason[:300],
        ]
        try:
            with self._ledger_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                new_file = not path.exists()
                with path.open("a", encoding="utf-8", newline="") as fh:
                    w = csv.writer(fh)
                    if new_file:
                        w.writerow([
                            "timestamp_utc", "cue_path", "artist", "album",
                            "tracks", "source_kind", "outcome", "reason",
                        ])
                    w.writerow(row)
        except OSError as exc:
            logger.debug("Ledger write failed: %s", exc)

    def _cleanup_empty(self, staging_dir: Path) -> None:
        try:
            if staging_dir.exists() and not any(staging_dir.iterdir()):
                staging_dir.rmdir()
        except OSError:
            pass

    def _already_in_library(self, cue: Cue) -> Optional[Dict[str, Any]]:
        """CUE-based wrapper around _album_already_in_library()."""
        return self._album_already_in_library(
            (cue.performer or "").strip(), (cue.title or "").strip()
        )

    def _album_already_in_library(
        self, artist_name: str, album_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return the Lidarr album record if `artist_name` + `album_name` is
        already present in the library with its files, else None.

        "Present" means: Lidarr knows the artist, knows the album, and
        every monitored track has an imported file. We verify that two
        ways because different Lidarr builds are inconsistent about
        populating `statistics` on list responses:

          1) fetch /api/v1/album/{id}       -> stats.trackFileCount
          2) fetch /api/v1/track?albumId=X  -> count tracks where hasFile

        If EITHER says 100% imported (with >0 tracks), we treat the
        album as already in the library. The returned record carries a
        patched `statistics` dict so callers can read the real counts.

        All Lidarr errors are treated as "not present" so the pipeline
        falls through to normal processing when the API is unreachable.
        """
        artist_name = (artist_name or "").strip()
        album_name = (album_name or "").strip()
        if not artist_name or not album_name:
            return None
        try:
            artist = self.lidarr.find_artist(artist_name)
        except Exception as exc:
            logger.warning("Pre-flight: find_artist failed: %s", exc)
            return None
        if not artist:
            return None
        try:
            album = self.lidarr.find_album(artist["id"], album_name)
        except Exception as exc:
            logger.warning("Pre-flight: find_album failed: %s", exc)
            return None
        if not album:
            return None

        # GUARD against find_album's fuzzy both-direction substring match.
        # It returns a hit when EITHER title contains the other ("pearl" in
        # "rare pearls"), so it will happily return a DIFFERENT, complete album
        # we own for a download we don't. This decision is DESTRUCTIVE -- a
        # positive deletes the download WITHOUT importing -- so we only accept
        # it when the download's normalized tokens are a subset of the owned
        # album's tokens (owned is the same album, or a more-complete edition:
        # download "Ebbhead" vs owned "Ebbhead (2CD)"). The dangerous direction
        # -- owned title is a subset of the download ("Pearl" ⊂ "Rare Pearls",
        # "Hits" ⊂ "Greatest Hits") -- is rejected: re-importing something we
        # already own is merely wasteful, but deleting something we DON'T own
        # is data loss.
        ka = _match_key(album.get("title"))
        kb = _match_key(album_name)
        if ka != kb:
            owned_words = set(ka.split())
            dl_words = set(kb.split())
            if not (dl_words and owned_words and dl_words <= owned_words):
                logger.info(
                    "Pre-flight: find_album returned %r for download %r but the "
                    "titles are not a safe match -- NOT treating as owned "
                    "(letting normal import decide).",
                    album.get("title"), album_name,
                )
                return None

        album_id = album.get("id")
        # --- Source 1: per-album stats via the detail endpoint.
        # The list endpoint frequently omits `statistics`, so the list-
        # response stats alone are unreliable.
        stats_file = stats_total = 0
        if album_id:
            full = self.lidarr.get_album(album_id) or {}
            stats = full.get("statistics") or album.get("statistics") or {}
            stats_file = int(stats.get("trackFileCount") or 0)
            stats_total = int(stats.get("totalTrackCount") or 0)

        # --- Source 2: per-track hasFile, which is always reliable.
        track_file = track_total = 0
        if album_id:
            tracks = self.lidarr.list_tracks_for_album(album_id) or []
            for t in tracks:
                if not t.get("monitored", True):
                    # Ignore unmonitored tracks when deciding completeness;
                    # if Lidarr isn't tracking them, they don't count for
                    # or against our "already imported" decision.
                    continue
                track_total += 1
                if t.get("hasFile"):
                    track_file += 1

        def _complete(have: int, want: int) -> bool:
            return have > 0 and want > 0 and have >= want

        if _complete(stats_file, stats_total) or _complete(track_file, track_total):
            logger.info(
                "Pre-flight match: %s / %s (stats=%s/%s, tracks=%s/%s)",
                artist_name, album_name,
                stats_file, stats_total, track_file, track_total,
            )
            # Patch statistics back onto the record we return so the
            # caller's logging shows real counts.
            album.setdefault("statistics", {}).update({
                "trackFileCount": stats_file or track_file,
                "totalTrackCount": stats_total or track_total,
            })
            return album

        # Log the near-miss so you can tell WHY the pre-check decided
        # not to skip -- helps diagnose fuzzy-match / partial-import cases.
        logger.info(
            "Pre-flight miss for %s / %s: stats=%s/%s tracks=%s/%s -- "
            "proceeding with split+import",
            artist_name, album_name,
            stats_file, stats_total, track_file, track_total,
        )
        return None

    def _delete_source_folder(self, cue_path: Path) -> None:
        """
        Recursively remove the folder that contained the source CUE --
        scans/, rip logs, .nfo, .m3u, artwork, the whole lot. Then walk
        UP the tree removing any empty parent folders (e.g. an artist
        directory left behind after its only album was cleaned out),
        stopping at the watch root which is never touched.

        Only intended to run in the success branch; callers are
        responsible for that sequencing.

        Safety: refuses to operate on
          * a path that does not resolve under the configured watch
            root (prevents a spooky Path somehow escaping the tree),
          * the watch root itself,
          * a drive root or UNC share root (no parent -> bail).
        """
        folder = cue_path.parent
        watch_root = self.cfg.watch_root

        try:
            folder_r = folder.resolve(strict=False)
        except OSError:
            folder_r = folder

        if watch_root is None:
            logger.warning(
                "Source-folder cleanup skipped: watch_root is not configured."
            )
            return

        try:
            watch_root_r = watch_root.resolve(strict=False)
        except OSError:
            watch_root_r = watch_root

        logger.info("Source-folder cleanup: target=%s (watch_root=%s)",
                    folder_r, watch_root_r)

        if folder_r == watch_root_r:
            logger.info(
                "Source-folder cleanup skipped: CUE is at the watch root. "
                "Deleting originals only.",
            )
            return

        try:
            if watch_root_r not in folder_r.parents:
                logger.warning(
                    "Source-folder cleanup skipped: %s is not under watch root %s.",
                    folder_r, watch_root_r,
                )
                return
        except OSError:
            logger.warning(
                "Source-folder cleanup skipped: could not verify %s is under %s.",
                folder_r, watch_root_r,
            )
            return

        if folder_r.parent == folder_r:
            logger.warning(
                "Source-folder cleanup skipped: %s has no parent.", folder_r,
            )
            return

        if not folder.exists():
            logger.info(
                "Source folder %s is already gone; walking up for empty parents.",
                folder_r,
            )
        else:
            # rmtree can hit ENOTEMPTY on Unraid's shfs/FUSE even after its own
            # child deletions "succeed" -- deletions propagate with a lag, and a
            # torrent still seeding can briefly hold a file. This is transient,
            # so retry: unlink files individually (unlink succeeds even while a
            # file is held open on Linux), wait, and try the tree again.
            removed = False
            last_exc: Optional[OSError] = None
            for attempt in range(5):
                try:
                    shutil.rmtree(folder, ignore_errors=False)
                    removed = True
                    break
                except FileNotFoundError:
                    removed = True
                    break
                except OSError as exc:
                    last_exc = exc
                    try:
                        for child in sorted(folder.rglob("*"), reverse=True):
                            try:
                                if child.is_dir() and not child.is_symlink():
                                    child.rmdir()
                                else:
                                    child.unlink()
                            except OSError:
                                pass
                    except OSError:
                        pass
                    time.sleep(1.5)
            if removed:
                logger.info("Removed source folder: %s", folder_r)
            else:
                shutil.rmtree(folder, ignore_errors=True)
                if folder.exists():
                    logger.warning(
                        "Source folder %s still present after retries (%s) -- "
                        "leaving it; it will be retried on the next pass.",
                        folder_r, last_exc,
                    )
                    return
                logger.info("Removed source folder (lenient): %s", folder_r)

        # Walk up and remove any empty parent folders (e.g. artist dir
        # left behind after its only album was cleaned). Stop before
        # touching the watch root.
        self._rmdir_empty_parents(folder_r, watch_root_r)

    def _rmdir_empty_parents(self, start_folder: Path, watch_root_r: Path) -> None:
        """
        Walk from `start_folder`'s parent upward, rmdir'ing any empty
        directory we find, until we encounter one that still has
        contents or we reach the watch root. The watch root is never
        removed or even inspected for emptiness.
        """
        parent = start_folder.parent
        while True:
            # Stop if we're at or above the watch root.
            if parent == watch_root_r:
                break
            try:
                # True if `parent` is strictly below watch_root.
                if watch_root_r not in parent.parents:
                    break
            except OSError:
                break
            try:
                if not parent.exists():
                    parent = parent.parent
                    continue
                # Any contents at all -> stop walking up.
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                logger.info("Removed now-empty parent folder: %s", parent)
            except OSError as exc:
                logger.debug("rmdir of %s stopped: %s", parent, exc)
                break
            parent = parent.parent

    def _cleanup_source(self, cue_path: Path, audio_path: Path) -> None:
        """
        Move the original disc image + .cue out of the watch folder so we
        don't re-process them. We park them under `<staging_root>/_processed/`.
        """
        parked = self.cfg.staging_root / "_processed"
        parked.mkdir(parents=True, exist_ok=True)
        for src in (cue_path, audio_path):
            if not src.exists():
                continue
            dst = parked / src.name
            try:
                shutil.move(str(src), str(dst))
                logger.info("Parked original %s -> %s", src.name, dst)
            except OSError as exc:
                logger.warning("Could not park %s: %s", src, exc)
