import tempfile
import csv
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_clue_research as research


class EvidenceTests(unittest.TestCase):
    def test_large_noisy_ocr_field_is_preserved(self):
        noise = 'x' * 140000
        rows = list(csv.DictReader(io.StringIO('text\tconf\n'+noise+'\t0\n'), delimiter='\t'))
        self.assertEqual(rows[0]['text'], noise)

    def row(self):
        return {'references': [], 'assessments': {k: {'value': None, 'basis': 'unknown'}
                for k in ('location', 'event', 'photograph_date', 'event_date')}}

    def test_inaccessible_is_not_negative_match(self):
        row = self.row()
        row['references'] = [{'id': 'a', 'access_status': 'access_limited', 'verdict': 'no_match'}]
        with self.assertRaises(ValueError):
            research.validate_comparison(row)

    def test_caption_cannot_support_visual_match(self):
        row = self.row()
        row['references'] = [{'id': 'a', 'record_opened': True, 'image_inspected': False}]
        row['assessments']['location'] = {'basis': 'visual_match', 'reference_ids': ['a']}
        with self.assertRaises(ValueError):
            research.validate_comparison(row)

    def test_inspection_needs_actual_image(self):
        row = self.row()
        row['references'] = [{'id': 'a', 'record_opened': True, 'image_inspected': True}]
        with self.assertRaises(ValueError):
            research.validate_comparison(row)

    def test_date_requires_reasoning(self):
        row = self.row()
        row['assessments']['photograph_date'] = {'value': '1964-05-17', 'basis': 'caption'}
        with self.assertRaises(ValueError):
            research.validate_comparison(row)

    def test_unknown_is_valid(self):
        research.validate_comparison(self.row())

    def test_crop_bounds(self):
        self.assertEqual(research.crop_box([.1,.2,.9,.8], [100,200]), [10,40,90,160])
        with self.assertRaises(ValueError):
            research.crop_box([.9,0,.1,1], [100,200])

    def test_overwrite_and_escape(self):
        with tempfile.TemporaryDirectory() as d, patch.object(research, 'WORK', Path(d)):
            output = Path(d)/'evidence.json'
            research.save(output, {'metadata_adopted': False})
            with self.assertRaises(ValueError):
                research.save(output, {})
            with self.assertRaises(ValueError):
                research.destination(Path(d).parent/'outside-evidence.json')

    def test_writable_mount_rejected(self):
        with patch('subprocess.check_output', return_value='/dev/sdf1 /media/vladimir/Elements ntfs3 rw'):
            with self.assertRaises(ValueError):
                research.protected_source('/media/vladimir/Elements/restore/archive/example.tif')


if __name__ == '__main__':
    unittest.main()
