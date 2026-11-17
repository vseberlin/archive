"""Verify explicitly selected extraction receipts for downstream local research."""
import hashlib
import json
import re

from .contracts import Result, require
from .storage import target, stamp


def read_current(roots, work, key, _parents=()):
    require(isinstance(key,str) and re.fullmatch('[0-9a-f]{64}',key), 'Invalid receipt key')
    require(key not in _parents and len(_parents)<8, 'Cyclic or excessive receipt dependencies')
    data=target(roots.root(work,'work'),key+'.json').read_bytes(); row=json.loads(data)
    result=Result.from_record(row['result'])
    require(result.recipe.key_matches(result.page,result.region,key), 'Receipt key differs')
    require(result.status in ('complete','partial'), 'Extraction unavailable')
    roots.source(result.page.source)
    for artifact in result.artifacts:
        path=roots.artifact(artifact)
        require(stamp(path)==row['artifact_stamps'][artifact.path], 'Artifact stamp changed')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==artifact.sha256, 'Artifact content changed')
    roots.source(result.page.source)
    settings=result.recipe.record()['parameters'].get('settings',{})
    origin=(settings.get('ocr_origin') if result.recipe.extractor=='scan-layout' else
            settings.get('region_origin') if result.recipe.extractor=='visual' else
            settings.get('caption_origin') if result.recipe.extractor=='ocr' else None)
    if origin is not None:
        _,parent,digest=read_current(roots,work,origin['key'],(*_parents,key))
        require(digest==origin['receipt_sha256'] and parent.page==result.page,
                'Parent extraction receipt changed')
    return row,result,hashlib.sha256(data).hexdigest()
