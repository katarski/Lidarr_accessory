"""
Comparing album titles, song titles and artist names in ANY script.

Every matcher used to end in a [^a-z0-9] filter, which does not ignore other
scripts -- it DELETES them: "Группа крови" had no words at all, "Fältskog"
became "f" + "ltskog", "Røyksopp" became "ryksopp" (never equal to the
"Royksopp" a tracker writes), and a Japanese, Greek, Arabic or Korean title
was the empty string, so every such title compared equal to every other.

One fold, used everywhere a name is compared:
  * mojibake repaired (cp1251 read as latin-1);
  * case folded, '&' read as 'and';
  * accents stripped where they are decoration a tagger may or may not type
    (Latin, Greek, Cyrillic, Hebrew points, Arabic vowel marks) and KEPT where
    they change the letter (kana voicing marks: は is not ば; Indic and Thai
    vowel signs);
  * Latin letters that have no decomposition folded to their base
    (ø->o, æ->ae, ł->l, ß->ss ...);
  * Cyrillic and Greek transliterated, so a Latin folder meets a native title.
Words are runs of letters, digits and their marks in any script. Scripts
written WITHOUT spaces (Chinese, Japanese, Thai, Lao, Khmer, Myanmar) have no
words to split on, so a run of them is cut into overlapping character pairs --
the standard way to compare such text.

Whether a word is SIGNIFICANT -- evidence that two titles name the same record
-- is decided by its shape, not by any list of titles:
  * digits alone (years, volume numbers) are not;
  * edition/format words (deluxe, remaster, flac...) are not;
  * in Latin-script text (after transliteration) a word of 3 letters or fewer
    is not: that is where the articles and conjunctions of those languages
    live (the, and, der, und, les, los, och, и, на...);
  * in other spaced scripts (Arabic, Hebrew, Devanagari, Hangul...) 3 or more;
  * a character pair from an unspaced script is.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import List, Optional, Sequence, Set

# ---------------------------------------------------------------------------
# Transliteration and repair (lidarr.py re-exports these under the same names)
# ---------------------------------------------------------------------------

# Cyrillic -> Latin transliteration (Bulgarian/Russian/Ukrainian covered).
# Needed so folder "Azis" matches Lidarr artist "Азис", folder "Bolka"
# matches Lidarr album "Болка", etc. NFKD alone won't do this -- Cyrillic
# letters are distinct code points, not Latin-with-diacritics.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
    "ы": "y", "э": "e", "ё": "yo",
    # Ukrainian extras
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g",
    # Serbian / Macedonian extras (phonetic Latin)
    "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "ђ": "dj", "џ": "dz",
    "ѕ": "dz", "ѓ": "g", "ќ": "k", "ѐ": "e", "ѝ": "i", "ѣ": "e",
}

# Greek -> Latin, the common (ELOT 743-like) spelling a Latin folder uses:
# "Βαγγέλης" folds to "vaggelis", "Χατζιδάκις" to "chatzidakis".
_GREEK_TO_LATIN = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}

# Latin letters with no Unicode decomposition, so NFKD leaves them alone and an
# ASCII spelling of the same name never met them.
_LATIN_EXTRA = str.maketrans({
    "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ð": "d",
    "þ": "th", "ı": "i", "ħ": "h", "ŧ": "t", "ŋ": "ng", "ſ": "s",
})


def _translit_cyrillic(value: str) -> str:
    """
    Fold any Cyrillic characters in `value` to a rough Latin
    transliteration. ASCII letters pass through untouched. Used as a
    best-effort equalizer for cross-script name matching: the folder on
    disk is often Latin ("Azis") while Lidarr stores the canonical
    Cyrillic ("Азис"), or vice-versa.
    """
    if not value:
        return value
    # Fast path: no Cyrillic, nothing to do.
    if not any("Ѐ" <= ch <= "ӿ" for ch in value):
        return value
    out = []
    for ch in value:
        lower = ch.lower()
        rep = _CYRILLIC_TO_LATIN.get(lower)
        if rep is None:
            out.append(ch)
        elif ch == lower:
            out.append(rep)
        else:
            out.append(rep.capitalize() if len(rep) > 1 else rep.upper())
    return "".join(out)


def _demojibake(value: str) -> str:
    """
    Repair Cyrillic text that was written as cp1251 bytes and read back as
    latin-1: 'Ëèëè Èâàíîâà' -> 'Лили Иванова', 'Òàíãî' -> 'Танго'.

    Endemic in older Eastern-European rips -- most files in the Lili Ivanova
    discography carry tags in this state, which is why Lidarr's manualimport
    reported `artist=None album=None` for them.

    The test is deliberately strict, because a naive "did this produce any
    Cyrillic?" check mangles ordinary accented Latin: 'Beyoncé' round-trips to
    'Beyoncй' (é is 0xE9, which is 'й' in cp1251) and then transliterates to
    "beyoncy". Real mojibake is high-range almost throughout, so require at
    least 3 such characters AND at least 40% of the letters. 'Ëèëè Èâàíîâà' is
    11 of 11; 'Beyoncé' is 1 of 7 and is left alone.
    """
    if not value:
        return value
    letters = [c for c in value if c.isalpha()]
    suspect = [c for c in letters if "À" <= c <= "ÿ"]
    if len(suspect) < 3 or len(suspect) < 0.4 * len(letters):
        return value

    def _cyr(s: str) -> int:
        return sum(1 for c in s if "Ѐ" <= c <= "ӿ")

    try:
        fixed = value.encode("latin-1").decode("windows-1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return fixed if _cyr(fixed) > _cyr(value) else value


# ---------------------------------------------------------------------------
# Folding and words
# ---------------------------------------------------------------------------

# Scripts written without spaces between words.
_UNSPACED = ("฀-๿"        # Thai
             "຀-໿"        # Lao
             "က-႟"        # Myanmar
             "ក-៿"        # Khmer
             "぀-ヿ"        # Hiragana, Katakana
             "㄀-ㄯ"        # Bopomofo
             "㐀-䶿"        # CJK extension A
             "一-鿿"        # CJK unified
             "豈-﫿")       # CJK compatibility
_UNSPACED_RE = re.compile("[%s]" % _UNSPACED)
_SEGMENT_RE = re.compile("[%s]+|[^%s]+" % (_UNSPACED, _UNSPACED))

# A mark after a letter of these scripts is optional decoration (accents,
# Hebrew points, Arabic harakat) and is dropped. After any other script a mark
# is part of the letter -- a kana voicing mark, an Indic vowel sign -- and is
# kept, then recomposed.
_DROP_MARK_BASES = re.compile("[\u0000-ԯ֐-ۿḀ-῿]")


def strip_accents(s: str) -> str:
    """`s` without its decorative accents, case and script kept: 'Tiësto' ->
    'Tiesto', but 'ドラゴン' keeps the voicing marks that make it a word."""
    out: List[str] = []
    base = ""
    for c in unicodedata.normalize("NFKD", s or ""):
        if unicodedata.combining(c):
            if base and _DROP_MARK_BASES.match(base):
                continue
        else:
            base = c
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


def fold(s: str) -> str:
    """The comparable spelling of `s` (see module docstring)."""
    s = _demojibake(s or "").casefold().replace("&", " and ")
    s = strip_accents(s).translate(_LATIN_EXTRA)
    if any("Ͱ" <= ch <= "Ͽ" for ch in s):
        s = "".join(_GREEK_TO_LATIN.get(ch, ch) for ch in s)
    return _translit_cyrillic(s)


def _is_word_char(c: str) -> bool:
    return c.isalnum() or unicodedata.category(c)[0] == "M"


def _runs(s: str) -> List[str]:
    out: List[str] = []
    cur: List[str] = []
    for c in s:
        if _is_word_char(c):
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def words(s: str) -> List[str]:
    """The folded words of `s`, in order. An unspaced-script run stays whole."""
    return _runs(fold(s))


def key(s: str) -> str:
    """An equality key: the folded words joined, nothing else. Never '' for a
    string that has letters, in whatever script."""
    return "".join(words(s))


def tokens(s: str) -> List[str]:
    """The words of `s`, with unspaced-script runs cut into character pairs."""
    out: List[str] = []
    for run in words(s):
        if not _UNSPACED_RE.search(run):
            out.append(run)
            continue
        for seg in _SEGMENT_RE.findall(run):
            if not _UNSPACED_RE.match(seg) or len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


# Edition / format words: never evidence of WHICH album.
NOISE_WORDS = frozenset({
    "deluxe", "expanded", "remaster", "remastered", "remasters", "edition",
    "editions", "anniversary", "bonus", "disc", "discs", "mono", "stereo",
    "version", "versions", "reissue", "reissued", "collectors", "collector",
    "special", "limited", "digital", "hires", "sacd", "hdcd", "mfsl", "flac",
    "24bit", "16bit", "lossless", "vinyl",
})


def is_significant(tok: str) -> bool:
    if not tok or tok.isdigit() or tok in NOISE_WORDS:
        return False
    if _UNSPACED_RE.search(tok):
        return len(tok) >= 2
    if tok.isascii():
        return len(tok) >= 4
    return len(tok) >= 3


def significant(s: str) -> Set[str]:
    return {t for t in tokens(s) if is_significant(t)}


def contains_run(longer: Sequence[str], shorter: Sequence[str]) -> bool:
    """`shorter` appears in `longer` as consecutive whole words."""
    n = len(shorter)
    if not n or n > len(longer):
        return False
    return any(list(longer[i:i + n]) == list(shorter)
               for i in range(len(longer) - n + 1))


# " / ", " _ " (a slash on a filesystem that forbids one), " + ", " | " with
# space on both sides joins two titles: "Album A / Album B" is a two-in-one
# release. A colon replaced by "_" has no space before it ("Title_ Subtitle").
_JOIN_RE = re.compile(r"\s+[/_+|／＋]\s+")


def joined_titles(s: str) -> List[str]:
    """The titles a two-in-one name joins ("A / B"), or [] when it is one title.
    Only splits when every part carries a significant word of its own."""
    parts = [p.strip() for p in _JOIN_RE.split(s or "") if p.strip()]
    if len(parts) < 2 or not all(significant(p) for p in parts):
        return []
    return parts


def same_record_evidence(download: str, owned: str) -> str:
    """
    Why `owned` could be the record `download` names, or "" when nothing in
    the two titles supports it. The LLM chooses; this only refuses a choice
    the titles themselves contradict:
      * they share a significant word, or one is a run of two or more whole
        words inside the other;
      * and when the download joins several titles ("A / B"), EVERY part
        shares a significant word with the owned one -- a two-in-one release
        is not either of its albums alone.
    """
    dt, ot = tokens(download), tokens(owned)
    ds, os_ = significant(download), significant(owned)
    shared = ds & os_
    run = ((len(ot) >= 2 and contains_run(dt, ot))
           or (len(dt) >= 2 and contains_run(ot, dt)))
    if not shared and not run:
        return ""
    parts = joined_titles(download)
    if parts and not all(significant(p) & os_ for p in parts):
        return ""
    return "shared words %s" % ", ".join(sorted(shared)) if shared else "word run"


# Where a folder name separates its fields: " - ", brackets, a joiner.
_FIELD_SEP_RE = re.compile(r"\s+[-–—]\s+|[\[\](){}]|\s+[/_+|]\s+")


def _drop_the(t: List[str]) -> List[str]:
    return t[1:] if len(t) > 1 and t[0] == "the" else t


def names_artist(path_parts: Sequence[str], artist: str) -> bool:
    """
    Does a folder on this path carry the artist's whole name? A name with a
    significant word ("ABBA", "Cocteau Twins") may sit anywhere in a folder
    name as consecutive words. A name with none -- "X", "U2", "Yes", "The The"
    -- is a word inside countless other names ("X-Ray Spex", "Yes Sir"), so it
    must fill a whole field of the folder name ("X - Los Angeles").
    """
    want = _drop_the(tokens(artist))
    if not want:
        return False
    if any(is_significant(t) for t in want):
        return any(contains_run(tokens(p), want) for p in path_parts)
    for p in path_parts:
        for field in _FIELD_SEP_RE.split(p or ""):
            if _drop_the(tokens(field)) == want:
                return True
    return False


# ---------------------------------------------------------------------------
# Does a release title NAME an album?
# ---------------------------------------------------------------------------

# Words a tracker writes around an album's name that do not make it another
# record: edition, format, medium, source, pressing. A word holding a digit
# (a year, "2cd", "24bit", "180g", "dsd64") is one too.
DECOR_WORDS = NOISE_WORDS | frozenset({
    "cd", "cds", "cdda", "lp", "lps", "dvd", "dvda", "bd", "bluray", "blu",
    "ray", "spec", "bluspec", "shm", "shmcd", "xrcd", "uhqcd", "hqcd", "k2hd",
    "mp3", "ape", "alac", "wav", "wv", "wavpack", "aac", "m4a", "ogg", "opus",
    "dsd", "dsf", "dff", "pcm", "mqa", "tta", "aiff", "aif", "lossy", "hi",
    "res", "khz", "kbps", "kbit", "bit", "vbr", "cbr", "web", "webrip",
    "cdrip", "rip", "vinylrip", "eac", "image", "cue", "log", "tracks",
    "scans", "scan", "covers", "artwork", "booklet", "japan", "japanese",
    "jpn", "jp", "uk", "us", "usa", "eu", "import", "press", "pressing",
    "repress", "qobuz", "tidal", "deezer", "bandcamp", "itunes", "hdtracks",
    "mofi", "pbthal", "ost", "soundtrack", "score", "original", "album",
    "extended", "remastering",
})

# Release-type words: decoration next to a name ("Album EP"), but a sibling
# title that adds one ("X" beside "X EP") is a different record.
_TYPE_WORDS = frozenset({"ep", "single", "maxi"})

# Where a release title separates its fields: " - ", brackets, double quotes
# or a pair of single ones ('Entre Eux Deux'), "/", "|", "+", "•", ";", and a
# run of spaces ("Macklemore & Ryan Lewis   BEN" -- a whole family of
# uploaders separates artist and album that way). Not "." and not a colon
# inside a word run: "After Silence" is not "After Silence II. Devotion".
# Not "," either: "Bach, Dove, Monteverdi" is one list. Not "...": "The
# Real... Gipsy Kings" is a compilation, not the album "Gipsy Kings". A lone
# apostrophe is not a quote: "The Drifters' Golden Hits".
_RELEASE_SEP_RE = re.compile(
    "[\[\](){}<>/\\|+«»•·;\"“”„]|\s{2,}"
    "|(?:^|\s)[-–—~_]+|[-–—~_]+(?=\s|$)")
_QUOTED_RE = re.compile("(?<!\S)['‘]([^'‘’]+)['’](?!\w)")

# A colon then a space OPENS a field -- "Bryan Adams: Classic" names
# "Classic" -- but does not close one: after an album's name it begins a
# subtitle that can make it another record, "Joker: Folie à Deux" or
# "Destination: Treasure Island".
_COLON_RE = re.compile(r":+(?=\s)")

# " & " joins two names ("Lady Soul & Aretha Now") but is also inside one
# ("Kool & the Gang"): it stays the word "and", and its place is a boundary.
_AMP_RE = re.compile(r"\s&\s")

_ARTICLES = frozenset({"a", "an", "the"})
_GLUE = _ARTICLES | {"and", "of"}


def is_decor(w: str) -> bool:
    return (w in DECOR_WORDS or w in _TYPE_WORDS
            or any(c.isdigit() for c in w))


def _text(s: str) -> str:
    t = html.unescape(s or "")
    if t and not any(c.isspace() for c in t):
        # A scene name has no spaces: "Artist-Album-2CD-FLAC-2002-GRP".
        t = re.sub(r"[._]+", " ", t).replace("-", " - ")
    return _QUOTED_RE.sub(r'"\1"', t)


def _fields(s: str) -> List[List[str]]:
    """The fields of an album's own title; its colon does separate them:
    "Ladies and Gentlemen: Barenaked Ladies and The Persuasions"."""
    return [w for f in _RELEASE_SEP_RE.split(_text(s))
            for w in (words(p) for p in _COLON_RE.split(f)) if w]


def _stream(title: str):
    """The title's words in order; the indexes where a field starts (a word
    may follow one), where one ends (a word may precede one), and those of
    an "and" that was a joining " & "."""
    ws: List[str] = []
    opens, closes, joins = set(), set(), set()
    for f in _RELEASE_SEP_RE.split(_text(title)):
        opens.add(len(ws))
        closes.add(len(ws))
        for i, sub in enumerate(_COLON_RE.split(f)):
            if i:
                opens.add(len(ws))
            for k, piece in enumerate(_AMP_RE.split(sub)):
                if k:
                    joins.add(len(ws))
                    ws.append("and")
                ws.extend(words(piece))
    return ws, opens, closes, joins


def _runs_of(ws: Sequence[str], pat: Sequence[str]) -> List[tuple]:
    n = len(pat)
    if not n:
        return []
    return [(i, i + n) for i in range(len(ws) - n + 1)
            if list(ws[i:i + n]) == list(pat)]


def more_specific_albums(album: str, others: Sequence[str]) -> List[List[str]]:
    """
    The artist's other album titles that hold this album's whole name plus a
    word of their own that is not decoration: for "Dusty", "Dusty in
    Memphis" and "Ev'rything's Coming Up Dusty"; for "Blue", "Blue Eyed
    Soul". "Album (Deluxe Edition)" is an edition of "Album", not another
    record; a number is not decoration here: "Chicago 17" is not "Chicago".
    Computed once per album, not per release.
    """
    a = words(album)
    out: List[List[str]] = []
    if not a:
        return out
    for o in others or ():
        b = words(o)
        if len(b) <= len(a) or not contains_run(b, a):
            continue
        extra = list(b)
        i = _runs_of(b, a)[0][0]
        del extra[i:i + len(a)]
        if any(w not in DECOR_WORDS for w in extra):
            out.append(b)
    return out


def _spellings(pat: List[str], art: List[str]) -> List[List[str]]:
    """How a tracker may write an album's words: as they are; without the
    decoration Lidarr's title carries ("A Star Is Born Soundtrack"); without
    a leading article ("Bedlam in Goliath"); without the artist's name the
    credit already gives ("Crystal Waters - The Best Of")."""
    def trim(p):
        i, j = 0, len(p)
        while i < j and (p[i] in DECOR_WORDS or p[i] in _TYPE_WORDS):
            i += 1
        while j > i and (p[j - 1] in DECOR_WORDS or p[j - 1] in _TYPE_WORDS):
            j -= 1
        return list(p[i:j]) or list(p)

    out = [list(pat), trim(pat)]
    if len(pat) > 1 and pat[0] in _ARTICLES:
        out.append(trim(pat[1:]))
    if art and len(pat) > len(art):
        for s, e in _runs_of(pat, art)[:1]:
            rest = list(pat[:s]) + list(pat[e:])
            if any(w not in _GLUE and not is_decor(w) for w in rest):
                out.append(trim(rest))
    seen, uniq = set(), []
    for p in out:
        if p and tuple(p) not in seen:
            seen.add(tuple(p))
            uniq.append(p)
    return uniq


def album_naming(title: str, album: str, artist: str = "",
                 specific: Sequence[Sequence[str]] = ()) -> Optional[str]:
    """
    Does release `title` NAME `album`, or only contain its words?

      "named"    -- the album's words stand as a whole field of the title,
                    after the artist's credit is taken out once: bounded by
                    a separator, the title's edge, the artist's name ("Kusa
                    No Ran by Deep Forest"), decoration or a leading article.
                    "Jewel - 0304 - 2003" names "0304"; "Frida - Frida" names
                    "Frida"; "Pink Floyd - The Wall 1979 FLAC" names "The Wall".
      "sibling"  -- every place the album's words occur is inside the name of
                    a more specific album of the same artist (`specific`, from
                    more_specific_albums): "Dusty Springfield - Dusty In
                    Memphis" is not "Dusty".
      "embedded" -- the words occur only inside a longer name, or only in the
                    artist's credit: "Joni Mitchell - Blue Eyed Soul" is not
                    "Blue", even when Lidarr does not know that album, and
                    "ABBA, Björn, Benny, Agnetha & Frida - Waterloo" is not
                    Frida's "Frida".
      None       -- the album has no words to judge by.
    """
    ws, opens, closes, joins = _stream(title)
    whole = words(album)
    if not whole:
        return None
    # The artist's credit, taken out once: the earliest occurrence of the
    # name (or of the name without its leading "the").
    art = words(artist)
    pats = [art] + ([art[1:]] if len(art) > 1 and art[0] == "the" else [])
    art_occ = [sp for p in pats for sp in _runs_of(ws, p)]
    first = min(art_occ, key=lambda sp: (sp[0], -sp[1]), default=None)
    ms, me = first if first else (0, 0)
    art_starts = {s for s, _e in art_occ}
    art_ends = {e for _s, e in art_occ}

    def free(sp):
        # Clear of the credit -- or holding all of it and more: in
        # "Miles Davis - Cookin' with the Miles Davis Quintet" the credit
        # found first is inside the album's own name.
        s, e = sp
        return (e <= ms or s >= me or ms == me
                or (s <= ms and me <= e and e - s > me - ms))

    def left_ok(i):
        return (i == 0 or i in opens or i in art_ends or (i - 1) in joins
                or is_decor(ws[i - 1]))

    def bounded(sp):
        s, e = sp
        left = left_ok(s) or (ws[s - 1] in _ARTICLES and left_ok(s - 1))
        right = (e == len(ws) or e in closes or e in art_starts
                 or e in joins or is_decor(ws[e])
                 or (ws[e] == "by" and e + 1 in art_starts))
        return left and right

    covers = [sp for b in specific or () for sp in _runs_of(ws, b) if free(sp)]

    def covered(sp):
        return any(bs <= sp[0] and sp[1] <= be for bs, be in covers)

    def named(pat):
        return any(free(sp) and bounded(sp) and not covered(sp)
                   for p in _spellings(pat, art) for sp in _runs_of(ws, p))

    if named(whole):
        return "named"
    # An album titled in fields -- "Joker (Original Motion Picture
    # Soundtrack)" -- is named when each field is, wherever the tracker put
    # it: "Joker (by Hildur Gudnadottir) (Original Motion Picture
    # Soundtrack)". A field of pure decoration is not required.
    parts = [p for p in _fields(album) if not all(is_decor(w) for w in p)]
    if len(parts) >= 2 and all(named(p) for p in parts):
        return "named"
    occ = [sp for p in _spellings(whole, art) for sp in _runs_of(ws, p)
           if free(sp)]
    if occ and covers and all(covered(sp) for sp in occ):
        return "sibling"
    return "embedded"
