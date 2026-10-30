#!/usr/bin/env python3
"""Bounded Paragon source discovery and drain-register refresh; never deletes."""
import argparse, collections, csv, hashlib, json, os, re, sqlite3, subprocess, time
from pathlib import Path
from datetime import datetime, timezone

W = Path('/mnt/volume/iss-archive-work/paragon')
U = Path('/mnt/volume/iss-archive/units/paragon')
S = Path('/media/vladimir/Elements/restore/archive')
OLD = W/'work/source-disposition/paragon-source-disposition-20260727-001'
VIS = Path('/home/vladimir/archive-visual-index/visual-index.sqlite')
INDEX = Path('/home/vladimir/Archive-restoration/archive_index.sqlite')
RX = re.compile(r'(?<![A-Z0-9])(VL[LV]-\d{4}(?:-\d+)?)(?!\d)', re.I)

def read_csv(p):
    with p.open(newline='') as f: return list(csv.DictReader(f))

def guard():
    output = subprocess.run(['findmnt','-rn','-T',str(S),'-o','SOURCE,TARGET,FSTYPE,OPTIONS'],check=True,capture_output=True,text=True).stdout.strip()
    fields=output.split()
    assert fields[:3]==['/dev/sdf1','/media/vladimir/Elements','ntfs3'] and 'ro' in fields[3].split(',') and 'rw' not in fields[3].split(',')
    return output

def write_json(p,d): p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')

def write_csv(p,fields,rows):
    with p.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,quoting=csv.QUOTE_ALL)
        writer.writeheader();writer.writerows(rows)

def mapped(path):
    if path.startswith('/mnt/volume/archive/'):
        return str(S/Path(path).relative_to('/mnt/volume/archive'))
    return path

def other_unit_hold(links):
    return any(r.get('inventory') or r.get('final_path') or
               r.get('preservation_status') not in ('not_assessed','preservation_unresolved','')
               for r in links)

