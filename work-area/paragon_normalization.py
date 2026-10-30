"""User-approved Paragon catalogue normalization; media and source decisions stay intact."""
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import photo_normalization as common

U = Path('/mnt/volume/iss-archive/units/paragon')
W = Path('/mnt/volume/iss-archive-work')
DB = 'db-restore/RESTORE.sqlite'
REPS = 'db-restore/manifests/representations.csv'
DOCS = 'db-restore/manifests/documents.csv'
MD = 'db-restore/manifests/md-export.csv'
DISP = 'db-restore/manifests/source-disposition.csv'
CHECKS = 'db-restore/checksums.csv'
OBJECTS = 'metadata/objects.csv'
MOVES = {'README.md':'00_README.md', 'CATALOG-HUMAN.csv':OBJECTS,
         'CATALOG-README.md':'metadata/catalog-fields.md'}
UNRESOLVED = {'VLL-1013','VLV-1004-1','VLV-1018-3','VLV-1050-1','VLV-1053-1',
              'VLV-1092-2','VLV-1127','VLV-1130-5','VLV-1161'}

def stage(rel): return common.staged('paragon', rel)
def read(rel): return common.read_csv(U/rel)
def write(rel, data): stage(rel).write_bytes(data)
def jsonwrite(rel, data): common.jwrite(stage(rel), data)
def digest(value): return value.removeprefix('sha256:')
def conn(path):
    c=sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)
    c.row_factory=sqlite3.Row
    return c
def active(c):
    return [dict(r) for r in c.execute("""SELECT ef.entity_id, ef.role, f.*
      FROM entity_files ef JOIN file_occurrences f USING(occurrence_id)
      WHERE ef.status='active' ORDER BY f.occurrence_id""")]
def current_path(old):
    for prefix, target in [('units/WF-PARAGON/data/masters/','masters/'),
                           ('units/WF-PARAGON/data/derivatives/','export/access/')]:
        if old.startswith(prefix): return target+old[len(prefix):]
    raise AssertionError(old)
def part(note):
    try: value=json.loads(note).get('representation_part','')
    except ValueError:
        m=re.search(r'representation_part=([^;]+)', note)
        value=m[1] if m else ''
    return value.removesuffix('_access')
def note_json(note):
    try: return json.loads(note)
    except ValueError: return {}

