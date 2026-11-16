"""Finish saved Paragon selections and documents; keep uncertain bindings pending."""
import argparse
import copy
import hashlib
import json
import os
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
from reconcile_drain_decisions import counters

W, U = review.ROOT, review.ARCHIVE
RECEIPT = 'db-restore/manifests/unit-finish-20260921.json'
ENTITY, INVENTORY = 'ISS-2026-E5ES-0', 'VLV-1130'
AUTH = 'Vladimir: ok lets finish this unit. Require the same physical specimen; keep uncertain bindings pending.'
MODEL = ['archive_objects', 'entity_names', 'entity_identifiers', 'entity_relations', 'evidence_assertions']
CONTROLS = ['CATALOG.csv', 'CATALOG.xlsx', 'metadata/objects.csv', 'db-restore/RESTORE.csv',
    'db-restore/RESTORE.xlsx', 'db-restore/RESTORE.sqlite', 'db-restore/RESTORE.sql',
    'db-restore/manifests/representations.csv', 'db-restore/manifests/documents.csv',
    'db-restore/manifests/source-disposition.csv', 'db-restore/manifests/drain-list.csv',
    'db-restore/manifests/drain-evidence.json', 'documents/CONTENTS.md', '00_README.md',
    'catalog-export-summary.json', 'db-restore/export-summary.json', RECEIPT,
    'db-restore/manifests/accession-remaining-20260921.csv',
    'db-restore/manifests/accession-remaining-20260921.json',
    'db-restore/manifests/ACCESSION_REMAINING_20260921.md']


def now(): return datetime.now(timezone.utc).isoformat()
def save(r): review.write_json(W / Path(RECEIPT).name, r)
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def check_source(e):
    p = Path(e['source_path'])
    assert p.resolve(strict=True) == p and not p.is_symlink() and p.is_relative_to(app.SOURCE)
    assert stamp(p) == [e['source_size_bytes'], e['source_mtime_ns']], str(p)