def discover():
    assert not (U/'db-restore/manifests/drain-review.json').exists(),'Completed drain review: inspect the current receipt; do not rerun discovery blindly.'
    guard(); started=time.monotonic()
    old={r['source_path']:r for r in read_csv(OLD/'usb-inventory.csv')}
    registry={r['source_path']:r for r in read_csv(U/'db-restore/manifests/source-disposition.csv')}
    all_files={}; dirs=0; errors=[]; symlinks=[];shared_caches=[];last=time.monotonic(); stack=[S]
    while stack:
        parent=stack.pop(); dirs+=1
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.is_symlink(): symlinks.append(entry.path); continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold().endswith('.lrdata') and 'paragon' not in entry.path.casefold():
                            shared_caches.append(entry.path);continue
                        stack.append(Path(entry.path));continue
                    if not entry.is_file(follow_symlinks=False):continue
                    st=entry.stat(follow_symlinks=False)
                    all_files[entry.path]={'source_path':entry.path,'size_bytes':st.st_size,'mtime_ns':st.st_mtime_ns}
        except OSError as exc: errors.append({'path':str(parent),'error':str(exc)})
        if time.monotonic()-last>15:
            print('USB namespace scan:',len(all_files),'files,',dirs,'directories',flush=True);last=time.monotonic()
    assert not errors,errors
    print('USB namespace complete:',len(all_files),'files,',dirs,'directories',flush=True)
    candidates={}
    def add(path,reason,**evidence):
        if path not in all_files:return
        r=candidates.setdefault(path,dict(all_files[path],reasons=[],evidence={}))
        if reason not in r['reasons']:r['reasons'].append(reason)
        r['evidence'].update(evidence)
    known_inv={r['archival_label'] for r in read_csv(U/'metadata/objects.csv')}
    for path in all_files:
        if path in registry:add(path,'existing_exact_path_register')
        if 'paragon' in str(Path(path).relative_to(S)).casefold():add(path,'Paragon_path_label')
        identifiers=set(m.upper() for m in RX.findall(Path(path).name))
        if identifiers:add(path,'Paragon_inventory_filename',filename_inventories=sorted(identifiers),known_inventories=sorted(identifiers & known_inv))
    c=sqlite3.connect(INDEX.as_uri()+'?mode=ro',uri=True)
    for path,inv,kind,confidence,origin in c.execute("SELECT f.path,l.inventory_number,l.link_type,l.confidence,l.source FROM files f JOIN file_object_links l ON f.id=l.file_id WHERE l.inventory_number LIKE 'VLV-%' OR l.inventory_number LIKE 'VLL-%'"):
        p=mapped(path)
        add(p,'metadata_asset_reference')
        if p in candidates:candidates[p]['evidence'].setdefault('metadata_links',[]).append({'inventory':inv,'type':kind,'confidence':confidence,'source':origin})
    metadata={r[0] for r in c.execute("SELECT DISTINCT source_path FROM object_sources WHERE inventory_number LIKE 'VLV-%' OR inventory_number LIKE 'VLL-%'")}
    for path in metadata:
        inventories={r[0] for r in c.execute('SELECT DISTINCT inventory_number FROM object_sources WHERE source_path=?',(path,))}
        add(mapped(path),'Paragon_metadata_source',metadata_inventories=sorted(inventories),mixed_metadata=any(not re.fullmatch(r'VL[LBV]-\d+(?:-\d+)?',i,re.I) for i in inventories))
    c.close()
    # Existing perceptual hashes find renamed/format variants. Similarity only
    # discovers a candidate; it never asserts ownership or authorizes deletion.
    c=sqlite3.connect(VIS.as_uri()+'?mode=ro',uri=True); c.row_factory=sqlite3.Row
    visual={}
    query="""SELECT a.asset_id,r.root_path,a.relative_path,a.size_bytes,a.mtime_ns,a.width_px,a.height_px,f.extractor_id,f.feature_hex
        FROM visual_assets a JOIN visual_roots r USING(root_id) JOIN visual_features f USING(asset_id)
        WHERE r.root_path='/media/vladimir/Elements' AND a.relative_path LIKE 'restore/archive/%' AND a.status='indexed'"""
    for r in c.execute(query):
        path=str(Path(r['root_path'])/r['relative_path']); live=all_files.get(path)
        if not live or (live['size_bytes'],live['mtime_ns'])!=(r['size_bytes'],r['mtime_ns']):continue
        v=visual.setdefault(path,{'asset_id':r['asset_id'],'width':r['width_px'],'height':r['height_px'],'features':{}})
        v['features'][r['extractor_id']]=int(r['feature_hex'],16)
    c.close()
    ph='phash-dct-64-v2'; dh='dhash-64-v2'; ah='ahash-64-v2'
    seeds={p:v for p,v in visual.items() if p in candidates and p in registry and ph in v['features']}
    buckets=collections.defaultdict(set)
    for p,v in seeds.items():
        for n in range(4):buckets[(n,(v['features'][ph]>>(n*16))&65535)].add(p)
    for p,v in visual.items():
        if ph not in v['features']:continue
        neighbours=set()
        for n in range(4):neighbours.update(buckets.get((n,(v['features'][ph]>>(n*16))&65535),()))
        matches=[]
        for sp in neighbours:
            if sp==p:continue
            sv=seeds[sp]; distances={k:(v['features'][k]^sv['features'][k]).bit_count() for k in (ph,dh,ah) if k in v['features'] and k in sv['features']}
            if distances.get(ph,64)<=6 and distances.get(dh,64)<=10 and distances.get(ah,64)<=10:
                matches.append({'source_path':sp,'phash_distance':distances[ph],'dhash_distance':distances[dh],'ahash_distance':distances[ah]})
        if matches:
            matches.sort(key=lambda r:(r['phash_distance']+r['dhash_distance']+r['ahash_distance'],r['source_path']))
            add(p,'indexed_visual_similarity',visual_matches=matches[:8],visual_width=v['width'],visual_height=v['height'])
    print('Named/reference/visual candidates:',len(candidates),flush=True)
    # SHA-256 inventory reuse and exact-copy search across the entire namespace.
    # Only files with a candidate's size are read for byte identity.
    digest_map=collections.defaultdict(list)
    for p,r in old.items():
        live=all_files.get(p)
        if live and (live['size_bytes'],live['mtime_ns'])==(int(r['size_bytes']),int(r['mtime_ns'])):
            if p in candidates:candidates[p]['sha256']=r['sha256'].removeprefix('sha256:')
            digest_map[(int(r['size_bytes']),r['sha256'].removeprefix('sha256:'))].append(p)
    sizes={key[0] for key in digest_map}; hashed=0; hashed_bytes=0; last=time.monotonic()
    # A few exact byte windows can reject a different file, but can never
    # establish equality. Full SHA-256 is still required for every match.
    reference_by_size=collections.defaultdict(list)
    for (size,digest),paths in digest_map.items():reference_by_size[size].append(paths[0])
    sample_cache={};sample_rejections=0
    def sample(path,size):
        values=[]
        with open(path,'rb') as f:
            for offset in sorted({0,max(0,size//2-2048),max(0,size-4096)}):
                f.seek(offset);values.append(f.read(4096))
        return tuple(values)
    write_json(W/'drain-discovery-candidates.json',{'files_scanned':len(all_files),'directories_scanned':dirs,'scan_errors':errors,'symlinks_not_followed':symlinks,'shared_lightroom_caches_retained':shared_caches,'candidates':list(candidates.values()),'possible_exact_copies':[r for p,r in all_files.items() if p not in candidates and r['size_bytes'] in sizes]})
    for p,live in all_files.items():
        needs=p in candidates and 'sha256' not in candidates[p]
        if p in old and p in candidates and 'sha256' in candidates[p]:continue
        if not needs and live['size_bytes'] not in sizes:continue
        if not needs:
            size=live['size_bytes']
            if size not in sample_cache:sample_cache[size]={sample(sp,size) for sp in reference_by_size[size]}
            if sample(p,size) not in sample_cache[size]:
                sample_rejections+=1;continue
        with open(p,'rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        st=os.stat(p);assert (st.st_size,st.st_mtime_ns)==(live['size_bytes'],live['mtime_ns'])
        hashed+=1;hashed_bytes+=st.st_size
        matches=digest_map.get((live['size_bytes'],digest),[])
        if matches:add(p,'exact_sha256_copy',exact_copies=matches)
        if p in candidates:candidates[p]['sha256']=digest
        if time.monotonic()-last>15:
            print('Targeted hash checks:',hashed,'files,',round(hashed_bytes/1e9,2),'GB, candidates',len(candidates),flush=True);last=time.monotonic()
    # Cross-unit manifests identify exact source paths which must remain shared.
    other_links=collections.defaultdict(list)
    for unit in U.parent.iterdir():
        if not unit.is_dir() or unit==U:continue
        for relative in ['00_MANIFESTS/source-disposition.csv','db-restore/manifests/source-disposition.csv']:
            p=unit/relative
            if not p.is_file():continue
            for r in read_csv(p):
                sp=r.get('source_path','')
                if sp in candidates:other_links[sp].append({'unit':unit.name,'inventory':r.get('inventory',''),'role':r.get('source_role','')})
    for p,links in other_links.items():candidates[p]['evidence']['other_unit_links']=links
    for p,r in candidates.items():
        if p in visual:
            r['visual_index']={'asset_id':visual[p]['asset_id'],'width':visual[p]['width'],'height':visual[p]['height'],'features':{k:f'{v:016x}' for k,v in visual[p]['features'].items()}}
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'source_mount':guard(),'scope':str(S),'files_scanned':len(all_files),'directories_scanned':dirs,'scan_errors':errors,'symlinks_not_followed':symlinks,'candidate_paths':len(candidates),'existing_register_paths':len(registry),'existing_paths_absent':sorted(set(registry)-set(all_files)),'new_paths':len(set(candidates)-set(registry)),'reused_hashes':sum(p in old and r.get('sha256')==old[p]['sha256'].removeprefix('sha256:') for p,r in candidates.items()),'targeted_files_hashed':hashed,'targeted_bytes_hashed':hashed_bytes,'different_byte_samples_rejected':sample_rejections,'source_modified':False,'deletion_authorized':False,'elapsed_seconds':round(time.monotonic()-started,1),'candidates':sorted(candidates.values(),key=lambda r:r['source_path'])}
    result['shared_lightroom_caches_retained']=shared_caches
    result['initial_full_namespace_scan']=json.loads((W/'drain-full-namespace-check.json').read_text())
    write_json(W/'drain-discovery.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ['candidates','symlinks_not_followed']},ensure_ascii=False,indent=2),flush=True)

def build_plan():
    assert not (U/'db-restore/manifests/drain-review.json').exists(),'Completed drain review: the current register is already installed.'
    guard()
    discovery=json.loads((W/'drain-discovery.json').read_text())
    current=read_csv(U/'db-restore/manifests/source-disposition.csv')
    by_path={r['source_path']:r for r in current};fields=list(current[0])
    catalog=read_csv(U/'CATALOG.csv');objects=read_csv(U/'metadata/objects.csv')
    by_id={r['iss_id']:r for r in objects};by_inv={r['archival_label']:r for r in objects}
    checks={r['path']:r for r in read_csv(U/'db-restore/checksums.csv')}
    def norm(d):return d.removeprefix('sha256:')
    # Final paths and hashes are paired explicitly; historical lists were sorted
    # independently. Rebuild pairs from the current checksum manifest.
    def final_pairs(paths):
        return [(p,checks[p]['sha256']) for p in sorted(set(paths)) if p in checks]
    exact=collections.defaultdict(set);preserved=collections.defaultdict(set)
    for r in catalog:
        exact[norm(r['final_sha256'])].add(r['final_relative_path'])
        if r['file_role'] in ('preservation_master','preservation_surrogate') and r['source_sha256']:
            preserved[norm(r['source_sha256'])].add(r['final_relative_path'])
    for r in current:
        if r['preservation_status'].startswith('preserved_'):
            paths=r['final_path'].split(' | ')
            assert {norm(checks[p]['sha256']) for p in paths}=={norm(h) for h in r['final_sha256'].split(' | ')}
            preserved[norm(r['source_sha256'])].update(paths)
    source_hashes_by_final=collections.defaultdict(set)
    for digest,paths in preserved.items():
        for p in paths:
            if p.startswith('masters/'):source_hashes_by_final[p].add(digest)
    con=sqlite3.connect((U/'db-restore/RESTORE.sqlite').as_uri()+'?mode=ro',uri=True)
    master_by_digest={norm(r['final_sha256']):r['final_relative_path'] for r in catalog if r['file_role'] in ('preservation_master','preservation_surrogate')}
    for action,payload in con.execute("SELECT action,after_json FROM logbook WHERE action IN ('register_preservation_surrogate','register_preservation_master','replace_surrogate_with_master')"):
        d=json.loads(payload);final=norm(d.get('content_digest',d.get('master_digest','')));source=norm(d.get('source_digest',''))
        if final in master_by_digest and source:
            p=master_by_digest[final];preserved[source].add(p);source_hashes_by_final[p].add(source)
    latest={};reject={}
    for log_id,at,actor,after,note in con.execute("SELECT log_id,at_utc,actor,after_json,note FROM logbook WHERE action='review_source_candidate' ORDER BY at_utc,log_id"):
        d=json.loads(after)
        if d.get('evidence_id'):
            latest[(d['entity_id'],d['evidence_id'])]=dict(d,log_id=log_id,at=at,actor=actor,note=note)
    for at,after,note in con.execute("SELECT at_utc,after_json,note FROM logbook WHERE action='review_surrogate_swap_candidate' ORDER BY at_utc,log_id"):
        d=json.loads(after)
        if d.get('candidate_path'):reject[mapped(d['candidate_path'])]=dict(d,at=at,note=note)
    con.close()
    reviewed=collections.defaultdict(list)
    for d in latest.values():
        if d.get('content_digest'):reviewed[norm(d['content_digest'])].append(d)
    foreign=collections.defaultdict(list)
    candidate_paths={r['source_path'] for r in discovery['candidates']}
    for unit in U.parent.iterdir():
        if not unit.is_dir() or unit==U:continue
        for relative in ('00_MANIFESTS/source-disposition.csv','db-restore/manifests/source-disposition.csv'):
            fp=unit/relative
            if not fp.is_file():continue
            for r in read_csv(fp):
                if r.get('source_path') in candidate_paths:
                    foreign[r['source_path']].append(dict(unit=unit.name,inventory=r.get('inventory',''),role=r.get('source_role',''),final_path=r.get('final_path',''),preservation_status=r.get('preservation_status',''),basis=r.get('preservation_basis','')))
    current_ids={r['final_relative_path']:r['record_key'] for r in catalog}
    decisions=json.loads((W/'master-decisions.json').read_text())
    excluded=[];rows=[];details=[]
    for item in discovery['candidates']:
        p=item['source_path'];path=Path(p)
        assert path.is_relative_to(S) and not path.is_symlink()
        st=path.stat();assert (st.st_size,st.st_mtime_ns)==(item['size_bytes'],item['mtime_ns'])
        digest=item['sha256'];old=by_path.get(p);ev=item['evidence'];ev['other_unit_links']=foreign.get(p,[])
        # A perceptual match alone does not put another collection into Paragon.
        owned=bool(old or 'Paragon_path_label' in item['reasons'] or 'Paragon_inventory_filename' in item['reasons'] or 'metadata_asset_reference' in item['reasons'] or 'exact_sha256_copy' in item['reasons'])
        if not owned:
            excluded.append(item);continue
        row=dict(old) if old else {f:'' for f in fields}
        row.update(source_path=p,source_kind='file',unit='paragon',source_sha256='sha256:'+digest,source_size_bytes=str(st.st_size),deletion_authorized='false')
        if not row['source_role']:
            row['source_role']='source_tiff_image' if path.suffix.lower() in ('.tif','.tiff') else 'source_jpeg_image' if path.suffix.lower() in ('.jpg','.jpeg') else 'project_history_document' if path.suffix.lower()=='.pdf' else 'research_note' if path.suffix.lower()=='.docx' else 'source_file'
        matches=[d for d in reviewed.get(digest,[]) if d.get('entity_id') in by_id and by_id[d['entity_id']]['master_file']]
        master_paths=set()
        for d in matches:
            inv=by_id[d['entity_id']]['archival_label']
            master_paths.update(r['final_relative_path'] for r in catalog if r['inventory']==inv and r['file_role'] in ('preservation_master','preservation_surrogate'))
        exact_paths=exact.get(digest,set());preserved_paths=preserved.get(digest,set())
        paths=exact_paths|preserved_paths
        basis='';note=[];review_ids=[]
        if paths:
            row.update(preservation_status='preserved_exact_copy' if exact_paths else 'preserved_as_metadata_enriched_master',exact_copy_proven='true' if exact_paths else 'false',drain_state='candidate_after_separate_review')
            basis=('Stored source SHA-256 equals a current final file hash.' if exact_paths else 'Stored source SHA-256 is bound to the accepted metadata-enriched Master by accession provenance.')
            linked_ids={current_ids[p] for p in paths if p in current_ids}
            inventories=sorted(by_id[i]['archival_label'] for i in linked_ids if i in by_id)
            if inventories:row['inventory']=' | '.join(inventories)
        elif matches and all(d['state']=='redundant' or (
                d['state']=='selected_master' and master_paths and
                all(source_hashes_by_final[p] and digest not in source_hashes_by_final[p] for p in master_paths)) for d in matches):
            paths=master_paths
            row.update(source_role='redundant_candidate',preservation_status='reviewed_not_selected',exact_copy_proven='false',drain_state='candidate_after_separate_review')
            row['inventory']=' | '.join(sorted({by_id[d['entity_id']]['archival_label'] for d in matches}))
            if all(d['state']=='redundant' for d in matches):
                basis='The saved human review marks this exact source hash redundant; the same object has an accessioned selected Master. This alternative is not preserved byte for byte.'
            else:
                basis='This exact hash was previously reviewed for the same object. The current accepted Master has different, explicitly recorded source provenance; this earlier selected source is superseded. The alternative is not preserved byte for byte.'
            review_ids=[d['log_id'] for d in matches]
            note += [d['note'] for d in matches if d['note']]
        else:
            row.update(exact_copy_proven='false',drain_state='retain_not_preserved')
            if old and old['preservation_status'] in ('reviewed_not_selected','not_preserved_intermediate_metadata'):
                basis=old['preservation_basis']
            else:
                row['preservation_status']='preservation_unresolved'
                basis='Related source retained: no current preservation binding or explicit redundant-hash decision is established. Filename and visual similarity are discovery evidence only.'
            if matches:
                note.append('Historical review states: '+', '.join(sorted({d['state'] for d in matches}))+'. Do not infer a new Master choice from this drain review.')
            if p in reject and reject[p]['state']=='rejected':
                note.append('Earlier surrogate-swap rejection retained: '+reject[p].get('note',''))
            if not row['inventory']:
                ids=ev.get('known_inventories',[])
                if ids:row['inventory']=' | '.join(ids)
        if other_unit_hold(ev.get('other_unit_links',[])) or ev.get('mixed_metadata'):
            row['drain_state']='retain_not_preserved'
            note.append('Shared or conflicting collection evidence: retain the whole source to protect other collections. '+json.dumps(ev.get('other_unit_links',[]),ensure_ascii=False))
        elif ev.get('other_unit_links'):
            note.append('Also discovered as an unassessed, unassigned candidate by '+', '.join(sorted({r['unit'] for r in ev['other_unit_links']}))+'. That search lead is not an accepted other-unit object or preservation link. Current Paragon evidence controls this row; other-unit records are unchanged.')
        if not old:note.append('Added by full restore/archive discovery: '+', '.join(item['reasons'])+'.')
        if review_ids:note.append('Saved review log IDs: '+', '.join(review_ids)+'.')
        pairs=final_pairs(paths)
        row.update(final_path=' | '.join(p for p,h in pairs),final_sha256=' | '.join(h for p,h in pairs),preservation_basis=basis+' Live source size/mtime match the checked hash evidence.',notes=' '.join(dict.fromkeys(note+['Scope is this exact Paragon-related file only; other sources remain untouched. No deletion authorized.'])))
        rows.append(row)
        details.append(dict(source_path=p,source_size_bytes=st.st_size,source_mtime_ns=st.st_mtime_ns,source_sha256='sha256:'+digest,discovery_reasons=item['reasons'],review_log_ids=review_ids,other_unit_links=ev.get('other_unit_links',[])))
    assert set(by_path)<=set(r['source_path'] for r in rows)
    rows.sort(key=lambda r:r['source_path'])
    write_csv(W/'source-disposition.planned.csv',fields,rows)
    write_csv(W/'drain-list.csv',fields,[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    write_csv(W/'retain-review.csv',fields,[r for r in rows if r['drain_state']!='candidate_after_separate_review'])
    write_json(W/'drain-evidence.json',details)
    write_json(W/'drain-excluded-discovery.json',excluded)
    summary=dict(checked_at=datetime.now(timezone.utc).isoformat(),source_scope=str(S),source_mount=guard(),namespace_files_scanned=discovery['initial_full_namespace_scan']['files_scanned'],namespace_directories_scanned=discovery['initial_full_namespace_scan']['directories_scanned'],payload_files_rechecked=discovery['files_scanned'],shared_lightroom_caches_retained=discovery['shared_lightroom_caches_retained'],rows=len(rows),source_bytes=sum(int(r['source_size_bytes']) for r in rows),candidate_rows=sum(r['drain_state']=='candidate_after_separate_review' for r in rows),retain_rows=sum(r['drain_state']!='candidate_after_separate_review' for r in rows),new_paths=len(rows)-len(current),unconfirmed_visual_matches_excluded=len(excluded),preservation_status=dict(collections.Counter(r['preservation_status'] for r in rows)),source_roles=dict(collections.Counter(r['source_role'] for r in rows)),deletion_authorized=False,sources_modified=False,scope_complete_claim='All known and confirmed Paragon-related paths found by full namespace traversal, recorded metadata, stored hashes and visual-index discovery. Unconfirmed visual similarity does not establish ownership.',limitations='Shared Lightroom caches remain untouched as mixed-source containers. Unmatched or cropped/rotated images with opaque names may need further visual identification. Retain rows prevent claiming that the source can already be emptied.')
    summary['excluded_visual_paths']=[r['source_path'] for r in excluded]
    summary['shared_catalogues_retained']=json.loads((W/'drain-shared-catalogues.json').read_text())
    docs=json.loads((W/'drain-document-search.json').read_text())
    summary['document_context_paths_excluded']=[r['source_path'] for r in docs if 'paragon' not in r['source_path'].casefold()]
    summary['new_retained_material']=[r['source_path'] for r in rows if r['source_path'] not in by_path and r['drain_state']=='retain_not_preserved']
    write_json(W/'drain-review-summary.json',summary);print(json.dumps({k:v for k,v in summary.items() if k not in ('shared_lightroom_caches_retained','document_context_paths_excluded')},ensure_ascii=False,indent=2),flush=True)

def apply_plan():
    guard()
    rows=read_csv(W/'source-disposition.planned.csv');summary=json.loads((W/'drain-review-summary.json').read_text())
    evidence={r['source_path']:r for r in json.loads((W/'drain-evidence.json').read_text())}
    checks=read_csv(U/'db-restore/checksums.csv');check_by={r['path']:r for r in checks}
    target='db-restore/manifests/source-disposition.csv'
    report_path='db-restore/manifests/drain-review.json'
    evidence_path='db-restore/manifests/drain-evidence.json'
    assert not (U/report_path).exists() and not (U/evidence_path).exists(),'This update is already installed'
    assert len(rows)==len(evidence)==summary['rows'] and len({r['source_path'] for r in rows})==len(rows)
    before_dirs={str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    before={str(p.relative_to(U)):(p.stat().st_size,p.stat().st_mtime_ns) for p in U.rglob('*') if p.is_file()}
    before_source=set(r['source_path'] for r in read_csv(U/target))
    assert before_source<=set(evidence)
    def validate():
        guard()
        for row in rows:
            p=Path(row['source_path']);assert p.is_relative_to(S) and not p.is_symlink() and p.resolve(strict=True)==p
            st=p.stat();e=evidence[str(p)]
            assert (st.st_size,st.st_mtime_ns)==(e['source_size_bytes'],e['source_mtime_ns'])
            assert row['source_sha256']==e['source_sha256'] and row['deletion_authorized']=='false' and row['unit']=='paragon'
            if row['drain_state']=='candidate_after_separate_review':
                assert row['final_path'] and row['final_sha256'] and not other_unit_hold(e['other_unit_links'])
            if row['final_path']:
                paths=row['final_path'].split(' | ');hashes=row['final_sha256'].split(' | ')
                assert len(paths)==len(hashes)
                for fp,digest in zip(paths,hashes):
                    assert check_by[fp]['sha256']==digest and (U/fp).stat().st_size==int(check_by[fp]['size_bytes'])
    validate()
    candidate_rows=[r for r in rows if r['drain_state']=='candidate_after_separate_review']
    retained=[r for r in rows if r['drain_state']!='candidate_after_separate_review']
    assert len(candidate_rows)==summary['candidate_rows'] and len(retained)==summary['retain_rows']
    summary.update(completed_at=datetime.now(timezone.utc).isoformat(),result='passed',authorization='Update the Paragon drain list for all Paragon files and derivatives in restore/archive, keeping other sources untouched.',source_disposition=target,source_evidence=evidence_path,candidate_source_bytes=sum(int(r['source_size_bytes']) for r in candidate_rows),retained_source_bytes=sum(int(r['source_size_bytes']) for r in retained),master_selections_changed=False,derivatives_generated=0,final_media_rehashed=0,sqlite_schema_changed=False,source_modified=False,other_unit_files_changed=False,source_deletion_performed=False,directory_structure_unchanged=True,checksums_file='db-restore/checksums.csv')
    # The current SQLite catalogue has no source-disposition table. Its Master
    # decisions were read as evidence; there is no reason to change its schema.
    db=sqlite3.connect((U/'db-restore/RESTORE.sqlite').as_uri()+'?mode=ro',uri=True)
    assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert not db.execute('pragma foreign_key_check').fetchall();db.close()
    summary['sqlite_master_catalogue_unchanged']=True
    write_csv(U/target,list(rows[0]),rows)
    write_json(U/report_path,summary)
    write_json(U/evidence_path,list(evidence.values()))
    readme=U/'00_README.md';content=readme.read_text()
    marker='## Earlier export and normalization checkpoint'
    addition=f'''## Current Paragon source/drain register — 2026-09-15

The current exact-path register is `{target}`. The review traversed
all {summary['namespace_files_scanned']:,} files under `/media/vladimir/Elements/restore/archive`
and checked existing hashes, metadata references, visual-index leads and saved
human candidate decisions. It now lists {summary['rows']:,} Paragon-related source
paths, including {summary['new_paths']:,} paths outside the previous register.

{summary['candidate_rows']:,} files are candidates for a later separate drain review.
{summary['retain_rows']:,} files remain on hold because preservation, identity,
redundancy or exclusive Paragon scope is not established. Copies and derivatives
are included where the evidence identifies them. Perceptual similarity alone
does not make another collection's file a Paragon drain target.

The aim is to remove only Paragon-related files from that USB source subtree
after the remaining holds and explicit path-specific deletion authorization are
resolved. No directory-wide deletion is implied. Shared and other-collection
material must remain. Every row still has `deletion_authorized=false`.

`{report_path}` records scope, counts and limitations.
Work-area `drain-list.csv` and `retain-review.csv` are filtered views of this
register. The current Master catalogue and all accessioned media are unchanged.
The preceding accession receipt is historical for its checksum checkpoint;
`db-restore/checksums.csv` now also covers this authorized drain-register update.

'''
    assert marker in content and '## Current Paragon source/drain register' not in content
    readme.write_text(content.replace(marker,addition+marker,1))
    changed=[target,report_path,evidence_path,'00_README.md']
    for relative in changed:
        path=U/relative
        with path.open('rb') as f:digest='sha256:'+hashlib.file_digest(f,'sha256').hexdigest()
        check_by[relative]={'path':relative,'sha256':digest,'size_bytes':str(path.stat().st_size)}
    # Current export summary carries the current checksum count; historical
    # accession and review receipts remain evidence of their own checkpoint.
    export_path=U/'db-restore/export-summary.json';export=json.loads(export_path.read_text())
    export['checksums']=len(check_by);export['source_disposition_review']={'rows':len(rows),'candidates':len(candidate_rows),'retain':len(retained),'receipt':report_path}
    write_json(export_path,export)
    with export_path.open('rb') as f:hash_value='sha256:'+hashlib.file_digest(f,'sha256').hexdigest()
    check_by['db-restore/export-summary.json']={'path':'db-restore/export-summary.json','sha256':hash_value,'size_bytes':str(export_path.stat().st_size)}
    write_csv(U/'db-restore/checksums.csv',list(checks[0]),sorted(check_by.values(),key=lambda r:r['path']))
    changed += ['db-restore/checksums.csv','db-restore/export-summary.json']
    assert read_csv(U/target)==rows
    assert {str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}==before_dirs
    for relative,st in before.items():
        if relative not in changed and not relative.endswith('.sqlite-shm'):
            p=U/relative;assert (p.stat().st_size,p.stat().st_mtime_ns)==st,relative
    validate()
    summary['directory_structure_unchanged']=True
    write_json(W/'drain-review-summary.json',summary)
    # Preserve the original receipt as-is; this work summary records the later
    # review. No historical accession runner or hashing step is repeated.
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--discover',action='store_true');parser.add_argument('--plan',action='store_true');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.discover:discover()
    if args.plan:build_plan()
    if args.apply:apply_plan()
