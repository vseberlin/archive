"""Read-only visual/metadata evidence helpers for search preparation."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import unicodedata

from archive_common import require

HASHES = ('phash-dct-64-v2', 'dhash-64-v2', 'ahash-64-v2')
FIELDS = ('inventory', 'title', 'description', 'date', 'creator', 'places', 'tags', 'rights')
TAGS = {
    'inventory': ('DocumentName', 'Identifier'),
    'title': ('Title', 'ObjectName'),
    'description': ('Description', 'ImageDescription', 'Caption-Abstract'),
    'date': ('DateCreated', 'DateTimeOriginal'),
    'creator': ('Artist', 'Creator', 'By-line'),
    'places': ('Location', 'City', 'Sub-location', 'Country'),
    'tags': ('Subject', 'Keywords'),
    'rights': ('Copyright', 'Rights', 'UsageTerms'),
}
META_PROFILE = 'exiftool-fast2-descriptive-fields-v1'


def stamp(path):
    info = Path(path).stat()
    return {'size_bytes': info.st_size, 'mtime_ns': info.st_mtime_ns}


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def normalized(value):
    return ' '.join(unicodedata.normalize('NFC', str(value)).casefold().split())


def values(metadata, field):
    return {normalized(v['value']) for v in metadata.get(field, []) if str(v.get('value', '')).strip()}


def compare_metadata(left, right, unavailable=False):
    rows = []
    for field in FIELDS:
        a, b = values(left, field), values(right, field)
        state = ('unavailable' if unavailable else 'unknown' if not a and not b else
                 'archive_only' if not a else 'source_only' if not b else 'agreement' if a == b else 'different_values')
        rows.append({'field': field, 'source': left.get(field, []), 'archive': right.get(field, []), 'status': state})
    return rows


def metadata_question(rows):
    return any(r['status'] in ('source_only', 'different_values', 'unavailable') for r in rows)


def catalogue_metadata(row, catalogue):
    mapping = {'date': 'date_text', 'rights': 'rights', **{f: f for f in FIELDS if f != 'date'}}
    out = {}
    for field, column in mapping.items():
        if row.get(column, '').strip():
            out[field] = [{'value': row[column], 'source': str(catalogue), 'field': column,
                           'record_key': row['record_key'], 'authority': row.get('metadata_authority', '')}]
    return out


def extract_metadata(paths):
    """One ExifTool process per bounded batch; no write flags or pixel payload tags."""
    command = ['exiftool', '-j', '-G1', '-s', '-fast2']
    command += ['-' + name for name in sorted({tag for group in TAGS.values() for tag in group})]
    command += ['-IFD0:ImageWidth', '-IFD0:ImageHeight', '-IFD0:Compression', '-IFD0:Orientation', '-Warning', '-Error']
    result = subprocess.run(command + [str(p) for p in paths], capture_output=True, text=True, timeout=180)
    try:
        records = {r['SourceFile']: r for r in json.loads(result.stdout)}
    except (ValueError, KeyError):
        records = {}
    output = {}
    for path in paths:
        raw = records.get(str(path), {})
        errors = [str(v) for k, v in raw.items() if k.split(':')[-1] == 'Error']
        if not raw:
            errors.append(result.stderr.strip()[-1000:] or 'No ExifTool record returned')
        meta = {}
        for field, tags in TAGS.items():
            entries = []
            for tag, value in raw.items():
                if tag.split(':')[-1] not in tags:
                    continue
                for item in value if isinstance(value, list) else [value]:
                    if str(item).strip():
                        entries.append({'value': item, 'source': str(path), 'field': tag,
                                        'basis': 'embedded value; semantic attribution not inferred'})
            if entries:
                meta[field] = entries
        output[str(path)] = {'metadata': meta, 'raw': raw, 'errors': errors, 'profile': META_PROFILE}
    return output


class VisualIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.con = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
        self.con.row_factory = sqlite3.Row
        self.roots = [(r['root_id'], Path(r['root_path'])) for r in self.con.execute('SELECT * FROM visual_roots')]
        self.profiles = {r['extractor_id']: dict(r) for r in self.con.execute('SELECT * FROM feature_extractors')}
        require(all(key in self.profiles for key in HASHES), 'Index lacks the requested p/d/a extractor profiles')

    def get(self, path, current):
        path = Path(path)
        for root_id, root in self.roots:
            if not path.is_relative_to(root):
                continue
            row = self.con.execute('SELECT * FROM visual_assets WHERE root_id=? AND relative_path=? '
                                   'AND size_bytes=? AND mtime_ns=? ORDER BY indexed_at DESC LIMIT 1',
                                   (root_id, str(path.relative_to(root)), current['size_bytes'], current['mtime_ns'])).fetchone()
            if row is None:
                continue
            features = {r['extractor_id']: r['feature_hex'] for r in self.con.execute(
                'SELECT extractor_id,feature_hex FROM visual_features WHERE asset_id=?', (row['asset_id'],))}
            result = {'asset_id': row['asset_id'], 'path': str(path), 'stamp': current,
                      'status': row['status'], 'error': row['error'], 'width': row['width_px'], 'height': row['height_px']}
            if row['status'] == 'indexed' and all(k in features for k in HASHES):
                result['hashes'] = [int(features[k], 16) for k in HASHES]
            return result
        return {'path': str(path), 'stamp': current, 'status': 'missing_or_stale_index', 'error': ''}


def visual_matches(source, targets, limits=(8, 12, 16)):
    """Independent Hamming distances, direct and approximate polarity complement."""
    if not source.get('hashes'):
        return []
    out = []
    for target in targets:
        feature = target['feature']
        if not feature.get('hashes'):
            continue
        for mode, masks in [('direct', (0, 0, 0)), ('negative_positive_lead', ((1 << 63) - 1, (1 << 64) - 1, (1 << 64) - 1))]:
            distances = [(a ^ b ^ mask).bit_count() for a, b, mask in zip(source['hashes'], feature['hashes'], masks)]
            if all(d <= limit for d, limit in zip(distances, limits)):
                out.append({'target_id': target['id'], 'engine': 'cached-pda', 'polarity': mode,
                            'phash_distance': distances[0], 'dhash_distance': distances[1], 'ahash_distance': distances[2],
                            'hash_bits': 64, 'source_index_path': source['path'], 'target_index_path': feature['path']})
    return out


def text_tokens(metadata):
    text = ' '.join(str(v['value']) for f in ('title', 'description', 'inventory') for v in metadata.get(f, []))
    return {t for t in re.findall(r'[^\W_]+', normalized(text)) if len(t) >= 3}


class MetadataIndex:
    def __init__(self, targets):
        self.targets = {t['id']: t for t in targets}
        self.tokens = {t['id']: text_tokens(t['metadata']) for t in targets}
        self.postings = defaultdict(set)
        self.identifiers = defaultdict(set)
        for tid, tokens in self.tokens.items():
            for token in tokens:
                self.postings[token].add(tid)
            for inventory in values(self.targets[tid]['metadata'], 'inventory'):
                self.identifiers[inventory].add(tid)
        self.weights = {token: math.log(1 + len(targets) / len(ids)) for token, ids in self.postings.items()}

    def search(self, metadata):
        exact = set().union(*(self.identifiers.get(v, set()) for v in values(metadata, 'inventory')))
        out = [{'target_id': tid, 'engine': 'metadata', 'basis': 'exact_inventory_value'} for tid in sorted(exact)]
        tokens = text_tokens(metadata)
        hits = Counter(tid for token in tokens for tid in self.postings.get(token, ()))
        denominator = sum(self.weights.get(t, 0) for t in tokens)
        for tid, count in hits.most_common(30):
            if tid in exact or count < 3 or not denominator:
                continue
            score = sum(self.weights[t] for t in tokens & self.tokens[tid]) / denominator
            if score >= .6:
                out.append({'target_id': tid, 'engine': 'metadata', 'basis': 'weighted_shared_words',
                            'shared_words': sorted(tokens & self.tokens[tid]), 'source_word_coverage': round(score, 4)})
        return out


def comparison_buffer(path, edge=256, prefer_thumbnail=True):
    """Small temporary comparison image; prefer a TIFF's embedded thumbnail.

    A thumbnail is a discovery proxy only. Its relation to full pixels/layers is
    not certified. Never use this buffer to authorize redundancy.
    """
    from PIL import Image, ImageOps
    path = Path(path)
    with Image.open(path) as image:
        if image.format != 'TIFF':
            image.thumbnail((edge, edge))
            return ImageOps.exif_transpose(image).convert('RGB'), 'decoded_image'
        if prefer_thumbnail:
            try:
                image.seek(1)
                if max(image.size) <= 1024:
                    image.thumbnail((edge, edge))
                    return ImageOps.exif_transpose(image).convert('RGB'), 'embedded_tiff_thumbnail'
            except EOFError:
                pass
        image.seek(0)
        tags = image.tag_v2
        bits = tuple(tags.get(258, ()))
        if (tags.get(259) == 1 and tags.get(262) == 2 and tags.get(284, 1) == 1 and
                bits in ((8, 8, 8), (16, 16, 16)) and 273 in tags and tags.get(277, 3) == 3):
            width, height = int(tags[256]), int(tags[257])
            offsets, strip_rows, orientation = tuple(tags[273]), int(tags.get(278, height)), tags.get(274, 1)
        else:
            image.thumbnail((edge, edge))
            return ImageOps.exif_transpose(image).convert('RGB'), 'decoded_tiff_primary_image'
    step = max(1, math.ceil(max(width, height) / edge))
    row_bytes = width * 3 * (bits[0] // 8)
    samples = []
    with path.open('rb') as handle:
        little = handle.read(2) == b'II'
        for y in range(0, height, step):
            handle.seek(offsets[y // strip_rows] + (y % strip_rows) * row_bytes)
            row = handle.read(row_bytes)
            require(len(row) == row_bytes, 'Truncated TIFF row')
            if bits[0] == 16:
                row = row[1 if little else 0::2]
            samples.append(row)
    image = Image.frombytes('RGB', (width, len(samples)), b''.join(samples))
    image = image.resize((max(1, round(width / step)), len(samples)), Image.Resampling.LANCZOS)
    image.getexif()[274] = orientation
    return ImageOps.exif_transpose(image), 'sampled_uncompressed_tiff_rows'


def parse_czkawka(groups, source_map, target_map, algorithm, settings=None):
    """Reference-mode output is [reference, [matches]], not all-pairs groups."""
    out = defaultdict(list)
    from czkawka_config import DEFAULT_PASS, pass_id
    settings = settings or {**DEFAULT_PASS, 'algorithm': algorithm}
    for group in groups:
        require(isinstance(group, list) and len(group) == 2 and isinstance(group[0], dict)
                and isinstance(group[1], list), 'Unsupported Czkawka reference JSON schema')
        target = target_map.get(group[0].get('path'))
        if target is None:
            continue
        for match in group[1]:
            source = source_map.get(match.get('path'))
            if source is not None:
                out[source].append({'target_id': target, 'engine': 'czkawka', **settings,
                                    'pass_id': pass_id(settings), 'difference': match['difference'],
                                    'basis': 'comparison_proxy_lead'})
    return out
