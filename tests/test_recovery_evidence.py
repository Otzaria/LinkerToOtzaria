"""Precision regressions for recovered citations, including real PR #13 counterexamples."""
import os
import re
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))
import link_books as lb
from test_citation_plausibility import TalmudRef, _candidates
from test_parenthesized_address import _Titles, _LEAF, _work

NS = types.SimpleNamespace
VERSES = {
    'Genesis 1:5': 'ויקרא אלהים לאור יום ולחשך קרא לילה ויהי ערב ויהי בקר יום אחד',
    'Genesis 48:8': 'וירא ישראל את בני יוסף ויאמר מי אלה',
    'Genesis 48:12': 'ויוצא יוסף אתם מעם ברכיו וישתחו לאפיו ארצה',
    'Genesis 48:13': 'ויקח יוסף את שניהם את אפרים בימינו משמאל ישראל',
    'Genesis 48:17': 'וירא יוסף כי ישית אביו יד ימינו על ראש אפרים',
    'Proverbs 19:13': 'הוות לאביו בן כסיל ודלף טרד מדיני אשה',
    'Proverbs 19:14': 'בית והון נחלת אבות ומה אשה משכלת',
    'Leviticus 15:19': 'ואשה כי תהיה זבה דם יהיה זבה בבשרה שבעת ימים תהיה בנדתה',
    'Leviticus 11:4': 'אך את זה לא תאכלו ממעלי הגרה וממפרסי הפרסה את הגמל',
    'Deuteronomy 14:7': 'אך את זה לא תאכלו ממעלי הגרה וממפריסי הפרסה השסועה את הגמל',
    'Deuteronomy 1:13': 'הבו לכם אנשים חכמים ונבנים וידעים לשבטיכם ואשימם בראשיכם',
}
BOOKS = {book: ' '.join(v for k, v in VERSES.items() if k.startswith(book + ' '))
         for book in ('Genesis', 'Leviticus', 'Deuteronomy', 'Proverbs')}


class Verse:
    def __init__(self, normal):
        title, nums = normal.rsplit(' ', 1)
        if title not in BOOKS:
            raise ValueError(normal)
        self.index = NS(title=title, categories=['Tanakh', 'Torah'], dependence=None, base_text_titles=[])
        self.sections = [int(n) for n in nums.split(':')]
        self.toSections = list(self.sections)
        self._normal = normal

    def normal(self):
        return self._normal

    def is_talmud(self):
        return False

    def is_empty(self):
        return self._normal not in VERSES


class Span:
    def __init__(self, content, start, end):
        self.range = start, end
        self.text = content[start:end]
        self.doc = NS(text=content, subspan=lambda s: Span(content, s.start, s.stop))


class Part:
    def __init__(self, kind, text, start, end):
        self.type, self.text, self.span = NS(name=kind), text, NS(range=(start, end))

    def realign_to_new_raw_ref(self, old, new):
        delta = old.range[0] - new.range[0]
        return Part(self.type.name, self.text, self.span.range[0] + delta, self.span.range[1] + delta)


class Raw:
    def __init__(self, span, lang, parts):
        self.span, self.lang, self.raw_ref_parts = span, lang, parts

    def subspan(self, which):
        parts = self.raw_ref_parts[which]
        return self.span.doc.subspan(slice(self.span.range[0] + parts[0].span.range[0],
                                          self.span.range[0] + parts[-1].span.range[1]))


def citation(content, anchor, parts, refs=(), failed=False):
    start = content.index(anchor)
    found, cursor = [], 0
    for kind, text in parts:
        at = anchor.index(text, cursor)
        found.append(Part(kind, text, at, at + len(text)))
        cursor = at + len(text)
    raw = Raw(Span(content, start, start + len(anchor)), 'he', found)
    return NS(raw_entity=raw, resolution_failed=failed, is_ambiguous=len(refs) > 1,
              ref=refs[0] if len(refs) == 1 else None, context_type=None,
              resolved_raw_refs=[NS(ref=r, context_type=None) for r in refs])


class QuoteFixtures:
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(lb, '_hebrew_ref_words', lambda n: lb.hebrew_quote_words(VERSES.get(n, ''))).start()
        mock.patch.object(lb, '_hebrew_book_quote_text', lambda n: ' ' + BOOKS[n] + ' ').start()
        mock.patch.object(lb, '_tanakh_quote_books', lambda: tuple(
            (n, ' ' + words + ' ') for n, words in BOOKS.items())).start()


