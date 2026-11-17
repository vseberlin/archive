"""Compatibility import; implementation lives in archive_research.web.admin."""
import sys
from importlib import import_module
sys.modules[__name__] = import_module('archive_research.web.admin')
