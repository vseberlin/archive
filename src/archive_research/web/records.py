"""Bounded research records and projections; archive sources remain read-only."""
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from archive_research.common import ReviewError, atomic_write, checked_member, content_revision, file_lock, read_bytes, require
from .models import ResearchCase
from .people import RELATION as PERSON_ATTRIBUTION, validate_candidates, validate_attribution
from archive_research.review_feedback import view_locator, validate_pair

TEXT_LIMITS = {'value': 500, 'scope': 1000, 'uncertainty': 1000, 'reason': 3000}
REVIEW_STATES = ('accepted', 'rejected', 'unresolved')
ASSERTION_KEYS = {'photo-type', 'alternative-type', 'type-patent'}
PHYSICAL_LIMITS = {'inventory_reference': 250, 'catalogue_url': 1000, 'location': 500,
                   'location_source': 1000, 'location_date': 10, 'relationship': 40}
PHYSICAL_RELATIONSHIPS = (
    ('', 'Not established'), ('same_type', 'Same type'),
    ('possible_specimen', 'Possibly the depicted specimen'),
    ('confirmed_specimen', 'Confirmed same specimen'),
    ('other_connection', 'Other connection described in the source note'),
)


def validate_physical_object(value):
    require(isinstance(value, dict) and set(value) <= set(PHYSICAL_LIMITS), 'Invalid physical-object fields')
    for key, text in value.items():
        require(isinstance(text, str) and len(text) <= PHYSICAL_LIMITS[key], f'Invalid physical-object {key}')
    require(value.get('relationship', '') in dict(PHYSICAL_RELATIONSHIPS), 'Invalid object relationship')
    if value.get('location_date'):
        try:
            require(date.fromisoformat(value['location_date']).isoformat() == value['location_date'], 'Use YYYY-MM-DD')
        except ValueError as error:
            raise ReviewError('Invalid location date; use YYYY-MM-DD') from error
    if value.get('catalogue_url'):
        try:
            URLValidator(schemes=['http', 'https'])(value['catalogue_url'])
        except ValidationError as error:
            raise ReviewError('Catalogue link must be an HTTP or HTTPS URL') from error
    return value


def physical_assessment(records, case_key):
    matches = [r for r in records if assertion_case(r) == case_key and r['relation'] == 'research_assessment']
    require(len(matches) == 1, 'One case assessment is required for the optional physical-object reference')
    return matches[0]


class StaleProjection(ReviewError):
    def __init__(self, revision, message='Browsing is behind the readable records. Rebuild browsing before continuing.'):
        super().__init__(message)
        self.revision = revision


class SaveOutcomeUnknown(ReviewError):
    """The readable history could not confirm whether the submission was saved."""


def sample_path(relative, directory='sample'):
    root = settings.PILOT_ROOT / directory
    require(isinstance(relative, str) and not Path(relative).is_absolute(), 'Relative source path required')
    path = root / relative
    require('..' not in Path(relative).parts and path.resolve().is_relative_to(root)
            and path.resolve() == path, 'Source path escapes or redirects the root')
    return path


def binding_path(binding):
    name = binding.get('root', 'sample')
    if name in ('sample', 'cache'):
        return sample_path(binding['path'], name)
    require(settings.PROJECT.get('mode') == 'local_review', 'External roots require local review mode')
    roots = settings.MACHINE.get('source_roots', {})
    require(name in roots and name in ('archive', 'elements'), 'Undeclared read-only source root')
    root = Path(roots[name])
    expected = {'archive': Path('/mnt/volume/iss-archive'), 'elements': Path('/media/vladimir/Elements')}
    require(root == expected[name], 'Unapproved source root mapping')
    relative = Path(binding['path'])
    require(not relative.is_absolute() and '..' not in relative.parts, 'Relative source path required')
    return checked_member({**binding, 'path': str(root / relative)}, [root])


