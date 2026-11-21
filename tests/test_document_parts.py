"""Mixed-material routing, source binding and real downstream OCR reuse."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from archive_research.contracts import Recipe
from archive_research.extractors.document_layout import downstream_plan, text_selection, TEXT_SCOPE, part_kind
from archive_research.extractors.document_text import geometric_order, inputs
from archive_research.extractors.visual_features import validate_inputs
from archive_research.evaluation import part_score
from archive_research.runner import extract_one, recipe_for
from archive_research.receipts import read_current
from archive_research.result_records import origin
import test_positioned_extraction as positioned


def block(index,label,region):
    return {'index':index,'label':label,'region':dict(zip(('x','y','width','height'),region)),
            'pixel_box':[v*1000 for v in region],'score':.9,
            'ocr_eligible':label in ('title','plain text','figure_caption','table','abandon')}


class PartRoutingTests(unittest.TestCase):
    def test_mixed_page_retains_specialists_and_excludes_nested_table_text(self):
        blocks=[block(0,'title',(.05,.02,.9,.05)),block(1,'figure',(.05,.1,.4,.3)),
                block(2,'figure_caption',(.05,.42,.4,.05)),block(3,'table',(.5,.1,.4,.3)),
                block(4,'plain text',(.52,.12,.3,.1)),block(5,'isolate_formula',(.05,.6,.4,.1)),
                block(6,'handwriting',(.5,.6,.4,.1)),block(7,'abandon',(.05,.8,.4,.1))]
        before=deepcopy(blocks)
        selected,excluded=text_selection(blocks,TEXT_SCOPE)
        self.assertEqual(selected,[0,2])
        self.assertEqual({e['block'] for e in excluded},{1,3,4,5,6,7})
        self.assertEqual(next(e for e in excluded if e['block']==4)['containers'],[3])
        self.assertEqual(blocks,before)
        self.assertIn(3,text_selection(blocks)[0])  # Legacy readings remain valid.
        self.assertEqual(geometric_order(blocks,TEXT_SCOPE),[0,2])

    def test_unknown_detector_class_is_not_ordinary_text(self):
        self.assertEqual(part_kind('new-unrecognized-label'),'unresolved')
        self.assertEqual(text_selection([block(0,'abandon',(.1,.1,.2,.2))],TEXT_SCOPE)[0],[])

    def test_geometry_success_does_not_hide_wrong_material_class(self):
        region={'x':.1,'y':.1,'width':.2,'height':.2}
        score=part_score([{'region':region,'kind':'table'}],[{'region':region,'kind':'text'}],complete_reference=True)
        self.assertEqual(score['region_detection']['recall'],1)
        self.assertEqual(score['typed_recall'],0)
        self.assertEqual(score['kind_confusion'],{'table':{'text':1}})

    def test_partial_annotations_do_not_invent_false_positive_rate(self):
        a={'region':{'x':0,'y':0,'width':.4,'height':1},'kind':'text'}
        b={'region':{'x':.5,'y':0,'width':.4,'height':1},'kind':'visual'}
        score=part_score([a],[a,b])
        self.assertIsNone(score['region_detection']['precision'])
        self.assertEqual(score['unjudged_predictions'],[1])
        self.assertFalse(score['qualified'])

    def test_merge_cannot_count_as_two_extracted_parts(self):
        refs=[{'region':{'x':x,'y':0,'width':.5,'height':1},'kind':'text'} for x in (0,.5)]
        score=part_score(refs,[{'region':{'x':0,'y':0,'width':1,'height':1},'kind':'text'}],complete_reference=True)
        self.assertEqual(score['typed_recall'],.5)


class PartIntegrationTests(unittest.TestCase):
    setUp=positioned.PositionedTests.setUp
    bind=positioned.PositionedTests.bind
    pdf=positioned.PositionedTests.pdf

    def layout(self):
        page=self.pdf()
        blocks=[block(0,'plain text',(.01,.01,.98,.5)),
                block(1,'figure',(.1,.6,.3,.2)),block(2,'table',(.5,.6,.3,.2))]
        feature={'blocks':blocks,'caption_links':[], 'coverage':{'semantic_layout_complete':False}}
        recipe=Recipe.create('document-layout','synthetic-test',settings={
            'dpi':100,'clockwise':0,'model':asdict(page.source)})
        with patch('archive_research.extractors.document_layout.recipe_for',return_value=recipe), \
             patch('archive_research.extractors.document_layout._extract',return_value=(b'{}',[],feature,[])):
            result=extract_one(self.roots,'artifacts',{'id':'layout','extractor':'document-layout','page':asdict(page)})
        self.assertEqual(result['state'],'partial',result)
        return page,result['key'],blocks

    def test_plan_reuses_layout_and_routes_to_independent_processors(self):
        page,key,blocks=self.layout()
        original=(self.output/(key+'.json')).read_bytes()
        with patch('archive_research.extractors.document_layout._extract',side_effect=AssertionError('must reuse')):
            plan=downstream_plan(self.roots,'artifacts',key,profile='historical_newspaper',
                                 languages='eng',visual_model=asdict(page.source))
        self.assertEqual([t['extractor'] for t in plan['tasks']],['ocr','visual'])
        self.assertEqual([p['kind'] for p in plan['parts']],['text','visual','table'])
        self.assertEqual(plan['parts'][2]['status'],'retained_without_processing')
        self.assertEqual(plan['page'],asdict(page))
        self.assertEqual((self.output/(key+'.json')).read_bytes(),original)
        text,visual=plan['tasks']
        self.assertNotIn('ocr_profile',visual)
        self.assertEqual(visual['settings']['region_origin']['block'],1)
        self.assertEqual(visual['region'],blocks[1]['region'])
        # A downstream recipe change must leave its sibling and layout untouched.
        changed={**text,'settings':{**text['settings'],'languages':'deu'}}
        self.assertNotEqual(recipe_for(text),recipe_for(changed))
        self.assertEqual(plan['tasks'][1],visual)

    def test_real_crop_ocr_assembly_and_reuse_keep_table_explicit(self):
        page,key,blocks=self.layout()
        plan=downstream_plan(self.roots,'artifacts',key,profile='historical_newspaper',languages='eng')
        reading=extract_one(self.roots,'artifacts',plan['tasks'][0])
        self.assertEqual(reading['state'],'complete',reading)
        row,_,digest=read_current(self.roots,'artifacts',reading['key'])
        self.assertIn('ALPHA',[w['text'] for w in row['words']])
        settings={'layout_origin':plan['layout_origin'],'block_order':[0],
                  'ocr_origins':[origin(self.roots,'artifacts',reading['key'],digest)],
                  'order_method':'geometric-cuts-v1','text_scope':TEXT_SCOPE}
        task={'id':'text','extractor':'document-text','page':asdict(page),
              'ocr_profile':plan['material_profile'],'settings':settings}
        assembled=extract_one(self.roots,'artifacts',task)
        self.assertEqual(assembled['state'],'partial',assembled)
        row,_,_=read_current(self.roots,'artifacts',assembled['key'])
        self.assertEqual([p['block'] for p in row['feature']['unassembled_parts']],[1,2])
        self.assertIn('ALPHA',[w['text'] for w in row['words']])
        with patch('archive_research.runner.render',side_effect=AssertionError('must reuse')):
            self.assertTrue(extract_one(self.roots,'artifacts',plan['tasks'][0])['reused'])
            self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])
        # Specialist exclusions cannot excuse an omitted eligible text block.
        bad={**settings,'block_order':[],'ocr_origins':[]}
        with self.assertRaisesRegex(ValueError,'every detected text block'):
            inputs(self.roots,'artifacts',page,bad,plan['material_profile'])

    def test_visual_origin_rejects_text_wrong_crop_and_changed_parent(self):
        page,key,blocks=self.layout()
        plan=downstream_plan(self.roots,'artifacts',key,profile='historical_newspaper',visual_model=asdict(page.source))
        task=plan['tasks'][1]
        recipe=SimpleNamespace(record=lambda:{'parameters':{'settings':task['settings']}})
        validate_inputs(self.roots,'artifacts',task,recipe)
        task['region']={**task['region'],'x':.2}
        with self.assertRaisesRegex(ValueError,'crop differs'):validate_inputs(self.roots,'artifacts',task,recipe)
        task['settings']['region_origin']['block']=0
        with self.assertRaisesRegex(ValueError,'not a figure'):validate_inputs(self.roots,'artifacts',task,recipe)
        task['settings']['region_origin']['block']=1
        task['settings']['region_origin']['receipt_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'changed'):validate_inputs(self.roots,'artifacts',task,recipe)

    def test_source_change_or_profile_mismatch_refuses_routing(self):
        page,key,_=self.layout()
        with self.assertRaisesRegex(ValueError,'preparation differs'):
            downstream_plan(self.roots,'artifacts',key,profile='rd_reports')
        (self.input/page.source.path).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            downstream_plan(self.roots,'artifacts',key,profile='historical_newspaper')

    def test_cli_operation_preserves_existing_plan_and_rejects_path_escape(self):
        from archive_research.pipeline import document_parts
        _,key,_=self.layout()
        config=self.base/'roots.toml'
        config.write_text(f'[roots.source]\npath = "{self.input}"\naccess = "read"\n'
                          f'[roots.artifacts]\npath = "{self.output}"\naccess = "work"\n')
        document_parts(config,key,'historical_newspaper','parts.json',languages='eng')
        before=(self.output/'parts.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'already exists'):
            document_parts(config,key,'historical_newspaper','parts.json',languages='eng')
        with self.assertRaises(ValueError):
            document_parts(config,key,'historical_newspaper','../escaped.json',languages='eng')
        self.assertEqual((self.output/'parts.json').read_bytes(),before)


if __name__=='__main__': unittest.main()
