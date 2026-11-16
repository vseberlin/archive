"""Bounded accession of the reviewed remaining Paragon files; existing layout/schema only."""
import argparse,copy,hashlib,json,os,shutil,sqlite3,subprocess
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from PIL import Image,ImageOps
from openpyxl import load_workbook
import paragon_workbench as app
import retained_candidates as review
from accession_carrier_section import read_csv,write_csv,stamp,connect,update_workbook,render_range
from accession_reviewed import embed,verify_workbook
from accession_replacements_20260921 import metadata,descriptive_metadata
from accession_retained import new_id
from reconcile_drain_decisions import counters
W,U=review.ROOT,review.ARCHIVE
PLAN=W/'remaining-accession-plan-20260921.json'
RECEIPT='db-restore/manifests/remaining-accession-completion-20260921.json'
R=W/Path(RECEIPT).name
TABLES=['CATALOG.csv','metadata/objects.csv','db-restore/RESTORE.csv','db-restore/manifests/representations.csv','db-restore/manifests/source-disposition.csv']
MODEL=['archive_objects','entity_names','entity_identifiers','entity_relations','evidence_assertions']
CONTROLS=TABLES+['CATALOG.xlsx','db-restore/RESTORE.xlsx','db-restore/RESTORE.sqlite','db-restore/RESTORE.sql','db-restore/manifests/drain-list.csv','db-restore/manifests/drain-evidence.json','db-restore/manifests/accession-remaining-20260921.json','db-restore/manifests/accession-remaining-20260921.csv','db-restore/manifests/ACCESSION_REMAINING_20260921.md','00_README.md','catalog-export-summary.json','db-restore/export-summary.json',RECEIPT]
def now():return datetime.now(timezone.utc).isoformat()
def save(r):review.write_json(R,r)
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def media(r):return [f for p in r['files'] for f in p.get('media',[])]
def source_check(e):
 p=Path(e['source_path']);assert p.resolve(strict=True)==p and p.is_relative_to(app.SOURCE) and not p.is_symlink()
 assert stamp(p)==[e['source_size_bytes'],e['source_mtime_ns']],str(p)
def prepare():
 app.mount_guard();assert not R.exists() and not (U/RECEIPT).exists(),'Existing receipt: inspect, never replay.'
 plan=review.readjson(PLAN);files=plan['files'];assert len(files)==len({p['source'] for p in files})==142
 evidence={e['source_path']:e for e in review.readjson(W/'drain-evidence.json')}
 for e in evidence.values():source_check(e)
 with app.connect() as c:
  assert review.readable_decisions(c)==review.readjson(review.DECISIONS)
  saved=review.saved_reviews(c)
 tables={p:read_csv(U/p) for p in TABLES};objects={o['archival_label']:o for o in tables['metadata/objects.csv'][1]}
 assert len(objects)==260 and len(tables['CATALOG.csv'][1])==649
 for a,b in [('source-disposition.csv','source-disposition.planned.csv'),('drain-list.csv','drain-list.csv'),('drain-evidence.json','drain-evidence.json')]:assert (U/'db-restore/manifests'/a).read_bytes()==(W/b).read_bytes()
 prior_db={};used=set()
 for db in [W/'work/catalog.sqlite',U/'db-restore/RESTORE.sqlite']:
  with connect(db) as c:
   assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
   used.update(x[0] for x in c.execute('select entity_id from entities'))
   prior_db[str(db)]={t:[dict(x) for x in c.execute('select * from '+t+' order by rowid')] for t in MODEL+['entity_files']}
 owner={f['path']:(key,f) for key,d in saved.items() for f in d['files']}
 new_objects={}
 for p in files:
  e=evidence[p['source']];assert e['source_sha256']==p['source_sha256'];key,f=owner[p['source']]
  assert f['sha256']==p['source_sha256'] and saved[key]['action']!='ignore'
  p.update(source_evidence=e,case_key=key,prior_file=f,media=[])
  if p['kind'] not in ['new','member']:continue
  assert not e.get('other_unit_links'),p['source']
  if p['kind']=='new':
   eid=new_id(used);label=p['inventory'] or eid;assert label not in objects
   p.update(entity_id=eid,inventory=label,evidence_source_id='SOURCE-'+uuid4().hex.upper())
   o={k:'' for k in tables['metadata/objects.csv'][0]};o.update(iss_id=eid,archival_label=label,canonical_name=label,descriptive_title=p['title'],description=p['description'],date_year=p['date'],object_type=p['object_type'],rights_status='not_recorded_in_minimal_core_v1',record_state='represented',master_member_count='0',notes_for_review='Local source label; no museum-digital inventory assigned. Description transcribed from visible content; unknown identity/date fields left blank. '+RECEIPT)
   p['object']=o;new_objects[p['index']]=o
 for p in files:
  if p['kind'] not in ['new','member']:continue
  if p['kind']=='member':
   o=new_objects[int(p['inventory'][1:])] if p['inventory'].startswith('@') else objects[p['inventory']]
   p.update(inventory=o['archival_label'],entity_id=o['iss_id'],object=copy.deepcopy(o))
  o=p['object'];suffix=Path(p['source']).suffix.lower();suffix='.tif' if suffix=='.tiff' else suffix
  base=p['inventory'] if p['inventory']==p['entity_id'] else p['inventory']+'__'+p['entity_id']
  if p['kind']=='member':base+='__section-'+Path(p['source']).stem
  p.update(master_path='masters/'+base+'__master'+suffix,access_path='export/access/'+base+'__access.jpg')
  for rel in [p['master_path'],p['access_path']]:assert (U/rel).parent.is_dir() and not (U/rel).exists(),rel
  with Image.open(p['source']) as im:
   im.load();assert im.format in ['TIFF','JPEG'] and getattr(im,'n_frames',1)==1 and im.mode in ['RGB','L','P']
   p['technical']=dict(width=im.width,height=im.height,mode=im.mode,orientation=im.getexif().get(274,1),compression=im.info.get('compression','JPEG' if im.format=='JPEG' else 'raw'))
  if o['master_file']:
   p['metadata_reference']=o['master_file'].splitlines()[0];p['metadata_reference_stamp']=stamp(U/p['metadata_reference']);p['prior_embedded']=metadata(U/p['metadata_reference'])
 r=dict(plan=plan,files=files,authorization=plan['authorization'],started_at=now(),result='prepared',prior_tables=tables,prior_reviews=saved,prior_pending=review.readjson(review.PENDING_ACCESSION),source_evidence=evidence,prior_databases=prior_db,original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),original_files={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))},log_id='LOG-'+uuid4().hex.upper(),source_modified=False,deletion_authorized=False,publication_performed=False,workbook_runtime='Artifact Tool XLSX import failed: runtime lacks WASM SIMD. Bundled openpyxl narrow editor and independent cell/style verification used.')
 assert shutil.disk_usage(U).free>2*sum(p['source_evidence']['source_size_bytes'] for p in files if p['kind'] in ['new','member'])
 save(r);print(json.dumps(dict(prepared=True,counts=dict(Counter(p['kind'] for p in files)))),flush=True)

