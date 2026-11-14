#!/usr/bin/env python3
"""Bind harvested image proposals to existing workbench evidence, never adopt them."""
import argparse
import hashlib
import json
from pathlib import Path

import jsonschema

from archive_common import atomic_write, read_bytes, require, work_root
from workbench import Workbench, MODEL_PROPOSALS


def prepare(app, research):
    research = work_root(research)
    raw = read_bytes(research / 'openai-set111-harvest.json')
    harvest = json.loads(raw)
    manifest = json.loads(read_bytes(research / 'manifest.json'))
    schema = json.loads(read_bytes(research / 'openai-batch10-schema.json'))
    candidates = {c['id']: c for c in manifest['candidates']}
    require(len(candidates) == len(harvest) == 111, 'Expected the completed 111-image set')
    require(len({r['candidate_id'] for r in harvest}) == 111, 'Duplicate result')
    require({r['candidate_id'] for r in harvest} == set(candidates), 'Harvest coverage mismatch')
    records = []
    for row in harvest:
        require(row['status'] == 'success' and row['metadata_adopted'] is False, 'Unsuccessful or adopted result')
        jsonschema.validate(row['result'], schema)
        source = candidates[row['candidate_id']]
        oid, cid = source['id'], source['candidate_id']
        candidate = app.candidates.get(oid, {}).get(cid)
        require(candidate is not None, f'Unknown workbench candidate: {oid}/{cid}')
        require(any(m['path'] == source['path'] and
                    all(m[k] == source['stamp'][k] for k in ('size_bytes', 'mtime_ns'))
                    for m in candidate['members']), f'Source binding mismatch: {oid}')
        preview = (research / source['preview']).resolve()
        require(preview.is_relative_to(research / 'previews'), 'Preview outside research root')
        require(hashlib.sha256(read_bytes(preview)).hexdigest() == source['preview_sha256'], 'Preview changed')
        records.append({'object_id': oid, 'candidate_id': cid, 'source_path': source['path'],
                        'source_stamp': source['stamp'], 'preview_path': str(preview),
                        'preview_sha256': source['preview_sha256'], 'preview_size': source['preview_size'],
                        'preview_recipe': source['recipe'], 'result': row['result'],
                        'model': 'gpt-6-astra', 'reasoning_effort': 'low',
                        'usage': row['usage'], 'elapsed_seconds': row['elapsed_seconds'],
                        'context': {'city': 'Berlin', 'photographer': 'Kurt Schwarz', 'basis': 'owner supplied'},
                        'batch': row['batch'], 'metadata_adopted': False})
    return {'format': 'archive-model-proposals-1', 'metadata_adopted': False,
            'harvest_path': str(research / 'openai-set111-harvest.json'),
            'harvest_sha256': hashlib.sha256(raw).hexdigest(), 'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', required=True, type=Path)
    parser.add_argument('--research-root', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    app = Workbench(args.work_root, 'proposal import validation')
    data = prepare(app, args.research_root)
    target = app.root / MODEL_PROPOSALS
    if args.apply:
        require(not target.exists() and not target.is_symlink(), 'Proposal sidecar already exists; refusing overwrite')
        atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2).encode())
    print(json.dumps({'status': 'written' if args.apply else 'validated', 'count': len(data['records']),
                      'target': str(target), 'metadata_adopted': False}))


if __name__ == '__main__':
    main()
