"""A download is thrown away only when Lidarr rejects EVERY file as 'not an
upgrade', and throwing it away never takes a sibling album with it."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import Orchestrator  # noqa: E402

NOT_UPGRADE = {"reason": "Not an upgrade for existing track file(s)"}


class FakeLidarr:
    def __init__(self, cands, queue=()):
        self.cands, self.queue, self.removed = cands, list(queue), []

    def find_artist(self, name):
        return {"id": 7}

    def windows_to_lidarr(self, p):
        return str(p)

    def manual_import_candidates(self, path, artist_id=None):
        return self.cands

    def queue_list(self):
        return self.queue

    def queue_remove(self, qid, remove_from_client=False, blocklist=False):
        self.removed.append((qid, remove_from_client, blocklist))
        return True


class FakeQbt:
    def __init__(self, torrents, files=()):
        self.t, self.f, self.prio, self.gone = torrents, list(files), [], []

    def torrents(self):
        return self.t

    def files(self, h):
        return self.f

    def set_file_priority(self, h, idx, p):
        self.prio.append((h, sorted(idx), p))
        return True

    def remove(self, h, delete_files=False):
        self.gone.append((h, delete_files))
        return True

    def lookup(self, h):
        return True, next((t for t in self.t
                           if str(t.get("hash")).lower() == str(h).lower()), None)


def orch(root, lidarr, qbt, delete_folder=False, delete_originals=False):
    s = SimpleNamespace(
        lidarr=lidarr, cfg=SimpleNamespace(
            watch_root=root, delete_source_folder_on_success=delete_folder,
            delete_originals_on_success=delete_originals, qbt_category="lidarr"),
        deleted=[], orphan=[])
    s._get_qbt = lambda: qbt
    s._qbt_ours = lambda t: t.get("category") == "lidarr"
    s._qbt_ours_by_hash = lambda q, h: True
    s._delete_source_folder = lambda sentinel, **kw: s.deleted.append(sentinel.parent)
    s._delete_orphan_cue = lambda cue, reason: s.orphan.append(cue)
    s._reap_torrent = lambda h, blocklist=True: s.deleted.append(("reap", h))
    s._lidarr_generation = lambda: 0
    for name in ("_folder_fully_owned", "_our_torrent_for_folder",
                 "_dispose_redundant_download", "_deselect_album_in_torrent",
                 "_remove_torrent"):
        setattr(s, name, getattr(Orchestrator, name).__get__(s))
    return s


class Owned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.album = self.root / "Album"
        self.album.mkdir()
        self.files = []
        for n in ("01.flac", "02.flac"):
            (self.album / n).write_bytes(b"x")
            self.files.append(self.album / n)

    def tearDown(self):
        self.tmp.cleanup()

    def cands(self, *rej):
        return [{"path": str(f), "tracks": [{"id": i}], "rejections": r}
                for i, (f, r) in enumerate(zip(self.files, rej))]

    def test_every_file_not_an_upgrade_is_owned(self):
        o = orch(self.root, FakeLidarr(self.cands([NOT_UPGRADE], [NOT_UPGRADE])), None)
        self.assertTrue(o._folder_fully_owned(self.album, "A", self.files)[0])

    def test_an_upgrade_or_an_unknown_song_keeps_the_download(self):
        o = orch(self.root, FakeLidarr(self.cands([NOT_UPGRADE], [])), None)
        self.assertFalse(o._folder_fully_owned(self.album, "A", self.files)[0])
        o = orch(self.root, FakeLidarr(self.cands([NOT_UPGRADE])), None)
        owned, why = o._folder_fully_owned(self.album, "A", self.files)
        self.assertFalse(owned)
        self.assertIn("02.flac", why)

    def test_lidarr_failing_keeps_the_download(self):
        o = orch(self.root, FakeLidarr([]), None)
        self.assertFalse(o._folder_fully_owned(self.album, "A", self.files)[0])

    def test_partial_download_and_vanished_folder(self):
        o = orch(self.root, FakeLidarr(self.cands([NOT_UPGRADE], [NOT_UPGRADE])), None)
        (self.album / "03.flac.part").write_bytes(b"")
        self.assertFalse(o._folder_fully_owned(self.album, "A", self.files)[0])
        self.assertIsNone(o._folder_fully_owned(self.root / "gone", "A", [])[0])

    def test_album_inside_a_discography_is_deselected_never_blocklisted(self):
        disco = self.root / "Disco"
        leaf = disco / "Vol 1"
        leaf.mkdir(parents=True)
        t = {"hash": "H", "category": "lidarr", "content_path": str(disco),
             "save_path": str(self.root), "name": "Disco"}
        q = FakeQbt([t], [{"index": 0, "name": "Disco/Vol 1/01.flac", "priority": 1},
                          {"index": 1, "name": "Disco/Vol 2/01.flac", "priority": 1}])
        lid = FakeLidarr([], queue=[{"id": 5, "downloadId": "H", "title": "Disco"}])
        o = orch(self.root, lid, q, delete_folder=True)
        o._dispose_redundant_download(leaf, None, [], "A", "Vol 1", "r")
        self.assertEqual(lid.removed, [])
        self.assertEqual(q.gone, [])
        self.assertEqual(q.prio, [("H", [0], 0)])
        self.assertEqual(o.deleted, [leaf])

    def test_single_album_torrent_is_blocklisted_by_hash_and_flags_are_obeyed(self):
        t = {"hash": "H", "category": "lidarr", "content_path": str(self.album),
             "save_path": str(self.root), "name": "Album"}
        queue = [{"id": 4, "downloadId": "OTHER", "title": "Album"},
                 {"id": 5, "downloadId": "h", "title": "whatever"}]
        lid = FakeLidarr([], queue=queue)
        o = orch(self.root, lid, FakeQbt([t]))
        o._dispose_redundant_download(self.album, None, self.files, "A", "Album", "r")
        self.assertEqual(lid.removed, [(5, False, True)])   # no delete flags: data stays
        self.assertEqual(o.deleted, [])
        # Deleting: blocklisted by its row, but taken out of the client by us
        # (category + claims checked), never by Lidarr's unchecked delete.
        lid, q = FakeLidarr([], queue=queue), FakeQbt([t])
        o = orch(self.root, lid, q, delete_folder=True)
        o._dispose_redundant_download(self.album, None, self.files, "A", "Album", "r")
        self.assertEqual((lid.removed, q.gone), ([(5, False, True)], [("h", True)]))

    def test_other_categories_are_never_touched(self):
        t = {"hash": "H", "category": "movies", "content_path": str(self.album),
             "save_path": str(self.root), "name": "Album"}
        lid = FakeLidarr([], queue=[{"id": 5, "downloadId": "H"}])
        q = FakeQbt([t])
        o = orch(self.root, lid, q, delete_folder=True)
        o._dispose_redundant_download(self.album, None, self.files, "A", "Album", "r")
        self.assertEqual((lid.removed, q.gone), ([], []))


class AtomicEncode(unittest.TestCase):
    """A .flac only ever appears complete (real ffmpeg, in the image)."""
    def stub(self):
        s = SimpleNamespace(cfg=SimpleNamespace(ffmpeg_binary="ffmpeg"))
        s._PARTIAL = Orchestrator._PARTIAL
        s._encode_flac = Orchestrator._encode_flac.__get__(s)
        s._clear_partials = Orchestrator._clear_partials
        return s

    def test_published_only_when_verified(self):
        import shutil
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed here")
        o = self.stub()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "01.flac"
            self.assertEqual(o._encode_flac(["-f", "lavfi", "-i", "sine=d=2"], out, "t"), out)
            self.assertTrue(out.exists())
            self.assertFalse((Path(d) / "01.flac.partial").exists())
            bad = Path(d) / "02.flac"
            self.assertIsNone(o._encode_flac(["-i", str(Path(d) / "missing.wav")], bad, "t"))
            self.assertFalse(bad.exists() or (Path(d) / "02.flac.partial").exists())
            staged = o._encode_flac(["-f", "lavfi", "-i", "sine=d=2"], Path(d) / "03.flac",
                                    "t", publish=False)
            self.assertEqual(staged.name, "03.flac.partial")
            self.assertFalse((Path(d) / "03.flac").exists())
            Orchestrator._clear_partials(Path(d))
            self.assertFalse(staged.exists())


class ReleaseSwitch(unittest.TestCase):
    def stub(self, held):
        calls = []
        lid = SimpleNamespace(
            list_tracks_for_album=lambda aid: held,
            set_album_monitored_release=lambda aid, rid: calls.append(rid) or True)
        s = SimpleNamespace(lidarr=lid)
        s._release_switch_safe = Orchestrator._release_switch_safe.__get__(s)
        s._force_abort = Orchestrator._force_abort.__get__(s)
        return s, calls

    def test_an_album_with_files_is_not_repointed_away_from_them(self):
        rows = [{"foreignRecordingId": r} for r in ("a", "b", "c")]
        s, _ = self.stub([{"hasFile": True, "foreignRecordingId": "a"},
                          {"hasFile": True, "foreignRecordingId": "z"}])
        full = {"statistics": {"trackFileCount": 2}}
        self.assertFalse(s._release_switch_safe(full, 1, 9, rows))
        s, _ = self.stub([{"hasFile": True, "foreignRecordingId": "a"},
                          {"hasFile": False, "foreignRecordingId": "z"}])
        self.assertTrue(s._release_switch_safe(full, 1, 9, rows))
        self.assertTrue(s._release_switch_safe({"statistics": {}}, 1, 9, []))

    def test_abort_restores_the_previous_release(self):
        s, calls = self.stub([])
        self.assertFalse(s._force_abort(1, 7, True))
        self.assertFalse(s._force_abort(1, 7, False))
        self.assertEqual(calls, [7])


class FolderIdentity(unittest.TestCase):
    def stub(self, known=()):
        s = SimpleNamespace(lidarr=SimpleNamespace(
            find_artist=lambda n: {"artistName": n} if n in known else None))
        s._DISC_SUBDIR_RE = Orchestrator._DISC_SUBDIR_RE
        for n in ("_album_folder_identity", "_is_lidarr_artist"):
            setattr(s, n, getattr(Orchestrator, n).__get__(s))
        return s

    def test_a_year_is_never_the_artist(self):
        s = self.stub()
        f = Path("/d/Cyndi Lauper - Discography [FLAC]/2011 - To Memphis With Love [A 1]")
        self.assertEqual(s._album_folder_identity(f), ("Cyndi Lauper", "To Memphis With Love"))
        s = self.stub(known=("Blue Stahli",))
        f = Path("/d/Blue Stahli/2012 - Antisleep Vol. 2")
        self.assertEqual(s._album_folder_identity(f), ("Blue Stahli", "Antisleep Vol. 2"))
        s = self.stub()
        self.assertEqual(s._album_folder_identity(Path("/d/Stuff/1998 - Origin")), ("", "Origin"))
        self.assertEqual(s._album_folder_identity(Path("/d/Cher - Believe")), ("Cher", "Believe"))


if __name__ == "__main__":
    unittest.main()
