"""Explicit manual CLI; each page runs in its own process, with no daemon."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .contracts import require
from .runner import extract_one, load_plan
from .sources import Roots
from .storage import lock, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roots', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--work', default='artifacts')
    parser.add_argument('--expected-plan', help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command', required=True)
    one = commands.add_parser('extract'); one.add_argument('--index', type=int, required=True)
    run = commands.add_parser('run'); run.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    roots = Roots.from_toml(args.roots)
    plan_digest = args.expected_plan or hashlib.sha256(args.plan.read_bytes()).hexdigest()
    plan = load_plan(args.plan, plan_digest)
    if args.command == 'extract':
        require(0 <= args.index < len(plan['tasks']), 'Task index outside plan')
        print(json.dumps(extract_one(roots, args.work, plan['tasks'][args.index])))
        return
    require(args.limit >= 0, 'Limit must be nonnegative')
    output = roots.root(args.work, 'work')
    indices = list(range(len(plan['tasks'])))[:args.limit or None]
    summary = {'format': 'archive-research-run-1', 'metadata_adopted': False,
               'plan_sha256': plan_digest,
               'requested': len(plan['tasks']), 'attempts': []}
    # This distinct batch lock prevents two manual orchestrators racing between
    # per-page locks. Worker processes still acquire the output writer lock.
    import fcntl, os
    descriptor = os.open(output / '.batch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a+') as handle:
        require(os.fstat(handle.fileno()).st_nlink == 1, 'Unsafe batch lock')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for index in indices:
            process = subprocess.run([sys.executable, '-B', '-m', 'archive_research', '--roots', str(args.roots.absolute()),
                                      '--plan', str(args.plan.absolute()), '--work', args.work,
                                      '--expected-plan', plan_digest,
                                      'extract', '--index', str(index)], capture_output=True, text=True)
            if process.returncode:
                attempt = {'id': plan['tasks'][index]['id'], 'state': 'worker_error', 'error': process.stderr[-1500:]}
            else:
                attempt = {'id': plan['tasks'][index]['id'], **json.loads(process.stdout)}
            summary['attempts'].append(attempt)
            with lock(output):
                write_json(output, 'run-summary.json', summary)
            print(json.dumps(attempt), flush=True)
    print(json.dumps({'attempted': len(summary['attempts']), 'requested': summary['requested']}))


if __name__ == '__main__':
    main()
