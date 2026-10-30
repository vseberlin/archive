#!/usr/bin/env python3
"""Create one unsealed, metadata-enriched Trafo accession pilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


INVENTORY = "TRO-1949-001"
PILOT_ID = "trafo-pilot-003-TRO-1949-001"
UNIT_ID = PILOT_ID
ACTOR = "ISS archive bounded Trafo pilot"
PROFILE = "iss-tiff-v2+trafo-page-pilot-1"

WORK_SCOPE = Path("/mnt/volume/iss-archive-work")
FINAL_SCOPE = Path("/mnt/volume/iss-archive/units")
TRAFO_WORK = WORK_SCOPE / "trafo"
PILOT_WORK = TRAFO_WORK / "work" / "accession-pilots" / PILOT_ID
PACKAGE_WORK = PILOT_WORK / "package"
FINAL_UNIT = FINAL_SCOPE / PILOT_ID

FINAL_MANIFEST = (
    TRAFO_WORK
    / "work"
    / "finalization-plan"
    / "trafo-confirmed-reconciliation-001"
    / "final-manifest.csv"
)
MD_OBJECTS = (
    TRAFO_WORK
    / "work"
    / "reconciliation"
    / "museum-digital"
    / "runs"
    / "20260724T165726Z-trafo"
    / "objects.csv"
)
LOCAL_METADATA = Path(
    "/media/vladimir/Elements/restore/archive/WF/Trafo/2.Upload/"
    "TRO-Basisangaben_08_finalAG.csv"
)
TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
COPYHASH = TOOL_ROOT / "bin" / "archive-copyhash"

sys.path.insert(0, str(TOOL_ROOT))
import copyhash_runner  # noqa: E402
import paragon_document_bundle  # noqa: E402
import tiff_accession  # noqa: E402
import tiff_metadata  # noqa: E402


class PilotError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PilotError(f"refusing to replace existing file: {path}")
    path.write_bytes(
        (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PilotError(f"refusing to replace existing file: {path}")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def load_rows() -> tuple[list[dict[str, str]], dict[str, str], dict[str, str]]:
    with FINAL_MANIFEST.open(encoding="utf-8-sig", newline="") as handle:
        selected = [
            row for row in csv.DictReader(handle)
            if row["inventory"] == INVENTORY
        ]
    with MD_OBJECTS.open(encoding="utf-8-sig", newline="") as handle:
        md_matches = [
            row for row in csv.DictReader(handle)
            if row["inventory"] == INVENTORY
        ]
    with LOCAL_METADATA.open(encoding="cp1252", newline="") as handle:
        local_matches = [
            row for row in csv.DictReader(handle, delimiter=";")
            if row["inventory_number"] == INVENTORY
        ]
    if len(md_matches) != 1 or len(local_matches) != 1:
        raise PilotError("pilot requires one exact MD row and one exact local row")
    pages = sorted(
        (row for row in selected if row["role"] == "tiff_page"),
        key=lambda row: int(row["sequence"]),
    )
    pdfs = [row for row in selected if row["role"] == "local_pdf"]
    if len(pages) != 4 or len(pdfs) != 1:
        raise PilotError("pilot requires four TIFF pages and one retained local PDF")
    sequences = [int(row["sequence"]) for row in pages]
    if sequences != [1, 2, 3, 4]:
        raise PilotError(f"unexpected page sequence: {sequences}")
    return pages + pdfs, md_matches[0], local_matches[0]


def selected_path(row: dict[str, str]) -> Path:
    path = (Path(row["storage_root"]) / row["destination_path"]).resolve(
        strict=True
    )
    try:
        path.relative_to(WORK_SCOPE)
    except ValueError as error:
        raise PilotError(f"selected source is outside work scope: {path}") from error
    if not path.is_file() or path.is_symlink():
        raise PilotError(f"selected source is not one regular file: {path}")
    return path


def source_snapshot(
    rows: list[dict[str, str]],
    *,
    verify_digest: bool,
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        path = selected_path(row)
        stat = path.stat()
        observed_digest = sha256_file(path) if verify_digest else ""
        if stat.st_size != int(row["size_bytes"]):
            raise PilotError(f"selected source size changed: {path}")
        if observed_digest and observed_digest != row["sha256"]:
            raise PilotError(f"selected source digest changed: {path}")
        result.append(
            {
                "inventory": row["inventory"],
                "role": row["role"],
                "sequence": row["sequence"],
                "path": str(path),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": observed_digest or row["sha256"],
            }
        )
    return result


def plan_document(
    rows: list[dict[str, str]],
    md: dict[str, str],
    local: dict[str, str],
) -> dict[str, Any]:
    return {
        "format": "iss-trafo-accession-pilot-v1",
        "pilot_id": PILOT_ID,
        "unit_id": UNIT_ID,
        "status": "planned-unsealed-pilot",
        "created_at": utc_now(),
        "inventory": INVENTORY,
        "museum_digital_object_id": md["object_id"],
        "source_manifest": str(FINAL_MANIFEST),
        "source_manifest_sha256": sha256_file(FINAL_MANIFEST),
        "metadata_sources": {
            "museum_digital": str(MD_OBJECTS),
            "museum_digital_sha256": sha256_file(MD_OBJECTS),
            "local": str(LOCAL_METADATA),
            "local_sha256": sha256_file(LOCAL_METADATA),
        },
        "metadata": {
            "title": local["object_title"],
            "description": local["object_description"],
            "object_type": local["object_type"],
            "material_technique": local["object_material_technique"],
            "dimensions": local["object_dimensions"],
            "date_created": local["commissioned_when1"],
            "creator": local["commissioned_who1"],
            "coverage": "; ".join(
                value
                for value in (
                    local["related_place1"],
                    local["commissioned_where1"],
                )
                if value
            ),
            "contributor": local["related_actor1"],
            "collection": local["collection_name1"],
            "rights_holder": local["rights_object"],
            "rights_usage_terms": local["rights_of_use"],
            "metadata_license": local["rights_metadata_license"],
            "keywords": [
                local.get(f"tag{index}", "")
                for index in range(1, 7)
                if local.get(f"tag{index}", "")
            ],
        },
        "selected_representations": [
            {
                "role": row["role"],
                "sequence": row["sequence"],
                "path": str(selected_path(row)),
                "sha256": row["sha256"],
                "size_bytes": int(row["size_bytes"]),
            }
            for row in rows
        ],
        "embedding_profile": PROFILE,
        "work_output": str(PILOT_WORK),
        "final_output": str(FINAL_UNIT),
        "rules": {
            "source_unchanged": True,
            "fresh_outputs_only": True,
            "page_tiffs_metadata_enriched": True,
            "retained_pdf_exact_bytes": True,
            "pilot_unsealed": True,
        },
    }


def page_tags(
    *,
    plan_digest: str,
    source_row: dict[str, str],
    md: dict[str, str],
    local: dict[str, str],
    page_number: int,
    page_count: int,
) -> dict[str, str]:
    title = f"{local['object_title']}; Seite {page_number} von {page_count}"
    description = (
        f"{local['object_description']} "
        f"Digitale Seitenrepräsentation: Seite {page_number} von {page_count}."
    )
    metadata = {
        "entity_id": (
            f"iss:trafo:{INVENTORY}:page:{page_number:03d}"
        ),
        "archival_label": INVENTORY,
        "descriptive_title": title,
        "description": description,
        "object_type": "Zeitungsseite",
        "material_technique": local["object_material_technique"],
        "dimensions": local["object_dimensions"],
        "creator": local["commissioned_who1"],
        "date_created": INVENTORY.split("-")[1],
        "rights_statement": local["rights_of_use"],
        "rights_holder": local["rights_object"],
        "rights_usage_terms": local["rights_of_use"],
        "credit_line": local["image_owner1"],
        "keywords": ", ".join(
            local.get(f"tag{index}", "")
            for index in range(1, 7)
            if local.get(f"tag{index}", "")
        ),
        "coverage": "; ".join(
            value
            for value in (
                local["related_place1"],
                local["commissioned_where1"],
            )
            if value
        ),
        "contributor": local["related_actor1"],
        "source_collection": local["collection_name1"],
        "image_reference": (
            f"{Path(source_row['destination_path']).name}; "
            f"page {page_number} of {page_count}"
        ),
    }
    provenance = {
        "case_id": PILOT_ID,
        "source_file_id": Path(source_row["destination_path"]).name,
        "source_digest": source_row["sha256"],
        "reviewed_bundle_digest": plan_digest,
        "museum_digital_id": md["object_id"],
        "representation_role": "preservation_master",
        "provenance_gaps": [],
    }
    tags = tiff_metadata.expected_tags(
        tiff_metadata.PROFILE_V2,
        metadata,
        provenance,
    )
    tags["XMP-dc:Relation"] += (
        f"; iss:carrier:{INVENTORY}; "
        f"iss:page:{page_number:03d}-of-{page_count:03d}; "
        f"iss:publication-date:{local['commissioned_when1']}"
    )
    tags["XMP-xmp:Label"] = PROFILE
    return tags


def write_xlsx(
    path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    sheet_name: str,
) -> None:
    if path.exists():
        raise PilotError(f"refusing to replace existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = paragon_document_bundle.table_xlsx_bytes(
        fields,
        [
            {field: str(row.get(field, "")) for field in fields}
            for row in rows
        ],
        sheet_name=sheet_name,
        table_name="PilotTable",
    )
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def create_restore_database(
    path: Path,
    object_row: dict[str, Any],
    representations: list[dict[str, Any]],
) -> None:
    if path.exists():
        raise PilotError(f"refusing to replace existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE unit (
                unit_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                pilot INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE archive_object (
                inventory TEXT PRIMARY KEY,
                museum_digital_object_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                object_type TEXT NOT NULL,
                material_technique TEXT NOT NULL,
                dimensions TEXT NOT NULL,
                date_created TEXT NOT NULL,
                collection_name TEXT NOT NULL,
                rights_usage_terms TEXT NOT NULL,
                metadata_license TEXT NOT NULL
            );
            CREATE TABLE representation (
                representation_id TEXT PRIMARY KEY,
                inventory TEXT NOT NULL REFERENCES archive_object(inventory),
                role TEXT NOT NULL,
                sequence INTEGER,
                relative_path TEXT NOT NULL UNIQUE,
                source_path TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                decoded_pixel_sha256 TEXT NOT NULL,
                embedding_profile TEXT NOT NULL,
                metadata_readback TEXT NOT NULL,
                technical_readback TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO unit VALUES (?, 'unsealed-pilot', 1, ?)",
            (UNIT_ID, utc_now()),
        )
        connection.execute(
            """INSERT INTO archive_object VALUES (
                   :inventory, :museum_digital_object_id, :title, :description,
                   :object_type, :material_technique, :dimensions,
                   :date_created, :collection_name, :rights_usage_terms,
                   :metadata_license
               )""",
            object_row,
        )
        for row in representations:
            connection.execute(
                """INSERT INTO representation VALUES (
                       :representation_id, :inventory, :role, :sequence,
                       :relative_path, :source_path, :source_sha256,
                       :content_sha256, :size_bytes, :decoded_pixel_sha256,
                       :embedding_profile, :metadata_readback,
                       :technical_readback
                   )""",
                row,
            )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PilotError("restore database integrity check failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    sql_path = path.with_suffix(".sql")
    if sql_path.exists():
        raise PilotError(f"refusing to replace existing file: {sql_path}")
    sql_path.write_text(sql, encoding="utf-8")


def build_package(
    rows: list[dict[str, str]],
    md: dict[str, str],
    local: dict[str, str],
    plan: dict[str, Any],
    plan_digest: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page_rows = [row for row in rows if row["role"] == "tiff_page"]
    pdf_row = next(row for row in rows if row["role"] == "local_pdf")
    representations: list[dict[str, Any]] = []
    page_reports = []

    for page_number, source_row in enumerate(page_rows, start=1):
        source = selected_path(source_row)
        target = (
            PACKAGE_WORK
            / "masters"
            / INVENTORY
            / "pages"
            / Path(source_row["destination_path"]).name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise PilotError(f"refusing to replace pilot TIFF: {target}")
        technical_source = tiff_accession._exif_values(
            source,
            tiff_accession.TECHNICAL_TAGS,
        )
        bit_depth = tiff_accession._scalar_sample_depth(
            technical_source.get("IFD0:BitsPerSample")
        )
        if tiff_accession._page_count(source) != 1:
            raise PilotError(f"source TIFF is not one image directory: {source}")
        tags = page_tags(
            plan_digest=plan_digest,
            source_row=source_row,
            md=md,
            local=local,
            page_number=page_number,
            page_count=len(page_rows),
        )
        tiff_accession._write_candidate(source, target, tags)
        readback = tiff_accession._exif_values(target, tuple(tags))
        if readback != tags:
            raise PilotError(f"embedded metadata readback differs: {target}")
        technical_target = tiff_accession._exif_values(
            target,
            tiff_accession.TECHNICAL_TAGS,
        )
        if technical_target != technical_source:
            raise PilotError(f"TIFF technical metadata changed: {target}")
        decoded_source = tiff_accession._decoded_pixel_digest(
            source,
            bit_depth,
        )
        decoded_target = tiff_accession._decoded_pixel_digest(
            target,
            bit_depth,
        )
        if decoded_target != decoded_source:
            raise PilotError(f"decoded pixels changed: {target}")
        relative = target.relative_to(PACKAGE_WORK).as_posix()
        report = {
            "inventory": INVENTORY,
            "page_number": page_number,
            "page_count": len(page_rows),
            "source": str(source),
            "source_sha256": source_row["sha256"],
            "target": relative,
            "target_sha256": sha256_file(target),
            "size_bytes": target.stat().st_size,
            "decoded_pixel_sha256": decoded_target,
            "embedding_profile": PROFILE,
            "embedded_metadata": tags,
            "technical_metadata": technical_target,
        }
        report_path = (
            PACKAGE_WORK
            / "db-restore"
            / "manifests"
            / f"{INVENTORY}-page-{page_number:03d}.json"
        )
        write_json(report_path, report)
        page_reports.append(report)
        representations.append(
            {
                "representation_id": (
                    f"iss:trafo:{INVENTORY}:page:{page_number:03d}"
                ),
                "inventory": INVENTORY,
                "role": "preservation_master_page",
                "sequence": page_number,
                "relative_path": relative,
                "source_path": str(source),
                "source_sha256": source_row["sha256"],
                "content_sha256": report["target_sha256"],
                "size_bytes": report["size_bytes"],
                "decoded_pixel_sha256": decoded_target,
                "embedding_profile": PROFILE,
                "metadata_readback": json.dumps(
                    tags,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "technical_readback": json.dumps(
                    technical_target,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )

    source_pdf = selected_path(pdf_row)
    pdf_target = (
        PACKAGE_WORK / "export" / "access" / f"{INVENTORY}.pdf"
    )
    pdf_target.parent.mkdir(parents=True, exist_ok=True)
    pdf_digest = copyhash_runner.copy_file(
        COPYHASH,
        source_pdf,
        pdf_target,
        pdf_row["sha256"],
    )
    pdfinfo = subprocess.run(
        ["pdfinfo", str(pdf_target)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if pdfinfo.returncode != 0 or "Pages:           4" not in pdfinfo.stdout:
        raise PilotError("retained PDF failed four-page structural validation")
    representations.append(
        {
            "representation_id": f"iss:trafo:{INVENTORY}:access:pdf",
            "inventory": INVENTORY,
            "role": "retained_access_pdf",
            "sequence": None,
            "relative_path": pdf_target.relative_to(PACKAGE_WORK).as_posix(),
            "source_path": str(source_pdf),
            "source_sha256": pdf_row["sha256"],
            "content_sha256": pdf_digest,
            "size_bytes": pdf_target.stat().st_size,
            "decoded_pixel_sha256": "",
            "embedding_profile": "none-exact-source-bytes",
            "metadata_readback": "",
            "technical_readback": json.dumps(
                {"pdf_pages": 4, "pdfinfo_status": 0},
                sort_keys=True,
            ),
        }
    )

    metadata_snapshot = {
        "pilot_id": PILOT_ID,
        "inventory": INVENTORY,
        "museum_digital": md,
        "local_catalog": local,
        "accepted_source": "local catalog corroborated by museum-digital",
        "metadata_license": local["rights_metadata_license"],
    }
    write_json(
        PACKAGE_WORK
        / "documents"
        / "metadata-sources"
        / f"{INVENTORY}.json",
        metadata_snapshot,
    )
    write_json(
        PACKAGE_WORK / "export" / "md" / "metadata" / f"{INVENTORY}.json",
        md,
    )

    object_row = {
        "inventory": INVENTORY,
        "museum_digital_object_id": md["object_id"],
        "title": local["object_title"],
        "description": local["object_description"],
        "object_type": local["object_type"],
        "material_technique": local["object_material_technique"],
        "dimensions": local["object_dimensions"],
        "date_created": local["commissioned_when1"],
        "collection_name": local["collection_name1"],
        "rights_usage_terms": local["rights_of_use"],
        "metadata_license": local["rights_metadata_license"],
        "page_count": len(page_rows),
        "representation_count": len(representations),
        "status": "unsealed-pilot",
    }
    object_fields = list(object_row)
    representation_fields = list(representations[0])
    write_csv(
        PACKAGE_WORK / "CATALOG.csv",
        [object_row],
        object_fields,
    )
    write_xlsx(
        PACKAGE_WORK / "CATALOG.xlsx",
        [object_row],
        object_fields,
        sheet_name="Trafo pilot",
    )
    write_csv(
        PACKAGE_WORK / "db-restore" / "RESTORE.csv",
        representations,
        representation_fields,
    )
    write_xlsx(
        PACKAGE_WORK / "db-restore" / "RESTORE.xlsx",
        representations,
        representation_fields,
        sheet_name="Representations",
    )
    write_csv(
        PACKAGE_WORK / "db-restore" / "manifests" / "representations.csv",
        representations,
        representation_fields,
    )
    create_restore_database(
        PACKAGE_WORK / "db-restore" / "RESTORE.sqlite",
        object_row,
        representations,
    )
    readme = f"""# Trafo accession pilot: {INVENTORY}

