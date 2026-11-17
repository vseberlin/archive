#!/usr/bin/env python3
"""Compatibility command; install iss-archive-research and prefer archive-research."""
import sys
from archive_research.cli import main as application_main


def main():
    return application_main(['packet', *sys.argv[1:]])


if __name__ == '__main__':
    main()
