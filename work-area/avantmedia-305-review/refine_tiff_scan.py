#!/usr/bin/env python3
"""Refine only unresolved image comparisons using saved hashes and decisions."""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import compare_all_tiffs as scan

ROOT = Path(__file__).resolve().parent
BITCOUNTS = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)


def popcounts(values):
    return BITCOUNTS[values.astype(np.uint64).view(np.uint8).reshape(-1, 8)].sum(axis=1)


def bits(value):
    return np.array([int(c) for c in f'{value:064b}'], dtype=np.uint8).reshape(8, 8)


def integer(array):
    return int(''.join(str(int(x)) for x in array.flat), 2)


def transforms(feature):
    p, _, a = feature['hashes']
    pb, ab = bits(p), bits(a)
    row_signs = np.indices((8, 8))[0] % 2
    col_signs = np.indices((8, 8))[1] % 2
    for mirror in (False, True):
        for angle in (0, 90, 180, 270):
            aa = np.fliplr(ab) if mirror else ab.copy()
            pp = pb ^ col_signs if mirror else pb.copy()
            for _ in range(angle // 90):
                aa = np.rot90(aa)
                pp = pp.T ^ row_signs
            pp[0, 0] = 1
            for inverted in (False, True):
                pi, ai = integer(pp), integer(aa)
                if inverted:
                    pi ^= (1 << 63) - 1
                    ai ^= (1 << 64) - 1
                width, height = feature['width'], feature['height']
                if angle % 180:
                    width, height = height, width
                yield angle, mirror, inverted, pi, ai, width / height


def main():
    scan.workbench.mount_guard()
    records = [json.loads(l) for l in (ROOT / 'tiff-scan-results.jsonl').read_text().splitlines()]
    masters = [json.loads(l) for l in (ROOT / 'tiff-scan-masters.jsonl').read_text().splitlines()]
    summary = json.loads((ROOT / 'tiff-scan-summary.json').read_text())
    # Fill the single missing Master feature without changing either shared
    # index. This JPEG is small; normalization stays entirely in memory.
    sys.path.insert(0, '/home/vladimir/wp/tools/archive-preservation')
    from visual_index import feature_hashes
    filled = 0
    for master in masters:
        if master['feature']['hashes']:
            continue
        path = Path(master['master_path'])
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original).convert('RGB')
            dimensions = image.size
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            values = feature_hashes(image)
        master['feature'] = {'indexed_path': str(path), 'status': 'computed_in_memory',
            'source_stamp': scan.stamp(path), 'width': dimensions[0], 'height': dimensions[1],
            'hashes': [int(values[k], 16) for k in ['phash_dct_64', 'dhash_64', 'ahash_64']],
            'normalization': 'Pillow RGB auto-orient fit64 LANCZOS; same existing DCT/aHash/dHash implementation; small normalization difference from original libvips cache'}
        master['feature_basis'] = 'current_master_gap_filled_in_memory'
        filled += 1
    assert all(m['feature']['hashes'] for m in masters)
    by_key = {m['record_key']: m for m in masters}
    target_p = np.array([m['feature']['hashes'][0] for m in masters], dtype=np.uint64)
    target_a = np.array([m['feature']['hashes'][2] for m in masters], dtype=np.uint64)
    target_ratios = np.array([m['feature']['width']/m['feature']['height'] for m in masters])
    prepared = {r['source_path']: r for r in [json.loads(l) for l in (ROOT / 'accession-preparation.jsonl').read_text().splitlines()]}
    census = scan.rows(scan.UNIT / 'db-restore/manifests/object-census-decisions.csv')
    decisions = {}
    for r in census:
        decisions.setdefault(scan.normalize(Path(r['filename']).stem), []).append(r)
    indexes = scan.Indexes()
    for number, record in enumerate(records):
        record['initial_finding'] = record['finding']
        if record['source_path'] in prepared:
            for master in prepared[record['source_path']]['existing_masters']:
                if master['unit'] == 'kurt-schwarz' and master['record_key'] in by_key:
                    record['name_record_keys'] = sorted(set(record['name_record_keys']) | {master['record_key']})
        prior = []
        for decision in decisions.get(scan.normalize(Path(record['filename']).stem), []):
            feature = indexes.get(decision['primary_source_path'])
            if feature['hashes'] and record['feature']['hashes']:
                dd = scan.distance(feature['hashes'], record['feature']['hashes'])
                if dd == [0, 0, 0] and feature['width'] == record['feature']['width'] and feature['height'] == record['feature']['height']:
                    prior.append({'decision': decision, 'binding': 'same_normalized_filename_and_dimensions_and_all_three_visual_hashes',
                                  'compared_source': feature})
        record['prior_census_decisions'] = prior
        if not record['feature']['hashes']:
            record['orientation_matches'] = []
            continue
        if record['strong_matches'] or record['finding'] == 'other_unit_similarity':
            record['orientation_matches'] = []
            continue
        best = {}
        for angle, mirror, inverted, p, a, ratio in transforms(record['feature']):
            pd = popcounts(target_p ^ np.uint64(p))
            ad = popcounts(target_a ^ np.uint64(a))
            aspect = np.abs(ratio / target_ratios - 1)
            scores = 2*pd + ad + np.minimum(aspect*20, 30)
            nearest = np.argsort(scores)[:8]
            for i in nearest:
                master = masters[int(i)]
                key = master['record_key']
                if key in best and best[key]['score'] <= float(scores[i]):
                    continue
                best[key] = {'record_key': key, 'inventory': master['inventory'],
                    'master_path': master['master_path'], 'title': master['title'],
                    'angle': angle, 'mirror': mirror, 'inverted': inverted,
                    'phash_distance': int(pd[i]), 'ahash_distance': int(ad[i]),
                    'aspect_relative_delta': float(aspect[i]), 'score': float(scores[i]),
                    'passes_orientation_screen': bool(int(pd[i]) <= 8 and int(ad[i]) <= 12 and aspect[i] <= .07),
                    'method': 'Saved pHash coefficient sign/transpose approximation plus exact aHash bit geometry; a search lead, not a newly decoded image comparison'}
        record['orientation_matches'] = sorted(best.values(), key=lambda r: r['score'])[:5]
        close = [m for m in record['orientation_matches'] if m['passes_orientation_screen']]
        if close and record['finding'] not in ['existing_kurt_catalogue_link']:
            record['finding'] = 'orientation_similarity_to_current_master'
        elif record['name_record_keys'] and record['finding'] == 'unmatched_needs_examination':
            record['finding'] = 'existing_kurt_identity_lead'
        if number % 200 == 0:
            print('Refined', number, flush=True)
    summary['master_index_gaps'] = []
    summary['master_features_newly_computed'] = sum(m['feature_basis'] == 'current_master_gap_filled_in_memory' for m in masters)
    summary['comparison_master_count'] = len(masters)
    summary['findings'] = dict(Counter(r['finding'] for r in records))
    summary['orientation_search'] = 'For residual nonmatches only: four right-angle rotations, mirroring and both polarities using saved pHash/aHash bits. pHash sign transformations are approximate; direct visual checking is still required for residual identity claims.'
    (ROOT / 'tiff-scan-masters.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in masters))
    (ROOT / 'tiff-scan-results.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records))
    (ROOT / 'tiff-scan-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary['findings'], indent=2))


if __name__ == '__main__':
    main()
