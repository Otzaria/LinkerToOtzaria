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
    """otzaria#1185 — the verse marker "(נח)" that opens a Tanach line was linked
    as a citation of Parashat Noach. A span inside the leading marker is not a
    citation and must not become a record."""

    def test_span_inside_leading_verse_marker_is_dropped(self):
        content = "(נח) וַיִּקְרְאוּ לְרִבְקָה וַיֹּאמְרוּ אֵלֶיהָ"
        marker = _ResolvedRef(1, 3, "Genesis 6:9")
        real = _ResolvedRef(5, 15, "Genesis 24:58")
        records, _words = link_books.process_batch(
            _Linker([_Doc([marker, real])]),
            link_books.BookKey("Tanach", "בראשית"),
            [(674, content, None)],
            lambda _line: None,
        )
        self.assertEqual([r.target_ref for r in records], ["Genesis 24:58"])

    def test_marker_only_matters_at_line_start(self):
        content = "ראה (נח) וגם נח איש צדיק"
        inner = _ResolvedRef(4, 6, "Genesis 6:9")
        records, _words = link_books.process_batch(
            _Linker([_Doc([inner])]),
            link_books.BookKey("MoreBooks", "ספר"),
            [(0, content, None)],
            lambda _line: None,
        )
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
