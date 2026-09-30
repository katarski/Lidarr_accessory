"""No ManualImport switches an album onto a release that orphans its files.

Lidarr makes the release an import names the album's monitored release.
On 30 Sep 'ABBA' (1975) moved from its 11-track release to an 18-track one
and 13 of its filed tracks lost their files: on disk, with no record."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lidarr as L  # noqa: E402

OLD, NEW = 180, 191


class _Client(L.LidarrClient):
    """The real guard over a fake Lidarr: album 17 monitors OLD and holds
    'Mamma Mia' (recording m1) and 'SOS' (s-old); NEW carries m1 and s-new."""

    def __init__(self, fail=False, held=True):
        self.failure_generation = 0
        self.posted = []
        self.fail = fail
        self.held = held

    def get_album(self, album_id):
        if self.fail:
            self.failure_generation += 1
            return None
        return {"id": album_id, "title": "ABBA",
                "releases": [{"id": OLD, "monitored": True},
                             {"id": NEW, "monitored": False}]}

    def list_tracks_for_album(self, album_id):
        return [{"id": 1, "foreignRecordingId": "m1", "hasFile": self.held},
                {"id": 2, "foreignRecordingId": "s-old", "hasFile": self.held}]

    def list_tracks_for_release(self, album_id, release_id):
        if release_id == NEW:
            return [{"id": 11, "foreignRecordingId": "m1"},
                    {"id": 12, "foreignRecordingId": "s-new"}]
        return [{"id": 1, "foreignRecordingId": "m1"},
                {"id": 2, "foreignRecordingId": "s-old"}]

    def _post(self, path, payload):
        self.posted.append(payload)
        return {"id": 99}


def _item(rid, path="/downloads/x/01.flac"):
    return {"path": path, "artist": {"id": 4}, "album": {"id": 17},
            "albumRelease": {"id": rid}, "tracks": [{"id": 11}]}


def _file(rid, path="/downloads/x/01.flac"):
    return {"path": path, "artistId": 4, "albumId": 17,
            "albumReleaseId": rid, "trackIds": [11]}


class ReleaseSwitchGuard(unittest.TestCase):

    def test_an_orphaning_switch_is_not_sent(self):
        c = _Client()
        self.assertIsNone(c.manual_import_apply([_item(NEW)]))
        self.assertIsNone(c.manual_import_apply_files([_file(NEW)]))
        self.assertEqual(c.posted, [])

    def test_the_monitored_release_is_sent(self):
        c = _Client()
        self.assertEqual(c.manual_import_apply([_item(OLD)]), 99)
        self.assertEqual(c.manual_import_apply_files([_file(OLD)]), 99)

    def test_an_album_with_no_files_may_switch(self):
        c = _Client(held=False)
        self.assertEqual(c.manual_import_apply([_item(NEW)]), 99)

    def test_only_the_orphaning_files_are_dropped(self):
        c = _Client()
        c.manual_import_apply_files([_file(NEW, "/a.flac"), _file(OLD, "/b.flac")])
        self.assertEqual([f["path"] for f in c.posted[0]["files"]], ["/b.flac"])

    def test_an_album_lidarr_cannot_describe_sends_nothing(self):
        c = _Client(fail=True)
        self.assertIsNone(c.manual_import_apply([_item(OLD)]))
        self.assertEqual(c.posted, [])

    def test_positional_is_guarded_too(self):
        c = _Client()
        self.assertIsNone(c.manual_import_positional(
            [{"path": "/a.flac"}], [{"id": 11}], 17, NEW, 4))
        self.assertEqual(c.posted, [])


if __name__ == "__main__":
    unittest.main()
