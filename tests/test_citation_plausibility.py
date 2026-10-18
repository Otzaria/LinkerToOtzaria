import os
import re
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


class TalmudRef:
    """A Sefaria Ref stand-in for Talmud addresses: Gittin ends at 90b, Shabbat at 157b."""
    HEBREW = {"Gittin": "גיטין", "Rashi on Gittin": "רש\"י על גיטין", "Berakhot": "ברכות",
              "Bava Metzia": "בבא מציעא", "Rashi on Bava Metzia": "רש\"י על בבא מציעא",
              "Shabbat": "שבת", "Tosafot on Shabbat": "תוספות על שבת", "Rif Gittin": "רי\"ף גיטין",
              "Hagahot HaBach on Rif Gittin": "הגהות הב\"ח על רי\"ף גיטין"}
    LAST_DAF = 90
    LAST = {"Shabbat": 157, "Tosafot on Shabbat": 157}

    def __init__(self, text):
        text = {he: en for en, he in self.HEBREW.items()}.get(text, text)
        match = re.fullmatch(r"(.+?)(?: (\d+)([ab])(?::\d+)*)?", text)
        title, daf, amud = match.group(1), match.group(2), match.group(3)
        last = self.LAST.get(title, self.LAST_DAF)
        if title not in self.HEBREW or (daf and not 2 <= int(daf) <= last):
            raise ValueError(text)
        self.text = text
        base = [title.split(" on ")[-1]] if " on " in title else []
        dependent = {"dependence": "Commentary"} if base or title.startswith("Rif ") else {}
        self.index = type("Index", (), {
            "title": title, "base_text_titles": base or (["Gittin"] if dependent else []),
            "categories": ["Talmud", "Bavli"] + (["Rishonim on Talmud"] if dependent else [])
            + ["Seder Nashim"], **dependent,
            "get_title": lambda _self, lang: self.HEBREW[title]})()
        self.sections = [2 * int(daf) - (amud == "a")] if daf else []
        self.toSections = self.sections

    def normal(self):
        return self.text

    def is_talmud(self):
        return bool(self.sections)


class OwnTractateRelativeRefTest(unittest.TestCase):
    """Rashi's own-tractate citations that Sefaria leaves unlinked (Otzaria/otzaria#2408)."""

    def recover(self, anchor, source="Rashi on Gittin 3a:14:1"):
        found = link_books.own_tractate_relative_ref(anchor, TalmudRef(source), TalmudRef)
        return None if found is None else (found[0].normal(), found[1])

    def test_the_reported_forms_link_to_the_spelled_daf(self):
        for anchor, expected in [
            ("לקמן גיטין דף כג:", ("Gittin 23b", "below")),
            ("לקמן גיטין פו.", ("Gittin 86a", "below")),
            ("לקמן גיטין ד' כג:", ("Gittin 23b", "below")),
            ("לקמן גיטין כ\"ג:", ("Gittin 23b", "below")),
            ("(לקמן גיטין כג, ב)", ("Gittin 23b", "below")),
            ("לקמן גיטין דף כג ע\"ב", ("Gittin 23b", "below")),
            ("ולקמן גיטין דף ד ע\"ב", ("Gittin 4b", "below")),
        ]:
            with self.subTest(anchor=anchor):
                self.assertEqual(self.recover(anchor), expected)
        self.assertEqual(
            self.recover("לעיל גיטין דף ג.", source="Rashi on Gittin 5a:2:1"), ("Gittin 3a", "above"))
        self.assertEqual(self.recover("לקמן גיטין דף כג:", source="Gittin 3a:14"),
                         ("Gittin 23b", "below"))
        self.assertEqual(
            self.recover("לעיל גיטין דף ג.", source="Rashi on Gittin 3b:1:1"), ("Gittin 3a", "above"))
        self.assertEqual(
            self.recover("לקמן בבא מציעא דף כג:", source="Rashi on Bava Metzia 3a:1:1"),
            ("Bava Metzia 23b", "below"))

    def test_the_wrong_side_or_the_same_amud_is_dropped(self):
        self.assertIsNone(self.recover("לעיל גיטין דף כג:"))
        self.assertIsNone(self.recover("לקמן גיטין דף ג.", source="Rashi on Gittin 5a:2:1"))
        self.assertIsNone(self.recover("לקמן גיטין דף ג.", source="Rashi on Gittin 3a:2:1"))
        self.assertIsNone(self.recover("לעיל גיטין דף ג.", source="Rashi on Gittin 3a:2:1"))

    def test_another_tractate_or_an_unclear_address_is_dropped(self):
        for anchor in [
            "לקמן ברכות דף כג:",
            "לקמן גיטין דף כג: ודף כד.",
            "לקמן גיטין דף צה.",
            "לעיל גיטין פרק ב.",
            "לעיל גיטין פ\"ב.",
            "לקמן גיטין פ\"ב.",
            "לקמן דף כג:",
            "גיטין דף כג:",
            "לקמן גיטין כג, ב.",
            "לקמן גיטין דף כג: ע\"ש.",
            "(לקמן רי\"ף גיטין דף טז ע\"ב)",
            "(לקמן רי\"ף גיטין דף כג.)",
            "(לקמן גיטין יה:)",
            "(לקמן גיטין דף הוא ע\"א)",
            "(לקמן גיטין דבכג:)",
            "(לקמן גיטין ד' ע\"ב)",
            "(לקמן גיטין ע\"ב.)",
        ]:
            with self.subTest(anchor=anchor):
                self.assertIsNone(self.recover(anchor, source="Rashi on Gittin 30a:1:1"
                                               if "לעיל" in anchor else "Rashi on Gittin 3a:14:1"))


