"""Verified search projection reuse without touching review history."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep, time_ns
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from archive_research.search_projection import FORMAT, seal, validate
from . import projection_cache, workspace


class ProjectionCacheTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='archive-projection-test-')
        self.addCleanup(directory.cleanup)
        self.addCleanup(projection_cache._load.cache_clear)
        self.root = Path(directory.name)
        self.output = self.root / 'artifacts'
        self.output.mkdir()
        self.config = self.root / 'roots.toml'
        self.config.write_text(f'[roots.artifacts]\npath="{self.output}"\naccess="work"\n')
        self.path = self.output / 'index.json'
        override = override_settings(PROJECT={'extraction_roots': str(self.config), 'research_index': self.path.name})
        override.enable()
        self.addCleanup(override.disable)
        self.source = {'root': 'sample', 'path': 'paper.pdf', 'size_bytes': 120, 'mtime_ns': 12345}
        self.value = self.projection('First reading')
        self.path.write_text(json.dumps(self.value))

    def projection(self, reading):
        catalogue = {'kind': 'catalogue', 'key': 'context', 'fields': {'title': 'Recorded title'},
                     'source_identity': {'unit': 'trial', 'object_id': 'sample-object'},
                     'evidence': {'catalogue': self.source, 'representation_paths': [self.source['path']]}}
        extraction = {'kind': 'extraction', 'key': 'prepared', 'extractor': 'pdf-text', 'availability': 'available',
                      'fields': {'text': reading},
                      'evidence': {'page': {'source': self.source, 'kind': 'pdf', 'index': 1}}}
        unrelated = {**extraction, 'key': 'other', 'evidence': {'page': {
            **extraction['evidence']['page'], 'source': {**self.source, 'mtime_ns': 12346}}}}
        return seal({'format': FORMAT, 'metadata_adopted': False, 'entries': [catalogue, extraction, unrelated],
                     'selection': {'findings': [{'root': 'unavailable-history', 'path': 'assertions.jsonl'}]}})

    def replace(self, value):
        temporary = self.output / 'replacement.json'
        before = self.path.stat()
        temporary.write_text(json.dumps(value))
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        temporary.replace(self.path)

    def test_case_reuses_single_verification_and_exact_indexed_context(self):
        with patch.object(projection_cache, 'time_ns', return_value=time_ns() + projection_cache._SETTLE_NS):
            cold = workspace.text_projection()
            indexed = projection_cache.case_index(cold)
            with (patch.object(projection_cache.os, 'open', side_effect=AssertionError('Warm read reopened the index')),
                  patch.object(projection_cache, '_case_index', side_effect=AssertionError('Warm read rescanned entries'))):
                warm = workspace.text_projection()
                self.assertIs(warm, cold)
                self.assertIs(projection_cache.case_index(warm), indexed)
        self.assertEqual(indexed.catalogue['sample', 'paper.pdf'][0]['fields']['title'], 'Recorded title')
        view = {'source': self.source, 'page_sequence': 1}
        matches = indexed.prepared_entries(view)
        self.assertEqual([entry['key'] for entry in matches], ['prepared'])
        self.assertEqual(indexed.prepared_entries({**view, 'page_sequence': 2}), ())

    @patch.object(projection_cache, 'time_ns', side_effect=lambda: time_ns() + projection_cache._SETTLE_NS)
    def test_atomic_replacement_and_same_size_corruption_invalidate(self, _clock):
        cold = workspace.text_projection()
        before = self.path.stat()
        self.replace(self.projection('Other reading'))
        after = self.path.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertNotEqual(before.st_ino, after.st_ino)
        fresh = workspace.text_projection()
        self.assertNotEqual(cold['projection_sha256'], fresh['projection_sha256'])
        sleep(.02)  # Put the write in a later tick on coarse-clock test filesystems.
        self.path.write_text(self.path.read_text().replace('Other reading', 'False reading'))
        os.utime(self.path, ns=(after.st_atime_ns, after.st_mtime_ns))
        corrupt = self.path.stat()
        self.assertEqual((corrupt.st_ino, corrupt.st_size, corrupt.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'Search projection changed'):
            workspace.text_projection()

    def test_initial_snapshot_under_unsettled_stamp_is_never_reused(self):
        stamp = projection_cache._file_stamp(self.path.stat())
        with (patch.object(projection_cache, '_file_stamp', return_value=stamp),
              patch.object(projection_cache, 'time_ns', side_effect=(stamp[-1], stamp[-1] + projection_cache._SETTLE_NS))):
            self.assertEqual(workspace.text_projection()['projection_sha256'], self.value['projection_sha256'])
            self.path.write_text(self.path.read_text().replace('First reading', 'False reading'))
            with self.assertRaisesRegex(ValueError, 'Search projection changed'):
                workspace.text_projection()

    def test_replacement_during_validation_cannot_publish_older_snapshot(self):
        replacement = self.projection('Other reading')

        def validate_then_replace(value):
            validate(value)
            self.replace(replacement)
            return value

        with patch.object(projection_cache, 'validate', side_effect=validate_then_replace):
            with self.assertRaisesRegex(ValueError, 'Search projection changed'):
                workspace.text_projection()
        self.assertEqual(workspace.text_projection()['projection_sha256'], replacement['projection_sha256'])
