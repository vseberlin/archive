#!/usr/bin/env python3
"""Apply 38 saved drain choices and accession 16 selected sections of four carriers.

Bounded continuation of the existing Paragon accession. No new objects, directories,
schema, source mutations, publication or deletion. Completed batches are not rerun.
"""
import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps
from openpyxl import load_workbook
import paragon_workbench as app
import retained_candidates as review
from accession_carrier_section import read_csv, write_csv, stamp, connect, file_rows, update_workbook, render_range
from accession_reviewed import embed, verify_workbook
from accession_retained import wrap_new_hashes

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/retained-sections-accession.json'
WORK_RECEIPT = W/'retained-sections-accession.json'
AUTHORIZATION = 'ok do it'
SCOPE = 'Incorporate the 38 ready saved drain decisions and accession the 16 retained carrier sections.'
EXPECTED = {'VLV-1148': 6, 'VLV-1151': 1, 'VLV-1155': 4, 'VLV-1156': 5}
CONTROLS = ['metadata/objects.csv', 'CATALOG.csv', 'CATALOG.xlsx', 'db-restore/RESTORE.csv',
    'db-restore/RESTORE.xlsx', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/representations.csv', 'db-restore/manifests/source-disposition.csv',
    'db-restore/manifests/drain-evidence.json', '00_README.md', 'catalog-export-summary.json',
    'db-restore/export-summary.json', RECEIPT]


def save(receipt):
    review.write_json(WORK_RECEIPT, receipt)


