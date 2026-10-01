"""A grab qBittorrent refuses because it already holds the torrent is an
answer about that release, not Lidarr failing.

Lidarr passes qBittorrent's 409 Conflict on as HTTP 500 "Failed to connect
to qBittorrent". Fehlfarben - Monarchie und Alltag, held uncategorised, was
grabbed first every pass, and the count ended the whole pass there."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_not_found import client  # noqa: E402

CONFLICT = (b'{"message": "Failed to connect to qBittorrent, check your '
            b'settings.", "description": "NzbDrone.Core.Download.Clients.'
            b'DownloadClientException: Failed to connect to qBittorrent, check '
            b'your settings.\n ---> NzbDrone.Common.Http.HttpException: HTTP '
            b'request failed: [409:Conflict] [POST] at [http://q:8080/api/v2/'
            b'torrents/add]"}')


class AlreadyInTheClient(unittest.TestCase):

    def test_the_conflict_is_not_counted_against_lidarr(self):
        c, inner = client(body=CONFLICT)
        self.assertFalse(c.release_grab("g", 7, allow_unmonitored=True))
        self.assertEqual(c.failure_generation, 0)
        self.assertTrue(c.available())

    def test_other_lidarr_errors_still_count(self):
        c, inner = client(body=b'{"message": "database is locked"}')
        self.assertFalse(c.release_grab("g", 7, allow_unmonitored=True))
        self.assertGreater(c.failure_generation, 0)


if __name__ == "__main__":
    unittest.main()
