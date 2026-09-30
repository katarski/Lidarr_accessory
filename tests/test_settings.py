"""The WebUI Settings tab and the config layers under it (config.yaml <
container variables < webui_overrides.json): a saved tab value is a
deliberate change, the tab shows what actually runs, and the template keeps
control of everything the owner did not change."""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _orch(overrides, base, raw=None):
    from orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._raw_cfg = json.loads(json.dumps(raw if raw is not None else base))
    o._base_cfg = base
    o.cfg = type("C", (), {"webui_overrides_file": overrides})()
    return o


class SaveKeepsOnlyChanges(unittest.TestCase):
    """orch3 SETTINGS-SAVE-1: the tab posted every field and save_settings
    wrote each one, so the overrides file held all 125 keys and every
    template variable was frozen (ISEARCH_INTERVAL 3600 lost to 1000)."""

    BASE = {"lidarr": {"interactive_search_interval_seconds": 3600,
                       "min_match_percent": 50},
            "staging": {"delete_source_folder_on_success": True}}

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.ov = Path(d.name) / "webui_overrides.json"

    def saved(self):
        return json.loads(self.ov.read_text(encoding="utf-8"))

    def test_an_echo_of_the_template_value_is_not_frozen(self):
        o = _orch(self.ov, self.BASE)
        ok, msg = o.save_settings({
            "lidarr.interactive_search_interval_seconds": "3600",
            "lidarr.min_match_percent": "50",          # float row, int in yaml
            "staging.delete_source_folder_on_success": True,
        })
        self.assertTrue(ok, msg)
        self.assertEqual(self.saved(), {})

    def test_a_real_change_is_kept(self):
        o = _orch(self.ov, self.BASE)
        o.save_settings({"lidarr.interactive_search_interval_seconds": "1800"})
        self.assertEqual(self.saved(), {
            "lidarr": {"interactive_search_interval_seconds": 1800}})

    def test_setting_the_template_value_hands_the_key_back(self):
        self.ov.write_text(json.dumps(
            {"lidarr": {"interactive_search_interval_seconds": 1000}}))
        o = _orch(self.ov, self.BASE,
                  raw={"lidarr": {"interactive_search_interval_seconds": 1000}})
        ok, msg = o.save_settings(
            {"lidarr.interactive_search_interval_seconds": "3600"})
        self.assertEqual(self.saved(), {})
        self.assertIn("1 now follow", msg)
        self.assertEqual(o._raw_cfg["lidarr"]["interactive_search_interval_seconds"],
                         3600)

    def test_echoes_saved_before_the_fix_go_on_the_next_save(self):
        # The live file: every schema key, most equal to what the template
        # gives. Any save clears those; the real differences stay.
        self.ov.write_text(json.dumps({
            "lidarr": {"interactive_search_interval_seconds": 1000,
                       "min_match_percent": 50.0,
                       "interactive_search_max_albums_per_pass": 15},
            "staging": {"delete_source_folder_on_success": True}}))
        o = _orch(self.ov, self.BASE)
        ok, _m = o.save_settings({})
        self.assertTrue(ok)
        self.assertEqual(self.saved(), {"lidarr": {
            "interactive_search_interval_seconds": 1000,
            # nothing below sets it: kept, its fallback is main.py's to decide
            "interactive_search_max_albums_per_pass": 15}})

    def test_without_a_base_nothing_is_dropped(self):
        o = _orch(self.ov, {})
        o.save_settings({"lidarr.interactive_search_interval_seconds": "3600"})
        self.assertEqual(self.saved(), {
            "lidarr": {"interactive_search_interval_seconds": 3600}})

    def test_the_tab_shows_which_values_are_saved_here(self):
        self.ov.write_text(json.dumps(
            {"lidarr": {"interactive_search_interval_seconds": 1000}}))
        o = _orch(self.ov, self.BASE,
                  raw={"lidarr": {"interactive_search_interval_seconds": 1000}})
        rows = {r["id"]: r for r in o.get_settings()}
        r = rows["lidarr.interactive_search_interval_seconds"]
        self.assertEqual((r["value"], r["overridden"], r["base"]), (1000, True, 3600))
        r = rows["lidarr.min_match_percent"]
        self.assertEqual((r["overridden"], r["base"]), (False, 50))

    def test_the_page_posts_only_edited_fields(self):
        import webui
        js = webui._PAGE[webui._PAGE.index("function saveSettings("):]
        js = js[:js.index("\nfunction ", 1)]
        self.assertIn("getAttribute('data-orig'))return;", js)
        self.assertIn("data-orig=", webui._PAGE)


