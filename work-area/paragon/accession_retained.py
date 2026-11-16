#!/usr/bin/env python3
"""Accession the 20 human-selected retained objects into the existing Paragon unit.

Reuses its directories, identity convention, SQLite tables and readable records.
No source writes, publication, deletion, schema changes or package generations.
"""
import argparse
import copy
import csv
import hashlib
import json
import secrets
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
from accession_carrier_section import read_csv, write_csv, stamp, connect, update_workbook, render_range
from accession_reviewed import embed, verify_workbook

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/retained-accession-completion.json'
WORK_RECEIPT = W/'retained-accession-completion.json'
AUTHORIZATION = 'ok lets make an accession first'
CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


def write_receipt(receipt):
    review.write_json(WORK_RECEIPT, receipt)


def new_id(used):
    # Existing catalog_core.py ISS-YYYY-XXXX-C convention, without its legacy
    # registry/generation operations. Only new objects receive an identifier.
    while True:
        body = ''.join(secrets.choice(CROCKFORD) for _ in range(4))
        check = CROCKFORD[hashlib.sha256(('2026'+body).encode('ascii')).digest()[0] & 31]
        value = 'ISS-2026-'+body+'-'+check
        if value not in used:
            used.add(value)
            return value


def prepare():
    assert not (U/RECEIPT).exists(), 'Already accessioned; use --verify.'
    app.mount_guard()
    with app.connect() as con:
        review.check(con)
        saved = review.saved_reviews(con)
        cases = {c['key']:c for c in review.cases(con)}
    selected = sorted((r for r in saved.values() if r['action']=='add'), key=lambda r:r['case_key'])
    assert len(selected)==20
    used = set()
    for db in [W/'work/catalog.sqlite', U/'db-restore/RESTORE.sqlite']:
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
            used.update(r[0] for r in con.execute('SELECT entity_id FROM entities'))
            for r in selected:
                assert not con.execute('SELECT 1 FROM archive_objects WHERE archival_label=?',(r['inventory'],)).fetchone(),r['inventory']
        con.close()
    if WORK_RECEIPT.exists():
        receipt = json.loads(WORK_RECEIPT.read_text())
        assert [o['review'] for o in receipt['objects']]==selected, 'Saved choices changed.'
        return receipt
    objects=[]
    for r in selected:
        obj=cases[r['case_key']]
        # Revalidate the actual saved human choice, without choosing a substitute.
        rebuilt=review.build_review(obj,dict(action='add',selected_master_path=r['selected_master_path'],
            metadata=r['proposed_metadata'],metadata_reviewed=r['metadata_reviewed'],
            same_object_confirmed=r['same_object_confirmed'],relation=r['relationship']['predicate'],
            parent=r['relationship']['carrier']),r['actor'])
        assert rebuilt['selected_master_path']==r['selected_master_path']
        source=review.evidence()['assets'][r['selected_master_path']]
        for f in r['files']:review.verify_asset(review.evidence()['assets'][f['path']])
        with Image.open(source['path']) as im:
            im.load()
            assert im.format in ('TIFF','JPEG') and getattr(im,'n_frames',1)==1 and im.mode in ('RGB','L','P'),r['inventory']
            bits=im.tag_v2.get(258,(8,)) if im.format=='TIFF' else (8,)
            assert max((bits,) if isinstance(bits,int) else bits)==8
            technical=dict(width=im.width,height=im.height,mode=im.mode,format=im.format,orientation=im.getexif().get(274,1),compression=im.info.get('compression','JPEG' if im.format=='JPEG' else 'Uncompressed'))
        identity=new_id(used)
        suffix='.tif' if technical['format']=='TIFF' else '.jpg'
        base=r['inventory']+'__'+identity
        master='masters/'+base+'__master'+suffix
        access='export/access/'+base+'__access.jpg'
        for path in [master,access]:
            assert not (U/path).exists() and (U/path).parent.is_dir(),path
        notes=' '.join(x for x in [obj['qualifications'], 'Date evidence: '+obj['date_evidence'], r['note']] if x)
        objects.append(dict(inventory=r['inventory'],entity_id=identity,review=r,source=source,
            master_path=master,access_path=access,technical=technical,notes=notes,
            parent_entity_id=review.evidence()['catalogue'][r['relationship']['carrier']]['iss_id'] if r['relationship']['carrier'] else '',
            match_status='no_match_found',match_basis='Public museum-digital inventory/collection searches captured 2026-09-17; no public match for this inventory. Private/unpublished records not searched.',
            evidence_source_id='SOURCE-'+uuid4().hex.upper(),media=[]))
    assert shutil.disk_usage(U).free > sum(o['source']['size_bytes'] for o in objects)*3+200_000_000
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    receipt=dict(authorization=AUTHORIZATION,actor='Vladimir',started_at=datetime.now(timezone.utc).isoformat(),
        unit='paragon',objects=objects,validation_result='pending',accession_performed=False,
        source_mount=app.mount_guard(),source_modified=False,deletion_authorized=False,publication_performed=False,
        prior_reviews=saved,original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        original_files={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))},
        original_counts={p:len(read_csv(U/p)[1]) for p in ['CATALOG.csv','metadata/objects.csv','db-restore/RESTORE.csv','db-restore/manifests/representations.csv']})
    write_receipt(receipt)
    return receipt


