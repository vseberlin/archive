"""Local profile-guided packets from explicit retained receipts; no network or inference."""
from dataclasses import asdict
import hashlib
import json
import re

from .contracts import Region, Result, encoded, require, text
from .geometry import processed_region
from .storage import target, stamp
from .result_records import identity


def references(values, count):
    require(isinstance(values, list) and len(values) == len(set(values)) and
            all(type(i) is int and 0 <= i < count for i in values),
            'Evidence references must be distinct packet positions')


def selected_words(words, region):
    """Keep original readings/order; select by word centre, without correcting OCR."""
    result = []
    for word in words:
        box = Region(**word['region'])
        require(text(word['text']), 'Invalid extracted word')
        if region is None or (region.x <= box.x + box.width/2 <= region.x + region.width and
                              region.y <= box.y + box.height/2 <= region.y + region.height):
            result.append(word)
    return result


def read_evidence(roots, work, item):
    require(set(item) <= {'key', 'label', 'kind', 'region', 'profiles', 'lineage', 'attribution', 'discovery_key', 'web_reference'} and
            all(text(item.get(k)) for k in ('key', 'label', 'kind', 'lineage', 'attribution')),
            'Evidence needs a receipt, label, kind, lineage and attribution')
    require(re.fullmatch('[0-9a-f]{64}', item['key']), 'Invalid receipt key')
    require(item['kind'] in ('scene', 'detail', 'caption', 'inscription', 'document', 'person', 'face'),
            'Unsupported evidence kind')
    data = target(roots.root(work, 'work'), item['key'] + '.json').read_bytes()
    receipt = json.loads(data)
    result = Result.from_record(receipt['result'])
    require(result.recipe.key_matches(result.page, result.region, item['key']), 'Receipt key differs')
    region = Region(**item['region']) if item.get('region') is not None else result.region
    shown = processed_region(region, result.transform, result.page) if region and result.transform else None
    words = selected_words(receipt['words'], region)
    available, error = result.status in ('complete', 'partial'), ''
    web_reference = None
    if available:
        try:
            roots.source(result.page.source)
            if result.recipe.extractor in ('scan-layout', 'visual') or result.recipe.record()['parameters'].get('settings',{}).get('caption_origin'):
                from .receipts import read_current
                read_current(roots,work,item['key'])
            if item.get('web_reference'):
                from .external_evidence import read_reference
                web_reference=read_reference(roots,item['web_reference'],result.page.source)
            require(any(a.kind == 'processed-page' for a in result.artifacts), 'Missing prepared page')
            for artifact in result.artifacts:
                path = roots.artifact(artifact)
                require(stamp(path) == receipt['artifact_stamps'][artifact.path], 'Prepared artifact changed')
        except (OSError, ValueError) as exc:
            available, error = False, str(exc)
    else:
        error = '; '.join(result.limitations)
    # Source page and its OCR variants count as one source, not multiple witnesses.
    page = asdict(result.page)
    source_group = {k: page['source'][k] for k in ('root', 'path', 'size_bytes', 'mtime_ns')}
    source_group.update(kind=page['kind'], index=page['index'])
    discovery, discovery_sha = None, ''
    if item.get('discovery_key'):
        require(region is None, 'Attach whole-page discovery to a whole-page evidence view')
        require(re.fullmatch('[0-9a-f]{64}', item['discovery_key']), 'Invalid discovery receipt key')
        discovery_data = target(roots.root(work, 'work'), item['discovery_key']+'.json').read_bytes()
        found = json.loads(discovery_data)
        found_result = Result.from_record(found['result'])
        require(found_result.page == result.page and found_result.recipe.extractor in ('text-regions', 'line-ocr')
                and found_result.recipe.key_matches(found_result.page, None, item['discovery_key']),
                'Discovery belongs to another page or recipe')
        discovery_sha = identity(found, discovery_data)
        discovery = found.get('discovery')
        if discovery is None:
            discovery = {'coverage': {'status': 'processing_failed', 'scan_complete': False},
                         'regions': [], 'error': '; '.join(found_result.limitations)}
        for artifact in found_result.artifacts:
            try:
                path = roots.artifact(artifact)
                require(stamp(path) == found['artifact_stamps'][artifact.path], 'Discovery artifact changed')
            except (OSError, ValueError) as exc:
                available, error = False, str(exc)
        for candidate in discovery['regions']:
            shown_candidate = processed_region(Region(**candidate['region']), result.transform, result.page)
            candidate['display_percent'] = {k: getattr(shown_candidate,k)*100 for k in ('x','y','width','height')}
    return {**item, 'page': page, 'region': asdict(region) if region else None,
            'display_region': asdict(shown) if shown else None,
            'receipt_sha256': identity(receipt, data),
            'extractor': result.recipe.record(), 'state': result.status,
            'available': available, 'error': error, 'text': ' '.join(w['text'] for w in words),
            'words': words, 'limitations': list(result.limitations), 'source_group': source_group,
            'layout': receipt.get('feature') if result.recipe.extractor in ('pdf-layout','scan-layout') else None,
            'faces': receipt.get('feature') if result.recipe.extractor == 'faces' else None,
            'web_context':web_reference,
            'discovery': discovery, 'discovery_sha256': discovery_sha, 'metadata_adopted': False}


