"""Bounded page extraction and readable resumable receipts; one manual writer."""
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from xml.etree.ElementTree import ParseError

from .contracts import Artifact, Page, Recipe, Result, Source, require
from .preparation import render
from .sources import Roots
from . import storage


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
    require(extractor in ('pdf-text', 'ocr'), 'Unknown extractor')
    dependencies = {'Pillow': importlib.metadata.version('Pillow'),
                    'pdftoppm': tool_version(['pdftoppm', '-v'])}
    command = ['pdftotext', '-v'] if extractor == 'pdf-text' else ['tesseract', '--version']
    dependencies[command[0]] = tool_version(command)
    settings = {'dpi': 300, 'clockwise': 0, 'languages': 'deu+eng', 'psm': 3, 'timeout': 240,
                **task.get('settings', {})}
    require(set(settings) == {'dpi', 'clockwise', 'languages', 'psm', 'timeout'}, 'Unknown extractor setting')
    require(type(settings['timeout']) is int and 1 <= settings['timeout'] <= 600, 'Invalid bounded timeout')
    return Recipe.create(extractor, '1', settings=settings, dependencies=dependencies,
                         coverage={'member': task['page']['member'], 'kind': task['page']['kind'],
                                   'index': task['page']['index']}, output='bbox-xhtml' if extractor == 'pdf-text' else 'alto')


def cached(roots, work, key, page, recipe):
    root = roots.root(work, 'work')
    path = storage.target(root, key + '.json')
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    result = Result.from_record(value['result'])
    require(result.page == page and result.recipe == recipe, 'Cached result binding differs')
    if result.status not in ('complete', 'partial'):
        return None
    roots.source(page.source)
    for artifact in result.artifacts:
        file = roots.artifact(artifact)
        require(storage.stamp(file) == value['artifact_stamps'][artifact.path], 'Cached artifact changed')
    require(len(result.artifacts) == 2, 'Incomplete cached artifacts')
    return value


def extract_one(roots, work, task):
    page = page_from_record(task['page'])
    recipe = recipe_for(task)
    key = recipe.cache_key(page)
    root = roots.root(work, 'work')
    start = time.monotonic()
    with storage.lock(root):
        try:
            roots.source(page.source)
            prior = cached(roots, work, key, page, recipe)
            if prior is not None:
                return {'key': key, 'state': prior['result']['status'], 'reused': True,
                        'word_count': len(prior['words'])}
            settings = recipe.record()['parameters']['settings']
            with tempfile.TemporaryDirectory(prefix='.page-', dir=root) as temporary:
                image, transform, rotation = render(roots, page, Path(temporary), dpi=settings['dpi'],
                                                     clockwise=settings['clockwise'])
                if task['extractor'] == 'pdf-text':
                    from .pdf_text import extract
                    raw, words = extract(roots, page)
                    suffix, kind = '.bbox.xhtml', 'native-or-embedded-pdf-text'
                    note = 'Native or pre-existing embedded PDF text; its original OCR provenance may be unknown.'
                else:
                    from .ocr import extract
                    raw, words = extract(image, page, transform, languages=settings['languages'],
                                         psm=settings['psm'], timeout=settings['timeout'])
                    suffix, kind = '.alto.xml', 'machine-ocr-alto'
                    note = 'Unreviewed machine OCR; confidence is not factual verification.'
                roots.source(page.source)
                roots.root(work, 'work')
                artifacts, stamps = [], {}
                for name, content, artifact_kind in [(key + '.png', image.read_bytes(), 'processed-page'),
                                                      (key + suffix, raw, kind)]:
                    path = storage.write(root, name, content)
                    artifacts.append(Artifact(work, name, artifact_kind, hashlib.sha256(content).hexdigest()))
                    stamps[name] = storage.stamp(path)
            roots.source(page.source)
            limitations = (note, 'Coverage is this explicitly requested page/frame, not the whole object.')
            if not words:
                limitations += ('No words returned; this does not establish that the page is blank.',)
            result = Result(page, recipe, 'complete' if words else 'partial', time.monotonic()-start,
                            tuple(artifacts), transform=transform, limitations=limitations)
            value = {'result': result.record(), 'words': words, 'artifact_stamps': stamps,
                     'display_rotation': rotation,
                     'metrics': {'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                 'child_peak_rss_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                                 'retained_bytes': sum(s['size_bytes'] for s in stamps.values())}}
            storage.write_json(root, key + '.json', value)
            return {'key': key, 'state': result.status, 'reused': False, 'word_count': len(words)}
        except (OSError, ValueError, KeyError, ParseError, subprocess.SubprocessError) as error:
            status = 'changed_source' if str(error).startswith('Source changed') else 'error'
            result = Result(page, recipe, status, time.monotonic()-start, limitations=(str(error)[:1500],))
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
    return value
