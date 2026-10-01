"""The artist's catalogue says which record a download is, before the model.

29 Sep: the model took Armin van Buuren's 'Breathe In (2024) Extended
Edition' for the owned 'Breathe' and Ultravox's 'Quartet + Lament' two-in-one
for the owned 'Lament' (Quartet 0/118); both torrents were removed as
"already in library"."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as D  # noqa: E402


def alb(i, title, have, total):
    return {"id": i, "title": title, "releaseDate": "2000-01-01",
            "statistics": {"trackFileCount": have, "totalTrackCount": total}}


def run(albums, name, model_says):
    by_id = {a["id"]: a for a in albums}
    lid = SimpleNamespace(
        find_artist=lambda n: {"id": 7},
        list_albums_for_artist=lambda i: albums,
        get_album=lambda i: by_id[i],
        list_tracks_for_album=lambda i: [])
    asked = []

    def pick(download, owned):
        asked.append(download)
        return model_says
    return D.album_complete_in_library(
        lid, "Artist", name, llm=SimpleNamespace(pick_owned_album=pick)), asked


class CatalogueIdentity(unittest.TestCase):

    def test_an_edition_of_another_record_is_that_record(self):
        out, asked = run([alb(1, "Breathe", 51, 51), alb(2, "Breathe In", 0, 24)],
                         "Breathe In Extended Edition", "Breathe")
        self.assertEqual((out, asked), ((False, 0, 24), []))

    def test_a_two_in_one_with_a_missing_album_is_not_owned(self):
        out, asked = run([alb(1, "Quartet", 0, 118), alb(2, "Lament", 72, 72)],
                         "Ultravox_Quartet_Lament_2000", "Lament")
        self.assertEqual((out, asked), ((False, 0, 0), []))

    def test_a_number_is_part_of_the_record(self):
        self.assertNotEqual(D._core("24 Hours"), D._core("Hours"))
        self.assertEqual(D._core("Breathe In (2024) Extended Edition"),
                         D._core("Breathe In"))

    def test_a_title_inside_a_longer_one_is_not_a_second_record(self):
        named = D._records_named(D._core("Blue Train Live"),
                                 [alb(1, "Blue", 9, 9), alb(2, "Blue Train", 0, 5)],
                                 {"artist"})
        self.assertEqual(named, [])

    def test_two_readings_of_one_title_are_not_a_two_in_one(self):
        # 1 Oct, live: 'The Unforgettable Nat King Cole' was held to carry
        # both 'Unforgettable' and 'The Unforgettable'.
        named = D._records_named(
            D._core("The Unforgettable Nat King Cole"),
            [alb(1, "Unforgettable", 0, 12), alb(2, "The Unforgettable", 0, 10)],
            {"nat", "king", "cole"})
        self.assertEqual(named, [])
        two = D._records_named(D._core("Ultravox_Quartet_Lament_2000"),
                               [alb(1, "Quartet", 0, 9), alb(2, "Lament", 9, 9)],
                               {"ultravox"})
        self.assertEqual(sorted(a["title"] for a in two), ["Lament", "Quartet"])


if __name__ == "__main__":
    unittest.main()