def check_binding(binding):
    path = binding_path(binding)
    with path.open('rb') as handle:
        st = os.fstat(handle.fileno())
    require((st.st_size, st.st_mtime_ns) == (binding['size_bytes'], binding['mtime_ns']),
            'Source changed; comparison unavailable')
    return path


def bound_bytes(binding):
    path = check_binding(binding)
    with path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        require((before.st_size, before.st_mtime_ns) == (binding['size_bytes'], binding['mtime_ns']),
                'Source changed; comparison unavailable')
        data = handle.read()
        after = os.fstat(handle.fileno())
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and len(data) == before.st_size, 'Source changed during read')
    return data


def assertion_case(record):
    return record.get('case_key', 'photo')


def case_synthetic(case, evidence):
    return case.get('synthetic', evidence.get('synthetic', True))


def snapshot():
    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        return _snapshot()


def _snapshot():
    """Caller holds the research lock through any dependent write/projection."""
    # Compatible shared lock/read contract: token and displayed values come from
    # the same captured bytes. No old workbench files or locks are opened.
    return validate_records(*_record_bytes())


def _record_bytes():
    root = settings.PILOT_ROOT / 'records'
    return read_bytes(root / 'evidence.json'), read_bytes(root / 'assertions.jsonl')


def validate_records(evidence_bytes, assertion_bytes):
    """Validate prepared additions before replacing either authority file."""
    evidence = json.loads(evidence_bytes)
    assertions = [json.loads(line) for line in assertion_bytes.splitlines() if line.strip()]
    require(evidence.get('metadata_adopted') is False, 'Adopted evidence is not supported')
    require(evidence.get('synthetic') is True or settings.PROJECT.get('mode') == 'local_review',
            'Real evidence requires explicit local review configuration')
    cases = {case['key']: case for case in evidence['cases']}
    require(cases and len(cases) == len(evidence['cases']), 'Unique case keys required')
    for case in cases.values():
        synthetic = case_synthetic(case, evidence)
        require(isinstance(synthetic, bool), 'Explicit evidence type required')
        if not synthetic:
            permission = case.get('intended_use', {})
            require(permission.get('scope') == 'private_local_research' and permission.get('authority')
                    and permission.get('external_redistribution') is False,
                    'Recorded local-use authority and redistribution boundary required')
        require(isinstance(case.get('read_only', False), bool), 'Invalid read-only flag')
        if case.get('superseded_by'):
            require(case['read_only'] and case['superseded_by'] in cases and
                    case['superseded_by'] != case['key'] and
                    not cases[case['superseded_by']].get('superseded_by'),
                    'Retired case needs a current research case; history remains read-only')
        require(type(case.get('research_priority', 0)) is int and
                0 <= case.get('research_priority', 0) <= 100, 'Invalid research priority')
        views = case['views']
        require(1 <= len(views) <= 12 and len({v['key'] for v in views}) == len(views), 'Unique bounded source views required')
        validate_candidates(case)
        pairs = case.get('reproduction_pairs', {})
        require(isinstance(pairs, dict) and len(pairs) <= 20, 'Bounded reproduction comparisons required')
        for key, pair in pairs.items():
            require(isinstance(key, str) and isinstance(pair, dict) and
                    set(pair) == {'query_view_key', 'candidate_view_key'} and
                    set(pair.values()) <= {v['key'] for v in views} and
                    pair['query_view_key'] != pair['candidate_view_key'], 'Unknown reproduction comparison views')
        for view in views:
            require(view['rotation'] in (0, 90, 180, 270), 'Unsupported rotation')
            require(isinstance(view['page_sequence'], int) and 1 <= view['page_sequence'] <= view['page_count'],
                    'Citation page is outside its representation')
            region = view['region_xywh']
            require(len(region) == 4 and all(0 <= n <= 1 for n in region) and region[2] > 0 and region[3] > 0
                    and region[0] + region[2] <= 1 and region[1] + region[3] <= 1, 'Invalid citation region')
            require(isinstance(view.get('preview_crop', False), bool), 'Invalid preview crop flag')
            for field in ('source', 'preview'):
                binding = view.get(field)
                if binding is None:
                    require(view.get('unavailable_reason'), 'Missing binding needs an explicit unavailable reason')
                    continue
                require(binding.get('root', 'sample') in ('sample', 'cache', 'archive', 'elements'), 'Unsupported source root')
                relative = Path(binding['path'])
                require(not relative.is_absolute() and '..' not in relative.parts, 'Relative source path required')
                if field == 'preview':
                    require(binding['path'].endswith('.png'), 'Only prepared PNG previews may be served')
            external = view.get('external_reference')
            if external is not None:
                require(isinstance(external, dict) and external.get('status') == 'unavailable'
                        and isinstance(external.get('note'), str) and 0 < len(external['note']) <= 1000,
                        'Only an explicit unavailable external-reference note is supported')
    current = {}
    for record in assertions:
        owner = assertion_case(record)
        require(owner in cases, 'Unknown assertion case')
        case = cases[owner]
        require(record.get('synthetic') is case_synthetic(case, evidence)
                and record.get('metadata_adopted') is False, 'Evidence kind mismatch or adopted assertion')
        validate_physical_object(record.get('physical_object', {}))
        if record.get('change_kind') == 'physical_object':
            require(record.get('recorded_by') and record.get('recorded_at') and
                    isinstance(record.get('physical_object_reason'), str) and
                    0 < len(record['physical_object_reason']) <= TEXT_LIMITS['reason'],
                    'Physical-object correction needs its author, date and reason')
        view_keys = {v['key'] for v in case['views']}
        require(isinstance(record['citation_keys'], list) and record['citation_keys']
                and len(set(record['citation_keys'])) == len(record['citation_keys'])
                and set(record['citation_keys']) <= view_keys, 'Unknown or duplicate citation')
        for field, limit in TEXT_LIMITS.items():
            require(isinstance(record[field], str) and (field == 'reason' or record[field].strip())
                    and len(record[field]) <= limit, f'Invalid assertion {field}')
        validate_attribution(record, case)
        if 'reproduction_pair' in record:
            require(record['relation'] == 'reproduced_in', 'Comparison feedback requires a reproduction finding')
            validate_pair(record['reproduction_pair'])
        require(record['review'] in {'unreviewed', 'accepted', 'rejected', 'unresolved'}, 'Invalid review')
        identity = (owner, record['key'])
        previous = current.get(identity)
        if previous:
            require(all(record[field] == previous[field] for field in ('subject', 'relation')), 'Assertion subject/relation changed')
        require(record['revision'] == (previous['revision'] + 1 if previous else 0), 'Broken revision sequence')
        require(record['supersedes_revision'] == (previous['revision'] if previous else None), 'Broken correction linkage')
        current[identity] = record
    require({owner for owner, key in current} == set(cases), 'Every case needs its prepared review record')
    for owner, case in cases.items():
        finding_views = case.get('finding_views', {})
        require(isinstance(finding_views, dict) and len(finding_views) <= 40,
                'Bounded finding views required')
        by_view = {v['key']: v for v in case['views']}
        for key, focus in finding_views.items():
            require((owner, key) in current and isinstance(focus, dict) and
                    set(focus) - {'evidence_pages'} == {'question', 'source_view', 'evidence_view', 'source_context', 'evidence_context'} and
                    isinstance(focus['question'], str) and 0 < len(focus['question'].strip()) <= 400,
                    'Finding views need an existing finding and a question')
            require(all(isinstance(focus[side + '_view'], str) and
                        focus[side + '_view'] in by_view for side in ('source', 'evidence')),
                    'Unknown finding inspection views')
            if key in case.get('reproduction_pairs', {}):
                pair = case['reproduction_pairs'][key]
                for side, field in [('source', 'query_view_key'), ('evidence', 'candidate_view_key')]:
                    selected, original = by_view[focus[side + '_view']], by_view[pair[field]]
                    require(selected['source'] == original['source'] and
                            selected['page_sequence'] == original['page_sequence'],
                            'Fixed photograph comparison must preserve the saved pair')
            cited = [by_view[k] for k in current[(owner, key)]['citation_keys']]
            for side in ('source', 'evidence'):
                anchor = focus[side + '_view']; context = focus[side + '_context']
                require(isinstance(anchor, str) and anchor in by_view and isinstance(context, list) and
                        all(isinstance(k, str) for k in context) and len(context) <= 1 and
                        len(set(context)) == len(context) and anchor not in context and
                        set(context) <= set(by_view), 'Unknown finding inspection views')
                selected = by_view[anchor]
                def same_page(other):
                    return other['source'] == selected['source'] and other['page_sequence'] == selected['page_sequence']
                require(any(same_page(v) for v in cited), 'Finding view must use a cited source page')
                require(all(same_page(by_view[k]) for k in context),
                        'Inspection must stay on the fixed source page')
                require(all(by_view[k]['region_xywh'] == [0, 0, 1, 1] and
                            not by_view[k].get('preview_crop', False) for k in context),
                        'Full-page inspection needs a full-page view')
            pages = focus.get('evidence_pages', [])
            require(isinstance(pages, list) and len(pages) <= 8 and
                    len(pages) == len(set(pages)) and set(pages) <= set(by_view) and
                    focus['evidence_view'] not in pages, 'Invalid question-bound report pages')
            selected = by_view[focus['evidence_view']]
            require(not pages or (selected.get('source') and selected['source']['path'].lower().endswith('.pdf') and
                    not focus['evidence_context'] and all(
                        by_view[k]['source'] == selected['source'] and
                        any(v['source'] == by_view[k]['source'] and
                            v['page_sequence'] == by_view[k]['page_sequence'] for v in cited)
                        for k in pages)), 'Report navigation must stay on cited pages of the assigned report')
        for record in [r for (c, _), r in current.items() if c == owner and 'component_assertions' in r]:
            children = record['component_assertions']
            pairs = case.get('reproduction_pairs', {})
            require(record['relation'] == 'reproduced_in' and isinstance(children, list) and
                    2 <= len(children) <= 20 and len(set(children)) == len(children) and
                    record['key'] not in children and set(children) <= set(pairs),
                    'A combined reproduction finding must reference distinct comparison pairs')
            require(len({pairs[k]['query_view_key'] for k in children}) == 1,
                    'Comparison components must share a fixed source')
            require(all((owner, k) in current and not current[(owner, k)].get('component_assertions')
                        for k in children), 'Comparison components must be individual findings')
        for mention in case.get('documentary_overview', {}).get('mentions', []):
            require((owner, mention['assertion_key']) in current and
                    current[(owner, mention['assertion_key'])]['relation'] == 'names_event_participant',
                    'Every documentary mention needs its own finding')
            require({s['view_key'] for s in mention['sources']} <=
                    set(current[(owner, mention['assertion_key'])]['citation_keys']),
                    'The finding must cite every displayed name source')
        for key in case.get('reproduction_pairs', {}):
            require((owner, key) in current and current[(owner, key)]['relation'] == 'reproduced_in',
                    'Every comparison needs a reproduction finding')
    if 'photo' in cases and case_synthetic(cases['photo'], evidence):
        require({key for owner, key in current if owner == 'photo'} == ASSERTION_KEYS
                and len([r for r in assertions if assertion_case(r) == 'photo']) >= 7,
                'Complete initial synthetic history required')
    return evidence, assertions, list(current.values()), content_revision(evidence_bytes, assertion_bytes)


