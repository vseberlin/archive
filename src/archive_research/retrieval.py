"""Bounded local text/visual index over shared receipts; no persistent database.

The readable index is rebuildable and points back to sources/pages/regions. Search
results are research leads; neither cosine similarity nor geometry adopts identity.
"""
from dataclasses import asdict
import hashlib
import json
import math

from .contracts import encoded, require
from .receipts import read_current


def build_index(roots, work, keys):
    require(isinstance(keys,list) and 1 <= len(keys) <= 2048 and len(keys)==len(set(keys)),
            'Select 1-2048 distinct extraction receipts')
    entries=[]; unavailable=[]
    for key in keys:
        try:
            row,result,digest=read_current(roots,work,key)
            entry={'key':key,'receipt_sha256':digest,'page':asdict(result.page),
                   'region':asdict(result.region) if result.region else None,
                   'extractor':result.recipe.extractor,'text':' '.join(w['text'] for w in row['words']),
                   'vector':None,'space':None}
            if result.recipe.extractor=='visual':
                raw=next(a for a in result.artifacts if a.kind=='normalized-visual-embedding')
                feature=json.loads(roots.artifact(raw).read_text())
                require(feature==row['feature'],'Visual receipt differs from feature artifact')
                vector=feature['vector'];config=result.recipe.record()['parameters']
                require(len(vector)==feature['dimensions']==config['dimensions'] and
                        all(type(v) in (int,float) and math.isfinite(v) for v in vector) and
                        abs(sum(v*v for v in vector)-1)<1e-4,'Invalid normalized feature vector')
                space={k:config[k] for k in ('encoder','dimensions','preprocessing','dependencies')}
                space['model_sha256']=config['settings']['model']['sha256']
                entry.update(vector=vector,space=hashlib.sha256(encoded(space).encode()).hexdigest())
            entries.append(entry)
        except (OSError,ValueError,KeyError,StopIteration) as error:
            unavailable.append({'key':key,'reason':str(error)})
    return {'format':'archive-research-local-index-1','metadata_adopted':False,
            'selection':keys,'entries':entries,'unavailable':unavailable,
            'coverage':{'selected_receipts':len(keys),'available_receipts':len(entries),
                        'whole_archive':False},
            'limitations':['Selected receipts only; omitted units/pages are not searched.',
                           'Text and visual similarities are leads, not independent confirmations.']}


def refresh_index(roots, work, stored):
    require(stored.get('format')=='archive-research-local-index-1' and stored.get('metadata_adopted') is False,
            'Unsupported local index')
    current=build_index(roots,work,stored['selection'])
    require(encoded(current)==encoded(stored),'Index inputs changed; rebuild explicitly')
    return current


def same_object(a,b):
    return (a['page']['source']['unit'],a['page']['source']['object_id']) == (
        b['page']['source']['unit'],b['page']['source']['object_id'])


def visual_search(index, query_key, limit=5):
    require(type(limit) is int and 1 <= limit <= 50,'Invalid retrieval limit')
    query=next((e for e in index['entries'] if e['key']==query_key),None)
    require(query is not None and query['vector'] is not None,'Visual query unavailable')
    hits=[]; excluded=[]
    for entry in index['entries']:
        if entry['vector'] is None or same_object(entry,query):continue
        if entry['space']!=query['space']:
            excluded.append(entry['key']);continue
        score=max(-1.,min(1.,sum(a*b for a,b in zip(query['vector'],entry['vector']))))
        hits.append({'query':query_key,'target':entry['key'],'similarity':score,
                     'query_receipt_sha256':query['receipt_sha256'],
                     'target_receipt_sha256':entry['receipt_sha256'],
                     'identity_confirmed':False})
    hits.sort(key=lambda h:(-h['similarity'],h['target']))
    return {'hits':hits[:limit],'eligible':len(hits),'incompatible_space':excluded,
            'limit':limit,'same_object_excluded':True}


def text_search(index, query, limit=10):
    require(isinstance(query,str) and query.strip() and len(query)<=200,'Bounded text query required')
    require(type(limit) is int and 1<=limit<=50,'Invalid text retrieval limit')
    tokens=query.casefold().split();hits=[]
    for entry in index['entries']:
        value=entry['text']; folded=value.casefold()
        if all(t in folded for t in tokens):
            start=max(0,min(folded.index(t) for t in tokens)-100)
            hits.append({'target':entry['key'],'snippet':value[start:start+450],
                         'target_receipt_sha256':entry['receipt_sha256'],
                         'identity_confirmed':False})
    return {'query':query,'hits':hits[:limit],'total_matches':len(hits),'limit':limit}
