#!/usr/bin/env python3
"""Register the 42 explicitly reviewed JPEG alternatives; delete nothing."""
import argparse
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import paragon_workbench as app
import retained_candidates as review
from reconcile_drain_decisions import read_csv, write_csv, stamp, connect, snapshot, counters

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/reviewed-jpeg-drain.json'
AUTHORIZATION = 'ok register redundant jpegs'
CONTROLS = ['db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/source-disposition.csv', 'db-restore/manifests/drain-evidence.json',
    'db-restore/export-summary.json', '00_README.md', RECEIPT]


def digest(path):
    with path.open('rb') as source:
        return 'sha256:' + hashlib.file_digest(source, 'sha256').hexdigest()


def plan():
    app.mount_guard()
    assert not (U / RECEIPT).exists() and not (W / Path(RECEIPT).name).exists(), 'Already started or completed; inspect receipt, do not replay.'
    review.verify_evidence()
    with app.connect() as con:
        saved = review.saved_reviews(con)
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
        proposals = review.drain_proposals(con)
        assert proposals == review.readjson(review.DRAIN_PROPOSALS)
        pending = [f for f in proposals['files'] if not f['in_drain_register']]
        logs = {json.loads(row['after_json'])['case_key']: dict(row) for row in con.execute(
            'SELECT * FROM logbook WHERE action=? ORDER BY at_utc,log_id', (review.ACTION,))
            if json.loads(row['after_json']).get('source_report') == review.REPORT.name}
    fields, rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W / 'drain-evidence.json').read_bytes() == (U / 'db-restore/manifests/drain-evidence.json').read_bytes()
    assert len(rows) == 1419 and counters(rows)['candidate_rows'] == 900
    assert all(row['deletion_authorized'] == 'false' for row in rows)
    by_path = {row['source_path']: row for row in rows}
    assert len(by_path) == len(rows)
    assert read_csv(W / 'drain-list.csv')[1] == [r for r in rows if r['drain_state'] == 'candidate_after_separate_review']
    decisions = {r['source_version_pair']['source_path']: r for r in saved.values() if r.get('source_version_pair')}
    paths = {f['path'] for f in pending}
    assert len(pending) == len(paths) == len(decisions) == 42
    assert paths == set(decisions) == set(review.hires_pairs())
    updates, sources = [], {}
    for proposal in pending:
        path = proposal['path']; decision = decisions[path]
        selected = decision['selected_master_path']; pair = decision['source_version_pair']
        assert Path(path).suffix.lower() == '.jpg' and Path(selected).suffix.lower() in {'.tif', '.tiff'}
        assert pair['target_path'] == selected and decision['same_object_confirmed'] is True
        assert proposal['disposition'] == 'redundant' and not proposal['deletion_authorized']
        masters = [f for f in decision['files'] if f['state'] == 'selected_master']
        assert len(masters) == 1 and masters[0]['path'] == selected
        assert selected not in paths and by_path[selected]['drain_state'] == 'retain_not_preserved'
        assert by_path[path]['drain_state'] == 'retain_not_preserved'
        for source_path, expected_hash in [(path, proposal['sha256']), (selected, masters[0]['sha256'])]:
            asset = review.evidence()['assets'][source_path]
            review.verify_asset(asset)
            assert asset['sha256'] == expected_hash == by_path[source_path]['source_sha256']
            sources[source_path] = dict(sha256=expected_hash, stamp=stamp(Path(source_path)))
        log = logs[decision['case_key']]
        assert json.loads(log['after_json']) == decision
        before = by_path[path]; after = copy.deepcopy(before)
        after.update(source_role='redundant_candidate', final_path='', final_sha256='',
            preservation_status='reviewed_not_selected', exact_copy_proven='false',
            preservation_basis='Human-confirmed same item and full frame: Vladimir selected the TIFF Master and marked this JPEG redundant. Registration explicitly authorized; accession remains separate.',
            drain_state='candidate_after_separate_review', deletion_authorized='false',
            notes='Selected retained TIFF: ' + selected + '; ' + masters[0]['sha256'] +
                  '. Saved decision: ' + log['log_id'] + '. Registration: ' + RECEIPT +
                  '. No accession, exact-byte equivalence or deletion claim. Earlier note: ' + before['notes'])
        updates.append(dict(source_path=path, before=before, after=after,
            case_key=decision['case_key'], decision_log_id=log['log_id'], reviewed_at=decision['reviewed_at'],
            selected_master_path=selected, selected_master_sha256=masters[0]['sha256'],
            same_object_confirmed=True, comparison_only=False, deletion_authorized=False))
    assert len({u['selected_master_path'] for u in updates}) == 42
    return dict(fields=fields, rows=rows, saved=saved, updates=updates, source_evidence=sources,
        decision_bytes=review.DECISIONS.read_bytes(), stable_review_stamp=stamp(W / 'retain-review.csv'))


