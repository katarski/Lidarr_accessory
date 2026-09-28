"""
Folders a worker is processing RIGHT NOW, for the whole process.

The CUE worker, the cueless sweep, the qBittorrent lifecycle, the reapers and
the imported-downloads purge all act on /downloads from their own threads, and
nothing told one that another was mid-way through a folder: the lifecycle
removed the `Feels Like Christmas` torrent -- data and all -- four minutes
into the CUE worker's import of it, and a startup sweep and the first periodic
sweep handed the same 27 folders to Lidarr twice.

A worker claims a folder for the duration of its work (claim / release, or the
`held` context manager). Claims are re-entrant for the thread that holds them,
so a hand-off inside a CUE job does not block itself. Anything that deletes
asks `busy(path)` first and defers while it answers a folder: a claimed folder
equal to the path, inside it (a torrent's content holding the album), or
containing it.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

_lock = threading.Lock()
_freed = threading.Condition(_lock)
_held: Dict[str, Tuple[int, int]] = {}   # path -> (owner thread id, depth)


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p))).replace("\\", "/").rstrip("/")


def claim(path, wait: bool = False) -> bool:
    """Claim `path` for this thread. False when another thread holds it --
    or, with wait=True, block until that thread releases it."""
    key, me = _norm(path), threading.get_ident()
    with _lock:
        while _conflict(key, me) is not None:
            if not wait:
                return False
            _freed.wait()
        owner = _held.get(key)
        _held[key] = (me, (owner[1] if owner else 0) + 1)
        return True


def _conflict(key: str, me: int) -> Optional[str]:
    """A folder another thread holds that equals, contains or sits inside
    `key` (a sweep on an album and a CUE job on its CD1 overlap). _lock held."""
    for k, (owner, _) in _held.items():
        if owner != me and (k == key or k.startswith(key + "/")
                            or key.startswith(k + "/")):
            return k
    return None


def release(path) -> None:
    key, me = _norm(path), threading.get_ident()
    with _lock:
        owner = _held.get(key)
        if owner is None or owner[0] != me:
            return
        if owner[1] <= 1:
            del _held[key]
            _freed.notify_all()
        else:
            _held[key] = (me, owner[1] - 1)


@contextmanager
def held(path):
    """`with held(folder) as ok:` -- ok is False when another thread has it."""
    ok = claim(path)
    try:
        yield ok
    finally:
        if ok:
            release(path)


def busy(path) -> Optional[str]:
    """A folder ANOTHER thread is working on that `path` equals, contains or
    sits inside -- or None. The caller's own claims never count."""
    key, me = _norm(path), threading.get_ident()
    with _lock:
        return _conflict(key, me)


def busy_torrent(content_path: str) -> Optional[str]:
    """busy() for a torrent's content path as qBittorrent reports it. The
    client may mount the downloads elsewhere (/data/... against our
    /downloads), so a claimed folder also counts when the torrent's top
    folder NAME is one of its path segments."""
    if not content_path:
        return None
    hit = busy(content_path)
    if hit:
        return hit
    name = os.path.basename(str(content_path).replace("\\", "/").rstrip("/"))
    if not name:
        return None
    seg, me = "/" + os.path.normcase(name) + "/", threading.get_ident()
    with _lock:
        for k, (owner, _) in _held.items():
            if owner != me and seg in k + "/":
                return k
    return None
