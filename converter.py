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
import subprocess
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("converter")

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
}


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

    def __init__(self, root: Path, ffmpeg: str = "ffmpeg", llm=None,
                 lidarr=None, lidarr_root: str = ""):
        self.root = Path(root)
        self.ffmpeg = ffmpeg
        self.llm = llm
        # Optional Lidarr client + the path prefix Lidarr sees for `root`, used
        # by OVERWRITE mode to tell Lidarr a library file was replaced.
        self.lidarr = lidarr
        self.lidarr_root = str(lidarr_root or "")
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}   # id -> job dict
        self._queue: List[str] = []
        self._active = 0
        self._workers = 2

    # ---------- public ----------
    def options(self) -> Dict[str, Any]:
        return {"codecs": CODEC_OPTIONS,
                "concurrency": list(range(1, 11)),
                # Replace-in-place: delete the source (e.g. the FLAC) once the
                # lossy encode is verified, repoint sidecar .xml files at the new
                # filename, and have Lidarr rescan so it reflects the change.
                "overwrite": {
                    "label": "Overwrite existing (delete original)",
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
        act = [j for j in jobs if j["state"] in ("running", "queued")]
        done = [j for j in jobs if j["state"] not in ("running", "queued")]
        total_pct = 0.0
        if act:
            total_pct = sum(j.get("pct", 0.0) for j in act) / len(act)
        return {"active": act, "done": done[-25:],
                "total_pct": round(total_pct, 1),
                "n_active": len(act)}

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
        for rel in rels:
            p = (self.root / rel.strip("/")).resolve()
            try:
                if p != root_r and root_r not in p.parents:
                    errors.append(f"refused: {rel}")
                    continue
                if not p.is_file():
                    errors.append(f"not a file: {rel}")
                    continue
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
                continue
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "rel": rel, "name": p.name,
                "codec": codec, "opts": dict(opts or {}),
                "state": "queued", "pct": 0.0, "msg": "",
                "added": time.time(),
            }
            with self._lock:
                self._jobs[jid] = job
                self._queue.append(jid)
            queued += 1
        self._pump()
        return queued, errors

    # ---------- internals ----------
    def _pump(self) -> None:
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

    def _build_args(self, codec: str, opts: Dict[str, Any]) -> List[str]:
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
            overwrite = bool((job.get("opts") or {}).get("overwrite"))
            dst = src.with_suffix(spec["ext"])
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
                if dst == src:
                    dst = src.with_name(src.stem + " (converted)" + spec["ext"])
                n = 1
                while dst.exists():
                    dst = src.with_name(f"{src.stem} ({n}){spec['ext']}")
                    n += 1
            job["state"] = "running"
            job["out"] = dst.name
            dur = self._duration(src)
            args = self._build_args(job["codec"], job["opts"])
            meta: List[str] = ["-map_metadata", "0"]
            for k, v in self._missing_tags(src).items():
                meta += ["-metadata", f"{k}={v}"]
            cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y",
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
            with self._lock:
                self._active -= 1
            self._pump()
