"""Normalize the seven existing PDF/ordered-page units authorized on 2026-09-12.

Only existing unit controls, current paths and catalogue views change. No source
access, Master decision, media transformation, publication or schema change.
Preparation and results use the existing work root, like photo_normalization.py.
"""
import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import photo_normalization as common

ROOT = Path('/mnt/volume/iss-archive/units')
WORK = Path('/mnt/volume/iss-archive-work')
UNITS = {'aeb': 'aeb', 'bibliothek': 'bibliothek', 'archiv-wf': 'archiv-wf',
         'brigadebuecher': 'brigadebuecher',
         'feb': 'FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF',
         'trafo': 'trafo', 'wf-sender': 'wf-sender'}
EXPECTED = {'aeb':68, 'bibliothek':3, 'archiv-wf':54, 'brigadebuecher':73,
            'feb':79, 'trafo':1592, 'wf-sender':1863}
FIELDS = common.FIELDS + ['master_representation_id', 'master_representation_type',
                         'master_member_count', 'pages', 'source_url', 'content_note',
                         'content_match_status', 'native_file_role']
OBJECT_FIELDS = ['record_key','inventory','title','master_representation_id',
                 'master_representation_type','master_member_count','ordered_master_paths',
                 'access_pdf_paths','preview_paths','page_image_paths','access_note']

def out(unit, name): return WORK / ('pdf-' + unit + '--' + name.replace('/', '__'))
def dump(path, value): path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
def readcsv(path): return common.read_csv(path)
def bcsv(fields, rows): return common.csv_bytes(fields, rows)
def digest(path): return common.sha(path)
def strval(v): return '' if v is None else str(v)
def textrows(rows): return [{k:strval(v) for k,v in r.items()} for r in rows]
def j(v): return json.dumps(v, ensure_ascii=False)
def q(v): return '"'+v.replace('"','""')+'"'

