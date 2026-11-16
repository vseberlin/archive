"""Bounded owner-requested drain exclusions, review projection and ZIP extraction."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

W = Path(__file__).resolve().parent
U = Path('/mnt/volume/iss-archive/units/tfa')
PREFIX = 'drain-corrections-20260923'
PLAN = W / (PREFIX + '-plan.json')
RECEIPT = U / 'db-restore/manifests' / (PREFIX + '.json')
ZIP = 'documents/historical/drafts-and-superseded.zip'
TABLES = {'representations': 'db-restore/manifests/representations.csv',
          'checksums': 'db-restore/checksums.csv',
          'source_disposition': 'db-restore/manifests/source-disposition.csv',
          'drain_list': 'db-restore/manifests/drain-list.csv'}

def helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

R = helper('boundary_read_helpers', W / 'tfa_redundancy_recheck_20260923.py')

def read(p):
    with p.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        return list(r.fieldnames), list(r)

def encode(fields, rows):
    f = io.StringIO(newline='')
    w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
    w.writeheader(); w.writerows(rows)
    return f.getvalue().encode()

def sha(p):
    with p.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()

def stamp(p):
    s = p.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ino]

def write(p, data):
    assert p.parent.is_dir()
    tmp = p.with_name('.' + p.name + '.tmp')
    assert not tmp.exists()
    with tmp.open('xb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)

def jswrite(p, data):
    write(p, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())

def stage(rel):
    return W / (PREFIX + '--' + rel.replace('/', '__'))

def prepare():
    R.guard()
    assert not PLAN.exists() and not RECEIPT.exists()
    fields, drains = read(U / TABLES['drain_list'])
    assert len(drains) == 61499
    dm = {r['source_path']: r for r in drains}
    overlaps = R.other_unit_paths(set(dm))
    assert len(overlaps) == 73
    objects = {r['record_key']: r for r in read(U / 'db-restore/RESTORE.csv')[1]}
    pending = {}
    for r in drains:
        match = re.search(r'selected MD TIFF Master\(s\) (.*?); a/d/p=', r['notes'])
        if match and not r['final_path']:
            keys = ['md:' + s.strip().removeprefix('md:') for s in match[1].split(';')]
            if not any(k in objects and objects[k]['accession_result'] == 'accessioned' for k in keys):
                assert len(keys) == 1
                pending[r['source_path']] = keys[0]
    assert len(pending) == 14 and not set(pending) & set(overlaps)
    assert Counter(pending.values()) == {'md:93608': 4, 'md:93609': 4, 'md:85823': 6}
    prior = {p: dm[p] for p in set(overlaps) | set(pending)}
    bindings = read(W / 'visual-only-institution-master-bindings.csv')[1]
    wanted = set(pending) | {b['selected_master_path'] for b in bindings}
    indexed = R.features(wanted)
    assert len(indexed) == 17
    hold_fields, holds = read(W / 'reference-photo-workbench-holds.csv')
    assert len(holds) == 44
    added_holds = []
    for b in bindings:
        key = 'md:' + b['md_object_id']
        paths = [p for p, k in pending.items() if k == key] + [b['selected_master_path']]
        for p in paths:
            a = indexed[p]
            assert stamp(Path(p))[:2] == [int(a['size_bytes']), int(a['mtime_ns'])]
            features = a['features']
            row = dict.fromkeys(hold_fields, '')
            row.update(normalized_inventory=b['md_inventory'], inventory_members=b['md_inventory'],
                       visual_group_id=key, asset_id=a['asset_id'], source_path=p,
                       filename=Path(p).name, extension=Path(p).suffix.lstrip('.').lower(),
                       size_bytes=str(a['size_bytes']), mtime_ns=str(a['mtime_ns']),
                       width_px=str(a['width_px']), height_px=str(a['height_px']),
                       ahash=features['ahash-64-v2'], dhash=features['dhash-64-v2'],
                       phash=features['phash-dct-64-v2'],
                       exact_visual_group_tiff_count=str(sum(Path(x).suffix.lower() in {'.tif', '.tiff'} for x in paths)),
                       classification_basis=f"Known museum-digital {key}: {b['md_title']}; archive coverage not established; retained for human review using saved visual matches",
                       review_state='unresolved_hold', deletion_authorized='false')
            added_holds.append(row)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    payloads = []
    with zipfile.ZipFile(U / ZIP) as z:
        members = list(csv.DictReader(io.StringIO(z.read('MANIFEST.csv').decode())))
        assert len(members) == 11 and len(z.namelist()) == 13
        member_map = {r['member']: r for r in members}
        for name in z.namelist():
            assert Path(name).name == name and name not in {'', '.', '..'}
            rel = 'documents/historical/' + name
            assert not (U / rel).exists()
            p = stage(rel)
            write(p, z.read(name))
            digest = sha(p)  # Each newly extracted final payload is hashed once.
            original = member_map.get(name)
            if original:
                assert digest == original['source_sha256'] and p.stat().st_size == int(original['size_bytes'])
            payloads.append({'path': rel, 'sha256': digest, 'size_bytes': str(p.stat().st_size),
                             'member': name, 'original': original})
    control_rels = list(TABLES.values()) + ['CATALOG.csv', 'CATALOG.xlsx',
                    'db-restore/manifests/documents.csv', 'documents/CONTENTS.md', '00_README.md',
                    'db-restore/RESTORE.sql', 'db-restore/RESTORE.sqlite',
                    'db-restore/accession-completion.json', 'metadata/accession-state.json']
    work_files = ['source-disposition-proposal.csv', 'drain-list-proposal.csv',
                  'reference-photo-workbench-holds.csv', 'reference-photo-hold-workbench-preparation.json',
                  'orphan-object-decisions.csv', 'orphan-source-disposition-proposal.csv']
    plan = {'prepared_at': now, 'overlaps': overlaps, 'pending': pending,
            'prior_source_rows': prior, 'added_hold_rows': added_holds,
            'payloads': payloads, 'source_stamps': {p: stamp(Path(p)) for p in set(prior) | wanted},
            'archive_controls_before': {rel: sha(U / rel) for rel in control_rels},
            'work_controls_before': {rel: sha(W / rel) for rel in work_files},
            'payload_stamps_before': {r['path']: stamp(U / r['path']) for r in read(U / TABLES['checksums'])[1]},
            'archive_directories_before': sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
            'objects_sha256': sha(U / 'db-restore/RESTORE.csv'),
            'object_workbook_sha256': sha(U / 'db-restore/RESTORE.xlsx')}
    jswrite(PLAN, plan)
    print('Prepared 73 exclusions, 14 source holds in 3 MD cases, 11 documents and 2 package records.', flush=True)

def build():
    plan = json.loads(PLAN.read_text())
    assert not RECEIPT.exists()
    data = {table: read(U / rel) for table, rel in TABLES.items()}
    doc_fields, docs = read(U / 'db-restore/manifests/documents.csv')
    cat_fields, cat = read(U / 'CATALOG.csv')
    sources = {r['source_path']: r for r in data['source_disposition'][1]}
    for p, owners in plan['overlaps'].items():
        r = sources[p]
        r.update(preservation_status='retained_existing_unit_evidence', drain_state='retain_existing_unit_scope')
        r['notes'] += ' Owner instruction 2026-09-23: exclude from TFA drain; recorded other-unit/mixed-project scope: ' + ', '.join(owners) + '. Existing TFA Master unchanged.'
    for p, key in plan['pending'].items():
        r = sources[p]
        r.update(preservation_status='unresolved', drain_state='pending_next_pass')
        r['notes'] += f' Owner instruction 2026-09-23: remove from drain and review in /holds as {key}; no accessioned TFA Master establishes coverage.'
    # Preserve the package in place, now explicitly redundant. All members remain accessible.
    zip_doc = next(r for r in docs if r['final_relative_path'] == ZIP)
    zip_cat = next(r for r in cat if r['final_relative_path'] == ZIP)
    zip_rep = next(r for r in data['representations'][1] if r['final_relative_path'] == ZIP)
    zip_sum = next(r for r in data['checksums'][1] if r['path'] == ZIP)
    zip_doc.update(file_role='redundant_document_package', disposition='redundant',
                   condition='All eleven documents and both package records extracted unchanged; ZIP retained pending separate deletion approval.')
    zip_cat.update(file_role=zip_doc['file_role'], condition=zip_doc['condition'], notes=zip_doc['condition'])
    zip_rep['role'] = zip_sum['content_role'] = zip_doc['file_role']
    zip_rep['basis'] = zip_doc['condition']
    by_original = {}
    by_hash = {}
    for payload in plan['payloads']:
        original = payload['original']
        rel = payload['path']
        role = 'historical_document' if original else 'documentation_package_record'
        src = original['source_path'] if original else str(U / ZIP) + '::' + payload['member']
        desc = ('Historical TFA source document: ' + original['original_filename'] if original
                else 'Original ZIP ' + payload['member'] + ' preserved after extraction')
        note = 'Extracted byte-for-byte from ' + ZIP + '::' + payload['member'] + '; redundant ZIP retained; deletion not authorized.'
        doc = dict.fromkeys(doc_fields, '')
        doc.update(document_id=original['document_id'] if original else zip_doc['document_id'] + '::' + payload['member'],
                   final_relative_path=rel, filename=payload['member'], file_role=role, description=desc,
                   source_path=src, source_kind='file' if original else 'container_member', relationship='direct',
                   condition=original['condition'] if original else 'original_package_record', disposition='accessioned',
                   package_member_count='0', size_bytes=payload['size_bytes'], sha256=payload['sha256'], deletion_authorized='false')
        docs.append(doc)
        rep = dict.fromkeys(data['representations'][0], '')
        rep.update(record_key='unit:tfa', role=role, source_path=src,
                   source_sha256=original['source_sha256'] if original else payload['sha256'],
                   final_relative_path=rel, final_sha256=payload['sha256'], size_bytes=payload['size_bytes'],
                   completed_at=plan['prepared_at'], copy_policy='unchanged_zip_member_extraction', basis=note,
                   deletion_authorized='false')
        data['representations'][1].append(rep)
        data['checksums'][1].append({'path': rel, 'sha256': payload['sha256'], 'size_bytes': payload['size_bytes'], 'content_role': role})
        row = dict.fromkeys(cat_fields, '')
        row.update(record_key='unit:tfa', title=desc, file_role=role, final_relative_path=rel,
                   filename=payload['member'], file_type=Path(rel).suffix.lstrip('.'), size_bytes=payload['size_bytes'],
                   final_sha256=payload['sha256'].removeprefix('sha256:'), description=desc, original_source_path=src,
                   source_sha256=rep['source_sha256'], relationship='direct', condition=doc['condition'], notes=note,
                   metadata_path='db-restore/manifests/documents.csv', provenance_path='documents/historical/MANIFEST.csv')
        cat.append(row)
        if original:
            by_original[src] = payload
            by_hash[payload['sha256']] = payload
    changed_doc_paths = []
    for r in sources.values():
        if r['final_path'] != ZIP:
            continue
        payload = by_original.get(r['source_path']) or by_hash.get(r['source_sha256'])
        assert payload, r['source_path']
        r.update(final_path=payload['path'], final_sha256=payload['sha256'], exact_copy_proven='true')
        r['notes'] += ' ZIP unpacked 2026-09-23; final_path now identifies the exact extracted member; stored member/source checksum verified.'
        changed_doc_paths.append(r['source_path'])
    assert len(changed_doc_paths) == 12
    zip_row = dict.fromkeys(data['source_disposition'][0], '')
    zip_row.update(source_path=str(U / ZIP), source_kind='file', unit='tfa', source_role='redundant_document_package',
                   final_path=json.dumps([p['path'] for p in plan['payloads']], ensure_ascii=False),
                   preservation_status='redundant', preservation_basis='all_zip_members_preserved_as_extracted_files',
                   source_sha256=zip_doc['sha256'], source_size_bytes=zip_doc['size_bytes'], exact_copy_proven='false',
                   drain_state='proposed_redundant', deletion_authorized='false',
                   notes='Owner requested ZIP drain registration after extraction. Archive-local ZIP, not a USB path. All 11 originals and MANIFEST.csv/README.md preserved; individual hashes in representations.csv. ZIP remains unchanged in place. No deletion authorized.')
    sources[zip_row['source_path']] = zip_row
    data['source_disposition'] = (data['source_disposition'][0], [sources[k] for k in sorted(sources)])
    removed = set(plan['overlaps']) | set(plan['pending'])
    data['drain_list'] = (data['drain_list'][0], [sources[r['source_path']] for r in data['drain_list'][1] if r['source_path'] not in removed] + [zip_row])
    assert len(data['drain_list'][1]) == 61413
    contents = ['# TFA documentation', '',
                '24 approved USB source records remain preserved. The historical ZIP has been unpacked into its existing directory.',
                'There are 17 active document outputs, two extracted package records, and one redundant ZIP retained pending separate deletion approval.',
                'The original member provenance remains readable in `historical/MANIFEST.csv`.', '',
                '| Path | Role | Original source | SHA-256 |', '| --- | --- | --- | --- |']
    for d in docs:
        if d['final_relative_path'] == 'documents/CONTENTS.md':
            continue
        contents.append('| `' + d['final_relative_path'] + '` | ' + d['file_role'] + ' | ' + d['source_path'].replace('|', '&#124;') + ' | `' + d['sha256'] + '` |')
    contents += ['', 'Restricted contract and finance ZIPs remain unchanged. No deletion is authorized.',
                 'The mixed-project DIGIS presentation remains accessioned, but its USB source is excluded from the TFA drain.', '']
    content_bytes = '\n'.join(contents).encode()
    write(stage('documents/CONTENTS.md'), content_bytes)
    digest = sha(stage('documents/CONTENTS.md'))
    for rows, pathkey, hashkey in [(docs, 'final_relative_path', 'sha256'),
                (cat, 'final_relative_path', 'final_sha256'),
                (data['representations'][1], 'final_relative_path', 'final_sha256'),
                (data['checksums'][1], 'path', 'sha256')]:
        for r in rows:
            if r[pathkey] == 'documents/CONTENTS.md':
                r[hashkey] = digest.removeprefix('sha256:') if rows is cat else digest
                r['size_bytes'] = str(len(content_bytes))
    for table, rel in TABLES.items():
        write(stage(rel), encode(*data[table]))
    write(stage('db-restore/manifests/documents.csv'), encode(doc_fields, docs))
    write(stage('CATALOG.csv'), encode(cat_fields, cat))
    # Existing established fallback: Artifact Tool XLSX import/export is unavailable (WASM SIMD).
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    wb = load_workbook(U / 'CATALOG.xlsx')
    ws = wb['Catalog']; assert [c.value for c in ws[1]] == cat_fields
    old_last = ws.max_row
    numeric = {'size_bytes', 'width', 'height', 'sequence', 'presentation_rotation_cw'}
    old = {r['final_relative_path']: r for r in read(U / 'CATALOG.csv')[1]}
    for index, row in enumerate(cat, 2):
        if old.get(row['final_relative_path']) == row:
            continue
        for col, k in enumerate(cat_fields, 1):
            value = int(row[k]) if k in numeric and row[k] else row[k]
            cell = ws.cell(index, col, value)
            if index > old_last:
                cell._style = copy.copy(ws.cell(old_last, col)._style)
            if isinstance(value, str): cell.data_type = 's'
        if index > old_last:
            ws.row_dimensions[index].height = max(54, ws.row_dimensions[old_last].height or 34)
    for table in ws.tables.values():
        table.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
        if table.autoFilter: table.autoFilter.ref = table.ref
    if ws.auto_filter.ref: ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    wb.save(stage('CATALOG.xlsx')); wb.close()
    # Read back every cell of the catalog and unchanged Objects sheet.
    wb = load_workbook(stage('CATALOG.xlsx'), read_only=True)
    for sheet, expected in [('Catalog', cat), ('Objects', read(U / 'db-restore/RESTORE.csv')[1])]:
        it = wb[sheet].iter_rows(values_only=True); fields = list(next(it))
        for got, wanted in zip(it, expected, strict=True):
            assert ['' if v is None else str(v) for v in got] == [wanted[k] for k in fields]
    wb.close()
    jswrite(W / (PREFIX + '-delta.json'), {'changed_document_source_paths': changed_doc_paths, 'zip_row': zip_row})
    print('Staged records and workbook verified; projected drain count 61,413.', flush=True)

def sync_table(c, table, fields, rows):
    c.execute('delete from ' + table)
    c.executemany('insert into ' + table + ' (' + ','.join(fields) + ') values (' + ','.join('?' for _ in fields) + ')',
                  [tuple(r[k] for k in fields) for r in rows])

def apply():
    R.guard()
    plan = json.loads(PLAN.read_text())
    assert not RECEIPT.exists()
    for rel, digest in plan['archive_controls_before'].items(): assert sha(U / rel) == digest, rel
    for rel, digest in plan['work_controls_before'].items(): assert sha(W / rel) == digest, rel
    for p, expected in plan['source_stamps'].items(): assert stamp(Path(p)) == expected, p
    for rel, expected in plan['payload_stamps_before'].items(): assert stamp(U / rel) == expected, rel
    # No existing archive payload is replaced except the generated human-readable documentation index.
    for payload in plan['payloads']:
        target = U / payload['path']
        assert not target.exists()
        with target.open('xb') as f: f.write(stage(payload['path']).read_bytes())
        assert target.stat().st_size == int(payload['size_bytes'])
    changed = list(TABLES.values()) + ['CATALOG.csv', 'CATALOG.xlsx', 'db-restore/manifests/documents.csv', 'documents/CONTENTS.md']
    for rel in changed: write(U / rel, stage(rel).read_bytes())
    c = sqlite3.connect(U / 'db-restore/RESTORE.sqlite')
    with c:
        for table, rel in TABLES.items(): sync_table(c, table, *read(U / rel))
        c.execute("update meta set value=? where key='documentation_scope'", ('24 selected USB sources; 17 active document outputs, 2 extracted package records, 1 redundant ZIP retained, plus index',))
    assert c.execute('pragma quick_check').fetchone()[0] == 'ok' and not c.execute('pragma foreign_key_check').fetchall()
    write(U / 'db-restore/RESTORE.sql', ('\n'.join(c.iterdump()) + '\n').encode()); c.close()
    # Apply only the 87 exclusions/holds to legacy work proposals; do not replay accession.
    source_fields, sources = read(W / 'source-disposition-proposal.csv')
    removed = set(plan['overlaps']) | set(plan['pending'])
    final_sources = {r['source_path']: r for r in read(U / TABLES['source_disposition'])[1]}
    for r in sources:
        if r['source_path'] in removed:
            current = final_sources[r['source_path']]
            for k in ['preservation_status', 'drain_state', 'notes']: r[k] = current[k]
    drain_fields, drains = read(W / 'drain-list-proposal.csv')
    drains = [r for r in drains if r['source_path'] not in removed]
    write(W / 'source-disposition-proposal.csv', encode(source_fields, sources))
    write(W / 'drain-list-proposal.csv', encode(drain_fields, drains))
    c = sqlite3.connect(W / 'reconciliation.sqlite')
    with c:
        for r in sources:
            if r['source_path'] in removed:
                c.execute('update source_disposition_proposal set preservation_status=?,drain_state=?,notes=? where source_path=?',
                          (r['preservation_status'], r['drain_state'], r['notes'], r['source_path']))
        c.executemany('delete from drain_list_proposal where source_path=?', [(p,) for p in removed])
    assert c.execute('pragma quick_check').fetchone()[0] == 'ok' and not c.execute('pragma foreign_key_check').fetchall()
    c.close()
    hold_fields, holds = read(W / 'reference-photo-workbench-holds.csv')
    holds += plan['added_hold_rows']
    write(W / 'reference-photo-workbench-holds.csv', encode(hold_fields, holds))
    projection_path = W / 'reference-photo-hold-workbench-preparation.json'
    projection = json.loads(projection_path.read_text())
    projection.update(workbench_candidate_paths=len(holds), workbench_visual_groups=47, decodable_candidate_paths=59)
    projection['output_sha256']['workbench'] = sha(W / 'reference-photo-workbench-holds.csv')
    projection['drain_review_extension'] = {'updated_at': plan['prepared_at'], 'md_object_ids': ['85823', '93608', '93609'],
                                          'candidate_paths_added': 17, 'drain_paths_held': 14, 'receipt': str(RECEIPT)}
    jswrite(projection_path, projection)
    text = (U / '00_README.md').read_text()
    text = text.replace('Redundant USB source paths in the drain list: 61499', 'Drain candidates: 61,412 USB paths and one archive-local redundant ZIP (61,413 total)')
    text = text.replace('Accessioned documentation outputs: 7 plus `documents/CONTENTS.md`', 'Documentation: 17 active outputs, two extracted package records, one redundant ZIP retained, plus `documents/CONTENTS.md`')
    text = text.replace('15,546 registered media/documentation file rows', '15,559 registered media/documentation file rows')
    text += '\n## Drain corrections — 23 September 2026\n\n73 other-unit/mixed-project source paths are explicitly excluded. Fourteen paths for MD 93608, 93609 and 85823 are held outside the drain and shown as three cases in the existing workbench, with three existing TIFF candidates. No Master was selected or replaced. The historical documentation ZIP was unpacked into eleven originals and two package records in its existing directory. The ZIP remains unchanged as one archive-local drain candidate; USB and archive candidates are distinct exact paths. Bulk missing-path/hash enrichment remains deferred. No deletion is authorized. Receipt: `db-restore/manifests/drain-corrections-20260923.json`. Earlier dated counts are historical.\n'
    write(U / '00_README.md', text.encode())
    receipt = {'result': 'ok', 'completed_at': plan['prepared_at'], 'deletion_authorized': False,
               'source_modified': False, 'source_read_only': True, 'master_selections_changed': False,
               'cross_unit_paths_excluded': 73, 'drain_paths_held_for_review': 14, 'workbench_cases_added': 3,
               'workbench_paths_added': 17, 'documents_extracted': 11, 'package_records_extracted': 2,
               'drain_rows': 61413, 'usb_drain_rows': 61412, 'archive_local_drain_rows': 1,
               'work_drain_rows': len(drains),
               'item_3_bulk_enrichment_applied': False, 'overlaps': plan['overlaps'], 'pending': plan['pending'],
               'prior_source_rows': plan['prior_source_rows'], 'extracted_members': plan['payloads'],
               'zip_drain_row': json.loads((W / (PREFIX + '-delta.json')).read_text())['zip_row'],
               'source_stamps': plan['source_stamps']}
    jswrite(RECEIPT, receipt); jswrite(W / (PREFIX + '.json'), receipt)
    state = json.loads((U / 'metadata/accession-state.json').read_text())
    state.update(updated_at=plan['prepared_at'], drain_correction_receipt=str(RECEIPT))
    jswrite(U / 'metadata/accession-state.json', state)
    completion_path = U / 'db-restore/accession-completion.json'
    completion = json.loads(completion_path.read_text())
    completion.update(drain_rows=61413, source_disposition_rows=83878, catalogue_rows=15559,
                      representation_rows=15559, checksum_rows=15559, document_manifest_rows=21,
                      documentation_outputs=17, all_accessioned_usb_sources_in_drain=False)
    completion['drain_corrections'] = {'receipt': str(RECEIPT), 'updated_at': plan['prepared_at'],
                                     'archive_local_zip_candidates': 1, 'extracted_package_records': 2}
    changed += ['db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql', '00_README.md', 'metadata/accession-state.json', str(RECEIPT.relative_to(U))]
    for rel in changed: completion['control_file_sha256'][rel] = sha(U / rel)
    jswrite(completion_path, completion)
    print('Applied 73 exclusions, 14 holds, extracted 11 documents, registered intact ZIP. No source deletion.', flush=True)

def verify():
    R.guard()
    plan = json.loads(PLAN.read_text())
    sources = {r['source_path']: r for r in read(U / TABLES['source_disposition'])[1]}
    drains = read(U / TABLES['drain_list'])[1]; dm = {r['source_path']: r for r in drains}
    assert len(drains) == len(dm) == 61413
    assert not (set(plan['overlaps']) | set(plan['pending'])) & set(dm)
    assert all(sources[p] == r for p, r in dm.items())
    assert all(r['deletion_authorized'] == 'false' for r in sources.values())
    assert not any(r['preservation_status'] == 'genuine_loss' for r in drains)
    assert sha(U / 'db-restore/RESTORE.csv') == plan['objects_sha256']
    assert sha(U / 'db-restore/RESTORE.xlsx') == plan['object_workbook_sha256']
    assert sha(W / 'orphan-object-decisions.csv') == plan['work_controls_before']['orphan-object-decisions.csv']
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()) == plan['archive_directories_before']
    for p, expected in plan['source_stamps'].items(): assert stamp(Path(p)) == expected
    for rel, expected in plan['payload_stamps_before'].items():
        if rel != 'documents/CONTENTS.md': assert stamp(U / rel) == expected, rel
    # Byte comparison of the small extracted originals, without recalculating hashes.
    with zipfile.ZipFile(U / ZIP) as z:
        for payload in plan['payloads']: assert (U / payload['path']).read_bytes() == z.read(payload['member'])
    c = sqlite3.connect((U / 'db-restore/RESTORE.sqlite').as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    for table, rel in TABLES.items():
        fields, rows = read(U / rel)
        assert Counter(tuple(r[k] for k in fields) for r in rows) == Counter(tuple(str(r[k]) for k in fields) for r in c.execute('select * from ' + table)), table
    restored = sqlite3.connect(':memory:')
    restored.executescript((U / 'db-restore/RESTORE.sql').read_text())
    for (table,) in c.execute("select name from sqlite_master where type='table'"):
        assert Counter(tuple(r) for r in c.execute('select * from ' + table)) == Counter(restored.execute('select * from ' + table)), table
    assert c.execute('pragma quick_check').fetchone()[0] == 'ok' and not c.execute('pragma foreign_key_check').fetchall()
    restored.close(); c.close()
    wc = sqlite3.connect((W / 'reconciliation.sqlite').as_uri() + '?mode=ro', uri=True)
    wc.row_factory = sqlite3.Row
    for table, file in [('source_disposition_proposal', 'source-disposition-proposal.csv'), ('drain_list_proposal', 'drain-list-proposal.csv')]:
        fields, rows = read(W / file)
        assert Counter(tuple(r[k] for k in fields) for r in rows) == Counter(tuple('' if r[k] is None else str(r[k]) for k in fields) for r in wc.execute('select * from ' + table)), table
    assert wc.execute('pragma quick_check').fetchone()[0] == 'ok' and not wc.execute('pragma foreign_key_check').fetchall()
    wc.close()
    from openpyxl import load_workbook
    wb = load_workbook(U / 'CATALOG.xlsx', read_only=True)
    for sheet, file in [('Catalog', 'CATALOG.csv'), ('Objects', 'db-restore/RESTORE.csv')]:
        it = wb[sheet].iter_rows(values_only=True); fields = list(next(it))
        for got, expected in zip(it, read(U / file)[1], strict=True):
            assert ['' if v is None else str(v) for v in got] == [expected[k] for k in fields]
    wb.close()
    completion = json.loads((U / 'db-restore/accession-completion.json').read_text())
    for rel, digest in completion['control_file_sha256'].items(): assert sha(U / rel) == digest, rel
    result = {'result': 'ok', 'drain_rows': 61413, 'source_rows': len(sources),
              'csv_sqlite_sql_xlsx_agree': True, 'objects_and_master_selections_unchanged': True,
              'source_stamps_unchanged': len(plan['source_stamps']), 'existing_payloads_unchanged_except_documentation_index': len(plan['payload_stamps_before']) - 1,
              'all_13_extracted_members_byte_equal': True, 'directories_unchanged': True, 'deletion_authorized': False}
    jswrite(W / (PREFIX + '-verification.json'), result)
    print(json.dumps(result), flush=True)

if __name__ == '__main__':
    {'prepare': prepare, 'build': build, 'apply': apply, 'verify': verify}[sys.argv[1]]()