class QuotationEvidenceTest(QuoteFixtures, unittest.TestCase):

    def test_the_reported_niddah_citation_needs_the_actual_verse_words(self):
        text = 'שנאמר בְּנִדָּה <small>(ויקרא טו יט)</small> "שבעת ימים תהיה בנדתה"'
        raw = citation(text, text[text.index('בְּנִדָּה'):text.index('יט') + 2],
                       [('NAMED', 'בְּנִדָּה'), ('NAMED', 'ויקרא'), ('NUMBERED', 'טו'), ('NUMBERED', 'יט')]).raw_entity
        node = _Titles(_work('Niddah', 'Talmud'), _work('Mishnah Niddah', 'Mishnah'))
        linker = NS(_ref_resolver=NS(get_ref_part_title_trie=lambda: NS(
            get_continuations=lambda key, **_: (node if key == 'בנדה' else None, None))))
        module = types.ModuleType('sefaria.model.linker.ref_part')
        module.LEAF_TRIE_ENTRY = _LEAF
        with mock.patch.dict(sys.modules, {'sefaria.model.linker.ref_part': module}):
            self.assertTrue(lb.lead_names_other_works(linker, raw, 1, Verse('Leviticus 15:19'), text))
            wrong = text.replace('שבעת ימים תהיה בנדתה', 'הבו לכם אנשים חכמים ונבנים')
            self.assertFalse(lb.lead_names_other_works(linker, raw, 1, Verse('Leviticus 15:19'), wrong))

    def test_a_title_that_is_part_of_the_quote_can_be_recovered(self):
        text = 'ויקרא (בראשית א ה) אלהים לאור יום ולחשך קרא לילה'
        end = text.index(')')
        self.assertTrue(lb.quoted_tanakh_address(Verse('Genesis 1:5'), text, 0, end, absorbed_lead=True))
        for after in ('', ' כי אדם כי יקריב מכם קרבן', ' אלהים לאור אחר'):
            wrong = text[:end + 1] + after
            self.assertFalse(lb.quoted_tanakh_address(Verse('Genesis 1:5'), wrong, 0, end, absorbed_lead=True))

    def test_html_attributes_do_not_supply_quotation_evidence(self):
        text = 'ויקרא (בראשית א ה) <span title="אלהים לאור יום ' + 'x' * 400 + '">קרבן אחר</span>'
        self.assertFalse(lb.quoted_tanakh_address(Verse('Genesis 1:5'), text, 0, text.index(')'), absorbed_lead=True))
        text = 'ויקרא (בראשית א ה) <span title="' + 'x' * 400 + '">אלהים לאור יום</span>'
        self.assertTrue(lb.quoted_tanakh_address(Verse('Genesis 1:5'), text, 0, text.index(')'), absorbed_lead=True))
        for quote in ('"', "'"):
            text = f'ויקרא (בראשית א ה) <span title={quote}> אלהים לאור יום{quote}>קרבן אחר</span>'
            self.assertFalse(lb.quoted_tanakh_address(
                Verse('Genesis 1:5'), text, 0, text.index(')'), absorbed_lead=True))
        # A ')' inside a tag after the NER span is not the citation's closing ')'.
        text = 'ויקרא (בראשית א ה<span title="> ) אלהים לאור יום">קרבן אחר</span>'
        self.assertFalse(lb.quoted_tanakh_address(
            Verse('Genesis 1:5'), text, 0, text.index('<'), absorbed_lead=True))

    def test_an_author_outside_the_ner_span_is_not_an_introducer(self):
        text = 'וכמו שכתב רש"י ז"ל בחומש (דברים א יג) שם דנבון דומה לשולחני תגר'
        start, end = text.index('בחומש'), text.index(')')
        self.assertFalse(lb.quoted_tanakh_address(Verse('Deuteronomy 1:13'), text, start, end, absorbed_lead=True))
        # Even quoting the verse after an author's name does not discard that name.
        text = 'וכמו שכתב רש"י ז"ל בחומש (דברים א יג) הבו לכם אנשים חכמים ונבנים'
        self.assertFalse(lb.quoted_tanakh_address(Verse('Deuteronomy 1:13'), text, start, end, absorbed_lead=True))

    def test_shared_biblical_formulas_are_not_positive_evidence(self):
        text = 'וירא ישראל את בני יוסף (שם שם ח)'
        rr = citation(text, 'שם שם ח', [('IBID', 'שם'), ('IBID', 'שם'), ('NUMBERED', 'ח')], failed=True)
        with mock.patch.object(lb, '_hebrew_book_quote_text', return_value=(
                ' ' + VERSES['Genesis 48:8'] + ' ' + VERSES['Genesis 48:8'] + ' ')):
            self.assertIsNone(lb.contextual_ibid_ref(None, rr, text, *rr.raw_entity.span.range,
                                                     Verse('Genesis 48:13'), Verse))

    def test_adjacent_occurrences_share_a_boundary_but_are_still_ambiguous(self):
        with mock.patch.object(lb, '_hebrew_book_quote_text', return_value=' וירא ישראל את בני וירא ישראל את בני '):
            self.assertFalse(lb._unique_quote('Genesis', ('וירא', 'ישראל', 'את', 'בני')))

    def test_ibid_recovers_the_spelled_verse_and_never_changes_its_number(self):
        for address, expected in [('שם שם ח', 'Genesis 48:8'), ('שם מח ח', 'Genesis 48:8')]:
            text = f'וירא ישראל את בני יוסף ויאמר מי אלה ({address})'
            parts = [('IBID', 'שם'), ('IBID', 'שם'), ('NUMBERED', 'ח')] if 'שם שם' in address else [
                ('IBID', 'שם'), ('NUMBERED', 'מח'), ('NUMBERED', 'ח')]
            rr = citation(text, address, parts, failed=True)
            target = lb.contextual_ibid_ref(None, rr, text, *rr.raw_entity.span.range, Verse('Genesis 48:13'), Verse)
            self.assertEqual(target.normal(), expected)
        wrong = 'וירא ישראל את בני יוסף (שם שם יב)'
        rr = citation(wrong, 'שם שם יב', [('IBID', 'שם'), ('IBID', 'שם'), ('NUMBERED', 'יב')], failed=True)
        self.assertIsNone(lb.contextual_ibid_ref(None, rr, wrong, *rr.raw_entity.span.range,
                                               Verse('Genesis 48:13'), Verse))

    def test_maqaf_separates_words_and_offsets_are_not_normalized(self):
        self.assertEqual(lb.hebrew_quote_words('וַיַּרְא <b>אֶת־בְּנֵי</b> יוֹסֵף'), ('וירא', 'את', 'בני', 'יוסף'))
        self.assertEqual(lb.hebrew_quote_words('אֹתָ֖ם אַתֶּם אוֹתָם'), ('אותם', 'אתם', 'אותם'))


