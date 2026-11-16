"""Optional SIFT/geometry evidence for existing search leads, never identity authority."""
from collections import Counter
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

from PIL import __version__ as pillow_version

from archive_common import atomic_write, elements_guard, plain_file, require
from matching import comparison_buffer, stamp


DEFAULTS = {'proxy_edge': 1600, 'max_features': 2000, 'ratio': 0.7,
            'min_inliers': 12, 'min_inlier_ratio': 0.5, 'reprojection': 4.0,
            'min_coverage': 0.15, 'min_other_coverage': 0.01,
            'variants': ['direct', 'inverted', 'mirrored', 'mirrored_inverted']}


def load_profile(args):
    options = ('proxy_edge', 'ratio', 'min_inliers', 'min_inlier_ratio')
    supplied = {name: getattr(args, 'sift_' + name, None) for name in options
                if getattr(args, 'sift_' + name, None) is not None}
    require(getattr(args, 'sift', False) or not supplied, 'Supply --sift with SIFT settings')
    if not getattr(args, 'sift', False):
        return None
    profile = {**DEFAULTS, **supplied, 'format': 'archive-tools-sift-1'}
    require(type(profile['proxy_edge']) is int and 256 <= profile['proxy_edge'] <= 4096,
            'SIFT proxy edge must be 256–4096 pixels')
    require(0.1 <= profile['ratio'] < 1, 'SIFT ratio must be at least 0.1 and below 1')
    require(type(profile['min_inliers']) is int and 8 <= profile['min_inliers'] <= 2000,
            'SIFT minimum inliers must be 8–2000')
    require(0.1 <= profile['min_inlier_ratio'] <= 1, 'SIFT minimum inlier ratio must be 0.1–1')
    return profile


