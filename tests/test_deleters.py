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


class AssemblyPlansSurviveAnOutage(unittest.TestCase):
    """ASM-PLAN-1: a pass during a Lidarr outage prunes no plan, and a
    keep-set that cannot be read stops the deselect instead of reading as
    'assembly off'."""

    class _Store:
        def __init__(self):
            self.plans, self.pruned = {"5": {"id": 5}}, []

        def list(self):
            return list(self.plans.values())

        def get(self, aid):
            return self.plans.get(str(aid))

        def remove(self, aid):
            return self.plans.pop(str(aid), None) is not None

        def upsert(self, aid, plan):
            self.plans[str(aid)] = plan

        def keep_only(self, ids):
            self.pruned.append(list(ids))
            keep = {str(i) for i in ids}
            self.plans = {k: v for k, v in self.plans.items() if k in keep}

    def _pass(self, root, albums, tracks=lambda aid: []):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        src = Path(root) / "Best Of"
        src.mkdir()
        (src / "01.flac").write_bytes(b"x")
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace()
        o.assembly = store = self._Store()
        o.held = SimpleNamespace(list=lambda: [{"source_path": str(src)}])
        o._read_song_tags = lambda p: ("Song", "Some Artist", "Best Of")
        lid = SimpleNamespace(failure_generation=0)
        lid.list_all_albums = lambda: albums(lid)
        o.lidarr = lid
        o._album_track_titles_rows = tracks
        o._assembly_continue_hunts = lambda **k: 0
        o.assembly_plan_pass()
        return store

    GAP = {"id": 7, "monitored": True, "title": "Album",
           "artist": {"artistName": "Some Artist"},
           "statistics": {"totalTrackCount": 10, "trackFileCount": 2}}

    def test_an_outage_prunes_no_plan(self):
        def down(lid):
            lid.failure_generation += 1
            return []
        with tempfile.TemporaryDirectory() as root:
            store = self._pass(root, down)
        self.assertEqual((store.pruned, list(store.plans)), ([], ["5"]))

    def test_a_missing_track_list_prunes_no_plan(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._pass(root, lambda lid: [self.GAP])
        self.assertEqual((store.pruned, list(store.plans)), ([], ["5"]))

    def test_an_answered_pass_still_prunes(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._pass(root, lambda lid: [])
        self.assertEqual((store.pruned, store.plans), ([[]], {}))

    def test_an_unreadable_keep_set_is_not_assembly_off(self):
        import inspect
        from types import SimpleNamespace
        import main
        self.assertIsNone(main._assembly_keep(SimpleNamespace(assembly=None)))

        class Broken:
            def needed_files(self):
                raise OSError("plans unreadable")
        with self.assertRaises(OSError):
            main._assembly_keep(SimpleNamespace(assembly=Broken()))
        src = inspect.getsource(main)
        self.assertIn("acted = asm_known and auto_deselect_pass(", src)
        self.assertIn("assembly_keep=_asm_keep_now(), llm=match_llm", src)


class SongEvidenceDecides(unittest.TestCase):
    """orch2 M11821: a release whose songs only partly match is not accepted
    on its file count, and not blocklisted; a disc image is judged by the
    image rules, not by its one file name."""

    def _verify(self, cov, image=False):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace()
        o._await_torrent_files = lambda q, h, l: [{"name": "x.flac"}]
        o._classify_torrent_files = lambda f: {
            "audio_count": 1 if image else 21, "is_dsd": False, "is_iso": False,
            "is_single_image": image, "has_cue": image}
        scored = []
        o._best_release_title_coverage = (
            lambda f, aid: scored.append(aid) or (cov, int(cov * 12), 12))
        verdict, _info = o._verify_torrent(object(), "h", 12, "Dusty", album_id=7)
        return verdict, scored, o

    def test_the_gray_zone_is_not_accepted_nor_blocklisted(self):
        verdict, _s, o = self._verify(0.25)
        self.assertEqual(verdict, "unsure:songs-partly-match(3/12)")
        self.assertFalse(o._blocklists(verdict))
        self.assertTrue(o._blocklists(self._verify(0.2)[0]))
        self.assertEqual(self._verify(0.6)[0], "accept")

    def test_a_disc_image_is_not_scored_by_its_file_name(self):
        verdict, scored, _o = self._verify(0.0, image=True)
        self.assertEqual((verdict, scored), ("accept", []))

    def test_an_unsure_grab_is_removed_without_blocklisting(self):
        import inspect
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        seen = []
        o._remove_torrent = lambda h, **k: seen.append(k["blocklist"]) or True
        o._reject_grab("A", "B", "h", None, blocklist=False)
        o._reject_by_hash("h", None, blocklist=False)
        o._reject_grab("A", "B", "h", None)
        self.assertEqual(seen, [False, False, True])
        src = inspect.getsource(Orchestrator)
        self.assertEqual(src.count("block = self._blocklists(verdict)"), 3)


class TitleNamesTheAlbum(unittest.TestCase):
    """orch2 F4: a release title is evidence for an album only when it NAMES
    it -- the album's words a whole field once the artist is taken out, and
    not inside a more specific album of the same artist. Below the title
    floor nothing is grabbed, not even after better candidates failed."""

    DUSTY = ["Ev'rything's Coming Up Dusty", "Dusty in Memphis",
             "Simply Dusty", "A Girl Called Dusty"]

    def test_naming_verdicts(self):
        import titlematch as T
        rows = [
            # (title, album, artist, siblings, verdict)
            ("Dusty Springfield-Ev'rything's Coming Up Dusty(1965;1998) [FLAC]",
             "Dusty", "Dusty Springfield", self.DUSTY, "sibling"),
            ("Dusty Springfield-Ev'rything's Coming Up Dusty(1965;1998) [FLAC]",
             "Dusty", "Dusty Springfield", [], "embedded"),
            ("Dusty Springfield - Dusty In Memphis (Deluxe Edition FLAC) TNT V",
             "Dusty", "Dusty Springfield", self.DUSTY, "sibling"),
            ("Dusty Springfield - Dusty (1964) [FLAC]",
             "Dusty", "Dusty Springfield", self.DUSTY, "named"),
            ("(Pop / Folk / Soft Rock) Jewel - 0304 - 2003 (CD-Extra), FLAC "
             "(image+.cue) lossless", "0304", "Jewel", [], "named"),
            ("Frida - Frida (1996) [FLAC]", "Frida", "Frida", [], "named"),
            ("ABBA, Björn, Benny, Agnetha & Frida - Waterloo 2 lp - 1974, DSD 128",
             "Frida", "Frida", [], "embedded"),
            ("Kool & The Gang - Kool and the Gang - 1969",
             "Kool and the Gang", "Kool & the Gang", [], "named"),
            ("Joni Mitchell - Blue Eyed Soul (2003)", "Blue", "Joni Mitchell",
             [], "embedded"),
            ("Pink Floyd - The Wall 1979 FLAC", "The Wall", "Pink Floyd", [],
             "named"),
            ("[TR24][OF] Voces8 - Bach, Dove, Monteverdi, Stopford, Whitacre - "
             "After Silence II. Devotion - 2020 (Classical)", "After Silence",
             "VOCES8", [], "embedded"),
            ("(Score) [CD] / Joker (by Hildur Guðnadóttir / Hildur Gudnadottir) "
             "(Original Motion Picture Soundtrack) - 2019, FLAC (tracks+.cue)",
             "Joker (Original Motion Picture Soundtrack)", "Hildur Guðnadóttir",
             [], "named"),
            ("Beatles - The Beatles (White Album)", "The Beatles",
             "The Beatles", [], "named"),
            ("Dusty_Springfield-Dusty_In_Memphis-2CD-FLAC-2002-DJ",
             "Dusty in Memphis", "Dusty Springfield", [], "named"),
            ("Chicago - Chicago 17 (1984) [FLAC]", "Chicago", "Chicago",
             ["Chicago 17"], "sibling"),
            # An edition is not another record.
            ("Artist - Album (Deluxe Edition) [FLAC]", "Album", "Artist",
             ["Album (Deluxe Edition)"], "named"),
            ("[TR24][OF] Alanis Morissette - Jagged Little Pill [Acoustic] "
             "[24bit-192kHz] - 1995 / 2005 (Rock)", "Jagged Little Pill",
             "Alanis Morissette", ["Jagged Little Pill Acoustic"], "sibling"),
            ("Simply Red   Blue Eyed Soul (2019) [CD Rip] [320 KBPS]", "Blue",
             "Simply Red", ["Blue Eyed Soul"], "sibling"),
            # Found in Lidarr's grab history: separators trackers really use.
            ("Macklemore & Ryan Lewis   BEN (2023) [24Bit 44.1kHz] FLAC",
             "Ben", "Macklemore", [], "named"),
            ("[TR24][OF][LDR] Bryan Adams - Bryan Adams: Classic (Reissue) - "
             "2025 (Rock Pop)", "Classic", "Bryan Adams", [], "named"),
            ("Miley Cyrus &ndash; Bass Persuades (The Miley Edition) Pop (2026)",
             "Bass Persuades", "MILEY", [], "named"),
            ("(Melodic Rock) [CD] Michael Bolton - Michael Bolton•Everybody's "
             "Crazy - Two Originals", "Everybody’s Crazy", "Michael Bolton", [],
             "named"),
            ("(Midwest Rap, Hip Hop, Scene) [CD] Twista - The Dark Horse "
             "(Deluxe Version) - 2014, FLAC (tracks), lossless", "Dark Horse",
             "Twista", [], "named"),
            ("[SACD-R][OF] Miles Davis - Cookin' with the Miles Davis Quintet "
             "(Analogue Productions) - 2014", "Cookin’ With the Miles Davis "
             "Quintet", "Miles Davis Quintet", [], "named"),
            ("(Pop-rock) Barenaked Ladies & The Persuasions - Ladies and "
             "Gentlemen - 2017, MP3, 320 kbps", "Ladies and Gentlemen: Barenaked "
             "Ladies and The Persuasions", "Barenaked Ladies", [], "named"),
            ("(Funk) [CD] Kool & The Gang - Wild And Peaceful - 1996, FLAC",
             "Kool and the Gang", "Kool & the Gang", [], "embedded"),
            ("(Pop) [CD] ABBA - The Best Of ABBA - 2000, FLAC (image+.cue)",
             "ABBA", "ABBA", [], "embedded"),
            ("(Soundtrack) Kusa No Ran by Deep Forest - 2004, FLAC",
             "Kusa no Ran", "Deep Forest", [], "named"),
            ("(vocal jazz) Ray Charles - Ray Charles Invites You To Listen - "
             "1967", "Invites You to Listen", "Ray Charles", [], "named"),
            ("The Mars Volta - Bedlam in Goliath [2008] [320kbps]",
             "The Bedlam in Goliath", "The Mars Volta", [], "named"),
            ("(House) [CD] Crystal Waters - The Best Of - 1998, FLAC",
             "The Best of Crystal Waters", "Crystal Waters", [], "named"),
            ("Aretha Franklin - Lady Soul & Aretha Now (MFSL 623) FLAC",
             "Aretha Now", "Aretha Franklin", [], "named"),
            ("Gipsy Kings - The Real... Gipsy Kings (2014) (3 CD) [FLAC]",
             "Gipsy Kings", "Gipsy Kings", [], "embedded"),
            ("[TR24][OF] Joe Bonamassa - B.B. King's Blues Summit 100 - 2026",
             "Blues Summit", "B.B. King", [], "embedded"),
            # A colon opens a field but does not close one: after the name
            # it begins a subtitle that can make it another record.
            ("[TR24][OF][FM] Hildur Guðnadóttir - Joker: Folie à Deux (Score) "
             "(Original Motion Picture Soundtrack) - 2024",
             "Joker: Original Motion Picture Soundtrack", "Hildur Guðnadóttir",
             [], "embedded"),
            # A lone apostrophe is not a quote.
            ("[TR24][OF] The Drifters - The Drifters' Golden Hits (1959-1966 "
             "mono) - 1968/2021", "The Drifters", "The Drifters", [],
             "embedded"),
            ("(Jazz) [LP] Melody Gardot & Philippe Powell 'Entre Eux Deux' - "
             "2022, WavPack", "Entre eux deux", "Melody Gardot", [], "named"),
        ]
        for title, album, artist, others, want in rows:
            spec = T.more_specific_albums(album, others)
            got = T.album_naming(title, album, artist, spec)
            self.assertEqual(got, want, (title, album))

    def test_the_three_dusty_grabs_now_score_below_the_live_floor(self):
        import titlematch as T
        from orchestrator import Orchestrator
        rel = Orchestrator._title_relation
        spec = T.more_specific_albums("Dusty", self.DUSTY)
        for wrong in ("Dusty Springfield-Ev'rything's Coming Up Dusty(1965;1998) [FLAC]",
                      "Dusty Springfield - Dusty In Memphis (Deluxe Edition FLAC) TNT V",
                      "Dusty Springfield Simply Dusty 4CD 2000 FLAC-DJ"):
            self.assertLess(rel("Dusty Springfield", "Dusty", wrong, spec), 0.45,
                            wrong)
        # Not even without the discography: the words sit in a longer name.
        self.assertLess(rel("Dusty Springfield", "Dusty",
                            "Dusty Springfield-Ev'rything's Coming Up Dusty(1965;1998)"
                            " [FLAC]"), 0.45)
        self.assertLess(rel("Joni Mitchell", "Blue",
                            "Joni Mitchell - Blue Eyed Soul (2003)"), 0.45)
        self.assertLess(rel("Kool & the Gang", "Kool and the Gang",
                            "Kool & The Gang - Wild And Peaceful"), 0.45)
        for right, artist, album in (
                ("Dusty Springfield - Dusty (1964) [FLAC]", "Dusty Springfield",
                 "Dusty"),
                ("(Pop / Folk / Soft Rock) Jewel - 0304 - 2003 (CD-Extra), FLAC "
                 "(image+.cue) lossless", "Jewel", "0304"),
                ("Frida - Frida (1996) [FLAC]", "Frida", "Frida")):
            self.assertEqual(rel(artist, album, right, spec
                                 if album == "Dusty" else ()), 1.0, right)

    def _orch(self, albums, fail=False):
        from types import SimpleNamespace
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.cfg = SimpleNamespace(
            interactive_search_require_lossless=True,
            interactive_search_min_title_ratio=0.45,
            interactive_search_min_seeders=1,
            interactive_search_refuse_unofficial=True,
            interactive_search_max_candidates=1000,
            interactive_search_dry_run=False)
        asked = []

        def list_albums_for_artist(aid):
            asked.append(aid)
            if fail:
                o.lidarr.failure_generation += 1
                return []
            return [{"id": i, "title": t} for i, t in albums]
        o.lidarr = SimpleNamespace(failure_generation=0,
                                   list_albums_for_artist=list_albums_for_artist,
                                   available=lambda: True)
        return o, asked

    def _rel(self, title, seeders=5):
        return {"protocol": "torrent", "guid": title, "indexerId": 1,
                "title": title, "seeders": seeders, "leechers": 0,
                "artistName": "Dusty Springfield",
                "quality": {"quality": {"name": "FLAC"}}}

    def test_the_ranker_asks_the_discography_once_per_pass(self):
        o, asked = self._orch([(1, "Dusty"), (2, "Dusty in Memphis"),
                               (3, "Ev'rything's Coming Up Dusty")])
        rec = {"id": 1, "artistId": 9, "title": "Dusty"}
        rels = [self._rel("Dusty Springfield - Dusty In Memphis [FLAC]", 50),
                self._rel("Dusty Springfield - Dusty (1964) [FLAC]", 2)]
        for _ in range(2):
            ranked = o._rank_releases(rels, "Dusty Springfield", "Dusty", [],
                                      album_rec=rec)
        self.assertEqual(asked, [9])
        self.assertEqual(ranked[0]["title"],
                         "Dusty Springfield - Dusty (1964) [FLAC]")
        self.assertEqual(ranked[1]["_title_ratio"], 0.0)
        # The album's own title is never its own sibling.
        self.assertNotIn("Dusty", o._artist_album_titles(9, exclude=1))

    def test_a_failed_album_list_is_not_cached(self):
        o, asked = self._orch([], fail=True)
        self.assertEqual(o._artist_album_titles(9), [])
        self.assertEqual(o._artist_album_titles(9), [])
        self.assertEqual(asked, [9, 9])

    def test_below_the_floor_is_never_grabbed(self):
        o, _asked = self._orch([])
        grabbed = []
        top = {"guid": "g1", "indexerId": 1, "title": "right", "_title_ratio": 1.0}
        low = {"guid": "g2", "indexerId": 1, "title": "wrong", "_title_ratio": 0.3}
        o._rank_releases = lambda *a, **k: [top, low]
        o.lidarr.release_search = lambda aid: [top, low]
        o.lidarr.release_grab = (
            lambda guid, idx, **k: grabbed.append(guid) or True)
        o._queue_download_ids = lambda: set()
        o._await_grab = lambda *a, **k: ("h", {})
        o.record_grab_target = lambda *a, **k: None
        o._verify_torrent = lambda *a, **k: ("reject:songs", {})
        o._reject_grab = lambda *a, **k: None
        o._isearch_prowlarr_album = lambda *a, **k: False
        alb = {"id": 1, "artistId": 9, "title": "Dusty",
               "artist": {"artistName": "Dusty Springfield"}}
        self.assertFalse(o._isearch_one_album(alb, {}, None))
        self.assertEqual(grabbed, ["g1"])


class HandoffKeepsWhatLidarrDidNotTake(unittest.TestCase):
    """orch2 F9: a multi-disc album is handed off at its PARENT, audio in
    CD1/CD2. Success is judged by the files sent, "remaining" by a walk of
    the whole folder, and the source (folder and .cue) is kept while any
    audio Lidarr did not take is still in it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / "Artist - Album (2CD)"
        old = time.time() - 600
        for disc in ("CD1", "CD2"):
            (self.folder / disc).mkdir(parents=True)
            for n in ("01.flac", "02.flac"):
                f = self.folder / disc / n
                f.write_bytes(b"x")
                os.utime(f, (old, old))

    def tearDown(self):
        self._tmp.cleanup()

    def _orch(self, offered, moves, delete_folder=True):
        from types import SimpleNamespace
        import orchestrator as O
        o = O.Orchestrator.__new__(O.Orchestrator)
        o.cfg = SimpleNamespace(
            sweep_min_stable_seconds=0, transcode_lossless_to_flac=False,
            pre_split_monitored_gap_only=False, pre_check_lidarr_library=False,
            manual_import_timeout_seconds=60, cleanup_lidarr_queue=False,
            verify_library_after_import=True, min_match_percent=50,
            delete_source_folder_on_success=delete_folder,
            watch_root=self.root)
        o._skip_seen, o.acoustid, o.cued = set(), None, []

        def apply(items):
            for it in items:        # Lidarr MOVES what it imports
                if Path(it["path"]) in moves:
                    Path(it["path"]).unlink()
            return 9
        o.lidarr = SimpleNamespace(
            find_artist=lambda n: {"id": 5}, queue_find_for=lambda a, b: None,
            windows_to_lidarr=str, lidarr_to_windows=str,
            manual_import_candidates=lambda p, artist_id=None: [
                {"path": str(f)} for f in offered],
            manual_import_apply=apply,
            command_record=lambda c: {"status": "completed",
                                      "result": "successful"})
        o._read_audio_tags = lambda a: ("Artist", "Album")
        o._is_placeholder_identity = lambda a, b: False
        o._align_release_to_disk = lambda *a: None
        o._log_rejections = lambda c: None
        o._filter_acceptable = lambda c: c
        o._hydrate_candidates = lambda c, *a: [dict(x, artistId=5) for x in c]
        o._record = lambda *a, **k: None
        o._trigger_artist_refresh = lambda *a, **k: None
        o._log_command_record = lambda *a: None
        # Lidarr's library reads the album complete BY NAME in every case.
        o._verify_library_reflects_album = lambda *a, **k: True
        o._delete_orphan_cue = lambda cue, reason: o.cued.append(cue)
        return o

    def _run(self, o):
        clock = iter(range(0, 10 ** 6, 7))
        with mock.patch("orchestrator.time.monotonic",
                        side_effect=lambda: next(clock)), \
                mock.patch("orchestrator.time.sleep"):
            o._handoff_inner(None, self.folder, reason="t")

    def _disc(self, n):
        return sorted((self.folder / f"CD{n}").glob("*.flac"))

    def test_a_listing_looks_below_the_parent(self):
        import orchestrator as O
        o = O.Orchestrator.__new__(O.Orchestrator)
        exts = O._ALL_AUDIO_EXTS
        self.assertFalse(o._staging_cleared(self.folder, exts))
        for f in self._disc(1):
            f.unlink()
        self.assertTrue(o._staging_cleared(self.folder, exts,
                                           [self.folder / "CD1" / "01.flac"]))
        self.assertFalse(o._staging_cleared(self.folder, exts))
        for f in self._disc(2):
            f.unlink()
        self.assertTrue(o._staging_cleared(self.folder, exts))

    def test_sent_paths_keep_their_disc_folder(self):
        import orchestrator as O
        o = O.Orchestrator.__new__(O.Orchestrator)
        got = o._submitted_paths(self.folder, "/lidarr/Artist - Album (2CD)", [
            {"path": "/lidarr/Artist - Album (2CD)/CD2/01.flac"}, {"path": ""}])
        self.assertEqual(got, [self.folder / "CD2" / "01.flac"])

    def test_a_disc_never_sent_keeps_the_source(self):
        for delete_folder in (True, False):
            with self.subTest(delete_folder=delete_folder):
                cd1 = self._disc(1)
                o = self._orch(cd1, set(cd1), delete_folder)
                self._run(o)
                self.assertEqual(len(self._disc(2)), 2)
                self.assertEqual(o.cued, [])
                old = time.time() - 600
                for f in cd1:           # put Disc 1 back for the next round
                    f.write_bytes(b"x")
                    os.utime(f, (old, old))

    def test_a_sent_file_still_here_is_not_success(self):
        cd1 = self._disc(1)
        o = self._orch(cd1 + self._disc(2), set())
        self._run(o)
        self.assertEqual(len(self._disc(1)) + len(self._disc(2)), 4)
        self.assertEqual(o.cued, [])

    def test_everything_taken_cleans_up(self):
        every = self._disc(1) + self._disc(2)
        o = self._orch(every, set(every), delete_folder=False)
        self._run(o)
        self.assertEqual(o.cued, [None])
        self.assertTrue(self.folder.exists())
        o = self._orch([], set(), delete_folder=True)
        o._finish_handoff_source(
            None, self.folder, every, artist_name="Artist",
            album_name="Album", artist_id=5, expected_tracks=4,
            context="t", reason="t")
        self.assertFalse(self.folder.exists())


class _Held:
    def __init__(self, paths=()):
        self.items = {str(p): "failed" for p in paths}

    def add(self, source_path, **kw):
        self.items[source_path] = kw.get("outcome")

    def remove_by_path(self, p):
        return self.items.pop(str(p), None) is not None


class DiscCuesAreOneAlbum(unittest.TestCase):
    """orch2 F3 / loops F6: every disc .cue of a multi-disc album resolves to
    the album root. One hand-off per pass, one outcome and one attempt count
    for every disc .cue, one held entry at the root, and the album's clean-up
    is the root and all its disc .cues."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.cfgdir = base / "config"
        self.cfgdir.mkdir()
        self.dl = base / "downloads"
        self.album = self.dl / "Artist - Box (4CD)"
        self.cues = []
        for n in range(1, 5):
            d = self.album / f"CD{n:02d}"
            d.mkdir(parents=True)
            for t in ("01.flac", "02.flac"):
                (d / t).write_bytes(b"x")
            c = d / f"Box (CD{n:02d}).cue"
            c.write_text("FILE x", encoding="utf-8")
            self.cues.append(c)

    def tearDown(self):
        self._tmp.cleanup()

    def _orch(self, outcome="skipped_unmonitored", held=()):
        import threading
        from types import SimpleNamespace
        import orchestrator as O
        o = O.Orchestrator.__new__(O.Orchestrator)
        o.cfg = SimpleNamespace(
            watch_root=self.dl, wait_for_lidarr=False,
            ledger_file=self.cfgdir / "ledger.csv", cue_ledger_enabled=True,
            cue_ledger_file=self.cfgdir / "cue_seen.json",
            cue_ledger_max_attempts=3, cue_ledger_ttl_days=90,
            delete_cue_if_pre_split=True, delete_source_folder_on_success=False)
        o._skip_seen = O._SeenSet(86400)
        o._tl, o._ledger_lock = threading.local(), threading.Lock()
        o._repair_temps = set()
        o.lidarr = SimpleNamespace(failure_generation=0)
        o.held = _Held(held)
        o.handoffs = []
        o._wait_for_stability = lambda c: True
        o._looks_pre_split = lambda f: True
        o._cue_is_single_image = lambda c: False
        o._add_held_one = lambda folder, oc, reason, artist="", album="": \
            o.held.add(str(folder), outcome=oc)

        def handoff(cue, folder, reason=""):
            o.handoffs.append((cue, folder))
            o._skip_seen.add(cue)                  # as _handoff_inner does
            o._record(cue, outcome=outcome, pre_split=True, reason="t")
        o._handoff_pre_split_to_lidarr = handoff
        return o

    def _pass(self, o):
        for c in self.cues:
            o.process(c)
        return o

    def _ledger(self):
        import json
        led = json.loads((self.cfgdir / "cue_seen.json").read_text("utf-8"))
        return led["cues"]

    def test_a_disc_cue_resolves_to_its_album(self):
        import orchestrator as O
        o = self._orch()
        self.assertEqual(o._multidisc_cue_unit(self.cues[2]),
                         (self.album, self.cues))
        loose = self.dl / "Single" / "x.cue"
        loose.parent.mkdir()
        loose.write_text("x", encoding="utf-8")
        self.assertIsNone(o._multidisc_cue_unit(loose))
        for n in (1, 2):                  # CD1/CD2 loose in the watch root
            (self.dl / f"CD{n}").mkdir()
        (self.dl / "CD1" / "a.cue").write_text("x", encoding="utf-8")
        self.assertIsNone(o._multidisc_cue_unit(self.dl / "CD1" / "a.cue"))
        seen = []
        real = O.claims.claim
        with mock.patch("orchestrator.claims.claim",
                        side_effect=lambda p, wait=False: seen.append(Path(p))
                        or real(p, wait)):
            o.process(self.cues[1])
        self.assertEqual(seen[0], self.album)   # the job holds the album
        self.assertNotIn(O.claims._norm(self.album), O.claims._held)

    def test_one_handoff_per_pass_and_one_count(self):
        for n in (1, 2, 3):                     # three restarts
            o = self._pass(self._orch())
            self.assertEqual(o.handoffs, [(self.cues[0], self.album)])
            led = self._ledger()
            self.assertEqual(
                {tuple(led[str(c)][2:]) for c in self.cues},
                {("skipped_unmonitored", n, str(self.album))})
        o = self._pass(self._orch())            # the ALBUM gave up after 3
        self.assertEqual(o.handoffs, [])

    def test_the_album_is_one_held_entry(self):
        discs = [c.parent for c in self.cues]
        o = self._pass(self._orch(held=discs))  # the per-disc leftovers
        self.assertEqual(o.held.items, {str(self.album): "skipped_unmonitored"})
        o = self._pass(self._orch(outcome="imported_via_manual"))
        self.assertEqual(o.held.items, {})

    def test_a_changed_cue_is_done_again(self):
        self._pass(self._orch(outcome="imported_via_manual"))
        self.assertEqual(self._pass(self._orch()).handoffs, [])
        self.cues[2].write_text("FILE x\nREM edited", encoding="utf-8")
        o = self._pass(self._orch())
        self.assertEqual(o.handoffs, [(self.cues[2], self.album)])

    def test_a_single_image_disc_is_split_on_its_own(self):
        o = self._orch()
        o._cue_is_single_image = lambda c: True
        split = []
        o._find_companion_candidates = lambda c: split.append(c) or []
        self._pass(o)
        self.assertEqual(split, self.cues)
        self.assertEqual([f for _, f in o.handoffs],
                         [c.parent for c in self.cues])
        self.assertTrue(all(len(v) == 4 for v in self._ledger().values()))

    def test_the_album_cleanup_is_the_root_and_every_disc_cue(self):
        for f in self.album.rglob("*.flac"):   # Lidarr took every disc
            f.unlink()
        o = self._orch()
        o._tl.unit = (self.cues[0], self.album, self.cues)
        o._finish_handoff_source(
            self.cues[0], self.album, [], artist_name="A", album_name="B",
            artist_id=1, expected_tracks=8, context="t", reason="t")
        self.assertEqual([c for c in self.cues if c.exists()], [])
        o.cfg.delete_source_folder_on_success = True
        gone = []
        o._delete_source_folder = lambda s, **k: gone.append(s.parent)
        o._finish_handoff_source(
            self.cues[0], self.album, [], artist_name="A", album_name="B",
            artist_id=1, expected_tracks=8, context="t", reason="t")
        self.assertEqual(gone, [self.album])


class PreferLosslessPerDiscFolder(unittest.TestCase):
    """orch2 F11: a song is a file name in ONE folder. Grouped per folder,
    the lossy twin is quarantined in its own folder, and every folder that
    changed is rescanned. Nothing is deleted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.album = Path(self._tmp.name) / "Artist" / "Album (2CD)"
        for d, names in (("CD1", ("01 - Intro.flac", "02 - Ветрове.mp3",
                                  "02 - Другая.flac")),
                         ("CD2", ("01 - Intro.mp3", "02 - Song.flac",
                                  "02 - Song.mp3"))):
            (self.album / d).mkdir(parents=True)
            for n in names:
                (self.album / d / n).write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_folder_one_song(self):
        from types import SimpleNamespace
        import orchestrator as O
        o = O.Orchestrator.__new__(O.Orchestrator)
        o.cfg = SimpleNamespace(prefer_lossless_over_lossy=True)
        rescanned = []
        o.lidarr = SimpleNamespace(windows_to_lidarr=str,
                                   rescan_folder=rescanned.append)
        audios = sorted(p for p in self.album.rglob("*") if p.is_file())
        self.assertEqual(o._prefer_lossless_in_album({"id": 7}, 1, audios), 1)
        q = O.Orchestrator.QUARANTINE_DIR
        self.assertEqual(
            sorted(str(p.relative_to(self.album)).replace("\\", "/")
                   for p in self.album.rglob("*") if p.is_file()),
            ["CD1/01 - Intro.flac", "CD1/02 - Ветрове.mp3", "CD1/02 - Другая.flac",
             "CD2/01 - Intro.mp3", "CD2/02 - Song.flac", f"CD2/{q}/02 - Song.mp3"])
        self.assertEqual(rescanned, [str(self.album / "CD2")])


if __name__ == "__main__":
    unittest.main()
