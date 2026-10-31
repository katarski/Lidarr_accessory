"""
Torrents the owner added by hand stay as they are.

The owner, 31 Oct 2026: "if it hand added - it stays." And: "but listen. if it
found a better torrent than the hand added one - we keep the better one!"
Asked what that means for a hand-added torrent whose music Lidarr already
holds, he chose:

  * the hand-added torrent is WORSE and Lidarr holds every song of it in
    better quality -> "Remove it with its files";
  * the hand-added torrent is BETTER, or Lidarr lacks that music -> "Yes, copy
    it in": Lidarr imports a COPY, the torrent and its files stay exactly as
    they are.

A hand-added torrent is one with no qBittorrent category and no proof that this
pipeline grabbed it. Proof is any of: the pipeline's own tag
(QbtClient.SELF_ADDED_TAG); its hash in the pipeline's grab record
(grab_targets.json, a JSON object keyed by lowercase infohash); a Lidarr
history event of type Grabbed whose downloadId is the hash. The client is
shared with other apps, so anything without that proof is the owner's (or
another app's) and is never touched -- and a torrent whose proof could not be
asked for (Lidarr down) counts as hand-added too: it fails closed.

This module keeps the folders of those torrents, in the pipeline's view of the
download folder, so every pass that splits, moves or deletes in the download
folder can skip them (`protected`). Only Orchestrator.handadded_pass acts on
them, and only as the rules above say.

It imports no project module but the qBittorrent client (for the tag), so
every other module can import it without a cycle. Its state is shared by the
qBittorrent loop thread, the startup (main) thread, the cue-handadded thread
and the CUE worker: the dict and the tuple of roots are replaced together
under a lock and never mutated in place, so a reader takes a local reference.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from qbittorrent_client import QbtClient

logger = logging.getLogger("handadded")

# Lidarr's per-file verdict that the library holds a file at the same or a
# better quality. The same text as dedup_downloads.NOT_AN_UPGRADE, which this
# module cannot import (no project imports here); the orchestrator passes the
# real constant to library_strictly_better.
_NOT_AN_UPGRADE = "not an upgrade"

_LOCK = threading.Lock()
# lowercase infohash -> what classify() recorded; and the folders they cover.
# Both are replaced together under _LOCK (set_state, forget), never mutated.
_TORRENTS: Dict[str, Dict[str, Any]] = {}
_ROOTS: Tuple[str, ...] = ()

# grab_targets.json path -> ((mtime, size), hashes): re-read only on a change.
_GRAB_CACHE: Dict[str, Tuple[Tuple[float, int], frozenset]] = {}


# ---- paths -------------------------------------------------------------

def _norm(p: Any) -> str:
    """A path as one comparable string: forward slashes, no `.`/`..`/doubled
    separators, no trailing slash (except the root itself)."""
    s = os.path.normpath(str(p).replace("\\", "/")).replace("\\", "/")
    if len(s) > 1:
        s = s.rstrip("/") or "/"
    return s


def _opt(p: Any) -> str:
    """_norm() of a path that may be missing: '' when it is None or blank."""
    if p is None or not str(p).strip():
        return ""
    return _norm(p)


def _join(root: str, rel: str) -> str:
    return root.rstrip("/") + "/" + rel.lstrip("/")


def protected(path: Any) -> Optional[str]:
    """The hand-added root that equals `path` or is an ancestor of it, or None.
    Whole path components only: /downloads/Abc does not protect
    /downloads/Abcd."""
    if path is None:
        return None
    s = str(path)
    if not s.strip():
        return None
    roots = _ROOTS                         # a local reference, no lock
    if not roots:
        return None
    p = _norm(s)
    for r in roots:
        if p == r or p.startswith(r + "/"):
            return r
    return None


def roots_for(t: dict, watch_root: str,
              files: Optional[list] = None) -> List[str]:
    """Map a qBittorrent torrent to its folders in the pipeline's view.

    qBittorrent may mount the downloads elsewhere (/data/... against our
    /downloads), so the part of the content path below the save path is
    appended to the watch root, and so is the content's own name (a save path
    deeper than the mapping). A torrent with no root folder (its files sit
    directly in the save path) is covered by the first component of each of
    its files. [] when nothing maps."""
    wr = _opt(watch_root)
    if not wr:
        return []
    cp = _opt(t.get("content_path"))
    sp = _opt(t.get("save_path"))
    roots: List[str] = []

    def add(r: str) -> None:
        if r and r not in roots:
            roots.append(r)

    if cp and sp and cp != sp and cp.startswith(sp + "/"):
        rel = cp[len(sp) + 1:]
        add(_join(wr, rel))
        base = cp.rsplit("/", 1)[-1]
        if base and base != rel:
            add(_join(wr, base))
    elif cp and cp != sp:
        base = cp.rsplit("/", 1)[-1]
        if base:
            add(_join(wr, base))
    else:
        for f in files or []:
            name = str((f or {}).get("name") or "").replace("\\", "/")
            first = name.split("/")[0].strip()
            if first:
                add(_join(wr, first))
    return roots


def _has_root_folder(t: dict) -> bool:
    cp = _opt(t.get("content_path"))
    sp = _opt(t.get("save_path"))
    return bool(cp) and cp != sp


# ---- proof -------------------------------------------------------------

def _tags_of(t: dict) -> set:
    return {x.strip() for x in str(t.get("tags") or "").split(",")}


def _grab_record(path: Any) -> set:
    """The lowercase infohashes in the pipeline's grab record, or an empty set
    when the file is missing or unreadable. Read again only when it changes."""
    if not path:
        return set()
    key = str(path)
    try:
        st = os.stat(key)
    except OSError as exc:
        logger.debug("grab record %s: %s", key, exc)
        return set()
    sig = (st.st_mtime, st.st_size)
    hit = _GRAB_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return set(hit[1])
    try:
        with open(key, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.debug("grab record %s: %s", key, exc)
        return set()
    hashes = frozenset(str(k).strip().lower() for k in d) \
        if isinstance(d, dict) else frozenset()
    _GRAB_CACHE[key] = (sig, hashes)
    return set(hashes)


def make_proof(lidarr: Any, grab_targets_path: Any = None,
               ttl_negative: int = 21600,
               now: Callable[[], float] = time.time,
               ) -> Callable[[dict], Optional[bool]]:
    """A proof(t) function: True when the pipeline grabbed torrent `t`, False
    when nothing says so, None when that could not be found out (Lidarr could
    not be asked). Proof is the pipeline's own tag, its hash in the grab
    record, or a Lidarr Grabbed event for it. A True from Lidarr is kept for
    good, a False for `ttl_negative` seconds (a grab shows up in Lidarr's
    history at once, so a False rarely changes), a None never."""
    cache: Dict[str, Tuple[bool, float]] = {}
    cache_lock = threading.Lock()

    def proof(t: dict) -> Optional[bool]:
        h = str((t or {}).get("hash") or "").strip().lower()
        if not h:
            return None
        if QbtClient.SELF_ADDED_TAG in _tags_of(t):
            return True
        if grab_targets_path and h in _grab_record(grab_targets_path):
            return True
        t_now = now()
        with cache_lock:
            hit = cache.get(h)
        if hit is not None and (hit[0] or t_now - hit[1] < ttl_negative):
            return hit[0]
        try:
            res = lidarr.grabbed_download(h)
        except Exception as exc:  # noqa: BLE001
            logger.debug("grab proof for %s: %s", h[:12], exc)
            res = None
        if res is None:
            return None
        res = bool(res)
        with cache_lock:
            cache[h] = (res, t_now)
        return res

    return proof


# ---- the hand-added set ------------------------------------------------

def classify(torrents: list, proof: Callable[[dict], Optional[bool]],
             watch_root: str,
             files_of: Optional[Callable[[str], list]] = None,
             ) -> Dict[str, Dict[str, Any]]:
    """The hand-added torrents among `torrents`: no category, not tagged as the
    pipeline's own, and no proof that it grabbed them. A torrent the proof
    cannot settle (None) is kept as well: failing closed means leaving it
    alone. A proven one is skipped -- the adoption takes it."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in torrents or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("category") or "").strip():
            continue
        if QbtClient.SELF_ADDED_TAG in _tags_of(t):
            continue
        raw_hash = str(t.get("hash") or "").strip()
        if not raw_hash:
            continue
        try:
            p = proof(t)
        except Exception as exc:  # noqa: BLE001
            logger.debug("proof for %s: %s", raw_hash[:12], exc)
            p = None
        if p is True:
            continue
        files = None
        if files_of is not None and not _has_root_folder(t):
            try:
                files = files_of(raw_hash)
            except Exception as exc:  # noqa: BLE001
                logger.debug("files of %s: %s", raw_hash[:12], exc)
                files = None
        roots = roots_for(t, watch_root, files)
        if not roots:
            continue
        out[raw_hash.lower()] = {
            "name": t.get("name"),
            "hash": raw_hash.lower(),
            "roots": roots,
            "content_path": t.get("content_path"),
            "save_path": t.get("save_path"),
            "added_on": t.get("added_on"),
            "proof": "none" if p is False else "unknown",
        }
    return out