def runtime():
    # Probe native libraries out of process so missing/incompatible wheels produce
    # a normal preparation failure, even when a native import raises SIGILL.
    code = ('import cv2 as cv, numpy as np; '
            'cv.SIFT_create().detectAndCompute(np.zeros((32,32),np.uint8),None); '
            'print(cv.__version__)')
    probe = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=30)
    require(probe.returncode == 0,
            f'SIFT runtime check failed (exit {probe.returncode}); use a compatible OpenCV environment. '
            + probe.stderr[-1200:])
    import cv2
    import numpy
    return cv2, numpy


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Verifier:
    def __init__(self, root, prior, profile, libraries, threads=2):
        self.cv, self.np = libraries
        self.cv.setNumThreads(threads)
        self.profile = profile
        self.root = Path(root)
        self.directory = self.root / 'sift-cache'
        require(not self.directory.is_symlink(), 'SIFT cache must not be a symlink')
        self.directory.mkdir(exist_ok=True)
        self.prior = [Path(p) for p in prior if Path(p).is_dir() and Path(p) != self.root]
        self.extractor = {'format': 'primary-gray-sift-1', 'opencv': self.cv.__version__,
                          'numpy': self.np.__version__, 'pillow': pillow_version, 'proxy_edge': profile['proxy_edge'],
                          'max_features': profile['max_features']}
        self.detector = self.cv.SIFT_create(nfeatures=profile['max_features'])
        self.counts = Counter()
        self.results = {}
        for directory in [*self.prior, self.root]:
            path = directory / 'sift-pairs.jsonl'
            if path.exists():
                plain_file(path)
                with path.open() as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                            if row['result']['status'] != 'unavailable':
                                self.results[row['key']] = row['result']
                        except (ValueError, KeyError, TypeError):
                            continue  # Interrupted cache append is recomputed.
        self.output = plain_file(self.root / 'sift-pairs.jsonl')

    @lru_cache(maxsize=16)
    def features(self, value, size_bytes, mtime_ns, variant):
        path = Path(value)
        observed = {'size_bytes': size_bytes, 'mtime_ns': mtime_ns}
        elements_guard(path)
        require(path.resolve() == path, 'SIFT inputs must be canonical')
        require(stamp(path) == observed, f'SIFT input changed: {path}')
        meta = {'path': value, 'stamp': observed, 'variant': variant, **self.extractor}
        key = signature(meta)
        for directory in [self.root, *self.prior]:
            cache = directory / 'sift-cache' / (key + '.npz')
            if not cache.exists():
                continue
            require(cache.parent == cache.parent.resolve(), 'SIFT cache directory must be canonical')
            plain_file(cache)
            try:
                with self.np.load(cache, allow_pickle=False) as data:
                    if json.loads(str(data['metadata'])) != meta:
                        continue
                    points, descriptors, shape = data['points'], data['descriptors'], data['shape']
                    if (points.ndim != 2 or points.shape[1] != 2 or descriptors.shape != (len(points), 128)
                            or points.dtype != self.np.float32 or descriptors.dtype != self.np.float32
                            or shape.shape != (2,) or not self.np.isfinite(points).all()
                            or not self.np.isfinite(descriptors).all()):
                        continue
                    self.counts['features_reused'] += 1
                    return points, descriptors, shape
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
        image, _ = comparison_buffer(path, self.profile['proxy_edge'], prefer_thumbnail=False)
        gray = self.np.asarray(image.convert('L'))
        if 'mirrored' in variant:
            gray = self.np.fliplr(gray)
        if 'inverted' in variant:
            gray = 255 - gray
        gray = self.np.ascontiguousarray(gray)
        keypoints, descriptors = self.detector.detectAndCompute(gray, None)
        points = self.np.array([k.pt for k in keypoints], dtype=self.np.float32).reshape(-1, 2)
        if descriptors is None:
            descriptors = self.np.empty((0, 128), dtype=self.np.float32)
        require(stamp(path) == observed, f'SIFT input changed during extraction: {path}')
        shape = self.np.array(gray.shape)
        buffer = io.BytesIO()
        self.np.savez_compressed(buffer, metadata=json.dumps(meta, sort_keys=True),
                                 points=points, descriptors=descriptors, shape=shape)
        atomic_write(self.directory / (key + '.npz'), buffer.getvalue())
        self.counts['features_extracted'] += 1
        return points, descriptors, shape

    def compare(self, source, target):
        cv, np, profile = self.cv, self.np, self.profile
        sp, sd, ss = source
        tp, td, ts = target
        result = {'status': 'insufficient_features', 'source_keypoints': len(sp), 'target_keypoints': len(tp),
                  'source_preview_size': [int(ss[1]), int(ss[0])], 'target_preview_size': [int(ts[1]), int(ts[0])],
                  'matches': 0, 'inliers': 0, 'inlier_ratio': 0.0,
                  'source_coverage': 0.0, 'target_coverage': 0.0}
        if min(len(sp), len(tp)) < profile['min_inliers']:
            return result
        cv.setRNGSeed(0)
        matcher = cv.FlannBasedMatcher(dict(algorithm=1, trees=5), dict(checks=64))
        def nearest(a, b):
            return {m.queryIdx: m.trainIdx for pair in matcher.knnMatch(a, b, k=2)
                    if len(pair) == 2 for m, n in [pair] if m.distance < profile['ratio'] * n.distance}
        forward, reverse = nearest(sd, td), nearest(td, sd)
        matches, used_s, used_t = [], set(), set()
        for a, b in forward.items():
            sa, tb = tuple(np.round(sp[a]).astype(int)), tuple(np.round(tp[b]).astype(int))
            if reverse.get(b) == a and sa not in used_s and tb not in used_t:
                matches.append((a, b)); used_s.add(sa); used_t.add(tb)
        result.update(status='not_supported', matches=len(matches))
        if len(matches) < profile['min_inliers']:
            return result
        a = np.float32([sp[i] for i, _ in matches])
        b = np.float32([tp[j] for _, j in matches])
        matrix, mask = cv.findHomography(a, b, cv.RANSAC, profile['reprojection'], maxIters=3000, confidence=0.995)
        if matrix is None or mask is None or not np.isfinite(matrix).all():
            return result
        good = mask.ravel().astype(bool)
        n = int(good.sum())
        def coverage(points, shape):
            return float(cv.contourArea(cv.convexHull(points))) / float(np.prod(shape)) if len(points) >= 3 else 0.0
        sc, tc = coverage(a[good], ss), coverage(b[good], ts)
        result.update(inliers=n, inlier_ratio=n / len(matches), source_coverage=sc, target_coverage=tc)
        try:
            inverse = np.linalg.inv(matrix)
            mapped_a = cv.perspectiveTransform(a[good, None], matrix)[:, 0]
            mapped_b = cv.perspectiveTransform(b[good, None], inverse)[:, 0]
            error = float(max(np.median(np.linalg.norm(mapped_a - b[good], axis=1)),
                              np.median(np.linalg.norm(mapped_b - a[good], axis=1)))) if n else float('inf')
        except np.linalg.LinAlgError:
            return result
        if np.isfinite(error):
            result.update(median_symmetric_error=error, homography=matrix.tolist())
        if (n >= profile['min_inliers'] and result['inlier_ratio'] >= profile['min_inlier_ratio']
                and max(sc, tc) >= profile['min_coverage'] and min(sc, tc) >= profile['min_other_coverage']
                and error <= profile['reprojection']):
            result['status'] = 'supported'
        return result

    def provenance(self, source, target):
        return {'source_path': source['path'], 'source_stamp': source['stamp'],
                'target_path': target['path'], 'target_stamp': target['stamp'],
                'extractor': self.extractor, 'profile': self.profile}

    def pair(self, source, target):
        provenance = self.provenance(source, target)
        key = signature(provenance)
        if key in self.results:
            self.counts['pairs_reused'] += 1
            result = self.results[key]
        else:
            attempts = []
            for variant in self.profile['variants']:
                try:
                    s = self.features(source['path'], **source['stamp'], variant=variant)
                    t = self.features(target['path'], **target['stamp'], variant='direct')
                    attempt = self.compare(s, t)
                except (OSError, ValueError, self.cv.error) as error:
                    attempt = {'status': 'unavailable', 'error': str(error), 'inliers': 0, 'inlier_ratio': 0}
                attempts.append({**attempt, 'variant': variant})
                if attempt['status'] in ('supported', 'unavailable'):
                    break
            best = max(attempts, key=lambda r: (r['status'] == 'supported', r['inliers'], r['inlier_ratio']))
            if any(a['status'] == 'unavailable' for a in attempts) and best['status'] != 'supported':
                best = next(a for a in attempts if a['status'] == 'unavailable')
            result = {**best, **provenance, 'attempts': attempts, 'identity_confirmed': False}
            self.results[key] = result
            self.counts['pairs_computed'] += 1
        with self.output.open('a') as handle:
            handle.write(json.dumps({'key': key, 'result': result}, ensure_ascii=False, allow_nan=False) + '\n')
        return {'engine': 'sift', 'target_id': target['id'], **result}


