"""Documentary candidates and reviewer-authored attributions in existing records."""
from copy import deepcopy
from datetime import datetime, timezone
import re
import unicodedata

from archive_research.common import require


RELATION = 'person_attribution'
CONTEXT_FIELDS = ('event', 'date', 'department', 'attendance', 'catalogue_neighbours')


def _text(value, limit, label, optional=False):
    require(isinstance(value, str) and len(value) <= limit and
            (optional or value.strip()), f'Invalid {label}')


def _citations(keys, allowed, label):
    require(isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)
            and len(set(keys)) == len(keys) and set(keys) <= allowed, f'Invalid {label}')


def validate_candidates(case):
    people = case.get('documentary_people', [])
    require(isinstance(people, list) and len(people) <= 40, 'Select at most 40 documentary candidates')
    allowed = {v['key'] for v in case['views']}
    require(not case.get('documentary_target_view') or case['documentary_target_view'] in allowed,
            'Unknown default attribution photograph')
    if 'documentary_targets' in case:
        targets = case['documentary_targets']
        require(isinstance(targets, list) and targets and all(isinstance(k, str) for k in targets)
                and len(set(targets)) == len(targets) and set(targets) <= allowed,
                'Select distinct known photographs for navigation')
    seen = set()
    for person in people:
        require(isinstance(person, dict) and set(person) == {
            'key', 'name', 'reason', 'context', 'citation_keys', 'portraits', 'uncertainty'},
            'Documentary candidate fields required')
        key = person['key']
        require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key)
                and key not in seen, 'Unique local candidate keys required')
        seen.add(key)
        for field, limit in [('name', 200), ('reason', 1500), ('uncertainty', 1000)]:
            _text(person[field], limit, f'candidate {field}')
        context = person['context']
        require(isinstance(context, dict) and set(context) <= set(CONTEXT_FIELDS), 'Invalid documentary context')
        for value in context.values():
            _text(value, 500, 'context', optional=True)
        _citations(person['citation_keys'], allowed, 'documentary citations')
        require(isinstance(person['portraits'], list) and len(person['portraits']) <= 4,
                'Select at most four documented portraits per candidate')
        for portrait in person['portraits']:
            require(isinstance(portrait, dict) and set(portrait) == {'view_key', 'caption', 'citation_keys'}
                    and portrait['view_key'] in allowed, 'Invalid documented portrait')
            _text(portrait['caption'], 1500, 'portrait attribution')
            _citations(portrait['citation_keys'], allowed, 'portrait attribution citations')
    validate_overview(case)


def validate_overview(case):
    """Optional document/event view; photo counts are observations, not identities."""
    overview = case.get('documentary_overview')
    if overview is None:
        return
    require(isinstance(overview, dict) and set(overview) == {
        'event', 'date', 'photographs', 'documents', 'mentions'}, 'Invalid documentary overview')
    _text(overview['event'], 500, 'event')
    _text(overview['date'], 100, 'event date')
    allowed = {v['key'] for v in case['views']}
    for field in ('photographs', 'documents'):
        rows = overview[field]
        require(isinstance(rows, list) and 1 <= len(rows) <= 4 and
                len({r['view_key'] for r in rows}) == len(rows), 'Distinct bounded overview views required')
        for row in rows:
            require(row['view_key'] in allowed, 'Unknown overview source')
            if field == 'photographs':
                require(set(row) == {'view_key', 'people_count', 'count_source'} and
                        type(row['people_count']) is int and 0 <= row['people_count'] <= 500,
                        'A photograph count needs its source')
                _text(row['count_source'], 500, 'count source')
            else:
                require(set(row) == {'view_key', 'label', 'note', 'additional_mentions'}, 'Invalid document description')
                for name in ('label', 'note', 'additional_mentions'):
                    _text(row[name], 1500, name, optional=name == 'additional_mentions')
    mentions = overview['mentions']
    people = {p['key'] for p in case.get('documentary_people', [])}
    documents = {r['view_key'] for r in overview['documents']}
    require(isinstance(mentions, list) and 1 <= len(mentions) <= 40, 'Bounded documentary mentions required')
    require(len({r['candidate_key'] for r in mentions}) == len(mentions) and
            len({r['assertion_key'] for r in mentions}) == len(mentions), 'Distinct documentary findings required')
    for row in mentions:
        require(set(row) == {'candidate_key', 'assertion_key', 'sources'} and
                row['candidate_key'] in people and isinstance(row['assertion_key'], str), 'Invalid documentary mention')
        require(isinstance(row['sources'], list) and 1 <= len(row['sources']) <= len(documents) and
                len({s['view_key'] for s in row['sources']}) == len(row['sources']), 'Distinct mention sources required')
        for source in row['sources']:
            require(set(source) == {'view_key', 'wording', 'qualification'} and source['view_key'] in documents,
                    'Unknown documentary mention source')
            _text(source['wording'], 300, 'source wording')
            _text(source['qualification'], 700, 'mention qualification', optional=True)


