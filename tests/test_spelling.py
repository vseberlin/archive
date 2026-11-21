from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from dataclasses import asdict
import hashlib
import json

import test_research_evidence as fixtures
from archive_research import Source
from archive_research.runner import extract_one
from archive_research.receipts import read_current
from archive_research.search_projection import build_projection,query

from archive_research.extractors.spelling import Lexicon,correct


def word(text,line=0,block=0):
    return {'text':text,'block_index':block,'line_index':line,
            'region':{'x':.1,'y':.1+line*.1,'width':.1,'height':.05,'units':'normalized','origin':'top-left'}}


class SpellingTests(TestCase):
    def lex(self,*terms,names=()):return Lexicon([(t,100) for t in terms],{'names':list(names)})

    def test_unique_one_edit_preserves_case_punctuation_and_original(self):
        words=[word('Aufnahne,'),word('aufnahme')];before=deepcopy(words)
        result=correct(words,self.lex('aufnahme'))
        self.assertEqual(words,before)
        self.assertEqual(result['corrected_text'],'Aufnahme, aufnahme')
        self.assertEqual(result['corrected_tokens'][0]['status'],'auto_distance_1')
        self.assertFalse(result['literal_ocr_replaced'])

    def test_frequency_never_hides_a_second_candidate(self):
        lex=Lexicon([('haus',100000),('maus',1)],{})
        result=correct([word('laus')],lex)['corrected_tokens'][0]
        self.assertEqual(result['status'],'review');self.assertEqual(result['text'],'laus')
        self.assertEqual(len(result['candidates']),2)

    def test_distance_two_and_no_candidate_remain_review(self):
        for term in ('huusx','zzzzzz'):
            result=correct([word(term)],self.lex('haus'))
            self.assertEqual(result['review_items'],[0]);self.assertEqual(result['corrected_text'],term)

    def test_join_then_correct_retains_both_boxes(self):
        words=[word('Auf-'),word('nahne',1)]
        result=correct(words,self.lex('aufnahme'));unit=result['corrected_tokens'][0]
        self.assertEqual(result['corrected_text'],'Aufnahme')
        self.assertEqual(unit['raw_indices'],[0,1]);self.assertEqual(unit['regions'],[w['region'] for w in words])
        self.assertTrue(unit['joined_line_hyphen'])

    def test_do_not_join_columns_same_line_missing_layout_or_ellipsis(self):
        variants=[[word('Leucht-'),word('schirm')],
                  [word('Leucht-'),word('schirm',1,1)],
                  [word('Leucht-'),word('und',1)],
                  [{'text':'Leucht-','region':word('')['region']},word('schirm',1)]]
        for words in variants:
            self.assertEqual(len(correct(words,self.lex('leuchtschirm'))['corrected_tokens']),2)

    def test_exact_compound_parts_preserve_word_without_inserting_spaces(self):
        unit=correct([word('Kathodenentwicklung')],self.lex('kathoden','entwicklung'))['corrected_tokens'][0]
        self.assertEqual(unit['status'],'compound_preserved');self.assertEqual(unit['text'],'Kathodenentwicklung')
        self.assertEqual(unit['compound_parts'],['kathoden','entwicklung'])

    def test_names_units_acronyms_and_formulas_protected(self):
        words=[word(t) for t in ('Müller','VEB','WF','lx','Sb2S3','B43','ABCD','Schöneweide')]
        result=correct(words,self.lex('müllers','abend','schöneweide',names=['Müller']))
        self.assertTrue(all(t['status']=='protected' for t in result['corrected_tokens']))
        target=correct([word('Müllerx')],self.lex(names=['Müller']))['corrected_tokens'][0]
        self.assertEqual(target['status'],'review')

    def test_protected_region_blocks_even_plausible_word_correction(self):
        w=word('Aufnahne');result=correct([w],self.lex('aufnahme'),[w['region']])
        self.assertEqual(result['corrected_text'],'Aufnahne')
        # Aligned character OCR can protect the actual formula word without
        # protecting a neighbouring word touched only by crop padding.
        result=correct([word('SbS'),word('Aufnahne')],self.lex('aufnahme'),protected_word_indices=[0])
        self.assertEqual(result['corrected_text'],'SbS Aufnahme')

    def test_known_words_and_capitalization_not_rewritten(self):
        result=correct([word('Leiter'),word('leiter')],self.lex('leiter','leiser'))
        self.assertEqual(result['corrected_text'],'Leiter leiter')

    def test_custom_technical_vocabulary_can_supply_unique_candidate(self):
        lex=Lexicon([],{'terms':['Rieselikonoskop']})
        result=correct([word('Rieselikonoskof')],lex)
        self.assertEqual(result['corrected_text'],'Rieselikonoskop')

    def test_ocr_long_dash_joins_before_correction_and_unjoined_fragment_is_review(self):
        lex=self.lex('umwandlung','umland')
        joined=correct([word('Umwand—'),word('lung',1)],lex)
        self.assertEqual(joined['corrected_text'],'Umwandlung')
        fragment=correct([word('Umwand—')],lex)['corrected_tokens'][0]
        self.assertEqual(fragment['text'],'Umwand—');self.assertEqual(fragment['status'],'review')
        parenthesized=correct([word('(Kohle-'),word('abdruck),',1)],self.lex('kohleabdruck'))
        self.assertEqual(parenthesized['corrected_text'],'(Kohleabdruck),')
        self.assertEqual(len(parenthesized['corrected_tokens'][0]['regions']),2)

    def test_checked_technical_spelling_overrides_general_dictionary_gap(self):
        lex=Lexicon([('gedämpft',100)],{'terms':['gedampft']})
        self.assertEqual(correct([word('gedampft')],lex)['corrected_text'],'gedampft')


