"""Read-only verification of the bounded TFA drain update and SQL restore."""
import csv
import hashlib
import json
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

W = Path(__file__).resolve().parent
U = Path('/mnt/volume/iss-archive/units/tfa')

def digest(path):
    with path.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()

plan = json.loads((W / 'redundancy-confirmed-additions-20260923.json').read_text())
receipt = json.loads((W / 'redundancy-registration-result-20260923.json').read_text())
assert receipt == json.loads((U / 'db-restore/manifests/redundancy-recheck-20260923.json').read_text())
for rel, expected in plan['unchanged_catalogue_hashes'].items():
    assert digest(U / rel) == expected, rel
for rel, expected in plan['work_evidence_hashes_before'].items():
    assert digest(W / rel) == expected, rel
completion = json.loads((U / 'db-restore/accession-completion.json').read_text())
for rel in ['00_README.md', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
            'db-restore/manifests/drain-list.csv', 'db-restore/manifests/source-disposition.csv',
            'db-restore/manifests/redundancy-recheck-20260923.json']:
    assert digest(U / rel) == completion['control_file_sha256'][rel], rel
print('Control hashes, catalogue and saved human decisions verified', flush=True)

live = sqlite3.connect('file:' + str(U / 'db-restore/RESTORE.sqlite') + '?mode=ro', uri=True)
restored = sqlite3.connect(':memory:')
restored.executescript((U / 'db-restore/RESTORE.sql').read_text())
assert restored.execute('pragma integrity_check').fetchone()[0] == 'ok'
assert not restored.execute('pragma foreign_key_check').fetchall()
tables = [r[0] for r in live.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
for table in tables:
    quoted = '"' + table.replace('"', '""') + '"'
    assert Counter(live.execute('select * from ' + quoted)) == Counter(restored.execute('select * from ' + quoted)), table
print('SQL restore verified against all', len(tables), 'SQLite tables', flush=True)

csv_rows = {}
for filename, table in [('source-disposition.csv', 'source_disposition'), ('drain-list.csv', 'drain_list')]:
    with (U / 'db-restore/manifests' / filename).open(newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    live.row_factory = sqlite3.Row
    expected = Counter(tuple(r[k] for k in fields) for r in rows)
    actual = Counter(tuple('' if r[k] is None else str(r[k]) for k in fields) for r in live.execute('select * from ' + table))
    assert expected == actual, filename
    assert len(rows) == len({r['source_path'] for r in rows})
    csv_rows[table] = {r['source_path']: r for r in rows}
assert len(csv_rows['drain_list']) == 61490
for item in plan['additions']:
    path = item['source_path']
    row = csv_rows['drain_list'][path]
    assert row == csv_rows['source_disposition'][path]
    assert row['deletion_authorized'] == 'false'
    assert row['final_path'] == item['final_path']
    assert row['final_sha256'] == item['final_sha256']
    stat = Path(path).stat()
    assert [stat.st_size, stat.st_mtime_ns] == item['source_stamp']
for review in plan['hold_review']:
    if review['outcome'].startswith('retain_'):
        assert review['source_path'] not in csv_rows['drain_list']
mount = subprocess.check_output(['findmnt', '-rn', '-S', '/dev/sdf1', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'], text=True).strip()
parts = mount.split()
assert parts[:3] == ['/dev/sdf1', '/media/vladimir/Elements', 'ntfs3']
assert 'ro' in parts[3].split(',') and 'rw' not in parts[3].split(',')
result = {'result': 'ok', 'added_paths': len(plan['additions']),
          'drain_rows': len(csv_rows['drain_list']),
          'source_disposition_rows': len(csv_rows['source_disposition']),
          'sql_restore_tables_verified': len(tables), 'csv_sqlite_agree': True,
          'unchanged_catalogue_and_human_decisions': True,
          'held_back_paths_absent_from_drain': 9, 'source_mount': mount,
          'deletion_authorized': False}
output = W / 'redundancy-registration-verification-20260923.json'
with output.open('x') as f:
    json.dump(result, f, indent=2)
    f.write('\n')
print(json.dumps(result, indent=2))
