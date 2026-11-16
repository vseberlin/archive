#!/usr/bin/env python3
"""Apply Vladimir's existing-hash and recorded-unreadable drain rules, without content rechecks."""
import argparse
import copy
import hashlib
import json
import sqlite3
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import paragon_workbench as app
import retained_candidates as review
from reconcile_drain_decisions import read_csv,write_csv,stamp,connect,snapshot,counters

W,U=review.ROOT,review.ARCHIVE
RECEIPT='db-restore/manifests/duplicates-and-unreadable-drain.json'
AUTHORIZATION='equal hashes are going to drain without extra checks; damaged img go to drain list without checks'
CONTROLS=['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql','db-restore/manifests/source-disposition.csv',
    'db-restore/manifests/drain-evidence.json','db-restore/export-summary.json','00_README.md',RECEIPT]


def plan():
    app.mount_guard()
    assert not (U/RECEIPT).exists(),'Already applied; use --verify.'
    with app.connect() as con:
        saved=review.saved_reviews(con);cases=review.cases(con);proposals=review.drain_proposals(con)['files']
        prior_logs=[dict(row) for row in con.execute('SELECT * FROM logbook WHERE action=? ORDER BY at_utc,log_id',(review.ACTION,))]
    con.close()
    assert len(saved)==132
    targets={p['derivative_evidence']['coverage_target']['path'] for p in proposals if p['covering_source_awaits_accession']}
    assert len(targets)==81
    unresolved={};groups=defaultdict(list);case_by={c['key']:c for c in cases}
    for case in cases:
        states={f['path']:f['state'] for f in saved.get(case['key'],{}).get('files',[])}
        for source in case['sources']:
            if states.get(source['path'],'unresolved')=='unresolved':
                unresolved[source['path']]=source|{'case_key':case['key']};groups[source['sha256']].append(source)
    assert len(unresolved)==938
    changes={}
    for digest,group in groups.items():
        if len(group)<2:continue
        preferred=[s for s in group if s['path'] in targets];assert len(preferred)<=1
        keep=preferred[0] if preferred else min(group,key=lambda s:s['path'])
        for source in group:
            if source['path']==keep['path']:continue
            changes[source['path']]=dict(kind='duplicate',sha256=digest,retained_source_path=keep['path'],
                retained_source_sha256=keep['sha256'],basis='Equal stored SHA-256; one unresolved source copy is retained. No archival Master is selected.',receipt=RECEIPT)
    errors={p:e['error'] for e in json.loads((W/'retained-visual-comparison.json').read_text())['errors'] for p in e['paths']}
    for path,source in unresolved.items():
        if source['decoded'] or source['format'] not in ('JPG','TIF'):continue
        assert path not in changes and path in errors
        changes[path]=dict(kind='unreadable',sha256=source['sha256'],
            recorded_error=errors[path],evidence_report='retained-visual-comparison.json',
            basis='Vladimir instructed that recorded damaged/unreadable images go to the drain list without another check.',receipt=RECEIPT)
    assert Counter(v['kind'] for v in changes.values())=={'duplicate':217,'unreadable':19}
    assert not targets&changes.keys()
    for d in changes.values():
        if d['kind']=='duplicate':assert d['retained_source_path'] not in changes
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv')
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    by={r['source_path']:r for r in rows};updates=[]
    now=datetime.now(timezone.utc).isoformat();new_reviews=[]
    for key in sorted({unresolved[p]['case_key'] for p in changes}):
        obj=case_by[key]
        if key in saved:
            result=copy.deepcopy(saved[key]);assert result['action']=='retain' and not result['selected_master_path']
        else:
            result=review.build_review(obj,dict(action='retain',metadata=obj['proposed_metadata']),'Vladimir')
        for f in result['files']:
            if f['path'] not in changes:continue
            d=changes[f['path']];assert f['state']=='unresolved' and f['sha256']==d['sha256']
            f['state']='redundant' if d['kind']=='duplicate' else 'noise_drain'
            f['duplicate_evidence' if d['kind']=='duplicate' else 'rejection_evidence']=d
        result.update(reviewed_at=now,actor='Vladimir',note=(result['note']+'\n' if result['note'] else '')+
            'Applied explicit hash-duplicate/unreadable-file instruction without content rechecks. Exact paths and retained copies: '+RECEIPT+'. No deletion authorized.')
        new_reviews.append(result)
    for path,decision in changes.items():
        old=by[path];assert old['drain_state']=='retain_not_preserved' and old['source_sha256']==decision['sha256']
        row=copy.deepcopy(old);duplicate=decision['kind']=='duplicate'
        row.update(source_role='redundant_candidate' if duplicate else 'noise',final_path='',final_sha256='',
            preservation_status='reviewed_not_selected' if duplicate else 'reviewed_noise_not_accessioned',
            preservation_basis=decision['basis'],exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',
            notes=('Identical source retained at '+decision['retained_source_path']+'. ' if duplicate else 'Rejected using the recorded unreadable-image result. ')+
                'Receipt: '+RECEIPT+'. Existing other-unit references remain recorded. No final preservation claim or source deletion authorization.')
        updates.append(dict(source_path=path,before=old,after=row,evidence=decision))
    expected=saved|{r['case_key']:r for r in new_reviews}
    assert len(expected)==204 and len(new_reviews)==159
    small_remaining={p:s for p,s in unresolved.items() if p not in changes and s.get('width') and max(s['width'],s['height'])<=1600}
    report=json.loads((W/'retained-derivative-check.json').read_text())
    reasons={p:g.get('reason','') for g in report['groups'] for p in g['source_paths']}
    small_summary=dict(before=306,identical_extra_copies_listed=105,remaining=len(small_remaining),
        prior_comparison_results=dict(Counter(reasons[p] for p in small_remaining)),
        files=[dict(path=p,sha256=s['sha256'],width=s['width'],height=s['height'],case_key=s['case_key'],prior_comparison_result=reasons[p]) for p,s in small_remaining.items()])
    assert small_summary['remaining']==201
    return dict(fields=fields,rows=rows,prior_logs=prior_logs,prior_reviews=saved,expected_reviews=expected,
        new_reviews=new_reviews,updates=updates,covering_originals=sorted(targets),small_images=small_summary,at_utc=now)


