"""Real crop geometry and ALTO/search structure; no model downloads in tests."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from archive_research import Root,Roots,Source
from archive_research.contracts import Page,Region,Transform
from archive_research.geometry import inverse_rotation,source_region
from archive_research.preparation import crop_prepared,inspect_page,render
from archive_research.prepared_views import prepared_view
from archive_research.extractors.document_text import compose,NS
from archive_research.extractors.document_layout import text_selection,validate_ocr,offline_inference
from archive_research.extractors.document_text import inputs,propose_order,geometric_order
from archive_research.search_projection import fold
from archive_research.extractors.document_evaluation import normalize,distance,article_order,article_contiguity
from test_research_layout import raster_pdf


class DocumentTextTests(unittest.TestCase):
    def test_ocr_upscale_keeps_rotated_crop_source_coordinates(self):
        import io
        from PIL import Image
        from archive_research.imaging import prepare_ocr_image
        page=Page(Source('test','test','image','source','page.png',1,1),1,'image',0,200,100)
        transform=Transform(100,60,(0,.5,20,-.5,0,80))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'crop.png';Image.new('RGB',(100,60),'white').save(path)
            original=path.read_bytes()
            data,scaled=prepare_ocr_image(path,transform,method='gray-up2-v1',dpi=300)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size,(200,120));self.assertEqual(image.mode,'L')
                self.assertAlmostEqual(image.info['dpi'][0],600,places=1)
            self.assertEqual(source_region((10,10,40,20),transform,page),
                             source_region((20,20,80,40),scaled,page))
            self.assertEqual(path.read_bytes(),original)

    def test_closing_joins_dark_stroke_gap_without_pixel_translation(self):
        import io
        import numpy as np
        from PIL import Image
        from archive_research.imaging import prepare_ocr_image
        pixels=np.full((30,30),255,dtype=np.uint8)
        pixels[6:19,10:13]=0;pixels[6:19,14:17]=0
        transform=Transform(30,30,(1,0,0,0,1,0))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'broken.png';Image.fromarray(pixels).save(path)
            original=path.read_bytes()
            data,mapped=prepare_ocr_image(path,transform,method='gray-close2-v1',dpi=300)
            with Image.open(io.BytesIO(data)) as image:repaired=np.asarray(image)
            self.assertTrue(np.all(repaired[6:19,10:17]==0))
            self.assertEqual(int((repaired<128).sum()),13*7)
            self.assertEqual(mapped.to_source,transform.to_source)
            self.assertEqual(path.read_bytes(),original)

    def test_custom_language_directory_does_not_need_alto_config_file(self):
        import os
        import re
        import shutil
        import subprocess
        from PIL import Image
        from archive_research.extractors.ocr import extract
        if not shutil.which('tesseract'):
            self.skipTest('Tesseract unavailable')
        info=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,check=True)
        directory=Path(re.search(r'"([^"]+)"',info.stdout+info.stderr).group(1))
        if not (directory/'eng.traineddata').exists():
            self.skipTest('English model unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            shutil.copyfile(directory/'eng.traineddata',root/'eng.traineddata')
            image=root/'blank.png';Image.new('RGB',(100,100),'white').save(image)
            page=Page(Source('test','test','image','source','blank.png',1,1),1,'image',0,100,100)
            with patch.dict(os.environ,{'TESSDATA_PREFIX':str(root)}):
                raw,words=extract(image,page,Transform(100,100,(1,0,0,0,1,0)),languages='eng',psm=6)
            self.assertEqual(ET.fromstring(raw).tag.rsplit('}',1)[-1],'alto')
            self.assertEqual(words,[])

    def test_ocr_retains_column_blocks_lines_and_word_locations(self):
        from archive_research.extractors.ocr import positioned_alto
        source=Source('test','test','image','source','test.png',10,1)
        page=Page(source,1,'image',0,100,100)
        transform=Transform(100,100,(1,0,0,0,1,0))
        raw='''<alto><Layout><Page WIDTH="100" HEIGHT="100"><PrintSpace>
          <TextBlock HPOS="10" VPOS="20" WIDTH="30" HEIGHT="20">
            <TextLine HPOS="10" VPOS="20" WIDTH="30" HEIGHT="5"><String HPOS="10" VPOS="20" WIDTH="20" HEIGHT="5" CONTENT="Caption15"/></TextLine>
            <TextLine HPOS="10" VPOS="30" WIDTH="30" HEIGHT="5"><String HPOS="10" VPOS="30" WIDTH="20" HEIGHT="5" CONTENT="left-continuation"/></TextLine>
          </TextBlock>
          <TextBlock HPOS="60" VPOS="20" WIDTH="30" HEIGHT="20">
            <TextLine HPOS="60" VPOS="20" WIDTH="30" HEIGHT="5"><String HPOS="60" VPOS="20" WIDTH="20" HEIGHT="5" CONTENT="Caption16"/></TextLine>
          </TextBlock>
        </PrintSpace></Page></Layout></alto>'''
        words,feature=positioned_alto(raw,page,transform)
        self.assertEqual([b['text'] for b in feature['text_blocks']], ['Caption15\nleft-continuation','Caption16'])
        self.assertEqual([(w['block_index'],w['line_index']) for w in words],[(0,0),(0,1),(1,0)])
        self.assertEqual(words[2]['region']['x'],.6)
        self.assertFalse(feature['coverage']['full_page_accuracy_qualified'])
        # The shared search projection must keep caption boundaries as well.
        from archive_research.search_projection import receipt_entry
        result=SimpleNamespace(page=page,region=None,recipe=SimpleNamespace(extractor='ocr'))
        roots=SimpleNamespace(source=lambda source: None)
        with patch('archive_research.search_projection.read_current',
                   return_value=({'words':words,'feature':feature},result,'b'*64)):
            entry=receipt_entry(roots,'artifacts','a'*64,{})
        self.assertEqual(entry['fields']['positioned_lines'],
                         'Caption15\nleft-continuation\n\nCaption16')
        self.assertEqual(entry['evidence']['text_blocks'][1]['region']['x'], .6)
        self.assertEqual(entry['evidence']['text_blocks'][1]['text'], 'Caption16')

    def test_explicit_ocr_normalization_preserves_source_and_geometry(self):
        import importlib.util
        if not importlib.util.find_spec('cv2'):
            self.skipTest('Optional local vision dependency is unavailable')
        import io
        from PIL import Image, ImageDraw
        from archive_research.imaging import normalize_ocr_image
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.png'
            image=Image.new('RGB',(300,200),(240,220,160))
            ImageDraw.Draw(image).text((40,50),'Caption 24',fill=(130,150,180))
            image.save(path); original=path.read_bytes()
            normalized=normalize_ocr_image(path,dpi=300)
            self.assertEqual(path.read_bytes(),original)
            with Image.open(io.BytesIO(normalized)) as result:
                self.assertEqual(result.size,image.size)
                self.assertEqual(result.mode,'L')
                self.assertLess(result.getextrema()[0],result.getextrema()[1])
            self.assertNotEqual(normalized,original)

    def test_local_detector_network_hooks_are_blocked_and_restored(self):
        import socket
        original=socket.create_connection
        with offline_inference():
            with self.assertRaisesRegex(OSError,'disabled'):socket.create_connection(('127.0.0.1',1))
            with socket.socket() as sock:
                with self.assertRaisesRegex(OSError,'disabled'):sock.connect(('127.0.0.1',1))
        self.assertIs(socket.create_connection,original)

    def test_crop_cache_matches_direct_render_rotated_pdf(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);src=base/'source';out=base/'out';tmp=base/'tmp'
            for p in (src,out,tmp):p.mkdir()
            f=src/'page.pdf';f.write_bytes(raster_pdf(90));s=f.stat()
            roots=Roots({'source':Root(src,'read'),'artifacts':Root(out,'work')})
            source=Source('test','test','pdf','source',f.name,s.st_size,s.st_mtime_ns)
            page=inspect_page(roots,source,1,'pdf',1);region=Region(.07,.2,.65,.4)
            direct,dt,dr=render(roots,page,tmp,dpi=100,clockwise=90,region=region)
            with Image.open(direct) as im:expected=(im.size,im.tobytes())
            prepared_view(roots,page,tmp,out,dpi=100,clockwise=90)
            with patch('archive_research.prepared_views.render',side_effect=AssertionError('rerender')):
                cached,ct,cr=prepared_view(roots,page,tmp,out,dpi=100,clockwise=90,region=region)
            with Image.open(cached) as im:self.assertEqual((im.size,im.tobytes()),expected)
            self.assertEqual((dt,dr),(ct,cr))

    def test_alto_uses_full_transform_unique_ids_and_explicit_order(self):
        source=Source('test','test','image','source','test.png',10,1)
        page=Page(source,1,'tiff',0,1000,800,units='pixels')
        transform=inverse_rotation(1000,800,1000,800,0)
        # Crop rotated 90 degrees, half-scale, then translated to source.
        crop=Transform(50,100,(0,2,100,-2,0,300))
        region=asdict(source_region((0,0,50,100),crop,page))
        blocks=[{'index':i,'region':region,'label':'plain text','ocr_eligible':True,
                 'pixel_box':[100+i*250,200,200,100],'score':.9} for i in range(2)]
        raw=b'<alto><Layout><Page><PrintSpace><TextBlock ID="b"><TextLine ID="l" HPOS="5" VPOS="10" WIDTH="20" HEIGHT="10"><String ID="w" HPOS="5" VPOS="10" WIDTH="20" HEIGHT="10" CONTENT="Name" WC=".8"/></TextLine></TextBlock></PrintSpace></Page></Layout></alto>'
        data,words,feature=compose(page,transform,blocks,{i:(raw,crop,{'key':str(i)}) for i in range(2)},[1,0])
        doc=ET.fromstring(data);ids=[n.attrib['ID'] for n in doc.iter() if 'ID' in n.attrib]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual([n.attrib['REF'] for n in doc.iter('{'+NS+'}ElementRef')],['block-1','block-0'])
        word=next(doc.iter('{'+NS+'}String'))
        self.assertEqual([float(word.attrib[k]) for k in ('HPOS','VPOS','WIDTH','HEIGHT')],[120,250,20,40])
        self.assertAlmostEqual(words[0]['region']['x'],.12)
        self.assertEqual((words[0]['block_index'],words[0]['line_index'],words[0]['word_index']), (1,0,0))
        self.assertEqual([b['index'] for b in feature['text_blocks']],[1,0])
        self.assertFalse(feature['coverage']['full_page_accuracy_qualified'])

    def test_duplicate_suppression_keeps_adjacent_columns(self):
        blocks=[{'index':i,'label':'plain text','pixel_box':box,'score':score,'ocr_eligible':True}
                for i,(box,score) in enumerate([([0,0,100,200],.9),([0,0,100,100],.5),([105,0,100,200],.8)])]
        self.assertEqual(text_selection(blocks),([0,2],[{'block':1,'duplicate_of':0}]))

    def test_table_text_is_retained_and_illustration_noise_is_accounted_for(self):
        blocks=[{'index':i,'label':label,'pixel_box':box,'score':score,'ocr_eligible':eligible}
                for i,(label,box,score,eligible) in enumerate([
                    ('table',[0,900,1000,80],.8,False),
                    ('figure',[0,0,300,300],.9,False),
                    ('abandon',[1,1,300,300],.5,True),
                    ('plain text',[0,400,300,100],.8,True),
                    ('figure_caption',[1,401,300,100],.7,True)])]
        selected,suppressed=text_selection(blocks)
        self.assertEqual(selected,[0,3])
        self.assertEqual({r['block'] for r in suppressed},{2,4})
        self.assertIn('inspect',next(r for r in suppressed if r['block']==2)['reason'])

    def test_dehyphenation_does_not_cross_block_boundaries(self):
        self.assertEqual(fold('Tier-\npark'),'tierpark')
        self.assertEqual(fold('Tier-\n\nPark'),'tier-\n\npark')

    def test_metrics_separate_reading_order_from_recognition(self):
        self.assertEqual(normalize('Tier-\npark  „Berlin“'),'Tierpark "Berlin"')
        self.assertEqual(distance('Tierpark','Tierpork'),1)
        regions=[{'x':0,'y':0,'width':.4,'height':1},{'x':.5,'y':0,'width':.4,'height':1}]
        blocks=[{'region':r} for r in regions]
        words=[{'text':'same','region':regions[i]} for i in [1,0]]
        value=article_order(words,blocks,[0,1])
        self.assertEqual(value['inversion_rate'],1)
        self.assertEqual(value['missing_blocks'],[])

    def test_layout_origin_cannot_authorize_a_different_crop(self):
        source=Source('test','test','image','source','test.png',10,1)
        page=Page(source,1,'image',0,100,100)
        expected={'x':.1,'y':.2,'width':.3,'height':.4}
        row={'feature':{'blocks':[{'ocr_eligible':True,'region':expected}]}}
        result=SimpleNamespace(recipe=SimpleNamespace(extractor='document-layout'))
        recipe=SimpleNamespace(record=lambda:{'parameters':{'settings':{
            'layout_origin':{'key':'a'*64,'receipt_sha256':'b'*64,'block':0}}}})
        task={'page':asdict(page),'region':expected}
        with patch('archive_research.extractors.document_layout.read_origin',return_value=(row,result,'b'*64)):
            validate_ocr(None,'artifacts',task,recipe)
            task['region']={**expected,'x':.2}
            with self.assertRaisesRegex(ValueError,'differs'):validate_ocr(None,'artifacts',task,recipe)

    def test_assembly_refuses_missing_detected_text_block(self):
        blocks=[{'index':i,'label':'plain text','pixel_box':[i*110,0,100,100],
                 'score':.9,'ocr_eligible':True} for i in range(2)]
        row={'feature':{'blocks':blocks}}
        result=SimpleNamespace(recipe=SimpleNamespace(extractor='document-layout'))
        settings={'layout_origin':{},'block_order':[0],'ocr_origins':[]}
        with patch('archive_research.extractors.document_text.read_origin',return_value=(row,result,'digest')):
            with self.assertRaisesRegex(ValueError,'every detected text block'):inputs(None,'artifacts',None,settings)

    def test_order_anchors_keep_columns_and_insert_missing_heading(self):
        rects=[(0,0,.9,.1),(0,.2,.4,.6),(.5,.2,.4,.6)]
        blocks=[{'index':i,'label':'title' if i==0 else 'plain text','pixel_box':[x*100,y*100,w*100,h*100],
                 'region':dict(zip(('x','y','width','height'),r)),'score':.9,'ocr_eligible':True}
                for i,r in enumerate(rects) for x,y,w,h in [r]]
        words=[{'region':blocks[i]['region'],'text':'Body'} for i in [1,2]]
        order,info=propose_order(blocks,words)
        self.assertEqual(order,[0,1,2]);self.assertEqual(info['unanchored_blocks'],[0])
        check=article_contiguity([0,2,1],[{'label':'A','blocks':[0,1]},{'label':'B','blocks':[2]}])
        self.assertEqual(check['interleaved_articles'],['A'])

    def test_geometric_order_keeps_caption_rows_and_bottom_caption(self):
        # The right caption starts slightly above the left caption. Reading the
        # page by raw y positions or OCR word anchors would interleave them.
        rects=[(.08,.21,.4,.09),(.56,.20,.36,.11),(.08,.51,.4,.08),
               (.55,.50,.37,.12),(.29,.80,.45,.08),(.03,.93,.92,.05)]
        blocks=[{'index':i,'label':'figure_caption','ocr_eligible':True,'score':.9,
                 'pixel_box':[v*1000 for v in r],
                 'region':dict(zip(('x','y','width','height'),r))} for i,r in enumerate(rects)]
        self.assertEqual(geometric_order(blocks),[0,1,2,3,4,5])

    def test_geometric_order_reads_columns_between_full_width_header_and_footer(self):
        rects=[(.04,.02,.92,.05),(.04,.12,.43,.7),(.53,.13,.43,.25),
               (.53,.42,.43,.4),(.04,.93,.92,.04)]
        blocks=[{'index':i,'label':'plain text','ocr_eligible':True,'score':.9,
                 'pixel_box':[v*1000 for v in r],
                 'region':dict(zip(('x','y','width','height'),r))} for i,r in enumerate(rects)]
        self.assertEqual(geometric_order(blocks),[0,1,2,3,4])


if __name__=='__main__':unittest.main()