def prepare():
    app.mount_guard()
    assert not (U/RECEIPT).exists(), 'Already accessioned; use --verify.'
    with app.connect() as con:
        review.check(con)
        saved = review.saved_reviews(con)
    if WORK_RECEIPT.exists():
        receipt = json.loads(WORK_RECEIPT.read_text())
        assert receipt['prior_reviews'] == saved
        return receipt
    assert len(saved) == 132
    assert (W/'source-disposition.planned.csv').read_bytes() == (U/'db-restore/manifests/source-disposition.csv').read_bytes()
    register = {r['source_path']: r for r in read_csv(U/'db-restore/manifests/source-disposition.csv')[1]}
    proposals = json.loads((W/'retained-proposed-drain.json').read_text())['files']
    ready = [p for p in proposals if not p['awaiting_accession'] and register[p['path']]['drain_state'] != 'candidate_after_separate_review']
    assert Counter(p['disposition'] for p in ready) == {'redundant': 26, 'noise_drain': 12}
    checks = {r['path']: r for r in read_csv(U/'db-restore/checksums.csv')[1]}
    carriers = []
    for inventory, number in EXPECTED.items():
        decision = saved[inventory]
        files = [f for f in decision['files'] if f['state'] == 'carrier_section']
        assert len(files) == number and decision['retention']['carrier'] == 'retain'
        carrier = review.evidence()['catalogue'][inventory]
        assert carrier['master_member_count'] == '1'
        obj = dict(inventory=inventory, entity_id=carrier['iss_id'], review=decision,
            prior_metadata=carrier, overview=carrier['master_file'], sections=[],
            coverage_complete=False, selected_master_representations=1)
        for f in files:
            source = review.evidence()['assets'][f['path']]
            review.verify_asset(source)
            assert f['carrier'] == inventory and f['sha256'] == source['sha256']
            assert register[f['path']]['drain_state'] == 'retain_not_preserved'
            with Image.open(source['path']) as im:
                im.load()
                assert im.format == 'TIFF' and im.mode == 'L' and getattr(im, 'n_frames', 1) == 1
                assert im.tag_v2.get(258) == (8,)
                technical = dict(width=im.width, height=im.height, mode=im.mode,
                    orientation=im.getexif().get(274, 1), compression=im.info.get('compression', 'raw'))
            slug = re.sub(r'[^A-Za-z0-9-]+', '-', Path(f['path']).stem).strip('-')
            base = inventory+'__'+carrier['iss_id']+'__section-'+slug
            master, access = 'masters/'+base+'__master.tif', 'export/access/'+base+'__access.jpg'
            for path in (master, access):
                assert not (U/path).exists() and (U/path).parent.is_dir(), path
            obj['sections'].append(dict(source=source, technical=technical, master_path=master,
                access_path=access, part='section shown in source '+Path(f['path']).name, media=[]))
        obj['selected_master_members'] = [obj['overview']] + [s['master_path'] for s in obj['sections']]
        carriers.append(obj)
    all_sources = [p['path'] for p in ready] + [s['source']['path'] for o in carriers for s in o['sections']]
    assert len(all_sources) == len(set(all_sources)) == 54
    for p in all_sources:
        review.verify_asset(review.evidence()['assets'][p])
        assert register[p]['source_sha256'] == review.evidence()['assets'][p]['sha256']
    for p in ready:
        if p['disposition'] == 'noise_drain':
            assert any(f['path'] == p['path'] and f['state'] == 'noise_drain' for r in saved.values() for f in r['files'])
            continue
        target = p['derivative_evidence']['coverage_target']
        path = Path(target['path'])
        assert target['kind'] == 'accessioned_master' and path.resolve().is_relative_to(U)
        relative = str(path.relative_to(U))
        assert checks[relative]['sha256'].removeprefix('sha256:') == target['sha256'].removeprefix('sha256:')
        assert path.stat().st_size == target['size_bytes'] == int(checks[relative]['size_bytes'])
        p['verified_target_stamp'] = stamp(path)
        with connect(U/'db-restore/RESTORE.sqlite') as con:
            assert con.execute("SELECT 1 FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) WHERE fo.relative_path=? AND fo.content_digest=? AND ef.status='active' AND ef.role IN ('preservation_master','preservation_surrogate')", (relative, checks[relative]['sha256'])).fetchone()
        con.close()
    baseline_db = {}
    for db in [W/'work/catalog.sqlite', U/'db-restore/RESTORE.sqlite']:
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
            for o in carriers: assert len(file_rows(con, o['entity_id'])) == 2
            baseline_db[str(db)] = dict(objects=[dict(r) for r in con.execute('SELECT * FROM archive_objects ORDER BY entity_id')],
                relations=[dict(r) for r in con.execute('SELECT * FROM entity_relations ORDER BY relation_id')],
                files=[dict(r) for r in con.execute('SELECT * FROM entity_files ORDER BY entity_id,occurrence_id')])
        con.close()
    assert shutil.disk_usage(U).free > 1_200_000_000
    receipt = dict(authorization=AUTHORIZATION, authorized_scope=SCOPE, actor='Vladimir',
        started_at=datetime.now(timezone.utc).isoformat(), unit='paragon', carriers=carriers,
        ready_drain_decisions=ready, prior_reviews=saved, prior_source_rows={p:register[p] for p in all_sources},
        prior_databases=baseline_db, validation_result='pending', accession_performed=False,
        source_modified=False, deletion_authorized=False, publication_performed=False,
        original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        original_files={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))},
        original_counts={p:len(read_csv(U/p)[1]) for p in ['CATALOG.csv','metadata/objects.csv','db-restore/RESTORE.csv','db-restore/manifests/representations.csv']},
        stable_review_source_stamp=stamp(W/'retain-review.csv'), changed_controls=CONTROLS,
        log_id='LOG-'+uuid4().hex.upper())
    save(receipt)
    return receipt


