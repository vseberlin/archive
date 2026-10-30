#!/usr/bin/env python3
"""Stage, validate, publish, or roll back the bounded Trafo 1988 addition."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_ID = "trafo-1988-addition-001"
UNIT_ID = "trafo"
PROFILE = "iss-tiff-v2+trafo-page-1"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
WORK = Path("/mnt/volume/iss-archive-work/trafo")
RUN_ROOT = WORK / "work" / "accession-runs" / RUN_ID
STAGE = RUN_ROOT / "package-addition"
ROLLBACK = RUN_ROOT / "rollback-metadata"
SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/Trafo_Bibliocopy-gesamt/"
    "Trafo tifs 1970-1990"
)
DRAIN = (
    WORK
    / "work/source-plan/plan-20260725T-final-human-overrides/"
    "drain-candidates.csv"
)
TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
COPYHASH = TOOL_ROOT / "bin/archive-copyhash"
STATE = RUN_ROOT / "state.json"
PLAN = RUN_ROOT / "addition-plan.json"
BASELINE = RUN_ROOT / "baseline.json"
NEW_FILES = RUN_ROOT / "new-files.json"
VALIDATION = RUN_ROOT / "staged-validation.json"
PUBLICATION = RUN_ROOT / "publication.json"
FINAL_VALIDATION = RUN_ROOT / "final-validation.json"

sys.path.insert(0, str(TOOL_ROOT))
import copyhash_runner  # noqa: E402
import paragon_document_bundle  # noqa: E402
import tiff_accession  # noqa: E402
import tiff_metadata  # noqa: E402


CATALOG_FIELDS = [
    "inventory",
    "museum_digital_object_id",
    "title",
    "description",
    "object_type",
    "material_technique",
    "dimensions",
    "date_created",
    "collection_name",
    "rights_usage_terms",
    "metadata_license",
    "metadata_source",
    "page_count",
    "representation_count",
    "status",
]
REPRESENTATION_FIELDS = [
    "representation_id",
    "inventory",
    "role",
    "sequence",
    "relative_path",
    "source_path",
    "source_sha256",
    "content_sha256",
    "size_bytes",
    "decoded_pixel_sha256",
    "embedding_profile",
    "metadata_readback",
    "technical_readback",
]
REPLACED_FILES = [
    "CATALOG.csv",
    "CATALOG.xlsx",
    "README.md",
    "db-restore/RESTORE.csv",
    "db-restore/RESTORE.xlsx",
    "db-restore/RESTORE.sql",
    "db-restore/RESTORE.sqlite",
    "db-restore/manifests/representations.csv",
    "db-restore/checksums.csv",
]
EXPECTED_SUFFIXES = [
    "001",
    "002",
    "003",
    "004-005",
    "006",
    "007",
    "008",
]
EXPECTED_ISSUES = 42
EXPECTED_TIFFS = EXPECTED_ISSUES * len(EXPECTED_SUFFIXES)
BASE_OBJECTS = 1548
BASE_REPRESENTATIONS = 11636
BASE_TIFFS = 10083
BASE_ACCESS_PDFS = 1549
ADDED_REPRESENTATIONS = EXPECTED_TIFFS + EXPECTED_ISSUES


class AdditionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def replace_json(path: Path, value: Any) -> None:
    replace_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
    )


def create_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def create_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_xlsx(
    path: Path,
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    sheet: str,
    table: str,
) -> None:
    payload = paragon_document_bundle.table_xlsx_bytes(
        fields,
        [{field: str(row.get(field, "")) for field in fields} for row in rows],
        sheet_name=sheet,
        table_name=table,
    )
    replace_bytes(path, payload)


def run(arguments: list[str]) -> str:
    result = subprocess.run(
        arguments,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AdditionError(
            f"{arguments[0]} failed ({result.returncode}): {result.stdout.strip()}"
        )
    return result.stdout


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pdf_pages(path: Path) -> int:
    output = run(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise AdditionError(f"cannot read PDF page count: {path}")
    return int(match.group(1))


def source_rows() -> list[dict[str, Any]]:
    drain = {
        row["path"]: row
        for row in read_csv(DRAIN)
        if re.search(r"/TRO-1988-\d{3}/", row["path"])
    }
    rows: list[dict[str, Any]] = []
    for issue in range(1, EXPECTED_ISSUES + 1):
        inventory = f"TRO-1988-{issue:03d}"
        directory = SOURCE_ROOT / inventory
        observed = sorted(
            path for path in directory.iterdir() if path.is_file() and not path.is_symlink()
        )
        expected = [
            directory / f"{inventory}_{suffix}.tif" for suffix in EXPECTED_SUFFIXES
        ]
        if observed != expected:
            raise AdditionError(f"1988 issue file shape differs: {inventory}")
        for sequence, (path, label) in enumerate(
            zip(expected, EXPECTED_SUFFIXES, strict=True), 1
        ):
            drain_row = drain.get(str(path))
            if not drain_row:
                raise AdditionError(f"source is absent from drain inventory: {path}")
            size = path.stat().st_size
            if (
                drain_row["candidate_class"] != "unselected_tiff_candidate"
                or drain_row["delete_authorized"] != "no"
                or int(drain_row["size_bytes"]) != size
            ):
                raise AdditionError(f"drain evidence differs: {path}")
            rows.append(
                {
                    "inventory": inventory,
                    "issue_number": issue,
                    "sequence": sequence,
                    "printed_page_label": label.lstrip("0").replace("-00", "-"),
                    "source_path": str(path),
                    "source_size_bytes": size,
                }
            )
    if len(rows) != EXPECTED_TIFFS or len(drain) != EXPECTED_TIFFS:
        raise AdditionError("1988 drain/source coverage differs")
    return rows


def assert_source_read_only() -> None:
    output = run(["findmnt", "-no", "TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE_ROOT)])
    fields = output.strip().split(maxsplit=2)
    if len(fields) != 3 or "ro" not in fields[2].split(","):
        raise AdditionError(f"source root is not on a read-only mount: {output.strip()}")


def preflight() -> dict[str, Any]:
    assert_source_read_only()
    if not FINAL.is_dir() or FINAL.is_symlink():
        raise AdditionError("final Trafo unit is unavailable")
    if not copyhash_runner.helper_available(COPYHASH):
        raise AdditionError("copy/hash helper is unavailable")
    for tool in ("exiftool", "magick", "pdfinfo", "findmnt"):
        if shutil.which(tool) is None:
            raise AdditionError(f"required tool is unavailable: {tool}")
    rows = source_rows()
    catalog = read_csv(FINAL / "CATALOG.csv")
    representations = read_csv(FINAL / "db-restore/manifests/representations.csv")
    if len(catalog) != BASE_OBJECTS or len(representations) != BASE_REPRESENTATIONS:
        raise AdditionError("base catalog or representation count differs")
    if any(row["inventory"].startswith("TRO-1988-") for row in catalog):
        raise AdditionError("1988 is already present in the catalog")
    for row in rows:
        inventory = row["inventory"]
        source = Path(row["source_path"])
        final_master = FINAL / "masters" / inventory / "pages" / source.name
        final_pdf = FINAL / "export/access" / f"{inventory}.pdf"
        if final_master.exists() or final_pdf.exists():
            raise AdditionError(f"1988 final path already exists: {inventory}")
    usage = shutil.disk_usage(FINAL)
    source_bytes = sum(row["source_size_bytes"] for row in rows)
    if usage.free < source_bytes * 2:
        raise AdditionError("insufficient free space for staged TIFFs and access PDFs")
    return {
        "checked_at": now(),
        "source_mount_read_only": True,
        "issues": EXPECTED_ISSUES,
        "tiff_sources": len(rows),
        "source_bytes": source_bytes,
        "base_objects": len(catalog),
        "base_representations": len(representations),
        "free_bytes": usage.free,
    }


def build_plan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_identity = [
        {
            "inventory": row["inventory"],
            "sequence": row["sequence"],
            "printed_page_label": row["printed_page_label"],
            "source_path": row["source_path"],
            "source_size_bytes": row["source_size_bytes"],
        }
        for row in rows
    ]
    payload = {
        "format": "iss-trafo-addition-plan-v1",
        "run_id": RUN_ID,
        "unit_id": UNIT_ID,
        "scope": "TRO-1988-001 through TRO-1988-042 only",
        "mutation_policy": (
            "additive publication; existing accepted media paths are never rewritten"
        ),
        "metadata_authority": (
            "source filenames and visually sampled mastheads only; no museum-digital "
            "or local catalog record is present"
        ),
        "rights_policy": "unknown; no neighboring-record inheritance",
        "exact_issue_date_policy": "not reconciled; record year only",
        "access_pdf_policy": (
            "generated derivative from the seven accessioned TIFF scan files; "
            "the 004-005 scan remains one PDF page"
        ),
        "provenance_gaps": [
            "museum-digital-record-not-present",
            "local-catalog-record-not-present",
            "exact-publication-date-not-reconciled",
            "rights-not-supplied",
            "metadata-license-not-supplied",
        ],
        "source_identity": source_identity,
    }
    payload["reviewed_bundle_digest"] = sha256_bytes(canonical_json(payload))
    return payload


def baseline_snapshot() -> dict[str, Any]:
    rows = read_csv(FINAL / "db-restore/manifests/representations.csv")
    media = []
    for row in rows:
        path = FINAL / row["relative_path"]
        if not path.is_file() or path.is_symlink():
            raise AdditionError(f"base representation path unavailable: {path}")
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise AdditionError(f"base representation size differs: {path}")
        media.append(
            {
                "relative_path": row["relative_path"],
                "size_bytes": stat.st_size,
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "content_sha256": row["content_sha256"],
            }
        )
    return {
        "captured_at": now(),
        "representation_count": len(media),
        "identity_digest": sha256_bytes(canonical_json(media)),
        "media": media,
    }


def metadata_for(inventory: str, issue_number: int) -> dict[str, str]:
    return {
        "metadata_source": "local-source-minimal-no-md-record",
        "title": f"TRO-Betriebszeitung 'Der Trafo'; Nr. {issue_number}/1988",
        "description": (
            "Lokale Bibliocopy-Digitalisierung der Betriebszeitung 'Der Trafo'. "
            "Sieben TIFF-Scans; die gedruckten Seiten 4 und 5 liegen als "
            "gemeinsamer Scan vor. Kein zugehöriger Datensatz wurde im "
            "akzeptierten museum-digital-Abzug oder in der lokalen "
            "Metadaten-Tabelle gefunden."
        ),
        "object_type": "Zeitung",
        "material_technique": "",
        "dimensions": "",
        "date_created": "1988",
        "collection": "Betriebszeitung des TRO - Der Trafo",
        "rights_usage_terms": "unknown",
        "metadata_license": "not supplied",
        "inventory": inventory,
    }


def page_tags(
    row: dict[str, Any],
    metadata: dict[str, str],
    source_digest: str,
    reviewed_digest: str,
) -> dict[str, str]:
    sequence = row["sequence"]
    label = row["printed_page_label"]
    tags = tiff_metadata.expected_tags(
        tiff_metadata.PROFILE_V2,
        {
            "entity_id": f"iss:trafo:{row['inventory']}:page:{sequence:03d}",
            "archival_label": row["inventory"],
            "descriptive_title": (
                f"{metadata['title']}; Scan {sequence} von 7 "
                f"(gedruckte Seite(n) {label})"
            ),
            "description": (
                f"{metadata['description']} Digitale Seitenrepräsentation: "
                f"Scan {sequence} von 7; gedruckte Seite(n) {label}."
            ),
            "object_type": "Zeitungsseite",
            "date_created": "1988",
            "source_collection": metadata["collection"],
            "image_reference": (
                f"{Path(row['source_path']).name}; scan {sequence} of 7; "
                f"printed page label {label}"
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
                "local-catalog-record-not-present",
                "exact-publication-date-not-reconciled",
                "rights-not-supplied",
                "metadata-license-not-supplied",
            ],
        },
    )
    tags["XMP-dc:Relation"] += (
        f"; iss:carrier:{row['inventory']}; "
        f"iss:scan:{sequence:03d}-of-007; "
        f"iss:printed-page-label:{label}; iss:publication-year:1988"
    )
    tags["XMP-xmp:Label"] = PROFILE
    return tags


def embed_in_place(path: Path, tags: dict[str, str]) -> None:
    arguments = ["exiftool", "-overwrite_original"]
    arguments.extend(f"-{tag}={value}" for tag, value in tags.items())
    arguments.append(str(path))
    output = run(arguments)
    if "Warning:" in output:
        raise AdditionError(f"ExifTool warning for {path}: {output.strip()}")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def stage_page(
    row: dict[str, Any], metadata: dict[str, str], reviewed_digest: str
) -> dict[str, Any]:
    inventory = row["inventory"]
    sequence = row["sequence"]
    report_path = (
        STAGE
        / "db-restore/manifests/pages"
        / inventory
        / f"page-{sequence:03d}.json"
    )
    target = STAGE / "masters" / inventory / "pages" / Path(row["source_path"]).name
    if report_path.exists():
        report = load_json(report_path)
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size != report["size_bytes"]
            or report["source_size_bytes"] != row["source_size_bytes"]
        ):
            raise AdditionError(f"resumed page checkpoint differs: {target}")
        return report

    source = Path(row["source_path"])
    technical_source = tiff_accession._exif_values(
        source, tiff_accession.TECHNICAL_TAGS
    )
    bit_depth = tiff_accession._scalar_sample_depth(
        technical_source.get("IFD0:BitsPerSample")
    )
    if tiff_accession._page_count(source) != 1:
        raise AdditionError(f"TIFF is not one image directory: {source}")
    source_pixel = tiff_accession._decoded_pixel_digest(source, bit_depth)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_digest = copyhash_runner.copy_file(COPYHASH, source, target)
    tags = page_tags(row, metadata, source_digest, reviewed_digest)
    embed_in_place(target, tags)
    if tiff_accession._exif_values(target, tuple(tags)) != tags:
        raise AdditionError(f"embedded metadata readback differs: {target}")
    technical_target = tiff_accession._exif_values(
        target, tiff_accession.TECHNICAL_TAGS
    )
    if technical_target != technical_source:
        raise AdditionError(f"TIFF technical metadata changed: {target}")
    target_pixel = tiff_accession._decoded_pixel_digest(target, bit_depth)
    if target_pixel != source_pixel:
        raise AdditionError(f"decoded TIFF pixels changed: {target}")
    report = {
        "inventory": inventory,
        "sequence": sequence,
        "scan_count": 7,
        "printed_page_label": row["printed_page_label"],
        "source": str(source),
        "source_size_bytes": row["source_size_bytes"],
        "source_sha256": source_digest,
        "target": target.relative_to(STAGE).as_posix(),
        "target_sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "decoded_pixel_sha256": target_pixel,
        "embedding_profile": PROFILE,
        "embedded_metadata": tags,
        "technical_metadata": technical_target,
    }
    create_json(report_path, report)
    return report


def page_representation(report: dict[str, Any]) -> dict[str, Any]:
    inventory = report["inventory"]
    sequence = report["sequence"]
    return {
        "representation_id": f"iss:trafo:{inventory}:page:{sequence:03d}",
        "inventory": inventory,
        "role": "preservation_master_page",
        "sequence": sequence,
        "relative_path": report["target"],
        "source_path": report["source"],
        "source_sha256": report["source_sha256"],
        "content_sha256": report["target_sha256"],
        "size_bytes": report["size_bytes"],
        "decoded_pixel_sha256": report["decoded_pixel_sha256"],
        "embedding_profile": PROFILE,
        "metadata_readback": json.dumps(
            report["embedded_metadata"], ensure_ascii=False, sort_keys=True
        ),
        "technical_readback": json.dumps(
            report["technical_metadata"], ensure_ascii=False, sort_keys=True
        ),
    }


def stage_pdf(
    inventory: str, source_rows_for_issue: list[dict[str, Any]]
) -> dict[str, Any]:
    report_path = (
        STAGE / "db-restore/manifests/access-pdfs" / f"{inventory}.json"
    )
    target = STAGE / "export/access" / f"{inventory}.pdf"
    if report_path.exists():
        report = load_json(report_path)
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size != report["size_bytes"]
            or pdf_pages(target) != 7
        ):
            raise AdditionError(f"resumed PDF checkpoint differs: {target}")
        return report
    pages = [
        STAGE / "masters" / inventory / "pages" / Path(row["source_path"]).name
        for row in source_rows_for_issue
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.tmp.pdf")
    run(
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
    if pdf_pages(temporary) != 7:
        temporary.unlink(missing_ok=True)
        raise AdditionError(f"generated access PDF is not seven pages: {inventory}")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    report = {
        "inventory": inventory,
        "role": "generated_access_pdf",
        "source_representation_ids": [
            f"iss:trafo:{inventory}:page:{row['sequence']:03d}"
            for row in source_rows_for_issue
        ],
        "target": target.relative_to(STAGE).as_posix(),
        "target_sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "pdf_pages": 7,
        "generation": "ImageMagick JPEG-compressed access derivative, quality 85",
    }
    create_json(report_path, report)
    return report


def pdf_representation(report: dict[str, Any]) -> dict[str, Any]:
    inventory = report["inventory"]
    return {
        "representation_id": f"iss:trafo:{inventory}:access:pdf",
        "inventory": inventory,
        "role": "generated_access_pdf",
        "sequence": "",
        "relative_path": report["target"],
        "source_path": "generated from accessioned TIFF preservation pages",
        "source_sha256": "",
        "content_sha256": report["target_sha256"],
        "size_bytes": report["size_bytes"],
        "decoded_pixel_sha256": "",
        "embedding_profile": "imagemagick-access-pdf-jpeg-quality-85",
        "metadata_readback": "",
        "technical_readback": json.dumps({"pdf_pages": 7}, sort_keys=True),
    }


def catalog_row(inventory: str, issue_number: int) -> dict[str, Any]:
    metadata = metadata_for(inventory, issue_number)
    return {
        "inventory": inventory,
        "museum_digital_object_id": "",
        "title": metadata["title"],
        "description": metadata["description"],
        "object_type": metadata["object_type"],
        "material_technique": metadata["material_technique"],
        "dimensions": metadata["dimensions"],
        "date_created": metadata["date_created"],
        "collection_name": metadata["collection"],
        "rights_usage_terms": metadata["rights_usage_terms"],
        "metadata_license": metadata["metadata_license"],
        "metadata_source": metadata["metadata_source"],
        "page_count": 7,
        "representation_count": 8,
        "status": "unsealed",
    }


def metadata_source_record(
    inventory: str, issue_number: int, reviewed_digest: str
) -> dict[str, Any]:
    return {
        "inventory": inventory,
        "metadata_source": "local-source-minimal-no-md-record",
        "accepted_metadata": metadata_for(inventory, issue_number),
        "museum_digital": None,
        "local_catalog": None,
        "evidence": {
            "source_directory": str(SOURCE_ROOT / inventory),
            "filename_identity": inventory,
            "visual_sampling": (
                "Issue 001 and issue 042 mastheads were inspected before accession; "
                "the year and serial identity agreed with the source filenames."
            ),
            "exact_issue_date": "not reconciled",
            "reviewed_bundle_digest": reviewed_digest,
        },
        "provenance_gaps": [
            "museum-digital-record-not-present",
            "local-catalog-record-not-present",
            "exact-publication-date-not-reconciled",
            "rights-not-supplied",
            "metadata-license-not-supplied",
        ],
    }


def copy_database_with_additions(
    objects: list[dict[str, Any]], representations: list[dict[str, Any]]
) -> None:
    destination = STAGE / "db-restore/RESTORE.sqlite"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".RESTORE.sqlite.tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(
        f"file:{FINAL / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
        database_representations = [
            {**row, "sequence": None if row["sequence"] == "" else row["sequence"]}
            for row in representations
        ]
        target_connection.executemany(
            """
            INSERT INTO archive_object (
                inventory, museum_digital_object_id, title, description,
                object_type, material_technique, dimensions, date_created,
                collection_name, rights_usage_terms, metadata_license,
                metadata_source, page_count, representation_count, status
            ) VALUES (
                :inventory, :museum_digital_object_id, :title, :description,
                :object_type, :material_technique, :dimensions, :date_created,
                :collection_name, :rights_usage_terms, :metadata_license,
                :metadata_source, :page_count, :representation_count, :status
            )
            """,
            objects,
        )
        target_connection.executemany(
            """
            INSERT INTO representation (
                representation_id, inventory, role, sequence, relative_path,
                source_path, source_sha256, content_sha256, size_bytes,
                decoded_pixel_sha256, embedding_profile, metadata_readback,
                technical_readback
            ) VALUES (
                :representation_id, :inventory, :role, :sequence, :relative_path,
                :source_path, :source_sha256, :content_sha256, :size_bytes,
                :decoded_pixel_sha256, :embedding_profile, :metadata_readback,
                :technical_readback
            )
            """,
            database_representations,
        )
        target_connection.commit()
        if target_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AdditionError("staged restore database integrity failed")
        counts = (
            target_connection.execute("SELECT count(*) FROM archive_object").fetchone()[0],
            target_connection.execute("SELECT count(*) FROM representation").fetchone()[0],
        )
        if counts != (
            BASE_OBJECTS + EXPECTED_ISSUES,
            BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS,
        ):
            raise AdditionError(f"staged restore database counts differ: {counts}")
        sql = "\n".join(target_connection.iterdump()) + "\n"
    finally:
        source_connection.close()
        target_connection.close()
    os.replace(temporary, destination)
    replace_bytes(STAGE / "db-restore/RESTORE.sql", sql.encode())


def staged_readme() -> bytes:
    return f"""# Betriebszeitung TRO – Der Transformator