def make_media(r):
 assert r['result']=='prepared';app.mount_guard()
 with app.connect() as c:assert review.saved_reviews(c)==r['prior_reviews']
 for p in r['files']:
  if p['kind'] not in ['new','member']:continue
  source_check(p['source_evidence']);p['source_embedded']=metadata(p['source'])
  # Record all source tags before explicitly assigning accepted object metadata.
  o=p['object'];p['embedded']=dict(iss_id=p['entity_id'],archival_label=p['inventory'],descriptive_title=o['descriptive_title'],description=o['description']+'\n\nAnsicht: '+p['part']+'. Quelldatei: '+Path(p['source']).name+'.')
  for kind in ['master','access']:
   rel=p[kind+'_path']
   prior=next((f for f in p['media'] if f['path']==rel),None)
   if prior:
    assert stamp(U/rel)==prior['stamp'];continue
   assert not (U/rel).exists(),rel
   staged=W/('remaining-final-'+Path(rel).name)
   if staged.exists():
    assert p['index']==10 and kind=='access','Unexpected staged file; inspect before resuming.'
   elif kind=='master':
    with Path(p['source']).open('rb') as src,staged.open('xb') as dst:shutil.copyfileobj(src,dst,1024*1024)
   else:
    with Image.open(U/p['master_path']) as im,staged.open('xb') as dst:ImageOps.exif_transpose(im).convert('RGB').save(dst,'JPEG',quality=92,subsampling=0)
   if p.get('metadata_reference'):
    ref=U/p['metadata_reference'];assert stamp(ref)==p['metadata_reference_stamp']
    subprocess.run(['exiftool','-overwrite_original','-P','-XMP:All=','-IPTC:All=','-IFD0:ImageDescription=','-IFD0:Artist=','-IFD0:Copyright=','-TagsFromFile',str(ref),'-XMP','-IPTC','-IFD0:ImageDescription','-IFD0:Artist','-IFD0:Copyright',str(staged)],check=True,capture_output=True)
    assert descriptive_metadata(metadata(staged))==descriptive_metadata(p['prior_embedded'])
   embed(staged,p['embedded'])
   with Image.open(staged) as im:
    im.load();width,height,mode=im.width,im.height,im.mode
    if kind=='master':
     with Image.open(p['source']) as src:
      src.load();assert im.size==src.size and im.mode==src.mode and im.tobytes()==src.tobytes()
      assert im.getexif().get(274,1)==src.getexif().get(274,1)
      if im.mode=='P':assert im.getpalette()==src.getpalette()
   os.rename(staged,U/rel)
   f=dict(path=rel,role='preservation_master' if kind=='master' else 'access_derivative',sha256=sha(U/rel),size_bytes=(U/rel).stat().st_size,stamp=stamp(U/rel),width=width,height=height,mode=mode,media_type='image/tiff' if Path(rel).suffix=='.tif' else 'image/jpeg',compression=p['technical']['compression'] if kind=='master' else 'JPEG',occurrence_id='OCC-'+uuid4().hex.upper(),observed_mtime_utc=datetime.fromtimestamp((U/rel).stat().st_mtime,timezone.utc).isoformat())
   f['provenance']=dict(source_path=p['source'],source_sha256=p['source_sha256'],source_size_bytes=p['source_evidence']['source_size_bytes'],source_mtime_ns=p['source_evidence']['source_mtime_ns'],entity_id=p['entity_id'],inventory=p['inventory'],master_path=p['master_path'],part=p['part'],derivation='metadata_enriched_selected_master' if kind=='master' else 'generated_from_selected_master',binding_evidence=p['reason'],date_text=o['date_year'],metadata_source='Existing catalogue unchanged; new descriptions based on visible source evidence and explicit accession authorization.',receipt=RECEIPT,deletion_authorized=False)
   p['media'].append(f);save(r)
  print('Final media',p['index'],p['inventory'],flush=True)
 r['result']='media_written';save(r)

