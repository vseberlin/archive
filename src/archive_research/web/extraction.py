"""Read existing page receipts for the pilot; never run processors in requests."""
import hashlib
import json
from pathlib import Path
import re

from django.conf import settings

from archive_research.common import plain_file, require, work_root


def configured():
    return bool(settings.PROJECT.get('extraction_roots') and settings.PROJECT.get('extraction_plan'))


def snapshot():
    if not configured():
        return None, [], ''
    from archive_research import Result
    from archive_research.runner import load_plan, page_from_record
    from archive_research.sources import Roots
    roots_path = Path(settings.PROJECT['extraction_roots'])
    plan_path = Path(settings.PROJECT['extraction_plan'])
    require(roots_path.parent == plan_path.parent, 'Extraction configuration and plan must share their approved directory')
    work_root(roots_path.parent)
    roots = Roots.from_toml(roots_path)
    root = roots.root('artifacts', 'work')
    plan = load_plan(plan_path)
    summary_path = root / 'run-summary.json'
    if not summary_path.exists():
        return roots, [], hashlib.sha256(plan_path.read_bytes()).hexdigest()
    summary_bytes = plain_file(summary_path).read_bytes()
    summary = json.loads(summary_bytes)
    require(summary.get('plan_sha256') == hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            'Extraction plan changed; results must be refreshed explicitly')
    tasks = {task['id']: task for task in plan['tasks']}
    rows = []
    digest = hashlib.sha256(plan_path.read_bytes() + roots_path.read_bytes() + summary_bytes)
    for attempt in summary['attempts']:
        task = tasks[attempt['id']]
        key = attempt.get('key')
        if not key:
            rows.append({'id': task['id'], 'state': 'error', 'error': attempt.get('error', 'Worker unavailable'),
                         'available': False, 'key': '', 'words': [], 'page': task['page']})
            continue
        require(re.fullmatch('[0-9a-f]{64}', key), 'Invalid extraction receipt key')
        data = plain_file(root / (key + '.json')).read_bytes()
        digest.update(data)
        receipt = json.loads(data)
        result = Result.from_record(receipt['result'])
        require(result.page == page_from_record(task['page']) and result.recipe.extractor == task['extractor'],
                'Result does not belong to the requested page')
        from archive_research import Region
        expected_region = Region(**task['region']) if task.get('region') is not None else None
        require(result.region == expected_region and
                result.recipe.cache_key(result.page, result.region) == key, 'Result recipe key differs')
        row = {'id': task['id'], 'key': key, 'state': attempt['state'], 'result': result,
               'page': receipt['result']['page'], 'words': receipt['words'], 'receipt': receipt,
               'available': False, 'error': attempt.get('error', ''),
               'text': ' '.join(w['text'] for w in receipt['words'])}
        if result.status in ('complete', 'partial') and attempt['state'] in ('complete', 'partial'):
            try:
                roots.source(result.page.source)
                for artifact in result.artifacts:
                    path = roots.artifact(artifact)
                    stamp = receipt['artifact_stamps'][artifact.path]
                    info = path.stat()
                    require((info.st_size, info.st_mtime_ns) == (stamp['size_bytes'], stamp['mtime_ns']),
                            'Prepared extraction artifact changed')
                row['available'] = True
            except (OSError, ValueError) as error:
                row['error'] = str(error)
        rows.append(row)
    return roots, rows, digest.hexdigest()


def lookup(key, revision):
    roots, rows, current = snapshot()
    require(revision and revision == current, 'Extraction results changed; reload the search')
    row = next((r for r in rows if r['key'] == key), None)
    require(row is not None and row['available'], 'Bound extraction source or result unavailable')
    return roots, row, current


def search(query):
    roots, rows, revision = snapshot()
    tokens = query.casefold().split()
    hits = []
    for row in rows:
        if tokens and not all(t in row.get('text', '').casefold() for t in tokens):
            continue
        matching = [i for i, word in enumerate(row['words']) if any(t in word['text'].casefold() for t in tokens)]
        offset = max(0, matching[0]-8) if matching else 0
        hits.append({**row, 'word': matching[0] if matching else None,
                     'snippet': ' '.join(w['text'] for w in row['words'][offset:offset+35])})
    return hits, rows, revision


