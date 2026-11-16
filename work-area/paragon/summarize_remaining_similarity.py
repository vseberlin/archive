#!/usr/bin/env python3
"""Summarize similarity evidence as review suggestions; never apply decisions."""
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from paragon_workbench import mount_guard

W = Path(__file__).resolve().parent
KEYS = ['phash-dct-64-v2', 'dhash-64-v2', 'ahash-64-v2']


def native_cache(path):
    """Read Czkawka 12.0.1's version-120 Vec<ImagesEntry>, bincode varints.

    Format checked against the tagged Czkawka common/cache.rs and ImagesEntry.
    This only reads the cache; it neither writes nor replaces Czkawka files.
    """
    blob=path.read_bytes();position=0
    def take(n):
        nonlocal position
        value=blob[position:position+n];position+=n
        assert len(value)==n
        return value
    def integer():
        marker=take(1)[0]
        if marker<251:return marker
        assert marker in (251,252,253,254)
        return int.from_bytes(take({251:2,252:4,253:8,254:16}[marker]),'little')
    entries=[]
    for _ in range(integer()):
        filename=take(integer()).decode('utf-8')
        size,width,height,modified=[integer() for _ in range(4)]
        hashes=[take(integer()).hex() for _ in range(integer())]
        difference=integer()
        assert hashes and all(len(h)==64 for h in hashes) and difference==0
        entries.append(dict(path=filename,size_bytes=size,width=width,height=height,
                            modified_seconds=modified,hashes=hashes))
    assert position==len(blob),(position,len(blob))
    assert len(entries)==len({e['path'] for e in entries})
    return {e['path']:e for e in entries}