class CitationBoundaryTest(QuoteFixtures, unittest.TestCase):
    def book(self, batch_size, intervening=False):
        first = 'מפורש (בראשית מח יג)'
        second = 'וירא ישראל את בני יוסף ויאמר מי אלה (שם שם ח)'
        rows = [(0, first, None)]
        docs = {first: NS(resolved_refs=[citation(first, 'בראשית מח יג', [
            ('NAMED', 'בראשית'), ('NUMBERED', 'מח'), ('NUMBERED', 'יג')], [Verse('Genesis 48:13')])])}
        if intervening:
            bad = 'ועיין בספר שלא זוהה'
            rows.append((1, bad, None))
            docs[bad] = NS(resolved_refs=[citation(bad, 'בספר שלא זוהה', [('NAMED', 'בספר שלא זוהה')], failed=True)])
        rows.append((len(rows), second, None))
        docs[second] = NS(resolved_refs=[citation(second, 'שם שם ח', [
            ('IBID', 'שם'), ('IBID', 'שם'), ('NUMBERED', 'ח')], [Verse('Genesis 48:8'), Verse('Leviticus 11:4')])])
        resolver = NS(_ibid_history=NS(last_refs=[]), reset_ibid_history=lambda: None)
        linker = NS(_ref_resolver=resolver, bulk_link=lambda texts, **_: [docs[t] for t in texts])
        with mock.patch.object(lb, 'BATCH_LINES', batch_size), \
                mock.patch.object(lb, 'clause_names_elsewhere', return_value=False):
            records, _ = lb.process_book(linker, lb.BookKey('Sefaria', 'אגדת בראשית'), rows,
                                         lambda _: None, lambda: None, context_ref_factory=Verse)
        return [(r.line_index, r.target_ref) for r in records]

    def test_recovery_is_identical_across_transport_boundaries(self):
        self.assertEqual(self.book(100), [(0, 'Genesis 48:13'), (1, 'Genesis 48:8')])
        self.assertEqual(self.book(1), self.book(100))

    def test_independent_unique_quotation_can_resolve_after_a_failure(self):
        self.assertEqual(self.book(100, intervening=True), [
            (0, 'Genesis 48:13'), (2, 'Genesis 48:8')])
        self.assertEqual(self.book(1, intervening=True), self.book(100, intervening=True))

    def test_checkpoint_preserves_the_exact_immediate_antecedent(self):
        ctx = lb.IbidContext()
        ref = Verse('Genesis 48:13')
        ctx.previous_citation = (9, 42, True, (), ref)
        key, batch = lb.BookKey('Sefaria', 'אגדת בראשית'), [(9, 'שורה', None)]
        state = ctx.state(key, 0, batch, 'a' * 64)
        restored = lb.IbidContext()
        restored.restore_state(state, Verse, key, 0, batch, 'a' * 64)
        self.assertEqual(restored.previous_citation[:4], (9, 42, True, ()))
        self.assertEqual(restored.previous_citation[4].normal(), ref.normal())
        for bad in ({}, {'line': 9}, {**state['previous_citation'], 'failed': 1},
                    {**state['previous_citation'], 'end': -1},
                    {**state['previous_citation'], 'refs': 'Genesis 48:13'}):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                restored.restore_state({**state, 'previous_citation': bad}, Verse, key, 0, batch, 'a' * 64)
            self.assertEqual(restored.previous_citation[4].normal(), ref.normal())
        without_previous = {k: v for k, v in state.items() if k != 'previous_citation'}
        with self.assertRaises(RuntimeError):
            restored.restore_state(without_previous, Verse, key, 0, batch, 'a' * 64)


