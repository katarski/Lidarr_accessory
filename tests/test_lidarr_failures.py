"""Lidarr failing is counted, fails fast while it lasts, and nothing concluded
during a failed hand-off is recorded as a verdict."""
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402


class Inner:
    def __init__(self):
        self.headers, self.calls, self.fail = {}, 0, None

    def request(self, method, url, **kw):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        r = requests.Response()
        r.status_code, r._content = 200, b"[]"
        return r


def client():
    cfg = SimpleNamespace(base_url="http://lidarr", api_key="k",
                          path_mapping_from="", path_mapping_to="",
                          library_root_lidarr="/music", library_root_windows="/music",
                          manualimport_timeout=60)
    inner = Inner()
    return L.LidarrClient(cfg, session=inner), inner


class Breaker(unittest.TestCase):
    def test_failure_is_counted_and_fails_fast_until_the_probe(self):
        c, inner = client()
        self.assertEqual(c._get("/api/v1/artist"), [])
        self.assertEqual(c.failure_generation, 0)
        inner.fail = requests.ConnectionError("refused")
        with self.assertRaises(requests.ConnectionError):
            c._get("/api/v1/artist")
        g = c.failure_generation
        self.assertGreater(g, 0)
        self.assertFalse(c.available())
        with self.assertRaises(L.LidarrUnavailable):
            c._get("/api/v1/artist")
        self.assertEqual(inner.calls, 2)            # the fast failure sent nothing
        self.assertGreater(c.failure_generation, g)
        inner.fail = None
        c._down_until = 0.0                          # backoff elapsed
        self.assertEqual(c._get("/api/v1/artist"), [])
        self.assertTrue(c.available())
        self.assertEqual(c._down_backoff, 0.0)

    def test_read_timeout_counts_but_does_not_open_the_breaker(self):
        c, inner = client()
        inner.fail = requests.exceptions.ReadTimeout()
        with self.assertRaises(requests.exceptions.ReadTimeout):
            c._get("/x")
        self.assertEqual(c.failure_generation, 1)
        self.assertTrue(c.available())

    def test_artist_list_keeps_the_stale_copy(self):
        c, inner = client()
        c._artists, c._artists_at = [{"id": 1}], 0.0
        inner.fail = requests.ConnectionError()
        self.assertEqual(c.artists(), [{"id": 1}])


class HandoffRecords(unittest.TestCase):
    def stub(self):
        from orchestrator import Orchestrator
        lid = SimpleNamespace(failure_generation=0)
        rows = []
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        s = SimpleNamespace(lidarr=lid, _tl=threading.local(),
                            _ledger_lock=threading.Lock(),
                            cfg=SimpleNamespace(ledger_file=Path(self.tmp.name) / "l.csv"))
        s._OUTCOMES_REAL_UNDER_FAILURE = Orchestrator._OUTCOMES_REAL_UNDER_FAILURE
        for n in ("_lidarr_generation", "_handoff_pre_split_to_lidarr",
                  "_lidarr_failed_in_last_handoff", "_record"):
            setattr(s, n, getattr(Orchestrator, n).__get__(s))
        s._cue_ledger_mark = lambda p, o: rows.append(o)
        s._update_held = lambda *a, **k: None
        return s, lid, rows

    def test_a_verdict_reached_while_lidarr_failed_is_not_recorded(self):
        s, lid, rows = self.stub()

        def inner(cue, folder, reason=""):
            lid.failure_generation += 1              # a Lidarr call failed
            s._record(folder, outcome="skipped_unmonitored", pre_split=True)
        s._handoff_inner = inner
        s._handoff_pre_split_to_lidarr(None, Path("/d/A"), reason="t")
        self.assertEqual(rows, [])
        self.assertTrue(s._lidarr_failed_in_last_handoff())

    def test_facts_are_recorded_and_a_clean_handoff_is_remembered(self):
        s, lid, rows = self.stub()

        def inner(cue, folder, reason=""):
            lid.failure_generation += 1
            s._record(folder, outcome="imported_via_manual", pre_split=True)
        s._handoff_inner = inner
        s._handoff_pre_split_to_lidarr(None, Path("/d/A"), reason="t")
        self.assertEqual(rows, ["imported_via_manual"])

        s._handoff_inner = lambda cue, folder, reason="": s._record(
            folder, outcome="skipped_unmonitored", pre_split=True)
        s._handoff_pre_split_to_lidarr(None, Path("/d/B"), reason="t")
        self.assertEqual(rows, ["imported_via_manual", "skipped_unmonitored"])
        self.assertFalse(s._lidarr_failed_in_last_handoff())


if __name__ == "__main__":
    unittest.main()