class UntitledRelativeRefTest(unittest.TestCase):
    """"(לקמן יא:)" in a Shas commentary names a daf of its own tractate (Otzaria/otzaria#2408)."""

    def recover(self, anchor, source="Tosafot on Shabbat 3a:1:1"):
        found = link_books.untitled_relative_ref(anchor, TalmudRef(source), TalmudRef)
        return None if found is None else (found[0].normal(), found[1])

    def test_a_single_spelled_address_links_to_the_own_tractate(self):
        for anchor, source, expected in [
            ("(לקמן יא:)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 11b", "below")),
            ("(לקמן דף סו.)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 66a", "below")),
            ("(לקמן מז:)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 47b", "below")),
            ("(לקמן צה ע\"ב)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 95b", "below")),
            ("(לעיל ב,א)", "Tosafot on Shabbat 5a:1:1", ("Shabbat 2a", "above")),
            ("(לעיל ד,א)", "Tosafot on Shabbat 5a:1:1", ("Shabbat 4a", "above")),
            ("לקמן כג, ב", "Tosafot on Shabbat 3a:1:1", ("Shabbat 23b", "below")),
            ("לקמן כ\"ג.", "Tosafot on Shabbat 3a:1:1", ("Shabbat 23a", "below")),
            ("ולקמן ד' כג:", "Tosafot on Shabbat 3a:1:1", ("Shabbat 23b", "below")),
            ("דלעיל דף ג ע\"א", "Tosafot on Shabbat 5a:1:1", ("Shabbat 3a", "above")),
            ("להלן ז:", "Rashi on Gittin 3a:1:1", ("Gittin 7b", "below")),
            ("לקמן יא:", "Shabbat 3a:2", ("Shabbat 11b", "below")),
            ("(לקמן דף ד ע\"ב)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 4b", "below")),
            ("(לקמן ד.)", "Tosafot on Shabbat 3a:1:1", ("Shabbat 4a", "below")),
        ]:
            with self.subTest(anchor=anchor, source=source):
                self.assertEqual(self.recover(anchor, source), expected)

    def test_a_lone_amud_is_the_other_side_of_the_source_daf(self):
        self.assertEqual(self.recover("(לקמן ע\"ב)", "Tosafot on Shabbat 3a:1:1"),
                         ("Shabbat 3b", "below"))
        self.assertEqual(self.recover("(להלן ע\"ב)", "Shabbat 3a:2"), ("Shabbat 3b", "below"))
        self.assertEqual(self.recover("(לעיל ע\"א)", "Tosafot on Shabbat 3b:1:1"),
                         ("Shabbat 3a", "above"))
        for anchor, source in [
            ("(לקמן ע\"ב)", "Tosafot on Shabbat 3b:1:1"),
            ("(לקמן ע\"א)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ע\"א)", "Tosafot on Shabbat 3b:1:1"),
            ("(לעיל ע\"א)", "Tosafot on Shabbat 3a:1:1"),
            ("(לעיל ע\"ב)", "Tosafot on Shabbat 3b:1:1"),
            ("(לעיל ע\"ב)", "Tosafot on Shabbat 4a:1:1"),
            ("(לקמן ע\"ב.)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן דף ע\"ב)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ד' ע\"ב)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ד׳ ע״ב)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ד ע\"ב)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ד'.)", "Tosafot on Shabbat 3a:1:1"),
        ]:
            with self.subTest(anchor=anchor, source=source):
                self.assertIsNone(self.recover(anchor, source))

    def test_a_lone_amud_needs_a_source_on_one_amud(self):
        source = TalmudRef("Tosafot on Shabbat 3b:1:1")
        source.toSections = [source.sections[0] + 1]  # 3b-4a
        self.assertIsNone(link_books.untitled_relative_ref("(לעיל ע\"א)", source, TalmudRef))

    def test_the_wrong_side_the_same_amud_or_a_missing_daf_is_dropped(self):
        for anchor, source in [
            ("(לעיל יא:)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן ב.)", "Tosafot on Shabbat 5a:1:1"),
            ("(לקמן ג.)", "Tosafot on Shabbat 3a:1:1"),
            ("(לעיל ג.)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן קס:)", "Tosafot on Shabbat 3a:1:1"),
            ("(לקמן דף פנ:)", "Rashi on Gittin 3a:1:1"),
        ]:
            with self.subTest(anchor=anchor, source=source):
                self.assertIsNone(self.recover(anchor, source))

    def test_noise_and_unclear_anchors_stay_unlinked(self):
        for anchor in [
            "(לקמן קיב)",
            "(לקמן כז)",
            "(לקמן סי' נז)",
            "(לעיל סי' קנח)",
            "(לקמן ה\"ה)",
            "(לקמן שם)",
            "(לעיל בפרק הלוקח בהמה)",
            "(לעיל ריש פירקין)",
            "(לעיל דך לח:)",
            "(לקמן דן קמא: ושם)",
            "(לקמן ' ף קז: ושם)",
            "(לקמן המצניע דף צא:)",
            "(לעיל עמוד א)",
            "(לקמן סד,סוע\"ב)",
            "(לעיל . כט:)",
            "(לקמן קמיב:)",
            "(לקמן יא: ושם)",
            "(לקמן יא: ודף יב.)",
            "בגמרא לקמן (דף יג.",
            "לקמן ו: ד\"ה ושמואל",
            "לקמן פ\"ב.",
            "(לקמן פנ:)",
            "(לעיל יה.)",
            "(לעיל ס\"ס.)",
            "(לקמן <b>יא:</b>)",
            "יא:",
        ]:
            with self.subTest(anchor=anchor):
                self.assertIsNone(self.recover(anchor, "Tosafot on Shabbat 30a:1:1"
                                               if "לעיל" in anchor else "Tosafot on Shabbat 3a:1:1"))

    def test_only_the_tractate_or_its_commentary_supplies_the_tractate(self):
        self.assertIsNone(self.recover("(לקמן יא:)", "Rif Gittin 3a:1"))
        self.assertIsNone(self.recover("(לקמן יא:)", "Hagahot HaBach on Rif Gittin 5a:1"))
        for source in ("Rif Gittin 3a:1", "Hagahot HaBach on Rif Gittin 5a:1"):
            self.assertIsNone(link_books.own_tractate_relative_ref(
                "(לקמן גיטין דף כג:)", TalmudRef(source), TalmudRef))
        self.assertIsNone(link_books.untitled_relative_ref("(לקמן יא:)", None, TalmudRef))
        self.assertIsNone(link_books.untitled_relative_ref(
            "(לקמן יא:)", TalmudRef("Tosafot on Shabbat 3a:1:1"), None))