def code_candidates(reading, dictionary):
    """Suggest only explicit dictionary entries; preserve original spelling/spacing."""
    require(text(reading), 'Literal reading required')
    ambiguous = ({'0', 'O'}, {'1', 'I'})
    found = []
    for entry in dictionary:
        value = entry['value']
        require(text(value), 'Dictionary value required')
        if len(value) != len(reading):
            continue
        changes = []
        for index, (observed, candidate) in enumerate(zip(reading, value)):
            if observed == candidate:
                continue
            if not any(observed in group and candidate in group for group in ambiguous):
                break
            changes.append({'position': index, 'observed': observed, 'candidate': candidate})
        else:
            found.append({**entry, 'changes': changes, 'status': 'unreviewed suggestion'})
    return {'literal_reading': reading, 'candidates': found, 'metadata_adopted': False}


def build_packet(roots, work, selection, profiles):
    """Profiles are supplied by the existing research_routes owner, not registered here."""
    require(set(selection) <= {'format', 'metadata_adopted', 'case_key', 'question', 'profiles',
                              'evidence', 'caption_links', 'hypotheses', 'code_readings', 'omissions',
                              'cross_references'},
            'Unexpected selection fields')
    require(selection.get('format') == 'archive-research-selection-1' and
            selection.get('metadata_adopted') is False, 'Invalid local research selection')
    require(text(selection.get('case_key')) and text(selection.get('question')), 'Case/question required')
    require(selection.get('profiles') == list(profiles) and profiles, 'Resolved profile selection differs')
    items = selection.get('evidence')
    require(isinstance(items, list) and 1 <= len(items) <= 32, 'Select 1–32 evidence regions')
    evidence = []
    for item in items:
        chosen = item.get('profiles', selection['profiles'])
        require(isinstance(chosen, list) and chosen and all(p in profiles for p in chosen)
                and len(chosen) == len(set(chosen)), 'Region profiles must belong to the packet')
        evidence.append(read_evidence(roots, work, {**item, 'profiles': chosen}))
    count = len(evidence)
    links = []
    for link in selection.get('caption_links', []):
        require(set(link) == {'caption', 'subject', 'basis', 'quote', 'position', 'note'},
                'Caption links require explicit basis, quote, position and note')
        references([link['caption'], link['subject']], count)
        caption, subject = evidence[link['caption']], evidence[link['subject']]
        require(caption['kind'] in ('caption', 'inscription', 'document') and
                subject['kind'] in ('scene', 'detail', 'person', 'face'), 'Invalid caption/subject roles')
        require(link['basis'] in ('explicit-caption', 'proximity'), 'Unknown caption link basis')
        require(all(isinstance(link[k], str) for k in ('quote', 'position', 'note')) and text(link['note']),
                'Explain the proposed caption link')
        available = caption['available'] and subject['available']
        if link['basis'] == 'explicit-caption':
            require(text(link['quote']), 'Explicit caption links need a literal quotation')
            if available:
                require(link['quote'] in caption['text'], 'Caption quote is not in the selected evidence')
            if subject['kind'] in ('person', 'face'):
                require(text(link['position']), 'Documentary person links require an explicit position')
        links.append({**link, 'status': 'documentary proposal' if available and
                      link['basis'] == 'explicit-caption' else 'proximity only' if available else 'unavailable',
                      'identity_confirmed': False})
    cross_references = []
    for link in selection.get('cross_references', []):
        require(set(link) == {'source', 'target', 'basis', 'quote', 'note'},
                'Cross-reference needs source, target, basis, quote and note')
        references([link['source'], link['target']], count)
        require(link['basis'] in ('textual', 'visual') and text(link['note']) and
                isinstance(link['quote'], str), 'Invalid cross-reference basis or note')
        source, target_item = evidence[link['source']], evidence[link['target']]
        if link['basis'] == 'textual':
            require(text(link['quote']) and link['quote'] in source['text'],
                    'Cross-reference quote absent from source evidence')
        else:
            require(not link['quote'], 'Visual comparison is not a literal text quotation')
        cross_references.append({**link, 'status': 'unreviewed lead' if
                                 source['available'] and target_item['available'] else 'unavailable',
                                 'identity_confirmed': False})
    hypotheses = []
    for hypothesis in selection.get('hypotheses', []):
        require(set(hypothesis) == {'claim', 'supports', 'contradicts'} and text(hypothesis['claim']),
                'Hypotheses need a claim and separate evidence references')
        for field in ('supports', 'contradicts'):
            references(hypothesis[field], count)
        hypotheses.append({**hypothesis, 'status': 'unreviewed hypothesis'})
    readings = []
    for reading in selection.get('code_readings', []):
        require(set(reading) == {'evidence', 'literal_reading', 'dictionary'}, 'Invalid code reading')
        references([reading['evidence']], count)
        item = evidence[reading['evidence']]
        require('tubes' in item['profiles'], 'Type-code candidates require the tube profile')
        require(reading['literal_reading'] in item['text'], 'Literal reading absent from selected text')
        for entry in reading['dictionary']:
            require(set(entry) == {'value', 'evidence'}, 'Dictionary candidates need source evidence')
            references([entry['evidence']], count)
            require(entry['value'] in evidence[entry['evidence']]['text'], 'Dictionary value absent from evidence')
        readings.append({'evidence': reading['evidence'],
                         **code_candidates(reading['literal_reading'], reading['dictionary'])})
    omissions = selection.get('omissions', [])
    require(isinstance(omissions, list) and all(text(s) for s in omissions), 'Invalid omission notes')
    groups = []
    for index, item in enumerate(evidence):
        prior = next((g for g in groups if g['source'] == item['source_group']), None)
        if prior:
            prior['evidence'].append(index)
        else:
            groups.append({'source': item['source_group'], 'evidence': [index]})
    return {'format': 'archive-research-packet-1', 'metadata_adopted': False,
            'external_submission_authorized': False, 'case_key': selection['case_key'],
            'question': selection['question'], 'selection': json.loads(encoded(selection)),
            'profiles': profiles, 'evidence': evidence, 'caption_links': links, 'hypotheses': hypotheses,
            'code_readings': readings, 'source_groups': groups, 'omissions': omissions,
            'cross_references': cross_references,
            'limitations': ['Local preparation only; no model assessment or curator decision.',
                            'Source groups deduplicate page/OCR variants, not unknown republication lineage.',
                            'Caption proximity and model/visual similarity do not establish identity.']}


