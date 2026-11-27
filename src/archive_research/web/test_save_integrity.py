"""Review history outcomes and the disposable SQLite projection on invented evidence."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from archive_research.common import ReviewError, atomic_write, content_revision, file_lock, read_bytes
from .fixtures import create_fixture
from .models import ResearchCase
from .people import save_attribution
from .records import SaveOutcomeUnknown, checked_cases, rebuild, save_physical_object, save_review, snapshot


@contextmanager
def failed_release(path):
    with file_lock(path):
        try:
            yield
        finally:
            raise OSError('injected lock release failure')


class SaveIntegrityTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='archive-save-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Keep every history, binding and media change inside this fixture.
        configuration = override_settings(PILOT_ROOT=self.root, PROJECT={'mode': 'local_review'})
        configuration.enable()
        self.addCleanup(configuration.disable)
        for name in ('records', 'sample', 'cache'):
            (self.root / name).mkdir()
        create_fixture(self.root)
        self.evidence_path = self.root / 'records/evidence.json'
        self.history_path = self.root / 'records/assertions.jsonl'
        evidence, history, current, _ = snapshot()
        other = deepcopy(evidence['cases'][0])
        other.update(key='object', synthetic=False, title='Invented physical-object fixture', intended_use={
            'scope': 'private_local_research', 'authority': 'Disposable fixture', 'external_redistribution': False})
        other['documentary_people'] = [
            {'key': 'invented', 'name': 'Ada Example', 'reason': 'Invented attendance list.', 'context': {},
             'citation_keys': ['report-figure'], 'portraits': [], 'uncertainty': 'No portrait prepared.'}]
        evidence['cases'].append(other)
        history.append({**current[0], 'case_key': 'object', 'subject': 'object', 'key': 'assessment',
                        'relation': 'research_assessment', 'synthetic': False, 'revision': 0,
                        'supersedes_revision': None})
        atomic_write(self.evidence_path, json.dumps(evidence, indent=3).encode() + b'\n')
        self.original_history = b'\n'.join(json.dumps(record).encode() for record in history)
        atomic_write(self.history_path, self.original_history)
        self.base_revision = rebuild()
        self.submission = {**next(record for record in current if record['key'] == 'photo-type'),
                           'value': 'Fixture café', 'revision': self.base_revision, 'review': 'accepted'}

    def save(self):
        return save_review('photo-type', self.submission, 'operator')

    def committed_revision(self):
        return content_revision(self.evidence_path.read_bytes(), self.history_path.read_bytes())

    def test_exact_history_and_targeted_sqlite_refresh(self):
        other = ResearchCase.objects.get(key='object').payload
        with CaptureQueriesContext(connection) as queries:
            result = self.save()
        self.assertEqual(result, {'indexed': True, 'revision': self.committed_revision()})
        payload_updates = [query['sql'] for query in queries.captured_queries
                           if query['sql'].startswith('UPDATE') and '"payload" =' in query['sql']]
        self.assertEqual(len(payload_updates), 1)
        self.assertIn("'photo'", payload_updates[0])
        self.assertEqual(ResearchCase.objects.get(key='object').payload, other)
        self.assertEqual({row.revision for row in checked_cases()}, {result['revision']})
        saved = self.history_path.read_bytes()
        self.assertTrue(saved.startswith(self.original_history + b'\n'))
        self.assertIn('Fixture café'.encode(), saved)
        with self.assertRaisesRegex(ReviewError, 'Records changed'):
            self.save()
        self.assertEqual(self.history_path.read_bytes(), saved)
        self.submission['revision'] = result['revision']
        self.save()
        self.assertEqual(len(self.history_path.read_bytes().splitlines()), len(saved.splitlines()) + 1)

    def test_failed_revision_stamp_rolls_back_changed_case_payload(self):
        before = list(ResearchCase.objects.order_by('key').values())

        def reject_stamp(execute, sql, params, many, context):
            if sql.startswith('UPDATE') and '"payload" =' not in sql:
                raise DatabaseError('injected revision stamp failure')
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reject_stamp), self.assertLogs('archive_research.web.records', level='ERROR'):
            result = self.save()
        self.assertEqual(result, {'indexed': False, 'revision': self.committed_revision(),
                                  'failed_component': 'browsing', 'rebuild_available': True})
        self.assertEqual(list(ResearchCase.objects.order_by('key').values()), before)
        self.assertEqual(rebuild(), result['revision'])
        self.assertEqual({row.revision for row in checked_cases()}, {result['revision']})

    def test_save_paths_leave_the_corpus_index_unopened_even_when_missing(self):
        def attribution():
            return save_attribution('object', {'revision': self.committed_revision(), 'candidate_key': 'invented',
                'target_view_key': 'photo-view', 'position': 'At left', 'review': 'accepted',
                'reason': 'Fixture attribution.', 'uncertainty': 'Tentative.', 'citation_keys': ['photo-view']}, 'operator')

        def physical():
            return save_physical_object('object', {'revision': self.committed_revision(),
                'inventory_reference': 'TEST-1', 'reason': 'Fixture correction.'}, 'operator')

        with override_settings(PROJECT={'mode': 'local_review', 'research_index': 'index.json'}), \
                patch('archive_research.web.extraction.base_search_projection', side_effect=FileNotFoundError) as load, \
                patch('archive_research.web.feedback.staged_write') as publish:
            for index, save in enumerate((self.save, attribution, physical), start=9):
                with self.subTest(save=save.__name__):
                    result = save()
                    self.assertEqual(result, {'indexed': True, 'revision': self.committed_revision()})
                    self.assertEqual(len(snapshot()[1]), index)
                    self.assertEqual({row.revision for row in checked_cases()}, {result['revision']})
            load.assert_not_called()
            publish.assert_not_called()

    def test_directory_sync_failure_with_readable_replacement_reports_saved(self):
        def failed_after_replace(path, data):
            atomic_write(path, data)
            raise OSError('injected directory sync failure')

        with patch('archive_research.web.records.atomic_write', side_effect=failed_after_replace), \
                self.assertLogs('archive_research.web.records', level='ERROR'):
            result = self.save()
        self.assertEqual(result, {'indexed': False, 'revision': self.committed_revision(),
                                  'failed_component': 'storage', 'rebuild_available': True})

    def test_unreadable_storage_confirmation_reports_unknown_and_replay_is_stale(self):
        def failed_after_replace(path, data):
            atomic_write(path, data)
            raise OSError('injected directory sync failure')

        with patch('archive_research.web.records.atomic_write', side_effect=failed_after_replace), \
                patch('archive_research.web.records.read_bytes', side_effect=[
                    read_bytes(self.evidence_path), self.original_history, OSError('injected confirmation failure')]), \
                self.assertLogs('archive_research.web.records', level='ERROR'), \
                self.assertRaises(SaveOutcomeUnknown):
            self.save()
        self.assertEqual(len(snapshot()[1]), 9)
        with self.assertRaisesRegex(ReviewError, 'Records changed'):
            self.save()
        self.assertEqual(len(snapshot()[1]), 9)

    def test_lock_release_failure_preserves_each_successful_save(self):
        def attribution():
            return save_attribution('object', {'revision': self.committed_revision(), 'candidate_key': 'invented',
                'target_view_key': 'photo-view', 'position': 'At left', 'review': 'accepted',
                'reason': 'Fixture attribution.', 'uncertainty': 'Tentative.', 'citation_keys': ['photo-view']}, 'operator')

        def physical():
            return save_physical_object('object', {'revision': self.committed_revision(),
                'inventory_reference': 'TEST-1', 'reason': 'Fixture correction.'}, 'operator')

        for index, save in enumerate((self.save, attribution, physical), start=9):
            with self.subTest(save=save.__name__), \
                    patch('archive_research.web.records.file_lock', side_effect=failed_release), \
                    self.assertLogs('archive_research.web.records', level='ERROR') as logs:
                self.assertEqual(save(), {'indexed': True, 'revision': self.committed_revision()})
                self.assertEqual(len(self.history_path.read_bytes().splitlines()), index)
            self.assertIn('Could not release the research lock', logs.output[-1])

    def test_lock_release_failure_preserves_prewrite_error(self):
        with patch('archive_research.web.records.file_lock', side_effect=failed_release), \
                patch('archive_research.web.records.atomic_write', side_effect=OSError('injected prewrite failure')), \
                self.assertLogs('archive_research.web.records', level='ERROR'), \
                self.assertRaisesRegex(OSError, 'injected prewrite failure'):
            self.save()
        self.assertEqual(self.history_path.read_bytes(), self.original_history)

    def test_lock_release_failure_preserves_unknown_outcome(self):
        def failed_after_replace(path, data):
            atomic_write(path, data)
            raise OSError('injected directory sync failure')

        with patch('archive_research.web.records.file_lock', side_effect=failed_release), \
                patch('archive_research.web.records.atomic_write', side_effect=failed_after_replace), \
                patch('archive_research.web.records.read_bytes', side_effect=[
                    read_bytes(self.evidence_path), self.original_history, OSError('injected confirmation failure')]), \
                self.assertLogs('archive_research.web.records', level='ERROR'), \
                self.assertRaises(SaveOutcomeUnknown):
            self.save()
        self.assertEqual(len(snapshot()[1]), 9)

    def test_physical_object_requires_unchanged_preview(self):
        preview = self.root / 'sample/report-figure.png'
        preview.write_bytes(preview.read_bytes() + b'changed')
        with self.assertRaisesRegex(ReviewError, 'Source changed'):
            save_physical_object('object', {'revision': self.base_revision, 'inventory_reference': 'TEST-1',
                                            'reason': 'Fixture correction.'}, 'operator')
        self.assertEqual(self.history_path.read_bytes(), self.original_history)
