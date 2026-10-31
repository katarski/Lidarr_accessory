"""An SACD image goes from ISO to FLAC in ONE sweep pass, and the image goes
when it is spent.

The owner, 31 Oct 2026, about Lara Fabian's '9': "why are there dsf file not
converted to flac? why does it still exists there?" and "once moved -
everything associated with the download - had to be deleted". The cueless
sweep extracted the ISO to DSF and then `continue`d: the DSD block waited for
the NEXT pass and its FLAC for yet another (a file made later in a pass is
"too new" for both age guards, and one pass took over two and a half hours).
With delete_source_folder_on_success off nothing ever deleted the 10 GB image
either."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator as O  # noqa: E402


class FakeLidarr:
    def __init__(self, album_media=(), fail=False):
        self.album_media, self.fail = list(album_media), fail

    def album_trackfile_media(self, album_id):
        if self.fail:
            raise RuntimeError("Lidarr is down")
        return list(self.album_media)


class Folder(unittest.TestCase):
    """A Lara Fabian-shaped download folder: the ISO and the torrent's four
    other files, in a temp directory."""
    TRACKS = 11

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "LARA FABIAN - 9 (2005) [SACD] (ISO)"
        self.folder.mkdir()
        self.iso = self.folder / "FABIAN LARA - 9 CD1.iso"
        self.iso.write_bytes(b"iso")
        for n in ("FABIAN LARA - 9 CD1.md5", "FABIAN LARA - 9 CD1.sfv",
                  "folder.jpeg", "sacd_log.txt"):
            (self.folder / n).write_bytes(b"x")
        p = mock.patch.object(O.handadded, "protected", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, ext, n=None, where=None, size=1):
        where = where or self.folder
        where.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(1, (n or self.TRACKS) + 1):
            f = where / ("%02d - TRACK.%s" % (i, ext))
            f.write_bytes(b"d" * size)
            out.append(f)
        return out

    def orch(self, channels=6, tracks=None, area="m", delete=True,
             file_channels=6):
        """`file_channels`: an int, or {file name: channels} (0 = unreadable)."""
        o = O.Orchestrator.__new__(O.Orchestrator)
        o._skip_seen = set()
        o.cfg = SimpleNamespace(delete_originals_on_success=delete)
        o._sacd_best_area = lambda iso: area
        o._sacd_area_info = lambda iso, a: (
            None if channels is None else
            {"channels": channels,
             "tracks": self.TRACKS if tracks is None else tracks,
             "seconds": 100.0})
        if isinstance(file_channels, dict):
            o._audio_channels = lambda p: file_channels.get(p.name, 6)
        else:
            o._audio_channels = lambda p: file_channels
        o._album_folder_identity = lambda p: ("Lara Fabian", "9")
        o._album_already_in_library = lambda who, what: {
            "id": 5, "statistics": {"trackFileCount": 11, "totalTrackCount": 11}}
        o.lidarr = FakeLidarr()
        return o


class SpentByItsFlac(Folder):
    """_sacd_iso_spent, first case: the folder's FLAC carry the whole area."""

    def spent(self, o):
        return o._sacd_iso_spent(self.folder, self.iso)

    def test_eleven_six_channel_flac_of_an_eleven_track_area(self):
        self.put("flac")
        why = self.spent(self.orch())
        self.assertTrue(why)
        self.assertIn("11 FLAC", why)
        self.assertIn("6-channel", why)

    def test_a_stereo_area_is_carried_by_stereo_flac(self):
        self.put("flac")
        self.assertTrue(self.spent(self.orch(channels=2, area="2",
                                             file_channels=2)))

    def test_fewer_flac_than_tracks_is_not_spent(self):
        self.put("flac", n=10)
        self.assertEqual(self.spent(self.orch()), "")

    def test_a_flac_with_fewer_channels_is_not_spent(self):
        self.put("flac")
        o = self.orch(file_channels={"05 - TRACK.flac": 2})
        self.assertEqual(self.spent(o), "")

    def test_an_unreadable_channel_count_is_not_spent(self):
        self.put("flac")
        o = self.orch(file_channels={"11 - TRACK.flac": 0})
        self.assertEqual(self.spent(o), "")

    def test_a_dsd_file_left_at_the_top_level_is_not_spent(self):
        self.put("flac")
        for ext in ("dsf", "dff"):
            f = self.folder / ("99 - LEFT." + ext)
            f.write_bytes(b"d")
            self.assertEqual(self.spent(self.orch()), "", ext)
            f.unlink()
        self.assertTrue(self.spent(self.orch()))

    def test_an_unreadable_disc_is_not_spent(self):
        self.put("flac")
        self.assertEqual(self.spent(self.orch(area=None)), "")      # not an SACD
        self.assertEqual(self.spent(self.orch(channels=None)), "")  # no listing
        self.assertEqual(self.spent(self.orch(tracks=0)), "")       # no tracks

    def test_a_folder_with_two_images_is_never_judged(self):
        self.put("flac")
        other = self.folder / "FABIAN LARA - 9 CD2.iso"
        other.write_bytes(b"iso2")
        self.assertEqual(self.spent(self.orch()), "")
        self.assertEqual(O.Orchestrator._sacd_iso_spent(
            self.orch(), self.folder, other), "")

    def test_a_vanished_image_is_not_spent(self):
        self.put("flac")
        self.iso.unlink()
        self.assertEqual(self.spent(self.orch()), "")

    def test_an_image_that_is_not_in_the_folder_is_never_spent(self):
        self.put("flac")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        iso = elsewhere / "x.iso"
        iso.write_bytes(b"iso")
        self.assertEqual(self.orch()._sacd_iso_spent(self.folder, iso), "")

    def test_flac_below_the_top_level_do_not_count(self):
        self.put("flac", where=self.folder / "CD1")
        self.assertEqual(self.spent(self.orch()), "")


