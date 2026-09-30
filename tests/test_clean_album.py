"""A folder named 'Title - YYYY.MM.DD' is not read as 'Artist - Year - Album'
(loops missed): 'Hidden Figures (Original Score) - 2017.01.06' became album
'01.06', no Lidarr lookup could find it, and the lifecycle deleted the folder
as an unwanted leftover."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbt_deselect as QD  # noqa: E402


class CleanAlbum(unittest.TestCase):

    def test_a_trailing_date_is_not_an_album(self):
        self.assertEqual(QD._clean_album(
            "Hidden Figures (Original Score) - 2017.01.06"), "Hidden Figures")
        self.assertEqual(QD._clean_album("Some Title - 2019-03-01"), "Some Title")

    def test_a_dated_show_keeps_its_title(self):
        self.assertEqual(QD._clean_album(
            "Bonnie Raitt - 1971-03-27 - The Jabberwocky Club, Syracuse "
            "University, NY, SBD [Flac]"),
            "The Jabberwocky Club, Syracuse University, NY, SBD")

    def test_artist_year_album_still_works(self):
        for name, album in (("Mahalia Jackson - 2005 - Gospel Train", "Gospel Train"),
                            ("Etta James - 1961. At Last", "At Last"),
                            ("Prince - 1982 - 1999", "1999"),
                            ("Adele - 2015 - 25", "25")):
            self.assertEqual(QD._clean_album(name), album)


if __name__ == "__main__":
    unittest.main()
