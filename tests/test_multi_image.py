"""One CUE sheet for several images -- both sides of a vinyl rip -- is split
image by image and imported as one album.

`Ray Parker Jr. - After Dark` ships one .cue with FILE '(Side 1)' (tracks
1-5) and FILE '(Side 2)' (6-10), each side timed from 00:00. The parser had
no track-to-FILE link: side 2's restart read as an invalid sheet, the two
whole sides went to Lidarr as two "tracks", and it refused them."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cue_parser as CP  # noqa: E402
import splitter as SP  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402

VINYL = """REM DATE 1987
PERFORMER "Ray Parker Jr."
TITLE "After Dark"
FILE "After Dark (Side 1).wv" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 01 04:16:00
  TRACK 03 AUDIO
    TITLE "Three"
    INDEX 01 08:56:00
FILE "After Dark (Side 2).wv" WAVE
  TRACK 04 AUDIO
    TITLE "Four"
    INDEX 01 00:00:00
  TRACK 05 AUDIO
    TITLE "Five"
    INDEX 01 04:32:00
"""

# EAC per-track sheet: track 2's pregap at the end of FILE 1.
PER_TRACK = """PERFORMER "A"
TITLE "B"
FILE "01.flac" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 00 03:58:00
FILE "02.flac" WAVE
    INDEX 01 00:00:00
  TRACK 03 AUDIO
    TITLE "Three"
FILE "03.flac" WAVE
    INDEX 01 00:00:00
"""


class SheetWithSides(unittest.TestCase):

    def test_two_sides_parse_as_two_images(self):
        cue = CP.parse_cue_text(VINYL)
        CP._fill_end_times(cue, 1000.0)
        self.assertTrue(cue.is_multi_image)
        self.assertTrue(cue.is_valid())
        self.assertEqual([t.file_index for t in cue.tracks], [0, 0, 0, 1, 1])
        self.assertEqual([t.end_seconds for t in cue.tracks],
                         [256.0, 536.0, None, 272.0, None])
        self.assertEqual(cue.is_disc_image(audio_duration=600.0), (True, ""))

    def test_a_per_track_sheet_is_still_not_an_image(self):
        cue = CP.parse_cue_text(PER_TRACK)
        self.assertFalse(cue.is_multi_image)
        ok, why = cue.is_disc_image()
        self.assertFalse(ok)
        self.assertIn("separate audio files", why)

    def test_each_track_is_cut_from_its_own_image(self):
        cue = CP.parse_cue_text(VINYL)
        CP._fill_end_times(cue, None)
        for t in cue.tracks:
            t.source = Path("/d/side%d.flac" % (t.file_index + 1))
        cmds = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(SP.subprocess, "run",
                                  lambda cmd, **k: cmds.append(cmd)), \
                mock.patch.object(SP, "_wait_for_write", lambda *a: None):
            SP.split_cue(cue, Path("/d/side1.flac"), Path(d))
        srcs = [c[c.index("-i") + 1] for c in cmds]
        self.assertEqual([Path(s).name for s in srcs],
                         ["side1.flac"] * 3 + ["side2.flac"] * 2)


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(audio_extensions=[".flac", ".ape", ".wv", ".wav"],
                            cue_ledger_enabled=True, cue_ledger_max_attempts=3,
                            cue_ledger_file=None)
    return o


class OrchestratorSides(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.cue = self.dir / "After Dark.cue"
        self.cue.write_text(VINYL, encoding="utf-8")
        self.sides = [self.dir / "After Dark (Side 1).flac",
                      self.dir / "After Dark (Side 2).flac"]
        for p in self.sides:
            p.write_bytes(b"x" * 1000)

    def test_images_resolve_across_an_extension_change(self):
        o = _orch()
        cue = CP.parse_cue_text(VINYL)
        self.assertEqual(o._resolve_cue_images(self.cue, cue), self.sides)
        self.sides[1].unlink()
        self.assertIsNone(o._resolve_cue_images(self.cue, cue))

    def test_the_sheet_needs_splitting_not_a_pre_split_hand_off(self):
        self.assertTrue(_orch()._cue_is_single_image(self.cue))

    def test_every_image_goes_with_the_sheet(self):
        o = _orch()
        o._image_set = {self.sides[0]: list(self.sides)}
        o._delete_originals(self.cue, self.sides[0])
        self.assertEqual(sorted(os.listdir(self.dir)), [])

    def test_a_sheet_given_up_before_gets_one_more_try(self):
        o = _orch()
        sig = o._cue_signature(self.cue)
        old = time.time() - 86400 * 3
        if old >= o._MULTI_IMAGE_SINCE:
            old = o._MULTI_IMAGE_SINCE - 86400
        o._cue_led = {str(self.cue): [sig, old, "failed", 3]}
        self.assertIsNone(o._cue_ledger_verdict(self.cue))
        o._cue_led = {str(self.cue): [sig, old, "failed", 4]}
        self.assertIn("gave up", o._cue_ledger_verdict(self.cue))


if __name__ == "__main__":
    unittest.main()
