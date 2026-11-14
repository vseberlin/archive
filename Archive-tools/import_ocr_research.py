#!/usr/bin/env python3
"""Attach bounded OCR evidence to existing, source-bound proposal records."""
import argparse
import datetime
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--research', type=Path, required=True)
    args = parser.parse_args()
    root = Path('/mnt/volume/iss-archive-work').resolve()
    for path in (args.review, args.research):
        if not path.resolve().is_relative_to(root) or not path.is_dir():
            raise ValueError('Expected existing work directory')
    sidecar = args.review / 'model-proposals.json'
    data = read(sidecar)
    assert data['metadata_adopted'] is False
    original = {r['object_id']: r['result'] for r in data['records']}
    candidates = {}
    sources = [args.research / 'hypothesis10-20261108-ocr-raw.json']
    sources += sorted(args.research.glob('ocr-remaining101-*-ocr.json'))
    for path in sources:
        if not path.exists():
            continue
        try:
            for row in read(path).get('results', []):
                complete = bool(row.get('runs')) and all(r.get('returncode') == 0 for r in row['runs'])
                rank = (complete, sum(r.get('returncode') == 0 for r in row['runs']), path.stat().st_mtime_ns)
                oid = row['object_id']
                if oid not in candidates or rank > candidates[oid][0]:
                    candidates[oid] = (rank, row, path)
        except (ValueError, KeyError):
            continue  # A running receipt may not yet be complete.
    clues_path = args.research / 'hypothesis10-20261108-ocr-clues.json'
    findings_path = args.research / 'hypothesis10-20261108-research2-results.json'
    clues = {r['object_id']: r for r in read(clues_path)['records']} if clues_path.exists() else {}
    findings = {r['object_id']: r for r in read(findings_path)['records']} if findings_path.exists() else {}
    statuses = []
    for path in args.research.glob('ocr-remaining101-*-status.json'):
        try:
            statuses.append((path.stat().st_mtime_ns, path, read(path)))
        except ValueError:
            pass
    now = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    counts = {}
    for proposal in data['records']:
        oid = proposal['object_id']
        evidence = {'status': 'pending_or_failed', 'snapshot_at': now,
                    'metadata_adopted': False, 'text_verified': False, 'runs': []}
        if oid in candidates:
            rank, row, path = candidates[oid]
            if row['source_stamp'] != proposal['source_stamp'] or row['source'] != proposal['source_path']:
                raise ValueError(f'OCR source binding mismatch: {oid}')
            evidence.update(status='complete' if rank[0] else 'partial', receipt_path=str(path),
                            source_stamp=row['source_stamp'], native_dimensions=row['native_dimensions'])
            for run in row['runs']:
                text = run.get('raw_text', '')
                evidence['runs'].append({'label': run['label'], 'returncode': run.get('returncode'),
                    'error': run.get('error'), 'image_path': run.get('image_path'),
                    'native_box': run.get('native_box'), 'dimensions': run.get('dimensions'),
                    'raw_text_excerpt': text[:4000], 'text_truncated': len(text) > 4000,
                    'raw_text_characters': len(text), 'psm': run.get('psm')})
        attempts = []
        for _, path, state in sorted(statuses):
            for attempt in state.get('results', []):
                if attempt['object_id'] == oid:
                    attempts.append({**attempt, 'batch_status': state['status'], 'status_path': str(path)})
            if state.get('status') == 'running' and state.get('current_object_id') == oid:
                evidence['recovery'] = 'running at snapshot time'
        evidence['attempts'] = attempts
        if oid in clues:
            c = clues[oid]
            evidence['reviewed_clues'] = {k: c.get(k) for k in ('readable', 'uncertain', 'graphics', 'next')}
        if oid in findings:
            f = findings[oid]
            evidence['reference_research'] = {k: f.get(k) for k in ('status', 'new_evidence',
                'location_assessment', 'event_assessment', 'photograph_date_assessment', 'remaining_gap', 'sources')}
        if oid == 'VASSET-65B9F846C4B9C4A4A31344BDC48D11FE' and not proposal.get('sculpture_identification'):
            evidence['human_followup'] = 'User identifies a panther-like big cat in the enlarged crop and suggests Tiergarten. Location and sculpture remain unconfirmed; earlier bear-based searches are superseded as the main lead.'
        proposal['ocr_research'] = evidence
        counts[evidence['status']] = counts.get(evidence['status'], 0) + 1
    assert original == {r['object_id']: r['result'] for r in data['records']}
    data['ocr_research_snapshot'] = {'snapshot_at': now, 'counts': counts,
        'note': 'OCR processing status is not text verification. Original model results unchanged.'}
    temp = sidecar.with_name('model-proposals-ocr-import.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temp.replace(sidecar)
    print(json.dumps(data['ocr_research_snapshot']))


if __name__ == '__main__':
    main()
