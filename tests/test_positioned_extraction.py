"""Actual Poppler/Tesseract trial on invented media; no archive payloads."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'Archive-tools')]
from archive_research import Page, Root, Roots, Source, Region, Transform
from archive_research.geometry import inverse_rotation, source_region, displayed_region
from archive_research.preparation import inspect_page, render
from archive_research.pdf_text import extract as pdf_text
from archive_research.ocr import extract as ocr
from archive_research.runner import extract_one, load_plan
from research_web.fixtures import pdf_bytes


class PositionedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.input = self.base/'input';self.input.mkdir()
        self.output = self.base/'output';self.output.mkdir()
        self.roots = Roots({'source': Root(self.input,'read'), 'artifacts': Root(self.output,'work')})

    def bind(self, path):
        stat=path.stat()
        return Source('synthetic-unit','synthetic-unit#object','synthetic-selected','source',path.name,
                      stat.st_size,stat.st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest())

    def pdf(self, rotation=0):
        path=self.input/f'rotation-{rotation}.pdf'
        path.write_bytes(pdf_bytes('TEST TOKEN',1,'VII','ALPHA BETA',rotation=rotation))
        return inspect_page(self.roots,self.bind(path),1,'pdf',1,printed_label='VII')

    def test_embedded_pdf_rotation_maps_native_word_to_same_source_region(self):
        regions=[]
        for rotation in (0,90,180,270):
            page=self.pdf(rotation)
            raw,words=pdf_text(self.roots,page)
            region=next(w['region'] for w in words if w['text']=='TEST')
            regions.append(region)
            self.assertIn(b'TEST',raw)
        for region in regions[1:]:
            for key in ('x','y','width','height'):
                self.assertAlmostEqual(region[key],regions[0][key],places=5)

    def test_orthogonal_roundtrip_region(self):
        page=self.pdf()
        region=Region(.1,.2,.3,.4)
        for angle in (0,90,180,270):
            width,height=(1000,1200) if angle in (90,270) else (1200,1000)
            displayed=displayed_region(region,angle)
            pixels=(displayed[0]*width,displayed[1]*height,displayed[2]*width,displayed[3]*height)
            restored=source_region(pixels,inverse_rotation(page.width,page.height,width,height,angle),page)
            for key in ('x','y','width','height'):
                self.assertAlmostEqual(getattr(restored,key),getattr(region,key))

    def test_real_alto_words_and_small_text_coordinates(self):
        from PIL import Image,ImageDraw,ImageFont
        path=self.input/'label.png'
        image=Image.new('RGB',(1400,600),'white')
        draw=ImageDraw.Draw(image);font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',36)
        draw.text((100,180),'SYNTHETIC LABEL 1972',font=font,fill='black');image.save(path)
        page=inspect_page(self.roots,self.bind(path),1,'image',0)
        image,transform,rotation=render(self.roots,page,self.output)
        raw,words=ocr(image,page,transform,languages='eng',psm=6,timeout=60)
        self.assertIn(b'alto',raw)
        word=next(w for w in words if w['text']=='1972')
        self.assertTrue(.2 < word['region']['x'] < .6)
        self.assertTrue(.25 < word['region']['y'] < .4)
        self.assertIsNotNone(word['confidence'])

    def test_resume_reuses_complete_and_recipe_change_is_selective(self):
        page=self.pdf()
        task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        before=(self.output/(first['key']+'.png')).stat().st_mtime_ns
        with patch('archive_research.runner.render',side_effect=AssertionError('must reuse')):
            second=extract_one(self.roots,'artifacts',task)
        self.assertTrue(second['reused'])
        self.assertEqual(before,(self.output/(first['key']+'.png')).stat().st_mtime_ns)
        changed={**task,'settings':{'dpi':110}}
        third=extract_one(self.roots,'artifacts',changed)
        self.assertNotEqual(first['key'],third['key'])
        self.assertTrue((self.output/(first['key']+'.json')).exists())

    def test_source_change_withholds_cached_result_and_retains_receipt(self):
        page=self.pdf();task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        receipt=self.output/(first['key']+'.json');before=receipt.read_bytes()
        (self.input/page.source.path).write_bytes(b'changed')
        later=extract_one(self.roots,'artifacts',task)
        self.assertEqual(later['state'],'changed_source')
        self.assertEqual(receipt.read_bytes(),before)

    def test_missing_and_unreadable_are_explicit_errors(self):
        page=self.pdf();(self.input/page.source.path).unlink()
        task={'id':'missing','page':asdict(page),'extractor':'pdf-text'}
        self.assertEqual(extract_one(self.roots,'artifacts',task)['state'],'error')
        broken=self.input/'broken.tif';broken.write_bytes(b'not a TIFF')
        page=Page(self.bind(broken),1,'tiff',0,100,100)
        task={'id':'broken','page':asdict(page),'extractor':'ocr'}
        self.assertEqual(extract_one(self.roots,'artifacts',task)['state'],'error')

    def test_timeout_is_not_cached_as_completed(self):
        page=self.pdf();task={'id':'timeout','page':asdict(page),'extractor':'ocr'}
        with patch('archive_research.ocr.extract',side_effect=subprocess.TimeoutExpired('tesseract',1)):
            result=extract_one(self.roots,'artifacts',task)
        self.assertEqual(result['state'],'error')
        receipt=json.loads((self.output/(result['key']+'.json')).read_text())
        self.assertFalse(receipt['result']['artifacts'])

    def test_corrupt_cached_output_withheld(self):
        page=self.pdf();task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        (self.output/(first['key']+'.png')).write_bytes(b'bad')
        self.assertEqual(extract_one(self.roots,'artifacts',task)['state'],'error')

    def test_changed_plan_refused_before_task_loading(self):
        path=self.output/'plan.json';path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'plan changed'):
            load_plan(path, '0'*64)


if __name__=='__main__':
    unittest.main()
