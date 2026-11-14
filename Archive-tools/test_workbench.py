"""Regression tests using only synthetic files and temporary work areas."""
from copy import deepcopy
import csv
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

import archive_common as common
import workbench as wb


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-tools-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.obj = {'id': 'photo', 'title': 'Synthetic photo', 'metadata': {}, 'candidates': [],
                    'matches': [{'a': 'full', 'b': 'other', 'algorithm': 'pHash', 'distance': 1, 'profile': 'synthetic'}]}
        for cid, color in [('full', 'red'), ('crop', 'pink'), ('other', 'blue')]:
            path = self.root / (cid + '.tiff')
            Image.new('RGB', (80, 60), color).save(path)
            st = path.stat()
            self.obj['candidates'].append({'id': cid, 'label': cid, 'eligible': True, 'complete': True,
                                          'members': [{'path': str(path), 'size_bytes': st.st_size,
                                                       'mtime_ns': st.st_mtime_ns, 'pages': 1}]})
        self.config = {'format': wb.FORMAT, 'read_only': False, 'read_roots': [], 'completion_receipts': [], 'objects': [self.obj]}
        self.write_input()
        self.app = wb.Workbench(self.root, 'test reviewer')

    def write_input(self):
        (self.root / wb.INPUT).write_text(json.dumps(self.config))

    def test_checked_positive_view_inverts_rotates_and_preserves_source(self):
        member = self.obj['candidates'][0]['members'][0]
        recipe = {'visual_checked': True, 'page': 0, 'polarity': 'inverted research view', 'rotation': -90}
        record = {'object_id': 'photo', 'candidate_id': 'full', 'source_path': member['path'],
                  'source_stamp': {k: member[k] for k in ('size_bytes','mtime_ns')},
                  'result': {}, 'preview_recipe': recipe}
        sidecar = self.root / wb.MODEL_PROPOSALS
        def save():
            sidecar.write_text(json.dumps({'format':'archive-model-proposals-1','metadata_adopted':False,'records':[record]}))
        save(); source = Path(member['path']); original = source.read_bytes()
        data, mime = self.app.media('photo','full',0,1,1200)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size,(60,80))
            r,g,b = image.getpixel((20,20))
            self.assertLess(r,8); self.assertGreater(g,247); self.assertGreater(b,247)
        self.assertEqual(source.read_bytes(),original)
        self.assertIsNone(self.app.display_recipe(member,2))
        self.assertIsNone(self.app.display_recipe(self.obj['candidates'][1]['members'][0],1))
        recipe['visual_checked']=False;save()
        self.assertIsNone(self.app.display_recipe(member,1))
        recipe['visual_checked']=True;record['source_stamp']['mtime_ns']+=1;save()
        self.assertIsNone(self.app.display_recipe(member,1))

    def test_model_proposals_bind_only_to_exact_source_without_changing_decisions(self):
        before = self.app.snapshot('photo')
        member = self.obj['candidates'][0]['members'][0]
        record = {'object_id': 'photo', 'candidate_id': 'full', 'source_path': member['path'],
                  'source_stamp': {k: member[k] for k in ('size_bytes', 'mtime_ns')},
                  'result': {'title': {'value': 'Unreviewed model title'}},
                  'ocr_research': {'status': 'partial', 'text_verified': False,
                                   'runs': [{'raw_text_excerpt': '<untrusted OCR>'}]}}
        sidecar = self.root / wb.MODEL_PROPOSALS
        sidecar.write_text(json.dumps({'format': 'archive-model-proposals-1', 'metadata_adopted': False,
                                      'records': [record]}))
        after = self.app.snapshot('photo')
        self.assertEqual(after['decision'], before['decision'])
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['object']['metadata'], {})
        self.assertEqual(after['model_proposal']['candidate_id'], 'full')
        self.assertEqual(after['model_proposal']['ocr_research'], record['ocr_research'])
        self.assertEqual(self.app.snapshot()['model_proposal_count'], 1)
        record['source_stamp']['mtime_ns'] += 1
        sidecar.write_text(json.dumps({'format': 'archive-model-proposals-1', 'metadata_adopted': False,
                                      'records': [record]}))
        self.assertIsNone(self.app.snapshot('photo')['model_proposal'])
        self.assertIn('source bindings', self.app.snapshot()['model_proposal_warning'])
        sidecar.write_text('{broken')
        self.assertIsNone(self.app.snapshot('photo')['model_proposal'])
        self.assertIn('unavailable', self.app.snapshot()['model_proposal_warning'])

    def metadata_payload(self):
        member = self.obj['candidates'][0]['members'][0]
        record = {'object_id': 'photo', 'candidate_id': 'full', 'source_path': member['path'],
                  'source_stamp': {k: member[k] for k in ('size_bytes', 'mtime_ns')},
                  'result': {'title': {'value': 'Original hypothesis'}}}
        (self.root / wb.MODEL_PROPOSALS).write_text(json.dumps({'format': 'archive-model-proposals-1',
            'metadata_adopted': False, 'records': [record]}))
        return {'object_id': 'photo', 'revision': self.app.snapshot('photo')['metadata_revision'],
                'fields': {k: 'Edited title' if k == 'title' else '' for k in wb.METADATA_FIELDS}}

    def test_metadata_roundtrip_preserves_decisions_and_evidence(self):
        payload = self.metadata_payload()
        before = deepcopy(self.app.snapshot('photo')['decision'])
        original = (self.root / wb.MODEL_PROPOSALS).read_bytes()
        source = (self.root / 'full.tiff').read_bytes()
        self.app.save_metadata(payload)
        saved = deepcopy(wb.Workbench(self.root, 'another reviewer').snapshot('photo')['decision'])
        metadata = saved.pop('metadata_proposal')
        self.assertEqual(saved, before)
        self.assertEqual(metadata['fields'], payload['fields'])
        self.assertFalse(metadata['metadata_adopted'])
        self.assertEqual((self.root / wb.MODEL_PROPOSALS).read_bytes(), original)
        self.assertEqual((self.root / 'full.tiff').read_bytes(), source)
        self.assertTrue(self.app.snapshot()['projection_agrees'])
        self.assertEqual(json.loads(self.csv_rows()[0]['metadata_proposal']), metadata)
        self.app.save(self.payload(note='Separate review note'))
        self.assertEqual(self.app.snapshot('photo')['decision']['metadata_proposal'], metadata)

    def test_latest_research_reaches_editor_and_queue_without_rewriting_evidence(self):
        self.metadata_payload()
        path = self.root / wb.MODEL_PROPOSALS
        data = json.loads(path.read_text()); record = data['records'][0]
        record['result'].update(description={'value':'Old scene'}, date={'value':'1930','basis':'visual_estimate'})
        record['reference_check'] = {'status':'compared','result':{
            'depicted_object':{'status':'supported','value':'Pfefferberg street frontage'},
            'location':{'status':'supported','value':'Schönhauser Allee 176, Berlin'},
            'photograph_date':{'status':'unresolved','value':None,'explanation':'Exposure date unknown'},
            'observations':[{'reference_id':'INPUT','description':'Delivery tricycle outside the bank.'}],
            'hypotheses':[{'candidate':'Pfefferberg','status':'supported'},
                          {'candidate':'Alternative','status':'contradicted'},
                          {'candidate':'Vehicle model','status':'tentative','assessment':'Not proven'}],
            'limitations':['No dated original print']}}
        path.write_text(json.dumps(data)); before = path.read_bytes()
        snapshot = self.app.snapshot('photo')
        fields = self.browser_action(snapshot, '', 'proposalFields(snapshot.model_proposal)')
        self.assertEqual(fields['title'],'Pfefferberg street frontage')
        self.assertIn('Delivery tricycle',fields['description'])
        self.assertNotIn('Old scene',fields['description'])
        self.assertEqual(fields['places'],'Schönhauser Allee 176, Berlin')
        self.assertEqual(fields['date'],'')
        self.assertEqual(self.app.snapshot()['objects'][0]['model_proposal_title'],fields['title'])
        hypotheses = self.browser_action(snapshot, '', 'locationHypotheses(snapshot.model_proposal)')
        self.assertEqual([h['title'] for h in hypotheses],['Vehicle model'])
        self.assertEqual(path.read_bytes(),before)
        self.app.save_metadata({'object_id':'photo','revision':snapshot['metadata_revision'],
                                'fields':{**fields,'title':'Reviewer wording'}})
        self.assertEqual(self.app.snapshot()['objects'][0]['model_proposal_title'],'Reviewer wording')
        self.assertEqual(self.app.snapshot('photo')['decision']['metadata_proposal']['fields']['title'],'Reviewer wording')
        self.assertEqual(path.read_bytes(),before)

    def test_tentative_identity_and_reviewed_editorial_corrections_keep_their_status(self):
        proposal={'result':{'title':{'value':'Machines'}},'reference_check':{'status':'compared',
            'result':{'depicted_object':{'status':'tentative','value':'Wetzig H'},
                      'location':{'status':'tentative','value':'Berlin'}}}}
        fields=wb.proposal_fields(proposal)
        self.assertEqual(fields['title'],'Machines');self.assertEqual(fields['places'],'Berlin (tentative)')
        proposal['consolidated_proposal']={'title':'Reviewed scene','description':'Corrected lettering'}
        self.assertEqual(wb.proposal_fields(proposal)['description'],'Corrected lettering')
        proposal['reference_check']['status']='failed'
        self.assertEqual(wb.proposal_fields(proposal)['places'],'')

    def test_research_override_saves_separately_and_automatic_can_be_restored(self):
        payload=self.metadata_payload()
        payload['research_routing']={'primary':'machinery_industry','secondary':['people_activity']}
        result=self.app.save_metadata(payload)
        self.assertEqual(result['research_routing']['primary'],'machinery_industry')
        self.assertTrue(result['research_routing']['reviewer_override'])
        self.assertEqual(result['decision']['metadata_proposal']['fields'],payload['fields'])
        payload['revision']=result['metadata_revision'];payload['research_routing']=None
        result=self.app.save_metadata(payload)
        self.assertFalse(result['research_routing']['reviewer_override'])
        self.assertNotIn('research_routing',result['decision']['metadata_proposal'])

    def test_metadata_rejects_stale_saves_and_changed_research(self):
        payload = self.metadata_payload()
        self.app.save_metadata(payload)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.save_metadata(payload)
        payload['revision'] = self.app.snapshot('photo')['metadata_revision']
        sidecar = self.root / wb.MODEL_PROPOSALS
        sidecar.write_text(sidecar.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.save_metadata(payload)

    def test_metadata_rejects_missing_source_and_read_only(self):
        payload = self.metadata_payload()
        (self.root / 'full.tiff').unlink()
        with self.assertRaises((ValueError, OSError)):
            self.app.save_metadata(payload)
        self.assertFalse((self.root / wb.DECISIONS).exists())
        self.config['read_only'] = True
        self.write_input()
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.app.save_metadata(payload)

    def test_metadata_rejects_unexpected_fields(self):
        payload = self.metadata_payload()
        payload['fields']['selected_candidate_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'fields'):
            self.app.save_metadata(payload)
        payload['fields'].pop('selected_candidate_id')
        payload['fields']['title'] = {'value': 'not text'}
        with self.assertRaisesRegex(ValueError, 'text'):
            self.app.save_metadata(payload)

    def payload(self, **changes):
        snapshot = self.app.snapshot('photo')
        data = {key: deepcopy(snapshot['decision'][key]) for key in wb.EDIT_FIELDS}
        data.update(object_id='photo', revision=snapshot['revision'])
        data.update(changes)
        return data

    def csv_rows(self):
        return list(csv.DictReader(io.StringIO((self.root / wb.CSV).read_text())))

    def browser_action(self, snapshot, actions, result='draft'):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8')), nodes = {};
const context = vm.createContext({structuredClone, snapshot: input.snapshot,
  document: {querySelectorAll: () => [], getElementById: id => nodes[id] ??= {hidden: true}}});
const source = fs.readFileSync(input.source, 'utf8').split('$("search").addEventListener')[0];
vm.runInContext(source, context);
vm.runInContext('current=snapshot; draft=structuredClone(current.decision); overview={token:"test"}; renderDecisions=renderObject=renderQueue=()=>{};', context);
(async () => {
  await vm.runInContext(input.actions, context);
  process.stdout.write(vm.runInContext('JSON.stringify(' + input.result + ')', context));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        data = {'source': str(Path(wb.__file__).with_suffix('.js')), 'snapshot': snapshot,
                'actions': actions, 'result': result}
        return json.loads(subprocess.check_output(['node', '-e', script], input=json.dumps(data).encode()))

    def test_browser_preserves_research_alternatives_without_reviving_superseded_leads(self):
        proposal = {'result': {'research_leads': [
            '1. Bahnhof Friedrichstraße — schwache Arbeitshypothese: Gleise fehlen.',
            '2. Alter Friedrichstadt-Palast — schwache Alternative: Kein eindeutiges Theatermerkmal.',
            '3. Deutsche Oper Berlin — nachrangige Hypothese: Vergleichsfotos erforderlich.']}}
        result = self.browser_action(self.app.snapshot('photo'), '',
            'locationHypotheses(' + json.dumps(proposal) + ')')
        self.assertEqual([r['title'] for r in result],
            ['Bahnhof Friedrichstraße', 'Alter Friedrichstadt-Palast', 'Deutsche Oper Berlin'])
        self.assertIn('Gleise fehlen', result[0]['reasoning'])
        proposal['consolidated_proposal'] = {'places': 'Corrected location'}
        self.assertEqual(self.browser_action(self.app.snapshot('photo'), '',
            'locationHypotheses(' + json.dumps(proposal) + ')'), [])

    def test_browser_reference_conclusion_uses_final_fields_not_stale_summary(self):
        result={'summary':'Definitely identified and dated 1972',
                'depicted_object':{'status':'tentative','value':'Candidate building'},
                'location':{'status':'unresolved','value':None},
                'photograph_date':{'status':'unresolved','value':None}}
        shown=self.browser_action(self.app.snapshot('photo'),'',
            'referenceConclusion('+json.dumps(result)+')')
        self.assertIn('Object: tentative',shown)
        self.assertIn('Location: unresolved',shown)
        self.assertIn('Photograph date: unresolved',shown)
        self.assertNotIn('Definitely',shown);self.assertNotIn('1972',shown)

    def test_browser_consolidation_overrides_superseded_fields(self):
        proposal = {'result': {'title': {'value': 'Bear'}, 'description': {'value': 'Old guess'},
            'date': {'display': '1928', 'basis': 'hypothesis'}, 'subjects': [{'value': 'Bear'}]},
            'consolidated_proposal': {'title': 'Jaguar', 'description': 'People around a sculpture',
                'date': '', 'subjects': 'Jaguar', 'notes': 'Sculpture date is not photograph date'}}
        fields = self.browser_action(self.app.snapshot('photo'), '',
            'proposalFields(' + json.dumps(proposal) + ')')
        self.assertEqual(fields['title'], 'Jaguar')
        self.assertEqual(fields['date'], '')
        self.assertNotIn('Bear', json.dumps(fields))
        self.assertEqual(set(fields), wb.METADATA_FIELDS)

    def test_browser_metadata_save_failure_keeps_edits_and_does_not_advance(self):
        self.metadata_payload()
        snap = self.app.snapshot('photo')
        result = self.browser_action(snap,
            'draft.metadata_proposal={fields:{title:"Unsaved edit"}}; dirty=true; '
            'navigateStep=()=>{throw Error("Unexpected advance")}; '
            'api=async()=>{throw Error("Connection lost")}; saveMetadata(true);',
            '({title:draft.metadata_proposal.fields.title,dirty,saving})')
        self.assertEqual(result, {'title': 'Unsaved edit', 'dirty': True, 'saving': False})

    def test_browser_master_switch_preserves_unordered_pair_without_duplicate(self):
        snap = self.app.save(self.payload(selected_candidate_id='full', same_pairs=[['full', 'crop']]))
        draft = self.browser_action(snap, "selectMaster('crop'); selectMaster('full');")
        self.assertEqual(draft['same_pairs'], [['full', 'crop']])
        result = self.app.save(self.payload(**{key: draft[key] for key in wb.EDIT_FIELDS}))
        self.assertEqual(result['decision']['redundant_candidate_ids'], ['crop'])

    def test_browser_tiff_confirmation_preserves_choice_and_saves_md_proposal(self):
        self.discovery_policy()
        jpeg = self.root / 'substitute.jpg'
        Image.new('RGB', (80, 60), 'red').save(jpeg)
        reference = self.obj['candidates'][1]
        reference.update(archive_unit='unit', source_record={
            'object_selection_basis': 'curator_confirmed_unreadable_master_replacement'})
        reference['members'] = [{'path': str(jpeg), 'size_bytes': jpeg.stat().st_size,
                                 'mtime_ns': jpeg.stat().st_mtime_ns, 'pages': 1}]
        del self.obj['candidates'][2]['reference_object_id']
        self.obj['candidates'][2].update(eligible=False, source_role='derivative')
        self.config['destination_units'] = [{'path': 'unit', 'label': 'Synthetic archive'}]
        self.obj['matches'] = [{'a': 'full', 'b': 'crop', 'engine': 'sift', 'status': 'supported'}]
        self.write_input()
        before = jpeg.read_bytes()
        snap = self.app.save(self.payload(selected_candidate_id='full', same_pairs=[['other', 'full']]))
        draft = self.browser_action(snap, "samePhoto('full','crop'); selectMaster('full');")
        self.assertEqual(draft['selected_candidate_id'], 'full')
        self.assertNotIn('crop', draft['redundant_candidate_ids'])
        result = self.app.save(self.payload(**{key: draft[key] for key in wb.EDIT_FIELDS}))
        self.assertEqual(result['selection_context']['action'], 'proposed_master_upgrade')
        self.assertEqual(result['selection_context']['archive_object_id'], 'unit#md:1')
        self.assertEqual(result['decision']['redundant_candidate_ids'], ['other'])
        self.assertEqual(jpeg.read_bytes(), before)

    def test_browser_reports_save_rejection_and_keeps_draft(self):
        result = self.browser_action(self.app.snapshot('photo'), '''
dirty=true; draft.note='keep my draft';
globalThis.fetch=async()=>({ok:false,status:409,json:async()=>({error:'Duplicate same-photo pair'})});
save();''', '({status:$("save-status").textContent,dirty,note:draft.note,saving})')
        self.assertIn('Duplicate same-photo pair', result['status'])
        self.assertTrue(result['dirty'])
        self.assertEqual(result['note'], 'keep my draft')
        self.assertFalse(result['saving'])

    def test_browser_ordinary_confirmation_keeps_archive_master(self):
        self.discovery_policy()
        self.obj['candidates'][1]['archive_unit'] = 'unit'
        self.config['destination_units'] = [{'path': 'unit', 'label': 'Synthetic archive'}]
        self.write_input()
        draft = self.browser_action(self.app.snapshot('photo'), "selectMaster('full'); samePhoto('full','crop');")
        self.assertEqual(draft['selected_candidate_id'], 'crop')
        result = self.app.save(self.payload(**{key: draft[key] for key in wb.EDIT_FIELDS}))
        self.assertEqual(result['selection_context']['action'], 'retain_archive_master')

    def test_browser_does_not_report_acknowledged_save_as_failed(self):
        result = self.browser_action(self.app.snapshot('photo'), '''
dirty=true;
globalThis.fetch=async path=>{
  if(path==='/api/decision') return {ok:true,json:async()=>snapshot};
  throw Error('Connection lost during refresh');
};
save();''', '({status:$("save-status").textContent,dirty,saving})')
        self.assertTrue(result['status'].startswith('Decisions saved.'))
        self.assertFalse(result['dirty'])
        self.assertFalse(result['saving'])

    def test_selecting_master_does_not_make_other_candidates_redundant(self):
        self.app.save(self.payload(selected_candidate_id='full'))
        self.assertEqual([r['disposition'] for r in self.csv_rows()], ['selected_master', 'unresolved', 'unresolved'])

    def test_fixed_archive_save_retains_master_and_keeps_metadata_as_evidence(self):
        self.config['archive_masters_fixed'] = True
        self.obj.update(review_required=True, metadata_review_required=True)
        self.obj['candidates'][1].update(reference_object_id='unit#photo')
        self.obj['candidates'][2].update(reference_object_id='unit#other')
        self.write_input()
        result = self.app.save(self.payload(selected_candidate_id='full', same_pairs=[['full','crop']],
                                            note='Keep source metadata as evidence'))
        self.assertEqual(result['decision']['selected_candidate_id'], 'crop')
        self.assertEqual(result['decision']['redundant_candidate_ids'], ['full'])
        self.assertEqual(result['decision']['previous_master_ids'], ['full'])
        self.assertFalse(result['decision']['metadata_reviewed'])
        self.assertEqual(result['decision']['note'], 'Keep source metadata as evidence')
        self.assertEqual(result['selection_context']['action'], 'retain_archive_master')
        self.assertTrue(result['archive_resolved'])
        overview = self.app.snapshot()['objects'][0]
        self.assertFalse(overview['metadata_pending'])
        self.assertFalse(overview['review_pending'])
        self.assertEqual(self.csv_rows()[2]['disposition'], 'reference')

    def test_fixed_archive_unconfirmed_candidate_is_not_a_new_object(self):
        self.config['archive_masters_fixed'] = True
        self.obj['candidates'][1].update(reference_object_id='unit#photo')
        self.write_input()
        result = self.app.save(self.payload(selected_candidate_id='full'))
        self.assertEqual(result['selection_context']['action'], 'archive_identity_review')
        self.assertFalse(result['archive_resolved'])

    def discovery_policy(self):
        self.config.update(archive_masters_fixed=True, review_scope=wb.DISCOVERY_SCOPE)
        for c in self.obj['candidates'][1:]:
            c['reference_object_id'] = 'unit#md:' + ('1' if c['id'] == 'crop' else '2')
        self.obj.update(review_required=True, metadata_review_required=True)

    def test_discovery_excludes_known_md_assignment_without_inventing_copy_identity(self):
        self.discovery_policy()
        self.obj['matches'] = [{'a':'full','b':'other','engine':'sift','status':'not_supported','inliers':0}]
        self.obj['previous_proposals'] = [{'selected_path':self.obj['candidates'][0]['members'][0]['path'],
            'assigned_object_id':'unit#md:99','record':{'metadata_assignment_confirmed':True}}]
        self.write_input()
        snap = self.app.snapshot('photo')
        self.assertTrue(snap['scope']['closed'])
        self.assertTrue(snap['read_only'])
        self.assertFalse(snap['archive_resolved'])
        self.assertEqual(snap['decision']['same_pairs'], [])
        self.assertEqual(snap['selection_context']['action'], 'existing_md_object')
        self.assertEqual(self.app.disposition(snap['decision'], 'full'), 'existing_md_source')
        with self.assertRaisesRegex(ValueError, 'outside new-object discovery'):
            self.app.save(self.payload(selected_candidate_id='full'))

    def test_confirmed_historical_md_assignment_survives_missing_selected_master(self):
        self.discovery_policy()
        proposal = {'selected_path':self.obj['candidates'][0]['members'][0]['path'],
                    'assigned_object_id':'', 'destination_unit':'unit',
                    'record':{'action':'assign_source', 'object_record_key':'md:99',
                              'metadata_key':'md:99', 'master_key':'md:2',
                              'metadata_assignment_confirmed':True}}
        self.obj['previous_proposals'] = [proposal]
        self.write_input()
        snap = self.app.snapshot('photo')
        self.assertTrue(snap['scope']['closed'])
        self.assertEqual(snap['selection_context']['archive_object_id'], 'unit#md:99')
        self.assertEqual(snap['decision']['same_pairs'], [])
        self.assertFalse(snap['archive_resolved'])
        proposal['record']['action'] = 'new_object'
        self.write_input()
        self.assertFalse(self.app.snapshot('photo')['scope']['closed'])
        proposal['record']['action'] = 'assign_source'
        proposal['record']['metadata_key'] = 'md:98'
        self.write_input()
        self.assertFalse(self.app.snapshot('photo')['scope']['closed'])

    def test_failed_suggestions_do_not_give_unknown_source_an_md_identity(self):
        self.discovery_policy()
        self.obj['matches'] = [{'a':'full','b':'other','engine':'prior_similarity',
                               'evidence':{'passes_secondary_checks':False}},
                              {'a':'full','b':'other','engine':'sift','status':'not_supported','inliers':0}]
        self.write_input()
        snap = self.app.save(self.payload(selected_candidate_id='full'))
        self.assertFalse(snap['scope']['closed'])
        self.assertEqual(snap['scope']['active_reference_ids'], [])
        self.assertEqual(snap['selection_context']['action'], 'proposed_new_object')
        self.assertTrue(all(not c['active_comparison'] for c in snap['object']['candidates'][1:]))

    def test_supported_md_suggestion_still_needs_source_identity_confirmation(self):
        self.discovery_policy()
        self.obj['matches'] = [{'a':'full','b':'other','engine':'sift','status':'supported','inliers':30}]
        self.write_input()
        snap = self.app.save(self.payload(selected_candidate_id='full'))
        self.assertFalse(snap['scope']['closed'])
        self.assertEqual(snap['scope']['active_reference_ids'], ['other'])
        self.assertEqual(snap['selection_context']['action'], 'archive_identity_review')

    def test_discovery_allows_only_documented_jpeg_substitute_tiff_proposal(self):
        self.discovery_policy()
        jpeg = self.root / 'substitute.jpg'; Image.new('RGB',(80,60),'red').save(jpeg)
        c = self.obj['candidates'][1]
        c.update(source_record={'object_selection_basis':'curator_confirmed_unreadable_master_replacement'})
        c['members'] = [{'path':str(jpeg),'size_bytes':jpeg.stat().st_size,'mtime_ns':jpeg.stat().st_mtime_ns,'pages':1}]
        self.config['initial_decisions'] = {'photo':{'selected_candidate_id':'crop','same_pairs':[['full','crop']]}}
        self.write_input()
        before = jpeg.read_bytes()
        snap = self.app.snapshot('photo')
        self.assertTrue(snap['scope']['tiff_recovery_pending'])
        self.assertFalse(snap['archive_resolved'])
        snap = self.app.save(self.payload(selected_candidate_id='full'))
        self.assertEqual(snap['decision']['selected_candidate_id'], 'full')
        self.assertEqual(snap['selection_context']['action'], 'proposed_master_upgrade')
        self.assertEqual(self.app.disposition(snap['decision'],'crop',True), 'reference')
        self.assertFalse(self.app.snapshot()['objects'][0]['metadata_pending'])
        self.assertEqual(jpeg.read_bytes(), before)
        self.assertFalse(snap['decision']['deletion_authorized'])

    def test_known_archive_match_does_not_hide_a_separate_unknown_source(self):
        self.discovery_policy()
        del self.obj['candidates'][1]['reference_object_id']
        self.obj['previous_proposals'] = [{'selected_path':c['members'][0]['path'],
            'assigned_object_id':'unit#md:2','record':{'metadata_assignment_confirmed':i == 0}}
            for i,c in enumerate(self.obj['candidates'][:2])]
        self.config['initial_decisions'] = {'photo':{'selected_candidate_id':'other','same_pairs':[['full','other']]}}
        self.write_input()
        snap = self.app.snapshot('photo')
        self.assertFalse(snap['scope']['closed'])
        self.assertFalse(snap['archive_resolved'])

    def test_confirmed_transitive_group_is_saved_redundant_and_updates_overview(self):
        result = self.app.save(self.payload(selected_candidate_id='full',
                                            same_pairs=[['full', 'crop'], ['crop', 'other']],
                                            note='Human-confirmed same-photo group', metadata_reviewed=True))
        self.assertEqual(result['decision']['redundant_candidate_ids'], ['crop', 'other'])
        self.assertEqual([r['disposition'] for r in self.csv_rows()], ['selected_master', 'redundant', 'redundant'])
        self.assertTrue(all(r['retained_master_id'] == 'full' for r in self.csv_rows()))
        self.assertEqual(result['decision']['note'], 'Human-confirmed same-photo group')
        self.assertTrue(result['decision']['metadata_reviewed'])
        stored = [json.loads(line) for line in (self.root / wb.DECISIONS).read_text().splitlines()]
        self.assertEqual(stored[0]['redundant_candidate_ids'], ['crop', 'other'])
        reopened = wb.Workbench(self.root, 'second reviewer')
        self.assertEqual(reopened.snapshot()['objects'][0]['unresolved_count'], 0)
        self.assertTrue(reopened.snapshot()['projection_agrees'])

    def test_master_change_carries_confirmed_redundant_group_but_not_unrelated_leads(self):
        self.app.save(self.payload(selected_candidate_id='full', redundant_candidate_ids=['crop']))
        result = self.app.save(self.payload(selected_candidate_id='crop', redundant_candidate_ids=[]))
        self.assertEqual(result['decision']['redundant_candidate_ids'], ['full'])
        self.assertEqual([r['disposition'] for r in self.csv_rows()], ['redundant', 'selected_master', 'unresolved'])
        self.assertEqual(result['decision']['previous_master_ids'], ['full'])

    def test_transitive_same_photo_and_different_photo_conflict_is_rejected(self):
        with self.assertRaisesRegex(wb.ReviewError, 'different photos|Conflicting same-photo'):
            self.app.save(self.payload(selected_candidate_id='full',
                                       same_pairs=[['full', 'crop'], ['crop', 'other']],
                                       different_pairs=[['full', 'other']]))
        self.assertFalse((self.root / wb.DECISIONS).exists())

    def test_explicit_crop_redundancy_and_false_positive_survive_restart(self):
        self.app.save(self.payload(selected_candidate_id='full', redundant_candidate_ids=['crop'], different_pairs=[['full', 'other']]))
        reopened = wb.Workbench(self.root, 'second reviewer').snapshot('photo')
        self.assertEqual(reopened['object']['proposed_matches'], [])
        self.assertEqual(reopened['decision']['input_object']['matches'][0]['distance'], 1)
        self.assertEqual([r['disposition'] for r in self.csv_rows()], ['selected_master', 'redundant', 'different_photo'])
        self.assertEqual(self.csv_rows()[1]['retained_master_id'], 'full')
        self.assertTrue(all(r['deletion_authorized'] == 'false' for r in self.csv_rows()))

    def test_pair_can_be_rejected_before_selecting_master(self):
        result = self.app.save(self.payload(different_pairs=[['full', 'other']]))
        self.assertEqual(result['object']['proposed_matches'], [])
        self.assertTrue(all(r['disposition'] == 'unresolved' for r in self.csv_rows()))
        self.assertEqual(json.loads(self.csv_rows()[0]['different_from_ids']), ['other'])

    def test_pair_is_specific_not_a_global_disposition(self):
        self.app.save(self.payload(selected_candidate_id='crop', different_pairs=[['full', 'other']]))
        self.assertEqual([r['disposition'] for r in self.csv_rows()], ['unresolved', 'selected_master', 'unresolved'])

    def test_changed_input_no_longer_suppresses_a_rejected_match(self):
        self.app.save(self.payload(different_pairs=[['full', 'other']]))
        with (self.root / 'other.tiff').open('ab') as f:
            f.write(b'changed')
        snapshot = self.app.snapshot('photo')
        self.assertEqual(len(snapshot['object']['proposed_matches']), 1)
        self.assertTrue(snapshot['object']['candidates'][2]['problems'])
        with self.assertRaisesRegex(wb.ReviewError, 'changed'):
            self.app.save(self.payload())

    def test_former_master_returns_to_ordinary_unresolved_candidate(self):
        self.app.save(self.payload(selected_candidate_id='full', redundant_candidate_ids=['crop']))
        self.app.save(self.payload(selected_candidate_id='crop', redundant_candidate_ids=[]))
        self.assertEqual(self.csv_rows()[0]['disposition'], 'redundant')
        self.assertEqual(self.app.snapshot('photo')['decision']['previous_master_ids'], ['full'])

    def test_redundancy_requires_master_and_consistent_identity(self):
        invalid = [dict(redundant_candidate_ids=['crop']),
                   dict(selected_candidate_id='full', redundant_candidate_ids=['full']),
                   dict(selected_candidate_id='full', redundant_candidate_ids=['crop'], different_pairs=[['full', 'crop']]),
                   dict(selected_candidate_id='full', redundant_candidate_ids=['crop', 'other'], different_pairs=[['crop', 'other']]),
                   dict(different_pairs=[['full', 'other'], ['other', 'full']])]
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(wb.ReviewError):
                self.app.save(self.payload(**update))
        self.assertFalse((self.root / wb.DECISIONS).exists())

    def test_incomplete_master_and_cross_object_source_claim_are_rejected(self):
        self.obj['candidates'][0]['complete'] = False
        self.write_input()
        with self.assertRaisesRegex(wb.ReviewError, 'complete'):
            self.app.save(self.payload(selected_candidate_id='full'))
        self.obj['candidates'][0]['complete'] = True
        second = deepcopy(self.obj)
        second['id'] = 'second'
        self.config['objects'].append(second)
        self.write_input()
        self.app.save(self.payload(selected_candidate_id='full'))
        request = self.payload(selected_candidate_id='full', object_id='second')
        with self.assertRaisesRegex(wb.ReviewError, 'another object'):
            self.app.save(request)

    def test_metadata_and_provenance_are_preserved_without_transfer(self):
        self.obj['metadata'] = {'date': {'value': 'unknown', 'source': 'Original catalogue'}}
        self.obj['candidates'][2]['metadata'] = {'title': 'A different photograph'}
        self.obj['source_record'] = {'comparison': 'warning', 'original_key': 'unchanged'}
        self.write_input()
        result = self.app.save(self.payload(selected_candidate_id='other'))
        self.assertEqual(result['decision']['input_object'], self.obj)
        self.assertNotIn('title', result['object']['metadata'])

    def test_missing_unresolved_file_does_not_block_notes(self):
        (self.root / 'other.tiff').unlink()
        result = self.app.save(self.payload(selected_candidate_id='full', note='Other source unavailable; leave unresolved'))
        self.assertEqual(result['decision']['note'], 'Other source unavailable; leave unresolved')
        with self.assertRaises(FileNotFoundError):
            self.app.save(self.payload(different_pairs=[['full', 'other']]))

    def test_stale_browser_and_manifest_changes_cannot_overwrite_decisions(self):
        request = self.payload(selected_candidate_id='full')
        self.app.save(self.payload(note='first save'))
        before = (self.root / wb.DECISIONS).read_bytes()
        with self.assertRaisesRegex(wb.ReviewError, 'reload'):
            self.app.save(request)
        self.assertEqual((self.root / wb.DECISIONS).read_bytes(), before)
        self.obj['metadata'] = {'title': 'changed evidence'}
        self.write_input()
        with self.assertRaisesRegex(wb.ReviewError, 'evidence changed'):
            self.app.snapshot()

    def test_unreadable_or_incomplete_authority_is_not_silently_discarded(self):
        for content in ('', '{broken}\n', '{}\n', 'null\n'):
            with self.subTest(content=content):
                (self.root / wb.DECISIONS).write_text(content)
                with self.assertRaises((wb.ReviewError, json.JSONDecodeError)):
                    self.app.snapshot()

    def test_csv_rebuild_never_depends_on_old_csv(self):
        self.app.save(self.payload(selected_candidate_id='full'))
        expected = (self.root / wb.CSV).read_bytes()
        for corrupt in (None, b'broken,old,schema\n'):
            if corrupt is None:
                (self.root / wb.CSV).unlink()
            else:
                (self.root / wb.CSV).write_bytes(corrupt)
            app = wb.Workbench(self.root, 'repair')
            self.assertFalse(app.snapshot()['projection_agrees'])
            app.rebuild_csv()
            self.assertEqual((self.root / wb.CSV).read_bytes(), expected)

    def test_projection_failure_reports_successful_authority_save(self):
        original = wb.atomic_write
        def fail_csv(path, data):
            if path.name == wb.CSV:
                raise OSError('synthetic disk error')
            return original(path, data)
        with patch.object(wb, 'atomic_write', side_effect=fail_csv):
            result = self.app.save(self.payload(selected_candidate_id='full'))
        self.assertIn('Decisions saved', result['warning'])
        self.assertEqual(wb.Workbench(self.root, 'reopen').snapshot('photo')['decision']['selected_candidate_id'], 'full')

    def test_completion_receipts_freeze_edits_even_when_malformed(self):
        for name in wb.RECEIPTS:
            with self.subTest(receipt=name):
                receipt = self.root / name
                receipt.write_text('malformed receipt still freezes')
                self.assertTrue(self.app.snapshot()['read_only'])
                with self.assertRaisesRegex(wb.ReviewError, 'read-only'):
                    self.app.save(self.payload())
                with self.assertRaises(wb.ReviewError):
                    self.app.rebuild_csv()
                receipt.unlink()

    def test_completion_during_source_inspection_prevents_save(self):
        request = self.payload(selected_candidate_id='full')
        original = wb.checked_member
        def complete(member, roots):
            result = original(member, roots)
            (self.root / 'accession-completion.json').write_text('{}')
            return result
        with patch.object(wb, 'checked_member', side_effect=complete), self.assertRaisesRegex(wb.ReviewError, 'read-only'):
            self.app.save(request)
        self.assertFalse((self.root / wb.DECISIONS).exists())

    def test_protected_and_symlink_roots_cannot_receive_demo_files(self):
        for root in common.PROTECTED:
            with self.subTest(root=root), patch.object(Path, 'mkdir') as mkdir:
                with self.assertRaises(wb.ReviewError):
                    wb.create_demo(root / 'synthetic-forbidden')
                mkdir.assert_not_called()
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(wb.ReviewError):
            wb.create_demo(link / 'nested')

    def test_output_symlink_and_source_scope_are_rejected(self):
        target = self.root / 'full.tiff'
        before = target.read_bytes()
        (self.root / wb.CSV).symlink_to(target)
        with self.assertRaises(wb.ReviewError):
            self.app.save(self.payload(selected_candidate_id='full'))
        self.assertEqual(target.read_bytes(), before)
        with self.assertRaises(wb.ReviewError):
            common.source_path('/etc/passwd', [self.root])

    def test_source_symlink_is_not_followed(self):
        path = self.root / 'other.tiff'
        path.unlink()
        path.symlink_to(self.root / 'full.tiff')
        self.assertTrue(self.app.snapshot('photo')['object']['candidates'][2]['problems'])
        with self.assertRaises(wb.ReviewError):
            self.app.media('photo', 'other', 0, 1, 1200)

    def test_elements_mount_must_be_exact_and_read_only(self):
        mount = {'source': '/dev/sdf1', 'target': str(common.ELEMENTS), 'fstype': 'ntfs3', 'options': 'ro,nosuid'}
        def result(m):
            return subprocess.CompletedProcess([], 0, json.dumps({'filesystems': [m]}), '')
        with patch.object(common.subprocess, 'run', return_value=result(mount)):
            common.elements_guard(common.ELEMENTS / 'source.tif')
        for update in ({'options': 'rw'}, {'source': '/dev/sdg1'}, {'target': '/media/vladimir/Elements1'}, {'fstype': 'fuseblk'}):
            with self.subTest(update=update), patch.object(common.subprocess, 'run', return_value=result({**mount, **update})):
                with self.assertRaises(wb.ReviewError):
                    common.elements_guard(common.ELEMENTS / 'source.tif')
        with self.assertRaises(wb.ReviewError):
            common.elements_guard(Path('/media/vladimir/Elements1/source.tif'))

    def test_competing_process_views_cannot_both_save(self):
        other = wb.Workbench(self.root, 'second')
        request = self.payload(note='concurrent')
        barrier = threading.Barrier(2)
        outcomes = []
        def save(app):
            barrier.wait()
            try:
                app.save(request)
                outcomes.append('saved')
            except wb.ReviewError:
                outcomes.append('stale')
        workers = [threading.Thread(target=save, args=(app,)) for app in (self.app, other)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(outcomes, ['saved', 'stale'])

    def test_check_command_writes_nothing_and_reports_missing_sources(self):
        before = sorted(p.name for p in self.root.iterdir())
        command = ['python3', '-B', str(wb.HERE / 'workbench.py'), '--work-root', str(self.root), '--check']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        (self.root / 'other.tiff').unlink()
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('unavailable_or_stale_sources', result.stdout)

    def test_preview_reads_requested_page_and_leaves_sources_unchanged(self):
        before = hashlib.sha256((self.root / 'full.tiff').read_bytes()).hexdigest()
        data, mime = self.app.media('photo', 'full', 0, 1, 1200)
        self.assertEqual(mime, 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(data)).size, (80, 60))
        self.assertEqual(hashlib.sha256((self.root / 'full.tiff').read_bytes()).hexdigest(), before)
        for args in [('photo', 'full', -1, 1, 1200), ('photo', 'full', 0, 2, 1200), ('photo', 'full', 0, 1, 999999)]:
            with self.assertRaises(wb.ReviewError):
                self.app.media(*args)

    def test_demo_has_working_tiff_pdf_and_jpeg_previews(self):
        root = self.root / 'demo'
        wb.create_demo(root)
        app = wb.Workbench(root, 'demo')
        for oid, cid, page in [('demo-photo', 'crop', 1), ('demo-pages', 'pages', 2), ('demo-pdf', 'pdf', 2)]:
            with self.subTest(oid=oid):
                data, mime = app.media(oid, cid, 0, page, 1200)
                self.assertTrue(mime.startswith('image/'))
                self.assertTrue(Image.open(io.BytesIO(data)).width > 0)
        with self.assertRaises(wb.ReviewError):
            wb.create_demo(root)

    def test_http_saves_require_local_host_origin_and_token(self):
        server = wb.Server(('127.0.0.1', 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host = f'127.0.0.1:{server.server_port}'
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/overview')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        token = json.loads(response.read())['token']
        self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
        payload = json.dumps(self.payload(selected_candidate_id='full'))
        headers = {'Content-Type': 'application/json', 'Origin': 'https://unrelated.invalid', 'X-Review-Token': token}
        connection.request('POST', '/api/decision', payload, headers)
        response = connection.getresponse(); response.read()
        self.assertEqual(response.status, 403)
        self.assertFalse((self.root / wb.DECISIONS).exists())
        headers['Origin'] = 'http://' + host
        connection.request('POST', '/api/decision', payload, headers)
        response = connection.getresponse(); response.read()
        self.assertEqual(response.status, 200)
        metadata_payload = json.dumps(self.metadata_payload())
        headers['Origin'] = 'https://unrelated.invalid'
        connection.request('POST', '/api/metadata', metadata_payload, headers)
        response = connection.getresponse(); response.read()
        self.assertEqual(response.status, 403)
        headers['Origin'] = 'http://' + host
        connection.request('POST', '/api/metadata', metadata_payload, headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())['decision']['metadata_proposal']['fields']['title'], 'Edited title')
        connection.request('GET', '/api/overview', headers={'Host': 'unrelated.invalid'})
        response = connection.getresponse(); response.read()
        self.assertEqual(response.status, 403)


if __name__ == '__main__':
    unittest.main()