def prepare():
    assert not (U/OBJECTS).exists(), 'Already normalized; use verify'
    oh, original_objects=read('CATALOG-HUMAN.csv')
    rh, restore=read('db-restore/RESTORE.csv')
    _, reps=read(REPS); _, docs=read(DOCS); _, md=read(MD)
    ch, checks=read(CHECKS); _, dispositions=read(DISP)
    checkmap={r['path']:r for r in checks}
    assert len(checkmap)==len(checks)==492
    assert len(original_objects)==240 and len(restore)==475
    assert len(reps)==233 and len(docs)==8 and len(md)==3
    assert len(dispositions)==1413 and {r['deletion_authorized'] for r in dispositions}=={'false'}
    c=conn(U/DB); ar=active(c)
    assert len(ar)==466
    bypath={current_path(r['relative_path']):r for r in ar}
    assert len(bypath)==466
    plan={'unit':'paragon','authorization':'ok lets normalize paragon',
          'prepared_utc':datetime.now(timezone.utc).isoformat(), 'moves':MOVES,
          'inputs':{},'payload_stats':{},'writes':[], 'active_path_updates':{},
          'source_work_db':str(W/'paragon/work/catalog.sqlite')}
    payload=set()
    for r in reps:
        for role in ['master','access']:
            p=r[role+'_path']; payload.add(p)
            a=bypath[p]
            assert a['status']=='verified' and a['occurrence_kind']=='archive'
            assert a['entity_id']==r['iss_id']
            assert a['role']==(r['source_role'] if role=='master' else 'access_derivative')
            assert digest(a['content_digest'])==digest(r[role+'_sha256'])==digest(checkmap[p]['sha256'])
            plan['active_path_updates'][a['occurrence_id']]={'old':a['relative_path'],'new':p}
    payload.update(r['relative_path'] for r in docs)
    payload.update(r['path'] for r in md)
    assert len(payload)==477
    for p in payload:
        st=(U/p).stat(); row=checkmap[p]
        assert st.st_size==int(row['size_bytes']), p
        plan['payload_stats'][p]={'size':st.st_size,'mtime_ns':st.st_mtime_ns,
                                 'inode':st.st_ino,'sha256':digest(row['sha256'])}
    for p in set(checkmap)-payload | {CHECKS}:
        plan['inputs'][p]=common.sha(U/p)
    plan['source_work_db_sha256']=common.sha(plan['source_work_db'])
    plan['original_objects']=original_objects
    # Snapshot only the existing database, with no new schema or service.
    assert not stage(DB).exists()
    out=sqlite3.connect(stage(DB)); c.backup(out)
    out.execute('pragma foreign_keys=on')
    for occurrence_id, mapping in plan['active_path_updates'].items():
        cur=out.execute('update file_occurrences set relative_path=? where occurrence_id=? and relative_path=?',
                        (mapping['new'],occurrence_id,mapping['old']))
        assert cur.rowcount==1
    # Existing triggers count path edits. This is a synchronized restore projection;
    # no generation, event, approval state or Master decision is created.
    out.execute('update catalog_state set published_revision=catalog_revision, dirty_since=NULL where singleton=1')
    out.commit()
    revision=out.execute('select catalog_revision from catalog_state').fetchone()[0]
    assert revision==9976+466
    assert out.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert out.execute('pragma foreign_key_check').fetchall()==[]
    sql='\n'.join(out.iterdump())+'\n'
    write('db-restore/RESTORE.sql', sql.encode())
    out.close(); c.close()
    plan['catalog_revision']=revision
    plan['writes'] += [DB,'db-restore/RESTORE.sql']
    for r in restore:
        r['catalog_revision']=str(revision)
        if r['relative_path']:
            r['relative_path']=current_path(r['relative_path'])
            a=bypath[r['relative_path']]
            assert (r['iss_id'],r['representation_role'],r['sha256']) == (a['entity_id'],a['role'],digest(a['content_digest']))
    write('db-restore/RESTORE.csv',common.csv_bytes(rh,restore))
    plan['writes'].append('db-restore/RESTORE.csv')
    grouped=defaultdict(list)
    for r in reps: grouped[r['iss_id']].append(r)
    order={'cover':0,'front':0,'open_interior':1,'verso':1,'':0}
    objects=[]
    for old in original_objects:
        o=dict(old); members=sorted(grouped[o['iss_id']],key=lambda r:order[part(bypath[r['master_path']]['note'])])
        assert bool(old['master_file'])==bool(members)
        if members:
            assert current_path(old['master_file']) in {r['master_path'] for r in members}
            assert current_path(old['access_file']) in {r['access_path'] for r in members}
        o['master_file']='\n'.join(r['master_path'] for r in members)
        o['access_file']='\n'.join(r['access_path'] for r in members)
        o['master_member_count']=str(len(members))
        o['master_parts']='\n'.join(part(bypath[r['master_path']]['note']) for r in members)
        objects.append(o)
    assert {o['archival_label'] for o in objects if not o['master_file']}==UNRESOLVED
    assert Counter(o['master_member_count'] for o in objects)=={'1':229,'2':2,'0':9}
    assert {o['archival_label']:o['master_parts'] for o in objects if o['master_member_count']=='2'}=={
        'VLV-1095':'cover\nopen_interior','VLV-1097':'front\nverso'}
    oh=oh+['master_member_count','master_parts']
    write(OBJECTS,common.csv_bytes(oh,objects)); plan['writes'].append(OBJECTS)
    ob={o['iss_id']:o for o in objects}
    restore_by_path={r['relative_path']:r for r in restore if r['relative_path']}
    source_by_final=defaultdict(list)
    for r in dispositions:
        if r['preservation_status'] in {'preserved_as_metadata_enriched_master','preserved_exact_copy','preserved_exact_duplicate'}:
            for p in r['final_path'].split(' | '): source_by_final[p].append(r)
    rows=[]
    for rep in reps:
        o=ob[rep['iss_id']]
        for kind in ['master','access']:
            p=rep[kind+'_path']; a=bypath[p]; rr=restore_by_path[p]; j=note_json(a['note'])
            component=part(a['note']); sources=source_by_final[p]
            source_paths=sorted({s['source_path'] for s in sources})
            source_hashes={digest(s['source_sha256']) for s in sources if s['source_sha256']}
            exact_source=j.get('derivation','') in {'exact_selected_jpeg_source','exact_low_resolution_jpeg_source_copy'}
            derivation=''
            if kind=='access' and j.get('derivation') in {'generated_from_lossless_master','local_master_to_local_derivative_to_museum_digital_publication'}:
                assert digest(j['master_digest'])==digest(rep['master_sha256'])
                derivation=rep['master_path']
            elif kind=='master':
                sm=re.search(r'(?:JPEG )?source sha256:([a-f0-9]{64})',a['note'])
                if sm and sm[1]==digest(rep['access_sha256']): derivation=rep['access_path']
            # Existing path notes are provenance, never normalized by text replacement.
            recorded_source=j.get('source_path','') or '\n'.join(source_paths)
            if kind=='access' and not recorded_source: recorded_source=rep['access_source']
            source_hash=next(iter(source_hashes)) if len(source_hashes)==1 else ''
            if exact_source: source_hash=digest(rep['access_sha256'])
            mid=re.search(r'museum-digital:(\d+)',o['identifiers'])
            relation=('member of the single selected Master for '+o['archival_label'] if kind=='master' else 'access file paired with the selected Master member')
            if component: relation+='; part='+component
            condition=('retained source JPEG; not generated from the TIFF' if exact_source else '')
            if kind=='master' and a['role']=='preservation_surrogate':
                condition='manufactured preservation surrogate; not an original high-resolution source'
            values={'record_key':o['iss_id'],'inventory':o['archival_label'],
                    'title':o['descriptive_title'],'file_role':a['role'],
                    'final_relative_path':p,'master_relative_path':rep['master_path'],
                    'filename':Path(p).name,'file_type':Path(p).suffix[1:],
                    'size_bytes':checkmap[p]['size_bytes'],'final_sha256':digest(rep[kind+'_sha256']),
                    'description':o['description'],'date_text':o['date_year'],
                    'object_type':o['object_type'],'material':o['material_technique'],'dimensions':o['dimensions'],
                    'md_object_id':mid[1] if mid else '',
                    'md_url':'https://berlin.museum-digital.de/object/'+mid[1] if mid else '',
                    'rights':rr['rights_statement'],'rights_holder':rr['rights_holder'],
                    'original_source_path':recorded_source,'source_sha256':source_hash,
                    'derivative_of':derivation,'width':rr['width_px'],'height':rr['height_px'],
                    'relationship':relation,'access_restriction':'publication '+rr['publication_status'],
                    'condition':condition,
                    'notes':a['note']+'\nAccession export: '+rep['derivation_status']+'\nRights status: '+o['rights_status'],
                    'metadata_path':OBJECTS,'provenance_path':REPS+'\n'+DISP+'\ndb-restore/RESTORE.csv'}
            rows.append({f:values.get(f,'') for f in common.FIELDS})
    for d in docs:
        p=d['relative_path']
        assert digest(d['final_sha256'])==digest(checkmap[p]['sha256'])
        values={'record_key':d['document_id'],'title':d['description'],'file_role':'documentation_original',
                'final_relative_path':p,'filename':Path(p).name,'file_type':Path(p).suffix[1:],
                'size_bytes':d['size_bytes'],'final_sha256':digest(d['final_sha256']),
                'description':d['description'],'date_text':d['date_note'],
                'original_source_path':d['source_path'],'relationship':d['relation_to_paragon'],
                'condition':d['condition'],'notes':d['copy_verification'],
                'metadata_path':DOCS,'provenance_path':DOCS}
        rows.append({f:values.get(f,'') for f in common.FIELDS})
    for d in md:
        p=d['path']; assert digest(d['sha256'])==digest(checkmap[p]['sha256'])
        values={'record_key':'WF-PARAGON','title':Path(p).name,'file_role':d['role'],
                'final_relative_path':p,'filename':Path(p).name,'file_type':Path(p).suffix[1:],
                'size_bytes':d['size_bytes'],'final_sha256':digest(d['sha256']),
                'relationship':'unit-level museum-digital export package',
                'metadata_path':MD,'provenance_path':MD,
                'notes':'Retained existing export; no submission or publication performed.'}
        rows.append({f:values.get(f,'') for f in common.FIELDS})
    rows.sort(key=lambda r:(r['file_role'] in {'documentation_original','museum_digital_metadata'},
                            r['inventory'],r['record_key'],r['master_relative_path'],
                            r['file_role']=='access_derivative',r['final_relative_path']))
    assert len(rows)==477 and {r['final_relative_path'] for r in rows}==payload
    write('CATALOG.csv',common.csv_bytes(common.FIELDS,rows)); plan['writes'].append('CATALOG.csv')
    jsonwrite('workbook.json',{'unit':'paragon','catalog_fields':common.FIELDS,'catalog_rows':rows,
                             'object_fields':oh,'object_rows':objects})
    jsonwrite('restore-workbook.json',{'fields':rh,'rows':restore})
    schema=common.schema_text('tfa')
    schema=schema[:schema.index('Exactly one preservation_master row')]+'''Each resolved Paragon object has one selected Master representation. It has one
file member for 229 objects and two members for VLV-1095 and VLV-1097. The existing
roles preservation_master (121 TIFFs) and preservation_surrogate (112 TIFFs) are
retained. A surrogate is not an original high-resolution source. All 233 JPEGs
retain the legacy access_derivative role; condition, notes and derivative_of
distinguish generated JPEGs from exact retained source copies. A blank
derivative_of means that no derivation is asserted here.

In metadata/objects.csv, master_file and access_file contain complete unit-relative
member paths separated by newlines. master_member_count is 0, 1 or 2, not the
number of selected alternative Masters. master_parts supplies the corresponding
known labels for the two paired objects. Blank part labels mean unrecorded.
representation_summary retains the original file-role counts. No new object or
representation identifier is assigned. The retired VLV-1095-1 identifier and the
ISS-2026-H5TK-G filename remain aliases of ISS-2026-YM10-B (VLV-1095).

CATALOG.csv includes 466 photo files, eight original documents and three existing
museum-digital export files. Generated control files are indexed by checksums.csv.
The Objects sheet includes all 240 native descriptive records, including the nine
unresolved records. Original descriptive fields, publication blocks and rights
status are preserved. New member counts and labels only expose existing decisions.

Recovery starts with metadata/objects.csv, the representation/document/source
CSVs in db-restore/manifests, and db-restore/RESTORE.csv and RESTORE.sql. The SQL
dump also retains all historical decisions, aliases and rejected/superseded
occurrences. RESTORE.sqlite is the rebuildable index. Its 466 active archive file
paths are relative to this unit. Historical occurrence paths and provenance notes
remain verbatim. Existing dormant generation records are historical evidence.
The separate legacy work catalogue still addresses its existing work-area files;
it was not edited or treated as the authority for this normalized final projection.

The workbook is a generated readable view, not a second editing authority.
Source dispositions retain deletion_authorized=false.
'''
    write('metadata/catalog-fields.md',schema.encode());plan['writes'].append('metadata/catalog-fields.md')
    text=(U/'README.md').read_text()
    text=text.replace('- `CATALOG.xlsx`: human-readable object catalog.',
        '- `CATALOG.csv` and `CATALOG.xlsx` (Catalog sheet): one row per retained photo, document or MD export file.\n'
        '- `metadata/objects.csv` and the Objects sheet: all 240 native object records.\n'
        '- `metadata/catalog-fields.md`: field definitions, paired views, provenance and recovery guidance.')
    text=text.replace('Catalog revision: 9976',f'Catalog revision: {revision} (unit-relative restore projection; descriptive decisions unchanged)')
    text+='''
## Photo catalogue convention

Normalized on 2026-09-11 using the first photo batch convention. CATALOG.csv and
the Catalog sheet contain the same 477 retained file rows: 121 preservation-master
TIFFs, 112 preservation-surrogate TIFFs, 233 existing JPEG use copies, eight original
supporting documents and three existing museum-digital export files.

The 231 represented objects comprise 229 single-member Masters and two Masters
with two complementary members. VLV-1095 contains cover and open-interior views;
VLV-1097 contains front and verso. Both members are listed in each object's
master_file/access_file cells and identified by master_parts. The nine unresolved
objects retain empty file assignments: VLL-1013, VLV-1004-1, VLV-1018-3, VLV-1050-1,
VLV-1053-1, VLV-1092-2, VLV-1127, VLV-1130-5 and VLV-1161.

Current file links are unit-relative. Historical source/work paths in provenance
records remain unchanged. The files and readable records are the recovery starting
point; db-restore/RESTORE.sqlite is a rebuildable index, with a matching SQL dump,
CSV and workbook. Existing db-restore/manifests remains the manifest location.

No media or source-document bytes, selected members, aliases, metadata decisions,
publication restrictions or source dispositions changed. No derivatives were
generated during normalization. The collection descriptions above remain verbatim.
'''
    write('00_README.md',text.encode());plan['writes'].append('00_README.md')
    text=(U/'documents/CONTENTS.md').read_text().replace(
        '`../CATALOG-HUMAN.csv` and `../CATALOG.xlsx`: current readable archive catalogs.',
        '`../CATALOG.csv` and `../CATALOG.xlsx`: current file catalogue and workbook.\n'
        '- `../metadata/objects.csv`: all native object records, also in the Objects sheet.').replace(
        '`../db-restore/`: authoritative machine-restorable catalog and manifests.',
        '`../db-restore/`: readable restore records, manifests and the rebuildable SQLite index.')
    write('documents/CONTENTS.md',text.encode());plan['writes'].append('documents/CONTENTS.md')
    plan['catalog_rows']=len(rows);plan['object_rows']=len(objects)
    plan['roles']=dict(Counter(r['file_role'] for r in rows))
    jsonwrite('plan.json',plan)
    print(json.dumps({k:plan[k] for k in ['catalog_rows','object_rows','roles','catalog_revision','writes']}))

