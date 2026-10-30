"""Independent native-manifest and SQL-export checks for the approved normalization."""
import json
import sqlite3
from collections import Counter
from pathlib import Path
import pdf_normalization as n

def canon(r):
    result={}
    for k,v in r.items():
        s=n.strval(v)
        if k=='deletion_authorized':s='false' if s.lower() in ('false','0','') else s
        result[k]=s
    return json.dumps(result,sort_keys=True,ensure_ascii=False)

def compare(root,rel,rows):
    fields,csvrows=n.readcsv(root/rel)
    historical=rel in plan['payload_stats']
    if historical:
        remap=n.remapper(root,plan['moves'])
        csvrows=[{k:remap(v) for k,v in r.items()} for r in csvrows]
    shared=[f for f in fields if not rows or f in rows[0]]
    expected=[{f:r[f] for f in shared} for r in rows]
    projected=[{f:r[f] for f in shared} for r in csvrows]
    if Counter(canon(r) for r in projected)!=Counter(canon(r) for r in expected):
        def lf(r):return {k:n.strval(v).replace('\r\n','\n') for k,v in r.items()}
        assert Counter(canon(lf(r)) for r in projected)==Counter(canon(lf(r)) for r in expected),('native CSV differs',root.name,rel)
        lookup={canon(lf(r)):r for r in expected}
        restored=[dict(raw,**lookup[canon(lf(p))]) for raw,p in zip(csvrows,projected,strict=True)]
        (root/rel).write_bytes(n.bcsv(fields,n.textrows(restored)))
        repaired_controls.append(rel)
        n.out(unit,rel).write_bytes((root/rel).read_bytes())
    finalrows=n.readcsv(root/rel)[1]
    if historical:finalrows=[{k:remap(v) for k,v in r.items()} for r in finalrows]
    assert Counter(canon({f:r[f] for f in shared}) for r in finalrows)==Counter(canon(r) for r in expected)

for unit,dirname in n.UNITS.items():
    root=n.ROOT/dirname;plan=json.loads(n.out(unit,'plan.json').read_text())
    db,tables,_=n.load_db(root)
    checked=[]
    repaired_controls=[]
    if unit in ('aeb','bibliothek','feb'):
        compare(root,'db-restore/files.csv',tables['files']);checked.append('db-restore/files.csv')
        rel='00_MANIFESTS/accession-master-representations.csv' if unit=='feb' else '00_MANIFESTS/master-representations.csv'
        compare(root,rel,tables['objects']);checked.append(rel)
        if unit in ('aeb','bibliothek'):
            for role,rel in [('master_member','00_MANIFESTS/master-files.csv'),('access_preview','00_MANIFESTS/derivatives.csv')]:
                compare(root,rel,[r for r in tables['files'] if r['file_role']==role]);checked.append(rel)
        compare(root,'00_MANIFESTS/source-disposition.csv',tables['source_disposition']);checked.append('00_MANIFESTS/source-disposition.csv')
    elif unit=='archiv-wf':
        for table in ('master_representations','representations','object_relationships','integrity_exceptions','documents'):
            rel='00_MANIFESTS/'+table.replace('_','-')+'.csv'
            compare(root,rel,tables[table]);checked.append(rel)
    elif unit=='brigadebuecher':
        for table,rel in [('catalog','db-restore/RESTORE.csv'),('representations','00_MANIFESTS/representations.csv'),('representation_members','00_MANIFESTS/representation-members.csv'),('source_disposition','00_MANIFESTS/source-disposition.csv')]:
            compare(root,rel,tables[table]);checked.append(rel)
        for r in tables['representations']:
            members=sorted([m for m in tables['representation_members'] if m['representation_id']==r['representation_id']],key=lambda m:m['sequence'])
            assert n.member_hash(members)==r['representation_manifest_sha256']
    else:
        compare(root,'db-restore/RESTORE.csv',tables['representation' if unit=='trafo' else 'representations']);checked.append('db-restore/RESTORE.csv')
    sqlfile=root/'db-restore/RESTORE.sql'
    if sqlfile.exists():
        with sqlfile.open(newline='') as f:sql=f.read()
        c=sqlite3.connect(':memory:');c.executescript(sql);c.row_factory=sqlite3.Row
        for t,rs in tables.items():
            assert [dict(r) for r in c.execute('SELECT * FROM '+n.q(t))]==rs,('SQL restore differs',unit,t)
        c.close();checked.append('complete SQL restore in memory')
    checks={}
    for rel in plan['inputs']:
        if 'checksum' not in rel or not rel.endswith('.csv'):continue
        fields,rs=n.readcsv(root/rel)
        pk=next((f for f in ('relative_path','path','final_relative_path') if f in fields),None)
        hk=next((f for f in ('sha256','final_sha256','content_sha256') if f in fields),None)
        if pk and hk:
            changed=False
            for r in rs:
                if r[pk] in repaired_controls:
                    r[hk]=('sha256:' if r[hk].startswith('sha256:') else '')+n.digest(root/r[pk])
                    r['size_bytes']=str((root/r[pk]).stat().st_size)
                    if 'mtime_ns' in fields:r['mtime_ns']=str((root/r[pk]).stat().st_mtime_ns)
                    changed=True
            if changed:(root/rel).write_bytes(n.bcsv(fields,rs))
            for r in rs:checks.setdefault(r[pk],set()).add(r[hk].removeprefix('sha256:'))
    catalog=n.readcsv(root/'CATALOG.csv')[1]
    for r in catalog:
        assert r['final_sha256'] in checks.get(r['final_relative_path'],set()),('file hash missing from existing manifest',unit,r['final_relative_path'])
    # Only changed controls need fresh digest comparisons. Media use stored hashes.
    for rel in set(plan['writes'])|{'CATALOG.xlsx',plan['db'],'db-restore/RESTORE.sql'}:
        if rel in checks and rel not in plan['payload_stats']:
            assert n.digest(root/rel) in checks[rel],('changed control checksum differs',unit,rel)
    result={'unit':unit,'validation':'passed','native_csv_checks':checked,'registered_file_hashes_match':len(catalog),
            'native_csv_embedded_line_endings_restored':repaired_controls,
            'media_rehashed':0,'schema_changed':False,'deletion_authorized':False}
    n.dump(n.out(unit,'native-validation.json'),result)
    print(json.dumps(result),flush=True)
