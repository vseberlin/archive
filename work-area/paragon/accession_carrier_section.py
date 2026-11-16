#!/usr/bin/env python3
"""Accession the explicitly selected VLV-1130 section using existing unit records."""
import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import textwrap
from datetime import datetime, timezone
from uuid import uuid4
from collections import Counter

from PIL import Image, ImageOps, ImageDraw, ImageFont
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
import paragon_workbench as review

W = review.ROOT
U = review.ARCHIVE
SOURCE = review.SOURCE / 'Paragon/Wandzeitung/Rohscans 01/VLV-1130-5.tif'
ENTITY = 'ISS-2026-E5ES-0'
PHOTO = 'ISS-2026-JS55-S'
INVENTORY = 'VLV-1130'
OVERVIEW = 'masters/VLV-1130__ISS-2026-E5ES-0__surrogate.tif'
MASTER = 'masters/VLV-1130__ISS-2026-E5ES-0__section-VLV-1130-5__master.tif'
ACCESS = 'export/access/VLV-1130__ISS-2026-E5ES-0__section-VLV-1130-5__access.jpg'
RECEIPT = 'db-restore/manifests/carrier-section-accession.json'
WORK_RECEIPT = W / 'carrier-section-accession.json'
AUTHORIZATION = 'ok let accession that'
PART = 'section shown in source VLV-1130-5.tif'
NOTE = ('Vladimir assigned the full raw VLV-1130-5.tif to carrier VLV-1130 and authorized accession. '
        'This scan contains the tower photograph, another photograph and surrounding carrier paper. '
        'It is a section, not a complete carrier view. The existing overview and this complementary '
        'section are members of one selected carrier Master. The separately catalogued photograph '
        'VLV-1130-5 retains its cropped Master. Complete high-resolution carrier coverage remains unresolved.')

def read_csv(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)

def write_csv(path, fields, rows):
    with Path(path).open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')

def stamp(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns]

