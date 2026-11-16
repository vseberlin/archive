#!/usr/bin/env python3
"""Append Vladimir's selected five-page Urbanitzky Master to the existing unit."""
import csv, json, shutil, sqlite3, subprocess, sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from PIL import Image
from openpyxl import load_workbook
from cleanup_bibliothek import read_csv, csv_bytes, json_bytes, stamp, render
from prepare_bibliothek import guard
sys.path.insert(0, '/mnt/volume/iss-archive-work/paragon')
from accession_reviewed import update_workbook
W=Path('/mnt/volume/iss-archive-work/bibliothek')
U=Path('/mnt/volume/iss-archive/units/bibliothek')
KEY='BIB-1002'
CID=KEY+':DOC-E4DF43C2F450C39371963AC8FF5E49C8'
MASTER='masters/BIB-1002-local-00889e1311ce.pdf'
PREVIEW='export/access/BIB-1002-local-00889e1311ce-preview.jpg'
EXTENT='Five scanned pages of title/front matter and contents; not a complete book scan.'
NOTE=('Vladimir explicitly selected: "Select this five-page PDF as Master". '
      'Chosen path: Findbücher/Andere Scans vom Ricoah/Urbanitzky.pdf. '+EXTENT+
      ' The other two identical local copies are redundant; unrelated MD attachment 106189 remains excluded.')
TITLE='Alfred von Urbanitzky, Die Elektrizität im Dienste der Menschheit'

