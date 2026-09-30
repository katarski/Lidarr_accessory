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


class InteractiveSearchState(unittest.TestCase):
    def test_iso_ts(self):
        from orchestrator import _iso_ts
        self.assertGreater(_iso_ts("2026-09-25T00:00:00Z"), 1.7e9)
        self.assertEqual(_iso_ts(None), 0.0)
        self.assertEqual(_iso_ts("garbage"), 0.0)

    def test_a_pass_during_a_lidarr_failure_keeps_the_state(self):
        from orchestrator import Orchestrator
        saved = []
        lid = SimpleNamespace(failure_generation=0)

        def wanted_missing():
            lid.failure_generation += 1          # listing failed part-way
            return []
        lid.wanted_missing = wanted_missing
        s = SimpleNamespace(lidarr=lid, cfg=SimpleNamespace(
            interactive_search_enabled=True, interactive_search_state_file=None))
        s._load_isearch_state = lambda: {"12": {"first_missing": 1.0}}
        s._save_isearch_state = saved.append
        s._lidarr_generation = Orchestrator._lidarr_generation.__get__(s)
        self.assertEqual(Orchestrator.interactive_search_pass(s), 0)
        self.assertEqual(saved, [])


def _mb_answer(title):
    return {"recordings": [{"title": title, "releases": [{
        "title": "Best Of", "id": "r1",
        "release-group": {"primary-type": "Album",
                          "secondary-types": ["Compilation"]}}]}]}


class CouldNotAskIsNotAnAnswer(unittest.TestCase):
    """orch3 COMP-NEG-1: a MusicBrainz or Lidarr failure during the
    compilation hunt or the external audit is not recorded as "no
    compilation" / "missing from Lidarr"."""

    def _mb(self, down=()):
        import musicbrainz as M
        mb = M.MusicBrainzClient.__new__(M.MusicBrainzClient)
        mb._get = lambda path, **kw: (
            None if any(d in kw.get("query", "") for d in down)
            else _mb_answer(kw["query"].split('"')[1]))
        return mb

    def test_a_failed_song_makes_the_walk_incomplete(self):
        songs = [{"title": "Song A"}, {"title": "Song B"}]
        got, complete = self._mb().compilations_for_tracks(songs, "arid")
        self.assertTrue(complete)
        self.assertEqual(got[0]["coverage"], 2)
        got, complete = self._mb(down=("Song B",)).compilations_for_tracks(
            songs, "arid")
        self.assertFalse(complete)
        self.assertEqual(got[0]["tracks"], ["Song A"])     # still used

    def _orch(self, mb, tracks_fail=False):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        lid = SimpleNamespace(failure_generation=0)
        lid.get_album = lambda i: {"title": "Album",
                                   "artist": {"foreignArtistId": "arid",
                                              "artistName": "Artist"}}

        def tracks(i):
            if tracks_fail:
                lid.failure_generation += 1
                return []
            return [{"title": "Song A"}, {"title": "Song B"}]
        lid.list_tracks_for_album = tracks
        o.lidarr = lid
        o.cfg = SimpleNamespace(comp_hunt_enabled=True)
        plans = {1: {"album": "Album", "artist": "Artist",
                     "missing": [{"track": "Song A"}, {"track": "Song B"}]}}
        o.assembly = SimpleNamespace(
            get=lambda i: dict(plans[i]),
            upsert=lambda i, p: plans.__setitem__(i, p))
        o._get_prowlarr = lambda: object()
        o._get_mb = lambda: mb
        o._comp_search_titles = lambda pw, titles, searched, a, i: ([], "n")
        return o, plans

    def test_an_outage_leaves_the_hunt_on(self):
        for mb, fail in ((self._mb(down=("Song",)), False), (self._mb(), True)):
            o, plans = self._orch(mb, tracks_fail=fail)
            ok, msg = o.assembly_find_compilation(1)
            self.assertFalse(ok)
            self.assertIn("could not be asked", msg)
            self.assertNotIn("comp", plans[1])       # nothing written

    def test_a_partial_answer_is_used_but_not_cached(self):
        o, plans = self._orch(self._mb(down=("Song B",)))
        o.assembly_find_compilation(1)
        comp = plans[1]["comp"]
        self.assertEqual(comp["titles"], [{"title": "Best Of", "coverage": 1}])
        self.assertNotIn("looked_up", comp)
        self.assertTrue(comp.get("active"))

    def test_a_real_empty_answer_still_ends_the_hunt(self):
        import musicbrainz as M
        mb = M.MusicBrainzClient.__new__(M.MusicBrainzClient)
        mb._get = lambda path, **kw: {"recordings": []}
        o, plans = self._orch(mb)
        ok, msg = o.assembly_find_compilation(1)
        self.assertFalse(plans[1]["comp"]["active"])
        self.assertIn("does not list", msg)

    def test_the_external_audit_records_nothing_while_lidarr_fails(self):
        import json
        import tempfile
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        o = Orchestrator.__new__(Orchestrator)
        lid = SimpleNamespace(failure_generation=0)
        lid.list_artists = lambda: [
            {"id": i, "monitored": True, "foreignArtistId": "m%d" % i,
             "artistName": "A%d" % i} for i in (1, 2)]

        def albums(i):
            lid.failure_generation += 1              # the read failed
            return []
        lid.list_albums_for_artist = albums
        o.lidarr = lid
        o._mb = SimpleNamespace(release_groups=lambda *a, **k: [
            {"title": "Studio One", "first_release_date": "1990"}])
        path = Path(tmp.name) / "audit.json"
        o.cfg = SimpleNamespace(external_audit_file=path)
        self.assertEqual(o.external_album_audit_pass(), 0)
        self.assertEqual(json.loads(path.read_text("utf-8"))["artists"], {})