def _main_defaults():
    """{(section, key): [literal defaults]} from `<x>_cfg.get("key", default)`
    in main.py, the defaults the pipeline actually runs with."""
    tree = ast.parse(Path(ROOT, "main.py").read_text(encoding="utf-8"))
    sections = {"qcfg": "qbittorrent"}      # the deselect loop's parameter
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and isinstance(n.targets[0], ast.Name):
            v = n.value.values[0] if isinstance(n.value, ast.BoolOp) else n.value
            if isinstance(v, ast.Call) and getattr(v.func, "attr", "") == "get" \
                    and v.args and isinstance(v.args[0], ast.Constant):
                sections.setdefault(n.targets[0].id, v.args[0].value)
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "get" \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id in sections and len(n.args) == 2 \
                and isinstance(n.args[0], ast.Constant):
            try:
                d = ast.literal_eval(n.args[1])
            except ValueError:
                continue                    # a nested fallback, not a literal
            out.setdefault((sections[n.func.value.id], n.args[0].value), []).append(d)
    return out


class DefaultsAreMainsDefaults(unittest.TestCase):
    def test_the_tab_shows_the_default_that_runs(self):
        # The tab showed "Interactive search: on, dry run: off" for an unset
        # key while main.py ran it off and dry. A Save of the whole form then
        # switched it on for real.
        from orchestrator import Orchestrator
        found = _main_defaults()
        checked = 0
        for sid, section, key, _l, typ, default, _h in Orchestrator._SETTINGS_SCHEMA:
            for d in found.get((section, key), []):
                if typ in ("int", "float"):
                    self.assertEqual(float(d), float(default), sid)
                else:
                    self.assertEqual(d, default, sid)
                checked += 1
        self.assertGreater(checked, 90)


class OverrideWarning(unittest.TestCase):
    def test_logging_is_configured_before_the_overrides_are_applied(self):
        # Configured after, the "overrides the container variable" warning
        # went to an unconfigured logger: 0 lines in pipeline.log.
        tree = ast.parse(Path(ROOT, "main.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                first[n.func.id] = min(first.get(n.func.id, n.lineno), n.lineno)
        self.assertLess(first["configure_logging"], first["apply_webui_overrides"])

    def test_a_per_pass_setting_is_not_treated_as_a_secret(self):
        import main
        os.environ["ISEARCH_MAX_ALBUMS"] = "15"
        try:
            cfg = main.apply_env_overrides({})
            with tempfile.TemporaryDirectory() as d:
                ov = Path(d) / "webui_overrides.json"
                ov.write_text(json.dumps(
                    {"lidarr": {"interactive_search_max_albums_per_pass": 40}}))
                with self.assertLogs("cue_pipeline", "WARNING") as cm:
                    main.apply_webui_overrides(cfg, ov)
        finally:
            del os.environ["ISEARCH_MAX_ALBUMS"]
            main._ENV_SET.pop(("lidarr", "interactive_search_max_albums_per_pass"),
                              None)
        self.assertIn("(40)", cm.output[0])
        self.assertIn("(15)", cm.output[0])


class EnvIsParsedByTheSchemaType(unittest.TestCase):
    """orch3 ENV-BOOL-1: five env overrides were cast with bool(), so
    HARVEST_ENABLED=false turned the harvest on."""

    def _env_of_bool_rows(self):
        import main
        from orchestrator import Orchestrator
        bools = {(sec, k) for _i, sec, k, _l, typ, _d, _h
                 in Orchestrator._SETTINGS_SCHEMA if typ == "bool"}
        src = Path(main.__file__).read_text(encoding="utf-8")
        out = {}
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "put"
                    and len(n.args) >= 3
                    and all(isinstance(a, ast.Constant) for a in n.args[:3])):
                sec, k, env = (a.value for a in n.args[:3])
                if (sec, k) in bools:
                    out[env] = (sec, k)
        return out

    def _apply(self, env, value):
        import main
        os.environ[env] = value
        try:
            return main.apply_env_overrides({})
        finally:
            del os.environ[env]
            main._ENV_SET.clear()

    def test_false_is_false_for_every_boolean_variable(self):
        rows = self._env_of_bool_rows()
        for env in ("HARVEST_ENABLED", "HARVEST_DRY_RUN", "COMP_HUNT_ENABLED",
                    "COMP_HUNT_LIDARR_INDEXERS_ONLY", "RECHECK_SKIP_UNCHANGED"):
            self.assertIn(env, rows)
        for env, (sec, k) in rows.items():
            for text, want in (("false", False), ("0", False), ("no", False),
                               ("true", True), ("1", True)):
                self.assertIs(self._apply(env, text)[sec][k], want,
                              "%s=%s" % (env, text))

    def test_a_number_follows_its_schema_type(self):
        self.assertEqual(
            self._apply("MIN_MATCH_PERCENT", "50")["lidarr"]["min_match_percent"],
            50.0)


if __name__ == "__main__":
    unittest.main()
