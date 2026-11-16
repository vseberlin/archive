"""Apply Vladimir's September 13 NEF decisions to the existing accession.

No accession restart, new directories, schema changes or source reads. The only
media changes are page 5's reading JPEG and page 11's descriptive metadata.
"""
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from collections import Counter

from PIL import Image
from openpyxl import load_workbook
import wf_alben_accession as accession
import wf_alben_workbench as workbench
import nef_workbench as nef

W, U, R = accession.WORK, accession.UNIT, accession.RECON
REPORT = W / 'nef-finalization.json'
PROFILE = 'imagemagick-full-resolution-gray-levels-65-80-gamma1-jpeg-q95'
TITLE = 'Fotoalbum NEF, S. 11, Gesamtansicht; Foto 1946'
NOW = datetime.now(timezone.utc).isoformat()
TABLE_FILES = {'objects':'metadata/objects.csv', 'representations':'db-restore/representations.csv',
 'checksums':'db-restore/checksums.csv','reconciliation_decisions':'db-restore/reconciliation-decisions.csv',
 'source_disposition':'db-restore/source-disposition.csv',
 'metadata_editorial_decisions':'db-restore/metadata-editorial-decisions.csv'}

def read(path):
    with path.open(newline='', encoding='utf-8') as f:
        reader=csv.DictReader(f);return reader.fieldnames,list(reader)

def ro(path):
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c

