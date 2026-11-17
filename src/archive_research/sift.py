"""Prepared-image SIFT geometry shared by installed and legacy callers."""
import hashlib
import io
import subprocess
import sys
from .common import require

DEFAULTS = {'proxy_edge': 1600, 'max_features': 2000, 'ratio': 0.7,
            'min_inliers': 12, 'min_inlier_ratio': 0.5, 'reprojection': 4.0,
            'min_coverage': 0.15, 'min_other_coverage': 0.01,
            'variants': ['direct', 'inverted', 'mirrored', 'mirrored_inverted']}


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

def compare_features(self, source, target):
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


def compare_prepared(source_bytes, target_bytes):
    """Compare caller-verified prepared views without creating a second cache tree.

    The caller retains source/page/region and byte hashes. This bounded diagnostic
    uses direct orientation only; a failure is not evidence of different sites.
    """
    from PIL import Image
    from types import SimpleNamespace
    cv, np = runtime()
    cv.setNumThreads(2)
    profile = {**DEFAULTS, 'variants': ['direct']}
    detector = cv.SIFT_create(nfeatures=profile['max_features'])

    def features(data):
        with Image.open(io.BytesIO(data)) as image:
            gray = image.convert('L')
            gray.thumbnail((profile['proxy_edge'], profile['proxy_edge']))
            pixels = np.asarray(gray)
            keypoints, descriptors = detector.detectAndCompute(pixels, None)
            points = np.array([k.pt for k in keypoints], dtype=np.float32).reshape(-1, 2)
            if descriptors is None:
                descriptors = np.empty((0, 128), dtype=np.float32)
            return points, descriptors, np.array(pixels.shape)

    result = compare_features(SimpleNamespace(cv=cv, np=np, profile=profile),
                              features(source_bytes), features(target_bytes))
    return {**result, 'profile': profile, 'opencv': cv.__version__, 'numpy': np.__version__,
            'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
            'target_sha256': hashlib.sha256(target_bytes).hexdigest(),
            'identity_confirmed': False,
            'limitation': 'Direct-orientation geometry only; drawings, alterations and viewpoints can defeat matching.'}
