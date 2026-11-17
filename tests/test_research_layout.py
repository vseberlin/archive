"""Real Poppler geometry and deliberately incomplete semantic coverage."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from archive_research import Root,Roots,Source
from archive_research.preparation import inspect_page
from archive_research.runner import extract_one
from archive_research.layout import text_blocks,caption_candidates


def raster_pdf(rotation=0,full_page=False,mirror=False):
    pixels=zlib.compress(bytes([90,120,180])*100)
    placement='600 0 0 500 0 0' if full_page else ('-200 0 0 100 250 250' if mirror else '200 0 0 100 50 250')
    stream=(f'q {placement} cm /Im1 Do Q\nBT /F1 12 Tf 50 230 Td (Abb. 1 Invented machine) Tj ET').encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
             f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 500] /Rotate {rotation} /Resources << /Font << /F1 4 0 R >> /XObject << /Im1 5 0 R >> >> /Contents 6 0 R >>'.encode(),
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             f'<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(pixels)} >>\nstream\n'.encode()+pixels+b'\nendstream',
             f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream']
    data=bytearray(b'%PDF-1.4\n'); offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(data);data.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(data)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
        self.source=self.base/'source';self.source.mkdir();self.output=self.base/'output';self.output.mkdir()
        self.roots=Roots({'source':Root(self.source,'read'),'artifacts':Root(self.output,'work')})

    def task(self,rotation=0,full_page=False,mirror=False):
        p=self.source/f'{rotation}-{full_page}-{mirror}.pdf';p.write_bytes(raster_pdf(rotation,full_page,mirror));s=p.stat()
        source=Source('invented','test','pdf','source',p.name,s.st_size,s.st_mtime_ns)
        page=inspect_page(self.roots,source,1,'pdf',1)
        return {'id':'layout','extractor':'pdf-layout','page':asdict(page),'settings':{'dpi':100}}

    def run_task(self,task):
        attempt=extract_one(self.roots,'artifacts',task)
        self.assertEqual(attempt['state'],'partial',attempt)
        return json.loads((self.output/(attempt['key']+'.json')).read_text()),attempt

    def test_raster_placement_caption_and_cache(self):
        task=self.task();row,attempt=self.run_task(task);f=row['feature']
        self.assertEqual(len(f['figures']),1);figure=f['figures'][0]
        self.assertAlmostEqual(figure['region']['x'],50/600,places=4)
        self.assertAlmostEqual(figure['region']['y'],150/500,places=4)
        self.assertEqual(len(figure['caption_candidates']),1)
        self.assertEqual(figure['caption_candidates'][0]['basis'],'proximity')
        self.assertFalse(f['coverage']['semantic_layout_complete'])
        self.assertEqual(list(self.source.iterdir()),[self.source/task['page']['source']['path']])
        with patch('archive_research.runner.render',side_effect=AssertionError('Reuse expected')):
            self.assertTrue(extract_one(self.roots,'artifacts',task)['reused'])

    def test_rotations_and_mirrored_placement_keep_original_region(self):
        for rotation in (0,90,180,270):
            for mirrored in (False,True):
                with self.subTest(rotation=rotation,mirrored=mirrored):
                    row,_=self.run_task(self.task(rotation,mirror=mirrored))
                    r=row['feature']['figures'][0]['region']
                    for name,value in [('x',50/600),('y',150/500),('width',200/600),('height',100/500)]:
                        self.assertAlmostEqual(r[name],value,places=3)

    def test_full_page_scan_is_not_a_segmented_figure(self):
        row,_=self.run_task(self.task(full_page=True));f=row['feature']
        self.assertEqual(f['figures'],[])
        self.assertIn('needs segmentation',f['omitted'][0]['reason'])

    def test_mixed_font_inline_caption_label_stays_with_its_body(self):
        def line(text,x,y,w,font):return {'text':text,'font':font,'region':{'x':x,'y':y,'width':w,'height':.02}}
        blocks=text_blocks([line('Abb. 1',.6,.4,.08,'0'),line('Machine',.681,.399,.15,'1'),
                            line('type B',.6,.425,.2,'1')])
        self.assertEqual(len(blocks),1)
        self.assertEqual(blocks[0]['text'],'Abb. 1 Machine type B')
        self.assertTrue(blocks[0]['labelled'])

    def test_overlaid_scan_text_is_not_a_caption(self):
        blocks=[{'region':{'x':.2,'y':.2,'width':.2,'height':.1},'labelled':True}]
        self.assertEqual(caption_candidates({'x':0,'y':0,'width':1,'height':1},blocks,.12),[])


if __name__=='__main__':unittest.main()
