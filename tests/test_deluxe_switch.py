"""An album held complete whose folder also holds a bigger edition's files
is switched to that edition and the files are filed.

`Radiohead / Pablo Honey`: the 12-track release filed at the top of the
folder, the 34-track deluxe in `CD 01` + `CD 02`, untracked since the album
was moved to the standard release (1,364 such files in 163 folders)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

STD, DLX = 1, 2


class _Lidarr:
    def __init__(self, root, dlx_titles=("Alpha", "Beta", "Gamma", "Delta"),
                 lands=True):
        self.root, self.dlx_titles, self.lands = root, dlx_titles, lands
        self.failure_generation = 0
        self.monitored = STD
        self.switches = []
        self.filed = 2
        self.stale_stats = False  # GET /album/{id} still describing the old release
        self.lag = 0          # reads after a switch that still show the old release
        self.pending = 0

    def get_album(self, i):
        return {"id": i, "title": "Album",
                "statistics": {"trackFileCount": 0 if self.stale_stats else self.filed,
                               "totalTrackCount": 2 if self.monitored == STD else 4},
                "releases": [{"id": STD, "trackCount": 2,
                              "monitored": self.monitored == STD},
                             {"id": DLX, "trackCount": 4,
                              "monitored": self.monitored == DLX}]}

    def list_trackfiles_for_album(self, i):
        return [{"path": str(self.root / n)} for n in
                ("Artist - Album - 01 - Alpha.flac", "Artist - Album - 02 - Beta.flac")]

    def lidarr_to_windows(self, p):
        return p

    def list_tracks_for_release(self, i, rid):
        return [{"id": k, "title": t} for k, t in enumerate(self.dlx_titles, 10)]

    def set_album_monitored_release(self, i, rid):
        self.switches.append(rid)
        self.old, self.monitored = self.monitored, rid
        self.pending = self.lag
        return True

    def list_tracks_for_album(self, i):
        rel = self.monitored
        if self.pending > 0:
            self.pending -= 1
            rel = self.old
        n = 2 if rel == STD else 4
        return [{"id": rel * 100 + k, "hasFile": k < self.filed} for k in range(n)]

    def wait_for_command(self, cmd, timeout_seconds=60):
        return {}


def _setup(dlx_titles=None, lands=True):
    d = tempfile.TemporaryDirectory()
    root = Path(d.name) / "Album (1993)"
    for sub, names in (("", ("01 - Alpha", "02 - Beta")),
                       ("CD 01", ("01 - Alpha", "02 - Beta")),
                       ("CD 02", ("01 - Gamma", "02 - Delta"))):
        (root / sub).mkdir(parents=True, exist_ok=True)
        for n in names:
            (root / sub / ("Artist - Album - %s.flac" % n)).write_bytes(b"x")
    lid = _Lidarr(root, **({"dlx_titles": dlx_titles} if dlx_titles else {}))
    o = Orchestrator.__new__(Orchestrator)
    o.lidarr = lid
    o._SETTLE_POLL = 0
    imported = []

    def imp(rec, aid, files):
        imported.append(sorted(p.name for p in files))
        lid.settled_at_import = lid.pending == 0
        if lands and lid.monitored == DLX:
            lid.filed = 4
        elif lid.monitored == STD:
            lid.filed = 2
        else:
            lid.filed = 0
        return 77
    o._import_library_folder_by_tracknumber = imp
    return d, o, root, lid, imported


class DeluxeSwitch(unittest.TestCase):

    def test_the_bigger_edition_is_switched_to_and_filed(self):
        d, o, root, lid, imported = _setup()
        self.addCleanup(d.cleanup)
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertEqual(lid.switches, [DLX])
        self.assertEqual(len(imported[0]), 4)          # CD 01 + CD 02
        self.assertIn("switched to the 4-track release 2", out)

    def test_titles_that_do_not_fit_switch_nothing(self):
        d, o, root, lid, imported = _setup(dlx_titles=("W", "X", "Y", "Z"))
        self.addCleanup(d.cleanup)
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertEqual((lid.switches, imported), ([], []))
        self.assertIn("no bigger release fits", out)

    def test_an_edition_that_loses_files_is_undone(self):
        d, o, root, lid, imported = _setup(lands=False)
        self.addCleanup(d.cleanup)
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertEqual(lid.switches, [DLX, STD])
        self.assertEqual(lid.filed, 2)
        self.assertIn("restored release 1", out)
        # The restore re-files what Lidarr held, not the folder's other files.
        self.assertEqual(imported[1], ["Artist - Album - 01 - Alpha.flac",
                                       "Artist - Album - 02 - Beta.flac"])

    def test_what_the_album_holds_is_counted_from_its_files(self):
        # After a switch Lidarr's GET /album/{id} kept the old release's
        # statistics (Allred / Covers: 0 of 10 while 12 of 12 were filed);
        # measured against 0, a switch that lost files was kept.
        d, o, root, lid, imported = _setup(lands=False)
        self.addCleanup(d.cleanup)
        lid.stale_stats = True
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertEqual(lid.switches, [DLX, STD])
        self.assertIn("held 0 of 2 before -- restored release 1", out)

    def test_a_second_copy_of_the_album_is_not_switched(self):
        # Aaliyah: '(2003) - Aaliyah - Age Ain't Nothing But A Number' beside
        # Lidarr's own folder -- none of Lidarr's files are in it.
        d, o, root, lid, imported = _setup()
        self.addCleanup(d.cleanup)
        copy = root.parent / "(2003) - Album"
        copy.mkdir()
        for n in ("01 - Alpha", "02 - Beta", "03 - Gamma", "04 - Delta"):
            (copy / ("Artist - Album - %s.flac" % n)).write_bytes(b"x")
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, copy)
        self.assertEqual((lid.switches, imported), ([], []))
        self.assertIn("second copy", out)

    def test_another_record_in_the_folder_is_not_an_edition(self):
        # Allred / Covers: MusicBrainz groups `Covers, Volume II` with it, the
        # folder held both, and Volume II's untracked files were switched to.
        d, o, root, lid, imported = _setup(dlx_titles=("W", "X", "Y", "Z"))
        self.addCleanup(d.cleanup)
        for sub in ("CD 01", "CD 02"):
            for p in (root / sub).iterdir():
                p.unlink()
            (root / sub).rmdir()
        for n in ("01 - W", "02 - X", "03 - Y", "04 - Z"):
            (root / ("Artist - Album - %s.flac" % n)).write_bytes(b"x")
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertEqual((lid.switches, imported), ([], []))
        self.assertIn("another record, not switched", out)

    def test_only_a_switch_spends_the_pass_budget(self):
        # 303 albums had untracked files; looking at one that has no edition
        # spent the 10-per-pass budget all the same.
        d, o, root, lid, imported = _setup(dlx_titles=("W", "X", "Y", "Z"))
        self.addCleanup(d.cleanup)
        budget = [1]
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root,
                                           budget=budget)
        self.assertIn("no bigger release fits", out)
        self.assertEqual(budget, [1])
        lid.dlx_titles = ("Alpha", "Beta", "Gamma", "Delta")
        o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root,
                                     budget=budget)
        self.assertEqual((budget, lid.switches), ([0], [DLX]))
        lid.monitored, lid.filed = STD, 2
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root,
                                           budget=budget)
        self.assertEqual((out, lid.switches), ("deferred (per-pass limit)", [DLX]))
        # ... and the audit hands it the budget instead of spending it first.
        import inspect
        src = inspect.getsource(Orchestrator)
        self.assertIn("album_rec, aid, album_dir, budget=deluxe_budget)", src)
        self.assertNotIn("deluxe_budget[0] -= 1", src)

    def test_nothing_is_filed_or_counted_before_lidarr_takes_the_switch(self):
        d, o, root, lid, imported = _setup()
        self.addCleanup(d.cleanup)
        lid.lag = 3
        out = o._switch_to_fitting_release({"id": 5, "title": "Album"}, 3, root)
        self.assertTrue(lid.settled_at_import)
        self.assertEqual(lid.switches, [DLX])
        self.assertIn("switched to the 4-track release 2", out)


if __name__ == "__main__":
    unittest.main()
