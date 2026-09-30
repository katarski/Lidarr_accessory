"""The harvest keep folder holds one copy of each song, byte for byte.

The same song reached it from several boxes and a collision renamed each
copy ('... (CD1)', '... (_harvest_pending) (_harvest_pending)'): three
identical 'Without Love's sat there, each fingerprinted every pass."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import song_harvest as SH  # noqa: E402


class KeepFolderDedupe(unittest.TestCase):

    def test_exact_copies_go_and_the_shortest_name_stays(self):
        with tempfile.TemporaryDirectory() as d:
            same = b"A" * 1000
            for n in ("07. Without Love.mp3",
                      "07. Without Love (CD1) (_harvest_pending).mp3",
                      "07. Without Love (_harvest_pending) (_harvest_pending).mp3"):
                open(os.path.join(d, n), "wb").write(same)
            open(os.path.join(d, "08. Other.mp3"), "wb").write(b"B" * 1000)
            open(os.path.join(d, "notes.txt"), "wb").write(same)
            self.assertEqual(SH.dedupe_keep_dir(d), 2)
            self.assertEqual(sorted(os.listdir(d)),
                             ["07. Without Love.mp3", "08. Other.mp3", "notes.txt"])


if __name__ == "__main__":
    unittest.main()
