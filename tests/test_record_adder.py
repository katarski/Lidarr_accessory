"""A library folder whose record Lidarr does not list is identified by its
songs and given to Lidarr -- one album, by hand, unmonitored -- and owned.

Donny Hathaway / ' In Performance (1980)' is a live album; the owner's
metadata profile lists studio albums and soundtracks only. Added by hand on
1 Oct it survived the refresh, whose rescan linked the 6 files: 6/6."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import record_adder as R  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from song_harvest import norm_title  # noqa: E402

ARTIST = {"id": 113, "artistName": "Donny Hathaway", "foreignArtistId": "dh"}
SONGS = ["To Be Young, Gifted and Black", "A Song for You", "Nu-Po",
         "I Love You More Than You'll Ever Know", "We Need You Right Now",
         "Sack Full of Dreams"]


def album(fid, title, rels, artist="dh", kind=("Album", ["Live"]), lid=None):
    return {"id": lid, "foreignAlbumId": fid, "title": title,
            "albumType": kind[0], "secondaryTypes": kind[1],
            "artist": {"foreignArtistId": artist},
            "releases": [{"foreignReleaseId": r, "trackCount": n} for r, n in rels]}


class _MB:
    def __init__(self, tracklists, recordings=None):
        self.tracklists, self.recordings = tracklists, recordings or {}

    def release_track_titles(self, mbid):
        return self.tracklists.get(mbid, [])

    def recording_releases_or_none(self, title, artist, duration=0.0):
        return [{"rg": rg} for rg in self.recordings.get(title, [])]


class _Lookup:
    def __init__(self, answers):
        self.answers, self.terms = answers, []

    def lookup_albums(self, term):
        self.terms.append(term)
        return self.answers.get(term, [])


def ident(lookup, mb, names=("In Performance",), songs=SONGS, web=None):
    return R.identify_record(lookup, ARTIST, list(names),
                             [(s, 300.0) for s in songs], len(songs),
                             norm_title, mb=mb, web=web)


PERF = album("perf", "In Performance", [("r6", 6)])
LIVE_PLUS = album("liveplus", "Live + In Performance", [("r14", 14)],
                  kind=("Album", ["Compilation", "Live"]))


class Identify(unittest.TestCase):

    def test_a_live_album_lidarr_does_not_list_is_found_by_its_name(self):
        mb = _MB({"r6": SONGS, "r14": SONGS + ["x%d" % i for i in range(8)]})
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [LIVE_PLUS, PERF]}), mb)
        self.assertEqual((res["album"]["title"], res["release"]["foreignReleaseId"],
                          res["hit"], res["via"]),
                         ("In Performance", "r6", 6, "its name"))

    def test_a_few_shared_songs_are_not_the_record(self):
        # Barbara Cook's 'No One Is Alone' shares two songs with 'Oscar Winners'.
        oscar = album("oscar", "Oscar Winners", [("r13", 13)])
        mb = _MB({"r13": SONGS[:2] + ["y%d" % i for i in range(11)]})
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [oscar]}), mb)
        self.assertIsNone(res)
        self.assertIn("no record of Donny Hathaway carries its songs", why)

    def test_another_artists_record_is_not_taken(self):
        va = album("va", "In Performance", [("r6", 6)], artist="various")
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [va]}), _MB({"r6": SONGS}))
        self.assertIsNone(res)

    def test_an_unlisted_record_must_be_nearly_all_here_or_carry_its_name(self):
        # The Beatles / 'Magical Mystery Tour (1967)': Lidarr's lookup offered
        # only 'The Beatles Collection, Volume 7' (17 tracks), 11 of them its.
        mb = _MB({"c10": SONGS + ["z%d" % i for i in range(4)]})
        coll = album("coll", "The Collection, Volume 7", [("c10", 10)],
                     kind=("Album", ["Compilation"]))
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [coll]}), mb)
        self.assertIsNone(res)
        named = album("named", "In Performance: The Best Of", [("c10", 10)],
                      kind=("Album", ["Compilation"]))
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [named]}), mb)
        self.assertEqual(res["album"]["title"], "In Performance: The Best Of")
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [dict(coll, id=7)]}), mb)
        self.assertEqual(res["album"]["id"], 7)    # listed: only its gaps are filled

    def test_its_songs_find_it_when_its_name_does_not(self):
        mb = _MB({"r6": SONGS}, recordings={s: ["perf"] for s in SONGS[:4]})
        lookup = _Lookup({"lidarr:perf": [PERF]})
        res, why = ident(lookup, mb, names=("Unknown Tape 3",))
        self.assertEqual((res["album"]["title"], res["via"]),
                         ("In Performance", "its songs on MusicBrainz"))

    def test_songs_on_many_records_name_none_of_them(self):
        # Édith Piaf / 'je sais comment': 9 release groups voted for, and
        # which 3 were read decided it ('Milord', then 'L'Intégrale').
        mb = _MB({"r6": SONGS}, recordings={s: ["perf", "a", "b", "c"] for s in SONGS})
        lookup = _Lookup({"lidarr:perf": [PERF]})
        res, why = ident(lookup, mb, names=("je sais comment",))
        self.assertEqual((res, why), (None, "its songs are on 4 records of "
                                            "Donny Hathaway -- not sure which"))
        self.assertNotIn("lidarr:perf", lookup.terms)

    def test_two_records_that_fit_as_well_are_no_answer(self):
        twin = album("twin", "In Concert", [("t6", 6)])
        mb = _MB({"r6": SONGS, "t6": SONGS})
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [PERF, twin]}), mb)
        self.assertIsNone(res)
        self.assertIn("two records fit as well", why)

    def test_musicbrainz_down_is_not_a_verdict(self):
        class Down(_MB):
            def release_track_titles(self, mbid):
                return None
        res, why = ident(_Lookup({"Donny Hathaway In Performance": [PERF]}), Down({}))
        self.assertEqual((res, why), (None, "MusicBrainz could not be asked -- not judged"))

    def test_a_web_result_names_it(self):
        class Web:
            def titles(self, q):
                return ["In Performance - Album by Donny Hathaway | Spotify -- open.spotify.com"]
        lookup = _Lookup({"Donny Hathaway In Performance": [PERF]})
        res, why = ident(lookup, _MB({"r6": SONGS}), names=("Live 1980 rip",), web=Web())
        self.assertEqual(res["via"], "a web search")


class _Lidarr:
    failure_generation = 0

    def __init__(self, links=True, listed=None, held=()):
        self.links, self.added, self.removed, self.filled = links, [], [], 0
        self.mb = _MB({"r6": SONGS})
        self.web = None
        self.listed, self.refreshed = listed, 0
        self.held = None if held is None else list(held)

    def lookup_albums(self, term):
        if term != "Donny Hathaway In Performance":
            return []
        return [dict(PERF, id=self.listed)] if self.listed else [PERF]

    def list_trackfiles_for_artist(self, aid):
        if self.held is None:
            return None
        return [{"path": p, "albumId": 46993} for p in self.held]

    def add_album_unmonitored(self, resource, artist_id):
        self.added.append((resource["foreignAlbumId"], artist_id))
        return {"id": 46993}

    def get_album(self, i):
        return {"id": i, "title": "In Performance",
                "releases": [{"id": 9, "foreignReleaseId": "r6", "monitored": True}]}

    def list_tracks_for_album(self, i):
        return [{"id": k, "hasFile": k < self.filled} for k in range(6)]

    def refresh_artist(self, aid, force=False):
        self.refreshed += 1
        if self.links:
            self.filled = 6
        return 5

    def wait_for_command(self, cmd, timeout_seconds=60):
        return {}

    def set_album_monitored_release(self, a, r):
        return True

    def remove_added_album(self, i):
        self.removed.append(i)
        return True


def _orch(lid):
    o = Orchestrator.__new__(Orchestrator)
    o.lidarr = lid
    o._import_library_folder_by_tracknumber = lambda *a, **k: None
    return o


class GiveLidarrTheRecord(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name) / "Donny Hathaway" / " In Performance (1980)"
        root.mkdir(parents=True)
        self.audios = []
        for i, s in enumerate(SONGS, 1):
            p = root / ("%02d - %s.flac" % (i, s))
            p.write_bytes(b"x")
            self.audios.append(p)

    def test_the_record_is_added_unmonitored_and_its_files_owned(self):
        lid = _Lidarr()
        out = _orch(lid)._give_lidarr_the_record(113, ARTIST, self.audios,
                                                 ["In Performance"])
        self.assertEqual(lid.added, [("perf", 113)])
        self.assertEqual(out, "record: added 'In Performance' (Album/Live) to "
                              "Lidarr, unmonitored -- 6 of 6 track(s) filed "
                              "(its name)")

    def test_an_added_album_that_owns_nothing_is_taken_back(self):
        lid = _Lidarr(links=False)
        out = _orch(lid)._give_lidarr_the_record(113, ARTIST, self.audios,
                                                 ["In Performance"])
        self.assertEqual(lid.removed, [46993])
        self.assertIn("the album was removed again", out)

    def test_a_folder_lidarr_already_owns_is_given_nothing(self):
        # The Beatles / 'Abbey Road (1969)' is Lidarr's 'Abbey Road', 17 of
        # 17, while its lookup offered 'The Alternate Abbey Road'.
        held = ["/music/Music/Donny Hathaway/ In Performance (1980)/" + p.name
                for p in self.audios]
        lid = _Lidarr(held=held)
        out = _orch(lid)._give_lidarr_the_record(113, ARTIST, self.audios,
                                                 ["In Performance"])
        self.assertEqual(out, "record: Lidarr already owns this folder as "
                              "'In Performance' (6 of 6 file(s))")
        self.assertEqual((lid.added, lid.refreshed), ([], 0))
        lid = _Lidarr(held=held[:3])
        out = _orch(lid)._give_lidarr_the_record(113, ARTIST, self.audios,
                                                 ["In Performance"])
        self.assertEqual(out, "record: none given -- Lidarr owns 3 of its 6 "
                              "file(s) as 'In Performance'")
        self.assertEqual((lid.added, lid.refreshed), ([], 0))
        out = _orch(_Lidarr(held=None))._give_lidarr_the_record(
            113, ARTIST, self.audios, ["In Performance"])
        self.assertEqual(out, "record: Lidarr failed to list its files -- not judged")

    def test_a_listed_record_held_from_elsewhere_makes_this_folder_a_copy(self):
        held = ["/music/Music/Donny Hathaway/In Performance [FLAC]/" + p.name
                for p in self.audios]
        lid = _Lidarr(listed=46993, held=held)
        lid.filled = 6
        out = _orch(lid)._give_lidarr_the_record(113, ARTIST, self.audios,
                                                 ["In Performance"])
        self.assertEqual(out, "record: a copy of 'In Performance' (its name; "
                              "Lidarr holds 6 of 6)")
        self.assertEqual(lid.refreshed, 0)

    def test_the_audit_asks_for_it_and_rejudges_older_verdicts(self):
        import inspect
        src = inspect.getsource(Orchestrator)
        self.assertIn("record = self._give_lidarr_the_record(", src)
        self.assertIn('and "record: " not in str(prev[3])))', src)
        self.assertIn('not (reason == "album not in Lidarr"', src)


class AddedBody(unittest.TestCase):

    def test_added_by_hand_unmonitored_and_never_searched(self):
        from lidarr import LidarrClient
        sent = []
        c = LidarrClient.__new__(LidarrClient)
        c._post = lambda path, body: sent.append((path, body)) or {"id": 1}
        c.add_album_unmonitored(dict(PERF, id=None), 113)
        path, body = sent[0]
        self.assertEqual((path, body["artistId"], body["monitored"], body["addOptions"]),
                         ("/api/v1/album", 113, False,
                          {"addType": "manual", "searchForNewAlbum": False}))
        self.assertNotIn("id", body)


if __name__ == "__main__":
    unittest.main()
