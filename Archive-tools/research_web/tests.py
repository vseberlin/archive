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

from archive_common import ReviewError, atomic_write, content_revision
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
        cls.override = override_settings(PILOT_ROOT=cls.root, ALLOWED_HOSTS=['testserver', '127.0.0.1'])
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
        for changes in [{'reason': ' '}, {'value': 'x'*501}, {'citation_keys': ['unknown']},
                        {'citation_keys': []}, {'review': 'adopted'}]:
            response = self.client.post('/cases/photo/review/photo-type/', {**data, **changes})
            self.assertEqual(response.status_code, 400)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)

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
        with patch('research_web.records._project', side_effect=RuntimeError('injected projection failure')):
            with self.assertLogs('research_web.records', level='ERROR'):
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
        with patch('research_web.records.atomic_write', side_effect=OSError('injected write failure')):
            response = self.client.post('/cases/photo/review/photo-type/', self.review_data())
        self.assertContains(response, 'This submission was not saved', status_code=409)
        self.assertEqual((self.root / 'records/assertions.jsonl').read_bytes(), self.original_assertions)
        self.assertEqual(ResearchCase.objects.get().payload, previous)

    def test_post_replace_confirmation_failure_reports_saved(self):
        def replaced_then_failed(path, data):
            atomic_write(path, data)
            raise OSError('injected directory sync failure')
        with patch('research_web.records.atomic_write', side_effect=replaced_then_failed):
            with self.assertLogs('research_web.records', level='ERROR'):
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
        with patch('research_web.views.bound_bytes', return_value=b'<html>unsupported</html>'):
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
            with patch('research_web.processing.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=text)):
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
        from archive_common import checked_member
        member = {'path': '/media/vladimir/Elements/synthetic-never-read.pdf', 'size_bytes': 1, 'mtime_ns': 1}
        responses = [subprocess.CompletedProcess([], 1, stdout=''),
                     subprocess.CompletedProcess([], 0, stdout=json.dumps({'filesystems': [{
                         'source': '/dev/sdf1', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'rw'}]})),
                     subprocess.CompletedProcess([], 0, stdout=json.dumps({'filesystems': [{
                         'source': '/dev/wrong', 'target': '/media/vladimir/Elements', 'fstype': 'ntfs3', 'options': 'ro'}]}))]
        for response in responses:
            with patch('archive_common.subprocess.run', return_value=response), patch.object(Path, 'stat') as stat:
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
            with patch('archive_common.subprocess.run', return_value=subprocess.CompletedProcess([], 1, stdout='')):
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
        with patch('research_web.records._project', side_effect=RuntimeError('forced projection failure')):
            with self.assertLogs('research_web.records', level='ERROR'):
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