Status: unsealed pilot, ready for review.

This bounded package tests one normal four-page Trafo issue. The issue is the
carrier object. Each TIFF is one ordered page-level preservation
representation with embedded XMP/IPTC metadata. The retained ABBYY PDF is an
exact-byte access representation and was not rewritten.

- Pilot: `{PILOT_ID}`
- Museum-digital object: `{md['object_id']}`
- TIFF pages: `4`
- Retained access PDF: `1`
- Embedding profile: `{PROFILE}`
- Plan digest: `{plan_digest}`

The validated selected-copy sources remain unchanged. This pilot is not sealed,
published, or authorized for deletion or replacement of any source.
"""
    readme_path = PACKAGE_WORK / "README.md"
    if readme_path.exists():
        raise PilotError(f"refusing to replace existing file: {readme_path}")
    readme_path.write_text(readme, encoding="utf-8")
    write_json(
        PACKAGE_WORK / "db-restore" / "manifests" / "accession-plan.json",
        {**plan, "plan_digest": plan_digest},
    )
    return representations, page_reports


def write_checksums() -> list[dict[str, Any]]:
    checksums_path = PACKAGE_WORK / "db-restore" / "checksums.csv"
    rows = []
    for path in sorted(PACKAGE_WORK.rglob("*")):
        if path.is_file() and path != checksums_path:
            rows.append(
                {
                    "path": path.relative_to(PACKAGE_WORK).as_posix(),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    write_csv(
        checksums_path,
        rows,
        ["path", "sha256", "size_bytes"],
    )
    return rows


def copy_package_to_final(checksums: list[dict[str, Any]]) -> None:
    if FINAL_UNIT.exists():
        raise PilotError(f"final pilot unit already exists: {FINAL_UNIT}")
    FINAL_UNIT.mkdir(parents=True, exist_ok=False)
    checksum_by_path = {row["path"]: row["sha256"] for row in checksums}
    files = sorted(path for path in PACKAGE_WORK.rglob("*") if path.is_file())
    files.sort(
        key=lambda path: path.name == "checksums.csv"
    )
    for source in files:
        relative = source.relative_to(PACKAGE_WORK)
        destination = FINAL_UNIT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = checksum_by_path.get(relative.as_posix(), sha256_file(source))
        copied = copyhash_runner.copy_file(
            COPYHASH,
            source,
            destination,
            expected,
        )
        if copied != expected:
            raise PilotError(f"final copy digest differs: {relative}")


def validate_final(page_reports: list[dict[str, Any]]) -> dict[str, Any]:
    checksums = FINAL_UNIT / "db-restore" / "checksums.csv"
    with checksums.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    errors = []
    bytes_checked = 0
    for row in rows:
        path = FINAL_UNIT / row["path"]
        if not path.is_file():
            errors.append(f"missing: {row['path']}")
            continue
        observed = sha256_file(path)
        bytes_checked += path.stat().st_size
        if observed != row["sha256"]:
            errors.append(f"digest mismatch: {row['path']}")
        if path.is_symlink() or path.stat().st_nlink != 1:
            errors.append(f"link violation: {row['path']}")
    for report in page_reports:
        path = FINAL_UNIT / report["target"]
        expected_tags = report["embedded_metadata"]
        if tiff_accession._exif_values(path, tuple(expected_tags)) != expected_tags:
            errors.append(f"embedded metadata mismatch: {report['target']}")
        technical = tiff_accession._exif_values(
            path,
            tiff_accession.TECHNICAL_TAGS,
        )
        if technical != report["technical_metadata"]:
            errors.append(f"technical metadata mismatch: {report['target']}")
        bit_depth = tiff_accession._scalar_sample_depth(
            technical.get("IFD0:BitsPerSample")
        )
        if (
            tiff_accession._decoded_pixel_digest(path, bit_depth)
            != report["decoded_pixel_sha256"]
        ):
            errors.append(f"decoded pixel mismatch: {report['target']}")
    connection = sqlite3.connect(
        f"file:{FINAL_UNIT / 'db-restore' / 'RESTORE.sqlite'}"
        "?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        object_count = connection.execute(
            "SELECT count(*) FROM archive_object"
        ).fetchone()[0]
        representation_count = connection.execute(
            "SELECT count(*) FROM representation"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        errors.append("restore SQLite integrity failed")
    if object_count != 1 or representation_count != 5:
        errors.append("restore SQLite row counts differ")
    return {
        "format": "iss-trafo-accession-pilot-validation-v1",
        "pilot_id": PILOT_ID,
        "validated_at": utc_now(),
        "result": "ok" if not errors else "error",
        "errors": errors,
        "checksum_rows": len(rows),
        "bytes_checked": bytes_checked,
        "objects": object_count,
        "representations": representation_count,
        "embedded_tiff_pages_verified": len(page_reports),
        "restore_sqlite_integrity": integrity,
        "final_unit": str(FINAL_UNIT),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    if not copyhash_runner.helper_available(COPYHASH):
        raise PilotError(f"copy helper is unavailable: {COPYHASH}")
    rows, md, local = load_rows()
    plan = plan_document(rows, md, local)
    if arguments.dry_run:
        snapshot = source_snapshot(rows, verify_digest=True)
        print(
            json.dumps(
                {
                    "result": "ok",
                    "dry_run": True,
                    "pilot_id": PILOT_ID,
                    "inventory": INVENTORY,
                    "sources": snapshot,
                    "work_output_fresh": not PILOT_WORK.exists(),
                    "final_output_fresh": not FINAL_UNIT.exists(),
                    "work_output": str(PILOT_WORK),
                    "final_output": str(FINAL_UNIT),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    if PILOT_WORK.exists() or FINAL_UNIT.exists():
        raise PilotError("pilot requires fresh work and final output paths")
    before = source_snapshot(rows, verify_digest=True)
    PILOT_WORK.mkdir(parents=True, exist_ok=False)
    plan_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(plan)
    ).hexdigest()
    write_json(
        PILOT_WORK / "plan.json",
        {**plan, "plan_digest": plan_digest},
    )
    representations, page_reports = build_package(
        rows,
        md,
        local,
        plan,
        plan_digest,
    )
    checksums = write_checksums()
    copy_package_to_final(checksums)
    validation = validate_final(page_reports)
    after = source_snapshot(rows, verify_digest=True)
    if before != after:
        raise PilotError("selected source fingerprint changed during pilot")
    validation["sources_unchanged"] = True
    validation["representation_rows"] = len(representations)
    write_json(PILOT_WORK / "final-validation.json", validation)
    if validation["result"] != "ok":
        raise PilotError(
            "final pilot validation failed: "
            + "; ".join(validation["errors"])
        )
    print(json.dumps(validation, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        PilotError,
        copyhash_runner.CopyHashError,
        tiff_accession.TiffAccessionError,
        tiff_metadata.TiffMetadataError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
