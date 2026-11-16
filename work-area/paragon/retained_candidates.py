"""Bounded retained-source decisions for the existing Paragon workbench.

Reuses the work catalogue logbook and human-readable JSON. No schema, archive,
source, or authoritative drain-register changes. Images are rendered in memory.
"""
import csv
import copy
import io
import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
ARCHIVE = Path('/mnt/volume/iss-archive/units/paragon')
SOURCE = Path('/media/vladimir/Elements/restore/archive')
REPORT = ROOT / 'retained-accession-candidates.json'
DECISIONS = ROOT / 'retained-decisions.json'
DRAIN_PROPOSALS = ROOT / 'retained-proposed-drain.json'
PENDING_ACCESSION = ROOT / 'accession-remaining-20260921.json'
ACCESSION_RECEIPT = ARCHIVE / 'db-restore/manifests/retained-accession-completion.json'
SECTION_ACCESSION_RECEIPT = ARCHIVE / 'db-restore/manifests/retained-sections-accession.json'
CONFIRMED_FINISH_RECEIPT = ARCHIVE / 'db-restore/manifests/confirmed-finish-20260921.json'
ACTION = 'review_retained_candidate'


def readjson(path):
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def evidence():
    report = readjson(REPORT)
    visual = readjson(ROOT / 'retained-visual-comparison.json')
    checked = {r['source_path']: r for r in readjson(ROOT / 'drain-evidence.json')}
    with (ROOT / 'retain-review.csv').open(newline='') as f:
        rows = list(csv.DictReader(f))
    with (ARCHIVE / 'metadata/objects.csv').open(newline='') as f:
        catalogue = {r['archival_label']: r for r in csv.DictReader(f)}
    groups = {p: g for family in visual['families'] for g in family['groups'] for p in g['paths']}
    assets = {}
    for row in rows:
        p = row['source_path']; g = groups.get(p, {}); stamp = checked[p]
        assets[p] = dict(path=p, name=Path(p).name, source='retained',
                         sha256=stamp['source_sha256'], size_bytes=stamp['source_size_bytes'],
                         mtime_ns=stamp['source_mtime_ns'], width=g.get('width'), height=g.get('height'),
                         decoded=bool(g), format=Path(p).suffix[1:].upper(),
                         inventories=row['inventory'], preservation_basis=row['preservation_basis'],
                         other_unit_links=stamp.get('other_unit_links', []))
    refs = []
    for ref in visual['references']:
        p = ref['path']; st = Path(p).stat()
        assets[p] = dict(path=p, name=Path(p).name, source='archive', decoded=True,
                         size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                         inventory=ref['inventory'], title=ref['title'])
        refs.append(assets[p])
    # The invitation's retired interior label must be compared with its actual
    # accessioned member, not only the cover chosen for the visual atlas.
    invitation = catalogue['VLV-1095']
    for relative, part in zip(invitation['access_file'].splitlines(),
                              invitation['master_parts'].splitlines(), strict=True):
        p = str(ARCHIVE / relative)
        if p not in assets:
            st = Path(p).stat()
            assets[p] = dict(path=p, name=Path(p).name, source='archive', decoded=True,
                             size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                             inventory='VLV-1095', title=invitation['descriptive_title'])
            refs.append(assets[p])
        assets[p]['part'] = part
        assets[p]['title'] = invitation['descriptive_title'] + ' · ' + part.replace('_', ' ')
    accessioned = {}
    if ACCESSION_RECEIPT.exists():
        receipt = readjson(ACCESSION_RECEIPT)
        if receipt.get('validation_result') == 'ok':
            for obj in receipt['objects']:
                accessioned[obj['inventory']] = dict(entity_id=obj['entity_id'],
                    master_path=obj['master_path'], access_path=obj['access_path'],
                    selected_source=obj['source']['path'], source_sha256=obj['source']['sha256'],
                    completed_at=receipt['completed_at'], receipt=str(ACCESSION_RECEIPT))
                p = str(ARCHIVE / obj['access_path']); st = Path(p).stat()
                assets[p] = dict(path=p, name=Path(p).name, source='archive', decoded=True,
                    size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                    inventory=obj['inventory'], title=obj['review']['proposed_metadata']['title'])
                refs.append(assets[p])
    section_accessions = {}
    if SECTION_ACCESSION_RECEIPT.exists():
        receipt = readjson(SECTION_ACCESSION_RECEIPT)
        if receipt.get('validation_result') == 'ok':
            for obj in receipt['carriers']:
                section_accessions[obj['inventory']] = dict(entity_id=obj['entity_id'],
                    member_count=len(obj['selected_master_members']), sections_added=len(obj['sections']),
                    completed_at=receipt['completed_at'], receipt=str(SECTION_ACCESSION_RECEIPT),
                    coverage_complete=obj['coverage_complete'])
                for section in obj['sections']:
                    p = str(ARCHIVE / section['access_path']); st = Path(p).stat()
                    assets[p] = dict(path=p, name=Path(p).name, source='archive', decoded=True,
                        size_bytes=st.st_size, mtime_ns=st.st_mtime_ns, inventory=obj['inventory'],
                        title=section['embedded_metadata']['descriptive_title'], part=section['part'])
                    refs.append(assets[p])
    confirmed_sources = {}
    if CONFIRMED_FINISH_RECEIPT.exists():
        receipt = readjson(CONFIRMED_FINISH_RECEIPT)
        if receipt.get('result') == 'passed':
            for item in receipt['files']:
                if item['media']:
                    confirmed_sources[item['source']] = item
                if item['kind'] != 'section':
                    continue
                p = str(ARCHIVE / item['access_path']); st = Path(p).stat()
                assets[p] = dict(path=p, name=Path(p).name, source='archive', decoded=True,
                    size_bytes=st.st_size, mtime_ns=st.st_mtime_ns, inventory=item['inventory'],
                    title=item['embedded']['descriptive_title'], part=item['part'])
                refs.append(assets[p])
    with (ARCHIVE / 'db-restore/manifests/source-disposition.csv').open(newline='') as f:
        disposition_rows = list(csv.DictReader(f))
    # Current reconciliation can exclude a source after the archive checkpoint.
    # Use the work register for drain membership; final bindings still come from
    # the accessioned unit's manifest below.
    with (ROOT / 'source-disposition.planned.csv').open(newline='') as f:
        work_disposition_rows = list(csv.DictReader(f))
    drain_registered = {r['source_path'] for r in work_disposition_rows
                        if r['drain_state'] == 'candidate_after_separate_review'}
    selected_paths = {p for o in catalogue.values() for p in o['master_file'].splitlines()}
    source_bindings = {r['source_path']: dict(master_path=r['final_path'],
        master_sha256=r['final_sha256'], selected_source=r['source_path'],
        source_sha256=r['source_sha256'], inventory=r['inventory'],
        receipt='db-restore/manifests/source-disposition.csv')
        for r in disposition_rows if r['final_path'] in selected_paths and r['final_sha256']}
    pending = {}
    if PENDING_ACCESSION.exists():
        for row in readjson(PENDING_ACCESSION)['files']:
            p = row['source_path']
            if p in pending or p not in assets or assets[p]['sha256'] != row['source_sha256']:
                raise ValueError('Pending accession list does not match the exact retained sources')
            if p in drain_registered:
                raise ValueError('Pending accession list contains a registered drain path')
            pending[p] = row
    carrier_labels = readjson(ROOT / 'drain-candidate-review.json')['carrier_rule']['carrier_inventories']
    stamps = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in
              [REPORT, ROOT / 'retain-review.csv', ARCHIVE / 'metadata/objects.csv',
               ROOT / 'remaining-similarity-report.json', ROOT / 'similarity-drain.json',
               ARCHIVE / 'db-restore/manifests/source-disposition.csv',
               ROOT / 'source-disposition.planned.csv']}
    if PENDING_ACCESSION.exists():
        stamps[str(PENDING_ACCESSION)] = (PENDING_ACCESSION.stat().st_size, PENDING_ACCESSION.stat().st_mtime_ns)
    if CONFIRMED_FINISH_RECEIPT.exists():
        stamps[str(CONFIRMED_FINISH_RECEIPT)] = (CONFIRMED_FINISH_RECEIPT.stat().st_size, CONFIRMED_FINISH_RECEIPT.stat().st_mtime_ns)
    return dict(report=report, visual=visual, rows=rows, assets=assets,
                catalogue=catalogue, carriers=carrier_labels, references=refs, stamps=stamps,
                accessioned=accessioned, section_accessions=section_accessions, drain_registered=drain_registered,
                pending_accession=pending, confirmed_sources=confirmed_sources, source_bindings=source_bindings)