def verify_matches(sources, targets, matches, evidence, root, prior, profile, libraries, threads, progress):
    by_id = {t['id']: t for t in targets}
    queue, covered_sources = [], 0
    for source in sources:
        leads = matches[source['path']]
        confirmed = {m['target_id'] for m in leads if m['engine'] == 'confirmed' and m['target_id'] in by_id
                     and (source['path'], m['target_id']) not in evidence.rejected}
        covered = any(m['engine'] == 'confirmed' and m['target_id'] in confirmed and
                      m.get('adequacy_confirmed') for m in leads)
        if covered and not evidence.issues[source['path']] and source['path'] not in evidence.proposals:
            # More tentative matches cannot make an adequately covered source new.
            # Keep every lead for review, including any separate metadata question.
            covered_sources += 1
            continue
        pairs = sorted({m['target_id'] for m in leads if m['target_id'] in by_id
                        and m['target_id'] not in confirmed
                        and (source['path'], m['target_id']) not in evidence.rejected})
        queue.extend((source, by_id[tid]) for tid in pairs)
    verifier = Verifier(root, prior, profile, libraries, threads)
    planned = len(queue)
    source_count = len({s['path'] for s, _ in queue})
    planned_new = len({signature(verifier.provenance(s, t)) for s, t in queue} - verifier.results.keys())
    progress(f'SIFT queue: {planned} candidate pairs; sources: {source_count}; '
             f'cached pairs: {planned - planned_new}; covered sources skipped: {covered_sources}')
    statuses = Counter()
    started = last_report = time.monotonic()
    computed_seconds = 0.0
    for total, (source, target) in enumerate(queue, 1):
        before = time.monotonic()
        computed = verifier.counts['pairs_computed']
        result = verifier.pair(source, target)
        now = time.monotonic()
        if verifier.counts['pairs_computed'] > computed:
            computed_seconds += now - before
        matches[source['path']].append(result)
        statuses[result['status']] += 1
        if total % 25 == 0 or total == planned or now - last_report >= 30:
            computed = verifier.counts['pairs_computed']
            remaining_new = planned_new - computed
            # Cached pairs can finish in milliseconds. Do not use those to predict
            # the cost of the still-uncomputed image comparisons.
            eta = (f'~{computed_seconds / computed * remaining_new / 60:.1f} min remaining'
                   if computed else 'cached work only' if not remaining_new else 'estimating remaining time')
            progress(f'SIFT: {total}/{planned} candidate pairs checked; '
                     f'{statuses["supported"]} geometrically supported; {eta}')
            last_report = now
    verifier.features.cache_clear()
    progress(f'SIFT completed: {planned} candidate pairs; {dict(statuses)}')
    return {'enabled': True, 'profile': profile, 'extractor': verifier.extractor, 'pairs': planned,
            'statuses': dict(statuses), 'cache': dict(verifier.counts),
            'source_files': source_count, 'planned_pairs': planned, 'planned_uncached_pairs': planned_new,
            'covered_sources_skipped': covered_sources, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'scope': 'existing_uncovered_candidate_pairs', 'identity_authority': False}


def rank(leads):
    """Support outranks inconclusive/failed geometry; original distances break ties."""
    sift = [m for m in leads if m['engine'] == 'sift']
    if not sift:
        return (1, 0, 0)
    return min((0 if m['status'] == 'supported' else 2 if m['status'] == 'not_supported' else 1,
                -m.get('inliers', 0) if m['status'] == 'supported' else 0,
                -m.get('inlier_ratio', 0) if m['status'] == 'supported' else 0) for m in sift)