def word_region(row, number):
    from archive_research import Region
    from archive_research.geometry import processed_region
    require(type(number) is int and 0 <= number < len(row['words']), 'Word outside extraction result')
    word = row['words'][number]
    region = Region(**word['region'])
    shown = processed_region(region, row['result'].transform, row['result'].page)
    return word, {name: getattr(shown, name)*100 for name in ('x', 'y', 'width', 'height')}


def preview_bytes(roots, row):
    artifact = next(a for a in row['result'].artifacts if a.kind == 'processed-page')
    path = roots.artifact(artifact)
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == artifact.sha256, 'Prepared image digest changed')
    roots.source(row['result'].page.source)
    return data


def visual_leads():
    path = settings.PROJECT.get('visual_leads')
    if not path or not configured():
        return None, [], ''
    from archive_research import Source, Roots
    config = Path(settings.PROJECT['extraction_roots'])
    path = Path(path)
    require(path.parent == config.parent, 'Visual lead projection outside configured trial')
    work_root(path.parent)
    data = plain_file(path).read_bytes()
    value = json.loads(data)
    require(value.get('format') == 'archive-visual-leads-1' and value.get('metadata_adopted') is False,
            'Unsupported visual lead projection')
    roots = Roots.from_toml(config)
    leads = []
    for index, lead in enumerate(value['leads']):
        assets = []
        for side in ('candidate', 'reference'):
            asset = {**lead[side], 'side': side, 'available': False}
            try:
                roots.source(Source(**asset['source']))
                roots.source(Source(**asset['preview']))
                asset['available'] = True
            except (OSError, ValueError) as error:
                asset['error'] = str(error)
            assets.append(asset)
        leads.append({'index': index, 'assets': assets, 'similarity': lead['similarity']})
    return roots, leads, hashlib.sha256(data + config.read_bytes()).hexdigest()


def lead_image(index, side, revision):
    from archive_research import Source
    roots, leads, current = visual_leads()
    require(revision and revision == current, 'Visual leads changed; reload the comparison')
    require(0 <= index < len(leads) and side in ('candidate', 'reference'), 'Unknown visual lead')
    asset = next(a for a in leads[index]['assets'] if a['side'] == side)
    require(asset['available'], 'Visual source unavailable; no preview substitution')
    roots.source(Source(**asset['source']))
    preview = Source(**asset['preview'])
    path = roots.source(preview)
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == preview.sha256, 'Visual preview changed')
    roots.source(Source(**asset['source']))
    require(data.startswith(b'\xff\xd8'), 'Expected the existing JPEG research preview')
    return data


def research_packet():
    """Refresh one explicitly configured local packet; never trust stored availability."""
    name = settings.PROJECT.get('research_packet')
    if not name or not configured():
        return None, None, ''
    from archive_research.pipeline import current_packet
    from archive_research.sources import Roots
    from archive_research.storage import target
    from archive_research.contracts import encoded
    from archive_research.profiles import research_profiles
    config = Path(settings.PROJECT['extraction_roots'])
    work_root(config.parent)
    roots = Roots.from_toml(config)
    data = target(roots.root('artifacts', 'work'), name).read_bytes()
    stored = json.loads(data)
    packet = current_packet(roots, 'artifacts', stored)
    response_name=settings.PROJECT.get('research_assessment_response')
    transport_name=settings.PROJECT.get('research_assessment_transport')
    require(bool(response_name)==bool(transport_name),'Assessment needs response and transport bindings')
    if response_name:
        require(all(e['available'] for e in packet['evidence']),'Assessment evidence unavailable')
        from archive_research.assessment.io import validate_response
        response=json.loads(target(roots.root('artifacts','work'),response_name).read_text())
        transport=json.loads(target(roots.root('artifacts','work'),transport_name).read_text())
        packet['assessment']=validate_response(response,stored,transport)
    revision = hashlib.sha256(data + config.read_bytes() + encoded(packet).encode()).hexdigest()
    for item in packet['evidence']:
        region = item['display_region']
        item['display_percent'] = {k: region[k]*100 for k in ('x','y','width','height')} if region else None
    return roots, packet, revision


def packet_image(index, revision, candidate=None, enhancement=None):
    from archive_research.evidence import evidence_image
    roots, packet, current = research_packet()
    require(packet is not None and revision and revision == current, 'Evidence packet changed; reload it')
    require(type(index) is int and 0 <= index < len(packet['evidence']), 'Unknown packet evidence')
    return evidence_image(roots, 'artifacts', packet['evidence'][index], candidate, enhancement)
