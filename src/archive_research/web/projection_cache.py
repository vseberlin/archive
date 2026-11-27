"""Read-only reuse of the configured, verified search projection."""
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property, lru_cache
import json
import os
from pathlib import Path
import stat
from threading import RLock
from time import time_ns
from types import MappingProxyType

from django.conf import settings

from archive_research.common import ReviewError, require, work_root
from archive_research.search_projection import catalogue_lookup, validate
from archive_research.sources import Roots
from archive_research.storage import target


_read_lock = RLock()
# A freshly written file may still share a filesystem timestamp tick with a second write.
_SETTLE_NS = 1_000_000_000


class ProjectionChanged(ReviewError):
    """A concurrent replacement invalidated this read. The next read can retry."""


def _check_stamp_matches(matches):
    if not matches:
        raise ProjectionChanged('Search projection changed while reading. Run the search again.')


def _file_stamp(info):
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe existing output')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _root_identity(root):
    info = root.stat()
    return info.st_dev, info.st_ino


def _source_binding(source, *, default_root=None):
    return (source.get('root', default_root), source.get('path'),
            source.get('size_bytes'), source.get('mtime_ns'))


@dataclass(frozen=True)
class CaseIndex:
    catalogue: MappingProxyType
    text: MappingProxyType

    def prepared_entries(self, view):
        source = _source_binding(view['source'], default_root='sample')
        return tuple(entry for entry in self.text.get(source, ())
                     if entry['evidence']['page']['index'] ==
                     (view['page_sequence'] if entry['evidence']['page']['kind'] == 'pdf' else 0))


def _case_index(value):
    text = defaultdict(list)
    for entry in value['entries']:
        if (entry['kind'] != 'extraction' or
                entry.get('extractor') not in ('ocr', 'pdf-text', 'document-text', 'handwriting') or
                entry['availability'] != 'available'):
            continue
        evidence = entry['evidence']
        if evidence.get('region') is None and entry['fields']['text'].strip():
            text[_source_binding(evidence['page']['source'])].append(entry)
    return CaseIndex(MappingProxyType({key: tuple(entries) for key, entries in catalogue_lookup(value).items()}),
                     MappingProxyType({key: tuple(entries) for key, entries in text.items()}))


class _VerifiedProjection(dict):
    """Shared by read-only consumers. Derived projections use copy-on-write."""

    @cached_property
    def case_index(self):
        return _case_index(self)


def case_index(value):
    if isinstance(value, _VerifiedProjection):
        return value.case_index
    return _case_index(value)


def _read(path, file_stamp):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding='utf-8') as handle:
        opened = os.fstat(handle.fileno())
        _check_stamp_matches(opened.st_nlink != 0 and _file_stamp(opened) == file_stamp)
        value = validate(json.load(handle))
        opened = os.fstat(handle.fileno())
        _check_stamp_matches(opened.st_nlink != 0 and _file_stamp(opened) == file_stamp)
    return _VerifiedProjection(value)


@lru_cache(maxsize=1)
def _load(path, root_identity, file_stamp):
    return _read(path, file_stamp)


def base_search_projection():
    """Return freshly checked roots and their read-only on-disk projection."""
    config = Path(settings.PROJECT['extraction_roots'])
    work_root(config.parent)
    roots = Roots.from_toml(config)
    root = roots.root('artifacts', 'work')
    name = settings.PROJECT['research_index']
    with _read_lock:
        path = target(root, name)
        root_identity = _root_identity(root)
        file_stamp = _file_stamp(path.stat())
        if time_ns() - file_stamp[-1] < _SETTLE_NS:
            value = _read(path, file_stamp)
        else:
            value = _load(path, root_identity, file_stamp)
        _check_stamp_matches(target(root, name) == path and _root_identity(root) == root_identity and
                             _file_stamp(path.stat()) == file_stamp)
    return roots, value
