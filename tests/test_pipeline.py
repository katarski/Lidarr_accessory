"""Library matching, deselect deferral, the orchestrator's LLM wait list and
the CUE repair error -- the places an LLM that cannot be asked used to be
recorded as a 'no'."""
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as dd  # noqa: E402
import ollama_client as oc  # noqa: E402


def album(i, title, have, total, year="2000"):
    return {"id": i, "title": title, "releaseDate": year + "-01-01",
            "statistics": {"trackFileCount": have, "totalTrackCount": total}}


class FakeLidarr:
    def __init__(self, albums):
        self.albums = {a["id"]: a for a in albums}

    def find_artist(self, name):
        return {"id": 1, "artistName": name}

    def list_albums_for_artist(self, _id):
        return list(self.albums.values())

    def get_album(self, i):
        return self.albums[i]

    def list_tracks_for_album(self, _i):
        return []


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def pick_owned_album(self, download, owned):
        self.asked.append((download, owned))
        return self.answer


class NormTitle(unittest.TestCase):
    def test_non_latin_titles_no_longer_collide(self):
        self.assertTrue(dd.norm_title("ラブ・イズ・オーヴァー"))
        self.assertNotEqual(dd.norm_title("ラブ・イズ・オーヴァー"), dd.norm_title("愛の歌"))
        self.assertNotEqual(dd.norm_title("Αγάπη"), dd.norm_title("Ελπίδα"))

    def test_the_old_equalities_still_hold(self):
        self.assertEqual(dd.norm_title("Traveler's Blues"), dd.norm_title("Travelers Blues"))
        self.assertEqual(dd.norm_title("Up!"), dd.norm_title("Up"))
        self.assertEqual(dd.norm_title("Disintegration (Deluxe)"), dd.norm_title("Disintegration"))
        self.assertNotEqual(dd.norm_title("Rare Pearls"), dd.norm_title("Pearl"))

    def test_cyrillic_disc_folder(self):
        self.assertTrue(dd.is_disc_folder("Диск 2"))
        self.assertTrue(dd.is_disc_folder("CD2"))
        self.assertFalse(dd.is_disc_folder("Группа крови"))


class LibraryMatch(unittest.TestCase):
    def test_llm_pick_goes_through_the_same_title_guard(self):
        lid = FakeLidarr([album(1, "Evanescence", 16, 16, "2011"),
                          album(2, "Evanescence", 0, 11, "1998")])
        got = dd.album_complete_in_library(lid, "Evanescence", "Evanescence EP",
                                           llm=FakeLLM("Evanescence"), out={})
        self.assertFalse(got[0])

    def test_llm_unavailable_is_deferred_not_missing(self):
        lid = FakeLidarr([album(1, "Blackstar", 7, 7)])
        out = {}
        got = dd.album_complete_in_library(lid, "David Bowie", "★ (Vinyl Rip)",
                                           llm=FakeLLM(oc.UNAVAILABLE), out=out)
        self.assertEqual(got, (False, 0, 0))
        self.assertTrue(out.get("llm_deferred"))

    def test_word_subset_works_in_other_scripts(self):
        lid = FakeLidarr([album(1, "Группа крови", 11, 11)])
        got = dd.album_complete_in_library(lid, "Кино", "Группа крови Remastered 2012")
        self.assertTrue(got[0])


class Deselect(unittest.TestCase):
    def test_owner_path_check_uses_whole_names(self):
        import qbt_deselect as q
        self.assertFalse(q._path_names_artist(["X-Ray Spex - Germfree Adolescents"], "X"))
        self.assertTrue(q._path_names_artist(["Cocteau Twins", "Treasure"], "Cocteau Twins"))
        self.assertTrue(q.plan_is_deferred([{"deferred": True}, {}]))
        self.assertFalse(q.plan_is_deferred([{}]))


class OrchestratorWait(unittest.TestCase):
    def stub(self, available):
        from orchestrator import Orchestrator
        requeued = []
        led = {"/d/A": ["sig", 1], "/d/B.cue": ["sig", 1]}
        s = SimpleNamespace(
            _llm_waiting={}, _llm_wait_lock=threading.Lock(),
            ollama=SimpleNamespace(available=lambda: available),
            requeue_cue=requeued.append, _skip_seen={Path("/d/A"), Path("/d/B.cue")},
            _reconcile_cache={"/d/A": "x"}, _sweep_led_dirty=False)
        s._sweep_ledger = lambda: led
        for name in ("_llm_wait", "_llm_answered", "_llm_blocked", "_release_llm_waiting"):
            setattr(s, name, getattr(Orchestrator, name).__get__(s))
        return s, requeued, led

    def test_nothing_is_released_while_the_llm_cannot_be_asked(self):
        s, requeued, led = self.stub(False)
        s._llm_wait("/d/A", "content-identify")
        self.assertTrue(s._llm_blocked("/d/A"))
        self.assertEqual(s._release_llm_waiting(), 0)
        self.assertIn("/d/A", led)

    def test_release_forgets_every_write_off_and_requeues_cues(self):
        s, requeued, led = self.stub(True)
        s._llm_wait("/d/A", "content-identify")
        s._llm_wait("/d/B.cue", "CUE repair")
        self.assertEqual(s._release_llm_waiting(), 2)
        self.assertEqual(s._skip_seen, set())
        self.assertEqual(led, {})
        self.assertEqual(s._reconcile_cache, {})
        self.assertEqual(requeued, [Path("/d/B.cue")])
        self.assertTrue(s._sweep_led_dirty)


class CueRepair(unittest.TestCase):
    def test_unavailable_repair_is_its_own_error(self):
        import tempfile
        from cue_parser import LLMUnavailableError, parse_cue
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.cue"
            p.write_text("this is not a cue sheet\n", encoding="utf-8")
            llm = SimpleNamespace(repair_cue=lambda text: oc.UNAVAILABLE, enabled=True)
            with self.assertRaises(LLMUnavailableError):
                parse_cue(p, 100.0, ollama=llm)
            llm = SimpleNamespace(repair_cue=lambda text: "", enabled=True)
            with self.assertRaises(ValueError) as cm:
                parse_cue(p, 100.0, ollama=llm)
            self.assertNotIsInstance(cm.exception, LLMUnavailableError)


class SeenSet(unittest.TestCase):
    def test_forgets_on_content_change_and_after_ttl(self):
        import tempfile
        import time
        from orchestrator import _SeenSet
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Album"
            folder.mkdir()
            (folder / "01.flac").write_bytes(b"x")
            seen = _SeenSet(3600)
            seen.add(folder)
            self.assertIn(folder, seen)
            (folder / "02.flac").write_bytes(b"y")          # more files arrived
            self.assertNotIn(folder, seen)
            seen.add(folder)
            seen.ttl = 0.0
            time.sleep(0.01)
            self.assertNotIn(folder, seen)
            seen = _SeenSet(3600)
            seen.add(Path(d) / "gone#dvda")                  # no content: TTL only
            self.assertIn(Path(d) / "gone#dvda", seen)
            seen.discard(Path(d) / "gone#dvda")
            self.assertNotIn(Path(d) / "gone#dvda", seen)


if __name__ == "__main__":
    unittest.main()
