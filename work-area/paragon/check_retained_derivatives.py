"""Bounded read-only derivative comparison for the remaining Paragon cases.

Writes one work-area report. Does not save decisions or modify any media.
Stored hashes identify groups; image comparisons establish same-frame coverage.
"""
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageOps, ImageStat

import paragon_workbench as app
import retained_candidates as review

ROOT = review.ROOT
OUTPUT = ROOT / 'retained-derivative-check.json'


def load_image(path):
    with Image.open(path) as original:
        return ImageOps.exif_transpose(original).convert('RGB')


def simple_image(path):
    with Image.open(path) as image:
        bits = image.tag_v2.get(258, (8,)) if image.format == 'TIFF' else (8,)
        if isinstance(bits, int):
            bits = (bits,)
        return getattr(image, 'n_frames', 1) == 1 and image.mode in ('L', 'RGB') and max(bits) <= 8


def metrics(left, right, size=384):
    a = np.asarray(left.resize((size, size), Image.Resampling.LANCZOS), dtype=np.float32)
    b = np.asarray(right.resize((size, size), Image.Resampling.LANCZOS), dtype=np.float32)
    delta = np.abs(a - b)
    x, y = a.mean(axis=2), b.mean(axis=2)
    correlation = float(np.corrcoef(x.ravel(), y.ravel())[0, 1]) if min(x.std(), y.std()) > 2 else 0
    tiles = [float(delta[i:i+48, j:j+48].mean()) for i in range(0, size, 48) for j in range(0, size, 48)]
    return dict(mean_error=float(delta.mean()), p99_error=float(np.percentile(delta, 99)),
                max_tile_error=max(tiles), correlation=correlation)


def compare(group, targets, assets, excluded):
    paths = group['remaining_paths']
    usable = [p for p in paths if not assets[p]['other_unit_links'] and p not in excluded]
    result = dict(source_paths=paths, source_sha256=group['sha256'], comparisons=[], confirmed=False)
    if not usable:
        result['reason'] = 'Other-unit evidence or explicit prior source-retention hold.'
        return result
    if not targets:
        result['reason'] = 'No eligible equal-or-greater-resolution comparison target in existing evidence.'
        return result
    source_path = usable[0]
    if not simple_image(source_path):
        result['reason'] = 'Additional frames, channels or bit depth require separate review.'
        return result
    source = load_image(source_path)
    for target in targets:
        stamp = Path(target['path']).stat()
        if target['kind'] == 'accessioned_master':
            assert stamp.st_size == target['size_bytes'], target['path']
        original = load_image(target['path'])
        # Use the identical sampling grid for both images. A thumbnail followed
        # by stretching introduces a second interpolation only on the target.
        small = original.resize((384, 384), Image.Resampling.LANCZOS)
        possible = []
        for angle in (0, 90, 180, 270):
            sw, sh = source.size
            tw, th = original.size if angle in (0, 180) else original.size[::-1]
            if sw > tw or sh > th or abs(sw / sh - tw / th) / (tw / th) > .003:
                continue
            candidate = small.rotate(angle, expand=True)
            sample = metrics(source, candidate)
            possible.append((sample['mean_error'], angle, sample))
        if not possible:
            continue
        _, angle, sample = min(possible)
        candidate = original.rotate(angle, expand=True)
        # Requiring low differences across all 64 tiles prevents a small shared
        # region or similar composition from passing as full-image coverage.
        if sample['mean_error'] > 2.5 or sample['p99_error'] > 15 or sample['max_tile_error'] > 6 or sample['correlation'] < .997:
            result['comparisons'].append(dict(target=target, rotation=angle, sample=sample, accepted=False))
            continue
        scaled = candidate.resize(source.size, Image.Resampling.LANCZOS)
        difference = ImageChops.difference(source, scaled)
        stats = ImageStat.Stat(difference)
        full = dict(mean_error=sum(stats.mean)/3, rms_error=math.sqrt(sum(v*v for v in stats.rms)/3),
                    maximum_error=max(high for low, high in difference.getextrema()))
        exact = full['maximum_error'] == 0 and source.size == candidate.size
        # TIFF versions allow only exact pixels or one-level rounding. JPEG
        # access versions may differ by small compression/resampling residuals.
        jpeg = Path(source_path).suffix.lower() in ('.jpg', '.jpeg')
        rounding_only = (source.size == candidate.size and full['maximum_error'] <= 1 and full['mean_error'] <= .05)
        accepted = exact or rounding_only or (jpeg and full['mean_error'] <= 3 and full['rms_error'] <= 6)
        item = dict(target=target, rotation=angle, source_dimensions=list(source.size),
                    target_dimensions=list(candidate.size), sample=sample, full=full,
                    exact_decoded_pixels=exact, one_level_rounding_only=rounding_only, accepted=accepted)
        after = Path(target['path']).stat()
        assert (after.st_size, after.st_mtime_ns) == (stamp.st_size, stamp.st_mtime_ns), target['path']
        result['comparisons'].append(item)
        if accepted:
            result.update(confirmed=True, eligible_paths=usable, evidence=item,
                          classification='same_decoded_image' if exact else 'same_frame_raster_copy' if rounding_only else 'same_frame_jpeg_derivative')
            return result
    result['reason'] = 'No sufficiently close full-frame match at equal or greater retained resolution.'
    return result


