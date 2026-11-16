#!/usr/bin/env python3
"""Embed approved local metadata in the existing Costa Master, without source writes."""
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from copy import copy
from pathlib import Path

from PIL import Image
import openpyxl
import costa_accession_20261014 as archive

WORK, UNIT, TFA = archive.WORK, archive.UNIT, archive.TFA
INV, MASTER = archive.INV, archive.MASTER
AUTHORITY = 'Vladimir: ok lets fix it add meta to masters, 2026-10-14. Archive copies only; source files, image pixels and Master selection unchanged.'
RECEIPT = UNIT / '00_MANIFESTS/costa-master-metadata-20261014.json'
TECH_TAGS = ['FileType', 'ImageWidth', 'ImageHeight', 'BitsPerSample', 'Compression', 'PhotometricInterpretation', 'SamplesPerPixel', 'PlanarConfiguration', 'XResolution', 'YResolution', 'ResolutionUnit', 'Orientation', 'ProfileDescription', 'ColorSpace']

def pixel_digest(p):
    with Image.open(p) as im:
        im.load()
        h = hashlib.sha256(f'{im.width}x{im.height}:{im.mode}:'.encode())
        h.update(im.tobytes())
        return h.hexdigest()

def exif(p, tags):
    result = subprocess.run(['exiftool', '-j', '-G1', '-s', '-n', *['-' + t for t in tags], str(p)], check=True, capture_output=True, text=True)
    value = json.loads(result.stdout)[0]
    value.pop('SourceFile', None)
    return value

def csv_from_work(name):
    return archive.read_csv(name, WORK)