class TalmudPositionsTest(unittest.TestCase):
    """A non-Sefaria "X על <tractate>" is placed by its own daf headings (Otzaria/otzaria#2408)."""

    BOOK = link_books.BookKey("DictaToOtzaria", "חידושי הר\"ן על גיטין")

    def positions(self, contents, book=BOOK):
        lines = [(i, content, f"{book.canonical_he_title} {i + 1}")
                 for i, content in enumerate(contents)]
        found = link_books.talmud_positions(book, lines, TalmudRef)
        return {i: ref.normal() for i, ref in found.items()}

    def test_a_daf_heading_with_its_amud_places_the_lines_below_it(self):
        self.assertEqual(self.positions([
            "<h1>חידושי הר\"ן על גיטין</h1>",
            "הקדמה",
            "<h2>דף ד.</h2>",
            "מתני'",
            "<h2>דף ד:</h2>",
            "גמ'",
            "<h2>דף ה ע\"א</h2>",
            "שם",
            "<h2>דף ה עמוד ב</h2>",
            "<h3>רש\"י</h3>",
            "ד\"ה",
        ]), {2: "Gittin 4a", 3: "Gittin 4a", 4: "Gittin 4b", 5: "Gittin 4b", 6: "Gittin 5a",
             7: "Gittin 5a", 8: "Gittin 5b", 9: "Gittin 5b", 10: "Gittin 5b"})

    def test_an_unclear_or_contradicted_heading_places_nothing(self):
        self.assertEqual(self.positions([
            "<h2>סימן א</h2>", "<h3>ב.</h3>", "טקסט",
            "<h2>דף יד:</h2>", "<h3>צ' ע\"א</h3>", "טקסט", "<h3>רש\"י</h3>", "טקסט",
            "<h2>דף ד</h2>", "טקסט",
            "<h2>דף יה.</h2>", "טקסט",
            "<h2>דף צה.</h2>", "טקסט",
            "<h2>דף ו.</h2>", "<h3>דף ז.</h3>", "טקסט",
            "<h2>פרק שני</h2>", "טקסט",
            "<h2>דף ו - ז</h2>", "<h3>דף ו.</h3>", "טקסט",
            "<h2>דף ז.</h2>", "<h3>ב</h3>", "טקסט",
        ]), {3: "Gittin 14b", 6: "Gittin 14b", 7: "Gittin 14b", 14: "Gittin 6a",
             22: "Gittin 7a", 23: "Gittin 7a", 24: "Gittin 7a"})

    def test_only_a_non_sefaria_commentary_on_a_bavli_tractate_is_placed(self):
        contents = ["<h2>דף ד.</h2>", "טקסט"]
        self.assertEqual(self.positions(contents), {0: "Gittin 4a", 1: "Gittin 4a"})
        for book in [
            link_books.BookKey("Sefaria", "חידושי הר\"ן על גיטין"),
            link_books.BookKey("KSK", "קובץ שיטות קמאי על גיטין"),
            link_books.BookKey("DictaToOtzaria", "חידושים על רי\"ף גיטין"),
            link_books.BookKey("DictaToOtzaria", "חידושים על בראשית"),
            link_books.BookKey("DictaToOtzaria", "גיטין"),
        ]:
            with self.subTest(book=book):
                self.assertEqual(self.positions(contents, book), {})
        self.assertEqual(link_books.talmud_positions(
            self.BOOK, [(0, "<h2>דף ד.</h2>", "x"), (1, "טקסט", "x")], None), {})
        # Only the tractate's own title: Ref may also accept an alternate one.
        alias = {"מסכת גיטין": "Gittin"}
        self.assertEqual(link_books.talmud_positions(
            link_books.BookKey("DictaToOtzaria", "חידושים על מסכת גיטין"),
            [(0, "<h2>דף ד.</h2>", "x")], lambda text: TalmudRef(alias.get(text, text))), {})


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


