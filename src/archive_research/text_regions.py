"""Compatibility import; implementation lives in archive_research.extractors.text_regions."""
import sys
from importlib import import_module
sys.modules[__name__] = import_module('archive_research.extractors.text_regions')