def main():
    assert not RECEIPT.exists(), 'Completed metadata embedding must not be replayed.'
    started = archive.now()
    completion_path = UNIT / '00_MANIFESTS/costa-accession-20261014.json'
    completion = json.loads(completion_path.read_text())
    final_by_path = {r['relative_path']: r for r in completion['final_media']}
    source_rows = csv_from_work('matthias-costa-drain-list-20261014.csv')
    archive.source_guard(source_rows)
    masters = archive.read_csv('00_MANIFESTS/master-representations.csv')
    master = next(r for r in masters if r['master_representation_id'] == MASTER)
    paths = json.loads(master['member_paths_json'])
    sources = json.loads(master['original_source_paths_json'])
    assert len(paths) == len(sources) == 22 and len([r for r in masters if r['archival_object_id'] == INV]) == 1
    catalog = archive.read_csv('CATALOG.csv')
    representations = archive.read_csv('00_MANIFESTS/representations.csv')
    source_dispositions = archive.read_csv('00_MANIFESTS/source-disposition.csv')
    drains = archive.read_csv('00_MANIFESTS/drain-list.csv')
    bounded_drains = archive.read_csv('00_MANIFESTS/costa-drain-list-20261014.csv')
    checksums = archive.read_csv('00_MANIFESTS/payload-checksums.csv')
    objects = archive.read_csv('db-restore/objects.csv')
    native_objects = archive.read_csv('db-restore/RESTORE.csv')
    assert (len(catalog), len(masters), len(representations), len(source_dispositions), len(drains), len(bounded_drains)) == (163, 55, 163, 329, 274, 85)
    by_path = {r['final_relative_path']: r for r in catalog}
    unchanged_media = {r['final_relative_path']: archive.signature(UNIT / r['final_relative_path']) for r in catalog if r['final_relative_path'] not in paths}
    workbook = openpyxl.load_workbook(UNIT / 'CATALOG.xlsx')
    workbook_before = {s.title: [[(c.value, copy(c._style)) for c in row] for row in s] for s in workbook}
    database = UNIT / 'db-restore/RESTORE.sqlite'
    with archive.db_ro(database) as c:
        schema_before = c.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name").fetchall()
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    completed = []
    for index, (relative, source) in enumerate(zip(paths, sources), 1):
        archive.source_guard(source_rows)
        current = UNIT / relative
        assert current.stat().st_size == int(final_by_path[relative]['size_bytes'])
        stage = WORK / ('costa-meta--' + current.name)
        assert not stage.exists(), 'Unexpected metadata candidate: ' + str(stage)
        current_signature = archive.signature(current)
        old_technical = exif(current, TECH_TAGS)
        old_meta = exif(current, ['XMP-dc:Title', 'XMP-dc:Description', 'XMP-dc:Identifier', 'IPTC:Caption-Abstract'])
        assert 'XMP-dc:Identifier' not in old_meta and 'XMP-dc:Description' not in old_meta, 'Unexpected existing descriptive metadata.'
        original_caption = old_meta.get('IPTC:Caption-Abstract')
        before_pixels = pixel_digest(current)
        copied_hash = hashlib.sha256()
        with current.open('rb') as incoming, stage.open('xb') as outgoing:
            for block in iter(lambda: incoming.read(8 * 1024 * 1024), b''):
                copied_hash.update(block)
                outgoing.write(block)
        assert copied_hash.hexdigest() == final_by_path[relative]['sha256'], 'Master bytes changed before metadata work.'
        preview = UNIT / next(r['relative_path'] for r in representations if r['archival_object_id'] == INV and r['role'] == 'generated_access_derivative' and r['derivative_source_member'] == relative)
        known = exif(preview, ['XMP-dc:Title', 'XMP-dc:Description'])
        title = known['XMP-dc:Title']
        description = known['XMP-dc:Description']
        expected = {'XMP-dc:Title': title, 'XMP-dc:Description': description, 'XMP-dc:Identifier': INV, 'XMP-dc:Source': source + '; source SHA-256 ' + by_path[relative]['source_sha256'], 'XMP-dc:Relation': MASTER, 'XMP-dc:Type': 'Dossier', 'XMP-dc:Subject': ['Matthias Costa', 'Werk für Fernsehelektronik (WF)']}
        args = ['exiftool', '-overwrite_original', *['-' + k + '=' + v for k, v in expected.items() if k != 'XMP-dc:Subject'], '-XMP-dc:Subject=Matthias Costa', '-XMP-dc:Subject+=Werk für Fernsehelektronik (WF)']
        if current.suffix.lower() == '.tif':
            expected.update({'IFD0:DocumentName': current.name, 'IFD0:ImageDescription': description, 'IPTC:Caption-Abstract': description})
            args += ['-IFD0:DocumentName=' + current.name, '-IFD0:ImageDescription=' + description, '-IPTC:CodedCharacterSet=UTF8', '-IPTC:Caption-Abstract=' + description]
        # Existing album JPEG IPTC caption, dates, software and all unrelated fields are preserved.
        result = subprocess.run(args + [str(stage)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        actual = exif(stage, list(expected) + ['IPTC:Caption-Abstract'])
        for key, value in expected.items():
            assert actual.get(key) == value, (current.name, key, actual.get(key), value)
        if original_caption is not None:
            assert actual['IPTC:Caption-Abstract'] == original_caption, 'Original caption changed.'
        after_technical = exif(stage, TECH_TAGS)
        assert after_technical == old_technical, (current.name, old_technical, after_technical)
        after_pixels = pixel_digest(stage)
        assert after_pixels == before_pixels, 'Image samples changed.'
        # One final byte hash after completed metadata write and readback.
        final_hash = archive.sha(stage)
        final_size = stage.stat().st_size
        assert archive.signature(current) == current_signature and stage.stat().st_dev == current.stat().st_dev
        os.replace(stage, current)
        completed.append({'relative_path': relative, 'original_source_path': source, 'original_source_sha256': by_path[relative]['source_sha256'], 'previous_archive_sha256': final_by_path[relative]['sha256'], 'sha256': final_hash, 'size_bytes': str(final_size), 'decoded_pixel_sha256_before': before_pixels, 'decoded_pixel_sha256_after': after_pixels, 'technical_tags_unchanged': True, 'embedded_tags': expected, 'original_caption_preserved': original_caption is not None})
        print('METADATA_EMBEDDED', index, '/22', current.name, flush=True)
    new_media = {r['relative_path']: r for r in completed}
    source_to_final = dict(zip(sources, paths))
    for r in representations:
        if r['relative_path'] in new_media:
            fresh = new_media[r['relative_path']]
            r.update(sha256=fresh['sha256'], size_bytes=fresh['size_bytes'], provenance='Human-selected dossier Master member; local descriptive metadata embedded on 2026-10-14; decoded pixels and original technical tags unchanged. Source byte hash retained separately.')
        elif r['archival_object_id'] == INV and r['role'] == 'generated_access_derivative':
            r['provenance'] += '; Master descriptive metadata subsequently embedded without changing its image content; preview bytes unchanged'
    master['member_sha256_json'] = json.dumps([new_media[p]['sha256'] for p in paths])
    master['representation_note'] += ' Descriptive metadata embedded in all 22 Master members on 2026-10-14; image samples unchanged, archive byte hashes differ from source byte hashes.'
    for r in catalog:
        if r['final_relative_path'] in new_media:
            fresh = new_media[r['final_relative_path']]
            r.update(final_sha256=fresh['sha256'], size_bytes=fresh['size_bytes'], notes='Local descriptive metadata embedded; decoded pixels and technical tags unchanged; source SHA-256 retained separately.')
        elif r['record_key'] == INV and r['file_role'] == 'access_derivative':
            r['notes'] = 'Existing 1600 px preview unchanged; its original generation-source hash is retained. Master descriptive metadata changed; image samples unchanged.'
    def correct_dispositions(rows):
        for r in rows:
            if r['inventory'] != INV:
                continue
            target = json.loads(r['final_archive_paths_json'])[0]
            r['relationship'] = 'selected_master_source_pixels_preserved_after_metadata_embedding' if r['disposition'] == 'selected_master' else 'small_jpeg_content_covered_by_metadata_enriched_master' if r['candidate_role'] == 'existing_derivative' else 'exact_usb_duplicate_of_selected_source'
            r['reason'] = 'Preserved in approved Archiv WF dossier. Archive Master descriptive metadata embedded; original image content unchanged. USB source hash remains ' + r['candidate_sha256'] + '; finalized Master hash is ' + new_media[target]['sha256'] + '. Archive/source byte identity is not claimed. Small-JPEG source metadata claims are captured in metadata/matthias-costa-source-metadata.json. Deletion not authorised.'
    for data in [source_dispositions, drains, bounded_drains]:
        correct_dispositions(data)
    with sqlite3.connect(database) as c:
        for r in completed:
            assert c.execute('UPDATE representations SET sha256=?,size_bytes=? WHERE relative_path=?', (r['sha256'], int(r['size_bytes']), r['relative_path'])).rowcount == 1
        for r in bounded_drains:
            assert c.execute('UPDATE source_disposition SET relationship=? WHERE candidate_id=?', (r['relationship'], r['candidate_id'])).rowcount == 1
    for relative, rows in [('CATALOG.csv', catalog), ('00_MANIFESTS/master-representations.csv', masters), ('00_MANIFESTS/representations.csv', representations), ('00_MANIFESTS/source-disposition.csv', source_dispositions), ('00_MANIFESTS/drain-list.csv', drains), ('00_MANIFESTS/costa-drain-list-20261014.csv', bounded_drains)]:
        archive.write_csv(UNIT / relative, rows)
    with archive.db_ro(database) as c:
        (UNIT / 'db-restore/RESTORE.sql').write_text('\n'.join(c.iterdump()) + '\n')
    # The existing workbook fallback was established earlier in this session: Artifact Tool WASM SIMD is unavailable.
    subprocess.run(['node', '/home/vladimir/.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.909.12148/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs', '--operation-kind', 'edit', '--expected-output-count', '1', '--output-format', 'xlsx'], check=True)
    sheet = workbook['Catalog']
    columns = {c.value: c.column for c in sheet[1]}
    edits = set()
    for index, r in enumerate(catalog, 2):
        if r['record_key'] != INV:
            continue
        for field in ['final_sha256', 'size_bytes', 'source_sha256', 'notes']:
            cell = sheet.cell(index, columns[field])
            value = int(r[field]) if field == 'size_bytes' else r[field]
            if cell.value != value:
                edits.add(('Catalog', index, cell.column))
                cell.value = value
    workbook.save(UNIT / 'CATALOG.xlsx')
    reopened = openpyxl.load_workbook(UNIT / 'CATALOG.xlsx')
    for sheet_name, old_rows in workbook_before.items():
        for i, old in enumerate(old_rows, 1):
            for j, (value, style) in enumerate(old, 1):
                cell = reopened[sheet_name].cell(i, j)
                assert cell._style == style
                if (sheet_name, i, j) not in edits:
                    assert cell.value == value
    for name, rows in [('Catalog', catalog), ('Objects', objects)]:
        s = reopened[name]
        headers = [c.value for c in s[1]]
        for i, r in enumerate(rows, 2):
            for j, field in enumerate(headers, 1):
                val = s.cell(i, j).value
                assert str('' if val is None else val) == r[field], (name, i, field)
    tfa_rows = archive.read_csv('db-restore/manifests/source-disposition.csv', TFA)
    tfa_changed = []
    for r in tfa_rows:
        if 'MC_Matthias-Costa-Archiv/' not in r['source_path']:
            continue
        target = 'masters/' + Path(r['source_path']).name
        r.update(final_sha256=new_media[target]['sha256'], exact_copy_proven='false', preservation_basis='verified_master_metadata_embedding_pixels_unchanged', notes='Costa belongs to Archiv WF dossier ' + INV + '. Archive Master metadata embedded on 2026-10-14; original decoded image content verified unchanged. Source SHA-256 and final Master SHA-256 differ and remain separately recorded. Excluded from TFA accession/drain; Archiv WF records this USB path. Deletion not authorised.')
        tfa_changed.append(r)
    assert len(tfa_changed) == 21
    with sqlite3.connect(TFA / 'db-restore/RESTORE.sqlite') as c:
        for r in tfa_changed:
            assert c.execute('UPDATE source_disposition SET final_sha256=?,exact_copy_proven=?,preservation_basis=?,notes=? WHERE source_path=?', (r['final_sha256'], r['exact_copy_proven'], r['preservation_basis'], r['notes'], r['source_path'])).rowcount == 1
    archive.write_csv(TFA / 'db-restore/manifests/source-disposition.csv', tfa_rows)
    with archive.db_ro(TFA / 'db-restore/RESTORE.sqlite') as c:
        (TFA / 'db-restore/RESTORE.sql').write_text('\n'.join(c.iterdump()) + '\n')
    tfa_receipt = TFA / 'db-restore/manifests/costa-archiv-wf-routing-20261014.json'
    info = json.loads(tfa_receipt.read_text())
    info['after'] = tfa_changed
    info['metadata_embedding'] = {'authority': AUTHORITY, 'receipt': str(RECEIPT), 'archive_source_byte_identity': False, 'image_pixels_unchanged': True}
    archive.write_json(tfa_receipt, info)
    recon_path = UNIT / 'metadata/matthias-costa-reconciliation.json'
    recon = json.loads(recon_path.read_text())
    recon.update(original_scans_unchanged=True, original_scans_note='The protected USB originals remain unchanged. Archive Master bytes now include descriptive metadata.', archive_master_metadata_embedded=True, archive_master_pixels_unchanged=True, archive_master_bytes_match_usb_sources=False, master_metadata_receipt=str(RECEIPT))
    archive.write_json(recon_path, recon)
    readme_path = UNIT / '00_README.md'
    readme = readme_path.read_text()
    readme = readme.replace('21 unchanged original TIFF scans in filename order, followed by the unchanged high-resolution 1985 album-page JPEG.', '21 original TIFF scans in filename order, followed by the high-resolution 1985 album-page JPEG. Descriptive metadata is now embedded in these archive copies; the selected image content and technical tags are unchanged.')
    readme += '\n\n### Costa Master metadata completion — 14 October 2026\n\nAll 22 archive Master members now carry XMP title, description, dossier identifier, original source path/hash, representation relation, type and subject. TIFFs also carry ImageDescription, DocumentName and UTF-8 IPTC caption. The original album JPEG IPTC caption was preserved exactly. Unknown or unverified rights/creator claims were not promoted to facts. Metadata readback, unchanged decoded pixel hashes and unchanged technical tags passed for every Master member. Updated final hashes are in the existing manifests and SQLite; USB source hashes remain separate.\n\nThe 85-row drain addition now describes preservation after metadata embedding and does not claim byte identity between source files and archive Masters. Nothing was deleted. Existing preview files remain unchanged at maximum 1600 pixels; no full-resolution derivative was generated in this metadata-only task. Receipt: `00_MANIFESTS/costa-master-metadata-20261014.json`.\n'
    readme_path.write_text(readme)
    completion['master_metadata_embedding'] = {'authority': AUTHORITY, 'completed_at': archive.now(), 'members': 22, 'decoded_pixels_unchanged': True, 'technical_tags_unchanged': True, 'original_album_caption_preserved': True, 'source_byte_identity': False, 'receipt': str(RECEIPT), 'derivatives_regenerated': False}
    for r in completion['final_media']:
        if r['relative_path'] in new_media:
            fresh = new_media[r['relative_path']]
            r.update(sha256=fresh['sha256'], size_bytes=fresh['size_bytes'])
    archive.write_json(completion_path, completion)
    archive.write_json(WORK / 'costa-accession-completion-20261014.json', completion)
    changed = {'CATALOG.csv', 'CATALOG.xlsx', '00_MANIFESTS/master-representations.csv', '00_MANIFESTS/representations.csv', '00_MANIFESTS/source-disposition.csv', '00_MANIFESTS/drain-list.csv', '00_MANIFESTS/costa-drain-list-20261014.csv', 'db-restore/RESTORE.sql', 'db-restore/RESTORE.sqlite', 'metadata/matthias-costa-reconciliation.json', '00_README.md'}
    for r in checksums:
        if r['relative_path'] in new_media:
            fresh = new_media[r['relative_path']]
            r.update(sha256=fresh['sha256'], size_bytes=fresh['size_bytes'])
        elif r['relative_path'] in changed:
            p = UNIT / r['relative_path']
            r.update(sha256=archive.sha(p), size_bytes=str(p.stat().st_size))
    archive.write_csv(UNIT / '00_MANIFESTS/payload-checksums.csv', checksums)
    for r in source_rows:
        target = str(Path(r['final_archive_path']).relative_to(UNIT))
        r.update(final_master_sha256=new_media[target]['sha256'], exact_archive_copy_proven='false', preservation_basis='verified_image_content_preserved_after_master_metadata_embedding')
    archive.write_csv(WORK / 'matthias-costa-drain-list-20261014.csv', source_rows, list(source_rows[0]))
    archive.write_csv(WORK / 'matthias-costa-drain-proposal-20261014.csv', source_rows, list(source_rows[0]))
    for relative, sig in unchanged_media.items():
        assert archive.signature(UNIT / relative) == sig, relative
    archive.source_guard(source_rows)
    with archive.db_ro(database) as c:
        assert schema_before == c.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY name").fetchall()
        assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT count(*) FROM master_representations WHERE archival_object_id=?', (INV,)).fetchone()[0] == 1
    result = {'result': 'ok', 'started_at': started, 'completed_at': archive.now(), 'authority': AUTHORITY, 'Master_representation': MASTER, 'master_members_updated': 22, 'members': completed, 'source_files_unchanged': 85, 'unchanged_archive_media': len(unchanged_media), 'derivative_files_changed': 0, 'full_resolution_derivatives_generated': 0, 'xlsx_csv_agree': True, 'xlsx_other_values_and_all_styles_unchanged': True, 'SQLite_schema_unchanged': True, 'SQLite_quick_check': 'ok', 'foreign_key_violations': 0, 'drain_rows': 85, 'deletion_authorized': False, 'source_mount': archive.mount_guard()}
    archive.write_json(RECEIPT, result, exclusive=True)
    archive.write_json(WORK / 'costa-master-metadata-completion-20261014.json', result, exclusive=True)
    print(json.dumps(result | {'members': '<22 metadata readbacks and before/after pixel hashes recorded>'}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
