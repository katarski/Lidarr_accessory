"""SearXNG gives the model web evidence for the few ownership questions it
still gets -- and is asked only then, sparingly, with every answer kept.

The model took Armin van Buuren's 'Breathe In (2024) Extended Edition' for
the owned 'Breathe' without knowing 'Breathe In' is an album of its own."""
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as D  # noqa: E402
import websearch as W  # noqa: E402
from tests.test_llm import Resp, client, verdict  # noqa: E402

PAGE = {"results": [
    {"title": "Armin van Buuren - Breathe In (2024)", "url": "https://www.discogs.com/release/1"},
    {"title": "Breathe In (Extended Edition)", "url": "https://music.apple.com/x"}]}


def answer(payload):
    return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(payload).encode()),
                          __exit__=lambda *a: False)


class Client(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cache = os.path.join(d.name, "websearch_cache.json")

    def test_titles_are_kept_and_not_searched_twice(self):
        w = W.WebSearch("http://SearXNG:8080", self.cache, min_interval=0)
        with mock.patch.object(W.urllib.request, "urlopen",
                               return_value=answer(PAGE)) as op:
            self.assertEqual(w.titles("Armin van Buuren Breathe In album"),
                             ["Armin van Buuren - Breathe In (2024) -- discogs.com",
                              "Breathe In (Extended Edition) -- music.apple.com"])
            w.titles("Armin van Buuren  Breathe In album")
        self.assertEqual(op.call_count, 1)
        again = W.WebSearch("http://SearXNG:8080", self.cache, min_interval=0)
        with mock.patch.object(W.urllib.request, "urlopen") as op2:
            self.assertEqual(len(again.titles("Armin van Buuren Breathe In album")), 2)
        op2.assert_not_called()

    def test_blocked_or_capped_is_no_answer_and_not_kept(self):
        w = W.WebSearch("http://SearXNG:8080", self.cache, min_interval=0, max_per_hour=1)
        blocked = {"results": [], "unresponsive_engines": [["brave", "too many requests"]]}
        with mock.patch.object(W.urllib.request, "urlopen", return_value=answer(blocked)):
            self.assertIsNone(w.titles("a"))
            self.assertIsNone(w.titles("b"))          # over the hourly cap
        self.assertFalse(os.path.exists(self.cache))


class Evidence(unittest.TestCase):

    def test_web_titles_reach_the_model_only_when_it_is_asked(self):
        calls = []

        def ev():
            calls.append(1)
            return ["Armin van Buuren - Breathe In (2024) -- discogs.com"]
        c = client(verdict(resident=True), [Resp(200, {"response": "NONE"})])
        self.assertIsNone(c.pick_owned_album("Breathe In Extended Edition",
                                             ["Breathe"], evidence=ev))
        self.assertIn("Breathe In (2024) -- discogs.com", c.session.sent[0][0]["prompt"])
        self.assertEqual(calls, [1])
        c2 = client(verdict(resident=True), [])
        self.assertIsNone(c2.pick_owned_album("Lotos Land", ["Crises"], evidence=ev))
        self.assertEqual(calls, [1])                  # not asked, not searched

    def test_the_ownership_check_searches_artist_and_title(self):
        owned = {"id": 1, "title": "Breathe", "releaseDate": "2025-01-01",
                 "statistics": {"trackFileCount": 51, "totalTrackCount": 51}}
        queries = []
        lid = SimpleNamespace(
            find_artist=lambda n: {"id": 7}, list_albums_for_artist=lambda i: [owned],
            get_album=lambda i: owned, list_tracks_for_album=lambda i: [],
            web=SimpleNamespace(titles=lambda q: queries.append(q) or ["x"]))
        seen = []

        def pick(download, titles, evidence=None):
            seen.append(evidence())
            return None
        D.album_complete_in_library(lid, "Armin van Buuren", "Breathe Again Live",
                                    llm=SimpleNamespace(pick_owned_album=pick))
        self.assertEqual((queries, seen),
                         (["Armin van Buuren Breathe Again Live album"], [["x"]]))


if __name__ == "__main__":
    unittest.main()
