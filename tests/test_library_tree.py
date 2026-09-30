"""The Converter tab's library tree is refreshed when someone looks at it,
not by an hourly timer that stat'ed all ~95k library files (loops F14)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter import LibraryTree  # noqa: E402


class OnDemand(unittest.TestCase):

    def test_no_timer_walks_the_library(self):
        import main
        src = Path(main.__file__).read_text(encoding="utf-8")
        self.assertNotIn("cue-lib-rescan", src)
        self.assertNotIn("maybe_scan", src)

    def test_a_stale_tree_is_rescanned_once_a_fresh_one_not_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "lib"
            (root / "A" / "B").mkdir(parents=True)
            (root / "A" / "B" / "01.flac").write_bytes(b"x")
            lt = LibraryTree(root, Path(d) / "tree.json")
            lt._scanned_ts = time.time()
            self.assertFalse(lt.refresh_in_background(3600))      # fresh
            lt._scanned_ts = time.time() - 7200
            started = lt.refresh_in_background(3600)
            again = lt.refresh_in_background(3600)                # one at a time
            for _ in range(50):
                if lt._scanned_ts > time.time() - 60:
                    break
                time.sleep(0.1)
            self.assertEqual((started, again), (True, False))
            self.assertGreater(lt._scanned_ts, time.time() - 60)  # it ran


if __name__ == "__main__":
    unittest.main()
