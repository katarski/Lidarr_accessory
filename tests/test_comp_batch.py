"""A compilation-hunt batch is tried whole (orch3 COMP-BATCH-1): with
comp_hunt_grabs_per_pass > 1 the hunt sliced that many candidates off its
queue, but the grab tried only the first; the rest were never tried and never
queued again."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(interactive_search_max_candidates=5)
    o.lidarr = SimpleNamespace(release_grab=lambda guid, idx: False)

    def no_qbt():
        raise RuntimeError("no qBittorrent")
    o._get_qbt = no_qbt
    return o


CANDS = [{"guid": "http://x/%d.torrent" % i, "indexerId": 1,
          "title": "Comp %d" % i} for i in range(3)]


class GrabCap(unittest.TestCase):

    def test_the_compilation_hunt_tries_its_whole_batch(self):
        hunt = {}
        _orch()._assembly_grab_for_songs(1, "A", ["Song"], CANDS[:2], hunt=hunt,
                                         cap=2)
        self.assertEqual(hunt["tried"], [c["guid"] for c in CANDS[:2]])

    def test_a_resumed_hunt_still_takes_one_per_pass(self):
        hunt = {}
        _orch()._assembly_grab_for_songs(1, "A", ["Song"], CANDS, hunt=hunt)
        self.assertEqual(hunt["tried"], [CANDS[0]["guid"]])


if __name__ == "__main__":
    unittest.main()
