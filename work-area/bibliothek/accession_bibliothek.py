#!/usr/bin/env python3
"""Accession the three explicitly selected Bibliothek MD representations.

Default/--check is read-only. --execute refuses an existing target. Original
bytes and decisions are retained; known wrong MD content stays explicitly named.
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
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps
from prepare_bibliothek import ROOT, SOURCE, guard, read_csv, csv_bytes, now
from workbench_bibliothek import Review

TARGET = Path('/mnt/volume/iss-archive/units/bibliothek')
DIRS = ('00_MANIFESTS', 'masters', 'export', 'metadata', 'documents', 'db-restore')
AUTHORIZATION = 'ok make an accession'
BUNDLED_PYTHON = '/home/vladimir/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python'
MISMATCH = ('Known mismatch: selected MD resource 106189 is an unrelated newspaper page, '
            'not a scan of the Urbanitzky book. The local five-page book scans are retained '
            'at source and their content is not preserved by this accession.')
FIELDS = ('relative_path,object_key,file_role,title,content_note,description,md_object_id,'
          'selected_candidate_id,sequence,date,creator,book_title,content_match_status,pages,'
          'rights,owner,object_url,source_path,source_url,source_size_bytes,source_mtime_ns,'
          'source_sha256,master_relative_path,metadata_path,width,height,size_bytes,final_sha256').split(',')
EVIDENCE = {
    'objects.csv':'metadata/md-objects.csv',
    'candidates.csv':'metadata/candidates.csv',
    'md-resources.csv':'metadata/md-resources.csv',
    'md-export.json':'metadata/md-export.json',
    'md-request.json':'metadata/md-request.json',
    'local-evidence.json':'metadata/local-evidence.json',
    'search-summary.json':'metadata/search-summary.json',
    'urbanitzky-md-attachment-check.json':'metadata/urbanitzky-md-attachment-check.json',
    'master-decisions.csv':'00_MANIFESTS/master-decisions.csv',
    'candidate-dispositions.csv':'00_MANIFESTS/candidate-dispositions.csv',
    'master-confirmation-receipt.json':'00_MANIFESTS/master-confirmation-receipt.json',
}
BOOKS = {
    'BIB-1001':('1924','Wilhelm Beck','Cover photograph and four scanned spreads of front matter, contents and opening text; not a full book scan.'),
    'BIB-1002':('1885','Alfred von Urbanitzky','One unrelated newspaper page headed "Das interessiert nicht nur die Jugend", with Kulturhaus programming.'),
    'BIB-1003':('1898','Arthur Wilke','Three scanned spreads of the title page and contents; not a full book scan.'),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(path):
    st = Path(path).stat()
    return [st.st_size, st.st_mtime_ns]


def jb(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def row(**values):
    require(set(values) <= set(FIELDS), 'Unknown catalogue field')
    return {field: str(values.get(field, '')) for field in FIELDS}


def path_for(relative):
    p = Path(relative)
    require(not p.is_absolute() and '..' not in p.parts, 'Unsafe output path')
    require((len(p.parts)==1 and p.name in {'00_README.md','CATALOG.csv','CATALOG.xlsx'})
            or (len(p.parts)==2 and p.parts[0] in DIRS), 'Unapproved output nesting')
    return TARGET / p


def assert_inputs(plan):
    guard()
    for name, expected in plan['input_stamps'].items():
        p=Path(name)
        require(not p.is_symlink() and p.resolve(strict=True)==p, 'Input is not its recorded path')
        require(stamp(p)==expected, 'Input changed: '+name)


def preflight(allow_existing=False):
    review=Review()
    check=review.check()
    require(check['objects']==check['resolved_objects']==3, 'Exactly three resolved objects required')
    require(TARGET.parent.resolve(strict=True)==Path('/mnt/volume/iss-archive/units'), 'Archive root differs')
    if allow_existing:
        require(TARGET.is_dir() and not TARGET.is_symlink(), 'Expected interrupted unit is missing')
    else:
        require(not TARGET.exists() and not TARGET.is_symlink(), 'Target already exists; refusing overwrite')
    decisions=review.decisions()
    require(all(decisions[k]['selected_candidate_id']==k+':md' for k in BOOKS), 'MD selections changed')
    confirmation=json.loads((ROOT/'master-confirmation-receipt.json').read_text())
    require(confirmation['user_request']=='ok accept all md masters', 'Missing explicit MD Master acceptance')
    require(confirmation['selected_masters']==list(decisions.values()), 'Confirmation differs from saved decisions')
    inputs={}
    for c in review.candidates:
        for m in json.loads(c['members_json']):
            p=review.checked_path(m)
            inputs[str(p)]=[int(m['size_bytes']),int(m['mtime_ns'])]
    evidence=dict(EVIDENCE)
    for c in review.candidates:
        for m in json.loads(c['members_json']):
            p=Path(m['technical_path'])
            require(p.parent==ROOT, 'Technical evidence outside work root')
            evidence[p.name]='metadata/'+p.name
    for name in [*evidence,'accession_bibliothek.py','catalog_bibliothek.py']:
        inputs[str(ROOT/name)]=stamp(ROOT/name)
    masters=[]
    objects=[]
    for o in review.objects:
        key=o['object_key']
        wrong=key=='BIB-1002'
        resource_rows=sorted([r for r in review.resources if r['object_key']==key],key=lambda r:int(r['sequence']))
        c=review.by_candidate[key+':md']
        members=json.loads(c['members_json'])
        require([m['path'] for m in members]==[r['work_path'] for r in resource_rows], 'Selected MD member order differs')
        paths=[]
        for r in resource_rows:
            relative='masters/'+Path(r['work_path']).name
            paths.append(relative)
            technical=json.loads(Path(r['technical_path']).read_text())
            title=('MD attachment 106189: unrelated newspaper page' if wrong else o['title'])
            description=(BOOKS[key][2] if r['type']=='text' else 'Cover photograph of the catalogued Beck book.')
            masters.append(row(relative_path=relative,object_key=key,file_role='master_member',title=title,
                content_note=MISMATCH if wrong else 'Partial representation; physical book extent is not digital scan extent.',
                description=description,md_object_id=o['md_object_id'],selected_candidate_id=key+':md',
                sequence=r['sequence'],date='' if wrong else BOOKS[key][0],creator='' if wrong else BOOKS[key][1],
                book_title=o['title'],content_match_status='ambiguous' if wrong else 'matched',pages=r['pages'],
                rights=r['rights'],owner=r['owner'],object_url=o['object_url'],source_path=r['work_path'],
                source_url=r['source_url'],source_size_bytes=r['size_bytes'],source_mtime_ns=r['mtime_ns'],
                source_sha256=r['sha256'],metadata_path='metadata/objects.jsonl',
                width=technical.get('width',''),height=technical.get('height','')))
        objects.append(dict(object_key=key,md_object_id=o['md_object_id'],title=o['title'],
            catalogued_book_date=BOOKS[key][0],catalogued_book_creator=BOOKS[key][1],
            original_md_description=o['description'],object_url=o['object_url'],
            selected_candidate_id=key+':md',ordered_master_paths=json.dumps(paths),
            representation_extent=BOOKS[key][2],content_match_status='ambiguous' if wrong else 'matched',
            content_note=MISMATCH if wrong else 'Partial scan only; no full-book completeness claim.',
            metadata_resolution=('Keep the MD book identity separate from the observed newspaper attachment. '
                'The MD description also contains unrelated brigade-diary passages; retain it verbatim as source evidence. '
                'The MD print event names Deutsche Sporthalle; retain that source claim without adopting it as publication-place metadata. '
                'Do not attribute book title, author or year to the newspaper file.' if wrong else
                'MD book metadata retained; observed digital extent recorded separately. Original file bytes unchanged.'),
            actor=decisions[key]['actor'],master_decided_at=decisions[key]['decided_at'],
            decision_note=decisions[key]['note'],deletion_authorized='false'))
    require(len(masters)==4 and len({r['relative_path'] for r in masters})==4, 'Four unique selected files required')
    require(shutil.disk_usage(TARGET.parent).free>100_000_000, 'Insufficient space')
    require(all(shutil.which(x) for x in ['pdftoppm','pdfinfo',BUNDLED_PYTHON]), 'Accession tool missing')
    plan=dict(input_stamps=inputs,objects=objects,masters=masters,evidence=evidence,
        candidates=review.candidates,dispositions=read_csv('candidate-dispositions.csv'))
    assert_inputs(plan)
    return plan


def write_new(relative,payload):
    with path_for(relative).open('xb') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def copy_new(source,relative):
    with Path(source).open('rb') as src,path_for(relative).open('xb') as dst:
        shutil.copyfileobj(src,dst,1024*1024)
        dst.flush()
        os.fsync(dst.fileno())


def xlsx_matches(csv_rows):
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    matrix=[list(csv_rows[0])]+[list(r.values()) for r in csv_rows]
    with zipfile.ZipFile(path_for('CATALOG.xlsx')) as z:
        require(z.testzip() is None,'XLSX damaged')
        shared=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            shared=[''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        tree=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        require(not tree.findall('.//s:f',ns),'Unexpected catalogue formula')
        actual=[['']*len(matrix[0]) for _ in matrix]
        for r in tree.findall('s:sheetData/s:row',ns):
            rn=int(r.attrib['r'])-1
            for c in r:
                col=re.match('[A-Z]+',c.attrib['r'])[0]
                cn=0
                for ch in col: cn=cn*26+ord(ch)-64
                require(rn<len(actual) and cn<=len(matrix[0]),'Unexpected XLSX cell')
                v=c.find('s:v',ns)
                value=v.text or '' if v is not None else ''
                if c.attrib.get('t')=='s': value=shared[int(value)]
                elif c.attrib.get('t')=='inlineStr':
                    inline=c.find('s:is',ns)
                    value=''.join(inline.itertext()) if inline is not None else ''
                actual[rn][cn-1]=value
        require(actual==matrix,'XLSX values differ from CSV')
    return True


def execute(authorization):
    require(authorization==AUTHORIZATION,'Exact current accession authorization required')
    plan=preflight()
    started=now()
    # First archive mutation: one approved unit and the six standard folders.
    TARGET.mkdir(exist_ok=False)
    for name in DIRS: (TARGET/name).mkdir(exist_ok=False)
    catalog=[]
    checksums=[]

    def register(r):
        p=path_for(r['relative_path'])
        with p.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        size,mtime=stamp(p)
        final=dict(r,size_bytes=str(size),final_sha256=digest)
        catalog.append(final)
        checksums.append(dict(relative_path=r['relative_path'],size_bytes=str(size),mtime_ns=str(mtime),sha256=digest))
        if r['source_sha256']: require(digest==r['source_sha256'],'Copied payload hash differs: '+r['relative_path'])
        return final

    for r in plan['masters']:
        copy_new(r['source_path'],r['relative_path'])
        p=path_for(r['relative_path'])
        if p.suffix=='.pdf':
            info=subprocess.check_output(['pdfinfo',str(p)],text=True,timeout=30)
            require(int(re.search(r'^Pages:\s+(\d+)',info,re.M)[1])==int(r['pages']),'PDF page count changed')
        else:
            with Image.open(p) as im:
                im.load()
                require(im.format=='JPEG' and im.size==(int(r['width']),int(r['height'])),'Master image changed')
        register(r)
    print('Copied and checked four Master files against their stored hashes.',flush=True)
    for m in list(catalog):
        master=path_for(m['relative_path'])
        relative='export/'+master.stem+'-preview.jpg'
        target=path_for(relative)
        if master.suffix=='.pdf':
            with target.open('xb') as out:
                subprocess.run(['pdftoppm','-f','1','-l','1','-singlefile','-scale-to','1600','-jpeg',
                    '-jpegopt','quality=85',str(master)],stdout=out,stderr=subprocess.PIPE,
                    timeout=60,check=True,env={**os.environ,'OMP_THREAD_LIMIT':'1'})
        else:
            with Image.open(master) as source:
                im=ImageOps.exif_transpose(source)
                im.thumbnail((1600,1600))
                with target.open('xb') as out: im.convert('RGB').save(out,'JPEG',quality=85)
        with Image.open(target) as im:
            im.load()
            require(im.format=='JPEG' and 0<max(im.size)<=1600,'Invalid accession preview')
            width,height=im.size
        r=dict(m,relative_path=relative,file_role='access_preview',
            description='Page 1 preview of the selected Master member.' if master.suffix=='.pdf' else 'Preview of selected cover photograph.',
            source_path=str(master),source_url='',source_size_bytes=m['size_bytes'],source_mtime_ns=str(stamp(master)[1]),
            source_sha256='',master_relative_path=m['relative_path'],pages='1',width=str(width),height=str(height),
            size_bytes='',final_sha256='')
        final=register(r)
        # This records derivation, not byte identity with the preview.
        final['source_sha256']=m['final_sha256']
    print('Generated and decoded four previews from the accessioned Masters.',flush=True)
    for name,relative in plan['evidence'].items():
        source=ROOT/name
        copy_new(source,relative)
        require(source.read_bytes()==path_for(relative).read_bytes(),'Evidence copy differs')
        register(row(relative_path=relative,file_role='reconciliation_evidence',title=name,
            description='Retained Bibliothek reconciliation evidence; historical status fields describe the original checkpoint.',
            source_path=str(source),source_size_bytes=plan['input_stamps'][str(source)][0],
            source_mtime_ns=plan['input_stamps'][str(source)][1]))
    write_new('metadata/objects.jsonl',''.join(json.dumps(o,ensure_ascii=False)+'\n' for o in plan['objects']).encode())
    register(row(relative_path='metadata/objects.jsonl',file_role='object_metadata',title='Bibliothek object metadata',
        description='One selected representation per object, physical-book metadata, observed file content, and explicit conflicts.'))
    masters=[r for r in catalog if r['file_role']=='master_member']
    previews=[r for r in catalog if r['file_role']=='access_preview']
    by_source={r['source_path']:r for r in masters}
    by_hash={r['final_sha256']:r for r in masters}
    sources={}
    for c in plan['candidates']:
        for m in json.loads(c['members_json']):
            p=m['path']
            if p in sources:
                sources[p]['candidate_ids']=','.join(sorted(set(sources[p]['candidate_ids'].split(','))|{c['candidate_id']}))
                continue
            final=by_source.get(p) or by_hash.get(m['sha256'])
            selected=p in by_source
            sources[p]=dict(source_path=p,source_kind='md_work_capture' if Path(p).is_relative_to(ROOT) else 'protected_usb',
                unit='bibliothek',inventory=c['object_key'],source_role='selected_master_member' if selected else 'redundant_candidate',
                final_path=final['relative_path'] if final else '',
                preservation_status='preserved_exact_copy' if final else 'not_preserved_content_mismatch',
                preservation_basis='Stored source hash equals final Master hash; source size and mtime unchanged.' if final else MISMATCH,
                source_sha256=m['sha256'],final_sha256=final['final_sha256'] if final else '',
                source_size_bytes=str(m['size_bytes']),source_mtime_ns=str(m['mtime_ns']),
                exact_copy_proven='true' if final else 'false',drain_state='retained' if final else 'retain_required',
                deletion_authorized='false',candidate_ids=c['candidate_id'],
                notes=('Selected file retained with known content mismatch; exact bytes do not establish book-content coverage.' if c['object_key']=='BIB-1002' and selected else
                    'Selected Master member; source retained.' if selected else
                    'Redundant representation decision does not authorize deletion; source retained.'))
    source_rows=list(sources.values())
    require(len(source_rows)==12 and sum(r['exact_copy_proven']=='true' for r in source_rows)==9,'Unexpected source coverage')
    require(sum(r['disposition']=='redundant' for r in plan['dispositions'])==8,'Redundant count differs')
    assert_inputs(plan)
    for relative, rows in [('00_MANIFESTS/master-representations.csv',plan['objects']),
        ('00_MANIFESTS/master-files.csv',masters),('00_MANIFESTS/derivatives.csv',previews),
        ('00_MANIFESTS/source-disposition.csv',source_rows),('00_MANIFESTS/payload-checksums.csv',checksums),
        ('CATALOG.csv',catalog)]: write_new(relative,csv_bytes(rows))
    # The same CSV values become the mandatory Excel view. No extra database or service.
    subprocess.run([BUNDLED_PYTHON,'-B',str(ROOT/'catalog_bibliothek.py'),str(path_for('CATALOG.csv')),
        str(path_for('CATALOG.xlsx')),str(ROOT/'catalog-preview.png')],check=True,timeout=120)
    finish(plan,catalog,checksums,source_rows,started,authorization)


def finish(plan,catalog,checksums,source_rows,started,authorization):
    masters=[r for r in catalog if r['file_role']=='master_member']
    previews=[r for r in catalog if r['file_role']=='access_preview']
    xlsx_matches(catalog)
    tables=[('objects',plan['objects'],'object_key'),('files',catalog,'relative_path'),
        ('source_disposition',source_rows,'source_path'),('candidate_dispositions',plan['dispositions'],'candidate_id')]
    db=sqlite3.connect(path_for('db-restore/catalog.sqlite'))
    try:
        with db:
            for name,rows,primary in tables:
                fields=list(rows[0])
                columns=','.join('"'+f+'" TEXT'+(' PRIMARY KEY' if f==primary else '') for f in fields)
                db.execute(f'CREATE TABLE {name} ({columns})')
                values=[tuple(str(r[f]) for f in fields) for r in rows]
                db.executemany(f'INSERT INTO {name} VALUES ({",".join("?" for _ in fields)})',values)
                require(db.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()==values,'SQLite differs from readable '+name)
            require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','SQLite integrity failure')
    finally: db.close()
    write_new('db-restore/README.md',b'''# Rebuildable Bibliothek catalogue

The filesystem and readable records are primary. catalog.sqlite has four simple
TEXT-column tables, with these sources and primary keys:

- objects: ../00_MANIFESTS/master-representations.csv; object_key
- files: ../CATALOG.csv; relative_path
- source_disposition: ../00_MANIFESTS/source-disposition.csv; source_path
- candidate_dispositions: ../00_MANIFESTS/candidate-dispositions.csv; candidate_id

Rebuild with Python csv.DictReader and sqlite3 parameterized inserts, using CSV
column names/order and literal values including empty strings. CATALOG.xlsx is
the same file catalogue. Existing BIB inventory numbers and MD IDs are retained.
No service, external schema or identifier registry is required.
''')
    write_new('documents/CONTENTS.md',b'''# Bibliothek supporting documents

No separate supporting documents were approved for this accession. Selected MD
resources are in masters/; reconciliation evidence is in metadata/ and
00_MANIFESTS/. Four additional local book leads remain outside the accession
scope, with their paths and search evidence retained in metadata/local-evidence.json.
''')
    write_new('00_README.md',f'''# Bibliothek

Accession: {started}. Vladimir authorized: "{authorization}" after explicitly
selecting all three MD Master representations. Initial scope: MD collection 556,
records 49999, 50002 and 50003. Bibliothek can grow locally after further review;
additional books do not require MD records. Four discovered growth leads are
outside this accession, recorded in metadata/local-evidence.json.

## Selected representations

- BIB-1001 / Beck: one ordered Master representation, comprising the cover JPG
  and a four-page PDF of scanned spreads. This is a partial representation.
- BIB-1002 / Urbanitzky: one selected MD PDF, resource 106189. {MISMATCH}
- BIB-1003 / Wilke: one selected MD PDF of three scanned spreads of title and
  contents. This is a partial representation.

The eight alternative representations are redundant by the saved human
decision. Their source files remain retained. Redundant does not mean safe to
delete. Nine of the 12 candidate source paths have exact byte coverage in the
selected Masters; the three correct local Urbanitzky PDFs have no content
coverage in this accession and must remain at source.

## Metadata and known gaps

MD capture and full descriptions are preserved verbatim. The catalogue separates
physical-book identity from observed digital content and scan extent. For
Urbanitzky, the book title, author and year are not attributed to the newspaper
file. Its MD description also contains unrelated brigade-diary passages and its
print event names Deutsche Sporthalle. These source claims are retained without
being adopted as facts about the attachment. The MD link and selected file are
unchanged; their identity conflict remains explicit. See metadata/objects.jsonl
and 00_MANIFESTS/master-representations.csv. Accession preserves the user-selected
MD representation; it does not repair the MD catalogue or preserve the missing
Urbanitzky book scan.

Master bytes were copied unchanged; metadata accompanies them in the catalogues
and metadata/. The four export/ JPEG previews were generated during accession
from the selected Master members. PDF previews show page 1 only. Rights labels
are recorded exactly as captured; absent licence versions were not invented.

## Files and recovery

CATALOG.csv and CATALOG.xlsx contain the same {len(catalog)} payload rows. The
SQLite catalogue in db-restore/ is rebuildable from readable CSV records. The
source-disposition register lists all 12 candidate paths; all deletion flags are
false. documents/CONTENTS.md states the supporting-document scope.

00_MANIFESTS/payload-checksums.csv records one final SHA-256 per payload after
its last write. Catalogue/manifest/README/database/completion controls are outside
that payload list to avoid circular checksums. Original stored hashes were
reused for unchanged sources; selected copies were checked against those hashes.

Historical evidence, including the Master-confirmation receipt, describes the
checkpoint when it was saved. 00_MANIFESTS/accession-completion.json records this
accession's outcome. Source files and other archive units were not changed.
No publication, source deletion or sealing occurred.
'''.encode())
    assert_inputs(plan)
    for r in checksums:
        require(stamp(path_for(r['relative_path']))==[int(r['size_bytes']),int(r['mtime_ns'])],'Final payload changed after hashing')
    actual_dirs={str(p.relative_to(TARGET)) for p in TARGET.rglob('*') if p.is_dir()}
    require(actual_dirs==set(DIRS),'Unexpected final directory structure')
    controls={'00_README.md','CATALOG.csv','CATALOG.xlsx','documents/CONTENTS.md','db-restore/README.md',
        'db-restore/catalog.sqlite','00_MANIFESTS/master-representations.csv','00_MANIFESTS/master-files.csv',
        '00_MANIFESTS/derivatives.csv','00_MANIFESTS/source-disposition.csv','00_MANIFESTS/payload-checksums.csv'}
    actual={str(p.relative_to(TARGET)) for p in TARGET.rglob('*') if p.is_file()}
    require(actual=={r['relative_path'] for r in catalog}|controls,'Unexpected final file census')
    receipt=dict(unit='bibliothek',collection='556',target=str(TARGET),actor='Vladimir',
        user_authorization=authorization,master_authorization='ok accept all md masters',started_at=started,completed_at=now(),
        accession_performed=True,validation_result='ok',objects=3,master_representations=3,master_files=4,
        pdf_master_pages=8,derivatives_generated=4,redundant_candidates=8,source_paths=12,
        exact_copy_source_paths=9,unpreserved_local_urbanitzky_paths=3,payload_files=len(catalog),
        final_files=len(actual)+1,known_content_mismatch=MISMATCH,
        BIB_1002_book_content_preserved=False,BIB_1002_attachment_identity_conflict_resolved=False,
        metadata_handling='Source claims preserved; book identity and observed attachment content explicitly separated.',
        source_mount=guard(),sources_modified=False,deletion_authorized=False,publication_performed=False,sealing_performed=False,
        checks=['three saved MD selections and complete member order','exact protected source mount',
            'source sizes and mtimes unchanged','copied Master hashes match stored hashes','PDF extent and JPEG decode',
            'four previews from accessioned Masters','CSV/XLSX literal value equality','SQLite/CSV equality and integrity',
            'source coverage does not claim preservation of unrelated book content','approved directory and file census',
            'final payload stamps unchanged after one final hash pass'])
    write_new('00_MANIFESTS/accession-completion.json',jb(receipt))
    with (ROOT/'accession-completion.json').open('xb') as f: f.write(jb(receipt))
    print(json.dumps(receipt,ensure_ascii=False,indent=2),flush=True)


def resume(authorization):
    """Finish this exact interruption after XLSX export; never recopy or rehash."""
    require(authorization==AUTHORIZATION,'Exact current accession authorization required')
    plan=preflight(allow_existing=True)
    def read(relative):
        with path_for(relative).open(newline='') as f:return list(csv.DictReader(f))
    catalog=read('CATALOG.csv')
    checksums=read('00_MANIFESTS/payload-checksums.csv')
    sources=read('00_MANIFESTS/source-disposition.csv')
    expected={r['relative_path'] for r in catalog}|{'CATALOG.csv','CATALOG.xlsx',
        '00_MANIFESTS/master-representations.csv','00_MANIFESTS/master-files.csv',
        '00_MANIFESTS/derivatives.csv','00_MANIFESTS/source-disposition.csv','00_MANIFESTS/payload-checksums.csv'}
    actual={str(p.relative_to(TARGET)) for p in TARGET.rglob('*') if p.is_file()}
    require(actual==expected,'Interrupted output is not the expected post-XLSX checkpoint')
    require(len(catalog)==len(checksums)==27,'Interrupted payload count differs')
    require({r['relative_path'] for r in catalog}=={r['relative_path'] for r in checksums},'Payload checksum mapping differs')
    require(read('00_MANIFESTS/master-representations.csv')==plan['objects'],'Saved object metadata differs')
    by_path={r['relative_path']:r for r in catalog}
    for r in checksums:
        final=by_path[r['relative_path']]
        require((r['sha256'],r['size_bytes'])==(final['final_sha256'],final['size_bytes']),'Saved hash records differ')
        require(stamp(path_for(r['relative_path']))==[int(r['size_bytes']),int(r['mtime_ns'])], 'Final payload changed since hashing')
    for r in plan['masters']:
        saved=by_path[r['relative_path']]
        require(all(saved[k]==v for k,v in r.items() if k not in {'size_bytes','final_sha256'}),'Saved Master record differs')
        require(saved['final_sha256']==r['source_sha256'],'Final Master does not match stored source hash')
    for name,relative in plan['evidence'].items():
        require(path_for(relative).read_bytes()==(ROOT/name).read_bytes(),'Work evidence changed after its copy')
    generated=''.join(json.dumps(o,ensure_ascii=False)+'\n' for o in plan['objects']).encode()
    require(path_for('metadata/objects.jsonl').read_bytes()==generated,'Object metadata JSONL differs')
    require(read('00_MANIFESTS/master-files.csv')==[r for r in catalog if r['file_role']=='master_member'],'Master manifest differs')
    require(read('00_MANIFESTS/derivatives.csv')==[r for r in catalog if r['file_role']=='access_preview'],'Derivative manifest differs')
    require(len(sources)==12 and all(r['deletion_authorized']=='false' for r in sources),'Source register differs')
    require(sum(r['exact_copy_proven']=='true' for r in sources)==9,'Source coverage differs')
    assert_inputs(plan)
    from datetime import datetime,timezone
    started=datetime.fromtimestamp(min(int(r['mtime_ns']) for r in checksums)/1e9,timezone.utc).isoformat()
    print('Resuming remaining catalogue controls; 27 finalized payload stamps and stored hashes reused.',flush=True)
    finish(plan,catalog,checksums,sources,started,authorization)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--authorization',default='')
    args=parser.parse_args()
    if args.resume: resume(args.authorization)
    elif args.execute: execute(args.authorization)
    else:
        p=preflight()
        print(json.dumps(dict(result='ok',target=str(TARGET),objects=len(p['objects']),
            master_files=len(p['masters']),redundant_candidates=8,evidence_files=len(p['evidence']),
            known_content_mismatch=MISMATCH,accession_performed=False,deletion_authorized=False),indent=2))
