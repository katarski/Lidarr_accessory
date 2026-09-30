"""Reconcile's "nothing importable here" survives a restart.

It lived in memory, so every start re-probed every folder with Lidarr's
/manualimport (10-20s of Lidarr CPU each): 60-173 probes an hour."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


def _orch(state_dir, calls):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(sweep_min_stable_seconds=0, content_identify=False,
                            manual_import_timeout_seconds=5,
                            sweep_ledger_file=Path(state_dir) / "sweep_seen.json")
    o.lidarr = SimpleNamespace(
        failure_generation=0, available=lambda: True,
        list_all_albums=lambda: [{
            "id": 1, "monitored": True, "artist": {"artistName": "Some Artist"},
            "statistics": {"totalTrackCount": 2, "trackFileCount": 0}}],
        manual_import_folder=lambda f, **k: calls.append(f) or (None, 0, []),
        windows_to_lidarr=lambda p: str(p))
    o._release_llm_waiting = lambda: None
    o._llm_blocked = lambda f: False
    return o


class ReconcileVerdictsPersist(unittest.TestCase):

    def test_a_restart_does_not_probe_a_known_folder_again(self):
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as state:
            f = Path(root) / "Some Artist - Album"
            f.mkdir()
            for n in ("01.flac", "02.flac"):
                (f / n).write_bytes(b"x")
            calls = []
            _orch(state, calls).reconcile_monitored_gaps(Path(root))
            self.assertEqual(len(calls), 1)
            _orch(state, calls).reconcile_monitored_gaps(Path(root))   # restart
            self.assertEqual(len(calls), 1)
            (f / "03.flac").write_bytes(b"y")                          # changed
            os.utime(f / "03.flac", (2e9, 2e9))
            _orch(state, calls).reconcile_monitored_gaps(Path(root))
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
