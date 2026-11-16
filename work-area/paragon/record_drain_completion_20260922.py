"""Record the completed exact-list drain in existing Paragon records only.

No source write, mount change, deletion, new directory, or schema change.
"""
import argparse
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import execute_drain_20260922 as drain

W, U = drain.W, drain.U
REL = 'db-restore/manifests/drain-execution-20260922.json'


def rows(path):
    with path.open(newline='') as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def write_csv(path, fields, values):
    with path.open('w', newline='') as f:
        out = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        out.writeheader()
        out.writerows(values)


def write_json(path, value):
    with path.open('w') as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def db_snapshot(c, exclude_log=False):
    result = {}
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if exclude_log and name == 'logbook':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        data = sorted(repr(tuple(row)) for row in c.execute('SELECT * FROM ' + quoted))
        result[name] = hashlib.sha256('\n'.join(data).encode()).hexdigest()
    return result


def prepare():
    drain.mount_guard('ro')
    receipt = json.loads(drain.RECEIPT.read_text())
    assert receipt['result'] == 'passed' and receipt['read_only_restored'] and not receipt['errors']
    assert receipt['files_deleted'] == 1375 and receipt['bytes_deleted'] == drain.BYTES
    assert receipt['excluded_unchanged'] == 44 and receipt['unlisted_files_deleted'] == 0
    assert not (U / REL).exists() and 'completion_record' not in receipt, 'Already recorded; do not replay'
    preflight = json.loads(drain.PREFLIGHT.read_text())
    drain.verify_inputs(preflight)
    drain.verify_archive(preflight['archive_target_records'])
    fields, disposition = rows(W / 'source-disposition.planned.csv')
    raw = (W / 'drain-list.csv').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == drain.LIST_SHA
    approved = list(csv.DictReader(io.StringIO(raw.decode(), newline='')))
    deleted = {r['source_path']: r for r in receipt['deleted_files']}
    assert len(deleted) == 1375 and set(deleted) == {r['source_path'] for r in approved}
    for record in preflight['source_identity_records']:
        if record['source_path'] in deleted:
            assert not os.path.lexists(record['source_path'])
        else:
            drain.verify_source(record)
    drain.verify_neighbors(receipt['parent_entries_before'],
                           drain.parent_snapshot(receipt['parent_entries_before']), set(deleted))
    before = copy.deepcopy(disposition)
    for row in disposition:
        if row['source_path'] in deleted:
            row.update(drain_state='deleted', deletion_authorized='true',
                       notes='Deleted after explicit exact-list authorization on 2026-09-22. Receipt: ' + REL +
                             '. Earlier pre-deletion note: ' + row['notes'])
    assert sum(a != b for a, b in zip(before, disposition, strict=True)) == 1375
    assert [r for r in before if r['source_path'] not in deleted] == [r for r in disposition if r['source_path'] not in deleted]
    return receipt, preflight, fields, disposition, raw, deleted


