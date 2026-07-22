"""
Ollama HTTP client.

Two jobs:
  * repair_cue(text)        -> clean .cue text (or "" on failure)
  * normalize_tags(plans)   -> tweaked TagPlan list (or None to keep original)

Both calls are best-effort: if Ollama is down or returns garbage, callers
fall back to the deterministic path.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, replace
from typing import List, Optional, TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from tagger import TagPlan

logger = logging.getLogger(__name__)


# Keys a JSON-forcing LLM is likely to use when it wraps an array in an
# object despite being told not to. Ordered by how common they are in
# qwen2.5 output.
_COMMON_WRAPPER_KEYS = (
    "tracks", "items", "data", "result", "results",
    "tags", "array", "list", "output",
)


def _coerce_to_list(parsed, expected_len: int):
    """
    Normalise LLM JSON output into a list. Handles three shapes:
        [...]                          -> as-is
        {"tracks": [...]}              -> unwrap the obvious key
        {"0": {...}, "1": {...}, ...}  -> values() if keys are index-like
    Falls through unchanged if nothing matches.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # 1. Single-key dict whose value is a list of the right length.
        for key in _COMMON_WRAPPER_KEYS:
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        # 2. Any single-key dict wrapping a list.
        if len(parsed) == 1:
            only = next(iter(parsed.values()))
            if isinstance(only, list):
                return only
        # 3. Dict keyed by "0","1",... -- return ordered values.
        keys = list(parsed.keys())
        if keys and all(k.isdigit() for k in keys) and len(keys) == expected_len:
            return [parsed[k] for k in sorted(keys, key=int)]
    return parsed


# Prompts are intentionally short and strict about output format.
# Keep them terse; big prompts slow down small local models.

_CUE_REPAIR_SYSTEM = (
    "You repair broken .cue sheet files. "
    "Input is the raw text of a .cue file that a standard parser rejected. "
    "Output ONLY the corrected .cue text, nothing else. No commentary, "
    "no code fences. Preserve all FILE, TRACK, TITLE, PERFORMER, INDEX "
    "lines. Ensure every TRACK has an INDEX 01 line in MM:SS:FF form. "
    "Make sure track numbers are sequential starting at 01."
)


_TAG_NORMALIZE_SYSTEM = (
    "You clean up music metadata for a library. Input is a JSON array of "
    "track tag objects. Return ONLY a JSON array with the same length and "
    "same keys. Rules: "
    "1) Use proper title case for titles and album names. "
    "2) Remove junk tokens like [320kbps], (FLAC), (2CD Remaster Bonus). "
    "3) Unify featured-artist style to 'feat. X' (not 'ft.' or 'featuring'). "
    "4) Do NOT invent dates, ISRCs, or genres. "
    "5) Do NOT change artist names beyond obvious capitalisation fixes."
)


