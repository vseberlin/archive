#!/usr/bin/env python3
"""Bounded public-catalogue evidence for the existing 305-candidate review.

Images are decoded in memory only. This never writes sources or archive files,
selects Masters, or changes review decisions. Captions remain proposals.
"""
import argparse
import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageOps
from scipy.fft import dctn

ROOT = Path(__file__).resolve().parent
STAMP = datetime.now(timezone.utc).isoformat()

def get(url):
    r = requests.get(url, timeout=45)
    r.raise_for_status()
    return r

def hashes(im):
    gray = im.convert('L')
    a = np.asarray(gray.resize((8, 8), Image.Resampling.LANCZOS), dtype=float)
    d = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=float)
    p = dctn(np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=float), type=2)[:8, :8]
    def bits(v):
        return int(''.join('1' if x else '0' for x in v.flatten()), 2)
    return [bits(p > np.median(p)), bits(d[:, 1:] > d[:, :-1]), bits(a > a.mean())]

def capture():
    first = get('https://berlin.museum-digital.de/export/json/collection:783?limit=100&offset=0').json()
    total = first['results']['total']
    objects = dict(first['results']['objects'])
    def page(offset):
        return get(f'https://berlin.museum-digital.de/export/json/collection:783?limit=100&offset={offset}').json()['results']['objects']
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(page, range(100, total, 100)):
            objects.update(part)
    assert len(objects) == total
    with (ROOT/'web-catalogue-evidence.jsonl').open('w') as out:
        for key, obj in objects.items():
            assert int(key) == obj['object_id']
            out.write(json.dumps({'retrieved_at': STAMP, 'url': f'https://berlin.museum-digital.de/json/object/{key}', 'record': obj}, ensure_ascii=False)+'\n')
    print(f'Captured {len(objects)} public objects', flush=True)

def catalogue_hashes():
    rows = [json.loads(s) for s in (ROOT/'web-catalogue-evidence.jsonl').read_text().splitlines()]
    def one(row):
        obj = row['record']
        results = []
        for im in obj.get('object_images', []):
            if not im.get('preview'): continue
            url = 'https://berlin.museum-digital.de/data/berlin/'+im['folder']+'/'+im['preview']
            try:
                pil = Image.open(io.BytesIO(get(url).content))
                results.append({'object_id': obj['object_id'], 'url':url, 'hashes':hashes(pil), 'size':pil.size})
            except Exception as e:
                results.append({'object_id':obj['object_id'], 'url':url, 'error':str(e)})
        return results
    with (ROOT/'web-image-evidence.jsonl').open('w') as out, ThreadPoolExecutor(max_workers=6) as pool:
        for n, group in enumerate(pool.map(one, rows)):
            for row in group: out.write(json.dumps(row)+'\n')
            if n % 200 == 0: print(f'Public images: {n}/{len(rows)}',flush=True)

def candidate_hashes():
    import workbench
    workbench.mount_guard()
    rows = list(csv.DictReader((ROOT/'candidates.csv').open()))
    with (ROOT/'candidate-image-evidence.jsonl').open('w') as out:
        for n, row in enumerate(rows):
            result = {'source_relative_path': row['source_relative_path'], 'filename':row['filename'], 'variants': []}
            try:
                if not row['proxy_source_path']: raise ValueError('No same-stem JPEG')
                path = Path(row['proxy_source_path']).resolve(strict=True)
                assert path.is_relative_to(workbench.SOURCE_ROOT)
                with Image.open(path) as original:
                    original.draft('RGB', (800,800))
                    im = ImageOps.exif_transpose(original).convert('L')
                    im.thumbnail((800,800))
                    for angle in (0,90,180,270):
                        oriented = im.rotate(angle, expand=True)
                        for crop in (0,.02,.05,.10):
                            w,h=oriented.size
                            cut=oriented.crop((int(w*crop),int(h*crop),int(w*(1-crop)),int(h*(1-crop))))
                            result['variants'].append({'angle':angle,'crop':crop,'hashes':hashes(cut)})
                st=path.stat();result.update(proxy_source_path=str(path),source_size=st.st_size,source_mtime_ns=st.st_mtime_ns)
            except Exception as e: result['error']=str(e)
            out.write(json.dumps(result)+'\n');out.flush()
            if n%25==0:print(f'Local previews: {n}/{len(rows)}',flush=True)

def matches():
    public = [json.loads(s) for s in (ROOT/'web-image-evidence.jsonl').read_text().splitlines()]
    public = [r for r in public if 'hashes' in r]
    with (ROOT/'web-match-evidence.jsonl').open('w') as out:
        for line in (ROOT/'candidate-image-evidence.jsonl').read_text().splitlines():
            row = json.loads(line)
            best = {}
            for variant in row['variants']:
                for ref in public:
                    dd=[(a^b).bit_count() for a,b in zip(variant['hashes'],ref['hashes'])]
                    for inverted in (False,True):
                        ds=[64-v for v in dd] if inverted else dd
                        score=ds[0]*2+ds[1]+ds[2]
                        key=ref['object_id']
                        if key not in best or score<best[key]['score']:
                            best[key]={'object_id':key,'url':ref['url'],'score':score,'distances':ds,'angle':variant['angle'],'crop':variant['crop'],'inverted':inverted}
            top=sorted(best.values(),key=lambda r:r['score'])[:3]
            out.write(json.dumps({'source_relative_path':row['source_relative_path'],'filename':row['filename'],'matches':top})+'\n')
    print('Saved ranked image leads; no metadata or decisions applied.',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('operation',choices=['capture','catalogue_hashes','candidate_hashes','matches'])
    globals()[parser.parse_args().operation]()
