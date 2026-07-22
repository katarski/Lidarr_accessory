"""
Selective download for qBittorrent: for a torrent (e.g. an artist discography),
deselect the albums you ALREADY HAVE in Lidarr's library so only the missing
ones download.

Works off the torrent's FILE PATHS (the files aren't downloaded yet, so there
are no tags) + Lidarr's library state.

Two ways to use it:
  * CLI, dry-run first:
        python qbt_deselect.py --name "Dire Straits" --artist "Dire Straits"
        python qbt_deselect.py --name "Dire Straits" --artist "Dire Straits" --apply
  * Built into the pipeline (auto mode): main.py runs auto_deselect_pass() on a
    schedule when qbittorrent.auto_deselect is true.

Safety: a whole album is deselected only when Lidarr reports it 100% present
AND its title normalizes-equal (no fuzzy over-match). Mis-parses err toward
KEEP (download), never toward deselecting something you don't have.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional

import yaml

from lidarr import LidarrClient, LidarrConfig
from ollama_client import OllamaClient
from qbittorrent_client import QbtClient
from dedup_downloads import AUDIO_EXTS, album_complete_in_library, human

logger = logging.getLogger("qbt_deselect")

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
# Leading "1960 " / "1960,1961 " / "1960 - " / "1960. " year prefix(es).
_LEAD_YEARS = re.compile(r"^\s*(?:19|20)\d{2}(?:\s*,\s*(?:19|20)\d{2})*[\s.\-]+")
# Leading "01. " / "01 - " track/disc number (needs a . or - separator so a
# real leading number like "20 Greatest Hits" is left alone).
_LEAD_NUM = re.compile(r"^\s*\d+\s*[.\-]\s*")
# "Artist - YYYY - Album" (or "Artist - YYYY. Album") -> keep only "Album".
# The year must sit AFTER a " - " (so a real trailing year like "Live In 1960"
# with no prefix is left alone) and be FOLLOWED by a - or . separator (so it's
# clearly a prefix, not part of the title). Lazy prefix + leftmost match means
# the FIRST such " - YYYY - " wins, and the album keeps any internal " - ".
_ARTIST_YEAR_ALBUM = re.compile(r"(?i)^.*?\s-\s*(?:19|20)\d{2}\s*[-.]\s*(.+)$")
# A disc subfolder: "CD1", "CD 1", "Disc 2", "disc-3", "DVD1", a bare "1"/"2",
# or a titled disc like "Disc 1 - Shout Sister Shout" / "CD2 Rock Me". Requires
# a digit right after the keyword so real albums ("Discovery") aren't matched.
_DISC_DIR = re.compile(r"(?i)^(?:cd|disc|disk|dvd|side)\s*[.\-_]?\s*\d{1,3}\b|^\d{1,2}$")
# A non-audio sidecar subfolder (art/scans/etc.) -- audio never lives here, but
# guard anyway so it never becomes an "album".
_ART_DIR = re.compile(
    r"(?i)^-?(?:scans?|artwork|art|covers?|cover|sleeves?|booklet|"
    r"images?|thumbs?|logs?|info)$"
)


def _clean_album(name: str, artist: str = "") -> str:
    """
    Turn a messy album-folder name into a bare album title:
      '1988 Etta James - Seven Year Itch (1988 Canada Island CID-1210)'
        -> 'Seven Year Itch'
      '1960,1961 Etta James - At Last, The Second Time Around (2012 ...)'
        -> 'At Last, The Second Time Around'
    `artist` (when known) lets us strip an embedded 'Artist - ' prefix
    (including '& guest' credits) without eating album titles that merely
    contain ' - '.
    """
    s = _LEAD_NUM.sub("", name)          # '01. ' / '01 - '
    s = _LEAD_YEARS.sub("", s)           # '1960 ' / '1960,1961 ' / '1960 - '
    m = _ARTIST_YEAR_ALBUM.match(s)      # 'Artist - 2005 - Gospel Train' -> 'Gospel Train'
    if m:
        s = m.group(1)
    if artist:
        na = re.escape(artist.strip())
        # 'Etta James - ...' or 'Etta James & Eddie ... - ...' at the start.
        s = re.sub(rf"(?i)^\s*{na}\b[^-]*?-\s*", "", s, count=1)
    s = re.sub(r"[(\[\{][^)\]\}]*[)\]\}]", " ", s)   # drop (edition)[tag]{cat}
    return re.sub(r"\s{2,}", " ", s).strip(" -_.")


def _clean_artist(name: str) -> str:
    s = re.sub(r"[\(\[\{][^)\]\}]*[\)\]\}]", " ", name)
    s = re.sub(r"(?i)\b(discography|complete|collection|studio albums?|flac|mp3)\b", " ", s)
    s = _YEAR.sub(" ", s)
    if " - " in s:
        s = s.split(" - ", 1)[0]
    return re.sub(r"\s{2,}", " ", s).strip(" -_.")


def _rel_parts(name: str) -> list:
    return [p for p in name.replace("\\", "/").split("/") if p]


def _album_dir_parts(folder_parts: list) -> list:
    """
    Given the folder path of a file (no filename), walk up past any trailing
    disc ('Disc 1', 'CD2') or sidecar ('Artwork', 'Scans', 'Covers')
    subfolders to the real album folder, so multi-disc albums group as ONE
    album, sidecar files map to their album, and a disc/art folder never
    becomes the album title.
    """
    p = list(folder_parts)
    while len(p) > 1 and (_DISC_DIR.match(p[-1]) or _ART_DIR.match(p[-1])):
        p.pop()
    return p


def _file_album_key(f: Dict[str, Any], torrent_name: str) -> str:
    """The album-folder key a file belongs to (audio OR sidecar)."""
    parts = _rel_parts(f.get("name", ""))
    folder_parts = parts[:-1] if len(parts) >= 2 else []
    adir = _album_dir_parts(folder_parts)
    return "/".join(adir) if adir else torrent_name


def plan_torrent(
    lidarr: LidarrClient, torrent_name: str, files: List[Dict[str, Any]],
    forced_artist: str = "", llm=None,
) -> List[Dict[str, Any]]:
    """
    Return a per-album plan for a torrent's audio files:
    [{artist, album, files:[...], size, have:bool, have_count, total}].
    Empty if the torrent has no audio.
    """
    audio = [f for f in files if Path(f.get("name", "")).suffix.lower() in AUDIO_EXTS]
    if not audio:
        return []
    # Group AUDIO by album folder (this drives the have-decision), and ALL
    # files by the same folder key (this drives what we deselect). When an
    # album is already in the library we deselect the WHOLE folder -- .cue,
    # .log, covers, art, everything -- not just the audio. A leftover .cue
    # would otherwise download and trip the pipeline's watcher.
    groups: Dict[str, list] = {}
    all_by_key: Dict[str, list] = {}
    for f in audio:
        groups.setdefault(_file_album_key(f, torrent_name), []).append(f)
    for f in files:
        all_by_key.setdefault(_file_album_key(f, torrent_name), []).append(f)

    # Cache Lidarr artist/album lookups across this torrent's many folders
    # (a discography can be 100+ folders -- don't re-query per folder).
    lib_cache: Dict[str, Any] = {}
    plan: List[Dict[str, Any]] = []
    for key, afiles in sorted(groups.items()):
        parts = _rel_parts(key)
        raw_artist = parts[0] if len(parts) >= 2 else torrent_name
        artist = forced_artist or _clean_artist(raw_artist)
        album_raw = parts[-1] if parts else torrent_name
        album = _clean_album(album_raw, artist=(forced_artist or raw_artist))
        complete, have, total = album_complete_in_library(
            lidarr, artist, album, _cache=lib_cache, llm=llm
        )
        # Deselect only when the library fully has it AND has >= as many
        # tracks as the torrent folder (don't drop a bigger edition).
        deselect = bool(complete and total >= len(afiles))
        allf = all_by_key.get(key, afiles)
        plan.append({
            "artist": artist, "album": album,
            "files": afiles,       # audio only (drives the have-decision)
            "all_files": allf,     # every file in the folder (what we deselect)
            "size": sum(int(x.get("size") or 0) for x in allf),
            "have": deselect, "have_count": have, "total": total,
        })
    return plan


def process_torrent(
    qbt: QbtClient, lidarr: LidarrClient, torrent: Dict[str, Any],
    forced_artist: str = "", apply: bool = False,
    emit: Callable[[str], None] = logger.info,
    files: Optional[List[Dict[str, Any]]] = None,
    llm=None,
) -> tuple:
    """Plan + (optionally) deselect one torrent. Returns (deselected, kept)."""
    thash = torrent.get("hash")
    tname = torrent.get("name") or "?"
    if files is None:
        files = qbt.files(thash)
    plan = plan_torrent(lidarr, tname, files, forced_artist, llm=llm)
    if not plan:
        return 0, 0
    emit(f"Torrent: {tname}")
    to_deselect: List[int] = []
    deselected = kept = 0
    for a in sorted(plan, key=lambda x: -x["size"]):
        if a["have"]:
            deselected += 1
            folder_files = a.get("all_files", a["files"])
            to_deselect.extend(int(x["index"]) for x in folder_files if "index" in x)
            n_audio = len(a["files"])
            n_all = len(folder_files)
            extra = f" (+{n_all - n_audio} sidecar)" if n_all > n_audio else ""
            emit(f"  HAVE  [{human(a['size']):>9}]  {a['artist']} / {a['album']} "
                 f"(library {a['have_count']}/{a['total']}) -> deselect {n_all} file(s){extra} [whole folder]")
        else:
            kept += 1
            why = "not in library" if a["total"] == 0 else f"library {a['have_count']}/{a['total']}"
            emit(f"  KEEP  [{human(a['size']):>9}]  {a['artist']} / {a['album']} ({why})")
    if apply and to_deselect:
        ok = qbt.set_file_priority(thash, to_deselect, 0)
        emit(f"  -> {'deselected' if ok else 'FAILED'} {len(to_deselect)} file(s)")
    return deselected, kept


def auto_deselect_pass(
    qbt: QbtClient, lidarr: LidarrClient, seen: set,
    category: str = "", emit: Callable[[str], None] = logger.info,
    pause_during_scan: bool = True, llm=None,
) -> int:
    """
    One scheduled pass for the pipeline: for each INCOMPLETE music torrent we
    haven't handled yet, deselect already-have albums. Marks torrents seen so
    we don't reprocess. Returns number of torrents acted on.

    To keep bandwidth from leaking on already-owned albums before we act, a
    freshly-seen torrent is PAUSED the instant we notice it, its file list is
    read, the owned albums are deselected, and only then is its ORIGINAL
    start-state restored. We never override what Lidarr/you set: a force-started
    torrent comes back force-started, a normal one comes back normally started,
    and a "don't start" (stopped/paused) torrent is left stopped and never
    paused in the first place. A torrent whose metadata hasn't resolved yet
    (magnet, empty file list) is left as-is and retried next pass.
    """
    acted = 0
    for t in qbt.torrents(category=category):
        h = t.get("hash")
        if not h or h in seen:
            continue
        # Only touch torrents that are still downloading (progress < 1.0);
        # nothing to gain deselecting a finished one.
        if float(t.get("progress") or 0) >= 1.0:
            seen.add(h)
            continue
        # Capture the torrent's original start-state so we restore EXACTLY what
        # Lidarr/you set -- never impose one.
        state = (t.get("state") or "").lower()
        was_forced = bool(t.get("force_start")) or state.startswith("forced")
        was_stopped = ("paused" in state) or ("stopped" in state)
        paused_by_us = False
        try:
            # Pause only a running torrent (to stop owned albums downloading
            # while we decide). A "don't start" torrent is left alone.
            if pause_during_scan and not was_stopped:
                qbt.pause(h)
                paused_by_us = True
            files = qbt.files(h)
            if not files:
                # Metadata not ready (e.g. magnet still resolving). Can't plan
                # without the file list -- retry next pass, don't mark seen.
                # (The finally-block restores the original state meanwhile.)
                continue
            d, _k = process_torrent(
                qbt, lidarr, t, apply=True, emit=emit, files=files, llm=llm
            )
            seen.add(h)
            if d:
                acted += 1
        finally:
            # Restore the original intent, in priority order.
            if was_stopped:
                pass                      # leave a "don't start" torrent stopped
            elif was_forced:
                qbt.force_start(h)        # preserve Lidarr's force-start
            elif paused_by_us:
                qbt.resume(h)             # normal start (only if we paused it)
    return acted


def _map_to_download_root(content_path: str, save_path: str, download_root: str) -> str:
    """
    qBittorrent reports a torrent's content_path under ITS OWN save path
    (e.g. /data/Foo). Map that to the path the pipeline sees the same files at
    (download_root, e.g. /downloads/Foo) by swapping the save-path prefix.
    """
    cp = (content_path or "").replace("\\", "/")
    sp = (save_path or "").replace("\\", "/").rstrip("/")
    if sp and (cp == sp or cp.startswith(sp + "/")):
        rel = cp[len(sp):].lstrip("/")
    else:
        rel = os.path.basename(cp)
    return os.path.join(download_root, rel) if rel else download_root


def _count_audio_on_disk(path: str) -> int:
    n = 0
    for _dp, _dn, fn in os.walk(path):
        for x in fn:
            if os.path.splitext(x)[1].lower() in AUDIO_EXTS:
                n += 1
    return n


def torrent_lifecycle_pass(
    qbt: QbtClient, download_root: str, category: str = "",
    emit: Callable[[str], None] = logger.info,
) -> tuple:
    """
    Manage COMPLETED music torrents by how much of their content the pipeline
    has already moved into the library (i.e. deleted from the download folder):

      * fully moved      (no audio left on disk)  -> REMOVE torrent + data
      * partially moved  (some audio gone)        -> PAUSE (stop seeding while
                                                      the rest finishes importing)
      * nothing moved yet (all audio still there) -> leave running

    Non-music torrents (no selected audio -- TV, movies) are ignored.
    Reads the real download folder, so it never deletes a torrent whose files
    haven't actually been imported. Returns (removed, paused).
    """
    # Guard: if the download root isn't visible, do NOTHING -- otherwise an
    # unmounted path would look like "everything moved" and nuke torrents.
    if not download_root or not os.path.isdir(download_root):
        emit(f"lifecycle: download root {download_root!r} not visible -- skipping")
        return 0, 0

    removed = paused = 0
    for t in qbt.torrents(category=category):
        if float(t.get("progress") or 0) < 1.0:
            continue  # still downloading -> the deselect pass owns it
        h = t.get("hash")
        if not h:
            continue
        files = qbt.files(h)
        sel_audio = sum(
            1 for f in files
            if f.get("priority", 1) != 0
            and os.path.splitext(f.get("name", ""))[1].lower() in AUDIO_EXTS
        )
        if sel_audio == 0:
            continue  # not a music torrent (or all audio deselected) -> ignore

        folder = _map_to_download_root(
            t.get("content_path"), t.get("save_path"), download_root
        )
        on_disk = _count_audio_on_disk(folder) if os.path.exists(folder) else 0
        state = (t.get("state") or "").lower()
        name = t.get("name") or "?"

        if on_disk == 0:
            # Everything moved to the library -> remove torrent + leftover data.
            if qbt.remove(h, delete_files=True):
                removed += 1
                emit(f"lifecycle: REMOVED (fully imported) {name!r}")
        elif on_disk < sel_audio:
            # Partially moved -> pause so it stops seeding while the pipeline
            # finishes importing the rest (paused torrents keep their files,
            # so the pipeline can still read them).
            if "paused" not in state and "stopped" not in state:
                qbt.pause(h)
                paused += 1
                emit(
                    f"lifecycle: PAUSED (imported {sel_audio - on_disk}/{sel_audio} "
                    f"so far) {name!r}"
                )
        # else on_disk == sel_audio: nothing imported yet -> leave running.
    return removed, paused


def main() -> int:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument("--name", default="")
    ap.add_argument("--hash", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-ai", action="store_true",
                    help="disable the LLM fallback for library matching")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    lc = cfg["lidarr"]
    qc = cfg.get("qbittorrent") or {}
    if not qc.get("base_url"):
        print("No [qbittorrent] section in config. Add base_url/username/password.")
        return 1

    lidarr = LidarrClient(LidarrConfig(
        base_url=lc["base_url"], api_key=lc["api_key"],
        library_root_lidarr=lc["library_root_lidarr"],
        library_root_windows=lc["library_root_windows"],
        path_mapping_from=lc["path_mapping"]["from"],
        path_mapping_to=lc["path_mapping"]["to"],
    ))
    if not lidarr.ping():
        print("Lidarr unreachable -- aborting.")
        return 1
    qbt = QbtClient(qc["base_url"], qc.get("username", ""), qc.get("password", ""))
    if not qbt.login():
        print("qBittorrent login failed -- check base_url/username/password.")
        return 1

    # Optional LLM client for the library-match fallback (mirrors main.py).
    llm = None
    if not args.no_ai:
        oc = cfg.get("ollama") or {}
        if oc.get("enabled", True):
            provider = str(oc.get("provider", "ollama")).lower()
            try:
                if provider in ("openai", "gemini", "cloud", "openai-compatible"):
                    from cloud_llm import CloudLLMClient
                    llm = CloudLLMClient(
                        base_url=oc.get("base_url", ""), model=oc.get("model", ""),
                        api_key=oc.get("api_key", ""),
                        timeout=int(oc.get("timeout_seconds", 60)),
                        enabled=True,
                    )
                else:
                    llm = OllamaClient(
                        base_url=oc.get("base_url", "http://127.0.0.1:11434"),
                        model=oc.get("model", "qwen2.5:14b"),
                        timeout=int(oc.get("timeout_seconds", 300)),
                        enabled=True,
                        keep_alive=str(oc.get("keep_alive", "30m")),
                        num_ctx=int(oc.get("num_ctx", 8192)),
                    )
                if llm and not llm.ping():
                    print(f"LLM ({provider}) unreachable -- AI match disabled for this run.")
                    llm = None
            except Exception as exc:  # noqa: BLE001
                print(f"LLM init failed ({exc}); AI match disabled.")
                llm = None

    torrents = qbt.torrents(category=args.category or qc.get("category", ""))
    if args.hash:
        torrents = [t for t in torrents if t.get("hash") == args.hash]
    if args.name:
        nl = args.name.lower()
        torrents = [t for t in torrents if nl in (t.get("name") or "").lower()]
    if not torrents:
        print("No matching torrents.")
        return 0

    tot_d = tot_k = 0
    for t in torrents:
        print("")
        d, k = process_torrent(qbt, lidarr, t, forced_artist=args.artist,
                               apply=args.apply, emit=print, llm=llm)
        tot_d += d
        tot_k += k
    print(f"\nSummary: {tot_d} already-have album(s) (deselect), {tot_k} to download.")
    if not args.apply:
        print("DRY RUN -- nothing changed. Re-run with --apply to deselect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