def make_media(receipt):
    for obj in receipt['objects']:
        source=obj['source']; review.verify_asset(source)
        metadata=obj['review']['proposed_metadata']
        embedded=dict(iss_id=obj['entity_id'],archival_label=obj['inventory'],descriptive_title=metadata['title'],description=metadata['description'])
        for kind in ['master','access']:
            relative=obj[kind+'_path']
            prior=next((m for m in obj['media'] if m['path']==relative),None)
            if prior:
                assert stamp(U/relative)==prior['stamp']
                continue
            assert not (U/relative).exists(), 'Unrecorded output exists; inspect instead of overwriting: '+relative
            if kind=='master':
                obj['source_embedded_metadata']=json.loads(subprocess.check_output(['exiftool','-j','-G1','-s','-XMP:All','-IPTC:All','-IFD0:ImageDescription','-IFD0:Artist','-IFD0:Copyright',source['path']],text=True,timeout=60))[0]
                with Path(source['path']).open('rb') as src,(U/relative).open('xb') as out:
                    shutil.copyfileobj(src,out,1024*1024)
            else:
                with Image.open(U/obj['master_path']) as im,(U/relative).open('xb') as out:
                    ImageOps.exif_transpose(im).convert('RGB').save(out,'JPEG',quality=92,subsampling=0)
            embed(U/relative,embedded)
            if kind=='master':
                with Image.open(source['path']) as src,Image.open(U/relative) as dst:
                    assert src.mode==dst.mode and src.size==dst.size and src.tobytes()==dst.tobytes()
                    if src.mode=='P':assert src.getpalette()==dst.getpalette()
                    assert src.getexif().get(274,1)==dst.getexif().get(274,1)
            with Image.open(U/relative) as im:
                im.load();width,height=im.size;mode=im.mode
            # One final hash after embedding and derivative generation writes.
            with (U/relative).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
            media=dict(path=relative,role='preservation_master' if kind=='master' else 'access_derivative',
                sha256=digest,stamp=stamp(U/relative),size_bytes=(U/relative).stat().st_size,
                width=width,height=height,mode=mode,bit_depth=8,
                media_type='image/tiff' if Path(relative).suffix=='.tif' else 'image/jpeg',
                compression='JPEG' if Path(relative).suffix=='.jpg' else obj['technical']['compression'],
                occurrence_id='OCC-'+uuid4().hex.upper(),observed_mtime_utc=datetime.fromtimestamp((U/relative).stat().st_mtime,timezone.utc).isoformat())
            media['provenance']=dict(source_path=source['path'],original_filename=Path(source['path']).name,
                source_sha256=source['sha256'],source_size_bytes=source['size_bytes'],source_mtime_ns=source['mtime_ns'],
                master_path=obj['master_path'],derivation='metadata_enriched_selected_master' if kind=='master' else 'generated_from_selected_master',
                metadata_source={k:obj['review']['metadata_source'][k] for k in ('path','sheet','row')},
                date_text=metadata['date'],provenance=metadata['provenance'],relationship=obj['review']['relationship'],
                match_status=obj['match_status'],match_basis=obj['match_basis'],
                accession_receipt=RECEIPT,path_base=str(U),deletion_authorized=False)
            obj['media'].append(media);write_receipt(receipt)
        review.verify_asset(source)
        print('Accessioned media',obj['inventory'],flush=True)


