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
"""

from __future__ import annotations

import logging
from typing import Optional

from ollama_client import OllamaClient

logger = logging.getLogger("cloud_llm")


class CloudLLMClient(OllamaClient):
    def __init__(self, base_url: str, model: str, api_key: str,
                 timeout: int = 60, enabled: bool = True):
        super().__init__(base_url=base_url, model=model, timeout=timeout,
                         enabled=enabled, keep_alive="0", num_ctx=0)
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")

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

    def _generate(
        self, system: str, prompt: str, format_json: bool = False,
        num_predict: Optional[int] = None, timeout: Optional[float] = None,
    ) -> str:
        if not self.enabled:
            return ""
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
        try:
            r = self.session.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(), json=body,
                timeout=timeout if timeout is not None else self.timeout,
            )
            if r.status_code == 400 and format_json:
                # Retry once without response_format for servers that reject it.
                body.pop("response_format", None)
                r = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(), json=body,
                    timeout=timeout if timeout is not None else self.timeout,
                )
            r.raise_for_status()
            data = r.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cloud LLM generate failed: %s", exc)
            return ""
