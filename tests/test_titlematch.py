"""titlematch: one fold and one set of word rules for every script."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import titlematch as t  # noqa: E402


class Fold(unittest.TestCase):
    def test_latin_accents_and_letters_without_decomposition(self):
        for a, b in [("Sigur Rós", "Sigur Ros"), ("Motörhead", "Motorhead"),
                     ("Røyksopp", "Royksopp"), ("Straße", "Strasse"),
                     ("Tiësto", "Tiesto"), ("Łódź", "Lodz")]:
            self.assertEqual(t.key(a), t.key(b), (a, b))

    def test_cyrillic_and_greek_meet_their_latin_spelling(self):
        self.assertEqual(t.key("Азис"), t.key("Azis"))
        self.assertEqual(t.key("Βαγγέλης"), t.key("Vaggelis"))

    def test_mojibake_is_repaired(self):
        self.assertEqual(t.key("Ëèëè Èâàíîâà"), t.key("Lili Ivanova"))

    def test_kana_voicing_is_a_different_letter(self):
        self.assertNotEqual(t.fold("ブルー"), t.fold("フルー"))
        self.assertEqual(t.fold("ブルー"), t.fold("ﾌﾞﾙｰ"))  # half-width

    def test_indic_vowel_signs_are_kept(self):
        self.assertNotEqual(t.key("संगीत"), t.key("सगत"))

    def test_no_script_folds_to_empty(self):
        for s in ["浜崎あゆみ", "사랑", "عمرو دياب", "עמרו", "ความรัก", "Группа крови"]:
            self.assertTrue(t.key(s), s)
        self.assertNotEqual(t.key("浜崎あゆみ"), t.key("宇多田ヒカル"))
        self.assertNotEqual(t.key("عمرو دياب"), t.key("فيروز"))


class Words(unittest.TestCase):
    def test_unspaced_scripts_become_character_pairs(self):
        self.assertEqual(t.tokens("浜崎あゆみ"), ["浜崎", "崎あ", "あゆ", "ゆみ"])

    def test_significance_is_shape_not_a_title_list(self):
        self.assertFalse(t.is_significant("the"))
        self.assertFalse(t.is_significant("1998"))
        self.assertFalse(t.is_significant("deluxe"))
        self.assertTrue(t.is_significant("blackstar"))
        self.assertTrue(t.is_significant("浜崎"))


class SameRecord(unittest.TestCase):
    def test_refuses_what_the_titles_contradict(self):
        self.assertFalse(t.same_record_evidence("A", "The Album"))
        self.assertFalse(t.same_record_evidence(
            "At The Gate Of Horn _ Ballads For Americans And Other American Ballads",
            "Odetta Sings the Ballad for Americans and Other American Ballads"))
        self.assertFalse(t.same_record_evidence("Before I Self Destruct",
                                                "Get Rich or Die Tryin'"))

    def test_accepts_editions_and_other_scripts(self):
        self.assertTrue(t.same_record_evidence("Reprise - Remixes", "Reprise"))
        self.assertTrue(t.same_record_evidence("Live Upon A Blackstar",
                                               "Wish Upon a Blackstar"))
        self.assertTrue(t.same_record_evidence("Группа крови (Remastered)",
                                               "Gruppa krovi"))
        self.assertTrue(t.same_record_evidence("浜崎あゆみ A BEST", "A BEST"))

    def test_two_in_one_needs_every_part(self):
        self.assertEqual(t.joined_titles("Blue Train / Soultrane"),
                         ["Blue Train", "Soultrane"])
        self.assertFalse(t.same_record_evidence("Blue Train / Soultrane", "Soultrane"))


class NamesArtist(unittest.TestCase):
    def test_short_names_must_fill_a_whole_field(self):
        self.assertFalse(t.names_artist(["X-Ray Spex - Germfree Adolescents"], "X"))
        self.assertTrue(t.names_artist(["X - Los Angeles (1980)"], "X"))
        self.assertTrue(t.names_artist(["Music", "U2", "The Joshua Tree"], "U2"))
        self.assertTrue(t.names_artist(["The The - Soul Mining"], "The The"))
        self.assertFalse(t.names_artist(["The Cure - Disintegration"], "The The"))

    def test_significant_names_may_sit_anywhere(self):
        self.assertTrue(t.names_artist(["ABBA Gold"], "ABBA"))
        self.assertFalse(t.names_artist(["Bruno Mars - 24K Magic"], "Cocteau Twins"))
        self.assertTrue(t.names_artist(["Лили Иванова - Танго"], "Lili Ivanova"))
        self.assertTrue(t.names_artist(["浜崎あゆみ - A BEST"], "浜崎あゆみ"))


if __name__ == "__main__":
    unittest.main()
