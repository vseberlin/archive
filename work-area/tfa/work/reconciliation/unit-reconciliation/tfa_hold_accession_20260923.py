"""Append the seven selected September 23 holds using existing TFA conventions.

No source writes, new directories, schema changes, or previous accession replay.
"""
import argparse
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

W = Path(__file__).resolve().parent
os.environ['TMPDIR'] = str(W)
os.environ['MAGICK_TEMPORARY_PATH'] = str(W)
U = Path('/mnt/volume/iss-archive/units/tfa')
PLAN = W / 'hold-accession-plan-20260923.json'
RESULT = W / 'hold-accession-completion-20260923.json'
ARCHIVE_RESULT = U / 'db-restore/manifests/hold-accession-20260923.json'

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

L = module('tfa_local_helpers', W.parent.parent / 'tools/tfa_local_accession.py')
R = module('tfa_boundary_helpers', W / 'tfa_redundancy_recheck_20260923.py')
L.CANDIDATES = W
L.RECEIPTS = W

TABLE_FILES = {
    'objects': 'db-restore/RESTORE.csv',
    'representations': 'db-restore/manifests/representations.csv',
    'checksums': 'db-restore/checksums.csv',
    'subdivisions': 'db-restore/manifests/subdivision-register.csv',
    'object_subdivisions': 'db-restore/manifests/subdivision-memberships.csv',
    'reconciliation_decisions': 'db-restore/manifests/reconciliation-decisions.csv',
    'source_disposition': 'db-restore/manifests/source-disposition.csv',
    'drain_list': 'db-restore/manifests/drain-list.csv',
    'accession_holds': 'db-restore/manifests/accession-holds.csv',
}
EXTRA_CONTROLS = ['CATALOG.csv', 'CATALOG.xlsx', 'db-restore/RESTORE.xlsx',
                  'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql', '00_README.md',
                  'metadata/subdivision-memberships.csv', 'metadata/subdivision-register.csv',
                  'metadata/accession-state.json', 'db-restore/accession-completion.json']

def sha(path):
    with path.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()

def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        return list(r.fieldnames), list(r)

def encoded(fields, rows):
    f = io.StringIO(newline='')
    writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return f.getvalue().encode()

def write_json(path, value):
    L.MD.atomic_json(path, value)

def stamp(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]

def db_rows(c, name, fields):
    return Counter(tuple('' if r[k] is None else str(r[k]) for k in fields)
                   for r in c.execute('select * from ' + name))

def guard_plan(plan, controls=True):
    R.guard()
    assert not ARCHIVE_RESULT.exists(), 'Completed accession must not be replayed'
    assert sha(W / 'orphan-object-decisions.csv') == plan['decisions_sha256']
    assert sha(W / 'orphan-source-disposition-proposal.csv') == plan['dispositions_sha256']
    for path, expected in plan['source_stamps'].items():
        assert stamp(Path(path)) == expected, path
    if controls:
        for rel, expected in plan['control_hashes_before'].items():
            assert sha(U / rel) == expected, 'Archive control changed: ' + rel

