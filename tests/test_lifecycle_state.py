"""The torrent-lifecycle re-check state survives a restart (loops F10): it
lived only in memory, so every deploy re-planned every completed torrent."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import qbt_deselect as QD  # noqa: E402


class _Qbt:
    def __init__(self, torrents, files):
        self._t, self._f = torrents, files

    def torrents(self, category=""):
        return self._t

    def files(self, h):
        return self._f

    def remove(self, h, delete_files=False):
        return True

    def pause(self, h):
        return True


class LifecycleStateSurvivesARestart(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.path = self.dir / "lifecycle_state.json"

    def test_a_checked_torrent_is_not_replanned_after_a_restart(self):
        root = self.dir / "dl"
        folder = root / "Album"
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"x")
        old = time.time() - 3600
        os.utime(folder / "01.flac", (old, old))
        qbt = _Qbt([{"hash": "h1", "progress": 1, "name": "Album",
                     "content_path": str(folder), "save_path": str(root),
                     "state": "stalledUP"}],
                   [{"name": "Album/01.flac", "priority": 1},
                    {"name": "Album/02.flac", "priority": 1}])
        plans = []

        def plan(*a, **k):
            plans.append(1)
            return {"albums": []}

        def run(checked, seen):
            with mock.patch.object(QD, "plan_torrent", plan), \
                 mock.patch.object(QD, "plan_is_deferred", lambda p: False), \
                 mock.patch.object(QD, "_songs_on_disk_owned",
                                   lambda *a: (False, "kept")):
                QD.torrent_lifecycle_pass(
                    qbt, str(root), lidarr=object(), min_stable_seconds=0,
                    completed_seen=seen, checked=checked,
                    on_complete=lambda f: None)

        checked, seen = main._load_lifecycle_state(self.path)
        run(checked, seen)
        self.assertEqual(len(plans), 1)
        main._save_lifecycle_state(self.path, checked, seen)
        checked, seen = main._load_lifecycle_state(self.path)   # a restart
        self.assertIn("h1", seen)
        run(checked, seen)
        self.assertEqual(len(plans), 1)                          # not re-planned

    def test_old_entries_are_pruned_and_a_waiting_stamp_is_kept(self):
        now = time.time()
        checked = {"old": ((1, 1), now - 40 * 86400),
                   "waiting": ((1, 1), -now),
                   "fresh": ((2, 5), now)}
        seen = main._StampedSet()
        seen["gone"] = now - 40 * 86400
        seen.add("new")
        main._save_lifecycle_state(self.path, checked, seen)
        c2, s2 = main._load_lifecycle_state(self.path)
        self.assertEqual(set(c2), {"waiting", "fresh"})
        self.assertEqual(c2["fresh"][0], (2, 5))
        self.assertEqual(set(s2), {"new"})

    def test_the_pass_checkpoints_as_it_goes(self):
        root = self.dir / "dl"
        (root / "x").mkdir(parents=True)
        qbt = _Qbt([{"hash": "h%d" % i, "progress": 1} for i in range(45)], [])
        ticks = []
        QD.torrent_lifecycle_pass(qbt, str(root), checked={},
                                  on_progress=lambda: ticks.append(1))
        self.assertEqual(len(ticks), 2)


if __name__ == "__main__":
    unittest.main()