def _roots_of(torrents: Dict[str, Dict[str, Any]]) -> Tuple[str, ...]:
    roots: List[str] = []
    for info in torrents.values():
        for r in (info or {}).get("roots") or []:
            n = _opt(r)
            # Never the root of the filesystem or of nothing: that would turn
            # every path into a protected one (or none).
            if n and n not in (".", "/") and n not in roots:
                roots.append(n)
    return tuple(roots)


def set_state(torrents: Dict[str, Dict[str, Any]]) -> None:
    global _TORRENTS, _ROOTS
    new = {str(k).lower(): dict(v) for k, v in (torrents or {}).items()
           if isinstance(v, dict)}
    roots = _roots_of(new)
    with _LOCK:
        _TORRENTS, _ROOTS = new, roots


def torrents() -> Dict[str, Dict[str, Any]]:
    """The hand-added torrents known now (a shallow copy)."""
    return dict(_TORRENTS)


def forget(torrent_hash: str) -> None:
    """Drop one torrent (it was removed) and rebuild the folders."""
    global _TORRENTS, _ROOTS
    h = str(torrent_hash or "").strip().lower()
    with _LOCK:
        if h not in _TORRENTS:
            return
        new = {k: v for k, v in _TORRENTS.items() if k != h}
        _TORRENTS, _ROOTS = new, _roots_of(new)


