"""
Minimal qBittorrent Web API (v2) client -- just what the selective-download
tool needs: log in, list torrents, list a torrent's files, and set per-file
priority (0 = don't download).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests

import claims

logger = logging.getLogger("qbittorrent")

# Lidarr hands out release guids shaped "<indexerId>_magnet:?xt=urn:btih:...".
# The prefix has to come off before the magnet is usable, and the infohash is
# the only identifier that survives the round trip -- qBittorrent renames a
# torrent to its own `name` field, so verifying a grab by TITLE is unreliable.
_BTIH_RE = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")


def magnet_from_guid(guid: str) -> Optional[str]:
    """The bare magnet URI out of a Lidarr guid (strips any '<id>_' prefix)."""
    s = str(guid or "")
    i = s.find("magnet:")
    return s[i:] if i >= 0 else None


def btih_from_magnet(magnet: str) -> Optional[str]:
    """Infohash from a magnet, LOWERCASED -- magnets carry it uppercase while
    qBittorrent reports lowercase, so every comparison must be normalized."""
    m = _BTIH_RE.search(str(magnet or ""))
    return m.group(1).lower() if m else None


class _ReauthSession:
    """The client's session: a 403 (the SID expired, qBittorrent restarted)
    logs in again and repeats the request once. The client lives for the
    whole process now, so this is its only re-authentication."""

    def __init__(self, client: "QbtClient") -> None:
        self.raw = requests.Session()
        self._client = client

    def _send(self, method: str, url: str, **kw):
        r = getattr(self.raw, method)(url, **kw)
        if getattr(r, "status_code", 0) == 403 and "/api/v2/auth/" not in url:
            if self._client._relogin():
                r = getattr(self.raw, method)(url, **kw)
        return r

    def get(self, url: str, **kw):
        return self._send("get", url, **kw)

    def post(self, url: str, **kw):
        return self._send("post", url, **kw)

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


_shared: Dict[tuple, "QbtClient"] = {}
_shared_lock = threading.Lock()


def shared(base_url: str, username: str = "", password: str = "") -> "QbtClient":
    """One client per qBittorrent for the whole process. Every loop built its
    own every pass (a new session, a probe and an INFO line every 30 s, and a
    fresh login whenever auth is on)."""
    key = (str(base_url or "").rstrip("/"), username or "", password or "")
    with _shared_lock:
        q = _shared.get(key)
        if q is None:
            q = _shared[key] = QbtClient(base_url, username, password)
        return q


def infohash_of(data: bytes) -> Optional[str]:
    """The v1 infohash of a .torrent: SHA-1 of its bencoded `info` value,
    lowercase hex. None when it does not parse."""
    def skip(i: int) -> int:
        c = data[i:i + 1]
        if c == b"i":
            return data.index(b"e", i) + 1
        if c in (b"l", b"d"):
            i += 1
            while data[i:i + 1] != b"e":
                if i >= len(data):
                    raise ValueError("unterminated")
                i = skip(i)
            return i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            return colon + 1 + int(data[i:colon])
        raise ValueError("bad bencode at %d" % i)
    try:
        if data[:1] != b"d":
            return None
        i = 1
        while data[i:i + 1] != b"e":
            kend = skip(i)
            key = data[data.index(b":", i) + 1:kend]
            vend = skip(kend)
            if key == b"info":
                return hashlib.sha1(data[kend:vend]).hexdigest()
            i = vend
    except (ValueError, IndexError, RecursionError):
        return None
    return None


class QbtClient:
    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.s = _ReauthSession(self)
        self._logged_in = False
        self._auth_mode = ""
        self._login_lock = threading.Lock()

    def _http(self):
        return getattr(self.s, "raw", self.s)

    def _api_ok(self) -> bool:
        """True if the API answers without a 403 -- i.e. we're authorized
        (either auth is bypassed for our IP, or we already have a session)."""
        try:
            r = self._http().get(f"{self.base}/api/v2/app/webapiVersion", timeout=10)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _relogin(self) -> bool:
        self._logged_in = False
        return self.login()

    def _note_auth(self, mode: str) -> None:
        if mode != self._auth_mode:
            self._auth_mode = mode
            logger.info("qBittorrent: %s", "authorized without login (auth "
                        "bypassed for this host)" if mode == "bypass"
                        else "logged in")

    def login(self) -> bool:
        # Many setups bypass auth for LAN/whitelisted IPs -> no login needed.
        # Probe first; only POST credentials if the API actually challenges us.
        with self._login_lock:
            return self._login_locked()

    def _login_locked(self) -> bool:
        if self._api_ok():
            self._logged_in = True
            if self._auth_mode != "login":
                self._note_auth("bypass")
            return True
        try:
            r = self._http().post(
                f"{self.base}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base},
                timeout=15,
            )
            if r.status_code == 200 and r.text.strip().lower() == "ok.":
                self._logged_in = self._api_ok()
                if self._logged_in:
                    self._note_auth("login")
                return self._logged_in
            logger.warning("qBittorrent login failed (status=%s body=%r)",
                           r.status_code, r.text[:100])
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("qBittorrent login error: %s", exc)
            return False

    def torrents(self, category: str = "", state_filter: str = "",
                 tag: str = "") -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
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

    # Tag stamped on every torrent this pipeline adds ITSELF (song/compilation
    # hunt). Such a torrent is deliberately added stopped and must stay stopped
    # until the hunt has narrowed it to the one or two songs it wants -- so the
    # generic deselect pass, which starts anything it finds added-stopped, has to
    # be able to recognise and leave it alone. Without this the pass would start
    # a 3-CD box in full and defeat the entire point of the hunt.
    SELF_ADDED_TAG = "cue-assembly"

    def add_magnet(self, magnet: str, category: str = "",
                   paused: bool = True,
                   stop_on_metadata: bool = True,
                   tags: str = SELF_ADDED_TAG) -> Optional[str]:
        """
        Add a magnet directly and return its infohash (lowercase), or None.

        Needed because Lidarr's grab endpoint REFUSES any release it cannot
        attribute to a library artist -- it answers 404 "Unable to find
        matching artist and albums". That is every cross-artist compilation,
        which is exactly where a song hunt finds stray tracks (a Sam Cooke
        Christmas disc holding Billie Holiday sides). So for the harvest path
        we add the torrent ourselves.

        The caller wants the torrent to sit still until the file-selection step
        has dropped the tracks it does not want -- otherwise a 3-CD box pulls in
        full to take two songs. But a magnet CARRIES NO FILE LIST: the file
        names live in metadata that has to be fetched from peers, and a STOPPED
        torrent connects to nobody, so a magnet added stopped can never populate
        its files. The old code added it stopped and then waited up to 180s for
        a file list that was never going to arrive, and discarded the release --
        which is how "The Essential Billie Holiday 3 cd boxset[flac]", holding
        27 of the wanted sides, got blocklisted for "no file list".

        So `stop_on_metadata` uses qBittorrent's own `stopCondition`
        (MetadataReceived, available since 4.5): the torrent starts, fetches
        ONLY the metadata, and qBittorrent stops it the instant that lands --
        before any content transfers. Measured on the release above: 65 files
        known after 5 seconds, 0 bytes of content downloaded.
        """
        mag = magnet_from_guid(magnet) or str(magnet or "")
        ih = btih_from_magnet(mag)
        if not mag.startswith("magnet:"):
            logger.warning("add_magnet: not a magnet URI, refusing")
            return None
        data: Dict[str, str] = {"urls": mag}
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        if paused and stop_on_metadata:
            data["stopCondition"] = "MetadataReceived"
        elif paused:
            # Hard stop -- no metadata will be fetched, so only use this when
            # the file list genuinely is not needed.
            # qBittorrent 5.x renamed `paused` to `stopped`; sending both keeps
            # this working across versions (the unknown key is ignored).
            data["paused"] = "true"
            data["stopped"] = "true"
        if not self._api_ok():
            self.login()
        try:
            r = self.s.post(f"{self.base}/api/v2/torrents/add",
                            data=data, timeout=30)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_magnet failed (%s): %s",
                           (ih or "?")[:12], exc)
            return None
        # qBittorrent answers a bare "Ok." even when it silently ignores a
        # duplicate, so confirm by hash rather than trusting the response.
        if ih:
            for _ in range(10):
                if self.torrent_by_hash(ih):
                    logger.info("add_magnet: qBittorrent accepted %s", ih[:12])
                    return ih
                time.sleep(2)
            logger.warning("add_magnet: %s never appeared in qBittorrent",
                           ih[:12])
            return None
        return ih

    @staticmethod
    def _fetch_torrent(url: str) -> Tuple[Optional[bytes], Optional[str], str]:
        """GET a .torrent through its (Prowlarr) link: (body, magnet, why).
        A redirect to a magnet gives the magnet; an HTTP refusal gives why;
        body None, magnet None and why "" means we could not ask at all."""
        try:
            r = requests.get(url, timeout=60, allow_redirects=False)
            for _ in range(3):
                if r.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = str(r.headers.get("Location") or "")
                if loc.startswith("magnet:"):
                    return None, loc, ""
                r = requests.get(loc, timeout=60, allow_redirects=False)
        except requests.RequestException as exc:
            logger.debug("torrent fetch %s failed: %s", url[:60], exc)
            return None, None, ""
        if r.status_code != 200:
            return None, None, "HTTP %d %s" % (r.status_code,
                                               (r.text or "")[:120].strip())
        body = r.content or b""
        if not body.startswith(b"d"):
            return None, None, "not a .torrent (%r)" % body[:40]
        return body, None, ""

    def add_torrent_url(self, url: str, category: str = "",
                        paused: bool = True,
                        stop_on_metadata: bool = True,
                        tags: str = SELF_ADDED_TAG,
                        timeout: int = 40) -> Optional[str]:
        """
        Add a torrent from an http(s) .torrent URL and return its infohash.

        `add_magnet` refuses anything that is not a magnet URI, but a lot of
        trackers publish no magnet and no infohash at all -- every RuTracker
        music result is only a Prowlarr /download proxy link. qBittorrent takes
        both forms in the same `urls` field, so the add is identical; what
        differs is that the infohash CANNOT be known in advance, and every
        verification step downstream is keyed on it.

        So the add carries a one-off tag (`cue-add-<nonce>`) and the new torrent
        is found by that tag inside our category. It used to be found by
        diffing EVERY torrent in the client before and after: a failed snapshot
        made every torrent "new", and any other app's add in the same seconds
        (the client is shared) was as likely to be picked -- and the pick is
        what the verifier later blocklists or deletes. A duplicate add (the
        torrent is already there) yields None.
        """
        u = str(url or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            logger.warning("add_torrent_url: not an http(s) URL, refusing")
            return None
        # Fetch it ourselves first. Handing qBittorrent the link hid every
        # failure: RuTracker behind Cloudflare refused the fetch, nothing
        # appeared, and each such candidate cost a 40s wait ending in "a
        # duplicate, or the fetch failed" (28 times on 29-30 Sep). With the
        # file in hand the reason is known at once, and so is the infohash.
        body, magnet, why = self._fetch_torrent(u)
        if magnet:
            return self.add_magnet(magnet, category=category, paused=paused,
                                   stop_on_metadata=stop_on_metadata, tags=tags)
        if body is None and why:
            logger.warning("add_torrent_url: the indexer refused the .torrent "
                           "(%s) -- %s", why, u[:80])
            return None
        if body is not None:
            return self._add_torrent_file(body, category, paused,
                                          stop_on_metadata, tags)
        if not self._api_ok():
            self.login()
        nonce = "cue-add-" + uuid.uuid4().hex[:12]
        data: Dict[str, str] = {"urls": u, "tags": ",".join(t for t in (tags, nonce) if t)}
        if category:
            data["category"] = category
        if paused and stop_on_metadata:
            data["stopCondition"] = "MetadataReceived"
        elif paused:
            data["paused"] = "true"
            data["stopped"] = "true"
        try:
            r = self.s.post(f"{self.base}/api/v2/torrents/add",
                            data=data, timeout=60)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_torrent_url failed: %s", exc)
            return None
        # Fetching the .torrent through Prowlarr and parsing it takes a moment,
        # so poll rather than reading once.
        ih = None
        deadline = time.time() + max(5, int(timeout))
        try:
            while time.time() < deadline:
                found = [str(t.get("hash") or "").lower()
                         for t in (self.torrents(category=category, tag=nonce) or [])]
                found = [h for h in found if h]
                if found:
                    ih = found[0]
                    logger.info("add_torrent_url: qBittorrent accepted %s", ih[:12])
                    return ih
                time.sleep(2)
            logger.warning("add_torrent_url: nothing appeared in qBittorrent for "
                           "%s (a duplicate, or the fetch failed)", u[:80])
            return None
        finally:
            self._drop_tag(ih, nonce)

    def _add_torrent_file(self, body: bytes, category: str, paused: bool,
                          stop_on_metadata: bool, tags: str) -> Optional[str]:
        """Upload a fetched .torrent and return its infohash once qBittorrent
        has it. A torrent already in the client is ours only in our category;
        another app's is never taken over (None)."""
        ih = infohash_of(body)
        if not ih:
            logger.warning("add_torrent_url: the .torrent does not parse")
            return None
        if not self._api_ok():
            self.login()
        answered, have = self.lookup(ih)
        if not answered:
            return None
        if have is not None:
            if category and str(have.get("category") or "") == category:
                logger.info("add_torrent_url: %s is already in qBittorrent "
                            "(ours)", ih[:12])
                return ih
            logger.warning("add_torrent_url: %s is already in qBittorrent under "
                           "category %r -- not ours, not touched", ih[:12],
                           have.get("category"))
            return None
        data: Dict[str, str] = {}
        if tags:
            data["tags"] = tags
        if category:
            data["category"] = category
        if paused and stop_on_metadata:
            data["stopCondition"] = "MetadataReceived"
        elif paused:
            data["paused"] = "true"
            data["stopped"] = "true"
        try:
            r = self.s.post(f"{self.base}/api/v2/torrents/add", data=data,
                            files={"torrents": ("%s.torrent" % ih, body,
                                                "application/x-bittorrent")},
                            timeout=60)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_torrent_url: upload of %s failed: %s", ih[:12], exc)
            return None
        for _ in range(10):
            if self.torrent_by_hash(ih):
                logger.info("add_torrent_url: qBittorrent accepted %s", ih[:12])
                return ih
            time.sleep(2)
        logger.warning("add_torrent_url: %s never appeared in qBittorrent", ih[:12])
        return None

    def _drop_tag(self, torrent_hash: Optional[str], tag: str) -> None:
        """Take a one-off tag off the torrent and out of the client."""
        try:
            if torrent_hash:
                self.s.post(f"{self.base}/api/v2/torrents/removeTags",
                            data={"hashes": torrent_hash, "tags": tag}, timeout=15)
            self.s.post(f"{self.base}/api/v2/torrents/deleteTags",
                        data={"tags": tag}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not drop tag %s: %s", tag, exc)

    def lookup(self, torrent_hash: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """(answered, torrent). answered is False when qBittorrent could not be
        asked -- an outage, which is neither "it has the torrent" nor "it is
        gone", so no delete is verified and no removal decided on it."""
        try:
            r = self.s.get(f"{self.base}/api/v2/torrents/info",
                           params={"hashes": torrent_hash}, timeout=20)
            r.raise_for_status()
            rows = r.json() or []
            return True, (rows[0] if rows else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("torrent lookup (%s) failed: %s",
                           str(torrent_hash)[:12], exc)
            return False, None

    def torrent_by_hash(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """The one torrent, or None if qBittorrent does not have it -- or could
        not be asked; lookup() tells the two apart where that matters."""
        return self.lookup(torrent_hash)[1]

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
        """Delete a torrent. delete_files=True also removes its data on disk.
        Refused while another worker is processing a folder of it (claims),
        and when qBittorrent cannot say what the torrent is. The orchestrator
        removes through Orchestrator._remove_torrent, which also checks the
        category and drops Lidarr's queue rows; the lifecycle and the harvest
        check the category themselves. True only once qBittorrent confirms
        the torrent is gone: the delete endpoint answers 200 for any hash, and
        a lookup that failed is not a confirmation."""
        answered, info = self.lookup(torrent_hash)
        if not answered:
            logger.info("qBittorrent: not removing %s -- it could not be read",
                        str(torrent_hash)[:12])
            return False
        if info is None:
            return True                       # it is not there
        hit = claims.busy_torrent(str(info.get("content_path") or ""))
        if hit:
            logger.info("qBittorrent: not removing %s yet -- %s is being "
                        "processed right now", str(info.get("name") or torrent_hash)[:60],
                        hit)
            return False
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
        for _ in range(3):
            answered, info = self.lookup(torrent_hash)
            if answered and info is None:
                return True
            time.sleep(1)
        logger.warning("qBittorrent still lists %s after delete (or cannot say) "
                       "-- not counted as removed", str(torrent_hash)[:12])
        return False
