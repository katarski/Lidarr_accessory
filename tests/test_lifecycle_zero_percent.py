"""Finished torrents that qBittorrent shows at 0 % belong to the lifecycle.

The owner, 1 Nov 2026: "I see torrents with 0 perceent taht are also seeding.
why are they not removed?" Two kinds completed once and then read 0 %:

  * every file deselected: qBittorrent counts the torrent finished at once and
    "seeds" it at 0 % (18 such torrents on 1 Nov);
  * missingFiles: the pipeline moved or deleted files of it (32 on 1 Nov, 31
    with their folder still there).

The lifecycle skipped both (progress < 1.0 read as "still downloading"), and
the dead-grab reaper took them for grabs that never started: 24 h after the
add it deleted them with their data, unread, and blocklisted the release."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbt_deselect as QD  # noqa: E402

NAME = "Stevie Wonder - The Definitive Collection (2002) (2 CD) [FLAC]"
NOW = 1_800_000_000.0
DAY = 86400.0


class _Qbt:
    def __init__(self, torrents, files_by_hash):
        self._t, self._f = list(torrents), dict(files_by_hash)
        self.removed, self.paused = [], []
        self.refuse = False

    def torrents(self, category=""):
        return [t for t in self._t
                if not category or t.get("category") == category]

    def files(self, h):
        return self._f.get(h, [])

    def remove(self, h, delete_files=False):
        if self.refuse:
            return False
        self.removed.append((h, delete_files))
        return True

    def pause(self, h):
        self.paused.append(h)
        return True


class _Lidarr:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.blocklisted = []

    def queue_list(self):
        return list(self.rows)

    def queue_remove(self, qid, remove_from_client=True, blocklist=False):
        self.blocklisted.append((qid, remove_from_client, blocklist))
        return True


def torrent(h="h1", name=NAME, state="forcedUP", progress=0, size=0,
            completion_on=NOW - DAY, added_on=NOW - 3 * DAY, category="lidarr"):
    return {"hash": h, "name": name, "state": state, "progress": progress,
            "size": size, "completion_on": completion_on,
            "added_on": added_on, "content_path": "/data/" + name,
            "save_path": "/data", "category": category,
            "num_seeds": 0, "num_complete": 0, "last_activity": NOW - 9 * DAY}


def album_files(prefix=NAME, priority=0, extra=("folder.jpg", "album.cue")):
    names = ["CD1/01 - Track.flac", "CD1/02 - Track.flac"] + list(extra)
    return [{"name": prefix + "/" + n, "index": i, "priority": priority,
             "progress": 0} for i, n in enumerate(names)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "other.txt").write_text("keeps the root non-empty")
        self.folder = self.root / NAME
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *names):
        for n in names:
            f = self.folder / n
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")

    def run_pass(self, q, lidarr=None, plan=None, owned=(False, "not owned"),
                 **kw):
        lidarr = lidarr if lidarr is not None else _Lidarr()

        def no_plan(*a, **k):
            if plan is None:
                raise AssertionError("not planned in this case")
            return plan
        with mock.patch.object(QD, "plan_torrent", no_plan), \
                mock.patch.object(QD, "_songs_on_disk_owned",
                                  lambda *a, **k: owned):
            return QD.torrent_lifecycle_pass(
                q, str(self.root), category="lidarr", emit=self.lines.append,
                lidarr=lidarr, min_stable_seconds=kw.pop("min_stable", 0),
                now=NOW, **kw)


class Owner(unittest.TestCase):
    def test_who_owns_what(self):
        own = QD._lifecycle_owns
        self.assertTrue(own(torrent(progress=1, size=5, completion_on=0)))
        self.assertTrue(own(torrent()))                         # nothing selected
        self.assertTrue(own(torrent(state="stoppedUP")))
        self.assertTrue(own(torrent(state="missingFiles", size=9)))
        self.assertFalse(own(torrent(completion_on=0)))         # never finished
        self.assertFalse(own(torrent(state="missingFiles", completion_on=-1)))
        self.assertFalse(own(torrent(state="stalledDL", size=9)))
        self.assertFalse(own(torrent(state="forcedMetaDL")))
        self.assertFalse(own(torrent(state="checkingUP")))
        t = torrent()
        del t["size"]                                           # unknown size
        self.assertFalse(own(t))


class NothingSelected(Base):
    def test_partial_files_only_removes_it_with_its_data_and_blocklists(self):
        self.make("CD1/01 - Track.flac.!qB", "CD1/02 - Track.flac.!qB",
                  "folder.jpg.!qB")
        lid = _Lidarr([{"id": 7, "downloadId": "H1"}, {"id": 8, "downloadId": "x"}])
        q = _Qbt([torrent()], {"h1": album_files()})
        self.assertEqual(self.run_pass(q, lidarr=lid), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertEqual(lid.blocklisted, [(7, False, True)])
        self.assertEqual(self.lines, [
            "lifecycle: REMOVED (every file deselected; its partial files and "
            "extras deleted; blocklisted in Lidarr) %r" % NAME])

    def test_an_empty_folder_is_nothing_left(self):
        self.folder.mkdir()
        q = _Qbt([torrent()], {"h1": album_files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertNotIn("blocklisted", self.lines[0])        # no queue row

    def test_a_finished_song_on_disk_keeps_it(self):
        for kept in ("CD1/01 - Track.flac", "CD1/disc.iso"):
            self.tearDown()
            self.setUp()
            self.make(kept, "CD1/02 - Track.flac.!qB")
            lid = _Lidarr([{"id": 7, "downloadId": "h1"}])
            q = _Qbt([torrent()], {"h1": album_files()})
            self.assertEqual(self.run_pass(q, lidarr=lid), (0, 0), kept)
            self.assertEqual((q.removed, q.paused, lid.blocklisted),
                             ([], [], []), kept)

    def test_no_folder_but_its_part_file_removes_it_with_its_data(self):
        (self.root / ".h1.parts").write_bytes(b"p")
        q = _Qbt([torrent()], {"h1": album_files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertIn("its part file deleted", self.lines[0])

    def test_nothing_on_disk_removes_the_entry_only(self):
        q = _Qbt([torrent()], {"h1": album_files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", False)])
        self.assertIn("removed torrent entry only", self.lines[0])

    def test_just_deselected_waits_for_the_next_pass(self):
        q = _Qbt([torrent(completion_on=NOW - 60)], {"h1": album_files()})
        self.assertEqual(self.run_pass(q, min_stable=300), (0, 0))
        self.assertEqual(q.removed, [])
        q = _Qbt([torrent(completion_on=NOW - 600)], {"h1": album_files()})
        self.assertEqual(self.run_pass(q, min_stable=300), (1, 0))

    def test_a_torrent_with_no_music_is_still_ignored(self):
        files = [{"name": NAME + "/clip.mkv", "priority": 0},
                 {"name": NAME + "/cover.jpg", "priority": 0}]
        q = _Qbt([torrent()], {"h1": files})
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(q.removed, [])

    def test_an_extra_still_selected_is_not_nothing_selected(self):
        files = album_files()
        files[-1]["priority"] = 1                               # the .cue
        q = _Qbt([torrent(progress=1, size=10)], {"h1": files})
        self.assertEqual(self.run_pass(q), (0, 0))              # as before
        self.assertEqual(q.removed, [])

    def test_a_removal_the_client_refuses_is_not_counted(self):
        self.folder.mkdir()
        q = _Qbt([torrent()], {"h1": album_files()})
        q.refuse = True
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(self.lines, [])

    def test_downloading_at_zero_is_still_the_deselect_pass_s(self):
        q = _Qbt([torrent(state="stalledDL", size=500, completion_on=0)],
                 {"h1": album_files(priority=1)})
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(q.removed, [])


class MissingFiles(Base):
    def missing(self, **kw):
        return torrent(state="missingFiles", size=500, **kw)

    def test_audio_gone_removes_it_with_its_data(self):
        self.make("album.cue", "folder.jpg")                   # songs imported
        q = _Qbt([self.missing()], {"h1": album_files(priority=1)})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertEqual(self.lines,
                         ["lifecycle: REMOVED (fully imported) %r" % NAME])

    def test_songs_not_imported_stay_and_are_not_paused(self):
        self.make("CD1/01 - Track.flac", "album.cue")          # one moved out
        plan = [{"artist_id": 1, "owned": False, "files": []}]
        q = _Qbt([self.missing()], {"h1": album_files(priority=1)})
        self.assertEqual(self.run_pass(q, plan=plan), (0, 0))
        self.assertEqual((q.removed, q.paused), ([], []))
        self.assertIn("NOT removing", self.lines[0])

    def test_songs_the_library_holds_remove_it(self):
        self.make("CD1/01 - Track.flac")
        plan = [{"artist_id": 1, "owned": True, "files": []}]
        q = _Qbt([self.missing()], {"h1": album_files(priority=1)})
        self.assertEqual(
            self.run_pass(q, plan=plan, owned=(True, "all in the library")),
            (1, 0))
        self.assertEqual(q.removed, [("h1", True)])

    def test_no_folder_removes_the_entry_only(self):
        q = _Qbt([self.missing()], {"h1": album_files(priority=1)})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", False)])

    def test_never_completed_is_left_alone(self):
        self.make("album.cue")
        q = _Qbt([self.missing(completion_on=0)], {"h1": album_files(priority=1)})
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(q.removed, [])


class Reapers(unittest.TestCase):
    def reap(self, t):
        q = _Qbt([t], {t["hash"]: album_files()})
        lid = _Lidarr([{"id": 3, "downloadId": t["hash"]}])
        lines = []
        n = QD.dead_grab_reaper_pass(q, lid, category="lidarr",
                                     grace_seconds=DAY, emit=lines.append,
                                     now=NOW)
        return n, q.removed, lid.blocklisted

    def test_the_dead_grab_reaper_leaves_what_completed_once(self):
        for t in (torrent(), torrent(state="stalledUP"),
                  torrent(state="missingFiles", size=500)):
            self.assertEqual(self.reap(t), (0, [], []), t["state"])

    def test_a_real_dead_grab_is_still_reaped(self):
        for t in (torrent(state="stalledDL", size=500, completion_on=0),
                  torrent(state="missingFiles", size=500, completion_on=0),
                  torrent(state="forcedMetaDL", completion_on=0)):
            self.assertEqual(self.reap(t), (1, [("h1", True)], [(3, False, True)]),
                             t["state"])

    def test_the_stalled_reaper_leaves_a_completed_missing_files_torrent(self):
        t = torrent(state="missingFiles", size=500, progress=0.5)
        q = _Qbt([t], {"h1": album_files(priority=1)})
        lines = []
        out = QD.stalled_grab_reaper_pass(q, None, category="lidarr",
                                          grace_seconds=DAY, emit=lines.append,
                                          now=NOW)
        self.assertEqual(out, (0, 0))
        self.assertEqual((q.removed, lines), ([], []))


if __name__ == "__main__":
    unittest.main()
