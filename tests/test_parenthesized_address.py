import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("resource", types.SimpleNamespace(
    getrusage=lambda *_: types.SimpleNamespace(ru_maxrss=0), RUSAGE_SELF=0,
    getrlimit=lambda *_: (0, 0), setrlimit=lambda *_: None, RLIMIT_AS=0, RLIM_INFINITY=-1,
))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class _Span:
    def __init__(self, start, end, text=""):
        self.range = (start, end)
        self.text = text


class _Part:
    def __init__(self, type_name, start, end, text):
        self.type = types.SimpleNamespace(name=type_name)
        self.span = _Span(start, end)
        self.text = text


def _raw(content, start, end, parts):
    """A RawRef stand-in; part offsets are relative to the citation, as in Sefaria."""
    text = content[start:end]
    found = []
    cursor = 0
    for type_name, part_text in parts:
        at = text.index(part_text, cursor)
        found.append(_Part(type_name, at, at + len(part_text), part_text))
        cursor = at + len(part_text)
    return types.SimpleNamespace(span=_Span(start, end, text), raw_ref_parts=found)


class ParenthesizedAddressPartsTest(unittest.TestCase):
    """Citations from Otzaria/otzaria#2490 whose leading chapter phrase fails resolution."""

    def first(self, content, citation, parts):
        start = content.index(citation)
        return link_books.parenthesized_address_parts(
            _raw(content, start, start + len(citation), parts), content)

    def test_the_reported_forms_split_at_the_parenthesis(self):
        for content, citation, parts in [
            ("דדרשינן בריש מרובה (ב\"ק דף סג ושם)", "בריש מרובה (ב\"ק דף סג",
             [("NAMED", "בריש מרובה"), ("NAMED", "ב\"ק"), ("NUMBERED", "דף סג")]),
            ("אבל בריש המפקיד (ב\"מ דף לד:) הקשה", "בריש המפקיד (ב\"מ דף לד:",
             [("NAMED", "בריש המפקיד"), ("NAMED", "ב\"מ"), ("NUMBERED", "דף לד:")]),
            ("כדאמר בפרק דלקמן (שבועות דף מז:) מנה", "בפרק דלקמן (שבועות דף מז:",
             [("RELATIVE", "בפרק דלקמן"), ("NAMED", "שבועות"), ("NUMBERED", "דף מז:")]),
        ]:
            with self.subTest(citation=citation):
                self.assertEqual(self.first(content, citation, parts), 1)

    def test_an_address_without_its_own_title_is_not_split(self):
        content = "הכל מפורש בפרק שני דכתובות (דף יח: ושם"
        self.assertIsNone(self.first(content, "בפרק שני דכתובות (דף יח",
                                     [("NAMED", "בפרק שני דכתובות"), ("NUMBERED", "דף יח")]))

    def test_no_parenthesis_or_a_closed_one_is_not_split(self):
        content = "בפרק הזהב ב\"מ דף מד."
        self.assertIsNone(self.first(content, content, [
            ("NAMED", "בפרק הזהב"), ("NAMED", "ב\"מ"), ("NUMBERED", "דף מד.")]))
        content = "בפרק הזהב (ב\"מ) דף מד."
        self.assertIsNone(self.first(content, content, [
            ("NAMED", "בפרק הזהב"), ("NAMED", "ב\"מ"), ("NUMBERED", "דף מד.")]))

    def test_nothing_before_the_parenthesis_is_not_split(self):
        content = "(ב\"ק דף סג:"
        self.assertIsNone(self.first(content, content, [("NAMED", "ב\"ק"), ("NUMBERED", "דף סג:")]))

    def test_a_commentator_before_the_parenthesis_is_the_cited_work(self):
        content = "ועיין בטו\"ז (או\"ח סי' קכ\"ח סק\"ב) שכתב"
        self.assertIsNone(self.first(content, "בטו\"ז (או\"ח סי' קכ\"ח סק\"ב", [
            ("NAMED", "בטו\"ז"), ("NAMED", "או\"ח"), ("NUMBERED", "סי' קכ\"ח"), ("NUMBERED", "סק\"ב")]))
        content = "כמ\"ש המל\"מ (פ\"י מה' מלכים)"
        self.assertIsNone(self.first(content, "המל\"מ (פ\"י מה' מלכים", [
            ("NAMED", "המל\"מ"), ("NUMBERED", "פ\"י"), ("NAMED", "מה' מלכים")]))

    def test_chapter_locators_with_html_or_prefixes_are_accepted(self):
        for lead in ("בס\"פ השולח", "ובסוף השולח", "פרק המקבל", "בפ' אין מעמידין", "<b>בפרק</b> הזהב"):
            content = f"{lead} (גיטין דף מז:)"
            with self.subTest(lead=lead):
                self.assertEqual(self.first(content, content[:-1], [
                    ("NAMED", lead), ("NAMED", "גיטין"), ("NUMBERED", "דף מז:")]), 1)

    def test_a_number_or_ibid_outside_the_address_is_not_dropped(self):
        content = "פרק ב (ב\"ק דף סג:"
        self.assertIsNone(self.first(content, content, [
            ("NUMBERED", "פרק ב"), ("NAMED", "ב\"ק"), ("NUMBERED", "דף סג:")]))
        content = "בריש מרובה (שם דף סג:"
        self.assertIsNone(self.first(content, content, [
            ("NAMED", "בריש מרובה"), ("IBID", "שם"), ("NUMBERED", "דף סג:")]))


