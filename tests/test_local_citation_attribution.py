"""Local ownership, preservation and independent QA counterexamples."""
import unittest
import test_chapter_daf_recovery as helpers
from test_chapter_daf_recovery import Ref, lb, citation, NS, Chapter
from unittest import mock


class LocalAttributionTest(unittest.TestCase):
    setUp = helpers.ChapterDafRecoveryTest.setUp
    records = helpers.ChapterDafRecoveryTest.records

    def test_reconstructed_relatives_receive_the_same_proven_display_owner(self):
        for predicate, owner in (
                ('הארכתי בו לעיל בריש', 'Tosafot'),
                ('כדפרישית בסוף', 'Tosafot'),
                ('כמו שאבאר קצת לקמן בריש', 'Tosafot'),
                ('כדפי\' בקונטרס בריש', 'Rashi'),
                ('ובקונטרס פי\' לקמן בריש', 'Rashi')):
            below = 'לקמן' in predicate
            source = 'Tosafot on Shevuot 2a:1:1' if below else 'Tosafot on Shevuot 19a:1:1'
            anchor = ('לקמן' if below else 'לעיל') + ' בפ"ק (דף ג.'
            text = predicate + ' ' + anchor + ')'
            rows = self.records(text, anchor, [('RELATIVE', anchor.split()[0]),
                ('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ג.')], source=source)
            self.assertEqual([r.target_ref for r in rows], [owner + ' on Shevuot 3a'], predicate)

    def test_named_prefix_and_postfix_share_work_availability_and_address_guards(self):
        anchor = 'בפ"ק (דף ב.'
        for before in ('כדפי\' בקונטרס ', 'רש"י פירש ', 'ברש"י ', 'בקונטרס גופיה פירש ', 'כדכתב בתוספות '):
            target = ('Tosafot' if 'תוספות' in before else 'Rashi') + ' on Shevuot 2a'
            rows = self.records(before + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a:3'])
            self.assertEqual([r.target_ref for r in rows], [target], before)
        lb._TARGET_TEXT_CACHE.clear()
        original_empty = Ref.is_empty
        with mock.patch.object(Ref, 'is_empty', lambda ref:
                bool(ref.index.base_text_titles) or original_empty(ref)):
            rows = self.records('רש"י פירש ' + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a:3'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a:3'])

    def test_prefix_author_cannot_cross_a_new_subject_or_borrow_a_qualified_work(self):
        anchor = 'בפ"ק (דף ב.'
        for before in ('רש"י פירש ואילו ', 'רש"י פירש אבל אמרינן ',
                       'רש"י לא פירש ', 'פירש בתוספות ישנים ', 'פירש בתוספות רי"ד ',
                       'פירש בתוספות הרא"ש ', 'פירש ר"ת '):
            text = before + anchor + ')'
            self.assertIsNone(lb.citation_prefix_commentary(text, len(before), len(before + anchor)), before)
            rows = self.records(text, anchor, [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], before)

    def test_a_new_writer_inside_the_entity_has_the_same_scope_as_outside(self):
        text = 'פירש רש"י והרמב"ן כתב בפ"ק (דף ב.)'
        for anchor in ('בפ"ק (דף ב.', 'והרמב"ן כתב בפ"ק (דף ב.'):
            rows = self.records(text, anchor, [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], anchor)
        anchor = 'בפ"ק (דף ב.'
        for before in ('עיין ברש"י וכן ', 'רש"י פירש וכן ', 'פירש בקונטרס וכן '):
            for predicate in ('אמרינן', 'נמי אמרינן'):
                text = before + anchor + ') ' + predicate
                for entity in (anchor, anchor + ') ' + predicate):
                    rows = self.records(text, entity,
                        [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
                    self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], (before, entity))

    def test_self_upgrade_respects_a_known_chapter_on_either_side_of_the_entity(self):
        text = 'כתבתי בפ"ק (דף לה.)'
        for anchor in ('בפ"ק (דף לה.', 'דף לה.'):
            rows = self.records(text, anchor, ([('NAMED', 'בפ"ק')] if anchor.startswith('בפ') else [])
                + [('NUMBERED', 'דף לה.')], refs=['Shevuot 35a'], source='Tosafot on Shevuot 79a:1:1')
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 35a'], anchor)

    def test_self_upgrade_cannot_borrow_a_known_chapter_from_a_foreign_work(self):
        foreign = helpers.BOOKS + (('Gittin', ('גיטין',), ((Chapter('Gittin', 3, 40),
                   {'פרק השולח', 'השולח'}),)),)
        with mock.patch.object(lb, '_bavli_chapter_books', return_value=foreign):
            lb._chapter_alias_works.cache_clear()
            self.assertFalse(lb.self_locator_gap_is_clear(['בפרק', 'השולח'], target=Ref('Shevuot 2a')))
            text = 'כתבתי בפרק השולח (דף ב.)'
            for anchor in ('בפרק השולח (דף ב.', 'דף ב.'):
                parts = ([('NAMED', 'בפרק השולח')] if anchor.startswith('בפרק') else [])
                rows = self.records(text, anchor, parts + [('NUMBERED', 'דף ב.')],
                    refs=['Shevuot 2a'], source='Tosafot on Shevuot 19a:1:1')
                self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], anchor)

    def test_a_title_prefix_is_not_a_complete_foreign_chapter_name(self):
        foreign = helpers.BOOKS + (('Gittin', ('גיטין',), ((Chapter('Gittin', 3, 40),
                   {'שני אחים', 'פרק שני אחים'}),)),)
        with mock.patch.object(lb, '_bavli_chapter_books', return_value=foreign):
            lb._chapter_alias_works.cache_clear()
            anchor = 'בפרק שני (דף יד.'
            rows = self.records('פירשתי ' + anchor + ')', anchor,
                [('NAMED', 'בפרק שני'), ('NUMBERED', 'דף יד.')], refs=['Shevuot 14a'],
                source='Tosafot on Shevuot 19a:1:1')
            self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 14a'])

    def test_editorial_amud_correction_requires_one_actual_opening_heading(self):
        anchor = 'לעיל (דף ג:'
        text = 'פי\' ' + anchor + ') [בד"ה הלכה ברורה שם ע"א]'
        source, target = Ref('Tosafot on Shevuot 19a:1:1'), Ref('Shevuot 3b')
        segment = Ref('Tosafot on Shevuot 3a:1:1')
        with mock.patch.object(Ref, 'all_segment_refs', create=True, return_value=[segment]), \
                mock.patch.object(Ref, 'text', create=True, return_value=NS(
                    as_string=lambda: '<b>הלכה ברורה היא</b> ועוד ביאור')):
            result = lb.editorial_commentary_dh_ref(target, text, 4, 4 + len(anchor), source, Ref)
            self.assertEqual(result.normal(), segment.normal())
            for predicate in ("פי'", 'פירשתי'):
                content = predicate + ' ' + anchor + ') [בד"ה הלכה ברורה שם ע"א]'
                rows = self.records(content, anchor, [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ג:')],
                    refs=['Shevuot 3b'], source=source.normal())
                self.assertEqual([r.target_ref for r in rows], [segment.normal()])
            for modified in (text.replace('שם ע"א', 'שם'), text.replace('הלכה ברורה', 'דיבור אחר'),
                             text.replace("פי'", 'אמרינן'), text.replace("פי'", 'רש"י פי\''),
                             text.replace("פי'", 'הרמב"ן פי\'')):
                at = modified.index(anchor)
                self.assertIsNone(lb.editorial_commentary_dh_ref(
                    target, modified, at, at + len(anchor), source, Ref), modified)
            with mock.patch.object(Ref, 'all_segment_refs', return_value=[segment, segment]):
                self.assertIsNone(lb.editorial_commentary_dh_ref(
                    target, text, 4, 4 + len(anchor), source, Ref))
            self.assertIsNone(lb.editorial_commentary_dh_ref(
                target, text, 4, 4 + len(anchor), Ref('Tosafot on Shevuot 2a:1:1'), Ref))

    def test_elliptical_chapter_names_require_a_local_edge_and_matching_printed_amud(self):
        for gap in (['בריש', 'העדות'], ['בסוף', 'שבועת', 'העדות', 'זוטא']):
            self.assertFalse(lb.self_locator_gap_is_clear(gap))
            self.assertTrue(lb.self_locator_gap_is_clear(gap, target=Ref('Shevuot 35a')))
            self.assertFalse(lb.self_locator_gap_is_clear(gap, target=Ref('Shevuot 2a')))
        self.assertFalse(lb.self_locator_gap_is_clear(['העדות'], target=Ref('Shevuot 35a')))
        self.assertFalse(lb.self_locator_gap_is_clear(
            ['בריש', 'שבועת', 'העדות'], target=Ref('Shevuot 2a')))

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
            self.assertEqual([r.target_ref for r in rows], ['Rashi on Shevuot 2a'])

    def test_own_work_proof_replaces_a_context_only_or_base_candidate(self):
        anchor = 'בפ"ק (דף ב.'
        for target in ('Shevuot 2a', 'Tosafot on Shevuot 2a'):
            for context in (None, 'CURRENT_BOOK'):
                rows = self.records("כדפי' " + anchor + ')', anchor,
                    [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=[target], context_type=context)
                self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_unavailable_named_and_own_commentaries_preserve_the_existing_gemara(self):
        anchor = 'בפ"ק (דף ב.'
        original_empty = Ref.is_empty
        with mock.patch.object(Ref, 'is_empty', lambda ref:
                bool(ref.index.base_text_titles) or original_empty(ref)):
            for before, tail in (('', ' פי\' רש"י'), ('', ' מפורש בתוספות'), ('כתבתי ', '')):
                rows = self.records(before + anchor + ')' + tail, anchor,
                    [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a:3'])
                self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a:3'])
                self.assertEqual(rows[0].source_hash, lb.content_hash(before + anchor + ')' + tail))
            relative = 'לעיל (דף ב.'
            rows = self.records('כתבתי ' + relative + ')', relative,
                [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a:3'],
                source='Tosafot on Shevuot 19a:1:1', context_type='CURRENT_BOOK')
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a:3'])
            self.assertEqual(rows[0].relative_direction, 'above')
            rows = self.records('כתבתי ' + relative + ')', relative,
                [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')],
                source='Tosafot on Shevuot 19a:1:1', context_type='CURRENT_BOOK')
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_named_commentary_upgrade_drops_gemara_segments_and_preserves_utf16_anchor(self):
        anchor = 'בפ"ק (דף ב.'
        text = '😀 ' + anchor + ') פי\' רש"י'
        rows = self.records(text, anchor, [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')],
                            refs=['Shevuot 2a:7'])
        self.assertEqual([r.target_ref for r in rows], ['Rashi on Shevuot 2a'])
        self.assertEqual(text.encode('utf-16-le')[rows[0].start*2:rows[0].end*2].decode('utf-16-le'), anchor)

    def test_other_tosafot_works_and_rabbenu_tam_cannot_become_ordinary_tosafot(self):
        anchor = 'בפ"ק (דף ב.'
        for tail in (' מפרש ר"ת', ' בתוס\' יו"ט', ' בתוס\' רי"ד',
                     ' בתוספות ישנים', ' בתוספות הרא"ש', ' מפורש בתוספות רי"ד'):
            rows = self.records(anchor + ')' + tail, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], tail)

    def test_named_upgrade_cannot_seed_an_ambiguous_ibid(self):
        anchor = 'בפ"ק (דף ב.'
        text = anchor + ') פי\' רש"י וכן שם'
        first = citation(text, anchor, [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a')])
        following = citation(text, 'שם', [('IBID', 'שם')],
                             refs=[Ref('Rashi on Shevuot 2a:3'), Ref('Rashi on Shevuot 3a:3')])
        self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[first, following])]
        history = lb.IbidContext()
        history.resolve = lambda _linker, invoke: invoke()
        rows, _ = lb.process_batch(self.linker, lb.BookKey('Sefaria', 'ספר'),
            [(7, text, 'Tosafot on Shevuot 3a:1:1')], self.fail,
            context_ref_factory=Ref, ibid_context=history)
        self.assertEqual([r.target_ref for r in rows], ['Rashi on Shevuot 2a'])
        self.assertIsNone(history.last_emitted_ref)

    def test_a_valid_base_ibid_is_upgraded_only_after_both_ibid_checks(self):
        first_anchor, second_anchor = 'בשבועות (דף ב.', 'שם (דף ב.'
        text = first_anchor + ') וכן ' + second_anchor + ') פרש"י'
        first = citation(text, first_anchor, [('NAMED', 'בשבועות'), ('NUMBERED', 'דף ב.')],
                         refs=[Ref('Shevuot 2a')])
        second = citation(text, second_anchor, [('IBID', 'שם'), ('NUMBERED', 'דף ב.')],
                          refs=[Ref('Shevuot 2a')])
        self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[first, second])]
        rows, _ = lb.process_batch(self.linker, lb.BookKey('Sefaria', 'ספר'),
            [(7, text, 'Tosafot on Shevuot 3a:1:1')], self.fail, context_ref_factory=Ref)
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a', 'Rashi on Shevuot 2a'])

    def test_an_amud_mark_outside_the_entity_still_rejects_the_wrong_fallback(self):
        anchor = 'בפ"ק (דף ב'
        for tail in ('.) פי\' רש"י', '.) מפורש בתוספות', '.) פרש"י'):
            rows = self.records(anchor + tail, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב')], refs=['Shevuot 2b'])
            self.assertEqual(rows, [], tail)
        for mark, target in (('.', 'Rashi on Shevuot 2a'), (':', 'Rashi on Shevuot 2b')):
            rows = self.records(anchor + mark + ') פרש"י', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב')], refs=['Shevuot 2'])
            self.assertEqual([r.target_ref for r in rows], [target])
        rows = self.records('כתבתי ' + anchor + '.)', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב')], refs=['Shevuot 2b'])
        self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])
        for anchor in ('בפ"ק (דף ב', 'בפ"ק (דף ב.'):
            rows = self.records('כתבתי ' + anchor + ('.)' if anchor.endswith('ב') else ')'), anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', anchor.split('(')[1])], refs=['Shevuot 2'])
            self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_unqualified_tosafot_followed_by_a_new_clause_can_be_upgraded(self):
        anchor = 'בפ"ק (דף ב.'
        rows = self.records(anchor + ') בתוס\' ולסברת המחבר', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])

    def test_a_named_foreign_tractate_cannot_be_upgraded_in_the_offered_tractate(self):
        anchor = 'בגיטין (דף ב.'
        rows = self.records(anchor + ') פרש"י', anchor,
            [('NAMED', 'בגיטין'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual(rows, [])
        anchor = 'דף ב.'
        rows = self.records('בגיטין (' + anchor + ') פרש"י', anchor,
            [('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual(rows, [])
        rows = self.records('כבר בגיטין נדון ענין אחר. בשבועות (' + anchor + ') פרש"י', anchor,
            [('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Rashi on Shevuot 2a'])

    def test_display_upgrade_preserves_base_ibid_history_across_checkpoint(self):
        first_anchor, second_anchor, third_anchor = 'בשבועות (דף ב.', 'שם (דף ב.', 'ושם'
        texts = [first_anchor + ')', second_anchor + ') פרש"י', third_anchor]
        citations = [
            citation(texts[0], first_anchor, [('NAMED', 'בשבועות'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a:1')]),
            citation(texts[1], second_anchor, [('IBID', 'שם'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a:1')]),
            citation(texts[2], third_anchor, [('IBID', 'ושם')], refs=[Ref('Shevuot 2a:3'), Ref('Shevuot 3a:3')]),
        ]
        by_text = dict(zip(texts, citations))
        self.linker.bulk_link = lambda inputs, **_k: [NS(resolved_refs=[by_text[t]]) for t in inputs]
        book = lb.BookKey('Sefaria', 'ספר')
        batch = [(i, text, 'Tosafot on Shevuot 3a:1:1') for i, text in enumerate(texts)]
        def history():
            result = lb.IbidContext()
            result.resolve = lambda _linker, invoke: invoke()
            return result
        original_empty = Ref.is_empty
        for missing in (False, True):
            lb._TARGET_TEXT_CACHE.clear()
            with mock.patch.object(Ref, 'is_empty', lambda ref:
                    missing and ref.index.title.startswith('Rashi on ') or original_empty(ref)):
                whole_history = history()
                whole, _ = lb.process_batch(self.linker, book, batch, self.fail,
                    context_ref_factory=Ref, ibid_context=whole_history)
                self.assertEqual([r.target_ref for r in whole],
                    ['Shevuot 2a:1', 'Shevuot 2a:1' if missing else 'Rashi on Shevuot 2a', 'Shevuot 2a:3'])
                split = history()
                left, _ = lb.process_batch(self.linker, book, batch[:2], self.fail,
                    context_ref_factory=Ref, ibid_context=split)
                state = split.state(book, 0, batch[:2], 'a' * 64)
                resumed = history()
                resumed.restore_state(state, Ref, book, 0, batch[:2], 'a' * 64)
                right, _ = lb.process_batch(self.linker, book, batch[2:], self.fail,
                    context_ref_factory=Ref, ibid_context=resumed)
                self.assertEqual([r.to_dict() for r in left + right], [r.to_dict() for r in whole])

    def test_existing_named_chapter_range_is_preserved_without_a_daf_upgrade(self):
        target = Ref('Shevuot 2a:3')
        target.toSections = [27, 4]
        anchor = 'בפרק קמא'
        text = anchor + ' מפורש בתוספות'
        self.assertIsNone(lb.named_commentary_citation_ref(target, text, 0, len(anchor), Ref))

    def test_existing_address_numbers_are_not_tractate_or_chapter_aliases(self):
        for anchor, target in (('שבועות דל"ט ע"ב', 'Shevuot 39b'),
                               ('שבועות דל"ט.', 'Shevuot 39a'),
                               ('שבועות דל"ט:', 'Shevuot 39b'),
                               ('שבועות פ"ח ע"ב', 'Shevuot 88b'),
                               ('שבועות פ"ח.', 'Shevuot 88a'),
                               ('שבועות פ"ח:', 'Shevuot 88b'),
                               ('שבועות דף ע"ב', 'Shevuot 72'),
                               ('שבועות ע"ז א\'', 'Shevuot 77a')):
            self.assertTrue(lb.existing_base_address_agrees(Ref(target), anchor, Ref), anchor)
        books = list(helpers.BOOKS)
        title, aliases, chapters = books[-1]
        books[-1] = title, aliases + ('ע"ז', 'ס"ח'), chapters
        with mock.patch.object(lb, '_bavli_chapter_books', return_value=tuple(books)):
            self.assertTrue(lb.existing_base_address_agrees(Ref('Shevuot 77a'), 'שבועות ע"ז א\'', Ref))
            self.assertTrue(lb.existing_base_address_agrees(Ref('Shevuot 68'), 'בשבועות דף ס"ח', Ref))

    def test_an_implicit_own_chapter_cannot_upgrade_a_foreign_upstream_work_guess(self):
        anchor = 'בפרק קמא (דף ב.'
        text = 'כתבתי ' + anchor + ')'
        rr = citation(text, anchor, [('NAMED', 'בפרק קמא'), ('NUMBERED', 'דף ב.')], refs=[Ref('Gittin 2a')])
        actual = lb.own_commentary_self_ref(rr, Ref('Gittin 2a'), text, *rr.raw_entity.span.range,
                                          Ref('Tosafot on Shevuot 3a:1:1'), Ref)
        self.assertEqual(actual.normal(), 'Tosafot on Shevuot 2a')
        text = 'כתבתי במקום אחר (דף ב.)'
        self.assertIsNone(lb.own_commentary_self_ref(rr, Ref('Gittin 2a'), text, 7, len(text),
                                                   Ref('Tosafot on Shevuot 3a:1:1'), Ref))

    def test_an_explicit_foreign_tractate_inside_parentheses_keeps_its_own_commentary(self):
        with mock.patch.dict(Ref.BASES, {'Tosafot on Gittin': ['Gittin']}):
            for anchor in ('בפרק קמא (גיטין דף ב:', 'בפרק קמא (גיטין דף ב:)'):
                text = 'כתבתי ' + anchor
                rr = citation(text, anchor, [('NAMED', 'בפרק קמא'), ('NAMED', 'גיטין'),
                    ('NUMBERED', 'דף ב:')], refs=[Ref('Gittin 2b')])
                actual = lb.own_commentary_self_ref(rr, Ref('Gittin 2b'), text, *rr.raw_entity.span.range,
                                                   Ref('Tosafot on Shevuot 3a:1:1'), Ref)
                self.assertEqual(actual.normal(), 'Tosafot on Gittin 2b')

    def test_an_independent_printed_self_address_repairs_the_page_without_stale_ibid(self):
        anchor = 'בשבועות דף ב.'
        text = 'כתבתי ' + anchor + ')'
        first = citation(text, anchor, [('NAMED', 'בשבועות'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 3a')])
        stale = citation('שם', 'שם', [('IBID', 'שם')], refs=[Ref('Shevuot 3a:4')])
        self.linker.bulk_link = lambda inputs, **_k: [NS(resolved_refs=[first if t == text else stale]) for t in inputs]
        book = lb.BookKey('Sefaria', 'ספר')
        batch = [(7, text, 'Tosafot on Shevuot 19a:1:1'), (8, 'שם', 'Tosafot on Shevuot 19a:1:1')]
        history = lb.IbidContext()
        history.resolve = lambda _linker, invoke: invoke()
        first_rows, _ = lb.process_batch(self.linker, book, batch[:1], self.fail,
            context_ref_factory=Ref, ibid_context=history)
        self.assertEqual([r.target_ref for r in first_rows], ['Tosafot on Shevuot 2a'])
        self.assertIsNone(history.last_emitted_ref)
        self.assertIsNone(history.previous_citation[4])
        state = history.state(book, 0, batch[:1], 'a' * 64)
        resumed = lb.IbidContext()
        resumed.resolve = lambda _linker, invoke: invoke()
        resumed.restore_state(state, Ref, book, 0, batch[:1], 'a' * 64)
        rows, _ = lb.process_batch(self.linker, book, batch[1:], self.fail,
            context_ref_factory=Ref, ibid_context=resumed)
        self.assertEqual(rows, [])

    def test_own_dh_is_proven_independently_without_changing_base_ibid_history(self):
        first_anchor, dh_anchor = 'בפ"ק (לעיל דף ב.', 'שם ד"ה והא'
        texts = ['פירשנו ' + first_anchor + ')', 'פירשתי ' + dh_anchor, 'שם']
        segment = Ref('Tosafot on Shevuot 2a:1:1')
        segment.text = lambda _lang: NS(as_string=lambda: 'והא דאמרינן - פירוש הדברים')
        citations = [
            citation(texts[0], first_anchor, [('NAMED', 'בפ"ק'), ('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a')]),
            citation(texts[1], dh_anchor, [('IBID', 'שם'), ('DH', 'ד"ה והא')], refs=[segment]),
            citation(texts[2], 'שם', [('IBID', 'שם')], refs=[Ref('Shevuot 2a:4')]),
        ]
        by_text = dict(zip(texts, citations))
        self.linker.bulk_link = lambda inputs, **_k: [NS(resolved_refs=[by_text[t]]) for t in inputs]
        book = lb.BookKey('Sefaria', 'ספר')
        batch = [(i, text, 'Tosafot on Shevuot 19a:1:1') for i, text in enumerate(texts)]
        def history():
            result = lb.IbidContext()
            result.resolve = lambda _linker, invoke: invoke()
            return result
        with mock.patch.object(Ref, 'all_segment_refs', lambda _ref: [segment], create=True):
            whole, _ = lb.process_batch(self.linker, book, batch, self.fail,
                context_ref_factory=Ref, ibid_context=history())
            self.assertEqual([r.target_ref for r in whole],
                ['Tosafot on Shevuot 2a', segment.normal(), 'Shevuot 2a:4'])
            for boundary in (1, 2):
                left_history = history()
                left, _ = lb.process_batch(self.linker, book, batch[:boundary], self.fail,
                    context_ref_factory=Ref, ibid_context=left_history)
                state = left_history.state(book, 0, batch[:boundary], 'a' * 64)
                resumed = history()
                resumed.restore_state(state, Ref, book, 0, batch[:boundary], 'a' * 64)
                right, _ = lb.process_batch(self.linker, book, batch[boundary:], self.fail,
                    context_ref_factory=Ref, ibid_context=resumed)
                self.assertEqual([r.to_dict() for r in left + right], [r.to_dict() for r in whole])
            attached_dh = 'ושם ד"ה והא'
            text = 'פירשנו ' + first_anchor + ' ' + attached_dh + ') וכדאמר שם'
            first = citation(text, first_anchor, [('NAMED', 'בפ"ק'), ('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], refs=[Ref('Shevuot 2a')])
            dh = citation(text, attached_dh, [('IBID', 'ושם'), ('DH', 'ד"ה והא')], refs=[segment])
            native = citation(text, 'וכדאמר שם', [('IBID', 'שם')], refs=[Ref('Shevuot 2a:4')])
            self.assertNotEqual(lb.citation_local_attribution(text, *dh.raw_entity.span.range), 'self')
            self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[first, dh, native])]
            rows, _ = lb.process_batch(self.linker, book, [(7, text, 'Tosafot on Shevuot 19a:1:1')],
                self.fail, context_ref_factory=Ref, ibid_context=history())
            self.assertEqual([r.target_ref for r in rows],
                ['Tosafot on Shevuot 2a', segment.normal(), 'Shevuot 2a:4'])

    def test_a_chapter_fallback_cannot_contradict_its_attached_printed_daf(self):
        anchor = 'בפרק קמא'
        text = 'פירשתי ' + anchor + ' (לעיל דף לה.)'
        rows = self.records(text, anchor, [('NAMED', anchor)], refs=['Shevuot 2a'],
                            source='Tosafot on Shevuot 36b:1:1')
        self.assertEqual(rows, [])
        text = 'פירשתי ' + anchor + ' (לעיל דף ב.)'
        rows = self.records(text, anchor, [('NAMED', anchor)], refs=['Shevuot 2a'],
                            source='Tosafot on Shevuot 36b:1:1')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])
        for anchor, following in (('בפ"ק', ' (לעיל דף כט:) פרש"י'),
                                  ('בפ"ק (', 'לעיל דף כט:) פרש"י')):
            target = Ref('Shevuot 2a:3')
            target.toSections = [27, 4]
            self.assertFalse(lb.existing_base_address_agrees(target, anchor, Ref, following=following))

    def test_existing_legacy_chapter_recovery_validates_base_before_self_upgrade(self):
        anchor = 'בפ"ק (דף ב'
        for missing in (False, True):
            original_empty = Ref.is_empty
            lb._TARGET_TEXT_CACHE.clear()
            with (mock.patch.object(lb, 'parenthesized_address', return_value=None),
                  mock.patch.object(lb, 'chapter_led_citation', return_value=(
                      Ref('Shevuot 2'), (Chapter('Shevuot', 3, 27),))),
                  mock.patch.object(Ref, 'is_empty', lambda ref:
                      missing and ref.index.title.startswith('Tosafot on ') or original_empty(ref))):
                rows = self.records('כתבתי ' + anchor + ':)', anchor,
                    [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב')])
                self.assertEqual([r.target_ref for r in rows],
                    ['Shevuot 2b' if missing else 'Tosafot on Shevuot 2b'])

    def test_a_legacy_relative_with_no_offered_ref_preserves_its_proven_base_address(self):
        anchor = 'לעיל דף ב.'
        rows = self.records('הא פרישנא טעמא משום דין אחר בפרק קמא (' + anchor + ')', anchor,
            [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], source='Rashi on Shevuot 19a:1')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])
        self.assertEqual(rows[0].relative_direction, 'above')
        rows = self.records('פירשו ' + anchor + ')', anchor,
            [('RELATIVE', 'לעיל'), ('NUMBERED', 'דף ב.')], source='Tosafot on Shevuot 19a:1:1')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_look_instruction_before_an_explicit_tractate_is_not_an_eruvin_alias(self):
        books = list(helpers.BOOKS)
        title, aliases, chapters = books[-1]
        books[-1] = title, aliases + ("עי'",), chapters
        anchor = 'בשבועות (דף ב.'
        with mock.patch.object(lb, '_bavli_chapter_books', return_value=tuple(books)):
            for command in ("עי'", "ועי'", 'עיין', 'ועיין', 'ראה', 'וראה'):
                rows = self.records(command + ' ' + anchor + ') ברש"י ד"ה אחר', anchor,
                    [('NAMED', 'בשבועות'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
                self.assertEqual([r.target_ref for r in rows], ['Rashi on Shevuot 2a'], command)

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
        for context, expected in ((None, ['Shevuot 2a']), ('CURRENT_BOOK', [])):
            rows = self.records('בגיטין (דף ב.) וכדאיתא ' + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 3a'], context_type=context)
            self.assertEqual([r.target_ref for r in rows], expected)

    def test_postfix_commentary_after_parenthetical_qualifier_is_not_gemara(self):
        anchor = 'בפ"ק (דף ב.'
        for tail in (' מפורש בתוספות', ' (גז"ש) מפורש בתוספות'):
            rows = self.records(anchor + ')' + tail, anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])
        rows = self.records(anchor + ') (גז"ש) מפורש בגמרא', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'])

    def test_local_self_objects_and_independent_native_rejection_keep_their_work(self):
        anchor = 'בפ"ק (דף ב.'
        for prefix in ('וכבר כתבתי זה ', 'הא פרישנא טעמא '):
            rows = self.records(prefix + anchor + ')', anchor,
                [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
            self.assertEqual([r.target_ref for r in rows], ['Tosafot on Shevuot 2a'])
        rows = self.records('כדפי\' שם בקונטרס הא דפסלינן התם ' + anchor + ')', anchor,
            [('NAMED', 'בפ"ק'), ('NUMBERED', 'דף ב.')], refs=['Shevuot 2a'])
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
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 2a'], before)

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
