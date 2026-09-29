"""The passes that delete torrent data act only on proof: Lidarr owns every
song file by file, the import has landed, the torrent is ours and is exactly
this folder. Deselected, unknown, submitted or unreadable is never proof."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dedup_downloads  # noqa: E402
import qbt_deselect as QD  # noqa: E402
import song_harvest as SH  # noqa: E402
from qbittorrent_client import QbtClient  # noqa: E402


class _Qbt:
    def __init__(self, torrents=(), files=()):
        self._t, self._f = list(torrents), list(files)
        self.removed, self.categorised = [], []

    def torrents(self, category=""):
        return [t for t in self._t
                if not category or t.get("category") == category]

    def files(self, h):
        return self._f

    def remove(self, h, delete_files=False):
        self.removed.append((h, delete_files))
        return True

    def lookup(self, h):
        return True, next((t for t in self._t
                           if str(t.get("hash")).lower() == str(h).lower()), None)

    def set_file_priority(self, h, idx, prio):
        return True

    def set_category(self, h, cat):
        self.categorised.append(h)
        return True

    def pause(self, h):
        return True


def _entry(name, owned, total=10, artist_id=5):
    return {"artist": "A", "album": "B", "files": [{"name": name, "index": 0}],
            "all_files": [{"name": name, "index": 0}], "size": 1, "have": True,
            "owned": owned, "have_count": total if owned else 0,
            "total": total if owned else 0, "artist_id": artist_id}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "Alb"))
        Path(self.root, "Alb", "01.flac").write_bytes(b"x")
        Path(self.root, "other.txt").write_text("keep the root non-empty")
        self.t = {"hash": "h1", "name": "Alb", "progress": 1, "state": "stalledUP",
                  "content_path": "/data/Alb", "save_path": "/data",
                  "category": "lidarr"}
        self.q = _Qbt([self.t], [{"name": "Alb/01.flac", "index": 0, "priority": 1}])

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, plan, owned_verdict):
        with mock.patch.object(QD, "plan_torrent", lambda *a, **k: plan), \
             mock.patch.object(dedup_downloads, "folder_fully_owned",
                               lambda *a, **k: owned_verdict):
            QD.torrent_lifecycle_pass(self.q, self.root, category="lidarr",
                                      lidarr=object(), min_stable_seconds=0,
                                      now=time.time() + 1000)
        return self.q.removed

    def test_an_album_lidarr_does_not_know_keeps_its_data(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=False)],
                                       (True, "")), [])

    def test_owned_by_count_but_not_file_by_file_keeps_its_data(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=True)],
                                       (False, "01.flac would be imported")), [])

    def test_every_song_owned_file_by_file_is_removed(self):
        self.assertEqual(self.run_pass([_entry("Alb/01.flac", owned=True)],
                                       (True, "")), [("h1", True)])


class Reap(unittest.TestCase):
    def test_deselected_is_not_owned(self):
        f = [{"name": "T/x.flac", "index": 0, "priority": 1}]
        for owned, want in ((False, []), (True, [("h", True)])):
            q = _Qbt(files=f)
            with mock.patch.object(QD, "plan_torrent",
                                   lambda *a, **k: [_entry("T/x.flac", owned)]):
                QD.process_torrent(q, None, {"hash": "h", "name": "T"},
                                   apply=True, reap_useless=True,
                                   emit=lambda s: None)
            self.assertEqual(q.removed, want)


class Adopt(unittest.TestCase):
    def test_only_our_own_torrents_are_adopted(self):
        mine = {"hash": "a", "category": "", "tags": QbtClient.SELF_ADDED_TAG}
        theirs = {"hash": "b", "category": "", "tags": ""}
        q = _Qbt([mine, theirs], [{"name": "x.flac"}])
        QD.adopt_uncategorised(q, "lidarr", emit=lambda s: None)
        self.assertEqual(q.categorised, ["a"])


class _Lidarr:
    failure_generation = 0

    def __init__(self, status, filed):
        self.status, self.filed = status, filed

    def command_status(self, cid):
        return self.status

    def list_tracks_for_album(self, aid):
        return [{"id": 100, "hasFile": self.filed}]


class HarvestPurge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "Box")
        os.makedirs(self.src)
        self.song = os.path.join(self.src, "01.flac")
        Path(self.song).write_bytes(b"x")
        Path(self.src, "cover.jpg").write_bytes(b"x")
        self.led = SH.HarvestLedger(os.path.join(self.tmp.name, "led.json"))
        self.led.add_pending(self.src, {"cid": 1, "mode": "copy",
                                        "paths": [self.song],
                                        "tracks": {"10": [100]}})
        self.q = _Qbt([
            {"hash": "box", "name": "Box", "category": "lidarr",
             "content_path": "/data/Box"},
            {"hash": "disco", "name": "Disco", "category": "lidarr",
             "content_path": "/data/Disco"}])

    def tearDown(self):
        self.tmp.cleanup()

    def settle(self, lidarr):
        return SH.settle_pending_purges(lidarr, self.led, {}, qbt=self.q,
                                        category="lidarr")

    def test_a_running_import_purges_nothing_and_is_remembered(self):
        self.settle(_Lidarr("started", False))
        self.assertTrue(os.path.exists(os.path.join(self.src, "cover.jpg")))
        self.assertIn(self.src, SH.HarvestLedger(self.led.path).pending)

    def test_a_track_left_without_a_file_purges_nothing(self):
        self.settle(_Lidarr("completed", False))
        self.assertEqual(self.q.removed, [])
        self.assertEqual(self.led.pending, {})

    def test_a_landed_import_removes_exactly_this_torrent(self):
        self.settle(_Lidarr("completed", True))
        self.assertFalse(os.path.exists(os.path.join(self.src, "cover.jpg")))
        self.assertEqual(self.q.removed, [("box", True)])

    def test_a_song_nobody_proved_keeps_the_folder_and_its_torrent(self):
        Path(self.src, "02.flac").write_bytes(b"x")
        none = type("R", (), {"matches": []})()
        with mock.patch.object(SH, "read_tags", lambda p: None), \
             mock.patch.object(SH, "match_files", lambda *a, **k: none):
            self.settle(_Lidarr("completed", True))
        self.assertTrue(os.path.exists(os.path.join(self.src, "02.flac")))
        self.assertEqual(self.q.removed, [])


class Discard(unittest.TestCase):
    def test_an_unreadable_file_list_or_another_albums_data_reaps_nothing(self):
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wr = str(Path(tmp.name).resolve()).replace("\\", "/")
        t = {"hash": "d", "name": "Disco", "content_path": wr + "/Disco"}
        this = {"name": "Disco/Album/01.flac", "index": 0, "priority": 1}
        other = {"name": "Disco/Other/01.flac", "index": 1, "priority": 0,
                 "progress": 0.4}
        reaped = []
        for files, want in (([], []), ([this, other], []), ([this], ["d"])):
            q = _Qbt(files=files)
            o = Orchestrator.__new__(Orchestrator)
            o.cfg = type("C", (), {"watch_root": Path(tmp.name)})()
            o._get_qbt = lambda q=q: q
            o._qbt_ours = lambda t: True
            reaped.clear()
            o._reap_torrent = lambda h, blocklist=True: reaped.append(h) or True
            o._deselect_album_in_torrent(t, Path(wr + "/Disco/Album"),
                                         reap_if_last=True)
            self.assertEqual(reaped, want)


class GrabBinding(unittest.TestCase):
    def orch(self, rows):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        removed = []
        o.lidarr = type("L", (), {
            "failure_generation": 0,
            "queue_list": lambda self: rows,
            "queue_remove": lambda self, i, **k: removed.append(i) or True})()
        o._lidarr_generation = lambda: 0
        return o, removed

    def test_another_downloads_title_containing_the_album_is_not_this_grab(self):
        live = {"id": 1, "downloadId": "F3A3", "title": "Confidence Man - 5AM (LA LA LA)",
                "albumId": 77}
        o, _ = self.orch([live])
        before = o._queue_download_ids()
        with mock.patch("orchestrator.time.sleep", lambda s: None):
            self.assertEqual(o._await_grab("Priscilla Ahn - La La La", album_id=5,
                                           before=before, timeout=0), (None, None))
            mine = {"id": 2, "downloadId": "3B55", "title": "Priscilla Ahn - La La La",
                    "albumId": 5}
            o.lidarr.queue_list = lambda: [live, mine]
            self.assertEqual(o._await_grab("x", album_id=5, before=before,
                                           timeout=5)[0], "3b55")

    def test_a_reject_removes_only_the_row_with_its_hash(self):
        rows = [{"id": 1, "downloadId": "AAA", "title": "Other - La La La"},
                {"id": 2, "downloadId": "BBB", "title": "Mine"}]
        o, removed = self.orch(rows)
        o.cfg = type("C", (), {"qbt_category": "lidarr"})()
        q = _Qbt([{"hash": "bbb", "category": "lidarr", "name": "Mine",
                   "content_path": "/downloads/Mine"}])
        o._reject_grab("Priscilla Ahn", "La La La", "bbb", q)
        self.assertEqual((removed, q.removed), ([2], [("bbb", True)]))


class AssemblyAdd(unittest.TestCase):
    """Add to library imports onto empty tracks only, and a source goes only
    after Lidarr has filed its song."""

    def run_add(self, filled_before, files_it):
        from assembly import AssemblyStore
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name, "comp", "07 - Song.flac")
        src.parent.mkdir()
        src.write_bytes(b"x")
        tracks = {1: 55 if filled_before else 0}
        sent = []

        class L:
            failure_generation = 0

            def get_album(self, aid):
                return {"releases": [{"id": 9, "monitored": True}], "artistId": 3}

            def list_tracks_for_album(self, aid):
                return [{"id": t, "trackFileId": f} for t, f in tracks.items()]

            def list_trackfiles_for_album(self, aid):
                return [{"id": 77, "path": "/music/A/B/01.flac"}] if tracks[1] == 77 else []

            def manual_import_candidates(self, path, artist_id=None):
                return []

            def manual_import_apply_files(self, items, import_mode="move"):
                sent.extend(items)
                if files_it:
                    tracks[1] = 77
                return 1

            def wait_for_command(self, cmd, **k):
                return {"status": "completed"}

        o = Orchestrator.__new__(Orchestrator)
        o.lidarr = L()
        o.cfg = type("C", (), {"staging_root": tmp.name})()
        o.assembly = AssemblyStore(None)
        o.assembly.upsert(5, {"artist": "A", "album": "B", "total": 1,
                              "matched": [{"source": str(src), "track_id": 1,
                                           "number": 1, "track": "Song"}],
                              "sources": {str(src): 1}})
        o._lidarr_generation = lambda: 0
        o._trigger_artist_refresh = lambda *a, **k: None
        o._write_basic_tags = lambda *a, **k: None
        ok, _msg = o.assembly_add_to_library(5)
        return ok, sent, src.exists()

    def test_a_track_filled_since_the_plan_is_not_imported_onto(self):
        ok, sent, kept = self.run_add(filled_before=True, files_it=True)
        self.assertEqual((ok, sent, kept), (False, [], True))

    def test_a_completed_command_that_filed_nothing_keeps_the_source(self):
        ok, sent, kept = self.run_add(filled_before=False, files_it=False)
        self.assertEqual((ok, len(sent), kept), (False, 1, True))

    def test_a_filed_song_frees_its_source(self):
        ok, sent, kept = self.run_add(filled_before=False, files_it=True)
        self.assertEqual((ok, len(sent), kept), (True, 1, False))
        self.assertNotIn("_source", sent[0])


class WebuiResolve(unittest.TestCase):
    """Add/Overwrite imports only the files it copied, and deletes the held
    folder only when every song has an equal copy in the library."""

    def resolve(self, held, library, overwrite=False):
        from orchestrator import Orchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src, lib = Path(tmp.name, "held"), Path(tmp.name, "lib")
        for rel, data in held.items():
            Path(src, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(src, rel).write_bytes(data)
        lib.mkdir()
        for rel, data in library.items():
            Path(lib, rel).write_bytes(data)
        forced, deleted = [], []
        o = Orchestrator.__new__(Orchestrator)
        o.lidarr = type("L", (), {
            "downloaded_albums_scan_rescan": lambda self, p: 1,
            "process_monitored_downloads": lambda self: 1})()
        o._held_audio_files = lambda f: sorted(f.rglob("*.flac"))
        o._resolve_library_target = lambda e: (lib, None)
        o._force_import_library_folder = (
            lambda t, a, files: forced.extend(p.name for p in files) or True)
        o._remove_torrent_for_folder = lambda f: ""
        o._delete_folder_under_watch = lambda f: deleted.append(f) or True
        ok, _ = o._apply_to_library({"source_path": str(src)}, overwrite)
        return ok, sorted(forced), bool(deleted)

    def test_two_discs_with_one_file_name_are_refused(self):
        self.assertEqual(self.resolve({"CD1/01.flac": b"a", "CD2/01.flac": b"b"}, {}),
                         (False, [], False))

    def test_a_different_library_file_of_that_name_keeps_the_held_folder(self):
        self.assertEqual(self.resolve({"01.flac": b"new", "02.flac": b"x"},
                                      {"01.flac": b"library"}),
                         (False, ["02.flac"], False))

    def test_every_song_copied_or_identical_deletes_and_imports_the_copies(self):
        self.assertEqual(self.resolve({"01.flac": b"same", "02.flac": b"x"},
                                      {"01.flac": b"same"}),
                         (True, ["02.flac"], True))

    def test_overwrite_never_force_imports_a_replaced_file(self):
        self.assertEqual(self.resolve({"01.flac": b"new!"}, {"01.flac": b"old"},
                                      overwrite=True), (True, [], True))


class _ElsewhereClaim:
    """Another thread claims `path` for the duration of the with-block."""

    def __init__(self, path):
        import threading
        self.path, self.got, self.done = path, threading.Event(), threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import claims
        claims.claim(self.path)
        self.got.set()
        self.done.wait(10)
        claims.release(self.path)

    def __enter__(self):
        self.th.start()
        self.got.wait(5)
        return self

    def __exit__(self, *a):
        self.done.set()
        self.th.join(5)


class ClaimsFunnel(unittest.TestCase):
    """CLAIMS-1: no folder is deleted, and no torrent removed, while another
    worker is processing it; every torrent removal is one checked route."""

    def orch(self, torrents, rows, gen_bump=False):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(qbt_category="lidarr")
        lid = SimpleNamespace(failure_generation=0, removed=[])

        def queue_list():
            if gen_bump:
                lid.failure_generation += 1
                return []
            return rows

        def queue_remove(i, remove_from_client=False, blocklist=False):
            lid.removed.append((i, remove_from_client, blocklist))
            return True
        lid.queue_list, lid.queue_remove = queue_list, queue_remove
        o.lidarr = lid
        q = _Qbt(torrents)
        o._get_qbt = lambda: q
        return o, lid, q

    def test_folder_is_not_deleted_while_another_worker_holds_it(self):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / "Artist - Album"
            f.mkdir()
            (f / "01.flac").write_bytes(b"x")
            o = Orchestrator.__new__(Orchestrator)
            o.cfg = SimpleNamespace(watch_root=Path(root))
            with _ElsewhereClaim(f):
                self.assertFalse(o._delete_folder_under_watch(f))
                self.assertTrue((f / "01.flac").exists())
            self.assertTrue(o._delete_folder_under_watch(f))
            self.assertFalse(f.exists())

    def test_claimed_torrent_keeps_its_rows_and_its_data(self):
        t = {"hash": "abc", "category": "lidarr", "name": "Album",
             "content_path": "/downloads/Album"}
        o, lid, q = self.orch([t], [{"id": 9, "downloadId": "ABC"}])
        with _ElsewhereClaim("/downloads/Album"):
            self.assertFalse(o._reap_torrent("ABC", blocklist=True))
        self.assertEqual((lid.removed, q.removed), ([], []))

    def test_lidarr_never_deletes_from_the_client(self):
        t = {"hash": "abc", "category": "lidarr", "name": "Album",
             "content_path": "/downloads/Album"}
        o, lid, q = self.orch([t], [{"id": 9, "downloadId": "ABC"},
                                    {"id": 8, "downloadId": "OTHER"}])
        self.assertTrue(o._reap_torrent("ABC", blocklist=True))
        self.assertEqual((lid.removed, q.removed), ([(9, False, True)],
                                                    [("abc", True)]))

    def test_another_apps_torrent_is_refused_rows_and_all(self):
        t = {"hash": "abc", "category": "tv", "content_path": "/downloads/Show"}
        o, lid, q = self.orch([t], [{"id": 9, "downloadId": "abc"}])
        self.assertFalse(o._reap_torrent("abc"))
        self.assertEqual((lid.removed, q.removed), ([], []))

    def test_qbittorrent_outage_removes_nothing(self):
        o, lid, q = self.orch([], [{"id": 9, "downloadId": "abc"}])
        q.lookup = lambda h: (False, None)
        self.assertFalse(o._reap_torrent("abc"))
        self.assertEqual((lid.removed, q.removed), ([], []))

    def test_unreadable_queue_defers_a_blocklisting_removal(self):
        t = {"hash": "abc", "category": "lidarr", "content_path": "/downloads/A"}
        o, lid, q = self.orch([t], [], gen_bump=True)
        self.assertFalse(o._reap_torrent("abc", blocklist=True))
        self.assertEqual(q.removed, [])

    def test_webui_discard_waits_for_the_worker(self):
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / "Held"
            f.mkdir()
            o = Orchestrator.__new__(Orchestrator)
            o._discard = lambda e: (True, "discarded")
            with _ElsewhereClaim(f):
                ok, msg = o.discard({"source_path": str(f)})
            self.assertFalse(ok)
            self.assertIn("being processed", msg)
            self.assertEqual(o.discard({"source_path": str(f)}), (True, "discarded"))

    def test_harvest_purge_of_a_claimed_source_stays_pending(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as src:
            ledger = SimpleNamespace(
                pending={src: {"cid": 1, "mode": "copy", "paths": [],
                               "tracks": {"5": [1]}}},
                save=lambda: None)
            ledger.drop_pending = lambda s: ledger.pending.pop(s, None)
            lid = SimpleNamespace(
                failure_generation=0, command_status=lambda c: "completed",
                list_tracks_for_album=lambda a: [{"id": 1, "hasFile": True}])
            purged = []
            with mock.patch.object(SH, "purge_leftovers",
                                   lambda s, **k: purged.append(s)):
                with _ElsewhereClaim(src):
                    self.assertEqual(SH.settle_pending_purges(lid, ledger, {}), 0)
                self.assertEqual((purged, list(ledger.pending)), ([], [src]))
                self.assertEqual(SH.settle_pending_purges(lid, ledger, {}), 1)
            self.assertEqual((purged, ledger.pending), ([src], {}))

    def test_qbt_remove_is_not_confirmed_by_a_failed_lookup(self):
        c = QbtClient.__new__(QbtClient)
        c.base = "http://q"
        answers = iter([(True, {"hash": "abc", "content_path": "/downloads/A"})]
                       + [(False, None)] * 3)
        c.lookup = lambda h: next(answers)
        c.s = mock.Mock()
        with mock.patch("qbittorrent_client.time.sleep"):
            self.assertFalse(c.remove("abc"))
        c.lookup = lambda h: (False, None)
        c.s = mock.Mock()
        self.assertFalse(c.remove("abc"))
        c.s.post.assert_not_called()


class ClaimsEveryActor(unittest.TestCase):
    """loops F4: reconcile, the library audit, held auto-resolve, the nudge
    and the library move each work on a folder only while holding it."""

    def _reconcile(self, landed=True):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(sweep_min_stable_seconds=0, content_identify=False,
                                manual_import_timeout_seconds=5)
        calls = []
        o.lidarr = SimpleNamespace(
            list_all_albums=lambda: [{
                "id": 1, "monitored": True, "artist": {"artistName": "Some Artist"},
                "statistics": {"totalTrackCount": 2, "trackFileCount": 0}}],
            manual_import_folder=lambda f, **k: calls.append(f) or (77, 2, ["Album"]))
        o._release_llm_waiting = lambda: None
        o._llm_blocked = lambda f: False
        o._wait_for_manual_import = lambda c, f, timeout, **k: landed
        o._reconcile_cache = {}
        return o, calls

    @staticmethod
    def _album(root):
        f = Path(root) / "Some Artist - Album"
        f.mkdir()
        for n in ("01.flac", "02.flac"):
            (f / n).write_bytes(b"x")
        return f

    def test_reconcile_skips_a_claimed_folder_without_a_verdict(self):
        with tempfile.TemporaryDirectory() as root:
            f = self._album(root)
            o, calls = self._reconcile()
            with _ElsewhereClaim(f):
                self.assertEqual(o.reconcile_monitored_gaps(Path(root)), 0)
            self.assertEqual((calls, o._reconcile_cache), ([], {}))
            self.assertEqual(o.reconcile_monitored_gaps(Path(root)), 2)
            self.assertEqual(calls, [str(f)])

    def test_reconcile_counts_only_an_import_that_landed(self):
        with tempfile.TemporaryDirectory() as root:
            f = self._album(root)
            o, calls = self._reconcile(landed=False)
            self.assertEqual(o.reconcile_monitored_gaps(Path(root)), 0)
            self.assertIn(str(f), o._reconcile_cache)
            # Not submitted again while Lidarr may still be on it.
            self.assertEqual(o.reconcile_monitored_gaps(Path(root)), 0)
            self.assertEqual(calls, [str(f)])

    def test_audit_skips_a_library_folder_a_worker_holds(self):
        import inspect
        import claims
        from orchestrator import Orchestrator
        self.assertIn("self._claimed_dirs(album_children",
                      inspect.getsource(Orchestrator.audit_library_vs_lidarr))
        with tempfile.TemporaryDirectory() as root:
            a, b = Path(root) / "A", Path(root) / "B"
            seen = []
            with _ElsewhereClaim(a):
                for d in Orchestrator._claimed_dirs([a, b], "audit"):
                    seen.append((d, claims._norm(d) in claims._held))
            self.assertEqual(seen, [(b, True)])
            for d in Orchestrator._claimed_dirs([b], "audit"):
                break
            self.assertNotIn(claims._norm(b), claims._held)

    def test_held_auto_resolve_waits_for_the_worker(self):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / "Held"
            f.mkdir()
            row = {"id": 1, "source_path": str(f), "artist": "A", "album": "B",
                   "existing": {"lidarr": {"present": True, "monitored": True,
                                           "have": 0, "total": 2}}}
            gone, tried = [], []
            o = Orchestrator.__new__(Orchestrator)
            o.held = SimpleNamespace(list=lambda: [row], remove=gone.append)
            o._held_audio_files = lambda p: [p / "1.flac", p / "2.flac"]
            o._try_positional_force_import = lambda *a: tried.append(a[1]) or True
            with _ElsewhereClaim(f):
                self.assertEqual(o._auto_resolve_held_gaps(), 0)
            self.assertEqual((tried, gone), ([], []))
            self.assertEqual(o._auto_resolve_held_gaps(), 1)
            self.assertEqual((tried, gone), ([f], [1]))

    def test_nudge_and_library_move_wait_for_the_audit(self):
        import threading
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as root:
            d = Path(root) / "Artist" / "Album"
            o = Orchestrator.__new__(Orchestrator)
            o.cfg = SimpleNamespace(library_root_windows=Path(root),
                                    album_folder_template="")
            ran = []
            o._nudge_positional = lambda *a: ran.append("nudge") or True
            o._move_into_library = lambda p, s, t: ran.append(str(t)) or t
            plan = SimpleNamespace(albumartist="Artist", artist="Artist")
            with mock.patch("orchestrator.album_folder_name",
                            lambda p, template=None: "Album"):
                with _ElsewhereClaim(d):
                    ths = [threading.Thread(target=o._nudge_positional_force_import,
                                            args=(1, 2, d, 3), daemon=True),
                           threading.Thread(target=o._manual_move_to_library,
                                            args=([plan], []), daemon=True)]
                    for th in ths:
                        th.start()
                    time.sleep(0.3)
                    self.assertEqual(ran, [])
                for th in ths:
                    th.join(5)
            self.assertEqual(sorted(ran), sorted(["nudge", str(d)]))


if __name__ == "__main__":
    unittest.main()
