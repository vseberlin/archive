#!/usr/bin/env python3
"""Read-only Paragon source/index review; writes findings only in this work root."""
import csv
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

WORK = Path('/mnt/volume/iss-archive-work/paragon')
UNIT = Path('/mnt/volume/iss-archive/units/paragon')
SOURCE = Path('/media/vladimir/Elements')
KEYS = ['VLL-1013', 'VLV-1004-1', 'VLV-1018-3', 'VLV-1050-1',
        'VLV-1053-1', 'VLV-1092-2', 'VLV-1127', 'VLV-1130-5', 'VLV-1161']
mount = subprocess.check_output(['findmnt', '-rn', '-T', str(SOURCE), '-o',
                                 'SOURCE,TARGET,FSTYPE,OPTIONS'], text=True).strip()
parts = mount.split()
assert parts[:3] == ['/dev/sdf1', str(SOURCE), 'ntfs3'] and 'ro' in parts[3].split(',')
manifest = list(csv.DictReader((UNIT / 'db-restore/manifests/source-disposition.csv').open(newline='')))
source_rows = {r['source_path']: r for r in manifest}
objects = {r['archival_label']: r for r in csv.DictReader((UNIT / 'metadata/objects.csv').open(newline=''))}
visual_path = Path('/home/vladimir/archive-visual-index/visual-index.sqlite')
visual = sqlite3.connect(visual_path.as_uri() + '?mode=ro', uri=True)
visual.row_factory = sqlite3.Row
index_path = Path('/home/vladimir/Archive-restoration/archive_index.sqlite')
index = sqlite3.connect(index_path.as_uri() + '?mode=ro', uri=True)
index.row_factory = sqlite3.Row
results = []
for key in KEYS:
    indexed = [dict(r) for r in visual.execute('''
        SELECT r.root_path,a.relative_path,a.size_bytes,a.mtime_ns,a.width_px,
               a.height_px,a.status,a.error
        FROM visual_assets a JOIN visual_roots r USING(root_id)
        WHERE a.original_filename LIKE ? ORDER BY a.size_bytes DESC
        ''', (key + '.%',))]
    candidates = []
    for row in indexed:
        if row['root_path'] != str(SOURCE):
            continue
        path = SOURCE / row['relative_path']
        assert path.resolve().is_relative_to(SOURCE / 'restore/archive')
        item = dict(source_path=str(path), exists=path.is_file(), index_status=row['status'])
        if item['exists']:
            before = path.stat()
            item.update(size_bytes=before.st_size, mtime_ns=before.st_mtime_ns,
                        size_matches_index=before.st_size == row['size_bytes'],
                        mtime_matches_index=before.st_mtime_ns == row['mtime_ns'])
            old = source_rows.get(str(path))
            if old:
                item.update(stored_sha256=old['source_sha256'],
                            size_matches_hash_manifest=before.st_size == int(old['source_size_bytes']))
            else:
                item.update(stored_sha256='', size_matches_hash_manifest=None)
            # This pass includes additional large originals absent from the
            # manifest's inventory grouping and binds every result to index stamps.
            try:
                with Image.open(path) as image:
                    image.load()
                    item.update(decoded=True, format=image.format,
                                width=image.width, height=image.height, mode=image.mode)
            except Exception as error:
                item.update(decoded=False, decode_error=str(error))
            after = path.stat()
            assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        candidates.append(item)
    readable = [r for r in candidates if r.get('decoded')]
    results.append(dict(inventory=key, catalogue=objects[key],
        result='surviving_candidates_require_master_review' if readable else 'unresolved_identity_no_exact_image',
        genuine_loss_confirmed=False, indexed_paths_all_roots=len(indexed),
        readable_source_candidates=len(readable), candidates=candidates))

