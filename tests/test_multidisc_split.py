"""A disc of a multi-disc album is split into its disc folder and never
imported on its own; the album is imported whole once every disc is split.

Christina Aguilera's '1999 - 2003' (CD-1, CD-2): each disc was split into a
staging subfolder and imported alone, and Lidarr refused both halves
("Couldn't find similar album", 31 Oct 2026)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cue_parser import Track  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from splitter import SplitResult  # noqa: E402


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(watch_root=Path("/nonexistent"),
                            audio_extensions=[".flac", ".wav", ".ape", ".wv"])
    o._skip_seen = set()
    return o


class MultiDiscSplit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Artist_Album_2003"
        for d in ("CD-1", "CD-2"):
            (self.root / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_twin_cues_of_the_deleted_image_go_and_others_stay(self):
        cd1 = self.root / "CD-1"
        (cd1 / "a.cue").write_text('FILE "Album CD1.wav" WAVE\n')
        (cd1 / "b.cue").write_text('FILE "Album CD1.flac" WAVE\n')
        (cd1 / "other.cue").write_text('FILE "Something Else.flac" WAVE\n')
        _orch()._delete_twin_cues(cd1, [cd1 / "Album CD1.flac", None])
        self.assertEqual(sorted(p.name for p in cd1.iterdir()), ["other.cue"])

    def test_tracks_move_into_the_disc_folder_and_staging_goes(self):
        cd1 = self.root / "CD-1"
        stage = cd1 / "Album (2003)"
        stage.mkdir()
        splits = []
        for i in (1, 2):
            f = stage / ("Artist - Album - %02d - Song.flac" % i)
            f.write_bytes(b"x")
            splits.append(SplitResult(track=Track(number=i), output_path=f))
        n = _orch()._disc_tracks_into_disc_folder(splits, cd1, stage)
        self.assertEqual(n, 2)
        self.assertFalse(stage.exists())
        self.assertEqual([s.output_path.parent for s in splits], [cd1, cd1])

    def test_the_sweep_waits_while_a_disc_still_has_its_cue(self):
        old = time.time() - 3600
        for d in ("CD-1", "CD-2"):
            f = self.root / d / "01 - Song.flac"
            f.write_bytes(b"x")
            os.utime(f, (old, old))
        (self.root / "CD-2" / "Album CD2.cue").write_text('FILE "x.flac" WAVE\n')
        o = _orch()
        eligible = [(self.root / "CD-1", [self.root / "CD-1" / "01 - Song.flac"])]
        self.assertEqual(o._unify_multidisc_eligible(eligible, time.time(), 10), [])
        (self.root / "CD-2" / "Album CD2.cue").unlink()
        out = o._unify_multidisc_eligible(eligible, time.time(), 10)
        self.assertEqual([(p, len(a)) for p, a in out], [(self.root, 2)])


if __name__ == "__main__":
    unittest.main()
