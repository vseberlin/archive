#!/usr/bin/env python3
"""Accession the eight saved Paragon choices into the existing final unit."""
import argparse
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from uuid import uuid4

from PIL import Image, ImageOps
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

import paragon_workbench as review

W = review.ROOT
U = review.ARCHIVE
RECEIPT = 'db-restore/manifests/reviewed-accession-completion.json'
TITLE_ID = 'ISS-2026-F4ZA-M'
AUTHORIZATION = 'oklets finish that'
NOW = datetime.now(timezone.utc).isoformat()
PROGRESS = W / 'reviewed-accession-progress.json'


def read_csv(path):
    with Path(path).open(newline='') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def write_csv(path, fields, rows):
    with Path(path).open('w', newline='') as f:
        writer = csv.DictWriter(f, fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def stat(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns]


def preflight(resume=False):
    review.check()
    assert not (U / RECEIPT).exists(), 'This accession is already completed; validate it instead.'
    with review.connect() as c:
        objects = review.load_objects(c)
    selected = []
    for o in objects:
        if not o['selected_path']:
            assert o['inventory'] == 'VLV-1092-2' and o['note']
            continue
        assert not o['catalogue']['master_file'], 'Already accessioned object: ' + o['inventory']
        candidate = next(c for c in o['candidates'] if c['source_path'] == o['selected_path'])
        review.verify_candidate(candidate)
        suffix = '.tif' if candidate['format'] == 'TIFF' else '.jpg'
        base = o['inventory'] + '__' + o['catalogue']['iss_id']
        selected.append({'object': o, 'source': candidate,
                         'master': 'masters/' + base + '__master' + suffix,
                         'access': 'export/access/' + base + '__access.jpg'})
    assert len(selected) == 8
    for item in selected:
        for key in ('master', 'access'):
            assert resume or not (U / item[key]).exists(), item[key]
            assert (U / item[key]).parent.is_dir()
    with sqlite3.connect(f'file:{U}/db-restore/RESTORE.sqlite?mode=ro', uri=True) as c:
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute("SELECT count(*) FROM entity_files WHERE status='active'").fetchone()[0] == 466
    fields, checks = read_csv(U / 'db-restore/checksums.csv')
    for row in checks:
        assert (U / row['path']).stat().st_size == int(row['size_bytes']), row['path']
    return selected


def embed(path, obj, title_only=False):
    """Metadata-only ExifTool writes; immediately confirm identical decoded pixels."""
    with Image.open(path) as im:
        before = im.convert('RGB').tobytes()
        dimensions = im.size
    title = obj['descriptive_title']
    args = ['exiftool', '-overwrite_original', '-P', '-charset', 'IPTC=UTF8']
    tags = {'XMP-dc:Title': title, 'XMP-iptcExt:ArtworkTitle': title,
            'IPTC:CodedCharacterSet': 'UTF8'}
    if title and len(title.encode()) <= 64:
        tags['IPTC:ObjectName'] = title
    if not title_only:
        tags.update({'XMP-dc:Identifier': obj['iss_id'],
                     'XMP-iptcExt:ArtworkSourceInventoryNo': obj['archival_label'],
                     'XMP-dc:Description': obj['description'],
                     'IFD0:ImageDescription': obj['description'],
                     'IPTC:Caption-Abstract': obj['description']})
    args += ['-' + key + '=' + value for key, value in tags.items() if value]
    subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=60)
    got = json.loads(subprocess.check_output(
        ['exiftool', '-j', '-G1', '-s', *['-' + key for key in tags], str(path)],
        text=True, timeout=30))[0]
    for key, value in tags.items():
        if value:
            assert got.get(key) == value, (path, key, got.get(key), value)
    with Image.open(path) as im:
        im.load()
        assert im.size == dimensions and im.convert('RGB').tobytes() == before
    return dimensions


