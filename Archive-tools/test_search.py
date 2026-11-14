"""Synthetic preparation and cross-collection review regression tests."""
import argparse
from contextlib import redirect_stdout
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image

from matching import (HASHES, compare_metadata, comparison_buffer, metadata_question, normalized,
                      parse_czkawka, stamp, visual_matches)
from search import prepare, previous_pair_decisions
from workbench import EDIT_FIELDS, ReviewError, Workbench

CZKAWKA = Path('/mnt/volume/iss-archive-work/kurt-schwarz/work/tools/czkawka_cli-12.0.1')


def write_csv(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


class MatchingTests(unittest.TestCase):
    def test_candidate_admission_and_substitute_boundary(self):
        from consolidation import active_comparison
        from catalogues import ersatz_jpeg, genuine_tiff
        failed={'engine':'prior_similarity','evidence':{'passes_secondary_checks':False}}
        unsupported={'engine':'sift','status':'not_supported','inliers':0}
        self.assertFalse(active_comparison([failed,unsupported]))
        self.assertFalse(active_comparison([failed]))
        self.assertTrue(active_comparison([failed,{'engine':'sift','status':'supported'}]))
        self.assertTrue(active_comparison([unsupported,{'engine':'confirmed'}]))
        jpeg={'id':'unit#md:1','path':'master.jpg','row':{}}
        self.assertFalse(ersatz_jpeg(jpeg))
        jpeg['row']['object_selection_basis']='curated_5000px_jpeg'
        self.assertTrue(ersatz_jpeg(jpeg))
        jpeg['id']='unit#local:1'
        self.assertFalse(ersatz_jpeg(jpeg))
        self.assertFalse(genuine_tiff({'path':'manufactured.tif','is_surrogate':True}))

    def test_metadata_missing_is_not_conflict_and_normalization_preserves_suffixes(self):
        left = {'title': [{'value': 'Factory   Hall', 'source': 'embedded'}]}
        right = {'title': [{'value': 'factory hall', 'source': 'catalogue'}], 'creator': [{'value': 'Photographer'}]}
        fields = {r['field']: r for r in compare_metadata(left, right)}
        self.assertEqual(fields['title']['status'], 'agreement')
        self.assertEqual(fields['creator']['status'], 'archive_only')
        self.assertFalse(metadata_question(list(fields.values())))
        self.assertNotEqual(normalized('TFA-123.4'), normalized('TFA-1234'))
        self.assertTrue(metadata_question(compare_metadata(left, right, unavailable=True)))

    def test_different_caption_and_additional_rights_remain_review_questions(self):
        left = {'description': [{'value': 'Photo A'}], 'rights': [{'value': 'Restricted'}]}
        right = {'description': [{'value': 'Photo B'}]}
        fields = {r['field']: r for r in compare_metadata(left, right)}
        self.assertEqual(fields['description']['status'], 'different_values')
        self.assertEqual(fields['rights']['status'], 'source_only')
        self.assertTrue(metadata_question(list(fields.values())))

    def test_czkawka_reference_groups_do_not_create_source_source_identity(self):
        groups = [[{'path': 'ref'}, [{'path': 'a', 'difference': 1}, {'path': 'b', 'difference': 2}]]]
        result = parse_czkawka(groups, {'a': 'source-a', 'b': 'source-b'}, {'ref': 'master'}, 'Gradient')
        self.assertEqual(result['source-a'][0]['target_id'], 'master')
        self.assertEqual(result['source-b'][0]['difference'], 2)
        self.assertNotIn('source-b', str(result['source-a']))

    def test_perceptual_leads_are_independent_distances_including_polarity(self):
        target = {'id': 'master', 'feature': {'hashes': [0, 0, 0], 'path': '/target'}}
        found = visual_matches({'hashes': [1, 2, 4], 'path': '/source'}, [target])
        self.assertEqual(found[0]['phash_distance'], 1)
        self.assertNotIn('confirmed', found[0])
        self.assertFalse(visual_matches({'path': '/missing'}, [target]))

    def test_sampled_tiff_preview_honors_all_eight_orientations(self):
        operations = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                      4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                      6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}
        with tempfile.TemporaryDirectory() as td:
            base = Image.new('RGB', (16, 10))
            base.putdata([(x * 10, y * 20, (x + y) * 5) for y in range(10) for x in range(16)])
            for orientation in range(1, 9):
                path = Path(td) / f'{orientation}.tif'
                exif = Image.Exif(); exif[274] = orientation
                base.save(path, exif=exif)
                actual, method = comparison_buffer(path, 100, prefer_thumbnail=False)
                expected = base.transpose(operations[orientation]) if orientation in operations else base
                self.assertEqual(actual.size, expected.size)
                self.assertEqual(actual.tobytes(), expected.tobytes())


