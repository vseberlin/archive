"""Rebuildable catalogue/text/finding projection. Queries perform no source I/O."""
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from pathlib import PurePosixPath

from .contracts import Source, encoded, require
from .identity import valid_uid
from .receipts import read_current
from .result_records import sha


FORMAT = 'archive-research-search-projection-1'


def fold(text):
    text = re.sub(r'(?<=\w)[-\u00ad][^\S\n]*\n[^\S\n]*(?=\w)', '', text.casefold())
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
    blocks=row.get('feature',{}).get('text_blocks',[]) if result.recipe.extractor in ('ocr','document-text','handwriting','handwriting-decode') else []
    if blocks:
        # Preserve block boundaries so dehyphenation cannot join separate blocks.
        lines='\n\n'.join('\n'.join(line['text'] for line in block['lines']) for block in blocks)
    fields = {'text': lines if blocks else ' '.join(w['text'] for w in words), 'positioned_lines': lines}
    spelling=row.get('feature',{}) if result.recipe.extractor=='spelling' else None
    if spelling:fields['corrected_text']=spelling['corrected_text']
    comparison=row.get('feature',{}).get('token_comparison')
    if result.recipe.extractor=='scientific-region' and comparison:
        # Search may surface an unresolved reading, but must retain its engine and
        # review state. Agreement is a queue hint, never catalogue adoption.
        fields.update(formula_ocr_raw=comparison['tesseract']['raw'],
                      formula_latex_raw=comparison['vision']['raw'])
        for engine in ('tesseract','vision'):
            fields['formula_'+engine+'_proposals']=' '.join(
                v['normalized'] for v in comparison[engine]['alternatives'])
        if comparison.get('search_normalized'):
            fields.update(formula_normalized=comparison['search_normalized'],
                          formula_unicode=comparison['display_unicode'])
    entry = base_entry(key, 'extraction', asdict(result.page.source), mapping, fields,
                       {'receipt_key': key, 'receipt_sha256': digest, 'page': asdict(result.page),
                        'region': asdict(result.region) if result.region else None},
                       availability=availability, reason=reason)
    entry['extractor'] = result.recipe.extractor
    if spelling:
        entry['evidence']['spelling']={k:spelling[k] for k in
            ('corrected_tokens','review_items','ocr_origin','lexicon_sources','policy')}
        entry['evidence']['review_required']=True
    if result.recipe.extractor=='scientific-region' and comparison:
        entry['evidence']['formula_comparison']=comparison
        entry['evidence']['review_required']=True
    if blocks:
        entry['evidence']['text_blocks'] = [
            {'index': block['index'], 'region': block['region'], 'text': block['text']}
            for block in blocks if block['text'].strip()]
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


def catalogue_table(roots, spec):
    source = Source(**spec['source'])
    require(source.sha256, 'Catalogue snapshot needs a checksum')
    data = roots.source(source, verify_hash=True).read_bytes()
    require(hashlib.sha256(data).hexdigest() == source.sha256, 'Catalogue changed during refresh')
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig')))
    required = {spec['object_field'], *spec['fields'].values(), *spec.get('path_fields', [])}
    require(required <= set(reader.fieldnames or []), 'Unknown catalogue columns')
    rows = list(reader)
    require(all(all(isinstance(row.get(column), str) for column in required) for row in rows),
            'Malformed catalogue row')
    return source, rows


