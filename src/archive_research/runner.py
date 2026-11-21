"""Bounded page extraction and readable resumable receipts; one manual writer."""
from dataclasses import asdict, replace
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

from .imaging import RENDER_POLICY
from .contracts import Artifact, Page, Recipe, Region, Result, Source, page_binding, require
from .prepared_views import prepared_view as render
from .sources import Roots
from . import storage
from . import result_records

# Explicit adapters keep feature implementations out of the page runner.
FEATURE_ADAPTERS = {'visual': 'extractors.visual_features', 'pdf-layout': 'extractors.layout', 'scan-layout': 'extractors.scan_layout', 'faces': 'extractors.faces', 'document-layout': 'extractors.document_layout', 'document-text': 'extractors.document_text', 'scientific-region': 'extractors.scientific_region'}
FEATURE_ADAPTERS['scientific-table']='extractors.scientific_table'
FEATURE_ADAPTERS['ruled-table']='extractors.ruled_table'
FEATURE_ADAPTERS['spelling']='extractors.spelling'
FEATURE_ADAPTERS['handwriting']='extractors.handwriting'


def page_from_record(value):
    value = dict(value)
    value['source'] = Source(**value['source'])
    return Page(**value)


def tool_version(command):
    process = subprocess.run(command, capture_output=True, text=True, timeout=10)
    require(process.returncode == 0, f'Processor unavailable: {command[0]}')
    return (process.stdout + process.stderr).splitlines()[0]


def tessdata_fingerprints(languages):
    """Bind the language models Tesseract will load, not only their names.

    The directory is resolved as Tesseract resolves it (TESSDATA_PREFIX included);
    only names and hashes enter the recipe, so moving identical files changes nothing.
    """
    import re
    process = subprocess.run(['tesseract', '--list-langs'], capture_output=True, text=True, timeout=10)
    require(process.returncode == 0, 'Tesseract language list unavailable')
    found = re.search(r'"([^"]+)"', process.stdout + process.stderr)
    require(found, 'Tesseract tessdata directory not reported')
    directory = Path(found[1])
    out = {}
    for language in languages.split('+'):
        path = directory / (language + '.traineddata')
        require(path.is_file(), f'Tesseract language model missing: {language}')
        with path.open('rb') as handle:
            out[language] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return out


def retryable(value, recipe):
    """Unfinished work (deadline, error) is retried; deterministic limits are reused."""
    if recipe.extractor == 'faces':
        coverage = value.get('feature', {}).get('coverage', {})
        return not coverage.get('scan_complete') and coverage.get('stopped') != 'budget'
    discovery = value.get('discovery')
    if discovery:
        if any(p.get('state') in ('not_attempted', 'error') for p in discovery.get('passes', [])):
            return True
        coverage = discovery.get('coverage', {})
        if coverage.get('recognition_stopped') in ('timeout', 'error'):
            return True
        # Older receipts have no stop reason. Missing variants inside the
        # recognition limit mean a deadline stop, not a deliberate region cap.
        expected = coverage.get('expected_variants_per_region')
        if expected is not None:
            from .extractors.text_regions import recognition_coverage
            regions = discovery.get('regions', [])
            progress = recognition_coverage(regions, expected, coverage.get('recognition_limit', len(regions)))
            if progress['recognition_stopped'] in ('timeout', 'error'):
                return True
        return any(v.get('status') == 'error' for r in discovery.get('regions', []) for v in r.get('variants', []))
    return False


def recipe_for(task):
    from .profiles import resolve_ocr_task, ocr_profile
    task = resolve_ocr_task(task)
    recipe = _recipe_for(task)
    if 'ocr_profile' in task:
        profile = ocr_profile(task['ocr_profile']['name'])
        recipe = Recipe.create(recipe.extractor, recipe.version, **recipe.record()['parameters'],
                               ocr_profile={'name': profile['name'], 'version': profile['version'],
                                            'accuracy_qualified': False, 'checks': profile['checks'],
                                            'limits': profile['limits']})
    return recipe


