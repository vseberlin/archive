"""Rebuildable catalogue/text/finding projection. Queries perform no source I/O."""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
import re
import unicodedata

from .contracts import Source, encoded, require
from .identity import valid_uid
from .receipts import read_current
from .result_records import sha


FORMAT = 'archive-research-search-projection-1'


def fold(text):
    text = re.sub(r'(?<=\w)[-\u00ad]\s*\n\s*(?=\w)', '', text.casefold())
    for old, new in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        text = text.replace(old, new)
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def tokens(text):
    return re.findall(r'\w+', fold(text))


def canonical(source, mapping):
    identity = (source['unit'], source['object_id'])
    return mapping.get(identity) or (source['object_id'] if valid_uid(source['object_id']) else None)


def mapping_rows(rows):
    mapping = {}
    for row in rows:
        require(set(row) == {'unit', 'object_id', 'uid'} and valid_uid(row['uid']), 'Invalid explicit UID binding')
        key = (row['unit'], row['object_id'])
        require(key not in mapping, 'Ambiguous explicit UID binding')
        mapping[key] = row['uid']
    return mapping


def base_entry(key, kind, source, mapping, fields, evidence, *, availability='available', reason=''):
    uid = canonical(source, mapping)
    fields = {**fields, 'source_identifier': source['object_id'], 'canonical_uid': uid or ''}
    require(all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()), 'Search fields must be literal text')
    return {'key': key, 'kind': kind, 'object_uid': uid,
            'identity_status': 'canonical' if uid else 'unmapped',
            'source_identity': source, 'fields': fields, 'evidence': evidence,
            'availability': availability, 'availability_reason': reason,
            'vector': None, 'space': None, 'metadata_adopted': False}


def receipt_entry(roots, work, key, mapping):
    # Artifact verification belongs here at explicit refresh, not in queries.
    row, result, digest = read_current(roots, work, key, verify_source=False)
    availability, reason = 'available', ''
    try:
        roots.source(result.page.source)
    except FileNotFoundError as error:
        availability, reason = 'offline', str(error)
    except (OSError, ValueError) as error:
        reason = str(error)
        availability = 'changed' if reason.startswith('Source changed') else 'unavailable'
    words = row['words']
    lines = ''
    previous = None
    for word in words:
        box = word.get('region', {})
        new_line = previous and abs(box.get('y', 0) - previous.get('y', 0)) > max(box.get('height', 0), previous.get('height', 0)) * .5
        lines += ('\n' if new_line else ' ' if lines else '') + word['text']
        previous = box
    fields = {'text': ' '.join(w['text'] for w in words), 'positioned_lines': lines}
    entry = base_entry(key, 'extraction', asdict(result.page.source), mapping, fields,
                       {'receipt_key': key, 'receipt_sha256': digest, 'page': asdict(result.page),
                        'region': asdict(result.region) if result.region else None},
                       availability=availability, reason=reason)
    entry['extractor'] = result.recipe.extractor
    if result.recipe.extractor == 'visual':
        from .extractors.visual_features import space_id
        artifact = next(a for a in result.artifacts if a.kind == 'normalized-visual-embedding')
        feature = json.loads(roots.artifact(artifact).read_text())
        require(feature == row['feature'], 'Visual receipt differs from artifact')
        vector = feature['vector']
        require(len(vector) == feature['dimensions'] == result.recipe.record()['parameters']['dimensions']
                and all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                and abs(sum(v*v for v in vector) - 1) < 1e-4, 'Invalid normalized feature vector')
        entry.update(vector=vector, space=space_id(result.recipe))
    return entry


def catalogue_entries(roots, spec, mapping):
    """Explicit CSV field mapping; repeated file rows stay distinct occurrences."""
    require(set(spec) <= {'source', 'object_field', 'fields', 'unit', 'aliases'} and
            {'source', 'object_field', 'fields', 'unit'} <= spec.keys(), 'Explicit catalogue field mapping required')
    source = Source(**spec['source'])
    require(source.sha256, 'Catalogue snapshot needs a checksum')
    path = roots.source(source, verify_hash=True)
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == source.sha256, 'Catalogue changed during refresh')
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
    require(spec['object_field'] in (reader.fieldnames or []) and
            set(spec['fields'].values()) <= set(reader.fieldnames or []), 'Unknown catalogue columns')
    aliases = spec.get('aliases', {})
    require(isinstance(aliases, dict) and all(isinstance(v, list) and all(isinstance(x, str) for x in v)
                                           for v in aliases.values()), 'Invalid documented catalogue aliases')
    for number, row in enumerate(reader, 2):
        object_id = row[spec['object_field']]
        require(isinstance(object_id, str), 'Catalogue row has a malformed identity field')
        fields = {name: row[column] or '' for name, column in spec['fields'].items()}
        fields['identifier'] = object_id
        fields['aliases'] = '\n'.join(aliases.get(object_id, []))
        evidence = {'catalogue': asdict(source), 'row_number': number, 'row_sha256': sha(row),
                    'field_columns': spec['fields'], 'alias_basis': 'explicit selection, not inferred'}
        key = 'catalogue:' + sha({'unit': spec['unit'], 'snapshot': source.sha256, 'row': number})
        yield base_entry(key, 'catalogue', {'unit': spec['unit'], 'object_id': object_id}, mapping, fields, evidence)


