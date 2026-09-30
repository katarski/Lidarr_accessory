"""find_artist's spelling fallback takes a variant, never another name.

'J. D. Blackfoot' scored 0.90 against the band 'Blackfoot' and was taken for
it 49 times; the true variants in the logs must still resolve."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402

LIB = [{"id": 1, "artistName": "Blackfoot"},
       {"id": 2, "artistName": "Candi Staton"},
       {"id": 3, "artistName": "Fats Domino"},
       {"id": 4, "artistName": "Olivier Derivière"},
       {"id": 5, "artistName": "Armin van Buuren"},
       {"id": 6, "artistName": "Hildur Guðnadóttir"}]


def _client():
    c = L.LidarrClient.__new__(L.LidarrClient)
    c.artists = lambda: LIB
    c.mb = None
    return c


class SpellingVariantOnly(unittest.TestCase):

    def test_another_name_is_not_a_variant(self):
        self.assertIsNone(_client().find_artist("J. D. Blackfoot"))

    def test_the_real_variants_still_resolve(self):
        c = _client()
        for name, want in (("77-Candi Staton", 2), ("Fats Dominoo", 3),
                           ("Oliver Deriviere", 4), ("armin van Buuren ] 76", 5),
                           ("Hildur Guonadottir", 6)):
            got = c.find_artist(name)
            self.assertEqual((got or {}).get("id"), want, name)


if __name__ == "__main__":
    unittest.main()
