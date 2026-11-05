"""Add to library (Assembly tab) on an album whose files a release switch
orphaned.

5 Nov: Counting Crows / Hard Candy sat at 0/15 with its 15 files in
`Hard Candy (2002)`, Jagged Little Pill at 0/12 with 29 in its folder. Every
Add staged copies, Lidarr named each onto a path that already held the
orphaned file, and every one failed on DestinationAlreadyExists -- the Add
"did nothing". The staging folder it left at `/downloads/assembly-37` was
then taken for a download by the sweep, whose record step switched the
album's release from a stale snapshot and orphaned the 29 files the library
audit had filed five minutes earlier.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402
from dedup_downloads import ASSEMBLY_STAGING_DIR, in_pipeline_temp  # noqa: E402


def _client(album, tracks, release_tracks, readable=True):
    c = L.LidarrClient.__new__(L.LidarrClient)
    c.failure_generation = 0
    puts = []

    def list_tracks_for_album(aid):
        if not readable:
            c.failure_generation += 1
            return []
        return tracks

    c.get_album = lambda aid: dict(album, releases=[dict(r) for r in album["releases"]])
    c.list_tracks_for_album = list_tracks_for_album
    c.list_tracks_for_release = lambda aid, rid: release_tracks.get(rid, [])
    c._refresh_running = lambda *a: False
    c._put = lambda path, body: puts.append(body)
    return c, puts


ALBUM = {"id": 37, "artistId": 9, "title": "Jagged Little Pill",
         "statistics": {"trackFileCount": 29},
         "releases": [{"id": 543, "monitored": True},
                      {"id": 555, "monitored": False}]}
HELD = ([{"id": i, "hasFile": True, "foreignRecordingId": "orig-%d" % i}
         for i in range(12)]
        + [{"id": 100 + i, "hasFile": True, "foreignRecordingId": "live-%d" % i}
           for i in range(17)])
REL_555 = [{"id": 500 + i, "foreignRecordingId": "orig-%d" % i} for i in range(12)]


class ReleaseSwitchGuard(unittest.TestCase):
    def test_a_switch_that_would_orphan_held_files_is_refused(self):
        c, puts = _client(ALBUM, HELD, {555: REL_555})
        with self.assertLogs(L.logger, "WARNING") as logs:
            self.assertFalse(c.set_album_monitored_release(37, 555))
        self.assertEqual(puts, [])
        self.assertIn("17 file(s)", logs.output[0])

    def test_a_release_carrying_every_held_recording_is_switched_to(self):
        bigger = REL_555 + [{"id": 600 + i, "foreignRecordingId": "live-%d" % i}
                            for i in range(17)]
        c, puts = _client(ALBUM, HELD, {555: bigger})
        self.assertTrue(c.set_album_monitored_release(37, 555))
        self.assertEqual(len(puts), 1)

    def test_an_album_holding_nothing_switches_without_reading_tracks(self):
        empty = dict(ALBUM, statistics={"trackFileCount": 0})
        c, puts = _client(empty, HELD, {})
        c.list_tracks_for_album = None          # must not be called
        self.assertTrue(c.set_album_monitored_release(37, 555))
        self.assertEqual(len(puts), 1)

    def test_lidarr_unreadable_is_not_an_answer(self):
        c, puts = _client(ALBUM, HELD, {555: REL_555}, readable=False)
        with self.assertLogs(L.logger, "WARNING"):
            self.assertFalse(c.set_album_monitored_release(37, 555))
        self.assertEqual(puts, [])

    def test_a_caller_that_refiles_the_folder_may_switch(self):
        c, puts = _client(ALBUM, HELD, {555: REL_555})
        self.assertTrue(c.set_album_monitored_release(37, 555,
                                                      allow_orphaning=True))
        self.assertEqual(len(puts), 1)


class StagingIsNotADownload(unittest.TestCase):
    def test_the_assembly_staging_folder_is_pipeline_temp(self):
        self.assertTrue(in_pipeline_temp(
            "/downloads/%s/assembly-37/01 - All I Really Want.flac"
            % ASSEMBLY_STAGING_DIR))
        self.assertFalse(in_pipeline_temp("/downloads/assembly-37"))
        self.assertFalse(ASSEMBLY_STAGING_DIR.startswith("."))   # Lidarr skips dot dirs


class _AddLidarr:
    """Album 6598 with one track; `filled` is its trackFileId."""
    failure_generation = 0

    def __init__(self):
        self.filled = 0
        self.sent = []
        self.adopt_calls = 0

    def get_album(self, aid):
        return {"id": aid, "releases": [{"id": 46004, "monitored": True}],
                "artistId": 392}

    def list_tracks_for_album(self, aid):
        return [{"id": 1, "trackFileId": self.filled}]

    def list_trackfiles_for_album(self, aid):
        return ([{"id": self.filled, "path": "/music/Counting Crows/Hard Candy (2002)/x.flac"}]
                if self.filled else [])

    def manual_import_candidates(self, path, artist_id=None):
        return []

    def manual_import_apply_files(self, items, import_mode="move"):
        self.sent.extend(items)
        self.filled = 77
        return 2

    def wait_for_command(self, cmd, **k):
        return {"status": "completed"}


class AddAdoptsTheAlbumFolder(unittest.TestCase):
    def make(self, album_dir_files=True):
        from assembly import AssemblyStore
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        src = root / "comp" / "01 - Hard Candy.flac"
        src.parent.mkdir()
        src.write_bytes(b"x")
        lib = root / "lib" / "Counting Crows" / "Hard Candy (2002)"
        lib.mkdir(parents=True)
        orphan = lib / "Counting Crows - Hard Candy - 01 - Hard Candy.flac"
        orphan.write_bytes(b"y")
        L_ = _AddLidarr()
        o = Orchestrator.__new__(Orchestrator)
        o.lidarr = L_
        o.cfg = type("C", (), {"staging_root": root / "downloads"})()
        o.assembly = AssemblyStore(None)
        o.assembly.upsert(6598, {"artist": "Counting Crows", "album": "Hard Candy",
                                 "total": 1,
                                 "matched": [{"source": str(src), "track_id": 1,
                                              "number": 1, "track": "Hard Candy"}],
                                 "sources": {str(src): 1}})
        o._lidarr_generation = lambda: 0
        o._trigger_artist_refresh = lambda *a, **k: None
        o._write_basic_tags = lambda *a, **k: None
        o._find_album_on_disk = lambda a, b: ((lib, [orphan]) if album_dir_files
                                              else (None, []))
        o._album_audio_everywhere = lambda d: [orphan]

        def adopt(rec, aid, audios, import_mode="move"):
            L_.adopt_calls += 1
            self.assertEqual(audios, [orphan])
            L_.filled = 55                      # Lidarr files it in place
            return 1
        o._import_library_folder_by_tracknumber = adopt
        return o, L_, src, root

    def test_an_orphaned_library_file_is_filed_in_place_and_nothing_is_copied(self):
        o, L_, src, _ = self.make()
        ok, msg = o.assembly_add_to_library(6598)
        self.assertTrue(ok, msg)
        self.assertEqual(L_.adopt_calls, 1)
        self.assertEqual(L_.sent, [])           # no copy onto its path
        self.assertTrue(src.exists())           # nothing was filed from it
        self.assertIsNone(o.assembly.get(6598))
        self.assertIn("untracked", msg)

    def test_without_a_folder_the_copy_is_staged_outside_the_sweeps_reach(self):
        o, L_, src, root = self.make(album_dir_files=False)
        ok, msg = o.assembly_add_to_library(6598)
        self.assertTrue(ok, msg)
        self.assertEqual(L_.adopt_calls, 0)
        self.assertEqual(len(L_.sent), 1)
        self.assertIn("/%s/assembly-6598/" % ASSEMBLY_STAGING_DIR,
                      L_.sent[0]["path"].replace("\\", "/"))
        self.assertFalse((root / "downloads" / "assembly-6598").exists())

    def test_a_folder_of_another_name_is_never_read(self):
        o, L_, src, root = self.make()
        other = root / "lib" / "Counting Crows" / "Hard Candy (Live)"
        other.mkdir()
        o._find_album_on_disk = lambda a, b: (other, [])
        ok, msg = o.assembly_add_to_library(6598)
        self.assertEqual(L_.adopt_calls, 0)
        self.assertEqual(len(L_.sent), 1)


class LegacyStagingIsCleared(unittest.TestCase):
    def test_only_folders_shaped_like_an_adds_staging_are_removed(self):
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        old = root / "assembly-37"
        old.mkdir()
        (old / "01 - All I Really Want.flac").write_bytes(b"x")
        (old / "02 - You Oughta Know.flac").write_bytes(b"x")
        odd = root / "assembly-38"
        (odd / "CD1").mkdir(parents=True)
        named = root / "assembly-39"
        named.mkdir()
        (named / "song.flac").write_bytes(b"x")
        real = root / "Assembly - Live"
        real.mkdir()
        (real / "01 - Intro.flac").write_bytes(b"x")
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = type("C", (), {"staging_root": root})()
        self.assertEqual(o._drop_legacy_assembly_staging(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(odd.exists() and named.exists() and real.exists())


if __name__ == "__main__":
    unittest.main()
