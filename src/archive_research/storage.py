"""One work-area lock and atomic derived-file replacement; no database or journal."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from .contracts import encoded, relative_path, require


def target(root, name):
    relative_path(name)
    path = root / name
    require(path.parent == root and path.resolve() == path, 'Expected a flat real output file')
    if path.exists():
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe existing output')
    return path


def write(root, name, data):
    path = target(root, name)
    descriptor, temporary = tempfile.mkstemp(prefix='.extract-', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        target(root, name)
        os.replace(temporary, path)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def write_json(root, name, value):
    return write(root, name, (encoded(value) + '\n').encode())


def stamp(path):
    value = path.stat()
    return {'size_bytes': value.st_size, 'mtime_ns': value.st_mtime_ns}


@contextmanager
def lock(root):
    path = root / '.extraction.lock'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a+') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe extraction lock')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
