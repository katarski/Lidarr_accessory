"""A CUE given up under the old rules gets one more try under split-once."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class SplitOnceRetry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cue = Path(self.tmp.name) / "a.cue"
        self.cue.write_text('FILE "a.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
        self.o = Orchestrator.__new__(Orchestrator)
        self.o.cfg = SimpleNamespace(cue_ledger_enabled=True, cue_ledger_max_attempts=3)

    def tearDown(self):
        self.tmp.cleanup()

    def verdict(self, ts, attempts):
        sig = self.o._cue_signature(self.cue)
        self.o._cue_led = {str(self.cue): [sig, ts, "failed", attempts]}
        return self.o._cue_ledger_verdict(self.cue)

    def test_given_up_before_split_once_is_tried_again(self):
        self.assertIsNone(self.verdict(Orchestrator._SPLIT_ONCE_SINCE - 60, 3))

    def test_given_up_after_it_stays_given_up(self):
        self.assertIn("gave up", self.verdict(Orchestrator._SPLIT_ONCE_SINCE + 60, 3))
        self.assertIn("gave up", self.verdict(Orchestrator._SPLIT_ONCE_SINCE - 60, 4))


if __name__ == "__main__":
    unittest.main()