def _recipe_for(task):
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
    if 'character_boxes' in settings:
        require(extractor=='ocr' and settings['character_boxes'] is True,'Invalid character OCR selection')
        allowed.add('character_boxes')
    if 'image_preparation' in settings:
        from .imaging import OCR_PREPARATIONS
        require(extractor == 'ocr' and settings['image_preparation'] in OCR_PREPARATIONS,
                'Unknown OCR image preparation')
        require('normalization' not in settings, 'Select one OCR image preparation')
        allowed.add('image_preparation')
        if OCR_PREPARATIONS[settings['image_preparation']][2]:
            dependencies.update({name:importlib.metadata.version(name) for name in ('numpy','opencv-python-headless')})
    if 'normalization' in settings:
        require(extractor == 'ocr' and settings['normalization'] == 'local-red-v1',
                'Unknown OCR normalization')
        allowed.add('normalization')
        dependencies.update({name:importlib.metadata.version(name) for name in ('numpy','opencv-python-headless')})
    if 'thresholding_method' in settings:
        require(extractor == 'ocr' and type(settings['thresholding_method']) is int and
                settings['thresholding_method'] in (0,1,2), 'Invalid OCR thresholding method')
        allowed.add('thresholding_method')
    if 'caption_origin' in settings:
        require(extractor=='ocr' and task.get('region') is not None,'Caption OCR needs a region')
        allowed.add('caption_origin')
    if 'layout_origin' in settings:
        require(extractor=='ocr' and task.get('region') is not None, 'Layout OCR needs a region')
        allowed.add('layout_origin')
    require(set(settings) == allowed, 'Unknown extractor setting')
    require(type(settings['timeout']) is int and 1 <= settings['timeout'] <= 600, 'Invalid bounded timeout')
    if extractor == 'ocr':
        dependencies['tessdata'] = tessdata_fingerprints(settings['languages'])
    return Recipe.create(extractor, '4' if extractor == 'ocr' else '1', settings=settings, preparation=RENDER_POLICY, dependencies=dependencies,
                         coverage={'member': task['page']['member'], 'kind': task['page']['kind'],
                                   'index': task['page']['index']}, output='bbox-xhtml' if extractor == 'pdf-text' else 'alto')


def cached(roots, work, key, page, recipe, region=None):
    root = roots.root(work, 'work')
    path = storage.target(root, key + '.json')
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    result_records.identity(value)
    result = Result.from_record(value['result'])
    require(page_binding(result.page) == page_binding(page) and result.recipe == recipe and result.region == region,
            'Cached result binding differs')
    if result.status not in ('complete', 'partial') or retryable(value, recipe):
        return None
    # Same identity and content at a new locator/stamp: prove content once by hash.
    roots.source(page.source, verify_hash=bool(page.source.sha256) and result.page.source != page.source)
    for artifact in result.artifacts:
        file = roots.artifact(artifact)
        require(storage.stamp(file) == value['artifact_stamps'][artifact.path], 'Cached artifact changed')
        require(hashlib.sha256(file.read_bytes()).hexdigest() == artifact.sha256, 'Cached artifact content changed')
    if recipe.extractor=='ocr' and recipe.record()['parameters']['settings'].get('character_boxes'):
        require(len(result.artifacts)==3 and {a.kind for a in result.artifacts}==
                {'processed-page','machine-ocr-alto','machine-ocr-character-hocr'},'Incomplete character OCR artifacts')
    else:
        require(len(result.artifacts) >= 2 if recipe.extractor in ('text-regions', 'line-ocr', *FEATURE_ADAPTERS) else len(result.artifacts) == 2,
                'Incomplete cached artifacts')
    return value


