#!/usr/bin/env python3
"""Bounded AEB accession. Default/--check is read-only; --execute requires explicit authority.

An existing target is never overwritten. Interrupted output is retained for
inspection; this program never deletes, rolls back, or creates another generation.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from PIL import Image, ImageOps
import prepare_accession_591 as prep

ROOT, TARGET, DIRS = prep.ROOT, prep.TARGET, prep.DIRS
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def now():
    return datetime.now(timezone.utc).isoformat()


def xlsx_bytes(rows):
    """Existing archive literal-cell OOXML convention, with exact cell readback."""
    fields = list(rows[0])
    matrix = [fields] + [[str(row[key]) for key in fields] for row in rows]

    def column(n):
        result = ''
        while n:
            n, remainder = divmod(n - 1, 26)
            result = chr(65 + remainder) + result
        return result

    body = []
    for row_number, values in enumerate(matrix, 1):
        cells = []
        for col_number, value in enumerate(values, 1):
            prep.require(len(value) <= 32767 and not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value),
                         'XLSX would lose a catalogue value')
            literal = escape(value).replace('\r', '&#13;')
            cells.append(f'<c r="{column(col_number)}{row_number}" t="inlineStr"><is><t xml:space="preserve">{literal}</t></is></c>')
        body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
             f'<cols><col min="1" max="{len(fields)}" width="30" customWidth="1"/></cols>'
             f'<sheetData>{"".join(body)}</sheetData><autoFilter ref="A1:{column(len(fields))}{len(matrix)}"/></worksheet>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="CATALOG" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', sheet)
    output.seek(0)
    with zipfile.ZipFile(output) as archive:
        prep.require(archive.testzip() is None, 'XLSX archive is damaged')
        tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        actual = [[cell.find(f'{{{NS}}}is/{{{NS}}}t').text or '' for cell in row]
                  for row in tree.find(f'{{{NS}}}sheetData')]
        prep.require(actual == matrix, 'XLSX cells differ from CSV values')
        prep.require(not tree.findall(f'.//{{{NS}}}f'), 'Catalogue values must not become formulas')
    return output.getvalue()


def preflight():
    plan = prep.build_plan()
    prep.check_saved(plan)
    prep.require(TARGET == prep.TARGET and tuple(plan['structure']) == DIRS, 'Output configuration differs')
    return plan


def assert_inputs(plan):
    prep.guard()
    for path, expected in plan['input_stamps'].items():
        prep.checked_input(path, *expected)


def destination(relative):
    if relative in {'00_README.md', 'CATALOG.csv', 'CATALOG.xlsx'}:
        return TARGET / relative
    return TARGET / prep.checked_relative(relative)


def write_new(relative, payload):
    target = destination(relative)
    with target.open('xb') as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def identical(left, right):
    with Path(left).open('rb') as source, Path(right).open('rb') as copy:
        while True:
            a, b = source.read(1024 * 1024), copy.read(1024 * 1024)
            if a != b:
                return False
            if not a:
                return True


def finalize_sources(plan, catalog):
    """Use final payload hashes; retain nonidentical variants without exact coverage claims."""
    by_path = {row['relative_path']: row for row in catalog}
    result = []
    for original in plan['source_disposition']:
        row = dict(original)
        final = by_path[row['final_path']]
        row['final_sha256'] = final['final_sha256']
        exact = bool(row['source_sha256']) and row['source_sha256'] == final['final_sha256']
        row['exact_copy_proven'] = 'true' if exact else 'false'
        row['preservation_status'] = 'preserved_exact_copy' if exact else 'object_master_preserved_source_bytes_not_preserved'
        row['preservation_basis'] = ('Stored source hash equals final output hash; source size and mtime unchanged.'
                                      if exact else 'Selected object Master accessioned; this redundant source has different bytes and remains retained.')
        row['notes'] = row['notes'].replace('final_path is proposed only; no accession coverage is claimed.',
                                          'Final selected Master/document path recorded; source retained.')
        row['notes'] = row['notes'].replace('Proposed destination only.', 'Final selected Master/document path recorded; source retained.')
        prep.require(row['deletion_authorized'] == 'false', 'Deletion authority must remain false')
        result.append(row)
    return result


def execute(authorization):
    prep.require(bool(authorization.strip()), 'Explicit accession authorization text is required')
    plan = preflight()
    assert_inputs(plan)
    started = now()
    # This is the first archive mutation. Everything above is a read-only gate.
    TARGET.mkdir(exist_ok=False)
    for name in DIRS:
        (TARGET / name).mkdir(exist_ok=False)
    catalog, checksums = [], []

    def register(row):
        target = destination(row['relative_path'])
        with target.open('rb') as output:
            digest = hashlib.file_digest(output, 'sha256').hexdigest()
        size, mtime = prep.stamp(target)
        final = dict(row, size_bytes=size, final_sha256=digest)
        catalog.append(final)
        checksums.append(dict(relative_path=row['relative_path'], size_bytes=size, mtime_ns=mtime, sha256=digest))
        return final

    for index, row in enumerate(plan['files'], 1):
        relative = row['relative_path']
        if relative in plan['generated_payloads']:
            write_new(relative, plan['generated_payloads'][relative].encode())
        else:
            source = Path(row['source_path'])
            prep.checked_input(source, row['source_size_bytes'], row['source_mtime_ns'])
            with source.open('rb') as src, destination(relative).open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            prep.checked_input(source, row['source_size_bytes'], row['source_mtime_ns'])
            if not row['source_sha256']:
                prep.require(identical(source, destination(relative)), f'Evidence copy differs: {relative}')
        final = register(row)
        if row['source_sha256']:
            prep.require(final['final_sha256'] == row['source_sha256'], f'Copied payload differs from its stored hash: {relative}')
        if row['file_role'] == 'master_member':
            if relative.endswith('.pdf'):
                info = subprocess.run(['pdfinfo', str(destination(relative))], capture_output=True, text=True, timeout=30, check=True).stdout
                match = re.search(r'^Pages:\s+(\d+)', info, re.M)
                prep.require(match is not None and int(match[1]) == int(row['pages']), f'PDF extent changed: {relative}')
            else:
                with Image.open(destination(relative)) as image:
                    image.load()
                    prep.require(image.format == 'JPEG' and ImageOps.exif_transpose(image).size == (int(row['width']), int(row['height'])),
                                 f'Image format or dimensions differ: {relative}')
        if index % 25 == 0 or index == len(plan['files']):
            print(f'Copied/generated and validated {index}/{len(plan["files"])} payloads', flush=True)
    by_path = {row['relative_path']: row for row in catalog}
    for index, planned in enumerate(plan['derivatives'], 1):
        row = dict(planned)
        master_row = by_path[row['master_relative_path']]
        master, target = destination(row['master_relative_path']), destination(row['relative_path'])
        if master.suffix == '.pdf':
            with target.open('xb') as output:
                subprocess.run(['pdftoppm', '-f', '1', '-l', '1', '-singlefile', '-scale-to', '1600',
                                '-jpeg', '-jpegopt', 'quality=85', str(master)], stdout=output,
                               stderr=subprocess.PIPE, timeout=120, check=True,
                               env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
        else:
            with Image.open(master) as original:
                image = ImageOps.exif_transpose(original)
                image.thumbnail((1600, 1600))
                with target.open('xb') as output:
                    image.convert('RGB').save(output, format='JPEG', quality=85)
        with Image.open(target) as image:
            image.load()
            prep.require(image.format == 'JPEG' and 0 < max(image.size) <= 1600, 'Invalid generated preview')
            row.update(width=str(image.width), height=str(image.height))
        row.update(source_sha256=master_row['final_sha256'], source_size_bytes=master_row['size_bytes'],
                   source_mtime_ns=prep.stamp(master)[1])
        register(row)
        if index % 25 == 0 or index == len(plan['derivatives']):
            print(f'Generated and decoded {index}/{len(plan["derivatives"])} previews', flush=True)
    assert_inputs(plan)
    sources = finalize_sources(plan, catalog)
    masters = [row for row in catalog if row['file_role'] == 'master_member']
    derivatives = [row for row in catalog if row['file_role'] == 'access_preview']
    ordered = [path for row in plan['objects'] for path in json.loads(row['ordered_master_paths'])]
    prep.require(len(ordered) == len(set(ordered)) == 109 and set(ordered) == {row['relative_path'] for row in masters},
                 'One-Master ordered representation mapping differs')
    prep.require(len(catalog) == plan['summary']['planned_catalogue_rows'], 'Catalogue extent differs')
    tables = [('objects', plan['objects'], 'md_object_id'), ('files', catalog, 'relative_path'),
              ('source_disposition', sources, 'source_path')]
    for relative, rows in [('00_MANIFESTS/master-representations.csv', plan['objects']),
                           ('00_MANIFESTS/master-files.csv', masters), ('00_MANIFESTS/derivatives.csv', derivatives),
                           ('00_MANIFESTS/source-disposition.csv', sources),
                           ('00_MANIFESTS/payload-checksums.csv', checksums), ('CATALOG.csv', catalog)]:
        write_new(relative, prep.csv_bytes(rows))
    write_new('CATALOG.xlsx', xlsx_bytes(catalog))
    connection = sqlite3.connect(destination('db-restore/catalog.sqlite'))
    try:
        with connection:
            for name, rows, primary in tables:
                fields = list(rows[0])
                columns = ','.join('"' + key + '" TEXT' + (' PRIMARY KEY' if key == primary else '') for key in fields)
                connection.execute(f'CREATE TABLE {name} ({columns})')
                expected = [tuple(str(row[key]) for key in fields) for row in rows]
                connection.executemany(f'INSERT INTO {name} VALUES ({",".join("?" for _ in fields)})', expected)
                prep.require(connection.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall() == expected,
                             f'SQLite and readable records differ: {name}')
            prep.require(connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'SQLite integrity check failed')
    finally:
        connection.close()
    write_new('db-restore/README.md', b'''# Rebuildable AEB catalogue

The filesystem and readable manifests are primary. catalog.sqlite has three TEXT-column tables, each with one primary key. Rebuild objects from ../00_MANIFESTS/master-representations.csv (md_object_id), files from ../CATALOG.csv (relative_path), and source_disposition from ../00_MANIFESTS/source-disposition.csv (source_path). CSV column names and order are the table column names and order. Use csv.DictReader and parameterized sqlite3 inserts; preserve every cell literally, including empty strings. No external schema, ORM, service, identifier registry, or history is needed. CATALOG.xlsx is a literal-cell view of the same file rows.
''')
    contents = ['# AEB supporting documentation', '', 'Seven accepted original files. Original bytes are preserved.', '']
    for document in plan['documents']:
        if document['decision'] != 'included':
            continue
        contents.extend([f"## {Path(document['source_path']).name}", '', document['description'], '',
                         f"Date: {document['document_date'] or 'unknown'}. Basis: {document['date_basis'] or 'not recorded'}.",
                         f"Relationship: {document['relationship']}. Condition: {document['condition']}.",
                         f"Source: `{document['source_path']}`", '', f"Content evidence: {document['content_evidence']}", ''])
    write_new('documents/CONTENTS.md', ('\n'.join(contents) + '\n').encode())
    readme = f'''# Sammlung AEB — Collection 591

Accession started {started}. Explicit user authorization: {authorization}

68 objects have exactly one selected MD Master representation each. Forty are single files and 28 are ordered sets, together 109 members: 67 PDFs (638 pages) and 42 original JPEG resources. See `00_MANIFESTS/master-representations.csv` for complete member order and `master-files.csv` for source/final hashes. An ordered set is one Master representation, not competing Masters. Sources remain byte-for-byte unchanged.

`documents/CONTENTS.md` describes the seven accepted original documents. Eight wider-scope document groups (10 paths) are excluded from this unit, with saved scope decisions retained in `00_MANIFESTS/documentation-review.csv`. Their source paths do not appear in the AEB source-disposition register. The mixed DIGIS presentation remains a retained shared source.

The 109 JPEG files in `export/` were generated from the final selected Master members during accession. PDF previews show page 1 only. Originals are never replaced by these previews. Full MD object metadata, descriptions, events, resource rights/creator, local comparisons, search bounds and documentation evidence are in `metadata/` and the catalogues. Source bytes were not rewritten to embed metadata. The local unit is now authoritative; MD is its projection, with no MD update or publication performed here.

`CATALOG.csv` and `CATALOG.xlsx` contain the same {len(catalog)} payload rows. `db-restore/catalog.sqlite` has three small tables rebuildable from CSV. `00_MANIFESTS/payload-checksums.csv` records each final payload hash once, after its last write; catalogues, manifests, descriptions, database and receipts are controls outside that payload list. Each copied payload with an existing source hash was checked against it; other evidence copies were compared byte-for-byte.

`00_MANIFESTS/source-disposition.csv` covers 122 exact source paths: 109 MD work captures, six redundant USB alternatives, and seven accepted documentation files. Three redundant alternatives are exact copies of the final selected MD member; three have different bytes and only reviewed object/content coverage. Every source is retained. All deletion_authorized values are false. No deletion, source change, sealing, publication, source cleanup or new directory hierarchy is authorized by this accession.

The captured search screened 916 indexed paths. Ten broad title queries and the Wissenschaftsorganisation query were capped; unindexed USB files were not exhaustively scanned. Wider context and rejected visual leads remain search evidence, outside the AEB drain register. Copied preparation/search reports describe their original checkpoints. `00_MANIFESTS/accession-completion.json` determines completed accession status; historical reports do not override it.
'''
    write_new('00_README.md', readme.encode())
    assert_inputs(plan)
    for item in checksums:
        prep.require(prep.stamp(destination(item['relative_path'])) == (item['size_bytes'], item['mtime_ns']),
                     'A finalized payload changed after hashing')
    expected_files = {row['relative_path'] for row in catalog} | set(plan['controls'])
    actual_files = {str(path.relative_to(TARGET)) for path in TARGET.rglob('*') if path.is_file()}
    prep.require(actual_files == expected_files - {'00_MANIFESTS/accession-completion.json'}, 'Final file census differs')
    prep.require({str(path.relative_to(TARGET)) for path in TARGET.rglob('*') if path.is_dir()} == set(DIRS),
                 'Unapproved final directory structure')
    receipt = dict(unit='aeb', collection='591', target=str(TARGET), actor='Vladimir', user_authorization=authorization,
                   started_at=started, completed_at=now(), validation_result='ok', accession_performed=True,
                   **plan['summary'], derivatives_generated=len(derivatives),
                   exact_copy_source_paths=sum(row['exact_copy_proven'] == 'true' for row in sources),
                   metadata_authority='local archive; museum-digital is a projection',
                   sources_modified=False, deletion_authorized=False, publication_performed=False,
                   checks=['exact protected mount', 'saved decisions and complete MD member order',
                           'unchanged source size/mtime; stored hashes matched copied payloads',
                           '67 final PDF page counts; 42 decoded original JPEGs', '109 decoded accession previews',
                           'CSV/XLSX exact literal-cell equality', 'SQLite/CSV row equality and integrity',
                           'source-register scope and false deletion flags', 'final payload stamps and exact file/directory census'])
    write_new('00_MANIFESTS/accession-completion.json', prep.json_bytes(receipt))
    prep.atomic(ROOT / 'accession-completion.json', prep.json_bytes(receipt))
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true')
    group.add_argument('--execute', action='store_true')
    parser.add_argument('--authorization', default='')
    args = parser.parse_args()
    if args.execute:
        execute(args.authorization)
    else:
        plan = preflight()
        print(json.dumps(dict(result='ok', target=str(TARGET), **plan['summary'],
                              accession_performed=False, execution_requires_explicit_authorization=True,
                              deletion_authorized=False), indent=2))


if __name__ == '__main__':
    main()
