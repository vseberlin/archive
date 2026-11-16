#!/usr/bin/env python3
"""Move only the approved 85 Costa source files into the reversible work hold."""
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

WORK = Path('/mnt/volume/iss-archive-work')
HOLD = WORK / 'Costa-drain-hold-20261014'
UNIT = Path('/mnt/volume/iss-archive/units/archiv-wf')
TFA = Path('/mnt/volume/iss-archive/units/tfa')
PLAN = WORK / 'costa-work-hold-plan-20261014.csv'
PLAN_HASH = '13582ab2264fdc420428948c42ad8ce7005682e65d9cbce29eda844afd5cf79d'
ROOTS = [Path('/media/vladimir/Elements/restore/archive/MC_Matthias-Costa-Archiv'), Path('/media/vladimir/Elements/restore/archive/WF/Freistellen/MC_Matthias-Costa-Archiv')]
AUTHORITY = 'Vladimir requested a reversible move of the Costa drain files before permanent deletion and chose a hold folder on the work drive on 2026-10-14.'

def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def sig(p):
    s = p.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]

def mount(mode):
    raw = subprocess.check_output(['findmnt', '-rn', '-M', '/media/vladimir/Elements', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'], text=True).strip()
    a = raw.split()
    assert a[:3] == ['/dev/sdf1', '/media/vladimir/Elements', 'ntfs3'] and mode in a[3].split(','), raw
    return raw

def read_csv(p):
    with p.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def write_csv(p, rows):
    s = io.StringIO(newline='')
    w = csv.DictWriter(s, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    with p.open('w', encoding='utf-8', newline='') as f:
        f.write(s.getvalue())
        f.flush()
        os.fsync(f.fileno())

def source_sig(r):
    return [int(r['original_device']), int(r['original_inode']), int(r['size_bytes']), int(r['original_mtime_ns'])]

def confined(r):
    src, dst = Path(r['original_path']), Path(r['hold_path'])
    assert any(src.is_relative_to(root) for root in ROOTS)
    assert dst.parent == HOLD and not src.is_symlink() and not dst.is_symlink()
    return src, dst

def remove_originals(manifest_hash):
    # Privileged part is limited to the approved USB paths; it never writes archive records.
    assert os.geteuid() == 0 and sha(HOLD / 'MOVES.csv') == manifest_hash
    mount('rw')
    rows = read_csv(HOLD / 'MOVES.csv')
    assert len(rows) == 85 and len({r['original_path'] for r in rows}) == 85
    for r in rows:
        src, dst = confined(r)
        assert r['copied_and_verified'] == 'true' and r['source_sha256'] == r['verified_hold_sha256']
        assert sig(src) == source_sig(r)
        assert sig(dst) == json.loads(r['verified_hold_signature'])
    for r in rows:
        src, dst = confined(r)
        assert sig(src) == source_sig(r) and sig(dst) == json.loads(r['verified_hold_signature'])
        src.unlink()
        print(json.dumps({'usb_path_removed': str(src)}), flush=True)

def main():
    assert sha(PLAN) == PLAN_HASH and not HOLD.exists()
    mount('ro')
    rows = read_csv(PLAN)
    assert len(rows) == 85 and sum(int(r['size_bytes']) for r in rows) == 3694440021
    assert shutil.disk_usage(WORK).free > 5 * 1024**3
    media = read_csv(UNIT / 'CATALOG.csv')
    media_before = {r['final_relative_path']: sig(UNIT / r['final_relative_path']) for r in media}
    for r in rows:
        src, dst = confined(r)
        assert sig(src) == source_sig(r) and not dst.exists() and Path(r['final_archive_path']).is_file()
    HOLD.mkdir()
    write_csv(HOLD / 'MOVES.csv', rows)
    (HOLD / 'README.md').write_text('# Matthias Costa reversible drain hold\n\nThis folder holds only the 85 source files from the approved Costa drain list, totaling 3,694,440,021 bytes. Original files were copied unchanged and verified against their recorded SHA-256 before their original USB paths were removed as a reversible move. Archive Masters remain in Archiv WF and are not held here. Their descriptive metadata makes their byte hashes different from these original source files.\n\n`MOVES.csv` records each original USB path, current hold path, original source hash/size/modification time and completed move. `archive-root--` and `freistellen--` filenames distinguish the two source folders; `--JPGs_1000px--` preserves the small-export grouping without introducing subfolders.\n\n## Undo\n\nRestore exactly the files listed in MOVES.csv to their `original_path`. Verify each holding file against `source_sha256`; refuse any existing original target; temporarily make the exact Elements mount writable; copy each file back unchanged and restore `original_mtime_ns`; verify the restored bytes; return Elements to read-only. Keep the holding copies and this mapping until restoration has been checked. The original source directories are left in place.\n\nYou can request: “Restore the 85 Costa files from this holding folder to their original USB paths.” The original/current mapping and hashes make this restoration unambiguous.\n\nNo permanent deletion is authorised. No holding payload or archive Master may be deleted by this receipt.\n')
    for i, r in enumerate(rows, 1):
        mount('ro')
        src, dst = confined(r)
        before = sig(src)
        h = hashlib.sha256()
        with src.open('rb') as incoming, dst.open('xb') as outgoing:
            for block in iter(lambda: incoming.read(8 * 1024 * 1024), b''):
                h.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        shutil.copystat(src, dst)
        assert sig(src) == before == source_sig(r)
        assert h.hexdigest() == r['source_sha256'] and sha(dst) == r['source_sha256']
        assert dst.stat().st_size == int(r['size_bytes'])
        r.update(copied_and_verified='true', verified_hold_sha256=r['source_sha256'], verified_hold_signature=json.dumps(sig(dst)))
        print('HOLD_COPY_VERIFIED', i, '/85', dst.name, flush=True)
    write_csv(HOLD / 'MOVES.csv', rows)
    write_csv(WORK / 'costa-work-hold-moves-20261014.csv', rows)
    manifest_hash = sha(HOLD / 'MOVES.csv')
    # Copy verification is complete before any source path is removed.
    try:
        subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,rw', '/media/vladimir/Elements'], check=True)
        mount('rw')
        result = subprocess.run(['sudo', '-n', sys.executable, '-B', str(Path(__file__)), '--remove-originals', manifest_hash], capture_output=True, text=True)
        if result.returncode:
            print(result.stdout, result.stderr, flush=True)
        assert result.returncode == 0, 'Some source paths may have moved; use MOVES.csv and existing paths to reconcile, never replay blindly.'
    finally:
        subprocess.run(['sudo', '-n', 'mount', '-o', 'remount,ro', '/media/vladimir/Elements'], check=True)
        mount('ro')
        for r in rows:
            r['usb_path_removed'] = str(not Path(r['original_path']).exists()).lower()
        write_csv(HOLD / 'MOVES.csv', rows)
        write_csv(WORK / 'costa-work-hold-moves-20261014.csv', rows)
    assert all(r['usb_path_removed'] == 'true' and sig(Path(r['hold_path'])) == json.loads(r['verified_hold_signature']) for r in rows)
    assert set(p.name for p in HOLD.iterdir()) == {Path(r['hold_path']).name for r in rows} | {'MOVES.csv', 'README.md'}
    for path, before in media_before.items():
        assert sig(UNIT / path) == before
    # Record the current holding location without replacing original provenance paths.
    by_original = {r['original_path']: r for r in rows}
    connection = sqlite3.connect(UNIT / 'db-restore/RESTORE.sqlite')
    for name in ['source-disposition.csv', 'drain-list.csv', 'costa-drain-list-20261014.csv']:
        p = UNIT / '00_MANIFESTS' / name
        current = read_csv(p)
        for r in current:
            if r['source_path'] not in by_original:
                continue
            move = by_original[r['source_path']]
            r['drain_state'] = 'moved_to_work_hold_not_deleted'
            r['reason'] += ' Reversible move completed 2026-10-14; current source location: ' + move['hold_path'] + '. Original USB path retained as provenance; holding bytes verified unchanged. Permanent deletion not authorised.'
            if name == 'source-disposition.csv':
                assert connection.execute('UPDATE source_disposition SET drain_state=? WHERE candidate_id=?', (r['drain_state'], r['candidate_id'])).rowcount == 1
        write_csv(p, current)
    connection.commit()
    (UNIT / 'db-restore/RESTORE.sql').write_text('\n'.join(connection.iterdump()) + '\n')
    assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not connection.execute('PRAGMA foreign_key_check').fetchall()
    connection.close()
    for name in ['matthias-costa-drain-list-20261014.csv', 'matthias-costa-drain-proposal-20261014.csv']:
        p = WORK / name
        current = read_csv(p)
        for r in current:
            move = by_original[r['source_path']]
            r.update(current_hold_path=move['hold_path'], usb_path_removed='true', permanent_deletion_authorized='false')
        write_csv(p, current)
    tfa_source = TFA / 'db-restore/manifests/source-disposition.csv'
    tfa_rows = read_csv(tfa_source)
    tc = sqlite3.connect(TFA / 'db-restore/RESTORE.sqlite')
    for r in tfa_rows:
        if r['source_path'] in by_original:
            r['notes'] += ' Source moved reversibly to ' + by_original[r['source_path']]['hold_path'] + ' on 2026-10-14; original path retained as provenance. No permanent deletion.'
            assert tc.execute('UPDATE source_disposition SET notes=? WHERE source_path=?', (r['notes'], r['source_path'])).rowcount == 1
    tc.commit()
    write_csv(tfa_source, tfa_rows)
    (TFA / 'db-restore/RESTORE.sql').write_text('\n'.join(tc.iterdump()) + '\n')
    assert tc.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not tc.execute('PRAGMA foreign_key_check').fetchall()
    tc.close()
    write_csv(UNIT / '00_MANIFESTS/costa-hold-moves-20261014.csv', rows)
    meta_path = UNIT / 'metadata/matthias-costa-reconciliation.json'
    meta = json.loads(meta_path.read_text())
    meta['source_hold'] = {'authority': AUTHORITY, 'folder': str(HOLD), 'moves': str(UNIT / '00_MANIFESTS/costa-hold-moves-20261014.csv'), 'paths': 85, 'bytes': 3694440021, 'source_bytes_unchanged': True, 'original_usb_paths_removed': True, 'permanent_deletion_authorized': False}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
    readme = UNIT / '00_README.md'
    readme.write_text(readme.read_text() + '\n\n### Costa reversible source hold — 14 October 2026\n\nThe 85 Costa source files were moved to `/mnt/volume/iss-archive-work/Costa-drain-hold-20261014/` at Vladimir’s request before permanent deletion. All copies were hashed and verified before removing the original USB paths. `00_MANIFESTS/costa-hold-moves-20261014.csv` records exact original/holding paths, hashes, sizes and original timestamps for undo. The original directories remain on USB. Original source paths remain provenance fields; current holding locations are in the move mapping and source-disposition notes. All 163 archive media files are unchanged by this move. No holding file was deleted and no permanent deletion is authorised. Elements was returned to read-only.\n')
    changed = ['00_MANIFESTS/source-disposition.csv', '00_MANIFESTS/drain-list.csv', '00_MANIFESTS/costa-drain-list-20261014.csv', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql', 'metadata/matthias-costa-reconciliation.json', '00_README.md']
    checksums_path = UNIT / '00_MANIFESTS/payload-checksums.csv'
    checksums = read_csv(checksums_path)
    for r in checksums:
        if r['relative_path'] in changed:
            p = UNIT / r['relative_path']
            r.update(sha256=sha(p), size_bytes=str(p.stat().st_size))
    mapping = UNIT / '00_MANIFESTS/costa-hold-moves-20261014.csv'
    checksums.append({'relative_path': str(mapping.relative_to(UNIT)), 'sha256': sha(mapping), 'size_bytes': str(mapping.stat().st_size)})
    write_csv(checksums_path, checksums)
    receipt = {'result': 'ok', 'authority': AUTHORITY, 'hold_folder': str(HOLD), 'payload_files_moved': 85, 'payload_bytes': 3694440021, 'copy_hashes_verified': 85, 'original_usb_paths_removed': 85, 'source_directories_removed': 0, 'holding_payload_files_deleted': 0, 'permanent_deletion_authorized': False, 'archive_media_unchanged': 163, 'original_paths_preserved_in_mapping': True, 'source_mount_after': mount('ro'), 'mapping': str(mapping), 'undo': 'Restore the exact original paths from MOVES.csv using verified holding files; refuse overwrites; preserve recorded modification times. Holding copies remain available.'}
    data = json.dumps(receipt, ensure_ascii=False, indent=2) + '\n'
    (WORK / 'costa-work-hold-completion-20261014.json').write_text(data)
    (UNIT / '00_MANIFESTS/costa-work-hold-20261014.json').write_text(data)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--remove-originals':
        remove_originals(sys.argv[2])
    else:
        assert len(sys.argv) == 1
        main()