def verify():
    app.mount_guard()
    receipt = review.readjson(U / RECEIPT)
    assert receipt == review.readjson(W / Path(RECEIPT).name) and receipt['result'] == 'passed'
    rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')[1]
    by_path = {row['source_path']: row for row in rows}
    assert len(rows) == len(by_path) == 1419 and counters(rows)['candidate_rows'] == 942
    assert all(row['deletion_authorized'] == 'false' for row in rows)
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W / 'drain-evidence.json').read_bytes() == (U / 'db-restore/manifests/drain-evidence.json').read_bytes()
    assert read_csv(W / 'drain-list.csv')[1] == [r for r in rows if r['drain_state'] == 'candidate_after_separate_review']
    with app.connect() as con:
        saved = review.saved_reviews(con)
        assert review.readable_decisions(con) == review.readjson(review.DECISIONS)
        proposals = review.drain_proposals(con)
        assert proposals == review.readjson(review.DRAIN_PROPOSALS)
        assert len(proposals['files']) == 619 and all(f['in_drain_register'] for f in proposals['files'])
        assert review.hires_index(con)['remaining'] == 0
    for update in receipt['updates']:
        assert by_path[update['source_path']] == update['after']
        assert by_path[update['selected_master_path']]['drain_state'] == 'retain_not_preserved'
        decision = saved[update['case_key']]
        assert decision['selected_master_path'] == update['selected_master_path']
        assert decision['reviewed_at'] == update['reviewed_at']
        assert sum(f['state'] == 'selected_master' for f in decision['files']) == 1
    for path, evidence in receipt['source_evidence'].items():
        assert stamp(Path(path)) == evidence['stamp']
    log_rows = []
    for path in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        con = connect(path)
        assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        log_rows.append(dict(con.execute('SELECT * FROM logbook WHERE log_id=?', (receipt['log_id'],)).fetchone()))
        con.close()
    assert log_rows[0] == log_rows[1]
    assert json.loads(log_rows[0]['after_json'])['updates'] == receipt['updates']
    checks = {row['path']: row for row in read_csv(U / 'db-restore/checksums.csv')[1]}
    for relative in CONTROLS:
        assert checks[relative]['sha256'] == digest(U / relative)
        assert int(checks[relative]['size_bytes']) == (U / relative).stat().st_size
    return dict(ok=True, jpegs_registered=42, selected_tiffs_retained=42,
        drain_candidates=942, paths_outside_drain=477, pending_drain=0,
        decisions_unchanged=True, registers_agree=True, deletion_authorized=False,
        source_modified=False, accession_performed=False)


