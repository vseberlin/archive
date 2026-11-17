"""Bounded page extraction and readable resumable receipts; one manual writer."""
from dataclasses import asdict
import hashlib
import importlib.metadata
import importlib
import json
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from xml.etree.ElementTree import ParseError

from .contracts import Artifact, Page, Recipe, Region, Result, Source, require
from .preparation import render
from .sources import Roots
from . import storage

# Explicit adapters keep feature implementations out of the page runner.
FEATURE_ADAPTERS = {'visual': 'extractors.visual_features', 'pdf-layout': 'extractors.layout', 'scan-layout': 'extractors.scan_layout'}


def page_from_record(value):
    value = dict(value)
    value['source'] = Source(**value['source'])
    return Page(**value)


def tool_version(command):
    process = subprocess.run(command, capture_output=True, text=True, timeout=10)
    require(process.returncode == 0, f'Processor unavailable: {command[0]}')
    return (process.stdout + process.stderr).splitlines()[0]


def recipe_for(task):
    extractor = task['extractor']
    if extractor in FEATURE_ADAPTERS:
        return importlib.import_module('.'+FEATURE_ADAPTERS[extractor], __package__).recipe_for(task)
    if extractor == 'line-ocr':
        from .extractors.line_ocr import recipe_for as line_recipe
        return line_recipe(task)
    if extractor == 'text-regions':
        require(task.get('region') is None, 'Text discovery scans the complete selected page')
        from .extractors.text_regions import recipe_for as discovery_recipe
        return discovery_recipe(task)
    require(extractor in ('pdf-text', 'ocr'), 'Unknown extractor')
    require(not task.get('region') or extractor == 'ocr', 'Region extraction currently requires OCR')
    dependencies = {'Pillow': importlib.metadata.version('Pillow'),
                    'pdftoppm': tool_version(['pdftoppm', '-v'])}
    command = ['pdftotext', '-v'] if extractor == 'pdf-text' else ['tesseract', '--version']
    dependencies[command[0]] = tool_version(command)
    settings = {'dpi': 300, 'clockwise': 0, 'languages': 'deu+eng', 'psm': 3, 'timeout': 240,
                **task.get('settings', {})}
    allowed = {'dpi', 'clockwise', 'languages', 'psm', 'timeout'}
    if 'thresholding_method' in settings:
        require(extractor == 'ocr' and type(settings['thresholding_method']) is int and
                settings['thresholding_method'] in (0,1,2), 'Invalid OCR thresholding method')
        allowed.add('thresholding_method')
    if 'caption_origin' in settings:
        require(extractor=='ocr' and task.get('region') is not None,'Caption OCR needs a region')
        allowed.add('caption_origin')
    require(set(settings) == allowed, 'Unknown extractor setting')
    require(type(settings['timeout']) is int and 1 <= settings['timeout'] <= 600, 'Invalid bounded timeout')
    return Recipe.create(extractor, '2' if set(settings)&{'thresholding_method','caption_origin'} else '1', settings=settings, dependencies=dependencies,
                         coverage={'member': task['page']['member'], 'kind': task['page']['kind'],
                                   'index': task['page']['index']}, output='bbox-xhtml' if extractor == 'pdf-text' else 'alto')


def cached(roots, work, key, page, recipe, region=None):
    root = roots.root(work, 'work')
    path = storage.target(root, key + '.json')
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    result = Result.from_record(value['result'])
    require(result.page == page and result.recipe == recipe and result.region == region,
            'Cached result binding differs')
    if result.status not in ('complete', 'partial'):
        return None
    roots.source(page.source)
    for artifact in result.artifacts:
        file = roots.artifact(artifact)
        require(storage.stamp(file) == value['artifact_stamps'][artifact.path], 'Cached artifact changed')
    require(len(result.artifacts) >= 2 if recipe.extractor in ('text-regions', 'line-ocr', *FEATURE_ADAPTERS) else len(result.artifacts) == 2,
            'Incomplete cached artifacts')
    return value