def prepare():
    app.mount_guard()
    assert not (U / RECEIPT).exists(), 'Completed job: verification only.'
    assert not (W / Path(RECEIPT).name).exists(), 'Inspect existing receipt; do not repeat preparation.'
    assert (W / 'source-disposition.planned.csv').read_bytes() == (U / 'db-restore/manifests/source-disposition.csv').read_bytes()
    fields, rows = read_csv(U / 'db-restore/manifests/source-disposition.csv')
    assert counters(rows)['candidate_rows'] == 1197 and len(rows) == 1419
    evidence = {e['source_path']: e for e in review.readjson(W / 'drain-evidence.json')}
    for e in evidence.values(): check_source(e)
    decisions = review.readjson(review.DECISIONS)
    decision = next(d for d in decisions['reviews'] if d['case_key'] == INVENTORY)
    pending = review.readjson(W / 'accession-remaining-20260921.json')
    obj = next(o for o in read_csv(U / 'metadata/objects.csv')[1] if o['iss_id'] == ENTITY)
    assert len(obj['master_file'].splitlines()) == 2
    sections = []
    for d in decision['files']:
        if d['state'] != 'carrier_section': continue
        assert d['carrier'] == INVENTORY and d['relation'] == 'section_of'
        e = evidence[d['path']]
        assert d['sha256'] == e['source_sha256']
        with Image.open(d['path']) as im:
            im.load(); assert im.mode == 'RGB' and im.format == 'TIFF' and im.n_frames == 1
            technical = dict(width=im.width, height=im.height, mode=im.mode, compression=im.info.get('compression', 'raw'))
        base = INVENTORY + '__' + ENTITY + '__section-' + Path(d['path']).stem
        sections.append(dict(source=e, part='section shown in source ' + Path(d['path']).name,
            master_path='masters/' + base + '__master.tif', access_path='export/access/' + base + '__access.jpg',
            technical=technical, media=[], saved_selection=d))
    assert [Path(s['source']['source_path']).stem for s in sections] == ['VLV-1130-2', 'VLV-1130-3', 'VLV-1130-4', 'VLV-1130-6']
    docs = []
    specs = [
        ('Paragon_Broschüre.pdf', 'project-history', 'PARAGON 1911–2019. Eine Zeitreise von der Druckerei zum modernen Nahversorgungszentrum. Annette Godefroid.', '2019; PDF created 2019-04-11.', 'Readable 18-page PDF with text and illustrations.', 'pdf-readable;pages=18'),
        ('Paragon-Kassenblockfabrik.docx', 'research-notes', 'Research note on Paragon Kassenblock AG, company history and a June 1925 share certificate; cites Peus Nachf.', 'Company history includes 1911–1982; note date unknown.', 'Readable DOCX containing a short company-history and share-certificate note.', 'docx-readable'),
    ]
    for number, (name, category, description, date, condition, verification) in enumerate(specs, 9):
        f = next(f for f in pending['files'] if Path(f['source_path']).name == name)
        assert f['saved_state'] == 'outside_picture_review'
        docs.append(dict(document_id=f'PARAGON-DOC-{number:03d}', source=evidence[f['source_path']],
            path='documents/' + category + '/' + name, category=category, description=description,
            date_note=date, condition=condition, verification=verification, media=[]))
    excluded = [f for f in pending['files'] if f['saved_state'] == 'outside_picture_review' and f['source_path'] not in [d['source']['source_path'] for d in docs]]
    assert len(excluded) == 8
    by = {r['source_path']: r for r in rows}
    assert all(by[f['source_path']]['preservation_status'] == 'not_preserved_intermediate_metadata' for f in excluded)
    assert 'Nine intermediate upload paths' in (U / 'documents/CONTENTS.md').read_text()
    for path in [s[k] for s in sections for k in ['master_path', 'access_path']] + [d['path'] for d in docs]:
        assert (U / path).parent.is_dir() and not (U / path).exists(), path
    baseline = {}
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        with connect(db) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not c.execute('PRAGMA foreign_key_check').fetchall()
            baseline[str(db)] = {t: [dict(x) for x in c.execute('SELECT * FROM ' + t + ' ORDER BY rowid')] for t in MODEL}
    assert shutil.disk_usage(U).free > 1_000_000_000
    r = dict(authorization=AUTH, started_at=now(), result='prepared', carrier=obj, sections=sections,
        documents=docs, excluded_intermediates=excluded, prior_document_selection=(U / 'documents/CONTENTS.md').read_text(),
        prior_databases=baseline, prior_decisions=decisions, prior_pending=pending,
        prior_source_rows=by, source_evidence=evidence, log_id='LOG-' + uuid4().hex.upper(),
        original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        original_files={str(p.relative_to(U)): stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal', '-shm', '-journal'))},
        source_modified=False, deletion_authorized=False, publication_performed=False,
        workbook_runtime='Artifact Tool unavailable: WASM SIMD unsupported. Existing bundled openpyxl editor preserves tabs, styles and checks CSV agreement.')
    save(r)
    print(json.dumps(dict(sections=4, documents=2, excluded_intermediates=8, pending_after=208)))


def stage(r):
    assert r['result'] == 'prepared'
    for s in r['sections']:
        e = s['source']; check_source(e)
        source = e['source_path']
        source_meta = json.loads(subprocess.check_output(['exiftool', '-j', '-G1', '-s', '-XMP:All', '-IPTC:All', '-IFD0:ImageDescription', '-IFD0:Artist', '-IFD0:Copyright', source], text=True))[0]
        assert set(source_meta) == {'SourceFile'}, 'Reconcile unexpected source metadata before embedding.'
        s['source_embedded_metadata'] = source_meta
        m = r['carrier']
        s['embedded'] = dict(iss_id=ENTITY, archival_label=INVENTORY,
            descriptive_title=m['descriptive_title'] + ' — Teilscan ' + Path(source).stem,
            description=m['description'] + '\n\nTeilscan derselben Wandzeitung VLV-1130. Quelldatei: ' + Path(source).name + '. Keine Gesamtansicht.')
        for kind in ['master', 'access']:
            path = s[kind + '_path']; staged = W / ('unit-finish-' + Path(path).name)
            prior = next((f for f in s['media'] if f['path'] == path), None)
            if prior:
                assert stamp(staged) == prior['stamp']; continue
            assert not staged.exists()
            if kind == 'master':
                with Path(source).open('rb') as inp, staged.open('xb') as out: shutil.copyfileobj(inp, out)
            else:
                with Image.open(s['media'][0]['staged']) as im, staged.open('xb') as out:
                    ImageOps.exif_transpose(im).convert('RGB').save(out, 'JPEG', quality=92, subsampling=0)
            embed(staged, s['embedded'])
            with Image.open(staged) as im:
                im.load(); width, height = im.size
                if kind == 'master':
                    with Image.open(source) as src:
                        src.load(); assert src.mode == im.mode and src.size == im.size and src.tobytes() == im.tobytes()
                        assert src.getexif().get(274, 1) == im.getexif().get(274, 1)
            f = dict(path=path, staged=str(staged), role='preservation_master' if kind == 'master' else 'access_derivative',
                sha256=sha(staged), stamp=stamp(staged), size_bytes=staged.stat().st_size, width=width, height=height,
                media_type='image/tiff' if kind == 'master' else 'image/jpeg', compression=s['technical']['compression'] if kind == 'master' else 'JPEG',
                occurrence_id='OCC-' + uuid4().hex.upper(), observed_mtime_utc=datetime.fromtimestamp(staged.stat().st_mtime, timezone.utc).isoformat())
            f['provenance'] = dict(source_path=source, source_sha256=e['source_sha256'], source_mtime_ns=e['source_mtime_ns'],
                master_path=s['master_path'], carrier_inventory=INVENTORY, representation_part=s['part'],
                derivation='metadata_enriched_selected_master' if kind == 'master' else 'generated_from_selected_master',
                receipt=RECEIPT, deletion_authorized=False)
            s['media'].append(f); save(r)
        print('Prepared section', Path(source).name, flush=True)
    for d in r['documents']:
        if d['media']: continue
        e = d['source']; check_source(e); staged = W / ('unit-finish-' + Path(d['path']).name)
        assert not staged.exists()
        with Path(e['source_path']).open('rb') as inp, staged.open('xb') as out: shutil.copyfileobj(inp, out)
        digest = sha(staged); assert 'sha256:' + digest == e['source_sha256']
        d['media'] = [dict(path=d['path'], staged=str(staged), role='documentation_original', sha256=digest,
            stamp=stamp(staged), size_bytes=staged.stat().st_size, occurrence_id='OCC-' + uuid4().hex.upper(),
            media_type='application/pdf' if staged.suffix == '.pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            observed_mtime_utc=datetime.fromtimestamp(staged.stat().st_mtime, timezone.utc).isoformat(),
            provenance=dict(source_path=e['source_path'], source_sha256=e['source_sha256'], source_mtime_ns=e['source_mtime_ns'],
                document_id=d['document_id'], relation='direct', receipt=RECEIPT, derivation='exact_copy', deletion_authorized=False))]
        save(r)
    changed = {}
    for s in r['sections']:
        row = copy.deepcopy(r['prior_source_rows'][s['source']['source_path']]); f = s['media'][0]
        row.update(inventory=INVENTORY, source_role='selected_master_candidate', final_path=f['path'], final_sha256='sha256:' + f['sha256'],
            preservation_status='preserved_as_metadata_enriched_master', exact_copy_proven='false',
            preservation_basis='Saved section_of VLV-1130 selection accessioned as a member of its existing single selected Master. Same mounted photographs, handwritten arrows and carrier layout confirmed visually; original pixels preserved.')
        changed[row['source_path']] = row
    for d in r['documents']:
        row = copy.deepcopy(r['prior_source_rows'][d['source']['source_path']]); f = d['media'][0]
        row.update(final_path=f['path'], final_sha256='sha256:' + f['sha256'], preservation_status='preserved_exact_copy', exact_copy_proven='true',
            preservation_basis='Contents reviewed as directly relevant project documentation; accession copy hash equals stored unchanged source hash.')
        changed[row['source_path']] = row
    for d in r['excluded_intermediates']:
        row = copy.deepcopy(r['prior_source_rows'][d['source_path']])
        row.update(source_role='redundant_candidate', preservation_status='reviewed_not_selected', exact_copy_proven='false',
            preservation_basis='Intermediate caption export rejected by the existing document selection in documents/CONTENTS.md. Full pre-upload and final CSV/XLSX retained. This records non-selection, without claiming this intermediate is an exact copy or has identical contents.')
        changed[row['source_path']] = row
    for row in changed.values():
        row.update(drain_state='candidate_after_separate_review', deletion_authorized='false', notes='Unit completion authorized by Vladimir on 2026-09-21. Exact decision and accession evidence: ' + RECEIPT + '. Sources retained; no deletion.')
    assert len(changed) == 14
    r['source_disposition_rows'] = changed
    r['result'] = 'staged'; save(r)


def media(r): return [f for group in r['sections'] + r['documents'] for f in group['media']]


def register(c, r):
    assert not c.execute('SELECT 1 FROM logbook WHERE log_id=?', (r['log_id'],)).fetchone(), 'Already registered.'
    for f in media(r):
        digest = 'sha256:' + f['sha256']
        c.execute('INSERT OR IGNORE INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (digest,f['size_bytes'],f['media_type'],f.get('width'),f.get('height'),8 if 'width' in f else None,'RGB' if 'width' in f else '',f.get('compression',''),1 if 'width' in f else None,r['started_at']))
        c.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (f['occurrence_id'],'WF-PARAGON',digest,'archive',f['path'],Path(f['path']).name,f['observed_mtime_utc'],r['started_at'],'verified',json.dumps(f['provenance'],ensure_ascii=False)))
        if f['role'] != 'documentation_original':
            c.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)',(ENTITY,f['occurrence_id'],f['role'],'active',r['started_at']))
    c.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (r['log_id'],r['started_at'],'Vladimir','WF-PARAGON','finish_reviewed_accession','archive_unit','WF-PARAGON',
         json.dumps({p:r['prior_source_rows'][p] for p in r['source_disposition_rows']},ensure_ascii=False),
         json.dumps(dict(receipt=RECEIPT,media=media(r),source_disposition_rows=r['source_disposition_rows'],pending_sources=208,same_physical_specimen_required=True,deletion_authorized=False),ensure_ascii=False),RECEIPT,AUTH))
    assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok' and not c.execute('PRAGMA foreign_key_check').fetchall()


