"""Meaningful decision-boundary tests; all saved decisions use in-memory SQLite."""
import copy
import io
import json
import sqlite3
import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import retained_candidates as r
import paragon_workbench as app


class RetainedReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = sqlite3.connect(f'file:{r.ROOT}/work/catalog.sqlite?mode=ro', uri=True)
        cls.con.row_factory = sqlite3.Row
        cls.cases = r.cases(cls.con)
        cls.by_key = {c['key']: c for c in cls.cases}
        cls.accepted_members = cls.con.execute("SELECT count(*) FROM entity_relations WHERE predicate='component_of' AND status='accepted'").fetchone()[0]
        # Decision fixtures are pre-accession choices; completion behavior is
        # covered separately with an explicit receipt fixture below.
        cls.live_evidence = r.evidence()
        cls.evidence_patch = patch.object(r, 'evidence', return_value=dict(cls.live_evidence, accessioned={}, section_accessions={}, drain_registered=set(), confirmed_sources={}))
        cls.evidence_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.evidence_patch.stop()
        cls.con.close()

    def payload(self, key='VLV-1043', action='add'):
        obj = self.by_key[key]
        return dict(action=action, selected_master_path=obj['sources'][0]['path'],
                    metadata=obj['proposed_metadata'], metadata_reviewed=True,
                    same_object_confirmed=True, relation='standalone', parent='',
                    file_choices={}, retention={}, coverage_confirmed=False, note='')

    def build(self, key='VLV-1043', **changes):
        return r.build_review(self.by_key[key], self.payload(key) | changes, 'test fixture')

    def memory_db(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        for name in ['logbook', 'units']:
            sql = self.con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
            db.execute(sql)
        unit = self.con.execute("SELECT * FROM units WHERE unit_id='WF-PARAGON'").fetchone()
        db.execute('INSERT INTO units VALUES (?,?,?,?,?,?)', tuple(unit))
        return db

    def test_every_retained_path_occurs_once(self):
        paths = [s['path'] for c in self.cases for s in c['sources']]
        self.assertEqual(len(paths), 1132)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(sum(c['kind'] == 'carrier' for c in self.cases), 32)
        self.assertEqual(sum(len(c['members']) for c in self.cases), self.accepted_members)

    def test_pending_view_contains_exact_excluded_sources_and_preserves_selections(self):
        pending = {f['source_path']: f for f in r.readjson(r.PENDING_ACCESSION)['files']}
        before = r.readable_decisions(self.con)
        result = r.index(self.con)
        pictures = [p for p in result['pictures'] if p['pending_accession']]
        self.assertEqual({p['path'] for p in pictures}, set(pending))
        self.assertEqual(result['pending_count'], 143)
        self.assertEqual(result['pending_selected_count'], 42)
        for p in pictures:
            self.assertEqual(p['pending_accession']['source_sha256'], pending[p['path']]['source_sha256'])
            if p['state'] == 'selected_master':
                self.assertTrue(p['read_only'])
        self.assertEqual(r.readable_decisions(self.con), before)

    def test_pending_catalogue_context_does_not_become_similarity_evidence(self):
        row = next(f for f in r.readjson(r.PENDING_ACCESSION)['files']
                   if f['saved_state'] == 'selected_master' and f['existing_catalogue_association'] == 'VLV-1061')
        obj = r.get_case(self.con, row['case_key'])
        pending = obj['pending_sources'][row['source_path']]
        self.assertEqual(pending['catalogue_context']['inventory'], 'VLV-1061')
        self.assertFalse(obj['source_comparisons'].get(row['source_path'], {}).get('reference'))
        before = copy.deepcopy(obj['decision'])
        with self.assertRaises(ValueError):
            r.build_picture_review(obj, dict(source_path=row['source_path'], file_state='redundant',
                comparison_path=pending['catalogue_context']['path']), 'test fixture')
        self.assertEqual(obj['decision'], before)

    def test_registered_invitation_preserves_existing_member_comparison(self):
        obj = r.get_case(self.con, 'VLV-1095-1')
        self.assertFalse(obj['pending_sources'])
        hint = obj['source_comparisons'][obj['sources'][0]['path']]
        self.assertEqual(hint['reference']['inventory'], 'VLV-1095')
        self.assertEqual(hint['reference']['part'], 'open_interior')

    def test_confirmed_accession_is_bound_to_exact_source_and_current_master(self):
        with patch.object(r, 'evidence', return_value=self.live_evidence):
            result = r.index(self.con)
            selected = self.live_evidence['confirmed_sources']
            self.assertEqual(len(selected), 28)
            for picture in result['pictures']:
                if picture['path'] not in selected:
                    continue
                item = selected[picture['path']]
                self.assertTrue(picture['registered'] and picture['read_only'])
                self.assertFalse(picture['pending_accession'] or picture['remaining'])
                self.assertEqual(picture['comparison']['finding'], 'accessioned_source')
                self.assertEqual(picture['comparison']['reference']['path'], str(r.ARCHIVE / item['access_path']))
            obj = copy.deepcopy(r.get_case(self.con, 'VLV-1131'))
            source = next(s for s in obj['sources'] if s['path'] in selected)
            source['mtime_ns'] -= 1
            self.assertIsNone(r.source_comparisons(obj)[source['path']]['reference'])

    def test_documented_invitation_match_supports_redundancy_with_durable_evidence(self):
        obj = r.get_case(self.con, 'VLV-1095-1')
        source = obj['sources'][0]
        hint = obj['source_comparisons'][source['path']]
        self.assertEqual(hint['finding'], 'documented_member')
        self.assertEqual(hint['reference']['part'], 'open_interior')
        self.assertEqual(hint['master_dimensions'], [3478, 1210])
        self.assertEqual(hint['master_format'], 'TIFF')
        before = copy.deepcopy(obj['decision'])
        review = r.build_picture_review(obj, dict(source_path=source['path'],
            file_state='redundant', comparison_path=hint['reference']['path'],
            note='Same invitation interior; keep the accessioned TIFF.'), 'test fixture')
        self.assertEqual(review['files'][0]['state'], 'redundant')
        self.assertEqual(review['files'][0]['catalogue_evidence']['part'], 'open_interior')
        self.assertEqual(review['proposed_metadata'], before['proposed_metadata'])
        self.assertEqual(review['relationship'], before['relationship'])
        self.assertEqual(review['selected_master_path'], before['selected_master_path'])
        self.assertEqual(obj['decision'], before)
        with self.memory_db() as db:
            r.record_review(db, review)
            self.assertEqual(r.readable_decisions(db)['reviews'][0], review)
            proposal = r.drain_proposals(db)['files'][0]
            self.assertEqual(proposal['catalogue_evidence'], review['files'][0]['catalogue_evidence'])
            self.assertFalse(proposal['deletion_authorized'])

    def test_documented_invitation_match_rejects_changed_files_and_member_binding(self):
        original = self.by_key['VLV-1095-1']
        path = original['sources'][0]['path']
        for field in ['sha256', 'mtime_ns']:
            obj = copy.deepcopy(original)
            obj['sources'][0][field] = 'changed'
            self.assertIsNone(r.source_comparisons(obj)[path]['reference'])
        details = r.archive_master_details(r.archived_views('VLV-1095')[1])
        for field in ['master_path', 'master_mtime_ns', 'master_dimensions']:
            with patch.object(r, 'archive_master_details', return_value=details | {field:'changed'}):
                self.assertIsNone(r.source_comparisons(original)[path]['reference'])
        verify = r.verify_asset
        def changed_reference(asset):
            if asset['source'] == 'archive':
                raise ValueError('Changed archive image')
            return verify(asset)
        with patch.object(r, 'verify_asset', side_effect=changed_reference):
            self.assertIsNone(r.source_comparisons(original)[path]['reference'])

    def test_catalogue_refresh_reuses_only_unchanged_comparison_and_master_binding(self):
        obj = self.by_key['VLV-1129']
        source = obj['sources'][0]['path']
        rows, cached, _ = r.comparison_evidence()
        with patch.object(r, 'comparison_evidence', return_value=(rows, cached, {})):
            hint = r.source_comparisons(obj)[source]
            self.assertEqual(hint['reference']['inventory'], 'VLV-1150')
            stale = copy.deepcopy(cached)
            stale[hint['reference']['path']]['mtime_ns'] -= 1
            with patch.object(r, 'comparison_evidence', return_value=(rows, stale, {})):
                self.assertEqual(r.source_comparisons(obj)[source], dict(finding='stale', reference=None))
            ev = copy.deepcopy(r.evidence())
            ev['catalogue']['VLV-1150']['master_file'] = 'masters/a-different-master.tif'
            with patch.object(r, 'evidence', return_value=ev):
                self.assertEqual(r.source_comparisons(obj)[source], dict(finding='stale', reference=None))

    def test_carrier_context_exposes_archived_members_without_claiming_full_scan_coverage(self):
        before = r.saved_reviews(self.con)['VLV-1131']
        obj = r.get_case(self.con, 'VLV-1131')
        source = next(s for s in obj['sources'] if s['path'].endswith('/Rohscans 01/VLV-1131-2.tif'))
        views = obj['carrier_context']
        self.assertEqual(views[0]['inventory'], 'VLV-1131')
        self.assertGreater(min(views[0]['master_dimensions']), 2500)
        self.assertEqual({v['inventory'] for v in views if v['inventory'] != 'VLV-1131'}, {m['inventory'] for m in obj['members']})
        member = next(v for v in views if v['inventory'] == 'VLV-1131-2')
        self.assertEqual(member['master_dimensions'], [3703, 2722])
        self.assertEqual([source['width'], source['height']], [5024, 6992])
        self.assertIsNone(obj['source_comparisons'][source['path']]['reference'])
        for comparison in ['', member['path'], views[0]['path']]:
            with self.assertRaises(ValueError):
                r.build_picture_review(obj, dict(source_path=source['path'],
                    file_state='redundant', comparison_path=comparison), 'test fixture')
        self.assertEqual(obj['decision'], before)

    def test_filename_mismatch_supplies_comparison_without_reassigning_object(self):
        obj = r.get_case(self.con, 'VLV-1129')
        source = obj['sources'][0]
        hint = obj['source_comparisons'][source['path']]
        self.assertEqual(hint['reference']['inventory'], 'VLV-1150')
        self.assertEqual(hint['filename_label'], 'VLV-1129')
        self.assertTrue(hint['filename_mismatch'])
        self.assertEqual(obj['label'], 'VLV-1129')
        self.assertEqual(obj['references'][0]['inventory'], 'VLV-1129')
        self.assertEqual(obj['sources'], self.by_key['VLV-1129']['sources'])
        self.assertEqual(obj['proposed_metadata'], self.by_key['VLV-1129']['proposed_metadata'])
        self.assertEqual(obj['decision'], r.saved_reviews(self.con).get('VLV-1129'))
        with self.assertRaises(ValueError):
            r.build_review(obj, self.payload('VLV-1129', 'retain') | {
                'file_choices': {hint['reference']['path']: {'state': 'redundant'}}}, 'test fixture')

    def test_similarity_comparisons_follow_exact_source_and_warn_about_surrogates(self):
        obj = r.get_case(self.con, 'VLV-1132')
        for number in (6, 7):
            source = str(r.SOURCE / f'Paragon/Wandzeitung/Rohscans 01/VLV-1132-{number}.tif')
            hint = obj['source_comparisons'][source]
            self.assertEqual(hint['reference']['inventory'], f'VLV-1132-{number}')
            self.assertEqual(hint['finding'], 'close')
            self.assertFalse(hint['filename_mismatch'])
        source = str(r.SOURCE / 'Paragon/nur für Micha interessant/Zugeordnete Objekte/VLV-1107.tif')
        case = next(c for c in self.cases if source in {s['path'] for s in c['sources']})
        self.assertTrue(r.source_comparisons(case)[source]['manufactured_surrogate'])

    def test_changed_reference_is_not_offered_as_current_similarity(self):
        obj = self.by_key['VLV-1129']
        original = r.verify_asset
        def changed(asset):
            if asset['source'] == 'archive':
                raise ValueError('File changed')
            return original(asset)
        with patch.object(r, 'verify_asset', side_effect=changed):
            hint = r.source_comparisons(obj)[obj['sources'][0]['path']]
        self.assertEqual(hint, dict(finding='stale', reference=None))

    def test_picture_browser_comparison_and_forward_only_navigation(self):
        html = (r.ROOT / 'retained-workbench.html').read_text()
        self.assertNotIn('id="status"', html)
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        script = script[:script.rindex('(async()=>')]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let source='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>source+=s);
process.stdin.on('end',async()=>{
 const elements=new Map();
 const element=s=>{if(!elements.has(s))elements.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,classList:{toggle(){}}});return elements.get(s);};
 const context=vm.createContext({document:{querySelector:element,querySelectorAll:()=>[]},window:{addEventListener(){}},history:{replaceState(){}},location:{pathname:'/retained/',search:''},confirm:()=>true,console});
 vm.runInContext(source,context);
 const read=code=>vm.runInContext(code,context);
 read(`$('#match').value='all';
 indexData={token:'fixture',pictures:[
 {path:'a',name:'wrong.jpg',case_key:'wrong',remaining:true,state:'unresolved',decoded:true,comparison:{finding:'close',filename_mismatch:true,filename_label:'wrong',reference:{path:'archive-a',inventory:'correct'}}},
 {path:'b',name:'whole-carrier.tif',case_key:'IMG',remaining:true,state:'unresolved',decoded:true,high_resolution:true,comparison:{finding:'near',reference:{path:'archive-b',inventory:'VLV-1132'}}},
 {path:'c',name:'new.tif',case_key:'new',remaining:true,state:'unresolved',decoded:true,high_resolution:true,comparison:{finding:'no_close_hash_match',reference:null}},
 {path:'d',name:'retained.jpg',case_key:'retained',remaining:false,state:'retain',pending_accession:{saved_state:'retain'}},
 {path:'e',name:'selected.tif',case_key:'selected',remaining:false,state:'selected_master',read_only:true,pending_accession:{saved_state:'selected_master'}},
 {path:'f',name:'redundant.jpg',case_key:'redundant',remaining:false,state:'redundant'},
 {path:'g',name:'section.tif',case_key:'section',remaining:false,state:'carrier_section',registered:true,read_only:true}]};
 globalThis.savedBefore=JSON.stringify(indexData.pictures.slice(3));
 api=async(url,options)=>{if(options)throw Error('Unexpected write');
 const key=decodeURIComponent(url.split('key=')[1]),p=indexData.pictures.find(p=>p.case_key===key);
 return {key,decision:{reviewed_at:'fixture-time',files:[{path:p.path,state:p.state,note:'Keep saved evidence'}]},source_comparisons:{[p.path]:p.comparison}};};
 renderList();`);
 assert.equal(read("filtered.map(p=>p.path).join(',')"),'a,b,c');
 assert.match(read("$('#counts').textContent"),/3 undecided/);
 assert.doesNotMatch(read("$('#pictureList').innerHTML"),/retained.jpg|selected.tif|redundant.jpg|section.tif/);
 read("$('#search').value='retained.jpg';renderList()");assert.equal(read('filtered.length'),0);
 read("$('#search').value='';$('#match').value='new';renderList()");assert.equal(read("filtered.map(p=>p.path).join(',')"),'c');
 read("$('#match').value='all';renderList()");
 assert.equal(read("resolveLink('VLV-1132').path"),'b');
 assert.equal(read("resolveLink('d').path"),'d');
 // Old exact-file links and saved case links cannot reopen a decision form.
 for(const path of ['d','e','f','g']){
  await read(`openPicture('${path}',true)`);
  assert.match(read("$('#mainInner').innerHTML"),/Decision already saved/);
  assert.doesNotMatch(read("$('#mainInner').innerHTML"),/<img|name="choice"|id="save"/);
  assert.equal(read('currentPath'),'');
  assert.equal(read("filtered.map(p=>p.path).join(',')"),'a,b,c');
 }
 await read("openPicture('a',true)");
 assert.match(read("$('#mainInner').innerHTML"),/archive-a/);
 assert.match(read("$('#mainInner').innerHTML"),/filename says wrong/);
 assert.equal(read('dirty'),false);
 // Unchanged unresolved choice is not rewritten.
 read("$('input[name=choice]:checked').value='unresolved';$('#note').value='';controls()");
 assert.equal(read("$('#save').disabled"),true);
 await read('save(false)');
 assert.equal(read('current.decision.files[0].note'),'Keep saved evidence');
 // A successful Save removes the picture even without Save and next.
 read(`globalThis.readApi=api;globalThis.writes=[];
 api=async(url,options)=>{if(options){const sent=JSON.parse(options.body);writes.push(sent);
 const p=indexData.pictures.find(p=>p.path===sent.source_path);p.state=sent.file_state;p.remaining=p.state==='unresolved';return {ok:true};}
 if(url==='/retained/api/cases')return indexData;return readApi(url);};
 $('input[name=choice]:checked').value='retain';changed();`);
 await read('save(false)');
 assert.equal(read('writes.length'),1);
 assert.equal(read('writes[0].source_path'),'a');
 assert.equal(read('writes[0].comparison_path'),'archive-a');
 assert.equal(read("filtered.map(p=>p.path).join(',')"),'b,c');
 assert.equal(read('currentPath'),'');
 assert.match(read("$('#mainInner').innerHTML"),/Choice saved/);
 assert.doesNotMatch(read("$('#mainInner').innerHTML"),/<img|name="choice"/);
 await read("openPicture('b',true)");
 read("$('input[name=choice]:checked').value='redundant';changed()");
 await read('save(true)');
 assert.equal(read('currentPath'),'c');
 assert.equal(read("filtered.map(p=>p.path).join(',')"),'c');
 assert.match(read("$('#mainInner').innerHTML"),/No whole-image match found/);
 assert.doesNotMatch(read("$('#mainInner').innerHTML"),/archive-a|archive-b/);
 assert.equal(read("forwardPath(['a','b','c'],'c')"),undefined);
 // Context switching must preserve the current source and unsaved note.
 read(`current.label='VLV-1131';current.carrier_context=[
 {path:'board',inventory:'VLV-1131',view_label:'Whole carrier',decoded:true,master_dimensions:[4000,3000],master_format:'TIFF'},
 {path:'photo',inventory:'VLV-1131-2',view_label:'Photograph',decoded:true,master_dimensions:[3703,2722],master_format:'TIFF'}];
 renderPicture();$('#note').value='preserve board area';$('input[name=choice]:checked').value='retain';dirty=true;
 $('#archiveView').value='photo';showCarrierView();`);
 assert.equal(read("$('#archiveHeading').textContent"),'Already archived · VLV-1131-2');
 assert.equal(read("$('#note').value"),'preserve board area');
 assert.equal(read('currentPath'),'c');assert.equal(read('dirty'),true);
 // A failed save keeps the undecided source and draft intact.
 read("api=async()=>{throw Error('This review changed in another tab');}");
 await read('save(true)');
 assert.equal(read('currentPath'),'c');assert.equal(read('dirty'),true);assert.equal(read('saving'),false);
 assert.match(read("$('#message').textContent"),/another tab/);
 assert.equal(read("filtered.map(p=>p.path).join(',')"),'c');
 // A decision saved in another tab between index/detail reads also stays hidden.
 read("dirty=false;api=async()=>({key:'new',decision:{files:[{path:'c',state:'retain'}]},source_comparisons:{}})");
 await read("openPicture('c',true)");
 assert.equal(read('filtered.length'),0);
 assert.match(read("$('#mainInner').innerHTML"),/Decision already saved/);
 assert.match(read("$('#mainInner').innerHTML"),/All picture decisions are recorded/);
 assert.doesNotMatch(read("$('#mainInner').innerHTML"),/<img|name="choice"|continueReview/);
 assert.equal(read('JSON.stringify(indexData.pictures.slice(3))'),read('savedBefore'));
 console.log('Undecided-only filtering, old links, Save removal, forward navigation, stale-index protection and draft preservation passed');
});
'''
        result = subprocess.run(['node', '-e', harness], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pending_evidence_view_keeps_saved_choices_read_only(self):
        html = (r.ROOT / 'retained-workbench.html').read_text()
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        script = script[:script.rindex('(async()=>')]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let source='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>source+=s);
process.stdin.on('end',async()=>{
 const elements=new Map();
 const element=s=>{if(!elements.has(s))elements.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,classList:{toggle(){}}});return elements.get(s);};
 const context=vm.createContext({document:{querySelector:element,querySelectorAll:()=>[]},window:{addEventListener(){}},history:{replaceState(){}},location:{pathname:'/retained/',search:'?view=pending'},confirm:()=>true,console});
 vm.runInContext(source,context);const run=code=>vm.runInContext(code,context);
 run(`$('#match').value='all';$('#question').value='all';
 indexData={catalogue:[],pending_count:2,pictures:[
 {path:'selected',name:'selected.tif',case_key:'one',remaining:false,state:'selected_master',read_only:true,decoded:true,pending_accession:{review_note:'Confirm the physical leaf'}},
 {path:'retained',name:'retained.tif',case_key:'two',remaining:false,state:'retain',decoded:true,pending_accession:{review_note:'Confirm ownership'}},
 {path:'complete',name:'complete.tif',case_key:'done',remaining:false,state:'selected_master',registered:true},
 {path:'excluded',name:'book.tif',case_key:'excluded',remaining:false,state:'retain'},
 {path:'undecided',name:'new.tif',case_key:'new',remaining:true,state:'unresolved'}]};
 auditData={pending_question_groups:[{question:'Leaf',paths:['selected']},{question:'Ownership',paths:['retained']}]};
 globalThis.before=JSON.stringify(indexData.pictures);globalThis.writes=0;
 api=async(url,options)=>{if(options){writes++;throw Error('Unexpected write');}
 const key=decodeURIComponent(url.split('key=')[1]),p=indexData.pictures.find(p=>p.case_key===key);
 return {key,decision:{files:[{path:p.path,state:p.state,note:'Earlier saved note'}]},pending_sources:{[p.path]:p.pending_accession},source_comparisons:{},carrier_context:[]};};
 renderList();`);
 assert.equal(run("filtered.map(p=>p.path).join(',')"),'selected,retained');
 assert.match(run("$('#counts').textContent"),/2 pending/);
 await run("openPicture('selected',true)");
 assert.match(run("$('#mainInner').innerHTML"),/Confirm the physical leaf/);
 assert.match(run("$('#mainInner').innerHTML"),/selected.tif/);
 assert.doesNotMatch(run("$('#mainInner').innerHTML"),/name="choice"|id="save"|id="saveNext"/);
 assert.equal(run('currentPicture.read_only'),true);
 await run('save(false)');assert.equal(run('writes'),0);
 await run('step(1)');assert.equal(run('currentPath'),'retained');
 await run('step(1)');assert.equal(run('currentPath'),'');
 assert.match(run("$('#mainInner').innerHTML"),/End of this evidence view/);
 run("$('#question').value='Leaf';renderList()");
 assert.equal(run("filtered.map(p=>p.path).join(',')"),'selected');
 assert.equal(run('JSON.stringify(indexData.pictures)'),run('before'));
 // Recheck membership before displaying a stale link. Completed sources stay out.
 run('api=async()=>({pending_sources:{}})');
 await run("openPicture('selected',true)");
 assert.equal(run('filtered.length'),0);
 assert.match(run("$('#mainInner').innerHTML"),/no longer pending/);
 assert.doesNotMatch(run("$('#mainInner').innerHTML"),/<img/);
 console.log('Pending membership, grouping, read-only saved choices, navigation and concurrent completion passed');
});
'''
        result = subprocess.run(['node', '-e', harness], input=script, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def picture_fixture(self, key='VLV-1132', suffix='/VLV-1132-6.tif'):
        obj = r.get_case(self.con, key)
        obj['decision'] = None
        obj['registered_drain_paths'] = []
        source = next(s for s in obj['sources'] if s['path'].endswith(suffix))
        hint = obj['source_comparisons'].get(source['path'], {})
        payload = dict(source_path=source['path'], file_state='redundant',
                       comparison_path=(hint.get('reference') or {}).get('path', ''), note='Same full photograph')
        return obj, source, payload

    def test_picture_review_changes_only_exact_source_preserving_metadata_and_evidence(self):
        obj, source, payload = self.picture_fixture()
        previous = r.build_review(obj, self.payload('VLV-1132', 'retain'), 'earlier reviewer')
        previous['proposed_metadata']['unknown_field'] = ''
        previous['prior_evidence'] = {'keep': True}
        for f in previous['files']:
            f['prior_file_evidence'] = {'keep': f['path']}
        obj['decision'] = copy.deepcopy(previous)
        result = r.build_picture_review(obj, payload, 'test fixture')
        before = {f['path']: f for f in previous['files']}
        for f in result['files']:
            if f['path'] != source['path']:
                self.assertEqual(f, before[f['path']])
        for field in previous:
            if field not in {'files', 'actor', 'reviewed_at'}:
                self.assertEqual(result[field], previous[field])
        target = next(f for f in result['files'] if f['path'] == source['path'])
        self.assertEqual(target['state'], 'redundant')
        self.assertEqual(target['prior_file_evidence'], before[source['path']]['prior_file_evidence'])
        self.assertIn(payload['note'], target['note'])
        self.assertEqual(target['similarity_evidence']['comparison_target']['inventory'], 'VLV-1132-6')
        self.assertEqual(result['selected_master_path'], '')
        self.assertFalse(result['accession_authorized'])
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(obj['decision'], previous)
        with self.memory_db() as db:
            r.record_review(db, result)
            self.assertEqual(r.readable_decisions(db)['reviews'][0], result)
            self.assertEqual(r.drain_proposals(db)['files'][0]['reason'], target['note'])

    def test_repeated_picture_choice_preserves_note_and_checks_completed_guards(self):
        obj, source, payload = self.picture_fixture()
        obj['decision'] = r.build_picture_review(obj, payload, 'earlier reviewer')
        obj['decision']['extra_evidence'] = {'unknown': ''}
        repeat = payload | {'note': '  '}
        result = r.build_picture_review(obj, repeat, 'another reviewer')
        self.assertEqual(result, obj['decision'])
        self.assertIsNot(result, obj['decision'])
        for completed in ('accession', 'section_accession'):
            obj[completed] = {'complete': True}
            with self.assertRaises(ValueError): r.build_picture_review(obj, repeat, 'test')
            del obj[completed]

    def test_picture_review_rejects_unsupported_redundancy_and_preserves_master_choices(self):
        obj, source, payload = self.picture_fixture()
        for changes in ({'comparison_path':'some-other-file'}, {'source_path':payload['comparison_path']},
                        {'file_state':'selected_master'}):
            with self.assertRaises(ValueError): r.build_picture_review(obj, payload | changes, 'test fixture')
        obj['registered_drain_paths'] = [source['path']]
        with self.assertRaises(ValueError): r.build_picture_review(obj, payload, 'test fixture')
        obj['registered_drain_paths'] = []
        for decision in ({'selected_master_path':source['path']}, {'source_version_pair':True},
                         {'files':[{'path':source['path'], 'state':'noise_drain'}]}):
            obj['decision'] = decision
            with self.assertRaises(ValueError): r.build_picture_review(obj, payload, 'test fixture')
        obj['decision'] = None
        obj['source_comparisons'] = {}
        payload['comparison_path'] = ''
        with self.assertRaises(ValueError): r.build_picture_review(obj, payload, 'test fixture')
        result = r.build_picture_review(obj, payload | {'file_state':'retain'}, 'test fixture')
        self.assertEqual(result['selected_master_path'], '')
        self.assertEqual([f['state'] for f in result['files']].count('retain'), 1)
        obj['accession'] = {'complete': True}
        with self.assertRaises(ValueError): r.build_picture_review(obj, payload | {'file_state':'retain'}, 'test fixture')

    def test_picture_index_finds_full_carrier_without_filename_grouping(self):
        index = r.index(self.con)
        pictures = index['pictures']
        self.assertEqual(len(pictures), len({p['path'] for p in pictures}))
        source = str(r.SOURCE / 'SCANS-fertig/Paragon/Paragon_Tif/IMG_20190815_143819.tif')
        full = next(p for p in pictures if p['path'] == source)
        self.assertEqual(full['comparison']['reference']['inventory'], 'VLV-1132')
        self.assertTrue(full['high_resolution'])
        self.assertTrue(full['remaining'])
        self.assertTrue(full['comparison']['manufactured_surrogate'])
        for p in pictures:
            if p['remaining']:
                self.assertTrue(p['decoded'])
                self.assertFalse(p['read_only'])
                self.assertEqual(p['state'], 'unresolved')

    def test_add_has_exactly_one_master_and_readable_evidence(self):
        review = self.build()
        self.assertEqual([f['state'] for f in review['files']].count('selected_master'), 1)
        self.assertEqual([f['state'] for f in review['files']].count('redundant'), 1)
        self.assertIn('metadata_source', review)
        self.assertFalse(review['accession_authorized'])
        self.assertFalse(review['deletion_authorized'])

    def test_keep_local_object_excludes_the_distinct_archived_comparison(self):
        obj = self.by_key['VLV-1043']
        selected = next(s['path'] for s in obj['sources'] if s['format'] == 'TIF')
        review = self.build(selected_master_path=selected)
        self.assertEqual(review['inventory'], 'VLV-1043')
        self.assertEqual(review['relationship']['predicate'], 'standalone')
        self.assertEqual(review['selected_master_path'], selected)
        self.assertEqual(review['proposed_metadata'], obj['proposed_metadata'])
        reference_paths = {ref['path'] for ref in obj['references']}
        self.assertTrue(reference_paths.isdisjoint({f['path'] for f in review['files']}))
        self.assertEqual({f['state'] for f in review['files']}, {'selected_master', 'redundant'})
        self.assertFalse(review['archive_changed'])

    def test_archived_comparison_cannot_be_chosen_or_marked_redundant(self):
        reference = self.by_key['VLV-1043']['references'][0]['path']
        with self.assertRaises(ValueError): self.build(selected_master_path=reference)
        with self.assertRaises(ValueError):
            self.build(file_choices={reference:dict(state='redundant')})

    def test_add_requires_reviewed_identity_metadata_and_one_master(self):
        for changes in [dict(selected_master_path=''), dict(metadata_reviewed=False),
                        dict(same_object_confirmed=False), dict(relation='unresolved'),
                        dict(metadata=dict(title='x', description='', provenance='source')),
                        dict(selected_master_path='/etc/passwd')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.build(**changes)

    def test_source_switch_preserves_name_metadata_and_unknown_values(self):
        key = str(r.SOURCE / 'Paragon/nur für Micha interessant/Nicht zugeordnete Objekte/IMG_20190815_141737.jpg')
        obj = r.get_case(self.con, key)
        selected = str(r.SOURCE / 'SCANS-fertig/Paragon/Paragon_Tif/IMG_20190815_141737.tif')
        for metadata in [dict(title='', description='', date='', provenance=''),
                         dict(title='Original item name', description='Original\r\ntext',
                              date='Uncertain', provenance='Original attribution')]:
            with self.subTest(metadata=metadata):
                case = copy.deepcopy(obj)
                case['decision']['proposed_metadata'] = metadata
                case['decision']['metadata_reviewed'] = False
                before = copy.deepcopy(case)
                review = r.build_review(case, dict(action='add', preserve_metadata=True,
                    selected_master_path=selected, same_object_confirmed=True,
                    metadata=dict(title='Do not import comparison metadata'),
                    relation='standalone', metadata_reviewed=True), 'test fixture')
                self.assertEqual(review['case_key'], key)
                self.assertEqual(review['inventory'], case['label'])
                self.assertEqual(review['proposed_metadata'], metadata)
                self.assertEqual(review['metadata_source'], case['decision']['metadata_source'])
                self.assertEqual(review['relationship'], case['decision']['relationship'])
                self.assertFalse(review['metadata_reviewed'])
                self.assertTrue(review['preserve_metadata'])
                self.assertEqual(review['selected_master_path'], selected)
                self.assertEqual(sum(f['state'] == 'selected_master' for f in review['files']), 1)
                self.assertTrue(all(f['state'] == 'redundant' for f in review['files'] if f['path'] != selected))
                self.assertEqual(case, before)
                self.assertFalse(review['archive_changed'])
                self.assertFalse(review['accession_authorized'])
                self.assertFalse(review['deletion_authorized'])
                with self.memory_db() as db:
                    r.record_review(db, review)
                    self.assertEqual(r.readable_decisions(db)['reviews'], [review])

    def test_preserving_metadata_does_not_bypass_master_or_identity_guards(self):
        for changes in [dict(selected_master_path=''), dict(same_object_confirmed=False),
                        dict(selected_master_path=self.by_key['VLV-1043']['references'][0]['path'])]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(preserve_metadata=True, **changes)
        obj = copy.deepcopy(self.by_key['VLV-1043'])
        obj['registered_drain_paths'] = [obj['sources'][0]['path']]
        obj['decision'] = {'files': [dict(path=obj['sources'][0]['path'], state='redundant')]}
        with self.assertRaisesRegex(ValueError, 'already recorded'):
            r.build_review(obj, self.payload() | dict(preserve_metadata=True), 'test fixture')

    def clone_work_db(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.con.backup(db)
        self.addCleanup(db.close)
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='logbook'").fetchall()
        for trigger in triggers:
            db.execute('DROP TRIGGER "' + trigger['name'].replace('"', '""') + '"')
        # Reconstruct the pre-pair fixture in memory. Live human choices must
        # neither make the tests stale nor be removed from the real catalogue.
        for row in db.execute('SELECT log_id,after_json FROM logbook WHERE action=?', (r.ACTION,)).fetchall():
            review = json.loads(row['after_json'])
            if any(field in review for field in ('source_version_pair', 'source_version_note', 'review_in_case')):
                db.execute('DELETE FROM logbook WHERE log_id=?', (row['log_id'],))
        for trigger in triggers:
            db.execute(trigger['sql'])
        db.commit()
        return db

    def test_saved_similar_master_is_visible_without_joining_cases_or_changing_decisions(self):
        pairs = r.hires_pairs()
        key = next(k for k,p in pairs.items() if p['name']=='IMG_20190815_142146.jpg')
        related = next(k for k,p in pairs.items() if p['name']=='IMG_20190815_142201.jpg')
        with self.clone_work_db() as db:
            c = r.hires_case(db, related)
            r.save_hires_review(db, dict(key=related, previous_reviews=c['previous_reviews'],
                action='add', selected_master_path=c['target']['path'], same_object_confirmed=True), 'test fixture')
            before = r.readable_decisions(db)
            result = r.public_hires_case(db, key)
            saved = next(x for x in result['related_saved_masters'] if x['pair_key']==related)
            self.assertTrue(saved['comparison_only'])
            self.assertEqual(saved['asset']['path'], c['target']['path'])
            self.assertEqual(saved['strength'], 'weak')
            self.assertEqual(len(result['earlier_files']), 3)
            self.assertTrue(all(f['state']=='redundant' for f in result['earlier_files']))
            self.assertFalse(result['resolved'])
            self.assertEqual(result['selected_master_path'], '')
            self.assertEqual(r.readable_decisions(db), before)
            with self.assertRaisesRegex(ValueError, 'Select one of these two files'):
                r.save_hires_review(db, dict(key=key, previous_reviews=result['previous_reviews'],
                    action='add', selected_master_path=c['target']['path'], same_object_confirmed=True), 'test fixture')

    def test_unresolved_pair_is_identified_when_reopened(self):
        key = next(iter(r.hires_pairs()))
        with self.clone_work_db() as db:
            c = r.hires_case(db, key)
            r.save_hires_review(db, dict(key=key, previous_reviews=c['previous_reviews'],
                action='unresolved', note='Compare with the saved sheet'), 'test fixture')
            result = r.public_hires_case(db, key)
            self.assertTrue(result['left_unresolved'])
            self.assertFalse(result['resolved'])
            self.assertEqual(result['saved_note'], 'Compare with the saved sheet')
            self.assertTrue(next(p for p in r.hires_index(db)['pairs'] if p['key']==key)['left_unresolved'])

    def test_browser_navigation_stops_at_end_and_skips_concurrently_saved_pairs(self):
        # Run the real page's save/navigation functions with an in-memory DOM
        # and API. No HTTP writes, browser state, or source files are involved.
        script = (r.ROOT / 'remaining-similarity.html').read_text().split('<script>', 1)[1].split('</script>', 1)[0]
        script = script[:script.rindex('(async()=>')]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let source='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>source+=s);
process.stdin.on('end',async()=>{
 const elements=new Map();
 const element=s=>{if(!elements.has(s))elements.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,className:'',checked:true});return elements.get(s);};
 const context=vm.createContext({document:{querySelector:element,querySelectorAll:()=>[]},window:{addEventListener(){}},history:{replaceState(){}},confirm:()=>true,console});
 vm.runInContext(source,context);
 vm.runInContext(`
 var opened=[],ended=0,posts=0,serverPairs=[],serverResolved=new Set(),failSave=false;
 render=()=>{opened.push(current.pair.key);dirty=false;};
 renderEnd=()=>{ended++;current=null;dirty=false;};
 api=async(url,options)=>{
  if(options){posts++;if(failSave)throw Error('fixture rejected');const body=JSON.parse(options.body);if(body.action==='add')serverResolved.add(body.key);return {};}
  if(url==='/retained/api/hires')return {token:'test',total:serverPairs.length,remaining:serverPairs.filter(p=>!serverResolved.has(p.key)).length,pairs:serverPairs.map(p=>({...p,resolved:serverResolved.has(p.key)}))};
  const key=decodeURIComponent(url.split('key=')[1]);return {pair:{key},name:key,can_choose:!serverResolved.has(key),resolved:serverResolved.has(key)};
 };
 function fixture(keys,key,resolved=[]){
  opened=[];ended=0;posts=0;failSave=false;serverPairs=keys.map(key=>({key,name:key}));serverResolved=new Set(resolved);
  rows=[...serverPairs];index={token:'test'};current={pair:{key},name:key,can_choose:true,previous_reviews:{}};dirty=true;saving=false;
  $('#scope').value='remaining';$('input[name=master]:checked').value='fixture-master';$('#confirmed').checked=true;
 }
 `,context);
 async function run(setup,action){vm.runInContext(setup,context);await vm.runInContext(action,context);return JSON.parse(vm.runInContext('JSON.stringify({opened,ended,posts,key:current?.pair.key,dirty})',context));}
 let result=await run("fixture(['a','b'],'b')","save('add')");assert.equal(result.ended,1);assert.deepEqual(result.opened,[]);assert.equal(result.posts,1);
 result=await run("fixture(['a','b'],'b')","save('unresolved')");assert.equal(result.ended,1);assert.deepEqual(result.opened,[]);
 result=await run("fixture(['a','b','c'],'a',['b'])","save('add')");assert.deepEqual(result.opened,['c']);assert.equal(result.ended,0);
 result=await run("fixture(['a','b'],'a',['a'])","openPair('a')");assert.deepEqual(result.opened,['b']);assert.equal(result.posts,0);
 result=await run("fixture(['a','b'],'a');failSave=true","save('add')");assert.equal(result.key,'a');assert.equal(result.dirty,true);assert.equal(result.ended,0);
 console.log('Navigation: saved/skip end, concurrent save, and failed save checks passed');
});
'''
        result = subprocess.run(['node', '-e', harness], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_focused_shortlist_uses_42_stored_pairs_and_three_split_cases(self):
        pairs = r.hires_pairs()
        self.assertEqual(len(pairs), 42)
        self.assertEqual(sum(p['case_key'] != p['target_case_key'] for p in pairs.values()), 3)
        with self.clone_work_db() as db:
            index = r.hires_index(db)
            self.assertEqual(index['remaining'], 42)
            self.assertTrue(all(p['can_choose'] for p in index['pairs']))
            self.assertEqual(sum(bool(r.hires_case(db,k)['existing_inventory']) for k in pairs), 19)

    def test_all_42_choices_preserve_metadata_old_evidence_and_unique_path_ownership(self):
        with self.clone_work_db() as db:
            initial = r.saved_reviews(db)
            initial_drain = {f['path'] for f in r.drain_proposals(db)['files']}
            initial_files = {f['path']:copy.deepcopy(f) for d in initial.values() for f in d['files']}
            changed_paths = set()
            for key, pair in r.hires_pairs().items():
                c = r.hires_case(db,key)
                previous = r.saved_reviews(db).get(pair['case_key'], {})
                result = r.save_hires_review(db, dict(key=key, previous_reviews=c['previous_reviews'],
                    action='add', selected_master_path=pair['target_path'], same_object_confirmed=True,
                    note='Checked same item and full frame'), 'test fixture')
                self.assertEqual(result['proposed_metadata'], c['metadata'])
                self.assertEqual(result['metadata_source'], c['metadata_source'])
                self.assertEqual(result['inventory'], c['label'])
                self.assertEqual(result['relationship'], previous.get('relationship',dict(predicate='unresolved',carrier='')))
                self.assertEqual(result['source_version_pair']['original_name'], c['name'])
                self.assertEqual(sum(f['state']=='selected_master' for f in result['files']), 1)
                self.assertEqual(result['selected_master_path'], pair['target_path'])
                self.assertFalse(result['accession_authorized'])
                self.assertFalse(result['archive_changed'])
                changed_paths.update([pair['source_path'],pair['target_path']])
            self.assertEqual(r.hires_index(db)['remaining'], 0)
            effective = r.saved_reviews(db)
            files = [f for review in effective.values() for f in review['files']]
            self.assertEqual(len(files), len({f['path'] for f in files}))
            after = {f['path']:f for f in files}
            for p, f in initial_files.items():
                if p not in changed_paths:
                    self.assertEqual(after[p], f, p)
            live_cases = r.cases(db)
            paths = [s['path'] for c in live_cases for s in c['sources']]
            self.assertEqual(len(paths),1132)
            self.assertEqual(len(set(paths)),1132)
            self.assertEqual({f['path'] for f in r.drain_proposals(db)['files']},
                             initial_drain | {p['source_path'] for p in r.hires_pairs().values()})
            self.assertTrue(all(not f['deletion_authorized'] for f in files))
            for pair in r.hires_pairs().values():
                if pair['target_case_key'] != pair['case_key']:
                    self.assertEqual(effective[pair['target_case_key']]['files'], [])
                    self.assertEqual(effective[pair['target_case_key']]['review_in_case'],pair['case_key'])
            outputs = {}
            with patch.object(r,'write_json',side_effect=lambda p,d: outputs.update({p:copy.deepcopy(d)})):
                r.export_decisions(db)
            self.assertEqual(outputs[r.DECISIONS],r.readable_decisions(db))
            self.assertEqual(outputs[r.DRAIN_PROPOSALS],r.drain_proposals(db))

    def test_focused_review_rejects_stale_unknown_unconfirmed_and_registered_choices(self):
        key = next(iter(r.hires_pairs()))
        with self.clone_work_db() as db:
            c=r.hires_case(db,key)
            payload=dict(key=key,previous_reviews=c['previous_reviews'],action='add',
                         selected_master_path=c['target']['path'],same_object_confirmed=True)
            for change in [dict(previous_reviews={}), dict(key='/etc/passwd'),
                           dict(selected_master_path='/etc/passwd'), dict(same_object_confirmed=False)]:
                with self.subTest(change=change),self.assertRaises(ValueError):
                    r.save_hires_review(db,payload|change,'test fixture')
            with patch.object(r,'evidence',return_value=dict(r.evidence(),drain_registered={c['target']['path']})):
                with self.assertRaisesRegex(ValueError,'no longer available'):
                    r.save_hires_review(db,payload,'test fixture')
            r.save_hires_review(db,payload,'test fixture')
            with self.assertRaisesRegex(ValueError,'changed in another tab'):
                r.save_hires_review(db,payload,'test fixture')

    def test_leave_split_pair_unresolved_preserves_files_and_does_not_join_cases(self):
        key=next(k for k,p in r.hires_pairs().items() if p['case_key']!=p['target_case_key'])
        with self.clone_work_db() as db:
            c=r.hires_case(db,key);before=r.saved_reviews(db)
            review=r.save_hires_review(db,dict(key=key,previous_reviews=c['previous_reviews'],
                                      action='unresolved',note='Edges need closer review'),'test fixture')
            self.assertEqual(review['files'],before[c['pair']['case_key']]['files'])
            self.assertEqual(review['note'],before[c['pair']['case_key']]['note'])
            self.assertEqual(r.saved_reviews(db)[c['pair']['target_case_key']],before[c['pair']['target_case_key']])
            self.assertNotIn('source_version_pair',review)
            self.assertEqual(r.hires_case(db,key)['saved_note'],'Edges need closer review')
            self.assertEqual(r.hires_index(db)['remaining'],42)

    def test_jpeg_can_remain_master_when_human_prefers_it(self):
        key=next(iter(r.hires_pairs()))
        with self.clone_work_db() as db:
            c=r.hires_case(db,key)
            review=r.save_hires_review(db,dict(key=key,previous_reviews=c['previous_reviews'],action='add',
                selected_master_path=c['source']['path'],same_object_confirmed=True),'test fixture')
            self.assertEqual(review['selected_master_path'],c['source']['path'])
            self.assertEqual(next(f['state'] for f in review['files'] if f['path']==c['target']['path']),'redundant')

    def test_focused_http_save_handles_split_case_and_removes_only_saved_pair(self):
        key=next(k for k,p in r.hires_pairs().items() if p['case_key']!=p['target_case_key'])
        db=self.clone_work_db()
        @contextmanager
        def connection(readonly=True):
            with db: yield db
        c=r.hires_case(db,key)
        payload=dict(key=key,previous_reviews=c['previous_reviews'],action='add',
                     selected_master_path=c['target']['path'],same_object_confirmed=True)
        def request(token='fixture-token'):
            raw=json.dumps(payload).encode();handler=app.Handler.__new__(app.Handler)
            handler.path='/retained/api/hires-decision'
            handler.headers={'Host':'127.0.0.1:8766','X-Review-Token':token,'Content-Length':str(len(raw))}
            handler.rfile=io.BytesIO(raw)
            handler.server=SimpleNamespace(server_port=8766,token='fixture-token',actor='test fixture')
            output=[];handler.response=lambda data,status=200,**kwargs:output.append((status,data))
            handler.do_POST();return output[-1]
        fake_file=SimpleNamespace(name=r.DECISIONS.name,read_text=lambda:json.dumps(r.readable_decisions(db)))
        with patch.object(app,'RETAINED_REVIEW',True),patch.object(app,'connect',connection), \
             patch.object(r,'DECISIONS',fake_file),patch.object(r,'export_decisions') as export:
            self.assertEqual(request('wrong')[0],403)
            status,result=request()
            self.assertEqual(status,200,result)
            self.assertEqual(result['review']['proposed_metadata'],c['metadata'])
            self.assertEqual(result['review']['selected_master_path'],c['target']['path'])
            self.assertEqual(r.hires_index(db)['remaining'],41)
            self.assertEqual(export.call_count,1)
            self.assertEqual(request()[0],409)
        db.close()

    def test_accessioned_object_rejects_new_review_decisions(self):
        obj = dict(self.by_key['VLV-1043'], accession={'receipt':'completed receipt'})
        for action in ['add','retain','noise_drain','unresolved']:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError,'has been accessioned'):
                r.build_review(obj,self.payload(action=action),'test fixture')

    def test_completed_accession_releases_only_its_own_redundant_proposal_dependency(self):
        review = self.build()
        ev = dict(r.evidence())
        completed = dict(selected_source=review['selected_master_path'],source_sha256='sha256:selected',receipt='completed receipt')
        ev['accessioned'] = {'VLV-1043':completed}
        with self.memory_db() as db, patch.object(r,'evidence',return_value=ev):
            r.record_review(db,review)
            proposals=r.drain_proposals(db)['files']
            self.assertTrue(proposals)
            self.assertFalse(proposals[0]['awaiting_accession'])
            self.assertEqual(proposals[0]['accession_coverage'],completed)
            self.assertFalse(proposals[0]['deletion_authorized'])

    def test_accessioned_carrier_sections_reject_changed_decisions(self):
        obj = dict(self.by_key['VLV-1148'], section_accession={'receipt':'completed sections receipt'})
        with self.assertRaisesRegex(ValueError,'sections have been accessioned'):
            r.build_review(obj,self.payload('VLV-1148','retain'),'test fixture')

    def test_registered_noise_stays_recorded_without_claiming_coverage(self):
        obj = copy.deepcopy(self.by_key['VLV-1103-1'])
        path = obj['sources'][0]['path']
        payload = self.payload('VLV-1103-1','noise_drain') | dict(file_choices={path:dict(state='noise_drain')})
        decision = r.build_review(obj,payload,'test fixture')
        obj.update(decision=decision,registered_drain_paths=[path])
        with self.assertRaisesRegex(ValueError,'already recorded'):
            r.build_review(obj,payload | dict(action='retain',file_choices={path:dict(state='retain')}),'test fixture')
        unchanged = r.build_review(obj,payload,'test fixture')
        with self.memory_db() as db,patch.object(r,'evidence',return_value=dict(r.evidence(),drain_registered={path})):
            r.record_review(db,unchanged)
            proposal = next(f for f in r.drain_proposals(db)['files'] if f['path']==path)
            self.assertTrue(proposal['in_drain_register'])
            self.assertFalse(proposal['awaiting_accession'])
            self.assertNotIn('accession_coverage',proposal)
            self.assertFalse(proposal['deletion_authorized'])

    def test_false_gap_cannot_be_added_under_another_number(self):
        for key in ['VLV-1049', 'VLV-1065', 'VLV-1057', 'VLV-1095-1', 'VLV-1019']:
            with self.subTest(key=key), self.assertRaises(ValueError): self.build(key)

    def test_invitation_interior_shows_both_archived_parts_and_can_be_retained(self):
        obj = self.by_key['VLV-1095-1']
        self.assertEqual(obj['existing_inventory'], 'VLV-1095')
        self.assertEqual([ref['part'] for ref in obj['references']], ['open_interior', 'cover'])
        self.assertTrue(all(ref['inventory'] == 'VLV-1095' for ref in obj['references']))
        self.assertEqual(obj['proposed_metadata']['date'], '')
        path = obj['sources'][0]['path']
        review = self.build('VLV-1095-1', action='retain', file_choices={path:dict(state='retain')})
        self.assertEqual([(f['path'], f['state']) for f in review['files']], [(path, 'retain')])
        self.assertEqual(review['selected_master_path'], '')
        self.assertFalse(review['archive_changed'])
        with self.memory_db() as db:
            r.record_review(db, review)
            self.assertEqual(r.readable_decisions(db)['reviews'], [review])
            self.assertEqual(r.drain_proposals(db)['files'], [])

    def test_date_proposals_preserve_uncertainty_and_do_not_use_employment_tenure(self):
        self.assertEqual(self.by_key['VLV-1043']['proposed_metadata']['date'], 'c.1985')
        for key in ['VLV-1042', 'VLV-1047', 'VLV-1099', 'VLV-1103-1', 'VLV-1113']:
            self.assertEqual(self.by_key[key]['proposed_metadata']['date'], '')

    def test_ignore_is_not_a_drain_decision(self):
        path = self.by_key['VLV-1043']['sources'][0]['path']
        review = self.build(action='ignore', file_choices={path:dict(state='noise_drain')})
        self.assertEqual(review['selected_master_path'], '')
        self.assertTrue(all(f['state'] == 'unresolved' for f in review['files']))

    def test_noise_is_exact_path_with_optional_reason_and_coverage(self):
        obj = self.by_key['VLV-1103-1']
        path = next(s['path'] for s in obj['sources'] if s['format'] == 'TIF')
        choices = {path:dict(state='noise_drain')}
        for optional in [{}, {'note':'Optional reason'}, {'coverage_confirmed':True},
                         {'note':'Optional reason', 'coverage_confirmed':True}]:
            with self.subTest(optional=optional):
                review = r.build_review(obj, dict(action='noise_drain', file_choices=choices) | optional, 'test fixture')
                self.assertEqual(sum(f['state'] == 'noise_drain' for f in review['files']), 1)
                self.assertEqual(sum(f['state'] == 'unresolved' for f in review['files']), 1)
                self.assertEqual(review['note'], optional.get('note', ''))
                self.assertEqual(review['coverage_confirmed'], optional.get('coverage_confirmed', False))
                self.assertTrue(all(not f['deletion_authorized'] for f in review['files']))
                with self.memory_db() as db:
                    r.record_review(db, review)
                    self.assertEqual(r.readable_decisions(db)['reviews'], [review])
                    proposed = r.drain_proposals(db)['files']
                    self.assertEqual([f['path'] for f in proposed], [path])
                    self.assertEqual(proposed[0]['reason'], optional.get('note', ''))
        with self.assertRaises(ValueError):
            self.build('VLV-1103-1', action='noise_drain', file_choices={})

    def test_unknown_paths_cannot_be_drained(self):
        with self.assertRaises(ValueError):
            self.build(action='noise_drain', file_choices={'/etc/passwd':dict(state='noise_drain')},
                       coverage_confirmed=True, note='No arbitrary file access')

    def test_partial_derivative_review_keeps_unresolved_sources_visible_and_accession_pending(self):
        obj = self.by_key['VLV-1103-1']; path = obj['sources'][0]['path']
        review = self.build('VLV-1103-1', action='retain', file_choices={path:dict(state='redundant')})
        reviewed_file = next(f for f in review['files'] if f['path'] == path)
        reviewed_file['derivative_evidence'] = dict(report='retained-derivative-check.json',
            compared_to=obj['sources'][1]['path'], awaiting_accession=True)
        with self.memory_db() as db:
            r.record_review(db, review)
            with patch.object(r, 'cases', return_value=self.cases):
                case = next(c for c in r.index(db)['cases'] if c['key'] == obj['key'])
            self.assertTrue(case['reviewed_at'])
            self.assertEqual(case['unresolved_source_count'], 1)
            proposal = r.drain_proposals(db)['files'][0]
            self.assertFalse(proposal['awaiting_accession'])
            self.assertTrue(proposal['covering_source_awaits_accession'])
            self.assertFalse(proposal['deletion_authorized'])
            self.assertEqual(proposal['derivative_evidence'], reviewed_file['derivative_evidence'])

    def test_carrier_and_members_keep_independent_retention(self):
        obj = self.by_key['VLV-1130']; path = obj['sources'][0]['path']; member = obj['members'][0]['inventory']
        review = self.build('VLV-1130', action='retain', file_choices={path:dict(state='carrier_section')},
                            retention=dict(carrier='retain', members={member:'unresolved'}))
        self.assertEqual(review['selected_master_path'], '')
        self.assertEqual(review['retention']['carrier'], 'retain')
        self.assertEqual(review['retention']['members'][member], 'unresolved')
        self.assertEqual(review['files'][0]['relation'], 'section_of')
        self.assertTrue(all(f['state'] != 'redundant' for f in review['files']))

    def test_resaving_partial_review_preserves_derivative_dependency_from_saved_evidence(self):
        obj = copy.deepcopy(self.by_key['VLV-1103-1'])
        path = obj['sources'][0]['path']
        payload = dict(action='retain', file_choices={path:dict(state='redundant')})
        original = r.build_review(obj, payload, 'test fixture')
        original['files'][0]['derivative_evidence'] = dict(awaiting_accession=True, report='retained-derivative-check.json')
        obj['decision'] = original
        revised = r.build_review(obj, payload, 'test fixture')
        self.assertEqual(revised['files'][0]['derivative_evidence'], original['files'][0]['derivative_evidence'])
        obj['decision'] = None
        payload['file_choices'][path]['derivative_evidence'] = dict(awaiting_accession=False)
        self.assertNotIn('derivative_evidence', r.build_review(obj, payload, 'test fixture')['files'][0])

    def test_new_member_content_does_not_invent_object_identity(self):
        obj = self.by_key['VLV-1148']; path = obj['sources'][0]['path']
        review = self.build('VLV-1148', action='retain', file_choices={path:dict(state='member',member_target='')})
        self.assertEqual(review['files'][0]['member_target'], '')
        self.assertEqual(review['files'][0]['carrier'], 'VLV-1148')
        self.assertEqual(review['files'][0]['relation'], 'component_of')
        self.assertEqual(review['selected_master_path'], '')

    def test_registered_hash_duplicate_survives_partial_review_with_other_unit_evidence(self):
        obj = copy.deepcopy(self.by_key['VLV-1103-1']);path = obj['sources'][0]['path']
        payload = dict(action='retain',file_choices={path:dict(state='redundant')})
        original = r.build_review(obj,payload,'test fixture')
        original['files'][0]['duplicate_evidence'] = dict(retained_source_path='/retained/source',basis='equal stored SHA-256')
        obj['sources'][0]['other_unit_links'] = [{'unit':'other','preservation_status':'not_assessed'}]
        with self.assertRaisesRegex(ValueError,'another-unit'):
            r.build_review(obj,payload,'test fixture')
        obj.update(decision=original,registered_drain_paths=[path])
        revised = r.build_review(obj,payload,'test fixture')
        self.assertEqual(revised['files'][0]['duplicate_evidence'],original['files'][0]['duplicate_evidence'])
        self.assertEqual(revised['files'][1]['state'],'unresolved')
        with self.assertRaisesRegex(ValueError,'already recorded'):
            r.build_review(obj,dict(action='retain',file_choices={path:dict(state='retain')}),'test fixture')

    def test_similarity_proof_survives_resave_without_accepting_injected_proof(self):
        obj=copy.deepcopy(self.by_key['VLV-1103-1']);path=obj['sources'][0]['path']
        payload=dict(action='retain',file_choices={path:dict(state='redundant')})
        original=r.build_review(obj,payload,'test fixture')
        proof=dict(comparison_target=dict(target='/retained/surviving-source'),receipt='similarity-drain.json')
        original['files'][0]['similarity_evidence']=proof
        obj.update(decision=original,registered_drain_paths=[path])
        revised=r.build_review(obj,payload|{'note':'Review of another file in this case'},'test fixture')
        self.assertEqual(revised['files'][0]['similarity_evidence'],proof)
        self.assertEqual(revised['files'][1]['state'],'unresolved')
        obj.update(decision=None,registered_drain_paths=[])
        payload['file_choices'][path]['similarity_evidence']=proof
        self.assertNotIn('similarity_evidence',r.build_review(obj,payload,'test fixture')['files'][0])

    def test_invalid_carrier_member_relationship_is_rejected(self):
        obj = self.by_key['VLV-1130']; path = obj['sources'][0]['path']
        with self.assertRaises(ValueError):
            self.build('VLV-1130', action='retain', file_choices={path:dict(state='member',member_target='VLV-1043')})
        with self.assertRaises(ValueError): self.build(relation='component_of', parent='VLV-nonexistent')

    def test_carrier_redundancy_requires_exact_file_but_note_and_coverage_are_optional(self):
        obj = self.by_key['VLV-1130']; path = obj['sources'][0]['path']
        review = self.build('VLV-1130', action='retain', file_choices={path:dict(state='redundant')})
        self.assertEqual([f['path'] for f in review['files'] if f['state'] == 'redundant'], [path])
        self.assertTrue(all(f['state'] == 'unresolved' for f in review['files'] if f['path'] != path))
        self.assertEqual(review['note'], '')
        self.assertFalse(review['coverage_confirmed'])
        self.assertFalse(review['archive_changed'])
        self.assertFalse(review['deletion_authorized'])

    def test_json_and_existing_sqlite_logbook_agree_and_revise(self):
        with self.memory_db() as db:
            review = self.build(); r.record_review(db, review)
            outputs = {}
            with patch.object(r, 'write_json', side_effect=lambda p,d: outputs.update({p:json.loads(json.dumps(d))})):
                r.export_decisions(db)
            self.assertEqual(outputs[r.DECISIONS], r.readable_decisions(db))
            self.assertEqual(len(outputs[r.DECISIONS]['reviews']), 1)
            self.assertTrue(outputs[r.DRAIN_PROPOSALS]['files'][0]['awaiting_accession'])
            self.assertFalse(outputs[r.DRAIN_PROPOSALS]['files'][0]['deletion_authorized'])
            revised = self.build(action='metadata_review', note='Identity needs a closer look')
            r.record_review(db, revised)
            self.assertEqual(r.readable_decisions(db)['reviews'][0]['action'], 'metadata_review')
            self.assertEqual(r.drain_proposals(db)['files'], [])
            self.assertEqual(db.execute('SELECT count(*) FROM logbook').fetchone()[0], 2)

    def test_review_image_cannot_read_arbitrary_paths(self):
        with self.assertRaises(ValueError): r.image_bytes('/etc/passwd')

    def test_picture_http_save_records_exact_comparison_and_rejects_stale_save(self):
        db = self.memory_db(); db.commit()
        obj, source, payload = self.picture_fixture()
        payload.update(case_key=obj['key'], previous_review='')
        @contextmanager
        def connection(readonly=True):
            with db: yield db
        def case(con, key):
            return copy.deepcopy(obj) | {'decision': r.saved_reviews(con).get(key)}
        outputs = []
        def request(body):
            raw = json.dumps(body).encode(); handler = app.Handler.__new__(app.Handler)
            handler.path = '/retained/api/decision'
            handler.headers = {'Host':'127.0.0.1:8766', 'Origin':'http://127.0.0.1:8766',
                               'X-Review-Token':'fixture-token', 'Content-Length':str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler.server = SimpleNamespace(server_port=8766, token='fixture-token', actor='test fixture')
            handler.response = lambda data,status=200,**kwargs: outputs.append((status,data))
            handler.do_POST()
            return outputs[-1]
        fake_file = SimpleNamespace(name=r.DECISIONS.name, read_text=lambda:json.dumps(r.readable_decisions(db)))
        exported = {}
        # Patch disk writes only; exercise the real handler, builder and export data.
        try:
            with patch.object(app, 'RETAINED_REVIEW', True), patch.object(app, 'connect', connection), \
                 patch.object(r, 'get_case', side_effect=case), patch.object(r, 'DECISIONS', fake_file), \
                 patch.object(r, 'write_json', side_effect=lambda path,data: exported.update({path.name:data})):
                status, result = request(payload)
                self.assertEqual(status, 200)
                self.assertEqual(result['review']['selected_master_path'], '')
                self.assertEqual(exported[r.DECISIONS.name], r.readable_decisions(db))
                proposals = exported[r.DRAIN_PROPOSALS.name]['files']
                self.assertEqual([f['path'] for f in proposals], [source['path']])
                self.assertEqual(proposals[0]['similarity_evidence']['comparison_target']['target'], payload['comparison_path'])
                self.assertEqual(request(payload)[0], 409)
                export_before = copy.deepcopy(exported)
                exported.clear()
                repeat = payload | dict(previous_review=result['review']['reviewed_at'], note='')
                status, unchanged = request(repeat)
                self.assertEqual(status, 200)
                self.assertTrue(unchanged['unchanged'])
                self.assertEqual(unchanged['review'], result['review'])
                self.assertEqual(exported, {})
                self.assertEqual(db.execute('SELECT count(*) FROM logbook').fetchone()[0], 1)
                self.assertEqual(export_before[r.DECISIONS.name], r.readable_decisions(db))
                self.assertEqual(request(repeat | dict(comparison_path='stale'))[0], 409)
                status, changed = request(repeat | dict(note='New comparison detail'))
                self.assertEqual(status, 200)
                self.assertEqual(db.execute('SELECT count(*) FROM logbook').fetchone()[0], 2)
                self.assertIn('New comparison detail', next(f['note'] for f in changed['review']['files'] if f['path'] == source['path']))
        finally:
            db.close()

    def test_http_handler_saves_and_rejects_stale_or_unauthenticated_requests(self):
        db = self.memory_db(); db.commit()
        @contextmanager
        def connection(readonly=True):
            with db: yield db
        def case(con, key):
            return copy.deepcopy(self.by_key[key]) | {'decision':r.saved_reviews(con).get(key)}
        payload = self.payload() | dict(case_key='VLV-1043', previous_review='')
        outputs = []
        def request(body, token='fixture-token'):
            raw = json.dumps(body).encode(); handler = app.Handler.__new__(app.Handler)
            handler.path = '/retained/api/decision'
            handler.headers = {'Host':'127.0.0.1:8766','Origin':'http://127.0.0.1:8766',
                               'X-Review-Token':token,'Content-Length':str(len(raw))}
            handler.rfile = io.BytesIO(raw)
            handler.server = SimpleNamespace(server_port=8766, token='fixture-token', actor='test fixture')
            handler.response = lambda data,status=200,**kwargs: outputs.append((status,data))
            handler.do_POST()
            return outputs[-1]
        fake_file = SimpleNamespace(name=r.DECISIONS.name, read_text=lambda:json.dumps(r.readable_decisions(db)))
        with patch.object(app, 'RETAINED_REVIEW', True), patch.object(app, 'connect', connection), \
             patch.object(r, 'get_case', side_effect=case), patch.object(r, 'DECISIONS', fake_file), \
             patch.object(r, 'export_decisions') as export:
            status, result = request(payload)
            self.assertEqual(status, 200)
            self.assertEqual(result['review']['action'], 'add')
            self.assertEqual(export.call_count, 1)
            self.assertEqual(request(payload)[0], 409)
            self.assertEqual(request(payload, token='wrong')[0], 403)
            revised = payload | dict(action='ignore', previous_review=result['review']['reviewed_at'])
            self.assertEqual(request(revised)[0], 200)
            self.assertEqual(r.saved_reviews(db)['VLV-1043']['action'], 'ignore')
            self.assertEqual(r.drain_proposals(db)['files'], [])
            self.assertEqual(db.execute('SELECT count(*) FROM logbook').fetchone()[0], 2)
        db.close()


if __name__ == '__main__': unittest.main(verbosity=2)