def load(path: Any) -> int:
    """Read the saved set at startup, so the folders are protected before the
    first pass has asked qBittorrent. The number loaded; 0 (state kept) when
    the file is missing or unreadable."""
    try:
        with open(str(path), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        ts = d.get("torrents") if isinstance(d, dict) else None
        if not isinstance(ts, dict):
            raise ValueError("no 'torrents' object in the file")
    except FileNotFoundError:
        logger.debug("hand-added state %s: not there yet", path)
        return 0
    except (OSError, ValueError) as exc:
        logger.warning("hand-added state %s could not be read: %s", path, exc)
        return 0
    set_state(ts)
    return len(_TORRENTS)


def save(path: Any) -> None:
    try:
        body = {"torrents": torrents(), "saved": time.time()}
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, str(path))
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("hand-added state %s could not be saved: %s", path, exc)


def refresh(qbt: Any, proof: Callable[[dict], Optional[bool]],
            watch_root: str, path: Any = None,
            emit: Callable[[str], None] = logger.info) -> Optional[int]:
    """Re-read the torrents, rebuild the hand-added set and keep it on disk
    (when it changed). An empty answer is "qBittorrent did not answer" -- the
    real client always has torrents -- so the state is kept and None is
    returned. Otherwise the number of hand-added torrents."""
    ts = qbt.torrents()
    if not ts:
        return None
    new = classify(ts, proof, watch_root, files_of=qbt.files)
    old = torrents()
    for h, info in new.items():
        if h in old:
            continue
        emit("hand-added torrent %r: it stays as it is (no category, %s)" % (
            info.get("name") or h[:12],
            "no grab record" if info.get("proof") == "none"
            else "Lidarr could not be asked yet"))
    changed = set(new) != set(old)
    set_state(new)
    if path and changed:
        save(path)
    return len(new)


# What a music rip carries beside its audio: sheets, rip logs and checksums,
# artwork and scans, notes, playlists. A torrent holding anything else (video,
# a bonus DVD, programs, archives) is never removed: only its music was checked.
SIDECAR_EXTS = frozenset({
    ".cue", ".log", ".txt", ".nfo", ".m3u", ".m3u8", ".pls", ".accurip",
    ".sfv", ".md5", ".ffp", ".st5", ".toc", ".sha1", ".sha256",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".pdf",
})