def make_media(receipt):
    for obj in receipt['carriers']:
        for s in obj['sections']:
            src = s['source']; review.verify_asset(src)
            m = obj['prior_metadata']
            description = m['description']+'\n\nTeilscan der Wandzeitung '+obj['inventory']+'. Quelldatei: '+Path(src['path']).name+'. Keine Gesamtansicht; vollständige hochauflösende Abdeckung bleibt ungeklärt.'
            embedded = dict(iss_id=obj['entity_id'], archival_label=obj['inventory'],
                descriptive_title=m['descriptive_title']+' — Teilscan '+Path(src['path']).stem, description=description)
            s['embedded_metadata'] = embedded
            for kind in ['master','access']:
                relative=s[kind+'_path']; prior=next((f for f in s['media'] if f['path']==relative),None)
                if prior:
                    assert stamp(U/relative)==prior['stamp']
                    continue
                assert not (U/relative).exists(), 'Refusing to overwrite unrecorded output: '+relative
                if kind=='master':
                    s['source_embedded_metadata']=json.loads(subprocess.check_output(['exiftool','-j','-G1','-s','-XMP:All','-IPTC:All','-IFD0:ImageDescription','-IFD0:Artist','-IFD0:Copyright',src['path']],text=True,timeout=60))[0]
                    with Path(src['path']).open('rb') as inp,(U/relative).open('xb') as out:shutil.copyfileobj(inp,out,1024*1024)
                else:
                    with Image.open(U/s['master_path']) as im,(U/relative).open('xb') as out:
                        ImageOps.exif_transpose(im).convert('RGB').save(out,'JPEG',quality=92,subsampling=0)
                embed(U/relative,embedded)
                if kind=='master':
                    with Image.open(src['path']) as original,Image.open(U/relative) as output:
                        assert original.mode==output.mode=='L' and original.size==output.size and original.tobytes()==output.tobytes()
                        assert original.getexif().get(274,1)==output.getexif().get(274,1)
                with Image.open(U/relative) as im: im.load(); width,height=im.size; mode=im.mode
                with (U/relative).open('rb') as inp:digest=hashlib.file_digest(inp,'sha256').hexdigest()
                f=dict(path=relative, role='preservation_master' if kind=='master' else 'access_derivative',
                    sha256=digest, stamp=stamp(U/relative), size_bytes=(U/relative).stat().st_size,
                    width=width,height=height,mode=mode,bit_depth=8,media_type='image/tiff' if kind=='master' else 'image/jpeg',
                    compression=s['technical']['compression'] if kind=='master' else 'JPEG',
                    occurrence_id='OCC-'+uuid4().hex.upper(), observed_mtime_utc=datetime.fromtimestamp((U/relative).stat().st_mtime,timezone.utc).isoformat())
                f['provenance']=dict(source_path=src['path'], original_filename=Path(src['path']).name,
                    source_sha256=src['sha256'], source_size_bytes=src['size_bytes'], source_mtime_ns=src['mtime_ns'],
                    carrier_inventory=obj['inventory'], entity_id=obj['entity_id'], representation_part=s['part'],
                    master_path=s['master_path'], selected_master_members=obj['selected_master_members'],
                    derivation='metadata_enriched_selected_master' if kind=='master' else 'generated_from_selected_master',
                    accession_receipt=RECEIPT, path_base=str(U), coverage_complete=False, deletion_authorized=False)
                s['media'].append(f);save(receipt)
            review.verify_asset(src)
            print('Accessioned section',Path(src['path']).name,flush=True)


def register_media(con,receipt):
    now=receipt['started_at']
    for o in receipt['carriers']:
        for s in o['sections']:
            for f in s['media']:
                digest='sha256:'+f['sha256']
                existing=con.execute('SELECT * FROM file_occurrences WHERE occurrence_id=?',(f['occurrence_id'],)).fetchone()
                if existing:
                    assert existing['relative_path']==f['path'] and existing['content_digest']==digest
                    continue
                con.execute('INSERT INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (digest,f['size_bytes'],f['media_type'],f['width'],f['height'],8,f['mode'],f['compression'],1,now))
                con.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (f['occurrence_id'],'WF-PARAGON',digest,'archive',f['path'],Path(f['path']).name,f['observed_mtime_utc'],now,'verified',json.dumps(f['provenance'],ensure_ascii=False)))
                con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)',(o['entity_id'],f['occurrence_id'],f['role'],'active',now))
    if not con.execute('SELECT 1 FROM logbook WHERE log_id=?',(receipt['log_id'],)).fetchone():
        result=dict(carriers=receipt['carriers'],ready_drain_decisions=receipt['ready_drain_decisions'],
            source_disposition_rows=receipt['source_disposition_rows'],accession_receipt=RECEIPT,deletion_authorized=False)
        con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (receipt['log_id'],now,'Vladimir','WF-PARAGON','accession_carrier_sections','archive_unit','WF-PARAGON',
             json.dumps(receipt['prior_source_rows'],ensure_ascii=False),json.dumps(result,ensure_ascii=False),RECEIPT,SCOPE))
    assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()