Unit ID: `trafo`

Status: unsealed accession package.

This filesystem-readable unit contains {BASE_OBJECTS + EXPECTED_ISSUES}
newspaper objects, {BASE_TIFFS + EXPECTED_TIFFS} metadata-enriched TIFF
preservation pages, four exact-PDF preservation fallbacks, and
{BASE_ACCESS_PDFS + EXPECTED_ISSUES} access PDFs.

The 42 issues from 1988 were added from 294 locally held TIFF scans. They were
absent from both the accepted museum-digital capture and the local metadata
table. Their catalog records therefore contain only minimal source-supported
metadata; exact issue dates, rights, and metadata license remain explicitly
unresolved. The 1988 access PDFs are generated derivatives from the seven TIFF
scan files per issue. The combined scan of printed pages 4 and 5 remains one
PDF page.

Three earlier TIFF source pages were confirmed as genuine source losses during
the original accession and are represented by their exact issue PDFs. The
damaged byte streams are not retained in this final unit. Those loss decisions
and original intake identities remain recorded in
`documents/source-losses.json`.

TIFF metadata readback, technical structure, decoded pixels, and the
post-enrichment hashes were verified during staging. Existing accepted media
were not rewritten or rehashed by the 1988 addition. Same-filesystem atomic
publication preserved the staged media bytes.

