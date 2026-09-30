"""Lidarr answering NotFound (as HTTP 500) is an answer, not an outage.

A queue row Lidarr lists but cannot delete by id (575591606) was DELETEd
every 10 minutes; each 500 opened the breaker for every thread."""
import os
import sys
import unittest
from types import SimpleNamespace

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402

NOT_FOUND = (b'{"message": "NotFound", "description": "Lidarr.Http.REST.'
             b'NotFoundException: NotFound"}')


class Inner:
    def __init__(self, status=500, body=NOT_FOUND):
        self.headers, self.calls = {}, 0
        self.status, self.body = status, body

    def request(self, method, url, **kw):
        self.calls += 1
        r = requests.Response()
        r.status_code, r._content = self.status, self.body
        return r


def client(**kw):
    cfg = SimpleNamespace(base_url="http://lidarr", api_key="k",
                          path_mapping_from="", path_mapping_to="",
                          library_root_lidarr="/music", library_root_windows="/music",
                          manualimport_timeout=60)
    inner = Inner(**kw)
    return L.LidarrClient(cfg, session=inner), inner


class NotFoundIsAnAnswer(unittest.TestCase):

    def test_not_found_does_not_open_the_breaker(self):
        c, inner = client()
        self.assertFalse(c.queue_remove(575591606))
        self.assertEqual(c.failure_generation, 0)
        self.assertTrue(c.available())

    def test_the_row_is_not_asked_about_again(self):
        c, inner = client()
        c.queue_remove(575591606)
        c._down_until = 0.0            # no breaker in the way either way
        c.queue_remove(575591606)
        self.assertEqual(inner.calls, 1)

    def test_lidarrs_own_error_counts_but_leaves_the_breaker_shut(self):
        c, inner = client(body=b'{"message": "Failed to connect to qBittorrent"}')
        self.assertFalse(c.queue_remove(1))
        self.assertGreater(c.failure_generation, 0)
        self.assertTrue(c.available())

    def test_a_500_without_lidarrs_answer_opens_the_breaker(self):
        for status, body in ((500, b"<html>oops</html>"), (502, b"")):
            c, inner = client(status=status, body=body)
            self.assertFalse(c.queue_remove(1))
            self.assertGreater(c.failure_generation, 0)
            self.assertFalse(c.available(), status)


if __name__ == "__main__":
    unittest.main()
