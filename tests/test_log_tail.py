"""The Log tab's 'last N lines' comes from the live log while it has them
(orch3 LOG-TAIL-1): a guessed need*220 bytes held fewer lines when lines were
long, and the view spliced the rotated log's tail in front of a partial live
tail."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class LogTail(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.log = Path(d.name) / "pipeline.log"
        self.o = Orchestrator.__new__(Orchestrator)
        self.o.cfg = SimpleNamespace(log_file=str(self.log))

    def write(self, path, tag, n, width):
        path.write_bytes(b"".join(
            (b"%s %05d " % (tag, i)).ljust(width, b"x") + b"\n"
            for i in range(n)))

    def test_long_lines_come_from_the_live_log_only(self):
        self.write(Path(str(self.log) + ".1"), b"OLD", 500, 300)
        self.write(self.log, b"NEW", 1000, 300)
        lines = self.o.read_log(400).splitlines()
        self.assertEqual(len(lines), 400)
        self.assertTrue(all(l.startswith("NEW") for l in lines))
        self.assertTrue(lines[0].startswith("NEW 00600"))
        self.assertTrue(lines[-1].startswith("NEW 00999"))

    def test_a_short_live_log_continues_into_the_rotation(self):
        self.write(Path(str(self.log) + ".1"), b"OLD", 500, 50)
        self.write(self.log, b"NEW", 100, 50)
        lines = self.o.read_log(150).splitlines()
        self.assertEqual(len(lines), 150)
        self.assertTrue(lines[0].startswith("OLD 00450"))
        self.assertTrue(lines[49].startswith("OLD 00499"))
        self.assertTrue(lines[50].startswith("NEW 00000"))

    def test_whole_file_mode(self):
        self.write(self.log, b"NEW", 10, 20)
        self.assertEqual(len(self.o.read_log(0).splitlines()), 10)


if __name__ == "__main__":
    unittest.main()
