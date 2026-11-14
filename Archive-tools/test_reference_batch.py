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
            with patch.object(self.runner.fetcher,'commons_api',return_value=[{'candidate_image_url':'https://upload.wikimedia.org/a.jpg'}]) as api:
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

    def test_revisited_record_reaches_remaining_photos_without_seen_slots(self):
        rows=[{'source_id':'museum','record_url':'https://museum.example/object','title':'Machine',
               'candidate_image_url':'https://museum.example/'+str(i)} for i in range(4)]
        with patch.object(self.runner.fetcher,'image',side_effect=lambda url:
                          {'image_sha256':hashlib.sha256(url.encode()).hexdigest()}) as fetch:
            pool,docs,issues=self.runner.gather(rows,{},set(r['candidate_image_url'] for r in rows[:2]),2)
        self.assertEqual([p['candidate_image_url'] for p in pool], [r['candidate_image_url'] for r in rows[2:]])
        self.assertEqual(fetch.call_count,2)

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
        self.runner.settings['research_goal']='location'  # Object identity alone does not finish this goal.
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


class ResearchQualityTests(unittest.TestCase):
    setUp = RefactorTests.setUp
    def visual(self, verdict='matching', boxes=()):
        return receipt({'comparisons':[{'reference_id':'R1','verdict':verdict,
            'matches':'window geometry','conflicts':'','limitation':'angle'}],
            'paired_crops':list(boxes),'unresolved_feature':'roof ornament'})

    def test_explicit_thumbnail_upgrades_only_with_observed_group_and_keeps_provenance(self):
        body=b'<img src="/building-800x600.jpg" srcset="/building-800x600.jpg 800w, /building-2000x1500.jpg 2000w" alt="Facade">'
        meta={'content_type':'text/html','final_url':'https://museum.example/article'}
        requested='https://museum.example/building-800x600.jpg'
        with patch.object(self.runner.fetcher,'fetch',return_value=(body,meta)):
            rows=self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],
                'image_url':requested,'relevance':'facade'})
        photo=next(r for r in rows if r['evidence_type']=='reference_photo')
        self.assertEqual(photo['candidate_image_url'],'https://museum.example/building-2000x1500.jpg')
        self.assertEqual(photo['discovered_image_url'],requested)
        self.assertEqual(photo['caption'],'Facade')

    def test_commons_file_page_is_not_a_full_resolution_image_link(self):
        page=rb.Page();page.feed('<a href="/wiki/File:Facade.jpg"><img alt="Facade" '
            'src="https://upload.wikimedia.org/thumb/a/Facade.jpg/320px-Facade.jpg" '
            'srcset="https://upload.wikimedia.org/thumb/a/Facade.jpg/640px-Facade.jpg 2x"></a>')
        photos=rb.ranked_page_images(page.images,'facade')
        self.assertEqual(len(photos),1)
        self.assertTrue(photos[0]['src'].startswith('https://upload.wikimedia.org/'))
        self.assertFalse(any('/wiki/' in u for u in photos[0]['variants']))
        self.assertTrue(photos[0]['src'].endswith('/640px-Facade.jpg'))

    def test_commons_category_uses_api_media_and_preserves_discovery_page(self):
        url='https://commons.wikimedia.org/wiki/Category%3AFacades'
        record={'record_url':'https://commons.wikimedia.org/wiki/File:Facade.jpg',
                'candidate_image_url':'https://upload.wikimedia.org/a/Facade.jpg'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(b'<html/>',{'url':url})), \
             patch.object(self.runner.fetcher,'commons_category',return_value=[record]) as api:
            out=self.runner.fetcher.record({'url':url,'source_id':'commons','relevance':'facade'})
        api.assert_called_once_with('Facades')
        self.assertEqual(out[0]['discovery_page_url'],url)
        self.assertTrue(out[0]['url_provenance']['page_fetched'])

    def test_missing_commons_file_is_retrieval_failure_not_empty_success(self):
        entry={'url':'https://commons.wikimedia.org/wiki/File:Missing.jpg',
               'source_id':'commons','relevance':'facade','url_provenance':{'page_fetched':False}}
        with patch.object(self.runner.fetcher,'commons_api',return_value=[]), \
             self.assertRaisesRegex(ValueError,'no usable image record'):
            self.runner.fetcher.record(entry)
        self.assertFalse(entry['url_provenance']['page_fetched'])

    def test_pdf_title_does_not_promote_search_hypothesis_to_document_evidence(self):
        entry={'url':'https://museum.example/history.pdf','source_id':'museum',
               'relevance':'This proves the address and date'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(b'%PDF-',{'content_type':'application/pdf'})), \
             patch.object(rb.shutil,'which',return_value='/usr/bin/pdftotext'), \
             patch.object(rb.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=b'Unrelated historical text')):
            doc=self.runner.fetcher.record(entry)[0]
        self.assertEqual(doc['title'],'history.pdf')
        self.assertEqual(doc['discovery_relevance'],entry['relevance'])
        prompt=self.runner.compare_prompt(self.item,[],[],[],[{**doc,'id':'D1'}])
        self.assertNotIn(entry['relevance'],prompt)

    def test_blind_stage_cannot_see_captions_and_context_date_is_not_photo_date(self):
        self.runner.settings['visual_verification']=True
        ref={'id':'R1','labelled_image':str(self.preview),'title':'SECRET IDENTITY','caption':'SECRET CAPTION'}
        result=comparison(['R1']);result['depicted_object']=assessment('supported',['R1'],'Site')
        result['photograph_date']=assessment('tentative',['R1'],'1921–1945')
        result['date_basis']={'kind':'historical_context','explanation':'Business occupancy','reference_ids':['R1']}
        record={}
        with patch.object(self.runner,'call',side_effect=[self.visual('different'),receipt(result)]) as call:
            out=self.runner.compare_verified(self.item,[ref],[],[],self.preview,'r1',record)
        blind=call.call_args_list[0].args[1]
        self.assertNotIn('SECRET',blind)
        self.assertIn('SECRET CAPTION',call.call_args_list[1].args[1])
        self.assertEqual(out['result']['depicted_object']['status'],'tentative')
        self.assertIsNone(out['result']['photograph_date']['value'])
        self.assertEqual(out['raw_result']['photograph_date']['value'],'1921–1945')
        self.assertEqual(rb.row_tokens({'rounds':[{**record,'comparison':out}]}),200)

    def test_paired_crops_use_original_pixels_and_reject_modified_original(self):
        self.runner.settings.update(native_crops=True)
        original=self.root/'original.png';Image.new('RGB',(2000,1000),'blue').save(original)
        overview=self.root/'R1.jpg';rb.save_jpeg(rb.labelled(rb.open_rgb(original),'R1',900),overview)
        ref={'id':'R1','original_image':str(original),'labelled_image':str(overview),
             'download_receipt':{'sha256':rb.digest(original)}}
        box={'label':'window','x':.1,'y':.2,'w':.4,'h':.4}
        req={'reference_id':'R1','reason':'windows','input_box':box,'reference_box':box}
        with patch.object(self.runner,'native_details',return_value=([self.preview],'native')):
            paths,notes=self.runner.paired_details(self.item,self.preview,[ref],[req],'paired')
            self.assertEqual(len(paths),2)
            with Image.open(paths[1]) as im:self.assertGreaterEqual(im.width,800)
            original.write_bytes(b'changed')
            paths,notes=self.runner.paired_details(self.item,self.preview,[ref],[req],'changed')
        self.assertFalse(paths);self.assertIn('changed',notes[0]['error'])

    def test_identification_goal_stops_even_when_date_or_location_remains_open(self):
        result=comparison(['R1'],more=True)
        result['depicted_object']=assessment('supported',['R1'],'Named sculpture')
        self.assertTrue(self.runner.goal_met(result))
        for goal in ('location','dating'):
            self.runner.settings['research_goal']=goal
            self.assertFalse(self.runner.goal_met(result))

    def test_adaptive_identification_does_not_search_again_only_for_date(self):
        self.runner.settings.update(discovery_mode='adaptive')
        result=comparison(['R1'],more=True)
        result['depicted_object']=assessment('supported',['R1'],'Named building')
        result['next']={'reason':'Date still unknown','search_terms':['business occupancy'],'place_names':[]}
        pool=[{'image_sha256':'a','local_image':str(self.preview),'record_url':'https://museum.example/article'}]
        with patch.object(self.runner,'call',side_effect=[self.adaptive_search(),receipt(result)]) as call, \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',return_value=(pool,[],[])):
            row=self.runner.process(self.item,1)
        self.assertEqual(call.call_count,2)
        self.assertEqual(row['stop_reason'],'identified')
        self.assertEqual(len(row['rounds']),1)

    def test_invalid_blind_reference_becomes_insufficient_without_aborting(self):
        self.runner.settings['visual_verification']=True
        ref={'id':'R1','labelled_image':str(self.preview)}
        visual=self.visual();visual['result']['comparisons'][0]['reference_id']='R9'
        answer=comparison(['R1']);answer['depicted_object']=assessment('supported',['R1'],'Site')
        answer['date_basis']={'kind':'none','explanation':'Unknown','reference_ids':[]}
        record={}
        with patch.object(self.runner,'call',side_effect=[visual,receipt(answer)]) as call:
            result=self.runner.compare_verified(self.item,[ref],[],[],self.preview,'r1',record)
        self.assertFalse(result['errors']);self.assertEqual(call.call_count,2)
        self.assertEqual(result['result']['depicted_object']['status'],'tentative')
        self.assertEqual(record['visual_check']['result']['comparisons'][0]['verdict'],'insufficient')

    def test_usage_limit_after_blind_check_prevents_another_paid_call(self):
        self.runner.settings.update(visual_verification=True,max_item_tokens=50)
        with patch.object(self.runner,'call',return_value=self.visual()) as call:
            record={}
            result=self.runner.compare_verified(self.item,[{'id':'R1','labelled_image':str(self.preview)}],
                [],[],self.preview,'r1',record)
        call.assert_called_once();self.assertEqual(result['stop_reason'],'usage_limit')
        self.assertEqual(rb.row_tokens({'rounds':[{**record,'comparison':result}]}),100)

    def test_detail_recheck_precedes_caption_comparison_and_is_counted(self):
        self.runner.settings['visual_verification']=True
        ref={'id':'R1','labelled_image':str(self.preview)}
        result=comparison(['R1']);result['date_basis']={'kind':'none','explanation':'Unknown','reference_ids':[]}
        record={}
        with patch.object(self.runner,'paired_details',return_value=([self.preview,self.preview],[])), \
             patch.object(self.runner,'call',side_effect=[self.visual(),self.visual(),receipt(result)]) as call:
            out=self.runner.compare_verified(self.item,[ref],[],[],self.preview,'r1',record)
        self.assertIn('visual-details',call.call_args_list[1].args[0])
        self.assertIn('CAPTION-BLIND',call.call_args_list[2].args[1])
        self.assertEqual(rb.row_tokens({'rounds':[{**record,'comparison':out}]}),300)
    # Reuse fixtures without inheriting the older tests a second time.
    def test_gallery_excludes_related_cards_and_keeps_caption_and_full_resolution(self):
        page=rb.Page()
        page.feed('<main><figure><a href="https://museum.example/original.jpg">'
                  '<img src="https://museum.example/small.jpg" alt="East facade" '
                  'srcset="https://museum.example/medium.jpg 800w, https://museum.example/large.jpg 1600w">'
                  '</a><figcaption>Photographer, 1930</figcaption></figure>'
                  '<section class="bzi-suggest-more-section"><img src="https://museum.example/related.jpg"></section>'
                  '<footer><img src="https://museum.example/footer.jpg"></footer></main>')
        photos=rb.ranked_page_images(page.images,'facade')
        self.assertEqual(len(photos),1)
        self.assertEqual(photos[0]['src'],'https://museum.example/original.jpg')
        self.assertEqual(photos[0]['alt'],'East facade')
        self.assertEqual(photos[0]['caption'],'Photographer, 1930')
        self.assertEqual(len(photos[0]['variants']),4)

    def test_larger_wordpress_variant_keeps_alt_and_gallery_caption_separate(self):
        page=rb.Page();page.feed('<meta property="og:image" content="https://a/wp-content/uploads/harbour-scaled.jpg">'
            '<figure><img src="https://a/wp-content/uploads/harbour-1024x682.jpg" alt="Harbour from river">'
            '<img src="https://a/wp-content/uploads/warehouse.jpg" alt="Warehouse">'
            '<figcaption>Gallery credit</figcaption></figure>')
        photos=rb.ranked_page_images(page.images,'harbour')
        harbour=next(p for p in photos if 'harbour' in p['src'])
        self.assertEqual(len(photos),2)
        self.assertEqual(harbour['alt'],'Harbour from river')
        self.assertEqual(harbour['caption'],'')
        self.assertEqual(harbour['gallery_caption'],'Gallery credit')

    def test_supplied_image_is_preserved_with_its_page(self):
        body=b'<title>Gallery</title><img src="/one.jpg"><img src="/two.jpg" alt="Rear facade">'
        meta={'content_type':'text/html','final_url':'https://museum.example/article'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(body,meta)):
            rows=self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],
                'image_url':'https://museum.example/two.jpg','relevance':'facade'})
        photos=[r for r in rows if r['evidence_type']=='reference_photo']
        self.assertEqual(photos[0]['candidate_image_url'],'https://museum.example/two.jpg')
        self.assertEqual(photos[0]['caption'],'Rear facade')
        self.assertEqual(photos[0]['record_url'],meta['final_url'])
        self.assertTrue(photos[0]['url_provenance']['page_fetched'])
        with patch.object(self.runner.fetcher,'fetch',return_value=(body,meta)), self.assertRaisesRegex(ValueError,'neither'):
            self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],
                'image_url':'https://unobserved.example/invented.jpg','relevance':'facade'})

    def test_responsive_group_bridges_og_image_without_duplicate_or_caption_loss(self):
        page=rb.Page();page.feed('<meta property="og:image" content="https://museum.example/large.jpg">'
            '<img src="https://museum.example/small.jpg" alt="Facade" '
            'srcset="https://museum.example/small.jpg 800w, https://museum.example/large.jpg 1600w">')
        photos=rb.ranked_page_images(page.images,'facade')
        self.assertEqual(len(photos),1)
        self.assertEqual(photos[0]['src'],'https://museum.example/large.jpg')
        self.assertEqual(photos[0]['alt'],'Facade')

    def test_action_url_admitted_without_claiming_search_or_fetch_success(self):
        self.runner.settings.update(discovery_mode='adaptive',max_rounds=1)
        response=self.adaptive_search();url=response['observed_urls'][0]
        response.update(observed_urls=[],action_urls=[url])
        with patch.object(self.runner,'call',return_value=response), \
             patch.object(self.runner.fetcher,'record',side_effect=ValueError('HTTP 403')):
            row=self.runner.process(self.item,1)
        state=row['rounds'][0]['url_provenance'][0]
        self.assertFalse(state['observed_in_search']);self.assertTrue(state['requested_by_tool'])
        self.assertFalse(state['page_fetched']);self.assertEqual(row['stop_reason'],'retrieval_failed')

    def test_large_gallery_can_continue_after_per_round_limit(self):
        rows=[{'source_id':'museum','record_url':'https://museum.example/gallery','title':'Photograph',
               'candidate_image_url':f'https://museum.example/{i}.jpg'} for i in range(12)]
        with patch.object(self.runner.fetcher,'image',side_effect=lambda u:{'image_sha256':hashlib.sha256(u.encode()).hexdigest()}):
            seen=set(); first=self.runner.gather(rows,{},seen,1)[0]; second=self.runner.gather(rows,{},seen,2)[0]
        self.assertEqual((len(first),len(second)),(8,4))
        self.assertEqual(len({p['candidate_image_url'] for p in first+second}),12)

    def test_visual_selection_can_choose_a_photo_below_text_top_six(self):
        pool=[{'cid':f'C{i}','local_image':str(self.preview)} for i in range(1,9)]
        result=receipt({'picks':[{'candidate_id':'C8','verdict':'likely','reason':'Matching windows'}],
                        'next':{'reason':'','search_terms':[],'place_names':[]}})
        record={}
        with patch.object(self.runner,'call',return_value=result) as call:
            picked=self.runner.visual_selection(pool,self.preview,[],[],'r1',record)
        self.assertEqual(picked[0],pool[-1]);self.assertEqual(len(picked),6)
        self.assertEqual(len(call.call_args.args[3]),2) # INPUT and gallery
        self.assertFalse(call.call_args.kwargs.get('web',False))

    def test_action_only_provenance_is_distinct_from_search_and_fetch(self):
        event={'type':'item.completed','item':{'type':'web_search',
               'action':{'type':'open_page','url':'https://museum.example/article'}}}
        self.assertTrue(rb.has_url_provenance([event]))
        self.assertEqual(rb.observed_urls([event]),[])
        self.assertEqual(rb.action_urls([event]),['https://museum.example/article'])

    def adaptive_search(self, boxes=()):
        url='https://museum.example/article'
        return {**receipt({'research_routing':{'primary':'buildings_places','secondary':[],
            'confidence':'high','reason':'Facade'},'visual_features':['windows'],'crop_boxes':list(boxes),
            'records':[{'source_id':'museum','page_url':url,'image_url':None,'relevance':'facade'}],
            'queries_reported':['facade'],'limitations':[]}), 'observed_urls':[url]}

    def test_crops_precede_first_comparison_and_references_reach_next_discovery(self):
        self.runner.settings.update(discovery_mode='adaptive',native_crops=True)
        crop=self.root/'detail.jpg';Image.new('RGB',(100,100)).save(crop)
        calls=[]
        def call(stem,prompt,schema,images=(),**kw):
            calls.append((stem,list(images)))
            if stem.endswith('adaptive'):
                return self.adaptive_search([{'label':'sign','x':.1,'y':.1,'w':.4,'h':.4}])
            value=comparison(['R1'],more=False)
            value['next']={'reason':'Unresolved inscription','search_terms':['inscription facade'],'place_names':[]}
            return receipt(value) # an actionable question survives needs_more_references=false
        pool=[{'image_sha256':'a','local_image':str(self.preview),'record_url':'https://museum.example/article'}]
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner,'research_input',return_value=(self.preview,'test')), \
             patch.object(self.runner,'native_details',return_value=([crop],'native crop')) as native, \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',return_value=(pool,[],[])):
            row=self.runner.process(self.item,1)
        self.assertEqual(len(calls),4)
        self.assertIn(crop,calls[1][1])
        self.assertEqual(len(calls[2][1]),3) # INPUT, crop, retained reference
        self.assertEqual(native.call_count,2) # includes final-round crop request
        self.assertEqual(row['stop_reason'],'round_limit')

    def test_partial_result_remains_retryable_after_failed_second_search(self):
        self.runner.settings.update(discovery_mode='adaptive')
        pool=[{'image_sha256':'a','local_image':str(self.preview),'record_url':'https://museum.example/article'}]
        calls=[self.adaptive_search(),receipt(comparison(['R1'],more=True)),receipt(None,errors=['timeout'])]
        with patch.object(self.runner,'call',side_effect=calls), \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',return_value=(pool,[],[])):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'compared')
        self.assertEqual(row['stop_reason'],'search_failed')
        self.assertTrue(rb.retryable(row));self.assertEqual(row['comparison']['result']['comparisons'][0]['reference_id'],'R1')

    def test_retry_run_revisits_partial_failure_without_attaching_pilot(self):
        self.runner.args.retry_failed=True;self.runner.args.limit=0
        rb.write(self.runner.path('harvest.json'),[{'object_id':'object','status':'compared','stop_reason':'access_limited'}])
        result={'object_id':'object','number':1,'status':'compared','stop_reason':'identified','rounds':[]}
        with patch.object(self.runner,'load_plan',return_value={'items':[self.item],'benchmark':True}), \
             patch.object(self.runner,'process',return_value=result) as process, \
             patch.object(self.runner,'publish'), patch.object(self.runner,'attach') as attach, \
             patch.object(rb.signal,'signal'):
            self.runner.run()
        process.assert_called_once();attach.assert_not_called()

    def test_direct_control_makes_one_comparison_without_search(self):
        self.runner.settings.update(discovery_mode='direct',direct_reference={
            'page_url':'https://museum.example/article','image_url':'https://museum.example/known.jpg'})
        candidate={'source_id':'museum','record_url':'https://museum.example/article','title':'',
                   'candidate_image_url':'https://museum.example/known.jpg'}
        result=comparison(['R1']);result['depicted_object']=assessment('supported',['R1'],'Site')
        result['location']=assessment('supported',['R1'],'Address')
        with patch.object(self.runner.fetcher,'record',return_value=[candidate]), \
             patch.object(self.runner.fetcher,'image',return_value={'image_sha256':'a','local_image':str(self.preview)}), \
             patch.object(self.runner,'call',return_value=receipt(result)) as call:
            row=self.runner.process(self.item,1)
        call.assert_called_once();self.assertFalse(call.call_args.kwargs.get('web',False))
        self.assertEqual(row['stop_reason'],'identified');self.assertEqual(row['queries_used'],0)


