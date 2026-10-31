"""Multichannel takes precedence over stereo, and what is replaced is replaced.

The owner, 31 Oct 2026, about Lara Fabian's '9': "multichannel takes
precedence over 2 Chanel and should have been replaced". Lidarr's quality
profile never counts channels: a 6-channel FLAC over the library's stereo copy
is rejected "Not an upgrade for existing track file(s)", so the rejection alone
called the download redundant (it was deleted), the pipeline never accepted it
for import, and the SACD extraction refused an image whose album was "already
complete in the library" by name -- the library held it as 11 AAC stereo files."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads as D  # noqa: E402
import orchestrator as O  # noqa: E402
from lidarr import LidarrClient  # noqa: E402

NOT_UPGRADE = {"reason": "Not an upgrade for existing track file(s)"}


class FakeLidarr:
    """What the checks ask of Lidarr; `media` is {track file id: (channels,
    quality)} and `album_media` [(channels, quality)] of the album's files."""

    def __init__(self, cands=(), media=None, album_media=None, fail=False):
        self.cands = list(cands)
        self.media = dict(media or {})
        self.album_media = list(album_media or [])
        self.fail = fail
        self.asked = []

    def windows_to_lidarr(self, p):
        return str(p)

    def lidarr_to_windows(self, p):
        return str(p)

    def manual_import_candidates(self, path, artist_id=None, force=False):
        return self.cands

    def trackfile_media(self, ids):
        self.asked.append(list(ids))
        if self.fail:
            raise RuntimeError("Lidarr is down")
        return {i: self.media[i] for i in ids if i in self.media}

    def album_trackfile_media(self, album_id):
        if self.fail:
            raise RuntimeError("Lidarr is down")
        return list(self.album_media)


def channels_of(table):
    """A stand-in for audio_open.File: {file name: channels}; unknown names
    and 0 read as a file mutagen cannot open."""
    def _file(path):
        n = table.get(Path(str(path)).name, 0)
        if not n:
            return None
        return SimpleNamespace(info=SimpleNamespace(channels=n))
    return _file


def cand(path, track_file_ids=(101,), rej=(NOT_UPGRADE,), album=None):
    return {"path": str(path),
            "tracks": [{"id": 1000 + i, "trackFileId": f}
                       for i, f in enumerate(track_file_ids)],
            "album": album or {"id": 17, "title": "9"},
            "rejections": list(rej)}


class OverLibrary(unittest.TestCase):
    """dedup_downloads.multichannel_over_library."""

    def ask(self, local_ch, media, ids=(101,), name="01.flac", fail=False):
        lid = FakeLidarr(media=media, fail=fail)
        with mock.patch("audio_open.File", channels_of({name: local_ch})):
            return D.multichannel_over_library(
                lid, Path("/downloads/A/" + name), cand("/x/" + name, ids))

    def test_six_over_two_is_multichannel(self):
        self.assertEqual(self.ask(6, {101: (2, "AAC-VBR")}), (6, 2))

    def test_equal_or_fewer_channels_is_not(self):
        self.assertIsNone(self.ask(2, {101: (2, "FLAC")}))
        self.assertIsNone(self.ask(2, {101: (6, "FLAC")}))
        self.assertIsNone(self.ask(6, {101: (6, "FLAC")}))

    def test_unknown_never_counts(self):
        self.assertIsNone(self.ask(0, {101: (2, "FLAC")}))      # file unreadable
        self.assertIsNone(self.ask(6, {101: (0, "FLAC")}))      # Lidarr: unknown
        self.assertIsNone(self.ask(6, {}))                       # no answer
        self.assertIsNone(self.ask(6, {101: (2, "FLAC")}, fail=True))

    def test_no_library_file_for_the_track(self):
        for ids in ((), (0,), (None,)):
            lid = FakeLidarr(media={101: (2, "FLAC")})
            with mock.patch("audio_open.File", channels_of({"01.flac": 6})):
                self.assertIsNone(D.multichannel_over_library(
                    lid, Path("/d/01.flac"), cand("/x/01.flac", ids)))
            self.assertEqual(lid.asked, [])       # nothing to ask Lidarr

    def test_the_library_side_is_its_best_file(self):
        media = {101: (2, "FLAC"), 102: (6, "FLAC")}
        self.assertIsNone(self.ask(6, media, ids=(101, 102)))
        self.assertEqual(self.ask(6, {101: (2, "FLAC"), 102: (2, "MP3-320")},
                                  ids=(101, 102)), (6, 2))


