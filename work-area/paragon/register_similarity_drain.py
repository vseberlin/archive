#!/usr/bin/env python3
"""Register approved similarity-based extra copies; keep source representatives."""
import argparse
import copy
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import paragon_workbench as app
import retained_candidates as review
from reconcile_drain_decisions import read_csv, write_csv, stamp, connect, snapshot, counters

W,U=review.ROOT,review.ARCHIVE
RECEIPT='db-restore/manifests/similarity-drain.json'
AUTHORIZATION='add to draon list likely redundndunt and strong matches'
CONTROLS=['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql',
    'db-restore/manifests/source-disposition.csv','db-restore/manifests/drain-evidence.json',
    'db-restore/export-summary.json','00_README.md',RECEIPT]


def plan():
    app.mount_guard()
    assert not (U/RECEIPT).exists(),'Already applied; use --verify.'
    picked=json.loads((W/'similarity-drain-plan.json').read_text())
    report=json.loads((W/'remaining-similarity-report.json').read_text())
    reports={r['path']:r for r in report['sources']}
    assets=review.evidence()['assets']
    with app.connect() as con:
        saved=review.saved_reviews(con);cases=review.cases(con)
        assert json.loads(review.DECISIONS.read_text())==review.readable_decisions(con)
    assert len(saved)==204
    case_by={c['key']:c for c in cases}
    changes={r['path']:r for r in picked['drain']};keepers={r['path'] for r in picked['kept_sources']}
    assert len(changes)==186 and len(keepers)==98 and not keepers&changes.keys()
    assert Counter(r['category'] for r in changes.values())=={'likely_redundant':82,'strong_match_keep_for_master_or_coverage_review':104}
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv');by={r['source_path']:r for r in rows}
    assert len(rows)==1419 and counters(rows)['candidate_rows']==714
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    checked={};proof={}
    for path,item in changes.items():
        source=assets[path];target=item['comparison']['target'];comparison=item['comparison']
        assert item['category']==reports[path]['category'] and comparison in reports[path]['candidates']
        assert comparison['strength']=='strong' and comparison['algorithm_count']==3
        assert target not in changes and (target not in by or by[target]['drain_state']=='retain_not_preserved')
        for p in [path,target]:
            review.verify_asset(assets[p]);checked[p]=dict(stamp=stamp(Path(p)),sha256=assets[p].get('sha256',''))
        assert by[path]['drain_state']=='retain_not_preserved' and by[path]['source_sha256']==source['sha256']
        proof[path]=dict(source_sha256=source['sha256'],source_path=path,
            category=item['category'],comparison_target=copy.deepcopy(comparison),
            evidence_report='remaining-similarity-report.json',receipt=RECEIPT,
            basis='Vladimir approved likely-redundant and strong-match extra copies for the drain list. A direct strong comparison survives outside this batch. Similarity is not exact-byte proof; no new Master selection or deletion authorization.',
            comparison_is_exact_byte_copy=False,deletion_authorized=False)
    for p in keepers:
        assert by[p]['drain_state']=='retain_not_preserved';review.verify_asset(assets[p])
        checked[p]=dict(stamp=stamp(Path(p)),sha256=assets[p]['sha256'])
    now=datetime.now(timezone.utc).isoformat();new=[]
    for key in sorted({r['case_key'] for r in changes.values()}):
        if key in saved:
            result=copy.deepcopy(saved[key]);assert result['action']=='retain' and not result['selected_master_path']
        else:
            obj=case_by[key];result=review.build_review(obj,dict(action='retain',metadata=obj['proposed_metadata']),'Vladimir')
        for f in result['files']:
            if f['path'] in changes:
                assert f['state']=='unresolved' and f['sha256']==proof[f['path']]['source_sha256']
                f['state']='redundant';f['similarity_evidence']=proof[f['path']]
        result.update(reviewed_at=now,actor='Vladimir',note=(result['note']+'\n' if result['note'] else '')+
            'Applied approved similarity-based extra-copy drain choices. Direct comparison targets and surviving sources: '+RECEIPT+'. No Master selected or source deletion authorized.')
        new.append(result)
    expected=saved|{r['case_key']:r for r in new};assert len(new)==149 and len(expected)==239
    updates=[]
    for path in sorted(changes):
        before=by[path];after=copy.deepcopy(before);target=proof[path]['comparison_target']['target']
        after.update(source_role='redundant_candidate',final_path='',final_sha256='',
            preservation_status='reviewed_not_selected',preservation_basis=proof[path]['basis'],
            exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',
            notes='User-approved similarity redundancy. Direct unlisted comparison: '+target+'. Receipt: '+RECEIPT+'. Existing other-unit provenance is retained. No final preservation or exact-byte-copy claim; no deletion authorized.')
        updates.append(dict(source_path=path,before=before,after=after,evidence=proof[path]))
    # Keep older proof as historical evidence and record its current surviving
    # comparison if a previously named source representative is now redundant.
    redirected=[]
    for r in saved.values():
        for f in r['files']:
            old=f.get('duplicate_evidence',{}).get('retained_source_path') or f.get('derivative_evidence',{}).get('coverage_target',{}).get('path')
            if old in changes:
                redirected.append(dict(source_path=f['path'],previous_comparison=old,
                    current_surviving_comparison=proof[old]['comparison_target'],
                    basis='Historical exact/derivative comparison plus the explicitly approved direct similarity decision for its former source representative. This is provenance, not a new exact-byte or accession-coverage claim.'))
    return dict(at_utc=now,picked=picked,rows=rows,fields=fields,prior_reviews=saved,
        new_reviews=new,expected_reviews=expected,updates=updates,source_evidence=checked,redirected=redirected)


