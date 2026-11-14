"""Bounded full-resolution coverage proof for photo candidates; inputs are read-only.

Similarity engines discover pairs. Only compatible complete rasters can pass here.
This is a preparation disposition, never permission to remove either file.
"""
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess

from PIL import Image, __version__ as pillow_version

from archive_common import atomic_write, elements_guard, plain_file, require
from catalogues import compare_quality
from matching import stamp


PROFILE = 'full-raster-v1-exact-or-uint16-inverse-max1'
BASIS = 'verified_full_resolution_pixels'
CHUNK = 65536
PHOTO_PROFILE = 'retained-archive-native-rotation-inversion-v1'


def compare_archive_photo(source, target):
    """Full native raster identity, allowing orientation, polarity and ICC changes.

    Existing archive Masters stay fixed. SIFT/proxies never establish this proof.
    Extra image content and resampling that cannot be proved remain unresolved.
    libvips is the workstation's existing native-depth decoder.
    """
    import numpy as np
    paths = [Path(source), Path(target)]
    before = []
    specs = []
    try:
        for path in paths:
            elements_guard(path); plain_file(path); before.append(stamp(path))
            with Image.open(path) as im:
                supported(im.format in ('TIFF', 'JPEG'), 'Unsupported photograph format')
                if im.format == 'TIFF':
                    tags = im.tag_v2
                    supported(not any(k in tags for k in (330, 338, 37724)), 'Additional image content needs review')
                    bits = tuple(tags.get(258, (8,)))
                    channels = int(tags.get(277, 1))
                    supported(len(set(bits)) == 1 and bits[0] in (8, 16) and channels in (1, 3), 'Unsupported native samples')
                    sample = tags.get(339, (1,))
                    supported(set(sample if isinstance(sample, tuple) else (sample,)) == {1}, 'Unsupported native samples')
                    spec = (im.width, im.height, channels, bits[0])
                    for frame in range(1, im.n_frames):
                        im.seek(frame)
                        supported(im.tag_v2.get(254, 0) == 1 and max(im.size) <= 512,
                                  'Additional image content needs review')
                else:
                    supported(im.mode in ('RGB', 'L') and not im.info.get('mp'), 'Additional JPEG image content needs review')
                    spec = (im.width, im.height, 3 if im.mode == 'RGB' else 1, 8)
                specs.append(spec)
        aw, ah, bands, bits = specs[0]
        bw, bh, other_bands, other_bits = specs[1]
        if sorted((aw, ah)) != sorted((bw, bh)):
            return {'status': 'unsupported', 'reason': 'Different raster dimensions; identity needs confirmation'}
        supported((bands, bits) == (other_bands, other_bits), 'Different native sample layouts')
        arrays = []
        for path, (w, h, channels, depth) in zip(paths, specs):
            decoded = subprocess.run(['vips', 'rawsave', str(path), '/dev/stdout'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
            supported(decoded.returncode == 0, 'Native decode failed: ' + decoded.stderr.decode(errors='replace')[-400:])
            supported(len(decoded.stdout) == w * h * channels * depth // 8, 'Unexpected decoded raster size')
            arrays.append(np.frombuffer(decoded.stdout, dtype=np.uint16 if depth == 16 else np.uint8).reshape(h, w, channels))
        a, b = arrays
        limit = (1 << bits) - 1
        for mirror in (False, True):
            for rotation in range(4):
                transformed = np.rot90(np.fliplr(a) if mirror else a, rotation)
                if transformed.shape != b.shape:
                    continue
                direct = inverse = 0
                for y in range(0, bh, 64):
                    x = transformed[y:y+64].astype(np.int32)
                    z = b[y:y+64].astype(np.int32)
                    direct = max(direct, int(np.abs(x-z).max()))
                    inverse = max(inverse, int(np.abs(limit-x-z).max()))
                    if direct and inverse > (1 if bits == 16 else 0):
                        break
                else:
                    return {'status': 'verified', 'full_raster_compared': True,
                            'basis': PHOTO_PROFILE, 'rotation_quarters': rotation, 'mirrored': mirror,
                            'inverted': bool(direct), 'max_sample_error': inverse if direct else 0,
                            'compared_samples': int(a.size), 'archive_master_unchanged': True}
        return {'status': 'not_equivalent', 'reason': 'Decoded samples differ beyond rotation and polarity'}
    except (Unsupported, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        return {'status': 'unsupported', 'reason': str(exc)}
    finally:
        for path, observed in zip(paths, before):
            require(stamp(path) == observed, 'Input changed during retained-Master photo comparison')


class Unsupported(ValueError):
    pass


def supported(condition, message):
    if not condition:
        raise Unsupported(message)


def layout(path, source=False):
    """Inspect structure without letting Pillow down-convert a 16-bit RGB TIFF."""
    with Image.open(path) as image:
        if image.format == 'JPEG':
            supported(image.mode in ('RGB', 'L'), 'JPEG colour mode needs review')
            supported(image.getexif().get(274, 1) == 1, 'Non-default orientation needs review')
            supported(not image.info.get('photoshop') and not image.info.get('mp'),
                      'JPEG auxiliary content needs review')
            return {'format': 'JPEG', 'width': image.width, 'height': image.height,
                    'channels': 3 if image.mode == 'RGB' else 1, 'bits': 8,
                    'photo': 2 if image.mode == 'RGB' else 1,
                    'icc': hashlib.sha256(image.info.get('icc_profile', b'')).hexdigest()}
        supported(image.format == 'TIFF', 'Unsupported representation')
        tags = image.tag_v2
        supported(not any(k in tags for k in (330, 338, 37724)),
                  'Subimages, masks, alpha or Photoshop layers need review')
        supported(not source or 34377 not in tags, 'Source Photoshop resources need review')
        supported(not any(k in tags for k in (301, 318, 319, 320, 340, 341)),
                  'TIFF transfer/colour tables or sample ranges need review')
        supported(tags.get(254, 0) == 0 and tags.get(255, 1) == 1,
                  'Primary image is reduced or part of a multi-page representation')
        supported(tags.get(259, 1) == 1 and tags.get(284, 1) == 1 and tags.get(274, 1) == 1
                  and tags.get(317, 1) == 1 and tags.get(266, 1) == 1,
                  'Compressed, planar, oriented or transformed TIFF needs review')
        width, height = int(tags[256]), int(tags[257])
        channels = int(tags.get(277, 1))
        bits = tuple(tags.get(258, (1,)))
        sample_format = tags.get(339, (1,))
        sample_format = (sample_format,) if isinstance(sample_format, int) else sample_format
        photo = tags.get(262)
        supported(width > 0 and height > 0 and channels in (1, 3) and
                  len(bits) == channels and len(set(bits)) == 1 and bits[0] in (8, 16) and
                  set(sample_format) == {1} and ((channels == 3 and photo == 2) or
                  (channels == 1 and photo in (0, 1))), 'Unsupported TIFF sample layout')
        offsets, counts = tuple(tags.get(273, ())), tuple(tags.get(279, ()))
        rows = int(tags.get(278, height))
        supported(rows > 0, 'Invalid TIFF strip height')
        expected = [min(rows, height - y) * width * channels * bits[0] // 8 for y in range(0, height, rows)]
        supported(counts == tuple(expected) and len(offsets) == len(counts), 'Incomplete or padded TIFF strips')
        size = Path(path).stat().st_size
        regions = sorted(zip(offsets, counts))
        supported(all(o >= 8 and n > 0 and o + n <= size for o, n in regions) and
                  all(a + n <= b for (a, n), (b, _) in zip(regions, regions[1:])),
                  'Invalid or overlapping TIFF strips')
        result = {'format': 'TIFF', 'width': width, 'height': height, 'channels': channels,
                  'bits': bits[0], 'photo': photo, 'offsets': offsets, 'counts': counts,
                  'icc': hashlib.sha256(tags.get(34675, b'')).hexdigest()}
        # Only explicitly reduced thumbnails are ignorable. An ordinary extra page
        # may contain another photograph, even if it is smaller than page zero.
        thumbnails = 0
        for frame in range(1, image.n_frames):
            image.seek(frame)
            t = image.tag_v2
            supported(t.get(254, 0) == 1 and int(t[256]) < width and int(t[257]) < height
                      and max(int(t[256]), int(t[257])) <= 512 and
                      not any(k in t for k in (330, 338, 37724)),
                      'Additional image content needs review')
            thumbnails += 1
        result['reduced_thumbnails'] = thumbnails
    with Path(path).open('rb') as handle:
        header = handle.read(4)
    supported(header in (b'II*\x00', b'MM\x00*'), 'BigTIFF or unknown TIFF byte order needs review')
    result['byte_order'] = '<' if header[:2] == b'II' else '>'
    return result


def samples(path, info, stack, np):
    """Yield uniform sample chunks irrespective of TIFF strip boundaries."""
    if info['format'] == 'JPEG':
        image = stack.enter_context(Image.open(path))
        values = np.asarray(image).reshape(-1)
        for start in range(0, values.size, CHUNK):
            yield values[start:start + CHUNK].astype(np.int32)
        return
    handle = stack.enter_context(Path(path).open('rb'))
    unit = info['bits'] // 8
    dtype = info['byte_order'] + ('u2' if unit == 2 else 'u1')
    pending = b''
    for offset, count in zip(info['offsets'], info['counts']):
        handle.seek(offset)
        remaining = count
        while remaining:
            amount = min(CHUNK * unit - len(pending), remaining)
            data = handle.read(amount)
            supported(len(data) == amount, 'Truncated TIFF raster')
            pending += data
            remaining -= amount
            if len(pending) == CHUNK * unit:
                yield np.frombuffer(pending, dtype=dtype).astype(np.int32)
                pending = b''
    if pending:
        yield np.frombuffer(pending, dtype=dtype).astype(np.int32)


def compare_pixels(source, target):
    """Return proof or an explicit unresolved/non-equivalent outcome; never write."""
    import numpy as np
    try:
        a, b = layout(source, source=True), layout(target)
        contract = ('width', 'height', 'channels', 'bits', 'photo', 'icc')
        if any(a[k] != b[k] for k in contract):
            return {'status': 'incompatible', 'reason': 'Geometry, native samples or colour profile differ'}
        total = a['width'] * a['height'] * a['channels']
        compared, direct, inverse, inverse_sum = 0, 0, 0, 0
        low, high = 65535, 0
        with ExitStack() as stack:
            left, right = samples(source, a, stack, np), samples(target, b, stack, np)
            for x, y in zip(left, right, strict=True):
                supported(x.size == y.size, 'Raster sample counts differ')
                compared += int(x.size)
                direct = max(direct, int(np.abs(x - y).max()))
                delta = np.abs(65535 - x - y) if a['bits'] == 16 else None
                inverse = max(inverse, int(delta.max())) if delta is not None else 65535
                inverse_sum += int(delta.sum()) if delta is not None else 0
                low, high = min(low, int(x.min())), max(high, int(x.max()))
                if direct and inverse > 1:
                    return {'status': 'not_equivalent', 'compared_samples': compared,
                            'total_samples': total, 'full_raster_compared': compared == total}
        supported(compared == total, 'Incomplete full raster comparison')
        if direct and high - low < 256:
            return {'status': 'unsupported', 'reason': 'Insufficient tonal range for inversion proof'}
        return {'status': 'verified', 'transform': 'direct' if direct == 0 else 'inverted_uint16',
                'max_sample_error': direct if direct == 0 else inverse,
                'mean_sample_error': 0 if direct == 0 else inverse_sum / total,
                'compared_samples': compared, 'total_samples': total, 'full_raster_compared': True,
                'width': a['width'], 'height': a['height'], 'bits': a['bits'], 'channels': a['channels'],
                'source_reduced_thumbnails': a.get('reduced_thumbnails', 0)}
    except (Unsupported, OSError, ValueError, TypeError, KeyError, SyntaxError, Image.DecompressionBombError) as exc:
        return {'status': 'unsupported', 'reason': str(exc)}


def verify_coverage(groups, targets, matches, evidence, root, reused, progress, fixed_archive=False):
    """Check candidate Masters, including all members of an existing source group."""
    try:
        import numpy as np
    except ImportError:
        return {'enabled': True, 'available': False, 'reason': 'NumPy is unavailable; coverage remains unresolved'}
    profile = {'algorithm': PHOTO_PROFILE if fixed_archive else PROFILE, 'numpy': np.__version__, 'pillow': pillow_version}
    cache = {}
    sources_by_path = {s['path']: s for group in groups for s in group}
    by_id = {t['id']: t for t in targets}
    for directory in reused:
        path = directory / 'pixel-coverage.jsonl' if directory.is_dir() else None
        if path and path.exists():
            plain_file(path)
            evidence.input_stamps[str(path)] = stamp(path)
            with path.open() as handle:
                for line in handle:
                    row = json.loads(line)
                    if row.get('profile') == profile:
                        cache[row['key']] = row
                    # Skipped covered cases are absent from the old workbench
                    # manifest. Retain their stamped pair as a discovery lead too.
                    old = row.get('inputs', {})
                    source, target = sources_by_path.get(old.get('source')), by_id.get(old.get('target_id'))
                    if source and target and source['stamp'] == old.get('source_stamp') and \
                            target['path'] == old.get('target') and target['stamp'] == old.get('target_stamp') and \
                            not any(m['target_id'] == target['id'] for m in matches[source['path']]):
                        matches[source['path']].append({'engine': 'prior_similarity',
                            'target_id': target['id'], 'basis': 'previous_pixel_candidate'})
    rows, counts, verified_masters = [], Counter(), {}
    progress('Checking full-resolution pixel coverage of candidate archive Masters')
    for group in groups:
        tids = sorted({m['target_id'] for s in group for m in matches[s['path']] if m['target_id'] in by_id})
        for source in group:
            for tid in tids:
                path = source['path']; target = by_id[tid]
                if not source['eligible'] or (path, tid) in evidence.rejected or any(
                    m['engine'] == 'confirmed' and m['target_id'] == tid for m in matches[path]):
                    continue
                for item in (source, target):
                    elements_guard(Path(item['path']))
                    plain_file(Path(item['path']))
                    require(stamp(item['path']) == item['stamp'], 'Pixel coverage input changed')
                key_data = {'source': path, 'source_stamp': source['stamp'], 'target': target['path'],
                            'target_stamp': target['stamp'], 'target_id': tid,
                            'catalogue_sha256': target['sha256'], 'profile': profile}
                key = hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
                row = cache.get(key)
                if row and row.get('inputs') == key_data:
                    row = dict(row); counts['reused_pairs'] += 1
                else:
                    row = {'key': key, 'inputs': key_data, 'profile': profile,
                           'result': compare_archive_photo(path, target['path']) if fixed_archive else compare_pixels(path, target['path'])}
                    counts['compared_pairs'] += 1
                result = row['result']
                if result['status'] == 'verified':
                    # The full raster and the current catalogued Master are separate
                    # proofs. A successful prior pair already contains both, stamped.
                    master_sha = row.get('verified_master_sha256') or verified_masters.get(target['path'])
                    if not master_sha:
                        with Path(target['path']).open('rb') as handle:
                            master_sha = hashlib.file_digest(handle, 'sha256').hexdigest()
                    require(not target['sha256'] or master_sha == target['sha256'],
                            f'Stored archive hash disagrees with bytes: {target["path"]}')
                    row['verified_master_sha256'] = master_sha
                    verified_masters[target['path']] = master_sha
                    matches[path].append({'engine': 'confirmed', 'target_id': tid, 'basis': BASIS,
                        'adequacy_confirmed': compare_quality(source, target) == 'covered',
                        'pixel_coverage_key': key, 'pixel_coverage': result})
                for item in (source, target):
                    require(stamp(item['path']) == item['stamp'], 'File changed during pixel coverage check')
                matches[path].append({'engine': 'pixel_coverage', 'target_id': tid, **result,
                                      'profile': profile, 'cache_key': key})
                rows.append(row); counts[result['status']] += 1
                if len(rows) % 100 == 0:
                    progress(f'Full-resolution coverage: {len(rows)} pairs, {counts["verified"]} verified')
    atomic_write(root / 'pixel-coverage.jsonl', b''.join(
        json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in rows))
    return {'enabled': True, 'available': True, 'profile': profile, **counts,
            'candidate_pairs_only': True, 'deletion_authorized': False}
