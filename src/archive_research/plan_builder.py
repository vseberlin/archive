"""Build sharded first-pass extraction plans from qualified unit catalogues.

This is the missing step between existing catalogue/manifest records and the
finite ``archive-research-plan-1`` contract. It performs no recognition and no
recursive filesystem discovery: every object comes from ``CATALOG.csv`` through
``units.selected_members``, which already refuses unresolved order, duplicate
witnesses and size disagreements. Records that cannot be planned are listed with
their reason instead of stopping the whole unit.

Page geometry is read with the existing ``preparation.inspect_page``. Kind is
taken from file signatures, not filename suffixes. Output is a set of new flat
plan shards plus one manifest in the configured work root; nothing is overwritten.
"""
from dataclasses import asdict
import json
from pathlib import Path

from .contracts import Source, encoded, require
from .storage import lock, target, write_json

FORMAT = 'archive-research-plan-spec-1'
MATERIALS = ('clean_print', 'historical_newspaper', 'rd_reports', 'photo')
ROLES = ('preservation_master', 'preservation_surrogate')
DEFAULT_EXTRACTORS = {
    'clean_print': ['pdf-text', 'ocr'],
    'historical_newspaper': ['pdf-text', 'ocr', 'document-layout'],
    'rd_reports': ['pdf-text', 'ocr', 'document-layout'],
    'photo': ['visual', 'faces'],
}
MODEL_FOR = {'document-layout': 'layout', 'visual': 'visual', 'faces': 'faces'}
SPEC_KEYS = {'format', 'metadata_adopted', 'root', 'unit', 'identity_namespace', 'material',
             'output_prefix'}
OPTIONAL_KEYS = {'record_keys', 'extractors', 'models', 'languages', 'roles',
                 'surrogate_fallback', 'shard_size', 'max_pages_per_member'}


