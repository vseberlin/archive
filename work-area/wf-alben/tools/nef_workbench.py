#!/usr/bin/env python3
"""Focused NEF comparison UI over the existing WF-Alben decisions and CSVs.

Archive and website media are read-only. Preview conversions stay in memory.
No initialization, schema changes, accession, or source changes are performed.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import closing
import csv
import hashlib
import io
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler

from PIL import Image, ImageOps
import wf_alben_workbench as existing

WORK = existing.WORK_ROOT
UNIT = Path('/mnt/volume/iss-archive/units/wf-alben')
SOURCE = Path('/media/vladimir/Elements')
HTML = Path(__file__).with_name('nef_workbench.html')
VERSION = '1.0.3'
ISSUES = {
    'md:93484': ('Levels normalization complete · page 5', 'Finalized: the full-resolution reading JPEG uses grayscale levels with black point 65%, white point 80% and gamma 1.0. The selected color TIFF Master is unchanged. Compare the TIFF and normalized reading copy below.'),
    'md:93485': ('Selected TIFF accepted · page 7', 'Decision recorded: the selected TIFF and its framing are acceptable. Keep this Master. No wider album-leaf replacement is requested. The earlier website comparison and alternate source remain optional evidence.'),
    'md:93489': ('Page 11 finalized', 'Finalized: the archive catalogue and embedded titles now say S. 11. The original S. 7 title remains in the editorial provenance. The selected TIFF and reading JPEG retain their image pixels.'),
}


class ReviewError(ValueError):
    pass


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def source_mount():
    """Reject parent filesystems, alternate targets, writable mounts and devices."""
    try:
        p = subprocess.run(['findmnt', '-J', '-T', str(SOURCE), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'],
                           capture_output=True, text=True, check=True, timeout=5)
        rows = json.loads(p.stdout).get('filesystems', [])
        good = (len(rows) == 1 and rows[0].get('source') == '/dev/sdf1'
                and rows[0].get('target') == str(SOURCE) and rows[0].get('fstype') == 'ntfs3'
                and 'ro' in rows[0].get('options', '').split(',')
                and 'rw' not in rows[0].get('options', '').split(','))
        return {'available': good, 'required': '/dev/sdf1 /media/vladimir/Elements ntfs3 ro',
                'observed': rows}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {'available': False, 'required': '/dev/sdf1 /media/vladimir/Elements ntfs3 ro', 'error': str(e)}


def guarded_source(candidate):
    if not source_mount()['available']:
        raise ReviewError('Source unavailable: the exact protected Elements mount is required.')
    path = Path(candidate['source_path'])
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(SOURCE / 'restore/archive') or resolved != path:
        raise ReviewError('Source path is outside the recorded protected archive or is a symlink.')
    st = path.stat()
    if st.st_size != candidate['size_bytes'] or st.st_mtime_ns != candidate['mtime_ns']:
        raise ReviewError('Source size or modification time differs from the recorded candidate.')
    return path


def version_of(decision):
    # A form edit check over a small decision record, never a media checksum.
    return hashlib.sha256(json.dumps(dict(decision), sort_keys=True).encode()).hexdigest()


def invariant_check(con):
    decisions = {r['record_key']: dict(r) for r in con.execute('SELECT * FROM decisions')}
    objects = {r['record_key']: dict(r) for r in con.execute('SELECT * FROM objects')}
    candidates = {r['candidate_id']: dict(r) for r in con.execute('SELECT * FROM candidates')}
    dispositions = {}
    for r in con.execute('SELECT * FROM candidate_dispositions'):
        dispositions.setdefault(r['record_key'], {})[r['candidate_id']] = dict(r)
    for key, d in decisions.items():
        own = {cid for cid, c in candidates.items() if c['record_key'] == key}
        ds = dispositions.get(key, {})
        if set(ds) != own or d['deletion_authorized'] or any(r['deletion_authorized'] for r in ds.values()):
            raise ReviewError('Decision/disposition coverage or deletion invariant failed.')
        if d['decision_state'] == 'selected_master':
            cid = d['selected_candidate_id']
            if cid not in own or any(r['disposition'] != ('selected_master' if c == cid else 'redundant') for c, r in ds.items()):
                raise ReviewError('Exactly one selected Master and redundant alternatives are required.')
        elif d['decision_state'] == 'redundant_derivative':
            parent = objects[key]['linked_record_key']
            if (d['selected_candidate_id'] or objects[key]['link_relation'] != 'derived_from'
                    or decisions.get(parent, {}).get('decision_state') != 'selected_master'
                    or any(r['disposition'] != 'redundant' for r in ds.values())):
                raise ReviewError('A linked detail must remain redundant under its selected parent.')
        elif d['decision_state'] == 'unresolved':
            if d['selected_candidate_id'] or any(r['disposition'] != 'undecided' for r in ds.values()):
                raise ReviewError('Unresolved Master invariant failed.')
        else:
            raise ReviewError('Unknown decision state.')


def export_current(con):
    existing.atomic_csv(existing.DECISIONS_CSV, existing.DECISION_FIELDS, existing.decision_rows(con))
    existing.atomic_csv(existing.DISPOSITIONS_CSV, existing.DISPOSITION_FIELDS, existing.disposition_rows(con))
    # Keep the existing readable parent/detail projection aligned after a change.
    rows = read_csv(existing.DERIVATIVE_RELATIONSHIPS_CSV)
    paths = {r['record_key']: r['selected_master_path'] for r in existing.decision_rows(con)}
    for r in rows:
        r['parent_selected_master_path'] = paths[r['parent_record_key']]
    if rows:
        existing.atomic_csv(existing.DERIVATIVE_RELATIONSHIPS_CSV, rows[0].keys(), rows)


def mirrors_match(con):
    if existing.decision_rows(con) != read_csv(existing.DECISIONS_CSV):
        raise ReviewError('SQLite and decision CSV differ. Resolve the existing mismatch before saving.')
    if existing.disposition_rows(con) != read_csv(existing.DISPOSITIONS_CSV):
        raise ReviewError('SQLite and disposition CSV differ. Resolve the existing mismatch before saving.')
    paths = {r['record_key']: r['selected_master_path'] for r in existing.decision_rows(con)}
    for r in read_csv(existing.DERIVATIVE_RELATIONSHIPS_CSV):
        if r['parent_selected_master_path'] != paths[r['parent_record_key']]:
            raise ReviewError('The readable detail relationship and selected parent differ.')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReviewError('Unexpected redirect from the local website image.')


class NEFWorkbench:
    def __init__(self, actor='vladimir'):
        self.actor = actor
        self.lock = threading.Lock()
        self.preview_slots = threading.BoundedSemaphore(2)
        self.cache_lock = threading.Lock()
        self.web_cache = OrderedDict()
        self.mappings = {r['record_key']: r for r in read_csv(WORK / 'wordpress-album-mapping.csv') if r['album_key'] == 'ALBUM-NEF'}
        self.order = [r for r in read_csv(WORK / 'album-reading-order.csv') if r['album_key'] == 'ALBUM-NEF']
        self.archived = {r['record_key']: r for r in read_csv(UNIT / 'metadata/objects.csv')}
        self.hashes = {r['source_path']: r.get('source_sha256', '') for r in read_csv(UNIT / 'db-restore/source-disposition.csv')}
        self.local = {}
        for key in self.mappings:
            obj = self.archived.get(key, {})
            for kind, field in [('master', 'master_relative_path'), ('access', 'access_relative_path')]:
                if obj.get(field):
                    path = UNIT / obj[field]
                    self.register_local((key, kind), path, UNIT)
            saved = WORK / (self.mappings[key]['inventory'] + '--wp.jpg')
            if saved.is_file():
                self.register_local((key, 'web'), saved, WORK)
        if len(self.mappings) != 63 or len(self.order) != 24:
            raise ReviewError('NEF mapping counts changed; review the scope first.')

    def register_local(self, key, path, root):
        resolved = path.resolve(strict=True)
        if resolved != path or not resolved.is_relative_to(root):
            raise ReviewError('Local image path is outside its fixed root or is a symlink.')
        st = path.stat()
        self.local[key] = (path, st.st_size, st.st_mtime_ns)

    def get_local(self, key):
        if key not in self.local:
            raise ReviewError('No registered image for this record and role.')
        path, size, mtime = self.local[key]
        st = path.stat()
        if path.resolve(strict=True) != path or st.st_size != size or st.st_mtime_ns != mtime:
            raise ReviewError('Registered image changed since startup; restart after reviewing the change.')
        return path

    def record(self, key):
        if key not in self.mappings:
            raise ReviewError('This workbench accepts only the 63 mapped NEF records.')
        with closing(existing.connect(read_only=True)) as con:
            obj = con.execute('SELECT * FROM objects WHERE record_key=?', (key,)).fetchone()
            decision = con.execute('SELECT * FROM decisions WHERE record_key=?', (key,)).fetchone()
            candidates = [dict(r) for r in con.execute('''SELECT c.*,d.disposition FROM candidates c
                 JOIN candidate_dispositions d ON c.candidate_id=d.candidate_id AND c.record_key=d.record_key
                 WHERE c.record_key=? ORDER BY CASE WHEN c.extension IN ('tif','tiff') THEN 0 ELSE 1 END,c.source_path''', (key,))]
        if not obj or not decision:
            raise ReviewError('Mapped object is absent from the existing workbench.')
        return dict(obj), dict(decision), candidates

    def overview(self):
        with closing(existing.connect(read_only=True)) as con:
            decisions = {r['record_key']: dict(r) for r in con.execute('SELECT * FROM decisions')}
        pages = []
        for row in self.order:
            r = dict(row)
            r['details'] = [m for m in self.mappings.values() if m['parent_record_key'] == row['record_key']]
            r['issue'] = ISSUES.get(row['record_key'], ('', ''))[0]
            r['decision'] = decisions[row['record_key']]
            pages.append(r)
        return {'pages': pages, 'mount': source_mount(), 'version': VERSION,
                'source_note': 'Source paths and technical data are recorded evidence until the protected disk is available.',
                'sequence_note': 'Existing website order is the starting evidence. Labels 9, 23 and 43 are absent; physical completeness is unresolved.'}

    def review(self, key):
        obj, decision, candidates = self.record(key)
        mapping = self.mappings[key]
        parent = mapping['parent_record_key']
        primary = parent or key
        archive = self.archived.get(primary, {})
        mount = source_mount()
        assets = []
        for kind, label in [('access', 'Archive JPEG'), ('master', 'Selected archive TIFF')]:
            if (primary, kind) in self.local:
                path = self.get_local((primary, kind))
                with Image.open(path) as im:
                    dims = list(im.size)
                assets.append({'record': primary, 'kind': kind, 'label': ('Parent · ' if parent else '') + ('Normalized reading JPEG' if primary == 'md:93484' and kind == 'access' else label),
                               'path': str(path), 'dimensions': dims, 'available': True,
                               'sha256': archive.get('master_sha256' if kind == 'master' else 'access_sha256', ''),
                               'size_bytes': path.stat().st_size})
        assets.append({'record': key, 'kind': 'web', 'label': 'Website detail · reference only' if parent else 'Website JPEG · reference only',
                       'path': str(self.local[(key, 'web')][0]) if (key, 'web') in self.local else mapping['wp_image_url'],
                       'available': True, 'evidence': 'Saved 2026-09-12 comparison' if (key, 'web') in self.local else 'Read from the mapped local website attachment'})
        for c in candidates:
            c['sha256'] = self.hashes.get(c['source_path'], '')
            c['eligible'] = not parent and c['extension'].lower() in ('tif', 'tiff') and c['visual_index_state'] == 'indexed'
            c['available'] = False
            c['unavailable_reason'] = 'Protected source disk unavailable.'
            if mount['available']:
                try:
                    guarded_source(c)
                    c['available'] = True
                    c['unavailable_reason'] = ''
                except (ReviewError, OSError) as e:
                    c['unavailable_reason'] = str(e)
            label = ('TIFF candidate · ' if c['eligible'] else 'Source comparison · ') + c['filename']
            if 'Ausstellung Unvollendete Metropole' in c['source_path']:
                label += ' · exhibition'
            elif '/Geschichte-und-WF-Archiv/NEF/' in c['source_path']:
                label += ' · NEF archive'
            else:
                label += ' · ' + Path(c['source_path']).parent.name
            assets.append({'record': key, 'kind': 'candidate', 'candidate': c['candidate_id'], 'label': label,
                           'path': c['source_path'], 'dimensions': [c['width_px'], c['height_px']],
                           'size_bytes': c['size_bytes'], 'sha256': c['sha256'], 'available': c['available'],
                           'evidence': c['unavailable_reason'] or 'Protected source; size and modification time checked'})
        issue = ISSUES.get(key, ('Reference only · no comparison required', 'No specific image discrepancy is recorded for this page. The selected TIFF is retained. You do not need to confirm the Master again. Optional image references are available below.'))
        if parent:
            issue = ('Linked detail · reference only', 'The existing parent link and redundant-detail decision are retained. No specific mismatch is recorded for this detail, so no repeat decision is required. The small website crop is optional evidence of framing and caption content; it is never a Master candidate.')
        return {'object': obj, 'mapping': mapping, 'archive': archive, 'decision': decision,
                'version': version_of(decision), 'candidates': candidates, 'assets': assets,
                'issue': issue, 'parent': parent, 'mount': mount,
                'details': [m for m in self.mappings.values() if m['parent_record_key'] == key],
                'metadata': {'title': self.archived.get(key, {}).get('title', obj['title']), 'description': self.archived.get(key, {}).get('description', ''),
                             'inventory_label': mapping['inventory_page_label'], 'title_label': mapping['archive_title_page_label'],
                             'website_label': mapping['wp_page_label'], 'label_note': mapping['page_label_note'],
                             'reading_label': next((r['proposed_page_label'] for r in self.order if r['record_key'] == key), '')},
                'work_differs_from_archive': bool(not parent and decision['selected_candidate_id'] != archive.get('selected_candidate_id'))}

    def web_bytes(self, key):
        if (key, 'web') in self.local:
            return self.get_local((key, 'web')).read_bytes()
        if key not in self.mappings:
            raise ReviewError('Unknown website image.')
        with self.cache_lock:
            if key in self.web_cache:
                self.web_cache.move_to_end(key)
                return self.web_cache[key]
        url = urlsplit(self.mappings[key]['wp_image_url'])
        if (url.scheme != 'http' or url.hostname not in ('192.168.2.31', '127.0.0.1', 'localhost')
                or url.port != 8082 or not url.path.startswith('/wp-content/uploads/')
                or url.query or url.fragment or '..' in url.path):
            raise ReviewError('Image URL is outside the mapped local website uploads.')
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open('http://127.0.0.1:8082' + url.path, timeout=12) as response:
            data = response.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise ReviewError('Website image exceeds the bounded preview size.')
        with self.cache_lock:
            self.web_cache[key] = data
            while sum(map(len, self.web_cache.values())) > 40 * 1024 * 1024:
                self.web_cache.popitem(last=False)
        return data

    def image(self, key, kind, candidate_id='', size=1800):
        if key not in self.mappings or size not in (320, 1800, 5000):
            raise ReviewError('Unknown image or unsupported preview size.')
        with self.preview_slots:
            if kind == 'web':
                source = io.BytesIO(self.web_bytes(key))
            elif kind in ('master', 'access'):
                source = self.get_local((key, kind))
            elif kind == 'candidate':
                _, _, candidates = self.record(key)
                candidate = next((c for c in candidates if c['candidate_id'] == candidate_id), None)
                if not candidate:
                    raise ReviewError('Candidate does not belong to this NEF record.')
                source = guarded_source(candidate)
            else:
                raise ReviewError('Unknown image role.')
            with Image.open(source) as im:
                if im.width * im.height > 60_000_000:
                    raise ReviewError('Image exceeds the bounded decoder size.')
                im = ImageOps.exif_transpose(im)
                im.thumbnail((size, size))
                output = io.BytesIO()
                im.convert('RGB').save(output, 'JPEG', quality=94)
                return output.getvalue()

    def save(self, payload):
        key = payload.get('record', '')
        _, _, candidates = self.record(key)
        action = payload.get('action')
        note = payload.get('note', '')
        if action not in ('note', 'unresolved', 'keep', 'select', 'redundant') or not isinstance(note, str) or len(note) > 2000:
            raise ReviewError('Choose an existing decision action and keep the note under 2000 characters.')
        candidate_id = payload.get('candidate', '')
        candidate = next((r for r in candidates if r['candidate_id'] == candidate_id), None)
        with self.lock:
            con = existing.connect()
            try:
                con.execute('BEGIN IMMEDIATE')
                mirrors_match(con)
                old = dict(con.execute('SELECT * FROM decisions WHERE record_key=?', (key,)).fetchone())
                if payload.get('version') != version_of(old):
                    raise ReviewError('This decision changed in another window. Reload before saving.')
                is_detail = bool(self.mappings[key]['parent_record_key'])
                if action == 'select':
                    if is_detail or old['decision_state'] == 'redundant_derivative':
                        raise ReviewError('A linked derivative cannot be promoted to a Master.')
                    if not candidate or candidate['extension'].lower() not in ('tif', 'tiff') or candidate['visual_index_state'] != 'indexed':
                        raise ReviewError('Select an existing readable page TIFF candidate. JPEG derivatives are comparison evidence.')
                    guarded_source(candidate)
                    con.execute("UPDATE decisions SET decision_state='selected_master',selected_candidate_id=? WHERE record_key=?", (candidate_id, key))
                    con.execute("""UPDATE candidate_dispositions SET disposition=CASE WHEN candidate_id=? THEN 'selected_master' ELSE 'redundant' END,
                                   decision_basis='Explicit NEF human Master selection; other candidates redundant' WHERE record_key=?""", (candidate_id, key))
                elif action == 'keep':
                    if old['decision_state'] != 'selected_master':
                        raise ReviewError('There is no selected Master to keep.')
                elif action == 'redundant':
                    if not is_detail or old['decision_state'] != 'redundant_derivative':
                        raise ReviewError('This action is limited to the existing linked detail records.')
                elif action == 'unresolved':
                    # The existing workbench forbids clearing accepted Masters.
                    # Leave this comparison question open as a note, preserving
                    # the accepted Master and parent relationships.
                    note = 'NEF comparison unresolved. ' + note.removeprefix('NEF comparison unresolved. ')
                con.execute('UPDATE decisions SET note=?,decided_by=?,decided_at=? WHERE record_key=?',
                            (note, self.actor, existing.utc_now(), key))
                invariant_check(con)
                con.commit()
                export_current(con)
                mirrors_match(con)
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
        return self.review(key)

    def check(self):
        with closing(existing.connect(read_only=True)) as con:
            quick = con.execute('PRAGMA quick_check').fetchone()[0]
            foreign = con.execute('PRAGMA foreign_key_check').fetchall()
            if quick != 'ok' or foreign:
                raise ReviewError('Existing database integrity check failed.')
            invariant_check(con)
            mirrors_match(con)
            total = con.execute('SELECT COUNT(*) FROM objects').fetchone()[0]
        for key in self.local:
            self.get_local(key)
        return {'passed': True, 'version': VERSION, 'nef_primary_images': len(self.order),
                'nef_linked_details': len(self.mappings) - len(self.order), 'existing_objects_preserved': total,
                'sqlite_and_csv_agree': True, 'source_mount': source_mount(),
                'archive_writes': False, 'deletion_authorized': False}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, app):
        self.app = app
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        bound_port = self.server_address[1]
        self.expected_hosts = {f'127.0.0.1:{bound_port}', f'localhost:{bound_port}'}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, data, mime='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in self.server.expected_hosts

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Local workbench host required.'})
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        get = lambda name, default='': args.get(name, [default])[0]
        try:
            if url.path == '/':
                body = HTML.read_bytes().replace(b'__CSRF_TOKEN__', self.server.token.encode())
                self.send(200, body, 'text/html; charset=utf-8')
            elif url.path == '/api/overview':
                self.send(200, self.server.app.overview())
            elif url.path == '/api/review':
                self.send(200, self.server.app.review(get('record')))
            elif url.path == '/image':
                data = self.server.app.image(get('record'), get('kind'), get('candidate'), int(get('size', '1800')))
                self.send(200, data, 'image/jpeg')
            elif url.path == '/health':
                self.send(200, self.server.app.check())
            else:
                self.send(404, {'error': 'Unknown workbench route.'})
        except (ReviewError, ValueError, OSError, sqlite3.Error) as e:
            self.send(400, {'error': str(e)})

    def do_POST(self):
        origin = self.headers.get('Origin', '')
        if (not self.valid_host() or origin not in {f'http://{host}' for host in self.server.expected_hosts}
                or not secrets.compare_digest(self.headers.get('X-NEF-Token', ''), self.server.token)):
            return self.send(403, {'error': 'Work from the local NEF page to save a decision.'})
        if self.path != '/api/decision':
            return self.send(404, {'error': 'Unknown decision route.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16000 or self.headers.get('Content-Type') != 'application/json':
                raise ReviewError('A bounded JSON decision is required.')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ReviewError('Invalid decision body.')
            self.send(200, self.server.app.save(payload))
        except (ReviewError, ValueError, OSError, sqlite3.Error) as e:
            self.send(409, {'error': str(e)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--actor', default='vladimir')
    args = parser.parse_args()
    app = NEFWorkbench(args.actor)
    print(json.dumps(app.check(), ensure_ascii=False), flush=True)
    if args.check:
        return
    server = Server(args.port, app)
    print(f'NEF workbench: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
