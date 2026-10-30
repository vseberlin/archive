#!/usr/bin/env python3
"""Read-only TIFF comparison against the current Kurt Schwarz Masters.

Writes bounded JSON evidence in this existing work area. Never changes an index,
archive object, source file, Master selection or disposition.
"""
import csv
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import workbench

ROOT = Path(__file__).resolve().parent
SOURCE = workbench.SOURCE_ROOT
UNIT = workbench.ARCHIVE_UNITS / 'kurt-schwarz'
INDEXES = [Path('/home/vladimir/archive-visual-index/visual-index.sqlite'),
           Path('/mnt/volume/iss-archive-work/tfa/work/reconciliation/wf-full-visual-index/wf-full-visual-index.sqlite')]
FEATURES = ['phash-dct-64-v2', 'dhash-64-v2', 'ahash-64-v2']


def rows(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def stamp(path):
    st = Path(path).stat()
    return {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def normalize(value):
    return ''.join(c.lower() for c in value if c.isalnum())


class Indexes:
    def __init__(self):
        self.sources = []
        self.cache = {}
        for path in INDEXES:
            connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            roots = [(r['root_id'], Path(r['root_path'])) for r in connection.execute('SELECT * FROM visual_roots')]
            extractors = [dict(r) for r in connection.execute('SELECT * FROM feature_extractors') if r['extractor_id'] in FEATURES]
            assert len(extractors) == 3
            self.sources.append((path, connection, roots))

    def get(self, path):
        path = Path(path)
        if str(path) in self.cache:
            return self.cache[str(path)]
        current = stamp(path) if path.is_file() else None
        errors = []
        for index_path, connection, roots in self.sources:
            for root_id, root in roots:
                if not path.is_relative_to(root):
                    continue
                candidates = connection.execute(
                    'SELECT * FROM visual_assets WHERE root_id=? AND relative_path=? ORDER BY indexed_at DESC',
                    (root_id, str(path.relative_to(root))))
                for asset in candidates:
                    if not current or any(asset[k] != current[k] for k in ('size_bytes', 'mtime_ns')):
                        continue
                    feature_rows = {r['extractor_id']: r['feature_hex'] for r in connection.execute(
                        'SELECT * FROM visual_features WHERE asset_id=?', (asset['asset_id'],))}
                    record = {'index_path': str(index_path), 'indexed_path': str(path),
                              'asset_id': asset['asset_id'], 'indexed_at': asset['indexed_at'],
                              'source_stamp': current, 'width': asset['width_px'], 'height': asset['height_px'],
                              'status': asset['status'], 'error': asset['error'],
                              'hashes': [int(feature_rows[k], 16) for k in FEATURES] if all(k in feature_rows for k in FEATURES) else None}
                    if record['status'] == 'indexed' and record['hashes']:
                        self.cache[str(path)] = record
                        return record
                    errors.append(record)
        result = errors[0] if errors else {'indexed_path': str(path), 'source_stamp': current,
                                         'status': 'missing_or_stale_index', 'hashes': None}
        self.cache[str(path)] = result
        return result


def distance(first, second, inverted=False):
    masks = [(1 << 63) - 1, (1 << 64) - 1, (1 << 64) - 1] if inverted else [0, 0, 0]
    return [(a ^ b ^ mask).bit_count() for a, b, mask in zip(first, second, masks)]


def match_record(target, distances, inverted, aspect):
    return {'record_key': target['record_key'], 'inventory': target['inventory'],
            'master_path': target['master_path'], 'title': target['title'],
            'phash_distance': distances[0], 'dhash_distance': distances[1],
            'ahash_distance': distances[2], 'polarity': 'negative_positive' if inverted else 'as_indexed',
            'aspect_relative_delta': aspect,
            'passes_secondary_checks': distances[0] <= 6 and distances[1] <= 10 and distances[2] <= 12 and aspect <= .05,
            'score': 2 * distances[0] + distances[1] + distances[2]}


def main():
    mount = workbench.mount_guard()
    input_stamps = {str(p): stamp(p) for p in INDEXES + [UNIT / 'CATALOG.csv', UNIT / 'db-restore/RESTORE.sqlite']}
    catalogue = rows(UNIT / 'CATALOG.csv')
    masters = [r for r in catalogue if r['file_role'] == 'preservation_master']
    assert len(masters) == len({r['record_key'] for r in masters}) == 1935
    index = Indexes()
    master_records = []
    names = defaultdict(set)
    md = {}
    con = sqlite3.connect(f'file:{UNIT / "db-restore/RESTORE.sqlite"}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    objects = {r['record_key']: dict(r) for r in con.execute('SELECT * FROM objects')}
    assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    con.close()
    for master in masters:
        key = master['record_key']
        path = UNIT / master['final_relative_path']
        assert path.resolve().is_relative_to(UNIT.resolve())
        assert objects[key]['master_relative_path'] == master['final_relative_path']
        assert objects[key]['master_sha256'] == master['final_sha256']
        feature = index.get(path)
        feature_basis = 'current_master_index'
        if not feature['hashes']:
            # The file catalogue representation, not a stale object selection
            # field, identifies the actual source of this accessioned Master.
            assert master['original_source_path'] == master['representation_source_path']
            assert master['representation_final_relative_path'] == master['final_relative_path']
            feature = index.get(master['representation_source_path'])
            feature_basis = 'indexed_source_of_current_master'
        if feature['hashes']:
            assert master['representation_basis'] in (
                'catalog_metadata_reconciled_pixels_and_structure_unchanged',
                'curator_selected_local_master_metadata_in_catalog',
                'curator_confirmed_unreadable_master_replacement')
        obj = objects[key]
        master_records.append({'record_key': key, 'inventory': master['inventory'],
            'md_object_id': master['md_object_id'], 'title': master['title'],
            'master_path': str(path), 'master_stamp': stamp(path),
            'stored_final_sha256': master['final_sha256'],
            'access_path': str(UNIT / obj['access_relative_path']) if obj['access_relative_path'] else '',
            'original_source_path': master['representation_source_path'],
            'representation_basis': master['representation_basis'],
            'feature_basis': feature_basis, 'feature': feature})
        if master['md_object_id']:
            md[master['md_object_id']] = key
        for name in [master['inventory'], Path(master['filename']).stem,
                     Path(master['original_source_path']).stem, master['object_scan_signature']]:
            if name:
                names[normalize(name)].add(key)
        for field in ('object_all_candidate_paths', 'object_original_title_filenames'):
            value = master.get(field, '')
            try:
                parts = json.loads(value)
                if not isinstance(parts, list):
                    parts = [value]
            except (ValueError, TypeError):
                parts = value.split(';')
            for part in parts:
                if part:
                    names[normalize(Path(str(part).strip()).stem)].add(key)
    indexed = [r for r in master_records if r['feature']['hashes']]
    print('Current Masters:', len(masters), 'with reusable hashes:', len(indexed), flush=True)
    (ROOT / 'tiff-scan-masters.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in master_records))
    proposals = {r['source_path']: r for r in rows(ROOT / 'metadata-proposals.csv')}
    old_leads = {r['source_path']: r for r in rows(ROOT.parent / 'avantmedia-unit-comparison-20261014.csv')}
    other_matches = defaultdict(list)
    for row in rows(ROOT.parent / 'avantmedia-master-polarity-phash-matches-20261014.csv'):
        if row['unit'] != 'kurt-schwarz':
            other_matches[row['avantmedia_path']].append(row)
    tiffs = []
    excluded = Counter()
    for directory, _, filenames in os.walk(SOURCE):
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in ('.tif', '.tiff'):
                tiffs.append(path)
            else:
                excluded[path.suffix.lower()] += 1
    tiffs.sort()
    result = []
    for number, path in enumerate(tiffs):
        feature = index.get(path)
        p = proposals.get(str(path), {})
        catalogue_keys = [md[p['catalogue_object_id']]] if p.get('catalogue_object_id') in md else []
        name_keys = set(names.get(normalize(path.stem), []))
        # Existing inventory/alias evidence is advisory and is rebound to the
        # current Master records before use.
        old = old_leads.get(str(path), {})
        for _, inventory, _, _ in json.loads(old.get('current_catalog_filename_evidence_json') or '[]'):
            name_keys.update(names.get(normalize(inventory), []))
        hits = []
        if feature['hashes']:
            ratio = feature['width'] / feature['height']
            for target in indexed:
                other = target['feature']
                aspect = abs(ratio / (other['width'] / other['height']) - 1)
                options = [(distance(feature['hashes'], other['hashes'], inv), inv) for inv in (False, True)]
                dd, inv = min(options, key=lambda x: 2*x[0][0]+x[0][1]+x[0][2])
                hits.append(match_record(target, dd, inv, aspect))
            hits.sort(key=lambda x: (x['score'], x['phash_distance'], x['record_key']))
        strong = [h for h in hits if h['passes_secondary_checks']]
        if catalogue_keys:
            finding = 'existing_kurt_catalogue_link'
        elif strong:
            finding = 'strong_similarity_to_current_master'
        elif name_keys:
            finding = 'existing_kurt_identity_lead'
        elif other_matches[str(path)]:
            finding = 'other_unit_similarity'
        elif not feature['hashes']:
            finding = 'unreadable_or_unindexed'
        else:
            finding = 'unmatched_needs_examination'
        result.append({'source_path': str(path), 'source_relative_path': str(path.relative_to(SOURCE)),
            'filename': path.name, 'source_stamp': stamp(path), 'feature': feature,
            'finding': finding, 'catalogue_record_keys': catalogue_keys,
            'name_record_keys': sorted(name_keys), 'strong_matches': strong,
            'nearest_matches': hits[:5], 'other_unit_matches': other_matches[str(path)],
            'accepted_metadata': p, 'selected_master_path': '', 'deletion_authorized': False})
        if number % 200 == 0:
            print('Compared TIFFs:', number, '/', len(tiffs), flush=True)
    assert all(stamp(Path(p)) == old for p, old in input_stamps.items())
    assert all(stamp(Path(r['source_path'])) == r['source_stamp'] for r in result)
    assert all(stamp(Path(r['master_path'])) == r['master_stamp'] for r in master_records)
    (ROOT / 'tiff-scan-results.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in result))
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(), 'mount': mount,
        'source_root': str(SOURCE), 'unit': str(UNIT), 'candidate_count': len(result),
        'source_tiff_bytes': sum(r['source_stamp']['size_bytes'] for r in result),
        'excluded_non_tiffs': dict(excluded), 'current_master_count': len(masters),
        'master_hashes_reused': len(indexed), 'tiff_hashes_reused': sum(bool(r['feature']['hashes']) for r in result),
        'master_index_gaps': [r['record_key'] for r in master_records if not r['feature']['hashes']],
        'findings': dict(Counter(r['finding'] for r in result)),
        'input_stamps': input_stamps, 'source_stamps_unchanged': True, 'master_stamps_unchanged': True,
        'indexes_modified': False, 'archive_modified': False, 'source_modified': False,
        'new_payload_hashes_calculated': 0, 'derivatives_written': 0, 'deletion_authorized': False,
        'method': 'Existing 64-bit DCT pHash, dHash and aHash; direct and negative/positive complement; exact current Master representation links. pHash<=6, dHash<=10, aHash<=12 and aspect delta<=0.05 are similarity leads, not identity proofs.'}
    (ROOT / 'tiff-scan-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