def verify_evidence():
    for path, expected in evidence()['stamps'].items():
        st = Path(path).stat()
        if (st.st_size, st.st_mtime_ns) != expected:
            raise ValueError('Review evidence changed. Restart and recheck the workbench.')


def verify_asset(asset):
    root = SOURCE if asset['source'] == 'retained' else ARCHIVE
    p = Path(asset['path'])
    if not p.resolve().is_relative_to(root):
        raise ValueError('Image is outside its approved root')
    st = p.stat()
    if (st.st_size, st.st_mtime_ns) != (asset['size_bytes'], asset['mtime_ns']):
        raise ValueError('File changed since the recorded evidence; review must be refreshed')
    return p


def archived_views(inventory):
    return [r for r in evidence()['references'] if r['inventory'] == inventory]


def base_case(key, label, title, kind, paths, why, priority=False):
    return dict(key=key, label=label, title=title, kind=kind, priority=priority,
                sources=[evidence()['assets'][p] for p in paths], references=[], members=[],
                why=why, qualifications='', date_evidence='', metadata_source=None,
                proposed_metadata=dict(title=title, date='', description='', provenance=''),
                default_parent='', can_add=kind not in ['carrier', 'unreadable', 'document'],
                add_block_reason='', default_source='', existing_inventory='')


def cases(con):
    ev = evidence(); catalogue = ev['catalogue']; used = set(); result = []
    initial = ev['report']['candidates'] + ev['report']['not_recommended_as_additional_objects']
    for raw in initial:
        paths = [x['path'] for x in raw['source_candidates']]
        obj = base_case(raw['inventory'], raw['inventory'], raw['title'], 'local', paths,
                        raw['visual_finding'], True)
        obj.update(qualifications=raw['qualifications'], date_evidence=raw['date_evidence'],
                   metadata_source=raw['metadata_source'], default_source=raw['display_source'] or '')
        meta = raw['metadata_source']['values']
        date = raw['date_evidence'].split(', according to the workbook')[0]
        if date in {'Undated', 'Photograph date unspecified', 'Unresolved'}:
            date = ''
        obj['proposed_metadata'] = dict(title=meta.get('object_title', raw['title']),
                                       date=date,
                                       description=meta.get('object_description', ''),
                                       provenance=meta.get('owner_previous', '') or 'Local Paragon workbook, Tabelle1 row ' + str(raw['metadata_source']['row']))
        obj['references'] = [ev['assets'][x['path']] | {'comparison_kind': raw['comparison_kind']}
                             for x in raw['archived_comparisons']]
        if raw['inventory'] == 'VLV-1047': obj['default_parent'] = 'VLV-1148'
        if raw['inventory'] in ['VLV-1042', 'VLV-1047', 'VLV-1099']:
            obj['proposed_metadata']['date'] = ''
        if raw['inventory'] in ['VLV-1019', 'VLV-1049', 'VLV-1057', 'VLV-1065', 'VLV-1095-1']:
            obj['can_add'] = False
            obj['add_block_reason'] = raw['visual_finding']
            obj['kind'] = 'exception'
        if raw['inventory'] == 'VLV-1095-1':
            obj['existing_inventory'] = 'VLV-1095'
            obj['references'] = [ref | {'comparison_kind': 'Already accessioned member of VLV-1095'}
                                 for ref in sorted(archived_views('VLV-1095'),
                                                   key=lambda ref: ref.get('part') != 'open_interior')]
            obj['why'] = ('This is the open interior of invitation VLV-1095. Its archived interior '
                          'is shown first on the right; the cover is also available in the selector.')
            obj['qualifications'] = ('The cover and open interior are already preserved together as '
                                     'one selected Master. Retain this local JPEG or record it as '
                                     'redundant/noise; adding it would duplicate the existing object.')
            obj['add_block_reason'] = obj['qualifications']
            obj['date_evidence'] = ('Printed invitation and accessioned catalogue: 1961. The workbook '
                                    'title/date say 1962, but its description says 1961.')
            obj['proposed_metadata']['date'] = ''
        result.append(obj); used.update(paths)

    relations = [dict(x) for x in con.execute(
        "SELECT a.archival_label member,b.archival_label carrier,r.status,r.confidence "
        "FROM entity_relations r JOIN archive_objects a ON a.entity_id=r.subject_entity_id "
        "JOIN archive_objects b ON b.entity_id=r.object_entity_id "
        "WHERE r.predicate='component_of' AND r.status='accepted'")]
    priority_carriers = {r['inventory'] for r in ev['report']['carrier_coverage']}
    for label in ev['carriers']:
        # Filename association is only a discovery lead. File roles stay unresolved.
        paths = [r['source_path'] for r in ev['rows'] if r['source_path'] not in used and
                 re.match(re.escape(label) + r'(?:[-.]|$)', Path(r['source_path']).name)]
        row = catalogue[label]
        obj = base_case(label, label, row['descriptive_title'], 'carrier', paths,
                        'Compare the whole carrier, its accepted member photographs and retained sources. A crop does not replace its carrier, and a carrier overview does not replace a distinct member.', label in priority_carriers)
        obj['references'] = archived_views(label)
        obj['proposed_metadata'] = dict(title=row['descriptive_title'], date=row['date_year'], description=row['description'], provenance='Accessioned catalogue ' + label)
        obj['qualifications'] = 'Source filenames suggest this family but do not prove a carrier/member relationship. Assign each retained file separately. Existing archive Masters remain unchanged.'
        obj['members'] = [dict(inventory=rel['member'], title=catalogue[rel['member']]['descriptive_title'],
                               description=catalogue[rel['member']]['description'], relation='component_of',
                               status=rel['status'], confidence=rel['confidence'],
                               references=archived_views(rel['member'])) for rel in relations if rel['carrier'] == label]
        result.append(obj); used.update(paths)

    for family in ev['visual']['families']:
        paths = [p for g in family['groups'] for p in g['paths'] if p not in used]
        if not paths: continue
        first = paths[0]; source = ev['assets'][first]
        labels = set()
        for p in paths:
            labels.update(x.strip() for x in ev['assets'][p]['inventories'].split('|') if x.strip() in catalogue)
        obj = base_case(first, Path(first).stem, Path(first).name, 'other', paths,
                        'Retained visual family from the complete screening. Similarity groups are display aids; confirm object identity before selecting a Master.')
        obj['default_source'] = family['display_path'] if family['display_path'] in paths else first
        obj['proposed_metadata'] = dict(title='', date='', description='', provenance='')
        if len(labels) == 1:
            label = next(iter(labels)); row = catalogue[label]
            obj['existing_inventory'] = label
            obj['references'] = archived_views(label)
            obj['can_add'] = False
            obj['add_block_reason'] = 'This family is associated with accessioned ' + label + '. Use metadata review to resolve a distinct-object identity before adding another object.'
            obj['title'] = label + ' · retained versions / context'
        else:
            near = next(g for g in family['groups'] if first in g['paths'])['nearest'][0]
            obj['references'] = [ev['assets'][near['path']] | {'comparison_kind': 'Similarity lead only; not a confirmed match'}]
        result.append(obj); used.update(paths)
    for row in ev['rows']:
        p = row['source_path']
        if p in used: continue
        image = Path(p).suffix.lower() in ['.jpg', '.jpeg', '.tif', '.tiff', '.png']
        obj = base_case(p, Path(p).stem, Path(p).name, 'unreadable' if image else 'document', [p],
                        'This image cannot be decoded; failure alone does not justify draining it.' if image else 'Retained metadata or project document; use a note to record its relevance.')
        obj['add_block_reason'] = obj['why']; result.append(obj); used.add(p)
    if used != {r['source_path'] for r in ev['rows']}:
        raise ValueError('Retained workbench does not account for every source path')
    if len({r['key'] for r in result}) != len(result):
        raise ValueError('Conflicting review case keys')
    # Only an explicitly saved same-item choice joins historically split cases.
    # Keep the old key as a redirect and preserve each path exactly once.
    by_key = {c['key']: c for c in result}
    for review in saved_reviews(con).values():
        pair = review.get('source_version_pair', {})
        other = pair.get('other_case_key')
        if other and other != review['case_key']:
            owner, donor = by_key[review['case_key']], by_key[other]
            owner['sources'].extend(donor['sources'])
            donor.update(sources=[], default_source='', can_add=False,
                         review_in_case=owner['key'],
                         add_block_reason='These source versions are reviewed with ' + owner['label'])
    # A completed version choice may no longer have an active local Master
    # selection. Preserve its already-saved donor redirect and file ownership.
    for donor_key, decision in saved_reviews(con).items():
        owner_key = decision.get('review_in_case')
        if owner_key and owner_key != donor_key:
            owner, donor = by_key[owner_key], by_key[donor_key]
            if donor['sources']:
                owner['sources'].extend(donor['sources'])
                donor.update(sources=[], default_source='', can_add=False,
                             review_in_case=owner['key'],
                             add_block_reason='These source versions are reviewed with ' + owner['label'])
    return result


