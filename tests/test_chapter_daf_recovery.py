"""v36 reports: chapter locators, cut-off amud marks and absolute daf lists."""
import re
import sys
import types
import unittest
from unittest import mock

from test_recovery_evidence import lb, citation, Raw, NS


class Ref:
    BASES = {
        'Tosafot on Shevuot': ['Shevuot'],
        'Rashba on Shabbat': ['Shabbat'],
        'Ran on Shevuot': ['Rif Shevuot'],
        'Rif Shevuot': [],
    }

    def __init__(self, text):
        title, daf, amud, segment = re.fullmatch(
            r'(.+?)(?: (\d+)([ab])(?::([\d:]+))?)?', text).groups()
        if title not in {'Shevuot', 'Sukkah', 'Shabbat', 'Gittin', *self.BASES}:
            raise ValueError(text)
        base = self.BASES.get(title, [])
        self.index = NS(title=title, base_text_titles=base,
                        dependence='Commentary' if base else None,
                        categories=['Talmud', 'Bavli'] + (['Rishonim on Talmud'] if base else [])
                        + ['Seder Moed'], get_title=lambda _: title)
        self.sections = [] if daf is None else [2 * int(daf) - (amud == 'a')]
        if segment:
            self.sections += [int(n) for n in segment.split(':')]
        self.toSections = self.sections[:]
        self.value = text

    def normal(self):
        return self.value

    def is_talmud(self):
        return bool(self.sections)

    def is_empty(self):
        return not self.sections or self.sections[0] > 300

    def is_range(self):
        return False

    def order_id(self):
        return tuple(self.sections)


class Chapter:
    def __init__(self, title, first, last):
        self.title, self.first, self.last = title, first, last

    def overlaps(self, ref):
        return ref.index.title == self.title and self.first <= ref.sections[0] <= self.last


BOOKS = (
    ('Shevuot', ('שבועות',), (
        (Chapter('Shevuot', 3, 27), {'פרק קמא', 'פ"ק'}),
        (Chapter('Shevuot', 27, 38), {'פ"ב', 'ספ"ב'}),
        (Chapter('Shevuot', 38, 58), {'פ"ג'}),
        (Chapter('Shevuot', 59, 70), {'פרק שבועת העדות', 'שבועת העדות'}),
    )),
    ('Sukkah', ('סוכה',), ((Chapter('Sukkah', 3, 40), {'פרק קמא', 'פ"ק'}),)),
    ('Shabbat', ('שבת',), ((Chapter('Shabbat', 3, 40), {'פרק קמא', 'פ"ק'}),)),
)


