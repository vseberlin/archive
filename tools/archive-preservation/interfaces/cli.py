#!/usr/bin/env python3
"""ISS preservation tooling for accepted ADR-001/ADR-002."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import source_registry

from archive_core import ArchiveError
from workflows.ingest import (
    command_add_gap,
    command_add_identifier,
    command_add_testimony,
    command_create_event,
    command_create_object,
    command_create_unit,
    command_core_document_assess,
    command_core_pdf_accession,
    command_init,
    command_intake,
    command_link,
)
from workflows.publish import (
    command_core_create_entity,
    command_core_create_unit,
    command_core_export_unit_catalog,
    command_core_init,
    command_core_migrate,
    command_core_publish,
    command_core_rebuild,
    command_core_status,
    command_rebuild,
    command_refresh_controls,
    command_scan_source_registry,
    command_seal,
    command_split_working_databases,
    command_status,
    command_validate,
)
from workflows.reconcile import (
    command_core_pdf_reconcile,
    command_decide_metadata,
    command_reconcile_museum_digital,
    command_review_external_identity,
)


def common_root_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--note", default="")

def add_evidence(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence", default="")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("init")
    common_root_actor(command)
    command.add_argument("--workstation-id")
    command.set_defaults(handler=command_init)

    command = subparsers.add_parser("create-unit")
    common_root_actor(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--kind", choices=("retrospective", "contemporary", "fixture"), required=True)
    command.set_defaults(handler=command_create_unit)

    command = subparsers.add_parser("create-object")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--assignment-year", type=int, required=True)
    command.add_argument("--display-identifier", default="")
    command.add_argument("--object-type", required=True)
    command.add_argument("--label", required=True)
    command.add_argument("--publication-status", choices=("allowed", "blocked", "unknown"), default="unknown")
    command.add_argument("--status", choices=("active", "withdrawn", "superseded", "unresolved"), default="active")
    command.set_defaults(handler=command_create_object)

    command = subparsers.add_parser("create-event")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--assignment-year", type=int, required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--starts-on", default="")
    command.add_argument("--ends-on", default="")
    command.add_argument("--circa", action="store_true")
    command.add_argument("--location", default="")
    command.add_argument("--description", default="")
    command.add_argument("--status", choices=("active", "withdrawn", "superseded", "unresolved"), default="active")
    command.set_defaults(handler=command_create_event)

    command = subparsers.add_parser("add-testimony")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--event-id", default="")
    command.add_argument("--contributor", required=True)
    command.add_argument("--recorded-at", required=True)
    command.add_argument("--verbatim", required=True)
    command.add_argument("--confidence", choices=("certain", "probable", "possible", "unknown"), default="unknown")
    command.add_argument("--status", choices=("accepted", "rejected", "unresolved"), default="unresolved")
    command.set_defaults(handler=command_add_testimony)

    command = subparsers.add_parser("add-identifier")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--entity-type", choices=("object", "event"), required=True)
    command.add_argument("--entity-id", required=True)
    command.add_argument("--scheme", required=True)
    command.add_argument("--value", required=True)
    command.add_argument("--status", choices=("accepted", "rejected", "unresolved"), default="unresolved")
    command.set_defaults(handler=command_add_identifier)

    command = subparsers.add_parser("add-gap")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--entity-type", choices=("unit", "object", "event", "testimony", "source_file"), required=True)
    command.add_argument("--entity-id", required=True)
    command.add_argument("--gap-type", required=True)
    command.add_argument("--detail", required=True)
    command.add_argument("--next-action", default="")
    command.add_argument("--status", choices=("open", "reviewing", "resolved", "accepted_unknown"), default="open")
    command.set_defaults(handler=command_add_gap)

    command = subparsers.add_parser("intake")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--batch", required=True)
    command.add_argument("--source-root-id", required=True)
    command.add_argument("--source-root", required=True)
    command.add_argument("--path", required=True)
    command.add_argument("--role", choices=("master", "source"), required=True)
    command.set_defaults(handler=command_intake)

    command = subparsers.add_parser("link")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--subject-type", choices=("object", "event", "testimony", "source_file"), required=True)
    command.add_argument("--subject-id", required=True)
    command.add_argument("--predicate", required=True)
    command.add_argument("--object-type", choices=("object", "event", "testimony", "source_file"), required=True)
    command.add_argument("--object-id", required=True)
    command.add_argument("--status", choices=("accepted", "rejected", "unresolved"), default="unresolved")
    command.add_argument("--confidence", choices=("certain", "probable", "possible", "unknown"), default="unknown")
    command.set_defaults(handler=command_link)

    command = subparsers.add_parser("review-external-identity")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--local-object-id", required=True)
    command.add_argument("--external-system", required=True)
    command.add_argument("--external-id", required=True)
    command.add_argument("--snapshot-digest", default="")
    command.add_argument(
        "--outcome",
        choices=("same_object", "component_of_carrier", "wrong_master", "not_same", "defer"),
        required=True,
    )
    command.add_argument("--related-object-id", default="")
    command.add_argument("--target-relationship-id", default="")
    command.add_argument("--confidence", choices=("certain", "probable", "possible", "unknown"), default="certain")
    command.add_argument("--reason", required=True)
    command.set_defaults(handler=command_review_external_identity)

    command = subparsers.add_parser("decide-metadata")
    common_root_actor(command)
    add_evidence(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--object-id", required=True)
    command.add_argument(
        "--field-name",
        choices=("descriptive_title", "description", "material_technique", "dimensions", "depicted_event"),
        required=True,
    )
    command.add_argument(
        "--action",
        choices=(
            "keep_local",
            "accept_external",
            "corrected",
            "split_event",
            "component_events",
            "defer",
        ),
        required=True,
    )
    command.add_argument("--accepted-value", default="")
    command.add_argument("--external-system", required=True)
    command.add_argument("--external-id", required=True)
    command.add_argument("--snapshot-digest", default="")
    command.add_argument("--reason", required=True)
    command.add_argument("--assignment-year", type=int, default=datetime.now(UTC).year)
    command.add_argument("--event-title", default="")
    command.add_argument("--starts-on", default="")
    command.add_argument("--ends-on", default="")
    command.add_argument("--circa", action="store_true")
    command.add_argument("--location", default="")
    command.add_argument("--event-description", default="")
    command.add_argument("--confidence", choices=("certain", "probable", "possible", "unknown"), default="certain")
    command.set_defaults(handler=command_decide_metadata)

    command = subparsers.add_parser("seal")
    common_root_actor(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--preservation-root", required=True)
    command.add_argument("--validator-python", default=sys.executable)
    command.set_defaults(handler=command_seal)

    command = subparsers.add_parser("validate")
    command.add_argument("--bag", required=True)
    command.add_argument("--validator-python", default=sys.executable)
    command.set_defaults(handler=command_validate)

    command = subparsers.add_parser("status")
    command.add_argument("--root", required=True)
    command.set_defaults(handler=command_status)

    command = subparsers.add_parser("reconcile-museum-digital")
    command.add_argument("--root", required=True)
    command.add_argument("--unit", default="")
    command.add_argument("--institution-id", type=int, default=29)
    command.add_argument("--limit", type=int, default=25)
    command.add_argument("--timeout", type=float, default=20.0)
    command.add_argument("--delay", type=float, default=0.2)
    command.add_argument("--output", default="")
    command.add_argument(
        "--source-records",
        action="append",
        default=[],
        help="original CSV/XLSX metadata table; repeat for multiple source files",
    )
    command.add_argument(
        "--comparison-image-root",
        action="append",
        default=[],
        help="known local image root used to resolve source-row filenames; repeat for multiple roots",
    )
    command.set_defaults(handler=command_reconcile_museum_digital)

    command = subparsers.add_parser("scan-source-registry")
    common_root_actor(command)
    command.add_argument(
        "--source-root",
        action="append",
        required=True,
        help="metadata source root as LABEL=/absolute/path; repeat for multiple roots",
    )
    command.add_argument("--max-hash-bytes", type=int, default=source_registry.DEFAULT_MAX_HASH_BYTES)
    command.add_argument(
        "--defer-hash-suffix",
        action="append",
        default=[],
        help="register matching occurrences without hashing yet, for example .pdf; repeat as needed",
    )
    command.add_argument(
        "--extract-pdf",
        action="store_true",
        help="also run pdftotext; PDFs are otherwise registered and exact-deduplicated without text extraction",
    )
    command.add_argument(
        "--replace-derived-index",
        action="store_true",
        help="clear only rebuildable source-registry rows before scanning; source files and prior run reports are untouched",
    )
    command.add_argument("--output", default="")
    command.set_defaults(handler=command_scan_source_registry)

    command = subparsers.add_parser("refresh-controls")
    common_root_actor(command)
    command.set_defaults(handler=command_refresh_controls)

    command = subparsers.add_parser("split-working-databases")
    command.add_argument("--root", required=True)
    command.set_defaults(handler=command_split_working_databases)

    command = subparsers.add_parser("core-init")
    common_root_actor(command)
    command.add_argument(
        "--retained-index",
        default="",
        help="existing source-discovery SQLite index to fingerprint and open read-only",
    )
    command.set_defaults(handler=command_core_init)

    command = subparsers.add_parser("core-publish")
    common_root_actor(command)
    command.add_argument("--preservation-root", required=True)
    command.set_defaults(handler=command_core_publish)

    command = subparsers.add_parser("core-export-unit-catalog")
    command.add_argument("--root", required=True)
    command.add_argument("--unit", required=True)
    command.set_defaults(handler=command_core_export_unit_catalog)

    command = subparsers.add_parser("core-pdf-reconcile")
    common_root_actor(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--entity", required=True)
    command.add_argument("--local-pdf", action="append", required=True)
    command.add_argument("--md-id", required=True)
    command.add_argument(
        "--select",
        required=True,
        help="explicit candidate ID such as local:1 or md:105386",
    )
    command.add_argument("--timeout", type=float, default=30.0)
    command.set_defaults(handler=command_core_pdf_reconcile)

    command = subparsers.add_parser("core-pdf-accession")
    command.add_argument("--root", required=True)
    command.add_argument("--plan", required=True)
    command.add_argument("--actor", required=True)
    command.set_defaults(handler=command_core_pdf_accession)

    command = subparsers.add_parser("core-document-assess")
    command.add_argument("--path", required=True)
    command.set_defaults(handler=command_core_document_assess)

    command = subparsers.add_parser("core-migrate")
    common_root_actor(command)
    command.add_argument("--preservation-root", required=True)
    command.set_defaults(handler=command_core_migrate)

    command = subparsers.add_parser("core-create-unit")
    common_root_actor(command)
    command.add_argument("--unit", required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--kind", required=True)
    command.set_defaults(handler=command_core_create_unit)

    command = subparsers.add_parser("core-create-entity")
    common_root_actor(command)
    command.add_argument(
        "--kind",
        choices=("archive_object", "historical_event", "physical_carrier"),
        required=True,
    )
    command.add_argument("--name", required=True)
    command.add_argument("--unit", required=True)
    command.add_argument("--role", default="primary")
    command.add_argument("--language", default="")
    command.add_argument("--assignment-year", type=int)
    command.add_argument("--archival-label", default="")
    command.add_argument("--descriptive-title", default="")
    command.add_argument(
        "--publication-status",
        choices=("allowed", "blocked", "unknown"),
        default="unknown",
    )
    command.add_argument("--event-title", default="")
    command.add_argument("--starts-on", default="")
    command.add_argument("--ends-on", default="")
    command.add_argument("--circa", action="store_true")
    command.add_argument("--description", default="")
    command.add_argument("--material-technique", default="")
    command.add_argument("--carrier-type", default="")
    command.add_argument("--dimensions", default="")
    command.add_argument("--condition-note", default="")
    command.set_defaults(handler=command_core_create_entity)

    command = subparsers.add_parser("core-rebuild")
    command.add_argument("--generation", required=True)
    command.add_argument("--output-db", required=True)
    command.set_defaults(handler=command_core_rebuild)

    command = subparsers.add_parser("core-status")
    command.add_argument("--root", required=True)
    command.set_defaults(handler=command_core_status)

    command = subparsers.add_parser("rebuild")
    command.add_argument("--bag", action="append", required=True)
    command.add_argument("--output-db", required=True)
    command.add_argument("--actor", required=True)
    command.add_argument("--workstation-id")
    command.set_defaults(handler=command_rebuild)
    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (
        ArchiveError,
        catalog_core.CoreCatalogError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as error:
        print(json.dumps({"result": "fail", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
