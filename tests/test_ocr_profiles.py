"""Material preset boundaries and real positioned extraction, without archive inputs."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from archive_research.profiles import ocr_profile, resolve_ocr_task
from archive_research.runner import extract_one, recipe_for, _recipe_for, load_plan
import test_positioned_extraction as positioned


class OCRProfileTests(unittest.TestCase):
    def test_profile_is_detached_and_does_not_claim_accuracy(self):
        result=ocr_profile('rd_reports');result['checks'].clear()
        self.assertTrue(ocr_profile('rd_reports')['checks'])
        self.assertFalse(result['accuracy_qualified'])

    def test_defaults_and_source_binding_are_not_in_place_edits(self):
        task={'extractor':'ocr','page':{'source':'unchanged'},'region':{'x':.1},
              'ocr_profile':{'name':'rd_reports','version':1}}
        before=deepcopy(task);resolved=resolve_ocr_task(task)
        self.assertEqual(task,before)
        self.assertEqual(resolved['settings']['psm'],6)
        self.assertEqual(resolved['settings']['image_preparation'],'gray-v1')
        self.assertEqual(resolved['page'],before['page'])
        self.assertEqual(resolve_ocr_task(resolved),resolved)

    def test_unknown_versions_experiments_and_deferred_engines_are_rejected(self):
        task={'extractor':'ocr','ocr_profile':{'name':'rd_reports','version':1}}
        variants=[{'ocr_profile':{'name':'unknown','version':1}},
                  {'ocr_profile':{'name':'rd_reports','version':2}},
                  {'extractor':'scientific-region'}, {'extractor':'spelling'},
                  {'settings':{'psm':11}}, {'settings':{'dpi':400}},
                  {'settings':{'image_preparation':'gray-up2-v1'}},
                  {'settings':{'thresholding_method':2}}]
        for change in variants:
            with self.subTest(change=change),self.assertRaises(ValueError):
                resolve_ocr_task({**task,**change})
        with self.assertRaisesRegex(ValueError,'reading-order'):
            resolve_ocr_task({**task,'extractor':'document-text'})

    def test_assembly_refuses_ocr_from_a_different_material_profile(self):
        from archive_research.extractors.document_text import inputs
        box={'x':.1,'y':.1,'width':.5,'height':.2}
        layout={'feature':{'blocks':[{'index':0,'label':'plain text','region':box,'pixel_box':[10,10,50,20],
                                     'ocr_eligible':True,'score':.9}]}}
        lr=SimpleNamespace(recipe=SimpleNamespace(extractor='document-layout',record=lambda:{'parameters':{'settings':{'normalization':'local-red-v1'}}}))
        reading=SimpleNamespace(recipe=SimpleNamespace(extractor='ocr',record=lambda:{'parameters':{
            'ocr_profile':{'name':'historical_newspaper','version':1}}}))
        settings={'layout_origin':{'receipt_sha256':'a'*64},'block_order':[0],'ocr_origins':[{}]}
        with patch('archive_research.extractors.document_text.read_origin',side_effect=[(layout,lr,'a'*64),({},reading,'b'*64)]):
            with self.assertRaisesRegex(ValueError,'selected material profile'):
                inputs(None,None,None,settings,{'name':'rd_reports','version':1})


class OCRProfileExtractionTests(unittest.TestCase):
    setUp=positioned.PositionedTests.setUp
    bind=positioned.PositionedTests.bind
    pdf=positioned.PositionedTests.pdf

    def test_native_text_and_profile_ocr_keep_source_coordinates_and_reuse(self):
        from archive_research.contracts import Region
        page=self.pdf();base={'id':'clean','extractor':'ocr','page':asdict(page),'settings':{'languages':'eng'}}
        self.assertEqual(recipe_for(base),_recipe_for(base))
        clean={**base,'ocr_profile':{'name':'clean_print','version':1}}
        native=extract_one(self.roots,'artifacts',{**clean,'extractor':'pdf-text'})
        result=extract_one(self.roots,'artifacts',clean)
        self.assertEqual(result['state'],'complete',result)
        self.assertTrue(extract_one(self.roots,'artifacts',clean)['reused'])
        original=json.loads((self.output/(native['key']+'.json')).read_text())
        recognized=json.loads((self.output/(result['key']+'.json')).read_text())
        self.assertFalse(recognized['result']['recipe']['parameters']['ocr_profile']['accuracy_qualified'])
        word=next(w for w in recognized['words'] if w['text']=='ALPHA')
        reference=next(w for w in original['words'] if w['text']=='ALPHA')
        for key in ('x','y','width','height'):
            self.assertAlmostEqual(word['region'][key],reference['region'][key],delta=.025)
        crop={**base,'id':'caption','region':asdict(Region(.01,.01,.98,.98)),
              'ocr_profile':{'name':'rd_reports','version':1}}
        rd=extract_one(self.roots,'artifacts',crop)
        self.assertEqual(rd['state'],'complete',rd)
        self.assertTrue(extract_one(self.roots,'artifacts',crop)['reused'])
        row=json.loads((self.output/(rd['key']+'.json')).read_text())
        self.assertIn('ALPHA',[w['text'] for w in row['words']])
        self.assertNotEqual(recipe_for(crop).cache_key(page,Region(**crop['region'])),
            recipe_for({**crop,'ocr_profile':{'name':'historical_newspaper','version':1}}).cache_key(page,Region(**crop['region'])))

    def test_invalid_profile_plan_fails_before_any_task_execution(self):
        path=self.output/'plan.json'
        path.write_text(json.dumps({'format':'archive-research-plan-1','metadata_adopted':False,'tasks':[
            {'id':'first','extractor':'ocr','page':asdict(self.pdf()),'ocr_profile':{'name':'rd_reports','version':999}}]}))
        with self.assertRaisesRegex(ValueError,'profile version'):load_plan(path)


if __name__=='__main__':unittest.main()
