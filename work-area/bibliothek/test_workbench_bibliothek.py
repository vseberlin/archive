"""Decision and source-boundary checks; real human decisions are never written."""
import csv
import io
import json
import unittest
from unittest.mock import patch

import workbench_bibliothek as wb


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.review = wb.Review()

    def test_one_selected_representation_and_shared_cover(self):
        local = next(c for c in self.review.candidates if c['object_key'] == 'BIB-1001' and c['origin'] == 'protected_usb')
        writes = {}
        with patch.object(wb, 'atomic', side_effect=lambda name, payload: writes.__setitem__(name, payload)):
            self.review.save('BIB-1001', local['candidate_id'], 'Test in memory only')
        decisions = list(csv.DictReader(io.StringIO(writes['master-decisions.csv'].decode())))
        dispositions = list(csv.DictReader(io.StringIO(writes['candidate-dispositions.csv'].decode())))
        self.assertEqual(sum(r['disposition'] == 'selected_master' for r in dispositions if r['object_key'] == 'BIB-1001'), 1)
        self.assertTrue(all(r['deletion_authorized'] == 'false' for r in decisions + dispositions))
        members = json.loads(local['members_json'])
        self.assertEqual([m['role'] for m in members], ['cover', 'book_scan'])
        self.assertTrue(members[0]['path'].endswith('BIB-1001-md-23654.jpg'))

    def test_wrong_attachment_and_cross_book_selection_are_refused(self):
        with patch.object(wb, 'atomic') as write:
            # An ineligible attachment remains blocked; the real catalogue may
            # contain Vladimir's later explicit selection of the known mismatch.
            with patch.dict(self.review.by_candidate['BIB-1002:md'], eligible='false'):
                with self.assertRaises(ValueError):
                    self.review.save('BIB-1002', 'BIB-1002:md', '')
            with self.assertRaises(ValueError):
                self.review.save('BIB-1001', 'BIB-1003:md', '')
            write.assert_not_called()

    def test_changed_source_and_unknown_page_are_refused(self):
        candidate = next(c for c in self.review.candidates if c['origin'] == 'protected_usb')
        member = self.review.member('candidate:' + candidate['candidate_id']).copy()
        member['mtime_ns'] = int(member['mtime_ns']) + 1
        with self.assertRaises(ValueError):
            self.review.checked_path(member)
        with self.assertRaises(ValueError):
            self.review.preview('candidate:' + candidate['candidate_id'], 0)
        with self.assertRaises((KeyError, ValueError)):
            self.review.member('candidate:../../outside')

    def test_return_to_unresolved_preserves_other_objects(self):
        before = self.review.decisions()
        writes = {}
        with patch.object(wb, 'atomic', side_effect=lambda name, payload: writes.__setitem__(name, payload)):
            self.review.save('BIB-1003', '', 'Review remains open')
        after = {r['object_key']: r for r in csv.DictReader(io.StringIO(writes['master-decisions.csv'].decode()))}
        self.assertEqual(after['BIB-1001'], before['BIB-1001'])
        self.assertEqual(after['BIB-1002'], before['BIB-1002'])
        rows = list(csv.DictReader(io.StringIO(writes['candidate-dispositions.csv'].decode())))
        self.assertTrue(all(r['disposition'] == 'unresolved' for r in rows if r['object_key'] == 'BIB-1003'))


if __name__ == '__main__':
    unittest.main()
