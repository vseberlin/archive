"""PAGE reference interchange and bounded OCR evaluation over retained readings.

This module never runs recognition or promotes a transcription to ground truth.
An operator-unchecked reference can produce development diagnostics only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET

from .extractors.document_evaluation import distance, inside, positioned_text

PAGE_NS = 'http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15'


def literal(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())


def page_xml(image, size, regions, *, status='pending_operator'):
    """Explicit region text/order; no invented line or character segmentation."""
    ET.register_namespace('', PAGE_NS)
    def add(parent, name, attrs=None, text=None):
        node = ET.SubElement(parent, '{'+PAGE_NS+'}'+name, attrs or {})
        node.text = text
        return node
    root = ET.Element('{'+PAGE_NS+'}PcGts')
    meta = add(root, 'Metadata')
    add(meta, 'Creator', text='archive_research evaluation; reference status: '+status)
    stamp = datetime.now(timezone.utc).isoformat()
    add(meta, 'Created', text=stamp)
    add(meta, 'LastChange', text=stamp)
    width, height = size
    page = add(root, 'Page', {'imageFilename': str(image), 'imageWidth': str(width), 'imageHeight': str(height)})
    order = add(add(page, 'ReadingOrder'), 'OrderedGroup', {'id': 'order'})
    for index, item in enumerate(regions):
        key = 'r'+str(index)
        add(order, 'RegionRefIndexed', {'index': str(index), 'regionRef': key})
        region = add(page, 'TextRegion', {'id': key})
        box = item['region']
        x, y = round(box['x']*width), round(box['y']*height)
        right, bottom = min(width, round((box['x']+box['width'])*width)), min(height, round((box['y']+box['height'])*height))
        if not (0 <= x < right <= width and 0 <= y < bottom <= height):
            raise ValueError('Invalid normalized reference coordinates')
        add(region, 'Coords', {'points': f'{x},{y} {right},{y} {right},{bottom} {x},{bottom}'})
        add(add(region, 'TextEquiv'), 'Unicode', text=item.get('text', ''))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def critical_score(expected, actual):
    """Score labeled occurrences through alignment; another occurrence cannot hide a deletion."""
    from rapidfuzz.distance import Levenshtein
    ref, hyp = literal(expected['text']), literal(actual)
    ops = Levenshtein.opcodes(ref, hyp)
    results = []
    for token in expected.get('critical', []):
        start, end = token['start'], token['end']
        if ref[start:end] != token['text'] or start >= end:
            raise ValueError('Critical labels must refer to literal-normalized reference offsets')
        affected = [o for o in ops if (o.src_start < end and o.src_end > start) or
                    (o.tag == 'insert' and (start < o.src_start < end or
                     (o.src_start in (start, end) and
                      any(c.isalnum() for c in hyp[o.dest_start:o.dest_end]))))]
        correct = all(o.tag == 'equal' for o in affected) and bool(affected)
        results.append({**token, 'correct': correct})
    return results


def layout_coverage(reference, predicted, threshold=.5):
    """One-to-one rectangular IoU matching; merges cannot count as multiple detections."""
    def iou(a,b):
        area = max(0,min(a['x']+a['width'],b['x']+b['width'])-max(a['x'],b['x'])) * max(0,min(a['y']+a['height'],b['y']+b['height'])-max(a['y'],b['y']))
        return area/(a['width']*a['height']+b['width']*b['height']-area) if area else 0
    pairs = sorted(((iou(a,b),i,j) for i,a in enumerate(reference) for j,b in enumerate(predicted)), reverse=True)
    seen_ref,seen_pred,matches=set(),set(),[]
    for score,i,j in pairs:
        if score>=threshold and i not in seen_ref and j not in seen_pred:
            seen_ref.add(i);seen_pred.add(j);matches.append([i,j,score])
    return {'matches': matches,'missing': sorted(set(range(len(reference)))-seen_ref),
            'unmatched_predictions': sorted(set(range(len(predicted)))-seen_pred),
            'recall': len(matches)/len(reference) if reference else None,
            'precision': len(matches)/len(predicted) if predicted else None,'iou_threshold': threshold}


def part_score(reference, parts, *, threshold=.5, complete_reference=False):
    """Score region extraction separately from downstream text/visual quality.

    References contain source-coordinate `region` and independently assigned
    `kind`. A partial annotation cannot label extra detections as false positives.
    This diagnostic never changes review status or selects a winning recipe.
    """
    from .contracts import Region, require
    require(0 < threshold <= 1 and type(complete_reference) is bool,'Invalid part scoring policy')
    kinds={'text','visual','table','formula','handwriting','unresolved'}
    for row in [*reference,*parts]:
        Region(**row['region'])
        require(row['kind'] in kinds,'Unknown reference or predicted part kind')
    coverage=layout_coverage([r['region'] for r in reference],[p['region'] for p in parts],threshold)
    confusion={};correct=0
    for i,j,_ in coverage['matches']:
        expected,actual=reference[i]['kind'],parts[j]['kind']
        confusion.setdefault(expected,{})
        confusion[expected][actual]=confusion[expected].get(actual,0)+1
        correct+=expected==actual
    unjudged=[]
    if not complete_reference:
        unjudged=coverage.pop('unmatched_predictions')
        coverage['precision']=None
    return {'region_detection':coverage,'kind_confusion':confusion,
            'correctly_typed_matches':correct,'matched_regions':len(coverage['matches']),
            'typed_recall':correct/len(reference) if reference else None,
            'unjudged_predictions':unjudged,'complete_reference':complete_reference,
            'qualified':False,
            'limits':['One-to-one IoU matching; report misses, splits and merged regions separately from OCR.',
                      'Caption/article associations, reading order and downstream recognition/retrieval need their own checks.']}


def evaluate(manifest_path, output):
    from PIL import Image
    from dinglehopper.cli import process
    manifest_path, output = Path(manifest_path), Path(output)
    manifest = json.loads(manifest_path.read_text())
    if not output.is_dir():
        raise ValueError('Use an existing evaluation output directory')
    results=[];part_results=[]
    for page in manifest['pages']:
        if page.get('split') == 'final-test' and (page.get('readings') or page.get('layout_readings')):
            if manifest.get('final_test_locked', True):
                raise ValueError('Final-test recipes are not frozen; scoring remains locked')
            if page.get('reference_status') != 'operator_checked':
                raise ValueError('Final-test references require independent operator checking')
        if not re.fullmatch(r'[A-Za-z0-9_-]+',page['id']): raise ValueError('Unsafe evaluation ID')
        def bound_reading(reading):
            if not re.fullmatch(r'[A-Za-z0-9_-]+',reading['id']): raise ValueError('Unsafe reading ID')
            data=Path(reading['receipt']).read_bytes();row=json.loads(data)
            source_page=row['result']['page']
            if any(source_page[k] != page['page'][k] for k in ['index','kind']) or any(
                    source_page['source'].get(k)!=page['page']['source'].get(k) for k in ['root','path','size_bytes','mtime_ns']):
                raise ValueError('Reading and reference page bindings disagree')
            return row,hashlib.sha256(data).hexdigest()
        for reading in page.get('layout_readings',[]):
            from .extractors.document_layout import part_kind
            row,digest=bound_reading(reading)
            if row['result']['recipe']['extractor']!='document-layout':
                raise ValueError('Part evaluation needs document-layout evidence')
            parts=[{'region':b['region'],'kind':part_kind(b['label'])} for b in row['feature']['blocks']]
            part_results.append({'page':page['id'],'variant':reading['id'],
                'reference_status':page['reference_status'],'receipt':reading['receipt'],'receipt_sha256':digest,
                'scope':'region extraction and material classification; not recognition accuracy',
                **part_score(page['part_regions'],parts,threshold=manifest.get('part_iou_threshold',.5),
                             complete_reference=page.get('part_reference_complete',False))})
        size=Image.open(page['image']).size
        refs=page['regions']
        if not refs: continue
        gt=output/(page['id']+'.gt.page.xml')
        gt.write_bytes(page_xml(page['image'],size,refs,status=page['reference_status']))
        for reading in page.get('readings',[]):
            row,digest=bound_reading(reading)
            words=row['words']
            actual=[{**ref,'text':literal(positioned_text([w for w in words if inside(w,ref['region'])]))} for ref in refs]
            name=page['id']+'--'+reading['id']
            predicted=output/(name+'.ocr.page.xml')
            predicted.write_bytes(page_xml(page['image'],size,actual,status='machine_reading'))
            process(str(gt),str(predicted),name,str(output),differences=True)
            metrics=json.loads((output/(name+'.json')).read_text())
            nc=ne=0;critical=[];regions=[]
            for ref,hyp in zip(refs,actual):
                text=literal(ref['text']);edits=distance(text,hyp['text']);nc+=len(text);ne+=edits
                critical.extend(critical_score(ref,hyp['text']))
                regions.append({'id':ref['block'],'expected':text,'actual':hyp['text'],'characters':len(text),'errors':edits})
            results.append({'page':page['id'],'variant':reading['id'],'reference_status':page['reference_status'],
                            'scope':'selected development regions; not full-page accuracy','dinglehopper':metrics,
                            'literal_characters':nc,'literal_errors':ne,'literal_cer':ne/nc if nc else None,
                            'critical':critical,'regions':regions,'receipt':reading['receipt'],
                            'receipt_sha256':digest})
    summary={'format':'archive-ocr-scorecard-1','metadata_adopted':False,
             'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
             'evaluator':{'dinglehopper':importlib.metadata.version('dinglehopper'),
                          'normalization':'dinglehopper built-in SBB normalization; separate NFC/whitespace-only literal score'},
             'qualified':False,'reason':'Development diagnostics; operator-checked independent acceptance is separate',
             'results':results,'part_results':part_results}
    filename=manifest.get('scorecard_name','evaluation-scorecard-20261120.json')
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.json',filename): raise ValueError('Unsafe scorecard filename')
    (output/filename).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();result=evaluate(args.manifest,args.output)
    print(json.dumps({'comparisons':len(result['results']),'qualified':False}))


if __name__ == '__main__': main()
