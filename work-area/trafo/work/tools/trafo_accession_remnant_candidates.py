#!/usr/bin/env python3
"""Guarded additive accession for two reviewed Trafo remnant candidates."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
BASE_TOOL = Path(
    "/mnt/volume/iss-archive-work/trafo/work/tools/trafo_add_1988.py"
)
SPEC = importlib.util.spec_from_file_location("trafo_addition_base", BASE_TOOL)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load additive accession base: {BASE_TOOL}")
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)

RUN_ID = "trafo-remnant-candidates-001"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
WORK = Path("/mnt/volume/iss-archive-work/trafo")
RUN_ROOT = WORK / "work/accession-runs" / RUN_ID
STAGE = RUN_ROOT / "package-addition"
ROLLBACK = RUN_ROOT / "rollback-metadata"
DRY_RUN = (
    WORK
    / "work/accession-runs/trafo-remnant-candidates-dry-run-003"
)
SOURCE_ROOT = Path("/media/vladimir/Elements")
PROFILE = "iss-tiff-v2+trafo-page-1"

ISSUE = "TRO-1957-037-02"
DECK = "TRO-1989-Deckblatt"
ISSUE_TIFFS = [
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Charge 02 tif bis 1969/"
    f"{ISSUE}/{ISSUE}_{page:03d}.tif"
    for page in range(1, 5)
]
DECK_TIFFS = [
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Trafo tifs 1970-1990/"
    f"{DECK}/{DECK}_{page:03d}.tif"
    for page in range(1, 3)
]
DECK_PDF = (
    SOURCE_ROOT
    / "restore/archive/Trafo_Bibliocopy-gesamt/Charge 02 pdf/"
    f"{DECK}.pdf"
)

BASE_OBJECTS = 1590
BASE_REPRESENTATIONS = 11972
BASE_TIFFS = 10377
BASE_ACCESS_PDFS = 1591
ADDED_OBJECTS = 2
ADDED_TIFFS = 6
ADDED_ACCESS_PDFS = 2
ADDED_REPRESENTATIONS = 8
EXPECTED_NEW_FILES = 19
EXPECTED_CHECKSUMS = 25559

OBJECTS = {
    ISSUE: {
        "inventory": ISSUE,
        "museum_digital_object_id": "",
        "title": (
            "TRO-Betriebszeitung 'Der Transformator'; Nr. 37/1957 "
            "(September 1957), Bestand 02"
        ),
        "description": (
            "Der Transformator, Betriebszeitung des Transformatorenwerkes "
            "\"Karl Liebknecht\"; 9. Jahrgang; Nr. 37, September 1957; "
            "Herausgeber: SED-Betriebsparteiorganisation; 4 Seiten. Lokale "
            "Bibliocopy-Digitalisierung. Der Bestand wurde vom Kurator als "
            "eigenständiger Zugang TRO-1957-037-02 bestätigt; ein "
            "gleichnamiger museum-digital-Datensatz liegt nicht vor."
        ),
        "object_type": "Zeitung",
        "page_object_type": "Zeitungsseite",
        "material_technique": "Papier",
        "dimensions": "",
        "date_created": "September 1957",
        "collection_name": "Betriebszeitung des TRO - Der Trafo",
        "rights_usage_terms": "unknown",
        "metadata_license": "not supplied",
        "metadata_source": "local-source-curator-reviewed-no-md-record",
        "page_count": 4,
        "representation_count": 5,
        "status": "unsealed",
        "page_label": "Seite",
    },
    DECK: {
        "inventory": DECK,
        "museum_digital_object_id": "",
        "title": (
            "Bestandsdeckblatt des Pressearchivs des Heimatmuseums Köpenick "
            "zu 'Der Trafo' (1989–1990)"
        ),
        "description": (
            "Zweiseitige Sammlungsbegleitunterlage des Pressearchivs des "
            "Heimatmuseums Köpenick. Blatt 1 verzeichnet für 'Der Trafo' die "
            "Bestände 1989 Nr. 1–29 und 31–49 sowie 1990 Nr. 1–4, 6–14 und "
            "18–23; Blatt 2 zeigt das Köpenicker Siegelmotiv. Lokale "
            "Bibliocopy-Digitalisierung; vom Kurator als collection-level "
            "front matter bestätigt."
        ),
        "object_type": "Sammlungsbegleitmaterial",
        "page_object_type": "Sammlungsbegleitmaterialseite",
        "material_technique": "Papier",
        "dimensions": "",
        "date_created": "1989–1990",
        "collection_name": "Betriebszeitung des TRO - Der Trafo",
        "rights_usage_terms": "unknown",
        "metadata_license": "not supplied",
        "metadata_source": "local-source-curator-reviewed-no-md-record",
        "page_count": 2,
        "representation_count": 3,
        "status": "unsealed",
        "page_label": "Blatt",
    },
}


def configure_base() -> None:
    values = {
        "RUN_ID": RUN_ID,
        "FINAL": FINAL,
        "WORK": WORK,
        "RUN_ROOT": RUN_ROOT,
        "STAGE": STAGE,
        "ROLLBACK": ROLLBACK,
        "STATE": RUN_ROOT / "state.json",
        "PLAN": RUN_ROOT / "addition-plan.json",
        "BASELINE": RUN_ROOT / "baseline.json",
        "NEW_FILES": RUN_ROOT / "new-files.json",
        "VALIDATION": RUN_ROOT / "staged-validation.json",
        "PUBLICATION": RUN_ROOT / "publication.json",
        "FINAL_VALIDATION": RUN_ROOT / "final-validation.json",
        "BASE_OBJECTS": BASE_OBJECTS,
        "BASE_REPRESENTATIONS": BASE_REPRESENTATIONS,
        "BASE_TIFFS": BASE_TIFFS,
        "BASE_ACCESS_PDFS": BASE_ACCESS_PDFS,
        "EXPECTED_ISSUES": ADDED_OBJECTS,
        "EXPECTED_TIFFS": ADDED_TIFFS,
        "ADDED_REPRESENTATIONS": ADDED_REPRESENTATIONS,
    }
    for key, value in values.items():
        setattr(base, key, value)


configure_base()
AdditionError = base.AdditionError


def source_rows() -> list[dict[str, Any]]:
    frozen = {
        row["path"]: row
        for row in json.loads(
            (DRY_RUN / "source-identities.json").read_text(encoding="utf-8")
        )
    }
    rows: list[dict[str, Any]] = []
    for inventory, paths in ((ISSUE, ISSUE_TIFFS), (DECK, DECK_TIFFS)):
        for sequence, path in enumerate(paths, 1):
            identity = frozen.get(str(path))
            if not identity or not path.is_file() or path.is_symlink():
                raise AdditionError(f"selected source unavailable: {path}")
            stat = path.stat()
            if (
                stat.st_dev != identity["device"]
                or stat.st_ino != identity["inode"]
                or stat.st_size != identity["size_bytes"]
                or stat.st_mtime_ns != identity["mtime_ns"]
            ):
                raise AdditionError(f"selected source identity changed: {path}")
            rows.append(
                {
                    "inventory": inventory,
                    "sequence": sequence,
                    "scan_count": len(paths),
                    "printed_page_label": str(sequence),
                    "source_path": str(path),
                    "source_size_bytes": stat.st_size,
                }
            )
    if len(rows) != ADDED_TIFFS:
        raise AdditionError("selected TIFF source count differs")
    return rows


def assert_source_read_only() -> None:
    output = base.run(
        ["findmnt", "-no", "TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE_ROOT)]
    )
    fields = output.strip().split(maxsplit=2)
    if (
        len(fields) != 3
        or "ro" not in fields[2].split(",")
        or "rw" in fields[2].split(",")
    ):
        raise AdditionError(f"source USB is not read-only: {output.strip()}")


def preflight() -> dict[str, Any]:
    assert_source_read_only()
    if not FINAL.is_dir() or FINAL.is_symlink():
        raise AdditionError("final Trafo unit is unavailable")
    if not base.copyhash_runner.helper_available(base.COPYHASH):
        raise AdditionError("copy/hash helper is unavailable")
    for tool in ("exiftool", "magick", "pdfinfo", "findmnt"):
        if shutil.which(tool) is None:
            raise AdditionError(f"required tool unavailable: {tool}")
    validation = json.loads((DRY_RUN / "validation.json").read_text())
    if validation.get("status") != "ok":
        raise AdditionError("corrected dry-run validation is unavailable")
    rows = source_rows()
    deck_identity = {
        row["path"]: row
        for row in json.loads(
            (DRY_RUN / "source-identities.json").read_text(encoding="utf-8")
        )
    }.get(str(DECK_PDF))
    if not deck_identity or not DECK_PDF.is_file() or DECK_PDF.is_symlink():
        raise AdditionError("selected Deckblatt PDF unavailable")
    deck_stat = DECK_PDF.stat()
    if (
        deck_stat.st_dev != deck_identity["device"]
        or deck_stat.st_ino != deck_identity["inode"]
        or deck_stat.st_size != deck_identity["size_bytes"]
        or deck_stat.st_mtime_ns != deck_identity["mtime_ns"]
        or base.pdf_pages(DECK_PDF) != 2
    ):
        raise AdditionError("selected Deckblatt PDF identity changed")
    catalog = base.read_csv(FINAL / "CATALOG.csv")
    representations = base.read_csv(
        FINAL / "db-restore/manifests/representations.csv"
    )
    if (len(catalog), len(representations)) != (
        BASE_OBJECTS,
        BASE_REPRESENTATIONS,
    ):
        raise AdditionError("accepted package baseline counts differ")
    if {ISSUE, DECK} & {row["inventory"] for row in catalog}:
        raise AdditionError("candidate inventory already exists")
    planned = planned_relative_paths()
    if any((FINAL / path).exists() for path in planned):
        raise AdditionError("one or more planned final paths already exist")
    free = shutil.disk_usage(FINAL).free
    source_bytes = sum(row["source_size_bytes"] for row in rows) + deck_stat.st_size
    if free < source_bytes * 3:
        raise AdditionError("insufficient free space for guarded staging")
    return {
        "checked_at": base.now(),
        "source_mount_read_only": True,
        "objects": ADDED_OBJECTS,
        "tiff_sources": ADDED_TIFFS,
        "pdf_sources": 1,
        "source_bytes": source_bytes,
        "base_objects": len(catalog),
        "base_representations": len(representations),
        "free_bytes": free,
    }


def planned_relative_paths() -> set[str]:
    paths = {
        f"masters/{ISSUE}/pages/{path.name}" for path in ISSUE_TIFFS
    }
    paths.update(f"masters/{DECK}/pages/{path.name}" for path in DECK_TIFFS)
    paths.add(f"export/access/{ISSUE}.pdf")
    paths.add(f"export/access/{DECK}.pdf")
    return paths


def build_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    deck_stat = DECK_PDF.stat()
    payload = {
        "format": "iss-trafo-reviewed-remnant-addition-v1",
        "run_id": RUN_ID,
        "unit_id": "trafo",
        "scope": [ISSUE, DECK],
        "mutation_policy": (
            "additive publication; existing accepted media paths are never rewritten"
        ),
        "curator_decisions": json.loads(
            (DRY_RUN / "decisions.json").read_text(encoding="utf-8")
        )["decisions"],
        "metadata_authority": (
            "curator decision, local source mastheads, and collection-front "
            "visual evidence; no matching museum-digital object"
        ),
        "rights_policy": "unknown; no neighboring-record inheritance",
        "metadata_license_policy": "not supplied",
        "source_identity": [
            {
                "inventory": row["inventory"],
                "sequence": row["sequence"],
                "source_path": row["source_path"],
                "source_size_bytes": row["source_size_bytes"],
            }
            for row in rows
        ]
        + [
            {
                "inventory": DECK,
                "role": "retained_access_pdf",
                "source_path": str(DECK_PDF),
                "source_size_bytes": deck_stat.st_size,
            }
        ],
        "access_pdf_policy": {
            ISSUE: "generated from four staged TIFF preservation pages",
            DECK: "retain curator-selected two-page Bibliocopy PDF",
        },
        "provenance_gaps": [
            "museum-digital-record-not-present",
            "rights-not-supplied",
            "metadata-license-not-supplied",
        ],
    }
    payload["reviewed_bundle_digest"] = base.sha256_bytes(
        base.canonical_json(payload)
    )
    return payload


def metadata_for(inventory: str) -> dict[str, Any]:
    return OBJECTS[inventory]


def page_tags(
    row: dict[str, Any],
    metadata: dict[str, Any],
    source_digest: str,
    reviewed_digest: str,
) -> dict[str, str]:
    sequence = int(row["sequence"])
    count = int(row["scan_count"])
    label = metadata["page_label"]
    # ExifTool applies date print conversion to XMP-dc:Date. Keep this
    # machine-readable field at the source-supported year so strict readback
    # is stable; the catalog, title, and description retain the fuller
    # "September 1957" and "1989–1990" statements.
    embedded_date = "1957" if row["inventory"] == ISSUE else "1989"
    tags = base.tiff_metadata.expected_tags(
        base.tiff_metadata.PROFILE_V2,
        {
            "entity_id": f"iss:trafo:{row['inventory']}:page:{sequence:03d}",
            "archival_label": row["inventory"],
            "descriptive_title": (
                f"{metadata['title']}; {label} {sequence} von {count}"
            ),
            "description": (
                f"{metadata['description']} Digitale Repräsentation: "
                f"{label} {sequence} von {count}."
            ),
            "object_type": metadata["page_object_type"],
            "material_technique": metadata["material_technique"],
            "dimensions": metadata["dimensions"],
            "date_created": embedded_date,
            "source_collection": metadata["collection_name"],
            "image_reference": (
                f"{Path(row['source_path']).name}; {label.casefold()} "
                f"{sequence} of {count}"
            ),
        },
        {
            "case_id": RUN_ID,
            "source_file_id": Path(row["source_path"]).name,
            "source_digest": source_digest,
            "reviewed_bundle_digest": reviewed_digest,
            "representation_role": "preservation_master",
            "provenance_gaps": [
                "museum-digital-record-not-present",
                "rights-not-supplied",
                "metadata-license-not-supplied",
            ],
        },
    )
    tags["XMP-dc:Relation"] += (
        f"; iss:carrier:{row['inventory']}; "
        f"iss:page:{sequence:03d}-of-{count:03d}"
    )
    tags["XMP-xmp:Label"] = PROFILE
    return tags


def stage_page(
    row: dict[str, Any], metadata: dict[str, Any], reviewed_digest: str
) -> dict[str, Any]:
    inventory = row["inventory"]
    sequence = int(row["sequence"])
    report_path = (
        STAGE
        / "db-restore/manifests/pages"
        / inventory
        / f"page-{sequence:03d}.json"
    )
    target = (
        STAGE
        / "masters"
        / inventory
        / "pages"
        / Path(row["source_path"]).name
    )
    if report_path.exists():
        report = base.load_json(report_path)
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size != report["size_bytes"]
            or report["source_size_bytes"] != row["source_size_bytes"]
        ):
            raise AdditionError(f"resumed page checkpoint differs: {target}")
        return report
    source = Path(row["source_path"])
    technical_source = base.tiff_accession._exif_values(
        source, base.tiff_accession.TECHNICAL_TAGS
    )
    bit_depth = base.tiff_accession._scalar_sample_depth(
        technical_source.get("IFD0:BitsPerSample")
    )
    if base.tiff_accession._page_count(source) != 1:
        raise AdditionError(f"TIFF is not one image directory: {source}")
    source_pixel = base.tiff_accession._decoded_pixel_digest(source, bit_depth)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_digest = base.copyhash_runner.copy_file(
        base.COPYHASH, source, target
    )
    tags = page_tags(row, metadata, source_digest, reviewed_digest)
    base.embed_in_place(target, tags)
    if base.tiff_accession._exif_values(target, tuple(tags)) != tags:
        raise AdditionError(f"embedded metadata readback differs: {target}")
    technical_target = base.tiff_accession._exif_values(
        target, base.tiff_accession.TECHNICAL_TAGS
    )
    if technical_target != technical_source:
        raise AdditionError(f"TIFF technical metadata changed: {target}")
    target_pixel = base.tiff_accession._decoded_pixel_digest(target, bit_depth)
    if target_pixel != source_pixel:
        raise AdditionError(f"decoded TIFF pixels changed: {target}")
    report = {
        "inventory": inventory,
        "sequence": sequence,
        "scan_count": int(row["scan_count"]),
        "printed_page_label": row["printed_page_label"],
        "source": str(source),
        "source_size_bytes": row["source_size_bytes"],
        "source_sha256": source_digest,
        "target": target.relative_to(STAGE).as_posix(),
        "target_sha256": base.sha256_file(target),
        "size_bytes": target.stat().st_size,
        "decoded_pixel_sha256": target_pixel,
        "embedding_profile": PROFILE,
        "embedded_metadata": tags,
        "technical_metadata": technical_target,
    }
    base.create_json(report_path, report)
    return report


def stage_generated_pdf(
    inventory: str, source_rows_for_issue: list[dict[str, Any]]
) -> dict[str, Any]:
    report_path = (
        STAGE / "db-restore/manifests/access-pdfs" / f"{inventory}.json"
    )
    target = STAGE / "export/access" / f"{inventory}.pdf"
    expected_pages = len(source_rows_for_issue)
    if report_path.exists():
        report = base.load_json(report_path)
        if (
            not target.is_file()
            or target.stat().st_size != report["size_bytes"]
            or base.pdf_pages(target) != expected_pages
        ):
            raise AdditionError(f"resumed PDF checkpoint differs: {target}")
        return report
    pages = [
        STAGE / "masters" / inventory / "pages" / Path(row["source_path"]).name
        for row in source_rows_for_issue
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.tmp.pdf")
    base.run(
        [
            "magick",
            *[str(path) for path in pages],
            "-strip",
            "-compress",
            "jpeg",
            "-quality",
            "85",
            str(temporary),
        ]
    )
    if base.pdf_pages(temporary) != expected_pages:
        raise AdditionError(f"generated PDF page count differs: {inventory}")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    report = {
        "inventory": inventory,
        "role": "generated_access_pdf",
        "source_representation_ids": [
            f"iss:trafo:{inventory}:page:{int(row['sequence']):03d}"
            for row in source_rows_for_issue
        ],
        "target": target.relative_to(STAGE).as_posix(),
        "target_sha256": base.sha256_file(target),
        "size_bytes": target.stat().st_size,
        "pdf_pages": expected_pages,
        "generation": "ImageMagick JPEG-compressed access derivative, quality 85",
    }
    base.create_json(report_path, report)
    return report


def stage_retained_pdf(inventory: str) -> dict[str, Any]:
    report_path = (
        STAGE / "db-restore/manifests/access-pdfs" / f"{inventory}.json"
    )
    target = STAGE / "export/access" / f"{inventory}.pdf"
    if report_path.exists():
        report = base.load_json(report_path)
        if (
            not target.is_file()
            or target.stat().st_size != report["size_bytes"]
            or base.pdf_pages(target) != 2
        ):
            raise AdditionError(f"resumed retained PDF differs: {target}")
        return report
    target.parent.mkdir(parents=True, exist_ok=True)
    source_digest = base.copyhash_runner.copy_file(
        base.COPYHASH, DECK_PDF, target
    )
    if base.pdf_pages(target) != 2:
        raise AdditionError("retained Deckblatt PDF is not two pages")
    report = {
        "inventory": inventory,
        "role": "retained_access_pdf",
        "source": str(DECK_PDF),
        "source_sha256": source_digest,
        "target": target.relative_to(STAGE).as_posix(),
        "target_sha256": source_digest,
        "size_bytes": target.stat().st_size,
        "pdf_pages": 2,
        "generation": "byte-identical retained curator-selected local PDF",
    }
    base.create_json(report_path, report)
    return report


def pdf_representation(report: dict[str, Any]) -> dict[str, Any]:
    inventory = report["inventory"]
    retained = report["role"] == "retained_access_pdf"
    return {
        "representation_id": f"iss:trafo:{inventory}:access:pdf",
        "inventory": inventory,
        "role": report["role"],
        "sequence": "",
        "relative_path": report["target"],
        "source_path": (
            report["source"]
            if retained
            else "generated from accessioned TIFF preservation pages"
        ),
        "source_sha256": report.get("source_sha256", ""),
        "content_sha256": report["target_sha256"],
        "size_bytes": report["size_bytes"],
        "decoded_pixel_sha256": "",
        "embedding_profile": (
            "retained-local-pdf"
            if retained
            else "imagemagick-access-pdf-jpeg-quality-85"
        ),
        "metadata_readback": "",
        "technical_readback": json.dumps(
            {"pdf_pages": report["pdf_pages"]}, sort_keys=True
        ),
    }


def catalog_row(inventory: str) -> dict[str, Any]:
    metadata = metadata_for(inventory)
    return {
        field: metadata[field]
        for field in base.CATALOG_FIELDS
    }


def metadata_source_record(
    inventory: str, rows: list[dict[str, Any]], reviewed_digest: str
) -> dict[str, Any]:
    metadata = metadata_for(inventory)
    return {
        "inventory": inventory,
        "metadata_source": metadata["metadata_source"],
        "accepted_metadata": {
            key: value
            for key, value in metadata.items()
            if key not in {"page_object_type", "page_label"}
        },
        "museum_digital": None,
        "local_catalog": None,
        "evidence": {
            "source_paths": [row["source_path"] for row in rows],
            "curator_decision": (
                "confirmed with TIFFs"
                if inventory == ISSUE
                else "confirmed collection-level front matter"
            ),
            "reviewed_bundle_digest": reviewed_digest,
        },
        "provenance_gaps": [
            "museum-digital-record-not-present",
            "rights-not-supplied",
            "metadata-license-not-supplied",
        ],
    }


def staged_readme() -> bytes:
    return """# Betriebszeitung TRO – Der Transformator