def connect(path, writable=False):
    c = sqlite3.connect(f'file:{path}?mode={"rw" if writable else "ro"}', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def file_rows(c, entity):
    return [dict(r) for r in c.execute('SELECT ef.*,fo.relative_path,fo.content_digest,fo.note '
                'FROM entity_files ef JOIN file_occurrences fo USING(occurrence_id) '
                'WHERE ef.entity_id=? AND ef.status=\'active\' ORDER BY fo.relative_path', (entity,))]

def preflight():
    review.enable_drain_review()
    health = review.check()
    assert health['decisions_agree']
    assert not (U/RECEIPT).exists(), 'Accession already recorded; validate, do not repeat.'
    assert not WORK_RECEIPT.exists(), 'Inspect the existing accession record before repeating.'
    assert SOURCE.resolve(strict=True) == SOURCE and not SOURCE.is_symlink()
    for path in [MASTER, ACCESS]:
        assert not (U/path).exists(), f'Refusing overwrite: {path}'
        assert (U/path).parent.is_dir()
    evidence = next(r for r in json.loads((W/'drain-evidence.json').read_text())
                    if r['source_path'] == str(SOURCE))
    assert stamp(SOURCE) == [evidence['source_size_bytes'], evidence['source_mtime_ns']]
    assert evidence['source_sha256'] == 'sha256:f64f4b4e73f7ddfaca44a3e889e661ed4f83b4510feef6e086e54a34f5e43923'
    with Image.open(SOURCE) as im:
        im.load()
        assert im.size == (5024, 6960) and im.mode == 'RGB' and getattr(im, 'n_frames', 1) == 1
    assert shutil.disk_usage(U).free > 350_000_000
    for db in [W/'work/catalog.sqlite', U/'db-restore/RESTORE.sqlite']:
        with connect(db) as c:
            assert c.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            assert c.execute('SELECT status FROM archive_objects WHERE entity_id=?',(ENTITY,)).fetchone()[0] == 'active'
            assert len(file_rows(c, ENTITY)) == 2
            if db == U/'db-restore/RESTORE.sqlite':
                assert len(file_rows(c, PHOTO)) == 2
    objects = read_csv(U/'metadata/objects.csv')[1]
    carrier = next(r for r in objects if r['iss_id'] == ENTITY)
    assert carrier['master_file'] == OVERVIEW and carrier['master_member_count'] == '1'
    return evidence, carrier, health

def embed(path, carrier):
    # Metadata writes happen only to the new accession file; compare decoded pixels directly.
    with Image.open(path) as im:
        pixels, dimensions = im.tobytes(), im.size
    title = carrier['descriptive_title'] + ' — Teilscan VLV-1130-5'
    description = (carrier['description'] + '\n\nTeilscan der Wandzeitung VLV-1130: '
                   'Turmfoto, weiteres Foto und umgebendes Trägerpapier. '
                   'Quelldatei VLV-1130-5.tif. Keine Gesamtansicht.')
    tags = {'XMP-dc:Title': title, 'XMP-dc:Identifier': ENTITY,
            'XMP-iptcExt:ArtworkSourceInventoryNo': INVENTORY,
            'XMP-dc:Description': description, 'IFD0:ImageDescription': description,
            'IPTC:CodedCharacterSet':'UTF8', 'IPTC:Caption-Abstract': description}
    subprocess.run(['exiftool','-overwrite_original','-P','-charset','IPTC=UTF8',
                    *['-'+k+'='+v for k,v in tags.items()],str(path)],
                   check=True,capture_output=True,timeout=90)
    actual = json.loads(subprocess.check_output(['exiftool','-j','-G1','-s',
                        *['-'+k for k in tags],str(path)], text=True,timeout=30))[0]
    assert all(actual.get(k)==v for k,v in tags.items()), actual
    with Image.open(path) as im:
        im.load()
        assert im.size == dimensions and im.tobytes() == pixels
    return {'embedded_title':title,'embedded_description':description,'pixel_preservation_verified':True}

def render_range(path, sheet_name, first_row, last_row, first_col, last_col, output):
    # Read-only workbook preview fallback: use saved cell sizes, fills, wrapping and fonts.
    wb=load_workbook(path); sheet=wb[sheet_name]
    widths=[max(40,int((sheet.column_dimensions[get_column_letter(c)].width or 13)*7))
            for c in range(first_col,last_col+1)]
    heights=[max(24,int((sheet.row_dimensions[r].height or 15)*4/3)) for r in range(first_row,last_row+1)]
    image=Image.new('RGB',(sum(widths)+1,sum(heights)+1),'white'); draw=ImageDraw.Draw(image)
    regular='/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'
    bold='/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf'
    y=0
    for ri,r in enumerate(range(first_row,last_row+1)):
        x=0
        for ci,c in enumerate(range(first_col,last_col+1)):
            cell=sheet.cell(r,c); width=widths[ci]; height=heights[ri]
            fill=cell.fill.fgColor
            color=('#'+fill.rgb[-6:]) if fill.type=='rgb' and cell.fill.patternType=='solid' else '#ffffff'
            draw.rectangle((x,y,x+width,y+height),fill=color,outline='#d9ded6')
            font=ImageFont.truetype(bold if cell.font.bold else regular,max(10,round((cell.font.sz or 11)*4/3)))
            text=str(cell.value or '')
            lines=[]
            for line in text.split('\n'):
                if cell.alignment.wrap_text:
                    current=''
                    for word in line.split():
                        while font.getlength(word)>width-10:
                            if current:lines.append(current); current=''
                            cut=1
                            while cut<len(word) and font.getlength(word[:cut+1])<=width-10:cut+=1
                            lines.append(word[:cut]);word=word[cut:]
                        proposed=(current+' '+word).strip()
                        if font.getlength(proposed)>width-10 and current:
                            lines.append(current); current=word
                        else: current=proposed
                    lines.append(current)
                else: lines.append(line)
            tile=Image.new('RGB',(width-2,height-2),color); td=ImageDraw.Draw(tile)
            td.multiline_text((4,4),'\n'.join(lines),font=font,fill='#24352f',spacing=2)
            image.paste(tile,(x+1,y+1)); x+=width
        y+=heights[ri]
    image.save(output); wb.close()

def update_workbook(path, datasets):
    wb=load_workbook(path)
    baseline={s.title:{'freeze':s.freeze_panes,'widths':{k:v.width for k,v in s.column_dimensions.items()},
                       'styles':{c.coordinate:copy.copy(c._style) for row in s for c in row},
                       'values':{c.coordinate:c.value for row in s for c in row},
                       'heights':{k:v.height for k,v in s.row_dimensions.items()}}
              for s in wb}
    changed={}; appended={}
    numeric={'size_bytes','sequence','width','height','presentation_rotation_cw','master_member_count'}
    for name,fields,rows in datasets:
        sheet=wb[name]; assert [c.value for c in sheet[1]]==fields
        old_max=sheet.max_row; assert len(rows)+1 >= old_max
        changed[name]=set(); appended[name]=set()
        style_row=2
        for ri,row in enumerate(rows,2):
            for ci,field in enumerate(fields,1):
                value=row[field] or None
                if Path(path).name != 'RESTORE.xlsx' and field in numeric and str(value).isdigit():value=int(value)
                cell=sheet.cell(ri,ci)
                if ri>old_max:
                    cell._style=copy.copy(sheet.cell(style_row,ci)._style)
                    if field in {'final_relative_path','master_relative_path','relative_path','original_filename',
                                 'filename','provenance_note','notes','relationship','provenance_path'}:
                        alignment=copy.copy(cell.alignment); alignment.wrap_text=True; cell.alignment=alignment
                    appended[name].add(cell.coordinate)
                if cell.value != value:
                    cell.value=value
                    if isinstance(value,str):cell.data_type='s'
                    changed[name].add(cell.coordinate)
            if ri>old_max:
                lines=max([1]+[sum(max(1,len(textwrap.wrap(line,width=max(10,int(sheet.column_dimensions[cell.column_letter].width*.9)))))
                          for line in str(cell.value or '').split('\n')) for cell in sheet[ri] if cell.alignment.wrap_text])
                sheet.row_dimensions[ri].height=max(23,lines*14+7)
            if name=='Objects' and row.get('iss_id')==ENTITY:
                alignment=copy.copy(sheet.cell(ri,13).alignment); alignment.wrap_text=True; sheet.cell(ri,13).alignment=alignment
                sheet.row_dimensions[ri].height=max(sheet.row_dimensions[ri].height or 23,112)
        for table in sheet.tables.values():
            table.ref=f'A1:{get_column_letter(len(fields))}{len(rows)+1}'
            if table.autoFilter:table.autoFilter.ref=table.ref
    wb.save(path); wb.close()
    wb=load_workbook(path)
    assert wb.sheetnames==list(baseline)
    for sheet in wb:
        before=baseline[sheet.title]
        assert sheet.freeze_panes==before['freeze']
        assert {k:v.width for k,v in sheet.column_dimensions.items()}==before['widths']
        for cell,style in before['styles'].items():
            if not (sheet.title=='Objects' and cell=='M135'):
                assert sheet[cell]._style==style,(path,sheet.title,cell,'style')
        for cell,value in before['values'].items():
            if cell not in changed.get(sheet.title,set()):assert sheet[cell].value==value
        for row,height in before['heights'].items():
            if not (sheet.title=='Objects' and row==135):assert sheet.row_dimensions[row].height==height
    for name,fields,rows in datasets:
        actual=list(wb[name].values)
        assert len(actual)==len(rows)+1
        for values,row in zip(actual[1:],rows):
            assert [str(v) if v is not None else '' for v in values]==[str(row[f]) for f in fields]
    wb.close()
    return {name:{'changed_cells':len(changed[name]),'appended_cells':len(appended[name])} for name in changed}

def apply():
    evidence,carrier,health=preflight()
    now=datetime.now(timezone.utc).isoformat()
    originals={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm'))}
    dirs={str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}
    source_before=stamp(SOURCE)
    oh,objects=read_csv(U/'metadata/objects.csv'); ch,catalog=read_csv(U/'CATALOG.csv')
    rh,restore=read_csv(U/'db-restore/RESTORE.csv'); ph,reps=read_csv(U/'db-restore/manifests/representations.csv')
    kh,checks=read_csv(U/'db-restore/checksums.csv'); checksum={r['path']:r for r in checks}
    original_photo=[copy.deepcopy(r) for r in catalog if r['record_key']==PHOTO]
    source_metadata=json.loads(subprocess.check_output(['exiftool','-j','-G1','-s','-XMP:All','-IPTC:All',
                '-IFD0:ImageDescription','-IFD0:Artist','-IFD0:Copyright',str(SOURCE)],text=True))[0]
    before_dispositions={}
    for label,path in [('work',W/'source-disposition.planned.csv'),('final',U/'db-restore/manifests/source-disposition.csv')]:
        rows=read_csv(path)[1]; before_dispositions[label]=next(r for r in rows if r['source_path']==str(SOURCE))
    changed=set(); media=[]
    def register(relative):
        assert relative not in changed or relative not in checksum, 'Already hashed in this accession'
        with (U/relative).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        row={'path':relative,'sha256':'sha256:'+digest,'size_bytes':str((U/relative).stat().st_size)}
        checksum[relative]=row; changed.add(relative)
        return digest
    review.mount_guard()
    with SOURCE.open('rb') as src,(U/MASTER).open('xb') as out:shutil.copyfileobj(src,out,1024*1024)
    embedded=embed(U/MASTER,carrier)
    with Image.open(SOURCE) as src,Image.open(U/MASTER) as dst:
        assert src.mode==dst.mode and src.size==dst.size and src.tobytes()==dst.tobytes()
    master_hash=register(MASTER)
    with Image.open(U/MASTER) as master,(U/ACCESS).open('xb') as out:
        ImageOps.exif_transpose(master).convert('RGB').save(out,'JPEG',quality=92,subsampling=0)
    embed(U/ACCESS,carrier)
    access_hash=register(ACCESS)
    decision={'unit':'paragon','authorization':AUTHORIZATION,'actor':'Vladimir','at_utc':now,
              'inventory':INVENTORY,'entity_id':ENTITY,'source_path':str(SOURCE),
              'original_filename':SOURCE.name,'source_sha256':evidence['source_sha256'],
              'source_size_bytes':source_before[0],'source_mtime_ns':source_before[1],
              'selected_master_members':[OVERVIEW,MASTER],'member_labels':['carrier overview (surrogate)',PART],
              'master_representations_for_carrier':1,'coverage_complete':False,
              'master_path':MASTER,'master_sha256':'sha256:'+master_hash,
              'access_path':ACCESS,'access_sha256':'sha256:'+access_hash,
              'width':5024,'height':6960,'source_metadata':source_metadata,'metadata':embedded,
              'note':NOTE,'prior_source_dispositions':before_dispositions,
              'separate_photograph_master_unchanged':True,'accession_performed':True,
              'sources_modified':False,'deletion_authorized':False,'publication_performed':False,
              'files_hashed_after_final_write':[MASTER,ACCESS],'validation_result':'pending'}
    # This readable record allows inspection if a later catalogue export is interrupted.
    write_json(WORK_RECEIPT,decision)
    parent=next(r for r in objects if r['iss_id']==ENTITY)
    parent.update(master_file=OVERVIEW+'\n'+MASTER,access_file=parent['access_file']+'\n'+ACCESS,
                  master_member_count='2',master_parts='carrier overview (surrogate)\n'+PART,
                  representation_summary='access_derivative: 2; preservation_master: 1; preservation_surrogate: 1',
                  notes_for_review=parent['notes_for_review']+'; carrier section accessioned; complete high-resolution coverage unresolved')
    prior_restore=next(r for r in restore if r['iss_id']==ENTITY and r['representation_role']=='preservation_surrogate')
    overview_row=next(r for r in catalog if r['final_relative_path']==OVERVIEW)
    overview_row['relationship']='member of the single selected Master for VLV-1130; part=carrier overview (surrogate)'
    for role,path,digest,master in [('preservation_master',MASTER,master_hash,True),('access_derivative',ACCESS,access_hash,False)]:
        provenance={'source_path':str(SOURCE),'original_filename':SOURCE.name,'source_digest':evidence['source_sha256'],
                    'carrier_inventory':INVENTORY,'entity_id':ENTITY,'representation_part':PART,
                    'master_path':MASTER,'master_digest':'sha256:'+master_hash,
                    'derivation':'metadata_enriched_selected_master' if master else 'generated_from_selected_master',
                    'authorization':AUTHORIZATION,'decision_record':RECEIPT,'deletion_authorized':False}
        note=json.dumps(provenance,ensure_ascii=False)
        mtime=datetime.fromtimestamp((U/path).stat().st_mtime,timezone.utc).isoformat()
        media.append({'path':path,'role':role,'hash':digest,'note':note,'mtime':mtime,'occurrence_id':'OCC-'+uuid4().hex.upper()})
        rr=dict(prior_restore)
        rr.update(representation_role=role,relative_path=path,original_filename=Path(path).name,
                  sha256=digest,size_bytes=str((U/path).stat().st_size),media_type='image/tiff' if master else 'image/jpeg',
                  width_px='5024',height_px='6960',bit_depth='8',color_space='RGB',
                  compression='Uncompressed' if master else 'JPEG',observed_mtime_utc=mtime,provenance_note=note,
                  source_root_id='',source_batch_id='')
        restore.append(rr)
        row={f:'' for f in ch}
        for key in ['record_key','inventory','title','date_text','creator','object_type','material','dimensions',
                    'places','tags','md_object_id','md_url','rights','rights_holder','access_restriction']:
            row[key]=overview_row[key]
        row.update(file_role=role,final_relative_path=path,master_relative_path=MASTER,filename=Path(path).name,
                   file_type='tif' if master else 'jpg',size_bytes=rr['size_bytes'],final_sha256=digest,
                   description=embedded['embedded_description'],metadata_authority='Vladimir: carrier-section assignment and accession; retained carrier metadata',
                   original_source_path=str(SOURCE) if master else str(U/MASTER),
                   source_sha256=evidence['source_sha256'].removeprefix('sha256:') if master else master_hash,
                   derivative_of='' if master else MASTER,width='5024',height='6960',
                   relationship='member of the single selected Master for VLV-1130; part='+PART if master else 'generated access image of the carrier-section Master member',
                   condition='Partial carrier coverage; complete high-resolution coverage unresolved.',
                   notes='Full raw section preserved with carrier VLV-1130; separate photograph Master retained. Source unchanged.',
                   metadata_path='metadata/objects.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/source-disposition.csv')
        catalog.append(row)
    reps.append(dict(zip(ph,[ENTITY,INVENTORY,'preservation_master',MASTER,'sha256:'+master_hash,ACCESS,
                            'sha256:'+access_hash,MASTER,'generated_from_selected_master','local_access',
                            '5024','6960','paragon/accession_carrier_section.py',now])))
    log={'log_id':'LOG-'+uuid4().hex.upper(),'at_utc':now,'actor':'Vladimir','unit_id':'WF-PARAGON',
         'action':'review_source_candidate','entity_type':'reconciliation_decision','entity_id':'SOURCE-CANDIDATE-'+uuid4().hex.upper(),
         'before_json':json.dumps(before_dispositions,ensure_ascii=False),
         'after_json':json.dumps({'entity_id':ENTITY,'evidence_id':str(SOURCE),'content_digest':evidence['source_sha256'],
             'candidate_role':'master_tiff_candidate','source_name':SOURCE.name,'source_path':str(SOURCE),
             'state':'selected_master','selected_master_members':[OVERVIEW,MASTER],
             'source_report':'carrier-section-accession.json','accession_receipt':RECEIPT},ensure_ascii=False),
         'evidence':evidence['source_sha256'],'note':NOTE}
    with connect(U/'db-restore/RESTORE.sqlite',True) as con:
        final_before=file_rows(con,ENTITY); photo_before=file_rows(con,PHOTO)
        for entry in media:
            con.execute('INSERT INTO content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        ('sha256:'+entry['hash'],(U/entry['path']).stat().st_size,'image/tiff' if entry['role']=='preservation_master' else 'image/jpeg',5024,6960,8,'RGB','Uncompressed' if entry['role']=='preservation_master' else 'JPEG',1,now))
            con.execute('INSERT INTO file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (entry['occurrence_id'],'WF-PARAGON','sha256:'+entry['hash'],'archive',entry['path'],Path(entry['path']).name,entry['mtime'],now,'verified',entry['note']))
            con.execute('INSERT INTO entity_files VALUES (?,?,?,?,?)',(ENTITY,entry['occurrence_id'],entry['role'],'active',now))
        con.execute(f'INSERT INTO logbook ({",".join(log)}) VALUES ({",".join("?" for _ in log)})',tuple(log.values()))
        # Annotate the retained overview membership; its file bytes stay unchanged.
        con.execute("UPDATE file_occurrences SET note=note||? WHERE occurrence_kind='archive' AND relative_path=?",
                    ('; representation_part=carrier overview (surrogate); complementary carrier section accessioned; see '+RECEIPT,OVERVIEW))
        con.execute('UPDATE catalog_state SET published_revision=catalog_revision,dirty_since=NULL WHERE singleton=1')
        assert len(file_rows(con,ENTITY))==4 and file_rows(con,PHOTO)==photo_before
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        revision=con.execute('SELECT catalog_revision FROM catalog_state').fetchone()[0]
    con.close()
    with connect(U/'db-restore/RESTORE.sqlite') as con:
        sql='\n'.join(con.iterdump())+'\n'
        in_memory=sqlite3.connect(':memory:'); in_memory.executescript(sql)
        assert in_memory.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not in_memory.execute('PRAGMA foreign_key_check').fetchall(); in_memory.close()
        (U/'db-restore/RESTORE.sql').write_text(sql)
        current_overview_note=con.execute('SELECT note FROM file_occurrences WHERE relative_path=?',(OVERVIEW,)).fetchone()[0]
    con.close()
    for row in restore:
        row['catalog_revision']=str(revision)
        if row['relative_path']==OVERVIEW:row['provenance_note']=current_overview_note
    with review.connect(False) as con:
        con.execute(f'INSERT INTO logbook ({",".join(log)}) VALUES ({",".join("?" for _ in log)})',tuple(log.values()))
    disposition={**before_dispositions['work'],'inventory':INVENTORY,'source_role':'selected_master_candidate',
        'final_path':MASTER,'preservation_status':'preserved_as_metadata_enriched_master',
        'preservation_basis':'Full source pixels preserved in the accessioned carrier-section Master member; carrier assignment and accession explicitly authorized by Vladimir.',
        'final_sha256':'sha256:'+master_hash,'exact_copy_proven':'false',
        'drain_state':'candidate_after_separate_review','deletion_authorized':'false',
        'notes':f'Original filename VLV-1130-5.tif; section of carrier VLV-1130. Not the separately catalogued cropped photograph. Receipt: {RECEIPT}. Decision: {log["log_id"]}. Source retained; no deletion authorized.'}
    for target in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:
        fields,rows=read_csv(target); matching=[i for i,r in enumerate(rows) if r['source_path']==str(SOURCE)]
        assert len(matching)==1; rows[matching[0]]=disposition; write_csv(target,fields,rows)
        if not target.is_relative_to(U):
            write_csv(W/'drain-list.csv',fields,[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
            write_csv(W/'retain-review.csv',fields,[r for r in rows if r['drain_state']!='candidate_after_separate_review'])
    for target in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:
        entries=json.loads(target.read_text()); entry=next(r for r in entries if r['source_path']==str(SOURCE))
        entry.update(accession_receipt=RECEIPT,carrier_inventory=INVENTORY,final_path=MASTER,final_sha256='sha256:'+master_hash,accession_decision_log_id=log['log_id'])
        write_json(target,entries)
    datasets=[('metadata/objects.csv',oh,objects),('CATALOG.csv',ch,catalog),
              ('db-restore/RESTORE.csv',rh,restore),('db-restore/manifests/representations.csv',ph,reps)]
    for path,fields,rows in datasets:write_csv(U/path,fields,rows)
    workbook_checks={}
    workbook_checks['CATALOG.xlsx']=update_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)])
    workbook_checks['db-restore/RESTORE.xlsx']=update_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])
    # Keep the focused comparison useful after accession, without adding decision actions.
    report=json.loads(review.REPORT.read_text()); case=next(o for o in report['objects'] if o['inventory']=='VLV-1130-5')
    case['why']=NOTE+' Accession is complete; the original source is retained.'
    case['accession']={'inventory':INVENTORY,'master_path':MASTER,'access_path':ACCESS,'receipt':RECEIPT,'completed_at':now}
    write_json(review.REPORT,report)
    with review.connect() as con:review.export_decisions(con)
    # Update current summary counts, preserving historical reports and unrelated decisions.
    current=read_csv(W/'source-disposition.planned.csv')[1]
    work_summary=json.loads((W/'drain-review-summary.json').read_text())
    candidates=[r for r in current if r['drain_state']=='candidate_after_separate_review']
    retained=[r for r in current if r['drain_state']!='candidate_after_separate_review']
    work_summary.update(candidate_rows=len(candidates),retain_rows=len(retained),
        candidate_source_bytes=sum(int(r['source_size_bytes']) for r in candidates),
        retained_source_bytes=sum(int(r['source_size_bytes']) for r in retained),
        preservation_status=dict(Counter(r['preservation_status'] for r in current)),
        source_roles=dict(Counter(r['source_role'] for r in current)),
        carrier_section_accession={'receipt':RECEIPT,'source_path':str(SOURCE),'inventory':INVENTORY,'at_utc':now})
    work_summary['current_register']='source-disposition.planned.csv (work decisions; final register updated only for the accessioned VLV-1130 section)'
    work_summary['installed_final_checkpoint']['carrier_section_accession']=RECEIPT
    write_json(W/'drain-review-summary.json',work_summary)
    readme=(U/'00_README.md').read_text(); title,body=readme.split('\n',1)
    intro=f'\n\n## Carrier section accession — 2026-09-17\n\n{NOTE}\n\nNew member: `{MASTER}`. Its access JPEG is `{ACCESS}`.\nVLV-1130 now has one selected Master with two complementary file members.\nThe unit contains 239 represented objects with 242 Master members and 242 access files.\nReceipt and source metadata: `{RECEIPT}`. Source files remain unchanged;\nno publication or deletion occurred. Earlier receipts below describe their own checkpoints.\n'
    (U/'00_README.md').write_text(title+intro+body)
    fields_path=U/'metadata/catalog-fields.md'; text=fields_path.read_text()
    text=text.replace('master_member_count is 0, 1 or 2, not the','master_member_count counts file members, not the')
    text+='\nCurrent addition (2026-09-17): VLV-1130 has two complementary members: its retained\noverview surrogate and the accessioned full section scan from VLV-1130-5.tif.\nThe section has one accession-generated JPEG. Its source name does not assign it\nto the separate photograph object. See '+RECEIPT+'.\n'
    fields_path.write_text(text)
    controls=[p for p,_,_ in datasets]+['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql',
              'CATALOG.xlsx','db-restore/RESTORE.xlsx','db-restore/manifests/source-disposition.csv',
              'db-restore/manifests/drain-evidence.json','00_README.md','metadata/catalog-fields.md']
    for path in controls:register(path)
    export=json.loads((U/'catalog-export-summary.json').read_text())
    export.update(updated_at=now,file_rows=len(catalog),object_rows=len(objects),
        carrier_section_accession={'inventory':INVENTORY,'members_added':1,'new_derivatives':1,'receipt':RECEIPT,'deletion_authorized':False})
    for value in export['outputs'].values():value['sha256']=checksum[str(Path(value['path']).relative_to(U))]['sha256']
    write_json(U/'catalog-export-summary.json',export); register('catalog-export-summary.json')
    summary=json.loads((U/'db-restore/export-summary.json').read_text())
    summary.update(updated_at=now,masters=len(reps),access=len(reps),
        catalog_state={'catalog_revision':revision,'published_revision':revision,'dirty_since':None},
        carrier_section_accession={'inventory':INVENTORY,'generated_access':1,'receipt':RECEIPT,'deletion_authorized':False})
    summary['restore']['rows']=len(restore)
    summary['checksums']=len(checksum)+(0 if 'db-restore/export-summary.json' in checksum else 1)+1
    write_json(U/'db-restore/export-summary.json',summary); register('db-restore/export-summary.json')
    assert {str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()}==dirs
    for path,prior in originals.items():
        if path not in changed and path!='db-restore/checksums.csv':assert stamp(U/path)==prior,('Unrelated file changed',path)
    assert [r for r in catalog if r['record_key']==PHOTO]==original_photo
    assert stamp(SOURCE)==source_before
    review.mount_guard(); after=review.check()
    assert after['saved_reviews']==health['saved_reviews'] and after['decisions_agree']
    assert len(catalog)==495 and len(restore)==485 and len(reps)==242
    assert len(objects)==240 and sum(bool(r['master_file']) for r in objects)==239
    assert sum(int(r['master_member_count']) for r in objects)==242
    decision.update(validation_result='ok',completed_at=datetime.now(timezone.utc).isoformat(),
        saved_reviews_preserved=health['saved_reviews'],catalog_rows=len(catalog),restore_rows=len(restore),
        represented_objects=239,master_representations=239,master_members=242,new_master_members=1,new_derivatives=1,
        catalog_revision=revision,decision_log_id=log['log_id'],workbook_checks=workbook_checks,
        sql_restore_verified=True,source_stamp_unchanged=True,directory_structure_unchanged=True,
        existing_media_unchanged=True)
    write_json(WORK_RECEIPT,decision); write_json(U/RECEIPT,decision); register(RECEIPT)
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checksum.values(),key=lambda r:r['path']))
    assert summary['checksums']==len(checksum)
    for row in checksum.values():assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    print(json.dumps({k:decision[k] for k in ['validation_result','inventory','master_path','master_members','new_derivatives','saved_reviews_preserved','sources_modified','deletion_authorized']},indent=2),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--apply',action='store_true'); parser.add_argument('--preview',action='store_true')
    args=parser.parse_args()
    if args.preview:
        render_range(U/'CATALOG.xlsx','Objects',135,135,13,19,W/'carrier-section-objects.png')
        render_range(U/'CATALOG.xlsx','Catalog',258,259,1,6,W/'carrier-section-catalog.png')
        render_range(U/'db-restore/RESTORE.xlsx','Catalog',271,272,22,29,W/'carrier-section-restore.png')
    elif args.apply:apply()
    else:
        evidence,carrier,health=preflight()
        print(json.dumps({'ready':True,'source':str(SOURCE),'inventory':INVENTORY,'selected_master_members':[OVERVIEW,MASTER],
                          'source_hash_reused':evidence['source_sha256'],'saved_reviews':health['saved_reviews'],
                          'source_unchanged':True,'new_directories':0,'new_schema':False},indent=2))
