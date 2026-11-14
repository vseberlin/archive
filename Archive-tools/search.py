#!/usr/bin/env python3
"""Prepare a read-only folder-to-collection comparison for the archive workbench."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import csv
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import traceback

from archive_common import atomic_write, elements_guard, plain_file, require, work_root
from matching import (FIELDS, HASHES, META_PROFILE, MetadataIndex, VisualIndex, catalogue_metadata,
                      compare_metadata, comparison_buffer, csv_rows, extract_metadata,
                      metadata_question, parse_czkawka, stamp, visual_matches)


def progress(message):
    print(message, flush=True)


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode()


def read_jsonl(path):
    if not Path(path).exists():
        return []
    plain_file(Path(path))
    with Path(path).open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path, rows):
    atomic_write(path, b''.join(json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in rows))


def member(path, current, sha256=''):
    result = {'path': str(path), **current, 'pages': 1}
    if sha256:
        result['sha256'] = sha256.removeprefix('sha256:')
    return result


from catalogues import PHOTO_EXTENSIONS, EmptyIndex, read_unit, digest, dimensions, quality, compare_quality
from consolidation import Evidence, jsonl, active_comparison, assigned_md_id, failed_historical_lead
from catalogues import DISCOVERY_SCOPE, ersatz_jpeg, genuine_tiff
from pixel_coverage import BASIS as PIXEL_BASIS, verify_coverage
from czkawka_config import load_profile, validate_profile, pass_id
from sift_matching import load_profile as load_sift_profile, rank as sift_rank, runtime as sift_runtime, verify_matches


def validate_comparison_command(command):
    """Fail closed before launching either Czkawka operation; no pass-through flags."""
    require(len(command) >= 2 and command[1] in ('dup', 'image'), 'Only read-only comparisons are allowed')
    values = {'-d', '-r', '-x', '-m', '-D', '-g', '-c', '-s', '-z',
              '--geometric-invariance', '-p', '-T', '-t'}
    switches = {'-Q', '-H', '-L', '-N', '-W'}
    seen = {}
    offset = 2
    while offset < len(command):
        option = command[offset]
        require(option in values | switches, f'Unapproved comparison option: {option}')
        require(option == '-d' or option not in seen, f'Duplicate comparison option: {option}')
        if option in values:
            require(offset + 1 < len(command), f'Missing comparison value: {option}')
            value = command[offset + 1]
            require(value and not value.startswith('-'), f'Invalid comparison value: {option}')
            seen[option] = value
            offset += 2
        else:
            seen[option] = True
            offset += 1
    require(seen.get('-D') == 'NONE' and seen.get('-Q') is True,
            'Comparisons require deletion NONE and dry-run; no files may be moved or deleted')


def run_logged(command, log, env, label):
    validate_comparison_command(command)
    plain_file(log)
    require(not log.exists() or log.stat().st_nlink == 1, 'Comparison log must not be a hard link')
    with log.open('w') as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=env)
        try:
            while True:
                try:
                    result = process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    progress(f'{label} is still comparing; progress log: {log.name}')
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            raise
    require(result == 0, f'{label} failed; see {log}')


def run_czkawka(binary, sources, targets, root, signature, threads, profile=None):
    """Compare temporary discovery proxies, mapping every hit back to its actual file/Master."""
    profile = validate_profile(profile or {})
    signature_file = root / 'czkawka-signature.json'
    # Include settings here too, so direct callers cannot reuse a differently configured run.
    signature = hashlib.sha256(json_bytes({'input': signature, 'profile': profile})).hexdigest()
    if signature_file.exists():
        saved = json.loads(signature_file.read_text())
        if saved.get('signature') == signature:
            return saved['matches'], saved['coverage']
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    progress(f'Czkawka: {version.splitlines()[0]}; preparing temporary comparison buffers')
    coverage = {'version': version, 'source_proxies': 0, 'target_proxies': 0, 'source_errors': {}, 'target_errors': {},
                'proxy_methods': {}, 'discovery_only': True, 'profile': profile,
                'passes': profile['passes'], 'algorithms': [p['algorithm'] for p in profile['passes']]}
    matches = defaultdict(list)
    # Buffers are temporary, not accession derivatives. Existing catalogue-linked access files can be read directly.
    with tempfile.TemporaryDirectory(prefix='.comparison-', dir=root) as temporary:
        tmp = Path(temporary); (tmp / 'source').mkdir(); (tmp / 'reference').mkdir()
        source_map, target_map, evidence = {}, {}, []
        tasks = [('source', s, i) for i, s in enumerate(sources)] + [('target', t, i) for i, t in enumerate(targets)]
        def make(task):
            kind, item, number = task
            destination = tmp / ('source' if kind == 'source' else 'reference') / f'{number:06d}.jpg'
            path = Path(item['path'])
            try:
                require(stamp(path) == item['stamp'], f'File changed: {path}')
                if kind == 'target' and item.get('preview_path') and profile['preview_mode'] == 'catalogued':
                    proxy = Path(item['preview_path'])
                    require(stamp(proxy) == item['preview_stamp'], 'Catalogue-linked access preview changed')
                    image, _ = comparison_buffer(proxy, profile['proxy_edge'])
                    image.save(destination, 'JPEG', quality=90)
                    method = 'catalogued_access_derivative_of_selected_master'
                else:
                    image, method = comparison_buffer(path, profile['proxy_edge'],
                                                     prefer_thumbnail=profile['preview_mode'] == 'catalogued')
                    image.save(destination, 'JPEG', quality=90)
                require(stamp(path) == item['stamp'], f'File changed: {path}')
                return kind, item, str(destination), method, ''
            except Exception as error:
                return kind, item, str(destination), '', str(error)
        with ThreadPoolExecutor(max_workers=threads) as pool:
            for count, (kind, item, destination, method, error) in enumerate(pool.map(make, tasks), 1):
                if error:
                    coverage[kind + '_errors'][item['path']] = error
                else:
                    (source_map if kind == 'source' else target_map)[destination] = item['path'] if kind == 'source' else item['id']
                    coverage[kind + '_proxies'] += 1
                    coverage['proxy_methods'][method] = coverage['proxy_methods'].get(method, 0) + 1
                    evidence.append({'temporary_path': destination, 'file_path': item['path'], 'file_stamp': item['stamp'],
                                     'method': method, 'proxy_path': item.get('preview_path'), 'proxy_stamp': item.get('preview_stamp')})
                if count % 200 == 0:
                    progress(f'Czkawka buffers: {count}/{len(tasks)} inspected')
        write_jsonl(root / 'czkawka-proxy-evidence.jsonl', evidence)
        if source_map and target_map:
            env = {**os.environ, 'CZKAWKA_CACHE_PATH': str(root / 'czkawka-cache'), 'CZKAWKA_CONFIG_PATH': str(root / 'czkawka-cache')}
            for settings in profile['passes']:
                algorithm = settings['algorithm']
                progress(f'Czkawka {algorithm}: {len(source_map)} sources against {len(target_map)} selected Master proxies')
                name = f'czkawka-{algorithm}-{pass_id(settings)}'
                output = root / f'{name}.json'
                plain_file(output)
                command = [str(binary), 'image', '-d', str(tmp / 'source'), '-r', str(tmp / 'reference'),
                           '-x', 'jpg,jpeg', '-m', '1', '-D', 'NONE', '-Q', '-g', algorithm,
                           '-c', str(settings['hash_size']), '-s', str(settings['max_difference']),
                           '-z', settings['resize_filter'], '--geometric-invariance', settings['geometric_invariance'],
                           '-p', str(output), '-N', '-W', '-T', str(threads)]
                log = root / f'{name}.log'
                run_logged(command, log, env, f'Czkawka {algorithm}')
                require(output.exists(), f'Czkawka {algorithm} produced no output; see {log}')
                parsed = parse_czkawka(json.loads(output.read_text()), source_map, target_map, algorithm, settings)
                for path, pairs in parsed.items():
                    matches[path].extend({**p, 'proxy_edge': profile['proxy_edge'], 'preview_mode': profile['preview_mode']} for p in pairs)
                progress(f'Czkawka {algorithm}: {sum(map(len, parsed.values()))} candidate pairs')
    atomic_write(signature_file, json_bytes({'signature': signature, 'matches': matches, 'coverage': coverage}))
    return matches, coverage


def paths(value):
    return [Path(p).absolute() for p in (value if isinstance(value, (list, tuple)) else [value]) if p is not None]


def run_blake3(binary, sources, targets, evidence, root, threads, reused=()):
    """Exact original-file comparison, using bounded explicit file lists, never previews."""
    selected = [s for s in sources if not evidence.confirmed[s['path']]]
    sizes = {s['stamp']['size_bytes'] for s in selected}
    files = {s['path']: s for s in selected}
    archive_files = defaultdict(list)
    for target in targets:
        for item in [target, *target['aliases']]:
            if item['stamp']['size_bytes'] in sizes:
                files[item['path']] = item
                archive_files[item['path']].append(target)
    buckets = defaultdict(list)
    for item in files.values():
        buckets[item['stamp']['size_bytes']].append(item)
    buckets = [group for group in buckets.values() if len(group) > 1]
    files = {item['path']: item for group in buckets for item in group}
    coverage = {'algorithm': 'BLAKE3', 'executed': False, 'input_files': len(files),
                'equal_byte_groups': 0, 'confirmed_pairs': 0, 'cached_result': False,
                'scope': 'Unconfirmed sources, selected archive Masters and catalogued derivatives in matching size buckets'}
    if not files:
        return coverage
    observed = {path: item['stamp'] for path, item in files.items()}
    signature = hashlib.sha256(json_bytes({'files': observed, 'binary': str(binary),
        'binary_stamp': stamp(binary), 'algorithm': 'BLAKE3', 'profile': 'explicit-original-files-v1'})).hexdigest()
    cache = root / 'czkawka-blake3-signature.json'
    saved = {}
    for directory in [root, *reused]:
        previous = directory / cache.name if directory.is_dir() else None
        if previous and previous.is_file():
            record = json.loads(plain_file(previous).read_text())
            if record.get('signature') == signature:
                saved = record
                if directory != root:
                    evidence.input_stamps[str(previous)] = stamp(previous)
                break
    if saved.get('signature') == signature:
        groups = saved['groups']; coverage['cached_result'] = True
    else:
        groups = []
        # Keep each equal-size bucket together so batching cannot lose cross-batch equality.
        batches, batch, length = [], [], 0
        limit = min(1000000, os.sysconf('SC_ARG_MAX') // 2)
        for group in buckets:
            cost = sum(len(os.fsencode(item['path'])) + 32 for item in group)
            require(cost < limit, 'An exact-copy size bucket exceeds the command limit; narrow the inspected folders')
            if batch and length + cost > 64000:
                batches.append(batch); batch, length = [], 0
            batch.extend(group); length += cost
        if batch:
            batches.append(batch)
        env = {**os.environ, 'CZKAWKA_CACHE_PATH': str(root / 'czkawka-cache'), 'CZKAWKA_CONFIG_PATH': str(root / 'czkawka-cache')}
        for number, batch in enumerate(batches, 1):
            progress(f'Czkawka BLAKE3 batch {number}/{len(batches)}: {len(batch)} original files')
            output = plain_file(root / f'czkawka-blake3-{number}.json')
            command = [str(binary), 'dup', '-s', 'HASH', '-t', 'BLAKE3', '-m', '1',
                       '-D', 'NONE', '-Q', '-H', '-L', '-N', '-W', '-T', str(threads), '-p', str(output)]
            for item in batch:
                require(stamp(item['path']) == item['stamp'], 'Exact comparison input changed')
                command.extend(['-d', item['path']])
            run_logged(command, root / f'czkawka-blake3-{number}.log', env, 'Czkawka BLAKE3')
            result = json.loads(output.read_text())
            require(isinstance(result, dict), 'Unsupported Czkawka duplicate JSON schema')
            for size_groups in result.values():
                require(isinstance(size_groups, list), 'Invalid duplicate groups')
                groups.extend(size_groups)
    for path, original in observed.items():
        require(stamp(path) == original, 'File changed during BLAKE3 comparison')
    verified = set()
    def verify_archive(item):
        if item['path'] in verified:
            return
        if item.get('sha256'):
            with Path(item['path']).open('rb') as handle:
                require(hashlib.file_digest(handle, 'sha256').hexdigest() == item['sha256'],
                        f'Stored archive hash disagrees with bytes: {item["path"]}')
        require(stamp(item['path']) == item['stamp'], 'Archive evidence changed during exact verification')
        verified.add(item['path'])
    for group in groups:
        require(isinstance(group, list) and len(group) >= 2 and all(isinstance(r, dict) and r.get('path') in files for r in group),
                'Exact result contains an unexpected file')
        hashes = {digest(r.get('hash')) for r in group}
        require(len(hashes) == 1 and '' not in hashes and
                len({files[r['path']]['stamp']['size_bytes'] for r in group}) == 1 and
                all(r.get('size') == files[r['path']]['stamp']['size_bytes'] for r in group), 'Invalid BLAKE3 equality evidence')
        value = hashes.pop()
        source_paths = [r['path'] for r in group if r['path'] in evidence.sources]
        if not source_paths:
            continue
        basis = {'basis': 'czkawka_blake3_equal_original_bytes', 'blake3': value,
                 'evidence_file': str(cache), 'input_stamps': {r['path']: observed[r['path']] for r in group}}
        for path in source_paths[1:]:
            evidence.same.append((source_paths[0], path, basis))
        for row in group:
            for target in archive_files[row['path']]:
                verify_archive(files[row['path']]); verify_archive(target)
                for path in source_paths:
                    evidence.confirmed[path].append({'target_id': target['id'], 'engine': 'confirmed',
                        'adequacy_confirmed': True, **basis})
                    coverage['confirmed_pairs'] += 1
    atomic_write(cache, json_bytes({'signature': signature, 'algorithm': 'BLAKE3', 'groups': groups, 'input_stamps': observed}))
    coverage.update(executed=True, equal_byte_groups=len(groups))
    return coverage


def load_targets(unit, index, extensions=PHOTO_EXTENSIONS):
    return read_unit(unit, index, extensions)[0]


def previous_pair_decisions(files, sources, targets):
    evidence = Evidence(sources, targets)
    for path in files:
        evidence.import_workbench(path)
    evidence.groups()
    return {p: rows[-1] for p, rows in evidence.confirmed.items() if rows}, evidence.rejected


def merge_metadata(items):
    out = defaultdict(list)
    for item in items:
        for field, entries in item['metadata'].items():
            for value in entries:
                if value not in out[field]:
                    out[field].append(value)
    return dict(out)


def inventory(source_roots, index, extensions):
    by_path, excluded, excluded_paths = {}, Counter(), set()
    for source in source_roots:
        elements_guard(source)
        require(source.is_dir() and source.resolve() == source, 'Use canonical existing source directories')
        for path in sorted(source.rglob('*')):
            require(not path.is_symlink(), f'Source symlink needs an explicit adapter: {path}')
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                if path not in excluded_paths:
                    excluded[path.suffix.lower() or '(no extension)'] += 1
                    excluded_paths.add(path)
                continue
            if str(path) in by_path:
                by_path[str(path)]['source_roots'].append(str(source))
                continue
            current = stamp(path); feature = index.get(path, current)
            by_path[str(path)] = {'id': feature.get('asset_id') or 'source:' + hashlib.sha256(str(path).encode()).hexdigest()[:24],
                'path': str(path), 'stamp': current, 'feature': feature, 'source_roots': [str(source)],
                'label': source.name + '/' + str(path.relative_to(source)), 'sha256': ''}
    return list(by_path.values()), excluded


def metadata_for_sources(sources, root, reused, input_stamps):
    cache = {}
    for directory in [*reused, root]:
        path = directory / 'metadata-cache.jsonl' if directory.is_dir() else None
        if path and path.exists():
            if directory != root:
                input_stamps[str(path)] = stamp(path)
            for row in jsonl(path):
                cache[row['path']] = row
    pending = [s for s in sources if cache.get(s['path'], {}).get('stamp') != s['stamp'] or
               cache.get(s['path'], {}).get('profile') != META_PROFILE]
    for offset in range(0, len(pending), 50):
        batch = pending[offset:offset + 50]
        extracted = extract_metadata([s['path'] for s in batch])
        for s in batch:
            require(stamp(s['path']) == s['stamp'], 'Source changed during metadata extraction')
            cache[s['path']] = {'path': s['path'], 'stamp': s['stamp'], **extracted[s['path']]}
        write_jsonl(root / 'metadata-cache.jsonl', cache.values())
        progress(f'Metadata: {min(offset + 50, len(pending))}/{len(pending)} missing records extracted')
    write_jsonl(root / 'metadata-cache.jsonl', (cache[s['path']] for s in sources))
    for source in sources:
        cached = cache[source['path']]
        source['metadata'], source['metadata_errors'] = cached['metadata'], cached['errors']
        source['width'], source['height'] = dimensions(source['path'], source['feature'])
        source['eligible'] = source['feature']['status'] != 'error' and bool(source['width'] and source['height'])
    return len(pending)


def file_hashes(sources, targets, units, evidence, root, inventories, reused, calculate):
    """Use stamped digests; hash only potential exact copies when explicitly enabled."""
    cached = {}
    for directory in [*reused, root]:
        path = directory / 'file-hashes.jsonl' if directory.is_dir() else None
        if path and path.exists():
            if directory != root:
                evidence.input_stamps[str(path)] = stamp(path)
            for row in jsonl(path):
                cached[(row['path'], row['size_bytes'], row['mtime_ns'])] = digest(row['sha256'])
    for path in inventories:
        evidence.input_stamps[str(path)] = stamp(path)
        for row in csv_rows(path):
            if row.get('mtime_ns') and row.get('size_bytes'):
                cached[(row.get('source_path', row.get('path')), int(row['size_bytes']), int(row['mtime_ns']))] = digest(row.get('sha256'))
    by_digest, sizes = defaultdict(list), set()
    source_sizes = {s['stamp']['size_bytes'] for s in sources}
    new_archive_hashes = 0
    computed_archive = set()
    # Selected-path catalogues may have no recorded hashes. Compute only compatible
    # byte-size candidates on explicit request, and keep these digests in the work area.
    for target in targets:
        for file in [target, *target['aliases']]:
            key = (file['path'], file['stamp']['size_bytes'], file['stamp']['mtime_ns'])
            if not file['sha256']:
                file['sha256'] = cached.get(key, '')
            if not file['sha256'] and calculate and file['stamp']['size_bytes'] in source_sizes:
                require(stamp(file['path']) == file['stamp'], 'Archive file changed before hashing')
                with Path(file['path']).open('rb') as handle:
                    file['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
                require(stamp(file['path']) == file['stamp'], 'Archive file changed while hashing')
                new_archive_hashes += 1
                computed_archive.add(file['path'])
    for t in targets:
        for file, kind in [(t, 'exact_master_bytes'), *[(a, 'catalogued_access_derivative') for a in t['aliases']]]:
            if file['sha256']:
                sizes.add(file['stamp']['size_bytes'])
                by_digest[file['sha256']].append((t, file, kind))
        if t['original_sha256'] and t['original_pixels_unchanged']:
            by_digest[t['original_sha256']].append((t, t, 'catalogued_unchanged_source_bytes'))
            if t['original_stamp']:
                sizes.add(t['original_stamp']['size_bytes'])
    # Source-disposition paths are reused only with a source digest and a verified current retained Master.
    for unit in units:
        for path in unit['disposition_files']:
            for row in csv_rows(path):
                source = evidence.sources.get(row.get('source_path'))
                if not source or not row.get('source_sha256'):
                    continue
                final = row.get('final_path') or row.get('final_archive_path')
                if not final:
                    continue
                target_path = Path(final) if Path(final).is_absolute() else Path(unit['unit']) / final
                target = evidence.targets.get(str(target_path))
                status = row.get('preservation_status', '')
                if target and target['sha256'] and status in ('redundant', 'preserved_exact_copy', 'accessioned'):
                    # A disposition for another object cannot be inherited solely through an inventory/name.
                    if digest(row.get('final_sha256')) != target['sha256']:
                        continue
                    evidence.leads[source['path']].append({'target_id': target['id'], 'engine': 'prior_provenance',
                        'basis': 'source_disposition_requires_current_digest', 'evidence_file': path})
                    source_digest = digest(row['source_sha256'])
                    approved = (source_digest == target['sha256'] or
                        target['original_pixels_unchanged'] and source_digest == target['original_sha256'] or
                        row.get('coverage_confirmed') == 'true' or
                        any(word in row.get('preservation_basis','') for word in ('human_confirmed_same_photo','user_confirmed_same_photo','verified_identical_decoded_pixels')))
                    if approved:
                        by_digest[source_digest].append((target, target, 'verified_source_disposition'))
                        sizes.add(source['stamp']['size_bytes'])
    duplicates = Counter(s['stamp']['size_bytes'] for s in sources)
    verified, computed = set(), 0
    def verify(item):
        if not item['sha256']:
            # A catalogued derivative can be verified even when its retained Master
            # has no historical digest. Observe that Master's digest read-only now.
            with Path(item['path']).open('rb') as handle:
                item['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
        key = (item['path'], item['sha256'])
        if key not in verified:
            require(stamp(item['path']) == item['stamp'], 'Hash evidence changed')
            with Path(item['path']).open('rb') as handle:
                require(hashlib.file_digest(handle, 'sha256').hexdigest() == item['sha256'],
                        f'Stored archive hash disagrees with bytes: {item["path"]}')
            require(stamp(item['path']) == item['stamp'], 'File changed while hashing')
            verified.add(key)
    hashed = []
    for source in sources:
        key = (source['path'], source['stamp']['size_bytes'], source['stamp']['mtime_ns'])
        value = cached.get(key, '')
        if not value and calculate and (source['stamp']['size_bytes'] in sizes or duplicates[source['stamp']['size_bytes']] > 1):
            require(stamp(source['path']) == source['stamp'], 'Source changed before hashing')
            with Path(source['path']).open('rb') as handle:
                value = hashlib.file_digest(handle, 'sha256').hexdigest()
            require(stamp(source['path']) == source['stamp'], 'Source changed while hashing')
            computed += 1
            if computed % 25 == 0:
                progress(f'Exact-copy candidates hashed: {computed}')
        source['sha256'] = value
        if not value:
            continue
        hashed.append({'path': source['path'], **source['stamp'], 'sha256': value})
        for target, file, basis in by_digest.get(value, []):
            verify(target)
            if file['path'] != target['path']:
                verify(file)
            evidence.confirmed[source['path']].append({'target_id': target['id'], 'engine': 'confirmed',
                'basis': basis, 'source_sha256': value, 'adequacy_confirmed': True})
    archive_hashes = [{'path': f['path'], **f['stamp'], 'sha256': f['sha256']}
                      for t in targets for f in [t, *t['aliases']] if f['sha256'] and
                      ((f['path'], f['sha256']) in verified or f['path'] in computed_archive)]
    write_jsonl(root / 'file-hashes.jsonl', [*hashed, *archive_hashes])
    identical = defaultdict(list)
    for s in sources:
        if s['sha256']:
            identical[s['sha256']].append(s['path'])
    for value, group in identical.items():
        for path in group[1:]:
            evidence.same.append((group[0], path, {'basis': 'same_stamped_file_sha256', 'sha256': value}))
    return {'new_source_hashes': computed, 'reused_source_hashes': len(hashed) - computed,
            'verified_archive_files': len(verified), 'new_archive_hashes': new_archive_hashes}


def candidate(item, cid, reference=False):
    result = {'id': cid, 'label': (Path(item['unit']).name + ' · ' + item['row'].get('inventory', item['record_key']) + ' · ' + item['row'].get('title', '')) if reference else item['label'],
              'eligible': True if reference else item['eligible'], 'complete': True if reference else item['eligible'],
              'members': [member(item['path'], item['stamp'], item['sha256'])], 'metadata': item['metadata'],
              'technical': {'width': item['width'], 'height': item['height'], 'format': Path(item['path']).suffix.lower(),
                            'is_surrogate': item.get('is_surrogate', False)},
              'preview_coverage': 'Primary image only; TIFF layers are not certified by perceptual matching'}
    if reference:
        result.update(reference_object_id=item['id'], archive_unit=item['unit'], source_record=item['row'],
                      evidence='Current selected archive Master. Archive files remain read-only.')
    else:
        result.update(source_roots=item['source_roots'], metadata_errors=item['metadata_errors'],
                      evidence='Inspected source file; selection is a proposal until separately authorized accession.')
    return result


def prepare(args):
    validate_preparation_paths(args)
    index = VisualIndex(args.index) if args.index else EmptyIndex()
    try:
        return _prepare(args, index)
    finally:
        if hasattr(index, 'con'):
            index.con.close()


def _prepare(args, index):
    profile = load_profile(args)
    sift_profile = load_sift_profile(args)
    sift_libraries = sift_runtime() if sift_profile else None
    require(args.czkawka or not any(getattr(args, name, None) is not None for name in
        ('czkawka_profile', 'czkawka_pass', 'czkawka_scope', 'czkawka_exact_hash', 'czkawka_proxy_edge', 'czkawka_preview_mode')),
        'Supply --czkawka when configuring Czkawka comparisons')
    source_roots = list(dict.fromkeys(paths(args.source)))
    archive_specs = list(dict.fromkeys(paths(args.against)))
    root = work_root(args.work_root, must_exist=False)
    require(source_roots and archive_specs, 'Supply source folders and archive units')
    require(not (root / 'workbench-input.json').exists() and not (root / 'decisions.jsonl').exists(),
            'This review is already prepared; use a new work directory to preserve its evidence and decisions')
    allowed = {'preparation.log', 'preparation-run.json'} if getattr(args, '_background_worker', False) else set()
    require(not root.exists() or root.is_dir() and all(p.name in allowed for p in root.iterdir()),
            'Preparation requires a new or empty work directory; reuse old evidence via --prior-review')
    input_stamps = {str(args.index): stamp(args.index)} if args.index else {}
    if getattr(args, 'czkawka_profile', None):
        input_stamps[str(args.czkawka_profile)] = stamp(args.czkawka_profile)
        require(Path(args.czkawka_profile).absolute() != root / 'czkawka-effective-profile.json',
                'Use a separate profile input; the effective profile is a preparation output')
    if args.czkawka:
        input_stamps[str(args.czkawka)] = stamp(args.czkawka)
    extensions = {'.' + x.lower().lstrip('.') for x in args.source_extensions.split(',')}
    target_extensions = {'.' + x.lower().lstrip('.') for x in args.master_extensions.split(',')}
    require(extensions <= PHOTO_EXTENSIONS and target_extensions <= PHOTO_EXTENSIONS,
            'This preparation adapter handles TIFF/JPEG photographs; other representations need explicit adapters')
    targets, units = [], []
    for spec in archive_specs:
        found, unit = read_unit(spec, index, target_extensions)
        require(unit['unit'] not in {u['unit'] for u in units}, 'Archive unit supplied more than once')
        targets.extend(found); units.append(unit); input_stamps.update(unit['input_stamps'])
    unit_paths = [Path(u['unit']) for u in units]
    for path in source_roots + unit_paths:
        require(not root.is_relative_to(path) and not path.is_relative_to(root), 'Work area overlaps an inspected/archive directory')
    require(not any(s.is_relative_to(u) or u.is_relative_to(s) for s in source_roots for u in unit_paths),
            'An archive unit cannot also be an inspected source folder in the same run')
    require(targets, 'No selected TIFF/JPEG Masters in the supplied units')
    root.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in root.iterdir()), 'Work-area outputs/caches cannot be symlinks')
    sources, excluded = inventory(source_roots, index, extensions)
    require(sources, 'No source photographs in requested formats')
    progress(f'{len(sources)} unique source files; {len(targets)} selected Masters across {len(units)} units')
    evidence = Evidence(sources, targets)
    reused = paths(getattr(args, 'prior_review', [])) + paths(getattr(args, 'prior_decisions', []))
    if getattr(args, 'avantmedia_evidence', None):
        reused += paths(args.avantmedia_evidence)
    reused = list(dict.fromkeys(reused))
    for path in reused:
        evidence.import_review(path)
    for path in paths(getattr(args, 'relations', [])):
        evidence.import_relations(path)
    metadata_extracted = metadata_for_sources(sources, root, reused, input_stamps)
    hash_counts = file_hashes(sources, targets, units, evidence, root,
                             paths(getattr(args, 'source_hashes', [])), reused, getattr(args, 'hash_missing', False))
    exact_coverage = run_blake3(args.czkawka, sources, targets, evidence, root, args.threads, reused) if profile['exact_hash'] == 'BLAKE3' else {'executed': False, 'reason': 'BLAKE3 not enabled'}
    source_groups = evidence.groups()
    target_by_id = {t['id']: t for t in targets}
    fixed_archive = getattr(args, 'archive_masters_fixed', True)
    known_existing = set()
    if fixed_archive:
        for group in source_groups:
            identity = assigned_md_id([evidence.proposals[s['path']] for s in group if s['path'] in evidence.proposals])
            target = target_by_id.get(identity)
            if identity and not (target and ersatz_jpeg(target) and any(genuine_tiff(s) for s in group)):
                known_existing.update(s['path'] for s in group)
    meta_index = MetadataIndex(targets)
    all_matches = {}
    progress('Combining previous consolidation, cached visual hashes, and metadata leads')
    for count, source in enumerate(sources, 1):
        previous_leads = []
        for lead in evidence.leads[source['path']]:
            if failed_historical_lead(lead) or source['path'] in known_existing:
                evidence.context[source['path']].append({'previous_comparison': lead,
                    'note': 'Outside active new-object discovery; historical evidence only'})
                continue
            if lead.get('engine') == 'pixel_coverage':
                evidence.context[source['path']].append({'previous_comparison': lead,
                    'note': 'Previous pixel comparison; coverage requires current stamped verification'})
                previous_leads.append({'engine': 'prior_similarity', 'target_id': lead['target_id'],
                                       'basis': 'previous_pixel_candidate'})
                continue
            if lead.get('engine') == 'sift':
                evidence.context[source['path']].append({'previous_comparison': lead,
                    'note': 'Previous SIFT evidence; current verification uses stamped feature/pair caches'})
                continue
            if lead.get('engine') == 'czkawka' and not any(
                all(lead.get(k) == settings[k] for k in ('algorithm', 'hash_size', 'resize_filter', 'geometric_invariance')) and
                lead.get('proxy_edge', 256) == profile['proxy_edge'] and lead.get('preview_mode', 'catalogued') == profile['preview_mode'] and
                lead.get('difference', 999) <= settings['max_difference'] for settings in profile['passes']):
                evidence.context[source['path']].append({'previous_comparison': lead, 'note': 'Outside current Czkawka profile; retained as historical evidence'})
            else:
                previous_leads.append(lead)
        evidence.leads[source['path']] = previous_leads
        all_matches[source['path']] = evidence.leads[source['path']] + evidence.confirmed[source['path']]
        assigned = assigned_md_id([evidence.proposals[source['path']]] if source['path'] in evidence.proposals else [])
        if fixed_archive and genuine_tiff(source) and assigned in target_by_id and ersatz_jpeg(target_by_id[assigned]):
            all_matches[source['path']].append({'engine': 'md_assignment', 'target_id': assigned,
                'basis': 'Known md object with a substitute JPEG Master; TIFF recovery needs review'})
        if not evidence.confirmed[source['path']] and source['path'] not in known_existing:
            all_matches[source['path']] += visual_matches(source['feature'], targets) + meta_index.search(source['metadata'])
        all_matches[source['path']] = [m for m in all_matches[source['path']]
                                      if (source['path'], m['target_id']) not in evidence.rejected]
        if count % 250 == 0:
            progress(f'Cached comparison: {count}/{len(sources)}')
    # Additional decoding is for residual discovery, after usable earlier leads have been joined.
    remaining = [s for s in sources if s['path'] not in known_existing and not evidence.confirmed[s['path']] and s['path'] not in evidence.proposals and
                 (profile['scope'] == 'unconfirmed' or not all_matches[s['path']])]
    coverage = {'source_proxies': 0, 'target_proxies': 0, 'source_errors': {}, 'target_errors': {},
                'executed': False, 'comparison_sources': len(remaining), 'residual_sources': len(remaining), 'profile': profile,
                'reason': 'No sources in requested scope' if not remaining else 'No visual passes configured' if not profile['passes'] else 'Czkawka not supplied'}
    if remaining and args.czkawka and profile['passes']:
        signature = hashlib.sha256(json_bytes({'sources': [(s['path'], s['stamp']) for s in remaining],
            'targets': [(t['id'], t['path'], t['stamp'], t['preview_path'], t['preview_stamp']) for t in targets],
            'binary': str(args.czkawka), 'binary_stamp': stamp(args.czkawka),
            'profile': profile})).hexdigest()
        extra, actual = run_czkawka(args.czkawka, remaining, targets, root, signature, args.threads, profile)
        coverage.update(actual, executed=True, reason='Configured visual comparisons completed')
        for path, pairs in extra.items():
            all_matches[path].extend(pairs)
        for path in coverage['source_errors']:
            evidence.sources[path]['eligible'] = False
    fixed_archive = getattr(args, 'archive_masters_fixed', True)
    pixel_coverage = verify_coverage(source_groups, targets, all_matches, evidence, root, reused, progress,
                                     fixed_archive=fixed_archive)
    sift_coverage = verify_matches(sources, targets, all_matches, evidence, root, reused,
        sift_profile, sift_libraries, args.threads, progress) if sift_profile else {'enabled': False}
    default_destination = str(Path(args.destination).absolute()) if getattr(args, 'destination', None) else ''
    require(not default_destination or default_destination in {u['unit'] for u in units},
            'Destination must be one of the explicitly supplied archive units')
    objects, initial, dispositions, matches_report = [], {}, [], []
    reasons = Counter()
    for group in source_groups:
        group = sorted(group, key=lambda s: (quality(s), s['path']))
        best = next((s for s in group if s['eligible']), group[0])
        oid = min(s['id'] for s in group)
        links = defaultdict(list)
        confirmed = set()
        for s in group:
            for lead in all_matches[s['path']]:
                if lead['target_id'] not in target_by_id or (s['path'], lead['target_id']) in evidence.rejected:
                    continue
                links[lead['target_id']].append({**lead, 'source_path': s['path']})
                if lead['engine'] == 'confirmed':
                    confirmed.add(lead['target_id'])
        issues = [issue for s in group for issue in evidence.issues[s['path']]]
        if any((s['path'], tid) in evidence.rejected for s in group for tid in confirmed):
            issues.append('Confirmed source group conflicts with a rejected archive match')
        proposals = [evidence.proposals[s['path']] for s in group if s['path'] in evidence.proposals]
        if len({(p.get('selected_path'),p.get('destination_unit')) for p in proposals}) > 1:
            issues.append('Several previous Master proposals require reconciliation')
        if not fixed_archive and proposals and any(x.get('basis') == PIXEL_BASIS for rows in links.values() for x in rows):
            issues.append('Verified archive image overlaps a saved Master proposal; reconcile the previous decision')
        if issues:
            confirmed = set()
        retained = min((target_by_id[tid] for tid in confirmed), key=lambda t: (quality(t), t['id']), default=None)
        adequacy = compare_quality(best, retained) if retained else None
        if fixed_archive and retained:
            adequacy = 'covered'
        if retained and any(x.get('adequacy_confirmed') and x['source_path'] == best['path'] for x in links[retained['id']]):
            adequacy = 'covered'
        if not fixed_archive and retained and adequacy == 'covered' and any(x.get('basis') == PIXEL_BASIS for x in links[retained['id']]):
            # A proof for one member must not hide another member with extra pages,
            # layers, a different crop, or unverified preservation quality.
            if not all(any(x['engine'] == 'confirmed' and x.get('adequacy_confirmed') and
                           x['source_path'] == s['path'] for x in links[retained['id']]) for s in group):
                adequacy = 'unknown'
        metadata = merge_metadata(group)
        meta_errors = any(s['metadata_errors'] for s in group)
        metadata_pending = bool(retained and any(
            metadata_question(compare_metadata(s['metadata'], retained['metadata'], bool(s['metadata_errors']))) and
            evidence.reviewed_metadata.get((s['path'], retained['id'])) != (s['metadata'], retained['metadata']) for s in group))
        if retained and any(x.get('record',{}).get('metadata') or x.get('record',{}).get('title')
                            for s in group for x in evidence.context[s['path']]):
            metadata_pending = True  # Researched/proposed fields also need consideration before hiding a source.
        if issues:
            reason = 'evidence_conflict'
        elif retained:
            reason = 'tiff_for_jpeg_review' if fixed_archive and ersatz_jpeg(retained) and any(genuine_tiff(s) for s in group) else 'redundant' if fixed_archive else 'master_upgrade' if adequacy == 'upgrade' else 'master_quality_review' if adequacy == 'unknown' else 'metadata_review' if metadata_pending else 'redundant'
        elif fixed_archive and any(s['path'] in known_existing for s in group):
            reason = 'existing_md_object'
        elif proposals:
            reason = proposals[0]['kind']
        elif any(active_comparison(leads) for leads in links.values()):
            reason = 'similarity_review'
        elif not best['eligible']:
            reason = 'evidence_unavailable'
        else:
            reason = 'proposed_new_object'
        # Absence is never asserted. Incomplete visual coverage remains visible on every new-object proposal.
        search_gaps = []
        if not best['feature'].get('hashes'):
            search_gaps.append('No current cached p/d/a hashes for the recommended source')
        if not coverage['executed'] and best in remaining:
            search_gaps.append('No Czkawka residual comparison was run')
        if any(not t['feature'].get('hashes') for t in targets):
            search_gaps.append('Some reference Masters lack current compatible p/d/a hashes')
        if coverage['target_errors']:
            search_gaps.append('Some reference images could not be decoded for Czkawka')
        czkawka_complete = coverage['executed'] and best in remaining and not coverage['target_errors'] and best['path'] not in coverage['source_errors']
        cached_complete = bool(best['feature'].get('hashes')) and all(t['feature'].get('hashes') for t in targets)
        if reason == 'proposed_new_object' and not (czkawka_complete or cached_complete) and not proposals:
            reason = 'search_coverage_review'
        reasons[reason] += 1
        selected_references = sorted((tid for tid in links if tid in confirmed or active_comparison(links[tid])), key=lambda tid: (tid not in confirmed,
            sift_rank(links[tid]),
            min((p.get('phash_distance', 999) for p in links[tid]), default=999), tid))
        shortlist = list(dict.fromkeys([*sorted(confirmed), *selected_references[:args.max_candidates]]))
        candidates, display_matches, comparisons = [], [], []
        source_ids = {s['path']: 'source' if len(group) == 1 else f'source:{n + 1}' for n, s in enumerate(group)}
        for source in group:
            c = candidate(source, source_ids[source['path']])
            c['prior_review_evidence'] = evidence.context[source['path']]
            candidates.append(c)
        for tid in shortlist:
            target = target_by_id[tid]; cid = 'archive:' + tid
            candidates.append(candidate(target, cid, True))
            for lead in links[tid]:
                display_matches.append({'a': source_ids[lead['source_path']], 'b': cid, **lead})
        for a_index, a in enumerate(candidates):
            for b in candidates[a_index + 1:]:
                comparisons.append({'a': a['id'], 'b': b['id'],
                    'fields': compare_metadata(a['metadata'], b['metadata'], bool(a.get('metadata_errors') or b.get('metadata_errors')))})
        destination = retained['unit'] if retained else default_destination
        recommended = 'archive:' + retained['id'] if retained and adequacy == 'covered' else source_ids[best['path']]
        initial_row = {'same_pairs': [[source_ids[group[0]['path']], source_ids[s['path']]] for s in group[1:]],
                       'destination_unit': destination, 'actor': 'preparation: evidence and Master preference'}
        for tid in sorted(confirmed):
            initial_row['same_pairs'].append([source_ids[group[0]['path']], 'archive:' + tid])
        if reason in ('redundant', 'metadata_review', 'tiff_for_jpeg_review'):
            initial_row.update(selected_candidate_id=recommended, redundant_candidate_ids=list(source_ids.values()))
        elif reason in ('proposed_new_object', 'master_upgrade', 'existing_md_object') and best['eligible']:
            initial_row.update(selected_candidate_id=recommended,
                               redundant_candidate_ids=[cid for path, cid in source_ids.items() if path != best['path']])
        if proposals and len(proposals) == 1 and not issues and not (fixed_archive and retained):
            proposal = proposals[0]
            if proposal['selected_path'] in source_ids and evidence.sources[proposal['selected_path']]['eligible']:
                initial_row['selected_candidate_id'] = source_ids[proposal['selected_path']]
                initial_row['redundant_candidate_ids'] = [cid for cid in initial_row.get('redundant_candidate_ids', [])
                                                          if cid != initial_row['selected_candidate_id']]
                if proposal.get('destination_unit') in {u['unit'] for u in units}:
                    initial_row['destination_unit'] = proposal['destination_unit']
                initial_row['note'] = 'Previous saved proposal preserved; see prior review evidence.'
        obj = {'id': oid, 'title': best['label'] + (f' · {len(group)} confirmed representations' if len(group) > 1 else ''),
               'description': 'Inspected sources compared with retained archive Masters. Existing archive Masters and metadata stay fixed.'
                   if fixed_archive else 'Inspected sources compared with selected archive Masters. All accession and replacement choices remain proposals.',
               'metadata': metadata, 'review_reason': reason, 'review_required': True,
               'metadata_review_required': metadata_pending, 'recommended_candidate_id': recommended,
               'source_candidate_ids': list(source_ids.values()), 'confirmed_reference_ids': ['archive:' + tid for tid in sorted(confirmed)],
               'candidates': candidates, 'matches': display_matches, 'metadata_comparisons': comparisons,
               'prior_review_evidence': [x for s in group for x in evidence.context[s['path']]],
               'search_coverage': {'candidate_records_found': len(links), 'candidate_records_shown': len(shortlist),
                   'sift_statuses': dict(Counter(m['status'] for rows in links.values() for m in rows if m['engine'] == 'sift')),
                   'issues': issues, 'gaps': search_gaps, 'source_errors': {s['path']:coverage['source_errors'].get(s['path'], s['feature'].get('error', '')) for s in group},
                   'unmatched_does_not_prove_absence': True, 'all_comparison_evidence': str(root / 'matches.jsonl')}}
        if proposals:
            obj['previous_proposals'] = proposals
        if reason != 'redundant':
            objects.append(obj); initial[oid] = initial_row
        for source in group:
            disposition = 'existing_md_source' if reason == 'existing_md_object' else 'tiff_for_jpeg_review' if reason == 'tiff_for_jpeg_review' and genuine_tiff(source) else 'redundant' if reason in ('redundant', 'metadata_review') else (
                'proposed_master' if initial_row.get('selected_candidate_id') == source_ids[source['path']] else
                'redundant_to_proposed_master' if source_ids[source['path']] in initial_row.get('redundant_candidate_ids', []) else 'unresolved')
            dispositions.append({'source_path': source['path'], 'source_stamp': source['stamp'],
                'source_roots': source['source_roots'], 'case_id': oid, 'review_case_id': oid if reason != 'redundant' else '',
                'disposition': disposition, 'review_reason': reason,
                'retained_master_path': retained['path'] if retained and adequacy == 'covered' else
                    next((s['path'] for s in group if source_ids[s['path']] == initial_row.get('selected_candidate_id')), '')
                    if disposition in ('proposed_master','redundant_to_proposed_master') else '',
                'archive_object_ids': sorted(confirmed) or ([assigned_md_id(proposals)] if reason == 'existing_md_object' else []), 'destination_unit': initial_row['destination_unit'],
                'metadata_review_required': metadata_pending, 'evidence': links,
                'source_modified': False, 'archive_modified': False, 'deletion_authorized': False})
            matches_report.append({'source_path': source['path'], 'source_stamp': source['stamp'],
                'source_feature': source['feature'], 'matches': all_matches[source['path']],
                'prior_context': evidence.context[source['path']], 'shortlisted_target_ids': shortlist,
                'case_id': oid, 'deletion_authorized': False})
    progress('Rechecking all inputs before publishing the preparation')
    input_stamps.update(evidence.input_stamps)
    for source in source_roots:
        elements_guard(source)
    for item in sources + targets:
        require(stamp(item['path']) == item['stamp'], f'File changed during preparation: {item["path"]}')
        for alias in item.get('aliases', []):
            require(stamp(alias['path']) == alias['stamp'], 'Archive derivative changed')
        if item['feature'].get('hashes'):
            require(stamp(item['feature']['path']) == item['feature']['stamp'], 'Indexed representation changed')
    for path, observed in input_stamps.items():
        require(stamp(path) == observed, f'Input evidence changed during preparation: {path}')
    write_jsonl(root / 'matches.jsonl', matches_report)
    write_jsonl(root / 'source-dispositions.jsonl', dispositions)
    write_jsonl(root / 'skipped-confirmed.jsonl', (r for r in dispositions if not r['review_case_id']))
    output = io.StringIO(newline='')
    fields = ['source_path','case_id','review_case_id','disposition','review_reason','retained_master_path','archive_object_ids','destination_unit','metadata_review_required','deletion_authorized']
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader()
    for row in dispositions:
        writer.writerow({k: json.dumps(row[k],ensure_ascii=False) if isinstance(row[k],(list,dict,bool)) else row[k] for k in fields})
    atomic_write(root / 'source-dispositions.csv', output.getvalue().encode())
    summary = {'format': 'archive-tools-preparation-2', 'prepared_at': datetime.now(timezone.utc).isoformat(),
        'source_roots': list(map(str,source_roots)), 'archive_units': [u['unit'] for u in units], 'units': units,
        'source_count': len(sources), 'master_count': len(targets), 'confirmed_source_groups': len(source_groups),
        'review_cases': len(objects), 'counts': dict(reasons), 'source_dispositions': dict(Counter(r['disposition'] for r in dispositions)),
        'source_cached_hash_triplets': sum(bool(s['feature'].get('hashes')) for s in sources),
        'master_linked_hash_triplets': sum(bool(t['feature'].get('hashes')) for t in targets),
        'metadata_errors': sum(bool(s['metadata_errors']) for s in sources), 'metadata_extracted': metadata_extracted,
        'prior_evidence': dict(evidence.stats), 'hashes': hash_counts, 'czkawka': coverage,
        'czkawka_exact': exact_coverage, 'czkawka_profile': profile, 'sift': sift_coverage,
        'pixel_coverage': pixel_coverage,
        'excluded_source_formats': dict(excluded), 'input_stamps': input_stamps, 'visual_extractors': index.profiles,
        'unmatched_does_not_prove_absence': True, 'source_modified': False, 'archive_modified': False,
        'deletion_authorized': False, 'accession_authorized': False}
    atomic_write(root / 'preparation-summary.json', json_bytes(summary))
    atomic_write(root / 'czkawka-effective-profile.json', json_bytes(profile))
    manifest = {'format': 'archive-tools-workbench-1', 'title': 'Archive reconciliation · ' + ', '.join(p.name for p in source_roots),
        'archive_masters_fixed': fixed_archive,
        'review_scope': DISCOVERY_SCOPE if fixed_archive else '',
        'read_only': False, 'read_roots': [str(p) for p in source_roots + unit_paths],
        'completion_receipts': [], 'reference_completion_receipts': [p for u in units for p in u['completion_receipts']],
        'destination_units': [{'path':u['unit'],'label':Path(u['unit']).name} for u in units],
        'objects': objects, 'initial_decisions': initial, 'preparation_summary': str(root / 'preparation-summary.json')}
    atomic_write(root / 'workbench-input.json', json_bytes(manifest))
    from workbench import Workbench
    Workbench(root, 'preparation').rebuild_csv()
    progress(json.dumps({'review_cases':len(objects),'source_files':len(sources),'case_counts':dict(reasons)},indent=2))
    return summary


def validate_preparation_paths(args):
    """The same write boundary applies before foreground or background preparation."""
    root = work_root(args.work_root, must_exist=False)
    # Validate write boundaries before creating even the log. The full adapters
    # and all evidence checks still run in the worker.
    inputs = paths(args.source) + paths(args.against)
    for source in paths(args.source):
        elements_guard(source)
        require(source.resolve() == source, 'Use canonical existing source directories')
    for spec in paths(args.against):
        elements_guard(spec)
        if spec.is_file():
            document = json.loads(spec.read_text())
            require(document.get('format') == 'archive-tools-unit-1', 'Unsupported normalized unit manifest')
            unit = Path(document['unit_root'])
            require(unit.is_absolute() and unit.resolve() == unit, 'Invalid unit_root')
            inputs.append(unit)
    for name in ('prior_review', 'prior_decisions', 'avantmedia_evidence', 'relations',
                 'source_hashes', 'index', 'czkawka_profile', 'czkawka', 'destination'):
        inputs.extend(paths(getattr(args, name, None)))
    for path in inputs:
        elements_guard(path)
        path = path.resolve()
        require(not root.is_relative_to(path) and not path.is_relative_to(root),
                'Work area overlaps an input/archive path')
    load_profile(args)
    load_sift_profile(args)
    return root


def start_background(args, argv):
    """Launch this same preparation in its own session; reserve a fresh work root."""
    root = validate_preparation_paths(args)
    require(not root.exists(), '--background requires a new work directory; existing reviews are preserved')
    root.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, '-B', '-u', str(Path(__file__).resolve()),
               *[value for value in argv if value != '--background'], '--_background-worker']
    record = {'state': 'starting', 'started_at': datetime.now(timezone.utc).isoformat(),
              'pid': None, 'exit_code': None, 'command': command,
              'work_root': str(root), 'log': str(root / 'preparation.log')}
    receipt = root / 'preparation-run.json'
    atomic_write(receipt, json_bytes(record))
    try:
        with (root / 'preparation.log').open('x') as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException as error:
        record.update(state='failed', exit_code=2, error=str(error),
                      finished_at=datetime.now(timezone.utc).isoformat())
        atomic_write(receipt, json_bytes(record))
        raise
    progress(json.dumps({'state': 'launched', 'pid': child.pid, 'status': str(receipt),
                         'log': record['log']}, indent=2))
    return 0


def background_worker(args):
    root = work_root(args.work_root)
    receipt = plain_file(root / 'preparation-run.json')
    record = json.loads(receipt.read_text())
    require(record['state'] == 'starting' and record['pid'] is None,
            'Background preparation has already started; use a new work directory')
    require(not (root / 'workbench-input.json').exists() and not (root / 'decisions.jsonl').exists(),
            'This review is already prepared; use a new work directory')
    record.update(state='running', pid=os.getpid())
    atomic_write(receipt, json_bytes(record))

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        progress(f'Preparation started; PID {os.getpid()}; work root: {root}')
        summary = prepare(args)
        record.update(state='completed', exit_code=0,
                      summary=str(root / 'preparation-summary.json'),
                      source_files=summary['source_count'], review_cases=summary['review_cases'],
                      counts=summary['counts'])
    except (SystemExit, KeyboardInterrupt) as error:
        record.update(state='interrupted', exit_code=error.code if isinstance(error, SystemExit) else 130)
        progress('Preparation interrupted; inspect the log before preparing in a new work directory')
    except BaseException as error:
        record.update(state='failed', exit_code=2, error=str(error))
        traceback.print_exc()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        progress(f'Preparation {record["state"]}; exit code {record["exit_code"]}')
        atomic_write(receipt, json_bytes(record))
    return record['exit_code']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source', type=Path, action='append', required=True, help='Inspected folder; repeat for more folders')
    parser.add_argument('--archive', '--against', dest='against', type=Path, action='append', required=True,
                        help='Ready archive unit or normalized unit JSON; repeat for more units')
    parser.add_argument('--work-root', type=Path, required=True)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument('--background', action='store_true',
                           help='Detach and return immediately; save progress and exit status in a NEW work root')
    execution.add_argument('--_background-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--index', type=Path, help='Existing read-only p/d/a index')
    parser.add_argument('--czkawka', type=Path, help='Optional Czkawka binary for residual visual comparisons')
    parser.add_argument('--czkawka-profile', type=Path, help='Reusable archive-tools-czkawka-1 JSON configuration')
    parser.add_argument('--czkawka-pass', action='append', help='ALGORITHM/SIZE/FILTER[/MAX_DIFFERENCE[/GEOMETRY]]; repeat to replace the visual pass list')
    parser.add_argument('--czkawka-scope', choices=('residual', 'unconfirmed'), help='Include already suggested but unconfirmed sources when retuning')
    parser.add_argument('--czkawka-exact-hash', choices=('off', 'BLAKE3'), help='Optional exact original-file comparison before visual matching')
    parser.add_argument('--czkawka-proxy-edge', type=int, help='Temporary comparison image maximum edge, 64–4096 pixels')
    parser.add_argument('--czkawka-preview-mode', choices=('catalogued', 'primary'), help='Use catalogue previews/thumbnails, or primary images/composites')
    parser.add_argument('--sift', action='store_true', help='Verify and rank existing candidate pairs with SIFT/RANSAC; requires OpenCV')
    parser.add_argument('--sift-proxy-edge', type=int, help='Primary-image SIFT preview edge, default 1600 pixels')
    parser.add_argument('--sift-ratio', type=float, help='Descriptor nearest-neighbour ratio limit, default 0.7')
    parser.add_argument('--sift-min-inliers', type=int, help='Minimum geometrically consistent distinct points, default 12')
    parser.add_argument('--sift-min-inlier-ratio', type=float, help='Minimum fraction of consistent matches, default 0.5')
    parser.add_argument('--prior-review', type=Path, action='append', default=[], help='Earlier work directory or decisions JSONL; repeatable')
    parser.add_argument('--prior-decisions', type=Path, action='append', default=[], help='Alias input for earlier workbench decisions')
    parser.add_argument('--avantmedia-evidence', type=Path, help='Compatibility alias for --prior-review on the legacy TIFF review')
    parser.add_argument('--relations', type=Path, action='append', default=[], help='Confirmed stamped relationships JSONL')
    parser.add_argument('--source-hashes', type=Path, action='append', default=[], help='Stamped path/size/mtime_ns/SHA-256 CSV')
    parser.add_argument('--hash-missing', action='store_true', help='Hash unindexed potential exact copies by compatible byte size')
    parser.add_argument('--destination', type=Path, help='Explicit destination unit for proposed new objects; otherwise left unresolved')
    parser.add_argument('--source-extensions', default='tif,tiff,jpg,jpeg')
    parser.add_argument('--master-extensions', default='tif,tiff,jpg,jpeg')
    parser.add_argument('--max-candidates', type=int, default=5)
    parser.add_argument('--threads', type=int, default=2)
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    try:
        require(1 <= args.threads <= 8 and 1 <= args.max_candidates <= 20, 'Invalid thread count or shortlist size')
        if args.background:
            return start_background(args, argv)
        if args._background_worker:
            return background_worker(args)
        prepare(args)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(2, f'search: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
