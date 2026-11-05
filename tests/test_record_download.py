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

    def add_album_unmonitored(self, alb, aid):
        self.added.append((alb["title"], aid))
        return {"id": 99}

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


if __name__ == "__main__":
    unittest.main()