def main():
    mount_guard()
    snapshot = json.loads((W / 'remaining-similarity-inputs.json').read_text())
    for path, expected in snapshot['input_stamps'].items():
        st=Path(path).stat()
        assert [st.st_size,st.st_mtime_ns]==expected, f'Comparison snapshot is stale: {path}'
    run = json.loads((W / 'remaining-czkawka-runs.json').read_text())
    inputs = snapshot['unresolved'] + snapshot['explicitly_retained'] + snapshot['references'] + snapshot['additional_index_candidates']
    assets = {r['path']: r for r in inputs if r.get('features') or r.get('source') == 'other_source_index'}
    for path,row in assets.items():
        st=Path(path).stat()
        assert [st.st_size,st.st_mtime_ns]==[row['size_bytes'],row['mtime_ns']],path
    unresolved = {r['path']: r for r in snapshot['unresolved'] if r.get('features')}
    pairs = {}
    seen = defaultdict(set)
    native,exclusions={},{}
    for algorithm in ('Gradient', 'Mean', 'Blockhash'):
        tag='mirror_flip_rotate90' if algorithm=='Gradient' else 'off'
        cache=native_cache(W/f'cache_similar_images_16_{algorithm}_Nearest_{tag}_120.bin')
        native[algorithm]=list(cache.values())
        exclusions[algorithm]=sorted(set(assets)-set(cache))
        assert set(cache).issubset(assets)
        for p,row in cache.items():
            a=assets[p]
            assert row['size_bytes']==a['size_bytes'] and row['modified_seconds']==a['mtime_ns']//10**9,p
            assert sorted([row['width'],row['height']])==sorted([a['width'],a['height']]),p
        hashes={p:[int(h,16) for h in row['hashes']] for p,row in cache.items()}
        # Exhaustive Hamming comparisons reuse native hashes. This also recovers
        # valid pairs omitted from Czkawka's non-overlapping display groups.
        for a in sorted(set(unresolved)&set(hashes)):
            for b, bh in hashes.items():
                if a==b or (b in unresolved and b<a):continue
                distance_value=min((aa^bb).bit_count() for aa in hashes[a] for bb in bh)
                if distance_value>40:continue
                key=tuple(sorted((a,b)))
                pair=pairs.setdefault(key,dict(paths=list(key),algorithms={}))
                pair['algorithms'][algorithm]=dict(distance=distance_value,measure='direct_from_native_hashes')
                seen[algorithm].update(key)
        print(f'{algorithm}: {len(cache)} native hashes reused; {len(exclusions[algorithm])} unsupported TIFFs',flush=True)
    (W/'remaining-czkawka-hashes.json').write_text(json.dumps(native,ensure_ascii=False,indent=2)+'\n')
    # Reuse existing hashes for every source-source/source-reference comparison.
    def distance(a, b):
        if not a.get('features') or not b.get('features'):
            return None
        variants_a = a.get('rotation_features', [a['features']])
        variants_b = b.get('rotation_features', [b['features']])
        options = []
        for i, aa in enumerate(variants_a):
            for j, bb in enumerate(variants_b):
                ds = [(int(aa[k], 16) ^ int(bb[k], 16)).bit_count() for k in KEYS]
                options.append(dict(distance=ds, first_rotation=i*90, second_rotation=j*90))
        return min(options, key=lambda d: sum(d['distance']))
    for path, source in unresolved.items():
        for target_path, target in assets.items():
            if path == target_path:
                continue
            key = tuple(sorted((path, target_path)))
            if target_path in unresolved and target_path < path:
                continue
            d = distance(assets[key[0]], assets[key[1]])
            if not d:
                continue
            ds = d['distance']
            if key in pairs or (ds[0] <= 8 and max(ds[1:]) <= 12 and sum(ds) <= 28):
                pair = pairs.setdefault(key, dict(paths=list(key), algorithms={}))
                pair['indexed_hashes'] = d

    metadata = json.loads((W / 'retained-derivative-metadata.json').read_text())
    def lineage(asset):
        meta = metadata.get(asset.get('sha256'), {})
        return {str(v).removeprefix('xmp.did:').lower() for k, v in meta.items()
                if k.endswith(('OriginalDocumentID', 'DerivedFromDocumentID', ':DocumentID')) and v}
    for pair in pairs.values():
        a, b = [assets[p] for p in pair['paths']]
        ra, rb = a['width']/a['height'], b['width']/b['height']
        aspect = min(abs(ra/rb-1), abs(ra*rb-1))
        pair['aspect_relative_difference'] = round(aspect, 6)
        pair['shared_embedded_document_ids'] = sorted(lineage(a) & lineage(b))
        ds = pair.get('indexed_hashes', {}).get('distance', [64, 64, 64])
        support = len(pair['algorithms'])
        bounds = [v['distance'] for v in pair['algorithms'].values()]
        hashclose = ds[0] <= 6 and max(ds[1:]) <= 8 and sum(ds) <= 18
        strict_czk = support == 3 and max(bounds) <= 16
        if support == 3 and aspect <= .02 and (hashclose or strict_czk):
            strength = 'strong'
        elif support >= 2 or hashclose:
            strength = 'moderate'
        else:
            strength = 'weak'
        pair['strength'] = strength

    links = defaultdict(list)
    for pair in pairs.values():
        for p in pair['paths']:
            if p in unresolved:
                links[p].append(pair)
    summaries = []
    for p, source in unresolved.items():
        candidates = []
        for pair in links[p]:
            target_path = next(x for x in pair['paths'] if x != p)
            target = assets[target_path]
            area_ratio = target['width']*target['height'] / (source['width']*source['height'])
            archive = target['source'] == 'archive'
            candidate = dict(target=target_path, target_source=target['source'],
                             target_inventory=target.get('inventory', ''), target_master=target.get('master_path', ''),
                             target_width=target['width'], target_height=target['height'],
                             target_case_key=target.get('case_key', ''), area_ratio=round(area_ratio, 5),
                             strength=pair['strength'], algorithm_count=len(pair['algorithms']),
                             algorithms=pair['algorithms'], indexed_hashes=pair.get('indexed_hashes'),
                             shared_embedded_document_ids=pair['shared_embedded_document_ids'],
                             aspect_relative_difference=pair['aspect_relative_difference'])
            candidate['coverage_candidate'] = (pair['strength'] == 'strong' and
                ((archive and area_ratio >= .95) or (not archive and area_ratio >= 1.05)))
            candidates.append(candidate)
        candidates.sort(key=lambda c: (not c['coverage_candidate'],
            {'strong': 0, 'moderate': 1, 'weak': 2}[c['strength']], c['target_source'] != 'archive',
            -c['algorithm_count'], sum(c['indexed_hashes']['distance']) if c['indexed_hashes'] else 192,
            -c['area_ratio']))
        if any(c['coverage_candidate'] for c in candidates):
            category = 'likely_redundant'
        elif any(c['strength'] == 'strong' for c in candidates):
            category = 'strong_match_keep_for_master_or_coverage_review'
        elif candidates:
            category = 'similarity_requires_review'
        else:
            category = 'no_match_at_search_thresholds'
        summaries.append(dict(path=p, name=source['name'], case_key=source['case_key'],
                              case_label=(source.get('inventories') or Path(source['case_key']).name).replace('|', ' / '),
                              native_comparison_available=p not in exclusions['Gradient'],
                              sha256=source['sha256'], width=source['width'], height=source['height'],
                              small=max(source['width'],source['height']) <= 1600,
                              category=category, candidates=candidates,
                              workbench_url='http://127.0.0.1:8766/retained/#' + quote(source['case_key'])))
    summaries.sort(key=lambda s: (s['category'] != 'likely_redundant', not s['small'], s['case_key'], s['path']))
    counts = Counter(s['category'] for s in summaries)
    small_counts = Counter(s['category'] for s in summaries if s['small'])
    report = dict(completed_at=datetime.now(timezone.utc).isoformat(),
                  counts=dict(counts), small_image_counts=dict(small_counts),
                  likely_redundant_archive_target=sum(s['category']=='likely_redundant' and s['candidates'][0]['target_source']=='archive' for s in summaries),
                  likely_redundant_retained_target=sum(s['category']=='likely_redundant' and s['candidates'][0]['target_source']!='archive' for s in summaries),
                  pair_counts=dict(Counter(p['strength'] for p in pairs.values())),
                  czkawka_matched_paths={k:len(v) for k,v in seen.items()},
                  method=dict(czkawka=run, source_features_reused=692,
                    index_images=snapshot['index_image_count'], searched_protected_archive_images=snapshot['protected_archive_index_count'],
                    native_hash_search_radius=40,
                    strength_rule='Strong: all 3 Czkawka algorithms within distance 40, relative aspect difference <=2%, plus pHash<=6/dHash<=8/aHash<=8 and sum<=18, or all three direct Czkawka distances <=16.',
                    coverage_candidate_rule='Strong match to an accessioned image with >=95% source pixels, or a retained image with >=105% source pixels. These are review suggestions, not coverage proof or Master decisions.',
                    note='Gradient includes rotations/flips; Mean and Blockhash use normal orientation. Existing reference pHashes also cover quarter-turn rotations. Direct exhaustive Hamming distances reuse the native Czkawka hashes; no group co-membership is treated as an exact distance. Similarity is not identity. Crops, carrier context, retouching and colour need review.'),
                  native_decode_exclusions=exclusions,
                  sources=summaries, pairs=list(pairs.values()), documents=snapshot['documents'],
                  decisions_changed=False, source_register_drain_count=714, deletion_authorized=False)
    (W/'remaining-similarity-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    lines = ['# Paragon remaining-image similarity results', '',
        'Read-only comparison completed '+report['completed_at']+'. No Master or drain decisions changed.', '',
        '692 unresolved images: all existing pHash/dHash/aHash features reused. 10 documents excluded from image comparison.',
        'Czkawka 12.0.1 Gradient, Mean and Blockhash; 16-bit-side (256-bit) hashes. The native CLI reports used distance 20; direct comparison of the same cached hashes widens discovery to distance 40, with tighter existing-hash agreement required for strong suggestions. Gradient includes rotations/flips; Mean and Blockhash confirm normal orientation. Existing reference pHashes also cover quarter-turn rotations.',
        '973 exact input files: 692 unresolved images, 3 explicitly retained images, 278 accessioned image references. Broader stored-index search covered 144,966 protected-source images.', '',
        'Eight palette TIFFs are unsupported by Czkawka\'s decoder; their existing pHash/dHash/aHash comparisons remain included. They are not damaged-image decisions. Native cache hashes were exported to readable JSON and compared directly, retaining all matching pairs instead of only Czkawka\'s display groups.', '',
        '| Finding | All images | Of 201 small images |', '|---|---:|---:|']
    labels = {'likely_redundant':'Likely redundant: strong match with equal/larger coverage candidate',
              'strong_match_keep_for_master_or_coverage_review':'Strong match, but higher-resolution source or equal-size alternatives need a Master/coverage choice',
              'similarity_requires_review':'Similarity needs identity or framing review',
              'no_match_at_search_thresholds':'No match at these thresholds'}
    for key in labels:
        lines.append(f'| {labels[key]} | {counts[key]} | {small_counts[key]} |')
    lines += ['', '**These are suggestions, not newly registered drain entries.** The drain count remains 714. Similarity can also connect crops, carrier context and differently processed originals. Even three agreeing algorithms do not establish full coverage.', '',
              'Inspect the [side-by-side comparison report](remaining-similarity.html), or use the workbench links below. Exact paths, individual algorithm evidence and alternate targets are in [JSON evidence](remaining-similarity-report.json).', '',
              '## Likely redundant candidates', '',
              '| Review | Source | Pixels | Strong comparison target | Target pixels |', '|---|---|---:|---|---:|']
    for s in summaries:
        if s['category'] != 'likely_redundant':
            continue
        t=s['candidates'][0]
        lines.append(f"| [{s['case_label']}]({s['workbench_url']}) | [{s['name']}]({quote(s['path'],safe='/')}) | {s['width']}×{s['height']} | [{Path(t['target']).name}]({quote(t['target'],safe='/')}) | {t['target_width']}×{t['target_height']} |")
    lines += ['', '## Remaining image findings', '', '| Review | Source | Finding |', '|---|---|---|']
    for s in summaries:
        if s['category'] == 'likely_redundant': continue
        lines.append(f"| [{s['case_label']}]({s['workbench_url']}) | [{s['name']}]({quote(s['path'],safe='/')}) | {labels[s['category']]} |")
    lines += ['', '## Documents outside image comparison', ''] + ['- '+d['path'] for d in snapshot['documents']]
    (W/'remaining-similarity-report.md').write_text('\n'.join(lines)+'\n')
    data=json.dumps(dict(counts=dict(counts),labels=labels,sources=summaries),ensure_ascii=False).replace('<','\\u003c')
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Paragon similarity comparison</title>
<style>body{font:16px system-ui;max-width:1500px;margin:24px auto;padding:0 20px;background:#f4f2ed;color:#202c28}h1{font-size:28px}button,select{font:inherit;padding:8px;margin:4px}select{max-width:70vw}nav{position:sticky;top:0;background:#f4f2ed;padding:12px 0;z-index:1}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.card{background:white;padding:16px;border:1px solid #c6cec7}.picture{height:520px;display:flex;align-items:center;justify-content:center;background:#ddd;overflow:hidden}.picture a{width:100%;height:100%;display:flex;align-items:center;justify-content:center}.picture img{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#126251}.muted{color:#53645b}@media(max-width:800px){.pair{grid-template-columns:1fr}.picture{height:350px}}</style>
<h1>Paragon · remaining image comparisons</h1><p>692 images compared using existing perceptual hashes and three Czkawka algorithms. Suggestions only; the drain list remains at 714. Eight TIFFs were compared with the existing index only.</p>
<nav><select id="category" aria-label="Finding"></select><button id="prev">← Previous</button><button id="next">Next →</button><span id="position"></span></nav><main id="main"></main>
<script>const data=DATA;let current=0,rows=[];const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const image=p=>'http://127.0.0.1:8766/retained/image?path='+encodeURIComponent(p);
for(const [k,label]of Object.entries(data.labels))$('#category').insertAdjacentHTML('beforeend',`<option value="${k}">${data.counts[k]||0} · ${esc(label)}</option>`);
function render(){const s=rows[current];$('#position').textContent=rows.length?`${current+1} of ${rows.length}`:'No entries';if(!s){$('#main').innerHTML='';return;}const t=s.candidates[0];$('#main').innerHTML=`<p><a href="${s.workbench_url}" target="_blank">Open ${esc(s.case_label)} in the workbench</a></p><div class="pair"><article class="card"><h2>Remaining source · ${s.width} × ${s.height}</h2><div class="picture"><a href="${image(s.path)}&full=1" target="_blank"><img src="${image(s.path)}" alt="Remaining source"></a></div><pre>${esc(s.path)}</pre></article>${t?`<article class="card"><h2>${t.target_source==='archive'?'Accessioned reference':'Retained comparison'} · ${t.target_width} × ${t.target_height}</h2><div class="picture"><a href="${image(t.target)}&full=1" target="_blank"><img src="${image(t.target)}" alt="Comparison image"></a></div><pre>${esc(t.target)}</pre></article>`:'<article class="card"><h2>No match at these thresholds</h2><p>The image remains for object and Master review.</p></article>'}</div>${t?`<p>${t.algorithm_count} Czkawka algorithms agree. Match: ${esc(t.strength)}. Indexed pHash/dHash/aHash distances: ${esc(t.indexed_hashes?.distance.join(' / ')||'orientation comparison unavailable')}.</p>`:''}<p class="muted">Compare the whole frame and its edges. Carrier context or additional content may require retaining both images. Clicking an image opens the full view. Save decisions in the existing workbench.</p><details><summary>All comparison evidence (${s.candidates.length} targets)</summary><pre>${esc(JSON.stringify(s.candidates,null,2))}</pre></details>`;}
function filter(){rows=data.sources.filter(s=>s.category===$('#category').value);current=0;render();}$('#category').onchange=filter;$('#prev').onclick=()=>{current=Math.max(0,current-1);render()};$('#next').onclick=()=>{current=Math.min(rows.length-1,current+1);render()};filter();</script></html>'''.replace('DATA',data)
    html=html.replace('<h1>Paragon · remaining image comparisons</h1>', '<h1>Paragon · remaining image comparisons</h1><p class="muted">Comparison snapshot: '+datetime.fromisoformat(report['completed_at']).strftime('%Y-%m-%d %H:%M UTC')+'</p>', 1)
    (W/'remaining-similarity.html').write_text(html)
    print(json.dumps({k:report[k] for k in ['counts','small_image_counts','likely_redundant_archive_target','likely_redundant_retained_target','pair_counts','czkawka_matched_paths']},indent=2),flush=True)


if __name__=='__main__':
    main()
