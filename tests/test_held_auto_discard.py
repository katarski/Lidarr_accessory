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

    def copy_row(self, name, quality=FLAC, details=None, lib=None, album="Play"):
        d = self.row(name, album=album, existing=dict({
            "_ts": time.time(), "in_library": True, "n_audio": 18,
            "_artist": "Moby", "_album": album}, **(lib or {})))
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

    def test_a_compilation_the_library_has_goes_without_a_preview(self):
        self.lidarr._albums[1].append(dict(_album("Gold: Greatest Hits", 18, 18), id=12))
        self.lidarr.tfq = {101: MP3}                      # would be an upgrade
        self.copy_row("Moby - Gold Greatest Hits", album="Gold: Greatest Hits")
        n = self.lidarr.previews
        self.assertEqual(self.run_lib(), 1)
        self.assertEqual(self.lidarr.previews, n)

    def test_lidarr_s_own_compilation_type_counts(self):
        self.lidarr._albums[1].append(dict(_album("Cross Road", 14, 14), id=13,
                                           secondaryTypes=[{"name": "Compilation"}]))
        self.lidarr.tfq = {101: MP3}                      # would be an upgrade
        self.copy_row("Moby - Cross Road (14 classic grooves)",
                      album="Cross Road (14 classic grooves)")
        self.assertEqual(self.run_lib(), 1)

    def test_another_title_s_compilation_type_does_not_count(self):
        self.lidarr._albums[1].append(dict(_album("Pla", 14, 14), id=14,
                                           secondaryTypes=["Compilation"]))
        self.assertEqual(self.o._held_lidarr_types("Moby", "Play"), [])
        self.assertEqual(self.o._held_lidarr_types("Moby", "Pla (2026) {X}"),
                         ["Compilation"])

    def test_why_a_row_stays_is_said(self):
        self.lidarr.tfq = {101: MP3}
        d = self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(), 0)
        self.assertIn("upgrade", self.o._held_stay_reasons[str(d).replace("\\", "/")])

    LIB_FLAC = {"n_audio": 18, "formats": {".flac": 18}, "lossless": True,
                "has_lossy": False, "max_channels": 2, "bits": 16,
                "sample_rate": 44100, "total_bytes": 18 * 30000000,
                "quality": "FLAC"}
    HELD_MP3 = {"n_audio": 1, "formats": {".mp3": 1}, "lossless": False,
                "has_lossy": True, "max_channels": 2, "bits": 0,
                "sample_rate": 44100, "total_bytes": 9000000, "quality": "MP3"}

    def test_when_the_songs_cannot_be_judged_the_folders_decide(self):
        self.copy_row("Moby - Play [MP3]", details=self.HELD_MP3, lib=self.LIB_FLAC)
        self.lidarr.preview = []                           # Lidarr parses nothing
        self.assertEqual(self.run_lib(), 1)                # MP3 over a FLAC library

    def test_a_better_folder_stays_when_the_songs_cannot_be_judged(self):
        held = dict(self.LIB_FLAC, n_audio=1, bits=24, sample_rate=96000,
                    quality="FLAC 24/96")
        self.copy_row("Moby - Play [24-96]", details=held, lib=self.LIB_FLAC)
        self.lidarr.preview = []
        self.assertEqual(self.run_lib(), 0)
        lossy_lib = dict(self.HELD_MP3, n_audio=18, total_bytes=18 * 9000000)
        self.o._held_copy_checked = {}
        self.copy_row("Moby - Play [FLAC]", details=dict(self.LIB_FLAC, n_audio=1),
                      lib=lossy_lib)
        self.lidarr.preview = []
        self.assertEqual(self.run_lib(), 0)                # FLAC over an MP3 library
        self.assertEqual(len(self.left()), 2)

    def test_an_artist_lidarr_lacks_is_judged_by_the_folders(self):
        self.copy_row("Moby - Play [MP3]", details=self.HELD_MP3, lib=self.LIB_FLAC)
        self.lidarr._artists = [a for a in self.lidarr._artists
                                if a["artistName"] != "Moby"]
        self.assertEqual(self.run_lib(), 1)

    def test_a_gap_every_song_of_which_the_library_holds_goes(self):
        self.lidarr._albums[1] = [dict(_album("Play", 10, 18), id=11)]
        self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(), 1)

    def test_a_gap_with_a_song_the_library_lacks_stays(self):
        self.lidarr._albums[1] = [dict(_album("Play", 10, 18), id=11)]
        self.songs.append(SH.SourceFile(path="", title="Natural Blues",
                                        duration_ms=250000))
        self.copy_row("Moby - Play [FLAC]")
        self.assertEqual(self.run_lib(), 0)
        self.assertEqual(self.left(), ["Moby - Play [FLAC]"])


