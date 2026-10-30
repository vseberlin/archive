#!/usr/bin/env python3
"""Build reviewable accession records in the existing work directory only.

No archive creation, source copying, derivative generation, hash recalculation,
new database, or changes to human decisions. Run after document review.
"""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import collection_551_workbench as wb

ROOT = wb.ROOT
TARGET = Path('/mnt/volume/iss-archive/units/FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF')


def write_csv(name, fields, rows):
    wb.atomic_replace(ROOT / name, wb.csv_payload(fields, rows))


def write_json(name, value):
    wb.atomic_replace(ROOT / name, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def distinct(values):
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def main():
    w = wb.Workbench('Vladimir')
    check = w.check()
    assert check['resolved_objects'] == 79 and not w.ordered_objects()
    assert not TARGET.exists(), 'An accession unit already exists; do not overwrite it.'
    decisions = w.decisions()
    assert wb.read_csv(wb.DISPOSITIONS_CSV) == w.dispositions_for(decisions)
    assert wb.read_csv(wb.DRAIN_CSV) == w.drain_for(decisions)
    metadata = []
    field_records = []
    blockers = []
    visible_difference_ids = {r['difference_id'] for group in w.metadata_groups_by_id.values() for r in group['members']}
    hidden_variants = []
    for obj in w.objects:
        oid = obj['md_object_id']
        seed = json.loads(w.metadata_by_id[oid]['md_values_json'])
        effective = dict(seed)
        by_field = defaultdict(list)
        for r in w.metadata_differences_by_object.get(oid, []):
            by_field[r['field']].append(r)
        for field, rows in by_field.items():
            # A hidden, semantically equivalent pending row is corroboration,
            # not a newly invented human decision. Keep the raw row unchanged.
            unresolved = [r for r in rows if r['decision_action'] == 'pending'
                          and r['difference_id'] in visible_difference_ids]
            if unresolved:
                blockers.append({'md_object_id': oid, 'field': field,
                                 'reason': 'non-equivalent pending metadata'})
            substitutes = distinct(r['local_value'] for r in rows if r['decision_action'] == 'substitute')
            additions = distinct(r['local_value'] for r in rows if r['decision_action'] == 'add')
            basis = 'MD seed; saved accept/ignore decisions and equivalent corroboration retained'
            variants = [r for r in rows if r['decision_action'] == 'pending'
                        and r['difference_id'] not in visible_difference_ids
                        and not wb.equivalent_to_md(field, r['md_value'], r['local_value'])]
            if variants:
                hidden_variants.extend({'md_object_id': oid, 'inventory': obj['inventory'], 'field': field,
                    'difference_id': r['difference_id'], 'md_value': r['md_value'], 'local_value': r['local_value'],
                    'treatment': 'Preserve MD seed and the unconfirmed variant as explicit source evidence; do not mark the raw row accepted.'} for r in variants)
                basis += '; hidden non-identical source variants remain explicitly unconfirmed evidence'
            if substitutes:
                if len(substitutes) > 1 and field != 'storage_location':
                    blockers.append({'md_object_id': oid, 'field': field,
                                     'reason': 'multiple distinct substitute values', 'values': substitutes})
                effective[field] = '; '.join(substitutes)
                basis = 'saved substitute values; all distinct values retained'
            elif field not in effective:
                accepted = distinct(r['effective_value'] for r in rows if r['decision_action'] == 'accept')
                if accepted:
                    effective[field] = '; '.join(accepted)
            if additions:
                effective[field] = '; '.join(distinct([effective.get(field, '')] + additions))
                basis += '; saved additions retained'
            if field in {'tags', 'storage_location'}:
                effective[field] = '; '.join(distinct(effective.get(field, '').split(';')))
            field_records.append({
                'md_object_id': oid, 'inventory': obj['inventory'], 'field': field,
                'md_value': seed.get(field, ''), 'effective_value': effective.get(field, ''),
                'basis': basis, 'evidence_json': json.dumps(rows, ensure_ascii=False),
                'deletion_authorized': 'false',
            })
        metadata.append({'md_object_id': oid, 'inventory': obj['inventory'],
                         'object_url': obj['object_url'], 'metadata': effective})
    assert not blockers, json.dumps(blockers, ensure_ascii=False)
    by_object = {r['md_object_id']: r for r in metadata}
    masters = []
    representations = []
    derivatives = []
    for obj in w.objects:
        oid = obj['md_object_id']; d = decisions[oid]
        selected = w.selected_candidates(oid, d)
        resources = {r['resource_id']: r for r in w.resources_by_object[oid]}
        targets = []
        for sequence, c in enumerate(selected, 1):
            src = Path(c['candidate_path'])
            relative = f"masters/{obj['inventory']}__{sequence:03d}__{src.name}"
            targets.append(relative)
            resource = resources.get(c['resource_id'], {})
            row = {
                'md_object_id': oid, 'inventory': obj['inventory'],
                'representation_type': d['master_representation_type'],
                'candidate_id': c['candidate_id'], 'resource_id': c['resource_id'],
                'source_kind': c['source_kind'], 'match_basis': c['match_basis'],
                'sequence': sequence, 'source_path': str(src),
                'source_url': resource.get('source_url', '') if c['source_kind'] == 'museum_digital_capture' else '',
                'related_md_resource_url': resource.get('source_url', ''),
                'source_size_bytes': c['size_bytes'], 'source_mtime_ns': src.stat().st_mtime_ns,
                'source_sha256': c['known_sha256'], 'proposed_relative_path': relative,
                'source_hash_basis': 'stored candidate hash reused' if c['known_sha256'] else 'not previously recorded; no source rehash during preparation',
                'final_sha256': '', 'format': c['format'], 'page_count': c['page_count'],
                'md_reference_resource_rights': resource.get('rights', ''), 'md_reference_resource_owner': resource.get('owner', ''),
                'metadata_policy': 'Preserve selected bytes; reconcile metadata in catalogue and readable evidence. No unproven embedding path.',
                'deletion_authorized': 'false',
            }
            masters.append(row)
            derivatives.append({'md_object_id': oid, 'inventory': obj['inventory'],
                'master_relative_path': relative,
                'proposed_relative_path': f"export/{Path(relative).stem}__preview.jpg",
                'operation': 'PDF first-page JPEG preview, longest edge 1600 px' if src.suffix.lower() == '.pdf' else 'TIFF/image JPEG access preview, longest edge 1600 px, no upscaling',
                'generation_time': 'during separately authorized accession only',
                'source': 'final selected Master', 'generated': 'false'})
        representations.append({'md_object_id': oid, 'inventory': obj['inventory'],
            'representation_type': d['master_representation_type'], 'member_count': len(selected),
            'ordered_master_paths': json.dumps(targets, ensure_ascii=False),
            'selected_resource_ids': d['selected_resource_ids'], 'actor': d['actor'],
            'decided_at': d['decided_at'], 'decision_note': d['decision_note'], 'deletion_authorized': 'false'})
    assert len(representations) == len({r['md_object_id'] for r in representations}) == 79
    assert len({r['proposed_relative_path'] for r in masters}) == len(masters)
    technical = wb.read_csv(ROOT / 'accession-master-technical-checks.csv')
    assert {r['source_path'] for r in technical} == {r['source_path'] for r in masters}
    assert all(r['result'] == 'ok' for r in technical)
    documents = wb.read_csv(ROOT / 'accession-document-review.csv')
    retained = [r for r in documents if r['decision'] == 'included']
    assert retained and all(r['content_evidence'] and r['current_source_check'] == 'unchanged' for r in retained)
    assert len({r['proposed_relative_path'] for r in retained}) == len(retained)
    assert all(r['relationship'] in {'direct', 'mixed', 'contextual'} for r in retained)
    for name, rows in [('accession-master-files.csv', masters),
                       ('accession-master-representations.csv', representations),
                       ('accession-derivatives-plan.csv', derivatives),
                       ('accession-metadata-fields.csv', field_records)]:
        write_csv(name, list(rows[0]), rows)
    wb.atomic_replace(ROOT / 'accession-metadata.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in metadata).encode())
    catalogue = []
    for r in masters:
        md = by_object[r['md_object_id']]['metadata']
        catalogue.append({'proposed_relative_path': r['proposed_relative_path'],
            'file_role': 'master_member' if r['representation_type'] == 'ordered_md_resource_set' else 'master',
            'md_object_id': r['md_object_id'], 'inventory': r['inventory'], 'sequence': r['sequence'],
            'title': md.get('title', ''), 'description': md.get('description', ''),
            'date': md.get('date', ''), 'creator': md.get('author', ''), 'source_path': r['source_path'],
            'source_sha256': r['source_sha256'], 'final_sha256': '',
            'metadata_json': json.dumps(md, ensure_ascii=False)})
    for r in retained:
        catalogue.append({'proposed_relative_path': r['proposed_relative_path'], 'file_role': 'documentation',
            'md_object_id': '', 'inventory': '', 'sequence': '', 'title': r['description'],
            'description': r['content_evidence'], 'date': r['document_date'], 'creator': '',
            'source_path': r['source_path'], 'source_sha256': r['content_sha256'], 'final_sha256': '',
            'metadata_json': json.dumps({'relationship': r['relationship'], 'date_basis': r['date_basis']}, ensure_ascii=False)})
    write_csv('accession-catalogue.csv', list(catalogue[0]), catalogue)
    # One exact-path register combines every existing drain row, every candidate,
    # and every supporting-document source and duplicate. No path is deletable.
    register = {}
    source_to_target = {r['source_path']: r['proposed_relative_path'] for r in masters}
    doc_to_target = {r['content_sha256']: r['proposed_relative_path'] for r in retained}
    for r in w.drain_for(decisions):
        register[r['source_path']] = {'source_path': r['source_path'], 'source_kind': r['source_kind'],
            'unit': 'collection-551', 'inventory': r['inventory'], 'source_role': r['source_class'],
            'final_path': '', 'preservation_status': 'not_accessioned', 'preservation_basis': r['evidence'],
            'source_sha256': r['sha256'], 'final_sha256': '', 'source_size_bytes': r['size_bytes'],
            'exact_copy_proven': 'false', 'drain_state': 'blocked_until_separately_authorized_validated_accession',
            'deletion_authorized': 'false', 'notes': r['note']}
    dispositions = {r['candidate_id']: r for r in w.dispositions_for(decisions)}
    for c in w.candidates:
        path = c['candidate_path']; disposition = dispositions[c['candidate_id']]['disposition']
        register[path] = {'source_path': path, 'source_kind': c['source_kind'], 'unit': 'collection-551',
            'inventory': c['inventory'], 'source_role': disposition, 'final_path': source_to_target.get(path, ''),
            'preservation_status': 'not_accessioned', 'preservation_basis': 'Saved human Master selection; redundant means not selected, never safe to delete.',
            'source_sha256': c['known_sha256'], 'final_sha256': '', 'source_size_bytes': c['size_bytes'],
            'exact_copy_proven': 'false', 'drain_state': 'retain_selected_master_source' if path in source_to_target else 'blocked_until_separate_path_specific_review',
            'deletion_authorized': 'false', 'notes': 'final_path is proposed only; no output or coverage is claimed.'}
    candidates_by_path = defaultdict(list)
    for c in w.candidates:
        candidates_by_path[c['candidate_path']].append(c)
    for path, candidates in candidates_by_path.items():
        register[path]['inventory'] = '; '.join(distinct(c['inventory'] for c in candidates))
        register[path]['notes'] += ' Candidate relations: ' + json.dumps([
            {'candidate_id': c['candidate_id'], 'md_object_id': c['md_object_id'],
             'disposition': dispositions[c['candidate_id']]['disposition']} for c in candidates], ensure_ascii=False)
    for r in documents:
        path = r['source_path']; target = doc_to_target.get(r['content_sha256'], '')
        retained_document = bool(target) and r['decision'] in {'included', 'redundant'}
        noise = r['decision'] == 'redundant' and r.get('review_note', '').startswith('Noise:')
        register[path] = {'source_path': path, 'source_kind': 'work_document' if wb.inside(Path(path), ROOT) else 'protected_usb_source', 'unit': 'collection-551',
            'inventory': '', 'source_role': 'documentation_noise' if noise else ('supporting_document' if r['decision'] == 'included' else 'byte_identical_document_copy') if retained_document else 'held_document_candidate',
            'final_path': target, 'preservation_status': 'not_accessioned', 'preservation_basis': r['content_evidence'],
            'source_sha256': r['content_sha256'], 'final_sha256': '', 'source_size_bytes': r['size_bytes'],
            'exact_copy_proven': 'false', 'drain_state': ('retain_mixed_project_source' if r['relationship'] == 'mixed' else 'blocked_until_separately_authorized_validated_accession') if retained_document else 'retain_unconfirmed_or_excluded_source',
            'deletion_authorized': 'false', 'notes': r.get('review_note', '') + ' ' + ('Original bytes retained at accession; every duplicate path recorded; proposed destination only.' if retained_document else 'Outside proposed accession payload; no source-disposal claim.') + ' Condition: ' + r['condition']}
    for path in wb.unrelated_document_paths(documents):
        register.pop(path, None)
    write_csv('accession-source-disposition.csv', list(next(iter(register.values()))), sorted(register.values(), key=lambda r: r['source_path']))
    manifest = {
        'prepared_at': wb.now(), 'unit': 'collection-551', 'title': 'Forschungs- und Entwicklungsberichte aus dem WF',
        'proposed_target': str(TARGET), 'accession_performed': False, 'accession_execution_authorized': False,
        'preparation_complete': True, 'master_objects': len(representations),
        'master_representation_types': dict(Counter(r['representation_type'] for r in representations)),
        'master_files': len(masters), 'master_bytes': sum(int(r['source_size_bytes']) for r in masters),
        'master_formats': dict(Counter(r['format'] for r in masters)),
        'master_source_kinds': dict(Counter(r['source_kind'] for r in masters)),
        'master_source_hashes_unrecorded': [r['source_path'] for r in masters if not r['source_sha256']],
        'documentation_files': len(retained), 'documentation_bytes': sum(int(r['size_bytes']) for r in retained),
        'documentation_exceptions': [{'source_path': r['source_path'], 'decision': r['decision'], 'condition': r['condition']} for r in documents if r['decision'] == 'needs_review' or r['condition'].startswith(('damaged', 'unreadable'))],
        'document_source_paths': sum(bool(doc_to_target.get(r['content_sha256'])) and r['decision'] in {'included', 'redundant'} for r in documents),
        'planned_derivatives': len(derivatives), 'derivatives_generated': 0, 'source_disposition_rows': len(register),
        'raw_metadata_decisions_preserved': True,
        'hidden_pending_metadata_rows': sum(r['decision_action'] == 'pending' for r in w.metadata_differences),
        'hidden_nonidentical_metadata_variants': hidden_variants,
        'metadata_conflicts_policy': 'Saved additions retained together; original evidence and decisions accompany reconciled values. Storage references and physical locations both retained.',
        'structure': ['00_README.md', 'CATALOG.csv', 'CATALOG.xlsx', '00_MANIFESTS/', 'masters/', 'export/', 'metadata/', 'documents/', 'db-restore/'],
        'hash_policy': 'Reuse unchanged candidate/document hashes; hash each final payload once after all accession operations. Final hash fields deliberately empty in preparation.',
        'catalogue_policy': 'CSV, XLSX, JSONL and a small rebuildable SQLite catalogue generated together during accession. No database created during preparation.',
        'deletion_authorized': False, 'source_modified': False, 'archive_modified': False,
        'remaining_execution_requirement': 'Explicit authorization to create this output unit and execute accession.',
    }
    write_json('accession-preparation.json', manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
