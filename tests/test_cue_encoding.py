"""A CUE that is not UTF-8 is read in the encoding its text is plausible in,
not the first one that decodes (loops F7): cp1251 decodes almost any byte, so
Western and Japanese CUEs used to become Cyrillic mojibake in tags and file
names -- 'Tiempo De Soleб', 'Calй Barн' (Ojos De Brujo, 29 Sep)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cue_parser  # noqa: E402

CUE = '''PERFORMER "{artist}"
TITLE "{album}"
FILE "{artist} - {album}.flac" WAVE
  TRACK 01 AUDIO
    TITLE "{t1}"
    PERFORMER "{artist}"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "{t2}"
    PERFORMER "{artist}"
    INDEX 01 04:00:00
'''


class ReadsTheEncodingTheTextIsIn(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)

    def cue(self, codec, bom=b"", **f):
        p = self.dir / "album.cue"
        p.write_bytes(bom + CUE.format(**f).encode(codec))
        return p

    def read(self, p):
        return cue_parser._read_cue_text(p)

    def test_western_cp1252(self):
        p = self.cue("cp1252", artist="Ojos De Brujo", album="Bari",
                     t1="Tiempo De Soleá", t2="Calé Barí")
        text = self.read(p)
        self.assertIn("Tiempo De Soleá", text)
        self.assertIn("Calé Barí", text)

    def test_a_short_western_name(self):
        p = self.cue("cp1252", artist="Beyoncé", album="Dangerously in Love",
                     t1="Crazy in Love", t2="Naughty Girl")
        self.assertIn('PERFORMER "Beyoncé"', self.read(p))

    def test_japanese_shift_jis(self):
        p = self.cue("shift_jis", artist="歌手", album="ドラゴンの歌",
                     t1="ドラゴンの歌", t2="月の光")
        text = self.read(p)
        self.assertIn("ドラゴンの歌", text)
        self.assertIn("月の光", text)

    def test_russian_cp1251_stays_cyrillic(self):
        p = self.cue("cp1251", artist="Кино", album="Группа крови",
                     t1="Группа крови", t2="Закрой за мной дверь, я ухожу")
        text = self.read(p)
        self.assertIn("Группа крови", text)
        self.assertIn("Закрой за мной дверь, я ухожу", text)

    def test_russian_capitals_are_not_half_width_katakana(self):
        p = self.cue("cp1251", artist="КИНО", album="ЗВЕЗДА ПО ИМЕНИ СОЛНЦЕ",
                     t1="ПАЧКА СИГАРЕТ", t2="СПОКОЙНАЯ НОЧЬ")
        self.assertIn("ЗВЕЗДА ПО ИМЕНИ СОЛНЦЕ", self.read(p))

    def test_utf8_with_and_without_bom(self):
        for bom in (b"", b"\xef\xbb\xbf"):
            p = self.cue("utf-8", bom=bom, artist="Björk", album="Homogenic",
                         t1="Jóga", t2="Bachelorette")
            text = self.read(p)
            self.assertTrue(text.startswith("PERFORMER"), repr(text[:5]))
            self.assertIn("Jóga", text)

    def test_the_parser_reads_the_titles(self):
        p = self.cue("cp1252", artist="Ojos De Brujo", album="Bari",
                     t1="Tiempo De Soleá", t2="Calé Barí")
        cue = cue_parser.parse_cue(p, 480.0)
        self.assertEqual([t.title for t in cue.tracks],
                         ["Tiempo De Soleá", "Calé Barí"])


class FileReferenceRepairKeepsTheText(unittest.TestCase):
    """The FILE-reference repair read a CUE as UTF-8 with errors ignored and
    wrote it back, erasing every non-ASCII letter of a cp1251/cp1252 CUE."""

    def test_a_cyrillic_cue_keeps_its_titles(self):
        from orchestrator import Orchestrator
        with tempfile.TemporaryDirectory() as d:
            cue = Path(d) / "album.cue"
            cue.write_bytes(CUE.format(
                artist="Кино", album="Группа крови", t1="Группа крови",
                t2="Спокойная ночь").encode("cp1251"))
            audio = Path(d) / "Кино - Группа крови.ape"
            audio.write_bytes(b"x")
            o = Orchestrator.__new__(Orchestrator)
            o._heal_cue_file_reference(cue, audio)
            text = cue.read_text(encoding="utf-8")
        self.assertIn('FILE "Кино - Группа крови.ape" WAVE', text)
        self.assertIn('TITLE "Спокойная ночь"', text)


if __name__ == "__main__":
    unittest.main()