def prepare():
    assert not PLAN.exists() and not RESULT.exists() and not ARCHIVE_RESULT.exists()
    mount = R.guard()
    recheck = json.loads((W / 'redundancy-registration-result-20260923.json').read_text())
    reviews = {r['record_key']: r for r in recheck['hold_review'] if r['outcome'].startswith('retain_')}
    decisions = [r for r in read(W / 'orphan-object-decisions.csv')[1] if r['record_key'] in reviews]
    assert len(decisions) == 9 and Counter(r['decision'] for r in decisions) == {'selected_master': 7, 'redundant': 1, 'noise': 1}
    assert all(r != reviews[r['record_key']]['prior_decision'] and r['deletion_authorized'] == 'false' for r in decisions)
    assets = {r['source_path']: r for r in read(W / 'reference-photo-workbench-holds.csv')[1]}
    paths = {reviews[r['record_key']]['source_path'] for r in decisions}
    assert not R.other_unit_paths(paths), 'Other-unit source claim'
    source_stamps = {}
    for path in paths:
        Path(path).resolve(strict=True).relative_to(Path('/media/vladimir/Elements/restore/archive'))
        s = stamp(Path(path))
        assert s[:2] == [int(assets[path]['size_bytes']), int(assets[path]['mtime_ns'])]
        source_stamps[path] = s
    current = {r['record_key']: r for r in read(U / TABLE_FILES['objects'])[1]}
    drain = {r['source_path'] for r in read(U / TABLE_FILES['drain_list'])[1]}
    assert not paths & drain
    for d in decisions:
        assert d['record_key'] not in current
    object_fields = read(U / TABLE_FILES['objects'])[0]
    root = next(r for r in read(U / TABLE_FILES['subdivisions'])[1] if r['subdivision_id'] == 'md-collection:857')
    objects = []
    for d in decisions:
        if d['decision'] != 'selected_master':
            continue
        path = d['selected_source_path']
        assert path == reviews[d['record_key']]['source_path']
        assert assets[path]['asset_id'] == d['selected_asset_id']
        assert not (U / ('masters/' + d['inventory'] + '.tif')).exists()
        assert not (U / ('export/access/' + d['inventory'] + '.jpg')).exists()
        assert not (W / (d['inventory'] + '.json')).exists()
        row = {k: '' for k in object_fields}
        note = reviews[d['record_key']]['evidence_note']
        row.update(record_key=d['record_key'], archival_object_identifier=d['record_key'], inventory=d['inventory'],
                   metadata_authority='human reconciliation manifest', metadata_reconciled='true',
                   metadata_reconciliation_basis='human_authorized_minimal_identity_and_provenance',
                   title=d['inventory'], object_type='Foto', collection_ids='857', collections=root['label'],
                   subdivision_ids='md-collection:857', last_updated=d['decided_at'],
                   selected_candidate_id=d['selected_asset_id'], selected_master_path=path,
                   selected_master_size_bytes=str(source_stamps[path][0]), master_decision_state='selected_master',
                   master_decided_by=d['decided_by'], master_decided_at=d['decided_at'], master_decision_note=d['note'],
                   provenance_chain='reference_photo_hold_human_selection | read-only source ' + path +
                       ' | source inventory evidence ' + assets[path]['normalized_inventory'] + ' | recheck: ' + note +
                       ' | human_authorized_minimal_identity_and_provenance; no unconfirmed MD identity or description transferred',
                   accession_state='ready_for_accession')
        objects.append(row)
    controls = {rel: sha(U / rel) for rel in set(TABLE_FILES.values()) | set(EXTRA_CONTROLS)}
    c = sqlite3.connect('file:' + str(U / 'db-restore/RESTORE.sqlite') + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    for table, rel in TABLE_FILES.items():
        fields, rows = read(U / rel)
        assert db_rows(c, table, fields) == Counter(tuple(r[k] for k in fields) for r in rows), table
    assert c.execute('pragma quick_check').fetchone()[0] == 'ok'
    c.close()
    payloads = {r['path']: stamp(U / r['path']) for r in read(U / TABLE_FILES['checksums'])[1]}
    plan = {'prepared_at': L.now(), 'source_mount': mount, 'deletion_authorized': False,
            'objects': objects, 'decisions': decisions, 'rechecks': reviews,
            'source_stamps': source_stamps, 'existing_payload_stamps': payloads,
            'control_hashes_before': controls,
            'decisions_sha256': sha(W / 'orphan-object-decisions.csv'),
            'dispositions_sha256': sha(W / 'orphan-source-disposition-proposal.csv')}
    write_json(PLAN, plan)
    print('PREPARED: 7 Masters, 7 access JPEGs, 9 exact drain paths; no cross-unit claims', flush=True)

def accession():
    plan = json.loads(PLAN.read_text())
    guard_plan(plan)
    for row in plan['objects']:
        receipt_path = W / (row['inventory'] + '.json')
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            assert receipt['record_key'] == row['record_key'] and receipt['source_path'] == row['selected_master_path']
            for prefix in ['master', 'access']:
                assert stamp(U / receipt[prefix + '_relative_path']) == [receipt[prefix + '_size_bytes'], receipt[prefix + '_mtime_ns'], receipt[prefix + '_inode']]
            print('REUSE completed', row['inventory'], flush=True)
            continue
        result = L.process_one(row, publish=True)
        print('ACCESSIONED', Path(result['source_path']).name, '->', result['master_relative_path'], flush=True)

def staged(rel):
    return W / ('hold-accession-20260923--' + rel.replace('/', '__'))

def append_workbook(rel, sheet_rows):
    # Artifact Tool import fails in this runtime with unsupported WASM SIMD.
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    wb = load_workbook(U / rel)
    numeric = {'size_bytes', 'width', 'height', 'sequence', 'presentation_rotation_cw',
               'selected_master_size_bytes', 'access_size_bytes'}
    for name, added in sheet_rows.items():
        ws = wb[name]
        fields = [cell.value for cell in ws[1]]
        old_last = ws.max_row
        for row in added:
            vals = [int(row[k]) if k in numeric and row[k] else row[k] for k in fields]
            ws.append(vals)
            for col, value in enumerate(vals, 1):
                cell = ws.cell(ws.max_row, col)
                cell._style = copy.copy(ws.cell(old_last, col)._style)
                if isinstance(value, str):
                    cell.data_type = 's'
                if fields[col - 1] in {'record_key', 'archival_object_identifier', 'inventory'}:
                    alignment = copy.copy(cell.alignment)
                    alignment.wrap_text = True
                    cell.alignment = alignment
            minimum_height = 54 if rel == 'db-restore/RESTORE.xlsx' else 34
            ws.row_dimensions[ws.max_row].height = max(minimum_height, ws.row_dimensions[old_last].height or 15)
        for table in ws.tables.values():
            if table.ref.endswith(str(old_last)):
                table.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
                if table.autoFilter is not None:
                    table.autoFilter.ref = table.ref
        if ws.auto_filter.ref:
            ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    wb.save(staged(rel))
    wb.close()

def build_controls():
    plan = json.loads(PLAN.read_text())
    guard_plan(plan)
    assert not R.other_unit_paths(set(plan['source_stamps']))
    data = {name: read(U / rel) for name, rel in TABLE_FILES.items()}
    receipts = {row['record_key']: json.loads((W / (row['inventory'] + '.json')).read_text()) for row in plan['objects']}
    comparison_contexts = {
        d['record_key']: [r for r in read(W / 'reference-photo-hold-context.csv')[1]
                          if 'orphan:' + r['visual_group_id'] == d['record_key']]
        for d in plan['decisions']
    }
    new_objects, new_reps, new_checks, new_memberships, new_decisions = [], [], [], [], []
    root = next(r for r in data['subdivisions'][1] if r['subdivision_id'] == 'md-collection:857')
    for original in plan['objects']:
        row = dict(original)
        rec = receipts[row['record_key']]
        for prefix in ['master', 'access']:
            assert stamp(U / rec[prefix + '_relative_path']) == [rec[prefix + '_size_bytes'], rec[prefix + '_mtime_ns'], rec[prefix + '_inode']]
        for field in ['master_relative_path', 'master_sha256', 'source_sha256', 'access_relative_path', 'access_sha256', 'access_size_bytes', 'derivative_profile']:
            row[field] = str(rec[field])
        row['accession_result'] = 'accessioned'
        new_objects.append(row)
        for role, prefix in [('preservation_master', 'master'), ('access_derivative', 'access')]:
            new_reps.append(dict(record_key=row['record_key'], inventory=row['inventory'], role=role,
                                 source_path=row['selected_master_path'] if prefix == 'master' else rec['master_relative_path'],
                                 source_sha256=rec['source_sha256'] if prefix == 'master' else rec['master_sha256'],
                                 final_relative_path=rec[prefix + '_relative_path'], final_sha256=rec[prefix + '_sha256'],
                                 size_bytes=str(rec[prefix + '_size_bytes']), completed_at=rec['completed_at'],
                                 copy_policy='verified_metadata_rewrite_atomic_publication' if prefix == 'master' else 'generated_from_selected_master',
                                 basis='human_selected_hold_minimal_metadata_pixels_and_structure_unchanged' if prefix == 'master' else L.DERIVATIVE_PROFILE,
                                 deletion_authorized='false'))
            new_checks.append(dict(path=rec[prefix + '_relative_path'], sha256=rec[prefix + '_sha256'],
                                   size_bytes=str(rec[prefix + '_size_bytes']), content_role=role))
        new_memberships.append(dict(record_key=row['record_key'], md_object_id='', inventory=row['inventory'],
                                    scheme_id=root['scheme_id'], subdivision_id=root['subdivision_id'], label=root['label'],
                                    membership_basis='human local metadata reconciliation'))
    for d in plan['decisions']:
        new_decisions.append(dict(record_key=d['record_key'], md_object_id='', inventory=d['inventory'],
                                 decision_state=d['decision'], selected_candidate_id=d['selected_asset_id'],
                                 selected_source_path=d['selected_source_path'], note=d['note'],
                                 decided_by=d['decided_by'], decided_at=d['decided_at']))
    data['objects'][1].extend(new_objects)
    data['representations'][1].extend(new_reps)
    data['checksums'][1].extend(new_checks)
    data['object_subdivisions'][1].extend(new_memberships)
    existing_decisions = {r['record_key'] for r in data['reconciliation_decisions'][1]}
    assert not existing_decisions & {r['record_key'] for r in new_decisions}
    data['reconciliation_decisions'][1].extend(new_decisions)
    root['assigned_object_count'] = str(int(root['assigned_object_count']) + 7)
    source = {r['source_path']: r for r in data['source_disposition'][1]}
    previous_sources, added_drain = {}, []
    for d in plan['decisions']:
        path = plan['rechecks'][d['record_key']]['source_path']
        previous_sources[path] = source.get(path)
        row = dict(source.get(path, {k: '' for k in data['source_disposition'][0]}))
        row.update(source_path=path, source_kind='file', unit='tfa', inventory=d['inventory'],
                   source_role='master_candidate', source_size_bytes=str(plan['source_stamps'][path][0]),
                   exact_copy_proven='false', deletion_authorized='false')
        if d['decision'] == 'selected_master':
            rec = receipts[d['record_key']]
            row.update(final_path=rec['master_relative_path'], final_sha256=rec['master_sha256'], source_sha256=rec['source_sha256'],
                       preservation_status='redundant', preservation_basis='selected_master_accessioned_pixels_and_structure_unchanged',
                       drain_state='proposed_redundant')
            reason = 'Selected source safely accessioned as the single Master; access JPEG generated from that Master.'
        else:
            row.update(final_path='', final_sha256='', preservation_status=d['decision'],
                       preservation_basis='human_' + d['decision'] + '_after_redundancy_recheck',
                       drain_state='proposed_noise' if d['decision'] == 'noise' else 'proposed_redundant')
            reason = 'Human rejected this candidate as ' + d['decision'] + '; no exact-copy or complete-image-coverage claim.'
        row['notes'] = (reason + ' Decision ' + d['record_key'] + ' by ' + d['decided_by'] + ' at ' + d['decided_at'] +
                        '. Earlier evidence retained: ' + plan['rechecks'][d['record_key']]['evidence_note'] +
                        ' Receipt: db-restore/manifests/hold-accession-20260923.json. Deletion not authorized.')
        source[path] = row
        added_drain.append(row)
    data['source_disposition'] = (data['source_disposition'][0], [source[k] for k in sorted(source)])
    data['drain_list'][1].extend(added_drain)
    catalog_fields, catalog = read(U / 'CATALOG.csv')
    bykey = {r['record_key']: r for r in new_objects}
    new_catalog = []
    for rep in new_reps:
        obj = bykey[rep['record_key']]
        rec = receipts[rep['record_key']]
        row = {field: obj.get(field, '') for field in catalog_fields}
        row.update(file_role=rep['role'], final_relative_path=rep['final_relative_path'], filename=Path(rep['final_relative_path']).name,
                   file_type=Path(rep['final_relative_path']).suffix.lstrip('.'), size_bytes=rep['size_bytes'],
                   final_sha256=rep['final_sha256'].removeprefix('sha256:'), original_source_path=rep['source_path'],
                   source_sha256=rep['source_sha256'].removeprefix('sha256:'),
                   derivative_of=rec['master_relative_path'] if rep['role'] == 'access_derivative' else '',
                   notes=obj['provenance_chain'], metadata_path='db-restore/RESTORE.csv',
                   provenance_path='db-restore/manifests/representations.csv')
        if plan['rechecks'][rep['record_key']]['outcome'] == 'retain_distinct_annotation':
            row['relationship'] = 'Same photograph with distinct carrier annotations; related object ' + '; '.join(
                r['context_record_key'] for r in comparison_contexts[rep['record_key']])
        new_catalog.append(row)
    catalog.extend(new_catalog)
    assert len(catalog) == 15546 and len(data['objects'][1]) == 7770
    assert len(data['drain_list'][1]) == 61499
    assert len({r['source_path'] for r in data['drain_list'][1]}) == 61499
    assert all(n == 1 for n in Counter(r['record_key'] for r in data['representations'][1] if r['role'] == 'preservation_master').values())
    for name, rel in TABLE_FILES.items():
        staged(rel).write_bytes(encoded(*data[name]))
    staged('CATALOG.csv').write_bytes(encoded(catalog_fields, catalog))
    for rel, table in [('metadata/subdivision-memberships.csv', 'object_subdivisions'), ('metadata/subdivision-register.csv', 'subdivisions')]:
        staged(rel).write_bytes(encoded(*data[table]))
    append_workbook('CATALOG.xlsx', {'Catalog': new_catalog, 'Objects': new_objects})
    append_workbook('db-restore/RESTORE.xlsx', {'Catalog': new_objects})
    delta = {'new_objects': new_objects, 'new_representations': new_reps, 'new_checksums': new_checks,
             'new_memberships': new_memberships, 'new_decisions': new_decisions,
             'added_drain': added_drain, 'previous_source_dispositions': previous_sources,
             'receipts': list(receipts.values()), 'root_subdivision': root,
             'comparison_contexts': comparison_contexts}
    write_json(W / 'hold-accession-controls-20260923.json', delta)
    print('STAGED catalogue CSV/XLSX and manifests: 7 objects, 14 files, 9 drain entries', flush=True)

def verify_workbooks():
    from openpyxl import load_workbook
    for rel, sheets in [('CATALOG.xlsx', {'Catalog': 'CATALOG.csv', 'Objects': TABLE_FILES['objects']}),
                        ('db-restore/RESTORE.xlsx', {'Catalog': TABLE_FILES['objects']})]:
        wb = load_workbook(staged(rel), read_only=True, data_only=False)
        for sheet, csv_rel in sheets.items():
            fields, expected = read(staged(csv_rel))
            iterator = wb[sheet].iter_rows(values_only=True)
            assert list(next(iterator)) == fields
            count = 0
            for got, wanted in zip(iterator, expected, strict=True):
                assert [str(v) if v is not None else '' for v in got] == [wanted[k] for k in fields], (rel, sheet, count)
                count += 1
            print('WORKBOOK VERIFIED', rel, sheet, count, flush=True)
        wb.close()

def finalize():
    plan = json.loads(PLAN.read_text())
    guard_plan(plan)
    verify_workbooks()
    delta = json.loads((W / 'hold-accession-controls-20260923.json').read_text())
    for rel, old_stamp in plan['existing_payload_stamps'].items():
        assert stamp(U / rel) == old_stamp, rel
    c = sqlite3.connect(U / 'db-restore/RESTORE.sqlite')
    c.row_factory = sqlite3.Row
    def insert(table, rows):
        fields = [r[1] for r in c.execute('pragma table_info(' + table + ')')]
        c.executemany('insert into ' + table + ' (' + ','.join(fields) + ') values (' + ','.join('?' for _ in fields) + ')',
                      [tuple(r[k] for k in fields) for r in rows])
    with c:
        for table, key in [('objects', 'new_objects'), ('representations', 'new_representations'), ('checksums', 'new_checksums'),
                           ('object_subdivisions', 'new_memberships'), ('reconciliation_decisions', 'new_decisions')]:
            insert(table, delta[key])
        c.execute('update subdivisions set assigned_object_count=? where subdivision_id=?',
                  (delta['root_subdivision']['assigned_object_count'], 'md-collection:857'))
        for row in delta['added_drain']:
            c.execute('delete from source_disposition where source_path=?', (row['source_path'],))
        insert('source_disposition', delta['added_drain'])
        insert('drain_list', delta['added_drain'])
        for table, rel in TABLE_FILES.items():
            fields, rows = read(staged(rel))
            assert db_rows(c, table, fields) == Counter(tuple(r[k] for k in fields) for r in rows), table
    assert c.execute('pragma quick_check').fetchone()[0] == 'ok' and not c.execute('pragma foreign_key_check').fetchall()
    with staged('db-restore/RESTORE.sql').open('w') as f:
        for line in c.iterdump():
            f.write(line + '\n')
    c.close()
    changed = list(TABLE_FILES.values()) + ['CATALOG.csv', 'CATALOG.xlsx', 'db-restore/RESTORE.xlsx', 'db-restore/RESTORE.sql',
                                           'metadata/subdivision-register.csv', 'metadata/subdivision-memberships.csv']
    for rel in changed:
        L.MD.atomic_bytes(U / rel, staged(rel).read_bytes())
    readme = (U / '00_README.md').read_text()
    readme = readme.replace('noise not covered by an accession', 'noise without an explicit human rejection')
    for old, new in [('Local expansion catalogue records: 2481', 'Local expansion catalogue records: 2488'),
                     ('Accessioned archival objects: 7762', 'Accessioned archival objects: 7769'),
                     ('Selected Masters: 7762', 'Selected Masters: 7769'),
                     ('Generated full-resolution JPEG access derivatives: 7762', 'Generated full-resolution JPEG access derivatives: 7769'),
                     ('drain list: 61490', 'drain list: 61499'), ('15,532 registered', '15,546 registered'), ('7,763 original', '7,770 current')]:
        assert old in readme, old
        readme = readme.replace(old, new)
    readme += ('\n## Selected holds accessioned — 23 September 2026\n\nSeven human-selected hold objects were added with one Master and one generated access JPEG each, using their existing visual object identifiers and minimal recorded metadata. Existing Masters were retained. Nine exact USB paths were added to the drain register: seven preserved source copies, one human-rejected redundant candidate and one noise file. The latter two dispositions do not claim identical image content. No source deletion is authorized. Receipt: `db-restore/manifests/hold-accession-20260923.json`. Previously recorded cross-unit drain issues remain outside this addition.\n')
    L.MD.atomic_bytes(U / '00_README.md', readme.encode())
    receipt = {'result': 'ok', 'completed_at': L.now(), 'objects_accessioned': 7, 'access_derivatives_generated': 7,
               'drain_paths_added': 9, 'archive_objects_accessioned': 7769, 'object_catalogue_rows': 7770,
               'catalogue_rows': 15546, 'drain_rows': 61499, 'source_read_only': True, 'source_modified': False,
               'deletion_authorized': False, 'decisions': plan['decisions'], 'recheck_evidence': plan['rechecks'],
               **delta, 'control_hashes_before': plan['control_hashes_before'],
               'existing_payloads_unchanged_by_stat': len(plan['existing_payload_stamps']),
               'csv_sqlite_agree': True, 'csv_xlsx_agree': True, 'sqlite_quick_check': 'ok', 'foreign_key_violations': 0,
               'whole_drain_boundary_clearance': False}
    write_json(ARCHIVE_RESULT, receipt)
    write_json(RESULT, receipt)
    state = json.loads((U / 'metadata/accession-state.json').read_text())
    state['updated_at'] = receipt['completed_at']
    state['hold_accession_completion'] = str(ARCHIVE_RESULT)
    write_json(U / 'metadata/accession-state.json', state)
    completion = json.loads((U / 'db-restore/accession-completion.json').read_text())
    completion.update(objects_accessioned=7769, local_objects_accessioned=2488, local_expansion_records=2488,
                      object_catalogue_rows=7770, catalogue_rows=15546, representation_rows=15546, checksum_rows=15546,
                      drain_rows=61499, source_disposition_rows=len(read(U / TABLE_FILES['source_disposition'])[1]),
                      subdivision_memberships=len(read(U / TABLE_FILES['object_subdivisions'])[1]))
    completion['hold_accession'] = {'completed_at': receipt['completed_at'], 'objects_added': 7, 'drain_paths_added': 9,
                                  'receipt': str(ARCHIVE_RESULT.relative_to(U)), 'deletion_authorized': False}
    changed.extend(['db-restore/RESTORE.sqlite', '00_README.md', 'metadata/accession-state.json', str(ARCHIVE_RESULT.relative_to(U))])
    for rel in changed:
        completion['control_file_sha256'][rel] = sha(U / rel)
    write_json(U / 'db-restore/accession-completion.json', completion)
    print('FINALIZED: 7 new Masters, 7 JPEGs; 61,499 drain paths; deletion remains unauthorized', flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'accession', 'build-controls', 'verify-workbooks', 'finalize'])
    args = parser.parse_args()
    {'prepare': prepare, 'accession': accession, 'build-controls': build_controls,
     'verify-workbooks': verify_workbooks, 'finalize': finalize}[args.command]()
