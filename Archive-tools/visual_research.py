#!/usr/bin/env python3
"""Disposable metadata-research previews and visual neighbours; no catalogue writes.

Inspired by visual_knn.py draft 66a4467. Uses the live review contract and local
selected-Master catalogue adapter. No metadata voting or identity assignment.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time

from archive_common import atomic_write, elements_guard, file_lock, require, work_root
from catalogues import EmptyIndex, read_unit
from matching import stamp
from workbench import Workbench

VERSION = '1'
PROFILE = '/usr/share/color/icc/colord/sRGB.icc'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def output(root, relative):
    p = root / relative
    require(p.resolve().is_relative_to(root) and not p.is_symlink(), 'Output escapes work root')
    return p


def save(root, name, value):
    atomic_write(output(root, name), encoded(value) + b'\n')


def load(root):
    return json.loads((root / 'manifest.json').read_text())


def progress(root, phase, **values):
    save(root, 'progress.json', {'phase': phase, 'updated_at': time.time(), **values})
    print(json.dumps({'phase': phase, **values}), flush=True)


def freeze(review, expected):
    app = Workbench(review, 'read-only metadata research')
    rows, revision = app.read_decisions()
    items = []
    for oid, obj in app.objects.items():
        row = rows[oid]
        if app.scope_context(oid, row)['kind'] != 'proposed_new_object':
            continue
        chosen = app.candidates[oid].get(row['selected_candidate_id'])
        basis = 'existing review selection'
        if chosen is None:
            eligible = [c for c in obj['candidates'] if not c.get('reference_object_id')
                        and c['eligible'] and c['complete']]
            require(len(eligible) == 1, f'Ambiguous research view: {oid}')
            chosen = eligible[0]
            basis = 'sole eligible source for research; no Master decision made'
        require(chosen and not chosen.get('reference_object_id'), f'No selected source: {oid}')
        require(chosen['eligible'] and chosen['complete'] and not app.problems(chosen), f'Unavailable source: {oid}')
        require(len(chosen['members']) == 1, 'Multi-member objects need explicit view selection')
        p = Path(chosen['members'][0]['path'])
        items.append({'id': oid, 'side': 'candidate', 'title': obj.get('title', p.name),
                      'path': str(p), 'stamp': stamp(p), 'candidate_id': chosen['id'],
                      'view_basis': basis,
                      'metadata': obj.get('metadata', {}), 'candidate_metadata': chosen.get('metadata', {}),
                      'prior_evidence': chosen.get('prior_review_evidence', []),
                      'previous_proposals': obj.get('previous_proposals', []), 'decision': row})
    require(len(items) == expected, f'Candidate count changed: expected {expected}, found {len(items)}')
    require(app.read_decisions()[1] == revision, 'Review changed during snapshot')
    return items, revision


def make_preview(root, row, override):
    p = Path(row['path'])
    elements_guard(p)
    require(stamp(p) == row['stamp'], f'Source changed: {p}')
    key = digest({'path': str(p), 'stamp': row['stamp'],
                  'override': {k: override[k] for k in ('invert', 'rotate') if override.get(k)}, 'recipe': VERSION})
    relative = f'previews/{key}.jpg'
    target = output(root, relative)
    recipe = {'edge': 1024, 'upscale': False, 'page': 0, 'orientation': 'EXIF autorotate',
              'colour': 'embedded profile to sRGB, sRGB fallback',
              'polarity': 'inverted research view' if override.get('invert') else 'unchanged',
              'rotation': override.get('rotate', 0), 'layers': 'primary composite only; not certified',
              'visual_checked': bool(override.get('checked') and override.get('source_stamp') == row['stamp']),
              'visual_note': override.get('note', '')}
    if not target.exists():
        tmp = output(root, f'previews/.{key}.jpg')
        require(not tmp.exists(), f'Existing temporary preview needs inspection: {tmp}')
        try:
            if override.get('invert') or override.get('rotate'):
                base = make_preview(root, row, {})
                shutil.copyfile(root / base['preview'], tmp)
            else:
                result = subprocess.run(['vips', 'thumbnail', str(p), str(tmp) + '[Q=92,strip]', '1024',
                                         '--height', '1024', '--size', 'down', '--fail-on', 'warning',
                                         '--import-profile', PROFILE, '--export-profile', PROFILE],
                                        capture_output=True, text=True, timeout=180,
                                        env={**os.environ, 'VIPS_CONCURRENCY': '1'})
                require(result.returncode == 0, result.stderr[-1200:])
            from PIL import Image, ImageOps
            with Image.open(tmp) as im:
                im.load()
                require(max(im.size) <= 1024 and min(im.size) > 0, 'Invalid preview size')
                if override.get('invert') or override.get('rotate'):
                    im = im.convert('RGB')
                    if override.get('invert'):
                        im = ImageOps.invert(im)
                    if override.get('rotate'):
                        im = im.rotate(override['rotate'], expand=True)
                    im.save(tmp, quality=92)
            require(stamp(p) == row['stamp'], f'Source changed during preview: {p}')
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    from PIL import Image
    with Image.open(target) as im:
        im.load()
        size = list(im.size)
    return {**row, 'preview': relative, 'preview_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'preview_size': size, 'recipe': recipe, 'status': 'ok'}


def prepare(args):
    root = args.work_root
    require(not root.is_relative_to(args.review.resolve()) and not args.review.resolve().is_relative_to(root),
            'Research work root must be separate from the live review')
    progress(root, 'reading_catalogues')
    items, revision = freeze(args.review, args.expected)
    references, receipts = [], []
    for unit in args.unit:
        targets, receipt = read_unit(unit, EmptyIndex())
        receipts.append(receipt)
        progress(root, 'reading_catalogues', unit=str(unit), references=len(targets))
        for t in targets:
            p = Path(t['preview_path'] or t['path'])
            references.append({'id': t['id'], 'side': 'reference', 'path': str(p), 'stamp': stamp(p),
                               'master_path': t['path'], 'master_stamp': t['stamp'],
                               'title': t['row'].get('title', t['record_key']),
                               'metadata': t['metadata'], 'catalogue_record': t['row']})
    overrides = json.loads(args.views.read_text()) if args.views else {}
    require(set(overrides) <= {x['id'] for x in items + references}, 'Unknown view override ID')
    output(root, 'previews').mkdir(exist_ok=True)
    snapshot = {'format': 'archive-visual-research-1', 'version': VERSION, 'review': str(args.review),
                'revision': revision, 'units': receipts, 'candidates': items, 'references': references,
                'created_at': time.time(), 'metadata_authority': False, 'accession_authorized': False}
    save(root, 'input-snapshot.json', snapshot)
    prepared = []
    def one(row):
        try:
            return make_preview(root, row, overrides.get(row['id'], {}))
        except Exception as e:
            return {**row, 'status': 'error', 'error': str(e)}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(one, items + references):
            prepared.append(row)
            if len(prepared) == len(items):
                sheets(root, prepared)
                save(root, 'candidate-previews.json', prepared)
            if len(prepared) % 50 == 0:
                progress(root, 'previews', done=len(prepared), total=len(items)+len(references))
    snapshot['candidates'] = [r for r in prepared if r['side'] == 'candidate']
    snapshot['references'] = [r for r in prepared if r['side'] == 'reference']
    save(root, 'manifest.json', snapshot)
    sheets(root, snapshot['candidates'])
    errors = [r for r in prepared if r['status'] != 'ok']
    progress(root, 'previews_complete', candidates=len(items), references=len(references), errors=len(errors))
    require(not errors, f'{len(errors)} preview errors; see manifest.json before embedding')


def sheets(root, rows):
    from PIL import Image, ImageDraw
    good = [r for r in rows if r['status'] == 'ok']
    for start in range(0, len(good), 30):
        page = Image.new('RGB', (1500, 1200), '#eeeeee')
        draw = ImageDraw.Draw(page)
        for n, row in enumerate(good[start:start+30]):
            x, y = (n % 6)*250, (n//6)*240
            with Image.open(root / row['preview']) as im:
                im.thumbnail((244, 205))
                page.paste(im, (x+(244-im.width)//2, y))
            draw.text((x+3, y+207), f'{start+n+1}: {Path(row["path"]).name}'[:37], fill='black')
            draw.text((x+3, y+222), row['id'][-16:], fill='black')
        buffer = io.BytesIO()
        page.save(buffer, format='JPEG', quality=92)
        atomic_write(output(root, f'contact-{start//30+1:02}.jpg'), buffer.getvalue())


def views(args):
    root = args.work_root
    manifest = load(root)
    overrides = json.loads(args.views.read_text())
    require(set(overrides) <= {r['id'] for r in manifest['candidates']}, 'Unknown candidate view ID')
    for row in manifest['candidates']:
        if row['id'] in overrides:
            row.update(make_preview(root, row, overrides[row['id']]))
    save(root, 'manifest.json', manifest)
    sheets(root, manifest['candidates'])
    progress(root, 'views_complete', checked=sum(r['recipe']['visual_checked'] for r in manifest['candidates']))


def embed(args):
    root = args.work_root
    os.environ['HF_HOME'] = str(output(root, 'models'))
    os.environ['HF_XET_CACHE'] = str(output(root, 'models/xet'))
    import importlib.metadata
    import numpy as np
    import open_clip
    import torch
    from PIL import Image
    manifest = load(root)
    rows = manifest['candidates'] + manifest['references']
    require(all(r['status'] == 'ok' for r in rows), 'Resolve preview errors first')
    require(all(r['recipe']['visual_checked'] for r in manifest['candidates']),
            'Inspect candidate contact sheets and record view checks before embedding')
    torch.set_num_threads(args.threads)
    # Phenom II lacks the ISA required by oneDNN primitives; use native CPU ops.
    torch.backends.mkldnn.enabled = False
    model, _, transform = open_clip.create_model_and_transforms(
        'ViT-B-32', pretrained='laion2b_s34b_b79k', device='cpu', cache_dir=str(output(root, 'models')))
    model.eval()
    config = {'model': 'ViT-B-32', 'pretrained': 'laion2b_s34b_b79k',
              'preprocess': str(transform), 'torch': torch.__version__,
              'open_clip': importlib.metadata.version('open_clip_torch'), 'recipe': VERSION}
    config['mkldnn'] = False
    config['weight_files'] = [str(p.relative_to(root)) for p in sorted((root / 'models').rglob('*.safetensors'))]
    output(root, 'embeddings').mkdir(exist_ok=True)
    keys = []
    start = time.monotonic()
    for offset in range(0, len(rows), args.batch):
        chunk = rows[offset:offset+args.batch]
        missing = []
        for row in chunk:
            key = digest({'config': config, 'image': row['preview_sha256']})
            keys.append(key)
            if not output(root, f'embeddings/{key}.npy').exists():
                p = root / row['preview']
                require(hashlib.sha256(p.read_bytes()).hexdigest() == row['preview_sha256'], 'Preview changed')
                with Image.open(p) as im:
                    missing.append((key, transform(im.convert('RGB'))))
        if missing:
            with torch.inference_mode():
                vectors = model.encode_image(torch.stack([im for _, im in missing])).float()
                vectors = vectors / vectors.norm(dim=-1, keepdim=True).clamp(min=1e-12)
            for (key, _), vector in zip(missing, vectors.numpy()):
                buffer = io.BytesIO()
                np.save(buffer, vector, allow_pickle=False)
                atomic_write(output(root, f'embeddings/{key}.npy'), buffer.getvalue())
        if offset % (args.batch*10) == 0:
            progress(root, 'embedding', done=min(offset+args.batch,len(rows)), total=len(rows),
                     elapsed_seconds=round(time.monotonic()-start,1))
    save(root, 'embedding-manifest.json', {'config': config, 'manifest_digest': digest(manifest),
                                         'items': [{'id': row['id'], 'key': key} for row,key in zip(rows,keys)]})
    progress(root, 'embedding_complete', total=len(rows))


def neighbours(vector, matrix, rows, k, exclude=None):
    import numpy as np
    scores = matrix @ vector
    order = np.argsort(-scores, kind='stable')
    return [{'id': rows[i]['id'], 'similarity': float(scores[i])}
            for i in order if rows[i]['id'] != exclude][:k]


def report(args):
    import numpy as np
    root = args.work_root
    m = load(root)
    e = json.loads((root / 'embedding-manifest.json').read_text())
    require(e['manifest_digest'] == digest(m), 'Embedding manifest is stale')
    by_id = {r['id']: r for r in m['candidates'] + m['references']}
    vectors = {r['id']: np.load(root / f'embeddings/{r["key"]}.npy', allow_pickle=False) for r in e['items']}
    ref = np.stack([vectors[r['id']] for r in m['references']])
    new = np.stack([vectors[r['id']] for r in m['candidates']])
    require(np.isfinite(ref).all() and np.isfinite(new).all(), 'Nonfinite embeddings')
    records = [{'id': r['id'], 'references': neighbours(vectors[r['id']], ref, m['references'], args.k),
                'candidates': neighbours(vectors[r['id']], new, m['candidates'], args.k, r['id'])}
               for r in m['candidates']]
    save(root, 'neighbours.json', {'model': e['config'], 'manifest_digest': digest(m), 'items': records,
                                  'meaning': 'Visual research leads only; similarity is not identity or metadata confidence'})
    def esc(value):
        return html.escape(str(value), quote=True)
    def card(row, score=None):
        return ('<figure><a href="'+esc(row['preview'])+'"><img loading="lazy" src="'+esc(row['preview'])+'"></a>'
                '<figcaption><b>'+esc(row['title'])+'</b><br>'+esc(row['id'])+
                (f'<br>Similarity {score:.3f}' if score is not None else '')+
                '<details><summary>Source and evidence</summary><pre>'+esc(json.dumps(row,ensure_ascii=False,indent=2))+
                '</pre></details></figcaption></figure>')
    parts = ['<!doctype html><meta charset="utf-8"><title>Visual metadata research</title><style>'
             'body{font:16px system-ui;margin:24px;background:#f4f3ee;color:#222}section{border-top:2px solid #888;margin-top:32px}'
             '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}'
             'figure{margin:0;padding:12px;background:white;overflow-wrap:anywhere}img{width:100%;height:210px;object-fit:contain}'
             'pre{white-space:pre-wrap;font-size:12px}details{margin-top:12px}</style>'
             '<h1>Visual metadata research</h1><p>'+str(len(records))+' provisional candidates; '+str(len(m['references']))+
             ' local reference photographs. Similarity provides research leads, not identity or metadata assignments. '
             'No automatic date, place, person or collection transfer. Primary-image previews do not certify TIFF layers.</p>']
    for n, result in enumerate(records, 1):
        row = by_id[result['id']]
        parts += [f'<section><h2>{n}. {esc(row["title"])}</h2><div class="grid">',card(row),'</div><h3>Archive neighbours</h3><div class="grid">']
        parts += [card(by_id[x['id']], x['similarity']) for x in result['references']]
        parts += ['</div><h3>Other provisional candidates</h3><div class="grid">']
        parts += [card(by_id[x['id']], x['similarity']) for x in result['candidates']]
        parts += ['</div></section>']
    atomic_write(output(root,'report.html'), ''.join(parts).encode())
    progress(root, 'complete', candidates=len(records), references=len(m['references']), report=str(root/'report.html'))


def run(args):
    if args.views:
        views(args)
    embed(args)
    report(args)


def evaluation(args):
    from visual_evaluation import evaluate
    evaluate(args)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work-root', type=Path, required=True)
    sub = p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--review',type=Path,required=True)
    a.add_argument('--unit',type=Path,action='append',required=True);a.add_argument('--expected',type=int,default=111)
    a.add_argument('--views',type=Path);a.add_argument('--workers',type=int,default=2);a.set_defaults(func=prepare)
    a=sub.add_parser('views');a.add_argument('--views',type=Path,required=True);a.set_defaults(func=views)
    a=sub.add_parser('embed');a.add_argument('--threads',type=int,default=2);a.add_argument('--batch',type=int,default=8);a.set_defaults(func=embed)
    a=sub.add_parser('report');a.add_argument('--k',type=int,default=6);a.set_defaults(func=report)
    a=sub.add_parser('evaluate');a.add_argument('--sample',type=int,default=400)
    a.add_argument('--seed',type=int,default=47);a.add_argument('--k',type=int,default=12)
    a.add_argument('--series-window',type=int,default=20);a.add_argument('--min-similarity',type=float,default=.5)
    a.add_argument('--min-support',type=float,default=.34);a.add_argument('--temperature',type=float,default=.05)
    a.add_argument('--name');a.set_defaults(func=evaluation)
    a=sub.add_parser('run');a.add_argument('--threads',type=int,default=2);a.add_argument('--batch',type=int,default=8)
    a.add_argument('--k',type=int,default=6);a.add_argument('--background',action='store_true')
    a.add_argument('--views',type=Path);a.set_defaults(func=run)
    args=p.parse_args();args.work_root=work_root(args.work_root)
    require(args.work_root.is_relative_to(Path('/mnt/volume/iss-archive-work')), 'Use the approved work area')
    if getattr(args, 'background', False):
        log = output(args.work_root, 'run.log')
        require(not log.exists() or log.stat().st_nlink == 1, 'Refusing hard-linked run log')
        with log.open('ab') as handle:
            proc = subprocess.Popen([sys.executable, '-B', '-u', str(Path(__file__).resolve()),
                                     *[x for x in sys.argv[1:] if x != '--background']],
                                    stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                                    start_new_session=True)
        save(args.work_root, 'launch.json', {'pid': proc.pid, 'started_at': time.time(),
                                           'log': str(log), 'status': 'launched; completion not yet verified'})
        print(f'Launched PID {proc.pid}; inspect progress.json and run.log for outcome')
        return
    print('Waiting for exclusive research lock', flush=True)
    with file_lock(output(args.work_root,'.research.lock')):
        try:
            args.func(args)
        except BaseException as e:
            if args.command == 'evaluate':
                save(args.work_root, 'evaluation-status.json', {'phase': 'failed', 'error': str(e)})
            else:
                progress(args.work_root, 'failed', command=args.command, error=str(e))
            raise


if __name__ == '__main__':
    main()