def rebuild(expected_revision=None):
    from .feedback import refresh_search

    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        captured = _snapshot()
        if expected_revision is not None:
            require(expected_revision == captured[3], 'Records changed. Reload before rebuilding.')
        revision = _project(captured)
    refresh_search()
    return revision


@dataclass(frozen=True)
class _ReviewProjection:
    captured: tuple
    case_key: str
    base_revision: str


def _project(source):
    review = source if isinstance(source, _ReviewProjection) else None
    captured = review.captured if review else source
    evidence, history, current, revision = captured
    cases = evidence['cases']
    with transaction.atomic():
        for case in cases:
            if review and case['key'] != review.case_key:
                continue
            payload = {**case, 'history': [r for r in history if assertion_case(r) == case['key']],
                       'assertions': [r for r in current if assertion_case(r) == case['key']],
                       'lineage': case.get('lineage', evidence.get('lineage', '')),
                       'synthetic': case_synthetic(case, evidence), 'metadata_adopted': False}
            assessment = next((r for r in payload['assertions'] if r['relation'] == 'research_assessment'), {})
            payload['physical_object'] = assessment.get('physical_object', {})
            payload['physical_object_relationship'] = dict(PHYSICAL_RELATIONSHIPS).get(payload['physical_object'].get('relationship', ''), '')
            values = {'title': case['title'], 'revision': revision, 'payload': payload}
            if review:
                updated = ResearchCase.objects.filter(key=case['key'], revision=review.base_revision).update(**values)
                require(updated == 1, 'Browsing changed during review projection.')
            else:
                ResearchCase.objects.update_or_create(key=case['key'], defaults=values)
        if review:
            others = [c['key'] for c in cases if c['key'] != review.case_key]
            stamped = ResearchCase.objects.filter(key__in=others, revision=review.base_revision).update(revision=revision)
            require(stamped == len(others), 'Browsing changed during review projection.')
        else:
            ResearchCase.objects.exclude(key__in=[c['key'] for c in cases]).delete()
    return revision