def disposition_rows(receipt):
    changed=copy.deepcopy(receipt['prior_source_rows'])
    for o in receipt['carriers']:
        for s in o['sections']:
            row=changed[s['source']['path']]
            row.update(inventory=o['inventory'],source_role='selected_master_candidate',final_path=s['master_path'],
                final_sha256='sha256:'+s['media'][0]['sha256'],preservation_status='preserved_as_metadata_enriched_master',
                preservation_basis='Full source pixels preserved as a complementary section of the single selected carrier Master; separate photograph Masters retained.')
    for p in receipt['ready_drain_decisions']:
        row=changed[p['path']]
        if p['disposition']=='noise_drain':
            row.update(source_role='noise',final_path='',final_sha256='',preservation_status='reviewed_noise_not_accessioned',
                preservation_basis='Vladimir explicitly marked this exact file as noise for the drain list. No preserved coverage is claimed.')
        else:
            target=p['derivative_evidence']['coverage_target']
            assert stamp(target['path'])==p['verified_target_stamp']
            row.update(source_role='redundant_candidate',final_path=str(Path(target['path']).relative_to(U)),
                final_sha256='sha256:'+target['sha256'].removeprefix('sha256:'),preservation_status='redundant_after_verified_derivative_comparison',
                preservation_basis='Verified image copy/reduced export covered by the accessioned Master; exact comparison and saved decision are recorded in '+RECEIPT+'.')
    for row in changed.values():
        row.update(exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',
            notes='Applied saved decision on 2026-09-18. Receipt: '+RECEIPT+'. Source retained; deletion is not authorized.')
    return changed


