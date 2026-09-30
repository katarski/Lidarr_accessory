"""The library audit does not repeat a repair on a folder that has not
changed since it last acted on it (neither on disk nor in Lidarr).

Every pass re-ran the whole repair -- two RefreshArtist calls, an album PUT,
a /manualimport probe -- on every folder it could not fix."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

ALBUM = {"id": 9, "title": "Album", "artistId": 5,
         "statistics": {"trackFileCount": 1, "totalTrackCount": 5}}


def _orch(root, state, touched, rows):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(library_root_windows=root,
                            library_audit_report_file=Path(state) / "r.csv")
    o.lidarr = SimpleNamespace(
        failure_generation=0,
        list_albums_for_artist=lambda aid: [ALBUM],
        get_album=lambda i: ALBUM,
        refresh_artist=lambda aid: touched.append("refresh"),
        set_album_auto_switch=lambda i, v: touched.append("put"),
        manual_import_candidates=lambda p, **k: touched.append("probe") or [],
        windows_to_lidarr=str)
    o._audit_load_report = lambda p: (True, set())      # act mode
    o._build_lidarr_artist_index = lambda: {"artist": {"id": 5}}
    o._lidarr_lookup_artist = lambda name, idx: {"id": 5}
    o._library_album_dirs = lambda d: [p for p in d.iterdir() if p.is_dir()]
    o._audit_append_rows = lambda f, r: rows.extend(r)
    o._prefer_lossless_in_album = lambda *a: None
    o._align_release_to_disk = lambda *a, **k: None
    o._import_library_folder_by_tracknumber = lambda *a, **k: None
    o._album_from_tags = lambda a: ""
    return o


class AuditRemembersWhatItDid(unittest.TestCase):

    def test_an_unchanged_folder_is_not_repaired_again(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as state:
            root = Path(tmp) / "Music"
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            for n in ("01.flac", "02.flac", "03.flac"):
                (album / n).write_bytes(b"x")
            touched, rows = [], []
            _orch(root, state, touched, rows).audit_library_vs_lidarr()
            self.assertIn("refresh", touched)
            n = len(touched)
            _orch(root, state, touched, rows).audit_library_vs_lidarr()  # restart
            self.assertEqual(len(touched), n)
            self.assertIn("not repeated", rows[-1][-1])
            (album / "04.flac").write_bytes(b"y")                      # changed
            os.utime(album / "04.flac", (2e9, 2e9))
            _orch(root, state, touched, rows).audit_library_vs_lidarr()
            self.assertGreater(len(touched), n)


if __name__ == "__main__":
    unittest.main()
