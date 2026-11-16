"""Explicit local CLI: init, rebuild, export or storage-check. No background jobs."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

sys.dont_write_bytecode = True
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'research_web.settings')
import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, transaction

from archive_common import atomic_write, file_lock, require
from .fixtures import create_fixture
from .models import ResearchCase
from .records import export_bundle, rebuild


def storage_check():
    root = settings.PILOT_ROOT / 'cache'
    marker = root / 'storage-check.txt'
    with file_lock(root / '.storage-check.lock'):
        program = ('import fcntl,sys; f=open(sys.argv[1],"a+"); '
                   'fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)')
        result = subprocess.run([sys.executable, '-c', program, str(root / '.storage-check.lock')],
                                capture_output=True, timeout=5)
        require(result.returncode != 0 and b'BlockingIOError' in result.stderr, 'Lock exclusion failed')
        atomic_write(marker, b'before\n')
        with marker.open('rb') as old:
            atomic_write(marker, b'after\n')
            require(old.read() == b'before\n' and marker.read_bytes() == b'after\n', 'Atomic replace failed')
    original = list(ResearchCase.objects.values())
    try:
        with transaction.atomic():
            ResearchCase.objects.all().update(title='Synthetic rollback check')
            raise RuntimeError('rollback')
    except RuntimeError:
        pass
    require(list(ResearchCase.objects.values()) == original, 'SQLite rollback failed')
    with connection.cursor() as cursor:
        require(cursor.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'SQLite check failed')
    print('Storage checks passed: cross-process lock exclusion, atomic replacement, SQLite rollback.')


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('action', choices=['init', 'rebuild', 'export', 'storage-check', 'ocr', 'package'])
    parser.add_argument('--case', help='Explicit case for export or OCR')
    parser.add_argument('--view', help='Existing citation key for explicit OCR')
    args = parser.parse_args()
    root = settings.PILOT_ROOT
    if args.action == 'init':
        call_command('migrate', run_syncdb=True, interactive=False, verbosity=0)
        if not (root / 'records/evidence.json').exists():
            create_fixture(root)
        rebuild()
        User = get_user_model()
        username = settings.MACHINE.get('operator_username', 'vladimir')
        if not User.objects.filter(username=username).exists():
            password = secrets.token_urlsafe(24)
            User.objects.create_superuser(username=username, email='', password=password)
            # atomic_write creates mode 0600; credentials remain private and outside Git.
            atomic_write(root / 'operator-login.txt',
                         f'Local URL: http://127.0.0.1:8785/\nUsername: {username}\nPassword: {password}\n'.encode())
        print('Research pilot initialized. Operator login is in the private runtime root.')
    elif args.action == 'rebuild':
        print('Rebuilt research cases:', rebuild())
    elif args.action == 'export':
        path = root / 'exports/synthetic-research.json'
        atomic_write(path, export_bundle(args.case))
        print('Unadopted synthetic export:', path)
    elif args.action == 'ocr':
        require(args.view, '--view is required for OCR')
        from .processing import generate_ocr
        print('Optional OCR:', generate_ocr(args.view, args.case or 'photo'))
    elif args.action == 'package':
        from .portable import build_package
        print('Synthetic-only package:', build_package())
    else:
        storage_check()


if __name__ == '__main__':
    main()
