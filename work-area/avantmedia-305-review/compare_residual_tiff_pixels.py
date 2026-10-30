#!/usr/bin/env python3
"""Decode only residual TIFFs, search geometric/crop variants in memory."""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

import workbench
import compare_all_tiffs as scan
from refine_tiff_scan import popcounts

sys.path.insert(0, '/home/vladimir/wp/tools/archive-preservation')
from visual_index import feature_hashes

ROOT = Path(__file__).resolve().parent


def variants(image):
    for mirrored in (False, True):
        original = ImageOps.mirror(image) if mirrored else image
        for angle in (0, 90, 180, 270):
            oriented = original.rotate(angle, expand=True)
            width, height = oriented.size
            for cx in (0, .025, .05, .1, .15):
                for cy in (0, .025, .05, .1, .15):
                    cut = oriented.crop((round(width*cx), round(height*cy), round(width*(1-cx)), round(height*(1-cy))))
                    cut.thumbnail((64, 64), Image.Resampling.LANCZOS)
                    values = feature_hashes(cut)
                    features = [int(values[k], 16) for k in ['phash_dct_64', 'dhash_64', 'ahash_64']]
                    yield {'mirror': mirrored, 'angle': angle, 'crop_x': cx, 'crop_y': cy}, features


def main():
    workbench.mount_guard()
    rows = [json.loads(l) for l in (ROOT / 'tiff-scan-results.jsonl').read_text().splitlines()]
    masters = [json.loads(l) for l in (ROOT / 'tiff-scan-masters.jsonl').read_text().splitlines()]
    pa = np.array([m['feature']['hashes'][0] for m in masters], dtype=np.uint64)
    da = np.array([m['feature']['hashes'][1] for m in masters], dtype=np.uint64)
    aa = np.array([m['feature']['hashes'][2] for m in masters], dtype=np.uint64)
    residual = [r for r in rows if r['feature']['hashes'] and (r['finding'] in ('existing_kurt_identity_lead', 'unmatched_needs_examination') or (r['finding'] == 'existing_kurt_catalogue_link' and not r['strong_matches'] and not any(m['passes_orientation_screen'] for m in r.get('orientation_matches', []))))]
    evidence = ROOT / 'tiff-residual-pixel-evidence.jsonl'
    existing = {r['source_path']: r for r in [json.loads(l) for l in evidence.read_text().splitlines()]} if evidence.exists() else {}
    for number, row in enumerate(residual):
        if row['source_path'] in existing and not existing[row['source_path']].get('error') and existing[row['source_path']]['source_stamp'] == row['source_stamp']:
            continue
        path = Path(row['source_path'])
        assert scan.stamp(path) == row['source_stamp']
        result = {'source_path': str(path), 'source_stamp': row['source_stamp'],
                  'method': 'Actual source TIFF sampled in memory; Pillow fit64 then existing DCT-pHash/dHash/aHash; 4 rotations, mirror, independent 0/2.5/5/10/15% edge crops and both polarities. No derivative file written.'}
        try:
            try:
                image = workbench.read_uncompressed_tiff_preview(path, edge=500).convert('L')
            except ValueError:
                with Image.open(path) as original:
                    try:
                        image = ImageOps.exif_transpose(original).convert('L')
                    except TypeError:
                        image = original.convert('L')
                    image.thumbnail((500, 500), Image.Resampling.LANCZOS)
            best = {}
            for geometry, features in variants(image):
                for inverse in (False, True):
                    p, d, a = features
                    if inverse:
                        p ^= (1 << 63) - 1
                        d ^= (1 << 64) - 1
                        a ^= (1 << 64) - 1
                    pd, dd, ad = popcounts(pa ^ np.uint64(p)), popcounts(da ^ np.uint64(d)), popcounts(aa ^ np.uint64(a))
                    score = 2*pd + dd + ad
                    for i in np.argsort(score)[:3]:
                        m = masters[int(i)]
                        key = m['record_key']
                        if key in best and best[key]['score'] <= int(score[i]):
                            continue
                        best[key] = {'record_key': key, 'inventory': m['inventory'],
                                     'master_path': m['master_path'], 'title': m['title'],
                                     **geometry, 'inverted': inverse, 'phash_distance': int(pd[i]),
                                     'dhash_distance': int(dd[i]), 'ahash_distance': int(ad[i]), 'score': int(score[i]),
                                     'passes_secondary_checks': bool(pd[i]<=8 and dd[i]<=12 and ad[i]<=14)}
            result['matches'] = sorted(best.values(), key=lambda r: r['score'])[:8]
        except (OSError, ValueError, TypeError) as error:
            result['error'] = str(error)
        assert scan.stamp(path) == row['source_stamp']
        with evidence.open('a') as out:
            out.write(json.dumps(result, ensure_ascii=False)+'\n')
            out.flush()
        top = result.get('matches', [])[:1]
        print(number+1, '/', len(residual), row['filename'], [(m['inventory'],m['score'],m['passes_secondary_checks']) for m in top], flush=True)


if __name__ == '__main__':
    main()
