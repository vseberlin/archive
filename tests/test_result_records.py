"""Real synthetic extraction, immutable evidence and binding compatibility."""
from dataclasses import asdict, replace
import hashlib
import json
import unittest
from unittest.mock import patch

import test_positioned_extraction as fixtures
from archive_research.runner import extract_one
from archive_research import result_records
from archive_research.receipts import read_current, read_origin


class ResultRecordTests(unittest.TestCase):
    setUp = fixtures.PositionedTests.setUp
    bind = fixtures.PositionedTests.bind
    pdf = fixtures.PositionedTests.pdf

    def task(self, page):
        return {'id': 'native', 'page': asdict(page), 'extractor': 'pdf-text', 'settings': {'dpi': 100}}

    def test_identity_collection_and_order_changes_reuse_actual_extraction(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        original = (self.output / (first['key'] + '.json')).read_bytes()
        changed = replace(page, source=replace(page.source, unit='another-unit',
                          object_id='ISS-2026-K7M3-J', representation='stable-representation'),
                          member=7, logical_page=12, printed_label='XII')
        with patch('archive_research.runner.render', side_effect=AssertionError('must reuse computation')):
            second = extract_one(self.roots, 'artifacts', self.task(changed))
        self.assertTrue(second['reused'], second)
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(first['result_sha256'], second['result_sha256'])
        self.assertEqual(original, (self.output / (first['key'] + '.json')).read_bytes())
        _, current, _ = read_current(self.roots, 'artifacts', second['key'])
        self.assertEqual(current.page, changed)
        historical, old, _ = result_records.retained(self.roots, 'artifacts', first['evidence_sha256'])
        self.assertEqual(old.page, page)
        self.assertTrue(historical['words'])

    def test_pinned_parent_and_historical_citation_survive_move_and_reocr(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        origin = result_records.origin(self.roots, 'artifacts', first['key'], first['result_sha256'])
        path = self.input / page.source.path
        moved = self.input / 'moved.pdf'
        path.rename(moved)
        changed = replace(page, source=self.bind(moved), member=2)
        second = extract_one(self.roots, 'artifacts', self.task(changed))
        self.assertTrue(second['reused'], second)
        _, parent, digest = read_origin(self.roots, 'artifacts', origin, changed)
        self.assertEqual(digest, first['result_sha256'])
        self.assertEqual(parent.page, page)
        new_task = self.task(changed)
        new_task['settings']['dpi'] = 110
        third = extract_one(self.roots, 'artifacts', new_task)
        self.assertNotEqual(third['result_sha256'], first['result_sha256'])
        _, _, historical_digest = read_origin(self.roots, 'artifacts', origin, changed)
        self.assertEqual(historical_digest, first['result_sha256'])

    def test_same_bytes_do_not_merge_holdings_or_current_bindings(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        other = replace(page, source=replace(page.source, object_id='unresolved-incoming-file'))
        second = extract_one(self.roots, 'artifacts', self.task(other))
        self.assertTrue(second['reused'], second)
        self.assertEqual(first['result_sha256'], second['result_sha256'])
        self.assertNotEqual(read_current(self.roots, 'artifacts', first['key'])[1].page.source.object_id,
                            read_current(self.roots, 'artifacts', second['key'])[1].page.source.object_id)

    def test_corrupt_lookup_cannot_authorize_reuse(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        row = json.loads((self.output / (first['key'] + '.json')).read_text())
        pointer = self.output / ('computation-' + row['computation_sha256'] + '.json')
        value = json.loads(pointer.read_text()); value['result_sha256'] = '0' * 64
        pointer.write_text(json.dumps(value))
        changed = replace(page, member=2)
        outcome = extract_one(self.roots, 'artifacts', self.task(changed))
        self.assertEqual(outcome['state'], 'error')
        self.assertIn('Retained computation differs', outcome['error'])

    def test_missing_lookup_rebuilds_from_selected_receipt_without_inference(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        row = json.loads((self.output / (first['key'] + '.json')).read_text())
        pointer = self.output / ('computation-' + row['computation_sha256'] + '.json')
        pointer.unlink()
        with patch('archive_research.runner.render', side_effect=AssertionError('must not re-render')):
            second = extract_one(self.roots, 'artifacts', self.task(page))
        self.assertTrue(second['reused'], second)
        self.assertTrue(pointer.exists())

    def test_retained_artifact_tampering_and_current_result_tampering_refused(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        row, result, _ = result_records.retained(self.roots, 'artifacts', first['evidence_sha256'])
        (self.output / result.artifacts[0].path).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Retained artifact changed'):
            result_records.retained(self.roots, 'artifacts', first['evidence_sha256'])
        path = self.output / (first['key'] + '.json')
        value = json.loads(path.read_text()); value['words'][0]['text'] = 'altered'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'Immutable result changed'):
            read_current(self.roots, 'artifacts', first['key'])
        with patch('archive_research.runner.render', side_effect=AssertionError('must not reauthorize tampered result')):
            retry = extract_one(self.roots, 'artifacts', self.task(page))
        self.assertEqual(retry['state'], 'error')
        self.assertIn('Immutable result changed', retry['error'])

    def test_legacy_raw_citation_remains_readable_after_upgrade(self):
        page = self.pdf()
        first = extract_one(self.roots, 'artifacts', self.task(page))
        path = self.output / (first['key'] + '.json')
        row = json.loads(path.read_text())
        row.pop('result_sha256'); row.pop('computation_sha256')
        path.write_text(json.dumps(row))
        legacy_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        extract_one(self.roots, 'artifacts', self.task(page))
        _, historical, digest = read_origin(self.roots, 'artifacts',
                                             {'key': first['key'], 'receipt_sha256': legacy_digest}, page)
        self.assertEqual(digest, legacy_digest)
        self.assertEqual(historical.page, page)

    def test_lookup_write_failure_reports_saved_evidence_and_retry_repairs(self):
        page = self.pdf()
        original = result_records.storage.write_json
        def fail_lookup(root, name, value):
            if name.startswith('computation-'):
                raise OSError('synthetic lookup failure')
            return original(root, name, value)
        with patch.object(result_records.storage, 'write_json', side_effect=fail_lookup):
            first = extract_one(self.roots, 'artifacts', self.task(page))
        self.assertEqual(first['state'], 'complete', first)
        self.assertIn('Evidence saved', first['warning'])
        result_records.retained(self.roots, 'artifacts', first['evidence_sha256'])
        with patch('archive_research.runner.render', side_effect=AssertionError('must not rerender')):
            second = extract_one(self.roots, 'artifacts', self.task(page))
        self.assertTrue(second['reused'], second)
        self.assertNotIn('warning', second)


if __name__ == '__main__':
    unittest.main()