class TanakhRef:
    """A Sefaria Ref stand-in for "<Hebrew title> <chapter> [<verse>]"; Psalms 18 has 51 verses."""
    TITLES = {"תהלים": "Psalms", "דברים": "Deuteronomy", "שה\"ש": "Song of Songs",
              "מלכים א": "I Kings"}
    primary_category = "Tanakh"

    def __init__(self, text):
        # Sefaria reads the longest title first: "מלכים א יח" is I Kings 18.
        title, numbers = re.fullmatch(r"(.+)((?: [א-ת]{1,3}){1,2})", text).groups()
        if title not in self.TITLES:
            title, numbers = re.fullmatch(r"(.+?)((?: [א-ת]{1,3}){1,2})", text).groups()
        values = [link_books._numeral(n) for n in numbers.split()]
        if title not in self.TITLES or None in values:
            raise ValueError(text)
        # Like Sefaria's Ref, a verse past the end of the chapter parses but is empty.
        self.empty = values[:1] == [18] and values[-1] > 51
        self.sections = values
        self.text = f"{self.TITLES[title]} {':'.join(map(str, values))}"

    def is_empty(self):
        return self.empty

    def normal(self):
        return self.text


class GereshNumeralRefTest(unittest.TestCase):
    """Cases from "שבע נתיבות התורה", which Sefaria leaves unresolved (otzaria book 6129)."""

    def rebuild(self, anchor, ref_factory=TanakhRef):
        ref = link_books.geresh_numeral_ref(anchor, ref_factory)
        return ref.normal() if ref is not None else None

    def test_a_geresh_after_a_multi_letter_number_is_read(self):
        for anchor, target in [
            ("(תהלים יח' לא')", "Psalms 18:31"),
            ("תהלים קיט' קסה'", "Psalms 119:165"),
            ("(דברים י' טז')", "Deuteronomy 10:16"),
            ("(שה\"ש ח' יג')", "Song of Songs 8:13"),
            ("(תהלים יח׳ לא׳)", "Psalms 18:31"),
            ("(תהלים יח', לא')", "Psalms 18:31"),
            ("(תהלים מה')", "Psalms 45"),
            ("(תהלים יח’ לא’)", "Psalms 18:31"),
            ("(מלכים א' יח')", "I Kings 18"),
            ("(מלכים א' יח' לא')", "I Kings 18:31"),
        ]:
            with self.subTest(anchor=anchor):
                self.assertEqual(self.rebuild(anchor), target)

    def test_anchors_sefaria_already_reads_are_left_to_it(self):
        for anchor in ("(דברים ל' ו')", "(תהלים יח לא)", "(תהלים י\"ח ל\"א)"):
            with self.subTest(anchor=anchor):
                self.assertIsNone(self.rebuild(anchor))

    def test_a_standard_quoted_number_can_accompany_the_bad_geresh(self):
        for geresh in link_books._GERESH:
            for quotes in ('"', '״', '“', '”', "''"):
                for anchor, target in (
                    (f"(תהלים י{quotes}ח לא{geresh})", "Psalms 18:31"),
                    (f"(תהלים יח{geresh} ל{quotes}א)", "Psalms 18:31"),
                    (f"(מלכים א' י{quotes}ח לא{geresh})", "I Kings 18:31"),
                    (f"(מלכים א' יח{geresh} ל{quotes}א)", "I Kings 18:31"),
                    (f"(תהלים קי{quotes}ט קסה{geresh})", "Psalms 119:165"),
                    (f"(תהלים קיט{geresh} קס{quotes}ה)", "Psalms 119:165"),
                ):
                    with self.subTest(anchor=anchor):
                        self.assertEqual(self.rebuild(anchor), target)

    def test_mixed_quotes_do_not_relax_canonical_numbers_or_anchor_boundaries(self):
        for anchor in (
            '(תהלים ח"י לא\')', '(תהלים יח\' א"ב)', '(תהלים יח\' ע"ג)',
            '(תהלים י"ח לא\' ב\')', '(תהלים י"ח ל"א)',
            '(תהלים יח" לא\')', '(תהלים י""ח לא\')',
            '(שם י"ח לא\')', '(שבת י"ב כב\')',
        ):
            with self.subTest(anchor=anchor):
                self.assertIsNone(self.rebuild(anchor))

    def test_section_mismatch_does_not_query_text_availability(self):
        from unittest.mock import Mock

        ref = Mock(primary_category="Tanakh", sections=[18])
        self.assertIsNone(self.rebuild('(תהלים י"ח לא\')', lambda _text: ref))
        ref.is_empty.assert_not_called()

    def test_unclear_or_impossible_anchors_stay_unlinked(self):
        for anchor in (
            "(שם כו')",              # no title for Ref to read
            "(תהלים יח' עג')",       # past the end of the chapter
            "(תהלים חי' לא')",       # not a canonical number
            "(תהלים יח' לא' ב')",    # more than a chapter and a verse
        ):
            with self.subTest(anchor=anchor):
                self.assertIsNone(self.rebuild(anchor))
        self.assertIsNone(self.rebuild("(תהלים יח' לא')", None))

    def test_only_a_tanakh_ref_with_the_spelled_sections_is_kept(self):
        """"(שבת יח')" is read as an amud and "(שבת יב' ב')" as a Mishnah: neither is linked."""
        talmud = type("Ref", (), {"primary_category": "Talmud", "sections": [35],
                                  "is_empty": lambda _self: False})()
        mishnah = type("Ref", (), {"primary_category": "Mishnah", "sections": [12, 2],
                                   "is_empty": lambda _self: False})()
        self.assertIsNone(self.rebuild("(שבת יח')", lambda text: talmud))
        self.assertIsNone(self.rebuild("(שבת יב' ב')", lambda text: mishnah))


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