def update_dispositions(receipt):
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv')
    changed=receipt['source_disposition_rows']
    rows=[changed.get(r['source_path'],r) for r in rows]
    for path in [U/'db-restore/manifests/source-disposition.csv',W/'source-disposition.planned.csv']:write_csv(path,fields,rows)
    write_csv(W/'drain-list.csv',fields,[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    for path in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:
        evidence=json.loads(path.read_text())
        for e in evidence:
            if e['source_path'] in changed:
                row=changed[e['source_path']]
                e.update(accession_receipt=RECEIPT,final_path=row['final_path'],final_sha256=row['final_sha256'],
                    preservation_status=row['preservation_status'],decision_log_id=receipt['log_id'])
        review.write_json(path,evidence)
    counts=dict(total=len(rows),candidate_rows=sum(r['drain_state']=='candidate_after_separate_review' for r in rows),
        retained_rows=sum(r['drain_state']!='candidate_after_separate_review' for r in rows))
    assert counts==dict(total=1419,candidate_rows=382,retained_rows=1037)
    receipt['source_disposition_counts']=counts
    summary=json.loads((W/'drain-review-summary.json').read_text())
    summary.update(candidate_rows=382,retain_rows=1037,
        candidate_source_bytes=sum(int(r['source_size_bytes']) for r in rows if r['drain_state']=='candidate_after_separate_review'),
        retained_source_bytes=sum(int(r['source_size_bytes']) for r in rows if r['drain_state']!='candidate_after_separate_review'),
        preservation_status=dict(Counter(r['preservation_status'] for r in rows)),source_roles=dict(Counter(r['source_role'] for r in rows)),
        retained_sections_accession=RECEIPT,current_register='Work and final source-disposition CSVs agree; 382 review candidates, no deletion authorized.')
    review.write_json(W/'drain-review-summary.json',summary);save(receipt)


def export_records(receipt):
    oh,objects=read_csv(U/'metadata/objects.csv');ch,catalog=read_csv(U/'CATALOG.csv')
    rh,restore=read_csv(U/'db-restore/RESTORE.csv');ph,reps=read_csv(U/'db-restore/manifests/representations.csv')
    assert len(catalog)==535 and len(restore)==525 and len(reps)==262
    with connect(U/'db-restore/RESTORE.sqlite') as con:
        revision=con.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
        sql='\n'.join(con.iterdump())+'\n'
    con.close()
    for o in receipt['carriers']:
        carrier=next(r for r in objects if r['iss_id']==o['entity_id'])
        overview=next(r for r in catalog if r['final_relative_path']==o['overview'])
        prior_restore=next(r for r in restore if r['iss_id']==o['entity_id'] and r['representation_role']=='preservation_surrogate')
        overview['relationship']='member of the single selected Master for '+o['inventory']+'; part=carrier overview (surrogate)'
        n=len(o['sections'])
        carrier.update(master_file='\n'.join(o['selected_master_members']),
            access_file=carrier['access_file']+'\n'+'\n'.join(s['access_path'] for s in o['sections']),
            master_member_count=str(n+1),master_parts='carrier overview (surrogate)\n'+'\n'.join(s['part'] for s in o['sections']),
            representation_summary=f'access_derivative: {n+1}; preservation_master: {n}; preservation_surrogate: 1',
            notes_for_review=carrier['notes_for_review']+'; retained sections accessioned; complete high-resolution carrier coverage remains unresolved')
        for sequence,s in enumerate(o['sections'],2):
            master,access=s['media']
            for f in s['media']:
                is_master=f['role']=='preservation_master'
                row={key:'' for key in ch}
                for key in ['record_key','inventory','title','date_text','creator','object_type','material','dimensions','places','tags','md_object_id','md_url','rights','rights_holder','access_restriction']:
                    row[key]=overview[key]
                row.update(file_role=f['role'],final_relative_path=f['path'],master_relative_path=s['master_path'],filename=Path(f['path']).name,
                    file_type='tif' if is_master else 'jpg',size_bytes=str(f['size_bytes']),final_sha256=f['sha256'],
                    description=s['embedded_metadata']['description'],metadata_authority='Vladimir: saved carrier-section assignment; existing carrier metadata retained',
                    original_source_path=s['source']['path'] if is_master else str(U/s['master_path']),
                    source_sha256=s['source']['sha256'].removeprefix('sha256:') if is_master else master['sha256'],
                    derivative_of='' if is_master else s['master_path'],sequence=str(sequence),width=str(f['width']),height=str(f['height']),
                    relationship='member of the single selected Master for '+o['inventory']+'; part='+s['part'] if is_master else 'generated access image of carrier-section Master member',
                    condition='Partial carrier coverage; complete high-resolution coverage unresolved.',
                    notes='Full raw section preserved; existing overview and separate photograph Masters unchanged.',
                    metadata_path='metadata/objects.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/source-disposition.csv')
                catalog.append(row)
                rr=dict(prior_restore)
                rr.update(representation_role=f['role'],relative_path=f['path'],original_filename=Path(f['path']).name,sha256=f['sha256'],
                    size_bytes=str(f['size_bytes']),media_type=f['media_type'],width_px=str(f['width']),height_px=str(f['height']),
                    bit_depth='8',color_space=f['mode'],compression=f['compression'],observed_mtime_utc=f['observed_mtime_utc'],
                    provenance_note=json.dumps(f['provenance'],ensure_ascii=False),provenance_status='occurrence_note_available',source_root_id='',source_batch_id='')
                restore.append(rr)
            reps.append(dict(zip(ph,[o['entity_id'],o['inventory'],'preservation_master',s['master_path'],'sha256:'+master['sha256'],
                s['access_path'],'sha256:'+access['sha256'],s['master_path'],'generated_from_selected_master','local_access',str(access['width']),str(access['height']),
                'paragon/accession_retained_sections.py',receipt['started_at']])))
    for row in restore:row['catalog_revision']=str(revision)
    for path,fields,rows in [('metadata/objects.csv',oh,objects),('CATALOG.csv',ch,catalog),('db-restore/RESTORE.csv',rh,restore),('db-restore/manifests/representations.csv',ph,reps)]:write_csv(U/path,fields,rows)
    memory=sqlite3.connect(':memory:');memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    receipt['workbook_checks']={
        'CATALOG.xlsx':update_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)]),
        'db-restore/RESTORE.xlsx':update_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])}
    wrap_new_hashes(receipt)
    wb=load_workbook(U/'CATALOG.xlsx');sheet=wb['Objects'];headers=[c.value for c in sheet[1]]
    for ri,row in enumerate(objects,2):
        if row['archival_label'] not in EXPECTED:continue
        for field in ['representation_summary','master_file','access_file','master_parts','notes_for_review']:
            cell=sheet.cell(ri,headers.index(field)+1);alignment=copy.copy(cell.alignment);alignment.wrap_text=True;cell.alignment=alignment
        sheet.row_dimensions[ri].height=max(sheet.row_dimensions[ri].height or 23,(EXPECTED[row['archival_label']]+1)*32+12)
    wb.save(U/'CATALOG.xlsx');wb.close()
    receipt['counts']=dict(objects=len(objects),represented_objects=sum(bool(r['master_file']) for r in objects),
        master_members=len(reps),access_files=len(reps),catalog_rows=len(catalog),restore_rows=len(restore),
        new_objects=0,carriers_extended=4,new_master_members=16,new_access_derivatives=16,drain_choices_applied=38)
    receipt['catalog_revision']=revision;save(receipt)