def checked_cases(expected_revision=None):
    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        captured = _snapshot()
        revision = captured[3]
        if expected_revision is not None:
            require(expected_revision == revision,
                    'This citation belongs to an earlier review snapshot. Reload the comparison before continuing.')
        return _checked_cases(revision, captured[0])


def _checked_cases(revision, evidence):
    cases = list(ResearchCase.objects.order_by('key'))
    if {c.key for c in cases} != {c['key'] for c in evidence['cases']} or any(c.revision != revision for c in cases):
        raise StaleProjection(revision)
    return sorted(cases, key=lambda case: (-case.payload.get('research_priority', 0), case.key))


def check_sources(evidence, case_key=None):
    for case in evidence['cases']:
        if case_key is not None and case['key'] != case_key:
            continue
        for view in case['views']:
            if view.get('unavailable_reason'):
                continue
            check_binding(view['source'])
            check_binding(view['preview'])


def display_region(view):
    """Map unrotated top-left normalized coordinates onto a prepared rotated page."""
    if view.get('preview_crop'):
        return {'x': 0, 'y': 0, 'width': 100, 'height': 100}
    x, y, w, h = view['region_xywh']
    rotations = {0: (x, y, w, h), 90: (1-y-h, x, h, w),
                 180: (1-x-w, 1-y-h, w, h), 270: (y, 1-x-w, h, w)}
    require(view['rotation'] in rotations, 'Unsupported rotation')
    return dict(zip(('x', 'y', 'width', 'height'), (n*100 for n in rotations[view['rotation']])))


