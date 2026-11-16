import io

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from archive_common import ReviewError
from .forms import PhysicalObjectForm, ReviewForm
from .records import assertion_case, bound_bytes, case_synthetic, check_binding, checked_cases, display_region, export_bundle, rebuild, save_review, snapshot
from .processing import ocr_status
from .records import physical_assessment, save_physical_object


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith('/static/'):
            response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'same-origin'
        return response


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
        record = next((c for c in checked_cases(request.GET.get('revision')) if c.key == key), None)
        if record is None:
            raise Http404
        views = []
        for view in record.payload['views']:
            try:
                if view.get('unavailable_reason'):
                    raise ReviewError(view['unavailable_reason'])
                check_binding(view['source'])
                check_binding(view['preview'])
                views.append({**view, 'available': True})
            except (ReviewError, OSError) as error:
                views.append({**view, 'available': False, 'error': str(error)})
        return render(request, 'research_web/case.html', {'case': record, 'views': views})
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
        if request.method == 'POST':
            if not form.is_valid():
                return render(request, 'research_web/review.html', context, status=400)
            try:
                result = save_review(assertion_key, form.cleaned_data, request.user.get_username(), case_key=key)
            except (ReviewError, OSError, ValueError, KeyError, TypeError, DatabaseError) as error:
                context['save_error'] = str(error)
                return render(request, 'research_web/review.html', context, status=409)
            if not result['indexed']:
                return render(request, 'research_web/unavailable.html', {
                    'saved': True, 'rebuild_revision': result['revision'],
                    'message': 'Your review is in the readable history. Do not submit it again. '
                               'Rebuild browsing to display the saved record.'}, status=503)
            messages.success(request, ('Synthetic' if case_synthetic(selected, evidence) else 'Research') + ' review saved. Earlier revisions are retained.')
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
