"""Exact photograph-comparison feedback; never a person or corpus exclusion."""
import json

from .contracts import require


def view_locator(view):
    source = view['source']
    return {'source': {k: source.get(k, 'sample' if k == 'root' else None)
                       for k in ('root', 'path', 'size_bytes', 'mtime_ns')},
            'page': view['page_sequence'] if source['path'].lower().endswith('.pdf') else 0,
            'region': view['region_xywh']}


def entry_locator(entry):
    evidence = entry.get('evidence', {})
    if 'page' not in evidence:
        return None
    page = evidence['page']
    region = evidence.get('region') or {'x': 0, 'y': 0, 'width': 1, 'height': 1}
    return {'source': {k: page['source'].get(k) for k in ('root', 'path', 'size_bytes', 'mtime_ns')},
            'page': page['index'], 'region': [region[k] for k in ('x', 'y', 'width', 'height')]}


def validate_pair(pair):
    require(isinstance(pair, dict) and set(pair) == {'query', 'candidate'}, 'Explicit reproduction pair required')
    for locator in pair.values():
        require(isinstance(locator, dict) and set(locator) == {'source', 'page', 'region'}, 'Invalid comparison locator')
        source = locator['source']
        require(set(source) == {'root', 'path', 'size_bytes', 'mtime_ns'} and
                isinstance(source['root'], str) and isinstance(source['path'], str) and
                type(source['size_bytes']) is int and type(source['mtime_ns']) is int,
                'Bound comparison sources required')
        region = locator['region']
        require(type(locator['page']) is int and locator['page'] >= 0 and
                isinstance(region, list) and len(region) == 4 and
                all(type(n) in (int, float) and 0 <= n <= 1 for n in region) and
                region[2] > 0 and region[3] > 0 and region[0]+region[2] <= 1 and region[1]+region[3] <= 1,
                'Invalid comparison page or region')


def feedback_rows(current, history):
    return [{'pair': row['reproduction_pair'], 'review': row['review'], 'case_key': row.get('case_key', 'photo'),
             'key': row['key'], 'revision': row['revision'], 'reviewer': row.get('reviewer'),
             'reviewed_at': row.get('reviewed_at'), 'history': history}
            for row in current if row.get('reproduction_pair') and row['review'] != 'unreviewed']


def reviewed_candidates(query, rows, feedback, *, show_rejected=False):
    """Rank reviewed matches first; keep raw scores, contradictory reviews and exact scope."""
    def identity(locator):
        # Numeric coordinates 0 and 0.0 describe the same region.
        return json.dumps({**locator, 'region': [float(n) for n in locator['region']]}, sort_keys=True)
    indexed = {}
    if query is not None:
        for item in feedback:
            if identity(item['pair']['query']) == identity(query):
                indexed.setdefault(identity(item['pair']['candidate']), []).append(item)
            elif identity(item['pair']['candidate']) == identity(query):
                indexed.setdefault(identity(item['pair']['query']), []).append(item)
    visible, hidden = [], 0
    for position, row in enumerate(rows):
        matches = indexed.get(identity(row['locator']), []) if row.get('locator') else []
        states = {r['review'] for r in matches}
        state = next(iter(states)) if len(states) == 1 else 'conflicting' if states else 'unreviewed'
        if state == 'rejected' and not show_rejected:
            hidden += 1
            continue
        visible.append({**row, 'review_state': state, 'review_evidence': matches, '_position': position})
    visible.sort(key=lambda r: (r['review_state'] != 'accepted', r['review_state'] == 'rejected', r['_position']))
    for row in visible:
        row.pop('_position')
    return visible, hidden