def saved_reviews(con):
    saved = {}
    for row in con.execute('SELECT after_json FROM logbook WHERE action=? ORDER BY at_utc,log_id', (ACTION,)):
        review = json.loads(row['after_json'])
        if review.get('source_report') == REPORT.name: saved[review['case_key']] = review
    return saved


def catalogue_choices():
    return [dict(inventory=k, title=v['descriptive_title']) for k, v in evidence()['catalogue'].items()]


def index(con):
    saved = saved_reviews(con); all_cases = cases(con)
    pending = evidence().get('pending_accession', {})
    pictures = []
    for case in all_cases:
        review = saved.get(case['key'], {})
        states = {f['path']: f for f in review.get('files', [])}
        comparisons = source_comparisons(case)
        completed = (case['key'] in evidence()['accessioned'] or
                     case['key'] in evidence()['section_accessions'])
        locked = completed or bool(review.get('selected_master_path') or review.get('source_version_pair'))
        for source in case['sources']:
            if source['format'] not in {'JPG', 'JPEG', 'TIF', 'TIFF', 'PNG'}:
                continue
            state = states.get(source['path'], {}).get('state', 'unresolved')
            registered = source['path'] in evidence()['drain_registered']
            hint = comparisons.get(source['path'])
            pictures.append({k: source[k] for k in ('path', 'name', 'width', 'height', 'decoded', 'format')} |
                dict(case_key=case['key'], case_label=case['label'], state=state,
                     remaining=source['decoded'] and state == 'unresolved' and not registered and not locked and review.get('action') != 'ignore',
                     read_only=registered or locked or review.get('action') == 'ignore' or state not in {'unresolved', 'retain', 'redundant'}, registered=registered,
                     excluded_from_paragon=review.get('action') == 'ignore',
                     pending_accession=pending.get(source['path']),
                     comparison=hint,
                     high_resolution=bool(source['decoded'] and max(source['width'], source['height']) > 1600)))
    return dict(cases=[{k: c[k] for k in ['key', 'label', 'title', 'kind', 'priority']} |
                       dict(source_count=len(c['sources']), member_count=len(c['members']),
                            accessioned=c['key'] in evidence().get('accessioned', {}),
                            sections_accessioned=c['key'] in evidence().get('section_accessions', {}),
                            registered_drain_count=sum(s['path'] in evidence().get('drain_registered', set()) for s in c['sources']),
                            decision=saved.get(c['key'], {}).get('action', ''),
                            unresolved_source_count=sum(
                                s['path'] not in evidence()['drain_registered'] and
                                next((f['state'] for f in saved.get(c['key'], {}).get('files', [])
                                      if f['path'] == s['path']), 'unresolved') == 'unresolved'
                                for s in c['sources']),
                            reviewed_at=saved.get(c['key'], {}).get('reviewed_at', '')) for c in all_cases],
                pictures=pictures,
                pending_count=sum(bool(p['pending_accession']) for p in pictures),
                pending_selected_count=sum(bool(p['pending_accession']) and p['state'] == 'selected_master' for p in pictures),
                source_count=len(evidence()['rows']), carriers=evidence()['carriers'],
                catalogue=catalogue_choices(), saved_count=len(saved),
                accession_authorized=False, deletion_authorized=False)