def finding_entries(roots, source_record, mapping):
    """Read the existing project-scoped assertion history, never adopt it."""
    source = Source(**source_record)
    require(source.sha256, 'Finding history needs a checksum')
    data = roots.source(source, verify_hash=True).read_bytes()
    require(hashlib.sha256(data).hexdigest() == source.sha256, 'Finding history changed during refresh')
    current = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (row.get('case_key', 'photo'), row['key'])
        previous = current.get(key)
        require(row.get('metadata_adopted') is False and
                row['revision'] == (previous['revision'] + 1 if previous else 0) and
                row['supersedes_revision'] == (previous['revision'] if previous else None), 'Broken finding history')
        require(row['review'] in ('unreviewed', 'accepted', 'rejected', 'unresolved') and row['citation_keys'],
                'Finding lacks review state or evidence')
        current[key] = row
    for (case, key), row in current.items():
        if row['review'] == 'rejected':
            continue
        subject = row.get('subject_uid', row.get('subject'))
        require(isinstance(subject, str) and subject, 'Finding subject missing')
        fields = {k: row.get(k, '') for k in ('value', 'reason', 'scope', 'uncertainty', 'relation')}
        fields['identifier'] = subject
        entry = base_entry('finding:' + sha([source.unit, source.object_id, case, key]), 'finding',
                           {'unit': source.unit, 'object_id': subject}, mapping, fields,
                           {'history': asdict(source), 'case_key': case, 'key': key, 'revision': row['revision'],
                            'citation_keys': row['citation_keys'], 'citation_digests': row.get('citation_digests', {}),
                            'reviewer': row.get('reviewer'), 'reviewed_at': row.get('reviewed_at')})
        entry['review'] = row['review']
        yield entry


def seal(value):
    value['projection_sha256'] = sha({k: v for k, v in value.items() if k != 'projection_sha256'})
    return value


def validate(value):
    require(value.get('format') == FORMAT and value.get('metadata_adopted') is False,
            'Unsupported search projection')
    require(value.get('projection_sha256') == sha({k: v for k, v in value.items() if k != 'projection_sha256'}),
            'Search projection changed; refresh explicitly')
    return value


def build_projection(roots, work, selection):
    keys = selection.get('keys', [])
    require(isinstance(keys, list) and len(keys) == len(set(keys)), 'Distinct selected receipts required')
    mapping = mapping_rows(selection.get('identity_bindings', []))
    entries, unavailable = [], []
    for key in keys:
        try:
            entries.append(receipt_entry(roots, work, key, mapping))
        except (OSError, ValueError, KeyError, StopIteration) as error:
            unavailable.append({'key': key, 'reason': str(error)})
    for kind, specs, loader in (('catalogue', selection.get('catalogues', []), catalogue_entries),
                               ('finding', selection.get('findings', []), finding_entries)):
        for position, spec in enumerate(specs):
            try:
                entries.extend(list(loader(roots, spec, mapping)))
            except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
                unavailable.append({'key': f'{kind}:{position}', 'reason': str(error)})
    require(len({e['key'] for e in entries}) == len(entries), 'Duplicate search entry')
    postings = defaultdict(list)
    for number, entry in enumerate(entries):
        terms = Counter(tokens(' '.join(entry['fields'].values())))
        entry['token_count'] = sum(terms.values())
        for token, frequency in terms.items():
            postings[token].append([number, frequency])
    coverage = {'selected_receipts': len(keys), 'indexed_receipts': sum(e['kind'] == 'extraction' for e in entries),
                'available_receipts': sum(e['kind'] == 'extraction' and e['availability'] == 'available' for e in entries),
                'selected_catalogues': len(selection.get('catalogues', [])),
                'selected_finding_histories': len(selection.get('findings', [])),
                'entries_by_kind': dict(Counter(e['kind'] for e in entries)),
                'entries_by_availability': dict(Counter(e['availability'] for e in entries)),
                'unmapped_entries': sum(e['object_uid'] is None for e in entries),
                'failed_inputs': len(unavailable), 'whole_archive': False}
    return seal({'format': FORMAT, 'metadata_adopted': False,
                 'refreshed_at': datetime.now(timezone.utc).isoformat(), 'selection': selection,
                 'entries': entries, 'postings': dict(postings), 'unavailable': unavailable,
                 'coverage': coverage, 'limitations': ['Availability is observed at refresh, not guaranteed at query time.',
                 'Selected material only. Similarity and spelling alternatives do not establish identity.',
                 'Unmapped identities remain explicit; canonical UIDs are never inferred from matching bytes.']})