def main(apply=False):
    guard()
    now=datetime.now(timezone.utc).isoformat()
    db=U/'db-restore/catalog.sqlite'
    con=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True); con.row_factory=sqlite3.Row
    tables={t:[dict(r) for r in con.execute('SELECT * FROM '+t)] for t in ('objects','files','source_disposition','candidate_dispositions')}
    schema=[tuple(r) for r in con.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; con.close()
    assert {r['object_key'] for r in tables['objects']}=={'BIB-1001','BIB-1003'}
    assert len(tables['files'])==23
    dirs={str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    old_files={r['relative_path']:dict(r) for r in tables['files']}
    old_stamps={p:stamp(U/p) for p in old_files}
    assert all(old_stamps[p][0]==int(r['size_bytes']) for p,r in old_files.items())
    candidate_fields,candidates=read_csv(W/'candidates.csv')
    selected=next(c for c in candidates if c['candidate_id']==CID)
    assert selected['eligible']=='true'
    member=json.loads(selected['members_json'])[0]; source=Path(member['path'])
    locals=[m for c in candidates if c['object_key']==KEY and c['origin']=='protected_usb' for m in json.loads(c['members_json'])]
    assert len(locals)==3 and {m['sha256'] for m in locals}=={member['sha256']}
    def sources_unchanged():
        guard()
        for m in locals:
            p=Path(m['path']); assert p.resolve(strict=True)==p
            assert stamp(p)[:2]==(m['size_bytes'],m['mtime_ns'])
    sources_unchanged()
    for relative in (MASTER,PREVIEW):
        assert (U/relative).parent.is_dir() and not (U/relative).exists()
    assert not (W/'urbanitzky-accession-completion.json').exists()
    if not apply:
        print(json.dumps(dict(result='preflight passed',selected_master=str(source),pages=member['pages'],objects_before=2,objects_after=3,new_directories=0),indent=2)); return
    decision_fields,decisions=read_csv(W/'master-decisions.csv')
    decision=next(r for r in decisions if r['object_key']==KEY)
    decision.update(selected_candidate_id=CID,decision='selected_master',actor='Vladimir',decided_at=now,note=NOTE)
    disp_fields,dispositions=read_csv(W/'candidate-dispositions.csv')
    for r in dispositions:
        if r['object_key']==KEY:
            r['disposition']='selected_master' if r['candidate_id']==CID else ('excluded' if r['candidate_id']==KEY+':md' else 'redundant')
    tables['candidate_dispositions']=dispositions
    (W/'master-decisions.csv').write_bytes(csv_bytes(decision_fields,decisions))
    (W/'candidate-dispositions.csv').write_bytes(csv_bytes(disp_fields,dispositions))
    # One exact preservation copy; PDF metadata stays in the readable catalogue.
    with source.open('rb') as src,(U/MASTER).open('xb') as dst: shutil.copyfileobj(src,dst)
    master_hash=sha256((U/MASTER).read_bytes()).hexdigest()
    assert master_hash==member['sha256']
    info=subprocess.run(['pdfinfo',str(U/MASTER)],check=True,capture_output=True,text=True).stdout
    assert next(x.split(':',1)[1].strip() for x in info.splitlines() if x.startswith('Pages:'))=='5'
    subprocess.run(['pdftoppm','-f','1','-singlefile','-scale-to','1600','-jpeg','-jpegopt','quality=92',str(U/MASTER),str((U/PREVIEW).with_suffix(''))],check=True,capture_output=True)
    with Image.open(U/PREVIEW) as img: img.load(); width,height=img.size
    preview_hash=sha256((U/PREVIEW).read_bytes()).hexdigest()
    obj={f:'' for f in tables['objects'][0]}
    obj.update(object_key=KEY,md_object_id='50002',title=TITLE,catalogued_book_date='1885',catalogued_book_creator='Alfred von Urbanitzky',
        object_url='https://berlin.museum-digital.de/object/50002',selected_candidate_id=CID,ordered_master_paths=json.dumps([MASTER]),
        representation_extent=EXTENT,content_match_status='matched',content_note=EXTENT,
        metadata_resolution='Title, author and 1885 verified from selected title page. Unrelated newspaper attachment and brigade-diary description excluded from current metadata. Full original MD description retained only as source evidence in metadata/md-export.json. Original PDF bytes unchanged.',
        actor='Vladimir',master_decided_at=now,decision_note=NOTE,deletion_authorized='false')
    tables['objects'].insert(1,obj)
    base={f:'' for f in tables['files'][0]}
    base.update(object_key=KEY,title=TITLE,content_note=EXTENT,md_object_id='50002',selected_candidate_id=CID,sequence='1',date='1885',creator=obj['catalogued_book_creator'],book_title=TITLE,content_match_status='matched',object_url=obj['object_url'],metadata_path='metadata/objects.jsonl')
    master=dict(base,relative_path=MASTER,file_role='master_member',description=EXTENT,pages='5',source_path=str(source),source_size_bytes=str(member['size_bytes']),source_mtime_ns=str(member['mtime_ns']),source_sha256=master_hash,size_bytes=str((U/MASTER).stat().st_size),final_sha256=master_hash)
    preview=dict(base,relative_path=PREVIEW,file_role='access_preview',description='Page 1 preview generated from the selected Urbanitzky Master during accession.',pages='1',source_path=str(U/MASTER),source_size_bytes=master['size_bytes'],source_mtime_ns=str((U/MASTER).stat().st_mtime_ns),source_sha256=master_hash,master_relative_path=MASTER,width=str(width),height=str(height),size_bytes=str((U/PREVIEW).stat().st_size),final_sha256=preview_hash)
    tables['files'] += [master,preview]
    for r in tables['source_disposition']:
        if r['inventory']!=KEY: continue
        if r['candidate_ids']==KEY+':md':
            r.update(preservation_basis='Excluded unrelated newspaper attachment; retained in work. Correct local Urbanitzky Master accessioned separately.',notes='Unrelated attachment remains excluded; no source deletion authorized.')
        else:
            r.update(source_role='selected_master_member' if r['candidate_ids']==CID else 'redundant_candidate',final_path=MASTER,final_sha256=master_hash,exact_copy_proven='true',preservation_status='preserved_exact_copy',preservation_basis='Stored source hash equals finalized selected PDF hash; recorded source size and mtime unchanged.',drain_state='retained',notes=NOTE)
    changes={}
    def csv_change(relative,fields,rows): changes[relative]=csv_bytes(fields,rows)
    csv_change('00_MANIFESTS/master-decisions.csv',decision_fields,decisions)
    csv_change('00_MANIFESTS/candidate-dispositions.csv',disp_fields,dispositions)
    # The ineligible MD row remains work evidence, outside the active candidate view.
    csv_change('metadata/candidates.csv',candidate_fields,[c for c in candidates if c['eligible']=='true'])
    csv_change('00_MANIFESTS/master-representations.csv',list(obj),tables['objects'])
    changes['metadata/objects.jsonl']=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in tables['objects']).encode()
    source_fields=list(tables['source_disposition'][0])
    csv_change('00_MANIFESTS/source-disposition.csv',source_fields,tables['source_disposition'])
    for r in tables['files']:
        p=r['relative_path']
        if p in changes: r.update(size_bytes=str(len(changes[p])),final_sha256=sha256(changes[p]).hexdigest())
    file_fields=list(base)
    csv_change('db-restore/files.csv',file_fields,tables['files'])
    for name,role in [('master-files.csv','master_member'),('derivatives.csv','access_preview')]:
        csv_change('00_MANIFESTS/'+name,file_fields,[r for r in tables['files'] if r['file_role']==role])
    fields,catalog=read_csv(U/'CATALOG.csv')
    for r in catalog:
        n=next(f for f in tables['files'] if f['relative_path']==r['final_relative_path'])
        r.update(size_bytes=n['size_bytes'],final_sha256=n['final_sha256'])
    for n in (master,preview):
        r={f:'' for f in fields}; r.update(record_key=KEY,inventory=KEY,title=TITLE,file_role='preservation_master' if n is master else 'access_preview',final_relative_path=n['relative_path'],master_relative_path=MASTER,filename=Path(n['relative_path']).name,file_type=Path(n['relative_path']).suffix[1:],date_text='1885',md_url=obj['object_url'],original_source_path=n['source_path'],derivative_of='' if n is master else MASTER,provenance_path='db-restore/files.csv',master_representation_id=CID,master_representation_type='single_file',master_member_count='1',native_file_role=n['file_role'],condition=EXTENT)
        for f in ('size_bytes','final_sha256','description','creator','md_object_id','source_sha256','sequence','width','height','metadata_path','pages','content_note','content_match_status'): r[f]=n[f]
        catalog.append(r)
    csv_change('CATALOG.csv',fields,catalog)
    common_fields,common=read_csv(U/'db-restore/objects.csv')
    co={f:'' for f in common_fields}; co.update(record_key=KEY,inventory=KEY,title=TITLE,master_representation_id=CID,master_representation_type='single_file',master_member_count='1',ordered_master_paths=json.dumps([MASTER]),access_pdf_paths='[]',preview_paths=json.dumps([PREVIEW]),page_image_paths='[]',access_note='Selected PDF is the private Master. '+EXTENT)
    for f,v in obj.items(): co['native_'+f]=v
    assert set(co)==set(common_fields); common.insert(1,co)
    csv_change('db-restore/objects.csv',common_fields,common)
    for p,old in old_stamps.items(): assert stamp(U/p)==old
    for p,payload in changes.items(): (U/p).write_bytes(payload)
    update_workbook(U/'CATALOG.xlsx',[('Catalog',fields,catalog),('Objects',common_fields,common)])
    checksum_fields,_=read_csv(U/'00_MANIFESTS/payload-checksums.csv')
    checksum=[dict(relative_path=r['relative_path'],size_bytes=r['size_bytes'],mtime_ns=str((U/r['relative_path']).stat().st_mtime_ns),sha256=r['final_sha256']) for r in tables['files']]
    (U/'00_MANIFESTS/payload-checksums.csv').write_bytes(csv_bytes(checksum_fields,checksum))
    con=sqlite3.connect(db)
    with con:
        for t,rows in tables.items():
            con.execute('DELETE FROM '+t)
            con.executemany('INSERT INTO '+t+' VALUES ('+','.join('?' for _ in rows[0])+')',[tuple(r.values()) for r in rows])
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert con.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()==schema
    for t,rows in tables.items(): assert con.execute('SELECT * FROM '+t).fetchall()==[tuple(r.values()) for r in rows]
    con.close()
    for r in catalog:
        p=r['final_relative_path']; assert (U/p).stat().st_size==int(r['size_bytes'])
        if p in old_files and p not in changes:
            assert stamp(U/p)==old_stamps[p] and r['final_sha256']==old_files[p]['final_sha256']
    for o in tables['objects']:
        members=sorted([r for r in tables['files'] if r['object_key']==o['object_key'] and r['file_role']=='master_member'],key=lambda r:int(r['sequence']))
        assert json.loads(o['ordered_master_paths'])==[r['relative_path'] for r in members]
        assert {r['selected_candidate_id'] for r in members}=={o['selected_candidate_id']}
    assert len(catalog)==25 and all(r['deletion_authorized']=='false' for r in tables['source_disposition'])
    assert {str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}==dirs
    sources_unchanged()
    for sheet in load_workbook(U/'CATALOG.xlsx'): render(sheet,W/('urbanitzky-after-'+sheet.title+'.png'))
    report=dict(unit='bibliothek',completed_at=now,validation_result='ok',authorization='oklets finish that',master_authorization='Select this five-page PDF as Master',selected_candidate_id=CID,source_path=str(source),master_path=MASTER,master_sha256=master_hash,preview_path=PREVIEW,pdf_pages=5,complete_book_scan=False,objects=3,master_representations=3,master_files=4,retained_previews=4,new_derivatives=1,payload_files=25,redundant_candidates=7,excluded_candidates=1,source_paths=12,exact_copy_source_paths=11,sqlite_csv_xlsx_agree=True,schema_unchanged=True,directory_structure_unchanged=True,sources_modified=False,deletion_authorized=False,publication_performed=False)
    receipt=json.loads((U/'00_MANIFESTS/accession-completion.json').read_text())
    receipt.update(updated_at=now,objects=3,master_representations=3,master_files=4,pdf_master_pages=12,retained_derivatives=4,redundant_candidates=7,excluded_candidates=1,exact_copy_source_paths=11,unpreserved_local_urbanitzky_paths=0,payload_files=25,final_files=sum(p.is_file() for p in U.rglob('*')),known_content_mismatch='Unrelated MD attachment excluded; selected local Urbanitzky PDF matches the book and contains five scanned pages.',BIB_1002_book_content_preserved=True,BIB_1002_complete_book_preserved=False,BIB_1002_current_status='accessioned_five_page_partial_scan',urbanitzky_accession=report)
    (U/'00_MANIFESTS/accession-completion.json').write_bytes(json_bytes(receipt))
    (W/'urbanitzky-accession-completion.json').write_bytes(json_bytes(report))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    assert sys.argv[1:] in (['--check'],['--apply'])
    main(sys.argv[1]=='--apply')