@lru_cache(maxsize=1)
def hires_pairs():
    """The bounded 42-pair shortlist, using the completed comparison evidence."""
    report = readjson(ROOT / 'remaining-similarity-report.json')
    completed = {u['source_path'] for u in readjson(ROOT / 'similarity-drain.json')['updates']}
    available = {s['path'] for s in report['sources']} - completed
    pairs = {}
    for source in report['sources']:
        if source['path'] not in available or Path(source['path']).suffix.lower() not in {'.jpg', '.jpeg'}:
            continue
        options = [t for t in source['candidates'] if t['target'] in available
                   and t['target_source'] == 'retained'
                   and Path(t['target']).suffix.lower() in {'.tif', '.tiff'}
                   and t['target_width'] > source['width'] and t['target_height'] > source['height']
                   and t['shared_embedded_document_ids'] and t['aspect_relative_difference'] < .01]
        if not options:
            continue
        target = min(options, key=lambda t: (-t['algorithm_count'],
                     sum(t['indexed_hashes']['distance']), -t['area_ratio'], t['target']))
        pairs[source['path']] = dict(key=source['path'], name=source['name'],
            case_key=source['case_key'], target_case_key=target['target_case_key'],
            source_path=source['path'], target_path=target['target'], comparison=target)
    return pairs


def hires_context(con):
    return {c['key']: c for c in cases(con)}, saved_reviews(con)


def hires_case(con, key, context=None):
    pair = hires_pairs().get(key)
    if not pair:
        raise ValueError('Unknown JPEG/TIFF comparison')
    all_cases, saved = context or hires_context(con)
    owner = copy.deepcopy(all_cases[pair['case_key']])
    other_key = pair['target_case_key']
    involved = list(dict.fromkeys([owner['key'], other_key]))
    combined_files = {f['path']: copy.deepcopy(f)
                      for k in involved for f in saved.get(k, {}).get('files', [])}
    if other_key != owner['key']:
        owner['sources'].extend(copy.deepcopy(all_cases[other_key]['sources']))
    decision = saved.get(owner['key'], {})
    registered = evidence()['drain_registered']
    source, target = (evidence()['assets'][pair[k]] for k in ['source_path', 'target_path'])
    file_states = {p: combined_files.get(p, {}).get('state', 'unresolved')
                   for p in [source['path'], target['path']]}
    resolved = any(p in registered or state != 'unresolved' for p, state in file_states.items())
    # The named catalogue association is retained. This is a source proposal,
    # never a second object or an update to an accessioned Master.
    can_choose = (not resolved and owner['kind'] == 'other'
                  and not evidence()['accessioned'].get(owner['key'])
                  and not evidence()['section_accessions'].get(owner['key'])
                  and source['decoded'] and target['decoded'])
    return dict(pair=pair, name=pair['name'], label=owner['label'],
                source=source, target=target, file_states=file_states,
                metadata=copy.deepcopy(decision.get('proposed_metadata', owner['proposed_metadata'])),
                metadata_source=copy.deepcopy(decision.get('metadata_source', owner['metadata_source'])),
                existing_inventory=owner['existing_inventory'],
                previous_reviews={k: saved.get(k, {}).get('reviewed_at', '') for k in involved},
                resolved=resolved, can_choose=can_choose,
                reviewed_at=decision.get('reviewed_at', ''),
                left_unresolved=not resolved and 'source_version_note' in decision,
                selected_master_path=decision.get('selected_master_path', ''),
                saved_note=decision.get('source_version_note', decision.get('source_version_pair', {}).get('note', '')),
                owner=owner, combined_files=combined_files)


def hires_index(con):
    context = hires_context(con)
    rows = []
    for key in hires_pairs():
        c = hires_case(con, key, context)
        rows.append({k: c[k] for k in ['name', 'label', 'resolved', 'can_choose', 'selected_master_path', 'left_unresolved']} |
                    dict(key=key, case_key=c['pair']['case_key'],
                         split_case=c['pair']['case_key'] != c['pair']['target_case_key']))
    proposals = drain_proposals(con)['files']
    return dict(pairs=rows, total=len(rows), remaining=sum(not x['resolved'] for x in rows),
                registered_drain=len(evidence()['drain_registered']),
                pending_drain=sum(not x['in_drain_register'] for x in proposals),
                accession_authorized=False, deletion_authorized=False)


@lru_cache(maxsize=1)
def stored_comparisons():
    return {s['path']: s['candidates'] for s in
            readjson(ROOT / 'remaining-similarity-report.json')['sources']}


def public_hires_case(con, key):
    c = hires_case(con, key)
    result = {k: v for k, v in c.items() if k not in {'owner', 'combined_files'}}
    registered = evidence()['drain_registered']
    result['earlier_files'] = [dict(path=p, state=f['state'], registered=p in registered,
        evidence=[label for field, label in [('duplicate_evidence', 'Equal stored hash'),
                   ('derivative_evidence', 'Recorded derivative'),
                   ('similarity_evidence', 'Reviewed similarity')]
                  if f.get(field)]) for p, f in c['combined_files'].items()
        if p not in c['file_states'] and (f['state'] != 'unresolved' or p in registered)]
    # A direct stored comparison to a saved Master is context, never permission
    # to join objects, import metadata, or replay the earlier choice.
    selected = {r['selected_master_path']: r for r in saved_reviews(con).values()
                if r.get('selected_master_path') and r['case_key'] not in c['previous_reviews']}
    matches = {}
    for path in c['file_states']:
        for match in stored_comparisons().get(path, []):
            target = match['target']
            if target not in selected or target not in evidence()['assets']:
                continue
            rank = (-match['algorithm_count'], sum(match['indexed_hashes']['distance']))
            if target not in matches or rank < matches[target][0]:
                matches[target] = (rank, match)
    related = []
    for path, (_, match) in sorted(matches.items(), key=lambda item: (item[1][0], item[0])):
        review = selected[path]
        related.append(dict(asset=evidence()['assets'][path], case_key=review['case_key'],
            pair_key=review.get('source_version_pair', {}).get('source_path', ''),
            label=review['inventory'], reviewed_at=review['reviewed_at'],
            strength=match['strength'], comparison_only=True))
    result['related_saved_masters'] = related
    return result


