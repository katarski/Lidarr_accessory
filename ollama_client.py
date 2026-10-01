"""
Ollama HTTP client.

Questions the deterministic code cannot settle:
  * repair_cue(text)              -> clean .cue text (or "" on failure)
  * parse_artist_album(folder)    -> (artist, album) of an unseparated name
  * pick_owned_album(name, owned) -> the owned album a download is, or None
  * confirm_album_match(...)      -> which candidate album a folder holds

All are best-effort: if Ollama is down or returns garbage, callers fall back
to the deterministic path. Tags are cleaned by rule (tagger.clean_tag), never
here: Lidarr rewrites every imported file's tags from MusicBrainz.

UNAVAILABLE is not "no". When the model could not be asked -- the GPU gate is
closed (see llm_gate.py), the PC is asleep, the call timed out, the model is not
installed, the reply came back empty -- every method returns the UNAVAILABLE
sentinel instead of an answer. It is falsy and equal to "", so a caller that only
falls back keeps working unchanged; a caller that REMEMBERS answers (a cache, a
ledger, a persisted verdict) must test `is_unavailable()` and defer instead of
recording a negative, then ask again on its next pass.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import List, Optional

import requests

import titlematch

logger = logging.getLogger(__name__)


class _AnswerFile(dict):
    """The model's answers, kept across restarts: every container recreate
    emptied the in-memory cache, and 324 asks in four days were 132
    questions. Keyed by question; each entry also names the model that gave
    it, and an answer from another model or older than `max_age_days` is not
    loaded. Written whole (temp file + os.replace) on every new answer --
    a few hundred small entries. UNAVAILABLE is never stored: it is not an
    answer."""

    def __init__(self, path, model: str, max_age_days: float = 30.0):
        super().__init__()
        self.path, self.model = path, model
        self._stamp: dict = {}
        cutoff = time.time() - max_age_days * 86400
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            try:
                kind, text, titles, answer, model_, ts = row
            except (TypeError, ValueError):
                continue
            if model_ == model and float(ts) >= cutoff:
                key = (kind, text, frozenset(titles))
                super().__setitem__(key, answer)
                self._stamp[key] = float(ts)

    def __setitem__(self, key, answer) -> None:
        if is_unavailable(answer):
            return
        super().__setitem__(key, answer)
        self._stamp[key] = time.time()
        rows = [[k[0], k[1], sorted(k[2]), v, self.model, self._stamp.get(k, 0)]
                for k, v in self.items()]
        tmp = "%s.tmp" % self.path
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.debug("LLM answer file %s not written: %s", self.path, exc)


class _Unavailable(str):
    """The model could not be asked. Falsy, equal to "", and one object."""
    _one = None

    def __new__(cls):
        if cls._one is None:
            cls._one = super().__new__(cls, "")
        return cls._one

    def __repr__(self) -> str:
        return "UNAVAILABLE"


UNAVAILABLE = _Unavailable()


def is_unavailable(x) -> bool:
    """True when an LLM result means "could not ask", not "the answer is no"."""
    if x is UNAVAILABLE:
        return True
    if isinstance(x, tuple):
        return any(v is UNAVAILABLE for v in x)
    return False


def keep_alive_seconds(value) -> float:
    """Ollama keep_alive (seconds, or a Go duration like "10m", "2h", "-1")
    as seconds; negative means forever."""
    if value is None or value == "":
        return 600.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    try:
        return float(s)
    except ValueError:
        pass
    total, num = 0.0, ""
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    for ch in s:
        if ch.isdigit() or ch in ".-":
            num += ch
        elif ch in units and num:
            total += float(num) * units[ch]
            num = ""
        else:
            return 600.0
    return total if not num else total + float(num)


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


_IDENTITY_SPLIT_SYSTEM = (
    "You split a music album FOLDER NAME into the artist and the album title. "
    "The folder has no separator between them and may carry rip/format tags "
    "(DTS, DTS_CD, SACD, FLAC), a year, or a catalog number, which you ignore. "
    "Use only words present in the folder name -- never invent or correct "
    "words. Reply with ONLY one line in the form 'ARTIST | ALBUM'. Never explain."
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


# Words in a DOWNLOAD name that mark a DIFFERENT KIND OF RECORD, not another
# edition of the same one: an "Original Broadway Cast Recording" of Jagged Little
# Pill is not Jagged Little Pill, and a karaoke or tribute version is nobody's
# album but its own. Measured 2026-08-19 on 91 real cases: the abliterated 14B
# matched exactly that Broadway case 3 times out of 3, which would deselect a
# download of an album the user does NOT own.
#
# Deliberately narrow. "live", "remixes", "acoustic", "deluxe", "remaster" are
# NOT here - those ARE editions of the same album and production has accepted
# them ("Reprise - Remixes" -> "Reprise", "Live Upon A Blackstar" -> "Wish Upon
# a Blackstar"). Adding one of those would turn good matches into misses.
_RELEASE_TYPE_WORDS = frozenset({
    "karaoke", "tribute", "instrumental", "instrumentals", "cast",
    "broadway", "musical", "covers", "cover", "soundalike", "starring",
})


def _release_type_mismatch(download_name: str, owned_title: str) -> bool:
    """True when the download carries a release-type word the owned album lacks."""
    dw = set(titlematch.tokens(download_name or ""))
    ow = set(titlematch.tokens(owned_title or ""))
    return bool((dw & _RELEASE_TYPE_WORDS) - ow)


def _clip(s: str, limit: int = 70) -> str:
    """One-line, length-capped rendering for log lines."""
    s = " ".join(str(s).split())
    return repr(s if len(s) <= limit else s[:limit] + "...")


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 300,
        enabled: bool = True,
        keep_alive="10m",
        num_ctx: int = 8192,
        think: Optional[bool] = False,
        gate=None,
        load_timeout: float = 180.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.enabled = enabled
        # Context window to load the model with. It must be the SAME as every
        # other client of this Ollama (Home Assistant's agent): a runner loaded
        # at another num_ctx is reloaded on the next request that differs, and
        # a 27B reload costs both sides the model for several seconds and the
        # prompt cache for good. When the model is already resident, the call
        # adopts the resident runner's context instead (see _generate).
        self.num_ctx = int(num_ctx)
        # How long Ollama keeps the model resident after a call, in seconds
        # (Go duration strings are accepted). A LONGER residency somebody else
        # set -- Home Assistant pins the voice model with -1 -- is never
        # shortened by our calls; see _keep_alive_for.
        self.keep_alive_s = keep_alive_seconds(keep_alive)
        self.keep_alive = keep_alive
        # Thinking models (qwen3.x) spend a short num_predict entirely on
        # hidden reasoning and return an EMPTY answer, so every call says
        # think:false. None leaves it out (a model or server that rejects it).
        self.think = think
        self._think_rejected = False
        # Decides whether the model may be asked right now (llm_gate.GpuGate).
        # None: always ask.
        self.gate = gate
        # Read timeout when the call has to load the model first.
        self.load_timeout = float(load_timeout)
        self.session = requests.Session()
        # "Model not installed" is a permanent condition until someone pulls it,
        # so it is reported ONCE with the fix rather than per call.
        self._missing_model_logged = False
        self._ctx_adopt_logged = 0
        self._empty_logged = False
        # Session-lifetime answer cache for album matching: the deselect
        # re-check and lifecycle passes ask the SAME (download, owned-albums)
        # question every cycle, so without this the model re-runs -- and the
        # log re-spams "AI album match rejected" -- forever. Caches ANSWERS,
        # positive and negative -- never an UNAVAILABLE, which is asked again.
        # Cleared only by restart (library growth changes the owned list and so
        # the key, so staleness self-heals).
        self._match_cache: dict = {}

    def use_answer_file(self, path) -> int:
        """Keep this client's answers in `path` across restarts (see
        _AnswerFile). Returns how many were loaded."""
        self._match_cache = _AnswerFile(str(path), self.model)
        return len(self._match_cache)

    # ---------- low-level ------------------------------------------------

    def _keep_alive_for(self, verdict) -> int:
        """Our keep_alive, but never shorter than what the resident model has
        left: a keep_alive on a request REPLACES the runner's expiry, so a
        plain 600 would cut Home Assistant's forever-pinned voice model down
        to ten minutes."""
        mine = self.keep_alive_s
        if mine < 0:
            return -1
        left = getattr(verdict, "expires_in", 0.0) if verdict is not None else 0.0
        if left == float("inf"):
            return -1
        return int(max(mine, left or 0.0))

    def _generate(
        self,
        system: str,
        prompt: str,
        format_json: bool = False,
        num_predict: Optional[int] = None,
        timeout: Optional[float] = None,
        label: str = "generate",
        subject: str = "",
    ) -> str:
        if not self.enabled:
            return UNAVAILABLE
        verdict = None
        if self.gate is not None:
            verdict = self.gate.check()
            if not verdict.open:
                self.gate.note_deferred()
                logger.debug("LLM %s: %s -> DEFERRED (%s)", label,
                             _clip(subject) if subject else "(no subject)",
                             verdict.reason)
                return UNAVAILABLE
        resident = bool(verdict is not None and verdict.resident)
        # Always cap output so a runaway (e.g. qwen getting stuck emitting
        # nested JSON structure under format=json) can't consume the entire
        # HTTP timeout. Callers may tighten further.
        options = {"temperature": 0.1}
        options["num_predict"] = num_predict if num_predict is not None else 2048
        num_ctx = self.num_ctx
        if resident and verdict.ctx and verdict.ctx != num_ctx:
            # Someone (Home Assistant) loaded this model at another context.
            # Asking at ours would reload it under them; ask at theirs.
            if self._ctx_adopt_logged != verdict.ctx:
                self._ctx_adopt_logged = verdict.ctx
                logger.info("LLM: %s is resident at num_ctx %d (configured %d) -- "
                            "using the resident context so it is not reloaded",
                            self.model, verdict.ctx, num_ctx)
            num_ctx = verdict.ctx
        if num_ctx:
            options["num_ctx"] = num_ctx

        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": options,
            "keep_alive": self._keep_alive_for(verdict),
        }
        if self.think is not None and not self._think_rejected:
            payload["think"] = bool(self.think)
        if format_json:
            payload["format"] = "json"
        # Per-call read timeout; a call that must load the model first gets the
        # load budget on top. Connecting gets 3 s: a PC that is asleep does not
        # answer at all, and waiting the whole read budget to learn that held a
        # worker for minutes.
        read = timeout if timeout is not None else self.timeout
        if self.gate is not None and not resident:
            read = max(read, self.load_timeout)
        http_timeout = (3.05, read)
        # Every LLM call gets ONE log line. Without this a successful call is
        # completely invisible (only failures used to log), so there was no way
        # to tell "the LLM answered" apart from "the LLM was never asked" --
        # which made the fallback impossible to reason about in production.
        started = time.monotonic()
        ok = False
        if self.gate is not None:
            self.gate.begin()
        try:
            r = self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=http_timeout,
            )
            if (r.status_code == 400 and "think" in payload
                    and "think" in (r.text or "").lower()):
                # This model/server does not take the think flag at all.
                self._think_rejected = True
                payload.pop("think", None)
                logger.info("LLM: %s does not accept 'think'; asking without it",
                            self.model)
                r = self.session.post(f"{self.base_url}/api/generate",
                                      json=payload, timeout=http_timeout)
            r.raise_for_status()
            data = r.json()
            out = (data.get("response") or "").strip()
            logger.info(
                "LLM %s: %s -> %s (%.1fs)", label,
                _clip(subject) if subject else "(no subject)",
                _clip(out) if out else "EMPTY",
                time.monotonic() - started,
            )
            ok = True
            if not out:
                # An empty reply is not an answer. The usual cause is a thinking
                # model that spent num_predict on hidden reasoning.
                if not self._empty_logged:
                    self._empty_logged = True
                    logger.warning(
                        "LLM %s returned an empty reply%s -- treated as "
                        "unavailable, never as 'no'", self.model,
                        " (it produced only hidden thinking)"
                        if data.get("thinking") else "")
                return UNAVAILABLE
            return out
        except requests.exceptions.ConnectTimeout:
            logger.info("LLM %s: Ollama at %s is not answering -- deferred",
                        label, self.base_url)
            return UNAVAILABLE
        except requests.exceptions.ReadTimeout:
            # The model is busy (one request at a time: Home Assistant's voice
            # turn queues ahead of us) or still loading. Not an answer; the
            # caller asks again later. No re-warm: loading the model is the
            # gate's decision, never a side effect of a timeout.
            logger.warning(
                "LLM %s: no reply within %ss -- deferred (not treated as 'no')",
                label, read)
            return UNAVAILABLE
        except Exception as exc:
            # A 404 from Ollama means THE MODEL IS NOT INSTALLED, not that the
            # endpoint or the server is missing -- and the bare
            # "404 Not Found for url: .../api/generate" it produced read exactly
            # like a wrong URL or a dead service, sending diagnosis in the wrong
            # direction. Seen live: the box answered /api/version fine while
            # /api/tags listed ZERO models, so every call 404'd. Say so once,
            # with the fix, instead of repeating an opaque warning per call.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404 and not self._missing_model_logged:
                self._missing_model_logged = True
                have = self._installed_models()
                logger.error(
                    "Ollama at %s has no model %r -- it is running but that model "
                    "is not installed, so every LLM call will fail. Installed "
                    "models: %s. Fix with: ollama pull %s   (the pipeline keeps "
                    "working; LLM decisions wait until the model is there)",
                    self.base_url, self.model,
                    ", ".join(have) if have else "NONE",
                    self.model)
            elif status != 404 or not self._missing_model_logged:
                logger.warning("Ollama /api/generate failed: %s", exc)
            return UNAVAILABLE
        finally:
            if self.gate is not None:
                self.gate.end(loaded_by_us=ok and not resident,
                              ha_on=bool(verdict is not None and verdict.ha_on))

    def _installed_models(self) -> List[str]:
        """Model names Ollama actually has, for the missing-model diagnostic.
        Empty on any failure -- this only ever decorates an error message."""
        try:
            r = self.session.get("%s/api/tags" % self.base_url, timeout=(3.05, 10))
            r.raise_for_status()
            return [str(m.get("name") or "") for m in (r.json().get("models") or [])]
        except Exception:  # noqa: BLE001
            return []

    def available(self) -> bool:
        """May the model be asked right now? (The gate's verdict, cached.)"""
        if not self.enabled:
            return False
        return True if self.gate is None else self.gate.check().open

    def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=(3.05, 5))
            r.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Ollama ping failed: %s", exc)
            return False

    def warmup(self) -> bool:
        """
        Load the model now -- only when the gate is open, so a warmup can never
        load it onto a GPU somebody else is using. Nothing calls this by
        default any more: the model is loaded by the first question that needs
        it (load on demand), and a warmup at startup held VRAM on a shared PC
        for work that might never come. Returns True if the model is loaded.
        """
        if not self.enabled:
            return False
        verdict = self.gate.check(force=True) if self.gate is not None else None
        if verdict is not None and not verdict.open:
            logger.info("LLM warmup skipped: %s", verdict.reason)
            return False
        payload = {
            "model": self.model,
            "prompt": "ready",
            "stream": False,
            # Load at the SAME num_ctx the real calls use, so warmup doesn't
            # load a 32k-context model that the first real call then reloads.
            "options": {"num_predict": 1, "temperature": 0.0, "num_ctx": self.num_ctx},
            "keep_alive": self._keep_alive_for(verdict),
        }
        if self.think is not None and not self._think_rejected:
            payload["think"] = bool(self.think)
        if self.gate is not None:
            self.gate.begin()
        ok = False
        try:
            r = self.session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=(3.05, max(self.timeout, self.load_timeout)),
            )
            r.raise_for_status()
            ok = True
            logger.info(
                "Ollama warmup succeeded; model %s is resident (keep_alive=%s)",
                self.model, payload["keep_alive"],
            )
            return True
        except Exception as exc:
            logger.warning("Ollama warmup failed: %s", exc)
            return False
        finally:
            if self.gate is not None:
                self.gate.end(loaded_by_us=ok and not (verdict and verdict.resident),
                              ha_on=bool(verdict is not None and verdict.ha_on))

    # ---------- CUE repair ----------------------------------------------

    def repair_cue(self, text: str) -> str:
        prompt = (
            "The following .cue file failed to parse. Fix it and return only "
            "the corrected .cue text:\n\n"
            f"----- BEGIN CUE -----\n{text}\n----- END CUE -----"
        )
        out = self._generate(_CUE_REPAIR_SYSTEM, prompt, label="repair_cue")
        if not out:
            return out          # "" or UNAVAILABLE -- the caller tells them apart
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

    def parse_artist_album(self, folder_name: str):
        """
        Split a tagless folder name with no 'Artist - Album' separator (e.g.
        'Enigma A Posteriori DTS_CD') into (artist, album). Used for DTS-CD /
        SACD rips that carry no tags. Hallucination-safe: EVERY word the model
        returns must appear in the folder name, else we return ("","") -- so it
        can't invent an unrelated artist/album.
        """
        if not folder_name:
            return "", ""
        if not self.enabled:
            return UNAVAILABLE, UNAVAILABLE
        prompt = (
            f"Album folder name with no separators:\n  {folder_name}\n\n"
            "Split it into the recording ARTIST and the ALBUM title. Ignore "
            "format/rip tags like DTS, DTS_CD, SACD, FLAC, bitrate, year, and "
            "catalog numbers.\nReply as EXACTLY one line: ARTIST | ALBUM"
        )
        out = self._generate(_IDENTITY_SPLIT_SYSTEM, prompt,
                             num_predict=64, timeout=60.0,
                             label="parse_artist_album", subject=folder_name)
        if is_unavailable(out):
            return UNAVAILABLE, UNAVAILABLE
        ans = (out or "").strip().strip("`").strip()
        if "|" not in ans:
            return "", ""
        parts = [x.strip().strip('"').strip("'").strip()
                 for x in ans.split("|", 1)]
        artist, album = parts[0], parts[1]
        if not (artist and album):
            return "", ""
        # Words in any script (titlematch): the [a-z0-9] sets this used were
        # EMPTY for a Cyrillic, Greek or CJK folder, so every such split was
        # rejected however right it was.
        folder_words = set(titlematch.tokens(folder_name))
        ret_words = set(titlematch.tokens(artist)) | set(titlematch.tokens(album))
        if not ret_words or not ret_words <= folder_words:
            logger.info(
                "AI identity split rejected (words not all in folder): "
                "%r -> %r / %r", folder_name, artist, album,
            )
            return "", ""
        return artist, album

    def pick_owned_album(self, download_name: str, owned_titles: List[str],
                         evidence=None) -> Optional[str]:
        """
        Ask the LLM which OWNED album (from `owned_titles`) is the same release
        as the downloaded folder `download_name`. Returns a title copied from
        `owned_titles`, or None. Hallucination-safe: the answer must map back
        to one of the supplied titles (exact or normalized) or we return None.

        `evidence`, when given, is called only if the model is about to be
        asked and returns web result titles for the download: the model took
        'Breathe In (2024) Extended Edition' for the owned 'Breathe' without
        knowing 'Breathe In' is an album of its own.
        """
        if not download_name or not owned_titles:
            return None
        cache_key = ("owned", download_name.strip().lower(),
                     frozenset(t.strip().lower() for t in owned_titles))
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]
        # Decided without the model when it cannot change the answer. Any pick
        # is refused below unless the two titles share evidence and the
        # download has no release-type word the owned title lacks; when no
        # owned title passes both, every possible answer ends in None. So
        # None IS the answer -- with the model, without it, or while the GPU
        # is busy (an answer then, not a deferral). 9 in 10 of the 324 asks
        # of 28 Sep-1 Oct came back NONE.
        if not any(titlematch.same_record_evidence(download_name, t)
                   and not _release_type_mismatch(download_name, t)
                   for t in owned_titles):
            logger.info("pick_owned_album: %r vs %d owned -> none, without the "
                        "model (no owned title shares evidence with it)",
                        download_name, len(owned_titles))
            self._match_cache[cache_key] = None
            return None
        if not self.enabled:
            return UNAVAILABLE
        listing = "\n".join(f"- {t}" for t in owned_titles)
        web: List[str] = []
        if evidence is not None:
            try:
                web = [str(t) for t in (evidence() or [])][:8]
            except Exception as exc:  # noqa: BLE001
                logger.debug("pick_owned_album: no web evidence: %s", exc)
        seen = ("Web search results for the download (they can show it is a "
                "record of its own, or an edition of an owned one):\n%s\n\n"
                % "\n".join(f"- {t}" for t in web)) if web else ""
        prompt = (
            f"Downloaded album folder name:\n  {download_name}\n\n"
            f"Albums the user already owns by this artist:\n{listing}\n\n"
            f"{seen}"
            "Which one is the SAME album as the download? "
            "Reply with the exact owned title, or NONE."
        )
        out = self._generate(_ALBUM_MATCH_SYSTEM, prompt, num_predict=64,
                             timeout=60.0, label="pick_owned_album",
                             subject=f"{download_name} vs {len(owned_titles)} owned")
        if is_unavailable(out):
            return UNAVAILABLE      # not asked -- never cached as "none"
        ans = (out or "").strip().strip("`").strip().strip('"').strip("'").strip()
        if not ans or ans.upper() == "NONE":
            self._match_cache[cache_key] = None
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
            logger.info(
                "AI album match rejected (answer not in owned list): %r -> %r",
                download_name, ans,
            )
            self._match_cache[cache_key] = None
            return None
        # SAFETY GUARD: reject a pick the two titles themselves contradict. A
        # model forced to choose from a list will sometimes pair two unrelated
        # albums by the same artist ("Before I Self Destruct" -> "Get Rich or
        # Die Tryin'"), or take a lone letter for a title ("A" -> "The
        # Album"), or one half of a two-in-one release for the whole. The rule
        # is titlematch.same_record_evidence -- words of any script, judged by
        # shape, not by a list of titles. False positives cost an album you
        # DON'T own being skipped, so err toward rejecting.
        why = titlematch.same_record_evidence(download_name, matched)
        if not why:
            logger.info(
                "AI album match rejected (titles share no evidence): %r -> %r",
                download_name, matched,
            )
            self._match_cache[cache_key] = None
            return None
        # SAFETY GUARD 2: a release-type word in the download that the owned
        # title does not have means it is a different record, however similar
        # the titles look. Same cost asymmetry as the overlap guard above:
        # rejecting costs a duplicate download, accepting costs a deselect.
        if _release_type_mismatch(download_name, matched):
            logger.info(
                "AI album match rejected (different release type): %r -> %r",
                download_name, matched,
            )
            self._match_cache[cache_key] = None
            return None
        logger.info("AI album match ACCEPTED (%s): %r -> %r", why,
                    download_name, matched)
        self._match_cache[cache_key] = matched
        return matched

    def confirm_album_match(
        self, context: str, candidate_titles: List[str],
    ) -> Optional[str]:
        """
        Ask the LLM which CANDIDATE album (a Lidarr album title) the described
        download IS, given richer context than a bare folder name: track titles,
        file listing, any tracklist scraped from a sidecar .nfo/.txt. Used by
        the content-identify sweep when title matching alone is inconclusive.

        Hallucination-safe: the answer must map back to one of the supplied
        titles (exact or normalized) or we return None. Unlike
        pick_owned_album there is NO word-overlap guard against the folder
        name -- the whole point of this path is that the folder name does NOT
        match the album title; the caller re-verifies the pick against the
        album's track count before acting on it.
        """
        if not context or not candidate_titles:
            return None
        if not self.enabled:
            return UNAVAILABLE
        cache_key = ("confirm", context.strip().lower()[:500],
                     frozenset(t.strip().lower() for t in candidate_titles))
        if cache_key in self._match_cache:
            return self._match_cache[cache_key]
        listing = "\n".join(f"- {t}" for t in candidate_titles)
        prompt = (
            f"Downloaded album (folder name, files, any tracklist found):\n"
            f"{context}\n\n"
            f"Albums by this artist it could be:\n{listing}\n\n"
            "Which one IS this download? Reply with the exact title from the "
            "list, copied verbatim, or NONE if unsure."
        )
        out = self._generate(
            _ALBUM_MATCH_SYSTEM, prompt, num_predict=64, timeout=60.0,
            label="confirm_album",
            subject=f"{context.splitlines()[0] if context else ''} "
                    f"vs {len(candidate_titles)} candidates")
        if is_unavailable(out):
            return UNAVAILABLE      # not asked -- never cached as "none"
        ans = (out or "").strip().strip("`").strip().strip('"').strip("'").strip()
        result = None
        if ans and ans.upper() != "NONE":
            for t in candidate_titles:
                if t.strip().lower() == ans.lower():
                    result = t
                    break
            if result is None:
                try:
                    from dedup_downloads import norm_title
                    na = norm_title(ans)
                    for t in candidate_titles:
                        if norm_title(t) == na:
                            result = t
                            break
                except Exception:  # noqa: BLE001
                    pass
            if result is None:
                logger.info(
                    "AI album confirm rejected (not in candidate list): %r",
                    ans)
        self._match_cache[cache_key] = result
        return result