class IndependentBiblicalQuoteTest(QuoteFixtures, unittest.TestCase):
    def recover(self, text, anchor, refs=(), failed=False, blocked=False):
        parts = [('IBID' if t == 'שם' else 'NUMBERED', t) for t in anchor.split()]
        rr = citation(text, anchor, parts, [Verse(r) for r in refs], failed)
        with mock.patch.object(lb, 'clause_names_elsewhere', return_value=blocked):
            ref = lb.quoted_biblical_ibid_ref(None, rr, text, *rr.raw_entity.span.range, Verse)
        return None if ref is None else ref.normal()

    def test_a_unique_quote_and_printed_chapter_verse_need_no_history(self):
        text = 'הוות לאביו בן כסיל ודלף טרד מדיני אשה (שם יט יג)'
        self.assertEqual(self.recover(text, 'שם יט יג', failed=True), 'Proverbs 19:13')
        self.assertIsNone(self.recover(text, 'שם יט יג', failed=True, blocked=True))

    def test_a_unique_quote_can_choose_only_a_resolver_supplied_chapter(self):
        text = 'וירא יוסף כי ישית אביו (שם שם יז)'
        self.assertEqual(self.recover(text, 'שם שם יז', ['Genesis 48:17', 'Leviticus 11:17']),
                         'Genesis 48:17')
        self.assertIsNone(self.recover(text, 'שם שם יז', failed=True))
        self.assertIsNone(self.recover(text, 'שם שם יז', ['Genesis 47:17', 'Leviticus 11:17']))

    def test_wrong_numbers_and_short_quotes_never_produce_an_address(self):
        for text, anchor in [
                ('הוות לאביו בן כסיל (שם יט יד)', 'שם יט יד'),
                ('הוות לאביו בן (שם יט יג)', 'שם יט יג'),
                ('הוות לאביו בן כסיל (שם שם יט יג)', 'שם שם יט יג')]:
            with self.subTest(text=text):
                self.assertIsNone(self.recover(text, anchor, failed=True))

    def test_a_phrase_repeated_in_another_book_is_not_unique(self):
        text = 'הוות לאביו בן כסיל (שם יט יג)'
        books = lb._tanakh_quote_books() + (('Other', ' הוות לאביו בן כסיל '),)
        with mock.patch.object(lb, '_tanakh_quote_books', return_value=books):
            self.assertIsNone(self.recover(text, 'שם יט יג', failed=True))

    def test_quotes_of_different_books_on_the_two_sides_are_ambiguous(self):
        text = 'הוות לאביו בן כסיל (שם יט יג) וירא ישראל את בני יוסף'
        self.assertIsNone(self.recover(text, 'שם יט יג', failed=True))

    def test_a_short_two_verse_quote_keeps_its_printed_start_address(self):
        text = VERSES['Proverbs 19:13'] + '. ' + VERSES['Proverbs 19:14'] + '. (שם יט יג)'
        self.assertEqual(self.recover(text, 'שם יט יג', failed=True), 'Proverbs 19:13')
        # The same words behind another citation cannot identify the current one.
        text = VERSES['Proverbs 19:13'] + ' (שם יט יד) פירוש הדברים (שם יט יג)'
        self.assertIsNone(self.recover(text, 'שם יט יג', failed=True))

    def test_only_proven_quoted_tokens_lose_their_title_reading(self):
        words = ['רש"י', 'הוות', 'לאביו', 'בן', 'כסיל', 'כתב', 'בית']
        self.assertEqual(lb.unquoted_clause_words(words, {' הוות לאביו בן כסיל '}),
                         ['רש"י', 'כתב', 'בית'])