@unittest.skipUnless(CZKAWKA.exists(), 'Czkawka integration binary unavailable')
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-prepare-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.unit = self.root / 'unit'; self.unit.mkdir()
        (self.unit / 'masters').mkdir(); (self.unit / 'export').mkdir()
        self.copy = self.source / 'same.tif'; self.other = self.source / 'other.jpg'
        image = Image.effect_noise((100, 70), 40).convert('RGB')
        image.save(self.copy)
        Image.effect_noise((70, 100), 80).convert('RGB').save(self.other)
        self.master = self.unit / 'masters' / 'master.tif'
        self.master.write_bytes(self.copy.read_bytes())
        image.save(self.unit / 'export' / 'access.jpg')
        self.row = {'record_key': 'md:1', 'inventory': 'TFA-1', 'title': 'Catalogue title', 'description': '',
                    'creator': '', 'date_text': '', 'places': '', 'tags': '', 'rights': '', 'metadata_authority': 'test',
                    'file_role': 'preservation_master', 'final_relative_path': 'masters/master.tif',
                    'master_relative_path': 'masters/master.tif', 'original_source_path': str(self.copy),
                    'size_bytes': str(self.master.stat().st_size),
                    'final_sha256': hashlib.sha256(self.master.read_bytes()).hexdigest(), 'source_sha256': '', 'derivative_of': ''}
        self.write_catalogue()
        self.index = self.root / 'index.sqlite'
        con = sqlite3.connect(self.index)
        con.executescript('CREATE TABLE visual_roots(root_id TEXT,root_path TEXT);'
                          'CREATE TABLE feature_extractors(extractor_id TEXT);'
                          'CREATE TABLE visual_assets(asset_id TEXT,root_id TEXT,relative_path TEXT,size_bytes INTEGER,mtime_ns INTEGER,indexed_at TEXT,status TEXT,error TEXT,width_px INTEGER,height_px INTEGER);'
                          'CREATE TABLE visual_features(asset_id TEXT,extractor_id TEXT,feature_hex TEXT);')
        con.execute('INSERT INTO visual_roots VALUES (?,?)', ('root', str(self.root)))
        con.executemany('INSERT INTO feature_extractors VALUES (?)', [(h,) for h in HASHES])
        for i, path in enumerate([self.copy, self.other, self.master]):
            st = stamp(path)
            con.execute('INSERT INTO visual_assets VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (str(i), 'root', str(path.relative_to(self.root)), st['size_bytes'], st['mtime_ns'], 'now', 'indexed', '', 100, 70))
            for h in HASHES:
                con.execute('INSERT INTO visual_features VALUES (?,?,?)', (str(i), h, '0123456789abcdef'))
        con.commit(); con.close()
        self.args = argparse.Namespace(source=self.source, against=self.unit, work_root=self.root / 'review', index=self.index,
                                       czkawka=CZKAWKA, avantmedia_evidence=None, prior_decisions=[], source_hashes=[],
                                       source_extensions='tif,jpg', master_extensions='tif,tiff', max_candidates=5, threads=2)

    def write_catalogue(self):
        derivative = {**self.row, 'file_role': 'access_derivative', 'final_relative_path': 'export/access.jpg',
                      'derivative_of': 'masters/master.tif', 'size_bytes': str((self.unit / 'export' / 'access.jpg').stat().st_size),
                      'final_sha256': hashlib.sha256((self.unit / 'export' / 'access.jpg').read_bytes()).hexdigest()}
        write_csv(self.unit / 'CATALOG.csv', [self.row, derivative])

    def run_prepare(self):
        with redirect_stdout(io.StringIO()):
            return prepare(self.args)

    def request(self, app, oid, **changes):
        snapshot = app.snapshot(oid)
        request = {k: snapshot['decision'][k] for k in EDIT_FIELDS}
        return {**request, 'object_id': oid, 'revision': snapshot['revision'], **changes}

    def test_real_engines_resolve_pixel_coverage_and_keep_unmatched_review(self):
        self.args.index = None  # Exercise real Czkawka on residuals, rather than fabricated cached p/d/a leads.
        summary = self.run_prepare()
        self.assertEqual(summary['source_count'], 2)
        self.assertEqual(summary['master_count'], 1)
        matches = [json.loads(line) for line in (self.args.work_root / 'matches.jsonl').read_text().splitlines()]
        same = next(r for r in matches if r['source_path'] == str(self.copy))
        self.assertEqual({p['algorithm'] for p in same['matches'] if p['engine'] == 'czkawka'}, {'Gradient', 'Blockhash'})
        app = Workbench(self.args.work_root, 'reviewer')
        for oid in app.objects:
            self.assertFalse(app.snapshot(oid)['decision']['selected_candidate_id'].startswith('archive:'))
            app.save(self.request(app, oid, note='Identity remains unresolved'))
        self.assertEqual(summary['counts']['redundant'], 1)
        self.assertEqual(len(app.read_decisions()[0]), 1)
        skipped = json.loads((self.args.work_root / 'skipped-confirmed.jsonl').read_text())
        self.assertEqual(skipped['source_path'], str(self.copy))
        with self.assertRaisesRegex(ReviewError, 'already prepared'):
            self.run_prepare()
        confirmed, rejected = previous_pair_decisions([self.args.work_root / 'decisions.jsonl'],
            [{'path': str(p), 'stamp': stamp(p)} for p in (self.copy, self.other)],
            [{'id': 'md:1', 'path': str(self.master), 'stamp': stamp(self.master)}])
        self.assertFalse(confirmed)  # Pixel coverage and saved notes are not imported human decisions.
        self.assertFalse(rejected)

    def test_only_stamped_exact_hash_copy_is_skipped(self):
        inventory = self.root / 'hashes.csv'
        write_csv(inventory, [{'source_path': str(self.copy), **stamp(self.copy), 'sha256': self.row['final_sha256']}])
        self.args.source_hashes = [inventory]
        result = self.run_prepare()
        self.assertEqual(result['counts']['redundant'], 1)
        app = Workbench(self.args.work_root, 'test')
        self.assertEqual(len(app.objects), 1)
        skipped = json.loads((self.args.work_root / 'skipped-confirmed.jsonl').read_text())
        self.assertFalse(skipped['deletion_authorized'])

    def test_confirmed_copy_keeps_metadata_evidence_without_reopening_archive(self):
        # Exact bytes can contain a caption that differs from the external catalogue.
        with Image.open(self.copy) as image:
            image.save(self.root / 'caption.tif', tiffinfo={270: 'Embedded source caption'})
        self.copy.write_bytes((self.root / 'caption.tif').read_bytes())
        self.master.write_bytes(self.copy.read_bytes())
        self.row.update(description='Different catalogue caption', size_bytes=str(self.master.stat().st_size),
                        final_sha256=hashlib.sha256(self.master.read_bytes()).hexdigest())
        self.write_catalogue()
        inventory = self.root / 'hashes.csv'
        write_csv(inventory, [{'source_path': str(self.copy), **stamp(self.copy), 'sha256': self.row['final_sha256']}])
        self.args.source_hashes = [inventory]
        result = self.run_prepare()
        self.assertEqual(result['counts']['redundant'], 1)
        app = Workbench(self.args.work_root, 'test')
        self.assertFalse(any(o['metadata_review_required'] for o in app.objects.values()))
        skipped = json.loads((self.args.work_root / 'skipped-confirmed.jsonl').read_text())
        self.assertEqual(skipped['disposition'], 'redundant')
        self.assertEqual(skipped['retained_master_path'], str(self.master))
        self.assertTrue(skipped['metadata_review_required'])  # Evidence retained, not an archive-edit blocker.


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-background-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        self.unit = self.root / 'unit'; self.unit.mkdir()
        Image.new('RGB', (30, 20), 'red').save(self.source / 'incoming.jpg')
        Image.new('RGB', (30, 20), 'blue').save(self.unit / 'master.jpg')
        self.spec = self.root / 'unit.json'
        self.spec.write_text(json.dumps({'format': 'archive-tools-unit-1', 'unit_root': str(self.unit),
            'objects': [{'record_key': 'photo', 'metadata': {}, 'master': {'path': 'master.jpg'}}]}))
        self.review = self.root / 'review'
        self.before = {p: (stamp(p), p.read_bytes()) for d in (self.source, self.unit)
                       for p in d.rglob('*') if p.is_file()}

    def launch(self, *extra):
        command = [sys.executable, '-B', str(Path(__file__).with_name('search.py')),
                   '--source', str(self.source), '--archive', str(self.spec),
                   '--work-root', str(self.review), '--background', *extra]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            pid = json.loads(result.stdout)['pid']
            def cleanup():
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.addCleanup(cleanup)
        return result

    def finished(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = json.loads((self.review / 'preparation-run.json').read_text())
            if row['state'] in ('completed', 'failed', 'interrupted'):
                return row
            time.sleep(0.05)
        self.fail('Background worker did not finish')

    def test_detached_success_is_reviewable_and_preserves_inputs(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'launched')
        row = self.finished()
        self.assertEqual(row['state'], 'completed', row)
        self.assertEqual(row['exit_code'], 0)
        summary = json.loads(Path(row['summary']).read_text())
        self.assertEqual(row['source_files'], summary['source_count'])
        self.assertEqual(row['review_cases'], summary['review_cases'])
        self.assertIn('Preparation completed', (self.review / 'preparation.log').read_text())
        app = Workbench(self.review, 'test')
        self.assertEqual(len(app.objects), row['review_cases'])
        self.assertFalse((self.review / 'decisions.jsonl').exists())
        for path, original in self.before.items():
            self.assertEqual((stamp(path), path.read_bytes()), original)
        saved = {p: p.read_bytes() for p in self.review.iterdir() if p.is_file()}
        second = self.launch()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('new work directory', second.stderr)
        self.assertEqual(saved, {p: p.read_bytes() for p in self.review.iterdir() if p.is_file()})

    def test_worker_failure_is_saved_with_nonzero_exit_and_error_log(self):
        result = self.launch('--czkawka', str(self.root / 'missing-engine'))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.finished()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['exit_code'], 2)
        self.assertIn('missing-engine', row['error'])
        self.assertIn('Traceback', (self.review / 'preparation.log').read_text())
        self.assertFalse((self.review / 'workbench-input.json').exists())

    def test_no_logs_or_directories_are_created_inside_inputs(self):
        for root in (self.source / 'review', self.unit / 'review'):
            with self.subTest(root=root):
                self.review = root
                result = self.launch()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('overlaps', result.stderr)
                self.assertFalse(root.exists())
        self.review = Path('/mnt/volume/iss-archive/never-create-background-test')
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('protected', result.stderr)
        self.assertFalse(self.review.exists())

    def test_termination_records_interruption_and_stops_comparison_child(self):
        engine = self.root / 'slow-engine'
        engine.write_text(f'#!{sys.executable}\n'
            'import os, pathlib, sys, time\n'
            'if "--version" in sys.argv:\n'
            '    print("synthetic slow engine")\n'
            'else:\n'
            f'    pathlib.Path({str(self.root / "engine.pid")!r}).write_text(str(os.getpid()))\n'
            '    time.sleep(120)\n')
        engine.chmod(0o700)
        result = self.launch('--czkawka', str(engine))
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = json.loads(result.stdout)['pid']
        deadline = time.monotonic() + 10
        marker = self.root / 'engine.pid'
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), (self.review / 'preparation.log').read_text())
        self.assertEqual(os.getsid(pid), pid)
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        os.kill(pid, signal.SIGTERM)
        row = self.finished()
        self.assertEqual(row['state'], 'interrupted')
        self.assertEqual(row['exit_code'], 143)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        self.assertFalse((self.review / 'workbench-input.json').exists())


if __name__ == '__main__':
    unittest.main()
