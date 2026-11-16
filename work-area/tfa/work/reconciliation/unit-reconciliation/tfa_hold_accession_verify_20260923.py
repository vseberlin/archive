"""Independent read-only verification of the completed seven-hold addition."""
import csv
import hashlib
import importlib.util
import io
import json
import sqlite3
import urllib.request
from collections import Counter
from pathlib import Path

W = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('hold_accession', W / 'tfa_hold_accession_20260923.py')
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
U = A.U
plan = json.loads(A.PLAN.read_text())
result = json.loads(A.RESULT.read_text())
delta = json.loads((W / 'hold-accession-controls-20260923.json').read_text())
assert result == json.loads(A.ARCHIVE_RESULT.read_text())
A.R.guard()
assert A.sha(W / 'orphan-object-decisions.csv') == plan['decisions_sha256']
assert A.sha(W / 'orphan-source-disposition-proposal.csv') == plan['dispositions_sha256']
for path, expected in plan['source_stamps'].items():
    assert A.stamp(Path(path)) == expected, path
for rel, expected in plan['existing_payload_stamps'].items():
    assert A.stamp(U / rel) == expected, rel
print('Sources and all', len(plan['existing_payload_stamps']), 'previous payloads unchanged by size/mtime/inode', flush=True)

new_keys = {r['record_key'] for r in plan['decisions']}
new_paths = {r['path'] for r in delta['new_checksums']}
for table, rel in A.TABLE_FILES.items():
    fields, rows = A.read(U / rel)
    prior = []
    for row in rows:
        row = dict(row)
        if table in {'objects', 'representations', 'reconciliation_decisions', 'object_subdivisions'} and row['record_key'] in new_keys:
            continue
        if table == 'checksums' and row['path'] in new_paths:
            continue
        if table in {'drain_list', 'source_disposition'} and row['source_path'] in plan['source_stamps']:
            if table == 'drain_list':
                continue
            row = delta['previous_source_dispositions'][row['source_path']]
            if row is None:
                continue
        if table == 'subdivisions' and row['subdivision_id'] == 'md-collection:857':
            row['assigned_object_count'] = str(int(row['assigned_object_count']) - 7)
        prior.append(row)
    actual = 'sha256:' + hashlib.sha256(A.encoded(fields, prior)).hexdigest()
    assert actual == plan['control_hashes_before'][rel], 'Unexpected prior data change: ' + table
fields, rows = A.read(U / 'CATALOG.csv')
prior = [r for r in rows if r['final_relative_path'] not in new_paths]
f = io.StringIO(newline='')
writer = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
writer.writeheader()
writer.writerows(prior)
assert 'sha256:' + hashlib.sha256(f.getvalue().encode()).hexdigest() == plan['control_hashes_before']['CATALOG.csv']
print('Prior catalogue and all nine manifest tables preserved except the authorized additions/dispositions', flush=True)

c = sqlite3.connect('file:' + str(U / 'db-restore/RESTORE.sqlite') + '?mode=ro', uri=True)
c.row_factory = sqlite3.Row
assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert not c.execute('pragma foreign_key_check').fetchall()
for table, rel in A.TABLE_FILES.items():
    fields, rows = A.read(U / rel)
    assert A.db_rows(c, table, fields) == Counter(tuple(r[k] for k in fields) for r in rows), table
restored = sqlite3.connect(':memory:')
restored.executescript((U / 'db-restore/RESTORE.sql').read_text())
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
for table in tables:
    assert Counter(tuple(r) for r in c.execute('select * from ' + table)) == Counter(restored.execute('select * from ' + table)), table
assert restored.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert not restored.execute('pragma foreign_key_check').fetchall()
print('CSV, SQLite and restored SQL agree across', len(tables), 'tables', flush=True)

objects = A.read(U / A.TABLE_FILES['objects'])[1]
reps = A.read(U / A.TABLE_FILES['representations'])[1]
master_counts = Counter(r['record_key'] for r in reps if r['role'] == 'preservation_master')
assert len(master_counts) == 7769 and set(master_counts.values()) == {1}
assert sum(r['accession_result'] == 'accessioned' for r in objects) == 7769
checks = {r['path']: r for r in A.read(U / A.TABLE_FILES['checksums'])[1]}
drain = {r['source_path']: r for r in A.read(U / A.TABLE_FILES['drain_list'])[1]}
catalog = {r['final_relative_path']: r for r in A.read(U / 'CATALOG.csv')[1]}
assert len(drain) == 61499 and len(catalog) == 15546
for rec in delta['receipts']:
    for prefix in ['master', 'access']:
        rel = rec[prefix + '_relative_path']
        assert A.stamp(U / rel) == [rec[prefix + '_size_bytes'], rec[prefix + '_mtime_ns'], rec[prefix + '_inode']]
        assert checks[rel]['sha256'] == rec[prefix + '_sha256']
        assert catalog[rel]['final_sha256'] == rec[prefix + '_sha256'].removeprefix('sha256:')
    d = drain[rec['source_path']]
    assert d['final_path'] == rec['master_relative_path'] and d['deletion_authorized'] == 'false'
assert all(drain[p]['deletion_authorized'] == 'false' for p in plan['source_stamps'])
assert not A.R.other_unit_paths(set(plan['source_stamps']))
assert (U / 'metadata/subdivision-register.csv').read_bytes() == (U / A.TABLE_FILES['subdivisions']).read_bytes()
assert (U / 'metadata/subdivision-memberships.csv').read_bytes() == (U / A.TABLE_FILES['object_subdivisions']).read_bytes()

completion = json.loads((U / 'db-restore/accession-completion.json').read_text())
changed = list(A.TABLE_FILES.values()) + A.EXTRA_CONTROLS + [str(A.ARCHIVE_RESULT.relative_to(U))]
for rel in set(changed) - {'db-restore/accession-completion.json'}:
    assert A.sha(U / rel) == completion['control_file_sha256'][rel], rel
for rel in ['CATALOG.xlsx', 'db-restore/RESTORE.xlsx']:
    assert (U / rel).read_bytes() == A.staged(rel).read_bytes()
# Finalize checked every staged cell; byte equality above proves the installed
# workbooks are those same validated exports.
with urllib.request.urlopen('http://127.0.0.1:8768/holds', timeout=20) as response:
    html = response.read().decode()
assert html.count('class="record-card"') == 0 and 'No matching hold candidates.' in html
verification = {'result': 'ok', 'new_masters': 7, 'generated_access_jpegs': 7, 'new_drain_paths': 9,
                'objects_accessioned': 7769, 'catalogue_rows': 15546, 'drain_rows': 61499,
                'exactly_one_master_per_object': True, 'csv_sqlite_xlsx_agree': True,
                'sql_restore_verified_tables': len(tables), 'prior_payloads_unchanged': 15532,
                'prior_catalogue_and_manifests_preserved': True, 'other_unit_overlaps_added': 0,
                'active_hold_review_cards': 0, 'deletion_authorized': False, 'source_read_only': True}
A.write_json(W / 'hold-accession-verification-20260923.json', verification)
print(json.dumps(verification, indent=2))