def install(r):
    assert r['result'] == 'staged'
    app.mount_guard()
    assert review.readjson(review.DECISIONS) == r['prior_decisions']
    for e in r['source_evidence'].values(): check_source(e)
    for f in media(r): assert stamp(f['staged']) == f['stamp'] and not (U/f['path']).exists()
    for db in r['prior_databases']:
        with connect(db) as c:
            m=sqlite3.connect(':memory:');m.row_factory=sqlite3.Row;c.backup(m);m.execute('PRAGMA foreign_keys=ON');register(m,r);m.close()
    r['result']='installing';save(r)
    for f in media(r):
        os.rename(f['staged'],U/f['path']);assert stamp(U/f['path'])==f['stamp']
    for db in r['prior_databases']:
        with connect(db,True) as c:register(c,r)
    r['result']='registered';save(r)


def export_records(r):
    assert r['result'] == 'registered'
    oh,objects=read_csv(U/'metadata/objects.csv');ch,catalog=read_csv(U/'CATALOG.csv')
    rh,restore=read_csv(U/'db-restore/RESTORE.csv');ph,reps=read_csv(U/'db-restore/manifests/representations.csv')
    dh,docs=read_csv(U/'db-restore/manifests/documents.csv')
    assert (len(objects),len(catalog),len(restore),len(reps),len(docs)) == (260,591,581,278,8)
    carrier=next(o for o in objects if o['iss_id']==ENTITY)
    assert carrier == r['carrier']
    overview=next(x for x in catalog if x['final_relative_path']==carrier['master_file'].splitlines()[0])
    prior_restore=next(x for x in restore if x['iss_id']==ENTITY and x['relative_path']==overview['final_relative_path'])
    carrier.update(master_file=carrier['master_file']+'\n'+'\n'.join(s['master_path'] for s in r['sections']),
        access_file=carrier['access_file']+'\n'+'\n'.join(s['access_path'] for s in r['sections']),
        master_member_count='6',master_parts=carrier['master_parts']+'\n'+'\n'.join(s['part'] for s in r['sections']),
        representation_summary='access_derivative: 6; preservation_master: 6',
        notes_for_review=carrier['notes_for_review']+'; remaining four saved carrier-section selections accessioned on 2026-09-21; no new claim of complete high-resolution coverage')
    for sequence,s in enumerate(r['sections'],3):
        master,access=s['media']
        for f in s['media']:
            is_master=f['role']=='preservation_master'
            row=dict(overview)
            row.update(file_role=f['role'],final_relative_path=f['path'],master_relative_path=s['master_path'],filename=Path(f['path']).name,
                file_type='tif' if is_master else 'jpg',size_bytes=str(f['size_bytes']),final_sha256=f['sha256'],description=s['embedded']['description'],
                metadata_authority='Existing carrier metadata; saved section_of selection; confirmed same physical specimen.',
                original_source_path=s['source']['source_path'] if is_master else str(U/s['master_path']),
                source_sha256=s['source']['source_sha256'].removeprefix('sha256:') if is_master else master['sha256'],
                derivative_of='' if is_master else s['master_path'],sequence=str(sequence),width=str(f['width']),height=str(f['height']),
                relationship='member of the single selected Master for VLV-1130; part='+s['part'] if is_master else 'generated access image of carrier-section Master member',
                condition='Partial carrier view; no assertion of complete high-resolution coverage.',
                notes='Saved carrier-section selection completed; source pixels preserved; separate photograph Masters unchanged.',
                metadata_path='metadata/objects.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/source-disposition.csv')
            catalog.append(row)
            rr=dict(prior_restore)
            rr.update(representation_role=f['role'],relative_path=f['path'],original_filename=Path(f['path']).name,sha256=f['sha256'],
                size_bytes=str(f['size_bytes']),media_type=f['media_type'],width_px=str(f['width']),height_px=str(f['height']),
                bit_depth='8',color_space='RGB',compression=f['compression'],observed_mtime_utc=f['observed_mtime_utc'],
                provenance_note=json.dumps(f['provenance'],ensure_ascii=False),provenance_status='occurrence_note_available',source_root_id='',source_batch_id='')
            restore.append(rr)
        reps.append(dict(zip(ph,[ENTITY,INVENTORY,'preservation_master',master['path'],'sha256:'+master['sha256'],
            access['path'],'sha256:'+access['sha256'],master['path'],'generated_from_selected_master','local_access',str(access['width']),str(access['height']),
            'paragon/finish_unit_20260921.py',r['started_at']])))
    for d in r['documents']:
        f=d['media'][0];e=d['source']
        dr=dict(document_id=d['document_id'],relative_path=d['path'],category=d['category'],format=Path(d['path']).suffix[1:],
            description=d['description'],relation_to_paragon='direct',date_note=d['date_note'],condition=d['condition'],source_kind='file',
            source_path=e['source_path'],duplicate_source_paths='',source_size_bytes=str(e['source_size_bytes']),source_mtime_ns=str(e['source_mtime_ns']),
            source_mtime_utc=datetime.fromtimestamp(e['source_mtime_ns']/1e9,timezone.utc).isoformat(),final_sha256='sha256:'+f['sha256'],
            size_bytes=str(f['size_bytes']),copy_verification='exact-byte-copy;'+d['verification'])
        docs.append(dr)
        row={k:'' for k in ch}
        row.update(record_key=d['document_id'],title=d['description'],file_role='documentation_original',final_relative_path=d['path'],
            filename=Path(d['path']).name,file_type=dr['format'],size_bytes=dr['size_bytes'],final_sha256=f['sha256'],
            description=d['description'],date_text=d['date_note'],original_source_path=e['source_path'],source_sha256=e['source_sha256'].removeprefix('sha256:'),
            relationship='direct',condition=d['condition'],notes=dr['copy_verification'],metadata_authority='Original document content; reviewed direct relation to Paragon.',
            metadata_path='db-restore/manifests/documents.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/documents.csv')
        catalog.append(row)
    with connect(U/'db-restore/RESTORE.sqlite',True) as c:
        c.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
        revision=c.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
    with connect(U/'db-restore/RESTORE.sqlite') as c: sql='\n'.join(c.iterdump())+'\n'
    memory=sqlite3.connect(':memory:');memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    for row in restore:row['catalog_revision']=str(revision)
    for path,fields,rows in [('metadata/objects.csv',oh,objects),('CATALOG.csv',ch,catalog),('db-restore/RESTORE.csv',rh,restore),('db-restore/manifests/representations.csv',ph,reps),('db-restore/manifests/documents.csv',dh,docs)]:
        write_csv(U/path,fields,rows)
    (U/'db-restore/RESTORE.sql').write_text(sql)
    r['workbook_checks']={
        'CATALOG.xlsx':update_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)]),
        'db-restore/RESTORE.xlsx':update_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])}
    for filename,oldmax in [('CATALOG.xlsx',592),('db-restore/RESTORE.xlsx',582)]:
        wb=load_workbook(U/filename);sheet=wb['Catalog'];headers=[c.value for c in sheet[1]]
        for ri in range(oldmax+1,sheet.max_row+1):
            for field in ['final_sha256','source_sha256','sha256']:
                if field not in headers:continue
                cell=sheet.cell(ri,headers.index(field)+1);align=copy.copy(cell.alignment);align.wrap_text=True;cell.alignment=align
        if filename=='CATALOG.xlsx':
            sheet=wb['Objects'];ri=next(i for i,o in enumerate(objects,2) if o['iss_id']==ENTITY)
            for field in ['representation_summary','master_file','access_file','master_parts','notes_for_review']:
                cell=sheet.cell(ri,oh.index(field)+1);align=copy.copy(cell.alignment);align.wrap_text=True;cell.alignment=align
            sheet.row_dimensions[ri].height=max(sheet.row_dimensions[ri].height or 23,204)
        wb.save(U/filename);wb.close()
    contents=r['prior_document_selection']
    newlines=''
    for d in docs[-2:]:
        newlines+='| '+' | '.join([d['document_id'],'`'+d['relative_path']+'`',d['description'],d['date_note'],'`'+d['source_path']+'`',d['relation_to_paragon'],d['condition']])+' |\n'
    contents=contents.replace('\n## Related package records',newlines+'\n## Related package records')
    contents+='\n## Completion, 21 September 2026\n\nThe directly relevant Paragon brochure and company-history note were added as exact original copies. The eight still-retained intermediate caption paths are now recorded as not selected in the drain register, applying the existing selection boundary above. No claim of byte identity or complete content equivalence is made for those intermediate exports.\n'
    (U/'documents/CONTENTS.md').write_text(contents)
    r['catalog_revision']=revision
    r['counts']=dict(objects=260,represented_objects=259,master_members=282,access_images=282,catalog_rows=601,restore_rows=589,documents=10,
        sections_added=4,access_images_generated=4,documents_added=2,rejected_intermediates_registered=8,source_paths_resolved=14,drain_candidates=1211,pending_source_paths=208)
    r['result']='exported';save(r)


