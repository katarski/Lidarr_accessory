"""9 Nov 2026: what put 0 B torrents, a Seal best-of and unforced downloads
in qBittorrent.

  * 'Seal - Best 1991-2004' passed the compilation filter (Best + years);
  * discographies were accepted without holding any missing album (Arielle
    Dombasle 7 CD for 'Iconics', the Trammps DJ discography for two
    Christmas albums), so every folder was deselected;
  * a torrent with only a .txt still selected was never removed;
  * Lidarr's Initial State (Force Start) was not applied to what the
    pipeline added or adopted.
"""
import time
import unittest
from unittest import mock

from orchestrator import Orchestrator
from qbittorrent_client import QbtClient
import qbt_deselect
from lidarr import LidarrClient


class CompilationTitles(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)

    def test_best_and_years(self):
        r = self.o._compilation_reason
        self.assertTrue(r("Best 1991-2004"))
        self.assertTrue(r("[DTSCD][DVDA] Seal - Best 1991-2004 - 2005 (Rock, Dance-Pop)"))
        self.assertTrue(r("Best"))
        for t in ("Best I Ever Had", "Best Friend", "Second Best", "Togetherland"):
            self.assertIsNone(r(t), t)

    def test_the_deselect_drops_it_too(self):
        u = qbt_deselect._album_looks_unofficial
        self.assertTrue(u("Best 1991-2004"))
        self.assertFalse(u("Best I Ever Had"))
        self.assertFalse(u("Seal II"))


class _Qbt:
    def __init__(self, files):
        self._files = files

    def torrent_by_hash(self, h):
        return {"hash": h}


class DiscographyHoldsAWantedAlbum(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)

    def _verify(self, names, missing):
        files = [{"name": n, "size": 1, "priority": 1} for n in names]
        with mock.patch.object(Orchestrator, "_await_torrent_files",
                               return_value=files):
            return self.o._verify_discography(object(), "h", "T", missing=missing)

    def test_none_of_the_missing_albums_rejects(self):
        names = ["Disco/1977 - Disco Inferno/01 - Disco Inferno.mp3",
                 "Disco/1978 - The Whole World's Dancing/01 - A.mp3",
                 "Disco/1980 - Mixin' It Up/01 - B.mp3"]
        v, n = self._verify(names, [("Soul Christmas with the Trammps", 10, 1),
                                    ("The Trammps Christmas", 12, 2)])
        self.assertEqual(v, "reject:no-wanted-album")
        self.assertEqual(n, 3)

    def test_a_missing_album_among_them_accepts(self):
        names = ["Disco/1977 - Disco Inferno/01 - Disco Inferno.mp3",
                 "Disco/2009 - The Trammps Christmas [2CD]/01 - A.mp3"]
        v, _n = self._verify(names, [("The Trammps Christmas", 12, 2)])
        self.assertEqual(v, "accept")

    def test_no_missing_list_keeps_the_old_rule(self):
        names = ["D/A/01.mp3", "D/B/01.mp3"]
        self.assertEqual(self._verify(names, None)[0], "accept")
        self.assertEqual(self._verify(["D/A/01.mp3"], None)[0],
                         "reject:single-album")


class JunkOnlySelection(unittest.TestCase):
    def test_a_selected_txt_does_not_keep_the_torrent(self):
        files = [{"name": "D/A/01.mp3", "priority": 0},
                 {"name": "D/Torrent_downloaded_from_Demonoid.is_.txt",
                  "priority": 1}]
        self.assertTrue(qbt_deselect._every_file_deselected_music(files))

    def test_a_selected_song_does(self):
        files = [{"name": "D/A/01.mp3", "priority": 1},
                 {"name": "D/info.txt", "priority": 1}]
        self.assertFalse(qbt_deselect._every_file_deselected_music(files))

    def test_no_music_at_all_is_not_ours(self):
        files = [{"name": "D/info.txt", "priority": 0}]
        self.assertFalse(qbt_deselect._every_file_deselected_music(files))


class _MirrorQbt:
    def __init__(self, torrents):
        self._t = torrents
        self.forced = []

    def torrents(self, category="", **_kw):
        return [t for t in self._t if t.get("category") == category]

    def force_start(self, h, value=True):
        self.forced.append(h)
        return True


class MirrorInitialState(unittest.TestCase):
    def _qbt(self):
        tag = QbtClient.SELF_ADDED_TAG
        return _MirrorQbt([
            {"hash": "a", "category": "lidarr", "state": "downloading"},
            {"hash": "b", "category": "lidarr", "state": "stalledDL"},
            {"hash": "c", "category": "lidarr", "state": "forcedDL",
             "force_start": True},
            {"hash": "d", "category": "lidarr", "state": "stoppedDL"},
            {"hash": "e", "category": "lidarr", "state": "metaDL", "tags": tag},
            {"hash": "f", "category": "tv", "state": "downloading"},
            {"hash": "g", "category": "lidarr", "state": "stalledUP"},
        ])

    def test_force_start_is_mirrored_on_running_torrents_only(self):
        q = self._qbt()
        n = qbt_deselect.mirror_initial_state(q, "lidarr", "force",
                                              emit=lambda *_: None)
        self.assertEqual(q.forced, ["a", "b", "g"])
        self.assertEqual(n, 3)

    def test_other_states_do_nothing(self):
        for st in ("start", "stop", None):
            q = self._qbt()
            self.assertEqual(qbt_deselect.mirror_initial_state(
                q, "lidarr", st, emit=lambda *_: None), 0)
            self.assertEqual(q.forced, [])


class LidarrInitialState(unittest.TestCase):
    def _client(self, clients):
        c = LidarrClient.__new__(LidarrClient)
        c._get = lambda path, **_p: clients
        return c

    @staticmethod
    def _dc(state, cat="lidarr", enable=True):
        return {"enable": enable, "implementation": "QBittorrent",
                "fields": [{"name": "musicCategory", "value": cat},
                           {"name": "initialState", "value": state}]}

    def test_reads_the_category_client(self):
        c = self._client([self._dc(0, "other"), self._dc(1, "lidarr")])
        self.assertEqual(c.qbit_initial_state("lidarr"), "force")

    def test_values(self):
        for v, want in ((0, "start"), (1, "force"), (2, "stop"), (9, None)):
            c = self._client([self._dc(v)])
            self.assertEqual(c.qbit_initial_state("lidarr"), want)

    def test_disabled_client_is_ignored(self):
        c = self._client([self._dc(1, enable=False)])
        self.assertIsNone(c.qbit_initial_state("lidarr"))


class RejectWaitsForTheQueueRow(unittest.TestCase):
    def test_returns_once_the_row_is_there(self):
        o = Orchestrator.__new__(Orchestrator)
        calls = {"n": 0}

        class L:
            def queue_list(self_inner):
                calls["n"] += 1
                return [{"downloadId": "ABC"}] if calls["n"] >= 2 else []
        o.lidarr = L()
        with mock.patch.object(time, "sleep", lambda *_: None):
            self.assertTrue(o._await_queue_row("abc", timeout=30))
        self.assertEqual(calls["n"], 2)

    def test_gives_up_after_the_timeout(self):
        o = Orchestrator.__new__(Orchestrator)

        class L:
            def queue_list(self_inner):
                return []
        o.lidarr = L()
        self.assertFalse(o._await_queue_row("abc", timeout=0))


if __name__ == "__main__":
    unittest.main()
