"""Bounded evidence for the existing Paragon workbench's drain-candidate review."""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
ARCHIVE = Path('/mnt/volume/iss-archive/units/paragon')
SOURCE = Path('/media/vladimir/Elements/restore/archive')
REPORT = ROOT / 'drain-candidate-review.json'
DECISIONS = ROOT / 'drain-candidate-decisions.json'


def rows(path):
    with path.open(newline='') as f:
        return list(csv.DictReader(f))


def digest(value):
    return value.removeprefix('sha256:')


def image_info(path, sha):
    stat = path.stat()
    item = dict(source_path=str(path), stored_sha256='sha256:' + digest(sha),
                size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                exists=True, decoded=False,
                format='TIFF' if path.suffix.lower() in {'.tif', '.tiff'} else path.suffix.upper().lstrip('.'),
                width=None, height=None, frames=None)
    try:
        with Image.open(path) as image:
            item.update(format=image.format, width=image.width, height=image.height,
                        frames=getattr(image, 'n_frames', 1), mode=image.mode)
            image.load()
        item['decoded'] = True
    except (OSError, ValueError) as error:
        item['decode_error'] = str(error)
    return item


REASONS = {
    'VLV-1130-5': 'VLV-1130 is the complete carrier; VLV-1130-1 through VLV-1130-6 are its separate photographs. The raw VLV-1130-5.tif shows the selected photograph together with another photograph and surrounding paper. These views cover different content, so choosing between them cannot establish redundancy. Compare the raw scan with the complete carrier and each accepted photograph. The existing Masters remain selected. Vladimir has confirmed that the full raw scan must be preserved; its archive placement is pending.',
    'VLV-1011-1': 'The alternative is 3432 × 4944; the current Master is 1722 × 2479. More pixels alone do not prove more detail. Inspect lettering at original pixels and compare the crop and paper edges.',
    'VLV-1051': 'The rejected scan shows an Obst und Gemüse form, while the assigned Master shows a Schlachtvieh form. Related objects VLV-1051-1 and VLV-1052 show red and green versions. A filename rejection does not establish that this black-printed version is preserved. Compare the related references; leave unresolved with a note if the object identity needs correction.',
    'VLV-1008': 'The 103 MB TIFF cannot be decoded by the current image reader. Its pixels and completeness cannot be compared. An unreadable file is not proof that it has no recoverable content. The readable JPEG alternatives are also shown beside the current Master.',
    'VLV-1095': 'The current Master is one ordered representation with two members. Both are shown together. Compare the alternative against both members; keeping the current Master preserves their order. If the alternative is an additional view, leave unresolved and describe it.',
    'VLV-1129': 'These are versions of the same carrier image. Choose which version should be the Master; the other version can be marked redundant. Both are 4444 × 3174 pixels, with different pixel values. Compare the detail, background and tonal treatment. The rule to preserve carriers and their supporting images does not require preserving every version of the same image.',
    'VLV-1127': 'The TIFF was selected during the recent review; this JPEG was rejected. Compare the image content and crop. The final TIFF has the same dimensions, but does not preserve this JPEG byte for byte.',
}