class _TalmudRef:
    VALID = {"Gittin 51", "Gittin 51a", "Gittin 51b", "Genesis 51", "Bava Kamma 93", "Bava Kamma 93a"}

    def __init__(self, text):
        if text not in self.VALID:
            raise ValueError(text)
        self.text = text

    def normal(self):
        return self.text

    def is_talmud(self):
        return not self.text.startswith("Genesis")


class AmudMarkedDafTest(unittest.TestCase):
    def mark(self, target, content, anchor):
        start = content.index(anchor)
        end = start + len(anchor)
        found = link_books.amud_marked_daf(_TalmudRef(target), content, start, end, _TalmudRef)
        return None if found is None else (found[0].normal(), content[start:found[1]])

    def test_a_colon_after_the_daf_names_amud_b(self):
        self.assertEqual(self.mark("Gittin 51", "(גיטין דף נא: ושם)", "גיטין דף נא"),
                         ("Gittin 51b", "גיטין דף נא:"))
        self.assertEqual(self.mark("Gittin 51", "כמבואר בגיטין דף נ\"א:, דמ\"מ", "גיטין דף נ\"א"),
                         ("Gittin 51b", "גיטין דף נ\"א:"))

    def test_a_period_is_not_trusted(self):
        # "(X דף כג. ובY דף כד)" uses "." as a list separator.
        self.assertIsNone(self.mark("Bava Kamma 93", "(ב\"ק דף צג. ושם)", "ב\"ק דף צג"))

    def test_a_line_final_colon_closes_the_paragraph(self):
        self.assertIsNone(self.mark("Gittin 51", "רש\"י בגיטין דף נא: <br/> ", "גיטין דף נא"))

    def test_the_token_before_the_mark_must_spell_the_daf(self):
        self.assertIsNone(self.mark("Gittin 51", "וכן בגיטין שם: ושם", "גיטין שם"))

    def test_unmarked_or_non_talmud_targets_are_kept(self):
        self.assertIsNone(self.mark("Bava Kamma 93", "(בבא קמא דף צג) גבי", "בבא קמא דף צג"))
        self.assertIsNone(self.mark("Genesis 51", "(בראשית נא: ושם)", "בראשית נא"))
        self.assertIsNone(self.mark("Gittin 51b", "(גיטין דף נא: ושם)", "גיטין דף נא"))

    def test_a_missing_amud_is_not_invented(self):
        self.assertIsNone(self.mark("Bava Kamma 93", "(ב\"ק דף צג: ושם)", "ב\"ק דף צג"))


class ChapterLedSplitsTest(unittest.TestCase):
    """"בפרק שני דכתובות (דף יח:" names its tractate after the chapter (Otzaria/otzaria#2490)."""

    def splits(self, citation, parts):
        return [(lead, i) for lead, i, _ in link_books.chapter_led_splits(_raw(citation, 0, len(citation), parts))]

    def test_a_chapter_fused_to_its_tractate_splits_at_the_dalet(self):
        self.assertEqual(self.splits("בפרק שני דכתובות (דף יח", [("NAMED", "בפרק שני דכתובות"), ("NUMBERED", "דף יח")]),
                         [("בפרק שני", 0)])

    def test_a_separate_title_part_follows_the_lead(self):
        self.assertEqual(self.splits("שלהי פ\"ק דסנהדרין <small> (דף ט\"ז", [
            ("NAMED", "שלהי"), ("NUMBERED", "פ\"ק"), ("NAMED", "דסנהדרין"), ("NUMBERED", "דף ט\"ז")]),
            [("שלהי פ\"ק", 2)])

    def test_no_address_or_a_non_address_part_is_not_split(self):
        self.assertEqual(self.splits("בפרק שני דכתובות", [("NAMED", "בפרק שני דכתובות")]), [])
        self.assertEqual(self.splits("בפרק שני דכתובות (שם ד\"ה ובכולי", [
            ("NAMED", "בפרק שני דכתובות"), ("IBID", "שם"), ("DH", "ד\"ה ובכולי")]), [])
        self.assertEqual(self.splits("בפרק דלקמן דף יח", [("RELATIVE", "בפרק דלקמן"), ("NUMBERED", "דף יח")]), [])