def finish_records(r):
    assert r['result']=='exported'
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv')
    rows=[r['source_disposition_rows'].get(x['source_path'],x) for x in rows]
    for p in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(p,fields,rows)
    drain=[x for x in rows if x['drain_state']=='candidate_after_separate_review']
    for p in [W/'drain-list.csv',U/'db-restore/manifests/drain-list.csv']:write_csv(p,fields,drain)
    evidence=list(r['source_evidence'].values())
    for e in evidence:
        if e['source_path'] in r['source_disposition_rows']:
            row=r['source_disposition_rows'][e['source_path']]
            e.update(accession_receipt=RECEIPT,final_path=row['final_path'],final_sha256=row['final_sha256'],preservation_status=row['preservation_status'],decision_log_id=r['log_id'])
    for p in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(p,evidence)
    summary=review.readjson(W/'drain-review-summary.json');summary.update(counters(rows))
    summary.update(total_rows=1419,unit_finish_receipt=RECEIPT,current_register='1211 drain candidates; 208 source paths excluded pending identity/Master reconciliation. No deletion.')
    review.write_json(W/'drain-review-summary.json',summary)
    pending=copy.deepcopy(r['prior_pending']);pending['files']=[f for f in pending['files'] if f['source_path'] not in r['source_disposition_rows']]
    assert Counter(f['saved_state'] for f in pending['files'])=={'retain':166,'selected_master':42}
    pending.update(counts=dict(Counter(f['saved_state'] for f in pending['files'])),authorization=AUTH,unit_finish_receipt=RECEIPT,
        scope='208 source pictures remain pending; uncertain physical-specimen bindings are not accessioned or included in the drain list.',
        physical_specimen_rule='Require the same physical specimen; keep uncertain bindings pending')
    fh,_=read_csv(W/'accession-remaining-20260921.csv')
    for root in [W,U/'db-restore/manifests']:
        review.write_json(root/'accession-remaining-20260921.json',pending)
        write_csv(root/'accession-remaining-20260921.csv',fh,[{k:f[k] for k in fh} for f in pending['files']])
    old=(W/'ACCESSION_REMAINING_20260921.md').read_text()
    detail=old[old.index('## Saved TIFF choices'):]
    text='# Paragon: pending source bindings, 21 September 2026\n\nThe authorized accession is complete for confirmed decisions. **208 source paths remain pending:** 42 saved TIFF choices still need archive identity/quality reconciliation, and 166 retained pictures still need final object/Master binding.\n\nVladimir requires the same physical specimen. Uncertain bindings remain pending and are excluded from the drain list. The 26 earlier Master substitutions, four carrier sections and two documents are recorded in their accession receipts. Eight rejected intermediate caption exports are now registered as not selected.\n\nEvery exact pending path, stored source hash, saved decision and review note is in `accession-remaining-20260921.csv` and `.json` beside this file. Nothing is deleted.\n\n'+detail
    for root in [W,U/'db-restore/manifests']:(root/'ACCESSION_REMAINING_20260921.md').write_text(text)
    r['result']='records_written';save(r)


