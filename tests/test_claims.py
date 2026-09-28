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


if __name__ == "__main__":
    unittest.main()
