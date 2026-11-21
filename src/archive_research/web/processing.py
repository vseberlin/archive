"""Optional derived OCR; invoked explicitly from CLI, never from web requests."""
import json
import subprocess
import tempfile

from django.conf import settings

from archive_research.common import atomic_write, file_lock, read_bytes, require
from .records import bound_bytes, snapshot


def binding(view):
    return {key: view[key] for key in ('source', 'preview', 'page_sequence', 'rotation')}


def ocr_status(view):
    path = settings.PILOT_ROOT / 'cache' / f"{view['key']}-ocr.json"
    if not path.exists():
        return {'status': 'not_generated', 'note': 'OCR has not been generated. The image and manual citation remain usable.'}
    try:
        result = json.loads(read_bytes(path))
        require(isinstance(result, dict), 'Invalid OCR receipt')
        if result.get('binding') != binding(view):
            return {'status': 'stale', 'note': 'OCR belongs to an older source or preview and is withheld.'}
        require(result.get('status') in ('ready', 'unavailable'), 'Invalid OCR status')
        require(isinstance(result.get('text', ''), str) and isinstance(result.get('note', ''), str), 'Invalid OCR text')
        if 'tables' in result:
            validate_tables(result['tables'])
        return result
    except (ValueError, OSError, TypeError):
        return {'status': 'unavailable', 'note': 'OCR receipt is unreadable. The image and manual citation remain usable.'}


def validate_tables(tables):
    """Bounded prepared readings, rendered as escaped text, never imported HTML."""
    require(isinstance(tables, list) and len(tables) <= 8, 'Invalid prepared tables')
    for table in tables:
        require(isinstance(table, dict) and set(table) == {'header', 'rows', 'unassigned'},
                'Invalid table reading')
        require(all(isinstance(table[k], str) and len(table[k]) <= 20000
                    for k in ('header', 'unassigned')), 'Invalid table text')
        rows = table['rows']
        require(isinstance(rows, list) and 1 <= len(rows) <= 500, 'Invalid table rows')
        width = len(rows[0]) if isinstance(rows[0], list) else 0
        require(1 <= width <= 40 and all(isinstance(row, list) and len(row) == width and
                all(isinstance(cell, str) and len(cell) <= 5000 for cell in row) for row in rows),
                'Invalid table cells')


def generate_ocr(view_key, case_key='photo'):
    evidence = snapshot()[0]
    case = next((c for c in evidence['cases'] if c['key'] == case_key), None)
    require(case is not None, 'Unknown case')
    view = next((v for v in case['views'] if v['key'] == view_key), None)
    require(view is not None and not view.get('unavailable_reason'), 'Unavailable or unknown citation')
    bound_bytes(view['source'])
    image = bound_bytes(view['preview'])
    result = {'binding': binding(view), 'status': 'unavailable', 'text': '', 'note': ''}
    executable = settings.MACHINE.get('tesseract', 'tesseract')
    with tempfile.NamedTemporaryFile(suffix='.png', dir=settings.PILOT_ROOT / 'cache') as temp:
        temp.write(image)
        temp.flush()
        try:
            process = subprocess.run([executable, temp.name, 'stdout', '-l', 'eng'],
                                     capture_output=True, text=True, timeout=60)
            if process.returncode == 0:
                result.update(status='ready', text=process.stdout,
                              note='Machine transcription; check against the page. It is not a reviewed assertion.')
            else:
                result['note'] = 'OCR processor failed. The image and manual citation remain usable.'
        except (OSError, subprocess.TimeoutExpired):
            result['note'] = 'OCR processor is unavailable. The image and manual citation remain usable.'
    # Publication only. OCR never changes evidence, its revision or reviewer text.
    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        bound_bytes(view['source'])
        bound_bytes(view['preview'])
        atomic_write(settings.PILOT_ROOT / 'cache' / f'{view_key}-ocr.json',
                     json.dumps(result, ensure_ascii=False, indent=2).encode() + b'\n')
    return result['status']
