"""Tesseract ALTO in processed-image pixels, with explicitly mapped source regions."""
from dataclasses import asdict
import os
import subprocess
import xml.etree.ElementTree as ET

from .contracts import require
from .geometry import source_region


def extract(image, page, transform, *, languages='deu+eng', psm=3, timeout=240):
    require(psm in (3, 6, 7, 11, 12, 13), 'Unsupported OCR segmentation mode')
    require(languages and all(s.isalpha() for s in languages.split('+')), 'Invalid OCR language list')
    process = subprocess.run(['tesseract', str(image), 'stdout', '-l', languages,
                              '--psm', str(psm), 'alto'], capture_output=True, timeout=timeout,
                             env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
    require(process.returncode == 0, 'OCR failed: ' + process.stderr.decode(errors='replace')[-400:])
    root = ET.fromstring(process.stdout)
    pages = [n for n in root.iter() if n.tag.rsplit('}', 1)[-1] == 'Page']
    require(len(pages) == 1 and
            (int(pages[0].attrib['WIDTH']), int(pages[0].attrib['HEIGHT'])) ==
            (transform.width, transform.height), 'ALTO geometry differs from processed image')
    words = []
    for word in root.iter():
        if word.tag.rsplit('}', 1)[-1] != 'String' or not word.attrib.get('CONTENT', '').strip():
            continue
        box = tuple(float(word.attrib[k]) for k in ('HPOS', 'VPOS', 'WIDTH', 'HEIGHT'))
        region = source_region(box, transform, page)
        words.append({'text': word.attrib['CONTENT'], 'region': asdict(region),
                      'confidence': float(word.attrib['WC']) if 'WC' in word.attrib else None})
    return process.stdout, words
