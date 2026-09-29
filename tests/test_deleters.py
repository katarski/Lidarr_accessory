"""The passes that delete torrent data act only on proof: Lidarr owns every
song file by file, the import has landed, the torrent is ours and is exactly
this folder. Deselected, unknown, submitted or unreadable is never proof."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads  # noqa: E402
import qbt_deselect as QD  # noqa: E402
import song_harvest as SH  # noqa: E402
from qbittorrent_client import QbtClient  # noqa: E402


class _Qbt:
    def __init__(self, torrents=(), files=()):
        self._t, self._f = list(torrents), list(files)
        self.removed, self.categorised = [], []

    def torrents(self, category=""):
        return [t for t in self._t
                if not category or t.get("category") == category]

    def files(self, h):
        return self._f

    def remove(self, h, delete_files=False):
        self.removed.append((h, delete_files))
        return True

    def set_file_priority(self, h, idx, prio):
        return True

    def set_category(self, h, cat):
        self.categorised.append(h)
        return True

    def pause(self, h):
        return True


def _entry(name, owned, total=10, artist_id=5):
    return {"artist": "A", "album": "B", "files": [{"name": name, "index": 0}],
            "all_files": [{"name": name, "index": 0}], "size": 1, "have": True,
            "owned": owned, "have_count": total if owned else 0,
            "total": total if owned else 0, "artist_id": artist_id}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "Alb"))
        Path(self.root, "Alb", "01.flac").write_bytes(b"x")
        Path(self.root, "other.txt").write_text("keep the root non-empty")
        self.t = {"hash": "h1", "name": "Alb", "progress": 1, "state": "stalledUP",
                  "content_path": "/data/Alb", "save_path": "/data",
                  "category": "lidarr"}
        self.q = _Qbt([self.t], [{"name": "Alb/01.flac", "index": 0, "priority": 1}])

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, plan, owned_verdict):
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: plan), \
             mock.patch.object(dedup_downloads, "folder_fully_owned",
                               lambda *a, **k: owned_verdict):
            QD.torrent_lifecycle_pass(self.q, self.root, category="lidarr",
                                      lidarr=object(), min_stable_seconds=0,
                                      now=time.time() + 1000)
        return self.q.removed

    def test_an_album_lidarr_does_not_know_keeps_its_data(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=False)],
                                       (True, "")), [])

    def test_owned_by_count_but_not_file_by_file_keeps_its_data(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=True)],
                                       (False, "01.flac would be imported")), [])

    def test_every_song_owned_file_by_file_is_removed(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=True)],
                                       (True, "")), [("h1", True)])


class Reap(unittest.TestCase):
    def test_deselected_is_not_owned(self):
        f = [{"name": "T/x.flac", "index": 0, "priority": 1}]
        for owned, want in ((False, []), (True, [("h", True)])):
            q = _Qbt(files=f)
            with mock.patch.object(QD, "plan_torrent",
                                   lambda *a, **k: [_entry("T/x.flac", owned)]):
                QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                                   apply=True, reap_useless=True,
                                   emit=lambda s: None)
            self.assertEqual(q.removed, want)


class Adopt(unittest.TestCase):
    def test_only_our_own_torrents_are_adopted(self):
        mine = {"hash": "a", "category": "", "tags": QbtClient.SELF_ADDED_TAG}
        theirs = {"hash": "b", "category": "", "tags": ""}
        q = _Qbt([mine, theirs], [{"name": "x.flac"}])
        QD.adopt_uncategorised(q, "lidarr", emit=lambda s: None)
        self.assertEqual(q.categorised, ["a"])


class _Lidarr:
    failure_generation = 0

    def __init__(self, status, filed):
        self.status, self.filed = status, filed

    def command_status(self, cid):
        return self.status

    def list_tracks_for_album(self, aid):
        return [{"id": 100, "hasFile": self.filed}]


class HarvestPurge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "Box")
        os.makedirs(self.src)
        self.song = os.path.join(self.src, "01.flac")
        Path(self.song).write_bytes(b"x")
        Path(self.src, "cover.jpg").write_bytes(b"x")
        self.led = SH.HarvestLedger(os.path.join(self.tmp.name, "led.json"))
        self.led.add_pending(self.src, {"cid": 1, "mode": "copy",
                                        "paths": [self.song],
                                        "tracks": {"10": [100]}})
        self.q = _Qbt([
            {"hash": "box", "name": "Box", "category": "lidarr",
             "content_path": "/data/Box"},
            {"hash": "disco", "name": "Disco", "category": "lidarr",
             "content_path": "/data/Disco"}])

    def tearDown(self):
        self.tmp.cleanup()

    def settle(self, lidarr):
        return SH.settle_pending_purges(lidarr, self.led, {}, qbt=self.q,
                                        category="lidarr")

    def test_a_running_import_purges_nothing_and_is_remembered(self):
        self.settle(_Lidarr("started", False))
        self.assertTrue(os.path.exists(os.path.join(self.src, "cover.jpg")))
        self.assertIn(self.src, SH.HarvestLedger(self.led.path).pending)

    def test_a_track_left_without_a_file_purges_nothing(self):
        self.settle(_Lidarr("completed", False))
        self.assertEqual(self.q.removed, [])
        self.assertEqual(self.led.pending, {})

    def test_a_landed_import_removes_exactly_this_torrent(self):
        self.settle(_Lidarr("completed", True))
        self.assertFalse(os.path.exists(os.path.join(self.src, "cover.jpg")))
        self.assertEqual(self.q.removed, [("box", True)])

    def test_a_song_nobody_proved_keeps_the_folder_and_its_torrent(self):
        Path(self.src, "02.flac").write_bytes(b"x")
        none = type("R", (), {"matches": []})()
        with mock.patch.object(SH, "read_tags", lambda p: None), \
             mock.patch.object(SH, "match_files", lambda *a, **k: none):
            self.settle(_Lidarr("completed", True))
        self.assertTrue(os.path.exists(os.path.join(self.src, "02.flac")))
        self.assertEqual(self.q.removed, [])


class Discard(unittest.TestCase):
    def test_an_unreadable_file_list_or_another_albums_data_reaps_nothing(self):
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wr = str(Path(tmp.name).resolve()).replace("\\", "/")
        t = {"hash": "d", "name": "Disco", "content_path": wr + "/Disco"}
        this = {"name": "Disco/Album/01.flac", "index": 0, "priority": 1}
        other = {"name": "Disco/Other/01.flac", "index": 1, "priority": 0,
                 "progress": 0.4}
        reaped = []
        for files, want in (([], []), ([this, other], []), ([this], ["d"])):
            q = _Qbt(files=files)
            o = Orchestrator.__new__(Orchestrator)
            o.cfg = type("C", (), {"watch_root": Path(tmp.name)})()
            o._get_qbt = lambda q=q: q
            o._qbt_ours = lambda t: True
            reaped.clear()
            o._reap_torrent = lambda h, blocklist=True: reaped.append(h) or True
            o._deselect_album_in_torrent(t, Path(wr + "/Disco/Album"),
                                         reap_if_last=True)
            self.assertEqual(reaped, want)


class GrabBinding(unittest.TestCase):
    def orch(self, rows):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        removed = []
        o.lidarr = type("L", (), {
            "failure_generation": 0,
            "queue_list": lambda self: rows,
            "queue_remove": lambda self, i, **k: removed.append(i) or True})()
        o._lidarr_generation = lambda: 0
        return o, removed

    def test_another_downloads_title_containing_the_album_is_not_this_grab(self):
        live = {"id": 1, "downloadId": "F3A3", "title": "Confidence Man - 5AM (LA LA LA)",
                "albumId": 77}
        o, _ = self.orch([live])
        before = o._queue_download_ids()
        with mock.patch("orchestrator.time.sleep", lambda s: None):
            self.assertEqual(o._await_grab("Priscilla Ahn - La La La", album_id=5,
                                           before=before, timeout=0), (None, None))
            mine = {"id": 2, "downloadId": "3B55", "title": "Priscilla Ahn - La La La",
                    "albumId": 5}
            o.lidarr.queue_list = lambda: [live, mine]
            self.assertEqual(o._await_grab("x", album_id=5, before=before,
                                           timeout=5)[0], "3b55")

    def test_a_reject_removes_only_the_row_with_its_hash(self):
        rows = [{"id": 1, "downloadId": "AAA", "title": "Other - La La La"},
                {"id": 2, "downloadId": "BBB", "title": "Mine"}]
        o, removed = self.orch(rows)
        o._qbt_ours_by_hash = lambda q, h: False
        o._reject_grab("Priscilla Ahn", "La La La", "bbb", None)
        self.assertEqual(removed, [2])


if __name__ == "__main__":
    unittest.main()
