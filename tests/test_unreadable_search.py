"""A search whose answer Lidarr cannot read finds no records; it is not
Lidarr failing.

Every 'James Blake ...' album search answered HTTP 503 "Invalid response
received from LidarrAPI" from 12:50 on 1 Oct 2026 (Lidarr's metadata reply
named an artist it did not carry), while other searches answered. Counted as
a failure, the record step for '200 Press' was "not judged" on every audit
pass, though its songs' release group looked up fine."""
import os
import sys
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402
import record_adder as R  # noqa: E402
from song_harvest import norm_title  # noqa: E402

UNREADABLE = (b'{"message": "Search for \'James Blake 200 Press\' failed. Invalid '
              b'response received from LidarrAPI.", "description": "NzbDrone.Core.'
              b'MetadataSource.SkyHook.SkyHookException: Search for \'James Blake '
              b'200 Press\' failed. Invalid response received from LidarrAPI.\\n   '
              b'at NzbDrone.Core.MetadataSource.SkyHook.SkyHookProxy.SearchForNewAlbum'
              b'(String title, String artist)"}')
UNREACHED = (b'{"message": "Search for \'HIND\'S HALL\' failed. Unable to communicate '
             b'with LidarrAPI.", "description": "NzbDrone.Core.MetadataSource.SkyHook.'
             b'SkyHookException: Search for \'HIND\'S HALL\' failed. Unable to '
             b'communicate with LidarrAPI. HTTP request failed: [500:InternalServerError]"}')
EP = {"foreignAlbumId": "877128d5", "title": "200 Press", "albumType": "EP",
      "secondaryTypes": [], "artist": {"foreignArtistId": "jb"},
      "releases": [{"foreignReleaseId": "bbde", "trackCount": 4}]}
SONGS = ["200 Press", "Building It Still", "Words That We Both Know", "200 Pressure"]


class Lidarr503:
    """Name searches answer 503 with `body`; 'lidarr:<id>' answers the EP."""

    def __init__(self, body):
        self.headers, self.body = {}, body

    def request(self, method, url, **kw):
        term = (kw.get("params") or {}).get("term") or \
            parse_qs(urlparse(url).query).get("term", [""])[0]
        r = requests.Response()
        r.url = url
        if term.startswith("lidarr:"):
            r.status_code, r._content = 200, json_bytes([EP])
        else:
            r.status_code, r._content = 503, self.body
        return r


def json_bytes(obj):
    import json
    return json.dumps(obj).encode()


def client(body):
    cfg = SimpleNamespace(base_url="http://lidarr", api_key="k",
                          path_mapping_from="", path_mapping_to="",
                          library_root_lidarr="/music", library_root_windows="/music",
                          manualimport_timeout=60)
    return L.LidarrClient(cfg, session=Lidarr503(body))


class _MB:
    def release_track_titles(self, mbid):
        return SONGS if mbid == "bbde" else []

    def recording_releases_or_none(self, title, artist, duration=0.0):
        return [{"rg": "877128d5"}]


class UnreadableSearch(unittest.TestCase):

    def test_it_finds_nothing_and_is_not_counted_against_lidarr(self):
        c = client(UNREADABLE)
        self.assertEqual(c.lookup_albums("James Blake 200 Press"), [])
        self.assertEqual(c.failure_generation, 0)
        self.assertTrue(c.available())

    def test_a_metadata_server_lidarr_cannot_reach_still_counts(self):
        c = client(UNREACHED)
        with self.assertRaises(requests.HTTPError):
            c.lookup_albums("HIND'S HALL")
        self.assertGreater(c.failure_generation, 0)

    def test_the_record_is_found_by_its_songs_and_the_verdict_stands(self):
        # What _give_lidarr_the_record checks: the generation it read before
        # identify_record must be the one after, or nothing is judged.
        c = client(UNREADABLE)
        gen = c.failure_generation
        res, why = R.identify_record(
            c, {"artistName": "James Blake", "foreignArtistId": "jb"},
            ["200 Press", "200 Press (2014)"], [(s, 200.0) for s in SONGS], 4,
            norm_title, mb=_MB())
        self.assertEqual((res["album"]["title"], res["hit"], res["via"]),
                         ("200 Press", 4, "its songs on MusicBrainz"))
        self.assertEqual(c.failure_generation, gen)


if __name__ == "__main__":
    unittest.main()
