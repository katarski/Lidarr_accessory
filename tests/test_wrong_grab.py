"""A torrent grabbed for album X whose songs are not X, and that no record
Lidarr could take is either, is rejected: blocklisted in Lidarr (which then
searches X again), removed from qBittorrent with its files, and what it left
behind deleted. The owner, 31 Oct 2026: "Blocklist, delete, re-search"."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class FakeLidarr:
    def __init__(self):
        self.removed = []

    def queue_list(self):
        return [{"id": 5, "downloadId": "ABC"}]

    def queue_remove(self, qid, remove_from_client=False, blocklist=False):
        self.removed.append((qid, remove_from_client, blocklist))
        return True


class WrongGrab(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wr = Path(self.tmp.name) / "downloads"
        self.root = self.wr / "Some Torrent"
        (self.root / "CD-1").mkdir(parents=True)
        self.audios = []
        for i in (1, 2):
            p = self.root / "CD-1" / ("%02d - Song.flac" % i)
            p.write_bytes(b"x")
            self.audios.append(p)
        (self.root / "cover.jpg").write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def orch(self, cov=(0.1, 1, 10)):
        o = Orchestrator.__new__(Orchestrator)
        o.lidarr = FakeLidarr()
        o.cfg = SimpleNamespace(watch_root=self.wr, audio_extensions=[".flac"],
                                verify_track_titles_accept=0.6)
        o._skip_seen = set()
        o.recorded = []
        o._record = lambda *a, **k: o.recorded.append(k.get("reason"))
        o._best_release_title_coverage = lambda files, aid: cov
        o._grab_target_for_folder = lambda f: {
            "album_id": 9, "title": "Wanted Album", "hash": "abc",
            "root": str(self.root), "source": "recorded"}
        return o

    def test_rejected_blocklisted_and_deleted(self):
        o = self.orch()
        self.assertTrue(o._reject_wrong_grab(self.root, self.root, self.audios, "r"))
        self.assertEqual(o.lidarr.removed, [(5, True, True)])
        self.assertFalse(self.root.exists())

    def test_songs_that_agree_are_not_rejected(self):
        o = self.orch(cov=(0.9, 9, 10))
        self.assertFalse(o._reject_wrong_grab(self.root, self.root, self.audios, "r"))
        self.assertTrue(self.root.exists())
        self.assertEqual(o.lidarr.removed, [])

    def test_a_torrent_holding_more_than_this_folder_is_kept(self):
        extra = self.root / "Other Album" / "01.flac"
        extra.parent.mkdir()
        extra.write_bytes(b"x")
        o = self.orch()
        self.assertFalse(o._reject_wrong_grab(
            self.root / "CD-1", self.root / "CD-1", self.audios, "r"))
        self.assertTrue(extra.exists())
        self.assertEqual(o.lidarr.removed, [])


if __name__ == "__main__":
    unittest.main()
