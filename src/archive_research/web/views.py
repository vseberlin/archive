import io
import os

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from archive_research.common import ReviewError
from .forms import PhysicalObjectForm, ReviewForm, PersonAttributionForm, ComparisonDecisionForm
from .records import assertion_case, bound_bytes, case_synthetic, check_binding, checked_cases, display_region, export_bundle, rebuild, save_review, snapshot
from .processing import ocr_status
from .records import physical_assessment, save_physical_object
from . import extraction
from . import workspace


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith('/static/'):
            response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'same-origin'
        return response


@login_required
@require_GET
def page_search(request):
    operator(request)
    try:
        query = request.GET.get('q', '')[:200]
        from django.conf import settings
        if settings.PROJECT.get('research_index'):
            from archive_research.search_projection import query as search_query
            _, projection = extraction.search_projection()
            offset = int(request.GET.get('offset', 0))
            results = search_query(projection, query, offset=offset, fuzzy=request.GET.get('fuzzy') == '1') if query.strip() else None
            return render(request, 'research_web/aggregate_search.html', {
                'query': query, 'results': results, 'projection': projection,
                'revision': projection['projection_sha256'], 'fuzzy': request.GET.get('fuzzy') == '1'})
        hits, rows, revision = extraction.search(query)
        available = sum(row['available'] for row in rows)
        return render(request, 'research_web/search.html', {
            'query': query, 'hits': hits, 'revision': revision, 'total': len(rows),
            'available': available, 'unavailable': len(rows)-available, 'configured': extraction.configured()})
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def research_packet(request):
    operator(request)
    try:
        roots, packet, revision = extraction.research_packet()
        return render(request, 'research_web/research_packet.html', {'packet': packet, 'revision': revision})
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def packet_media(request, index):
    operator(request)
    try:
        candidate = int(request.GET['candidate']) if 'candidate' in request.GET else None
        data = extraction.packet_image(index, request.GET.get('revision'), candidate, request.GET.get('enhancement'))
        return FileResponse(io.BytesIO(data), content_type='image/png')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def packet_source(request, index):
    operator(request)
    try:
        from archive_research import Source
        from archive_research.common import require
        roots, packet, revision = extraction.research_packet()
        require(packet is not None and request.GET.get('revision') == revision,
                'Evidence packet changed; reload it')
        require(0 <= index < len(packet['evidence']), 'Unknown packet evidence')
        item = packet['evidence'][index]
        require(item['available'], 'Bound evidence source unavailable')
        return source_response(roots, Source(**item['page']['source']))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def image_leads(request):
    operator(request)
    try:
        roots, leads, revision = extraction.visual_leads()
        return render(request, 'research_web/image_leads.html', {'leads': leads, 'revision': revision})
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def image_lead_media(request, index, side):
    operator(request)
    try:
        data = extraction.lead_image(index, side, request.GET.get('revision'))
        return FileResponse(io.BytesIO(data), content_type='image/jpeg')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def extraction_citation(request, key):
    operator(request)
    try:
        roots, row, revision = extraction.lookup(key, request.GET.get('revision'))
        word, region = extraction.word_region(row, int(request.GET['word'])) if 'word' in request.GET else (None, None)
        source_path = roots.source(row['result'].page.source)
        cases = [case for case in checked_cases() if any(
            all(view.get('source', {}).get(field) == row['page']['source'][field]
                for field in ('path', 'size_bytes', 'mtime_ns')) and
            check_binding(view['source']) == source_path and
            view.get('page_sequence') == (row['page']['index'] if row['page']['kind'] == 'pdf' else 1)
            for view in case.payload['views'] if view.get('source'))]
        return render(request, 'research_web/extraction_citation.html', {
            'row': row, 'revision': revision, 'word': word, 'region': region, 'cases': cases})
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def extraction_media(request, key):
    operator(request)
    try:
        roots, row, revision = extraction.lookup(key, request.GET.get('revision'))
        return FileResponse(io.BytesIO(extraction.preview_bytes(roots, row)), content_type='image/png')
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def extraction_source(request, key):
    operator(request)
    try:
        roots, row, revision = extraction.lookup(key, request.GET.get('revision'))
        return source_response(roots, row['result'].page.source)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


