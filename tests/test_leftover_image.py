"""An image a finished sheet left behind is deleted by the sweep, as the
sheet's originals were.

'Ray Parker Jr. - After Dark -1987.cue' (Side 1 + Side 2) was found already
owned before its images were known: the .cue and Side 1 were deleted and
'... (Side 2).flac' stayed alone in a folder with no .cue, which no path
looked at again."""
import csv
import inspect
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

STEM = "Ray Parker Jr. - After Dark -1987"


class LeftoverImage(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.dir = self.root / "wv"
        self.dir.mkdir()
        self.ledger = self.root / "ledger.csv"
        self.asked = []
        self.lidarr_has = (True, 10, 10, 1, 2)

    def orch(self, rows):
        with self.ledger.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp_utc", "cue_path", "artist", "album",
                        "tracks", "source_kind", "outcome", "reason"])
            for outcome in rows:
                w.writerow(["t", str(self.dir / (STEM + ".cue")),
                            "Ray Parker Jr.", "After Dark", "0", "split_by_us",
                            outcome, ""])
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(ledger_file=self.ledger,
                                delete_originals_on_success=True)
        o._ledger_lock = threading.Lock()
        o.lidarr = SimpleNamespace(failure_generation=0)

        def has(artist, album, artist_id=None):
            self.asked.append((artist, album))
            return self.lidarr_has
        o._lidarr_album_is_imported = has
        return o

    def file(self, name):
        p = self.dir / name
        p.write_bytes(b"x")
        os.utime(p, (time.time() - 3600,) * 2)
        return p

    def test_the_side_left_behind_goes(self):
        side2 = self.file(STEM + " (Side 2).flac")
        o = self.orch(["failed", "already_in_lidarr"])
        self.assertTrue(o._drop_leftover_images(self.dir, [side2], time.time(), 10))
        self.assertFalse(side2.exists())
        self.assertEqual(self.asked, [("Ray Parker Jr.", "After Dark")])

    def test_kept_while_lidarr_does_not_hold_the_album(self):
        side2 = self.file(STEM + " (Side 2).flac")
        o = self.orch(["already_in_lidarr"])
        self.lidarr_has = (False, 7, 10, 1, 2)
        self.assertFalse(o._drop_leftover_images(self.dir, [side2], time.time(), 10))
        self.assertTrue(side2.exists())

    def test_other_files_and_unfinished_sheets_are_left(self):
        bonus = self.file(STEM + " - Bonus Track.flac")
        side2 = self.file(STEM + " (Side 2).flac")
        o = self.orch(["already_in_lidarr"])
        self.assertFalse(o._drop_leftover_images(self.dir, [bonus], time.time(), 10))
        self.assertTrue(bonus.exists())
        o = self.orch(["already_in_lidarr", "failed"])
        self.assertFalse(o._drop_leftover_images(self.dir, [side2], time.time(), 10))
        self.assertTrue(side2.exists())
        self.assertEqual(self.asked, [])

    def test_the_ledger_is_read_from_where_it_stopped(self):
        o = self.orch(["failed"])
        self.assertEqual(o._done_sheets().get(str(self.dir)), {})
        with self.ledger.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["t", str(self.dir / (STEM + ".cue")),
                                     "A", "B", "0", "split_by_us",
                                     "imported_via_manual", ""])
        self.assertEqual(o._done_sheets()[str(self.dir)][STEM.lower()],
                         ("A", "B", "imported_via_manual"))

    def test_the_sweep_looks_before_extracting_embedded_sheets(self):
        src = inspect.getsource(Orchestrator._sweep_cueless_pass)
        self.assertLess(src.index("_drop_leftover_images("),
                        src.index("_materialize_embedded_cues("))


if __name__ == "__main__":
    unittest.main()