def extract_one(roots, work, task):
    page = page_from_record(task['page'])
    region = Region(**task['region']) if task.get('region') is not None else None
    recipe = recipe_for(task)
    key = recipe.cache_key(page, region)
    root = roots.root(work, 'work')
    start = time.monotonic()
    with storage.lock(root):
        try:
            roots.source(page.source)
            if task['extractor'] in ('visual', 'scan-layout'):
                adapter = importlib.import_module('.'+FEATURE_ADAPTERS[task['extractor']], __package__)
                adapter.validate_inputs(roots, work, task, recipe)
            if task['extractor']=='ocr' and 'caption_origin' in task.get('settings',{}):
                from .extractors.caption_regions import validate_inputs
                validate_inputs(roots,work,task,recipe)
            if task['extractor'] == 'line-ocr':
                from .extractors.line_ocr import model_paths
                model_paths(roots, recipe.record()['parameters']['settings'])
            if task['extractor'] == 'text-regions':
                model = Artifact(**recipe.record()['parameters']['settings']['model'])
                require(hashlib.sha256(roots.artifact(model).read_bytes()).hexdigest() == model.sha256,
                        'Detector model changed')
            prior = cached(roots, work, key, page, recipe, region)
            if prior is not None:
                return {'key': key, 'state': prior['result']['status'], 'reused': True,
                        'word_count': len(prior['words'])}
            settings = recipe.record()['parameters']['settings']
            discovery, extra_artifacts, feature = None, [], None
            with tempfile.TemporaryDirectory(prefix='.page-', dir=root) as temporary:
                image, transform, rotation = render(roots, page, Path(temporary), dpi=settings['dpi'],
                                                     clockwise=settings['clockwise'], region=region)
                if task['extractor'] == 'visual':
                    from .extractors.visual_features import extract
                    raw, feature = extract(image, roots, recipe)
                    words = []; suffix, kind = '.visual.json', 'normalized-visual-embedding'
                    note = 'Local visual features for retrieval; no identification or OCR claim.'
                elif task['extractor'] == 'scan-layout':
                    from .extractors.scan_layout import extract
                    raw, words, feature, extra_artifacts = extract(image,page,transform,roots,settings,Path(temporary),key,work)
                    suffix, kind = '.layout.json', 'scan-figure-and-caption-candidates'
                    note = 'Scanned-page figure candidates and reused OCR; semantic coverage remains incomplete.'
                elif task['extractor'] == 'pdf-layout':
                    from .extractors.layout import extract
                    raw, words, feature, extra_artifacts = extract(image,page,transform,roots,settings,Path(temporary),key)
                    suffix, kind = '.layout.json', 'pdf-figure-and-caption-candidates'
                    note = 'Embedded raster placements and nearby text; full-page scans/vector figures need further segmentation.'
                elif task['extractor'] == 'pdf-text':
                    from .extractors.pdf_text import extract
                    raw, words = extract(roots, page)
                    suffix, kind = '.bbox.xhtml', 'native-or-embedded-pdf-text'
                    note = 'Native or pre-existing embedded PDF text; its original OCR provenance may be unknown.'
                elif task['extractor'] in ('text-regions', 'line-ocr'):
                    if task['extractor'] == 'line-ocr':
                        from .extractors.line_ocr import extract
                    else:
                        from .extractors.text_regions import extract
                    try:
                        raw, words, discovery, extra_artifacts = extract(
                            image, page, transform, roots, settings, Path(temporary), key)
                    except Exception as error:
                        raise ValueError('Text-region processing failed: '+str(error)) from error
                    suffix, kind = '.regions.json', 'text-region-detection-and-readings'
                    note = ('Explicit manual line regions and two local recognizers; no automatic coverage or identifier-accuracy claim.'
                            if task['extractor'] == 'line-ocr' else
                            'Automatic text candidates and independent OCR variants; no completeness or identifier-accuracy claim.')
                    for candidate in discovery['regions']:
                        for variant in candidate['variants']:
                            variant.pop('raw_alto', None)  # Full raw output remains in .regions.json.
                else:
                    from .extractors.ocr import extract
                    raw, words = extract(image, page, transform, languages=settings['languages'],
                                         psm=settings['psm'], timeout=settings['timeout'],
                                         **({'thresholding_method':settings['thresholding_method']}
                                            if 'thresholding_method' in settings else {}))
                    suffix, kind = '.alto.xml', 'machine-ocr-alto'
                    note = 'Unreviewed machine OCR; confidence is not factual verification.'
                roots.source(page.source)
                roots.root(work, 'work')
                artifacts, stamps = [], {}
                for name, content, artifact_kind in [(key + '.png', image.read_bytes(), 'processed-page'),
                                                      (key + suffix, raw, kind), *extra_artifacts]:
                    path = storage.write(root, name, content)
                    artifacts.append(Artifact(work, name, artifact_kind, hashlib.sha256(content).hexdigest()))
                    stamps[name] = storage.stamp(path)
            roots.source(page.source)
            limitations = (note, 'Coverage is this explicitly requested page/frame, not the whole object.')
            if not words and task['extractor'] != 'visual':
                limitations += ('No words returned; this does not establish that the page is blank.',)
            if region is not None:
                limitations += ('Selected region only; retained image is a crop, not the complete page.',)
            status = 'complete' if words else 'partial'
            if task['extractor'] == 'visual': status = 'complete'
            if task['extractor'] in ('pdf-layout', 'scan-layout'): status = 'partial'  # Semantic coverage is deliberately incomplete.
            if discovery and (not discovery['coverage']['scan_complete'] or
                              any(r['status'] != 'text_extracted' for r in discovery['regions'])):
                status = 'partial'
            result = Result(page, recipe, status, time.monotonic()-start,
                            tuple(artifacts), region=region, transform=transform, limitations=limitations)
            value = {'result': result.record(), 'words': words, 'artifact_stamps': stamps,
                     'display_rotation': rotation,
                     'metrics': {'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                 'child_peak_rss_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                                 'retained_bytes': sum(s['size_bytes'] for s in stamps.values())}}
            if discovery is not None:
                value['discovery'] = discovery
            if feature is not None:
                value['feature'] = feature
            storage.write_json(root, key + '.json', value)
            return {'key': key, 'state': result.status, 'reused': False, 'word_count': len(words)}
        except (OSError, ValueError, KeyError, ParseError, subprocess.SubprocessError) as error:
            status = 'changed_source' if str(error).startswith('Source changed') else 'error'
            result = Result(page, recipe, status, time.monotonic()-start, region=region,
                            limitations=(str(error)[:1500],))
            # Preserve any old completed receipt. Its live binding will withhold
            # stale display; the new run summary records the failed attempt.
            path = storage.target(root, key + '.json')
            if not path.exists():
                storage.write_json(root, path.name, {'result': result.record(), 'words': [],
                                                    'artifact_stamps': {}, 'metrics': {}})
            return {'key': key, 'state': status, 'reused': False, 'error': str(error)[:1500]}


def load_plan(path, expected_sha256=None):
    data = Path(path).read_bytes()
    require(not expected_sha256 or hashlib.sha256(data).hexdigest() == expected_sha256,
            'Extraction plan changed during the run')
    value = json.loads(data)
    require(value.get('format') == 'archive-research-plan-1' and value.get('metadata_adopted') is False,
            'Invalid research plan')
    require(isinstance(value['tasks'], list) and value['tasks'], 'Explicit bounded tasks required')
    require(len({task['id'] for task in value['tasks']}) == len(value['tasks']), 'Duplicate task labels')
    for task in value['tasks']:
        page_from_record(task['page'])
        if task.get('region') is not None:
            Region(**task['region'])
            require(task['extractor'] in ('ocr','visual'), 'Region extraction requires OCR or visual features')
    return value
