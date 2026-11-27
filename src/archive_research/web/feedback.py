"""Refresh only saved findings and exact comparison feedback in the selected index."""
import hashlib
import json

from django.conf import settings

from archive_research.common import ReviewError, file_lock, staged_write
from archive_research.review_feedback import view_locator, reviewed_candidates, feedback_rows


class SearchBaseUnavailable(ReviewError):
    """Refreshing findings requires an available, validated search index."""


def read_search_base():
    from .extraction import base_search_projection
    from .projection_cache import ProjectionChanged

    try:
        return base_search_projection()
    except ProjectionChanged:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SearchBaseUnavailable('The base search index is unavailable or invalid. Restore or regenerate it.') from error


def selected_history(roots, projection):
    history = settings.PILOT_ROOT / 'records/assertions.jsonl'
    selected = []
    for position, spec in enumerate(projection.get('selection', {}).get('findings', [])):
        root = roots.declared_root(spec['root'], 'read')
        if root.path / spec['path'] == history:
            root.checked()
            selected.append((position, spec))
    if len(selected) > 1:
        raise SearchBaseUnavailable('The base search index selects this review history more than once. Regenerate it.')
    return selected[0] if selected else None


def _source_stamp(selected, history_bytes):
    stamp = (settings.PILOT_ROOT / 'records/assertions.jsonl').stat()
    return {**selected, 'size_bytes': len(history_bytes), 'mtime_ns': stamp.st_mtime_ns,
            'sha256': hashlib.sha256(history_bytes).hexdigest()}


def _finding_snapshot(roots, projection):
    from .records import _checked_cases, _record_bytes, validate_records

    selected = selected_history(roots, projection)
    if selected is None:
        return (projection, None, None), None

    position, spec = selected
    with file_lock(settings.PILOT_ROOT / 'records/.research.lock'):
        authority = _record_bytes()
        evidence, _, current, revision = validate_records(*authority)
        cases = _checked_cases(revision, evidence)
        source = _source_stamp(spec, authority[1])
    unavailable = any(item['key'] == f'finding:{position}' for item in projection['unavailable'])
    pending = (authority, current) if source != spec or unavailable else None
    return (projection, cases, (source, revision)), pending


def _catch_up(artifacts):
    from archive_research.search_projection import refresh_validated_findings
    from archive_research.storage import target
    from .records import StaleProjection, _record_bytes

    roots, base = read_search_base()
    if roots.root('artifacts', 'work') != artifacts:
        raise ReviewError('Search configuration changed. Run the search again.')
    path = target(artifacts, settings.PROJECT['research_index'])
    history = settings.PILOT_ROOT / 'records/assertions.jsonl'
    for _ in range(3):
        state, pending = _finding_snapshot(roots, base)
        if pending is None:
            return state
        projection, cases, binding = state
        source, revision = binding
        authority, current = pending
        updated = refresh_validated_findings(projection, source, current)
        data = json.dumps(updated, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'

        try:
            with staged_write(path, data) as publish:
                with file_lock(history.parent / '.research.lock'):
                    if _record_bytes() != authority or history.stat().st_mtime_ns != source['mtime_ns']:
                        continue
                    publish()
                return updated, cases, binding
        except OSError as error:
            raise StaleProjection(revision, 'Could not refresh saved findings. Rebuild browsing to retry.') from error
    raise ReviewError('Saved findings changed during the refresh. Run the search again.')


def search_findings(roots, projection):
    """Catch up selected findings and bind the results to the same case revision."""
    from archive_research.storage import lock

    state, pending = _finding_snapshot(roots, projection)
    if pending is None:
        return state

    # The artifact lock coalesces refreshes and excludes explicit corpus builds.
    # Saves only need the research lock, held briefly to capture and publish.
    artifacts = roots.root('artifacts', 'work')
    try:
        with lock(artifacts):
            return _catch_up(artifacts)
    except BlockingIOError as error:
        raise ReviewError('Search artifacts are being refreshed. Run the search again shortly.') from error


def refresh_search():
    if settings.PROJECT.get('research_index'):
        search_findings(*read_search_base())


def comparisons(case, current, show_rejected=False):
    views = {v['key']: v for v in case['views']}
    records = {r['key']: r for r in current if r.get('case_key', 'photo') == case['key']}
    feedback = feedback_rows(current, {})
    visible, hidden = [], 0
    for key, pair in case.get('reproduction_pairs', {}).items():
        record = records[key]
        query, candidate = views[pair['query_view_key']], views[pair['candidate_view_key']]
        rows, count = reviewed_candidates(view_locator(query), [{'key': key, 'assertion': record,
            'query': query, 'candidate': candidate, 'locator': view_locator(candidate)}], feedback,
            show_rejected=show_rejected)
        visible.extend(rows)
        hidden += count
    visible.sort(key=lambda row: (row['review_state'] != 'accepted', row['review_state'] == 'rejected'))
    return visible, hidden
