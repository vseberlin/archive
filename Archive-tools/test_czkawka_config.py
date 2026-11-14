"""Configurable real-engine comparisons on synthetic files."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from archive_common import ReviewError
from catalogues import EmptyIndex, read_unit
from consolidation import Evidence
from czkawka_config import DEFAULT_PROFILE, load_profile, validate_profile
from matching import stamp
from search import inventory, run_blake3, run_czkawka
import test_contract as fixtures

CZKAWKA = Path('/mnt/volume/iss-archive-work/kurt-schwarz/work/tools/czkawka_cli-12.0.1')


class ProfileTests(unittest.TestCase):
    def test_cli_passes_replace_defaults_and_preserve_different_settings(self):
        result = load_profile(argparse.Namespace(czkawka_pass=['Gradient/16/Lanczos3/5/off', 'DoubleGradient/32/Gaussian/18'],
                             czkawka_exact_hash='BLAKE3', czkawka_scope='unconfirmed'))
        self.assertEqual(len(result['passes']), 2)
        self.assertEqual(result['passes'][0]['geometric_invariance'], 'off')
        self.assertEqual(result['passes'][1]['hash_size'], 32)
        self.assertEqual(result['passes'][1]['max_difference'], 18)
        self.assertEqual(result['exact_hash'], 'BLAKE3')
        self.assertEqual(DEFAULT_PROFILE['passes'][0]['max_difference'], 12)

    def test_invalid_or_destructive_settings_rejected(self):
        for profile in ({'delete_method': 'AEO'}, {'exact_hash': 'CRC32'}, {'proxy_edge': 0},
                        {'passes': [{'algorithm': 'BLAKE3'}]}, {'passes': [{'hash_size': 12}]},
                        {'passes': [{'max_difference': 41}]}, {'passes': [{'resize_filter': 'invented'}]},
                        {'passes': [{'geometric_invariance': 'arbitrary-rotation'}]}):
            with self.subTest(profile=profile), self.assertRaises(ReviewError):
                validate_profile(profile)


@unittest.skipUnless(CZKAWKA.exists(), 'Installed Czkawka unavailable')
class EngineConfigTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    unit = fixtures.ContractTests.unit
    run_prepare = fixtures.ContractTests.run_prepare

    def picture(self, path, size=(80, 60), caption=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new('RGB', size)
        image.putdata([((x * 13 + y * 3) % 256, (y * 17 + x * 5) % 256, (x * y) % 253)
                       for y in range(size[1]) for x in range(size[0])])
        image.save(path, **({'tiffinfo': {270: caption}} if caption else {}))
        return path

    def profile(self, **settings):
        path = self.root / 'profile.json'
        path.write_text(json.dumps({'format': 'archive-tools-czkawka-1', **settings}))
        self.args.czkawka = CZKAWKA
        self.args.czkawka_profile = path
        return path

    def test_blake3_confirms_original_bytes_without_touching_sources_or_catalogues(self):
        master = self.unit()
        for name in ('exact.tif', 'second.tif'):
            shutil.copyfile(master, self.source / name)
        # Same photograph with a changed metadata payload must not be treated as exact bytes.
        self.picture(self.source / 'metadata-changed.tif', caption='Another byte representation')
        extra = master.parent / 'not-a-selected-master.tif'; shutil.copyfile(master, extra)
        self.profile(exact_hash='BLAKE3', passes=[])
        before = {p: (stamp(p), p.read_bytes()) for d in [self.source, *self.units] for p in d.rglob('*') if p.is_file()}
        result, app = self.run_prepare()
        self.assertEqual(result['source_dispositions']['redundant'], 2)
        self.assertEqual(len(app.objects), 1)
        self.assertEqual(result['czkawka_exact']['confirmed_pairs'], 2)
        self.assertFalse(result['czkawka']['executed'])
        cache = json.loads((self.args.work_root / 'czkawka-blake3-signature.json').read_text())
        self.assertNotIn(str(extra), cache['input_stamps'])
        self.assertTrue(any(r['hash'] for group in cache['groups'] for r in group))
        for p, original in before.items():
            self.assertEqual((stamp(p), p.read_bytes()), original)

    def test_blake3_reuses_current_cache_but_checks_archive_sha256(self):
        master = self.unit(); photo = self.source / 'exact.tif'; shutil.copyfile(master, photo)
        sources, _ = inventory([self.source], EmptyIndex(), {'.tif'})
        targets, _ = read_unit(self.specs[0], EmptyIndex())
        root = self.root / 'direct'; root.mkdir()
        with redirect_stdout(io.StringIO()):
            first = run_blake3(CZKAWKA, sources, targets, Evidence(sources, targets), root, 2)
            second = run_blake3(CZKAWKA, sources, targets, Evidence(sources, targets), root, 2)
        self.assertFalse(first['cached_result']); self.assertTrue(second['cached_result'])
        retuned_root = self.root / 'retuned'; retuned_root.mkdir()
        third = run_blake3(CZKAWKA, sources, targets, Evidence(sources, targets), retuned_root, 2, [root])
        self.assertTrue(third['cached_result'])
        targets[0]['sha256'] = 'f' * 64
        with self.assertRaisesRegex(ReviewError, 'archive hash disagrees'):
            run_blake3(CZKAWKA, sources, targets, Evidence(sources, targets), root, 2)

    def test_real_doublegradient_32_and_cache_invalidation(self):
        master = self.unit(); shutil.copyfile(master, self.source / 'image.tif')
        sources, _ = inventory([self.source], EmptyIndex(), {'.tif'})
        targets, _ = read_unit(self.specs[0], EmptyIndex())
        root = self.root / 'direct'; root.mkdir()
        a = validate_profile({'passes': [{'algorithm': 'Gradient', 'hash_size': 16}]})
        b = validate_profile({'proxy_edge': 512, 'preview_mode': 'primary', 'passes': [
            {'algorithm': 'DoubleGradient', 'hash_size': 32, 'resize_filter': 'Gaussian', 'max_difference': 18, 'geometric_invariance': 'off'}]})
        with redirect_stdout(io.StringIO()):
            run_czkawka(CZKAWKA, sources, targets, root, 'same-inputs', 2, a)
            first = json.loads((root / 'czkawka-signature.json').read_text())['signature']
            matches, coverage = run_czkawka(CZKAWKA, sources, targets, root, 'same-inputs', 2, b)
        second = json.loads((root / 'czkawka-signature.json').read_text())['signature']
        self.assertNotEqual(first, second)
        lead = matches[str(self.source / 'image.tif')][0]
        for key, value in b['passes'][0].items():
            self.assertEqual(lead[key], value)
        self.assertEqual(lead['proxy_edge'], 512)
        self.assertEqual(coverage['profile'], b)
        with patch('search.subprocess.run', side_effect=AssertionError('unchanged configuration should reuse results')):
            self.assertEqual(run_czkawka(CZKAWKA, sources, targets, root, 'same-inputs', 2, b)[0], matches)

    def test_unconfirmed_scope_runs_new_algorithms_despite_existing_leads(self):
        master = self.unit(); shutil.copyfile(master, self.source / 'image.tif')
        self.args.czkawka = CZKAWKA
        lead = {'engine': 'cached-pda', 'target_id': str(self.units[0]) + '#same-key', 'phash_distance': 0}
        with patch('search.visual_matches', return_value=[lead]):
            first, _ = self.run_prepare()
        self.assertFalse(first['czkawka']['executed'])
        self.profile(scope='unconfirmed', passes=[{'algorithm': 'DoubleGradient', 'hash_size': 32}])
        with patch('search.visual_matches', return_value=[lead]):
            second, _ = self.run_prepare('retuned')
        self.assertTrue(second['czkawka']['executed'])
        self.assertEqual(second['czkawka']['algorithms'], ['DoubleGradient'])

    def test_old_profile_leads_remain_historical_when_retuning(self):
        self.unit()
        self.picture(self.source / 'image.tif', caption='Metadata still needs review')
        self.profile(passes=[{'algorithm': 'Gradient', 'hash_size': 16}])
        self.run_prepare()
        previous = self.args.work_root
        self.args.prior_review = [previous]
        self.profile(scope='unconfirmed', passes=[{'algorithm': 'DoubleGradient', 'hash_size': 32}])
        _, app = self.run_prepare('new-profile')
        obj = next(iter(app.objects.values()))
        self.assertEqual({m['algorithm'] for m in obj['matches'] if m['engine'] == 'czkawka'}, {'DoubleGradient'})
        self.assertTrue(any(e.get('previous_comparison', {}).get('algorithm') == 'Gradient' for e in obj['prior_review_evidence']))
        self.assertEqual(json.loads((self.args.work_root / 'czkawka-effective-profile.json').read_text())['passes'][0]['hash_size'], 32)


if __name__ == '__main__':
    unittest.main()
