"""Scan candidate acceptance, coordinate lineage, and stale dependency refusal."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive_research import Root,Roots,Source
from archive_research.preparation import inspect_page
from archive_research.runner import extract_one
from archive_research.extractors.scan_layout import detect, ocr_layout
from archive_research.extractors.layout import visual_tasks


class ScanLayoutTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image,ImageDraw
        import numpy as np
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
        self.source=self.base/'source';self.source.mkdir();self.out=self.base/'outputs';self.out.mkdir()
        self.roots=Roots({'source':Root(self.source,'read'),'artifacts':Root(self.out,'work')})
        self.image=Image.new('RGB',(800,1000),'white')
        self.image.paste(Image.fromarray(np.random.default_rng(4).integers(0,255,(250,300,3),dtype=np.uint8)),(100,150))
        self.settings={'max_side':1600,'min_area':.008,'max_area':.75,'max_figures':32}
        self.Draw=ImageDraw

    def test_photo_candidate_is_contained_and_blank_has_none(self):
        from PIL import Image
        found,_=detect(self.image,[],[],self.settings)
        self.assertEqual(len(found),1)
        x,y,w,h=found[0]['box']
        self.assertLess(abs(x-.125),.02);self.assertLess(abs(y-.15),.02)
        self.assertGreater(w,.35);self.assertGreater(h,.23)
        self.assertEqual(detect(Image.new('RGB',(800,1000),'white'),[],[],self.settings)[0],[])

    def test_masked_dense_text_is_not_a_figure(self):
        from PIL import Image
        im=Image.new('RGB',(800,1000),'white');d=self.Draw.Draw(im)
        for y in range(150,400,10):
            for x in range(100,400,6):d.rectangle((x,y,x+3,y+6),fill='black')
        self.assertEqual(detect(im,[(.12,.14,.4,.28)],[],self.settings)[0],[])

    def test_whole_page_and_thin_border_rejected_duplicate_provenance_retained(self):
        found,omitted=detect(self.image,[],[(0,0,1,1),(.125,.15,.375,.25),(0,0,.01,.9)],self.settings)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['basis'],['local-texture','tesseract-illustration'])
        self.assertEqual(len(omitted),2)

    def pipeline(self,clockwise=0):
        file=self.source/'scan.png';self.image.save(file);s=file.stat()
        source=Source('synthetic','one','image','source',file.name,s.st_size,s.st_mtime_ns)
        page=asdict(inspect_page(self.roots,source,1,'image',0))
        ocr={'id':'ocr','extractor':'ocr','page':page,'settings':{'languages':'eng','psm':11}}
        prior=extract_one(self.roots,'artifacts',ocr);self.assertNotEqual(prior['state'],'error',prior)
        key=prior['key'];origin={'key':key,'receipt_sha256':hashlib.sha256((self.out/(key+'.json')).read_bytes()).hexdigest()}
        task={'id':'layout','extractor':'scan-layout','page':page,'settings':{'ocr_origin':origin,'clockwise':clockwise}}
        attempt=extract_one(self.roots,'artifacts',task);self.assertEqual(attempt['state'],'partial',attempt)
        return task,attempt,json.loads((self.out/(attempt['key']+'.json')).read_text())

    def test_actual_pipeline_crops_cache_and_no_source_writes(self):
        task,attempt,row=self.pipeline();self.assertGreater(len(row['feature']['figures']),0)
        self.assertEqual([p.name for p in self.source.iterdir()],['scan.png'])
        with patch('archive_research.runner.render',side_effect=AssertionError('Should reuse')):
            self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])
        derived=visual_tasks(self.roots,'artifacts',attempt['key'],{})
        self.assertEqual(derived[0]['settings']['region_origin']['key'],attempt['key'])

    def test_pinned_ocr_version_survives_current_receipt_change(self):
        task,_,_=self.pipeline();path=self.out/(task['settings']['ocr_origin']['key']+'.json')
        row=json.loads(path.read_text());row['words'].append({'text':'injected'});path.write_text(json.dumps(row))
        attempt=extract_one(self.roots,'artifacts',task)
        self.assertTrue(attempt['reused'], attempt)
        # The explicitly pinned old evidence is retained. A direct read of the
        # edited current result still fails; it cannot replace that old evidence.
        from archive_research.receipts import read_current
        with self.assertRaisesRegex(ValueError, 'Immutable result changed'):
            read_current(self.roots, 'artifacts', task['settings']['ocr_origin']['key'])

    def test_rotation_maps_back_to_original_source_region(self):
        _,_,row=self.pipeline(clockwise=90)
        r=row['feature']['figures'][0]['region']
        self.assertAlmostEqual(r['x'],.125,delta=.02);self.assertAlmostEqual(r['y'],.15,delta=.02)
        self.assertAlmostEqual(r['width'],.375,delta=.025);self.assertAlmostEqual(r['height'],.25,delta=.025)

    def test_zero_sized_alto_illustration_is_visible_omission(self):
        _,_,row=self.pipeline()
        from archive_research.contracts import Result
        result=Result.from_record(row['result'])
        blocks,figures,omitted=ocr_layout(b'<alto><Illustration HPOS="0" VPOS="0" WIDTH="0" HEIGHT="2"/></alto>',result)
        self.assertEqual(figures,[]);self.assertEqual(len(omitted),1)


if __name__=='__main__':unittest.main()
