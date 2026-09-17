import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class TalmudAddressTest(unittest.TestCase):
    """Cases taken from the published artifacts (Otzaria/otzaria#1348)."""

    def test_relative_comma_daf_resolved_as_running_amud_is_contradicted(self):
        for anchor, target in [
            ("להלן עירובין טו, ב", "Eruvin 9b"),
            ("להלן עירובין יא, ב", "Eruvin 7b"),
            ("לקמן (ברכות נג,ב", "Berakhot 38a"),
            ("לקמן בבא בתרא קנג,ב", "Bava Batra 78b"),
        ]:
            with self.subTest(anchor=anchor):
                self.assertTrue(link_books.talmud_address_contradicts(target, anchor))

    def test_matching_addresses_are_kept(self):
        for anchor, target in [
            ("להלן עירובין טו, ב", "Eruvin 15b"),
            ("בגמ' (לקמן נ, א", "Niddah 50a"),
            ("לעיל מ״ג ע״א, ברש״י ד״ה לא", "Rashi on Chullin 43a:1:2"),
            ("רש\"י, לקמן (דף כז ע\"ב) ד\"ה ורבנן", "Rashi on Megillah 27b:4:1"),
            ("הש\"ס לעיל (דל\"ג ע\"א", "Shevuot 33a"),
            ("לעיל במתניתין דף י\"א ע\"ב", "Ketubot 11a:15-11b:2"),
        ]:
            with self.subTest(anchor=anchor):
                self.assertFalse(link_books.talmud_address_contradicts(target, anchor))

    def test_tractate_daf_is_not_misread_as_an_unquoted_amud_marker(self):
        anchor = "לקמן (עבודה זרה עא, א"
        self.assertEqual(link_books._spelled_addresses(anchor), [("עא", "א")])
        self.assertTrue(link_books.talmud_address_contradicts("Avodah Zarah 64a", anchor))

    def test_words_before_amud_marker_are_not_numbers(self):
        self.assertFalse(link_books.talmud_address_contradicts(
            "Tosafot on Bava Kamma 62b:15:2", "ולקמן ע\"ב תוס' ד\"ה יצאו"))

    def test_no_spelled_address_or_non_talmud_target_is_not_judged(self):
        self.assertFalse(link_books.talmud_address_contradicts("Horayot 9b:11-14a:7", "לקמן בפרק בתרא"))
        self.assertFalse(link_books.talmud_address_contradicts("Genesis 1:1", "לקמן א, ב"))


class SpelledTalmudRefTest(unittest.TestCase):
    class Ref:
        VALID = {"Eruvin 5a", "Eruvin 15b", "Bava Batra 4a", "Tosafot on Eruvin 12a"}

        def __init__(self, text):
            if text not in self.VALID:
                raise ValueError(text)
            self.text = text

        def normal(self):
            return self.text

    def rebuild(self, target, anchor):
        return link_books.spelled_talmud_ref(target, anchor, self.Ref)

    def test_the_address_in_the_text_wins_over_the_resolved_offset(self):
        self.assertEqual(self.rebuild("Eruvin 9b", "להלן עירובין טו, ב"), "Eruvin 15b")
        self.assertEqual(self.rebuild("Eruvin 12b", "דלעיל (עירובין ה, א"), "Eruvin 5a")
        self.assertEqual(self.rebuild("Bava Batra 3b", "לקמן בבא בתרא ד,א"), "Bava Batra 4a")

    def test_a_commentary_target_stays_in_its_own_book(self):
        self.assertEqual(
            self.rebuild("Tosafot on Eruvin 6b:2:1", "תוס' לקמן יב, א"), "Tosafot on Eruvin 12a")

    def test_unusable_anchors_and_nonexistent_dafim_are_dropped(self):
        self.assertIsNone(self.rebuild("Eruvin 9b", "להלן עירובין טו, ב - טז, א"))
        self.assertIsNone(self.rebuild("Eruvin 9b", "להלן עירובין שנ, ב"))
        self.assertIsNone(self.rebuild("Genesis 1:1", "לעיל ה, א"))
        self.assertIsNone(link_books.spelled_talmud_ref("Eruvin 9b", "להלן עירובין טו, ב", None))


