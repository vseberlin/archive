"""Register only the saved 21 September drain decisions; preserve all media."""
import copy
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import paragon_workbench as app
import retained_candidates as review
from reconcile_drain_decisions import read_csv, write_csv, stamp, connect, snapshot, counters

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/final-drain-registration-20260921.json'
FINAL_LIST = 'db-restore/manifests/drain-list.csv'
AUTHORIZATION = 'Vladimir: ok done. lets finalize accession and form final drain list'
CONTROLS = ['db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/source-disposition.csv', 'db-restore/manifests/drain-evidence.json',
    'db-restore/export-summary.json', '00_README.md', FINAL_LIST, RECEIPT]


def digest(path):
    with path.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()


def prepare():
    app.mount_guard()
    assert not (U / RECEIPT).exists() and not (W / Path(RECEIPT).name).exists(), 'Inspect existing receipt; do not replay.'
    review.verify_evidence()
    with app.connect() as con:
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
        proposals = review.drain_proposals(con)
        assert proposals == review.readjson(review.DRAIN_PROPOSALS)
        saved = review.saved_reviews(con)
        logs = {json.loads(row['after_json'])['case_key']: dict(row) for row in con.execute(
            'SELECT * FROM logbook WHERE action=? ORDER BY at_utc,log_id', (review.ACTION,))
            if json.loads(row['after_json']).get('source_report') == review.REPORT.name}
    fields, rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W / 'drain-evidence.json').read_bytes() == (U / 'db-restore/manifests/drain-evidence.json').read_bytes()
    assert len(rows) == 1419 and counters(rows)['candidate_rows'] == 942
    assert read_csv(W / 'drain-list.csv')[1] == [r for r in rows if r['drain_state'] == 'candidate_after_separate_review']
    evidence = review.readjson(W / 'drain-evidence.json')
    evidence_by = {e['source_path']: e for e in evidence}
    by_path = {r['source_path']: r for r in rows}
    assert len(by_path) == len(evidence_by) == len(rows)
    source_stamps = {}
    for path, row in by_path.items():
        p = Path(path)
        assert p.resolve(strict=True) == p and p.is_relative_to(review.SOURCE)
        source_stamps[path] = stamp(p)
        e = evidence_by[path]
        assert source_stamps[path] == [e['source_size_bytes'], e['source_mtime_ns']], path
        assert row['source_sha256'] == e['source_sha256'] and row['deletion_authorized'] == 'false'
    ownership = {f['path']: (r, f) for r in saved.values() for f in r['files']}
    pending = [p for p in proposals['files'] if not p['in_drain_register']]
    assert Counter(p['disposition'] for p in pending) == {'redundant': 220, 'noise_drain': 3}
    updates = []
    for p in pending:
        path = p['path']; decision, file = ownership[path]
        assert file['state'] == p['disposition'] and file['sha256'] == p['sha256'] == by_path[path]['source_sha256']
        assert not p['awaiting_accession'] and not p['deletion_authorized']
        asset = review.evidence()['assets'][path]
        review.verify_asset(asset)
        assert not asset.get('other_unit_links'), path
        log = logs[decision['case_key']]
        assert json.loads(log['after_json']) == decision
        before = by_path[path]; after = copy.deepcopy(before)
        assert before['drain_state'] != 'candidate_after_separate_review'
        after.update(source_role='redundant_candidate' if p['disposition'] == 'redundant' else 'noise_drain_candidate',
            preservation_status='reviewed_not_selected', exact_copy_proven='false',
            drain_state='candidate_after_separate_review', deletion_authorized='false',
            preservation_basis='Saved path-specific picture review under Vladimir\u2019s quality and identity rules. ' + file.get('note', p['reason']),
            notes='Registered under explicit 21 September finalization instruction. Review: ' + log['log_id'] +
                '. Receipt: ' + RECEIPT + '. Redundant means not selected; no deletion or accession-coverage claim. Prior note: ' + before['notes'])
        updates.append(dict(source_path=path, before=before, after=after, saved_file_decision=file,
            review_case_key=decision['case_key'], decision_log_id=log['log_id'], reviewed_at=decision['reviewed_at']))
    retained_states = Counter(f['state'] for r in saved.values() for f in r['files'])
    return dict(fields=fields, rows=rows, evidence=evidence, updates=updates, source_stamps=source_stamps,
        decision_bytes=review.DECISIONS.read_bytes(), stable_review_stamp=stamp(W / 'retain-review.csv'), retained_states=dict(retained_states))


