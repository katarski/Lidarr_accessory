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
import time
from pathlib import Path
from typing import Callable, List, Dict, Any, Iterable, Optional, Tuple

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
# A DIGIT-LESS bonus/extras disc ("Bonus", "Bonus Disc", "Bonus CD", "Bonus
# Tracks", "Extras") -- climbed into its album so it isn't judged as a phantom
# separate album that never matches Lidarr (backlog #12).
_BONUS_DIR = re.compile(
    r"(?i)^-?(?:bonus(?:[\s._-]*(?:disc|cd|dvd|tracks?|material|cuts?))?|"
    r"extras?|bonuses)$"
)
# A non-audio sidecar subfolder (art/scans/etc.) -- audio never lives here, but
# guard anyway so it never becomes an "album".
_ART_DIR = re.compile(
    r"(?i)^-?(?:scans?|artwork|art|covers?|cover|sleeves?|booklet|"
    r"images?|thumbs?|logs?|info)$"
)
# Discography "noise" category subfolders -- bootlegs, live sets, singles,
# compilations, remixes, collaborations, etc. Inside a big discography grab
# these are deselected by DEFAULT (only real studio albums Lidarr wants are
# downloaded), UNLESS Lidarr explicitly tracks a matching (still-missing)
# album in that folder. Matches a CONTAINER segment, never the album name.
_NOISE_DIR = re.compile(
    r"(?i)^-?(?:bootlegs?|live|lives|other|others|singles?|compilations?|"
    r"comps?|collaborations?|collabs?|remix(?:es)?|demos?|unofficial|misc|"
    r"rarit(?:y|ies)|b-?sides?|extras?|mixes|promos?|eps?|"
    # Not the artist's own studio work either -- same rule as the grab-time
    # filter (_UNOFFICIAL_RE): skip these unless Lidarr actually wants a
    # matching album, or an album assembly needs songs out of them (a needed
    # song is rescued later by assembly_keep in process_torrent).
    r"antholog(?:y|ies)|collections?|box\s*sets?|greatest\s*hits|best\s*of|"
    r"essentials?|definitive)$"
)


# An ALBUM NAME that advertises a compilation / hits package / live or remix
# product rather than a studio album. _NOISE_DIR above only inspects CONTAINER
# segments (so a real album titled "Live at Leeds" is never caught by it), which
# left a folder literally named "... All Their Greatest Hits 2001" downloading
# happily inside a discography just because Lidarr had no record of it.
# Used only to flip the DEFAULT to deselect: if Lidarr genuinely tracks a
# matching album that is still missing, it is kept and downloaded as normal, and
# an album assembly can still rescue individual songs from it later.
_UNOFFICIAL_ALBUM = re.compile(
    r"(?i)(?:^|[\s\-_.\(\[])("
    r"greatest\s+hits|best\s+of|the\s+very\s+best|very\s+best\s+of|"
    r"antholog(?:y|ies)|compilations?|collections?|box\s*sets?|sampler|"
    r"essential|definitive|singles\s+collection|hits\s+collection|"
    r"remix(?:es|ed)?|megamix|dj[\s\-_.]*mix|mixtape|karaoke|tribute|"
    r"bootleg|rarities|b[\s\-_.]*sides|outtakes|unreleased|"
    # LIVE material. Deliberately anchored ("live at/in/from", "in concert",
    # "unplugged") rather than a bare "live", because plenty of STUDIO albums
    # simply contain the word -- "Live Through This", "Live and Let Die" -- and
    # a bare match would deselect those. Without any live pattern at all, a
    # discography torrent's concert albums were kept and downloaded whenever
    # Lidarr had no record of them: "Bill Withers / Live At Carnegie Hall
    # (not in library)" was kept while the four owned studio albums beside it
    # were correctly dropped. Lidarr excludes live records from its default
    # metadata profile, so "not in library" is the NORMAL state for them.
    r"live\s+(?:at|in|from|on)\b|in\s+concert|unplugged|live\s+albums?|"
    r"live\s+recordings?|live\s+bootleg"
    r")(?:$|[\s\-_.\)\]0-9])")


def _album_looks_unofficial(album: str) -> bool:
    """True when the album TITLE itself advertises non-studio material."""
    return bool(_UNOFFICIAL_ALBUM.search(album or ""))


def _is_noise_category(key: str) -> bool:
    """True if the album-folder key sits under a discography 'noise' category
    subfolder (Bootlegs/Live/Singles/Compilations/Remixes/...). Checks only the
    CONTAINER segments, not the album name itself, so a real album titled e.g.
    'Live at X' isn't caught."""
    parts = _rel_parts(key)
    return any(_NOISE_DIR.match(p) for p in parts[:-1])


# --- Video exclusion (backlog #4) ------------------------------------------
# Lidarr sometimes grabs a release that carries video (a concert DVD/BD ripped
# alongside the audio, a bundled music video, a DVD-Video zone). We never want
# to download video into the music library, so deselect it. IMPORTANT: a
# DVD-Audio disc's AUDIO_TS zone (and a whole-disc .iso) IS the audio -- keep
# it; only the VIDEO_TS / BDMV video zones and standalone video containers are
# dropped.
_VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".wmv", ".mov", ".flv", ".mpg", ".mpeg",
    ".mpe", ".m2ts", ".mts", ".ts", ".vob", ".divx", ".ogv", ".webm", ".3gp",
    ".rm", ".rmvb", ".asf",
}
# A DVD-Video / Blu-ray video zone path segment.
_VIDEO_DIR = re.compile(r"(?i)(?:^|/)(?:video_ts|bdmv)(?:/|$)")
# A DVD-Audio zone -- this is AUDIO, never deselected as "video".
_AUDIO_TS_DIR = re.compile(r"(?i)(?:^|/)audio_ts(?:/|$)")


