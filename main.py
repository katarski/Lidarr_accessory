"""
Entry point. Run with:

    python main.py --config config.yaml

Watches `watch.root` for new .cue files and dispatches each one to the
Orchestrator via a background worker thread. Handles SIGINT gracefully.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from lidarr import LidarrClient, LidarrConfig
from ollama_client import OllamaClient
from orchestrator import Orchestrator, OrchestratorConfig

logger = logging.getLogger("cue_pipeline")


# --- Config loading -----------------------------------------------------


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _as_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Let container ENV VARS (set from the Unraid template UI) override the
    common config.yaml knobs, so they're editable from the Docker page with
    no file editing. Only vars that are actually set take effect; everything
    else falls back to config.yaml. Keeps the friendly UI and the file in
    sync -- the UI value wins when present.
    """
    cfg.setdefault("lidarr", {})
    cfg.setdefault("ollama", {})
    cfg.setdefault("staging", {})
    cfg.setdefault("qbittorrent", {})
    cfg.setdefault("acoustid", {})
    cfg.setdefault("watch", {})

    def put(section: str, key: str, env: str, cast=str) -> None:
        v = os.environ.get(env)
        if v is None or v == "":
            return
        try:
            cfg[section][key] = cast(v)
        except (TypeError, ValueError):
            logger.warning("Ignoring bad env %s=%r", env, v)

    # Connections
    put("lidarr", "base_url", "LIDARR_URL")
    put("lidarr", "api_key", "LIDARR_API_KEY")
    put("ollama", "enabled", "OLLAMA_ENABLED", _as_bool)
    put("ollama", "base_url", "OLLAMA_URL")
    # LLM provider: "ollama" (local GPU) or "openai" (Gemini/OpenAI/Groq/... , no GPU)
    put("ollama", "provider", "LLM_PROVIDER")
    put("ollama", "base_url", "LLM_BASE_URL")
    put("ollama", "model", "LLM_MODEL")
    put("ollama", "api_key", "LLM_API_KEY")
    # Cleanup behavior (destructive -- nice to see/toggle in the UI)
    put("staging", "delete_source_folder_on_success", "DELETE_SOURCE_FOLDER", _as_bool)
    put("staging", "delete_originals_on_success", "DELETE_ORIGINALS", _as_bool)
    # Matching / force-import
    put("lidarr", "min_match_percent", "MIN_MATCH_PERCENT", int)
    put("lidarr", "pre_split_monitored_gap_only", "MONITORED_GAP_ONLY", _as_bool)
    put("lidarr", "transcode_dts_cd", "TRANSCODE_DTS_CD", _as_bool)
    put("lidarr", "transcode_dsd", "TRANSCODE_DSD", _as_bool)
    put("lidarr", "extract_sacd_iso", "EXTRACT_SACD_ISO", _as_bool)
    put("lidarr", "extract_dvda_iso", "EXTRACT_DVDA_ISO", _as_bool)
    put("lidarr", "extract_archives", "EXTRACT_ARCHIVES", _as_bool)
    # Manual-attention WebUI (#11)
    put("lidarr", "webui_enabled", "WEBUI_ENABLED", _as_bool)
    put("lidarr", "webui_port", "WEBUI_PORT", int)
    put("lidarr", "webui_host", "WEBUI_HOST")
    put("lidarr", "webui_unmonitor_on_resolve", "WEBUI_UNMONITOR_ON_RESOLVE", _as_bool)
    put("lidarr", "webui_held_refresh_seconds", "WEBUI_HELD_REFRESH", int)
    put("lidarr", "held_auto_resolve", "HELD_AUTO_RESOLVE", _as_bool)
    put("lidarr", "held_auto_resolve_max_per_pass", "HELD_AUTO_RESOLVE_MAX", int)
    put("lidarr", "tag_identify_pre_split", "TAG_IDENTIFY_PRE_SPLIT", _as_bool)
    put("lidarr", "prefer_multichannel", "PREFER_MULTICHANNEL", _as_bool)
    put("lidarr", "transcode_lossless_to_flac", "TRANSCODE_LOSSLESS_TO_FLAC", _as_bool)
    put("lidarr", "force_import_on_count_match", "FORCE_IMPORT", _as_bool)
    put("lidarr", "force_import_max_missing_percent", "FORCE_IMPORT_MAX_MISSING", int)
    put("lidarr", "force_import_partial", "FORCE_IMPORT_PARTIAL", _as_bool)
    put("lidarr", "force_import_partial_min_percent", "FORCE_IMPORT_PARTIAL_MIN", int)
    put("lidarr", "force_import_max_extra_percent", "FORCE_IMPORT_MAX_EXTRA", int)
    # Library audit
    put("lidarr", "library_audit_enabled", "LIBRARY_AUDIT_ENABLED", _as_bool)
    # Queue reaper -- clears fully-downloaded-but-stuck torrents from Lidarr's
    # queue (and, by default, from qBit) so mass imports don't pile up.
    put("lidarr", "queue_reaper_enabled", "QUEUE_REAPER_ENABLED", _as_bool)
    put("lidarr", "queue_reaper_interval_seconds", "QUEUE_REAPER_INTERVAL", int)
    put("lidarr", "queue_reaper_grace_minutes", "QUEUE_REAPER_GRACE", int)
    put("lidarr", "queue_reaper_remove_from_client", "QUEUE_REAPER_REMOVE_FROM_CLIENT", _as_bool)
    put("lidarr", "queue_reaper_blocklist", "QUEUE_REAPER_BLOCKLIST", _as_bool)
    # Interactive search + smart-grab (backlog #10)
    put("lidarr", "interactive_search_enabled", "ISEARCH_ENABLED", _as_bool)
    put("lidarr", "interactive_search_interval_seconds", "ISEARCH_INTERVAL", int)
    put("lidarr", "interactive_search_min_missing_days", "ISEARCH_MIN_DAYS", int)
    put("lidarr", "interactive_search_dry_run", "ISEARCH_DRY_RUN", _as_bool)
    put("lidarr", "interactive_search_max_candidates", "ISEARCH_MAX_CANDIDATES", int)
    put("lidarr", "interactive_search_require_lossless", "ISEARCH_REQUIRE_LOSSLESS", _as_bool)
    put("lidarr", "interactive_search_min_title_ratio", "ISEARCH_MIN_TITLE_RATIO", float)
    put("lidarr", "interactive_search_max_albums_per_pass", "ISEARCH_MAX_ALBUMS", int)
    put("lidarr", "interactive_search_artist_level", "ISEARCH_ARTIST_LEVEL", _as_bool)
    put("lidarr", "interactive_search_min_seeders", "ISEARCH_MIN_SEEDERS", int)
    put("lidarr", "interactive_search_seeder_weight", "ISEARCH_SEEDER_WEIGHT", float)
    put("lidarr", "interactive_search_refuse_unofficial", "ISEARCH_REFUSE_UNOFFICIAL", _as_bool)
    put("lidarr", "assembly_enabled", "ASSEMBLY_ENABLED", _as_bool)
    put("lidarr", "assembly_interval_seconds", "ASSEMBLY_INTERVAL", int)
    put("lidarr", "assembly_min_score", "ASSEMBLY_MIN_SCORE", float)
    put("lidarr", "assembly_min_pct", "ASSEMBLY_MIN_PCT", float)
    put("lidarr", "assembly_max_albums_per_pass", "ASSEMBLY_MAX_ALBUMS", int)
    put("lidarr", "external_audit_enabled", "EXTERNAL_AUDIT_ENABLED", _as_bool)
    put("lidarr", "external_audit_interval_seconds", "EXTERNAL_AUDIT_INTERVAL", int)
    put("lidarr", "external_audit_artists_per_pass", "EXTERNAL_AUDIT_ARTISTS", int)
    put("lidarr", "external_audit_include_eps", "EXTERNAL_AUDIT_EPS", _as_bool)
    put("lidarr", "verify_track_titles", "VERIFY_TRACK_TITLES", _as_bool)
    put("lidarr", "verify_track_titles_accept", "VERIFY_TITLES_ACCEPT", float)
    put("lidarr", "verify_track_titles_reject", "VERIFY_TITLES_REJECT", float)
    # Reconcile pass -- import monitored gaps still sitting in downloads.
    put("lidarr", "reconcile_enabled", "RECONCILE_ENABLED", _as_bool)
    put("lidarr", "reconcile_interval_seconds", "RECONCILE_INTERVAL", int)
    put("lidarr", "reconcile_import_mode", "RECONCILE_IMPORT_MODE")
    put("lidarr", "reconcile_require_full_album", "RECONCILE_REQUIRE_FULL_ALBUM", _as_bool)
    put("lidarr", "reconcile_max_files_per_pass", "RECONCILE_MAX_FILES", int)
    put("lidarr", "reconcile_max_probes_per_pass", "RECONCILE_MAX_PROBES", int)
    put("lidarr", "reconcile_recheck_seconds", "RECONCILE_RECHECK", int)
    # Content-based album identification (folder-naming-discrepancy fix).
    put("lidarr", "content_identify", "CONTENT_IDENTIFY", _as_bool)
    put("lidarr", "content_identify_min_coverage",
        "CONTENT_IDENTIFY_MIN_COVERAGE", int)
    put("lidarr", "content_identify_llm", "CONTENT_IDENTIFY_LLM", _as_bool)
    # Purge-imported sweep -- continuously delete download folders whose album
    # Lidarr already has fully (catches re-downloads the pipeline skipped).
    put("lidarr", "purge_imported_enabled", "PURGE_IMPORTED_ENABLED", _as_bool)
    put("lidarr", "purge_imported_interval_seconds", "PURGE_IMPORTED_INTERVAL", int)
    put("lidarr", "purge_imported_dry_run", "PURGE_IMPORTED_DRY_RUN", _as_bool)
    put("lidarr", "purge_imported_min_stable_seconds", "PURGE_IMPORTED_MIN_STABLE", int)
    # qBittorrent selective-download
    put("qbittorrent", "base_url", "QBIT_URL")
    put("qbittorrent", "username", "QBIT_USER")
    put("qbittorrent", "password", "QBIT_PASS")
    put("qbittorrent", "category", "QBIT_CATEGORY")
    put("qbittorrent", "auto_deselect", "QBIT_AUTO_DESELECT", _as_bool)
    put("qbittorrent", "interval_seconds", "QBIT_INTERVAL", int)
    put("qbittorrent", "pause_during_scan", "QBIT_PAUSE_SCAN", _as_bool)
    put("qbittorrent", "deselect_video", "QBIT_DESELECT_VIDEO", _as_bool)
    put("qbittorrent", "redeselect_recheck_seconds", "QBIT_REDESELECT_RECHECK", int)
    put("qbittorrent", "adopt_uncategorised", "QBIT_ADOPT_UNCATEGORISED", _as_bool)
    put("qbittorrent", "stalled_reaper", "QBIT_STALLED_REAPER", _as_bool)
    put("qbittorrent", "stalled_grace_days", "QBIT_STALLED_GRACE_DAYS", int)
    put("qbittorrent", "stalled_blocklist", "QBIT_STALLED_BLOCKLIST", _as_bool)
    put("qbittorrent", "lifecycle_recheck_seconds", "QBIT_LIFECYCLE_RECHECK", int)
    put("qbittorrent", "dead_grab_reaper", "QBIT_DEAD_GRAB_REAPER", _as_bool)
    put("qbittorrent", "dead_grab_grace_minutes", "QBIT_DEAD_GRAB_GRACE_MINUTES", int)
    put("qbittorrent", "dead_grab_grace_hours", "QBIT_DEAD_GRAB_GRACE_HOURS", int)  # legacy
    put("qbittorrent", "dead_grab_blocklist", "QBIT_DEAD_GRAB_BLOCKLIST", _as_bool)
    put("qbittorrent", "manage_completed", "QBIT_MANAGE_COMPLETED", _as_bool)
    put("qbittorrent", "remove_when_library_complete", "QBIT_REMOVE_WHEN_LIBRARY_COMPLETE", _as_bool)
    # #9 recurring .cue re-scan
    put("watch", "cue_rescan_interval_seconds", "CUE_RESCAN_INTERVAL", int)
    put("watch", "sweep_ledger_enabled", "SWEEP_LEDGER_ENABLED", _as_bool)
    put("watch", "sweep_ledger_ttl_seconds", "SWEEP_LEDGER_TTL", int)
    put("qbittorrent", "ai_match", "QBIT_AI_MATCH", _as_bool)
    # AcoustID fingerprint identification (import fallback)
    put("acoustid", "enabled", "ACOUSTID_ENABLED", _as_bool)
    put("acoustid", "api_key", "ACOUSTID_KEY")
    return cfg