def retained_view(digest):
    from django.conf import settings
    from archive_research import Roots
    from archive_research.result_records import retained
    roots = Roots.from_toml(settings.PROJECT['extraction_roots'])
    row, result, identity = retained(roots, 'artifacts', digest)
    return roots, row, result, identity


@login_required
@require_GET
def retained_citation(request, digest):
    operator(request)
    try:
        _, row, result, identity = retained_view(digest)
        return render(request, 'research_web/retained_citation.html', {
            'digest': digest, 'identity': identity, 'row': row, 'page': result.page,
            'text': ' '.join(w['text'] for w in row['words'])})
    except (OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def retained_image(request, digest):
    operator(request)
    try:
        roots, _, result, _ = retained_view(digest)
        artifact = next(a for a in result.artifacts if a.kind == 'processed-page')
        import hashlib
        from archive_research.contracts import require
        data = roots.artifact(artifact).read_bytes()
        require(hashlib.sha256(data).hexdigest() == artifact.sha256, 'Retained image changed before serving')
        return FileResponse(io.BytesIO(data), content_type='image/png')
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        return unavailable(request, error)


def source_response(roots, source):
    """Serve only the checked source descriptor, shared by page and packet citations."""
    path = roots.source(source)
    content_types = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg',
                     '.jpeg': 'image/jpeg', '.tif': 'image/tiff', '.tiff': 'image/tiff'}
    if path.suffix.lower() not in content_types:
        raise ReviewError('Unsupported source format')
    handle = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb')
    try:
        info = os.fstat(handle.fileno())
        current = roots.source(source).stat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                current.st_dev, current.st_ino, source.size_bytes, source.mtime_ns):
            raise ReviewError('Source changed before opening')
    except Exception:
        handle.close()
        raise
    return FileResponse(handle, content_type=content_types[path.suffix.lower()],
                        as_attachment=path.suffix.lower() in ('.tif', '.tiff'), filename=path.name)


def unavailable(request, error):
    return render(request, 'research_web/unavailable.html', {
        'message': str(error), 'rebuild_revision': getattr(error, 'revision', None)}, status=409)


def operator(request):
    if not request.user.is_staff:
        raise PermissionDenied


@login_required
@require_GET
def index(request):
    operator(request)
    try:
        all_cases = [c for c in checked_cases() if not c.payload.get('superseded_by')]
        real = [c for c in all_cases if not c.payload['synthetic']]
        show_demo = request.GET.get('demo') == '1'
        visible = [c for c in all_cases if c.payload['synthetic']] if show_demo else (real or all_cases)
        return render(request, 'research_web/index.html', {'cases': visible, 'real_count': len(real), 'show_demo': show_demo})
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def case(request, key):
    operator(request)
    try:
        if request.GET.get('assertion', '').startswith('person-attribution-'):
            from urllib.parse import urlencode
            from django.urls import reverse
            return redirect(reverse('people', args=[key]) + '?' + urlencode({'edit': request.GET['assertion']}))
        context = workspace.context(request, key)
        if context['case'].payload.get('superseded_by') and not request.GET.get('revision'):
            return redirect('case', key=context['case'].payload['superseded_by'])
        return render(request, 'research_web/case.html', context)
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def media(request, key, view_key):
    operator(request)
    try:
        record, view = comparison_view(request, key, view_key)
        check_binding(view['source'])
        data = bound_bytes(view['preview'])
        return FileResponse(io.BytesIO(data), content_type='image/png')
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


def comparison_view(request, key, view_key):
    record = next((c for c in checked_cases(request.GET.get('revision')) if c.key == key), None)
    view = next((v for v in record.payload['views'] if v['key'] == view_key), None) if record else None
    if view is None:
        raise Http404
    if view.get('unavailable_reason'):
        raise ReviewError(view['unavailable_reason'])
    return record, view


