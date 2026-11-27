"""A single review workspace over existing cases, citations and review operations."""
from urllib.parse import urlencode

from django.http import Http404
from django.urls import reverse
from django.conf import settings

from archive_research.common import ReviewError
from .forms import ReviewForm
from .processing import ocr_status
from .projection_cache import base_search_projection, case_index
from .records import check_binding, checked_cases, display_region


# The 18 November evidence audit substantiated these legacy assessments before
# fixed finding views were introduced. The three parked pilot cases lacked a
# distinct research comparison as presented.
AUDITED_ASSESSMENTS = frozenset({'P03', 'P05', 'P06', 'L01', 'L02', 'L03', 'L04'})
EVALUATION_GROUPS = frozenset({'Prepared profile examples', 'Handwriting examples'})


def navigation_cases(records, *, synthetic=False):
    assessments, evaluations = [], []
    for record in records:
        payload = record.payload
        if payload['synthetic'] != synthetic or payload.get('superseded_by'):
            continue
        if synthetic:
            assessments.append(record)
            continue
        has_review = any(a.get('reviewer') and a['review'] != 'unreviewed' and not a.get('component_assertions')
                         for a in payload['assertions'])
        if payload.get('finding_views') or has_review or record.key in AUDITED_ASSESSMENTS:
            group = evaluations if payload.get('group_label') in EVALUATION_GROUPS else assessments
            group.append(record)
    return assessments, evaluations


def text_projection():
    """Read the selected projection; never discover sources or run OCR in a request."""
    if not settings.PROJECT.get('research_index'):
        return None
    return base_search_projection()[1]


def prepared_text(view, projection, *, indexed=None):
    cached = ocr_status(view)
    if not projection:
        return cached
    matches = (indexed if indexed is not None else case_index(projection)).prepared_entries(view)
    # A cache copied from an indexed extraction is a derived view, not a pinned
    # reading. Reusing a page in another case must not revive its older OCR.
    # Preserve separately prepared/crop-specific caches outside that selection.
    if cached['status'] == 'ready' and (view.get('preview_crop') or
            cached.get('extraction_key') not in {entry['key'] for entry in matches}):
        return cached
    if not matches:
        return cached
    selected = matches[-1]  # Last explicitly selected result; no inferred accuracy ranking.
    blocks = []
    # A cropped preview is not a full-page coordinate plane.
    if not view.get('preview_crop'):
        for block in selected['evidence'].get('text_blocks', []):
            region = block['region']
            shown = display_region({**view, 'region_xywh': [region[k] for k in ('x','y','width','height')]})
            blocks.append({**block, 'display_region': shown})
    return {'status': 'ready', 'text': selected['fields'].get('positioned_lines') or selected['fields']['text'],
            'blocks': blocks,
            'label': {'pdf-text':'Extracted PDF text','document-text':'Layout-guided OCR','handwriting':'Selected handwritten lines'}.get(selected['extractor'],'Unreviewed machine OCR'),
            'note': ('Article/block structure retained from the selected assembly. OCR remains unreviewed; full-page accuracy is not certified.'
                     if selected['extractor']=='document-text' else
                     'Prepared text from the selected page index. Check against the source; text quality is not certified.'),
            'url': reverse('extraction_citation', args=[selected['key']]) + '?' +
                   urlencode({'revision': projection['projection_sha256']})}


def case_url(key, query='', state='all', assertion=''):
    params = {k: v for k, v in (('q', query), ('state', state if state != 'all' else ''),
                                ('assertion', assertion)) if v}
    return reverse('case', args=[key]) + ('?' + urlencode(params) if params else '')