def overview_context(case, current, revision):
    from .records import check_binding
    from archive_research.common import ReviewError
    overview = case['documentary_overview']
    views = {}
    for original in case['views']:
        view = {**original, 'available': False}
        try:
            require(not view.get('unavailable_reason'), view.get('unavailable_reason', 'Source unavailable'))
            check_binding(view['source'])
            check_binding(view['preview'])
            view['available'] = True
        except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
            view['error'] = str(error)
        views[view['key']] = view
    people = {p['key']: p for p in case['documentary_people']}
    records = {r['key']: r for r in current if r.get('case_key') == case['key']}
    rows = []
    for mention in overview['mentions']:
        sources = {s['view_key']: s for s in mention['sources']}
        rows.append({'person': people[mention['candidate_key']], 'record': records[mention['assertion_key']],
                     'sources': [sources.get(d['view_key']) for d in overview['documents']]})
    return {'case': case, 'revision': revision, 'overview': overview, 'mentions': rows,
            'photographs': [{**p, 'view': views[p['view_key']]} for p in overview['photographs']],
            'documents': [{**d, 'view': views[d['view_key']]} for d in overview['documents']]}


def candidates(case, query=''):
    """Filter prepared documentary text only; no image features or identity ranking."""
    def tokens(text):
        normalized = unicodedata.normalize('NFKD', text.casefold())
        return re.findall(r'\w+', ''.join(c for c in normalized if not unicodedata.combining(c)))
    wanted = set(tokens(query[:200]))
    result = []
    for person in case.get('documentary_people', []):
        fields = {'name': person['name'], 'documentary_reason': person['reason'], **person['context']}
        found = {key: set(tokens(text)) for key, text in fields.items()}
        if wanted <= set().union(*found.values()):
            result.append({**person, 'matched_fields': [key.replace('_', ' ') for key, words in found.items()
                                                     if wanted & words]})
    return sorted(result, key=lambda p: (p['name'].casefold(), p['key']))


def validate_attribution(record, case):
    if record['relation'] != RELATION:
        require('person_attribution' not in record, 'Person attribution requires its own review operation')
        return
    detail = record.get('person_attribution')
    require(isinstance(detail, dict) and set(detail) == {
        'target_view_key', 'position', 'candidate', 'evidence_views', 'reviewer_citation_keys', 'method'}, 'Invalid person attribution')
    require(detail['method'] == 'reviewer_documentary_attribution' and
            record.get('change_kind') == RELATION and record.get('reviewer') and record.get('reviewed_at'),
            'An attribution must be authored by a reviewer')
    require(record['review'] in ('accepted', 'rejected', 'unresolved'), 'Choose an attribution outcome')
    require(detail['target_view_key'] in record['citation_keys'], 'Cite the target photograph')
    _citations(detail['reviewer_citation_keys'], set(record['citation_keys']), 'reviewer-selected sources')
    _text(detail['position'], 200, 'position in photograph')
    _text(record['reason'], 3000, 'reviewer attribution reason')
    views = detail['evidence_views']
    require(isinstance(views, list) and {v['key'] for v in views} == set(record['citation_keys']),
            'Retain the exact cited view descriptions and bindings')
    allowed = {v['key'] for v in case['views']}
    person = detail['candidate']
    if person is None:
        require(record['review'] == 'unresolved', 'A named candidate is required for an attribution or rejection')
    else:
        validate_candidates({'views': case['views'], 'documentary_people': [person]})
        require(set(person['citation_keys']) <= set(record['citation_keys']), 'Retain the documentary basis')
    require(detail['target_view_key'] in allowed, 'Unknown target photograph')