def verify_core(receipt):
    app.mount_guard()
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==receipt['original_directories']
    assert stamp(W/'retain-review.csv')==receipt['stable_review_source_stamp']
    changed=set(CONTROLS)|{'db-restore/checksums.csv'}
    for p,before in receipt['original_files'].items():
        if p not in changed:assert stamp(U/p)==before,('Unrelated archive file changed',p)
    with app.connect() as con:assert review.saved_reviews(con)==receipt['prior_reviews']
    ch,catalog=read_csv(U/'CATALOG.csv');oh,objects=read_csv(U/'metadata/objects.csv');rh,restore=read_csv(U/'db-restore/RESTORE.csv')
    verify_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)])
    verify_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])
    assert len(catalog)==567 and len(objects)==260 and len(restore)==557
    assert sum(int(r['master_member_count']) for r in objects)==278
    for db,prior in receipt['prior_databases'].items():
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
            assert [dict(r) for r in con.execute('SELECT * FROM archive_objects ORDER BY entity_id')]==prior['objects']
            assert [dict(r) for r in con.execute('SELECT * FROM entity_relations ORDER BY relation_id')]==prior['relations']
            files=[dict(r) for r in con.execute('SELECT * FROM entity_files ORDER BY entity_id,occurrence_id')]
            assert len(files)==len(prior['files'])+32 and all(f in files for f in prior['files'])
            log=json.loads(con.execute('SELECT after_json FROM logbook WHERE log_id=?',(receipt['log_id'],)).fetchone()[0])
            assert log['source_disposition_rows']==receipt['source_disposition_rows']
            for o in receipt['carriers']:
                assert len(file_rows(con,o['entity_id']))==2*len(o['selected_master_members'])
                obj=next(r for r in objects if r['iss_id']==o['entity_id'])
                assert obj['master_file'].splitlines()==o['selected_master_members']
                for s in o['sections']:
                    review.verify_asset(s['source'])
                    for f in s['media']:
                        assert stamp(U/f['path'])==f['stamp']
                        cat=next(r for r in catalog if r['final_relative_path']==f['path'])
                        rest=next(r for r in restore if r['relative_path']==f['path'])
                        assert cat['final_sha256']==rest['sha256']==f['sha256']
                        assert con.execute('SELECT content_digest FROM file_occurrences WHERE occurrence_id=?',(f['occurrence_id'],)).fetchone()[0]=='sha256:'+f['sha256']
        con.close()
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    register={r['source_path']:r for r in read_csv(U/'db-restore/manifests/source-disposition.csv')[1]}
    assert len(register)==1419 and all(r['deletion_authorized']=='false' for r in register.values())
    assert sum(r['drain_state']=='candidate_after_separate_review' for r in register.values())==382
    for p,row in receipt['source_disposition_rows'].items():assert register[p]==row
    for p in receipt['ready_drain_decisions']:
        review.verify_asset(review.evidence()['assets'][p['path']])
        if p['disposition']=='noise_drain':assert not register[p['path']]['final_path'] and not register[p['path']]['final_sha256']
        else:assert stamp(p['derivative_evidence']['coverage_target']['path'])==p['verified_target_stamp']
    return dict(ok=True,**receipt['counts'],source_register=receipt['source_disposition_counts'],saved_reviews_preserved=132,
        source_modified=False,deletion_authorized=False,directory_structure_unchanged=True,prior_objects_and_relationships_unchanged=True)