class IbidCandidateTest(unittest.TestCase):
    class Ref:
        def __init__(self, title, sections):
            self.index = type("Index", (), {"title": title})()
            self.sections = sections

    def rr(self, candidates, parts=("IBID", "NUMBERED")):
        Kind = lambda name: type("Kind", (), {"name": name})()  # noqa: E731
        return type("RR", (), {
            "is_ambiguous": True,
            "raw_entity": type("Raw", (), {
                "raw_ref_parts": [type("Part", (), {"type": Kind(p)})() for p in parts]})(),
            "resolved_raw_refs": [type("R", (), {"ref": c})() for c in candidates],
        })()

    def test_the_chapter_of_the_preceding_citation_decides(self):
        last = self.Ref("Genesis", [30, 1])
        chosen = link_books._ibid_candidate(
            self.rr([self.Ref("Genesis", [29, 14]), self.Ref("Genesis", [30, 14])]), last)
        self.assertEqual(chosen.sections, [30, 14])

    def test_candidates_from_another_book_are_never_guessed(self):
        last = self.Ref("Rashi on II Chronicles", [2, 13])
        self.assertIsNone(link_books._ibid_candidate(
            self.rr([self.Ref("II Chronicles", [28, 14]),
                     self.Ref("Rashi on II Chronicles", [28, 14])]), last))

    def test_no_antecedent_or_no_ibid_part_keeps_the_drop(self):
        cands = [self.Ref("Genesis", [29, 14]), self.Ref("Genesis", [30, 14])]
        self.assertIsNone(link_books._ibid_candidate(self.rr(cands), None))
        self.assertIsNone(link_books._ibid_candidate(
            self.rr(cands, parts=("NAMED", "NUMBERED")), self.Ref("Genesis", [30, 1])))

    def test_an_undecidable_ambiguity_is_still_dropped(self):
        last = self.Ref("Genesis", [30, 1])
        self.assertIsNone(link_books._ibid_candidate(
            self.rr([self.Ref("Genesis", [30, 14]), self.Ref("Genesis", [30, 15])]), last))

    def test_a_different_reference_depth_is_not_treated_as_continuation(self):
        last = self.Ref("Genesis", [30, 1])
        self.assertIsNone(link_books._ibid_candidate(
            self.rr([self.Ref("Genesis", [30, 14, 2]), self.Ref("Genesis", [29, 14, 2])]), last))


class AbbreviationMisreadTest(unittest.TestCase):
    def test_magen_avraham_and_page_numbers_are_dropped(self):
        self.assertTrue(link_books.is_abbreviation_misread("I Kings", "מ\"א"))
        self.assertTrue(link_books.is_abbreviation_misread("I Kings 1", "מ\"א סי' א"))
        self.assertTrue(link_books.is_abbreviation_misread("Amos 4", "עמ' פ\"ד"))

    def test_real_citations_of_the_same_books_are_kept(self):
        self.assertFalse(link_books.is_abbreviation_misread("I Kings 14", "מ\"א יד"))
        self.assertFalse(link_books.is_abbreviation_misread("I Kings 7", "מלכים א ז"))
        self.assertFalse(link_books.is_abbreviation_misread("Amos 7:7", "עמוס ז ז"))
        self.assertFalse(link_books.is_abbreviation_misread("Shabbat 2a", "מ\"א"))


class MishnahChapterMisreadTest(unittest.TestCase):
    def test_bavli_first_chapter_is_not_linked_to_mishnah(self):
        self.assertTrue(link_books.is_mishnah_chapter_misread(
            "Mishnah Eruvin 1", "בפ\"ק דעירובין"))

    def test_explicit_mishnah_reading_is_kept(self):
        self.assertFalse(link_books.is_mishnah_chapter_misread(
            "Mishnah Eruvin 1", "במשנה פ\"ק דעירובין"))


if __name__ == "__main__":
    unittest.main()