def apply_webui_overrides(cfg: Dict[str, Any], path: Path) -> Dict[str, Any]:
    """
    Overlay {section: {key: value}} from the WebUI's Settings tab (written to
    `path`) on top of the config -- HIGHEST precedence (beats env/template), so
    the WebUI is the authoritative control surface. Missing/malformed file is a
    no-op. Guarded so a bad overrides file can never stop the pipeline starting.
    """
    try:
        if not path or not path.exists():
            return cfg
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cfg
        for section, kv in data.items():
            if isinstance(kv, dict):
                cfg.setdefault(section, {})
                for k, v in kv.items():
                    cfg[section][k] = v
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("cue_pipeline").warning(
            "WebUI overrides (%s) ignored: %s", path, exc)
    return cfg


def configure_logging(cfg: Dict[str, Any]) -> None:
    root = logging.getLogger()
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_file = cfg.get("file")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        rot = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=int(cfg.get("max_bytes", 5_242_880)),
            backupCount=int(cfg.get("backup_count", 3)),
            encoding="utf-8",
        )
        rot.setFormatter(fmt)
        root.addHandler(rot)


# --- Watchdog -----------------------------------------------------------


def _resolve_exclude_dirs(
    watch_root: Path, entries, staging_root: Path, staging_mode: str
) -> list:
    """
    Turn config entries (relative or absolute) into resolved absolute paths.
    Missing paths are kept anyway: a folder that doesn't exist yet should
    still be excluded if/when it gets created.

    In "separate" staging mode we also exclude the staging tree so we don't
    re-process our own output. In "in_place" mode the staging sub-folders
    live inside each album's source folder (and get cleaned up on success),
    so there's no persistent tree to exclude -- excluding one would only
    fire on a misconfiguration.

    Hard guard: any entry that equals, contains, or resolves to the watch
    root is dropped with a warning. Otherwise one bad config line silently
    swallows the entire tree (symptom: "queued=0 skipped=<ALL>").
    """
    try:
        watch_root_r = watch_root.resolve(strict=False)
    except OSError:
        watch_root_r = watch_root

    def _safe_add(target: Path, out: list) -> None:
        try:
            tr = target.resolve(strict=False)
        except OSError:
            tr = target
        # Refuse to exclude the watch root or any ancestor of it -- doing
        # so would match every path in the tree and silently drop all work.
        if tr == watch_root_r or tr in watch_root_r.parents:
            logger.warning(
                "Refusing to add '%s' to exclude list: it equals or "
                "contains the watch root %s. Check your config.",
                tr, watch_root_r,
            )
            return
        out.append(tr)

    excluded: list = []
    for raw in list(entries or []):
        if not raw:
            continue
        p = Path(str(raw))
        if not p.is_absolute():
            p = watch_root / p
        _safe_add(p, excluded)

    if staging_mode != "in_place":
        _safe_add(staging_root, excluded)
    return excluded


def _is_excluded(path: Path, excluded: list) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    for ex in excluded:
        # Match if path equals or is inside the excluded dir.
        try:
            if resolved == ex or ex in resolved.parents:
                return True
        except OSError:
            continue
    return False


