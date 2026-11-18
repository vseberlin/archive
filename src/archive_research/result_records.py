"""Immutable selected evidence and a rebuildable computation lookup.

Existing receipt names remain current bindings. No source identity is allocated,
and no legacy receipt is discovered or converted without an explicit selection.
All files stay flat in the caller's existing work root, under its writer lock.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import re

from .contracts import Artifact, Result, encoded, require
from . import storage


def sha(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def checked_digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid evidence digest')
    return value


def content_page(page):
    """Verified bytes plus technical address; no catalogue membership or order."""
    source = page.source
    content = ({'sha256': source.sha256} if source.sha256 else
               {'root': source.root, 'path': source.path, **source.stamp})
    return {'content': content, **{k: getattr(page, k) for k in
                                 ('kind', 'index', 'width', 'height', 'units', 'origin')}}


def normalized(value):
    """Remove only declared locator structures, never arbitrary recipe settings."""
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if not isinstance(value, dict):
        return value
    if {'root', 'path', 'sha256'} <= value.keys() and value['sha256']:
        # Source/Artifact-shaped model and output declarations.
        return {k: normalized(value[k]) for k in ('sha256', 'kind') if k in value}
    if {'key', 'receipt_sha256'} <= value.keys():
        return {k: normalized(v) for k, v in value.items() if k not in ('key', 'evidence_sha256')}
    return {k: normalized(v) for k, v in value.items()}


def computation(page, recipe, region=None):
    record = normalized(recipe.record())
    # Runner coverage.member is logical membership, not a technical frame.
    coverage = record['parameters'].get('coverage')
    if isinstance(coverage, dict):
        coverage.pop('member', None)
    return {'format': 'archive-computation-1', 'input': content_page(page),
            'recipe': record, 'region': asdict(region) if region else None}


def computation_key(page, recipe, region=None):
    return sha(computation(page, recipe, region))


def _map_artifacts(value, paths):
    if isinstance(value, list):
        return [_map_artifacts(v, paths) for v in value]
    if isinstance(value, dict):
        return {k: paths.get(v, v) if k in ('path', 'crop') and isinstance(v, str)
                else _map_artifacts(v, paths) for k, v in value.items()}
    return value


def payload(row):
    result = Result.from_record(row['result'])
    paths = {a.path: 'sha256:' + a.sha256 for a in result.artifacts}
    return {'format': 'archive-immutable-result-1',
            'computation': computation(result.page, result.recipe, result.region),
            'status': result.status, 'transform': asdict(result.transform) if result.transform else None,
            'limitations': list(result.limitations),
            'artifacts': [{'kind': a.kind, 'sha256': a.sha256} for a in result.artifacts],
            **{k: normalized(_map_artifacts(row[k], paths)) for k in
               ('words', 'feature', 'discovery', 'display_rotation') if k in row}}


def identity(row, data=None):
    if 'result_sha256' in row:
        require(row['result_sha256'] == sha(payload(row)), 'Immutable result changed')
        return row['result_sha256']
    return hashlib.sha256(data if data is not None else (encoded(row) + '\n').encode()).hexdigest()


def immutable(root, name, data):
    path = storage.target(root, name)
    if path.exists():
        require(path.read_bytes() == data, 'Retained evidence content changed')
    else:
        storage.write(root, name, data)


def retain(roots, work, row, *, original_bytes=None):
    """Retain exact old receipt and copies of artifacts before any replacement."""
    root = roots.root(work, 'work')
    result = Result.from_record(row['result'])
    for artifact in result.artifacts:
        checked_digest(artifact.sha256)
        data = roots.artifact(artifact).read_bytes()
        require(hashlib.sha256(data).hexdigest() == artifact.sha256, 'Artifact content changed')
        immutable(root, 'artifact-' + artifact.sha256, data)
    data = original_bytes if original_bytes is not None else (encoded(row) + '\n').encode()
    require(encoded(json.loads(data)) == encoded(row), 'Retained receipt bytes differ')
    digest = hashlib.sha256(data).hexdigest()
    immutable(root, 'evidence-' + digest + '.json', data)
    return digest


def retained(roots, work, digest):
    """Read exact historical evidence, independently of today's source locator."""
    root = roots.root(work, 'work')
    data = storage.target(root, 'evidence-' + checked_digest(digest) + '.json').read_bytes()
    require(hashlib.sha256(data).hexdigest() == digest, 'Retained receipt changed')
    row = json.loads(data)
    original_identity = identity(row, data)
    result = Result.from_record(row['result'])
    paths = {}
    artifacts = []
    for artifact in result.artifacts:
        name = 'artifact-' + checked_digest(artifact.sha256)
        file = storage.target(root, name)
        require(hashlib.sha256(file.read_bytes()).hexdigest() == artifact.sha256, 'Retained artifact changed')
        paths[artifact.path] = name
        artifacts.append(Artifact(work, name, artifact.kind, artifact.sha256))
    row = deepcopy(row)
    for field in ('feature', 'discovery'):
        if field in row:
            row[field] = _map_artifacts(row[field], paths)
    row['result'] = replace(result, artifacts=tuple(artifacts)).record()
    row['artifact_stamps'] = {a.path: storage.stamp(storage.target(root, a.path)) for a in artifacts}
    return row, Result.from_record(row['result']), original_identity


