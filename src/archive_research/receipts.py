"""Verify explicitly selected extraction receipts for downstream local research."""
import hashlib
import json
import re

from .contracts import Result, require
from .storage import target, stamp
from .result_records import identity, retained, content_page


def read_current(roots, work, key, _parents=(), *, verify_source=True):
    require(isinstance(key,str) and re.fullmatch('[0-9a-f]{64}',key), 'Invalid receipt key')
    require(key not in _parents and len(_parents)<8, 'Cyclic or excessive receipt dependencies')
    data=target(roots.root(work,'work'),key+'.json').read_bytes(); row=json.loads(data)
    result=Result.from_record(row['result'])
    require(result.recipe.key_matches(result.page,result.region,key), 'Receipt key differs')
    require(result.status in ('complete','partial'), 'Extraction unavailable')
    digest = identity(row, data)
    if verify_source:
        roots.source(result.page.source)
    for artifact in result.artifacts:
        path=roots.artifact(artifact)
        require(stamp(path)==row['artifact_stamps'][artifact.path], 'Artifact stamp changed')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==artifact.sha256, 'Artifact content changed')
    if verify_source:
        roots.source(result.page.source)
    settings=result.recipe.record()['parameters'].get('settings',{})
    if result.recipe.extractor=='spelling':
        _,parent,_=read_origin(roots,work,settings['ocr_origin'],result.page,(*_parents,key),verify_source=verify_source)
        require(parent.recipe.extractor in ('ocr','document-text') and parent.region==result.region,'Spelling parent binding differs')
    origin=(settings.get('ocr_origin') if result.recipe.extractor=='scan-layout' else
            settings.get('region_origin') if result.recipe.extractor=='visual' else
            (settings.get('caption_origin') or settings.get('layout_origin')) if result.recipe.extractor=='ocr' else None)
    if origin is not None:
        _,parent,parent_digest=read_origin(roots,work,origin,result.page,(*_parents,key), verify_source=verify_source)
        require(parent_digest==origin['receipt_sha256'] and content_page(parent.page)==content_page(result.page),
                'Parent extraction receipt changed')
    if result.recipe.extractor=='document-text':
        for parent_origin in [settings['layout_origin'], *settings['ocr_origins'],
                              *([settings['order_origin']] if 'order_origin' in settings else [])]:
            read_origin(roots,work,parent_origin,result.page,(*_parents,key),verify_source=verify_source)
    if result.recipe.extractor=='document-layout' and settings.get('refinement'):
        for parent_origin in (settings['layout_origin'],settings['ocr_origin']):
            read_origin(roots,work,parent_origin,result.page,(*_parents,key),verify_source=verify_source)
    if result.recipe.extractor=='handwriting':
        read_origin(roots,work,settings['layout_origin'],result.page,(*_parents,key),verify_source=verify_source)
    return row,result,digest


def read_origin(roots, work, origin, page, _parents=(), *, verify_source=True):
    """Inspect the selected immutable parent using the child's verified binding."""
    require(origin['key'] not in _parents and len(_parents) < 8, 'Cyclic or excessive receipt dependencies')
    if 'evidence_sha256' in origin:
        row, result, digest = retained(roots, work, origin['evidence_sha256'])
        if origin['evidence_sha256'] == origin['receipt_sha256']:
            digest = origin['receipt_sha256']
    else:
        # Legacy dependencies keep their exact full-receipt hash. Retention makes
        # them inspectable even after the current receipt is rebound or retried.
        root = roots.root(work, 'work')
        saved = target(root, 'evidence-' + origin['receipt_sha256'] + '.json')
        if saved.exists():
            row, result, digest = retained(roots, work, origin['receipt_sha256'])
            digest = origin['receipt_sha256']
        else:
            row, result, digest = read_current(roots, work, origin['key'], _parents, verify_source=verify_source)
    require(digest == origin['receipt_sha256'] and content_page(result.page) == content_page(page),
            'Parent extraction receipt changed or belongs to another page')
    if verify_source:
        roots.source(page.source, verify_hash=bool(page.source.sha256) and result.page.source != page.source)
    return row, result, digest
