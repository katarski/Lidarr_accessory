"""The harvest's AcoustID gate proves a match by MusicBrainz id, and reads
spoken spellings as the same title.

Rejected every pass on 30 Sep: 'Hildegarde Neff' != 'Hildegard Knef' (one
artist), 'Your Cheating Heart' vs 'Your Cheatin' Heart', 'I Think It's
Going to Rain Today' vs '... Gonna Rain Today'."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import acoustid_client as AC  # noqa: E402
import song_harvest as SH  # noqa: E402


def _match(title, artist, rec="", art=""):
    ids = {k: v for k, v in (("recording_id", rec), ("artist_mbid", art)) if v}
    want = SH.WantedTrack(title=title, track_id=1, album_id=2, album_title="A",
                          artist_id=3, artist_name=artist, release_id=4, **ids)
    src = SH.SourceFile(path="/x/%s.mp3" % title, title=title, artist=artist)
    return SH.Match(src=src, want=want, delta_ms=0, confidence="exact")


class _AC:
    enabled = True

    def __init__(self, res):
        self.res = res

    def identify_file(self, path):
        return self.res


def _verify(m, res):
    ok, bad = SH.acoustid_verify([m], _AC(dict({"score": 0.9}, **res)))
    return bool(ok)


class AcoustIdGate(unittest.TestCase):

    def test_the_same_recording_id_is_proof(self):
        self.assertTrue(_verify(
            _match("Es kann zwischen heute und morgen", "Hildegard Knef", rec="r1"),
            {"title": "Zwischen Heute Und Morgen", "artist": "Hildegarde Neff",
             "recording_ids": ["r0", "r1"]}))

    def test_the_same_artist_id_under_another_name(self):
        self.assertTrue(_verify(
            _match("Without Love", "Hildegard Knef", art="knef"),
            {"title": "Without Love", "artist": "Hildegarde Neff",
             "artist_ids": ["knef"]}))

    def test_spoken_spellings_are_one_title(self):
        self.assertTrue(_verify(
            _match("Your Cheatin’ Heart", "Hank Williams"),
            {"title": "Your Cheating Heart", "artist": "Hank Williams"}))
        self.assertTrue(_verify(
            _match("I Think It’s Gonna Rain Today", "Nina Simone"),
            {"title": "I Think It’s Going to Rain Today",
             "artist": "Nina Simone"}))

    def test_another_song_is_still_rejected(self):
        self.assertFalse(_verify(
            _match("Tired of Crying", "Fats Domino", rec="r1", art="fd"),
            {"title": "Magic Isles", "artist": "Fats Domino",
             "recording_ids": ["r9"], "artist_ids": ["fd"]}))
        self.assertFalse(_verify(
            _match("Without Love", "Hildegard Knef", art="knef"),
            {"title": "Without Love", "artist": "Someone Else",
             "artist_ids": ["other"]}))


class NewRulesJudgeAgain(unittest.TestCase):

    def test_a_verdict_under_older_rules_is_not_unchanged(self):
        led = SH.HarvestLedger(None)
        led._seen["/src"] = {"folder": "1:2:3", "titles": [],
                             "wanted": "0:97d170e1550e"}     # before the bump
        self.assertFalse(led.unchanged("/src", "1:2:3", {}))
        led.mark("/src", "1:2:3", [], {})
        self.assertTrue(led.unchanged("/src", "1:2:3", {}))


class AcoustIdKeepsTheIds(unittest.TestCase):

    def test_best_carries_every_recording_and_artist_id(self):
        best = AC.AcoustIDClient._best({"results": [{"score": 0.9, "recordings": [
            {"id": "r1", "title": "T", "artists": [{"id": "a1", "name": "A"}]},
            {"id": "r2", "title": "T", "artists": [{"id": "a2", "name": "B"}]}]}]})
        self.assertEqual(best["recording_ids"], ["r1", "r2"])
        self.assertEqual(best["artist_ids"], ["a1", "a2"])

    def test_a_hit_stored_without_the_ids_is_asked_again(self):
        c = AC.AcoustIDClient.__new__(AC.AcoustIDClient)
        self.assertTrue(c._expired({"result": {"title": "T"}, "at": time.time()}))
        self.assertFalse(c._expired({"result": {"title": "T", "recording_ids": []},
                                     "at": time.time()}))


if __name__ == "__main__":
    unittest.main()
