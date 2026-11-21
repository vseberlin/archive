"""Application operations shared by commands and the workbench.

Explicit selections and existing result formats remain authoritative. No daemon,
automatic discovery, external submission or catalogue adoption is implied.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time

from .config import extraction_roots
from .contracts import encoded, require
from .evidence import build_packet, evidence_image, read_evidence, refresh_packet
from .profiles import research_profiles
from .retrieval import build_index, refresh_index, text_search, visual_search
from .storage import lock, target, write_json


def read_json(path):
    return json.loads(Path(path).read_text())


def current_packet(roots, work, stored):
    return refresh_packet(roots, work, stored, research_profiles(stored['selection']['profiles']))


def document_parts(roots_file, key, profile, output, *, work='artifacts', visual_model=None, languages='deu+eng', handwriting=None):
    """Write a new bounded downstream plan; never replace a prior selection."""
    from .extractors.document_layout import downstream_plan
    roots=extraction_roots(roots_file);root=roots.root(work,'work')
    with lock(root):
        require(not target(root,output).exists(),'Downstream plan already exists; select another output')
        value=downstream_plan(roots,work,key,profile=profile,visual_model=visual_model,languages=languages,handwriting=handwriting)
        write_json(root,output,value)
    return value


def document_tables(roots_file, layout_key, reading_key, output, *, work='artifacts'):
    """Prepare one explicit table refinement; use the ordinary runner to execute."""
    from .extractors.document_layout import table_refinement_task
    roots=extraction_roots(roots_file);root=roots.root(work,'work')
    with lock(root):
        require(not target(root,output).exists(),'Table plan already exists; select another output')
        value={'format':'archive-research-plan-1','metadata_adopted':False,
               'tasks':[table_refinement_task(roots,work,layout_key,reading_key)]}
        write_json(root,output,value)
    return value


def extract(roots_file, plan_file, *, work='artifacts', summary='run-summary.json', limit=0,
            index=None, expected_plan=None, chunk_size=1, worker_timeout=900,
            worker_start=None, worker_count=None, progress=None):
    from .runner import extract_one, load_plan
    roots = extraction_roots(roots_file)
    plan_file = Path(plan_file).absolute()
    digest = expected_plan or hashlib.sha256(plan_file.read_bytes()).hexdigest()
    plan = load_plan(plan_file, digest)
    if index is not None:
        require(0 <= index < len(plan['tasks']), 'Task index outside plan')
        return extract_one(roots, work, plan['tasks'][index])
    if worker_start is not None:
        require(type(worker_count) is int and 1 <= worker_count <= 16 and
                0 <= worker_start < len(plan['tasks']) and worker_start + worker_count <= len(plan['tasks']),
                'Invalid worker selection')
        root = roots.root(work, 'work')
        target(root, progress)
        value = {'plan_sha256': digest, 'worker_start': worker_start, 'attempts': []}
        encoders = {}
        for task in plan['tasks'][worker_start:worker_start+worker_count]:
            try:
                outcome = extract_one(roots, work, task, encoders=encoders)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                outcome = {'state': 'worker_error', 'error': str(error)[:1500]}
            value['attempts'].append({'id': task['id'], **outcome})
            with lock(root):
                write_json(root, progress, value)
        return value
    require(limit >= 0, 'Limit must be nonnegative')
    require(type(chunk_size) is int and 1 <= chunk_size <= 16 and
            type(worker_timeout) is int and 1 <= worker_timeout <= 7200, 'Invalid bounded worker limits')
    root = roots.root(work, 'work')
    target(root, summary)
    value = {'format': 'archive-research-run-1', 'metadata_adopted': False,
             'plan_sha256': digest, 'requested': len(plan['tasks']), 'attempts': [],
             'chunk_size': chunk_size, 'worker_timeout_seconds': worker_timeout}
    import fcntl
    fd = os.open(root / '.batch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as handle:
        require(os.fstat(handle.fileno()).st_nlink == 1, 'Unsafe batch lock')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        count = min(limit or len(plan['tasks']), len(plan['tasks']))
        for i in range(0, count, chunk_size):
            chunk_count = min(chunk_size, count-i)
            progress_name = 'worker-' + digest + '-' + str(i) + '.json'
            # Reset only this run's disposable progress; immutable results remain.
            with lock(root):
                write_json(root, progress_name, {'plan_sha256': digest, 'worker_start': i, 'attempts': []})
            command = [sys.executable, '-B', '-m', 'archive_research', 'extract',
                       '--roots', str(Path(roots_file).absolute()), '--plan', str(plan_file),
                       '--work', work, '--expected-plan', digest, '--worker-start', str(i),
                       '--worker-count', str(chunk_count), '--progress', progress_name]
            start = time.monotonic()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            timed_out = False
            try:
                _, stderr = process.communicate(timeout=worker_timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Worker exited at the timeout/interrupt boundary.
                _, stderr = process.communicate()
                if isinstance(error, KeyboardInterrupt):
                    partial = read_json(target(root, progress_name))
                    value['attempts'].extend(partial['attempts'])
                    value['interrupted'] = True
                    with lock(root):
                        write_json(root, summary, value)
                    raise
                timed_out = True
            partial = read_json(target(root, progress_name))
            require(partial['plan_sha256'] == digest and partial['worker_start'] == i, 'Worker progress differs')
            expected_ids = [task['id'] for task in plan['tasks'][i:i+chunk_count]]
            require([a['id'] for a in partial['attempts']] == expected_ids[:len(partial['attempts'])],
                    'Worker progress is not the selected prefix')
            value['attempts'].extend(partial['attempts'])
            for task_id in expected_ids[len(partial['attempts']):]:
                value['attempts'].append({'id': task_id, 'state': 'worker_timeout' if timed_out else 'worker_error',
                                          'error': 'Bounded worker stopped; retry uses completed receipts. ' + stderr[-1000:]})
            value['last_chunk_seconds'] = time.monotonic() - start
            with lock(root):
                write_json(root, summary, value)
    return value


def index(roots_file, selection, output, *, work='artifacts'):
    roots = extraction_roots(roots_file)
    root = roots.root(work, 'work')
    selection = selection if isinstance(selection, dict) else {'keys': selection}
    from .search_projection import build_projection
    with lock(root):
        value = build_projection(roots, work, selection)
        write_json(root, output, value)
    return value


def search(roots_file, selection, index_name, output, *, sift=False, work='artifacts'):
    roots = extraction_roots(roots_file)
    root = roots.root(work, 'work')
    require({'keys', 'visual_queries', 'text_queries', 'limit'} <= set(selection) <=
            {'keys', 'visual_queries', 'text_queries', 'limit', 'catalogues', 'findings', 'identity_bindings', 'offset', 'fuzzy'},
            'Explicit retrieval selection required')
    require(isinstance(selection['visual_queries'], list) and len(selection['visual_queries']) <= 32
            and all(k in selection['keys'] for k in selection['visual_queries']), 'Select bounded visual queries')
    require(isinstance(selection['text_queries'], list) and len(selection['text_queries']) <= 32, 'Select bounded text queries')
    require(output != index_name, 'Search output must preserve its index')
    with lock(root):
        stored = read_json(target(root, index_name))
        from .search_projection import FORMAT, validate, query, visual_query
        aggregate = stored.get('format') == FORMAT
        current = validate(stored) if aggregate else refresh_index(roots, work, stored)
        require((current['selection']['keys'] if aggregate else current['selection']) == selection['keys'],
                'Index differs from selected receipts')
        results = {'metadata_adopted': False, 'selection': selection, 'visual': [],
                   'index_sha256': hashlib.sha256(encoded(current).encode()).hexdigest(),
                   'text': [(query(current, q, limit=selection['limit'], offset=selection.get('offset', 0),
                                   fuzzy=selection.get('fuzzy', False)) if aggregate else
                             text_search(current, q, selection['limit'])) for q in selection['text_queries']],
                   'coverage': current['coverage'], 'unavailable': current['unavailable']}
        def picture(key):
            item = read_evidence(roots, work, {'key': key, 'label': 'Selected visual lead', 'kind': 'detail',
                                             'lineage': 'Source binding in index', 'attribution': 'Local research only'})
            return evidence_image(roots, work, item)
        for key in selection['visual_queries']:
            result = (visual_query(current, key, limit=selection['limit'], offset=selection.get('offset', 0))
                      if aggregate else visual_search(current, key, selection['limit']))
            if sift:
                from .sift import compare_prepared
                source = picture(key)
                for hit in result['hits']:
                    hit['geometry'] = compare_prepared(source, picture(hit['target']))
            results['visual'].append({'query': key, **result})
        write_json(root, output, results)
    return results


def packet(roots_file, selection, output, *, prompt_output=None, work='artifacts'):
    roots = extraction_roots(roots_file)
    root = roots.root(work, 'work')
    if prompt_output:
        require(prompt_output != output, 'Packet and prompt filenames must differ')
        target(root, prompt_output)
    with lock(root):
        value = build_packet(roots, work, selection, research_profiles(selection['profiles']))
        if prompt_output:
            from .assessment import prepare_assessment
            draft = prepare_assessment(roots, work, value, research_profiles(selection['profiles']))
            write_json(root, prompt_output, draft)
        write_json(root, output, value)
    return value


def run(roots_file, retrieval_selection, packet_selection, prefix, *, plan_file=None, sift=False, model=None):
    """One explicit local workflow; optional model request is prepared, never sent."""
    import re
    require(isinstance(prefix, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}', prefix), 'Simple output prefix required')
    roots = extraction_roots(roots_file)
    root = roots.root('artifacts', 'work')
    names = {k: prefix + '-' + k + '.json' for k in ('extraction', 'index', 'search', 'packet', 'draft', 'request', 'transport')}
    for name in names.values():
        require(not target(root, name).exists(), 'Workflow output exists; choose a new prefix or resume stages explicitly')
    if plan_file:
        result = extract(roots_file, plan_file, summary=names['extraction'])
        require(all(a['state'] in ('complete', 'partial') for a in result['attempts']), 'Extraction failures; inspect summary before continuing')
    indexed = index(roots_file, retrieval_selection, names['index'])
    search(roots_file, retrieval_selection, names['index'], names['search'], sift=sift)
    prepared = packet(roots_file, packet_selection, names['packet'], prompt_output=names['draft'])
    if model:
        from .assessment.io import prepare_request
        with lock(root):
            request, transport = prepare_request(roots, 'artifacts', prepared,
                                                 research_profiles(prepared['selection']['profiles']), model=model)
            write_json(root, names['request'], request)
            write_json(root, names['transport'], transport)
    return {'outputs': {k: v for k, v in names.items() if target(root, v).exists()},
            'indexed': len(indexed['entries']), 'unavailable': len(indexed['unavailable']),
            'evidence': len(prepared['evidence']), 'external_submission': False, 'metadata_adopted': False}
