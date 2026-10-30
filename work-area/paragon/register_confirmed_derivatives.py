#!/usr/bin/env python3
"""List the 96 confirmed derivatives without making accession a prerequisite."""
import argparse
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import paragon_workbench as app
import retained_candidates as review
from reconcile_drain_decisions import read_csv, write_csv, stamp, connect, snapshot, counters

W,U=review.ROOT,review.ARCHIVE
RECEIPT='db-restore/manifests/confirmed-derivative-drain.json'
AUTHORIZATION='what is up with derivatives? it goes into drain list. the access/hires images are regenerated during accession is that right?'
CONTROLS=['db-restore/RESTORE.sqlite','db-restore/RESTORE.sql',
    'db-restore/manifests/source-disposition.csv','db-restore/manifests/drain-evidence.json',
    'db-restore/export-summary.json','00_README.md',RECEIPT]


def plan():
    app.mount_guard()
    assert not (U/RECEIPT).exists(),'This correction is complete; use --verify.'
    with app.connect() as con:
        saved=review.saved_reviews(con)
        assert json.loads(review.DECISIONS.read_text())==review.readable_decisions(con)
        proposals=review.drain_proposals(con)['files']
    con.close()
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv')
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    by={r['source_path']:r for r in rows}
    selected=[p for p in proposals if not p['in_drain_register']]
    assert len(selected)==96 and len(saved)==132
    changes=[];sources={}
    for p in selected:
        assert p['disposition']=='redundant' and p['derivative_evidence']['source_sha256']==p['sha256']
        assert not p['awaiting_accession'] and p['covering_source_awaits_accession']
        target=p['derivative_evidence']['coverage_target']
        assert target['kind']=='retained_full_resolution_source'
        for path,digest in [(p['path'],p['sha256']),(target['path'],target['sha256'])]:
            asset=review.evidence()['assets'][path]
            review.verify_asset(asset)
            assert asset['sha256']==digest
            sources[path]=dict(sha256=digest,stamp=stamp(Path(path)))
        assert not review.evidence()['assets'][p['path']]['other_unit_links']
        before=by[p['path']];assert before['drain_state']=='retain_not_preserved'
        assert by[target['path']]['drain_state']=='retain_not_preserved'
        after=copy.deepcopy(before)
        after.update(source_role='redundant_candidate',preservation_status='reviewed_not_selected',
            preservation_basis='Confirmed derivative/copy. Vladimir instructed that existing derivatives belong on the drain list before accession; fresh access/hires output is generated from the selected Master during accession.',
            final_path='',final_sha256='',exact_copy_proven='false',drain_state='candidate_after_separate_review',deletion_authorized='false',
            notes='Covering retained source: '+target['path']+'; '+target['sha256']+'. Covering source is not yet accessioned; no final preservation claim. Exact comparison: '+RECEIPT+'. Listing does not authorize deletion.')
        changes.append(dict(source_path=p['path'],before=before,after=after,decision=p))
    return fields,rows,saved,changes,sources


def verify():
    receipt=json.loads((U/RECEIPT).read_text())
    assert receipt==json.loads((W/Path(RECEIPT).name).read_text()) and receipt['result']=='passed'
    app.mount_guard()
    fields,rows=read_csv(U/'db-restore/manifests/source-disposition.csv');by={r['source_path']:r for r in rows}
    assert len(rows)==1419 and sum(r['drain_state']=='candidate_after_separate_review' for r in rows)==478
    assert all(r['deletion_authorized']=='false' for r in rows)
    assert (W/'source-disposition.planned.csv').read_bytes()==(U/'db-restore/manifests/source-disposition.csv').read_bytes()
    assert (W/'drain-evidence.json').read_bytes()==(U/'db-restore/manifests/drain-evidence.json').read_bytes()
    assert read_csv(W/'drain-list.csv')[1]==[r for r in rows if r['drain_state']=='candidate_after_separate_review']
    for change in receipt['changes']:
        assert by[change['source_path']]==change['after']
        assert not change['after']['final_path'] and not change['after']['final_sha256']
    for path,data in receipt['source_evidence'].items():assert stamp(Path(path))==data['stamp']
    for path,before in receipt['original_files'].items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==before,path
    assert sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir())==receipt['original_directories']
    assert stamp(W/'retain-review.csv')==receipt['stable_review_source_stamp']
    logs=[]
    for path in [W/'work/catalog.sqlite',U/'db-restore/RESTORE.sqlite']:
        con=connect(path)
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not con.execute('PRAGMA foreign_key_check').fetchall()
        logs.append(dict(con.execute('SELECT * FROM logbook WHERE log_id=?',(receipt['log_id'],)).fetchone()))
        con.close()
    assert logs[0]==logs[1] and json.loads(logs[0]['after_json'])['changes']==receipt['changes']
    with app.connect() as con:
        assert review.saved_reviews(con)==receipt['saved_reviews']
        health=review.check(con);proposals=review.drain_proposals(con)['files']
    con.close()
    assert len(proposals)==155 and all(p['in_drain_register'] and not p['awaiting_accession'] for p in proposals)
    for row in read_csv(U/'db-restore/checksums.csv')[1]:assert (U/row['path']).stat().st_size==int(row['size_bytes']),row['path']
    return dict(ok=True,derivatives_added=96,drain_candidates=478,retained_paths=941,all_155_proposals_registered=True,
        saved_reviews=health['saved_reviews'],source_modified=False,deletion_authorized=False,media_and_catalogues_unchanged=True)


