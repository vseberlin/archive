#!/usr/bin/env python3
"""Prepare one AEB accession in the existing work directory; --check writes nothing."""
import argparse
import csv
import io
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

from prepare_591 import ROOT, SOURCE, guard, read_csv
from workbench_591 import Review, atomic

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools/archive-preservation'))
from reconciliation_workbench import checked_legacy_handoff

TARGET = Path('/mnt/volume/iss-archive/units/aeb')
DIRS = ('00_MANIFESTS', 'masters', 'export', 'metadata', 'documents', 'db-restore')
PLAN = ROOT / 'accession-plan.json'
SOURCE_FIELDS = 'source_path,source_kind,unit,inventory,source_role,final_path,preservation_status,preservation_basis,source_sha256,final_sha256,source_size_bytes,exact_copy_proven,drain_state,deletion_authorized,notes'.split(',')
FILE_FIELDS = 'relative_path,file_role,md_object_id,inventory,selected_candidate_id,resource_id,sequence,title,description,date,creator,object_url,rights,owner,match_status,match_basis,source_path,source_url,original_filename,source_size_bytes,source_mtime_ns,source_sha256,metadata_path,master_relative_path,pages,width,height,size_bytes,final_sha256'.split(',')
EVIDENCE = {
    'master-decisions.csv': '00_MANIFESTS/master-decisions.csv',
    'candidate-dispositions.csv': '00_MANIFESTS/candidate-dispositions.csv',
    'documentation-review.csv': '00_MANIFESTS/documentation-review.csv',
    'objects.csv': 'metadata/md-objects.csv',
    'candidates.csv': 'metadata/candidates.csv',
    'md-resources.csv': 'metadata/md-resources.csv',
    'md-export.json': 'metadata/md-export.json',
    'quality-comparisons.csv': 'metadata/quality-comparisons.csv',
    'local-search-evidence.json': 'metadata/local-search-evidence.json',
    'local-context-review.json': 'metadata/local-context-review.json',
    'visual-search-leads.json': 'metadata/visual-search-leads.json',
    'AEB-040-page-comparison.json': 'metadata/AEB-040-page-comparison.json',
    'exact-hash-matches.json': 'metadata/exact-hash-matches.json',
    'capture-summary.json': 'metadata/capture-summary.json',
    'content-query-summary.json': 'metadata/content-query-summary.json',
    'object-title-query-summary.json': 'metadata/object-title-query-summary.json',
    'visual-search-summary.json': 'metadata/visual-search-summary.json',
    'documentation-candidate-index.json': 'metadata/documentation-candidate-index.json',
    'documentation-search-queries.json': 'metadata/documentation-search-queries.json',
    'documentation-search-index-evidence.json': 'metadata/documentation-search-index-evidence.json',
    'documentation-internal-evidence.json': 'metadata/documentation-internal-evidence.json',
}
CONTROLS = ['00_README.md', 'CATALOG.csv', 'CATALOG.xlsx',
            '00_MANIFESTS/master-representations.csv', '00_MANIFESTS/master-files.csv',
            '00_MANIFESTS/derivatives.csv', '00_MANIFESTS/source-disposition.csv',
            '00_MANIFESTS/payload-checksums.csv', '00_MANIFESTS/accession-completion.json',
            'documents/CONTENTS.md', 'db-restore/catalog.sqlite', 'db-restore/README.md']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def csv_bytes(rows):
    require(bool(rows), 'Cannot infer fields for empty table')
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def stamp(path):
    stat = Path(path).stat()
    return str(stat.st_size), str(stat.st_mtime_ns)


def checked_input(path, size=None, mtime=None):
    path = Path(path)
    resolved = path.resolve(strict=True)
    require(path.is_file() and not path.is_symlink(), f'Not a regular source: {path}')
    require(resolved.is_relative_to(ROOT.resolve()) or resolved.is_relative_to(SOURCE.resolve()),
            f'Outside the approved source roots: {path}')
    actual = stamp(path)
    if size is not None:
        require(actual == (str(size), str(mtime)), f'Source changed: {path}')
    return actual


def checked_relative(relative):
    path = Path(relative)
    require(not path.is_absolute() and '..' not in path.parts, f'Unsafe destination: {relative}')
    require(len(path.parts) == 2 and path.parts[0] in DIRS, f'Unapproved nesting: {relative}')
    return path


def file_row(**values):
    require(set(values) <= set(FILE_FIELDS), 'Unknown catalogue field')
    return {key: str(values.get(key, '')) for key in FILE_FIELDS}


