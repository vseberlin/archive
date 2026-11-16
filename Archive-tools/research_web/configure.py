"""Explicitly configure a private synthetic runtime; never overwrite its secrets."""
import argparse
import json
from pathlib import Path
import secrets
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from archive_common import atomic_write, require, work_root


def configure(path, username='vladimir'):
    root = work_root(path, must_exist=False)
    require(username and len(username) <= 150 and all(c.isalnum() or c in '-_.' for c in username),
            'Use a simple local operator name')
    require(not (root / 'project.toml').exists() and not (root / 'machine.toml').exists(),
            'Existing project or machine configuration will not be overwritten')
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    root.chmod(0o700)
    for name in ('records', 'sample', 'cache', 'exports'):
        directory = root / name
        require(not directory.is_symlink(), 'Redirected runtime directory')
        directory.mkdir(mode=0o700, exist_ok=True)
    atomic_write(root / 'project.toml', b'name = "WF synthetic pilot"\nmode = "synthetic"\n')
    atomic_write(root / 'machine.toml', (
        f'runtime_root = {json.dumps(str(root))}\nsecret_key = {json.dumps(secrets.token_urlsafe(48))}\n'
        f'operator_username = {json.dumps(username)}\n').encode())
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('runtime_root')
    parser.add_argument('--username', default='vladimir')
    args = parser.parse_args()
    print('Configured private synthetic runtime:', configure(args.runtime_root, args.username))
