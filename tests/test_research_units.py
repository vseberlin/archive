"""Selected-member adapters must preserve existing identity and imperfect order."""
import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from archive_research import Root, Roots
from archive_research.units import selected_members


class UnitMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unit = self.root / 'units/bibliothek'
        (self.unit / '00_MANIFESTS').mkdir(parents=True)
        self.catalogue, self.manifest = [], []
        for name, sequence in [('second.pdf', '3'), ('first.jpg', '1')]:
            (self.unit / name).write_bytes(b'synthetic selected member')
            self.catalogue.append({'record_key': 'BOOK-1', 'file_role': 'preservation_master',
                                   'final_relative_path': name, 'sequence': sequence,
                                   'master_representation_id': 'existing-selected-master'})
            self.manifest.append({'relative_path': name, 'sequence': sequence})
        self.roots = Roots({'archive': Root(self.root, 'read')})

    def read(self):
        for path, rows in [(self.unit/'CATALOG.csv', self.catalogue),
                           (self.unit/'00_MANIFESTS/master-files.csv', self.manifest)]:
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader(); writer.writerows(rows)
        return selected_members(self.roots, 'archive', 'units/bibliothek', 'BOOK-1',
                                identity_namespace='existing-book-namespace')

    def test_existing_identity_order_and_gap_preserved(self):
        members = self.read()
        self.assertEqual([m.position for m in members], [1, 3])
        self.assertEqual(members[0].source.object_id, 'existing-book-namespace#BOOK-1')
        self.assertEqual(members[0].source.representation, 'existing-selected-master')
        self.assertEqual(members[0].source.role, 'preservation_master')

    def test_catalogue_manifest_disagreement_refused(self):
        self.manifest[0]['sequence'] = '2'
        with self.assertRaisesRegex(ValueError, 'order conflict'):
            self.read()

    def test_duplicate_witness_refused(self):
        self.manifest.append(dict(self.manifest[0]))
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.read()

    def test_duplicate_order_refused(self):
        self.manifest[0]['sequence'] = self.catalogue[0]['sequence'] = '1'
        with self.assertRaisesRegex(ValueError, 'duplicate member positions'):
            self.read()


if __name__ == '__main__':
    unittest.main()
