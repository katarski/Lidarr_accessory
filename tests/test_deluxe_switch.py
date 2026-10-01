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

    def get_album(self, i):
        return {"id": i, "title": "Album",
                "statistics": {"trackFileCount": self.filed,
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
        self.monitored = rid
        return True

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
    imported = []

    def imp(rec, aid, files):
        imported.append(sorted(p.name for p in files))
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


if __name__ == "__main__":
    unittest.main()