missing_key = 'VLV-1092-2'
missing = dict(
    exact_file_index_matches=[dict(r) for r in index.execute(
        'SELECT path,basename,size FROM files WHERE basename LIKE ?', (missing_key + '.%',))],
    lightroom_matches=[dict(r) for r in index.execute('''
        SELECT basename,extension,original_filename,root_path,folder_path
        FROM lightroom_files WHERE basename=? OR original_filename LIKE ?
        ''', (missing_key, missing_key + '.%'))],
    recorded_asset_references=[dict(r) for r in index.execute(
        'SELECT ref_name,ref_kind,source_path FROM asset_refs WHERE inventory_number=?', (missing_key,))],
    related_catalogue_records=[objects[k] for k in ['VLV-1092', 'VLV-1092-1', 'VLV-1092-2']],
    interpretation='No exact image found. The 1964 title conflicts with the 1962 event description, '
        'which overlaps the preserved VLV-1092 and VLV-1092-1 views. A metadata-only or duplicate '
        'record remains plausible. Neither a distinct missing original nor genuine loss is proved.')
run = dict(visual.execute('SELECT * FROM index_runs ORDER BY completed_at DESC LIMIT 1').fetchone())
visual.close()
index.close()
report = dict(checked_at=datetime.now(timezone.utc).isoformat(), source_mount=mount,
    result='unit_remains_open', reason='Eight records have readable surviving candidates; the ninth has unresolved identity.',
    objects_reviewed=9, objects_with_surviving_candidates=8, genuine_losses_confirmed=0,
    unit_closed=False, source_changed=False, archive_changed=False, master_selections_changed=False,
    deletion_authorized=False, media_generated=0, media_rehashed=0,
    evidence_paths=[str(visual_path), str(index_path), str(UNIT / 'db-restore/manifests/source-disposition.csv'),
                    str(UNIT / 'metadata/objects.csv')],
    visual_index_run={k: run[k] for k in ['completed_at', 'status', 'files_seen', 'images_seen', 'errors']},
    limitations='Stored indexes guide the search; live files were decoded and sizes/mtimes checked. '
        'Candidate filenames are not human Master selections. No renamed-image equivalence is presumed.',
    objects=results, unmatched_record=missing)
assert sum(bool(r['readable_source_candidates']) for r in results) == 8
assert all(c.get('size_matches_index') and c.get('mtime_matches_index')
           for r in results for c in r['candidates'] if c.get('decoded'))
(WORK / 'missing-originals-review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
lines = ['# Paragon missing-originals review — 2026-09-15', '',
    'The nine records cannot be closed as genuine losses. Eight have readable surviving images. '
    'VLV-1092-2 has no exact image and an unresolved identity/date conflict.', '',
    'The exact protected Elements mount was verified. No archive, source, Master selection or deletion flag changed.', '',
    '| Record | Readable source candidates | Largest readable candidate | Finding |',
    '| --- | ---: | --- | --- |']
for row in results:
    readable = [c for c in row['candidates'] if c.get('decoded')]
    largest = max(readable, key=lambda c: c['width'] * c['height'], default=None)
    dimensions = f"{largest['width']} × {largest['height']} {largest['format']}" if largest else 'none found'
    finding = 'Candidate review and Master selection remain' if largest else 'Identity/date conflict; loss unconfirmed'
    lines.append(f"| {row['inventory']} | {len(readable)} | {dimensions} | {finding} |")
lines += ['', '## VLV-1092-2', '', missing['interpretation'], '',
    'The source catalogue derives this label from Bildtexte workbooks/CSVs. The description and dimensions '
    'overlap the already represented 1962 invitation views. Do not invent a 1964 event, retire the record '
    'as a duplicate, or mark it lost without resolving this identity.', '',
    '## Evidence and continuation', '',
    'missing-originals-review.json records every exact candidate path, stored hash where available, '
    'live size/mtime comparison, decode result, index scope and catalogue provenance.', '',
    'Review the eight surviving candidate groups and select one Master per object. Resolve VLV-1092-2 '
    'against the invitation-card metadata and represented views. The unit remains open; no completion '
    'receipt or genuine-loss decision was fabricated.']
(WORK / 'MISSING_ORIGINALS_REVIEW.md').write_text('\n'.join(lines) + '\n')
print(json.dumps({k: report[k] for k in ['result', 'objects_reviewed', 'objects_with_surviving_candidates',
                                        'genuine_losses_confirmed', 'unit_closed']}, indent=2))
for row in results:
    print(row['inventory'], row['readable_source_candidates'])
