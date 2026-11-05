"""Lidarr failures are also counted per thread, and the song harvest judges
its pass by its own thread's: with the shared count, a ManualImport timing
out on another thread while the wanted index was built threw every verdict of
the pass away -- seventeen needs-attention folders scanned pass after pass and
never recorded as judged (5 Nov 2026)."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import song_harvest as SH  # noqa: E402
from lidarr import LidarrClient  # noqa: E402


def _client():
    c = LidarrClient.__new__(LidarrClient)
    c.failure_generation = 0
    c._thread_fail = threading.local()
    c._down_lock = threading.Lock()
    return c


class ThreadFailures(unittest.TestCase):
    def test_another_threads_failure_is_not_this_threads(self):
        c = _client()

        def fail():
            with c._down_lock:
                c.failure_generation += 1
                c._count_thread_failure()

        t = threading.Thread(target=fail)
        t.start()
        t.join()
        self.assertEqual(c.failure_generation, 1)
        self.assertEqual(c.thread_failures(), 0)
        self.assertEqual(SH._failures(c), 0)
        fail()
        self.assertEqual(c.thread_failures(), 1)
        self.assertEqual(SH._failures(c), 1)

    def test_a_client_without_the_thread_count_falls_back(self):
        class Fake:
            failure_generation = 3
        self.assertEqual(SH._failures(Fake()), 3)

    def test_the_went_down_path_counts_for_the_thread(self):
        c = _client()
        c._down_until, c._down_backoff = 0.0, 0.0
        try:
            c._went_down("test")
        except Exception:  # noqa: BLE001
            pass
        self.assertEqual(c.thread_failures(), c.failure_generation)


if __name__ == "__main__":
    unittest.main()
