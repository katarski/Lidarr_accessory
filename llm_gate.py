"""
When cue_pipeline may put a question to the LLM on a shared GPU (Daniel's 3090).

The owner's rules (2026-09-28):
  * use the SAME model Home Assistant uses, with the SAME context window, so the
    one runner in VRAM serves both and Ollama never has to reload it;
  * "halt everything and wait until the GPU is idle";
  * "if home assistant is off - just load the model".

So a question is only sent while this gate is OPEN:

  Ollama not answering ....................... CLOSED  the PC is asleep or off
  another model resident in Ollama ........... CLOSED  someone else's work
  Home Assistant not answering ............... OPEN    HA is off: just load it
  HA answering, no fresh GPU reading ......... CLOSED  unknown is not idle
  GPU load high, and not our own ............. CLOSED
  model not loaded and the GPU memory is
    held by something else, or too little
    of it is free for the model .............. CLOSED  a game, a render...
  otherwise .................................. OPEN    idle: use it, load if needed

A model that is ALREADY resident is used whatever else sits in VRAM (Whisper, the
TTS voices): asking it costs no memory, only GPU time, and that is what the load
reading measures.

CLOSED is not "no". Every LLM method returns UNAVAILABLE and the caller DEFERS
the decision -- it never caches or persists it as a negative answer -- and asks
again on its next pass. That is what "halt and wait" means for the one item
that needs the model; the rest of the pipeline carries on.

Nothing here runs on a timer, with one exception gated on the resource actually
being held: while a model THIS process loaded is inside its keep_alive lease and
Home Assistant is not using the PC for voice, a watcher looks every 30 s and
releases it (empty prompt + keep_alive 0 -- a control message that never loads
anything) as soon as something else wants the GPU. It exits with the lease.

With no Home Assistant configured the gate only checks Ollama itself: reachable,
and no other model resident.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests

logger = logging.getLogger("llm_gate")

_MIB = 1024 * 1024
# Ollama reports a keep_alive of -1 ("forever") as an expiry centuries away.
_FOREVER_SECONDS = 10 * 365 * 86400


def same_model(a: str, b: str) -> bool:
    """Ollama names a model with or without its ':latest' tag."""
    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        return s if ":" in s.rsplit("/", 1)[-1] else s + ":latest"
    return bool(a) and bool(b) and norm(a) == norm(b)


def _model_name(m: dict) -> str:
    return str(m.get("name") or m.get("model") or "")


def _seconds_until(stamp: str) -> float:
    """Seconds from now until an RFC 3339 time; 0 when unknown or past."""
    if not stamp:
        return 0.0
    try:
        # Ollama writes nanoseconds; Python parses at most microseconds.
        s = str(stamp).replace("Z", "+00:00")
        if "." in s:
            head, tail = s.split(".", 1)
            frac = ""
            rest = ""
            for i, ch in enumerate(tail):
                if not ch.isdigit():
                    rest = tail[i:]
                    break
                frac += ch
            s = head + "." + frac[:6] + rest
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Verdict:
    open: bool
    reason: str
    kind: str              # idle / ha_off / no_ha / asleep / busy / ...
    resident: bool = False  # our model is already loaded
    ctx: int = 0            # context_length of the loaded runner, 0 = unknown
    ha_on: bool = False
    expires_in: float = 0.0  # seconds left on the resident model's keep_alive
    other_mb: float = 0.0    # GPU memory held by anything that is not our model


class GpuGate:
    def __init__(
        self,
        ollama_base: str,
        model: str,
        *,
        ha_url: str = "",
        ha_token: str = "",
        load_entity: str = "sensor.daniel_gpuload",
        memory_entity: str = "sensor.daniel_gpu_memory",
        voice_switch_entity: str = "input_boolean.voice_use_daniel",
        busy_load_pct: float = 50.0,
        other_vram_max_mb: float = 4096.0,
        sensor_max_age_seconds: float = 180.0,
        recheck_seconds: float = 20.0,
        self_window_seconds: float = 60.0,
        keep_alive_seconds: float = 600.0,
        session: Optional[requests.Session] = None,
    ):
        self.ollama_base = (ollama_base or "").rstrip("/")
        self.model = model
        self.ha_url = (ha_url or "").rstrip("/")
        self.ha_token = ha_token or ""
        self.load_entity = load_entity
        self.memory_entity = memory_entity
        self.voice_switch_entity = voice_switch_entity
        self.busy_load_pct = float(busy_load_pct)
        self.other_vram_max_mb = float(other_vram_max_mb)
        self.sensor_max_age = float(sensor_max_age_seconds)
        self.recheck = float(recheck_seconds)
        self.self_window = float(self_window_seconds)
        self.keep_alive = float(keep_alive_seconds)
        self.session = session or requests.Session()

        self._lock = threading.Lock()
        self._cached: Optional[Verdict] = None
        self._cached_at = 0.0
        self._last_logged_kind = ""
        self._ha_auth_logged = False
        # Our own use of the GPU, so a load reading we caused is not read as
        # somebody else's work.
        self._inflight = 0
        self._last_end = 0.0
        # Learned from /api/ps the first time our model is seen resident; a
        # model not yet seen is estimated from its file size.
        self._need_mb = 0.0
        self._file_mb = 0.0
        # The lease on a model this process loaded (see module docstring).
        self._lease_until = 0.0
        self._watcher: Optional[threading.Thread] = None
        self.deferred = 0

    # ---------- probes ---------------------------------------------------

    def _ollama_ps(self) -> Optional[List[dict]]:
        try:
            r = self.session.get(self.ollama_base + "/api/ps", timeout=(3, 5))
            r.raise_for_status()
            return list(r.json().get("models") or [])
        except Exception:  # noqa: BLE001
            return None

    def _model_file_mb(self) -> float:
        if self._file_mb:
            return self._file_mb
        try:
            r = self.session.get(self.ollama_base + "/api/tags", timeout=(3, 5))
            r.raise_for_status()
            for m in r.json().get("models") or []:
                if same_model(_model_name(m), self.model):
                    self._file_mb = float(m.get("size") or 0) / _MIB
        except Exception:  # noqa: BLE001
            pass
        return self._file_mb

    def _ha_state(self, entity: str):
        """(True, state-dict|None) when HA answered, (False, None) when HA is
        not answering at all. A refused token or a missing entity is HA being
        ON with no reading -- never HA being off."""
        if not self.ha_url:
            return False, None
        headers = {"Authorization": "Bearer " + self.ha_token} if self.ha_token else {}
        try:
            r = self.session.get("%s/api/states/%s" % (self.ha_url, entity),
                                 headers=headers, timeout=(3, 5))
        except (requests.ConnectionError, requests.Timeout):
            return False, None
        except Exception:  # noqa: BLE001
            return True, None
        if r.status_code in (401, 403):
            if not self._ha_auth_logged:
                self._ha_auth_logged = True
                logger.error(
                    "LLM gate: Home Assistant refused the token (HTTP %s), so "
                    "the GPU load cannot be read and model work will wait. "
                    "Set HA_TOKEN in the container template.", r.status_code)
            return True, None
        if r.status_code != 200:
            return True, None
        try:
            return True, r.json()
        except ValueError:
            return True, None

    def _fresh_number(self, st) -> Optional[float]:
        if not st:
            return None
        try:
            val = float(st.get("state"))
        except (TypeError, ValueError):
            return None                     # unavailable / unknown
        stamp = st.get("last_reported") or st.get("last_updated") or ""
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                   ).total_seconds()
        except (TypeError, ValueError):
            return None
        return val if age <= self.sensor_max_age else None

    # ---------- the decision --------------------------------------------

    def check(self, force: bool = False) -> Verdict:
        now = time.monotonic()
        with self._lock:
            if (not force and self._cached is not None
                    and now - self._cached_at < self.recheck):
                return self._cached
        v = self._decide(now)
        with self._lock:
            self._cached, self._cached_at = v, now
            if v.kind != self._last_logged_kind:
                self._last_logged_kind = v.kind
                logger.info("LLM gate %s: %s%s", "OPEN" if v.open else "CLOSED",
                            v.reason,
                            "" if v.open or not self.deferred
                            else " (%d question(s) deferred so far)" % self.deferred)
        return v

    def _decide(self, now: float) -> Verdict:
        ps = self._ollama_ps()
        if ps is None:
            return Verdict(False, "Ollama at %s is not answering" % self.ollama_base,
                           "asleep")
        ours = [m for m in ps if same_model(_model_name(m), self.model)]
        others = [_model_name(m) or "?" for m in ps
                  if not same_model(_model_name(m), self.model)]
        resident = bool(ours)
        ctx = int(ours[0].get("context_length") or 0) if ours else 0
        ours_mb = float(ours[0].get("size_vram") or 0) / _MIB if ours else 0.0
        expires = _seconds_until(ours[0].get("expires_at")) if ours else 0.0
        if expires > _FOREVER_SECONDS:
            expires = math.inf
        if ours_mb:
            self._need_mb = ours_mb
        base = dict(resident=resident, ctx=ctx, expires_in=expires)

        # Someone else's model on the GPU is somebody else's work, whether or
        # not Home Assistant is up to say so.
        if others:
            return Verdict(False, "another model is on the GPU (%s)" % ", ".join(others),
                           "other_model", **base)

        if not self.ha_url:
            return Verdict(True, "no Home Assistant configured; Ollama is free",
                           "no_ha", **base)
        ha_on, load_st = self._ha_state(self.load_entity)
        if not ha_on:
            return Verdict(True, "Home Assistant is off, so the model is ours to load",
                           "ha_off", **base)
        _, mem_st = self._ha_state(self.memory_entity)
        load = self._fresh_number(load_st)
        mem = self._fresh_number(mem_st)
        base["ha_on"] = True
        if load is None or mem is None:
            return Verdict(False, "no fresh GPU reading from Home Assistant (%s, %s)"
                           % (self.load_entity, self.memory_entity),
                           "no_reading", **base)
        other_mb = max(0.0, mem - ours_mb)
        base["other_mb"] = other_mb
        ours_recent = self._inflight > 0 or (now - self._last_end) < self.self_window
        if load >= self.busy_load_pct and not ours_recent:
            return Verdict(False, "the GPU is busy (%.0f%% load)" % load,
                           "busy", **base)
        if not resident:
            if other_mb > self.other_vram_max_mb:
                return Verdict(False, "%.0f MB of GPU memory is held by something "
                               "else" % other_mb, "other_vram", **base)
            need = self._need_mb or self._model_file_mb() * 1.15
            try:
                free = float(((mem_st or {}).get("attributes") or {}).get("free"))
            except (TypeError, ValueError):
                free = None
            if need and free is not None and free < need:
                return Verdict(False, "only %.0f MB of GPU memory free, the model "
                               "needs about %.0f MB" % (free, need), "no_room", **base)
        return Verdict(True, "the GPU is idle (%.0f%% load, %.0f MB in use)" % (load, mem),
                       "idle", **base)

    # ---------- bookkeeping around one generate --------------------------

    def begin(self) -> None:
        with self._lock:
            self._inflight += 1

    def end(self, loaded_by_us: bool, ha_on: bool) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._last_end = time.monotonic()
            if loaded_by_us or self._lease_until:
                self._lease_until = time.monotonic() + self.keep_alive
            start = (loaded_by_us and ha_on
                     and (self._watcher is None or not self._watcher.is_alive()))
        if start:
            self._watcher = threading.Thread(target=self._watch_lease, daemon=True,
                                             name="llm-lease-watch")
            self._watcher.start()

    def note_deferred(self) -> None:
        with self._lock:
            self.deferred += 1

    # ---------- releasing a model we loaded ------------------------------

    def _voice_switch_off(self) -> bool:
        """True only on a definite 'off'. Unavailable is not off."""
        if not self.voice_switch_entity:
            return True
        ha_on, st = self._ha_state(self.voice_switch_entity)
        return bool(ha_on and st and st.get("state") == "off")

    def unload(self) -> bool:
        try:
            r = self.session.post(
                self.ollama_base + "/api/generate",
                json={"model": self.model, "prompt": "", "stream": False, "keep_alive": 0},
                timeout=(3, 10))
            return r.ok
        except Exception:  # noqa: BLE001
            return False

    def wanted_elsewhere(self, v: Verdict) -> bool:
        """Something other than us wants the GPU our model is sitting on."""
        return (v.kind in ("busy", "other_model")
                or (v.ha_on and v.other_mb > self.other_vram_max_mb))

    def _watch_lease(self) -> None:
        while True:
            time.sleep(30)
            with self._lock:
                if time.monotonic() >= self._lease_until:
                    self._lease_until = 0.0
                    return
                busy_ours = self._inflight > 0
            if busy_ours:
                continue
            v = self.check(force=True)
            if not v.resident:
                with self._lock:
                    self._lease_until = 0.0
                return
            if self.wanted_elsewhere(v) and self._voice_switch_off():
                ok = self.unload()
                logger.info("LLM gate: released %s because %s (%s)", self.model,
                            v.reason if v.kind != "idle"
                            else "%.0f MB of GPU memory is now held by something "
                                 "else" % v.other_mb,
                            "done" if ok else "unload failed")
                with self._lock:
                    self._lease_until = 0.0
                return


# ---------- building the client ------------------------------------------

_CLOUD_PROVIDERS = ("openai", "gemini", "cloud", "openai-compatible")


def _as_bool(v, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def build_llm(ocfg: dict):
    """
    The one place an LLM client is built from the `ollama:` config section,
    so the pipeline and the qbt_deselect CLI ask the same model the same way.
    Returns (client, label); (None, "") when the LLM is switched off.
    """
    ocfg = ocfg or {}
    if not _as_bool(ocfg.get("enabled"), True):
        return None, ""
    provider = str(ocfg.get("provider", "ollama")).lower()
    if provider in _CLOUD_PROVIDERS:
        from cloud_llm import CloudLLMClient
        client = CloudLLMClient(
            base_url=ocfg.get("base_url", ""),
            model=ocfg.get("model", ""),
            api_key=str(ocfg.get("api_key", "")),
            timeout=int(ocfg.get("timeout_seconds", 60)),
            enabled=True,
            rpm=int(ocfg.get("rpm", 10)),
            max_wait_seconds=float(ocfg.get("max_wait_seconds", 30)),
            max_retries=int(ocfg.get("max_retries", 3)),
            cooldown_seconds=float(ocfg.get("cooldown_seconds", 900)),
        )
        return client, ("cloud LLM (%s, model=%s, %s req/min)"
                        % (provider, ocfg.get("model"), ocfg.get("rpm", 10)))

    from ollama_client import OllamaClient, keep_alive_seconds
    base_url = ocfg.get("base_url", "http://127.0.0.1:11434")
    model = ocfg.get("model", "")
    keep_alive = ocfg.get("keep_alive", 600)
    gate = None
    if _as_bool(ocfg.get("gpu_gate"), True):
        gate = GpuGate(
            base_url, model,
            ha_url=str(ocfg.get("ha_url") or ""),
            ha_token=str(ocfg.get("ha_token") or ""),
            load_entity=str(ocfg.get("gpu_load_entity") or "sensor.daniel_gpuload"),
            memory_entity=str(ocfg.get("gpu_memory_entity") or "sensor.daniel_gpu_memory"),
            voice_switch_entity=str(ocfg.get("voice_switch_entity")
                                    or "input_boolean.voice_use_daniel"),
            busy_load_pct=float(ocfg.get("gpu_busy_load_pct", 50)),
            other_vram_max_mb=float(ocfg.get("gpu_other_vram_max_mb", 4096)),
            sensor_max_age_seconds=float(ocfg.get("gpu_sensor_max_age_seconds", 180)),
            recheck_seconds=float(ocfg.get("gate_recheck_seconds", 20)),
            keep_alive_seconds=max(0.0, keep_alive_seconds(keep_alive)),
        )
    think = ocfg.get("think", False)
    client = OllamaClient(
        base_url=base_url,
        model=model,
        timeout=int(ocfg.get("timeout_seconds", 120)),
        enabled=True,
        keep_alive=keep_alive,
        num_ctx=int(ocfg.get("num_ctx", 16384)),
        think=None if think is None or str(think).lower() == "none"
        else _as_bool(think, False),
        gate=gate,
        load_timeout=float(ocfg.get("load_timeout_seconds", 180)),
    )
    label = "Ollama (%s, model=%s, num_ctx=%s%s)" % (
        base_url, model, client.num_ctx,
        ", GPU gate via %s" % (gate.ha_url or "Ollama only") if gate else "")
    return client, label