def register_db(c,r,with_reviews=False):
 assert not c.execute('select 1 from logbook where log_id=?',(r['log_id'],)).fetchone()
 for p in r['files']:
  if p['kind']=='new':
   o=p['object'];eid=p['entity_id'];sid=p['evidence_source_id'];at=r['started_at'];note=o['notes_for_review']
   c.execute('insert into entities values (?,?,?,?,?)',(eid,'archive_object','active',at,at))
   c.execute('insert into evidence_sources (source_id,source_type,system,external_id,captured_at,local_absolute_path,citation,note) values (?,?,?,?,?,?,?,?)',(sid,'local_review','Paragon retained review',p['source'],at,str(U/RECEIPT),p['source'],p['reason']))
   c.execute('insert into archive_objects values (?,?,?,?,?,?,?,?,?,?,?)',(eid,o['archival_label'],o['descriptive_title'],o['description'],'','','blocked','active',note,at,at))
   c.execute('insert into entity_names values (?,?,?,?,?,?,?,?,?)',('NAME-'+uuid4().hex.upper(),eid,'canonical',o['canonical_name'],'','active',sid,at,at))
   c.execute('insert into entity_unit_memberships values (?,?,?,?)',(eid,'WF-PARAGON','primary',at))
   for key,val in [('archive_descriptive_title',o['descriptive_title']),('archive_description',o['description']),('archive_object_type',o['object_type'])]:
    c.execute('insert into evidence_assertions values (?,?,?,?,?,?,?,?,?,?,?,?)',('ASSERT-'+uuid4().hex.upper(),eid,key,val,None,'accepted','certain',sid,'Vladimir; applied by Codex',at,None,'Visible source content. No uncertain physical identity, date or museum inventory inferred.'))
 # A detail may precede its new parent in source order. Create every object first.
 for p in r['files']:
  for f in p['media']:
   c.execute('insert or ignore into content_blobs (content_digest,size_bytes,media_type,width_px,height_px,bit_depth,color_space,compression,page_count,created_at) values (?,?,?,?,?,?,?,?,?,?)',('sha256:'+f['sha256'],f['size_bytes'],f['media_type'],f['width'],f['height'],8,f['mode'],f['compression'],1,r['started_at']))
   c.execute('insert into file_occurrences (occurrence_id,unit_id,content_digest,occurrence_kind,relative_path,original_filename,observed_mtime_utc,observed_at_utc,status,note) values (?,?,?,?,?,?,?,?,?,?)',(f['occurrence_id'],'WF-PARAGON','sha256:'+f['sha256'],'archive',f['path'],Path(f['path']).name,f['observed_mtime_utc'],r['started_at'],'verified',json.dumps(f['provenance'],ensure_ascii=False)))
   c.execute('insert into entity_files values (?,?,?,?,?)',(p['entity_id'],f['occurrence_id'],f['role'],'active',r['started_at']))
 c.execute('update catalog_state set catalog_revision=catalog_revision+1,published_revision=catalog_revision+1,dirty_since=NULL')
 c.execute('insert into logbook values (?,?,?,?,?,?,?,?,?,?,?)',(r['log_id'],r['started_at'],'Vladimir','WF-PARAGON','accession_remaining_sources','archive_unit','WF-PARAGON','{}',json.dumps(dict(receipt=RECEIPT,files=r['files'],decisions=r['updated_reviews']),ensure_ascii=False),RECEIPT,r['authorization']))
 if with_reviews:
  for d in r['updated_reviews'].values():review.record_review(c,d)
 assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()