class ReviewFixTests(unittest.TestCase):
    setUp = RefactorTests.setUp
    visual = ResearchQualityTests.visual
    adaptive_search = ResearchQualityTests.adaptive_search

    def answer(self, ids=('R1',), supported=True):
        result=comparison([rid for rid in ids if rid.startswith('R')])
        if supported:result['depicted_object']=assessment('supported',ids,'Named building')
        result['date_basis']={'kind':'none','explanation':'Unknown','reference_ids':[]}
        return result

    def test_layout_classes_keep_article_images_but_exclude_actual_related_sections(self):
        for tag,cls in [('body','no-sidebar'),('html','has-sidebar'),('div','content-sidebar-wrap'),
                        ('main','related'),('article','sidebar')]:
            with self.subTest(tag=tag,cls=cls):
                p=rb.Page();p.feed(f'<{tag} class="{cls}"><img src="/facade.jpg">'
                    '<section class="related-posts"><img src="/other.jpg"></section>'
                    f'<footer><img src="/footer.jpg"></footer></{tag}>')
                self.assertEqual([x['src'] for x in rb.ranked_page_images(p.images,'')],['/facade.jpg'])

    def test_picture_groups_non_wordpress_sources_and_ignores_video(self):
        p=rb.Page();p.feed('<picture><source srcset="/large.webp 1600w"><img src="/small.jpg" alt="Facade"></picture>'
            '<video><source src="/movie.mp4"></video>')
        photos=rb.ranked_page_images(p.images,'')
        self.assertEqual(len(photos),1);self.assertEqual(photos[0]['src'],'/large.webp')
        self.assertEqual(set(photos[0]['variants']),{'/large.webp','/small.jpg'})

    def test_original_failure_uses_only_known_variant_and_reports_resolution(self):
        original='https://museum.example/original.jpg';small='https://museum.example/thumb.jpg'
        candidate={'source_id':'museum','title':'','record_url':'https://museum.example/page',
                   'candidate_image_url':original,'original_image_url':original,'image_variants':[small,original]}
        for error in ('Response exceeds 12 MB limit','HTTP 403'):
            with self.subTest(error=error), patch.object(self.runner.fetcher,'image',side_effect=[ValueError(error),
                    {'image_sha256':'a','local_image':str(self.preview),'original_size':[1800,1200]}]) as download:
                pool,docs,issues=self.runner.gather([candidate],{},set(),1)
            self.assertEqual([c.args[0] for c in download.call_args_list],[original,small])
            self.assertEqual(len(pool),1);self.assertFalse(issues)
            self.assertEqual(pool[0]['delivered_variant'],small)
            self.assertFalse(pool[0]['native_reference_available'])
            self.assertEqual(pool[0]['fallback_attempts'][0]['error'],error)
            self.assertTrue(pool[0]['image_provenance']['image_fetched'])

    def test_all_variant_failures_respect_attempt_cap(self):
        c={'source_id':'museum','title':'','record_url':'https://museum.example/page',
           'candidate_image_url':'https://museum.example/0.jpg',
           'image_variants':[f'https://museum.example/{i}.jpg' for i in range(30)]}
        with patch.object(self.runner.fetcher,'image',side_effect=ValueError('blocked')) as download:
            pool,docs,issues=self.runner.gather([c],{},set(),1)
        self.assertEqual(download.call_count,rb.MAX_IMAGE_ATTEMPTS);self.assertFalse(pool);self.assertTrue(issues)

    def test_tool_opened_image_admitted_without_fabricating_search_or_page_association(self):
        url='https://cdn.example/image.jpg';page='https://museum.example/article'
        entry={'source_id':'museum','url':page,'image_url':url,'image_requested_by_tool':True,'relevance':'facade'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(b'<title>Article</title><p>Context</p>',
                          {'content_type':'text/html','final_url':page})):
            records=self.runner.fetcher.record(entry)
        photo=next(c for c in records if c.get('candidate_image_url'))
        self.assertIn(url,self.runner.fetcher.observed_media)
        state=photo['image_provenance']
        self.assertTrue(state['requested_by_tool']);self.assertFalse(state['observed_in_search'])
        self.assertFalse(state['associated_with_page']);self.assertFalse(state['image_fetched'])
        with patch.object(self.runner.fetcher,'image',return_value={'image_sha256':'a'}) as download:
            pool,_,_=self.runner.gather(records,{},set(),1)
        download.assert_called_once_with(url);self.assertTrue(pool[0]['image_provenance']['image_fetched'])

    def test_document_citation_cannot_bypass_visual_rejection_and_summary_agrees(self):
        result=self.answer(('R1','D1'))
        result['summary']='Definitely identified, dated 1972'
        result['photograph_date']=assessment('supported',['D1'],'1972')
        result['date_basis']={'kind':'historical_context','explanation':'Event date only','reference_ids':['D1']}
        result['hypotheses']=[{'candidate':'Named building','field':'depicted_object','status':'supported',
                              'assessment':'Certain','reference_ids':['R1','D1']}]
        raw=deepcopy(result);out=receipt(result)
        refs=[{'id':'R1','record_url':'page'}]
        rb.verify_assessments(out,self.visual('different')['result'],refs,[{'id':'D1'}])
        self.assertEqual(out['raw_result'],raw)
        self.assertEqual(out['result']['depicted_object']['status'],'tentative')
        self.assertEqual(out['result']['hypotheses'][0]['status'],'tentative')
        self.assertNotIn('Definitely',out['result']['summary']);self.assertNotIn('1972',out['result']['summary'])
        self.assertIn('Aufnahmedatum: ungeklärt',out['result']['summary'])
        self.assertEqual(rb.retain_evidence(out['result'],refs,[])[0],[])
        once=deepcopy(out)
        rb.verify_assessments(out,self.visual('different')['result'],refs,[{'id':'D1'}])
        self.assertEqual(out,once) # cached rerun preserves raw output and is idempotent

    def test_document_only_round_skips_blind_call_and_can_support_bound_record(self):
        self.runner.settings['visual_verification']=True
        result=self.answer(('D1',));result['comparisons']=[];record={}
        url='https://museum.example/photo-record'
        self.item['document_bindings']=[{'object_id':self.item['object_id'],
            'preview_sha256':self.item['preview_sha256'],'record_url':url,
            'fields':['depicted_object'],'explanation':'Reviewer matched the photograph inventory mark to this record.'}]
        with patch.object(self.runner,'call',return_value=receipt(result)) as call:
            out=self.runner.compare_verified(self.item,[],[],[],self.preview,'docs',record,[{'id':'D1','record_url':url}])
        self.assertEqual(call.call_count,1);self.assertTrue(record['visual_check']['skipped'])
        self.assertEqual(out['result']['depicted_object']['status'],'supported')
        self.runner.check_result(out['result'],[],[{'id':'D1'}])

    def test_visible_input_date_remains_tentative_not_exposure_date(self):
        value=self.answer((),False);value['photograph_date']=assessment('supported',['INPUT'],'1. Mai 1972')
        value['observations']=[{'reference_id':'INPUT','description':'Banner in INPUT reads 1. Mai 1972'}]
        value['date_basis']={'kind':'visible_time_bound','explanation':'Visible banner text','reference_ids':['INPUT'],
                             'input_observation':'Banner in INPUT reads 1. Mai 1972'}
        out=receipt(value);rb.verify_assessments(out,{'comparisons':[]},[],[])
        self.assertEqual(out['result']['photograph_date']['status'],'tentative')
        self.assertEqual(out['result']['photograph_date']['value'],'1. Mai 1972')
        self.assertIn('not a verified exposure date',out['result']['photograph_date']['explanation'])
        self.runner.check_result(out['result'],[])
        out['result']['depicted_object']=assessment('supported',['INPUT'],'Name')
        with self.assertRaises(ValueError):self.runner.check_result(out['result'],[])

    def test_existing_detail_reaches_blind_call_with_neutral_banner(self):
        self.runner.settings['visual_verification']=True
        crop=self.root/'discovery-detail.jpg'
        rb.save_jpeg(rb.labelled(rb.open_rgb(self.preview),'SECRET CANDIDATE',1800),crop)
        ref={'id':'R1','labelled_image':str(self.preview),'title':'SECRET CANDIDATE'}
        for verdict,expected in [('matching','supported'),('different','tentative')]:
            with patch.object(self.runner,'call',side_effect=[self.visual(verdict),receipt(self.answer())]) as call:
                out=self.runner.compare_verified(self.item,[ref],[crop],[],self.preview,'neutral',{})
            sent=call.call_args_list[0].args[3]
            self.assertEqual(len(sent),3);self.assertNotEqual(str(sent[1]),str(crop))
            self.assertNotIn('SECRET',call.call_args_list[0].args[1])
            with Image.open(crop) as before, Image.open(sent[1]) as after:
                self.assertEqual(before.size,after.size)
                self.assertNotEqual(before.crop((0,0,200,28)).tobytes(),after.crop((0,0,200,28)).tobytes())
                self.assertEqual(before.crop((0,28,before.width,before.height)).tobytes(),
                                 after.crop((0,28,after.width,after.height)).tobytes())
            self.assertEqual(out['result']['depicted_object']['status'],expected)

    def test_production_adaptive_revisits_full_size_candidates_omitted_from_shortlist(self):
        self.runner.settings.update(discovery_mode='adaptive',visual_verification=True,max_item_tokens=500000)
        pool=[{'image_sha256':str(i),'cid':f'C{i}','local_image':str(self.preview),
               'record_url':f'https://museum.example/{i}'} for i in range(1,9)]
        seen=[]
        def call(stem,prompt,schema,images=(),**kw):
            seen.append((stem,list(images)))
            if stem.endswith('adaptive'):return self.adaptive_search()
            if stem.endswith('shortlist'):return receipt({'picks':[{'candidate_id':'C999','verdict':'likely','reason':'bad id'}],
                'next':{'reason':'','search_terms':[],'place_names':[]}})
            if stem.endswith('visual'):
                count=len(images)-1
                return receipt({'comparisons':[{'reference_id':f'R{i+1}','verdict':'different',
                    'matches':'','conflicts':'wrong geometry','limitation':''} for i in range(count)],
                    'paired_crops':[],'unresolved_feature':'windows'})
            return receipt(self.answer([f'R{i+1}' for i in range(len(images)-1)],False))
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',side_effect=[(deepcopy(pool),[],[]),([],[],[])]):
            row=self.runner.process(self.item,1)
        self.assertEqual(sum(stem.endswith('adaptive') for stem,_ in seen),1)
        self.assertTrue(row['rounds'][1]['inspection_only'])
        self.assertEqual([r['image_sha256'] for r in row['rounds'][1]['references']],['7','8'])
        self.assertEqual(row['uninspected_candidates'],[])
        self.assertFalse(any(c.get('errors') for c in rb.row_calls(row)))
        self.assertEqual(len(row['rounds'][0]['references']),6)

    def test_production_adaptive_document_and_mismatched_image_cannot_finish_identification(self):
        self.runner.settings.update(discovery_mode='adaptive',visual_verification=True,max_rounds=1)
        pool=[{'image_sha256':'a','local_image':str(self.preview),'record_url':'https://museum.example/article'}]
        docs=[{'record_url':'https://museum.example/article','title':'Article','excerpt':'Context only'}]
        with patch.object(self.runner,'call',side_effect=[self.adaptive_search(),self.visual('different'),
                 receipt(self.answer(('R1','D1')))]), \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'gather',return_value=(pool,docs,[])):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['status'],'compared');self.assertEqual(row['stop_reason'],'search_exhausted')
        self.assertEqual(row['comparison']['result']['depicted_object']['status'],'tentative')
        self.assertEqual(row['comparison']['raw_result']['depicted_object']['status'],'supported')
        self.assertIn('Objekt: vorläufig',row['comparison']['result']['summary'])

    def test_production_portal_mode_inspects_pending_references_without_another_search(self):
        self.runner.settings.update(discovery_mode='portals',visual_verification=True,max_item_tokens=500000)
        pool=[{'image_sha256':str(i),'cid':f'C{i}','local_image':str(self.preview),'source_id':'museum',
               'record_url':f'https://museum.example/{i}'} for i in range(1,9)]
        def call(stem,prompt,schema,images=(),**kw):
            if schema is rb.DISCOVERY:return receipt(self.discovery)
            if stem.endswith('shortlist'):return receipt({'picks':[],
                'next':{'reason':'','search_terms':[],'place_names':[]}})
            ids=[f'R{i+1}' for i in range(len(images)-1)]
            if stem.endswith('visual'):
                return receipt({'comparisons':[{'reference_id':rid,'verdict':'different',
                    'matches':'','conflicts':'wrong geometry','limitation':''} for rid in ids],
                    'paired_crops':[],'unresolved_feature':'windows'})
            return receipt(self.answer(ids,False))
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner,'retrieve',side_effect=RefactorTests.fake_retrieve(self)) as retrieve, \
             patch.object(self.runner,'gather',return_value=(pool,[],[])):
            row=self.runner.process(self.item,1)
        retrieve.assert_called_once()
        self.assertTrue(row['rounds'][1]['inspection_only'])
        self.assertEqual([r['image_sha256'] for r in row['rounds'][1]['references']],['5','6','7','8'])
        self.assertEqual(row['uninspected_candidates'],[])

    def test_goal_stop_labels_and_rejected_evidence_retention(self):
        for goal,label in [('identification','identified'),('location','located'),('dating','dated')]:
            self.runner.settings['research_goal']=goal;self.assertEqual(self.runner.goal_stop(),label)
        result=self.answer(('R2',))
        result['hypotheses']=[{'candidate':'Other','status':'tentative','assessment':'possible','reference_ids':['R1']}]
        refs=[{'id':'R1','record_url':'a','visual_verdict':'compatible'},
              {'id':'R2','record_url':'b','visual_verdict':'matching'},
              {'id':'R3','record_url':'c','visual_verdict':'different'}]
        kept,_=rb.retain_evidence(result,refs,[])
        self.assertEqual([r['id'] for r in kept],['R2','R1'])

