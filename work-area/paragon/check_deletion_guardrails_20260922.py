"""Read-only Paragon deletion preflight; writes only its JSON/Markdown reports.

No execution mode, remount, deletion, decision save, or archive write exists.
Stored media hashes are reused only with matching recorded size/mtime evidence.
"""
import collections
import csv
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import paragon_workbench as app
import retained_candidates as retained

W, U, S = retained.ROOT, retained.ARCHIVE, retained.SOURCE
OUT = W / 'deletion-preflight-20260922.json'
DOC = W / 'DELETION_PREFLIGHT_20260922.md'


def read_json(p):
    return json.loads(p.read_text())


def csv_rows(p):
    with p.open(newline='') as f:
        return list(csv.DictReader(f))


def digest(p):
    with p.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()


def stamp(p):
    st = p.stat()
    return [st.st_size, st.st_mtime_ns]


def split_field(value):
    return re.split(r' \| |\n', value) if value else []


def main():
    mount_text = app.mount_guard()
    mounts = json.loads(subprocess.check_output([
        'findmnt', '--json', '-R', '/media/vladimir/Elements', '-o',
        'SOURCE,TARGET,FSTYPE,OPTIONS,UUID,MAJ:MIN,FSROOT'], text=True))['filesystems']
    mount = mounts[0]
    errors = []
    if len(mounts) != 1 or mount.get('children'):
        errors.append('Unexpected nested source mounts')
    if mount['uuid'] != '6CAAB316AAB2DC30':
        errors.append('Source filesystem UUID changed')
    source_device = os.makedev(*map(int, mount['maj:min'].split(':')))
    closure = read_json(W / 'retained-closure-20260922.json')
    pending = read_json(retained.PENDING_ACCESSION)
    register = {x['source_path']: x for x in csv_rows(W / 'source-disposition.planned.csv')}
    drain = csv_rows(W / 'drain-list.csv')
    paths = {x['source_path'] for x in drain}
    excluded = {x['source_path'] for x in pending['excluded_files']}
    owner_discard = set(closure['pending_source_paths'])
    evidence = {x['source_path']: x for x in read_json(W / 'drain-evidence.json')}
    checks = {x['path']: x for x in csv_rows(U / 'db-restore/checksums.csv')}
    catalogue = csv_rows(U / 'CATALOG.csv')
    if len(drain) != 1375 or len(paths) != 1375:
        errors.append('Drain count or unique path count is not 1375')
    if len(register) != 1419 or len(excluded) != 44 or paths & excluded:
        errors.append('Source partition or 44 other-unit exclusions changed')
    if paths | excluded != set(register) or pending['files']:
        errors.append('Source partition is incomplete or pending review has returned')
    if paths != {p for p, x in register.items() if x['drain_state'] == 'candidate_after_separate_review'}:
        errors.append('Drain list differs from the current source register')
    if any(x['deletion_authorized'] != 'false' for x in register.values()):
        errors.append('A source unexpectedly has deletion authorization')

    mirrors = {}
    for final, work in [('source-disposition.csv', 'source-disposition.planned.csv'),
                        ('drain-list.csv', 'drain-list.csv'), ('drain-evidence.json', 'drain-evidence.json'),
                        ('accession-remaining-20260921.json', 'accession-remaining-20260921.json'),
                        ('accession-remaining-20260921.csv', 'accession-remaining-20260921.csv'),
                        ('ACCESSION_REMAINING_20260921.md', 'ACCESSION_REMAINING_20260921.md'),
                        ('retained-closure-20260922.json', 'retained-closure-20260922.json')]:
        mirrors[final] = (U / 'db-restore/manifests' / final).read_bytes() == (W / work).read_bytes()
    if not all(mirrors.values()):
        errors.append('Work/archive control copies differ')
    control_names = ['CATALOG.csv', 'metadata/objects.csv', 'db-restore/RESTORE.sqlite',
                     'db-restore/RESTORE.sql', 'db-restore/export-summary.json',
                     'db-restore/manifests/drain-list.csv', 'db-restore/manifests/source-disposition.csv',
                     'db-restore/manifests/drain-evidence.json',
                     'db-restore/manifests/accession-remaining-20260921.json',
                     'db-restore/manifests/retained-closure-20260922.json']
    inputs = {str(W / n): digest(W / n) for n in ['drain-list.csv', 'source-disposition.planned.csv',
              'drain-evidence.json', 'retained-decisions.json', 'retained-proposed-drain.json',
              'accession-remaining-20260921.json', 'retained-closure-20260922.json', 'work/catalog.sqlite']}
    for name in control_names:
        actual = digest(U / name)
        inputs[str(U / name)] = actual
        if name not in checks or checks[name]['sha256'] != actual:
            errors.append('Control checksum mismatch: ' + name)

    parent_cache = {}
    inodes = collections.defaultdict(list)
    folded = collections.defaultdict(list)
    hardlinks = []
    source_records = []
    for path in sorted(paths | excluded):
        p = Path(path)
        problems = []
        if not p.is_absolute() or not p.is_relative_to(S) or os.path.normpath(path) != path:
            problems.append('Path outside exact approved source root or not canonical')
        if any(ord(c) < 32 for c in path):
            problems.append('Control character in source path')
        try:
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode) or p.resolve(strict=True) != p:
                problems.append('Not a regular non-symlink source file')
            for parent in p.parents:
                if parent not in parent_cache:
                    parent_cache[parent] = stat.S_ISLNK(parent.lstat().st_mode)
                if parent_cache[parent]:
                    problems.append('Symlink in ancestor path')
            if st.st_dev != source_device:
                problems.append('Source is on another device')
            expected = evidence[path]
            if [st.st_size, st.st_mtime_ns] != [expected['source_size_bytes'], expected['source_mtime_ns']]:
                problems.append('Source size/mtime differs from saved hash evidence')
            if int(register[path]['source_size_bytes']) != st.st_size or register[path]['source_sha256'] != expected['source_sha256']:
                problems.append('Source register and evidence disagree')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected['source_sha256']):
                problems.append('Missing or invalid saved source SHA-256')
            inodes[(st.st_dev, st.st_ino)].append(path)
            folded[unicodedata.normalize('NFC', path).casefold()].append(path)
            if st.st_nlink != 1:
                hardlinks.append({'path': path, 'link_count': st.st_nlink})
            item = dict(source_path=path, included_in_deletion_scope=path in paths,
                        deletion_authorized=False, size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                        st_dev=st.st_dev, st_ino=st.st_ino, link_count=st.st_nlink,
                        stored_sha256=expected['source_sha256'], identity_issues=problems)
            source_records.append(item)
        except (OSError, KeyError, ValueError) as exc:
            problems.append(str(exc))
        errors.extend(path + ': ' + issue for issue in problems)
    inode_aliases = [v for v in inodes.values() if len(v) > 1]
    case_aliases = [v for v in folded.values() if len(v) > 1]
    if inode_aliases or case_aliases:
        errors.append('Duplicate inode or case-normalized source identity')

    # Verify archive files against existing finalized hashes and receipt stamps.
    archive_targets = {}
    def archive_reference(path, expected_hash=None):
        p = Path(path)
        try:
            rel = str(p.relative_to(U))
            if p.resolve(strict=True) != p or not stat.S_ISREG(p.lstat().st_mode):
                raise ValueError('Unsafe archive target')
            checked = checks[rel]
            if expected_hash and expected_hash.removeprefix('sha256:') != checked['sha256'].removeprefix('sha256:'):
                raise ValueError('Stored final hashes disagree')
            if p.stat().st_size != int(checked['size_bytes']):
                raise ValueError('Archive target size changed')
            previous = closure['original_archive_files'][rel]
            if stamp(p) != previous:
                raise ValueError('Archive payload stamp changed since finalized record')
            archive_targets[path] = {'path': path, 'sha256': checked['sha256'], 'size_bytes': p.stat().st_size,
                                     'mtime_ns': p.stat().st_mtime_ns, 'live_payload_rehashed': False}
            return True
        except (OSError, ValueError, KeyError) as exc:
            errors.append('Archive reference ' + path + ': ' + str(exc))
            return False

    for row in catalogue:
        archive_reference(str(U / row['final_relative_path']), row['final_sha256'])
    direct = {}
    for path, row in register.items():
        finals, hashes = split_field(row['final_path']), split_field(row['final_sha256'])
        if len(finals) != len(hashes):
            errors.append('Final path/hash arity mismatch: ' + path)
        direct[path] = []
        for rel, h in zip(finals, hashes):
            q = str(U / rel)
            if archive_reference(q, h):
                direct[path].append(q)
        if row['exact_copy_proven'] == 'true' and row['source_sha256'] not in hashes:
            errors.append('Exact-copy assertion lacks matching stored final hash: ' + path)

    with app.connect() as c:
        saved = retained.saved_reviews(c)
        decision_agreement = retained.readable_decisions(c) == read_json(retained.DECISIONS)
        proposals = retained.drain_proposals(c)
        proposal_agreement = proposals == read_json(retained.DRAIN_PROPOSALS)
        all_proposals_registered = all(x['in_drain_register'] for x in proposals['files'])
    if not decision_agreement or not proposal_agreement or not all_proposals_registered:
        errors.append('SQLite/readable decisions or drain registration disagree')
    sqlite_checks = {}
    for db in [W / 'work/catalog.sqlite', U / 'db-restore/RESTORE.sqlite']:
        con = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
        sqlite_checks[str(db)] = {'quick_check': con.execute('pragma quick_check').fetchone()[0],
                                  'foreign_key_issues': len(con.execute('pragma foreign_key_check').fetchall())}
        con.close()
    if any(v['quick_check'] != 'ok' or v['foreign_key_issues'] for v in sqlite_checks.values()):
        errors.append('SQLite integrity or foreign-key check failed')

    files = {f['path']: f for d in saved.values() for f in d['files']}
    document_discards = {p for p, row in register.items()
                        if 'Intermediate caption export rejected' in row['preservation_basis']}
    # One missing per-file target was checked visually during this preflight.
    # Keep the observation bound to the exact saved files, without changing any
    # source decision or treating a filename/similarity hint as proof.
    visual_path = str(S / 'WF/Paragon fertig für md/Paragon_jpgs-fertig/VLV-1130-5.jpg')
    visual_master = str(U / 'masters/VLV-1130-5__ISS-2026-JS55-S__master.tif')
    visual_access = str(U / 'export/access/VLV-1130-5__ISS-2026-JS55-S__access.jpg')
    visual_proof = {
        'source_path': visual_path,
        'source_sha256': 'sha256:ceb7c17a98ea9cff865c64e0a183e7f61b8779d6fab7b3cb82aaa7dfdbd2cd3c',
        'master_path': visual_master,
        'master_sha256': 'sha256:61226aaa178de3dce49124453e374bc7e15db84a51b5625e9fa6bac636fbe743',
        'access_path': visual_access,
        'access_sha256': 'sha256:7e2c841f7926cd3e43a25b80436620894e910a993465f5ff00578cce5c21c011',
        'checked_on': '2026-09-22', 'checked_by': 'Codex',
        'finding': 'The 600x800 JPEG and archived access image show the same cut-out factory photograph, irregular cut edge, hand-drawn outline and creases. The archive image preserves all depicted content at greater detail; framing/blank margins differ. This is visual coverage, not exact byte identity.',
        'source_or_archive_changed': False}
    visual_valid = (register[visual_path]['source_sha256'] == visual_proof['source_sha256']
                    and archive_reference(visual_master, visual_proof['master_sha256'])
                    and archive_reference(visual_access, visual_proof['access_sha256']))
    if not visual_valid:
        errors.append('The exact files used for the bounded visual check changed')
    graph = collections.defaultdict(set)
    def add(path, value):
        if isinstance(value, str) and value.startswith('/'):
            graph[path].add(value)
        elif isinstance(value, dict):
            for key in ['path', 'target', 'master_path', 'target_master', 'retained_source_path']:
                add(path, value.get(key))
    for path, row in register.items():
        graph[path].update(direct[path])
        e, f = evidence[path], files.get(path, {})
        for proof in [f, e.get('saved_file_decision', {})]:
            add(path, proof.get('duplicate_evidence', {}))
            add(path, proof.get('derivative_evidence', {}).get('coverage_target', {}))
            add(path, proof.get('similarity_evidence', {}).get('comparison_target', {}))
        add(path, e.get('covering_source', {}))
        proof = e.get('drain_decision_evidence', {})
        add(path, proof.get('comparison_target', {}))
        add(path, proof.get('coverage_target', {}))
        add(path, proof)
        add(path, e.get('current_comparison_redirect', {}).get('current_surviving_comparison', {}))
        graph[path].discard(path)
    if visual_valid:
        graph[visual_path].update([visual_master, visual_access])
    for d in saved.values():
        pair = d.get('source_version_pair', {}) or d.get('prior_source_version_pair', {})
        if pair.get('source_path') and pair.get('target_path'):
            graph[pair['source_path']].add(pair['target_path'])
    by_hash = collections.defaultdict(list)
    for path, row in register.items():
        by_hash[row['source_sha256']].append(path)
    for same in by_hash.values():
        if len(same) > 1:
            for path in same:
                graph[path].update(q for q in same if q != path)
    trace_rows = []
    unknown_refs = set()
    for path in sorted(paths):
        todo, seen, survivors, rejected, kept = [path], set(), set(), set(), set()
        while todo:
            q = todo.pop()
            if q in seen:
                continue
            seen.add(q)
            # The owner's final discard supersedes earlier speculative comparisons.
            if q in owner_discard:
                rejected.add(q)
                continue
            if q.startswith(str(U) + '/'):
                if q in archive_targets or archive_reference(q):
                    survivors.add(q)
                continue
            if q in excluded:
                kept.add(q)
                continue
            if q in register:
                todo.extend(graph[q] - seen)
            else:
                unknown_refs.add(q)
        row = register[path]
        if direct[path]:
            category = 'direct_archive_binding'
        elif path in owner_discard:
            category = 'owner_directed_discard_19'
        elif path == visual_path and visual_valid:
            category = 'preflight_visual_comparison_to_archive'
        elif survivors:
            category = 'stored_comparison_chain_to_archive'
        elif kept:
            category = 'stored_comparison_chain_to_excluded_surviving_source'
        elif rejected:
            category = 'copy_chain_to_owner_directed_discard'
        elif row['preservation_status'] == 'reviewed_noise_not_accessioned' or row['source_role'] in {'noise', 'noise_drain_candidate'}:
            category = 'recorded_noise_or_unreadable_discard'
        elif 'Intermediate caption export rejected' in row['preservation_basis']:
            category = 'recorded_intermediate_document_discard'
        elif seen & document_discards:
            category = 'copy_chain_to_recorded_document_discard'
        else:
            category = 'no_current_surviving_target_in_traced_records'
        trace_rows.append({'source_path': path, 'category': category, 'direct_archive_targets': direct[path],
                           'stored_chain_archive_targets': sorted(survivors), 'retained_other_unit_targets': sorted(kept),
                           'owner_discard_targets': sorted(rejected), 'preservation_status': row['preservation_status'],
                           'document_discard_targets': sorted(seen & document_discards),
                           'decision_basis': row['preservation_basis'], 'deletion_authorized': False})
    trace_counts = dict(collections.Counter(x['category'] for x in trace_rows))
    unit_links = [link for p in paths for link in evidence[p].get('other_unit_links', [])]
    assigned_other_unit_links = [x for x in unit_links if x.get('inventory') or x.get('final_path') or x.get('preservation_status') != 'not_assessed']
    if assigned_other_unit_links:
        errors.append('Assigned other-unit preservation links need exact-path protection')
    unchanged_inputs = all(digest(Path(p)) == h for p, h in inputs.items())
    if not unchanged_inputs:
        errors.append('An input record changed during preflight')
    app.mount_guard()
    result = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Read-only preflight of the existing 1375 exact source-file drain list; no deletion executor.',
              'mechanical_guards_passed': not errors, 'errors': errors,
              'deletion_authorized': False, 'execution_enabled': False, 'files_deleted': 0,
              'mount': mount, 'mount_guard': mount_text, 'input_sha256': inputs,
              'drain_path_count': len(paths), 'total_bytes': sum(int(x['source_size_bytes']) for x in drain),
              'excluded_path_count': len(excluded), 'source_hash_method': 'Reuse saved SHA-256 only after live size/mtime match; no source media rehashed.',
              'source_identity_records': source_records, 'inode_aliases': inode_aliases,
              'case_normalized_aliases': case_aliases, 'hardlink_observations': hardlinks,
              'work_archive_mirrors': mirrors, 'sqlite_checks': sqlite_checks,
              'decisions_agree': decision_agreement, 'proposals_agree': proposal_agreement,
              'pending_drain_proposals': sum(not x['in_drain_register'] for x in proposals['files']),
              'catalogue_files_checked': len(catalogue), 'archive_target_records': list(archive_targets.values()),
              'coverage_trace_counts': trace_counts, 'coverage_traces': trace_rows,
              'bounded_visual_inspections': [visual_proof],
              'coverage_limit': 'Stored comparison links are evidence/provenance, not byte-identity or full-content guarantees. Owner-directed discards and recorded noise/document rejections remain intentional discard exceptions.',
              'unresolved_historical_reference_paths': sorted(unknown_refs),
              'unassessed_other_unit_search_links': len(unit_links) - len(assigned_other_unit_links),
              'assigned_other_unit_links': assigned_other_unit_links,
              'execution_blocks': [
                  'The user expressly prohibited deletion in this turn; all deletion_authorized flags remain false.',
                  'The exact source volume remains ntfs3 read-only. No writable mount is approved.',
                  'No current Paragon deletion executor has been validated. Legacy recovery retirement scripts use a different plan and are not approved for this list.'
              ],
              'future_execution_requirements': [
                  'Fresh explicit authorization must name this exact drain-list path/digest and accept the recorded intentional-discard exceptions.',
                  'Separately authorize the narrowly scoped source-write exception; preserve this device UUID and exact mountpoint, and restore read-only access afterward.',
                  'A bounded execution routine must parse the pinned CSV, reject all scope/count/digest changes, revalidate each source and surviving target, and stop on any mismatch.',
                  'Only unlink each authorized regular file by exact path; no recursive removal, wildcard expansion, directory deletion, symlink traversal, or other-unit paths.',
                  'Recheck the approved source fingerprints immediately before each deletion; rehash any changed file and stop rather than substituting a file.',
                  'Record actual outcomes for exact paths; leave failures and all unlisted paths untouched.'
              ]}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    lines = ['# Paragon deletion preflight — 22 September 2026', '',
             '**Read-only check completed. No files deleted. Deletion remains disabled.**', '',
             f'- Exact list: `{U / "db-restore/manifests/drain-list.csv"}`',
             f'- SHA-256: `{inputs[str(U / "db-restore/manifests/drain-list.csv")]}`',
             f'- Files: **{len(paths):,}**; bytes: **{result["total_bytes"]:,}** ({result["total_bytes"]/1e9:.2f} GB).',
             f'- Other-unit paths excluded: **{len(excluded)}**.',
             f'- Mechanical guards: **{"PASS" if not errors else "FAIL"}**.',
             f'- Source: `{mount_text}`; UUID `{mount["uuid"]}`.',
             '- No changed/missing source, symlink, device escape, duplicate path, or exclusion overlap is accepted.',
             '- Source/media hashes were reused with matching live size/mtime, as required by the archive rules; this was not a fresh full-byte read of every media file.',
             f'- {len(catalogue)} catalogue files and {len(archive_targets)} unique archive targets checked against saved hashes, sizes and finalized receipt stamps.',
             '- Work/archive control copies and SQLite/readable decisions checked; all deletion flags remain false.', '',
             '## Preservation evidence', '',
             'These categories describe stored evidence. Comparison chains do not prove byte identity or complete coverage. The prior owner decisions are preserved.', '',
             '| Evidence category | Source paths |', '| --- | ---: |']
    lines += [f'| {k.replace("_", " ")} | {v} |' for k, v in trace_counts.items()]
    lines += ['', 'Per-path evidence and source fingerprints are in `'+OUT.name+'`.', '',
              '## Execution remains blocked', ''] + ['- '+x for x in result['execution_blocks']]
    lines += ['', '## Requirements before any later execution', ''] + ['- '+x for x in result['future_execution_requirements']]
    if errors:
        lines += ['', '## Failed guards', ''] + ['- '+x for x in errors]
    DOC.write_text('\n'.join(lines)+'\n')
    print(json.dumps({k: result[k] for k in ['mechanical_guards_passed', 'drain_path_count',
          'total_bytes', 'excluded_path_count', 'coverage_trace_counts', 'unresolved_historical_reference_paths',
          'deletion_authorized', 'files_deleted']} | {'error_count': len(errors), 'first_errors': errors[:5]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
