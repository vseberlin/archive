"""Bounded WF-Alben image normalization authorized by Vladimir.

Retains image identities, album relationships, Master choices and every payload
byte. Uses the existing SQLite schema, CSV/SQL exports and manifest locations.
"""
import csv
import json
import shutil
import sqlite3
import sys
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import photo_normalization as common

UNIT='wf-alben'
U=Path('/mnt/volume/iss-archive/units')/UNIT
W=Path('/mnt/volume/iss-archive-work')
AW=W/'wf-alben/work/reconciliation'
ALBUM_DB=AW/'wf-alben-album-workbench.sqlite'
OBJECTS='metadata/objects.csv'
MEMBERS='metadata/album-memberships.csv'
REPS='db-restore/representations.csv'
DOCS='db-restore/document-provenance.csv'
CHECKS='db-restore/checksums.csv'
PAYLOAD='00_MANIFESTS/payload-checksums.csv'
RECEIPT='00_MANIFESTS/accession-completion.json'
DB='db-restore/RESTORE.sqlite'
MFIELDS=['album_key','album_title','record_key','md_object_id','inventory',
         'member_kind','component_label','linked_record_key','link_relation',
         'master_relative_path','access_relative_path']

def st(p):return common.staged(UNIT,p)
def read(p):return common.read_csv(U/p)
def jwrite(p,v):common.jwrite(st(p),v)
def sha(p):return common.sha(p)
def conn(p):
    c=sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row;return c