class CueEventHandler(FileSystemEventHandler):
    def __init__(self, q: "queue.Queue[Path]", excluded: list):
        super().__init__()
        self.q = q
        self.excluded = excluded

    def _maybe_enqueue(self, raw_path: str) -> None:
        p = Path(raw_path)
        if p.suffix.lower() != ".cue":
            return
        if _is_excluded(p, self.excluded):
            return
        logger.info("New CUE detected: %s", p)
        self.q.put(p)

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_enqueue(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._maybe_enqueue(event.dest_path)


# --- Worker -------------------------------------------------------------


def worker_loop(q: "queue.Queue[Path]", orch: Orchestrator, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            cue_path = q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            orch.process(cue_path)
        finally:
            q.task_done()


def heartbeat_loop(
    q: "queue.Queue[Path]",
    observer: Observer,
    stop: threading.Event,
    interval: int,
    watch_root: Path,
) -> None:
    """
    Periodically confirm the watcher is alive. Useful for long-running
    services where nothing happens for hours -- otherwise you can't tell
    from the log whether the process is still healthy.

    Fires a first heartbeat quickly (min(15s, interval)) so a fresh
    startup doesn't look frozen, then settles into the configured cadence.
    """
    first_delay = min(15, max(1, interval))
    cadence = interval
    delay = first_delay
    while not stop.wait(delay):
        try:
            alive = observer.is_alive()
        except Exception:
            alive = False
        logger.info(
            "heartbeat: watcher=%s queue_depth=%d root=%s",
            "alive" if alive else "DEAD",
            q.qsize(),
            watch_root,
        )
        delay = cadence


# --- Cueless sweep ------------------------------------------------------


def cueless_sweep_loop(
    orch: Orchestrator,
    watch_root: Path,
    excluded: list,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Periodically re-scan for pre-split folders that have no .cue file.
    The watcher never sees these (it only fires on .cue events), so
    unless we sweep, they sit forever.

    Minimum cadence is 60s to keep SMB happy. 0 means startup-only and
    this thread never runs.
    """
    cadence = max(60, interval)
    # Small stagger so the first sweep doesn't fight the startup scan
    # for SMB I/O.
    first_delay = min(cadence, 120)
    delay = first_delay
    while not stop.wait(delay):
        try:
            orch.sweep_cueless_pre_split_folders(watch_root, excluded)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cueless sweep thread: %s", exc)
        delay = cadence


def cue_rescan_loop(
    watch_root: Path,
    excluded: list,
    q: "queue.Queue[Path]",
    stop: threading.Event,
    interval: int,
    seen: set,
) -> None:
    """
    #9: periodically re-scan for .cue files the watchdog missed. The observer
    only fires on live create/move events (and can drop/coalesce them on the
    SMB/FUSE PollingObserver), and the startup scan runs only once -- so a
    dropped .cue would otherwise sit unimported until a restart. This re-runs
    the SMB-tolerant scan_existing walk with a shared `seen` set, so ONLY newly
    appeared / previously-missed .cue files are enqueued (idempotent via the
    worker's _skip_seen). Minimum cadence 60s; 0 disables the thread.
    """
    cadence = max(60, interval)
    delay = cadence
    while not stop.wait(delay):
        try:
            scan_existing(watch_root, excluded, q, seen=seen, quiet=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("cue re-scan thread: %s", exc)
        delay = cadence


def library_audit_loop(
    orch: Orchestrator,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Periodically audit the music library on disk against Lidarr's DB, but
    only when the library actually changed since last time (maybe_audit_library
    does the cheap signature check and skips the walk otherwise). First pass
    writes the report in dry-run mode; later passes act on new discrepancies.

    Minimum cadence is 300s. The first run is staggered one cadence out, so
    startup stays light.
    """
    cadence = max(300, interval)
    delay = cadence
    while not stop.wait(delay):
        try:
            orch.maybe_audit_library()
        except Exception as exc:  # noqa: BLE001
            logger.exception("library audit thread: %s", exc)
        delay = cadence


def queue_reaper_loop(
    orch: Orchestrator,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Periodically clear fully-downloaded-but-stuck torrents from Lidarr's
    queue (see Orchestrator.reap_lidarr_queue). The scale valve for mass
    discography imports: without it, compilations / unmatched grabs / title
    mismatches accumulate in the queue forever because Lidarr's native
    cleanup only reaps successful imports. Minimum cadence 60s; first pass
    is staggered one cadence out so startup stays light.
    """
    cadence = max(60, interval)
    delay = cadence
    while not stop.wait(delay):
        try:
            orch.reap_lidarr_queue()
        except Exception as exc:  # noqa: BLE001
            logger.exception("queue reaper thread: %s", exc)
        delay = cadence


def interactive_search_loop(
    orch: Orchestrator,
    qcfg: Dict[str, Any],
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Periodically run the interactive-search + smart-grab pass (backlog #10):
    find monitored albums missing for >N days, grab the best torrent release,
    then verify its contents in qBittorrent before committing (see
    Orchestrator.interactive_search_pass). A fresh QbtClient is built + logged
    in per pass (mirrors the qbt loop); if qBittorrent is unavailable the pass
    still runs but can't content-verify. Minimum cadence 300s; first pass is
    staggered one cadence out so startup stays light.
    """
    from qbittorrent_client import QbtClient

    cadence = max(300, interval)
    delay = cadence
    while not stop.wait(delay):
        try:
            qbt = QbtClient(qcfg.get("base_url", ""), qcfg.get("username", ""),
                            qcfg.get("password", ""))
            if not (qcfg.get("base_url") and qbt.login()):
                logger.warning("interactive search: qBittorrent unavailable "
                               "this pass; grabs can't be content-verified")
                qbt = None
            orch.interactive_search_pass(qbt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("interactive search thread: %s", exc)
        delay = cadence


def _assembly_keep(orch: Orchestrator) -> Optional[set]:
    """Match keys for every source song the album assemblies need (union across
    plans), for the qBittorrent deselect. None when assembly is off."""
    store = getattr(orch, "assembly", None)
    if store is None:
        return None
    try:
        from qbt_deselect import assembly_keep_tails
        return assembly_keep_tails(store.needed_files().keys())
    except Exception:  # noqa: BLE001
        return None


def assembly_loop(
    orch: Orchestrator,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Album assembly planner (requirement e): recompute, for each missing album,
    which of its songs are available inside the compilations/box sets stuck in
    needs-attention -- feeding the Assembly tab's % and missing-song list. Pure
    analysis: it reads tags and writes the plan store, it never moves files.
    Minimum cadence 300s. The FIRST pass runs shortly after startup (not a full
    cadence later) -- otherwise the Assembly tab sits empty for half an hour on
    a fresh start, which reads as "it never populates".
    """
    cadence = max(300, interval)
    delay = min(cadence, 45)
    while not stop.wait(delay):
        try:
            orch.assembly_plan_pass()
        except Exception as exc:  # noqa: BLE001
            logger.exception("assembly thread: %s", exc)
        delay = cadence


def external_audit_loop(
    orch: Orchestrator,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Requirement (c): periodically cross-check Lidarr's album list against
    MusicBrainz and report albums Lidarr has no record of (see
    Orchestrator.external_album_audit_pass). Read-only -- it writes a report,
    it never adds albums to Lidarr. Slow by design: MusicBrainz asks for <=1
    request/second, so each pass checks a handful of artists and the library
    rotates through over days. Minimum cadence 1h; the first pass runs shortly
    after startup so the report exists rather than appearing hours later.
    """
    cadence = max(3600, interval)
    delay = min(cadence, 120)
    while not stop.wait(delay):
        try:
            orch.external_album_audit_pass()
        except Exception as exc:  # noqa: BLE001
            logger.exception("external audit thread: %s", exc)
        delay = cadence


def reconcile_loop(
    orch: Orchestrator,
    watch_root: Path,
    excluded: list,
    stop: threading.Event,
    interval: int,
) -> None:
    """
    Periodically reconcile downloads against Lidarr's live monitored-gap list
    (Orchestrator.reconcile_monitored_gaps): import any downloaded album that
    Lidarr still shows as missing, via Lidarr's own manual-import matcher. This
    is the catch-all that guarantees a monitored album sitting in the download
    tree is never left un-merged -- regardless of a title mis-parse, an earlier
    "no monitored album" skip, or a deleted torrent. Minimum cadence 300s; the
    first pass is staggered one cadence out so startup stays light.
    """
    cadence = max(300, interval)
    delay = cadence
    while not stop.wait(delay):
        try:
            orch.reconcile_monitored_gaps(watch_root, excluded)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconcile thread: %s", exc)
        delay = cadence


def held_curator_loop(
    orch: Orchestrator, stop: threading.Event, interval: int,
) -> None:
    """
    Keep the needs-attention store fresh in the BACKGROUND
    (Orchestrator.curate_held_pass) so the WebUI serves it instantly instead
    of regenerating every entry per page load. First pass runs shortly after
    startup so the dashboard is warm, then every `interval` seconds.
    """
    cadence = max(60, interval)
    delay = min(30, cadence)
    while not stop.wait(delay):
        try:
            n = orch.curate_held_pass()
            if n:
                logger.info("held curator: refreshed %d entr%s",
                            n, "y" if n == 1 else "ies")
        except Exception as exc:  # noqa: BLE001
            logger.exception("held curator thread: %s", exc)
        delay = cadence


def qbt_auto_deselect_loop(
    qcfg: Dict[str, Any], lidarr, stop: threading.Event, interval: int,
    download_root: str = "", llm=None, q: "Optional[queue.Queue[Path]]" = None,
    assembly_keep_provider=None,
) -> None:
    """
    Poll qBittorrent on a schedule. Two jobs per pass:

      1. Deselect (priority 0) the albums an INCOMPLETE music torrent contains
         that Lidarr already has, so a discography grab only downloads what's
         missing (opt-in: qbittorrent.auto_deselect). New torrents are
         pause-scanned-resumed so owned albums never start downloading.

      2. Manage COMPLETED music torrents by move-state (opt-in:
         qbittorrent.manage_completed): pause a torrent while the pipeline is
         mid-import, and remove it (with data) once every album has been moved
         into the library. See qbt_deselect.torrent_lifecycle_pass.

    Cadence is kept short (min 10s). Login is re-checked each pass.
    """
    from qbittorrent_client import QbtClient
    from qbt_deselect import (
        auto_deselect_pass, torrent_lifecycle_pass, dead_grab_reaper_pass,
        stalled_grab_reaper_pass, adopt_uncategorised,
    )

    cadence = max(10, interval)
    category = qcfg.get("category", "") or ""
    pause_scan = _as_bool(qcfg.get("pause_during_scan", True))
    do_deselect = _as_bool(qcfg.get("auto_deselect", False))
    manage_completed = _as_bool(qcfg.get("manage_completed", True))
    deselect_video = _as_bool(qcfg.get("deselect_video", True))  # #4
    reap_useless = _as_bool(qcfg.get("reap_useless_torrents", True))
    # Dead-grab reaper: remove lidarr-category torrents stuck at ~0% past a
    # grace window (default 2 days) + blocklist so Lidarr re-searches a live
    # (lossless-preferred) alternative. Addresses the dead-torrent flood.
    dead_grab_reaper = _as_bool(qcfg.get("dead_grab_reaper", True))
    # Grace is now in MINUTES; fall back to the old *_hours key if present.
    if qcfg.get("dead_grab_grace_minutes") is not None:
        dead_grab_grace_minutes = int(qcfg.get("dead_grab_grace_minutes") or 360)
    else:
        dead_grab_grace_minutes = int(qcfg.get("dead_grab_grace_hours", 6) or 6) * 60
    dead_grab_blocklist = _as_bool(qcfg.get("dead_grab_blocklist", True))
    # Stalled-but-partly-downloaded reaper: no progress for N days -> salvage
    # anything usable, then drop the torrent (its data too when nothing is).
    stalled_reaper = _as_bool(qcfg.get("stalled_reaper", True))
    stalled_grace_days = int(qcfg.get("stalled_grace_days", 3) or 3)
    stalled_blocklist = _as_bool(qcfg.get("stalled_blocklist", True))
    # Adopt uncategorised music torrents so they are managed like the rest.
    adopt_uncategorised_on = _as_bool(qcfg.get("adopt_uncategorised", True))
    # AI fallback for library matching: only used when the deterministic
    # (Lidarr) match misses. Requires an enabled LLM client.
    ai_match = _as_bool(qcfg.get("ai_match", True))
    match_llm = llm if (ai_match and llm is not None and getattr(llm, "enabled", False)) else None
    # #5: also clean up a completed torrent whose album(s) Lidarr imported itself
    # (copy/hardlink import leaves files on disk, so the disk-empty check misses it).
    remove_when_library_complete = _as_bool(
        qcfg.get("remove_when_library_complete", True))
    # Remove a completed torrent once every album Lidarr WANTS from it is owned
    # (treat compilations/live/unknown leftovers as not-needed, deleted with it).
    reap_completed_wanted_only = _as_bool(
        qcfg.get("reap_completed_wanted_only", True))
    remove_min_stable = int(qcfg.get("remove_min_stable_seconds", 300) or 300)
    seen: set = set()
    # {hash: last_plan_ts} so an INCOMPLETE torrent is re-planned every
    # `redeselect_recheck` seconds -- catches albums that become owned AFTER the
    # torrent was first planned (Lidarr import, reconcile fill, another torrent
    # completing) instead of downloading them forever. 0 disables re-checks.
    planned_deselect: dict = {}
    redeselect_recheck = int(qcfg.get("redeselect_recheck_seconds", 1800) or 0)
    completed_seen: set = set()   # #8a: torrents we've already kicked to process
    # {hash: (disk_signature, ts)}: a completed torrent whose disk state hasn't
    # changed isn't re-planned (Lidarr+LLM) until this interval passes.
    lifecycle_checked: dict = {}
    lifecycle_recheck = int(qcfg.get("lifecycle_recheck_seconds", 21600) or 21600)

    def _enqueue_folder_cues(folder: str) -> None:
        """#8a: a music torrent just completed -> enqueue any .cue in its
        download folder so the pipeline starts immediately (no wait for the
        watcher/sweep). Cueless folders stay owned by the cueless sweep."""
        if q is None or not folder:
            return
        try:
            for dirpath, _d, files in os.walk(folder):
                for name in files:
                    if name.lower().endswith(".cue"):
                        cue = Path(dirpath) / name
                        q.put(cue)
                        logger.info("qbt complete -> queued %s", cue)
        except OSError as exc:
            logger.debug("qbt complete: cue scan of %s failed: %s", folder, exc)

    def _asm_keep_now():
        if assembly_keep_provider is None:
            return None
        try:
            return assembly_keep_provider()
        except Exception:  # noqa: BLE001
            return None

    delay = min(cadence, 10)  # first pass right after startup
    while not stop.wait(delay):
        delay = cadence
        try:
            qbt = QbtClient(qcfg["base_url"], qcfg.get("username", ""),
                            qcfg.get("password", ""))
            if not qbt.login():
                logger.warning("qbt loop: login failed; will retry next pass")
                continue
            # Pull in music torrents that have no category at all -- otherwise
            # every pass below skips them and they download in full.
            if adopt_uncategorised_on and category:
                try:
                    adopt_uncategorised(qbt, category, emit=logger.info)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("adopt uncategorised: %s", exc)
            if do_deselect:
                # Songs an album assembly needs must stay selected even when
                # their compilation looks fully "owned"/unwanted (requirement e).
                asm_keep = None
                if assembly_keep_provider is not None:
                    try:
                        asm_keep = assembly_keep_provider()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("assembly keep-set unavailable: %s", exc)
                acted = auto_deselect_pass(qbt, lidarr, seen, category=category,
                                           emit=logger.info,
                                           pause_during_scan=pause_scan,
                                           llm=match_llm,
                                           deselect_video=deselect_video,
                                           reap_useless=reap_useless,
                                           planned=planned_deselect,
                                           recheck_seconds=redeselect_recheck,
                                           assembly_keep=asm_keep)
                if acted:
                    logger.info("qbt auto-deselect: acted on %d torrent(s)", acted)
            if manage_completed:
                removed, paused = torrent_lifecycle_pass(
                    qbt, download_root, category=category, emit=logger.info,
                    lidarr=lidarr, llm=match_llm,
                    remove_when_library_complete=remove_when_library_complete,
                    min_stable_seconds=remove_min_stable,
                    on_complete=_enqueue_folder_cues if q is not None else None,
                    completed_seen=completed_seen,
                    wanted_only=reap_completed_wanted_only,
                    checked=lifecycle_checked,
                    recheck_seconds=lifecycle_recheck,
                )
                if removed or paused:
                    logger.info(
                        "qbt lifecycle: removed %d fully-imported, paused %d "
                        "mid-import torrent(s)", removed, paused,
                    )
            if dead_grab_reaper:
                try:
                    dead_grab_reaper_pass(
                        qbt, lidarr, category=category,
                        grace_seconds=dead_grab_grace_minutes * 60,
                        blocklist=dead_grab_blocklist, emit=logger.info,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dead-grab reaper: %s", exc)
            if stalled_reaper:
                try:
                    stalled_grab_reaper_pass(
                        qbt, lidarr, category=category,
                        grace_seconds=max(3600, stalled_grace_days * 86400),
                        blocklist=stalled_blocklist, emit=logger.info,
                        assembly_keep=_asm_keep_now(), llm=match_llm,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stalled reaper: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("qbt loop thread: %s", exc)


def purge_imported_loop(
    lidarr, watch_root: str, excluded: list, stop: threading.Event,
    interval: int, delete: bool, min_stable: int, llm=None,
) -> None:
    """
    Periodically delete download folders whose album Lidarr ALREADY has fully.

    This is the CONTINUOUS form of the process-time `pre_check_library`: the
    pipeline only re-checks a folder when it (re)processes it, so a re-download
    that lands in the in-memory `_skip_seen` set is never re-examined until a
    restart, and redundant copies accumulate on disk. This sweep closes that
    gap. It is safe by construction (see purge_imported_downloads): only the
    fully-in-library category is removed, disc subfolders / partial /
    unmatched / video are never touched, and in-flight folders are skipped.

    With delete=False it runs as a DRY RUN -- it only logs what it would
    remove, so you can watch a few passes before arming it.
    """
    from dedup_downloads import purge_imported_downloads, human

    cadence = max(300, interval)
    excluded_res: list = []
    for e in excluded:
        try:
            excluded_res.append(Path(e).resolve(strict=False))
        except OSError:
            pass
    # AI is intentionally NOT used here: this sweep DELETES, so it stays on the
    # deterministic (exact + word-subset, track-count-guarded) match only.
    delay = min(cadence, 120)  # first sweep shortly after startup
    while not stop.wait(delay):
        delay = cadence
        try:
            deleted, freed, dupes = purge_imported_downloads(
                lidarr, watch_root, excluded_res,
                delete=delete, llm=None, min_stable_seconds=min_stable,
                emit=logger.info,
            )
            if dupes:
                if delete:
                    logger.info(
                        "purge sweep: %d album(s) already fully in library -- "
                        "deleted %d, freed %s", len(dupes), deleted, human(freed),
                    )
                else:
                    logger.info(
                        "purge sweep (DRY RUN): %d album(s) already fully in "
                        "library would be deleted -- set purge_imported_dry_run "
                        "= false to arm.", len(dupes),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("purge sweep thread: %s", exc)


# --- Startup scan -------------------------------------------------------


def scan_existing(root: Path, excluded: list, q: "queue.Queue[Path]",
                  seen: "Optional[set]" = None, quiet: bool = False) -> int:
    """Enqueue any .cue files present under `root`, honoring excludes.

    Walks the tree manually (not Path.rglob) so a transient SMB failure on a
    single directory -- e.g. WinError 59 mid-walk over a UNC share -- logs and
    skips that subtree instead of aborting the whole scan.

    Used both at startup and by the recurring #9 re-scan. When `seen` is given,
    a .cue already in it is skipped and each newly enqueued cue is added to it,
    so the recurring re-scan only ever enqueues .cue files the watcher missed
    (not everything, every pass). `quiet` downgrades the summary logs to debug
    for the recurring caller.
    """
    count = 0
    skipped = 0
    errored = 0
    started = time.monotonic()
    _log = logger.debug if quiet else logger.info
    _log("%s scan: walking %s for .cue files...",
         "re-" if quiet else "Startup", root)

    def _on_walk_error(err: OSError) -> None:
        nonlocal errored
        errored += 1
        logger.warning("cue scan: skipping unreadable dir %s: %s",
                       getattr(err, "filename", "?"), err)

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        for name in filenames:
            if not name.lower().endswith(".cue"):
                continue
            cue = Path(dirpath) / name
            if _is_excluded(cue, excluded):
                skipped += 1
                continue
            if seen is not None:
                key = str(cue)
                if key in seen:
                    continue
                seen.add(key)
            q.put(cue)
            count += 1
    if errored:
        logger.warning("cue scan: %d director(y/ies) were unreadable "
                       "(network/SMB errors); their .cue files were NOT queued.",
                       errored)
    elapsed = time.monotonic() - started
    if count or not quiet:
        # Startup always logs; the recurring re-scan only logs when it actually
        # found a missed cue (otherwise it would spam like the old audit line).
        (logger.info if (count and quiet) else _log)(
            "%s scan done in %.1fs: queued=%d skipped=%d (excluded)",
            "re-" if quiet else "Startup", elapsed, count, skipped,
        )
    return count


# --- main --------------------------------------------------------------


def _check_mounts(watch_root: Path, library_root: Path) -> None:
    """Shout at startup if a mount is wrong, instead of letting the pipeline
    infer the consequences.

    Docker creates a bind-mount source that does not exist on the host, so a
    mistyped path in the Unraid template arrives as an existing, EMPTY
    directory -- indistinguishable from "you have nothing to do" until passes
    start acting on that emptiness. Both roots being non-empty is the normal
    state; either being empty is worth a banner.
    """
    for label, root, why in (
        ("watch root (/downloads)", watch_root,
         "completed torrents will look already-imported and nothing can import"),
        ("library root (/music)", library_root,
         "every album will look missing from the library"),
    ):
        if not str(root):
            continue
        try:
            if not root.is_dir():
                logger.error("MOUNT PROBLEM: %s %s does not exist -- %s",
                             label, root, why)
            elif not any(root.iterdir()):
                logger.error(
                    "MOUNT PROBLEM: %s %s is EMPTY. If that is unexpected the "
                    "host path in the container template is wrong (Docker "
                    "silently creates a missing bind-mount source) -- %s",
                    label, root, why)
            else:
                logger.info("%s %s ok", label, root)
        except OSError as exc:
            logger.error("MOUNT PROBLEM: cannot read %s %s: %s", label, root, exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml", type=Path)
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg = apply_env_overrides(cfg)
    webui_overrides_path = Path(args.config).parent / "webui_overrides.json"
    cfg = apply_webui_overrides(cfg, webui_overrides_path)
    configure_logging(cfg.get("logging", {}))

    # Permissions: create every file/dir group- AND other-writable (0666/0777),
    # matching Unraid's "Docker Safe New Permissions" convention. Set process-
    # wide and BEFORE any worker/subprocess starts so ffmpeg / 7z / sacd_extract
    # / dvda2wav all inherit it -- otherwise pipeline-created FLACs land 0644
    # (owner 99:100) and can't be deleted over SMB. Overridable via FILE_UMASK
    # (octal, e.g. "002" for group-writable only).
    try:
        _umask = int(str(os.environ.get("FILE_UMASK", "0")).strip() or "0", 8)
    except ValueError:
        _umask = 0
    os.umask(_umask)

    watch_cfg = cfg["watch"]
    staging_cfg = cfg["staging"]
    ff_cfg = cfg["ffmpeg"]
    lidarr_cfg = cfg["lidarr"]
    ollama_cfg = cfg["ollama"]

    watch_root = Path(watch_cfg["root"])
    staging_root = Path(staging_cfg["root"])
    watch_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    _check_mounts(watch_root, Path(lidarr_cfg.get("library_root_windows") or ""))

    lidarr = LidarrClient(
        LidarrConfig(
            base_url=lidarr_cfg["base_url"],
            api_key=lidarr_cfg["api_key"],
            library_root_lidarr=lidarr_cfg["library_root_lidarr"],
            library_root_windows=lidarr_cfg["library_root_windows"],
            path_mapping_from=lidarr_cfg["path_mapping"]["from"],
            path_mapping_to=lidarr_cfg["path_mapping"]["to"],
            manualimport_timeout=int(
                lidarr_cfg.get("manualimport_query_timeout_seconds", 180)),
        )
    )

    ollama_client = None
    if ollama_cfg.get("enabled", True):
        provider = str(ollama_cfg.get("provider", "ollama")).lower()
        if provider in ("openai", "gemini", "cloud", "openai-compatible"):
            from cloud_llm import CloudLLMClient
            ollama_client = CloudLLMClient(
                base_url=ollama_cfg["base_url"],
                model=ollama_cfg["model"],
                api_key=str(ollama_cfg.get("api_key", "")),
                timeout=int(ollama_cfg.get("timeout_seconds", 60)),
                enabled=True,
                rpm=int(ollama_cfg.get("rpm", 10)),
                max_wait_seconds=float(ollama_cfg.get("max_wait_seconds", 30)),
                max_retries=int(ollama_cfg.get("max_retries", 3)),
                cooldown_seconds=float(ollama_cfg.get("cooldown_seconds", 900)),
            )
            label = (f"cloud LLM ({provider}, model={ollama_cfg['model']}, "
                     f"{ollama_cfg.get('rpm', 10)} req/min)")
        else:
            ollama_client = OllamaClient(
                base_url=ollama_cfg["base_url"],
                model=ollama_cfg["model"],
                timeout=int(ollama_cfg.get("timeout_seconds", 300)),
                enabled=True,
                keep_alive=str(ollama_cfg.get("keep_alive", "30m")),
                num_ctx=int(ollama_cfg.get("num_ctx", 8192)),
            )
            label = f"Ollama ({ollama_cfg['base_url']}, model={ollama_cfg['model']})"
        if ollama_client.ping():
            logger.info("LLM reachable: %s", label)
            # Local Ollama benefits from a warmup (VRAM preload); cloud is
            # a no-op. Non-fatal either way.
            if ollama_cfg.get("warmup_on_start", True):
                ollama_client.warmup()
        else:
            logger.warning("LLM unreachable (%s) -- continuing without LLM fallback", label)

    # Optional AcoustID fingerprint identifier (best-effort; identifies a
    # pre-split folder whose tags can't, so it can still be imported).
    acoustid_client = None
    ac_cfg = cfg.get("acoustid") or {}
    if _as_bool(ac_cfg.get("enabled", False)) and ac_cfg.get("api_key"):
        from acoustid_client import AcoustIDClient
        acoustid_client = AcoustIDClient(
            api_key=str(ac_cfg.get("api_key", "")),
            fpcalc=str(ac_cfg.get("fpcalc", "fpcalc")),
            enabled=True,
            max_rps=float(ac_cfg.get("max_rps", 3.0)),
            timeout=int(ac_cfg.get("timeout_seconds", 20)),
        )
        if acoustid_client._have_fpcalc():
            logger.info("AcoustID fingerprint identify: enabled (%.1f req/s)",
                        float(ac_cfg.get("max_rps", 3.0)))
        else:
            logger.warning("AcoustID enabled but fpcalc missing -- disabled")
            acoustid_client = None

    if lidarr.ping():
        logger.info("Lidarr reachable at %s", lidarr_cfg["base_url"])
    else:
        logger.warning("Lidarr unreachable -- API-based import will fail; manual fallback only")

    ledger_file_cfg = staging_cfg.get("ledger_file")
    ledger_path = Path(ledger_file_cfg) if ledger_file_cfg else None

    # Queue-reaper grace clock lives next to the other /config state files.
    _reaper_state_cfg = lidarr_cfg.get("queue_reaper_state_file")
    if _reaper_state_cfg:
        reaper_state_path = Path(_reaper_state_cfg)
    elif ledger_path is not None:
        reaper_state_path = ledger_path.parent / "queue_reaper.json"
    elif lidarr_cfg.get("library_audit_report_file"):
        reaper_state_path = (
            Path(lidarr_cfg["library_audit_report_file"]).parent
            / "queue_reaper.json"
        )
    else:
        reaper_state_path = None
    # Interactive-search "how long missing" clock lives next to the other
    # /config state files (mirrors the reaper state path resolution).
    _isearch_state_cfg = lidarr_cfg.get("interactive_search_state_file")
    if _isearch_state_cfg:
        isearch_state_path = Path(_isearch_state_cfg)
    elif ledger_path is not None:
        isearch_state_path = ledger_path.parent / "interactive_search.json"
    elif lidarr_cfg.get("library_audit_report_file"):
        isearch_state_path = (
            Path(lidarr_cfg["library_audit_report_file"]).parent
            / "interactive_search.json"
        )
    else:
        isearch_state_path = None
    # External album cross-check (requirement c): the MusicBrainz-vs-Lidarr gap
    # report lives beside the other /config state files.
    _ext_audit_cfg = lidarr_cfg.get("external_audit_file")
    if _ext_audit_cfg:
        external_audit_path = Path(_ext_audit_cfg)
    elif isearch_state_path is not None:
        external_audit_path = isearch_state_path.parent / "external_album_audit.json"
    else:
        external_audit_path = None
    # Persistent cueless-sweep ledger -- beside the other /config state files.
    _led_cfg = lidarr_cfg.get("sweep_ledger_file") or watch_cfg.get("sweep_ledger_file")
    if _led_cfg:
        sweep_ledger_path = Path(_led_cfg)
    elif isearch_state_path is not None:
        sweep_ledger_path = isearch_state_path.parent / "sweep_seen.json"
    else:
        sweep_ledger_path = None
    # Album-assembly store (requirement e) -- beside the other /config state.
    _asm_cfg = lidarr_cfg.get("assembly_file")
    if _asm_cfg:
        assembly_path = Path(_asm_cfg)
    elif isearch_state_path is not None:
        assembly_path = isearch_state_path.parent / "assembly.json"
    else:
        assembly_path = None
    # Manual-attention WebUI (#11): held-items state lives next to the other
    # /config state files (mirrors the reaper/isearch state path resolution).
    _held_state_cfg = lidarr_cfg.get("held_items_file") or staging_cfg.get("held_items_file")
    if _held_state_cfg:
        held_items_path = Path(_held_state_cfg)
    elif ledger_path is not None:
        held_items_path = ledger_path.parent / "held_items.json"
    elif lidarr_cfg.get("library_audit_report_file"):
        held_items_path = (
            Path(lidarr_cfg["library_audit_report_file"]).parent / "held_items.json"
        )
    else:
        held_items_path = None
    heartbeat_seconds = int(staging_cfg.get("heartbeat_seconds", 600) or 0)

    orch_cfg = OrchestratorConfig(
        audio_extensions=list(watch_cfg.get("audio_extensions", [
            ".flac", ".ape", ".wv", ".wav", ".m4a", ".alac", ".aiff", ".aif",
            ".tak", ".tta", ".dsf", ".dff", ".mp3", ".ogg", ".opus", ".wma", ".shn",
        ])),
        stable_seconds=int(watch_cfg.get("stable_seconds", 20)),
        staging_root=staging_root,
        lidarr_grace_seconds=int(staging_cfg.get("lidarr_grace_seconds", 90)),
        ffmpeg_binary=ff_cfg.get("binary", "ffmpeg"),
        flac_compression_level=int(ff_cfg.get("flac_compression_level", 8)),
        ffmpeg_extra_args=list(ff_cfg.get("extra_args", [])),
        library_root_windows=Path(lidarr_cfg["library_root_windows"]),
        album_folder_template=staging_cfg.get("album_folder_template", "{album} ({year})"),
        staging_mode=str(staging_cfg.get("mode", "in_place")).lower(),
        filename_template=staging_cfg.get(
            "filename_template",
            "{artist} - {album} - {number:02d} - {title}.{ext}",
        ),
        min_match_percent=float(lidarr_cfg.get("min_match_percent", 60)),
        cleanup_lidarr_queue=bool(lidarr_cfg.get("cleanup_lidarr_queue", True)),
        manual_import_timeout_seconds=int(
            lidarr_cfg.get("manual_import_timeout_seconds", 300)
        ),
        delete_originals_on_success=bool(
            staging_cfg.get("delete_originals_on_success", True)
        ),
        delete_source_folder_on_success=bool(
            staging_cfg.get("delete_source_folder_on_success", True)
        ),
        pre_check_lidarr_library=bool(
            lidarr_cfg.get("pre_check_library", True)
        ),
        pre_split_monitored_gap_only=bool(
            lidarr_cfg.get("pre_split_monitored_gap_only", True)
        ),
        transcode_dts_cd=bool(
            lidarr_cfg.get("transcode_dts_cd", True)
        ),
        transcode_dsd=bool(
            lidarr_cfg.get("transcode_dsd", True)
        ),
        extract_sacd_iso=bool(
            lidarr_cfg.get("extract_sacd_iso", True)
        ),
        extract_dvda_iso=bool(
            lidarr_cfg.get("extract_dvda_iso", True)
        ),
        extract_archives=bool(
            lidarr_cfg.get("extract_archives", True)
        ),
        tag_identify_pre_split=bool(
            lidarr_cfg.get("tag_identify_pre_split", True)
        ),
        prefer_multichannel=bool(
            lidarr_cfg.get("prefer_multichannel", True)
        ),
        transcode_lossless_to_flac=bool(
            lidarr_cfg.get("transcode_lossless_to_flac", True)
        ),
        force_import_on_count_match=bool(
            lidarr_cfg.get("force_import_on_count_match", True)
        ),
        force_import_max_missing_percent=int(
            lidarr_cfg.get("force_import_max_missing_percent", 10)
        ),
        force_import_partial=bool(
            lidarr_cfg.get("force_import_partial", True)
        ),
        force_import_partial_min_percent=int(
            lidarr_cfg.get("force_import_partial_min_percent", 50)
        ),
        force_import_max_extra_percent=int(
            lidarr_cfg.get("force_import_max_extra_percent", 25)
        ),
        delete_cue_if_pre_split=bool(
            staging_cfg.get("delete_cue_if_pre_split", True)
        ),
        strict_import_only=bool(
            lidarr_cfg.get("strict_import_only", False)
        ),
        wait_for_lidarr=bool(
            lidarr_cfg.get("wait_for_lidarr", True)
        ),
        lidarr_availability_wait_seconds=int(
            lidarr_cfg.get("availability_wait_seconds", 10800)
        ),
        watch_root=watch_root,
        ledger_file=ledger_path,
        webui_enabled=bool(lidarr_cfg.get("webui_enabled", True)),
        webui_host=str(lidarr_cfg.get("webui_host", "0.0.0.0")),
        webui_port=int(lidarr_cfg.get("webui_port", 8830)),
        webui_unmonitor_on_resolve=bool(
            lidarr_cfg.get("webui_unmonitor_on_resolve", True)),
        webui_held_refresh_seconds=int(
            lidarr_cfg.get("webui_held_refresh_seconds", 300)),
        held_auto_resolve=bool(lidarr_cfg.get("held_auto_resolve", True)),
        held_auto_resolve_max_per_pass=int(
            lidarr_cfg.get("held_auto_resolve_max_per_pass", 6)),
        qbt_url=str((cfg.get("qbittorrent") or {}).get("base_url", "") or ""),
        qbt_user=str((cfg.get("qbittorrent") or {}).get("username", "") or ""),
        qbt_pass=str((cfg.get("qbittorrent") or {}).get("password", "") or ""),
        log_file=(Path((cfg.get("logging") or {}).get("file"))
                  if (cfg.get("logging") or {}).get("file") else None),
        flac2mp3_log=str(
            os.environ.get("FLAC2MP3_LOG")
            or lidarr_cfg.get("flac2mp3_log", "/lidarr-logs/flac2mp3.txt")),
        container_name=str(lidarr_cfg.get("container_name", "cue_pipeline") or "cue_pipeline"),
        held_items_file=held_items_path,
        webui_overrides_file=webui_overrides_path,
        sweep_cueless_pre_split=bool(
            watch_cfg.get("sweep_cueless_pre_split", False)
        ),
        sweep_interval_seconds=int(
            watch_cfg.get("sweep_interval_seconds", 0)
        ),
        sweep_min_stable_seconds=int(
            watch_cfg.get("sweep_min_stable_seconds", 300)
        ),
        verify_library_after_import=bool(
            lidarr_cfg.get("verify_library_after_import", True)
        ),
        lidarr_verify_timeout_seconds=int(
            lidarr_cfg.get("verify_timeout_seconds", 1800)
        ),
        library_audit_enabled=bool(
            lidarr_cfg.get("library_audit_enabled", False)
        ),
        library_audit_on_startup=bool(
            lidarr_cfg.get("library_audit_on_startup", False)
        ),
        library_audit_interval_seconds=int(
            lidarr_cfg.get("library_audit_interval_seconds", 0)
        ),
        library_audit_skip_unchanged=bool(
            lidarr_cfg.get("library_audit_skip_unchanged", True)
        ),
        library_audit_report_file=(
            Path(lidarr_cfg["library_audit_report_file"])
            if lidarr_cfg.get("library_audit_report_file") else None
        ),
        queue_reaper_enabled=bool(
            lidarr_cfg.get("queue_reaper_enabled", False)
        ),
        queue_reaper_interval_seconds=int(
            lidarr_cfg.get("queue_reaper_interval_seconds", 600)
        ),
        queue_reaper_grace_minutes=int(
            lidarr_cfg.get("queue_reaper_grace_minutes", 30)
        ),
        queue_reaper_remove_from_client=bool(
            lidarr_cfg.get("queue_reaper_remove_from_client", True)
        ),
        queue_reaper_blocklist=bool(
            lidarr_cfg.get("queue_reaper_blocklist", False)
        ),
        queue_reaper_state_file=reaper_state_path,
        interactive_search_enabled=bool(
            lidarr_cfg.get("interactive_search_enabled", False)
        ),
        interactive_search_interval_seconds=int(
            lidarr_cfg.get("interactive_search_interval_seconds", 3600)
        ),
        interactive_search_min_missing_days=int(
            lidarr_cfg.get("interactive_search_min_missing_days", 3)
        ),
        interactive_search_dry_run=bool(
            lidarr_cfg.get("interactive_search_dry_run", True)
        ),
        interactive_search_max_candidates=int(
            lidarr_cfg.get("interactive_search_max_candidates", 5)
        ),
        interactive_search_require_lossless=bool(
            lidarr_cfg.get("interactive_search_require_lossless", True)
        ),
        interactive_search_min_title_ratio=float(
            lidarr_cfg.get("interactive_search_min_title_ratio", 0.55)
        ),
        interactive_search_max_albums_per_pass=int(
            lidarr_cfg.get("interactive_search_max_albums_per_pass", 15)
        ),
        interactive_search_artist_level=bool(
            lidarr_cfg.get("interactive_search_artist_level", True)
        ),
        interactive_search_min_seeders=int(
            lidarr_cfg.get("interactive_search_min_seeders", 1)
        ),
        interactive_search_seeder_weight=float(
            lidarr_cfg.get("interactive_search_seeder_weight", 1.0)
        ),
        interactive_search_refuse_unofficial=bool(
            lidarr_cfg.get("interactive_search_refuse_unofficial", True)
        ),
        verify_track_titles=bool(
            lidarr_cfg.get("verify_track_titles", True)
        ),
        verify_track_titles_accept=float(
            lidarr_cfg.get("verify_track_titles_accept", 0.60)
        ),
        verify_track_titles_reject=float(
            lidarr_cfg.get("verify_track_titles_reject", 0.25)
        ),
        external_audit_enabled=bool(
            lidarr_cfg.get("external_audit_enabled", True)
        ),
        external_audit_interval_seconds=int(
            lidarr_cfg.get("external_audit_interval_seconds", 21600)
        ),
        external_audit_artists_per_pass=int(
            lidarr_cfg.get("external_audit_artists_per_pass", 10)
        ),
        external_audit_recheck_seconds=int(
            lidarr_cfg.get("external_audit_recheck_seconds", 604800)
        ),
        external_audit_include_eps=bool(
            lidarr_cfg.get("external_audit_include_eps", False)
        ),
        external_audit_file=external_audit_path,
        assembly_enabled=bool(lidarr_cfg.get("assembly_enabled", True)),
        assembly_interval_seconds=int(
            lidarr_cfg.get("assembly_interval_seconds", 1800)),
        assembly_min_score=float(lidarr_cfg.get("assembly_min_score", 0.87)),
        assembly_require_artist=bool(
            lidarr_cfg.get("assembly_require_artist", True)),
        assembly_min_pct=float(lidarr_cfg.get("assembly_min_pct", 10.0)),
        assembly_max_source_files=int(
            lidarr_cfg.get("assembly_max_source_files", 20000)),
        assembly_max_albums_per_pass=int(
            lidarr_cfg.get("assembly_max_albums_per_pass", 150)),
        assembly_hunt_per_pass=int(
            lidarr_cfg.get("assembly_hunt_per_pass", 2)),
        assembly_extra_source_dirs=list(
            lidarr_cfg.get("assembly_extra_source_dirs") or []),
        assembly_file=assembly_path,
        sweep_ledger_enabled=bool(
            watch_cfg.get("sweep_ledger_enabled",
                          lidarr_cfg.get("sweep_ledger_enabled", True))),
        sweep_ledger_file=sweep_ledger_path,
        sweep_ledger_ttl_seconds=int(
            watch_cfg.get("sweep_ledger_ttl_seconds",
                          lidarr_cfg.get("sweep_ledger_ttl_seconds", 86400))),
        interactive_search_cooldown_seconds=int(
            lidarr_cfg.get("interactive_search_cooldown_seconds", 43200)
        ),
        interactive_search_state_file=isearch_state_path,
        reconcile_enabled=bool(lidarr_cfg.get("reconcile_enabled", True)),
        reconcile_interval_seconds=int(
            lidarr_cfg.get("reconcile_interval_seconds", 1800)
        ),
        reconcile_import_mode=str(
            lidarr_cfg.get("reconcile_import_mode", "copy")
        ).lower(),
        reconcile_require_full_album=bool(
            lidarr_cfg.get("reconcile_require_full_album", True)
        ),
        reconcile_max_files_per_pass=int(
            lidarr_cfg.get("reconcile_max_files_per_pass", 500)
        ),
        reconcile_max_probes_per_pass=int(
            lidarr_cfg.get("reconcile_max_probes_per_pass", 60)
        ),
        reconcile_recheck_seconds=int(
            lidarr_cfg.get("reconcile_recheck_seconds", 21600)
        ),
        content_identify=bool(lidarr_cfg.get("content_identify", True)),
        content_identify_min_coverage=int(
            lidarr_cfg.get("content_identify_min_coverage", 60)
        ),
        content_identify_llm=bool(lidarr_cfg.get("content_identify_llm", True)),
    )

    orch = Orchestrator(orch_cfg, lidarr, ollama_client, acoustid=acoustid_client,
                        raw_cfg=cfg)
    q: "queue.Queue[Path]" = queue.Queue()
    stop = threading.Event()
    # WebUI Converter tab (library browser + AAC/MP3/Opus conversion) --
    # file-backed library tree cache + conversion job manager on the orch so
    # the webui handler reaches them through `actions`.
    try:
        from converter import ConvertManager, LibraryTree
        lib_root = Path(orch_cfg.library_root_windows)
        cache_path = (Path(args.config).parent / "library_tree.json")
        orch.library_tree = LibraryTree(lib_root, cache_path)
        orch.converter = ConvertManager(
            lib_root, ffmpeg=orch_cfg.ffmpeg_binary, llm=ollama_client,
            # Overwrite mode needs Lidarr (to rescan a replaced file) and the
            # path prefix Lidarr sees for the library root.
            lidarr=lidarr, lidarr_root=lidarr_cfg["library_root_lidarr"],
            tree=orch.library_tree)
        orch.work_queue = q   # cue-split queue, shown in the Converter tab

        def _lib_rescan_loop():
            interval = max(300, int(os.environ.get("LIBRARY_RESCAN", "3600")))
            while not stop.wait(60 if orch.library_tree._scanned_ts == 0
                                else interval):
                try:
                    orch.library_tree.maybe_scan(interval)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("library rescan: %s", exc)

        threading.Thread(target=_lib_rescan_loop, daemon=True,
                         name="cue-lib-rescan").start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Converter tab disabled: %s", exc)

    excluded_dirs = _resolve_exclude_dirs(
        watch_root,
        watch_cfg.get("exclude_dirs", []),
        staging_root,
        str(staging_cfg.get("mode", "in_place")).lower(),
    )
    if excluded_dirs:
        logger.info(
            "Excluded folders: %s",
            ", ".join(str(p) for p in excluded_dirs),
        )

    handler = CueEventHandler(q, excluded_dirs)

    # Watchdog's default Observer uses ReadDirectoryChangesW on Windows.
    # Over a UNC share (\\host\share\...), change notifications depend on
    # the SMB server forwarding them -- which is unreliable in practice
    # and frequently delivers nothing at all. PollingObserver walks the
    # tree every `poll_interval` seconds instead; slower but reliable.
    #
    # Config: watch.observer = "auto" | "native" | "polling"
    #   auto    -> polling if watch_root looks like a UNC path, else native
    #   polling -> always polling (safe default for SMB/NFS)
    #   native  -> always ReadDirectoryChangesW (fast, local filesystems only)
    observer_mode = str(watch_cfg.get("observer", "auto")).lower()
    poll_interval = int(watch_cfg.get("poll_interval_seconds", 30))
    watch_root_str = str(watch_root)
    is_unc = watch_root_str.startswith("\\\\") or watch_root_str.startswith("//")
    if observer_mode == "polling" or (observer_mode == "auto" and is_unc):
        observer = PollingObserver(timeout=poll_interval)
        why = ("watch root is UNC/SMB; native notifications are unreliable there"
               if is_unc else
               "polling configured (reliable on FUSE/network mounts like /mnt/user)")
        logger.info("Using PollingObserver (every %ds) -- %s.", poll_interval, why)
    else:
        observer = Observer()
        logger.info("Using native Observer (ReadDirectoryChangesW).")

    observer.schedule(handler, str(watch_root), recursive=True)
    observer.start()

    worker = threading.Thread(
        target=worker_loop, args=(q, orch, stop), daemon=True, name="cue-worker"
    )
    worker.start()

    heartbeat_thread = None
    if heartbeat_seconds > 0:
        heartbeat_thread = threading.Thread(
            target=heartbeat_loop,
            args=(q, observer, stop, heartbeat_seconds, watch_root),
            daemon=True,
            name="cue-heartbeat",
        )
        heartbeat_thread.start()

    # Shared with the #9 recurring re-scan so it only enqueues .cue files the
    # watchdog later misses -- not everything, every pass.
    cue_seen: set = set()
    pre_existing = scan_existing(watch_root, excluded_dirs, q, seen=cue_seen)
    if pre_existing:
        logger.info("Queued %d pre-existing .cue files at startup", pre_existing)

    # #9: recurring .cue re-scan so a .cue the watcher dropped is picked up
    # without a restart. Reuses the polling cadence (watch.poll_interval_seconds
    # -> cue_rescan_interval_seconds), 0 disables.
    cue_rescan_interval = int(watch_cfg.get(
        "cue_rescan_interval_seconds", 120) or 0)
    if cue_rescan_interval > 0:
        threading.Thread(
            target=cue_rescan_loop,
            args=(watch_root, excluded_dirs, q, stop, cue_rescan_interval, cue_seen),
            daemon=True, name="cue-rescan",
        ).start()
        logger.info("Cue re-scan: enabled (every %ds)", max(60, cue_rescan_interval))

    # Optional: hand off pre-split folders that have NO .cue file at all
    # (the watcher only fires on .cue events, so those are invisible to it).
    sweep_thread = None
    if orch_cfg.sweep_cueless_pre_split:
        logger.info(
            "Cueless sweep: starting startup pass in BACKGROUND (min_stable=%ds, "
            "interval=%ds) -- so it never blocks the audit/deselect/reaper threads",
            orch_cfg.sweep_min_stable_seconds,
            orch_cfg.sweep_interval_seconds,
        )

        def _startup_cueless_sweep() -> None:
            try:
                orch.sweep_cueless_pre_split_folders(watch_root, excluded_dirs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("cueless sweep (startup): %s", exc)

        # Run as a daemon thread: with a large download backlog this pass can
        # take a very long time (each ManualImport may wait for Lidarr), and it
        # used to run synchronously here -- which delayed the queue reaper and
        # qBit auto-deselect threads (started further down) from ever starting.
        threading.Thread(
            target=_startup_cueless_sweep,
            daemon=True,
            name="cue-cueless-startup",
        ).start()

        if orch_cfg.sweep_interval_seconds > 0:
            sweep_thread = threading.Thread(
                target=cueless_sweep_loop,
                args=(
                    orch,
                    watch_root,
                    excluded_dirs,
                    stop,
                    orch_cfg.sweep_interval_seconds,
                ),
                daemon=True,
                name="cue-cueless-sweep",
            )
            sweep_thread.start()
            logger.info(
                "Cueless sweep: periodic thread started (interval=%ds)",
                orch_cfg.sweep_interval_seconds,
            )

    # --- Reconcile: import monitored gaps still sitting in downloads ---
    # Catch-all safety net so a downloaded album can never stay un-merged
    # while Lidarr shows it missing (title mis-parse, prior skip, deleted
    # torrent). Driven by Lidarr's own gap list + manual-import matcher, so
    # it re-checks every pass and needs no per-artist configuration.
    if orch_cfg.reconcile_enabled:
        reconcile_thread = threading.Thread(
            target=reconcile_loop,
            args=(orch, watch_root, excluded_dirs, stop,
                  orch_cfg.reconcile_interval_seconds),
            daemon=True,
            name="cue-reconcile",
        )
        reconcile_thread.start()
        logger.info(
            "Reconcile: enabled (every %ds, import_mode=%s, max_files/pass=%d)",
            max(300, orch_cfg.reconcile_interval_seconds),
            orch_cfg.reconcile_import_mode,
            orch_cfg.reconcile_max_files_per_pass,
        )
    else:
        logger.info("Reconcile: disabled")

    # --- Album assembly planner (requirement e) -------------------------
    if getattr(orch_cfg, "assembly_enabled", False) and orch.assembly is not None:
        threading.Thread(
            target=assembly_loop,
            args=(orch, stop, orch_cfg.assembly_interval_seconds),
            daemon=True,
            name="cue-assembly",
        ).start()
        logger.info(
            "Album assembly: enabled (every %ds, min_score=%.2f, min_pct=%.0f%%, "
            "store=%s)",
            max(300, orch_cfg.assembly_interval_seconds),
            orch_cfg.assembly_min_score, orch_cfg.assembly_min_pct,
            assembly_path)
    else:
        logger.info("Album assembly: disabled")

    # --- External album cross-check (requirement c) ---------------------
    # Ask MusicBrainz which studio albums an artist has that Lidarr never
    # listed, and report them. Read-only; nothing is added to Lidarr.
    if getattr(orch_cfg, "external_audit_enabled", False):
        threading.Thread(
            target=external_audit_loop,
            args=(orch, stop, orch_cfg.external_audit_interval_seconds),
            daemon=True,
            name="cue-external-audit",
        ).start()
        logger.info(
            "External album audit: enabled (every %ds, %d artist(s)/pass, "
            "re-check every %dd, report=%s)",
            max(3600, orch_cfg.external_audit_interval_seconds),
            orch_cfg.external_audit_artists_per_pass,
            max(1, orch_cfg.external_audit_recheck_seconds // 86400),
            external_audit_path)
    else:
        logger.info("External album audit: disabled")

    # --- Library audit: disk vs Lidarr (scheduled, change-gated) -------
    # Runs on a schedule only -- NOT at startup, not coupled to anything.
    # Each cycle first checks a cheap library dir-signature and skips the
    # whole walk unless something actually changed.
    audit_thread = None
    audit_enabled = (
        orch_cfg.library_audit_enabled or orch_cfg.library_audit_on_startup
    )
    if audit_enabled:
        if not orch_cfg.library_audit_report_file:
            logger.warning(
                "Library audit enabled but library_audit_report_file is not "
                "set -- audit disabled. Configure a report file path."
            )
        elif orch_cfg.library_audit_interval_seconds <= 0:
            logger.warning(
                "Library audit enabled but library_audit_interval_seconds<=0 "
                "-- nothing to schedule. Set an interval (>=300s)."
            )
        else:
            audit_thread = threading.Thread(
                target=library_audit_loop,
                args=(orch, stop, orch_cfg.library_audit_interval_seconds),
                daemon=True,
                name="cue-library-audit",
            )
            audit_thread.start()
            logger.info(
                "Library audit: scheduled every %ds (change-gated=%s, report=%s)",
                max(300, orch_cfg.library_audit_interval_seconds),
                orch_cfg.library_audit_skip_unchanged,
                orch_cfg.library_audit_report_file,
            )

    # --- qBittorrent loop: auto-deselect + completed-torrent lifecycle -
    qbt_thread = None
    qbt_cfg = cfg.get("qbittorrent") or {}
    qbt_deselect_on = _as_bool(qbt_cfg.get("auto_deselect", False))
    qbt_manage_on = _as_bool(qbt_cfg.get("manage_completed", True))
    if qbt_deselect_on or qbt_manage_on:
        if not qbt_cfg.get("base_url"):
            logger.warning(
                "qBittorrent features on but base_url is not set -- skipping. "
                "Configure base_url/username/password."
            )
        else:
            interval = int(qbt_cfg.get("interval_seconds", 30) or 30)
            qbt_thread = threading.Thread(
                target=qbt_auto_deselect_loop,
                args=(qbt_cfg, lidarr, stop, interval, str(watch_root),
                      ollama_client, q,
                      # Fresh each pass: the union of source songs every album
                      # assembly needs, so the deselect keeps them.
                      (lambda: _assembly_keep(orch))),
                daemon=True,
                name="cue-qbt",
            )
            qbt_thread.start()
            logger.info(
                "qBittorrent loop: enabled (every %ds, deselect=%s, "
                "manage_completed=%s, pause-scan=%s, %s, category=%r)",
                max(10, interval), qbt_deselect_on, qbt_manage_on,
                _as_bool(qbt_cfg.get("pause_during_scan", True)),
                qbt_cfg["base_url"], qbt_cfg.get("category", ""),
            )

    # --- Purge-imported sweep (opt-in) ---------------------------------
    # Continuously delete download folders whose album Lidarr already has
    # fully -- the background counterpart to the process-time pre_check that
    # also reclaims re-downloads the pipeline skipped via _skip_seen.
    purge_thread = None
    lidarr_cfg = cfg.get("lidarr") or {}
    if _as_bool(lidarr_cfg.get("purge_imported_enabled", False)):
        purge_interval = int(lidarr_cfg.get("purge_imported_interval_seconds", 1800) or 1800)
        purge_dry_run = _as_bool(lidarr_cfg.get("purge_imported_dry_run", True))
        purge_min_stable = int(lidarr_cfg.get("purge_imported_min_stable_seconds", 300) or 300)
        purge_thread = threading.Thread(
            target=purge_imported_loop,
            args=(lidarr, str(watch_root), excluded_dirs, stop,
                  purge_interval, not purge_dry_run, purge_min_stable),
            daemon=True,
            name="cue-purge-imported",
        )
        purge_thread.start()
        logger.info(
            "Purge-imported sweep: enabled (every %ds, %s, min_stable=%ds)",
            max(300, purge_interval),
            "DRY RUN" if purge_dry_run else "DELETING",
            purge_min_stable,
        )

    # --- Queue reaper (opt-in, legacy) ---------------------------------
    # Time-based clearing of stuck Lidarr queue rows. SUPERSEDED by the qBit
    # completed-torrent lifecycle above, which is disk-aware and won't delete a
    # torrent whose files haven't actually been imported yet. We only run the
    # old reaper when the lifecycle is OFF, so the two can't fight (the reaper
    # would otherwise delete a partially-imported torrent's remaining data).
    reaper_thread = None
    if orch_cfg.queue_reaper_enabled and not qbt_manage_on:
        reaper_thread = threading.Thread(
            target=queue_reaper_loop,
            args=(orch, stop, orch_cfg.queue_reaper_interval_seconds),
            daemon=True,
            name="cue-queue-reaper",
        )
        reaper_thread.start()
        logger.info(
            "Queue reaper: enabled (every %ds, grace=%dm, "
            "remove_from_client=%s, blocklist=%s)",
            max(60, orch_cfg.queue_reaper_interval_seconds),
            orch_cfg.queue_reaper_grace_minutes,
            orch_cfg.queue_reaper_remove_from_client,
            orch_cfg.queue_reaper_blocklist,
        )
    elif orch_cfg.queue_reaper_enabled and qbt_manage_on:
        logger.info(
            "Queue reaper: superseded by qBittorrent completed-torrent "
            "lifecycle (disk-aware) -- legacy reaper not started."
        )

    # --- Interactive search + smart-grab (opt-in, backlog #10) ---------
    # Proactively grab monitored albums Lidarr has left missing for >N days,
    # verifying each candidate torrent's contents in qBittorrent before
    # committing. Ships disabled + dry-run by default (no surprise downloads).
    isearch_thread = None
    if orch_cfg.interactive_search_enabled:
        isearch_thread = threading.Thread(
            target=interactive_search_loop,
            args=(orch, cfg.get("qbittorrent") or {}, stop,
                  orch_cfg.interactive_search_interval_seconds),
            daemon=True,
            name="cue-isearch",
        )
        isearch_thread.start()
        logger.info(
            "Interactive search: enabled (every %ds, min_missing=%dd, %s, "
            "max_candidates=%d, require_lossless=%s)",
            max(300, orch_cfg.interactive_search_interval_seconds),
            orch_cfg.interactive_search_min_missing_days,
            "DRY RUN" if orch_cfg.interactive_search_dry_run else "GRABBING",
            orch_cfg.interactive_search_max_candidates,
            orch_cfg.interactive_search_require_lossless,
        )

    # --- Manual-attention WebUI (backlog #11) --------------------------
    # A small dashboard of items the pipeline couldn't finish, where the user
    # can copy the stuck path, "keep existing" (discard held files) or "move
    # held" (move them into the library + rescan). WebUI-only, no push alerts.
    if orch_cfg.webui_enabled and orch.held is not None:
        try:
            from webui import run_webui
            webui_thread = threading.Thread(
                target=run_webui,
                args=(orch.held, orch, orch_cfg.webui_host,
                      orch_cfg.webui_port, stop),
                daemon=True,
                name="cue-webui",
            )
            webui_thread.start()
            logger.info(
                "WebUI: enabled on http://%s:%d (held-items dashboard, "
                "state=%s)", orch_cfg.webui_host, orch_cfg.webui_port,
                held_items_path,
            )
            curator_thread = threading.Thread(
                target=held_curator_loop,
                args=(orch, stop,
                      int(getattr(orch_cfg, "webui_held_refresh_seconds", 300))),
                daemon=True,
                name="cue-held-curator",
            )
            curator_thread.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebUI: failed to start (%s) -- continuing without it", exc)
    else:
        logger.info("WebUI: disabled")

    def handle_signal(signum, _frame):
        logger.info("Signal %s received, shutting down.", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Watching %s", watch_root)
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        observer.stop()
        observer.join(timeout=5)
        worker.join(timeout=5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