def main():
    if OUTPUT.exists() and json.loads(OUTPUT.read_text()).get('decisions_saved'):
        raise ValueError('Completed decision evidence must not be overwritten.')
    mount = app.mount_guard()
    ev = review.evidence()
    review.verify_evidence()
    with app.connect() as con:
        cases = review.cases(con)
        saved = review.saved_reviews(con)
        latest = {}
        for row in con.execute("SELECT after_json FROM logbook WHERE action='review_source_candidate' ORDER BY at_utc,log_id"):
            data = json.loads(row['after_json'])
            if data.get('source_path'):
                latest[data['source_path']] = data
    remaining = [c for c in cases if c['key'] not in saved]
    assets = {s['path']:s for c in remaining for s in c['sources']}
    for asset in ev['assets'].values():
        review.verify_asset(asset)
    with (review.ARCHIVE/'CATALOG.csv').open() as handle:
        catalogue = list(csv.DictReader(handle))
    final = {str(review.ARCHIVE/r['final_relative_path']):r for r in catalogue}
    selected = {r['selected_master_path'] for r in saved.values() if r['action'] == 'add'}
    excluded = {p for p,d in latest.items() if d.get('state') == 'candidate'}
    held_hashes = {assets[p]['sha256'] for p in excluded if p in assets}
    excluded.update(p for p,s in assets.items() if s['sha256'] in held_hashes)
    tiffs = defaultdict(list)
    for asset in ev['assets'].values():
        if asset['source']=='retained' and asset['decoded'] and asset['format'] in ('TIF','TIFF'):
            tiffs[Path(asset['path']).stem.casefold()].append(asset)
    tasks = []
    for family in ev['visual']['families']:
        for raw in family['groups']:
            paths = [p for p in raw['paths'] if p in assets]
            if not paths:
                continue
            group = raw | {'remaining_paths':paths}
            targets = []
            seen = set()
            def add_target(path, kind, width, height, **details):
                w,h=sorted((group['width'],group['height']));tw,th=sorted((width,height))
                if path in seen or path in paths or w>tw or h>th or abs(w/h-tw/th)/(tw/th)>.003:
                    return
                seen.add(path)
                targets.append(dict(path=path, kind=kind, awaiting_accession=kind!='accessioned_master', **details))
            for near in group['nearest']:
                if near['score']>18:
                    continue
                ref=final[near['path']]
                master=final[str(review.ARCHIVE/ref['master_relative_path'])]
                add_target(str(review.ARCHIVE/master['final_relative_path']), 'accessioned_master',
                           int(master['width']),int(master['height']),inventory=master['inventory'],
                           sha256=master['final_sha256'],size_bytes=int(master['size_bytes']))
            if Path(paths[0]).suffix.lower() in ('.jpg','.jpeg'):
                candidates={s['path']:s for p in paths for s in tiffs[Path(p).stem.casefold()]}
                for asset in sorted(candidates.values(),key=lambda s:(s['path'] not in selected, s['size_bytes'],s['path'])):
                    if asset['width']*asset['height'] <= group['width']*group['height']:
                        continue
                    add_target(asset['path'],'selected_master_awaiting_accession' if asset['path'] in selected else 'retained_full_resolution_source',
                               asset['width'],asset['height'],sha256=asset['sha256'])
            tasks.append((group,targets))
    results=[]
    print(f'Checking {len(tasks)} stored-hash groups; {sum(bool(t) for g,t in tasks)} have eligible comparison targets.',flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for i,result in enumerate(pool.map(lambda task:compare(*task, assets, excluded),tasks),1):
            results.append(result)
            if i%50==0:
                print(f'{i}/{len(tasks)} groups; {sum(len(r.get("eligible_paths",[])) for r in results if r["confirmed"])} confirmed paths',flush=True)
    for asset in ev['assets'].values():
        review.verify_asset(asset)
    review.verify_evidence()
    report=dict(checked_at=datetime.now(timezone.utc).isoformat(),source_mount=mount,
                scope='Previously unsaved retained-review cases only',case_count=len(remaining),path_count=len(assets),
                case_kinds=dict(Counter(c['kind'] for c in remaining)),formats=dict(Counter(s['format'] for s in assets.values())),
                source_bytes=sum(s['size_bytes'] for s in assets.values()),
                authorization='what full view contains? if derivatives just add it to drain list',
                method='Existing source hashes grouped unchanged files; existing visual features and names only supplied comparison candidates. Full-frame RGB comparison at original source resolution and 64 spatial tiles; TIFFs require exact decoded equality or same dimensions with maximum one-level error and mean <=0.05. JPEGs require equal-or-greater target dimensions, matching aspect, sample mean <=2.5, p99 <=15, worst-tile mean <=6, correlation >=0.997, full mean <=3 and RMS <=6. No crop matching. Other-unit evidence and prior explicit source holds excluded.',
                groups=results,unreadable_paths=[p for p,s in assets.items() if not s['decoded'] and s['format'] in ('JPG','JPEG','TIF','TIFF')],
                document_paths=[p for p,s in assets.items() if s['format'] not in ('JPG','JPEG','TIF','TIFF')],
                decisions_saved=False,source_modified=False,archive_modified=False,deletion_authorized=False)
    OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(confirmed_paths=sum(len(r.get('eligible_paths',[])) for r in results if r['confirmed']),
                         target_kinds=dict(Counter(r['evidence']['target']['kind'] for r in results if r['confirmed'])),
                         report=str(OUTPUT))),flush=True)


