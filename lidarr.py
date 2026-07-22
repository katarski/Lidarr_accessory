"""
Lidarr API client.

Two strategies, used in order:

1. Ask Lidarr to scan a staging folder (DownloadedAlbumsScan). Lidarr
   handles matching, moving, and renaming itself. This is the clean path.

2. If Lidarr doesn't move the files within a grace window, we move them
   into the library ourselves and trigger RefreshArtist so Lidarr picks
   up the on-disk state.

Path mapping: the Windows service sees paths like
`V:/Dan/Internet Downloads/_split_staging/<album>`; Lidarr (running in
Docker on Unraid) sees them as e.g. `/downloads/dan/_split_staging/<album>`.
The `path_mapping` config handles that translation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class LidarrConfig:
    base_url: str
    api_key: str
    library_root_lidarr: str
    library_root_windows: str
    path_mapping_from: str
    path_mapping_to: str


class LidarrClient:
    def __init__(self, cfg: LidarrConfig, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"X-Api-Key": cfg.api_key})

    # ---- Path translation ------------------------------------------------

    def windows_to_lidarr(self, windows_path: Path) -> str:
        """Translate a Windows path under `path_mapping.from` to Lidarr's view."""
        norm = str(windows_path).replace("\\", "/")
        src = self.cfg.path_mapping_from.replace("\\", "/").rstrip("/")
        dst = self.cfg.path_mapping_to.rstrip("/")
        if norm.lower().startswith(src.lower()):
            remainder = norm[len(src):].lstrip("/")
            return f"{dst}/{remainder}" if remainder else dst
        # No mapping match -- return as-is; Lidarr will likely reject it.
        logger.warning("Path %s is not under mapped prefix %s", norm, src)
        return norm

    # ---- HTTP helpers ----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}{path}"

    def _get(self, path: str, **params) -> Any:
        r = self.session.get(self._url(path), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        r = self.session.post(self._url(path), json=payload, timeout=30)
        r.raise_for_status()
        if r.content:
            return r.json()
        return None

    def _put(self, path: str, payload: Dict[str, Any]) -> Any:
        r = self.session.put(self._url(path), json=payload, timeout=30)
        r.raise_for_status()
        if r.content:
            return r.json()
        return None

    def set_album_auto_switch(self, album_id: int, enabled: bool) -> bool:
        """
        Set Lidarr's 'Automatically Switch Release' flag (album.anyReleaseOk).
        Returns False (no-op) if the flag is already at the requested value,
        avoiding spurious PUTs that trigger unnecessary artist refreshes.
        """
        album = self.get_album(album_id)
        if not album:
            return False
        if bool(album.get("anyReleaseOk")) == bool(enabled):
            return True  # already at desired state, no PUT needed
        album["anyReleaseOk"] = bool(enabled)
        try:
            self._put(f"/api/v1/album/{album_id}", album)
            logger.info("Set anyReleaseOk=%s on album %s", enabled, album_id)
            return True
        except Exception as exc:
            logger.error("set_album_auto_switch failed: %s", exc)
            return False

    def iter_album_releases(self, album_id: int) -> List[Dict[str, Any]]:
        """Return every release for this album (id, trackCount, title, etc)."""
        album = self.get_album(album_id)
        if not album:
            return []
        return album.get("releases") or []

    def set_album_monitored_release(
        self, album_id: int, release_id: int
    ) -> bool:
        """
        PUT /api/v1/album/{id} with the given release marked monitored=True
        and all other releases monitored=False. Lidarr's ManualImport maps
        files against the monitored release, so switching it lets us import
        files whose track count differs from the default release.
        """
        album = self.get_album(album_id)
        if not album:
            return False
        releases = album.get("releases") or []
        if not any(r.get("id") == release_id for r in releases):
            logger.warning(
                "set_album_monitored_release: release %s not in album %s",
                release_id, album_id,
            )
            return False
        for r in releases:
            r["monitored"] = (r.get("id") == release_id)
        album["releases"] = releases
        try:
            self._put(f"/api/v1/album/{album_id}", album)
            logger.info(
                "Switched album %s monitored release to %s",
                album_id, release_id,
            )
            return True
        except Exception as exc:
            logger.error("set_album_monitored_release failed: %s", exc)
            return False

    def find_release_matching_track_count(
        self, album_id: int, desired_track_count: int
    ) -> Optional[int]:
        """
        Return the release id whose track count most closely matches the
        desired count (disk file count). Returns None if nothing is within
        +/- 1 of the desired count.
        """
        album = self.get_album(album_id)
        if not album:
            return None
        best_id, best_delta = None, 9999
        for r in album.get("releases") or []:
            tc = r.get("trackCount") or 0
            delta = abs(tc - desired_track_count)
            if delta < best_delta:
                best_id, best_delta = r.get("id"), delta
        if best_id is not None and best_delta <= 1:
            return best_id
        return None

    # ---- Public API ------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._get("/api/v1/system/status")
            return True
        except Exception as exc:
            logger.error("Lidarr ping failed: %s", exc)
            return False

    def downloaded_albums_scan(
        self,
        staging_dir: Path,
        download_client_id: Optional[str] = None,
    ) -> Optional[int]:
        """
        Ask Lidarr to import a completed download from `staging_dir`.

        If `download_client_id` is given, Lidarr ties the scan to the
        matching queue entry -- so on success it removes the queue row
        and marks that specific download as grabbed-and-imported (exactly
        what unpackerr does for stuck entries).

        Returns the command id on success, None on failure.
        """
        lidarr_path = self.windows_to_lidarr(staging_dir)
        payload: Dict[str, Any] = {
            "name": "DownloadedAlbumsScan",
            "path": lidarr_path,
            "importMode": "Move",
        }
        if download_client_id:
            payload["downloadClientId"] = download_client_id
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info(
                "Triggered DownloadedAlbumsScan id=%s on %s (dl_id=%s)",
                cmd_id, lidarr_path, download_client_id or "<none>",
            )
            return cmd_id
        except Exception as exc:
            logger.error("DownloadedAlbumsScan failed: %s", exc)
            return None

    def queue_find_for(
        self, artist_name: str, album_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Look up the download-client queue entry matching this artist/album.
        Matches on artist name (exact) first, then falls back to 'album
        name appears in the queue title'. Returns the full queue record
        (we need its `downloadId` for scan correlation).
        """
        entries = self.queue_list()
        if not entries:
            return None
        tgt_artist = (artist_name or "").strip().lower()
        tgt_album = (album_name or "").strip().lower()
        # Pass 1: exact artist + album-in-title.
        for e in entries:
            e_artist = ((e.get("artist") or {}).get("artistName") or "").lower()
            title = (e.get("title") or "").lower()
            if tgt_artist and e_artist == tgt_artist and (
                not tgt_album or tgt_album in title
            ):
                return e
        # Pass 2: album match anywhere (torrent title often starts with artist).
        if tgt_album:
            for e in entries:
                title = (e.get("title") or "").lower()
                output = (e.get("outputPath") or "").lower()
                if tgt_album in title or tgt_album in output:
                    return e
        return None

    def process_monitored_downloads(self) -> Optional[int]:
        """
        Nudge Lidarr to re-examine its download-client queue right now
        instead of waiting for its next scheduled sweep. Equivalent to
        clicking 'Interactive Search' / 'Refresh' on the Activity page.
        """
        try:
            resp = self._post(
                "/api/v1/command", {"name": "ProcessMonitoredDownloads"}
            )
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info("Triggered ProcessMonitoredDownloads id=%s", cmd_id)
            return cmd_id
        except Exception as exc:
            logger.warning("ProcessMonitoredDownloads failed: %s", exc)
            return None

    def command_status(self, command_id: int) -> Optional[str]:
        try:
            resp = self._get(f"/api/v1/command/{command_id}")
            return resp.get("status")  # queued | started | completed | failed
        except Exception as exc:
            logger.warning("command_status(%s) failed: %s", command_id, exc)
            return None

    def command_record(self, command_id: int) -> Optional[Dict[str, Any]]:
        """Full command record including message/exception/result fields."""
        try:
            return self._get(f"/api/v1/command/{command_id}")
        except Exception as exc:
            logger.warning("command_record(%s) failed: %s", command_id, exc)
            return None

    def wait_for_command(
        self,
        command_id: int,
        timeout_seconds: int = 60,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Poll /api/v1/command/{id} until it reaches a terminal state
        (completed / failed / aborted) or `timeout_seconds` elapses.
        Returns the final record (or last partial if timed out).
        """
        terminal = {"completed", "failed", "aborted"}
        deadline = time.monotonic() + timeout_seconds
        last: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            rec = self.command_record(command_id)
            if rec:
                last = rec
                status = (rec.get("status") or "").lower()
                if status in terminal:
                    return rec
            time.sleep(poll_interval)
        return last

    def manual_import_candidates(self, folder_path: str) -> List[Dict[str, Any]]:
        """
        Ask Lidarr what it would do with the given folder via its manual
        import endpoint. Returns the list of parsed track records, each
        with whatever match Lidarr found plus a `rejections` array that
        explains *why* a file wouldn't auto-import. Invaluable for
        diagnosing silent refusals.
        """
        try:
            r = self.session.get(
                self._url("/api/v1/manualimport"),
                params={"folder": folder_path, "filterExistingFiles": "false"},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json() or []
            logger.info(
                "manualimport: %s -> %d candidates",
                folder_path, len(data),
            )
            for c in data[:5]:
                logger.info(
                    "  path=%s artist=%s album=%s tracks=%d rejections=%s",
                    c.get("path"),
                    (c.get("artist") or {}).get("artistName"),
                    (c.get("album") or {}).get("title"),
                    len(c.get("tracks") or []),
                    [r.get("reason") for r in (c.get("rejections") or [])],
                )
            return data
        except Exception as exc:
            logger.warning("manualimport query failed for %s: %s", folder_path, exc)
            return []

    def manual_import_positional(
        self,
        candidates: List[Dict[str, Any]],
        tracks: List[Dict[str, Any]],
        album_id: int,
        release_id: int,
        artist_id: int,
    ) -> Optional[int]:
        """
        Last-resort force-import: pair candidates (file paths) to tracks
        by POSITION. Used when Lidarr's fuzzy matcher refuses all files
        even though disk count == release trackCount -- the tracks and
        files name differently but live in the same order.

        We take candidates sorted by path (natural filename order) and
        tracks sorted by (mediumNumber, absoluteTrackNumber) and zip them.
        Each item is sent with disableReleaseSwitching=True so Lidarr
        doesn't second-guess us.

        Returns the command id or None if the counts don't align or the
        ManualImport fails.
        """
        if not candidates or not tracks:
            logger.warning(
                "manual_import_positional: no candidates (%d) or tracks (%d)",
                len(candidates), len(tracks),
            )
            return None
        usable_cands = [c for c in candidates if c.get("path")]
        if len(usable_cands) != len(tracks):
            logger.warning(
                "manual_import_positional: count mismatch "
                "(candidates=%d tracks=%d); refusing to force-match",
                len(usable_cands), len(tracks),
            )
            return None
        usable_cands.sort(key=lambda c: (c.get("path") or "").lower())
        tracks_sorted = sorted(
            tracks,
            key=lambda t: (
                t.get("mediumNumber") or 1,
                t.get("absoluteTrackNumber")
                or t.get("trackNumber")
                or 0,
            ),
        )
        files = []
        for cand, trk in zip(usable_cands, tracks_sorted):
            tid = trk.get("id")
            if not tid:
                continue
            files.append({
                "path": cand.get("path"),
                "artistId": artist_id,
                "albumId": album_id,
                "albumReleaseId": release_id,
                "trackIds": [tid],
                "quality": cand.get("quality"),
                "releaseGroup": cand.get("releaseGroup") or "",
                "disableReleaseSwitching": True,
                "additionalFile": False,
                "replaceExistingFiles": False,
            })
        if not files:
            logger.warning("manual_import_positional: no usable file/track pairs")
            return None
        payload = {
            "name": "ManualImport",
            "files": files,
            "importMode": "move",
            "replaceExistingFiles": False,
        }
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info(
                "Triggered positional ManualImport id=%s with %d files "
                "(album=%s release=%s)",
                cmd_id, len(files), album_id, release_id,
            )
            return cmd_id
        except Exception as exc:
            body = ""
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                try:
                    body = resp_obj.text[:500]
                except Exception:
                    body = "(body unreadable)"
            logger.error(
                "positional ManualImport failed: %s | body=%s | first=%s",
                exc, body, (files[0] if files else None),
            )
            return None

    def manual_import_apply(self, items: List[Dict[str, Any]]) -> Optional[int]:
        """
        Commit a ManualImport command. `items` is what you got back from
        manual_import_candidates() after filtering out anything with
        hard rejections. Returns the command id or None.

        We're conservative about what we send: Lidarr's ManualImport
        returns 500 when required fields (artistId / albumId /
        albumReleaseId / trackIds) are missing, so items without all of
        them are dropped here.
        """
        if not items:
            return None
        files = []
        for it in items:
            artist_id = (it.get("artist") or {}).get("id")
            album_id = (it.get("album") or {}).get("id")
            release_id = (it.get("albumRelease") or {}).get("id")
            track_ids = [t["id"] for t in (it.get("tracks") or []) if t.get("id")]
            if not all([it.get("path"), artist_id, album_id, release_id, track_ids]):
                logger.debug(
                    "Skipping ManualImport item with missing fields: "
                    "path=%r artistId=%r albumId=%r releaseId=%r tracks=%d",
                    it.get("path"), artist_id, album_id, release_id, len(track_ids),
                )
                continue
            files.append({
                "path": it.get("path"),
                "artistId": artist_id,
                "albumId": album_id,
                "albumReleaseId": release_id,
                "trackIds": track_ids,
                "quality": it.get("quality"),
                "releaseGroup": it.get("releaseGroup") or "",
                "disableReleaseSwitching": False,
                "additionalFile": False,
                "replaceExistingFiles": False,
            })
        if not files:
            logger.warning("ManualImport: no items have the fields Lidarr requires")
            return None
        payload = {
            "name": "ManualImport",
            "files": files,
            "importMode": "move",
            "replaceExistingFiles": False,
        }
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info("Triggered ManualImport id=%s with %d files", cmd_id, len(files))
            return cmd_id
        except Exception as exc:
            # Surface the HTTP body (if any) so we can see Lidarr's reason.
            body = ""
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                try:
                    body = resp_obj.text[:500]
                except Exception:
                    body = "(body unreadable)"
            logger.error(
                "ManualImport failed: %s | body=%s | first file=%s",
                exc, body, (files[0] if files else None),
            )
            return None

    # ---- Download-client queue management ----------------------------

    def queue_list(self) -> List[Dict[str, Any]]:
        """Return all items currently in Lidarr's download-client queue."""
        try:
            resp = self._get(
                "/api/v1/queue",
                pageSize=1000,
                includeUnknownArtistItems="true",
            )
        except Exception as exc:
            logger.warning("queue list fetch failed: %s", exc)
            return []
        if isinstance(resp, dict) and "records" in resp:
            return resp.get("records") or []
        return resp if isinstance(resp, list) else []

    def queue_remove(
        self,
        queue_id: int,
        remove_from_client: bool = False,
        blocklist: bool = False,
    ) -> bool:
        """DELETE /api/v1/queue/{id}. Returns True on success."""
        url = self._url(f"/api/v1/queue/{queue_id}")
        params = {
            "removeFromClient": "true" if remove_from_client else "false",
            "blocklist": "true" if blocklist else "false",
        }
        try:
            r = self.session.delete(url, params=params, timeout=30)
            if r.status_code == 404:
                # Row already gone -- common with grouped downloads where
                # removing one entry collapses the whole download's grouping.
                logger.debug("queue_remove(%s): already gone (404)", queue_id)
                return False
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("queue_remove(%s) failed: %s", queue_id, exc)
            return False

    def find_artist(self, name: str) -> Optional[Dict[str, Any]]:
        """Search Lidarr's local library for an artist by (fuzzy) name match."""
        if not name:
            return None
        try:
            results = self._get("/api/v1/artist")
        except Exception as exc:
            logger.error("Artist list fetch failed: %s", exc)
            return None

        target = name.strip().lower()
        # Exact-ish first.
        for a in results:
            if a.get("artistName", "").strip().lower() == target:
                return a
        # Substring fallback.
        for a in results:
            if target in a.get("artistName", "").strip().lower():
                return a
        return None

    def list_albums_for_artist(self, artist_id: int) -> List[Dict[str, Any]]:
        """Return every album Lidarr has under this artist."""
        try:
            return self._get("/api/v1/album", artistId=artist_id) or []
        except Exception as exc:
            logger.warning("list_albums_for_artist(%s) failed: %s", artist_id, exc)
            return []

    def find_album(
        self, artist_id: int, album_title: str
    ) -> Optional[Dict[str, Any]]:
        """Fuzzy lookup of an album record by title under a given artist."""
        target = (album_title or "").strip().lower()
        if not target:
            return None
        albums = self.list_albums_for_artist(artist_id)
        # Exact match on title first.
        for a in albums:
            if (a.get("title") or "").strip().lower() == target:
                return a
        # Substring either direction (handles "Ebbhead" vs "Ebbhead (2CD)").
        for a in albums:
            at = (a.get("title") or "").strip().lower()
            if at and (target in at or at in target):
                return a
        return None

    def get_album(self, album_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single album record, including its releases."""
        try:
            return self._get(f"/api/v1/album/{album_id}")
        except Exception as exc:
            logger.warning("get_album(%s) failed: %s", album_id, exc)
            return None

    def list_tracks_for_album(self, album_id: int) -> List[Dict[str, Any]]:
        """Return the track list for an album."""
        try:
            return self._get("/api/v1/track", albumId=album_id) or []
        except Exception as exc:
            logger.warning("list_tracks_for_album(%s) failed: %s", album_id, exc)
            return []

    def refresh_artist(self, artist_id: int) -> Optional[int]:
        payload = {"name": "RefreshArtist", "artistId": artist_id}
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info("Triggered RefreshArtist id=%s for artistId=%s", cmd_id, artist_id)
            return cmd_id
        except Exception as exc:
            logger.error("RefreshArtist failed: %s", exc)
            return None

    def rescan_artist(self, artist_id: int) -> Optional[int]:
        """
        Lidarr does NOT have a RescanArtist command -- every payload
        variant we tried (artistId / artistIds) returns 500. Retained as
        a no-op so callers don't need to special-case it. Use
        refresh_artist() + rescan_folder() or downloaded_albums_scan_rescan()
        to actually trigger a re-scan.
        """
        logger.debug(
            "rescan_artist(%s): no-op (RescanArtist command doesn't exist "
            "in Lidarr); caller should use refresh_artist or "
            "downloaded_albums_scan_rescan instead",
            artist_id,
        )
        return None

    def rescan_folder(self, lidarr_path: str) -> Optional[int]:
        """
        Ask Lidarr to rescan a specific folder (by the path Lidarr sees,
        not the Windows path). Useful when we know files landed in the
        library but Lidarr's artist record isn't reflecting them yet --
        e.g. because Lidarr hasn't learned the artist, or an import
        completed but the release mapping didn't stick. RescanFolder
        walks the folder and imports whatever it finds.
        """
        payload = {"name": "RescanFolders", "folders": [lidarr_path]}
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info("Triggered RescanFolders id=%s on %s", cmd_id, lidarr_path)
            return cmd_id
        except Exception as exc:
            logger.warning("RescanFolders failed on %s: %s", lidarr_path, exc)
            return None

    def downloaded_albums_scan_rescan(
        self, lidarr_path: str
    ) -> Optional[int]:
        """
        DownloadedAlbumsScan without a `downloadClientId` -- used as a
        post-import reconciliation on the LIBRARY folder (not staging).
        Forces Lidarr to re-import whatever's sitting under that path,
        which is what finally makes the album show up when the initial
        ManualImport chose the wrong release or skipped a mapping.
        """
        payload: Dict[str, Any] = {
            "name": "DownloadedAlbumsScan",
            "path": lidarr_path,
            "importMode": "Move",
        }
        try:
            resp = self._post("/api/v1/command", payload)
            cmd_id = resp.get("id") if isinstance(resp, dict) else None
            logger.info(
                "Triggered post-import DownloadedAlbumsScan id=%s on %s",
                cmd_id, lidarr_path,
            )
            return cmd_id
        except Exception as exc:
            logger.warning(
                "Post-import DownloadedAlbumsScan failed on %s: %s",
                lidarr_path, exc,
            )
            return None

    def lidarr_to_windows(self, lidarr_path: str) -> str:
        """Inverse of `windows_to_lidarr` -- map Lidarr's path back to Windows."""
        norm = (lidarr_path or "").replace("\\", "/").rstrip("/")
        src = self.cfg.path_mapping_to.replace("\\", "/").rstrip("/")
        dst = self.cfg.path_mapping_from.rstrip("/")
        if norm.lower().startswith(src.lower()):
            remainder = norm[len(src):].lstrip("/")
            return f"{dst}/{remainder}" if remainder else dst
        return norm

    def library_windows_to_lidarr(self, windows_path: Path) -> str:
        """
        Translate a path under the music LIBRARY root (not downloads) to
        Lidarr's view, using `library_root_windows` -> `library_root_lidarr`
        mapping. This is independent of the downloads path_mapping.
        """
        norm = str(windows_path).replace("\\", "/")
        src = self.cfg.library_root_windows.replace("\\", "/").rstrip("/")
        dst = self.cfg.library_root_lidarr.rstrip("/")
        if norm.lower().startswith(src.lower()):
            remainder = norm[len(src):].lstrip("/")
            return f"{dst}/{remainder}" if remainder else dst
        logger.debug(
            "library_windows_to_lidarr: %s not under %s", norm, src,
        )
        return norm