class SpellingIntegrationTests(TestCase):
    setUp=fixtures.EvidenceTests.setUp
    receipt=fixtures.EvidenceTests.receipt
    item=fixtures.EvidenceTests.item

    def test_bound_sidecar_cache_and_search_preserve_raw_and_both_boxes(self):
        key=self.receipt('ocr','spell-parent',['Auf-','nahne'])
        parent=self.output/(key+'.json');row=json.loads(parent.read_text())
        for i,w in enumerate(row['words']):w.update(block_index=0,line_index=i)
        parent.write_text(json.dumps(row));original=parent.read_bytes()
        _,_,digest=read_current(self.roots,'artifacts',key)
        def bound(name,content):
            p=self.input/name;p.write_text(content);st=p.stat()
            return asdict(Source('synthetic','dictionary',name,'source',name,st.st_size,st.st_mtime_ns,hashlib.sha256(p.read_bytes()).hexdigest()))
        task={'id':'spell','extractor':'spelling','page':asdict(self.page),'settings':{
            'ocr_origin':{'key':key,'receipt_sha256':digest},
            'dictionary':bound('de.txt','aufnahme 100\n'),
            'custom_lexicon':bound('custom.json','{"terms":[],"names":[]}')}}
        first=extract_one(self.roots,'artifacts',task)
        self.assertEqual(first['state'],'partial')
        result=json.loads((self.output/(first['key']+'.json')).read_text())
        self.assertEqual(result['words'],row['words']);self.assertEqual(parent.read_bytes(),original)
        self.assertEqual(result['feature']['corrected_text'],'Aufnahme')
        self.assertEqual(len(result['feature']['corrected_tokens'][0]['regions']),2)
        with patch('archive_research.extractors.spelling.extract',side_effect=AssertionError('cache expected')):
            self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])
        projection=build_projection(self.roots,'artifacts',{'keys':[first['key']]})
        hits=query(projection,'Aufnahme')['hits'];self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]['evidence']['spelling']['corrected_tokens'][0]['raw_texts'],['Auf-','nahne'])
        (self.input/'de.txt').write_text('different 10\n')
        self.assertEqual(extract_one(self.roots,'artifacts',task)['state'],'changed_source')
