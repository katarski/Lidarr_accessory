"""An album with more than one monitored release blinds the pipeline.

Lidarr answers HTTP 500 "Sequence contains more than one matching element"
to every read that maps such an album (/queue, /album, /album/{id},
/wanted/missing). The client remembers that answer, and the reconcile pass
asks heal_multi_release_albums to find the album and have Lidarr's own
RefreshAlbum leave one monitored release. A release switch waits for a
running refresh, which is what can leave two monitored."""
import ast
import __future__
import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import lidarr as L  # noqa: E402

BASE = "http://lidarr"
MARK = (500, json.dumps({
    "message": "Sequence contains more than one matching element",
    "description": "x"}).encode())
LOCKED = (500, json.dumps({"message": "database is locked"}).encode())


class Inner:
    """The requests session under the client: each request goes to
    `route(method, path, params, payload) -> (status, body)` and is recorded
    as (method, url, params)."""

    def __init__(self, route):
        self.headers, self.route, self.calls = {}, route, []

    def request(self, method, url, **kw):
        params = dict(kw.get("params") or {})
        self.calls.append((method, url, params))
        status, body = self.route(method, url[len(BASE):], params, kw.get("json"))
        r = requests.Response()
        r.status_code = status
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return r


def client(route):
    cfg = SimpleNamespace(base_url=BASE, api_key="k",
                          path_mapping_from="", path_mapping_to="",
                          library_root_lidarr="/music", library_root_windows="/music",
                          manualimport_timeout=60)
    inner = Inner(route)
    return L.LidarrClient(cfg, session=inner), inner


def sent(inner, start=0):
    """The recorded calls from `start` on, as (method, path, params)."""
    return [(m, u[len(BASE):], p) for m, u, p in inner.calls[start:]]


class FakeLidarr:
    """Lidarr with artists 1 and 2. Artist 2's album 7 holds two monitored
    releases until a RefreshAlbum is posted (never, when `stuck`); while it
    does, every read that includes it answers the MARK 500. Album 8 (artist 2)
    and artist 1's albums are fine."""

    def __init__(self, stuck=False):
        self.stuck, self.broken = stuck, True
        self.posted = []
        self.timeouts = set()       # artists whose album list times out
        self.probe_timeout = False  # the unfiltered album list times out

    def __call__(self, method, path, params, payload):
        if method == "POST" and path == "/api/v1/command":
            self.posted.append(payload)
            if not self.stuck:
                self.broken = False
            return 200, {"id": 55}
        if method != "GET":
            return 405, {"message": "%s %s" % (method, path)}
        if path == "/api/v1/command/55":
            return 200, {"id": 55, "status": "completed"}
        if path == "/api/v1/artist":
            return 200, [{"id": 1}, {"id": 2}]
        if path == "/api/v1/album":
            aid = params.get("artistId")
            if aid in self.timeouts or (aid is None and self.probe_timeout):
                raise requests.exceptions.ReadTimeout()
            if aid == 1:
                return 200, []
            return MARK if self.broken else (200, [])
        if path == "/api/v1/queue":
            return MARK if self.broken else (200, {"records": []})
        if path == "/api/v1/track":
            return 200, ([{"albumId": 7}, {"albumId": 7}, {"albumId": 8}]
                         if params.get("artistId") == 2 else [])
        if path == "/api/v1/album/7":
            return MARK if self.broken else (200, {})
        if path == "/api/v1/album/8":
            return 200, {}
        return 404, {"message": path}


