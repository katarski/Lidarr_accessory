"""The one CUE worker drops a queued .cue whose folder is gone, at once
(loops F11): a missing file used to count as "still being written" for at
least 60 s, 120 s for the two CUEs of a Cream folder the lifecycle removed."""
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402


class _Clock:
    """monotonic() that moves one second per call; sleep() is free."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        self.t += 1.0
        return self.t

    def sleep(self, s):
        pass


class GoneIsGone(unittest.TestCase):

    def test_the_stability_wait_says_gone_at_once(self):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(stable_seconds=10)
        clock = _Clock()
        with mock.patch.object(orchestrator.time, "monotonic", clock.monotonic), \
             mock.patch.object(orchestrator.time, "sleep", clock.sleep):
            self.assertIsNone(o._wait_for_stability(Path("/nonexistent/x.cue")))
        self.assertLess(clock.t, 5)            # no waiting out the deadline

    def test_a_file_that_is_there_still_waits_for_stability(self):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(stable_seconds=3)
        clock = _Clock()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.cue"
            p.write_text("x")
            with mock.patch.object(orchestrator.time, "monotonic", clock.monotonic), \
                 mock.patch.object(orchestrator.time, "sleep", clock.sleep):
                self.assertTrue(o._wait_for_stability(p))

    def test_the_worker_drops_a_gone_cue_and_forgets_it(self):
        import main
        q = queue.Queue()
        stop = threading.Event()
        processed = []
        seen = {"/gone/a.cue"}
        q.put(Path("/gone/a.cue"))
        orch = SimpleNamespace(process=lambda p: processed.append(p))
        t = threading.Thread(target=main.worker_loop, args=(q, orch, stop, seen))
        t.start()
        for _ in range(50):              # q.join() would hang if the worker died
            if not q.unfinished_tasks:
                break
            stop.wait(0.1)
        stop.set()
        t.join(5)
        self.assertEqual(processed, [])
        self.assertEqual(seen, set())


if __name__ == "__main__":
    unittest.main()
