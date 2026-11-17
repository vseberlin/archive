"""Deterministic invented media; no real archive or USB files are opened."""
import json
from pathlib import Path
import re
import subprocess

from archive_research.common import atomic_write, file_lock, require


def pdf_bytes(title, pages, printed_label, detail, rotation=0):
    """Small vector fixture PDF, using only the standard library."""
    objects = [b'', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for number in range(1, pages + 1):
        page_id = len(objects) + 1
        stream_id = page_id + 1
        kids.append(f'{page_id} 0 R')
        lines = ['0.97 0.96 0.92 rg 0 0 600 500 re f', '0.1 0.2 0.2 rg']
        def text(x, y, size, value):
            value = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            lines.append(f'BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET')
        text(30, 460, 12, 'SYNTHETIC FIXTURE - invented evidence')
        text(30, 420, 21, title)
        text(30, 386, 12, detail if number == pages else 'Placeholder page for citation navigation')
        if number == pages:
            lines += ['0.2 0.35 0.33 RG 3 w 65 130 440 205 re S',
                      '0.85 0.89 0.86 rg 85 150 400 165 re f']
            for x in [130, 270, 410]:
                lines += [f'0.15 0.3 0.28 rg {x} 205 32 32 re f']
            text(110, 170, 12, 'Three controls; no individual serial number')
            text(30, 85, 13, 'Comparison illustration only')
        text(30, 30, 12, f'PDF page {number}' + (f' / printed {printed_label}' if number == pages else ''))
        stream = '\n'.join(lines).encode('ascii')
        objects += [f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 500] /Rotate {rotation} /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>'.encode(),
                    f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream']
    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1] = f'<< /Type /Pages /Count {pages} /Kids [{" ".join(kids)}] >>'.encode()
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(data)


def binding(path):
    st = path.stat()
    return {'path': path.name, 'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def create_fixture(root):
    records = root / 'records'
    require(not (records / 'evidence.json').exists() and not (records / 'assertions.jsonl').exists(),
            'Existing readable records will not be overwritten')
    from importlib.resources import files
    blocks = json.loads(files('archive_research.web').joinpath('fixture.json').read_text())
    history = blocks[1] + blocks[2] + [blocks[3]]
    for record in history:
        record['synthetic'] = True
    specs = [('photo-view', 'Apparatus view', 'Synthetic photograph stand-in', 1, '1', ''),
             ('report-figure', 'Report figure', 'Figure 3: Type B family - test arrangement', 14, '12', '3'),
             ('patent-passage', 'Patent passage', 'Claim 1: circuit arrangement with three controls', 2, '2', '')]
    views = []
    for key, title, detail, count, printed, figure in specs:
        pdf = root / 'sample' / f'{key}.pdf'
        atomic_write(pdf, pdf_bytes(title, count, printed, detail))
        prefix = root / 'sample' / key
        subprocess.run(['pdftoppm', '-f', str(count), '-l', str(count), '-singlefile',
                        '-scale-to', '1000', '-png', str(pdf), str(prefix)], check=True,
                       capture_output=True, timeout=30)
        png = prefix.with_suffix('.png')
        citation = next(c for c in blocks[0]['citations'] if c['key'] == key)
        views.append({'key': key, 'label': title, 'root': 'sample',
                      'source': binding(png if key == 'photo-view' else pdf), 'preview': binding(png),
                      'candidate_id': 'full' if key == 'photo-view' else None,
                      'item_key': citation['item_key'], 'page_sequence': count, 'page_count': count,
                      'printed_page_label': None if key == 'photo-view' else printed,
                      'figure': figure, 'passage_label': citation.get('passage_label', ''),
                      'region_xywh': citation['region_xywh'], 'rotation': 0,
                      'observation': citation.get('observation', citation.get('transcription', '')),
                      'transcription_method': 'Manual, synthetic',
                      'alt': f'{title}: invented three-control apparatus, labelled synthetic'})
    evidence = {'synthetic': True, 'metadata_adopted': False,
                'cases': [{'key': 'photo', 'title': 'Apparatus, report and patent', 'views': views}],
                'lineage': 'The report and its catalogue republication share one source lineage; '
                           'they are not two independent confirmations.'}
    with file_lock(records / '.research.lock'):
        atomic_write(records / 'evidence.json', json.dumps(evidence, indent=2).encode() + b'\n')
        atomic_write(records / 'assertions.jsonl', b''.join(json.dumps(x).encode() + b'\n' for x in history))