def register(r):
 assert r['result']=='media_written'
 updated=copy.deepcopy(r['prior_reviews']);byindex={p['index']:p for p in r['files']}
 for p in r['files']:
  if p['kind']=='pending':continue
  d=updated[p['case_key']];f=next(f for f in d['files'] if f['path']==p['source']);assert f==p['prior_file']
  if p['kind']=='redundant':
   f['state']='redundant'
   if p.get('coverage_index'):
    target=byindex[p['coverage_index']];f['derivative_evidence']=dict(coverage_target=dict(kind='accessioned_master' if target['media'] else 'retained_source',path=str(U/target['master_path']) if target['media'] else target['source'],sha256='sha256:'+target['media'][0]['sha256'] if target['media'] else target['source_sha256']),awaiting_accession=not bool(target['media']),basis=p['reason'])
  else:f['state']='selected_master' if p['kind']=='new' else 'carrier_section'
  f.update(note=f.get('note','')+'\nAccession review 21 September: '+p['reason'],accession_evidence=dict(receipt=RECEIPT,inventory=p.get('inventory',''),entity_id=p.get('entity_id',''),master_path=p.get('master_path',''),reason=p['reason'],deletion_authorized=False))
  if p['kind']=='member':f.update(relation='section_of',carrier=p['inventory'])
  if p['kind']=='redundant' and d.get('selected_master_path')==p['source']:
   d['selected_master_path']=''
   if d.get('source_version_pair',{}).get('selected_master_path')==p['source'] or d.get('source_version_pair',{}).get('tiff_path')==p['source']:d['source_version_pair']={}
  d.update(actor='Vladimir; applied by Codex',reviewed_at=r['started_at'])
 for key,d in updated.items():
  if d==r['prior_reviews'][key]:continue
  selected=[f for f in d['files'] if f['state']=='selected_master']
  assert len(selected)<=1,(key,len(selected))
  if selected:
   d.update(action='add',selected_master_path=selected[0]['path'])
  else:
   d.update(action='retain',selected_master_path='');d.pop('source_version_pair',None)
 r['updated_reviews']={k:v for k,v in updated.items() if v!=r['prior_reviews'][k]};save(r)
 for db in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
  with connect(db) as c:
   if db==W/'work/catalog.sqlite':assert review.saved_reviews(c)==r['prior_reviews']
   m=sqlite3.connect(':memory:');m.row_factory=sqlite3.Row;c.backup(m);m.execute('pragma foreign_keys=ON');register_db(m,r,db==W/'work/catalog.sqlite');m.close()
 # Generate assertion/name IDs once by applying archive first, then copy exact new catalogue rows to work.
 with connect(U/'db-restore/RESTORE.sqlite',True) as c:register_db(c,r)
 with connect(U/'db-restore/RESTORE.sqlite') as a,connect(W/'work/catalog.sqlite',True) as c:
  # New rows must have identical keys and values in both rebuildable catalogues.
  eids=[p['entity_id'] for p in r['files'] if p['kind']=='new'];marks=','.join('?' for _ in eids)
  for table,col in [('entities','entity_id'),('evidence_sources','source_id'),('archive_objects','entity_id'),('entity_names','entity_id'),('entity_unit_memberships','entity_id'),('evidence_assertions','subject_entity_id')]:
   values=[p['evidence_source_id'] for p in r['files'] if p['kind']=='new'] if table=='evidence_sources' else eids
   for row in a.execute('select * from '+table+' where '+col+' in ('+marks+')',values):c.execute('insert into '+table+' values ('+','.join('?' for _ in row)+')',tuple(row))
  for p in r['files']:
   for f in p['media']:
    for table,col,val in [('content_blobs','content_digest','sha256:'+f['sha256']),('file_occurrences','occurrence_id',f['occurrence_id'])]:
     row=a.execute('select * from '+table+' where '+col+'=?',(val,)).fetchone();c.execute('insert '+('or ignore ' if table=='content_blobs' else '')+'into '+table+' values ('+','.join('?' for _ in row)+')',tuple(row))
    row=a.execute('select * from entity_files where occurrence_id=?',(f['occurrence_id'],)).fetchone();c.execute('insert into entity_files values (?,?,?,?,?)',tuple(row))
  row=a.execute('select * from logbook where log_id=?',(r['log_id'],)).fetchone();c.execute('insert into logbook values (?,?,?,?,?,?,?,?,?,?,?)',tuple(row))
  c.execute('update catalog_state set catalog_revision=catalog_revision+1,published_revision=catalog_revision+1,dirty_since=NULL')
  for d in r['updated_reviews'].values():review.record_review(c,d)
  assert not c.execute('pragma foreign_key_check').fetchall()
 r['result']='registered';save(r)

def sync_work_bindings(r):
 # Existing work rows still use legacy units/WF-PARAGON/data paths. Align only
 # the objects touched by this accession with the current accession catalogue.
 affected={p['entity_id'] for p in r['files'] if p['media']}
 with connect(U/'db-restore/RESTORE.sqlite') as archive,connect(W/'work/catalog.sqlite',True) as c:
  before=[dict(x) for x in c.execute('select * from entity_files order by rowid') if x['entity_id'] in affected]
  for eid in sorted(affected):
   active=[dict(x) for x in archive.execute("select * from entity_files where entity_id=? and status='active' and role in ('preservation_master','preservation_surrogate','access_derivative')",(eid,))]
   c.execute("update entity_files set status='superseded' where entity_id=? and status='active' and role in ('preservation_master','preservation_surrogate','access_derivative')",(eid,))
   for link in active:
    occurrence=dict(archive.execute('select * from file_occurrences where occurrence_id=?',(link['occurrence_id'],)).fetchone())
    blob=dict(archive.execute('select * from content_blobs where content_digest=?',(occurrence['content_digest'],)).fetchone())
    for table,row,key in [('content_blobs',blob,'content_digest'),('file_occurrences',occurrence,'occurrence_id')]:
     cols=list(row);suffix='do nothing' if table=='content_blobs' else 'do update set '+','.join(k+'=excluded.'+k for k in cols if k!=key)
     c.execute('insert into '+table+' ('+','.join(cols)+') values ('+','.join('?' for _ in cols)+') on conflict('+key+') '+suffix,tuple(row.values()))
    c.execute('insert into entity_files values (?,?,?,?,?) on conflict(entity_id,occurrence_id,role) do update set status=excluded.status,created_at=excluded.created_at',tuple(link.values()))
  assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
  after=[dict(x) for x in c.execute('select * from entity_files order by rowid') if x['entity_id'] in affected]
 r['work_binding_alignment']=dict(affected_entities=sorted(affected),before=before,after=after,reason='Current archive bindings replace legacy work-index path aliases for these accessioned objects; source and archive files are unchanged.')
 save(r)

