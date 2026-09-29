"""Client behaviour under failure: an outage is never remembered as an answer,
caches reach disk, the held list is written once per pass and never wiped by an
empty mount, and one qBittorrent client serves the process."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import musicbrainz  # noqa: E402
import titlematch  # noqa: E402
from held_store import HeldStore  # noqa: E402


def _http_error(code, retry_after=None):
    hdrs = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("http://mb", code, "busy", hdrs, io.BytesIO(b""))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class MusicBrainz(unittest.TestCase):
    def setUp(self):
        C = musicbrainz.MusicBrainzClient
        self._saved = (C._cache, C._cache_loaded, C._last_call, C._not_before,
                       C._cache_file, C._cache_dirty, C._cache_saved)
        self.tmp = tempfile.TemporaryDirectory()
        C._cache, C._cache_loaded, C._last_call, C._not_before = {}, True, 0.0, 0.0
        C._cache_file = Path(self.tmp.name) / "mb.json"
        C._cache_dirty, C._cache_saved = False, 0.0
        self.mb = C(min_interval=1.0, retries=0)
        self.sleep = mock.patch.object(musicbrainz.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        C = musicbrainz.MusicBrainzClient
        (C._cache, C._cache_loaded, C._last_call, C._not_before,
         C._cache_file, C._cache_dirty, C._cache_saved) = self._saved
        self.tmp.cleanup()

    def test_a_503_is_unavailable_not_no_match_and_is_not_cached(self):
        from lidarr import LidarrClient
        lc = LidarrClient.__new__(LidarrClient)
        lc.mb = self.mb
        artist = {"artistName": "Ms. Lauryn Hill", "foreignArtistId": "e841"}
        with mock.patch.object(musicbrainz.urllib.request, "urlopen",
                               side_effect=_http_error(503, "1")):
            with self.assertRaises(musicbrainz.MusicBrainzUnavailable):
                self.mb.artist_mbid_for_name("Lauryn Hill")
            self.assertIsNone(lc._find_artist_via_musicbrainz("Lauryn Hill", [artist]))
        self.assertEqual(lc._mb_artist_cache, {})           # asked again next time
        musicbrainz.MusicBrainzClient._not_before = 0.0
        body = json.dumps({"artists": [{"id": "e841", "name": "Ms. Lauryn Hill",
                                        "aliases": [{"name": "Lauryn Hill"}]}]})
        with mock.patch.object(musicbrainz.urllib.request, "urlopen",
                               return_value=_Resp(body.encode())):
            self.assertIs(lc._find_artist_via_musicbrainz("Lauryn Hill", [artist]), artist)

    def test_retry_after_is_shared_and_a_long_wait_answers_at_once(self):
        calls = []

        def fail(*a, **k):
            calls.append(1)
            raise _http_error(503, "60")
        with mock.patch.object(musicbrainz.urllib.request, "urlopen", side_effect=fail):
            self.assertIsNone(self.mb._get("/artist", query="a"))
            self.assertIsNone(self.mb._get("/artist", query="b"))   # backing off
        self.assertEqual(len(calls), 1)
        self.assertGreater(musicbrainz.MusicBrainzClient._not_before,
                           musicbrainz.time.time() + 50)

    def test_the_tail_of_a_burst_is_flushed(self):
        C = musicbrainz.MusicBrainzClient
        C._cache_saved = musicbrainz.time.time()          # debounce holds it
        self.mb._remember("u1", {"x": 1})
        self.assertFalse(C._cache_file.exists())
        C.flush()
        self.assertIn("u1", json.loads(C._cache_file.read_text()))
        self.assertFalse(C._cache_dirty)

    def test_aliases_in_any_script_are_offered(self):
        body = json.dumps({"artists": [{"id": "1", "name": "Filipp Kirkorov",
                                        "aliases": [{"name": "Филипп Киркоров"},
                                                    {"name": "Philipp Kirkorov"}]}]})
        with mock.patch.object(musicbrainz.urllib.request, "urlopen",
                               return_value=_Resp(body.encode())):
            out = self.mb.artist_aliases("Filipp Kirkorov")
        self.assertIn("Филипп Киркоров", out)


class Accents(unittest.TestCase):
    def test_only_decorative_marks_go(self):
        self.assertEqual(titlematch.strip_accents("Tiësto"), "Tiesto")
        self.assertEqual(titlematch.strip_accents("ドラゴン"), "ドラゴン")
        self.assertEqual(titlematch.strip_accents("हिन्दी"), "हिन्दी")


class Held(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "cfg" / "held.json"

    def tearDown(self):
        self.tmp.cleanup()

    def writes(self, store):
        n = [0]
        real = store._save_locked

        def counting():
            n[0] += 1
            real()
        store._save_locked = counting
        return n

    def test_a_pass_writes_once_and_a_timestamp_alone_writes_nothing(self):
        st = HeldStore(self.file)
        a = st.add("/downloads/A")
        b = st.add("/downloads/B")
        n = self.writes(st)
        with st.batch():
            st.update(a["id"], existing={"in_library": False, "_ts": 1})
            st.update(b["id"], existing={"in_library": False, "_ts": 1})
        self.assertEqual(n[0], 1)
        st.update(a["id"], existing={"in_library": False, "_ts": 2})
        self.assertEqual(n[0], 1)
        self.assertEqual(st.get(a["id"])["existing"]["_ts"], 2)

    def test_an_empty_mount_prunes_nothing(self):
        downloads = self.root / "downloads"
        downloads.mkdir()
        st = HeldStore(self.file)
        st.add(str(downloads / "Gone"))
        st.add(str(downloads / "Also gone" / "CD1"))
        self.assertEqual(st.prune_missing(roots=[downloads]), 0)   # empty: no answer
        self.assertEqual(len(st.list()), 2)
        (downloads / "Other").mkdir()                              # mounted
        self.assertEqual(st.prune_missing(roots=[downloads]), 2)

    def test_outside_every_root_the_parent_must_answer(self):
        st = HeldStore(self.file)
        st.add(str(self.root / "nowhere" / "X"))
        self.assertEqual(st.prune_missing(roots=[]), 0)

    def test_an_outage_summary_never_replaces_the_stored_one(self):
        from orchestrator import Orchestrator
        st = HeldStore(self.file)
        e = st.add(str(self.root))          # exists: not pruned
        st.update(e["id"], existing={"in_library": False, "n_audio": 3, "_ts": 0})
        o = Orchestrator.__new__(Orchestrator)
        o.held = st
        o.cfg = type("C", (), {"held_auto_resolve": False,
                               "webui_held_refresh_seconds": 300})()
        o.lidarr = type("L", (), {"failure_generation": 0})()
        o.expand_box_set = lambda it: None

        def summary(it):
            o.lidarr.failure_generation += 1
            return {"in_library": False}
        o.existing_album_summary = summary
        o.curate_held_pass()
        self.assertEqual(st.get(e["id"])["existing"]["n_audio"], 3)


class Qbt(unittest.TestCase):
    def test_one_client_per_process(self):
        import qbittorrent_client as qc
        qc._shared.clear()
        self.assertIs(qc.shared("http://q:8080/", "u", "p"), qc.shared("http://q:8080", "u", "p"))

    def test_a_403_logs_in_again_and_repeats_once(self):
        import qbittorrent_client as qc
        q = qc.QbtClient("http://q")
        seq = [403, 200]
        R = lambda code: type("R", (), {"status_code": code})()
        q.s.raw = type("S", (), {"get": lambda self, url, **k: R(seq.pop(0))})()
        relog = []
        q._relogin = lambda: relog.append(1) or True
        self.assertEqual(q.s.get("http://q/api/v2/torrents/info").status_code, 200)
        self.assertEqual(relog, [1])

    def test_the_auth_mode_is_logged_once(self):
        import qbittorrent_client as qc
        q = qc.QbtClient("http://q")
        q._api_ok = lambda: True
        with self.assertLogs("qbittorrent", "INFO") as cm:
            q.login()
            q.login()
            qc.logger.info("end")
        self.assertEqual(sum("authorized without login" in m for m in cm.output), 1)


if __name__ == "__main__":
    unittest.main()