def update_workbook(path, datasets):
    wb = load_workbook(path)
    numeric = {'size_bytes', 'sequence', 'width', 'height', 'presentation_rotation_cw',
               'master_member_count'}
    for name, fields, rows in datasets:
        sheet = wb[name]
        assert [c.value for c in sheet[1]] == fields
        styles = [copy.copy(c._style) for c in sheet[2]]
        old_max = sheet.max_row
        for ri, row in enumerate(rows, 2):
            for ci, field in enumerate(fields, 1):
                value = row[field]
                if name != 'Catalog' or Path(path).name != 'RESTORE.xlsx':
                    if field in numeric and str(value).isdigit():
                        value = int(value)
                cell = sheet.cell(ri, ci, value)
                if value == '':
                    cell.value = None
                if isinstance(value, str):
                    cell.data_type = 's'
                if ri > old_max:
                    cell._style = copy.copy(styles[ci - 1])
            lines = max([1] + [sum(max(1, len(textwrap.wrap(line,
                        width=max(10, int(sheet.column_dimensions[c.column_letter].width * .9)))))
                        for line in str(c.value or '').split('\n'))
                        for c in sheet[ri] if c.alignment.wrap_text])
            sheet.row_dimensions[ri].height = max(sheet.row_dimensions[ri].height or 23, lines * 14 + 7)
        if old_max > len(rows) + 1:
            sheet.delete_rows(len(rows) + 2, old_max - len(rows) - 1)
        for table in sheet.tables.values():
            table.ref = f'A1:{get_column_letter(len(fields))}{len(rows)+1}'
            if table.autoFilter:
                table.autoFilter.ref = table.ref
    wb.save(path)
    verify_workbook(path, datasets)


def verify_workbook(path, datasets):
    wb = load_workbook(path, read_only=True, data_only=False)
    for name, fields, rows in datasets:
        vals = list(wb[name].values)
        assert list(vals[0]) == fields and len(vals) == len(rows) + 1
        for actual, expected in zip(vals[1:], rows):
            assert [str(x) if x is not None else '' for x in actual] == [str(expected[f]) for f in fields]
    wb.close()


def identical(left, right):
    with Path(left).open('rb') as a, Path(right).open('rb') as b:
        while True:
            chunk = a.read(1024 * 1024)
            if chunk != b.read(1024 * 1024):
                return False
            if not chunk:
                return True


