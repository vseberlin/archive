#!/usr/bin/env python3
"""OCR-first, proposal-only photo research. No model calls or catalogue writes."""
import argparse
import csv
import html
import io
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

# Large noisy native-resolution crops can produce fields beyond csv's 128 KiB
# default. Preserve them as raw evidence, never as confirmed words.
csv.field_size_limit(16 * 1024 * 1024)

WORK = Path('/mnt/volume/iss-archive-work')
ELEMENTS = Path('/media/vladimir/Elements')


def read(path):
    return json.loads(Path(path).read_text())


def destination(path):
    path = Path(path)
    if not path.parent.is_dir() or not path.resolve().is_relative_to(WORK.resolve()):
        raise ValueError('Output must be in an existing iss-archive-work directory')
    if path.exists() or path.is_symlink():
        raise ValueError(f'Refusing overwrite: {path}')
    return path


def save(path, value):
    path = destination(path)
    with path.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def stamp(path):
    st = path.stat()
    return {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def protected_source(path):
    path = Path(path)
    if not path.resolve().is_relative_to(ELEMENTS / 'restore/archive'):
        raise ValueError('Expected protected Elements archive source')
    mount = subprocess.check_output(
        ['findmnt', '-no', 'SOURCE,TARGET,FSTYPE,OPTIONS', '--target', str(path)],
        text=True).split()
    if mount[:3] != ['/dev/sdf1', str(ELEMENTS), 'ntfs3'] or 'ro' not in mount[3].split(','):
        raise ValueError('Exact protected Elements mount is not read-only')
    return path


def crop_box(region, size):
    if len(region) != 4 or not all(isinstance(v, (int, float)) for v in region):
        raise ValueError('Crop must be four normalized coordinates')
    x0, y0, x1, y1 = region
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError('Invalid normalized crop')
    w, h = size
    box = [round(x0*w), round(y0*h), round(x1*w), round(y1*h)]
    if box[0] == box[2] or box[1] == box[3]:
        raise ValueError('Empty crop')
    return box


def run_ocr(args):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 150000000
    output = destination(args.output)
    images = read(args.plan)['images']
    if args.limit:
        images = images[:args.limit]
    if len({i['number'] for i in images}) != len(images) or len({i['id'] for i in images}) != len(images):
        raise ValueError('Duplicate input numbers or object IDs')
    regions = read(args.regions) if args.regions else {}
    version = subprocess.check_output(['tesseract', '--version'], text=True).splitlines()[0]
    langs = subprocess.check_output(['tesseract', '--list-langs'], text=True).splitlines()
    if not all(lang in langs for lang in args.languages.split('+')):
        raise ValueError('Requested Tesseract languages unavailable')
    # Preflight every source and output before starting any OCR.
    for item in images:
        protected_source(item['source_path_from_manifest'])
        destination(output.with_name(f'{output.stem}-{item["number"]:02d}-receipt.json'))
        if item['recipe'].get('visual_checked') is not True:
            raise ValueError('Orientation/polarity must first be visually checked')
        if item['recipe'].get('rotation', 0) not in (0, 90, -90, 180, 270):
            raise ValueError('Use right-angle recipe rotations only')
        names = ['overview'] + [r['label'] for r in regions.get(item['id'], [])]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate crop labels')
        for name in names:
            if not re.fullmatch(r'[a-z0-9-]+', name):
                raise ValueError('Crop labels must use lowercase letters, digits and hyphens')
            for ext in ('png', 'txt', 'tsv'):
                destination(output.with_name(f'{output.stem}-{item["number"]:02d}-{name}.{ext}'))
    result = {'metadata_adopted': False, 'tesseract_version': version,
              'languages': args.languages, 'results': []}
    for item in images:
        source = protected_source(item['source_path_from_manifest'])
        before = stamp(source)
        if item.get('source_stamp') and item['source_stamp'] != before:
            raise ValueError('Source stamp changed')
        with Image.open(source) as src:
            src.seek(0)  # A thumbnail is not a photographed verso.
            frame_count = getattr(src, 'n_frames', 1)
            im = ImageOps.exif_transpose(src).convert('L')
        recipe = item['recipe']
        if 'inverted' in recipe.get('polarity', ''):
            im = ImageOps.invert(im)
        im = im.rotate(recipe.get('rotation', 0), expand=True)
        overview = im.copy()
        overview.thumbnail((args.overview_edge, args.overview_edge))
        jobs = [('overview', overview, None, 11)]
        for region in regions.get(item['id'], []):
            box = crop_box(region['box'], im.size)
            psm = region.get('psm', 11)
            if psm not in (6, 7, 11, 12, 13):
                raise ValueError('Unsupported PSM')
            jobs.append((region['label'], im.crop(box), box, psm))
        row = {'number': item['number'], 'object_id': item['id'],
               'filename': item['filename'], 'source': str(source), 'source_stamp': before,
               'native_dimensions': list(im.size), 'frame_count': frame_count,
               'recipe': recipe, 'runs': []}
        for label, image, box, psm in jobs:
            stem = output.with_name(f'{output.stem}-{item["number"]:02d}-{label}')
            image_path = destination(str(stem) + '.png')
            image.save(image_path)
            run = {'label': label, 'image_path': str(image_path), 'native_box': box,
                   'dimensions': list(image.size), 'psm': psm,
                   'interpretation': 'unreviewed OCR; confidence is not factual verification'}
            try:
                proc = subprocess.run(['tesseract', str(image_path), str(stem), '-l',
                                       args.languages, '--psm', str(psm), 'txt', 'tsv'],
                                      capture_output=True, text=True, timeout=args.timeout,
                                      env={**os.environ, 'OMP_THREAD_LIMIT': '2'})
                run.update(returncode=proc.returncode, stderr=proc.stderr[-2000:])
                if proc.returncode == 0:
                    run['raw_text'] = Path(str(stem)+'.txt').read_text()
                    run['words'] = [w for w in csv.DictReader(
                        io.StringIO(Path(str(stem)+'.tsv').read_text()), delimiter='\t')
                        if w.get('text', '').strip()]
            except subprocess.TimeoutExpired:
                run['error'] = f'timeout after {args.timeout}s; not a completed OCR result'
            row['runs'].append(run)
            print(item['number'], label, run.get('returncode', run.get('error')), flush=True)
        if before != stamp(source):
            raise ValueError('Source changed during OCR')
        row['source_stamp_unchanged'] = True
        result['results'].append(row)
        save(output.with_name(f'{output.stem}-{item["number"]:02d}-receipt.json'), row)
    save(output, result)


def packets(args):
    """Use reviewed observations, never raw OCR strings, to create search queries."""
    plan = read(args.plan)['images']
    ocr = {r['object_id']: r for r in read(args.ocr)['results']}
    observations = {r['object_id']: r for r in read(args.observations)['records']}
    registry = read(args.registry)
    menu = {s['id']: s for s in registry['sources']}
    result = {'metadata_adopted': False, 'source_registry': str(args.registry), 'records': []}
    for item in plan:
        ident = item['id']
        if ident not in ocr or ident not in observations:
            raise ValueError(f'Missing OCR or observations: {ident}')
        obs = observations[ident]
        queries = obs.get('queries', [])
        if len(queries) > 6 or len({q['source_id'] for q in queries}) > 4:
            raise ValueError('Budget exceeded: six queries, four sources per photograph')
        if queries and not any(q['kind'] == 'neutral' for q in queries):
            raise ValueError('Include a neutral query to test seeded hypotheses')
        searches = []
        for q in queries:
            if q['kind'] not in ('neutral', 'hypothesis', 'alternative'):
                raise ValueError('Unknown query kind')
            source = menu[q['source_id']]
            searches.append({**q, 'record_entry': source['url'],
                             'domains': source.get('domains', []),
                             'web_search_url': 'https://www.google.com/search?q='+quote_plus(
                                 q['query']+' site:'+source['domains'][0]),
                             'access_status': 'untested'})
        result['records'].append({
            'object_id': ident, 'filename': item['filename'], 'preview_path': item['preview_path'],
            'ocr_evidence': ocr[ident], 'observations': obs, 'searches': searches,
            'comparison_budget': 4,
            'research_instructions': (
                'Inspect overview and native crops before hypotheses. Raw OCR is not confirmed text. '
                'Use neutral and hypothesis queries; uncertain readings need alternatives. '
                'Open one usable record AND inspect its actual image before batching. '
                'Do not bypass access challenges. Record access_limited separately from no_match. '
                'Compare pose, geometry, base, graphic layout and context; record contradictions '
                'and occlusions. A different cast or pose rejects that candidate, not the species. '
                'Deduplicate mirrors by institution/accession. Do not identify faces. '
                'Separate location, event, event date, object creation date and exposure date. '
                'No source or catalogue writes. Stop after budget; unresolved is a valid result.')})
    save(args.output, result)


def validate_comparison(row):
    refs = row.get('references', [])
    ids = [r['id'] for r in refs]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate reference IDs')
    for r in refs:
        if r.get('image_inspected') and not (
                r.get('record_opened') and r.get('record_url') and
                r.get('image_path') and Path(r['image_path']).is_file() and
                r.get('inspection_note')):
            raise ValueError('Inspected reference requires record, local image and comparison note')
        if r.get('access_status') == 'access_limited' and r.get('verdict') == 'no_match':
            raise ValueError('Access-limited is not no-match')
    for field in ('location', 'event', 'photograph_date', 'event_date'):
        assessment = row['assessments'][field]
        linked = assessment.get('reference_ids', [])
        if any(i not in ids for i in linked):
            raise ValueError('Unknown evidence reference')
        if assessment.get('basis') == 'visual_match' and not any(
                r['id'] in linked and r.get('image_inspected') and r.get('matches') for r in refs):
            raise ValueError('Visual support requires inspected matching image')
        if field == 'photograph_date' and assessment.get('value') and not assessment.get('reasoning'):
            raise ValueError('Explain date inference; reference date is not exposure date')


def report(args):
    data = read(args.comparisons)
    if data.get('metadata_adopted') is not False:
        raise ValueError('Expected proposal-only comparisons')
    for row in data['records']:
        validate_comparison(row)
    out = destination(args.output)
    esc = html.escape
    parts = ['<!doctype html><meta charset="utf-8"><title>Photo clue research</title>',
             '<style>body{font:17px system-ui;max-width:1000px;margin:40px auto}pre{white-space:pre-wrap}</style>',
             '<h1>Photo clue research</h1><p>Review proposals only. No metadata adopted.</p>']
    for row in data['records']:
        parts.append('<h2>'+esc(row['object_id'])+'</h2><pre>'+esc(json.dumps(row, ensure_ascii=False, indent=2))+'</pre>')
    with out.open('x') as stream:
        stream.write('\n'.join(parts))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    o = sub.add_parser('ocr')
    for name in ('plan', 'output'):
        o.add_argument('--'+name, required=True)
    o.add_argument('--regions', help='Object ID -> [{label, box:[x0,y0,x1,y1], psm:11}]')
    o.add_argument('--limit', type=int, default=0)
    o.add_argument('--overview-edge', type=int, default=4500)
    o.add_argument('--timeout', type=int, default=90)
    o.add_argument('--languages', default='deu+eng')
    o.set_defaults(func=run_ocr)
    q = sub.add_parser('packets')
    for name in ('plan', 'ocr', 'observations', 'registry', 'output'):
        q.add_argument('--'+name, required=True)
    q.set_defaults(func=packets)
    r = sub.add_parser('report')
    r.add_argument('--comparisons', required=True)
    r.add_argument('--output', required=True)
    r.set_defaults(func=report)
    args = p.parse_args()
    if getattr(args, 'limit', 0) < 0 or getattr(args, 'timeout', 1) <= 0 or getattr(args, 'overview_edge', 1) <= 0:
        p.error('Limits must be positive (limit=0 means entire explicit plan)')
    try:
        args.func(args)
    except (ValueError, KeyError) as exc:
        p.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
