"""Bounded proposed decisions; only a readable work-area JSONL file is written.

The archive catalogue and accessioned files are read-only inputs. The existing
workbench SQLite file holds a rebuildable index of these readable decisions.
This file does not perform accession or apply metadata to any source or Master.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DECISIONS = ROOT / 'tiff-review-decisions.jsonl'
ACTIONS = {'confirm_existing', 'assign_source', 'assign_master', 'new_object', 'redundant', 'unresolved'}


def inputs():
    metadata = json.loads((ROOT / 'tiff-review-metadata.json').read_text())
    proposals = {r['source_path']: r for r in (json.loads(s) for s in
                 (ROOT / 'new-object-metadata-proposals.jsonl').read_text().splitlines())}
    return metadata, proposals


def load(path=DECISIONS):
    raw = path.read_bytes() if path.exists() else b''
    decisions = {}
    for line in raw.splitlines():
        row = json.loads(line)
        if row['source_path'] in decisions:
            raise ValueError('Duplicate TIFF review rows')
        decisions[row['source_path']] = row
    return decisions, review_revision(raw)


def review_revision(raw):
    # A browser must not approve a caption it has not seen after a research update.
    return hashlib.sha256(raw + (ROOT/'tiff-review-metadata.json').read_bytes() +
                          (ROOT/'new-object-metadata-proposals.jsonl').read_bytes()).hexdigest()


def index_rows(con, decisions):
    """Tiny rebuildable index inside the existing review database, never an authority."""
    with con:
        con.execute('CREATE TABLE IF NOT EXISTS tiff_review_decisions (source_path TEXT PRIMARY KEY, decision_json TEXT NOT NULL)')
        con.execute('DELETE FROM tiff_review_decisions')
        con.executemany('INSERT INTO tiff_review_decisions VALUES (?,?)',
                        [(key, json.dumps(row, ensure_ascii=False, sort_keys=True)) for key, row in sorted(decisions.items())])


def index_agrees(con, decisions):
    indexed = {r[0]: json.loads(r[1]) for r in con.execute('SELECT source_path,decision_json FROM tiff_review_decisions')}
    return indexed == decisions


def rebuild_index():
    decisions, _ = load()
    validate_set(decisions)
    con = sqlite3.connect(f'file:{ROOT / "review.sqlite"}?mode=rw', uri=True)
    try:
        index_rows(con, decisions)
    finally:
        con.close()


def verify_stamp(path, expected):
    st = Path(path).stat()
    if (st.st_size, st.st_mtime_ns) != (expected['size_bytes'], expected['mtime_ns']):
        raise ValueError('An image changed since the comparison; refresh the evidence before deciding')


def make_decision(payload, candidates, masters, metadata, proposals, existing, revision):
    required = {'source_path', 'action', 'metadata_key', 'master_key', 'note', 'previous_revision'}
    if not isinstance(payload, dict) or set(payload) != required or any(not isinstance(v, str) for v in payload.values()):
        raise ValueError('Invalid TIFF review fields')
    if payload['previous_revision'] != revision:
        raise ValueError('Review decisions changed in another tab. Reload before saving.')
    if payload['action'] not in ACTIONS or len(payload['note']) > 1000:
        raise ValueError('Choose a supported action and a note of at most 1000 characters')
    source = payload['source_path']
    candidate = next((r for r in candidates if r['source_path'] == source), None)
    if candidate is None:
        raise ValueError('Unknown source TIFF')
    action, key, master_key = payload['action'], payload['metadata_key'], payload['master_key']
    note = payload['note'].strip()
    if key and key not in metadata and key != source:
        raise ValueError('Unknown metadata choice')
    if key == source and source not in proposals:
        raise ValueError('No researched caption exists for this TIFF')
    if master_key and master_key not in masters:
        raise ValueError('Unknown current Master')
    target = ''
    selected = ''
    caption = {}
    redundant = []
    unresolved = []
    affected = []
    if action in {'confirm_existing', 'assign_source', 'assign_master'}:
        if key not in metadata:
            raise ValueError('Choose an existing catalogue identity for this action')
        target = key
        caption = metadata[key]
        if action == 'confirm_existing':
            if key != master_key or key not in masters:
                raise ValueError('Confirm requires the displayed Master to belong to the selected metadata record')
            selected = masters[key]['master_path']
            redundant = [source]
        elif action == 'assign_source':
            if not candidate.get('feature', {}).get('hashes'):
                raise ValueError('An unreadable TIFF cannot be selected as Master')
            selected = source
        else:
            if master_key not in masters:
                raise ValueError('Choose the existing archive image to receive this metadata')
            selected = masters[master_key]['master_path']
            unresolved.append(source)
            if master_key != key:
                affected.append(dict(record_key=master_key, current_master_path=selected,
                                     note='Its current image is proposed for another identity. Review this object before accession.'))
        former = masters.get(key, {}).get('master_path', '')
        if former and former != selected:
            unresolved.append(former)
            affected.append(dict(record_key=key, current_master_path=former,
                                 note='Former Master remains unresolved for image identity; it is not marked redundant.'))
    elif action == 'new_object':
        if not candidate.get('feature', {}).get('hashes'):
            raise ValueError('An unreadable TIFF cannot be selected as Master')
        if key not in ('', source):
            raise ValueError('For a new object choose its researched caption, or leave metadata unresolved. Existing captions remain context only.')
        selected, target = source, source  # Existing source path; no inventory number is issued.
        caption = proposals.get(source, {}) if key == source else {}
    elif action == 'redundant':
        redundant = [source]
    else:
        unresolved = [source]
    for path in [source, selected, *unresolved]:
        if not path:
            continue
        if path == source:
            verify_stamp(path, candidate['source_stamp'])
        else:
            m = next((m for m in masters.values() if m['master_path'] == path), None)
            if not m:
                raise ValueError('Only known source TIFFs and existing Masters may be selected')
            verify_stamp(path, m['master_stamp'])
    row = dict(source_path=source, source_relative_path=candidate['source_relative_path'],
               action=action, state='selected_master' if selected else ('redundant' if redundant else 'unresolved'),
               object_record_key=target, selected_master_path=selected, metadata=caption,
               metadata_assignment_confirmed=bool(caption),
               metadata_key=key, master_key=master_key, note=note,
               redundant_candidates=redundant, unresolved_candidates=unresolved,
               affected_existing_objects=affected, prior_alias_decisions=candidate.get('prior_alias_decisions', []),
               prior_census_decisions=candidate.get('prior_census_decisions', []),
               reviewed_at=datetime.now(timezone.utc).isoformat(), source_stamp=candidate['source_stamp'],
               applied_to_archive=False, accession_authorized=False, deletion_authorized=False)
    next_rows = {**existing, source: row}
    validate_set(next_rows)
    return row


def validate_set(decisions):
    objects, files, captions = {}, {}, {}
    redundant = set()
    for row in decisions.values():
        selected, target = row['selected_master_path'], row['object_record_key']
        if selected:
            if target in objects and objects[target] != selected:
                raise ValueError(f'{target} already has a different proposed Master. Resolve that decision first.')
            if selected in files and files[selected] != target:
                raise ValueError('That image is already proposed for another object. Resolve that decision first.')
            caption = json.dumps(row['metadata'], sort_keys=True)
            if target in captions and captions[target] != caption:
                raise ValueError('Conflicting metadata proposals for one object')
            objects[target], files[selected], captions[target] = selected, target, caption
        redundant.update(row['redundant_candidates'])
    if set(files).intersection(redundant):
        raise ValueError('A selected Master cannot also be rejected or redundant')


def save(payload, candidates, masters, path=DECISIONS):
    """Caller holds the existing workbench LOCK and checks the protected mount."""
    existing, revision = load(path)
    metadata, proposals = inputs()
    row = make_decision(payload, candidates, masters, metadata, proposals, existing, revision)
    existing[row['source_path']] = row
    raw = ''.join(json.dumps(existing[k], ensure_ascii=False)+'\n' for k in sorted(existing)).encode()
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return row, review_revision(raw)


def data():
    metadata, proposals = inputs()
    decisions, revision = load()
    validate_set(decisions)
    return dict(metadata_records=metadata, new_metadata=proposals, decisions=decisions, revision=revision)
