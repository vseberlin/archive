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
from .forms import PhysicalObjectForm, ReviewForm
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
        all_cases = checked_cases()
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
        return render(request, 'research_web/case.html', workspace.context(request, key))
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
        if request.method == 'GET':
            checked_cases()
        form = ReviewForm(request.POST if request.method == 'POST' else None,
                          initial={**assertion, 'reason': '', 'revision': revision},
                          views=selected['views'])
        context = {'form': form, 'assertion': assertion, 'case_key': key,
                   'citation_views': selected['views'], 'is_synthetic': case_synthetic(selected, evidence),
                   'citation_revision': request.POST.get('revision', '') if request.method == 'POST' else revision}
        in_workspace = request.method == 'POST' and request.POST.get('workspace') == '1'

        def response(status, error=''):
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
                result = save_review(assertion_key, form.cleaned_data, request.user.get_username(), case_key=key)
            except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
                return response(409, str(error))
            if not result['indexed']:
                return render(request, 'research_web/unavailable.html', {
                    'saved': True, 'rebuild_revision': result['revision'],
                    'message': 'Your review is in the readable history. Do not submit it again. '
                               'Rebuild browsing to display the saved record.'}, status=503)
            messages.success(request, ('Synthetic' if case_synthetic(selected, evidence) else 'Research') + ' review saved. Earlier revisions are retained.')
            if navigation:
                target = navigation['next_url'] if request.POST.get('advance') == '1' else ''
                return redirect(target or navigation['return_url'])
            return redirect('case', key=key)
        return render(request, 'research_web/review.html', context)
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