def snap(c):
    return {t:[dict(r) for r in c.execute('SELECT * FROM "'+t+'"')]
            for t, in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

def binding(p):
    s=p.stat();return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def cell_text(value):
    return str(value if value is not None else '').replace('\r\n','\n').replace('\r','\n')

NUMERIC={'size_bytes','sequence','width','height','presentation_rotation_cw','master_size_bytes','access_size_bytes'}

def write_csv(path, fields, rows):
    assert path.parent.is_dir()
    accession.atomic_bytes(path,accession.csv_bytes(fields,rows))

def pixels_equal(a,b):
    with Image.open(a) as x, Image.open(b) as y:
        assert (x.mode,x.size)==(y.mode,y.size)
        assert x.tobytes()==y.tobytes(),f'Pixels changed: {a.name}'

def change_row(c,table,key_field,key,values):
    query='UPDATE "'+table+'" SET '+','.join('"'+f+'"=?' for f in values)+' WHERE "'+key_field+'"=?'
    assert c.execute(query,[*values.values(),key]).rowcount==1,(table,key)

def main():
    assert sys.argv[1:] == ['apply-approved-nef-decisions']
    assert not REPORT.exists(), 'Existing finalization report: inspect it; do not repeat this command.'
    assert W.resolve()==W and U.resolve()==U and R.is_dir()
    receipt_path=U/'00_MANIFESTS/accession-completion.json'
    receipt=json.loads(receipt_path.read_text());assert 'nef_finalization' not in receipt
    c=ro(U/'db-restore/RESTORE.sqlite');before=snap(c)
    schema=c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
    assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    c.close()
    fields={t:read(U/p)[0] for t,p in TABLE_FILES.items()}
    for t,p in TABLE_FILES.items():assert read(U/p)[1]==before[t],p
    assert read(U/'db-restore/RESTORE.csv')[1]==before['objects']
    catalog_fields,catalog=read(U/'CATALOG.csv')
    originals=copy.deepcopy(catalog)
    assert len(catalog)==317 and len(before['objects'])==143
    members=read(U/'metadata/album-memberships.csv')[1]
    album_by_key={r['record_key']:r['album_key'] for r in members}
    original_wb=load_workbook(U/'CATALOG.xlsx')
    for name,rows in {'Catalog':catalog,'Objects':[dict(o,album_key=album_by_key[o['record_key']]) for o in before['objects']]}.items():
        s=original_wb[name];headers=[cell.value for cell in s[1]]
        assert s.max_row==len(rows)+1
        for i,row in enumerate(rows,2):
            for j,f in enumerate(headers,1):assert cell_text(s.cell(i,j).value)==cell_text(row[f]),(name,i,f)
    keys={r['record_key'] for r in members if r['album_key']=='ALBUM-NEF' and r['member_kind']=='accessioned_image'}
    assert len(keys)==24
    app=nef.NEFWorkbench();assert app.check()['passed']
    wc=ro(R/'workbench.sqlite');work_before=snap(wc)
    decisions={r['record_key']:r for r in workbench.decision_rows(wc)}
    wc.close()
    objects={r['record_key']:r for r in before['objects']}
    for key in keys:
        assert objects[key]['selected_candidate_id']==decisions[key]['selected_candidate_id']
    assert 'levels normalization' in decisions['md:93484']['note']
    assert 'TIFF is acceptable' in decisions['md:93485']['note']
    assert 'this is page 11' in decisions['md:93489']['note']
    media_before={r['final_relative_path']:binding(U/r['final_relative_path']) for r in catalog}
    assert all(not (U/p).is_symlink() for p in media_before)
    directories={str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    controls_before={str(p.relative_to(U)):binding(p) for base in [U,U/'metadata',U/'db-restore',U/'00_MANIFESTS']
                     for p in base.iterdir() if p.is_file()}
    source_dispositions=copy.deepcopy(before['source_disposition'])

    # Prepare only affected files in the existing work root. No TIFF pixels change.
    page5=objects['md:93484'];page11=objects['md:93489']
    assert page11['title']==page11['image_name']=='Fotoalbum NEF, S. 7, Gesamtansicht; Foto 1946'
    stage5=W/'nef-page-05-levels.jpg'
    assert stage5.is_file() and accession.dimensions(stage5)==accession.dimensions(U/page5['master_relative_path'])
    assert accession.dimensions(stage5)[:2]==(4571,3616)
    with Image.open(stage5) as image:
        image.load();assert image.mode=='L'
    row5=dict(page5,source_path=page5['selected_master_source_path'])
    plan5=accession.metadata_plan(row5,page5['source_sha256'],page5['master_relative_path'],page5['master_sha256'])
    plan5.update({'XMP-xmp:CreatorTool':'ImageMagick; grayscale levels black=65%, white=80%, gamma=1.0; full resolution JPEG Q95',
                  'XMP-photoshop:Instructions':'Reading copy with black, white and gray levels normalized by human decision 2026-09-13. Selected color TIFF Master unchanged.'})
    accession.write_metadata(stage5,plan5);accession.verify_metadata(stage5,plan5)
    title_plan={tag:TITLE for tag in ['XMP-dc:Title','XMP-iptcExt:ArtworkTitle','IPTC:ObjectName']}
    staged11=W/'nef-page-11-metadata.tif'
    assert not staged11.exists()
    accession.copy_with_metadata(U/page11['master_relative_path'],staged11,title_plan)
    accession.verify_metadata(staged11,title_plan)
    accession.verify_master(U/page11['master_relative_path'],staged11,verify_pixels=False)
    pixels_equal(U/page11['master_relative_path'],staged11)
    staged11jpg=W/'nef-page-11-metadata.jpg';assert not staged11jpg.exists()
    shutil.copy2(U/page11['access_relative_path'],staged11jpg)
    accession.write_metadata(staged11jpg,title_plan);accession.verify_metadata(staged11jpg,title_plan)
    pixels_equal(U/page11['access_relative_path'],staged11jpg)

    # Guard each existing target immediately before the explicitly authorized correction.
    modified=[page5['access_relative_path'],page11['master_relative_path'],page11['access_relative_path']]
    assert all(binding(U/p)==s for p,s in media_before.items())
    assert all(binding(U/p)==s for p,s in controls_before.items())
    hashes={}
    for relative,staged in [(page5['access_relative_path'],stage5),(page11['master_relative_path'],staged11)]:
        assert binding(U/relative)==media_before[relative]
        os.replace(staged,U/relative)
        hashes[relative]='sha256:'+sha(U/relative)  # One final payload hash, after placement.
    new_master_hash=hashes[page11['master_relative_path']]
    row11=dict(page11,title=TITLE,image_name=TITLE,source_path=page11['selected_master_source_path'])
    plan11=accession.metadata_plan(row11,page11['source_sha256'],page11['master_relative_path'],new_master_hash)
    accession.write_metadata(staged11jpg,plan11);accession.verify_metadata(staged11jpg,plan11)
    pixels_equal(U/page11['access_relative_path'],staged11jpg)
    os.replace(staged11jpg,U/page11['access_relative_path'])
    hashes[page11['access_relative_path']]='sha256:'+sha(U/page11['access_relative_path'])

    # Reuse the existing editorial overlay for the two corrected title fields.
    editorial=[]
    for field in ['object_name','image:69829:name']:
        editorial.append({'md_object_id':'93489','inventory':page11['inventory'],'field_name':field,
          'original_value':page11['title'],'resolved_value':TITLE,'decision_state':'resolved_corrected',
          'basis':'Explicit human confirmation: page 11 is page 11. Finalization authorized 2026-09-13; original captured title retained as provenance.',
          'decided_by':'vladimir','decided_at':NOW,'deletion_authorized':'false'})
    wc=sqlite3.connect(R/'workbench.sqlite');wc.row_factory=sqlite3.Row
    assert snap(wc)==work_before,'Workbench decisions changed during preparation'
    for row in editorial:
        vals=dict(row,deletion_authorized=0)
        wc.execute('INSERT INTO metadata_editorial_decisions ('+','.join(vals)+') VALUES ('+','.join('?' for _ in vals)+')',list(vals.values()))
    wc.execute("UPDATE meta SET value=? WHERE key='expected_metadata_editorial_decisions'",(str(len(before['metadata_editorial_decisions'])+2),))
    for key,note in {
      'md:93484':'Finalized 2026-09-13: full-resolution reading JPEG generated from the retained TIFF with grayscale levels black=65%, white=80%, gamma=1.0, JPEG quality 95. TIFF unchanged.',
      'md:93485':'Finalized 2026-09-13: selected TIFF and existing reading image retained without changes.',
      'md:93489':'Finalized 2026-09-13: title and image title corrected to S. 11 in catalogue and embedded metadata. Master and JPEG pixels unchanged. Original S. 7 title retained in editorial provenance.'}.items():
        wc.execute('UPDATE decisions SET note=?,decided_by=?,decided_at=? WHERE record_key=?',
                   (decisions[key]['note']+'\n\n'+note,'vladimir',NOW,key))
    wc.commit();nef.export_current(wc)
    metadata_rows=workbench.metadata_editorial_rows(wc)
    write_csv(R/'metadata-editorial-decisions.csv',workbench.METADATA_EDITORIAL_FIELDS,metadata_rows)
    decisions={r['record_key']:r for r in workbench.decision_rows(wc)}
    nef.mirrors_match(wc);wc.close()

    db=sqlite3.connect(U/'db-restore/RESTORE.sqlite');db.row_factory=sqlite3.Row
    for key in keys:
        values={f:decisions[key][f] for f in ['note','decided_by','decided_at']}
        change_row(db,'reconciliation_decisions','record_key',key,values)
    for key in ['md:93484','md:93489']:
        o=objects[key];updates={}
        for role in ['master','access']:
            path=o[role+'_relative_path']
            if path in hashes:
                updates[role+'_sha256']=hashes[path];updates[role+'_size_bytes']=str((U/path).stat().st_size)
                change_row(db,'representations','final_relative_path',path,{'final_sha256':hashes[path],'size_bytes':updates[role+'_size_bytes']})
                change_row(db,'checksums','path',path,{'sha256':hashes[path],'size_bytes':updates[role+'_size_bytes']})
        if key=='md:93484':updates['derivative_profile']=PROFILE
        else:updates.update(title=TITLE,image_name=TITLE)
        change_row(db,'objects','record_key',key,updates)
    change_row(db,'representations','final_relative_path',page5['access_relative_path'],{'copy_policy':PROFILE,'basis':'generated_from_selected_master_during_authorized_accession_finalization; user-approved black/white/gray levels'})
    change_row(db,'representations','final_relative_path',page11['access_relative_path'],{'source_sha256':new_master_hash})
    db.execute('UPDATE source_disposition SET final_master_sha256=? WHERE record_key=?',(new_master_hash,'md:93489'))
    for row in editorial:
        db.execute('INSERT INTO metadata_editorial_decisions ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
    db.commit();after=snap(db)
    assert db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()==schema
    for table in ['candidate_dispositions','linked_object_relationships','derived_image_relationships','document_provenance','meta']:
        assert after[table]==before[table],table
    for table,p in TABLE_FILES.items():write_csv(U/p,fields[table],after[table])
    write_csv(U/'db-restore/RESTORE.csv',fields['objects'],after['objects'])
    accession.atomic_bytes(U/'db-restore/RESTORE.sql',('\n'.join(db.iterdump())+'\n').encode())
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()
    db.close()
    reps={r['final_relative_path']:r for r in after['representations']}
    for row in catalog:
        p=row['final_relative_path']
        if row['record_key']=='md:93489':row['title']=TITLE
        if p in hashes:
            row['final_sha256']=hashes[p].removeprefix('sha256:');row['size_bytes']=str((U/p).stat().st_size)
        if p==page5['access_relative_path']:
            row['notes']=reps[p]['basis']+'; '+PROFILE
            row['width']='4571';row['height']='3616'
        if p==page11['access_relative_path']:row['source_sha256']=new_master_hash.removeprefix('sha256:')
    write_csv(U/'CATALOG.csv',catalog_fields,catalog)

    # Artifact Tool cannot load XLSX in this runtime (WASM SIMD unavailable).
    # Use the established openpyxl fallback and preserve every unrelated cell/style.
    wb=load_workbook(U/'CATALOG.xlsx')
    sheets={'Catalog':catalog,'Objects':[dict(o,album_key=album_by_key[o['record_key']]) for o in after['objects']]}
    cell_changes=[]
    for name,rows in sheets.items():
        sheet=wb[name];headers=[cell.value for cell in sheet[1]]
        assert (sheet.max_row,sheet.max_column)==(len(rows)+1,len(headers))
        for ri,row in enumerate(rows,2):
            for ci,f in enumerate(headers,1):
                cell=sheet.cell(ri,ci);desired=row[f]
                if cell_text(cell.value)!=cell_text(desired):
                    assert row['record_key'] in {'md:93484','md:93489'}
                    cell_changes.append((name,cell.coordinate,cell.value,desired))
                    cell.value=int(desired) if f in NUMERIC and desired!='' else desired or None
    xlsx=W/'nef-catalogue.xlsx';wb.save(xlsx)
    checked=load_workbook(xlsx)
    assert checked.sheetnames==original_wb.sheetnames
    for name,rows in sheets.items():
        s=checked[name];old=original_wb[name];headers=[c.value for c in s[1]]
        assert s.freeze_panes==old.freeze_panes and s.auto_filter.ref==old.auto_filter.ref
        assert {k:dict(v) for k,v in s.column_dimensions.items()}=={k:dict(v) for k,v in old.column_dimensions.items()}
        assert {k:dict(v) for k,v in s.row_dimensions.items()}=={k:dict(v) for k,v in old.row_dimensions.items()}
        assert dict(s.tables.items())==dict(old.tables.items())
        for ri,row in enumerate(rows,2):
            for ci,f in enumerate(headers,1):
                cell=s.cell(ri,ci);assert cell_text(cell.value)==cell_text(row[f])
                assert cell._style==old.cell(ri,ci)._style
    os.replace(xlsx,U/'CATALOG.xlsx')
    readme=U/'00_README.md'
    readme.write_text(readme.read_text()+f'\n## NEF decisions finalized — 2026-09-13\n\nAll 24 selected NEF TIFF Masters remain selected. Page 5 has a full-resolution\n4571 × 3616 grayscale reading JPEG generated from its unchanged selected TIFF\nwith levels black=65%, white=80%, gamma=1.0, JPEG quality 95. Framing and pixel\npositions are preserved. Page 7 and its framing are accepted unchanged.\nPage 11 has the corrected title `{TITLE}` in the catalogue and embedded metadata.\nIts TIFF and JPEG pixels are unchanged. The original S. 7 title remains in\n`db-restore/metadata-editorial-decisions.csv` and the immutable metadata capture.\nThe 39 linked detail records and all Master choices are unchanged. Sources were\nnot accessed, and no publication or deletion occurred. The existing accession\nreceipt records the bounded finalization and affected file checksums.\n')

    # Refresh the existing control-file inventory, reusing unchanged payload hashes.
    changed_controls=[p for p,s in controls_before.items() if binding(U/p)!=s]
    payload_fields,payload=read(U/'00_MANIFESTS/payload-checksums.csv')
    control_hashes={p:sha(U/p) for p in changed_controls}
    for row in payload:
        p=row['relative_path']
        if p in control_hashes:
            row['sha256']=control_hashes[p];row['size_bytes']=str((U/p).stat().st_size)
    write_csv(U/'00_MANIFESTS/payload-checksums.csv',payload_fields,payload)
    control_hashes['00_MANIFESTS/payload-checksums.csv']=sha(U/'00_MANIFESTS/payload-checksums.csv')
    for p,h in control_hashes.items():receipt['control_file_sha256'][p]='sha256:'+h
    receipt['metadata_decisions']=len(after['metadata_editorial_decisions'])
    result={'completed_utc':NOW,'authorization':'ok so we can finalize it',
      'scope':'NEF decisions of 2026-09-13 applied to existing accession',
      'result':'ok','selected_nef_masters':24,'all_unit_master_selections_unchanged':143,
      'master_pixels_unchanged':True,'master_files_byte_unchanged':142,
      'modified_media':{p:{'sha256':hashes[p],'size_bytes':str((U/p).stat().st_size)} for p in modified},
      'page5':{'master':page5['master_relative_path'],'access':page5['access_relative_path'],
          'width':4571,'height':3616,'colorspace':'gray','black_point_percent':65,'white_point_percent':80,'gamma':1.0,'jpeg_quality':95},
      'page7':'selected TIFF and access JPEG unchanged',
      'page11':{'title':TITLE,'original_title':page11['title'],'pixel_data_unchanged':True},
      'media_generated':1,'metadata_only_media_updates':2,'final_media_hashes_calculated':3,
      'unchanged_media_rehashed':False,'file_catalogue_rows':317,'image_objects':143,
      'linked_nef_details_unchanged':39,'candidate_dispositions_unchanged':1070,
      'source_accessed':False,'source_modified':False,'publication_performed':False,
      'deletion_authorized':False,'new_directories':False,'schema_changed':False,
      'xlsx_cells_changed':len(cell_changes),'xlsx_all_cells_and_styles_checked':True,
      'xlsx_artifact_tool_blocker':'WASM SIMD unavailable; established openpyxl fallback used'}
    receipt['nef_finalization']=result
    accession.atomic_json(receipt_path,receipt)

    # Final independent checks of existing restore projections and preserved scope.
    final=ro(U/'db-restore/RESTORE.sqlite');assert snap(final)==after
    restored=sqlite3.connect(':memory:');restored.row_factory=sqlite3.Row
    restored.executescript((U/'db-restore/RESTORE.sql').read_bytes().decode('utf-8'));assert snap(restored)==after
    for table,p in TABLE_FILES.items():assert read(U/p)[1]==after[table]
    assert all(binding(U/p)==b for p,b in media_before.items() if p not in modified)
    assert directories=={str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    assert Counter(r['role'] for r in after['representations'])=={'preservation_master':143,'access_derivative':143}
    checks={r['path']:r for r in after['checksums']}
    cat={r['final_relative_path']:r for r in catalog}
    for o in after['objects']:
        assert o['selected_candidate_id']==objects[o['record_key']]['selected_candidate_id']
        for role in ['master','access']:
            p=o[role+'_relative_path'];assert (U/p).stat().st_size==int(o[role+'_size_bytes'])
            assert o[role+'_sha256']==reps[p]['final_sha256']==checks[p]['sha256']=='sha256:'+cat[p]['final_sha256']
        assert len([r for r in after['representations'] if r['record_key']==o['record_key'] and r['role']=='preservation_master'])==1
    for old,new in zip(source_dispositions,after['source_disposition']):
        assert {k:v for k,v in old.items() if k!='final_master_sha256'}=={k:v for k,v in new.items() if k!='final_master_sha256'}
        if old['record_key']!='md:93489':assert old==new
    assert all(r.get('deletion_authorized','false')=='false' for rows in after.values() for r in rows)
    for p,h in receipt['control_file_sha256'].items():assert 'sha256:'+sha(U/p)==h,p
    assert nef.NEFWorkbench().check()['passed']
    result.update(sqlite_csv_agree=True,sql_restore_verified=True,all_final_sizes_and_stored_hashes_agree=True)
    receipt['nef_finalization']=result;accession.atomic_json(receipt_path,receipt)
    accession.atomic_json(REPORT,result)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
