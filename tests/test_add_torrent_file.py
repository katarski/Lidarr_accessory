"""A Prowlarr .torrent link is fetched by us, not handed to qBittorrent.

Handed over, a refused fetch (RuTracker behind Cloudflare) showed as nothing
appearing after a 40s wait, 28 times on 29-30 Sep. Fetched, the reason is
known at once and the infohash is exact."""
import hashlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbittorrent_client as qc  # noqa: E402

INFO = b"d6:lengthi1e4:name1:ae"
TORRENT = b"d8:announce3:foo4:info" + INFO + b"e"
IH = hashlib.sha1(INFO).hexdigest()


def _client(have=None):
    q = qc.QbtClient.__new__(qc.QbtClient)
    q.base = "http://qbt"
    q._api_ok = lambda: True
    posts, state = [], {"have": have}

    class S:
        def post(self, url, data=None, files=None, timeout=None):
            posts.append((url.rsplit("/", 1)[-1], dict(data or {}), files))
            if files:
                state["have"] = {"hash": IH, "category": (data or {}).get("category")}
            return type("R", (), {"raise_for_status": lambda self: None})()
    q.s = S()
    q.lookup = lambda h: (True, state["have"] if h == IH else None)
    return q, posts


class FetchFirst(unittest.TestCase):

    def test_infohash_of_a_torrent(self):
        self.assertEqual(qc.infohash_of(TORRENT), IH)
        self.assertIsNone(qc.infohash_of(b"<html>"))

    def test_the_file_is_uploaded_and_its_hash_returned(self):
        q, posts = _client()
        q._fetch_torrent = lambda u: (TORRENT, None, "")
        self.assertEqual(q.add_torrent_url("http://prowlarr/1/download",
                                           category="lidarr"), IH)
        self.assertEqual(posts[0][0], "add")
        self.assertIn("torrents", posts[0][2])
        self.assertEqual(posts[0][1]["category"], "lidarr")

    def test_a_refused_fetch_answers_at_once(self):
        q, posts = _client()
        q._fetch_torrent = lambda u: (None, None, "HTTP 403 Cloudflare")
        with mock.patch.object(qc.time, "sleep") as slept:
            self.assertIsNone(q.add_torrent_url("http://prowlarr/1/download",
                                                category="lidarr"))
        self.assertEqual(posts, [])
        slept.assert_not_called()

    def test_a_magnet_redirect_goes_to_add_magnet(self):
        q, posts = _client()
        q._fetch_torrent = lambda u: (None, "magnet:?xt=urn:btih:" + IH, "")
        q.add_magnet = lambda m, **k: ("magnet", m)
        self.assertEqual(q.add_torrent_url("http://prowlarr/1/download")[0], "magnet")

    def test_another_apps_copy_is_not_taken(self):
        q, posts = _client(have={"hash": IH, "category": "radarr"})
        q._fetch_torrent = lambda u: (TORRENT, None, "")
        self.assertIsNone(q.add_torrent_url("http://prowlarr/1/download",
                                            category="lidarr"))
        self.assertEqual(posts, [])

    def test_our_own_copy_is_ours(self):
        q, posts = _client(have={"hash": IH, "category": "lidarr"})
        q._fetch_torrent = lambda u: (TORRENT, None, "")
        self.assertEqual(q.add_torrent_url("http://prowlarr/1/download",
                                           category="lidarr"), IH)
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