def check_embedded_exports():
    report = json.loads(OUTPUT.read_text())
    if report.get('decisions_saved'):
        raise ValueError('Completed decision evidence must not be overwritten.')
    app.mount_guard()
    metadata = json.loads((ROOT/'retained-derivative-metadata.json').read_text())
    with app.connect() as con:
        saved = review.saved_reviews(con)
        remaining = [c for c in review.cases(con) if c['key'] not in saved]
        latest = {}
        for row in con.execute("SELECT after_json FROM logbook WHERE action='review_source_candidate' ORDER BY at_utc,log_id"):
            data = json.loads(row['after_json'])
            if data.get('source_path'):
                latest[data['source_path']] = data
    assets = {s['path']:s for c in remaining for s in c['sources']}
    held = {p for p,d in latest.items() if d.get('state') == 'candidate'}
    held_hashes = {assets[p]['sha256'] for p in held if p in assets}
    excluded = held | {p for p,s in assets.items() if s['sha256'] in held_hashes}
    already = {p for g in report['groups'] if g['confirmed'] for p in g['eligible_paths']}
    rejected = {f['path'] for r in saved.values() for f in r['files'] if f['state'] in ('redundant','noise_drain')}
    selected = {r['selected_master_path'] for r in saved.values() if r['action']=='add'}

    def ids(data):
        return {str(v).lower() for k,v in data.items() if k.endswith(('DocumentID','OriginalDocumentID','DerivedFromDocumentID','DerivedFromOriginalDocumentID')) and isinstance(v,str)}

    tiffs = defaultdict(list)
    for asset in review.evidence()['assets'].values():
        if asset['source']=='retained' and asset['decoded'] and asset['format'] in ('TIF','TIFF') and asset['path'] not in rejected:
            for identity in ids(metadata.get(asset['sha256'],{})):
                tiffs[identity].append(asset)
    confirmed = []
    checked = []
    for path, asset in assets.items():
        data = metadata.get(asset['sha256'],{})
        if path in already or path in excluded or asset['other_unit_links'] or asset['format']!='JPG' or not asset['decoded'] or max(asset['width'],asset['height'])>1600:
            continue
        if 'converted from image/tiff to image/jpeg' not in str(data.get('XMP-xmpMM:HistoryParameters','')).lower():
            continue
        targets = {t['path']:t for identity in ids(data) for t in tiffs[identity]}
        sw,sh = sorted((asset['width'],asset['height']))
        targets = [t for t in targets.values() if (lambda tw,th:tw>=2*sw and th>=2*sh and abs(sw/sh-tw/th)/(tw/th)<.003)(*sorted((t['width'],t['height'])))]
        if not targets or not simple_image(path):
            continue
        review.verify_asset(asset)
        source = load_image(path)
        for target in sorted(targets,key=lambda t:(t['path'] not in selected,t['size_bytes'],t['path'])):
            review.verify_asset(target)
            original = load_image(target['path'])
            candidates=[]
            for angle in (0,90,180,270):
                tw,th=original.size if angle in (0,180) else original.size[::-1]
                w,h=source.size
                if abs(w/h-tw/th)/(tw/th)<.003:
                    sample=metrics(source,original.resize((384,384),Image.Resampling.LANCZOS).rotate(angle,expand=True))
                    candidates.append((sample['mean_error'],angle,sample))
            if not candidates:
                continue
            _,angle,sample=min(candidates)
            accepted=sample['correlation']>=.99 and sample['mean_error']<=15 and sample['max_tile_error']<=35
            item=dict(source_path=path,source_sha256=asset['sha256'],target=dict(path=target['path'],sha256=target['sha256'],kind='selected_master_awaiting_accession' if target['path'] in selected else 'retained_full_resolution_source',awaiting_accession=True),shared_document_ids=sorted(ids(data)&ids(metadata[target['sha256']])),history_parameters=data['XMP-xmpMM:HistoryParameters'],rotation=angle,sample=sample,source_dimensions=list(source.size),target_dimensions=list(original.size),accepted=accepted,classification='embedded_lineage_reduced_jpeg_export')
            checked.append(item)
            review.verify_asset(target)
            if accepted:
                confirmed.append(item)
                break
        review.verify_asset(asset)
        if len(checked)%10==0:
            print('Embedded comparisons:',len(checked),'confirmed:',len(confirmed),flush=True)
    report['embedded_export_check']=dict(method='Explicit TIFF-to-JPEG export history and matching source-document identifiers; JPEG at most 1600px, retained TIFF at least twice each dimension, aspect difference <0.003; full-frame sample correlation >=0.99, mean error <=15, worst tile <=35. These are related reduced exports, not assertions of identical processing. Retained-source accession remains required.',comparisons=checked,confirmed=confirmed)
    review.write_json(OUTPUT,report)
    print('Additional embedded-lineage exports:',len(confirmed),flush=True)


if __name__=='__main__':
    check_embedded_exports() if '--metadata' in sys.argv else main()
