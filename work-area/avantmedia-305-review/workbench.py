#!/usr/bin/env python3
"""Small localhost reviewer for the 305 unresolved Avantmedia TIFFs."""

from __future__ import annotations

import argparse
import csv
from functools import lru_cache
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps
import tiff_review


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path('/media/vladimir/Elements/restore/archive/Avantmedia')
MOUNT = Path('/media/vladimir/Elements')
CANDIDATES = ROOT / 'candidates.csv'
DECISIONS = ROOT / 'review-decisions.csv'
METADATA_PROPOSALS = ROOT / 'metadata-proposals.csv'
DATABASE = ROOT / 'review.sqlite'
PAGE = ROOT / 'workbench.html'
PREPARATION = ROOT / 'accession-preparation.json'
PHASH_REPORT = Path('/mnt/volume/iss-archive-work/avantmedia-phash-nearest-by-unit-20261014.csv')
ARCHIVE_UNITS = Path('/mnt/volume/iss-archive/units')
LOCK = threading.RLock()
IMAGE_SLOTS = threading.BoundedSemaphore(2)
COMPARISON_IMAGES: dict[str, tuple[Path, ...]] = {}
FIELDS = [
    'source_path', 'source_relative_path', 'review_proxy_path', 'proxy_source_path',
    'size_bytes', 'folder', 'filename', 'embedded_descriptive_metadata',
    'review_state', 'note', 'reviewed_at',
]


def mount_guard() -> str:
    result = subprocess.run(
        ['findmnt', '-rn', '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS', str(MOUNT)],
        capture_output=True, text=True, check=True,
    )
    fields = result.stdout.strip().split()
    if (len(fields) != 4 or fields[:3] != ['/dev/sdf1', str(MOUNT), 'ntfs3']
            or 'ro' not in fields[3].split(',') or 'rw' in fields[3].split(',')):
        raise ValueError('The required read-only Elements mount is unavailable.')
    return result.stdout.strip()


