"""Held downloads nothing wants are deleted (owner, 1 Nov 2026, of the held
downloads with no album in the library: "delete them") only on proof: no such
album in the library, no monitored Lidarr album still missing it, the song
harvest judged the folder as it is now and holds nothing pending there, the
download finished, not hand-added, not the harvest's keep folder. Any doubt
keeps the row for a later pass."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handadded  # noqa: E402
import song_harvest as SH  # noqa: E402
from held_store import HeldStore  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402


class _Lidarr:
    def __init__(self, artists=(), albums=None):
        self._artists = list(artists)
        self._albums = albums or {}
        self.failure_generation = 0
        self.fail_albums = False
        self.pidx = {}                     # quality id -> place in the profile
        self.tracks = {}                   # album id -> [track dicts]
        self.tfq = {}                      # track file id -> quality id
        self.preview = []                  # manual-import candidates
        self.previews = 0

    def quality_profile_index(self, pid):
        return dict(self.pidx)

    def list_tracks_for_album(self, album_id):
        return list(self.tracks.get(album_id, []))

    def trackfile_qualities(self, ids):
        return {i: self.tfq[i] for i in ids if i in self.tfq}

    def manual_import_candidates(self, folder, artist_id=None, force=False):
        self.previews += 1
        return list(self.preview)

    def windows_to_lidarr(self, p):
        return "L:" + str(p)

    def artists(self, force=False):
        return list(self._artists)

    def find_artist(self, name):
        return next((a for a in self._artists
                     if a["artistName"].lower() == name.lower()), None)

    def list_albums_for_artist(self, aid):
        if self.fail_albums:            # Lidarr's read methods answer [] on failure
            self.failure_generation += 1
            return []
        return list(self._albums.get(aid, []))


class _Qbt:
    def __init__(self, torrents=()):
        self.t = list(torrents)

    def torrents(self, category=""):
        return list(self.t)


def _album(title, have, total, monitored=True):
    return {"title": title, "monitored": monitored,
            "statistics": {"trackFileCount": have, "totalTrackCount": total}}


class _Held(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.watch = self.root / "downloads"
        self.keep = self.watch / "_harvest_pending"
        self.keep.mkdir(parents=True)
        self.ledger_file = self.root / "harvest_ledger.json"
        self.store = HeldStore(self.root / "held_items.json")
        self.lidarr = _Lidarr(
            artists=[{"id": 1, "artistName": "Moby"},
                     {"id": 2, "artistName": "Miles Davis"}],
            albums={1: [_album("Play", 18, 18)],
                    2: [_album("Kind of Blue", 0, 5)]})
        self.qbt = _Qbt()
        self.checked, self.pending = {}, {}
        self.discarded = []
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(
            harvest_enabled=True, harvest_dry_run=False,
            harvest_ledger_file=self.ledger_file,
            harvest_keep_dir=str(self.keep), watch_root=self.watch,
            staging_root=self.root / "staging",
            library_root_windows=self.root / "library")
        o.held = self.store
        o.lidarr = self.lidarr
        o._get_qbt = lambda: self.qbt

        def discard(entry):
            self.discarded.append(Path(entry["source_path"]).name)
            return (True, "Discarded (%s) and removed + blocklisted the torrent; "
                          "kept monitored so Lidarr will try again"
                    % Path(entry["source_path"]).name)
        o.discard = discard
        self.o = o

    def row(self, name, artist="Moby", album="Alice CDS", judged=True,
            existing=None, created=None):
        d = self.watch / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "01.flac").write_bytes(b"x" * 10)
        e = self.store.add(str(d), artist=artist, album=album, tracks=1,
                           reason="no monitored album target",
                           outcome="skipped_unmonitored")
        ex = existing if existing is not None else {
            "_ts": time.time(), "in_library": False, "_artist": artist,
            "_album": album}
        self.store.update(e["id"], existing=ex,
                          **({"created": created} if created else {}))
        if judged:
            self.checked[str(d)] = {"folder": SH.folder_signature(str(d)),
                                    "titles": [], "wanted": ""}
        return d

    def run_pass(self, budget=20):
        self.ledger_file.write_text(json.dumps(
            {"checked": self.checked, "pending": self.pending}))
        return self.o._auto_discard_held_unwanted(budget=budget)

    def left(self):
        return sorted(Path(i["source_path"]).name for i in self.store.list())


class AutoDiscard(_Held):
    def test_nothing_wants_it_is_discarded_and_its_row_dropped(self):
        self.row("Moby - Alice CDS")                         # album unknown to Lidarr
        self.row("Fats", artist="Fats Dominoo", album="Classics [Disc 2]")
        self.assertEqual(self.run_pass(), 2)
        self.assertEqual(sorted(self.discarded), ["Fats", "Moby - Alice CDS"])
        self.assertEqual(self.left(), [])

    def test_an_album_lidarr_monitors_and_misses_is_never_discarded(self):
        self.row("Kind", artist="Miles Davis", album="Kind of Blue (CD 1)")
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.left(), ["Kind"])

    def test_a_complete_or_unmonitored_lidarr_album_is_discarded(self):
        self.lidarr._albums[1] = [_album("Play", 18, 18),
                                  _album("Alice CDS", 0, 4, monitored=False)]
        self.row("Moby - Play", album="Play")
        self.row("Moby - Alice CDS")
        self.assertEqual(self.run_pass(), 2)

    def test_a_folder_the_harvest_has_not_judged_as_it_is_now_stays(self):
        self.row("Unjudged", judged=False)
        d = self.row("Changed")
        (d / "02.flac").write_bytes(b"y" * 20)          # grew since the harvest looked
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.left(), ["Changed", "Unjudged"])

    def test_a_pending_harvest_import_keeps_the_folder(self):
        d = self.row("Box")
        self.pending["/elsewhere"] = {"cid": 1, "mode": "move",
                                      "paths": [str(d / "01.flac")], "tracks": {}}
        self.assertEqual(self.run_pass(), 0)

    def test_the_keep_folder_hand_added_and_roots_are_never_touched(self):
        self.store.add(str(self.keep), artist="Fats Dominoo", album="X",
                       outcome="skipped_unmonitored")
        for i in self.store.list():
            self.store.update(i["id"], existing={"_ts": time.time(),
                                                 "in_library": False})
        self.checked[str(self.keep)] = {
            "folder": SH.folder_signature(str(self.keep))}
        self.row("Mine")
        with mock.patch.object(handadded, "protected",
                               lambda p: "r" if str(p).endswith("Mine") else None):
            self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.discarded, [])

    def test_a_library_copy_or_an_unanswered_summary_is_not_this_rule(self):
        self.row("InLib", existing={"_ts": time.time(), "in_library": True,
                                    "n_audio": 10})
        self.row("NoAnswer", existing={})
        self.row("Audio", existing={"_ts": time.time(), "in_library": False,
                                    "n_audio": 3})
        self.assertEqual(self.run_pass(), 0)

    def test_a_download_still_running_stays_a_finished_one_goes(self):
        self.row("Running")
        self.row("Done")
        wr = str(self.watch).replace("\\", "/")
        self.qbt.t = [
            {"content_path": "/data/torrents/Running", "progress": 0.4,
             "completion_on": -1},
            {"content_path": wr + "/Done", "progress": 0.0,     # all deselected,
             "completion_on": 1700000000}]                       # finished once
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.left(), ["Running"])

    def test_no_qbittorrent_answer_keeps_everything(self):
        self.row("A")
        self.o._get_qbt = lambda: None
        self.assertEqual(self.run_pass(), 0)

    def test_lidarr_failing_during_the_lookup_keeps_the_row(self):
        self.row("Moby - Alice CDS")
        self.lidarr.fail_albums = True
        self.assertEqual(self.run_pass(), 0)
        self.lidarr._artists = []                  # no artist list at all
        self.lidarr.fail_albums = False
        self.assertEqual(self.run_pass(), 0)

    def test_no_live_harvest_no_ledger_or_switch_off_deletes_nothing(self):
        self.row("A")
        self.o.cfg.harvest_dry_run = True
        self.assertEqual(self.run_pass(), 0)
        self.o.cfg.harvest_dry_run = False
        self.o.cfg.harvest_ledger_file = self.root / "missing.json"
        self.assertEqual(self.o._auto_discard_held_unwanted(budget=20), 0)
        self.assertEqual(self.discarded, [])

    def test_the_budget_takes_the_oldest_first(self):
        self.row("New", created=time.time())
        self.row("Old", created=time.time() - 86400 * 70)
        self.assertEqual(self.run_pass(budget=1), 1)
        self.assertEqual(self.discarded, ["Old"])

    def test_a_refused_discard_keeps_the_row(self):
        self.row("Busy")
        self.o.discard = lambda e: (False, "Busy is being processed right now")
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.left(), ["Busy"])

    def test_the_curator_runs_it_only_when_switched_on(self):
        self.row("A")
        calls = []
        self.o._auto_discard_held_unwanted = \
            lambda budget, library_budget=0: calls.append(budget) or 0
        self.o._drop_redundant_nested_held = lambda: 0
        self.o._auto_resolve_held_gaps = lambda budget=6: 0
        self.o.expand_box_set = lambda it: None
        self.o._lidarr_generation = lambda: 0
        self.o.existing_album_summary = lambda it: {}
        cfg = self.o.cfg
        cfg.held_auto_resolve, cfg.held_auto_resolve_max_per_pass = True, 6
        cfg.webui_held_refresh_seconds, cfg.held_recheck_seconds = 300, 1800
        cfg.held_auto_discard_unwanted, cfg.held_auto_discard_max_per_pass = False, 20
        self.o.curate_held_pass()
        self.assertEqual(calls, [])
        cfg.held_auto_discard_unwanted = True
        self.o.curate_held_pass()
        self.assertEqual(calls, [20])


FLAC, MP3 = 6, 2                           # Lidarr quality ids


class LibraryCopies(_Held):
    """Rows whose summary says the library has the album (owner, 1 Nov 2026:
    "if library has better - then why are these downloaded"; "if the download
    is better than the library - then it should overwrite it")."""

    def setUp(self):
        super().setUp()
        lid = self.lidarr
        lid._artists[0]["qualityProfileId"] = 1
        lid._albums[1] = [dict(_album("Play", 18, 18), id=11)]
        lid.pidx = {MP3: 5, FLAC: 10}
        lid.tracks = {11: [{"title": "Porcelain", "duration": 241000, "trackFileId": 101}]}
        lid.tfq = {101: FLAC}
        self.songs = [SH.SourceFile(path="", title="Porcelain", duration_ms=240000)]
        p = mock.patch.object(SH, "scan_folder", self._scan)
        p.start()
        self.addCleanup(p.stop)

    def _scan(self, root):
        return [SH.SourceFile(path=os.path.join(root, "01.flac"), title=s.title,
                              duration_ms=s.duration_ms) for s in self.songs]

    def copy_row(self, name, quality=FLAC, details=None):
        d = self.row(name, album="Play", existing={
            "_ts": time.time(), "in_library": True, "n_audio": 18,
            "_artist": "Moby", "_album": "Play"})
        self.lidarr.preview = [{"path": "L:" + str(d / "01.flac"),
                                "quality": {"quality": {"id": quality}}}]
        if details:
            it = next(i for i in self.store.list() if i["source_path"] == str(d))
            self.store.update(it["id"], details=details)
        return d

    def run_lib(self, library_budget=3):
        self.ledger_file.write_text(json.dumps(
            {"checked": self.checked, "pending": self.pending}))
        return self.o._auto_discard_held_unwanted(
            budget=20, library_budget=library_budget)

    def test_same_quality_as_the_library_is_deleted(self):
        self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(), 1)
        self.assertEqual(self.left(), [])

    def test_worse_than_the_library_is_deleted(self):
        self.copy_row("Moby - Play [MP3]", quality=MP3)
        self.assertEqual(self.run_lib(), 1)

    def test_a_better_file_keeps_the_row_and_is_not_asked_again_soon(self):
        self.lidarr.tfq = {101: MP3}
        self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(), 0)
        self.assertEqual(self.left(), ["Moby - Play [FLAC]"])
        n = self.lidarr.previews
        self.assertEqual(self.run_lib(), 0)
        self.assertEqual(self.lidarr.previews, n)          # 6 h memo

    def test_a_live_take_is_not_the_studio_song(self):
        self.lidarr.tfq = {101: MP3}
        self.songs = [SH.SourceFile(path="", title="Porcelain (Live)", duration_ms=240000)]
        self.copy_row("Moby - Play Live")
        self.assertEqual(self.run_lib(), 1)                # no upgrade of the studio song

    def test_another_length_is_not_the_same_recording(self):
        self.lidarr.tfq = {101: MP3}
        self.songs = [SH.SourceFile(path="", title="Porcelain", duration_ms=300000)]
        self.copy_row("Moby - Play Edit")
        self.assertEqual(self.run_lib(), 1)

    def test_anything_unknown_keeps_the_row(self):
        d = self.copy_row("Unparsed")
        self.lidarr.preview = []                           # no quality for the file
        self.assertEqual(self.run_lib(), 0)
        self.o._held_copy_checked = {}
        self.songs = [SH.SourceFile(path="", title="", duration_ms=240000)]
        self.lidarr.preview = [{"path": "L:" + str(d / "01.flac"),
                                "quality": {"quality": {"id": FLAC}}}]
        self.assertEqual(self.run_lib(), 0)                # untagged file
        self.o._held_copy_checked = {}
        self.songs = [SH.SourceFile(path="", title="Porcelain", duration_ms=240000)]
        self.lidarr.pidx = {}
        self.assertEqual(self.run_lib(), 0)                # profile not read
        self.assertEqual(self.left(), ["Unparsed"])

    def test_a_multichannel_download_stays(self):
        self.copy_row("Moby - Play 5.1", details={"max_channels": 6, "multichannel": True})
        self.assertEqual(self.run_lib(), 0)

    def test_off_or_no_budget_leaves_library_copies(self):
        self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(library_budget=0), 0)
        self.assertEqual(self.run_pass(), 0)               # the old call: no library budget


class Ledger(unittest.TestCase):
    def test_judged_needs_the_folders_current_signature(self):
        with tempfile.TemporaryDirectory() as d:
            led = SH.HarvestLedger(None)
            self.assertFalse(led.judged(d, SH.folder_signature(d)))
            led.mark(d, SH.folder_signature(d), [], {})
            self.assertTrue(led.judged(d, SH.folder_signature(d)))
            Path(d, "01.flac").write_bytes(b"z")
            self.assertFalse(led.judged(d, SH.folder_signature(d)))


if __name__ == "__main__":
    unittest.main()
