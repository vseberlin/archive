#!/usr/bin/env python3
"""Local Bibliothek review: select one representation, redundant alternatives, or unresolved."""
import argparse
import json
import mimetypes
import os
import secrets
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from prepare_bibliothek import ROOT, SOURCE, guard, read_csv, csv_bytes, now, run
from ui_bibliothek import esc, url, viewer, comparison_tools, text_difference, STYLE, SCRIPT

LOCK = threading.Lock()
RENDER_LIMIT = threading.BoundedSemaphore(2)


def atomic(name, payload):
    target = ROOT / name
    temporary = ROOT / ('.' + name + '.tmp')
    with temporary.open('wb') as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, target)


class Review:
    def __init__(self):
        guard()
        self.objects = read_csv('objects.csv')
        self.candidates = read_csv('candidates.csv')
        self.resources = read_csv('md-resources.csv')
        self.by_id = {r['object_key']: r for r in self.objects}
        self.by_candidate = {r['candidate_id']: r for r in self.candidates}
        self.local = json.loads((ROOT / 'local-evidence.json').read_text())

    def decisions(self):
        return {r['object_key']: r for r in read_csv('master-decisions.csv')}

    def member(self, key):
        kind, _, identifier = key.partition(':')
        if kind == 'resource':
            r = next(r for r in self.resources if r['resource_id'] == identifier)
            return {'path': r['work_path'], 'pages': int(r['pages']),
                    'size_bytes': int(r['size_bytes']), 'mtime_ns': int(r['mtime_ns'])}
        if kind == 'candidate':
            c = self.by_candidate[identifier]
            return next(m for m in json.loads(c['members_json']) if m['role'] == 'book_scan')
        if kind == 'local':
            r = next(r for r in self.local if r['document_id'] == identifier)
            return {'path': r['source_path'], 'pages': int(r.get('pages') or 1),
                    'size_bytes': r['size_bytes'], 'mtime_ns': r['mtime_ns']}
        raise ValueError('Unknown review item')

    def checked_path(self, member):
        guard()
        p = Path(member['path']).resolve(strict=True)
        if not (p.is_relative_to(ROOT) or p.is_relative_to(SOURCE.resolve())):
            raise ValueError('Outside approved review roots')
        st = p.stat()
        if (st.st_size, st.st_mtime_ns) != (int(member['size_bytes']), int(member['mtime_ns'])):
            raise ValueError('Source changed since inspection')
        return p

    def preview(self, key, page, text=False):
        member = self.member(key)
        p = self.checked_path(member)
        if not 1 <= page <= int(member['pages']):
            raise ValueError('Page outside this document')
        if p.suffix.lower() == '.pdf':
            return render_pdf(str(p), int(member['size_bytes']), int(member['mtime_ns']), page, text)
        if text:
            raise ValueError('Text comparison requires PDFs')
        return p.read_bytes()

    def dispositions(self, decisions):
        return [{'object_key': c['object_key'], 'candidate_id': c['candidate_id'],
                 'disposition': 'excluded' if c['eligible'] != 'true' else (('selected_master' if decisions[c['object_key']]['selected_candidate_id'] == c['candidate_id'] else 'redundant') if decisions[c['object_key']]['selected_candidate_id'] else 'unresolved'),
                 'source_paths_json': json.dumps([m['path'] for m in json.loads(c['members_json'])], ensure_ascii=False),
                 'deletion_authorized': 'false'} for c in self.candidates]

    def check(self):
        guard()
        d = self.decisions()
        if set(d) != set(self.by_id) or len(self.by_id) != len(self.objects):
            raise ValueError('Object and decision records disagree')
        if len(self.by_candidate) != len(self.candidates):
            raise ValueError('Duplicate candidate identifier')
        for key, row in d.items():
            if row['deletion_authorized'] != 'false':
                raise ValueError('Deletion is not authorized')
            selected = row['selected_candidate_id']
            if selected and (selected not in self.by_candidate or self.by_candidate[selected]['object_key'] != key or self.by_candidate[selected]['eligible'] != 'true'):
                raise ValueError('Invalid Master selection')
        for candidate in self.candidates:
            for member in json.loads(candidate['members_json']):
                self.checked_path(member)
        if self.dispositions(d) != read_csv('candidate-dispositions.csv'):
            raise ValueError('Readable decisions and candidate dispositions disagree')
        return {'result': 'ok', 'unit': 'bibliothek', 'objects': len(self.objects),
                'md_resources': len(self.resources), 'candidate_representations': len(self.candidates),
                'resolved_objects': sum(bool(r['selected_candidate_id']) for r in d.values()),
                'selected_master_content_mismatches': ['BIB-1002: selected MD attachment contains an unrelated newspaper page'] if d.get('BIB-1002', {}).get('selected_candidate_id') == 'BIB-1002:md' else [],
                'local_growth_leads': sum(not r['object_key'] for r in self.local),
                'accession_performed': False, 'deletion_authorized': False}

    def save(self, key, candidate_id, note):
        if (ROOT / 'reconciliation-decisions.jsonl').exists() or (ROOT / 'reconciliation-decisions.jsonl').is_symlink():
            raise ValueError('This unit uses consolidated decisions; the legacy workbench is read-only')
        if key not in self.by_id or len(note) > 2000:
            raise ValueError('Invalid object or note')
        with LOCK:
            guard()
            d = self.decisions()
            if candidate_id:
                candidate = self.by_candidate.get(candidate_id)
                if not candidate or candidate['object_key'] != key or candidate['eligible'] != 'true':
                    raise ValueError('Choose an eligible candidate for this book')
                for member in json.loads(candidate['members_json']):
                    self.checked_path(member)
            d[key].update(selected_candidate_id=candidate_id, decision='selected_master' if candidate_id else 'unresolved',
                          actor='Vladimir', decided_at=now(), note=note.strip())
            atomic('master-decisions.csv', csv_bytes(list(d.values())))
            atomic('candidate-dispositions.csv', csv_bytes(self.dispositions(d)))


