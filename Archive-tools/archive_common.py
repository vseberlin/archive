"""Small file/evidence helpers shared by the archive command-line tools."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


class ReviewError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReviewError(message)


ELEMENTS = Path('/media/vladimir/Elements')
PROTECTED = (ELEMENTS, Path('/mnt/volume/archive'),
             Path('/mnt/volume/archive-intake'), Path('/mnt/volume/iss-archive'))


def elements_guard(path):
    """Check the actual mount before inspecting protected source evidence."""
    if path.is_relative_to(ELEMENTS.parent) and len(path.parts) > 3 and path.parts[3].startswith('Elements'):
        require(path.is_relative_to(ELEMENTS), 'Alternate Elements mount is not allowed')
    if not path.is_relative_to(ELEMENTS):
        return
    result = subprocess.run(
        ['findmnt', '-J', '-T', str(ELEMENTS), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'],
        capture_output=True, text=True, timeout=5, check=False)
    require(result.returncode == 0, 'Protected Elements source is not mounted')
    mounts = json.loads(result.stdout).get('filesystems', [])
    require(len(mounts) == 1, 'Ambiguous Elements mount')
    m = mounts[0]
    require((m.get('source'), m.get('target'), m.get('fstype')) ==
            ('/dev/sdf1', str(ELEMENTS), 'ntfs3') and
            'ro' in m.get('options', '').split(',') and
            'rw' not in m.get('options', '').split(','),
            'Require /dev/sdf1 /media/vladimir/Elements ntfs3 ro')


def work_root(value, must_exist=True):
    path = Path(value).absolute()
    require(path == path.resolve(), 'Work root must be a real path without symlinks')
    require(not any(path.is_relative_to(root) for root in PROTECTED),
            'Work root is a protected archive/source')
    require(not (path.is_relative_to(ELEMENTS.parent) and
                 len(path.parts) > 3 and path.parts[3].startswith('Elements')),
            'Work root is on protected backup media')
    if must_exist:
        require(path.is_dir(), 'Work root must already exist')
    return path


def plain_file(path):
    require(not path.is_symlink(), f'Refusing symlink: {path.name}')
    require(not path.exists() or path.is_file(), f'Expected a regular file: {path.name}')
    return path


def read_bytes(path, optional=False):
    plain_file(path)
    return b'' if optional and not path.exists() else path.read_bytes()


def atomic_write(path, data):
    """Replace a single authority atomically; derived CSV can be rebuilt."""
    plain_file(path)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def file_lock(path):
    plain_file(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe lock file')
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def source_path(value, roots):
    require(isinstance(value, str) and Path(value).is_absolute(), 'Source paths must be absolute')
    path = Path(value)
    require('..' not in path.parts, 'Source paths must be canonical')
    require(any(path.is_relative_to(root) for root in roots), 'Source outside declared read roots')
    # Do not traverse an offline protected mount just to prepare an object list.
    return path


def checked_member(member, roots):
    path = source_path(member['path'], roots)
    elements_guard(path)
    require(path.resolve() == path, 'Source path changed or contains a symlink')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode), 'Source is not a regular file')
    require((info.st_size, info.st_mtime_ns) == (member['size_bytes'], member['mtime_ns']),
            f'Source changed since preparation: {path.name}')
    return path


def preview(path, page, edge, as_text=False):
    """Render a requested page in memory; no archive derivative is created."""
    if path.suffix.lower() == '.pdf':
        args = (['pdftotext', '-f', str(page), '-l', str(page), str(path), '-'] if as_text else
                ['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile',
                 '-scale-to', str(edge), '-png', str(path)])
        result = subprocess.run(args, capture_output=True, timeout=30, check=False)
        require(result.returncode == 0 and (as_text or result.stdout), 'PDF preview failed')
        return (result.stdout[:200000], 'text/plain; charset=utf-8') if as_text else (result.stdout, 'image/png')
    require(not as_text, 'Embedded text is available only for PDF files')
    if path.suffix.lower() in ('.tif', '.tiff') and page == 1:
        from matching import comparison_buffer
        image, _ = comparison_buffer(path, edge, prefer_thumbnail=False)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        return output.getvalue(), 'image/jpeg'
    from PIL import Image, ImageOps
    with Image.open(path) as image:
        require(page <= getattr(image, 'n_frames', 1), 'Page is not present in source')
        image.seek(page - 1)
        image.thumbnail((edge, edge))
        image = ImageOps.exif_transpose(image).convert('RGB')
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90)
        return output.getvalue(), 'image/jpeg'