def catalogue_entries(roots, spec, mapping):
    """Explicit catalogue/object-record mappings with exact representation links."""
    require(set(spec) <= {'source', 'object_field', 'fields', 'unit', 'aliases',
                         'group_objects', 'path_fields', 'object_metadata', 'label'} and
            {'source', 'object_field', 'fields', 'unit'} <= spec.keys(), 'Explicit catalogue field mapping required')
    source, rows = catalogue_table(roots, spec)
    require(type(spec.get('group_objects', False)) is bool, 'Invalid catalogue grouping')
    extra = spec.get('object_metadata'); extra_rows = {}
    if extra:
        require(set(extra) == {'source', 'object_field', 'fields'} and
                not (set(extra['fields']) & set(spec['fields'])), 'Supplementary metadata must not overwrite catalogue fields')
        extra_source, records = catalogue_table(roots, extra)
        for number, row in enumerate(records, 2):
            key = row[extra['object_field']]
            if not key:
                continue
            require(key not in extra_rows, 'Ambiguous supplementary object metadata')
            extra_rows[key] = (number, row)
    aliases = spec.get('aliases', {})
    require(isinstance(aliases, dict) and all(isinstance(v, list) and all(isinstance(x, str) for x in v)
                                           for v in aliases.values()), 'Invalid documented catalogue aliases')
    grouped = {}
    for number, row in enumerate(rows, 2):
        object_id = row[spec['object_field']]
        require(isinstance(object_id, str), 'Catalogue row has a malformed identity field')
        fields = {name: row[column] or '' for name, column in spec['fields'].items()}
        fields['identifier'] = object_id
        fields['aliases'] = '\n'.join(aliases.get(object_id, []))
        evidence = {'catalogue': asdict(source), 'catalogue_label': spec.get('label', spec['unit']),
                    'row_number': number, 'row_sha256': sha(row),
                    'field_columns': spec['fields'], 'alias_basis': 'explicit selection, not inferred'}
        if extra:
            fields.update({field: '' for field in extra['fields']})
            if object_id in extra_rows:
                extra_number, record = extra_rows[object_id]
                fields.update({field: record[column] for field, column in extra['fields'].items()})
                evidence['object_metadata'] = {'source': asdict(extra_source), 'row_number': extra_number,
                                               'row_sha256': sha(record), 'field_columns': extra['fields']}
        paths = []
        for column in spec.get('path_fields', []):
            if not row[column]:
                continue
            relative = PurePosixPath(row[column])
            require(not relative.is_absolute() and '..' not in relative.parts and '\\' not in row[column],
                    'Catalogue representation path must stay within its unit')
            paths.append(str(PurePosixPath(source.path).parent / relative))
        grouped_key = sha([object_id, fields]) if spec.get('group_objects') and object_id else str(number)
        if grouped_key not in grouped:
            key = 'catalogue:' + sha({'unit': spec['unit'], 'snapshot': source.sha256, 'row': number})
            evidence.update(representation_paths=[], catalogue_rows=[])
            grouped[grouped_key] = base_entry(key, 'catalogue', {'unit': spec['unit'], 'object_id': object_id}, mapping, fields, evidence)
        entry = grouped[grouped_key]
        entry['evidence']['representation_paths'].extend(paths)
        entry['evidence']['catalogue_rows'].append({'number': number, 'sha256': sha(row)})
    for entry in grouped.values():
        paths = sorted(set(entry['evidence']['representation_paths']))
        entry['evidence']['representation_paths'] = paths
        if paths:
            entry['fields']['representation_paths'] = '\n'.join(paths)
        yield entry


def catalogue_lookup(value):
    """In-memory exact file links; no basename, title or fuzzy identity joins."""
    lookup = defaultdict(list); objects = defaultdict(list)
    catalogues = [e for e in value['entries'] if e['kind'] == 'catalogue']
    for entry in catalogues:
        identity = entry['source_identity']
        if identity['object_id']:
            objects[(identity['unit'], identity['object_id'])].append(entry)
    for entry in catalogues:
        source = entry['evidence']['catalogue']
        identity = entry['source_identity']
        related = objects.get((identity['unit'], identity['object_id']), [entry])
        for path in entry['evidence'].get('representation_paths', []):
            lookup[(source['root'], path)].extend(related)
    for key, entries in lookup.items():
        lookup[key] = list({e['key']: e for e in entries}.values())
    return lookup