def save_hires_review(con, payload, actor):
    """Save one human pair choice to the existing case records and logbook."""
    c = hires_case(con, payload.get('key'))
    if payload.get('previous_reviews') != c['previous_reviews']:
        raise ValueError('This comparison changed in another tab. Reload before saving.')
    if not c['can_choose']:
        raise ValueError('This pair already has a decision or is no longer available for Master review.')
    note = text_field(payload.get('note', ''), 'Review note', 1000)
    action = payload.get('action')
    if action not in {'add', 'unresolved'}:
        raise ValueError('Select one Master or leave this pair unresolved')
    pair = c['pair']; owner = c['owner']; selected = payload.get('selected_master_path', '')
    if action == 'add' and (selected not in {pair['source_path'], pair['target_path']}
                          or payload.get('same_object_confirmed') is not True):
        raise ValueError('Select one of these two files and confirm the same item and full frame')
    saved = saved_reviews(con)
    old = saved.get(owner['key'], {})
    # Only the two displayed files may acquire new dispositions. Retain all
    # earlier decisions and evidence, including other unresolved coverage.
    files = []
    for source in owner['sources']:
        state = c['combined_files'].get(source['path'])
        if state is None:
            state = dict(path=source['path'], sha256=source['sha256'], size_bytes=source['size_bytes'],
                         mtime_ns=source['mtime_ns'], state='unresolved', member_target='',
                         relation='', carrier='', deletion_authorized=False)
        files.append(copy.deepcopy(state))
    if action == 'unresolved':
        # A note does not join cases or alter any previous file decision.
        original = {s['path'] for s in hires_context(con)[0][owner['key']]['sources']}
        files = [f for f in files if f['path'] in original]
        review = copy.deepcopy(old)
        if not review:
            review = build_review(owner, dict(action='unresolved',
                metadata=c['metadata'], note=note), actor)
        review.update(files=files, note=old.get('note', review.get('note', '')),
                      source_version_note=note,
                      actor=actor, reviewed_at=datetime.now(timezone.utc).isoformat())
        record_review(con, review)
        return review
    for source in [c['source'], c['target']]:
        verify_asset(source)
    # Reuse validation on the exact displayed pair; preserve the item's saved
    # name/metadata and use its existing key even when the TIFF was split out.
    bounded = copy.deepcopy(owner)
    bounded.update(sources=[c['source'], c['target']], can_add=True, decision=old,
                   registered_drain_paths=[], accession=None, section_accession=None)
    review = build_review(bounded, dict(action='add', preserve_metadata=True,
        selected_master_path=selected, same_object_confirmed=True,
        note=old.get('note', '')), actor)
    for f in files:
        if f['path'] in {pair['source_path'], pair['target_path']}:
            f['state'] = 'selected_master' if f['path'] == selected else 'redundant'
    if sum(f['state'] == 'selected_master' for f in files) != 1:
        raise ValueError('This item already has another Master choice; resolve that choice first')
    review.update(files=files, source_version_pair=dict(
        source_path=pair['source_path'], target_path=pair['target_path'],
        other_case_key=pair['target_case_key'], original_name=pair['name'],
        existing_inventory=c['existing_inventory'], note=note,
        evidence_report='remaining-similarity-report.json', comparison=pair['comparison']))
    record_review(con, review)
    if pair['target_case_key'] != owner['key']:
        other = copy.deepcopy(saved.get(pair['target_case_key'], {}))
        if not other:
            other = build_review(hires_context(con)[0][pair['target_case_key']],
                                 dict(action='retain'), actor)
        other.update(action='retain', files=[], selected_master_path='',
                     review_in_case=owner['key'], actor=actor, reviewed_at=review['reviewed_at'])
        record_review(con, other)
    return review


@lru_cache(maxsize=1)
def comparison_evidence():
    """Reuse the bounded recheck; it supplies display hints, never decisions."""
    path = ROOT / 'accessioned-similarity-recheck.json'
    if not path.exists():
        return {}, {}, {}
    report = readjson(path)
    inputs = readjson(ROOT / 'remaining-similarity-inputs.json')
    return ({r['path']: r for r in report['sources']},
            {r['path']: r for r in inputs['unresolved'] + inputs['references']},
            report['input_stamps'])


def source_comparisons(obj):
    rows, cached, controls = comparison_evidence()
    result = {}
    documented = {r['source_path']: r for r in
                  evidence()['report'].get('confirmed_member_comparisons', [])
                  if r['case_key'] == obj['key']}
    # Accession changes catalogue exports without changing every image. Reuse
    # only comparisons whose exact files and current Master association agree.
    # Changed images remain unavailable; the old search cannot cover new files.
    catalogue_changed = any(controls.get(str(path)) != [path.stat().st_size, path.stat().st_mtime_ns]
                            for path in (ARCHIVE / 'CATALOG.csv', ARCHIVE / 'metadata/objects.csv'))
    for source in obj['sources']:
        accession = evidence().get('confirmed_sources', {}).get(source['path'])
        if accession:
            result[source['path']] = dict(finding='stale', reference=None)
            proof = accession['source_evidence']
            reference = evidence()['assets'].get(str(ARCHIVE / accession['access_path']))
            if not reference or source['sha256'] != proof['source_sha256']:
                continue
            if any(source[k] != proof['source_' + k] for k in ('size_bytes', 'mtime_ns')):
                continue
            try:
                verify_asset(source); verify_asset(reference)
                master = archive_master_details(reference)
                unchanged = all([Path(ARCHIVE / f['path']).stat().st_size,
                                 Path(ARCHIVE / f['path']).stat().st_mtime_ns] == f['stamp']
                                for f in accession['media'])
            except (OSError, ValueError):
                continue
            if not unchanged or not master or master['master_path'] != str(ARCHIVE / accession['master_path']):
                continue
            result[source['path']] = dict(finding='accessioned_source', reference=reference,
                master_dimensions=master['master_dimensions'], master_format=master['master_format'],
                accession_receipt=str(CONFIRMED_FINISH_RECEIPT))
            continue
        proof = documented.get(source['path'])
        if proof:
            result[source['path']] = dict(finding='stale', reference=None)
            reference = evidence()['assets'].get(proof['reference_path'])
            if not reference or source['sha256'] != proof['source_sha256']:
                continue
            if any(source[k] != proof['source_' + k] for k in ('size_bytes', 'mtime_ns')):
                continue
            if any(reference[k] != proof['reference_' + k] for k in ('size_bytes', 'mtime_ns')):
                continue
            try:
                verify_asset(source); verify_asset(reference)
                master = archive_master_details(reference)
            except (OSError, ValueError):
                continue
            if not master or any(master[k] != proof[k] for k in
                    ('master_path', 'master_dimensions', 'master_size_bytes', 'master_mtime_ns')):
                continue
            if reference.get('inventory') != proof['inventory'] or reference.get('part') != proof['part']:
                continue
            result[source['path']] = dict(finding='documented_member', reference=reference,
                master_dimensions=master['master_dimensions'], master_format=master['master_format'],
                catalogue_evidence=copy.deepcopy(proof))
            continue
        row = rows.get(source['path'])
        if not row:
            continue
        stale = dict(finding='stale', reference=None)
        result[source['path']] = stale
        old = cached.get(source['path'], {})
        if row['sha256'] != source['sha256'] or any(
                old.get(k) != source[k] for k in ('size_bytes', 'mtime_ns')):
            continue
        try:
            verify_asset(source)
        except (OSError, ValueError):
            continue
        if row['bucket'] not in {'close', 'near'}:
            result[source['path']] = dict(finding='no_close_hash_match', reference=None,
                catalogue_changed=catalogue_changed)
            continue
        match = row['best']
        reference = evidence()['assets'].get(match['target'])
        old = cached.get(match['target'], {})
        if not reference or reference['source'] != 'archive' or any(
                old.get(k) != reference[k] for k in ('size_bytes', 'mtime_ns')):
            continue
        try:
            verify_asset(reference)
        except (OSError, ValueError):
            continue
        current = evidence()['catalogue'].get(reference['inventory'], {})
        access_paths = current.get('access_file', '').splitlines()
        master_paths = current.get('master_file', '').splitlines()
        relative = str(Path(reference['path']).relative_to(ARCHIVE))
        if relative not in access_paths or len(master_paths) != len(access_paths):
            continue
        master = ARCHIVE / master_paths[access_paths.index(relative)]
        if str(master) != match['master_path']:
            continue
        with Image.open(master) as im:
            dimensions = list(im.size)
            master_format = im.format
        if dimensions != match['master_dimensions']:
            continue
        filename_label = re.match(r'^(V(?:LV|LL)-\d+(?:-\d+)?)(?=[._ ]|$)', source['name'])
        label = filename_label.group(1) if filename_label else ''
        result[source['path']] = dict(finding=row['bucket'],
            reference=reference | {'comparison_kind': 'Suggested visual comparison'},
            distance=match['distance'], filename_label=label,
            filename_mismatch=bool(label and label != reference['inventory']),
            manufactured_surrogate=match['master_role'] == 'preservation_surrogate',
            master_dimensions=match['master_dimensions'],
            master_format=master_format,
            source_has_more_pixels=match['master_pixel_ratio'] < .5)
    return result


