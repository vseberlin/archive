"""Read archive catalogues into one photo-Master contract. Never write to a unit."""
from collections import Counter, defaultdict
import json
from pathlib import Path

from archive_common import elements_guard, plain_file, require
from matching import catalogue_metadata, csv_rows, stamp

PHOTO_EXTENSIONS = {'.tif', '.tiff', '.jpg', '.jpeg'}
DISCOVERY_SCOPE = 'new_objects_and_tiff_for_jpeg'


def md_identity(value):
    key = str(value or '').rsplit('#', 1)[-1]
    return key.startswith('md:') and key[3:].isdigit()


def genuine_tiff(item):
    path = item.get('path') or item['members'][0]['path']
    return (Path(path).suffix.lower() in ('.tif', '.tiff') and
            item.get('eligible', True) and item.get('complete', True) and
            not item.get('technical', item).get('is_surrogate'))


def ersatz_jpeg(item):
    """A catalogued md JPEG substitute, not an arbitrary JPEG photograph."""
    identity = item.get('reference_object_id', item.get('id', ''))
    path = item.get('path') or item['members'][0]['path']
    if not md_identity(identity) or Path(path).suffix.lower() not in ('.jpg', '.jpeg'):
        return False
    row = item.get('source_record', item.get('row', {}))
    bases = {row.get(k, '') for k in ('object_selection_basis', 'representation_basis', 'basis')}
    return bool(item.get('technical', item).get('is_surrogate') or bases.intersection({
        'curator_confirmed_unreadable_master_replacement', 'curated_5000px_jpeg'}))


def digest(value):
    value = str(value or '').removeprefix('sha256:').lower()
    require(not value or len(value) == 64 and all(c in '0123456789abcdef' for c in value), 'Invalid catalogue SHA-256')
    return value


def unit_path(unit, value):
    path = Path(value)
    path = path if path.is_absolute() else unit / path
    require(path.is_relative_to(unit) and path == path.resolve(), f'Archive member escapes its unit: {path}')
    plain_file(path)
    return path


def dimensions(path, feature=None, row=None):
    from PIL import Image
    feature, row = feature or {}, row or {}
    width, height = row.get('width'), row.get('height')
    if not width or not height:
        width, height = feature.get('width'), feature.get('height')
    try:
        if int(width or 0) > 0 and int(height or 0) > 0:
            return int(width), int(height)
        with Image.open(path) as image:
            # Pillow can report orientation-adjusted TIFF geometry; pixel count is unchanged.
            return image.size
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None, None


def quality(item):
    width, height = item.get('width'), item.get('height')
    rank = 2 if item.get('is_surrogate') else 0 if Path(item['path']).suffix.lower() in ('.tif', '.tiff') else 1
    return rank, -(width * height if width and height else 0)


def compare_quality(source, retained):
    """Known same photograph only. This is not an identity test or an upsampling detector."""
    a, b = quality(source), quality(retained)
    if a[0] != b[0]:
        return 'upgrade' if a[0] < b[0] else 'covered'
    if not a[1] or not b[1]:
        return 'unknown'
    return 'upgrade' if a[1] < b[1] else 'covered'


def _normalized_rows(document):
    rows = []
    for obj in document['objects']:
        master = obj['master']
        require('members' not in master, 'Ordered multi-member Masters need a representation-specific adapter')
        row = {'record_key': obj['record_key'], 'inventory': obj.get('inventory', obj['record_key']),
               'title': obj.get('title', ''), 'file_role': 'preservation_master',
               'final_relative_path': master['path'], 'master_relative_path': master['path'],
               'size_bytes': master.get('size_bytes', ''), 'final_sha256': master.get('sha256', ''),
               'width': master.get('width', ''), 'height': master.get('height', ''),
               'original_source_path': master.get('original_source_path', ''),
               'source_sha256': master.get('source_sha256', ''), 'basis': master.get('basis', ''),
               '_metadata': obj.get('metadata', {}), '_surrogate': master.get('is_surrogate', False)}
        rows.append(row)
        for derivative in obj.get('derivatives', []):
            rows.append({**row, 'file_role': 'access_derivative', 'final_relative_path': derivative['path'],
                         'size_bytes': derivative.get('size_bytes', ''), 'final_sha256': derivative.get('sha256', ''),
                         'derivative_of': master['path']})
    return rows


