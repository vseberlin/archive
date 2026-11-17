from django.apps import AppConfig


class ResearchWebConfig(AppConfig):
    name = 'archive_research.web'
    # Keep existing tables, content types, permissions and admin URLs unchanged.
    label = 'research_web'
    verbose_name = 'Archive research'
