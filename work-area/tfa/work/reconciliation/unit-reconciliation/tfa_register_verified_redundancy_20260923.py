"""Register only the exact reviewed additions in the current TFA archive controls."""
import csv
import hashlib
import importlib.util
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

W=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('recheck',W/'tfa_redundancy_recheck_20260923.py')
R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
U=R.U
PLAN=W/'redundancy-confirmed-additions-20260923.json'
RECEIPT=U/'db-restore/manifests/redundancy-recheck-20260923.json'

def sha(path):
    with path.open('rb') as f:
        return 'sha256:'+hashlib.file_digest(f,'sha256').hexdigest()

def atomic(path,data):
    temp=path.with_name('.'+path.name+'.recheck-tmp')
    with temp.open('xb') as f:
        f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

def csv_bytes(fields,rows):
    import io
    f=io.StringIO(newline='');writer=csv.DictWriter(f,fieldnames=fields,quoting=csv.QUOTE_ALL)
    writer.writeheader();writer.writerows(rows)
    return f.getvalue().encode('utf-8')

def database_rows(c,table,fields):
    return Counter(tuple('' if row[k] is None else str(row[k]) for k in fields) for row in c.execute('select * from '+table))

def apply():
    R.guard()
    assert not RECEIPT.exists(), 'Already applied; do not replay'
    plan=json.loads(PLAN.read_text())
    assert plan['deletion_authorized'] is False and plan['master_selection_changed'] is False
    for rel,digest in plan['archive_control_hashes_before'].items():
        assert sha(U/rel)==digest, 'Archive control changed: '+rel
    for rel,digest in plan['work_evidence_hashes_before'].items():
        assert sha(W/rel)==digest, 'Saved review changed: '+rel
    additions=plan['additions'];assert additions
    paths={r['source_path'] for r in additions};assert len(paths)==len(additions)
    assert not R.other_unit_paths(paths), 'Other-unit source overlap'
    source_rows=R.read(U/'db-restore/manifests/source-disposition.csv')
    drain_rows=R.read(U/'db-restore/manifests/drain-list.csv')
    source={r['source_path']:r for r in source_rows};drain={r['source_path']:r for r in drain_rows}
    assert not paths & set(drain), 'An addition is already in the drain'
    objects={r['record_key']:r for r in R.read(U/'db-restore/RESTORE.csv')}
    representations=R.read(U/'db-restore/manifests/representations.csv')
    masters=Counter(r['record_key'] for r in representations if r['role']=='preservation_master')
    fields=list(source_rows[0]);previous={};changed=[]
    for item in additions:
        obj=objects[item['record_key']]
        assert obj['accession_result']=='accessioned' and masters[item['record_key']]==1
        assert obj['master_relative_path']==item['final_path'] and obj['master_sha256']==item['final_sha256']
        final=U/item['final_path'];assert final.is_file()
        rep=next(r for r in representations if r['record_key']==item['record_key'] and r['role']=='preservation_master')
        assert final.stat().st_size==int(rep['size_bytes'])
        p=Path(item['source_path']);p.resolve(strict=True).relative_to(Path('/media/vladimir/Elements/restore/archive').resolve(strict=True))
        s=p.stat();assert [s.st_size,s.st_mtime_ns]==item['source_stamp']
        previous[str(p)]=source.get(str(p))
        row=dict(source.get(str(p),{k:'' for k in fields}))
        assert row.get('preservation_status')!='genuine_loss'
        row.update(source_path=str(p),source_kind='file',unit='tfa',inventory=obj['inventory'],
                   source_role='derivative_candidate' if p.suffix.lower() in {'.jpg','.jpeg'} else 'master_candidate',
                   final_path=obj['master_relative_path'],preservation_status='redundant',
                   preservation_basis='verified_visual_redundancy_to_accessioned_tfa_master',
                   final_sha256=obj['master_sha256'],source_size_bytes=str(s.st_size),exact_copy_proven='false',
                   drain_state='proposed_redundant',deletion_authorized='false',
                   notes=item['evidence_note']+' Recheck: db-restore/manifests/redundancy-recheck-20260923.json. Source unchanged; deletion not authorized.')
        source[str(p)]=row;drain[str(p)]=row;changed.append(row)
    current_source=[source[p] for p in sorted(source)];current_drain=[drain[p] for p in sorted(drain)]
    source_data=csv_bytes(fields,current_source);drain_data=csv_bytes(fields,current_drain)
    db=U/'db-restore/RESTORE.sqlite';c=sqlite3.connect(db);c.row_factory=sqlite3.Row
    assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
    assert database_rows(c,'source_disposition',fields)==Counter(tuple(r[k] for k in fields) for r in source_rows)
    assert database_rows(c,'drain_list',fields)==Counter(tuple(r[k] for k in fields) for r in drain_rows)
    decisions_before=c.execute('select * from reconciliation_decisions order by record_key').fetchall()
    with c:
        for row in changed:
            c.execute('delete from source_disposition where source_path=?',(row['source_path'],))
            values=tuple(row[k] for k in fields)
            c.execute('insert into source_disposition ('+','.join(fields)+') values ('+','.join('?' for _ in fields)+')',values)
            c.execute('insert into drain_list ('+','.join(fields)+') values ('+','.join('?' for _ in fields)+')',values)
    atomic(U/'db-restore/manifests/source-disposition.csv',source_data)
    atomic(U/'db-restore/manifests/drain-list.csv',drain_data)
    dump=U/'db-restore/.RESTORE.sql.recheck-tmp'
    with dump.open('x') as f:
        for line in c.iterdump():f.write(line+'\n')
        f.flush();os.fsync(f.fileno())
    os.replace(dump,U/'db-restore/RESTORE.sql')
    assert c.execute('pragma quick_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall()
    assert database_rows(c,'source_disposition',fields)==Counter(tuple(r[k] for k in fields) for r in current_source)
    assert database_rows(c,'drain_list',fields)==Counter(tuple(r[k] for k in fields) for r in current_drain)
    assert c.execute('select * from reconciliation_decisions order by record_key').fetchall()==decisions_before
    c.close()
    for rel,digest in plan['unchanged_catalogue_hashes'].items():assert sha(U/rel)==digest,rel
    readme=(U/'00_README.md').read_text()
    old=f'- Redundant USB source paths in the drain list: {len(drain_rows)}'
    assert old in readme
    readme=readme.replace(old,f'- Redundant USB source paths in the drain list: {len(current_drain)}')
    readme+='\n## Redundancy recheck — 23 September 2026\n\n'+str(len(changed))+' exact source paths were added after comparison with their accepted TFA Masters. No Master, archive media, object metadata, source file or deletion permission changed. Evidence and exclusions: `db-restore/manifests/redundancy-recheck-20260923.json`. Earlier accession-run receipts and the legacy pre-accession workbench proposal remain historical. Other recorded drain-boundary issues remain unresolved; this is not deletion clearance.\n'
    atomic(U/'00_README.md',readme.encode())
    now=datetime.now(timezone.utc).isoformat()
    receipt={'result':'ok','completed_at':now,'deletion_authorized':False,'master_selection_changed':False,
             'source_modified':False,'source_mount':R.guard(),'archive_drain_before':len(drain_rows),
             'archive_drain_after':len(current_drain),'source_disposition_before':len(source_rows),
             'source_disposition_after':len(current_source),'added_paths':len(changed),'additions':additions,
             'previous_source_dispositions':previous,'hold_review':plan['hold_review'],
             'jpeg_review':plan.get('jpeg_review',{}),'archive_control_hashes_before':plan['archive_control_hashes_before'],
             'unchanged_catalogue_hashes':plan['unchanged_catalogue_hashes'],
             'sqlite_quick_check':'ok','foreign_key_violations':0,'csv_sqlite_agree':True,
             'remaining_boundary_issues_not_resolved':True}
    payload=(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode()
    atomic(RECEIPT,payload);atomic(W/'redundancy-registration-result-20260923.json',payload)
    completion=json.loads((U/'db-restore/accession-completion.json').read_text())
    completion['drain_rows']=len(current_drain);completion['source_disposition_rows']=len(current_source)
    completion['redundancy_recheck']={'completed_at':now,'added_paths':len(changed),'receipt':str(RECEIPT.relative_to(U)),'deletion_authorized':False}
    for rel in ['00_README.md','db-restore/RESTORE.sqlite','db-restore/RESTORE.sql','db-restore/manifests/drain-list.csv','db-restore/manifests/source-disposition.csv',str(RECEIPT.relative_to(U))]:
        completion['control_file_sha256'][rel]=sha(U/rel)
    atomic(U/'db-restore/accession-completion.json',(json.dumps(completion,ensure_ascii=False,indent=2)+'\n').encode())
    print(json.dumps({k:receipt[k] for k in ['result','added_paths','archive_drain_after','source_disposition_after','csv_sqlite_agree','deletion_authorized']},indent=2))

if __name__=='__main__':
    apply()
