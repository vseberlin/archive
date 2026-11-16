#!/usr/bin/env python3
"""Bounded, explicitly authorised Costa dossier addition; never delete sources."""
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
import openpyxl

WORK = Path('/mnt/volume/iss-archive-work')
UNIT = Path('/mnt/volume/iss-archive/units/archiv-wf')
TFA = Path('/mnt/volume/iss-archive/units/tfa')
INV = 'MC_Matthias-Costa-Archiv'
MASTER = 'master:' + INV
TITLE = 'Matthias Costa: persönliche WF-Unterlagen und Fotoalbumseite'
AUTHORITY = 'Vladimir approved Archiv WF accession, one dossier and the 21 original TIFFs plus the high-resolution album-page JPEG on 2026-10-14; compile USB drain list, no deletion.'
ROTATIONS = {'MC-0003': 90, 'MC-0004': 90, 'MC-0018': 90, 'MC-0019': 270, 'MC-0020': 90}
RECEIPT = UNIT / '00_MANIFESTS/costa-accession-20261014.json'
FIELDS = {}

def now():
    return datetime.now(timezone.utc).isoformat()

def mount_guard():
    text = subprocess.check_output(['findmnt', '-rn', '-M', '/media/vladimir/Elements', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'], text=True).strip()
    parts = text.split()
    assert parts[:3] == ['/dev/sdf1', '/media/vladimir/Elements', 'ntfs3'] and 'ro' in parts[3].split(','), text
    return text

def signature(p):
    s = p.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]

def sha(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()

def read_csv(relative, root=UNIT):
    p = root / relative
    with p.open(newline='', encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        FIELDS[str(p)] = r.fieldnames
        return list(r)

def write_csv(p, rows, fields=None):
    fields = fields or FIELDS[str(p)]
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=fields, quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
    p.write_text(out.getvalue(), encoding='utf-8')

def write_json(p, data, exclusive=False):
    with p.open('x' if exclusive else 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')

def row(relative, **values):
    return dict.fromkeys(FIELDS[str(UNIT / relative)], '') | values

def db_ro(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)

def source_guard(rows):
    mount_guard()
    for r in rows:
        p = Path(r['source_path'])
        assert str(p.resolve()).startswith('/media/vladimir/Elements/restore/archive/')
        assert p.stat().st_size == int(r['size_bytes']) and p.stat().st_mtime_ns == int(r['mtime_ns']), str(p)

def main():
    assert not RECEIPT.exists(), 'Completed accession must not be replayed.'
    started = now()
    review = json.loads((WORK / 'matthias-costa-review-20261014.json').read_text())
    drain_proposal = read_csv('matthias-costa-drain-proposal-20261014.csv', WORK)
    source_guard(drain_proposal)
    assert len(drain_proposal) == 85 and len({r['source_path'] for r in drain_proposal}) == 85
    chosen = sorted([r for r in review['inventory'] if r['relative_path'].endswith('.tif')], key=lambda r: r['relative_path'])
    chosen += [r for r in review['inventory'] if r['relative_path'].startswith('Arbeitsessen')]
    assert len(chosen) == 22
    selected_sources = {r['source_path'] for r in chosen}
    preflight_path = WORK / 'costa-accession-preflight-20261014.json'
    prior_preflight = json.loads(preflight_path.read_text()) if preflight_path.exists() else None
    if prior_preflight:
        assert prior_preflight['authority'] == AUTHORITY and prior_preflight['master_sources'] == sorted(selected_sources)
        started = prior_preflight['started_at']
    source_records = {r['source_path']: r for r in review['inventory']}
    for r in review['inventory']:
        if r['alternate_source_path']:
            source_records[r['alternate_source_path']] = r
    groups = read_csv('matthias-costa-document-groups-20261014.csv', WORK)
    descriptions = {}
    for g in groups:
        for stem in g['scan_names'].split('; '):
            descriptions[stem] = g

    catalog = read_csv('CATALOG.csv')
    objects = read_csv('db-restore/objects.csv')
    native = read_csv('db-restore/RESTORE.csv')
    masters = read_csv('00_MANIFESTS/master-representations.csv')
    representations = read_csv('00_MANIFESTS/representations.csv')
    dispositions = read_csv('00_MANIFESTS/source-disposition.csv')
    drains = read_csv('00_MANIFESTS/drain-list.csv')
    checksums = read_csv('00_MANIFESTS/payload-checksums.csv')
    decisions = read_csv('metadata/master-decisions.csv')
    assert (len(catalog), len(objects), len(native), len(masters), len(representations), len(dispositions), len(drains)) == (119, 54, 54, 54, 119, 244, 189)
    assert not any(r['record_key'] == INV for r in objects)
    new_source_paths = {r['source_path'] for r in drain_proposal}
    assert not new_source_paths.intersection(r['source_path'] for r in dispositions)
    assert not new_source_paths.intersection(r['source_path'] for r in drains)
    assert len({r['archival_object_id'] for r in masters}) == len(masters)
    database = UNIT / 'db-restore/RESTORE.sqlite'
    with db_ro(database) as c:
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute("SELECT count(*) FROM objects WHERE md_object_id='' ").fetchone()[0] == 0
        old_schema = c.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name").fetchall()
    baseline_media = {r['final_relative_path']: signature(UNIT / r['final_relative_path']) for r in catalog}
    assert all((UNIT / r['relative_path']).stat().st_size == int(r['size_bytes']) for r in checksums)
    before_counts = {'objects': 54, 'master_members': 65, 'catalog_files': 119, 'source_disposition_rows': 244, 'drain_rows': 189}
    baseline_tfa = read_csv('db-restore/manifests/source-disposition.csv', TFA)
    tfa_before = [r.copy() for r in baseline_tfa if 'MC_Matthias-Costa-Archiv/' in r['source_path']]
    assert len(tfa_before) == 21 and all(r['preservation_status'] == 'not_assessed' and not r['final_path'] for r in tfa_before)
    with db_ro(TFA / 'db-restore/RESTORE.sqlite') as c:
        c.row_factory = sqlite3.Row
        sql = [dict(r) for r in c.execute("SELECT * FROM source_disposition WHERE source_path LIKE '%MC_Matthias-Costa-Archiv/%'")]
        assert sorted(sql, key=lambda r: r['source_path']) == sorted(tfa_before, key=lambda r: r['source_path'])
    workbook = openpyxl.load_workbook(UNIT / 'CATALOG.xlsx')
    before_workbook = {s.title: [[(c.value, copy(c._style)) for c in r] for r in s] for s in workbook}
    assert workbook.sheetnames == ['Catalog', 'Objects']
    assert shutil.disk_usage(UNIT).free > 3 * 1024**3
    for r in chosen:
        p = UNIT / 'masters' / Path(r['source_path']).name
        if p.exists():
            assert prior_preflight and sha(p) == r['source_sha256'], 'Unexpected existing target: ' + str(p)
        access = UNIT / 'export/access' / (Path(r['source_path']).stem + '__access.jpg')
        assert not access.exists() or prior_preflight, 'Unexpected existing access file.'
    for relative in ['metadata/matthias-costa-source-metadata.json', 'metadata/matthias-costa-document-groups.csv', 'metadata/matthias-costa-reconciliation.json', '00_MANIFESTS/costa-drain-list-20261014.csv']:
        assert not (UNIT / relative).exists()
    if not prior_preflight:
        write_json(preflight_path, {'started_at': started, 'authority': AUTHORITY, 'before_counts': before_counts, 'existing_media_signatures': baseline_media, 'source_files': drain_proposal, 'tfa_before': tfa_before, 'sqlite_schema': old_schema, 'master_sources': sorted(selected_sources), 'deletion_authorized': False}, exclusive=True)

    # Unchanged selected source scans are the Master; no metadata is written to them.
    new_master_paths = []
    new_access_paths = []
    technical = {}
    for position, r in enumerate(chosen, 1):
        source_guard(drain_proposal)
        src = Path(r['source_path'])
        dest = UNIT / 'masters' / src.name
        if not dest.exists():
            with src.open('rb') as incoming, dest.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 8 * 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        assert dest.stat().st_size == r['source_size_bytes']
        relative = str(dest.relative_to(UNIT))
        new_master_paths.append(relative)
        access = UNIT / 'export/access' / (src.stem + '__access.jpg')
        with Image.open(dest) as decoded:
            decoded.load()
            width, height = decoded.size
            image = ImageOps.exif_transpose(decoded).convert('RGB')
            rotation = ROTATIONS.get(src.stem, 0)
            if rotation:
                image = image.rotate(-rotation, expand=True)
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            with access.open('wb' if prior_preflight and access.exists() else 'xb') as f:
                image.save(f, 'JPEG', quality=88, optimize=True)
        g = descriptions[src.stem]
        note = g['proposed_description'] + '. ' + g['observed_dates'] + '. ' + g['note']
        if 'Arbeitsessen' in src.stem:
            original = next(m for m in review['source_metadata'] if m['SourceFile'] == str(src))
            note += '\nCaptured source caption (unverified identifications): ' + original.get('Caption-Abstract', '')
        note = note.replace('\r\n', '\n').replace('\r', '\n')
        subprocess.run(['exiftool', '-overwrite_original', '-XMP-dc:Title=' + g['proposed_description'], '-XMP-dc:Description=' + note, '-XMP-dc:Identifier=' + INV, '-XMP-dc:Source=' + relative, '-IPTC:Caption-Abstract=' + note, str(access)], check=True, capture_output=True)
        embedded = json.loads(subprocess.check_output(['exiftool', '-j', '-XMP-dc:Identifier', '-XMP-dc:Description', str(access)], text=True))[0]
        assert embedded['Identifier'] == INV and embedded['Description'] == note
        with Image.open(access) as f:
            f.load()
            access_dimensions = list(f.size)
        access_relative = str(access.relative_to(UNIT))
        new_access_paths.append(access_relative)
        technical[relative] = {'width': width, 'height': height, 'rotation_cw': rotation, 'access': access_relative, 'access_dimensions': access_dimensions, 'source': str(src), 'description': note}
        print('ACCESSION_MEDIA', position, '/22', src.name, flush=True)

    # Hash each finalized new media file once, after derivative metadata readback.
    finalized = {}
    for relative in new_master_paths + new_access_paths:
        p = UNIT / relative
        finalized[relative] = {'relative_path': relative, 'sha256': sha(p), 'size_bytes': str(p.stat().st_size)}
    for r, relative in zip(chosen, new_master_paths):
        assert finalized[relative]['sha256'] == r['source_sha256'], relative
    source_guard(drain_proposal)
    primary_access = new_access_paths[-1]
    master_note = 'One ordered dossier representation: MC-0001 through MC-0021, then the 1985 album-page JPEG. Document associations within the dossier remain described, without separate object identities. Masters copied unchanged; previews retain complete scan frames and rotate booklet spreads for reading.'
    master_row = row('00_MANIFESTS/master-representations.csv', master_representation_id=MASTER, archival_object_id=INV, md_object_id='', inventory=INV, representation_type='ordered_file_set', member_count='22', member_paths_json=json.dumps(new_master_paths), member_sha256_json=json.dumps([finalized[x]['sha256'] for x in new_master_paths]), selected_candidate_id=INV, original_source_paths_json=json.dumps([r['source_path'] for r in chosen]), representation_note=master_note, decision_note=AUTHORITY, actor='Vladimir', decided_at=started, deletion_authorized='false')
    masters.append(master_row)
    native_row = row('db-restore/RESTORE.csv', archival_object_id=INV, md_object_id='', inventory=INV, title=TITLE, object_type='Dossier', date='1983–1991 (document dates); 1985 (album caption)', master_representation_id=MASTER, master_representation_type='ordered_file_set', master_member_count='22', master_member_paths_json=json.dumps(new_master_paths), access_derivative_path=primary_access, metadata_authority='human reconciliation and captured local source evidence', object_url='', deletion_authorized='false')
    native.append(native_row)
    objects.append(row('db-restore/objects.csv', record_key=INV, inventory=INV, title=TITLE, master_representation_id=MASTER, master_representation_type='ordered_file_set', master_member_count='22', ordered_master_paths=json.dumps(new_master_paths), access_pdf_paths='[]', preview_paths=json.dumps([primary_access]), page_image_paths=json.dumps(new_access_paths), access_note='22 accession-generated JPEG reading previews; no reading PDF. Booklet gaps and internal associations remain recorded in metadata/matthias-costa-document-groups.csv.'))
    for k, v in native_row.items():
        field = 'native_' + ('date_text' if k == 'date' else k)
        if field in objects[-1]:
            objects[-1][field] = '0' if k == 'deletion_authorized' else v
    decisions.append(row('metadata/master-decisions.csv', md_object_id='', inventory=INV, decision_state='selected_master', selected_candidate_id=INV, master_representation_type='ordered_file_set', selected_resource_ids='[]', representation_note=master_note, decision_note=AUTHORITY, actor='Vladimir', decided_at=started, deletion_authorized='false'))
    source_to_master = {r['source_path']: relative for r, relative in zip(chosen, new_master_paths)}
    for position, (r, relative, access_relative) in enumerate(zip(chosen, new_master_paths, new_access_paths), 1):
        meta = technical[relative]
        g = descriptions[Path(r['source_path']).stem]
        for role, path, original, derivative_of, source_member in [('selected_master_member', relative, r['source_path'], '', ''), ('generated_access_derivative', access_relative, relative, MASTER, relative)]:
            h = finalized[path]
            is_master = role == 'selected_master_member'
            representation = row('00_MANIFESTS/representations.csv', representation_id=MASTER + (':member:' if is_master else ':access:') + f'{position:02d}', master_representation_id=MASTER, archival_object_id=INV, md_object_id='', inventory=INV, role=role, member_position=str(position), relative_path=path, original_source_path=original, source_url='', format=Path(path).suffix[1:], sha256=h['sha256'], size_bytes=h['size_bytes'], page_count='1', width_px=str(meta['width'] if is_master else meta['access_dimensions'][0]), height_px=str(meta['height'] if is_master else meta['access_dimensions'][1]), derivative_of=derivative_of, derivative_source_member=source_member, provenance='human-selected dossier member copied unchanged' if is_master else 'generated during authorised accession from the selected Master member; full scan frame, recorded rotation, max 1600 px, JPEG quality 88', deletion_authorized='false')
            representations.append(representation)
            catalog.append(row('CATALOG.csv', record_key=INV, inventory=INV, title=TITLE, file_role='preservation_master' if is_master else 'access_derivative', final_relative_path=path, master_relative_path=relative, filename=Path(path).name, file_type=Path(path).suffix[1:], size_bytes=h['size_bytes'], final_sha256=h['sha256'], description=g['proposed_description'], date_text=g['observed_dates'], creator='', object_type='Dossier', material='', dimensions='', places='', tags='Matthias Costa; WF', md_object_id='', md_url='', rights='', rights_holder='', metadata_authority=native_row['metadata_authority'], original_source_path=original, source_sha256=r['source_sha256'], derivative_of=derivative_of, sequence=str(position), width=representation['width_px'], height=representation['height_px'], presentation_rotation_cw=str(meta['rotation_cw']) if is_master else '0', relationship='ordered_dossier_member' if is_master else 'reading_preview_of_selected_member', access_restriction='', condition='Internal document relationships and booklet completeness remain qualified in the dossier metadata.', notes='Master unchanged; scanner backgrounds retained. Small JPEG metadata claims are captured separately.', metadata_path='metadata/matthias-costa-source-metadata.json', provenance_path='00_MANIFESTS/representations.csv', master_representation_id=MASTER, master_representation_type='ordered_file_set', master_member_count='22', pages='1', source_url='', content_note=g['note'], content_match_status='same physical content checked against existing JPEG export; cropping/rotation differences retained', native_file_role=role))
    new_dispositions = []
    review_rows = {r['relative_path']: r for r in review['inventory']}
    for number, candidate in enumerate(drain_proposal, 1):
        p = Path(candidate['source_path'])
        anchor = source_records[str(p)]
        target_source = anchor['source_path']
        if p.suffix.lower() == '.jpg' and p.name.startswith('MC-'):
            target_source = str(Path(review['scope']) / (p.stem + '.tif'))
        target = source_to_master[target_source]
        selected = str(p) in selected_sources
        is_small = candidate['original_file_role'] == 'existing_small_jpeg_derivative'
        relationship = 'selected_dossier_master_source' if selected else 'existing_small_jpeg_same_content_crop_or_rotation' if is_small else 'verified_byte_identical_source_copy'
        reason = 'Source is preserved as the approved Archiv WF dossier member; final hash equals the cached source hash.' if selected or not is_small else 'Existing small JPEG contains the same document content as the selected TIFF, with crop/rotation/export differences. Embedded title, keywords, creator and rights claims are captured unchanged in metadata/matthias-costa-source-metadata.json.'
        values = row('00_MANIFESTS/source-disposition.csv', candidate_id=INV + f'-candidate-{number:03d}', md_object_id='', inventory=INV, source_path=str(p), source_kind='protected_usb_source', candidate_role='selected_master_source' if selected else 'existing_derivative' if is_small else 'exact_copy', candidate_sha256=candidate['source_sha256'], size_bytes=candidate['size_bytes'], disposition='selected_master' if selected else 'redundant', relationship=relationship, covered_by_selected_master='true', final_archive_paths_json=json.dumps([target]), drain_state='proposed_redundant_after_validated_accession', reason=reason, deletion_authorized='false')
        new_dispositions.append(values)
        candidate.update(final_archive_path=str(UNIT / target), accession_verified='true')
    assert Counter(r['disposition'] for r in new_dispositions) == {'selected_master': 22, 'redundant': 63}
    dispositions.extend(new_dispositions)
    drains.extend(new_dispositions)
    for g in groups:
        g.update(selected_master=MASTER, decision='selected_master', note=g['note'] + ' Included within the human-approved single dossier; this row does not create a separate archival object.')
    write_csv(UNIT / 'metadata/matthias-costa-document-groups.csv', groups, FIELDS[str(WORK / 'matthias-costa-document-groups-20261014.csv')])
    write_json(UNIT / 'metadata/matthias-costa-source-metadata.json', {'authority': AUTHORITY, 'captured_at': review['created_utc'], 'source_metadata': review['source_metadata'], 'copy_pairs': [{'source_path': r['source_path'], 'alternate_source_path': r['alternate_source_path'], 'sha256': r['source_sha256'], 'exact_copy': r['alternate_exact_copy']} for r in review['inventory']], 'note': 'Original source metadata claims are evidence. Generic creator/rights tags were not silently promoted to verified dossier-wide facts. The original high-resolution album-page Master retains its embedded caption unchanged.'}, exclusive=True)
    write_json(UNIT / 'metadata/matthias-costa-reconciliation.json', {'authority': AUTHORITY, 'object': INV, 'one_master_representation': MASTER, 'members': new_master_paths, 'original_scans_unchanged': True, 'source_content_comparison': 'All 21 TIFF/JPEG contents checked: complete scan frames contain the JPEG document content. Narrow TIFFs include scanner backgrounds; MC-0003/0004 and booklet scans have recorded reading rotations. This is visual content coverage, not byte identity between TIFF and JPEG.', 'rotations_cw': ROTATIONS, 'metadata_limits': review['limits'][:2] + ['MC-0013 has no identified second page in this bounded folder.', 'Qualification booklet scans show selected spreads, not proven complete coverage.', 'MC-0021 folder association is not resolved; retained inside the approved dossier.', 'Photo identities/date remain source-caption claims, including an unidentified person.'], 'tfa_previous_records': tfa_before, 'deletion_authorized': False}, exclusive=True)

    # Existing small, rebuildable catalogue schema is unchanged.
    with sqlite3.connect(database) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (INV, '', INV, TITLE, 'Dossier', native_row['date'], MASTER, 'ordered_file_set', primary_access, native_row['metadata_authority'], '', 0))
        c.execute('INSERT INTO master_representations VALUES (?,?,?,?,?,?,?,?,?,?)', (MASTER, INV, 'ordered_file_set', 22, json.dumps(new_master_paths), INV, AUTHORITY, 'Vladimir', started, 0))
        for r in representations[119:]:
            c.execute('INSERT INTO representations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (r['representation_id'], MASTER, INV, r['role'], int(r['member_position']), r['relative_path'], r['original_source_path'], r['format'], r['sha256'], int(r['size_bytes']), r['derivative_of'], 0))
        for r in new_dispositions:
            c.execute('INSERT INTO source_disposition VALUES (?,?,?,?,?,?,?,?,?)', (r['candidate_id'], INV, r['source_path'], r['disposition'], r['relationship'], 1, r['final_archive_paths_json'], r['drain_state'], 0))
    for relative, data in [('CATALOG.csv', catalog), ('db-restore/objects.csv', objects), ('db-restore/RESTORE.csv', native), ('00_MANIFESTS/master-representations.csv', masters), ('00_MANIFESTS/representations.csv', representations), ('00_MANIFESTS/source-disposition.csv', dispositions), ('00_MANIFESTS/drain-list.csv', drains), ('metadata/master-decisions.csv', decisions)]:
        write_csv(UNIT / relative, data)
    write_csv(UNIT / '00_MANIFESTS/costa-drain-list-20261014.csv', new_dispositions, FIELDS[str(UNIT / '00_MANIFESTS/source-disposition.csv')])
    with db_ro(database) as c:
        (UNIT / 'db-restore/RESTORE.sql').write_text('\n'.join(c.iterdump()) + '\n', encoding='utf-8')

    # Artifact Tool cannot run on this machine (WASM SIMD unavailable); preserve the existing workbook using the documented fallback.
    subprocess.run(['node', '/home/vladimir/.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.909.12148/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs', '--operation-kind', 'edit', '--expected-output-count', '1', '--output-format', 'xlsx'], check=True)
    for sheet_name, data, existing_rows in [('Catalog', catalog[119:], 120), ('Objects', objects[54:], 55)]:
        s = workbook[sheet_name]
        headers = [c.value for c in s[1]]
        template = list(s[existing_rows])
        for item in data:
            s.append([item.get(k, '') for k in headers])
            for old, cell in zip(template, s[s.max_row]):
                cell._style = copy(old._style)
                if cell.value is not None and cell.value != '' and cell.column in [9, 26, 27, 28, 40] and sheet_name == 'Catalog':
                    if str(cell.value).isdigit():
                        cell.value = int(cell.value)
                if sheet_name == 'Objects' and cell.column == 6:
                    cell.value = int(cell.value)
            s.row_dimensions[s.max_row].height = 180 if sheet_name == 'Objects' else 96
        table = next(iter(s.tables.values()))
        table.ref = f'A1:{openpyxl.utils.get_column_letter(s.max_column)}{s.max_row}'
        if table.autoFilter:
            table.autoFilter.ref = table.ref
    workbook.save(UNIT / 'CATALOG.xlsx')
    reopened = openpyxl.load_workbook(UNIT / 'CATALOG.xlsx')
    for sheet_name, old_rows in before_workbook.items():
        for i, old in enumerate(old_rows, 1):
            for j, (value, style) in enumerate(old, 1):
                cell = reopened[sheet_name].cell(i, j)
                assert cell.value == value and cell._style == style, (sheet_name, i, j)
    for sheet_name, data in [('Catalog', catalog), ('Objects', objects)]:
        s = reopened[sheet_name]
        headers = [c.value for c in s[1]]
        assert s.max_row == len(data) + 1
        for i, expected in enumerate(data, 2):
            for j, key in enumerate(headers, 1):
                actual = s.cell(i, j).value
                assert str('' if actual is None else actual) == str(expected.get(key, '')), (sheet_name, i, key)

    # The former TFA discovery references now route explicitly to this preserved dossier.
    tfa_after = []
    for r in baseline_tfa:
        if 'MC_Matthias-Costa-Archiv/' not in r['source_path']:
            continue
        selected = next(x for x in chosen if Path(x['source_path']).name == Path(r['source_path']).name)
        relative = source_to_master[selected['source_path']]
        r.update(source_role='general_or_cross_project_reference', final_path=str(UNIT / relative), preservation_status='retained_existing_unit_evidence', preservation_basis='user_confirmed_archiv_wf_dossier_accession', source_sha256=selected['source_sha256'], final_sha256=finalized[relative]['sha256'], exact_copy_proven='true', drain_state='outside_tfa_document_scope', deletion_authorized='false', notes='Vladimir assigned Costa material to Archiv WF on 2026-10-14. Preserved in dossier ' + INV + '; excluded from TFA accession and drain. The exact source path is listed in Archiv WF 00_MANIFESTS/costa-drain-list-20261014.csv; deletion is not authorised.')
        tfa_after.append(r)
    with sqlite3.connect(TFA / 'db-restore/RESTORE.sqlite') as c:
        for r in tfa_after:
            fields = [k for k in r if k != 'source_path']
            sql = 'UPDATE source_disposition SET ' + ','.join('"' + k + '"=?' for k in fields) + ' WHERE source_path=?'
            assert c.execute(sql, [r[k] for k in fields] + [r['source_path']]).rowcount == 1
    write_csv(TFA / 'db-restore/manifests/source-disposition.csv', baseline_tfa)
    with db_ro(TFA / 'db-restore/RESTORE.sqlite') as c:
        (TFA / 'db-restore/RESTORE.sql').write_text('\n'.join(c.iterdump()) + '\n', encoding='utf-8')
    write_json(TFA / 'db-restore/manifests/costa-archiv-wf-routing-20261014.json', {'authority': AUTHORITY, 'before': tfa_before, 'after': tfa_after, 'tfa_master_selections_changed': False, 'deletion_authorized': False}, exclusive=True)

    readme = UNIT / '00_README.md'
    readme.write_text(readme.read_text() + '\n\n## Matthias Costa dossier — 14 October 2026\n\nVladimir approved this local dossier in Archiv WF with exactly one ordered Master representation, `' + MASTER + '`: 21 unchanged original TIFF scans in filename order, followed by the unchanged high-resolution 1985 album-page JPEG. There is no museum-digital object assignment for the dossier; the earlier 54 MD objects remain unchanged. Current unit totals are 55 objects, 87 Master members and 163 registered files.\n\nThe 22 reading JPEGs were generated from the selected Master members during this accession. Booklet-spread rotations are recorded; complete scan frames remain in the Masters. The album-page reading JPEG is the primary dossier preview. Document groups, source metadata claims, dates and qualified internal relationships are preserved in `metadata/matthias-costa-document-groups.csv`, `metadata/matthias-costa-source-metadata.json` and `metadata/matthias-costa-reconciliation.json`. The booklet is not certified complete; MC-0013 has no identified second page in this bounded set, and the presentation-folder association is unspecified.\n\nAll 85 exact source paths in the two inspected Costa folders are recorded: 22 selected source files and 63 redundant copies/old small JPEG derivatives. `00_MANIFESTS/costa-drain-list-20261014.csv` is the bounded addition to the cumulative `drain-list.csv` (274 rows). Unlike the earlier MD accession policy, this explicit user request includes selected USB source copies after their final archive copies have been verified. The final archival Masters are excluded from the drain list. Every deletion flag remains false. Nothing was deleted or published; USB remains read-only. The 21 former TFA discovery references route to this dossier and are excluded from the TFA drain.\n\nCompletion evidence: `00_MANIFESTS/costa-accession-20261014.json`. The earlier 54-object accession receipt remains historical evidence.\n', encoding='utf-8')

    # Reuse all unchanged payload hashes; replace only changed small catalogue/manifest records and append finalized media hashes.
    changed = {'00_README.md', 'CATALOG.csv', 'db-restore/RESTORE.csv', 'db-restore/RESTORE.sql', 'db-restore/RESTORE.sqlite', '00_MANIFESTS/master-representations.csv', '00_MANIFESTS/representations.csv', '00_MANIFESTS/source-disposition.csv', '00_MANIFESTS/drain-list.csv', 'metadata/master-decisions.csv'}
    for item in checksums:
        if item['relative_path'] in changed:
            p = UNIT / item['relative_path']
            item.update(sha256=sha(p), size_bytes=str(p.stat().st_size))
    checksums.extend(finalized.values())
    for relative in ['CATALOG.xlsx', 'db-restore/objects.csv', 'metadata/matthias-costa-source-metadata.json', 'metadata/matthias-costa-document-groups.csv', 'metadata/matthias-costa-reconciliation.json', '00_MANIFESTS/costa-drain-list-20261014.csv']:
        p = UNIT / relative
        checksums.append({'relative_path': relative, 'sha256': sha(p), 'size_bytes': str(p.stat().st_size)})
    assert len({r['relative_path'] for r in checksums}) == len(checksums)
    write_csv(UNIT / '00_MANIFESTS/payload-checksums.csv', checksums)
    for relative, sig in baseline_media.items():
        assert signature(UNIT / relative) == sig, 'Existing payload changed: ' + relative
    source_guard(drain_proposal)
    with db_ro(database) as c:
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert old_schema == c.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name").fetchall()
        assert c.execute('SELECT count(*) FROM objects').fetchone()[0] == 55
        assert c.execute('SELECT count(*) FROM master_representations').fetchone()[0] == 55
        assert c.execute('SELECT count(*) FROM representations').fetchone()[0] == 163
        assert c.execute('SELECT count(*) FROM source_disposition').fetchone()[0] == 329
    with db_ro(TFA / 'db-restore/RESTORE.sqlite') as c:
        c.row_factory = sqlite3.Row
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        sql = [dict(r) for r in c.execute("SELECT * FROM source_disposition WHERE source_path LIKE '%MC_Matthias-Costa-Archiv/%'")]
        assert sorted(sql, key=lambda r: r['source_path']) == sorted(tfa_after, key=lambda r: r['source_path'])
    write_csv(WORK / 'matthias-costa-drain-proposal-20261014.csv', drain_proposal)
    write_csv(WORK / 'matthias-costa-drain-list-20261014.csv', drain_proposal, FIELDS[str(WORK / 'matthias-costa-drain-proposal-20261014.csv')])
    result = {'result': 'ok', 'started_at': started, 'completed_at': now(), 'authority': AUTHORITY, 'unit': str(UNIT), 'object': INV, 'master_representation_id': MASTER, 'master_representations_added': 1, 'master_members_added': 22, 'generated_access_derivatives': 22, 'source_dispositions_added': 85, 'selected_source_files': 22, 'redundant_source_files': 63, 'drain_rows_added': 85, 'drain_source_bytes': sum(int(r['size_bytes']) for r in drain_proposal), 'unit_totals': {'objects': 55, 'master_representations': 55, 'master_members': 87, 'catalog_files': 163, 'source_disposition_rows': 329, 'drain_rows': 274}, 'final_media': list(finalized.values()), 'existing_media_unchanged': 119, 'tfa_references_routed': 21, 'sqlite_schema_changed': False, 'sqlite_quick_check': 'ok', 'sqlite_foreign_key_violations': 0, 'tfa_sqlite_csv_agreement': True, 'xlsx_csv_all_cells_match': True, 'xlsx_existing_values_and_styles_unchanged': True, 'source_mount': mount_guard(), 'source_files_modified': 0, 'source_files_deleted': 0, 'deletion_authorized': False, 'publication_performed': False}
    write_json(RECEIPT, result, exclusive=True)
    write_json(WORK / 'costa-accession-completion-20261014.json', result, exclusive=True)
    print(json.dumps(result | {'final_media': '<22 Masters and 22 generated JPEGs recorded in receipt>'}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
