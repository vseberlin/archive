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
            PROJECT={**settings.PROJECT,'extraction_roots':str(config),'extraction_plan':str(self.plan)})
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

    def test_stale_result_link_and_invalid_word_refused(self):
        revision=self.current()
        self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}&word=999').status_code,409)
        self.assertEqual(self.client.get(f'/research/media/{self.key}/?revision=old').status_code,409)
        path=self.output/(self.key+'.json');value=json.loads(path.read_text());value['words'][0]['text']='BETA';path.write_text(json.dumps(value))
        self.assertEqual(self.client.get(f'/research/pages/{self.key}/?revision={revision}&word=0').status_code,409)

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
