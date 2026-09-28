"""The startup path's contract: main.py builds OrchestratorConfig with keyword
arguments, so the class must stay a dataclass and every keyword main passes
must be one of its fields. (A class inserted above it once took its
@dataclass decorator, and the container crash-looped on start.)"""
import ast
import dataclasses
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class Startup(unittest.TestCase):
    def test_orchestrator_config_accepts_what_main_passes(self):
        from orchestrator import OrchestratorConfig
        self.assertTrue(dataclasses.is_dataclass(OrchestratorConfig))
        fields = {f.name for f in dataclasses.fields(OrchestratorConfig)}
        tree = ast.parse(open(os.path.join(ROOT, "main.py"), encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "OrchestratorConfig"]
        self.assertTrue(calls)
        for c in calls:
            passed = {k.arg for k in c.keywords if k.arg}
            self.assertEqual(passed - fields, set())

    def test_every_module_imports(self):
        import importlib
        for f in sorted(os.listdir(ROOT)):
            if f.endswith(".py"):
                importlib.import_module(f[:-3])

    def test_a_settings_value_beating_a_template_variable_is_reported(self):
        import json
        import tempfile
        from pathlib import Path
        import main
        os.environ["LLM_MODEL"] = "from-template"
        try:
            cfg = main.apply_env_overrides({"ollama": {"model": "from-yaml"}})
            with tempfile.TemporaryDirectory() as d:
                ov = Path(d) / "webui_overrides.json"
                ov.write_text(json.dumps({"ollama": {"model": "stale-saved"}}))
                with self.assertLogs("cue_pipeline", "WARNING") as cm:
                    cfg = main.apply_webui_overrides(cfg, ov)
            self.assertEqual(cfg["ollama"]["model"], "stale-saved")
            self.assertIn("LLM_MODEL", cm.output[0])
        finally:
            del os.environ["LLM_MODEL"]


if __name__ == "__main__":
    unittest.main()