class ExplicitListsTest(unittest.TestCase):
    def split(self, text, resolve):
        anchor = 'ויקרא יא ד, דברים יד ז'
        rr = citation(text, anchor, [('NAMED', 'ויקרא'), ('NUMBERED', 'יא'), ('NUMBERED', 'ד'),
                                     ('NAMED', 'דברים'), ('NUMBERED', 'יד'), ('NUMBERED', 'ז')], failed=True)
        module = types.ModuleType('sefaria.model.linker.ref_part')
        module.RawRef = Raw
        linker = NS(_ref_resolver=NS(resolve_raw_ref=resolve))
        with mock.patch.dict(sys.modules, {'sefaria.model.linker.ref_part': module}):
            return lb.split_explicit_verses(linker, rr, text)

    def test_two_explicit_refs_keep_separate_original_offsets(self):
        text = '😀 <b>גמל גמל</b> (ויקרא יא ד, דברים יד ז)'
        seen = []
        def resolve(context, raw):
            self.assertIsNone(context)
            seen.append(raw.span.text)
            target = {'ויקרא יא ד': 'Leviticus 11:4', 'דברים יד ז': 'Deuteronomy 14:7'}[raw.span.text]
            return NS(is_ambiguous=False, ref=Verse(target), context_type=None)
        found = self.split(text, resolve)
        self.assertEqual(seen, ['ויקרא יא ד', 'דברים יד ז'])
        self.assertEqual([r.ref.normal() for r in found], ['Leviticus 11:4', 'Deuteronomy 14:7'])
        self.assertEqual([text[s:e] for s, e in (r.raw_entity.span.range for r in found)], seen)
        self.assertTrue(all(r.text_rebuilt for r in found))

    def test_a_group_with_an_unresolved_or_contradicted_address_rejects_the_split(self):
        for target in ('Genesis 48:8', None):
            def resolve(_context, _raw):
                return None if target is None else NS(is_ambiguous=False, ref=Verse(target), context_type=None)
            self.assertEqual(self.split('(ויקרא יא ד, דברים יד ז)', resolve), ())

    def test_lists_use_the_normal_record_path_and_keep_legacy_history_unchanged(self):
        text = '😀 <b>גמל גמל</b> (ויקרא יא ד, דברים יד ז)'
        anchor = 'ויקרא יא ד, דברים יד ז'
        rr = citation(text, anchor, [('NAMED', 'ויקרא'), ('NUMBERED', 'יא'), ('NUMBERED', 'ד'),
                                    ('NAMED', 'דברים'), ('NUMBERED', 'יד'), ('NUMBERED', 'ז')], failed=True)
        def resolve(_context, raw):
            target = {'ויקרא יא ד': 'Leviticus 11:4', 'דברים יד ז': 'Deuteronomy 14:7'}[raw.span.text]
            return NS(is_ambiguous=False, ref=Verse(target), context_type=None)
        module = types.ModuleType('sefaria.model.linker.ref_part')
        module.RawRef = Raw
        linker = NS(_ref_resolver=NS(resolve_raw_ref=resolve),
                    bulk_link=lambda *_args, **_kw: [NS(resolved_refs=[rr])])
        previous = Verse('Genesis 48:13')
        history = NS(last_emitted_ref=previous, previous_citation=None, resolve=lambda _l, f: f())
        with mock.patch.dict(sys.modules, {'sefaria.model.linker.ref_part': module}):
            records, _ = lb.process_batch(linker, lb.BookKey('test', 'test'), [(4, text, None)],
                                           lambda _: None, context_ref_factory=Verse, ibid_context=history)
        self.assertEqual(len(records), 2)
        for record, label in zip(records, ['ויקרא יא ד', 'דברים יד ז']):
            s = text.index(label)
            self.assertEqual(record.start, len(text[:s].encode('utf-16-le')) // 2)
            self.assertEqual(record.end, len(text[:s + len(label)].encode('utf-16-le')) // 2)
            self.assertEqual(record.source_hash, lb.content_hash(text))
        self.assertIs(history.last_emitted_ref, previous)
        self.assertEqual(history.previous_citation[4].normal(), 'Deuteronomy 14:7')


class BiblicalCorpusTest(unittest.TestCase):
    def corpus(self, count, empty=False):
        indexes = [NS(title=str(i), categories=['Tanakh', 'Torah']) for i in range(count)]
        indexes.append(NS(title='ancillary complex work', categories=['Tanakh', 'Masorah']))
        model = types.ModuleType('sefaria.model')
        model.library = NS(get_indexes_in_category=lambda *a, **kw: indexes)
        lb._tanakh_quote_books.cache_clear()
        self.addCleanup(lb._tanakh_quote_books.cache_clear)
        def text(title):
            self.assertNotEqual(title, 'ancillary complex work')
            return '' if empty and title == '0' else ' quoted text '
        with mock.patch.dict(sys.modules, {'sefaria.model': model}), \
                mock.patch.object(lb, '_hebrew_book_quote_text', side_effect=text):
            return lb._tanakh_quote_books()

    def test_global_evidence_requires_every_base_book_and_excludes_ancillary_works(self):
        self.assertEqual(len(self.corpus(39)), 39)
        self.assertEqual(self.corpus(38), ())
        self.assertEqual(self.corpus(39, empty=True), ())


class NamedCommentaryTest(unittest.TestCase):
    def test_a_named_rashi_target_is_never_replaced_by_gemara(self):
        text = 'רש"י (דף כא.)'
        rr = _candidates('Rashi on Gittin 21a')
        rr.raw_entity = NS(raw_ref_parts=[NS(type=NS(name='NAMED'), text='רש"י'),
                                          NS(type=NS(name='NUMBERED'), text='דף כא.')])
        self.assertIsNone(lb.own_tractate_daf_ref(
            rr, text, 0, len(text), TalmudRef('Rashi on Gittin 18a:1:1'), TalmudRef))

    def test_own_tractate_ibid_guard_is_independent_of_batch_size(self):
        rows = [(0, 'כדאיתא (דף ג.) בענין זה', 'Tosafot on Shabbat 18a:1:1'),
                (1, 'וכן אמרו שם (דף כא.) בענין זה', 'Tosafot on Shabbat 18a:1:1')]
        def run(size):
            docs = {}
            for li, text, _ctx in rows:
                anchor = 'דף ג.' if li == 0 else 'שם (דף כא.'
                rr = _candidates('Shabbat 3a' if li == 0 else 'Shabbat 21a',
                                 'Tosafot on Shabbat 3a' if li == 0 else 'Tosafot on Shabbat 21a')
                rr.resolution_failed, rr.context_type = False, None
                at = text.index(anchor)
                rr.raw_entity = NS(span=NS(range=(at, at + len(anchor))), raw_ref_parts=[
                    NS(type=NS(name=p)) for p in (['NUMBERED'] if li == 0 else ['IBID', 'NUMBERED'])])
                docs[text] = NS(resolved_refs=[rr])
            linker = NS(_ref_resolver=NS(_ibid_history=NS(last_refs=[]), reset_ibid_history=lambda: None),
                        bulk_link=lambda texts, **_: [docs[t] for t in texts])
            with mock.patch.multiple(lb, BATCH_LINES=size, clause_names_elsewhere=lambda *_: False):
                records, _ = lb.process_book(linker, lb.BookKey('Sefaria', 'תוספות על שבת'), rows,
                                             lambda _: None, lambda: None, context_ref_factory=TalmudRef)
            return [(r.line_index, r.target_ref) for r in records]
        self.assertEqual(run(100), [(0, 'Shabbat 3a'), (1, 'Shabbat 21a')])
        self.assertEqual(run(1), run(100))


if __name__ == '__main__':
    unittest.main()
