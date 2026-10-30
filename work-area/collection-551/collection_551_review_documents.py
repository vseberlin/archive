#!/usr/bin/env python3
"""Bounded FEB documentation review using the existing index and file internals."""
import csv
import json
import re
import sqlite3
import subprocess
import struct
import zlib
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from pathlib import Path
from zipfile import ZipFile

import collection_551_workbench as wb

ROOT = wb.ROOT
USB = Path('/media/vladimir/Elements/restore/archive')
INDEX = Path('/mnt/volume/iss-archive-work/document-index/document-index.sqlite')
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
WORDS = re.compile(r'Forschungs.{0,12}(?:und.{0,8})?Entwicklungsbericht|Forschungsberichte|FEB[- ]?0(?:08|68)|2400 Berichten|2[.,]400 Berichte|Patentbücher', re.I)


def readable_local_xml(path):
    """Read intact local ZIP members when the central directory is damaged.

    This is partial content inspection, not source repair. Accept only complete
    XML members with their recorded CRC; never reconstruct an Office file.
    """
    raw = path.read_bytes(); recovered = []
    for match in re.finditer(b'PK\x03\x04', raw):
        start = match.start()
        try:
            _, _, flags, method, _, _, crc, compressed, _, namesize, extrasize = struct.unpack_from('<I5H3I2H', raw, start)
            name = raw[start+30:start+30+namesize].decode('utf-8')
            if name not in {'word/document.xml', 'xl/sharedStrings.xml', 'xl/workbook.xml'}: continue
            if flags & 8 or not compressed or compressed > 30_000_000: continue
            offset = start+30+namesize+extrasize
            payload = raw[offset:offset+compressed]
            data = zlib.decompress(payload, -15) if method == 8 else payload if method == 0 else b''
            if not data or zlib.crc32(data) & 0xffffffff != crc: continue
            node = ET.fromstring(data)
            text = '\n'.join(''.join(p.itertext()) for p in node.findall('.//w:p', NS)) if name == 'word/document.xml' else ' '.join(node.itertext())
            recovered.append({'member': name, 'offset': start, 'crc_verified': True, 'text': text})
        except (ValueError, UnicodeError, struct.error, zlib.error, ET.ParseError): continue
    return recovered