Restore state and complete fixity information are under `db-restore/`.
""".encode()


def build_metadata(
    added_objects: list[dict[str, Any]],
    added_representations: list[dict[str, Any]],
    plan: dict[str, Any],
) -> None:
    base_objects = read_csv(FINAL / "CATALOG.csv")
    base_representations = read_csv(
        FINAL / "db-restore/manifests/representations.csv"
    )
    objects = base_objects + added_objects
    representations = base_representations + added_representations
    write_csv(STAGE / "CATALOG.csv", objects, CATALOG_FIELDS)
    write_xlsx(
        STAGE / "CATALOG.xlsx",
        objects,
        CATALOG_FIELDS,
        sheet="Trafo",
        table="TrafoCatalog",
    )
    write_csv(
        STAGE / "db-restore/RESTORE.csv",
        representations,
        REPRESENTATION_FIELDS,
    )
    write_xlsx(
        STAGE / "db-restore/RESTORE.xlsx",
        representations,
        REPRESENTATION_FIELDS,
        sheet="Representations",
        table="TrafoRepresentations",
    )
    write_csv(
        STAGE / "db-restore/manifests/representations.csv",
        representations,
        REPRESENTATION_FIELDS,
    )
    copy_database_with_additions(added_objects, added_representations)
    replace_bytes(STAGE / "README.md", staged_readme())
    addition_manifest = {
        **plan,
        "completed_at": now(),
        "added_objects": len(added_objects),
        "added_tiff_pages": EXPECTED_TIFFS,
        "added_access_pdfs": EXPECTED_ISSUES,
        "added_representations": len(added_representations),
        "final_expected_counts": {
            "objects": BASE_OBJECTS + EXPECTED_ISSUES,
            "tiff_pages": BASE_TIFFS + EXPECTED_TIFFS,
            "access_pdfs": BASE_ACCESS_PDFS + EXPECTED_ISSUES,
            "representations": BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS,
        },
    }
    replace_json(
        STAGE / f"db-restore/manifests/additions/{RUN_ID}.json",
        addition_manifest,
    )


def build_checksums() -> list[dict[str, Any]]:
    old_rows = read_csv(FINAL / "db-restore/checksums.csv")
    replaced = set(REPLACED_FILES)
    stage_files = {
        path.relative_to(STAGE).as_posix(): path
        for path in STAGE.rglob("*")
        if path.is_file() and path.relative_to(STAGE).as_posix()
        != "db-restore/checksums.csv"
    }
    rows: list[dict[str, Any]] = [
        {
            "path": row["path"],
            "sha256": row["sha256"],
            "size_bytes": int(row["size_bytes"]),
        }
        for row in old_rows
        if row["path"] not in replaced
    ]
    page_reports = {
        report["target"]: report
        for report in (
            load_json(path)
            for path in sorted(
                (STAGE / "db-restore/manifests/pages").rglob("page-*.json")
            )
        )
    }
    pdf_reports = {
        report["target"]: report
        for report in (
            load_json(path)
            for path in sorted(
                (STAGE / "db-restore/manifests/access-pdfs").glob("*.json")
            )
        )
    }
    for relative, path in sorted(stage_files.items()):
        if relative in page_reports:
            digest = page_reports[relative]["target_sha256"]
        elif relative in pdf_reports:
            digest = pdf_reports[relative]["target_sha256"]
        else:
            digest = sha256_file(path)
        rows.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": path.stat().st_size,
            }
        )
    if len({row["path"] for row in rows}) != len(rows):
        raise AdditionError("combined checksum paths are not unique")
    rows.sort(key=lambda row: row["path"])
    write_csv(
        STAGE / "db-restore/checksums.csv",
        rows,
        ["path", "sha256", "size_bytes"],
    )
    return rows


def stage() -> dict[str, Any]:
    preflight_result = preflight()
    rows = source_rows()
    plan = build_plan(rows)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    if PLAN.exists():
        if load_json(PLAN) != plan:
            raise AdditionError("existing addition plan differs")
    else:
        create_json(PLAN, plan)
    if BASELINE.exists():
        baseline = load_json(BASELINE)
    else:
        baseline = baseline_snapshot()
        create_json(BASELINE, baseline)
    replace_json(
        STATE,
        {
            "run_id": RUN_ID,
            "phase": "staging",
            "updated_at": now(),
            "preflight": preflight_result,
        },
    )
    added_objects: list[dict[str, Any]] = []
    added_representations: list[dict[str, Any]] = []
    for issue in range(1, EXPECTED_ISSUES + 1):
        inventory = f"TRO-1988-{issue:03d}"
        metadata = metadata_for(inventory, issue)
        issue_rows = [row for row in rows if row["inventory"] == inventory]
        for row in issue_rows:
            report = stage_page(
                row, metadata, plan["reviewed_bundle_digest"]
            )
            added_representations.append(page_representation(report))
        pdf_report = stage_pdf(inventory, issue_rows)
        added_representations.append(pdf_representation(pdf_report))
        source_record = (
            STAGE / "documents/metadata-sources" / f"{inventory}.json"
        )
        if not source_record.exists():
            create_json(
                source_record,
                metadata_source_record(
                    inventory, issue, plan["reviewed_bundle_digest"]
                ),
            )
        added_objects.append(catalog_row(inventory, issue))
        replace_json(
            STATE,
            {
                "run_id": RUN_ID,
                "phase": "staging",
                "updated_at": now(),
                "completed_issues": issue,
                "total_issues": EXPECTED_ISSUES,
                "current_inventory": inventory,
            },
        )
        print(f"staged {inventory} ({issue}/{EXPECTED_ISSUES})", flush=True)
    if len(added_representations) != ADDED_REPRESENTATIONS:
        raise AdditionError("added representation count differs")
    build_metadata(added_objects, added_representations, plan)
    checksums = build_checksums()
    new_files = sorted(
        path.relative_to(STAGE).as_posix()
        for path in STAGE.rglob("*")
        if path.is_file() and path.relative_to(STAGE).as_posix() not in REPLACED_FILES
    )
    replace_json(
        NEW_FILES,
        {
            "run_id": RUN_ID,
            "paths": new_files,
            "path_count": len(new_files),
        },
    )
    result = {
        "run_id": RUN_ID,
        "phase": "staged",
        "completed_at": now(),
        "issues": len(added_objects),
        "tiff_pages": EXPECTED_TIFFS,
        "access_pdfs": EXPECTED_ISSUES,
        "representations": len(added_representations),
        "combined_checksum_rows": len(checksums),
        "base_identity_digest": baseline["identity_digest"],
    }
    replace_json(STATE, result)
    return result


def assert_base_unchanged() -> None:
    baseline = load_json(BASELINE)
    for row in baseline["media"]:
        path = FINAL / row["relative_path"]
        if not path.is_file() or path.is_symlink():
            raise AdditionError(f"base media path unavailable: {path}")
        stat = path.stat()
        if (
            stat.st_size != row["size_bytes"]
            or stat.st_dev != row["device"]
            or stat.st_ino != row["inode"]
        ):
            raise AdditionError(f"base media identity changed: {path}")


def validate_package_view(*, published: bool) -> dict[str, Any]:
    if not PLAN.exists() or not BASELINE.exists() or not NEW_FILES.exists():
        raise AdditionError("stage checkpoint is incomplete")
    assert_base_unchanged()

    def view(relative: str) -> Path:
        staged = STAGE / relative
        if staged.exists():
            return staged
        return FINAL / relative

    catalog = read_csv(view("CATALOG.csv"))
    representations = read_csv(
        view("db-restore/manifests/representations.csv")
    )
    checksums = read_csv(view("db-restore/checksums.csv"))
    if len(catalog) != BASE_OBJECTS + EXPECTED_ISSUES:
        raise AdditionError("combined catalog count differs")
    if len(representations) != BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS:
        raise AdditionError("combined representation count differs")
    added_catalog = [
        row for row in catalog if row["inventory"].startswith("TRO-1988-")
    ]
    if len(added_catalog) != EXPECTED_ISSUES:
        raise AdditionError("1988 catalog coverage differs")
    if any(
        row["museum_digital_object_id"]
        or row["date_created"] != "1988"
        or row["rights_usage_terms"] != "unknown"
        or row["metadata_license"] != "not supplied"
        for row in added_catalog
    ):
        raise AdditionError("1988 minimal metadata policy differs")
    checksum_map = {row["path"]: row for row in checksums}
    if len(checksum_map) != len(checksums):
        raise AdditionError("checksum paths are not unique")
    new_paths = load_json(NEW_FILES)["paths"]
    for relative in new_paths:
        path = view(relative)
        if not path.is_file() or path.is_symlink():
            raise AdditionError(f"new package file unavailable: {relative}")
        checksum = checksum_map.get(relative)
        if not checksum or path.stat().st_size != int(checksum["size_bytes"]):
            raise AdditionError(f"new package size/checksum record differs: {relative}")
    added_representations = [
        row
        for row in representations
        if row["inventory"].startswith("TRO-1988-")
    ]
    if len(added_representations) != ADDED_REPRESENTATIONS:
        raise AdditionError("1988 representation coverage differs")
    for row in added_representations:
        path = view(row["relative_path"])
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != int(row["size_bytes"])
            or checksum_map[row["relative_path"]]["sha256"]
            != row["content_sha256"]
        ):
            raise AdditionError(
                f"1988 representation record differs: {row['relative_path']}"
            )
        if row["role"] == "generated_access_pdf" and pdf_pages(path) != 7:
            raise AdditionError(f"1988 PDF page count differs: {path}")
    database = sqlite3.connect(
        f"file:{view('db-restore/RESTORE.sqlite')}?mode=ro", uri=True
    )
    try:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AdditionError("restore database integrity differs")
        db_counts = (
            database.execute("SELECT count(*) FROM archive_object").fetchone()[0],
            database.execute("SELECT count(*) FROM representation").fetchone()[0],
        )
    finally:
        database.close()
    expected_counts = (
        BASE_OBJECTS + EXPECTED_ISSUES,
        BASE_REPRESENTATIONS + ADDED_REPRESENTATIONS,
    )
    if db_counts != expected_counts:
        raise AdditionError(f"restore database counts differ: {db_counts}")
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
        missing = sorted(expected_paths - set(checksum_map))[:10]
        extra = sorted(set(checksum_map) - expected_paths)[:10]
        raise AdditionError(f"checksum path coverage differs: missing={missing}, extra={extra}")
    return {
        "run_id": RUN_ID,
        "validated_at": now(),
        "published": published,
        "objects": len(catalog),
        "representations": len(representations),
        "added_objects": len(added_catalog),
        "added_representations": len(added_representations),
        "checksum_rows": len(checksums),
        "sqlite_integrity": "ok",
        "base_media_identity": "unchanged",
        "new_file_sizes_and_manifest_digests": "consistent",
        "media_rehashed_during_validation": False,
    }


def validate_staged() -> dict[str, Any]:
    result = validate_package_view(published=False)
    replace_json(VALIDATION, result)
    replace_json(
        STATE,
        {
            "run_id": RUN_ID,
            "phase": "validated",
            "updated_at": now(),
            "validation": result,
        },
    )
    return result


def backup_metadata() -> None:
    ROLLBACK.mkdir(parents=True, exist_ok=True)
    for relative in REPLACED_FILES:
        source = FINAL / relative
        destination = ROLLBACK / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != sha256_file(source):
                raise AdditionError(f"rollback backup differs: {relative}")
        else:
            shutil.copy2(source, destination)
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())


def prune_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and parent != parent.parent:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def rollback() -> dict[str, Any]:
    if not ROLLBACK.is_dir():
        raise AdditionError("rollback metadata backup is unavailable")
    new_paths = load_json(NEW_FILES)["paths"]
    moved_back = 0
    for relative in reversed(new_paths):
        source = FINAL / relative
        destination = STAGE / relative
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise AdditionError(f"rollback stage target already exists: {relative}")
            os.rename(source, destination)
            prune_empty_parents(source, FINAL)
            moved_back += 1
    for relative in REPLACED_FILES:
        backup = ROLLBACK / relative
        if not backup.is_file():
            raise AdditionError(f"rollback metadata file unavailable: {relative}")
        destination = FINAL / relative
        temporary = destination.with_name(f".{destination.name}.rollback.tmp")
        shutil.copy2(backup, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    result = {
        "run_id": RUN_ID,
        "rolled_back_at": now(),
        "new_files_moved_back": moved_back,
        "metadata_restored": len(REPLACED_FILES),
        "base_media_rewritten": False,
    }
    replace_json(PUBLICATION, {"status": "rolled-back", **result})
    replace_json(STATE, {"phase": "rolled-back", **result})
    return result


def publish() -> dict[str, Any]:
    if not VALIDATION.exists():
        raise AdditionError("staged validation checkpoint is unavailable")
    if load_json(VALIDATION).get("base_media_identity") != "unchanged":
        raise AdditionError("staged validation checkpoint is not accepted")
    backup_metadata()
    new_paths = load_json(NEW_FILES)["paths"]
    replace_json(
        PUBLICATION,
        {
            "run_id": RUN_ID,
            "status": "publishing",
            "started_at": now(),
            "new_files_total": len(new_paths),
            "new_files_published": 0,
        },
    )
    try:
        for index, relative in enumerate(new_paths, 1):
            source = STAGE / relative
            destination = FINAL / relative
            if destination.exists():
                if source.exists():
                    raise AdditionError(f"publication path collision: {relative}")
                continue
            if not source.is_file() or source.is_symlink():
                raise AdditionError(f"staged publication file unavailable: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
            replace_json(
                PUBLICATION,
                {
                    "run_id": RUN_ID,
                    "status": "publishing",
                    "updated_at": now(),
                    "new_files_total": len(new_paths),
                    "new_files_published": index,
                    "last_path": relative,
                },
            )
        for relative in REPLACED_FILES:
            source = STAGE / relative
            destination = FINAL / relative
            if not source.is_file():
                raise AdditionError(f"staged replacement unavailable: {relative}")
            os.replace(source, destination)
        os.sync()
        result = validate_package_view(published=True)
        replace_json(FINAL_VALIDATION, result)
        publication = {
            "run_id": RUN_ID,
            "status": "published",
            "completed_at": now(),
            "new_files_published": len(new_paths),
            "metadata_replaced": len(REPLACED_FILES),
            "base_media_rewritten": False,
            "validation": result,
            "rollback_metadata": str(ROLLBACK),
        }
        replace_json(PUBLICATION, publication)
        replace_json(STATE, {"phase": "published", **publication})
        return publication
    except Exception:
        rollback()
        raise


def status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": RUN_ID,
        "run_root": str(RUN_ROOT),
        "stage_exists": STAGE.exists(),
        "final_exists": FINAL.exists(),
    }
    for name, path in (
        ("state", STATE),
        ("staged_validation", VALIDATION),
        ("publication", PUBLICATION),
        ("final_validation", FINAL_VALIDATION),
    ):
        result[name] = load_json(path) if path.exists() else None
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("preflight", "stage", "validate", "publish", "rollback", "status"),
    )
    arguments = parser.parse_args()
    actions = {
        "preflight": preflight,
        "stage": stage,
        "validate": validate_staged,
        "publish": publish,
        "rollback": rollback,
        "status": status,
    }
    try:
        result = actions[arguments.action]()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