class FollowupReviewTests(unittest.TestCase):
    setUp = RefactorTests.setUp
    answer = ReviewFixTests.answer
    visual = ResearchQualityTests.visual
    adaptive_search = ResearchQualityTests.adaptive_search

    def binding(self, fields):
        return {'object_id':self.item['object_id'], 'preview_sha256':self.item['preview_sha256'],
                'record_url':'https://museum.example/record', 'fields':fields,
                'explanation':'Reviewer matched the input inventory mark to this photograph record.'}

    def test_generic_document_cannot_identify_rejected_or_unseen_candidate(self):
        for verdict in ('different','insufficient','compatible'):
            for cited in (['D1'],['R1','D1']):
                with self.subTest(verdict=verdict,cited=cited):
                    value=self.answer(cited);value['comparisons']=self.answer()['comparisons']
                    out=receipt(value)
                    refs=[{'id':'R1','record_url':'page'}];docs=[{'id':'D1','record_url':'page'}]
                    rb.verify_assessments(out,self.visual(verdict)['result'],refs,docs)
                    self.runner.check_result(out['result'],refs,docs)
                    self.assertEqual(out['result']['depicted_object']['status'],'tentative')
                    self.assertFalse(self.runner.goal_met(out['result']))
        out=receipt(self.answer(['D1']))
        rb.verify_assessments(out,{'comparisons':[]},[],[{'id':'D1','record_url':'page'}])
        self.assertEqual(out['result']['depicted_object']['status'],'tentative')

    def test_document_binding_is_field_and_input_specific(self):
        binding=self.binding(['depicted_object'])
        self.assertEqual(rb.document_bindings([binding],[self.item]),[binding])
        for key,value in [('object_id','other'),('preview_sha256','changed'),('explanation',''),
                          ('fields',['invented']),('record_url','file:///tmp/record')]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                rb.document_bindings([{**binding,key:value}],[self.item])
        out=receipt(self.answer(['D1']))
        out['result']['location']=assessment('supported',['D1'],'Berlin')
        refs=[{'id':'R1','record_url':binding['record_url']}]
        docs=[{'id':'D1','record_url':binding['record_url']}]
        rb.verify_assessments(out,self.visual('different')['result'],refs,docs,[binding])
        self.assertEqual(out['result']['depicted_object']['status'],'supported')
        self.assertEqual(out['result']['location']['status'],'tentative')
        self.assertEqual(out['raw_result']['location']['status'],'supported')

    def test_prepare_freezes_bindings_and_rejects_them_in_blind_pilot(self):
        previews=self.root/'previews';previews.mkdir()
        preview=previews/'input.jpg';preview.write_bytes(self.preview.read_bytes())
        record={**self.item,'candidate_id':'candidate','preview_path':str(preview),
                'result':{'title':{'value':'Building'},'description':{'value':'Facade'}}}
        rb.write(self.root/'model-proposals.json',{'metadata_adopted':False,'records':[record]})
        rb.write(self.root/'workbench-input.json',{'objects':[{'id':self.item['object_id'],'candidates':[]}]})
        (self.root/'decisions.jsonl').write_text(json.dumps({'object_id':self.item['object_id']})+'\n')
        binding=self.binding(['depicted_object']);path=self.root/'binding.json';rb.write(path,[binding])
        self.runner.args=types.SimpleNamespace(prefix='prepared',model='test-model',object_id=[self.item['object_id']],
            pilot_only=False,blind_pilot=False,document_bindings=path,compare_effort='high',max_rounds=2,
            max_item_tokens=None,max_average_tokens=None,web_search=True,native_crops=False,
            discovery_mode='adaptive',expected=None)
        self.runner.prepare()
        rb.write(path,[])
        plan=rb.read(self.root/'prepared-plan.json')
        self.assertEqual(plan['items'][0]['document_bindings'],[binding])
        self.assertEqual(plan['version'],12)
        self.assertEqual(plan['settings']['max_item_tokens'],500000)
        self.runner.args.prefix='blind';self.runner.args.blind_pilot=True
        with self.assertRaisesRegex(ValueError,'incompatible with blind pilot'):self.runner.prepare()

    def test_document_from_matching_page_can_support_object_but_other_page_cannot(self):
        refs=[{'id':'R1','record_url':'matched','image_provenance':{'associated_with_page':True}}]
        for page,expected in [('matched','supported'),('other','tentative')]:
            out=receipt(self.answer(['D1']))
            rb.verify_assessments(out,self.visual('matching')['result'],refs,[{'id':'D1','record_url':page}])
            self.assertEqual(out['result']['depicted_object']['status'],expected)

    def test_reference_photo_dates_do_not_date_input_even_for_matching_building(self):
        for verdict in ('different','matching'):
            for ids in (['R1'],['D1']):
                out=receipt(self.answer(['R1'],False))
                out['result']['photograph_date']=assessment('supported',ids,'1972')
                out['result']['date_basis']={'kind':'photo_record','explanation':'Reference caption','reference_ids':ids}
                refs=[{'id':'R1','record_url':'page'}];docs=[{'id':'D1','record_url':'page'}]
                rb.verify_assessments(out,self.visual(verdict)['result'],refs,docs)
                self.runner.check_result(out['result'],refs,docs)
                self.runner.settings['research_goal']='dating'
                self.assertFalse(self.runner.goal_met(out['result']))
                self.assertIsNone(out['result']['photograph_date']['value'])

    def test_explicit_photograph_record_binding_can_date_input(self):
        binding=self.binding(['photograph_date'])
        out=receipt(self.answer([],False))
        out['result']['photograph_date']=assessment('supported',['D1'],'1972')
        out['result']['date_basis']={'kind':'photo_record','explanation':'Bound photograph record','reference_ids':['D1']}
        docs=[{'id':'D1','record_url':binding['record_url']}]
        rb.verify_assessments(out,{'comparisons':[]},[],docs,[binding])
        self.runner.check_result(out['result'],[],docs)
        self.assertEqual(out['result']['photograph_date']['status'],'supported')
        again=deepcopy(out)
        rb.verify_assessments(out,{'comparisons':[]},[],docs,[binding])
        self.assertEqual(out,again)

    def test_visible_time_bound_cannot_be_supported_and_rejected_images_cannot_supply_it(self):
        for verdict,expected in [('matching','unresolved'),('different','unresolved')]:
            out=receipt(self.answer(['R1'],False))
            out['result']['photograph_date']=assessment('supported',['R1'],'After 1972')
            out['result']['date_basis']={'kind':'visible_time_bound','explanation':'Sign','reference_ids':['R1']}
            rb.verify_assessments(out,self.visual(verdict)['result'],[{'id':'R1','record_url':'page'}],[])
            self.assertEqual(out['result']['photograph_date']['status'],expected)

    def run_queue(self, mode='adaptive', tokens=500000, goal=False, followup=True):
        self.runner.settings.update(discovery_mode=mode,visual_verification=True,max_item_tokens=tokens)
        pool=[{'image_sha256':str(i),'cid':f'C{i}','local_image':str(self.preview),
               'source_id':'museum','record_url':f'https://museum.example/{i}'} for i in range(8)]
        calls=[];gathers=[]
        def gather(*args):
            gathers.append(True)
            return (deepcopy(pool),[],[]) if len(gathers)==1 else ([],[],[])
        def call(stem,prompt,schema,images=(),**kw):
            calls.append((stem,prompt))
            if schema is rb.DISCOVERY:return receipt(self.discovery)
            if stem.endswith('adaptive'):return self.adaptive_search()
            if stem.endswith('shortlist'):return receipt({'picks':[], 'next':{'reason':'','search_terms':[],'place_names':[]}})
            ids=[f'R{i+1}' for i in range(len(images)-1)]
            if stem.endswith('visual'):
                return receipt({'comparisons':[{'reference_id':rid,'verdict':'matching' if goal else 'different',
                    'matches':'geometry','conflicts':'','limitation':''} for rid in ids],
                    'paired_crops':[],'unresolved_feature':'windows'})
            value=self.answer(ids,goal)
            # Only the first batch asks a question. Later inspection must preserve it.
            if followup and '-r1-' in stem:
                value['needs_more_references']=True
                value['next']={'reason':'Unresolved ornament','search_terms':['chimney diamond ornament'],'place_names':[]}
            return receipt(value)
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner.fetcher,'record',return_value=[]), \
             patch.object(self.runner,'retrieve',side_effect=RefactorTests.fake_retrieve(self)) as retrieve, \
             patch.object(self.runner,'gather',side_effect=gather):
            row=self.runner.process(self.item,1)
        return row,calls,retrieve.call_count

    def test_pending_inspection_preserves_second_discovery_and_features(self):
        row,calls,_=self.run_queue()
        searches=[(stem,prompt) for stem,prompt in calls if stem.endswith('adaptive')]
        self.assertEqual(len(searches),2)
        self.assertIn('chimney diamond ornament',searches[1][1])
        self.assertEqual(row['discovery_rounds'],2)
        self.assertEqual([r['inspection_only'] for r in row['rounds']],[False,True,False])
        self.assertEqual(row['uninspected_candidates'],[])
        inspect=next(prompt for stem,prompt in calls if stem.endswith('r2-compare'))
        self.assertIn('FEATURES: ["windows"]',inspect)

    def test_portal_pending_inspection_preserves_second_retrieval(self):
        row,calls,retrievals=self.run_queue('portals')
        self.assertEqual(retrievals,2)
        self.assertEqual(row['discovery_rounds'],2)
        self.assertEqual(row['rounds'][2]['queries']['priority_queries'],['chimney diamond ornament'])

    def test_last_discovery_still_inspects_remaining_images_without_extra_search(self):
        self.runner.settings['max_rounds']=1
        row,calls,_=self.run_queue()
        self.assertEqual(row['discovery_rounds'],1)
        self.assertEqual(len(row['rounds']),2)
        self.assertEqual(row['uninspected_candidates'],[])
        self.assertEqual(row['stop_reason'],'round_limit')
        self.assertEqual(sum(stem.endswith('adaptive') for stem,_ in calls),1)

    def test_pending_loop_stops_on_goal_budget_or_no_question(self):
        for kwargs,stop in [({'goal':True},'identified'),({'tokens':250},'usage_limit'),
                            ({'followup':False},'search_exhausted')]:
            with self.subTest(kwargs=kwargs):
                row,calls,_=self.run_queue(**kwargs)
                self.assertEqual(row['stop_reason'],stop)
                self.assertEqual(sum(stem.endswith('adaptive') for stem,_ in calls),1)
                self.assertLessEqual(len(row['rounds']),2)

    def test_overview_has_input_banner_and_details_have_numbered_banners(self):
        self.runner.settings.update(native_crops=True,discovery_mode='adaptive')
        self.item['preview_recipe']={'visual_checked':True}
        with patch.object(rb,'protected_source',return_value=self.preview), \
             patch.object(rb,'labelled',wraps=rb.labelled) as labelled:
            self.runner.research_input(self.item,'overview')
            self.runner.native_details(self.item,[{'label':'window','x':0,'y':0,'w':.5,'h':.5}],'detail')
        self.assertEqual([c.args[1] for c in labelled.call_args_list],['INPUT','INPUT native detail 1'])

    def test_oversize_content_length_fails_before_body_read_and_is_cached(self):
        url='https://museum.example/large.jpg'
        with patch.object(self.runner.fetcher,'validate'),patch.object(rb.urllib.request,'build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.headers={'Content-Length':'13000000'}
            with self.assertRaisesRegex(ValueError,'12 MB'):self.runner.fetcher.fetch(url)
            response.read.assert_not_called()
            with self.assertRaisesRegex(ValueError,'12 MB'):self.runner.fetcher.fetch(url)
            self.assertEqual(opener.call_count,1)
        meta=rb.read(next(self.root.glob('test-http-*.json')))
        self.assertGreater(meta['retry_after']-rb.time.time(),300)

    def test_layout_modifiers_do_not_hide_main_photographs(self):
        for cls in ('sidebar-right','footer-fixed','related-layout'):
            page=rb.Page();page.feed(f'<div class="{cls}"><img src="/main.jpg"></div>')
            self.assertEqual([p['src'] for p in rb.ranked_page_images(page.images,'')],['/main.jpg'])


class EvidenceCoverageTests(unittest.TestCase):
    setUp = RefactorTests.setUp
    answer = ReviewFixTests.answer
    visual = ResearchQualityTests.visual
    adaptive_search = ResearchQualityTests.adaptive_search

    def test_event_and_location_require_the_appropriate_visual_relationship(self):
        for relation,event,location in [('same_object_or_site','tentative','supported'),
                ('same_technical_type','tentative','tentative'),('same_event','supported','supported'),
                ('same_photograph','supported','supported'),('unestablished','tentative','tentative')]:
            for ids in (['R1'],['D1']):
                with self.subTest(relation=relation,ids=ids):
                    result=self.answer(['R1']);result['event']=assessment('supported',ids,'Opening ceremony')
                    result['location']=assessment('supported',ids,'Berlin')
                    visual=self.visual('matching')['result'];visual['comparisons'][0]['relationship']=relation
                    out=receipt(result);refs=[{'id':'R1','record_url':'page'}];docs=[{'id':'D1','record_url':'page'}]
                    rb.verify_assessments(out,visual,refs,docs)
                    self.runner.check_result(out['result'],refs,docs)
                    self.assertEqual(out['result']['event']['status'],event)
                    self.assertEqual(out['result']['location']['status'],location)

    def test_event_binding_remains_independent_of_an_incidental_reference(self):
        out=receipt(self.answer(['R1'],False));out['result']['event']=assessment('supported',['D1'],'Ceremony')
        rb.verify_assessments(out,self.visual('different')['result'],[{'id':'R1','record_url':'page'}],
            [{'id':'D1','record_url':'record'}],[{'record_url':'record','fields':['event']}])
        self.assertEqual(out['result']['event']['status'],'supported')

    def test_input_time_clue_requires_a_specific_input_observation(self):
        for observation,expected in [('', 'unresolved'),('Different text','unresolved'),
                ('INPUT banner reads 1972','tentative')]:
            out=receipt(self.answer([],False))
            out['result']['observations']=[{'reference_id':'INPUT','description':'INPUT banner reads 1972'}]
            out['result']['photograph_date']=assessment('supported',['INPUT'],'After 1972')
            out['result']['date_basis']={'kind':'visible_time_bound','reference_ids':['INPUT'],
                'explanation':'A visible clue','input_observation':observation}
            rb.verify_assessments(out,{'comparisons':[]},[],[])
            self.runner.check_result(out['result'],[])
            self.assertEqual(out['result']['photograph_date']['status'],expected)

    def gallery(self, count):
        return [{'source_id':'museum','title':'Gallery','record_url':'https://museum.example/gallery',
                 'candidate_image_url':f'https://museum.example/{i}.jpg'} for i in range(count)]

    def test_gather_reports_bounded_deferred_urls_without_marking_them_seen(self):
        seen=set()
        with patch.object(self.runner.fetcher,'image',side_effect=lambda url:{'image_sha256':'hash:'+url}):
            pool,_,issues=self.runner.gather(self.gallery(100),{},seen,1)
            coverage=self.runner.gather_coverage
            self.assertEqual(len(pool),8);self.assertEqual(issues,[])
            self.assertEqual(coverage['deferred_count'],92)
            self.assertEqual(len(coverage['deferred']),rb.MAX_DEFERRED)
            self.assertEqual(coverage['omitted_deferred_count'],44)
            self.assertNotIn('https://museum.example/8.jpg',seen)
            more,_,_=self.runner.gather(coverage['deferred'],{},seen,2)
            self.assertEqual(more[0]['candidate_image_url'],'https://museum.example/8.jpg')

    def test_record_image_limit_is_reported_and_footer_widgets_excluded(self):
        body=('<title>Gallery</title>'+''.join(f'<img src="/{i}.jpg">' for i in range(30))+
              '<div class="footer-widgets"><img src="/unrelated.jpg"></div>').encode()
        meta={'content_type':'text/html','final_url':'https://museum.example/gallery'}
        with patch.object(self.runner.fetcher,'fetch',return_value=(body,meta)):
            rows=self.runner.fetcher.record({'source_id':'museum','url':meta['final_url'],'relevance':'facade'})
        self.assertEqual(rows[0]['gallery_coverage'],{'observed_candidates':30,'emitted_candidates':24,'omitted_by_record_limit':6})
        self.assertFalse(any(r.get('candidate_image_url','').endswith('unrelated.jpg') for r in rows))

    def gallery_pipeline(self, mode):
        self.runner.settings.update(discovery_mode=mode,visual_verification=True,max_rounds=1,max_item_tokens=500000)
        active=[];searches=[]
        compare=self.runner.compare_verified
        def compared(item,refs,*args,**kw):
            active[:]=refs
            return compare(item,refs,*args,**kw)
        def call(stem,prompt,schema,images=(),**kw):
            if schema is rb.DISCOVERY:return receipt(self.discovery)
            if stem.endswith('adaptive'):
                searches.append(stem);return self.adaptive_search()
            if stem.endswith('shortlist'):return receipt({'picks':[],'next':{'reason':'','search_terms':[],'place_names':[]}})
            target=[r['id'] for r in active if r['candidate_image_url'].endswith('/8.jpg')]
            if stem.endswith('visual'):
                return receipt({'comparisons':[{'reference_id':r['id'],'verdict':'matching' if r['id'] in target else 'different',
                    'relationship':'same_object_or_site','matches':'facade geometry','conflicts':'','limitation':''} for r in active],
                    'paired_crops':[],'unresolved_feature':'facade'})
            result=self.answer([r['id'] for r in active],False)
            if target:result['depicted_object']=assessment('supported',target,'Ninth gallery building')
            return receipt(result)
        with patch.object(self.runner,'call',side_effect=call), \
             patch.object(self.runner,'compare_verified',side_effect=compared), \
             patch.object(self.runner.fetcher,'record',return_value=self.gallery(15)), \
             patch.object(self.runner,'retrieve',side_effect=RefactorTests.fake_retrieve(self,self.gallery(15))), \
             patch.object(self.runner.fetcher,'image',side_effect=lambda url:{'image_sha256':'hash:'+url,'local_image':str(self.preview)}):
            row=self.runner.process(self.item,1)
        self.assertEqual(row['stop_reason'],'identified')
        self.assertEqual(row['discovery_rounds'],1)
        self.assertEqual(row['retrieval_continuations'],1)
        self.assertEqual(row['rounds'][0]['retrieval_coverage']['deferred_count'],7)
        self.assertEqual(row['comparison']['result']['depicted_object']['value'],'Ninth gallery building')
        self.assertLessEqual(len(row['rounds']),(1+rb.MAX_CONTINUATIONS)*(rb.MAX_POOL+1))

    def test_adaptive_reaches_ninth_gallery_image_without_new_search(self):
        self.gallery_pipeline('adaptive')

    def test_portals_reaches_ninth_gallery_image_without_new_search(self):
        self.gallery_pipeline('portals')

    def test_cached_portal_restores_observed_deferred_cdn_urls(self):
        self.runner.settings.update(discovery_mode='portals',max_rounds=1)
        queries={k:self.discovery[k] for k in ('subject','search_terms','place_names','candidate_queries')}
        urls=['https://media.example/photo.jpg','https://media.example/large.jpg']
        rb.write(self.runner.path('001-r1-retrieval.json'),{'queries':queries,'pool':[],
            'documents':[{'record_url':'https://museum.example/record','excerpt':'A photograph record'}],
            'issues':[],'deferred_candidates':[{'candidate_image_url':urls[0],'image_variants':urls}],
            'search':{'sources_used':[],'queries_remaining':6}})
        with patch.object(self.runner,'call',side_effect=[receipt(self.discovery),receipt(comparison(docs=['D1']))]):
            self.runner.process(self.item,1)
        self.assertTrue(set(urls)<=self.runner.fetcher.observed_media)

    def test_failed_retrieval_is_incomplete_coverage_in_both_modes(self):
        for mode in ('adaptive','portals'):
            with self.subTest(mode=mode):
                self.runner.args.prefix='failure-'+mode
                self.runner.settings.update(discovery_mode=mode,visual_verification=True)
                def call(stem,prompt,schema,images=(),**kw):
                    return self.adaptive_search() if mode=='adaptive' else receipt(self.discovery)
                with patch.object(self.runner,'call',side_effect=call), \
                     patch.object(self.runner.fetcher,'record',side_effect=ValueError('HTTP 503')), \
                     patch.object(self.runner,'retrieve',return_value=([],[{'source':'museum','error':'HTTP 503'}])):
                    row=self.runner.process(self.item,1)
                self.assertTrue(row['coverage_incomplete'])
                self.assertEqual(row['status'],'access_limited')


if __name__=='__main__':unittest.main()
