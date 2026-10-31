"""An interrupted extraction is done again, and what it left goes first (the
owner, 31 Oct 2026: "I expect if someting is interupted, to be resumed
later"; "will the leftovers from the interupted extraction be deleted? they
should be"). Lara Fabian's '9': the 15:15 stop killed sacd_extract; after the
restart the leftover `.sacd_extract_tmp` made the folder look extracted, the
sweep transcoded its half-written track and nothing extracted it again. And
the SACD gets a progress bar like a CUE split ("make a progress bar for teh
sacd like when splitting a regular cue")."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator as O  # noqa: E402
from dedup_downloads import in_pipeline_temp  # noqa: E402

LISTING = """Area count: 2
\tArea Information [0]:
\tArea Description: 2 CHANNEL
\tTrack Count: 11
\tTotal play time: 42:15:32 [mins:secs:frames]
\tSpeaker config: 2 Channel
\tTrack list [0]:
\t\tTitle[0]: LA LETTRE
\t\tDuration: 03:54:20 [mins:secs:frames]
\tArea Information [1]:
\tArea Description: MULTI CHANNEL
\tTrack Count: 11
\tTotal play time: 42:15:32 [mins:secs:frames]
\tSpeaker config: 6 Channel
"""


def _orch():
    o = O.Orchestrator.__new__(O.Orchestrator)
    o._skip_seen = set()
    o.cfg = SimpleNamespace(prefer_multichannel=True, flac_compression_level=5)
    return o


class TempFolders(unittest.TestCase):
    def test_the_pipelines_own_folders_are_never_downloads(self):
        self.assertTrue(in_pipeline_temp(
            "/downloads/LARA FABIAN - 9/.sacd_extract_tmp/9/6ch"))
        self.assertTrue(in_pipeline_temp("/downloads/X/.dvda_extract_tmp"))
        self.assertFalse(in_pipeline_temp("/downloads/LARA FABIAN - 9"))
        self.assertFalse(in_pipeline_temp("/downloads/a.sacd_extract_tmp.b"))


class SacdListing(unittest.TestCase):
    def test_the_multichannel_area(self):
        a = O.parse_sacd_area(LISTING, "m")
        self.assertEqual((a["channels"], a["tracks"]), (6, 11))
        self.assertAlmostEqual(a["seconds"], 42 * 60 + 15 + 32 / 75.0, places=2)

    def test_the_stereo_area_and_none(self):
        self.assertEqual(O.parse_sacd_area(LISTING, "2")["channels"], 2)
        stereo_only = LISTING.split("\tArea Information [1]:")[0]
        self.assertIsNone(O.parse_sacd_area(stereo_only, "m"))
        self.assertIsNone(O.parse_sacd_area("", "m"))


class SacdProgress(unittest.TestCase):
    def test_bytes_against_the_area_size(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "9" / "6ch"
            t.mkdir(parents=True)
            (t / "01 - LA LETTRE.dsf").write_bytes(b"x" * 100)
            (t / "02 - UN AVE MARIA.dsf").write_bytes(b"x" * 50)
            pct, began, name = O.sacd_progress(Path(d), 1000.0, 11)
            self.assertEqual((pct, began, name), (15.0, 2, "02 - UN AVE MARIA"))
            self.assertEqual(O.sacd_progress(Path(d), 100.0, 11)[0], 99.0)
            self.assertEqual(O.sacd_progress(Path(d), 0.0, 4)[0], 25.0)

    def test_nothing_written_yet(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(O.sacd_progress(Path(d), 1000.0, 11), (0.0, 0, ""))
            self.assertEqual(O.sacd_progress(Path(d), 0.0, 0), (None, 0, ""))


class SacdLeftovers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "LARA FABIAN - 9 (2005) [SACD] (ISO)"
        left = self.folder / ".sacd_extract_tmp" / "9" / "6ch"
        left.mkdir(parents=True)
        (left / "01 - LA LETTRE.flac").write_bytes(b"f")
        (left / "02 - UN AVE MARIA.dsf").write_bytes(b"half")
        self.iso = self.folder / "FABIAN LARA - 9 CD1.iso"
        self.iso.write_bytes(b"iso")
        self.now = self.iso.stat().st_mtime + 1000

    def tearDown(self):
        self.tmp.cleanup()

    def test_leftovers_go_first_and_do_not_count_as_extracted(self):
        o = _orch()
        o._album_folder_identity = lambda p: ("", "")
        o._sacd_best_area = lambda iso: None        # stops before extracting
        did = O.Orchestrator._extract_sacd_folder(
            o, self.folder, [self.iso], self.now, 10)
        # True (handled), not False ("already extracted -> DSD path")
        self.assertTrue(did)
        self.assertFalse((self.folder / ".sacd_extract_tmp").exists())
        self.assertTrue(self.iso.exists())

    def test_an_extraction_this_process_runs_is_left_alone(self):
        o = _orch()
        o._sacd_busy = {str(self.folder)}
        self.assertTrue(O.Orchestrator._extract_sacd_folder(
            o, self.folder, [self.iso], self.now, 10))
        self.assertTrue((self.folder / ".sacd_extract_tmp").exists())


class DvdaLeftovers(unittest.TestCase):
    def test_a_temp_folder_from_before_this_process_is_not_a_running_rip(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Album"
            tmp = folder / ".dvda_extract_tmp"
            tmp.mkdir(parents=True)
            (tmp / "01.flac").write_bytes(b"f")
            iso = folder / "disc.iso"
            iso.write_bytes(b"iso")
            old = O._PROCESS_START - 100
            os.utime(tmp, (old, old))
            o = _orch()
            self.assertFalse(O.Orchestrator._dvda_rip_in_progress(
                o, folder, [iso], time.time()))
            self.assertFalse(tmp.exists())

    def test_a_rip_of_this_process_is_still_in_progress(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Album"
            tmp = folder / ".dvda_extract_tmp"
            tmp.mkdir(parents=True)
            iso = folder / "disc.iso"
            iso.write_bytes(b"iso")
            o = _orch()
            self.assertTrue(O.Orchestrator._dvda_rip_in_progress(
                o, folder, [iso], time.time()))
            self.assertTrue(tmp.exists())


class DsdProgress(unittest.TestCase):
    def test_each_track_moves_the_bar(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for n in ("01 - A.dsf", "02 - B.dsf"):
                (folder / n).write_bytes(b"d")
            o = _orch()
            bars = []
            o._activity_start = lambda f, stage, detail="", pct=None: bars.append(
                (stage, detail, pct))
            o._audio_channels = lambda p: 2

            def enc(args, out, label, **k):
                out.write_bytes(b"flac")
                return out
            o._encode_flac = enc
            made = O.Orchestrator._transcode_dsd_folder(o, folder)
            self.assertEqual(len(made), 2)
            self.assertEqual(bars, [("DSD→FLAC", "track 1/2 — 01 - A", 0.0),
                                    ("DSD→FLAC", "track 2/2 — 02 - B", 50.0)])


if __name__ == "__main__":
    unittest.main()
