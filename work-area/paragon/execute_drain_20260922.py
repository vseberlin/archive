"""One authorized Paragon drain. Default is read-only; no recursive deletion.

The pinned CSV remains the immutable scope. Refuse replay if a receipt exists.
Historical preflight hashes are reused with unchanged source/archive stamps.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
from datetime import datetime, timezone

W = Path('/mnt/volume/iss-archive-work/paragon')
U = Path('/mnt/volume/iss-archive/units/paragon')
MOUNT = Path('/media/vladimir/Elements')
SOURCE = MOUNT / 'restore/archive'
LIST = U / 'db-restore/manifests/drain-list.csv'
PREFLIGHT = W / 'deletion-preflight-20260922.json'
RECEIPT = W / 'drain-execution-20260922.json'
LIST_SHA = '5d63313f1f5c24bbe8c6728ce1ff1e978fc1c378046de155b240561a2407ffd1'
PREFLIGHT_SHA = '66aaac71638e7e146e66cc29ae46842801f9960c0c814412d7785c9b4b0d8a98'
COUNT = 1375
BYTES = 22986884818
DEVICE = os.makedev(8, 81)
AUTHORIZATION = 'ok lets drain. no outher units should be touched no files outside of the list should be deleted.ok?'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def mount_guard(mode):
    mounts = json.loads(subprocess.check_output([
        '/usr/bin/findmnt', '--json', '-R', str(MOUNT), '-o',
        'SOURCE,TARGET,FSTYPE,OPTIONS,UUID,MAJ:MIN,FSROOT'], text=True))['filesystems']
    require(len(mounts) == 1 and not mounts[0].get('children'), 'Unexpected mount tree')
    m = mounts[0]
    expected = dict(source='/dev/sdf1', target=str(MOUNT), fstype='ntfs3',
                    uuid='6CAAB316AAB2DC30', **{'maj:min': '8:81', 'fsroot': '/'})
    require(all(m.get(k) == v for k, v in expected.items()), 'Wrong source mount identity')
    options = set(m['options'].split(','))
    require(mode in options and ({'ro', 'rw'} - {mode}).isdisjoint(options), 'Wrong source mount mode')
    return m


def source_path(path):
    p = Path(path)
    require(p.is_absolute() and p.is_relative_to(SOURCE) and p != SOURCE,
            'Path outside approved source root: ' + path)
    require(str(p) == path and os.path.normpath(path) == path and
            not any(ord(c) < 32 for c in path), 'Non-canonical source path')
    return p


def directory_fd(path):
    """Walk from / without following any symlink, including mount ancestors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in Path(path).parts[1:]:
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        require(os.fstat(fd).st_dev == DEVICE, 'Parent directory is on another device')
        return fd
    except BaseException:
        os.close(fd)
        raise


def identity(st):
    return dict(size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                st_dev=st.st_dev, st_ino=st.st_ino, link_count=st.st_nlink)


def verify_source(record, parent_fd=None):
    p = source_path(record['source_path'])
    owned = parent_fd is None
    fd = directory_fd(p.parent) if owned else parent_fd
    try:
        st = os.stat(p.name, dir_fd=fd, follow_symlinks=False)
        require(stat.S_ISREG(st.st_mode) and st.st_dev == DEVICE and st.st_nlink == 1,
                'Not a single-link regular source file: ' + str(p))
        require(all(record[k] == v for k, v in identity(st).items()),
                'Source identity changed: ' + str(p))
    finally:
        if owned:
            os.close(fd)


def verify_archive(records):
    for r in records:
        p = Path(r['path'])
        require(p.is_relative_to(U) and p.resolve(strict=True) == p, 'Unsafe archive path')
        st = p.lstat()
        require(stat.S_ISREG(st.st_mode) and
                [st.st_size, st.st_mtime_ns] == [r['size_bytes'], r['mtime_ns']],
                'Archive payload changed: ' + str(p))


def verify_inputs(preflight):
    require(sha(PREFLIGHT) == PREFLIGHT_SHA, 'Preflight fingerprint changed')
    for path, expected in preflight['input_sha256'].items():
        require('sha256:' + sha(path) == expected, 'Input fingerprint changed: ' + path)