def snapshot(c):
    return {t:[dict(r) for r in c.execute('SELECT * FROM "'+t+'"')]
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
def meta_albums(albums,members):
    # Four album entries in the existing key/value table mirror the readable CSV.
    # Album IDs and member IDs already exist. No new table or object is created.
    return [{'key':'album_membership:'+a['carrier_key'],
             'value':json.dumps({'title':a['title'],'membership_csv':MEMBERS,
                                'record_keys':[m['record_key'] for m in members if m['album_key']==a['carrier_key']]},
                               ensure_ascii=False,separators=(',',':'))} for a in albums]

def prepare():
    assert not (U/'export/access').exists() and not (U/OBJECTS).exists()
    assert not (U/'CATALOG.xlsx').exists()
    oh,objects=read('CATALOG.csv');rh,reps=read(REPS);ch,checks=read(CHECKS)
    dh,documents=read(DOCS);ph,payload=read(PAYLOAD)
    assert read('CATALOG.csv')==read('db-restore/RESTORE.csv')==read('metadata/museum-digital-effective.csv')
    assert len(objects)==143 and len(reps)==len(checks)==286
    included=[d for d in documents if d['disposition']=='included_representative']
    assert len(documents)==64 and len(included)==31
    c=conn(U/DB);original=snapshot(c)
    normalized_db_objects=[dict(r) for r in original['objects']]
    empty_ids=[]
    for r in normalized_db_objects:
        for field in ['md_image_id','parent_md_object_id']:
            if r[field]=='None':
                r[field]='';empty_ids.append((r['record_key'],field))
    assert len(empty_ids)==270
    assert normalized_db_objects==objects and original['representations']==reps and original['checksums']==checks
    assert original['document_provenance']==documents
    ac=conn(ALBUM_DB);albums=[dict(r) for r in ac.execute('select * from objects')];ac.close()
    assert {a['carrier_key'] for a in albums}=={'ALB-001','ALB-002','ALB-003','ALBUM-NEF'}
    mdmap={md:a for a in albums for md in json.loads(a['md_object_ids_json'])}
    assert len(mdmap)==sum(len(json.loads(a['md_object_ids_json'])) for a in albums)==178
    moves={r['final_relative_path']:'export/access/'+Path(r['final_relative_path']).name
           for r in reps if r['role']=='access_derivative'}
    moves['metadata/museum-digital-effective.csv']=OBJECTS
    assert len(moves)==144
    plan={'unit':UNIT,'authorization':'lets normalize images too preserving album relations.',
          'prepared_utc':datetime.now(timezone.utc).isoformat(),'moves':moves,
          'inputs':{},'writes':[],'payload_stats':{},'work_inputs':{},
          'original_database':original,'albums':albums,'empty_id_fields':empty_ids,
          'original_readme':(U/'00_README.md').read_text()}
    for base in [U,U/'00_MANIFESTS',U/'metadata',U/'db-restore']:
        for p in base.iterdir():
            if p.is_file() and p.suffix in {'.csv','.json','.md','.sql','.sqlite'}:
                plan['inputs'][str(p.relative_to(U))]=sha(p)
    for p in [ALBUM_DB,AW/'workbench.sqlite',*AW.glob('wf-alben-album-*.csv'),AW/'wf-alben-album-workbench-preflight.json']:
        plan['work_inputs'][str(p)]=sha(p)
    hashes={r['path']:r for r in checks}
    for d in included:
        assert d['accession_path'] not in hashes
        hashes[d['accession_path']]={'sha256':d['content_sha256'],'size_bytes':d['size_bytes']}
    assert len(hashes)==317
    for p,h in hashes.items():
        stat=(U/p).stat();assert stat.st_size==int(h['size_bytes']),p
        plan['payload_stats'][p]={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'inode':stat.st_ino,
                                  'sha256':h['sha256'].removeprefix('sha256:')}
    def put(p,data):
        st(p).write_bytes(data);plan['writes'].append(p)
    def putcsv(p,h,r):put(p,common.csv_bytes(h,r))
    # Change only current access locations. Historical provenance stays verbatim.
    objects=[dict(r,access_relative_path=moves[r['access_relative_path']]) for r in objects]
    reps=[dict(r,final_relative_path=moves.get(r['final_relative_path'],r['final_relative_path'])) for r in reps]
    checks=[dict(r,path=moves.get(r['path'],r['path'])) for r in checks]
    for p in [OBJECTS,'db-restore/RESTORE.csv']:putcsv(p,oh,objects)
    putcsv(REPS,rh,reps);putcsv(CHECKS,ch,checks)
    ob={o['record_key']:o for o in objects}
    members=[]
    for o in objects:
        a=mdmap[o['md_object_id']]
        members.append({'album_key':a['carrier_key'],'album_title':a['title'],
            'record_key':o['record_key'],'md_object_id':o['md_object_id'],'inventory':o['inventory'],
            'member_kind':'accessioned_image','component_label':o['component_label'],
            'linked_record_key':o['linked_record_key'],'link_relation':o['link_relation'],
            'master_relative_path':o['master_relative_path'],'access_relative_path':o['access_relative_path']})
    for r in original['derived_image_relationships']:
        a=mdmap[r['derived_md_object_id']];parent=ob[r['parent_record_key']]
        assert mdmap[parent['md_object_id']]['carrier_key']==a['carrier_key']
        members.append({'album_key':a['carrier_key'],'album_title':a['title'],
            'record_key':r['derived_record_key'],'md_object_id':r['derived_md_object_id'],'inventory':r['derived_inventory'],
            'member_kind':'redundant_derived_record','component_label':'',
            'linked_record_key':r['parent_record_key'],'link_relation':r['relationship'],
            'master_relative_path':'','access_relative_path':''})
    members.sort(key=lambda r:(r['album_key'],r['member_kind'],r['inventory'],r['record_key']))
    assert len(members)==len({m['record_key'] for m in members})==182
    counts=Counter(m['album_key'] for m in members if m['member_kind']=='accessioned_image')
    assert counts=={'ALB-001':55,'ALB-002':25,'ALB-003':39,'ALBUM-NEF':24}
    putcsv(MEMBERS,MFIELDS,members)
    plan['album_counts']=dict(counts);plan['album_meta']=meta_albums(albums,members)
    # Stage the existing restore database, keeping its schema intact.
    assert not st(DB).exists()
    target=sqlite3.connect(st(DB));c.backup(target);c.close()
    for old,new in moves.items():
        if not old.startswith('export/'):continue
        for table,col in [('objects','access_relative_path'),('representations','final_relative_path'),('checksums','path')]:
            cur=target.execute('UPDATE "'+table+'" SET "'+col+'"=? WHERE "'+col+'"=?',(new,old))
            assert cur.rowcount==1,(table,old)
    for row in plan['album_meta']:
        assert target.execute('select count(*) from meta where key=?',(row['key'],)).fetchone()[0]==0
        target.execute('insert into meta(key,value) values (?,?)',(row['key'],row['value']))
    for key,field in empty_ids:
        cur=target.execute('UPDATE objects SET "'+field+'"=\'\' WHERE record_key=? AND "'+field+'"=\'None\'',(key,))
        assert cur.rowcount==1
    target.commit()
    put('db-restore/RESTORE.sql',('\n'.join(target.iterdump())+'\n').encode('utf-8'))
    target.close();plan['writes'].append(DB)
    surrogate={r['record_key']:r for r in json.loads((U/'metadata/catalogue-surrogate-resolution.json').read_text())['surrogates']}
    fields=common.FIELDS+['album_key']
    rows=[]
    for rep in reps:
        o=ob[rep['record_key']];album=mdmap[o['md_object_id']]['carrier_key']
        p=rep['final_relative_path'];master=rep['role']=='preservation_master'
        relationship='album='+album
        if o['component_label']:relationship+='; component='+o['component_label']
        if o['linked_record_key']:relationship+='; '+o['link_relation']+'='+o['linked_record_key']
        condition=''
        if master and o['record_key'] in surrogate:
            condition='user-approved catalogue surrogate; '+surrogate[o['record_key']]['source_dimensions']
        values={'record_key':o['record_key'],'inventory':o['inventory'],'title':o['title'],
            'file_role':rep['role'],'final_relative_path':p,'master_relative_path':o['master_relative_path'],
            'filename':Path(p).name,'file_type':Path(p).suffix[1:],'size_bytes':rep['size_bytes'],
            'final_sha256':rep['final_sha256'].removeprefix('sha256:'),
            'description':o['description'],'date_text':o['date_text'],'creator':o['creator'],
            'object_type':o['object_type'],'material':o['material_technique'],'dimensions':o['dimensions'],
            'places':o['place'],'tags':o['subjects'],'md_object_id':o['md_object_id'],'md_url':o['md_url'],
            'rights':o['image_rights'],'rights_holder':o['image_rights_holder'],
            'metadata_authority':o['metadata_authority'],'original_source_path':rep['source_path'],
            'source_sha256':rep['source_sha256'].removeprefix('sha256:'),
            'derivative_of':'' if master else o['master_relative_path'],
            'relationship':relationship,'condition':condition,'notes':rep['basis']+'; '+rep['copy_policy'],
            'metadata_path':OBJECTS+'\n'+MEMBERS,'provenance_path':REPS,'album_key':album}
        rows.append({f:values.get(f,'') for f in fields})
    for d in included:
        p=d['accession_path'];wrong_pdf=d['filename']=='Album_NEF.pdf'
        if wrong_pdf:
            with (U/p).open('rb') as f:assert f.read(3)==b'\xff\xd8\xff'
        values={'record_key':d['document_id'],'title':d['filename'],'file_role':'documentation_original',
            'final_relative_path':p,'filename':Path(p).name,'file_type':'jpg' if wrong_pdf else Path(p).suffix[1:],
            'size_bytes':d['size_bytes'],'final_sha256':d['content_sha256'].removeprefix('sha256:'),
            'description':d['reason'],'original_source_path':d['source_path'],
            'source_sha256':d['content_sha256'].removeprefix('sha256:'),
            'relationship':d['document_role'],
            'condition':'JPEG content stored with .pdf extension; retained as documented source evidence' if wrong_pdf else 'retained original; no new content validation during normalization',
            'notes':d['reason']+'; recorded inventory coverage='+d['inventory_coverage_count'],
            'metadata_path':DOCS,'provenance_path':DOCS}
        rows.append({f:values.get(f,'') for f in fields})
    rows.sort(key=lambda r:(r['file_role']=='documentation_original',r['album_key'],r['inventory'],r['record_key'],r['file_role']=='access_derivative'))
    assert len(rows)==317
    putcsv('CATALOG.csv',fields,rows)
    # The Objects sheet has the intact native fields plus the derived album key.
    workbook_objects=[dict(o,album_key=mdmap[o['md_object_id']]['carrier_key']) for o in objects]
    jwrite('workbook.json',{'unit':UNIT,'catalog_fields':fields,'catalog_rows':rows,
                          'object_fields':oh+['album_key'],'object_rows':workbook_objects})
    text='# WF-Alben supporting documents\n\n31 retained originals. The 64 source occurrences and their selection/duplicate decisions remain in `../db-restore/document-provenance.csv`. The matching preparation copy is `../00_MANIFESTS/document-provenance.csv`.\n\n'
    text+='Dates and condition are reported only where recorded. No document version was merged or discarded.\n\n'
    from urllib.parse import quote
    for d in sorted(included,key=lambda r:r['accession_path']):
        p=d['accession_path'];r=next(r for r in rows if r['final_relative_path']==p)
        duplicates=[x['source_path'] for x in documents if x['accession_path']==p and x['disposition']=='exact_duplicate_of_included_document']
        text+=f'## {d["filename"]}\n\n'
        text+=f'- File: [{Path(p).name}]({quote(Path(p).name)})\n- ID: `{d["document_id"]}`\n'
        text+=f'- Relation: {d["reason"]}\n- Recorded role: `{d["document_role"]}`\n- Recorded inventory coverage: {d["inventory_coverage_count"]}\n'
        text+=f'- Source: `{d["source_path"]}`\n- Date: not recorded in the document provenance table.\n'
        text+=f'- Condition: {r["condition"]}.\n- SHA-256: `{d["content_sha256"]}`\n'
        if duplicates:text+='- Exact duplicate sources: '+'; '.join('`'+x+'`' for x in duplicates)+'\n'
        text+='\n'
    put('documents/CONTENTS.md',text.encode())
    guide=common.schema_text('pressebildarchiv-wf')
    guide=guide[:guide.index('Objects CSV:')]
    guide+='''Objects CSV: `metadata/objects.csv`, also exported as `db-restore/RESTORE.csv`.
File provenance: `db-restore/representations.csv` and `db-restore/document-provenance.csv`.

## Album relationships

The extra album_key column uses the four existing carrier IDs ALB-001, ALB-002,
ALB-003 and ALBUM-NEF. It is a grouping field and does not select an album Master.
metadata/album-memberships.csv lists all 143 accessioned image objects and the
39 redundant derived records, with their existing parent/component links.
The four groups were read from the existing album workbench's objects table and
matched through its explicit museum-digital ID lists. Membership was not guessed
from filename similarity. SQLite's existing meta key/value table mirrors each
album's title and member record keys; no schema or object identity changed.

The Objects sheet includes the complete native object CSV plus the album_key
join from album-memberships.csv. The Catalog sheet equals CATALOG.csv. Album
memberships and the four component pairs do not establish physical page order
or complete album coverage; sequence is intentionally empty until that review.
Each of the eight paired image components retains its own selected Master.

Image rights and metadata rights remain separate in the native Objects records.
The common file catalogue's rights fields describe image rights only. Documentation
does not inherit image licences. The two approved catalogue surrogates retain
their recorded role and explicit condition notes.

## File types and recovery

The catalogue covers 143 Masters, 143 existing access JPEGs and 31 documents.
Album_NEF.pdf has JPEG content: file_type is jpg, while its retained filename
keeps the original .pdf extension and condition explains the mismatch.
No PDF is created, repaired or accepted by this normalization.

The original 43-column object fields are preserved in metadata/objects.csv and
db-restore/RESTORE.csv. The earlier equivalent metadata/museum-digital-effective.csv
was renamed to metadata/objects.csv. db-restore/RESTORE.sql restores the full
existing catalogue and all decision/relationship tables, including the four album
metadata entries mirrored by album-memberships.csv. Historical source paths stay
verbatim. The separate unfinished album workbench is unchanged.

The legacy SQLite text placeholder None in missing md_image_id/parent_md_object_id
fields was aligned to the existing CSV's empty values (135 records, two fields).
No identifiers were assigned, removed or changed.

Final image hashes are in db-restore/checksums.csv. Documentation and supporting
record hashes are in 00_MANIFESTS/payload-checksums.csv; current changed-control
hashes are also recorded by 00_MANIFESTS/accession-completion.json. Existing media
hashes are reused. Files and readable records are the recovery starting point.
'''
    put('metadata/catalog-fields.md',guide.encode())
    text=plan['original_readme'].replace('This unit contains the fully reconciled and accessioned WF-Alben collection.',
        'This unit contains the accessioned image components of four identified WF-Alben albums. Whole-album reconstruction and PDF Master decisions remain separate, unfinished work.')
    text=text.replace('`export/` contains one full-resolution JPEG generated from',
                      '`export/access/` contains one full-resolution JPEG generated from')
    text=text.replace('`CATALOG.csv` is the human-readable object catalogue.',
        '`CATALOG.csv` and the Catalog sheet in `CATALOG.xlsx` contain 317 retained-file rows. `metadata/objects.csv` preserves all 143 complete object records; the Objects sheet adds their album membership for browsing.')
    text+='\n## Photo catalogue convention\n\nNormalized on 2026-09-11. The MD collection description above is unchanged.\n\n'
    text+='| Album | Recorded title | Accessioned image objects | Linked redundant derived records |\n| --- | --- | ---: | ---: |\n'
    for a in albums:
        key=a['carrier_key'];text+=f'| {key} | {a["title"]} | {counts[key]} | {sum(m["album_key"]==key and m["member_kind"]=="redundant_derived_record" for m in members)} |\n'
    text+='''
metadata/album-memberships.csv keeps all 182 image/derived-record memberships
readable, using the existing album and image identifiers. The four component
pairs remain in db-restore/linked-object-relationships.csv, and the 39 NEF derived
records retain their parent links in db-restore/derived-image-relationships.csv.
Each of the 143 image objects still has exactly one selected Master. Album
membership does not establish complete page coverage or a confirmed reading order.

documents/CONTENTS.md indexes all 31 retained originals, including the misnamed
Album_NEF.pdf whose actual content is JPEG. Its filename and bytes are preserved.
metadata/catalog-fields.md explains catalogue columns, rights, roles and recovery.

Only the 143 existing access JPEG locations and the object-catalogue filename were
normalized. Media bytes, Master selections, metadata decisions, source dispositions
and the separate album-level workbench are unchanged. No derivatives were generated.
'''
    put('00_README.md',text.encode())
    plan['writes']=sorted(set(plan['writes']));jwrite('plan.json',plan)
    print(json.dumps({'staged':plan['writes'],'album_counts':plan['album_counts'],'file_rows':317,'object_rows':143,'memberships':182}))

def workbook():
    common.workbook_fallback(UNIT)
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter
    wb=load_workbook(st('CATALOG.xlsx'))
    for name,wrapped in [('Catalog',{'relationship':90}),('Objects',{'component_label':40,'image_description':75})]:
        sheet=wb[name];fields=[c.value for c in sheet[1]]
        for field,width in wrapped.items():
            col=fields.index(field)+1;sheet.column_dimensions[get_column_letter(col)].width=width
            for row in sheet.iter_rows(min_row=2,min_col=col,max_col=col):
                cell=row[0];cell.alignment=Alignment(wrap_text=True,vertical='top')
                lines=sum(max(1,len(textwrap.wrap(line,width=int(width*.92)))) for line in str(cell.value or '').split('\n'))
                sheet.row_dimensions[cell.row].height=max(sheet.row_dimensions[cell.row].height or 23,14*lines+7)
    wb.save(st('CATALOG.xlsx'))
    common.check_workbook(UNIT,st('CATALOG.xlsx'))
    plan=json.loads(st('plan.json').read_text());plan['writes']=sorted(set(plan['writes']+['CATALOG.xlsx']))
    jwrite('plan.json',plan)

def finalize():
    plan=json.loads(st('plan.json').read_text())
    plan['writes']=[p for p in plan['writes'] if p not in {PAYLOAD,RECEIPT}]
    fields,rows=read(PAYLOAD);by={r['relative_path']:r for r in rows}
    for p in plan['writes']:
        by[p]={'relative_path':p,'size_bytes':str(st(p).stat().st_size),'sha256':sha(st(p)),'payload_type':'unit_record'}
    st(PAYLOAD).write_bytes(common.csv_bytes(fields,[by[p] for p in sorted(by)]))
    plan['writes'].append(PAYLOAD)
    receipt=json.loads((U/RECEIPT).read_text())
    controls=receipt['control_file_sha256']
    controls.pop('metadata/museum-digital-effective.csv')
    for p in plan['writes']:controls[p]='sha256:'+sha(st(p))
    receipt['photo_normalization']={'authorization':plan['authorization'],
        'normalized_utc':datetime.now(timezone.utc).isoformat(),'catalogue_row_kind':'retained_file',
        'file_rows':317,'object_rows':143,'album_memberships':182,'album_image_counts':plan['album_counts'],
        'component_pairs':4,'derived_record_relationships':39,'access_files_moved':143,
        'payload_checksum_rows':len(by),'media_bytes_changed':False,'masters_changed':False,
        'media_rehashed':False,'derivatives_generated':0,'album_workbench_changed':False,'deletion_authorized':False}
    jwrite(RECEIPT,receipt);plan['writes'].append(RECEIPT)
    plan['output_hashes']={p:sha(st(p)) for p in plan['writes']}
    jwrite('plan.json',plan)
    verify(True)

def verify(staged=False):
    plan=json.loads(st('plan.json').read_text());moves=plan['moves']
    def resolve(p):return st(p) if staged and p in plan['writes'] else U/p
    for p,h in plan['work_inputs'].items():assert sha(Path(p))==h,('Changed work evidence',p)
    for old,data in plan['payload_stats'].items():
        p=U/(old if staged else moves.get(old,old));s=p.stat()
        assert (s.st_size,s.st_mtime_ns,s.st_ino)==(data['size'],data['mtime_ns'],data['inode']),old
    for p,h in plan['inputs'].items():
        if p not in plan['writes'] and p not in moves:assert sha(U/p)==h,('Changed unrelated control',p)
    for p,h in plan['output_hashes'].items():assert sha(resolve(p))==h,p
    _,objects=common.read_csv(resolve(OBJECTS));_,files=common.read_csv(resolve('CATALOG.csv'))
    _,members=common.read_csv(resolve(MEMBERS));_,reps=common.read_csv(resolve(REPS))
    _,checks=common.read_csv(resolve(CHECKS))
    assert common.read_csv(resolve(OBJECTS))==common.read_csv(resolve('db-restore/RESTORE.csv'))
    assert len(files)==317 and len(objects)==143 and len(members)==182
    ob={o['record_key']:o for o in objects};fm={r['final_relative_path']:r for r in files}
    assert len(fm)==317
    assert Counter(r['file_role'] for r in files)=={'preservation_master':143,'access_derivative':143,'documentation_original':31}
    for o in objects:
        selected=[r for r in reps if r['record_key']==o['record_key'] and r['role']=='preservation_master']
        assert len(selected)==1 and selected[0]['final_relative_path']==o['master_relative_path']
        assert sum(r['record_key']==o['record_key'] and r['role']=='access_derivative' for r in reps)==1
    cm={r['path']:r for r in checks}
    for r in reps:
        p=r['final_relative_path'];f=fm[p]
        assert (f['record_key'],f['file_role'],f['final_sha256'],f['size_bytes'])==(
            r['record_key'],r['role'],r['final_sha256'].removeprefix('sha256:'),r['size_bytes'])
        assert cm[p]['sha256']==r['final_sha256']
        if f['file_role']=='access_derivative':assert f['derivative_of']==ob[f['record_key']]['master_relative_path']
    db=conn(resolve(DB));actual=snapshot(db)
    expected=json.loads(json.dumps(plan['original_database']))
    for table,col in [('objects','access_relative_path'),('representations','final_relative_path'),('checksums','path')]:
        for r in expected[table]:r[col]=moves.get(r[col],r[col])
    expected['meta']+=plan['album_meta']
    for r in expected['objects']:
        for field in ['md_image_id','parent_md_object_id']:
            if [r['record_key'],field] in plan['empty_id_fields']:r[field]=''
    assert actual==expected,'Only current access paths, empty ID placeholders and four album metadata entries may change'
    assert actual['objects']==objects and actual['representations']==reps and actual['checksums']==checks
    assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert not db.execute('pragma foreign_key_check').fetchall()
    original=conn(U/DB) if staged else None
    if original:
        assert original.execute('select type,name,sql from sqlite_master order by name').fetchall()==db.execute('select type,name,sql from sqlite_master order by name').fetchall()
        original.close()
    restored=sqlite3.connect(':memory:');restored.row_factory=sqlite3.Row
    restored.executescript(resolve('db-restore/RESTORE.sql').read_bytes().decode('utf-8'))
    assert snapshot(restored)==actual,'SQL restore changes saved data'
    restored.close();db.close()
    mdmap={md:a['carrier_key'] for a in plan['albums'] for md in json.loads(a['md_object_ids_json'])}
    membership={r['record_key']:r for r in members};assert len(membership)==182
    assert Counter(r['album_key'] for r in members if r['member_kind']=='accessioned_image')==plan['album_counts']
    assert {r['record_key'] for r in members if r['member_kind']=='accessioned_image'}==set(ob)
    for o in objects:
        m=membership[o['record_key']];assert m['album_key']==mdmap[o['md_object_id']]
        for f in ['md_object_id','inventory','component_label','linked_record_key','link_relation','master_relative_path','access_relative_path']:
            assert m[f]==o[f]
        assert all(f['album_key']==m['album_key'] for f in files if f['record_key']==o['record_key'])
    for r in actual['derived_image_relationships']:
        m=membership[r['derived_record_key']]
        assert m['linked_record_key']==r['parent_record_key'] and not m['master_relative_path'] and not m['access_relative_path']
        assert m['album_key']==mdmap[r['derived_md_object_id']]==membership[r['parent_record_key']]['album_key']
    for r in actual['linked_object_relationships']:
        a=ob[r['primary_record_key']];b=ob[r['extension_record_key']]
        assert a['linked_record_key']==b['record_key'] and b['linked_record_key']==a['record_key']
        assert membership[a['record_key']]['album_key']==membership[b['record_key']]['album_key']
    assert [r for r in actual['meta'] if r['key'].startswith('album_membership:')]==meta_albums(plan['albums'],members)
    for rows in actual.values():
        for r in rows:
            if 'deletion_authorized' in r:assert r['deletion_authorized']=='false'
    for d in actual['document_provenance']:
        if d['disposition']=='included_representative':
            f=fm[d['accession_path']]
            assert f['final_sha256']==d['content_sha256'].removeprefix('sha256:') and f['size_bytes']==d['size_bytes']
            assert f['rights']==f['rights_holder']==''
    nef=next(r for r in files if r['filename'].endswith('__Album_NEF.pdf'))
    assert nef['file_type']=='jpg' and 'JPEG' in nef['condition']
    common.check_workbook(UNIT,resolve('CATALOG.xlsx'))
    _,payload=common.read_csv(resolve(PAYLOAD));assert len(payload)==len({r['relative_path'] for r in payload})
    for r in payload:
        p=r['relative_path'];path=resolve(p)
        assert path.stat().st_size==int(r['size_bytes']),p
        if p in plan['payload_stats']:assert r['sha256'].removeprefix('sha256:')==plan['payload_stats'][p]['sha256']
        else:assert sha(path)==r['sha256'].removeprefix('sha256:'),p
    receipt=json.loads(resolve(RECEIPT).read_text())
    for p,h in receipt['control_file_sha256'].items():assert sha(resolve(p))==h.removeprefix('sha256:'),p
    text=resolve('00_README.md').read_text()
    assert text.split('<!-- md-collection-description:end -->')[0]==plan['original_readme'].split('<!-- md-collection-description:end -->')[0]
    if not staged:
        assert len(list((U/'export/access').glob('*.jpg')))==143
        assert not list((U/'export').glob('*.jpg')) and not (U/'metadata/museum-digital-effective.csv').exists()
    result={'unit':UNIT,'state':'staged_validated' if staged else 'complete','file_rows':317,'image_objects':143,
        'album_image_counts':plan['album_counts'],'album_memberships':182,'component_pairs':4,
        'derived_record_relationships':39,'masters_unchanged':143,'payload_files_unchanged':317,
        'source_relationships_unchanged':1070,'csv_sqlite_agree':True,'xlsx_all_cells_verified':True,
        'sql_restore_verified':True,'media_rehashed':False,'media_generated':0,
        'album_workbench_unchanged':True,'deletion_authorized':False}
    jwrite('validation.json',result);print(json.dumps(result))
    return result

def apply():
    plan=json.loads(st('plan.json').read_text())
    for p,h in plan['inputs'].items():assert sha(U/p)==h,p
    for p,h in plan['output_hashes'].items():assert sha(st(p))==h,p
    for old,new in plan['moves'].items():assert (U/old).is_file() and not (U/new).exists()
    assert not (U/'export/access').exists()
    for p in plan['writes']:
        if p not in plan['inputs'] and p not in plan['moves'].values():assert not (U/p).exists(),p
    wal=Path(str(U/DB)+'-wal');assert not wal.exists() or wal.stat().st_size==0
    # The only new directory is the approved export/access location.
    (U/'export/access').mkdir()
    for old,new in plan['moves'].items():(U/old).rename(U/new)
    for p in plan['writes']:shutil.copyfile(st(p),U/p)
    result=verify();result['completed_utc']=datetime.now(timezone.utc).isoformat();jwrite('result.json',result)

if __name__=='__main__':
    {'prepare':prepare,'workbook':workbook,'finalize':finalize,'apply':apply,'verify':verify}[sys.argv[1]]()