def _blocklist_torrent(lidarr: LidarrClient, thash: str) -> None:
    """Blocklist the Lidarr queue row for this torrent (so Lidarr won't grab the
    same release again) -- used to BAN a video-only torrent. Best-effort."""
    if not (lidarr and thash):
        return
    try:
        for r in lidarr.queue_list():
            if str(r.get("downloadId") or "").lower() == thash.lower() and r.get("id") is not None:
                lidarr.queue_remove(r["id"], remove_from_client=False, blocklist=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("blocklist_torrent(%s) failed: %s", thash[:12], exc)


def _video_file_indices(files: List[Dict[str, Any]]) -> List[int]:
    """
    qBit file indices that are VIDEO (a VIDEO_TS/BDMV zone, or a standalone
    video container), EXCLUDING anything under a DVD-Audio AUDIO_TS zone (that's
    audio). Used to deselect video so a music grab never pulls it.
    """
    out: List[int] = []
    for f in files:
        low = str(f.get("name", "")).replace("\\", "/").lower()
        if _AUDIO_TS_DIR.search(low):
            continue  # DVD-Audio zone -> keep
        ext = os.path.splitext(low)[1]
        if _VIDEO_DIR.search(low) or ext in _VIDEO_EXTS:
            if "index" in f:
                out.append(int(f["index"]))
    return out


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
    # Drop bracketed/parenthesized tags FIRST. Edition/source/catalog tags like
    # "[DTSCD][UP]", "(1994)", "{KOC-5836}" often sit at the START of the name;
    # leaving them there blocks the ANCHORED (^) year/number/artist-prefix
    # strips below. That was the "[DTSCD][UP] Elton John - The Big Picture" bug:
    # the leading "[DTSCD][UP]" stopped the "Elton John - " artist prefix from
    # being stripped, so the album kept the artist words and wrongly word-subset
    # matched the self-titled album "Elton John".
    s = re.sub(r"[(\[\{][^)\]\}]*[)\]\}]", " ", name)   # drop (edition)[tag]{cat}
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = _LEAD_NUM.sub("", s)             # '01. ' / '01 - '
    s = _LEAD_YEARS.sub("", s)           # '1960 ' / '1960,1961 ' / '1960 - '
    m = _ARTIST_YEAR_ALBUM.match(s)      # 'Artist - 2005 - Gospel Train' -> 'Gospel Train'
    if m:
        s = m.group(1)
    if artist:
        na = re.escape(artist.strip())
        # 'Etta James - ...' or 'Etta James & Eddie ... - ...' at the start.
        s = re.sub(rf"(?i)^\s*{na}\b[^-]*?-\s*", "", s, count=1)
    return re.sub(r"\s{2,}", " ", s).strip(" -_.")


def _clean_artist(name: str) -> str:
    s = re.sub(r"[\(\[\{][^)\]\}]*[\)\]\}]", " ", name)
    s = re.sub(r"@\S+", " ", s)  # bitrate/quality tags: @320, @192, @VBR
    s = re.sub(r"(?i)\b(discography|complete|collection|studio albums?|flac|mp3|kbps|vbr)\b", " ", s)
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
    while len(p) > 1 and (_DISC_DIR.match(p[-1]) or _ART_DIR.match(p[-1])
                          or _BONUS_DIR.match(p[-1])):
        p.pop()
    return p


def _file_album_key(f: Dict[str, Any], torrent_name: str) -> str:
    """The album-folder key a file belongs to (audio OR sidecar)."""
    parts = _rel_parts(f.get("name", ""))
    folder_parts = parts[:-1] if len(parts) >= 2 else []
    adir = _album_dir_parts(folder_parts)
    return "/".join(adir) if adir else torrent_name


def _release_big_enough(
    lidarr: LidarrClient, album: Optional[Dict[str, Any]], want: int,
    _cache: Optional[Dict[int, int]] = None,
) -> int:
    """
    The track count of the LARGEST release Lidarr knows for this album, if it
    can hold `want` tracks -- else 0.

    This answers "does an edition with these bonus tracks actually exist?".
    Lidarr's release list comes straight from MusicBrainz, so a US bonus-track
    edition that MusicBrainz has appears here and a tracker-only assembly does
    not. Read-only: it decides whether to KEEP the download, never switches
    anything.
    """
    if not album or want <= 0:
        return 0
    try:
        album_id = int(album.get("id") or 0)
    except (TypeError, ValueError):
        return 0
    if not album_id:
        return 0
    if _cache is not None and album_id in _cache:
        biggest = _cache[album_id]
    else:
        biggest = 0
        try:
            for r in lidarr.iter_album_releases(album_id) or []:
                biggest = max(biggest, int(r.get("trackCount") or 0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("release lookup failed for album %s: %s", album_id, exc)
            return 0
        if _cache is not None:
            _cache[album_id] = biggest
    return biggest if biggest >= want else 0


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
    rel_cache: Dict[int, int] = {}
    plan: List[Dict[str, Any]] = []
    for key, afiles in sorted(groups.items()):
        parts = _rel_parts(key)
        raw_artist = parts[0] if len(parts) >= 2 else torrent_name
        artist = forced_artist or _clean_artist(raw_artist)
        album_raw = parts[-1] if parts else torrent_name
        # Strip using the CLEANED artist ("50 Cent"), not the raw folder/torrent
        # name ("50 Cent - Before I Self Destruct (2009) [FLAC]") -- otherwise a
        # single-album torrent keeps its "Artist - " prefix and never matches.
        album = _clean_album(album_raw, artist=artist)
        matched: Dict[str, Any] = {}
        complete, have, total = album_complete_in_library(
            lidarr, artist, album, _cache=lib_cache, llm=llm, out=matched
        )
        if _is_noise_category(key) or _album_looks_unofficial(album):
            # Noise-category folder (bootlegs/live/singles/...): deselect by
            # default. Keep ONLY if Lidarr explicitly tracks a matching album
            # (total > 0) that is still missing (not complete) -- i.e. Lidarr
            # actually wants this exact release.
            deselect = not (total > 0 and not complete)
        else:
            # Deselect only when the library fully has it AND has >= as many
            # tracks as the torrent folder (don't drop a bigger edition).
            deselect = bool(complete and total >= len(afiles))
            if complete and total < len(afiles):
                # Owned, but the tracker folder is BIGGER than the release
                # Lidarr monitors -- a deluxe/bonus edition. Keeping the whole
                # folder means re-downloading every track you already have to
                # gain the extras, which is what made Priscilla Ahn's "This Is
                # Where We Are" (13/13 owned, 16 files on the tracker: 13 +
                # 3 US bonus) sit selected for download.
                #
                # So keep it only when the extras have somewhere to LAND: a
                # release of this same album, known to Lidarr/MusicBrainz, big
                # enough to hold them. The import path switches to it later
                # (_align_release_to_disk, exact count + immediate import) --
                # re-pointing a populated album HERE would unmap the files you
                # already have, which is what cost Let It Be 27 tracks.
                # No such release exists -> the extras can never be tracked,
                # so the folder is redundant and gets dropped.
                room = _release_big_enough(
                    lidarr, matched.get("album"), len(afiles), _cache=rel_cache)
                deselect = not room
                logger.info(
                    "  edition check: %s / %s -- library release holds %d, "
                    "tracker folder has %d -> %s", artist, album, total,
                    len(afiles),
                    "keeping (a %d-track release exists to hold the extras)"
                    % room if room else
                    "deselecting (no release of this album can hold the extras)")
        allf = all_by_key.get(key, afiles)
        plan.append({
            "artist": artist, "album": album,
            "files": afiles,       # audio only (drives the have-decision)
            "all_files": allf,     # every file in the folder (what we deselect)
            "size": sum(int(x.get("size") or 0) for x in allf),
            "have": deselect, "have_count": have, "total": total,
        })
    return plan


def assembly_keep_tails(needed_paths: Iterable[str]) -> set:
    """
    Turn absolute source paths the assembly planner needs into match keys for a
    qBittorrent file list. qBit reports names RELATIVE to the torrent root
    ("Box Set/CD2/05 - Song.mp3") while the planner holds absolute paths, so we
    compare the last two segments ("cd2/05 - song.mp3") -- specific enough to
    avoid the basename collisions that plague compilations ("01 - Track.mp3"
    exists in hundreds of folders).
    """
    out: set = set()
    for p in needed_paths or []:
        parts = [s for s in str(p).replace("\\", "/").split("/") if s]
        if len(parts) >= 2:
            out.add("/".join(parts[-2:]).lower())
        elif parts:
            out.add(parts[-1].lower())
    return out


def _needed_for_assembly(name: str, keep: set) -> bool:
    """Is this torrent file one an album assembly needs?"""
    if not keep:
        return False
    parts = [s for s in str(name or "").replace("\\", "/").split("/") if s]
    if len(parts) >= 2 and "/".join(parts[-2:]).lower() in keep:
        return True
    return bool(parts) and parts[-1].lower() in keep


def process_torrent(
    qbt: QbtClient, lidarr: LidarrClient, torrent: Dict[str, Any],
    forced_artist: str = "", apply: bool = False,
    emit: Callable[[str], None] = logger.info,
    files: Optional[List[Dict[str, Any]]] = None,
    llm=None, deselect_video: bool = True, reap_useless: bool = False,
    assembly_keep: Optional[set] = None,
) -> tuple:
    """Plan + (optionally) deselect one torrent. Returns (deselected, kept)."""
    thash = torrent.get("hash")
    tname = torrent.get("name") or "?"
    if files is None:
        files = qbt.files(thash)
    # Video exclusion (#4) is independent of the album plan: even a torrent with
    # no owned albums (or no audio-album grouping) should drop its video files.
    video_idx = _video_file_indices(files) if deselect_video else []
    plan = plan_torrent(lidarr, tname, files, forced_artist, llm=llm)
    if not plan and not video_idx:
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
                 f"(library {a['have_count']}/{a['total']}) -> deselect {n_all} file(s){extra} [whole folder]"
                 + (" [not an official studio album]"
                    if (a["total"] == 0 and _album_looks_unofficial(a["album"]))
                    else ""))
        else:
            kept += 1
            why = "not in library" if a["total"] == 0 else f"library {a['have_count']}/{a['total']}"
            emit(f"  KEEP  [{human(a['size']):>9}]  {a['artist']} / {a['album']} ({why})")
    # Merge in video files (VIDEO_TS/BDMV/containers, AUDIO_TS kept) -- always
    # dropped for a music torrent, on top of any already-have album folders.
    if video_idx:
        already = set(to_deselect)
        fresh = [i for i in video_idx if i not in already]
        if fresh:
            to_deselect.extend(fresh)
            emit(f"  VIDEO -> deselect {len(fresh)} video file(s) "
                 f"(VIDEO_TS/BDMV/containers; AUDIO_TS + .iso kept)")
    # Idempotent apply: only touch files NOT already deselected, so a periodic
    # re-check (see auto_deselect_pass recheck) is quiet and issues no needless
    # priority churn on a torrent it already handled. `cur` is every file's
    # current priority; `desel_all` is what WILL be deselected (already-0 plus
    # this pass's picks) and drives the "nothing wanted left" reap decision.
    cur = {int(f["index"]): int(f.get("priority", 1))
           for f in files if "index" in f}
    # ASSEMBLY PROTECTION (requirement e): this torrent may be a compilation
    # whose albums are all "owned"/unknown -- so everything above wants to be
    # deselected -- while individual SONGS in it are exactly what a missing
    # album needs. Keep those files selected (and out of the reap decision).
    # One compilation can feed several assemblies, so `assembly_keep` is the
    # union across all plans.
    if assembly_keep:
        by_index = {int(f["index"]): str(f.get("name") or "")
                    for f in files if "index" in f}
        rescued = [i for i in to_deselect
                   if _needed_for_assembly(by_index.get(i, ""), assembly_keep)]
        if rescued:
            keepset = set(rescued)
            to_deselect = [i for i in to_deselect if i not in keepset]
            emit(f"  ASSEMBLY -> keeping {len(rescued)} song(s) needed to "
                 f"assemble a missing album")
    # ASSEMBLY-ONLY NARROWING: when a torrent is here to supply songs for an
    # album assembly, download ONLY what is actually needed. Everything else in
    # it (the rest of a 3-CD compilation) is dropped -- unless it belongs to an
    # album Lidarr genuinely still wants, which is never sacrificed. Applies only
    # when the torrent really does hold assembly-needed songs, so ordinary
    # torrents behave exactly as before.
    if assembly_keep:
        by_index2 = {int(f["index"]): str(f.get("name") or "")
                     for f in files if "index" in f}
        needed_idx = {i for i, nm in by_index2.items()
                      if _needed_for_assembly(nm, assembly_keep)}
        if needed_idx:
            wanted_idx = set()
            for a in plan:
                total = int(a.get("total") or 0)
                if total > 0 and not a.get("have"):
                    for x in (a.get("files") or []):
                        if "index" in x:
                            wanted_idx.add(int(x["index"]))
            drop = [i for i, nm in by_index2.items()
                    if i not in needed_idx and i not in wanted_idx
                    and os.path.splitext(nm)[1].lower() in AUDIO_EXTS
                    and cur.get(i, 1) != 0]
            if drop:
                already_d = set(to_deselect)
                fresh = [i for i in drop if i not in already_d]
                if fresh:
                    to_deselect.extend(fresh)
                    emit(f"  ASSEMBLY-ONLY -> keeping {len(needed_idx)} needed "
                         f"song(s) (+{len(wanted_idx)} for wanted albums), "
                         f"deselecting {len(fresh)} other track(s)")
    desel_all = {i for i, p in cur.items() if p == 0} | set(to_deselect)
    to_apply = sorted(i for i in to_deselect if cur.get(i, 1) != 0)
    if apply and to_apply:
        ok = qbt.set_file_priority(thash, to_apply, 0)
        emit(f"  -> {'deselected' if ok else 'FAILED'} {len(to_apply)} file(s)")
    # Reap a torrent that has nothing we want left: either every music file is
    # deselected (all albums already owned -> only garbage remains) or the
    # torrent carries NO music at all but has video (a video-only grab). The
    # video-only case is also BLOCKLISTED so Lidarr never re-grabs it.
    if reap_useless and apply:
        desel = desel_all
        audio_all = [f for f in files
                     if Path(f.get("name", "")).suffix.lower() in AUDIO_EXTS]
        audio_kept = [f for f in audio_all
                      if int(f.get("index", -1)) not in desel]
        if audio_all and not audio_kept:
            if qbt.remove(thash, delete_files=True):
                emit("  -> REMOVED torrent (all wanted music already in library)")
        elif (not audio_all) and video_idx:
            _blocklist_torrent(lidarr, thash)
            if qbt.remove(thash, delete_files=True):
                emit("  -> REMOVED + BLOCKLISTED video-only torrent (banned)")
    return deselected, kept


def auto_deselect_pass(
    qbt: QbtClient, lidarr: LidarrClient, seen: set,
    category: str = "", emit: Callable[[str], None] = logger.info,
    pause_during_scan: bool = True, llm=None, deselect_video: bool = True,
    reap_useless: bool = True,
    planned: Optional[Dict[str, float]] = None, recheck_seconds: int = 0,
    now: Optional[float] = None, assembly_keep: Optional[set] = None,
    on_progress: Optional[Callable[[], None]] = None,
    progress_every: int = 10,
    start_added_stopped: bool = True,
) -> int:
    """
    One scheduled pass for the pipeline: for each INCOMPLETE music torrent,
    deselect already-have albums. Returns number of torrents acted on.

    To keep bandwidth from leaking on already-owned albums before we act, a
    FRESHLY-seen torrent is PAUSED the instant we notice it, its file list is
    read, the owned albums are deselected, and only then is its ORIGINAL
    start-state restored. We never override what Lidarr/you set: a force-started
    torrent comes back force-started, a normal one comes back normally started,
    and a "don't start" (stopped/paused) torrent is left stopped and never
    paused in the first place. A torrent whose metadata hasn't resolved yet
    (magnet, empty file list) is left as-is and retried next pass.

    RE-CHECK (fixes owned-later leaks): an album a torrent carries can become
    owned AFTER the torrent was first planned -- Lidarr imports it, the reconcile
    pass fills it, another torrent completes it. With a permanent "seen" skip
    those newly-owned albums would keep downloading forever. So when `planned`
    (a persistent {hash: last_plan_ts}) and a positive `recheck_seconds` are
    given, a still-incomplete torrent is RE-PLANNED once that interval elapses
    and any newly-owned albums are deselected too. Re-checks don't pause (the
    apply is idempotent and only touches not-yet-deselected files), so a
    long-running download isn't disturbed. Without `planned` the old
    plan-once/`seen` behaviour is unchanged.
    """
    if now is None:
        now = time.time()
    acted = 0
    progressed = 0
    # NEWEST FIRST. One pass can walk hundreds of torrents, each costing Lidarr
    # (and sometimes LLM) calls, so a torrent that has only just been grabbed
    # could wait many minutes to be narrowed -- by which time a fast swarm has
    # already pulled the whole thing. Handling the newest arrivals first keeps
    # the deselect ahead of the download.
    _tors = list(qbt.torrents(category=category))
    try:
        _tors.sort(key=lambda x: float(x.get("added_on") or 0), reverse=True)
    except Exception:  # noqa: BLE001
        pass
    for t in _tors:
        h = t.get("hash")
        if not h:
            continue
        # Only touch torrents that are still downloading (progress < 1.0);
        # nothing to gain deselecting a finished one -- mark it permanently.
        if float(t.get("progress") or 0) >= 1.0:
            seen.add(h)
            if planned is not None:
                planned.pop(h, None)
            continue
        first_sight = h not in seen
        if not first_sight:
            # Already handled once. Re-plan only if the recheck window is
            # enabled and has elapsed; otherwise skip (cheap steady state).
            if planned is None or recheck_seconds <= 0:
                continue
            if (now - planned.get(h, 0.0)) < recheck_seconds:
                continue
        # Capture the torrent's original start-state so we restore EXACTLY what
        # Lidarr/you set -- never impose one.
        state = (t.get("state") or "").lower()
        was_forced = bool(t.get("force_start")) or state.startswith("forced")
        was_stopped = ("paused" in state) or ("stopped" in state)
        paused_by_us = False
        try:
            # Pause only on FIRST sight of a running torrent (to stop owned
            # albums downloading while we first decide). A re-check doesn't
            # pause -- the idempotent apply only flips not-yet-deselected files.
            if pause_during_scan and not was_stopped and first_sight:
                qbt.pause(h)
                paused_by_us = True
            files = qbt.files(h)
            if not files:
                # Metadata not ready (e.g. magnet still resolving). Can't plan
                # without the file list -- retry next pass, don't mark seen.
                # (The finally-block restores the original state meanwhile.)
                continue
            d, _k = process_torrent(
                qbt, lidarr, t, apply=True, emit=emit, files=files, llm=llm,
                deselect_video=deselect_video, reap_useless=reap_useless,
                assembly_keep=assembly_keep,
            )
            seen.add(h)
            if planned is not None:
                planned[h] = now
            if d:
                acted += 1
            # Checkpoint the caller's ledger DURING the walk. A full pass over a
            # few hundred torrents takes the better part of an hour (each one
            # costs Lidarr queries and sometimes an LLM call), so saving only at
            # the end meant a restart anywhere in that hour threw away the whole
            # pass's progress -- which is exactly the re-planning this ledger
            # exists to prevent.
            if on_progress is not None:
                progressed = progressed + 1
                if progressed % max(1, int(progress_every)) == 0:
                    try:
                        on_progress()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("deselect progress callback: %s", exc)
        finally:
            # Restore the original intent -- only if WE paused it (a re-check
            # never pauses, so it never touches start-state).
            if paused_by_us:
                # We paused it only to narrow the file selection -- verify it
                # actually starts again, otherwise the torrent sits at 0% with
                # nothing in the log to say why.
                if was_forced:
                    ok_start = qbt.ensure_started(h)   # preserve force-start
                else:
                    ok_start = qbt.ensure_resumed(h)   # normal start
                if not ok_start:
                    emit(f"  WARNING: narrowed {str(t.get('name'))[:60]!r} but "
                         f"qBittorrent did not restart it -- it is still paused")
            elif (start_added_stopped and was_stopped and first_sight
                  and float(t.get("progress") or 0) <= 0.0
                  and not float(t.get("completion_on") or 0) > 0
                  and QbtClient.SELF_ADDED_TAG not in
                      {x.strip() for x in
                       str(t.get("tags") or "").split(",")}):
                # A FRESH grab that arrived already stopped. This is what makes
                # Lidarr's "Initial State = Stopped" usable: Lidarr adds the
                # torrent stopped so NOTHING downloads before the file selection
                # is applied, and we start it here now that it has been narrowed.
                # Without this the torrent would sit at 0% forever, because the
                # branch above only restarts what this pass paused itself.
                #
                # Deliberately narrow: only on first sight, only at 0% with no
                # completion timestamp. A torrent YOU stopped by hand has either
                # progress or a completion time (or has been seen before), so it
                # is never resumed against your wishes.
                #
                # And NEVER one the pipeline added itself (SELF_ADDED_TAG): the
                # song/compilation hunt adds its grabs stopped ON PURPOSE and
                # starts them only after narrowing to the one or two songs it
                # wants. Starting those here would pull a whole 3-CD box and
                # defeat the hunt.
                if qbt.ensure_resumed(h):
                    emit(f"  started {str(t.get('name'))[:60]!r} -- it was added "
                         f"stopped and is now narrowed")
                else:
                    emit(f"  WARNING: narrowed {str(t.get('name'))[:60]!r} but "
                         f"qBittorrent would not start it")
    return acted


# Torrent states that mean "present but NOT productively downloading" -- the
# candidates for dead-grab reaping when stuck near 0% past the grace period.
# Deliberately excludes actively-downloading states and paused/stopped (a
# stopped torrent is a deliberate "don't download", never reaped).
_DEAD_STATES = frozenset({
    "metadl", "forcedmetadl", "stalleddl", "stalledup", "forcedup",
    "queueddl", "missingfiles", "error", "unknown",
})

# States that claim to be ACTIVELY DOWNLOADING. A torrent here is dead only when
# its swarm is provably empty, so they need the extra seeder test below rather
# than a place in _DEAD_STATES.
#
# They must be considered at all because THE PIPELINE'S OWN force-start moves
# torrents out of every state above: `ensure_started` flips a grab to forcedDL,
# and a force-started torrent with no peers then sits at 0% in forcedDL
# permanently -- a state the reaper did not recognise, so it was immortal.
# Measured: 34 of 254 torrents were at 0 seeders and 0% in forcedDL, some 6-9
# DAYS old, surviving a 6-hour grace, while the forcedMetaDL ones beside them
# were reaped correctly.
_DOWNLOADING_STATES = frozenset({"downloading", "forceddl", "dl"})


def dead_grab_reaper_pass(
    qbt: QbtClient, lidarr: Optional[LidarrClient], category: str = "",
    grace_seconds: int = 172800, blocklist: bool = True,
    emit: Callable[[str], None] = logger.info, now: Optional[float] = None,
) -> int:
    """
    Remove lidarr-category torrents that were grabbed but never got anywhere:
    progress ~0 in a dead/stalled/metadata state for longer than `grace_seconds`
    (default 2 days, measured from the torrent's add time). Such a torrent is a
    dead release (no seeders / no metadata) clogging qBit + Lidarr's queue.

    Each is removed from qBit (with its stub data) and, when a matching Lidarr
    queue row exists, that row is removed and BLOCKLISTED (so Lidarr re-searches
    and grabs a live alternative -- lossless preferred). Paused/stopped torrents
    and any that made real progress are left alone. Returns the count removed.
    """
    if now is None:
        now = time.time()
    q_by_hash: Dict[str, list] = {}
    try:
        for r in (lidarr.queue_list() if lidarr else []):
            dlid = str(r.get("downloadId") or "").lower()
            if dlid:
                q_by_hash.setdefault(dlid, []).append(r)
    except Exception as exc:  # noqa: BLE001
        emit(f"dead-grab reaper: Lidarr queue fetch failed ({exc}); "
             f"removing from qBit only")
    removed = 0
    for t in qbt.torrents(category=category):
        h = (t.get("hash") or "")
        if not h:
            continue
        state = (t.get("state") or "").lower()
        prog = float(t.get("progress") or 0.0)
        added = float(t.get("added_on") or 0)
        if prog >= 0.01:
            continue                       # making real progress -> leave alone
        if state not in _DEAD_STATES:
            # A force-started grab with an empty swarm looks "downloading"
            # forever. Both seeder figures must be zero: `num_seeds` is what we
            # are connected to, `num_complete` is what the tracker reports
            # exists, so requiring both avoids killing a torrent whose peers we
            # simply have not reached yet.
            if not (state in _DOWNLOADING_STATES
                    and int(t.get("num_seeds") or 0) == 0
                    and int(t.get("num_complete") or 0) <= 0):
                continue
        age = now - added if added else 0.0
        if age < grace_seconds:
            continue  # still within the grace window -> wait it out
        name = t.get("name") or h[:12]
        for r in q_by_hash.get(h.lower(), []):
            qid = r.get("id")
            if qid is not None:
                lidarr.queue_remove(
                    qid, remove_from_client=False, blocklist=blocklist)
        if qbt.remove(h, delete_files=True):
            removed += 1
            emit(f"dead-grab reaper: removed {name!r} (0% for {age/86400:.1f}d, "
                 f"state={state}{'; blocklisted' if blocklist else ''})")
    if removed:
        emit(f"dead-grab reaper: removed {removed} dead grab(s)")
    return removed


def adopt_uncategorised(
    qbt: QbtClient, category: str,
    emit: Callable[[str], None] = logger.info, max_adopt: int = 50,
) -> int:
    """
    Give the managed category to music torrents that have NO category.

    Everything in this module filters by `category` (Lidarr sets it on its own
    grabs), so a torrent added by hand -- or by anything that forgets the
    category -- is invisible to the pipeline: no deselect, no lifecycle, no
    reaping. It just downloads in full. A real case: "Beck - Odelay (2016)
    [FLAC 24-88]" sitting at 27% with an empty category while 263 other torrents
    were managed.

    Only torrents whose file list is predominantly AUDIO are adopted, so a
    tv-sonarr-style download (or anything else sharing the client) is never
    hijacked. Returns the number adopted.
    """
    if not category:
        return 0
    adopted = 0
    for t in qbt.torrents():                      # no filter: we want the gaps
        if adopted >= max_adopt:
            break
        if (t.get("category") or "").strip():
            continue
        h = t.get("hash")
        if not h:
            continue
        try:
            files = qbt.files(h)
        except Exception:  # noqa: BLE001
            continue
        if not files:
            continue                              # metadata not in yet -- later
        audio = sum(1 for f in files
                    if os.path.splitext(str(f.get("name") or ""))[1].lower()
                    in AUDIO_EXTS)
        if audio == 0 or audio * 2 < len(files):
            continue                              # not a music download
        if qbt.set_category(h, category):
            adopted += 1
            emit(f"adopted uncategorised music torrent into '{category}': "
                 f"{(t.get('name') or h[:12])!r} ({audio}/{len(files)} audio "
                 f"files) -- it can now be deselected and managed")
    if adopted:
        emit(f"adopted {adopted} uncategorised music torrent(s)")
    return adopted


def stalled_grab_reaper_pass(
    qbt: QbtClient, lidarr: Optional[LidarrClient], category: str = "",
    grace_seconds: int = 259200, blocklist: bool = True,
    emit: Callable[[str], None] = logger.info, now: Optional[float] = None,
    assembly_keep: Optional[set] = None, llm=None,
) -> Tuple[int, int]:
    """
    Reap a torrent that IS partly downloaded but has not progressed in
    `grace_seconds` (default 3 days, from qBittorrent's own `last_activity`).

    SALVAGE FIRST -- never throw away usable music. A stalled torrent often has
    whole songs finished (one real case here had 30 of 46 files complete), and
    those may be exactly what a monitored album or an album assembly needs. So:

      * something salvageable -> remove the TORRENT ONLY and KEEP the data on
        disk, so the normal sweep/reconcile/assembly can import those songs. The
        stalled download stops wasting slots, the music survives.
      * nothing salvageable -> remove the torrent AND its data, and blocklist the
        release so Lidarr looks for a different one.

    A file counts as salvageable when it is COMPLETE (per-file progress 1.0),
    is audio, and either an assembly needs it or it belongs to an album Lidarr
    still wants. Torrents at 0% are left to dead_grab_reaper_pass; paused /
    stopped torrents are never touched (a deliberate "don't download").
    Returns (salvaged, trashed).
    """
    if now is None:
        now = time.time()
    salvaged = trashed = 0
    for t in qbt.torrents(category=category):
        h = t.get("hash") or ""
        state = (t.get("state") or "").lower()
        prog = float(t.get("progress") or 0.0)
        if not h or prog >= 1.0:
            continue
        if "paused" in state or "stopped" in state:
            continue                       # deliberate: leave it alone
        if prog < 0.01:
            continue                       # 0% -> dead_grab_reaper's job
        last = float(t.get("last_activity") or 0)
        if not last or (now - last) < grace_seconds:
            continue
        name = t.get("name") or h[:12]
        idle_days = (now - last) / 86400.0
        try:
            files = qbt.files(h)
        except Exception as exc:  # noqa: BLE001
            emit(f"stalled reaper: cannot read files of {name!r}: {exc}")
            continue
        done_audio = [
            f for f in files
            if float(f.get("progress") or 0) >= 1.0
            and os.path.splitext(str(f.get("name") or ""))[1].lower() in AUDIO_EXTS
        ]
        # (a) does an album assembly need any finished song?
        wanted = [f for f in done_audio
                  if _needed_for_assembly(str(f.get("name") or ""), assembly_keep or set())]
        why = "an album assembly needs them" if wanted else ""
        # (b) otherwise, is any finished song part of an album Lidarr still wants?
        if not wanted and lidarr is not None and done_audio:
            try:
                plan = plan_torrent(lidarr, name, files, llm=llm)
            except Exception:  # noqa: BLE001
                plan = []
            keep_keys = set()
            for a in plan:
                total = int(a.get("total") or 0)
                if total > 0 and not a.get("have"):
                    for x in a.get("files") or []:
                        keep_keys.add(str(x.get("name") or ""))
            if keep_keys:
                wanted = [f for f in done_audio
                          if str(f.get("name") or "") in keep_keys]
                if wanted:
                    why = "Lidarr still wants their album"
        if wanted:
            # Keep the DATA, drop the stalled torrent. The pipeline imports from
            # the download folder, so the finished songs stay usable.
            if qbt.remove(h, delete_files=False):
                salvaged += 1
                emit(f"stalled reaper: SALVAGED {len(wanted)} finished song(s) "
                     f"from {name!r} (idle {idle_days:.1f}d, {prog*100:.0f}%) -- "
                     f"{why}; torrent removed, files LEFT on disk for import")
            continue
        blocklisted_any = False
        for r in (lidarr.queue_list() if (lidarr and blocklist) else []):
            if str(r.get("downloadId") or "").lower() == h.lower() \
                    and r.get("id") is not None:
                try:
                    if lidarr.queue_remove(int(r["id"]),
                                           remove_from_client=False,
                                           blocklist=True):
                        blocklisted_any = True
                except Exception as exc:  # noqa: BLE001
                    emit(f"  WARNING: blocklist failed for {r.get('id')}: {exc}")
        if qbt.remove(h, delete_files=True):
            trashed += 1
            emit(f"stalled reaper: TRASHED {name!r} (idle {idle_days:.1f}d at "
                 f"{prog*100:.0f}%, nothing salvageable"
                 f"{'; blocklisted' if blocklisted_any else ''})")
            if blocklist and not blocklisted_any:
                emit(f"  note: no Lidarr queue row accepted the blocklist for "
                     f"{name[:60]!r} -- the same release could come back")
    if salvaged or trashed:
        emit(f"stalled reaper: {salvaged} salvaged, {trashed} trashed")
    return salvaged, trashed


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
    # A single-file torrent's content_path IS the file itself, not a folder --
    # os.walk() on a file yields nothing, which previously read as "0 audio =
    # fully imported" and got the torrent (and its data) deleted. Handle it.
    if os.path.isfile(path):
        return 1 if os.path.splitext(path)[1].lower() in AUDIO_EXTS else 0
    n = 0
    for _dp, _dn, fn in os.walk(path):
        for x in fn:
            if os.path.splitext(x)[1].lower() in AUDIO_EXTS:
                n += 1
    return n


def _folder_newest_mtime(path: str) -> float:
    """Newest mtime under `path` (the folder itself if empty). 0.0 on error."""
    newest = 0.0
    try:
        newest = os.path.getmtime(path)
        for root, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
                except OSError:
                    continue
    except OSError:
        return 0.0
    return newest


def torrent_lifecycle_pass(
    qbt: QbtClient, download_root: str, category: str = "",
    emit: Callable[[str], None] = logger.info,
    lidarr: Optional[LidarrClient] = None,
    llm=None,
    remove_when_library_complete: bool = True,
    min_stable_seconds: int = 300,
    now: Optional[float] = None,
    on_complete: Optional[Callable[[str], None]] = None,
    completed_seen: Optional[set] = None,
    wanted_only: bool = True,
    checked: Optional[dict] = None,
    recheck_seconds: int = 21600,
) -> tuple:
    """
    Manage COMPLETED music torrents by how much of their content the pipeline
    has already moved into the library (i.e. deleted from the download folder):

      * fully moved      (no audio left on disk)  -> REMOVE torrent + data
      * partially moved  (some audio gone)        -> PAUSE (stop seeding while
                                                      the rest finishes importing)
      * nothing moved yet (all audio still there) -> see below

    For the "nothing moved yet" case, Lidarr may have imported the torrent
    ITSELF (its default copy/hardlink import leaves the files in the download
    folder, so the pipeline never sees them vanish). When `remove_when_library_
    complete` is on and a `lidarr` client is supplied, ask Lidarr whether it
    already fully owns EVERY album the torrent contains (plan_torrent +
    album_complete_in_library); if so, REMOVE torrent + data (backlog #5). A
    stability guard (`min_stable_seconds`) avoids racing an in-progress import.
    Otherwise the torrent is left running.

    Non-music torrents (no selected audio -- TV, movies) are ignored.
    Reads the real download folder, so it never deletes a torrent whose files
    haven't actually been imported. Returns (removed, paused).
    """
    if now is None:
        now = time.time()
    # Guard: if the download root isn't visible, do NOTHING -- otherwise an
    # unmounted path would look like "everything moved" and nuke torrents.
    if not download_root or not os.path.isdir(download_root):
        emit(f"lifecycle: download root {download_root!r} not visible -- skipping")
        return 0, 0
    # ...and isdir() is not enough: Docker CREATES a missing bind-mount source,
    # so a mistyped host path arrives as an existing, empty directory. Every
    # completed torrent then maps to a folder that does not exist, which reads
    # as "already imported" and costs the user their torrents. An empty root
    # has nothing to import either way, so refusing to act loses nothing.
    try:
        root_empty = not any(os.scandir(download_root))
    except OSError as exc:
        emit(f"lifecycle: cannot read download root {download_root!r}: {exc}"
             f" -- skipping")
        return 0, 0
    if root_empty:
        emit(f"lifecycle: download root {download_root!r} is EMPTY -- refusing "
             f"to treat completed torrents as imported (is the mount correct?)")
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
        folder_exists = os.path.exists(folder)
        on_disk = _count_audio_on_disk(folder) if folder_exists else 0
        state = (t.get("state") or "").lower()
        name = t.get("name") or "?"

        # #8a: a music torrent has just completed and still has audio on disk
        # -> kick the pipeline to start processing NOW (enqueue its .cue), once
        # per torrent. Fires before the remove/pause logic below.
        if (on_complete is not None and folder_exists and on_disk > 0
                and completed_seen is not None and h not in completed_seen):
            completed_seen.add(h)
            try:
                on_complete(folder)
            except Exception as exc:  # noqa: BLE001
                emit(f"lifecycle: on_complete failed for {name!r}: {exc}")

        if folder_exists and on_disk == 0:
            # We can SEE this torrent's folder and its audio is gone -> the
            # pipeline moved every track into the library. Safe to remove the
            # torrent plus any leftover data (extras / the now-empty folder),
            # because we deleted files at a path we actually verified.
            if qbt.remove(h, delete_files=True):
                removed += 1
                emit(f"lifecycle: REMOVED (fully imported) {name!r}")
        elif not folder_exists:
            # The torrent's content can't be located under download_root.
            # Either (a) it was imported and the source folder was already
            # deleted, or (b) this torrent is stored on a DIFFERENT save
            # path/share the pipeline never sees. We must not guess "imported"
            # and delete_files here: qBittorrent deletes at the torrent's REAL
            # content_path regardless of our mapped view, so in case (b) that
            # would erase UN-IMPORTED data. In case (a) the files are already
            # gone, so dropping just the dead torrent entry (delete_files=False)
            # is correct and loses nothing. Either way, never delete data we
            # could not confirm.
            if qbt.remove(h, delete_files=False):
                removed += 1
                emit(
                    f"lifecycle: removed torrent entry only (content not visible "
                    f"under {download_root} -- imported+cleaned, or stored "
                    f"elsewhere; data left untouched) {name!r}"
                )
        else:
            # Audio still on disk (partly moved, or Lidarr self-imported in
            # place). Decide from the library plan. With wanted_only (default),
            # remove the torrent + data once every album Lidarr WANTS from it is
            # owned -- treating compilations / live / box-exclusive / albums
            # Lidarr doesn't know (total==0) as "not needed" leftovers that go
            # with the torrent. An album Lidarr KNOWS but we don't fully own yet
            # (total>0 and not have) still blocks removal, so un-imported wanted
            # content is never deleted (it stays for the WebUI to resolve).
            reaped = False
            # Re-check throttle: a completed torrent whose DISK STATE hasn't
            # changed since the last library check is not re-planned until
            # `recheck_seconds` pass. Without this, every pass re-ran the
            # full Lidarr+LLM plan for every stuck compilation ("NOT removing
            # ..." cycling in the log every few minutes, forever). Any change
            # in the folder (an import moved files out) re-checks immediately.
            newest_mtime = _folder_newest_mtime(folder)
            if checked is not None:
                sig = (on_disk, int(newest_mtime))
                prev = checked.get(h)
                if (prev and prev[0] == sig
                        and now - prev[1] < max(60, recheck_seconds)):
                    continue
            if (remove_when_library_complete and lidarr is not None
                    and now - newest_mtime >= max(0, min_stable_seconds)):
                try:
                    plan = plan_torrent(lidarr, name, files, llm=llm)
                except Exception as exc:  # noqa: BLE001
                    plan = None
                    emit(f"lifecycle: library check failed for {name!r}: {exc}")
                if plan and checked is not None:
                    # Only a SUCCESSFUL plan is throttled -- a Lidarr hiccup
                    # keeps retrying every pass as before.
                    checked[h] = ((on_disk, int(newest_mtime)), now)
                if plan:
                    def _blocks(a):
                        return int(a.get("total") or 0) > 0 and not a.get("have")
                    ok = (not any(_blocks(a) for a in plan) if wanted_only
                          else all(a.get("have") for a in plan))
                    owned = sum(1 for a in plan if a.get("have"))
                    # DATA-LOSS GUARD: never delete a torrent's data when NOT ONE
                    # of its albums is confirmed present in the library. An
                    # album Lidarr can't resolve comes back total==0 (looks like
                    # a "leftover"), and a Lidarr timeout makes EVERY album look
                    # that way -- which previously deleted a fully-downloaded,
                    # not-yet-imported album (e.g. Eminem "Infinite" during a
                    # Lidarr overload). If nothing is owned, there is nothing to
                    # clean up after: keep the data and retry next pass.
                    if ok and owned < 1:
                        emit(
                            f"lifecycle: NOT removing {name!r} -- no album "
                            f"confirmed in library (still importing, or Lidarr "
                            f"unreachable); keeping data for a later pass"
                        )
                        ok = False
                    if ok:
                        leftover = len(plan) - owned
                        if qbt.remove(h, delete_files=True):
                            removed += 1
                            reaped = True
                            emit(
                                f"lifecycle: REMOVED (all wanted albums in library"
                                + (f"; {leftover} un-wanted leftover(s) deleted" if leftover else "")
                                + f") {name!r}"
                            )
            if (not reaped and on_disk < sel_audio
                    and "paused" not in state and "stopped" not in state):
                # Partially moved, still has wanted content -> pause seeding
                # while the pipeline finishes the rest.
                qbt.pause(h)
                paused += 1
                emit(
                    f"lifecycle: PAUSED (imported {sel_audio - on_disk}/{sel_audio} "
                    f"so far) {name!r}"
                )
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
                        rpm=int(oc.get("rpm", 10)),
                        max_wait_seconds=float(oc.get("max_wait_seconds", 30)),
                        max_retries=int(oc.get("max_retries", 3)),
                        cooldown_seconds=float(oc.get("cooldown_seconds", 900)),
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