def publish(roots, work, key, row):
    """Save evidence first, then current binding and disposable reuse pointer."""
    root = roots.root(work, 'work')
    value = deepcopy(row)
    result = Result.from_record(value['result'])
    result_payload = payload(value)
    value['result_sha256'] = sha(result_payload)
    value['computation_sha256'] = computation_key(result.page, result.recipe, result.region)
    immutable(root, 'result-' + value['result_sha256'] + '.json', (encoded(result_payload) + '\n').encode())
    evidence = retain(roots, work, value)
    storage.write_json(root, key + '.json', value)
    try:
        storage.write_json(root, 'computation-' + value['computation_sha256'] + '.json',
                           {'format': 'archive-computation-lookup-1', 'evidence_sha256': evidence,
                            'result_sha256': value['result_sha256'], 'computation_sha256': value['computation_sha256']})
    except OSError as error:
        # Evidence and current binding were saved. Failure of a disposable
        # lookup must not report a failed extraction or invite duplicate work.
        value['projection_warning'] = 'Evidence saved; reuse lookup may be stale: ' + str(error)[:1000]
    return value, evidence


def reuse(roots, work, page, recipe, region):
    key = computation_key(page, recipe, region)
    path = storage.target(roots.root(work, 'work'), 'computation-' + key + '.json')
    if not path.exists():
        return None
    pointer = json.loads(path.read_text())
    require(pointer.get('format') == 'archive-computation-lookup-1' and pointer['computation_sha256'] == key,
            'Computation lookup differs')
    row, result, digest = retained(roots, work, pointer['evidence_sha256'])
    require(digest == pointer['result_sha256'] and computation_key(result.page, result.recipe, result.region) == key,
            'Retained computation differs')
    from .runner import retryable
    if result.status not in ('complete', 'partial') or retryable(row, result.recipe):
        return None
    # A lookup is no proof of current content or archival object equivalence.
    roots.source(page.source, verify_hash=bool(page.source.sha256))
    row['rebound_from'] = {'page': asdict(result.page), 'evidence_sha256': pointer['evidence_sha256']}
    row['result'] = replace(result, page=page, recipe=recipe).record()
    require(sha(payload(row)) == digest, 'Rebinding changed extraction meaning')
    return row


def origin(roots, work, key, digest):
    """Add exact-version addressing to a newly prepared dependency, if retained."""
    path = storage.target(roots.root(work, 'work'), key + '.json')
    data = path.read_bytes()
    row = json.loads(data)
    require(identity(row, data) == digest, 'Dependency changed while selecting')
    evidence = hashlib.sha256(data).hexdigest()
    value = {'key': key, 'receipt_sha256': digest}
    if storage.target(path.parent, 'evidence-' + evidence + '.json').exists():
        value['evidence_sha256'] = evidence
    return value
