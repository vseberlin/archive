"""Material/recognition boundary and literal line provenance; no model download."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import unittest
from unittest.mock import patch

from archive_research.contracts import Region
from archive_research.preparation import inspect_page
from archive_research.runner import extract_one
from archive_research.receipts import read_current
from archive_research.extractors.document_layout import downstream_plan,material_annotation,recipe_for
from archive_research.extractors.handwriting import ctc_decode,onnx_input,model_files,selected_lines,ended_with_eos
import test_positioned_extraction as positioned


class HandwritingTests(unittest.TestCase):
    setUp=positioned.PositionedTests.setUp
    bind=positioned.PositionedTests.bind

    def prepare(self):
        from PIL import Image
        p=self.input/'page.png';Image.new('RGB',(200,200),'white').save(p)
        page=inspect_page(self.roots,self.bind(p),1,'image',0)
        region=lambda x,y,w,h:asdict(Region(x,y,w,h))
        annotation={'format':'archive-material-regions-1','metadata_adopted':False,'page':asdict(page),
            'blocks':[{'index':0,'label':'handwriting','region':region(.1,.1,.8,.3),
                       'lines':[{'id':'line-1','region':region(.1,.1,.8,.1)}]},
                      {'index':1,'label':'figure','region':region(.1,.5,.3,.3)},
                      {'index':2,'label':'plain text','region':region(.5,.5,.4,.1)},
                      {'index':3,'label':'formula','region':region(.5,.7,.3,.1)}],
            'caption_links':[{'text_block':0,'image_block':1,'status':'proposed'}]}
        p=self.input/'annotation.json';p.write_text(json.dumps(annotation))
        task={'id':'layout','extractor':'document-layout','page':asdict(page),'settings':{'region_annotation':asdict(self.bind(p))}}
        return page,annotation,task

    def test_material_routing_never_sends_photos_formulas_or_handwriting_to_print_ocr(self):
        page,annotation,task=self.prepare()
        with patch('archive_research.extractors.document_layout._extract',side_effect=AssertionError('No detector')):
            result=extract_one(self.roots,'artifacts',task)
        self.assertEqual(result['state'],'partial',result)
        plan=downstream_plan(self.roots,'artifacts',result['key'],profile='brigadebuch',handwriting={'backend':'tesseract-control'})
        self.assertEqual([t['extractor'] for t in plan['tasks']],['ocr','handwriting'])
        self.assertEqual(plan['tasks'][0]['settings']['layout_origin']['block'],2)
        h=plan['tasks'][1]
        self.assertEqual(selected_lines(self.roots,'artifacts',page,h['settings']),annotation['blocks'][0]['lines'])
        # Recognition cannot take its region or words from a different source.
        other=deepcopy(h['settings']);other['layout_origin']['block']=1
        with self.assertRaisesRegex(ValueError,'handwritten'):selected_lines(self.roots,'artifacts',page,other)
        with patch('archive_research.extractors.ocr.extract',return_value=(b'', [{'text':'raw W/mekK'}])):
            reading=extract_one(self.roots,'artifacts',h)
        self.assertEqual(reading['state'],'partial',reading)
        row,_,_=read_current(self.roots,'artifacts',reading['key'])
        self.assertEqual(row['feature']['lines'][0]['text'],'raw W/mekK')
        self.assertEqual(row['words'][0]['region'],annotation['blocks'][0]['lines'][0]['region'])
        self.assertIsNone(row['words'][0]['confidence'])
        self.assertTrue(extract_one(self.roots,'artifacts',h)['reused'])

    def test_changed_annotation_and_wrong_page_are_rejected(self):
        page,value,task=self.prepare();recipe=recipe_for(task)
        (self.input/'annotation.json').write_text('{}')
        with self.assertRaises(ValueError):material_annotation(self.roots,recipe,page)
        page,value,task=self.prepare();value['page']['source']['object_id']='other'
        # Identity aliases intentionally do not alter content binding; change member bytes instead.
        value['page']['source']['sha256']='f'*64
        p=self.input/'annotation.json';p.write_text(json.dumps(value));task['settings']['region_annotation']=asdict(self.bind(p))
        with self.assertRaisesRegex(ValueError,'another page'):material_annotation(self.roots,recipe_for(task),page)

    def test_line_outside_region_and_caption_to_text_are_rejected(self):
        page,value,task=self.prepare();value['blocks'][0]['lines'][0]['region']['y']=.8
        p=self.input/'annotation.json';p.write_text(json.dumps(value));task['settings']['region_annotation']=asdict(self.bind(p))
        with self.assertRaisesRegex(ValueError,'outside'):material_annotation(self.roots,recipe_for(task),page)
        page,value,task=self.prepare();value['caption_links'][0]['image_block']=2
        p.write_text(json.dumps(value));task['settings']['region_annotation']=asdict(self.bind(p))
        with self.assertRaisesRegex(ValueError,'caption'):material_annotation(self.roots,recipe_for(task),page)

    def test_ctc_blank_separates_repeats_and_invalid_classes_fail(self):
        self.assertEqual(ctc_decode([0,1,1,0,1,2,2,0],'ab'),'aab')
        with self.assertRaises(ValueError):ctc_decode([3],'ab')

    def test_onnx_keeps_whole_long_line_and_white_padding(self):
        from PIL import Image
        config={'format_version':2,'image_height':64,'max_width':1024,'input_channels':1,'blank_index':0}
        pixels,reduced=onnx_input(Image.new('L',(4000,40),0),config)
        self.assertEqual(pixels.shape,(1,1,64,1024));self.assertTrue(reduced)
        self.assertEqual(float(pixels[0,0,0,0]),1.0);self.assertEqual(float(pixels[0,0,32,-1]),0.0)

    def test_model_content_hash_is_checked_before_use(self):
        files=[]
        for name in ('config.json','model.onnx'):
            p=self.input/name;p.write_bytes(b'fixture');files.append({'name':name,'path':name,'size_bytes':7,'sha256':hashlib.sha256(b'fixture').hexdigest()})
        p=self.input/'model.json';p.write_text(json.dumps({'repository':'naeyn/de-htr-web-v2','revision':'a'*40,'files':files}))
        s={'backend':'dehtr-v2','model_manifest':asdict(self.bind(p))};self.assertEqual(len(model_files(self.roots,s)),2)
        (self.input/'model.onnx').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):model_files(self.roots,s)

    def test_generation_accepts_scalar_and_multiple_stop_tokens(self):
        self.assertTrue(ended_with_eos(2,2))
        self.assertTrue(ended_with_eos(2,[2,3]))
        self.assertFalse(ended_with_eos(4,[2,3]))


if __name__=='__main__':unittest.main()
