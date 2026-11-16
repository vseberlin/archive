"""Read-only comparison of current unresolved paths with current Paragon views.

Reuses stamped existing hash evidence. Writes only the two named analysis files
beside this script; never writes decisions, databases, registers or archive files.
The diagnostic thresholds here do not change Master or redundancy rules.
"""
import csv
import json
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import retained_candidates as rc

ROOT, ARCHIVE = rc.ROOT, rc.ARCHIVE
HASHES = ('phash-dct-64-v2', 'dhash-64-v2', 'ahash-64-v2')


def stamp(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns]


def compare(source, target):
    variants = target.get('rotation_features') or [target['features']]
    choices = []
    for i, feature in enumerate(variants):
        distances = [(int(source['features'][k], 16) ^ int(feature[k], 16)).bit_count()
                     for k in HASHES]
        w, h = target['width'], target['height']
        if i % 2:
            w, h = h, w
        a, b = source['width'] / source['height'], w / h
        aspect = abs(a - b) / max(a, b)
        p, d, a = distances
        close = p <= 6 and d <= 8 and a <= 8 and sum(distances) <= 18 and aspect <= .02
        near = p <= 12 and d <= 16 and a <= 16 and sum(distances) <= 36
        choices.append(dict(distance=distances, sum=sum(distances), rotation=i*90,
                            aspect_relative_difference=aspect, close=close, near=near))
    # Prefer an actual threshold-passing orientation over a smaller total which
    # fails one of the individual bounds.
    best = min(choices, key=lambda x: (not x['close'], not x['near'], x['sum'], x['distance']))
    return dict(target=target['path'], inventory=target.get('inventory', ''),
                master_path=target.get('master_path', ''),
                target_dimensions=[target['width'], target['height']],
                target_pixel_ratio=target['width']*target['height']/(source['width']*source['height']),
                **best)