def connect(readonly: bool = True) -> sqlite3.Connection:
    mode = 'ro' if readonly else 'rw'
    con = sqlite3.connect(f'file:{DATABASE}?mode={mode}', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def database(readonly: bool = True):
    con = connect(readonly)
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize() -> None:
    """Create the one-table rebuildable review index, preserving any saved notes."""
    if DATABASE.exists():
        con = connect(False)
    else:
        con = sqlite3.connect(DATABASE)
        con.row_factory = sqlite3.Row
    try:
        con.execute('''CREATE TABLE IF NOT EXISTS candidates (
            source_path TEXT PRIMARY KEY,
            source_relative_path TEXT NOT NULL UNIQUE,
            review_proxy_path TEXT NOT NULL DEFAULT '',
            proxy_source_path TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
            folder TEXT NOT NULL,
            filename TEXT NOT NULL,
            embedded_descriptive_metadata TEXT NOT NULL,
            review_state TEXT NOT NULL CHECK(review_state = 'unresolved'),
            note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL DEFAULT ''
        )''')
        existing = con.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
        with CANDIDATES.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 305:
            raise ValueError(f'Expected 305 readable candidates, found {len(rows)}')
        if existing == 0:
            con.executemany(
                '''INSERT INTO candidates (
                    source_path, source_relative_path, review_proxy_path, proxy_source_path,
                    size_bytes, folder, filename, embedded_descriptive_metadata,
                    review_state, note, reviewed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                [(r['source_path'], r['source_relative_path'], r['review_proxy_path'],
                  r['proxy_source_path'], int(r['size_bytes']), r['folder'], r['filename'],
                  r['embedded_descriptive_metadata'], 'unresolved', '', '') for r in rows],
            )
            con.commit()
        else:
            db_rows = {r['source_path']: r for r in con.execute('SELECT * FROM candidates')}
            csv_rows = {r['source_path']: r for r in rows}
            if set(db_rows) != set(csv_rows):
                raise ValueError('SQLite and the readable candidate list have different paths')
            for path, dbrow in db_rows.items():
                row = csv_rows[path]
                if any(str(dbrow[k]) != str(row[k]) for k in (
                    'source_relative_path', 'proxy_source_path',
                    'folder', 'filename', 'embedded_descriptive_metadata'
                )) or int(dbrow['size_bytes']) != int(row['size_bytes']):
                    raise ValueError(f'SQLite candidate evidence differs from CSV: {path}')
                if str(dbrow['review_proxy_path']) != row['review_proxy_path']:
                    con.execute('UPDATE candidates SET review_proxy_path=? WHERE source_path=?',
                                (row['review_proxy_path'], path))
            con.commit()
    finally:
        con.close()


def export_csv(con: sqlite3.Connection) -> None:
    temporary = DECISIONS.with_suffix('.csv.tmp')
    rows = con.execute('SELECT * FROM candidates ORDER BY folder, filename, source_path')
    with temporary.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, DECISIONS)


def check() -> dict[str, object]:
    mount = mount_guard()
    con = connect()
    try:
        integrity = con.execute('PRAGMA quick_check').fetchone()[0]
        rows = [dict(r) for r in con.execute('SELECT * FROM candidates ORDER BY folder,filename,source_path')]
        if integrity != 'ok' or len(rows) != 305:
            raise ValueError(f'SQLite check failed: {integrity}; candidate count={len(rows)}')
        if any(row['review_state'] != 'unresolved' for row in rows):
            raise ValueError('A candidate has an unsupported review state')
        if not DECISIONS.exists():
            raise ValueError('Readable review-decisions.csv is missing')
        with DECISIONS.open(encoding='utf-8-sig', newline='') as handle:
            readable = list(csv.DictReader(handle))
        expected = [{k: str(v) for k, v in row.items()} for row in rows]
        if expected != readable:
            raise ValueError('SQLite and review-decisions.csv differ')
        proposals = load_metadata_proposals()
        if set(proposals) != {row['source_relative_path'] for row in rows}:
            raise ValueError('Metadata proposals do not cover the exact candidate set')
        tiff_decisions, _ = tiff_review.load()
        tiff_review.validate_set(tiff_decisions)
        if not tiff_review.index_agrees(con, tiff_decisions):
            raise ValueError('TIFF review SQLite index and readable JSONL differ')
        return {
            'ok': True, 'mount': mount, 'candidates': len(rows),
            'unresolved': len(rows),
            'with_visual_proxy': sum(bool(r['proxy_source_path']) for r in rows),
            'metadata_proposals': len(proposals),
            'public_catalogue_leads': sum(bool(r['catalogue_object_id']) for r in proposals.values()),
            'sqlite_csv_agree': True,
            'tiff_review_decisions': len(tiff_decisions),
            'tiff_review_sqlite_jsonl_agree': True,
        }
    finally:
        con.close()


def load_comparisons() -> dict[str, list[dict[str, object]]]:
    """Join saved pHash nearest-Master evidence to existing unit catalogue rows."""
    with CANDIDATES.open(encoding='utf-8-sig', newline='') as handle:
        candidate_paths = {row['source_path'] for row in csv.DictReader(handle)}
    hits_by_candidate: dict[str, list[dict[str, str]]] = {}
    with PHASH_REPORT.open(encoding='utf-8-sig', newline='') as handle:
        for hit in csv.DictReader(handle):
            if hit['avantmedia_path'] in candidate_paths:
                hits_by_candidate.setdefault(hit['avantmedia_path'], []).append(hit)
    needed: set[tuple[str, str]] = set()
    for source, hits in hits_by_candidate.items():
        hits.sort(key=lambda hit: (int(hit['minimum_phash_distance']), hit['unit'], hit['nearest_unit_source_path']))
        needed.update((hit['unit'], hit['nearest_unit_source_path']) for hit in hits)

    source_rows: dict[tuple[str, str], dict[str, str]] = {}
    for unit in {unit for unit, _ in needed}:
        catalog = ARCHIVE_UNITS / unit / 'CATALOG.csv'
        if not catalog.is_file():
            continue
        with catalog.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                key = (unit, row.get('original_source_path', ''))
                if key in needed:
                    current = source_rows.get(key)
                    rank = {'preservation_master': 0, 'master': 1}.get(row.get('file_role', ''), 2)
                    current_rank = {'preservation_master': 0, 'master': 1}.get(
                        current.get('file_role', '') if current else '', 3)
                    if current is None or rank < current_rank:
                        source_rows[key] = row

    per_candidate: dict[str, list[dict[str, object]]] = {}
    COMPARISON_IMAGES.clear()
    archive_root = ARCHIVE_UNITS.resolve(strict=True)
    unit_roots: dict[str, Path] = {}
    for source, hits in hits_by_candidate.items():
        hits.sort(key=lambda hit: (int(hit['minimum_phash_distance']), hit['unit'], hit['nearest_unit_source_path']))
        close = [hit for hit in hits if int(hit['minimum_phash_distance']) <= 16]
        hits = close[:3] if close else hits[:1]
        for hit in hits:
            unit = hit['unit']
            match = source_rows.get((unit, hit['nearest_unit_source_path']))
            comparison_id = hashlib.sha256(
                f"{source}\0{unit}\0{hit['nearest_unit_source_path']}".encode()
            ).hexdigest()[:24]
            image_paths: tuple[Path, ...] = ()
            if match:
                unit_root = unit_roots.get(unit)
                if unit_root is None:
                    unit_root = (ARCHIVE_UNITS / unit).resolve(strict=True)
                    if not unit_root.is_relative_to(archive_root):
                        continue
                    unit_roots[unit] = unit_root
                paths = []
                names = []
                for relative in (match.get('master_relative_path', ''),
                                 match.get('access_relative_path', ''),
                                 match.get('final_relative_path', '')):
                    relative_path = Path(relative) if relative else None
                    if (relative_path is None or relative_path.is_absolute()
                            or '..' in relative_path.parts):
                        continue
                    paths.append(unit_root / relative_path)
                    names.append(relative_path.name)
                if paths:
                    image_paths = tuple(paths)
                    COMPARISON_IMAGES[comparison_id] = image_paths
                    image_name = names[0]
                else:
                    image_name = ''
            else:
                image_name = ''
            per_candidate.setdefault(source, []).append({
                'id': comparison_id,
                'unit': unit,
                'source_path': hit['nearest_unit_source_path'],
                'distance': int(hit['minimum_phash_distance']),
                'similar': int(hit['minimum_phash_distance']) <= 16,
                'title': (match.get('title') or match.get('object_current_title') or '') if match else '',
                'date': (match.get('date_text') or match.get('object_current_date_text') or '') if match else '',
                'description': (match.get('description') or match.get('object_current_description') or '') if match else '',
                'creator': (match.get('creator') or '') if match else '',
                'material': (match.get('material') or '') if match else '',
                'dimensions': (match.get('dimensions') or '') if match else '',
                'places': (match.get('places') or '') if match else '',
                'tags': (match.get('tags') or '') if match else '',
                'inventory': (match.get('inventory') or match.get('object_current_inventory') or '') if match else '',
                'image_name': image_name,
                'catalogue_joined': bool(match),
            })
    return per_candidate


def load_metadata_proposals() -> dict[str, dict[str, str]]:
    """Read web-researched caption proposals without applying them to candidates."""
    with METADATA_PROPOSALS.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 305:
        raise ValueError(f'Expected metadata proposals for 305 candidates, found {len(rows)}')
    proposals = {row['source_relative_path']: row for row in rows}
    if len(proposals) != 305:
        raise ValueError('Metadata proposals contain duplicate or missing candidate paths')
    return proposals


def load_preparation() -> tuple[dict, dict]:
    """Expose the bounded preparation report without adding decision states."""
    if not PREPARATION.exists():
        return {}, {}
    summary = json.loads(PREPARATION.read_text())
    if summary['metadata_acceptance']['sha256'] != hashlib.sha256(METADATA_PROPOSALS.read_bytes()).hexdigest():
        raise ValueError('Metadata changed after accession preparation; refresh the preparation report.')
    rows = [json.loads(line) for line in (ROOT / 'accession-preparation.jsonl').read_text().splitlines()]
    data = {row['source_path']: row for row in rows}
    with CANDIDATES.open(newline='', encoding='utf-8-sig') as handle:
        paths = {row['source_path'] for row in csv.DictReader(handle)}
    if len(rows) != 305 or set(data) != paths:
        raise ValueError('Accession preparation does not cover the exact review set.')
    for row in rows:
        if row['selected_master_path'] or row['accession_authorized'] or row['deletion_authorized']:
            raise ValueError('The preparation report must not apply Master or accession decisions.')
        for master in row['existing_masters']:
            key = f"accession:{master['unit']}:{master['record_key']}"
            paths = tuple(Path(p) for p in (master['access_path'], master['master_path']) if p)
            if any(not p.resolve(strict=True).is_relative_to(ARCHIVE_UNITS.resolve()) for p in paths):
                raise ValueError('Prepared Master comparison escaped the archive units.')
            COMPARISON_IMAGES[key] = paths
            master['comparison_id'] = key
    return summary, data


@lru_cache(maxsize=16)
def render_proxy_preview(path_text: str, large: bool) -> bytes:
    """Render display-only previews in memory; never write derived images to disk."""
    path = Path(path_text).resolve(strict=True)
    if not path.is_relative_to(SOURCE_ROOT) or not path.is_file():
        raise ValueError('Visual aid escaped the approved Avantmedia source root')
    if not large:
        result = subprocess.run(
            ['exiftool', '-b', '-ThumbnailImage', str(path)],
            capture_output=True, check=False, timeout=30,
        )
        if result.returncode != 0 or not result.stdout.startswith(b'\xff\xd8'):
            raise FileNotFoundError('No embedded JPEG thumbnail in the visual aid')
        return result.stdout
    with Image.open(path) as original:
        if getattr(original, 'format', '') == 'JPEG':
            original.draft('RGB', (1200, 1200))
        preview = ImageOps.exif_transpose(original).convert('RGB')
        preview.thumbnail((1200, 1200))
        output = io.BytesIO()
        preview.save(output, format='JPEG', quality=88)
        return output.getvalue()


def read_uncompressed_tiff_preview(path: Path, edge: int = 1200,
                                   allowed_root: Path = SOURCE_ROOT) -> Image.Image:
    """Sample existing uncompressed RGB TIFF rows in memory for review only."""
    path = path.resolve(strict=True)
    if not path.is_relative_to(allowed_root.resolve(strict=True)):
        raise ValueError('TIFF escaped the approved source root')
    with Image.open(path) as original:
        tags = original.tag_v2
        bits = tuple(tags.get(258, ()))
        if (tags.get(259) != 1 or tags.get(262) != 2 or
                tags.get(284, 1) != 1 or bits not in ((8, 8, 8), (16, 16, 16))):
            raise ValueError('This TIFF layout needs a different visual reader')
        # Pillow's size already reflects orientation; strip offsets use stored dimensions.
        width, height = int(tags[256]), int(tags[257])
        offsets = tuple(tags[273])
        strip_rows = int(tags.get(278, height))
        orientation = tags.get(274, 1)
    step = max(1, max(width, height) // edge)
    bytes_per_sample = bits[0] // 8
    row_bytes = width * 3 * bytes_per_sample
    samples = []
    with path.open('rb') as handle:
        little = handle.read(2) == b'II'
        for y in range(0, height, step):
            handle.seek(offsets[y // strip_rows] + (y % strip_rows) * row_bytes)
            row = handle.read(row_bytes)
            if len(row) != row_bytes:
                raise ValueError('Truncated TIFF row')
            if bytes_per_sample == 2:
                row = row[1 if little else 0::2]
            samples.append(row)
    image = Image.frombytes('RGB', (width, len(samples)), b''.join(samples))
    image = image.resize((max(1, round(width / step)), len(samples)), Image.Resampling.LANCZOS)
    image.getexif()[274] = orientation
    return ImageOps.exif_transpose(image)


@lru_cache(maxsize=7)
def render_tiff_preview(path_text: str) -> bytes:
    preview = read_uncompressed_tiff_preview(Path(path_text))
    output = io.BytesIO()
    preview.save(output, format='JPEG', quality=88)
    return output.getvalue()


# workbench.html owns the review presentation.


@lru_cache(maxsize=1)
def tiff_scan_data() -> tuple[dict, list, dict]:
    summary = json.loads((ROOT / 'tiff-scan-summary.json').read_text())
    candidates = [json.loads(line) for line in (ROOT / 'tiff-scan-results.jsonl').read_text().splitlines()]
    masters = {row['record_key']: row for row in (
        json.loads(line) for line in (ROOT / 'tiff-scan-masters.jsonl').read_text().splitlines())}
    if len(candidates) != summary['candidate_count'] or any(Path(r['source_path']).suffix.lower() not in ('.tif', '.tiff') for r in candidates):
        raise ValueError('The full scan must contain exactly the recorded TIFF candidates.')
    return summary, candidates, masters


@lru_cache(maxsize=40)
def scan_preview(path_text: str, inverted: bool, master: bool, edge: int = 1000) -> bytes:
    allowed = ARCHIVE_UNITS / 'kurt-schwarz' if master else SOURCE_ROOT
    path = Path(path_text).resolve(strict=True)
    if not path.is_relative_to(allowed.resolve(strict=True)):
        raise ValueError('Comparison image escaped its approved root.')
    if path.suffix.lower() in ('.tif', '.tiff'):
        try:
            preview = read_uncompressed_tiff_preview(path, edge=edge, allowed_root=allowed)
        except (ValueError, TypeError):
            with Image.open(path) as original:
                try:
                    preview = ImageOps.exif_transpose(original).convert('RGB')
                except TypeError:
                    preview = original.convert('RGB')
                preview.thumbnail((edge, edge))
    else:
        with Image.open(path) as original:
            original.draft('RGB', (edge, edge))
            preview = ImageOps.exif_transpose(original).convert('RGB')
            preview.thumbnail((edge, edge))
    if inverted:
        preview = ImageOps.invert(preview.convert('RGB'))
    output = io.BytesIO()
    preview.save(output, format='JPEG', quality=88)
    return output.getvalue()



class Handler(BaseHTTPRequestHandler):
    def respond(self, body: bytes, status: int = 200, kind: str = 'application/json') -> None:
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def json(self, value: object, status: int = 200) -> None:
        self.respond(json.dumps(value, ensure_ascii=False).encode(), status)

    def valid_host(self) -> bool:
        return self.headers.get('Host') in {
            f'127.0.0.1:{self.server.server_port}',
            f'localhost:{self.server.server_port}',
        }

    def do_GET(self) -> None:
        try:
            if not self.valid_host():
                return self.json({'error': 'Localhost access required'}, 403)
            route = urlparse(self.path)
            if route.path == '/':
                return self.respond(PAGE.read_bytes(), kind='text/html; charset=utf-8')
            if route.path == '/tiff-scan':
                return self.respond((ROOT / 'tiff-scan.html').read_bytes(), kind='text/html; charset=utf-8')
            if route.path == '/api/tiff-scan':
                summary, candidates, masters = tiff_scan_data()
                with LOCK:
                    review = tiff_review.data()
                return self.json({'summary': summary, 'candidates': candidates, 'masters': masters,
                                  'token': self.server.token, **review})
            if route.path == '/tiff-review-decisions':
                with LOCK:
                    decisions, _ = tiff_review.load()
                body = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in decisions.values())
                return self.respond(body.encode(), kind='application/x-ndjson; charset=utf-8')
            if route.path in ('/tiff-scan-image', '/tiff-scan-master'):
                mount_guard()
                _, candidates, masters = tiff_scan_data()
                query = parse_qs(route.query)
                is_master = route.path == '/tiff-scan-master'
                if is_master:
                    row = masters.get(query.get('record', [''])[0])
                    path = row['master_path'] if row else ''
                else:
                    source = query.get('source', [''])[0]
                    row = next((r for r in candidates if r['source_path'] == source), None)
                    path = row['source_path'] if row else ''
                if not path:
                    return self.json({'error': 'Unknown TIFF or current Master'}, 404)
                with IMAGE_SLOTS:
                    result = scan_preview(path, query.get('invert', ['0'])[0] == '1', is_master,
                                          260 if query.get('thumb', ['0'])[0] == '1' else 1000)
                return self.respond(result, kind='image/jpeg')
            if route.path == '/health':
                return self.json(check())
            if route.path == '/api/candidates':
                with LOCK, database() as con:
                    rows = [dict(r) for r in con.execute(
                        'SELECT * FROM candidates ORDER BY folder,filename,source_path')]
                comparison_data = getattr(self.server, 'comparisons', {})
                metadata_data = load_metadata_proposals()
                preparation_summary, preparation_data = load_preparation()
                for row in rows:
                    row['comparisons'] = comparison_data.get(row['source_path'], [])
                    row['metadata_proposal'] = metadata_data.get(row['source_relative_path'])
                    row['accession_preparation'] = preparation_data.get(row['source_path'])
                return self.json({'candidates': rows, 'token': self.server.token,
                                  'preparation': preparation_summary})
            if route.path == '/accession-preparation':
                load_preparation()
                return self.respond((ROOT / 'ACCESSION_PREPARATION.md').read_bytes(),
                                    kind='text/plain; charset=utf-8')
            if route.path == '/comparison-image':
                comparison_id = parse_qs(route.query).get('id', [''])[0]
                paths = COMPARISON_IMAGES.get(comparison_id)
                if not paths:
                    return self.json({'error': 'Unknown comparison image'}, 404)
                archive_root = ARCHIVE_UNITS.resolve(strict=True)
                path = None
                for candidate in paths:
                    try:
                        resolved = candidate.resolve(strict=True)
                    except FileNotFoundError:
                        continue
                    if resolved.is_relative_to(archive_root) and resolved.is_file():
                        path = resolved
                        break
                if path is None:
                    return self.json({'error': 'No existing archive Master image is available'}, 404)
                with IMAGE_SLOTS:
                    with Image.open(path) as original:
                        preview = ImageOps.exif_transpose(original).convert('RGB')
                        preview.thumbnail((1400, 1400))
                        output = io.BytesIO()
                        preview.save(output, format='JPEG', quality=88, optimize=True)
                return self.respond(output.getvalue(), kind='image/jpeg')
            if route.path == '/image':
                mount_guard()
                source_path = parse_qs(route.query).get('source', [''])[0]
                with LOCK, database() as con:
                    row = con.execute('SELECT proxy_source_path FROM candidates WHERE source_path=?',
                                      (source_path,)).fetchone()
                if not row:
                    return self.json({'error': 'Unknown candidate'}, 404)
                if not row['proxy_source_path']:
                    with IMAGE_SLOTS:
                        preview = render_tiff_preview(source_path)
                    return self.respond(preview, kind='image/jpeg')
                path = Path(row['proxy_source_path'])
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(SOURCE_ROOT) or not resolved.is_file():
                    raise ValueError('Preview escaped the approved Avantmedia source root')
                large = parse_qs(route.query).get('large', ['0'])[0] == '1'
                try:
                    with IMAGE_SLOTS:
                        preview = render_proxy_preview(str(resolved), large)
                except FileNotFoundError as error:
                    return self.json({'error': str(error)}, 404)
                return self.respond(preview, kind='image/jpeg')
            return self.json({'error': 'Not found'}, 404)
        except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
            return self.json({'error': str(error)}, 409)

    def do_POST(self) -> None:
        try:
            if (not self.valid_host() or self.path not in ('/api/review', '/api/tiff-review') or
                    not secrets.compare_digest(self.headers.get('X-Review-Token', ''), self.server.token)):
                return self.json({'error': 'Invalid review request'}, 403)
            origin = self.headers.get('Origin')
            if origin and origin not in {
                f'http://127.0.0.1:{self.server.server_port}',
                f'http://localhost:{self.server.server_port}',
            }:
                return self.json({'error': 'Origin does not match this workbench'}, 403)
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size < 4000:
                raise ValueError('Invalid review request size')
            payload = json.loads(self.rfile.read(size))
            if self.path == '/api/tiff-review':
                mount_guard()
                _, candidates, masters = tiff_scan_data()
                with LOCK:
                    decision, revision = tiff_review.save(payload, candidates, masters)
                    tiff_review.rebuild_index()
                return self.json({'ok': True, 'decision': decision, 'revision': revision})
            if (not isinstance(payload, dict) or set(payload) != {
                    'source_path', 'state', 'note', 'previous_reviewed_at'}):
                raise ValueError('Review request fields are invalid')
            if payload['state'] != 'unresolved':
                raise ValueError('This set has no resolved object identities; keep the candidate unresolved')
            note = payload['note']
            if not isinstance(note, str) or len(note) > 1000:
                raise ValueError('A review note must be text of at most 1000 characters')
            source = Path(payload['source_path'])
            if not source.is_relative_to(SOURCE_ROOT):
                raise ValueError('Candidate is outside the Avantmedia source root')
            mount_guard()
            reviewed_at = datetime.now(timezone.utc).isoformat()
            with LOCK, database(False) as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT reviewed_at FROM candidates WHERE source_path=?',
                                  (str(source),)).fetchone()
                if not row:
                    raise ValueError('Unknown candidate')
                if row['reviewed_at'] != payload['previous_reviewed_at']:
                    raise ValueError('This note changed in another tab; reload before saving')
                con.execute('UPDATE candidates SET review_state=?,note=?,reviewed_at=? WHERE source_path=?',
                            ('unresolved', note.strip(), reviewed_at, str(source)))
                con.commit()
                export_csv(con)
            return self.json({'ok': True, 'note': note.strip(), 'reviewed_at': reviewed_at})
        except (ValueError, TypeError, OSError, sqlite3.Error, subprocess.CalledProcessError,
                json.JSONDecodeError) as error:
            return self.json({'error': str(error)}, 409)


def serve(port: int) -> None:
    mount_guard()
    initialize()
    tiff_review.rebuild_index()
    with database(False) as con:
        export_csv(con)
    status = check()
    if not status['ok']:
        raise SystemExit('Workbench health check failed')
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.token = secrets.token_urlsafe(32)
    server.comparisons = load_comparisons()
    tiff_scan_data()  # The completed scan is immutable while this review service runs.
    print(f"Avantmedia review: http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check mount, SQLite and readable CSV agreement')
    parser.add_argument('--init', action='store_true', help='Initialize SQLite and readable review decisions')
    parser.add_argument('--port', type=int, default=8774)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False, indent=2))
    elif args.init:
        mount_guard()
        initialize()
        tiff_review.rebuild_index()
        with database(False) as con:
            export_csv(con)
        print(json.dumps(check(), ensure_ascii=False, indent=2))
    else:
        serve(args.port)


if __name__ == '__main__':
    main()
