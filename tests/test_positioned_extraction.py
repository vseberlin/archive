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
from archive_research import Page, Recipe, Root, Roots, Source, Region, Transform
from archive_research.geometry import inverse_rotation, source_region, displayed_region, processed_region
from archive_research.preparation import inspect_page, render
from archive_research.extractors.pdf_text import extract as pdf_text
from archive_research.extractors.ocr import extract as ocr
from archive_research.runner import extract_one, load_plan
from archive_research.web.fixtures import pdf_bytes


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
        before=(self.output/json.loads((self.output/(first['key']+'.json')).read_text())['result']['artifacts'][0]['path']).stat().st_mtime_ns
        with patch('archive_research.runner.render',side_effect=AssertionError('must reuse')):
            second=extract_one(self.roots,'artifacts',task)
        self.assertTrue(second['reused'])
        self.assertEqual(before,(self.output/json.loads((self.output/(first['key']+'.json')).read_text())['result']['artifacts'][0]['path']).stat().st_mtime_ns)
        changed={**task,'settings':{'dpi':110}}
        third=extract_one(self.roots,'artifacts',changed)
        self.assertNotEqual(first['key'],third['key'])
        self.assertTrue((self.output/(first['key']+'.json')).exists())

    def test_character_ocr_retains_both_formats_and_reuses_cached_three_artifacts(self):
        page=self.pdf()
        task={'id':'character-ocr','page':asdict(page),'extractor':'ocr',
              'settings':{'dpi':150,'languages':'eng','psm':6,'character_boxes':True}}
        first=extract_one(self.roots,'artifacts',task)
        self.assertEqual(first['state'],'complete')
        row=json.loads((self.output/(first['key']+'.json')).read_text())
        self.assertEqual(len(row['result']['artifacts']),3)
        self.assertEqual(''.join(w['text'] for w in row['words']),
                         ''.join(w['text'] for w in row['feature']['character_reading']['words']))
        with patch('archive_research.extractors.ocr.extract_characters',side_effect=AssertionError('must reuse')):
            second=extract_one(self.roots,'artifacts',task)
        self.assertTrue(second['reused'])

    def test_shared_render_survives_extractor_change_without_decoding_source(self):
        page=self.pdf()
        first=extract_one(self.roots,'artifacts',{'page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}})
        with patch('archive_research.prepared_views.render',side_effect=AssertionError('source decoded twice')):
            second=extract_one(self.roots,'artifacts',{'page':asdict(page),'extractor':'ocr','settings':{'dpi':100}})
        self.assertNotEqual(first['key'],second['key'])
        rows=[json.loads((self.output/(r['key']+'.json')).read_text()) for r in (first,second)]
        self.assertEqual(rows[0]['result']['artifacts'][0],rows[1]['result']['artifacts'][0])
        self.assertEqual(second['state'],'complete')

    def test_moved_or_touched_identical_file_is_rebound_without_reextraction(self):
        import os, shutil
        from archive_research.receipts import read_current
        page=self.pdf();task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        moved_dir=self.input/'moved';moved_dir.mkdir()
        moved=moved_dir/page.source.path
        shutil.move(self.input/page.source.path,moved);os.utime(moved,ns=(1,2_000_000_000))
        new_page=inspect_page(self.roots,replace(self.bind(moved),path='moved/'+moved.name),1,'pdf',1,printed_label='VII')
        with patch('archive_research.prepared_views.render',side_effect=AssertionError('must not re-render')), \
             patch('archive_research.extractors.pdf_text.extract',side_effect=AssertionError('must not re-extract')):
            second=extract_one(self.roots,'artifacts',{**task,'page':asdict(new_page)})
        self.assertEqual((second['key'],second['reused'],second['rebound']),(first['key'],True,True))
        row,result,_=read_current(self.roots,'artifacts',first['key'])
        self.assertEqual(result.page,new_page)
        self.assertEqual(row['rebound_from']['page']['source']['path'],page.source.path)
        # Legacy (version 1) receipts are found and refiled under the current key.
        legacy=result.recipe.cache_key(new_page,version=1)
        (self.output/(first['key']+'.json')).rename(self.output/(legacy+'.json'))
        third=extract_one(self.roots,'artifacts',{**task,'page':asdict(new_page)})
        self.assertEqual((third['key'],third['reused'],third['rebound']),(first['key'],True,True))

    def test_changed_content_with_stale_stored_hash_is_not_reused(self):
        page=self.pdf();task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        path=self.input/page.source.path
        path.write_bytes(pdf_bytes('OTHER TOKEN',1,'VII','GAMMA',rotation=0))
        stale=replace(page.source,size_bytes=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns)
        later=extract_one(self.roots,'artifacts',{**task,'page':asdict(replace(page,source=stale))})
        self.assertEqual(later['state'],'error')
        self.assertTrue((self.output/(first['key']+'.json')).exists())

    def test_new_extractor_rejects_prepared_image_after_stale_hash_rebind(self):
        page=self.pdf()
        extract_one(self.roots,'artifacts',{'page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}})
        prepared=next(self.output.glob('prepared-*.json'));before=prepared.read_bytes()
        path=self.input/page.source.path
        path.write_bytes(pdf_bytes('OTHER TOKEN',1,'VII','GAMMA',rotation=0))
        stale=replace(page.source,size_bytes=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns)
        with patch('archive_research.extractors.ocr.extract',side_effect=AssertionError('stale render reached OCR')) as recognize:
            result=extract_one(self.roots,'artifacts',{'page':asdict(replace(page,source=stale)),
                              'extractor':'ocr','settings':{'dpi':100}})
        self.assertEqual(result['state'],'error')
        recognize.assert_not_called()
        self.assertEqual(prepared.read_bytes(),before)

    def test_prepared_image_verifies_first_binding_rebind_and_legacy_receipt_once(self):
        from archive_research.prepared_views import prepared_view
        import os
        page=self.pdf()
        def prepare(current):
            return prepared_view(self.roots,current,self.output,self.output,dpi=100,clockwise=0)
        def verified_calls(spy):
            return sum(call.kwargs.get('verify_hash',False) for call in spy.call_args_list)
        with patch.object(self.roots,'source',wraps=self.roots.source) as source:
            first=prepare(page)
            self.assertEqual(verified_calls(source),1)
        receipt=next(self.output.glob('prepared-*.json'))
        self.assertEqual(json.loads(receipt.read_text())['verified_source'],asdict(page.source))
        path=self.input/page.source.path
        moved=self.input/'moved.pdf';path.rename(moved);os.utime(moved,ns=(1,2_000_000_000))
        moved_page=replace(page,source=self.bind(moved))
        with patch('archive_research.prepared_views.render',side_effect=AssertionError('must reuse')), \
             patch.object(self.roots,'source',wraps=self.roots.source) as source:
            second=prepare(moved_page);prepare(moved_page)
            self.assertEqual(first,second)
            self.assertEqual(verified_calls(source),1)
            value=json.loads(receipt.read_text());value.pop('verified_source')
            receipt.write_text(json.dumps(value))
            source.reset_mock()
            prepare(moved_page);prepare(moved_page)
            self.assertEqual(verified_calls(source),1)

    def test_first_preparation_rejects_incorrect_content_hash(self):
        page=self.pdf();page=replace(page,source=replace(page.source,sha256='0'*64))
        with patch('archive_research.prepared_views.render',side_effect=AssertionError('unverified source rendered')) as render:
            result=extract_one(self.roots,'artifacts',{'page':asdict(page),'extractor':'pdf-text'})
        self.assertEqual(result['state'],'error');render.assert_not_called()
        self.assertFalse(list(self.output.glob('prepared-*')))

    def test_unfinished_work_is_retryable_but_deterministic_limits_are_reused(self):
        from archive_research.runner import retryable
        faces=Recipe.create('faces','2');regions=Recipe.create('text-regions','2')
        self.assertTrue(retryable({'feature':{'coverage':{'scan_complete':False,'stopped':'timeout'}}},faces))
        self.assertFalse(retryable({'feature':{'coverage':{'scan_complete':False,'stopped':'budget'}}},faces))
        self.assertFalse(retryable({'feature':{'coverage':{'scan_complete':True}}},faces))
        self.assertTrue(retryable({'discovery':{'passes':[{'state':'not_attempted'}],'regions':[]}},regions))
        self.assertTrue(retryable({'discovery':{'passes':[{'state':'complete'}],
                                                'regions':[{'variants':[{'status':'error'}]}]}},regions))
        self.assertFalse(retryable({'discovery':{'passes':[{'state':'complete'}],
                                                 'regions':[{'variants':[{'status':'complete'}]}]}},regions))

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
        with patch('archive_research.extractors.ocr.extract',side_effect=subprocess.TimeoutExpired('tesseract',1)):
            result=extract_one(self.roots,'artifacts',task)
        self.assertEqual(result['state'],'error')
        receipt=json.loads((self.output/(result['key']+'.json')).read_text())
        self.assertFalse(receipt['result']['artifacts'])

    def test_corrupt_cached_output_withheld(self):
        page=self.pdf();task={'id':'native','page':asdict(page),'extractor':'pdf-text','settings':{'dpi':100}}
        first=extract_one(self.roots,'artifacts',task)
        (self.output/json.loads((self.output/(first['key']+'.json')).read_text())['result']['artifacts'][0]['path']).write_bytes(b'bad')
        self.assertEqual(extract_one(self.roots,'artifacts',task)['state'],'error')

    def test_changed_plan_refused_before_task_loading(self):
        path=self.output/'plan.json';path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'plan changed'):
            load_plan(path, '0'*64)

    def test_explicit_adaptive_threshold_has_own_cache_recipe(self):
        page=self.pdf();task={'id':'adaptive','page':asdict(page),'extractor':'ocr',
                             'settings':{'languages':'eng','psm':6,'dpi':150}}
        base=extract_one(self.roots,'artifacts',task)
        task['settings']['thresholding_method']=2
        adaptive=extract_one(self.roots,'artifacts',task)
        self.assertEqual(adaptive['state'],'complete',adaptive)
        self.assertNotEqual(base['key'],adaptive['key'])
        self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])
        row=json.loads((self.output/(adaptive['key']+'.json')).read_text())
        self.assertIn('ALPHA',[w['text'] for w in row['words']])

    def test_upscaled_ocr_recipe_and_source_coordinates(self):
        page=self.pdf();task={'id':'upscale','page':asdict(page),'extractor':'ocr',
                             'settings':{'languages':'eng','psm':6,'dpi':150}}
        base=extract_one(self.roots,'artifacts',task)
        task['settings']['image_preparation']='gray-up2-v1'
        enlarged=extract_one(self.roots,'artifacts',task)
        self.assertEqual(enlarged['state'],'complete',enlarged)
        self.assertNotEqual(base['key'],enlarged['key'])
        a=json.loads((self.output/(base['key']+'.json')).read_text())
        b=json.loads((self.output/(enlarged['key']+'.json')).read_text())
        self.assertEqual(b['result']['transform']['width'],2*a['result']['transform']['width'])
        for key in ['x','y','width','height']:
            before=next(w['region'][key] for w in a['words'] if w['text']=='ALPHA')
            after=next(w['region'][key] for w in b['words'] if w['text']=='ALPHA')
            self.assertAlmostEqual(before,after,delta=.01)
        self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])

    def test_crop_transform_roundtrips_for_each_rotation(self):
        from PIL import Image
        path = self.input/'regions.png'
        Image.new('RGB', (400, 200), 'white').save(path)
        page = inspect_page(self.roots, self.bind(path), 1, 'image', 0)
        region = Region(.2, .1, .4, .6)
        for angle in (0, 90, 180, 270):
            image, transform, _ = render(self.roots, page, self.output, clockwise=angle, region=region)
            restored = source_region((0, 0, transform.width, transform.height), transform, page)
            for field in ('x', 'y', 'width', 'height'):
                self.assertAlmostEqual(getattr(restored, field), getattr(region, field), places=2)
            shown = processed_region(region, transform, page)
            self.assertAlmostEqual(shown.width, 1, places=2)
            self.assertAlmostEqual(shown.height, 1, places=2)

    def test_crop_ocr_preserves_source_coordinates_and_cache(self):
        from PIL import Image, ImageDraw, ImageFont
        path = self.input/'detail.png'
        im = Image.new('RGB', (1000, 500), 'white')
        ImageDraw.Draw(im).text((300, 210), 'TUBE 18 LK 15', fill='black',
                              font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 40))
        im.save(path); im.close()
        page = inspect_page(self.roots, self.bind(path), 1, 'image', 0)
        task = {'id': 'detail', 'page': asdict(page), 'extractor': 'ocr',
                'region': asdict(Region(.25, .35, .6, .25)), 'settings': {'languages': 'eng', 'psm': 6}}
        first = extract_one(self.roots, 'artifacts', task)
        self.assertEqual(first['state'], 'complete')
        receipt = json.loads((self.output/(first['key']+'.json')).read_text())
        self.assertEqual(receipt['result']['region'], task['region'])
        word = next(w for w in receipt['words'] if w['text'] == 'TUBE')
        self.assertTrue(.29 < word['region']['x'] < .32)
        self.assertTrue(.4 < word['region']['y'] < .5)
        self.assertTrue(extract_one(self.roots, 'artifacts', task)['reused'])
        changed = {**task, 'region': asdict(Region(.2, .3, .7, .4))}
        self.assertNotEqual(extract_one(self.roots, 'artifacts', changed)['key'], first['key'])


if __name__=='__main__':
    unittest.main()