@login_required
@require_GET
def citation(request, key, view_key):
    operator(request)
    try:
        record, view = comparison_view(request, key, view_key)
        check_binding(view['source'])
        check_binding(view['preview'])
        return render(request, 'research_web/citation.html', {
            'case': record, 'view': view,
            'region': display_region(view), 'ocr': ocr_status(view),
            'source_is_pdf': view['source']['path'].lower().endswith('.pdf'),
            'source_is_tiff': view['source']['path'].lower().endswith(('.tif', '.tiff')),
            'assertions': [a for a in record.payload['assertions'] if view_key in a['citation_keys']]})
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_GET
def source(request, key, view_key):
    operator(request)
    try:
        record, view = comparison_view(request, key, view_key)
        data = bound_bytes(view['source'])
        # Never render/process a document during an HTTP request.
        if view['source']['path'].lower().endswith('.pdf') and data.startswith(b'%PDF-'):
            content_type = 'application/pdf'
        elif view['source']['path'].lower().endswith('.png') and data.startswith(b'\x89PNG\r\n\x1a\n'):
            content_type = 'image/png'
        elif view['source']['path'].lower().endswith(('.jpg', '.jpeg')) and data.startswith(b'\xff\xd8'):
            content_type = 'image/jpeg'
        elif view['source']['path'].lower().endswith(('.tif', '.tiff')) and data[:4] in (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'):
            content_type = 'image/tiff'
        else:
            raise ReviewError('Unsupported source representation; use the prepared preview')
        return FileResponse(io.BytesIO(data), content_type=content_type,
                            as_attachment=content_type == 'image/tiff',
                            filename=view['source']['path'].rsplit('/', 1)[-1])
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as error:
        return unavailable(request, error)


@login_required
@require_http_methods(['GET', 'POST'])
def review(request, key, assertion_key):
    operator(request)
    try:
        evidence, history, current, revision = snapshot()
        selected = next((c for c in evidence['cases'] if c['key'] == key), None)
        if selected is None:
            raise Http404
        if selected.get('read_only', False):
            raise PermissionDenied('This case is read-only')
        assertion = next((r for r in current if r['key'] == assertion_key and assertion_case(r) == key), None)
        if assertion is None:
            raise Http404
        if assertion['relation'] == 'person_attribution':
            from urllib.parse import urlencode
            from django.urls import reverse
            if request.method == 'POST':
                raise PermissionDenied('Use the person-attribution form to correct this record')
            return redirect(reverse('people', args=[key]) + '?' + urlencode({'edit': assertion_key}))
        if request.method == 'GET':
            checked_cases()
        if assertion.get('component_assertions'):
            if request.method == 'POST':
                return unavailable(request, ReviewError('Review each photograph comparison separately'))
            return redirect(workspace.case_url(key, assertion=assertion_key))
        pair_workspace = request.POST.get('comparison_workspace') == '1' and request.method == 'POST'
        return_assertion = request.POST.get('return_assertion', assertion_key)
        if not any(r['key'] == return_assertion and assertion_case(r) == key and
                   r['relation'] != 'person_attribution' for r in current):
            return_assertion = assertion_key
        form = ReviewForm(request.POST if request.method == 'POST' else None,
                          initial={**assertion, 'reason': '', 'revision': revision},
                          views=selected['views'])
        if pair_workspace:
            form = ComparisonDecisionForm(request.POST)
        context = {'form': form, 'assertion': assertion, 'case_key': key,
                   'citation_views': selected['views'], 'is_synthetic': case_synthetic(selected, evidence),
                   'documentary_workspace': request.POST.get('documentary_workspace') == '1',
                   'citation_revision': request.POST.get('revision', '') if request.method == 'POST' else revision}
        in_workspace = request.method == 'POST' and request.POST.get('workspace') == '1'

        def response(status, error=''):
            if pair_workspace:
                return render(request, 'research_web/case.html', workspace.context(
                    request, key, assertion_key=return_assertion, comparison_form=form,
                    comparison_key=assertion_key, save_error=error), status=status)
            if request.POST.get('people_workspace') == '1':
                return people(request, key, match_form_override=form, match_error=error, match_status=status)
            if in_workspace:
                try:
                    return render(request, 'research_web/case.html', workspace.context(
                        request, key, assertion_key=assertion_key, form=form, save_error=error), status=status)
                except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError):
                    # Preserve the submitted draft even if the browsing projection is stale.
                    pass
            return render(request, 'research_web/review.html', {**context, 'save_error': error}, status=status)

        if request.method == 'POST':
            if not form.is_valid():
                return response(400)
            try:
                navigation = workspace.context(request, key, assertion_key=assertion_key) if in_workspace else None
                result = save_review(assertion_key, form.cleaned_data, request.user.get_username(), case_key=key,
                                     comparison_decision=pair_workspace)
            except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
                return response(409, str(error))
            if not result['indexed']:
                return render(request, 'research_web/unavailable.html', {
                    'saved': True, 'rebuild_revision': result['revision'],
                    'message': 'Your review is in the readable history. Do not submit it again. '
                               'Rebuild browsing to display the saved record.'}, status=503)
            messages.success(request, ('Synthetic' if case_synthetic(selected, evidence) else 'Research') + ' review saved. Earlier revisions are retained.')
            if pair_workspace:
                from urllib.parse import urlencode
                return redirect(workspace.case_url(key, assertion=return_assertion) + '&' +
                                urlencode({'comparison': assertion_key}))
            if request.POST.get('documentary_workspace') == '1':
                from django.urls import reverse
                return redirect(reverse('people', args=[key]) + '?mode=documents#documented-names')
            if request.POST.get('people_workspace') == '1':
                from urllib.parse import urlencode
                from django.urls import reverse
                pair = selected.get('reproduction_pairs', {}).get(assertion_key)
                target = pair['candidate_view_key'] if pair else request.POST.get('target_view_key', '')
                if request.POST.get('advance') == '1':
                    pending = [p['candidate_view_key'] for k, p in selected.get('reproduction_pairs', {}).items()
                               if k != assertion_key and any(r['key'] == k and r.get('case_key') == key and
                                   r['review'] in ('unreviewed', 'unresolved') for r in current)]
                    target = pending[0] if pending else target
                return redirect(reverse('people', args=[key]) + '?' + urlencode({
                    'mode': 'matches', 'target': target, 'show_rejected': request.POST.get('show_rejected', '')}))
            if navigation:
                target = navigation['next_url'] if request.POST.get('advance') == '1' else ''
                return redirect(target or navigation['return_url'])
            return redirect('case', key=key)
        return render(request, 'research_web/review.html', context)
    except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
        return unavailable(request, error)


