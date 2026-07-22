"""
One-off cleanup: delete download folders whose album is ALREADY fully in
the Lidarr library. Safe by design -- a folder is only ever removed when
Lidarr confirms every monitored track of that album has an imported file,
AND the library has at least as many tracks as the download (so a larger
edition is never deleted against a smaller complete album).

Usage (from C:\\ESD\\cue_pipeline, with the venv active):

    python dedup_downloads.py                 # DRY RUN -- shows what it would do
    python dedup_downloads.py --delete        # actually delete the dupes
    python dedup_downloads.py --config config.yaml

Nothing in the LIBRARY is ever touched -- Lidarr is queried read-only and
only the redundant DOWNLOAD folders are removed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

# Windows consoles default to cp1252 and choke on album names with curly
# quotes, accents, emoji, etc. Force UTF-8 so printing paths never crashes.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def is_disc_folder(name: str) -> bool:
    """
    True if a folder name is ONE disc of a multi-disc set, so we never
    auto-delete a partial album. Catches disc DESIGNATORS -- 'CD1', 'CD 2',
    'Disc2', 'Disk 3', bare 'Disc', 'Green disc', 'Bonus Disc' -- while NOT
    tripping on a 'CD' *source tag* inside a long release name (e.g.
    '[FLAC CD]', '-CD-FLAC-', '{CD}', 'Pearl (Columbia CD 64188)').

    Rule: a glued designator like 'cd1'/'disc2' always counts; a bare
    cd/disc/disk token only counts when the whole folder name is short
    (<=3 tokens), i.e. the name's whole purpose is naming a disc.
    """
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]
    for tok in toks:
        if re.match(r"^(cd|disc|disk)\d+$", tok):   # cd1, disc2, disk3
            return True
    if len(toks) <= 3 and any(t in ("cd", "disc", "disk") for t in toks):
        return True                                  # "CD 2", "Green disc", "Disc"
    return False


def norm_title(s: str) -> str:
    """
    Aggressively normalize an album title for EQUALITY comparison:
    lowercase, & -> and, drop ()/[] edition tags, strip all non-alphanumerics.
    So "Traveler's Blues" == "Travelers Blues", "Up!" == "Up", and
    "Rare Pearls" != "Pearl", "Congratulations Remixes [EP]" != "Congratulations".
    """
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s)   # drop (Deluxe), [EP], (Japan)...
    return re.sub(r"[^a-z0-9]+", "", s)

from lidarr import LidarrClient, LidarrConfig

AUDIO_EXTS = {
    ".flac", ".ape", ".wv", ".wav", ".aiff", ".aif",
    ".m4a", ".m4b", ".alac",
    ".mp3", ".ogg", ".opus", ".oga",
    ".wma", ".dsf", ".dff", ".tak", ".tta", ".shn",
}

# Anything here marks a folder as NON-music (TV, movies, concert/bonus DVDs).
# The music cleanup must never delete or re-acquire a folder containing these.
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".iso", ".vob", ".flv", ".webm", ".divx", ".ogm",
}


def read_tags(path: Path) -> tuple[str, str]:
    """(artist, album) from a file's tags, filename fallback. Empty on failure."""
    artist = album = ""
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(path))
        if mf is not None and mf.tags:
            def first(keys):
                for k in keys:
                    v = mf.tags.get(k)
                    if v:
                        if isinstance(v, list) and v:
                            v = v[0]
                        s = str(v).strip()
                        if s:
                            return s
                return ""
            artist = first(("albumartist", "ALBUMARTIST", "artist", "ARTIST",
                            "\xa9ART", "aART", "TPE1", "TPE2"))
            album = first(("album", "ALBUM", "\xa9alb", "TALB"))
    except Exception:
        pass
    if not artist or not album:
        parts = [p.strip() for p in path.stem.split(" - ")]
        if len(parts) >= 2:
            artist = artist or parts[0]
            album = album or parts[1]
    return artist, album


