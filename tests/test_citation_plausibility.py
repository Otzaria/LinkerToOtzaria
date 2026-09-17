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

    def test_words_before_amud_marker_are_not_numbers(self):
        self.assertFalse(link_books.talmud_address_contradicts(
            "Tosafot on Bava Kamma 62b:15:2", "ולקמן ע\"ב תוס' ד\"ה יצאו"))

    def test_no_spelled_address_or_non_talmud_target_is_not_judged(self):
        self.assertFalse(link_books.talmud_address_contradicts("Horayot 9b:11-14a:7", "לקמן בפרק בתרא"))
        self.assertFalse(link_books.talmud_address_contradicts("Genesis 1:1", "לקמן א, ב"))


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


if __name__ == "__main__":
    unittest.main()
