#!/usr/bin/env python3
"""Bounded similarity evidence only: no decisions, source writes, or accession."""
import csv
import json
import os
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import paragon_workbench as app
import retained_candidates as review
from retained_visual_review import image_feature, preview

W, U, S = review.ROOT, review.ARCHIVE, review.SOURCE
INDEX = Path('/home/vladimir/archive-visual-index/visual-index.sqlite')
BINARY = Path('/mnt/volume/iss-archive-work/kurt-schwarz/work/tools/czkawka_cli-12.0.1')
KEYS = ['phash-dct-64-v2', 'dhash-64-v2', 'ahash-64-v2']


def dump(name, data):
    (W / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def stamp(path):
    st = Path(path).stat()
    return [st.st_size, st.st_mtime_ns]


def main():
    app.mount_guard()
    controls = [W / 'retained-decisions.json', W / 'retained-proposed-drain.json',
                W / 'source-disposition.planned.csv', W / 'drain-list.csv',
                U / 'db-restore/manifests/source-disposition.csv',
                U / 'metadata/objects.csv', U / 'CATALOG.csv', INDEX]
    before = {str(p): stamp(p) for p in controls}
    con = sqlite3.connect(f'file:{W}/work/catalog.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    saved, cases = review.saved_reviews(con), review.cases(con)
    con.close()
    unresolved, retained = [], []
    for case in cases:
        states = {f['path']: f['state'] for f in saved.get(case['key'], {}).get('files', [])}
        for source in case['sources']:
            state = states.get(source['path'], 'unresolved')
            if state in ('unresolved', 'retain'):
                item = source | dict(case_key=case['key'], decision=state)
                (unresolved if state == 'unresolved' else retained).append(item)
    assert len(unresolved) == 702 and len(retained) == 3
    images = [s for s in unresolved if s['decoded']]
    documents = [s for s in unresolved if not s['decoded']]
    assert len(images) == 692 and len(documents) == 10
    features = json.loads((W / 'retained-visual-index-evidence.json').read_text())
    for source in images + retained:
        assert stamp(source['path']) == [source['size_bytes'], source['mtime_ns']]
        assert source['path'] in features
        source['features'] = features[source['path']]['features']
    oldrefs = {r['path']: r for r in review.evidence()['visual']['references']}
    refs = []
    representations = list(csv.DictReader((U / 'db-restore/manifests/representations.csv').open()))
    computed = 0
    for r in representations:
        path = str(U / r['access_path'])
        old = oldrefs.get(path)
        rotations = old.get('rotation_features') if old else None
        if not rotations:
            im = preview(path, 64)
            rotations = [image_feature(im.rotate(a, expand=True)) for a in (0, 90, 180, 270)]
            computed += 1
        refs.append(dict(path=path, source='archive', inventory=r['inventory'],
                         master_path=str(U / r['master_path']),
                         width=int(r['width_px']), height=int(r['height_px']),
                         features=rotations[0], rotation_features=rotations,
                         size_bytes=Path(path).stat().st_size,
                         mtime_ns=Path(path).stat().st_mtime_ns))
    inputs = images + retained + refs
    bypath = {r['path']: r for r in inputs}
    assert len(bypath) == len(inputs)
    print(json.dumps(dict(unresolved=len(unresolved), images=len(images), documents=len(documents),
                          source_index_reused=len(images), references=len(refs),
                          reference_features_reused=len(refs)-computed,
                          reference_features_computed=computed)), flush=True)

    # The index is read immutably; no WAL/SHM or historical root is modified.
    assert not Path(str(INDEX) + '-wal').stat().st_size
    index = sqlite3.connect(f'file:{INDEX}?mode=ro&immutable=1', uri=True)
    index.row_factory = sqlite3.Row
    sql = '''SELECT a.asset_id, r.root_path, a.relative_path, a.size_bytes, a.mtime_ns,
        a.width_px, a.height_px,
        max(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex END) AS ahash,
        max(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex END) AS dhash,
        max(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex END) AS phash
        FROM visual_assets a JOIN visual_roots r USING(root_id)
        JOIN visual_features f USING(asset_id)
        WHERE a.status='indexed' AND r.root_path='/media/vladimir/Elements'
        AND a.relative_path LIKE 'restore/archive/%'
        AND f.extractor_id IN ('ahash-64-v2','dhash-64-v2','phash-dct-64-v2')
        GROUP BY a.asset_id HAVING count(DISTINCT f.extractor_id)=3'''
    global_rows = [dict(r) for r in index.execute(sql)]
    index_total = index.execute('SELECT count(*) FROM visual_features WHERE extractor_id=?', (KEYS[0],)).fetchone()[0]
    index.close()
    pop = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)
    matrices = [np.array([list(bytes.fromhex(r[k])) for r in global_rows], dtype=np.uint8)
                for k in ('phash', 'dhash', 'ahash')]
    registered = review.evidence()['drain_registered']
    global_matches, extras = [], {}
    for source in images:
        d0 = pop[np.bitwise_xor(matrices[0], np.frombuffer(bytes.fromhex(source['features'][KEYS[0]]), dtype=np.uint8))].sum(axis=1)
        idxs = np.flatnonzero(d0 <= 6)
        matches = []
        for ix in idxs:
            raw = global_rows[int(ix)]
            p = str(Path(raw['root_path']) / raw['relative_path'])
            if p == source['path'] or p in registered:
                continue
            ds = [int(d0[ix])] + [int(pop[np.bitwise_xor(matrices[j][ix], np.frombuffer(bytes.fromhex(source['features'][KEYS[j]]), dtype=np.uint8))].sum()) for j in (1, 2)]
            if max(ds[1:]) > 10 or sum(ds) > 24:
                continue
            matches.append(dict(path=p, distance=ds, width=raw['width_px'], height=raw['height_px'],
                                size_bytes=raw['size_bytes'], mtime_ns=raw['mtime_ns']))
        matches.sort(key=lambda r: (sum(r['distance']), -(r['width']*r['height'])))
        # Record all indexed leads, but only feed bounded live external leads to Czkawka.
        if matches:
            global_matches.append(dict(source=source['path'], matches=matches))
        for r in [m for m in matches if m['path'] not in bypath][:5]:
            p = Path(r['path'])
            if not p.is_relative_to(S) or not p.is_file() or stamp(p) != [r['size_bytes'], r['mtime_ns']]:
                continue
            if p.suffix.lower() not in ('.jpg', '.jpeg', '.tif', '.tiff', '.png'):
                continue
            extras[str(p)] = r | dict(source='other_source_index', decision='not_assessed')
    inputs.extend(extras.values())
    # Existing drain paths stay out of this search so a listed copy is not proposed as its own coverage.
    assert not set(registered).intersection(r['path'] for r in inputs)
    print(json.dumps(dict(global_index_images=index_total, protected_archive_index_images=len(global_rows),
                          sources_with_index_leads=len(global_matches), additional_bounded_images=len(extras),
                          czkawka_inputs=len(inputs))), flush=True)
    snapshot = dict(created_at=datetime.now(timezone.utc).isoformat(), input_stamps=before,
                    unresolved=unresolved, explicitly_retained=retained, references=refs,
                    additional_index_candidates=list(extras.values()), documents=documents,
                    index_path=str(INDEX), index_image_count=index_total,
                    protected_archive_index_count=len(global_rows), global_index_matches=global_matches,
                    source_features_reused=len(images), reference_features_reused=len(refs)-computed,
                    reference_features_computed=computed, decisions_changed=False, deletion_authorized=False)
    dump('remaining-similarity-inputs.json', snapshot)
    env = os.environ | {'CZKAWKA_CACHE_PATH': str(W), 'CZKAWKA_CONFIG_PATH': str(W), 'RUST_LOG': 'info'}
    runs = []
    for alg in ('Gradient', 'Mean', 'Blockhash'):
        output = W / f'remaining-czkawka-{alg.lower()}.json'
        assert not output.exists(), f'Reuse existing completed result: {output}'
        geometry = 'mirror-flip-rotate90' if alg == 'Gradient' else 'off'
        command = [str(BINARY), 'image', '-x', 'jpg,jpeg,tif,tiff,png', '-m', '1',
                   '-g', alg, '-c', '16', '-s', '20', '--geometric-invariance', geometry,
                   '-p', str(output), '-N', '-W', '-T', '4', '-D', 'NONE']
        for item in inputs:
            command += ['-d', item['path']]
        print(f'Starting Czkawka {alg}: {len(inputs)} exact files', flush=True)
        with (W / f'remaining-czkawka-{alg.lower()}.log').open('x') as log:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        assert result.returncode == 0, (alg, result.returncode)
        groups = json.loads(output.read_text())
        runs.append(dict(algorithm=alg, hash_size=16, max_difference=20,
                         geometric_invariance=geometry, output=str(output), groups=len(groups)))
        print(f'Finished Czkawka {alg}: {len(groups)} groups', flush=True)
    for p, old in before.items():
        assert stamp(p) == old, f'Control changed: {p}'
    for item in inputs:
        assert stamp(item['path']) == [item['size_bytes'], item['mtime_ns']], item['path']
    app.mount_guard()
    dump('remaining-czkawka-runs.json', dict(completed_at=datetime.now(timezone.utc).isoformat(),
         binary=str(BINARY), input_count=len(inputs), runs=runs, unchanged_inputs=True,
         unchanged_controls=True, decisions_changed=False, deletion_authorized=False))


if __name__ == '__main__':
    main()
