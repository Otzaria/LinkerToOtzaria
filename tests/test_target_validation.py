"""Published v35 regressions: invalid legacy refs must never become emitted targets."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
import link_books as lb
from test_recovery_evidence import citation

NS = types.SimpleNamespace


class Ref:
    def __init__(self, normal, empty=False, endpoints=None):
        self.value, self.empty, self.endpoints = normal, empty, endpoints
        self.index = NS(title=normal.rsplit(' ', 1)[0])

    def normal(self):
        return self.value

    def is_empty(self):
        return self.empty

    def is_range(self):
        return self.endpoints is not None

    def starting_ref(self):
        return self.endpoints[0]

    def ending_ref(self):
        return self.endpoints[1]


class CommentaryRef(Ref):
    def __init__(self, normal):
        import re
        title, chapter, note = re.fullmatch(r'(.+) (\d+):(\d+)', normal).groups()
        super().__init__(normal)
        self.index = NS(title=title)
        self.sections = [int(chapter), int(note)]
        self.toSections = self.sections


class TargetValidationTest(unittest.TestCase):
    def setUp(self):
        lb._TARGET_TEXT_CACHE.clear()

    def run_rows(self, rows, docs, batch_size=100):
        resolver = NS(_ibid_history=NS(last_refs=[]), reset_ibid_history=lambda: None)
        linker = NS(_ref_resolver=resolver, bulk_link=lambda texts, **_: [docs[t] for t in texts])
        with mock.patch.object(lb, 'BATCH_LINES', batch_size):
            records, _ = lb.process_book(linker, lb.BookKey('Sefaria', 'בדיקה'), rows,
                                         lambda _: None, lambda: None)
        return [r.to_dict() for r in records]

    def test_out_of_bounds_range_with_nonempty_parent_is_not_available(self):
        start = Ref('Proverbs 27:31', empty=True)
        end = Ref('Proverbs 27:32', empty=True)
        self.assertFalse(lb.target_has_text(Ref('Proverbs 27:31-32', endpoints=(start, end))))
        start = Ref('Proverbs 27:1')
        self.assertFalse(lb.target_has_text(Ref('Proverbs 27:1-32', endpoints=(start, end))))
        self.assertTrue(lb.target_has_text(Ref('Proverbs 27:1-27', endpoints=(start, Ref('Proverbs 27:27')))))

    def test_schema_backed_targets_must_round_trip_their_exported_key(self):
        class SchemaRef(Ref):
            index_node = object()

            def __init__(self, normal):
                super().__init__('Canonical 1:1' if normal == 'alias' else normal)
        ref = SchemaRef('Canonical 1:1')
        self.assertTrue(lb.target_has_text(ref))
        ref = SchemaRef('Canonical 1:1')
        ref.value = 'alias'  # resolver-built object with a noncanonical exported key
        self.assertFalse(lb.target_has_text(ref))

    def test_every_legacy_target_is_validated_before_emission(self):
        for target in ('Shulchan Arukh, Yoreh De\'ah 216:119', 'Mishnah Bava Batra 5:75',
                       'Mishneh Torah, Divorce 4:31'):
            text = 'ועיין (כתובת שגויה)'
            rr = citation(text, 'כתובת שגויה', [('NAMED', 'כתובת')], [Ref(target, empty=True)])
            self.assertEqual(self.run_rows([(0, text, None)], {text: NS(resolved_refs=[rr])}), [])

    def test_commentary_address_cannot_target_base_code_even_when_segment_exists(self):
        for anchor in ('יו"ד סימן רכא סק"ה', 'חו"מ ס"ק יב', 'ובסק״א', "ובס׳ק יב", 'בסעיף קטן א'):
            self.assertTrue(lb.base_code_has_commentary_address(Ref("Shulchan Arukh, Yoreh De'ah 221:5"), anchor), anchor)
            self.assertFalse(lb.base_code_has_commentary_address(Ref("Siftei Kohen on Shulchan Arukh, Yoreh De'ah 221:5"), anchor))
        self.assertFalse(lb.base_code_has_commentary_address(Ref("Shulchan Arukh, Yoreh De'ah 221:5"), 'יו"ד סימן רכא סעיף ה'))

    def test_explicit_commentary_uses_only_the_printed_author_and_numbers(self):
        for author, code, expected in [
            ('וכן כתב הש"ך ז"ל', 'יו"ד סימן רט"ז סקי"ט', "Siftei Kohen on Shulchan Arukh, Yoreh De'ah 216:19"),
            ("וכתב הט\"ז ז\"ל בסי' הנ\"ל", 'יו"ד סימן רכ"א סקמ"ד', "Turei Zahav on Shulchan Arukh, Yoreh De'ah 221:44"),
            ('קושית הט"ז', 'חו"מ ר"ג סק"א', 'Turei Zahav on Shulchan Arukh, Choshen Mishpat 203:1'),
            ('ועיין ש"ך ז"ל שם', 'חו"מ סימן ש"נ סק"א', 'Siftei Kohen on Shulchan Arukh, Choshen Mishpat 350:1'),
        ]:
            text = f'{author} ({code}) גבי ענין זה'
            part = "Yoreh De'ah" if 'יו"ד' in code else 'Choshen Mishpat'
            # The resolver's number is deliberately unrelated. It supplies no number evidence.
            base = Ref(f'Shulchan Arukh, {part} 1:999')
            found = lb.explicit_code_commentary_ref(base, text, text.index(code), text.index(')'), CommentaryRef)
            self.assertIsNotNone(found, text)
            self.assertEqual(found.normal(), expected)

    def test_commentary_recovery_rejects_ambiguous_or_incomplete_evidence(self):
        base = Ref("Shulchan Arukh, Yoreh De'ah 216:119")
        for text in ('ועיין (יו"ד סימן רט"ז סקי"ט)',
                     'הבית יוסף והש"ך ז"ל (יו"ד סימן רט"ז סקי"ט)',
                     'ועיין הט"ז והש"ך (יו"ד סימן רט"ז סקי"ט)',
                     'ועיין הש"ך (יו"ד שם סקי"ט)',
                     'ועיין הש"ך (יו"ד סימן רט"ז סעיף יט)',
                     'ועיין הש"ך (חו"מ סימן רט"ז סקי"ט)',
                     'ועיין הש"ך (יו"ד סימן רט"ז סקי"ט-כ)',
                     "<span title='> הש\"ך'>אחר</span> (יו\"ד סימן רט\"ז סקי\"ט)"):
            self.assertIsNone(lb.explicit_code_commentary_ref(
                base, text, text.index('(') + 1, text.index(')'), CommentaryRef), text)

    def test_prose_abbreviations_require_explicit_number_evidence(self):
        ref = Ref('Mishneh Torah, Vows 4:20')
        for text in ('והרמב"ם כ\' פ"ד מהלכות נדרים', 'בפ"ד מהלכות גירושין לא', 'וברמב"ם פ"ז מה"נ'):
            word = "כ'" if "כ'" in text else 'מה"נ' if 'מה"נ' in text else 'לא'
            rr = citation(text, text, [('NUMBERED', word)], [ref])
            self.assertTrue(lb.has_unproven_code_number(rr, ref))
        for number in ("הלכה כ'", 'הלכה לא', 'הל"א', 'ה"כ'):
            rr = citation(number, number, [('NUMBERED', number)], [ref])
            self.assertFalse(lb.has_unproven_code_number(rr, ref))

    def test_rejected_target_cannot_seed_a_later_unambiguous_ibid(self):
        for batch in (1, 100):
            first, second = 'ועיין (יו"ד רטז סקי"ט)', 'ועיין שם סעיף א'
            bad = citation(first, 'יו"ד רטז סקי"ט', [('NAMED', 'יו"ד')],
                           [Ref("Shulchan Arukh, Yoreh De'ah 216:119", empty=True)])
            # Sefaria remembered the failed work and offers an existing, wrong base segment.
            ibid = citation(second, 'שם סעיף א', [('IBID', 'שם'), ('NUMBERED', 'סעיף א')],
                            [Ref("Shulchan Arukh, Yoreh De'ah 216:1")])
            docs = {first: NS(resolved_refs=[bad]), second: NS(resolved_refs=[ibid])}
            self.assertEqual(self.run_rows([(0, first, None), (1, second, None)], docs, batch), [])

    def test_accepted_target_still_seeds_ibid(self):
        first, second = 'ועיין בראשית א א', 'ושם ב'
        good = citation(first, 'בראשית א א', [('NAMED', 'בראשית')], [Ref('Genesis 1:1')])
        ibid = citation(second, 'שם ב', [('IBID', 'שם')], [Ref('Genesis 1:2')])
        docs = {first: NS(resolved_refs=[good]), second: NS(resolved_refs=[ibid])}
        for batch in (1, 100):
            self.assertEqual([r['target_ref'] for r in self.run_rows(
                [(0, first, None), (1, second, None)], docs, batch)], ['Genesis 1:1', 'Genesis 1:2'])

    def test_only_an_independently_named_chapter_may_trim_empty_boundaries(self):
        normal = 'Tosafot on Sukkah 29b:4-42b:4'
        scope = Ref(normal, endpoints=(Ref('Tosafot on Sukkah 29b:4'), Ref('Tosafot on Sukkah 42b:4', empty=True)))
        scope.index = NS(title='Tosafot on Sukkah', categories=['Talmud', 'Bavli'], base_text_titles=['Sukkah'])
        scope.is_segment_level = lambda: False
        bounded = Ref('Tosafot on Sukkah 29b:4:1-42b:3:1')
        with mock.patch.object(lb, '_book_chapters', return_value=((scope, {'לולב הגזול'}),)), \
                mock.patch.object(lb, '_chapter_content_bounds', return_value=bounded.normal()) as bounds:
            anchor = 'תוספות ר"פ לולב הגזול'
            rr = citation(anchor, anchor, [('NAMED', anchor)], [scope])
            self.assertIs(lb.named_chapter_content_ref(scope, rr, anchor, lambda _: bounded), bounded)
            for bad in ('תוספות דף כט ע"ב', 'לולב הגזול', 'תוספות בפרק אחר'):
                wrong = citation(bad, bad, [('NAMED', bad)], [scope])
                self.assertIsNone(lb.named_chapter_content_ref(scope, wrong, bad, lambda _: bounded), bad)
            # An explicit numeric endpoint may not be silently replaced, even near a chapter name.
            explicit = anchor + ' דף מב'
            wrong = citation(explicit, explicit, [('NAMED', anchor), ('NUMBERED', 'דף מב')], [scope])
            self.assertIsNone(lb.named_chapter_content_ref(scope, wrong, explicit, lambda _: bounded))
            self.assertEqual(bounds.call_count, 1)

    def test_prefixed_ibids_keep_the_whole_book_history(self):
        for word in ('שם', 'ושם', 'משם', 'ומשם', 'בשם', 'כשם', 'לשם', 'ששם'):
            self.assertTrue(lb.requires_book_context([(0, f'ועיין {word} סעיף א', '')]), word)

    def test_availability_query_failure_fails_the_book(self):
        text = 'ועיין בראשית א א'
        ref = Ref('Genesis 1:1')
        ref.is_empty = mock.Mock(side_effect=OSError('database unavailable'))
        rr = citation(text, 'בראשית א א', [('NAMED', 'בראשית')], [ref])
        with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            self.run_rows([(0, text, None)], {text: NS(resolved_refs=[rr])})


if __name__ == '__main__':
    unittest.main()
