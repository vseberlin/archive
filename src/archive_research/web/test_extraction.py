"""Read-only search/citation behavior on prepared synthetic extraction receipts."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, Client

from .fixtures import create_fixture
from .records import rebuild


class ExtractionTests(TestCase):
    def setUp(self):
        from archive_research import Source, Page, Recipe, Result, Artifact, Transform
        self.temp = tempfile.TemporaryDirectory(dir=settings.PILOT_ROOT/'cache', prefix='extraction-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('sample','records','cache','exports'):(self.root/name).mkdir()
        create_fixture(self.root)
        self.output=self.root/'cache'/'extracted';self.output.mkdir()
        config=self.root/'roots.toml'
        config.write_text(f'[roots.sample]\npath="{self.root}/sample"\naccess="read"\n'
                          f'[roots.artifacts]\npath="{self.output}"\naccess="work"\n')
        source_path=self.root/'sample'/'report-figure.pdf';stat=source_path.stat()
        source=Source('synthetic','synthetic#report','report','sample',source_path.name,stat.st_size,stat.st_mtime_ns,role='synthetic')
        self.page=Page(source,1,'pdf',14,600,500,printed_label='12',units='pdf-points')
        recipe=Recipe.create('pdf-text','1',coverage={'page':14})
        self.key=recipe.cache_key(self.page)
        preview=self.output/(self.key+'.png');shutil.copyfile(self.root/'sample'/'report-figure.png',preview)
        raw=self.output/(self.key+'.bbox.xhtml');raw.write_text('Synthetic retained raw output')
        artifacts=tuple(Artifact('artifacts',p.name,k,hashlib.sha256(p.read_bytes()).hexdigest())
                        for p,k in [(preview,'processed-page'),(raw,'native-or-embedded-pdf-text')])
        result=Result(self.page,recipe,'complete',.1,artifacts,
                      transform=Transform(1000,834,(.6,0,0,0,500/834,0)))
        stamps={p.name:{'size_bytes':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in (preview,raw)}
        receipt={'result':result.record(),'artifact_stamps':stamps,'display_rotation':0,'words':[
            {'text':'ALPHA','region':{'x':.1,'y':.2,'width':.2,'height':.1},'confidence':None}]}
        (self.output/(self.key+'.json')).write_text(json.dumps(receipt))
        self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'format':'archive-research-plan-1','metadata_adopted':False,
            'tasks':[{'id':'synthetic-page','page':asdict(self.page),'extractor':'pdf-text'}]}))
        summary={'plan_sha256':hashlib.sha256(self.plan.read_bytes()).hexdigest(),
                 'attempts':[{'id':'synthetic-page','key':self.key,'state':'complete'}]}
        (self.output/'run-summary.json').write_text(json.dumps(summary))
        self.override=override_settings(PILOT_ROOT=self.root,ALLOWED_HOSTS=['testserver'],
            PROJECT={**settings.PROJECT,'extraction_roots':str(config),'extraction_plan':str(self.plan),
                     'research_index':None, 'research_packet':None,'research_assessment_response':None,
                     'research_assessment_transport':None,'visual_leads':None})
        self.override.enable();self.addCleanup(self.override.disable)
        rebuild()
        user=get_user_model().objects.create_user('extraction-operator',is_staff=True)
        self.client.force_login(user)
        self.history=(self.root/'records/assertions.jsonl').read_bytes()

    def current(self):
        response=self.client.get('/research/?q=ALPHA')
        self.assertEqual(response.status_code,200)
        return response.context['revision']

    def test_login_staff_and_read_only_requests(self):
        for path in ['/research/',f'/research/pages/{self.key}/',f'/research/media/{self.key}/',f'/research/sources/{self.key}/']:
            self.assertEqual(Client().get(path).status_code,302)
            self.assertEqual(self.client.post(path,{}).status_code,405)
        user=get_user_model().objects.create_user('nonstaff')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/research/').status_code,403)

    def test_search_hit_citation_and_history_unchanged(self):
        with patch('archive_research.runner.extract_one',side_effect=AssertionError('HTTP processing forbidden')):
            revision=self.current()
            response=self.client.get(f'/research/pages/{self.key}/?revision={revision}&word=0')
        self.assertContains(response,'ALPHA')
        self.assertContains(response,'left:10.000%')
        self.assertContains(response,'Existing comparison')
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(),self.history)
        self.assertEqual(self.client.get('/research/?q=ABSENT').context['hits'],[])

    def test_aggregate_search_uses_projection_then_verifies_only_selected_evidence(self):
        from archive_research.sources import Roots
        from archive_research.search_projection import build_projection
        roots = Roots.from_toml(self.root/'roots.toml')
        value = build_projection(roots, 'artifacts', {'keys': [self.key]})
        (self.output/'search-index.json').write_text(json.dumps(value))
        with override_settings(PROJECT={**settings.PROJECT, 'research_index': 'search-index.json'}):
            with patch('archive_research.receipts.read_current', side_effect=AssertionError('query reopens corpus')):
                response = self.client.get('/research/?q=ALPHA')
            self.assertContains(response, 'ALPHA')
            revision = response.context['revision']
            self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}').status_code, 200)
            path = self.output/(self.key+'.json')
            row = json.loads(path.read_text());row['words'][0]['text'] = 'changed'
            path.write_text(json.dumps(row))
            self.assertContains(self.client.get('/research/?q=ALPHA'), 'ALPHA')
            self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}').status_code, 409)
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(), self.history)

    def test_stale_result_link_and_invalid_word_refused(self):
        revision=self.current()
        self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}&word=999').status_code,409)
        self.assertEqual(self.client.get(f'/research/media/{self.key}/?revision=old').status_code,409)
        path=self.output/(self.key+'.json');value=json.loads(path.read_text());value['words'][0]['text']='BETA';path.write_text(json.dumps(value))
        self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}&word=0').status_code,409)

    def test_retained_citation_is_exact_offline_read_only_and_authenticated(self):
        from archive_research.sources import Roots
        from archive_research.result_records import retain
        roots = Roots.from_toml(self.root/'roots.toml')
        data = (self.output/(self.key+'.json')).read_bytes()
        digest = retain(roots, 'artifacts', json.loads(data), original_bytes=data)
        path = f'/research/evidence/{digest}/'
        (self.root/'sample'/self.page.source.path).unlink()
        for url in (path, path+'image/'):
            self.assertEqual(Client().get(url).status_code, 302)
            self.assertEqual(self.client.post(url).status_code, 405)
        self.assertContains(self.client.get(path), 'ALPHA')
        image = self.client.get(path+'image/')
        self.assertEqual(image.status_code, 200)
        self.assertTrue(b''.join(image.streaming_content).startswith(b'\x89PNG'))
        receipt = json.loads(data)
        image_hash = receipt['result']['artifacts'][0]['sha256']
        (self.output/('artifact-'+image_hash)).write_bytes(b'tampered')
        self.assertEqual(self.client.get(path).status_code, 409)
        self.assertEqual(self.client.get(path+'image/').status_code, 409)
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(), self.history)

    def test_changed_source_withholds_preview_and_original(self):
        revision=self.current()
        (self.root/'sample'/self.page.source.path).write_bytes(b'changed')
        self.assertEqual(self.client.get('/research/').context['available'],0)
        for part in ('media','sources'):
            self.assertEqual(self.client.get(f'/research/{part}/{self.key}/?revision={revision}').status_code,409)

    def test_media_and_source_are_bound(self):
        revision=self.current()
        image=self.client.get(f'/research/media/{self.key}/?revision={revision}')
        self.assertEqual(image.status_code,200)
        self.assertTrue(b''.join(image.streaming_content).startswith(b'\x89PNG'))
        source=self.client.get(f'/research/sources/{self.key}/?revision={revision}')
        self.assertEqual(source.status_code,200)
        self.assertTrue(b''.join(source.streaming_content).startswith(b'%PDF'))
        self.assertEqual(source['Cache-Control'],'no-store')

    def test_missing_config_and_worker_failure_are_visible(self):
        with override_settings(PROJECT={}):
            self.assertContains(self.client.get('/research/'),'has not been configured')
        summary=self.output/'run-summary.json';value=json.loads(summary.read_text());value['attempts'][0]={'id':'synthetic-page','state':'worker_error','error':'Synthetic worker failed'};summary.write_text(json.dumps(value))
        self.assertContains(self.client.get('/research/'),'Synthetic worker failed')

    def leads_fixture(self):
        from archive_research import Source
        path=self.root/'sample'/'lead.jpg';path.write_bytes(b'\xff\xd8synthetic preview')
        info=path.stat()
        preview=Source('synthetic','synthetic#lead','preview','sample',path.name,
                       info.st_size,info.st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest())
        asset={'id':'synthetic#lead','title':'Invented image lead',
               'source':asdict(self.page.source),'preview':asdict(preview)}
        projection=self.root/'leads.json'
        projection.write_text(json.dumps({'format':'archive-visual-leads-1','metadata_adopted':False,
                                         'leads':[{'candidate':asset,'reference':asset,'similarity':.4}]}))
        self.override_leads=override_settings(PROJECT={**settings.PROJECT,'visual_leads':str(projection)})
        self.override_leads.enable();self.addCleanup(self.override_leads.disable)
        return path

    def test_image_leads_authenticated_read_only_and_bound(self):
        self.leads_fixture()
        for path in ['/research/leads/','/research/leads/0/candidate/']:
            self.assertEqual(Client().get(path).status_code,302)
            self.assertEqual(self.client.post(path).status_code,405)
        response=self.client.get('/research/leads/')
        self.assertContains(response,'Invented image lead')
        revision=response.context['revision']
        image=self.client.get(f'/research/leads/0/candidate/?revision={revision}')
        self.assertEqual(image.status_code,200)
        self.assertTrue(b''.join(image.streaming_content).startswith(b'\xff\xd8'))
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(),self.history)
        self.assertEqual(self.client.get('/research/leads/0/candidate/?revision=stale').status_code,409)

    def test_missing_visual_source_withholds_existing_preview(self):
        preview=self.leads_fixture()
        revision=self.client.get('/research/leads/').context['revision']
        (self.root/'sample'/self.page.source.path).unlink()
        self.assertTrue(preview.exists())
        self.assertContains(self.client.get('/research/leads/'),'Source unavailable')
        self.assertEqual(self.client.get(f'/research/leads/0/candidate/?revision={revision}').status_code,409)

    def test_visual_preview_digest_checked(self):
        import os
        preview=self.leads_fixture();info=preview.stat()
        revision=self.client.get('/research/leads/').context['revision']
        preview.write_bytes(b'x'*info.st_size)
        os.utime(preview, ns=(info.st_atime_ns,info.st_mtime_ns))
        self.assertEqual(self.client.get(f'/research/leads/0/candidate/?revision={revision}').status_code,409)

    def packet_fixture(self):
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        selection = {'format':'archive-research-selection-1','metadata_adopted':False,
                     'case_key':'photo','question':'Synthetic profile comparison',
                     'profiles':['buildings','equipment','tubes','people'],
                     'evidence':[{'key':self.key,'label':'Invented caption','kind':'caption',
                                  'region':{'x':.1,'y':.2,'width':.2,'height':.1},
                                  'lineage':'One synthetic page.','attribution':'Invented test fixture.'}],
                     'hypotheses':[{'claim':'Unsupported identity remains unresolved',
                                    'supports':[],'contradicts':[0]}]}
        roots = Roots.from_toml(self.root/'roots.toml')
        packet = build_packet(roots,'artifacts',selection,research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        override = override_settings(PROJECT={**settings.PROJECT,'research_packet':'packet.json'})
        override.enable();self.addCleanup(override.disable)

    def test_packet_is_read_only_and_preserves_history(self):
        self.packet_fixture()
        for path in ['/research/packet/','/research/packet/media/0/','/research/packet/sources/0/']:
            self.assertEqual(Client().get(path).status_code,302)
            self.assertEqual(self.client.post(path).status_code,405)
        with patch('archive_research.runner.extract_one',side_effect=AssertionError('No HTTP processing')):
            page = self.client.get('/research/packet/')
        self.assertContains(page,'Synthetic profile comparison')
        self.assertContains(page,'left:10.000%')
        self.assertContains(page,'Buildings')
        self.assertContains(page,'Tubes')
        revision = page.context['revision']
        media = self.client.get(f'/research/packet/media/0/?revision={revision}')
        self.assertTrue(b''.join(media.streaming_content).startswith(b'\x89PNG'))
        source = self.client.get(f'/research/packet/sources/0/?revision={revision}')
        self.assertTrue(b''.join(source.streaming_content).startswith(b'%PDF'))
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(),self.history)
        self.assertEqual(self.client.get('/research/packet/media/0/?revision=stale').status_code,409)

    def test_packet_missing_source_and_changed_receipt_fail_closed(self):
        self.packet_fixture()
        revision = self.client.get('/research/packet/').context['revision']
        (self.root/'sample'/self.page.source.path).unlink()
        self.assertContains(self.client.get('/research/packet/'),'Source unavailable')
        self.assertEqual(self.client.get(f'/research/packet/media/0/?revision={revision}').status_code,409)
        self.assertEqual(self.client.get(f'/research/packet/sources/0/?revision={revision}').status_code,409)
        receipt = self.output/(self.key+'.json'); value = json.loads(receipt.read_text())
        value['words'][0]['text']='CHANGED';receipt.write_text(json.dumps(value))
        self.assertEqual(self.client.get('/research/packet/').status_code,409)

    def test_packet_unconfigured_and_outside_work_root(self):
        with override_settings(PROJECT={**settings.PROJECT,'research_packet':None}):
            self.assertContains(self.client.get('/research/packet/'),'No local evidence packet')
        with override_settings(PROJECT={**settings.PROJECT,'research_packet':'../outside.json'}):
            self.assertEqual(self.client.get('/research/packet/').status_code,409)

    def test_packet_displays_cross_reference_as_unreviewed_lead(self):
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        self.packet_fixture()
        selection=json.loads((self.output/'packet.json').read_text())['selection']
        selection['evidence'].append({**selection['evidence'][0], 'kind':'detail', 'label':'Facade detail'})
        selection['cross_references']=[{'source':0,'target':1,'basis':'visual','quote':'',
                                      'note':'Repeated windows alone cannot establish identity.'}]
        packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,
                            research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        response=self.client.get('/research/packet/')
        self.assertContains(response,'Cross-references')
        self.assertContains(response,'unreviewed lead')
        self.assertContains(response,'Repeated windows alone cannot establish identity.')

    def test_packet_distinguishes_visual_features_and_layout_gaps(self):
        from dataclasses import replace
        from archive_research import Recipe,Result
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        self.packet_fixture()
        original=json.loads((self.output/(self.key+'.json')).read_text())
        selection=json.loads((self.output/'packet.json').read_text())['selection']
        for extractor in ('visual','pdf-layout','scan-layout'):
            result=replace(Result.from_record(original['result']),recipe=Recipe.create(extractor,'synthetic'))
            key=result.recipe.cache_key(result.page,result.region)
            receipt={**original,'result':result.record(),'words':[],
                     'feature':{'coverage':{'figures_prepared':0,'embedded_images_seen':1},
                                'figures':[],'omitted':[{'reason':'full-page raster needs segmentation'}]}}
            (self.output/(key+'.json')).write_text(json.dumps(receipt))
            selection['evidence'][0]['key']=key
            packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,
                                research_profiles(selection['profiles']))
            (self.output/'packet.json').write_text(json.dumps(packet))
            response=self.client.get('/research/packet/')
            if extractor=='visual':
                self.assertContains(response,'OCR was not requested for this view.')
                self.assertNotContains(response,'No text extracted;')
            else:
                self.assertContains(response,'Figure and caption discovery')
                self.assertContains(response,'full-page raster needs segmentation')

    def test_bound_model_proposal_visible_without_changing_review_records(self):
        from archive_research.contracts import encoded
        self.packet_fixture();packet=json.loads((self.output/'packet.json').read_text())
        response={'id':'synthetic-assessment','model':'synthetic-model','status':'completed','output':[
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps({
                'observations':[{'claim':'Synthetic proposed reading','evidence':[0],'basis':'literal-text','uncertainty':'Needs review'}],
                'hypotheses':[],'unresolved':['Synthetic only'],'next_local_search':'Find another page'})}]}]}
        transport={'packet_sha256':hashlib.sha256(encoded(packet).encode()).hexdigest(),
                   'request_sha256':'0'*64,'model':'synthetic-model'}
        (self.output/'response.json').write_text(json.dumps(response));(self.output/'transport.json').write_text(json.dumps(transport))
        with override_settings(PROJECT={**settings.PROJECT,'research_assessment_response':'response.json',
                                        'research_assessment_transport':'transport.json'}):
            page=self.client.get('/research/packet/')
            self.assertContains(page,'Model assessment · unreviewed proposal')
            self.assertContains(page,'Synthetic proposed reading')
            transport['packet_sha256']='1'*64;(self.output/'transport.json').write_text(json.dumps(transport))
            self.assertEqual(self.client.get('/research/packet/').status_code,409)
        self.assertEqual((self.root/'records/assertions.jsonl').read_bytes(),self.history)

    def discovery_fixture(self):
        from archive_research import Artifact, Recipe, Result, Transform
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        self.packet_fixture()
        original=json.loads((self.output/(self.key+'.json')).read_text())
        recipe=Recipe.create('text-regions','1'); key=recipe.cache_key(self.page)
        artifacts=tuple(Artifact(**a) for a in original['result']['artifacts'])
        crop=Artifact(artifacts[0].root,artifacts[0].path,'text-region-crop-0',artifacts[0].sha256)
        result=Result(self.page,recipe,'partial',.1,artifacts+(crop,),
                      transform=Transform(1000,834,(.6,0,0,0,500/834,0)),limitations=('Unreadable candidate retained.',))
        region={'x':.1,'y':.2,'width':.2,'height':.1}
        receipt={**original,'result':result.record(),'words':[],
                 'discovery':{'coverage':{'requested_passes':2,'completed_passes':1,'scan_complete':False},
                              'regions':[{'region':region,'status':'unreadable','readings':[],
                                          'crop':crop.path,'variants':[],'observations':[0],'detection_score':.9}]}}
        (self.output/(key+'.json')).write_text(json.dumps(receipt))
        stored=json.loads((self.output/'packet.json').read_text());selection=stored['selection']
        selection['evidence'][0].pop('region');selection['evidence'][0]['discovery_key']=key
        packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        return key

    def test_unreadable_candidate_and_incomplete_coverage_visible(self):
        self.discovery_fixture()
        response=self.client.get('/research/packet/')
        self.assertContains(response,'Automatic text-region coverage')
        self.assertContains(response,'Incomplete scan')
        self.assertContains(response,'unreadable')
        self.assertContains(response,'No readable text returned. The detected region is retained.')
        revision=response.context['revision']
        crop=self.client.get(f'/research/packet/media/0/?revision={revision}&candidate=0')
        self.assertEqual(crop.status_code,200)
        self.assertTrue(b''.join(crop.streaming_content).startswith(b'\x89PNG'))
        self.assertEqual(self.client.get(f'/research/packet/media/0/?revision={revision}&candidate=9').status_code,409)

    def test_discovery_change_and_page_mismatch_withhold_packet(self):
        key=self.discovery_fixture()
        path=self.output/(key+'.json');receipt=json.loads(path.read_text())
        receipt['discovery']['regions'][0]['status']='changed';path.write_text(json.dumps(receipt))
        self.assertEqual(self.client.get('/research/packet/').status_code,409)
        receipt['result']['page']['index']=13;path.write_text(json.dumps(receipt))
        self.assertEqual(self.client.get('/research/packet/').status_code,409)

    def test_enhancement_links_are_bound_and_unknown_choices_refused(self):
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        key=self.discovery_fixture()
        path=self.output/(key+'.json');receipt=json.loads(path.read_text())
        crop=next(a for a in receipt['result']['artifacts'] if a['kind']=='text-region-crop-0')
        receipt['result']['artifacts'].append({**crop,'kind':'text-region-crop-0-levels'})
        receipt['discovery']['regions'][0]['enhancements']=[{'recipe':'original','crop':crop['path']},
                                                           {'recipe':'levels','crop':crop['path']}]
        path.write_text(json.dumps(receipt))
        selection=json.loads((self.output/'packet.json').read_text())['selection']
        packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        response=self.client.get('/research/packet/')
        self.assertContains(response,'Compare image preparations')
        revision=response.context['revision']
        base=f'/research/packet/media/0/?revision={revision}'
        self.assertContains(response,base.replace('&','&amp;')+'&amp;candidate=0&amp;enhancement=levels')
        for name in ('original','levels'):
            image=self.client.get(base+'&candidate=0&enhancement='+name)
            self.assertEqual(image.status_code,200)
            self.assertTrue(b''.join(image.streaming_content).startswith(b'\x89PNG'))
        for suffix in ('&candidate=0&enhancement=unknown','&enhancement=levels','&candidate=0&enhancement=../levels'):
            self.assertEqual(self.client.get(base+suffix).status_code,409)
        receipt['result']['artifacts']=[a for a in receipt['result']['artifacts'] if a['kind']!='text-region-crop-0-levels']
        path.write_text(json.dumps(receipt))
        packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        revision=self.client.get('/research/packet/').context['revision']
        self.assertEqual(self.client.get(f'/research/packet/media/0/?revision={revision}&candidate=0&enhancement=levels').status_code,409)

    def test_manual_lines_show_engine_and_do_not_claim_automatic_coverage(self):
        from archive_research import Recipe
        from archive_research.evidence import build_packet
        from archive_research.sources import Roots
        from archive_research.profiles import research_profiles
        old=self.discovery_fixture();receipt=json.loads((self.output/(old+'.json')).read_text())
        recipe=Recipe.create('line-ocr','1');key=recipe.cache_key(self.page)
        receipt['result']['recipe']=recipe.record()
        receipt['discovery']['coverage']={'mode':'manual_lines','scan_complete':False,'recognition_complete':True}
        receipt['discovery']['regions'][0].update(label='Complete type line',selection='manual full-line region',
            variants=[{'engine':'crnn-en','languages':'Latin/digits only','text':'abc123','status':'complete'}])
        (self.output/(key+'.json')).write_text(json.dumps(receipt))
        selection=json.loads((self.output/'packet.json').read_text())['selection'];selection['evidence'][0]['discovery_key']=key
        packet=build_packet(Roots.from_toml(self.root/'roots.toml'),'artifacts',selection,research_profiles(selection['profiles']))
        (self.output/'packet.json').write_text(json.dumps(packet))
        response=self.client.get('/research/packet/')
        self.assertContains(response,'Selected full-line comparison');self.assertContains(response,'crnn-en')
        self.assertContains(response,'Complete type line');self.assertNotContains(response,'Automatic text-region coverage')
        self.assertNotContains(response,'Incomplete scan.')
