"""Real SIFT on synthetic photographs; search evidence never becomes a decision."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from archive_common import ReviewError
from matching import stamp
import sift_matching as sift
import test_contract as fixtures


class SiftSettingsTests(unittest.TestCase):
    def test_opt_in_and_invalid_thresholds(self):
        self.assertIsNone(sift.load_profile(argparse.Namespace()))
        for settings in ({'sift': False, 'sift_ratio': 0.6}, {'sift': True, 'sift_ratio': 1},
                         {'sift': True, 'sift_ratio': float('nan')},
                         {'sift': True, 'sift_proxy_edge': 0}, {'sift': True, 'sift_min_inliers': 4}):
            with self.subTest(settings=settings), self.assertRaises(ReviewError):
                sift.load_profile(argparse.Namespace(**settings))

    def test_native_import_crash_is_a_normal_error(self):
        with patch('sift_matching.subprocess.run', return_value=subprocess.CompletedProcess([], -4, '', '')):
            with self.assertRaisesRegex(ReviewError, 'exit -4'):
                sift.runtime()


@unittest.skipUnless(importlib.util.find_spec('cv2'), 'Use the SIFT virtual environment')
class SiftTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    unit = fixtures.ContractTests.unit
    picture = fixtures.ContractTests.picture
    relate = fixtures.ContractTests.relate
    run_prepare = fixtures.ContractTests.run_prepare
    save = fixtures.ContractTests.save

    @classmethod
    def setUpClass(cls):
        cls.libraries = sift.runtime()
        cls.np = cls.libraries[1]

    def texture(self, path, seed=11):
        rng = self.np.random.default_rng(seed)
        image = Image.fromarray(rng.integers(0, 256, (360, 480), dtype=self.np.uint8)).filter(ImageFilter.GaussianBlur(1))
        draw = ImageDraw.Draw(image)
        for i in range(90):
            x, y = map(int, rng.integers([0, 0], [450, 330]))
            draw.ellipse((x, y, x + int(rng.integers(5, 30)), y + int(rng.integers(5, 30))),
                         outline=int(rng.integers(0, 256)), width=2)
            draw.text((x, y), str(i), fill=int(rng.integers(0, 256)))
        image.save(path)
        return image

    def verifier(self, name='cache', prior=(), **settings):
        root = self.root / name; root.mkdir(exist_ok=True)
        profile = sift.load_profile(argparse.Namespace(sift=True, **settings))
        return sift.Verifier(root, prior, profile, self.libraries)

    def item(self, path, ident='target'):
        return {'id': ident, 'path': str(path), 'stamp': stamp(path)}

    def test_crop_resize_rotation_mirror_and_negative(self):
        target = self.root / 'photo.tif'
        image = self.texture(target)
        variations = {'crop': image.crop((65, 45, 415, 315)).resize((420, 324)),
                      'rotation': image.rotate(90, expand=True),
                      'mirror': ImageOps.mirror(image), 'negative': ImageOps.invert(image)}
        checker = self.verifier()
        original = target.read_bytes()
        for name, changed in variations.items():
            with self.subTest(name=name):
                source = self.source / (name + '.jpg'); changed.save(source, quality=90)
                result = checker.pair(self.item(source), self.item(target))
                self.assertEqual(result['status'], 'supported', result)
                self.assertGreaterEqual(result['inliers'], 12)
                self.assertFalse(result['identity_confirmed'])
        self.assertEqual(target.read_bytes(), original)

    def test_unrelated_photos_and_featureless_images_stay_unresolved(self):
        target = self.root / 'photo.tif'; self.texture(target)
        source = self.source / 'different.jpg'; self.texture(source, 97)
        checker = self.verifier()
        result = checker.pair(self.item(source), self.item(target))
        self.assertEqual(result['status'], 'not_supported')
        blank = self.source / 'blank.jpg'; Image.new('L', (480, 360), 128).save(blank)
        self.assertEqual(checker.pair(self.item(blank), self.item(target))['status'], 'insufficient_features')
        bad = self.source / 'broken.tif'; bad.write_bytes(b'not a TIFF')
        result = checker.pair(self.item(bad), self.item(target))
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('error', result)

    def test_small_shared_patch_does_not_support_whole_photo(self):
        target = self.root / 'photo.tif'; a = self.texture(target)
        source = self.source / 'different.jpg'; b = self.texture(source, 97)
        b.paste(a.crop((0, 0, 70, 70)), (0, 0)); b.save(source)
        result = self.verifier().pair(self.item(source), self.item(target))
        self.assertEqual(result['status'], 'not_supported', result)

    def test_pair_and_features_reuse_and_settings_invalidation(self):
        target = self.root / 'photo.tif'; image = self.texture(target)
        source = self.source / 'image.jpg'; image.save(source)
        first = self.verifier(); a, b = self.item(source), self.item(target)
        expected = first.pair(a, b)
        second = self.verifier('second', [first.root])
        with patch.object(second, 'features', side_effect=AssertionError('Must reuse pair')):
            self.assertEqual(second.pair(a, b), expected)
        changed = self.verifier('threshold', [first.root], sift_ratio=0.65)
        with patch('sift_matching.comparison_buffer', side_effect=AssertionError('Must reuse features')):
            self.assertEqual(changed.pair(a, b)['status'], 'supported')
        self.assertEqual(changed.counts['features_reused'], 2)
        resized = self.verifier('edge', [first.root], sift_proxy_edge=800)
        resized.pair(a, b)
        self.assertEqual(resized.counts['features_extracted'], 2)
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1000000))
        fresh = self.verifier('new-stamp', [first.root])
        fresh.pair(self.item(source), b)
        self.assertEqual(fresh.counts['pairs_computed'], 1)
        self.assertEqual(fresh.counts['features_extracted'], 1)

    def test_sift_ranks_actual_match_over_false_zero_hash_without_confirming(self):
        wrong = self.unit('wrong', hashed=False); self.texture(wrong, 97)
        right = self.unit('right', hashed=False); image = self.texture(right)
        source = self.source / 'incoming.jpg'; image.resize((400, 300)).save(source)
        self.args.sift = True; self.args.max_candidates = 1
        wrong_id, right_id = (str(u) + '#same-key' for u in self.units)
        leads = [{'target_id': wrong_id, 'engine': 'cached-pda', 'phash_distance': 0},
                 {'target_id': right_id, 'engine': 'cached-pda', 'phash_distance': 5}]
        before = {p: (stamp(p), p.read_bytes()) for d in [self.source, *self.units] for p in d.rglob('*') if p.is_file()}
        with patch('search.visual_matches', return_value=leads), patch('search.progress') as report_progress:
            summary, app = self.run_prepare()
        obj = next(iter(app.objects.values()))
        references = [c['reference_object_id'] for c in obj['candidates'] if c.get('reference_object_id')]
        self.assertEqual(references, [right_id])
        self.assertEqual(obj['review_reason'], 'similarity_review')
        self.assertEqual(obj['confirmed_reference_ids'], [])
        decision = app.snapshot(obj['id'])['decision']
        self.assertFalse(decision['same_pairs'])
        self.assertFalse(decision['redundant_candidate_ids'])
        self.assertFalse(decision['selected_candidate_id'])
        self.assertEqual(summary['sift']['statuses'], {'not_supported': 1, 'supported': 1})
        self.assertEqual(summary['sift']['planned_pairs'], 2)
        self.assertEqual(summary['sift']['planned_uncached_pairs'], 2)
        self.assertEqual(summary['sift']['source_files'], 1)
        logs = [call.args[0] for call in report_progress.call_args_list]
        self.assertTrue(any('SIFT queue: 2 candidate pairs; sources: 1' in s for s in logs))
        self.assertTrue(any('SIFT: 2/2 candidate pairs' in s and 'min remaining' in s for s in logs))
        report = json.loads((self.args.work_root / 'matches.jsonl').read_text())
        self.assertEqual({r['target_id'] for r in report['matches']}, {wrong_id, right_id})
        for path, original in before.items():
            self.assertEqual((stamp(path), path.read_bytes()), original)
        # Retuning keeps old SIFT in historical evidence; disabling never reuses its rank.
        self.args.prior_review = [self.args.work_root]; self.args.sift = False
        with patch('search.visual_matches', return_value=leads):
            _, plain = self.run_prepare('without-sift')
        current = next(iter(plain.objects.values()))
        self.assertFalse(any(m['engine'] == 'sift' for m in current['matches']))
        self.assertTrue(any(e.get('previous_comparison', {}).get('engine') == 'sift'
                            for e in current['prior_review_evidence']))

    def test_confirmations_and_human_rejections_are_not_overruled(self):
        master = self.unit(hashed=False); image = self.texture(master)
        source = self.source / 'incoming.jpg'; image.save(source)
        self.args.sift = True
        self.relate(source, master, 'different_photo')
        with patch('search.visual_matches', return_value=[{'engine': 'cached-pda',
                   'target_id': str(self.units[0]) + '#same-key', 'phash_distance': 0}]):
            summary, _ = self.run_prepare()
        self.assertEqual(summary['sift']['pairs'], 0)
        self.args.relations = []
        relation = self.root / 'relationships.jsonl'; relation.unlink()
        self.relate(source, master, 'same_photo')
        summary, app = self.run_prepare('confirmed')
        self.assertEqual(summary['sift']['pairs'], 0)
        # Existing relation is still authoritative even with SIFT enabled.
        row = json.loads((self.args.work_root / 'source-dispositions.jsonl').read_text())
        self.assertEqual(row['archive_object_ids'], [str(self.units[0]) + '#same-key'])
        self.assertEqual(row['disposition'], 'redundant')

    def test_pixel_coverage_precedes_sift_and_skips_other_leads_without_hiding_metadata(self):
        master = self.unit('covered', hashed=False); image = self.texture(master)
        other = self.unit('unrelated', hashed=False); self.texture(other, 97)
        source = self.source / 'incoming.tif'
        image.save(source, tiffinfo={270: 'Additional source description'})
        self.args.sift = True
        leads = [{'engine': 'cached-pda', 'target_id': str(unit) + '#same-key', 'phash_distance': 0}
                 for unit in self.units]
        with patch('search.visual_matches', return_value=leads), \
                patch.object(sift.Verifier, 'pair', side_effect=AssertionError('Covered source must not enter SIFT')):
            summary, app = self.run_prepare()
        self.assertEqual(summary['pixel_coverage']['verified'], 1)
        self.assertEqual(summary['sift']['pairs'], 0)
        self.assertEqual(summary['sift']['covered_sources_skipped'], 1)
        self.assertEqual(summary['counts'], {'metadata_review': 1})
        obj = next(iter(app.objects.values()))
        self.assertEqual({m['target_id'] for m in obj['matches']}, {m['target_id'] for m in leads})
        self.assertTrue(obj['metadata_review_required'])

    def test_reused_sift_pairs_are_excluded_from_uncached_work_estimate(self):
        master = self.unit(hashed=False); image = self.texture(master)
        source = self.source / 'incoming.jpg'; image.resize((400, 300)).save(source)
        self.args.sift = True
        lead = {'engine': 'cached-pda', 'target_id': str(self.units[0]) + '#same-key', 'phash_distance': 1}
        with patch('search.visual_matches', return_value=[lead]):
            self.run_prepare()
        self.args.prior_review = [self.args.work_root]
        with patch.object(sift.Verifier, 'features', side_effect=AssertionError('Must reuse completed SIFT pair')), \
                patch('search.progress') as report_progress:
            summary, _ = self.run_prepare('cached-sift')
        self.assertEqual(summary['sift']['planned_pairs'], 1)
        self.assertEqual(summary['sift']['planned_uncached_pairs'], 0)
        self.assertEqual(summary['sift']['cache']['pairs_reused'], 1)
        self.assertTrue(any('cached work only' in call.args[0] for call in report_progress.call_args_list))

    def test_background_uses_the_same_sift_interpreter_and_writes_evidence(self):
        master = self.unit(hashed=False, metadata={'title': [{'value': 'Shared archive caption'}]})
        image = self.texture(master)
        source = self.source / 'image.tif'; image.resize((400, 300)).save(source, tiffinfo={270: 'Shared archive caption'})
        # Supply a stamped prior search lead, so this exercises the real CLI too.
        prior = self.root / 'prior'; prior.mkdir()
        target_id = str(self.units[0]) + '#same-key'
        (prior / 'preparation-summary.json').write_text('{}')
        (prior / 'workbench-input.json').write_text(json.dumps({'objects': [{'candidates': [{
            'id': 'archive:' + target_id, 'reference_object_id': target_id,
            'members': [{'path': str(master), **stamp(master)}]}]}]}))
        (prior / 'matches.jsonl').write_text(json.dumps({'source_path': str(source), 'source_stamp': stamp(source),
            'matches': [{'target_id': target_id, 'engine': 'cached-pda', 'phash_distance': 0}]}) + '\n')
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('search.py')),
            '--source', str(self.source), '--archive', str(self.specs[0]), '--work-root', str(self.args.work_root),
            '--prior-review', str(prior), '--sift', '--background'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = json.loads(result.stdout)['pid']
        def cleanup():
            try:
                import signal
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            row = json.loads((self.args.work_root / 'preparation-run.json').read_text())
            if row['state'] in ('completed', 'failed', 'interrupted'):
                break
            time.sleep(0.05)
        self.assertEqual(row['state'], 'completed', row)
        summary = json.loads(Path(row['summary']).read_text())
        self.assertEqual(summary['sift']['statuses'], {'supported': 1})
        self.assertEqual(row['command'][0], sys.executable)


if __name__ == '__main__':
    unittest.main()
