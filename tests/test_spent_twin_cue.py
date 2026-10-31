"""A twin sheet an earlier split left behind is deleted, not failed.

'2 Unlimited 1998 - II (Enhanced)' carried two sheets for one APE image:
'... ape.cue' and '... wav.cue'. The APE was split and deleted with its
sheet at 14:41 on 31 Oct, minutes before the twins started going with it
(b6091cf), so '... wav.cue' failed as "No companion audio" on every try
until it gave up into the needs-attention list. qBittorrent still lists
the image as downloaded whole: that is the proof the twin is spent."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

TOP = "2 Unlimited 1998 - II (Enhanced)"
IMG = "2 Unlimited - II (Enhanced)"


class FakeQbt:
    def __init__(self, torrents, files):
        self._t, self._f = torrents, files
        self.files_asked = []

    def torrents(self, category="", state_filter=""):
        return list(self._t)

    def files(self, h):
        self.files_asked.append(h)
        return list(self._f.get(h, []))


def _torrent(cat="lidarr", name=TOP, h="h1"):
    return {"hash": h, "name": name, "category": cat, "save_path": "/data",
            "content_path": "/data/" + name}


def _files(progress=1, folder=TOP, ext=".ape"):
    return {"h1": [
        {"name": "%s/%s%s" % (folder, IMG, ext), "progress": progress},
        {"name": "%s/%s ape.cue" % (TOP, IMG), "progress": 1},
        {"name": "%s/%s wav.cue" % (TOP, IMG), "progress": 1},
        {"name": "%s/%s.log" % (TOP, IMG), "progress": 1},
    ]}


class SpentTwinCue(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "downloads"
        self.dir = self.root / TOP
        self.dir.mkdir(parents=True)
        self.cue = self.dir / ("%s wav.cue" % IMG)
        self.cue.write_text('PERFORMER "2 Unlimited"\nTITLE "II"\n'
                            'FILE "%s.wav" WAVE\n  TRACK 01 AUDIO\n' % IMG)
        # Same stem as the image, but not audio: it must not count as it.
        (self.dir / ("%s.log" % IMG)).write_text("EAC log")
        self.qbt = FakeQbt([_torrent()], _files())

    def tearDown(self):
        self.tmp.cleanup()

    def _orch(self, qbt="default"):
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(watch_root=self.root, qbt_category="lidarr",
                                qbt_url="http://qbt", cue_ledger_file=None,
                                audio_extensions=[".flac", ".ape", ".wv", ".wav"])
        q = self.qbt if qbt == "default" else qbt
        o._get_qbt = lambda: q
        o._skip_seen = set()
        o._cue_led = {str(self.cue): ["1452:1790867426", 1.0, "failed", 2],
                      "/downloads/other.cue": ["1:1", 1.0, "failed", 1]}
        return o

    def test_spent_twin_is_named_and_dropped(self):
        o = self._orch()
        why = o._spent_twin_cue(self.cue)
        self.assertIn("%s.ape" % IMG, why)
        self.assertIn(TOP, why)
        self.assertTrue(o._drop_spent_twin_cue(self.cue, why))
        self.assertFalse(self.cue.exists())
        self.assertNotIn(str(self.cue), o._cue_led)
        self.assertIn("/downloads/other.cue", o._cue_led)
        self.assertIn(self.cue, o._skip_seen)
        self.assertTrue((self.dir / ("%s.log" % IMG)).exists())

    def test_image_still_downloading_keeps_it(self):
        self.qbt = FakeQbt([_torrent()], _files(progress=0.42))
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_image_on_disk_keeps_it_without_asking_qbittorrent(self):
        (self.dir / ("%s.ape" % IMG)).write_bytes(b"x")
        o = self._orch()
        self.assertEqual(o._spent_twin_cue(self.cue), "")
        self.assertEqual(self.qbt.files_asked, [])

    def test_someone_elses_torrent_keeps_it(self):
        self.qbt = FakeQbt([_torrent(cat="radarr")], _files())
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_uncategorised_torrent_keeps_it(self):
        self.qbt = FakeQbt([_torrent(cat="")], _files())
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_no_torrent_holds_the_folder_keeps_it(self):
        self.qbt = FakeQbt([_torrent(name="Someone Else - Album")], _files())
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_no_qbittorrent_keeps_it(self):
        self.assertEqual(self._orch(qbt=None)._spent_twin_cue(self.cue), "")

    def test_image_in_another_folder_of_the_torrent_keeps_it(self):
        self.qbt = FakeQbt([_torrent()], _files(folder=TOP + "/CD2"))
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_image_never_in_the_torrent_keeps_it(self):
        self.qbt = FakeQbt([_torrent()], {"h1": [
            {"name": "%s/Something Else.ape" % TOP, "progress": 1}]})
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_every_image_must_be_spent(self):
        self.cue.write_text('FILE "%s.wav" WAVE\nFILE "Bonus.wav" WAVE\n' % IMG)
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_sheet_without_file_line_keeps_it(self):
        self.cue.write_text('PERFORMER "2 Unlimited"\nTITLE "II"\n')
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_qbittorrent_failure_keeps_it(self):
        class Broken(FakeQbt):
            def torrents(self, category="", state_filter=""):
                raise OSError("connection refused")
        self.qbt = Broken([], {})
        self.assertEqual(self._orch()._spent_twin_cue(self.cue), "")

    def test_gave_up_twin_is_deleted_on_its_next_look(self):
        o = self._orch()
        o._cue_ledger_verdict = lambda p: (
            "gave up after 4 attempt(s) (failed at 2026-10-31T16:28:54+00:00); "
            "it is in the WebUI needs-attention list")
        self.assertIsNone(o._process(self.cue))
        self.assertFalse(self.cue.exists())
        self.assertNotIn(str(self.cue), o._cue_led)

    def test_gave_up_sheet_with_its_image_is_only_skipped(self):
        (self.dir / ("%s.wav" % IMG)).write_bytes(b"RIFF")
        o = self._orch()
        o._cue_ledger_verdict = lambda p: "gave up after 4 attempt(s) (failed)"
        self.assertIsNone(o._process(self.cue))
        self.assertTrue(self.cue.exists())
        self.assertIn(self.cue, o._skip_seen)
        self.assertEqual(self.qbt.files_asked, [])

    def test_no_companion_branch_deletes_a_spent_twin_instead_of_failing(self):
        o = self._orch()
        o._cue_ledger_verdict = lambda p: None
        o.cfg.wait_for_lidarr = False
        o._wait_for_stability = lambda p: True
        o._looks_pre_split = lambda d: False
        o._is_sidecar_dir = lambda d: False
        recorded = []
        o._record = lambda *a, **k: recorded.append((a, k))
        self.assertIsNone(o._process(self.cue))
        self.assertFalse(self.cue.exists())
        self.assertEqual(recorded, [])

    def test_no_companion_branch_still_fails_a_sheet_that_is_not_spent(self):
        self.qbt = FakeQbt([_torrent()], _files(progress=0.5))
        o = self._orch()
        o._cue_ledger_verdict = lambda p: None
        o.cfg.wait_for_lidarr = False
        o._wait_for_stability = lambda p: True
        o._looks_pre_split = lambda d: False
        o._is_sidecar_dir = lambda d: False
        recorded = []
        o._record = lambda *a, **k: recorded.append((a, k))
        self.assertIsNone(o._process(self.cue))
        self.assertTrue(self.cue.exists())
        self.assertEqual(recorded[0][1].get("outcome"), "failed")


if __name__ == "__main__":
    unittest.main()
