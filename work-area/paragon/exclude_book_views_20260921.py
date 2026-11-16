"""Apply Vladimir's exact book-family exclusion using existing records only."""
import copy,json,sqlite3,hashlib,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import paragon_workbench as app
import retained_candidates as review
from accession_carrier_section import read_csv,write_csv,stamp
from reconcile_drain_decisions import counters
W,U=review.ROOT,review.ARCHIVE
NAME='book-views-exclusion-20260921.json'
REL='db-restore/manifests/'+NAME
NOTE=('Vladimir confirmed: Keep all those book views outside Paragon. Excluded from '
      'Paragon accession, review and drain proposals, including their documented copies '
      'and derivatives. Retain every source for the other unit; its name is unspecified. '
      'Earlier redundancy evidence remains historical evidence, not a Paragon drain instruction. '
      'No file move, deletion, new accession or ownership transfer. '+REL)
CONTROL=['db-restore/manifests/'+p for p in ['source-disposition.csv','drain-list.csv',
 'drain-evidence.json','accession-remaining-20260921.json','accession-remaining-20260921.csv',
 'ACCESSION_REMAINING_20260921.md',NAME]]+['00_README.md','db-restore/export-summary.json']
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def verify(r):
 app.mount_guard();paths=set(r['excluded_paths']);assert len(paths)==25
 for rel,st in r['archive_stamps'].items():
  if rel not in set(CONTROL)|{'db-restore/checksums.csv'}:assert stamp(U/rel)==st,rel
 assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==r['archive_directories']
 for e in r['source_evidence']:
  p=Path(e['source_path']);assert p.resolve(strict=True)==p and p.is_relative_to(app.SOURCE)
  assert stamp(p)==[e['source_size_bytes'],e['source_mtime_ns']],str(p)
 for final,work in [('source-disposition.csv','source-disposition.planned.csv'),('drain-list.csv','drain-list.csv'),('drain-evidence.json','drain-evidence.json'),('accession-remaining-20260921.json','accession-remaining-20260921.json'),('accession-remaining-20260921.csv','accession-remaining-20260921.csv'),('ACCESSION_REMAINING_20260921.md','ACCESSION_REMAINING_20260921.md')]:
  assert (U/'db-restore/manifests'/final).read_bytes()==(W/work).read_bytes()
 rows=read_csv(W/'source-disposition.planned.csv')[1];drain=read_csv(W/'drain-list.csv')[1]
 assert len(rows)==1419 and len(drain)==1368 and not paths&{x['source_path'] for x in drain}
 assert all(x['deletion_authorized']=='false' for x in rows)
 pending=review.readjson(review.PENDING_ACCESSION)
 assert len(pending['files'])==26 and {x['source_path'] for x in pending['excluded_files']}==paths
 assert {x['source_path'] for x in pending['files']+pending['excluded_files']}=={x['source_path'] for x in rows if x['drain_state']!='candidate_after_separate_review'}
 with app.connect() as c:
  saved=review.saved_reviews(c);assert saved=={**r['prior_reviews'],**r['updated_reviews']}
  assert review.readable_decisions(c)==review.readjson(review.DECISIONS)
  proposals=review.drain_proposals(c);assert proposals==review.readjson(review.DRAIN_PROPOSALS)
  assert not paths&{p['path'] for p in proposals['files']}
  for d in saved.values():
   if any(f['path'] in paths for f in d['files']):assert d['action']=='ignore'
  assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
  idx=review.index(c);assert idx['pending_count']==26 and not any(p['remaining'] for p in idx['pictures'])
  health=review.check(c)
 return dict(ok=True,excluded_source_paths=25,newly_excluded_pending_paths=8,duplicate_paths_removed_from_drain=16,
   pending_paths=26,drain_paths=1368,undecided_pictures=0,source_stamps_unchanged=1419,
   accessioned_media_and_metadata_unchanged=True,directory_structure_unchanged=True,
   sqlite_and_readable_decisions_agree=True,source_modified=False,deletion_authorized=False,workbench_check=health)