def source_row(**values):
    defaults = dict(unit='aeb', preservation_status='proposed_only_no_accession',
                    final_sha256='', exact_copy_proven='false',
                    drain_state='retain_source_no_deletion_authority', deletion_authorized='false',
                    notes='final_path is proposed only; no accession coverage is claimed.')
    defaults.update(values)
    return {key: str(defaults.get(key, '')) for key in SOURCE_FIELDS}


def build_plan():
    consolidated_revision = checked_legacy_handoff(ROOT, 'collection-591')
    mount = guard()
    require(TARGET.parent.resolve() == Path('/mnt/volume/iss-archive/units'), 'Unexpected archive path')
    require(not TARGET.exists() and not TARGET.is_symlink(), 'AEB output already exists; refusing overwrite')
    allowed = {'00_README.md', '00_MANIFESTS', 'units', '90_UNRECONCILED', '99_TRANSIENT_REFERENCE'}
    require({p.name for p in TARGET.parent.parent.iterdir()} == allowed, 'Archive structure differs')
    review = Review()
    health = review.check()
    require(health['resolved_objects'] == 68 and health['documentation_pending_groups'] == 0,
            'Master or documentation decisions remain unresolved')
    decisions = review.decisions()
    md_export = json.loads((ROOT / 'md-export.json').read_text())['results']['objects']
    exported_objects = {str(row['object_id']): row for row in md_export.values()}
    require(set(exported_objects) == set(decisions), 'MD capture and decision identities disagree')
    resources = {r['resource_id']: r for r in review.resources}
    require(len(resources) == 109, 'Resource IDs are not unique')
    files, objects, derivatives, sources, technical = [], [], [], [], []
    snapshots = {}
    for obj in review.objects:
        oid = obj['md_object_id']
        decision = decisions[oid]
        candidate = review.by_candidate[decision['selected_candidate_id']]
        require(candidate['candidate_id'] == 'md-' + oid and candidate['eligible'] == 'true',
                f'This accession plan requires the approved MD Master: {oid}')
        require(decision['decision'] == 'selected_master' and decision['actor'] and decision['decided_at'],
                f'Master decision is not confirmed: {oid}')
        require(re.fullmatch(r'[A-Za-z0-9_-]+', obj['inventory']), 'Inventory needs a safe filename mapping')
        members = json.loads(candidate['members_json'])
        expected = sorted((r for r in review.resources if r['md_object_id'] == oid), key=lambda r: int(r['sequence']))
        require([m['resource_id'] for m in members] == [r['resource_id'] for r in expected],
                f'MD member order or completeness differs: {oid}')
        metadata = json.loads(obj['metadata_json'])
        require(metadata == exported_objects[oid], f'Object metadata differs from the preserved MD capture: {oid}')
        raw_resources = {str(r['quell_id']): r for r in metadata['object_images']}
        events = metadata.get('object_events', [])
        dates = list(dict.fromkeys(f"{e.get('event_type_name', '')}: {e['time']['time_name']}".strip(': ')
                                  for e in events if isinstance(e.get('time'), dict) and e['time'].get('time_name')))
        ordered_paths = []
        for member in members:
            resource = resources[member['resource_id']]
            require(member['source_path'] == resource['work_path'] and member['sha256'] == resource['sha256'],
                    'Candidate and MD resource evidence disagree')
            require(str(member['size_bytes']) == resource['size_bytes'] and str(member['sequence']) == resource['sequence'],
                    'Resource size or order differs between manifests')
            checked_input(member['source_path'], member['size_bytes'], member['mtime_ns'])
            snapshots[member['source_path']] = list(stamp(member['source_path']))
            suffix = Path(member['source_path']).suffix.lower()
            require(suffix in {'.pdf', '.jpg'} and resource['state'] == 'readable', 'Unsupported or unreadable Master')
            stem = f"{obj['inventory']}--{int(member['sequence']):02d}--{member['resource_id']}"
            relative = 'masters/' + stem + suffix
            raw = raw_resources[member['resource_id']]
            row = file_row(relative_path=relative, file_role='master_member', md_object_id=oid,
                           inventory=obj['inventory'], selected_candidate_id=candidate['candidate_id'],
                           resource_id=member['resource_id'], sequence=member['sequence'], title=obj['title'],
                           description=obj['description'], date=' | '.join(dates), creator=raw.get('creator', ''),
                           object_url=obj['object_url'], rights=resource['rights'], owner=resource['owner'],
                           match_status='matched', match_basis=candidate['match_basis'], source_path=member['source_path'],
                           source_url=resource['source_url'], original_filename=unquote(Path(urlparse(resource['source_url']).path).name),
                           source_size_bytes=member['size_bytes'], source_mtime_ns=member['mtime_ns'],
                           source_sha256=member['sha256'], metadata_path='metadata/md-export.json',
                           pages=resource['pages'], width=resource['width'], height=resource['height'])
            files.append(row)
            ordered_paths.append(relative)
            derivative = dict(row)
            derivative.update(relative_path='export/' + stem + '-preview.jpg', file_role='access_preview',
                              description='First PDF page or complete image; JPEG, at most 1600 pixels on the long edge.',
                              source_path=str(TARGET / relative), source_url='', original_filename=Path(relative).name,
                              source_size_bytes='', source_mtime_ns='', source_sha256='',
                              master_relative_path=relative, pages='1', width='', height='')
            derivatives.append(derivative)
            sources.append(source_row(source_path=member['source_path'], source_kind='museum_digital_capture',
                                      inventory=obj['inventory'], source_role='selected_master_member', final_path=relative,
                                      source_sha256=member['sha256'], source_size_bytes=member['size_bytes'],
                                      preservation_basis='Selected complete MD representation; byte-preserving copy planned.'))
            tech_path = resource['technical_path']
            checked_input(tech_path)
            snapshots[tech_path] = list(stamp(tech_path))
            technical.append({'md_object_id': oid, 'resource_id': member['resource_id'],
                              'original_evidence_path': tech_path, 'technical': json.loads(Path(tech_path).read_text())})
        objects.append({key: str(value) for key, value in dict(md_object_id=oid, inventory=obj['inventory'],
                       selected_candidate_id=candidate['candidate_id'], title=obj['title'], object_url=obj['object_url'],
                       ordered_master_paths=json.dumps(ordered_paths, ensure_ascii=False),
                       metadata_path='metadata/md-export.json', actor=decision['actor'],
                       decided_at=decision['decided_at'], note=decision['note']).items()})
    masters = list(files)
    for candidate in review.candidates:
        if candidate['origin'] != 'protected_usb':
            continue
        selected = [r for r in masters if r['md_object_id'] == candidate['md_object_id']]
        for member in json.loads(candidate['members_json']):
            checked_input(member['source_path'], member['size_bytes'], member['mtime_ns'])
            snapshots[member['source_path']] = list(stamp(member['source_path']))
            exact = [r for r in selected if r['source_sha256'] == member['sha256']]
            require(len(exact) <= 1, 'Ambiguous exact-source mapping')
            related = exact or [r for r in selected if r['relative_path'].endswith('.pdf')]
            require(len(related) == 1, 'Local alternative needs an explicit final member mapping')
            sources.append(source_row(source_path=member['source_path'], source_kind='protected_usb',
                                      inventory=candidate['inventory'], source_role='redundant_candidate',
                                      final_path=related[0]['relative_path'], source_sha256=member['sha256'],
                                      source_size_bytes=member['size_bytes'],
                                      preservation_basis=('Stored hashes match selected MD member; verify against final output.'
                                                          if exact else 'Different source bytes; reviewed object match only. No exact-copy claim.'),
                                      notes='Proposed destination only. ' + candidate['match_basis'] + ' ' + candidate['quality_note']))
    documents = read_csv('documentation-review.csv')
    require(Counter(r['decision'] for r in documents) == {'included': 7, 'excluded': 10}, 'Document scope changed')
    for document in documents:
        document['proposed_relative_path'] = ''
        if document['decision'] != 'included':
            continue
        path = Path(document['source_path'])
        checked_input(path, document['size_bytes'], document['mtime_ns'])
        snapshots[str(path)] = list(stamp(path))
        require(document['condition'] == 'readable', 'Included document is not readable')
        relative = 'documents/' + path.name
        document['proposed_relative_path'] = relative
        files.append(file_row(relative_path=relative, file_role='documentation', title=path.name,
                              description=document['description'], date=document['document_date'],
                              match_status='not_searched', match_basis='Included as unit documentation by saved human scope decision; not assigned to an MD object.',
                              source_path=str(path), original_filename=path.name,
                              source_size_bytes=document['size_bytes'], source_mtime_ns=document['mtime_ns'],
                              source_sha256=document['content_sha256'], metadata_path='00_MANIFESTS/documentation-review.csv'))
        sources.append(source_row(source_path=str(path), source_kind='protected_usb', source_role='included_documentation',
                                  final_path=relative, source_sha256=document['content_sha256'], source_size_bytes=document['size_bytes'],
                                  preservation_basis='One unchanged original selected by saved documentation decision.',
                                  drain_state='retain_mixed_project_source' if document['relationship'] == 'mixed' else 'retain_source_no_deletion_authority',
                                  notes='Proposed destination only. Relationship: ' + document['relationship'] + '. ' + document['description']))
    evidence = dict(EVIDENCE)
    if (ROOT / 'reconciliation-decisions.jsonl').exists():
        evidence['reconciliation-decisions.jsonl'] = '00_MANIFESTS/reconciliation-decisions.jsonl'
        evidence['reconciliation-input.json'] = 'metadata/reconciliation-input.json'
    for name, relative in evidence.items():
        size, mtime = checked_input(ROOT / name)
        snapshots[str(ROOT / name)] = [size, mtime]
        files.append(file_row(relative_path=relative, file_role='reconciliation_evidence', title=name,
                              description='Captured evidence or saved reconciliation decisions; historical observations retain their original context.',
                              source_path=str(ROOT / name), original_filename=name,
                              source_size_bytes=size, source_mtime_ns=mtime))
    generated = {'metadata/resource-technical.jsonl': ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in technical)}
    files.append(file_row(relative_path='metadata/resource-technical.jsonl', file_role='reconciliation_evidence',
                          title='Recorded MD resource technical inspection',
                          description='109 original technical records, with their original evidence paths, collected without reinspection or altered values.'))
    paths = [r['relative_path'] for r in files + derivatives] + CONTROLS
    require(len(paths) == len(set(paths)) and len(paths) == len({p.casefold() for p in paths}), 'Destination filename collision')
    for relative in paths:
        if relative not in {'00_README.md', 'CATALOG.csv', 'CATALOG.xlsx'}:
            checked_relative(relative)
    require(len(objects) == 68 and len(masters) == 109 and len(derivatives) == 109, 'Unexpected Master scope')
    require(Counter(len(json.loads(row['ordered_master_paths'])) == 1 for row in objects) == {True: 40, False: 28},
            'Single-file and ordered-set Master counts differ')
    require(len(sources) == len({r['source_path'] for r in sources}) == 122, 'Source register scope differs')
    excluded = {r['source_path'] for r in documents if r['decision'] == 'excluded'}
    require(not excluded.intersection(r['source_path'] for r in sources), 'Broader documents entered unit source register')
    require(sum(int(r['pages']) for r in masters if r['relative_path'].endswith('.pdf')) == 638, 'PDF extent differs')
    require(all(shutil.which(tool) for tool in ['pdfinfo', 'pdftoppm']), 'Existing PDF dependency is missing')
    for row in files + derivatives + objects + sources:
        require(all(len(str(value)) <= 32767 and not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', str(value))
                    for value in row.values()), 'A catalogue cell cannot be represented losslessly in XLSX')
    payload_bytes = sum(int(r['source_size_bytes'] or 0) for r in files)
    require(shutil.disk_usage(TARGET.parent).free > max(2_000_000_000, payload_bytes * 3), 'Insufficient archive space')
    summary = dict(master_objects=68, single_file_masters=40, ordered_set_masters=28, master_files=109,
                   master_formats=dict(Counter(Path(r['relative_path']).suffix for r in masters)), pdf_pages=638,
                   documentation_files=7, excluded_document_groups=8, excluded_document_paths=10,
                   evidence_files=sum(r['file_role'] == 'reconciliation_evidence' for r in files),
                   planned_previews=109, source_disposition_rows=122, protected_usb_source_rows=13,
                   planned_catalogue_rows=len(files) + len(derivatives), copied_bytes=payload_bytes)
    require(checked_legacy_handoff(ROOT, 'collection-591') == consolidated_revision,
            'Consolidated decisions changed during accession planning')
    return dict(unit='aeb', collection='591', target=str(TARGET), structure=list(DIRS), summary=summary,
                objects=objects, files=files, derivatives=derivatives, documents=documents,
                source_disposition=sources, generated_payloads=generated, controls=CONTROLS,
                input_stamps=snapshots, protected_mount={k: mount[k] for k in ['source', 'target', 'fstype']},
                accession_authorized=False, accession_performed=False, deletion_authorized=False)


