"""Explicit line geometry, model bindings and independent recognizer receipts."""
from dataclasses import asdict
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_research import Artifact,Page,Root,Roots,Source,Transform
from archive_research.extractors.line_ocr import extract,model_paths,recipe_for,settings_for
from archive_research.runner import extract_one


class LineTests(unittest.TestCase):
    def setUp(self):
        import cv2 as cv,numpy as np
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.output=base/'out';self.output.mkdir();source=base/'source';source.mkdir()
        self.roots=Roots({'source':Root(source,'read'),'artifacts':Root(self.output,'work')})
        self.image=source/'line.png';cv.imwrite(str(self.image),np.full((100,400,3),230,np.uint8))
        stat=self.image.stat();self.page=Page(Source('test','test','test','source','line.png',stat.st_size,stat.st_mtime_ns),1,'image',0,400,100)
        def model(name):
            p=self.output/name;p.write_bytes(('synthetic '+name).encode())
            return asdict(Artifact('artifacts',name,'model',hashlib.sha256(p.read_bytes()).hexdigest()))
        self.task={'extractor':'line-ocr','page':asdict(self.page),'settings':{
            'tesseract_models':{'rus':model('rus.traineddata'),'eng':model('eng.traineddata')},
            'crnn_model':model('model.onnx'),'lines':[{'label':'selected line','polygon':[[.1,.2],[.9,.2],[.9,.6],[.1,.6]]}],
            'enhancements':['original','levels'],'recognition_angles':[0],'polarities':['original'],'psms':[7]}}
        class FakeCRNN:
            def __init__(self,*args):pass
            def setPreferableBackend(self,*args):pass
            def setPreferableTarget(self,*args):pass
            def setDecodeType(self,*args):pass
            def setVocabulary(self,*args):pass
            def setInputParams(self,**kwargs):pass
            def recognize(self,image):return 'abc123'
        self.fake=FakeCRNN

    def test_invalid_polygons_and_missing_language_bindings_refused(self):
        for polygon in ([[0,0],[1,1],[0,1],[1,0]],[[0,0],[0,0],[0,0],[0,0]],[[0,0],[2,0],[1,1],[0,1]]):
            task=copy.deepcopy(self.task);task['settings']['lines'][0]['polygon']=polygon
            with self.assertRaises(ValueError):settings_for(task)
        task=copy.deepcopy(self.task);task['settings']['tesseract_models'].pop('rus')
        with self.assertRaises(ValueError):settings_for(task)

    def test_model_fingerprint_checked_before_inference(self):
        settings=settings_for(self.task);model_paths(self.roots,settings)
        (self.output/'rus.traineddata').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'Recognizer model changed'):model_paths(self.roots,settings)

    def test_crop_or_model_changes_get_distinct_cache_keys(self):
        key=recipe_for(self.task).cache_key(self.page)
        task=copy.deepcopy(self.task);task['settings']['lines'][0]['polygon'][0][0]=.11
        self.assertNotEqual(key,recipe_for(task).cache_key(self.page))
        task=copy.deepcopy(self.task);task['settings']['crnn_model']['sha256']='0'*64
        self.assertNotEqual(key,recipe_for(task).cache_key(self.page))

    def test_receipt_keeps_engine_outputs_and_no_automatic_coverage_claim(self):
        with patch('cv2.dnn_TextRecognitionModel',self.fake),patch('archive_research.extractors.ocr.extract',return_value=(b'<alto/>',[])):
            _,words,result,crops=extract(self.image,self.page,Transform(400,100,(1,0,0,0,1,0)),self.roots,
                                         settings_for(self.task),self.output,'a'*64)
        self.assertEqual(result['coverage']['mode'],'manual_lines');self.assertFalse(result['coverage']['scan_complete'])
        self.assertTrue(result['coverage']['recognition_complete']);self.assertEqual(len(crops),2)
        variants=result['regions'][0]['variants'];self.assertEqual(len(variants),4)
        self.assertEqual([v['engine'] for v in variants],['tesseract-best','crnn-en']*2)
        self.assertEqual(words[0]['text'],'abc123')
        self.assertEqual(variants[0]['transform']['to_source'],variants[1]['transform']['to_source'])

    def test_cache_reuse_checks_model_and_preserves_prior_receipt(self):
        with patch('cv2.dnn_TextRecognitionModel',self.fake),patch('archive_research.extractors.ocr.extract',return_value=(b'<alto/>',[])):
            first=extract_one(self.roots,'artifacts',self.task)
        self.assertEqual(first['state'],'partial');p=self.output/(first['key']+'.json');before=p.read_bytes()
        second=extract_one(self.roots,'artifacts',self.task);self.assertTrue(second['reused'])
        (self.output/'eng.traineddata').write_bytes(b'changed')
        third=extract_one(self.roots,'artifacts',self.task);self.assertEqual(third['state'],'error')
        self.assertEqual(before,p.read_bytes())

    def test_deadline_between_successful_variants_is_retried(self):
        import json
        clock=[0.0]
        def recognize(*args,**kwargs):
            clock[0]=10_000.0
            return b'<alto/>',[]
        with patch('cv2.dnn_TextRecognitionModel',self.fake), \
             patch('archive_research.extractors.ocr.extract',side_effect=recognize), \
             patch('archive_research.extractors.line_ocr.time.monotonic',side_effect=lambda:clock[0]):
            first=extract_one(self.roots,'artifacts',self.task)
        path=self.output/(first['key']+'.json');row=json.loads(path.read_text())
        self.assertEqual(len(row['discovery']['regions'][0]['variants']),1)
        self.assertTrue(all(v['status']=='complete' for v in row['discovery']['regions'][0]['variants']))
        with patch('cv2.dnn_TextRecognitionModel',self.fake), \
             patch('archive_research.extractors.ocr.extract',return_value=(b'<alto/>',[])):
            second=extract_one(self.roots,'artifacts',self.task)
        self.assertFalse(second['reused'])
        self.assertEqual(row['discovery']['coverage']['recognition_stopped'],'timeout')
        self.assertEqual(row['discovery']['coverage']['requested_variants'],4)
        self.assertEqual(row['discovery']['coverage']['completed_variants'],1)
        self.assertTrue(json.loads(path.read_text())['discovery']['coverage']['recognition_complete'])
        self.assertTrue(extract_one(self.roots,'artifacts',self.task)['reused'])


if __name__=='__main__':unittest.main()