@login_required
@require_http_methods(['GET', 'POST'])
def people(request, key, *, match_form_override=None, match_error='', match_status=200):
    """Documentary candidate inspection and explicit human attribution."""
    from urllib.parse import urlencode
    from django.urls import reverse
    from .people import RELATION, candidates, save_attribution
    operator(request)
    try:
        evidence, history, current, revision = snapshot()
        selected = next((c for c in evidence['cases'] if c['key'] == key), None)
        if selected is None or not selected.get('documentary_people'):
            raise Http404
        if request.method == 'GET':
            checked_cases(revision)
        params = request.POST if request.method == 'POST' else request.GET
        if selected.get('documentary_overview') and (params.get('mode') == 'documents' or (
                request.method == 'GET' and not any(params.get(k) for k in ('mode', 'target', 'candidate', 'edit')))):
            if request.method != 'GET':
                return HttpResponse(status=405)
            from .people import overview_context
            return render(request, 'research_web/people_documents.html', overview_context(selected, current, revision))
        from .feedback import comparisons
        mode = 'matches' if params.get('mode') == 'matches' and selected.get('reproduction_pairs') else 'people'
        if request.method == 'POST' and mode == 'matches' and match_form_override is None:
            return HttpResponse(status=405)
        show_rejected = params.get('show_rejected') == '1'
        comparison_rows, hidden_rejected = comparisons(selected, current, show_rejected or match_form_override is not None)
        edit = params.get('attribution_key', '') if request.method == 'POST' else params.get('edit', '')
        saved = [r for r in current if r.get('case_key') == key and r['relation'] == RELATION]
        previous = next((r for r in saved if r['key'] == edit), None)
        if edit and previous is None:
            raise Http404
        detail = previous.get('person_attribution', {}) if previous else {}
        target_key = params.get('target_view_key') or params.get('target') or detail.get('target_view_key') or \
                     selected.get('documentary_target_view') or selected['views'][0]['key']
        if mode == 'matches' and match_form_override is None:
            allowed_targets = [r['candidate']['key'] for r in comparison_rows]
            if target_key not in allowed_targets:
                target_key = allowed_targets[0] if allowed_targets else ''
        candidate_key = params.get('candidate_key') or params.get('candidate') or \
                        (detail.get('candidate') or {}).get('key', '')
        initial = {'revision': revision, 'attribution_key': edit, 'target_view_key': target_key,
                   'candidate_key': candidate_key, 'citation_keys': [target_key]}
        if previous:
            initial.update({k: previous[k] for k in ('reason', 'uncertainty', 'review', 'citation_keys')})
            initial['position'] = detail['position']
        form = PersonAttributionForm(request.POST if request.method == 'POST' else None,
                                     initial=initial, case=selected)
        error, status = match_error, match_status
        if request.method == 'POST' and mode == 'people':
            if not form.is_valid():
                status = 400
            else:
                try:
                    result = save_attribution(key, form.cleaned_data, request.user.get_username())
                except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as failure:
                    error, status = str(failure), 409
                else:
                    if not result['indexed']:
                        return render(request, 'research_web/unavailable.html', {
                            'saved': True, 'rebuild_revision': result['revision'],
                            'message': 'Your attribution is saved in the readable history. Do not submit again; rebuild browsing.'}, status=503)
                    messages.success(request, 'Your attribution was saved with its sources and uncertainty. Earlier revisions are retained.')
                    targets = selected.get('documentary_targets', [v['key'] for v in selected['views']])
                    if request.POST.get('advance') == '1' and target_key in targets:
                        target_key = targets[(targets.index(target_key)+1) % len(targets)]
                    return redirect(reverse('people', args=[key]) + '?' + urlencode({'mode': 'people', 'target': target_key}))
        views = {}
        for original in selected['views']:
            view = {**original, 'available': False}
            try:
                if view.get('unavailable_reason'):
                    raise ReviewError(view['unavailable_reason'])
                check_binding(view['source'])
                check_binding(view['preview'])
                view['available'] = True
            except (ReviewError, OSError, ValueError, KeyError, TypeError) as failure:
                view['error'] = str(failure)
            views[view['key']] = view
        query = params.get('q', '')[:200]
        cards = []
        for person in candidates(selected, query):
            cards.append({**person, 'sources': [views[k] for k in person['citation_keys']],
                          'context_rows': [(k.replace('_', ' ').title(), v) for k, v in person['context'].items()],
                          'documented_here': any(p['view_key'] == target_key for p in person['portraits']),
                          'portraits': [{**p, 'view': views[p['view_key']],
                                        'sources': [views[k] for k in p['citation_keys']]} for p in person['portraits']],
                          'select_url': reverse('people', args=[key]) + '?' + urlencode({
                              'target': target_key, 'candidate': person['key'], 'q': query, 'edit': edit}),
                          'selected': person['key'] == candidate_key})
        match = next((r for r in comparison_rows if r['candidate']['key'] == target_key), None)
        if match:
            match = {**match, 'candidate': views[match['candidate']['key']], 'query': views[match['query']['key']]}
            match['display'] = next((v for v in views.values() if v['key'] in match['assertion']['citation_keys']
                                     and v.get('preview_crop') and v['source'] == match['query']['source']), match['query'])
        match_form = match_form_override or (ReviewForm(initial={**match['assertion'], 'reason': '', 'revision': revision},
                                                       views=selected['views']) if match else None)
        target_keys = ([r['candidate']['key'] for r in comparison_rows] if mode == 'matches' else
                       selected.get('documentary_targets', [v['key'] for v in selected['views']]))
        navigation = []
        for target in target_keys:
            row = next((r for r in comparison_rows if r['candidate']['key'] == target), None)
            decisions = [r for r in saved if r['person_attribution']['target_view_key'] == target]
            navigation.append({'view': views[target], 'active': target == target_key,
                'state': row['review_state'] if mode == 'matches' and row else 'reviewed' if decisions else 'unreviewed',
                'url': reverse('people', args=[key])+'?'+urlencode({'target': target, 'mode': mode,
                                                                'show_rejected': '1' if show_rejected else ''})})
        return render(request, 'research_web/people.html', {
            'case': selected, 'revision': revision, 'views': list(views.values()),
            'target': views.get(target_key), 'candidates': cards, 'query': query,
            'candidate_total': len(selected['documentary_people']), 'form': form, 'save_error': error,
            'saved_attributions': saved, 'edit': edit,
            'mode': mode, 'match': match, 'match_form': match_form, 'navigation': navigation,
            'hidden_rejected': hidden_rejected, 'show_rejected': show_rejected,
            'reviewed_count': sum(n['state'] != 'unreviewed' for n in navigation),
            'match_history': [r for r in history if r.get('case_key') == key and r['key'] in selected.get('reproduction_pairs', {})],
            'history': [r for r in history if r.get('case_key') == key and r['relation'] == RELATION]}, status=status)
    except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
        return unavailable(request, error)