class NoSleep(unittest.TestCase):
    """Waits in the code under test are no-ops here."""

    def setUp(self):
        p = mock.patch("lidarr.time.sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)


class Remembered(unittest.TestCase):
    def test_a_500_with_the_mark_is_remembered_and_keeps_the_breaker_closed(self):
        c, inner = client(lambda m, p, q, b: MARK)
        self.assertEqual(c._multi_release_seen_at, 0.0)
        before = time.time()
        with self.assertRaises(requests.HTTPError):
            c._get("/api/v1/queue")
        self.assertGreaterEqual(c._multi_release_seen_at, before)
        self.assertEqual(c.failure_generation, 1)       # counted, once
        self.assertTrue(c.available())                  # Lidarr is up

    def test_another_lidarr_500_is_not_remembered(self):
        c, inner = client(lambda m, p, q, b: LOCKED)
        with self.assertRaises(requests.HTTPError):
            c._get("/api/v1/queue")
        self.assertEqual(c._multi_release_seen_at, 0.0)
        self.assertEqual(c.failure_generation, 1)
        self.assertTrue(c.available())

    def test_only_the_message_of_a_json_object_counts(self):
        def resp(body):
            r = requests.Response()
            r.status_code, r._content = 500, body
            return r
        self.assertTrue(L.LidarrClient._is_multi_release(resp(MARK[1])))
        for body in (b"<html>Sequence contains more than one matching "
                     b"element</html>", b"[]", b'{"message": null}', b"",
                     b'{"description": "Sequence contains more than one '
                     b'matching element"}'):
            self.assertFalse(L.LidarrClient._is_multi_release(resp(body)), body)


class Heal(NoSleep):
    def broken_client(self, fake=None):
        """A client that has met the 500 on a queue read, like the pipeline."""
        fake = fake or FakeLidarr()
        c, inner = client(fake)
        with self.assertRaises(requests.HTTPError):
            c._get("/api/v1/queue")
        self.assertGreater(c._multi_release_seen_at, 0.0)
        return c, inner, fake

    def test_nothing_is_sent_when_no_such_500_was_seen(self):
        c, inner = client(FakeLidarr())
        self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(inner.calls, [])
        self.assertEqual(c._heal_last_at, 0.0)
        c._multi_release_seen_at, c._heal_last_at = 100.0, 200.0   # before the last heal
        self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(inner.calls, [])

    def test_the_album_is_repaired_with_one_refresh_album(self):
        c, inner, fake = self.broken_client()
        start = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [7])
        self.assertEqual(fake.posted, [{"name": "RefreshAlbum", "albumId": 7}])
        calls = sent(inner, start)
        self.assertEqual([m for m, _, _ in calls if m in ("PUT", "DELETE")], [])
        self.assertEqual([m for m, _, _ in calls if m == "POST"], ["POST"])
        self.assertIn(("GET", "/api/v1/album/8", {}), calls)    # looked at, left alone
        self.assertGreater(c._heal_last_at, 0.0)
        self.assertGreaterEqual(c._heal_last_at, c._multi_release_seen_at)

    def test_one_heal_per_interval(self):
        c, inner, fake = self.broken_client()
        self.assertEqual(c.heal_multi_release_albums(), [7])
        c._heal_last_at = time.time() - 60           # it finished a minute ago
        fake.broken = True
        with self.assertRaises(requests.HTTPError):
            c._get("/api/v1/queue")                  # the 500 again
        self.assertGreater(c._multi_release_seen_at, c._heal_last_at)
        n = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(len(inner.calls), n)        # nothing sent
        c._heal_last_at = time.time() - c._HEAL_INTERVAL - 60
        self.assertEqual(c.heal_multi_release_albums(), [7])
        self.assertEqual(len(fake.posted), 2)

    def test_a_500_seen_while_a_heal_ran_does_not_start_another(self):
        c, inner, fake = self.broken_client()
        self.assertEqual(c.heal_multi_release_albums(), [7])
        # The heal's own probes met the 500 before it finished.
        self.assertLessEqual(c._multi_release_seen_at, c._heal_last_at)
        c._heal_last_at = time.time() - c._HEAL_INTERVAL - 60
        c._multi_release_seen_at = c._heal_last_at - 1
        n = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(len(inner.calls), n)

    def test_an_album_list_that_reads_needs_no_repair(self):
        c, inner, fake = self.broken_client()
        fake.broken = False                          # mended by other means
        start = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(sent(inner, start), [("GET", "/api/v1/album", {})])
        self.assertEqual(fake.posted, [])
        self.assertGreater(c._heal_last_at, 0.0)

    def test_a_probe_that_fails_ends_the_heal_with_a_warning(self):
        c, inner, fake = self.broken_client()
        fake.probe_timeout = True
        with self.assertLogs(L.logger, "WARNING") as logs:
            self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertIn("probe failed", logs.output[0])
        self.assertEqual(fake.posted, [])
        self.assertGreater(c._heal_last_at, 0.0)

    def test_a_repair_that_does_not_take_is_an_error_in_the_log(self):
        c, inner, fake = self.broken_client(FakeLidarr(stuck=True))
        with self.assertLogs(L.logger, "ERROR") as logs:
            self.assertEqual(c.heal_multi_release_albums(), [])
        self.assertEqual(len(fake.posted), 1)        # one try, not a loop
        self.assertIn("still has more than one monitored release", logs.output[0])
        self.assertIn("album 7", logs.output[0])
        self.assertGreater(c._heal_last_at, 0.0)

    def test_the_artist_refreshed_last_is_scanned_first(self):
        c, inner, fake = self.broken_client()
        c._refresh_seen = {1: (10.0, 8), 2: (50.0, 9)}
        start = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [7])
        asked = [p["artistId"] for m, path, p in sent(inner, start)
                 if path == "/api/v1/album" and "artistId" in p]
        self.assertEqual(asked, [2])                 # found it; artist 1 not scanned

        c, inner, fake = self.broken_client()
        c._refresh_seen = {1: (50.0, 8), 2: (10.0, 9)}
        start = len(inner.calls)
        self.assertEqual(c.heal_multi_release_albums(), [7])
        asked = [p["artistId"] for m, path, p in sent(inner, start)
                 if path == "/api/v1/album" and "artistId" in p]
        self.assertEqual(asked, [1, 2])

    def test_an_artist_whose_albums_time_out_is_skipped(self):
        fake = FakeLidarr()
        fake.timeouts = {1}
        c, inner, fake = self.broken_client(fake)
        gen = c.failure_generation
        self.assertEqual(c.heal_multi_release_albums(), [7])
        self.assertGreater(c.failure_generation, gen)     # counted, not hidden
        self.assertTrue(c.available())


