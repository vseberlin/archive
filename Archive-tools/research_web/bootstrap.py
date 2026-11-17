"""Compatibility launcher; implementation is installed in archive_research.web."""
import sys
from archive_research.web.bootstrap import main

if __name__ == "__main__":
    main(sys.argv[1:])