Unit ID: `trafo`

Status: unsealed accession package.

This filesystem-readable unit contains 1591 newspaper objects, one
collection-level front-matter object, 10383 metadata-enriched TIFF
preservation pages, four exact-PDF preservation fallbacks, and 1593 access
PDFs.

The 42 issues from 1988 were added from 294 locally held TIFF scans. They were
absent from both the accepted museum-digital capture and the local metadata
table. Their exact issue dates, issue numbers, and Jahrgang statements were
subsequently confirmed through curator review of the local issue mastheads.
Rights and metadata licence remain explicitly unresolved.

The reviewed remnant addition adds `TRO-1957-037-02` from four TIFF
preservation pages plus a generated access PDF, and `TRO-1989-Deckblatt` as
two-page collection-level front matter from two TIFF preservation pages and
the retained two-page local PDF. Neither object has a matching
museum-digital record. Rights and metadata licence remain unresolved.

Three earlier TIFF source pages were confirmed as genuine source losses during
the original accession and are represented by their exact issue PDFs. The
damaged byte streams are not retained in this final unit. Those loss decisions
and original intake identities remain recorded in
`documents/source-losses.json`.

TIFF metadata readback, technical structure, decoded pixels, and staged
content hashes were verified before publication. Existing accepted media were
not rewritten or rehashed by additive accessions.