@login_required
@require_http_methods(['GET', 'POST'])
def physical_object(request, key):
    operator(request)
    try:
        evidence, history, current, revision = snapshot()
        selected = next((c for c in evidence['cases'] if c['key'] == key), None)
        if selected is None or case_synthetic(selected, evidence):
            raise Http404
        if selected.get('read_only', False):
            raise PermissionDenied('This case is read-only')
        assessment = physical_assessment(current, key)
        if request.method == 'GET':
            checked_cases(revision)
        form = PhysicalObjectForm(request.POST if request.method == 'POST' else None,
                                  initial={**assessment.get('physical_object', {}), 'revision': revision})
        context = {'form': form, 'case_key': key, 'case_title': selected['title']}
        if request.method == 'POST':
            if not form.is_valid():
                return render(request, 'research_web/physical_object.html', context, status=400)
            try:
                result = save_physical_object(key, form.cleaned_data, request.user.get_username())
            except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
                context['save_error'] = str(error)
                return render(request, 'research_web/physical_object.html', context, status=409)
            if not result['indexed']:
                return render(request, 'research_web/unavailable.html', {
                    'saved': True, 'rebuild_revision': result['revision'],
                    'message': 'Your physical-object change is saved in the readable history. '
                               'Do not submit it again. Rebuild browsing to display it.'}, status=503)
            messages.success(request, 'Physical-object reference saved. Research review outcome unchanged.')
            return redirect('case', key=key)
        return render(request, 'research_web/physical_object.html', context)
    except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
        return unavailable(request, error)


@login_required
@require_POST
def rebuild_projection(request):
    operator(request)
    try:
        rebuild(expected_revision=request.POST.get('revision', ''))
    except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
        return unavailable(request, error)
    messages.success(request, 'Browsing rebuilt from readable records. No review was repeated.')
    return redirect('case', key='photo')


@login_required
@require_GET
def export(request):
    operator(request)
    try:
        response = HttpResponse(export_bundle(request.GET.get('case')), content_type='application/json; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="synthetic-research.json"'
        return response
    except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
        return unavailable(request, error)
