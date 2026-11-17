"""Retrieval binding, model-space isolation and unchanged source reuse."""
from dataclasses import asdict,replace
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_research_evidence as fixtures
from archive_research import Artifact,Recipe,Result,Transform,Source
from archive_research.retrieval import build_index,refresh_index,visual_search,text_search
from archive_research.extractors.visual_features import validate_inputs


class RetrievalTests(unittest.TestCase):
    setUp=fixtures.EvidenceTests.setUp
    receipt=fixtures.EvidenceTests.receipt
    item=fixtures.EvidenceTests.item

    def feature(self,object_id,vector,model='a'*64):
        page=replace(self.page,source=replace(self.page.source,object_id=object_id))
        recipe=Recipe.create('visual','1',encoder='synthetic',dimensions=2,
                             preprocessing={'synthetic':True},dependencies={},settings={'model':{'sha256':model}})
        key=recipe.cache_key(page);image=self.output/(key+'.png')
        image.write_bytes((self.input/'synthetic.png').read_bytes())
        feature={'vector':vector,'dimensions':2}
        raw=self.output/(key+'.visual.json');raw.write_text(json.dumps(feature))
        artifacts=tuple(Artifact('artifacts',p.name,kind,hashlib.sha256(p.read_bytes()).hexdigest()) for p,kind in
                        [(image,'processed-page'),(raw,'normalized-visual-embedding')])
        result=Result(page,recipe,'complete',.1,artifacts,transform=Transform(400,200,(1,0,0,0,1,0)))
        row={'result':result.record(),'words':[],'feature':feature,
             'artifact_stamps':{p.name:{'size_bytes':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in (image,raw)}}
        (self.output/(key+'.json')).write_text(json.dumps(row));return key

    def test_cosine_order_same_object_exclusion_and_model_isolation(self):
        q=self.feature('query',[1.,0.]);a=self.feature('match',[.8,.6]);b=self.feature('other',[0.,1.])
        incompatible=self.feature('different model',[1.,0.],'b'*64)
        index=build_index(self.roots,'artifacts',[q,a,b,incompatible])
        result=visual_search(index,q,2)
        self.assertEqual([h['target'] for h in result['hits']],[a,b])
        self.assertEqual(result['incompatible_space'],[incompatible])
        self.assertFalse(result['hits'][0]['identity_confirmed'])

    def test_text_lookup_preserves_literal_text_and_partial_coverage(self):
        index=build_index(self.roots,'artifacts',[self.key,'e'*64])
        result=text_search(index,'invented person')
        self.assertIn('Invented person',result['hits'][0]['snippet'])
        self.assertEqual(len(index['unavailable']),1)
        self.assertFalse(index['coverage']['whole_archive'])

    def test_modified_artifact_or_stored_index_is_refused(self):
        key=self.feature('query',[1.,0.]);index=build_index(self.roots,'artifacts',[key])
        tampered=json.loads(json.dumps(index));tampered['entries'][0]['vector']=[0.,1.]
        with self.assertRaisesRegex(ValueError,'changed'):
            refresh_index(self.roots,'artifacts',tampered)
        (self.output/(key+'.visual.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError,'changed'):
            refresh_index(self.roots,'artifacts',index)

    def test_nonfinite_or_unnormalized_vectors_not_searchable(self):
        for vector in ([2.,0.],[float('nan'),0.]):
            key=self.feature('invalid',vector)
            index=build_index(self.roots,'artifacts',[key])
            self.assertEqual(index['entries'],[]);self.assertEqual(len(index['unavailable']),1)

    def test_changed_model_refused_before_cached_features(self):
        model_file=self.input/'model.safetensors';model_file.write_bytes(b'original');st=model_file.stat()
        model=Source('models','synthetic','model','source',model_file.name,st.st_size,st.st_mtime_ns,
                     hashlib.sha256(model_file.read_bytes()).hexdigest())
        recipe=Recipe.create('visual','1',settings={'model':asdict(model),'region_origin':None})
        task={'page':asdict(self.page)}
        validate_inputs(self.roots,'artifacts',task,recipe)
        model_file.write_bytes(b'changed!')
        os.utime(model_file,ns=(st.st_atime_ns,st.st_mtime_ns))
        with self.assertRaises(ValueError):validate_inputs(self.roots,'artifacts',task,recipe)

    def test_layout_crop_origin_must_match_page_region_and_receipt(self):
        origin={'key':self.key,'figure':0,'receipt_sha256':'f'*64}
        recipe=Recipe.create('visual','1',settings={'model':{},'region_origin':origin})
        with patch('archive_research.extractors.visual_features.model_path',return_value=None):
            with self.assertRaisesRegex(ValueError,'Layout origin'):
                validate_inputs(self.roots,'artifacts',{'page':asdict(self.page)},recipe)


if __name__=='__main__':unittest.main()
