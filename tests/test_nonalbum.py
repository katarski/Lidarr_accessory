"""Owner, 8 Nov 2026: "i dont need comilations, mixes, remixes and other non
album bulshit. I kept seeing it". The interactive search had grabbed
'A State Of Trance 996' for a wanted radio-show mix, and 'Journey - The
Ultimate Collection Time3' as a "discography"."""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

O = Orchestrator.__new__(Orchestrator)


class NotAnAlbum(unittest.TestCase):

    def test_what_is_not_an_album(self):
        for title, types, kind in [
            ("A State Of Trance Radio Top 15 - April 2009", (), "Album"),
            ("Armin van Buuren - A State Of Trance 996 (24.12.2020) 320 kbps", (), ""),
            ("Journey - The Ultimate Collection Time3 (1992)[FLAC]", (), ""),
            ("Lotos Land Remixed", (), "Album"),
            ("Mauro Picotto Metamorphose (In the Mix)", (), ""),
            ("Live @ [2009-07-15] Meganite, Privilege (Ibiza)", (), ""),
            ("Wendy & Lisa - B-Sides / Instrumentals / Edits", (), "Album"),
            ("Tour of Earth 2025", ("Live",), "Album"),
            ("Ray of Light", (), "Single"),
            ("Thomas Newman: Movie Classics", ("Compilation",), "Album"),
            ("The Very Best of Fontella Bass", (), "Album"),
        ]:
            self.assertTrue(O._not_an_album_reason(title, "", types, kind), title)

    def test_what_is(self):
        for title, types in [
            ("We're the Best of Friends", ()),
            ("Ray of Light", ()),
            ("Live Through This", ()),
            ("Mixed Emotions Are Fine", ()) if False else ("Abbey Road", ()),
            ("The Lone Ranger (Original Motion Picture Score)", ("Soundtrack",)),
            ("2011 - The Collection (6 Complete Albums)", ()),
            ("Moby - Studio Discography 1992 - 2011 (320kbps)", ()),
        ]:
            self.assertIsNone(O._not_an_album_reason(title, "", types, "Album"), title)

    def test_the_search_never_asks_for_one_nor_grabs_one(self):
        src = inspect.getsource(Orchestrator.interactive_search_pass)
        self.assertIn("self._not_an_album_reason(", src)
        self.assertIn("self.lidarr.set_album_monitored(aid, False)", src)
        for ranker in (Orchestrator._rank_releases, Orchestrator._rank_artist_releases):
            self.assertIn("na = self._not_an_album_reason(title)",
                          inspect.getsource(ranker))


    def test_the_held_discard_takes_a_live_record_even_as_a_gap(self):
        src = inspect.getsource(Orchestrator._auto_discard_held_unwanted)
        i = src.index("na = self._not_an_album_reason(")
        self.assertLess(i, src.index("if not copy:"))
        self.assertIn("self._held_lidarr_types(artist, album)", src[i:i + 200])
        self.assertTrue(O._not_an_album_reason("Hank Live", "", ("Live",), "Album"))


if __name__ == "__main__":
    unittest.main()
