"""MusicBrainz names records Lidarr's list leaves out (singles,
compilations): a download it names as one of those is not owned, and no
model is asked. VOCES8's 'A Choral Christmas' (2023) went to the model,
which said the owned 'Christmas' (2012)."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as D  # noqa: E402

XMAS = {"id": 1, "title": "Christmas", "foreignAlbumId": "rg-xmas",
        "releaseDate": "2012-01-01",
        "statistics": {"trackFileCount": 18, "totalTrackCount": 18}}
JOKER = {"id": 2, "title": "Joker: Original Motion Picture Soundtrack",
         "foreignAlbumId": "rg-joker", "releaseDate": "2019-01-01",
         "statistics": {"trackFileCount": 12, "totalTrackCount": 12}}


def run(albums, groups, name, model_says):
    by_id = {a["id"]: a for a in albums}
    lid = SimpleNamespace(
        find_artist=lambda n: {"id": 7, "foreignArtistId": "artist-mbid"},
        list_albums_for_artist=lambda i: albums,
        get_album=lambda i: by_id[i],
        list_tracks_for_album=lambda i: [],
        mb=SimpleNamespace(release_groups=lambda mbid, **k: groups))
    asked = []

    def pick(download, owned):
        asked.append(download)
        return model_says
    return D.album_complete_in_library(
        lid, "Artist", name, llm=SimpleNamespace(pick_owned_album=pick)), asked


class MusicBrainzIdentity(unittest.TestCase):

    def test_a_record_lidarr_does_not_list_is_not_owned(self):
        out, asked = run([XMAS], [{"title": "Christmas", "mbid": "rg-xmas"},
                                  {"title": "A Choral Christmas", "mbid": "rg-choral"}],
                         "A Choral Christmas FLAC ⭐️", "Christmas")
        self.assertEqual((out, asked), ((False, 0, 0), []))

    def test_a_record_lidarr_lists_under_another_title_is_that_album(self):
        out, asked = run([JOKER], [{"title": "Joker", "mbid": "rg-joker"}],
                         "Joker", None)
        self.assertEqual((out, asked), ((True, 12, 12), []))

    def test_without_an_answer_the_usual_way_goes_on(self):
        out, asked = run([XMAS], [], "A Choral Christmas", None)
        self.assertEqual((out, asked), ((False, 0, 0), ["A Choral Christmas"]))


if __name__ == "__main__":
    unittest.main()
