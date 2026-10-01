"""The post-import check finds the album under the artist Lidarr filed it
under, when the CUE's performer is another name for it.

'Ray Parker Jr. And Raydio' / A Woman Needs Love landed 8/8 under 'Ray
Parker Jr.' and was recorded "imported_unverified"."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402


class FolderNamesTheArtist(unittest.TestCase):

    def test_the_artist_folder_answers_when_the_performer_does_not(self):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(lidarr_verify_timeout_seconds=0)
        album_dir = Path("/music/Music/Ray Parker Jr/A Woman Needs Love (1981)")
        o._find_album_on_disk = lambda a, b: (album_dir, ["x"] * 8)
        o.lidarr = SimpleNamespace(
            find_artist=lambda n: {"id": 7} if n == "Ray Parker Jr" else None,
            refresh_artist=lambda aid: None,
            windows_to_lidarr=str,
            downloaded_albums_scan_rescan=lambda p: None)
        o._lidarr_album_is_imported = lambda a, b, artist_id=None, album_id=None: (
            (True, 8, 8, 7, 99) if artist_id == 7 else (False, 0, 0, None, None))
        self.assertTrue(o._verify_library_reflects_album(
            "Ray Parker Jr. And Raydio", "A Woman Needs Love", 8))


if __name__ == "__main__":
    unittest.main()