def verify():
    app.mount_guard()
    receipt = review.readjson(U / RECEIPT)
    assert receipt == review.readjson(W / Path(RECEIPT).name)
    fields, rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    candidates = [r for r in rows if r['drain_state'] == 'candidate_after_separate_review']
    assert counters(rows)['candidate_rows'] == 1165 and len(rows) == 1419
    assert read_csv(W / 'drain-list.csv')[1] == read_csv(U / FINAL_LIST)[1] == candidates
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W / 'drain-evidence.json').read_bytes() == (U / 'db-restore/manifests/drain-evidence.json').read_bytes()
    assert all(r['deletion_authorized'] == 'false' for r in rows)
    for path, old in receipt['source_stamps'].items(): assert stamp(Path(path)) == old, path
    logs = []
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
            logs.append(dict(con.execute('SELECT * FROM logbook WHERE log_id=?', (receipt['log_id'],)).fetchone()))
    assert logs[0] == logs[1]
    checks = {r['path']: r for r in read_csv(U / 'db-restore/checksums.csv')[1]}
    for relative in CONTROLS:
        assert checks[relative]['sha256'] == digest(U / relative), relative
    review.evidence.cache_clear()
    with app.connect() as con:
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
        proposals = review.drain_proposals(con)
        assert proposals == review.readjson(review.DRAIN_PROPOSALS)
        assert all(p['in_drain_register'] for p in proposals['files'])
    return dict(ok=True, added=223, drain_candidates=1165, excluded_source_paths=254,
        pending_saved_drain_proposals=0, accession_performed=False, deletion_authorized=False)