def archive_master_details(reference):
    """Describe the actual selected member paired with this access preview."""
    current = evidence()['catalogue'].get(reference['inventory'], {})
    access = current.get('access_file', '').splitlines()
    masters = current.get('master_file', '').splitlines()
    relative = str(Path(reference['path']).relative_to(ARCHIVE))
    if relative not in access or len(access) != len(masters):
        return None
    master = ARCHIVE / masters[access.index(relative)]
    if not master.resolve().is_relative_to(ARCHIVE):
        raise ValueError('Master is outside the archive')
    st = master.stat()
    with Image.open(master) as im:
        return dict(master_path=str(master), master_dimensions=list(im.size),
                    master_format=im.format, master_size_bytes=st.st_size,
                    master_mtime_ns=st.st_mtime_ns)


def get_case(con, key):
    obj = next((c for c in cases(con) if c['key'] == key), None)
    if obj is None: raise ValueError('Unknown retained review case')
    obj['decision'] = saved_reviews(con).get(key)
    obj['accession'] = evidence().get('accessioned', {}).get(key)
    obj['section_accession'] = evidence().get('section_accessions', {}).get(key)
    if obj['section_accession']:
        obj['qualifications'] = ''
    obj['registered_drain_paths'] = [s['path'] for s in obj['sources']
                                     if s['path'] in evidence().get('drain_registered', set())]
    if obj['accession']:
        obj['references'] = archived_views(obj['label']) + obj['references']
    obj['hires_review_key'] = next((p['key'] for p in hires_pairs().values()
                                   if key in {p['case_key'], p['target_case_key']}), '')
    obj['source_comparisons'] = ({} if obj['accession'] or obj['section_accession']
                                  else source_comparisons(obj))
    # A full-image search can miss raw carrier sections and cropped members.
    # Expose the existing accepted relationships as comparison context only;
    # they do not make a raw scan interchangeable with a photograph Master.
    obj['carrier_context'] = []
    if obj['kind'] == 'carrier':
        for ref in obj['references']:
            obj['carrier_context'].append(ref | (archive_master_details(ref) or {}) |
                dict(view_label=('Carrier section · ' + ref['inventory'] + ' · ' + ref['part'])
                     if ref.get('part') else 'Whole carrier · ' + ref['inventory']))
        for member in obj['members']:
            for ref in member['references']:
                obj['carrier_context'].append(ref | (archive_master_details(ref) or {}) |
                    dict(view_label='Photograph · ' + member['inventory']))
    obj['pending_sources'] = {}
    for source in obj['sources']:
        pending = evidence().get('pending_accession', {}).get(source['path'])
        if not pending:
            continue
        # A historical catalogue label is context only, never a supported
        # similarity match or a reason to enable a redundancy decision.
        label = pending['existing_catalogue_association']
        context = next((ref for ref in obj['references'] if ref.get('inventory') == label), None)
        if context is None and label:
            context = next(iter(archived_views(label)), None)
        if context:
            context = context | (archive_master_details(context) or {})
        obj['pending_sources'][source['path']] = dict(pending,
            catalogue_context=context)
    return obj