def apply():
    receipt, preflight, fields, disposition, raw, deleted = prepare()
    at = drain.now()
    log_id = 'LOG-' + uuid4().hex.upper()
    original_dirs = sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())
    original_files = {str(p.relative_to(U)): [p.stat().st_size, p.stat().st_mtime_ns]
                      for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))}
    archive_controls = [REL, 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
                        'db-restore/manifests/source-disposition.csv', 'db-restore/manifests/drain-list.csv',
                        'db-restore/manifests/drain-evidence.json', 'db-restore/export-summary.json', '00_README.md']
    # Keep the exact approved input bytes inside its receipt before marking rows completed.
    receipt['approved_list_csv_utf8'] = raw.decode('utf-8')
    receipt['completion_record'] = dict(recorded_at=at, log_id=log_id, state='recording',
        current_drain_list_meaning='1375 completed rows marked deleted; zero candidates remain. Original approved CSV bytes are included in this receipt.',
        current_source_disposition=dict(total=1419, deleted=1375, candidates=0, retained_other_unit=44),
        original_archive_directories=original_dirs, original_archive_file_stamps=original_files)
    write_json(drain.RECEIPT, receipt)
    write_json(U / REL, receipt)
    after = dict(files_deleted=1375, bytes_deleted=drain.BYTES, excluded_unchanged=44,
                 unlisted_files_deleted=0, source_mount_read_only=True,
                 approved_list_sha256='sha256:' + drain.LIST_SHA, receipt=REL)
    log = (log_id, at, 'Vladimir', 'WF-PARAGON', 'drain_sources', 'archive_unit', 'WF-PARAGON',
           json.dumps(dict(candidate_files=1375, excluded_files=44)),
           json.dumps(after, ensure_ascii=False), REL, drain.AUTHORIZATION)
    db_results = {}
    for path in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with sqlite3.connect(path) as c:
            c.execute('PRAGMA foreign_keys=ON')
            before = db_snapshot(c, exclude_log=True)
            old_logs = c.execute('SELECT count(*) FROM logbook').fetchone()[0]
            c.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', log)
            assert before == db_snapshot(c, exclude_log=True)
            assert c.execute('SELECT count(*) FROM logbook').fetchone()[0] == old_logs + 1
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            db_results[str(path)] = dict(non_log_tables_unchanged=True, quick_check='ok', foreign_key_issues=0)
    with sqlite3.connect('file:' + str(U / 'db-restore/RESTORE.sqlite') + '?mode=ro', uri=True) as c:
        sql = '\n'.join(c.iterdump()) + '\n'
        expected = db_snapshot(c)
    with sqlite3.connect(':memory:') as c:
        c.executescript(sql)
        assert db_snapshot(c) == expected
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    (U / 'db-restore/RESTORE.sql').write_bytes(sql.encode('utf-8'))
    for p in [W / 'source-disposition.planned.csv', U / 'db-restore/manifests/source-disposition.csv']:
        write_csv(p, fields, disposition)
    completed = [r for r in disposition if r['source_path'] in deleted]
    for p in [W / 'drain-list.csv', U / 'db-restore/manifests/drain-list.csv']:
        write_csv(p, fields, completed)
    evidence = json.loads((W / 'drain-evidence.json').read_text())
    old_excluded_evidence = [copy.deepcopy(r) for r in evidence if r['source_path'] not in deleted]
    for row in evidence:
        if row['source_path'] in deleted:
            row.update(source_deleted=True, deleted_at=deleted[row['source_path']]['deleted_at'],
                       deletion_authorized=True, drain_state='deleted', drain_execution_receipt=REL,
                       drain_execution_log_id=log_id)
    assert old_excluded_evidence == [r for r in evidence if r['source_path'] not in deleted]
    for p in [W / 'drain-evidence.json', U / 'db-restore/manifests/drain-evidence.json']:
        write_json(p, evidence)
    summary = json.loads((W / 'drain-review-summary.json').read_text())
    summary.update(updated_at=at, candidate_rows=0, candidate_source_bytes=0, deleted_rows=1375,
        deleted_source_bytes=drain.BYTES, retain_rows=44, deletion_authorized=True,
        sources_modified=True, source_modified=True, source_deletion_performed=True,
        other_unit_files_changed=False, drain_execution_receipt=REL,
        current_register='Drain complete: 1375 listed sources deleted; 0 candidates remain; 44 other-unit paths retained unchanged. See execution receipt; earlier review checkpoints are historical.')
    write_json(W / 'drain-review-summary.json', summary)
    note = ('## Paragon drain completed — 22 September 2026\n\n'
            '**Deleted exactly 1,375 approved source files (22,986,884,818 bytes). Zero drain candidates remain.** '
            'All 44 excluded other-unit files remain unchanged. No unlisted file or directory was deleted. '
            'The exact Elements mount is restored to read-only. All 739 checked archive payloads are unchanged; '
            'Master selections, metadata and relationships are unchanged.\n\n'
            'Receipt: `' + REL + '`. Work copy: `paragon/drain-execution-20260922.json`. '
            'The work and archive drain lists retain the 1,375 completed rows with `drain_state=deleted`; '
            'the original approved CSV bytes and SHA-256 are preserved in the receipt. '
            'The 1,419-row source-disposition registers now show 1,375 deleted and 44 retained. '
            'SQLite logbooks and readable records agree.\n\n'
            'The source-review workbench on port 8766 is stopped. **Do not rerun its source-existence checks, '
            'completed accession/registration jobs, the pre-deletion checker, or the drain executor.** '
            'The execution script refuses replay. The preflight and earlier authorization=false statements '
            'are historical snapshots superseded by this explicit drain authorization and receipt. '
            'Separate catalogue issues remain outside this completed source drain; no whole-unit completion is implied.\n\n'
            'Earlier checkpoints below are historical.\n\n---\n\n')
    for p in [W / '00_README.md', U / '00_README.md', W.parent / 'HANDOFF.md']:
        p.write_text(note + p.read_text())
    fields_checks, old_checks = rows(U / 'db-restore/checksums.csv')
    checks = {r['path']: r for r in old_checks}
    export = json.loads((U / 'db-restore/export-summary.json').read_text())
    export.update(updated_at=at, source_disposition_review=dict(total=1419, candidate_rows=0,
        deleted_rows=1375, retained_rows=44, pending_paths=0, excluded_paths=44, receipt=REL),
        source_drain=dict(completed_at=receipt['finished_at'], files_deleted=1375,
                          bytes_deleted=drain.BYTES, receipt=REL),
        checksums=len(set(checks) | set(archive_controls)))
    write_json(U / 'db-restore/export-summary.json', export)
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()) == original_dirs
    for rel, original in original_files.items():
        if rel not in archive_controls and rel != 'db-restore/checksums.csv':
            p = U / rel
            assert [p.stat().st_size, p.stat().st_mtime_ns] == original, rel
    assert set(str(p.relative_to(U)) for p in U.rglob('*') if p.is_file() and
               not p.name.endswith(('-wal','-shm','-journal'))) == set(original_files) | {REL}
    drain.verify_archive(preflight['archive_target_records'])
    drain.mount_guard('ro')
    receipt['completion_record'].update(state='passed', sqlite_checks=db_results,
        sql_rebuild_all_tables_equal=True, archive_structure_unchanged=True,
        excluded_disposition_and_evidence_unchanged=True, zero_drain_candidates=True,
        archive_payloads_unchanged=739, workbench_stopped=True)
    for p in [drain.RECEIPT, U / REL]:
        write_json(p, receipt)
    for rel in archive_controls:
        p = U / rel
        checks[rel] = dict(path=rel, sha256='sha256:' + drain.sha(p), size_bytes=str(p.stat().st_size))
    write_csv(U / 'db-restore/checksums.csv', fields_checks, sorted(checks.values(), key=lambda r:r['path']))
    for rel in archive_controls:
        assert checks[rel]['sha256'] == 'sha256:' + drain.sha(U / rel)
    for work, final in [('source-disposition.planned.csv','source-disposition.csv'),
                        ('drain-list.csv','drain-list.csv'), ('drain-evidence.json','drain-evidence.json'),
                        (drain.RECEIPT.name, drain.RECEIPT.name)]:
        assert (W / work).read_bytes() == (U / 'db-restore/manifests' / final).read_bytes()
    assert hashlib.sha256(receipt['approved_list_csv_utf8'].encode('utf-8')).hexdigest() == drain.LIST_SHA
    report = ('# Paragon drain completed — 22 September 2026\n\n'
              '- Deleted: **1,375 exact approved files**, **22,986,884,818 bytes**.\n'
              '- Unlisted files deleted: **0**. Directories deleted: **0**.\n'
              '- Other-unit exclusions: **44 files verified unchanged**.\n'
              '- Unlisted entries in the 30 affected parent directories: **' + str(receipt['unlisted_neighbor_entries_unchanged']) + ' verified unchanged**.\n'
              '- Archive payloads: **739 verified unchanged using stored hashes and matching size/mtime evidence**.\n'
              '- Source protection restored: `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`, UUID `6CAAB316AAB2DC30`.\n'
              '- Work/archive registers agree: **1,375 deleted, 44 excluded, 0 candidates**.\n'
              '- Both SQLite logbooks contain the same completion record; all other database tables unchanged. SQL export rebuilt and checked.\n'
              '- Source-review workbench stopped; completed jobs must not be replayed.\n\n'
              'Approved-list SHA-256: `' + drain.LIST_SHA + '`. Original CSV bytes are embedded in '
              '[the execution receipt](drain-execution-20260922.json), alongside per-file identity and deletion times.\n\n'
              'The approved scope included 149 previously disclosed intentional-discard cases without established archive coverage. '
              'This drain does not make a new preservation claim for them. Separate catalogue issues are outside this completed source drain.\n')
    (W / 'DRAIN_COMPLETED_20260922.md').write_text(report)
    print(json.dumps(dict(recorded='passed', deleted=1375, excluded=44, candidates=0,
                         archive_payloads_unchanged=739, sql_tables_rebuilt=len(expected),
                         checksum_entries=len(checks), receipt=str(drain.RECEIPT)), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply()
    else:
        prepare()
        print('Completion bookkeeping precheck passed; no records changed.')
