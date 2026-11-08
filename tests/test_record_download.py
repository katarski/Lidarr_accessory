"""A download no listed album takes is given to Lidarr as the record its songs
are on MusicBrainz, and imported into it (owner, 31 Oct 2026: "force a sync
with lidarr ... given that musicbrainz ... confirms this is the correct music
for the missing album/artist")."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

REC = {"album": {"title": "Comp", "foreignAlbumId": "rg1"},
       "release": {"foreignReleaseId": "rel1"}, "hit": 2, "tracks": 2,
       "via": "its songs on MusicBrainz"}


class FakeLidarr:
    failure_generation = 0
    mb = object()
    web = None

    def __init__(self, filled=2):
        self.added, self.removed, self.monitored = [], [], []
        self.filled = filled

    def find_artist(self, name):
        return {"id": 7, "artistName": "Artist"} if name == "Artist" else None

    def add_album_unmonitored(self, alb, aid, new_artist_ok=False):
        self.added.append((alb["title"], aid))
        return dict({"id": 99}, **getattr(self, "answer", {}))

    def remove_empty_artist(self, artist_id):
        self.removed.append(("artist", artist_id))
        return True

    def get_album(self, album_id):
        return {"id": album_id, "releases": [
            {"id": 1, "foreignReleaseId": "other", "monitored": True},
            {"id": 2, "foreignReleaseId": "rel1", "monitored": False}]}

    def set_album_monitored_release(self, album_id, rid, keep_manual=False, **kw):
        self.monitored.append((album_id, rid, keep_manual))
        return True

    def wait_for_command(self, cmd, timeout_seconds=0):
        return {"status": "completed"}

    def list_tracks_for_album(self, album_id):
        return [{"hasFile": i < self.filled} for i in range(2)]

    def remove_added_album(self, album_id):
        self.removed.append(album_id)
        return True

    def refresh_artist(self, aid, force=False):
        return None


def _orch(lidarr):
    o = Orchestrator.__new__(Orchestrator)
    o.lidarr = lidarr
    o.cfg = SimpleNamespace()
    o.recorded = []
    o._read_audio_tags = lambda p: ("Artist", "Comp")
    o._album_from_tags = lambda a: "Comp"
    o._record_song_titles = lambda a: [(p, "Song %d" % i) for i, p in enumerate(a)]
    o._audio_duration_seconds = lambda p: 200.0
    o._import_library_folder_by_tracknumber = lambda alb, aid, a, **k: 5
    o._trigger_artist_refresh = lambda *a, **k: None
    o._record = lambda *a, **k: o.recorded.append(k.get("outcome"))
    return o


AUDIOS = [Path("/downloads/Artist - Comp/01.flac"), Path("/downloads/Artist - Comp/02.flac")]


class RecordStepForDownloads(unittest.TestCase):

    def call(self, o, rec=(REC, "")):
        with mock.patch("record_adder.identify_record", return_value=rec):
            return o._give_lidarr_the_download(
                AUDIOS[0].parent, AUDIOS[0].parent, AUDIOS, "Artist", "Comp", "r")

    def test_added_filed_on_its_release_and_recorded(self):
        lid = FakeLidarr()
        o = _orch(lid)
        self.assertTrue(self.call(o))
        self.assertEqual(lid.added, [("Comp", 7)])
        self.assertEqual(lid.monitored, [(99, 2, True)])
        self.assertEqual(o.recorded, ["imported_via_manual"])

    def test_nothing_filed_takes_the_added_album_back(self):
        lid = FakeLidarr(filled=0)
        o = _orch(lid)
        self.assertFalse(self.call(o))
        self.assertEqual(lid.removed, [99])
        self.assertEqual(o.recorded, [])

    def test_no_record_on_musicbrainz_adds_nothing(self):
        lid = FakeLidarr()
        o = _orch(lid)
        self.assertFalse(self.call(o, rec=(None, "no record of Artist carries its songs")))
        self.assertEqual(lid.added, [])

    def test_an_artist_lidarr_lacks_is_left(self):
        lid = FakeLidarr()
        o = _orch(lid)
        o._read_audio_tags = lambda p: ("Nobody", "")
        with mock.patch("record_adder.identify_record") as ident:
            self.assertFalse(o._give_lidarr_the_download(
                AUDIOS[0].parent, AUDIOS[0].parent, AUDIOS, "Nobody", "Comp", "r"))
            ident.assert_not_called()


class SearchesLidarrCannotRead(unittest.TestCase):
    """Owner, 8 Nov 2026: "Lidarr's album search fails with a 503 -- should
    not be failing". Lidarr 3.1.6 drops a whole answer when one result's track
    credits an artist its album does not list; 'VOCES8 A Choral Christmas'
    failed on its own record."""

    def client(self, by_id, server_ids):
        import requests
        from lidarr import LidarrClient
        c = LidarrClient.__new__(LidarrClient)
        bad = requests.Response()
        bad.status_code = 503
        bad._content = (b'{"message":"Search for \'x\' failed. Invalid response '
                        b'received from LidarrAPI."}')
        asked = []

        def get(path, **kw):
            asked.append((path, kw))
            if path == "/api/v1/config/metadataprovider":
                return {"metadataSource": ""}
            term = kw.get("term", "")
            if term.startswith("lidarr:") and term[7:] in by_id:
                return by_id[term[7:]]
            raise requests.HTTPError(response=bad)
        c._get = get

        class Answer:
            def __init__(self, ids):
                self.ids = ids

            def raise_for_status(self):
                if self.ids is None:
                    raise requests.HTTPError("502")

            def json(self):
                return [{"id": i} for i in self.ids]
        patcher = mock.patch("lidarr.requests.get",
                             side_effect=lambda url, **kw: (
                                 asked.append((url, kw)) or Answer(server_ids)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return c, asked

    def test_the_answer_is_read_by_us_and_each_record_by_id(self):
        c, asked = self.client({"a1": [{"title": "A Choral Christmas"}],
                                "a2": [{"title": "A Choral Tapestry"}]}, ["a1", "a2", "a1"])
        got = c.lookup_albums("VOCES8 A Choral Christmas")
        self.assertEqual([g["title"] for g in got],
                         ["A Choral Christmas", "A Choral Tapestry"])
        url, kw = next(a for a in asked if a[0].startswith("http"))
        self.assertEqual((url, kw["params"]),
                         ("https://api.lidarr.audio/api/v0.4/search",
                          {"type": "album", "query": "VOCES8 A Choral Christmas"}))

    def test_an_unread_answer_or_record_is_not_no_records(self):
        from lidarr import SearchUnread
        c, _ = self.client({}, None)
        with self.assertRaises(SearchUnread):
            c.lookup_albums("VOCES8 A Choral Christmas")
        c, _ = self.client({"a1": [{"title": "x"}]}, ["a1", "a9"])
        with self.assertRaises(SearchUnread):
            c.lookup_albums("VOCES8 A Choral Christmas")
        with self.assertRaises(SearchUnread):
            c.lookup_albums("lidarr:a9")

    def test_the_record_step_calls_it_not_judged(self):
        import record_adder as R
        from song_harvest import norm_title

        class Lid:
            def lookup_albums(self, term):
                raise RuntimeError("unread")

        class MB:
            def recording_releases_or_none(self, *a, **k):
                return []
        res, why = R.identify_record(
            Lid(), {"artistName": "VOCES8", "foreignArtistId": "v8"},
            ["A Choral Christmas"], [("Joy to the World", 200.0)], 1,
            norm_title, mb=MB())
        self.assertIsNone(res)
        self.assertIn("not judged", why)

    def test_not_judged_never_rejects_the_grab(self):
        lid = FakeLidarr()
        o = _orch(lid)
        rec = (None, "2 album search(es) could not be read -- not judged")
        self.assertIsNone(RecordStepForDownloads.call(self, o, rec=rec))
        import inspect
        src = inspect.getsource(Orchestrator)
        self.assertEqual(src.count("if given is not None and self._reject_wrong_grab("), 2)
        self.assertNotIn("if self._reject_wrong_grab(", src)


class ARecordOfAnotherArtist(unittest.TestCase):

    def test_added_with_its_own_artist_unmonitored(self):
        from lidarr import LidarrClient
        sent = []
        c = LidarrClient.__new__(LidarrClient)
        c._get = lambda path, **kw: {"id": 7, "qualityProfileId": 2,
                                     "metadataProfileId": 3,
                                     "path": "/music/VOCES8"}
        c._post = lambda path, body: sent.append(body) or {"id": 1, "artistId": 40}
        alb = {"title": "A Choral Christmas", "foreignAlbumId": "a1",
               "artist": {"id": None, "foreignArtistId": "cb4d",
                          "artistName": "VOCES8 Foundation Choir",
                          "qualityProfileId": 0}}
        c.add_album_unmonitored(alb, 7, new_artist_ok=True)
        art = sent[0]["artist"]
        self.assertEqual((art["foreignArtistId"], art["qualityProfileId"],
                          art["metadataProfileId"], art["rootFolderPath"],
                          art["monitored"], art["monitorNewItems"], art["addOptions"]),
                         ("cb4d", 2, 3, "/music", False, "none",
                          {"monitor": "none", "searchForMissingAlbums": False}))
        sent.clear()
        c.add_album_unmonitored(alb, 7)            # the library step: unchanged
        self.assertEqual(sent[0]["artist"]["qualityProfileId"], 0)

    def test_filed_under_the_new_artist_and_taken_back_when_nothing_lands(self):
        lid = FakeLidarr()
        lid.answer = {"artistId": 40, "artist": {"artistName": "VOCES8 Foundation Choir"}}
        o = _orch(lid)
        seen = []
        o._import_library_folder_by_tracknumber = lambda alb, aid, a, **k: seen.append(aid) or 5
        self.assertTrue(RecordStepForDownloads.call(self, o))
        self.assertEqual(seen, [40])
        lid2 = FakeLidarr(filled=0)
        lid2.answer = {"artistId": 40}
        self.assertFalse(RecordStepForDownloads.call(self, _orch(lid2)))
        self.assertEqual(lid2.removed, [99, ("artist", 40)])


if __name__ == "__main__":
    unittest.main()
