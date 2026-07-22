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
import re
import sys
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional

import yaml

from lidarr import LidarrClient, LidarrConfig
from qbittorrent_client import QbtClient
from dedup_downloads import AUDIO_EXTS, album_complete_in_library, human

logger = logging.getLogger("qbt_deselect")

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _clean_album(name: str) -> str:
    """'01. Album (1985) [FLAC] {CAT-123}' -> 'Album'."""
    s = re.sub(r"^\s*\d+[\.\-]\s*", "", name)
    s = re.sub(r"^\s*(19|20)\d{2}\s*[\.\-]\s*", "", s)
    s = re.sub(r"[\(\[\{][^)\]\}]*[\)\]\}]", " ", s)
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


def plan_torrent(
    lidarr: LidarrClient, torrent_name: str, files: List[Dict[str, Any]],
    forced_artist: str = "",
) -> List[Dict[str, Any]]:
    """
    Return a per-album plan for a torrent's audio files:
    [{artist, album, files:[...], size, have:bool, have_count, total}].
    Empty if the torrent has no audio.
    """
    audio = [f for f in files if Path(f.get("name", "")).suffix.lower() in AUDIO_EXTS]
    if not audio:
        return []
    groups: Dict[str, list] = {}
    for f in audio:
        parts = _rel_parts(f.get("name", ""))
        key = "/".join(parts[:-1]) if len(parts) >= 2 else torrent_name
        groups.setdefault(key, []).append(f)

    plan: List[Dict[str, Any]] = []
    for key, afiles in sorted(groups.items()):
        parts = _rel_parts(key)
        album = _clean_album(parts[-1]) if parts else _clean_album(torrent_name)
        artist = (forced_artist or
                  (_clean_artist(parts[0]) if len(parts) >= 2 else _clean_artist(torrent_name)))
        complete, have, total = album_complete_in_library(lidarr, artist, album)
        # Deselect only when the library fully has it AND has >= as many
        # tracks as the torrent folder (don't drop a bigger edition).
        deselect = bool(complete and total >= len(afiles))
        plan.append({
            "artist": artist, "album": album, "files": afiles,
            "size": sum(int(x.get("size") or 0) for x in afiles),
            "have": deselect, "have_count": have, "total": total,
        })
    return plan


def process_torrent(
    qbt: QbtClient, lidarr: LidarrClient, torrent: Dict[str, Any],
    forced_artist: str = "", apply: bool = False,
    emit: Callable[[str], None] = logger.info,
) -> tuple:
    """Plan + (optionally) deselect one torrent. Returns (deselected, kept)."""
    thash = torrent.get("hash")
    tname = torrent.get("name") or "?"
    plan = plan_torrent(lidarr, tname, qbt.files(thash), forced_artist)
    if not plan:
        return 0, 0
    emit(f"Torrent: {tname}")
    to_deselect: List[int] = []
    deselected = kept = 0
    for a in sorted(plan, key=lambda x: -x["size"]):
        if a["have"]:
            deselected += 1
            to_deselect.extend(int(x["index"]) for x in a["files"] if "index" in x)
            emit(f"  HAVE  [{human(a['size']):>9}]  {a['artist']} / {a['album']} "
                 f"(library {a['have_count']}/{a['total']}) -> deselect {len(a['files'])}")
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
) -> int:
    """
    One scheduled pass for the pipeline: for each INCOMPLETE music torrent we
    haven't handled yet, deselect already-have albums. Marks torrents seen so
    we don't reprocess. Returns number of torrents acted on.
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
        d, _k = process_torrent(qbt, lidarr, t, apply=True, emit=emit)
        seen.add(h)
        if d:
            acted += 1
    return acted


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
                               apply=args.apply, emit=print)
        tot_d += d
        tot_k += k
    print(f"\nSummary: {tot_d} already-have album(s) (deselect), {tot_k} to download.")
    if not args.apply:
        print("DRY RUN -- nothing changed. Re-run with --apply to deselect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