Restore state and complete fixity information are under `db-restore/`.
""".encode("utf-8")


def stage() -> dict[str, Any]:
    preflight_result = preflight()
    rows = source_rows()
    plan = build_plan(rows)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    if base.PLAN.exists():
        if base.load_json(base.PLAN) != plan:
            raise AdditionError("existing addition plan differs")
    else:
        base.create_json(base.PLAN, plan)
    if base.BASELINE.exists():
        baseline = base.load_json(base.BASELINE)
    else:
        baseline = base.baseline_snapshot()
        base.create_json(base.BASELINE, baseline)
    base.replace_json(
        base.STATE,
        {
            "run_id": RUN_ID,
            "phase": "staging",
            "updated_at": base.now(),
            "preflight": preflight_result,
        },
    )
    added_objects: list[dict[str, Any]] = []
    added_representations: list[dict[str, Any]] = []
    for inventory in (ISSUE, DECK):
        metadata = metadata_for(inventory)
        object_rows = [row for row in rows if row["inventory"] == inventory]
        for row in object_rows:
            report = stage_page(
                row, metadata, plan["reviewed_bundle_digest"]
            )
            added_representations.append(base.page_representation(report))
        pdf_report = (
            stage_generated_pdf(inventory, object_rows)
            if inventory == ISSUE
            else stage_retained_pdf(inventory)
        )
        added_representations.append(pdf_representation(pdf_report))
        source_record = (
            STAGE / "documents/metadata-sources" / f"{inventory}.json"
        )
        if not source_record.exists():
            base.create_json(
                source_record,
                metadata_source_record(
                    inventory, object_rows, plan["reviewed_bundle_digest"]
                ),
            )
        added_objects.append(catalog_row(inventory))
        base.replace_json(
            base.STATE,
            {
                "run_id": RUN_ID,
                "phase": "staging",
                "updated_at": base.now(),
                "completed_objects": len(added_objects),
                "total_objects": ADDED_OBJECTS,
                "current_inventory": inventory,
            },
        )
        print(f"staged {inventory}", flush=True)
    if (
        len(added_objects) != ADDED_OBJECTS
        or len(added_representations) != ADDED_REPRESENTATIONS
    ):
        raise AdditionError("staged object/representation count differs")
    base.staged_readme = staged_readme
    base.build_metadata(added_objects, added_representations, plan)
    checksums = base.build_checksums()
    new_files = sorted(
        path.relative_to(STAGE).as_posix()
        for path in STAGE.rglob("*")
        if path.is_file()
        and path.relative_to(STAGE).as_posix() not in base.REPLACED_FILES
    )
    if len(new_files) != EXPECTED_NEW_FILES:
        raise AdditionError(f"new package file count differs: {len(new_files)}")
    if len(checksums) != EXPECTED_CHECKSUMS:
        raise AdditionError(f"combined checksum count differs: {len(checksums)}")
    base.replace_json(
        base.NEW_FILES,
        {"run_id": RUN_ID, "paths": new_files, "path_count": len(new_files)},
    )
    result = {
        "run_id": RUN_ID,
        "phase": "staged",
        "completed_at": base.now(),
        "objects": ADDED_OBJECTS,
        "tiff_pages": ADDED_TIFFS,
        "access_pdfs": ADDED_ACCESS_PDFS,
        "representations": ADDED_REPRESENTATIONS,
        "new_package_files": len(new_files),
        "combined_checksum_rows": len(checksums),
        "base_identity_digest": baseline["identity_digest"],
    }
    base.replace_json(base.STATE, result)
    return result


def validate_package_view(*, published: bool) -> dict[str, Any]:
    if (
        not base.PLAN.exists()
        or not base.BASELINE.exists()
        or not base.NEW_FILES.exists()
    ):
        raise AdditionError("stage checkpoint is incomplete")
    base.assert_base_unchanged()

    def view(relative: str) -> Path:
        staged = STAGE / relative
        return staged if staged.exists() else FINAL / relative

    catalog = base.read_csv(view("CATALOG.csv"))
    representations = base.read_csv(
        view("db-restore/manifests/representations.csv")
    )
    checksums = base.read_csv(view("db-restore/checksums.csv"))
    if (len(catalog), len(representations), len(checksums)) != (
        BASE_OBJECTS + ADDED_OBJECTS,
        BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS,
        EXPECTED_CHECKSUMS,
    ):
        raise AdditionError("combined package counts differ")
    added_catalog = [
        row for row in catalog if row["inventory"] in {ISSUE, DECK}
    ]
    if len(added_catalog) != ADDED_OBJECTS:
        raise AdditionError("candidate catalog coverage differs")
    for row in added_catalog:
        expected = OBJECTS[row["inventory"]]
        for field in base.CATALOG_FIELDS:
            if str(row[field]) != str(expected[field]):
                raise AdditionError(
                    f"candidate metadata differs: {row['inventory']} {field}"
                )
    checksum_map = {row["path"]: row for row in checksums}
    if len(checksum_map) != len(checksums):
        raise AdditionError("checksum paths are not unique")
    new_paths = base.load_json(base.NEW_FILES)["paths"]
    if len(new_paths) != EXPECTED_NEW_FILES:
        raise AdditionError("new-file checkpoint count differs")
    for relative in new_paths:
        path = view(relative)
        checksum = checksum_map.get(relative)
        if (
            not path.is_file()
            or path.is_symlink()
            or not checksum
            or path.stat().st_size != int(checksum["size_bytes"])
        ):
            raise AdditionError(f"new package file differs: {relative}")
    added_representations = [
        row for row in representations if row["inventory"] in {ISSUE, DECK}
    ]
    if len(added_representations) != ADDED_REPRESENTATIONS:
        raise AdditionError("candidate representation coverage differs")
    role_counts: dict[tuple[str, str], int] = {}
    for row in added_representations:
        key = (row["inventory"], row["role"])
        role_counts[key] = role_counts.get(key, 0) + 1
        path = view(row["relative_path"])
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != int(row["size_bytes"])
            or checksum_map[row["relative_path"]]["sha256"]
            != row["content_sha256"]
        ):
            raise AdditionError(
                f"candidate representation differs: {row['relative_path']}"
            )
        if row["role"] in {"generated_access_pdf", "retained_access_pdf"}:
            expected_pages = 4 if row["inventory"] == ISSUE else 2
            if base.pdf_pages(path) != expected_pages:
                raise AdditionError(f"candidate PDF page count differs: {path}")
    expected_roles = {
        (ISSUE, "preservation_master_page"): 4,
        (ISSUE, "generated_access_pdf"): 1,
        (DECK, "preservation_master_page"): 2,
        (DECK, "retained_access_pdf"): 1,
    }
    if role_counts != expected_roles:
        raise AdditionError(f"candidate role counts differ: {role_counts}")
    database = sqlite3.connect(
        f"file:{view('db-restore/RESTORE.sqlite')}?mode=ro", uri=True
    )
    try:
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        db_counts = (
            database.execute("SELECT count(*) FROM archive_object").fetchone()[0],
            database.execute("SELECT count(*) FROM representation").fetchone()[0],
        )
    finally:
        database.close()
    if integrity != "ok" or db_counts != (
        BASE_OBJECTS + ADDED_OBJECTS,
        BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS,
    ):
        raise AdditionError(f"restore database validation differs: {db_counts}")
    for relative in ("CATALOG.xlsx", "db-restore/RESTORE.xlsx"):
        with zipfile.ZipFile(view(relative)) as archive:
            if archive.testzip() is not None:
                raise AdditionError(f"XLSX integrity differs: {relative}")
    expected_paths = {
        path.relative_to(FINAL).as_posix()
        for path in FINAL.rglob("*")
        if path.is_file()
        and path.relative_to(FINAL).as_posix() != "db-restore/checksums.csv"
    }
    expected_paths.update(
        path.relative_to(STAGE).as_posix()
        for path in STAGE.rglob("*")
        if path.is_file()
        and path.relative_to(STAGE).as_posix() != "db-restore/checksums.csv"
    )
    if set(checksum_map) != expected_paths:
        raise AdditionError("combined checksum path coverage differs")
    return {
        "run_id": RUN_ID,
        "validated_at": base.now(),
        "published": published,
        "objects": len(catalog),
        "representations": len(representations),
        "tiff_pages": BASE_TIFFS + ADDED_TIFFS,
        "access_pdfs": BASE_ACCESS_PDFS + ADDED_ACCESS_PDFS,
        "added_objects": ADDED_OBJECTS,
        "added_representations": ADDED_REPRESENTATIONS,
        "new_package_files": len(new_paths),
        "checksum_rows": len(checksums),
        "sqlite_integrity": integrity,
        "base_media_identity": "unchanged",
        "new_file_sizes_and_manifest_digests": "consistent",
        "media_rehashed_during_validation": False,
    }


def validate_staged() -> dict[str, Any]:
    result = validate_package_view(published=False)
    base.replace_json(base.VALIDATION, result)
    base.replace_json(
        base.STATE,
        {
            "run_id": RUN_ID,
            "phase": "validated",
            "updated_at": base.now(),
            "validation": result,
        },
    )
    return result


def main() -> int:
    base.validate_package_view = validate_package_view
    base.staged_readme = staged_readme
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("preflight", "stage", "validate", "publish", "rollback", "status"),
    )
    action = parser.parse_args().action
    actions = {
        "preflight": preflight,
        "stage": stage,
        "validate": validate_staged,
        "publish": base.publish,
        "rollback": base.rollback,
        "status": base.status,
    }
    try:
        result = actions[action]()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
