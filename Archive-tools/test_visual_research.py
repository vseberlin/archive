import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_common import ReviewError
from matching import stamp
from visual_research import digest, freeze, make_preview, neighbours, output, report


class VisualResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'previews').mkdir()
        self.source = self.root / 'source.tif'
        Image.new('RGB', (1600, 800), (40, 80, 120)).save(self.source)
        self.row = {'id': 'test', 'side': 'candidate', 'path': str(self.source), 'stamp': stamp(self.source)}

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_preserves_source_and_aspect(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = make_preview(self.root, self.row, {})
        self.assertEqual(result['preview_size'], [1024,512])
        self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertFalse(result['recipe']['visual_checked'])

    def test_no_upscale_and_explicit_inversion(self):
        Image.new('RGB',(40,20),(0,0,0)).save(self.source)
        self.row['stamp']=stamp(self.source)
        result=make_preview(self.root,self.row,{'invert':True,'rotate':90,'checked':True})
        self.assertEqual(result['preview_size'],[20,40])
        with Image.open(self.root/result['preview']) as im:
            self.assertGreater(im.getpixel((0,0))[0],250)

    def test_source_change_refused(self):
        self.source.write_bytes(b'changed')
        with self.assertRaises(ReviewError):
            make_preview(self.root,self.row,{})

    def test_view_check_is_bound_to_source_stamp(self):
        stale=make_preview(self.root,self.row,{'checked':True,'source_stamp':{'size_bytes':1}})
        self.assertFalse(stale['recipe']['visual_checked'])
        current=make_preview(self.root,self.row,{'checked':True,'source_stamp':self.row['stamp']})
        self.assertTrue(current['recipe']['visual_checked'])

    def test_preexisting_temporary_hardlink_is_preserved_and_refused(self):
        key=digest({'path':str(self.source),'stamp':self.row['stamp'],'override':{},'recipe':'1'})
        tmp=self.root/f'previews/.{key}.jpg'
        import os
        os.link(self.source,tmp)
        before=self.source.read_bytes()
        with self.assertRaises(ReviewError):
            make_preview(self.root,self.row,{})
        self.assertEqual(self.source.read_bytes(),before)
        self.assertTrue(tmp.exists())

    def test_external_output_symlink_refused(self):
        (self.root/'escape').symlink_to('/tmp',target_is_directory=True)
        with self.assertRaises(ReviewError):
            output(self.root,'escape/example')
        with self.assertRaises(ReviewError):
            output(self.root,'../outside')

    def test_failed_decode_is_not_cached(self):
        self.source.write_bytes(b'broken TIFF')
        self.row['stamp']=stamp(self.source)
        with self.assertRaises(ReviewError):
            make_preview(self.root,self.row,{})
        self.assertEqual(list((self.root/'previews').iterdir()),[])

    def test_self_exclusion_and_small_reference_pool(self):
        matrix=np.array([[1.,0.],[0.,1.]])
        rows=[{'id':'a'},{'id':'b'}]
        result=neighbours(matrix[0],matrix,rows,6,'a')
        self.assertEqual([x['id'] for x in result],['b'])

    def test_stale_embedding_manifest_refused(self):
        (self.root/'manifest.json').write_text('{}')
        (self.root/'embedding-manifest.json').write_text(json.dumps({'manifest_digest':'stale'}))
        from types import SimpleNamespace
        with self.assertRaises(ReviewError):
            report(SimpleNamespace(work_root=self.root,k=6))

    def test_freeze_uses_scope_and_does_not_select_master(self):
        from types import SimpleNamespace
        candidate={'id':'source','eligible':True,'complete':True,
                   'members':[{'path':str(self.source)}]}
        decision={'selected_candidate_id':''}
        fake=SimpleNamespace(objects={'new':{'candidates':[candidate]},'known':{'candidates':[candidate]}},
                             candidates={'new':{'source':candidate}},
                             read_decisions=lambda: ({'new':decision,'known':decision},'revision'),
                             scope_context=lambda oid,row: {'kind':'proposed_new_object' if oid=='new' else 'existing_md_object'},
                             problems=lambda c: [])
        with patch('visual_research.Workbench',return_value=fake):
            rows,revision=freeze(self.root,1)
            self.assertEqual([r['id'] for r in rows],['new'])
            self.assertEqual(decision['selected_candidate_id'],'')
            self.assertIn('no Master decision',rows[0]['view_basis'])
            with self.assertRaises(ReviewError):
                freeze(self.root,2)

    def test_report_preserves_evidence_and_escapes_markup(self):
        from types import SimpleNamespace
        candidate={**self.row,'title':'<script>bad</script>','preview':'previews/a.jpg'}
        reference={**candidate,'id':'unit#md:1','title':'Reference',
                   'metadata':{'date':[{'value':'commissioned 1972','field':'commission_date'}]}}
        manifest={'candidates':[candidate], 'references':[reference]}
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        (self.root/'embeddings').mkdir()
        for key in ('a','b'):
            np.save(self.root/f'embeddings/{key}.npy',np.array([1.,0.]))
        (self.root/'embedding-manifest.json').write_text(json.dumps({
            'config':{},'manifest_digest':digest(manifest),
            'items':[{'id':'test','key':'a'},{'id':'unit#md:1','key':'b'}]}))
        report(SimpleNamespace(work_root=self.root,k=6))
        text=(self.root/'report.html').read_text()
        self.assertNotIn('<script>',text)
        self.assertIn('commission_date',text)
        result=json.loads((self.root/'neighbours.json').read_text())['items'][0]
        self.assertEqual(result['candidates'],[])
        self.assertEqual(result['references'][0]['id'],'unit#md:1')


if __name__=='__main__':
    unittest.main()
