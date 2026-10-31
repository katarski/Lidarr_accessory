"""The Assembly tab's Find missing answers at once (the owner, 31 Oct 2026:
"when i go to assembly nd I hit find missing, I am not sure anythign
happens"): the hunt runs on a background thread and its state stays on the
plan, where the tab shows it."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


def _orch(plans):
    o = Orchestrator.__new__(Orchestrator)
    o.assembly = SimpleNamespace(
        get=lambda i: dict(plans[i]) if i in plans else None,
        upsert=lambda i, p: plans.__setitem__(i, p))
    o._asm_find_loop = lambda: None        # no worker thread in a test
    return o


class FindMissingAnswersAtOnce(unittest.TestCase):
    def test_start_queues_the_album_and_answers(self):
        plans = {1: {"artist": "A", "album": "B", "missing": [{"track": "S"}]}}
        o = _orch(plans)
        self.assertEqual(o.assembly_find_missing_start(1),
                         (True, "search started"))
        self.assertEqual(plans[1]["hunt"]["state"], "searching")
        self.assertEqual(o._asm_find_state()["queue"].get_nowait(), 1)
        self.assertEqual(o.assembly_find_missing_start(1),
                         (True, "already searching"))

    def test_nothing_to_do_is_refused_at_once(self):
        plans = {1: {"artist": "A", "album": "B", "missing": []}}
        o = _orch(plans)
        ok, msg = o.assembly_find_missing_start(1)
        self.assertFalse(ok)
        self.assertIn("nothing missing", msg)
        self.assertEqual(o.assembly_find_missing_start(2),
                         (False, "no plan for that album"))
        self.assertTrue(o._asm_find_state()["queue"].empty())

    def test_a_refusal_is_kept_on_the_row(self):
        plans = {1: {"artist": "A", "album": "B", "missing": [{"track": "S"}],
                     "hunt": {"active": True, "tried": ["g0"]}}}
        o = _orch(plans)
        o.assembly_find_missing = lambda i: (
            False, "your indexers offered no torrent with at least 1 seeder(s)")
        o._assembly_find_job(1)
        hunt = plans[1]["hunt"]
        self.assertEqual(hunt["state"], "stopped")
        self.assertFalse(hunt["active"])
        self.assertIn("no torrent", hunt["note"])
        self.assertEqual(hunt["tried"], ["g0"])
        self.assertIn("finished", hunt)

    def test_a_hunt_that_goes_on_says_so(self):
        plans = {1: {"artist": "A", "album": "B", "missing": [{"track": "S"}]}}
        o = _orch(plans)

        def find(i):
            p = plans[i]
            hunt = dict(p.get("hunt") or {})
            hunt.update(active=True, tried=["g1"], candidates=7)
            p["hunt"] = hunt
            plans[i] = p
            return True, "grabbed 'Best Of' to look for 1 missing song(s)"
        o.assembly_find_missing = find
        o._assembly_find_job(1)
        hunt = plans[1]["hunt"]
        self.assertEqual(hunt["state"], "hunting")
        self.assertTrue(hunt["ok"])
        self.assertIn("grabbed", hunt["note"])

    def test_a_dismissed_album_is_left_alone(self):
        plans = {}
        o = _orch(plans)
        self.assertEqual(o._assembly_find_job(1),
                         (False, "no plan for that album"))


if __name__ == "__main__":
    unittest.main()