def refresh_catalogues(roots, value, specs):
    """Replace selected catalogue projections only; fail before publishing any partial refresh."""
    from copy import deepcopy
    value = deepcopy(validate(value))
    mapping = mapping_rows(value['selection'].get('identity_bindings', []))
    additions = [entry for spec in specs for entry in catalogue_entries(roots, spec, mapping)]
    entries = [e for e in value['entries'] if e['kind'] != 'catalogue'] + additions
    require(len({e['key'] for e in entries}) == len(entries), 'Duplicate catalogue entry')
    value['selection']['catalogues'] = specs
    value.update(entries=entries, postings=entry_postings(entries),
                 catalogues_refreshed_at=datetime.now(timezone.utc).isoformat())
    value['unavailable'] = [r for r in value['unavailable'] if not r['key'].startswith('catalogue:')]
    value['coverage'].update(selected_catalogues=len(specs),
        catalogue_rows=sum(len(e['evidence']['catalogue_rows']) for e in additions),
        catalogue_records=len(additions),
        entries_by_kind=dict(Counter(e['kind'] for e in entries)),
        entries_by_availability=dict(Counter(e['availability'] for e in entries)),
        unmapped_entries=sum(e['object_uid'] is None for e in entries), failed_inputs=len(value['unavailable']))
    return seal(value)


def entry_postings(entries):
    postings = defaultdict(list)
    for number, entry in enumerate(entries):
        _add_entry_postings(postings, number, entry)
    return dict(postings)


def _add_entry_postings(postings, number, entry):
    terms = Counter(tokens(' '.join(entry['fields'].values())))
    entry['token_count'] = sum(terms.values())
    for token, frequency in terms.items():
        postings[token].append([number, frequency])


def _remap_postings(postings, positions, additions):
    # Earlier entry numbers are stable, so most posting lists can be reused as-is.
    prefix = next((n for n, number in enumerate(positions) if n != number), len(positions))
    result = {}
    for token, rows in postings.items():
        added = additions.pop(token, [])
        if rows and rows[-1][0] >= prefix:
            split = bisect_left(rows, [prefix])
            tail = [[positions[number], frequency] for number, frequency in rows[split:]
                    if positions[number] is not None]
            tail.extend(added)
            tail.sort()
            rows = rows[:split] + tail
        elif added:
            rows = rows + added
        if rows:
            result[token] = rows
    result.update(additions)
    return result


def read_finding_history(roots, source_record):
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
    return list(current.values())


def finding_entries(roots, source_record, mapping):
    yield from entries_for_current(read_finding_history(roots, source_record), source_record, mapping)


def entries_for_current(current, source_record, mapping):
    source = Source(**source_record)
    for row in current:
        case, key = row.get('case_key', 'photo'), row['key']
        if row['review'] == 'rejected' or row.get('component_assertions'):
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


def refresh_findings(value, source_record, current):
    """Validate the input and refresh one selected review history."""
    return refresh_validated_findings(validate(value), source_record, current)


def refresh_validated_findings(value, source_record, current):
    """Refresh a validated projection, sharing untouched entries and posting rows."""
    from .review_feedback import feedback_rows

    def same(source):
        return all(source.get(k) == source_record[k] for k in ('root', 'path', 'unit', 'object_id'))

    specs = list(value['selection'].get('findings', []))
    selected = [i for i, spec in enumerate(specs) if same(spec)]
    require(len(selected) == 1, 'Review history must be selected exactly once in this search index')
    specs[selected[0]] = source_record
    entries, replaced = [], {}
    positions = [None] * len(value['entries'])
    for number, entry in enumerate(value['entries']):
        if entry['kind'] == 'finding' and same(entry['evidence']['history']):
            replaced[entry['key']] = (number, entry)
        else:
            positions[number] = len(entries)
            entries.append(entry)

    additions = defaultdict(list)
    mapping = mapping_rows(value['selection'].get('identity_bindings', []))
    for entry in entries_for_current(current, source_record, mapping):
        previous = replaced.get(entry['key'])
        if previous and list(previous[1]['fields'].items()) == list(entry['fields'].items()):
            positions[previous[0]] = len(entries)
            entry['token_count'] = previous[1]['token_count']
        else:
            _add_entry_postings(additions, len(entries), entry)
        entries.append(entry)

    feedback = [r for r in value.get('review_feedback', []) if not same(r['history'])]
    unavailable = [r for r in value['unavailable'] if r['key'] != f'finding:{selected[0]}']
    return seal({**value,
                 'selection': {**value['selection'], 'findings': specs},
                 'entries': entries,
                 'postings': _remap_postings(value['postings'], positions, additions),
                 'review_feedback': feedback + feedback_rows(current, source_record),
                 'findings_refreshed_at': datetime.now(timezone.utc).isoformat(),
                 'unavailable': unavailable,
                 'coverage': {**value['coverage'],
                              'entries_by_kind': dict(Counter(e['kind'] for e in entries)),
                              'entries_by_availability': dict(Counter(e['availability'] for e in entries)),
                              'unmapped_entries': sum(e['object_uid'] is None for e in entries),
                              'failed_inputs': len(unavailable)}})


