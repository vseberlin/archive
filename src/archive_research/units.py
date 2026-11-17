"""Read existing selected-member mappings for the bounded Trafo/FEB/Bibliothek trial."""
import csv
from dataclasses import dataclass
from pathlib import Path

from .contracts import Source, relative_path, require


@dataclass(frozen=True)
class Member:
    source: Source
    position: int
    title: str
    mapping: str


def table(root, relative):
    relative_path(relative)
    path = root / relative
    require(path.resolve() == path and path.is_file(), 'Mapping must be an existing real file')
    before = path.stat()
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    after = path.stat()
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), 'Mapping changed during read')
    return rows


def selected_members(roots, root_name, unit, record_key, *, identity_namespace):
    """No selection or sequence is invented; namespace is supplied from existing identity."""
    require(identity_namespace and record_key, 'Existing identity namespace and record key required')
    relative_path(unit)
    archive = roots.root(root_name, 'read')
    name = Path(unit).name
    manifests = {
        'trafo': 'db-restore/manifests/representations.csv',
        'bibliothek': '00_MANIFESTS/master-files.csv',
        'FEB_ Forschungs-und-Entwicklungsberichte_aus_dem_WF': '00_MANIFESTS/master-files.csv',
    }
    require(name in manifests, 'Unit mapping has not been qualified by this bounded adapter')
    catalogue = table(archive, f'{unit}/CATALOG.csv')
    selected = [r for r in catalogue if r.get('record_key') == record_key and
                r.get('file_role') in ('preservation_master', 'preservation_surrogate')]
    require(selected, 'No existing selected representation for this object')
    mapping_path = f'{unit}/{manifests[name]}'
    rows = table(archive, mapping_path)
    members = []
    for row in selected:
        location = row['final_relative_path']
        witnesses = [r for r in rows if (r.get('relative_path') or r.get('final_relative_path')) == location]
        require(len(witnesses) == 1, 'Selected member needs exactly one manifest witness')
        witness = witnesses[0]
        sequence = witness.get('sequence', '')
        require(sequence.isdigit() and int(sequence) > 0, 'Manifest member order is unresolved')
        require(not row.get('sequence') or row['sequence'] == sequence, 'Catalogue/manifest order conflict')
        path = archive / unit / location
        relative_path(location)
        require(path.resolve() == path and path.is_file(), 'Selected member missing or redirected')
        stat = path.stat()
        require(not row.get('size_bytes') or int(row['size_bytes']) == stat.st_size,
                'Selected member size differs from catalogue')
        source = Source(identity_namespace, identity_namespace + '#' + record_key,
                        row.get('master_representation_id') or witness.get('representation_id') or location,
                        root_name, f'{unit}/{location}', stat.st_size, stat.st_mtime_ns,
                        row.get('final_sha256', '').removeprefix('sha256:'), row.get('original_source_path', ''),
                        row['file_role'])
        roots.source(source)
        members.append(Member(source, int(sequence), row.get('title', ''), mapping_path))
    require(len({m.position for m in members}) == len(members), 'Ambiguous duplicate member positions')
    require(len({m.source.path for m in members}) == len(members), 'Duplicate selected member paths')
    return sorted(members, key=lambda member: member.position)
