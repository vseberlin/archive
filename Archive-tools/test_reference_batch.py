"""Offline regression tests; no model calls or real archive mutation."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from PIL import Image

import reference_batch as rb


class ReferenceBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.registry={'sources':[{'id':'commons','domains':['commons.wikimedia.org']}]}
        self.fetcher=rb.Fetcher(self.root,'test',self.registry)

    def test_private_unknown_and_credential_urls_rejected(self):
        for url in ['file:///etc/passwd','http://127.0.0.1/x','http://example.org/x',
                    'https://user:pass@commons.wikimedia.org/x','https://commons.wikimedia.org:8783/x']:
            with self.assertRaises(ValueError):self.fetcher.validate(url)
        with patch.object(rb.socket,'getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'Non-public'):
                self.fetcher.validate('https://commons.wikimedia.org/x')

    def test_commons_keeps_record_identity_and_caption(self):
        data={'query':{'pages':{'12':{'pageid':12,'title':'File:Courtyard.jpg','index':1,
            'imageinfo':[{'url':'https://upload.wikimedia.org/a.jpg','descriptionurl':'https://commons.wikimedia.org/wiki/File:Courtyard.jpg',
                'extmetadata':{'ImageDescription':{'value':'<b>Named courtyard</b>'},'DateTimeOriginal':{'value':'1967'}}}]}}}}
        with patch.object(self.fetcher,'fetch',return_value=(json.dumps(data).encode(),{'url':'API'})):
            row=self.fetcher.commons('Courtyard Berlin')[0]
        self.assertEqual(row['record_id'],'12');self.assertEqual(row['caption'],'Named courtyard')
        self.assertEqual(row['reference_date'],'1967')
        self.assertEqual(row['record_url'],'https://commons.wikimedia.org/wiki/File:Courtyard.jpg')

    def test_claims_cannot_cite_undelivered_images(self):
        runner=rb.Runner.__new__(rb.Runner)
        a={'status':'supported','value':'Location','explanation':'Match','reference_ids':['R2']}
        result={'location':a,'photograph_date':{'status':'unresolved','reference_ids':[]},
            'depicted_object':{'status':'unresolved','reference_ids':[]}, 'event':{'status':'unresolved','reference_ids':[]},
            'hypotheses':[],'comparisons':[{'reference_id':'R1'}]}
        with self.assertRaisesRegex(ValueError,'absent'):runner.check_result(result,[{'id':'R1'}])
        a['reference_ids']=['R1'];self.assertIs(runner.check_result(result,[{'id':'R1'}]),result)
        result['comparisons']=[]
        with self.assertRaises(ValueError):runner.check_result(result,[{'id':'R1'}])

    def test_attach_preserves_model_ocr_consolidation_and_human_edits(self):
        runner=rb.Runner.__new__(rb.Runner);runner.review=runner.root=self.root;runner.args=types.SimpleNamespace(prefix='test')
        record={'object_id':'o','candidate_id':'source','source_path':'/source/photo.tif','source_stamp':{'size_bytes':1},
                'preview_sha256':'abc','result':{'title':'Original'},'ocr_research':{'runs':['raw']},
                'consolidated_proposal':{'title':'Correction'},'sculpture_identification':{'title':'Jaguar'}}
        rb.write(self.root/'model-proposals.json',{'metadata_adopted':False,'records':[record]})
        decision=b'{"human_edit":"Keep this"}\n';(self.root/'decisions.jsonl').write_bytes(decision)
        row={'number':1,'status':'compared','comparison':{'result':{'summary':'Tentative'}},'references':[]}
        runner.attach(record,row)
        updated=rb.read(self.root/'model-proposals.json')['records'][0]
        check=updated.pop('reference_check')
        self.assertEqual(updated,record);self.assertFalse(check['metadata_adopted'])
        self.assertEqual((self.root/'decisions.jsonl').read_bytes(),decision)
        wrong=deepcopy(record);wrong['preview_sha256']='changed'
        with self.assertRaisesRegex(ValueError,'binding changed'):runner.attach(wrong,row)

    def test_exterior_ranking_prefers_buildings_over_plaques_and_interiors(self):
        rows=[{'title':'Gedenktafel Brauerei','caption':'memorial plaque'},
              {'title':'Brauerei Keller','caption':'interior'},
              {'title':'Brauerei Hof','caption':'courtyard facade'}]
        ranked=rb.Runner.rank_records(rows,'Gepflasterter Gewerbehof mit Schornstein und Fassade')
        self.assertEqual(ranked[0]['title'],'Brauerei Hof')

    def test_gather_deduplicates_identical_downloads(self):
        runner=rb.Runner.__new__(rb.Runner);runner.fetcher=self.fetcher
        records=[{'source_id':'commons','title':'Photo', 'record_url':'https://commons.wikimedia.org/wiki/File:'+str(i),
                  'candidate_image_url':'https://upload.wikimedia.org/'+str(i)} for i in range(3)]
        with patch.object(self.fetcher,'image',return_value={'image_sha256':'same'}):
            refs,docs,issues=runner.gather(records,{},set(),1)
        self.assertEqual(len(refs),1);self.assertEqual(issues,[])



def assessment(status='unresolved', ids=(), value=None):
    return {'status':status, 'value':value, 'explanation':'Evidence assessment', 'reference_ids':list(ids)}


def comparison(refs=(), docs=(), more=False):
    result={k:assessment() for k in ('depicted_object','location','event','photograph_date')}
    if docs: result['depicted_object']=assessment('supported',docs,'Jaguar')
    result.update(observations=[], comparisons=[{'reference_id':i,'matches':'pose','differences':'base','limitation':'viewpoint'} for i in refs],
        hypotheses=[], summary='Object known; location/date unresolved', limitations=[],
        needs_more_references=more, next={'reason':'Need another viewpoint' if more else '',
                                         'search_terms':['Jaguar base'] if more else [],'place_names':[]})
    return result


def receipt(result, tokens=100, errors=()):
    return {'result':result, 'errors':list(errors), 'usage':{'total_tokens':tokens}, 'provenance_available':True}


class RefactorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.registry={'selection_policy':{'initial_sources_per_image':2,'maximum_total_sources_per_image':4,
            'initial_search_query_budget':6,'routing':{'building':['commons','museum','denkmal'],'unresolved':['museum','denkmal']}},
            'sources':[{'id':'commons','domains':['commons.wikimedia.org']},
                       {'id':'museum','domains':['museum.example'],'query_template':'site:museum.example {object} {year}'},
                       {'id':'denkmal','domains':['denkmaldatenbank.berlin.de']}]}
        self.runner=rb.Runner.__new__(rb.Runner)
        self.runner.root=self.runner.review=self.root
        self.runner.args=types.SimpleNamespace(prefix='test',model='test-model',retry_failed=False)
        self.runner.registry=self.registry
        self.runner.fetcher=rb.Fetcher(self.root,'test',self.registry)
        self.runner.settings={'max_rounds':2,'max_item_tokens':30000,'max_average_tokens':30000,
                              'compare_effort':'low','web_search':True,'native_crops':False}
        self.preview=self.root/'preview.jpg';Image.new('RGB',(400,300),'gray').save(self.preview)
        self.item={'object_id':'object','preview_path':str(self.preview),'preview_sha256':rb.digest(self.preview),
                   'title':'Old guess','description':'','hypotheses':[],'source_path':str(self.preview),
                   'source_stamp':rb.stamp(self.preview)}
        self.discovery={'visual_features':['low animal pose'],'crop_boxes':[],'subject':'building','searchable':True,
                        'reason':'distinctive object','place_names':[],'candidate_queries':[],'search_terms':['Jaguar'],'limitations':[]}

    def test_adaptive_search_sees_pixels_and_follows_comparison_in_fresh_call(self):
        self.runner.settings['discovery_mode']='adaptive'
        calls=[]
        def call(stem,prompt,schema,images=(),**kwargs):
            calls.append((stem,prompt,list(images),kwargs))
            if stem.endswith('adaptive'):
                n=1 if 'r1-' in stem else 2
                url=f'https://new{n}.example/record'
                return {**receipt({'research_routing':{'primary':'objects_art','secondary':[],
                    'confidence':'high','reason':'Sculpture visible'},'visual_features':['machine housing'],'crop_boxes':[],
                    'records':[{'source_id':'external','url':url,'relevance':'roller machine'}],
                    'queries_reported':['machine housing'],'limitations':[]}),
                    'observed_urls':[url], 'search_queries_executed':2}
            return receipt(comparison(refs=['R1'],more='r1-' in stem))
        pool=[{'image_sha256':'photo','local_image':str(self.preview),'record_url':'https://new.example/record'}]
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',return_value=(pool,[],[])):
            row=self.runner.process(self.item,1)
        self.assertEqual(len(calls),4)
        self.assertTrue(all(c[2][0]==self.preview for c in calls))
        self.assertIn('Jaguar base',calls[2][1])
        self.assertEqual(len(calls[1][2]),2) # target plus individual reference, no sheet
        self.assertEqual(row['queries_used'],4)
        self.assertEqual(row['tokens'] if 'tokens' in row else rb.row_tokens(row),400)
        self.assertEqual(row['status'],'compared')
        self.assertIn('new2.example',self.runner.fetcher.domains)
        self.assertEqual(row['research_routing']['primary'],'objects_art')
        self.assertIn('Objects and artworks',calls[2][1])

    def test_adaptive_keeps_reviewer_route_when_model_proposes_another(self):
        self.runner.settings.update(discovery_mode='adaptive',max_rounds=1)
        item={**self.item,'research_routing':{'primary':'events_celebrations','secondary':[],
            'confidence':'high','reason':'Reviewer','reviewer_override':True}}
        result={'research_routing':{'primary':'buildings_places','secondary':[], 'confidence':'medium','reason':'Facade'},
                'records':[],'visual_features':[],'crop_boxes':[],'queries_reported':[]}
        with patch.object(self.runner,'call',return_value={**receipt(result),'observed_urls':[]}):
            row=self.runner.process(item,1)
        self.assertEqual(row['research_routing']['primary'],'events_celebrations')
        self.assertEqual(row['rounds'][0]['suggested_routing']['primary'],'buildings_places')

    def test_adaptive_missing_url_provenance_is_integration_error(self):
        self.runner.settings['discovery_mode']='adaptive'
        search=receipt({'visual_features':[],'crop_boxes':[],'records':[], 'queries_reported':[], 'limitations':[]})
        search['provenance_available']=False
        with patch.object(self.runner,'call',return_value=search), patch.object(self.runner.fetcher,'record') as fetch:
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'integration_error');fetch.assert_not_called()

    def test_lazy_images_skip_placeholders_and_deduplicate_sizes(self):
        p=rb.Page();p.feed('''<img src="data:image/svg+xml,placeholder" data-src="/wp-content/uploads/machine-300x200.jpg"
            srcset="/wp-content/uploads/machine-300x200.jpg 300w, /wp-content/uploads/machine-1800x1200.jpg 1800w"
            alt="roller machine"><img data-original="/other.jpg" src="data:image/svg+xml,placeholder">''')
        images=rb.ranked_page_images(p.images,'roller machine')
        self.assertEqual([i['src'] for i in images],['/wp-content/uploads/machine-1800x1200.jpg','/other.jpg'])

    def test_crop_banner_removed_before_native_coordinates(self):
        boxes=[{'label':'mark','x':.2,'y':.2,'w':.3,'h':.4}]
        box=rb.photo_crop_boxes(boxes,(1800,1900),1)[0]
        self.assertAlmostEqual(box['y'],280/1800)
        self.assertAlmostEqual(box['h'],760/1800)

    def test_video_graphics_and_double_extension_thumbnails_do_not_take_slots(self):
        urls=['/images/YouTube_23392.png','/images/lbl-filmklappe.png',
              '/wp-content/uploads/machine-300x190.jpg.webp','/wp-content/uploads/machine-scaled.jpg']
        result=rb.ranked_page_images([{'src':u,'alt':'','caption':''} for u in urls],'machine')
        self.assertEqual([r['src'] for r in result],[urls[-1]])

    def test_observed_commons_record_is_resolved_directly_not_searched(self):
        for url in ['https://commons.wikimedia.org/wiki/File:MIAG_Walzenstuhl_.jpg',
                    'https://commons.wikimedia.org/wiki/File%3AMIAG_Walzenstuhl_.jpg']:
            with patch.object(self.runner.fetcher,'commons_api',return_value=[]) as api:
                self.runner.fetcher.record({'url':url})
            self.assertEqual(api.call_args.args[0],{'titles':'File:MIAG_Walzenstuhl_.jpg','redirects':1})

    def test_url_provenance_accepts_colon_encoding_without_weakening_path_boundaries(self):
        key=rb.url_provenance_key
        self.assertEqual(key('https://commons.wikimedia.org/wiki/File%3AMIAG.jpg'),
                         key('https://commons.wikimedia.org/wiki/File:MIAG.jpg'))
        self.assertNotEqual(key('https://example.org/a%2fb'),key('https://example.org/a/b'))
        self.assertNotEqual(key('https://example.org/a?q=%3A'),key('https://example.org/a?q=:'))

    def test_native_positive_recipe_preserves_source_and_rotates_after_inversion(self):
        source=self.root/'negative.tif';Image.new('RGB',(60,40),(10,20,30)).save(source)
        preview=self.root/'positive.jpg';Image.new('RGB',(40,60),(245,235,225)).save(preview)
        original=source.read_bytes()
        item={**self.item,'source_path':str(source),'source_stamp':rb.stamp(source),'preview_path':str(preview),
              'preview_sha256':rb.digest(preview),'preview_recipe':{'visual_checked':True,'page':0,
              'rotation':-90,'polarity':'inverted research view'}}
        self.runner.settings.update(native_crops=True,discovery_mode='adaptive')
        with patch.object(rb,'protected_source',return_value=source):
            path,note=self.runner.research_input(item,'positive-test')
        with Image.open(path) as im:
            self.assertEqual(im.width,40)
            self.assertTrue(all(abs(a-b)<=3 for a,b in zip(im.getpixel((20,im.height-10)),(245,235,225))))
        self.assertEqual(source.read_bytes(),original)

    def test_production_schema_has_numeric_boxes_and_text_assessments(self):
        d=deepcopy(self.discovery);d['crop_boxes']=[{'label':'base','x':.1,'y':.2,'w':.4,'h':.5}]
        rb.jsonschema.validate(d,rb.DISCOVERY)
        rb.jsonschema.validate(comparison(docs=['D1']),rb.COMPARISON)
        d['crop_boxes'][0]['x']='0.1'
        with self.assertRaises(rb.jsonschema.ValidationError):rb.jsonschema.validate(d,rb.DISCOVERY)

    def test_document_can_support_object_without_image_or_location(self):
        result=comparison(docs=['D1'])
        self.runner.check_result(result,[],[{'id':'D1'}])
        self.assertEqual(result['location']['status'],'unresolved')
        result['event']['reference_ids']=['D9']
        with self.assertRaisesRegex(ValueError,'absent'):self.runner.check_result(result,[],[{'id':'D1'}])

    def test_multiple_views_from_one_record_and_failed_first_download(self):
        rows=[{'source_id':'museum','record_url':'https://museum.example/object','title':'Jaguar',
               'candidate_image_url':'https://museum.example/'+str(i)} for i in range(3)]
        def download(url):
            if url.endswith('/0'):raise ValueError('temporary failure')
            return {'image_sha256':hashlib.sha256(url.encode()).hexdigest(),'local_image':str(self.preview)}
        with patch.object(self.runner.fetcher,'image',side_effect=download):
            pool,docs,issues=self.runner.gather(rows,{},set(),1)
        self.assertEqual(len(pool),2);self.assertEqual(len(issues),1)
        self.assertEqual(len({r['record_url'] for r in pool}),1)

    def test_revisited_record_does_not_promote_decorations(self):
        rows=[{'source_id':'museum','record_url':'https://museum.example/object','title':'Machine',
               'candidate_image_url':'https://museum.example/'+str(i)} for i in range(4)]
        with patch.object(self.runner.fetcher,'image') as fetch:
            pool,docs,issues=self.runner.gather(rows,{},set(r['candidate_image_url'] for r in rows[:2]),2)
        self.assertEqual(pool,[]);fetch.assert_not_called()

    def test_named_candidate_drawing_survives_generic_type_references(self):
        result=comparison()
        result['depicted_object'].update(status='supported',reference_ids=['R1','R2','R3','R4'])
        result['hypotheses']=[{'candidate':'Wetzig H','status':'tentative','reference_ids':['R4','D3']}]
        refs=[{'id':'R'+str(i),'record_url':'https://museum.example/'+str(i)} for i in range(1,5)]
        docs=[{'id':'D3','record_url':refs[-1]['record_url']}]
        kept,documents=rb.retain_evidence(result,refs,docs)
        self.assertEqual(kept[0]['id'],'R4');self.assertEqual(documents,docs)

    def test_registry_scope_guides_search_without_restricting_discovery(self):
        self.registry['selection_policy']['subject_routes']={'machinery_industry':['museum']}
        self.registry['sources'][1]['best_for']=['grain milling only']
        prompt=self.runner.adaptive_prompt({**self.item,'research_routing':{'primary':'machinery_industry','secondary':[]}},[])
        self.assertIn('grain milling only',prompt)
        self.assertIn('New candidates and public sources are allowed',prompt)
        self.assertIn('targeted search',prompt)

    def test_object_asset_is_not_replaced_by_footer_and_page_text(self):
        url='https://museum.example/objekte/anleitung-walzenstuhl-modell-h/'
        body=b'<title>Walzenstuhl</title><img src="/anleitung-walzenstuhl-modell-h-123.jpg"><img src="/footer.jpg"><img src="/ministry.png" alt="Logo ministry">'
        with patch.object(self.runner.fetcher,'fetch',return_value=(body,{'content_type':'text/html','final_url':url})):
            records=self.runner.fetcher.record({'url':url,'source_id':'museum','relevance':'machine'})
        photos=[r for r in records if r['evidence_type']=='reference_photo']
        self.assertEqual(len(photos),1);self.assertEqual(photos[0]['caption'],'')
        self.assertIn('verify association',photos[0]['caption_caution'])

    def test_authoritative_slot_reserved_only_for_plausible_shortlist_entries(self):
        candidates=[{'source_id':'commons','image_sha256':str(i)} for i in range(4)]
        museum={'source_id':'museum','image_sha256':'museum'}
        refs=rb.select_references(candidates+[museum],[],4)
        self.assertIn(museum,refs)
        self.assertEqual(rb.select_references(candidates,[],4),candidates)

    def test_registry_routes_and_templates_drive_search(self):
        first=self.runner.route('building',set(),1)
        self.assertEqual([s['id'] for s in first],['commons','museum'])
        second=self.runner.route('building',{'commons','museum'},2)
        self.assertEqual([s['id'] for s in second],['denkmal'])
        self.assertEqual(rb.research_queries(first[1],['Jaguar Harth']),['site:museum.example Jaguar Harth'])

    def test_observed_urls_exclude_model_assertions_and_unobserved_open_requests(self):
        events=[{'type':'item.completed','item':{'type':'agent_message','text':'https://invented.example/x'}},
                {'type':'item.completed','item':{'type':'web_search','action':{'url':'https://guessed.example'},
                 'results':[{'url':'https://museum.example/record'}]}}]
        self.assertEqual(rb.observed_urls(events),['https://museum.example/record'])

    def test_portal_search_filters_invented_and_wrong_source_urls(self):
        result={'records':[{'source_id':'museum','url':u,'relevance':'Jaguar'} for u in
                          ['https://museum.example/real','https://museum.example/invented','https://elsewhere.example/real']],
                'limitations':[]}
        response={**receipt(result),'observed_urls':['https://museum.example/real','https://elsewhere.example/real']}
        row={}
        with patch.object(self.runner,'call',return_value=response):
            found=self.runner.search_portals('web',[{'source_id':'museum','query':'q'}],row,'portal_search',[])
        self.assertEqual([r['url'] for r in found],['https://museum.example/real'])
        self.assertEqual(row['rejected_urls'],['https://museum.example/invented'])

    def test_denkmal_only_uses_observed_record_links(self):
        page=b'<a href="daobj.php?obj_dok_nr=09012345">Record</a><a href="other.php">Other</a>'
        meta={'final_url':'https://denkmaldatenbank.berlin.de/suchresultat.php'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(page,meta)) as fetch:
            found=self.runner.fetcher.denkmal('Schultheiß')
        self.assertEqual(len(found),1)
        self.assertIn('stichwort=Schulthei%C3%9F',fetch.call_args.args[0])
        self.assertTrue(found[0]['url'].endswith('obj_dok_nr=09012345'))

    def test_records_preserve_text_and_prioritize_full_denkmal_images(self):
        page=b'<title>Actual record</title>Obj.-Dok.-Nr. 09012345 Material: Bronze <img src="/logo.png"><a href="/media/uploads/Pre/a.jpg"><img src="/media/uploads/Pre/a_klein.jpg" alt="side"></a>'
        meta={'content_type':'text/html','final_url':'https://denkmaldatenbank.berlin.de/daobj.php?obj_dok_nr=09012345'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(page,meta)):
            rows=self.runner.fetcher.record({'source_id':'denkmal','url':meta['final_url'],'relevance':'Bronze'})
        self.assertEqual(rows[0]['evidence_type'],'catalogue_record')
        self.assertIn('Bronze',rows[0]['excerpt'])
        self.assertEqual(rows[0]['title'],'Actual record')
        self.assertTrue(rows[1]['candidate_image_url'].endswith('/a.jpg'))
        self.assertEqual(len([r for r in rows if r.get('candidate_image_url')]),1)

    def test_pdf_preserves_page_and_excerpt_without_images(self):
        meta={'content_type':'application/pdf','final_url':'https://museum.example/finding-aid.pdf'}
        extracted=types.SimpleNamespace(returncode=0,stdout=b'Unrelated page\fInventory B III 106 Jaguar Harth 1928\f')
        with patch.object(self.runner.fetcher,'fetch',return_value=(b'%PDF-test',meta)), \
             patch.object(rb.shutil,'which',return_value='/usr/bin/pdftotext'), \
             patch.object(rb.subprocess,'run',return_value=extracted):
            rows=self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],'relevance':'Jaguar Harth'})
        self.assertEqual(rows[0]['page_number'],2)
        self.assertEqual(rows[0]['evidence_type'],'archival_document')
        self.assertIn('B III 106',rows[0]['excerpt'])

    def test_failure_cache_expires_including_403(self):
        url='https://museum.example/a'
        key=hashlib.sha256(url.encode()).hexdigest()[:24]
        meta=self.root/f'test-http-{key}.json'
        rb.write(meta,{'error':'HTTP 403','retry_after':rb.time.time()+100})
        with patch.object(self.runner.fetcher,'validate'),patch.object(rb.urllib.request,'build_opener') as opener:
            with self.assertRaisesRegex(ValueError,'403'):self.runner.fetcher.fetch(url)
            opener.assert_not_called()
            rb.write(meta,{'error':'HTTP 403','retry_after':0})
            response=types.SimpleNamespace(read=lambda _:b'ok',url=url,headers={'Content-Type':'text/html'})
            opener.return_value.open.return_value.__enter__.return_value=response
            data,_=self.runner.fetcher.fetch(url)
        self.assertEqual(data,b'ok')

    def test_ambiguous_wikidata_name_never_drives_geosearch(self):
        data={'search':[{'id':'Q1','label':'Opera'},{'id':'Q2','label':'Opera'}]}
        with patch.object(self.runner.fetcher,'fetch',return_value=(json.dumps(data).encode(),{})) as fetch:
            self.assertIsNone(self.runner.fetcher.wikidata('Opera'))
            self.assertEqual(fetch.call_count,1)

    def test_retrieval_uses_registry_portal_even_when_commons_empty(self):
        row={'number':1,'discovery':receipt(self.discovery),'rounds':[{'round':1}]}
        response={**receipt({'records':[],'limitations':[]}),'observed_urls':[]}
        with patch.object(self.runner.fetcher,'commons',return_value=[]),patch.object(self.runner,'call',return_value=response) as call:
            candidates,issues=self.runner.retrieve(self.discovery,self.item,1,row,[],set(),[6])
        self.assertEqual(candidates,[]);self.assertEqual(issues,[])
        self.assertIn('site:museum.example Jaguar',call.call_args.args[1])
        self.assertEqual(row['rounds'][0]['queries_remaining'],4)

    def test_wider_search_observed_external_url_uses_remaining_source_slot(self):
        row={'number':1,'rounds':[{'round':2}]}
        found={'source_id':'external','url':'https://newmuseum.example/object','relevance':'Jaguar'}
        response={**receipt({'records':[found],'limitations':[]}),'observed_urls':[found['url']]}
        used={'commons','museum'}
        with patch.object(self.runner.fetcher,'denkmal',return_value=[]), \
             patch.object(self.runner,'call',return_value=response), \
             patch.object(self.runner.fetcher,'record',return_value=[{'evidence_type':'catalogue_record'}]) as record:
            candidates,issues=self.runner.retrieve(self.discovery,self.item,2,row,[],used,[4])
        self.assertTrue(candidates)
        self.assertIn('external:newmuseum.example',used)
        self.assertIn('newmuseum.example',self.runner.fetcher.domains)
        self.assertEqual(record.call_count,1)

    def test_negative_history_preserves_contradiction_and_status(self):
        result=comparison(refs=['R1'],more=True)
        result['hypotheses']=[{'candidate':'Bear','status':'contradicted','assessment':'Wrong pose','reference_ids':['R1']}]
        memory=rb.round_memory(1,result,[{'id':'R1','record_url':'https://museum.example/bear'}])
        self.assertEqual(memory['hypotheses'][0]['status'],'contradicted')
        self.assertEqual(memory['comparisons'][0]['contradictions'],'base')
        self.assertNotIn('reference_ids',memory['hypotheses'][0])

    def fake_retrieve(self, candidates=(), issues=()):
        def retrieve(queries,item,rnd,row,history,used,budget):
            used.add('museum');budget[0]-=1
            row['rounds'][-1].update(source_searches=[],sources_used=sorted(used),queries_remaining=budget[0])
            return deepcopy(list(candidates)),list(issues)
        return retrieve

    def test_empty_success_is_no_results_not_access_limited(self):
        with patch.object(self.runner,'call',return_value=receipt(self.discovery)) as call, \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve()):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'no_results');self.assertEqual(call.call_count,1)

    def test_blocked_retrieval_is_access_limited(self):
        with patch.object(self.runner,'call',return_value=receipt(self.discovery)), \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve(issues=[{'error':'403'}])):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'access_limited')

    def test_document_only_processing_and_followup_keeps_prior_success(self):
        doc={'source_id':'museum','record_url':'https://museum.example/object','title':'Jaguar',
             'evidence_type':'catalogue_record','excerpt':'B III 106','page_number':None}
        calls=[receipt(self.discovery),receipt(comparison(docs=['D1'],more=True)),receipt(None,errors=['timeout'])]
        with patch.object(self.runner,'call',side_effect=calls) as call, \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve([doc])):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'compared');self.assertEqual(row['stop_reason'],'failed')
        self.assertEqual(row['comparison']['result']['depicted_object']['value'],'Jaguar')
        self.assertIn('PREVIOUS ROUNDS',call.call_args_list[-1].args[1])
        self.assertEqual(row['final_round'],1)

    def test_token_limit_after_discovery_prevents_retrieval(self):
        with patch.object(self.runner,'call',return_value=receipt(self.discovery,30001)), \
             patch.object(self.runner,'retrieve') as retrieve:
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'usage_limit');retrieve.assert_not_called()

    def test_native_crop_rejects_unchecked_recipe_before_source_access(self):
        self.runner.settings['native_crops']=True
        with patch.object(rb,'protected_source') as source:
            with self.assertRaisesRegex(ValueError,'visually checked'):
                self.runner.native_details(self.item,[{'label':'base','x':0,'y':0,'w':.5,'h':.5}],'001')
            source.assert_not_called()

    def test_crop_from_preview_is_never_upscaled(self):
        paths=rb.make_crops(self.preview,[{'label':'detail','x':0,'y':0,'w':.5,'h':.5}],self.root/'detail')
        with Image.open(paths[0]) as image:self.assertEqual(image.width,200)

    def test_completed_call_reused_and_orphan_attempt_retained(self):
        (self.root/'test-instructions.txt').write_text('Instructions')
        old=self.root/'test-001-discover-events.jsonl';old.write_text('{"type":"turn.started"}\n')
        before=old.read_bytes()
        def popen(cmd,**kwargs):
            result_path=Path(cmd[cmd.index('-o')+1]);rb.write(result_path,self.discovery)
            kwargs['stdout'].write(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':20}})+'\n')
            kwargs['stdout'].flush()
            return types.SimpleNamespace(stdin=io.StringIO(),poll=lambda:0,returncode=0)
        with patch.object(rb.subprocess,'Popen',side_effect=popen) as launch:
            result=self.runner.call('001-discover','prompt',rb.DISCOVERY,[self.preview])
            again=self.runner.call('001-discover','prompt',rb.DISCOVERY,[self.preview])
        self.assertEqual(old.read_bytes(),before);self.assertEqual(launch.call_count,1)
        self.assertEqual(result['errors'],[]);self.assertEqual(again,result)
        self.assertTrue(result['usage_incomplete'])
        self.assertTrue((self.root/'test-001-discover-attempt2-receipt.json').exists())
        with self.assertRaisesRegex(ValueError,'inputs changed'):
            self.runner.call('001-discover','different prompt',rb.DISCOVERY,[self.preview])

    def test_web_queries_counted_once_across_start_and_complete(self):
        item={'id':'search1','type':'web_search','action':{'type':'search','queries':['a','b']}}
        self.assertEqual(rb.web_query_count([{'type':'item.started','item':item},{'type':'item.completed','item':item}]),2)



    def test_visual_pipeline_delivers_labelled_references_and_preserves_unknown_date(self):
        candidate={'source_id':'museum','record_url':'https://museum.example/object','title':'Jaguar',
                   'candidate_image_url':'https://museum.example/photo.jpg'}
        short={'picks':[{'candidate_id':'C1','verdict':'possible','reason':'pose'}],
               'next':{'reason':'','search_terms':[],'place_names':[]}}
        with patch.object(self.runner,'call',side_effect=[receipt(self.discovery),receipt(short),receipt(comparison(refs=['R1']))]) as calls, \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve([candidate])), \
             patch.object(self.runner.fetcher,'image',return_value={'image_sha256':rb.digest(self.preview),
                         'image_url':candidate['candidate_image_url'],'local_image':str(self.preview)}):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'compared')
        self.assertTrue(row['references'][0]['image_delivered'])
        self.assertEqual(row['comparison']['result']['photograph_date']['status'],'unresolved')
        images=calls.call_args_list[-1].args[3]
        self.assertEqual(len(images),2)
        self.assertTrue(str(images[1]).endswith('-R1.jpg'))

    def test_token_limit_after_shortlist_prevents_comparison(self):
        candidate={'source_id':'museum','record_url':'https://museum.example/object','title':'Jaguar',
                   'candidate_image_url':'https://museum.example/photo.jpg'}
        short={'picks':[{'candidate_id':'C1','verdict':'possible','reason':'pose'}],
               'next':{'reason':'','search_terms':[],'place_names':[]}}
        with patch.object(self.runner,'call',side_effect=[receipt(self.discovery),receipt(short,30001)]) as calls, \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve([candidate])), \
             patch.object(self.runner.fetcher,'image',return_value={'image_sha256':rb.digest(self.preview),
                         'image_url':candidate['candidate_image_url'],'local_image':str(self.preview)}):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'usage_limit');self.assertEqual(calls.call_count,2)

    def test_native_crops_use_original_resolution_and_checked_recipe(self):
        original=self.root/'original.tif';Image.new('RGB',(1600,1200),'white').save(original)
        item={**self.item,'source_path':str(original),'source_stamp':rb.stamp(original),
              'preview_recipe':{'visual_checked':True,'rotation':0,'polarity':'normal'}}
        self.runner.settings['native_crops']=True
        with patch.object(rb,'protected_source',return_value=original):
            paths,note=self.runner.native_details(item,[{'label':'base','x':0,'y':0,'w':.5,'h':.5}],'001')
        with Image.open(paths[0]) as im:self.assertEqual(im.width,800)
        self.assertEqual(rb.stamp(original),item['source_stamp'])

    def test_search_cap_rejects_excess_queries_even_if_process_finishes_before_poll(self):
        (self.root/'test-instructions.txt').write_text('Instructions')
        def popen(cmd,**kwargs):
            rb.write(Path(cmd[cmd.index('-o')+1]),{'records':[],'limitations':[]})
            events=[{'type':'item.completed','item':{'id':'s1','type':'web_search',
                    'action':{'type':'search','queries':['one','two']},'results':[]}},
                    {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':20}}]
            kwargs['stdout'].write(''.join(json.dumps(e)+'\n' for e in events));kwargs['stdout'].flush()
            return types.SimpleNamespace(stdin=io.StringIO(),poll=lambda:0,returncode=0)
        with patch.object(rb.subprocess,'Popen',side_effect=popen):
            result=self.runner.call('web','search',rb.SEARCH,web=True,query_limit=1)
        self.assertIn('Web search budget exceeded',result['errors'])

    def test_record_can_use_observed_public_cdn_but_not_private_cdn(self):
        page=b'<title>Jaguar</title><img src="https://cdn.example/object.jpg">'
        meta={'content_type':'text/html','final_url':'https://museum.example/object'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(page,meta)):
            rows=self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],'relevance':'Jaguar'})
        url=rows[1]['candidate_image_url']
        with patch.object(rb.socket,'getaddrinfo',return_value=[(None,None,None,None,('8.8.8.8',443))]):
            self.runner.fetcher.validate(url)
            with self.assertRaisesRegex(ValueError,'registry'):self.runner.fetcher.validate('https://cdn.example/unobserved.jpg')
        with patch.object(rb.socket,'getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'Non-public'):self.runner.fetcher.validate(url)



    def test_missing_web_event_provenance_is_integration_error(self):
        response=receipt({'records':[{'source_id':'museum','url':'https://museum.example/object','relevance':'Jaguar'}],
                          'limitations':[]})
        response.update(observed_urls=[],provenance_available=False)
        row={}
        with patch.object(self.runner,'call',return_value=response):
            result=self.runner.search_portals('s',[{'source_id':'museum','query':'q'}],row,'portal_search',[])
        self.assertEqual(result,[])
        self.assertIn('integration_error:',row['portal_search']['errors'][0])

    def test_missing_provenance_cannot_become_no_results(self):
        with patch.object(self.runner,'call',return_value=receipt(self.discovery)), \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve(issues=[
                 {'error':'integration_error: web events contain no supported URL provenance field'}])):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'integration_error')

    def test_empty_supported_provenance_is_valid_zero_results(self):
        event={'type':'item.completed','item':{'id':'s','type':'web_search',
               'query':'q','action':{'type':'search','query':'q'},'results':[]}}
        self.assertTrue(rb.has_url_provenance([event]))
        self.assertEqual(rb.observed_urls([event]),[])
        del event['item']['results']
        self.assertFalse(rb.has_url_provenance([event]))
        event['item']['action']['sources']=[{'url':'https://museum.example/object'}]
        self.assertTrue(rb.has_url_provenance([event]))
        self.assertEqual(rb.observed_urls([event]),['https://museum.example/object'])

    def test_second_query_scheduled_and_executed(self):
        queries={**self.discovery,'search_terms':['Berlin Tierplastik','Philipp Harth fauchender Jaguar']}
        row={'number':1,'rounds':[{'round':1}]};budget=[6]
        response={**receipt({'records':[],'limitations':[]}),'observed_urls':[]}
        with patch.object(self.runner.fetcher,'commons',return_value=[]) as commons, \
             patch.object(self.runner,'call',return_value=response):
            self.runner.retrieve(queries,self.item,1,row,[],set(),budget)
        self.assertEqual([c.args[0] for c in commons.call_args_list],
                         ['Berlin Tierplastik','Philipp Harth fauchender Jaguar'])
        self.assertEqual(budget[0],3)
        self.assertEqual(len(row['rounds'][0]['scheduled_queries']),3)

    def test_query_schedule_does_not_repeat_tried_pairs(self):
        queries={**self.discovery,'search_terms':['first','second']}
        sources=self.runner.route('building',set(),1)
        initial=self.runner.schedule_queries(queries,sources,[],3)
        history=[{'source_searches':[{**s,'status':'no_results'} for s in initial]}]
        again=self.runner.schedule_queries(queries,sources,history,3)
        self.assertEqual([(s['source_id'],s['term']) for s in again],[('museum','second')])

    def test_three_source_limit_reserves_wider_search_before_new_portal(self):
        self.runner.registry['selection_policy']['maximum_total_sources_per_image']=3
        used={'commons','museum'}
        queries={**self.discovery,'search_terms':['first','second']}
        initial=self.runner.schedule_queries(queries,self.runner.route('building',set(),1),[],3)
        previous={'source_searches':[{**s,'status':'no_results'} for s in initial]}
        row={'number':1,'rounds':[previous,{'round':2}]}
        response={**receipt({'records':[],'limitations':[]}),'observed_urls':[]}
        with patch.object(self.runner,'call',return_value=response), \
             patch.object(self.runner.fetcher,'commons',return_value=[]), \
             patch.object(self.runner.fetcher,'denkmal') as native:
            self.runner.retrieve(queries,self.item,2,row,[],used,[3])
        self.assertIn('web_fallback',row['rounds'][-1])
        native.assert_not_called()

    def test_catalogue_queries_strip_qualifiers_not_real_place_disambiguation(self):
        terms,names=rb.query_terms({'search_terms':['Kosmos (tentative)'],
               'place_names':['Filmtheater Kosmos, Berlin (Reviewer-Zuordnung)','Klosterkirche (Hypothese)','Berlin (Mitte)']})
        self.assertEqual(terms,['Kosmos'])
        self.assertEqual(names,['Filmtheater Kosmos, Berlin','Klosterkirche','Berlin (Mitte)'])

    def test_native_detail_clues_reach_next_retrieval(self):
        discovery=deepcopy(self.discovery)
        discovery['crop_boxes']=[{'label':'sign','x':0,'y':0,'w':.5,'h':.5}]
        refined={**self.discovery,'search_terms':['newly readable inscription'],'visual_features':['new sign']}
        capture=[]
        retrieve=self.fake_retrieve()
        def gather(queries,*args):
            capture.append(deepcopy(queries))
            return retrieve(queries,*args)
        with patch.object(self.runner,'call',side_effect=[receipt(discovery),receipt(refined)]), \
             patch.object(self.runner,'native_details',return_value=([self.preview],'Native detail')), \
             patch.object(self.runner,'retrieve',side_effect=gather):
            row=self.runner.process(self.item,1)
        self.assertEqual(capture[0]['search_terms'],['Jaguar'])
        self.assertEqual(capture[1]['search_terms'],['newly readable inscription','Jaguar'])
        self.assertIn('clue_refinement',row['rounds'][1])
        self.assertEqual(rb.row_tokens(row),200)



    def test_named_candidate_survives_detail_refinement_and_reaches_wider_search(self):
        previous={**self.discovery,'candidate_queries':['Philipp Harth Jaguar'],
                  'search_terms':['crouching animal'],'place_names':['Köllnischer Park'],'subject':'building'}
        fresh={**self.discovery,'candidate_queries':[],'search_terms':['open mouth animal sculpture']}
        merged=rb.merge_queries(previous,fresh)
        self.assertEqual(merged['candidate_queries'],['Philipp Harth Jaguar'])
        terms,_=rb.query_terms(merged)
        self.assertEqual(terms[0],'Philipp Harth Jaguar')
        self.assertIn('open mouth animal sculpture',terms)
        row={'number':1,'rounds':[{'round':1,'source_searches':[]},{'round':2}]}
        response={**receipt({'records':[],'limitations':[]}),'observed_urls':[]}
        with patch.object(self.runner,'call',return_value=response) as calls, \
             patch.object(self.runner.fetcher,'denkmal',return_value=[]):
            self.runner.retrieve(merged,self.item,2,row,[],{'commons','museum'},[3])
        self.assertIn('web_fallback',row['rounds'][1])
        self.assertEqual(row['rounds'][1]['source_searches'][-1]['query'],'Philipp Harth Jaguar')
        self.assertIn('Philipp Harth Jaguar',calls.call_args.args[1])

    def test_refinement_keeps_specific_followup_question_ahead_of_old_identity(self):
        initial={**self.discovery,'candidate_queries':['Klosterkirche'],'place_names':['Klosterkirche']}
        followup={'reason':'Date the tower construction stage','search_terms':['Fernsehturm 1967 Plattform vor Kugelmontage'],
                  'place_names':['Berliner Fernsehturm']}
        queries=rb.merge_queries(initial,followup,followup=True)
        refined=rb.merge_queries(queries,{**self.discovery,'search_terms':['pointed church windows']})
        self.assertEqual(refined['reason'],'Date the tower construction stage')
        tasks=self.runner.schedule_queries(refined,[self.registry['sources'][0]],[],2)
        self.assertEqual(tasks[0]['query'],'Fernsehturm 1967 Plattform vor Kugelmontage')
        self.assertIn('Klosterkirche',refined['candidate_queries'])
        self.assertIn('pointed church windows',refined['search_terms'])

    def test_reviewer_artwork_candidate_is_retained_even_if_model_omits_it(self):
        item={**self.item,'sculpture_identification':{'artist':'Philipp Harth (1885–1968)','title':'Jaguar'}}
        self.assertEqual(rb.reviewer_candidates(item),['Philipp Harth Jaguar'])
        with patch.object(self.runner,'call',return_value=receipt(self.discovery)), \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve()) as retrieve:
            self.runner.process(item,1)
        self.assertIn('Philipp Harth Jaguar',retrieve.call_args_list[0].args[0]['candidate_queries'])
        self.assertIn('Philipp Harth Jaguar',retrieve.call_args_list[1].args[0]['candidate_queries'])

    def test_input_observations_need_no_external_identity_citation(self):
        result=comparison(more=True)
        result['observations']=[{'description':'Children hold scooters beside a traffic sign.','reference_id':'INPUT'}]
        rb.jsonschema.validate(result,rb.COMPARISON)
        self.runner.check_result(result,[])
        result['location']=assessment('supported',['INPUT'],'Wuhlheide')
        with self.assertRaisesRegex(ValueError,'absent'):self.runner.check_result(result,[])
        result['location']=assessment()
        result['depicted_object']=assessment('supported',[],'Named artwork')
        with self.assertRaisesRegex(ValueError,'Supported claim'):self.runner.check_result(result,[])

    def test_direct_observation_does_not_abort_followup_research(self):
        doc={'source_id':'museum','record_url':'https://museum.example/context','title':'Traffic education',
             'evidence_type':'catalogue_record','excerpt':'Children learn traffic rules.','page_number':None}
        result=comparison(more=True)
        result['observations']=[{'description':'Children with scooters and a one-way sign.','reference_id':'INPUT'}]
        result['next']={'reason':'Test candidate location','search_terms':['Wuhlheide Verkehrsgarten historische Fotos'],
                        'place_names':['Wuhlheide']}
        done=comparison()
        done['observations']=result['observations']
        with patch.object(self.runner,'call',side_effect=[receipt(self.discovery),receipt(result),receipt(done)]), \
             patch.object(self.runner,'retrieve',side_effect=self.fake_retrieve([doc])) as retrieve:
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'compared')
        self.assertEqual(len(row['rounds']),2)
        self.assertEqual(retrieve.call_args_list[1].args[0]['priority_queries'][0],'Wuhlheide Verkehrsgarten historische Fotos')
        self.assertEqual(row['comparison']['errors'],[])

    def test_provenance_phrases_are_not_catalogue_name_components(self):
        terms,names=rb.query_terms({'candidate_queries':['Jaguar (according to reviewer)'],
                                  'place_names':['Filmtheater Kosmos, Berlin (laut Vladimir)']})
        self.assertEqual(terms,['Jaguar'])
        self.assertEqual(names,['Filmtheater Kosmos, Berlin'])



    def test_specific_artwork_candidate_precedes_speculative_place_names(self):
        query={**self.discovery,'candidate_queries':['Philipp Harth Jaguar'],
               'place_names':['Köllnischer Park'],'search_terms':['crouching animal']}
        tasks=self.runner.schedule_queries(query,[self.registry['sources'][0]],[],2)
        self.assertEqual(tasks[0]['query'],'Philipp Harth Jaguar')
        self.assertIn('Köllnischer Park',[t['query'] for t in tasks])

    def test_rejected_shortlist_cannot_prioritize_generic_terms_over_named_candidate(self):
        discovery={**self.discovery,'candidate_queries':['Philipp Harth Jaguar'],'place_names':['Köllnischer Park']}
        candidate={'source_id':'commons','record_url':'https://commons.wikimedia.org/wiki/File:Bear.jpg',
                   'title':'Different bear','candidate_image_url':'https://commons.wikimedia.org/bear.jpg'}
        short={'picks':[],'next':{'reason':'Wrong pose','search_terms':['crouching animal open mouth'],'place_names':[]}}
        first=self.fake_retrieve([candidate]);second=self.fake_retrieve()
        captures=[]
        def retrieve(queries,item,rnd,*args):
            captures.append(deepcopy(queries))
            return (first if rnd==1 else second)(queries,item,rnd,*args)
        with patch.object(self.runner,'call',side_effect=[receipt(discovery),receipt(short)]), \
             patch.object(self.runner,'retrieve',side_effect=retrieve), \
             patch.object(self.runner.fetcher,'image',return_value={'image_sha256':rb.digest(self.preview),
                         'image_url':candidate['candidate_image_url'],'local_image':str(self.preview)}):
            self.runner.process(self.item,1)
        later=captures[1]
        self.assertFalse(later.get('priority_queries'))
        self.assertIn('crouching animal open mouth',later['search_terms'])
        self.assertEqual(rb.query_terms(later)[0][0],'Philipp Harth Jaguar')


if __name__=='__main__':unittest.main()
