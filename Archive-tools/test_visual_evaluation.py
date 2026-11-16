import unittest
from types import SimpleNamespace
import numpy as np
from visual_evaluation import exact_year, series, identities, title_series, exclusion_mask, predict, summarize, evaluate, pool_counts, prevalence, matched_prior, subtract_counts, make_policy


def row(id='u#md:1', inventory='KS-5-BZ_0568-1', **fields):
    return {'id':id,'catalogue_record':{'inventory':inventory,**fields},'title':fields.get('title','')}


class EvaluationTests(unittest.TestCase):
    def test_exact_year_rejects_approximation_and_event_text(self):
        for value in ['ca. 1954','1960er Jahre','1960-1969','vor 1970','vermutlich 1959','Herstellung 1954; Auftrag 1953','1954?','']:
            self.assertIsNone(exact_year(value),value)
        for value in ['1954','16.06.1954','16. Juni 1954','Juni 1954']:
            self.assertEqual(exact_year(value),1954)

    def test_series_uses_main_inventory_not_frame_suffix(self):
        self.assertEqual(series(row())[-1],568)
        self.assertEqual(series(row(inventory='TFA-538105-2'))[-1],538105)
        self.assertIsNone(series(row(inventory='unknown')))

    def test_mask_excludes_self_identity_hash_and_series(self):
        rs=[row(),row('other#md:1',md_object_id='1'),row('u#md:2',inventory='KS-5-BZ_0570-2'),row('u#md:3',inventory='KS-5-BZ_0800')]
        rs[0]['catalogue_record']['md_object_id']='1'
        ids=[identities(r) for r in rs];keys=[series(r) for r in rs];titles=[title_series(r) for r in rs]
        self.assertEqual(exclusion_mask(0,rs,ids,keys,titles,False,20).tolist(),[True,True,False,False])
        self.assertEqual(exclusion_mask(0,rs,ids,keys,titles,True,20).tolist(),[True,True,True,False])
        rs[3]['catalogue_record']['source_sha256']='same';rs[0]['catalogue_record']['final_sha256']='sha256:same'
        self.assertTrue(identities(rs[3]) & identities(rs[0]))

    def test_numbered_title_exclusion(self):
        rs=[row(title='Labor, Bild 1; Foto'),row('u#md:2',inventory='KS-8-BZ_9999',title='Labor, Bild 2; Foto')]
        self.assertTrue(exclusion_mask(0,rs,[identities(r) for r in rs],[series(r) for r in rs],[title_series(r) for r in rs],True,20)[1])

    def test_prediction_threshold_and_voters(self):
        args=SimpleNamespace(min_similarity=.5,min_support=.34,temperature=.05)
        rs=[row(tags='Industry; Portrait',date_text='1954'),row('u#2',tags='Industry',date_text='1956')]
        pred=predict(rs,[0,1],[.9,.9],args)
        self.assertEqual([p['value'] for p in pred['tags']],['industry'])
        self.assertEqual(pred['date'][0]['value'],1954)
        self.assertEqual(predict(rs,[0,1],[.4,.4],args)['tags'],[])

    def test_missing_labels_unscorable_and_abstentions_reduce_coverage(self):
        records=[{'truth':{'tags':['a','b']},'prediction':{'tags':[{'value':'a','band':'strong'}]}},
                 {'truth':{'tags':['a']},'prediction':{'tags':[]}},
                 {'truth':{'tags':[]},'prediction':{'tags':[{'value':'x','band':'weak'}]}}]
        s=summarize(records,'prediction','tags')
        self.assertEqual(s['scorable'],2);self.assertEqual(s['coverage'],.5)
        self.assertEqual(s['agreement'],1);self.assertEqual(s['recall'],1/3)
        self.assertEqual(s['unscorable_predictions'],1)

    def test_date_error_is_not_range_midpoint(self):
        records=[{'truth':{'date':1950},'prediction':{'date':[{'value':1960,'band':'weak'}]}}]
        s=summarize(records,'prediction','date');self.assertEqual(s['mae_years'],10);self.assertEqual(s['agreement'],0)

    def test_complete_evaluation_uses_cached_vectors_and_excludes_queries(self):
        import tempfile,json,contextlib,io
        from pathlib import Path
        from visual_research import digest
        from archive_common import ReviewError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'embeddings').mkdir();rs=[];items=[]
            for i in range(8):
                unit='a' if i<4 else 'b'
                r=row(f'/units/{unit}#md:{i}',inventory=f'INV-{100*i}',tags=unit,date_text='1950' if i<4 else '1970')
                r.update(status='ok',preview_sha256=f'preview-{i}');rs.append(r)
                key=digest({'config':{},'image':r['preview_sha256']});items.append({'id':r['id'],'key':key})
                np.save(root/f'embeddings/{key}.npy',np.array([1.,0.]) if i<4 else np.array([0.,1.]))
            manifest={'references':rs,'candidates':[]}
            (root/'manifest.json').write_text(json.dumps(manifest))
            (root/'embedding-manifest.json').write_text(json.dumps({'config':{},'manifest_digest':digest(manifest),'items':items}))
            args=SimpleNamespace(work_root=root,sample=4,seed=47,k=3,series_window=20,min_similarity=.5,min_support=.34,temperature=.05,name='test-eval')
            with contextlib.redirect_stdout(io.StringIO()):evaluate(args)
            result=json.loads((root/'test-eval.json').read_text())
            self.assertEqual(result['sample_by_unit'],{'a':2,'b':2})
            self.assertEqual(result['summary']['all']['series_hidden']['prediction']['tags']['agreement'],1)
            cases=[json.loads(x) for x in (root/'test-eval-cases.jsonl').read_text().splitlines()]
            self.assertEqual(len(cases),12)
            for case in cases:self.assertNotIn(case['query_id'],[x['id'] for x in case['neighbours']])
            for case in cases:
                if case['mode']=='unit_hidden':
                    self.assertTrue(all(x['id'].split('#')[0]!=case['unit'] for x in case['neighbours']))
                    self.assertTrue(all(not values for values in case['unit_baseline'].values()))
                    self.assertTrue(all(not values for values in case['by_tier']['all']['unit_matched'].values()))
            with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(ReviewError):evaluate(args)

    def test_counted_prior_matches_original_uniform_vote(self):
        from visual_evaluation import FIELDS,labels
        args=SimpleNamespace(min_similarity=.5,min_support=.34,temperature=.05)
        rs=[row(f'u#{i}',tags='a; b' if i<3 else 'a; c',places='Berlin',date_text=str(1950+i),object_type='Foto') for i in range(5)]
        meta=[{f:exact_year(r['catalogue_record'].get('date_text')) if f=='date' else sorted(labels(r,f)) for f in FIELDS} for r in rs]
        counts=pool_counts(meta,list(range(5)))
        self.assertEqual(prevalence(counts,args),predict(rs,list(range(5)),np.ones(5),args,uniform=True))
        reduced=subtract_counts(counts,pool_counts(meta,[0]))
        self.assertEqual(prevalence(reduced,args),predict(rs,list(range(1,5)),np.ones(4),args,uniform=True))

    def test_matched_prior_uses_output_count_not_query_truth(self):
        counts={'n':10,'fields':{'tags':{'common':9,'rare':1},'places':{},'date':{1950:8,1970:2},'object_type':{}}}
        pred={'tags':[{'value':'rare'}],'places':[],'date':[{'value':1970}],'object_type':[]}
        result=matched_prior(counts,pred)
        self.assertEqual(result['tags'][0]['value'],'common')
        self.assertEqual(len(result['tags']),1)
        self.assertEqual(result['date'][0]['value'],1950)

    def test_policy_respects_unit_scope_baseline_gain_and_date_fallback(self):
        from copy import deepcopy
        good={'gain_vs_pool':{'queries':30,'gain':.2,'bootstrap_95':[.1,.3]},
              'gain_vs_unit':{'queries':30,'gain':.2,'bootstrap_95':[.1,.3]}}
        comp={u:{'series_hidden':{tier:{f:deepcopy(good) for f in ('tags','places','date','object_type')}}}
              for u,tier in [('tfa','high_medium'),('kurt-schwarz','high')]}
        comp['tfa']['series_hidden']['high_medium']['places']['gain_vs_unit']['gain']=0
        policy=make_policy(comp)
        self.assertTrue(policy['units']['tfa']['tags']['enabled_for_review_proposals'])
        self.assertFalse(policy['units']['tfa']['places']['enabled_for_review_proposals'])
        self.assertEqual(policy['units']['kurt-schwarz']['tags']['allowed_tiers'],['high'])
        self.assertFalse(policy['units']['kurt-schwarz']['date']['enabled_for_review_proposals'])
        self.assertFalse(policy['units']['kurt-schwarz']['places']['enabled_for_review_proposals'])
        fallback=policy['heterogeneous_date_workflow']['unit_median_fallback']
        self.assertFalse(fallback['observed_date']);self.assertTrue(fallback['requires_explicit_choice'])
        comp['kurt-schwarz']['series_hidden']['high']['tags']['gain_vs_pool']['queries']=3
        self.assertFalse(make_policy(comp)['units']['kurt-schwarz']['tags']['enabled_for_review_proposals'])
        comp['tfa']['series_hidden']['high']=deepcopy(comp['tfa']['series_hidden']['high_medium'])
        comp['tfa']['series_hidden']['high_medium']['tags']['gain_vs_unit']['gain']=-.1
        self.assertEqual(make_policy(comp)['units']['tfa']['tags']['allowed_tiers'],['high'])


if __name__=='__main__':unittest.main()