def text_field(value, label, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(label + ' is invalid or too long')
    return value.strip()


def build_review(obj, payload, actor):
    if obj.get('section_accession'):
        raise ValueError('These carrier sections have been accessioned. Their completed decisions cannot be changed in retained review.')
    if obj.get('accession'):
        raise ValueError('This object has been accessioned. Its completed Master decision cannot be changed in retained review.')
    action = payload.get('action')
    if action not in {'add', 'metadata_review', 'ignore', 'retain', 'noise_drain', 'unresolved'}:
        raise ValueError('Choose a supported review action')
    note = text_field(payload.get('note', ''), 'Review note', 2000)
    metadata = payload.get('metadata', {})
    if not isinstance(metadata, dict): raise ValueError('Invalid metadata')
    metadata = {k: text_field(metadata.get(k, ''), k, 3000 if k == 'description' else 500)
                for k in ['title', 'date', 'description', 'provenance']}
    relation = payload.get('relation', 'unresolved'); parent = payload.get('parent', '')
    preserve_metadata = action == 'add' and payload.get('preserve_metadata') is True
    previous_review = obj.get('decision') or {}
    metadata_source = obj['metadata_source']
    metadata_reviewed = payload.get('metadata_reviewed') is True
    if preserve_metadata:
        # A source-version choice must not replace the item's description,
        # attribution, identity or unresolved relationship with client input.
        metadata = copy.deepcopy(previous_review.get('proposed_metadata', obj['proposed_metadata']))
        metadata_source = copy.deepcopy(previous_review.get('metadata_source', metadata_source))
        metadata_reviewed = previous_review.get('metadata_reviewed', False)
        relationship = previous_review.get('relationship', dict(predicate='unresolved', carrier=''))
        relation, parent = relationship['predicate'], relationship['carrier']
    if relation not in {'standalone', 'component_of', 'unresolved'}:
        raise ValueError('Unsupported object relationship')
    if relation == 'component_of' and parent not in evidence()['carriers']:
        raise ValueError('Choose an existing carrier for this member')
    if relation != 'component_of': parent = ''
    selected = payload.get('selected_master_path', '')
    if not isinstance(selected, str): raise ValueError('Invalid Master choice')
    source_paths = {s['path'] for s in obj['sources']}
    if selected and selected not in source_paths: raise ValueError('Master is not a source in this case')
    if action == 'add':
        if not obj['can_add']: raise ValueError(obj['add_block_reason'] or 'Resolve this case before adding an object')
        if not selected: raise ValueError('Select exactly one Master for the proposed object')
        if not evidence()['assets'][selected]['decoded']: raise ValueError('Unreadable files cannot be selected as Masters')
        if not preserve_metadata:
            if not metadata['title'] or not metadata['description'] or not metadata['provenance']:
                raise ValueError('A title, description and provenance are required; an unknown date may remain blank')
            if relation == 'unresolved': raise ValueError('Confirm standalone object or member of a carrier')
            if not metadata_reviewed: raise ValueError('Review and confirm the metadata, including any conflicts')
        if len({s['sha256'] for s in obj['sources']}) > 1 and payload.get('same_object_confirmed') is not True:
            raise ValueError('Confirm that these candidates are versions of one object, or leave the case for metadata review')
    else:
        selected = ''

    file_input = payload.get('file_choices', {})
    if not isinstance(file_input, dict) or not set(file_input).issubset(source_paths):
        raise ValueError('File choices must refer only to sources shown in this case')
    states = []
    previous_files = {f['path']: f for f in (obj.get('decision') or {}).get('files', [])}
    for source in obj['sources']:
        previous = previous_files.get(source['path'], {})
        choice = file_input.get(source['path'], {})
        if not isinstance(choice, dict): raise ValueError('Invalid file choice')
        state = choice.get('state', 'unresolved'); target = choice.get('member_target', '')
        if state not in {'unresolved', 'retain', 'carrier_section', 'member', 'redundant', 'noise_drain'}:
            raise ValueError('Unsupported source-file choice')
        if action == 'add': state = 'selected_master' if source['path'] == selected else 'redundant'
        if action in {'ignore', 'metadata_review', 'unresolved'}: state = 'unresolved'; target = ''
        if state in {'carrier_section', 'member'} and obj['kind'] != 'carrier':
            raise ValueError('Assign sections and member content in their carrier case')
        if state == 'member' and target and target not in {m['inventory'] for m in obj['members']}:
            raise ValueError('Member target must be an existing member of this carrier, or left for object review')
        if state != 'member': target = ''
        if state in {'noise_drain', 'redundant'} and action != 'add':
            registered_unchanged = (source['path'] in obj.get('registered_drain_paths', [])
                                    and previous.get('state') == state
                                    and previous.get('sha256') == source['sha256'])
            if source.get('other_unit_links') and not registered_unchanged:
                raise ValueError('This file has another-unit evidence; leave it retained until ownership is resolved')
        states.append(dict(path=source['path'], sha256=source['sha256'], size_bytes=source['size_bytes'],
                           mtime_ns=source['mtime_ns'], state=state, member_target=target,
                           relation='section_of' if state == 'carrier_section' else 'component_of' if state == 'member' else '',
                           carrier=obj['label'] if state in {'carrier_section', 'member'} else '',
                           deletion_authorized=False))
        previous = previous_files.get(source['path'], {})
        if source['path'] in obj.get('registered_drain_paths', []) and state != previous.get('state'):
            raise ValueError('This file decision is already recorded in the archive drain list. It cannot be changed by a review proposal.')
        if state in {'redundant', 'noise_drain'} and previous.get('sha256') == source['sha256'] and previous.get('derivative_evidence'):
            states[-1]['derivative_evidence'] = previous['derivative_evidence']
        if state == previous.get('state') and previous.get('sha256') == source['sha256']:
            for key in ['duplicate_evidence', 'rejection_evidence', 'similarity_evidence']:
                if key in previous: states[-1][key] = previous[key]
    if action == 'noise_drain' and not any(s['state'] == 'noise_drain' for s in states):
        raise ValueError('Mark the exact source files proposed as noise/drain')
    if sum(s['state'] == 'selected_master' for s in states) != int(action == 'add'):
        raise ValueError('Exactly one Master is required for each proposed resolved object')
    retention = payload.get('retention', {})
    if not isinstance(retention, dict): raise ValueError('Invalid retention choices')
    carrier_retention = retention.get('carrier', 'retain')
    if carrier_retention not in {'retain', 'unresolved'}: raise ValueError('Existing carrier retention may be confirmed or left unresolved')
    members = retention.get('members', {})
    allowed_members = {m['inventory'] for m in obj['members']}
    if not isinstance(members, dict) or not set(members).issubset(allowed_members) or any(v not in {'retain', 'unresolved'} for v in members.values()):
        raise ValueError('Invalid member retention choices')
    return dict(case_key=obj['key'], inventory=obj['label'], source_report=REPORT.name,
                action=action, selected_master_path=selected, proposed_metadata=metadata,
                metadata_source=metadata_source, metadata_reviewed=metadata_reviewed,
                preserve_metadata=preserve_metadata,
                relationship=dict(predicate=relation, carrier=parent),
                retention=dict(carrier=carrier_retention, members={k: members.get(k, 'retain') for k in allowed_members}) if obj['kind'] == 'carrier' else {},
                files=states, note=note, actor=actor, reviewed_at=datetime.now(timezone.utc).isoformat(),
                same_object_confirmed=payload.get('same_object_confirmed') is True,
                coverage_confirmed=payload.get('coverage_confirmed') is True,
                archive_changed=False, accession_authorized=False, deletion_authorized=False)


def record_review(con, review):
    unit = con.execute("SELECT unit_id,status FROM units WHERE unit_id='WF-PARAGON'").fetchone()
    if not unit or unit['status'] != 'open': raise ValueError('The existing Paragon work unit must be open')
    con.execute('INSERT INTO logbook (log_id,at_utc,actor,unit_id,action,entity_type,entity_id,before_json,after_json,evidence,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('LOG-' + uuid4().hex.upper(), review['reviewed_at'], review['actor'], unit['unit_id'], ACTION,
                 'reconciliation_decision', review['case_key'], '{}', json.dumps(review, ensure_ascii=False), REPORT.name, review['note']))


