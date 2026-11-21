"""CTC alignment arithmetic, soft vocabulary and bounded cursive candidates."""
import itertools
import math
import unittest
from dataclasses import asdict
from unittest.mock import patch
import numpy as np
from archive_research.extractors.handwriting import ctc_decode
from archive_research.extractors.handwriting_decode import (
    DEFAULTS,Lexicon,ctc_scores,prefix_beam,decode,log_probabilities,candidates_from_confusions)


class DecodingTests(unittest.TestCase):
    def test_forward_and_prefix_mass_match_exhaustive_paths_including_repeats(self):
        probs=np.array([[.2,.5,.3],[.4,.4,.2],[.2,.3,.5],[.6,.2,.2]])
        expected={}
        for path in itertools.product(range(3),repeat=4):
            text=ctc_decode(path,'ab');prob=math.prod(probs[t,c] for t,c in enumerate(path))
            expected[text]=expected.get(text,0)+prob
        actual=dict(zip(expected,ctc_scores(np.log(probs),list(expected),'ab')))
        beam={r['text']:r['search_log_probability'] for r in prefix_beam(np.log(probs),'ab',width=200,token_limit=3)}
        for text,prob in expected.items():
            self.assertAlmostEqual(math.exp(actual[text]),prob,places=12)
            self.assertAlmostEqual(math.exp(beam[text]),prob,places=12)
        self.assertAlmostEqual(sum(map(math.exp,actual.values())),1.)

    def test_beam_sums_paths_instead_of_choosing_greedy_alignment(self):
        result=decode(np.log([[.4,.35,.25],[.4,.35,.25]]),'ab',DEFAULTS)
        self.assertEqual(result['raw_text'],'')
        self.assertEqual(result['text'],'a')
        self.assertFalse(result['score_is_calibrated_confidence'])

    def test_lexicon_soft_preference_does_not_exclude_unknown_names(self):
        lex=Lexicon([('Ab',100)],[])
        logits=np.full((3,4),-30.);logits[0,1]=logits[1,2]=logits[2,3]=0.
        result=decode(logits,'Zyx',{**DEFAULTS,'mode':'lexicon'},lex)
        self.assertEqual(result['text'],'Zyx')
        self.assertEqual(lex.word_score('Zyx'),0)

    def test_multichar_diacritic_and_boundary_candidates(self):
        lex=Lexicon([(w,10) for w in ['Mann','und','für','da','Straße','Brigade','Buch','Brigadebuch']],[])
        for raw,expected in [('Mam','Mann'),('uud','und'),('fur','für'),('cla','da'),('Strafse','Straße'),('StraBe','Straße')]:
            self.assertIn(expected,[v[0] for v in lex.variants(raw)],raw)
        self.assertIn('Brigade Buch',[v[0] for v in lex.variants('BrigadeBuch')])
        candidates=candidates_from_confusions(['Brigade buch'],lex,64)
        self.assertIn('Brigadebuch',[v[0] for v in candidates])
        self.assertIn('Brigade buch',[v[0] for v in candidates])
        self.assertLessEqual(len(candidates_from_confusions(['Mamn uud fur cla Strafse'],lex,16)),16)

    def test_invalid_scores_and_impossible_strings(self):
        for logits,alphabet in [(np.zeros((2,3)),'aa'),(np.zeros((2049,2)),'a'),([[0,float('nan')]],'a')]:
            with self.assertRaises(ValueError):log_probabilities(logits,alphabet)
        result=ctc_scores(np.log([[.5,.5]]),['aa','z',''],'a')
        self.assertEqual(result[:2],[-math.inf,-math.inf])
        self.assertAlmostEqual(result[2],math.log(.5))


class BoundDecodingTests(unittest.TestCase):
    from test_handwriting import HandwritingTests
    setUp=HandwritingTests.setUp
    bind=HandwritingTests.bind
    prepare=HandwritingTests.prepare

    def test_retained_scores_round_trip_and_parent_is_not_overwritten(self):
        from archive_research.runner import extract_one
        from archive_research.receipts import read_current
        from archive_research.extractors.document_layout import downstream_plan
        from archive_research.extractors.handwriting import recipe_for
        from archive_research.extractors.handwriting_decode import task_for
        from types import SimpleNamespace
        page,annotation,layout=self.prepare()
        region=extract_one(self.roots,'artifacts',layout)
        task=downstream_plan(self.roots,'artifacts',region['key'],profile='brigadebuch',
                             handwriting={'backend':'tesseract-control'})['tasks'][1]
        manifest=self.input/'model.json';manifest.write_text('{}')
        task['settings'].update(backend='dehtr-v2',model_manifest=asdict(self.bind(manifest)))
        config={'alphabet':'ab','format_version':2,'image_height':64,'max_width':1024,
                'input_channels':1,'blank_index':0,'input_name':'input','output_name':'output'}
        model=SimpleNamespace(run=lambda *args:[np.log(np.array([[[.4,.35,.25],[.4,.35,.25]]],dtype=np.float32))])
        with patch('archive_research.extractors.handwriting.model_files',return_value={}), \
             patch('archive_research.extractors.handwriting.load_model',return_value=(model,config)):
            old_recipe=recipe_for(task)
            task['settings']['retain_scores']=True
            self.assertNotEqual(old_recipe.cache_key(page,None),recipe_for(task).cache_key(page,None))
            parent=extract_one(self.roots,'artifacts',task)
        self.assertEqual(parent['state'],'partial',parent)
        parent_path=self.output/(parent['key']+'.json');before=parent_path.read_bytes()
        task=task_for(self.roots,'artifacts',parent['key'],'beam')
        result=extract_one(self.roots,'artifacts',task)
        self.assertEqual(result['state'],'partial',result)
        row,_,_=read_current(self.roots,'artifacts',result['key'])
        self.assertEqual(row['feature']['lines'][0]['raw_text'],'')
        self.assertEqual(row['feature']['lines'][0]['text'],'a')
        self.assertEqual(parent_path.read_bytes(),before)
        self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])
        # A forged parent binding cannot be accepted or silently re-decoded.
        task['settings']['reading_origin']['receipt_sha256']='f'*64
        failed=extract_one(self.roots,'artifacts',task)
        self.assertEqual(failed['state'],'error',failed)
