"""Refresh only saved findings and exact comparison feedback in the selected index."""
import hashlib
import json
from pathlib import Path

from django.conf import settings

from archive_research.common import atomic_write, read_bytes, require
from archive_research.review_feedback import view_locator, reviewed_candidates, feedback_rows


def refresh_search(captured):
    """Caller holds the existing research lock through save and projection."""
    if not settings.PROJECT.get('research_index'):
        return
    from .extraction import search_projection
    from archive_research.search_projection import refresh_findings
    from archive_research.storage import target
    roots, projection = search_projection(allow_stale_reviews=True)
    history = settings.PILOT_ROOT / 'records/assertions.jsonl'
    selected = [spec for spec in projection['selection'].get('findings', [])
                if roots.root(spec['root'], 'read') / spec['path'] == history]
    require(len(selected) == 1, 'Current review history is not selected for search refresh')
    data = read_bytes(history)
    stamp = history.stat()
    source = {**selected[0], 'size_bytes': len(data), 'mtime_ns': stamp.st_mtime_ns,
              'sha256': hashlib.sha256(data).hexdigest()}
    if source == selected[0]:
        return
    updated = refresh_findings(projection, source, captured[2])
    path = target(roots.root('artifacts', 'work'), settings.PROJECT['research_index'])
    atomic_write(path, json.dumps(updated, ensure_ascii=False, indent=2).encode()+b'\n')


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