def parent_snapshot(parents):
    result = {}
    for parent in sorted(parents):
        fd = directory_fd(parent)
        try:
            entries = {}
            for name in os.listdir(fd):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                entries[name] = dict(identity(st), kind=stat.S_IFMT(st.st_mode))
            result[parent] = entries
        finally:
            os.close(fd)
    return result


def verify_neighbors(before, after, deleted):
    for parent, original in before.items():
        removed = {Path(p).name for p in deleted if str(Path(p).parent) == parent}
        current = after[parent]
        require(set(current) == set(original) - removed,
                'Unexpected directory-entry change: ' + parent)
        for name, actual in current.items():
            expected = original[name]
            # Directory size/mtime may change when an explicitly listed child is removed.
            keys = ('kind', 'st_dev', 'st_ino') if expected['kind'] == stat.S_IFDIR else tuple(expected)
            require(all(actual[k] == expected[k] for k in keys),
                    'Unlisted neighbor changed: ' + str(Path(parent) / name))


def validate_plan():
    require(not RECEIPT.exists(), 'Execution receipt already exists; replay prohibited')
    require(sha(PREFLIGHT) == PREFLIGHT_SHA, 'Preflight fingerprint changed')
    preflight = json.loads(PREFLIGHT.read_text())
    require(preflight['mechanical_guards_passed'] and not preflight['errors'], 'Failed preflight')
    mount = mount_guard('ro')
    verify_inputs(preflight)
    raw = LIST.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == LIST_SHA, 'Drain list fingerprint changed')
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8'), newline='')))
    paths = [r['source_path'] for r in rows]
    require(len(paths) == len(set(paths)) == COUNT, 'Wrong path count or duplicate paths')
    require(all(r['unit'] == 'paragon' and r['source_kind'] == 'file' and
                r['drain_state'] == 'candidate_after_separate_review' for r in rows), 'Wrong list scope')
    allowed = frozenset(paths)
    records = preflight['source_identity_records']
    require(len(records) == 1419 and len({r['source_path'] for r in records}) == 1419,
            'Wrong source identity record count')
    included = {r['source_path'] for r in records if r['included_in_deletion_scope']}
    excluded = {r['source_path'] for r in records if not r['included_in_deletion_scope']}
    require(included == allowed and len(excluded) == 44 and not allowed & excluded, 'Scope/exclusions changed')
    require(sum(r['size_bytes'] for r in records if r['source_path'] in allowed) == BYTES, 'Wrong total bytes')
    for record in records:
        require(not record['identity_issues'], 'Preflight identity issue')
        verify_source(record)
    verify_archive(preflight['archive_target_records'])
    neighbors = parent_snapshot({str(Path(p).parent) for p in allowed})
    return preflight, allowed, records, neighbors, mount


def delete_exact(record, allowed):
    path = record['source_path']
    require(path in allowed and record['included_in_deletion_scope'], 'Deletion outside allowlist')
    p = source_path(path)
    fd = directory_fd(p.parent)
    try:
        verify_source(record, fd)
        os.unlink(p.name, dir_fd=fd)
    finally:
        os.close(fd)