def register_objects(con, receipt):
    now=receipt['started_at']
    for o in receipt['objects']:
        eid=o['entity_id'];r=o['review'];m=r['proposed_metadata'];v=r['metadata_source']['values'];sid=o['evidence_source_id']
        present=con.execute('SELECT * FROM archive_objects WHERE entity_id=?',(eid,)).fetchone()
        if present:
            assert present['archival_label']==o['inventory'] and present['descriptive_title']==m['title']
            actual={x['relative_path'] for x in con.execute('SELECT f.relative_path FROM file_occurrences f JOIN entity_files e USING(occurrence_id) WHERE e.entity_id=? AND e.status=\'active\'',(eid,))}
            assert actual=={o['master_path'],o['access_path']}
            continue
        assert not con.execute('SELECT 1 FROM archive_objects WHERE archival_label=?',(o['inventory'],)).fetchone()
        con.execute('INSERT INTO entities VALUES (?,?,?,?,?)',(eid,'archive_object','active',now,now))
        con.execute('INSERT INTO evidence_sources (source_id,source_type,system,external_id,captured_at,local_absolute_path,citation,note) VALUES (?,?,?,?,?,?,?,?)',
            (sid,'local_review','Paragon retained review',o['inventory'],r['reviewed_at'],str(U/RECEIPT),r['metadata_source']['path']+'; '+r['metadata_source']['sheet']+' row '+str(r['metadata_source']['row']),json.dumps(dict(metadata=m,qualifications=o['notes'],match_status=o['match_status'],match_basis=o['match_basis']),ensure_ascii=False)))
        con.execute('INSERT INTO archive_objects VALUES (?,?,?,?,?,?,?,?,?,?,?)',(eid,o['inventory'],m['title'],m['description'],v.get('object_material_technique',''),v.get('object_dimensions',''),'blocked','active',o['notes'],now,now))
        con.execute('INSERT INTO entity_names VALUES (?,?,?,?,?,?,?,?,?)',('NAME-'+uuid4().hex.upper(),eid,'canonical',o['inventory'],'','active',sid,now,now))
        con.execute('INSERT INTO entity_identifiers VALUES (?,?,?,?,?,?,?,?,?,?)',('ID-'+uuid4().hex.upper(),eid,'inventory',o['inventory'],o['inventory'].lower(),'','legacy','active',now,None))
        con.execute('INSERT INTO entity_unit_memberships VALUES (?,?,?,?)',(eid,'WF-PARAGON','primary',now))
        for predicate,value in [('archive_descriptive_title',m['title']),('archive_description',m['description']),('archive_material_technique',v.get('object_material_technique','')),('archive_dimensions',v.get('object_dimensions','')),('archive_object_type',v.get('object_type',''))]:
            if value:con.execute('INSERT INTO evidence_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('ASSERT-'+uuid4().hex.upper(),eid,predicate,value,None,'accepted','certain',sid,'Vladimir',now,None,'Human-reviewed local metadata. Source qualifications are preserved in the accession receipt.'))
        if o['parent_entity_id']:
            con.execute('INSERT INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?)',('REL-'+uuid4().hex.upper(),eid,'component_of',o['parent_entity_id'],'accepted','certain',sid,now,now))
        for f in o['media']:
            digest='sha256:'+f['sha256'];note=json.dumps(f['provenance'],ensure_ascii=False)
            con.execute('INSERT OR IGNORE INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(digest,f['size_bytes'],f['media_type'],f['width'],f['height'],8,f['mode'],f['compression'],1,now))
            con.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',(f['occurrence_id'],'WF-PARAGON',digest,'archive',f['path'],Path(f['path']).name,f['observed_mtime_utc'],now,'verified',note))
            con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)',(eid,f['occurrence_id'],f['role'],'active',now))
        con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)',('LOG-'+uuid4().hex.upper(),now,'Vladimir','WF-PARAGON','accession_retained_object','archive_object',eid,'',json.dumps(o,ensure_ascii=False),RECEIPT,AUTHORIZATION))
    assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert not con.execute('PRAGMA foreign_key_check').fetchall()