def read_unit(spec, index, extensions=PHOTO_EXTENSIONS):
    """CSV layouts: common accession, inline representation fields, selected-path catalogue.

    Other layouts can supply archive-tools-unit-1 JSON outside the protected unit.
    Namespaces use canonical unit paths, never bare record keys shared by collections.
    """
    spec = Path(spec).absolute()
    elements_guard(spec)
    require(spec.resolve() == spec, 'Use canonical archive paths')
    inputs = {}
    if spec.is_file():
        document = json.loads(spec.read_text())
        require(document.get('format') == 'archive-tools-unit-1', 'Unsupported normalized unit manifest')
        unit = Path(document['unit_root'])
        require(unit.is_absolute() and unit.resolve() == unit and unit.is_dir(), 'Invalid unit_root')
        rows = _normalized_rows(document)
        catalogue, adapter = spec, 'normalized-unit-1'
    else:
        unit, catalogue = spec, spec / 'CATALOG.csv'
        rows = csv_rows(catalogue)
        require(rows, f'Empty catalogue: {catalogue}')
        if {'record_key', 'file_role', 'final_relative_path'} <= rows[0].keys():
            adapter = 'accession-catalogue'
        elif {'archival_object_id', 'selected_master_path'} <= rows[0].keys():
            adapter = 'selected-path-catalogue'
            converted = []
            for old in rows:
                if not old['selected_master_path']:
                    continue
                row = {**old, 'record_key': old['archival_object_id'], 'file_role': 'preservation_master',
                       'final_relative_path': old['selected_master_path'], 'master_relative_path': old['selected_master_path']}
                converted.append(row)
                if old.get('access_derivative_path'):
                    converted.append({**row, 'file_role': 'access_derivative',
                                      'final_relative_path': old['access_derivative_path'],
                                      'derivative_of': old['selected_master_path']})
            rows = converted
        else:
            raise ValueError(f'{catalogue}: unsupported layout; supply an archive-tools-unit-1 manifest')
    elements_guard(unit)
    inputs[str(catalogue)] = stamp(catalogue)
    representations = {}
    representation_file = unit / 'db-restore/manifests/representations.csv'
    if representation_file.is_file():
        inputs[str(representation_file)] = stamp(representation_file)
        for row in csv_rows(representation_file):
            # Paragon's manifest describes access derivation; its selected role remains in CATALOG.
            if row.get('final_relative_path') and row.get('role') in ('preservation_master', 'preservation_surrogate'):
                representations[row['final_relative_path']] = row
    master_rows = [r for r in rows if r.get('file_role') in ('preservation_master', 'preservation_surrogate')]
    exclusions = Counter(Path(r['final_relative_path']).suffix.lower() or '(no extension)'
                         for r in master_rows if Path(r['final_relative_path']).suffix.lower() not in extensions)
    selected = [r for r in master_rows if Path(r['final_relative_path']).suffix.lower() in extensions]
    counts = Counter(r['record_key'] for r in selected)
    require(all(n == 1 for n in counts.values()),
            f'{unit}: multiple selected photo members per object require an ordered-representation adapter')
    derivatives = defaultdict(list)
    for row in rows:
        if row.get('file_role') == 'access_derivative' and row.get('master_relative_path'):
            link = row.get('derivative_of', '')
            if not link or link == row['master_relative_path']:
                derivatives[row['master_relative_path']].append(row)
    targets = []
    guarded = set()
    for row in selected:
        path = unit_path(unit, row['final_relative_path'])
        current = stamp(path)
        require(not row.get('size_bytes') or current['size_bytes'] == int(row['size_bytes']), f'Catalogue size changed: {path}')
        final_hash = digest(row.get('final_sha256'))
        representation = representations.get(row['final_relative_path'], {})
        original = row.get('original_source_path', '') or representation.get('source_path', '')
        original_hash = digest(row.get('source_sha256') or row.get('representation_source_sha256') or representation.get('source_sha256'))
        basis = representation.get('basis') or row.get('representation_basis') or row.get('basis', '')
        representation_hash = digest(representation.get('final_sha256') or row.get('representation_final_sha256'))
        unchanged = bool(final_hash and original_hash and 'pixels_and_structure_unchanged' in basis and
                         (not representation_hash or representation_hash == final_hash) and
                         (not representation.get('source_path') or representation['source_path'] == original))
        feature, feature_basis = index.get(path, current), 'current_master'
        original_stamp = None
        if original and Path(original).is_absolute():
            original_path = Path(original)
            key = tuple(original_path.parts[:4])
            if key not in guarded:
                elements_guard(original_path); guarded.add(key)
            if original_path.is_file() and original_path.resolve() == original_path:
                original_stamp = stamp(original_path)
                if not feature.get('hashes') and unchanged:
                    feature = index.get(original_path, original_stamp)
                    feature_basis = 'catalogued_unchanged_pixel_source_of_master'
        aliases, preview_path, preview_stamp = [], None, None
        for access in derivatives[row['final_relative_path']]:
            derivative = unit_path(unit, access['final_relative_path'])
            observed = stamp(derivative)
            require(not access.get('size_bytes') or observed['size_bytes'] == int(access['size_bytes']),
                    f'Derivative size differs from catalogue: {derivative}')
            aliases.append({'path': str(derivative), 'stamp': observed, 'sha256': digest(access.get('final_sha256')),
                            'basis': 'catalogued_derivative_of_selected_master'})
            if preview_path is None and derivative.suffix.lower() in ('.jpg', '.jpeg'):
                preview_path, preview_stamp = str(derivative), observed
        width, height = dimensions(path, feature, row)
        object_id = str(unit) + '#' + row['record_key']
        targets.append({'id': object_id, 'record_key': row['record_key'], 'unit': str(unit),
                        'path': str(path), 'stamp': current, 'sha256': final_hash,
                        'feature': feature, 'feature_basis': feature_basis,
                        'preview_path': preview_path, 'preview_stamp': preview_stamp, 'aliases': aliases,
                        'original_path': original, 'original_stamp': original_stamp,
                        'original_sha256': original_hash, 'original_pixels_unchanged': unchanged,
                        'width': width, 'height': height,
                        'is_surrogate': row.get('file_role') == 'preservation_surrogate' or bool(row.get('_surrogate')),
                        'row': row, 'metadata': row.get('_metadata', catalogue_metadata(row, catalogue))})
    disposition_files = [p for p in (unit / '00_MANIFESTS/source-disposition.csv',
                                      unit / 'db-restore/manifests/source-disposition.csv') if p.is_file()]
    for p in disposition_files:
        inputs[str(p)] = stamp(p)
    return targets, {'unit': str(unit), 'adapter': adapter, 'photo_masters': len(targets),
                     'excluded_master_formats': dict(exclusions), 'input_stamps': inputs,
                     'disposition_files': [str(p) for p in disposition_files],
                     'completion_receipts': [str(p) for base in ('db-restore', '00_MANIFESTS')
                                             for p in (unit / base).glob('*completion.json')]}


class EmptyIndex:
    profiles = {}

    def get(self, path, current):
        return {'path': str(path), 'stamp': current, 'status': 'index_not_supplied', 'error': ''}