def save_attribution(case_key, submitted, actor):
    """Append to the existing readable review history, with existing lock/recovery."""
    from .records import (_save_lock, _record_bytes, _checked_cases, check_sources, assertion_case,
                          case_synthetic, validate_records, _append_record)
    with _save_lock():
        authority = _record_bytes()
        evidence, _, current, revision = validate_records(*authority)
        case = next((c for c in evidence['cases'] if c['key'] == case_key), None)
        require(case is not None and case.get('documentary_people'), 'No documentary candidates prepared for this case')
        require(not case.get('read_only', False), 'This case is read-only')
        require(submitted.get('revision') == revision, 'Records changed. Nothing was saved; compare your draft with the current record.')
        _checked_cases(revision, evidence)
        check_sources(evidence, case_key)
        _text(actor, 200, 'authenticated reviewer')
        key = submitted.get('attribution_key', '')
        owned = [r for r in current if assertion_case(r) == case_key and r['relation'] == RELATION]
        previous = next((r for r in owned if r['key'] == key), None)
        require(not key or previous is not None, 'Unknown attribution to correct')
        if not key:
            used = {r['key'] for r in current if assertion_case(r) == case_key}
            index = 1
            while f'person-attribution-{index}' in used:
                index += 1
            key = f'person-attribution-{index}'
        view_key = submitted.get('target_view_key')
        views = {v['key']: v for v in case['views']}
        require(view_key in views, 'Choose a target photograph')
        candidate_key = submitted.get('candidate_key', '')
        person = next((p for p in case['documentary_people'] if p['key'] == candidate_key), None)
        require(not candidate_key or person is not None, 'Choose a prepared documentary candidate')
        outcome = submitted.get('review')
        require(outcome in ('accepted', 'rejected', 'unresolved'), 'Choose an attribution outcome')
        require(person is not None or outcome == 'unresolved', 'Leave an unnamed person unresolved')
        position, reason, uncertainty = (submitted.get(k, '') for k in ('position', 'reason', 'uncertainty'))
        for value, limit, label in [(position, 200, 'position'), (reason, 3000, 'attribution reason'),
                                     (uncertainty, 1000, 'uncertainty')]:
            _text(value, limit, label)
        citations = submitted.get('citation_keys')
        _citations(citations, set(views), 'supporting citations')
        # Preserve documentary retrieval evidence separately from the reviewer's reasoning.
        reviewer_citations = list(citations)
        portrait_context = [key for portrait in person['portraits']
                            for key in [portrait['view_key'], *portrait['citation_keys']]] if person else []
        citations = list(dict.fromkeys([view_key] + (person['citation_keys'] if person else []) +
                                       portrait_context + reviewer_citations))
        detail = {'target_view_key': view_key, 'position': position.strip(),
                  'candidate': deepcopy(person), 'evidence_views': [deepcopy(views[k]) for k in citations],
                  'reviewer_citation_keys': reviewer_citations,
                  'method': 'reviewer_documentary_attribution'}
        record = {'case_key': case_key, 'key': key, 'subject': case_key, 'relation': RELATION,
                  'value': f"{person['name'] if person else 'Unnamed person'} — {position.strip()}",
                  'scope': 'Reviewer attribution in one specified photograph. Documentary candidacy alone does not establish depiction.',
                  'uncertainty': uncertainty.strip(), 'reason': reason.strip(), 'review': outcome,
                  'citation_keys': citations, 'person_attribution': detail, 'change_kind': RELATION,
                  'revision': previous['revision'] + 1 if previous else 0,
                  'supersedes_revision': previous['revision'] if previous else None,
                  'reviewer': actor, 'reviewed_at': datetime.now(timezone.utc).isoformat(),
                  'based_on_revision': revision, 'synthetic': case_synthetic(case, evidence), 'metadata_adopted': False}
        return _append_record(authority, record)
