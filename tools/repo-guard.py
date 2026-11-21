#!/usr/bin/env python3
"""Repository-bound Git commands and local hooks; Python standard library only."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


# Each independent repository owns its policy; no sibling checkout is required.
ROOTS = ('/home/vladimir/archive-local/source',)
BRANCHES = ('archive/scripts',)
WORKTREE_PREFIXES = ()
URLS = (
    'git@github.com:vseberlin/archive.git',
    'https://github.com/vseberlin/archive.git',
)
GIT = '/usr/bin/git'
HOOKS = ('pre-commit', 'prepare-commit-msg', 'pre-merge-commit', 'pre-push', 'pre-rebase')
MARKER = '# Managed by tools/repo-guard.py v1'
REDIRECTS = {
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
    'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE',
    'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
    'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM',
    'GIT_SHALLOW_FILE',
}


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


class Guard:
    def __init__(self, hook=False):
        self.root = Path.cwd().resolve() if hook else Path(__file__).resolve().parents[1]
        injected = REDIRECTS.intersection(os.environ)
        if hook:
            injected -= {'GIT_DIR', 'GIT_INDEX_FILE'}  # Git supplies these to hooks.
        require(not injected, 'Unset Git redirection/configuration variables: ' + ', '.join(sorted(injected)))
        self.env = dict(os.environ)
        self.env['GIT_TERMINAL_PROMPT'] = '0'
        top = self.git('rev-parse', '--show-toplevel').strip()
        require(Path(top).resolve() == self.root, 'Entrypoint is not at its repository root.')
        common = self.git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        self.common = Path(common).resolve()
        owners = [Path(p) for p in ROOTS if self.common == Path(p) / '.git']
        require(len(owners) == 1, 'Git storage does not belong to an approved project checkout.')
        self.owner = owners[0]
        require(not (self.owner / '.git').is_symlink(), 'Git storage must not be a symlink.')
        if self.root != self.owner:
            trees = self.git('worktree', 'list', '--porcelain').splitlines()
            require('worktree ' + str(self.root) in trees, 'Unregistered linked worktree.')
        gitdir = Path(self.git('rev-parse', '--absolute-git-dir').strip()).resolve()
        if hook and 'GIT_INDEX_FILE' in os.environ:
            index = Path(os.environ['GIT_INDEX_FILE']).resolve()
            require(index.parent == gitdir and
                    (index.name == 'index' or index.name.startswith('next-index-')),
                    'Hook index is outside the expected Git index location.')
        require(self.run('config', '--get', 'core.hooksPath').returncode == 1,
                'Custom core.hooksPath conflicts with managed hooks; inspect it before repair.')
        self.branch = self.git('symbolic-ref', '--quiet', '--short', 'HEAD', optional=True).strip()
        allowed = self.branch in BRANCHES or (
            self.root != self.owner and self.branch.startswith(WORKTREE_PREFIXES))
        require(allowed, 'Unexpected or detached branch; use the documented branch/worktree workflow.')
        for kind in ((), ('--push',)):
            urls = self.git('remote', 'get-url', *kind, '--all', 'origin').splitlines()
            require(len(urls) == 1 and urls[0] in URLS,
                    'Origin fetch/push destination does not match this project.')
        for relative in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
            path = Path(self.git('rev-parse', '--git-path', relative).strip())
            if not path.is_absolute():
                path = self.root / path
            require(not path.exists(), 'Unfinished Git operation; inspect and resolve it explicitly.')

    def run(self, *args, capture=True, input=None):
        return subprocess.run([GIT, '-C', str(self.root), *args], env=self.env,
                              text=True, input=input, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None, check=False)

    def git(self, *args, optional=False):
        result = self.run(*args)
        require(result.returncode == 0 or (optional and result.returncode == 1),
                'Git check failed: ' + ' '.join(args[:3]))
        return result.stdout

    def hooks(self, repair=False):
        directory = self.common / 'hooks'
        script = self.owner / 'tools/repo-guard.py'
        require(script.is_file() and not script.is_symlink(), 'Canonical guard script is missing or symlinked.')
        require(not directory.is_symlink(), 'Expected a real Git hooks directory, not a symlink.')
        if not directory.exists():
            require(repair, 'Managed hooks directory is missing; run this guard with install.')
            directory.mkdir(mode=0o755)
        require(directory.is_dir(), 'Expected a real Git hooks directory.')
        expected = {}
        # Preflight every file before writing any; never replace an unrelated hook.
        for name in HOOKS:
            path = directory / name
            body = '#!/bin/sh\n' + MARKER + '\nexec /usr/bin/python3 -B ' + shlex.quote(str(script)) + ' _hook ' + name + ' "$@"\n'
            require(not path.is_symlink(), 'Refusing a symlinked hook: ' + name)
            if path.exists():
                require(path.is_file() and path.read_text().splitlines()[1:2] == [MARKER],
                        'Unmanaged hook requires explicit integration: ' + name)
            expected[path] = body
        for path, body in expected.items():
            valid = path.exists() and path.read_text() == body and os.access(path, os.X_OK)
            if not valid:
                require(repair, 'Managed hooks need repair; run this guard with install.')
                temporary = directory / (path.name + '.repo-guard-' + str(os.getpid()))
                try:
                    with temporary.open('x') as stream:
                        stream.write(body)
                    temporary.chmod(0o755)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                print('Repaired hook: ' + path.name, file=sys.stderr)

    def files(self, tree=None):
        records = self.git('ls-tree', '-rz', tree) if tree else self.git('ls-files', '--stage', '-z')
        for entry in records.split('\0'):
            if not entry:
                continue
            meta, name = entry.split('\t', 1)
            require(meta.split()[0] != '160000', 'Nested Git repository/submodule is not permitted: ' + name)
        if tree:
            return
        changed = self.git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
        for name in changed.split('\0'):
            if name:
                self.path(name)

    def path(self, name):
        path = self.root / name
        require(not Path(name).is_absolute() and '..' not in Path(name).parts,
                'Stage paths must be explicit project-relative files.')
        require('.git' not in Path(name).parts and path.resolve().is_relative_to(self.root),
                'Path escapes the project or enters Git storage.')
        require(not path.is_dir(), 'Stage individual files; directory-wide staging is disabled.')
        for parent in path.parents:
            if parent == self.root:
                break
            require(not (parent / '.git').exists(), 'File is inside a nested repository: ' + name)

    def clean(self):
        require(not self.git('status', '--porcelain'),
                'Working tree is dirty; preserve/classify changes before sync or publication.')

    def fetch(self):
        require(self.run('fetch', '--no-tags', 'origin',
                         'refs/heads/main:refs/remotes/origin/main', capture=False).returncode == 0,
                'Fetch failed; no merge or publication attempted.')

    def publication(self):
        self.clean()
        self.files('HEAD')
        self.fetch()
        head = self.git('rev-parse', 'HEAD').strip()
        remote = self.git('rev-parse', 'origin/main').strip()
        require(self.run('merge-base', '--is-ancestor', remote, head).returncode == 0,
                'Publication would replace/diverge from remote history; inspect before proceeding.')
        return {'root': str(self.root), 'branch': self.branch, 'head': head,
                'tree': self.git('rev-parse', 'HEAD^{tree}').strip(),
                'remote': 'origin', 'destination': 'refs/heads/main', 'expected_remote_head': remote}

    def hook(self, name, arguments):
        self.hooks()
        require(name != 'pre-rebase', 'Automatic rebase is disabled; recovery requires explicit review.')
        if name == 'pre-push':
            require(len(arguments) == 2 and arguments[0] == 'origin' and arguments[1] in URLS,
                    'Push must use this project\'s origin, not an alternate URL/remote.')
            self.clean()
            head = self.git('rev-parse', 'HEAD').strip()
            self.files(head)
            for line in sys.stdin:
                parts = line.split()
                require(len(parts) == 4, 'Malformed push update.')
                local_ref, local_oid, remote_ref, remote_oid = parts
                require(local_ref in ('HEAD', 'refs/heads/' + self.branch) and local_oid == head and
                        remote_ref == 'refs/heads/main' and set(remote_oid) != {'0'},
                        'Only current HEAD to existing main is allowed; no deletion or other refs.')
                require(self.run('merge-base', '--is-ancestor', remote_oid, local_oid).returncode == 0,
                        'Non-fast-forward or unknown remote head; fetch and inspect before pushing.')
        else:
            self.files()

    def align_publication(self, receipt_path, published_head):
        """Adopt an identical API publication, retaining local history atomically."""
        self.clean()
        receipt = json.loads(Path(receipt_path).read_text())
        require(isinstance(receipt, dict), 'Expected a publish-check receipt.')
        require(receipt.get('root') == str(self.root) and receipt.get('branch') == self.branch and
                receipt.get('remote') == 'origin' and receipt.get('destination') == 'refs/heads/main',
                'Publication receipt belongs to another checkout or destination.')
        for oid in (receipt.get('head'), receipt.get('tree'), receipt.get('expected_remote_head'), published_head):
            require(isinstance(oid, str) and len(oid) == 40 and all(c in '0123456789abcdef' for c in oid),
                    'Publication requires full hexadecimal object IDs.')
        head = self.git('rev-parse', 'HEAD').strip()
        require(head == receipt['head'] and self.git('rev-parse', 'HEAD^{tree}').strip() == receipt['tree'],
                'Local HEAD changed since publication review.')
        self.files(head)
        self.fetch()
        require(self.git('rev-parse', 'origin/main').strip() == published_head,
                'Remote HEAD changed; inspect before alignment.')
        self.files(published_head)
        require(self.git('rev-parse', published_head + '^{tree}').strip() == receipt['tree'],
                'Published tree differs from the reviewed local tree.')
        for tip in (head, published_head):
            require(self.run('merge-base', '--is-ancestor', receipt['expected_remote_head'], tip).returncode == 0,
                    'Publication does not preserve the reviewed remote ancestry.')
        backup = 'refs/tags/local-before-publication/' + head
        existing = self.git('rev-parse', '--verify', '--quiet', backup, optional=True).strip()
        require(not existing or existing == head, 'Local recovery tag already names different history.')
        self.clean()
        transaction = ('start\n' + ('verify ' + backup + ' ' + head if existing else 'create ' + backup + ' ' + head) +
                       '\nverify refs/remotes/origin/main ' + published_head +
                       '\nupdate refs/heads/' + self.branch + ' ' + published_head + ' ' + head + '\nprepare\ncommit\n')
        require(self.run('update-ref', '--stdin', '-m', 'Align verified API publication; retain local history',
                         input=transaction).returncode == 0,
                'Atomic alignment refused; inspect refs before retrying.')
        print(json.dumps({'head': published_head, 'tree': receipt['tree'], 'retained_local_history': backup}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'install', 'status', 'sync', 'publish-check', 'push'):
        commands.add_parser(name)
    commands.add_parser('stage').add_argument('paths', nargs='+')
    commands.add_parser('commit').add_argument('message')
    align = commands.add_parser('align-publication', help='Explicitly align an identical API publication, retaining local history')
    align.add_argument('--receipt', required=True)
    align.add_argument('--published-head', required=True)
    hook = commands.add_parser('_hook')
    hook.add_argument('name', choices=HOOKS)
    hook.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    guard = Guard(hook=args.command == '_hook')
    if args.command == '_hook':
        guard.hook(args.name, args.arguments)
        return
    guard.hooks(repair=args.command != 'check')
    if args.command in ('check', 'install'):
        print('Repository and hooks verified: ' + str(guard.root) + ' [' + guard.branch + ']')
    elif args.command == 'status':
        require(guard.run('status', '--short', '--branch', capture=False).returncode == 0, 'Status failed.')
    elif args.command == 'stage':
        for name in args.paths:
            guard.path(name)
        require(guard.run('--literal-pathspecs', 'add', '--', *args.paths, capture=False).returncode == 0,
                'Staging failed; inspect the index.')
        guard.files()
    elif args.command == 'commit':
        guard.files()
        require(guard.run('commit', '-m', args.message, capture=False).returncode == 0, 'Commit failed.')
    elif args.command == 'align-publication':
        guard.align_publication(args.receipt, args.published_head)
    elif args.command == 'sync':
        guard.clean()
        guard.fetch()
        guard.files('origin/main')
        if guard.run('merge-base', '--is-ancestor', 'origin/main', 'HEAD').returncode == 0:
            print('Already contains origin/main; local commits preserved.')
        else:
            require(guard.run('merge-base', '--is-ancestor', 'HEAD', 'origin/main').returncode == 0,
                    'Histories diverged; no automatic merge, reset, or rebase.')
            require(guard.run('merge', '--ff-only', 'origin/main', capture=False).returncode == 0,
                    'Fast-forward failed; inspect the working tree.')
    else:
        receipt = guard.publication()
        print(json.dumps(receipt, indent=2), flush=True)
        if args.command == 'push':
            require(guard.run('push', 'origin', 'HEAD:refs/heads/main', capture=False).returncode == 0,
                    'Push failed; inspect the remote before retrying.')


if __name__ == '__main__':
    try:
        main()
    except (Refused, OSError, ValueError) as error:
        print('repo-guard: ' + str(error), file=sys.stderr)
        sys.exit(2)
