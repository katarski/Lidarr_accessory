"""Files are paired with tracks by song title, never onto a track that already
has a file, and a folder whose titles contradict the album imports nothing."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class _Lidarr:
    def __init__(self, tracks):
        self.tracks = tracks
        self.applied = None

    def get_album(self, album_id):
        return {"id": album_id, "title": "Album",
                "releases": [{"id": 7, "monitored": True}]}

    def list_tracks_for_release(self, album_id, rid):
        return self.tracks

    def list_tracks_for_album(self, album_id):
        return self.tracks

    def manual_import_candidates(self, folder, artist_id=None):
        return [{"path": str(p), "quality": {"q": 1}} for p in self.files]

    def windows_to_lidarr(self, p):
        return str(p)

    def manual_import_apply_files(self, items, import_mode="move"):
        self.applied = items
        return 99


def _tracks(titles, filled=(), per_disc=None):
    out = []
    for i, title in enumerate(titles, 1):
        md, tn = per_disc[i - 1] if per_disc else (1, i)
        out.append({"id": 100 + i, "title": title, "mediumNumber": md,
                    "trackNumber": tn, "hasFile": i in filled})
    return out


def _orch(tracks, files, tags=None, numbers=None):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = type("C", (), {"verify_track_titles_accept": 0.60,
                           "verify_track_titles_reject": 0.25})()
    o.lidarr = _Lidarr(tracks)
    o.lidarr.files = files
    tags = tags or {}
    numbers = numbers or {}
    o._tag_title = lambda p: tags.get(p.name, "")
    o._tag_disc_and_track = lambda p: numbers.get(p.name, (0, 0))
    return o


def _run(o, files):
    o.lidarr.applied = None
    o._import_library_folder_by_tracknumber({"id": 1, "title": "Album"}, 5, files)
    return {Path(i["path"]).name: i["trackIds"][0] for i in (o.lidarr.applied or [])}


class Pairing(unittest.TestCase):
    def test_another_records_songs_are_not_filed_by_their_numbers(self):
        blue = ["Mellow My Mind", "Someday in My Life", "Sad Old Red", "Blue"]
        files = [Path("/d/CD 2/%02d - %s.flac" % (i, t)) for i, t in enumerate(
            ["King Bee", "Rainin' in My Heart", "Scratch My Back", "Te-Ni-Nee-Ni-Nu"], 1)]
        o = _orch(_tracks(blue), files,
                  numbers={f.name: (2, i) for i, f in enumerate(files, 1)})
        self.assertEqual(_run(o, files), {})

    def test_a_track_that_has_a_file_is_never_a_target(self):
        titles = ["Coming Home", "Dust My Broom", "The Sky Is Crying"]
        files = [Path("/d/%02d - %s.flac" % (i, t)) for i, t in enumerate(titles, 1)]
        o = _orch(_tracks(titles, filled={1, 3}), files)
        self.assertEqual(_run(o, files), {"02 - Dust My Broom.flac": 102})

    def test_a_gap_stays_a_gap(self):
        titles = ["One", "Two", "Three", "Four", "Five", "Six"]
        have = [t for t in titles if t != "Four"]
        files = [Path("/d/%02d - %s.mp3" % (i, t)) for i, t in enumerate(have, 1)]
        o = _orch(_tracks(titles), files,
                  numbers={f.name: (1, i) for i, f in enumerate(files, 1)})
        got = _run(o, files)
        self.assertEqual(got["04 - Five.mp3"], 105)
        self.assertNotIn(104, got.values())

    def test_equal_titles_are_told_apart_by_number(self):
        titles = ["Intro", "That's All", "Middle", "That's All"]
        files = [Path("/d/%02d - %s.flac" % (i, t)) for i, t in enumerate(titles, 1)]
        o = _orch(_tracks(titles, per_disc=[(1, 1), (1, 2), (2, 1), (2, 2)]), files,
                  numbers={f.name: (0, i) for i, f in enumerate(files, 1)})
        got = _run(o, files)
        self.assertEqual(got["02 - That's All.flac"], 102)
        self.assertEqual(got["04 - That's All.flac"], 104)

    def test_a_wrong_tag_loses_to_a_right_filename(self):
        titles = ["I Know You Can Feel It", "A Question of Trust"]
        files = [Path("/d/NIN - TRON - 01 - I Know You Can Feel It.flac"),
                 Path("/d/NIN - TRON - 02 - A Question of Trust.flac")]
        o = _orch(_tracks(titles, filled={1}), files,
                  tags={files[1].name: "I Know You Can Feel It"})
        self.assertEqual(_run(o, files), {files[1].name: 102})

    def test_untitled_files_go_by_number_only_when_the_folder_is_this_album(self):
        titles = ["Alpha", "Beta", "Gamma"]
        files = [Path("/d/Track %02d.wav" % i) for i in (1, 2, 3)]
        nums = {f.name: (1, i) for i, f in enumerate(files, 1)}
        o = _orch(_tracks(titles), files, numbers=nums)
        self.assertEqual(len(_run(o, files)), 3)          # count is the release's
        o = _orch(_tracks(titles + ["Delta"]), files, numbers=nums)
        self.assertEqual(_run(o, files), {})              # no evidence at all

    def test_a_transliterated_folder_meets_cyrillic_metadata(self):
        titles = ["Ветрове", "Стари мой приятелю", "Панаири"]
        files = [Path("/d/%02d - %s.mp3" % (i, t)) for i, t in enumerate(
            ["Vetrove", "Stari moi priyatelyu", "Panairi"], 1)]
        o = _orch(_tracks(titles), files)
        self.assertEqual(len(_run(o, files)), 3)


class GrabTarget(unittest.TestCase):
    def test_our_own_record_still_needs_the_songs_to_agree(self):
        o = _orch(_tracks(["Blue", "Sad Old Red"]), [])
        o._best_release_title_coverage = lambda files, aid: (0.0, 0, 2)
        o._align_release_to_disk = lambda *a, **k: None
        called = []
        o._import_library_folder_by_tracknumber = lambda *a: called.append(1)
        files = [Path("/d/01 - King Bee.flac"), Path("/d/02 - Shake Your Hips.flac")]
        ok = o._import_grab_target(Path("/d"), Path("/d"), files,
                                   {"album_id": 1, "artist_id": 5,
                                    "source": "recorded"}, "test")
        self.assertFalse(ok)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
