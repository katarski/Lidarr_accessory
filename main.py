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
    put("qbittorrent", "manage_completed", "QBIT_MANAGE_COMPLETED", _as_bool)
    put("qbittorrent", "ai_match", "QBIT_AI_MATCH", _as_bool)
    # AcoustID fingerprint identification (import fallback)
    put("acoustid", "enabled", "ACOUSTID_ENABLED", _as_bool)
    put("acoustid", "api_key", "ACOUSTID_KEY")
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


def qbt_auto_deselect_loop(
    qcfg: Dict[str, Any], lidarr, stop: threading.Event, interval: int,
    download_root: str = "", llm=None,
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
    from qbt_deselect import auto_deselect_pass, torrent_lifecycle_pass

    cadence = max(10, interval)
    category = qcfg.get("category", "") or ""
    pause_scan = _as_bool(qcfg.get("pause_during_scan", True))
    do_deselect = _as_bool(qcfg.get("auto_deselect", False))
    manage_completed = _as_bool(qcfg.get("manage_completed", True))
    # AI fallback for library matching: only used when the deterministic
    # (Lidarr) match misses. Requires an enabled LLM client.
    ai_match = _as_bool(qcfg.get("ai_match", True))
    match_llm = llm if (ai_match and llm is not None and getattr(llm, "enabled", False)) else None
    seen: set = set()
    delay = min(cadence, 10)  # first pass right after startup
    while not stop.wait(delay):
        delay = cadence
        try:
            qbt = QbtClient(qcfg["base_url"], qcfg.get("username", ""),
                            qcfg.get("password", ""))
            if not qbt.login():
                logger.warning("qbt loop: login failed; will retry next pass")
                continue
            if do_deselect:
                acted = auto_deselect_pass(qbt, lidarr, seen, category=category,
                                           emit=logger.info,
                                           pause_during_scan=pause_scan,
                                           llm=match_llm)
                if acted:
                    logger.info("qbt auto-deselect: acted on %d torrent(s)", acted)
            if manage_completed:
                removed, paused = torrent_lifecycle_pass(
                    qbt, download_root, category=category, emit=logger.info
                )
                if removed or paused:
                    logger.info(
                        "qbt lifecycle: removed %d fully-imported, paused %d "
                        "mid-import torrent(s)", removed, paused,
                    )
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


def scan_existing(root: Path, excluded: list, q: "queue.Queue[Path]") -> int:
    """Enqueue any .cue files already present at startup, honoring excludes.

    Walks the tree manually (not Path.rglob) so a transient SMB failure on a
    single directory -- e.g. WinError 59 mid-walk over a UNC share -- logs and
    skips that subtree instead of aborting the whole startup scan.
    """
    count = 0
    skipped = 0
    errored = 0
    started = time.monotonic()
    logger.info("Startup scan: walking %s for existing .cue files...", root)

    def _on_walk_error(err: OSError) -> None:
        nonlocal errored
        errored += 1
        logger.warning("Startup scan: skipping unreadable dir %s: %s",
                       getattr(err, "filename", "?"), err)

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        for name in filenames:
            if not name.lower().endswith(".cue"):
                continue
            cue = Path(dirpath) / name
            if _is_excluded(cue, excluded):
                skipped += 1
                continue
            q.put(cue)
            count += 1
    if errored:
        logger.warning("Startup scan: %d director(y/ies) were unreadable "
                       "(network/SMB errors); their .cue files were NOT queued.",
                       errored)
    elapsed = time.monotonic() - started
    # Always log the result -- silence here made the service look dead
    # after "Watching...". Now you see zero-vs-many immediately.
    logger.info(
        "Startup scan done in %.1fs: queued=%d skipped=%d (excluded folders)",
        elapsed, count, skipped,
    )
    return count


# --- main --------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml", type=Path)
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg = apply_env_overrides(cfg)
    configure_logging(cfg.get("logging", {}))

    watch_cfg = cfg["watch"]
    staging_cfg = cfg["staging"]
    ff_cfg = cfg["ffmpeg"]
    lidarr_cfg = cfg["lidarr"]
    ollama_cfg = cfg["ollama"]

    watch_root = Path(watch_cfg["root"])
    staging_root = Path(staging_cfg["root"])
    watch_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    lidarr = LidarrClient(
        LidarrConfig(
            base_url=lidarr_cfg["base_url"],
            api_key=lidarr_cfg["api_key"],
            library_root_lidarr=lidarr_cfg["library_root_lidarr"],
            library_root_windows=lidarr_cfg["library_root_windows"],
            path_mapping_from=lidarr_cfg["path_mapping"]["from"],
            path_mapping_to=lidarr_cfg["path_mapping"]["to"],
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
            )
            label = f"cloud LLM ({provider}, model={ollama_cfg['model']})"
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
    )

    orch = Orchestrator(orch_cfg, lidarr, ollama_client, acoustid=acoustid_client)
    q: "queue.Queue[Path]" = queue.Queue()
    stop = threading.Event()

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

    pre_existing = scan_existing(watch_root, excluded_dirs, q)
    if pre_existing:
        logger.info("Queued %d pre-existing .cue files at startup", pre_existing)

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
                      ollama_client),
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