def verify():
    receipt=json.loads((U/RECEIPT).read_text())
    assert receipt==json.loads((W/Path(RECEIPT).name).read_text()) and receipt['result']=='passed'
    app.mount_guard()
    rows=read_csv(U/'db-restore/manifests/source-disposition.csv')[1];by={r['source_path']:r for r in rows}
    assert len(rows)==1419 and sum(r['drain_state']=='candidate_after_separate_review' for r in rows)==714
    assert all(r['deletion_authorized']=='false' for r in rows)
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W/'drain-evidence.json').read_bytes()==(U/'db-restore/manifests/drain-evidence.json').read_bytes()
    assert read_csv(W/'drain-list.csv')[1]==[r for r in rows if r['drain_state']=='candidate_after_separate_review']
    for u in receipt['updates']:
        assert by[u['source_path']]==u['after']
        if u['evidence']['kind']=='duplicate':
            assert by[u['evidence']['retained_source_path']]['drain_state']=='retain_not_preserved'
            assert by[u['evidence']['retained_source_path']]['source_sha256']==u['after']['source_sha256']
    assert all(by[p]['drain_state']=='retain_not_preserved' for p in receipt['covering_originals'])
    for path,prior in receipt['original_files'].items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==prior,path
    assert stamp(W/'retain-review.csv')==receipt['stable_review_source_stamp']
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==receipt['original_directories']
    for path in [W/'work/catalog.sqlite',U/'db-restore/RESTORE.sqlite']:
        con=connect(path)
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
        assert review.saved_reviews(con)==receipt['saved_reviews_after']
        con.close()
    with app.connect() as con:
        health=review.check(con);index=review.index(con);proposals=review.drain_proposals(con)['files']
    con.close()
    assert len(proposals)==391 and all(p['in_drain_register'] and not p['awaiting_accession'] for p in proposals)
    assert sum(c['unresolved_source_count'] for c in index['cases'])==702
    for row in read_csv(U/'db-restore/checksums.csv')[1]:assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    return dict(ok=True,identical_extra_copies_added=217,recorded_unreadable_images_added=19,
        drain_candidates=714,retained_paths=705,unresolved_files=702,explicitly_retained=3,
        small_images_remaining=201,covering_originals_retained=81,saved_case_reviews=health['saved_reviews'],
        source_content_rechecked=False,source_files_rehashed=0,source_files_decoded=0,
        source_modified=False,deletion_authorized=False,media_catalogues_and_master_choices_unchanged=True)


