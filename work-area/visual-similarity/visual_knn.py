#!/usr/bin/env python3
"""Compatibility entry point for the installed archive-research reference commands."""
import sys
from importlib import import_module
from archive_research.reference import main

if __name__ == '__main__':
    raise SystemExit(main())
else:
    sys.modules[__name__] = import_module('archive_research.reference')
