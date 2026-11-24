"""Plan builder: catalogue-bound pages, explicit exclusions, sharded flat plans."""
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from archive_research import Root, Roots
from archive_research.plan_builder import FORMAT, file_kind, write_plans
from archive_research.runner import load_plan

HAVE_PDF_TOOLS = shutil.which('pdfinfo') is not None


class PlanBuilderTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.archive, self.work = base / 'archive', base / 'work'
        self.unit = self.archive / 'units/bibliothek'
        (self.unit / '00_MANIFESTS').mkdir(parents=True); self.work.mkdir()
        catalogue, manifest = [], []

        def add(key, name, sequence, role='preservation_master'):
            catalogue.append({'record_key': key, 'file_role': role, 'final_relative_path': name,
                              'sequence': sequence, 'master_representation_id': 'rep-' + name})
            manifest.append({'relative_path': name, 'sequence': sequence})
        Image.new('L', (40, 30), 200).save(self.unit / 'photo.dat', format='JPEG')  # suffix is not proof
        add('REC-IMG', 'photo.dat', '1')
        frames = [Image.new('L', (20, 10), v) for v in (0, 128, 255)]
        frames[0].save(self.unit / 'scan.tif', save_all=True, append_images=frames[1:])
        add('REC-TIFF', 'scan.tif', '1')
        if HAVE_PDF_TOOLS:
            pages = [Image.new('RGB', (100, 140), 'white') for _ in range(2)]
            pages[0].save(self.unit / 'book.pdf', save_all=True, append_images=pages[1:])
            add('REC-PDF', 'book.pdf', '1')
        Image.new('L', (8, 8)).save(self.unit / 'orphan.png')
        add('REC-ORDER', 'orphan.png', '')  # unresolved order must be excluded, not guessed
        Image.new('L', (8, 8)).save(self.unit / 'surrogate.png')
        add('REC-SURR', 'surrogate.png', '1', role='preservation_surrogate')
        for path, rows in [(self.unit / 'CATALOG.csv', catalogue),
                           (self.unit / '00_MANIFESTS/master-files.csv', manifest)]:
            with path.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader(); writer.writerows(rows)
        self.roots = Roots({'archive': Root(self.archive, 'read'), 'artifacts': Root(self.work, 'work')})

    def spec(self, **extra):
        return {'format': FORMAT, 'metadata_adopted': False, 'root': 'archive', 'unit': 'units/bibliothek',
                'identity_namespace': 'existing-book-namespace', 'material': 'clean_print',
                'output_prefix': 'bib-pass1', **extra}

    def test_signature_not_suffix(self):
        self.assertEqual(file_kind(self.unit / 'photo.dat'), 'image')
        self.assertEqual(file_kind(self.unit / 'scan.tif'), 'tiff')

    def test_pages_exclusions_and_shards(self):
        manifest = write_plans(self.roots, self.spec(shard_size=2), validate=False)
        reasons = {e['record_key']: e['reason'] for e in manifest['excluded']}
        self.assertIn('REC-ORDER', reasons)
        self.assertNotIn('REC-SURR', reasons)  # surrogate fallback when no master exists
        expected_pages = 1 + 3 + 1 + (2 if HAVE_PDF_TOOLS else 0)
        self.assertEqual(manifest['pages'], expected_pages)
        tasks = []
        for shard in manifest['shards']:
            plan = load_plan(self.work / shard['name'])
            self.assertLessEqual(len(plan['tasks']), 2)
            tasks.extend(plan['tasks'])
        self.assertEqual(len(tasks), manifest['tasks'])
        self.assertEqual(len({t['id'] for t in tasks}), len(tasks))
        tiff = [t for t in tasks if t['page']['kind'] == 'tiff']
        self.assertEqual(sorted(t['page']['index'] for t in tiff), [0, 1, 2])
        self.assertTrue(all(t['extractor'] == 'ocr' for t in tiff))  # no pdf-text on images
        self.assertTrue(all(t['ocr_profile'] == {'name': 'clean_print', 'version': 1} for t in tasks))
        self.assertEqual({t['page']['source']['object_id'] for t in tiff}, {'existing-book-namespace#REC-TIFF'})
        if HAVE_PDF_TOOLS:
            pdf = [t for t in tasks if t['page']['kind'] == 'pdf']
            self.assertEqual(len(pdf), 4)  # two pages x pdf-text + ocr
            self.assertTrue(all(t['page']['units'] == 'pdf-points' for t in pdf))

    def test_existing_output_refused(self):
        write_plans(self.roots, self.spec(), validate=False)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            write_plans(self.roots, self.spec(), validate=False)

    def test_missing_model_skips_extractor_and_visual_requires_model(self):
        manifest = write_plans(self.roots, self.spec(material='historical_newspaper'), validate=False)
        self.assertEqual(manifest['extractors_skipped_without_model'], ['document-layout'])
        with self.assertRaisesRegex(ValueError, 'models.visual'):
            write_plans(self.roots, self.spec(material='photo', output_prefix='p'), validate=False)

    def test_spec_fields_checked(self):
        with self.assertRaisesRegex(ValueError, 'Unknown or missing'):
            write_plans(self.roots, self.spec(surprise=True), validate=False)
        with self.assertRaisesRegex(ValueError, 'distinct subset'):
            write_plans(self.roots, self.spec(extractors=['visual']), validate=False)

    def test_page_bound_excludes_large_member(self):
        manifest = write_plans(self.roots, self.spec(max_pages_per_member=2), validate=False)
        self.assertIn('REC-TIFF', {e['record_key'] for e in manifest['excluded']})


if __name__ == '__main__':
    unittest.main()
