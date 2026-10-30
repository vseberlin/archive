#!/usr/bin/env python3
"""Apply saved Paragon source decisions to the existing exact-path registers."""
import argparse
import copy
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import paragon_workbench as review

W, U, S = review.ROOT, review.ARCHIVE, review.SOURCE
RECEIPT = 'db-restore/manifests/drain-reconciliation.json'
DECISIONS = 'metadata/drain-candidate-decisions.json'
AUTHORIZATION = 'ok lets reconcile'

def read_csv(path):
    with path.open(newline='') as f:
        reader=csv.DictReader(f)
        return reader.fieldnames,list(reader)

def write_csv(path,fields,rows):
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fields,quoting=csv.QUOTE_ALL)
        writer.writeheader();writer.writerows(rows)

def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def stamp(path):
    st=path.stat();return [st.st_size,st.st_mtime_ns]

def connect(path,write=False):
    c=sqlite3.connect(f'file:{path}?mode={"rw" if write else "ro"}',uri=True)
    c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    return c

def snapshot(c):
    return {name:[tuple(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY rowid')]
            for name in ['archive_objects','entity_files','file_occurrences','content_blobs','entity_relations','catalog_state']}

def counters(rows):
    candidates=[r for r in rows if r['drain_state']=='candidate_after_separate_review']
    retained=[r for r in rows if r['drain_state']!='candidate_after_separate_review']
    return {'rows':len(rows),'candidate_rows':len(candidates),'retain_rows':len(retained),
            'candidate_source_bytes':sum(int(r['source_size_bytes']) for r in candidates),
            'retained_source_bytes':sum(int(r['source_size_bytes']) for r in retained),
            'preservation_status':dict(Counter(r['preservation_status'] for r in rows)),
            'source_roles':dict(Counter(r['source_role'] for r in rows))}

def plan():
    review.enable_drain_review(); health=review.check()
    fields,working=read_csv(W/'source-disposition.planned.csv')
    final_fields,final_rows=read_csv(U/'db-restore/manifests/source-disposition.csv')
    assert fields==final_fields
    evidence=json.loads((W/'drain-evidence.json').read_text())
    e_by={r['source_path']:r for r in evidence}
    by={r['source_path']:r for r in working};final_by={r['source_path']:r for r in final_rows}
    assert len(working)==len(by)==len(final_by)==len(e_by)==1419 and set(by)==set(final_by)==set(e_by)
    source_stamps={}
    for path,row in by.items():
        p=Path(path);assert p.is_relative_to(S) and not p.is_symlink() and p.resolve(strict=True)==p
        source_stamps[path]=stamp(p);e=e_by[path]
        assert source_stamps[path]==[e['source_size_bytes'],e['source_mtime_ns']],path
        assert row['source_sha256']==e['source_sha256'] and int(row['source_size_bytes'])==e['source_size_bytes']
        assert row['deletion_authorized']=='false' and row['unit']=='paragon'
    with review.connect() as work:
        saved=review.readable_decisions(work)
        assert saved==json.loads((W/'drain-candidate-decisions.json').read_text())
        logs=[dict(r) for r in work.execute("SELECT * FROM logbook WHERE action='review_source_candidate' ORDER BY at_utc,log_id")]
    output=copy.deepcopy(working);out_by={r['source_path']:r for r in output}
    checks={r['path']:r for r in read_csv(U/'db-restore/checksums.csv')[1]}
    catalog={r['final_relative_path']:r for r in read_csv(U/'CATALOG.csv')[1]}
    object_reviews=[o['current_review'] for o in saved['objects'] if o.get('current_review')]
    assert len(object_reviews)==health['saved_reviews']==14
    updates=[]
    for decision in object_reviews:
        members=decision['selected_master_members'];assert members
        paths=[str(Path(m['source_path']).relative_to(U)) for m in members]
        for member,path in zip(members,paths):
            assert checks[path]['sha256']==member['stored_sha256']
            assert catalog[path]['inventory']==decision['inventory']
            assert catalog[path]['file_role'] in {'preservation_master','preservation_surrogate'}
        selected=[c for c in decision['candidates'] if c['state']=='selected_master']
        assert len(selected)==1 and selected[0]['source_path']==decision['selected_master_path']
        log_ids=[r['log_id'] for r in logs if json.loads(r['after_json']).get('object_review')==decision]
        assert len(log_ids)==1
        for candidate in decision['candidates']:
            if candidate['state']!='redundant':continue
            row=out_by[candidate['source_path']]
            assert candidate['stored_sha256']==row['source_sha256']
            assert row['inventory']==decision['inventory']
            before=copy.deepcopy(row)
            row.update(source_role='redundant_candidate',final_path=' | '.join(paths),
                preservation_status='reviewed_not_selected',
                preservation_basis='Vladimir saved Keep current Master in the source-comparison workbench. This exact alternative is redundant; the selected Master member(s) remain accessioned. Original source retained.',
                final_sha256=' | '.join(checks[p]['sha256'] for p in paths),exact_copy_proven='false',
                drain_state='candidate_after_separate_review',deletion_authorized='false',
                notes='Current saved decision: '+log_ids[0]+'. Readable decision: '+DECISIONS+
                      '. No source deletion authorized. Previous register note (historical): '+row['notes'])
            if not candidate.get('decoded',True):
                row['notes']+=' The TIFF remains unreadable in the review reader; redundancy follows the explicit human decision, not the decode failure.'
            e=e_by[row['source_path']]
            e['current_review_log_ids']=log_ids;e['current_review_inventory']=decision['inventory']
            e['selected_master_paths']=paths
            updates.append({'inventory':decision['inventory'],'source_path':row['source_path'],
                            'decision_log_id':log_ids[0],'before_work':before,'after':copy.deepcopy(row)})
    assert len(updates)==17
    # Read the actual archive bindings; the final CSV is not the sole proof of a selected Master.
    final=connect(U/'db-restore/RESTORE.sqlite')
    active={r['relative_path']:dict(r) for r in final.execute(
        "SELECT ef.entity_id,ef.role,fo.relative_path,fo.content_digest FROM entity_files ef "
        "JOIN file_occurrences fo USING(occurrence_id) WHERE ef.status='active' AND fo.occurrence_kind='archive'")}
    referenced=set()
    for row in output:
        if row['final_path']:
            paths=row['final_path'].split(' | ');hashes=row['final_sha256'].split(' | ')
            assert len(paths)==len(hashes)
            for path,digest in zip(paths,hashes):
                p=U/path;assert p.resolve(strict=True).is_relative_to(U) and not p.is_symlink()
                assert checks[path]['sha256']==digest and p.stat().st_size==int(checks[path]['size_bytes'])
                referenced.add(path)
        if row['drain_state']=='candidate_after_separate_review':
            assert row['final_path'] and row['final_sha256']
            assert row['preservation_status'] in {'preserved_exact_copy','preserved_as_metadata_enriched_master','reviewed_not_selected'}
            links=e_by[row['source_path']].get('other_unit_links',[])
            assert not any(link.get('inventory') or link.get('final_path') or
                link.get('preservation_status') not in ('not_assessed','preservation_unresolved','') for link in links)
    for update in updates:
        for path in update['after']['final_path'].split(' | '):
            assert active[path]['entity_id']==catalog[path]['record_key']
            assert active[path]['role'] in {'preservation_master','preservation_surrogate'}
    existing={r['log_id']:dict(r) for r in final.execute('SELECT * FROM logbook')}
    missing=[r for r in logs if r['log_id'] not in existing]
    for row in logs:
        if row['log_id'] in existing:assert row==existing[row['log_id']]
    relevant_ids={d['entity_id'] for d in object_reviews}|{'ISS-2026-JS55-S','ISS-2026-T8XE-H'}
    assert all(json.loads(r['after_json']).get('entity_id') in relevant_ids for r in missing)
    model=snapshot(final);memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
    final.backup(memory);final.close()
    for row in missing:
        memory.execute(f'INSERT INTO logbook ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',tuple(row.values()))
    assert review.readable_decisions(memory)==saved and snapshot(memory)==model
    assert not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    stats=counters(output);assert stats['candidate_rows']==287 and stats['retain_rows']==1132
    assert len([r for r in output if r['drain_state']!=by[r['source_path']]['drain_state']])==1
    holds=[r['source_path'] for r in output if r['drain_state']=='retain_not_preserved' and final_by[r['source_path']]['drain_state']=='candidate_after_separate_review']
    assert len(holds)==6
    return dict(fields=fields,rows=output,evidence=evidence,saved=saved,missing_logs=missing,
        source_stamps=source_stamps,model=model,stats=stats,updates=updates,holds=holds,
        final_changed_rows=sum(r!=final_by[r['source_path']] for r in output),
        referenced_final_paths=sorted(referenced),before_work=counters(working),before_final=counters(final_rows),
        work_inputs={str(p):stamp(p) for p in [W/'drain-candidate-decisions.json',W/'source-disposition.planned.csv',W/'drain-evidence.json']})

def apply():
    assert not (U/RECEIPT).exists(), 'Completed reconciliation exists; verify it instead of repeating.'
    p=plan(); now=datetime.now(timezone.utc).isoformat()
    original_files={str(x.relative_to(U)):stamp(x) for x in U.rglob('*') if x.is_file() and not x.name.endswith(('-wal','-shm'))}
    original_dirs={str(x.relative_to(U)) for x in U.rglob('*') if x.is_dir()}
    print('Preflight passed:',json.dumps(p['stats']),flush=True)
    for path,prior in p['work_inputs'].items():assert stamp(Path(path))==prior
    final=connect(U/'db-restore/RESTORE.sqlite',True)
    with final:
        for row in p['missing_logs']:
            final.execute(f'INSERT INTO logbook ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',tuple(row.values()))
        assert snapshot(final)==p['model']
        assert review.readable_decisions(final)==p['saved']
        assert final.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not final.execute('PRAGMA foreign_key_check').fetchall()
    sql='\n'.join(final.iterdump())+'\n';final.close()
    memory=sqlite3.connect(':memory:');memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    write_csv(W/'source-disposition.planned.csv',p['fields'],p['rows'])
    write_csv(U/'db-restore/manifests/source-disposition.csv',p['fields'],p['rows'])
    write_csv(W/'drain-list.csv',p['fields'],[r for r in p['rows'] if r['drain_state']=='candidate_after_separate_review'])
    write_csv(W/'retain-review.csv',p['fields'],[r for r in p['rows'] if r['drain_state']!='candidate_after_separate_review'])
    write_json(W/'drain-evidence.json',p['evidence']);write_json(U/'db-restore/manifests/drain-evidence.json',p['evidence'])
    write_json(U/DECISIONS,p['saved'])
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W/'drain-evidence.json').read_bytes()==(U/'db-restore/manifests/drain-evidence.json').read_bytes()
    record={'unit':'paragon','authorization':AUTHORIZATION,'completed_at':now,'result':'passed',
        **p['stats'],'saved_object_reviews':14,'reviewed_redundant_sources':17,
        'review_logs_copied_to_final_sqlite':len(p['missing_logs']),'copied_log_ids':[r['log_id'] for r in p['missing_logs']],
        'work_before':p['before_work'],'final_before':p['before_final'],
        'final_register_rows_updated':p['final_changed_rows'],'retained_holds_removed_from_final_candidates':p['holds'],
        'saved_review_updates':p['updates'],'source_rows_validated':len(p['source_stamps']),
        'final_file_bindings_validated':len(p['referenced_final_paths']),
        'registers_agree':True,'readable_decisions_match_work_and_final_sqlite':True,'sql_restore_verified':True,
        'master_selections_changed':False,'accession_performed':False,'derivatives_generated':0,
        'source_hashes_reused':True,'final_media_rehashed':0,'sources_modified':False,'source_deletion_performed':False,
        'deletion_authorized':False,'publication_performed':False,'sqlite_schema_changed':False,
        'directory_structure_unchanged':True,'existing_media_and_catalogues_unchanged':True,
        'source_disposition':'db-restore/manifests/source-disposition.csv','readable_decisions':DECISIONS,
        'checksums_file':'db-restore/checksums.csv','source_mount':review.mount_guard()}
    summary=json.loads((W/'drain-review-summary.json').read_text())
    summary.update(p['stats']);summary.update(updated_at=now,result='passed',current_register='Work and final source-disposition registers agree.',
        installed_final_checkpoint={'candidate_rows':287,'retain_rows':1132,'source_disposition':str(U/'db-restore/manifests/source-disposition.csv'),'reconciliation_receipt':RECEIPT},
        reconciliation={'receipt':RECEIPT,'saved_reviews':14,'reviewed_redundant_sources':17,'deletion_authorized':False})
    write_json(W/'drain-review-summary.json',summary)
    validation=json.loads((W/'drain-review-validation.json').read_text())
    validation.update(checked_at=now,result='passed',drain_candidates=287,retain_rows=1132,
        canonical_csv_matches_reviewed_plan=True,source_evidence_matches=True,source_stamps_unchanged=True,
        existing_accession_media_and_catalogue_stamps_unchanged=False,
        existing_accession_media_and_catalogue_rows_unchanged=True,
        reconciliation_receipt=RECEIPT,source_decision_log_rows_copied=len(p['missing_logs']),
        active_master_and_access_files=484,sqlite_integrity='ok',sqlite_foreign_key_errors=0)
    # The final catalogue revision is unchanged: source log inserts do not alter Master assignments.
    export=json.loads((U/'db-restore/export-summary.json').read_text())
    export.update(updated_at=now,source_disposition_review={'rows':1419,'candidates':287,'retain':1132,'receipt':RECEIPT})
    old=(U/'00_README.md').read_text();title,body=old.split('\n',1)
    text=f'''\n\n## Current source register — reconciled 2026-09-17

The work and final registers now agree: **287 candidate files and 1,132 retained
files**, one row per exact source path (1,419 total). All 14 saved version reviews
are applied, covering 17 redundant alternatives. The VLV-1129 TIFF alternative
is now correctly a candidate. Six unresolved coverage/identity holds have been
removed from the older final candidate list and remain retained. The accessioned
VLV-1130 carrier section remains correctly bound to its new Master member.

Current receipt: `{RECEIPT}`. Saved reviews: `{DECISIONS}` and the same existing
source-review log entries in RESTORE.sqlite/RESTORE.sql. Exact-path evidence and
stored hashes were verified without rehashing sources or existing payloads.
No Master, media file, identity, relationship, or catalogue data row changed.
All source files remain retained and `deletion_authorized=false`; candidate
status does not permit deletion. The retained files still require resolution.
Earlier receipts below are historical checkpoints.
'''
    (U/'00_README.md').write_text(title+text+body)
    controls=['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql','db-restore/manifests/source-disposition.csv',
              'db-restore/manifests/drain-evidence.json',DECISIONS,'00_README.md',RECEIPT,'db-restore/export-summary.json']
    kh,checks=read_csv(U/'db-restore/checksums.csv');checks={r['path']:r for r in checks}
    expected_count=len(set(checks)|set(controls));export['checksums']=expected_count
    validation['checksums']=expected_count
    write_json(U/'db-restore/export-summary.json',export)
    # Verify the completed data before writing the final receipt and hashing only changed controls.
    with review.connect() as c:assert review.readable_decisions(c)==p['saved']
    assert {str(x.relative_to(U)) for x in U.rglob('*') if x.is_dir()}==original_dirs
    for path,prior in original_files.items():
        if path not in controls and path!='db-restore/checksums.csv':assert stamp(U/path)==prior,path
    for path,prior in p['source_stamps'].items():assert stamp(Path(path))==prior,path
    assert review.check()['saved_reviews']==14
    write_json(W/'drain-reconciliation.json',record);write_json(U/RECEIPT,record)
    for relative in controls:
        path=U/relative
        with path.open('rb') as f:digest='sha256:'+hashlib.file_digest(f,'sha256').hexdigest()
        checks[relative]={'path':relative,'sha256':digest,'size_bytes':str(path.stat().st_size)}
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checks.values(),key=lambda r:r['path']))
    assert len(checks)==expected_count
    for row in checks.values():assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    write_json(W/'drain-review-validation.json',validation)
    print(json.dumps({'result':'passed',**p['stats'],'review_logs_copied':len(p['missing_logs']),
        'work_final_registers_equal':True,'deletion_authorized':False},indent=2),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.apply:apply()
    else:
        p=plan();print(json.dumps({'ready':True,**p['stats'],'review_logs_to_copy':len(p['missing_logs']),
            'final_rows_to_update':p['final_changed_rows'],'retained_holds':p['holds'],'source_stamps_verified':len(p['source_stamps']),
            'final_file_bindings_verified':len(p['referenced_final_paths'])},indent=2))
