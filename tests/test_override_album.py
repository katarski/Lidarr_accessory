"""The match-% override never forces a download into another album.

On 30 Sep 'More Abba Gold' (a compilation) scored 50.2% against the 1975
album 'ABBA'; the override (floor 50%) forced it in over Lidarr's 80% rule,
the album switched release, and its own 13 files lost their Lidarr record."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

REJ = [{"reason": "Album match is not close enough: 50.2 % vs 80 % "
                  "[album, year, country, tracks, unmatched tracks]"},
       {"reason": "Has unmatched tracks"}]


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(min_match_percent=50)
    return o


def _cand(title, rej=REJ):
    return {"path": "/downloads/x/01.flac",
            "album": {"id": 17, "title": title}, "rejections": list(rej)}


class OverrideStaysInItsAlbum(unittest.TestCase):

    def test_another_album_is_not_forced(self):
        o = _orch()
        self.assertEqual(
            o._filter_acceptable([_cand("ABBA")], album_name="More Abba Gold"), [])

    def test_the_same_album_is_still_forced(self):
        o = _orch()
        self.assertEqual(len(o._filter_acceptable(
            [_cand("ABBA")], album_name="ABBA")), 1)

    def test_an_edition_of_the_album_is_still_forced(self):
        o = _orch()
        self.assertEqual(len(o._filter_acceptable(
            [_cand("Spirit")], album_name="Spirit (Deluxe Edition)")), 1)
        self.assertEqual(len(o._filter_acceptable(
            [_cand("Weezer (Deluxe Edition)")], album_name="Weezer")), 1)

    def test_a_title_word_is_not_an_edition(self):
        o = _orch()
        self.assertEqual(o._filter_acceptable(
            [_cand("Blue")], album_name="Blue Train"), [])

    def test_the_real_cases(self):
        o = _orch()
        cases = [("ABBA", "More Abba Gold", "ABBA", False),
                 ("Candi", "In Retrospect", "The Toasters", False),
                 ("The Drifters", "The Drifters Ultimate Collection",
                  "The Drifters", False),
                 ("ObsCure II", "Obscure 2 OST", "Olivier Deriviere", True),
                 ("Monarchie und Alltag",
                  "Fehlfarben - 1980 - Monarchie und Alltag (Edt. 2000)",
                  "Fehlfarben", True),
                 ("11-11 Memories Retold", "11-11: Memories Retold",
                  "Olivier Deriviere", True)]
        for theirs, ours, artist, ok in cases:
            got = o._filter_acceptable([_cand(theirs)], album_name=ours,
                                       artist_name=artist)
            self.assertEqual(bool(got), ok, (theirs, ours))

    def test_a_clean_match_is_lidarrs_own_and_kept(self):
        o = _orch()
        self.assertEqual(len(o._filter_acceptable(
            [_cand("ABBA", rej=[])], album_name="More Abba Gold")), 1)


if __name__ == "__main__":
    unittest.main()