def apply():
    p = plan()
    original_files = {str(f.relative_to(U)): stamp(f) for f in U.rglob('*')
        if f.is_file() and not f.name.endswith(('-wal', '-shm', '-journal'))}
    original_directories = {str(f.relative_to(U)) for f in U.rglob('*') if f.is_dir()}
    before_models, before_reviews, before_counts = {}, {}, {}
    for path in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        con = connect(path)
        before_models[path] = snapshot(con); before_reviews[path] = review.saved_reviews(con)
        before_counts[path] = con.execute('SELECT count(*) FROM logbook').fetchone()[0]
        con.close()
    assert before_reviews[W / 'work/catalog.sqlite'] == p['saved']
    now = datetime.now(timezone.utc).isoformat()
    receipt = dict(authorization=AUTHORIZATION, unit='paragon', result='pending', completed_at=now,
        updates=p['updates'], source_evidence=p['source_evidence'], log_id='LOG-' + uuid4().hex.upper(),
        master_choices_changed=False, accession_performed=False, derivatives_generated=0,
        source_modified=False, source_files_rehashed=0, deletion_authorized=False)
    review.write_json(W / Path(RECEIPT).name, receipt)
    log = (receipt['log_id'], now, 'Vladimir', 'WF-PARAGON', 'register_reviewed_jpegs',
        'archive_unit', 'WF-PARAGON', '{}', json.dumps(dict(updates=p['updates'], receipt=RECEIPT,
        deletion_authorized=False), ensure_ascii=False), RECEIPT, AUTHORIZATION)
    for path in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        con = connect(path, True)
        with con:
            con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)', log)
            assert snapshot(con) == before_models[path]
            assert review.saved_reviews(con) == before_reviews[path]
            assert con.execute('SELECT count(*) FROM logbook').fetchone()[0] == before_counts[path] + 1
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
        con.close()
    con = connect(U / 'db-restore/RESTORE.sqlite')
    sql = '\n'.join(con.iterdump()) + '\n'; con.close()
    restored = sqlite3.connect(':memory:')
    restored.executescript(sql)
    assert restored.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not restored.execute('PRAGMA foreign_key_check').fetchall()
    assert snapshot(restored) == before_models[U / 'db-restore/RESTORE.sqlite']
    restored.close()
    (U / 'db-restore/RESTORE.sql').write_text(sql)
    updates = {u['source_path']: u for u in p['updates']}
    rows = [updates[r['source_path']]['after'] if r['source_path'] in updates else r for r in p['rows']]
    assert sum(a != b for a, b in zip(rows, p['rows'], strict=True)) == 42
    for path in [W / 'source-disposition.planned.csv', U / 'db-restore/manifests/source-disposition.csv']:
        write_csv(path, p['fields'], rows)
    write_csv(W / 'drain-list.csv', p['fields'], [r for r in rows if r['drain_state'] == 'candidate_after_separate_review'])
    evidence = review.readjson(W / 'drain-evidence.json')
    for item in evidence:
        if item['source_path'] in updates:
            change = updates[item['source_path']]
            item.update(drain_decision_receipt=RECEIPT, drain_decision_log_id=receipt['log_id'],
                current_review_log_ids=[change['decision_log_id']],
                covering_source=dict(path=change['selected_master_path'], sha256=change['selected_master_sha256'],
                    kind='human_selected_retained_tiff', awaiting_accession=True),
                final_path='', final_sha256='', preservation_status='reviewed_not_selected')
    for path in [W / 'drain-evidence.json', U / 'db-restore/manifests/drain-evidence.json']:
        review.write_json(path, evidence)
    stats = counters(rows)
    assert stats['candidate_rows'] == 942 and stats['retain_rows'] == 477
    summary = review.readjson(W / 'drain-review-summary.json')
    summary.update(stats); summary.update(updated_at=now, reviewed_jpeg_drain_receipt=RECEIPT,
        current_register='Work and final registers agree: 942 drain candidates and 477 paths outside drain. No deletion authorized.')
    review.write_json(W / 'drain-review-summary.json', summary)
    title, body = (U / '00_README.md').read_text().split('\n', 1)
    (U / '00_README.md').write_text(title + '\n\n## Current source register — reviewed JPEGs, 20 September 2026\n\n'
        'Registered the 42 JPEG alternatives from Vladimir\u2019s completed same-item/full-frame\n'
        'reviews. Their 42 selected source TIFFs remain outside the drain list and await\n'
        'separate accession decisions. Current register: 942 candidates and 477 other\n'
        'source paths. No Master choice, object identity, metadata, relationship, archive\n'
        'media or source file changed. Deletion remains unauthorized. Receipt: `' + RECEIPT +
        '`. Earlier counts below are historical.\n\n' + body)
    fields, checkrows = read_csv(U / 'db-restore/checksums.csv')
    checks = {r['path']: r for r in checkrows}
    export = review.readjson(U / 'db-restore/export-summary.json')
    export.update(updated_at=now, source_disposition_review=dict(total=1419, candidate_rows=942,
        retained_rows=477, receipt=RECEIPT), checksums=len(set(checks) | set(CONTROLS)),
        reviewed_jpeg_drain_receipt=RECEIPT)
    review.write_json(U / 'db-restore/export-summary.json', export)
    for path, old in original_files.items():
        if path not in set(CONTROLS) | {'db-restore/checksums.csv'}:
            assert stamp(U / path) == old, path
    assert {str(f.relative_to(U)) for f in U.rglob('*') if f.is_dir()} == original_directories
    assert stamp(W / 'retain-review.csv') == p['stable_review_stamp']
    assert review.DECISIONS.read_bytes() == p['decision_bytes']
    for path, old in p['source_evidence'].items():
        assert stamp(Path(path)) == old['stamp']
    receipt.update(result='passed', source_register=stats, sql_restore_verified=True,
        archive_media_metadata_and_catalogues_unchanged=True, directory_structure_unchanged=True)
    for path in [W / Path(RECEIPT).name, U / RECEIPT]:
        review.write_json(path, receipt)
    for relative in CONTROLS:
        checks[relative] = dict(path=relative, sha256=digest(U / relative), size_bytes=str((U / relative).stat().st_size))
    write_csv(U / 'db-restore/checksums.csv', fields, sorted(checks.values(), key=lambda r: r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:
        review.export_decisions(con)
    assert review.DECISIONS.read_bytes() == p['decision_bytes']
    print(json.dumps(verify(), indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--apply', action='store_true'); choice.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply()
    elif args.verify:
        print(json.dumps(verify(), indent=2))
    else:
        p = plan()
        print(json.dumps(dict(ready=True, exact_jpegs=len(p['updates']), selected_tiffs=42,
            source_stamps_checked=len(p['source_evidence']), source_register_rows=1419,
            drain_before=900, drain_after=942, source_modified=False, deletion_authorized=False), indent=2))