def save_receipt(data, stream):
    stream.seek(0)
    json.dump(data, stream, indent=2, ensure_ascii=False)
    stream.write('\n')
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def execute(plan, approved_digest):
    require(approved_digest == LIST_SHA, 'Explicit pinned-list digest required')
    require(os.geteuid() == 0, 'Execution needs privilege only for remount and exact unlink')
    preflight, allowed, records, neighbors, initial_mount = plan
    with socket.socket() as s:
        require(s.connect_ex(('127.0.0.1', 8766)) != 0, 'Stop review workbench before execution')
    result = dict(started_at=now(), authorization_quote=AUTHORIZATION,
                  authorization_date='2026-09-22', deletion_authorized=True,
                  approved_list=str(LIST), approved_list_sha256='sha256:' + LIST_SHA,
                  preflight=str(PREFLIGHT), preflight_sha256='sha256:' + PREFLIGHT_SHA,
                  approved_count=COUNT, approved_bytes=BYTES, initial_mount=initial_mount,
                  source_hash_method=preflight['source_hash_method'],
                  coverage_trace_counts=preflight['coverage_trace_counts'],
                  excluded_paths=sorted(set(r['source_path'] for r in records) - allowed),
                  parent_entries_before=neighbors, deleted_files=[], errors=[],
                  result='in_progress', read_only_restored=False)
    with RECEIPT.open('x', encoding='utf-8') as out:
        save_receipt(result, out)
        def interrupted(signum, frame):
            raise InterruptedError('Interrupted by signal ' + str(signum))
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, interrupted)
        remount_attempted = False
        try:
            verify_inputs(preflight)
            mount_guard('ro')
            remount_attempted = True
            subprocess.run(['/usr/bin/mount', '-o', 'remount,rw', '/dev/sdf1', str(MOUNT)], check=True)
            result['writable_mount'] = mount_guard('rw')
            for record in records:
                if record['source_path'] not in allowed:
                    continue
                mount_guard('rw')
                # Fingerprint the two exact lists before each individual unlink.
                require(sha(LIST) == LIST_SHA and sha(W / 'drain-list.csv') == LIST_SHA,
                        'Drain list changed during execution')
                delete_exact(record, allowed)
                result['deleted_files'].append(dict(record, deleted_at=now(), deletion_authorized=True))
                save_receipt(result, out)
                if len(result['deleted_files']) % 100 == 0:
                    print('Deleted exact listed files:', len(result['deleted_files']), flush=True)
        except BaseException as exc:
            result['errors'].append(type(exc).__name__ + ': ' + str(exc))
        finally:
            # A second signal cannot interrupt the restoration attempt.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)
            try:
                if remount_attempted:
                    subprocess.run(['/usr/bin/mount', '-o', 'remount,ro', '/dev/sdf1', str(MOUNT)], check=True)
                result['final_mount'] = mount_guard('ro')
                result['read_only_restored'] = True
            except BaseException as exc:
                result['errors'].append('READ-ONLY RESTORATION: ' + str(exc))
            save_receipt(result, out)
        deleted = {r['source_path'] for r in result['deleted_files']}
        try:
            require(result['read_only_restored'], 'Source protection not restored')
            for r in records:
                if r['source_path'] in deleted:
                    require(not os.path.lexists(r['source_path']), 'Deleted path still exists')
                else:
                    verify_source(r)
            result['excluded_unchanged'] = 44
            verify_archive(preflight['archive_target_records'])
            result['archive_payloads_unchanged'] = len(preflight['archive_target_records'])
            verify_inputs(preflight)
            after = parent_snapshot(neighbors)
            verify_neighbors(neighbors, after, deleted)
            result['parent_entries_after'] = after
            result['unlisted_neighbor_entries_unchanged'] = sum(len(x) for x in after.values())
            result['all_directories_retained'] = True
            result['unlisted_files_deleted'] = 0
        except BaseException as exc:
            result['errors'].append('POSTCHECK: ' + str(exc))
        result['files_deleted'] = len(deleted)
        result['bytes_deleted'] = sum(r['size_bytes'] for r in result['deleted_files'])
        result['remaining_approved_paths'] = sorted(allowed - deleted)
        result['finished_at'] = now()
        result['result'] = 'passed' if not result['errors'] and deleted == allowed else 'stopped'
        save_receipt(result, out)
    print(json.dumps({k: result.get(k) for k in ['result','files_deleted','bytes_deleted',
          'excluded_unchanged','archive_payloads_unchanged','unlisted_neighbor_entries_unchanged',
          'unlisted_files_deleted','read_only_restored','errors']}, indent=2), flush=True)
    return 0 if result['result'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approved-digest')
    args = parser.parse_args()
    checked = validate_plan()
    print(json.dumps(dict(guards='passed', approved_files=COUNT, approved_bytes=BYTES,
                         exclusions=44, archive_payloads=739,
                         affected_parent_directories=len(checked[3]), deletion_enabled=args.execute)), flush=True)
    if args.execute:
        raise SystemExit(execute(checked, args.approved_digest))