class RefreshRunning(unittest.TestCase):
    def client(self, commands):
        def route(method, path, params, payload):
            if path == "/api/v1/command":
                return 200, commands
            return 404, {"message": path}
        return client(route)[0]

    def test_a_started_refresh_of_an_artist_runs_for_that_artist_only(self):
        c = self.client([{"name": "RefreshArtist", "status": "started",
                          "body": {"artistIds": [4]}}])
        self.assertTrue(c._refresh_running(4, None))
        self.assertFalse(c._refresh_running(5, None))
        self.assertFalse(c._refresh_running(None, 17))
        c = self.client([{"name": "RefreshArtist", "status": "Queued",
                          "body": {"artistId": 4}}])
        self.assertTrue(c._refresh_running(4, 17))
        self.assertFalse(c._refresh_running(5, 17))

    def test_a_finished_refresh_does_not_count(self):
        for status in ("completed", "failed", "aborted", ""):
            c = self.client([{"name": "RefreshArtist", "status": status,
                              "body": {"artistIds": [4]}}])
            self.assertFalse(c._refresh_running(4, 17), status)

    def test_a_refresh_naming_no_artist_covers_every_artist(self):
        for body in ({}, {"artistIds": []}, {"artistIds": ["x"]}):
            c = self.client([{"name": "RefreshArtist", "status": "queued",
                              "body": body}])
            self.assertTrue(c._refresh_running(5, 17), body)

    def test_a_started_refresh_of_an_album_runs_for_that_album_only(self):
        c = self.client([{"name": "RefreshAlbum", "status": "started",
                          "body": {"albumId": 17}}])
        self.assertTrue(c._refresh_running(4, 17))
        self.assertFalse(c._refresh_running(4, 18))
        self.assertFalse(c._refresh_running(4, None))

    def test_other_commands_do_not_count(self):
        c = self.client([{"name": "DownloadedAlbumsScan", "status": "started",
                          "body": {}},
                         {"body": {"name": "RefreshAlbum", "albumId": 3},
                          "status": "started"},
                         "junk", None])
        self.assertFalse(c._refresh_running(4, 17))
        self.assertTrue(c._refresh_running(4, 3))     # the name can sit in the body

    def test_lidarr_that_cannot_say_does_not_hold_a_switch_up(self):
        c, inner = client(lambda m, p, q, b: LOCKED)
        self.assertFalse(c._refresh_running(4, 17))

        def down(method, path, params, payload):
            raise requests.ConnectionError("refused")
        c, inner = client(down)
        with self.assertLogs(L.logger, "WARNING"):      # the breaker's one line
            self.assertFalse(c._refresh_running(4, 17))
        self.assertFalse(c.available())
        self.assertTrue(c._wait_refresh_idle(4, 17))     # fails fast, no wait


ALBUM = {"id": 17, "artistId": 4,
         "releases": [{"id": 180, "monitored": True},
                      {"id": 191, "monitored": False}]}
STARTED = [{"name": "RefreshArtist", "status": "started",
            "body": {"artistIds": [4]}}]