def catalogue_context(view, lookup):
    """Display attributed existing descriptions; never turn them into findings."""
    source = view.get('source') or {}
    entries = lookup.get((source.get('root', 'sample'), source.get('path')), [])
    fields = [('title', 'Title'), ('date_text', 'Recorded date'), ('creator', 'Creator / author'),
              ('description', 'Existing description'), ('places', 'Places'), ('tags', 'Subjects'),
              ('relationship', 'Recorded relationships'), ('inscription', 'Recorded inscription'),
              ('collections', 'Collections'), ('subdivisions', 'Collection groups'),
              ('historical_title', 'Earlier recorded title'), ('historical_description', 'Earlier recorded description'),
              ('current_title', 'Current object title'), ('current_description', 'Current object description'),
              ('historical_date_text', 'Earlier recorded date'), ('object_type', 'Object type'),
              ('material', 'Material'), ('dimensions', 'Dimensions'), ('condition_note', 'Condition'),
              ('storage_location', 'Recorded location'), ('rights', 'Rights'), ('rights_holder', 'Rights holder')]
    rows = []
    for key, label in fields:
        values = {}
        for entry in entries:
            text = entry['fields'].get(key, '').strip()
            if key.startswith(('current_', 'historical_')) and text == entry['fields'].get(key.split('_', 1)[1], '').strip():
                continue
            if not text:
                continue
            reference = entry['evidence'].get('catalogue_label', entry['source_identity']['unit'])
            reference += ' · ' + (entry['fields'].get('inventory') or entry['source_identity']['object_id'] or 'unassigned record')
            values.setdefault(text, set()).add(reference)
        if values:
            rows.append({'key': key, 'label': label, 'conflict': len(values) > 1,
                         'values': [{'text': text, 'sources': sorted(sources)} for text, sources in values.items()]})
    identities = {(e['source_identity']['unit'], e['source_identity']['object_id']) for e in entries}
    return {'available': bool(entries), 'rows': rows, 'ambiguous_object': len(identities) > 1,
            'conflicting_fields': any(row['conflict'] for row in rows),
            'dates': sorted({e['fields']['date_text'] for e in entries if e['fields'].get('date_text')}),
            'creators': sorted({e['fields']['creator'] for e in entries if e['fields'].get('creator')})}


def navigation(request, key, *, assertion_key=None):
    """Resolve the queue and the selected finding without preparing media or search data."""
    records = checked_cases(request.GET.get('revision') if request.method == 'GET' else None)
    record = next((c for c in records if c.key == key), None)
    if record is None:
        raise Http404
    params = request.POST if request.method == 'POST' else request.GET
    query = params.get('q', '')[:200].strip()
    state = params.get('state', 'all')
    if state not in ('all', 'open', 'reviewed'):
        state = 'all'
    peers = [c for c in records if c.payload['synthetic'] == record.payload['synthetic']]
    queue = []
    for candidate in peers:
        if candidate.payload.get('superseded_by') and candidate.key != key:
            continue
        reviews = [a['review'] for a in candidate.payload['assertions'] if not a.get('component_assertions')]
        is_open = any(r in ('unreviewed', 'unresolved') for r in reviews)
        label = reviews[0].title() if len(set(reviews)) == 1 else 'Mixed findings'
        matches = (not query or query.casefold() in
                   (candidate.key + ' ' + candidate.title + ' ' + candidate.payload.get('question', '')).casefold())
        matches = matches and (state == 'all' or is_open == (state == 'open'))
        if matches or candidate.key == key:
            queue.append({'key': candidate.key, 'title': candidate.title, 'status': label,
                          'active': candidate.key == key, 'outside_filter': not matches,
                          'url': case_url(candidate.key, query, state)})
    position = next(i for i, c in enumerate(queue) if c['active'])
    assertions = [a for a in record.payload['assertions'] if a['relation'] != 'person_attribution']
    selected_key = assertion_key or request.GET.get('assertion') or assertions[0]['key']
    parent = next((a for a in assertions if a['key'] == selected_key and a.get('component_assertions')), None)
    if parent and record.payload.get('finding_views'):
        selected_key = parent['component_assertions'][0]
    assertion = next((a for a in assertions if a['key'] == selected_key), None)
    if assertion is None:
        raise Http404
    # Advance among findings of this case before leaving for another case.
    pending = next((a for a in assertions if a['key'] != selected_key and a['review'] == 'unreviewed'
                    and not a.get('component_assertions')), None)
    next_url = (case_url(key, query, state, pending['key']) if pending else
                queue[position + 1]['url'] if position + 1 < len(queue) else '')
    return {'case': record, 'assertion': assertion, 'assertions': assertions,
            'queue': queue, 'queue_query': query, 'queue_state': state,
            'position': position + 1, 'queue_count': len(queue),
            'previous_url': queue[position - 1]['url'] if position else '',
            'next_case_url': queue[position + 1]['url'] if position + 1 < len(queue) else '',
            'next_url': next_url, 'return_url': case_url(key, query, state, selected_key)}


