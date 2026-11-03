"""Local ownership, preservation and independent QA counterexamples."""
import unittest
import test_chapter_daf_recovery as helpers
from test_chapter_daf_recovery import Ref, lb, citation, NS


class LocalAttributionTest(unittest.TestCase):
    setUp = helpers.ChapterDafRecoveryTest.setUp
    records = helpers.ChapterDafRecoveryTest.records

    def test_distant_self_statement_does_not_change_an_independent_named_gemara(self):
        anchor = 'במסכת סוכה בפרק קמא (יג, ב'
        for predicate in ('כדאיתא', 'דגרסינן', 'כדאשכחן', 'אמרו', 'מפורש', 'קיי"ל', 'כדתנן'):
            before = 'וכמו שכתבתי למעלה בדעת רש"י ויש לחלק בענין אחר ' + predicate + ' '
            rows = self.records(before + anchor + ')', anchor,
                [('NAMED', 'במסכת סוכה בפרק קמא'), ('NUMBERED', 'יג, ב')], refs=['Sukkah 13b'])
            self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b'], predicate)

    def test_proof_and_saying_predicates_do_not_invent_an_own_work(self):
        anchor = 'במסכת סוכה בפרק קמא (יג, ב'
        for predicate in ('כמו שאמרנו', 'כמו שהבאתי', 'כמו שהוכחתי'):
            rows = self.records(predicate + ' ' + anchor + ')', anchor,
                [('NAMED', 'במסכת סוכה בפרק קמא'), ('NUMBERED', 'יג, ב')], refs=['Sukkah 13b'])
            self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b'])
        anchor = 'לעיל (דף ב.'
        rows = self.records('כדמוכח כמו שהבאתי מפרק קמא ' + anchor + ')', anchor,
            [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'],
            source='Tosafot on Shevuot 19a:1:1')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_postfix_subordinate_proof_does_not_name_the_authors_commentary(self):
        anchor = 'בפ"ק (דף ב.'
        for tail in (' שהוכחתי ממנה דין זה', ' שהבאתי ממנה ראיה', ' כמו שכתבתי למעלה', ' אמרו כך'):
            text = anchor + ')' + tail
            self.assertIsNone(lb.citation_local_attribution(text, 0, len(anchor)), tail)
            rows = self.records(text, anchor, [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], tail)

    def test_postfix_retrospective_writing_and_named_rashi_do_qualify_the_address(self):
        anchor = 'בפ"ק (דף ב.'
        for tail in (' הארכתי שם', ' פירשתי שם', ' שם ביארתי'):
            rows = self.records(anchor + ')' + tail, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])
        for tail in (' פי\' רש"י', ' פרש"י', ' פירש בקונטרס',
                     ' פי\' רש"י לא יצא', ' פרש"י לא יצא', ' כתב רש"י'):
            rows = self.records(anchor + ')' + tail, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual(rows, [])

    def test_own_work_proof_replaces_a_context_only_or_base_candidate(self):
        anchor = 'בפ"ק (דף ב.'
        for target in ('Shevuot 2a', 'Tosafot on Shevuot 2a'):
            for context in (None, 'CURRENT_BOOK'):
                rows = self.records("כדפי' " + anchor + ')', anchor,
                    [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=[target], context_type=context)
                self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_coordinated_and_negative_postfix_writers_do_not_own_a_native_citation(self):
        anchor = 'בפ"ק (דף ב.'
        for tail in (' ורש"י פירש בשמעתין', ' ור"ת מפרש בענין אחר',
                     " ובקונט' הביאה לעיל", ' ולר"ת אין נראה לפרש',
                     ' ובקונטרס לא פירש כן', ' בקונטרס לא פירש כן', ' כדפירש בקונטרס'):
            text = 'כדתנן ' + anchor + ')' + tail
            rows = self.records(text, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], tail)
        rows = self.records('כתבתי ' + anchor + ') ורש"י מפרש אחרת', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_communal_document_writing_is_not_the_authors_self_citation(self):
        anchor = 'בפ"ק (דף ב.'
        rows = self.records('לא כפריה כתבינן וכן ' + anchor + ')', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])
        self.assertFalse(lb.is_self_writing('כתבינן'))
        self.assertTrue(lb.is_self_writing('כדכתבינן'))

    def test_completed_retrospective_proof_does_not_own_a_new_coordinated_citation(self):
        anchor = 'בפ"ק (דף ב.'
        rows = self.records('שהבאתי לעיל וכן ' + anchor + ')', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_printed_foreign_discussion_blocks_only_implicit_source_chapter_guess(self):
        anchor = 'בפ"ק (דף ב.'
        for prefix in ('בגיטין (דף ב.) וכן ', 'בגיטין (דף ב.) ' + 'דיון ' * 250 + 'כדתנן התם '):
            rows = self.records(prefix + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')])
            self.assertEqual(rows, [])
        for prefix in ('ביצה היא מאכל וכן ', 'בגיטין (דף ב.) ובשבועות (דף ב.) וכן '):
            rows = self.records(prefix + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_editorial_self_writing_and_precise_chapter_location_retain_ownership(self):
        for prefix, anchor in (('(כדאמרינן) [כדכתבינן] ', 'בפ"ק (דף ב.'),
                               ('פירשתי בפ"ק סמוך לתחילתו ', 'לעיל (דף ב.')):
            text = prefix + anchor + ')'
            self.assertEqual(lb.citation_local_attribution(text, len(prefix), len(prefix) + len(anchor)), 'self')
        anchor = 'בפ"ק (דף ב.'
        rows = self.records(anchor + ') הבאתי שם ראיה', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])
        rows = self.records(anchor + ' ושם ד"ה אמר) הבאתי שם ראיה', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_unknown_writing_location_or_own_list_cannot_mint_base_gemara(self):
        anchor = 'בפ"ק (דף ב.'
        for prefix in ('פירשתי במקום בלתי מוכר ', 'פירשתי בתוספות גיטין (דף ב.) וכן '):
            rows = self.records(prefix + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')])
            self.assertEqual(rows, [])

    def test_native_conclusion_ends_prior_self_scope_across_its_printed_location(self):
        anchor = 'ובמסכת סוכה בפרק קמא (יג, ב'
        text = 'וטעמא פירשתי בריש פירקין (דף ב.) ' + anchor + ') מסיק דהא'
        rows = self.records(text, anchor,
            [('NAMED', 'ובמסכת סוכה בפרק קמא'), ('NUMBERED', 'יג, ב')], refs=['Sukkah 13b'])
        self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b'])
        prefix = 'וטעמא פירשתי בריש פירקין (דף ב.) '
        anchor = 'ובפרק קמא ('
        text = prefix + anchor + 'סוכה דף יג:) מסיק דהא'
        self.assertIsNone(lb.citation_local_attribution(text, len(prefix), len(prefix) + len(anchor)))

    def test_a_quoted_unique_chapter_independently_preserves_an_existing_ibid_amud(self):
        anchor = 'שם דף לה.'
        text = 'דגריס בשילהי. שבועת העדות (' + anchor + ')'
        rr = citation(text, anchor, [('IBID', 'שם'), ('NUMBERED', 'דף לה.')], refs=[Ref('Shevuot 35a')])
        self.assertTrue(lb.chapter_ibid_is_proven(rr, Ref('Shevuot 35a'), text,
                                                *rr.raw_entity.span.range, Ref))
        for prefix, body, target in (('דגריס בפרק קמא (', anchor, 'Shevuot 35a'),
                                     ('דגריס שבועת העדות (', 'שם', 'Shevuot 35a'),
                                     ('פירש שבועת העדות (', anchor, 'Shevuot 35a'),
                                     ('דגריס שבועת העדות (', anchor, 'Shevuot 26a')):
            text = prefix + body + ')'
            rr = citation(text, body, [('IBID', 'שם')], refs=[Ref(target)])
            self.assertFalse(lb.chapter_ibid_is_proven(rr, Ref(target), text,
                                                     *rr.raw_entity.span.range, Ref))

    def test_quoted_amud_and_whole_daf_self_addresses_keep_their_printed_scope(self):
        for anchor, target, expected in (
            ("בפרק קמא (דף ב א'", 'Shevuot 2a', 'Tosafot on Shevuot 2a'),
            ('בפרק קמא (דף ב', 'Shevuot 2', 'Tosafot on Shevuot 2'),
        ):
            rows = self.records('כתבתי ' + anchor + ')', anchor,
                [('NAMED', 'בפרק קמא'), ('NUMBERED', anchor.split('(')[1])], refs=[target])
            self.assertEqual([r.target_ref for r in rows], [expected])

    def test_invalid_whole_page_does_not_fail_book_or_hide_transport_errors(self):
        anchor = 'בפרק קמא (דף ר'
        text = 'כתבתי ' + anchor + ')'
        rr = citation(text, anchor, [('NAMED', 'בפרק קמא'), ('NUMBERED', 'דף ר')], refs=[Ref('Shevuot 2a')])
        def factory(value):
            if value == 'Shevuot 200':
                raise ValueError('invalid daf')
            return Ref(value)
        self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[rr])]
        rows, _ = lb.process_batch(self.linker, lb.BookKey('Sefaria', 'ספר'),
            [(7, text, 'Tosafot on Shevuot 3a:1:1')], self.fail, context_ref_factory=factory)
        self.assertEqual(rows, [])
        def unavailable(value):
            if value == 'Shevuot 200':
                raise RuntimeError('transport failed')
            return Ref(value)
        with self.assertRaisesRegex(RuntimeError, 'transport failed'):
            lb.own_commentary_self_ref(rr, Ref('Shevuot 2a'), text, *rr.raw_entity.span.range,
                Ref('Tosafot on Shevuot 3a:1:1'), unavailable)

    def test_foreign_offered_and_printed_work_cannot_become_the_source_tractate(self):
        anchor = 'לעיל (דף ב.'
        for refs in (['Gittin 2a'], []):
            rows = self.records('כתבתי בגיטין ' + anchor + ')', anchor,
                [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=refs,
                source='Tosafot on Shevuot 19a:1:1', context_type='CURRENT_BOOK')
            self.assertEqual(rows, [])

    def test_first_person_inside_an_attributed_author_quote_is_not_ours(self):
        anchor = 'לעיל (דף ב.'
        for before in ('כתב הרמב"ן כדפרישנא ', 'וכתב הרא"ש כדפרישית ', 'פירש רבנו משה כדפי\' '):
            rows = self.records(before + anchor + ')', anchor,
                [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'],
                source='Tosafot on Shevuot 19a:1:1', context_type='CURRENT_BOOK')
            self.assertEqual(rows, [], before)

    def test_gemara_self_reference_remains_in_its_own_base_work(self):
        anchor = 'לעיל (דף ב.'
        rows = self.records('כדפרישנא ' + anchor + ')', anchor,
            [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'],
            source='Shevuot 19a', context_type='CURRENT_BOOK')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_successful_original_base_and_own_refs_keep_ibid_history_across_checkpoint(self):
        for source, first_ref, next_ref, competing in (
            ('Shevuot 19a', 'Shevuot 2a:3', 'Shevuot 2a:4', 'Shevuot 3a:4'),
            ('Rashi on Shevuot 19a:1', 'Rashi on Shevuot 2a:3',
             'Rashi on Shevuot 2a:4', 'Rashi on Shevuot 3a:4'),
        ):
            with self.subTest(source=source):
                anchor = 'לעיל בסמוך (דף ב.'
                text = 'כדפרישנא ' + anchor + ')'
                first = citation(text, anchor, [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')],
                                 refs=[Ref(first_ref)])
                first.context_type = NS(name='CURRENT_BOOK')
                following = citation('שם', 'שם', [('IBID', 'שם')], refs=[Ref(next_ref), Ref(competing)])
                self.linker.bulk_link = lambda texts, **_k: [NS(resolved_refs=[first if t == text else following]) for t in texts]
                book = lb.BookKey('Sefaria', source)
                batch = [(7, text, source), (8, 'שם', source)]
                def history():
                    result = lb.IbidContext()
                    result.resolve = lambda _linker, invoke: invoke()
                    return result
                uninterrupted = history()
                whole, _ = lb.process_batch(self.linker, book, batch, self.fail,
                    context_ref_factory=Ref, ibid_context=uninterrupted)
                self.assertEqual([r.target_ref for r in whole], [first_ref, next_ref])
                split = history()
                left, _ = lb.process_batch(self.linker, book, batch[:1], self.fail,
                    context_ref_factory=Ref, ibid_context=split)
                state = split.state(book, 0, batch[:1], 'a' * 64)
                resumed = history()
                resumed.restore_state(state, Ref, book, 0, batch[:1], 'a' * 64)
                right, _ = lb.process_batch(self.linker, book, batch[1:], self.fail,
                    context_ref_factory=Ref, ibid_context=resumed)
                self.assertEqual([r.to_dict() for r in left + right], [r.to_dict() for r in whole])
                self.assertEqual(resumed.last_emitted_ref.normal(), next_ref)

    def test_self_reference_cannot_convert_a_wrong_relative_direction_to_absolute(self):
        for anchor, source, target in (('לקמן (דף ב.', 'Tosafot on Shevuot 19a:1:1', 'Shevuot 2a'),
                                        ('לעיל (דף כו.', 'Tosafot on Shevuot 3a:1:1', 'Shevuot 26a')):
            word = anchor.split()[0]
            rows = self.records('כתבתי ' + anchor + ')', anchor,
                [('RELATIVE', word), ('NUMBERED', anchor[anchor.index('דף'):])],
                refs=[target], source=source, context_type='CURRENT_BOOK')
            self.assertEqual(rows, [])

    def test_self_reference_cannot_bypass_a_contradictory_printed_chapter(self):
        anchor = 'בפ"ב (לעיל דף ב.'
        rows = self.records('כתבתי ' + anchor + ')', anchor,
            [('NUMBERED', 'בפ"ב'), ('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')],
            refs=['Shevuot 2a'], source='Tosafot on Shevuot 19a:1:1', context_type='CURRENT_BOOK')
        self.assertEqual(rows, [])

    def test_unsupported_ranges_and_lists_cannot_collapse_to_the_last_daf(self):
        for body in ('דף ב. - דף ג:', 'דף ב. דף ג:'):
            anchor = 'בפרק קמא (' + body
            rows = self.records('כתבתי ' + anchor + ')', anchor,
                [('NAMED', 'בפרק קמא'), ('NUMBERED', body)], refs=['Shevuot 3b'])
            self.assertEqual(rows, [])

    def test_hidden_html_attributes_cannot_supply_self_or_address_evidence(self):
        anchor = 'בפרק קמא (דף ב.'
        prefix = '<span title=" > כתבתי שם > ">דיון</span> '
        text = prefix + anchor + ')'
        self.assertIsNone(lb.citation_local_attribution(text, len(prefix), len(prefix) + len(anchor)))
        rr = citation(text, anchor, [('NAMED', 'בפרק קמא'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a')])
        self.assertIsNone(lb.own_commentary_self_ref(rr, Ref('Shevuot 2a'),
            'כתבתי <span title=" > דף ב. > ">כאן</span>', 7, 44, Ref('Tosafot on Shevuot 3a:1:1'), Ref))


if __name__ == '__main__':
    unittest.main()