def workbook():
    # Artifact Tool XLSX export already failed on this runtime's missing WASM SIMD.
    common.workbook_fallback('paragon')
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl import load_workbook
    # Restore remains a lossless single-sheet tabular export, as before.
    data=json.loads(stage('restore-workbook.json').read_text())
    wb=Workbook();s=wb.active;s.title='Catalog';s.append(data['fields'])
    for r in data['rows']:s.append([r[f] for f in data['fields']])
    s.freeze_panes='F2';s.sheet_view.showGridLines=False
    for row in s:
        for cell in row:
            cell.font=Font(name='Arial',size=10)
            if isinstance(cell.value,str):cell.data_type='s'
    for cell in s[1]:
        cell.font=Font(name='Arial',size=10,bold=True,color='FFFFFF')
        cell.fill=PatternFill('solid',fgColor='24445B');cell.alignment=Alignment(wrap_text=True)
    s.row_dimensions[1].height=34
    for i,f in enumerate(data['fields'],1):s.column_dimensions[get_column_letter(i)].width=45 if f in {'relative_path','description','provenance_note','descriptive_title'} else 23
    table=Table(displayName='RestoreCatalog',ref=s.dimensions)
    table.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
    s.add_table(table);wb.save(stage('db-restore/RESTORE.xlsx'))
    # Native object columns use legacy names; make long titles and paired paths readable.
    wb=load_workbook(stage('CATALOG.xlsx'));s=wb['Objects']
    fields=[c.value for c in s[1]]
    for i,f in enumerate(fields,1):
        if f in {'descriptive_title','identifiers','notes_for_review','master_file','access_file','master_parts'}:
            s.column_dimensions[get_column_letter(i)].width=70 if f.endswith('_file') else 52
            for row in s.iter_rows(min_row=2,min_col=i,max_col=i):row[0].alignment=Alignment(wrap_text=True,vertical='top')
        if f=='master_member_count':s.column_dimensions[get_column_letter(i)].width=23
    for row in s.iter_rows(min_row=2):
        n=max(sum(max(1,len(textwrap.wrap(line,width=max(10,int(s.column_dimensions[c.column_letter].width*.92)))))
                  for line in str(c.value or '').split('\n'))
              for c in row if c.alignment.wrap_text)
        s.row_dimensions[row[0].row].height=max(s.row_dimensions[row[0].row].height or 23,n*14+7)
    wb.save(stage('CATALOG.xlsx'))
    plan=json.loads(stage('plan.json').read_text())
    plan['writes']+=['CATALOG.xlsx','db-restore/RESTORE.xlsx']
    jsonwrite('plan.json',plan)
    verify_workbooks(stage)
    print('Both workbook exports match their CSV data.')

