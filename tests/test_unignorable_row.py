"""A queue row Lidarr cannot drop without touching the client is an answer
about that row; it is not Lidarr failing.

From 31 Oct 2026 13:13 the queue reaper asked every 10 minutes to drop row
468890554 (removeFromClient=false). The row has no artist or album, and
Lidarr 3.1.6 answered HTTP 500: IgnoredDownloadService.IgnoreDownload threw a
NullReferenceException. Each answer was counted against Lidarr."""
import os
import sys
import unittest
from types import SimpleNamespace

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402

UNIGNORABLE = (b'{"message": "Object reference not set to an instance of an object.", '
               b'"description": "System.NullReferenceException: Object reference not '
               b'set to an instance of an object.\\n   at NzbDrone.Core.Download.'
               b'IgnoredDownloadService.IgnoreDownload(TrackedDownload trackedDownload) '
               b'in ./NzbDrone.Core/Download/IgnoredDownloadService.cs:line 28"}')
OTHER = (b'{"message": "database is locked", "description": "code = Busy"}')


class LidarrDelete500:
    """Every DELETE answers 500 with `body`; counts the DELETEs."""

    def __init__(self, body):
        self.headers, self.body, self.deletes = {}, body, 0

    def request(self, method, url, **kw):
        r = requests.Response()
        r.url = url
        if method.upper() == "DELETE":
            self.deletes += 1
            r.status_code, r._content = 500, self.body
        else:
            r.status_code, r._content = 200, b"[]"
        return r


def client(body):
    cfg = SimpleNamespace(base_url="http://lidarr", api_key="k",
                          path_mapping_from="", path_mapping_to="",
                          library_root_lidarr="/music", library_root_windows="/music",
                          manualimport_timeout=60)
    s = LidarrDelete500(body)
    return L.LidarrClient(cfg, session=s), s


class UnignorableRow(unittest.TestCase):

    def test_it_is_not_counted_against_lidarr_and_not_asked_again(self):
        c, s = client(UNIGNORABLE)
        self.assertFalse(c.queue_remove(468890554))
        self.assertFalse(c.queue_remove(468890554))
        self.assertEqual(s.deletes, 1)
        self.assertEqual(c.failure_generation, 0)
        self.assertTrue(c.available())

    def test_another_lidarr_error_still_counts_and_is_asked_again(self):
        c, s = client(OTHER)
        self.assertFalse(c.queue_remove(1))
        self.assertFalse(c.queue_remove(1))
        self.assertEqual(s.deletes, 2)
        self.assertGreater(c.failure_generation, 0)


if __name__ == "__main__":
    unittest.main()