@contextmanager
def _save_lock():
    """Preserve the save outcome if releasing the lock also fails."""
    body_error = None
    completed = False
    try:
        with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
            try:
                yield
            except BaseException as error:
                body_error = error
                raise
            else:
                completed = True
    except OSError as error:
        if not completed and (body_error is None or error is body_error):
            raise
        logging.getLogger(__name__).exception('Could not release the research lock after a save attempt')
        if body_error is not None:
            raise body_error


def save_review(key, submitted, actor, case_key='photo', *, comparison_decision=False):
    """Replace one readable history file, then refresh its disposable projection."""
    with _save_lock():
        authority = _record_bytes()
        evidence, _, current, revision = validate_records(*authority)
        case = next((c for c in evidence['cases'] if c['key'] == case_key), None)
        require(case is not None, 'Unknown case')
        require(not case.get('read_only', False), 'This case is read-only; no review was saved')
        require(submitted.get('revision') == revision,
                'Records changed since this form was opened. Nothing was saved. Reload and compare your wording.')
        _checked_cases(revision, evidence)
        check_sources(evidence, case_key)
        previous = next((r for r in current if r['key'] == key and assertion_case(r) == case_key), None)
        require(previous is not None, 'Unknown assertion')
        require(not previous.get('component_assertions'), 'Review each photograph comparison separately')
        require(previous['relation'] != PERSON_ATTRIBUTION, 'Use the person-attribution form to correct this record')
        if comparison_decision:
            pair = case.get('reproduction_pairs', {}).get(key)
            require(pair is not None, 'An explicit photograph pair is required')
            views = {v['key']: v for v in case['views']}
            source, candidate = (views[pair[k]] for k in ('query_view_key', 'candidate_view_key'))
            submitted = {**submitted,
                'value': f"The photograph in {source['label'][:200]} is also shown in {candidate['label'][:200]}.",
                'scope': 'Comparison of these two cited views only. A different exposure may still document the same event.',
                'uncertainty': previous['uncertainty'],
                'citation_keys': list(dict.fromkeys([*previous['citation_keys'], *pair.values()]))}
        require(isinstance(actor, str) and actor.strip(), 'Authenticated reviewer required')
        fields = {}
        for field, limit in TEXT_LIMITS.items():
            value = submitted.get(field, '') if field == 'reason' else submitted.get(field)
            require(isinstance(value, str) and (field == 'reason' or value.strip()) and len(value) <= limit,
                    f'Reviewer note must be text of at most {limit} characters' if field == 'reason' else
                    f'{field.capitalize()} is required and must be at most {limit} characters')
            fields[field] = value.strip()
        require(submitted.get('review') in REVIEW_STATES, 'Choose accepted, rejected or unresolved')
        citations = submitted.get('citation_keys')
        allowed = {v['key'] for v in case['views']}
        require(isinstance(citations, list) and citations and len(set(citations)) == len(citations)
                and set(citations) <= allowed, 'Choose one or more known citations')
        record = {**previous, **fields, 'change_kind': 'review', 'review': submitted['review'], 'citation_keys': citations,
                  'revision': previous['revision'] + 1, 'supersedes_revision': previous['revision'],
                  'reviewer': actor, 'reviewed_at': datetime.now(timezone.utc).isoformat(),
                  'based_on_revision': revision, 'synthetic': case_synthetic(case, evidence), 'metadata_adopted': False}
        pair = case.get('reproduction_pairs', {}).get(key)
        if pair:
            views = {v['key']: v for v in case['views']}
            require(set(pair.values()) <= set(citations), 'Cite both photographs/pages in this comparison')
            record['reproduction_pair'] = {'query': view_locator(views[pair['query_view_key']]),
                                           'candidate': view_locator(views[pair['candidate_view_key']])}
        return _append_record(authority, record)


