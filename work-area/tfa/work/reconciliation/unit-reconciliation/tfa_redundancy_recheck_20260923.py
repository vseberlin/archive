"""Bounded TFA redundancy evidence check; no archive/source writes in inspect mode."""
import csv
import json
import sqlite3
import subprocess
from collections import defaultdict, Counter
from pathlib import Path

W = Path(__file__).resolve().parent
U = Path('/mnt/volume/iss-archive/units/tfa')
ROOT = U.parent
PREFIX = W / 'redundancy-recheck-20260923'

def read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))

def guard():
    mounts = json.loads(subprocess.check_output(['findmnt','-J','-T','/media/vladimir/Elements','-o','SOURCE,TARGET,FSTYPE,OPTIONS'], text=True))['filesystems']
    assert len(mounts) == 1
    mount = mounts[0]
    assert (mount['source'],mount['target'],mount['fstype']) == ('/dev/sdf1','/media/vladimir/Elements','ntfs3')
    assert 'ro' in mount['options'].split(',') and 'rw' not in mount['options'].split(',')
    return mount

def other_unit_paths(wanted):
    found = defaultdict(set)
    for unit in sorted(ROOT.iterdir()):
        if not unit.is_dir() or unit == U:
            continue
        files = [unit/'CATALOG.csv']
        for folder in ['00_MANIFESTS','db-restore/manifests','metadata']:
            if (unit/folder).is_dir():
                files.extend(p for p in (unit/folder).rglob('*.csv') if any(x in p.name for x in ['source','disposition','drain','representation','relation','document']))
        for path in files:
            if not path.is_file():
                continue
            for row in read(path):
                for value in row.values():
                    if value in wanted:
                        found[value].add(unit.name)
    return {path:sorted(units) for path,units in found.items()}

def features(wanted):
    result = {}
    for db in [Path('/home/vladimir/archive-visual-index/visual-index.sqlite'), W.parent/'wf-full-visual-index/wf-full-visual-index.sqlite']:
        c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        c.row_factory=sqlite3.Row
        assets={}
        for row in c.execute('select a.*,r.root_path from visual_assets a join visual_roots r using(root_id)'):
            path=str(Path(row['root_path'])/row['relative_path'])
            if path in wanted:
                item=dict(row); item['source_path']=path;item['features']={};item['index']=str(db)
                assets[row['asset_id']]=item
        ids=list(assets)
        for start in range(0,len(ids),400):
            batch=ids[start:start+400]
            for row in c.execute('select * from visual_features where asset_id in ('+','.join('?' for _ in batch)+')',batch):
                assets[row['asset_id']]['features'][row['extractor_id']]=row['feature_hex']
        for item in assets.values():
            if item['features']:
                result[item['source_path']]=item
        c.close()
    return result

def distance(a,b):
    keys=['ahash-64-v2','dhash-64-v2','phash-dct-64-v2']
    if not a or not b or not all(k in a['features'] and k in b['features'] for k in keys):
        return None
    return [int(a['features'][k],16).__xor__(int(b['features'][k],16)).bit_count() for k in keys]

def inspect():
    mount=guard()
    objects=read(U/'db-restore/RESTORE.csv'); obj={r['record_key']:r for r in objects}
    by_source={r['selected_master_path']:r for r in objects}
    drains={r['source_path']:r for r in read(U/'db-restore/manifests/drain-list.csv')}
    contexts={r['visual_group_id']:r for r in read(W/'reference-photo-hold-context.csv')}
    holds=[r for r in read(W/'orphan-source-disposition-proposal.csv') if r['preservation_status']=='redundant']
    linked=defaultdict(list)
    for r in read(W/'workbench-candidate-dispositions.csv'):
        if r['disposition']=='redundant' and r['source_path'] not in drains:
            linked[r['source_path']].append(r['record_key'])
    wanted=set(linked)|{r['source_path'] for r in holds}|{r['selected_master_path'] for r in objects}
    wanted|={c['source_path'] for c in contexts.values()}
    print('Reading existing indexes for',len(wanted),'paths',flush=True)
    indexed=features(wanted)
    ownership=other_unit_paths(wanted)
    checked={}
    for p,a in indexed.items():
        try:
            stat=Path(p).stat()
            checked[p]=stat.st_size==int(a['size_bytes']) and stat.st_mtime_ns==int(a['mtime_ns'])
        except OSError:
            checked[p]=False
    hold_rows=[]
    for r in holds:
        ctx=contexts[r['visual_group_id']]
        accepted=obj.get(ctx['context_record_key']) or by_source.get(ctx['source_path'])
        alternatives=[]
        if accepted is None:
            alternatives=[o for o in objects if o['inventory']==ctx['normalized_inventory']]
        hold_rows.append({'source':r,'context':ctx,'accepted':accepted,'inventory_alternatives':alternatives,
                          'other_units':ownership.get(r['source_path'],[]),
                          'source_stamp_matches_index':checked.get(r['source_path'],False),
                          'context_stamp_matches_index':checked.get(ctx['source_path'],False),
                          'hash_distances_to_context':distance(indexed.get(r['source_path']),indexed.get(ctx['source_path'])),
                          'already_in_drain':r['source_path'] in drains})
    image_rows=[]
    for path,keys in sorted(linked.items()):
        comparisons=[]
        for key in sorted(set(keys)):
            o=obj.get(key)
            if o and o['accession_result']=='accessioned':
                ds=distance(indexed.get(path),indexed.get(o['selected_master_path']))
                comparisons.append({'record_key':key,'inventory':o['inventory'],'master_relative_path':o['master_relative_path'],
                                    'source_master':o['selected_master_path'],'distances':ds,
                                    'master_source_stamp_matches':checked.get(o['selected_master_path'],False)})
        image_rows.append({'source_path':path,'other_units':ownership.get(path,[]),
                           'source_stamp_matches_index':checked.get(path,False),'comparisons':comparisons})
    output={'source_mount':mount,'deletion_authorized':False,'hold_redundancies':hold_rows,'image_discrepancies':image_rows}
    (PREFIX.with_suffix('.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print('HOLD_COUNT',len(hold_rows),'direct_archive_links',sum(bool(r['accepted']) for r in hold_rows))
    counts=Counter()
    close=[]
    for row in image_rows:
        if row['other_units']:
            counts['other_unit']+=1;continue
        if not row['source_stamp_matches_index']:
            counts['source_stamp_missing_or_changed']+=1;continue
        good=[c for c in row['comparisons'] if c['distances'] is not None and sum(c['distances'])<=15 and c['master_source_stamp_matches']]
        if good:
            counts['close_saved_hash_match']+=1
            close.append({'source_path':row['source_path'],'matches':good})
        else:counts['no_close_saved_hash_match']+=1
    print('IMAGE_COUNTS',dict(counts));print('CLOSE_MATCHES',json.dumps(close,ensure_ascii=False)[:16000])

if __name__=='__main__':
    inspect()