def export(r):
 assert r['result']=='registered';tables=copy.deepcopy(r['prior_tables'])
 ch,catalog=tables['CATALOG.csv'];oh,objects=tables['metadata/objects.csv'];rh,restore=tables['db-restore/RESTORE.csv'];ph,reps=tables['db-restore/manifests/representations.csv']
 for p in r['files']:
  if p['kind']=='new':objects.append(copy.deepcopy(p['object']))
 for p in sorted(r['files'],key=lambda p:(p['kind']!='new',p['index'])):
  if not p['media']:continue
  obj=next(o for o in objects if o['iss_id']==p['entity_id']);old_master=obj['master_file'].splitlines()[0] if obj['master_file'] else ''
  obj['master_file']='\n'.join(filter(None,[obj['master_file'],p['master_path']]))
  obj['access_file']='\n'.join(filter(None,[obj['access_file'],p['access_path']]))
  if not obj['master_parts'] and old_master:obj['master_parts']='existing selected detail/overview'
  obj['master_parts']='\n'.join(filter(None,[obj['master_parts'],p['part']]))
  obj['master_member_count']=str(len(obj['master_file'].splitlines()));obj['notes_for_review']+='; '+RECEIPT
  for f in p['media']:
   master=f['role']=='preservation_master';template=next((x for x in catalog if x['final_relative_path']==old_master),None)
   cr=copy.deepcopy(template) if template else {k:'' for k in ch}
   cr.update(record_key=p['entity_id'],inventory=p['inventory'],title=obj['descriptive_title'],description=p['embedded']['description'],file_role=f['role'],final_relative_path=f['path'],master_relative_path=p['master_path'],filename=Path(f['path']).name,file_type=Path(f['path']).suffix[1:],size_bytes=str(f['size_bytes']),final_sha256=f['sha256'],date_text=obj['date_year'],object_type=obj['object_type'],material=obj['material_technique'],dimensions=obj['dimensions'],original_source_path=p['source'] if master else str(U/p['master_path']),source_sha256=p['source_sha256'].removeprefix('sha256:') if master else p['media'][0]['sha256'],derivative_of='' if master else p['master_path'],sequence=obj['master_member_count'],width=str(f['width']),height=str(f['height']),relationship='member of the single selected Master for '+p['inventory']+'; part='+p['part'] if master else 'generated access image of selected Master member',access_restriction='publication blocked',condition='Existing source pixels and orientation preserved; complementary coverage where specified.',notes=p['reason'],metadata_path='metadata/objects.csv',provenance_path=RECEIPT+'\ndb-restore/manifests/source-disposition.csv')
   if not template:cr['metadata_authority']='Visible source evidence; applied under Vladimir accession authorization.'
   catalog.append(cr)
   template_r=next((x for x in restore if x['relative_path']==old_master and x['representation_status']=='active'),None)
   rr=copy.deepcopy(template_r) if template_r else {k:'' for k in rh}
   rr.update(catalog_format='iss-unit-catalog-v1',unit_id='WF-PARAGON',unit_title='Paragon provenance recovery',iss_id=p['entity_id'],membership_roles='primary',canonical_name=obj['canonical_name'],archival_label=p['inventory'],descriptive_title=obj['descriptive_title'],description=obj['description'],material_technique=obj['material_technique'],dimensions=obj['dimensions'],publication_status='blocked',object_status='active',object_note=obj['notes_for_review'],identifiers=obj['identifiers'],rights_status=obj['rights_status'],representation_role=f['role'],representation_status='active',file_status='verified',relative_path=f['path'],original_filename=Path(f['path']).name,sha256=f['sha256'],size_bytes=str(f['size_bytes']),media_type=f['media_type'],width_px=str(f['width']),height_px=str(f['height']),bit_depth='8',color_space=f['mode'],compression=f['compression'],page_count='1',observed_mtime_utc=f['observed_mtime_utc'],provenance_note=json.dumps(f['provenance'],ensure_ascii=False),provenance_status='occurrence_note_available',record_state='represented')
   restore.append(rr)
  m,a=p['media'];reps.append(dict(zip(ph,[p['entity_id'],p['inventory'],'preservation_master',m['path'],'sha256:'+m['sha256'],a['path'],'sha256:'+a['sha256'],m['path'],'generated_from_selected_master','local_access',str(a['width']),str(a['height']),Path(__file__).name,r['started_at']])))
 with connect(U/'db-restore/RESTORE.sqlite') as c:
  rev=c.execute('select catalog_revision from catalog_state').fetchone()[0]
  for o in objects:
   if o['iss_id'] in {p.get('entity_id') for p in r['files'] if p['media']}:
    counts=Counter(x[0] for x in c.execute("select role from entity_files where entity_id=? and status='active'",(o['iss_id'],)));o['representation_summary']='; '.join(k+': '+str(v) for k,v in sorted(counts.items()))
  sql='\n'.join(c.iterdump())+'\n'
 for row in restore:row['catalog_revision']=str(rev)
 for name in TABLES[:-1]:write_csv(U/name,*tables[name])
 (U/'db-restore/RESTORE.sql').write_text(sql);m=sqlite3.connect(':memory:');m.executescript(sql);assert m.execute('pragma quick_check').fetchone()[0]=='ok' and not m.execute('pragma foreign_key_check').fetchall();m.close()
 r['workbook_checks']={'CATALOG.xlsx':update_workbook(U/'CATALOG.xlsx',[('Catalog',ch,catalog),('Objects',oh,objects)]),'db-restore/RESTORE.xlsx':update_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,restore)])}
 # Fit only changed object rows; retain the established column widths and styles.
 wb=load_workbook(U/'CATALOG.xlsx');s=wb['Objects']
 for ri,o in enumerate(objects,2):
  if o['iss_id'] not in {p.get('entity_id') for p in r['files'] if p['media']}:continue
  for field in ['master_file','access_file','master_parts','notes_for_review','representation_summary']:
   cell=s.cell(ri,oh.index(field)+1);a=copy.copy(cell.alignment);a.wrap_text=True;cell.alignment=a
  if o['iss_id'] in {p['entity_id'] for p in r['files'] if p['kind']=='new'}:
   for field in ['archival_label','canonical_name']:
    cell=s.cell(ri,oh.index(field)+1);a=copy.copy(cell.alignment);a.wrap_text=True;cell.alignment=a
  s.row_dimensions[ri].height=max(s.row_dimensions[ri].height or 23,len(o['master_file'].splitlines())*30+30)
 wb.save(U/'CATALOG.xlsx');wb.close()
 wb=load_workbook(U/'db-restore/RESTORE.xlsx');s=wb['Catalog']
 for row in s.iter_rows(min_row=len(r['prior_tables']['db-restore/RESTORE.csv'][1])+2):
  for cell in row:
   if isinstance(cell.value,str):
    a=copy.copy(cell.alignment);a.wrap_text=True;cell.alignment=a
 wb.save(U/'db-restore/RESTORE.xlsx');wb.close()
 r.update(result='exported',catalog_revision=rev,counts=dict(objects=len(objects),represented_objects=sum(bool(o['master_file']) for o in objects),master_members=len(reps),access_images=len(reps),documents=10,catalog_rows=len(catalog),restore_rows=len(restore),new_objects=9,selected_sources=45,redundant_sources=63,derivatives_generated=45));save(r)