def apply():
    p = prepare()
    now = datetime.now(timezone.utc).isoformat()
    original_dirs = sorted(str(x.relative_to(U)) for x in U.rglob('*') if x.is_dir())
    original_files = {str(x.relative_to(U)): stamp(x) for x in U.rglob('*') if x.is_file() and not x.name.endswith(('-wal', '-shm', '-journal'))}
    receipt = dict(authorization=AUTHORIZATION, unit='paragon', started_at=now, result='pending',
        updates=p['updates'], source_stamps=p['source_stamps'], log_id='LOG-' + uuid4().hex.upper(),
        accession_performed=False, derivatives_generated=0, source_files_rehashed=0,
        source_modified=False, deletion_authorized=False,
        retained_decision_counts=p['retained_states'],
        scope='Final registration of currently saved drain decisions only. Retained candidates remain excluded; accession is not complete.')
    review.write_json(W / Path(RECEIPT).name, receipt)
    log = (receipt['log_id'], now, 'Vladimir', 'WF-PARAGON', 'register_reviewed_picture_drain',
        'archive_unit', 'WF-PARAGON', '{}', json.dumps(dict(updates=p['updates'], receipt=RECEIPT,
        deletion_authorized=False), ensure_ascii=False), RECEIPT, AUTHORIZATION)
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with connect(db, True) as con:
            before = snapshot(con)
            con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', log)
            assert snapshot(con) == before
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
    with connect(U / 'db-restore/RESTORE.sqlite') as con:
        sql = '\n'.join(con.iterdump()) + '\n'; expected = snapshot(con)
    memory = sqlite3.connect(':memory:'); memory.executescript(sql)
    assert snapshot(memory) == expected and memory.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not memory.execute('PRAGMA foreign_key_check').fetchall(); memory.close()
    (U / 'db-restore/RESTORE.sql').write_text(sql)
    updates = {u['source_path']: u for u in p['updates']}
    rows = [updates[r['source_path']]['after'] if r['source_path'] in updates else r for r in p['rows']]
    assert sum(a != b for a, b in zip(rows, p['rows'], strict=True)) == 223
    for path in [W / 'source-disposition.planned.csv', U / 'db-restore/manifests/source-disposition.csv']:
        write_csv(path, p['fields'], rows)
    candidates = [r for r in rows if r['drain_state'] == 'candidate_after_separate_review']
    for path in [W / 'drain-list.csv', U / FINAL_LIST]: write_csv(path, p['fields'], candidates)
    for e in p['evidence']:
        if e['source_path'] in updates:
            change = updates[e['source_path']]
            e.update(drain_decision_receipt=RECEIPT, drain_decision_log_id=receipt['log_id'],
                current_review_log_ids=[change['decision_log_id']], saved_file_decision=change['saved_file_decision'],
                preservation_status='reviewed_not_selected', deletion_authorized=False)
    for path in [W / 'drain-evidence.json', U / 'db-restore/manifests/drain-evidence.json']:
        review.write_json(path, p['evidence'])
    stats = counters(rows)
    assert stats['candidate_rows'] == 1165 and stats['retain_rows'] == 254
    summary = review.readjson(W / 'drain-review-summary.json')
    summary.update(stats, updated_at=now, final_picture_drain_receipt=RECEIPT,
        current_register='Work and final registers agree: 1165 drain candidates and 254 excluded source paths. Accession of retained candidates remains incomplete. No deletion authorized.')
    review.write_json(W / 'drain-review-summary.json', summary)
    title, body = (U / '00_README.md').read_text().split('\n', 1)
    (U / '00_README.md').write_text(title + '\n\n## Current source register — 21 September 2026\n\n'
        'Registered 223 saved picture decisions (220 redundant, 3 noise/drain). The current\n'
        'list contains 1,165 candidates; 254 source paths remain excluded. Accession of\n'
        'retained candidates is not complete. No source files, archive media, Master\n'
        'choices, object metadata or relationships changed in this registration.\n'
        'Redundant means not selected; deletion is not authorized.\n\n'
        'Current list: `' + FINAL_LIST + '`. Receipt: `' + RECEIPT + '`.\n'
        'Earlier counts below are historical.\n\n' + body)
    headers, old_checks = read_csv(U / 'db-restore/checksums.csv'); checks = {r['path']: r for r in old_checks}
    export = review.readjson(U / 'db-restore/export-summary.json')
    export.update(updated_at=now, source_disposition_review=dict(total=1419, candidate_rows=1165,
        retained_rows=254, receipt=RECEIPT), checksums=len(set(checks) | set(CONTROLS)), final_picture_drain_receipt=RECEIPT)
    review.write_json(U / 'db-restore/export-summary.json', export)
    assert sorted(str(x.relative_to(U)) for x in U.rglob('*') if x.is_dir()) == original_dirs
    for relative, old in original_files.items():
        if relative not in set(CONTROLS) | {'db-restore/checksums.csv'}: assert stamp(U / relative) == old, relative
    assert review.DECISIONS.read_bytes() == p['decision_bytes']
    assert stamp(W / 'retain-review.csv') == p['stable_review_stamp']
    for path, old in p['source_stamps'].items(): assert stamp(Path(path)) == old, path
    receipt.update(result='passed', completed_at=datetime.now(timezone.utc).isoformat(), source_register=stats,
        sql_restore_verified=True, archive_media_metadata_and_catalogues_unchanged=True, directory_structure_unchanged=True)
    for path in [W / Path(RECEIPT).name, U / RECEIPT]: review.write_json(path, receipt)
    for relative in CONTROLS:
        checks[relative] = dict(path=relative, sha256=digest(U / relative), size_bytes=str((U / relative).stat().st_size))
    write_csv(U / 'db-restore/checksums.csv', headers, sorted(checks.values(), key=lambda r: r['path']))
    review.evidence.cache_clear()
    with app.connect() as con: review.export_decisions(con)
    assert review.DECISIONS.read_bytes() == p['decision_bytes']
    print(json.dumps(verify(), indent=2))


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--apply']: apply()
    elif sys.argv[1:] == ['--verify']: print(json.dumps(verify(), indent=2))
    else:
        p = prepare()
        print(json.dumps(dict(ready=True, added=len(p['updates']), drain_before=942,
            drain_after=1165, excluded_paths=254, source_stamps_verified=len(p['source_stamps']), deletion_authorized=False)))
