#!/usr/bin/env python3
"""Prepare readable evidence only; never copy, select or accession a Master."""
import csv
import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import workbench

ROOT = workbench.ROOT
UNITS = workbench.ARCHIVE_UNITS


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def stamp(path):
    stat = path.stat()
    return {'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def technical(path):
    result = stamp(path)
    try:
        with warnings.catch_warnings(record=True) as caught, Image.open(path) as image:
            result.update(format=image.format, width=image.width, height=image.height,
                          mode=image.mode, inspection='header_only_not_full_decode')
            if image.format == 'TIFF':
                tags = image.tag_v2
                result.update(bits_per_sample=tags.get(258), compression=tags.get(259),
                              photometric=tags.get(262), orientation=tags.get(274, 1))
                offsets, lengths = tags.get(273, ()), tags.get(279, ())
                if offsets and len(offsets) == len(lengths):
                    result['strip_ranges_within_file'] = all(
                        0 <= int(offset) and int(offset) + int(length) <= result['size_bytes']
                        for offset, length in zip(offsets, lengths))
            if caught:
                result['header_warnings'] = [str(w.message) for w in caught]
    except (OSError, ValueError, TypeError) as error:
        result['header_error'] = str(error)
        if path.suffix.lower() in ('.tif', '.tiff'):
            with path.open('rb') as handle:
                header = handle.read(8)
            if len(header) == 8 and header[:4] in (b'II*\x00', b'MM\x00*'):
                offset = struct.unpack('<I' if header[:2] == b'II' else '>I', header[4:])[0]
                result['first_ifd_offset'] = offset
                result['first_ifd_within_file'] = offset + 2 <= result['size_bytes']
            probe = subprocess.run(['exiftool', '-j', '-s', '-Warning', '-Error', str(path)],
                                   capture_output=True, text=True, timeout=30, check=True)
            result['independent_exiftool_check'] = [
                {k: v for k, v in row.items() if k != 'SourceFile'} for row in json.loads(probe.stdout)]
    return result


def normalize(value):
    return ''.join(c.lower() for c in value if c.isalnum())


def main():
    mount = workbench.mount_guard()
    review_check = workbench.check()
    candidates = csv_rows(workbench.CANDIDATES)
    metadata = workbench.load_metadata_proposals()
    metadata_sha256 = hashlib.sha256(workbench.METADATA_PROPOSALS.read_bytes()).hexdigest()
    if workbench.PREPARATION.exists():
        accepted_hash = json.loads(workbench.PREPARATION.read_text())['metadata_acceptance']['sha256']
        if accepted_hash != metadata_sha256:
            raise ValueError('Metadata changed after user acceptance; obtain review of the changed fields before preparing them.')
    units_before = sorted(p.name for p in UNITS.iterdir())
    catalogue_stamps = {}
    masters = []
    by_md = defaultdict(list)
    for catalog in sorted(UNITS.glob('*/CATALOG.csv')):
        catalogue_stamps[str(catalog)] = stamp(catalog)
        for row in csv_rows(catalog):
            if row.get('file_role') not in ('preservation_master', 'master'):
                continue
            item = dict(row, unit=catalog.parent.name)
            masters.append(item)
            if row.get('md_object_id'):
                by_md[row['md_object_id']].append(item)

    # Check the existing authoritative Kurt object/Master links against the
    # readable catalogue. This does not rebuild or modify the database.
    kurt = UNITS / 'kurt-schwarz'
    con = sqlite3.connect(f'file:{kurt / "db-restore/RESTORE.sqlite"}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        objects = {r['record_key']: dict(r) for r in con.execute('SELECT * FROM objects')}
        for master in (r for r in masters if r['unit'] == 'kurt-schwarz'):
            obj = objects[master['record_key']]
            assert obj['master_relative_path'] == master['final_relative_path']
            assert obj['master_sha256'] == master['final_sha256']
        kurt_counts = Counter(r['record_key'] for r in masters if r['unit'] == 'kurt-schwarz')
        assert len(kurt_counts) == 1935 and set(kurt_counts.values()) == {1}
    finally:
        con.close()

    # Preserve previous search leads without turning a perceptual match into
    # a disposition or an assertion that an unmatched image is new.
    paths = {r['source_path'] for r in candidates}
    phash = defaultdict(list)
    for row in csv_rows(workbench.PHASH_REPORT):
        if row['avantmedia_path'] in paths:
            phash[row['avantmedia_path']].append(row)

    existing_dispositions = defaultdict(list)
    for unit in UNITS.iterdir():
        if not unit.is_dir():
            continue
        for relative in ('00_MANIFESTS/source-disposition.csv',
                         'db-restore/manifests/source-disposition.csv'):
            path = unit / relative
            if path.is_file():
                for row in csv_rows(path):
                    if row.get('source_path') in paths:
                        existing_dispositions[row['source_path']].append(dict(row, unit=unit.name))

    prepared = []
    decode_path = ROOT / 'accession-source-decode.json'
    decode_checks = {r['source_path']: r for r in json.loads(decode_path.read_text())['checks']} if decode_path.exists() else {}
    for candidate in candidates:
        source = Path(candidate['source_path']).resolve(strict=True)
        assert source.is_relative_to(workbench.SOURCE_ROOT)
        source_tech = technical(source)
        assert source_tech['size_bytes'] == int(candidate['size_bytes'])
        decoded = decode_checks.get(str(source))
        if decoded and any(decoded[k] != source_tech[k] for k in ('size_bytes', 'mtime_ns')):
            decoded = None
        meta = metadata[candidate['source_relative_path']]
        links = by_md.get(meta['catalogue_object_id'], []) if meta['catalogue_object_id'] else []
        basis = 'accepted_metadata_catalogue_id' if links else ''
        if not links:
            needle = normalize(source.stem)
            links = [r for r in masters if any(
                needle in normalize(r.get(key, ''))
                for key in ('inventory', 'filename', 'original_source_path',
                            'object_scan_signature', 'object_all_candidate_paths',
                            'object_original_title_filenames'))]
            if links:
                basis = 'filename_or_record_signature_lead_requires_visual_confirmation'
        matches = []
        for master in links:
            unit_root = UNITS / master['unit']
            path = (unit_root / master['final_relative_path']).resolve(strict=True)
            assert path.is_relative_to(unit_root.resolve())
            obj = objects.get(master['record_key'], {}) if master['unit'] == 'kurt-schwarz' else {}
            access_relative = obj.get('access_relative_path', '')
            matches.append({
                'unit': master['unit'], 'record_key': master['record_key'],
                'inventory': master['inventory'], 'master_path': str(path),
                'master_relative_path': master['final_relative_path'],
                'access_path': str(unit_root / access_relative) if access_relative else '',
                'original_source_path': master.get('original_source_path', ''),
                'stored_final_sha256': master.get('final_sha256', ''),
                'title': master.get('title', ''), 'date': master.get('date_text', ''),
                'description': master.get('description', ''),
                'creator': master.get('creator', ''),
                'selection_basis': master.get('object_selection_basis', ''),
                'technical': technical(path),
            })
        uncertain_creator = meta['proposed_creator'].startswith('Unbekannt;')
        if len(matches) > 1:
            group = 'ambiguous_archive_links'
            recommendation = 'Resolve the object relationship before choosing a Master.'
        elif matches:
            group = 'existing_catalogue_object' if meta['catalogue_object_id'] else 'existing_local_object_lead'
            recommendation = ('Compare with the existing selected Master. Retain that Master unless '
                              'full-content and usable-detail comparison supports an explicitly approved replacement; '
                              'record the unselected candidate as redundant only after that decision.')
        elif uncertain_creator:
            group = 'collection_placement_unresolved'
            recommendation = 'Leave unresolved until the owning collection is established; retain the accepted qualified caption.'
        else:
            group = 'possible_addition'
            recommendation = ('Proposed destination: existing kurt-schwarz unit. Confirm a distinct object and select '
                              'this TIFF as its one Master before accession; absence of a catalogue link is not proof of novelty.')
        source_issue = bool(source_tech.get('header_error') or source_tech.get('strip_ranges_within_file') is False)
        if source_issue:
            recommendation = ('Exclude this source TIFF from accession pending diagnosis: its image structure '
                              'could not be read or points outside the file. Keep the existing selected archive Master. '
                              'A working JPEG preview does not establish the TIFF is usable.')
        companions = []
        if candidate['proxy_source_path']:
            proxy = Path(candidate['proxy_source_path']).resolve(strict=True)
            assert proxy.is_relative_to(workbench.SOURCE_ROOT)
            companions.append({'source_path': str(proxy), **stamp(proxy),
                'role': 'existing_same_stem_review_jpeg',
                'proposed_disposition': 'redundant only after full content coverage and Master selection are confirmed',
                'deletion_authorized': False})
        prepared.append({
            'source_path': str(source), 'source_relative_path': candidate['source_relative_path'],
            'filename': candidate['filename'], 'source_technical': source_tech,
            'metadata_file': workbench.METADATA_PROPOSALS.name,
            'metadata_sha256': metadata_sha256, 'accepted_metadata': meta,
            'review_state': 'unresolved', 'selected_master_path': '',
            'preparation_group': group, 'archive_link_basis': basis,
            'existing_masters': matches, 'recommendation': recommendation,
            'source_format_issue': source_issue,
            'full_decode_check': decoded,
            'priority_jpeg_master_comparison': any(m['technical'].get('format') == 'JPEG' for m in matches),
            'known_companions': companions,
            'saved_similarity_leads': sorted(phash[str(source)], key=lambda x: int(x['minimum_phash_distance']))[:3],
            'existing_source_dispositions': existing_dispositions[str(source)],
            'accession_authorized': False, 'deletion_authorized': False,
        })

    assert len(prepared) == len({r['source_path'] for r in prepared}) == 305
    assert all(r['selected_master_path'] == '' for r in prepared)
    assert all(stamp(Path(path)) == before for path, before in catalogue_stamps.items())
    assert sorted(p.name for p in UNITS.iterdir()) == units_before
    assert all(stamp(Path(r['source_path'])) == {k: r['source_technical'][k] for k in ('size_bytes', 'mtime_ns')}
               for r in prepared)
    counts = dict(Counter(r['preparation_group'] for r in prepared))
    summary = {
        'prepared_at': datetime.now(timezone.utc).isoformat(), 'mount': mount,
        'scope': 'The 305 TIFF candidates already in the Avantmedia review workbench',
        'metadata_acceptance': {'actor': 'user', 'statement': 'ok meta is good. can we prepare accession?',
                                'file': workbench.METADATA_PROPOSALS.name,
                                'sha256': metadata_sha256, 'rows': 305,
                                'qualifications_preserved': True},
        'candidate_count': 305, 'source_tiff_bytes': sum(int(r['size_bytes']) for r in candidates),
        'known_companion_jpegs': sum(len(r['known_companions']) for r in prepared),
        'groups': counts,
        'priority_jpeg_master_comparisons': [r['filename'] for r in prepared if r['priority_jpeg_master_comparison']],
        'header_errors': [r['filename'] for r in prepared if r['source_technical'].get('header_error')],
        'source_format_issues': [r['filename'] for r in prepared if r['source_format_issue']],
        'full_decode_checks_passed': sum(bool(r['full_decode_check'] and r['full_decode_check']['ok']) for r in prepared),
        'planned_new_directories': [], 'approved_accession_rows': 0,
        'destination_proposal': str(kurt), 'available_work_volume_bytes': shutil.disk_usage(ROOT).free,
        'review_check': review_check, 'kurt_sqlite_catalogue_master_paths_and_hashes_agree': True,
        'catalogue_file_stamps': catalogue_stamps,
        'limits': ['Header checks do not establish complete image decoding or comparative quality.',
                   'No proposed replacement or redundancy decision is applied.',
                   'The 305-candidate scope does not cover every file in Avantmedia.',
                   'Seven qualified creator captions do not establish a collection placement.'],
        'source_modified': False, 'archive_modified': False,
        'accession_authorized': False, 'deletion_authorized': False,
    }
    (ROOT / 'accession-preparation.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in prepared))
    (ROOT / 'accession-preparation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    lines = ['# Avantmedia accession preparation', '',
             'The user accepted the 305 metadata proposals on 14 October 2026. All qualified dates, '
             'conflicting evidence and uncertain authors remain qualified. This report records preparation; '
             'it does not select, replace or accession a Master.', '',
             f'Metadata file SHA-256: `{metadata_sha256}`.', '',
             f'TIFF candidates: 305; source bytes: {summary["source_tiff_bytes"]:,}; known companion JPEGs: {summary["known_companion_jpegs"]}.', '',
             '| Relationship | Candidates |', '| --- | ---: |']
    labels = {'existing_catalogue_object': 'Accepted catalogue ID already has a Kurt Schwarz Master',
              'existing_local_object_lead': 'Filename link to an existing local Kurt Schwarz object',
              'possible_addition': 'Possible addition requiring identity and Master confirmation',
              'collection_placement_unresolved': 'Collection placement unresolved',
              'ambiguous_archive_links': 'Multiple archive links requiring resolution'}
    lines += [f'| {labels[k]} | {v} |' for k, v in counts.items()]
    lines += ['', '## Proposed bounded accession scope', '',
              'Reuse `/mnt/volume/iss-archive/units/kurt-schwarz` for confirmed Kurt Schwarz additions. '
              'No new unit or directory is proposed. Preserve the current Master for each existing object '
              'until an explicit comparison and human selection justify a replacement. The existing Master '
              'must not remain a second active Master after a replacement.', '',
              'Priority comparison: `C07-T128_002.tif` links to `md:103600` / `KS-7-BZ_0553-2`, '
              'whose current Master is a JPEG selected after an unreadable-TIFF case. Its larger raw TIFF '
              'candidate is a lead for improvement, not yet an approved replacement.', '',
              'Source TIFF format failures: ' + ', '.join('`' + r['filename'] + '`' for r in prepared if r['source_format_issue']) +
              '. Exclude these TIFFs from accession pending diagnosis. Pillow and ExifTool report unreadable '
              'directories; the first IFD offsets are recorded against file sizes. All three link to existing '
              'archive Masters whose headers are readable. The source JPEGs remain useful review aids but '
              'do not prove the TIFFs are intact.', '',
              '## Candidates requiring an object or collection decision', '',
              '| File | Preparation finding | Existing object |', '| --- | --- | --- |']
    for row in prepared:
        if row['preparation_group'] != 'existing_catalogue_object':
            lines.append(f'| {row["filename"]} | {labels[row["preparation_group"]]} | ' +
                         '; '.join(m['record_key'] for m in row['existing_masters']) + ' |')
    lines += ['', '## Before an authorized accession', '',
              '1. Confirm object identity and full-content/usable-detail comparison; select exactly one Master '
              'per resolved object and record every other known candidate as redundant or explicitly unresolved.',
              '2. Confirm the exact additions and any replacements in the existing unit; keep the seven '
              'unplaced files unresolved until collection ownership is established.',
              '3. On explicitly authorized accession, copy only selected Masters without unintended overwrite. '
              'Use the existing bounded TIFF metadata write/readback path where safe; otherwise retain original '
              'bytes with accepted metadata in the catalogue. Preserve date and attribution qualifications.',
              '4. Generate each access JPEG from its finalized selected Master during accession, following the '
              'existing Kurt Schwarz full-resolution JPEG convention. Hash finalized outputs once, then update '
              'the existing SQLite, CSV/XLSX, source dispositions and restore records together.',
              '5. Verify exactly one active Master per resolved object, readable-record/SQLite agreement, '
              'source provenance and unchanged protected source files. No deletion is included.', '',
              '## Evidence and checks', '',
              '`accession-preparation.jsonl` contains all 305 exact source paths, source stamps, technical headers, '
              'accepted captions, existing Master paths and stored hashes, companion paths and the proposed action. '
              '`accession-preparation.json` contains counts, the metadata acceptance record and validation.', '',
              'The protected mount is correct. The 305-row review SQLite and CSV agree. Kurt Schwarz SQLite '
              'quick_check passed; its 1,935 selected Master paths and hashes agree with the file catalogue. '
              'Header inspection is not a full decode or a quality verdict. No payload was copied or hashed, '
              'no derivative was written, and no source, archive, Master decision or directory structure changed.', '']
    if summary['full_decode_checks_passed']:
        lines += [f"Full raster decoding passed for {summary['full_decode_checks_passed']} proposed-addition or JPEG-comparison TIFFs using libvips. "
                  'The exact source paths and unchanged file stamps are recorded in `accession-source-decode.json`. '
                  'This establishes decodability, not object novelty or superior usable detail.', '']
    (ROOT / 'ACCESSION_PREPARATION.md').write_text('\n'.join(lines))
    print(json.dumps({k: summary[k] for k in ('candidate_count', 'source_tiff_bytes', 'groups',
          'priority_jpeg_master_comparisons', 'header_errors', 'approved_accession_rows')}, indent=2))


if __name__ == '__main__':
    main()