def save_physical_object(case_key, submitted, actor):
    """Correct a case reference without reviewing its research assertion."""
    with _save_lock():
        authority = _record_bytes()
        evidence, _, current, revision = validate_records(*authority)
        case = next((c for c in evidence['cases'] if c['key'] == case_key), None)
        require(case is not None and not case_synthetic(case, evidence), 'Real case required')
        require(not case.get('read_only', False), 'This case is read-only; nothing was saved')
        require(submitted.get('revision') == revision, 'Records changed since this form was opened. Nothing was saved. Reload and compare your wording.')
        _checked_cases(revision, evidence)
        check_sources(evidence, case_key)
        require(isinstance(actor, str) and actor.strip(), 'Authenticated operator required')
        previous = physical_assessment(current, case_key)
        physical = {}
        for key in PHYSICAL_LIMITS:
            value = submitted.get(key, '')
            require(isinstance(value, str), f'Invalid physical-object {key}')
            if value.strip():
                physical[key] = value.strip()
        validate_physical_object(physical)
        require(physical != previous.get('physical_object', {}), 'No physical-object fields changed')
        reason = submitted.get('reason')
        require(isinstance(reason, str) and reason.strip() and len(reason) <= TEXT_LIMITS['reason'], 'A reason for this change is required')
        record = {**previous, 'physical_object': physical, 'change_kind': 'physical_object',
                  'physical_object_reason': reason.strip(), 'recorded_by': actor,
                  'recorded_at': datetime.now(timezone.utc).isoformat(),
                  'revision': previous['revision'] + 1, 'supersedes_revision': previous['revision'],
                  'based_on_revision': revision}
        return _append_record(authority, record)


