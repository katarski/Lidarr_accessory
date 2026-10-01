"""The audit pass summary says what was done. It read "531 discrepancies
found, 531 DownloadedAlbumsScan actions triggered" when nearly all of them
were "unchanged -- not repeated" or "deferred" and no scan ran."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class Summary(unittest.TestCase):

    def test_outcomes_are_counted_by_kind(self):
        rows = [["", "x", "unchanged since it was last acted on (y) -- not repeated"],
                ["", "x", "unchanged since it was last acted on (z) -- not repeated"],
                ["", "x", "deferred (per-pass limit)"],
                ["", "x", "exception: boom"],
                ["", "x", "switched to the 12-track release 1 (12/12 titles)"],
                ["", "x", "DownloadedAlbumsScan cmd=5"]]
        self.assertEqual(
            Orchestrator._audit_outcome_summary(rows),
            "6 discrepancies found: 2 handled this pass, 2 unchanged since "
            "last handled, 1 deferred, 1 failed")

    def test_the_pass_line_uses_it(self):
        import inspect
        src = inspect.getsource(Orchestrator)
        self.assertIn("self._audit_outcome_summary(discrepancies)", src)
        self.assertNotIn('"%d discrepancies found, %d DownloadedAlbumsScan', src)


if __name__ == "__main__":
    unittest.main()