class FolderFullyOwned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.album = Path(self.tmp.name) / "Album"
        self.album.mkdir()
        self.files = []
        for n in ("01.flac", "02.flac"):
            (self.album / n).write_bytes(b"x")
            self.files.append(self.album / n)

    def tearDown(self):
        self.tmp.cleanup()

    def lidarr(self, library_ch=2):
        cands = [cand(f, (101 + i,)) for i, f in enumerate(self.files)]
        media = {101: (library_ch, "FLAC"), 102: (library_ch, "FLAC")}
        return FakeLidarr(cands, media)

    def owned(self, local_ch, library_ch=2):
        with mock.patch("audio_open.File", channels_of(
                {"01.flac": local_ch, "02.flac": local_ch})):
            return D.folder_fully_owned(self.lidarr(library_ch), self.album,
                                        self.files, artist_id=7)

    def test_a_six_channel_file_over_a_stereo_copy_is_not_owned(self):
        owned, why = self.owned(6)
        self.assertFalse(owned)
        self.assertIn("multichannel takes precedence", why)
        self.assertIn("6 channel(s)", why)
        self.assertIn("copy 2", why)

    def test_a_stereo_file_over_a_stereo_copy_is_still_owned(self):
        self.assertTrue(self.owned(2)[0])

    def test_equal_multichannel_is_still_owned(self):
        self.assertTrue(self.owned(6, library_ch=6)[0])

    def test_one_multichannel_song_keeps_the_whole_download(self):
        with mock.patch("audio_open.File", channels_of(
                {"01.flac": 2, "02.flac": 6})):
            owned, why = D.folder_fully_owned(
                self.lidarr(), self.album, self.files, artist_id=7)
        self.assertFalse(owned)
        self.assertIn("02.flac", why)

    def test_a_file_lidarr_would_import_is_still_the_old_verdict(self):
        c = [cand(self.files[0], (101,), rej=()),
             cand(self.files[1], (102,))]
        lid = FakeLidarr(c, {101: (2, "MP3-320"), 102: (2, "FLAC")})
        with mock.patch("audio_open.File", channels_of({})):
            owned, why = D.folder_fully_owned(lid, self.album, self.files, 7)
        self.assertFalse(owned)
        self.assertIn("would be imported", why)