def dispositions(r):
 assert r['result']=='exported';h,rows=copy.deepcopy(r['prior_tables']['db-restore/manifests/source-disposition.csv'])
 bypath={p['source']:p for p in r['files']};byindex={p['index']:p for p in r['files']};objects={o['archival_label']:o for o in read_csv(U/'metadata/objects.csv')[1]};catalog={c['final_relative_path']:c for c in read_csv(U/'CATALOG.csv')[1]}
 def target(p,seen=None):
  seen=set() if seen is None else seen;assert p['index'] not in seen;seen.add(p['index'])
  if p['media']:return p['master_path'],p['inventory']
  if p.get('coverage_index'):return target(byindex[p['coverage_index']],seen)
  if p.get('inventory') in objects:return objects[p['inventory']]['master_file'].splitlines()[0],p['inventory']
  return '',p.get('inventory','')
 changed=[]
 for row in rows:
  p=bypath.get(row['source_path'])
  if not p or p['kind']=='pending':continue
  before=copy.deepcopy(row);t,inv=target(p)
  row.update(inventory=inv or row['inventory'],source_role='selected_master_candidate' if p['media'] else 'redundant_candidate',final_path=t,final_sha256='sha256:'+catalog[t]['final_sha256'] if t else '',preservation_status='preserved_as_metadata_enriched_master' if p['media'] else ('covered_by_selected_master' if t else 'reviewed_not_selected'),preservation_basis=p['reason'],exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',notes=row['notes']+'; '+RECEIPT+'; source retained; no deletion.')
  changed.append(dict(before=before,after=copy.deepcopy(row)))
 assert len(changed)==108 and counters(rows)['candidate_rows']==1384
 for p in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(p,h,rows)
 for p in [W/'drain-list.csv',U/'db-restore/manifests/drain-list.csv']:write_csv(p,h,[x for x in rows if x['drain_state']=='candidate_after_separate_review'])
 evidence=copy.deepcopy(list(r['source_evidence'].values()));updates={x['after']['source_path']:x['after'] for x in changed}
 for e in evidence:
  if e['source_path'] in updates:
   row=updates[e['source_path']];e.update(accession_receipt=RECEIPT,final_path=row['final_path'],final_sha256=row['final_sha256'],preservation_status=row['preservation_status'],deletion_authorized=False)
 for p in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(p,evidence)
 old=r['prior_pending'];pending=copy.deepcopy(old);pending['files']=[];pending['excluded_files']=[]
 current={**r['prior_reviews'],**r['updated_reviews']};current_files={f['path']:(d,f) for d in current.values() for f in d['files']}
 for f in old['files']:
  p=bypath.get(f['source_path']);d,saved=current_files[f['source_path']]
  if d['action']=='ignore':
   f=copy.deepcopy(f);f['exclusion_reason']=d.get('note','Another unit; explicitly excluded by Vladimir.');pending['excluded_files'].append(f)
  elif p and p['kind']=='pending':
   f=copy.deepcopy(f);f.update(review_note=p['reason'],saved_state=saved['state']);pending['files'].append(f)
 assert len(pending['files'])==34 and len(pending['excluded_files'])==1
 pending.update(counts=dict(Counter(f['saved_state'] for f in pending['files'])),scope='34 exact source paths remain uncertain; one other-unit source is excluded separately. The confirmed 108 sources have been processed.',remaining_completion_receipt=RECEIPT,physical_specimen_rule=r['plan']['physical_specimen_rule'])
 fh,_=read_csv(W/'accession-remaining-20260921.csv')
 md='# Paragon remaining specimen and ownership questions\n\n21 September 2026. Accessioned 45 selected source images: 9 new objects and 36 complementary views. Recorded 63 redundant sources. **108 of 142 sources resolved; 34 remain pending.** One explicitly excluded other-unit source is listed separately in the JSON. No deletion authorized.\n\nAll existing names, descriptive metadata and relationships remain unchanged. Existing sharper detail Masters are preserved. Each resolved object has one selected Master representation, with complementary members where needed.\n\n| Source | Remaining question |\n| --- | --- |\n'
 for f in pending['files']:md+='| '+f['source_path'].replace('|','\\|')+' | '+f['review_note'].replace('|','\\|')+' |\n'
 for root in [W,U/'db-restore/manifests']:
  review.write_json(root/'accession-remaining-20260921.json',pending);write_csv(root/'accession-remaining-20260921.csv',fh,[{k:f.get(k,'') for k in fh} for f in pending['files']]);(root/'ACCESSION_REMAINING_20260921.md').write_text(md)
 summary=review.readjson(W/'drain-review-summary.json');summary.update(counters(rows),remaining_completion_receipt=RECEIPT,current_register='1384 drain candidates; 34 pending sources; one other-unit source excluded; no deletion.');review.write_json(W/'drain-review-summary.json',summary)
 review.evidence.cache_clear()
 with app.connect() as c:review.export_decisions(c)
 r.update(result='records_written',source_disposition_updates=changed);r['counts'].update(drain_candidates=1384,pending_paths=34,excluded_other_unit_paths=1,pending_paths_resolved=108);save(r)

def verify(r):
 app.mount_guard();assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==r['original_directories']
 allowed=set(CONTROLS)|{'db-restore/checksums.csv'}|{f['path'] for f in media(r)}
 for p,st in r['original_files'].items():
  if p not in allowed:assert stamp(U/p)==st,p
 for e in r['source_evidence'].values():source_check(e)
 tables={p:read_csv(U/p) for p in TABLES};ch,cat=tables['CATALOG.csv'];oh,objs=tables['metadata/objects.csv'];rh,rest=tables['db-restore/RESTORE.csv']
 assert (len(objs),len(cat),len(rest),len(tables['db-restore/manifests/representations.csv'][1]))==(269,739,727,347)
 verify_workbook(U/'CATALOG.xlsx',[('Catalog',ch,cat),('Objects',oh,objs)]);verify_workbook(U/'db-restore/RESTORE.xlsx',[('Catalog',rh,rest)])
 allowed_object_fields={'master_file','access_file','master_member_count','master_parts','representation_summary','notes_for_review'};objby={o['iss_id']:o for o in objs}
 for old in r['prior_tables']['metadata/objects.csv'][1]:
  for k,v in old.items():
   if k not in allowed_object_fields:assert objby[old['iss_id']][k]==v,(old['archival_label'],k)
 sets=[]
 for db,baseline in r['prior_databases'].items():
  with connect(db) as c:
   assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
   for t in MODEL:
    actual=[dict(x) for x in c.execute('select * from '+t+' order by rowid')];assert actual[:len(baseline[t])]==baseline[t],(db,t)
   links=[dict(x) for x in c.execute('select * from entity_files order by rowid')]
   if db==str(W/'work/catalog.sqlite') and r.get('work_binding_alignment'):
    affected=set(r['work_binding_alignment']['affected_entities'])
    assert [x for x in links if x['entity_id'] not in affected]==[x for x in baseline['entity_files'] if x['entity_id'] not in affected]
    assert [x for x in links if x['entity_id'] in affected]==r['work_binding_alignment']['after']
   else:assert links[:len(baseline['entity_files'])]==baseline['entity_files']
   aset={}
   for o in objs:
    actual={x[0] for x in c.execute("select fo.relative_path from file_occurrences fo join entity_files ef using(occurrence_id) where ef.entity_id=? and ef.status='active' and ef.role in ('preservation_master','preservation_surrogate')",(o['iss_id'],))}
    if o['iss_id'] in {p.get('entity_id') for p in r['files'] if p['media']}:assert actual==set(o['master_file'].splitlines()) and len(actual)==int(o['master_member_count'])
    if o['iss_id'] in {p.get('entity_id') for p in r['files'] if p['media']}:aset[o['iss_id']]=sorted(actual)
   sets.append(aset)
   for f in media(r):
    assert stamp(U/f['path'])==f['stamp'];assert c.execute('select content_digest from file_occurrences where occurrence_id=?',(f['occurrence_id'],)).fetchone()[0]=='sha256:'+f['sha256']
    assert next(x for x in cat if x['final_relative_path']==f['path'])['final_sha256']==f['sha256']
 assert sets[0]==sets[1]
 for rel,work in [('source-disposition.csv','source-disposition.planned.csv'),('drain-list.csv','drain-list.csv'),('drain-evidence.json','drain-evidence.json'),('accession-remaining-20260921.csv','accession-remaining-20260921.csv'),('accession-remaining-20260921.json','accession-remaining-20260921.json')]:assert (U/'db-restore/manifests'/rel).read_bytes()==(W/work).read_bytes()
 rows=tables['db-restore/manifests/source-disposition.csv'][1];pending=review.readjson(review.PENDING_ACCESSION)
 assert len(rows)==len({x['source_path'] for x in rows})==1419 and all(x['deletion_authorized']=='false' for x in rows)
 assert {f['source_path'] for f in pending['files']+pending['excluded_files']}=={x['source_path'] for x in rows if x['drain_state']!='candidate_after_separate_review'}
 with app.connect() as c:
  assert review.saved_reviews(c)=={**r['prior_reviews'],**r['updated_reviews']}
  assert review.readable_decisions(c)==review.readjson(review.DECISIONS);assert review.drain_proposals(c)==review.readjson(review.DRAIN_PROPOSALS)
 return dict(ok=True,**r['counts'],source_stamps_unchanged=1419,metadata_names_attribution_and_relationships_preserved=True,one_selected_master_representation_per_object_preserved=True,directory_structure_unchanged=True,sqlite_and_readable_records_agree=True,source_modified=False,deletion_authorized=False)

def finish(r):
 assert r['result']=='records_written';r['validation']=verify(r)
 intro='## Remaining-file accession, 21 September 2026\n\nAccessioned 45 selected images: nine new objects and 36 complementary views. Generated 45 access JPEGs from their selected Masters. Recorded 63 redundant sources. **108 of 142 sources resolved; 34 remain pending physical-specimen or ownership evidence.** One other-unit source is explicitly excluded. No deletion authorized.\n\nCurrent totals: **269 objects, 268 represented, 347 selected Master members, 347 access images, ten documents, 1,384 drain paths**. VLV-1092-2 still lacks an identified source. Existing names, metadata, attribution, relationships and sharper detail Masters are preserved. Unknown dates/identities remain blank. Current receipt: `'+RECEIPT+'`. Exact remaining questions: `db-restore/manifests/ACCESSION_REMAINING_20260921.md`. Older counts below are historical.\n\n'
 for p in [U/'00_README.md',W/'00_README.md']:
  title,body=p.read_text().split('\n',1);p.write_text(title+'\n\n'+intro+body)
 r.update(result='passed',accession_performed=True,completed_at=now(),final_payload_hashes_computed_once=len(media(r)));save(r);review.write_json(U/RECEIPT,r)
 export=review.readjson(U/'catalog-export-summary.json');export.update(updated_at=r['completed_at'],file_rows=739,object_rows=269,remaining_completion_receipt=RECEIPT)
 summary=review.readjson(U/'db-restore/export-summary.json');summary.update(updated_at=r['completed_at'],masters=347,access=347,documents=10,generated_access=summary.get('generated_access',0)+45,catalog_revision=r['catalog_revision'],catalog_state=dict(catalog_revision=r['catalog_revision'],published_revision=r['catalog_revision'],dirty_since=None),source_disposition_review=dict(total=1419,candidate_rows=1384,retained_rows=35,pending_paths=34,excluded_paths=1,receipt=RECEIPT),remaining_completion_receipt=RECEIPT);summary['restore']['rows']=727
 # Ensure the on-disk SQLite file includes every committed row before checksumming it.
 for db in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
  c=sqlite3.connect(str(db));assert c.execute('pragma wal_checkpoint(TRUNCATE)').fetchone()[0]==0;c.close()
 h,checks=read_csv(U/'db-restore/checksums.csv');checks={x['path']:x for x in checks}
 for f in media(r):checks[f['path']]=dict(path=f['path'],sha256='sha256:'+f['sha256'],size_bytes=str(f['size_bytes']))
 summary['checksums']=len(set(checks)|set(CONTROLS));review.write_json(U/'db-restore/export-summary.json',summary)
 for path in CONTROLS:
  if path=='catalog-export-summary.json':continue
  checks[path]=dict(path=path,sha256='sha256:'+sha(U/path),size_bytes=str((U/path).stat().st_size))
 for v in export['outputs'].values():v['sha256']=checks[str(Path(v['path']).relative_to(U))]['sha256']
 review.write_json(U/'catalog-export-summary.json',export);checks['catalog-export-summary.json']=dict(path='catalog-export-summary.json',sha256='sha256:'+sha(U/'catalog-export-summary.json'),size_bytes=str((U/'catalog-export-summary.json').stat().st_size))
 write_csv(U/'db-restore/checksums.csv',h,sorted(checks.values(),key=lambda x:x['path']))
 print(json.dumps(r['validation'],indent=2),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','apply','verify']);args=parser.parse_args()
 if args.action=='prepare':prepare()
 elif args.action=='apply':
  r=review.readjson(R);make_media(r);register(r);sync_work_bindings(r);export(r);dispositions(r);finish(r)
 else:
  r=review.readjson(R);assert r==review.readjson(U/RECEIPT) and r['result']=='passed';print(json.dumps(verify(r),indent=2))
