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


def extract(roots_file, plan_file, *, work='artifacts', summary='run-summary.json', limit=0,
            index=None, expected_plan=None):
    from .runner import extract_one, load_plan
    roots = extraction_roots(roots_file)
    plan_file = Path(plan_file).absolute()
    digest = expected_plan or hashlib.sha256(plan_file.read_bytes()).hexdigest()
    plan = load_plan(plan_file, digest)
    if index is not None:
        require(0 <= index < len(plan['tasks']), 'Task index outside plan')
        return extract_one(roots, work, plan['tasks'][index])
    require(limit >= 0, 'Limit must be nonnegative')
    root = roots.root(work, 'work')
    target(root, summary)
    value = {'format': 'archive-research-run-1', 'metadata_adopted': False,
             'plan_sha256': digest, 'requested': len(plan['tasks']), 'attempts': []}
    import fcntl
    fd = os.open(root / '.batch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as handle:
        require(os.fstat(handle.fileno()).st_nlink == 1, 'Unsafe batch lock')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for i in list(range(len(plan['tasks'])))[:limit or None]:
            process = subprocess.run([sys.executable, '-B', '-m', 'archive_research', 'extract',
                                      '--roots', str(Path(roots_file).absolute()), '--plan', str(plan_file),
                                      '--work', work, '--expected-plan', digest, '--index', str(i)],
                                     capture_output=True, text=True)
            outcome = ({'state': 'worker_error', 'error': process.stderr[-1500:]} if process.returncode
                       else json.loads(process.stdout))
            value['attempts'].append({'id': plan['tasks'][i]['id'], **outcome})
            with lock(root):
                write_json(root, summary, value)
    return value


def index(roots_file, selection, output, *, work='artifacts'):
    roots = extraction_roots(roots_file)
    root = roots.root(work, 'work')
    keys = selection['keys'] if isinstance(selection, dict) else selection
    with lock(root):
        value = build_index(roots, work, keys)
        write_json(root, output, value)
    return value


def search(roots_file, selection, index_name, output, *, sift=False, work='artifacts'):
    roots = extraction_roots(roots_file)
    root = roots.root(work, 'work')
    require(set(selection) == {'keys', 'visual_queries', 'text_queries', 'limit'}, 'Explicit retrieval selection required')
    require(isinstance(selection['visual_queries'], list) and len(selection['visual_queries']) <= 32
            and all(k in selection['keys'] for k in selection['visual_queries']), 'Select bounded visual queries')
    require(isinstance(selection['text_queries'], list) and len(selection['text_queries']) <= 32, 'Select bounded text queries')
    require(output != index_name, 'Search output must preserve its index')
    with lock(root):
        current = refresh_index(roots, work, read_json(target(root, index_name)))
        require(current['selection'] == selection['keys'], 'Index differs from selected receipts')
        results = {'metadata_adopted': False, 'selection': selection, 'visual': [],
                   'index_sha256': hashlib.sha256(encoded(current).encode()).hexdigest(),
                   'text': [text_search(current, q, selection['limit']) for q in selection['text_queries']],
                   'coverage': current['coverage'], 'unavailable': current['unavailable']}
        def picture(key):
            item = read_evidence(roots, work, {'key': key, 'label': 'Selected visual lead', 'kind': 'detail',
                                             'lineage': 'Source binding in index', 'attribution': 'Local research only'})
            return evidence_image(roots, work, item)
        for key in selection['visual_queries']:
            result = visual_search(current, key, selection['limit'])
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