def seal(value):
    value['projection_sha256'] = sha({k: v for k, v in value.items() if k != 'projection_sha256'})
    return value


def validate(value):
    require(isinstance(value, dict) and value.get('format') == FORMAT and value.get('metadata_adopted') is False,
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
    postings = entry_postings(entries)
    coverage = {'selected_receipts': len(keys), 'indexed_receipts': sum(e['kind'] == 'extraction' for e in entries),
                'available_receipts': sum(e['kind'] == 'extraction' and e['availability'] == 'available' for e in entries),
                'selected_catalogues': len(selection.get('catalogues', [])),
                'selected_finding_histories': len(selection.get('findings', [])),
                'entries_by_kind': dict(Counter(e['kind'] for e in entries)),
                'entries_by_availability': dict(Counter(e['availability'] for e in entries)),
                'unmapped_entries': sum(e['object_uid'] is None for e in entries),
                'failed_inputs': len(unavailable), 'whole_archive': False}
    feedback = []
    from .review_feedback import feedback_rows
    for source_record in selection.get('findings', []):
        try:
            feedback.extend(feedback_rows(read_finding_history(roots, source_record), source_record))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # The same failed history is already listed in unavailable above.
    return seal({'format': FORMAT, 'metadata_adopted': False, 'review_feedback': feedback,
                 'refreshed_at': datetime.now(timezone.utc).isoformat(), 'selection': selection,
                 'entries': entries, 'postings': dict(postings), 'unavailable': unavailable,
                 'coverage': coverage, 'limitations': ['Availability is observed at refresh, not guaranteed at query time.',
                 'Selected material only. Similarity and spelling alternatives do not establish identity.',
                 'Unmapped identities remain explicit; canonical UIDs are never inferred from matching bytes.']})


_TITLES = {'dr', 'ing', 'dipl', 'prof', 'kollege', 'kollegin', 'genosse', 'genossin'}
_WORD = re.compile(r'\w+(?:[-\u00ad][^\S\n]*\n[^\S\n]*\w+)*')
_word_fold = lru_cache(maxsize=8192)(fold)


def text_spans(text):
    """Normalized words with offsets into unchanged source text."""
    return [(_word_fold(m.group()), m.start(), m.end()) for m in _WORD.finditer(text)]


def ocr_key(word):
    # Comparison only: never rewrite OCR or identifiers. Deliberately opt-in.
    return word.replace('ue', 'ii').replace('rn', 'm').translate(
        str.maketrans({'1': 'l', 'i': 'l', 'c': 'e', 'b': 'h'}))


def long_stem(word):
    if not word.isalpha() or len(word) < 7:
        return word
    for ending in ('ern', 'en', 'er', 'es', 'e', 'n', 's'):
        if word.endswith(ending) and len(word) - len(ending) >= 6:
            return word[:-len(ending)]
    return word


def query_names(text):
    """Possible adjacent name pairs; these are retrieval hints, not identities."""
    words = [(word, start, end) for word, start, end in text_spans(text) if word not in _TITLES]
    pairs = []
    for left, right in zip(words, words[1:]):
        a, start, end = left; b, other_start, other_end = right
        if not (a.isalpha() and b.isalpha()):
            continue
        if re.search(r'[^\s.,]', text[end:other_start]):
            continue
        if ',' in text[end:other_start]:
            surname, given = a, b
        else:
            given, surname = a, b
        if len(surname) > 1:
            pairs.append((given, surname))
    return pairs


def match_snippet(content, spans, limit=500):
    """Center on the densest local evidence, retaining original spelling."""
    if not spans:
        return '', []
    counts = Counter(); right = 0; best = 0; most = 0
    for left, (term, start, _) in enumerate(spans):
        while right < len(spans) and spans[right][2] - start <= limit - 80:
            counts[spans[right][0]] += 1
            right += 1
        if len(counts) > most:
            best, most = left, len(counts)
        if right > left:
            counts[term] -= 1
            if not counts[term]:
                del counts[term]
        else:
            right = left + 1
    start = max(0, spans[best][1] - 80)
    end = min(len(content), start + limit - 2)
    prefix = '…' if start else ''
    snippet = prefix + content[start:end] + ('…' if end < len(content) else '')
    highlights = [{'start': a - start + len(prefix), 'end': b - start + len(prefix)}
                  for _, a, b in spans if a >= start and b <= end]
    return snippet, highlights


def snippet_parts(snippet, highlights):
    """Plain text segments for template autoescaping, never trusted HTML."""
    parts = []; cursor = 0
    for span in sorted(highlights, key=lambda s: (s['start'], s['end'])):
        start, end = max(cursor, span['start']), span['end']
        if end <= cursor:
            continue
        if start > cursor:
            parts.append({'text': snippet[cursor:start], 'matched': False})
        parts.append({'text': snippet[start:end], 'matched': True})
        cursor = end
    if cursor < len(snippet):
        parts.append({'text': snippet[cursor:], 'matched': False})
    return parts


def query(value, text, *, limit=20, offset=0, kinds=None, unit=None, fuzzy=False,
          finding_cases=None, finding_history=None):
    """Limit findings to listed cases, optionally within just one (root, path) history."""
    return query_validated(validate(value), text, limit=limit, offset=offset, kinds=kinds,
                           unit=unit, fuzzy=fuzzy, finding_cases=finding_cases, finding_history=finding_history)


def query_validated(value, text, *, limit=20, offset=0, kinds=None, unit=None, fuzzy=False,
                    finding_cases=None, finding_history=None):
    """Query an already verified or freshly sealed projection without mutating it."""
    require(isinstance(text, str) and text.strip() and len(text) <= 300, 'Bounded search query required')
    require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0, 'Invalid page')
    terms = list(dict.fromkeys(t for t in tokens(text) if t not in _TITLES))
    require(terms, 'Search query has no searchable terms')
    postings = value['postings']; alternatives = {}; variants_by_term = {}
    weights = {'exact': 1., 'name initial': .8, 'compound/stem': .65,
               'OCR alternative': .55, 'spelling alternative': .45}
    vocabulary = sorted(postings)
    for term in terms:
        variants = {term: 'exact'} if term in postings else {}
        stem = long_stem(term)
        if term.isalpha() and len(term) >= 7:
            for word in vocabulary:
                if word.isalpha() and word != term and (word.startswith(stem) or long_stem(word) == stem):
                    variants[word] = 'compound/stem'
        if fuzzy and term.replace('1', 'l').isalpha() and len(term) >= 4:
            import difflib
            for word in vocabulary:
                if word != term and ocr_key(word) == ocr_key(term):
                    variants.setdefault(word, 'OCR alternative')
            for word in difflib.get_close_matches(term, vocabulary, n=4, cutoff=.82):
                if word != term and word.isalpha():
                    variants.setdefault(word, 'spelling alternative')
        alternatives_for_term = [word for word, method in variants.items() if method.endswith('alternative')]
        if alternatives_for_term:
            alternatives[term] = alternatives_for_term
        variants_by_term[term] = variants
    names = query_names(text)
    # The surname retrieves candidates; initials are checked locally beside it.
    selected = {number for variants in variants_by_term.values() for word in variants
                for number, _ in postings[word]}
    required = 1 if len(terms) == 1 else len(terms) // 2 + 1
    average = max(1., sum(e['token_count'] for e in value['entries']) / max(1, len(value['entries'])))
    frequencies = {word: dict(postings[word]) for variants in variants_by_term.values() for word in variants}
    hits = []
    for number in selected:
        entry = value['entries'][number]
        if kinds and entry['kind'] not in kinds or unit and entry['source_identity']['unit'] != unit:
            continue
        if finding_cases is not None and entry['kind'] == 'finding':
            evidence = entry['evidence']
            history = evidence['history']
            if ((finding_history is None or (history['root'], history['path']) == finding_history)
                    and evidence['case_key'] not in finding_cases):
                continue
        exact = text.casefold() in {entry.get('object_uid', '').casefold() if entry.get('object_uid') else '',
                                  entry['fields'].get('identifier', '').casefold()}
        if valid_uid(text.strip()) and not exact:
            continue
        local = {term: {word: method for word, method in variants.items() if number in frequencies[word]}
                 for term, variants in variants_by_term.items()}
        possible = {term for term in terms if local[term]}
        possible.update(given for given, surname in names if surname in local and local[surname])
        if not exact and len(possible) < required:
            continue
        field_spans = {field: text_spans(content) for field, content in entry['fields'].items()}
        initial_locations = {}
        for given, surname in names:
            if given not in local or surname not in local or not local[surname]:
                continue
            for field, spans in field_spans.items():
                for i, (word, start, end) in enumerate(spans):
                    if word not in local[surname]:
                        continue
                    for j in (i - 1, i + 1):
                        if not 0 <= j < len(spans):
                            continue
                        other, a, b = spans[j]
                        between = entry['fields'][field][min(end, b):max(start, a)]
                        if '\n' in between or re.search(r'[^\s.,]', between):
                            continue
                        if other.isalpha() and other[0] == given[0] and min(len(other), len(given)) == 1:
                            local[given][other] = 'name initial'
                            initial_locations[(field, a, b)] = given
        matched = [term for term in terms if local[term]]
        missing = [term for term in terms if not local[term]]
        # Never relax a type designation or other mixed letter/number identifier.
        if not exact and (len(matched) < required or any(any(c.isdigit() for c in t) and
                            any(c.isalpha() for c in t) for t in missing)):
            continue
        details = []; evidence_spans = {}; quality = 0.; relevance = 0.
        for term in matched:
            methods = local[term]
            quality += max(weights[method] for method in methods.values())
            for word, method in methods.items():
                frequency = frequencies.get(word, {}).get(number, 1)
                df = len(postings.get(word, []))
                relevance += weights[method] * math.log(1 + (len(value['entries']) - df + .5)/(df + .5)) * (
                    frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * entry['token_count']/average)))
                for field, spans in field_spans.items():
                    for found, start, end in spans:
                        if found != word or (method == 'name initial' and initial_locations.get((field, start, end)) != term):
                            continue
                        evidence_spans.setdefault(field, []).append((term, start, end))
                        creator_position = fold(field) == 'creator'
                        role = creator_position or fold(field) in {'author', 'authors', 'autor', 'autoren', 'bearbeiter'}
                        preceding = entry['fields'][field][max(0, start - 60):start]
                        role = role or bool(re.search(r'\b(?:autor(?:en)?|author|bearbeiter)\s*:\s*[^\n;]{0,45}$', fold(preceding)))
                        details.append({'term': term, 'word': entry['fields'][field][start:end],
                                        'method': method, 'field': field, 'start': start, 'end': end,
                                        'author_position': role, 'creator_position': creator_position})
        author = any(d['author_position'] for d in details)
        # Keep every tie-break below the preceding tier, even for long queries.
        score = ((100000. if exact else 0.) + 100 * len(matched) + 10 * quality / len(terms)
                 + (.001 if author else 0) + .0001 * relevance / (1 + relevance))
        reasons = ['exact identifier'] if exact else []
        reasons += [f'{len(matched)}/{len(terms)} query terms matched']
        reasons += sorted({'text match' if d['method'] == 'exact' else d['method'] for d in details})
        if any(d['author_position'] and not d['creator_position'] for d in details):
            reasons.append('author/Bearbeiter position')
        if any(d['creator_position'] for d in details):
            reasons.append('credited creator')
        if any(d['method'] == 'name initial' for d in details):
            reasons.append('initial is ambiguous; verify name and shared context')
        if missing:
            reasons.append('missing: ' + ', '.join(missing))
        matched_fields = list(evidence_spans)
        best_field = max(matched_fields, key=lambda f: (len({t for t, _, _ in evidence_spans[f]}),
                          any(d['author_position'] for d in details if d['field'] == f), f == 'text')) if matched_fields else None
        snippet, highlights = match_snippet(entry['fields'][best_field], sorted(set(evidence_spans[best_field]), key=lambda s: s[1])) if best_field else ('', [])
        hits.append({'target': entry['key'], 'object_uid': entry['object_uid'], 'kind': entry['kind'],
                     'title': entry['fields'].get('title', ''),
                     'inventory': entry['fields'].get('inventory', ''),
                     'catalogue_fields': entry['fields'] if entry['kind'] == 'catalogue' else {},
                     'identity_status': entry['identity_status'], 'score': score,
                     'match_reasons': reasons, 'matched_fields': matched_fields,
                     'snippet': snippet, 'snippet_field': best_field, 'snippet_highlights': highlights,
                     'snippet_parts': snippet_parts(snippet, highlights),
                     'matched_terms': matched, 'missing_terms': missing, 'term_matches': details,
                     'evidence': entry['evidence'], 'availability': entry['availability'],
                     'review': entry.get('review'), 'identity_confirmed': False})
    hits.sort(key=lambda h: (-h['score'], h['target']))
    return {'query': text, 'hits': hits[offset:offset+limit], 'total_matches': len(hits),
            'offset': offset, 'limit': limit, 'next_offset': offset+limit if offset+limit < len(hits) else None,
            'spelling_alternatives': alternatives, 'coverage': value['coverage'],
            'refreshed_at': value['refreshed_at'], 'projection_sha256': value['projection_sha256']}


