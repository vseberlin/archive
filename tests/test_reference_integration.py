"""Reference evaluation uses explicit source bindings and excludes duplicate leakage."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from archive_research import Source
from archive_research.reference import open_image, fetch, cache_path, cmd_index_new, connect
from archive_research.reference_catalogue import heldout_mask, neighbours_for
from archive_research.extractors.visual_features import recipe_for, space_id
import test_research_retrieval as retrieval_fixtures


class ReferenceIntegrationTests(unittest.TestCase):
    def test_holdout_removes_different_records_with_same_bytes_or_near_vectors(self):
        matrix=np.asarray([[1.,0.],[1.,0.],[.99,.141067],[0.,1.]],dtype=np.float32)
        meta=[{'inventory':'A1','preview_sha256':'same'},
              {'inventory':'Z999','preview_sha256':'same'},
              {'inventory':'B100','preview_sha256':'different'},
              {'inventory':'C200','preview_sha256':'other'}]
        mask=heldout_mask(0,matrix,meta,series_window=20,near_duplicate_similarity=.95,hide_series=True)
        self.assertEqual(mask.tolist(),[True,True,True,False])
        found=neighbours_for(matrix[0],list('abcd'),matrix,meta,4,mask)
        self.assertEqual([x['md_object_id'] for x in found],['d'])
        baseline=heldout_mask(0,matrix,meta,series_window=20,near_duplicate_similarity=.95,hide_series=False)
        self.assertEqual(baseline.tolist(),[True,True,False,False])

    def test_model_weights_and_framing_have_distinct_spaces(self):
        model=Source('models','clip','weights','models','local.safetensors',1,1,'a'*64)
        a=recipe_for({'settings':{'model':asdict(model)}})
        b=recipe_for({'settings':{'model':asdict(replace(model,sha256='b'*64))}})
        c=recipe_for({'settings':{'model':asdict(model),'framing':'center'}})
        self.assertEqual(len({space_id(x) for x in (a,b,c)}),3)
        self.assertEqual(a.record()['parameters']['preprocessing']['crop'],'letterbox')

    def test_reference_cache_reuses_bytes_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=Path(temp);url='https://berlin.museum-digital.de/data/berlin/test.jpg'
            cache_path(url,cache).write_bytes(b'existing preview')
            with patch('archive_research.reference.urlopen',side_effect=AssertionError('network')):
                self.assertEqual(fetch(url,cache),b'existing preview')
            with self.assertRaisesRegex(ValueError,'institutional'):
                fetch('https://example.com/test.jpg',cache)

    def test_multiframe_preview_refused_instead_of_silently_using_first(self):
        import io
        data=io.BytesIO()
        with Image.new('L',(2,2)) as im:
            im.save(data,format='TIFF',save_all=True,append_images=[im])
        with self.assertRaisesRegex(ValueError,'one image'):
            open_image(data.getvalue())

    def test_new_images_import_verified_receipts_without_loading_encoder(self):
        f=retrieval_fixtures.RetrievalTests();f.setUp();self.addCleanup(f.doCleanups)
        key=f.feature('query',[1.,0.])
        row=json.loads((f.output/(key+'.json')).read_text())
        from archive_research import Result
        recipe=Result.from_record(row['result']).recipe
        config=f.base/'roots.toml'
        config.write_text(f'[roots.source]\npath={json.dumps(str(f.input))}\naccess="read"\n'
                          f'[roots.artifacts]\npath={json.dumps(str(f.output))}\naccess="work"\n')
        selection=f.base/'selected.json';selection.write_text(json.dumps({'keys':[key]}))
        work=f.base/'reference';work.mkdir()
        args=SimpleNamespace(roots=config,selection=selection,work_root=work)
        # A real synthetic two-dimensional recipe exercises receipt import.
        with patch('archive_research.reference.configured_recipe',return_value=recipe), \
             patch('archive_research.reference.Encoder',side_effect=AssertionError('must reuse existing vector')):
            self.assertEqual(cmd_index_new(args),0)
        with connect(work) as db:
            meta=json.loads(db.execute('SELECT meta_json FROM embeddings').fetchone()[0])
        self.assertEqual(meta['source']['object_id'],'query')
        self.assertEqual(meta['receipt_key'],key)


if __name__=='__main__':unittest.main()
