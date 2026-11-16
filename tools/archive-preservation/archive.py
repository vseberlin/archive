#!/usr/bin/env python3
"""Compatibility entry point for the modular ISS preservation application."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import catalog_core
import source_registry

from archive_core import ArchiveError
from archive_core.catalog import *
from archive_core.documents import *
from archive_core.filesystem import *
from archive_core.filesystem import _read_json_object
from archive_core.identity import *
from archive_core.metadata import *
from archive_core.validation import *
from interfaces.cli import add_evidence, build_parser, common_root_actor, main
from workflows.ingest import *
from workflows.publish import *
from workflows.publish import _core_mutation_connection
from workflows.reconcile import *
from workflows import reconcile as _reconcile_workflow


_reconcile_command = command_reconcile_museum_digital


def command_reconcile_museum_digital(args):
    """Compatibility bridge for callers that patch the old fetch helper."""

    original_fetch = _reconcile_workflow.fetch_museum_digital_query
    _reconcile_workflow.fetch_museum_digital_query = (
        fetch_museum_digital_query
    )
    try:
        return _reconcile_command(args)
    finally:
        _reconcile_workflow.fetch_museum_digital_query = original_fetch


if __name__ == "__main__":
    raise SystemExit(main())
