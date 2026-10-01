"""An album title too short to carry information must still be named by the
release: its own words are in the title.

For Fehlfarben's '?0??' (normalised '0') every Fehlfarben release scored
~0.5 on the artist's name alone, over the 0.45 floor, and 'Monarchie und
Alltag (1980)' was grabbed for it; 'P.I.F. 5' scored as 'P.I.F. 6'."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator as O  # noqa: E402

FLOOR = 0.45


class WeakAlbumTitle(unittest.TestCase):

    def test_another_album_of_the_artist_is_not_it(self):
        for art, alb, title in (
                ("Fehlfarben", "?0??", "Fehlfarben - Monarchie und Alltag (1980)"),
                ("Fehlfarben", "?0??", "Fehlfarben - 33 Tage in Ketten (1981)"),
                ("P.I.F.", "P.I.F. 6", "P.I.F. - P.I.F. 5 (2014) FLAC"),
                ("D2", "6", "D2 - Lazha (2003)")):
            self.assertLess(O._title_relation(art, alb, title), FLOOR, title)

    def test_the_album_itself_still_passes(self):
        for art, alb, title in (
                ("Fehlfarben", "?0??", "Fehlfarben - ?0?? (2010) FLAC"),
                ("P.I.F.", "P.I.F. 6", "P.I.F. - P.I.F. 6 (2015) FLAC"),
                ("D2", "6", "D2 - 6 (2005)"),
                ("Beyonce", "4", "Beyonce - 4 (2011) [FLAC]")):
            self.assertGreaterEqual(O._title_relation(art, alb, title), FLOOR, title)


if __name__ == "__main__":
    unittest.main()
