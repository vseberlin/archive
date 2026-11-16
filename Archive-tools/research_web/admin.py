from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils.html import format_html

from .models import ResearchCase
from .records import checked_cases


@admin.register(ResearchCase)
class ResearchCaseAdmin(admin.ModelAdmin):
    list_display = ['key', 'title', 'comparison']
    readonly_fields = ['key', 'title', 'revision', 'payload', 'comparison']
    actions = None

    def get_queryset(self, request):
        try:
            checked_cases()
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise PermissionDenied('Research projection unavailable; rebuild before browsing') from error
        return super().get_queryset(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff

    @admin.display(description='Comparison')
    def comparison(self, obj):
        return format_html('<a href="{}">Open comparison</a>', reverse('case', args=[obj.key]))


admin.site.site_header = 'WF research · synthetic pilot'
admin.site.site_title = 'WF research'
admin.site.index_title = 'Local pilot administration'
