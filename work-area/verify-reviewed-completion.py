#!/usr/bin/env python3
"""Read-only verification of the two completed bounded accession updates."""
import csv,json,sqlite3,sys,subprocess
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
from openpyxl import load_workbook
ROOT=Path('/mnt/volume/iss-archive/units'); WORK=Path('/mnt/volume/iss-archive-work')
def rows(path):
 with path.open(newline='') as f:return list(csv.DictReader(f))
def workbook(path,mapping):
 wb=load_workbook(path)
 for name,p in mapping.items():
  with p.open(newline='') as f:
   reader=csv.reader(f); expected=list(reader)
  actual=[[str(c.value) if c.value is not None else '' for c in r] for r in wb[name]]
  assert actual==expected,(path,name)

def bibliothek():
 u=ROOT/'bibliothek'; files=rows(u/'db-restore/files.csv'); cat=rows(u/'CATALOG.csv'); objects=rows(u/'00_MANIFESTS/master-representations.csv')
 db=sqlite3.connect((u/'db-restore/catalog.sqlite').as_uri()+'?mode=ro',uri=True)
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok' and not db.execute('pragma foreign_key_check').fetchall()
 for t,p in [('objects','00_MANIFESTS/master-representations.csv'),('files','db-restore/files.csv'),('source_disposition','00_MANIFESTS/source-disposition.csv'),('candidate_dispositions','00_MANIFESTS/candidate-dispositions.csv')]:
  expected=rows(u/p); assert db.execute('select * from '+t).fetchall()==[tuple(r.values()) for r in expected]
 assert objects==[json.loads(x) for x in (u/'metadata/objects.jsonl').read_text().splitlines()]
 by={r['relative_path']:r for r in files}; checks={r['relative_path']:r for r in rows(u/'00_MANIFESTS/payload-checksums.csv')}
 assert len(files)==len(cat)==len(checks)==25
 for r in cat:
  p=r['final_relative_path']; n=by[p]; c=checks[p]; st=(u/p).stat()
  assert r['final_sha256']==n['final_sha256']==c['sha256']
  assert st.st_size==int(r['size_bytes'])==int(n['size_bytes'])==int(c['size_bytes'])
  assert st.st_mtime_ns==int(c['mtime_ns'])
 assert not any('BIB-1002-md-106189' in p for p in by)
 assert len(objects)==3 and sum(r['file_role']=='master_member' for r in files)==4
 for o in objects:
  members=sorted([r for r in files if r['object_key']==o['object_key'] and r['file_role']=='master_member'],key=lambda r:int(r['sequence']))
  assert [r['relative_path'] for r in members]==json.loads(o['ordered_master_paths'])
  assert {r['selected_candidate_id'] for r in members}=={o['selected_candidate_id']}
 b=next(o for o in objects if o['object_key']=='BIB-1002')
 assert b['catalogued_book_date']=='1885' and not b['original_md_description']
 assert 'not a complete book scan' in b['representation_extent']
 d=rows(u/'00_MANIFESTS/candidate-dispositions.csv'); assert Counter(r['disposition'] for r in d)=={'selected_master':3,'redundant':7,'excluded':1}
 assert rows(WORK/'bibliothek/master-decisions.csv')==rows(u/'00_MANIFESTS/master-decisions.csv')
 source=rows(u/'00_MANIFESTS/source-disposition.csv'); assert len(source)==12 and sum(r['exact_copy_proven']=='true' for r in source)==11
 for r in source:
  assert r['deletion_authorized']=='false'
  if r['inventory']=='BIB-1002' and r['source_kind']=='protected_usb':
   st=Path(r['source_path']).stat(); assert (st.st_size,st.st_mtime_ns)==(int(r['source_size_bytes']),int(r['source_mtime_ns']))
   assert r['source_sha256']==r['final_sha256']==by[r['final_path']]['final_sha256']
 workbook(u/'CATALOG.xlsx',{'Catalog':u/'CATALOG.csv','Objects':u/'db-restore/objects.csv'})
 receipt=json.loads((u/'00_MANIFESTS/accession-completion.json').read_text()); assert receipt['final_files']==sum(p.is_file() for p in u.rglob('*'))
 return dict(result='passed',objects=3,master_members=4,previews=4,payload_rows=25,sqlite_csv_jsonl_xlsx_agree=True,selected_PDF_stored_hash_matches=True)

