"""Hydrate files unplaced files only into the album the download names.

find_album matches either title inside the other, so 'More Abba Gold' came
back as the 1975 album 'ABBA' and its unplaced files were filed into it."""
import os
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

ABBA = {"id": 17, "title": "ABBA", "artistId": 4,
        "releases": [{"id": 191, "trackCount": 2, "monitored": True}]}
TRACKS = [{"id": 2414, "title": "Honey, Honey", "albumReleaseId": 191},
          {"id": 2415, "title": "Ring, Ring", "albumReleaseId": 191}]


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o._tl = threading.local()
    o.lidarr = SimpleNamespace(
        failure_generation=0,
        find_artist=lambda n: {"id": 4, "artistName": "ABBA"},
        find_album=lambda aid, title, **k: ABBA,
        get_album=lambda i: ABBA,
        list_tracks_for_album=lambda i: TRACKS)
    return o


CANDS = [{"path": "/downloads/More Abba Gold/07 - Honey Honey.flac"}]


class HydrateStaysInItsAlbum(unittest.TestCase):

    def test_another_album_takes_no_unplaced_file(self):
        o = _orch()
        self.assertEqual(o._hydrate_candidates(CANDS, "ABBA", "More Abba Gold", 20),
                         [None])

    def test_the_named_album_still_takes_them(self):
        o = _orch()
        out = o._hydrate_candidates(CANDS, "ABBA", "ABBA", 2)
        self.assertEqual(out[0]["album"]["id"], 17)
        self.assertEqual(out[0]["tracks"][0]["id"], 2414)

    def test_the_real_cases(self):
        o = _orch()
        for theirs, ours, artist, ok in (
                ("Get Even", "Get Even (Original Soundtrack) (Promo)",
                 "Olivier Deriviere", True),
                ("ObsCure", "Obscure 2 OST", "Olivier Deriviere", False),
                ("Blurred Lines", "Blurred Lines (Deluxe)", "Robin Thicke", True),
                ("The Drifters", "The Drifters Ultimate Collection",
                 "The Drifters", False)):
            self.assertEqual(o._override_title_ok(theirs, ours, artist), ok,
                             (theirs, ours))

    def test_the_pinned_album_is_trusted(self):
        o = _orch()
        o._tl.album_pin = ABBA
        o._find_album = lambda aid, title, **k: ABBA
        out = o._hydrate_candidates(CANDS, "ABBA", "More Abba Gold", 20)
        self.assertEqual(out[0]["album"]["id"], 17)


if __name__ == "__main__":
    unittest.main()