def load_db(root):
    db = next((root/'db-restore').glob('*.sqlite'))
    c = sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    tables = {r['name']:[dict(x) for x in c.execute('SELECT * FROM '+q(r['name']))]
              for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    schema = list(map(tuple,c.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")))
    c.close()
    return db,tables,schema

def key(row):
    return strval(row.get('object_key') or row.get('archival_object_id') or
                  row.get('carrier_key') or row.get('md_object_id') or row.get('inventory'))

def unpack(unit, root, tables):
    """Read the existing native indexes; no format-based Master decisions."""
    object_table = {'brigadebuecher':'catalog','trafo':'archive_object'}.get(unit,'objects')
    objects = tables[object_table]
    if unit in ('aeb','bibliothek','feb'):
        files = [dict(r, _key=key(r), _role=r['file_role'], _provenance='db-restore/files.csv') for r in tables['files']]
    elif unit == 'archiv-wf':
        files = [dict(r, _key=key(r), _role=r['role'], _provenance='00_MANIFESTS/representations.csv') for r in tables['representations']]
        files += [dict(r,_key=key(r),_role='documentation_original',_provenance='00_MANIFESTS/documents.csv') for r in tables['documents']]
    elif unit == 'brigadebuecher':
        reps = {r['representation_id']:r for r in tables['representations']}
        files = [dict(reps[r['representation_id']], **r, _key=key(reps[r['representation_id']]),
                      _role=reps[r['representation_id']]['role'], _provenance='00_MANIFESTS/representation-members.csv')
                 for r in tables['representation_members']]
        files += [dict(r,_key='',_role='documentation_original',_provenance='00_MANIFESTS/document-provenance.csv') for r in tables['documents']]
    elif unit == 'trafo':
        files = [dict(r,_key=key(r),_role=r['role'],_provenance='db-restore/RESTORE.csv') for r in tables['representation']]
        files += [dict(r,_key='',_role='documentation_original',_provenance='db-restore/manifests/documents.csv')
                  for r in readcsv(root/'db-restore/manifests/documents.csv')[1]]
    else:
        files = [dict(r,_key=key(r),_role=r['role'],_provenance='db-restore/RESTORE.csv') for r in tables['representations']]
    return objects,files

def pathof(r): return r.get('relative_path',r.get('final_relative_path',''))
def ismaster(r):
    return r['_role'] in ('master','master_member','selected_master_member','preservation_master') or r['_role'].startswith('preservation_master_')
def isaccess(r):
    return r['_role'] in ('generated_access_derivative','access_derivative','access_preview','access_pdf',
                          'generated_access_pdf','retained_access_pdf','retained_access_pdf_from_fallback_master','full_issue_access_pdf')

def remapper(root, moves):
    # Exact current path tokens and absolute final-unit paths only. Work/source
    # path prefixes and original filenames remain historical evidence.
    literals = dict(moves)
    literals.update({str(root/a):str(root/b) for a,b in moves.items()})
    if not literals: return lambda s:s
    pattern = re.compile(r'(?<![\w/])(?:'+ '|'.join(re.escape(k) for k in sorted(literals,key=len,reverse=True)) + r')(?![\w./-])')
    return lambda s: pattern.sub(lambda m:literals[m.group()],s) if isinstance(s,str) else s

def remap_tables(tables, remap):
    return {t:[{k:remap(v) for k,v in r.items()} for r in rows] for t,rows in tables.items()}

def table_digest(tables):
    return hashlib.sha256(json.dumps(tables,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def member_hash(rows):
    projection=[{'sequence':int(r['sequence']),'final_relative_path':r['final_relative_path'],
                 'final_sha256':r['final_sha256'],'size_bytes':int(r['size_bytes'])} for r in rows]
    return hashlib.sha256(json.dumps(projection,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def catalog_views(unit, root, tables):
    objects,files = unpack(unit,root,tables)
    bykey = {key(o):o for o in objects}
    masters = defaultdict(list)
    for f in files:
        if ismaster(f): masters[f['_key']].append(f)
    assert set(masters)==set(bykey), ('Master coverage',unit,set(bykey)-set(masters),set(masters)-set(bykey))
    for k,rs in masters.items():
        rs.sort(key=lambda r:int(r.get('sequence') or r.get('member_position') or 1))
        positions = [int(r.get('sequence') or r.get('member_position') or 1) for r in rs]
        assert len(positions)==len(set(positions)), ('repeated Master sequence',unit,k)
        o = bykey[k]
        ordered = o.get('ordered_master_paths')
        if ordered: assert json.loads(ordered)==[pathof(r) for r in rs], (unit,k,'ordered membership differs')
    assert len(bykey)==EXPECTED[unit]
    result=[]
    for f in files:
        k=f['_key'];o=bykey.get(k,{})
        p=pathof(f);role=f['_role'];master=ismaster(f)
        meta={}
        if f.get('metadata_json'):
            try: meta=json.loads(f['metadata_json'])
            except ValueError: pass
        group=masters.get(k,[])
        paths=[pathof(x) for x in group]
        repid=o.get('master_representation_id',o.get('selected_candidate_id',''))
        group_ids={r.get('representation_id','') for r in group}
        if not repid and len(group_ids)==1:repid=next(iter(group_ids))
        reptype=o.get('representation_type',o.get('master_representation_type',o.get('representation_policy','')))
        if not reptype and group: reptype='single_file' if len(group)==1 else 'ordered_file_set'
        masterpath=(paths[0] if len(paths)==1 else str(Path(paths[0]).parent)) if paths else ''
        if group and len(group)>1 and Path(masterpath).name=='pages':masterpath=str(Path(masterpath).parent)
        rawmaster=f.get('master_relative_path','')
        source_final=f.get('original_source_path',f.get('source_path',''))
        if not rawmaster and source_final.startswith(str(root)+'/'):
            candidate=source_final[len(str(root))+1:]
            if candidate in paths:rawmaster=candidate
        if master:masterpath=p
        elif rawmaster:masterpath=rawmaster
        canonical_role='preservation_master' if master else ('access_preview' if role=='access_preview' else 'access_derivative' if isaccess(f) else role)
        # Retained PDFs are reading copies; do not invent a scan-to-PDF derivation.
        derivation=f.get('derivative_of','')
        if role in ('access_preview','generated_access_derivative'):derivation=rawmaster
        values={
          'record_key':k if o else f.get('document_id',''), 'inventory':o.get('inventory') or f.get('inventory') or (k if o else ''),
          'title':f.get('title') or o.get('title') or f.get('description') or Path(p).name,
          'file_role':canonical_role,'native_file_role':role,'final_relative_path':p,'master_relative_path':masterpath,
          'filename':Path(p).name,'file_type':Path(p).suffix.lstrip('.').lower(),'size_bytes':f['size_bytes'],
          'final_sha256':strval(f.get('final_sha256') or f.get('sha256') or f.get('content_sha256')).removeprefix('sha256:'),
          'description':f.get('description') or o.get('description') or '',
          'date_text':f.get('date_text') or f.get('date') or o.get('date_text') or o.get('date_created') or '',
          'creator':f.get('creator') or o.get('creator') or '',
          'object_type':o.get('object_type',f.get('object_type','')),'material':o.get('material_technique',''),
          'dimensions':o.get('dimensions',''),'places':o.get('places',''),'tags':o.get('tags',''),
          'md_object_id':o.get('md_object_id',o.get('museum_digital_object_id',f.get('md_object_id',''))),
          'md_url':o.get('object_url',o.get('md_url',f.get('object_url',''))),
          'rights':f.get('rights') or o.get('rights_usage_terms') or meta.get('rights',''),
          'rights_holder':f.get('owner') or meta.get('owner',''),
          'metadata_authority':o.get('metadata_authority',o.get('metadata_source','')),
          'original_source_path':f.get('original_source_path',f.get('source_path','')),
          'source_sha256':strval(f.get('source_sha256','')).removeprefix('sha256:'),'derivative_of':derivation,
          'sequence':f.get('sequence',f.get('member_position','')),'width':f.get('width',''),'height':f.get('height',''),
          'presentation_rotation_cw':'','relationship':f.get('relationship',f.get('relation_to_trafo','')),
          'access_restriction':f.get('access_restriction',''),'condition':f.get('condition',''),
          'notes':f.get('basis',f.get('copy_policy','')),'metadata_path':f.get('metadata_path','db-restore/objects.csv'),
          'provenance_path':f['_provenance'],'master_representation_id':repid,'master_representation_type':reptype,
          'master_member_count':len(group) if group else '', 'pages':f.get('pages',''),'source_url':f.get('source_url',''),
          'content_note':f.get('content_note',o.get('content_note','')),
          'content_match_status':f.get('content_match_status',o.get('content_match_status',''))}
        result.append({field:strval(values.get(field,'')) for field in FIELDS})
    assert len({r['final_relative_path'] for r in result})==len(result)
    native_fields=list(objects[0])
    object_fields=OBJECT_FIELDS+['native_'+f for f in native_fields]
    object_rows=[]
    for k,o in bykey.items():
        rs=[r for r in result if r['record_key']==k]
        ms=[r for r in rs if r['file_role']=='preservation_master']
        ms.sort(key=lambda r:int(r['sequence'] or 1))
        pdfs=[r['final_relative_path'] for r in rs if r['file_type']=='pdf' and r['file_role']=='access_derivative']
        previews=[r['final_relative_path'] for r in rs if r['file_role']=='access_preview' or (r['file_role']=='access_derivative' and r['file_type'] in ('jpg','jpeg','png'))]
        rawpdfs=[r['final_relative_path'] for r in ms if r['file_type']=='pdf']
        note=[]
        if not pdfs:note.append('No separate access PDF. '+('Selected PDF member(s) exist privately; export selection remains explicit.' if rawpdfs else 'No PDF reading copy is registered.'))
        if not previews:note.append('No JPEG preview is registered.')
        if len(rawpdfs)>1:note.append('Multiple ordered PDF members; not silently merged into one book.')
        if o.get('content_note'):note.append(o['content_note'])
        titles=list(dict.fromkeys(r['title'] for r in ms if r['title']))
        row=dict(record_key=k,inventory=o.get('inventory') or k,title=o.get('title') or ' | '.join(titles),
                 master_representation_id=ms[0]['master_representation_id'],
                 master_representation_type=ms[0]['master_representation_type'],master_member_count=str(len(ms)),
                 ordered_master_paths=j([r['final_relative_path'] for r in ms]),
                 access_pdf_paths=j(pdfs),preview_paths=j(previews),page_image_paths='[]',access_note=' '.join(note))
        row.update({'native_'+f:strval(o[f]) for f in native_fields})
        object_rows.append(row)
    return result,object_fields,object_rows

def prepare(unit):
    root=ROOT/UNITS[unit]
    assert not out(unit,'result.json').exists(),'Already applied; use verify'
    db,tables,schema=load_db(root)
    objects,files=unpack(unit,root,tables)
    initial_catalog,_,_=catalog_views(unit,root,tables)
    groups=defaultdict(list)
    for f in files:
        if ismaster(f):groups[f['_key']].append(f)
    moves={}
    for f in files:
        p=pathof(f)
        if isaccess(f) and Path(p).parent==Path('export'):
            moves[p]='export/access/'+Path(p).name
        if ismaster(f) and len(groups[f['_key']])>1 and Path(p).parent==Path('masters'):
            o=next(o for o in objects if key(o)==f['_key'])
            inv=o.get('inventory') or f['_key']
            assert re.fullmatch(r'[\w .-]+',inv),('unsafe existing inventory',inv)
            moves[p]='masters/'+inv+'/'+Path(p).name
    if (root/'README.md').exists():moves['README.md']='00_README.md'
    assert len(set(moves.values()))==len(moves)
    for old,new in moves.items(): assert (root/old).is_file() and not (root/new).exists(),(old,new)
    remap=remapper(root,moves)
    updated=remap_tables(tables,remap)
    manifest_hash_changes={}
    if unit=='brigadebuecher':
        for oldrep,newrep in zip(tables['representations'],updated['representations'],strict=True):
            rid=oldrep['representation_id']
            oldmembers=sorted([m for m in tables['representation_members'] if m['representation_id']==rid],key=lambda m:m['sequence'])
            newmembers=sorted([m for m in updated['representation_members'] if m['representation_id']==rid],key=lambda m:m['sequence'])
            assert member_hash(oldmembers)==oldrep['representation_manifest_sha256'],('existing member-manifest hash differs',rid)
            newhash=member_hash(newmembers)
            if newhash!=oldrep['representation_manifest_sha256']:
                manifest_hash_changes[oldrep['representation_manifest_sha256']]=newhash
                newrep['representation_manifest_sha256']=newhash
    rows,oh,ors=catalog_views(unit,root,updated)
    assert len(rows)==len(initial_catalog)
    payload_stats={}
    for r in initial_catalog:
        p=r['final_relative_path'];s=(root/p).stat()
        assert s.st_size==int(r['size_bytes']),('registered size mismatch',unit,p,s.st_size,r['size_bytes'])
        assert re.fullmatch('[a-f0-9]{64}',r['final_sha256']),('missing stored hash',unit,p)
        payload_stats[p]={'size':s.st_size,'mtime_ns':s.st_mtime_ns,'inode':s.st_ino,'sha256':r['final_sha256']}
    controls=[]
    for base in [root,root/'00_MANIFESTS',root/'db-restore',root/'db-restore/manifests']:
        if base.exists():controls += [p for p in base.iterdir() if p.is_file() and p.suffix in ('.csv','.json','.md','.sql','.sqlite','.xlsx')]
    # Source-captured metadata payload remains verbatim, including old evidence paths.
    inputs={str(p.relative_to(root)):digest(p) for p in controls}
    writes={}
    for p in controls:
        rel=str(p.relative_to(root))
        if rel in payload_stats or p.suffix not in ('.csv','.json','.md') or rel=='CATALOG.csv':continue
        with p.open(newline='') as source:old=source.read()
        new=remap(old)
        for oldhash,newhash in manifest_hash_changes.items():new=new.replace(oldhash,newhash)
        if old!=new:writes[moves.get(rel,rel)]=new
    writes['CATALOG.csv']=bcsv(FIELDS,rows).decode()
    writes['db-restore/objects.csv']=bcsv(oh,ors).decode()
    if unit in ('aeb','bibliothek','feb'):
        native=textrows(updated['files'])
        writes['db-restore/files.csv']=bcsv(list(native[0]),native).decode()
    # Native restore exports stay native; the common catalogue is a readable view.
    rt={'archiv-wf':'objects','brigadebuecher':'catalog','trafo':'representation','wf-sender':'representations'}.get(unit)
    if rt and moves and (root/'db-restore/RESTORE.csv').exists():
        h,rs=readcsv(root/'db-restore/RESTORE.csv')
        writes['db-restore/RESTORE.csv']=bcsv(h,[{k:remap(v) for k,v in r.items()} for r in rs]).decode()
    readme=moves.get('README.md','00_README.md')
    original=(root/('README.md' if 'README.md' in moves else '00_README.md')).read_text()
    intro=remap(original)
    intro+='\n## Shared file catalogue and access layout\n\n'
    intro+=f'Normalized on 2026-09-12. `CATALOG.csv` and the Catalog worksheet in `CATALOG.xlsx` describe {len(rows):,} registered files. `db-restore/objects.csv` and the Objects worksheet describe {len(ors):,} existing objects and their ordered Master members, current access files and recorded access gaps. The native object columns are retained with a `native_` prefix in that view; original metadata and native restore records retain their authority.\n\n'
    intro+='Private selected Masters remain in `masters/`. Multi-file Masters newly grouped during this normalization use one existing inventory folder; accepted older page-set nesting remains intact. Existing reading copies and previews use `export/access/`. PDF member order is preserved, including mixed PDF/image representations. A retained access PDF is not relabelled as generated from scans. `metadata/catalog-fields.md` describes the common fields.\n\n'
    intro+='Master choices, original scan bytes, PDF bytes, source decisions, metadata conflicts and deletion restrictions are unchanged. No new derivative was generated and no media was published. Access gaps in the Objects view are explicit; PDF Masters remain private until an approved public projection selects a reading copy. Earlier accession counts and wording above describe the accession checkpoint; the common file catalogue is the current location view.\n'
    writes[readme]=intro
    writes['metadata/catalog-fields.md']='''# Shared PDF and image catalogue fields

The Catalog worksheet and CATALOG.csv contain one row per registered file.
The Objects worksheet and db-restore/objects.csv contain one row per existing
object, including the complete native object fields. These are generated views
of the existing SQLite tables and readable native manifests, not new authorities.

One selected Master representation can contain several ordered files. Therefore
several preservation_master file rows for an object do not mean several Masters.
master_member_count and ordered_master_paths describe that one representation.
sequence is inherited from the existing manifest and is not a printed page label.

file_role uses preservation_master, access_derivative, access_preview, and the
existing documentation/evidence roles. native_file_role preserves the original
role vocabulary. Retained access PDFs have no invented derivative_of relation.
final_relative_path locates the file under the unit. master_relative_path links
the selected member or representation. final_sha256 reuses the stored final hash.
original_source_path and source_sha256 retain provenance, not deletion authority.
metadata_path and provenance_path point to complete readable native evidence.

All descriptive values, rights, conditions and content mismatches are inherited.
Blank rights do not authorize publication. No new descriptive facts are inferred.
Metadata licensing is not silently substituted for image licensing.

The Objects view lists existing access PDFs and JPEG previews and explicitly
notes missing reading copies or previews. page_image_paths is empty unless a
separate public page-image set has actually been prepared. Private Master paths
and source paths must not be sent to the website as a public catalogue.

The complete native files table, where present, is exported in db-restore/files.csv.
Other units retain their native representation/member CSVs and RESTORE.csv.
SQLite schemas are unchanged. CSV/SQL exports, manifests and filesystem files
remain sufficient to rebuild the catalogue.
'''
    db_updates=[]
    for t,oldrows in tables.items():
        for i,(old,new) in enumerate(zip(oldrows,updated[t],strict=True)):
            changes={k:v for k,v in new.items() if v!=old[k]}
            if changes:db_updates.append({'table':t,'old':old,'changes':changes})
    plan={'unit':unit,'root':str(root),'authorization':'agreed lets make it; Archive normalization and album mapping first',
          'prepared_utc':datetime.now(timezone.utc).isoformat(),'inputs':inputs,'moves':moves,
          'writes':list(writes),'payload_stats':payload_stats,'db':str(db.relative_to(root)),
          'db_updates':db_updates,'schema':schema,'catalog_rows':len(rows),'object_rows':len(ors),
          'expected_native_tables_digest':table_digest(updated),
          'file_roles':dict(Counter(r['file_role'] for r in rows)),
          'missing_access_pdf':sum(not json.loads(o['access_pdf_paths']) for o in ors),
          'missing_preview':sum(not json.loads(o['preview_paths']) for o in ors)}
    for rel,value in writes.items():out(unit,rel).write_text(value)
    dump(out(unit,'workbook.json'),{'unit':unit,'catalog_fields':FIELDS,'catalog_rows':rows,'object_fields':oh,'object_rows':ors})
    dump(out(unit,'plan.json'),plan)
    print(j({k:plan[k] for k in ('unit','catalog_rows','object_rows','file_roles','missing_access_pdf','missing_preview')}),flush=True)

def verify_workbook(unit,path):
    from openpyxl import load_workbook
    expected=json.loads(out(unit,'workbook.json').read_text())
    wb=load_workbook(path,read_only=True,data_only=False)
    assert wb.sheetnames==['Catalog','Objects']
    for name,h,rows in [('Catalog',expected['catalog_fields'],expected['catalog_rows']),('Objects',expected['object_fields'],expected['object_rows'])]:
        it=wb[name].iter_rows(values_only=True)
        assert list(next(it))==h
        for i,(got,want) in enumerate(zip(it,rows,strict=True)):
            assert [strval(v) for v in got]==[strval(want.get(f,'')) for f in h],(unit,name,i)
    wb.close()

def workbook(unit):
    # Artifact Tool export was attempted on 2026-09-12 and aborts in its WASM
    # runtime with unsupported SIMD. Use the already installed bundled fallback.
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table,TableStyleInfo
    data=json.loads(out(unit,'workbook.json').read_text())
    wb=Workbook();wb.remove(wb.active)
    numeric={'size_bytes','sequence','width','height','master_member_count','pages'}
    for name,fields,rows in [('Catalog',data['catalog_fields'],data['catalog_rows']),('Objects',data['object_fields'],data['object_rows'])]:
        ws=wb.create_sheet(name);ws.freeze_panes='C2';ws.sheet_view.showGridLines=False
        ws.append(fields)
        for ri,r in enumerate(rows,2):
            values=[int(r[f]) if f in numeric and re.fullmatch(r'\d+',strval(r.get(f))) else strval(r.get(f)) for f in fields]
            assert all(len(str(v))<=32767 for v in values),(unit,name,ri,'Excel text limit')
            ws.append(values)
            lengths=[]
            for f in fields[:6]:
                px=440 if f=='title' else 460 if 'path' in f else 280 if 'representation' in f else 230 if 'role' in f or 'type' in f else 165
                lengths.append(sum(max(1,len(textwrap.wrap(line,width=int(px/7)))) for line in strval(r.get(f)).split('\n')))
            ws.row_dimensions[ri].height=max(24,max(lengths)*14+8)
        for i,f in enumerate(fields,1):
            px=440 if f=='title' else 460 if 'path' in f or 'sha256' in f else 280 if 'representation' in f else 230 if 'role' in f or 'type' in f else 165
            ws.column_dimensions[get_column_letter(i)].width=(px-5)/7
        bodyfont=Font(name='Arial',size=10)
        for row in ws:
            for cell in row:
                cell.font=bodyfont
                field=fields[cell.column-1]
                cell.alignment=Alignment(vertical='center',wrap_text=(field in fields[:6]))
                if isinstance(cell.value,str):cell.data_type='s'
                elif isinstance(cell.value,int):cell.number_format='#,##0'
        for cell in ws[1]:
            cell.font=Font(name='Arial',size=10,bold=True,color='FFFFFF')
            cell.fill=PatternFill('solid',fgColor='24445B')
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
        ws.row_dimensions[1].height=38
        table=Table(displayName=name+'Table',ref=f'A1:{get_column_letter(len(fields))}{len(rows)+1}')
        table.tableStyleInfo=TableStyleInfo(name='TableStyleMedium2',showRowStripes=True)
        ws.add_table(table)
    wb.save(out(unit,'CATALOG.xlsx'))
    verify_workbook(unit,out(unit,'CATALOG.xlsx'))
    print(j({'unit':unit,'xlsx_readback':'all cells match','writer':'bundled openpyxl; Artifact Tool export unavailable (WASM SIMD)'}),flush=True)

def refresh_views(unit):
    """Finish the common-view corrections found during independent validation."""
    root=ROOT/UNITS[unit];plan=json.loads(out(unit,'plan.json').read_text())
    assert out(unit,'result.json').exists()
    _,tables,_=load_db(root)
    assert table_digest(tables)==plan['expected_native_tables_digest']
    rows,oh,ors=catalog_views(unit,root,tables)
    for r in rows:
        if r['master_relative_path']:assert (root/r['master_relative_path']).exists(),('invalid Master path',unit,r['master_relative_path'])
    out(unit,'CATALOG.csv').write_bytes(bcsv(FIELDS,rows))
    out(unit,'db-restore/objects.csv').write_bytes(bcsv(oh,ors))
    dump(out(unit,'workbook.json'),{'unit':unit,'catalog_fields':FIELDS,'catalog_rows':rows,'object_fields':oh,'object_rows':ors})
    workbook(unit)
    changed={'CATALOG.csv','CATALOG.xlsx','db-restore/objects.csv','metadata/path-normalization.csv','metadata/catalog-fields.md'}
    for rel in ('CATALOG.csv','CATALOG.xlsx','db-restore/objects.csv'):shutil.copyfile(out(unit,rel),root/rel)
    mapping=[{'previous_relative_path':a,'current_relative_path':b,'basis':'User-approved layout normalization, 2026-09-12; bytes unchanged.'} for a,b in plan['moves'].items()]
    (root/'metadata/path-normalization.csv').write_bytes(bcsv(['previous_relative_path','current_relative_path','basis'],mapping))
    p=root/'metadata/catalog-fields.md';text=p.read_text()
    addition='\nCurrent locations are in CATALOG.csv and db-restore/objects.csv. The exact\nnormalization rename map is metadata/path-normalization.csv. Source-captured\nmetadata and accession preparation files registered as evidence retain their\noriginal bytes and accession-time paths. In particular, FEB accession-* evidence\nCSVs are historical captures; compare their paths through the rename map.\nA derivative_of value can retain a native representation ID; master_relative_path\nalways resolves to the selected current file or directory.\n'
    if addition not in text:p.write_text(text+addition)
    for rel in plan['inputs']:
        if 'checksum' not in rel or not rel.endswith('.csv'):continue
        p=root/rel;fields,rs=readcsv(p)
        pk=next((f for f in ('relative_path','path','final_relative_path') if f in fields),None)
        hk=next((f for f in ('sha256','final_sha256','content_sha256') if f in fields),None)
        if not pk or not hk:continue
        for r in rs:
            if r[pk] in changed:
                r[hk]=('sha256:' if r[hk].startswith('sha256:') else '')+digest(root/r[pk])
                r['size_bytes']=str((root/r[pk]).stat().st_size)
                if 'mtime_ns' in fields:r['mtime_ns']=str((root/r[pk]).stat().st_mtime_ns)
        p.write_bytes(bcsv(fields,rs))
    verify(unit)

def apply(unit):
    plan=json.loads(out(unit,'plan.json').read_text());root=ROOT/UNITS[unit]
    assert not out(unit,'result.json').exists(),'Already applied'
    for rel,h in plan['inputs'].items():assert digest(root/rel)==h,('input changed',unit,rel)
    verify_workbook(unit,out(unit,'CATALOG.xlsx'))
    for old,st in plan['payload_stats'].items():
        s=(root/old).stat();assert (s.st_size,s.st_mtime_ns,s.st_ino)==(st['size'],st['mtime_ns'],st['inode'])
    for old,new in plan['moves'].items():
        assert (root/old).is_file() and not (root/new).exists()
    for new in plan['moves'].values():(root/new).parent.mkdir(parents=True,exist_ok=True)
    for old,new in plan['moves'].items():(root/old).rename(root/new)
    c=sqlite3.connect(root/plan['db'])
    c.execute('PRAGMA foreign_keys=ON')
    with c:
        for update in plan['db_updates']:
            old=update['old'];changes=update['changes']
            sql='UPDATE '+q(update['table'])+' SET '+','.join(q(k)+'=?' for k in changes)+' WHERE '+' AND '.join(q(k)+' IS ?' for k in old)
            cur=c.execute(sql,list(changes.values())+list(old.values()))
            assert cur.rowcount==1,('DB update is not unique',update['table'],cur.rowcount)
    for rel in plan['writes']:
        (root/rel).parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(out(unit,rel),root/rel)
    shutil.copyfile(out(unit,'CATALOG.xlsx'),root/'CATALOG.xlsx')
    changed=set(plan['writes'])|{'CATALOG.xlsx'}
    if plan['db_updates']:
        changed.add(plan['db'])
        sqlpath=root/'db-restore/RESTORE.sql'
        if sqlpath.exists():
            sqlpath.write_text('\n'.join(c.iterdump())+'\n');changed.add('db-restore/RESTORE.sql')
    c.close()
    # Existing control hashes are maintained only where already registered.
    # Unchanged payload bytes are never rehashed; catalogs never hash themselves.
    cached={}
    for rel in plan['inputs']:
        if rel.endswith('.csv') and ('checksum' in rel):
            p=root/plan['moves'].get(rel,rel);h,rs=readcsv(p)
            pk=next((x for x in ('relative_path','path','final_relative_path') if x in h),None)
            hk=next((x for x in ('sha256','final_sha256','content_sha256') if x in h),None)
            if not pk or not hk:continue
            for r in rs:
                rp=r[pk];r[pk]=plan['moves'].get(rp,rp);rp=r[pk]
                if rp in changed and rp!=str(p.relative_to(root)):
                    if rp not in cached:cached[rp]=digest(root/rp)
                    r[hk]=('sha256:' if r[hk].startswith('sha256:') else '')+cached[rp]
                    r['size_bytes']=str((root/rp).stat().st_size)
                    if 'mtime_ns' in h:r['mtime_ns']=str((root/rp).stat().st_mtime_ns)
            p.write_bytes(bcsv(h,rs))
    result=verify(unit,write=False)
    result.update({'completed_utc':datetime.now(timezone.utc).isoformat(),'media_generated':0,'media_rehashed':0,
                   'moved_files':len(plan['moves']),'master_selection_changed':False,'publication_performed':False,
                   'source_accessed':False,'deletion_authorized':False})
    # The existing completion receipt records this location/catalogue correction.
    receipt=root/'00_MANIFESTS/accession-completion.json'
    if receipt.exists():
        value=json.loads(receipt.read_text());value['pdf_normalization']=result;dump(receipt,value)
    dump(out(unit,'result.json'),result)
    print(j(result),flush=True)

def verify(unit,write=True):
    plan=json.loads(out(unit,'plan.json').read_text());root=ROOT/UNITS[unit]
    db,tables,schema=load_db(root)
    assert j(schema)==j(plan['schema']),('schema changed',unit)
    assert table_digest(tables)==plan['expected_native_tables_digest'],('unexpected native DB change',unit)
    c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
    assert c.execute('PRAGMA quick_check').fetchone()==('ok',)
    assert not list(c.execute('PRAGMA foreign_key_check'))
    c.close()
    rows,oh,ors=catalog_views(unit,root,tables)
    assert readcsv(root/'CATALOG.csv')==(FIELDS,rows),('catalog differs from native indexes',unit)
    assert readcsv(root/'db-restore/objects.csv')==(oh,ors)
    verify_workbook(unit,root/'CATALOG.xlsx')
    bypath={r['final_relative_path']:r for r in rows}
    for old,want in plan['payload_stats'].items():
        rel=plan['moves'].get(old,old);s=(root/rel).stat()
        assert (s.st_size,s.st_mtime_ns,s.st_ino)==(want['size'],want['mtime_ns'],want['inode']),('payload changed',unit,rel)
        got=bypath[rel]
        assert got['final_sha256']==want['sha256']
    for table,rs in tables.items():
        for r in rs:
            if 'deletion_authorized' in r:assert strval(r['deletion_authorized']).lower() in ('0','false','')
    result={'unit':unit,'validation':'passed','catalog_rows':len(rows),'object_rows':len(ors),
            'master_files':sum(r['file_role']=='preservation_master' for r in rows),
            'missing_access_pdf':sum(not json.loads(o['access_pdf_paths']) for o in ors),
            'missing_preview':sum(not json.loads(o['preview_paths']) for o in ors),
            'sqlite_quick_check':'ok','foreign_key_violations':0,'xlsx_readback':'all cells match',
            'payload_paths_sizes_inodes_mtimes':'unchanged except authorized paths','stored_payload_hashes':'unchanged'}
    if write:dump(out(unit,'validation.json'),result);print(j(result),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','workbook','apply','verify','refresh_views']);p.add_argument('unit',choices=list(UNITS))
    a=p.parse_args();globals()[a.command](a.unit)
