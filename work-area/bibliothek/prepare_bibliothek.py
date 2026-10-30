#!/usr/bin/env python3
"""Bounded Bibliothek evidence capture; sources and accession units stay read-only."""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = Path('/media/vladimir/Elements/restore/archive')
DOC_INDEX = Path('/mnt/volume/iss-archive-work/document-index/document-index.sqlite')
sys.path.insert(0, '/home/vladimir/wp/tools/archive-preservation')
from md_export_discovery import fetch_page, USER_AGENT


def guard():
    mounts = json.loads(subprocess.check_output([
        'findmnt', '-J', '-T', str(SOURCE), '-o', 'SOURCE,TARGET,FSTYPE,OPTIONS'
    ]))['filesystems']
    if len(mounts) != 1:
        raise ValueError('Ambiguous source mount')
    m = mounts[0]
    if (m['source'], m['target'], m['fstype']) != ('/dev/sdf1', '/media/vladimir/Elements', 'ntfs3') or 'ro' not in m['options'].split(','):
        raise ValueError('Exact protected Elements mount required')
    return m


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def write_json(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def read_csv(name):
    p = ROOT / name
    if not p.exists():
        return []
    with p.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def csv_bytes(rows, fields=None):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fields or list(rows[0]), quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def write_csv(name, rows, fields=None):
    (ROOT / name).write_bytes(csv_bytes(rows, fields))


def run(args, timeout=90):
    p = subprocess.run(args, capture_output=True, timeout=timeout,
                       env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
    if p.returncode:
        raise ValueError(p.stderr.decode(errors='replace')[:700])
    return p.stdout


def inspect(path):
    if path.suffix.lower() == '.pdf':
        info = run(['pdfinfo', str(path)]).decode(errors='replace')
        pages = int(re.search(r'^Pages:\s+(\d+)', info, re.M)[1])
        images = run(['pdfimages', '-list', str(path)]).decode(errors='replace')
        ppi = []
        for line in images.splitlines():
            t = line.split()
            if len(t) >= 14 and t[0].isdigit() and t[2] == 'image':
                try:
                    ppi.append(min(float(t[12]), float(t[13])))
                except ValueError:
                    pass
        text = run(['pdftotext', '-f', '1', '-l', str(min(4, pages)), '-layout', str(path), '-']).decode(errors='replace')
        return {'pages': pages, 'median_ppi': sorted(ppi)[len(ppi)//2] if ppi else '',
                'pdfinfo': info, 'text_sample': text[:24000],
                'inspection': 'PDF structure, embedded image resolution, first four pages of embedded text; no full-book completeness assertion'}
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        return {'pages': 1, 'width': im.width, 'height': im.height, 'median_ppi': '',
                'text_sample': '', 'inspection': 'Full image decode and dimensions'}


def capture():
    guard()
    url = 'https://berlin.museum-digital.de/export/json/collection:556?limit=100&offset=0'
    if not (ROOT / 'md-export.json').exists():
        status, payload, attempts = fetch_page(url, timeout=45, retries=2, retry_delay=2)
        data = json.loads(payload)
        if data['results']['total'] != 3:
            raise ValueError('MD collection scope changed; review before continuing')
        (ROOT / 'md-export.json').write_bytes(payload)
        write_json('md-request.json', {'url': url, 'status': status, 'attempts': attempts,
                                     'captured_at': now(), 'user_agent': USER_AGENT})
    data = json.loads((ROOT / 'md-export.json').read_text())
    objects = list(data['results']['objects'].values())
    if {str(o['object_id']) for o in objects} != {'49999', '50002', '50003'}:
        raise ValueError('Unexpected starting records')
    resources = []
    old = {r['resource_id']: r for r in read_csv('md-resources.csv')}
    for obj in objects:
        for resource in sorted(obj['object_images'], key=lambda r: int(r.get('order') or 0)):
            rid = str(resource['quell_id'])
            name = resource['filename_loc']
            source_url = name if name.startswith(('http://', 'https://')) else 'https://asset.museum-digital.org/berlin/' + urllib.parse.quote(resource['folder'].strip('/') + '/' + name)
            suffix = '.pdf' if resource['type'] == 'text' else Path(name).suffix.lower()
            path = ROOT / (obj['object_inventory_number'] + '-md-' + rid + suffix)
            if rid in old and path.is_file() and path.stat().st_size == int(old[rid]['size_bytes']):
                digest = old[rid]['sha256']
            else:
                time.sleep(1)
                status, payload, attempts = fetch_page(source_url, timeout=90, retries=2, retry_delay=2)
                if suffix == '.pdf' and not payload.startswith(b'%PDF-'):
                    raise ValueError('Expected a PDF: ' + source_url)
                path.write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
            tech = inspect(path)
            techname = path.stem + '-technical.json'
            write_json(techname, tech)
            resources.append({'object_key': obj['object_inventory_number'], 'md_object_id': str(obj['object_id']),
                              'resource_id': rid, 'sequence': resource['order'], 'type': resource['type'],
                              'description': resource.get('beschreibung', ''), 'source_url': source_url,
                              'work_path': str(path), 'size_bytes': path.stat().st_size,
                              'mtime_ns': path.stat().st_mtime_ns, 'sha256': digest,
                              'pages': tech['pages'], 'median_ppi': tech['median_ppi'],
                              'rights': resource.get('rights', ''), 'owner': resource.get('owner', ''),
                              'technical_path': str(ROOT / techname), 'deletion_authorized': 'false'})
            write_csv('md-resources.csv', resources)
            print('Captured', obj['object_inventory_number'], rid, tech['pages'], 'page(s)', flush=True)
    return objects, resources


def discover(resources):
    guard()
    db = sqlite3.connect(DOC_INDEX.as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    queries = {
        'book_identity': 'Urbanitzky OR "Wilhelm Beck" OR "Arthur Wilke" OR "BIB-1001" OR "BIB-1002" OR "BIB-1003" OR "Elektrizität im Dienste" OR "Elektrizität und ihre Technik" OR "Elektrizität ihre Erzeugung"',
        'library_path': 'filename : Bibliothek OR relative_path : Bibliothek',
    }
    found = {}
    query_counts = {}
    for reason, query in queries.items():
        rows = list(db.execute('SELECT d.* FROM document_fts f JOIN documents d ON d.document_id=f.document_id WHERE document_fts MATCH ?', (query,)))
        query_counts[reason] = len(rows)
        for row in rows:
            item = dict(row)
            item['search_basis'] = reason
            found[item['document_id']] = item
    for resource in resources:
        for row in db.execute('SELECT * FROM documents WHERE content_sha256=?', (resource['sha256'],)):
            item = dict(row)
            item['search_basis'] = 'Exact stored hash of MD resource ' + resource['resource_id']
            found[item['document_id']] = item
    for item in found.values():
        p = Path(item['source_path']).resolve(strict=True)
        if not p.is_relative_to(SOURCE.resolve()):
            raise ValueError('Search returned a path outside the protected source')
        st = p.stat()
        item['current_source_check'] = 'unchanged' if (st.st_size, st.st_mtime_ns) == (item['size_bytes'], item['mtime_ns']) else 'changed'
        name = p.name.lower()
        key = 'BIB-1001' if name.startswith('beck-') else 'BIB-1002' if name == 'urbanitzky.pdf' else 'BIB-1003' if name == 'bib-wilke.pdf' else ''
        exact = [r['object_key'] for r in resources if r['sha256'] == item['content_sha256']]
        if not key and len(set(exact)) == 1:
            key = exact[0]
        item['object_key'] = key
        item['relevance'] = 'same_book_candidate' if key else 'needs_review_for_local_growth'
        item['match_state'] = 'matched' if key else 'not_searched'
        item['unit_links'] = [dict(r) for r in db.execute('SELECT unit,relation_state,basis FROM unit_links WHERE document_id=?', (item['document_id'],))]
    db.close()
    write_json('local-evidence.json', list(found.values()))
    write_json('search-summary.json', {'searched_at': now(), 'index': str(DOC_INDEX), 'queries': queries,
                                     'query_counts': query_counts, 'source_paths': len(found),
                                     'distinct_stored_hashes': len({x['content_sha256'] for x in found.values()}),
                                     'search_scope': 'Existing document full-text/path index and exact MD-resource hashes; no recursive source scan',
                                     'deletion_authorized': False})
    return list(found.values())


def prepare():
    objects, resources = capture()
    local = discover(resources)
    saved = {r['object_key']: r for r in read_csv('master-decisions.csv')}
    rows, candidates = [], []
    for obj in objects:
        key = obj['object_inventory_number']
        rows.append({'object_key': key, 'md_object_id': str(obj['object_id']),
                     'title': obj['object_name'], 'description': obj['object_description'],
                     'object_url': 'https://berlin.museum-digital.de/object/' + str(obj['object_id']),
                     'scope_decision': 'included', 'scope_basis': 'Vladimir approved the three MD records as the Bibliothek starting scope on 2026-09-11',
                     'metadata_basis': 'MD capture; local evidence retained for comparison'})
        rr = [r for r in resources if r['object_key'] == key]
        members = [{'path': r['work_path'], 'size_bytes': r['size_bytes'], 'mtime_ns': r['mtime_ns'],
                    'sha256': r['sha256'], 'role': 'cover' if r['type'] == 'image' else 'book_scan',
                    'pages': r['pages'], 'technical_path': r['technical_path'], 'source_url': r['source_url']} for r in rr]
        human_selected_md = saved.get(key, {}).get('selected_candidate_id') == key + ':md'
        candidates.append({'object_key': key, 'candidate_id': key + ':md', 'label': 'Museum-digital representation',
                           'origin': 'museum_digital', 'eligible': 'false' if key == 'BIB-1002' and not human_selected_md else 'true', 'members_json': json.dumps(members, ensure_ascii=False),
                           'basis': 'All existing MD resources retained together in their recorded order',
                           'comparison': ('User explicitly selected the MD representation. Its single newspaper page is a known content mismatch, not a verified Urbanitzky book scan.' if human_selected_md else 'Wrong attachment: visual inspection confirms one unrelated newspaper page, not Urbanitzky. Retained as MD evidence; not eligible as this book\'s Master.') if key == 'BIB-1002' else 'Inspect the scan extent; a catalogue book record does not imply a full digital book.'})
        groups = {}
        for item in local:
            if item['object_key'] == key:
                groups.setdefault(item['content_sha256'], []).append(item)
        for digest, group in groups.items():
            sample = next((x for x in group if '/WF/Archiv/' in x['source_path']), group[0])
            if sample['current_source_check'] != 'unchanged':
                raise ValueError('Candidate changed; inspect before offering a Master choice')
            tech = inspect(Path(sample['source_path']))
            techpath = ROOT / (key + '-local-' + digest[:12] + '-technical.json')
            tech['index_ocr_sample'] = sample['header_text'][:24000]
            write_json(techpath.name, tech)
            pdf = next(r for r in rr if r['type'] == 'text')
            equality = digest == pdf['sha256']
            for item in group:
                item['technical_path'] = str(techpath)
                item['pages'] = tech['pages']
                item['md_pdf_sha256_equal'] = equality
                # One selected file plus provenance for exact copies. Other copies remain explicit alternatives.
                local_members = [m for m in members if m['role'] == 'cover'] + [{
                    'path': item['source_path'], 'size_bytes': item['size_bytes'], 'mtime_ns': item['mtime_ns'],
                    'sha256': digest, 'role': 'book_scan', 'pages': tech['pages'],
                    'technical_path': str(techpath), 'source_url': ''}]
                candidates.append({'object_key': key, 'candidate_id': key + ':' + item['document_id'],
                                   'label': Path(item['source_path']).name + ' · ' + str(Path(item['source_path']).parent.relative_to(SOURCE)),
                                   'origin': 'protected_usb', 'eligible': 'true',
                                   'members_json': json.dumps(local_members, ensure_ascii=False),
                                   'basis': 'Book identity from indexed title-page OCR; stored hash reused after size/mtime check',
                                   'comparison': 'PDF is byte-identical to the MD PDF (stored SHA-256).' if equality else 'Correct Urbanitzky title page and contents: five scan pages at 300 PPI. MD links to one unrelated newspaper page at 150 PPI; choose one of the three identical local copies.' if key == 'BIB-1002' else 'PDF hash differs from MD; compare scan extent, page content and technical quality.'})
    write_json('local-evidence.json', local)
    write_csv('objects.csv', rows)
    write_csv('candidates.csv', candidates)
    if not (ROOT / 'master-decisions.csv').exists():
        write_csv('master-decisions.csv', [{'object_key': r['object_key'], 'selected_candidate_id': '', 'decision': 'unresolved',
                                          'actor': '', 'decided_at': '', 'note': '', 'deletion_authorized': 'false'} for r in rows])
    from workbench_bibliothek import Review
    review = Review()
    write_csv('candidate-dispositions.csv', review.dispositions(review.decisions()))
    write_json('preflight.json', review.check())
    print(json.dumps(review.check(), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    prepare()
