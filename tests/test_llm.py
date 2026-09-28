"""The GPU gate and the Ollama client: ask only when the GPU is idle, and a
question that could not be asked is UNAVAILABLE -- never a 'no', never cached."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_gate  # noqa: E402
import ollama_client as oc  # noqa: E402
from llm_gate import GpuGate  # noqa: E402

MODEL = "huihui_ai/Qwen3.6-abliterated:27b"
MIB = 1024 * 1024


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.ok = status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError("%d" % self.status_code)
            err.response = self
            raise err


class FakeSession:
    """GET routes by URL suffix; POST answers from a queue and records payloads."""
    def __init__(self, routes=None, posts=None):
        self.routes = routes or {}
        self.posts = list(posts or [])
        self.sent = []

    def get(self, url, **kw):
        for suffix, val in self.routes.items():
            if url.endswith(suffix):
                if isinstance(val, Exception):
                    raise val
                return val
        return Resp(404)

    def post(self, url, json=None, timeout=None, **kw):
        self.sent.append((json, timeout))
        val = self.posts.pop(0)
        if isinstance(val, Exception):
            raise val
        return val


def sensor(value, age_s=5, **attrs):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    return Resp(200, {"state": str(value), "last_reported": stamp, "attributes": attrs})


def ps(*models):
    return Resp(200, {"models": list(models)})


def running(name=MODEL, ctx=16384, vram_mb=16800, expires_s=300):
    exp = (datetime.now(timezone.utc) + timedelta(seconds=expires_s)).isoformat()
    return {"name": name, "size_vram": vram_mb * MIB, "context_length": ctx,
            "expires_at": exp}


def gate(routes, **kw):
    return GpuGate("http://ollama", MODEL, ha_url="http://ha", ha_token="t",
                   session=FakeSession(routes), **kw)


class GateDecisions(unittest.TestCase):
    def test_ollama_not_answering_is_closed(self):
        v = gate({"/api/ps": requests.ConnectionError()}).check()
        self.assertEqual((v.open, v.kind), (False, "asleep"))

    def test_someone_elses_model_is_closed(self):
        v = gate({"/api/ps": ps(running(name="other:7b"))}).check()
        self.assertEqual((v.open, v.kind), (False, "other_model"))

    def test_home_assistant_off_means_load_it(self):
        v = gate({"/api/ps": ps(), "sensor.daniel_gpuload": requests.ConnectionError()}).check()
        self.assertEqual((v.open, v.kind), (True, "ha_off"))

    def test_refused_token_is_no_reading_not_ha_off(self):
        v = gate({"/api/ps": ps(), "sensor.daniel_gpuload": Resp(401),
                  "sensor.daniel_gpu_memory": Resp(401)}).check()
        self.assertEqual((v.open, v.kind), (False, "no_reading"))

    def test_stale_or_unavailable_sensor_is_no_reading(self):
        v = gate({"/api/ps": ps(), "sensor.daniel_gpuload": sensor(3, age_s=900),
                  "sensor.daniel_gpu_memory": sensor(1000, free=20000)}).check()
        self.assertEqual(v.kind, "no_reading")
        v = gate({"/api/ps": ps(), "sensor.daniel_gpuload": sensor("unavailable"),
                  "sensor.daniel_gpu_memory": sensor(1000, free=20000)}).check()
        self.assertEqual(v.kind, "no_reading")

    def test_busy_gpu_is_closed_unless_the_load_is_ours(self):
        routes = {"/api/ps": ps(running()), "sensor.daniel_gpuload": sensor(90),
                  "sensor.daniel_gpu_memory": sensor(17000, free=7000)}
        g = gate(routes)
        self.assertEqual(g.check().kind, "busy")
        g.begin()
        self.assertTrue(g.check(force=True).open)
        g._inflight = 0

    def test_not_enough_free_memory_to_load(self):
        routes = {"/api/ps": ps(), "sensor.daniel_gpuload": sensor(2),
                  "sensor.daniel_gpu_memory": sensor(12000, free=3000),
                  "/api/tags": Resp(200, {"models": [{"name": MODEL, "size": 17000 * MIB}]})}
        v = gate(routes, other_vram_max_mb=20000).check()
        self.assertEqual((v.open, v.kind), (False, "no_room"))

    def test_idle_is_open_and_reports_the_resident_runner(self):
        routes = {"/api/ps": ps(running(ctx=8192)), "sensor.daniel_gpuload": sensor(4),
                  "sensor.daniel_gpu_memory": sensor(17000, free=7000)}
        v = gate(routes).check()
        self.assertEqual((v.open, v.kind, v.resident, v.ctx), (True, "idle", True, 8192))
        self.assertGreater(v.expires_in, 200)

    def test_voice_switch_unavailable_is_not_off(self):
        g = gate({"input_boolean.voice_use_daniel": Resp(200, {"state": "unavailable"})})
        self.assertFalse(g._voice_switch_off())
        g = gate({"input_boolean.voice_use_daniel": Resp(200, {"state": "off"})})
        self.assertTrue(g._voice_switch_off())


class StubGate:
    def __init__(self, v):
        self.v, self.deferred, self.ended = v, 0, []

    def check(self, force=False):
        return self.v

    def note_deferred(self):
        self.deferred += 1

    def begin(self):
        pass

    def end(self, loaded_by_us, ha_on):
        self.ended.append(loaded_by_us)


def verdict(open_=True, **kw):
    return llm_gate.Verdict(open_, "test", "idle" if open_ else "busy", **kw)


def client(v, posts, **kw):
    c = oc.OllamaClient("http://ollama", MODEL, timeout=60, keep_alive=600,
                        num_ctx=16384, think=False, gate=StubGate(v), **kw)
    c.session = FakeSession(posts=posts)
    return c


class ClientBehaviour(unittest.TestCase):
    def test_closed_gate_defers_without_any_request(self):
        c = client(verdict(False), [])
        self.assertIs(c.pick_owned_album("Blackstar", ["Blackstar"]), oc.UNAVAILABLE)
        self.assertEqual(c.gate.deferred, 1)
        self.assertEqual(c.session.sent, [])

    def test_payload_matches_home_assistant(self):
        c = client(verdict(resident=True, ctx=16384, expires_in=100), [Resp(200, {"response": "x"})])
        c._generate("s", "p")
        payload, timeout = c.session.sent[0]
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["options"]["num_ctx"], 16384)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["keep_alive"], 600)
        self.assertEqual(timeout[0], 3.05)

    def test_resident_context_is_adopted_so_the_model_is_not_reloaded(self):
        c = client(verdict(resident=True, ctx=8192), [Resp(200, {"response": "x"})])
        c._generate("s", "p")
        self.assertEqual(c.session.sent[0][0]["options"]["num_ctx"], 8192)

    def test_keep_alive_never_shortens_the_resident_expiry(self):
        c = client(verdict(resident=True, expires_in=float("inf")), [Resp(200, {"response": "x"})])
        c._generate("s", "p")
        self.assertEqual(c.session.sent[0][0]["keep_alive"], -1)
        c = client(verdict(resident=True, expires_in=1800), [Resp(200, {"response": "x"})])
        c._generate("s", "p")
        self.assertEqual(c.session.sent[0][0]["keep_alive"], 1800)

    def test_a_cold_model_gets_the_load_budget(self):
        c = client(verdict(resident=False), [Resp(200, {"response": "x"})], load_timeout=180)
        c._generate("s", "p", timeout=60)
        self.assertEqual(c.session.sent[0][1], (3.05, 180))
        self.assertEqual(c.gate.ended, [True])  # we loaded it: our lease

    def test_think_flag_rejected_is_retried_without_it(self):
        c = client(verdict(resident=True), [Resp(400, text='"think" not supported'),
                                            Resp(200, {"response": "ok"})])
        self.assertEqual(c._generate("s", "p"), "ok")
        self.assertNotIn("think", c.session.sent[1][0])

    def test_failures_are_unavailable_and_never_cached(self):
        for failure in (requests.exceptions.ReadTimeout(), requests.exceptions.ConnectTimeout(),
                        Resp(200, {"response": ""}), Resp(500)):
            c = client(verdict(resident=True), [failure])
            self.assertIs(c.pick_owned_album("Blackstar", ["Blackstar", "Heathen"]),
                          oc.UNAVAILABLE, failure)
            self.assertEqual(c._match_cache, {}, failure)
            self.assertTrue(oc.is_unavailable(oc.UNAVAILABLE) and not oc.UNAVAILABLE)

    def test_a_real_none_is_cached(self):
        c = client(verdict(resident=True), [Resp(200, {"response": "NONE"})])
        self.assertIsNone(c.pick_owned_album("Blackstar", ["Heathen"]))
        self.assertEqual(list(c._match_cache.values()), [None])

    def test_pick_guard_refuses_contradicted_titles(self):
        c = client(verdict(resident=True), [Resp(200, {"response": "The Album"})])
        self.assertIsNone(c.pick_owned_album("A", ["The Album", "Arrival"]))
        c = client(verdict(resident=True), [Resp(200, {"response": "Wish Upon a Blackstar"})])
        self.assertEqual(c.pick_owned_album("Live Upon A Blackstar", ["Wish Upon a Blackstar"]),
                         "Wish Upon a Blackstar")

    def test_parse_split_accepts_non_latin_folders(self):
        c = client(verdict(resident=True), [Resp(200, {"response": "Лили Иванова | Танго"})])
        self.assertEqual(c.parse_artist_album("Лили Иванова - Танго (1985)"),
                         ("Лили Иванова", "Танго"))
        c = client(verdict(False), [])
        a, b = c.parse_artist_album("Лили Иванова - Танго")
        self.assertTrue(oc.is_unavailable(a) and oc.is_unavailable(b))


class TagMerge(unittest.TestCase):
    def plan(self, n, title):
        from tagger import TagPlan
        fields = {f: "" for f in TagPlan.__dataclass_fields__}
        fields.update(tracknumber=str(n), title=title, album="Album", artist="Artist",
                      albumartist="Artist")
        return TagPlan(**fields)

    def test_only_cosmetic_fixes_paired_by_track_number(self):
        plans = [self.plan(1, "hello world [320 kbps]"), self.plan(2, "second")]
        out = oc._merge_cosmetic(plans, [
            {"tracknumber": "2", "title": "Something Else"},
            {"tracknumber": "01", "title": "Hello World"}])
        self.assertEqual([p.title for p in out], ["Hello World", "second"])

    def test_numbers_that_do_not_pair_discard_everything(self):
        plans = [self.plan(1, "a"), self.plan(2, "b")]
        self.assertIsNone(oc._merge_cosmetic(plans, [{"tracknumber": "1", "title": "A"}]))


if __name__ == "__main__":
    unittest.main()