def refresh_packet(roots, work, stored, profiles):
    require(stored.get('format') == 'archive-research-packet-1' and stored.get('metadata_adopted') is False
            and stored.get('external_submission_authorized') is False, 'Unsupported local packet')
    current = build_packet(roots, work, stored['selection'], profiles)
    require(current['profiles'] == stored['profiles'], 'Profile guidance changed; rebuild the packet')
    require([e['receipt_sha256'] for e in current['evidence']] ==
            [e['receipt_sha256'] for e in stored['evidence']], 'Evidence receipt changed; rebuild the packet')
    require([e.get('discovery_sha256','') for e in current['evidence']] ==
            [e.get('discovery_sha256','') for e in stored['evidence']], 'Discovery changed; rebuild the packet')
    return current


def evidence_image(roots, work, item, candidate=None, enhancement=None):
    """Read the retained prepared image; HTTP never renders, crops or extracts."""
    fresh = read_evidence(roots, work, {k: item[k] for k in
                          ('key', 'label', 'kind', 'region', 'profiles', 'lineage', 'attribution', 'discovery_key', 'web_reference') if k in item})
    require(fresh['available'] and fresh['receipt_sha256'] == item['receipt_sha256'],
            'Bound evidence unavailable or changed')
    key = item['key']
    kind = 'processed-page'
    require(enhancement is None or candidate is not None, 'Enhancement needs a text candidate')
    if candidate is not None:
        require(type(candidate) is int and fresh['discovery'] is not None and
                0 <= candidate < len(fresh['discovery']['regions']) and
                fresh['discovery_sha256'] == item['discovery_sha256'], 'Unknown or changed text candidate')
        require(fresh['discovery']['regions'][candidate]['crop'], 'Candidate crop was not prepared')
        key, kind = item['discovery_key'], f'text-region-crop-{candidate}'
        if enhancement is not None:
            choices = fresh['discovery']['regions'][candidate].get('enhancements', [])
            require(isinstance(enhancement, str) and any(e['recipe'] == enhancement for e in choices),
                    'Unknown or unprepared enhancement')
            if enhancement != 'original':
                kind += '-' + enhancement
    receipt = json.loads(target(roots.root(work, 'work'), key + '.json').read_text())
    result = Result.from_record(receipt['result'])
    artifact = next((a for a in result.artifacts if a.kind == kind), None)
    require(artifact is not None, 'Prepared evidence image missing from receipt')
    data = roots.artifact(artifact).read_bytes()
    require(hashlib.sha256(data).hexdigest() == artifact.sha256, 'Prepared evidence image changed')
    roots.source(result.page.source)
    require(data.startswith(b'\x89PNG'), 'Expected a prepared PNG')
    return data