def query(value, text, *, limit=20, offset=0, kinds=None, unit=None, fuzzy=False):
    validate(value)
    require(isinstance(text, str) and text.strip() and len(text) <= 300, 'Bounded search query required')
    require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0, 'Invalid page')
    terms = list(dict.fromkeys(tokens(text)))
    require(terms, 'Search query has no searchable terms')
    postings = value['postings']; matches = []; alternatives = {}
    for term in terms:
        variants = [term] if term in postings else []
        if not variants and fuzzy and len(term) >= 4:
            import difflib
            variants = difflib.get_close_matches(term, sorted(postings), n=3, cutoff=.82)
            alternatives[term] = variants
        numbers = {number for variant in variants for number, _ in postings[variant]}
        matches.append(numbers)
    selected = set.intersection(*matches)
    average = max(1., sum(e['token_count'] for e in value['entries']) / max(1, len(value['entries'])))
    matched_terms = list(dict.fromkeys(variant for term in terms for variant in alternatives.get(term, [term])))
    frequencies = {term: dict(postings.get(term, [])) for term in matched_terms}
    hits = []
    for number in selected:
        entry = value['entries'][number]
        if kinds and entry['kind'] not in kinds or unit and entry['source_identity']['unit'] != unit:
            continue
        exact = text.casefold() in {entry.get('object_uid', '').casefold() if entry.get('object_uid') else '',
                                  entry['fields'].get('identifier', '').casefold()}
        score = 1000. if exact else 0.
        for term in matched_terms:
            frequency = frequencies[term].get(number, 0)
            df = len(frequencies[term])
            score += math.log(1 + (len(value['entries']) - df + .5)/(df + .5)) * (
                frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * entry['token_count']/average))) if frequency else 0
        reasons = ['exact identifier'] if exact else []
        reasons += ['spelling alternative' if alternatives else 'text match']
        matched_fields = [field for field, content in entry['fields'].items()
                          if any(term in tokens(content) for term in matched_terms)]
        hits.append({'target': entry['key'], 'object_uid': entry['object_uid'], 'kind': entry['kind'],
                     'identity_status': entry['identity_status'], 'score': score,
                     'match_reasons': reasons, 'matched_fields': matched_fields,
                     'snippet': ' | '.join(entry['fields'][f] for f in matched_fields)[:500],
                     'evidence': entry['evidence'], 'availability': entry['availability'],
                     'review': entry.get('review'), 'identity_confirmed': False})
    hits.sort(key=lambda h: (-h['score'], h['target']))
    return {'query': text, 'hits': hits[offset:offset+limit], 'total_matches': len(hits),
            'offset': offset, 'limit': limit, 'next_offset': offset+limit if offset+limit < len(hits) else None,
            'spelling_alternatives': alternatives, 'coverage': value['coverage'],
            'refreshed_at': value['refreshed_at'], 'projection_sha256': value['projection_sha256']}


def visual_query(value, key, *, limit=20, offset=0):
    validate(value)
    require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0, 'Invalid page')
    query_entry = next((e for e in value['entries'] if e['key'] == key), None)
    require(query_entry and query_entry['vector'] is not None, 'Visual query unavailable')
    hits, incompatible = [], []
    for entry in value['entries']:
        if entry['key'] == key or entry['vector'] is None:
            continue
        if entry['space'] != query_entry['space']:
            incompatible.append(entry['key']); continue
        score = max(-1., min(1., sum(a*b for a, b in zip(entry['vector'], query_entry['vector']))))
        hits.append({'target': entry['key'], 'object_uid': entry['object_uid'], 'similarity': score,
                     'evidence': entry['evidence'], 'availability': entry['availability'],
                     'match_reasons': ['visual similarity'], 'identity_confirmed': False})
    hits.sort(key=lambda h: (-h['similarity'], h['target']))
    return {'hits': hits[offset:offset+limit], 'eligible': len(hits), 'incompatible_space': incompatible,
            'same_object_excluded': False, 'offset': offset, 'limit': limit,
            'coverage': value['coverage'], 'refreshed_at': value['refreshed_at']}
