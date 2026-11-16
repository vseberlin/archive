"""Bounded, user-authorized normalization of three existing photo units.

prepare writes reviewable files only to iss-archive-work. apply changes one
named unit. Existing Masters, media bytes, decisions and DB schemas are retained.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/mnt/volume/iss-archive/units')
WORK = Path('/mnt/volume/iss-archive-work')
UNITS = ('baeckerhandwerk', 'tfa', 'pressebildarchiv-wf')
FIELDS = ['record_key','inventory','title','file_role','final_relative_path',
          'master_relative_path','filename','file_type','size_bytes','final_sha256',
          'description','date_text','creator','object_type','material','dimensions',
          'places','tags','md_object_id','md_url','rights','rights_holder',
          'metadata_authority','original_source_path','source_sha256','derivative_of',
          'sequence','width','height','presentation_rotation_cw','relationship',
          'access_restriction','condition','notes','metadata_path','provenance_path']
CONFIG = {
 'baeckerhandwerk': {'objects':'db-restore/RESTORE.csv','reps':'00_MANIFESTS/representations.csv','docs':'00_MANIFESTS/documents.csv','checks':'00_MANIFESTS/payload-checksums.csv','expected':76},
 'tfa': {'objects':'db-restore/RESTORE.csv','reps':'db-restore/manifests/representations.csv','docs':'db-restore/manifests/documents.csv','checks':'db-restore/checksums.csv','expected':7762},
 'pressebildarchiv-wf': {'objects':'metadata/objects.csv','reps':'00_MANIFESTS/representations.csv','docs':'00_MANIFESTS/documents.csv','checks':'00_MANIFESTS/checksums.csv','expected':48},
}

def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        r=csv.DictReader(f); return r.fieldnames, list(r)

def csv_bytes(fields, rows):
    f=io.StringIO(newline=''); w=csv.DictWriter(f,fieldnames=fields,lineterminator='\n')
    w.writeheader();w.writerows(rows);return f.getvalue().encode('utf-8')

def staged(unit, rel): return WORK / ('photo-' + unit + '--' + rel.replace('/', '__'))

def jwrite(path, value): Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def path_of(r): return r.get('final_relative_path', r.get('relative_path',''))
def key_of(r): return r.get('record_key',r.get('object_key',r.get('archival_object_id','')))

def remap_text(text, moves, unit):
    # Match literal unit-relative references, never historical work/source paths.
    for old,new in sorted(moves.items(),key=lambda x:-len(x[0])):
        if old == 'catalog.csv': continue
        text=text.replace(str(ROOT/unit/old),str(ROOT/unit/new))
        text=re.sub(r'(?<![\w/])'+re.escape(old)+r'(?![\w.])',lambda _:new,text)
    return text

def schema_text(unit):
    cfg=CONFIG[unit]
    return '''# Photo catalogue fields

`CATALOG.csv` and the Catalog sheet in `CATALOG.xlsx` contain the same current
file rows. The Objects sheet preserves the complete existing object catalogue,
including aliases or holds without a Master. The CSV records are UTF-8; blank
means not recorded, not zero and not public permission. Identifiers and dates
remain source text. No new identifiers or metadata decisions are introduced.

The catalogue covers the media and documentation registered in the existing
representation/document tables. Supporting metadata and generated control files
are described by the unit README and manifests; they are not new archival objects.

| Columns | Meaning |
| --- | --- |
| record_key, inventory | Existing object key and inventory; document rows may use their existing document ID or unit relationship. |
| title, description, date_text, creator, object_type, material, dimensions, places, tags | Accepted descriptive values, retained without editorial correction. |
| file_role | preservation_master, access_derivative, or the existing more specific documentation/supporting-image role. TIFF derivatives are not Masters. |
| final_relative_path, filename, file_type | Current file location relative to the unit; no drive-specific prefix is needed. |
| master_relative_path, derivative_of | Selected Master and recorded derivation; blank for unrelated unit documentation or original supporting views. |
| size_bytes, final_sha256 | Final file size and existing SHA-256; the optional sha256: prefix is omitted only in this common view. |
| original_source_path, source_sha256 | Original recorded source and its checksum; historical paths remain provenance. |
| sequence | Existing member order, when supplied; never inferred from filenames. |
| width, height, presentation_rotation_cw | Existing technical dimensions and presentation rotation; Masters are not rotated by catalogue normalization. |
| md_object_id, md_url, metadata_authority | Existing catalogue identity and authority; no live MD refresh is implied. |
| rights, rights_holder, access_restriction | Existing recorded rights and restrictions. Empty values grant no publication permission. |
| relationship, condition, notes | Existing relationships, condition and selection/provenance notes. |
| metadata_path, provenance_path | Unit-relative readable records with the complete original object and file metadata. |

Exactly one preservation_master row exists per resolved photo object. Additional
source images and manufactured derivatives keep their explicit roles. Redundant
source decisions and deletion_authorized=false remain in the source registers.

Recovery starts with the Objects CSV, representation and documentation CSVs,
existing decision/source registers, and SQL restore where present. SQLite is a
rebuildable index. The workbook is a generated view, not a second editing authority.

'''+f'Objects CSV: `{cfg["objects"]}`. File provenance: `{cfg["reps"]}` and `{cfg["docs"]}`.\n'

def prepare(unit):
    u=ROOT/unit; cfg=CONFIG[unit]; assert u.is_dir() and WORK.is_dir()
    assert not (u/'metadata/catalog-fields.md').exists(), 'Already normalized; use verify'
    oh,objects=read_csv(u/('catalog.csv' if unit=='pressebildarchiv-wf' else 'CATALOG.csv'))
    rh,reps=read_csv(u/cfg['reps']);dh,docs=read_csv(u/cfg['docs'])
    moves={}
    if unit=='pressebildarchiv-wf':
        for r in reps:
            if r['role']=='full_resolution_jpeg_derivative':
                moves[r['relative_path']]='export/access/'+Path(r['relative_path']).name
        moves['export/Inventar Primus 1 und 2_RECOVERED.xlsx']='documents/Inventar Primus 1 und 2_RECOVERED.xlsx'
        moves['catalog.csv']=cfg['objects']
        assert len(moves)==50
    else:
        assert read_csv(u/cfg['objects']) == (oh,objects), 'Object restore CSV differs'
    plan={'unit':unit,'prepared_utc':datetime.now(timezone.utc).isoformat(),'authorization':'right. do first batch',
          'moves':moves,'inputs':{},'writes':[],'payload_stats':{},'expected_masters':cfg['expected']}
    for old,new in moves.items():
        assert (u/old).is_file() and not (u/new).exists(), (old,new)
    # Capture unchanged byte-bearing file identity without rehashing media.
    for r in reps+docs:
        pth=path_of(r)
        if pth in plan['payload_stats']:continue
        st=(u/pth).stat(); stored=r.get('final_sha256',r.get('sha256','')).removeprefix('sha256:')
        assert st.st_size==int(r['size_bytes']), pth
        plan['payload_stats'][pth]={'size':st.st_size,'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'sha256':stored}
    # Only text controls are staged; no media or documentation originals copied.
    text_files=[]
    for base in [u,u/'metadata',u/'00_MANIFESTS',u/'db-restore',u/'db-restore/manifests']:
        if base.exists():
            text_files += [p for p in base.iterdir() if p.is_file() and p.suffix in ('.csv','.json','.jsonl','.md')]
    for p in text_files:
        rel=str(p.relative_to(u));plan['inputs'][rel]=sha(p)
        if unit=='pressebildarchiv-wf' and rel not in ['catalog.csv',cfg['checks'],'00_README.md','00_MANIFESTS/accession-completion.json']:
            old=p.read_text();new=remap_text(old,moves,unit)
            if old!=new:
                staged(unit,rel).write_text(new);plan['writes'].append(rel)
    def mapped(rows):
        return [{k:remap_text(v,moves,unit) for k,v in r.items()} for r in rows]
    objects=mapped(objects);reps=mapped(reps);docs=mapped(docs)
    if unit=='pressebildarchiv-wf':
        staged(unit,cfg['objects']).write_bytes(csv_bytes(oh,objects));plan['writes'].append(cfg['objects'])
        text='# Pressebildarchiv WF documentation\n\nFour retained source/derived documents support the photo records. Dates are not newly inferred.\n\n'
        for r in docs:
            text+=f'## {r["filename"]}\n\n- File: `{r["relative_path"]}`\n- Role: {r["role"]}\n- Source: `{r["original_source_path"]}`\n- Relationship: direct Primus catalogue documentation for this unit\n- Condition: {r["integrity_state"]}\n- Description: {r["review_note"]}\n- Coverage: {r["coverage"]}\n'
            if r['derivative_of']:text+=f'- Derived from: `{r["derivative_of"]}`\n'
            text+='\n'
        text+='Original workbook and recovered workbook remain distinct, with their existing provenance and hashes in `00_MANIFESTS/documents.csv`. No publication or source deletion is authorized.\n'
        staged(unit,'documents/CONTENTS.md').write_text(text);plan['writes'].append('documents/CONTENTS.md')
    bykey={key_of(r):r for r in objects};docmap={path_of(r):r for r in docs}
    combined={path_of(r):r for r in reps}
    for r in docs: combined.setdefault(path_of(r),r)
    rows=[]
    for pth,r in combined.items():
        o=bykey.get(key_of(r),{});d=docmap.get(pth,{})
        role=r.get('role',r.get('file_role',''))
        role={'selected_master':'preservation_master','full_resolution_jpeg_derivative':'access_derivative'}.get(role,role)
        values={
         'record_key':key_of(o) or key_of(r) or d.get('document_id',''),
         'inventory':o.get('inventory',r.get('inventory','')),
         'title':o.get('title',d.get('title',d.get('description',r.get('basis',Path(pth).name)))),
         'file_role':role,'final_relative_path':pth,'master_relative_path':o.get('master_relative_path',''),
         'filename':Path(pth).name,'file_type':Path(pth).suffix.lstrip('.').lower(),
         'size_bytes':r['size_bytes'],'final_sha256':r.get('final_sha256',r.get('sha256','')).removeprefix('sha256:'),
         'description':o.get('description',d.get('description',d.get('review_note',''))),
         'date_text':o.get('date_text',o.get('date',d.get('date',''))),'creator':o.get('creator',''),
         'object_type':o.get('object_type',''),'material':o.get('material',o.get('material_technique','')),
         'dimensions':o.get('dimensions',''),'places':o.get('places',''),'tags':o.get('tags',''),
         'md_object_id':o.get('md_object_id',''),'md_url':o.get('md_url',o.get('md_object_url','')),
         'rights':o.get('rights','') if role!='related_catalogue_image_original' else '',
         'rights_holder':o.get('rights_holder','') if role!='related_catalogue_image_original' else '',
         'metadata_authority':o.get('metadata_authority',o.get('reconciliation_state','')),
         'original_source_path':r.get('original_source_path',r.get('source_path','')),
         'source_sha256':r.get('source_sha256','').removeprefix('sha256:'),
         'derivative_of':r.get('derivative_of',o.get('master_relative_path','') if role=='access_derivative' else ''),
         'sequence':r.get('sequence',''),'width':r.get('width',''),'height':r.get('height',''),
         'presentation_rotation_cw':r.get('presentation_rotation_cw',''),
         'relationship':d.get('relationship',r.get('relationship','')),
         'access_restriction':d.get('access_restriction',''),'condition':d.get('condition',d.get('integrity_state','')),
         'notes':r.get('provenance',r.get('basis',r.get('selection_basis',''))),
         'metadata_path':cfg['objects'] if o else cfg['docs'],
         'provenance_path':cfg['docs'] if d else cfg['reps']}
        assert set(values)==set(FIELDS)
        rows.append(values)
    rows.sort(key=lambda r:(r['final_relative_path'].startswith('documents/'),r['inventory'],r['record_key'],r['file_role']!='preservation_master',r['final_relative_path']))
    counts=Counter(r['file_role'] for r in rows)
    assert counts['preservation_master']==cfg['expected']==counts['access_derivative']
    assert len({r['record_key'] for r in rows if r['file_role']=='preservation_master'})==cfg['expected']
    assert len(rows)==len({r['final_relative_path'] for r in rows})
    staged(unit,'CATALOG.csv').write_bytes(csv_bytes(FIELDS,rows));plan['writes'].append('CATALOG.csv')
    jwrite(staged(unit,'workbook.json'),{'unit':unit,'catalog_fields':FIELDS,'catalog_rows':rows,'object_fields':oh,'object_rows':objects})
    staged(unit,'metadata/catalog-fields.md').write_text(schema_text(unit));plan['writes'].append('metadata/catalog-fields.md')
    text=(u/'00_README.md').read_text()
    if unit=='pressebildarchiv-wf':
        text=text.replace('`export/` contains\nthe image derivatives generated from those Masters, the additional museum-digital images,\nand the recovered Primus workbook.', '`export/access/` contains the 48 primary full-resolution JPEG copies.\n`export/` retains the 48 metadata-bearing TIFF derivatives and 13 supporting MD image originals,\nincluding photograph backs, with their existing distinct roles. The recovered Primus workbook\nis in `documents/` alongside the original, and all four documents are indexed in `documents/CONTENTS.md`.')
        text=remap_text(text,moves,unit)
    text+='\n## Photo catalogue convention\n\n'
    text+=f'Normalized on 2026-09-11 using Kurt Schwarz as the photo-unit reference. `CATALOG.csv` and the Catalog sheet in `CATALOG.xlsx` contain the same {len(rows):,} registered media/documentation file rows. The Objects sheet contains all {len(objects):,} original object records, preserved in `{cfg["objects"]}`. `metadata/catalog-fields.md` defines the columns, file roles and recovery sources.\n\n'
    text+='Each resolved photo retains one selected Master in `masters/` and one primary full-resolution JPEG in `export/access/`. Existing source images, derivatives and supporting documents retain their explicit roles. Master selection, descriptive decisions, media bytes and source deletion permissions were not changed.\n\n'
    text+=f'File provenance: `{cfg["reps"]}`. Documentation: `{cfg["docs"]}` and `documents/CONTENTS.md`. Final hashes: `{cfg["checks"]}`. SQLite remains a rebuildable index of these readable records; the file catalogue is a view across its existing object/representation/document tables, not a replacement object table.\n\n'
    if unit=='baeckerhandwerk':text+='`00_MANIFESTS/` owns the current file and documentation manifests. The drain/document-source CSV copies under `db-restore/` are restore exports of the same records. The 16 presentation rotations remain recorded in the object and representation tables.\n'
    if unit=='tfa':text+='The Objects sheet and `db-restore/manifests/accession-holds.csv` retain duplicate MD record `md:85642` (TFA-538074), whose canonical photo is `md:55436`. It does not create another Master or a missing-photo claim. The subdivision copies under `metadata/` and `db-restore/manifests/` describe the same memberships.\n'
    if unit=='pressebildarchiv-wf':text+='TIFF derivatives remain derivatives, and the 13 additional originals remain supporting images. No automatic Master promotion, removal or publication occurred. `metadata/objects.csv` replaces the former lowercase object catalogue; original accepted fields and all descriptive detail are retained there.\n'
    staged(unit,'00_README.md').write_text(text);plan['writes'].append('00_README.md')
    plan['catalog_rows']=len(rows);plan['object_rows']=len(objects);plan['roles']=dict(counts)
    plan['writes']=sorted(set(plan['writes']));jwrite(staged(unit,'plan.json'),plan)
    print(json.dumps({k:plan[k] for k in ['unit','catalog_rows','object_rows','roles','moves','writes']},ensure_ascii=False),flush=True)

def workbook_fallback(unit):
    # Artifact Tool export is unavailable here: its .NET WASM runtime rejects SIMD.
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    data=json.loads(staged(unit,'workbook.json').read_text())
    wb=Workbook();wb.remove(wb.active)
    numeric={'size_bytes','sequence','width','height','presentation_rotation_cw','master_size_bytes','access_size_bytes','selected_master_size_bytes','master_width','master_height'}
    for name,fields,rows in [('Catalog',data['catalog_fields'],data['catalog_rows']),('Objects',data['object_fields'],data['object_rows'])]:
        ws=wb.create_sheet(name);ws.append(fields);ws.freeze_panes='C2';ws.sheet_view.showGridLines=False
        for r in rows:
            vals=[int(r[f]) if f in numeric and r.get(f,'')!='' else r.get(f,'') for f in fields]
            assert all(len(str(v))<=32767 for v in vals)
            ws.append(vals)
        for row in ws:
            line_count=1
            for c in row:
                c.font=Font(name='Arial',size=10)
                field=fields[c.column-1]
                wrap=field=='title' or (name=='Objects' and field in ('description','notes'))
                c.alignment=Alignment(vertical='top' if wrap else 'center',wrap_text=wrap)
                if c.row>1 and wrap:
                    chars=88 if field=='description' else 55
                    line_count=max(line_count,sum(max(1,len(textwrap.wrap(line,width=chars))) for line in str(c.value or '').split('\n')))
                if isinstance(c.value,str):c.data_type='s'
            ws.row_dimensions[row[0].row].height=max(23,14*line_count+7)
        for c in ws[1]:
            c.font=Font(name='Arial',size=10,bold=True,color='FFFFFF');c.fill=PatternFill('solid',fgColor='24445B')
            c.alignment=Alignment(vertical='center',horizontal='center',wrap_text=True)
        ws.row_dimensions[1].height=34
        for i,f in enumerate(fields,1):
            width=150
            if f in ('title','description','notes'):width=650 if f=='description' else 420
            elif 'path' in f or 'sha256' in f:width=450
            elif 'role' in f:width=230
            ws.column_dimensions[get_column_letter(i)].width=(width-5)/7
            if f in numeric:
                for row in ws.iter_rows(min_row=2,min_col=i,max_col=i):row[0].number_format='#,##0'
        ref=f'A1:{get_column_letter(len(fields))}{len(rows)+1}'
        tab=Table(displayName=name+'Table',ref=ref)
        tab.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
        ws.add_table(tab)
    wb.save(staged(unit,'CATALOG.xlsx'))
    print(json.dumps({'unit':unit,'xlsx':str(staged(unit,'CATALOG.xlsx')),'writer':'bundled openpyxl; Artifact Tool WASM export unavailable'}),flush=True)

def check_workbook(unit, path):
    from openpyxl import load_workbook
    data=json.loads(staged(unit,'workbook.json').read_text())
    wb=load_workbook(path,read_only=True,data_only=False)
    assert wb.sheetnames==['Catalog','Objects']
    for name,fields,rows in [('Catalog',data['catalog_fields'],data['catalog_rows']),('Objects',data['object_fields'],data['object_rows'])]:
        values=wb[name].iter_rows(values_only=True); assert list(next(values))==fields
        count=0
        for got,want in zip(values,rows,strict=True):
            assert [str(v) if v is not None else '' for v in got]==[str(want.get(f,'')) for f in fields], (name,count)
            count+=1
        assert count==len(rows)
    wb.close()

def sqlite_snapshot(path):
    c=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row
    result={}
    for t in [r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]:
        result[t]=[dict(r) for r in c.execute('select * from "'+t+'"')]
    c.close();return result

def check_file_rows(unit):
    u=ROOT/unit;cfg=CONFIG[unit];_,rows=read_csv(u/'CATALOG.csv')
    c=sqlite3.connect((u/'db-restore/RESTORE.sqlite').as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row
    rr=[dict(r) for r in c.execute('select * from representations')]
    dd=[dict(r) for r in c.execute('select * from documents')] if unit!='tfa' else []
    indexed={path_of(r):r for r in rr+dd}
    assert {r['final_relative_path'] for r in rows}==set(indexed), 'CSV/SQLite file coverage'
    for r in rows:
        v=indexed[r['final_relative_path']]
        assert int(r['size_bytes'])==int(v['size_bytes'])
        assert r['final_sha256']==v.get('final_sha256',v.get('sha256','')).removeprefix('sha256:')
        role=v.get('role',''); role={'selected_master':'preservation_master','full_resolution_jpeg_derivative':'access_derivative'}.get(role,role)
        assert role==r['file_role']
    objects_table='catalog' if unit=='baeckerhandwerk' else 'objects'
    oo=[dict(r) for r in c.execute('select * from '+objects_table)]
    native=read_csv(u/cfg['objects'])[1];bykey={key_of(r):r for r in native}
    for r in oo:
        other=bykey[key_of(r)]
        for k,v in r.items():
            if k=='deletion_authorized':assert str(v).lower() in ('0','false');continue
            assert str(v)==other[k],(unit,k,key_of(r))
    for t in [r[0] for r in c.execute("select name from sqlite_master where type='table'")]:
        cols=[r[1] for r in c.execute('pragma table_info("'+t+'")')]
        if 'deletion_authorized' in cols:
            assert not list(c.execute('select deletion_authorized from "'+t+'" where lower(cast(deletion_authorized as text)) not in (\'0\',\'false\') limit 1'))
    assert not list(c.execute('pragma foreign_key_check'))
    if unit=='pressebildarchiv-wf':assert c.execute('pragma quick_check').fetchone()[0]=='ok'
    c.close()
    return {'file_rows':len(rows),'object_rows':len(native),'master_rows':sum(r['file_role']=='preservation_master' for r in rows),'csv_sqlite_agree':True,'deletion_authorized':False}

def apply(unit):
    u=ROOT/unit;cfg=CONFIG[unit];plan=json.loads(staged(unit,'plan.json').read_text());moves=plan['moves']
    assert not (u/'metadata/catalog-fields.md').exists(), 'Already applied; run verify'
    check_workbook(unit,staged(unit,'CATALOG.xlsx'))
    for rel,digest in plan['inputs'].items():assert sha(u/rel)==digest,('Changed input',rel)
    for rel,v in plan['payload_stats'].items():
        st=(u/rel).stat();assert (st.st_size,st.st_mtime_ns,st.st_ino)==(v['size'],v['mtime_ns'],v['inode']),rel
    ch,checks=read_csv(u/cfg['checks'])
    old_db=sqlite_snapshot(u/'db-restore/RESTORE.sqlite') if unit=='pressebildarchiv-wf' else None
    # Precisely the previously proposed access directory; no other nesting added.
    if unit=='pressebildarchiv-wf':(u/'export/access').mkdir()
    for old,new in moves.items():(u/old).rename(u/new)
    changed=set(plan['writes'])|{'CATALOG.xlsx'}
    for rel in plan['writes']:shutil.copyfile(staged(unit,rel),u/rel)
    shutil.copyfile(staged(unit,'CATALOG.xlsx'),u/'CATALOG.xlsx')
    if old_db is not None:
        c=sqlite3.connect(u/'db-restore/RESTORE.sqlite');c.execute('pragma foreign_keys=ON')
        with c:
            for table in old_db:
                c.row_factory=sqlite3.Row
                for r in list(c.execute('select rowid as _rid_,* from "'+table+'"')):
                    for col,val in dict(r).items():
                        if isinstance(val,str):
                            new=remap_text(val,moves,unit)
                            if new!=val:c.execute('update "'+table+'" set "'+col+'"=? where rowid=?',(new,r['_rid_']))
        c.row_factory=None
        with (u/'db-restore/RESTORE.sql').open('w') as f:
            for line in c.iterdump():f.write(line+'\n')
        c.close();changed.update(['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql'])
        expected={t:[{k:remap_text(v,moves,unit) if isinstance(v,str) else v for k,v in r.items()} for r in rs] for t,rs in old_db.items()}
        assert sqlite_snapshot(u/'db-restore/RESTORE.sqlite')==expected,'Unexpected SQLite change'
    # Existing finalized payload hashes follow renames; only changed controls are hashed.
    digest_cache={}
    def final_digest(rel):
        if rel not in digest_cache:digest_cache[rel]=sha(u/rel)
        return digest_cache[rel]
    pk='path' if unit=='tfa' else 'relative_path'
    for r in checks:
        old=r[pk];r[pk]=moves.get(old,old)
        if r[pk] in changed:
            prefix='sha256:' if r['sha256'].startswith('sha256:') else ''
            r['sha256']=prefix+final_digest(r[pk]);r['size_bytes']=str((u/r[pk]).stat().st_size)
    if unit!='tfa':
        existing={r[pk] for r in checks}
        for rel in sorted(changed-existing):
            checks.append({'relative_path':rel,'size_bytes':str((u/rel).stat().st_size),'sha256':final_digest(rel),'payload_type':'unit_record'})
        checks.sort(key=lambda r:r[pk]);(u/cfg['checks']).write_bytes(csv_bytes(ch,checks));changed.add(cfg['checks'])
    result=check_file_rows(unit)
    for rel,v in plan['payload_stats'].items():
        p=u/moves.get(rel,rel);st=p.stat()
        assert (st.st_size,st.st_mtime_ns,st.st_ino)==(v['size'],v['mtime_ns'],v['inode']),('Changed payload bytes/stat',rel)
    result.update({'unit':unit,'completed_utc':datetime.now(timezone.utc).isoformat(),'master_files_unchanged':cfg['expected'],
                   'registered_payloads_unchanged':len(plan['payload_stats']),'media_rehashed':0,'media_generated':0,
                   'renamed_paths':moves,'catalogue_row_kind':'archived_file','workbook_matches_csv':True,
                   'workbook_writer':'bundled openpyxl; Artifact Tool export blocked by WASM SIMD runtime'})
    receipt_paths=[u/'db-restore/accession-completion.json',u/'db-restore/document-accession-completion.json'] if unit=='tfa' else [u/'00_MANIFESTS/accession-completion.json']
    for p in receipt_paths:
        receipt=json.loads(p.read_text());receipt['photo_normalization']=result
        if unit=='tfa':
            receipt['object_catalogue_rows']=plan['object_rows'];receipt['catalogue_rows']=plan['catalog_rows'];receipt['catalogue_row_kind']='archived_file'
            for rel in changed:
                receipt.setdefault('control_file_sha256',{})[rel]='sha256:'+final_digest(rel)
        else:
            prefix='payload_' if unit=='baeckerhandwerk' else ''
            receipt[prefix+'checksum_rows']=len(checks)
            receipt[prefix+'checksum_manifest_sha256']=final_digest(cfg['checks'])
            receipt[prefix+'checksum_manifest_size_bytes']=(u/cfg['checks']).stat().st_size
        jwrite(p,receipt)
    jwrite(staged(unit,'result.json'),result)
    print(json.dumps(result,ensure_ascii=False),flush=True)

def verify(unit):
    u=ROOT/unit;plan=json.loads(staged(unit,'plan.json').read_text());cfg=CONFIG[unit]
    check_workbook(unit,u/'CATALOG.xlsx');result=check_file_rows(unit)
    assert (u/'CATALOG.csv').read_bytes()==staged(unit,'CATALOG.csv').read_bytes()
    for rel,v in plan['payload_stats'].items():
        st=(u/plan['moves'].get(rel,rel)).stat()
        assert (st.st_size,st.st_mtime_ns,st.st_ino)==(v['size'],v['mtime_ns'],v['inode'])
    _,checks=read_csv(u/cfg['checks']);pk='path' if unit=='tfa' else 'relative_path'
    assert len(checks)==len({r[pk] for r in checks})
    for r in checks:assert (u/r[pk]).stat().st_size==int(r['size_bytes']),r[pk]
    checkmap={r[pk]:r['sha256'].removeprefix('sha256:') for r in checks}
    for r in read_csv(u/'CATALOG.csv')[1]:assert checkmap[r['final_relative_path']]==r['final_sha256']
    if unit=='baeckerhandwerk':
        for name in ['drain-list.csv','document-source-disposition.csv']:
            assert read_csv(u/'00_MANIFESTS'/name)==read_csv(u/'db-restore'/name)
        assert Counter(r['presentation_rotation_cw'] for r in read_csv(u/cfg['objects'])[1])==Counter({'0':60,'90':16})
    if unit=='tfa':
        for name in ['subdivision-register.csv','subdivision-memberships.csv']:
            assert read_csv(u/'metadata'/name)==read_csv(u/'db-restore/manifests'/name)
        held=[r for r in read_csv(u/cfg['objects'])[1] if not r['master_relative_path']]
        assert len(held)==1 and held[0]['record_key']=='md:85642'
    if unit=='pressebildarchiv-wf':
        assert len(list((u/'export/access').glob('*.jpg')))==48
        assert len(list((u/'export').glob('*.tif')))==48
        assert len(list((u/'export').glob('*.jpg')))==13
        assert (u/'documents/CONTENTS.md').is_file()
        assert not (u/'catalog.csv').exists()
    result.update({'unit':unit,'xlsx_readback':'all cells match','payload_paths_sizes_inodes_mtimes':'unchanged except authorized paths','stored_hashes':'agree','validation':'passed'})
    jwrite(staged(unit,'validation.json'),result);print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','workbook','apply','verify']);p.add_argument('unit',choices=UNITS);a=p.parse_args()
    {'prepare':prepare,'workbook':workbook_fallback,'apply':apply,'verify':verify}[a.command](a.unit)
