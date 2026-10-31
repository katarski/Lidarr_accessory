"""Torrents the owner added by hand stay as they are (handadded.py).

The owner, 31 Oct 2026: "if it hand added - it stays"; "if it found a better
torrent than the hand added one - we keep the better one!" A torrent with no
category and no proof that the pipeline grabbed it is hand-added: its folder is
kept out of every pass that splits, moves or deletes; a worse one is removed
only once Lidarr holds every song of it in better quality, and a better one
(or music Lidarr lacks) is imported as a COPY."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handadded  # noqa: E402
import qbt_deselect as QD  # noqa: E402
from lidarr import LidarrClient  # noqa: E402
from qbittorrent_client import QbtClient  # noqa: E402

WR = "/downloads"


def _t(h="abc", name="Album", cp="/data/Album", sp="/data", cat="", tags=""):
    return {"hash": h, "name": name, "content_path": cp, "save_path": sp,
            "category": cat, "tags": tags, "added_on": 1}


class _Clean(unittest.TestCase):
    def setUp(self):
        handadded.set_state({})

    def tearDown(self):
        handadded.set_state({})


class Protected(_Clean):
    def test_exact_nested_and_prefix_sibling(self):
        handadded.set_state({"h": {"roots": ["/downloads/Abc"]}})
        self.assertEqual(handadded.protected("/downloads/Abc"), "/downloads/Abc")
        self.assertEqual(handadded.protected("/downloads/Abc/CD1/01.flac"),
                         "/downloads/Abc")
        self.assertIsNone(handadded.protected("/downloads/Abcd"))
        self.assertIsNone(handadded.protected("/downloads/Abcd/01.flac"))
        self.assertIsNone(handadded.protected("/downloads"))
        self.assertIsNone(handadded.protected("/downloads/Ab"))

    def test_none_empty_and_no_state(self):
        self.assertIsNone(handadded.protected("/downloads/Abc"))
        handadded.set_state({"h": {"roots": ["/downloads/Abc"]}})
        self.assertIsNone(handadded.protected(None))
        self.assertIsNone(handadded.protected(""))
        self.assertIsNone(handadded.protected("  "))

    def test_paths_are_compared_normalised(self):
        handadded.set_state({"h": {"roots": ["/downloads/Abc/"]}})
        self.assertEqual(handadded.protected(Path("/downloads/Abc/x.cue")),
                         "/downloads/Abc")
        self.assertTrue(handadded.protected("/downloads/Abc/"))
        self.assertTrue(handadded.protected("/downloads/./Abc//CD1/../x.flac"))
        self.assertTrue(handadded.protected("\\downloads\\Abc\\x.flac"))
        self.assertIsNone(handadded.protected("/downloads/x/../Abcd"))

    def test_the_filesystem_root_is_never_a_root(self):
        handadded.set_state({"h": {"roots": ["/", ".", ""]}})
        self.assertIsNone(handadded.protected("/downloads/Abc"))
        self.assertEqual(handadded.torrents()["h"]["roots"], ["/", ".", ""])

    def test_forget_drops_one_torrent_and_its_folder(self):
        handadded.set_state({"a": {"roots": ["/downloads/A"]},
                             "b": {"roots": ["/downloads/B"]}})
        handadded.forget("A")
        self.assertIsNone(handadded.protected("/downloads/A/x"))
        self.assertTrue(handadded.protected("/downloads/B/x"))
        self.assertEqual(list(handadded.torrents()), ["b"])
        handadded.forget("nothing")
        self.assertEqual(list(handadded.torrents()), ["b"])


class RootsFor(unittest.TestCase):
    def test_content_under_the_save_path(self):
        self.assertEqual(handadded.roots_for(_t(), WR), ["/downloads/Album"])

    def test_a_nested_content_path_is_mapped_two_ways(self):
        t = _t(cp="/data/torrents/x.dmg", sp="/data")
        self.assertEqual(handadded.roots_for(t, WR),
                         ["/downloads/torrents/x.dmg", "/downloads/x.dmg"])

    def test_content_outside_the_save_path_maps_by_name(self):
        self.assertEqual(handadded.roots_for(_t(cp="/elsewhere/Album", sp="/data"), WR),
                         ["/downloads/Album"])
        self.assertEqual(handadded.roots_for(_t(cp="/data/Album", sp=""), WR),
                         ["/downloads/Album"])

    def test_no_root_folder_uses_the_first_component_of_each_file(self):
        t = _t(cp="/data", sp="/data")
        files = [{"name": "a.flac"}, {"name": "CD2/b.flac"},
                 {"name": "CD2\\c.flac"}, {"name": "a.flac"}]
        self.assertEqual(handadded.roots_for(t, WR, files),
                         ["/downloads/a.flac", "/downloads/CD2"])
        self.assertEqual(handadded.roots_for(_t(cp="", sp="/data"), WR, files),
                         ["/downloads/a.flac", "/downloads/CD2"])
        self.assertEqual(handadded.roots_for(t, WR, None), [])
        self.assertEqual(handadded.roots_for(t, WR, [{"name": ""}, {}]), [])

    def test_slashes_and_the_watch_root(self):
        t = _t(cp="D:\\Torrents\\Album\\", sp="D:\\Torrents")
        self.assertEqual(handadded.roots_for(t, "/downloads/"),
                         ["/downloads/Album"])
        self.assertEqual(handadded.roots_for(_t(), ""), [])
        self.assertEqual(handadded.roots_for(_t(), "/"), ["/Album"])


class Classify(_Clean):
    def test_proof_decides(self):
        ts = [_t("none", "N", "/data/N"), _t("unk", "U", "/data/U"),
              _t("yes", "Y", "/data/Y")]
        answers = {"none": False, "unk": None, "yes": True}
        out = handadded.classify(ts, lambda t: answers[t["hash"]], WR)
        self.assertEqual(sorted(out), ["none", "unk"])
        self.assertEqual(out["none"]["proof"], "none")
        self.assertEqual(out["unk"]["proof"], "unknown")
        self.assertEqual(out["none"]["roots"], ["/downloads/N"])
        self.assertEqual(out["none"]["name"], "N")

    def test_a_proof_that_raises_fails_closed(self):
        def boom(t):
            raise RuntimeError("lidarr down")
        out = handadded.classify([_t()], boom, WR)
        self.assertEqual(out["abc"]["proof"], "unknown")

    def test_category_and_own_tag_are_skipped_without_asking(self):
        asked = []

        def proof(t):
            asked.append(t["hash"])
            return False
        ts = [_t("cat", cat="lidarr"), _t("own", tags="x, " + QbtClient.SELF_ADDED_TAG),
              _t("blank", cat="   "), {"name": "no hash", "content_path": "/data/n"}]
        out = handadded.classify(ts, proof, WR)
        self.assertEqual(sorted(out), ["blank"])
        self.assertEqual(asked, ["blank"])

    def test_the_file_list_is_asked_only_for_a_torrent_without_a_root_folder(self):
        asked = []

        def files_of(h):
            asked.append(h)
            return [{"name": "CD1/a.flac"}]
        ts = [_t("rooted"), _t("flat", cp="/data", sp="/data")]
        out = handadded.classify(ts, lambda t: False, WR, files_of=files_of)
        self.assertEqual(asked, ["flat"])
        self.assertEqual(out["flat"]["roots"], ["/downloads/CD1"])

    def test_a_torrent_that_maps_nowhere_is_skipped(self):
        out = handadded.classify([_t("flat", cp="/data", sp="/data")],
                                 lambda t: False, WR, files_of=lambda h: [])
        self.assertEqual(out, {})

    def test_the_hash_key_is_lowercase(self):
        out = handadded.classify([_t("ABCDEF")], lambda t: False, WR)
        self.assertEqual(list(out), ["abcdef"])


class _Qbt:
    def __init__(self, torrents):
        self.t = list(torrents)

    def torrents(self, category=""):
        return list(self.t)

    def files(self, h):
        return []


class Refresh(_Clean):
    def test_an_empty_answer_keeps_the_state(self):
        handadded.set_state({"k": {"roots": ["/downloads/K"]}})
        self.assertIsNone(handadded.refresh(_Qbt([]), lambda t: False, WR))
        self.assertEqual(list(handadded.torrents()), ["k"])
        self.assertTrue(handadded.protected("/downloads/K/x"))

    def test_a_new_hand_added_torrent_is_said_once(self):
        said = []
        q = _Qbt([_t()])
        self.assertEqual(handadded.refresh(q, lambda t: False, WR,
                                           emit=said.append), 1)
        self.assertEqual(len(said), 1)
        self.assertIn("'Album'", said[0])
        self.assertIn("it stays as it is", said[0])
        self.assertIn("no grab record", said[0])
        handadded.refresh(q, lambda t: False, WR, emit=said.append)
        self.assertEqual(len(said), 1)
        handadded.set_state({})
        handadded.refresh(q, lambda t: None, WR, emit=said.append)
        self.assertEqual(len(said), 2)
        self.assertIn("Lidarr could not be asked yet", said[1])

    def test_a_torrent_that_got_a_category_or_went_drops_out(self):
        q = _Qbt([_t("a", "A", "/data/A"), _t("b", "B", "/data/B")])
        handadded.refresh(q, lambda t: False, WR, emit=lambda s: None)
        self.assertEqual(sorted(handadded.torrents()), ["a", "b"])
        q.t = [_t("a", "A", "/data/A", cat="lidarr")]
        self.assertEqual(handadded.refresh(q, lambda t: False, WR,
                                           emit=lambda s: None), 0)
        self.assertEqual(handadded.torrents(), {})
        self.assertIsNone(handadded.protected("/downloads/A/x"))

    def test_the_set_is_saved_when_it_changes_and_loaded_at_startup(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "handadded.json"
            q = _Qbt([_t()])
            with mock.patch.object(handadded, "save",
                                   wraps=handadded.save) as save:
                handadded.refresh(q, lambda t: False, WR, path=path,
                                  emit=lambda s: None)
                handadded.refresh(q, lambda t: False, WR, path=path,
                                  emit=lambda s: None)
                self.assertEqual(save.call_count, 1)
            self.assertEqual(sorted(json.loads(path.read_text())["torrents"]),
                             ["abc"])
            self.assertFalse(Path(str(path) + ".tmp").exists())
            handadded.set_state({})
            self.assertEqual(handadded.load(path), 1)
            self.assertEqual(handadded.protected("/downloads/Album/x.flac"),
                             "/downloads/Album")

    def test_load_keeps_the_state_on_a_bad_or_missing_file(self):
        handadded.set_state({"k": {"roots": ["/downloads/K"]}})
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(handadded.load(Path(d) / "nothing.json"), 0)
            bad = Path(d) / "bad.json"
            bad.write_text("{not json")
            self.assertEqual(handadded.load(bad), 0)
            bad.write_text(json.dumps({"torrents": ["list"]}))
            self.assertEqual(handadded.load(bad), 0)
        self.assertEqual(list(handadded.torrents()), ["k"])


class _Lidarr:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def grabbed_download(self, h):
        self.asked.append(h)
        return self.answer


class MakeProof(unittest.TestCase):
    def test_the_pipelines_own_tag(self):
        lid = _Lidarr(False)
        proof = handadded.make_proof(lid)
        self.assertIs(proof({"hash": "A", "tags": "x, " + QbtClient.SELF_ADDED_TAG}),
                      True)
        self.assertEqual(lid.asked, [])

    def test_the_grab_record(self):
        lid = _Lidarr(False)
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "grab_targets.json"
            rec.write_text(json.dumps({"abc123": {"album_id": 1}}))
            proof = handadded.make_proof(lid, rec)
            self.assertIs(proof({"hash": "ABC123", "tags": ""}), True)
            self.assertEqual(lid.asked, [])
            self.assertIs(proof({"hash": "def456", "tags": ""}), False)
            self.assertEqual(lid.asked, ["def456"])
            # The record changes: the new hash is seen without a restart.
            rec.write_text(json.dumps({"abc123": {}, "def456": {"x": 1}}))
            os.utime(rec, (1, 12345))
            self.assertIs(proof({"hash": "def456", "tags": ""}), True)

    def test_a_missing_or_unreadable_record_falls_back_to_lidarr(self):
        with tempfile.TemporaryDirectory() as d:
            for content in (None, "{not json", json.dumps(["abc"])):
                lid = _Lidarr(True)
                rec = Path(d) / "r.json"
                if content is None:
                    if rec.exists():
                        rec.unlink()
                else:
                    rec.write_text(content)
                proof = handadded.make_proof(lid, rec)
                self.assertIs(proof({"hash": "abc", "tags": ""}), True)
                self.assertEqual(lid.asked, ["abc"])

    def test_lidarr_true_is_kept_for_good_false_for_the_ttl(self):
        clock = [0.0]
        lid = _Lidarr(False)
        proof = handadded.make_proof(lid, None, ttl_negative=100,
                                     now=lambda: clock[0])
        t = {"hash": "ABC", "tags": ""}
        self.assertIs(proof(t), False)
        clock[0] = 99
        self.assertIs(proof(t), False)
        self.assertEqual(lid.asked, ["abc"])          # asked in lowercase, once
        clock[0] = 101
        lid.answer = True
        self.assertIs(proof(t), True)
        self.assertEqual(len(lid.asked), 2)
        clock[0] = 10 ** 9
        lid.answer = False
        self.assertIs(proof(t), True)                 # not asked again
        self.assertEqual(len(lid.asked), 2)

    def test_none_is_never_cached(self):
        lid = _Lidarr(None)
        proof = handadded.make_proof(lid)
        t = {"hash": "abc", "tags": ""}
        self.assertIsNone(proof(t))
        self.assertIsNone(proof(t))
        self.assertEqual(len(lid.asked), 2)
        lid.answer = False
        self.assertIs(proof(t), False)

    def test_a_lidarr_that_raises_is_none_and_no_hash_is_none(self):
        class Boom:
            def grabbed_download(self, h):
                raise RuntimeError("down")
        proof = handadded.make_proof(Boom())
        self.assertIsNone(proof({"hash": "abc", "tags": ""}))
        self.assertIsNone(proof({"tags": ""}))
        self.assertIsNone(proof({"hash": "", "tags": ""}))


def _cand(path, qid, tfids=(11,), rej=("Not an upgrade for existing track "
                                       "file(s). Existing quality: FLAC.",)):
    return {"path": path, "quality": {"quality": {"id": qid, "name": "q"}},
            "rejections": [{"reason": r} for r in rej],
            "tracks": [{"id": 1000 + i, "trackFileId": f, "hasFile": bool(f)}
                       for i, f in enumerate(tfids)]}


# quality 1 is the worst, 2 and 3 share a group, 4 is the best
_PROFILE = {1: 0, 2: 1, 3: 1, 4: 2}


class StrictlyBetter(unittest.TestCase):
    def check(self, cands, paths, tfq, profile=None):
        return handadded.library_strictly_better(
            cands, paths, tfq, _PROFILE if profile is None else profile)

    def test_every_library_file_in_a_higher_group(self):
        ok, why = self.check([_cand("/m/a.mp3", 1), _cand("/m/b.mp3", 2, (12,))],
                             ["/m/a.mp3", "/m/b.mp3"], {11: 2, 12: 4})
        self.assertTrue(ok)
        self.assertIn("all 2 song(s)", why)

    def test_an_equal_group_is_not_better(self):
        ok, _ = self.check([_cand("/m/a.mp3", 2)], ["/m/a.mp3"], {11: 3})
        self.assertFalse(ok)
        ok, _ = self.check([_cand("/m/a.mp3", 4)], ["/m/a.mp3"], {11: 1})
        self.assertFalse(ok)
        ok, _ = self.check([_cand("/m/a.mp3", 4)], ["/m/a.mp3"], {11: 4})
        self.assertFalse(ok)

    def test_one_worse_track_among_several_is_not_better(self):
        c = _cand("/m/a.mp3", 2, (11, 12))
        self.assertTrue(self.check([c], ["/m/a.mp3"], {11: 4, 12: 4})[0])
        self.assertFalse(self.check([c], ["/m/a.mp3"], {11: 4, 12: 3})[0])

    def test_anything_missing_is_never_better(self):
        good = [_cand("/m/a.mp3", 1)]
        self.assertTrue(self.check(good, ["/m/a.mp3"], {11: 4})[0])
        # no audio, an empty profile, an unmatched file, no tracks
        self.assertFalse(self.check(good, [], {11: 4})[0])
        self.assertFalse(self.check(good, ["/m/a.mp3"], {11: 4}, {})[0])
        self.assertFalse(self.check(good, ["/m/a.mp3", "/m/b.mp3"], {11: 4})[0])
        self.assertFalse(self.check([], ["/m/a.mp3"], {11: 4})[0])
        no_tracks = [dict(_cand("/m/a.mp3", 1), tracks=[])]
        self.assertFalse(self.check(no_tracks, ["/m/a.mp3"], {11: 4})[0])
        # a track without a library file (id 0 or absent)
        self.assertFalse(self.check([_cand("/m/a.mp3", 1, (0,))],
                                    ["/m/a.mp3"], {0: 4})[0])
        no_id = [_cand("/m/a.mp3", 1)]
        del no_id[0]["tracks"][0]["trackFileId"]
        self.assertFalse(self.check(no_id, ["/m/a.mp3"], {11: 4})[0])
        # the library file's quality unknown, or not in the profile
        self.assertFalse(self.check(good, ["/m/a.mp3"], {})[0])
        self.assertFalse(self.check(good, ["/m/a.mp3"], {11: 99})[0])
        # the hand-added file's quality missing, or not in the profile
        self.assertFalse(self.check([_cand("/m/a.mp3", 99)], ["/m/a.mp3"], {11: 4})[0])
        self.assertFalse(self.check([dict(_cand("/m/a.mp3", 1), quality=None)],
                                    ["/m/a.mp3"], {11: 4})[0])

    def test_a_file_lidarr_would_import_is_not_owned(self):
        accepted = [_cand("/m/a.mp3", 1, rej=())]
        ok, why = self.check(accepted, ["/m/a.mp3"], {11: 4})
        self.assertFalse(ok)
        self.assertIn("would be imported", why)
        other = [_cand("/m/a.mp3", 1, rej=("Has missing tracks",))]
        self.assertFalse(self.check(other, ["/m/a.mp3"], {11: 4})[0])
        # the verdict is read case-blind and the marker can be passed in
        loud = [_cand("/m/a.mp3", 1, rej=("NOT AN UPGRADE for the file",))]
        self.assertTrue(self.check(loud, ["/m/a.mp3"], {11: 4})[0])
        self.assertTrue(handadded.library_strictly_better(
            [_cand("/m/a.mp3", 1, rej=("Kein Upgrade",))], ["/m/a.mp3"],
            {11: 4}, _PROFILE, not_an_upgrade="kein upgrade")[0])


class _Files(object):
    """qBittorrent with the files each torrent holds."""

    def __init__(self, torrents, files_by_hash):
        self._t, self._f = list(torrents), files_by_hash
        self.categorised = []

    def torrents(self, category=""):
        return list(self._t)

    def files(self, h):
        return self._f.get(h, [])

    def set_category(self, h, cat):
        self.categorised.append(h)
        return True


class Adopt(unittest.TestCase):
    def run_adopt(self, ts, files, proof):
        q = _Files(ts, files)
        QD.adopt_uncategorised(q, "lidarr", emit=lambda s: None, proof=proof)
        return q.categorised

    def test_a_proven_torrent_is_adopted_whatever_else_it_holds(self):
        ts = [{"hash": "p", "category": "", "tags": ""}]
        files = {"p": [{"name": "a.flac"}, {"name": "b.flac"}]
                 + [{"name": "x%d.bin" % i} for i in range(18)]}
        self.assertEqual(self.run_adopt(ts, files, lambda t: True), ["p"])

    def test_a_proven_torrent_without_audio_is_not_adopted(self):
        ts = [{"hash": "p", "category": "", "tags": ""}]
        files = {"p": [{"name": "a.mkv"}, {"name": "a.nfo"}]}
        self.assertEqual(self.run_adopt(ts, files, lambda t: True), [])

    def test_an_unproven_music_torrent_is_never_adopted(self):
        ts = [{"hash": "h", "category": "", "tags": ""}]
        files = {"h": [{"name": "CD/x.flac"}, {"name": "CD/x.cue"},
                       {"name": "CD/x.log"}, {"name": "cover.jpg"}]}
        for proof in (lambda t: False, lambda t: None, None):
            self.assertEqual(self.run_adopt(ts, files, proof), [])

        def boom(t):
            raise RuntimeError("lidarr down")
        self.assertEqual(self.run_adopt(ts, files, boom), [])

    def test_the_pipelines_own_torrent_keeps_the_old_test(self):
        ts = [{"hash": "mostly", "category": "", "tags": QbtClient.SELF_ADDED_TAG},
              {"hash": "little", "category": "", "tags": QbtClient.SELF_ADDED_TAG},
              {"hash": "none", "category": "", "tags": QbtClient.SELF_ADDED_TAG}]
        files = {"mostly": [{"name": "a.flac"}, {"name": "b.mkv"}],
                 "little": [{"name": "a.flac"}, {"name": "b.mkv"}, {"name": "c.mkv"}],
                 "none": [{"name": "a.mkv"}]}
        # proof is not consulted for it (and may even say no)
        self.assertEqual(self.run_adopt(ts, files, lambda t: False), ["mostly"])

    def test_a_torrent_with_a_category_is_left_alone(self):
        ts = [{"hash": "t", "category": "tv", "tags": ""}]
        self.assertEqual(self.run_adopt(ts, {"t": [{"name": "y.flac"}]},
                                        lambda t: True), [])


class ExcludedByMain(_Clean):
    def test_main_skips_a_hand_added_folder(self):
        import main
        handadded.set_state({"h": {"roots": ["/downloads/Abc"]}})
        self.assertTrue(main._is_excluded(Path("/downloads/Abc/CD1/x.cue"), []))
        self.assertTrue(main._is_excluded(Path("/downloads/Abc"), []))
        self.assertFalse(main._is_excluded(Path("/downloads/Abcd/x.cue"), []))
        handadded.set_state({})
        self.assertFalse(main._is_excluded(Path("/downloads/Abc/CD1/x.cue"), []))


class OrchestratorSkips(_Clean):
    """The orchestrator's own passes leave a hand-added folder as it is. The
    guards are the first statements, so the methods run without an object."""

    def test_process_skips_a_hand_added_cue(self):
        import orchestrator
        handadded.set_state({"h": {"roots": ["/downloads/Abc"]}})
        # Anything past the guard would need self; it returns before that.
        self.assertIsNone(orchestrator.Orchestrator.process(
            None, Path("/downloads/Abc/CD1/x.cue")))

    def test_the_originals_of_a_hand_added_folder_are_not_deleted(self):
        import orchestrator
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "Abc"
            root.mkdir()
            cue, img = root / "x.cue", root / "x.flac"
            cue.write_text("c")
            img.write_bytes(b"x")
            handadded.set_state({"h": {"roots": [str(root)]}})
            orchestrator.Orchestrator._delete_originals(None, cue, img)
            self.assertTrue(cue.exists() and img.exists())
            handadded.set_state({})
            orchestrator.Orchestrator._delete_originals(None, cue, img)
            self.assertFalse(cue.exists() or img.exists())


class LidarrHelpers(unittest.TestCase):
    """The three new Lidarr reads, against a canned answer."""

    def client(self, get):
        c = LidarrClient.__new__(LidarrClient)
        c._get = get
        return c

    def test_grabbed_download(self):
        seen = []

        def get(path, **params):
            seen.append((path, params))
            return {"totalRecords": 2, "records": []}
        c = self.client(get)
        self.assertIs(c.grabbed_download("abcdef"), True)
        self.assertEqual(seen, [("/api/v1/history", {
            "downloadId": "ABCDEF", "eventType": 1, "page": 1, "pageSize": 1})])
        self.assertIs(self.client(lambda p, **k: {"totalRecords": 0}).grabbed_download("a"),
                      False)
        self.assertIs(self.client(lambda p, **k: {}).grabbed_download("a"), False)
        self.assertIsNone(self.client(lambda p, **k: []).grabbed_download("a"))
        self.assertIsNone(self.client(
            lambda p, **k: {"totalRecords": "many"}).grabbed_download("a"))

        def boom(p, **k):
            raise OSError("down")
        self.assertIsNone(self.client(boom).grabbed_download("a"))

    def test_trackfile_qualities_in_chunks(self):
        calls = []

        def get(path, **params):
            calls.append((path, list(params["trackFileIds"])))
            return [{"id": i, "quality": {"quality": {"id": 4 if i % 2 else 2}}}
                    for i in params["trackFileIds"]] + [{"id": None}, {"id": 999}]
        c = self.client(get)
        ids = list(range(1, 251)) + [5, 0, -3, "x", None, "7"]
        out = c.trackfile_qualities(ids)
        self.assertEqual([len(ch) for _, ch in calls], [100, 100, 50])
        self.assertTrue(all(p == "/api/v1/trackfile" for p, _ in calls))
        self.assertEqual(len(out), 250)
        self.assertEqual((out[1], out[2]), (4, 2))
        self.assertEqual(c.trackfile_qualities([]), {})
        self.assertEqual(c.trackfile_qualities([0, -1, "x"]), {})
        self.assertEqual(len(calls), 3)

        # a failure in a later chunk gives nothing, never a partial answer
        asked = []

        def second_fails(p, **k):
            asked.append(1)
            if len(asked) > 1:
                raise OSError("down")
            return [{"id": i, "quality": {"quality": {"id": 4}}}
                    for i in k["trackFileIds"]]
        self.assertEqual(self.client(second_fails).trackfile_qualities(
            list(range(1, 151))), {})
        self.assertEqual(len(asked), 2)

    def test_quality_profile_index_with_a_group(self):
        seen = []

        def get(path, **params):
            seen.append(path)
            return {"items": [
                {"quality": {"id": 1, "name": "MP3-128"}},
                {"name": "Lossy", "items": [{"quality": {"id": 2}},
                                            {"quality": {"id": 3}}]},
                {"quality": {"id": 4, "name": "FLAC"}}]}
        c = self.client(get)
        self.assertEqual(c.quality_profile_index(7), {1: 0, 2: 1, 3: 1, 4: 2})
        self.assertEqual(seen, ["/api/v1/qualityprofile/7"])
        self.assertEqual(c.quality_profile_index(0), {})
        self.assertEqual(c.quality_profile_index(None), {})
        self.assertEqual(len(seen), 1)

        def boom(p, **k):
            raise OSError("down")
        self.assertEqual(self.client(boom).quality_profile_index(7), {})

    def test_manual_import_folder_passes_the_artist_on(self):
        c = self.client(None)
        seen = []
        c.manual_import_candidates = (
            lambda folder, artist_id=None, force=False:
            seen.append((folder, artist_id)) or [])
        self.assertEqual(c.manual_import_folder("/f", artist_id=7), (None, 0, []))
        self.assertEqual(c.manual_import_folder("/f"), (None, 0, []))
        self.assertEqual(seen, [("/f", 7), ("/f", None)])


# ---- the pass itself ---------------------------------------------------

class _Q:
    """qBittorrent for the pass: lookup answers, remove records."""

    def __init__(self, torrent, flip_category_on=0, files=None):
        self.t, self.removed, self.lookups = torrent, [], 0
        self.flip = flip_category_on
        # qBittorrent's own file list, relative to the save path.
        self._files = files if files is not None else [
            {"name": "Album/01.flac", "priority": 1},
            {"name": "Album/02.flac", "priority": 1},
            {"name": "Album/cover.jpg", "priority": 1}]

    def files(self, h):
        return list(self._files)

    def lookup(self, h):
        self.lookups += 1
        t = dict(self.t)
        if self.flip and self.lookups >= self.flip:
            t["category"] = "lidarr"
        return True, t

    def remove(self, h, delete_files=True):
        self.removed.append((h, delete_files))
        return True


class _PassLidarr:
    def __init__(self, root, cands, tfq, pidx, rec="default"):
        self.root, self.cands, self.tfq, self.pidx = root, cands, tfq, pidx
        self.rec = {"id": 5, "artistName": "Artist", "qualityProfileId": 3} \
            if rec == "default" else rec
        self.probes, self.imports, self.profile_asks = [], [], []

    def find_artist(self, name):
        return self.rec

    def windows_to_lidarr(self, p):
        return str(p).replace(self.root, "/in")

    def manual_import_candidates(self, folder, artist_id=None, force=False):
        self.probes.append((folder, artist_id, force))
        return self.cands

    def trackfile_qualities(self, ids):
        return self.tfq

    def quality_profile_index(self, pid):
        self.profile_asks.append(pid)
        return self.pidx

    def get_album(self, album_id):
        return {"statistics": {"totalTrackCount": 2}}

    def manual_import_folder(self, folder, **kw):
        self.imports.append((folder, kw))
        return (77, 2, ["Album"])


class HandAddedPass(_Clean):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Album"
        self.root.mkdir()
        for n in ("01.flac", "02.flac"):
            (self.root / n).write_bytes(b"x")
        self.h = "abc"
        handadded.set_state({self.h: {"name": "Album", "roots": [str(self.root)]}})
        self.torrent = {"hash": self.h, "name": "Album", "category": "",
                        "progress": 1.0, "state": "stalledUP",
                        "content_path": "/data/Album", "save_path": "/data"}

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def cands(self, tfid=11, qid=1, rej=("Not an upgrade for existing track "
                                          "file(s).",)):
        out = []
        for i, n in enumerate(("01.flac", "02.flac")):
            c = _cand("/in/Album/" + n, qid, (tfid + i,), rej)
            c["album"] = {"id": 9, "title": "Album"}
            out.append(c)
        return out

    def run_pass(self, lid, q=None, **extra):
        q = q or _Q(self.torrent)
        waits, gave = [], []
        stub = SimpleNamespace(
            _get_qbt=lambda: q, lidarr=lid,
            _lidarr_generation=lambda: int(getattr(lid, "failure_generation", 0)),
            cfg=SimpleNamespace(manual_import_timeout_seconds=1,
                                watch_root=self.tmp.name),
            _read_audio_tags=lambda p: ("Artist", "Album"),
            _wait_for_manual_import=lambda *a, **k: waits.append((a, k)) or True,
            _give_lidarr_the_download=lambda *a, **k: gave.append((a, k)) or True)
        import orchestrator
        for k, v in extra.items():
            setattr(stub, k, v)
        stats = orchestrator.Orchestrator.handadded_pass(stub)
        return stats, q, waits, gave, stub

    def test_a_worse_copy_goes_with_its_files(self):
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        stats, q, waits, gave, _ = self.run_pass(lid)
        self.assertEqual(stats, {"removed": 1, "copied": 0, "left": 0})
        self.assertEqual(q.removed, [(self.h, True)])
        self.assertEqual(handadded.torrents(), {})
        self.assertEqual(lid.probes, [("/in/Album", 5, True)])
        self.assertEqual(lid.profile_asks, [3])
        self.assertEqual((lid.imports, gave), ([], []))

    def test_a_torrent_holding_more_than_music_is_not_removed(self):
        # The session's review (31 Oct): only what was checked may go, so a
        # video, a deselected song or a song outside the checked folder keeps
        # the torrent.
        for extra in ({"name": "Album/bonus.mkv", "priority": 1},
                      {"name": "Album/03.flac", "priority": 0},
                      {"name": "Other/01.flac", "priority": 1}):
            handadded.set_state({self.h: {"name": "Album",
                                          "roots": [str(self.root)]}})
            lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                              {1: 0, 4: 2})
            q = _Q(self.torrent, files=[
                {"name": "Album/01.flac", "priority": 1},
                {"name": "Album/02.flac", "priority": 1}, extra])
            stats, q, *_ = self.run_pass(lid, q)
            self.assertEqual((stats["removed"], q.removed), (0, []), extra)
            self.assertEqual(list(handadded.torrents()), [self.h])

    def test_a_lidarr_failure_during_the_check_decides_nothing(self):
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        orig = lid.manual_import_candidates

        def failing(*a, **k):
            lid.failure_generation = getattr(lid, "failure_generation", 0) + 1
            return orig(*a, **k)
        lid.manual_import_candidates = failing
        stats, q, waits, gave, _ = self.run_pass(lid)
        self.assertEqual(q.removed, [])
        self.assertEqual((lid.imports, gave), ([], []))

    def test_the_worse_check_runs_at_most_every_six_hours(self):
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 1, 12: 1},
                          {1: 0, 4: 2})
        stats, q, waits, gave, stub = self.run_pass(lid)
        forced = [p for p in lid.probes if p[2]]
        self.assertEqual(len(forced), 1)
        import orchestrator
        orchestrator.Orchestrator.handadded_pass(stub)
        self.assertEqual(len([p for p in lid.probes if p[2]]), 1)

    def test_a_copy_that_is_not_worse_is_not_removed(self):
        for tfq in ({11: 1, 12: 1}, {11: 4}, {}):
            handadded.set_state({self.h: {"name": "Album",
                                          "roots": [str(self.root)]}})
            lid = _PassLidarr(self.tmp.name, self.cands(), tfq, {1: 0, 4: 2})
            stats, q, waits, gave, _ = self.run_pass(lid)
            self.assertEqual(q.removed, [])
            self.assertEqual(stats["removed"], 0)
            self.assertEqual(list(handadded.torrents()), [self.h])
            # Lidarr rejects every file (it owns them): nothing to copy
            self.assertEqual((lid.imports, gave), ([], []))

    def test_a_category_given_meanwhile_stops_the_removal(self):
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        q = _Q(self.torrent, flip_category_on=2)
        stats, q, *_ = self.run_pass(lid, q)
        self.assertEqual((stats["removed"], q.removed), (0, []))
        self.assertEqual(list(handadded.torrents()), [self.h])

    def test_a_better_copy_goes_into_lidarr_as_a_copy_once_a_day(self):
        lid = _PassLidarr(self.tmp.name, self.cands(rej=()), {}, {1: 0, 4: 2})
        stats, q, waits, gave, stub = self.run_pass(lid)
        self.assertEqual(stats, {"removed": 0, "copied": 2, "left": 0})
        self.assertEqual(q.removed, [])
        self.assertEqual(len(lid.imports), 1)
        folder, kw = lid.imports[0]
        self.assertEqual(folder, "/in/Album")
        self.assertEqual((kw["import_mode"], kw["artist_id"],
                          kw["require_full_album"], kw["album_track_totals"]),
                         ("copy", 5, True, {9: 2}))
        # Only albums whose track count is known (the session's review).
        self.assertEqual(kw["allowed_album_ids"], {9})
        self.assertEqual(len(waits), 1)
        self.assertFalse(waits[0][1]["require_cleared"])
        self.assertEqual(list(handadded.torrents()), [self.h])
        # the next pass within a day leaves it
        import orchestrator
        again = orchestrator.Orchestrator.handadded_pass(stub)
        self.assertEqual((again["copied"], len(lid.imports)), (0, 1))

    def test_music_lidarr_does_not_list_goes_to_the_record_step_as_a_copy(self):
        lid = _PassLidarr(self.tmp.name, [], {}, {1: 0, 4: 2})
        stats, q, waits, gave, _ = self.run_pass(lid)
        self.assertEqual(stats["copied"], 1)
        self.assertEqual(lid.imports, [])
        (args, kw), = gave
        self.assertEqual(args[0], self.root)
        self.assertEqual(kw, {"import_mode": "copy"})
        self.assertEqual([p.name for p in args[2]], ["01.flac", "02.flac"])
        self.assertEqual(q.removed, [])

    def test_a_cue_sheet_keeps_it_as_it_is(self):
        (self.root / "x.cue").write_text("c")
        lid = _PassLidarr(self.tmp.name, [], {}, {1: 0, 4: 2})
        stats, q, waits, gave, _ = self.run_pass(lid)
        self.assertEqual(stats, {"removed": 0, "copied": 0, "left": 1})
        self.assertEqual((lid.imports, gave, q.removed), ([], [], []))

    def test_an_unknown_artist_keeps_it_as_it_is(self):
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2}, rec=None)
        stats, q, *_ = self.run_pass(lid)
        self.assertEqual(stats, {"removed": 0, "copied": 0, "left": 1})
        self.assertEqual(q.removed, [])
        self.assertEqual(lid.probes, [])

    def test_an_unfinished_busy_or_categorised_torrent_is_skipped(self):
        for change in ({"progress": 0.5}, {"state": "checkingUP"},
                       {"state": "missingFiles"}, {"category": "lidarr"}):
            lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                              {1: 0, 4: 2})
            stats, q, *_ = self.run_pass(lid, _Q(dict(self.torrent, **change)))
            self.assertEqual((stats["removed"], q.removed, lid.probes), (0, [], []))
        import claims
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        with mock.patch.object(claims, "busy_torrent", lambda p: "/x"):
            stats, q, *_ = self.run_pass(lid)
        self.assertEqual((stats["removed"], q.removed, lid.probes), (0, [], []))

    def test_a_torrent_with_two_folders_is_never_removed(self):
        other = Path(self.tmp.name) / "Other"
        other.mkdir()
        (other / "03.flac").write_bytes(b"x")
        handadded.set_state({self.h: {"name": "Album", "roots": [
            str(self.root), str(other)]}})
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        stats, q, *_ = self.run_pass(lid)
        self.assertEqual((stats["removed"], q.removed), (0, []))

    def test_one_torrent_that_fails_does_not_stop_the_pass(self):
        other = Path(self.tmp.name) / "Other"
        other.mkdir()
        (other / "03.flac").write_bytes(b"x")
        handadded.set_state({
            "bad": {"name": "Bad", "roots": [str(other)]},
            self.h: {"name": "Album", "roots": [str(self.root)]}})
        lid = _PassLidarr(self.tmp.name, self.cands(), {11: 4, 12: 4},
                          {1: 0, 4: 2})
        real = lid.manual_import_candidates

        def probe(folder, artist_id=None, force=False):
            if folder.endswith("Other"):
                raise RuntimeError("boom")
            return real(folder, artist_id, force)
        lid.manual_import_candidates = probe
        stats, q, *_ = self.run_pass(lid)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(q.removed, [(self.h, True)])
        self.assertEqual(list(handadded.torrents()), ["bad"])

    def test_without_qbittorrent_it_does_nothing(self):
        import orchestrator
        stub = SimpleNamespace(_get_qbt=lambda: None)
        self.assertEqual(orchestrator.Orchestrator.handadded_pass(stub),
                         {"removed": 0, "copied": 0, "left": 0})


if __name__ == "__main__":
    unittest.main()
