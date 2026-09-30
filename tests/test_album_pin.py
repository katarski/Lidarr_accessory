"""An album identified by content (or picked by the LLM) is the album the
rest of its hand-off works on (llm LLM-5): content-identify returned only a
title, and each later lookup re-resolved it by its own rule -- 'The Sky Is
Crying' identified as one album, matched by the pre-flight to another of that
title, whose release was then switched. And an LLM pick of an owned album is
refused when the download names another year (llm missed)."""
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

A = {"id": 1, "artistId": 7, "title": "The Sky Is Crying", "monitored": True,
     "statistics": {"trackFileCount": 0, "totalTrackCount": 18}}
B = {"id": 2, "artistId": 7, "title": "The Sky Is Crying", "monitored": True,
     "statistics": {"trackFileCount": 12, "totalTrackCount": 12}}


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o._tl = threading.local()
    o.lidarr = SimpleNamespace(
        failure_generation=0,
        find_album=lambda aid, title, **k: B,
        find_artist=lambda name: {"id": 7, "artistName": name},
        list_albums_for_artist=lambda aid: [B, A],
        get_album=lambda i: {1: A, 2: B}[i])
    return o


class AlbumPin(unittest.TestCase):

    def test_the_pinned_album_answers_for_its_title(self):
        o = _orch()
        o._tl.album_pin = A
        self.assertIs(o._find_album(7, "The Sky Is Crying"), A)
        self.assertIs(o._find_album(7, "Another Title"), B)
        self.assertIs(o._find_album(8, "The Sky Is Crying"), B)   # other artist
        o._tl.album_pin = None
        self.assertIs(o._find_album(7, "The Sky Is Crying"), B)

    def test_the_gap_check_uses_the_pinned_album(self):
        o = _orch()
        self.assertEqual(o._monitored_album_status("Elmore James",
                                                   "The Sky Is Crying")[0],
                         "redundant")                   # first by title: B
        o._tl.album_pin = A
        self.assertEqual(o._monitored_album_status("Elmore James",
                                                   "The Sky Is Crying")[0],
                         "import")

    def test_the_pin_lives_for_one_hand_off(self):
        o = _orch()
        seen = []

        def inner(*a, **k):
            o._tl.identified_album = A
            o._adopt_identified_album()
            seen.append(o._find_album(7, "The Sky Is Crying"))
        o._handoff_inner = inner
        o._handoff_pre_split_to_lidarr(Path("/x/a.cue"), Path("/x/folder"))
        self.assertEqual(seen, [A])
        self.assertIsNone(o._tl.album_pin)


class LlmPickNamesAnotherYear(unittest.TestCase):

    def test_an_owned_album_of_another_year_is_not_the_download(self):
        import dedup_downloads as DD
        owned = {"id": 5, "title": "Evanescence", "releaseDate": "2011-10-07",
                 "statistics": {"trackFileCount": 12, "totalTrackCount": 12}}
        lid = SimpleNamespace(
            find_artist=lambda n: {"id": 3, "artistName": "Evanescence"},
            list_albums_for_artist=lambda aid: [owned],
            get_album=lambda i: owned,
            list_tracks_for_album=lambda i: [{"hasFile": True}] * 12)
        llm = SimpleNamespace(pick_owned_album=lambda name, titles: "Evanescence")
        complete, _h, _t = DD.album_complete_in_library(
            lid, "Evanescence", "Origin Demos", llm=llm,
            folder_hint="Evanescence - Origin Demos 1998")
        self.assertFalse(complete)


if __name__ == "__main__":
    unittest.main()
