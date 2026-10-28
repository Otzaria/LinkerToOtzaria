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

    def test_spaces_and_tags_before_the_colon_preserve_the_original_anchor(self):
        for gap in (" ", "\t", "\u00a0", "</small>", " </small> <b> "):
            anchor = "גיטין דף נא"
            with self.subTest(gap=gap):
                self.assertEqual(self.mark("Gittin 51", f"({anchor}{gap}: ושם)", anchor),
                                 ("Gittin 51b", f"{anchor}{gap}:"))

    def test_html_already_inside_the_anchor_does_not_hide_the_number(self):
        anchor = 'גיטין דף <small title="עמוד">נא</small>'
        self.assertEqual(self.mark("Gittin 51", f"({anchor} : ושם)", anchor),
                         ("Gittin 51b", anchor + " :"))

    def test_a_period_is_not_trusted(self):
        # "(X דף כג. ובY דף כד)" uses "." as a list separator.
        for gap in ("", " ", "</small>"):
            with self.subTest(gap=gap):
                self.assertIsNone(self.mark("Bava Kamma 93", f"(ב\"ק דף צג{gap}. ושם)", "ב\"ק דף צג"))

    def test_a_line_final_colon_closes_the_paragraph(self):
        for gap in ("", " ", "</small>", " </small> <b> "):
            with self.subTest(gap=gap):
                self.assertIsNone(self.mark("Gittin 51", f"רש\"י בגיטין דף נא{gap}: <br/> ", "גיטין דף נא"))

    def test_the_token_before_the_mark_must_spell_the_daf(self):
        for gap in ("", " ", "</small>"):
            with self.subTest(gap=gap):
                self.assertIsNone(self.mark("Gittin 51", f"וכן בגיטין שם{gap}: ושם", "גיטין שם"))
                self.assertIsNone(self.mark("Gittin 51", f"וכן בגיטין דף נב{gap}: ושם", "גיטין דף נב"))

    def test_intervening_text_is_not_skipped_to_find_a_colon(self):
        self.assertIsNone(self.mark("Gittin 51", "גיטין דף נא ועוד: ושם", "גיטין דף נא"))
        self.assertIsNone(self.mark("Gittin 51", "גיטין דף נא</small> ועוד: ושם", "גיטין דף נא"))

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
    """A Ref stand-in: Bava Metzia chapter 3 (המפקיד) ends at 44a, chapter 4 (הזהב) opens there."""
    SPANS = {"Bava Metzia 44": {"44a", "44b"}, "Bava Metzia 44a": {"44a"}, "Bava Metzia 44b": {"44b"},
             "Bava Metzia 33": {"33a", "33b"}, "Bava Metzia 33a": {"33a"}, "Bava Metzia 33b": {"33b"},
             "Bava Metzia 33b-44a": {"33b", "44a"}, "Bava Metzia 44a-60b": {"44a", "44b", "60b"}}
    index = types.SimpleNamespace(title="Bava Metzia")

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


_CHAPTER_3, _CHAPTER_4 = _Amudim("Bava Metzia 33b-44a"), _Amudim("Bava Metzia 44a-60b")
_BAVA_METZIA = ((_CHAPTER_3, frozenset({"פרק המפקיד", "המפקיד", "פרק שלישי"})),
                (_CHAPTER_4, frozenset({"פרק הזהב", "הזהב", "פרק רביעי"})))


def _batch(content, rr, linker, **patches):
    linker.bulk_link = lambda *_a, **_k: [types.SimpleNamespace(resolved_refs=[rr])]
    with mock.patch.multiple(link_books, _book_chapters=lambda _title: _BAVA_METZIA, **patches):
        records = link_books.process_batch(linker, link_books.BookKey("MoreBooks", "ספר"),
                                           [(0, content, None)], lambda _line: None,
                                           context_ref_factory=_Amudim)[0]
    return [(r.target_ref, content[r.start:r.end]) for r in records]