def finalize():
    plan=json.loads(stage('plan.json').read_text())
    plan['writes']=[p for p in plan['writes'] if p not in {CHECKS,'catalog-export-summary.json','db-restore/export-summary.json'}]
    plan['writes']=sorted(set(plan['writes']))
    old=json.loads((U/'catalog-export-summary.json').read_text())
    old['normalized_at']=datetime.now(timezone.utc).isoformat()
    old['format']='iss-paragon-photo-catalog-v1'
    old['rows']=240;old['file_rows']=477;old['object_rows']=240
    old['source_catalog_csv']=str(U/'db-restore/RESTORE.csv')
    old['normalization']={'authorization':plan['authorization'],'active_file_paths_corrected':466,
        'single_member_objects':229,'two_member_objects':2,'unresolved_objects':9,
        'media_changed':False,'source_disposition_changed':False,'deletion_authorized':False}
    old['outputs']={}
    for key,p in [('catalog_csv','CATALOG.csv'),('object_csv',OBJECTS),('human_xlsx','CATALOG.xlsx'),
                  ('restore_csv','db-restore/RESTORE.csv'),('restore_xlsx','db-restore/RESTORE.xlsx')]:
        old['outputs'][key]={'path':str(U/p),'sha256':'sha256:'+common.sha(stage(p))}
    jsonwrite('catalog-export-summary.json',old);plan['writes'].append('catalog-export-summary.json')
    summary=json.loads((U/'db-restore/export-summary.json').read_text())
    summary['normalized_at']=old['normalized_at'];summary['catalog_state']={
        'catalog_revision':plan['catalog_revision'],'published_revision':plan['catalog_revision'],'dirty_since':None}
    summary['documents']=8
    summary['path_base']=str(U)
    summary['normalization']='Current restore paths relative to the final unit; historical provenance and legacy work catalogue unchanged.'
    plan['writes'].append('db-restore/export-summary.json')
    fields,checks=read(CHECKS)
    current={MOVES.get(r['path'],r['path']):dict(r,path=MOVES.get(r['path'],r['path'])) for r in checks}
    for p in plan['writes']:current.setdefault(p,{'path':p})
    summary['checksums']=len(current)
    jsonwrite('db-restore/export-summary.json',summary)
    for p in plan['writes']:
        current[p]={'path':p,'sha256':'sha256:'+common.sha(stage(p)),'size_bytes':str(stage(p).stat().st_size)}
    write(CHECKS,common.csv_bytes(fields,[current[p] for p in sorted(current)]))
    plan['writes'].append(CHECKS)
    plan['writes']=sorted(set(plan['writes']))
    plan['output_hashes']={p:common.sha(stage(p)) for p in plan['writes']}
    jsonwrite('plan.json',plan)
    verify(stage, before_apply=True)