def album_complete_in_library(
    lidarr: LidarrClient, artist: str, album: str, _cache: dict = None, llm=None
):
    """
    Return (complete: bool, have: int, total: int). `complete` is True only
    when Lidarr knows the artist+album and every monitored track has a file.
    Mirrors the pipeline's _album_already_in_library check.

    Matching is EXACT on a normalized title against the artist's full album
    list -- not Lidarr's fuzzy find_album substring search, which both
    over-matched ("Rare Pearls" -> "Pearl") and, more damagingly here,
    under-matched when the download folder carried a year/label prefix.
    `_cache` (dict) memoizes the per-artist (record, albums) lookup so a
    100-folder discography doesn't hammer the API.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    if not artist or not album:
        return False, 0, 0
    try:
        if _cache is None:
            _cache = {}
        akey = artist.lower()
        if akey in _cache:
            arec, albums = _cache[akey]
        else:
            arec = lidarr.find_artist(artist)
            albums = lidarr.list_albums_for_artist(arec["id"]) if arec else []
            _cache[akey] = (arec, albums)
        if not arec:
            return False, 0, 0
        target = norm_title(album)
        alb = None
        for a in albums:
            if norm_title(a.get("title")) == target:
                alb = a
                break
        # AI fallback: only when the deterministic match missed. Ask the LLM to
        # map the download folder to an album the user ACTUALLY OWNS (has files
        # for). It never sees not-owned albums, so it can't cause us to deselect
        # something absent -- ownership stays grounded in Lidarr's data.
        if alb is None and llm is not None:
            def _owned(a: Dict[str, Any]) -> bool:
                st = a.get("statistics") or {}
                f = int(st.get("trackFileCount") or 0)
                t = int(st.get("totalTrackCount") or 0)
                return f > 0 and t > 0 and f >= t
            owned = sorted({a.get("title", "") for a in albums
                            if a.get("title") and _owned(a)})
            if owned:
                picked = llm.pick_owned_album(album, owned)
                if picked:
                    for a in albums:
                        if a.get("title") == picked:
                            alb = a
                            break
        if not alb:
            return False, 0, 0
        album_id = alb.get("id")
        full = lidarr.get_album(album_id) or {}
        stats = full.get("statistics") or alb.get("statistics") or {}
        sfile = int(stats.get("trackFileCount") or 0)
        stotal = int(stats.get("totalTrackCount") or 0)
        tfile = ttotal = 0
        for t in (lidarr.list_tracks_for_album(album_id) or []):
            if not t.get("monitored", True):
                continue
            ttotal += 1
            if t.get("hasFile"):
                tfile += 1
    except Exception as exc:
        print(f"    ! Lidarr lookup failed for {artist} / {album}: {exc}")
        return False, 0, 0

    def complete(h, w):
        return h > 0 and w > 0 and h >= w

    if complete(sfile, stotal):
        return True, sfile, stotal
    if complete(tfile, ttotal):
        return True, tfile, ttotal
    return False, max(sfile, tfile), max(stotal, ttotal)


def dir_size(path: Path) -> int:
    total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += (Path(dp) / f).stat().st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} PB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument("--delete", action="store_true",
                    help="delete folders ALREADY IN LIBRARY (safe dupes)")
    ap.add_argument("--delete-empty", action="store_true",
                    help="delete EMPTY SHELLS (folders with no audio at all)")
    ap.add_argument("--delete-unmatched", action="store_true",
                    help="delete UNMATCHED audio folders Lidarr has NOTHING for "
                         "-- PERMANENT loss, these exist nowhere else")
    args = ap.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    lcfg = cfg["lidarr"]
    watch_root = Path(cfg["watch"]["root"])
    # Honor the same exclusions as the pipeline: `watch.exclude_dirs`,
    # entries either absolute or relative to watch.root (e.g. "torrents").
    excluded = []
    for p in (cfg["watch"].get("exclude_dirs") or []):
        pp = Path(p)
        excluded.append(pp if pp.is_absolute() else (watch_root / pp))

    lidarr = LidarrClient(LidarrConfig(
        base_url=lcfg["base_url"],
        api_key=lcfg["api_key"],
        library_root_lidarr=lcfg["library_root_lidarr"],
        library_root_windows=lcfg["library_root_windows"],
        path_mapping_from=lcfg["path_mapping"]["from"],
        path_mapping_to=lcfg["path_mapping"]["to"],
    ))
    if not lidarr.ping():
        print("Lidarr unreachable -- aborting (won't delete without confirmation).")
        return 1

    excluded_res = []
    for e in excluded:
        try:
            excluded_res.append(e.resolve(strict=False))
        except OSError:
            pass

    def is_excluded(p: Path) -> bool:
        try:
            pr = p.resolve(strict=False)
        except OSError:
            pr = p
        for e in excluded_res:
            if pr == e or e in pr.parents:
                return True
        # never touch the pipeline's own parked-originals area
        return "_processed" in p.parts

    # Single walk: collect dirs that DIRECTLY contain audio and dirs that
    # DIRECTLY contain video. Video marks a folder tree as non-music -- we
    # never delete or re-acquire anything with video under it (TV, movies,
    # concert/bonus DVDs live in this same downloads folder).
    candidates: list[Path] = []
    video_dirs: list[Path] = []
    for dp, dn, fn in os.walk(watch_root):
        folder = Path(dp)
        if is_excluded(folder):
            dn[:] = []
            continue
        exts = {Path(f).suffix.lower() for f in fn}
        if exts & VIDEO_EXTS or folder.name.upper() == "VIDEO_TS":
            video_dirs.append(folder)
        if exts & AUDIO_EXTS:
            candidates.append(folder)

    def has_video_under(folder: Path) -> bool:
        """True if `folder` itself or any subfolder holds video."""
        for vd in video_dirs:
            if vd == folder or folder in vd.parents:
                return True
        return False

    dupes: list = []       # fully in library                -> --delete
    review: list = []      # multi-disc, in library           -> manual only
    partial: list = []     # library has SOME tracks          -> NEVER delete
    unmatched: list = []   # audio present, Lidarr has NOTHING -> --delete-unmatched
    skipped_video: list = []  # contains video -> NEVER touched
    toplevel_with_audio: set = set()

    def _toplevel(folder: Path) -> Path:
        try:
            return watch_root / folder.relative_to(watch_root).parts[0]
        except (ValueError, IndexError):
            return folder

    print(f"Scanning {len(candidates)} music folder(s) under {watch_root}\n")
    for folder in candidates:
        # The pipeline may be running concurrently and moving/deleting source
        # folders out from under us -- tolerate a folder that's already gone.
        try:
            audios = [p for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        except OSError:
            continue
        if not audios:
            continue
        toplevel_with_audio.add(str(_toplevel(folder)).lower())
        # Music folder that also carries video (album + bonus DVD, etc.):
        # leave it entirely -- deleting would destroy the video.
        if has_video_under(folder):
            skipped_video.append((folder, dir_size(folder)))
            continue
        artist = album = ""
        for a in audios:
            artist, album = read_tags(a)
            if artist and album:
                break
        complete, have, total = album_complete_in_library(lidarr, artist, album)
        if complete and total >= len(audios):
            # Multi-disc safety: a disc subfolder (CD1/Disc 2/...) holds only
            # PART of the album, so route to review, not auto-delete.
            if is_disc_folder(folder.name):
                review.append((folder, dir_size(folder), have, total))
            else:
                dupes.append((folder, dir_size(folder), have, total))
        elif total == 0:
            # Lidarr knows nothing about this artist/album at all.
            unmatched.append((folder, dir_size(folder), len(audios)))
        else:
            # Library has SOME of it -- might hold the tracks this download is
            # missing (or vice-versa). Never auto-delete.
            partial.append((folder, have, total, len(audios)))

    # Empty shells: top-level folders with NO audio anywhere -- BUT only true
    # metadata leftovers. A no-audio folder that holds VIDEO is a TV/movie/DVD
    # download, NOT a shell -- never delete it.
    SHELL_MAX_BYTES = 100 * 1024 * 1024   # 100 MB: art/nfo/log, never media
    shells: list = []
    try:
        children = sorted(watch_root.iterdir())
    except OSError:
        children = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if is_excluded(child) or str(child).lower() in toplevel_with_audio:
            continue
        if has_video_under(child):
            skipped_video.append((child, dir_size(child)))
            continue
        # Never treat structural / system / pipeline folders as shells.
        if child.name.lower() in {
            "audio", "music", "cue_pipeline", "_processed", "_unmatched",
            "torrents", "scans", "artwork",
        }:
            skipped_video.append((child, dir_size(child)))
            continue
        # Multi-disc parent (holds CD1/Disc 2/Green disc/... subfolders):
        # deleting it would take the disc subfolders with it. Never a shell.
        try:
            if any(d.is_dir() and is_disc_folder(d.name) for d in child.iterdir()):
                skipped_video.append((child, dir_size(child)))
                continue
        except OSError:
            continue
        sz = dir_size(child)
        if sz <= SHELL_MAX_BYTES:
            shells.append((child, sz))
        else:
            # No audio, no video, but big -- unknown. Leave it alone.
            skipped_video.append((child, sz))

    # Drop any dupe nested under another dupe (delete the parent once).
    dupe_paths = {f for f, *_ in dupes}
    dupes = [(f, s, h, t) for (f, s, h, t) in dupes
             if not any(anc in dupe_paths for anc in f.parents)]

    def _report(title: str, rows: list) -> None:
        print(f"=== {title} ===")
        tot = 0
        for row in sorted(rows, key=lambda x: -x[1]):
            f, s = row[0], row[1]
            extra = f"   (library {row[2]}/{row[3]})" if len(row) >= 4 else ""
            print(f"  [{human(s):>10}]  {f}{extra}")
            tot += s
        print(f"\n  {len(rows)} folder(s), {human(tot)}\n")

    _report("ALREADY IN LIBRARY -- safe dupes  (--delete)", dupes)
    _report("EMPTY SHELLS -- tiny metadata leftovers  (--delete-empty)", shells)
    _report("UNMATCHED -- Lidarr has NOTHING; PERMANENT loss  (--delete-unmatched)",
            unmatched)
    if review:
        _report("REVIEW -- multi-disc, in library  (manual only)", review)
    if skipped_video:
        _report("SKIPPED -- video / non-music / bonus DVD (NEVER touched)",
                skipped_video)
    print("=== PARTIAL -- library has SOME tracks (never auto-deleted) ===")
    for f, have, total, n in partial:
        print(f"  {f}   (library {have}/{total}, folder {n})")
    print(f"\n  {len(partial)} folder(s) kept\n")

    if not (args.delete or args.delete_empty or args.delete_unmatched):
        print("DRY RUN -- nothing deleted. Flags:")
        print("  --delete            ALREADY-IN-LIBRARY dupes (safe)")
        print("  --delete-empty      EMPTY SHELLS (safe)")
        print("  --delete-unmatched  UNMATCHED audio -- PERMANENT, exists nowhere else")
        return 0

    def _do_delete(rows: list, label: str) -> tuple:
        removed = freed = 0
        for row in rows:
            f, s = row[0], row[1]
            try:
                shutil.rmtree(f)
                removed += 1
                freed += s
                print(f"  deleted [{label}] {f}")
            except OSError as exc:
                print(f"  ! failed to delete {f}: {exc}")
        return removed, freed

    tot_r = tot_f = 0
    if args.delete:
        r, fr = _do_delete(dupes, "in-library"); tot_r += r; tot_f += fr
    if args.delete_empty:
        r, fr = _do_delete(shells, "empty-shell"); tot_r += r; tot_f += fr
    if args.delete_unmatched:
        r, fr = _do_delete(unmatched, "unmatched"); tot_r += r; tot_f += fr
    print(f"\nDeleted {tot_r} folder(s), freed {human(tot_f)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
