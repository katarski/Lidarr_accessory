"""
Held-items store for the manual-attention WebUI (backlog #11).

When the pipeline can't finish an import on its own -- Lidarr rejected it, the
library didn't reflect a "successful" import, a manual move failed, etc. -- it
leaves the files on disk and records the fact here. The WebUI (webui.py) reads
this store to show the user what's stuck, and calls back into the orchestrator
to resolve each item ("keep existing" = discard the held files and trust
Lidarr's current library; "move held" = move the held files into the library
and rescan).

State is a single JSON file (default /config/held_items.json), guarded by a
lock and written atomically. Entries are keyed by a stable hash of the source
folder, so the same stuck folder recorded on every pass upserts one entry
rather than piling up. A later SUCCESS for the same folder clears its entry, so
the list self-heals when a transient failure (e.g. Lidarr restarting) later
resolves.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _entry_id(source_path: str) -> str:
    return hashlib.sha1(source_path.encode("utf-8", "replace")).hexdigest()[:12]


class HeldStore:
    """Thread-safe, JSON-backed list of items awaiting a manual decision."""

    # Inside batch(), unsaved changes are checkpointed at most this often.
    _CHECKPOINT_SECONDS = 30.0

    def __init__(self, path: Optional[Path], clock=time.time) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._clock = clock
        self._items: Dict[str, Dict[str, Any]] = {}
        self._tl = threading.local()
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ---- persistence --------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                self._items = {
                    e["id"]: e for e in items
                    if isinstance(e, dict) and e.get("id")
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("HeldStore: could not load %s: %s", self.path, exc)

    def _save_locked(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {"items": list(self._items.values())}
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
            self._last_save = self._clock()
        except OSError as exc:
            logger.warning("HeldStore: could not save %s: %s", self.path, exc)

    def _changed_locked(self) -> None:
        """A mutation happened. Written at once, except inside this thread's
        batch(), where it is written at the end (and checkpointed)."""
        self._dirty = True
        if (getattr(self._tl, "depth", 0)
                and self._clock() - self._last_save < self._CHECKPOINT_SECONDS):
            return
        self._save_locked()

    @contextlib.contextmanager
    def batch(self):
        """Many mutations, one write. The curator refreshed every entry and
        rewrote the whole file for each one -- 47,000 rewrites of a 200 KB
        file in the log window. Other threads' changes stay write-through
        (and carry this batch's pending ones with them)."""
        depth = getattr(self._tl, "depth", 0)
        self._tl.depth = depth + 1
        try:
            yield self
        finally:
            self._tl.depth = depth
            if depth == 0:
                with self._lock:
                    if self._dirty:
                        self._save_locked()

    # ---- mutations ----------------------------------------------------
    def add(
        self,
        source_path: str,
        artist: str = "",
        album: str = "",
        tracks: int = 0,
        reason: str = "",
        outcome: str = "",
        kind: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Upsert an item needing attention, keyed by its source folder."""
        source_path = str(source_path)
        eid = _entry_id(source_path)
        with self._lock:
            existing = self._items.get(eid, {})
            now = self._clock()
            entry = {
                "id": eid,
                "source_path": source_path,
                "artist": artist or existing.get("artist", ""),
                "album": album or existing.get("album", ""),
                "tracks": int(tracks or existing.get("tracks", 0) or 0),
                "reason": (reason or existing.get("reason", ""))[:500],
                "outcome": outcome or existing.get("outcome", ""),
                "kind": kind or existing.get("kind", ""),
                "details": details if details is not None
                else existing.get("details", {}),
                "created": existing.get("created") or now,
                "updated": now,
                "seen_count": int(existing.get("seen_count", 0)) + 1,
            }
            self._items[eid] = entry
            self._changed_locked()
        return entry

    @staticmethod
    def _sans_ts(v: Any) -> Any:
        return ({k: x for k, x in v.items() if k != "_ts"}
                if isinstance(v, dict) else v)

    def update(self, eid: str, **fields: Any) -> bool:
        """Merge fields into an entry (no seen_count bump). Used to cache the
        computed 'existing library album' summary. Returns True if updated.
        A refresh that changes nothing but its timestamp is kept in memory
        and not written."""
        with self._lock:
            e = self._items.get(eid)
            if e is None:
                return False
            changed = any(self._sans_ts(e.get(k)) != self._sans_ts(v)
                          for k, v in fields.items())
            e.update(fields)
            if changed:
                self._changed_locked()
        return True

    def remove(self, eid: str) -> bool:
        with self._lock:
            gone = self._items.pop(eid, None) is not None
            if gone:
                self._changed_locked()
        return gone

    def remove_by_path(self, source_path: str) -> bool:
        return self.remove(_entry_id(str(source_path)))

    @staticmethod
    def _populated(d: Path) -> bool:
        """`d` exists and holds something. An empty mount point looks exactly
        like a library whose every folder was deleted."""
        try:
            return d.is_dir() and any(True for _ in d.iterdir())
        except OSError:
            return False

    def prune_missing(self, roots=()) -> int:
        """
        Drop entries whose source folder no longer exists on disk -- e.g. a
        folder that was recorded as failed but has since been imported and
        cleaned up, or one the user resolved by hand. Keeps the dashboard
        truthful (only items with files actually still on disk to act on).
        Returns the number removed.

        A missing path is believed only when the disk it lives on answered:
        the root it is under (`roots`: the watch/staging/library mounts) is
        there and not empty, or -- outside every root -- its parent is. A
        container started with an empty /downloads mount otherwise deleted
        the whole list, and nothing ever re-created it.
        """
        root_paths = [Path(r) for r in (roots or ()) if r]
        mounted: Dict[str, bool] = {}

        def answered(p: Path) -> bool:
            for r in root_paths:
                try:
                    p.relative_to(r)
                except ValueError:
                    continue
                if str(r) not in mounted:
                    mounted[str(r)] = self._populated(r)
                return mounted[str(r)]
            return self._populated(p.parent)

        removed = 0
        with self._lock:
            for eid in list(self._items):
                sp = self._items[eid].get("source_path", "")
                try:
                    if sp and not Path(sp).exists() and answered(Path(sp)):
                        del self._items[eid]
                        removed += 1
                except OSError:
                    continue
            if removed:
                self._changed_locked()
        return removed

    # ---- reads --------------------------------------------------------
    def get(self, eid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            e = self._items.get(eid)
            return dict(e) if e else None

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(e) for e in self._items.values()),
                key=lambda e: e.get("updated", 0),
                reverse=True,
            )