@lru_cache(maxsize=20)
def render_pdf(path, size, mtime, page, text):
    # Review pages stay in RAM; no access derivative is generated.
    command = (['pdftotext', '-f', str(page), '-l', str(page), '-layout', path, '-'] if text else
               ['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', '1600', '-jpeg', path])
    with RENDER_LIMIT:
        return run(command, timeout=45)


def page(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bibliothek · Reconciliation</title><style>' + STYLE + '.decision-panel{position:static;box-shadow:none}.decision-panel label{display:block;margin:12px 0}.decision-panel select{max-width:100%}.controls label{max-width:100%}.controls select{max-width:100%}</style></head><body><header><a href="/">Bibliothek</a><a href="/local">Local growth candidates</a><a href="/scope">Scope and evidence</a></header><main>' + body + '</main><script>' + SCRIPT + '</script></body></html>').encode()


class Handler(BaseHTTPRequestHandler):
    def send(self, payload, status=200, kind='text/html; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            args = parse_qs(parsed.query)
            r = self.server.review
            if parsed.path == '/health':
                self.send(json.dumps(r.check()).encode(), kind='application/json')
            elif parsed.path == '/':
                d = r.decisions()
                body = '<h1>Bibliothek</h1><p>Three accepted starting records. Bibliothek can grow locally; additional books do not need a museum-digital record to belong to the unit.</p><p>' + str(sum(bool(x['selected_candidate_id']) for x in d.values())) + ' of ' + str(len(d)) + ' Master choices saved.</p><div class="cards">'
                for o in r.objects:
                    key = o['object_key']
                    pdf = next(x for x in r.resources if x['object_key'] == key and x['type'] == 'text')
                    representative = next((x for x in r.candidates if x['object_key'] == key and x['origin'] == 'protected_usb'), None)
                    preview_key = 'candidate:' + representative['candidate_id'] if key == 'BIB-1002' and representative else 'resource:' + pdf['resource_id']
                    reason = 'MD attachment is an unrelated newspaper page. Three identical local copies contain the correct Urbanitzky scan.' if key == 'BIB-1002' else 'The local PDF is byte-identical to the MD PDF. Select one representation and retain the scan extent.'
                    body += '<article class="card"><p class="eyebrow">' + esc(key) + '</p><img class="thumb" src="' + esc(url('/preview', key=preview_key, page=1)) + '" alt="' + esc(key) + ' title page"><h2>' + esc(o['title']) + '</h2><p class="badge">' + ('Master selected' if d[key]['selected_candidate_id'] else 'Master unresolved') + '</p><p class="reason">' + esc(reason) + '</p><p><a class="button" href="' + esc(url('/object', id=key)) + '">Compare and decide</a></p></article>'
                self.send(page(body + '</div>'))
            elif parsed.path == '/object':
                self.send(page(self.object_page(args)))
            elif parsed.path == '/preview':
                self.send(r.preview(args['key'][0], int(args.get('page', ['1'])[0])), kind='image/jpeg')
            elif parsed.path == '/file':
                member = r.member(args['key'][0])
                path = r.checked_path(member)
                self.send(path.read_bytes(), kind=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            elif parsed.path == '/text-difference':
                left = r.preview(args['left'][0], int(args['left_page'][0]), text=True).decode(errors='replace')
                right = r.preview(args['right'][0], int(args['right_page'][0]), text=True).decode(errors='replace')
                self.send(text_difference(left, right).encode())
            elif parsed.path == '/local':
                body = '<h1>Local growth candidates</h1><p>These indexed references need a separate relevance and object-identity review before joining Bibliothek. They do not change the three starting records.</p>'
                for x in r.local:
                    if x['object_key']:
                        continue
                    body += '<section><h2>' + esc(x['filename']) + '</h2><p class="reason">' + esc(x['search_basis']) + '</p><p><code>' + esc(x['source_path']) + '</code></p><p><a target="_blank" href="' + esc(url('/file', key='local:' + x['document_id'])) + '">Open original PDF ↗</a></p><details><summary>Indexed text and existing unit relationships</summary><pre>' + esc(x['header_text'][:10000]) + '</pre><pre>' + esc(json.dumps(x['unit_links'], ensure_ascii=False, indent=2)) + '</pre></details></section>'
                self.send(page(body))
            elif parsed.path == '/scope':
                self.send(page('<h1>Scope and evidence</h1><pre>' + esc((ROOT / '00_README.md').read_text()) + '</pre>'))
            else:
                self.send(b'Not found', 404, 'text/plain')
        except (ValueError, KeyError, StopIteration, AssertionError) as error:
            self.send(esc(str(error)).encode(), 400, 'text/plain')
        except (OSError, RuntimeError) as error:
            self.send(esc(str(error)).encode(), 503, 'text/plain')

    def object_page(self, args):
        r = self.server.review
        key = args['id'][0]
        o = r.by_id[key]
        d = r.decisions()[key]
        candidates = [x for x in r.candidates if x['object_key'] == key]
        local = [x for x in candidates if x['origin'] == 'protected_usb']
        selected_comparison = args.get('compare', [local[0]['candidate_id'] if local else ''])[0]
        comparison = next((x for x in local if x['candidate_id'] == selected_comparison), None)
        rr = [x for x in r.resources if x['object_key'] == key]
        pdf = next(x for x in rr if x['type'] == 'text')
        md = next((x for x in rr if x['resource_id'] == args.get('resource', [pdf['resource_id']])[0]), pdf)
        body = '<p class="eyebrow">' + esc(key) + '</p><h1>' + esc(o['title']) + '</h1><p><a target="_blank" href="' + esc(o['object_url']) + '">Museum-digital record ↗</a> · <a href="/">All books</a></p>'
        body += '<section class="reason"><b>Why this is here</b><p>The book is included in the starting scope. The MD scan and local title-page/contents scan are candidate representations. Their digital extent must remain explicit; the physical book description does not establish a full scan.</p><p>' + esc(comparison['comparison'] if comparison else '') + '</p></section>'
        body += '<form class="controls" method="get"><input type="hidden" name="id" value="' + esc(key) + '"><label>MD resource <select name="resource">' + ''.join('<option value="' + x['resource_id'] + '"' + (' selected' if x == md else '') + '>' + esc(x['description'] or Path(x['work_path']).name) + ' · ' + x['pages'] + ' page(s)</option>' for x in rr) + '</select></label>'
        if local:
            body += '<label>Local candidate <select name="compare">' + ''.join('<option value="' + esc(x['candidate_id']) + '"' + (' selected' if x == comparison else '') + '>' + esc(x['label']) + '</option>' for x in local) + '</select></label>'
        body += '<button>Show comparison</button></form>'
        body += comparison_tools() + '<div class="compare-grid">'
        body += self.viewer('resource:' + md['resource_id'], 'Museum-digital', int(md['pages']))
        if comparison:
            m = r.member('candidate:' + comparison['candidate_id'])
            body += self.viewer('candidate:' + comparison['candidate_id'], 'Local scan', int(m['pages']))
        body += '</div><section class="diff-result" id="difference-result" hidden><h2>Visual difference</h2><p role="status"></p><canvas aria-label="Difference between displayed pages"></canvas></section><section id="text-result" hidden aria-live="polite"></section>'
        if comparison:
            m = r.member('candidate:' + comparison['candidate_id'])
            tech = json.loads(Path(m['technical_path']).read_text())
            body += '<details><summary>Technical comparison and provenance</summary><table><tr><th></th><th>MD PDF</th><th>Local PDF</th></tr>'
            for label, a, b in [('Pages', pdf['pages'], m['pages']), ('Bytes', pdf['size_bytes'], m['size_bytes']), ('Median embedded image PPI', pdf['median_ppi'], tech['median_ppi']), ('Stored SHA-256', pdf['sha256'], m['sha256'])]:
                body += '<tr><th>' + esc(label) + '</th><td>' + esc(a) + '</td><td>' + esc(b) + '</td></tr>'
            body += '</table><p><code>' + esc(m['path']) + '</code></p><p>' + esc(comparison['basis']) + '</p></details>'
        body += '<details><summary>Book metadata and all MD resources</summary><pre>' + esc(o['description']) + '</pre><ul>'
        body += ''.join('<li>' + esc(x['description'] or Path(x['work_path']).name) + ' · ' + x['pages'] + ' page(s) · ' + esc(x['rights']) + '</li>' for x in rr) + '</ul></details>'
        extent_note = 'The cover image remains alongside whichever PDF is selected. ' if key == 'BIB-1001' else ''
        body += '<form class="decision-panel" method="post" action="/save"><input type="hidden" name="token" value="' + self.server.token + '"><input type="hidden" name="object_key" value="' + esc(key) + '"><h2>One Master representation</h2><p>' + extent_note + 'Other candidate representations become redundant when one Master is chosen.</p><label>Master <select name="candidate_id"><option value="">Leave unresolved</option>'
        body += ''.join('<option value="' + esc(x['candidate_id']) + '"' + (' selected' if d['selected_candidate_id'] == x['candidate_id'] else '') + (' disabled' if x['eligible'] != 'true' else '') + '>' + esc(x['label']) + (' — wrong attachment, unavailable' if x['eligible'] != 'true' else '') + '</option>' for x in candidates)
        body += '</select></label><label>Short note<textarea name="note" maxlength="2000">' + esc(d['note']) + '</textarea></label><button>Save choice</button><p class="muted">This saves reconciliation decisions only. Accession follows separate authorization.</p></form>'
        return body

    def viewer(self, key, title, pages):
        markup = viewer(key, title, pages, '')
        return markup.replace(esc(url('/file', path='')), esc(url('/file', key=key)))

    def do_POST(self):
        try:
            if self.path != '/save':
                raise ValueError('Unknown action')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 12000:
                raise ValueError('Invalid form size')
            args = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            if not secrets.compare_digest(args.get('token', [''])[0], self.server.token):
                raise ValueError('Reload the review page before saving')
            key = args['object_key'][0]
            self.server.review.save(key, args['candidate_id'][0], args.get('note', [''])[0])
            self.send_response(303)
            self.send_header('Location', url('/object', id=key))
            self.end_headers()
        except (ValueError, KeyError, OSError) as error:
            self.send(esc(str(error)).encode(), 400, 'text/plain')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--port', type=int, default=8791)
    args = parser.parse_args()
    review = Review()
    print(json.dumps(review.check()), flush=True)
    if not args.check:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        server.review = review
        server.token = secrets.token_urlsafe(24)
        print('Bibliothek workbench: http://127.0.0.1:' + str(args.port), flush=True)
        server.serve_forever()
