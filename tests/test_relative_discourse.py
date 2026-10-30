"""Work identity for explicit relative daf references in Tosafot (#3450)."""
import sys
import types
import unittest
from unittest import mock
from test_recovery_evidence import lb, citation, TalmudRef, NS

LEAF = object()


class RelativeDiscourseTest(unittest.TestCase):
    def scope(self, before, *, after=')', named=None):
        anchor = 'לקמן (דף ח.'
        text = before + anchor + after
        rr = citation(text, anchor, [('RELATIVE', 'לקמן'), ('NUMBERED', 'דף ח.')], failed=True)
        def continuation(phrase, **_):
            if phrase != named:
                return None, None
            if phrase == 'רש"י':
                index = NS(title='Rashi on Shabbat', dependence='Commentary', base_text_titles=['Shabbat'])
            else:
                index = NS(title='Gittin', categories=['Talmud'], base_text_titles=[],
                           get_title=lambda _: 'גיטין')
            return {LEAF: [NS(ref=lambda: NS(index=index, sections=[]))]}, None
        linker = NS(_ref_resolver=NS(get_ref_part_title_trie=lambda: NS(get_continuations=continuation)))
        mod = types.ModuleType('sefaria.model.linker.ref_part');mod.LEAF_TRIE_ENTRY = LEAF
        with mock.patch.dict(sys.modules, {'sefaria.model.linker.ref_part': mod}), \
                mock.patch.object(lb, 'clause_names_elsewhere', return_value=True):
            return lb.relative_clause_is_clear(linker, rr, text, *rr.raw_entity.span.range,
                                               TalmudRef('Tosafot on Shabbat 3a:1:1'), TalmudRef('Shabbat 8a'))

    def test_a_prior_rashi_explanation_can_quote_a_separate_gemara_argument(self):
        self.assertTrue(self.scope('פירש בקונטרס אלא דרשא גופה פריך '))
        self.assertTrue(self.scope('פירש רש"י בענין אחר והא אמרינן ', named='רש"י'))
        self.assertTrue(self.scope('וא"ת אע"פ דכה"ג אמר '))

    def test_governing_commentary_and_self_references_stay_out_of_gemara(self):
        for before in ('בקונטרס גופיה פירש ', 'פירשתי ', 'ועיין בר"ן ',
                       'רש"י אמר ', 'עיין ע"י ', 'הגאון ר"ע '):
            with self.subTest(before=before):
                self.assertFalse(self.scope(before, named='רש"י'))

    def test_an_explicit_other_tractate_is_not_erased_by_a_later_gemara_verb(self):
        self.assertFalse(self.scope('בגיטין הא אמרינן ', named='בגיטין'))

    def test_a_following_dh_never_selects_the_base_gemara(self):
        self.assertFalse(self.scope('כדמוכח ', after=') [בד"ה בדרבנן וכו\']'))

    def test_rabbenu_tam_subordinate_explanation_is_distinct_from_direct_self_citation(self):
        # The subordinate ד belongs to the NER anchor in this form.
        self.assertFalse(self.scope('ור"ת מפרש '))


if __name__ == '__main__':
    unittest.main()

class RelativeDhTest(unittest.TestCase):
    def recover(self, texts, before='כדמוכח ', after=') [בד"ה בדרבנן וכו\']', candidates=True):
        anchor='לעיל (דף ח.';text=before+anchor+after
        source=TalmudRef('Tosafot on Shabbat 18a:1:1');target=TalmudRef('Shabbat 8a')
        refs=[TalmudRef('Tosafot on Shabbat 8a')] if candidates else [target]
        rr=citation(text,anchor,[('RELATIVE','לעיל'),('NUMBERED','דף ח.')],refs=refs)
        segments=[NS(normal=lambda i=i: f'Tosafot on Shabbat 8a:{i}:1',
                     text=lambda _lang,t=t: NS(as_string=lambda:t)) for i,t in enumerate(texts,1)]
        with mock.patch.object(lb,'clause_names_elsewhere',return_value=False):
            found=lb.own_relative_dh_ref(None,rr,text,*rr.raw_entity.span.range,source,target,'above',
                                       lambda _: NS(all_segment_refs=lambda:segments))
        return None if found is None else found[0].normal()

    def test_unique_plain_and_bold_headings_select_the_commentary_segment(self):
        for text in ('בדרבנן תקנתא הוא. פירוש שצריך לתקן שבועה',
                     '<b>בדרבנן תקנתא הוא</b> פירוש שצריך לתקן שבועה'):
            self.assertEqual(self.recover(['דבר אחר. בדרבנן', text]),'Tosafot on Shabbat 8a:2:1')

    def test_body_occurrences_ambiguous_headings_and_missing_candidate_fail_closed(self):
        self.assertIsNone(self.recover(['ענין אחר. <b>בדרבנן</b> אינו דיבור המתחיל']))
        self.assertIsNone(self.recover(['בדרבנן תקנתא. א','בדרבנן נמי. ב']))
        self.assertIsNone(self.recover(['בדרבנן תקנתא. א'],candidates=False))
        self.assertIsNone(self.recover(['בדרבנן תקנתא. א'],after=') [בד"ה בדרבנן]'))

class ResolvedRelativeOwnershipTest(unittest.TestCase):
    def records(self, before, refs):
        anchor='לקמן (דף ח.';text=before+anchor+')'
        rr=citation(text,anchor,[('RELATIVE','לקמן'),('NUMBERED','דף ח.')],
                    refs=[TalmudRef(r) for r in refs],failed=not refs)
        linker=NS(bulk_link=lambda *_a,**_k:[NS(resolved_refs=[rr])],
                  _ref_resolver=NS(get_ref_part_title_trie=lambda:NS(
                      get_continuations=lambda *a,**kw:(None,None))))
        mod=types.ModuleType('sefaria.model.linker.ref_part');mod.LEAF_TRIE_ENTRY=LEAF
        with mock.patch.dict(sys.modules,{'sefaria.model.linker.ref_part':mod}):
            return lb.process_batch(linker,lb.BookKey('Sefaria','תוספות על שבת'),
                                    [(4,text,'Tosafot on Shabbat 3a:1:1')],lambda _:None,
                                    context_ref_factory=TalmudRef)[0]

    def test_successful_ambiguous_and_failed_guesses_use_the_same_ownership_guard(self):
        for refs in (['Shabbat 8a'],['Shabbat 8a','Tosafot on Shabbat 8a'],[]):
            with self.subTest(refs=refs):
                self.assertEqual(self.records('וכמו שכתב הרא"ש ז"ל ',refs),[])
                self.assertEqual([r.target_ref for r in self.records('כדאמרינן ',refs)],['Shabbat 8a'])