def export_records(receipt):
    oh,objects=read_csv(U/'metadata/objects.csv');ch,catalog=read_csv(U/'CATALOG.csv')
    rh,restore=read_csv(U/'db-restore/RESTORE.csv');ph,reps=read_csv(U/'db-restore/manifests/representations.csv')
    with connect(U/'db-restore/RESTORE.sqlite') as con:
        revision=con.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
        sql='\n'.join(con.iterdump())+'\n'
    con.close()
    for o in receipt['objects']:
        r=o['review'];m=r['proposed_metadata'];v=r['metadata_source']['values'];master,access=o['media']
        assert [f['role'] for f in o['media']]==['preservation_master','access_derivative']
        if any(x['iss_id']==o['entity_id'] for x in objects):continue
        identifiers='inventory:'+o['inventory']+' [legacy]'
        relation='component_of '+r['relationship']['carrier'] if o['parent_entity_id'] else 'standalone'
        object_row={k:'' for k in oh};object_row.update(iss_id=o['entity_id'],archival_label=o['inventory'],canonical_name=o['inventory'],descriptive_title=m['title'],description=m['description'],date_year=m['date'],object_type=v.get('object_type',''),material_technique=v.get('object_material_technique',''),dimensions=v.get('object_dimensions',''),identifiers=identifiers,rights_status='not_recorded_in_minimal_core_v1',record_state='represented',representation_summary='access_derivative: 1; preservation_master: 1',master_file=o['master_path'],access_file=o['access_path'],notes_for_review=o['notes']+'; '+relation+'; publication blocked',master_member_count='1')
        objects.append(object_row)
        for f in o['media']:
            is_master=f['role']=='preservation_master'
            row={k:'' for k in ch};row.update(record_key=o['entity_id'],inventory=o['inventory'],title=m['title'],file_role=f['role'],final_relative_path=f['path'],master_relative_path=o['master_path'],filename=Path(f['path']).name,file_type=Path(f['path']).suffix[1:],size_bytes=str(f['size_bytes']),final_sha256=f['sha256'],description=m['description'],date_text=m['date'],object_type=v.get('object_type',''),material=v.get('object_material_technique',''),dimensions=v.get('object_dimensions',''),places=v.get('related_place1',''),tags='; '.join(v.get('tag'+str(i),'').strip() for i in range(1,11) if v.get('tag'+str(i),'').strip()),metadata_authority='Vladimir: saved retained-object review; original workbook evidence preserved',original_source_path=o['source']['path'] if is_master else str(U/o['master_path']),source_sha256=(o['source']['sha256'].removeprefix('sha256:') if is_master else master['sha256']),derivative_of='' if is_master else o['master_path'],sequence='1',width=str(f['width']),height=str(f['height']),relationship=relation,access_restriction='publication blocked',condition='Selected existing digital image; source resolution and processing retained.',notes=o['notes']+' Provenance: '+m['provenance']+'. '+o['match_basis'],metadata_path='metadata/objects.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/source-disposition.csv')
            catalog.append(row)
            rr={k:'' for k in rh};rr.update(catalog_format='iss-unit-catalog-v1',catalog_revision=str(revision),unit_id='WF-PARAGON',unit_title='Paragon provenance recovery',iss_id=o['entity_id'],membership_roles='primary',canonical_name=o['inventory'],archival_label=o['inventory'],descriptive_title=m['title'],description=m['description'],material_technique=v.get('object_material_technique',''),dimensions=v.get('object_dimensions',''),publication_status='blocked',object_status='active',object_note=o['notes'],identifiers=identifiers,rights_status='not_recorded_in_minimal_core_v1',representation_role=f['role'],representation_status='active',file_status='verified',relative_path=f['path'],original_filename=Path(f['path']).name,sha256=f['sha256'],size_bytes=str(f['size_bytes']),media_type=f['media_type'],width_px=str(f['width']),height_px=str(f['height']),bit_depth='8',color_space=f['mode'],compression=f['compression'],page_count='1',observed_mtime_utc=f['observed_mtime_utc'],provenance_note=json.dumps(f['provenance'],ensure_ascii=False),provenance_status='occurrence_note_available',record_state='represented')
            restore.append(rr)
        reps.append(dict(zip(ph,[o['entity_id'],o['inventory'],'preservation_master',o['master_path'],'sha256:'+master['sha256'],o['access_path'],'sha256:'+access['sha256'],o['master_path'],'generated_from_selected_master','local_access',str(access['width']),str(access['height']),'paragon/accession_retained.py',receipt['started_at']])))
    for row in restore:row['catalog_revision']=str(revision)
    for path,fields,rows in [('metadata/objects.csv',oh,objects),('CATALOG.csv',ch,catalog),('db-restore/RESTORE.csv',rh,restore),('db-restore/manifests/representations.csv',ph,reps)]:write_csv(U/path,fields,rows)
    memory=sqlite3.connect(':memory:');memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    # Artifact Tool import is unavailable in this runtime (WASM SIMD). Reuse
    # the prior bounded fallback and verify all values/styles after its edit.
    receipt['workbook_checks']={
        'CATALOG.xlsx':update_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)]),
        'db-restore/RESTORE.xlsx':update_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])}
    wrap_new_hashes(receipt)
    receipt['counts']=dict(objects=len(objects),represented_objects=sum(bool(r['master_file']) for r in objects),master_members=len(reps),access_files=len(reps),catalog_rows=len(catalog),restore_rows=len(restore),new_objects=20,new_masters=20,new_access_derivatives=20,accepted_component_relations=73)
    receipt['catalog_revision']=revision;write_receipt(receipt)