class SwitchWaitsForRefresh(NoSleep):
    def make(self, commands):
        """Album 17 of artist 4 in a fake Lidarr whose command list is
        `commands()`; returns (client, the PUT bodies)."""
        puts = []

        def route(method, path, params, payload):
            if method == "GET" and path == "/api/v1/album/17":
                return 200, json.loads(json.dumps(ALBUM))
            if method == "GET" and path == "/api/v1/command":
                return 200, commands()
            if method == "PUT" and path == "/api/v1/album/17":
                puts.append(payload)
                return 200, payload
            return 404, {"message": path}
        return client(route)[0], puts

    def test_a_switch_does_not_land_in_a_refresh_that_keeps_running(self):
        c, puts = self.make(lambda: STARTED)
        with mock.patch.object(L.LidarrClient, "_REFRESH_IDLE_WAIT", 0.0), \
                self.assertLogs(L.logger, "WARNING") as logs:
            self.assertFalse(c.set_album_monitored_release(17, 191))
        self.assertEqual(puts, [])
        self.assertIn("still running", logs.output[0])

    def test_a_switch_with_no_refresh_running_goes_through(self):
        c, puts = self.make(lambda: [])
        self.assertTrue(c.set_album_monitored_release(17, 191))
        self.assertEqual(len(puts), 1)
        self.assertEqual([r["id"] for r in puts[0]["releases"] if r["monitored"]],
                         [191])
        self.sleep.assert_not_called()

    def test_a_switch_waits_for_the_refresh_to_end(self):
        polls = []

        def commands():
            polls.append(1)
            return STARTED if len(polls) < 3 else []
        c, puts = self.make(commands)
        self.assertTrue(c.set_album_monitored_release(17, 191))
        self.assertEqual(len(polls), 3)     # the check, then two polls
        self.assertEqual(self.sleep.call_count, 1)
        self.sleep.assert_called_with(3.0)
        self.assertEqual(len(puts), 1)

    def test_after_a_wait_the_switch_writes_the_album_lidarr_holds_now(self):
        """The refresh that was waited out added release 200 and moved the
        monitored flag onto it; the PUT must carry that state, not the copy
        read before the wait (a PUT writes every release of its body)."""
        polls, reads, puts = [], [], []
        after = {"id": 17, "artistId": 4,
                 "releases": [{"id": 180, "monitored": False},
                              {"id": 191, "monitored": False},
                              {"id": 200, "monitored": True}]}

        def route(method, path, params, payload):
            if method == "GET" and path == "/api/v1/album/17":
                reads.append(1)
                return 200, json.loads(json.dumps(ALBUM if len(reads) == 1
                                                  else after))
            if method == "GET" and path == "/api/v1/command":
                polls.append(1)
                return 200, STARTED if len(polls) < 2 else []
            if method == "PUT" and path == "/api/v1/album/17":
                puts.append(payload)
                return 200, payload
            return 404, {"message": path}
        c = client(route)[0]
        self.assertTrue(c.set_album_monitored_release(17, 191))
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(puts), 1)
        self.assertEqual(
            {r["id"]: r["monitored"] for r in puts[0]["releases"]},
            {180: False, 191: True, 200: False})

    def test_a_release_not_in_the_album_is_refused_before_any_wait(self):
        c, puts = self.make(lambda: STARTED)
        with self.assertLogs(L.logger, "WARNING"):
            self.assertFalse(c.set_album_monitored_release(17, 999))
        self.assertEqual(puts, [])
        self.sleep.assert_not_called()


class WithoutInit(unittest.TestCase):
    def test_a_client_built_without_init_is_not_held_up_and_heals_nothing(self):
        c = L.LidarrClient.__new__(L.LidarrClient)
        self.assertFalse(c._refresh_running(4, 17))
        self.assertTrue(c._wait_refresh_idle(4, 17))
        self.assertEqual(c.heal_multi_release_albums(), [])
        puts = []
        c.get_album = lambda i: json.loads(json.dumps(ALBUM))
        c._put = lambda path, body: puts.append(body)
        self.assertTrue(c.set_album_monitored_release(17, 191))
        self.assertEqual(len(puts), 1)


def _reconcile_loop():
    """main.reconcile_loop, compiled from main.py without importing main (the
    import needs packages a bare unit-test environment may lack)."""
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "reconcile_loop")
    code = compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    ns = {"logger": logging.getLogger("cue_pipeline")}
    exec(code, ns)
    return ns["reconcile_loop"]


class OneStop:
    """A stop event that lets one pass of the loop run, then ends it."""

    def __init__(self):
        self.waits = []

    def wait(self, delay):
        self.waits.append(delay)
        return len(self.waits) > 1


class ReconcileLoop(unittest.TestCase):
    def test_the_heal_runs_before_the_pass(self):
        order = []
        orch = SimpleNamespace(
            lidarr=SimpleNamespace(
                heal_multi_release_albums=lambda: order.append("heal") or []),
            reconcile_monitored_gaps=lambda root, excl: order.append("pass"))
        stop = OneStop()
        _reconcile_loop()(orch, Path("."), [], stop, 300)
        self.assertEqual(order, ["heal", "pass"])
        self.assertEqual(stop.waits, [300, 300])

    def test_a_failing_heal_does_not_stop_the_pass(self):
        order = []

        def heal():
            order.append("heal")
            raise RuntimeError("boom")
        orch = SimpleNamespace(
            lidarr=SimpleNamespace(heal_multi_release_albums=heal),
            reconcile_monitored_gaps=lambda root, excl: order.append("pass"))
        with self.assertLogs("cue_pipeline", "ERROR") as logs:
            _reconcile_loop()(orch, Path("."), [], OneStop(), 300)
        self.assertEqual(order, ["heal", "pass"])
        self.assertIn("multi-release heal", logs.output[0])


if __name__ == "__main__":
    unittest.main()
