"""A pure disc-image torrent leaves the client once its image is spent.

The owner, 31 Oct 2026, about Lara Fabian's '9': "once moved - everything
associated with the download - had to be deleted". The torrent is an SACD rip:
the .iso plus .md5/.sfv/folder.jpeg/sacd_log.txt. A .iso is not audio, so the
lifecycle pass counted no audio in it, took it for "not a music torrent" and
skipped it for ever -- the torrent and its extras sat in the client after the
pipeline had extracted the disc and imported its tracks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbt_deselect as QD  # noqa: E402

NAME = "LARA FABIAN - 9 (2005) [SACD] (ISO)"
EXTRAS = ("FABIAN LARA - 9 CD1.md5", "FABIAN LARA - 9 CD1.sfv",
          "folder.jpeg", "sacd_log.txt")


class _Qbt:
    def __init__(self, torrents, files_by_hash):
        self._t, self._f = list(torrents), dict(files_by_hash)
        self.removed, self.paused = [], []

    def torrents(self, category=""):
        return [t for t in self._t
                if not category or t.get("category") == category]

    def files(self, h):
        return self._f.get(h, [])

    def remove(self, h, delete_files=False):
        self.removed.append((h, delete_files))
        return True

    def pause(self, h):
        self.paused.append(h)
        return True


def torrent(name=NAME, h="h1", category="lidarr", progress=1):
    return {"hash": h, "name": name, "progress": progress, "state": "stalledUP",
            "content_path": "/data/" + name, "save_path": "/data",
            "category": category}


def sacd_files(prefix=NAME, iso_priority=1):
    return ([{"name": prefix + "/FABIAN LARA - 9 CD1.iso",
              "priority": iso_priority}]
            + [{"name": prefix + "/" + n, "priority": 1} for n in EXTRAS])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "other.txt").write_text("keeps the root non-empty")
        self.folder = self.root / NAME
        self.lines, self.completed, self.seen = [], [], set()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *names, where=None):
        where = where or self.folder
        where.mkdir(parents=True, exist_ok=True)
        for n in names:
            f = where / n
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")

    def run_pass(self, q, **kw):
        def no_plan(*a, **k):
            raise AssertionError("a disc-image torrent is never planned")
        with mock.patch.object(QD, "plan_torrent", no_plan):
            return QD.torrent_lifecycle_pass(
                q, str(self.root), category="lidarr", emit=self.lines.append,
                lidarr=kw.pop("lidarr", object()), min_stable_seconds=0,
                on_complete=self.completed.append, completed_seen=self.seen,
                **kw)


class DiscImageTorrent(Base):
    def test_the_image_still_on_disk_keeps_the_torrent(self):
        self.make("FABIAN LARA - 9 CD1.iso", *EXTRAS)
        q = _Qbt([torrent()], {"h1": sacd_files()})
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual((q.removed, q.paused), ([], []))

    def test_the_tracks_it_made_waiting_for_the_import_keep_the_torrent(self):
        for audio in ("01 - TRACK.dsf", "01 - TRACK.dff", "01 - TRACK.flac",
                      "CD1/01 - TRACK.flac"):
            self.tearDown()
            self.setUp()
            self.make(audio, *EXTRAS)               # the image is already gone
            q = _Qbt([torrent()], {"h1": sacd_files()})
            self.assertEqual(self.run_pass(q), (0, 0), audio)
            self.assertEqual(q.removed, [], audio)

    def test_only_extras_left_removes_the_torrent_with_its_data(self):
        self.make(*EXTRAS)                          # image gone, tracks imported
        q = _Qbt([torrent()], {"h1": sacd_files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertEqual(self.lines, [
            "lifecycle: REMOVED (disc image spent, its tracks imported) %r"
            % NAME])

    def test_an_empty_folder_counts_as_nothing_left(self):
        self.folder.mkdir()
        q = _Qbt([torrent()], {"h1": sacd_files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])

    def test_a_folder_that_is_not_visible_loses_its_entry_never_its_data(self):
        q = _Qbt([torrent()], {"h1": sacd_files()})   # no folder under the root
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", False)])
        self.assertIn("removed torrent entry only", self.lines[0])
        self.assertIn(repr(NAME), self.lines[0])

    def test_no_hook_no_pause_no_plan_for_a_disc_image_torrent(self):
        for setup in (lambda: self.make("FABIAN LARA - 9 CD1.iso", *EXTRAS),
                      lambda: self.make(*EXTRAS), lambda: None):
            self.tearDown()
            self.setUp()
            setup()
            q = _Qbt([torrent()], {"h1": sacd_files()})
            self.run_pass(q)                        # no_plan raises if planned
            self.assertEqual(q.paused, [])
            self.assertEqual(self.completed, [])
            self.assertEqual(self.seen, set())

    def test_the_extension_is_read_in_any_case(self):
        self.make("DISC.ISO", *EXTRAS)
        files = [{"name": NAME + "/DISC.ISO", "priority": 1}]
        q = _Qbt([torrent()], {"h1": files})
        self.assertEqual(self.run_pass(q), (0, 0))   # an image: counted, on disk
        (self.folder / "DISC.ISO").unlink()
        self.assertEqual(self.run_pass(q), (1, 0))

    def test_a_deselected_image_is_not_a_disc_torrent(self):
        q = _Qbt([torrent()], {"h1": sacd_files(iso_priority=0)})
        self.assertEqual(self.run_pass(q), (0, 0))   # as before: ignored
        self.assertEqual(q.removed, [])

    def test_a_single_file_torrent_is_the_image_itself(self):
        iso = self.root / "disc.iso"
        iso.write_bytes(b"iso")
        t = torrent("disc.iso")
        files = [{"name": "disc.iso", "priority": 1}]
        q = _Qbt([t], {"h1": files})
        self.assertEqual(self.run_pass(q), (0, 0))   # the file is still there
        iso.unlink()
        self.assertEqual(self.run_pass(q), (1, 0))   # gone: entry only
        self.assertEqual(q.removed, [("h1", False)])

    def test_another_category_and_a_torrent_still_downloading_are_left(self):
        self.make(*EXTRAS)
        q = _Qbt([torrent(category="tv"), torrent(h="h2", progress=0.5)],
                 {"h1": sacd_files(), "h2": sacd_files()})
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(q.removed, [])

    def test_what_cannot_be_read_is_not_gone(self):
        self.make(*EXTRAS)
        q = _Qbt([torrent()], {"h1": sacd_files()})
        real_walk = os.walk

        def walk(path, onerror=None, **k):
            if onerror is not None and str(path) == str(self.folder):
                onerror(PermissionError("denied"))
                return iter(())
            return real_walk(path, onerror=onerror, **k)
        with mock.patch.object(QD.os, "walk", walk):
            self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(q.removed, [])

    def test_a_removal_the_client_refuses_is_not_counted(self):
        self.make(*EXTRAS)
        q = _Qbt([torrent()], {"h1": sacd_files()})
        q.remove = lambda h, delete_files=False: False   # claimed / not read
        self.assertEqual(self.run_pass(q), (0, 0))
        self.assertEqual(self.lines, [])


class OrdinaryMusicTorrentIsUnchanged(Base):
    """A torrent with selected audio goes through the rules it always did."""

    def files(self):
        return [{"name": "Alb/01.flac", "priority": 1},
                {"name": "Alb/cover.jpg", "priority": 1}]

    def t(self):
        t = torrent("Alb")
        t["content_path"] = "/data/Alb"
        return t

    def test_audio_gone_removes_it_with_its_data_as_before(self):
        self.make("cover.jpg", where=self.root / "Alb")
        q = _Qbt([self.t()], {"h1": self.files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])
        self.assertEqual(self.lines, ["lifecycle: REMOVED (fully imported) 'Alb'"])

    def test_audio_still_there_is_not_a_disc_image_case(self):
        self.make("01.flac", "cover.jpg", where=self.root / "Alb")
        q = _Qbt([self.t()], {"h1": self.files()})
        calls = []
        done = []
        with mock.patch.object(QD, "plan_torrent",
                               lambda *a, **k: calls.append(1) or None):
            QD.torrent_lifecycle_pass(
                q, str(self.root), category="lidarr", emit=self.lines.append,
                lidarr=object(), min_stable_seconds=0, now=10 ** 12,
                on_complete=done.append, completed_seen=set())
        self.assertEqual(calls, [1])                # it IS planned
        self.assertEqual(done, [str(self.root / "Alb")])   # and the hook fires
        self.assertEqual(q.removed, [])

    def test_a_missing_folder_is_the_old_entry_only_removal(self):
        q = _Qbt([self.t()], {"h1": self.files()})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", False)])

    def test_audio_and_an_image_together_follow_the_audio_rules(self):
        files = self.files() + [{"name": "Alb/x.iso", "priority": 1}]
        self.make("cover.jpg", "x.iso", where=self.root / "Alb")  # audio gone
        q = _Qbt([self.t()], {"h1": files})
        self.assertEqual(self.run_pass(q), (1, 0))
        self.assertEqual(q.removed, [("h1", True)])  # the old rule: no audio left
        self.assertEqual(self.lines, ["lifecycle: REMOVED (fully imported) 'Alb'"])


class OnDisk(Base):
    def test_the_helper(self):
        d = self.root / "T"
        self.assertTrue(QD._disc_image_or_audio_on_disk(str(d)))    # unreadable
        d.mkdir()
        self.assertFalse(QD._disc_image_or_audio_on_disk(str(d)))   # empty
        self.make("a.jpg", "b.nfo", "c.txt", "log.sfv", where=d)
        self.assertFalse(QD._disc_image_or_audio_on_disk(str(d)))   # extras
        self.make("deep/er/x.MP3", where=d)
        self.assertTrue(QD._disc_image_or_audio_on_disk(str(d)))    # audio
        (d / "deep" / "er" / "x.MP3").unlink()
        self.make("deep/DISC.Iso", where=d)
        self.assertTrue(QD._disc_image_or_audio_on_disk(str(d)))    # image
        f = self.root / "one.flac"
        f.write_bytes(b"x")
        self.assertTrue(QD._disc_image_or_audio_on_disk(str(f)))    # a file
        g = self.root / "one.jpg"
        g.write_bytes(b"x")
        self.assertFalse(QD._disc_image_or_audio_on_disk(str(g)))


if __name__ == "__main__":
    unittest.main()
