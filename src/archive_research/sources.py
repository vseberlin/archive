"""Explicit roots and checked reads. Never create roots, mount media or repair files."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tomllib

from .contracts import Artifact, Source, integer, relative_path, require, text


ELEMENTS = Path('/media/vladimir/Elements')
VOLUME = Path('/mnt/volume')
PROTECTED = (Path('/media/vladimir'), VOLUME / 'iss-archive',
             VOLUME / 'archive', VOLUME / 'archive-intake')


def check_mount(target, source='', fstype='', options=()):
    process = subprocess.run(
        ['findmnt', '-J', '-T', str(target), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'],
        capture_output=True, text=True, timeout=5, check=False)
    require(process.returncode == 0, f'Required mount unavailable: {target}')
    mounts = json.loads(process.stdout).get('filesystems', [])
    require(len(mounts) == 1, 'Ambiguous required mount')
    mount = mounts[0]
    require(mount.get('target') == str(target), f'Required mount is absent: {target}')
    require(not source or mount.get('source') == source, 'Wrong mounted source')
    require(not fstype or mount.get('fstype') == fstype, 'Wrong mounted filesystem')
    actual = set(mount.get('options', '').split(','))
    require(set(options) <= actual, 'Required mount options missing')
    require('ro' not in options or 'rw' not in actual, 'Writable protected mount refused')


def check_location(path):
    """Apply workstation media guards even when a broader read root was configured."""
    if path.is_relative_to(ELEMENTS.parent) and len(path.parts) > 3:
        require(not path.parts[3].startswith('Elements') or path.is_relative_to(ELEMENTS),
                'Alternate Elements root refused')
    if path.is_relative_to(ELEMENTS):
        check_mount(ELEMENTS, '/dev/sdf1', 'ntfs3', ('ro',))
    if path.is_relative_to(VOLUME):
        check_mount(VOLUME)


@dataclass(frozen=True)
class Root:
    path: Path
    access: str
    mount_target: str = ''
    mount_source: str = ''
    mount_fstype: str = ''
    mount_options: tuple = ()
    min_free_bytes: int = 0

    def __post_init__(self):
        path = Path(self.path)
        require(path.is_absolute() and '..' not in path.parts, 'Root must be an absolute real path')
        object.__setattr__(self, 'path', path)
        require(self.access in ('read', 'work'), 'Root access must be read or work')
        require(integer(self.min_free_bytes), 'Invalid free-space requirement')
        require(all(isinstance(v, str) for v in (self.mount_target, self.mount_source, self.mount_fstype)),
                'Mount fields must be strings')
        require(isinstance(self.mount_options, tuple) and all(text(v) for v in self.mount_options),
                'Mount options must be an immutable sequence')
        if self.mount_target:
            mount = Path(self.mount_target)
            require(mount.is_absolute() and '..' not in mount.parts and path.is_relative_to(mount),
                    'Root must be inside its required mount')
        else:
            require(not (self.mount_source or self.mount_fstype or self.mount_options),
                    'Mount constraints require a target')
        if path.is_relative_to(VOLUME):
            require(self.mount_target == str(VOLUME), 'Volume roots require the volume mount')
        if path.is_relative_to(ELEMENTS.parent) and len(path.parts) > 3:
            require(not path.parts[3].startswith('Elements') or path.is_relative_to(ELEMENTS),
                    'Alternate Elements root refused')
        if self.access == 'work':
            require(not any(path.is_relative_to(p) or p.is_relative_to(path) for p in PROTECTED),
                    'Output root overlaps protected archive/source media')

    def checked(self):
        # Guard the protected device before resolving/stat-ing anything beneath it.
        check_location(self.path)
        if self.mount_target:
            check_mount(self.mount_target, self.mount_source, self.mount_fstype, self.mount_options)
        require(self.path.resolve() == self.path and self.path.is_dir(),
                f'Root is missing or redirected: {self.path}')
        if self.access == 'work':
            require(shutil.disk_usage(self.path).free >= self.min_free_bytes,
                    'Configured output root has insufficient free space')
        return self.path


class Roots:
    def __init__(self, roots):
        require(isinstance(roots, dict) and roots, 'Explicit root map required')
        require(all(isinstance(name, str) and re.fullmatch('[a-z][a-z0-9_]*', name)
                    and isinstance(root, Root) for name, root in roots.items()), 'Invalid named root')
        self._roots = dict(roots)
        for work in (r for r in roots.values() if r.access == 'work'):
            for read in (r for r in roots.values() if r.access == 'read'):
                require(not (work.path.is_relative_to(read.path) or read.path.is_relative_to(work.path)),
                        'Read and work roots must not overlap')

    @classmethod
    def from_toml(cls, path):
        path = Path(path)
        require(path.is_file() and path.resolve() == path.absolute(), 'Configuration must be a real file')
        with path.open('rb') as handle:
            document = tomllib.load(handle)
        require(set(document) == {'roots'}, 'Only explicit root configuration is supported')
        roots = {}
        for name, values in document['roots'].items():
            values = dict(values)
            values['mount_options'] = tuple(values.get('mount_options', ()))
            roots[name] = Root(**values)
        return cls(roots)

    def declared_root(self, name, access):
        """Identify a configured root without accessing its filesystem."""
        require(name in self._roots, f'Undeclared root: {name}')
        root = self._roots[name]
        require(root.access == access, f'Wrong root access: {name}')
        return root

    def root(self, name, access):
        return self.declared_root(name, access).checked()

    def _file(self, name, relative, access):
        relative_path(relative)
        root = self.root(name, access)
        path = root / relative
        check_location(path)
        require(path.resolve() == path, 'Symlinked or redirected member refused')
        info = path.stat()
        require(stat.S_ISREG(info.st_mode), 'Expected a regular file')
        if access == 'work':
            require(info.st_nlink == 1, 'Hard-linked output refused')
        return path, info

    def source(self, source, verify_hash=False):
        require(isinstance(source, Source), 'Expected a bound source')
        require(type(verify_hash) is bool, 'Hash verification must be explicit')
        path, info = self._file(source.root, source.path, 'read')
        require((info.st_size, info.st_mtime_ns) == (source.size_bytes, source.mtime_ns),
                f'Source changed since binding: {source.path}')
        if verify_hash:
            require(bool(source.sha256), 'No stored hash to verify')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as handle:
                opened = os.fstat(handle.fileno())
                require(path.resolve() == path and
                        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) ==
                        (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                        'Source changed before verifying hash')
                observed = hashlib.file_digest(handle, 'sha256').hexdigest()
            require(observed == source.sha256, 'Source content differs from stored hash')
            after = path.stat()
            require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) ==
                    (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns),
                    'Source changed while verifying hash')
        return path

    def artifact(self, artifact):
        require(isinstance(artifact, Artifact), 'Expected an artifact binding')
        return self._file(artifact.root, artifact.path, 'work')[0]
