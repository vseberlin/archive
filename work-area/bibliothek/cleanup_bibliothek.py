#!/usr/bin/env python3
"""Bounded removal of the confirmed unrelated Urbanitzky attachment.

Preserve excluded accession copies in the existing work directory. Keep the
two accepted book representations and all original source evidence unchanged.
"""
import copy
import csv
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from PIL import Image, ImageDraw, ImageFont

WORK = Path('/mnt/volume/iss-archive-work/bibliothek')
UNIT = Path('/mnt/volume/iss-archive/units/bibliothek')
DB = UNIT / 'db-restore/catalog.sqlite'
EXCLUDED = 'BIB-1002'
MOVES = {
    'masters/BIB-1002-md-106189.pdf': 'excluded-BIB-1002-md-106189.pdf',
    'export/access/BIB-1002-md-106189-preview.jpg': 'excluded-BIB-1002-md-106189-preview.jpg',
    'metadata/BIB-1002-md-106189-technical.json': 'excluded-BIB-1002-md-106189-technical.json',
    'metadata/BIB-1002-local-00889e1311ce-technical.json': 'excluded-BIB-1002-local-00889e1311ce-technical.json',
}
AUTHORIZATION = 'vlean biblioteque from unrelated content'
NOTE = ('2026-09-15: unrelated MD newspaper attachment 106189 excluded at '
        "Vladimir's request. Urbanitzky is outside the current accession and "
        'awaits selection of a correct local Master in the existing work area. '
        'The three local five-page scans remain retained at source. No deletion authorized.')


