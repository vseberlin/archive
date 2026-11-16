"""Mutation-boundary regression checks using disposable synthetic evidence only."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from archive_common import ReviewError
import search
import test_contract as fixtures


class PreparationGuardTests(unittest.TestCase):
    setUp = fixtures.ContractTests.setUp
    unit = fixtures.ContractTests.unit
    picture = fixtures.ContractTests.picture

    def test_destructive_and_misspelled_flags_never_launch(self):
        safe = ['/engine', 'dup', '-d', '/source', '-D', 'NONE', '-Q', '-p', '/work/result.json']
        search.validate_comparison_command(safe)
        variants = [safe[:6] + [mode] + safe[7:] for mode in ('AEO', 'AEN', 'HARD', 'none', 'NON')]
        variants += [safe[:7] + safe[8:], safe + ['-y'], safe + ['--move-to-trash'],
                     safe + ['-D', 'AEO'], [safe[0], 'delete', *safe[2:]], safe + ['--delete-method=AEO']]
        for command in variants:
            with self.subTest(command=command), patch('search.subprocess.Popen') as launch:
                with self.assertRaises(ReviewError):
                    search.run_logged(command, self.root / 'log', {}, 'test')
                launch.assert_not_called()
        self.assertFalse((self.root / 'log').exists())

    def test_foreground_cannot_overlap_prior_evidence_or_profile(self):
        self.unit(hashed=False)
        self.picture(self.source / 'photo.jpg')
        prior = self.root / 'prior'; prior.mkdir()
        marker = prior / 'metadata-cache.jsonl'; marker.write_bytes(b'preserved evidence\n')
        self.args.prior_review = [prior]
        self.args.work_root = prior
        with self.assertRaisesRegex(ReviewError, 'overlaps'):
            search.prepare(self.args)
        self.assertEqual(marker.read_bytes(), b'preserved evidence\n')
        self.args.prior_review = []
        self.args.work_root = self.root / 'profile-root'; self.args.work_root.mkdir()
        profile = self.args.work_root / 'input-profile.json'; profile.write_text('{}')
        self.args.czkawka_profile = profile
        with self.assertRaisesRegex(ReviewError, 'overlaps'):
            search.prepare(self.args)
        self.assertEqual(profile.read_text(), '{}')

    def test_source_alias_cannot_create_an_output_directory_inside_source(self):
        self.unit(hashed=False)
        alias = self.root / 'alias'; alias.symlink_to(self.source, target_is_directory=True)
        self.args.source = [alias]
        self.args.work_root = self.source / 'never-created'
        with self.assertRaisesRegex(ReviewError, 'canonical'):
            search.prepare(self.args)
        self.assertFalse(self.args.work_root.exists())

    def test_nonempty_work_root_and_hardlinked_log_are_refused(self):
        self.unit(hashed=False)
        photo = self.picture(self.source / 'photo.jpg')
        original = photo.read_bytes()
        root = self.args.work_root; root.mkdir()
        log = root / 'czkawka.log'; os.link(photo, log)
        with self.assertRaisesRegex(ReviewError, 'new or empty'):
            search.prepare(self.args)
        with patch('search.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ReviewError, 'hard link'):
                search.run_logged(['/engine', 'dup', '-D', 'NONE', '-Q'], log, {}, 'test')
            launch.assert_not_called()
        self.assertEqual(photo.read_bytes(), original)

    def test_python_mutations_stay_inside_work_root(self):
        self.unit(hashed=False)
        self.picture(self.source / 'photo.tif')
        root = self.args.work_root
        events = []
        enabled = True

        def check(path):
            if isinstance(path, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(path)).absolute()
                if path != root and not path.is_relative_to(root):
                    raise AssertionError(f'Preparation attempted mutation outside work root: {path}')

        def audit(event, args):
            if not enabled:
                return
            if event == 'open':
                path, mode, flags = args
                if (mode and any(c in mode for c in 'wax+')) or (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    check(path); events.append((event, str(path)))
            elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate', 'os.utime'):
                check(args[0]); events.append((event, str(args[0])))
            elif event in ('os.rename', 'os.link', 'os.symlink'):
                check(args[0]); check(args[1]); events.append((event, str(args[:2])))

        sys.addaudithook(audit)
        try:
            with redirect_stdout(io.StringIO()), patch('search.visual_matches', return_value=[{
                'engine': 'cached-pda', 'target_id': str(self.units[0]) + '#same-key', 'phash_distance': 0}]):
                result = search.prepare(self.args)
        finally:
            enabled = False
        self.assertTrue(events)
        self.assertEqual(result['pixel_coverage']['verified'], 1)
        self.assertFalse(result['source_modified'])
        self.assertFalse(result['archive_modified'])
        self.assertFalse(result['deletion_authorized'])
        for line in (root / 'source-dispositions.jsonl').read_text().splitlines():
            self.assertFalse(json.loads(line)['deletion_authorized'])


if __name__ == '__main__':
    unittest.main()