def visual_query(value, key, *, limit=20, offset=0, show_rejected=False):
    validate(value)
    require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0, 'Invalid page')
    query_entry = next((e for e in value['entries'] if e['key'] == key), None)
    require(query_entry and query_entry['vector'] is not None, 'Visual query unavailable')
    hits, incompatible = [], []
    from .review_feedback import entry_locator, reviewed_candidates
    for entry in value['entries']:
        if entry['key'] == key or entry['vector'] is None:
            continue
        if entry['space'] != query_entry['space']:
            incompatible.append(entry['key']); continue
        score = max(-1., min(1., sum(a*b for a, b in zip(entry['vector'], query_entry['vector']))))
        hits.append({'target': entry['key'], 'object_uid': entry['object_uid'], 'similarity': score,
                     'evidence': entry['evidence'], 'availability': entry['availability'],
                     'match_reasons': ['visual similarity'], 'identity_confirmed': False,
                     'locator': entry_locator(entry)})
    hits.sort(key=lambda h: (-h['similarity'], h['target']))
    hits, hidden = reviewed_candidates(entry_locator(query_entry), hits, value.get('review_feedback', []),
                                       show_rejected=show_rejected)
    return {'hits': hits[offset:offset+limit], 'eligible': len(hits), 'incompatible_space': incompatible,
            'hidden_rejected': hidden,
            'same_object_excluded': False, 'offset': offset, 'limit': limit,
            'coverage': value['coverage'], 'refreshed_at': value['refreshed_at']}
