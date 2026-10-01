"""A grab whose guid has no infohash is bound to its download through
Lidarr's grab history.

RuTracker guids are topic links ('3_https://rutracker.org/forum/viewtopic.php
?t=3982619'), and the queue row carries the torrent's own name ('Olivier
Derivière - 2007 - Obscure 2' for '(Score) (Soundtrack/Game) Obscure 2
(Olivier Derivière) - 2007, MP3, 320 kbps') and often no album -- 61 grabs
in four days were never verified ('grabbed but no queue hash')."""
import inspect
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator as OR  # noqa: E402
from lidarr import LidarrClient  # noqa: E402

GUID = "3_https://rutracker.org/forum/viewtopic.php?t=3982619"


class History(unittest.TestCase):

    def test_the_newest_grab_of_that_guid_since_the_grab(self):
        c = LidarrClient.__new__(LidarrClient)
        now = time.time()
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        rows = [
            {"date": iso(now - 86400), "downloadId": "OLD", "data": {"guid": GUID}},
            {"date": iso(now - 5), "downloadId": "EA31547D", "data": {"guid": GUID}},
            {"date": iso(now - 2), "downloadId": "OTHER", "data": {"guid": "3_x"}},
        ]
        seen = {}

        def get(path, **params):
            seen.update(params, path=path)
            return rows
        c._get = get
        self.assertEqual(c.grab_download_id(17, GUID, now - 300), "ea31547d")
        self.assertEqual(seen, {"path": "/api/v1/history/artist",
                                "artistId": 17, "eventType": 1})
        self.assertIsNone(c.grab_download_id(17, "3_none", now - 300))


class AwaitGrab(unittest.TestCase):

    def test_a_topic_link_grab_is_bound_by_history(self):
        o = OR.Orchestrator.__new__(OR.Orchestrator)
        row = {"downloadId": "EA31547D", "title": "Obscure 2", "albumId": None}
        o.lidarr = mock.Mock()
        o.lidarr.queue_list.return_value = [row]
        o.lidarr.grab_download_id.return_value = "ea31547d"
        with mock.patch.object(OR.time, "sleep", lambda s: None):
            did, rec = o._await_grab(
                "(Score) (Soundtrack/Game) Obscure 2 (Olivier Derivière) - 2007",
                album_id=12537, before=set(), guid=GUID, timeout=5,
                artist_id=17)
        self.assertEqual((did, rec), ("ea31547d", row))
        self.assertEqual(o.lidarr.grab_download_id.call_args[0][:2], (17, GUID))

    def test_both_searches_pass_the_artist(self):
        src = inspect.getsource(OR.Orchestrator)
        calls = src.split("self._await_grab(")[1:]
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertIn("artist_id=", c.split(")\n")[0] + c[:400])


if __name__ == "__main__":
    unittest.main()
