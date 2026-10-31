"""Lidarr 3.1.6.5079 names a release's protocol "TorrentDownloadProtocol";
the version before it said "torrent". Comparing with "torrent" alone dropped
every release from 31 Oct 2026 on: Find missing always answered "your
indexers offered no torrent", and every interactive search logged "no torrent
candidates"."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator, _is_torrent_release  # noqa: E402


class TorrentProtocolNames(unittest.TestCase):
    def test_every_name_lidarr_uses_for_a_torrent(self):
        for p in ("torrent", "Torrent", "TorrentDownloadProtocol", 2, "2"):
            self.assertTrue(_is_torrent_release({"protocol": p}), p)

    def test_usenet_and_unknown_are_not(self):
        for p in ("usenet", "UsenetDownloadProtocol", 1, "", None, "unknown"):
            self.assertFalse(_is_torrent_release({"protocol": p}), p)
        self.assertFalse(_is_torrent_release({}))
        self.assertFalse(_is_torrent_release(None))


class FindMissingSeesTodaysReleases(unittest.TestCase):
    def _orch(self, releases):
        o = Orchestrator.__new__(Orchestrator)
        plans = {1: {"album": "Album", "artist": "Artist",
                     "missing": [{"track": "Song A"}]}}
        o.assembly = SimpleNamespace(
            get=lambda i: dict(plans[i]),
            upsert=lambda i, p: plans.__setitem__(i, p))
        o.lidarr = SimpleNamespace(
            get_album=lambda i: {"artist": {"id": 5}},
            release_search_artist=lambda aid: list(releases),
            release_search=lambda aid: [])
        o.cfg = SimpleNamespace(interactive_search_min_seeders=1)
        o._assembly_owned_keys = lambda aid: set()
        o._release_unofficial_markers = lambda t: False
        o._release_format_bonus = lambda t: 0.0
        o._release_health = lambda s, l: float(s)
        o.grabbed_from = []

        def grab(album_id, artist, missing, ranked, hunt=None):
            o.grabbed_from.append([r["guid"] for r in ranked])
            return True, "grabbed"
        o._assembly_grab_for_songs = grab
        return o, plans

    def test_a_torrent_named_the_new_way_is_a_candidate(self):
        o, plans = self._orch([
            {"guid": "g1", "title": "Artist - Best Of", "seeders": 5,
             "leechers": 0, "protocol": "TorrentDownloadProtocol"},
            {"guid": "g2", "title": "Artist - Best Of", "seeders": 9,
             "leechers": 0, "protocol": "UsenetDownloadProtocol"},
        ])
        ok, msg = o.assembly_find_missing(1)
        self.assertTrue(ok, msg)
        self.assertEqual(o.grabbed_from, [["g1"]])
        self.assertTrue(plans[1]["hunt"]["active"])


if __name__ == "__main__":
    unittest.main()
