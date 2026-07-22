"""
Minimal qBittorrent Web API (v2) client -- just what the selective-download
tool needs: log in, list torrents, list a torrent's files, and set per-file
priority (0 = don't download).
"""

from __future__ import annotations

import logging
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

    def resume(self, torrent_hash: str) -> None:
        # qBittorrent 5.x renamed 'resume' -> 'start'; older builds use 'resume'.
        self._post_first_ok(("start", "resume"), {"hashes": torrent_hash})

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
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent delete(%s) failed: %s", torrent_hash, exc)
            return False