def verify():
    app.mount_guard();receipt=json.loads((U/RECEIPT).read_text())
    assert receipt==json.loads((W/'similarity-drain.json').read_text()) and receipt['result']=='passed'
    rows=read_csv(U/'db-restore/manifests/source-disposition.csv')[1];by={r['source_path']:r for r in rows}
    assert len(rows)==1419 and counters(rows)['candidate_rows']==900 and all(r['deletion_authorized']=='false' for r in rows)
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W/'drain-evidence.json').read_bytes()==(U/'db-restore/manifests/drain-evidence.json').read_bytes()
    assert read_csv(W/'drain-list.csv')[1]==[r for r in rows if r['drain_state']=='candidate_after_separate_review']
    for change in receipt['updates']:
        assert by[change['source_path']]==change['after']
        target=change['evidence']['comparison_target']['target']
        assert target not in by or by[target]['drain_state']=='retain_not_preserved'
    for item in receipt['kept_sources']:assert by[item['path']]['drain_state']=='retain_not_preserved'
    for path,e in receipt['source_evidence'].items():assert stamp(Path(path))==e['stamp'],path
    for path,old in receipt['original_files'].items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==old,path
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==receipt['original_directories']
    assert stamp(W/'retain-review.csv')==receipt['stable_review_source_stamp']
    for path in [W/'work/catalog.sqlite',U/'db-restore/RESTORE.sqlite']:
        con=connect(path)
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
        assert review.saved_reviews(con)==receipt['saved_reviews_after'];con.close()
    review.evidence.cache_clear()
    with app.connect() as con:
        health=review.check(con);index=review.index(con);proposals=review.drain_proposals(con)['files']
    assert len(proposals)==577 and all(p['in_drain_register'] and not p['awaiting_accession'] for p in proposals)
    assert sum(c['unresolved_source_count'] for c in index['cases'])==516
    assert sum('similarity_evidence' in p for p in proposals)==186
    for row in read_csv(U/'db-restore/checksums.csv')[1]:assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    return dict(ok=True,similarity_extra_copies_added=186,likely_redundant_added=82,strong_match_extra_copies_added=104,
        surviving_source_representatives=98,drain_candidates=900,retained_paths=519,unresolved_files=516,explicitly_retained=3,
        small_images_remaining=141,saved_case_reviews=health['saved_reviews'],source_files_rehashed=0,
        source_files_decoded=0,source_modified=False,master_choices_changed=False,deletion_authorized=False,
        media_catalogues_metadata_and_relationships_unchanged=True)


