import os
import sys
import types
import unittest

sys.modules.setdefault("resource", types.SimpleNamespace(
    getrusage=lambda *_: types.SimpleNamespace(ru_maxrss=0), RUSAGE_SELF=0,
    getrlimit=lambda *_: (0, 0), setrlimit=lambda *_: None, RLIMIT_AS=0, RLIM_INFINITY=-1,
))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import link_books  # noqa: E402


class _Span:
    def __init__(self, start, end):
        self.range = (start, end)


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
    return types.SimpleNamespace(span=_Span(start, end), raw_ref_parts=found)


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


if __name__ == "__main__":
    unittest.main()
