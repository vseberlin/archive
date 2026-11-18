"""Reuse a verified prepared image across extractors under the existing writer lock."""
from dataclasses import asdict
import hashlib
import importlib.metadata
import json

from . import storage
from .contracts import Transform, encoded, page_binding, require
from .imaging import RENDER_POLICY
from .result_records import content_page
from .preparation import render


def prepared_view(roots, page, directory, root, *, dpi, clockwise, region=None):
    from .runner import tool_version
    require(type(dpi) is int and 72 <= dpi <= 600, 'DPI outside bounded trial range')
    require(clockwise in (0,90,180,270), 'Unsupported editorial rotation')
    binding = {'page':content_page(page), 'key_version':3, 'region':asdict(region) if region else None,
               'dpi':dpi if page.kind == 'pdf' else None,
               'clockwise':clockwise, 'policy':RENDER_POLICY,
               'Pillow':importlib.metadata.version('Pillow')}
    if page.kind == 'pdf':
        binding['pdftoppm'] = tool_version(['pdftoppm','-v'])
    key = hashlib.sha256(encoded(binding).encode()).hexdigest()
    name = 'prepared-'+key
    image = storage.target(root, name+'.png')
    receipt = storage.target(root, name+'.json')
    if not receipt.exists():
        legacy_binding = {**binding, 'page': page_binding(page), 'key_version': 2}
        legacy_name = 'prepared-' + hashlib.sha256(encoded(legacy_binding).encode()).hexdigest()
        legacy = storage.target(root, legacy_name + '.json')
        if legacy.exists():
            # Read a selected compatible v2 preparation without rewriting it.
            receipt, image, binding = legacy, storage.target(root, legacy_name + '.png'), legacy_binding
    source = asdict(page.source)
    roots.source(page.source)
    if receipt.exists():
        value = json.loads(receipt.read_text())
        require(value['binding'] == json.loads(encoded(binding)), 'Prepared binding differs')
        require(storage.stamp(image) == value['stamp'] and
                hashlib.sha256(image.read_bytes()).hexdigest() == value['sha256'],
                'Prepared image changed')
        transform = value['transform']
        transform['to_source'] = tuple(transform['to_source'])
        transform['steps'] = tuple(transform['steps'])
        # A content-only key does not prove that the current file has that
        # content. Legacy receipts also need this first verified binding.
        rebound = value.get('verified_source') != source
        roots.source(page.source, verify_hash=bool(page.source.sha256) and rebound)
        if rebound:
            storage.write_json(root, receipt.name, {**value, 'verified_source':source})
        return image, Transform(**transform), value['rotation']
    roots.source(page.source, verify_hash=bool(page.source.sha256))
    rendered, transform, rotation = render(roots, page, directory, dpi=dpi,
                                           clockwise=clockwise, region=region)
    data = rendered.read_bytes()
    roots.source(page.source)
    storage.write(root, image.name, data)
    storage.write_json(root, receipt.name, {'binding':binding, 'transform':asdict(transform),
        'verified_source':source,
        'rotation':rotation, 'stamp':storage.stamp(image),
        'sha256':hashlib.sha256(data).hexdigest(), 'metadata_adopted':False})
    return image, transform, rotation