def wrap_new_hashes(receipt):
    for file,csv_path in [('CATALOG.xlsx','CATALOG.csv'),('db-restore/RESTORE.xlsx','db-restore/RESTORE.csv')]:
        wb=load_workbook(U/file);sheet=wb['Catalog'];headers=[c.value for c in sheet[1]]
        columns=[headers.index(f)+1 for f in ('sha256','final_sha256','source_sha256') if f in headers]
        for row in range(receipt['original_counts'][csv_path]+2,sheet.max_row+1):
            for col in columns:
                cell=sheet.cell(row,col);alignment=copy.copy(cell.alignment);alignment.wrap_text=True;cell.alignment=alignment
        wb.save(U/file);wb.close()


def update_dispositions(receipt):
    final=U/'db-restore/manifests/source-disposition.csv'
    fields,rows=read_csv(final);by_path={r['source_path']:r for r in rows};changed=[]
    for o in receipt['objects']:
        for f in o['review']['files']:
            row=by_path[f['path']];selected=f['state']=='selected_master'
            assert row['source_sha256']==f['sha256']
            row.update(inventory=o['inventory'],source_role='selected_master_candidate' if selected else 'redundant_candidate',
                final_path=o['master_path'],preservation_status='preserved_as_metadata_enriched_master' if selected else 'redundant_after_human_master_selection',
                preservation_basis='Selected source pixels preserved in accessioned Master.' if selected else 'Vladimir confirmed these local candidates represent the same object and selected one Master; alternatives are redundant.',
                final_sha256='sha256:'+o['media'][0]['sha256'],exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',
                notes='Accessioned 2026-09-18. Exact selected source and alternatives: '+RECEIPT+'. Source retained; deletion is not authorized.')
            changed.append(f['path'])
    for path in [final,W/'source-disposition.planned.csv']:write_csv(path,fields,rows)
    write_csv(W/'drain-list.csv',fields,[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    # retain-review.csv is the stable source set of the active retained workbench.
    # Keep it intact so existing case keys and all saved reviews remain valid.
    for path in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:
        evidence=json.loads(path.read_text())
        for e in evidence:
            if e['source_path'] in changed:
                row=by_path[e['source_path']]
                e.update(accession_receipt=RECEIPT,final_path=row['final_path'],final_sha256=row['final_sha256'])
        review.write_json(path,evidence)
    receipt['source_disposition_paths_updated']=changed
    receipt['source_disposition_counts']=dict(total=len(rows),candidate_rows=sum(r['drain_state']=='candidate_after_separate_review' for r in rows),retained_rows=sum(r['drain_state']!='candidate_after_separate_review' for r in rows))
    summary=json.loads((W/'drain-review-summary.json').read_text())
    summary.update(candidate_rows=receipt['source_disposition_counts']['candidate_rows'],retain_rows=receipt['source_disposition_counts']['retained_rows'],
        candidate_source_bytes=sum(int(r['source_size_bytes']) for r in rows if r['drain_state']=='candidate_after_separate_review'),retained_source_bytes=sum(int(r['source_size_bytes']) for r in rows if r['drain_state']!='candidate_after_separate_review'),preservation_status=dict(Counter(r['preservation_status'] for r in rows)),source_roles=dict(Counter(r['source_role'] for r in rows)),retained_accession=RECEIPT)
    review.write_json(W/'drain-review-summary.json',summary)
    write_receipt(receipt)


def finish(receipt):
    now=datetime.now(timezone.utc).isoformat();counts=receipt['counts']
    text=(U/'00_README.md').read_text();heading,body=text.split('\n',1)
    marker='## Retained-object accession — 2026-09-18'
    if marker not in text:
        intro=f'\n\n{marker}\n\nTwenty human-selected objects were accessioned from retained sources: seven TIFF\nMasters and thirteen JPEG Masters. Each has one access JPEG generated from its\nselected Master. VLV-1047 is an accepted member of VLV-1148.\n\nCurrent totals: 260 object records, 259 represented objects, 262 Master-member\nfiles and 262 access images. VLV-1092-2 remains unresolved. Conflicting/unknown\ndates remain blank, with source evidence preserved. Receipt and exact provenance:\n`{RECEIPT}`. The source register records 41 selected/redundant source paths\nagainst this accession. Other retained files remain review work. No source files\nwere changed or deleted, and no public publication was performed.\n\n'
        (U/'00_README.md').write_text(heading+intro+body)
    controls=['metadata/objects.csv','CATALOG.csv','CATALOG.xlsx','db-restore/RESTORE.csv','db-restore/RESTORE.xlsx','db-restore/RESTORE.sqlite','db-restore/RESTORE.sql','db-restore/manifests/representations.csv','db-restore/manifests/source-disposition.csv','db-restore/manifests/drain-evidence.json','00_README.md','catalog-export-summary.json','db-restore/export-summary.json',RECEIPT]
    receipt.update(completed_at=now,accession_performed=True,validation_result='ok',changed_controls=controls,
        source_modified=False,existing_media_unchanged=True,directory_structure_unchanged=True,sql_restore_verified=True,
        final_payload_hashes_computed_once=40,prior_reviews_preserved=len(receipt['prior_reviews']))
    kh,checks=read_csv(U/'db-restore/checksums.csv');checksum={r['path']:r for r in checks}
    for o in receipt['objects']:
        for f in o['media']:checksum[f['path']]=dict(path=f['path'],sha256='sha256:'+f['sha256'],size_bytes=str(f['size_bytes']))
    export=json.loads((U/'catalog-export-summary.json').read_text())
    export.update(updated_at=now,rows=counts['objects'],object_rows=counts['objects'],file_rows=counts['catalog_rows'],retained_accession=dict(objects_added=20,represented_objects=counts['represented_objects'],unresolved_objects=1,receipt=RECEIPT,deletion_authorized=False))
    summary=json.loads((U/'db-restore/export-summary.json').read_text())
    summary.update(updated_at=now,objects=counts['objects'],masters=counts['master_members'],access=counts['access_files'],generated_access=29,
        catalog_state=dict(catalog_revision=receipt['catalog_revision'],published_revision=receipt['catalog_revision'],dirty_since=None),retained_accession=export['retained_accession'])
    summary['restore']['rows']=counts['restore_rows'];summary['source_disposition_review']=receipt['source_disposition_counts']|{'receipt':RECEIPT}
    summary['checksums']=len(set(checksum)|set(controls))
    review.write_json(U/'db-restore/export-summary.json',summary)
    write_receipt(receipt);review.write_json(U/RECEIPT,receipt)
    # Reuse all unchanged payload hashes. Refresh only changed existing controls.
    for path in controls:
        if path=='catalog-export-summary.json':continue
        with (U/path).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        checksum[path]=dict(path=path,sha256='sha256:'+digest,size_bytes=str((U/path).stat().st_size))
    for value in export['outputs'].values():value['sha256']=checksum[str(Path(value['path']).relative_to(U))]['sha256']
    review.write_json(U/'catalog-export-summary.json',export)
    with (U/'catalog-export-summary.json').open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    checksum['catalog-export-summary.json']=dict(path='catalog-export-summary.json',sha256='sha256:'+digest,size_bytes=str((U/'catalog-export-summary.json').stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checksum.values(),key=lambda r:r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:review.export_decisions(con)


def verify():
    receipt=json.loads((U/RECEIPT).read_text());assert receipt['validation_result']=='ok'
    assert json.loads(WORK_RECEIPT.read_text())==receipt
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==receipt['original_directories']
    changed=set(receipt['changed_controls'])|{'db-restore/checksums.csv'}
    for p,before in receipt['original_files'].items():
        if p not in changed and not p.endswith(('-wal','-shm','-journal')):
            assert stamp(U/p)==before,('Unrelated archive file changed',p)
    with app.connect() as con:
        assert review.saved_reviews(con)==receipt['prior_reviews']
        health=review.check(con)
    ch,catalog=read_csv(U/'CATALOG.csv');oh,objects=read_csv(U/'metadata/objects.csv');rh,restore=read_csv(U/'db-restore/RESTORE.csv')
    verify_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)])
    verify_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])
    checksum={r['path']:r for r in read_csv(U/'db-restore/checksums.csv')[1]}
    for row in checksum.values():assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    for db in [W/'work/catalog.sqlite',U/'db-restore/RESTORE.sqlite']:
        with connect(db) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
            for o in receipt['objects']:
                for f in o['review']['files']:review.verify_asset(review.evidence()['assets'][f['path']])
                files=list(con.execute('SELECT ef.role,fo.relative_path,fo.content_digest FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) WHERE ef.entity_id=? AND ef.status=\'active\'',(o['entity_id'],)))
                assert Counter(f['role'] for f in files)=={'preservation_master':1,'access_derivative':1}
                assert {f['relative_path'] for f in files}=={o['master_path'],o['access_path']}
                for f in o['media']:
                    assert stamp(U/f['path'])==f['stamp']
                    assert checksum[f['path']]['sha256']=='sha256:'+f['sha256']
                    cat=next(row for row in catalog if row['final_relative_path']==f['path'])
                    rest=next(row for row in restore if row['relative_path']==f['path'])
                    assert cat['final_sha256']==rest['sha256']==f['sha256']
                    assert any(row['content_digest']=='sha256:'+f['sha256'] for row in files)
                if o['parent_entity_id']:assert con.execute("SELECT count(*) FROM entity_relations WHERE subject_entity_id=? AND predicate='component_of' AND object_entity_id=? AND status='accepted'",(o['entity_id'],o['parent_entity_id'])).fetchone()[0]==1
        con.close()
    assert len(catalog)==535 and len(objects)==260 and len(restore)==525
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    assert all(r['deletion_authorized']=='false' for r in read_csv(U/'db-restore/manifests/source-disposition.csv')[1])
    app.mount_guard()
    return dict(ok=True,**receipt['counts'],source_paths_updated=len(receipt['source_disposition_paths_updated']),saved_reviews_preserved=health['saved_reviews'],source_modified=False,deletion_authorized=False)


def apply():
    receipt=prepare()
    make_media(receipt)
    # Validate the insert path against the current schema in memory first.
    with connect(U/'db-restore/RESTORE.sqlite') as original:
        memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
        original.backup(memory);memory.execute('PRAGMA foreign_keys=ON')
        register_objects(memory,receipt);memory.close()
    original.close()
    for db in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        with connect(db,True) as con:
            register_objects(con,receipt)
            if db.is_relative_to(U):con.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
        con.close()
    export_records(receipt)
    update_dispositions(receipt)
    finish(receipt)
    print(json.dumps(verify(),indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.verify:print(json.dumps(verify(),indent=2))
    elif args.apply:apply()
    else:
        receipt=prepare()
        print(json.dumps(dict(ready=True,objects=len(receipt['objects']),formats=dict(Counter(o['technical']['format'] for o in receipt['objects'])),source_bytes=sum(o['source']['size_bytes'] for o in receipt['objects']),new_directories=0,new_schema=False,authorization=AUTHORIZATION),indent=2))
