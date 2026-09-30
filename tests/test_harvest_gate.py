"""The song harvest re-examines a source only when the source, or what Lidarr
wants under its titles, changed (loops F9): the gate hashed every wanted track
in the library, so any import anywhere re-opened every source, and each pass
re-read ~2,200 files with the slow sniffing tag open."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import song_harvest as SH  # noqa: E402


class _Lidarr:
    """One artist, one monitored album missing `wanted` (title -> track id)."""

    def __init__(self, wanted):
        self.wanted = dict(wanted)
        self.failure_generation = 0
        self.fail_tracks = False

    def list_artists(self):
        return [{"id": 1, "artistName": "Artist"}]

    def list_albums_for_artist(self, aid):
        return [{"id": 10, "title": "Album", "monitored": True,
                 "artist": {"id": 1, "artistName": "Artist"},
                 "statistics": {"trackFileCount": 0, "totalTrackCount": 9},
                 "releases": [{"id": 5, "monitored": True}]}]

    def list_tracks_for_album(self, album_id):
        if self.fail_tracks:
            self.failure_generation += 1
            return []
        return [{"id": i, "title": t, "hasFile": False, "monitored": True,
                 "duration": 1000} for t, i in self.wanted.items()]


def _tags(path):
    return SH.SourceFile(path=path, title="Song B", artist="Someone Else",
                         duration_ms=999000)


class GatePerSource(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.src = os.path.join(d.name, "Box")
        os.makedirs(self.src)
        for n in ("01.flac", "02.flac", "03.flac"):
            Path(self.src, n).write_bytes(b"x")
        self.led = SH.HarvestLedger(os.path.join(d.name, "led.json"))
        patch = mock.patch.object(SH, "read_tags", _tags)
        patch.start()
        self.addCleanup(patch.stop)

    def run_pass(self, lid, **kw):
        return SH.harvest_pass(lid, [self.src], ledger=self.led,
                               dry_run=True, **kw)

    def test_an_import_elsewhere_does_not_reopen_a_source(self):
        lid = _Lidarr({"Song A": 100})
        self.assertEqual(self.run_pass(lid)["scanned"], 3)
        lid.wanted["Another Song"] = 101          # wanted elsewhere
        st = self.run_pass(lid)
        self.assertEqual((st["skipped_unchanged"], st["scanned"]), (1, 0))

    def test_a_change_under_its_own_titles_does_reopen_it(self):
        lid = _Lidarr({"Song A": 100})
        self.run_pass(lid)
        lid.wanted["Song B"] = 102                 # now wanted: this file's title
        st = self.run_pass(lid)
        self.assertEqual((st["skipped_unchanged"], st["scanned"]), (0, 3))

    def test_nothing_is_recorded_while_lidarr_failed(self):
        lid = _Lidarr({"Song A": 100})
        lid.fail_tracks = True
        self.run_pass(lid)
        self.assertNotIn(self.src, self.led._seen)

    def test_a_scan_cut_by_the_file_cap_is_not_recorded(self):
        self.run_pass(_Lidarr({"Song A": 100}), max_files=2)
        self.assertNotIn(self.src, self.led._seen)

    def test_verdicts_survive_a_restart(self):
        lid = _Lidarr({"Song A": 100})
        self.run_pass(lid)
        again = SH.HarvestLedger(self.led.path)
        self.assertTrue(again.unchanged(self.src, SH.folder_signature(self.src),
                                        SH.build_wanted_index(lid)))


class TagsAreReadTypedAndCached(unittest.TestCase):

    def test_one_typed_open_per_unchanged_file(self):
        opens = []

        def fake_open(path, options=None, easy=False):
            opens.append(easy)
            return SimpleNamespace(tags={"title": ["Song"], "artist": ["A"]},
                                   info=SimpleNamespace(length=61.5),
                                   get=lambda k: {"title": ["Song"],
                                                  "artist": ["A"]}.get(k))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "01.flac")
            Path(p).write_bytes(b"x")
            with mock.patch("audio_open.File", fake_open):
                a = SH.read_tags(p)
                b = SH.read_tags(p)
                Path(p).write_bytes(b"xy")         # changed: read again
                SH.read_tags(p)
        self.assertEqual((a.title, a.artist, a.duration_ms), ("Song", "A", 61500))
        self.assertEqual(b.title, "Song")
        self.assertEqual(opens, [False, False])


if __name__ == "__main__":
    unittest.main()