_TERMS = {
    "perek": ["פרק", "ס\"פ", "ר\"פ", "פ'"],
    "second": ["שני", "פ\"ב", "רפ\"ב", "ספ\"ב", "ב", "ב׳", "ב'"],
    "chapter-214": ["האשה שנתארמלה"],
}
_SECOND = link_books.chapter_names([{"term_slugs": ["perek", "chapter-214"]}, {"term_slugs": ["perek", "second"]},
                                    {"term_slugs": ["chapter-214"]}], _TERMS.__getitem__)


class ChapterNamesTest(unittest.TestCase):
    def named(self, lead):
        return bool(link_books.chapter_lead_names(lead) & _SECOND)

    def test_sefaria_spellings_with_prefixes_and_edges(self):
        for lead in ("בפרק שני", "פ\"ב", "בפ״ב", "ברפ\"ב", "ובס\"פ ב'", "בריש פרק האשה שנתארמלה",
                     "שלהי פ\"ב", "<b>בפרק</b> שני", "האשה שנתארמלה"):
            with self.subTest(lead=lead):
                self.assertTrue(self.named(lead))

    def test_other_leads_name_no_chapter(self):
        for lead in ("בפרק שלישי", "פ\"ק", "ב", "שני", "הירושלמי פרק שני", "רש\"י פ\"ב", "פרק האשה"):
            with self.subTest(lead=lead):
                self.assertFalse(self.named(lead))


class _Amudim:
    """A Ref stand-in: Bava Metzia chapter 3 ends at 44a, chapter 4 starts there."""
    SPANS = {"Bava Metzia 44": {"44a", "44b"}, "Bava Metzia 44a": {"44a"}, "Bava Metzia 44b": {"44b"},
             "Bava Metzia 33b-44a": {"33b", "44a"}, "Bava Metzia 44a-60b": {"44a", "44b", "60b"}}

    def __init__(self, text):
        if text not in self.SPANS:
            raise ValueError(text)
        self.text = text

    def normal(self):
        return self.text

    def is_talmud(self):
        return True

    def overlaps(self, other):
        return bool(self.SPANS[self.text] & self.SPANS[other.text])


class ChapterLedAmudTest(unittest.TestCase):
    """The amud written after a chapter-led citation must agree with that chapter."""

    def records(self, content, chapter, target="Bava Metzia 44"):
        start = content.index("בפרק")
        end = content.index("מד") + 2
        rr = types.SimpleNamespace(resolution_failed=True, raw_entity=_raw(content, start, end, []))
        found = None if chapter is None else (_Amudim(target), _Amudim(chapter))
        linker = types.SimpleNamespace(bulk_link=lambda *_a, **_k: [types.SimpleNamespace(resolved_refs=[rr])])
        with mock.patch.object(link_books, "parenthesized_address", return_value=None), \
                mock.patch.object(link_books, "chapter_led_citation", return_value=found):
            records = link_books.process_batch(linker, link_books.BookKey("MoreBooks", "ספר"),
                                               [(0, content, None)], lambda _line: None,
                                               context_ref_factory=_Amudim)[0]
        return [(r.target_ref, content[r.start:r.end]) for r in records]

    def test_an_amud_outside_the_named_chapter_drops_the_citation(self):
        self.assertEqual(self.records("ועיין בפרק שלישי דבבא מציעא (דף מד: ושם)", "Bava Metzia 33b-44a"), [])

    def test_an_amud_inside_the_named_chapter_is_kept(self):
        content = "ועיין בפרק הזהב דבבא מציעא (דף מד: ושם)"
        self.assertEqual(self.records(content, "Bava Metzia 44a-60b"),
                         [("Bava Metzia 44b", "בפרק הזהב דבבא מציעא (דף מד:")])

    def test_an_unmarked_or_line_final_daf_stays_whole(self):
        for content in ("ועיין בפרק שלישי דבבא מציעא (דף מד) ושם", "ועיין בפרק שלישי דבבא מציעא (דף מד:"):
            with self.subTest(content=content):
                self.assertEqual(self.records(content, "Bava Metzia 33b-44a"),
                                 [("Bava Metzia 44", content[content.index("בפרק"):content.index("מד") + 2])])

    def test_no_chapter_reading_links_nothing(self):
        self.assertEqual(self.records("ועיין בפרק שלישי דבבא מציעא (דף מד: ושם)", None), [])


if __name__ == "__main__":
    unittest.main()