class SpentByTheLibrary(Folder):
    """_sacd_iso_spent, second case: the album was imported before this was
    known -- no audio left, and Lidarr holds it lossless at that many channels."""

    def spent(self, media, owned=True, fail=False):
        o = self.orch()
        o.lidarr = FakeLidarr(media, fail=fail)
        if not owned:
            o._album_already_in_library = lambda who, what: None
        return o._sacd_iso_spent(self.folder, self.iso)

    FLAC6 = [(6, "FLAC")] * 11

    def test_lossless_at_as_many_channels_is_spent(self):
        why = self.spent(self.FLAC6)
        self.assertTrue(why)
        self.assertIn("Lara Fabian / 9", why)
        self.assertIn("6 channel(s)", why)

    def test_lossy_or_fewer_channels_or_unknown_is_not(self):
        self.assertEqual(self.spent([(2, "AAC-VBR")] * 11), "")
        self.assertEqual(self.spent([(2, "FLAC")] * 11), "")
        self.assertEqual(self.spent([(6, "FLAC")] * 10 + [(2, "FLAC")]), "")
        self.assertEqual(self.spent([]), "")
        self.assertEqual(self.spent([(0, "FLAC")] * 11), "")
        self.assertEqual(self.spent(self.FLAC6, fail=True), "")

    def test_an_album_the_library_does_not_hold_is_not(self):
        self.assertEqual(self.spent(self.FLAC6, owned=False), "")

    def test_any_audio_left_is_not_spent(self):
        (self.folder / "bonus.mp3").write_bytes(b"m")
        self.assertEqual(self.spent(self.FLAC6), "")

    def test_audio_in_a_subfolder_is_not_spent(self):
        self.put("flac", n=2, where=self.folder / "CD1")
        self.assertEqual(self.spent(self.FLAC6), "")

    def test_an_extraction_s_own_working_folder_is_not_audio(self):
        self.put("dsf", n=2, where=self.folder / ".sacd_extract_tmp" / "9" / "6ch")
        self.assertTrue(self.spent(self.FLAC6))

    def test_an_unreadable_disc_is_not_spent(self):
        o = self.orch(area=None)
        o.lidarr = FakeLidarr(self.FLAC6)
        self.assertEqual(o._sacd_iso_spent(self.folder, self.iso), "")


