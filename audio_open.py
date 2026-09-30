"""One place that opens an audio file with mutagen.

`mutagen.File(path)` with no hint opens the file and SCORES every format parser
to guess the type. Measured on this library, over the array: 631 ms per FLAC
that way versus 264 ms when the parser is named -- 2.2x. py-spy on the running
pipeline showed four of five worker threads inside `mutagen/apev2.py:score` at
the same instant (the cueless sweep twice, song_harvest and album assembly), so
this is the hottest call it makes.

Naming the parser from the extension is only a HINT. A file whose extension
lies still has to work, so a typed open that returns None or raises falls
through to the full sniff -- same answers, usually much faster. `easy=True` and
an explicit `options=` are passed straight to mutagen: it ignores `easy` when
options are given, and being clever there would silently change what callers
get back.

Drop-in for `from mutagen import File`, so call sites need no changes.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import mutagen

logger = logging.getLogger("audio_open")


def _build_map() -> Dict[str, List[Any]]:
    """Extension -> parser. Any format this mutagen build lacks is simply
    absent, and those files fall back to sniffing."""
    out: Dict[str, List[Any]] = {}

    def add(exts, module, cls):
        try:
            mod = __import__("mutagen.%s" % module, fromlist=[cls])
            parser = getattr(mod, cls)
        except Exception:  # noqa: BLE001
            return
        for e in exts:
            out[e] = [parser]

    add([".flac"], "flac", "FLAC")
    add([".mp3"], "mp3", "MP3")
    add([".m4a", ".mp4", ".m4b", ".alac"], "mp4", "MP4")
    add([".ogg", ".oga"], "oggvorbis", "OggVorbis")
    add([".opus"], "oggopus", "OggOpus")
    add([".wav"], "wave", "WAVE")
    add([".aiff", ".aif"], "aiff", "AIFF")
    add([".ape"], "monkeysaudio", "MonkeysAudio")
    add([".wv"], "wavpack", "WavPack")
    add([".dsf"], "dsf", "DSF")
    add([".tta"], "trueaudio", "TrueAudio")
    add([".mpc"], "musepack", "Musepack")
    add([".wma"], "asf", "ASF")
    return out


_BY_EXT = _build_map()


_TITLE_KEYS = ("title", "TIT2", "\xa9nam", "Title")
_ARTIST_KEYS = ("artist", "TPE1", "\xa9ART", "Author",
                "albumartist", "TPE2", "aART")
_ALBUM_KEYS = ("album", "TALB", "\xa9alb", "WM/AlbumTitle")


def basic_tags(mf: Any) -> tuple:
    """(title, artist, album) from a TYPED open -- Vorbis/APE keys, ID3
    frames, MP4 atoms, ASF attributes -- blanks where absent. The typed open
    is what makes File() fast: easy=True skips it and sniffs every parser
    (631 ms vs 264 ms per FLAC on PARK's share)."""
    tags = getattr(mf, "tags", None) if mf is not None else None
    if tags is None:
        return "", "", ""

    def first(keys):
        for k in keys:
            try:
                v = tags.get(k)
            except Exception:  # noqa: BLE001 -- a key this format cannot hold
                v = None
            if v is None:
                continue
            v = getattr(v, "text", v)             # ID3 frame -> its values
            if isinstance(v, (list, tuple)):
                v = v[0] if v else ""
            s = str(v).strip()
            if s:
                return s
        return ""
    return first(_TITLE_KEYS), first(_ARTIST_KEYS), first(_ALBUM_KEYS)


def File(filething: Any, options: Any = None, easy: bool = False):
    """Same contract as mutagen.File, just faster when the extension is honest."""
    if options is not None or easy:
        return mutagen.File(filething, options=options, easy=easy)

    try:
        ext = os.path.splitext(str(filething))[1].lower()
    except Exception:  # noqa: BLE001
        ext = ""

    opts = _BY_EXT.get(ext)
    if opts:
        try:
            hit = mutagen.File(filething, options=opts)
            if hit is not None:
                return hit
        except Exception as exc:  # noqa: BLE001
            # The extension lied, or that parser choked on it. Not an error --
            # sniffing is exactly the right answer here.
            logger.debug("typed open failed for %s (%s); sniffing", filething, exc)

    return mutagen.File(filething)