def read_csv(path):
    with path.open(newline='') as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def csv_bytes(fields, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def stamp(path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def render(sheet, target):
    """Render literal opening cells using the saved workbook's layout/styles."""
    columns = 5
    rows = min(sheet.max_row, 8)
    font_path = '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'
    bold_path = '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf'
    widths = [round(sheet.column_dimensions[get_column_letter(i)].width * 7 + 5)
              for i in range(1, columns + 1)]
    heights = [round((sheet.row_dimensions[i].height or 24) * 4 / 3)
               for i in range(1, rows + 1)]
    result = Image.new('RGB', (sum(widths), sum(heights)), 'white')
    draw = ImageDraw.Draw(result)
    y = 0
    for ri, height in enumerate(heights, 1):
        x = 0
        for ci, width in enumerate(widths, 1):
            cell = sheet.cell(ri, ci)
            font = ImageFont.truetype(bold_path if cell.font.b else font_path,
                                      round((cell.font.sz or 10) * 4 / 3))
            fill = '#24445B' if ri == 1 else 'white'
            draw.rectangle((x, y, x + width, y + height), fill=fill)
            lines = []
            for paragraph in str(cell.value or '').split('\n'):
                line = ''
                for word in paragraph.split():
                    if line and draw.textlength(line + ' ' + word, font=font) > width - 12:
                        lines.append(line)
                        line = ''
                    line += (' ' if line else '') + word
                lines.append(line)
            assert len(lines) * 15 <= height - 2, (sheet.title, ri, ci, 'clipped text')
            for index, line in enumerate(lines):
                draw.text((x + 5, y + 1 + index * 15), line, font=font,
                          fill='white' if ri == 1 else '#202020')
            x += width
        draw.line((0, y + height - 1, result.width, y + height - 1), fill='#D5DADF')
        y += height
    result.save(target)


def inspect():
    workbook = load_workbook(UNIT / 'CATALOG.xlsx')
    for sheet in workbook:
        render(sheet, WORK / ('cleanup-before-' + sheet.title + '.png'))
    print('Current Catalog and Objects opening views rendered; no archive writes.')


def apply():
    assert not (WORK / 'unrelated-content-cleanup.json').exists(), 'Cleanup already recorded'
    now = datetime.now(timezone.utc).isoformat()
    directories = {str(p.relative_to(UNIT)) for p in UNIT.rglob('*') if p.is_dir()}
    con = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    tables = {name: [dict(r) for r in con.execute('SELECT * FROM ' + name)]
              for name in ('objects', 'files', 'source_disposition', 'candidate_dispositions')}
    schema = list(con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))
    con.close()
    assert {r['object_key'] for r in tables['objects']} == {'BIB-1001', EXCLUDED, 'BIB-1003'}
    old_files = {r['relative_path']: dict(r) for r in tables['files']}
    old_stamps = {path: stamp(UNIT / path) for path in old_files}
    for old, new in MOVES.items():
        assert old in old_files and not (WORK / new).exists(), (old, new)
        assert old_stamps[old][0] == int(old_files[old]['size_bytes'])

    final_changes = {}
    work_changes = {}
    work_decision_fields, work_decisions = read_csv(WORK / 'master-decisions.csv')
    for row in work_decisions:
        if row['object_key'] == EXCLUDED:
            row.update(selected_candidate_id='', decision='unresolved', actor='Vladimir',
                       decided_at=now, note=NOTE)
    work_changes['master-decisions.csv'] = csv_bytes(work_decision_fields, work_decisions)
    final_changes['00_MANIFESTS/master-decisions.csv'] = csv_bytes(
        work_decision_fields, [r for r in work_decisions if r['object_key'] != EXCLUDED])

    fields, dispositions = read_csv(WORK / 'candidate-dispositions.csv')
    for row in dispositions:
        if row['object_key'] == EXCLUDED:
            row['disposition'] = 'excluded' if row['candidate_id'] == 'BIB-1002:md' else 'unresolved'
    work_changes['candidate-dispositions.csv'] = csv_bytes(fields, dispositions)
    tables['candidate_dispositions'] = [r for r in dispositions if r['object_key'] != EXCLUDED]
    final_changes['00_MANIFESTS/candidate-dispositions.csv'] = csv_bytes(fields, tables['candidate_dispositions'])

    fields, candidates = read_csv(WORK / 'candidates.csv')
    for row in candidates:
        if row['candidate_id'] == 'BIB-1002:md':
            row.update(eligible='false', basis=NOTE,
                       comparison='Excluded unrelated newspaper page; not an Urbanitzky book scan.')
    work_changes['candidates.csv'] = csv_bytes(fields, candidates)
    final_changes['metadata/candidates.csv'] = csv_bytes(fields, [r for r in candidates if r['object_key'] != EXCLUDED])
    for relative in ('metadata/md-objects.csv', 'metadata/md-resources.csv'):
        fields, rows = read_csv(UNIT / relative)
        final_changes[relative] = csv_bytes(fields, [r for r in rows if r['object_key'] != EXCLUDED])

    tables['objects'] = [r for r in tables['objects'] if r['object_key'] != EXCLUDED]
    object_fields = list(tables['objects'][0])
    final_changes['00_MANIFESTS/master-representations.csv'] = csv_bytes(object_fields, tables['objects'])
    final_changes['metadata/objects.jsonl'] = ''.join(json.dumps(r, ensure_ascii=False) + '\n'
                                                   for r in tables['objects']).encode()
    for row in tables['source_disposition']:
        if row['inventory'] == EXCLUDED:
            row.update(final_path='', final_sha256='', exact_copy_proven='false',
                       source_role='excluded_candidate' if row['candidate_ids'] == 'BIB-1002:md' else 'unresolved_candidate',
                       preservation_status='not_preserved', preservation_basis=NOTE,
                       drain_state='retain_required', notes=NOTE)
    fields, _ = read_csv(UNIT / '00_MANIFESTS/source-disposition.csv')
    final_changes['00_MANIFESTS/source-disposition.csv'] = csv_bytes(fields, tables['source_disposition'])
    tables['files'] = [r for r in tables['files'] if r['relative_path'] not in MOVES]

    # Hash only finalized changed metadata payloads. Retained media hashes are reused.
    for row in tables['files']:
        relative = row['relative_path']
        if relative in final_changes:
            payload = final_changes[relative]
            row.update(size_bytes=str(len(payload)), final_sha256=sha256(payload).hexdigest())
    file_fields = list(tables['files'][0])
    final_changes['db-restore/files.csv'] = csv_bytes(file_fields, tables['files'])
    for name, role in [('master-files.csv', 'master_member'), ('derivatives.csv', 'access_preview')]:
        final_changes['00_MANIFESTS/' + name] = csv_bytes(file_fields, [r for r in tables['files'] if r['file_role'] == role])

    catalog_fields, catalog = read_csv(UNIT / 'CATALOG.csv')
    catalog = [r for r in catalog if r['final_relative_path'] not in MOVES]
    for row in catalog:
        native = next(r for r in tables['files'] if r['relative_path'] == row['final_relative_path'])
        row.update(size_bytes=native['size_bytes'], final_sha256=native['final_sha256'])
    final_changes['CATALOG.csv'] = csv_bytes(catalog_fields, catalog)
    common_fields, common_objects = read_csv(UNIT / 'db-restore/objects.csv')
    common_objects = [r for r in common_objects if r['record_key'] != EXCLUDED]
    final_changes['db-restore/objects.csv'] = csv_bytes(common_fields, common_objects)

    workbook = load_workbook(UNIT / 'CATALOG.xlsx')
    original_workbook = load_workbook(UNIT / 'CATALOG.xlsx')
    for sheet_name, fields, records, identity in [
        ('Catalog', catalog_fields, catalog, 'final_relative_path'),
        ('Objects', common_fields, common_objects, 'record_key'),
    ]:
        sheet = workbook[sheet_name]
        original = original_workbook[sheet_name]
        identity_col = fields.index(identity) + 1
        old_row_map = {str(original.cell(i, identity_col).value): i for i in range(2, original.max_row + 1)}
        keep = {r[identity] for r in records}
        for index in range(sheet.max_row, 1, -1):
            if str(sheet.cell(index, identity_col).value) not in keep:
                sheet.delete_rows(index)
        for index, row in enumerate(records, 2):
            original_index = old_row_map[row[identity]]
            sheet.row_dimensions[index].height = original.row_dimensions[original_index].height
            for column, field in enumerate(fields, 1):
                old_cell = original.cell(original_index, column)
                cell = sheet.cell(index, column)
                value = row[field]
                cell.value = int(value) if value and isinstance(old_cell.value, (int, float)) else (value or None)
                cell._style = copy.copy(old_cell._style)
                if value and not isinstance(cell.value, (int, float)):
                    cell.data_type = 's'
        for table in sheet.tables.values():
            table.ref = f'A1:{get_column_letter(len(fields))}{len(records) + 1}'
            if table.autoFilter:
                table.autoFilter.ref = table.ref
        assert sheet.freeze_panes == original.freeze_panes
        assert {k: dict(v) for k, v in sheet.column_dimensions.items()} == {k: dict(v) for k, v in original.column_dimensions.items()}
    buffer = io.BytesIO()
    workbook.save(buffer)
    check_workbook = load_workbook(io.BytesIO(buffer.getvalue()))
    for name, fields, records in [('Catalog', catalog_fields, catalog), ('Objects', common_fields, common_objects)]:
        actual = [['' if c.value is None else str(c.value) for c in row] for row in check_workbook[name]]
        assert actual == [fields] + [[r[f] for f in fields] for r in records], name
        assert all(c.data_type != 'f' for row in check_workbook[name] for c in row)
    final_changes['CATALOG.xlsx'] = buffer.getvalue()

    readme = (UNIT / '00_README.md').read_text()
    description_end = readme.index('<!-- md-collection-description:end -->') + len('<!-- md-collection-description:end -->')
    final_changes['00_README.md'] = (readme[:description_end] + f'''

## Current accession after unrelated-content cleanup

Updated {now}. Vladimir requested: "{AUTHORIZATION}".
The active accession contains two books, each with one selected Master representation:

- BIB-1001 / Beck: ordered cover JPEG and four-page PDF. Partial book scan.
- BIB-1003 / Wilke: three-page PDF of title and contents. Partial book scan.

There are three Master files and three existing accession-generated JPEG previews.
CATALOG.csv and CATALOG.xlsx contain {len(catalog)} matching file rows. The Objects
sheet, db-restore/objects.csv, metadata/objects.jsonl, master-representations.csv
and SQLite contain the same two accessioned objects.

## Urbanitzky returned to reconciliation

BIB-1002 remains a known book in /mnt/volume/iss-archive-work/bibliothek.
MD attachment 106189 is an unrelated newspaper page. Its former accession PDF,
preview and related technical evidence were moved into that existing work
directory under explicit excluded-* names, without deleting their bytes.
The wrong attachment is excluded and its Master selection is cleared in work.
The three identical local five-page Urbanitzky scans are unresolved candidates.
They remain at source; no replacement Master was automatically selected.

Current accession metadata and object views exclude the unrelated attachment
and Urbanitzky's contaminated description. The original full MD capture is
retained verbatim in metadata/md-export.json and the work area as source evidence.
It is not the current object catalogue. The earlier master-confirmation receipt
records its original checkpoint; current selections are master-decisions.csv.

## Provenance and recovery

The existing 12-row source-disposition register retains all source paths. Eight
have exact Master coverage; Urbanitzky's four candidate paths have no final
coverage and must be retained. Every deletion flag remains false.
Five alternative representations of the two accessioned books remain redundant.
The four Urbanitzky candidates are retained in the work records.

masters/ holds selected preservation members. export/access/ holds the three
retained previews. Metadata, source evidence and readable manifests retain their
existing locations. db-restore/catalog.sqlite is rebuildable from the documented
CSV sources. The approved directory structure is unchanged.

00_MANIFESTS/accession-completion.json records current counts and this cleanup.
Only changed metadata payloads were hashed. Retained media bytes and stored
hashes were reused. No derivative generation, source change, publication or
deletion occurred during cleanup.
''').encode()
    final_changes['db-restore/README.md'] = b'''# Rebuildable Bibliothek catalogue

The filesystem and readable records are primary. catalog.sqlite has four
unchanged TEXT-column tables, rebuilt with Python csv.DictReader and sqlite3:

- objects: ../00_MANIFESTS/master-representations.csv; key object_key
- files: files.csv; key relative_path
- source_disposition: ../00_MANIFESTS/source-disposition.csv; key source_path
- candidate_dispositions: ../00_MANIFESTS/candidate-dispositions.csv; key candidate_id

Preserve CSV column order and literal values, including empty strings.
CATALOG.csv and the Catalog worksheet are the common file view. objects.csv and
the Objects worksheet contain the same two active objects plus their native
columns. The excluded Urbanitzky book remains in the existing work area.
'''

    report = dict(checked_at=now, authorization=AUTHORIZATION, result='passed',
        unit='bibliothek', active_objects=2, selected_master_representations=2,
        master_files=3, retained_previews=3, payload_files=len(catalog),
        excluded_work_files=[dict(former_archive_path=old, work_path=str(WORK / new),
            stored_sha256=old_files[old]['final_sha256'], size_bytes=old_files[old]['size_bytes'])
            for old, new in MOVES.items()],
        unresolved_work_object=EXCLUDED, work_candidate_dispositions={'excluded': 1, 'unresolved': 3},
        source_paths=12, exact_copy_source_paths=8, source_modified=False,
        media_generated=0, media_rehashed=0, deletion_authorized=False,
        directory_structure_unchanged=True, schema_unchanged=True,
        sqlite_csv_xlsx_agree=True,
        workbook_engine='Artifact Tool import unavailable: WASM SIMD; bundled openpyxl fallback',
        initial_master_choices_preserved=['BIB-1001:md', 'BIB-1003:md'])

    receipt = json.loads((UNIT / '00_MANIFESTS/accession-completion.json').read_text())
    receipt['initial_accession_counts'] = {k: receipt[k] for k in [
        'objects', 'master_representations', 'master_files', 'pdf_master_pages',
        'derivatives_generated', 'redundant_candidates', 'exact_copy_source_paths',
        'payload_files', 'final_files']}
    receipt.update(updated_at=now, objects=2, master_representations=2, master_files=3,
        pdf_master_pages=7, retained_derivatives=3, redundant_candidates=5,
        exact_copy_source_paths=8, payload_files=len(catalog),
        final_files=sum(p.is_file() for p in UNIT.rglob('*')) - len(MOVES),
        known_content_mismatch='Unrelated Urbanitzky attachment excluded from accession; correct book remains unresolved in work.',
        BIB_1002_book_content_preserved=False, BIB_1002_attachment_identity_conflict_resolved=True,
        BIB_1002_current_status='outside_accession_pending_correct_master',
        unrelated_content_cleanup=report)
    final_changes['00_MANIFESTS/accession-completion.json'] = json_bytes(receipt)

    # All intended data are prepared and checked before the first archive change.
    for relative in old_files:
        assert stamp(UNIT / relative) == old_stamps[relative]
    for old, new in MOVES.items():
        (UNIT / old).rename(WORK / new)
        assert stamp(WORK / new) == old_stamps[old]
    for name, payload in final_changes.items():
        (UNIT / name).write_bytes(payload)
    for name, payload in work_changes.items():
        (WORK / name).write_bytes(payload)

    checksum_fields, checksum_rows = read_csv(UNIT / '00_MANIFESTS/payload-checksums.csv')
    checksum_rows = [r for r in checksum_rows if r['relative_path'] not in MOVES]
    by_path = {r['relative_path']: r for r in tables['files']}
    for row in checksum_rows:
        relative = row['relative_path']
        native = by_path[relative]
        row.update(size_bytes=native['size_bytes'], sha256=native['final_sha256'],
                   mtime_ns=str((UNIT / relative).stat().st_mtime_ns))
    (UNIT / '00_MANIFESTS/payload-checksums.csv').write_bytes(csv_bytes(checksum_fields, checksum_rows))

    con = sqlite3.connect(DB)
    with con:
        for table, rows in tables.items():
            fields = list(rows[0])
            con.execute('DELETE FROM ' + table)
            con.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in fields) + ')',
                            [[r[f] for f in fields] for r in rows])
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert list(con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")) == [tuple(r) for r in schema]
    con.close()

    check_con = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)
    for table, rows in tables.items():
        assert check_con.execute('SELECT * FROM ' + table).fetchall() == [tuple(r.values()) for r in rows]
    check_con.close()
    for row in catalog:
        path = row['final_relative_path']
        assert (UNIT / path).stat().st_size == int(row['size_bytes'])
        if path not in final_changes:
            assert stamp(UNIT / path) == old_stamps[path]
            assert row['final_sha256'] == old_files[path]['final_sha256']
    assert {str(p.relative_to(UNIT)) for p in UNIT.rglob('*') if p.is_dir()} == directories
    assert {r['object_key'] for r in tables['objects']} == {'BIB-1001', 'BIB-1003'}
    for obj in tables['objects']:
        members = sorted([r for r in tables['files'] if r['object_key'] == obj['object_key'] and r['file_role'] == 'master_member'], key=lambda r: int(r['sequence']))
        assert obj['selected_candidate_id'] == obj['object_key'] + ':md'
        assert json.loads(obj['ordered_master_paths']) == [r['relative_path'] for r in members]
    assert len(catalog) == len(checksum_rows) == 23
    assert all(r['deletion_authorized'] == 'false' for r in tables['source_disposition'])
    for sheet in check_workbook:
        render(sheet, WORK / ('cleanup-after-' + sheet.title + '.png'))
    (WORK / 'unrelated-content-cleanup.json').write_bytes(json_bytes(report))
    (WORK / 'handoff_CURRENT.md').write_text(f'''# Bibliothek cleanup completed — 2026-09-15

{NOTE}

The final accession contains Beck and Wilke: two selected representations,
three Master files, three retained previews and 23 registered payload files.
SQLite, native/common CSV and both XLSX sheets agree. Current archive counts
are in 00_MANIFESTS/accession-completion.json; the initial accession counts are
retained separately there. Source and media bytes were not changed.

The four excluded accession copies are preserved under excluded-* names here.
unrelated-content-cleanup.json records exact former/new paths and stored hashes.
The work decision for BIB-1002 is unresolved, the wrong MD candidate is excluded,
and its three correct local candidate copies await a human Master selection.
The original MD capture, original book description and technical evidence remain.
No new directory, database, schema, derivative, publication or deletion.

The source is currently mounted /dev/sdf1 /media/vladimir/Elements ntfs3 ro.
Recheck the mount before source access. Do not rerun completed accession or cleanup.
''')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    assert sys.argv[1:] in (['--inspect'], ['--apply'])
    inspect() if sys.argv[1] == '--inspect' else apply()