def context(request, key, *, assertion_key=None, form=None, save_error='', comparison_form=None, comparison_key=''):
    nav = navigation(request, key, assertion_key=assertion_key)
    record, assertion = nav['case'], nav['assertion']
    assertions = nav.pop('assertions')
    components = {k for a in assertions for k in a.get('component_assertions', [])}
    query, state = nav['queue_query'], nav['queue_state']
    params = request.POST if request.method == 'POST' else request.GET
    if form is None:
        form = ReviewForm(initial={**assertion, 'reason': '', 'revision': record.revision},
                          views=record.payload['views'])
    try:
        projection = text_projection()
        indexed = case_index(projection) if projection else None
    except (ReviewError, OSError, ValueError, KeyError, TypeError):
        projection = None  # Bound per-view OCR can still be shown; no source fallback.
        indexed = None
    metadata = indexed.catalogue if indexed else {}
    views = []
    for original in record.payload['views']:
        view = {**original, 'available': False, 'is_pdf': False, 'is_tiff': False}
        view['catalogue'] = catalogue_context(view, metadata)
        try:
            if view.get('unavailable_reason'):
                raise ReviewError(view['unavailable_reason'])
            check_binding(view['source'])
            check_binding(view['preview'])
            view.update(available=True, region=display_region(view), ocr=prepared_text(view, projection, indexed=indexed),
                        is_pdf=view['source']['path'].lower().endswith('.pdf'),
                        is_tiff=view['source']['path'].lower().endswith(('.tif', '.tiff')))
        except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
            view['error'] = str(error)
        views.append(view)
    default_evidence = next((i for i, view in enumerate(views) if i and
                             (view.get('source') or {}).get('path', '').lower().endswith('.pdf') and
                             view['key'] in assertion['citation_keys']), 1 if len(views) > 1 else 0)
    from .comparisons import context as comparison_context
    focus = record.payload.get('finding_views', {}).get(assertion['key'])
    comparison = comparison_context(record.payload, assertion, views, record.revision, comparison_form,
                                    assertion['key'] if focus else comparison_key or params.get('comparison', ''))
    if focus and assertion['key'] not in record.payload.get('reproduction_pairs', {}):
        comparison = None
    if comparison:
        default_evidence = comparison['selected_index']
    if focus:
        by_key = {v['key']: {**v, 'index': i} for i, v in enumerate(views)}
        focus = {**focus, 'anchor': by_key[focus['source_view']],
                 'evidence': by_key[focus['evidence_view']],
                 'anchor_views': [by_key[k] for k in [focus['source_view'], *focus['source_context']]],
                 'evidence_views': [by_key[k] for k in [focus['evidence_view'], *focus['evidence_context'], *focus.get('evidence_pages', [])]],
                 'page_navigation': bool(focus.get('evidence_pages'))}
        if comparison:
            comparison = {**comparison, 'candidates': [c for c in comparison['candidates'] if c['key'] == assertion['key']]}
        focus['anchor_indices'] = [v['index'] for v in focus['anchor_views']]
        focus['evidence_indices'] = [v['index'] for v in focus['evidence_views']]
        default_evidence = focus['evidence']['index']
    return {**nav, 'views': views,
            'finding_focus': focus,
            'default_anchor': focus['anchor']['index'] if focus else comparison['anchor_index'] if comparison else 0,
            'comparison': comparison,
            'comparison_finding': bool(assertion.get('component_assertions') or
                                       assertion['key'] in record.payload.get('reproduction_pairs', {})),
            'comparison_url': case_url(key, query, state, next(iter(record.payload['reproduction_pairs'])))
                              if record.payload.get('reproduction_pairs') else '',
            'default_evidence': default_evidence,
            'finding_links': [{**a, 'value': record.payload.get('finding_views', {}).get(a['key'], {}).get('question') or
                              ('Photograph comparisons' if a.get('component_assertions') else a['value']),
                               'url': case_url(key, query, state, a['key'])} for a in assertions
                              if (not a.get('component_assertions') if record.payload.get('finding_views') else a['key'] not in components)],
            'form': form, 'save_error': save_error,
            'advance': params.get('advance', '1') == '1' if request.method == 'GET' else params.get('advance') == '1',
            'inspection': request.GET.get('inspect') == '1'}