def write_preparation(plan):
    tables = {'accession-master-representations.csv': plan['objects'],
              'accession-files.csv': plan['files'], 'accession-derivatives.csv': plan['derivatives'],
              'accession-document-review.csv': plan['documents'], 'accession-source-disposition.csv': plan['source_disposition']}
    for name, rows in tables.items():
        atomic(ROOT / name, csv_bytes(rows))
    atomic(PLAN, json_bytes(plan))
    summary = plan['summary']
    documents = '\n'.join('- `' + Path(r['source_path']).name + '` — ' + r['relationship']
                          for r in plan['documents'] if r['decision'] == 'included')
    text = f'''# AEB accession preparation

Proposed destination: `{TARGET}`. No archive unit, database or derivative has been created by preparation.

## Approved content

68 selected MD Master representations: 40 single files and 28 ordered sets. Their 109 members are 67 PDFs (638 pages) and 42 JPEGs. `accession-master-representations.csv` fixes the selected member order. A multi-file set remains one Master, without joining, dropping or promoting its members.

Seven original supporting documents are included:

{documents}

Eight wider-scope groups (10 source paths) are excluded from this unit. Their decisions and original relationships remain in the documentation manifest and evidence. They are absent from the AEB source-disposition register.

## Proposed accession output

Use only `00_README.md`, `CATALOG.csv`, `CATALOG.xlsx` and the six approved folders: {', '.join('`'+d+'/`' for d in DIRS)}. No deeper directories.

Preserve Master/document bytes. MD identity, descriptions, event dates, resource creator/rights and full original metadata accompany them in the catalogues and `metadata/`. Preparation adds no speculative values or metadata authority. After approved accession the local unit is authoritative; MD is a projection. There is no MD update or publication in this operation.

Generate 109 bounded JPEG previews from the final selected Master members during accession. PDF previews show the first page only; complete PDFs remain available as Masters. No preview is made into a Master. Original JPEG resources remain original Masters; their smaller previews are separate derived files.

The payload catalogue will contain {summary['planned_catalogue_rows']} rows: 109 Master members, seven documents, {summary['evidence_files']} evidence files and 109 previews. CSV and XLSX will contain identical literal cell values. A small three-table SQLite catalogue (`objects`, `files`, `source_disposition`) will be rebuildable from the readable records. Generated catalogues, manifests, README and receipts are controls outside the payload catalogue.

`accession-source-disposition.csv` has 122 proposed paths: 109 MD work captures, six redundant USB alternatives and seven accepted documentation sources. Three local alternatives have stored hashes equal to the selected MD file; the other three have reviewed content matches only. All paths are retained, with a specific retention rule for the mixed-project DIGIS presentation. Proposed paths and stored source hashes do not establish final preservation. All final hashes are blank and all deletion flags remain false.

Hash each finalized payload exactly once after copying or derivative creation, and record it in `00_MANIFESTS/payload-checksums.csv`, CSV/XLSX and SQLite. Verify copied originals against their existing hashes; evidence without a stored hash gets a byte comparison. Do not hash controls into themselves or introduce another checksum framework.

## Search limits and verification

The captured search screened 916 indexed source paths. Ten broad title queries and the general Wissenschaftsorganisation query were capped; unindexed USB files were not exhaustively scanned. Wider local context and three rejected visual leads remain evidence, outside the unit's drain scope. The original workbench's reports are historical evidence; saved decisions and the eventual accession receipt determine current state.

Preparation checks the exact protected mount, completed decisions, complete resource order, unchanged input size/mtime, filename collisions, allowed layout, source-register exclusions, dependencies and free space. It reuses stored source hashes. No payload is copied into `iss-archive` during preparation.

Run `python3 -B {ROOT}/prepare_accession_591.py --check` to compare this preparation to live decisions and inputs. Accession requires explicit authorization for `{TARGET}`, its six standard folders, the catalogues, SQLite and accession previews. It does not authorize deletion, source changes, publication or sealing.
'''
    atomic(ROOT / 'ACCESSION_PREPARATION.md', text.encode())


def check_saved(plan):
    require(json.loads(PLAN.read_text()) == plan, 'Saved preparation differs from live input; prepare again before accession')
    tables = {'accession-master-representations.csv': 'objects', 'accession-files.csv': 'files',
              'accession-derivatives.csv': 'derivatives', 'accession-document-review.csv': 'documents',
              'accession-source-disposition.csv': 'source_disposition'}
    for name, key in tables.items():
        require((ROOT / name).read_bytes() == csv_bytes(plan[key]), f'CSV and plan disagree: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    plan = build_plan()
    if args.check:
        check_saved(plan)
    else:
        write_preparation(plan)
        check_saved(build_plan())
    print(json.dumps({'result': 'ok', 'target': plan['target'], **plan['summary'],
                      'preparation_complete': True, 'accession_authorized': False,
                      'archive_modified': False, 'derivatives_generated': 0, 'deletion_authorized': False}, indent=2))


if __name__ == '__main__':
    main()