def extract_one(roots, work, task, *, encoders=None):
    from .profiles import resolve_ocr_task
    task = resolve_ocr_task(task)
    page = page_from_record(task['page'])
    region = Region(**task['region']) if task.get('region') is not None else None
    recipe = recipe_for(task)
    key = recipe.cache_key(page, region)
    root = roots.root(work, 'work')
    start = time.monotonic()
    with storage.lock(root):
        try:
            roots.source(page.source)
            if task['extractor'] in FEATURE_ADAPTERS:
                adapter = importlib.import_module('.'+FEATURE_ADAPTERS[task['extractor']], __package__)
                adapter.validate_inputs(roots, work, task, recipe)
            if task['extractor']=='ocr' and 'caption_origin' in task.get('settings',{}):
                from .extractors.caption_regions import validate_inputs
                validate_inputs(roots,work,task,recipe)
            if task['extractor']=='ocr' and 'layout_origin' in task.get('settings',{}):
                from .extractors.document_layout import validate_ocr
                validate_ocr(roots,work,task,recipe)
            if task['extractor'] == 'line-ocr':
                from .extractors.line_ocr import model_paths
                model_paths(roots, recipe.record()['parameters']['settings'])
            if task['extractor'] == 'text-regions':
                model = Artifact(**recipe.record()['parameters']['settings']['model'])
                require(hashlib.sha256(roots.artifact(model).read_bytes()).hexdigest() == model.sha256,
                        'Detector model changed')
            prior, prior_key = cached(roots, work, key, page, recipe, region), key
            if prior is None:
                prior_key = recipe.cache_key(page, region, version=1)
                prior = cached(roots, work, prior_key, page, recipe, region)
            if prior is None:
                prior = result_records.reuse(roots, work, page, recipe, region)
                if prior is not None:
                    value, evidence = result_records.publish(roots, work, key, prior)
                    return {'key': key, 'state': value['result']['status'], 'reused': True,
                            'rebound': True, 'word_count': len(value['words']),
                            'result_sha256': value['result_sha256'], 'evidence_sha256': evidence,
                            **({'warning': value['projection_warning']} if value.get('projection_warning') else {})}
            if prior is not None:
                stored = Result.from_record(prior['result'])
                rebound = prior_key != key or stored.page != page
                old_data = storage.target(root, prior_key + '.json').read_bytes()
                result_records.retain(roots, work, prior, original_bytes=old_data)
                if rebound:
                    # No re-extraction: refile under the current key with the current locator.
                    value = {**prior, 'result': replace(stored, page=page).record(),
                             'rebound_from': {'key': prior_key, 'page': asdict(stored.page)}}
                else:
                    value = prior
                value, evidence = result_records.publish(roots, work, key, value)
                return {'key': key, 'state': prior['result']['status'], 'reused': True,
                        'rebound': rebound, 'word_count': len(prior['words']),
                        'result_sha256': value['result_sha256'], 'evidence_sha256': evidence,
                            **({'warning': value['projection_warning']} if value.get('projection_warning') else {})}
            # A selective retry may replace artifact filenames. Preserve earlier
            # readable results and bytes first, including unfinished observations.
            previous_path = storage.target(root, key + '.json')
            if previous_path.exists():
                previous_bytes = previous_path.read_bytes()
                result_records.retain(roots, work, json.loads(previous_bytes), original_bytes=previous_bytes)
            settings = recipe.record()['parameters']['settings']
            discovery, extra_artifacts, feature = None, [], None
            with tempfile.TemporaryDirectory(prefix='.page-', dir=root) as temporary:
                image, transform, rotation = render(roots, page, Path(temporary), root, dpi=settings['dpi'],
                                                     clockwise=settings['clockwise'], region=region)
                if settings.get('normalization') == 'local-red-v1':
                    from .imaging import normalize_ocr_image
                    image = storage.write(root, key+'.normalized.png', normalize_ocr_image(image, dpi=settings['dpi']))
                    transform = replace(transform, steps=transform.steps+(
                        'local-red-v1: red channel, Gaussian background sigma 30*dpi/300, gain 5, offset 230; geometry unchanged',))
                if settings.get('image_preparation'):
                    from .imaging import prepare_ocr_image
                    data, transform = prepare_ocr_image(image, transform,
                        method=settings['image_preparation'], dpi=settings['dpi'])
                    image = storage.write(root, key+'.ocr-input.png', data)
                if task['extractor'] == 'visual':
                    from .extractors.visual_features import extract, Encoder, space_id
                    encoder = None
                    if encoders is not None:
                        space = space_id(recipe)
                        if space not in encoders:
                            encoders.clear()  # One model at a time in a bounded worker.
                            encoders[space] = Encoder(roots, recipe)
                        encoder = encoders[space]
                    raw, feature = extract(image, roots, recipe, **({'encoder': encoder} if encoder else {}))
                    words = []; suffix, kind = '.visual.json', 'normalized-visual-embedding'
                    note = 'Local visual features for retrieval; no identification or OCR claim.'
                elif task['extractor'] == 'faces':
                    from .extractors.faces import extract
                    raw, feature = extract(image,page,transform,roots,recipe)
                    words = []; suffix, kind = '.faces.json', 'face-region-detections'
                    note = 'Face regions only; identities are not inferred; detection coverage is not recall.'
                elif task['extractor'] == 'spelling':
                    from .extractors.spelling import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,work,encoders)
                    suffix,kind='.spelling.json','derived-spelling-text'
                    note='Derived dictionary corrections; literal OCR and all contributing boxes retained.'
                elif task['extractor'] == 'handwriting':
                    from .extractors.handwriting import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,work,encoders)
                    suffix,kind='.handwriting.json','selected-handwritten-line-readings'
                    note='Explicit handwritten lines; line geometry only; unreviewed text and incomplete page coverage.'
                elif task['extractor'] == 'ruled-table':
                    from .extractors.ruled_table import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,work)
                    suffix,kind='.ruled-table.json','ruled-cell-text-proposal'
                    note='Deterministic ruled-cell candidates and literal OCR; forms stay in the text path.'
                elif task['extractor'] == 'scientific-table':
                    from .extractors.scientific_table import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,work)
                    suffix,kind='.scientific-table.json','scientific-table-proposal'
                    note='Unreviewed table structure from retained model output; cell text and spans require review.'
                elif task['extractor'] == 'scientific-region':
                    from .extractors.scientific_region import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,encoders=encoders,work=work)
                    suffix,kind='.scientific.json','scientific-region-proposal'
                    note='Unreviewed formula/table proposal; block coordinates only; original OCR retained.'
                elif task['extractor'] == 'document-text':
                    from .extractors.document_text import extract
                    raw,words,feature,extra_artifacts=extract(page,transform,roots,recipe,work,key)
                    suffix,kind='.alto.xml','assembled-page-alto'
                    note='Layout-bound regional OCR; explicit block order retained; full-page accuracy is unqualified.'
                elif task['extractor'] == 'document-layout':
                    from .extractors.document_layout import extract
                    raw,words,feature,extra_artifacts=extract(image,page,transform,roots,recipe,Path(temporary),key,work=work)
                    suffix,kind='.document-layout.json','document-layout-regions'
                    note='Detected document regions; article grouping and reading order require separate assessment.'
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
                    from .extractors.ocr import extract, positioned_alto
                    ocr_options={'languages':settings['languages'],'psm':settings['psm'],'timeout':settings['timeout'],
                        **({'thresholding_method':settings['thresholding_method']} if 'thresholding_method' in settings else {})}
                    if settings.get('character_boxes'):
                        from .extractors.ocr import extract_characters
                        raw,hocr,words,feature=extract_characters(image,page,transform,Path(temporary),**ocr_options)
                        extra_artifacts.append((key+'.hocr.html',hocr,'machine-ocr-character-hocr'))
                    else:
                        raw,words=extract(image,page,transform,**ocr_options)
                        words,feature=positioned_alto(raw,page,transform)
                    suffix, kind = '.alto.xml', 'machine-ocr-alto'
                    note = 'Unreviewed machine OCR; confidence is not factual verification.'
                roots.source(page.source)
                roots.root(work, 'work')
                artifacts = [Artifact(work, image.name, 'processed-page', hashlib.sha256(image.read_bytes()).hexdigest())]
                stamps = {image.name:storage.stamp(image)}
                for name, content, artifact_kind in [(key + suffix, raw, kind), *extra_artifacts]:
                    path = storage.write(root, name, content)
                    artifacts.append(Artifact(work, name, artifact_kind, hashlib.sha256(content).hexdigest()))
                    stamps[name] = storage.stamp(path)
            roots.source(page.source)
            limitations = (note, 'Coverage is this explicitly requested page/frame, not the whole object.')
            if not words and task['extractor'] not in ('visual','faces','scientific-region','scientific-table'):
                limitations += ('No words returned; this does not establish that the page is blank.',)
            if region is not None:
                limitations += ('Selected region only; retained image is a crop, not the complete page.',)
            status = 'complete' if words else 'partial'
            if task['extractor'] == 'visual': status = 'complete'
            if task['extractor'] in ('pdf-layout', 'scan-layout', 'document-layout', 'document-text','ruled-table','spelling','handwriting'): status = 'partial'  # Semantic coverage is deliberately incomplete.
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
            value, evidence = result_records.publish(roots, work, key, value)
            return {'key': key, 'state': result.status, 'reused': False, 'word_count': len(words),
                    'result_sha256': value['result_sha256'], 'evidence_sha256': evidence,
                            **({'warning': value['projection_warning']} if value.get('projection_warning') else {})}
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
        from .profiles import resolve_ocr_task
        resolve_ocr_task(task)  # Fail before launching a batch with an invalid preset.
        page_from_record(task['page'])
        if task.get('region') is not None:
            Region(**task['region'])
            require(task['extractor'] in ('ocr','visual','scientific-region','scientific-table','ruled-table','spelling'), 'Unsupported region extractor')
    return value
