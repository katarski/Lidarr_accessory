"""No pictures: the owner, 31 Oct 2026, "lidarr keeps images to be downlaoded.
At one pont i requested no pictures and I am still seeing them". The deselect
pass drops a lidarr torrent's pictures (covers, scans, artwork) that are still
to download, like its video, whatever the album plan says; finished pictures
and ones already deselected are left alone, so a re-check stays quiet."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbt_deselect as QD  # noqa: E402


def _f(index, name, priority=1, progress=0.0):
    return {"index": index, "name": name, "priority": priority,
            "progress": progress, "size": 1000}


class _Qbt:
    def __init__(self, files, torrents=()):
        # Own copies: set_file_priority changes them, and ALBUM is shared.
        self._files = [dict(f) for f in files]
        self._t = list(torrents)
        self.prio_calls = []
        self.removed = []
        self.paused = []

    def torrents(self, category=""):
        return [t for t in self._t
                if not category or t.get("category") == category]

    def files(self, h):
        return [dict(f) for f in self._files]

    def set_file_priority(self, h, indices, priority):
        self.prio_calls.append((h, list(indices), priority))
        for f in self._files:
            if f["index"] in indices:
                f["priority"] = priority
        return True

    def remove(self, h, delete_files=True):
        self.removed.append((h, delete_files))
        return True

    def pause(self, h):
        self.paused.append(h)

    def ensure_resumed(self, h):
        return True

    def ensure_started(self, h):
        return True


def _entry(path, have=False, owned=False, total=10, index=0):
    files = [{"name": path, "index": index, "size": 1000}]
    return {"artist": "A", "album": "B", "files": files, "all_files": files,
            "size": 1000, "have": have, "owned": owned, "have_count": 0,
            "total": total}


ALBUM = [
    _f(0, "Album/01 - One.flac"),
    _f(1, "Album/02 - Two.flac"),
    _f(2, "Album/Album.cue"),
    _f(3, "Album/Album.log"),
    _f(4, "Album/folder.jpg"),
    _f(5, "Album/Scans/Front.JPG"),
    _f(6, "Album/Scans/Back.jpeg"),
    _f(7, "Album/Artwork/cd.png"),
    _f(8, "Album/booklet.pdf"),
    _f(9, "Album/Scans/inlay.TIFF"),
    _f(10, "Album/cover.webp"),
]
PICTURES = [4, 5, 6, 7, 9, 10]


class PictureIndices(unittest.TestCase):
    def test_pictures_by_extension_any_case_any_folder(self):
        self.assertEqual(QD._picture_file_indices(ALBUM), PICTURES)

    def test_audio_sheets_logs_pdf_disc_and_video_are_not_pictures(self):
        files = [_f(0, "a.flac"), _f(1, "a.cue"), _f(2, "a.log"),
                 _f(3, "b.pdf"), _f(4, "x.iso"), _f(5, "v.mkv"),
                 _f(6, "n.nfo"), _f(7, "jpg"), _f(8, "folder.jpg.txt")]
        self.assertEqual(QD._picture_file_indices(files), [])

    def test_finished_or_deselected_pictures_are_left_alone(self):
        files = [_f(0, "a.jpg", progress=1.0), _f(1, "b.jpg", priority=0),
                 _f(2, "c.jpg", progress=0.5), _f(3, "d.png", progress=0.0)]
        self.assertEqual(QD._picture_file_indices(files), [2, 3])

    def test_a_file_without_index_or_with_bad_numbers_is_skipped(self):
        files = [{"name": "a.jpg"},
                 {"index": 1, "name": "b.jpg", "priority": "x"},
                 {"index": 2, "name": "c.jpg", "progress": "y"},
                 {"index": 3, "name": "d.jpg", "progress": None},
                 {"index": "4", "name": "e.jpg"}]
        self.assertEqual(QD._picture_file_indices(files), [3, 4])


class ProcessTorrent(unittest.TestCase):
    def run_one(self, plan, files=ALBUM, **kw):
        q = _Qbt(files)
        lines = []
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: plan):
            res = QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                                     apply=True, emit=lines.append, **kw)
        return q, lines, res

    def test_a_kept_album_downloads_its_music_but_not_its_pictures(self):
        q, lines, res = self.run_one([_entry("Album/01 - One.flac")])
        self.assertEqual(q.prio_calls, [("h", PICTURES, 0)])
        self.assertEqual(res, (0, 1))
        self.assertTrue(any("PICTURES -> deselect 6 picture file(s)" in s
                            for s in lines), lines)

    def test_no_album_plan_still_drops_the_pictures(self):
        q, lines, res = self.run_one([])
        self.assertEqual(q.prio_calls, [("h", PICTURES, 0)])
        self.assertEqual(res, (0, 0))
        self.assertIn("Torrent: T", lines)

    def test_switched_off_downloads_them(self):
        q, lines, res = self.run_one([], deselect_images=False)
        self.assertEqual(q.prio_calls, [])
        self.assertEqual(lines, [])

    def test_a_recheck_after_the_deselect_is_quiet(self):
        q = _Qbt(ALBUM)
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: []):
            QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                               apply=True, emit=lambda s: None)
            lines = []
            res = QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                                     apply=True, emit=lines.append)
        self.assertEqual(len(q.prio_calls), 1)
        self.assertEqual(lines, [])
        self.assertEqual(res, (0, 0))

    def test_an_owned_album_and_its_pictures_go_once_each(self):
        owned = _entry("Album/01 - One.flac", have=True, owned=True)
        owned["all_files"] = [{"name": f["name"], "index": f["index"]}
                              for f in ALBUM]
        q, lines, res = self.run_one([owned])
        self.assertEqual(len(q.prio_calls), 1)
        h, idx, prio = q.prio_calls[0]
        self.assertEqual(sorted(idx), list(range(len(ALBUM))))
        self.assertEqual(len(idx), len(set(idx)))
        self.assertEqual(prio, 0)
        # The album's folder already took the pictures: no PICTURES line.
        self.assertFalse(any("PICTURES" in s for s in lines), lines)

    def test_dry_run_changes_nothing(self):
        q = _Qbt(ALBUM)
        lines = []
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: []):
            QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                               apply=False, emit=lines.append)
        self.assertEqual(q.prio_calls, [])
        self.assertTrue(any("PICTURES" in s for s in lines), lines)

    def test_dropping_pictures_never_reaps_a_torrent_with_music_kept(self):
        q, lines, res = self.run_one([_entry("Album/01 - One.flac")],
                                     reap_useless=True)
        self.assertEqual(q.removed, [])

    def test_a_pictures_and_sheets_only_torrent_is_narrowed_not_removed(self):
        files = [_f(0, "Art/front.jpg"), _f(1, "Art/back.jpg"),
                 _f(2, "x.log")]
        q, lines, res = self.run_one([], files=files, reap_useless=True)
        self.assertEqual(q.prio_calls, [("h", [0, 1], 0)])
        self.assertEqual(q.removed, [])

    def test_video_and_pictures_together(self):
        files = ALBUM + [_f(11, "Album/VIDEO_TS/VTS_01_1.VOB"),
                         _f(12, "Album/clip.mkv")]
        q, lines, res = self.run_one([_entry("Album/01 - One.flac")],
                                     files=files)
        self.assertEqual(q.prio_calls, [("h", sorted(PICTURES + [11, 12]), 0)])


class AutoPass(unittest.TestCase):
    def run_pass(self, **kw):
        t = {"hash": "h", "name": "T", "category": "lidarr",
             "progress": 0.2, "state": "downloading", "added_on": 1}
        q = _Qbt(ALBUM, torrents=[t])
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: []):
            QD.auto_deselect_pass(q, None, set(), category="lidarr",
                                  emit=lambda s: None, **kw)
        return q

    def test_the_pass_drops_pictures_by_default(self):
        q = self.run_pass()
        self.assertEqual(q.prio_calls, [("h", PICTURES, 0)])

    def test_the_pass_passes_the_switch_on(self):
        q = self.run_pass(deselect_images=False)
        self.assertEqual(q.prio_calls, [])

    def test_a_finished_torrent_is_not_touched(self):
        t = {"hash": "h", "name": "T", "category": "lidarr",
             "progress": 1.0, "state": "uploading", "added_on": 1}
        q = _Qbt(ALBUM, torrents=[t])
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: []):
            QD.auto_deselect_pass(q, None, set(), category="lidarr",
                                  emit=lambda s: None)
        self.assertEqual(q.prio_calls, [])

    def test_another_category_is_not_touched(self):
        t = {"hash": "h", "name": "T", "category": "",
             "progress": 0.2, "state": "downloading", "added_on": 1}
        q = _Qbt(ALBUM, torrents=[t])
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: []):
            QD.auto_deselect_pass(q, None, set(), category="lidarr",
                                  emit=lambda s: None)
        self.assertEqual(q.prio_calls, [])


class Wiring(unittest.TestCase):
    def test_main_reads_the_switch_and_passes_it(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
        self.assertIn('qcfg.get("deselect_images", True)', src)
        self.assertIn("deselect_images=deselect_images", src)
        self.assertIn('"QBIT_DESELECT_IMAGES"', src)


if __name__ == "__main__":
    unittest.main()
