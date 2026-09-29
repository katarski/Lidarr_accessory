"""No secret leaves through the log, the persisted state or the WebUI: the log
and /api/settings are served on the LAN without authentication, and a Prowlarr
grab link is .../download?apikey=<key>."""
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prowlarr  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
URL = "http://prowlarr:9696/1/download?apikey=%s&link=abc&file=Album" % KEY


class Redact(unittest.TestCase):
    def test_query_secrets_are_masked_and_the_rest_kept(self):
        out = prowlarr.redact(URL)
        self.assertNotIn(KEY, out)
        self.assertIn("apikey=***", out)
        self.assertIn("link=abc", out)
        for k in ("api_key", "token", "access_token", "password", "passkey", "ApiKey"):
            self.assertNotIn("s3cret", prowlarr.redact("x?%s=s3cret&y=1" % k))

    def test_a_magnet_is_unchanged(self):
        m = "magnet:?xt=urn:btih:%s&dn=Some+Album" % ("a" * 40)
        self.assertEqual(prowlarr.redact(m), m)

    def test_the_result_guid_has_no_key_but_the_add_url_does(self):
        rec = {"downloadUrl": URL, "title": "Artist - Album", "seeders": 5,
               "indexerId": 1, "protocol": "torrent"}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return [rec]

        c = prowlarr.ProwlarrClient.__new__(prowlarr.ProwlarrClient)
        c.base, c.api_key, c.indexer_ids, c.timeout = "http://p", KEY, [], 5
        c.session = type("S", (), {"get": lambda self, *a, **k: R()})()
        rows = c.search("Artist Album", require_magnet=False)
        self.assertEqual(len(rows), 1)
        self.assertNotIn(KEY, rows[0]["guid"])
        self.assertEqual(rows[0]["grab_url"], URL)


class LogFilter(unittest.TestCase):
    def test_every_handler_line_is_redacted(self):
        import main
        f = main._RedactSecrets()
        rec = logging.LogRecord("cue_pipeline", logging.INFO, __file__, 1,
                                "adding %s", (URL,), None)
        self.assertTrue(f.filter(rec))
        self.assertNotIn(KEY, rec.getMessage())


class SettingsApi(unittest.TestCase):
    def orch(self, overrides):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o._raw_cfg = {"lidarr": {"prowlarr_api_key": KEY, "api_key": KEY}}
        o.cfg = type("C", (), {"webui_overrides_file": overrides})()
        return o

    def test_get_never_returns_a_secret_value(self):
        rows = self.orch(None).get_settings()
        self.assertNotIn(KEY, json.dumps(rows))
        secret_rows = [r for r in rows if r["id"].endswith("api_key")]
        for r in secret_rows:
            self.assertEqual(r["value"], "(set)")

    def test_saving_the_placeholder_keeps_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "webui_overrides.json"
            o = self.orch(ov)
            ids = [r["id"] for r in o.get_settings() if r["id"].endswith("api_key")]
            if not ids:
                self.skipTest("no secret in the settings schema")
            o.save_settings({i: "(set)" for i in ids})
            saved = json.loads(ov.read_text()) if ov.exists() else {}
            self.assertNotIn("(set)", json.dumps(saved))


class IsearchState(unittest.TestCase):
    def test_keys_saved_before_the_fix_are_redacted_on_load(self):
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "interactive_search.json"
            p.write_text(json.dumps({"12": {"blocklisted": [URL]}}))
            o = Orchestrator.__new__(Orchestrator)
            o.cfg = type("C", (), {"interactive_search_state_file": p})()
            st = o._load_isearch_state()
        self.assertEqual(st["12"]["blocklisted"], [prowlarr.redact(URL)])


if __name__ == "__main__":
    unittest.main()
