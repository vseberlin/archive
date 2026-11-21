"""Render explicit photograph pairs without case-specific UI branches."""
from .forms import ComparisonDecisionForm


LABELS = {'accepted': 'Same photograph', 'rejected': 'Different photograph',
          'unresolved': 'Uncertain', 'unreviewed': 'Not reviewed'}


def context(case, assertion, views, revision, form=None, selected_key=''):
    pairs = case.get('reproduction_pairs', {})
    records = {r['key']: r for r in case['assertions']}
    keys = assertion.get('component_assertions')
    focus = selected_key if selected_key in pairs else assertion['key'] if assertion['key'] in pairs else next(iter(pairs), '')
    if not keys and assertion['key'] in pairs:
        parent = next((r for r in records.values() if assertion['key'] in r.get('component_assertions', [])), None)
        keys = parent['component_assertions'] if parent else [
            k for k, pair in pairs.items() if pair['query_view_key'] == pairs[assertion['key']]['query_view_key']]
    if not keys:
        keys = [k for k, pair in pairs.items() if pair['query_view_key'] == pairs[focus]['query_view_key']] if focus else []
    if not keys:
        return None
    if selected_key not in keys:
        selected_key = assertion['key'] if assertion['key'] in keys else keys[0]
    by_view = {v['key']: v for v in views}
    anchor = by_view[pairs[keys[0]]['query_view_key']]
    candidates = []
    for key in keys:
        record = records[key]
        candidate = by_view[pairs[key]['candidate_view_key']]
        submitted = form is not None and form.is_bound and key == selected_key
        decision_form = form if submitted else ComparisonDecisionForm(initial={'revision': revision})
        decision_form.auto_id = f'comparison_{key}_%s'
        decision_form.fields['reason'].widget.attrs['form'] = f'pair-decision-{key}'
        candidates.append({'key': key, 'view': candidate, 'record': record, 'form': decision_form,
                           'view_index': views.index(candidate), 'active': key == selected_key,
                           'state_label': LABELS[record['review']], 'submitted': submitted,
                           'history': [r for r in case['history'] if r['key'] == key]})
    return {'anchor': anchor, 'anchor_index': views.index(anchor), 'candidates': candidates,
            'selected_index': next(c['view_index'] for c in candidates if c['active']),
            'accepted': [c for c in candidates if c['record']['review'] == 'accepted'],
            'rejected': [c for c in candidates if c['record']['review'] == 'rejected'],
            'pending': [c for c in candidates if c['record']['review'] in ('unreviewed', 'unresolved')]}
