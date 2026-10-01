"""A download name's trailing rip tags are not part of the album title.

'Black Roses 320' is the 'Black Roses' Inner Circle owns; four such names
(bitrate only) were sent to the model on 28 Sep-1 Oct. Replayed over the 17
logged questions with rip tags: no verdict changes, those four are settled
without it."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as D  # noqa: E402

ALB = {"id": 1, "title": "Black Roses", "releaseDate": "1999-01-01",
       "statistics": {"trackFileCount": 10, "totalTrackCount": 10}}


class RipTail(unittest.TestCase):

    def test_only_the_tail_is_cut(self):
        f = D.strip_rip_tail
        self.assertEqual(f("Black Roses 320"), "Black Roses")
        self.assertEqual(f("Wrapped Up In Your Love 192-320"), "Wrapped Up In Your Love")
        self.assertEqual(f("5AM FLAC 24-44.1"), "5AM")
        self.assertEqual(f("A State Of Trance 996 320 kbps"), "A State Of Trance 996")
        self.assertEqual(f("Best I Ever Had FLAC ⭐️"), "Best I Ever Had")
        for keep in ("Room 192 Blues", "21", "320", "Vol 1", "Magnum Opus",
                     "Charlotte's Web"):
            self.assertEqual(f(keep), keep)

    def test_an_owned_album_is_known_without_the_model(self):
        asked = []
        lid = SimpleNamespace(
            find_artist=lambda n: {"id": 591},
            list_albums_for_artist=lambda i: [ALB],
            get_album=lambda i: ALB,
            list_tracks_for_album=lambda i: [{"monitored": True, "hasFile": True}] * 10)
        llm = SimpleNamespace(pick_owned_album=lambda *a: asked.append(a))
        self.assertEqual(
            D.album_complete_in_library(lid, "Inner Circle", "Black Roses 320", llm=llm),
            (True, 10, 10))
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