class FilterAcceptable(unittest.TestCase):
    """Orchestrator._filter_acceptable: a not-an-upgrade verdict is overridable
    for a file with more channels than the library's copy, and for nothing else."""

    def orch(self, lidarr=None, floor=50):
        o = O.Orchestrator.__new__(O.Orchestrator)
        o.cfg = SimpleNamespace(min_match_percent=floor)
        if lidarr is not None:
            o.lidarr = lidarr
        return o

    def run_filter(self, local_ch, c=None, lidarr="default", album="9"):
        lid = FakeLidarr(media={101: (2, "AAC-VBR")}) if lidarr == "default" \
            else lidarr
        o = self.orch(lid)
        with mock.patch("audio_open.File", channels_of({"01.flac": local_ch})):
            return o._filter_acceptable(
                [c or cand("/downloads/9/01.flac")], album_name=album,
                artist_name="Lara Fabian")

    def test_a_multichannel_not_an_upgrade_is_accepted(self):
        with self.assertLogs("orchestrator", "INFO") as cm:
            got = self.run_filter(6)
        self.assertEqual(len(got), 1)
        self.assertTrue(any("[MULTICHANNEL] 01.flac -- 6 ch over the library's "
                            "2 ch: replacing" in m for m in cm.output))
        # no "match %" figure to report: the 80 % line is not logged for it
        self.assertFalse(any("[OVERRIDE]" in m for m in cm.output))

    def test_a_stereo_not_an_upgrade_is_still_dropped(self):
        self.assertEqual(self.run_filter(2), [])

    def test_without_lidarr_or_with_it_down_nothing_changes(self):
        o = self.orch(None)                     # no .lidarr at all
        with mock.patch("audio_open.File", channels_of({"01.flac": 6})):
            self.assertEqual(o._filter_acceptable(
                [cand("/downloads/9/01.flac")], album_name="9"), [])
        down = FakeLidarr(media={101: (2, "FLAC")}, fail=True)
        self.assertEqual(self.run_filter(6, lidarr=down), [])

    def test_the_other_checks_of_the_loop_still_hold(self):
        # another album than the download's: Lidarr's rule stands
        other = cand("/downloads/9/01.flac", album={"id": 3, "title": "Blue"})
        self.assertEqual(self.run_filter(6, c=other), [])
        # a rejection that is nothing to override beside it
        bad = cand("/downloads/9/01.flac",
                   rej=(NOT_UPGRADE, {"reason": "Unknown Artist"}))
        self.assertEqual(self.run_filter(6, c=bad), [])
        # the match-% floor beside it still judges its own rejection
        low = cand("/downloads/9/01.flac", rej=(
            NOT_UPGRADE, {"reason": "Album match is not close enough: "
                                    "30.0 % vs 80 % [album]"}))
        self.assertEqual(self.run_filter(6, c=low), [])
        ok = cand("/downloads/9/01.flac", rej=(
            NOT_UPGRADE, {"reason": "Album match is not close enough: "
                                    "70.0 % vs 80 % [album]"}))
        self.assertEqual(len(self.run_filter(6, c=ok)), 1)

    def test_a_candidate_without_a_library_file_is_not_forced(self):
        c = cand("/downloads/9/01.flac", track_file_ids=(0,))
        self.assertEqual(self.run_filter(6, c=c), [])

    def test_the_path_is_mapped_to_where_we_see_the_file(self):
        seen = []

        def read(path):
            seen.append(Path(str(path)).as_posix())
            return SimpleNamespace(info=SimpleNamespace(channels=6))

        lid = FakeLidarr(media={101: (2, "FLAC")})
        lid.lidarr_to_windows = lambda p: p.replace("/lidarr/dl", "/downloads")
        o = self.orch(lid)
        with mock.patch("audio_open.File", read):
            got = o._filter_acceptable(
                [cand("/lidarr/dl/9/01.flac")], album_name="9")
        self.assertEqual(len(got), 1)
        self.assertEqual(seen, ["/downloads/9/01.flac"])


class MediaAnswers(unittest.TestCase):
    """The two new LidarrClient reads: their shape and their failure contract."""

    class _Client(LidarrClient):
        def __init__(self, rows=None, fail=False):          # no HTTP session
            self.rows, self.fail, self.calls = rows, fail, []

        def _get(self, path, **params):
            self.calls.append((path, params))
            if self.fail:
                raise RuntimeError("down")
            return self.rows

    ROWS = [{"id": 5, "quality": {"quality": {"id": 1, "name": "AAC-VBR"}},
             "mediaInfo": {"audioChannels": 2}},
            {"id": 6, "quality": {"quality": {"name": "FLAC"}},
             "mediaInfo": {"audioChannels": 6}},
            {"id": 7, "quality": None, "mediaInfo": None},
            {"id": None, "mediaInfo": {"audioChannels": 6}}]

    def test_trackfile_media(self):
        c = self._Client(self.ROWS)
        self.assertEqual(c.trackfile_media([5, 6, 7, 5, 0, "x"]),
                         {5: (2, "AAC-VBR"), 6: (6, "FLAC"), 7: (0, "")})
        self.assertEqual(c.calls[0][1]["trackFileIds"], [5, 6, 7])

    def test_trackfile_media_failure_and_nothing_to_ask(self):
        self.assertEqual(self._Client(fail=True).trackfile_media([1]), {})
        c = self._Client(self.ROWS)
        self.assertEqual(c.trackfile_media([]), {})
        self.assertEqual(c.calls, [])

    def test_trackfile_media_batches_of_100(self):
        c = self._Client([])
        c.trackfile_media(list(range(1, 251)))
        self.assertEqual([len(p["trackFileIds"]) for _x, p in c.calls],
                         [100, 100, 50])

    def test_album_trackfile_media(self):
        c = self._Client(self.ROWS)
        self.assertEqual(c.album_trackfile_media(9),
                         [(2, "AAC-VBR"), (6, "FLAC"), (0, ""), (6, "")])
        self.assertEqual(c.calls[0], ("/api/v1/trackfile", {"albumId": 9}))
        self.assertEqual(self._Client(fail=True).album_trackfile_media(9), [])