_ALBUM_MATCH_SYSTEM = (
    "You match a downloaded music album folder to a list of albums the user "
    "ALREADY OWNS by the same artist. The folder name often differs from an "
    "owned album's title: edition/label tags, year, language, punctuation, "
    "abbreviations, 'remaster'/'deluxe'/'expanded' wording, disc or box-set "
    "naming, or extra words. Decide which owned album is the SAME underlying "
    "release/album as the download. Reply with ONLY the exact owned title from "
    "the list, copied verbatim, or the single word NONE if none is clearly the "
    "same album. Never invent a title. Never explain."
)


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 300,
        enabled: bool = True,
        keep_alive: str = "2h",
        num_ctx: int = 8192,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.enabled = enabled
        # Context window to load the model with. Ollama otherwise defaults
        # to the model's max (32k for qwen2.5), whose KV cache costs ~6 GB
        # on a 14B model. Our prompts are a single CUE sheet or track list
        # -- a few thousand tokens -- so a small window slashes VRAM with
        # no quality loss. 8192 tokens -> ~1.5 GB KV (vs ~6 GB at 32768).
        self.num_ctx = int(num_ctx)
        # Tell Ollama to keep the model resident in VRAM between calls.
        # Accepts Go-duration strings ("30m", "2h", "-1" for forever).
        # This is the single biggest latency win for a ~20 GB model like
        # qwen2.5:32b -- without it, every cold call pays the load cost.
        # Default is generous (2h) because pipeline idle gaps during big
        # backlogs can easily exceed 30 minutes between LLM calls.
        self.keep_alive = keep_alive
        self.session = requests.Session()
        # Guard against stacking re-warm threads if timeouts cluster.
        self._rewarm_lock = threading.Lock()
        self._rewarm_in_flight = False

    # ---------- low-level ------------------------------------------------

    def _generate(
        self,
        system: str,
        prompt: str,
        format_json: bool = False,
        num_predict: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> str:
        if not self.enabled:
            return ""
        # Always cap output so a runaway (e.g. qwen getting stuck emitting
        # nested JSON structure under format=json) can't consume the entire
        # HTTP timeout. Callers may tighten further.
        options = {"temperature": 0.1}
        options["num_predict"] = num_predict if num_predict is not None else 2048
        # Pin the context window so Ollama doesn't load the model at its
        # full 32k max (huge KV cache). Keeps VRAM close to the weight size.
        options["num_ctx"] = self.num_ctx

        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": options,
            "keep_alive": self.keep_alive,
        }
        if format_json:
            payload["format"] = "json"
        # Per-call timeout can be shorter than self.timeout (which is the
        # warmup/cold-load budget). Tag normalization is best-effort: we
        # don't want the pipeline blocked for 5 minutes on it.
        http_timeout = timeout if timeout is not None else self.timeout
        try:
            r = self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=http_timeout,
            )
            r.raise_for_status()
            data = r.json()
            return (data.get("response") or "").strip()
        except requests.exceptions.ReadTimeout as exc:
            # With keep_alive=2h and a warmup at startup, a ReadTimeout on
            # a warm model almost always means runaway generation rather
            # than cold-loading. Re-warm is still cheap insurance in case
            # Ollama dropped the model under memory pressure. The pipeline
            # falls back to deterministic rules regardless.
            logger.warning(
                "Ollama /api/generate timed out after %ss (best-effort; "
                "falling back to deterministic path). %s",
                http_timeout, exc,
            )
            self._schedule_rewarm()
            return ""
        except Exception as exc:
            logger.warning("Ollama /api/generate failed: %s", exc)
            return ""

    def _schedule_rewarm(self) -> None:
        """
        Fire a background warmup. Idempotent: if a re-warm is already in
        flight, we skip. Never raises into the caller.
        """
        with self._rewarm_lock:
            if self._rewarm_in_flight:
                return
            self._rewarm_in_flight = True

        def _run():
            try:
                self.warmup()
            finally:
                with self._rewarm_lock:
                    self._rewarm_in_flight = False

        t = threading.Thread(target=_run, name="ollama-rewarm", daemon=True)
        t.start()

    def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Ollama ping failed: %s", exc)
            return False

    def warmup(self) -> bool:
        """
        Force Ollama to load the model into VRAM and pin it there via
        keep_alive. Uses a trivial prompt so the load cost is paid up
        front at service startup rather than in the middle of the first
        real call. Returns True if the model is now loaded.
        """
        if not self.enabled:
            return False
        payload = {
            "model": self.model,
            "prompt": "ready",
            "stream": False,
            # Load at the SAME num_ctx the real calls use, so warmup doesn't
            # load a 32k-context model that the first real call then reloads.
            "options": {"num_predict": 1, "temperature": 0.0, "num_ctx": self.num_ctx},
            "keep_alive": self.keep_alive,
        }
        try:
            # Generous timeout -- this is specifically the cold-load path.
            r = self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=max(self.timeout, 600),
            )
            r.raise_for_status()
            logger.info(
                "Ollama warmup succeeded; model %s is resident (keep_alive=%s)",
                self.model, self.keep_alive,
            )
            return True
        except Exception as exc:
            logger.warning("Ollama warmup failed: %s", exc)
            return False

    # ---------- CUE repair ----------------------------------------------

    def repair_cue(self, text: str) -> str:
        prompt = (
            "The following .cue file failed to parse. Fix it and return only "
            "the corrected .cue text:\n\n"
            f"----- BEGIN CUE -----\n{text}\n----- END CUE -----"
        )
        out = self._generate(_CUE_REPAIR_SYSTEM, prompt)
        if not out:
            return ""
        # Some models wrap in code fences despite instructions; strip them.
        out = out.strip()
        if out.startswith("```"):
            lines = out.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            out = "\n".join(lines)
        return out.strip()

    # ---------- album match (library de-dup fallback) -------------------

    def pick_owned_album(self, download_name: str, owned_titles: List[str]) -> Optional[str]:
        """
        Ask the LLM which OWNED album (from `owned_titles`) is the same release
        as the downloaded folder `download_name`. Returns a title copied from
        `owned_titles`, or None. Hallucination-safe: the answer must map back
        to one of the supplied titles (exact or normalized) or we return None.
        """
        if not self.enabled or not download_name or not owned_titles:
            return None
        listing = "\n".join(f"- {t}" for t in owned_titles)
        prompt = (
            f"Downloaded album folder name:\n  {download_name}\n\n"
            f"Albums the user already owns by this artist:\n{listing}\n\n"
            "Which one is the SAME album as the download? "
            "Reply with the exact owned title, or NONE."
        )
        out = self._generate(_ALBUM_MATCH_SYSTEM, prompt, num_predict=64, timeout=60.0)
        ans = (out or "").strip().strip("`").strip().strip('"').strip("'").strip()
        if not ans or ans.upper() == "NONE":
            return None
        matched = None
        for t in owned_titles:
            if t.strip().lower() == ans.lower():
                matched = t
                break
        if matched is None:
            # LLM may have reformatted punctuation -- try a normalized compare.
            try:
                from dedup_downloads import norm_title
                na = norm_title(ans)
                for t in owned_titles:
                    if norm_title(t) == na:
                        matched = t
                        break
            except Exception:  # noqa: BLE001
                pass
        if matched is None:
            return None
        # SAFETY GUARD: reject a match with no meaningful word overlap. A weak
        # model, forced to pick from a list, will sometimes pair two unrelated
        # albums by the same artist (e.g. "Before I Self Destruct" ->
        # "Get Rich or Die Tryin'"). A legit edition/rename shares the core
        # title words; a wrong pairing shares none. False positives here are
        # costly (we'd skip an album you DON'T own), so err toward rejecting.
        def _sig_words(s: str) -> set:
            return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
                    if len(w) >= 4}
        dw, mw = _sig_words(download_name), _sig_words(matched)
        try:
            from dedup_downloads import norm_title
            nd, nm = norm_title(download_name), norm_title(matched)
        except Exception:  # noqa: BLE001
            nd = nm = ""
        contained = bool(nd) and bool(nm) and (nd in nm or nm in nd)
        if not (dw & mw) and not contained:
            logger.info(
                "AI album match rejected (no overlap): %r -> %r",
                download_name, matched,
            )
            return None
        return matched

    # ---------- tag normalization ---------------------------------------

    def normalize_tags(self, plans: List[TagPlan]) -> Optional[List[TagPlan]]:
        if not plans:
            return plans
        input_json = json.dumps([asdict(p) for p in plans], ensure_ascii=False)
        # Be very explicit about shape to reduce wrapped-object responses.
        prompt = (
            "Return a pure JSON array (starts with '[' and ends with ']'), "
            f"same length ({len(plans)}) and same keys as the input. "
            "Do NOT wrap in an object. Do NOT add 'tracks' or 'result' keys.\n\n"
            f"INPUT:\n{input_json}"
        )
        # Output should be roughly the same size as input_json. Add a
        # generous headroom (~2x + 256) for whitespace differences and
        # title-case changes, but cap hard at 8192 so a pathological
        # generation loop can't run forever. Empirically a 20-track
        # album needs ~1.5-2 KB of JSON out.
        estimated_input_tokens = max(1, len(input_json) // 3)  # ~3 chars/token
        token_cap = min(8192, estimated_input_tokens * 2 + 256)
        # Tag normalization is cosmetic. 90 seconds is more than enough
        # for a warm model and quick to fall back from if something's off.
        out = self._generate(
            _TAG_NORMALIZE_SYSTEM,
            prompt,
            format_json=True,
            num_predict=token_cap,
            timeout=90.0,
        )
        if not out:
            return None
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError as exc:
            logger.warning("Ollama returned invalid JSON for tag normalize: %s", exc)
            return None

        parsed = _coerce_to_list(parsed, expected_len=len(plans))
        if not isinstance(parsed, list) or len(parsed) != len(plans):
            # Log a preview so we can see what shape qwen actually returned.
            preview = (out[:300] + "...") if len(out) > 300 else out
            logger.warning(
                "Ollama tag output wrong shape (got %s items, expected %s). "
                "Preview: %s",
                len(parsed) if hasattr(parsed, "__len__") else "?",
                len(plans),
                preview.replace("\n", " "),
            )
            return None

        normalized: List[TagPlan] = []
        for original, patched in zip(plans, parsed):
            if not isinstance(patched, dict):
                normalized.append(original)
                continue
            # Only accept string values for the fields we know; anything else
            # falls back to the original.
            safe = {}
            for field_name in original.__dataclass_fields__:
                val = patched.get(field_name, getattr(original, field_name))
                safe[field_name] = val if isinstance(val, str) else getattr(original, field_name)
            normalized.append(replace(original, **safe))
        return normalized