def torrent_fully_checked(files: list, save_path: Any, content_path: Any,
                          watch_root: Any, checked: list,
                          audio_exts) -> Tuple[bool, str]:
    """Is what the removal checked ALL of the torrent? Its own file list
    (qBittorrent's names, relative to the save path) must hold only music and
    a rip's sidecars, every file must be downloaded (none deselected), and
    every audio file must be one of `checked` (the paths, in the pipeline's
    view, that library_strictly_better verified). (False, why) otherwise --
    including a torrent whose save path does not map onto the watch root."""
    if not files:
        return False, "its file list could not be read"
    sp, cp, wr = _opt(save_path), _opt(content_path), _opt(watch_root)
    if not (sp and cp and wr) or cp == sp or not cp.startswith(sp + "/"):
        return False, "its files cannot be mapped onto the download folder"
    seen = {_norm(c) for c in checked or []}
    exts = {str(e).lower() for e in audio_exts}
    for f in files:
        name = str((f or {}).get("name") or "").replace("\\", "/").strip("/")
        if not name:
            return False, "its file list has a nameless file"
        try:
            prio = int((f or {}).get("priority", 1))
        except (TypeError, ValueError):
            prio = 1
        if prio == 0:
            return False, "%s is not downloaded (deselected)" % name
        ext = os.path.splitext(name)[1].lower()
        if ext in exts:
            if _norm(_join(wr, name)) not in seen:
                return False, "%s was not checked" % name
        elif ext not in SIDECAR_EXTS:
            return False, "it holds more than music (%s)" % name
    return True, "all of its files were checked"


# ---- Lidarr holds it better --------------------------------------------

def library_strictly_better(
    cands: list, audio_paths: list,
    trackfile_quality: Dict[int, int], profile_index: Dict[int, int],
    not_an_upgrade: str = _NOT_AN_UPGRADE,
) -> Tuple[bool, str]:
    """Does Lidarr hold EVERY song of these files in a strictly better quality?

    `cands` is Lidarr's manual-import answer for the folder, `audio_paths` the
    files' paths as Lidarr sees them, `trackfile_quality` the quality id of
    each library file (by track-file id) and `profile_index` the artist's
    quality profile (quality id -> position, higher is better; a group's
    members share its position). (True, why) only when every file maps to
    tracks, Lidarr rejects it as "not an upgrade", every one of those tracks
    has a library file, and every such library file stands higher in the
    profile than the hand-added file. (False, why) for anything else -- a
    missing answer is never "better"."""
    if not audio_paths:
        return False, "it holds no audio"
    if not profile_index:
        return False, "the quality profile could not be read"
    by_path: Dict[str, dict] = {}
    for c in cands or []:
        if isinstance(c, dict) and c.get("path"):
            by_path[str(c["path"])] = c
    marker = str(not_an_upgrade or _NOT_AN_UPGRADE).lower()
    for p in audio_paths:
        name = os.path.basename(str(p))
        c = by_path.get(str(p))
        if c is None:
            return False, "%s is not in Lidarr's import check" % name
        tracks = c.get("tracks") or []
        if not tracks:
            return False, "%s matches no track in the library" % name
        reasons = [str((r or {}).get("reason") or "").lower()
                   for r in (c.get("rejections") or [])]
        if not any(marker in r for r in reasons):
            return False, "%s would be imported (Lidarr: %s)" % (
                name, "; ".join(reasons) or "accepted")
        fq = ((c.get("quality") or {}).get("quality") or {}).get("id")
        try:
            fi = profile_index.get(int(fq))
        except (TypeError, ValueError):
            fi = None
        if fi is None:
            return False, "the quality of %s is not in the profile" % name
        for tr in tracks:
            try:
                tfid = int((tr or {}).get("trackFileId") or 0)
            except (TypeError, ValueError):
                tfid = 0
            if tfid <= 0:
                return False, "a track of %s has no file in the library" % name
            lq = trackfile_quality.get(tfid)
            li = profile_index.get(lq) if lq is not None else None
            if li is None:
                return False, ("the quality of the library file for %s is "
                               "unknown" % name)
            if li <= fi:
                return False, ("the library file for %s is not in a better "
                               "quality" % name)
    return True, "Lidarr holds all %d song(s) in better quality" % len(audio_paths)
