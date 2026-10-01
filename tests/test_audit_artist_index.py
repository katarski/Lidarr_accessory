"""The audit reads a library artist folder as the Lidarr artist kept in it.

George Harrison's MusicBrainz disambiguation is "The Beatles" and Rob
Thomas's "Matchbox Twenty"; indexed by name, and listed first, they took
both bands' folders (1 Oct 2026): every Beatles album was "not in Lidarr"."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class _Lidarr:
    def artists(self):
        return [
            {"id": 1, "artistName": "George Harrison", "sortName": "harrison, george",
             "disambiguation": "The Beatles", "path": "/music/Music/George Harrison"},
            {"id": 2, "artistName": "Rob Thomas", "sortName": "rob thomas",
             "disambiguation": "Matchbox Twenty", "path": "/music/Music/Rob Thomas"},
            {"id": 3, "artistName": "The Beatles", "sortName": "beatles, the",
             "disambiguation": "", "path": "/music/Music/The Beatles"},
            {"id": 4, "artistName": "Matchbox Twenty", "sortName": "matchbox twenty",
             "disambiguation": "", "path": "/music/Music/Matchbox Twenty/"},
        ]


class ArtistFolder(unittest.TestCase):

    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)
        self.o.lidarr = _Lidarr()
        self.idx = self.o._build_lidarr_artist_index()

    def test_the_artist_kept_in_the_folder_answers_before_a_disambiguation(self):
        got = {n: (self.o._lidarr_lookup_artist(n, self.idx) or {}).get("id")
               for n in ("The Beatles", "Matchbox Twenty", "George Harrison", "Rob Thomas")}
        self.assertEqual(got, {"The Beatles": 3, "Matchbox Twenty": 4,
                               "George Harrison": 1, "Rob Thomas": 2})

    def test_a_folder_lidarr_does_not_keep_is_still_found_by_name(self):
        self.assertEqual((self.o._lidarr_lookup_artist("the beatles", self.idx) or {}).get("id"), 3)
        self.assertIsNone(self.o._lidarr_lookup_artist("Nobody At All", self.idx))


if __name__ == "__main__":
    unittest.main()