class SearchDuringAnOutageIsNotAnAttempt(unittest.TestCase):
    """orch3 ISEARCH-AVAIL-1: a failed search, a grab refused by an open
    breaker or a failed queue read is not an attempt -- no candidate burned,
    no Prowlarr fallback, no cooldown stamp."""

    def _orch(self):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(interactive_search_min_title_ratio=0.45,
                                interactive_search_dry_run=False,
                                interactive_search_max_candidates=1000)
        lid = SimpleNamespace(failure_generation=0, up=True)
        lid.available = lambda: lid.up
        o.lidarr = lid
        o.fallback, o.grabs = [], []
        o._rank_releases = lambda rels, *a, **k: rels
        o._queue_download_ids = lambda: set()
        o._isearch_prowlarr_album = lambda *a, **k: o.fallback.append(1) or False
        return o, lid

    ALB = {"id": 1, "artistId": 9, "title": "Hoochie",
           "artist": {"artistName": "Muddy Waters"}}
    CANDS = [{"guid": "g%d" % i, "indexerId": 1, "title": "Muddy Waters %d" % i,
              "_title_ratio": 1.0} for i in range(13)]

    def test_a_failed_search_is_no_answer(self):
        o, lid = self._orch()

        def search(aid):
            lid.failure_generation += 1
            return []
        lid.release_search = search
        self.assertIsNone(o._isearch_one_album(self.ALB, {}, None))
        self.assertEqual(o.fallback, [])

    def test_an_open_breaker_burns_no_candidates(self):
        o, lid = self._orch()
        lid.release_search = lambda aid: list(self.CANDS)

        def grab(guid, idx, **k):
            o.grabs.append(guid)
            lid.failure_generation += 1           # "breaker open"
            lid.up = False
            return False
        lid.release_grab = grab
        self.assertIsNone(o._isearch_one_album(self.ALB, {}, None))
        self.assertEqual(o.grabs, ["g0"])
        self.assertEqual(o.fallback, [])

    def test_a_refused_release_still_moves_on(self):
        o, lid = self._orch()
        lid.release_search = lambda aid: list(self.CANDS[:3])
        lid.release_grab = lambda guid, idx, **k: o.grabs.append(guid) or False
        self.assertFalse(o._isearch_one_album(self.ALB, {}, None))
        self.assertEqual(o.grabs, ["g0", "g1", "g2"])
        self.assertEqual(o.fallback, [1])

    def _pass(self, queue_fails=False):
        from orchestrator import Orchestrator
        lid = SimpleNamespace(failure_generation=0)
        lid.wanted_missing = lambda: [
            {"id": 1, "artistId": 9, "title": "A", "artist": {"artistName": "X"}},
            {"id": 2, "artistId": 8, "title": "B", "artist": {"artistName": "Y"}}]

        def queue_list():
            if queue_fails:
                lid.failure_generation += 1
            return []
        lid.queue_list = queue_list
        saved, tried = [], []
        s = SimpleNamespace(lidarr=lid, cfg=SimpleNamespace(
            interactive_search_min_missing_days=0,
            interactive_search_cooldown_seconds=3600,
            interactive_search_max_albums_per_pass=10,
            interactive_search_dry_run=False,
            interactive_search_artist_level=True))
        s._load_isearch_state = lambda: {}
        s._save_isearch_state = saved.append
        s._lidarr_generation = Orchestrator._lidarr_generation.__get__(s)
        s._isearch_one_album = lambda alb, st, q: tried.append(alb["id"])
        s._try_artist_fill = lambda *a: tried.append("artist")
        return Orchestrator.interactive_search_pass(s), saved, tried

    def test_a_failed_queue_read_skips_the_pass(self):
        n, saved, tried = self._pass(queue_fails=True)
        self.assertEqual((n, saved, tried), (0, [], []))

    def test_an_outage_mid_pass_stamps_nothing_and_stops(self):
        n, saved, tried = self._pass()
        self.assertEqual(tried, [1])                    # stopped at the first
        self.assertFalse(any("last_attempt" in v for v in saved[0].values()))


if __name__ == "__main__":
    unittest.main()
