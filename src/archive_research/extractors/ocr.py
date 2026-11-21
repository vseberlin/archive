"""Tesseract ALTO in processed-image pixels, with explicitly mapped source regions."""
from dataclasses import asdict
import os
import subprocess
import xml.etree.ElementTree as ET

from ..contracts import require
from ..geometry import source_region


def positioned_alto(raw, page, transform):
    """Retain ALTO block/line boundaries as well as source-bound word boxes."""
    root = ET.fromstring(raw)
    def local(node): return node.tag.rsplit('}', 1)[-1]
    pages = [n for n in root.iter() if local(n) == 'Page']
    require(len(pages) == 1 and
            (int(pages[0].attrib['WIDTH']), int(pages[0].attrib['HEIGHT'])) ==
            (transform.width, transform.height), 'ALTO geometry differs from processed image')
    def mapped(node):
        box = tuple(float(node.attrib[k]) for k in ('HPOS', 'VPOS', 'WIDTH', 'HEIGHT'))
        return asdict(source_region(box, transform, page))
    words = []; blocks = []
    for node in (n for n in root.iter() if local(n) == 'TextBlock'):
        index = len(blocks); lines = []
        for line in (n for n in node if local(n) == 'TextLine'):
            values = []
            for word in (n for n in line if local(n) == 'String' and n.get('CONTENT', '').strip()):
                words.append({'text': word.attrib['CONTENT'], 'region': mapped(word),
                    'block_index': index, 'line_index': len(lines), 'word_index': len(values),
                    'confidence': float(word.attrib['WC']) if 'WC' in word.attrib else None})
                values.append(word.attrib['CONTENT'])
            if values:
                lines.append({'text': ' '.join(values), 'region': mapped(line)})
        blocks.append({'index': index, 'label': 'ocr text', 'region': mapped(node),
                       'lines': lines, 'text': '\n'.join(line['text'] for line in lines)})
    expected = sum(local(n) == 'String' and bool(n.get('CONTENT', '').strip()) for n in root.iter())
    require(len(words) == expected, 'ALTO words outside a retained block/line')
    return words, {'text_blocks': blocks, 'block_order': [b['index'] for b in blocks],
                   'reading_order_basis': 'OCR block/line proposal; unreviewed',
                   'coverage': {'detected_text_blocks': len(blocks),
                                'full_page_accuracy_qualified': False}}


def extract(image, page, transform, *, languages='deu+eng', psm=3, timeout=240, tessdata_dir=None, thresholding_method=None):
    require(psm in (3, 4, 6, 7, 11, 12, 13), 'Unsupported OCR segmentation mode')
    require(languages and all(s.isalpha() for s in languages.split('+')), 'Invalid OCR language list')
    # A selected model directory need not contain the distribution's configs/alto.
    # Request ALTO directly rather than silently receiving plain text instead.
    extra = ['-c', 'tessedit_create_alto=1'] if tessdata_dir is None else ['--tessdata-dir', str(tessdata_dir),
                                                 '--oem', '1', '-c', 'tessedit_create_alto=1']
    if thresholding_method is not None:
        require(type(thresholding_method) is int and thresholding_method in (0,1,2), 'Invalid thresholding method')
        extra = ['-c', 'thresholding_method='+str(thresholding_method), *extra]
    process = subprocess.run(['tesseract', str(image), 'stdout', '-l', languages,
                              '--psm', str(psm), *extra], capture_output=True, timeout=timeout,
                             env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
    require(process.returncode == 0, 'OCR failed: ' + process.stderr.decode(errors='replace')[-400:])
    words, _ = positioned_alto(process.stdout, page, transform)
    return process.stdout, words


def extract_characters(image,page,transform,temporary,*,languages='deu+eng',psm=6,timeout=240,thresholding_method=None):
    """ALTO and hOCR from one recognition pass, retaining character geometry."""
    from pathlib import Path
    from .scientific_tokens import positioned_hocr,candidates
    require(psm in (3,4,6,7,11,12,13),'Unsupported OCR segmentation mode')
    require(languages and all(s.isalpha() for s in languages.split('+')),'Invalid OCR language list')
    base=Path(temporary)/'character-ocr'
    args=['tesseract',str(image),str(base),'-l',languages,'--psm',str(psm),
          '-c','tessedit_create_alto=1','-c','tessedit_create_hocr=1','-c','hocr_char_boxes=1']
    if thresholding_method is not None:args+=['-c','thresholding_method='+str(thresholding_method)]
    process=subprocess.run(args,capture_output=True,timeout=timeout,env={**os.environ,'OMP_THREAD_LIMIT':'1'})
    require(process.returncode==0,'Character OCR failed: '+process.stderr.decode(errors='replace')[-400:])
    alto=base.with_suffix('.xml').read_bytes();hocr=base.with_suffix('.hocr').read_bytes()
    words,feature=positioned_alto(alto,page,transform);reading=positioned_hocr(hocr,page,transform)
    require(''.join(w['text'] for w in words)==''.join(w['text'] for w in reading['words']),
            'ALTO and hOCR recognition streams differ')
    feature['character_reading']=reading;feature['scientific_candidates']=candidates(reading,page,transform)
    return alto,hocr,words,feature