def build_picture_review(obj, payload, actor):
    """Apply one picture choice using existing file states and readable records."""
    if obj.get('accession') or obj.get('section_accession'):
        raise ValueError('This completed accession decision cannot be changed in picture review')
    previous = obj.get('decision') or {}
    if previous.get('selected_master_path') or previous.get('source_version_pair'):
        raise ValueError('An existing Master choice must not be changed by a picture review')
    path = payload.get('source_path')
    source = next((s for s in obj['sources'] if s['path'] == path), None)
    if not source or not source['decoded']:
        raise ValueError('Choose a readable retained picture from this review')
    if path in obj.get('registered_drain_paths', []):
        raise ValueError('This file is already in the drain register')
    previous_file = next((f for f in previous.get('files', []) if f['path'] == path), {})
    if previous_file.get('state', 'unresolved') not in {'unresolved', 'retain', 'redundant'}:
        raise ValueError('This earlier file choice is read-only in picture review')
    state = payload.get('file_state')
    if state not in {'redundant', 'retain', 'unresolved'}:
        raise ValueError('Choose redundant, consider for accession, or unresolved')
    hint = obj.get('source_comparisons', {}).get(path, {})
    reference = hint.get('reference')
    if payload.get('comparison_path', '') != (reference['path'] if reference else ''):
        raise ValueError('The displayed comparison changed. Reload before saving')
    if state == 'redundant' and not reference:
        raise ValueError('A supported accessioned comparison is required to mark this image redundant')
    note = text_field(payload.get('note', ''), 'Picture note', 1000)
    # Revisiting a saved choice is not a new decision. In particular, an empty
    # note must not replace the saved explanation with generic text.
    if previous_file.get('state') == state and not note:
        return copy.deepcopy(previous)
    existing = {f['path']: f for f in previous.get('files', [])}
    choices = {p: dict(state=f['state'], member_target=f.get('member_target', '')) for p, f in existing.items()}
    choices[path] = dict(state=state, member_target='')
    relationship = previous.get('relationship', dict(predicate='unresolved', carrier=''))
    review = build_review(obj, dict(action='retain', file_choices=choices,
        metadata=previous.get('proposed_metadata', obj['proposed_metadata']),
        metadata_reviewed=previous.get('metadata_reviewed', False),
        relation=relationship['predicate'], parent=relationship['carrier'],
        retention=previous.get('retention', {}), note=previous.get('note', ''),
        same_object_confirmed=previous.get('same_object_confirmed', False),
        coverage_confirmed=previous.get('coverage_confirmed', False)), actor)
    # Only the displayed source may change. Preserve all earlier per-file
    # evidence and unrelated metadata/relationships, including unknown fields.
    for i, file in enumerate(review['files']):
        if file['path'] != path and file['path'] in existing:
            review['files'][i] = copy.deepcopy(existing[file['path']])
        elif file['path'] == path:
            review['files'][i] = copy.deepcopy(existing.get(path, {})) | file
    target = next(f for f in review['files'] if f['path'] == path)
    basis = {'redundant': 'Human confirmed the retained image is redundant to the displayed accessioned image.',
             'retain': 'Retained for accession consideration: a possible new image, additional content or better Master. No Master selected.',
             'unresolved': 'Picture left unresolved; no identity or redundancy confirmed.'}[state]
    target['note'] = basis + (' ' + note if note else '')
    if reference and hint.get('catalogue_evidence'):
        target['catalogue_evidence'] = copy.deepcopy(hint['catalogue_evidence']) | dict(
            evidence_report=REPORT.name, decision_basis=basis,
            comparison_is_exact_byte_copy=False, deletion_authorized=False)
    elif reference:
        match = comparison_evidence()[0][path]['best']
        target['similarity_evidence'] = dict(source_path=path, source_sha256=source['sha256'],
            comparison_target=copy.deepcopy(match) | {'target_source': 'archive'},
            evidence_report='accessioned-similarity-recheck.json', basis=basis,
            comparison_is_exact_byte_copy=False, deletion_authorized=False)
    if previous:
        preserved = copy.deepcopy(previous)
        preserved.update(files=review['files'], actor=actor, reviewed_at=review['reviewed_at'])
        # The case action only describes the grouped saved files; it must not
        # turn a former ignore/unresolved choice into a hidden picture decision.
        preserved['action'] = 'retain'
        return preserved
    return review


def readable_decisions(con):
    return dict(source_report=REPORT.name, catalogue='work/catalog.sqlite',
                reviews=sorted(saved_reviews(con).values(), key=lambda r: r['case_key']),
                accession_authorized=False, deletion_authorized=False)


def drain_proposals(con):
    completed = evidence().get('accessioned', {})
    def coverage(review, file):
        # The current exact-path register also covers accessions completed after
        # the original retained-object batch. Avoid stale awaiting-accession flags.
        bindings = evidence().get('source_bindings', {})
        if file['path'] in bindings:
            return bindings[file['path']]
        target = file.get('derivative_evidence', {}).get('coverage_target', {})
        if target.get('path') in bindings:
            return bindings[target['path']]
        if review['action'] == 'add' and review['case_key'] in completed:
            return completed[review['case_key']]
        target = file.get('derivative_evidence', {}).get('coverage_target', {})
        return next((a for a in completed.values() if target.get('path') == a['selected_source']
                     or target.get('sha256') == a['source_sha256']), None)
    return dict(source_report=DECISIONS.name, purpose='Confirmed derivatives belong on the drain list without waiting for accession. Covering-source accession is provenance information, not a listing prerequisite. No deletion is authorized.',
                files=[dict(path=f['path'], sha256=f['sha256'], reason=f.get('note', r['note']), disposition=f['state'],
                            inventory=r['inventory'], reviewed_at=r['reviewed_at'],
                            in_drain_register=f['path'] in evidence().get('drain_registered', set()),
                            awaiting_accession=not bool(coverage(r, f)) and r['action'] == 'add',
                            covering_source_awaits_accession=not bool(coverage(r, f)) and bool(f.get('derivative_evidence', {}).get('awaiting_accession', False)),
                            deletion_authorized=False) |
                       ({'derivative_evidence': f['derivative_evidence']} if 'derivative_evidence' in f else {}) |
                       ({'duplicate_evidence': f['duplicate_evidence']} if 'duplicate_evidence' in f else {}) |
                       ({'rejection_evidence': f['rejection_evidence']} if 'rejection_evidence' in f else {}) |
                       ({'similarity_evidence': f['similarity_evidence']} if 'similarity_evidence' in f else {}) |
                       ({'catalogue_evidence': f['catalogue_evidence']} if 'catalogue_evidence' in f else {}) |
                       ({'accession_coverage': coverage(r, f)} if coverage(r, f) else {})
                       for r in saved_reviews(con).values() if r['action'] != 'ignore'
                       for f in r['files'] if f['state'] in {'noise_drain', 'redundant'}],
                deletion_authorized=False)


def write_json(path, data):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w') as out:
        out.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n'); out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)


def export_decisions(con):
    write_json(DECISIONS, readable_decisions(con))
    write_json(DRAIN_PROPOSALS, drain_proposals(con))


def check(con):
    verify_evidence(); all_cases = cases(con)
    for asset in evidence()['assets'].values(): verify_asset(asset)
    for path, expected in [(DECISIONS, readable_decisions(con)), (DRAIN_PROPOSALS, drain_proposals(con))]:
        if path.exists() and readjson(path) != expected: raise ValueError(path.name + ' differs from SQLite')
    paths = [s['path'] for c in all_cases for s in c['sources']]
    if len(paths) != len(set(paths)) or len(paths) != len(evidence()['rows']):
        raise ValueError('Each retained path must occur in exactly one decision case')
    if not set(evidence().get('pending_accession', {})).issubset(paths):
        raise ValueError('A pending accession source is missing from the workbench')
    saved = saved_reviews(con)
    if not set(saved).issubset({c['key'] for c in all_cases}): raise ValueError('A saved case is missing from the current evidence')
    return dict(ok=True, cases=len(all_cases), priority_cases=sum(c['priority'] for c in all_cases),
                retained_paths=len(paths), carrier_cases=sum(c['kind'] == 'carrier' for c in all_cases),
                accepted_members=sum(len(c['members']) for c in all_cases), saved_reviews=len(saved),
                pending_accession=len(evidence().get('pending_accession', {})),
                decisions_agree=DECISIONS.exists(), accession_authorized=False, deletion_authorized=False)


@lru_cache(maxsize=96)
def _image(path, size, stamp):
    with Image.open(path) as original:
        original.draft('RGB', (size, size))
        im = ImageOps.exif_transpose(original)
        im.thumbnail((size, size))
        out = io.BytesIO(); im.convert('RGB').save(out, format='JPEG', quality=90)
    return out.getvalue()


def image_bytes(path, full=False):
    asset = evidence()['assets'].get(path)
    if not asset or not asset['decoded']: raise ValueError('No decodable registered image at this path')
    verify_asset(asset)
    return _image(path, 6000 if full else 1500, asset['mtime_ns'])