def finish(receipt):
    receipt['validation']=verify_core(receipt)
    now=datetime.now(timezone.utc).isoformat()
    receipt.update(completed_at=now,validation_result='ok',accession_performed=True,final_payload_hashes_computed_once=32,
        existing_media_unchanged=True,source_modified=False,sql_restore_verified=True)
    text=(U/'00_README.md').read_text();heading,body=text.split('\n',1)
    intro='\n\n## Retained carrier sections and drain decisions — 2026-09-18\n\nSixteen selected TIFF sections were accessioned to four existing carriers, each\nwith one selected Master containing its overview and complementary sections:\nVLV-1148: 7 members; VLV-1151: 2; VLV-1155: 5; VLV-1156: 6.\nSixteen access JPEGs were generated from those new sections. Complete high-resolution\ncarrier coverage remains unresolved; separate photograph Masters are unchanged.\n\nCurrent totals: 260 objects, 259 represented; 278 Master-member files and 278 access\nimages. The source register has 382 drain-review candidates and 1,037 retained\npaths. This update includes 26 verified derivatives and 12 explicit noise decisions;\nnoise is recorded without claiming preserved coverage. All deletion flags remain\nfalse. No source changes or public publication occurred. Receipt:\n`'+RECEIPT+'`. Earlier receipts describe their own checkpoints.\n\n'
    (U/'00_README.md').write_text(heading+intro+body)
    kh,checks=read_csv(U/'db-restore/checksums.csv');checksum={r['path']:r for r in checks}
    for o in receipt['carriers']:
        for s in o['sections']:
            for f in s['media']:checksum[f['path']]=dict(path=f['path'],sha256='sha256:'+f['sha256'],size_bytes=str(f['size_bytes']))
    export=json.loads((U/'catalog-export-summary.json').read_text())
    export.update(updated_at=now,file_rows=567,object_rows=260,retained_sections_accession=dict(carriers=4,master_members_added=16,new_derivatives=16,receipt=RECEIPT,deletion_authorized=False))
    summary=json.loads((U/'db-restore/export-summary.json').read_text())
    summary.update(updated_at=now,masters=278,access=278,generated_access=45,
        catalog_state=dict(catalog_revision=receipt['catalog_revision'],published_revision=receipt['catalog_revision'],dirty_since=None),
        retained_sections_accession=export['retained_sections_accession'],source_disposition_review=receipt['source_disposition_counts']|{'receipt':RECEIPT})
    summary['restore']['rows']=557;summary['checksums']=len(set(checksum)|set(CONTROLS))
    review.write_json(U/'db-restore/export-summary.json',summary)
    save(receipt);review.write_json(U/RECEIPT,receipt)
    for path in CONTROLS:
        if path=='catalog-export-summary.json':continue
        with (U/path).open('rb') as inp:digest=hashlib.file_digest(inp,'sha256').hexdigest()
        checksum[path]=dict(path=path,sha256='sha256:'+digest,size_bytes=str((U/path).stat().st_size))
    for value in export['outputs'].values():value['sha256']=checksum[str(Path(value['path']).relative_to(U))]['sha256']
    review.write_json(U/'catalog-export-summary.json',export)
    with (U/'catalog-export-summary.json').open('rb') as inp:digest=hashlib.file_digest(inp,'sha256').hexdigest()
    checksum['catalog-export-summary.json']=dict(path='catalog-export-summary.json',sha256='sha256:'+digest,size_bytes=str((U/'catalog-export-summary.json').stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checksum.values(),key=lambda r:r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:review.export_decisions(con)


def verify():
    receipt=json.loads((U/RECEIPT).read_text())
    assert receipt['validation_result']=='ok' and json.loads(WORK_RECEIPT.read_text())==receipt
    result=verify_core(receipt)
    checks={r['path']:r for r in read_csv(U/'db-restore/checksums.csv')[1]}
    for p,r in checks.items():assert (U/p).stat().st_size==int(r['size_bytes']),p
    for o in receipt['carriers']:
        for s in o['sections']:
            for f in s['media']:assert checks[f['path']]['sha256']=='sha256:'+f['sha256']
    with app.connect() as con:result['workbench']=review.check(con)
    return result


def apply():
    receipt=prepare();make_media(receipt)
    receipt['source_disposition_rows']=disposition_rows(receipt);save(receipt)
    for db in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        with connect(db) as original:
            memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row;original.backup(memory)
            memory.execute('PRAGMA foreign_keys=ON');register_media(memory,receipt);memory.close()
        original.close()
    for db in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        with connect(db,True) as con:
            register_media(con,receipt)
            if db.is_relative_to(U):con.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
        con.close()
    export_records(receipt);update_dispositions(receipt);finish(receipt)
    result=verify();review.write_json(W/'retained-sections-validation.json',result)
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.verify:print(json.dumps(verify(),indent=2))
    elif args.apply:apply()
    else:
        r=prepare()
        print(json.dumps(dict(ready=True,carriers=4,sections=16,ready_drain_decisions=38,new_directories=0,new_schema=False,scope=SCOPE),indent=2))
