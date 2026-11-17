"""Retain native/embedded PDF text and Poppler word boxes; never call it new OCR."""
from dataclasses import asdict
import subprocess
import xml.etree.ElementTree as ET

from ..contracts import require
from ..geometry import inverse_rotation, source_region
from ..preparation import pdf_info


def extract(roots, page):
    require(page.kind == 'pdf', 'Native text requires a PDF page')
    path = roots.source(page.source)
    width, height, rotation, _ = pdf_info(path, page.index)
    require(abs(width-page.width) < .01 and abs(height-page.height) < .01, 'Changed PDF geometry')
    process = subprocess.run(['pdftotext', '-f', str(page.index), '-l', str(page.index),
                              '-bbox-layout', str(path), '-'], capture_output=True, timeout=180)
    require(process.returncode == 0, 'Native PDF extraction failed')
    root = ET.fromstring(process.stdout)
    pages = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 'page']
    require(len(pages) == 1, 'Expected exactly one PDF page')
    # Poppler reports unrotated page dimensions but rotated word coordinates.
    dw, dh = (height, width) if rotation in (90, 270) else (width, height)
    transform = inverse_rotation(width, height, dw, dh, rotation, 'pdf-points')
    words = []
    for word in pages[0].iter():
        if word.tag.rsplit('}', 1)[-1] != 'word' or not word.text:
            continue
        box = tuple(float(word.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax'))
        region = source_region((box[0], box[1], box[2]-box[0], box[3]-box[1]), transform, page)
        words.append({'text': word.text, 'region': asdict(region), 'confidence': None})
    roots.source(page.source)
    return process.stdout, words
