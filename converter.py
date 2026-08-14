"""
Converter backend for the WebUI "Converter" tab.

Two parts:

* LibraryTree -- a file-backed reflection of the music library's folder
  structure (audio files only, rolled-up folder sizes), so the browser tab
  loads instantly from cache instead of walking the share per page load.
  A low-priority background rescan keeps it fresh; per-file audio details
  (format / bitrate / channels / sample rate) are read lazily with mutagen
  the first time a folder is listed and cached by (size, mtime).

* ConvertManager -- converts selected files to AAC / MP3 / Opus with
  dbpoweramp-style per-codec settings (mode CBR/VBR, bitrate, VBR quality,
  sample rate, channel downmix), up to 10 files concurrently, reporting
  per-file AND total progress (ffmpeg -progress pipe). Missing ID tags are
  regenerated from the filename/folder (optionally polished by the LLM)
  so lossy output is never tag-less.

Everything is path-guarded to the library root -- the WebUI can only see
and touch audio inside it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("converter")

# The destination root comes straight from the user, so a stray "../" or ":"
# must not escape the drive or break the path. `\w` keeps letters of any
# script (the library has Cyrillic and CJK artists) while dropping
# separators. NB: a literal dash must be LAST in the class or it forms a
# RANGE -- an earlier draft of this line did exactly that and sanitised
# nothing at all.
_SAFE_DIR_RE = re.compile(r"[^\w ._-]+", re.UNICODE)

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape",
    ".wv", ".aiff", ".aif", ".alac", ".dsf", ".dff", ".wma", ".mpc",
}

LOSSLESS_EXTS = {".flac", ".wav", ".ape", ".wv", ".aiff", ".aif", ".alac",
                 ".dsf", ".dff"}

# dbpoweramp-style option schema per codec, served to the UI so the dropdowns
# always match what the encoder actually supports.
CODEC_OPTIONS: Dict[str, Dict[str, Any]] = {
    "mp3": {
        "label": "MP3 (LAME)",
        "ext": ".mp3",
        "modes": ["CBR", "VBR"],
        "bitrates": [320, 256, 224, 192, 160, 128, 112, 96, 80, 64],
        "quality": [
            "V0 (~245 kbps)", "V1 (~225 kbps)", "V2 (~190 kbps)",
            "V3 (~175 kbps)", "V4 (~165 kbps)", "V5 (~130 kbps)",
            "V6 (~115 kbps)", "V7 (~100 kbps)", "V8 (~85 kbps)",
            "V9 (~65 kbps)",
        ],
        "sample_rates": ["keep", 48000, 44100, 32000, 24000, 22050],
        "channels": ["keep", "stereo", "mono"],
        "default_mode": "VBR",
        "default_quality": "V0 (~245 kbps)",
        "help": "CBR: fixed bitrate. VBR: LAME -V quality presets "
                "(V0 best .. V9 smallest).",
    },
    "aac": {
        "label": "AAC (M4A)",
        "ext": ".m4a",
        "modes": ["CBR", "VBR"],
        "bitrates": [320, 256, 224, 192, 160, 128, 96, 80, 64, 48, 32],
        "quality": [
            "Q1 (lowest)", "Q2 (low)", "Q3 (medium)", "Q4 (high)",
            "Q5 (highest)",
        ],
        "sample_rates": ["keep", 48000, 44100, 32000, 24000],
        "channels": ["keep", "stereo", "mono", "5.1"],
        "default_mode": "VBR",
        "default_quality": "Q5 (highest)",
        "help": "CBR: fixed bitrate. VBR: ffmpeg AAC quality scale "
                "(Q1 smallest .. Q5 best). AAC keeps multichannel if "
                "you pick 'keep'.",
    },
    "opus": {
        "label": "Opus",
        "ext": ".opus",
        "modes": ["VBR", "CVBR", "CBR"],
        "bitrates": [256, 224, 192, 160, 128, 96, 80, 64, 48, 32, 24],
        "quality": [
            "Effort 10 (best)", "Effort 9", "Effort 8", "Effort 7",
            "Effort 6", "Effort 5", "Effort 4", "Effort 3", "Effort 2",
            "Effort 1", "Effort 0 (fastest)",
        ],
        "sample_rates": ["48000 (Opus native)"],
        "channels": ["keep", "stereo", "mono"],
        "help": "Opus is always 48 kHz internally. VBR: quality-first; "
                "CVBR: constrained; CBR: hard bitrate. Effort = encoder "
                "compression effort (quality vs speed, all lossy-equal "
                "bitrate).",
    },
    # --- LOSSLESS -------------------------------------------------------
    # Bit depth only means anything here. A lossy codec stores no samples, so
    # "24-bit MP3" is not a thing -- the depth selector is hidden for those.
    "flac": {
        "label": "FLAC (lossless)",
        "ext": ".flac",
        "modes": ["Lossless"],
        "bitrates": [],
        "quality": [
            "8 (smallest, slowest)", "7", "6", "5 (default)", "4", "3",
            "2", "1", "0 (fastest, largest)",
        ],
        "sample_rates": ["keep", 192000, 96000, 88200, 48000, 44100],
        "channels": ["keep", "stereo", "mono"],
        "bit_depths": ["original", 16, 24],
        "default_mode": "Lossless",
        "default_quality": "5 (default)",
        "default_bit_depth": "original",
        "help": "Lossless. Quality is the FLAC COMPRESSION level -- it changes "
                "file size and encode time only, never the audio. Bit depth "
                "'original' keeps the source's depth; 16 downsamples a hi-res "
                "master (irreversible, but that is the point when shrinking a "
                "library). NO 32-bit option: ffmpeg's FLAC encoder caps at 24 "
                "and silently writes 24 if asked for 32, so offering it would "
                "be a promise it cannot keep. Use WAV for true 32-bit.",
    },
    "wav": {
        "label": "WAV (uncompressed)",
        "ext": ".wav",
        "modes": ["Lossless"],
        "bitrates": [],
        "quality": [],
        "sample_rates": ["keep", 192000, 96000, 88200, 48000, 44100],
        "channels": ["keep", "stereo", "mono"],
        "bit_depths": ["original", 16, 24, 32],
        "default_mode": "Lossless",
        "default_bit_depth": "original",
        "help": "Uncompressed PCM. Large; useful as an interchange format. "
                "Carries almost no tags -- prefer FLAC unless something "
                "downstream demands WAV.",
    },
}

# Bit depth -> ffmpeg. Keyed by STRING because it arrives from JSON as either.
# FLAC has no s24 sample format: it carries 24-bit inside s32 and needs
# -bits_per_raw_sample to say so. WAV instead encodes the depth in the codec
# name, so the two need different handling.
# No "32": the encoder caps at 24 (verified -- it writes
# bits_per_raw_sample=24 even when asked for 32), so the option is not offered.
_FLAC_FMT = {"16": "s16", "24": "s32"}
_WAV_PCM = {"16": "pcm_s16le", "24": "pcm_s24le", "32": "pcm_s32le"}


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


class LibraryTree:
    """File-backed structure cache of the library (audio only)."""

    def __init__(self, root: Path, cache_file: Path):
        self.root = Path(root)
        self.cache_file = Path(cache_file)
        self._lock = threading.Lock()
        # dirs: rel -> {"size": int, "files": int} (rolled up, audio only)
        self._dirs: Dict[str, Dict[str, Any]] = {}
        # details: rel file -> {"sig": "size:mtime", ...audio detail...}
        self._details: Dict[str, Dict[str, Any]] = {}
        self._scanned_ts: float = 0.0
        self._scanning = False
        self._load()

    # ---------- cache file ----------
    def _load(self) -> None:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            self._dirs = data.get("dirs") or {}
            self._details = data.get("details") or {}
            self._scanned_ts = float(data.get("scanned_ts") or 0)
        except Exception:  # noqa: BLE001 (missing/corrupt -> rescan)
            self._dirs, self._details, self._scanned_ts = {}, {}, 0.0

    def _save(self) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        try:
            with self._lock:
                payload = json.dumps({
                    "scanned_ts": self._scanned_ts,
                    "dirs": self._dirs,
                    "details": self._details,
                })
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError as exc:
            logger.debug("library cache save failed: %s", exc)

    # ---------- scanning ----------
    def scan(self) -> int:
        """Full structure walk (stat only, no tag reads). Returns dir count.
        Cheap enough to run periodically; heavy tag reads stay lazy."""
        if self._scanning:
            return len(self._dirs)
        self._scanning = True
        try:
            dirs: Dict[str, Dict[str, Any]] = {}
            root = self.root
            for dirpath, dirnames, filenames in os.walk(root):
                # Lowest priority: yield the GIL/IO between directories.
                time.sleep(0)
                rel = os.path.relpath(dirpath, root)
                rel = "" if rel == "." else rel.replace("\\", "/")
                size = 0
                nfiles = 0
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                        continue
                    try:
                        size += os.stat(os.path.join(dirpath, fn)).st_size
                        nfiles += 1
                    except OSError:
                        continue
                dirs[rel] = {"size": size, "files": nfiles}
            # Roll child sizes up into every ancestor.
            for rel in sorted(dirs, key=lambda r: r.count("/"), reverse=True):
                if not rel:
                    continue
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                if parent in dirs:
                    dirs[parent]["size"] += dirs[rel]["size"]
                    dirs[parent]["files"] += dirs[rel]["files"]
            # Prune empty (no audio anywhere) dirs from the view.
            dirs = {r: v for r, v in dirs.items() if v["files"] > 0 or r == ""}
            with self._lock:
                self._dirs = dirs
                self._scanned_ts = time.time()
                # Drop details of files that vanished.
                self._details = {
                    rel: d for rel, d in self._details.items()
                    if (self.root / rel).exists()
                }
            self._save()
            logger.info("library tree: scanned %d folder(s), %s audio files",
                        len(dirs), dirs.get("", {}).get("files", 0))
            return len(dirs)
        finally:
            self._scanning = False

    def maybe_scan(self, max_age_seconds: int) -> None:
        if time.time() - self._scanned_ts >= max_age_seconds:
            self.scan()

    # ---------- listing ----------
    def _safe_rel(self, rel: str) -> Optional[Path]:
        rel = (rel or "").strip().strip("/")
        p = (self.root / rel).resolve() if rel else self.root.resolve()
        try:
            root_r = self.root.resolve()
            if p != root_r and root_r not in p.parents:
                return None
        except OSError:
            return None
        return p

    def _file_detail(self, abs_path: Path, rel: str) -> Dict[str, Any]:
        """Audio detail via mutagen, cached by (size, mtime)."""
        try:
            st = abs_path.stat()
        except OSError:
            return {}
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        with self._lock:
            cached = self._details.get(rel)
        if cached and cached.get("sig") == sig:
            return cached
        det: Dict[str, Any] = {"sig": sig, "size": st.st_size}
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(str(abs_path))
            info = getattr(mf, "info", None)
            if info is not None:
                det["bitrate"] = int(getattr(info, "bitrate", 0) or 0)
                det["channels"] = int(getattr(info, "channels", 0) or 0)
                det["sample_rate"] = int(
                    getattr(info, "sample_rate", 0) or 0)
                det["length"] = round(float(
                    getattr(info, "length", 0) or 0), 1)
                det["bits"] = int(
                    getattr(info, "bits_per_sample", 0) or 0)
        except Exception:  # noqa: BLE001
            pass
        ext = abs_path.suffix.lower()
        det["format"] = ext.lstrip(".").upper()
        det["lossless"] = ext in LOSSLESS_EXTS
        with self._lock:
            self._details[rel] = det
        return det

    def refresh_dir(self, rel: str) -> Dict[str, Any]:
        """
        Re-stat a SINGLE folder and update the cache in place, so a file that was
        just written (or deleted) shows immediately instead of waiting for the
        hourly background rescan. Rolled-up ancestor totals are adjusted by the
        delta rather than re-walking the library.

        Returns {"files": n, "size": bytes} for the folder after refreshing.
        """
        rel = (rel or "").strip("/")
        p = self.root / rel if rel else self.root
        size = 0
        nfiles = 0
        try:
            for entry in os.scandir(p):
                if not entry.is_file():
                    continue
                if os.path.splitext(entry.name)[1].lower() not in AUDIO_EXTS:
                    continue
                try:
                    size += entry.stat().st_size
                    nfiles += 1
                except OSError:
                    continue
        except OSError:
            # Folder is gone -- drop it and let the caller re-render.
            with self._lock:
                self._dirs.pop(rel, None)
            self._save()
            return {"files": 0, "size": 0, "gone": True}

        with self._lock:
            prev = self._dirs.get(rel) or {"size": 0, "files": 0}
            # `prev` for a parent folder includes its children's rolled-up
            # totals, so keep that part and swap only this folder's OWN files.
            own_prev = prev.get("own_size", prev.get("size", 0))
            own_prev_n = prev.get("own_files", prev.get("files", 0))
            d_size = size - own_prev
            d_files = nfiles - own_prev_n
            self._dirs[rel] = {
                "size": prev.get("size", 0) + d_size,
                "files": prev.get("files", 0) + d_files,
                "own_size": size, "own_files": nfiles,
            }
            # Push the delta up the tree so folder sizes stay believable.
            parent = rel
            while parent:
                parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
                if parent in self._dirs:
                    self._dirs[parent]["size"] = max(
                        0, self._dirs[parent].get("size", 0) + d_size)
                    self._dirs[parent]["files"] = max(
                        0, self._dirs[parent].get("files", 0) + d_files)
                if not parent:
                    break
            # Forget per-file details for anything no longer on disk.
            pref = (rel + "/") if rel else ""
            live = set()
            try:
                for entry in os.scandir(p):
                    if entry.is_file():
                        live.add(pref + entry.name)
            except OSError:
                pass
            for k in [k for k in self._details
                      if k.startswith(pref) and "/" not in k[len(pref):]
                      and k not in live]:
                self._details.pop(k, None)
        self._save()
        return {"files": nfiles, "size": size}

    def list_dir(self, rel: str) -> Optional[Dict[str, Any]]:
        """One level: subfolders (with rolled-up sizes from the cache) +
        audio files (with lazily-cached detail). None if path escapes root."""
        p = self._safe_rel(rel)
        if p is None:
            return None
        rel = (rel or "").strip().strip("/")
        folders = []
        files = []
        try:
            entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
        except OSError:
            entries = []
        dirty = False
        for e in entries:
            crel = f"{rel}/{e.name}" if rel else e.name
            crel = crel.replace("\\", "/")
            try:
                if e.is_dir():
                    info = self._dirs.get(crel)
                    if info is None:
                        continue  # no audio anywhere under it
                    folders.append({
                        "name": e.name, "rel": crel,
                        "size": info["size"], "files": info["files"],
                        "size_h": _fmt_size(info["size"]),
                    })
                elif e.suffix.lower() in AUDIO_EXTS:
                    det = self._file_detail(e, crel)
                    dirty = True
                    br = det.get("bitrate") or 0
                    files.append({
                        "name": e.name, "rel": crel,
                        "size": det.get("size", 0),
                        "size_h": _fmt_size(det.get("size", 0)),
                        "format": det.get("format", ""),
                        "bitrate": (f"{br // 1000} kbps" if br else ""),
                        "channels": det.get("channels", 0),
                        "sample_rate": det.get("sample_rate", 0),
                        "bits": det.get("bits", 0),
                        "lossless": det.get("lossless", False),
                    })
            except OSError:
                continue
        if dirty:
            self._save()
        return {"rel": rel, "folders": folders, "files": files,
                "scanned_ts": self._scanned_ts}

    def file_info(self, rel: str) -> Optional[Dict[str, Any]]:
        """Full info popup: every ID tag + codec specs."""
        p = self._safe_rel(rel)
        if p is None or not p.is_file():
            return None
        out: Dict[str, Any] = {"path": rel, "name": p.name}
        out["detail"] = self._file_detail(p, rel.strip("/"))
        tags: Dict[str, str] = {}
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(str(p))
            if mf is not None and mf.tags:
                for k, v in mf.tags.items():
                    try:
                        if isinstance(v, list):
                            v = "; ".join(str(x) for x in v)
                        s = str(v)
                        if len(s) > 300:
                            s = s[:300] + "…"
                        tags[str(k)] = s
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        out["tags"] = dict(sorted(tags.items())[:60])
        return out

    def delete(self, rels: List[str]) -> Tuple[int, List[str]]:
        """Delete the given files/folders (must be under root). Returns
        (deleted_count, errors). Folders are removed recursively but only
        audio/sidecar content is expected -- we remove whatever's inside."""
        import shutil
        deleted = 0
        errors: List[str] = []
        for rel in rels:
            p = self._safe_rel(rel)
            if p is None or p == self.root.resolve():
                errors.append(f"refused: {rel}")
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                deleted += 1
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        if deleted:
            self.scan()
        return deleted, errors