def main():
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS',
                                     '--target', '/media/vladimir/Elements'], text=True).strip()
    device, target, fs, opts = mount.split()
    assert (device, target, fs) == ('/dev/sdf1', '/media/vladimir/Elements', 'ntfs3')
    assert 'ro' in opts.split(',') and 'rw' not in opts.split(',')
    controls = [ROOT/'work/catalog.sqlite', ROOT/'retained-decisions.json',
                ROOT/'retained-proposed-drain.json', ROOT/'source-disposition.planned.csv',
                ARCHIVE/'db-restore/manifests/source-disposition.csv', ARCHIVE/'CATALOG.csv',
                ARCHIVE/'metadata/objects.csv', ROOT/'remaining-similarity-inputs.json',
                ROOT/'remaining-czkawka-hashes.json', ROOT/'remaining-similarity-report.json']
    before = {str(p): stamp(p) for p in controls}
    con = sqlite3.connect(f'file:{ROOT}/work/catalog.sqlite?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('BEGIN')
    reviews = rc.saved_reviews(con)
    readable = rc.readjson(ROOT/'retained-decisions.json')
    assert rc.readable_decisions(con) == readable
    assert rc.drain_proposals(con) == rc.readjson(ROOT/'retained-proposed-drain.json')
    assert (ROOT/'source-disposition.planned.csv').read_bytes() == (ARCHIVE/'db-restore/manifests/source-disposition.csv').read_bytes()
    ev = rc.evidence()
    pending = []
    for case in rc.cases(con):
        states = {f['path']: f['state'] for f in reviews.get(case['key'], {}).get('files', [])}
        for asset in case['sources']:
            if asset['path'] not in ev['drain_registered'] and states.get(asset['path'], 'unresolved') == 'unresolved':
                pending.append(asset | dict(case_key=case['key'], case_label=case['label'], case_kind=case['kind']))
    assert len({x['path'] for x in pending}) == len(pending)
    data = rc.readjson(ROOT/'remaining-similarity-inputs.json')
    cached = {x['path']: x for x in data['unresolved'] + data['explicitly_retained']}
    references = data['references']
    catalogue = list(csv.DictReader((ARCHIVE/'CATALOG.csv').open()))
    current_refs = {str(ARCHIVE/r['final_relative_path']) for r in catalogue if r['file_role'] == 'access_derivative'}
    assert current_refs == {r['path'] for r in references}
    masters = [r for r in catalogue if r['file_role'] in ('preservation_master', 'preservation_surrogate')]
    master_rows = {str(ARCHIVE/r['final_relative_path']):r for r in masters}
    exact_final = {r['final_sha256'].removeprefix('sha256:') for r in masters}
    exact_source = {r['source_sha256'].removeprefix('sha256:') for r in masters if r['source_sha256']}
    all_catalogue_hashes = {r[k].removeprefix('sha256:') for r in catalogue
                           for k in ('final_sha256', 'source_sha256') if r[k]}
    images, documents = [], []
    for asset in pending:
        assert stamp(asset['path']) == [asset['size_bytes'], asset['mtime_ns']]
        if asset['decoded']:
            old = cached[asset['path']]
            assert stamp(asset['path']) == [old['size_bytes'], old['mtime_ns']]
            images.append(asset | dict(features=old['features']))
        else:
            documents.append(asset)
    for ref in references:
        assert stamp(ref['path']) == [ref['size_bytes'], ref['mtime_ns']]
    native_raw = rc.readjson(ROOT/'remaining-czkawka-hashes.json')
    native = {}
    relevant = {r['path'] for r in references + images}
    for algorithm, entries in native_raw.items():
        native[algorithm] = {}
        for row in entries:
            if row['path'] in relevant:
                st = Path(row['path']).stat()
                assert st.st_size == row['size_bytes'] and int(st.st_mtime) == row['modified_seconds']
                native[algorithm][row['path']] = [int(h, 16) for h in row['hashes']]
    old_report = {r['path']: r for r in rc.readjson(ROOT/'remaining-similarity-report.json')['sources']}
    rows = []
    for source in images:
        matches = []
        for ref in references:
            match = compare(source, ref)
            master = master_rows[match['master_path']]
            match['master_role'] = master['file_role']
            match['master_condition'] = master['condition']
            match['master_dimensions'] = [int(master['width']), int(master['height'])]
            match['master_pixel_ratio'] = int(master['width'])*int(master['height'])/(source['width']*source['height'])
            native_distances = {}
            for algorithm, entries in native.items():
                if source['path'] in entries and ref['path'] in entries:
                    native_distances[algorithm] = min((a^b).bit_count() for a in entries[source['path']] for b in entries[ref['path']])
            match['native_256_distances'] = native_distances
            match['native_algorithms_within_40'] = sum(x <= 40 for x in native_distances.values())
            matches.append(match)
        matches.sort(key=lambda x: (not x['close'], not x['near'], x['sum'], x['target']))
        best = matches[0]
        bucket = 'close' if best['close'] else 'near' if best['near'] else 'no_close_hash_match'
        candidates = [m for m in matches if m['near'] or m['native_algorithms_within_40'] >= 2]
        original = old_report.get(source['path'], {})
        rows.append(dict(path=source['path'], sha256=source['sha256'], case_key=source['case_key'],
                         case_label=source['case_label'], case_kind=source['case_kind'],
                         dimensions=[source['width'], source['height']],
                         bucket=bucket, best=best, candidates=candidates, nearest_three=matches[:3],
                         previous_category=original.get('category'),
                         exact_final_master_match=source['sha256'].removeprefix('sha256:') in exact_final,
                         exact_accessioned_source_match=source['sha256'].removeprefix('sha256:') in exact_source))
    # Saved source Masters are comparison evidence, not accessioned references.
    selected = []
    for review in reviews.values():
        p = review.get('selected_master_path')
        if review.get('source_version_pair') and p:
            assert stamp(p) == [cached[p]['size_bytes'], cached[p]['mtime_ns']]
            selected.append(cached[p] | dict(inventory=review.get('inventory', '')))
    saved_matches = []
    for source in images:
        matches = [compare(source, t) for t in selected]
        close = [x for x in matches if x['close']]
        if close:
            saved_matches.append(dict(path=source['path'], comparisons=close))
    internal = []
    for i, source in enumerate(images):
        for other in images[i+1:]:
            match = compare(source, other)
            if match['close']:
                internal.append(dict(path=source['path'], **match))
    after = {str(p): stamp(p) for p in controls}
    assert before == after, 'Authoritative inputs changed during comparison; rerun analysis.'
    counts = Counter(r['bucket'] for r in rows)
    summary = dict(unresolved_paths=len(pending), unresolved_cases=len({r['case_key'] for r in pending}),
                   images=len(images), documents=len(documents), accessioned_views=len(references),
                   compared_pairs=len(images)*len(references), **counts,
                   exact_final_master_matches=sum(r['exact_final_master_match'] for r in rows),
                   exact_accessioned_source_matches=sum(r['exact_accessioned_source_match'] for r in rows),
                   exact_catalogued_file_or_source_matches=sum(x['sha256'].removeprefix('sha256:') in all_catalogue_hashes for x in pending),
                   repeated_remaining_sha256=len(pending)-len({x['sha256'] for x in pending}),
                   native_only_leads=sum(r['bucket']=='no_close_hash_match' and bool(r['candidates']) for r in rows),
                   saved_pair_masters=len(selected), remaining_close_to_saved_masters=len(saved_matches),
                   remaining_close_pairs=len(internal),
                   remaining_paths_in_close_pairs=len({p for x in internal for p in (x['path'],x['target'])}),
                   close_to_preservation_master=sum(r['bucket']=='close' and r['best']['master_role']=='preservation_master' for r in rows),
                   close_to_manufactured_surrogate=sum(r['bucket']=='close' and r['best']['master_role']=='preservation_surrogate' for r in rows),
                   close_source_more_than_twice_master_pixels=sum(r['bucket']=='close' and r['best']['master_pixel_ratio']<.5 for r in rows),
                   close_native_hashes_unavailable=sum(r['bucket']=='close' and not r['best']['native_256_distances'] for r in rows))
    output = dict(checked_at=datetime.now(timezone.utc).isoformat(), summary=summary,
                  scope='Current unresolved Paragon sources versus every current Paragon accessioned image view. Other accessioned units were not searched.',
                  method=dict(hash_order=list(HASHES), close='p<=6,d<=8,a<=8,total<=18,aspect difference<=2%',
                              near='p<=12,d<=16,a<=16,total<=36, excluding close',
                              native='Minimum stored Czkawka 256-bit Hamming distances, each algorithm independent. Gradient variants include rotations and flips.',
                              references='Existing access images representing the selected accessioned Masters; all current CATALOG access paths covered.',
                              source_hashes_reused=True, all_used_cache_stamps_verified=True,
                              limitation='Similarity thresholds identify review candidates only; do not prove matching coverage, quality, metadata or redundancy. Secondary source-to-source comparisons use the stored orientation only.'),
                  protected_mount=mount, input_stamps=before, authoritative_inputs_unchanged=True,
                  sqlite_readable_decisions_agree=True, work_final_registers_agree=True,
                  decisions_changed=False, accession_authorized=False, deletion_authorized=False,
                  visual_spot_checks=[dict(inventory=label, finding='Same depicted image and main content visible in paired workbench previews; minor frame/tone differences. No pixel-level identity or redundancy decision made.')
                                      for label in ('VLV-1132-6','VLV-1132-7')],
                  sources=rows, documents=documents, close_to_saved_source_masters=saved_matches,
                  close_remaining_pairs=internal)
    (ROOT/'accessioned-similarity-recheck.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    md = ['# Current remaining Paragon images compared with accessioned images', '',
          f"Checked: {output['checked_at']}", '',
          'Read-only analysis. No Master choices, metadata, file states, source registers or archive files changed.', '',
          '## Current scope', '', *[f'- {k}: {v}' for k,v in summary.items()], '',
          '## Interpretation', '',
          'Close and near are discovery buckets, not redundancy decisions. The remaining files have different stored SHA-256 hashes from the accessioned Masters and their original source bindings. Reprocessed, resized or cropped copies can still depict the same image.', '',
          'All 278 current Paragon access views are represented, including the later accessions. Existing source/reference hashes were reused only after checking unchanged file stamps. The comparison is exhaustive across current unresolved images and these views, including four reference rotations. Other accessioned units were not searched.', '',
          'The live VLV-1132 screen had Show already recorded copies and their evidence enabled: seven registered copies were visible along with six unresolved files. The comparison initially showed the whole carrier; the matching member is available separately. This contributes to the appearance of repeated work.', '',
          'All six unresolved VLV-1132 files have close matches to their individual accessioned members. These were already classified similarity_requires_review in the earlier report. A saved carrier Retain content review did not resolve these six individual file versions. The old strong-match gate also required all three native algorithms, which these pairs do not pass.', '',
          'The paired VLV-1132-6 drawing and VLV-1132-7 photograph were visually inspected in the workbench on 20 September 2026. Each depicts the same image as the corresponding accessioned member, with minor frame/tone differences. This was not a pixel-level identity test or a saved redundancy decision.', '',
          '18 of the close candidates match records explicitly labelled manufactured preservation surrogate. A larger file or TIFF extension does not establish an original. Compare their provenance and actual quality before rejecting a possible better Master. Two other close candidates (IMG_20190815_141337.tif and IMG_20190815_153848.tif) have about 36 times the pixel count of the accessioned views and selected Masters: their retention for Master review is meaningful.', '',
          'The native hashes are unavailable for seven close source candidates (previously unsupported palette TIFF inputs); missing native results must not be read as dissimilarity. Their cached pHash/dHash/aHash evidence is present.', '',
          '## Close candidates', '',
          'Distances below are pHash/dHash/aHash out of 64 bits; smaller is closer. Archive/source pixel ratio is a size comparison, not proof of resolution or coverage.', '',
          '| Source | Accessioned match | Master role | Hash distances | Pixel ratio | Native Gradient/Mean/Blockhash | Workbench |',
          '|---|---|---|---|---|---|---|']
    for row in sorted(rows, key=lambda r:(r['bucket']!='close',r['best']['sum'],r['path'])):
        if row['bucket'] != 'close':
            continue
        b=row['best']; n=b['native_256_distances']
        source=str(Path(row['path']).relative_to(rc.SOURCE))
        url='http://127.0.0.1:8766/retained/#'+quote(row['case_key'],safe='')
        md.append(f"| {source} | {b['inventory']} | {b['master_role']} | {' / '.join(map(str,b['distance']))} | {b['target_pixel_ratio']:.3f} | {' / '.join(str(n.get(k,'n/a')) for k in ('Gradient','Mean','Blockhash'))} | [Open]({url}) |")
    md += ['', 'The JSON companion records every remaining source, exact target paths, the nearest three comparisons, all close/near or two-native-algorithm candidates, document exclusions, and secondary matches to saved source Masters and other unresolved sources.', '',
           'Next bounded action: review these direct source-to-accessioned-member pairs for identical content and sufficient preserved quality before recording additional redundant versions. Do not create another object or replace an existing Master from a hash score.', '']
    (ROOT/'accessioned-similarity-recheck.md').write_text('\n'.join(md))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
