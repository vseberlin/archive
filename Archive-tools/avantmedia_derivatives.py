#!/usr/bin/env python3
"""Reclassify an existing Avantmedia preparation under its approved delivery layout.

No matching, image decoding, accession, source mutation, or deletion. The original
review stays intact. Folder/identifier links are explicitly recorded as the user's
delivery-layout assumption, not as independently verified pixel identity.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import csv
import json
from pathlib import Path
import re

from archive_common import atomic_write, elements_guard, plain_file, require, work_root
from matching import catalogue_metadata, compare_metadata, csv_rows, metadata_question, stamp
from workbench import EDIT_FIELDS, Workbench, encoded

BASIS = 'User-approved Avantmedia derivative delivery layout; no deletion authority'
DERIVATIVE_FOLDERS = {'nutzungsfiles', 'nutzungsderivate', 'baecker_allejpgs'}


def enable_jpeg_fallback(obj, row):
    sources = [c for c in obj['candidates'] if not c.get('reference_object_id')]
    if any(c['eligible'] and c['complete'] and Path(c['members'][0]['path']).suffix.lower() in ('.tif', '.tiff')
           and not c.get('technical', {}).get('is_surrogate') for c in sources):
        return
    fallback = [c for c in sources if c['complete'] and not c.get('parent_candidates')
                and Path(c['members'][0]['path']).suffix.lower() in ('.jpg', '.jpeg')]
    for c in fallback:
        c.update(eligible=True, source_role='master_fallback',
                 master_eligibility_basis='Usable JPEG; no usable genuine source TIFF in this case')
        if c.get('label', '').startswith('JPEG derivative · '):
            c['label'] = c['label'].replace('JPEG derivative · ', 'JPEG Master fallback · ', 1)
    if fallback and not row.get('selected_candidate_id'):
        best = max(fallback, key=lambda c: (c.get('technical', {}).get('width') or 0) *
                   (c.get('technical', {}).get('height') or 0))
        row['selected_candidate_id'] = best['id']
        obj['recommended_candidate_id'] = best['id']
        row.setdefault('redundant_candidate_ids', [])
        row.setdefault('same_pairs', [])
        row['redundant_candidate_ids'] = sorted(Workbench.same_component(row, best['id']) - {best['id']})


def inherit_fallback_comparisons(obj, previous_objects):
    """Keep a usable JPEG's own archive leads when its TIFF cannot be used.

    This copies comparison evidence, never identity or Master decisions.
    """
    for fallback in list(obj['candidates']):
        if fallback.get('source_role') != 'master_fallback' or not fallback['eligible']:
            continue
        member = fallback['members'][0]
        for previous in previous_objects:
            sources = [c for c in previous['candidates'] if not c.get('reference_object_id')
                       and len(c['members']) == 1 and c['members'][0] == member]
            for source in sources:
                refs = {c['id']: c for c in previous['candidates'] if c.get('reference_object_id')}
                leads = [m for m in previous.get('matches', []) if source['id'] in (m.get('a'), m.get('b'))
                         and any(cid in (m.get('a'), m.get('b')) for cid in refs)]
                for lead in leads:
                    target_id = next(cid for cid in refs if cid in (lead['a'], lead['b']))
                    target = refs[target_id]
                    current = next((c for c in obj['candidates'] if c['id'] == target_id), None)
                    require(not current or current['members'] == target['members'], 'Fallback reference evidence changed')
                    if not current:
                        obj['candidates'].append(deepcopy(target))
                        obj.setdefault('metadata_comparisons', []).append({'a':fallback['id'], 'b':target_id,
                            'fields':compare_metadata(fallback.get('metadata', {}), target.get('metadata', {}))})
                    mapped = {**deepcopy(lead), 'a':fallback['id'] if lead['a'] == source['id'] else lead['a'],
                              'b':fallback['id'] if lead['b'] == source['id'] else lead['b']}
                    if mapped not in obj.setdefault('matches', []):
                        obj['matches'].append(mapped)
                if leads:
                    coverage = obj.setdefault('search_coverage', {})
                    coverage.setdefault('source_errors', {})[member['path']] = previous.get('search_coverage', {}).get('source_errors', {}).get(member['path'], '')
                    coverage['candidate_records_shown'] = sum(bool(c.get('reference_object_id')) for c in obj['candidates'])


def pairing(rows, source_root):
    names, frames = defaultdict(list), defaultdict(list)
    def frame(parts, stem):
        if parts[0] != 'Auftrag 1614930' or len(parts) < 4:
            return None
        magazine, number = re.match(r'(\d+) Magazin', parts[2]), re.search(r'(\d{5})$', stem)
        return (int(magazine[1]), int(number[1])) if magazine and number else None
    for path in rows:
        p = Path(path); parts = p.relative_to(source_root).parts
        if p.suffix.lower() in ('.tif', '.tiff'):
            names[(parts[0], p.stem.casefold())].append(path)
            key = frame(parts, p.stem)
            if key:
                frames[key].append(path)
    result = {}
    for path in rows:
        p = Path(path); parts = p.relative_to(source_root).parts
        if p.suffix.lower() not in ('.jpg', '.jpeg'):
            continue
        if parts == ('Start', 'WebTemplate', 'SimpleViewer2', 'images', 'sandstone.jpg'):
            result[path] = {'role': 'website_asset', 'parents': []}; continue
        require(any(x.casefold() in DERIVATIVE_FOLDERS for x in parts[:-1]),
                f'JPEG outside the approved derivative folders: {path}')
        hits = names.get((parts[0], p.stem.casefold()), [])
        rule = 'same_delivery_and_identifier'
        if not hits and frame(parts, p.stem):
            hits = frames.get(frame(parts, p.stem), []); rule = 'same_delivery_magazine_and_frame'
        if not hits:
            # A suffix is only a lead, never an automatic parent choice.
            stem = re.sub(r' \d+$', '', p.stem.casefold())
            hits = [q for (batch, name), paths in names.items() if batch == parts[0]
                    and re.sub(r' \d+$', '', name) == stem for q in paths]
            rule = 'ambiguous_suffix_requires_review'
        result[path] = {'role': 'derivative', 'parents': hits, 'rule': rule,
                        'parent': hits[0] if len(hits) == 1 and rule != 'ambiguous_suffix_requires_review' else ''}
    return result


def rebuild(prior, output, source_root, actor='Vladimir'):
    prior, output = work_root(prior), work_root(output, must_exist=False)
    source_root = Path(source_root).absolute()
    require(source_root == source_root.resolve(), 'Use the canonical Avantmedia source root')
    elements_guard(source_root)
    require(not output.exists(), 'Use a fresh work root; existing reviews must remain intact')
    require(not output.is_relative_to(prior) and not prior.is_relative_to(output), 'Work root overlaps prior evidence')
    old = Workbench(prior, actor)
    require(not old.frozen, 'Completed or read-only review cannot be reclassified')
    for value in old.config['read_roots']:
        p = Path(value)
        require(not output.is_relative_to(p) and not p.is_relative_to(output), 'Work root overlaps source/archive')
    inputs = {}
    def read(name, lines=False):
        p = plain_file(prior / name); inputs[str(p)] = stamp(p)
        with p.open() as handle:
            return [json.loads(line) for line in handle if line.strip()] if lines else json.load(handle)
    summary = read('preparation-summary.json')
    require(summary['source_roots'] == [str(source_root)], 'This adapter requires one explicit Avantmedia root')
    inputs[str(prior / 'workbench-input.json')] = stamp(prior / 'workbench-input.json')
    decisions_existed = (prior / 'decisions.jsonl').exists()
    if decisions_existed:
        inputs[str(prior / 'decisions.jsonl')] = stamp(prior / 'decisions.jsonl')
    decisions, _ = old.read_decisions()
    rows = {r['source_path']: r for r in read('source-dispositions.jsonl', True)}
    metadata = {r['path']: r for r in read('metadata-cache.jsonl', True)}
    for path, row in rows.items():
        p = Path(path)
        require(p.is_relative_to(source_root) and p == p.resolve(), 'Source escapes approved root')
        require(stamp(p) == row['source_stamp'] == metadata[path]['stamp'], f'Source/cache changed: {path}')
    links = pairing(rows, source_root)
    source_candidates, references, reference_stamps = {}, {}, {}
    objects, initial = {}, {}
    saved = 0
    for oid, obj in old.objects.items():
        sources = [c for c in obj['candidates'] if not c.get('reference_object_id')]
        kinds = {Path(c['members'][0]['path']).suffix.lower() in ('.tif', '.tiff') for c in sources}
        require(len(kinds) == 1, 'Mixed TIFF/JPEG saved case needs explicit reconciliation')
        for c in obj['candidates']:
            if c.get('reference_object_id'):
                references[c['reference_object_id']] = c
                reference_stamps[c['members'][0]['path']] = {k: c['members'][0][k] for k in ('size_bytes', 'mtime_ns')}
            else:
                source_candidates[c['members'][0]['path']] = c
        decision = decisions[oid]
        if True in kinds:
            objects[oid] = deepcopy(obj)
            initial[oid] = {k: deepcopy(decision[k]) for k in EDIT_FIELDS}
            initial[oid]['actor'] = decision['actor'] or 'preparation: ' + BASIS
            if decision.get('decided_at'):
                saved += 1
                initial[oid]['decided_at'] = decision['decided_at']
                objects[oid].setdefault('prior_review_evidence', []).append({
                    'file': str(prior / 'decisions.jsonl'), 'record': {k: deepcopy(decision[k]) for k in
                    ('actor', 'decided_at', 'selected_candidate_id', 'redundant_candidate_ids', 'same_pairs',
                     'different_pairs', 'metadata_reviewed', 'destination_unit', 'note', 'previous_master_ids')},
                    'note': 'Saved choices carried forward; attached derivatives are preparation proposals.'})
            selected = decision.get('selected_candidate_id', '')
            chosen = next((c for c in obj['candidates'] if c['id'] == selected), None)
            for c in sources:
                r = rows[c['members'][0]['path']]
                if selected == c['id']:
                    r.update(disposition='proposed_master', retained_master_path=c['members'][0]['path'])
                elif c['id'] in decision.get('redundant_candidate_ids', []) and chosen:
                    r.update(disposition='redundant' if chosen.get('reference_object_id') else 'redundant_to_proposed_master',
                             retained_master_path=chosen['members'][0]['path'])
                    if chosen.get('reference_object_id'):
                        r.update(archive_object_ids=[chosen['reference_object_id']], destination_unit=chosen['archive_unit'])
        else:
            require(not decision.get('decided_at'), 'Saved JPEG decision needs reconciliation before reclassification')
    # Reuse observed Master stamps without reading or hashing any image payload.
    pixel_file = prior / 'pixel-coverage.jsonl'
    if pixel_file.exists():
        for row in read(pixel_file.name, True):
            reference_stamps[row['inputs']['target']] = row['inputs']['target_stamp']
    exact_file = prior / 'czkawka-blake3-signature.json'
    if exact_file.exists():
        reference_stamps.update(read(exact_file.name)['input_stamps'])
    catalogue_records = {}
    for unit in summary['units']:
        cat = Path(unit['unit']) / 'CATALOG.csv'
        require(unit['adapter'] == 'accession-catalogue', 'Supply the established Avantmedia accession-catalogue units')
        require(stamp(cat) == unit['input_stamps'][str(cat)], 'Archive catalogue changed after preparation')
        inputs[str(cat)] = stamp(cat)
        for row in csv_rows(cat):
            if row.get('file_role') in ('preservation_master', 'preservation_surrogate'):
                catalogue_records[str(cat.parent) + '#' + row['record_key']] = (cat, row)
    def reference(row):
        matches = [tid for tid in row['archive_object_ids'] if tid in catalogue_records and
                   str(catalogue_records[tid][0].parent / catalogue_records[tid][1]['final_relative_path']) == row['retained_master_path']]
        require(len(matches) == 1, 'Retained archive Master must have one qualified object identity')
        tid = matches[0]; cat, record = catalogue_records[tid]; path = row['retained_master_path']
        require(path in reference_stamps and stamp(path) == reference_stamps[path], 'Retained archive Master changed or lacks stamped evidence')
        if tid not in references:
            references[tid] = {'id': 'archive:' + tid, 'label': cat.parent.name + ' · ' + record['inventory'],
                'eligible': True, 'complete': True, 'reference_object_id': tid, 'archive_unit': str(cat.parent),
                'members': [{'path': path, **reference_stamps[path], 'pages': 1,
                             'sha256': record['final_sha256'].removeprefix('sha256:')}],
                'metadata': catalogue_metadata(record, cat), 'source_record': record,
                'evidence': 'Retained archive Master from the completed preparation; read-only'}
        return deepcopy(references[tid])
    def source_candidate(path):
        if path in source_candidates:
            return deepcopy(source_candidates[path])
        row, meta = rows[path], metadata[path]
        return {'id': 'source', 'label': str(Path(path).relative_to(source_root)),
                'eligible': row['review_reason'] != 'evidence_unavailable',
                'complete': row['review_reason'] != 'evidence_unavailable',
                'members': [{'path': path, **row['source_stamp'], 'pages': 1}],
                'metadata': meta['metadata'], 'metadata_errors': meta['errors'], 'source_roots': [str(source_root)],
                'evidence': 'TIFF candidate retained from the completed preparation'}
    def metadata_case(parent, ref):
        row = rows[parent]; oid = row['case_id']
        if oid not in objects:
            c = source_candidate(parent); c['id'] = 'source'
            objects[oid] = {'id': oid, 'title': c['label'], 'description': 'Image already covered; derivative metadata needs review.',
                'metadata': deepcopy(c['metadata']), 'review_reason': 'metadata_review', 'review_required': True,
                'metadata_review_required': True, 'recommended_candidate_id': ref['id'],
                'source_candidate_ids': [c['id']], 'confirmed_reference_ids': [ref['id']],
                'candidates': [c, ref], 'matches': [], 'metadata_comparisons': [],
                'search_coverage': {'issues': [], 'gaps': [], 'all_comparison_evidence': str(prior / 'matches.jsonl')}}
            initial[oid] = {'selected_candidate_id': ref['id'], 'redundant_candidate_ids': [c['id']],
                'same_pairs': [[c['id'], ref['id']]], 'destination_unit': ref['archive_unit'],
                'actor': 'preparation: ' + BASIS, 'metadata_reviewed': False}
        elif not any(c['members'][0]['path'] == parent for c in objects[oid]['candidates']):
            c = source_candidate(parent)
            c['id'] = 'source:' + hashlib.sha256(parent.encode()).hexdigest()[:16]
            objects[oid]['candidates'].append(c); objects[oid]['source_candidate_ids'].append(c['id'])
            initial[oid]['redundant_candidate_ids'].append(c['id'])
            initial[oid]['same_pairs'].append([c['id'], ref['id']])
        row.update(review_case_id=oid, review_reason='metadata_review', metadata_review_required=True)
    attached, ambiguous, assets = 0, 0, 0
    for path, link in links.items():
        row = rows[path]; old_case = row['case_id']
        row.update(source_role=link['role'], derivative_basis=BASIS, original_case_id=old_case)
        if link['role'] == 'website_asset':
            assets += 1
            row.update(disposition='excluded_noncollection_asset', review_case_id='',
                review_reason='website_asset', retained_master_path='', archive_object_ids=[])
            continue
        parent = link['parent']
        if not parent:
            ambiguous += 1
            obj = deepcopy(old.objects[old_case]); obj['review_reason'] = 'evidence_conflict'
            obj['description'] = 'Derivative parent is ambiguous; resolve the parent link before assigning redundancy.'
            obj['recommended_candidate_id'] = ''
            for c in obj['candidates']:
                if not c.get('reference_object_id'):
                    c.update(eligible=False, source_role='derivative', parent_candidates=link['parents'])
            obj.setdefault('search_coverage', {}).setdefault('issues', []).append('Unresolved derivative parent: ' + ', '.join(link['parents']))
            objects[old_case] = obj
            initial[old_case] = {'actor': 'preparation: ' + BASIS}
            row.update(disposition='unresolved', review_reason='evidence_conflict', retained_master_path='',
                       archive_object_ids=[], parent_candidates=link['parents'])
            continue
        attached += 1
        p = rows[parent]; oid = p['case_id']; covered = p['disposition'] == 'redundant'
        ref = reference(p) if covered else None
        target_meta = ref['metadata'] if covered else metadata[parent]['metadata']
        jm = metadata[path]
        question = metadata_question(compare_metadata(jm['metadata'], target_meta, bool(jm['errors'])))
        if covered and question and not old.config.get('archive_masters_fixed', False):
            metadata_case(parent, ref)
        row.update(case_id=oid, review_case_id=oid if oid in objects else '', derivative_of=parent,
            derivative_rule=link['rule'], metadata_review_required=question, archive_object_ids=deepcopy(p['archive_object_ids']))
        if oid not in objects:
            row.update(disposition='redundant', review_reason='redundant', retained_master_path=p['retained_master_path'],
                       destination_unit=p['destination_unit'])
            continue
        obj, decision = objects[oid], initial[oid]
        parent_c = next(c for c in obj['candidates'] if c['members'][0]['path'] == parent)
        cid = 'derivative:' + hashlib.sha256(path.encode()).hexdigest()[:16]
        c = source_candidate(path); c.update(id=cid, eligible=False, source_role='derivative',
            label='JPEG derivative · ' + str(Path(path).relative_to(source_root)),
            derivative_of=parent, parent_candidate_id=parent_c['id'], evidence=BASIS)
        obj['candidates'].append(c); obj['source_candidate_ids'].append(cid)
        decision.setdefault('same_pairs', []).append([parent_c['id'], cid])
        obj.setdefault('metadata_comparisons', []).extend({'a': cid, 'b': other['id'],
            'fields': compare_metadata(c['metadata'], other['metadata'], bool(c.get('metadata_errors') or other.get('metadata_errors')))}
            for other in obj['candidates'] if other['id'] != cid)
        if question:
            obj['metadata_review_required'] = True; decision['metadata_reviewed'] = False
        selected = decision.get('selected_candidate_id', '')
        adequate = selected and (selected == parent_c['id'] or parent_c['id'] in decision.get('redundant_candidate_ids', []))
        if adequate:
            decision.setdefault('redundant_candidate_ids', []).append(cid)
            chosen = next(c for c in obj['candidates'] if c['id'] == selected)
            row.update(disposition='redundant' if chosen.get('reference_object_id') else 'redundant_to_proposed_master',
                       retained_master_path=chosen['members'][0]['path'])
        else:
            row.update(disposition='unresolved', retained_master_path='')
        row.update(review_reason=obj['review_reason'], destination_unit=decision.get('destination_unit', ''))
    for obj in objects.values():
        if old.config.get('archive_masters_fixed', False):
            decision = initial[obj['id']]
            enable_jpeg_fallback(obj, decision)
            inherit_fallback_comparisons(obj, old.objects.values())
            if any(c.get('source_role') == 'master_fallback' for c in obj['candidates']):
                chosen = next((c for c in obj['candidates'] if c['id'] == decision.get('selected_candidate_id')), None)
                if chosen:
                    connected = Workbench.same_component(decision, chosen['id'])
                    for c in obj['candidates']:
                        path = c['members'][0]['path']
                        if path in rows and c['id'] in connected:
                            rows[path].update(source_role=c.get('source_role', 'source'),
                                disposition='proposed_master' if c['id'] == chosen['id'] else
                                    'redundant' if chosen.get('reference_object_id') else 'redundant_to_proposed_master',
                                retained_master_path=chosen['members'][0]['path'])
        for candidate in obj['candidates']:
            for member in candidate['members']:
                require(stamp(member['path']) == {k: member[k] for k in ('size_bytes', 'mtime_ns')}, 'Candidate changed during reclassification')
    for path, row in rows.items():
        require(stamp(path) == row['source_stamp'], 'Source changed during reclassification')
        row.update(deletion_authorized=False, source_modified=False, archive_modified=False)
    require(decisions_existed == (prior / 'decisions.jsonl').exists(), 'Decisions changed during reclassification')
    for path, observed in inputs.items():
        require(stamp(path) == observed, 'Evidence changed during reclassification')
    elements_guard(source_root)
    config = {k: deepcopy(v) for k, v in old.config.items() if k not in ('objects', 'initial_decisions', 'preparation_summary')}
    config.update(title='Avantmedia · TIFF Masters and derivative metadata', objects=list(objects.values()),
        initial_decisions=initial, preparation_summary=str(output / 'preparation-summary.json'))
    result = {**summary, 'prepared_at': datetime.now(timezone.utc).isoformat(), 'reclassified_from': str(prior),
        'reclassification_input_stamps': inputs,
        'review_cases': len(objects), 'counts': dict(Counter(o['review_reason'] for o in objects.values())),
        'source_dispositions': dict(Counter(r['disposition'] for r in rows.values())),
        'derivative_reclassification': {'basis': BASIS, 'linked_jpegs': attached, 'ambiguous_jpegs': ambiguous,
            'website_assets': assets, 'saved_choices_carried_forward': saved, 'comparisons_rerun': False},
        'source_modified': False, 'archive_modified': False, 'deletion_authorized': False, 'accession_authorized': False}
    output.mkdir()
    atomic_write(output / 'workbench-input.json', encoded(config))
    atomic_write(output / 'preparation-summary.json', encoded(result))
    atomic_write(output / 'source-dispositions.jsonl', b''.join(encoded(r) + b'\n' for r in rows.values()))
    atomic_write(output / 'skipped-confirmed.jsonl', b''.join(encoded(r) + b'\n' for r in rows.values()
        if not r['review_case_id'] and r['disposition'] == 'redundant'))
    out = io.StringIO(newline='')
    fields = ['source_path','source_role','derivative_of','derivative_rule','case_id','review_case_id','disposition',
        'review_reason','retained_master_path','archive_object_ids','destination_unit','metadata_review_required','deletion_authorized']
    writer = csv.DictWriter(out, fieldnames=fields); writer.writeheader()
    for row in rows.values():
        writer.writerow({k: json.dumps(row.get(k), ensure_ascii=False) if isinstance(row.get(k), (bool, list, dict)) else row.get(k, '') for k in fields})
    atomic_write(output / 'source-dispositions.csv', out.getvalue().encode())
    app = Workbench(output, actor)
    if saved:
        carried, _ = app.read_decisions()
        for oid, row in carried.items():
            if oid in decisions and decisions[oid].get('decided_at'):
                row['previous_master_ids'] = deepcopy(decisions[oid]['previous_master_ids'])
        app.validate(carried)
        atomic_write(output / 'decisions.jsonl', b''.join(encoded(carried[oid]) + b'\n' for oid in app.objects))
    app.rebuild_csv()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior-review', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    result = rebuild(args.prior_review, args.work_root, args.source)
    print(json.dumps({k: result[k] for k in ('review_cases', 'counts', 'source_dispositions', 'derivative_reclassification')}, indent=2))