class ConvertManager:
    """Converts library files to MP3/AAC/Opus with live progress."""

    # How many active/queued jobs status() ships per poll. See status().
    STATUS_WINDOW = 40

    def __init__(self, root: Path, ffmpeg: str = "ffmpeg", llm=None,
                 lidarr=None, lidarr_root: str = "", tree=None,
                 busy_provider=None, nice_level: int = 15):
        self.root = Path(root)
        self.ffmpeg = ffmpeg
        self.llm = llm
        # Optional Lidarr client + the path prefix Lidarr sees for `root`, used
        # by OVERWRITE mode to tell Lidarr a library file was replaced.
        self.lidarr = lidarr
        self.lidarr_root = str(lidarr_root or "")
        # LibraryTree, so a finished conversion updates just its folder in the
        # cache and the new (or removed) file shows in the UI immediately.
        self.tree = tree
        # Manual conversions YIELD to the pipeline. `busy_provider()` is true
        # while a cue split / SACD / DVD-Audio / DTS / DSD extraction is
        # running: those are the jobs the pipeline needs to finish to get music
        # into the library, and they were competing with up to 10 converter
        # ffmpegs for the same cores with nothing arbitrating.
        self.busy_provider = busy_provider
        # ...and even when nothing is running, the converter's own ffmpeg is
        # niced so the pipeline wins any contention it does hit.
        self.nice_level = max(0, min(19, int(nice_level or 0)))
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}   # id -> job dict
        self._queue: List[str] = []
        self._active = 0
        self._repump = None
        self._paused = False
        self._workers = 2

    # ---------- public ----------
    # Where the container sees Unraid's unassigned devices. /mnt/disks is bind
    # mounted here so EVERY unassigned drive shows up without a container
    # recreate -- mounting one specific drive would need a new -v (and a
    # recreate) every time a disk is swapped.
    UNASSIGNED_ROOT = Path("/unassigned")

    def destinations(self) -> List[Dict[str, Any]]:
        """
        Output targets: the library itself (in place) plus every mounted
        unassigned device, with free space so a 3 TB job isn't aimed at a
        200 GB disk.

        Returns [] for the drives when /unassigned isn't mounted -- the WebUI
        then shows only in-place, which is the pre-existing behaviour.
        """
        out: List[Dict[str, Any]] = [{
            "id": "", "label": "Source folder (write beside the original)",
            "path": "", "kind": "inplace",
        }]
        root = self.UNASSIGNED_ROOT
        try:
            if not root.is_dir():
                return out
            for d in sorted(root.iterdir(), key=lambda e: e.name.lower()):
                if not d.is_dir():
                    continue
                free = total = 0
                try:
                    st = os.statvfs(str(d))
                    free = st.f_bavail * st.f_frsize
                    total = st.f_blocks * st.f_frsize
                except OSError:
                    pass
                out.append({
                    "id": d.name,
                    "label": "%s  (%s free of %s)" % (
                        d.name, _fmt_size(free), _fmt_size(total)),
                    "path": str(d), "kind": "unassigned",
                    "free": free, "total": total,
                })
        except OSError as exc:
            logger.debug("destinations: %s unreadable: %s", root, exc)
        return out

    def _resolve_out_dir(self, src: Path, opts: Dict[str, Any]) -> Path:
        """
        The folder a converted file goes in.

        `out_dest` empty  -> beside the source (unchanged behaviour)
        `out_dest` set    -> that unassigned drive, and when `preserve_path` is
                             on, the source's path UNDER THE LIBRARY ROOT is
                             recreated there, so
                             /music/Music/Artist/Album/01.flac becomes
                             /unassigned/<drive>/Artist/Album/01.flac
                             rather than dumping every track in one folder.
        Falls back to the source folder if the target cannot be created, so a
        bad destination never silently drops the job.
        """
        dest = str(opts.get("out_dest") or "").strip()
        if not dest:
            return src.parent
        base = self.UNASSIGNED_ROOT / dest
        try:
            base = base.resolve()
            if self.UNASSIGNED_ROOT.resolve() not in base.parents:
                logger.warning("convert: destination %r escapes %s -- writing "
                               "beside the source instead", dest,
                               self.UNASSIGNED_ROOT)
                return src.parent
        except OSError:
            return src.parent
        # Optional root folder INSIDE the drive, so the user gets one place
        # everything lands in instead of artists dumped at the drive root.
        root_name = str(opts.get("out_root") or "").strip().strip("/\\")
        if root_name:
            safe = _SAFE_DIR_RE.sub("_", root_name).strip(" .")
            if safe:
                base = base / safe
        out = base
        if opts.get("preserve_path", True):
            try:
                out = base / src.parent.relative_to(self.root.resolve())
            except (ValueError, OSError):
                out = base / src.parent.name
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("convert: cannot create %s (%s) -- writing beside "
                           "the source instead", out, exc)
            return src.parent
        return out

    def options(self) -> Dict[str, Any]:
        return {"codecs": CODEC_OPTIONS,
                "concurrency": list(range(1, 11)),
                # Output targets (in-place + each unassigned device).
                "destinations": self.destinations(),
                "preserve_path": {
                    "label": "Preserve source path",
                    "help": ("Recreate the source's folder structure under the "
                             "chosen drive, so Artist/Album stays Artist/Album "
                             "instead of every track landing in one folder. "
                             "Ignored when writing beside the original."),
                    "default": True,
                },
                "skip_existing": {
                    "label": "Skip files already converted",
                    "help": ("If the output file is already there, leave it "
                             "alone instead of encoding a second copy. Without "
                             "this the converter appends (1), (2), (3)... and "
                             "re-encodes the same track on every run."),
                    "default": True,
                },
                "out_root": {
                    "label": "Destination root folder",
                    "help": ("Optional folder created under the chosen drive "
                             "that everything is written into, e.g. "
                             "'Converted' -> <drive>/Converted/Artist/Album/. "
                             "Ignored when writing beside the original."),
                    "default": "",
                },
                "copy_lossy": {
                    "label": "Copy lossy files instead of skipping",
                    "help": ("With 'lossless sources only' on, carry the "
                             "already-lossy files across to the destination "
                             "untouched instead of leaving them out, so the "
                             "destination holds the whole selection. "
                             "Re-encoding them would lose quality twice; "
                             "skipping them leaves gaps. Needs a destination "
                             "drive -- copying beside the original would just "
                             "duplicate the file in place."),
                    "default": False,
                },
                "lossless_only": {
                    "label": "Skip files that are already lossy",
                    "help": ("Only convert LOSSLESS sources (FLAC/WAV/APE/WV/"
                             "AIFF/ALAC). Re-encoding an MP3 to another lossy "
                             "format loses quality twice, and re-encoding one "
                             "to FLAC just makes a bigger file with no gain."),
                    "default": False,
                },
                # What the Converter tab opens on. Per-codec mode/quality
                # defaults live in CODEC_OPTIONS above.
                "defaults": {"codec": "aac", "bitrate": 320,
                             "sample_rate": "keep", "channels": "keep",
                             "concurrency": 2},
                # Library root, so the WebUI can turn a tree-relative path into
                # the absolute path the audio player streams from.
                "root": str(self.root),
                # Replace-in-place: delete the source (e.g. the FLAC) once the
                # lossy encode is verified, repoint sidecar .xml files at the new
                # filename, and have Lidarr rescan so it reflects the change.
                "overwrite": {
                    "label": "Delete original",
                    "help": ("Replace the source file with the converted one: "
                             "the original (e.g. FLAC) is DELETED after a "
                             "verified encode, sidecar .xml files are repointed "
                             "at the new filename, and Lidarr is asked to "
                             "rescan the album folder."),
                    "default": False,
                }}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            jobs = [dict(j) for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("added", 0))
        running = [j for j in jobs if j["state"] == "running"]
        queued = [j for j in jobs if j["state"] == "queued"]
        done = [j for j in jobs if j["state"] not in ("running", "queued")]
        n_active = len(running) + len(queued)
        total_pct = 0.0
        if n_active:
            total_pct = (sum(j.get("pct", 0.0) for j in running)
                         / float(n_active))
        # SEND A WINDOW, NOT THE WHOLE QUEUE. This payload is polled every 3
        # seconds; selecting the library queues ~86,500 jobs, and shipping all
        # of them as JSON on every poll is what makes the browser fall over.
        # Everything running is always included -- that is at most the
        # concurrency setting -- plus enough of the queue to see what is next.
        shown = running + queued[:max(0, self.STATUS_WINDOW - len(running))]
        return {"active": shown, "done": done[-25:],
                "total_pct": round(total_pct, 1),
                "n_active": n_active,
                "n_running": len(running),
                "n_queued": len(queued),
                "n_shown": len(shown),
                "paused": bool(self._paused),
                "held_for_pipeline": self._pipeline_busy()}

    def clear_done(self) -> None:
        with self._lock:
            self._jobs = {k: j for k, j in self._jobs.items()
                          if j["state"] in ("running", "queued")}

    def start(self, rels: List[str], codec: str, opts: Dict[str, Any],
              concurrency: int = 2) -> Tuple[int, List[str]]:
        """Queue conversions. Returns (queued, errors)."""
        spec = CODEC_OPTIONS.get(codec)
        if spec is None:
            return 0, [f"unknown codec {codec}"]
        self._workers = max(1, min(10, int(concurrency or 2)))
        queued = 0
        errors: List[str] = []
        root_r = self.root.resolve()
        opts = dict(opts or {})
        lossless_only = bool(opts.get("lossless_only"))
        # Copying beside the original would just duplicate the file in place,
        # so it only means anything when writing to a destination.
        copy_lossy = bool(opts.get("copy_lossy")) and bool(
            str(opts.get("out_dest") or "").strip())
        skipped_lossy = 0
        copied_lossy = 0
        for rel in rels:
            p = (self.root / rel.strip("/")).resolve()
            try:
                if p != root_r and root_r not in p.parents:
                    errors.append(f"refused: {rel}")
                    continue
                if not p.is_file():
                    errors.append(f"not a file: {rel}")
                    continue
                if lossless_only and p.suffix.lower() not in LOSSLESS_EXTS:
                    # Lossy source with lossless-only on. Either drop it, or --
                    # when copy_lossy is set -- COPY it across untouched, so a
                    # destination drive ends up holding the WHOLE selection:
                    # the lossless re-encoded, the lossy carried over as-is.
                    # Re-encoding those would lose quality twice; leaving them
                    # out leaves gaps in the copy.
                    if not copy_lossy:
                        # Deliberately silent per file -- selecting a whole
                        # album with this on would otherwise produce an error
                        # per MP3. Counted and reported once below.
                        skipped_lossy += 1
                        continue
                    is_copy = True
                else:
                    is_copy = False
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
                continue
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "rel": rel, "name": p.name,
                # ONE shared dict for the whole batch, not a copy per job:
                # a full-library run is ~86k jobs and the options are identical
                # for all of them. Nothing mutates job["opts"] (the run path
                # only reads it), so sharing is safe and saves ~85 MB.
                "codec": codec, "opts": opts,
                "state": "queued", "pct": 0.0, "msg": "",
                "copy": is_copy,
                "added": time.time(),
            }
            if is_copy:
                copied_lossy += 1
            with self._lock:
                self._jobs[jid] = job
                self._queue.append(jid)
            queued += 1
        if copied_lossy:
            logger.info("convert: %d lossy file(s) will be COPIED, not "
                        "re-encoded (lossless-only is on)", copied_lossy)
        if skipped_lossy:
            msg = ("skipped %d already-lossy file(s) (lossless-only is on)"
                   % skipped_lossy)
            logger.info("convert: %s", msg)
            errors.append(msg)
        self._pump()
        return queued, errors

    # ---------- internals ----------
    def pause(self) -> bool:
        """Stop starting NEW jobs. Running encodes finish; nothing is lost."""
        with self._lock:
            self._paused = True
            n = len(self._queue)
        logger.info("convert: PAUSED (%d job(s) held, %d still running)",
                    n, self._active)
        return True

    def resume(self) -> bool:
        with self._lock:
            self._paused = False
        logger.info("convert: resumed")
        self._pump()
        return True

    def is_paused(self) -> bool:
        return bool(self._paused)

    def _pipeline_busy(self) -> bool:
        """Is the pipeline itself encoding right now?"""
        if self.busy_provider is None:
            return False
        try:
            return bool(self.busy_provider())
        except Exception:  # noqa: BLE001
            return False

    def _pump(self) -> None:
        if self._paused:
            return
        # Hold the queue while the pipeline is encoding. Jobs already running
        # are left alone -- killing them mid-encode would waste the work -- so
        # this drains the converter down rather than stopping it dead.
        if self._pipeline_busy():
            with self._lock:
                waiting = len(self._queue)
            if waiting:
                logger.debug("convert: pipeline is encoding -- holding %d "
                             "queued job(s)", waiting)
            self._schedule_repump()
            return
        with self._lock:
            while self._active < self._workers and self._queue:
                jid = self._queue.pop(0)
                job = self._jobs.get(jid)
                if not job or job["state"] != "queued":
                    continue
                self._active += 1
                t = threading.Thread(
                    target=self._run_job, args=(jid,), daemon=True,
                    name=f"convert-{jid}")
                t.start()

    def _schedule_repump(self, delay: float = 15.0) -> None:
        """Look again shortly; the pipeline's extractions are minutes long."""
        with self._lock:
            if getattr(self, "_repump", None) is not None:
                return
            t = threading.Timer(delay, self._repump_fire)
            t.daemon = True
            self._repump = t
        t.start()

    def _repump_fire(self) -> None:
        with self._lock:
            self._repump = None
        self._pump()

    def _nice_prefix(self) -> List[str]:
        """`nice`(+`ionice`) prefix for the converter's own ffmpeg."""
        if not self.nice_level:
            return []
        pre: List[str] = []
        if os.path.exists("/usr/bin/ionice") or os.path.exists("/bin/ionice"):
            pre += ["ionice", "-c", "3"]        # idle I/O class
        pre += ["nice", "-n", str(self.nice_level)]
        return pre

    def _duration(self, path: Path) -> float:
        ffprobe = str(Path(self.ffmpeg).with_name("ffprobe"))
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", str(path)],
                capture_output=True, text=True, timeout=30)
            return float((r.stdout or "0").strip() or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _missing_tags(self, path: Path) -> Dict[str, str]:
        """Regenerate absent core tags from the filename / folder layout
        (…/Artist/Album (Year)/NN - Title.ext), optionally polished by the
        LLM. Only returns keys that are MISSING in the file."""
        have: Dict[str, str] = {}
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(str(path))
            if mf is not None and mf.tags:
                def _first(keys):
                    for k in keys:
                        v = mf.tags.get(k)
                        if v:
                            return str(v[0] if isinstance(v, list) else v)
                    return ""
                have["title"] = _first(("title", "TITLE", "\xa9nam", "TIT2"))
                have["artist"] = _first(("artist", "ARTIST", "\xa9ART", "TPE1"))
                have["album"] = _first(("album", "ALBUM", "\xa9alb", "TALB"))
                have["track"] = _first((
                    "tracknumber", "TRACKNUMBER", "trkn", "TRCK"))
        except Exception:  # noqa: BLE001
            pass
        out: Dict[str, str] = {}
        stem = path.stem
        m = re.match(r"\s*(\d{1,3})\s*[-._ ]+\s*(.+)$", stem)
        f_track = (m.group(1).lstrip("0") or m.group(1)) if m else ""
        f_title = (m.group(2) if m else stem).strip(" -_.")
        album_dir = path.parent.name
        artist_dir = path.parent.parent.name
        album_guess = re.sub(r"\s*[\(\[]\d{4}[\)\]]\s*$", "", album_dir).strip()
        artist_guess = artist_dir
        if not have.get("title"):
            out["title"] = f_title
        if not have.get("track") and f_track:
            out["track"] = f_track
        if not have.get("album") and album_guess:
            out["album"] = album_guess
        if not have.get("artist") and artist_guess:
            # LLM polish: split a combined "Artist - Album" folder if needed.
            if self.llm is not None and getattr(self.llm, "enabled", False) \
                    and " - " not in artist_dir and len(artist_guess) > 40:
                try:
                    a, _b = self.llm.parse_artist_album(artist_dir)
                    if a:
                        artist_guess = a
                except Exception:  # noqa: BLE001
                    pass
            out["artist"] = artist_guess
        return out

    def _source_bit_depth(self, path: Path) -> int:
        """
        The source's REAL stored sample depth, or 0 when it has none.

        A lossy file (AAC/MP3/Opus/Vorbis) stores no sample depth at all: it
        reports sample_fmt=fltp and no bits_per_raw_sample, because it decodes to
        32-bit FLOAT. Treating that as "the original depth" is how converting an
        M4A to FLAC produced a 24-bit s32 file -- bigger than the source, with
        not one bit of extra information in it.

        Returns 0 for those so the caller can pick a sane depth instead of
        inheriting the decoder's working format.
        """
        try:
            r = subprocess.run(
                [self.ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error",
                 "-select_streams", "a:0", "-show_entries",
                 "stream=bits_per_raw_sample,sample_fmt", "-of", "json",
                 str(path)],
                capture_output=True, text=True, timeout=30)
            st = (json.loads(r.stdout).get("streams") or [{}])[0]
        except Exception:  # noqa: BLE001
            return 0
        try:
            n = int(st.get("bits_per_raw_sample") or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            return n
        fmt = str(st.get("sample_fmt") or "")
        # Integer formats carry a real depth; float ones do not.
        return {"u8": 8, "s16": 16, "s16p": 16,
                "s32": 32, "s32p": 32}.get(fmt, 0)

    def _build_args(self, codec: str, opts: Dict[str, Any],
                    src: Optional[Path] = None) -> List[str]:
        mode = str(opts.get("mode", "")).upper()
        br = int(opts.get("bitrate") or 0)
        q = str(opts.get("quality") or "")
        sr = str(opts.get("sample_rate") or "keep")
        ch = str(opts.get("channels") or "keep")
        args: List[str] = []
        if codec == "mp3":
            args += ["-c:a", "libmp3lame"]
            if mode == "VBR":
                mv = re.search(r"V?(\d)", q)
                args += ["-q:a", mv.group(1) if mv else "2"]
            else:
                args += ["-b:a", f"{br or 192}k"]
        elif codec == "aac":
            args += ["-c:a", "aac"]
            if mode == "VBR":
                mv = re.search(r"Q?(\d)", q)
                args += ["-q:a", mv.group(1) if mv else "4"]
            else:
                args += ["-b:a", f"{br or 192}k"]
        elif codec in ("flac", "wav"):
            depth = str(opts.get("bit_depth") or "original").strip().lower()
            if depth in ("", "original", "keep") and src is not None:
                # "original" means the SOURCE's depth -- resolve it explicitly
                # rather than letting the encoder inherit the decoder's format.
                # A lossy source has none (0), and 16 bits already holds
                # everything it can represent, so anything more is dead weight.
                sd = self._source_bit_depth(src)
                depth = "16" if sd <= 16 else ("24" if sd <= 24 else "32")
            if codec == "flac":
                args += ["-c:a", "flac"]
                mv = re.search(r"(\d+)", q)
                # FLAC "quality" is COMPRESSION LEVEL: size/time only, never
                # the audio. Keeping ffmpeg's default when unparsable.
                if mv:
                    args += ["-compression_level", mv.group(1)]
            if codec == "wav":
                # For WAV the PCM CODEC NAME is the bit depth -- adding
                # -sample_fmt on top just contradicts it. "original" names no
                # codec at all, so ffmpeg picks the pcm_* matching the source
                # instead of silently promoting everything to 24-bit.
                pcm = _WAV_PCM.get(depth)
                if pcm:
                    args += ["-c:a", pcm]
            elif depth not in ("", "original", "keep"):
                fmt = _FLAC_FMT.get(depth)
                if fmt:
                    args += ["-sample_fmt", fmt]
                    # FLAC stores 24-bit inside an s32 sample format, so the
                    # container needs telling explicitly or it writes 32.
                    if depth == "24":
                        args += ["-bits_per_raw_sample", "24"]
        elif codec == "opus":
            args += ["-c:a", "libopus", "-b:a", f"{br or 128}k"]
            if mode == "CBR":
                args += ["-vbr", "off"]
            elif mode == "CVBR":
                args += ["-vbr", "constrained"]
            else:
                args += ["-vbr", "on"]
            mv = re.search(r"(\d+)", q)
            if mv:
                args += ["-compression_level", mv.group(1)]
        if ch == "stereo":
            args += ["-ac", "2"]
        elif ch == "mono":
            args += ["-ac", "1"]
        elif ch in ("5.1", "6"):
            args += ["-ac", "6"]
        if codec != "opus" and sr not in ("", "keep") \
                and re.fullmatch(r"\d+", sr):
            args += ["-ar", sr]
        return args

    def _commit_replace(
        self, src: Path, tmp: Path, final: Path,
    ) -> Tuple[bool, str]:
        """
        OVERWRITE mode commit, run only after a verified encode:
          1. put the new lossy file at `final` (replacing it if it exists),
          2. DELETE the original file,
          3. repoint any sidecar .xml in the folder at the new filename,
          4. tell Lidarr the library file changed so it re-reads the folder.
        Returns (ok, note). On any failure before step 2 the original is left
        untouched and the temp file is removed, so nothing is ever lost.
        """
        notes: List[str] = []
        try:
            if final.exists() and final != src:
                final.unlink()
            tmp.replace(final)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False, f"could not place {final.name}: {exc}"
        # The original is gone only once the replacement is in place.
        removed = False
        try:
            if src.exists() and src.resolve() != final.resolve():
                src.unlink()
                removed = True
        except OSError as exc:
            notes.append(f"kept original ({exc})")
        try:
            os.chmod(final, 0o666)
        except OSError:
            pass
        notes.append(f"replaced {src.name} -> {final.name}"
                     if removed else f"wrote {final.name}")
        n_xml = self._repoint_sidecar_xml(src, final)
        if n_xml:
            notes.append(f"updated {n_xml} xml")
        if self._notify_lidarr(final):
            notes.append("Lidarr rescan queued")
        return True, "; ".join(notes)

    def _repoint_sidecar_xml(self, old: Path, new: Path) -> int:
        """
        Rewrite sidecar .xml files in the same folder that mention the old
        filename so they refer to the replaced file. Handles the raw name, the
        XML-escaped name, and a bare stem+extension reference. Returns the
        number of files changed.
        """
        changed = 0
        try:
            xmls = [p for p in old.parent.iterdir()
                    if p.is_file() and p.suffix.lower() == ".xml"]
        except OSError:
            return 0
        subs = [(old.name, new.name)]
        # XML-escaped variant (& -> &amp; etc.) in case the writer escaped it.
        esc_old = (old.name.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        esc_new = (new.name.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        if esc_old != old.name:
            subs.append((esc_old, esc_new))
        for x in xmls:
            try:
                text = x.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new_text = text
            for a, b in subs:
                if a in new_text:
                    new_text = new_text.replace(a, b)
            if new_text != text:
                try:
                    x.write_text(new_text, encoding="utf-8")
                    changed += 1
                    logger.info("converter: repointed %s -> %s in %s",
                                old.name, new.name, x.name)
                except OSError as exc:
                    logger.warning("converter: could not update %s: %s",
                                   x.name, exc)
        return changed

    def _notify_lidarr(self, changed_file: Path) -> bool:
        """
        Tell Lidarr a file in its library changed, by rescanning the containing
        album folder (RescanFolders re-reads what's on disk, which is what makes
        Lidarr drop the old trackfile and pick up the replacement). Translates
        the local path to the path Lidarr sees via `lidarr_root`.
        """
        if self.lidarr is None:
            return False
        folder = changed_file.parent
        try:
            rel = folder.resolve().relative_to(self.root.resolve())
        except Exception:  # noqa: BLE001
            rel = None
        if self.lidarr_root and rel is not None:
            target = str(PurePosixPath(self.lidarr_root) / rel.as_posix())
        else:
            target = str(folder)
        try:
            return self.lidarr.rescan_folder(target) is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("converter: Lidarr rescan of %s failed: %s",
                           target, exc)
            return False

    def _run_job(self, jid: str) -> None:
        job = self._jobs.get(jid)
        try:
            if not job:
                return
            src = (self.root / job["rel"].strip("/")).resolve()
            spec = CODEC_OPTIONS[job["codec"]]
            jopts = job.get("opts") or {}
            overwrite = bool(jopts.get("overwrite"))
            out_dir = self._resolve_out_dir(src, jopts)

            # COPY JOB: a lossy source under lossless-only, carried across
            # untouched so the destination holds the whole selection. Same
            # destination rules and the same skip-existing rule as an encode --
            # it just moves bytes instead of re-encoding them.
            if job.get("copy"):
                cdst = out_dir / src.name
                if cdst == src:
                    job["state"] = "skipped"
                    job["error"] = "source and destination are the same"
                    return
                if (jopts.get("skip_existing", True) and cdst.exists()
                        and cdst.stat().st_size == src.stat().st_size):
                    job["state"] = "skipped"
                    job["out"] = cdst.name
                    job["error"] = "already copied"
                    job["pct"] = 100.0
                    return
                job["state"] = "running"
                job["out"] = cdst.name
                tmp = cdst.with_name("." + cdst.name + ".copying")
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, tmp)
                    tmp.replace(cdst)
                except OSError as exc:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    job["state"] = "error"
                    job["error"] = "copy failed: %s" % exc
                    logger.warning("convert: copy %s failed: %s", src.name, exc)
                    return
                job["pct"] = 100.0
                job["state"] = "done"
                job["msg"] = "copied (lossy, not re-encoded)"
                logger.info("convert: copied %s -> %s", src.name, out_dir)
                # The tree refresh is done by the finally block for every job,
                # and it takes a REL path -- passing an absolute one here was
                # simply wrong as well as redundant.
                return
            dst = (out_dir / src.name).with_suffix(spec["ext"])
            if out_dir != src.parent:
                # Writing to another drive: "replace the original" makes no
                # sense there (the source is somewhere else entirely), so it is
                # ignored rather than deleting a file the user still has.
                overwrite = False
            if overwrite:
                # REPLACE MODE: the lossy file takes the source's place and the
                # original is deleted after a VERIFIED encode. Encode to a temp
                # name first so a failure can never destroy the original, and so
                # we never feed ffmpeg the same path as input and output.
                tmp = src.with_name(f".{src.stem}.converting{spec['ext']}")
                job["_final"] = str(dst)
                job["_replace"] = True
                dst = tmp
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                # Collision naming must stay IN out_dir. Using src.with_name()
                # here would quietly drop the file next to the original the
                # moment a same-named file already existed on the target drive.
                if dst == src:
                    dst = out_dir / (src.stem + " (converted)" + spec["ext"])
                # Already converted? Leave it. Without this the loop below
                # appends (1), (2), (3)... so every run re-encodes the same
                # track into yet another copy -- the whole library again on the
                # second pass, and again on the third.
                if (jopts.get("skip_existing", True) and dst.exists()
                        and dst.stat().st_size > 0):
                    job["state"] = "skipped"
                    job["out"] = dst.name
                    job["error"] = "already converted"
                    job["pct"] = 100.0
                    logger.info("convert: %s already exists -- skipping",
                                dst.name)
                    return
                n = 1
                while dst.exists():
                    dst = out_dir / f"{src.stem} ({n}){spec['ext']}"
                    n += 1
            job["state"] = "running"
            job["out"] = dst.name
            dur = self._duration(src)
            args = self._build_args(job["codec"], job["opts"], src)
            meta: List[str] = ["-map_metadata", "0"]
            for k, v in self._missing_tags(src).items():
                meta += ["-metadata", f"{k}={v}"]
            cmd = [*self._nice_prefix(),
                   self.ffmpeg, "-hide_banner", "-nostdin", "-y",
                   "-i", str(src), "-vn", *meta, *args,
                   "-progress", "pipe:1", "-nostats",
                   "-loglevel", "error", str(dst)]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if line.startswith("out_time_ms=") and dur > 0:
                    try:
                        pct = (int(line.split("=", 1)[1]) / 1e6) / dur * 100
                        job["pct"] = max(0.0, min(99.9, round(pct, 1)))
                    except ValueError:
                        pass
                elif line.startswith("progress=end"):
                    job["pct"] = 100.0
            rc = proc.wait(timeout=1800)
            err = (proc.stderr.read() or "")[-400:] if proc.stderr else ""
            ok = (rc == 0 and dst.exists() and dst.stat().st_size > 0)
            if ok and job.get("_replace"):
                # Commit the replacement only now that the encode is verified.
                ok, note = self._commit_replace(src, dst, Path(job["_final"]))
                job["msg"] = note
                if ok:
                    job["state"] = "done"
                    job["pct"] = 100.0
                    job["out"] = Path(job["_final"]).name
                else:
                    job["state"] = "error"
                return
            if ok:
                job["state"] = "done"
                job["pct"] = 100.0
                job["msg"] = f"-> {dst.name}"
            else:
                job["state"] = "error"
                job["msg"] = err or f"ffmpeg rc={rc}"
                try:
                    dst.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            if job:
                job["state"] = "error"
                job["msg"] = str(exc)[:300]
            logger.exception("convert job %s failed", jid)
        finally:
            # Whatever happened, this folder's contents may have changed (a new
            # lossy file, a replaced original). Update just that folder so the
            # tree is truthful without a full rescan.
            try:
                if self.tree is not None and job:
                    rel = str(job.get("rel") or "").strip("/")
                    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    self.tree.refresh_dir(parent)
                    job["refreshed"] = parent
            except Exception as exc:  # noqa: BLE001
                logger.debug("post-convert tree refresh failed: %s", exc)
            with self._lock:
                self._active -= 1
            self._pump()
