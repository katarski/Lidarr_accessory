"""The WebUI's HTTP surface: a state-changing POST runs only for the page this
server served, never for another site open in the same LAN browser."""
import http.client
import json
import logging
import os
import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


class _Tree:
    def __init__(self):
        self.deleted = []

    def delete(self, paths):
        self.deleted.extend(paths)
        return len(paths), []


class _Actions:
    def __init__(self):
        self.library_tree = _Tree()
        self.saved = []

    def save_settings(self, changes):
        self.saved.append(changes)
        return True, "saved"


class _Store:
    def list(self):
        return []

    def get(self, eid):
        return None


class _Server:
    def __init__(self):
        self.actions = _Actions()
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), webui.make_handler(_Store(), self.actions))
        self.httpd.daemon_threads = True
        self.host = "127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection(self.host, timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def page_token(self):
        status, _h, page = self.request("GET", "/")
        m = re.search(r"var CUE_TOKEN='([^']*)'", page.decode("utf-8"))
        return m.group(1) if m else None


class PostGate(unittest.TestCase):
    """orch3 WEBUI-CSRF-1: any page open on the LAN could delete library
    folders with fetch(..., {mode:'no-cors'}) -- a text/plain POST is a CORS
    'simple' request, sent without a preflight, and the server acted on it."""

    def setUp(self):
        self.s = _Server()
        self.addCleanup(self.s.close)
        self.own = "http://" + self.s.host
        lg = logging.getLogger("webui")
        self.addCleanup(lg.setLevel, lg.level)
        lg.setLevel(logging.CRITICAL)

    def delete(self, headers):
        return self.s.request("POST", "/api/library/delete",
                              body=json.dumps({"paths": ["Some Artist"]}),
                              headers=headers)

    def test_the_audit_cross_site_request_deletes_nothing(self):
        # Exactly what the audit sent: text/plain, another site's Origin.
        with self.assertLogs("webui", "WARNING") as cm:
            status, hdrs, _b = self.delete(
                {"Content-Type": "text/plain;charset=UTF-8",
                 "Origin": "http://evil.example"})
            self.assertEqual(self.s.actions.library_tree.deleted, [])
            self.assertEqual(status, 403)
            self.assertEqual(hdrs.get("X-CUE-Refused"), "origin")
        self.assertIn("refused POST /api/library/delete (origin", cm.output[0])

    def test_the_page_own_post_runs(self):
        tok = self.s.page_token()
        self.assertTrue(tok)
        status, _h, body = self.delete({"Content-Type": "application/json",
                                        "Origin": self.own, "X-CUE-Token": tok})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.s.actions.library_tree.deleted, ["Some Artist"])

    def test_a_token_does_not_excuse_a_foreign_origin(self):
        tok = self.s.page_token()
        status, hdrs, _b = self.delete({"Origin": "http://evil.example",
                                        "X-CUE-Token": tok})
        self.assertEqual((status, hdrs.get("X-CUE-Refused")), (403, "origin"))
        self.assertEqual(self.s.actions.library_tree.deleted, [])

    def test_no_origin_evidence_still_needs_the_token(self):
        # A sandboxed iframe sends Origin: null; a strict referrer policy
        # sends neither header. The token is what refuses those.
        for hdrs in ({"Origin": "null"}, {}, {"Origin": self.own},
                     {"Origin": self.own, "X-CUE-Token": "guess"}):
            status, rh, _b = self.delete(hdrs)
            self.assertEqual((status, rh.get("X-CUE-Refused")), (403, "token"),
                             hdrs)
        self.assertEqual(self.s.actions.library_tree.deleted, [])
        tok = self.s.page_token()
        status, _h, _b = self.delete({"Origin": "null", "X-CUE-Token": tok})
        self.assertEqual(status, 200)

    def test_referer_is_checked_when_origin_is_absent(self):
        tok = self.s.page_token()
        status, _h, _b = self.delete({"Referer": "http://evil.example/x",
                                      "X-CUE-Token": tok})
        self.assertEqual(status, 403)
        status, _h, _b = self.delete({"Referer": self.own + "/#settings",
                                      "X-CUE-Token": tok})
        self.assertEqual(status, 200)

    def test_every_post_endpoint_is_gated(self):
        # The gate sits above the dispatch: settings, restart and the held
        # actions are refused like delete, before anything is read or run.
        for path in ("/api/settings", "/api/restart", "/api/shutdown",
                     "/api/log/clear", "/api/held/discard", "/api/convert/start",
                     "/api/assembly/add", "/api/no/such"):
            status, _h, _b = self.s.request(
                "POST", path, body="{}",
                headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403, path)
        self.assertEqual(self.s.actions.saved, [])

    def test_the_page_and_the_token_endpoint_agree(self):
        tok = self.s.page_token()
        status, _h, body = self.s.request("GET", "/api/token")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["token"], tok)
        _s, _h, page = self.s.request("GET", "/")
        self.assertNotIn(b"__CUE_TOKEN__", page)
        # The page stamps its POSTs in one place, the fetch wrapper.
        self.assertIn(b"hd['X-CUE-Token']=CUE_TOKEN", page)

    def test_each_process_has_its_own_token(self):
        other = _Server()
        self.addCleanup(other.close)
        self.assertNotEqual(self.s.page_token(), other.page_token())
        status, hdrs, _b = self.s.request(
            "POST", "/api/library/delete", body="{}",
            headers={"Origin": self.own, "X-CUE-Token": other.page_token()})
        self.assertEqual((status, hdrs.get("X-CUE-Refused")), (403, "token"))

    def test_origin_comparison(self):
        m = webui._origin_matches
        self.assertTrue(m("http://NAS:8830", ["nas:8830"]))
        self.assertTrue(m("http://nas", ["nas"]))
        self.assertTrue(m("http://nas", ["nas:80"]))
        self.assertTrue(m("https://nas", ["nas:443"]))
        self.assertTrue(m("http://[fd00::5]:8830/x", ["[fd00::5]:8830"]))
        # behind a proxy that rewrites Host
        self.assertTrue(m("https://cue.example", ["10.0.0.9:8830", "cue.example"]))
        self.assertFalse(m("http://nas:8831", ["nas:8830"]))
        self.assertFalse(m("http://nas.evil.example:8830", ["nas:8830"]))
        self.assertFalse(m("https://nas", ["nas:80"]))
        self.assertFalse(m("file:///x", ["nas"]))
        self.assertFalse(m("http://nas:99999", ["nas"]))
        self.assertFalse(m("http://nas", [None, ""]))


if __name__ == "__main__":
    unittest.main()