def _append_record(authority, record):
    """Caller holds the research lock through readable append and SQLite refresh."""
    path = settings.PILOT_ROOT / 'records/assertions.jsonl'
    evidence_bytes, old_bytes = authority
    new_bytes = old_bytes + (b'' if old_bytes.endswith(b'\n') else b'\n') + json.dumps(record, ensure_ascii=False).encode() + b'\n'
    captured = validate_records(evidence_bytes, new_bytes)
    revision = captured[3]
    logger = logging.getLogger(__name__)
    try:
        atomic_write(path, new_bytes)
    except OSError:
        # The write can raise after replacing the history. Only the old or
        # prospective bytes can resolve the outcome under the same lock.
        try:
            confirmed = read_bytes(path)
        except (OSError, ReviewError) as confirmation_error:
            logger.exception('Could not confirm review history after a write error')
            raise SaveOutcomeUnknown('The review history could not be checked after a storage error.') from confirmation_error
        if confirmed == old_bytes:
            raise
        if confirmed != new_bytes:
            logger.exception('Review history did not match either snapshot after a write error')
            raise SaveOutcomeUnknown('The review history changed unexpectedly during write confirmation.')
        logger.exception('Review file replaced but storage confirmation failed')
        return {'indexed': False, 'revision': revision, 'failed_component': 'storage', 'rebuild_available': True}
    try:
        _project(_ReviewProjection(captured, assertion_case(record), record['based_on_revision']))
    except Exception:
        # The decision is already saved. Rebuild only; never resubmit it.
        logger.exception('Review saved; browsing refresh failed')
        return {'indexed': False, 'revision': revision, 'failed_component': 'browsing', 'rebuild_available': True}
    return {'indexed': True, 'revision': revision}


def export_bundle(case_key=None):
    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        evidence, history, current, revision = _snapshot()
        _checked_cases(revision, evidence)
        selected = [c for c in evidence['cases'] if case_key is None or c['key'] == case_key]
        require(selected and all(case_synthetic(c, evidence) for c in selected),
                'Real-material export is not enabled; local inspection and review only')
        check_sources(evidence, case_key)
        if case_key is not None:
            evidence = {'synthetic': True, 'metadata_adopted': False, 'cases': selected,
                        'lineage': selected[0].get('lineage', evidence.get('lineage', ''))}
            history = [r for r in history if assertion_case(r) == case_key]
            current = [r for r in current if assertion_case(r) == case_key]
        bundle = {'synthetic': True, 'purpose': 'research review only', 'metadata_adopted': False,
                  'accession_authorized': False, 'deletion_authorized': False,
                  'revision': revision, 'evidence': evidence, 'assertion_history': history,
                  'current': current,
                  'omitted': ['source images and PDFs', 'credentials', 'disposable browsing index'],
                  'citation_access': 'Requires separately permitted source files and verified root mapping'}
        return json.dumps(bundle, ensure_ascii=False, indent=2).encode() + b'\n'