def verify_core(r):
    app.mount_guard()
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==r['original_directories']
    for path,before in r['original_files'].items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==before,('Unrelated archive file changed',path)
    assert review.readjson(review.DECISIONS)==r['prior_decisions']
    for e in r['source_evidence'].values():check_source(e)
    ch,catalog=read_csv(U/'CATALOG.csv');oh,objects=read_csv(U/'metadata/objects.csv');rh,restore=read_csv(U/'db-restore/RESTORE.csv')
    reps=read_csv(U/'db-restore/manifests/representations.csv')[1]
    docs=read_csv(U/'db-restore/manifests/documents.csv')[1]
    assert (len(catalog),len(objects),len(restore),len(reps),len(docs))==(601,260,589,282,10)
    verify_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)])
    verify_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])
    for db,baseline in r['prior_databases'].items():
        with connect(db) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not c.execute('PRAGMA foreign_key_check').fetchall()
            for t,before in baseline.items():assert [dict(x) for x in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]==before,(db,t)
            log=json.loads(c.execute('SELECT after_json FROM logbook WHERE log_id=?',(r['log_id'],)).fetchone()[0])
            assert log['source_disposition_rows']==r['source_disposition_rows']
            for f in media(r):
                assert stamp(U/f['path'])==f['stamp']
                dbrow=c.execute('SELECT content_digest FROM file_occurrences WHERE occurrence_id=?',(f['occurrence_id'],)).fetchone()
                assert dbrow[0]=='sha256:'+f['sha256']
                assert next(x for x in catalog if x['final_relative_path']==f['path'])['final_sha256']==f['sha256']
                if f['role']!='documentation_original':assert next(x for x in restore if x['relative_path']==f['path'])['sha256']==f['sha256']
            members=[x[0] for x in c.execute("SELECT fo.relative_path FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) WHERE ef.entity_id=? AND ef.status='active' AND ef.role IN ('preservation_master','preservation_surrogate')",(ENTITY,))]
            obj=next(x for x in objects if x['iss_id']==ENTITY)
            assert len(members)==6 and set(members)==set(obj['master_file'].splitlines())
            with app.connect() as work:assert review.readable_decisions(work)==review.readjson(review.DECISIONS)
    for rel,work in [('source-disposition.csv','source-disposition.planned.csv'),('drain-list.csv','drain-list.csv'),('drain-evidence.json','drain-evidence.json'),('accession-remaining-20260921.csv','accession-remaining-20260921.csv'),('accession-remaining-20260921.json','accession-remaining-20260921.json')]:
        assert (U/'db-restore/manifests'/rel).read_bytes()==(W/work).read_bytes()
    rows=read_csv(U/'db-restore/manifests/source-disposition.csv')[1]
    assert len(rows)==1419 and len({x['source_path'] for x in rows})==1419
    assert sum(x['drain_state']=='candidate_after_separate_review' for x in rows)==1211
    assert all(x['deletion_authorized']=='false' for x in rows)
    pending=review.readjson(W/'accession-remaining-20260921.json')['files']
    assert {x['source_path'] for x in pending}=={x['source_path'] for x in rows if x['drain_state']!='candidate_after_separate_review'}
    return dict(ok=True,**r['counts'],source_stamps_unchanged=1419,object_metadata_and_relationships_preserved=True,
        one_selected_master_preserved=True,saved_decisions_preserved=True,source_modified=False,deletion_authorized=False,
        directory_structure_unchanged=True,sqlite_and_readable_records_agree=True,uncertain_bindings_pending=True)


