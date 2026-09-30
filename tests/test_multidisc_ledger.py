"""A multi-disc album handed off at its parent is skipped disc by disc.

The sweep discovers CD1..CD4 as separate folders and asks the ledger about
each; only the parent was recorded, so every pass (and every restart) handed
the same refused 4-CD album to Lidarr again."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(sweep_ledger_enabled=True, sweep_ledger_file=None,
                            sweep_ledger_ttl_seconds=86400)
    o._sweep_led = {}
    o._sweep_led_dirty = False
    return o


class MultiDiscLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.album = Path(self.tmp.name) / "2005 - Album [2xCD]"
        self.discs = {}
        for d in ("CD1", "CD2"):
            (self.album / d).mkdir(parents=True)
            files = []
            for i in (1, 2):
                p = self.album / d / ("%02d. Song.mp3" % i)
                p.write_bytes(b"x")
                files.append(p)
            self.discs[d] = files

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_disc_is_skipped_after_the_album_is_marked(self):
        o = _orch()
        now = time.time()
        allaudio = self.discs["CD1"] + self.discs["CD2"]
        o._sweep_ledger_mark(self.album, allaudio, now)
        self.assertTrue(o._sweep_ledger_skip(self.album, allaudio, now))
        for d, files in self.discs.items():
            self.assertTrue(o._sweep_ledger_skip(self.album / d, files, now), d)

    def test_a_changed_disc_is_found_again(self):
        o = _orch()
        now = time.time()
        o._sweep_ledger_mark(self.album, self.discs["CD1"] + self.discs["CD2"], now)
        extra = self.album / "CD2" / "03. New.mp3"
        extra.write_bytes(b"y")
        self.assertFalse(o._sweep_ledger_skip(
            self.album / "CD2", self.discs["CD2"] + [extra], now))
        self.assertTrue(o._sweep_ledger_skip(
            self.album / "CD1", self.discs["CD1"], now))

    def test_a_flat_folder_records_only_itself(self):
        o = _orch()
        now = time.time()
        o._sweep_ledger_mark(self.album / "CD1", self.discs["CD1"], now)
        self.assertEqual(list(o._sweep_led), [str(self.album / "CD1")])


if __name__ == "__main__":
    unittest.main()
