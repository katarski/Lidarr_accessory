"""The post-import check asks for the album Lidarr filed the import under.

'Hank Williams, Jr. / Five-O-Five' was imported 10/10 into the album Lidarr
calls 'Five‐O' (folder 'Five‐O (1985)'); found again by the CUE's title it
was not there (albumId=None), and the import was recorded
imported_unverified."""
import inspect
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


def cand(album_id):
    return {"path": "/x.flac", "artist": {"id": 797}, "album": {"id": album_id}}


class CommittedAlbum(unittest.TestCase):

    def test_the_album_every_candidate_names(self):
        self.assertEqual(Orchestrator._committed_album_id([cand(55), cand(55)]), 55)
        self.assertIsNone(Orchestrator._committed_album_id([cand(55), cand(56)]))
        self.assertIsNone(Orchestrator._committed_album_id([{"path": "x"}]))

    def test_the_check_asks_for_that_album(self):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(lidarr_verify_timeout_seconds=0)
        o._find_album_on_disk = lambda a, b: (
            Path("/music/Music/Hank Williams, Jr/Five‐O (1985)"), ["f"] * 10)
        o._find_album = lambda *a, **k: None          # the title misses
        o._year_from_name = lambda n: None
        o.lidarr = SimpleNamespace(
            find_artist=lambda n: {"id": 797},
            get_album=lambda i: ({"id": 55, "artistId": 797, "title": "Five‐O"}
                                 if i == 55 else None),
            list_tracks_for_album=lambda i: [{"monitored": True, "hasFile": True}] * 10,
            refresh_artist=lambda *a, **k: None,
            windows_to_lidarr=str,
            downloaded_albums_scan_rescan=lambda *a, **k: None)
        self.assertTrue(o._verify_library_reflects_album(
            "Hank Williams, Jr.", "Five-O-Five", 10,
            imported_artist_id=797, imported_album_id=55))

    def test_every_import_path_passes_it(self):
        src = inspect.getsource(Orchestrator)
        self.assertEqual(src.count("imported_album_id=self._committed_album_id("), 2)
        self.assertIn("imported_album_id=imported_album_id,", src)


if __name__ == "__main__":
    unittest.main()
