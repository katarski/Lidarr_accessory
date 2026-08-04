"""
Minimal qBittorrent Web API (v2) client -- just what the selective-download
tool needs: log in, list torrents, list a torrent's files, and set per-file
priority (0 = don't download).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("qbittorrent")


class QbtClient:
    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.s = requests.Session()
        self._logged_in = False

    def _api_ok(self) -> bool:
        """True if the API answers without a 403 -- i.e. we're authorized
        (either auth is bypassed for our IP, or we already have a session)."""
        try:
            r = self.s.get(f"{self.base}/api/v2/app/webapiVersion", timeout=10)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def login(self) -> bool:
        # Many setups bypass auth for LAN/whitelisted IPs -> no login needed.
        # Probe first; only POST credentials if the API actually challenges us.
        if self._api_ok():
            self._logged_in = True
            logger.info("qBittorrent: authorized without login (auth bypassed for this host)")
            return True
        try:
            r = self.s.post(
                f"{self.base}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base},
                timeout=15,
            )
            if r.status_code == 200 and r.text.strip().lower() == "ok.":
                self._logged_in = self._api_ok()
                return self._logged_in
            logger.warning("qBittorrent login failed (status=%s body=%r)",
                           r.status_code, r.text[:100])
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent login error: %s", exc)
            return False

    def torrents(self, category: str = "", state_filter: str = "") -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if category:
            params["category"] = category
        if state_filter:
            params["filter"] = state_filter   # e.g. "paused", "downloading"
        try:
            r = self.s.get(f"{self.base}/api/v2/torrents/info", params=params, timeout=30)
            r.raise_for_status()
            return r.json() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent torrents/info failed: %s", exc)
            return []

    def files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        try:
            r = self.s.get(
                f"{self.base}/api/v2/torrents/files",
                params={"hash": torrent_hash}, timeout=30,
            )
            r.raise_for_status()
            return r.json() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent torrents/files(%s) failed: %s",
                           torrent_hash, exc)
            return []

    def set_file_priority(
        self, torrent_hash: str, indices: List[int], priority: int
    ) -> bool:
        """priority 0 = do not download; 1 = normal; 6/7 = high/max."""
        if not indices:
            return True
        try:
            r = self.s.post(
                f"{self.base}/api/v2/torrents/filePrio",
                data={
                    "hash": torrent_hash,
                    "id": "|".join(str(i) for i in indices),
                    "priority": priority,
                },
                timeout=30,
            )
            r.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent filePrio failed: %s", exc)
            return False

    def _post_first_ok(self, endpoints, data) -> bool:
        """POST to each endpoint until one doesn't 404 (handles the qBittorrent
        5.x rename of pause->stop / resume->start across versions)."""
        for ep in endpoints:
            try:
                r = self.s.post(f"{self.base}/api/v2/torrents/{ep}",
                                data=data, timeout=15)
                if r.status_code != 404:
                    return r.status_code < 400
            except Exception:  # noqa: BLE001
                return False
        return False

    def pause(self, torrent_hash: str) -> None:
        # qBittorrent 5.x renamed 'pause' -> 'stop'; older builds use 'pause'.
        self._post_first_ok(("stop", "pause"), {"hashes": torrent_hash})

    def torrent_by_hash(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """The one torrent, or None if qBittorrent does not have it (which is
        how we verify a delete actually happened -- the delete endpoint answers
        200 OK for a hash it has never heard of)."""
        try:
            r = self.s.get(f"{self.base}/api/v2/torrents/info",
                           params={"hashes": torrent_hash}, timeout=20)
            r.raise_for_status()
            rows = r.json() or []
            return rows[0] if rows else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("torrent_by_hash(%s) failed: %s",
                           torrent_hash[:12], exc)
            return None

    def _running(self, torrent_hash: str) -> Optional[bool]:
        """True if qBittorrent has this torrent and it is not paused/stopped;
        None if it does not have it at all."""
        t = self.torrent_by_hash(torrent_hash)
        if t is None:
            return None
        state = str(t.get("state") or "")
        return not (state.startswith("paused") or state.startswith("stopped"))

    def ensure_resumed(self, torrent_hash: str) -> bool:
        """Resume WITHOUT setting force-start (so Lidarr's queue limits still
        apply) and confirm it actually left the paused state. We pause torrents
        to narrow their file selection; if the resume is silently dropped the
        torrent sits there forever looking like the pipeline did nothing."""
        self.resume(torrent_hash)
        for attempt in (0, 1):
            run = self._running(torrent_hash)
            if run is None:
                logger.warning("ensure_resumed: qBittorrent has no torrent %s",
                               torrent_hash[:12])
                return False
            if run:
                return True
            if attempt == 0:
                time.sleep(2)
        logger.warning("ensure_resumed: %s stayed paused after resume",
                       torrent_hash[:12])
        return False

    def ensure_started(self, torrent_hash: str) -> bool:
        """Force-start a torrent and CONFIRM it left the paused state.

        setForceStart returns 200 whether or not it did anything, so trusting
        the status code lets a narrowed torrent sit paused forever with nothing
        in the log. Verify, and fall back to a plain resume before giving up.
        """
        self.force_start(torrent_hash, True)
        for attempt in (0, 1):
            t = self.torrent_by_hash(torrent_hash)
            if t is None:
                logger.warning("ensure_started: qBittorrent has no torrent %s",
                               torrent_hash[:12])
                return False
            state = str(t.get("state") or "")
            if not state.startswith("paused") and state != "stoppedDL"                     and state != "stoppedUP":
                return True
            if attempt == 0:
                logger.info("ensure_started: %s still %s -- trying resume",
                            torrent_hash[:12], state)
                self.resume(torrent_hash)
                time.sleep(2)
        logger.warning("ensure_started: %s REFUSED to start (state=%s)",
                       torrent_hash[:12], state)
        return False

    def resume(self, torrent_hash: str) -> None:
        # qBittorrent 5.x renamed 'resume' -> 'start'; older builds use 'resume'.
        self._post_first_ok(("start", "resume"), {"hashes": torrent_hash})

    def force_start(self, torrent_hash: str, value: bool = True) -> bool:
        """Force-start a torrent so it ignores queue/ratio limits and connects
        immediately (this also un-pauses it). value=False clears the flag."""
        try:
            r = self.s.post(f"{self.base}/api/v2/torrents/setForceStart",
                        data={"hashes": torrent_hash,
                              "value": "true" if value else "false"},
                        timeout=15)
            return r.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("force_start(%s) failed: %s", torrent_hash[:12], exc)
        return False

    def set_category(self, torrent_hash: str, category: str) -> bool:
        """Assign a category to a torrent (qBit `torrents/setCategory`)."""
        # NOTE: _post_first_ok already prepends /api/v2/torrents/, so the
        # endpoint here is the bare ACTION name -- passing a full path produced
        # ".../torrents//api/v2/torrents/setCategory" and a silent 404.
        return self._post_first_ok(
            ["setCategory"],
            {"hashes": torrent_hash, "category": category},
        )

    def remove(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """Delete a torrent. delete_files=True also removes its data on disk."""
        try:
            r = self.s.post(
                f"{self.base}/api/v2/torrents/delete",
                data={
                    "hashes": torrent_hash,
                    "deleteFiles": "true" if delete_files else "false",
                },
                timeout=30,
            )
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent delete(%s) failed: %s", torrent_hash, exc)
            return False
        # the delete endpoint answers 200 for an unknown hash, so the only real
        # confirmation is that qBittorrent no longer lists it
        for _ in range(3):
            if self.torrent_by_hash(torrent_hash) is None:
                return True
            time.sleep(1)
        logger.warning("qBittorrent still lists %s after delete -- not removed",
                       torrent_hash[:12])
        return False
