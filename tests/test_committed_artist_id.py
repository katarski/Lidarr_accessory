"""The post-import check is given the artist Lidarr filed a ManualImport
under. Candidates carry it as `artist.id`; the split path and the pre-split
hand-off read `artistId` only, found none, and fell back to the CUE's
performer: 'Ray Parker Jr. And Raydio' / A Woman Needs Love, 8/8 under
'Ray Parker Jr.', was recorded imported_unverified."""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

CAND = {"path": "/x/01.flac", "artist": {"id": 807, "artistName": "Ray Parker Jr."},
        "album": {"id": 5}, "release": {"id": 6}, "tracks": [{"id": 1}]}


class CommittedArtist(unittest.TestCase):

    def test_the_artist_comes_from_the_candidate_record(self):
        self.assertEqual(Orchestrator._committed_artist_id([CAND]), 807)
        self.assertEqual(Orchestrator._committed_artist_id([{"artistId": 9}]), 9)
        self.assertIsNone(Orchestrator._committed_artist_id([{"path": "x"}, None]))

    def test_every_manual_import_path_reads_it(self):
        src = inspect.getsource(Orchestrator)
        self.assertNotIn('aid = h.get("artistId")\n', src)
        self.assertGreaterEqual(src.count("self._committed_artist_id("), 3)


if __name__ == "__main__":
    unittest.main()
