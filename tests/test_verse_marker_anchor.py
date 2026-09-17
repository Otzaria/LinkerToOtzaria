import os
import sys
import types
import unittest

# link_books imports the Unix-only `resource` module at import time; the CI
# runner is Linux, and on Windows a stub keeps this pure-logic test runnable.
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


class _RawEntity:
    def __init__(self, start, end):
        self.span = _Span(start, end)
        self.raw_ref_parts = ()


class _Ref:
    def __init__(self, normal):
        self._normal = normal

    def normal(self):
        return self._normal


class _ResolvedRef:
    is_ambiguous = False
    context_type = None

    def __init__(self, start, end, normal):
        self.raw_entity = _RawEntity(start, end)
        self.ref = _Ref(normal)


class _Doc:
    def __init__(self, resolved_refs):
        self.resolved_refs = resolved_refs


class _Linker:
    def __init__(self, docs):
        self._docs = docs

    def bulk_link(self, texts, book_context_refs=None, type_filter=None):
        return self._docs


class VerseMarkerAnchorTest(unittest.TestCase):
    """otzaria#1185 — numeric line markers must not become citations."""

    def _records(self, content, refs, source="MoreBooks"):
        return link_books.process_batch(
            _Linker([_Doc(refs)]),
            link_books.BookKey(source, "ספר"),
            [(0, content, None)],
            lambda _line: None,
        )[0]

    def test_span_inside_leading_verse_marker_is_dropped(self):
        content = "(נח) וַיִּקְרְאוּ לְרִבְקָה וַיֹּאמְרוּ אֵלֶיהָ"
        marker = _ResolvedRef(1, 3, "Genesis 6:9")
        real = _ResolvedRef(5, 15, "Genesis 24:58")
        records = self._records(content, [marker, real], source="Tanach")
        self.assertEqual([r.target_ref for r in records], ["Genesis 24:58"])

    def test_numeric_marker_is_dropped_outside_tanach_too(self):
        content = "(צו) סעיף הנסמן בבאר הגולה"
        records = self._records(content, [_ResolvedRef(1, 3, "Leviticus 6:1")])
        self.assertEqual(records, [])

    def test_only_canonical_hebrew_numerals_are_markers(self):
        marker_end = link_books.leading_hebrew_numeral_marker_end
        self.assertGreater(marker_end('(קכח) טקסט'), 0)
        self.assertGreater(marker_end('(ט״ו) טקסט'), 0)
        self.assertGreater(marker_end('(ט"ז) טקסט'), 0)
        self.assertEqual(marker_end('(ית) ציטוט'), 0)  # ascending, not a numeral
        self.assertEqual(marker_end('(מן) ציטוט'), 0)  # final letters are not numerals

    def test_short_real_citations_at_line_start_are_preserved(self):
        cases = (
            ("(שם) כמבואר לעיל", "Genesis 1:1"),
            ("(ב״י) עיין בבית יוסף", "Beit Yosef, Orach Chaim 1"),
            ("(יתרו) נאמר בפרשה", "Exodus 18:1"),
        )
        for content, target in cases:
            with self.subTest(content=content):
                close = content.index(")")
                records = self._records(content, [_ResolvedRef(1, close, target)])
                self.assertEqual([record.target_ref for record in records], [target])

    def test_marker_only_matters_at_line_start(self):
        content = "ראה (נח) וגם נח איש צדיק"
        inner = _ResolvedRef(4, 6, "Genesis 6:9")
        records = self._records(content, [inner])
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
