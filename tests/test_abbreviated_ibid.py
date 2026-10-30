"""Otzaria#2695: short quotations need stronger evidence than remembered ibid."""
import unittest
from unittest import mock
from test_recovery_evidence import lb, citation, Verse, QuoteFixtures, VERSES, BOOKS


class AbbreviatedIbidTest(QuoteFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        verses = {
            'Genesis 48:5': 'ועתה שני בניך הנולדים לך בארץ מצרים',
            'Genesis 48:14': 'וישלח ישראל את ימינו וישת על ראש אפרים',
            'Genesis 48:19': 'וימאן אביו ויאמר ידעתי בני ידעתי',
        }
        mock.patch.dict(VERSES, verses).start()
        mock.patch.dict(BOOKS, {'Genesis': BOOKS['Genesis'] + ' ' + ' '.join(verses.values())}).start()
        self.guard = mock.patch.object(lb, 'clause_names_elsewhere', return_value=False).start()

    def recover(self, text, numeral='ה', refs=('Genesis 48:5',), **kw):
        anchor = 'שם שם ' + numeral
        rr = citation(text, anchor, [('IBID', 'שם'), ('IBID', 'שם'), ('NUMBERED', numeral)],
                      refs=[Verse(r) for r in refs])
        ref = lb.abbreviated_biblical_ibid_ref(None, rr, text, *rr.raw_entity.span.range, Verse)
        return None if ref is None else ref.normal()

    def test_the_three_missing_short_quotes_have_unique_candidates(self):
        for text, number, target in [
                ("שנאמר ועתה שני בניך וגו' (שם שם ה)", 'ה', 'Genesis 48:5'),
                ("וישלח ישראל את יד ימינו וגו׳ (שם שם יד)", 'יד', 'Genesis 48:14'),
                ("אם אבקש, וימאן אביו וגומר (שם שם יט)", 'יט', 'Genesis 48:19')]:
            with self.subTest(text=text):
                self.assertEqual(self.recover(text, number, (target, 'Proverbs 19:13')), target)

    def test_no_invented_chapter_or_changed_verse(self):
        text = "ועתה שני בניך וגו' (שם שם ה)"
        for refs in ((), ('Genesis 1:5',), ('Proverbs 19:13',)):
            self.assertIsNone(self.recover(text, refs=refs))
        self.assertIsNone(self.recover("ועתה שני בניך וגו' (שם שם יד)", 'יד', ('Genesis 48:14',)))

    def test_entire_phrase_and_explicit_ellipsis_are_required(self):
        for quote in ('ועתה שני בניך', 'אינו ועתה שני בניך וגו׳',
                      'כבר אמרו ועתה שני בניך וגו׳',
                      'וישלח ישראל לא את יד ימינו וגו׳'):
            self.assertIsNone(self.recover(quote + ' (שם שם ה)'))

    def test_repeated_short_quote_and_author_attribution_fail_closed(self):
        text = "וימאן אביו וגו' (שם שם יט)"
        books = lb._tanakh_quote_books() + (('Other', ' וימאן אביו '),)
        with mock.patch.object(lb, '_tanakh_quote_books', return_value=books):
            self.assertIsNone(self.recover(text, 'יט', ('Genesis 48:19',)))
        self.guard.return_value = True
        self.assertIsNone(self.recover('רש״י כתב ' + text, 'יט', ('Genesis 48:19',)))

    def test_html_attributes_are_not_quotation_evidence(self):
        self.assertIsNone(self.recover('<span title="ועתה שני בניך >">אחר וגו׳</span> (שם שם ה)'))
        self.assertEqual(self.recover('<b>ועתה שני בניך</b> וגו׳ (שם שם ה)'), 'Genesis 48:5')


class FullyAddressedShortIbidTest(QuoteFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        verse = 'וימאן אביו ויאמר ידעתי בני ידעתי'
        mock.patch.dict(VERSES, {'Genesis 48:19': verse}).start()
        mock.patch.dict(BOOKS, {'Genesis': BOOKS['Genesis'] + ' ' + verse}).start()
        mock.patch.object(lb, 'clause_names_elsewhere', return_value=False).start()
    def test_explicit_chapter_and_verse_retain_the_same_global_quote_requirement(self):
        text = "שנא׳ וימאן אביו וגו׳ (שם מח יט)"
        rr = citation(text, 'שם מח יט', [('IBID','שם'),('NUMBERED','מח'),('NUMBERED','יט')],
                      refs=[Verse('Genesis 48:19')])
        ref = lb.abbreviated_biblical_ibid_ref(None,rr,text,*rr.raw_entity.span.range,Verse)
        self.assertEqual(ref.normal(),'Genesis 48:19')
        text = text.replace('מח','א')
        rr = citation(text,'שם א יט',[('IBID','שם'),('NUMBERED','א'),('NUMBERED','יט')],
                      refs=[Verse('Genesis 48:19')])
        self.assertIsNone(lb.abbreviated_biblical_ibid_ref(None,rr,text,*rr.raw_entity.span.range,Verse))

class SuccessfulIbidProofTest(QuoteFixtures, unittest.TestCase):
    def test_single_resolved_candidates_need_the_same_independent_proof(self):
        for verse,expected in [('ח','Genesis 48:8'),('יב',None)]:
            text='וירא ישראל את בני יוסף ויאמר מי אלה (שם שם '+verse+')'
            rr=citation(text,'שם שם '+verse,[('IBID','שם'),('IBID','שם'),('NUMBERED',verse)],
                        refs=[Verse('Genesis 48:'+('8' if verse=='ח' else '12'))])
            from types import SimpleNamespace as NS
            linker=NS(bulk_link=lambda *_a,**_k:[NS(resolved_refs=[rr])])
            with mock.patch.object(lb,'clause_names_elsewhere',return_value=False):
                records,_=lb.process_batch(linker,lb.BookKey('Sefaria','אגדת בראשית'),[(0,text,None)],
                                           lambda _:None,context_ref_factory=Verse)
            self.assertEqual([r.target_ref for r in records],[] if expected is None else [expected])

class ExplicitVerseLabelTest(QuoteFixtures, unittest.TestCase):
    def test_psuq_is_an_address_label_not_an_extra_number(self):
        text='ויקח יוסף את שניהם (שם פסוק יג)'
        rr=citation(text,'שם פסוק יג',[('IBID','שם'),('NUMBERED','פסוק יג')],
                    refs=[Verse('Genesis 48:13')])
        with mock.patch.object(lb,'clause_names_elsewhere',return_value=False):
            ref=lb.quoted_biblical_ibid_ref(None,rr,text,*rr.raw_entity.span.range,Verse)
            self.assertEqual(ref.normal(),'Genesis 48:13')
            text='ויקח יוסף את שניהם (שם פסוק מח יג)'
            rr=citation(text,'שם פסוק מח יג',[('IBID','שם'),('NUMBERED','פסוק מח'),('NUMBERED','יג')],
                        refs=[Verse('Genesis 48:13')])
            self.assertIsNone(lb.quoted_biblical_ibid_ref(None,rr,text,*rr.raw_entity.span.range,Verse))
