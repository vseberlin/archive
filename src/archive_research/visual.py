"""Opt-in wrapper for the existing visual preview callable; no visual imports here."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

from .contracts import Artifact, Page, Recipe, Result, Source, Transform, require, text, relative_path
from .sources import Roots


@dataclass(frozen=True)
class Preview:
    result: Result
    legacy: dict


def preview(roots: Roots, page: Page, work: str, row: dict, override: dict, *,
            renderer, version: str, dependencies: dict):
    """Call visual_research.make_preview without changing its output or cache naming.

    The caller supplies the existing callable explicitly. Installed core users do
    not need the legacy scripts or their optional dependencies. This wrapper does
    not select a Master, infer dimensions/order, or promise positioned OCR.
    """
    require(callable(renderer) and text(version), 'Explicit renderer callable/version required')
    require(isinstance(override, dict), 'Preview settings must be a dictionary')
    require(page.kind in ('image', 'tiff') and page.index == 0,
            'Legacy preview adapter supports only the first image/frame')
    source = roots.source(page.source)
    output = roots.root(work, 'work')
    require(row.get('id') == page.source.object_id and row.get('path') == str(source)
            and row.get('stamp') == page.source.stamp,
            'Legacy row differs from the explicit source/identity binding')
    require('members' not in row, 'Ordered members require an explicit per-member row')
    require(isinstance(dependencies, dict) and dependencies and
            all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in dependencies.items()),
            'Record the selected renderer dependency versions')
    # The existing renderer expects this directory and creates neither a new
    # work area nor its own permissions boundary. Reject redirection up front.
    previews = output / 'previews'
    require(previews.resolve() == previews and previews.is_dir(), 'Existing real previews directory required')
    settings = deepcopy(override)
    start = time.monotonic()
    legacy = renderer(output, deepcopy(row), settings)
    elapsed = time.monotonic() - start
    roots.source(page.source)
    roots.root(work, 'work')
    require(legacy.get('id') == page.source.object_id and legacy.get('path') == str(source)
            and legacy.get('stamp') == page.source.stamp and legacy.get('status') == 'ok',
            'Legacy renderer returned a different binding or incomplete preview')
    artifact = Artifact(work, legacy['preview'], 'visual-preview', legacy['preview_sha256'])
    roots.artifact(artifact)
    recipe = Recipe.create('legacy-visual-preview', version, settings=override,
                           dependencies=dependencies, coverage={'kind': page.kind, 'index': page.index},
                           preprocessing=legacy['recipe'])
    # EXIF orientation is not fully reported by the legacy renderer. An invented
    # inverse transform would make source highlights misleading.
    transform = Transform(*legacy['preview_size'], steps=('legacy EXIF orientation and thumbnail recipe',))
    result = Result(page, recipe, 'complete', elapsed, artifacts=(artifact,), transform=transform,
                    limitations=('Preview only; no OCR or page-completeness claim.',
                                 'Exact inverse transform unavailable; source-region highlighting withheld.',))
    return Preview(result, legacy)


def existing_leads(manifest_path, neighbours_path, preview_root, source_roots, limit=6):
    """Project a bounded set of existing retrieval leads, without rerunning a model."""
    require(type(limit) is int and 1 <= limit <= 20, 'Choose a bounded existing-lead sample')
    manifest = json.loads(Path(manifest_path).read_text())
    neighbours = json.loads(Path(neighbours_path).read_text())
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    require(neighbours['manifest_digest'] == digest, 'Existing neighbour results belong to another manifest')
    rows = {row['id']: row for row in manifest['candidates'] + manifest['references']}
    preview_root = Path(preview_root)
    require(preview_root.is_absolute() and preview_root.resolve() == preview_root, 'Real preview root required')

    def bind(row):
        path = Path(row['path'])
        matches = [(name, Path(root)) for name, root in source_roots.items() if path.is_relative_to(Path(root))]
        require(len(matches) == 1, 'Existing visual source needs one declared root')
        name, root = matches[0]
        source = Source(str(root), row['id'], row.get('master_path') or row['path'], name,
                        str(path.relative_to(root)), **row['stamp'], role='existing_visual_research_view')
        relative_path(row['preview'])
        image = preview_root / row['preview']
        require(image.resolve() == image and image.is_file(), 'Existing visual preview unavailable or redirected')
        stamp = image.stat()
        prepared = Source(str(preview_root), row['id'], row['preview'], 'legacy_images', row['preview'],
                          stamp.st_size, stamp.st_mtime_ns, row['preview_sha256'], role='research_preview')
        from dataclasses import asdict
        return {'id': row['id'], 'title': row.get('title', row['id']),
                'source': asdict(source), 'preview': asdict(prepared)}

    leads = []
    for item in neighbours['items']:
        for match in item['references'][:2]:
            leads.append({'candidate': bind(rows[item['id']]), 'reference': bind(rows[match['id']]),
                          'similarity': match['similarity']})
            if len(leads) == limit:
                return {'format': 'archive-visual-leads-1', 'metadata_adopted': False,
                        'manifest_digest': digest, 'model': neighbours['model'], 'leads': leads}
    return {'format': 'archive-visual-leads-1', 'metadata_adopted': False,
            'manifest_digest': digest, 'model': neighbours['model'], 'leads': leads}