def verify_workbooks(resolver):
    common.check_workbook('paragon',resolver('CATALOG.xlsx'))
    from openpyxl import load_workbook
    h,rs=common.read_csv(resolver('db-restore/RESTORE.csv'))
    w=load_workbook(resolver('db-restore/RESTORE.xlsx'),read_only=True)
    assert w.sheetnames==['Catalog']
    it=w['Catalog'].iter_rows(values_only=True);assert list(next(it))==h
    assert [[str(v) if v is not None else '' for v in row] for row in it]==[[r[f] for f in h] for r in rs]
    w.close()

def compare_database(before,after,mapping,revision):
    assert before.execute("select type,name,tbl_name,sql from sqlite_master order by type,name").fetchall()==after.execute("select type,name,tbl_name,sql from sqlite_master order by type,name").fetchall()
    tables=[r[0] for r in before.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for table in tables:
        a=[dict(r) for r in before.execute('select * from "'+table+'"')]
        b=[dict(r) for r in after.execute('select * from "'+table+'"')]
        if table=='file_occurrences':
            for r in a:
                if r['occurrence_id'] in mapping:r['relative_path']=mapping[r['occurrence_id']]['new']
        if table=='catalog_state':
            a[0].update(catalog_revision=revision,published_revision=revision,dirty_since=None)
        assert sorted(json.dumps(r,sort_keys=True) for r in a)==sorted(json.dumps(r,sort_keys=True) for r in b),table

def verify(resolver, before_apply=False):
    plan=json.loads(stage('plan.json').read_text())
    # Unchanged accession payloads reuse stored hashes; inode/mtime/size prove that
    # this normalization did not replace or rewrite them. No media rehashing.
    for p,saved in plan['payload_stats'].items():
        st=(U/p).stat()
        assert (st.st_size,st.st_mtime_ns,st.st_ino)==(saved['size'],saved['mtime_ns'],saved['inode']),p
    assert common.sha(plan['source_work_db'])==plan['source_work_db_sha256']
    for p in [REPS,DOCS,MD,DISP]:assert common.sha(U/p)==plan['inputs'][p]
    h,objects=common.read_csv(resolver(OBJECTS));_,files=common.read_csv(resolver('CATALOG.csv'))
    oldby={o['iss_id']:o for o in plan['original_objects']}
    assert len(objects)==240 and len(files)==477
    for o in objects:
        old=oldby[o['iss_id']]
        assert all(o[f]==v for f,v in old.items() if f not in {'master_file','access_file'}),o['iss_id']
        assert int(o['master_member_count'])==len(o['master_file'].splitlines())
    assert {o['archival_label'] for o in objects if not o['master_file']}==UNRESOLVED
    assert Counter(o['master_member_count'] for o in objects)=={'1':229,'2':2,'0':9}
    assert Counter(r['file_role'] for r in files)==plan['roles']
    filemap={r['final_relative_path']:r for r in files}; assert set(filemap)==set(plan['payload_stats'])
    for p,r in filemap.items():
        assert r['final_sha256']==plan['payload_stats'][p]['sha256']
        assert int(r['size_bytes'])==plan['payload_stats'][p]['size']
        if r['master_relative_path']:assert r['master_relative_path'] in filemap
        if r['derivative_of']:assert r['derivative_of'] in filemap
    original=conn(U/DB) if before_apply else conn(plan['source_work_db'])
    final=conn(resolver(DB))
    compare_database(original,final,plan['active_path_updates'],plan['catalog_revision'])
    assert final.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert final.execute('pragma foreign_key_check').fetchall()==[]
    dbfiles=active(final)
    assert len(dbfiles)==466
    for r in dbfiles:
        f=filemap[r['relative_path']]
        assert (f['record_key'],f['file_role'],f['final_sha256'])==(r['entity_id'],r['role'],digest(r['content_digest']))
    for o in objects:
        selected={r['relative_path'] for r in dbfiles if r['entity_id']==o['iss_id'] and r['role'] in {'preservation_master','preservation_surrogate'}}
        assert selected==set(o['master_file'].splitlines())
    restored=sqlite3.connect(':memory:');restored.row_factory=sqlite3.Row
    # Read without universal-newline translation: literal CRLF in descriptions
    # is original metadata and must survive the SQL restoration byte-for-byte.
    restored.executescript(resolver('db-restore/RESTORE.sql').read_bytes().decode('utf-8'))
    compare_database(final,restored,{},plan['catalog_revision'])
    original.close();final.close();restored.close()
    verify_workbooks(resolver)
    _,checks=common.read_csv(resolver(CHECKS));assert len(checks)==493
    for r in checks:
        p=r['path']; target=resolver(p) if p in plan['writes'] else U/p
        assert target.is_file() and target.stat().st_size==int(r['size_bytes']),p
        if p in plan['payload_stats']:
            assert digest(r['sha256'])==plan['payload_stats'][p]['sha256']
        else:assert common.sha(target)==digest(r['sha256']),p
    for p,sha in plan['output_hashes'].items():assert common.sha(resolver(p))==sha,p
    readme=resolver('00_README.md').read_text()
    oldreadme=(U/'README.md').read_text() if before_apply else (W/'photo-paragon--original-readme.txt').read_text()
    assert readme.split('<!-- md-collection-description:end -->')[0]==oldreadme.split('<!-- md-collection-description:end -->')[0]
    result={'unit':'paragon','status':'staged_validated' if before_apply else 'complete',
            'file_rows':477,'object_rows':240,'single_member_objects':229,'paired_objects':['VLV-1095','VLV-1097'],
            'unresolved_objects':sorted(UNRESOLVED),'roles':plan['roles'],
            'active_current_paths_corrected':466,'sql_restore_verified':True,
            'workbooks_match_csv':True,'unchanged_payload_files':477,
            'unchanged_source_disposition_rows':1413,'media_rehashed':False,
            'source_mutation':False,'deletion_authorized':False}
    jsonwrite('validation.json',result);print(json.dumps(result))

def apply():
    plan=json.loads(stage('plan.json').read_text())
    for p,sha in plan['inputs'].items():assert common.sha(U/p)==sha,p
    for p,sha in plan['output_hashes'].items():assert common.sha(stage(p))==sha,p
    for p in plan['writes']:
        if p not in plan['inputs'] and p not in MOVES.values():assert not (U/p).exists(),p
    for p in [DB,plan['source_work_db']]:
        path=Path(p) if Path(p).is_absolute() else U/p
        wal=Path(str(path)+'-wal');assert not wal.exists() or wal.stat().st_size==0
    (W/'photo-paragon--original-readme.txt').write_bytes((U/'README.md').read_bytes())
    (U/'metadata').mkdir(exist_ok=True)
    for old,new in MOVES.items():
        assert not (U/new).exists();(U/old).rename(U/new)
    for p in plan['writes']:shutil.copyfile(stage(p),U/p)
    verify(lambda p:U/p)
    result=json.loads(stage('validation.json').read_text())
    result['completed_utc']=datetime.now(timezone.utc).isoformat()
    jsonwrite('result.json',result)

if __name__=='__main__':
    {'prepare':prepare,'workbook':workbook,'finalize':finalize,
     'apply':apply,'verify':lambda:verify(lambda p:U/p)}[sys.argv[1]]()