class SacdOwnedBranch(unittest.TestCase):
    """_extract_sacd_folder when the album is already in the library by name:
    Lara Fabian's '9' was never extracted because of that, while the library
    held it as 11 AAC files of 2 channels."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "LARA FABIAN - 9 (2005) [SACD] (ISO)"
        self.folder.mkdir()
        self.iso = self.folder / "FABIAN LARA - 9 CD1.iso"
        self.iso.write_bytes(b"iso")
        self.now = self.iso.stat().st_mtime + 1000
        self.marked = []
        p = mock.patch.object(O.handadded, "protected", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def orch(self, album_media, disc_channels=6, delete=True, area="m"):
        o = O.Orchestrator.__new__(O.Orchestrator)
        o._skip_seen = set()
        o.cfg = SimpleNamespace(delete_originals_on_success=delete)
        o.lidarr = FakeLidarr(album_media=album_media)
        o._album_folder_identity = lambda p: ("Lara Fabian", "9")
        o._album_already_in_library = lambda who, what: {
            "id": 5, "statistics": {"trackFileCount": 11, "totalTrackCount": 11}}
        o._sacd_best_area = lambda iso: area
        o._sacd_area_info = lambda iso, a: {
            "channels": disc_channels, "tracks": 11, "seconds": 2535.0}
        o._sweep_ledger_mark = lambda f, isos, now: self.marked.append(f)
        o._sweep_ledger_save = lambda: None
        return o

    def run_extract(self, o):
        with mock.patch.object(
                O.subprocess, "Popen", side_effect=OSError("no sacd_extract")) as popen:
            did = O.Orchestrator._extract_sacd_folder(
                o, self.folder, [self.iso], self.now, 10)
        return did, popen.called

    AAC = [(2, "AAC-VBR")] * 11
    FLAC2 = [(2, "FLAC")] * 11
    FLAC6 = [(6, "FLAC")] * 11

    def test_a_stereo_library_gives_way_to_the_multichannel_disc(self):
        for lib in (self.AAC, self.FLAC2):
            o = self.orch(lib)
            with self.assertLogs("orchestrator", "INFO") as cm:
                did, extracting = self.run_extract(o)
            self.assertTrue(extracting, lib[0])
            self.assertTrue(self.iso.exists())
            self.assertTrue(any(
                "complete in the library at 2 channel(s)" in m
                and "the disc has 6 -- extracting: multichannel takes precedence"
                in m for m in cm.output), cm.output)
            self.assertEqual(self.marked, [])      # not written off as owned

    def test_a_lossy_library_gives_way_to_a_lossless_disc_at_equal_channels(self):
        o = self.orch(self.AAC, disc_channels=2, area="2")
        with self.assertLogs("orchestrator", "INFO") as cm:
            did, extracting = self.run_extract(o)
        self.assertTrue(extracting)
        self.assertTrue(any("lossless takes precedence" in m for m in cm.output))

    def test_a_lossless_library_with_as_many_channels_keeps_the_old_answer(self):
        o = self.orch(self.FLAC6)
        with self.assertLogs("orchestrator", "INFO") as cm:
            did, extracting = self.run_extract(o)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(any("already complete in the library (11/11) -- not "
                            "extracting." in m for m in cm.output))
        self.assertEqual(self.marked, [self.folder])
        # ... and the image is spent: the library holds all of it
        self.assertFalse(self.iso.exists())
        self.assertTrue(any("SACD: deleted FABIAN LARA - 9 CD1.iso" in m
                            for m in cm.output))

    def test_a_spent_image_stays_when_originals_are_kept(self):
        o = self.orch(self.FLAC6, delete=False)
        did, extracting = self.run_extract(o)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(self.iso.exists())

    def test_no_answer_from_lidarr_is_today_s_behaviour_exactly(self):
        for media in ([], None):
            o = self.orch(media or [])
            probes = []
            o._sacd_best_area = lambda iso: probes.append(iso) or "m"
            did, extracting = self.run_extract(o)
            self.assertTrue(did)
            self.assertFalse(extracting)
            self.assertTrue(self.iso.exists())
            self.assertEqual(probes, [])           # not even probed
        down = self.orch([])
        down.lidarr = FakeLidarr(fail=True)
        did, extracting = self.run_extract(down)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(self.iso.exists())

    def test_a_failure_while_comparing_is_no_verdict(self):
        o = self.orch(self.AAC)
        o._sacd_area_info = mock.Mock(side_effect=RuntimeError("boom"))
        did, extracting = self.run_extract(o)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(self.iso.exists())

    def test_an_album_of_mixed_files_settles_nothing(self):
        o = self.orch([(6, "FLAC")] * 10 + [(2, "FLAC")])
        did, extracting = self.run_extract(o)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(self.iso.exists())         # neither extracted nor deleted

    def test_an_image_that_is_not_a_readable_sacd_is_left_alone(self):
        o = self.orch(self.AAC, area=None)
        did, extracting = self.run_extract(o)
        self.assertTrue(did)
        self.assertFalse(extracting)
        self.assertTrue(self.iso.exists())


class QualityVerdict(unittest.TestCase):
    """Orchestrator._disc_vs_library on its own."""

    def v(self, media, ch=6):
        return O.Orchestrator._disc_vs_library(media, ch)[0]

    def test_lossy_names_and_the_lossless_list(self):
        for name in ("AAC-VBR", "MP3-320", "MP3-VBR-V0", "WMA", "Unknown",
                     "OGG Vorbis Q5"):
            self.assertEqual(self.v([(6, name)]), "better", name)
        for name in ("FLAC", "FLAC 24bit", "ALAC", "alac", "APE", "WavPack",
                     "WAV", "AIFF", "DSD64"):
            self.assertEqual(self.v([(6, name)]), "enough", name)

    def test_channels(self):
        self.assertEqual(self.v([(2, "FLAC")] * 11), "better")
        self.assertEqual(self.v([(6, "FLAC")] * 11), "enough")
        self.assertEqual(self.v([(8, "FLAC")] * 11), "enough")
        self.assertEqual(self.v([(2, "FLAC")], ch=2), "enough")

    def test_unknown_settles_nothing(self):
        self.assertEqual(self.v([]), "")
        self.assertEqual(self.v(None), "")
        self.assertEqual(self.v([(0, "FLAC")] * 3), "")          # channels unknown
        self.assertEqual(self.v([(6, "")] * 3), "")              # quality unknown
        self.assertEqual(self.v([(6, "FLAC")], ch=0), "")        # disc unknown
        self.assertEqual(self.v([(6, "FLAC")] * 10 + [(0, "FLAC")]), "")
        # lossy wins whatever else is unknown
        self.assertEqual(self.v([(0, "MP3-320")]), "better")


if __name__ == "__main__":
    unittest.main()
