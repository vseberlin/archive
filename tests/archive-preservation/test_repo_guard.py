"""Exercise real Git and hooks in temporary repositories, with no live remotes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = next(p / 'tools/repo-guard.py' for p in Path(__file__).resolve().parents
              if (p / 'tools/repo-guard.py').is_file())


class RepositoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='repo-guard-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.remote = self.base / 'remote.git'
        self.other = self.base / 'other'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
        self.git('init', '--quiet', '--initial-branch=main', str(self.root), cwd=self.base)
        self.git('init', '--quiet', '--bare', '--initial-branch=main', str(self.remote), cwd=self.base)
        self.git('config', 'user.name', 'Guard Test')
        self.git('config', 'user.email', 'guard@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.root / 'tools').mkdir()
        script = SOURCE.read_text()
        start = script.index('ROOTS = ')
        end = script.index('GIT = ')
        policy = (f'ROOTS = ({str(self.root)!r},)\nBRANCHES = ("main",)\n'
                  'WORKTREE_PREFIXES = ("fix/", "release/")\n'
                  f'URLS = ({str(self.remote)!r},)\n')
        self.script = self.root / 'tools/repo-guard.py'
        self.script.write_text(script[:start] + policy + script[end:])
        (self.root / 'file.txt').write_text('baseline\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'baseline')
        self.git('push', '--quiet', '-u', 'origin', 'main')
        self.other.mkdir()
        self.guard('install')

    def execute(self, argv, cwd=None, env=None, ok=True, input=None):
        result = subprocess.run(argv, cwd=cwd or self.root, env=env or self.env,
                                input=input, capture_output=True, text=True, check=False, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def git(self, *args, **kwargs):
        return self.execute(['/usr/bin/git', *args], **kwargs)

    def guard(self, *args, script=None, **kwargs):
        return self.execute([sys.executable, '-B', str(script or self.script), *args], **kwargs)

    def edit(self):
        (self.root / 'file.txt').write_text('changed\n')

    def commit(self):
        self.edit()
        self.guard('stage', 'file.txt')
        self.guard('commit', 'reviewed change')

    def test_wrong_cwd_selects_own_repository_and_preserves_other(self):
        self.git('init', '--quiet', str(self.other), cwd=self.base)
        self.edit()
        self.guard('stage', 'file.txt', cwd=self.other)
        self.assertIn('file.txt', self.git('diff', '--cached', '--name-only').stdout)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.other).stdout, '')
        self.guard('commit', 'from another cwd', cwd=self.other)
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), 'from another cwd')

    def test_wrong_remote_blocks_wrapper_and_native_commit_even_no_verify(self):
        self.edit()
        self.git('add', 'file.txt')
        self.git('remote', 'set-url', 'origin', 'https://example.invalid/other.git')
        self.guard('commit', 'wrong project', ok=False)
        self.git('commit', '--no-verify', '-m', 'wrong project', ok=False)
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), 'baseline')

    def test_wrong_push_url_rejected(self):
        self.git('remote', 'set-url', '--push', 'origin', 'https://example.invalid/other.git')
        self.guard('check', ok=False)

    def test_effective_url_rewrite_rejected(self):
        self.git('config', 'url.https://example.invalid/.insteadOf', str(self.remote))
        self.guard('check', ok=False)

    def test_git_environment_redirects_rejected_before_mutation(self):
        self.edit()
        before = (self.root / '.git/index').read_bytes()
        for name, value in [('GIT_DIR', str(self.remote)), ('GIT_WORK_TREE', str(self.other)),
                            ('GIT_INDEX_FILE', str(self.other / 'index')), ('GIT_CONFIG_COUNT', '0')]:
            with self.subTest(name=name):
                result = self.guard('stage', 'file.txt', env={**self.env, name: value}, ok=False)
                self.assertIn('Unset Git', result.stderr)
        self.assertEqual((self.root / '.git/index').read_bytes(), before)

    def test_unapproved_clone_rejected(self):
        clone = self.base / 'clone'
        self.git('clone', '--quiet', str(self.remote), str(clone), cwd=self.base)
        self.guard('install', script=clone / 'tools/repo-guard.py', ok=False)

    def test_wrong_branch_and_detached_head_rejected(self):
        self.git('switch', '--quiet', '-c', 'unrelated')
        self.guard('check', ok=False)
        self.git('checkout', '--quiet', '--detach')
        self.guard('check', ok=False)

    def test_registered_worktree_supported_with_common_hooks(self):
        worktree = self.base / 'release'
        self.git('worktree', 'add', '--quiet', '-b', 'release/test', str(worktree))
        script = worktree / 'tools/repo-guard.py'
        self.guard('check', script=script, cwd=self.other)
        (worktree / 'file.txt').write_text('release\n')
        self.guard('stage', 'file.txt', script=script, cwd=self.other)
        self.guard('commit', 'release change', script=script, cwd=self.other)
        self.assertEqual(self.git('status', '--porcelain').stdout, '')

    def test_missing_owned_hooks_repaired_without_touching_index(self):
        hook = self.root / '.git/hooks/pre-push'
        hook.unlink()
        self.guard('check', ok=False)
        before = (self.root / '.git/index').read_bytes()
        self.guard('status', cwd=self.other)
        self.assertTrue(hook.is_file())
        self.assertEqual((self.root / '.git/index').read_bytes(), before)
        hook.chmod(0o644)
        self.guard('install')
        self.assertTrue(os.access(hook, os.X_OK))

    def test_unmanaged_hook_not_overwritten_and_no_partial_repair(self):
        early = self.root / '.git/hooks/pre-commit'
        late = self.root / '.git/hooks/pre-push'
        early.unlink()
        late.write_text('#!/bin/sh\necho existing-user-hook\n')
        self.guard('install', ok=False)
        self.assertFalse(early.exists())
        self.assertIn('existing-user-hook', late.read_text())

    def test_missing_hooks_directory_repaired_without_touching_index(self):
        directory = self.root / '.git/hooks'
        shutil.rmtree(directory)
        before = (self.root / '.git/index').read_bytes()
        self.guard('check', ok=False)
        self.assertFalse(directory.exists())
        self.guard('status', cwd=self.other)
        self.guard('check')
        self.assertEqual((self.root / '.git/index').read_bytes(), before)
        self.commit()

    def test_hooks_directory_symlink_or_file_not_replaced(self):
        directory = self.root / '.git/hooks'
        shutil.rmtree(directory)
        directory.symlink_to(self.other, target_is_directory=True)
        self.guard('install', ok=False)
        self.assertEqual(list(self.other.iterdir()), [])
        directory.unlink()
        directory.symlink_to(self.other / 'missing-hooks', target_is_directory=True)
        self.guard('install', ok=False)
        self.assertFalse((self.other / 'missing-hooks').exists())
        directory.unlink()
        directory.write_text('unrelated file\n')
        self.guard('install', ok=False)
        self.assertEqual(directory.read_text(), 'unrelated file\n')

    def test_hook_symlink_and_config_override_rejected(self):
        hook = self.root / '.git/hooks/pre-push'
        hook.unlink()
        hook.symlink_to(self.other / 'hook')
        self.guard('install', ok=False)
        self.assertFalse((self.other / 'hook').exists())
        hook.unlink()
        self.guard('install')
        self.git('config', 'core.hooksPath', str(self.other))
        self.guard('check', ok=False)
        self.git('config', 'core.hooksPath', '')
        self.guard('check', ok=False)

    def test_staging_paths_cannot_escape_or_select_nested_repo(self):
        self.git('init', '--quiet', str(self.root / 'nested'), cwd=self.base)
        (self.root / 'nested/file.txt').write_text('third party\n')
        (self.root / 'escape').symlink_to(self.other, target_is_directory=True)
        for name in ('../other/file.txt', 'nested/file.txt', 'nested', '.', 'escape/file.txt', '.git/config'):
            with self.subTest(name=name):
                self.guard('stage', name, ok=False)
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout, '')

    def test_native_staged_gitlink_rejected(self):
        oid = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + oid + ',nested')
        self.git('commit', '-m', 'accidental nested repository', ok=False)

    def test_dirty_sync_preserves_index_files_and_head(self):
        self.edit()
        before = self.git('rev-parse', 'HEAD').stdout
        index = (self.root / '.git/index').read_bytes()
        self.guard('sync', ok=False)
        self.assertEqual(before, self.git('rev-parse', 'HEAD').stdout)
        self.assertEqual(index, (self.root / '.git/index').read_bytes())
        self.assertEqual((self.root / 'file.txt').read_text(), 'changed\n')

    def test_unfinished_operation_rejected(self):
        (self.root / '.git/MERGE_HEAD').write_text(self.git('rev-parse', 'HEAD').stdout)
        self.guard('check', ok=False)

    def test_native_partial_commit_uses_legitimate_temporary_index(self):
        self.edit()
        self.git('commit', '--only', '-m', 'one file', '--', 'file.txt')
        self.assertEqual(self.git('log', '-1', '--format=%s').stdout.strip(), 'one file')

    def test_fast_forward_sync_and_ahead_sync_preserve_local_work(self):
        clone = self.base / 'remote-writer'
        self.git('clone', '--quiet', str(self.remote), str(clone), cwd=self.base)
        self.git('config', 'user.name', 'Other Writer', cwd=clone)
        self.git('config', 'user.email', 'other@example.invalid', cwd=clone)
        self.git('config', 'commit.gpgsign', 'false', cwd=clone)
        (clone / 'other.txt').write_text('independent change\n')
        self.git('add', 'other.txt', cwd=clone)
        self.git('commit', '--quiet', '-m', 'remote advance', cwd=clone)
        self.git('push', '--quiet', 'origin', 'main', cwd=clone)
        self.guard('sync', cwd=self.other)
        self.assertEqual((self.root / 'other.txt').read_text(), 'independent change\n')
        self.commit()
        before = self.git('rev-parse', 'HEAD').stdout
        self.guard('sync')
        self.assertEqual(before, self.git('rev-parse', 'HEAD').stdout)

    def test_native_rebase_rejected(self):
        self.commit()
        self.git('rebase', '--force-rebase', 'HEAD~1', ok=False)

    def test_publish_receipt_and_fast_forward_push(self):
        self.commit()
        receipt = self.guard('publish-check').stdout
        self.assertIn('expected_remote_head', receipt)
        self.guard('push')
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout,
                         self.git('rev-parse', 'main', cwd=self.remote).stdout)

    def test_push_rejects_deletion_other_ref_and_alternate_remote(self):
        before = self.git('rev-parse', 'main', cwd=self.remote).stdout
        self.git('push', 'origin', ':main', ok=False)
        self.git('push', 'origin', 'HEAD:other', ok=False)
        self.git('push', str(self.remote), 'HEAD:main', ok=False)
        self.assertEqual(before, self.git('rev-parse', 'main', cwd=self.remote).stdout)

    def test_force_push_of_diverged_history_and_sync_rejected(self):
        self.commit()
        clone = self.base / 'remote-writer'
        self.git('clone', '--quiet', str(self.remote), str(clone), cwd=self.base)
        self.git('config', 'user.name', 'Other Writer', cwd=clone)
        self.git('config', 'user.email', 'other@example.invalid', cwd=clone)
        (clone / 'other.txt').write_text('independent change\n')
        self.git('add', 'other.txt', cwd=clone)
        self.git('commit', '--quiet', '-m', 'remote advance', cwd=clone)
        self.git('push', '--quiet', 'origin', 'main', cwd=clone)
        before = self.git('rev-parse', 'HEAD').stdout
        self.guard('sync', ok=False)
        self.guard('publish-check', ok=False)
        self.git('push', '--force', 'origin', 'HEAD:main', ok=False)
        self.assertEqual(before, self.git('rev-parse', 'HEAD').stdout)
        self.assertNotEqual(before, self.git('rev-parse', 'main', cwd=self.remote).stdout)


if __name__ == '__main__':
    unittest.main()
