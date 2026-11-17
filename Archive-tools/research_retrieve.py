#!/usr/bin/env python3
"""Build/search a bounded local receipt index, optionally verifying visual leads."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from archive_research.contracts import require,encoded
from archive_research.sources import Roots
from archive_research.storage import lock,write_json,target
from archive_research.retrieval import build_index,visual_search,text_search
from archive_research.evidence import read_evidence,evidence_image


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--roots',type=Path,required=True)
    p.add_argument('--selection',type=Path,required=True)
    p.add_argument('--index-output',required=True);p.add_argument('--output',required=True)
    p.add_argument('--sift',action='store_true')
    args=p.parse_args();roots=Roots.from_toml(args.roots);work='artifacts';root=roots.root(work,'work')
    selection=json.loads(args.selection.read_text())
    require(set(selection)=={'keys','visual_queries','text_queries','limit'},'Explicit retrieval selection required')
    require(isinstance(selection['visual_queries'],list) and len(selection['visual_queries'])<=32 and
            all(k in selection['keys'] for k in selection['visual_queries']),'Select bounded visual queries')
    require(isinstance(selection['text_queries'],list) and len(selection['text_queries'])<=32,
            'Select bounded text queries')
    require(args.output!=args.index_output,'Output filenames must differ')
    target(root,args.output);target(root,args.index_output)
    with lock(root):
        index=build_index(roots,work,selection['keys'])
        results={'metadata_adopted':False,'selection':selection,'visual':[],
                 'index_sha256':hashlib.sha256(encoded(index).encode()).hexdigest(),
                 'text':[text_search(index,q,selection['limit']) for q in selection['text_queries']],
                 'coverage':index['coverage'],'unavailable':index['unavailable']}
        def picture(key):
            item=read_evidence(roots,work,{'key':key,'label':'Selected visual lead','kind':'detail',
                                         'lineage':'Source binding in index','attribution':'Local research only'})
            return evidence_image(roots,work,item)
        for key in selection['visual_queries']:
            result=visual_search(index,key,selection['limit'])
            if args.sift:
                from sift_matching import compare_prepared
                source=picture(key)
                for hit in result['hits']:
                    hit['geometry']=compare_prepared(source,picture(hit['target']))
            results['visual'].append({'query':key,**result})
        write_json(root,args.index_output,index);write_json(root,args.output,results)
    print(json.dumps({'indexed':len(index['entries']),'unavailable':len(index['unavailable']),
                      'visual_queries':len(results['visual']),'external_submission':False}))


if __name__=='__main__':main()
