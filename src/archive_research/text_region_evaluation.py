"""Small diagnostic evaluator; detection recall and OCR alternatives are separate.

Inputs are retained receipts, not a live processing or authority interface.
Only complete annotations permit precision; any-variant text hits are an oracle
diagnostic, never accuracy of a selected transcription.
"""
from .contracts import Region, require
from .text_regions import overlap


def box(value):
    region = Region(**value)
    return region.x, region.y, region.width, region.height


def evaluate(case, receipt, threshold=.25, baseline=None):
    require(0 < threshold <= 1, 'Invalid overlap threshold')
    labels = case['regions']
    regions = receipt.get('discovery', {}).get('regions', [])
    available = receipt.get('result', {}).get('status') in ('complete', 'partial') and 'discovery' in receipt
    require(available, 'Discovery result unavailable for evaluation')
    # Maximum-cardinality one-to-one matching: duplicate detections cannot inflate
    # recall, and a large box cannot claim several annotated identifiers.
    edges = {i: sorted(((j, overlap(box(label['region']), box(region['region'])))
                       for j, region in enumerate(regions)), key=lambda pair: -pair[1])
             for i, label in enumerate(labels)}
    owners = {}

    def assign(i, seen):
        for j, score in edges[i]:
            if score < threshold or j in seen:
                continue
            seen.add(j)
            if j not in owners or assign(owners[j], seen):
                owners[j] = i
                return True
        return False

    for i in range(len(labels)):
        assign(i, set())
    matches = {i: j for j, i in owners.items()}
    recognized = []
    baseline_text = None
    if baseline is not None:
        require(baseline['result']['page'] == receipt['result']['page'], 'Baseline page differs')
        require(baseline['result']['status'] in ('complete', 'partial'), 'Baseline unavailable')
        baseline_text = ' '.join(word['text'] for word in baseline['words'])
    for i, label in enumerate(labels):
        expected = label.get('expected_text')
        if expected is None:
            continue
        readings = regions[matches[i]]['readings'] if i in matches else []
        recognized.append({'label': label['label'], 'expected_text': expected,
                           'any_variant_exact': expected in readings,
                           'baseline_contains_exact': expected in baseline_text if baseline_text is not None else None})
    return {'id': case['id'], 'split': case['split'], 'annotated_regions': len(labels),
            'detected_regions': len(regions), 'matched_regions': len(matches),
            'recall': len(matches)/len(labels) if labels else None,
            'precision': len(matches)/len(regions) if case['complete_annotations'] and regions else None,
            'unmatched_detections': len(regions)-len(matches) if case['complete_annotations'] else None,
            'matches': [{'label': labels[i]['label'], 'candidate': j,
                         'iou': overlap(box(labels[i]['region']), box(regions[j]['region']))}
                        for i, j in sorted(matches.items())],
            'recognition_diagnostic': recognized,
            'scan_complete': receipt['discovery']['coverage']['scan_complete'],
            'elapsed_seconds': receipt['result'].get('elapsed_seconds'),
            'metrics': receipt.get('metrics', {})}