def prepare():
    # Called only explicitly; never overwrite an existing review or saved decisions.
    import paragon_workbench as workbench
    workbench.mount_guard()
    if REPORT.exists() or DECISIONS.exists():
        raise ValueError('The drain-candidate review already exists; inspect it instead of rebuilding.')
    drain = rows(ROOT / 'drain-list.csv')
    evidence = {r['source_path']: r for r in json.loads((ROOT / 'drain-evidence.json').read_text())}
    final = rows(ARCHIVE / 'CATALOG.csv')
    catalogue = {r['archival_label']: r for r in rows(ARCHIVE / 'metadata/objects.csv')}
    alternatives = [r for r in drain if r['preservation_status'] == 'reviewed_not_selected']
    copies = {}
    for r in alternatives:
        source = Path(r['source_path'])
        stamp = source.stat()
        e = evidence[str(source)]
        if (stamp.st_size, stamp.st_mtime_ns) != (e['source_size_bytes'], e['source_mtime_ns']):
            raise ValueError(f'Source evidence changed: {source}')
        sha = digest(r['source_sha256'])
        p = Path('/mnt/volume/archive') / source.relative_to(SOURCE)
        if sha in copies or not p.is_file() or p.stat().st_size != stamp.st_size:
            continue
        with p.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() == sha:
                st = p.stat()
                copies[sha] = dict(source_path=str(p), sha256=sha,
                                   size_bytes=st.st_size, mtime_ns=st.st_mtime_ns)
    remaining = [r for r in alternatives if digest(r['source_sha256']) not in copies]
    if len(remaining) != 18 or len({r['source_sha256'] for r in remaining}) != 18:
        raise ValueError('The expected eighteen versions changed; inspect the evidence first.')
    groups = defaultdict(list)
    for r in remaining:
        if ' | ' in r['inventory']:
            raise ValueError('An ambiguous object binding requires explicit review setup.')
        groups[r['inventory']].append(r)
    order = ['VLV-1130-5', 'VLV-1011-1', 'VLV-1051', 'VLV-1008', 'VLV-1095', 'VLV-1129']
    objects = []
    for inv in sorted(groups, key=lambda v: (order.index(v) if v in order else len(order), v)):
        master_rows = [r for r in final if r['inventory'] == inv and r['file_role'] in
                       {'preservation_master', 'preservation_surrogate'}]
        master_rows.sort(key=lambda r: (int(r['sequence'] or 0), r['final_relative_path']))
        members = [image_info(ARCHIVE / r['final_relative_path'], r['final_sha256']) for r in master_rows]
        if not members or any(not m['decoded'] for m in members):
            raise ValueError(f'{inv}: current Master unavailable')
        current = dict(members[0], label='Current Master' + (' · ordered pair' if len(members) > 1 else ''),
                       is_current_master=True, members=members,
                       evidence_id=members[0]['source_path'], difference='Accepted archival representation.')
        candidates = [current]
        for r in groups[inv]:
            item = image_info(Path(r['source_path']), r['source_sha256'])
            item.update(evidence_id=r['source_path'], label=Path(r['source_path']).parent.name,
                        is_current_master=False, previous_disposition=r,
                        difference='Different source bytes; no identical copy confirmed in the final unit, retained Paragon list or checked older WF copy.')
            if item['decoded']:
                item['difference'] += f" Source: {item['width']} × {item['height']}. Current Master: " + ', '.join(f"{m['width']} × {m['height']}" for m in members) + '.'
            candidates.append(item)
        references = []
        ref_ids = (['VLV-1130'] + [f'VLV-1130-{i}' for i in range(1, 7)] if inv == 'VLV-1130-5' else
                   ['VLV-1050', 'VLV-1050-1', 'VLV-1051-1', 'VLV-1052'] if inv == 'VLV-1051' else [])
        for label in ref_ids:
            ref = catalogue[label]
            ref_path = ref['master_file'] if inv == 'VLV-1130-5' else ref['access_file']
            access = ARCHIVE / ref_path
            file_row = next(r for r in final if r['final_relative_path'] == ref_path)
            references.append(dict(ref, **image_info(access, file_row['final_sha256'])))
        objects.append(dict(inventory=inv, catalogue=catalogue[inv], candidates=candidates,
                            comparison_only=inv == 'VLV-1130-5',
                            references=references, why=REASONS.get(inv,
                            'This JPEG was marked redundant during Master selection, but its exact version has no confirmed surviving copy in the checked locations. Compare content, crop, colour and readable detail against the current Master.')))
    report = dict(source_report=REPORT.name, checked_at=datetime.now(timezone.utc).isoformat(),
                  source_root=str(SOURCE), archive_root=str(ARCHIVE),
                  source_list='drain-list.csv', alternative_files=18, objects=objects,
                  verified_older_copies=list(copies.values()),
                  coverage_note='59 of 77 alternative byte versions have verified older WF copies. This review covers the other 18. Other backup locations have not been exhaustively searched.',
                  accession_authorized=False, deletion_authorized=False)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return dict(objects=len(objects), alternatives=18, verified_older_versions=len(copies))


def load_objects(con, history):
    report = json.loads(REPORT.read_text())
    if report['source_root'] != str(SOURCE) or report['archive_root'] != str(ARCHIVE):
        raise ValueError('Unexpected drain-review roots')
    prior, saved = history(con)
    final = {r['final_relative_path']: r for r in rows(ARCHIVE / 'CATALOG.csv')}
    objects = []
    for raw in report['objects']:
        obj = dict(raw)
        entity = obj['catalogue']['iss_id']
        row = con.execute('SELECT status FROM archive_objects WHERE entity_id=?', (entity,)).fetchone()
        if not row or row[0] != 'active':
            raise ValueError('The reviewed object is no longer active')
        old = prior.get(entity, {})
        obj['candidates'] = []
        for c in raw['candidates']:
            item = dict(c)
            match = old.get(c['evidence_id']) or next((r for r in old.values()
                    if r.get('content_digest') == c['stored_sha256']), None)
            if match and not c['is_current_master']:
                item['evidence_id'] = match['evidence_id']
            item['prior'] = match
            obj['candidates'].append(item)
            for member in c.get('members', []):
                rel = str(Path(member['source_path']).relative_to(ARCHIVE))
                record = final.get(rel)
                if (not record or record['inventory'] != raw['inventory'] or
                        record['file_role'] not in {'preservation_master', 'preservation_surrogate'} or
                        digest(record['final_sha256']) != digest(member['stored_sha256'])):
                    raise ValueError('Current Master changed; refresh the review evidence')
        obj['prior_reviews'] = list(old.values())
        obj['decision'] = saved.get(raw['inventory'])
        obj['selected_path'] = (obj['decision']['selected_master_path'] if obj['decision']
                                else obj['candidates'][0]['source_path'])
        obj['note'] = obj['decision']['note'] if obj['decision'] else ''
        obj['status'] = ('Unresolved · note saved' if obj['decision'] and not obj['selected_path'] else
                         'Decision saved' if obj['decision'] else 'Needs review')
        obj['drain_review'] = True
        objects.append(obj)
    if len(objects) != 15 or sum(len(o['candidates']) - 1 for o in objects) != 18:
        raise ValueError('Unexpected drain-review scope')
    return objects


def verify_candidate(candidate):
    for member in candidate.get('members', [candidate]):
        path = Path(member['source_path'])
        root = ARCHIVE if path.is_relative_to(ARCHIVE) else SOURCE
        if not path.resolve().is_relative_to(root):
            raise ValueError('Review image is outside the approved roots')
        st = path.stat()
        if (st.st_size, st.st_mtime_ns) != (member['size_bytes'], member['mtime_ns']):
            raise ValueError('Review image changed; recheck evidence')
    return Path(candidate['source_path'])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', required=True)
    parser.parse_args()
    print(json.dumps(prepare(), indent=2))