class ChapterDafRecoveryTest(unittest.TestCase):
    def setUp(self):
        lb._TARGET_TEXT_CACHE.clear()
        self.addCleanup(mock.patch.stopall)
        module = types.ModuleType('sefaria.model.linker.ref_part')
        module.RawRef = Raw
        module.LEAF_TRIE_ENTRY = 'leaf'
        mock.patch.dict(sys.modules, {'sefaria.model.linker.ref_part': module}).start()
        mock.patch.object(lb, '_bavli_chapter_books', return_value=BOOKS).start()
        mock.patch.object(lb, '_book_chapters', side_effect=lambda title: next(
            (chapters for book, _, chapters in BOOKS if title == book), ())).start()
        self.linker = NS(_ref_resolver=NS(get_ref_part_title_trie=lambda: NS(
            get_continuations=lambda *_a, **_k: (None, None))))

    def records(self, text, anchor, parts, refs=(), source='Tosafot on Shevuot 3a:1:1',
                book_source='Sefaria', context_type=None):
        rr = citation(text, anchor, parts, [Ref(r) for r in refs], failed=not refs)
        rr.context_type = None if context_type is None else NS(name=context_type)
        self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[rr])]
        return lb.process_batch(self.linker, lb.BookKey(book_source, source), [(7, text, source)],
                                self.fail, context_ref_factory=Ref)[0]

    def test_a_chapter_and_failed_relative_address_use_the_new_governing_clause(self):
        for before, anchor, target, source in (
            ('דקאמר ידיעה בתחלה ויש בה ידיעה בסוף בר קרבן הוא ואמר נמי ',
             'בספ"ב (לקמן דף יט:', 'Shevuot 19b', 'Tosafot on Shevuot 3a:1:1'),
            ('דהא גמרינן מע"ז דכל שחייבים על זדונו כרת חייבים על שגגתו חטאת והא דאמר ',
             'בפ"ק (לעיל דף ב.', 'Shevuot 2a', 'Tosafot on Shevuot 19a:1:1'),
        ):
            parts = [('NUMBERED', anchor.split(' ')[0]),
                     ('RELATIVE', 'לעיל' if 'לעיל' in anchor else 'לקמן'),
                     ('NUMBERED', anchor[anchor.index('דף'):])]
            rows = self.records(before + anchor + ') אמרו', anchor, parts,
                                refs=['Tosafot on Shevuot 2a'] if 'לעיל' in anchor else (), source=source)
            self.assertEqual([r.target_ref for r in rows], [target])

    def test_cut_off_amud_is_recovered_before_relative_resolution_with_utf16_offsets(self):
        for gap in ('', ' ', '</small> '):
            anchor = 'בפ"ב (לקמן דף יד'
            text = '😀 וכן משמע ' + anchor + gap + ': ושם) אמרו'
            rows = self.records(text, anchor, [
                ('NUMBERED', 'בפ"ב'), ('RELATIVE', 'לקמן'), ('NUMBERED', 'דף יד')])
            self.assertEqual([r.target_ref for r in rows], ['Shevuot 14b'])
            row = rows[0]
            self.assertEqual(text.encode('utf-16-le')[2 * row.start:2 * row.end].decode('utf-16-le'),
                             anchor + gap + ':')
            self.assertEqual(row.source_hash, lb.content_hash(text))
            self.assertEqual(row.relative_direction, 'below')

    def test_chapter_outside_the_ner_entity_still_validates_the_cut_off_address(self):
        text = 'כדאמר בפ"ג (לקמן דף כא: ושם) דקאי בלהבא'
        rows = self.records(text, 'לקמן דף כא', [('RELATIVE', 'לקמן'), ('NUMBERED', 'דף כא')],
                            refs=['Shevuot 21a', 'Tosafot on Shevuot 21b'])
        self.assertEqual([(r.target_ref, text[r.start:r.end]) for r in rows],
                         [('Shevuot 21b', 'לקמן דף כא:')])

    def test_wrong_chapter_direction_foreign_work_and_dh_do_not_become_gemara(self):
        for before, anchor, after in (
            ('כדאמר ', 'בפ"ב (לקמן דף כא:', ')'),
            ('כדאמר ', 'בפ"ב (לעיל דף יד:', ')'),
            ('בקונטרס גופיה פירש ', 'לעיל (דף ב.', ')'),
            ('כדאמר ', 'לעיל (דף ב.', ') [בד"ה אחר וכו\']'),
        ):
            relative = 'לעיל' if 'לעיל' in anchor else 'לקמן'
            parts = [('RELATIVE', relative), ('NUMBERED', anchor[anchor.index('דף'):])]
            if anchor.startswith('בפ'):
                parts.insert(0, ('NUMBERED', anchor.split(' ')[0]))
            self.assertEqual(self.records(before + anchor + after, anchor, parts), [], anchor)
        text = 'כדאמר לקמן דף כא:'
        self.assertEqual(self.records(text, 'לקמן דף כא:', [('RELATIVE', 'לקמן'), ('NUMBERED', 'דף כא:')],
                                      refs=['Gittin 21b']), [])

    def test_cut_off_mark_is_not_taken_from_sentence_punctuation_or_html_attributes(self):
        for tail in (':', ': עוד אמרו', '. עוד אמרו', '<span title=": ושם">אחר</span>', ': ושםדבר',
                     '<span title=">: ושם">אחר</span>', ': <span title=">ושם">אחר</span>'):
            text = 'לקמן דף יד' + tail
            self.assertEqual(lb.relative_address_end(text, 0, len('לקמן דף יד')), len('לקמן דף יד'))

    def test_a_nearby_relative_in_rashba_uses_the_direct_base_tractate(self):
        text = 'וכדאיתא נמי לקמן בסמוך (מ, א)'
        rows = self.records(text, 'לקמן בסמוך (מ, א', [
            ('RELATIVE', 'לקמן'), ('RELATIVE', 'בסמוך'), ('NUMBERED', 'מ, א')],
            refs=['Rashba on Shabbat 40a'], source='Rashba on Shabbat 39b:6')
        self.assertEqual([r.target_ref for r in rows], ['Shabbat 40a'])

    def test_a_unique_named_chapter_in_ran_on_rif_uses_absolute_bavli_daf(self):
        anchor = 'בפרק שבועת העדות (דף לה.'
        rows = self.records('כמאן כרבי חנינא בר אידי ' + anchor + ') איתא', anchor,
                            [('NAMED', 'בפרק שבועת העדות'), ('NUMBERED', 'דף לה.')],
                            source='Ran on Shevuot 18a:7')
        self.assertEqual([r.target_ref for r in rows], ['Shevuot 35a'])
        self.assertIsNone(rows[0].relative_direction)
        self.assertIsNone(rows[0].context_ref)

    def test_an_explicit_chapter_daf_list_is_split_at_the_printed_addresses(self):
        anchor = 'בפרק קמא דסוכה (יג, ב. יד, א'
        text = '😀 והכין מוכח ' + anchor + ') גבי קצר לאכילה'
        rows = self.records(text, anchor, [('NAMED', 'בפרק קמא'), ('NAMED', 'דסוכה'),
                            ('NUMBERED', 'יג, ב'), ('NUMBERED', 'יד'), ('NUMBERED', 'א')],
                            refs=['Sukkah 20a'], source='Rashba on Shabbat 52b:1')
        self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b', 'Sukkah 14a'])
        self.assertEqual([text.encode('utf-16-le')[2*r.start:2*r.end].decode('utf-16-le') for r in rows],
                         ['בפרק קמא דסוכה (יג, ב', 'יד, א'])
        self.assertLessEqual(rows[0].end, rows[1].start)
        self.assertTrue(all(r.source_hash == lb.content_hash(text) for r in rows))

    def test_ambiguous_chapter_in_rif_commentary_cannot_use_its_unrelated_pagination(self):
        anchor = 'בפרק קמא (דף יג, ב'
        self.assertEqual(self.records(anchor + ')', anchor,
                                     [('NAMED', 'בפרק קמא'), ('NUMBERED', 'דף יג, ב')],
                                     source='Ran on Shevuot 18a:7'), [])

    def test_a_named_writer_or_following_dh_blocks_chapter_reconstruction(self):
        anchor = 'בפרק שבועת העדות (דף לה.'
        parts = [('NAMED', 'בפרק שבועת העדות'), ('NUMBERED', 'דף לה.')]
        for before, after in [('פירשתי ', ')'), ('וכמו שכתב ', ')'), ('', ') בד"ה אחר')]:
            self.assertEqual(self.records(before + anchor + after, anchor, parts,
                                          source='Ran on Shevuot 18a:7'), [])

    def test_chapter_reconstruction_requires_every_list_member_to_be_valid(self):
        for body in ('יג, ב. מא, א', 'יג, ב. פנ, א', 'יג, ב. יד', 'יג, ב. שם', 'יג, ב-יד, א'):
            anchor = 'בפרק קמא דסוכה (' + body
            rr = citation(anchor, anchor, [('NAMED', 'בפרק קמא'), ('NAMED', 'דסוכה')], failed=True)
            self.assertEqual(lb.explicit_chapter_dapim(self.linker, rr, anchor,
                                                      Ref('Rashba on Shabbat 52b:1'), Ref), (), body)

    def test_already_correct_precise_segment_is_retained(self):
        anchor = 'בפרק קמא דסוכה (יג, ב'
        rows = self.records(anchor + ')', anchor,
                            [('NAMED', 'בפרק קמא'), ('NAMED', 'דסוכה'), ('NUMBERED', 'יג, ב')],
                            refs=['Sukkah 13b:5'], source='Rashba on Shabbat 52b:1')
        self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b:5'])

    def test_rebuilt_list_preserves_legacy_history_and_checkpoints_the_last_printed_address(self):
        anchor = 'בפרק קמא דסוכה (יג, ב. יד, א'
        rr = citation(anchor, anchor, [('NAMED', 'בפרק קמא'), ('NAMED', 'דסוכה'),
                      ('NUMBERED', 'יג, ב'), ('NUMBERED', 'יד'), ('NUMBERED', 'א')],
                      refs=[Ref('Sukkah 20a')])
        self.linker.bulk_link = lambda *_a, **_k: [NS(resolved_refs=[rr])]
        history = lb.IbidContext()
        history.resolve = lambda _linker, invoke: invoke()
        history.last_emitted_ref = Ref('Shevuot 2a')
        history.resolved_refs = (Ref('Shevuot 2a'),)
        book = lb.BookKey('Sefaria', 'רשבא על שבת')
        batch = [(7, anchor, 'Rashba on Shabbat 52b:1')]
        rows, _ = lb.process_batch(self.linker, book, batch, self.fail,
                                    context_ref_factory=Ref, ibid_context=history)
        self.assertEqual([r.target_ref for r in rows], ['Sukkah 13b', 'Sukkah 14a'])
        self.assertEqual(history.last_emitted_ref.normal(), 'Shevuot 2a')
        self.assertEqual([r.normal() for r in history.resolved_refs], ['Shevuot 2a'])
        state = history.state(book, 0, batch, 'a' * 64)
        restored = lb.IbidContext()
        restored.restore_state(state, Ref, book, 0, batch, 'a' * 64)
        self.assertEqual(restored.state(book, 0, batch, 'a' * 64), state)
        self.assertEqual(restored.previous_citation[4].normal(), 'Sukkah 14a')

    def test_a_longer_competing_chapter_name_does_not_supply_unique_work_identity(self):
        competing = ('Gittin', ('גיטין',), ((Chapter('Gittin', 3, 160),
                                           {'פרק שבועת העדות והדיינים'}),))
        with mock.patch.object(lb, '_bavli_chapter_books', return_value=BOOKS + (competing,)):
            anchor = 'בפרק שבועת העדות (דף לה.'
            self.assertEqual(self.records(anchor, anchor,
                [('NAMED', 'בפרק שבועת העדות'), ('NUMBERED', 'דף לה.')],
                source='Ran on Shevuot 18a:7'), [])

    def test_non_sefaria_paragraph_context_cannot_disambiguate_a_shared_chapter(self):
        anchor = 'בפרק קמא (דף יג, ב'
        self.assertEqual(self.records(anchor, anchor,
            [('NAMED', 'בפרק קמא'), ('NUMBERED', 'דף יג, ב')], book_source='DictaToOtzaria'), [])

    def test_established_base_targets_keep_their_existing_ownership_checks(self):
        # A stricter recovery refusal is not proof an established Gemara target
        # was wrong. This was caught by replaying the whole Rashba book.
        anchor = 'לקמן (מ, א'
        with mock.patch.object(lb, 'relative_clause_is_clear', return_value=False), \
                mock.patch.object(lb, 'clause_names_elsewhere', return_value=False):
            rows = self.records('כדאמרינן ' + anchor + ')', anchor,
                [('RELATIVE', 'לקמן'), ('NUMBERED', 'מ, א')],
                refs=['Shabbat 40a'], source='Rashba on Shabbat 39b:6')
        self.assertEqual([r.target_ref for r in rows], ['Shabbat 40a'])

    def test_an_explicit_self_reference_in_rashba_keeps_its_commentary_identity(self):
        anchor = 'לקמן בסמוך (מ, א'
        rows = self.records('כבר כתבתי ' + anchor + ')', anchor,
            [('RELATIVE', 'לקמן'), ('RELATIVE', 'בסמוך'), ('NUMBERED', 'מ, א')],
            refs=['Rashba on Shabbat 40a'], source='Rashba on Shabbat 39b:6', context_type='CURRENT_BOOK')
        self.assertEqual([r.target_ref for r in rows], ['Rashba on Shabbat 40a'])


if __name__ == '__main__':
    unittest.main()
