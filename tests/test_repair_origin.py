"""When a CUE's audio was split from a lead-in-repaired copy, the download's
own file is what the clean-up deletes, not only the copy: the original
'.iso.wv' was left behind, the cueless sweep extracted its embedded cuesheet
again, and the job re-ran every few minutes (a 342 MB repair each time)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class RepairOrigin(unittest.TestCase):

    def test_the_original_goes_with_its_repaired_copy(self):
        with tempfile.TemporaryDirectory() as d:
            cue = Path(d) / "A.iso.cue"
            orig = Path(d) / "A.iso.wv"
            copy = Path(d) / "A.iso.cuefix.wv"
            for p in (cue, orig, copy):
                p.write_bytes(b"x")
            o = Orchestrator.__new__(Orchestrator)
            o._repair_origin = {copy: orig}
            o._delete_originals(cue, copy)
            self.assertEqual(sorted(os.listdir(d)), [])

    def test_a_plain_split_deletes_what_it_always_did(self):
        with tempfile.TemporaryDirectory() as d:
            cue, audio, other = (Path(d) / n for n in ("A.cue", "A.flac", "B.flac"))
            for p in (cue, audio, other):
                p.write_bytes(b"x")
            o = Orchestrator.__new__(Orchestrator)
            o._delete_originals(cue, audio)
            self.assertEqual(os.listdir(d), ["B.flac"])


if __name__ == "__main__":
    unittest.main()