def paragon():
 u=ROOT/'paragon'; w=WORK/'paragon'; objects=rows(u/'metadata/objects.csv'); cat=rows(u/'CATALOG.csv'); restore=rows(u/'db-restore/RESTORE.csv'); reps=rows(u/'db-restore/manifests/representations.csv')
 db=sqlite3.connect((u/'db-restore/RESTORE.sqlite').as_uri()+'?mode=ro',uri=True)
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok' and not db.execute('pragma foreign_key_check').fetchall()
 assert len(objects)==240 and len(cat)==493 and len(restore)==483 and len(reps)==241
 assert [o['archival_label'] for o in objects if not o['master_file']]==['VLV-1092-2']
 assert sum(bool(o['master_file']) for o in objects)==239
 assert sum(int(o['master_member_count']) for o in objects)==241
 checks={r['path']:r for r in rows(u/'db-restore/checksums.csv')}; assert len(checks)==511
 progress=json.loads((w/'reviewed-accession-progress.json').read_text())
 for p,r in progress['files'].items():
  st=(u/p).stat(); assert [st.st_size,st.st_mtime_ns]==r['stat'],p
  assert checks[p]['sha256'].removeprefix('sha256:')==r['sha256'],p
 for r in cat:
  p=r['final_relative_path']; assert (u/p).stat().st_size==int(r['size_bytes'])==int(checks[p]['size_bytes'])
  assert r['final_sha256'].removeprefix('sha256:')==checks[p]['sha256'].removeprefix('sha256:')
 for r in reps:
  for kind in ('master','access'):
   assert r[kind+'_sha256'].removeprefix('sha256:')==checks[r[kind+'_path']]['sha256'].removeprefix('sha256:')
 for o in objects:
  actual=db.execute('select descriptive_title,description from archive_objects where entity_id=?',(o['iss_id'],)).fetchone()
  assert actual==(o['descriptive_title'],o['description']),o['archival_label']
 by_path={r['relative_path']:r for r in restore if r['relative_path']}
 native=db.execute("select f.relative_path,f.content_digest,b.size_bytes from entity_files e join file_occurrences f using(occurrence_id) join content_blobs b using(content_digest) where e.status='active'").fetchall()
 assert len(native)==482
 for p,d,s in native:
  r=by_path[p]; assert d.removeprefix('sha256:')==r['sha256'].removeprefix('sha256:') and s==int(r['size_bytes'])
 title='Einladungskarte Brigadeempfang 9. Februar 1962'
 obj=next(o for o in objects if o['archival_label']=='VLV-1092-1'); assert obj['descriptive_title']==title and obj['date_year']=='1962'
 embedded=json.loads(subprocess.run(['exiftool','-j','-XMP-dc:Title','-XMP-iptcExt:ArtworkTitle','-IPTC:ObjectName',str(u/obj['master_file'])],capture_output=True,text=True,check=True).stdout)[0]
 assert all(embedded[k]==title for k in ('Title','ArtworkTitle','ObjectName'))
 for r in cat:
  if r['inventory']=='VLV-1092-1': assert r['title']==title and r['date_text']=='1962'
 note=db.execute('select note from archive_objects where archival_label=?',('VLV-1092-2',)).fetchone()[0]
 assert 'Genuine loss is unconfirmed' in note
 # Validate exactly the saved work decisions, independently of the accession runner.
 sys.path.insert(0,str(w)); import paragon_workbench as review
 with review.connect() as c:
  reviewed=review.load_objects(c)
 assert sum(bool(o['selected_path']) for o in reviewed)==8
 for o in reviewed:
  if not o['selected_path']:continue
  native_o=next(x for x in objects if x['archival_label']==o['inventory'])
  source=next(x for x in o['candidates'] if x['source_path']==o['selected_path'])
  review.verify_candidate(source)
  if o['inventory'] in ('VLV-1050-1','VLV-1053-1'):
   assert Path(source['source_path']).read_bytes()==(u/native_o['master_file']).read_bytes()
  assert native_o['master_file']
  assert all(x['prior']['state']==('selected_master' if x['source_path']==o['selected_path'] else 'redundant') for x in o['candidates'])
 workbook(u/'CATALOG.xlsx',{'Catalog':u/'CATALOG.csv','Objects':u/'metadata/objects.csv'})
 workbook(u/'db-restore/RESTORE.xlsx',{'Catalog':u/'db-restore/RESTORE.csv'})
 return dict(result='passed',objects=240,represented_objects=239,master_members=241,catalog_rows=493,restore_rows=483,unresolved=['VLV-1092-2'],title_catalogue_and_embedded_metadata='1962',sqlite_csv_xlsx_agree=True,final_stamps_and_stored_hashes_agree=True)

if __name__=='__main__':
 mount=subprocess.run(['findmnt','-rn','-T','/media/vladimir/Elements/restore/archive','-o','SOURCE,TARGET,FSTYPE,OPTIONS'],capture_output=True,text=True,check=True).stdout.strip().split()
 assert mount[:3]==['/dev/sdf1','/media/vladimir/Elements','ntfs3'] and 'ro' in mount[3].split(',') and 'rw' not in mount[3].split(',')
 result=dict(checked_at=datetime.now(timezone.utc).isoformat(),bibliothek=bibliothek(),paragon=paragon(),nef=json.loads((WORK/'wf-alben/nef-recheck.json').read_text())['result'])
 print(json.dumps(result,ensure_ascii=False,indent=2))
