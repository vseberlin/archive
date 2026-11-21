"""Behavior tests for the approved synthetic browse slice; no archive files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from archive_research.common import ReviewError, atomic_write, content_revision
from .fixtures import create_fixture
from .models import ResearchCase
from .records import bound_bytes, checked_cases, export_bundle, rebuild, save_review, snapshot


class PilotTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.temp = tempfile.TemporaryDirectory(dir=settings.PILOT_ROOT / 'cache', prefix='tests-')
        cls.root = Path(cls.temp.name)
        for name in ['sample', 'records', 'cache', 'exports']:
            (cls.root / name).mkdir()
        create_fixture(cls.root)
        cls.original_evidence = (cls.root / 'records/evidence.json').read_bytes()
        cls.original_assertions = (cls.root / 'records/assertions.jsonl').read_bytes()
        cls.override = override_settings(PILOT_ROOT=cls.root, ALLOWED_HOSTS=['testserver', '127.0.0.1'],
                                         PROJECT={'mode': 'synthetic'})
        cls.override.enable()

    @classmethod
    def tearDownClass(cls):
        cls.override.disable()
        cls.temp.cleanup()
        super().tearDownClass()

    def setUp(self):
        atomic_write(self.root / 'records/evidence.json', self.original_evidence)
        atomic_write(self.root / 'records/assertions.jsonl', self.original_assertions)
        rebuild()
        self.user = get_user_model().objects.create_superuser('operator', '', 'fixture-only-password')
        self.client.force_login(self.user)
        for path in (self.root / 'cache').glob('*-ocr.json'):
            path.unlink()

    def test_login_required_for_case_and_media(self):
        anon = Client()
        for url in ['/', '/cases/photo/', '/media/photo/report-figure/']:
            self.assertEqual(anon.get(url).status_code, 302)

    def test_prepared_table_reading_escapes_cells_and_withholds_stale_or_invalid_data(self):
        from .processing import binding, ocr_status
        view = snapshot()[0]['cases'][0]['views'][0]
        path = self.root / 'cache' / f"{view['key']}-ocr.json"
        result = {'binding': binding(view), 'status': 'ready', 'text': '', 'note': '',
                  'tables': [{'header': 'Raw heading', 'rows': [['<script>bad()</script>\nW/(m·K)', '']],
                              'unassigned': 'Loose OCR words'}]}
        path.write_text(json.dumps(result))
        response = self.client.get('/cases/photo/')
        self.assertContains(response, '&lt;script&gt;bad()&lt;/script&gt;')
        self.assertNotContains(response, '<script>bad()</script>')
        self.assertContains(response, '&lt;script&gt;bad()&lt;/script&gt;\nW/(m·K)</td>')
        self.assertContains(response, 'vertical-align:top;white-space:pre-wrap')
        self.assertContains(response, 'No words recovered')
        self.assertContains(response, 'Loose OCR words')
        self.assertEqual(ocr_status({**view, 'rotation': (view['rotation'] + 90) % 360})['status'], 'stale')
        result['tables'][0]['rows'].append(['wrong width'])
        path.write_text(json.dumps(result))
        self.assertEqual(ocr_status(view)['status'], 'unavailable')
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)

    def test_handwriting_alternative_is_escaped_and_does_not_change_review(self):
        from .processing import binding
        view = snapshot()[0]['cases'][0]['views'][0]
        result = {'binding': binding(view), 'status': 'ready', 'text': 'First reading',
                  'note': 'Selected handwritten lines only.', 'blocks': [
                      {'index': 0, 'text': 'First reading', 'display_region': [0, 0, 1, 1],
                       'alternative_text': '<script>alternative</script>'}]}
        (self.root / 'cache' / f"{view['key']}-ocr.json").write_text(json.dumps(result))
        response = self.client.get('/cases/photo/')
        self.assertContains(response, 'Alternative reading — check the scan')
        self.assertContains(response, '&lt;script&gt;alternative&lt;/script&gt;')
        self.assertNotContains(response, '<script>alternative</script>')
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)

    def test_search_highlights_caption_and_escapes_source_markup(self):
        from collections import Counter
        from archive_research.search_projection import base_entry, seal, tokens, FORMAT
        text = 'Page opening. ' * 100 + '<script>alert(1)</script> Pumpstraße im Werk.'
        entry = base_entry('a'*64, 'extraction', {'unit': 'synthetic', 'object_id': 'page'}, {}, {'text': text}, {})
        counts = Counter(tokens(' '.join(entry['fields'].values())))
        entry['token_count'] = sum(counts.values())
        projection = seal({'format': FORMAT, 'metadata_adopted': False, 'entries': [entry],
                           'postings': {word: [[0, count]] for word, count in counts.items()},
                           'coverage': {}, 'unavailable': [], 'refreshed_at': 'synthetic'})
        with override_settings(PROJECT={'mode': 'synthetic', 'research_index': 'synthetic.json'}), \
             patch('archive_research.web.extraction.search_projection', return_value=(None, projection)):
            response = self.client.get('/research/', {'q': 'Pumpstrasse'})
        self.assertContains(response, '<mark>Pumpstraße</mark>', html=True)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')

    def prepare_people_case(self):
        from copy import deepcopy
        evidence, _, current, _ = snapshot()
        case = deepcopy(evidence['cases'][0])
        case.update(key='people', title='Invented documentary attribution fixture')
        first, second = [v['key'] for v in case['views'][:2]]
        case['documentary_target_view'] = first
        case['documentary_people'] = [
            {'key': 'ada', 'name': 'Ada Example', 'reason': 'Named in the invented workshop attendance list.',
             'context': {'event': 'Workshop', 'date': '1967-03-06', 'department': 'Assembly'},
             'citation_keys': [second], 'uncertainty': 'Attendance does not establish depiction.',
             'portraits': [{'view_key': second, 'caption': 'Invented illustration, not a real portrait.',
                           'citation_keys': [second]}]},
            {'key': 'berta', 'name': 'Bärta Example', 'reason': 'Named in an invented catalogue caption.',
             'context': {'event': 'Award', 'date': '1955-10', 'department': 'Accounts'},
             'citation_keys': [second], 'uncertainty': 'No portrait prepared.', 'portraits': []}]
        evidence['cases'].append(case)
        initial = {**current[0], 'case_key': 'people', 'key': 'context', 'relation': 'research_assessment',
                   'subject': 'people', 'revision': 0, 'supersedes_revision': None}
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        with (self.root/'records/assertions.jsonl').open('ab') as handle:
            handle.write(json.dumps(initial).encode()+b'\n')
        rebuild()
        return {'revision': snapshot()[3], 'attribution_key': '', 'target_view_key': first,
                'position': 'Second from the left', 'candidate_key': 'ada', 'review': 'accepted',
                'reason': 'Invented reviewer reasoning, distinct from the attendance record.',
                'uncertainty': 'Tentative; documentary context alone is insufficient.', 'citation_keys': [first]}

    def test_documentary_people_filters_text_and_does_not_assign_names(self):
        from .people import candidates
        self.prepare_people_case()
        case = snapshot()[0]['cases'][-1]
        self.assertEqual([p['key'] for p in candidates(case, 'workshop 1967 assembly')], ['ada'])
        self.assertEqual([p['key'] for p in candidates(case, 'barta')], ['berta'])
        self.assertEqual(candidates(case, 'unmentioned'), [])
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.get('/cases/people/people/')
        for text in ['Why this person is a candidate', 'Documented portrait attribution',
                     'No independently documented portrait', 'No reviewer attribution has been saved']:
            self.assertContains(response, text)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())
        self.assertContains(self.client.get('/cases/people/people/?q=unknown'), 'does not establish that the person was absent')

    def test_person_attribution_add_correct_and_leave_unresolved_preserves_history(self):
        from .people import save_attribution
        data = self.prepare_people_case()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        save_attribution('people', data, 'operator')
        self.assertTrue((self.root/'records/assertions.jsonl').read_bytes().startswith(before))
        record = snapshot()[2][-1]
        self.assertEqual(record['reviewer'], 'operator')
        self.assertTrue(record['reviewed_at'])
        self.assertFalse(record['metadata_adopted'])
        detail = record['person_attribution']
        self.assertNotEqual(record['reason'], detail['candidate']['reason'])
        self.assertEqual(set(record['citation_keys']), {v['key'] for v in detail['evidence_views']})
        self.assertIn(detail['candidate']['citation_keys'][0], record['citation_keys'])
        with self.assertRaisesRegex(ReviewError, 'Records changed'):
            save_attribution('people', data, 'operator')
        data.update(revision=snapshot()[3], attribution_key=record['key'], candidate_key='', review='unresolved')
        save_attribution('people', data, 'second-reviewer')
        latest = snapshot()[2][-1]
        self.assertEqual(latest['revision'], 1)
        self.assertIsNone(latest['person_attribution']['candidate'])
        self.assertEqual(latest['reviewer'], 'second-reviewer')
        self.assertEqual(snapshot()[1][-2], record)
        self.assertContains(self.client.get('/cases/people/people/'), 'Attribution history · 2 revisions')
        with self.assertRaisesRegex(ReviewError, 'person-attribution form'):
            save_review(record['key'], {**latest, 'revision': snapshot()[3]}, 'operator', case_key='people')

    def test_person_attribution_rejects_unknown_fields_and_changed_sources_without_writes(self):
        from .people import save_attribution
        data = self.prepare_people_case()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        for changes in [{'candidate_key': 'invented-unknown'}, {'candidate_key': ''}, {'position': ''},
                        {'reason': ''}, {'citation_keys': ['unknown']}, {'attribution_key': 'context'},
                        {'target_view_key': 'unknown'}]:
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                save_attribution('people', {**data, **changes}, 'operator')
        with patch('archive_research.web.records.check_sources', side_effect=ReviewError('Source changed')):
            self.assertEqual(self.client.post('/cases/people/people/', data).status_code, 409)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_person_attribution_access_and_stale_draft(self):
        from .people import save_attribution
        data = self.prepare_people_case()
        url = '/cases/people/people/'
        self.assertEqual(Client().get(url).status_code, 302)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, data).status_code, 403)
        save_attribution('people', data, 'operator')
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.post(url, {**data, 'reason': 'My retained stale draft'})
        self.assertContains(response, 'My retained stale draft', status_code=409)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())
        self.user.is_staff = False
        self.user.save()
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_person_attribution_projection_failure_is_saved_once(self):
        data = self.prepare_people_case()
        before = len(snapshot()[1])
        with patch('archive_research.web.records._project', side_effect=RuntimeError('test failure')):
            response = self.client.post('/cases/people/people/', data)
        self.assertContains(response, 'Do not submit again', status_code=503)
        self.assertEqual(len(snapshot()[1]), before+1)
        rebuild()
        self.assertEqual(self.client.post('/cases/people/people/', data).status_code, 409)
        self.assertEqual(len(snapshot()[1]), before+1)

    def test_person_attribution_read_only_and_candidate_citation_validation(self):
        from .people import save_attribution
        data = self.prepare_people_case()
        evidence = snapshot()[0]
        evidence['cases'][-1]['read_only'] = True
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        rebuild()
        with self.assertRaisesRegex(ReviewError, 'read-only'):
            save_attribution('people', {**data, 'revision': snapshot()[3]}, 'operator')
        self.assertNotContains(self.client.get('/cases/people/people/'), 'Save attribution')
        evidence['cases'][-1]['documentary_people'][0]['citation_keys'] = ['not-a-source']
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        with self.assertRaisesRegex(ReviewError, 'documentary citations'):
            snapshot()

    def prepare_comparison_case(self):
        self.prepare_people_case()
        evidence, history, current, _ = snapshot()
        selected = evidence['cases'][-1]
        first, second = [v['key'] for v in selected['views'][:2]]
        selected['reproduction_pairs'] = {'context': {'query_view_key': second, 'candidate_view_key': first}}
        history[-1].update(relation='reproduced_in', review='unreviewed', reviewer=None, reviewed_at=None,
                           value='Distinctive fixture reproduction proposal', citation_keys=[first, second])
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()
        record = snapshot()[2][-1]
        return {**{k: record[k] for k in ('value', 'scope', 'uncertainty', 'citation_keys')},
                'revision': snapshot()[3], 'review': 'rejected', 'reason': 'Different photograph.',
                'people_workspace': '1', 'mode': 'matches', 'target_view_key': first}

    def prepare_documentary_overview(self):
        self.prepare_people_case()
        evidence, history, _, _ = snapshot()
        case = evidence['cases'][-1]
        first, second = [v['key'] for v in case['views'][:2]]
        case['documentary_overview'] = {
            'event': 'Invented founding event', 'date': '1967-03-06',
            'photographs': [{'view_key': key, 'people_count': count, 'count_source': 'Fixture observer count'}
                            for key, count in [(first, 8), (second, 5)]],
            'documents': [{'view_key': key, 'label': label, 'note': 'Separate source reading',
                           'additional_mentions': ''} for key, label in [(first, 'Newspaper'), (second, 'Diary')]],
            'mentions': [{'candidate_key': 'ada', 'assertion_key': 'context', 'sources': [
                {'view_key': first, 'wording': 'Ada Example', 'qualification': 'Printed full name'}]}]}
        history[-1].update(relation='names_event_participant', review='unreviewed', reviewer=None,
                           reviewed_at=None, value='Ada Example is named as an event participant.',
                           citation_keys=[first, second])
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()
        return first, second

    def prepare_grouped_comparisons(self):
        from copy import deepcopy
        self.prepare_comparison_case()
        evidence, history, _, _ = snapshot()
        case = evidence['cases'][-1]
        first, second = [v['key'] for v in case['views'][:2]]
        third = deepcopy(case['views'][0])
        third.update(key='another-candidate', label='Another invented reproduction', region_xywh=[0, 0, .5, .5])
        case['views'].append(third)
        case['reproduction_pairs'] = {
            key: {'query_view_key': second, 'candidate_view_key': view}
            for key, view in [('pair-a', first), ('pair-b', third['key'])]}
        original = deepcopy(history[-1])
        history[-1]['component_assertions'] = ['pair-a', 'pair-b']
        history.extend([{**original, 'key': key, 'citation_keys': [second, view]}
                        for key, view in [('pair-a', first), ('pair-b', third['key'])]])
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()

    def prepare_finding_views(self):
        from copy import deepcopy
        self.prepare_people_case()
        evidence, history, _, _ = snapshot()
        case = evidence['cases'][-1]
        first, second = [v['key'] for v in case['views'][:2]]
        full = deepcopy(case['views'][0])
        full.update(key='full-source-page', label='Full source page', region_xywh=[0, 0, 1, 1], preview_crop=False)
        case['views'].append(full)
        case['finding_views'] = {
            'context': {'question': 'Do these documents concern the same event?',
                        'source_view': first, 'evidence_view': second,
                        'source_context': [full['key']], 'evidence_context': []},
            'other-question': {'question': 'What names does this passage document?',
                               'source_view': second, 'evidence_view': first,
                               'source_context': [], 'evidence_context': [full['key']]}}
        history[-1].update(citation_keys=[first, second], review='unreviewed', reviewer=None, reviewed_at=None)
        history.append({**history[-1], 'key': 'other-question'})
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()
        return first, second

    def test_question_selects_fixed_sources_and_full_page_inspection(self):
        first, second = self.prepare_finding_views()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        for key, left, right in [('context', first, second), ('other-question', second, first)]:
            response = self.client.get('/cases/people/', {'assertion': key})
            self.assertEqual(response.context['assertion']['key'], key)
            focus = response.context['finding_focus']
            self.assertEqual((focus['anchor']['key'], focus['evidence']['key']), (left, right))
            self.assertContains(response, focus['question'])
            self.assertNotContains(response, 'id="anchor-select"')
            self.assertNotContains(response, 'id="evidence-select"')
            self.assertNotContains(response, 'aria-label="Choose comparison evidence"')
            self.assertContains(response, 'Full page')
            self.assertContains(response, 'data-inspect-index=')
            self.assertNotContains(response, 'data-view="2"')
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_fixed_question_save_and_stale_draft_keep_finding_binding(self):
        self.prepare_finding_views()
        row = next(r for r in snapshot()[2] if r.get('case_key') == 'people' and r['key'] == 'context')
        data = {**{k: row[k] for k in ('value', 'scope', 'uncertainty', 'citation_keys')},
                'workspace': '1', 'revision': snapshot()[3], 'review': 'accepted', 'reason': 'Checked event evidence.'}
        response = self.client.post('/cases/people/review/context/', data)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.url)
        self.assertEqual(page.context['assertion']['key'], 'context')
        self.assertEqual(page.context['finding_focus']['source_view'], 'photo-view')
        other = next(r for r in snapshot()[2] if r.get('case_key') == 'people' and r['key'] == 'other-question')
        self.assertEqual(other['review'], 'unreviewed')
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.post('/cases/people/review/context/', {**data, 'reason': 'Keep my stale draft'})
        self.assertContains(response, 'Keep my stale draft', status_code=409)
        self.assertEqual(response.context['finding_focus']['source_view'], 'photo-view')
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_finding_inspection_rejects_unrelated_or_uncited_sources(self):
        from copy import deepcopy
        from .records import validate_records
        self.prepare_finding_views()
        evidence = snapshot()[0]
        history = (self.root/'records/assertions.jsonl').read_bytes()
        for field, value, message in [('source_view', 'absent', 'Unknown finding'),
                                      ('source_view', 'patent-passage', 'cited source page'),
                                      ('source_context', ['report-figure'], 'fixed source page')]:
            with self.subTest(field=field, value=value):
                changed = deepcopy(evidence)
                changed['cases'][-1]['finding_views']['context'][field] = value
                with self.assertRaisesRegex(ReviewError, message):
                    validate_records(json.dumps(changed).encode(), history)

    def test_fixed_question_rejects_arbitrary_report_page(self):
        from .records import validate_records
        self.prepare_finding_views()
        evidence = snapshot()[0]
        evidence['cases'][-1]['finding_views']['context']['evidence_pages'] = ['patent-passage']
        with self.assertRaisesRegex(ReviewError, 'Report navigation'):
            validate_records(json.dumps(evidence).encode(), (self.root/'records/assertions.jsonl').read_bytes())

    def test_fixed_reproduction_question_preserves_pair_and_hides_other_candidates(self):
        self.prepare_grouped_comparisons()
        evidence = snapshot()[0]
        case = evidence['cases'][-1]
        case['finding_views'] = {
            key: {'question': 'Is this photograph reproduced here?',
                  'source_view': pair['query_view_key'], 'evidence_view': pair['candidate_view_key'],
                  'source_context': [], 'evidence_context': []}
            for key, pair in case['reproduction_pairs'].items()}
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        rebuild()
        response = self.client.get('/cases/people/')
        self.assertEqual(response.context['assertion']['key'], 'pair-a')
        self.assertEqual([c['key'] for c in response.context['comparison']['candidates']], ['pair-a'])
        self.assertEqual([a['key'] for a in response.context['finding_links']], ['pair-a', 'pair-b'])
        self.assertContains(response, 'data-fixed-comparison="true"')
        self.assertNotContains(response, 'id="evidence-select"')

    def test_fixed_report_question_allows_only_its_cited_pages(self):
        from copy import deepcopy
        first, second = self.prepare_finding_views()
        evidence, history, _, _ = snapshot()
        case = evidence['cases'][-1]
        original = next(v for v in case['views'] if v['key'] == second)
        original['source'] = {**original['source'], 'path': 'report.pdf'}
        original['page_sequence'] = 1
        original['page_count'] = 2
        page = deepcopy(original)
        page.update(key='report-page-two', page_sequence=2)
        case['views'].append(page)
        case['finding_views']['context']['evidence_pages'] = [page['key']]
        for row in history:
            if row.get('case_key') == 'people' and row['key'] == 'context':
                row['citation_keys'].append(page['key'])
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()
        response = self.client.get('/cases/people/', {'assertion': 'context'})
        self.assertContains(response, 'id="report-page-select"')
        self.assertNotContains(response, 'id="anchor-select"')
        self.assertEqual([v['key'] for v in response.context['finding_focus']['evidence_views']], [second, page['key']])

    def test_retired_case_redirect_preserves_historical_citations(self):
        self.prepare_finding_views()
        evidence = snapshot()[0]
        evidence['cases'][-1].update(read_only=True, superseded_by='photo')
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        rebuild()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.get('/cases/people/')
        self.assertRedirects(response, '/cases/photo/')
        historical = self.client.get('/cases/people/', {'revision': snapshot()[3]})
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_fixed_view_script_switches_only_local_inspection_and_reading(self):
        import shutil
        if not shutil.which('node'):
            self.skipTest('Node is required for the workspace interaction check')
        script = Path(__file__).with_name('templates').joinpath('research_web/workspace_script.html').read_text()
        script = script.removeprefix('<script>').strip().removesuffix('</script>')
        fixture = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
function element(dataset = {}, value = '') {
  return {dataset, value, hidden: false, textContent: '', attrs: {}, events: {},
    classList: {toggle() {}, contains() {return false;}},
    setAttribute(k,v) {this.attrs[k] = v;},
    addEventListener(k,fn) {this.events[k] = fn;}, querySelector() {return null;}};
}
const anchor = element({picker:'anchor'}, '0'), evidence = element({picker:'evidence'}, '2');
const panes = [0,1,2,3].map(i => element({pane:i < 2 ? 'anchor':'evidence', view:String(i)}));
const reading = [0,1,2,3].map(i => element({reading:String(i), readingSide:i < 2 ? 'anchor':'evidence'}));
const catalogue = ['anchor','evidence'].flatMap(side => [0,1,2,3].map(i => element({catalogueSide:side, catalogueView:String(i)})));
const inspection = [0,1,2,3].map(i => element({inspectSide:i < 2 ? 'anchor':'evidence', inspectIndex:String(i), label:'View '+i}));
const modes = ['single','compare'].map(mode => element({mode}));
const labels = {anchor:element(), evidence:element()};
const mark = {style:{}, hidden:true, offsetLeft:10, offsetTop:20, offsetWidth:30, offsetHeight:10};
const zoom = element({}, '100'), scroll = {clientWidth:100, clientHeight:100, scrollTo(value) {this.last=value;}};
const block = element();
const passage = element({readingTarget:'anchor', readingView:'1', x:'70', y:'10', width:'10', height:'30'});
passage.closest = () => block;
panes[1].querySelector = selector => ({'img':{naturalWidth:0}, '.viewer-scroll':scroll,
  '.text-region':mark, '[data-zoom]':zoom}[selector] || null);
const selectors = {'[data-picker="anchor"]':anchor, '[data-picker="evidence"]':evidence,
  '.workspace-viewers':element(), '[data-enlarge]':element(), '[data-toggle-queue]':element(),
  '.workspace-queue':element(), '[data-focused-label="anchor"]':labels.anchor,
  '[data-focused-label="evidence"]':labels.evidence};
const groups = {'[data-pane]':panes, '[data-reading]':reading, '[data-inspect-side]':inspection, '[data-mode]':modes, '[data-catalogue-side]':catalogue, '[data-show-text-region]':[passage]};
const root = element();
root.querySelector = selector => selectors[selector] || null;
root.querySelectorAll = selector => groups[selector] || [];
vm.runInNewContext(SCRIPT, {document:{getElementById:() => root}, window:{}, URL});
assert.deepEqual(panes.map(p => p.hidden), [false,true,false,true]);
assert.deepEqual(catalogue.filter(p => !p.hidden).map(p => p.dataset.catalogueView), ['0','2']);
inspection[1].events.click();
assert.equal(anchor.value, '1'); assert.equal(evidence.value, '2');
assert.equal(labels.anchor.textContent, 'View 1');
assert.equal(inspection[1].attrs['aria-pressed'], 'true');
passage.events.click();
assert.equal(mark.hidden, false); assert.equal(mark.style.left, '70%');
assert.equal(mark.style.top, '10%'); assert.equal(zoom.value, '200');
assert.equal(passage.attrs['aria-pressed'], 'true');
assert.equal(scroll.last.top, -25);
inspection[3].events.click();
assert.equal(evidence.value, '3'); assert.equal(anchor.value, '1');
assert.deepEqual(panes.map(p => p.hidden), [true,false,true,false]);
assert.deepEqual(reading.map(p => p.hidden), [true,false,true,false]);
assert.deepEqual(catalogue.filter(p => !p.hidden).map(p => p.dataset.catalogueView), ['1','3']);
modes[0].events.click();
assert.deepEqual(reading.map(p => p.hidden), [true,false,true,true]);
assert.deepEqual(catalogue.filter(p => !p.hidden).map(p => p.dataset.catalogueView), ['1']);
inspection[0].events.click();
assert.deepEqual(reading.map(p => p.hidden), [false,true,true,true]);
'''
        result = subprocess.run(['node'], input='const SCRIPT = '+json.dumps(script)+';\n'+fixture,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixed_source_comparisons_work_without_documentary_people_or_case_specific_code(self):
        self.prepare_grouped_comparisons()
        evidence = snapshot()[0]
        evidence['cases'][-1].pop('documentary_people')
        atomic_write(self.root/'records/evidence.json', json.dumps(evidence).encode())
        rebuild()
        response = self.client.get('/cases/people/')
        for text in ['Source · fixed reference', 'Compare with', '>Match</button>', '>Not a match</button>',
                     'Uncertain', 'Another invented reproduction', 'No reproduction has been confirmed yet']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'id="anchor-select"')
        self.assertContains(response, 'id="evidence-select"')
        self.assertContains(response, 'data-comparison-key=', count=2)
        self.assertContains(response, 'class="workspace-shell"')
        self.assertContains(response, 'class="workspace-queue"')
        self.assertContains(response, 'class="workspace-drawer"')
        self.assertNotContains(response, 'name="value"')
        self.assertContains(response, 'name="comparison_workspace"', count=2)

    def test_pair_decision_generates_finding_and_does_not_review_sibling_or_combined_claim(self):
        self.prepare_grouped_comparisons()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        data = {'comparison_workspace': '1', 'revision': snapshot()[3], 'review': 'accepted',
                'reason': 'Same scene details and crop.', 'value': 'Forged aggregate claim',
                'citation_keys': ['unknown']}
        response = self.client.post('/cases/people/review/pair-a/', data)
        self.assertEqual(response.status_code, 302)
        current = {r['key']: r for r in snapshot()[2] if r.get('case_key') == 'people'}
        self.assertEqual(current['pair-a']['review'], 'accepted')
        self.assertNotIn('Forged', current['pair-a']['value'])
        self.assertIn('also shown in', current['pair-a']['value'])
        self.assertIn('reproduction_pair', current['pair-a'])
        self.assertEqual(current['pair-b']['review'], 'unreviewed')
        self.assertEqual(current['context']['review'], 'unreviewed')
        self.assertTrue((self.root/'records/assertions.jsonl').read_bytes().startswith(before))
        data.update(revision=snapshot()[3], review='rejected')
        self.assertEqual(self.client.post('/cases/people/review/pair-b/', data).status_code, 302)
        response = self.client.get('/cases/people/')
        self.assertContains(response, 'This photograph is also shown in:')
        self.assertContains(response, 'Different photograph:')
        self.assertNotContains(response, '2 comparisons still open')
        reviewed = self.client.get('/cases/people/?state=reviewed')
        self.assertFalse(next(r for r in reviewed.context['queue'] if r['key'] == 'people')['outside_filter'])
        data.update(revision=snapshot()[3])
        with self.assertRaisesRegex(ReviewError, 'separately'):
            save_review('context', data, 'operator', case_key='people')

    def test_pair_stale_note_is_retained_and_changed_source_is_refused(self):
        self.prepare_grouped_comparisons()
        data = {'comparison_workspace': '1', 'revision': snapshot()[3], 'review': 'accepted', 'reason': ''}
        self.assertEqual(self.client.post('/cases/people/review/pair-a/', data).status_code, 302)
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.post('/cases/people/review/pair-b/', {**data, 'reason': 'My stale comparison note'})
        self.assertContains(response, 'My stale comparison note', status_code=409)
        self.assertContains(response, 'This submission was not saved', status_code=409)
        with patch('archive_research.web.records.check_sources', side_effect=ReviewError('Source changed')):
            response = self.client.post('/cases/people/review/pair-b/', {**data, 'revision': snapshot()[3]})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_candidate_dropdown_retains_each_decision_and_context_finding(self):
        self.prepare_grouped_comparisons()
        evidence, history, current, _ = snapshot()
        row = next(r for r in current if r.get('case_key') == 'people' and r['key'] == 'pair-a')
        history.append({**row, 'key': 'technical-context', 'relation': 'documented_technical_context',
                        'value': 'Independent technical context', 'revision': 0, 'supersedes_revision': None})
        atomic_write(self.root/'records/assertions.jsonl', b''.join(json.dumps(r).encode()+b'\n' for r in history))
        rebuild()
        url = '/cases/people/?assertion=technical-context'
        for key, review in [('pair-a', 'accepted'), ('pair-b', 'rejected')]:
            data = {'comparison_workspace': '1', 'return_assertion': 'technical-context',
                    'revision': snapshot()[3], 'review': review, 'reason': ''}
            response = self.client.post(f'/cases/people/review/{key}/', data)
            self.assertEqual(response.url, url+'&comparison='+key)
            page = self.client.get(response.url)
            self.assertContains(page, 'id="workspace-review"')
            self.assertEqual(page.context['assertion']['key'], 'technical-context')
            selected = next(c for c in page.context['comparison']['candidates'] if c['active'])
            self.assertEqual((selected['key'], selected['record']['review']), (key, review))
        for key, review in [('pair-a', 'accepted'), ('pair-b', 'rejected')]:
            page = self.client.get(url+'&comparison='+key)
            selected = next(c for c in page.context['comparison']['candidates'] if c['active'])
            self.assertEqual(selected['record']['review'], review)
        self.assertEqual(next(r for r in snapshot()[2] if r['key'] == 'technical-context')['review'], 'unreviewed')

    def test_event_overview_separates_document_mentions_and_photo_counts(self):
        self.prepare_documentary_overview()
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.get('/cases/people/people/')
        for text in ['One event, different records', '8 people in this photograph', '5 people in this photograph',
                     'Fixture observer count', 'Not named in this selected passage', 'Confirm name / role']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'name="position"')
        self.assertNotContains(response, 'name="candidate_key"')
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())
        self.assertContains(self.client.get('/cases/people/people/?mode=people'), 'name="position"')
        self.assertEqual(self.client.post('/cases/people/people/?mode=documents', {'mode': 'documents'}).status_code, 405)

    def test_documentary_confirmation_needs_no_position_and_keeps_stale_draft(self):
        self.prepare_documentary_overview()
        row = snapshot()[2][-1]
        data = {**{k: row[k] for k in ('value', 'scope', 'uncertainty', 'citation_keys')},
                'reason': '', 'review': 'accepted', 'revision': snapshot()[3], 'documentary_workspace': '1'}
        response = self.client.post('/cases/people/review/context/', data)
        self.assertRedirects(response, '/cases/people/people/?mode=documents#documented-names')
        current = snapshot()[2][-1]
        self.assertEqual(current['relation'], 'names_event_participant')
        self.assertEqual(current['review'], 'accepted')
        self.assertEqual(current['reviewer'], 'operator')
        self.assertNotIn('person_attribution', current)
        before = (self.root/'records/assertions.jsonl').read_bytes()
        stale = self.client.post('/cases/people/review/context/', {**data, 'reason': 'Retain source reading note'})
        self.assertContains(stale, 'Retain source reading note', status_code=409)
        self.assertContains(stale, 'name="documentary_workspace"', status_code=409)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())

    def test_overview_rejects_unbound_sources_and_counts_without_provenance(self):
        from copy import deepcopy
        from .records import validate_records
        self.prepare_documentary_overview()
        evidence = snapshot()[0]
        history = (self.root/'records/assertions.jsonl').read_bytes()
        for mutation in ['unknown_source', 'missing_count_source', 'unknown_finding', 'uncited_source']:
            changed = deepcopy(evidence)
            checked_history = history
            overview = changed['cases'][-1]['documentary_overview']
            if mutation == 'unknown_source':
                overview['mentions'][0]['sources'][0]['view_key'] = 'unbound'
            elif mutation == 'missing_count_source':
                overview['photographs'][0]['count_source'] = ''
            elif mutation == 'unknown_finding':
                overview['mentions'][0]['assertion_key'] = 'unbound'
            else:
                rows = [json.loads(line) for line in history.splitlines()]
                rows[-1]['citation_keys'] = [overview['documents'][1]['view_key']]
                checked_history = b''.join(json.dumps(row).encode()+b'\n' for row in rows)
            with self.subTest(mutation=mutation), self.assertRaises(ReviewError):
                validate_records(json.dumps(changed).encode(), checked_history)

    def test_documentary_review_cannot_remove_required_source_or_damage_history(self):
        _, second = self.prepare_documentary_overview()
        row = snapshot()[2][-1]
        data = {**{k: row[k] for k in ('value', 'scope', 'uncertainty')},
                'reason': '', 'review': 'accepted', 'revision': snapshot()[3], 'citation_keys': [second]}
        before = (self.root/'records/assertions.jsonl').read_bytes()
        response = self.client.post('/cases/people/review/context/', data)
        self.assertContains(response, 'must cite every displayed name source', status_code=409)
        self.assertEqual(before, (self.root/'records/assertions.jsonl').read_bytes())
        snapshot()

    def test_reproduction_review_updates_queue_and_correction_restores_pair(self):
        data = self.prepare_comparison_case()
        url = '/cases/people/people/?mode=matches'
        self.assertContains(self.client.get(url), 'Confirm match')
        response = self.client.post('/cases/people/review/context/', data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('mode=matches', response.url)
        latest = snapshot()[2][-1]
        self.assertEqual(set(latest['reproduction_pair']), {'query', 'candidate'})
        response = self.client.get(url)
        self.assertContains(response, '1 rejected comparison hidden')
        self.assertNotContains(response, 'Confirm match')
        self.assertContains(self.client.get(url+'&show_rejected=1'), 'Confirm match')
        stale = self.client.post('/cases/people/review/context/', {**data, 'reason': 'Retain my comparison draft'})
        self.assertContains(stale, 'Retain my comparison draft', status_code=409)
        data.update(revision=snapshot()[3], review='accepted', show_rejected='1')
        self.assertEqual(self.client.post('/cases/people/review/context/', data).status_code, 302)
        self.assertContains(self.client.get(url), 'Confirm match')
        self.assertEqual(snapshot()[2][-1]['review'], 'accepted')

    def test_save_refreshes_selected_search_without_reextracting_and_recovers_failure(self):
        import hashlib
        from types import SimpleNamespace
        from archive_research.search_projection import seal, FORMAT, query
        from archive_research.web.feedback import refresh_search
        data = self.prepare_comparison_case()
        history = self.root/'records/assertions.jsonl'
        spec = {'unit': 'fixture', 'object_id': 'history', 'representation': 'history', 'root': 'records',
                'path': 'assertions.jsonl', 'size_bytes': history.stat().st_size,
                'mtime_ns': history.stat().st_mtime_ns, 'sha256': hashlib.sha256(history.read_bytes()).hexdigest()}
        path = self.root/'cache/reviews-index.json'
        value = seal({'format': FORMAT, 'metadata_adopted': False, 'selection': {'findings': [spec]},
                      'entries': [], 'postings': {}, 'unavailable': [], 'coverage': {}, 'refreshed_at': 'fixture'})
        path.write_text(json.dumps(value))
        roots = SimpleNamespace(root=lambda name, access: self.root/('records' if name == 'records' else 'cache'))
        loader = lambda **kwargs: (roots, json.loads(path.read_text()))
        with override_settings(PROJECT={'mode': 'synthetic', 'research_index': path.name}), \
             patch('archive_research.web.extraction.search_projection', side_effect=loader):
            self.assertEqual(self.client.post('/cases/people/review/context/', data).status_code, 302)
            indexed = json.loads(path.read_text())
            self.assertEqual(indexed['review_feedback'][0]['review'], 'rejected')
            self.assertEqual(query(indexed, 'Distinctive fixture reproduction')['hits'], [])
            before = len(snapshot()[1])
            data.update(revision=snapshot()[3], review='accepted')
            with patch('archive_research.web.feedback.atomic_write', side_effect=OSError('index unavailable')):
                self.assertEqual(self.client.post('/cases/people/review/context/', data).status_code, 503)
            self.assertEqual(len(snapshot()[1]), before+1)

            rebuild()
            self.assertEqual(query(json.loads(path.read_text()), 'Distinctive fixture reproduction')['hits'][0]['review'], 'accepted')
            self.assertEqual(len(snapshot()[1]), before+1)

    def test_stale_finding_search_offers_existing_rebuild_action(self):
        from types import SimpleNamespace
        from archive_research.search_projection import seal, FORMAT
        self.prepare_comparison_case()
        spec = {'unit': 'fixture', 'object_id': 'history', 'representation': 'history', 'root': 'records',
                'path': 'assertions.jsonl', 'size_bytes': 1, 'mtime_ns': 1, 'sha256': '0'*64}
        path = self.root/'cache/stale-index.json'
        path.write_text(json.dumps(seal({'format': FORMAT, 'metadata_adopted': False,
            'selection': {'findings': [spec]}, 'entries': [], 'postings': {}, 'coverage': {}, 'unavailable': []})))
        roots = SimpleNamespace(root=lambda name, access: self.root/('records' if name == 'records' else 'cache'))
        with override_settings(PROJECT={'mode': 'synthetic', 'research_index': path.name,
                                        'extraction_roots': str(self.root/'roots.toml')}), \
             patch('archive_research.Roots.from_toml', return_value=roots):
            response = self.client.get('/research/?q=example')
        self.assertContains(response, 'Saved reviews are ahead of search', status_code=409)
        self.assertContains(response, 'Rebuild browsing', status_code=409)
        self.assertContains(response, snapshot()[3], status_code=409)

    def test_real_login_logout_and_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/login/', {'username': 'operator', 'password': 'fixture-only-password'}).status_code, 403)
        client.get('/login/')
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post('/login/', {'username': 'operator', 'password': 'fixture-only-password', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/cases/photo/').status_code, 200)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(client.post('/logout/', {'csrfmiddlewaretoken': token}).status_code, 302)
        self.assertEqual(client.get('/').status_code, 302)

    def test_nonstaff_cannot_browse(self):
        user = get_user_model().objects.create_user('reader', password='fixture-only')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/').status_code, 403)

    def test_comparison_retains_locators_history_and_lineage(self):
        response = self.client.get('/cases/photo/')
        for text in ['Page 14', 'printed 12', 'figure 3', 'Accepted', 'Rejected', 'Unresolved',
                     'Synthetic history · 7 revisions', 'one source lineage', 'B family; variant unresolved']:
            self.assertContains(response, text)
        self.assertEqual(response['Cache-Control'], 'no-store')
        response = self.client.get('/media/photo/report-figure/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b''.join(response.streaming_content).startswith(b'\x89PNG'))

    def test_generic_research_writes_still_blocked_even_for_superuser(self):
        for url in ['/', '/cases/photo/', '/media/photo/photo-view/']:
            self.assertEqual(self.client.post(url, {}).status_code, 405)
        for url in ['/admin/research_web/researchcase/add/',
                    '/admin/research_web/researchcase/photo/change/',
                    '/admin/research_web/researchcase/photo/delete/']:
            self.assertEqual(self.client.post(url, {'title': 'injected'}).status_code, 403)
        response = self.client.get('/admin/research_web/researchcase/photo/change/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        self.assertNotContains(response, 'deletelink')

    def test_stale_projection_withheld_and_rebuild_recovers(self):
        path = self.root / 'records/evidence.json'
        evidence = json.loads(path.read_text())
        evidence['cases'][0]['title'] = 'Revised synthetic wording'
        atomic_write(path, json.dumps(evidence).encode())
        self.assertEqual(self.client.get('/').status_code, 409)
        self.assertEqual(self.client.get('/media/photo/report-figure/').status_code, 409)
        self.assertEqual(self.client.get('/admin/research_web/researchcase/').status_code, 403)
        rebuild()
        self.assertContains(self.client.get('/'), 'Revised synthetic wording')

    def test_changed_source_no_fallback(self):
        record = ResearchCase.objects.get()
        view = record.payload['views'][1]
        view['source']['size_bytes'] += 1
        record.save()
        self.assertContains(self.client.get('/cases/photo/'), 'Source unavailable')
        self.assertEqual(self.client.get('/media/photo/report-figure/').status_code, 409)

    def test_source_path_escape_and_symlink_refused(self):
        for path in ['/etc/passwd', '../machine.toml']:
            with self.assertRaises(ReviewError):
                bound_bytes({'path': path})
        link = self.root / 'sample/redirect.png'
        link.symlink_to(self.root / 'records/evidence.json')
        try:
            with self.assertRaises(ReviewError):
                bound_bytes({'path': 'redirect.png'})
        finally:
            link.unlink()

    def test_missing_source_remains_unavailable(self):
        record = ResearchCase.objects.get()
        record.payload['views'][2]['source']['path'] = 'missing-patent.pdf'
        record.save()
        self.assertContains(self.client.get('/cases/photo/'), 'Source unavailable')
        self.assertEqual(self.client.get('/media/photo/patent-passage/').status_code, 409)

    def test_rebuild_rollback_does_not_destroy_old_projection(self):
        original = list(ResearchCase.objects.values())
        with patch('django.db.models.query.QuerySet.delete', side_effect=RuntimeError('projection failure')):
            with self.assertRaises(RuntimeError):
                rebuild()
        self.assertEqual(list(ResearchCase.objects.values()), original)

    def test_rebuild_from_files_preserves_seven_revisions(self):
        original = ResearchCase.objects.get().payload
        ResearchCase.objects.all().delete()
        with self.assertRaises(ReviewError):
            checked_cases()
        rebuild()
        self.assertEqual(ResearchCase.objects.get().payload, original)
        self.assertEqual(len(original['history']), 7)
        self.assertIs(original['metadata_adopted'], False)

    def test_snapshot_revision_uses_captured_bytes(self):
        from . import records
        original_read = records.read_bytes
        captured = []
        def read(path):
            data = original_read(path)
            captured.append(data)
            if path.name == 'evidence.json':
                # Replacement after capture must not produce a newer token for old text.
                atomic_write(path, data + b' ')
            return data
        with patch.object(records, 'read_bytes', side_effect=read):
            evidence, history, current, token = snapshot()
        self.assertEqual(token, content_revision(*captured))
        self.assertEqual(evidence['cases'][0]['title'], 'Apparatus, report and patent')

    def test_adopted_or_real_data_refused(self):
        path = self.root / 'records/evidence.json'
        data = json.loads(path.read_text())
        data['synthetic'] = False
        atomic_write(path, json.dumps(data).encode())
        self.assertEqual(self.client.get('/').status_code, 409)

    def review_data(self):
        evidence, history, current, revision = snapshot()
        return {**current[0], 'revision': revision, 'value': 'B family; corrected synthetic wording',
                'reason': 'Synthetic correction: the illustration supports the family only.'}

    def test_review_and_correction_keep_prior_bytes_and_authenticated_actor(self):
        original = self.original_assertions
        data = self.review_data()
        data.update(reviewer='forged', metadata_adopted=True, subject='forged', relation='forged')
        url = '/cases/photo/review/photo-type/'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertTrue((self.root / 'records/assertions.jsonl').read_bytes().startswith(original))
        history = snapshot()[1]
        self.assertEqual(len(history), 8)
        record = history[-1]
        self.assertEqual((record['reviewer'], record['revision'], record['supersedes_revision']), ('operator', 3, 2))
        self.assertEqual((record['subject'], record['relation']), ('photo', 'depicts_type'))
        self.assertFalse(record['metadata_adopted'])
        self.assertEqual(record['based_on_revision'], data['revision'])
        self.assertTrue(record['reviewed_at'])
        data = self.review_data()
        data.update(review='unresolved', value='Family remains uncertain', reason='Synthetic second correction.')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(snapshot()[1][-2], record)
        self.assertEqual(snapshot()[1][-1]['supersedes_revision'], 3)
        self.assertEqual(checked_cases()[0].payload['history'], snapshot()[1])

    def test_stale_second_form_does_not_overwrite_and_preserves_entered_text(self):
        data = self.review_data()
        url = '/cases/photo/review/photo-type/'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        before = (self.root / 'records/assertions.jsonl').read_bytes()
        data['value'] = 'Unsent stale wording'
        response = self.client.post(url, data)
        self.assertContains(response, 'Records changed', status_code=409)
        self.assertContains(response, 'Unsent stale wording', status_code=409)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), before)

    def test_changed_source_blocks_review_and_export_without_writing(self):
        data = self.review_data()
        path = self.root / 'sample/report-figure.pdf'
        st = path.stat()
        try:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
            response = self.client.post('/cases/photo/review/photo-type/', data)
            self.assertContains(response, 'Source changed', status_code=409)
            self.assertEqual(self.client.get('/export/').status_code, 409)
            self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        finally:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_invalid_review_data_cannot_write(self):
        data = self.review_data()
        for changes in [{'reason': 'x'*3001}, {'value': 'x'*501}, {'citation_keys': ['unknown']},
                        {'citation_keys': []}, {'review': 'adopted'}]:
            response = self.client.post('/cases/photo/review/photo-type/', {**data, **changes})
            self.assertEqual(response.status_code, 400)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)

    def test_optional_reviewer_note_preserves_prior_history_without_invented_reason(self):
        for note in ('', '   '):
            data = self.review_data()
            data.update(reason=note, workspace='1')
            response = self.client.post('/cases/photo/review/photo-type/', data)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(snapshot()[1][-1]['reason'], '')
            self.assertTrue((self.root / 'records/assertions.jsonl').read_bytes().startswith(self.original_assertions))
        data = self.review_data()
        data.pop('reason')
        save_review('photo-type', data, 'operator')
        self.assertEqual(snapshot()[1][-1]['reason'], '')
        self.assertEqual(checked_cases()[0].payload['history'], snapshot()[1])
        self.assertEqual(self.client.get('/export/').status_code, 200)

    def test_workspace_has_inline_form_and_bound_text_without_record_writes(self):
        from .processing import binding
        view = snapshot()[0]['cases'][0]['views'][1]
        path = self.root / 'cache' / (view['key'] + '-ocr.json')
        atomic_write(path, json.dumps({'binding': binding(view), 'status': 'ready',
                                      'text': 'OCR evidence <unsafe>', 'note': 'Unreviewed test text'}).encode())
        self.addCleanup(path.unlink, missing_ok=True)
        response = self.client.get('/cases/photo/')
        self.assertContains(response, 'id="workspace-review"')
        self.assertContains(response, 'OCR evidence &lt;unsafe&gt;')
        self.assertContains(response, 'Reviewer note')
        self.assertFalse(response.context['form'].fields['reason'].required)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        cached = json.loads(path.read_text())
        cached['binding']['page_sequence'] += 1
        atomic_write(path, json.dumps(cached).encode())
        response = self.client.get('/cases/photo/')
        self.assertNotContains(response, 'OCR evidence')
        self.assertContains(response, 'No usable text is available')

    def test_workspace_existing_catalogue_is_bound_escaped_and_separate_from_findings(self):
        from archive_research.search_projection import base_entry
        from .workspace import catalogue_context
        from archive_research.search_projection import catalogue_lookup
        views = snapshot()[0]['cases'][0]['views']
        entries = []
        for i, view in enumerate(views[:2]):
            entries.append(base_entry('catalogue:' + str(i), 'catalogue',
                {'unit': 'invented', 'object_id': str(i)}, {},
                {'title': 'Recorded title '+str(i), 'description': '<script>catalogue only</script>',
                 'date_text': '1955' if i == 0 else '1956', 'creator': 'Ada Example', 'inventory': 'INV-'+str(i)},
                {'catalogue': {'root': view['source'].get('root', 'sample')},
                 'catalogue_label': 'Invented catalogue', 'representation_paths': [view['source']['path']]}))
        projection = {'entries': entries}
        lookup = catalogue_lookup(projection)
        self.assertFalse(catalogue_context({'source': {'root': 'wrong-root', 'path': views[0]['source']['path']}}, lookup)['available'])
        with patch('archive_research.web.workspace.text_projection', return_value=projection), \
             patch('archive_research.web.workspace.ocr_status', return_value={'status': 'not_generated'}):
            response = self.client.get('/cases/photo/')
        self.assertContains(response, 'Existing catalogue')
        self.assertContains(response, '&lt;script&gt;catalogue only&lt;/script&gt;')
        self.assertNotContains(response, '<script>catalogue only</script>')
        displayed = response.context['views']
        self.assertEqual(displayed[0]['catalogue']['dates'], ['1955'])
        self.assertEqual(displayed[1]['catalogue']['dates'], ['1956'])
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        self.assertEqual((self.root / 'records/evidence.json').read_bytes(), self.original_evidence)
        with patch('archive_research.web.workspace.text_projection', return_value=projection), \
             patch('archive_research.web.workspace.check_binding', side_effect=ReviewError('Offline source')):
            response = self.client.get('/cases/photo/')
        self.assertFalse(response.context['views'][0]['available'])
        self.assertTrue(response.context['views'][0]['catalogue']['available'])

    def test_catalogue_context_retains_conflicting_and_historical_descriptions(self):
        from archive_research.search_projection import base_entry, catalogue_lookup
        from .workspace import catalogue_context
        entries = [base_entry('catalogue:'+str(i), 'catalogue', {'unit': 'invented', 'object_id': 'same'}, {},
                   {'description': description, 'historical_description': 'Earlier bear caption', 'inventory': 'INV-1'},
                   {'catalogue': {'root': 'archive'}, 'catalogue_label': 'Invented catalogue',
                    'representation_paths': ['units/invented/masters/photo.tif']})
                   for i, description in enumerate(['Bear being weighed', 'Desk and monitors'])]
        context = catalogue_context({'source': {'root': 'archive', 'path': 'units/invented/masters/photo.tif'}},
                                    catalogue_lookup({'entries': entries}))
        self.assertTrue(context['conflicting_fields'])
        self.assertFalse(context['ambiguous_object'])
        rows = {row['key']: row for row in context['rows']}
        self.assertEqual(len(rows['description']['values']), 2)
        self.assertEqual(rows['historical_description']['values'][0]['text'], 'Earlier bear caption')

    def test_workspace_invalid_and_stale_submissions_keep_draft_in_comparison(self):
        url = '/cases/photo/review/photo-type/'
        data = {**self.review_data(), 'workspace': '1', 'reason': '', 'q': 'apparatus', 'state': 'all'}
        response = self.client.post(url, {**data, 'citation_keys': []})
        self.assertContains(response, 'id="workspace-review"', status_code=400)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        saved = (self.root / 'records/assertions.jsonl').read_bytes()
        response = self.client.post(url, {**data, 'value': 'Keep this unsaved workspace finding'})
        self.assertContains(response, 'Records changed', status_code=409)
        self.assertContains(response, 'Keep this unsaved workspace finding', status_code=409)
        self.assertContains(response, 'id="workspace-review"', status_code=409)
        self.assertEqual(response.context['queue_query'], 'apparatus')
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), saved)

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_workspace_next_stays_in_filtered_real_queue_and_never_accepts_redirect_target(self):
        self.add_local_case()
        evidence, history, current, token = snapshot()
        second = {**evidence['cases'][-1], 'key': 'P02', 'title': 'Local second review'}
        evidence['cases'].append(second)
        record = {**current[-1], 'case_key': 'P02', 'key': 'P02-assessment'}
        atomic_write(self.root / 'records/evidence.json', json.dumps(evidence).encode())
        path = self.root / 'records/assertions.jsonl'
        atomic_write(path, path.read_bytes() + json.dumps(record).encode() + b'\n')
        rebuild()
        response = self.client.get('/cases/P01/?q=local&state=open')
        self.assertEqual([c['key'] for c in response.context['queue']], ['P01', 'P02'])
        original = next(r for r in snapshot()[2] if r.get('case_key') == 'P01')
        data = {**{k: original[k] for k in ('value', 'scope', 'uncertainty', 'citation_keys')},
                'revision': snapshot()[3], 'review': 'accepted', 'reason': '',
                'workspace': '1', 'advance': '1', 'q': 'local', 'state': 'open', 'next': 'https://invalid.example/'}
        response = self.client.post('/cases/P01/review/P01-assessment/', data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/cases/P02/?q=local&state=open')
        self.assertEqual(snapshot()[1][-1]['review'], 'accepted')
        response = self.client.get('/cases/P01/?q=does-not-match&state=open')
        self.assertEqual([c['key'] for c in response.context['queue']], ['P01'])
        self.assertTrue(response.context['queue'][0]['outside_filter'])

    def test_workspace_projection_failure_reports_saved_without_advancing(self):
        with patch('archive_research.web.records._project', side_effect=RuntimeError('test projection failure')):
            with self.assertLogs('archive_research.web.records', level='ERROR'):
                response = self.client.post('/cases/photo/review/photo-type/', {
                    **self.review_data(), 'workspace': '1', 'advance': '1', 'reason': ''})
        self.assertContains(response, 'Review saved; browsing needs rebuilding', status_code=503)
        self.assertEqual(len(snapshot()[1]), 8)
        self.assertEqual(snapshot()[1][-1]['reason'], '')

    def test_workspace_indexed_text_requires_exact_page_source_binding(self):
        from .workspace import prepared_text
        view = snapshot()[0]['cases'][0]['views'][1]
        source = {**view['source'], 'root': 'sample'}
        entry = {'kind': 'extraction', 'extractor': 'pdf-text', 'key': 'a'*64, 'availability': 'available',
                 'evidence': {'page': {'kind': 'pdf', 'index': view['page_sequence'], 'source': source}, 'region': None},
                 'fields': {'text': 'Bound native text', 'positioned_lines': 'Bound native text'}}
        projection = {'entries': [entry], 'projection_sha256': 'b'*64}
        with patch('archive_research.web.workspace.ocr_status', return_value={'status': 'not_generated'}):
            self.assertEqual(prepared_text(view, projection)['text'], 'Bound native text')
            self.assertEqual(prepared_text(view, projection)['label'], 'Extracted PDF text')
            entry['evidence']['page']['index'] += 1
            self.assertEqual(prepared_text(view, projection)['status'], 'not_generated')
            entry['evidence']['page']['index'] -= 1
            entry['evidence']['page']['source']['mtime_ns'] += 1
            self.assertEqual(prepared_text(view, projection)['status'], 'not_generated')

    def test_workspace_tiff_text_does_not_borrow_another_frame(self):
        from .workspace import prepared_text
        view = snapshot()[0]['cases'][0]['views'][0]
        entry = {'kind': 'extraction', 'extractor': 'ocr', 'key': 'a'*64, 'availability': 'available',
                 'evidence': {'page': {'kind': 'tiff', 'index': 0, 'source': {**view['source'], 'root': 'sample'}}, 'region': None},
                 'fields': {'text': 'Newspaper caption'}}
        projection = {'entries': [entry], 'projection_sha256': 'b'*64}
        with patch('archive_research.web.workspace.ocr_status', return_value={'status': 'not_generated'}):
            self.assertEqual(prepared_text(view, projection)['text'], 'Newspaper caption')
            entry['evidence']['page']['index'] = 1
            self.assertEqual(prepared_text(view, projection)['status'], 'not_generated')
            entry['evidence']['page']['index'] -= 1
            entry['evidence']['page']['source']['mtime_ns'] += 1
            self.assertEqual(prepared_text(view, projection)['status'], 'not_generated')

    def test_workspace_assembled_text_keeps_blocks_and_manual_cache_priority(self):
        from .workspace import prepared_text
        view=snapshot()[0]['cases'][0]['views'][1]
        entry={'kind':'extraction','extractor':'document-text','key':'a'*64,'availability':'available',
               'evidence':{'page':{'kind':'pdf','index':view['page_sequence'],
                                  'source':{**view['source'],'root':'sample'}},'region':None},
               'fields':{'text':'Title\n\nBody','positioned_lines':'Title\n\nBody'}}
        projection={'entries':[entry],'projection_sha256':'b'*64}
        with patch('archive_research.web.workspace.ocr_status',return_value={'status':'not_generated'}):
            text=prepared_text(view,projection)
            self.assertEqual(text['label'],'Layout-guided OCR')
            self.assertEqual(text['text'],'Title\n\nBody')
            entry['evidence']['text_blocks']=[{'index':0,'text':'Caption <check>',
                'region':{'x':.1,'y':.2,'width':.3,'height':.1}}]
            rotated={**view,'rotation':90,'preview_crop':False}
            block=prepared_text(rotated,projection)['blocks'][0]
            self.assertAlmostEqual(block['display_region']['x'],70)
            self.assertAlmostEqual(block['display_region']['y'],10)
            self.assertEqual(prepared_text({**rotated,'preview_crop':True},projection)['blocks'],[])
            with patch('archive_research.web.workspace.text_projection',return_value=projection):
                response=self.client.get('/cases/photo/')
            self.assertContains(response,'data-show-text-region')
            self.assertContains(response,'data-reading-view="1"')
            self.assertContains(response,'Caption &lt;check&gt;')
            self.assertContains(response,'data-reading-side="anchor"')
            self.assertContains(response,'data-reading-side="evidence"')
        saved={'status':'ready','text':'Existing operator-side OCR cache'}
        with patch('archive_research.web.workspace.ocr_status',return_value=saved):
            self.assertEqual(prepared_text(view,projection),saved)

    def test_reused_page_uses_selected_blocks_over_older_index_derived_cache(self):
        from .workspace import prepared_text
        view=snapshot()[0]['cases'][0]['views'][1]
        evidence={'page':{'kind':'pdf','index':view['page_sequence'],
                         'source':{**view['source'],'root':'sample'}},'region':None}
        old={'kind':'extraction','extractor':'ocr','key':'a'*64,'availability':'available',
             'evidence':evidence,'fields':{'text':'Interleaved old columns'}}
        selected={**old,'extractor':'document-text','key':'b'*64,
                  'evidence':{**evidence,'text_blocks':[{'index':0,'text':'Separated caption',
                      'region':{'x':.1,'y':.2,'width':.3,'height':.1}}]},
                  'fields':{'text':'Separated caption'}}
        projection={'entries':[old,selected],'projection_sha256':'c'*64}
        cache={'status':'ready','text':'Interleaved old columns','extraction_key':old['key']}
        with patch('archive_research.web.workspace.ocr_status',return_value=cache):
            shown=prepared_text(view,projection)
            self.assertEqual(shown['text'],'Separated caption')
            self.assertEqual(len(shown['blocks']),1)
            self.assertEqual(prepared_text(view,None),cache)
            self.assertEqual(prepared_text({**view,'preview_crop':True},projection),cache)
            self.assertEqual(prepared_text(view,{**projection,'entries':[]}),cache)
        with patch('archive_research.web.workspace.ocr_status',return_value={'status':'not_generated'}):
            self.assertEqual(prepared_text({**view,'key':'another-case-same-page'},projection),shown)

    def test_write_permissions_methods_and_csrf(self):
        data = self.review_data()
        url = '/cases/photo/review/photo-type/'
        anon = Client()
        for endpoint in [url, '/rebuild/', '/export/']:
            self.assertEqual(anon.post(endpoint, data).status_code, 302)
        reader = get_user_model().objects.create_user('nonstaff')
        self.client.force_login(reader)
        for endpoint in [url, '/rebuild/']:
            self.assertEqual(self.client.post(endpoint, data).status_code, 403)
        self.assertEqual(self.client.get('/export/').status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for endpoint in [url, '/rebuild/']:
            self.assertEqual(client.post(endpoint, data).status_code, 403)
        client.get(url)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(client.post(url, {**data, 'csrfmiddlewaretoken': token}).status_code, 302)
        self.assertEqual(client.get('/rebuild/').status_code, 405)
        self.assertEqual(client.post('/export/', {'csrfmiddlewaretoken': token}).status_code, 405)

    def test_saved_but_stale_recovery_never_repeats_review(self):
        data = self.review_data()
        with patch('archive_research.web.records._project', side_effect=RuntimeError('injected projection failure')):
            with self.assertLogs('archive_research.web.records', level='ERROR'):
                response = self.client.post('/cases/photo/review/photo-type/', data)
        self.assertContains(response, 'Review saved; browsing needs rebuilding', status_code=503)
        saved = (self.root / 'records/assertions.jsonl').read_bytes()
        self.assertEqual(len(snapshot()[1]), 8)
        self.assertEqual(self.client.get('/cases/photo/').status_code, 409)
        self.assertEqual(self.client.get('/export/').status_code, 409)
        self.assertEqual(self.client.post('/cases/photo/review/photo-type/', data).status_code, 409)
        self.assertEqual(self.client.post('/rebuild/', {'revision': data['revision']}).status_code, 409)
        token = snapshot()[3]
        self.assertEqual(self.client.post('/rebuild/', {'revision': token}).status_code, 302)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), saved)
        self.assertEqual(self.client.get('/cases/photo/').status_code, 200)
        self.assertEqual(checked_cases()[0].payload['history'], snapshot()[1])

    def test_authority_write_failure_leaves_history_and_projection_unchanged(self):
        previous = ResearchCase.objects.get().payload
        with patch('archive_research.web.records.atomic_write', side_effect=OSError('injected write failure')):
            response = self.client.post('/cases/photo/review/photo-type/', self.review_data())
        self.assertContains(response, 'This submission was not saved', status_code=409)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        self.assertEqual(ResearchCase.objects.get().payload, previous)

    def test_post_replace_confirmation_failure_reports_saved(self):
        def replaced_then_failed(path, data):
            atomic_write(path, data)
            raise OSError('injected directory sync failure')
        with patch('archive_research.web.records.atomic_write', side_effect=replaced_then_failed):
            with self.assertLogs('archive_research.web.records', level='ERROR'):
                result = save_review('photo-type', self.review_data(), 'operator')
        self.assertFalse(result['indexed'])
        self.assertEqual(len(snapshot()[1]), 8)
        rebuild()
        self.assertEqual(len(checked_cases()[0].payload['history']), 8)

    def test_rebuild_and_save_hold_lock_through_projection(self):
        from . import records
        project = records._project
        def check_lock(captured):
            result = subprocess.run([sys.executable, '-c',
                'import fcntl,sys; f=open(sys.argv[1],"a+"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
                str(self.root / 'records/.research.lock')], capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'BlockingIOError', result.stderr)
            return project(captured)
        with patch.object(records, '_project', side_effect=check_lock) as mocked:
            rebuild()
            save_review('photo-type', self.review_data(), 'operator')
            self.assertEqual(mocked.call_count, 2)

    def test_export_and_rebuild_agree_with_full_unadopted_history(self):
        save_review('photo-type', self.review_data(), 'operator')
        before = export_bundle()
        ResearchCase.objects.all().delete()
        rebuild()
        self.assertEqual(export_bundle(), before)
        response = self.client.get('/export/')
        self.assertEqual(response.content, before)
        self.assertIn('attachment;', response['Content-Disposition'])
        bundle = json.loads(before)
        self.assertFalse(bundle['metadata_adopted'])
        self.assertFalse(bundle['accession_authorized'])
        self.assertEqual(bundle['evidence'], snapshot()[0])
        self.assertEqual(bundle['assertion_history'], snapshot()[1])
        self.assertEqual(bundle['current'], snapshot()[2])
        self.assertEqual({r['review'] for r in bundle['current']}, {'accepted', 'rejected', 'unresolved'})
        self.assertIn('source images and PDFs', bundle['omitted'])
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes().count(b'\n'), 8)

    def test_citation_navigation_retains_page_label_region_and_source(self):
        token = snapshot()[3]
        response = self.client.get('/cases/photo/citations/report-figure/', {'revision': token})
        for text in ['Page 14 of 14', 'printed 12', 'figure 3',
                     'left:10.000%;top:20.000%;width:70.000%;height:50.000%',
                     f'/sources/photo/report-figure/?revision={token}#page=14',
                     f'/media/photo/report-figure/?revision={token}', 'Current assertions citing this view']:
            self.assertContains(response, text)
        for key in ['photo-type', 'alternative-type', 'type-patent']:
            self.assertContains(response, f'Review or correct {key}')
        response = self.client.get('/sources/photo/report-figure/', {'revision': token})
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response['Content-Disposition'].startswith('inline;'))
        self.assertEqual(b''.join(response.streaming_content), (self.root / 'sample/report-figure.pdf').read_bytes())
        self.assertEqual(response['Cache-Control'], 'no-store')
        response = self.client.get('/sources/photo/photo-view/', {'revision': token})
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(b''.join(response.streaming_content), (self.root / 'sample/photo-view.png').read_bytes())

    def test_review_and_history_link_to_citations_without_modifying_records(self):
        before = export_bundle()
        token = snapshot()[3]
        for url in ['/cases/photo/', '/cases/photo/review/photo-type/']:
            response = self.client.get(url)
            for key in ['photo-view', 'report-figure', 'patent-passage']:
                self.assertContains(response, f'/cases/photo/citations/{key}/?revision={token}')
        response = self.client.get('/cases/photo/')
        self.assertContains(response, 'Citations open the current bound evidence.')
        self.assertEqual(export_bundle(), before)

    def test_stale_citation_links_never_serve_newer_evidence(self):
        token = snapshot()[3]
        save_review('photo-type', self.review_data(), 'operator')
        for url in ['/cases/photo/citations/report-figure/', '/sources/photo/report-figure/',
                    '/media/photo/report-figure/', '/cases/photo/']:
            response = self.client.get(url, {'revision': token})
            self.assertContains(response, 'earlier review snapshot', status_code=409)
            self.assertEqual(self.client.get(url, {'revision': snapshot()[3]}).status_code, 200)

    def test_citation_source_and_preview_fail_closed(self):
        source = self.root / 'sample/report-figure.pdf'
        st = source.stat()
        try:
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
            for url in ['/cases/photo/citations/report-figure/', '/sources/photo/report-figure/']:
                self.assertEqual(self.client.get(url).status_code, 409)
        finally:
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
        record = ResearchCase.objects.get()
        record.payload['views'][1]['preview']['path'] = 'missing-preview.png'
        record.save()
        self.assertEqual(self.client.get('/cases/photo/citations/report-figure/').status_code, 409)

    def test_citation_endpoints_require_operator_and_refuse_writes_unknown_paths(self):
        urls = ['/cases/photo/citations/report-figure/', '/sources/photo/report-figure/']
        for url in urls:
            self.assertEqual(Client().get(url).status_code, 302)
            self.assertEqual(self.client.post(url, {}).status_code, 405)
        for url in ['/sources/photo/unknown/', '/sources/unknown/report-figure/',
                    '/cases/photo/citations/unknown/']:
            self.assertEqual(self.client.get(url).status_code, 404)
        reader = get_user_model().objects.create_user('nonstaff-citations')
        self.client.force_login(reader)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_citation_source_does_not_follow_arbitrary_paths_or_serve_unknown_formats(self):
        record = ResearchCase.objects.get()
        original = record.payload['views'][1]['source'].copy()
        for path in ['../machine.toml', '/etc/passwd']:
            record.payload['views'][1]['source'] = {**original, 'path': path}
            record.save()
            self.assertEqual(self.client.get('/sources/photo/report-figure/').status_code, 409)
        record.payload['views'][1]['source'] = original
        record.save()
        with patch('archive_research.web.views.bound_bytes', return_value=b'<html>unsupported</html>'):
            self.assertEqual(self.client.get('/sources/photo/report-figure/').status_code, 409)

    def test_empty_and_nonorthogonal_regions_remain_unsupported(self):
        path = self.root / 'records/evidence.json'
        for changes in [{'region_xywh': [0, 0, 0, 1]}, {'region_xywh': [0, 0, 1, 0]},
                        {'rotation': 45}]:
            evidence = json.loads(self.original_evidence)
            evidence['cases'][0]['views'][0].update(changes)
            atomic_write(path, json.dumps(evidence).encode())
            self.assertEqual(self.client.get('/cases/photo/citations/photo-view/').status_code, 409)

    def test_rotated_region_mapping_and_actual_pdf_render_dimensions(self):
        from .fixtures import pdf_bytes
        from .records import display_region
        expected = {0: (10, 20, 70, 50), 90: (30, 10, 50, 70),
                    180: (20, 30, 70, 50), 270: (20, 20, 50, 70)}
        for rotation, region in expected.items():
            actual = display_region({'region_xywh': [.1, .2, .7, .5], 'rotation': rotation})
            for a, e in zip(actual.values(), region):
                self.assertAlmostEqual(a, e)
            pdf = self.root / 'cache/rotation.pdf'
            pdf.write_bytes(pdf_bytes('Rotation fixture', 1, '1', 'Synthetic', rotation))
            result = subprocess.run(['pdftoppm', '-singlefile', '-scale-to', '600', '-png', str(pdf)],
                                    capture_output=True, check=True, timeout=10).stdout
            import struct
            width, height = struct.unpack('>II', result[16:24])
            self.assertEqual((width, height), (500, 600) if rotation in (90, 270) else (600, 500))

    def test_ocr_absence_failure_and_regeneration_preserve_citations_and_reviews(self):
        from .processing import generate_ocr, ocr_status
        save_review('photo-type', self.review_data(), 'operator')
        before = export_bundle()
        view = snapshot()[0]['cases'][0]['views'][1]
        self.assertEqual(ocr_status(view)['status'], 'not_generated')
        with override_settings(MACHINE={'tesseract': '/nonexistent/synthetic-ocr'}):
            self.assertEqual(generate_ocr('report-figure'), 'unavailable')
        self.assertContains(self.client.get('/cases/photo/citations/report-figure/'), 'OCR processor is unavailable')
        for text in ['First synthetic OCR text', 'Regenerated synthetic OCR text']:
            with patch('archive_research.web.processing.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=text)):
                self.assertEqual(generate_ocr('report-figure'), 'ready')
            response = self.client.get('/cases/photo/citations/report-figure/', {'revision': snapshot()[3]})
            self.assertContains(response, text)
            self.assertContains(response, 'Page 14 of 14')
            self.assertEqual(export_bundle(), before)
        changed = {**view, 'rotation': 90}
        self.assertEqual(ocr_status(changed)['status'], 'stale')
        for corrupt in ('broken receipt', 'null', '[]'):
            (self.root / 'cache/report-figure-ocr.json').write_text(corrupt)
            self.assertEqual(ocr_status(view)['status'], 'unavailable')
            self.assertEqual(self.client.get('/cases/photo/citations/report-figure/').status_code, 200)

    def test_unavailable_external_reference_keeps_explicit_status_and_local_page(self):
        path = self.root / 'records/evidence.json'
        evidence = json.loads(path.read_text())
        evidence['cases'][0]['views'][2]['external_reference'] = {
            'status': 'unavailable', 'note': 'Synthetic offline response; external image was not inspected.'}
        atomic_write(path, json.dumps(evidence).encode())
        rebuild()
        response = self.client.get('/cases/photo/citations/patent-passage/')
        self.assertContains(response, 'External reference unavailable')
        self.assertContains(response, 'external image was not inspected')
        self.assertContains(response, 'Page 2 of 2')
        bundle = json.loads(export_bundle())
        self.assertEqual(bundle['evidence']['cases'][0]['views'][2]['external_reference']['status'], 'unavailable')

    def test_read_only_case_blocks_get_post_and_direct_save_without_changing_history(self):
        path = self.root / 'records/evidence.json'
        evidence = json.loads(path.read_text())
        evidence['cases'][0]['read_only'] = True
        atomic_write(path, json.dumps(evidence).encode())
        rebuild()
        data = self.review_data()
        for response in [self.client.get('/cases/photo/review/photo-type/'),
                         self.client.post('/cases/photo/review/photo-type/', data)]:
            self.assertEqual(response.status_code, 403)
        with self.assertRaises(ReviewError):
            save_review('photo-type', data, 'operator')
        self.assertContains(self.client.get('/cases/photo/'), 'This case is read-only')
        self.assertNotContains(self.client.get('/cases/photo/'), 'Review or correct')
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        self.assertEqual(len(json.loads(export_bundle())['assertion_history']), 7)

    def test_protected_mount_guard_refuses_absent_wrong_and_writable_mount_before_stat(self):
        from archive_research.common import checked_member
        member = {'path': '/media/vladimir/Elements/synthetic-never-read.pdf', 'size_bytes': 1, 'mtime_ns': 1}
        responses = [subprocess.CompletedProcess([], 1, stdout=''),
                     subprocess.CompletedProcess([], 0, stdout=json.dumps({'filesystems': [{
                         'source': '/dev/sdf1', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'rw'}]})),
                     subprocess.CompletedProcess([], 0, stdout=json.dumps({'filesystems': [{
                         'source': '/dev/wrong', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'ro'}]}))]
        for response in responses:
            with patch('archive_research.common.subprocess.run', return_value=response), patch.object(Path, 'stat') as stat:
                with self.assertRaises(ReviewError):
                    checked_member(member, [Path('/media/vladimir/Elements')])
                stat.assert_not_called()
        with self.assertRaises(ReviewError):
            bound_bytes({'path': member['path'], 'size_bytes': 1, 'mtime_ns': 1})

    def test_portable_package_allowlist_and_configuration_refuse_secret_overwrite(self):
        import tarfile
        from .configure import configure
        from .portable import build_package
        (self.root / 'machine.toml').write_text('secret sentinel')
        (self.root / 'operator-login.txt').write_text('credential sentinel')
        with tarfile.open(build_package()) as package:
            names = package.getnames()
            self.assertIn('runtime/records/assertions.jsonl', names)
            self.assertIn('runtime/sample/report-figure.pdf', names)
            self.assertFalse(any(x.endswith(('machine.toml', 'operator-login.txt', '.sqlite3')) for x in names))
            data = package.extractfile('runtime/records/assertions.jsonl').read()
            self.assertEqual(data, self.original_assertions)
        configured = self.root / 'cache/configured'
        configure(configured, 'local-test')
        secret = (configured / 'machine.toml').read_bytes()
        with self.assertRaises(ReviewError):
            configure(configured, 'different')
        self.assertEqual((configured / 'machine.toml').read_bytes(), secret)
        self.assertEqual(configured.stat().st_mode & 0o077, 0)

    def add_local_case(self):
        evidence = json.loads(self.original_evidence)
        evidence['cases'][0]['synthetic'] = True
        evidence['synthetic'] = False
        local = {**evidence['cases'][0], 'key': 'P01', 'title': 'Local source review', 'synthetic': False,
                 'question': 'What does the source support?',
                 'intended_use': {'scope': 'private_local_research', 'authority': 'Explicit test authorization',
                                  'external_redistribution': False}}
        evidence['cases'].append(local)
        atomic_write(self.root / 'records/evidence.json', json.dumps(evidence).encode())
        record = json.loads(self.original_assertions.splitlines()[0])
        record.update(case_key='P01', key='P01-assessment', synthetic=False,
                      relation='research_assessment', review='unreviewed', reviewer=None, reviewed_at=None)
        atomic_write(self.root / 'records/assertions.jsonl', self.original_assertions + json.dumps(record).encode() + b'\n')
        rebuild()

    def test_mixed_local_cases_default_to_real_and_keep_demo_separate(self):
        with override_settings(PROJECT={'mode': 'local_review'}):
            self.add_local_case()
            response = self.client.get('/')
            self.assertContains(response, 'Local source review')
            self.assertNotContains(response, 'Apparatus, report and patent')
            self.assertContains(self.client.get('/?demo=1'), 'Apparatus, report and patent')
            self.assertEqual(len(checked_cases()), 2)
            self.assertEqual(self.client.get('/cases/P01/').status_code, 200)
            self.assertContains(self.client.get('/cases/P01/review/P01-assessment/'), 'Save research review')

    def test_real_review_is_scoped_and_preserves_demo_history(self):
        with override_settings(PROJECT={'mode': 'local_review'}):
            self.add_local_case()
            evidence, history, current, token = snapshot()
            record = next(r for r in current if r.get('case_key') == 'P01')
            data = {k: record[k] for k in ('value', 'scope', 'uncertainty', 'citation_keys')}
            data.update(revision=token, review='unresolved', reason='Local reviewer needs more evidence.')
            self.assertEqual(self.client.post('/cases/P01/review/photo-type/', data).status_code, 404)
            self.assertEqual(self.client.post('/cases/photo/review/P01-assessment/', data).status_code, 404)
            self.assertEqual(self.client.post('/cases/P01/review/P01-assessment/', data).status_code, 302)
            latest = snapshot()[1][-1]
            self.assertEqual(latest['case_key'], 'P01')
            self.assertFalse(latest['synthetic'])
            self.assertFalse(latest['metadata_adopted'])
            self.assertEqual(latest['reviewer'], 'operator')
            self.assertTrue((self.root / 'records/assertions.jsonl').read_bytes().startswith(self.original_assertions))
            self.assertEqual(len(ResearchCase.objects.get(key='photo').payload['history']), 7)

    def test_real_cases_cannot_enter_portable_or_generic_export(self):
        from .portable import build_package
        with override_settings(PROJECT={'mode': 'local_review'}):
            self.add_local_case()
            with self.assertRaises(ReviewError):
                build_package()
            self.assertEqual(self.client.get('/export/').status_code, 409)
            self.assertEqual(self.client.get('/export/?case=P01').status_code, 409)
            result = json.loads(self.client.get('/export/?case=photo').content)
            self.assertTrue(result['synthetic'])
            self.assertEqual([c['key'] for c in result['evidence']['cases']], ['photo'])
            self.assertEqual(len(result['assertion_history']), 7)

    def test_real_cases_require_recorded_local_use_and_explicit_mode(self):
        with override_settings(PROJECT={'mode': 'local_review'}):
            self.add_local_case()
            path = self.root / 'records/evidence.json'
            evidence = json.loads(path.read_text())
            del evidence['cases'][1]['intended_use']
            atomic_write(path, json.dumps(evidence).encode())
            self.assertEqual(self.client.get('/').status_code, 409)

    def test_external_root_binding_requires_exact_mapping_and_guard(self):
        from .records import check_binding
        binding = {'root': 'elements', 'path': 'never-read.pdf', 'size_bytes': 1, 'mtime_ns': 1}
        with override_settings(PROJECT={'mode': 'synthetic'}):
            with self.assertRaises(ReviewError):
                check_binding(binding)
        with override_settings(PROJECT={'mode': 'local_review'}, MACHINE={'source_roots': {'elements': '/media/vladimir/Elements1'}}):
            with self.assertRaises(ReviewError):
                check_binding(binding)
        with override_settings(PROJECT={'mode': 'local_review'}, MACHINE={'source_roots': {'elements': '/media/vladimir/Elements'}}):
            with patch('archive_research.common.subprocess.run', return_value=subprocess.CompletedProcess([], 1, stdout='')):
                with self.assertRaises(ReviewError):
                    check_binding(binding)

    def test_declared_unreadable_view_never_falls_back(self):
        record = ResearchCase.objects.get()
        record.payload['views'][0].update(unavailable_reason='Unverified source TIFF coverage', preview=None)
        record.save()
        self.assertContains(self.client.get('/cases/photo/'), 'Unverified source TIFF coverage')
        for url in ['/media/photo/photo-view/', '/sources/photo/photo-view/', '/cases/photo/citations/photo-view/']:
            self.assertEqual(self.client.get(url).status_code, 409)

    def physical_data(self, **changes):
        return {'revision': snapshot()[3], 'inventory_reference': 'TEST-18',
                'catalogue_url': 'https://museum.example.org/objects/18',
                'location': 'Test museum / case 4', 'location_source': 'Test inventory; checked by curator',
                'location_date': '2026-11-15', 'relationship': 'same_type',
                'reason': 'Record supplied inventory reference.', **changes}

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_physical_reference_add_correct_clear_preserves_review_and_history(self):
        self.add_local_case()
        path = self.root / 'records/assertions.jsonl'
        original = path.read_bytes()
        before = snapshot()[2][-1]
        self.assertContains(self.client.get('/cases/P01/'), 'No physical object recorded')
        self.assertEqual(self.client.post('/cases/P01/physical-object/', self.physical_data()).status_code, 302)
        self.assertTrue(path.read_bytes().startswith(original))
        saved = snapshot()[1][-1]
        for key in ['value', 'scope', 'uncertainty', 'reason', 'review', 'reviewer', 'reviewed_at', 'citation_keys']:
            self.assertEqual(saved[key], before[key])
        self.assertEqual(saved['recorded_by'], 'operator')
        self.assertEqual(saved['review'], 'unreviewed')
        self.assertFalse(saved['metadata_adopted'])
        self.assertContains(self.client.get('/cases/P01/'), 'Test museum / case 4')
        corrected = self.physical_data(location='Test store / shelf 2')
        self.assertEqual(self.client.post('/cases/P01/physical-object/', corrected).status_code, 302)
        clear = {'revision': snapshot()[3], 'reason': 'Remove a mistaken reference; preserve earlier values.'}
        self.assertEqual(self.client.post('/cases/P01/physical-object/', clear).status_code, 302)
        payload = ResearchCase.objects.get(key='P01').payload
        self.assertEqual(payload['physical_object'], {})
        self.assertEqual(payload['history'][-2]['physical_object']['location'], 'Test store / shelf 2')
        self.assertEqual(len(ResearchCase.objects.get(key='photo').payload['history']), 7)
        self.assertContains(self.client.get('/cases/P01/'), 'Physical-object reference changed')

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_physical_reference_stale_form_and_review_retention(self):
        self.add_local_case()
        old = self.physical_data()
        self.client.post('/cases/P01/physical-object/', old)
        path = self.root / 'records/assertions.jsonl'
        saved = path.read_bytes()
        response = self.client.post('/cases/P01/physical-object/', {**old, 'location': 'Unsent location'})
        self.assertContains(response, 'Unsent location', status_code=409)
        self.assertEqual(path.read_bytes(), saved)
        current = snapshot()[2][-1]
        review = {key: current[key] for key in ['value', 'scope', 'uncertainty', 'citation_keys']}
        review.update(revision=snapshot()[3], reason='Still unresolved.', review='unresolved')
        self.assertEqual(self.client.post('/cases/P01/review/P01-assessment/', review).status_code, 302)
        latest = snapshot()[1][-1]
        self.assertEqual(latest['change_kind'], 'review')
        self.assertEqual(latest['physical_object'], current['physical_object'])

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_physical_reference_invalid_values_do_not_write(self):
        from .records import save_physical_object
        self.add_local_case()
        path = self.root / 'records/assertions.jsonl'
        original = path.read_bytes()
        for bad in [{'catalogue_url': 'javascript:alert(1)'}, {'location_date': '2026-02-31'},
                    {'location': 'x' * 501}, {'relationship': 'certain'}, {'reason': ''}]:
            self.assertEqual(self.client.post('/cases/P01/physical-object/', self.physical_data(**bad)).status_code, 400)
            with self.assertRaises(ReviewError):
                save_physical_object('P01', self.physical_data(**bad), 'operator')
        self.assertEqual(path.read_bytes(), original)

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_physical_reference_access_csrf_and_read_only(self):
        self.add_local_case()
        url = '/cases/P01/physical-object/'
        self.assertEqual(Client().post(url, self.physical_data()).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(url, self.physical_data()).status_code, 403)
        user = get_user_model().objects.create_user('nonstaff')
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, self.physical_data()).status_code, 403)
        self.client.force_login(self.user)
        evidence_path = self.root / 'records/evidence.json'
        evidence = json.loads(evidence_path.read_text())
        evidence['cases'][1]['read_only'] = True
        atomic_write(evidence_path, json.dumps(evidence).encode())
        rebuild()
        self.assertEqual(self.client.post(url, self.physical_data()).status_code, 403)
        self.assertNotContains(self.client.get('/cases/P01/'), 'Add physical-object reference')
        self.assertEqual(self.client.get('/cases/photo/physical-object/').status_code, 404)

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_physical_reference_projection_recovery_does_not_repeat_save(self):
        self.add_local_case()
        with patch('archive_research.web.records._project', side_effect=RuntimeError('forced projection failure')):
            with self.assertLogs('archive_research.web.records', level='ERROR'):
                response = self.client.post('/cases/P01/physical-object/', self.physical_data())
        self.assertContains(response, 'Your physical-object change is saved', status_code=503)
        path = self.root / 'records/assertions.jsonl'
        saved = path.read_bytes()
        rebuild()
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(ResearchCase.objects.get(key='P01').payload['physical_object']['inventory_reference'], 'TEST-18')

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_empty_physical_reference_is_optional_and_noop_does_not_write(self):
        self.add_local_case()
        response = self.client.get('/cases/P01/physical-object/')
        self.assertEqual(response.status_code, 200)
        for key in ['inventory_reference', 'catalogue_url', 'location', 'location_source', 'location_date', 'relationship']:
            self.assertFalse(response.context['form'].fields[key].required)
        original = (self.root / 'records/assertions.jsonl').read_bytes()
        self.assertContains(self.client.post('/cases/P01/physical-object/', {
            'revision': snapshot()[3], 'reason': 'Leave empty.'}), 'No physical-object fields changed', status_code=409)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), original)

    def prepare_test_addition(self):
        self.add_local_case()
        evidence, history, current, revision = snapshot()
        case = {**evidence['cases'][1], 'key': 'R01'}
        proposal = {**next(r for r in current if r.get('case_key') == 'P01'),
                    'key': 'R01-link', 'subject': 'R01', 'reason': 'Specific fixture evidence.'}
        proposal = {k: proposal[k] for k in ('key', 'subject', 'relation', 'value', 'scope',
                                            'uncertainty', 'reason', 'citation_keys')}
        case['prepared_assertions'] = [proposal]
        packet = {'expected_revision': revision, 'cases': [case]}
        path = self.root / 'cache/research-additions-prepared.json'
        path.write_text(json.dumps(packet))
        return path, packet

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_sample_additions_preserve_review_history_and_refuse_replay(self):
        from .load_selection import apply_additions
        self.prepare_test_addition()
        before = (self.root / 'records/assertions.jsonl').read_bytes()
        existing = snapshot()[0]['cases']
        apply_additions()
        after = (self.root / 'records/assertions.jsonl').read_bytes()
        self.assertTrue(after.startswith(before))
        evidence, _, current, _ = snapshot()
        self.assertEqual(evidence['cases'][:len(existing)], existing)
        new = next(r for r in current if r.get('case_key') == 'R01')
        self.assertEqual(new['review'], 'unreviewed')
        self.assertIsNone(new['reviewer'])
        with self.assertRaisesRegex(ReviewError, 'Reviews changed'):
            apply_additions()
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), after)

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_invalid_sample_citations_write_neither_authority_file(self):
        from .load_selection import apply_additions
        path, packet = self.prepare_test_addition()
        packet['cases'][0]['prepared_assertions'][0]['citation_keys'] = ['unknown-source']
        path.write_text(json.dumps(packet))
        before = [(self.root / 'records' / name).read_bytes() for name in ('evidence.json', 'assertions.jsonl')]
        with self.assertRaisesRegex(ReviewError, 'Unknown or duplicate citation'):
            apply_additions()
        self.assertEqual(before, [(self.root / 'records' / name).read_bytes() for name in ('evidence.json', 'assertions.jsonl')])

    @override_settings(PROJECT={'mode': 'local_review'})
    def test_sample_preparation_refuses_existing_preview_keys_before_rendering(self):
        from .load_selection import prepare
        _, packet = self.prepare_test_addition()
        with patch('archive_research.web.load_selection.subprocess.run') as render:
            with self.assertRaisesRegex(ReviewError, 'overwrite existing previews'):
                prepare({'cases': packet['cases']}, additions=True)
            render.assert_not_called()