class NoMusicNoIdentity(_Held):
    """5 Nov 2026: 20 of 68 rows held no music at all, 7 had no artist."""

    def bare(self, name, files):
        d = self.watch / name
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_bytes(b"x" * 10)
        self.store.add(str(d), artist="", album=name, tracks=0,
                       outcome="skipped_unmonitored")
        return d

    def test_a_folder_with_no_audio_goes_without_a_summary_or_a_judgement(self):
        self.bare("Montell Jordan - More", ["01.flac.!qb", "02.mp3.!qb"])
        self.bare("C'est si bon", ["album.cue", "album.log", "cover.jpg"])
        self.bare("Abba Gold", [])
        self.assertEqual(self.run_pass(), 3)
        self.assertEqual(self.left(), [])

    def test_an_itunes_lp_s_interface_sounds_are_not_music(self):
        self.bare("sounds", ["Revelator.itlp/TuneKit/sounds/Exit.aif"])
        self.assertEqual(self.run_pass(), 1)

    def test_no_audio_still_downloading_stays(self):
        self.bare("Coming", ["01.flac.!qb"])
        wr = str(self.watch).replace("\\", "/")
        self.qbt.t = [{"content_path": wr + "/Coming", "progress": 0.3,
                       "completion_on": -1}]
        self.assertEqual(self.run_pass(), 0)

    def test_a_folder_an_assembly_plans_to_use_stays(self):
        d = self.row("Moby - Alice CDS")
        e = self.bare("Empty", [])
        self.o.assembly = SimpleNamespace(needed_files=lambda: {
            str(d / "01.flac"): ["Moby - Play"], str(e / "x.flac"): ["Moby - 18"]})
        self.assertEqual(self.run_pass(), 0)
        self.assertEqual(self.left(), ["Empty", "Moby - Alice CDS"])

    def test_no_identity_anywhere_goes_once_the_harvest_judged_it(self):
        d = self.row("Muddy (1990)", artist="", album="Muddy (1990)",
                     existing={"_ts": time.time()})
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.discarded, [d.name])

    def test_an_identity_found_since_keeps_the_row_for_the_summary(self):
        self.row("Barry Manilow – Even Now - (1978)", artist="",
                 album="x", existing={"_ts": time.time()})
        self.row("Unjudged", artist="", album="", judged=False,
                 existing={"_ts": time.time()})
        self.assertEqual(self.run_pass(), 0)


class OtherRecordOfThatName(_Held):
    """Lidarr misses tracks of Miles Davis's 'Blue Moods' (4 songs); the held
    'Blue Moods' is eleven other songs of his (5 Nov 2026)."""

    def setUp(self):
        super().setUp()
        self.lidarr._albums[2] = [dict(_album("Blue Moods", 0, 4), id=21)]
        self.lidarr.tracks = {21: [{"title": t} for t in (
            "Nature Boy", "Alone Together", "There's No You", "Easy Living")]}
        self.songs = ["Time After Time", "Summertime"]
        p = mock.patch.object(SH, "scan_folder", lambda root: [
            SH.SourceFile(path=os.path.join(root, "%02d.flac" % i), title=t,
                          duration_ms=200000) for i, t in enumerate(self.songs)])
        p.start()
        self.addCleanup(p.stop)

    def test_none_of_its_songs_on_the_wanted_album_goes(self):
        self.row("Blue Moods", artist="Miles Davis", album="Blue Moods")
        self.assertEqual(self.run_pass(), 1)

    def test_one_song_of_the_wanted_album_keeps_it(self):
        self.songs.append("Easy Living")
        self.row("Blue Moods", artist="Miles Davis", album="Blue Moods")
        self.assertEqual(self.run_pass(), 0)

    def test_an_untitled_file_keeps_it(self):
        self.songs.append("")
        self.row("Blue Moods", artist="Miles Davis", album="Blue Moods")
        self.assertEqual(self.run_pass(), 0)


class Names(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator.__new__(Orchestrator)
        self.o.lidarr = _Lidarr()

    def test_compilations_and_best_ofs(self):
        r = self.o._compilation_reason
        for t in ("Gold", "The Best Of Etta James", "Cross Road: The Best Of",
                  "Greatest Hits", "The Collection", "Die großen Erfolge",
                  "Etta James Collected", "Smooth Jazz Tribute"):
            self.assertTrue(r(t), t)
        for t in ("Golden Hour", "Hard Candy", "Jagged Little Pill",
                  "The Collection (6 Complete Albums)", "Outland"):
            self.assertIsNone(r(t), t)
        self.assertTrue(r("Hits", secondary_types=["Compilation"]))
        self.assertTrue(r("Now 12", artist="Various Artists"))
        self.assertTrue(r("Lounge", secondary_types=["Compilation", "Live"]))

    def test_dash_and_year_folder_names(self):
        f = self.o._album_folder_identity
        self.assertEqual(f(Path("/d/Barry Manilow – Even Now - (1978)")),
                         ("Barry Manilow", "Even Now"))
        self.assertEqual(f(Path("/d/2005 - Armin van Buuren - Shivers   Serenity")),
                         ("Armin van Buuren", "Shivers Serenity"))


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
