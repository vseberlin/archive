#!/usr/bin/env python3
"""Bounded, explicitly authorized Collection 551 accession; never deletes sources.

--check is read-only. --execute refuses an existing output unit. All source
copies are compared byte-for-byte, and final files are hashed once after writing.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from collections import Counter

from PIL import Image, ImageOps
import collection_551_workbench as wb

ROOT = wb.ROOT
TARGET = Path('/mnt/volume/iss-archive/units/FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF')
DIRS = ('00_MANIFESTS', 'masters', 'export', 'metadata', 'documents', 'db-restore')
AUTHORIZATION = 'ok lets make and accession and drain-list'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'


def read(name):
    return wb.read_csv(ROOT / name)


def json_write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def csv_write(path, rows):
    path.write_bytes(wb.csv_payload(list(rows[0]), rows))


def destination(relative):
    p = Path(relative)
    assert len(p.parts) == 2 and p.parts[0] in DIRS and '..' not in p.parts, relative
    return TARGET / p


def stamp(path):
    s = path.stat()
    return (s.st_size, s.st_mtime_ns)


def identical(a, b):
    with a.open('rb') as x, b.open('rb') as y:
        while True:
            first, second = x.read(1024 * 1024), y.read(1024 * 1024)
            if first != second:
                return False
            if not first:
                return True


def preflight():
    guard = wb.source_guard()
    w = wb.Workbench('Vladimir')
    assert w.check()['resolved_objects'] == 79 and not w.ordered_objects()
    assert not TARGET.exists(), 'Refusing to overwrite an existing accession unit.'
    prep = json.loads((ROOT / 'accession-preparation.json').read_text())
    assert prep['preparation_complete']
    masters = read('accession-master-files.csv')
    docs = read('accession-document-review.csv')
    assert all(r['decided_at'] and r['decision'] in {'included', 'redundant', 'excluded'} for r in docs)
    included = [r for r in docs if r['decision'] == 'included']
    assert len(masters) == 108 and len(included) == 21
    assert wb.read_csv(wb.DISPOSITIONS_CSV) == w.dispositions_for(w.decisions())
    assert wb.read_csv(wb.DRAIN_CSV) == w.drain_for(w.decisions())
    catalogue = read('accession-catalogue.csv')
    by_path = {r['source_path']: r for r in masters + included}
    assert {r['source_path'] for r in catalogue} == set(by_path)
    expected = {}
    for r in masters:
        expected[r['source_path']] = (int(r['source_size_bytes']), int(r['source_mtime_ns']))
    for r in included:
        expected[r['source_path']] = (int(r['size_bytes']), int(r['mtime_ns']))
    for source, check in expected.items():
        p = Path(source)
        assert wb.inside(p, ROOT) or wb.inside(p, wb.SOURCE_ROOT), source
        assert stamp(p) == check, f'Source changed: {source}'
    generated = {'documents/CONTENTS.md', '00_MANIFESTS/source-disposition.csv', 'CATALOG.csv'}
    evidence = [r for r in read('accession-evidence-plan.csv') if r['proposed_relative_path'] not in generated]
    for r in evidence:
        p = Path(r['source_path'])
        assert wb.inside(p, ROOT)
        expected[str(p)] = stamp(p)
    derivatives = read('accession-derivatives-plan.csv')
    paths = [r['proposed_relative_path'] for r in catalogue + evidence + derivatives]
    assert len(paths) == len(set(paths)) == 271
    for p in paths:
        destination(p)
    assert len(derivatives) == 108 and shutil.which('pdftoppm') and shutil.which('pdfinfo')
    assert shutil.disk_usage(TARGET.parent).free > 2_000_000_000
    return w, prep, masters, docs, included, catalogue, evidence, derivatives, expected, guard


def xlsx_write(path, rows):
    """Dependency-free literal-text OOXML view of the archive's CSV catalogue."""
    fields = list(rows[0])
    matrix = [fields] + [[str(row[k]) for k in fields] for row in rows]
    def col(n):
        result = ''
        while n:
            n, d = divmod(n - 1, 26)
            result = chr(65 + d) + result
        return result
    body = []
    for rn, values in enumerate(matrix, 1):
        cells = []
        for cn, value in enumerate(values, 1):
            assert len(value) <= 32767 and not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value)
            value = escape(value).replace('\r', '&#13;')
            cells.append(f'<c r="{col(cn)}{rn}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>')
        body.append(f'<row r="{rn}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{NS}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols><col min="1" max="{len(fields)}" width="30" customWidth="1"/></cols><sheetData>{"".join(body)}</sheetData><autoFilter ref="A1:{col(len(fields))}{len(matrix)}"/></worksheet>'
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="CATALOG" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        tree = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        actual = [[c.find(f'{{{NS}}}is/{{{NS}}}t').text or '' for c in row] for row in tree.find(f'{{{NS}}}sheetData')]
        assert matrix == actual, 'XLSX differs from CSV catalogue'


def execute(data):
    w, prep, masters, docs, included, planned, evidence, derivatives, expected, guard = data
    started = wb.now()
    authority = {'unit': 'collection-551', 'actor': 'Vladimir', 'authorized_at': started,
                 'user_instruction': AUTHORIZATION, 'scope': 'Selected Masters, documentation, accession derivatives, catalogue and drain list',
                 'deletion_authorized': False, 'source_mutation_authorized': False}
    json_write(ROOT / 'accession-authorization.json', authority)
    TARGET.mkdir()
    for name in DIRS:
        (TARGET / name).mkdir()
    json_write(TARGET / '00_MANIFESTS/accession-authorization.json', authority)
    catalog = []
    final_checks = []
    def register_file(relative, row):
        p = destination(relative)
        with p.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        stat = p.stat()
        final_checks.append({'relative_path': relative, 'size_bytes': str(stat.st_size),
                             'mtime_ns': str(stat.st_mtime_ns), 'sha256': digest})
        result = dict(row)
        result.pop('proposed_relative_path', None)
        result = {'relative_path': relative, **result, 'size_bytes': str(stat.st_size)}
        result['final_sha256'] = digest
        catalog.append(result)
        return digest
    for r in evidence:
        planned.append({k: '' for k in planned[0]})
        planned[-1].update(proposed_relative_path=r['proposed_relative_path'], file_role='evidence',
                           title=Path(r['source_path']).name, description=r['role'], source_path=r['source_path'])
    for i, row in enumerate(planned, 1):
        source = Path(row['source_path']); relative = row['proposed_relative_path']; target = destination(relative)
        assert stamp(source) == expected[str(source)]
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush(); os.fsync(dst.fileno())
        assert identical(source, target), f'Copy differs: {source}'
        assert stamp(source) == expected[str(source)]
        digest = register_file(relative, row)
        assert not row['source_sha256'] or digest == row['source_sha256'], f'Stored hash mismatch: {source}'
        catalog[-1]['source_sha256'] = digest
        if i % 25 == 0:
            print(f'Copied and verified {i}/{len(planned)} selected/evidence files', flush=True)
    by_relative = {r['relative_path']: r for r in catalog}
    for i, r in enumerate(derivatives, 1):
        master = destination(r['master_relative_path']); target = destination(r['proposed_relative_path'])
        if master.suffix.lower() == '.pdf':
            with target.open('xb') as out:
                result = subprocess.run(['pdftoppm', '-f', '1', '-l', '1', '-singlefile', '-scale-to', '1600', '-jpeg', '-jpegopt', 'quality=85', str(master)], stdout=out, stderr=subprocess.PIPE, timeout=120)
            assert result.returncode == 0, result.stderr.decode(errors='replace')
        else:
            with Image.open(master) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                image.convert('RGB').save(target, format='JPEG', quality=85)
        with Image.open(target) as image:
            image.load()
            assert image.format == 'JPEG' and 0 < max(image.size) <= 1600
        m = by_relative[r['master_relative_path']]
        row = {k: '' for k in planned[0]}
        row.update(file_role='access_preview', md_object_id=r['md_object_id'], inventory=r['inventory'],
                   sequence=m['sequence'], title=m['title'], description=r['operation'],
                   source_path=str(master), source_sha256=m['final_sha256'], metadata_json=json.dumps({'master_relative_path': r['master_relative_path']}))
        digest = register_file(r['proposed_relative_path'], row)
        r.update(generation_time=wb.now(), generated='true', final_sha256=digest)
        if i % 20 == 0 or i == len(derivatives):
            print(f'Generated and verified {i}/{len(derivatives)} previews', flush=True)
    # From here all catalogued bytes are final; only self-describing controls remain.
    by_relative = {r['relative_path']: r for r in catalog}
    copied_by_source = {r['source_path']: r for r in catalog if r['file_role'] != 'access_preview'}
    for r in masters:
        final = by_relative[r.pop('proposed_relative_path')]
        r.update(relative_path=final['relative_path'], final_sha256=final['final_sha256'], source_sha256=final['source_sha256'],
                 source_hash_basis='Final copy hash; source and final copy compared byte-for-byte; original source unchanged')
    register = read('accession-source-disposition.csv')
    doc_by_path = {r['source_path']: r for r in docs}
    for r in register:
        r['notes'] = r['notes'].replace('final_path is proposed only; no output or coverage is claimed.', '').replace('proposed destination only.', 'actual accession destination recorded.').replace('Original bytes retained at accession;', 'Original selected bytes retained at accession;')
        copy = copied_by_source.get(r['source_path'])
        if copy:
            r.update(final_path=copy['relative_path'], preservation_status='preserved_exact_copy',
                     preservation_basis='Source size/mtime unchanged; byte-for-byte source/output comparison; final output SHA-256 recorded',
                     source_sha256=copy['final_sha256'], final_sha256=copy['final_sha256'], exact_copy_proven='true',
                     drain_state='preserved_source_retained_no_deletion_authority')
        elif r['final_path']:
            final = by_relative[r['final_path']]
            source = Path(r['source_path']); doc = doc_by_path[r['source_path']]
            assert stamp(source) == (int(doc['size_bytes']), int(doc['mtime_ns']))
            assert r['source_sha256'] == final['final_sha256'] and identical(source, destination(r['final_path']))
            assert stamp(source) == (int(doc['size_bytes']), int(doc['mtime_ns']))
            r.update(final_sha256=final['final_sha256'], exact_copy_proven='true', preservation_status='preserved_exact_copy',
                     preservation_basis='Stored source hash equals final output hash; byte-for-byte comparison; source size/mtime unchanged',
                     drain_state='preserved_duplicate_retained_no_deletion_authority')
        else:
            r.update(preservation_status='not_selected_no_exact_copy_claim', exact_copy_proven='false', final_sha256='',
                     drain_state='retain_noise_source_pending_separate_path_specific_review' if r['source_role'] == 'documentation_noise' else 'retain_source_pending_separate_path_specific_review')
        if doc_by_path.get(r['source_path'], {}).get('relationship') == 'mixed':
            r['drain_state'] = 'retain_mixed_project_source'
        r['deletion_authorized'] = 'false'
    # The original, user-reviewed drain scope is preserved as an explicit subset;
    # the complete source-disposition register also includes candidates and captures.
    drain_paths = {r['source_path'] for r in read('drain-list.csv')}
    drain = [r for r in register if r['source_path'] in drain_paths]
    assert len(register) == 553 and len(drain) == 359
    assert not (wb.unrelated_document_paths(docs) & {r['source_path'] for r in register})
    assert all(r['deletion_authorized'] == 'false' for r in register)
    csv_write(TARGET / '00_MANIFESTS/source-disposition.csv', register)
    csv_write(TARGET / '00_MANIFESTS/drain-list.csv', drain)
    csv_write(TARGET / '00_MANIFESTS/master-files.csv', masters)
    csv_write(TARGET / '00_MANIFESTS/derivatives.csv', derivatives)
    csv_write(TARGET / 'CATALOG.csv', catalog)
    xlsx_write(TARGET / 'CATALOG.xlsx', catalog)
    representations = read('accession-master-representations.csv')
    assert len(representations) == len({r['md_object_id'] for r in representations}) == 79
    members = [p for r in representations for p in json.loads(r['ordered_master_paths'])]
    assert len(members) == len(set(members)) == 108
    assert set(members) == {r['relative_path'] for r in masters}
    connection = sqlite3.connect(TARGET / 'db-restore/catalog.sqlite')
    with connection:
        for table, rows, primary in [('files', catalog, 'relative_path'), ('objects', representations, 'md_object_id'), ('source_disposition', register, 'source_path')]:
            fields = list(rows[0])
            columns = ','.join('"' + f + '" TEXT' + (' PRIMARY KEY' if f == primary else '') for f in fields)
            connection.execute(f'CREATE TABLE {table} ({columns})')
            connection.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for f in fields)})', [[str(r[f]) for f in fields] for r in rows])
            assert connection.execute(f'SELECT * FROM {table}').fetchall() == [tuple(str(r[f]) for f in fields) for r in rows]
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    connection.close()
    (TARGET / 'db-restore/README.md').write_text('The rebuildable catalog.sqlite contains three text-only tables. Rebuild files from ../CATALOG.csv (relative_path primary key), objects from ../00_MANIFESTS/accession-master-representations.csv (md_object_id primary key), and source_disposition from ../00_MANIFESTS/source-disposition.csv (source_path primary key). CSV and JSON evidence remain authoritative and readable without SQLite.\n')
    contents = ['# Collection 551 documentation', '', '21 selected documents. The one final FEB inventory/import document is [FEB-Import-konsolidiert.csv](FEB-Import-konsolidiert.csv). Its earlier versions and damaged candidates remain noise records, with original sources retained. Supporting internal-content/OCR evidence and consolidation provenance are in ../metadata/.', '']
    for r in included:
        contents += [f"## {Path(r['proposed_relative_path']).name}", '', r['description'], '',
                     f"Date: {r['document_date'] or 'unknown'} ({r['date_basis']}). Relationship: {r['relationship']}. Condition: {r['condition']}.", '',
                     f"Source: `{r['source_path']}`", '', f"Content basis: {r['content_evidence']}", '']
    (TARGET / 'documents/CONTENTS.md').write_text('\n'.join(contents))
    notes = {'hidden_pending_metadata_rows': prep['hidden_pending_metadata_rows'],
             'hidden_nonidentical_metadata_variants': prep['hidden_nonidentical_metadata_variants'],
             'treatment': 'MD seed retained where no saved override exists; raw unconfirmed rows preserved, not promoted to human acceptance.',
             'consolidated_inventory_review_points': 'See metadata/FEB-Import-Pruefpunkte.csv; anomalies retained explicitly.'}
    json_write(TARGET / 'metadata/accession-metadata-notes.json', notes)
    (TARGET / '00_README.md').write_text(f'''# Collection 551 — Forschungs- und Entwicklungsberichte aus dem WF

Accession authorized by Vladimir: “{AUTHORIZATION}”. Built {started}.

79 objects have exactly one selected Master representation each: 57 single files and 22 ordered sets, together 108 files (106 PDFs, 1102 PDF pages, and 2 TIFFs). `00_MANIFESTS/accession-master-representations.csv` records the selected member order; `00_MANIFESTS/master-files.csv` records source identity and final hashes. The ordered set is one representation, not competing Masters.

`documents/` contains 21 selected supporting documents and their content descriptions in `CONTENTS.md`. The single final FEB inventory/import document is `documents/FEB-Import-konsolidiert.csv`; earlier variants, corrections and source provenance are evidence in `metadata/`, not competing final documents. Saved noise and unrelated decisions are retained in `00_MANIFESTS/accession-document-review.csv`.

`export/` contains 108 JPEG access previews generated during this accession from the selected final Masters. PDF previews show only page 1 and do not replace the full PDFs. Selected source bytes are preserved without metadata rewriting; reconciled object metadata is in `metadata/accession-metadata.jsonl` and the catalogues. Fourteen raw pending metadata rows remain evidence; two non-identical description variants retain the MD seed and explicit unconfirmed status in `metadata/accession-metadata-notes.json`. Historical inventory anomalies remain visible in `metadata/FEB-Import-Pruefpunkte.csv`.

`CATALOG.csv` and `CATALOG.xlsx` contain the same 271 file rows: 108 Master files, 21 documents, 34 provenance/evidence files and 108 previews. The three-table `db-restore/catalog.sqlite` is rebuildable from readable CSV records. Generated catalogues, receipts, descriptions and manifests are control files, outside the payload catalogue to avoid circular self-description. `00_MANIFESTS/final-checksums.csv` covers all finalized files except itself and the completion receipt; payload hashes are reused when this list is assembled.

`00_MANIFESTS/source-disposition.csv` is the complete 553-path source register. `00_MANIFESTS/drain-list.csv` is the 359-path reviewed drain subset, with final coverage, noise decisions and retention reasons. Unrelated documents are excluded from both. Consolidation is not byte-identical preservation of the old noise files. Redundant and damaged files remain at their original paths. All deletion_authorized values are false. No source was changed, moved or deleted, and accession does not authorize later deletion.

`00_MANIFESTS/accession-completion.json` is the completion authority. Older copied preparation fields remain historical evidence and do not override it. No publication, source cleanup or sealing is included.
''')
    # Validate final PDF structure against the recorded technical selection.
    pages = 0
    for r in masters:
        if r['format'] == 'pdf':
            info = subprocess.run(['pdfinfo', str(destination(r['relative_path']))], capture_output=True, text=True, timeout=30, check=True).stdout
            count = int(re.search(r'^Pages:\s+(\d+)', info, re.M).group(1))
            assert count == int(r['page_count'])
            pages += count
    assert pages == 1102
    for source, check in expected.items():
        assert stamp(Path(source)) == check, source
    assert wb.source_guard() == guard
    checks_by_path = {r['relative_path']: r for r in final_checks}
    for p in sorted(TARGET.rglob('*')):
        if p.is_file():
            relative = str(p.relative_to(TARGET))
            if relative not in checks_by_path:
                with p.open('rb') as f:
                    digest = hashlib.file_digest(f, 'sha256').hexdigest()
                final_checks.append({'relative_path': relative, 'size_bytes': str(p.stat().st_size),
                                     'mtime_ns': str(p.stat().st_mtime_ns), 'sha256': digest})
    csv_write(TARGET / '00_MANIFESTS/final-checksums.csv', sorted(final_checks, key=lambda r: r['relative_path']))
    receipt = {'unit': 'collection-551', 'target': str(TARGET), 'started_at': started, 'completed_at': wb.now(),
               'accession_performed': True, 'validation_result': 'ok', 'accession_execution_authorized': True,
               'master_objects': 79, 'master_files': 108, 'pdf_pages': pages, 'documentation_files': 21,
               'evidence_files': 34, 'derivatives_generated': 108, 'catalogue_rows': len(catalog),
               'source_disposition_rows': len(register), 'drain_rows': len(drain),
               'noise_paths': sum(r['source_role'] == 'documentation_noise' for r in register),
               'exact_copy_source_paths': sum(r['exact_copy_proven'] == 'true' for r in register),
               'checksum_files': len(final_checks), 'deletion_authorized': False, 'sources_modified': False,
               'checks': ['protected source mount', 'all human Master/document decisions resolved',
                          'exactly one representation per object and complete ordered members',
                          'all copied source bytes compared to output; unchanged size and mtime',
                          'stored hashes match finalized selected copies', '108 previews fully decoded',
                          '106 final PDFs readable with 1102 recorded pages', 'CSV/XLSX exact cell equality',
                          'SQLite rows equal readable CSV records; integrity_check ok',
                          '553 exact paths; reviewed 359-path drain subset; unrelated paths absent',
                          'all source disposition deletion flags false', 'final file hashes recorded once']}
    json_write(TARGET / '00_MANIFESTS/accession-completion.json', receipt)
    json_write(ROOT / 'accession-completion.json', receipt)
    csv_write(ROOT / 'accession-final-drain-list.csv', drain)
    csv_write(ROOT / 'accession-final-source-disposition.csv', register)
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    data = preflight()
    if args.execute:
        execute(data)
    else:
        print(json.dumps({'result': 'ok', 'target': str(TARGET), 'master_objects': 79, 'master_files': 108,
                          'documents': 21, 'derivatives': 108, 'source_paths': 553, 'deletion_authorized': False}))