def extract(path, cached):
    result = {'method': '', 'text': '', 'properties': {}, 'condition': 'readable', 'errors': []}
    if path.suffix.lower() in {'.docx', '.xlsx'}:
        result['method'] = 'direct OOXML members and properties'
        try:
            with ZipFile(path) as z:
                try:
                    bad = z.testzip()
                    if bad: result['errors'].append('ZIP CRC failure: ' + bad)
                except Exception as e: result['errors'].append('ZIP integrity: ' + str(e))
                if path.suffix.lower() == '.docx': names = ['word/document.xml']
                else: names = ['xl/sharedStrings.xml', 'xl/workbook.xml'] + [n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+.xml', n)]
                texts = []
                for name in names:
                    if name not in z.namelist(): continue
                    try:
                        node = ET.fromstring(z.read(name))
                        if name == 'word/document.xml':
                            value = '\n'.join(''.join(p.itertext()) for p in node.findall('.//w:p', NS))
                        else: value = ' '.join(node.itertext())
                        texts.append('[' + name + ']\n' + value)
                    except Exception as e: result['errors'].append(name + ': ' + str(e))
                result['text'] = '\n'.join(texts)
                if 'docProps/core.xml' in z.namelist():
                    try:
                        result['properties'] = {n.tag.rsplit('}', 1)[-1]: n.text or '' for n in ET.fromstring(z.read('docProps/core.xml'))}
                    except Exception as e: result['errors'].append('core properties: ' + str(e))
        except Exception as e: result['errors'].append(str(e))
        if not result['text'] and result['errors']:
            local = readable_local_xml(path)
            if local:
                result['text'] = '\n'.join('[' + r['member'] + '; local ZIP CRC verified]\n' + r['text'] for r in local)
                result['method'] += '; intact local ZIP XML members inspected despite damaged central structure'
                result['properties']['intact_local_members'] = [{'member':r['member'],'offset':r['offset'],'crc_verified':True} for r in local]
    elif path.suffix.lower() == '.pdf':
        result['method'] = 'direct pdfinfo and full embedded-text extraction; existing partial OCR where necessary'
        info = subprocess.run(['pdfinfo', str(path)], capture_output=True, text=True, timeout=60)
        result['properties'] = dict(line.split(':', 1) for line in info.stdout.splitlines() if ':' in line)
        result['properties'] = {k: v.strip() for k, v in result['properties'].items()}
        text = subprocess.run(['pdftotext', '-layout', str(path), '-'], capture_output=True, text=True, timeout=120)
        result['text'] = text.stdout
        if info.returncode or text.returncode: result['errors'].append((info.stderr + text.stderr)[:1000])
        if len(result['text'].strip()) < 80:
            result['text'] = cached
            result['method'] = 'direct pdfinfo; reuse existing indexed partial OCR (scope recorded in index parser)'
    elif path.suffix.lower() in {'.csv', '.txt'}:
        raw = path.read_bytes()
        result['text'] = raw.decode('utf-8-sig', errors='replace')
        result['method'] = 'direct original text bytes'
    else:
        raw = path.read_bytes()
        fragments = []
        for encoding in ['utf-16le', 'cp1252']:
            decoded = raw.decode(encoding, errors='ignore')
            fragments += [m.group(0) for m in re.finditer(r'[\x09\x0b\x0d\x20-\x7e\u00a0-\u024f]{20,}', decoded)]
        result['text'] = '\n'.join(fragments) or cached
        result['method'] = 'existing index lead; direct UTF-16LE/Windows-1252 binary text fragments and signature; partial extraction only'
        result['properties']['file_signature_hex'] = raw[:16].hex()
        result['condition'] = 'partial text evidence; legacy binary Office format not fully rendered'
        if path.suffix.lower() == '.doc' and not raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
            result['errors'].append('Incorrect compound Office file signature; readable fragments, if any, are partial evidence only.')
    if result['errors']:
        result['condition'] = 'damaged; partially readable internal members' if result['text'] else 'unreadable or incorrect file signature'
    return result


def main():
    wb.source_guard()
    c = sqlite3.connect(f'file:{INDEX}?mode=ro&immutable=1', uri=True)
    c.row_factory = sqlite3.Row
    hits = json.loads((ROOT / 'accession-document-content-candidates.json').read_text())
    rows = {r['document_id']: r for r in hits}
    columns = 'document_id,source_path,extension,content_sha256,size_bytes,mtime_ns,parser,extraction_state'
    metadata_sources = wb.read_csv(ROOT / 'metadata-source-documents.csv')
    seed_paths = {r['source_path'] for r in metadata_sources}
    for path in seed_paths:
        row = c.execute(f'SELECT {columns} FROM documents WHERE source_path=?', (path,)).fetchone()
        assert row, path
        rows[row['document_id']] = dict(row)
    # Indexed path leads supplement content search only for missing/weak extraction.
    # Membership below still requires content evidence or an explicit exception.
    for prefix in ['WF/Digis/2020/', 'WF/Digis/Antrag-Forschungsberichte/']:
        for row in c.execute(f'SELECT {columns} FROM documents WHERE source_path GLOB ?', (str(USB / prefix) + '/*',)):
            rows[row['document_id']] = dict(row)
    extra_ids = ['DOC-FFD22E9113BE96768F3219477E4F6259', 'DOC-779421210A3374C0B0AD8FFACF056F68',
                 'DOC-FFBB9069B4D7ABA4DBB76833B258132D', 'DOC-2858260F485B4029B7FEF3862F580710',
                 'DOC-EB01BF9D5065C427ABFADBB19F5E15F0', 'DOC-A83A3AEACF8E12584478E91D75182C02',
                 'DOC-C54F6FF0966DB0A0ABF03CF1182A5341', 'DOC-A5A6D4BDEF893288EE6CFAD839053EEF']
    for oid in extra_ids:
        row = c.execute(f'SELECT {columns} FROM documents WHERE document_id=?', (oid,)).fetchone()
        if row: rows[oid] = dict(row)
    groups = defaultdict(list)
    for r in rows.values(): groups[r['content_sha256'] or r['document_id']].append(r)
    review = []; evidence = []; seen_paths = set(); included_count = 0
    for digest, group in groups.items():
        # These are discovery leads. Every selected lead receives internal review.
        candidate = any(r['source_path'] in seed_paths or r['document_id'] in extra_ids or
                        ('/WF/Digis/2020/' in r['source_path'] and '/Stiftung ' not in r['source_path']) or
                        '/WF/Digis/Antrag-Forschungsberichte/' in r['source_path'] or
                        (r['extension'] in {'.xlsx', '.csv'} and re.search(r'FEB|Forschungs|008-bearbeitet|Autoren', Path(r['source_path']).name, re.I))
                        for r in group)
        if not candidate: continue
        # All exact-byte copies are found through the existing SHA index, including
        # indexed copies whose text extraction had been skipped for other units.
        if re.fullmatch('[0-9a-f]{64}', digest):
            group = [dict(r) for r in c.execute(f'SELECT {columns} FROM documents WHERE content_sha256=?', (digest,))]
        group = [r for r in group if '::' not in r['source_path'] and Path(r['source_path']).is_file()]
        if not group: continue
        group.sort(key=lambda r: (r['source_path'] not in seed_paths, '/Archiv/' in r['source_path'], len(r['source_path']), r['source_path']))
        r = group[0]; path = Path(r['source_path'])
        cached = c.execute('SELECT header_text FROM documents WHERE document_id=?', (r['document_id'],)).fetchone()[0]
        if not cached:
            cached_row = c.execute('SELECT extracted_text FROM content_cache WHERE content_sha256=?', (digest,)).fetchone()
            cached = cached_row[0] if cached_row else ''
        extracted = extract(path, cached)
        text = extracted['text']; compact = re.sub(r'\s+', ' ', text)
        matches = list(WORDS.finditer(compact))
        excerpts = list(dict.fromkeys(compact[max(0,m.start()-100):m.end()+350] for m in matches))[:6]
        has_seed = any(x['source_path'] in seed_paths for x in group)
        spreadsheet_inventory = bool(re.search(r'FEB[- ]?0(?:08|68)', compact, re.I))
        project = bool(re.search(r'(?:Projekt|Online.{0,3}Präsentation|Digitalisierung).{0,120}Forschungs.{0,15}(?:und.{0,8})?Entwicklungsbericht', compact, re.I))
        work_budget = '2400 Berichten' in compact or '2.400 Berichte' in compact
        known_context = any(x['document_id'] in extra_ids for x in group)
        if digest == '4da06e156fd841f22bd672584de2446dd7d7cfbb75782e87cf16ab101b8ff86f' and matches:
            known_context = True
        retain = bool((has_seed and text) or spreadsheet_inventory or project or work_budget or (known_context and matches))
        relationship = 'direct'
        if known_context and not project: relationship = 'mixed'
        if 'Geschichte des Werks' in path.name and not project: relationship = 'contextual'
        if retain:
            decision = 'included'; included_count += 1
            summary = ('FEB inventory/import or author spreadsheet; original row values retained as evidence.' if has_seed or spreadsheet_inventory else
                       'FEB digitisation application/work plan; distinct original draft or supporting calculation.' if project or work_budget else
                       'Archive description/history explaining FEB provenance, signatures, or cross-references.')
            proposed = 'documents/' + path.name
            # Original names remain legible; existing index identity disambiguates
            # different contents with identical basenames, without a new ID system.
            if any(x['proposed_relative_path'] == proposed and x['decision'] == 'included' for x in review):
                proposed = 'documents/' + path.stem + '__' + r['document_id'] + path.suffix
        else:
            decision = 'needs_review' if extracted['condition'].startswith(('damaged', 'unreadable')) else 'excluded'
            summary = 'No sufficient FEB-specific content evidence; source held unchanged outside the proposed documentation set.'
            proposed = ''
        if digest == 'c93eba333b4be52c54adef2aa4a132222de19538269bd072f3eb339506720e58':
            signature_evidence = [json.loads(line) for line in (ROOT/'accession-document-signature-evidence.jsonl').read_text().splitlines()]
            assert signature_evidence[-1]['pixel_identical']
            decision = 'excluded'; retain = False; relationship = 'direct'
            summary = 'Separate signed budget page; the same scan is already contained on page 15 of the retained complete FEB application.'
            excerpts = ['Live 1600-pixel rendering of this page is pixel-identical to page 15 of Industriesalon-Schöneweide_Digis-Antrag-fuer-2020_Forschungsberichte.pdf. OCR confirms project budget 73.200 Euro and Berlin, 18. September 2019.']
            proposed = ''
        props = extracted['properties']; date = ''; date_basis = 'unknown; source mtime is not document date'
        stated = re.search(r'Stand\s*:\s*(\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{4})', compact)
        if stated: date = re.sub(r'\s+', '', stated.group(1)); date_basis = 'date stated inside document'
        elif props.get('modified'): date = props['modified']; date_basis = 'OOXML internal modification date, not asserted authorship/publication date'
        elif props.get('CreationDate'): date = props['CreationDate']; date_basis = 'PDF internal creation date, not asserted document date'
        if not excerpts and has_seed: excerpts = [compact[:900]]
        evidence_record = {'document_id': r['document_id'], 'source_path': str(path), 'source_sha256': digest,
                          'index_parser': r['parser'], 'inspection_method': extracted['method'],
                          'condition': extracted['condition'], 'properties': props, 'errors': extracted['errors'],
                          'text_characters': len(text), 'relevance_excerpts': excerpts,
                          'text_scope': 'full extracted document text; existing OCR is partial where indicated',
                          'extracted_text': text}
        evidence.append(evidence_record)
        for number, source in enumerate(group):
            if source['source_path'] in seen_paths: continue
            seen_paths.add(source['source_path']); st = Path(source['source_path']).stat()
            unchanged = st.st_size == source['size_bytes'] and st.st_mtime_ns == source['mtime_ns']
            review.append({'document_id': source['document_id'], 'source_path': source['source_path'],
                'size_bytes': source['size_bytes'], 'mtime_ns': source['mtime_ns'], 'content_sha256': digest,
                'decision': 'redundant' if decision == 'included' and number else decision,
                'relationship': relationship, 'description': summary,
                'document_date': date, 'date_basis': date_basis,
                'content_evidence': ' | '.join(excerpts) if excerpts else summary,
                'index_parser': source['parser'], 'inspection_method': extracted['method'],
                'condition': extracted['condition'], 'current_source_check': 'unchanged' if unchanged else 'changed_since_index',
                'proposed_relative_path': proposed, 'retained_source_path': str(path) if retain else '',
                'notes': '; '.join(extracted['errors']) if extracted['errors'] else 'Stored index SHA-256 reused after size+mtime check; no new source hashing.',
                'deletion_authorized': 'false'})
        print(json.dumps({'file':path.name,'decision':decision,'copies':len(group),'condition':extracted['condition']},ensure_ascii=False),flush=True)
    wb.atomic_replace(ROOT / 'accession-document-review.csv', wb.csv_payload(list(review[0]), review))
    wb.atomic_replace(ROOT / 'accession-document-internal-evidence.jsonl', ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in evidence).encode())
    search = {'document_index':str(INDEX),'index_access':'read-only immutable; index WAL empty',
              'query':'header_text : (Forschungsberichte OR Entwicklungsberichte OR Forschungsbericht OR Entwicklungsbericht)',
              'content_hits_non_xml':len(hits),'content_hash_groups_non_xml':len(set(r['content_sha256'] for r in hits)),
              'supplement':'27 existing metadata sources; indexed 2020 FEB project paths for extraction gaps; SHA-256 duplicate expansion',
              'reviewed_source_paths':len(review),'reviewed_content_groups':len(evidence),
              'decisions':dict(Counter(r['decision'] for r in review)),
              'scope_limit':'Content search covers indexed extracted text/partial OCR, not every page of every archive document. Broad hits remain search evidence, not automatic accession membership.',
              'source_modified':False,'index_modified':False,'deletion_authorized':False}
    wb.atomic_replace(ROOT/'accession-document-search-summary.json',(json.dumps(search,ensure_ascii=False,indent=2)+'\n').encode())
    print(json.dumps(search,indent=2))


if __name__ == '__main__': main()
