"""AEB accession safety checks. Real source evidence is read; mutations are intercepted."""
import copy
import io
import json
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import prepare_accession_591 as prep
import accession_591 as accession


class AccessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = prep.build_plan()

    def test_one_master_per_object_preserves_complete_recorded_order(self):
        representations = self.plan['objects']
        self.assertEqual(68, len({row['md_object_id'] for row in representations}))
        actual = [path for row in representations for path in json.loads(row['ordered_master_paths'])]
        masters = [row for row in self.plan['files'] if row['file_role'] == 'master_member']
        self.assertEqual(109, len(set(actual)))
        self.assertEqual(set(actual), {row['relative_path'] for row in masters})
        candidate = next(row for row in representations if row['inventory'] == 'AEB_001')
        self.assertEqual(['27895', '96130', '28772', '28773', '28774', '28775', '28776'],
                         [Path(path).stem.split('--')[-1] for path in json.loads(candidate['ordered_master_paths'])])

    def test_preparation_does_not_write_or_create_directories(self):
        with patch.object(prep, 'atomic') as write, patch.object(Path, 'mkdir') as mkdir:
            plan = prep.build_plan()
        write.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(self.plan, plan)
        self.assertFalse(plan['accession_performed'])
        self.assertFalse(plan['accession_authorized'])
        self.assertTrue(all(not row['final_sha256'] for row in plan['files'] + plan['derivatives'] + plan['source_disposition']))

    def test_changed_decision_or_tampered_member_plan_cannot_execute(self):
        for change in ['selected_candidate_id', 'ordered_master_paths']:
            damaged = copy.deepcopy(self.plan)
            damaged['objects'][0][change] = '' if change == 'selected_candidate_id' else '[]'
            with patch.object(Path, 'read_text', return_value=json.dumps(damaged)):
                with self.assertRaisesRegex(ValueError, 'differs from live input'):
                    prep.check_saved(self.plan)
        with patch.object(accession, 'preflight', side_effect=ValueError('Saved preparation differs')), patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaises(ValueError):
                accession.execute('Explicit test authority; all writes intercepted')
            mkdir.assert_not_called()

    def test_incomplete_md_candidate_is_refused_before_any_write(self):
        review = prep.Review()
        original_health = review.check()
        candidate = review.by_candidate['md-55151']
        candidate['members_json'] = json.dumps(json.loads(candidate['members_json'])[:-1])
        with patch.object(prep, 'Review', return_value=review), patch.object(review, 'check', return_value=original_health), patch.object(prep, 'atomic') as write:
            with self.assertRaisesRegex(ValueError, 'order or completeness'):
                prep.build_plan()
            write.assert_not_called()

    def test_changed_source_or_missing_mount_refuses_execution_before_mkdir(self):
        with patch.object(accession, 'preflight', return_value=self.plan), patch.object(prep, 'guard', side_effect=ValueError('Wrong mount')), patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaises(ValueError):
                accession.execute('Explicit test authority; writes intercepted')
            mkdir.assert_not_called()
        source = self.plan['files'][0]
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            prep.checked_input(source['source_path'], int(source['source_size_bytes']) + 1, source['source_mtime_ns'])

    def test_existing_target_refused_and_empty_authority_cannot_mutate(self):
        exists = Path.exists
        with patch.object(Path, 'exists', lambda path: True if path == prep.TARGET else exists(path)):
            with self.assertRaisesRegex(ValueError, 'refusing overwrite'):
                prep.build_plan()
        with patch.object(accession, 'preflight') as preflight, patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaisesRegex(ValueError, 'authorization'):
                accession.execute('')
            preflight.assert_not_called()
            mkdir.assert_not_called()

    def test_outside_and_nested_destinations_are_refused(self):
        for relative in ['/tmp/aeb.pdf', '../aeb.pdf', 'masters/../aeb.pdf', 'masters/nested/aeb.pdf', 'other/aeb.pdf']:
            with self.assertRaises(ValueError):
                prep.checked_relative(relative)

    def test_nonidentical_sources_never_become_exact_copies(self):
        fake_catalog = [dict(row, final_sha256=row['source_sha256']) for row in self.plan['files']]
        sources = accession.finalize_sources(self.plan, fake_catalog)
        self.assertEqual(Counter({'true': 119, 'false': 3}), Counter(row['exact_copy_proven'] for row in sources))
        self.assertEqual(6, sum(row['source_role'] == 'redundant_candidate' for row in sources))
        self.assertTrue(all(row['deletion_authorized'] == 'false' for row in sources))
        mixed = [row for row in sources if row['drain_state'] == 'retain_mixed_project_source']
        self.assertEqual(1, len(mixed))
        self.assertTrue(mixed[0]['source_path'].endswith('2019-12-12_Digis-Abschluss.pptx'))
        excluded = {row['source_path'] for row in self.plan['documents'] if row['decision'] == 'excluded'}
        self.assertFalse(excluded & {row['source_path'] for row in sources})

    def test_catalogue_literals_roundtrip_without_formulas_or_lost_unicode(self):
        # In-memory serializer test only; no workbook artifact or directory is created.
        rows = [{'id': '00012', 'title': '=HYPERLINK("x")', 'description': 'ä & <b>\r\nsecond line', 'empty': ''}]
        payload = accession.xlsx_bytes(rows)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            self.assertFalse(tree.findall('.//{' + accession.NS + '}f'))
            actual = [[cell.find('{' + accession.NS + '}is/{' + accession.NS + '}t').text or '' for cell in row]
                      for row in tree.find('{' + accession.NS + '}sheetData')]
            self.assertEqual([list(rows[0]), list(rows[0].values())], actual)

    def test_derivatives_only_reference_selected_final_master_members(self):
        masters = {row['relative_path'] for row in self.plan['files'] if row['file_role'] == 'master_member'}
        self.assertEqual(109, len(self.plan['derivatives']))
        for row in self.plan['derivatives']:
            self.assertIn(row['master_relative_path'], masters)
            self.assertEqual(str(prep.TARGET / row['master_relative_path']), row['source_path'])
            self.assertTrue(row['relative_path'].startswith('export/'))
            self.assertEqual('access_preview', row['file_role'])


if __name__ == '__main__':
    unittest.main()