def file_kind(path):
    """Classify by signature; filenames are not proof of format."""
    with Path(path).open('rb') as handle:
        head = handle.read(8)
    if head.startswith(b'%PDF'):
        return 'pdf'
    if head[:4] in (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'):
        return 'tiff'
    if head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image'
    return None


def load_spec(value):
    require(isinstance(value, dict) and value.get('format') == FORMAT and value.get('metadata_adopted') is False,
            'Plan specification needs format ' + FORMAT + ' and metadata_adopted=false')
    require(SPEC_KEYS <= set(value) <= SPEC_KEYS | OPTIONAL_KEYS, 'Unknown or missing plan specification fields')
    require(value['material'] in MATERIALS, 'Unknown material')
    spec = {'extractors': DEFAULT_EXTRACTORS[value['material']], 'models': {}, 'languages': 'deu+eng',
            'roles': ['preservation_master'], 'surrogate_fallback': True, 'shard_size': 1000,
            'max_pages_per_member': 2000, 'record_keys': None, **value}
    allowed = set(DEFAULT_EXTRACTORS[spec['material']])
    require(spec['extractors'] and set(spec['extractors']) <= allowed and
            len(set(spec['extractors'])) == len(spec['extractors']),
            'Extractors must be a distinct subset of ' + ', '.join(sorted(allowed)))
    require(set(spec['roles']) <= set(ROLES) and spec['roles'], 'Unknown source role')
    require(type(spec['shard_size']) is int and 1 <= spec['shard_size'] <= 5000, 'Shard size outside 1–5000')
    require(type(spec['max_pages_per_member']) is int and spec['max_pages_per_member'] >= 1, 'Invalid page bound')
    require(spec['record_keys'] is None or (isinstance(spec['record_keys'], list) and
            all(isinstance(k, str) and k for k in spec['record_keys'])), 'record_keys must be a list of keys')
    models = spec['models']
    require(isinstance(models, dict) and set(models) <= {'layout', 'visual', 'faces'}, 'Unknown model binding')
    for name, model in models.items():
        require(len(Source(**model).sha256) == 64, f'Fingerprint the {name} model Source')
    for extractor in spec['extractors']:
        if extractor == 'visual':
            require('visual' in models, 'Visual extraction needs models.visual')
    prefix = spec['output_prefix']
    require(isinstance(prefix, str) and prefix and '/' not in prefix and not prefix.startswith('.'),
            'Flat output prefix required')
    return spec


def catalogue_keys(roots, root_name, unit, roles):
    """Distinct record keys in catalogue order; no keys are invented."""
    from .units import table
    rows = table(roots.root(root_name, 'read'), f'{unit}/CATALOG.csv')
    seen, keys = set(), []
    for row in rows:
        key = row.get('record_key', '')
        if key and row.get('file_role') in roles and key not in seen:
            seen.add(key); keys.append(key)
    return keys


def choose_members(members, roles, surrogate_fallback):
    chosen = [m for m in members if m.source.role in roles]
    if not chosen and surrogate_fallback:
        chosen = [m for m in members if m.source.role == 'preservation_surrogate']
    return chosen


def member_pages(roots, member, limit):
    from .preparation import inspect_page, pdf_info
    path = roots.source(member.source)
    kind = file_kind(path)
    require(kind is not None, 'Unsupported file signature')
    if kind == 'pdf':
        count = pdf_info(path, 1)[3]
        indices = range(1, count + 1)
    elif kind == 'tiff':
        from PIL import Image
        with Image.open(path) as image:
            count = getattr(image, 'n_frames', 1)
        indices = range(count)
    else:
        count, indices = 1, range(1)
    require(count <= limit, f'{count} pages exceed max_pages_per_member; plan this member separately')
    return [inspect_page(roots, member.source, member.position, kind, i) for i in indices]


def page_tasks(spec, record_key, page):
    material, models = spec['material'], spec['models']
    base = f'{record_key}/m{page.member}/{page.kind}{page.index}'
    profile = None if material == 'photo' else {'name': material, 'version': 1}
    tasks, skipped = [], []
    for extractor in spec['extractors']:
        if extractor == 'pdf-text' and page.kind != 'pdf':
            continue
        model_name = MODEL_FOR.get(extractor)
        if model_name and model_name not in models:
            skipped.append(extractor); continue
        task = {'id': f'{base}/{extractor}', 'extractor': extractor, 'page': asdict(page)}
        settings = {}
        if extractor == 'ocr':
            settings['languages'] = spec['languages']
        if model_name:
            settings['model'] = models[model_name]
        if settings:
            task['settings'] = settings
        if profile and extractor in ('pdf-text', 'ocr', 'document-layout'):
            task['ocr_profile'] = profile
        tasks.append(task)
    return tasks, skipped


def validate_recipes(tasks):
    """Build one recipe per distinct extractor/kind/settings signature.

    Recipes probe tool versions and hash language models, so checking every page
    would cost a subprocess per task. Settings are identical across pages of one
    signature; a failure here is an environment problem, not a record exclusion.
    """
    from .runner import recipe_for
    seen = set()
    for task in tasks:
        signature = (task['extractor'], task['page']['kind'], encoded(task.get('settings', {})),
                     encoded(task.get('ocr_profile')))
        if signature not in seen:
            seen.add(signature)
            recipe_for(task)
    return len(seen)


def build(roots, spec, *, validate=True):
    """Return (tasks, manifest) without writing."""
    from .units import selected_members
    keys = spec['record_keys'] or catalogue_keys(roots, spec['root'], spec['unit'], ROLES)
    tasks, excluded, skipped, pages = [], [], set(), 0
    for key in keys:
        try:
            members = selected_members(roots, spec['root'], spec['unit'], key,
                                       identity_namespace=spec['identity_namespace'])
            chosen = choose_members(members, spec['roles'], spec['surrogate_fallback'])
            require(chosen, 'No member with a selected role')
            planned = []
            for member in chosen:
                for page in member_pages(roots, member, spec['max_pages_per_member']):
                    found, missing = page_tasks(spec, key, page)
                    skipped.update(missing); planned.extend(found); pages += 1
            tasks.extend(planned)
        except (ValueError, OSError) as error:
            excluded.append({'record_key': key, 'reason': str(error)[:500]})
    signatures = validate_recipes(tasks) if validate else 0
    manifest = {'format': 'archive-research-plan-manifest-1', 'metadata_adopted': False,
                'specification': spec, 'records_considered': len(keys),
                'records_planned': len(keys) - len(excluded), 'pages': pages, 'tasks': len(tasks),
                'excluded': excluded, 'extractors_skipped_without_model': sorted(skipped),
                'recipe_signatures_validated': signatures,
                'limitations': ['Coverage is limited to catalogue records with resolved members.',
                                'Planned tasks are not extraction results and certify no accuracy.']}
    return tasks, manifest


def write_plans(roots, spec, *, work='artifacts', validate=True):
    spec = load_spec(spec)
    root = roots.root(work, 'work')
    tasks, manifest = build(roots, spec, validate=validate)
    require(tasks, 'No eligible tasks; see the manifest exclusions')
    size = spec['shard_size']
    shards = [tasks[i:i+size] for i in range(0, len(tasks), size)]
    names = [f"{spec['output_prefix']}-{n+1:04d}.json" for n in range(len(shards))]
    manifest_name = spec['output_prefix'] + '-manifest.json'
    with lock(root):
        for name in [*names, manifest_name]:
            require(not target(root, name).exists(), 'Plan output already exists: ' + name)
        for name, shard in zip(names, shards):
            write_json(root, name, {'format': 'archive-research-plan-1', 'metadata_adopted': False,
                                    'tasks': shard})
        manifest['shards'] = [{'name': n, 'tasks': len(s)} for n, s in zip(names, shards)]
        write_json(root, manifest_name, manifest)
    return manifest