def finalize(r):
    assert r['result']=='records_written'
    r['validation']=verify_core(r)
    introduction='## Completed accession with pending source bindings — 21 September 2026\n\nThe confirmed accession is finished: the 26 earlier Master substitutions plus\nfour saved VLV-1130 carrier sections and two project documents are accessioned.\nFour access images were generated for the new sections. Eight intermediate\ncaption exports were registered as not selected under the existing document rule.\n\nCurrent totals: **260 objects, 259 represented, 282 selected Master-member files,\n282 access images and 10 documents**. VLV-1092-2 remains without an identified source.\nThe drain list contains **1,211 exact source paths**. **208 picture paths remain\npending** (42 selected TIFFs awaiting archive identity/quality reconciliation and\n166 retained pictures awaiting object/Master binding). Vladimir requires the same\nphysical specimen; uncertain bindings remain pending. This is not a claim that\nevery discovered source has been accessioned.\n\nCurrent files in `db-restore/manifests/`:\n- `drain-list.csv`: exact current drain candidates; deletion remains unauthorized.\n- `accession-remaining-20260921.csv` and `.json`: every excluded path and saved decision.\n- `ACCESSION_REMAINING_20260921.md`: identity exceptions and comparison findings.\n- `unit-finish-20260921.json`: receipt and validation for this completion.\n\nObject names, descriptive metadata, attribution and relationships remain intact.\nThe existing single selected Master for VLV-1130 contains its overview and five\nselected sections. Separate photograph Masters remain intact. Sources are unchanged,\nno deletion or public publication occurred, and no directories or schemas were added.\nAll dated checkpoints below describe historical counts.\n\n'
    for p in [U/'00_README.md',W/'00_README.md']:
        heading,body=p.read_text().split('\n',1);p.write_text(heading+'\n\n'+introduction+body)
    r.update(result='passed',completed_at=now(),accession_performed=True,final_payload_hashes_computed_once=10,
        approved_accession_complete=True,all_source_bindings_resolved=False)
    save(r);review.write_json(U/RECEIPT,r)
    export=review.readjson(U/'catalog-export-summary.json')
    export.update(updated_at=r['completed_at'],file_rows=601,object_rows=260,unit_finish=dict(receipt=RECEIPT,**r['counts']))
    summary=review.readjson(U/'db-restore/export-summary.json')
    summary.update(updated_at=r['completed_at'],masters=282,access=282,documents=10,generated_access=73,catalog_revision=r['catalog_revision'],
        catalog_state=dict(catalog_revision=r['catalog_revision'],published_revision=r['catalog_revision'],dirty_since=None),
        source_disposition_review=dict(total=1419,candidate_rows=1211,retained_rows=208,receipt=RECEIPT),unit_finish=export['unit_finish'])
    summary['restore']['rows']=589
    kh,checks=read_csv(U/'db-restore/checksums.csv');checksum={x['path']:x for x in checks}
    for f in media(r):checksum[f['path']]=dict(path=f['path'],sha256='sha256:'+f['sha256'],size_bytes=str(f['size_bytes']))
    summary['checksums']=len(set(checksum)|set(CONTROLS))
    review.write_json(U/'db-restore/export-summary.json',summary)
    for path in CONTROLS:
        if path=='catalog-export-summary.json':continue
        checksum[path]=dict(path=path,sha256='sha256:'+sha(U/path),size_bytes=str((U/path).stat().st_size))
    for value in export['outputs'].values():value['sha256']=checksum[str(Path(value['path']).relative_to(U))]['sha256']
    review.write_json(U/'catalog-export-summary.json',export)
    checksum['catalog-export-summary.json']=dict(path='catalog-export-summary.json',sha256='sha256:'+sha(U/'catalog-export-summary.json'),size_bytes=str((U/'catalog-export-summary.json').stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checksum.values(),key=lambda x:x['path']))
    handoff=W.parent/'HANDOFF.md'
    header='# Paragon: confirmed accession finished, pending bindings retained\n\n'+introduction.replace('in `db-restore/manifests/`','in `/mnt/volume/iss-archive/units/paragon/db-restore/manifests/`')
    header+='Work root: `/mnt/volume/iss-archive-work/paragon`. Verify the current state with the bundled Python and `finish_unit_20260921.py --verify`. Do not replay completed accession/drain jobs or use their historical fixed-count verifiers as current authority. The protected source mount must remain `/dev/sdf1 /media/vladimir/Elements ntfs3 ro`.\n\n---\n\n'
    handoff.write_text(header+handoff.read_text())
    review.write_json(W/'finalization-validation-20260921.json',r['validation']|dict(checked_at=now(),receipt=RECEIPT))


def verify():
    r=review.readjson(U/RECEIPT);assert r['result']=='passed' and r==review.readjson(W/Path(RECEIPT).name)
    result=verify_core(r)
    checks={x['path']:x for x in read_csv(U/'db-restore/checksums.csv')[1]}
    for path in CONTROLS:assert checks[path]['sha256']=='sha256:'+sha(U/path),path
    for f in media(r):assert checks[f['path']]['sha256']=='sha256:'+f['sha256']
    result['changed_control_checksums_verified']=True
    review.write_json(W/'finalization-validation-20260921.json',result|dict(checked_at=now(),receipt=RECEIPT))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--verify',action='store_true');parser.add_argument('--apply-staged',action='store_true')
    args=parser.parse_args()
    if args.verify:print(json.dumps(verify(),indent=2))
    elif args.apply_staged:
        r=review.readjson(W/Path(RECEIPT).name);install(r);export_records(r);finish_records(r);finalize(r);print(json.dumps(verify(),indent=2))
    else:prepare();stage(review.readjson(W/Path(RECEIPT).name))
