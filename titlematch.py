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

import re
import unicodedata
from typing import List, Sequence, Set

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


def fold(s: str) -> str:
    """The comparable spelling of `s` (see module docstring)."""
    s = _demojibake(s or "").casefold().replace("&", " and ")
    out: List[str] = []
    base = ""
    for c in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(c):
            if base and _DROP_MARK_BASES.match(base):
                continue
        else:
            base = c
        out.append(c)
    s = unicodedata.normalize("NFC", "".join(out)).translate(_LATIN_EXTRA)
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