def execute(resume=False):
    selected = preflight(resume)
    original_dirs = {str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    # The latest human selection controls VLL-1013; preserve the older note as history.
    with review.LOCK, review.connect(False) as c:
        obj = next(o for o in review.load_objects(c) if o['inventory'] == 'VLL-1013')
        if not obj['note'].startswith('Current decision: retain the TIFF selected'):
            note = ('Current decision: retain the TIFF selected by Vladimir on 2026-09-15 '
                    'for VLL-1013; accession authorized by "oklets finish that". '
                    'Earlier disposition retained as historical evidence: ' + obj['note'])
            decision = review.build_review(obj, obj['selected_path'], note, 'Vladimir')
            review.record_review(c, obj, decision)
            c.commit()
            review.export_decisions(c)
    selected = preflight(resume)
    oh, objects = read_csv(U / 'metadata/objects.csv')
    ch, catalog = read_csv(U / 'CATALOG.csv')
    rh, restore = read_csv(U / 'db-restore/RESTORE.csv')
    ph, reps = read_csv(U / 'db-restore/manifests/representations.csv')
    dh, dispositions = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    kh, checks = read_csv(U / 'db-restore/checksums.csv')
    checksum = {r['path']: dict(r) for r in checks}
    original_media = {r['final_relative_path']: stat(U / r['final_relative_path']) for r in catalog}
    changed = set()
    progress = json.loads(PROGRESS.read_text()) if resume else {'authorization': AUTHORIZATION, 'files': {}, 'metadata_external': {}}
    assert progress['authorization'] == AUTHORIZATION
    hashed = {path: (row['stat'], row['sha256']) for path, row in progress['files'].items()}
    for path, (stamp, digest) in hashed.items():
        assert stat(U/path) == stamp, 'Completed output changed before resume: ' + path

    def register(path):
        full = U / path
        current = stat(full)
        if path not in hashed:
            with full.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            hashed[path] = (current, digest)
            progress['files'][path] = {'stat': current, 'sha256': digest}
            write_json(PROGRESS, progress)
        assert hashed[path][0] == current, 'File changed after its final hash: ' + path
        checksum[path] = {'path': path, 'sha256': 'sha256:' + hashed[path][1], 'size_bytes': str(current[0])}
        changed.add(path)
        return hashed[path][1]

    def save_csv(path, fields, rows):
        write_csv(U / path, fields, rows)
        changed.add(path)

    def save_json(path, value):
        write_json(U / path, value)
        changed.add(path)

    media = []
    for item in selected:
        obj, source = item['object']['catalogue'], item['source']
        review.mount_guard()
        src = review.verify_candidate(source)
        if item['master'] not in hashed:
            if (U/item['master']).exists():
                assert resume and identical(src, U/item['master']), 'Unregistered output is not the exact source copy'
            else:
                with src.open('rb') as f, (U / item['master']).open('xb') as out:
                    shutil.copyfileobj(f, out, 1024 * 1024)
            try:
                dimensions = embed(U / item['master'], obj)
            except subprocess.CalledProcessError as error:
                assert identical(src, U/item['master']), 'Failed metadata write changed the copied Master'
                progress['metadata_external'][item['master']] = error.stderr.decode(errors='replace').strip()
                write_json(PROGRESS, progress)
                print('Retaining exact Master bytes; metadata in catalogue: ' + obj['archival_label'], flush=True)
        with Image.open(U/item['master']) as im:
            dimensions = im.size
        master_hash = register(item['master'])
        if item['master'] in progress['metadata_external']:
            assert master_hash == source['stored_sha256'].removeprefix('sha256:')
        if item['access'] not in hashed:
            with Image.open(U / item['master']) as master:
                image = ImageOps.exif_transpose(master).convert('RGB')
                with (U / item['access']).open('xb') as out:
                    image.save(out, 'JPEG', quality=92, subsampling=0)
            embed(U / item['access'], obj)
        with Image.open(U/item['access']) as im:
            access_dimensions = im.size
        access_hash = register(item['access'])
        review.verify_candidate(source)
        base = next(o for o in objects if o['iss_id'] == obj['iss_id'])
        base.update(record_state='represented', representation_summary='access_derivative: 1; preservation_master: 1',
                    master_file=item['master'], access_file=item['access'], master_member_count='1', master_parts='',
                    notes_for_review=base['notes_for_review'].replace('no active representation; ', ''))
        if obj['archival_label'] == 'VLL-1013':
            base['notes_for_review'] += '; latest reviewed TIFF selected; earlier duplicate disposition retained in metadata/master-decisions.json'
        prior_restore = next(r for r in restore if r['iss_id'] == obj['iss_id'])
        restore = [r for r in restore if r['iss_id'] != obj['iss_id']]
        for role, path, digest, dims in [('preservation_master', item['master'], master_hash, dimensions),
                                       ('access_derivative', item['access'], access_hash, access_dimensions)]:
            with Image.open(U / path) as im:
                mode, bits = im.mode, 8
            provenance = {'source_path': source['source_path'], 'source_digest': source['stored_sha256'],
                          'master_path': item['master'], 'master_digest': 'sha256:' + master_hash,
                          'derivation': 'metadata_enriched_selected_master' if role == 'preservation_master' else 'generated_from_selected_master',
                          'decision': item['object']['decision'], 'deletion_authorized': False}
            if item['master'] in progress['metadata_external']:
                provenance['master_metadata_handling'] = 'Original JPEG metadata rejected safe writing; preserve exact bytes and catalogue accepted metadata.'
            entry = {'path': path, 'entity': obj['iss_id'], 'role': role, 'hash': digest, 'width': dims[0],
                     'height': dims[1], 'mode': mode, 'note': json.dumps(provenance, ensure_ascii=False)}
            media.append(entry)
            rr = dict(prior_restore)
            rr.update(representation_role=role, representation_status='active', file_status='verified',
                      relative_path=path, original_filename=Path(path).name, sha256=digest,
                      size_bytes=str((U/path).stat().st_size), media_type='image/tiff' if path.endswith('.tif') else 'image/jpeg',
                      width_px=str(dims[0]), height_px=str(dims[1]), bit_depth=str(bits), color_space=mode,
                      observed_mtime_utc=datetime.fromtimestamp((U/path).stat().st_mtime, timezone.utc).isoformat(),
                      provenance_note=entry['note'], record_state='represented')
            restore.append(rr)
            row = {f: '' for f in ch}
            row.update(record_key=obj['iss_id'], inventory=obj['archival_label'], title=obj['descriptive_title'],
                       file_role=role, final_relative_path=path, master_relative_path=item['master'], filename=Path(path).name,
                       file_type=Path(path).suffix[1:], size_bytes=rr['size_bytes'], final_sha256=digest,
                       description=obj['description'], date_text=obj['date_year'], object_type=obj['object_type'],
                       material=obj['material_technique'], dimensions=obj['dimensions'], metadata_authority='Vladimir: saved Master review and retained catalogue metadata',
                       original_source_path=source['source_path'] if role == 'preservation_master' else str(U/item['master']),
                       source_sha256=source['stored_sha256'].removeprefix('sha256:') if role == 'preservation_master' else master_hash,
                       derivative_of='' if role == 'preservation_master' else item['master'], width=str(dims[0]), height=str(dims[1]),
                       relationship='single selected Master' if role == 'preservation_master' else 'generated access image of selected Master',
                       access_restriction='publication blocked', notes=entry['note'], metadata_path='metadata/objects.csv',
                       provenance_path='metadata/master-decisions.json\ndb-restore/manifests/source-disposition.csv')
            if source['format'] == 'JPEG':
                row['condition'] = 'Small surviving JPEG selected by human review; no higher-resolution original is claimed.'
            catalog.append(row)
        reps.append(dict(zip(ph, [obj['iss_id'], obj['archival_label'], 'preservation_master', item['master'],
                                  'sha256:' + master_hash, item['access'], 'sha256:' + access_hash, item['master'],
                                  'generated_from_selected_master', 'local_access', str(dimensions[0]), str(dimensions[1]),
                                  'paragon/accession_reviewed.py', NOW])))
        by_source = {c['source_path']: c for c in item['object']['decision']['candidates']}
        for d in dispositions:
            if d['source_path'] not in by_source:
                continue
            candidate = by_source[d['source_path']]
            chosen = candidate['state'] == 'selected_master'
            d.update(source_role='selected_master_candidate' if chosen else 'redundant_candidate',
                     final_path=item['master'] if chosen else '',
                     preservation_status='preserved_as_metadata_enriched_master' if chosen else 'reviewed_not_selected',
                     preservation_basis='Saved human Master decision; copied image pixels preserved; reliable metadata embedded.' if chosen else 'Saved human decision marks this alternative redundant; exact source retained.',
                     final_sha256='sha256:' + master_hash if chosen else '', exact_copy_proven='false',
                     drain_state='retain_not_preserved', deletion_authorized='false', notes='Source retained; no deletion authorized.')
            if chosen and item['master'] in progress['metadata_external']:
                d.update(preservation_status='preserved_exact_copy', exact_copy_proven='true',
                         preservation_basis='Final Master hash equals stored source hash; metadata recorded in catalogue because embedded metadata is malformed.')
        print('Accessioned ' + obj['archival_label'], flush=True)

    correction = json.loads(review.REPORT.read_text())['metadata_corrections'][0]
    corrected = next(o for o in objects if o['iss_id'] == TITLE_ID)
    corrected.update(correction['accepted_fields'])
    corrected['notes_for_review'] += '; 1963 title corrected to 1962 by Vladimir on 2026-09-15'
    path = corrected['master_file']
    dimensions = embed(U/path, corrected, title_only=True)
    old_digest = checksum[path]['sha256']
    new_digest = register(path)
    for r in reps:
        if r['iss_id'] == TITLE_ID:
            r['master_sha256'] = 'sha256:' + new_digest
    for r in catalog:
        if r['record_key'] == TITLE_ID:
            r.update(title=corrected['descriptive_title'], date_text='1962')
        if r['final_relative_path'] == path:
            r.update(final_sha256=new_digest, size_bytes=checksum[path]['size_bytes'])
        if r['derivative_of'] == path and r['source_sha256'] == old_digest.removeprefix('sha256:'):
            r['source_sha256'] = new_digest
    for r in restore:
        if r['iss_id'] == TITLE_ID:
            r['descriptive_title'] = corrected['descriptive_title']
        if r['relative_path'] == path:
            r.update(sha256=new_digest, size_bytes=checksum[path]['size_bytes'])
    # Export exact current human decisions, including the unresolved exception.
    saved = json.loads(review.DECISIONS.read_text())
    exception = next(o for o in saved['objects'] if o['inventory'] == 'VLV-1092-2')
    for o in objects:
        if o['archival_label'] == 'VLV-1092-2':
            o['notes_for_review'] = 'publication blocked; ' + exception['note']
    for r in restore:
        if r['archival_label'] == 'VLV-1092-2':
            r['object_note'] = exception['note']
    report = json.loads(review.REPORT.read_text())
    report['metadata_corrections'][0].update(archive_applied=True, archive_applied_at=NOW,
                                          applies_to='working and final catalogue; embedded TIFF title')
    save_json('metadata/missing-originals-review.json', report)

    # Update the existing restore schema; no new database or catalogue model.
    con = sqlite3.connect(f'file:{U}/db-restore/RESTORE.sqlite?mode=rw', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    with con:
        for entry in media + [{'path': path, 'hash': new_digest, 'width': dimensions[0], 'height': dimensions[1], 'mode': 'RGB'}]:
            con.execute('INSERT OR IGNORE INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,created_at) VALUES (?,?,?,?,?,?,?,?)',
                        ('sha256:' + entry['hash'], (U/entry['path']).stat().st_size, 'image/tiff' if entry['path'].endswith('.tif') else 'image/jpeg', entry['width'], entry['height'], 8, entry['mode'], NOW))
            if entry not in media:
                con.execute("UPDATE file_occurrences SET content_digest=?, observed_at_utc=? WHERE occurrence_kind='archive' AND relative_path=?", ('sha256:' + entry['hash'], NOW, entry['path']))
                continue
            occurrence = 'OCC-' + uuid4().hex.upper()
            con.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (occurrence, 'WF-PARAGON', 'sha256:' + entry['hash'], 'archive', entry['path'], Path(entry['path']).name,
                         datetime.fromtimestamp((U/entry['path']).stat().st_mtime, timezone.utc).isoformat(), NOW, 'verified', entry['note']))
            con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)', (entry['entity'], occurrence, entry['role'], 'active', NOW))
        with review.connect() as work:
            accepted = dict(work.execute('SELECT * FROM evidence_assertions WHERE assertion_id=?', (correction['sqlite_assertion_id'],)).fetchone())
            source = dict(work.execute('SELECT * FROM evidence_sources WHERE source_id=?', (accepted['source_id'],)).fetchone())
            log = dict(work.execute('SELECT * FROM logbook WHERE entity_id=? AND action=?', (correction['decision_id'], 'accept_reconciliation_field')).fetchone())
            for table, row in [('evidence_sources', source), ('evidence_assertions', accepted), ('logbook', log)]:
                con.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
            reviewed_ids = {o['catalogue']['iss_id'] for o in saved['objects']}
            for row in work.execute("SELECT * FROM logbook WHERE action='review_source_candidate'"):
                if json.loads(row['after_json']).get('entity_id') in reviewed_ids:
                    values = dict(row)
                    con.execute(f'INSERT OR IGNORE INTO logbook ({",".join(values)}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
        con.execute("UPDATE evidence_assertions SET status='superseded' WHERE subject_entity_id=? AND predicate='archive_descriptive_title' AND status='accepted' AND assertion_id<>?", (TITLE_ID, accepted['assertion_id']))
        con.execute('UPDATE archive_objects SET descriptive_title=?, updated_at=? WHERE entity_id=?', (corrected['descriptive_title'], NOW, TITLE_ID))
        con.execute('UPDATE archive_objects SET note=?,updated_at=? WHERE entity_id=?', (exception['note'], NOW, exception['catalogue']['iss_id']))
        for o in saved['objects']:
            if o['inventory'] == 'VLL-1013':
                con.execute('UPDATE archive_objects SET note=?,updated_at=? WHERE entity_id=?', (o['note'], NOW, o['catalogue']['iss_id']))
        con.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
    assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not con.execute('PRAGMA foreign_key_check').fetchall()
    revision = con.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
    (U/'db-restore/RESTORE.sql').write_text('\n'.join(con.iterdump())+'\n')
    con.close()
    changed.update({'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql'})
    for r in restore:
        r['catalog_revision'] = str(revision)
        if r['archival_label'] == 'VLL-1013':
            r['object_note'] = next(o['note'] for o in saved['objects'] if o['inventory'] == 'VLL-1013')
    restore.sort(key=lambda r: (r['archival_label'], r['relative_path']))
    save_csv('metadata/objects.csv', oh, objects)
    with review.connect() as c:
        review.export_decisions(c)
    save_json('metadata/master-decisions.json', json.loads(review.DECISIONS.read_text()))
    save_csv('CATALOG.csv', ch, catalog)
    save_csv('db-restore/RESTORE.csv', rh, restore)
    save_csv('db-restore/manifests/representations.csv', ph, reps)
    save_csv('db-restore/manifests/source-disposition.csv', dh, dispositions)
    update_workbook(U/'CATALOG.xlsx', [('Catalog', ch, catalog), ('Objects', oh, objects)])
    update_workbook(U/'db-restore/RESTORE.xlsx', [('Catalog', rh, restore)])
    changed.update({'CATALOG.xlsx', 'db-restore/RESTORE.xlsx'})
    old = (U/'00_README.md').read_text()
    marker = '\nSimplified archive workflow export.'
    assert marker in old
    intro = f'''
## Current accession — 2026-09-15

Vladimir authorized completion with “{AUTHORIZATION}”. Eight reviewed Masters
have been added: five TIFFs and three surviving JPEGs. Eight full-resolution
access JPEGs were generated from those selected Masters during accession.
The current catalogue contains 240 records, 239 represented objects, 241 Master
members and 241 access files. Each represented object has one selected Master;
VLV-1095 and VLV-1097 retain their two ordered complementary members.

VLV-1092-1's title is corrected to 1962 in the catalogue and embedded TIFF
metadata. Both invitation references belong to 1962. The unverified 1964 record,
VLV-1092-2, remains the single explicit unresolved identity exception, with the
saved human note in metadata/master-decisions.json. No genuine loss is asserted.
The unit remains open for this exception; accession completion is not sealing.

VLL-1013 follows the newer explicit TIFF selection. Its earlier duplicate
disposition remains historical evidence in the review note. No object was merged
or deleted. Missing descriptive metadata for VLV-1127 remains unrecorded rather
than invented. Existing publication restrictions and source retention remain.

Current records: CATALOG.csv/XLSX, metadata/objects.csv, db-restore/RESTORE.*,
db-restore/manifests/representations.csv and source-disposition.csv. Checksums
are in db-restore/checksums.csv; the completion receipt is
db-restore/manifests/reviewed-accession-completion.json. Existing directories and
raw historical museum-digital exports are retained. Source files were unchanged.

## Earlier export and normalization checkpoint
'''
    (U/'00_README.md').write_text(old.replace(marker, intro + marker, 1))
    changed.add('00_README.md')
    for relative in list(changed):
        register(relative)
    export = json.loads((U/'catalog-export-summary.json').read_text())
    export.update(updated_at=NOW, file_rows=len(catalog), object_rows=len(objects))
    for value in export['outputs'].values():
        relative = str(Path(value['path']).relative_to(U))
        value['sha256'] = checksum[relative]['sha256']
    export['reviewed_accession'] = {'objects_added': 8, 'represented_objects': 239, 'unresolved_objects': 1,
                                   'receipt': RECEIPT, 'deletion_authorized': False}
    save_json('catalog-export-summary.json', export)
    summary = json.loads((U/'db-restore/export-summary.json').read_text())
    summary.update(updated_at=NOW, masters=len(reps), access=len(reps), generated_access=8,
                   catalog_state={'catalog_revision': revision, 'published_revision': revision, 'dirty_since': None})
    summary['restore']['rows'] = len(restore)
    summary['checksums'] = len(checksum)
    save_json('db-restore/export-summary.json', summary)
    register('catalog-export-summary.json')
    register('db-restore/export-summary.json')
    save_csv('db-restore/checksums.csv', kh, sorted(checksum.values(), key=lambda r: r['path']))
    assert {str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()} == original_dirs
    for relative, prior in original_media.items():
        if relative != path:
            assert stat(U/relative) == prior, 'Unrelated media changed: ' + relative
    for relative, (stamp, digest) in hashed.items():
        assert stat(U/relative) == stamp
    for item in selected:
        review.verify_candidate(item['source'])
    receipt = {'unit': 'paragon', 'authorization': AUTHORIZATION, 'completed_at': NOW,
               'accession_performed': True, 'validation_result': 'ok', 'objects': 240,
               'represented_objects': 239, 'master_representations': 239, 'master_members': 241,
               'new_masters': 8, 'new_derivatives': 8, 'redundant_review_candidates': 11,
               'unresolved_objects': ['VLV-1092-2'], 'unit_closed': False,
               'title_correction': correction['accepted_fields'], 'media_hashed_after_final_write': len(media)+1,
               'master_metadata_stored_in_catalogue': progress['metadata_external'],
               'sources_modified': False, 'deletion_authorized': False, 'publication_performed': False,
               'catalog_rows': len(catalog), 'restore_rows': len(restore), 'catalog_revision': revision,
               'checksums_file': 'db-restore/checksums.csv', 'checksums_sha256': hashlib.sha256((U/'db-restore/checksums.csv').read_bytes()).hexdigest()}
    write_json(U/RECEIPT, receipt)
    write_json(W/'reviewed-accession-completion.json', receipt)
    write_json(review.REPORT, report)
    with review.connect() as c:
        review.export_decisions(c)
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if args.apply:
        execute(args.resume)
    else:
        plan = preflight()
        print(json.dumps({'ready': True, 'masters': [p['master'] for p in plan],
                          'title_correction': 'VLV-1092-1: 1963 to 1962',
                          'unresolved_exception': 'VLV-1092-2'}, indent=2))
