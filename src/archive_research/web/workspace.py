"""A single review workspace over existing cases, citations and review operations."""
from urllib.parse import urlencode

from django.http import Http404
from django.urls import reverse
from django.conf import settings

from archive_research.common import ReviewError
from .forms import ReviewForm
from .processing import ocr_status
from .records import check_binding, checked_cases, display_region


def text_projection():
    """Read the selected projection; never discover sources or run OCR in a request."""
    if not settings.PROJECT.get('research_index'):
        return None
    from .extraction import search_projection
    return search_projection()[1]


def prepared_text(view, projection):
    cached = ocr_status(view)
    if cached['status'] == 'ready' or not projection:
        return cached
    matches = []
    for entry in projection['entries']:
        if entry['kind'] != 'extraction' or entry.get('extractor') not in ('ocr', 'pdf-text'):
            continue
        evidence = entry['evidence']
        page = evidence['page']
        if (page['kind'] == 'pdf' and page['index'] == view['page_sequence'] and
                evidence.get('region') is None and entry['availability'] == 'available' and
                all(page['source'].get(k) == view['source'].get(k, 'sample' if k == 'root' else None)
                    for k in ('root', 'path', 'size_bytes', 'mtime_ns')) and entry['fields']['text'].strip()):
            matches.append(entry)
    if not matches:
        return cached
    selected = matches[-1]  # Last explicitly selected result; no inferred accuracy ranking.
    return {'status': 'ready', 'text': selected['fields'].get('positioned_lines') or selected['fields']['text'],
            'label': 'Extracted PDF text' if selected['extractor'] == 'pdf-text' else 'Unreviewed machine OCR',
            'note': 'Prepared text from the selected page index. Check against the source; text quality is not certified.',
            'url': reverse('extraction_citation', args=[selected['key']]) + '?' +
                   urlencode({'revision': projection['projection_sha256']})}


def case_url(key, query='', state='all', assertion=''):
    params = {k: v for k, v in (('q', query), ('state', state if state != 'all' else ''),
                                ('assertion', assertion)) if v}
    return reverse('case', args=[key]) + ('?' + urlencode(params) if params else '')


def context(request, key, *, assertion_key=None, form=None, save_error=''):
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
        reviews = [a['review'] for a in candidate.payload['assertions']]
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
    assertions = record.payload['assertions']
    selected_key = assertion_key or request.GET.get('assertion') or assertions[0]['key']
    assertion = next((a for a in assertions if a['key'] == selected_key), None)
    if assertion is None:
        raise Http404
    if form is None:
        form = ReviewForm(initial={**assertion, 'reason': '', 'revision': record.revision},
                          views=record.payload['views'])
    try:
        projection = text_projection()
    except (ReviewError, OSError, ValueError, KeyError, TypeError):
        projection = None  # Bound per-view OCR can still be shown; no source fallback.
    views = []
    for original in record.payload['views']:
        view = {**original, 'available': False, 'is_pdf': False, 'is_tiff': False}
        try:
            if view.get('unavailable_reason'):
                raise ReviewError(view['unavailable_reason'])
            check_binding(view['source'])
            check_binding(view['preview'])
            view.update(available=True, region=display_region(view), ocr=prepared_text(view, projection),
                        is_pdf=view['source']['path'].lower().endswith('.pdf'),
                        is_tiff=view['source']['path'].lower().endswith(('.tif', '.tiff')))
        except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
            view['error'] = str(error)
        views.append(view)
    # Advance among findings of this case before leaving for another case.
    pending = next((a for a in assertions if a['key'] != selected_key and a['review'] == 'unreviewed'), None)
    next_url = (case_url(key, query, state, pending['key']) if pending else
                queue[position + 1]['url'] if position + 1 < len(queue) else '')
    default_evidence = next((i for i, view in enumerate(views) if i and
                             (view.get('source') or {}).get('path', '').lower().endswith('.pdf') and
                             view['key'] in assertion['citation_keys']), 1 if len(views) > 1 else 0)
    return {'case': record, 'views': views, 'queue': queue, 'queue_query': query, 'queue_state': state,
            'default_evidence': default_evidence,
            'position': position + 1, 'queue_count': len(queue),
            'previous_url': queue[position - 1]['url'] if position else '',
            'next_case_url': queue[position + 1]['url'] if position + 1 < len(queue) else '',
            'next_url': next_url, 'return_url': case_url(key, query, state, selected_key),
            'finding_links': [{**a, 'url': case_url(key, query, state, a['key'])} for a in assertions],
            'assertion': assertion, 'form': form, 'save_error': save_error,
            'advance': params.get('advance', '1') == '1' if request.method == 'GET' else params.get('advance') == '1',
            'inspection': request.GET.get('inspect') == '1'}