class DeleteSpent(Folder):
    def test_a_spent_image_is_deleted_and_said(self):
        self.put("flac")
        o = self.orch()
        with self.assertLogs("orchestrator", "INFO") as cm:
            self.assertTrue(o._delete_spent_sacd_iso(self.folder, [self.iso]))
        self.assertFalse(self.iso.exists())
        self.assertTrue(any(
            "SACD: deleted FABIAN LARA - 9 CD1.iso -- its 11 FLAC carry the "
            "whole 6-channel area" in m for m in cm.output), cm.output)
        # the torrent's own extras are the lifecycle's to take, not this one's
        self.assertTrue((self.folder / "sacd_log.txt").exists())
        self.assertEqual(len(list(self.folder.glob("*.flac"))), 11)

    def test_an_image_that_is_not_spent_stays(self):
        self.put("flac", n=3)
        self.assertFalse(self.orch()._delete_spent_sacd_iso(
            self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())

    def test_nothing_goes_while_originals_are_kept(self):
        self.put("flac")
        self.assertFalse(self.orch(delete=False)._delete_spent_sacd_iso(
            self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())

    def test_a_hand_added_folder_is_never_touched(self):
        self.put("flac")
        with mock.patch.object(O.handadded, "protected",
                               side_effect=lambda p: "/downloads/x"
                               if Path(str(p)) == self.folder else None):
            self.assertFalse(self.orch()._delete_spent_sacd_iso(
                self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())
        # ... nor a hand-added image on its own
        with mock.patch.object(O.handadded, "protected",
                               side_effect=lambda p: "/downloads/x"
                               if Path(str(p)) == self.iso else None):
            self.assertFalse(self.orch()._delete_spent_sacd_iso(
                self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())

    def test_a_folder_another_worker_has_or_this_process_extracts_stays(self):
        self.put("flac")
        with mock.patch.object(O.claims, "busy", return_value="/downloads/x"):
            self.assertFalse(self.orch()._delete_spent_sacd_iso(
                self.folder, [self.iso]))
        o = self.orch()
        o._sacd_busy = {str(self.folder)}
        self.assertFalse(o._delete_spent_sacd_iso(self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())

    def test_a_file_that_cannot_be_deleted_is_a_warning_not_a_crash(self):
        self.put("flac")
        real = Path.unlink

        def deny(p, *a, **k):
            if p == self.iso:
                raise PermissionError("denied")
            return real(p, *a, **k)
        with mock.patch.object(Path, "unlink", deny), \
                self.assertLogs("orchestrator", "WARNING") as cm:
            self.assertFalse(self.orch()._delete_spent_sacd_iso(
                self.folder, [self.iso]))
        self.assertTrue(any("could not delete" in m for m in cm.output))
        self.assertTrue(self.iso.exists())

    def test_a_judging_failure_is_a_warning_not_a_crash(self):
        self.put("flac")
        o = self.orch()
        o._sacd_best_area = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("orchestrator", "WARNING"):
            self.assertFalse(o._delete_spent_sacd_iso(self.folder, [self.iso]))
        self.assertTrue(self.iso.exists())

    def test_an_image_already_gone_is_nothing(self):
        self.put("flac")
        self.iso.unlink()
        self.assertFalse(self.orch()._delete_spent_sacd_iso(
            self.folder, [self.iso]))


class ExtractFolderAlreadyExtracted(Folder):
    """_extract_sacd_folder when the folder already holds what an earlier pass
    made: it still answers False (the DSD path goes on), and now takes the
    image away once its FLAC carry the disc."""

    def run_it(self, o):
        now = self.iso.stat().st_mtime + 1000
        return O.Orchestrator._extract_sacd_folder(
            o, self.folder, [self.iso], now, 10)

    def test_the_flac_of_an_earlier_pass_spend_the_image(self):
        self.put("flac")
        with self.assertLogs("orchestrator", "INFO"):
            self.assertFalse(self.run_it(self.orch()))
        self.assertFalse(self.iso.exists())

    def test_dsf_still_waiting_to_be_transcoded_keep_it(self):
        self.put("dsf")
        self.assertFalse(self.run_it(self.orch()))
        self.assertTrue(self.iso.exists())


class SweepPass(Folder):
    """The cueless sweep itself: extraction, transcode and hand-off in one pass."""

    def sweep_orch(self, extract=None, tracks=3):
        o = self.orch(tracks=tracks)
        o.cfg = SimpleNamespace(
            sweep_min_stable_seconds=10, extract_dvda_iso=False,
            extract_sacd_iso=True, extract_archives=False,
            transcode_dts_cd=False, transcode_dsd=True, prefer_multichannel=True,
            audio_extensions=[".flac", ".dsf", ".dff"],
            sweep_ledger_enabled=False, tag_identify_pre_split=False,
            delete_originals_on_success=True, flac_compression_level=5)
        o._release_llm_waiting = lambda: 0
        o._extract_sacd_folder = extract or (lambda f, i, n, m: True)
        o._drop_leftover_images = lambda *a, **k: False
        o._materialize_embedded_cues = lambda audios: []
        o._higher_channel_dsd_sibling = lambda f: None
        o._unify_multidisc_eligible = lambda el, now, ms: el
        o._drop_duplicate_editions = lambda el: []
        o._read_audio_tags = lambda p: ("Lara Fabian", "9")
        o._is_placeholder_identity = lambda a, b: False
        o.encoded = []

        def enc(args, out, label, **k):
            o.encoded.append(out.name)
            out.write_bytes(b"flac" * 25)
            return out
        o._encode_flac = enc
        o.handed = []
        o._handoff_pre_split_to_lidarr = lambda *a, **k: o.handed.append(
            (a[1], k.get("reason")))
        o._lidarr_failed_in_last_handoff = lambda: False
        return o

    def extracts_dsf(self, n=3):
        """The stand-in for sacd_extract: per-track DSF placed in the folder,
        made NOW (so after the pass began)."""
        def extract(folder, isos, now_ts, min_stable):
            for i in range(1, n + 1):
                (folder / ("%02d - TRACK.dsf" % i)).write_bytes(b"dsd" * 50)
            return True
        return extract

    def age(self, files, seconds=3600):
        t = time.time() - seconds
        for f in files:
            os.utime(f, (t, t))

    def test_one_pass_extracts_transcodes_and_hands_off(self):
        o = self.sweep_orch(self.extracts_dsf())
        with self.assertLogs("orchestrator", "INFO") as cm:
            n = o._sweep_cueless_pass(self.root)
        self.assertEqual(n, 1)
        self.assertEqual(o.handed, [(self.folder, "cueless sweep")])
        self.assertEqual(sorted(p.name for p in self.folder.glob("*.flac")),
                         ["01 - TRACK.flac", "02 - TRACK.flac", "03 - TRACK.flac"])
        self.assertEqual(list(self.folder.glob("*.dsf")), [])
        # the image is spent the moment its FLAC exist
        self.assertFalse(self.iso.exists())
        self.assertTrue(any("3 DSF just placed by the SACD extraction"
                            in m for m in cm.output), cm.output)
        self.assertTrue(any("SACD: deleted FABIAN LARA - 9 CD1.iso" in m
                            for m in cm.output))

    def test_the_spent_image_is_kept_while_originals_are_kept(self):
        o = self.sweep_orch(self.extracts_dsf())
        o.cfg.delete_originals_on_success = False
        o._sweep_cueless_pass(self.root)
        self.assertEqual(len(o.handed), 1)         # the chain does not depend on it
        self.assertTrue(self.iso.exists())

    def test_an_extraction_that_placed_nothing_ends_the_folder_for_this_pass(self):
        o = self.sweep_orch(lambda f, i, n, m: True)    # owned, settling, failed...
        self.assertEqual(o._sweep_cueless_pass(self.root), 0)
        self.assertEqual(o.handed, [])
        self.assertTrue(self.iso.exists())
        self.assertEqual(o.encoded, [])

    def test_dsf_in_a_folder_this_process_is_still_extracting_wait(self):
        inner = self.extracts_dsf()
        o = self.sweep_orch()

        def extract(folder, isos, now_ts, min_stable):
            inner(folder, isos, now_ts, min_stable)
            o._sacd_busy = {str(folder)}              # another extraction owns it
            return True
        o._extract_sacd_folder = extract
        self.assertEqual(o._sweep_cueless_pass(self.root), 0)
        self.assertEqual(o.handed, [])
        self.assertEqual(o.encoded, [])

    def test_a_failed_track_keeps_its_dsf_and_the_image_and_waits(self):
        o = self.sweep_orch(self.extracts_dsf())

        def enc(args, out, label, **k):
            if out.name.startswith("02"):
                return None                           # ffmpeg failed this one
            out.write_bytes(b"flac" * 25)
            return out
        o._encode_flac = enc
        o._sweep_cueless_pass(self.root)
        self.assertTrue((self.folder / "02 - TRACK.dsf").exists())
        self.assertTrue(self.iso.exists())            # not spent: a DSF is left
        # the DSF left is "too new" for the pre-split guard: no half album
        self.assertEqual(o.handed, [])

    def test_an_ordinary_dsd_folder_is_handed_off_in_the_same_pass_too(self):
        self.iso.unlink()
        self.age(self.put("dsf"))
        o = self.sweep_orch()
        self.assertEqual(o._sweep_cueless_pass(self.root), 1)
        self.assertEqual(len(o.encoded), self.TRACKS)
        self.assertEqual(o.handed, [(self.folder, "cueless sweep")])

    def test_dsf_still_arriving_are_still_not_touched(self):
        self.iso.unlink()
        self.put("dsf", n=3)                          # made a moment ago
        o = self.sweep_orch()
        self.assertEqual(o._sweep_cueless_pass(self.root), 0)
        self.assertEqual(o.encoded, [])
        self.assertEqual(o.handed, [])

    def test_a_flac_that_was_already_there_keeps_the_age_guard(self):
        # a torrent that ships both formats: 01.flac may still be arriving
        self.iso.unlink()
        dsf = self.put("dsf", n=2)
        self.age(dsf)
        (self.folder / "01 - TRACK.flac").write_bytes(b"flac" * 25)  # just now
        o = self.sweep_orch()
        self.assertEqual(o._sweep_cueless_pass(self.root), 0)
        self.assertEqual(o.encoded, ["02 - TRACK.flac"])   # only the missing one
        self.assertEqual(o.handed, [])

    def test_young_flac_of_an_ordinary_folder_still_wait(self):
        self.iso.unlink()
        self.put("flac", n=3)                         # made a moment ago
        o = self.sweep_orch()
        self.assertEqual(o._sweep_cueless_pass(self.root), 0)
        self.assertEqual(o.handed, [])

    def test_settled_flac_of_an_ordinary_folder_are_handed_off(self):
        self.iso.unlink()
        self.age(self.put("flac", n=3))
        o = self.sweep_orch()
        self.assertEqual(o._sweep_cueless_pass(self.root), 1)
        self.assertEqual(o.handed, [(self.folder, "cueless sweep")])


if __name__ == "__main__":
    unittest.main()