def apply():
    p=plan();original={str(f.relative_to(U)):stamp(f) for f in U.rglob('*') if f.is_file() and not f.name.endswith(('-wal','-shm','-journal'))}
    receipt=dict(authorization=AUTHORIZATION,result='pending',completed_at=p['at_utc'],updates=p['updates'],
        kept_sources=p['picked']['kept_sources'],representative_rule=p['picked']['selection_note'],
        prior_covering_originals_now_redundant=p['picked']['prior_covering_originals_now_redundant'],
        redirected_comparisons=p['redirected'],source_evidence=p['source_evidence'],original_files=original,
        original_directories=sorted(str(f.relative_to(U)) for f in U.rglob('*') if f.is_dir()),
        stable_review_source_stamp=stamp(W/'retain-review.csv'),saved_reviews_after=p['expected_reviews'],
        source_files_rehashed=0,source_files_decoded=0,source_modified=False,
        accession_performed=False,master_choices_changed=False,deletion_authorized=False)
    review.write_json(W/'similarity-drain.json',receipt)
    logs=[dict(log_id='LOG-'+uuid4().hex.upper(),at_utc=p['at_utc'],actor='Vladimir',unit_id='WF-PARAGON',
        action=review.ACTION,entity_type='reconciliation_decision',entity_id=r['case_key'],
        before_json=json.dumps(p['prior_reviews'].get(r['case_key'],{}),ensure_ascii=False),
        after_json=json.dumps(r,ensure_ascii=False),evidence=RECEIPT,note=AUTHORIZATION) for r in p['new_reviews']]
    for path in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        con=connect(path,True);before=snapshot(con)
        assert review.saved_reviews(con)==p['prior_reviews']
        with con:
            for log in logs:con.execute('INSERT INTO logbook ('+','.join(log)+') VALUES ('+','.join('?' for _ in log)+')',tuple(log.values()))
            assert snapshot(con)==before and review.saved_reviews(con)==p['expected_reviews']
        con.close()
    con=connect(U/'db-restore/RESTORE.sqlite');sql='\n'.join(con.iterdump())+'\n';con.close()
    restored=sqlite3.connect(':memory:');restored.executescript(sql)
    assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not restored.execute('PRAGMA foreign_key_check').fetchall();restored.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    updates={u['source_path']:u for u in p['updates']}
    rows=[updates[r['source_path']]['after'] if r['source_path'] in updates else r for r in p['rows']]
    for path in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(path,p['fields'],rows)
    write_csv(W/'drain-list.csv',p['fields'],[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    evidence=json.loads((W/'drain-evidence.json').read_text());redirects={r['source_path']:r for r in p['redirected']}
    for e in evidence:
        if e['source_path'] in updates:
            u=updates[e['source_path']];e.update(drain_decision_receipt=RECEIPT,
                drain_decision_evidence=u['evidence'],final_path='',final_sha256='',preservation_status='reviewed_not_selected')
        if e['source_path'] in redirects:e['current_comparison_redirect']=redirects[e['source_path']]|{'receipt':RECEIPT}
    for path in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(path,evidence)
    stats=counters(rows);assert stats['candidate_rows']==900 and stats['retain_rows']==519
    summary=json.loads((W/'drain-review-summary.json').read_text());summary.update(stats)
    summary.update(updated_at=p['at_utc'],similarity_drain_receipt=RECEIPT,current_register='Work and final registers agree: 900 drain candidates and 519 retained sources. No deletion authorized.')
    review.write_json(W/'drain-review-summary.json',summary)
    title,body=(U/'00_README.md').read_text().split('\n',1)
    (U/'00_README.md').write_text(title+'\n\n## Current source register — similarity decisions, 18 September 2026\n\nAdded 186 user-approved similarity extra copies: all 82 likely-redundant files\nand 104 extra strong-match copies. Kept 98 surviving source representatives\nwithout selecting archival Masters. Current register: 900 drain candidates and\n519 retained paths (516 unresolved plus 3 explicitly retained). Each newly listed\npath has a direct strong comparison outside the drain list. Three former covering\noriginals now have surviving comparison replacements, recorded in the receipt;\nhistorical exact/derivative evidence is retained without claiming exact equality\nof the replacement. No Master, media, catalogue data or source was changed.\nNothing was deleted. Receipt: `'+RECEIPT+'`. Earlier counts below are historical.\n\n'+body)
    kh,checkrows=read_csv(U/'db-restore/checksums.csv');checks={r['path']:r for r in checkrows}
    export=json.loads((U/'db-restore/export-summary.json').read_text())
    export.update(updated_at=p['at_utc'],source_disposition_review=dict(total=1419,candidate_rows=900,retained_rows=519,receipt=RECEIPT),
        checksums=len(set(checks)|set(CONTROLS)),similarity_drain_receipt=RECEIPT)
    review.write_json(U/'db-restore/export-summary.json',export)
    for path,old in original.items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==old,path
    receipt.update(result='passed',source_register=stats,media_and_catalogues_unchanged=True,sql_restore_verified=True)
    for path in [W/'similarity-drain.json',U/RECEIPT]:review.write_json(path,receipt)
    for relative in CONTROLS:
        with (U/relative).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        checks[relative]=dict(path=relative,sha256='sha256:'+digest,size_bytes=str((U/relative).stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checks.values(),key=lambda r:r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:review.export_decisions(con)
    result=verify();review.write_json(W/'similarity-drain-validation.json',result);print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    if args.apply:apply()
    elif args.verify:print(json.dumps(verify(),indent=2))
    else:
        p=plan();print(json.dumps(dict(ready=True,paths_to_list=len(p['updates']),source_representatives=98,
            affected_cases=len(p['new_reviews']),saved_case_reviews_after=len(p['expected_reviews']),
            comparison_redirects=len(p['redirected']),drain_total_after=900),indent=2))