def apply():
 app.mount_guard();assert not (W/NAME).exists() and not (U/REL).exists(),'Already applied; inspect receipt.'
 batch=review.readjson(W/'remaining-accession-completion-20260921.json')
 targets={p['source'] for p in batch['files'] if p['index'] in {39,40,41,72,101,102,103,104}}
 with app.connect() as c:
  before=review.saved_reviews(c);assert review.readable_decisions(c)==review.readjson(review.DECISIONS)
 pending=review.readjson(review.PENDING_ACCESSION);assert len(pending['files'])==34 and len(pending['excluded_files'])==1
 assert targets<={f['source_path'] for f in pending['files']}
 paths=targets|{f['source_path'] for f in pending['excluded_files']};links={}
 while True:
  added=set()
  for d in before.values():
   for f in d['files']:
    if f['path'] in paths or f['state'] not in {'redundant','noise_drain'}:continue
    t=f.get('similarity_evidence',{}).get('comparison_target',{})
    refs={f.get('derivative_evidence',{}).get('coverage_target',{}).get('path'),f.get('duplicate_evidence',{}).get('retained_source_path'),t.get('path'),t.get('target')}
    if refs&paths:added.add(f['path']);links[f['path']]=sorted(refs&paths)
  if not added:break
  paths|=added
 assert len(paths)==25 and len(links)==16
 at=datetime.now(timezone.utc).isoformat();updated={};owners={f['path']:(k,f) for k,d in before.items() for f in d['files']}
 for key,old in before.items():
  included={f['path'] for f in old['files']}&paths
  if not included or old['action']=='ignore':continue
  assert included=={f['path'] for f in old['files']} and not old.get('selected_master_path')
  d=copy.deepcopy(old);d.update(action='ignore',note=old.get('note','')+'\n'+NOTE,actor='Vladimir',reviewed_at=at)
  for f in d['files']:f['note']=f.get('note','')+'\n'+NOTE
  updated[key]=d
 assert len(updated)==9
 h,rows=read_csv(W/'source-disposition.planned.csv');assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
 old_rows=copy.deepcopy(rows);by_path={x['source_path']:x for x in rows};assert paths<=set(by_path)
 assert sum(by_path[p]['drain_state']=='candidate_after_separate_review' for p in paths)==16
 for p in paths:
  row=by_path[p];assert not row['final_path'] and not row['final_sha256']
  row.update(drain_state='retain_not_preserved',preservation_basis=NOTE,notes=row['notes']+'; '+NOTE)
 evidence=review.readjson(W/'drain-evidence.json')
 r=dict(authorization='Keep all those book views outside Paragon',at=at,result='prepared',
   excluded_paths=sorted(paths),previously_pending_paths=sorted(targets),copy_evidence_links=links,
   prior_reviews=before,updated_reviews=updated,prior_disposition=[x for x in old_rows if x['source_path'] in paths],
   source_evidence=evidence,archive_stamps={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))},
   archive_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
   other_unit_name=None,accession_performed=False,source_modified=False,deletion_authorized=False)
 for e in evidence:
  p=Path(e['source_path']);assert stamp(p)==[e['source_size_bytes'],e['source_mtime_ns']]
 review.write_json(W/NAME,r)
 with app.connect(False) as c:
  assert review.saved_reviews(c)==before
  for d in updated.values():review.record_review(c,d)
 for p in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(p,h,rows)
 for p in [W/'drain-list.csv',U/'db-restore/manifests/drain-list.csv']:write_csv(p,h,[x for x in rows if x['drain_state']=='candidate_after_separate_review'])
 ev_after=copy.deepcopy(evidence)
 for e in ev_after:
  if e['source_path'] in paths:e['paragon_exclusion']=dict(authorization=r['authorization'],reason=NOTE,receipt=REL,other_unit_name=None,deletion_authorized=False)
 for p in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(p,ev_after)
 existing={f['source_path']:f for f in pending['files']+pending['excluded_files']};fh,_=read_csv(W/'accession-remaining-20260921.csv')
 pending['files']=[f for f in pending['files'] if f['source_path'] not in paths];pending['excluded_files']=[]
 for p in sorted(paths):
  k,f=owners[p];d=updated.get(k,before[k]);e=copy.deepcopy(existing.get(p,{field:'' for field in fh}))
  e.update(source_path=p,source_sha256=f['sha256'],saved_state=f['state'],inventory=d['inventory'],case_key=k,
    selected_master_path=d.get('selected_master_path',''),metadata_reviewed=d.get('metadata_reviewed',False),
    review_note=NOTE,exclusion_reason=NOTE,deletion_authorized=False)
  pending['excluded_files'].append(e)
 pending.update(counts=dict(Counter(f['saved_state'] for f in pending['files'])),scope='26 Paragon sources remain pending; 25 exact book-family source paths are excluded for another unit.',book_ownership_exclusion_receipt=REL)
 md='# Paragon remaining specimen questions\n\n21 September 2026. **26 source paths remain pending.** Vladimir excluded all related patent/registration book views from Paragon: eight pending views and sixteen documented copies, plus the previously excluded picture. All 25 source paths remain retained for the unnamed other unit. The Paragon drain list now contains **1,368 paths**. No deletion authorized.\n\nThe completed accession remains unchanged: 45 images, nine new objects and 36 additional views.\n\n| Pending source | Missing evidence |\n| --- | --- |\n'
 for f in pending['files']:md+='| '+f['source_path'].replace('|','\\|')+' | '+f['review_note'].replace('|','\\|')+' |\n'
 md+='\n## Excluded book views and copies\n\nReceipt: `'+REL+'`. No other-unit name or ownership transfer inferred.\n\n'
 for p in sorted(paths):md+='- `'+p+'`\n'
 for root in [W,U/'db-restore/manifests']:
  review.write_json(root/'accession-remaining-20260921.json',pending);write_csv(root/'accession-remaining-20260921.csv',fh,[{k:f.get(k,'') for k in fh} for f in pending['files']]);(root/'ACCESSION_REMAINING_20260921.md').write_text(md)
 summary=review.readjson(W/'drain-review-summary.json');summary.update(counters(rows),book_ownership_exclusion_receipt=REL,current_register='1368 drain candidates; 26 pending sources; 25 other-unit book source paths excluded; no deletion.');review.write_json(W/'drain-review-summary.json',summary)
 export=review.readjson(U/'db-restore/export-summary.json');export.update(updated_at=at,book_ownership_exclusion_receipt=REL)
 export['source_disposition_review'].update(candidate_rows=1368,retained_rows=51,pending_paths=26,excluded_paths=25,receipt=REL)
 old_checks=read_csv(U/'db-restore/checksums.csv');export['checksums']=len(set(x['path'] for x in old_checks[1])|set(CONTROL));review.write_json(U/'db-restore/export-summary.json',export)
 intro='## Book ownership confirmed, 21 September 2026\n\nVladimir excluded all patent/registration book views from Paragon accession and draining. Eight pending views and sixteen documented copies join the previously excluded picture: **25 exact source paths retained for the unnamed other unit**. The current pending list has **26 sources**; the drain list has **1,368 paths**. Existing accessioned objects, Masters, metadata and derivatives are unchanged. No deletion or source movement. Receipt: `'+REL+'`. Earlier counts below are historical.\n\n'
 for p in [W/'00_README.md',U/'00_README.md']:
  title,body=p.read_text().split('\n',1);p.write_text(title+'\n\n'+intro+body)
 review.evidence.cache_clear()
 with app.connect() as c:review.export_decisions(c)
 r['validation']=verify(r);r['result']='passed';review.write_json(W/NAME,r);review.write_json(U/REL,r)
 ch,checks=old_checks;checks={x['path']:x for x in checks}
 for rel in CONTROL:checks[rel]=dict(path=rel,sha256='sha256:'+sha(U/rel),size_bytes=str((U/rel).stat().st_size))
 write_csv(U/'db-restore/checksums.csv',ch,sorted(checks.values(),key=lambda x:x['path']))
 print(json.dumps(r['validation'],indent=2))
if __name__=='__main__':
 if sys.argv[1]=='apply':apply()
 elif sys.argv[1]=='verify':
  r=review.readjson(W/NAME);assert r['result']=='passed' and r==review.readjson(U/REL)
  print(json.dumps(verify(r),indent=2))
 else:raise ValueError('Use apply once or verify')
