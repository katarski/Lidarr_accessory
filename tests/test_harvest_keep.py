"""A song already in the harvest keep folder stays as it is (loops F12): it
was "consolidated" onto itself every pass and renamed with one more
' (_harvest_pending)' each time, up to 255 characters."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import song_harvest as SH  # noqa: E402

WANTED = SH.WantedTrack(title="Going To The River", track_id=1, album_id=2,
                        album_title="A", artist_id=3, artist_name="Fats Domino",
                        release_id=4, duration_ms=150000)
INDEX = {SH.norm_title("Going To The River"): [WANTED]}


def _tags(path):
    return SH.SourceFile(path=path, title="Going To The River",
                         artist="Fats Domino", duration_ms=150000)


class KeepFolder(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.keep = os.path.join(d.name, "_harvest_pending")
        self.box = os.path.join(d.name, "Box")
        os.makedirs(self.keep)
        os.makedirs(self.box)
        p = mock.patch.object(SH, "read_tags", _tags)
        p.start()
        self.addCleanup(p.stop)

    def purge(self, src):
        return SH.purge_leftovers(src, [], INDEX, keep_dir=self.keep)

    def test_a_kept_song_is_not_renamed_by_its_own_folder(self):
        Path(self.keep, "10. Going To The River.flac").write_bytes(b"x")
        for _ in range(3):
            self.purge(self.keep)
        self.assertEqual(os.listdir(self.keep), ["10. Going To The River.flac"])

    def test_an_inflated_name_is_given_back(self):
        name = "10. Going To The River" + " (_harvest_pending)" * 5 + ".flac"
        Path(self.keep, name).write_bytes(b"x")
        self.purge(self.keep)
        self.assertEqual(os.listdir(self.keep), ["10. Going To The River.flac"])

    def test_a_same_named_song_from_another_box_still_gets_its_own_name(self):
        Path(self.keep, "10. Going To The River.flac").write_bytes(b"x")
        Path(self.box, "10. Going To The River.flac").write_bytes(b"y")
        self.purge(self.box)
        self.assertEqual(sorted(os.listdir(self.keep)),
                         ["10. Going To The River (Box).flac",
                          "10. Going To The River.flac"])


if __name__ == "__main__":
    unittest.main()
