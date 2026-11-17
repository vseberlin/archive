"""Compatibility launcher; implementation is installed in archive_research.web."""
import sys
from archive_research.web.manage import main

if __name__ == "__main__":
    main(['manage', *[a.replace('research_web', 'archive_research.web', 1) if a.startswith('research_web') else a for a in sys.argv[1:]]])
