"""One worker per folder: nothing deletes or re-hands-off a folder another
thread is processing."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claims  # noqa: E402


def in_thread(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()))
    t.start()
    t.join(5)
    return out.get("v")


class Claims(unittest.TestCase):
    def tearDown(self):
        claims._held.clear()

    def test_other_threads_are_refused_nested_paths_included(self):
        self.assertTrue(claims.claim("/downloads/Album"))
        self.assertTrue(claims.claim("/downloads/Album"))            # re-entrant
        self.assertFalse(in_thread(lambda: claims.claim("/downloads/Album")))
        self.assertFalse(in_thread(lambda: claims.claim("/downloads/Album/CD1")))
        self.assertFalse(in_thread(lambda: claims.claim("/downloads")))
        self.assertTrue(in_thread(lambda: claims.claim("/downloads/Other")))
        self.assertIsNone(claims.busy("/downloads/Album"))          # own claim
        self.assertTrue(in_thread(lambda: claims.busy("/downloads/Album/CD2")))
        claims.release("/downloads/Album")
        self.assertFalse(in_thread(lambda: claims.claim("/downloads/Album")))
        claims.release("/downloads/Album")
        self.assertTrue(in_thread(lambda: claims.claim("/downloads/Album")))

    def test_torrent_on_another_mount_is_matched_by_its_folder_name(self):
        claims.claim("/downloads/Cyndi Lauper - Discography/2001 - Feels Like Christmas")
        hit = in_thread(lambda: claims.busy_torrent("/data/torrents/Cyndi Lauper - Discography"))
        self.assertTrue(hit)
        self.assertIsNone(in_thread(lambda: claims.busy_torrent("/data/torrents/Other")))

    def test_waiting_claim_gets_the_folder_when_released(self):
        claims.claim("/downloads/A")
        got = []
        t = threading.Thread(target=lambda: got.append(claims.claim("/downloads/A", wait=True)))
        t.start()
        t.join(0.2)
        self.assertEqual(got, [])
        claims.release("/downloads/A")
        t.join(5)
        self.assertEqual(got, [True])

    def test_qbt_remove_is_refused_while_claimed(self):
        from qbittorrent_client import QbtClient
        q = QbtClient.__new__(QbtClient)
        q.torrent_by_hash = lambda h: {"content_path": "/data/Album", "name": "Album"}
        q.s = None                       # a request would raise
        claims.claim("/downloads/Album")
        self.assertFalse(in_thread(lambda: q.remove("H", delete_files=True)))


class AddByUrl(unittest.TestCase):
    """The new torrent is found by its one-off tag, never by diffing."""
    def client(self, appear):
        from qbittorrent_client import QbtClient
        q = QbtClient.__new__(QbtClient)
        q.base = "http://qbt"
        q._api_ok = lambda: True
        sent = []

        class S:
            def post(self, url, data=None, timeout=None):
                sent.append((url.rsplit("/", 1)[-1], dict(data or {})))
                return type("R", (), {"raise_for_status": lambda self: None})()
        q.s = S()

        def torrents(category="", state_filter="", tag=""):
            added = [d for u, d in sent if u == "add"]
            nonce = added[-1]["tags"].split(",")[-1] if added else None
            other = [{"hash": "0000other", "category": "radarr"}]  # someone else's add
            mine = [{"hash": "ABCDEF12", "category": category}] if appear and tag == nonce else []
            return mine if tag else other + mine
        q.torrents = torrents
        return q, sent

    def test_finds_only_its_own_add_and_cleans_the_tag(self):
        q, sent = self.client(appear=True)
        self.assertEqual(q.add_torrent_url("http://prowlarr/dl/1", category="lidarr",
                                           timeout=5), "abcdef12")
        names = [u for u, _ in sent]
        self.assertEqual(names, ["add", "removeTags", "deleteTags"])
        self.assertIn("cue-add-", sent[0][1]["tags"])

    def test_nothing_of_ours_appears_returns_none(self):
        import qbittorrent_client as qc
        q, sent = self.client(appear=False)
        real_sleep, qc.time.sleep = qc.time.sleep, lambda s: None
        try:
            self.assertIsNone(q.add_torrent_url("http://prowlarr/dl/1", category="lidarr",
                                                timeout=5))
        finally:
            qc.time.sleep = real_sleep
        self.assertEqual([u for u, _ in sent], ["add", "deleteTags"])


if __name__ == "__main__":
    unittest.main()