def apply():
    fields,rows,saved,changes,sources=plan();now=datetime.now(timezone.utc).isoformat()
    original={str(p.relative_to(U)):stamp(p) for p in U.rglob('*') if p.is_file() and not p.name.endswith(('-wal','-shm','-journal'))}
    receipt=dict(authorization=AUTHORIZATION,completed_at=now,unit='paragon',result='pending',changes=changes,source_evidence=sources,
        saved_reviews=saved,original_files=original,original_directories=sorted(str(p.relative_to(U)) for p in U.rglob('*') if p.is_dir()),
        stable_review_source_stamp=stamp(W/'retain-review.csv'),log_id='LOG-'+uuid4().hex.upper(),
        source_modified=False,deletion_authorized=False,accession_performed=False,derivatives_generated=0,
        rule='Existing confirmed derivatives are redundant and listed now; accession later generates required derivatives from the selected Master. Source deletion remains separate.')
    review.write_json(W/Path(RECEIPT).name,receipt)
    after=json.dumps(dict(changes=changes,rule=receipt['rule'],receipt=RECEIPT,deletion_authorized=False),ensure_ascii=False)
    log=(receipt['log_id'],now,'Vladimir','WF-PARAGON','register_confirmed_derivatives','archive_unit','WF-PARAGON','{}',after,RECEIPT,AUTHORIZATION)
    for path in [U/'db-restore/RESTORE.sqlite',W/'work/catalog.sqlite']:
        con=connect(path,True);before=snapshot(con)
        with con:
            con.execute('INSERT INTO logbook VALUES (?,?,?,?,?,?,?,?,?,?,?)',log)
            assert snapshot(con)==before
            assert not con.execute('PRAGMA foreign_key_check').fetchall()
        con.close()
    con=connect(U/'db-restore/RESTORE.sqlite');sql='\n'.join(con.iterdump())+'\n';con.close()
    restored=sqlite3.connect(':memory:');restored.executescript(sql)
    assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not restored.execute('PRAGMA foreign_key_check').fetchall();restored.close()
    (U/'db-restore/RESTORE.sql').write_text(sql)
    updates={c['source_path']:c for c in changes}
    rows=[updates[r['source_path']]['after'] if r['source_path'] in updates else r for r in rows]
    for path in [W/'source-disposition.planned.csv',U/'db-restore/manifests/source-disposition.csv']:write_csv(path,fields,rows)
    write_csv(W/'drain-list.csv',fields,[r for r in rows if r['drain_state']=='candidate_after_separate_review'])
    evidence=json.loads((W/'drain-evidence.json').read_text())
    for e in evidence:
        if e['source_path'] in updates:
            change=updates[e['source_path']]
            e.update(drain_decision_receipt=RECEIPT,drain_decision_log_id=receipt['log_id'],
                covering_source=change['decision']['derivative_evidence']['coverage_target'],
                final_path='',final_sha256='',preservation_status='reviewed_not_selected')
    for path in [W/'drain-evidence.json',U/'db-restore/manifests/drain-evidence.json']:review.write_json(path,evidence)
    stats=counters(rows);assert stats['candidate_rows']==478 and stats['retain_rows']==941
    summary=json.loads((W/'drain-review-summary.json').read_text())
    summary.update(stats);summary.update(updated_at=now,confirmed_derivative_drain=RECEIPT,
        current_register='Work and final registers agree: 478 drain candidates, 941 retained. All confirmed derivatives are listed; no deletion authorized.')
    review.write_json(W/'drain-review-summary.json',summary)
    title,body=(U/'00_README.md').read_text().split('\n',1)
    (U/'00_README.md').write_text(title+'\n\n## Current source register — 18 September 2026 correction\n\nAll confirmed existing derivatives belong on the drain list without waiting for\naccession. Fresh access/hires derivatives are generated from the selected Master\nduring accession. The remaining 96 confirmed derivative paths are now listed:\n478 total candidates and 941 retained sources (938 unresolved and 3 explicitly\nretained JPEGs). All 155 saved redundancy/noise proposals are registered.\nCovering originals remain retained; this does not claim that they are accessioned.\nNo source was deleted or modified, and no Master, media or catalogue row changed.\nReceipt: `'+RECEIPT+'`. All deletion flags remain false. Earlier counts below\nare historical checkpoints.\n\n'+body)
    kh,checkrows=read_csv(U/'db-restore/checksums.csv');checks={r['path']:r for r in checkrows}
    export=json.loads((U/'db-restore/export-summary.json').read_text())
    export.update(updated_at=now,source_disposition_review=dict(total=1419,candidate_rows=478,retained_rows=941,receipt=RECEIPT),
        checksums=len(set(checks)|set(CONTROLS)),confirmed_derivative_drain=RECEIPT)
    review.write_json(U/'db-restore/export-summary.json',export)
    for path,prior in original.items():
        if path not in set(CONTROLS)|{'db-restore/checksums.csv'}:assert stamp(U/path)==prior,path
    for path,data in sources.items():assert stamp(Path(path))==data['stamp']
    receipt.update(result='passed',source_register=stats,media_and_catalogues_unchanged=True,sql_restore_verified=True)
    for path in [W/Path(RECEIPT).name,U/RECEIPT]:review.write_json(path,receipt)
    for relative in CONTROLS:
        with (U/relative).open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
        checks[relative]=dict(path=relative,sha256='sha256:'+digest,size_bytes=str((U/relative).stat().st_size))
    write_csv(U/'db-restore/checksums.csv',kh,sorted(checks.values(),key=lambda r:r['path']))
    review.evidence.cache_clear()
    with app.connect() as con:review.export_decisions(con)
    con.close()
    result=verify();review.write_json(W/'confirmed-derivative-drain-validation.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    if args.apply:apply()
    elif args.verify:print(json.dumps(verify(),indent=2))
    else:
        fields,rows,saved,changes,sources=plan()
        print(json.dumps(dict(ready=True,confirmed_derivatives=len(changes),source_stamps_checked=len(sources),sources_modified=False),indent=2))