class ResolvedAmudAnchorTest(unittest.TestCase):
    def test_a_spaced_or_tagged_mark_keeps_original_utf16_offsets_and_hash(self):
        for prefix in ("ועיין ", "😀 ועיין "):
            for gap in (" ", "</small>"):
                with self.subTest(prefix=prefix, gap=gap):
                    content = f"{prefix}ב\"מ דף מד{gap}: ושם"
                    start, end = content.index('ב"מ'), content.index("מד") + 2
                    rr = types.SimpleNamespace(
                        resolution_failed=False, is_ambiguous=False, context_type=None,
                        ref=_Amudim("Bava Metzia 44"), raw_entity=_raw(content, start, end, []))
                    linker = types.SimpleNamespace(bulk_link=lambda *_a, **_k: [
                        types.SimpleNamespace(resolved_refs=[rr])])
                    records = link_books.process_batch(
                        linker, link_books.BookKey("MoreBooks", "ספר"), [(0, content, None)],
                        lambda _line: None, context_ref_factory=_Amudim)[0]
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    self.assertEqual(record.target_ref, "Bava Metzia 44b")
                    self.assertEqual(record.start, len(content[:start].encode("utf-16-le")) // 2)
                    self.assertEqual(record.end, len(content[:content.index(":") + 1].encode("utf-16-le")) // 2)
                    self.assertEqual(record.source_hash, link_books.content_hash(content))


class ChapterLedAmudTest(unittest.TestCase):
    """A chapter-led citation is where its daf and its named chapter meet."""

    def records(self, content, chapter, target="Bava Metzia 44"):
        start = content.index("בפרק")
        end = content.index("מד") + 2
        rr = types.SimpleNamespace(resolution_failed=True, raw_entity=_raw(content, start, end, []))
        found = None if chapter is None else (_Amudim(target), (chapter,))
        return _batch(content, rr, types.SimpleNamespace(), parenthesized_address=lambda *_: None,
                      chapter_led_citation=lambda *_: found)

    def anchor(self, content):
        return content[content.index("בפרק"):content.index("מד") + 2]

    def test_an_amud_outside_the_named_chapter_drops_the_citation(self):
        for content in ("ועיין בפרק שלישי דבבא מציעא (דף מד: ושם)", "ועיין בפרק שלישי דבבא מציעא (דף מד:",
                        "ועיין בפרק שלישי דבבא מציעא דף מד ע\"ב ושם", "ועיין בפרק שלישי דבבא מציעא (דף מד</small>: ושם)",
                        "ועיין בפרק שלישי דבבא מציעא דף מד : ושם"):
            with self.subTest(content=content):
                self.assertEqual(self.records(content, _CHAPTER_3), [])

    def test_an_amud_inside_the_named_chapter_is_kept(self):
        for gap in ("", " ", "</small>", " </small> <b> "):
            content = f"ועיין בפרק הזהב דבבא מציעא (דף מד{gap}: ושם)"
            with self.subTest(gap=gap):
                self.assertEqual(self.records(content, _CHAPTER_4),
                                 [("Bava Metzia 44b", self.anchor(content) + gap + ":")])
        for content in ("ועיין בפרק שלישי דבבא מציעא דף מד. ושם", "ועיין בפרק שלישי דבבא מציעא דף מד ע\"א"):
            with self.subTest(content=content):
                self.assertEqual(self.records(content, _CHAPTER_3), [("Bava Metzia 44a", self.anchor(content))])

    def test_an_unmarked_daf_narrows_to_the_amud_in_its_chapter(self):
        content = "ועיין בפרק שלישי דבבא מציעא (דף מד) ושם"
        self.assertEqual(self.records(content, _CHAPTER_3), [("Bava Metzia 44a", self.anchor(content))])

    def test_a_daf_inside_its_chapter_stays_whole(self):
        content = "ועיין בפרק הזהב דבבא מציעא (דף מד. ושם)"
        self.assertEqual(self.records(content, _CHAPTER_4), [("Bava Metzia 44", self.anchor(content))])

    def test_no_chapter_reading_links_nothing(self):
        self.assertEqual(self.records("ועיין בפרק שלישי דבבא מציעא (דף מד: ושם)", None), [])


class _RealignedPart(_Part):
    def realign_to_new_raw_ref(self, _old, _new):
        return self


class ParenthesizedChapterTest(unittest.TestCase):
    """The chapter before a parenthesized address is checked like a chapter before its title."""

    def records(self, content, lead_type, target):
        start = content.index("בפרק")
        daf = "מד" if "מד" in content else "לג"
        end = content.index(daf) + 2
        lead = content[start:content.index(" (")]
        raw = _raw(content, start, end, [(lead_type, lead), ("NAMED", "ב\"מ"), ("NUMBERED", f"דף {daf}")])
        raw.raw_ref_parts = [_RealignedPart(p.type.name, *p.span.range, p.text) for p in raw.raw_ref_parts]
        first = raw.raw_ref_parts[1].span.range[0]
        raw.subspan = lambda _parts: _Span(start + first, end)
        raw.lang = "he"
        rr = types.SimpleNamespace(resolution_failed=True, raw_entity=raw)
        resolved = types.SimpleNamespace(is_ambiguous=False, ref=_Amudim(target), context_type=None)
        linker = types.SimpleNamespace(_ref_resolver=types.SimpleNamespace(resolve_raw_ref=lambda *_: resolved))
        ref_part = types.ModuleType("sefaria.model.linker.ref_part")
        ref_part.RawRef = lambda *args: args
        with mock.patch.dict(sys.modules, {"sefaria": types.ModuleType("sefaria"),
                                           "sefaria.model": types.ModuleType("sefaria.model"),
                                           "sefaria.model.linker": types.ModuleType("sefaria.model.linker"),
                                           "sefaria.model.linker.ref_part": ref_part}):
            return _batch(content, rr, linker, chapter_led_citation=lambda *_: None)

    def test_the_address_stands_when_the_chapter_disagrees(self):
        # Authors use some chapter names for other chapters than Sefaria ("דיני ממונות בתרא").
        self.assertEqual(self.records("ועיין בפרק הזהב (ב\"מ דף לג ושם)", "NAMED", "Bava Metzia 33"),
                         [("Bava Metzia 33", "ב\"מ דף לג")])
        for gap in ("", " ", "</small>", " </small> <b> "):
            with self.subTest(gap=gap):
                self.assertEqual(self.records(f"ועיין בפרק המפקיד (ב\"מ דף מד{gap}: ושם)", "NAMED", "Bava Metzia 44"),
                                 [("Bava Metzia 44b", f"ב\"מ דף מד{gap}:")])

    def test_an_address_in_its_chapter_keeps_the_amud(self):
        self.assertEqual(self.records("ועיין בפרק הזהב (ב\"מ דף מד: ושם)", "NAMED", "Bava Metzia 44"),
                         [("Bava Metzia 44b", "ב\"מ דף מד:")])
        self.assertEqual(self.records("ועיין בפרק המפקיד (ב\"מ דף מד) ושם", "NAMED", "Bava Metzia 44"),
                         [("Bava Metzia 44a", "ב\"מ דף מד")])

    def test_a_lead_naming_no_chapter_of_the_book_is_not_checked(self):
        self.assertEqual(self.records("ועיין בפרק דלקמן (ב\"מ דף לג ושם)", "RELATIVE", "Bava Metzia 33"),
                         [("Bava Metzia 33", "ב\"מ דף לג")])


_LEAF = object()


class _Titles:
    """A title-trie continuation: the works a key completes (none when it only opens titles)."""

    def __init__(self, *works):
        self.nodes = [types.SimpleNamespace(ref=lambda work=work: types.SimpleNamespace(index=work))
                      for work in works]

    def __contains__(self, key):
        return key is _LEAF and bool(self.nodes)

    def __getitem__(self, key):
        assert key is _LEAF
        return self.nodes


def _work(title, category, base=()):
    return types.SimpleNamespace(title=title, categories=[category], base_text_titles=list(base))


_LEVITICUS = _work("Leviticus", "Tanakh")
_NIDDAH = _work("Niddah", "Talmud")
_TRIE = {
    "בנדה": _Titles(_NIDDAH, _work("Mishnah Niddah", "Mishnah")),
    "ירושלמי": _Titles(),
    "רש\"י": _Titles(_work("Rashi on Leviticus", "Tanakh", ["Leviticus"])),
    "ויקרא": _Titles(_LEVITICUS),
    "בספר הישר": _Titles(_work("Sefer HaYashar", "Talmud")),
    "בטור": _Titles(_work("Tur", "Halakhah")),
    "ובתוספתא": _Titles(_work("Tosefta Niddah", "Tosefta")),
}


def _ref(normal, category):
    index = types.SimpleNamespace(title=normal.rsplit(" ", 1)[0], categories=category, base_text_titles=[])
    return types.SimpleNamespace(index=index, normal=lambda: normal, is_talmud=lambda: category[0] == "Talmud")


class LeadNamesOtherWorksTest(unittest.TestCase):
    """Words the NER joins before a parenthesized citation (Otzaria/otzaria#3453)."""

    GENESIS = _ref("Genesis 1:5", ["Tanakh", "Torah"])
    SANHEDRIN = _ref("Sanhedrin 24a", ["Talmud", "Bavli", "Seder Nezikin"])

    def outside(self, lead, ref=None, chapters=()):
        ref = ref or _ref("Leviticus 15:19", ["Tanakh", "Torah"])
        raw = types.SimpleNamespace(raw_ref_parts=[_Part(kind, 0, len(text), text) for kind, text in lead])
        trie = types.SimpleNamespace(get_continuations=lambda key, allow_partial: (_TRIE.get(key), None))
        linker = types.SimpleNamespace(_ref_resolver=types.SimpleNamespace(get_ref_part_title_trie=lambda: trie))
        ref_part = types.ModuleType("sefaria.model.linker.ref_part")
        ref_part.LEAF_TRIE_ENTRY = _LEAF
        with mock.patch.dict(sys.modules, {"sefaria": types.ModuleType("sefaria"),
                                           "sefaria.model": types.ModuleType("sefaria.model"),
                                           "sefaria.model.linker": types.ModuleType("sefaria.model.linker"),
                                           "sefaria.model.linker.ref_part": ref_part}), \
                mock.patch.object(link_books, "named_chapters", lambda _text, _ref: chapters):
            return link_books.lead_names_other_works(linker, raw, len(lead), ref)

    def test_a_lead_without_quotation_evidence_is_not_discarded(self):
        for lead in ([("NAMED", "בְּנִדָּה")], [("NAMED", "<b>בנדה</b>")],
                     [("NAMED", "שנאמר")], [("NAMED", "שנאמר"), ("NAMED", "בנדה")],
                     [("NAMED", 'והשטמ"ק')], [("NAMED", "ובתורת גיטין")]):
            with self.subTest(lead=lead):
                self.assertFalse(self.outside(lead))

    def test_a_qualifier_or_a_related_work_keeps_the_drop(self):
        for lead in ("ירושלמי", "רש\"י"):
            with self.subTest(lead=lead):
                self.assertFalse(self.outside([("NAMED", lead)]))
        self.assertFalse(self.outside([("NAMED", "ובתוספתא")], _ref("Niddah 31a", ["Talmud", "Bavli", "Seder Tahorot"])))
        self.assertFalse(self.outside([("NAMED", "שנאמר"), ("NAMED", "ירושלמי")]))

    def test_a_work_of_the_same_category_needs_a_placing_chapter(self):
        self.assertFalse(self.outside([("NAMED", "ויקרא")], self.GENESIS))
        self.assertFalse(self.outside([("NAMED", "בספר הישר")], self.SANHEDRIN))
        chapter = types.SimpleNamespace(overlaps=lambda _ref: True)
        self.assertTrue(self.outside([("NAMED", "בספר הישר"), ("NUMBERED", "בפרק זה בורר")],
                                     self.SANHEDRIN, chapters=(chapter,)))
        elsewhere = types.SimpleNamespace(overlaps=lambda _ref: False)
        self.assertFalse(self.outside([("NAMED", "בספר הישר"), ("NUMBERED", "בפרק זה בורר")],
                                      self.SANHEDRIN, chapters=(elsewhere,)))
        self.assertFalse(self.outside([("NUMBERED", "בפרק זה בורר")], self.SANHEDRIN))

    def test_only_a_verse_or_a_bavli_daf_is_read_alone(self):
        self.assertFalse(self.outside([("NAMED", "שנאמר")], _ref("Shulchan Arukh, Orach Chayim 128", ["Halakhah"])))
        self.assertFalse(self.outside([("NAMED", "בטור")], _ref("Shulchan Arukh, Orach Chayim 128", ["Halakhah"])))
        self.assertFalse(self.outside([("NAMED", "שנאמר")], _ref("Mishnah Niddah 2:1", ["Mishnah"])))

    def test_a_number_ibid_or_relative_part_is_not_dropped(self):
        for kind, text in (("NUMBERED", "טו"), ("IBID", "שם"), ("DH", "ד\"ה")):
            with self.subTest(kind=kind):
                self.assertFalse(self.outside([(kind, text)]))

    def test_a_named_chapter_phrase_places_or_only_locates(self):
        chapter = types.SimpleNamespace(overlaps=lambda _ref: True)
        self.assertTrue(self.outside([("NAMED", "בספר הישר"), ("NAMED", "בפרק זה בורר")],
                                     self.SANHEDRIN, chapters=(chapter,)))
        self.assertFalse(self.outside([("NAMED", "בספר הישר"), ("NAMED", "בפרק זה בורר")], self.SANHEDRIN))
        self.assertFalse(self.outside([("RELATIVE", "לקמן"), ("NAMED", "בנדה")]))


class AbsorbedLeadTest(unittest.TestCase):
    """"בנדה (ויקרא טו יט)" links its parenthesized verse (Otzaria/otzaria#3453)."""

    def records(self, content, lead, lead_outside):
        start = content.index(lead)
        end = content.index("יט") + 2
        raw = _raw(content, start, end, [("NAMED", lead), ("NAMED", "ויקרא"), ("NUMBERED", "טו"), ("NUMBERED", "יט")])
        raw.raw_ref_parts = [_RealignedPart(p.type.name, *p.span.range, p.text) for p in raw.raw_ref_parts]
        first = raw.raw_ref_parts[1].span.range[0]
        raw.subspan = lambda _parts: _Span(start + first, end)
        raw.lang = "he"
        rr = types.SimpleNamespace(resolution_failed=True, raw_entity=raw)
        resolved = types.SimpleNamespace(is_ambiguous=False, ref=_ref("Leviticus 15:19", ["Tanakh", "Torah"]),
                                         context_type=None)
        linker = types.SimpleNamespace(_ref_resolver=types.SimpleNamespace(resolve_raw_ref=lambda *_: resolved))
        ref_part = types.ModuleType("sefaria.model.linker.ref_part")
        ref_part.RawRef = lambda *args: args
        with mock.patch.dict(sys.modules, {"sefaria": types.ModuleType("sefaria"),
                                           "sefaria.model": types.ModuleType("sefaria.model"),
                                           "sefaria.model.linker": types.ModuleType("sefaria.model.linker"),
                                           "sefaria.model.linker.ref_part": ref_part}):
            return _batch(content, rr, linker, chapter_led_citation=lambda *_: None,
                          lead_names_other_works=lambda *_: lead_outside)

    def test_the_parenthesized_verse_is_linked_alone(self):
        content = "וכן בְּנִדָּה <small>(ויקרא טו יט)</small> אסורה"
        self.assertEqual(self.records(content, "בְּנִדָּה", True), [("Leviticus 15:19", "ויקרא טו יט")])

    def test_a_lead_that_may_qualify_the_title_keeps_the_drop(self):
        content = "ועיין רש\"י (ויקרא טו יט) שם"
        self.assertEqual(self.records(content, "רש\"י", False), [])


class BookChaptersTest(unittest.TestCase):
    def test_a_grouping_node_without_a_range_is_skipped(self):
        nodes = [{"wholeRef": "Sippurei Maasiyot 1", "match_templates": [{"term_slugs": ["first"]}]},
                 {"titles": [{"text": "נוספים"}], "nodes": []}]
        index = types.SimpleNamespace(alt_structs={"Chapters": {"nodes": nodes}})
        model = types.ModuleType("sefaria.model")
        model.Ref = lambda text: text
        model.library = types.SimpleNamespace(get_index=lambda _title: index)
        schema = types.ModuleType("sefaria.model.schema")
        schema.NonUniqueTerm = types.SimpleNamespace(init=lambda _slug: types.SimpleNamespace(
            get_titles=lambda _lang: ["ראשון"]))
        link_books._book_chapters.cache_clear()
        try:
            with mock.patch.dict(sys.modules, {"sefaria": types.ModuleType("sefaria"), "sefaria.model": model,
                                               "sefaria.model.schema": schema}):
                self.assertEqual(link_books._book_chapters("Sippurei Maasiyot"),
                                 (("Sippurei Maasiyot 1", frozenset({"ראשון"})),))
        finally:
            link_books._book_chapters.cache_clear()


if __name__ == "__main__":
    unittest.main()
