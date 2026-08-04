"""
Cloud LLM provider (OpenAI-compatible chat completions) as a drop-in for the
local Ollama client -- so the pipeline's occasional LLM work (CUE repair, tag
normalization) can run on Google Gemini / OpenAI / Groq / OpenRouter / etc.
with ZERO local GPU use.

It subclasses OllamaClient and only overrides the low-level transport
(_generate) plus ping/warmup; all the higher-level prompt building and result
parsing (repair_cue, normalize_tags) are inherited unchanged.

Config (see main.py wiring):
    ollama:
      provider: openai
      base_url: https://generativelanguage.googleapis.com/v1beta/openai
      model:    gemini-2.0-flash
      api_key:  <your key>
      enabled:  true
      rpm: 10               # requests/minute ceiling (free tiers are ~10-15)
      max_wait_seconds: 30  # skip the LLM rather than block a worker longer
      cooldown_seconds: 900 # pause after the daily quota is exhausted

RATE LIMITING -- why this file is more than a transport
------------------------------------------------------
A local Ollama has no request ceiling, so the pipeline calls the LLM freely:
the qBittorrent lifecycle pass asks pick_owned_album() once per album per
torrent (a discography torrent is 10-50 calls), and the cueless sweep asks
parse_artist_album() per folder -- all from several worker threads at once.
Against a free cloud tier that is an instant 429 storm.

So requests are paced through a PROCESS-WIDE gate. It must be class-level, not
per-instance, because main.py and qbt_deselect.py each construct their own
client while sharing one API quota.

Pacing never blocks a worker for long: if the queue is deeper than
max_wait_seconds, the call is skipped and returns "" -- which every caller
already treats as "fall back to the deterministic path". Slow-and-degraded
beats stalling the import threads.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ollama_client import OllamaClient, _clip

logger = logging.getLogger("cloud_llm")


class CloudLLMClient(OllamaClient):
    # ---- process-wide quota state (see module docstring) ----------------
    _gate = threading.Lock()
    _next_slot = 0.0      # monotonic time at which the next request may start
    _blocked_until = 0.0  # circuit breaker: quota exhausted, stop trying
    _blocked_logged = False
    _skipped = 0          # calls dropped by the breaker, for one-line summary

    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: int = 60, enabled: bool = True,
                 rpm: int = 10, max_wait_seconds: float = 30.0,
                 max_retries: int = 3, cooldown_seconds: float = 900.0):
        super().__init__(base_url=base_url, model=model, timeout=timeout,
                         enabled=enabled, keep_alive="0", num_ctx=0)
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        # 0/negative rpm disables pacing (paid tier with a high ceiling).
        self.min_interval = (60.0 / rpm) if rpm and rpm > 0 else 0.0
        self.max_wait = float(max_wait_seconds)
        self.max_retries = max(0, int(max_retries))
        self.cooldown = float(cooldown_seconds)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def ping(self) -> bool:
        if not self.enabled:
            return False
        if not self.api_key:
            logger.warning("Cloud LLM: no api_key configured")
            return False
        try:
            r = self.session.get(f"{self.base_url}/models",
                                 headers=self._headers(), timeout=15)
            if r.status_code == 200:
                return True
            logger.warning("Cloud LLM ping got HTTP %s", r.status_code)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloud LLM ping failed: %s", exc)
            return False

    def warmup(self) -> bool:
        return self.enabled  # nothing to load -- no GPU, no cold start

    # ---------- quota gate ----------------------------------------------

    def _acquire_slot(self) -> bool:
        """Reserve the next pacing slot. False -> caller should skip the LLM.

        The sleep happens OUTSIDE the lock, so N waiting threads are spaced
        min_interval apart rather than all sleeping the same amount and then
        firing together (which is what produced the 8-requests-per-second
        bursts in the first place).
        """
        cls = CloudLLMClient
        with cls._gate:
            now = time.monotonic()
            if now < cls._blocked_until:
                cls._skipped += 1
                return False
            if self.min_interval <= 0:
                return True
            start = max(now, cls._next_slot)
            wait = start - now
            if wait > self.max_wait:
                # Queue is too deep -- don't hold an import thread hostage.
                cls._skipped += 1
                return False
            cls._next_slot = start + self.min_interval
        if wait > 0:
            time.sleep(wait)
        return True

    def _trip_breaker(self, detail: str) -> None:
        """Stop hammering a exhausted quota; log once, not once per call."""
        cls = CloudLLMClient
        with cls._gate:
            cls._blocked_until = time.monotonic() + self.cooldown
            first = not cls._blocked_logged
            cls._blocked_logged = True
        if first:
            logger.warning(
                "Cloud LLM quota exhausted -- pausing LLM calls for %.0fs and "
                "falling back to the deterministic path. %s",
                self.cooldown, detail,
            )

    @staticmethod
    def _retry_after(resp) -> Optional[float]:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw.strip()))
        except (TypeError, ValueError):
            return None

    # ---------- transport ------------------------------------------------

    def _generate(
        self, system: str, prompt: str, format_json: bool = False,
        num_predict: Optional[int] = None, timeout: Optional[float] = None,
        label: str = "generate", subject: str = "",
    ) -> str:
        if not self.enabled:
            return ""
        if not self._acquire_slot():
            logger.info("LLM %s: SKIPPED (rate limit / quota)", label)
            return ""
        started = time.monotonic()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": num_predict if num_predict is not None else 2048,
        }
        if format_json:
            # Most OpenAI-compatible servers (incl. Gemini's) accept this;
            # if one doesn't, the request just fails and we fall back to the
            # deterministic path -- never fatal.
            body["response_format"] = {"type": "json_object"}
        http_timeout = timeout if timeout is not None else self.timeout

        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(), json=body, timeout=http_timeout,
                )
                if r.status_code == 400 and format_json:
                    # Retry once without response_format for servers that
                    # reject it.
                    body.pop("response_format", None)
                    format_json = False
                    r = self.session.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(), json=body,
                        timeout=http_timeout,
                    )
                if r.status_code == 429:
                    detail = (r.text or "")[:300]
                    # A per-day quota won't clear by waiting seconds, so stop
                    # retrying and trip the breaker immediately.
                    if "per day" in detail.lower() or "PerDay" in detail:
                        self._trip_breaker(detail)
                        return ""
                    if attempt >= self.max_retries:
                        self._trip_breaker(detail)
                        return ""
                    backoff = self._retry_after(r) or (2.0 ** attempt) * 2.0
                    logger.info(
                        "Cloud LLM 429 -- backing off %.1fs (attempt %d/%d)",
                        backoff, attempt + 1, self.max_retries,
                    )
                    time.sleep(backoff)
                    continue
                r.raise_for_status()
                data = r.json()
                # A successful call means the quota is flowing again; let the
                # next exhaustion log afresh.
                CloudLLMClient._blocked_logged = False
                out = (data["choices"][0]["message"]["content"] or "").strip()
                logger.info(
                    "LLM %s: %s -> %s (%.1fs)", label,
                    _clip(subject) if subject else "(no subject)",
                    _clip(out) if out else "EMPTY",
                    time.monotonic() - started,
                )
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cloud LLM generate failed: %s", exc)
                return ""
        return ""

    # ---------- diagnostics ---------------------------------------------

    @classmethod
    def quota_summary(cls) -> str:
        """One-line state for the periodic heartbeat / WebUI."""
        with cls._gate:
            remaining = max(0.0, cls._blocked_until - time.monotonic())
            skipped = cls._skipped
        if remaining > 0:
            return f"LLM paused {remaining:.0f}s (quota), {skipped} call(s) skipped"
        return f"LLM ok ({skipped} call(s) skipped so far)"
