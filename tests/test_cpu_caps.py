"""The WebUI sets the CPU cap of this container and of Lidarr live, through
Docker's update call (no restart), and a cap it set is applied again at the
next start (a deploy recreates this container from its template)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class CpuCaps(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.calls = []
        self.o = Orchestrator.__new__(Orchestrator)
        self.o.cfg = SimpleNamespace(
            webui_overrides_file=self.dir / "webui_overrides.json",
            container_name="cue_pipeline")
        self.o._cpu_containers = lambda: {"pipeline": ["cue_pipeline"],
                                          "lidarr": ["lidarr"]}

        def docker(method, path, body=None, timeout=15.0):
            self.calls.append((method, path, body))
            if method == "GET":
                return 200, json.dumps({"HostConfig": {
                    "NanoCpus": 500000000, "CpusetCpus": "1,7"}})
            return 200, "{}"
        self.o._docker = docker

    def test_both_caps_are_applied_live_and_saved(self):
        ok, msg = self.o.set_cpu_caps({"pipeline": "0.75", "lidarr": 1.5})
        self.assertTrue(ok, msg)
        self.assertEqual(self.calls, [
            ("POST", "/containers/cue_pipeline/update", {"NanoCpus": 750000000}),
            ("POST", "/containers/lidarr/update", {"NanoCpus": 1500000000})])
        self.assertEqual(self.o._load_cpu_caps(),
                         {"pipeline": 0.75, "lidarr": 1.5})

    def test_a_cap_out_of_range_is_refused_untouched(self):
        for bad in ("0", "-1", str((os.cpu_count() or 1) + 1), "lots"):
            ok, _msg = self.o.set_cpu_caps({"lidarr": bad})
            self.assertFalse(ok, bad)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.o._load_cpu_caps(), {})

    def test_the_tab_shows_what_runs_now(self):
        caps = self.o.cpu_caps()
        self.assertEqual((caps["pipeline"]["cpus"], caps["pipeline"]["cpuset"]),
                         (0.5, "1,7"))
        self.assertEqual(caps["lidarr"]["cpus"], 0.5)

    def test_a_saved_cap_is_applied_again_at_start(self):
        import main
        src = Path(main.__file__).read_text(encoding="utf-8")
        self.assertIn("orch.set_cpu_caps(_caps, save=False)", src)

    def test_only_this_container_and_lidarr_can_be_capped(self):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(container_name="cue_pipeline")
        names = o._cpu_containers()
        self.assertEqual(set(names), {"pipeline", "lidarr"})


if __name__ == "__main__":
    unittest.main()