def apply():
    p=plan();original={str(f.relative_to(U)):stamp(f) for f in U.rglob('*') if f.is_file() and not f.name.endswith(('-wal','-shm','-journal'))}
    receipt=dict(authorization=AUTHORIZATION,result='pending',completed_at=p['at_utc'],updates=p['updates'],
        original_files=original,original_directories=sorted(str(f.relative_to(U)) for f in U.rglob('*') if f.is_dir()),
        stable_review_source_stamp=stamp(W/'retain-review.csv'),covering_originals=p['covering_originals'],
        small_images=p['small_images'],saved_reviews_after=p['expected_reviews'],
        source_content_rechecked=False,source_files_rehashed=0,source_files_decoded=0,
        source_modified=False,deletion_authorized=False,accession_performed=False)
    review.write_json(W/Path(RECEIPT).name,receipt)
    new_logs=[dict(log_id='LOG-'+uuid4().hex.upper(),at_utc=p['at_utc'],actor='Vladimir',unit_id='WF-PARAGON',
        action=review.ACTION,entity_type='reconciliation_decision',entity_id=r['case_key'],
        before_json=json.dumps(p['prior_reviews'].get(r['case_key'],{}),ensure_ascii=False),
        after_json=json.dumps(r,ensure_ascii=False),evidence=RECEIPT,note=AUTHORIZATION) for r in p['new_reviews']]
    for path in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        con=connect(path,True);before=snapshot(con)
        with con:
            logs=(p['prior_logs'] if path.is_relative_to(U) else [])+new_logs
            for log in logs:
                existing=con.execute('SELECT * FROM logbook WHERE log_id=?',(log['log_id'],)).fetchone()
                if existing:assert dict(existing)==log;continue
                con.execute('INSERT INTO logbook ('+','.join(log)+') VALUES ('+','.join('?' for _ in log)+')',tuple(log.values()))
            assert snapshot(con)==before
            assert review.saved_reviews(con)==p['expected_reviews']
        con.close()
    con=connect(U/'db-restore/RESTORE.sqlite');sql='\n'.join(con.iterdump())+'\n';con.close()
    memory=sqlite3.connect(':memory:');memory.executescript(sql)
    assert memory.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not memory.execute('PRAGMA foreign_key_check').fetchall();memory.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    by={u['source_path']:u for u in p['updates']}
    rows=[by[r['source_path']]['after'] if r['source_path'] in by else r for r in p['rows']]
    for path in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(path,p['fields'],rows)
    write_csv(W/'drain-list.csv',p['fields'],[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    evidence=json.loads((W/'drain-evidence.json').read_text())
    for e in evidence:
        if e['source_path'] in by:
            u=by[e['source_path']]
            e.update(drain_decision_receipt=RECEIPT,drain_decision_evidence=u['evidence'],
                final_path='',final_sha256='',preservation_status=u['after']['preservation_status'])
    for path in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(path,evidence)
    stats=counters(rows);assert stats['candidate_rows']==714 and stats['retain_rows']==705
    summary=json.loads((W/'drain-review-summary.json').read_text());summary.update(stats)
    summary.update(updated_at=p['at_utc'],duplicates_and_unreadable_receipt=RECEIPT,current_register='Work and final registers agree: 714 drain candidates and 705 retained sources. No deletion authorized.')
    review.write_json(W/'drain-review-summary.json',summary)
    title,body=(U/'00_README.md').read_text().split('\n',1)
    (U/'00_README.md').write_text(title+'\n\n## Current source register — duplicate/unreadable decisions, 18 September 2026\n\nVladimir instructed that equal-hash extra copies and recorded unreadable images\ngo to the drain list without further checks. Added 217 identical extra copies\nwhile retaining one source path per hash, plus 19 recorded unreadable images.\nNo source was rehashed or decoded again. Current register: 714 candidates and\n705 retained paths (702 unresolved plus 3 explicitly retained JPEGs). The 81\ncovering originals remain retained. The 306 small-image paths included 105 exact\nextra copies, leaving 201 distinct small images for remaining source review.\nNo Master, media, catalogue data or relationship changed. Nothing was deleted.\nReceipt: `'+RECEIPT+'`. Earlier counts below are historical.\n\n'+body)
    kh,checkrows=read_csv(U/'db-restore/checksums.csv');checks={r['path']:r for r in checkrows}
    export=json.loads((U/'db-restore/export-summary.json').read_text())
    export.update(updated_at=p['at_utc'],source_disposition_review=dict(total=1419,candidate_rows=714,retained_rows=705,receipt=RECEIPT),
        checksums=len(set(checks)|set(CONTROLS)),duplicates_and_unreadable_receipt=RECEIPT)
    review.write_json(U/'db-restore/export-summary.json',export)
    for path,prior in original.items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==prior,path
    receipt.update(result='passed',source_register=stats,media_and_catalogues_unchanged=True,sql_restore_verified=True)
    for path in [W/Path(RECEIPT).name,U/RECEIPT]:review.write_json(path,receipt)
    for relative in CONTROLS:
        with (U/relative).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        checks[relative]=dict(path=relative,sha256='sha256:'+digest,size_bytes=str((U/relative).stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checks.values(),key=lambda r:r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:review.export_decisions(con)
    con.close()
    result=verify();review.write_json(W/'duplicates-and-unreadable-validation.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    if args.apply:apply()
    elif args.verify:print(json.dumps(verify(),indent=2))
    else:
        p=plan();print(json.dumps(dict(ready=True,extra_identical_copies=217,recorded_unreadable_images=19,
            source_content_rechecked=False,new_review_cases=72,updated_cases=len(p['new_reviews']),small_images_remaining=201),indent=2))
