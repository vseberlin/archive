#!/usr/bin/env python3
"""Finalize the staged Trafo package after three curator-confirmed TIFF losses."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))
import trafo_accession_full as full  # noqa: E402


RUN_ROOT = full.RUN_ROOT
PACKAGE = full.PACKAGE
FINAL_STAGE = full.FINAL_STAGE
FINAL_UNIT = full.FINAL_UNIT
STATE = RUN_ROOT / "loss-resolution-001"
PASS_STATE = RUN_ROOT / "exception-pass-001"
PROGRESS = STATE / "progress.json"
FINAL_VALIDATION = RUN_ROOT / "final-validation.json"
LOSS_LEDGER = PACKAGE / "documents" / "source-losses.json"

LOSS_ROWS = {
    "TRO-1964-45|tiff_page|2|masters/TRO-1964-45/pages/TRO-1964-045_002.tif",
    "TRO-1970-021|tiff_page|7|masters/TRO-1970-021/pages/TRO-1970-021_008.tif",
    "TRO-1970-022|tiff_page|2|masters/TRO-1970-022/pages/TRO-1970-022_002.tif",
}


class FinalizeError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def row_key(row: dict[str, str]) -> str:
    return "|".join(
        (
            row["inventory"],
            row["role"],
            row["sequence"],
            row["destination_path"],
        )
    )


def write_progress(phase: str, **values: Any) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    full.replace_json(
        PROGRESS,
        {
            "run_id": full.RUN_ID,
            "resolution_id": STATE.name,
            "updated_at": now(),
            "phase": phase,
            **values,
        },
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest() -> tuple[
    list[dict[str, str]], dict[str, list[dict[str, str]]]
]:
    with full.MANIFEST.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["inventory"]].append(row)
    if len(rows) != 11634 or len(grouped) != 1548:
        raise FinalizeError("accepted manifest coverage differs")
    return rows, dict(grouped)


def load_confirmed_errors() -> dict[str, dict[str, Any]]:
    complete = read_json(PASS_STATE / "batch-complete.json")
    watchdog = read_json(PASS_STATE / "watchdog-success.json")
    if (
        not complete.get("batch_traversal_complete")
        or complete.get("exception_rows") != 3
        or watchdog.get("result") != "ok"
    ):
        raise FinalizeError("exception-tolerant batch is not the accepted checkpoint")
    errors: dict[str, dict[str, Any]] = {}
    with (PASS_STATE / "errors.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            errors[value["row_key"]] = value
    if set(errors) != LOSS_ROWS:
        raise FinalizeError("exception ledger differs from curator-confirmed losses")
    if any("File format error" not in value["detail"] for value in errors.values()):
        raise FinalizeError("one confirmed loss is not the expected format failure")
    return errors


def stored_pdf_digest(rows: list[dict[str, str]]) -> str:
    pdf_rows = [row for row in rows if row["role"] != "tiff_page"]
    if len(pdf_rows) != 1:
        raise FinalizeError("inventory PDF row shape differs")
    return pdf_rows[0]["sha256"]


def copy_and_hash_new_destination(
    source: Path,
    destination: Path,
    expected_digest: str,
) -> dict[str, Any]:
    receipt = STATE / "fallback-copy-receipts" / f"{destination.parent.name}.json"
    if receipt.exists():
        value = read_json(receipt)
        if (
            destination.is_file()
            and not destination.is_symlink()
            and destination.stat().st_size == value["size_bytes"]
            and value["sha256"] == expected_digest
        ):
            return value
        raise FinalizeError(f"fallback copy receipt differs: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        observed = full.sha256_file(destination)
        if observed != expected_digest:
            raise FinalizeError(f"existing fallback destination differs: {destination}")
    else:
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copy2(source, temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            observed = full.sha256_file(temporary)
            if observed != expected_digest:
                raise FinalizeError(f"fallback destination digest differs: {destination}")
            os.rename(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    value = {
        "copied_at": now(),
        "source": str(source),
        "destination": str(destination),
        "sha256": expected_digest,
        "size_bytes": destination.stat().st_size,
        "hash_rule": "destination-only against stored staging PDF hash",
    }
    full.write_json(receipt, value)
    return value


def resolve_losses(
    grouped: dict[str, list[dict[str, str]]],
    errors: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    decision_path = STATE / "decision.json"
    if decision_path.exists():
        decision_at = read_json(decision_path)["recorded_at"]
    else:
        decision_at = now()
        full.write_json(
            decision_path,
            {
                "decision": "genuine source loss with exact issue PDF fallback",
                "loss_rows": sorted(LOSS_ROWS),
                "damaged_byte_streams_retained_in_final": False,
                "recorded_at": decision_at,
            },
        )
    records: list[dict[str, Any]] = []
    stored: dict[str, str] = {}
    for key in sorted(LOSS_ROWS):
        error = errors[key]
        inventory = error["inventory"]
        rows = grouped[inventory]
        lost_row = next(row for row in rows if row_key(row) == key)
        lost_target = PACKAGE / lost_row["destination_path"]
        page_number = int(lost_row["sequence"])
        report = (
            PACKAGE
            / "db-restore"
            / "manifests"
            / "pages"
            / inventory
            / f"page-{page_number:03d}.json"
        )
        if lost_target.exists() or report.exists():
            raise FinalizeError(f"lost TIFF unexpectedly exists in package: {inventory}")
        access_pdf = PACKAGE / "export" / "access" / f"{inventory}.pdf"
        digest = stored_pdf_digest(rows)
        pdf_row = next(row for row in rows if row["role"] != "tiff_page")
        if (
            not access_pdf.is_file()
            or access_pdf.is_symlink()
            or access_pdf.stat().st_size != int(pdf_row["size_bytes"])
            or full.pdf_pages(access_pdf) != int(pdf_row["pdf_pages"])
        ):
            raise FinalizeError(f"validated access PDF differs: {inventory}")
        master_pdf = PACKAGE / "masters" / inventory / f"{inventory}.pdf"
        receipt = copy_and_hash_new_destination(access_pdf, master_pdf, digest)
        relative = master_pdf.relative_to(PACKAGE).as_posix()
        stored[relative] = digest
        records.append(
            {
                "inventory": inventory,
                "decision": "genuine_source_loss",
                "lost_role": "tiff_page",
                "lost_sequence": page_number,
                "lost_destination": lost_row["destination_path"],
                "selected_intake_path": error["source_path"],
                "selected_intake_sha256": error["source_sha256"],
                "selected_intake_size_bytes": int(lost_row["size_bytes"]),
                "failure": error["detail"],
                "damaged_byte_stream_retained_in_final": False,
                "fallback_role": "preservation_master_pdf_fallback_for_source_loss",
                "fallback_path": relative,
                "fallback_sha256": digest,
                "fallback_size_bytes": receipt["size_bytes"],
                "fallback_pdf_pages": int(pdf_row["pdf_pages"]),
                "fallback_access_path": access_pdf.relative_to(PACKAGE).as_posix(),
                "approved_at": decision_at,
            }
        )
    if LOSS_LEDGER.exists():
        existing = read_json(LOSS_LEDGER)
        if existing.get("losses") != records:
            raise FinalizeError("existing source-loss ledger differs")
    else:
        full.write_json(
            LOSS_LEDGER,
            {
                "format": "iss-trafo-source-loss-ledger-v1",
                "unit_id": full.UNIT_ID,
                "run_id": full.RUN_ID,
                "resolution_id": STATE.name,
                "decision": "record loss and use exact issue PDF fallback",
                "loss_count": len(records),
                "damaged_byte_streams_retained_in_final": False,
                "losses": records,
            },
        )
    return records, stored


def dry_run() -> dict[str, Any]:
    if not PACKAGE.is_dir() or FINAL_STAGE.exists() or FINAL_UNIT.exists():
        raise FinalizeError("staging/final path precondition differs")
    if PACKAGE.stat().st_dev != full.FINAL_SCOPE.stat().st_dev:
        raise FinalizeError("staging and final roots are not on one filesystem")
    _, grouped = load_manifest()
    errors = load_confirmed_errors()
    for key in sorted(LOSS_ROWS):
        error = errors[key]
        inventory = error["inventory"]
        rows = grouped[inventory]
        lost_row = next(row for row in rows if row_key(row) == key)
        page_number = int(lost_row["sequence"])
        if (PACKAGE / lost_row["destination_path"]).exists():
            raise FinalizeError(f"lost TIFF unexpectedly exists: {inventory}")
        report = (
            PACKAGE
            / "db-restore"
            / "manifests"
            / "pages"
            / inventory
            / f"page-{page_number:03d}.json"
        )
        if report.exists():
            raise FinalizeError(f"lost TIFF report unexpectedly exists: {inventory}")
        pdf_row = next(row for row in rows if row["role"] != "tiff_page")
        access_pdf = PACKAGE / "export" / "access" / f"{inventory}.pdf"
        if (
            not access_pdf.is_file()
            or access_pdf.stat().st_size != int(pdf_row["size_bytes"])
            or full.pdf_pages(access_pdf) != int(pdf_row["pdf_pages"])
        ):
            raise FinalizeError(f"loss fallback PDF differs: {inventory}")
    final_metadata = (
        PACKAGE / "CATALOG.csv",
        PACKAGE / "CATALOG.xlsx",
        PACKAGE / "README.md",
        PACKAGE / "db-restore" / "checksums.csv",
        PACKAGE / "db-restore" / "RESTORE.sqlite",
    )
    if any(path.exists() for path in final_metadata):
        raise FinalizeError("final package metadata unexpectedly exists")
    return {
        "result": "ok",
        "dry_run": True,
        "objects": len(grouped),
        "manifest_rows": sum(len(rows) for rows in grouped.values()),
        "confirmed_source_losses": len(errors),
        "staged_tiffs": sum(1 for _ in (PACKAGE / "masters").glob("*/pages/*.tif")),
        "page_reports": sum(
            1 for _ in (PACKAGE / "db-restore/manifests/pages").glob("*/*.json")
        ),
        "metadata_snapshots": sum(
            1 for _ in (PACKAGE / "documents/metadata-sources").glob("*.json")
        ),
        "access_pdfs_including_full_issue": sum(
            1 for _ in (PACKAGE / "export/access").glob("*.pdf")
        ),
        "same_filesystem_atomic_promotion": True,
        "media_rehash_planned": False,
        "usb_access_planned": False,
        "damaged_byte_streams_retained_in_final": False,
    }


def representation_from_page_report(
    inventory: str,
    page_number: int,
    row: dict[str, str],
    report: dict[str, Any],
) -> dict[str, Any]:
    target = PACKAGE / report["target"]
    if (
        report["inventory"] != inventory
        or report["page_number"] != page_number
        or report["source_sha256"] != row["sha256"]
        or not target.is_file()
        or target.is_symlink()
        or target.stat().st_size != report["size_bytes"]
        or report["embedding_profile"] != full.PROFILE
        or not report["embedded_metadata"]
        or not report["decoded_pixel_sha256"]
    ):
        raise FinalizeError(f"page report differs: {inventory}/{page_number}")
    return {
        "representation_id": f"iss:trafo:{inventory}:page:{page_number:03d}",
        "inventory": inventory,
        "role": "preservation_master_page",
        "sequence": page_number,
        "relative_path": report["target"],
        "source_path": report["source"],
        "source_sha256": report["source_sha256"],
        "content_sha256": report["target_sha256"],
        "size_bytes": report["size_bytes"],
        "decoded_pixel_sha256": report["decoded_pixel_sha256"],
        "embedding_profile": report["embedding_profile"],
        "metadata_readback": json.dumps(
            report["embedded_metadata"], ensure_ascii=False, sort_keys=True
        ),
        "technical_readback": json.dumps(
            report["technical_metadata"], ensure_ascii=False, sort_keys=True
        ),
    }


def collect_catalog(
    grouped: dict[str, list[dict[str, str]]],
    loss_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    loss_by_inventory = {row["inventory"]: row for row in loss_records}
    objects: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    stored: dict[str, str] = {}
    for inventory in sorted(grouped):
        rows = grouped[inventory]
        snapshot_path = (
            PACKAGE / "documents" / "metadata-sources" / f"{inventory}.json"
        )
        snapshot = read_json(snapshot_path)
        metadata = snapshot["accepted_metadata"]
        md_object_id = str(snapshot["museum_digital"]["object_id"])
        pages = sorted(
            [row for row in rows if row["role"] == "tiff_page"],
            key=lambda row: int(row["sequence"]),
        )
        issue: list[dict[str, Any]] = []
        for page_number, row in enumerate(pages, 1):
            key = row_key(row)
            if key in LOSS_ROWS:
                continue
            report_path = (
                PACKAGE
                / "db-restore"
                / "manifests"
                / "pages"
                / inventory
                / f"page-{page_number:03d}.json"
            )
            if not report_path.is_file():
                raise FinalizeError(f"missing page report: {inventory}/{page_number}")
            report = read_json(report_path)
            representation = representation_from_page_report(
                inventory, page_number, row, report
            )
            issue.append(representation)
            stored[representation["relative_path"]] = representation["content_sha256"]

        pdf_row = next(row for row in rows if row["role"] != "tiff_page")
        pdf_digest = pdf_row["sha256"]
        if pdf_row["role"] == "local_pdf_fallback":
            master_pdf = PACKAGE / "masters" / inventory / f"{inventory}.pdf"
            master_relative = master_pdf.relative_to(PACKAGE).as_posix()
            if not master_pdf.is_file() or master_pdf.stat().st_size != int(
                pdf_row["size_bytes"]
            ):
                raise FinalizeError(f"existing PDF fallback differs: {inventory}")
            issue.append(
                {
                    "representation_id": f"iss:trafo:{inventory}:master:pdf",
                    "inventory": inventory,
                    "role": "preservation_master_pdf_fallback",
                    "sequence": "",
                    "relative_path": master_relative,
                    "source_path": str(
                        Path(pdf_row["storage_root"]) / pdf_row["destination_path"]
                    ),
                    "source_sha256": pdf_digest,
                    "content_sha256": pdf_digest,
                    "size_bytes": master_pdf.stat().st_size,
                    "decoded_pixel_sha256": "",
                    "embedding_profile": "none-exact-source-bytes",
                    "metadata_readback": "",
                    "technical_readback": json.dumps(
                        {"pdf_pages": int(pdf_row["pdf_pages"])}, sort_keys=True
                    ),
                }
            )
            stored[master_relative] = pdf_digest
            access_name = f"{inventory}.pdf"
            access_role = "retained_access_pdf_from_fallback_master"
        else:
            access_name = (
                "TRO-1953-26-2-md.pdf"
                if inventory == "TRO-1953-26-2"
                else f"{inventory}.pdf"
            )
            access_role = "retained_access_pdf"

        if inventory in loss_by_inventory:
            loss = loss_by_inventory[inventory]
            master_pdf = PACKAGE / loss["fallback_path"]
            issue.append(
                {
                    "representation_id": (
                        f"iss:trafo:{inventory}:master:pdf-source-loss-fallback"
                    ),
                    "inventory": inventory,
                    "role": loss["fallback_role"],
                    "sequence": loss["lost_sequence"],
                    "relative_path": loss["fallback_path"],
                    "source_path": loss["fallback_access_path"],
                    "source_sha256": loss["fallback_sha256"],
                    "content_sha256": loss["fallback_sha256"],
                    "size_bytes": master_pdf.stat().st_size,
                    "decoded_pixel_sha256": "",
                    "embedding_profile": "none-exact-source-bytes",
                    "metadata_readback": "",
                    "technical_readback": json.dumps(
                        {
                            "pdf_pages": loss["fallback_pdf_pages"],
                            "genuine_source_loss": loss["lost_destination"],
                            "lost_source_sha256": loss["selected_intake_sha256"],
                        },
                        sort_keys=True,
                    ),
                }
            )
            stored[loss["fallback_path"]] = loss["fallback_sha256"]

        access_pdf = PACKAGE / "export" / "access" / access_name
        access_relative = access_pdf.relative_to(PACKAGE).as_posix()
        if (
            not access_pdf.is_file()
            or access_pdf.is_symlink()
            or access_pdf.stat().st_size != int(pdf_row["size_bytes"])
            or full.pdf_pages(access_pdf) != int(pdf_row["pdf_pages"])
        ):
            raise FinalizeError(f"access PDF differs: {inventory}")
        issue.append(
            {
                "representation_id": f"iss:trafo:{inventory}:access:pdf",
                "inventory": inventory,
                "role": access_role,
                "sequence": "",
                "relative_path": access_relative,
                "source_path": str(
                    Path(pdf_row["storage_root"]) / pdf_row["destination_path"]
                ),
                "source_sha256": pdf_digest,
                "content_sha256": pdf_digest,
                "size_bytes": access_pdf.stat().st_size,
                "decoded_pixel_sha256": "",
                "embedding_profile": "none-exact-source-bytes",
                "metadata_readback": "",
                "technical_readback": json.dumps(
                    {"pdf_pages": int(pdf_row["pdf_pages"])}, sort_keys=True
                ),
            }
        )
        stored[access_relative] = pdf_digest
        representations.extend(issue)
        objects.append(
            {
                "inventory": inventory,
                "museum_digital_object_id": md_object_id,
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
                "page_count": len(pages),
                "representation_count": len(issue),
                "status": "unsealed",
            }
        )

    full_pdf = PACKAGE / "export" / "access" / "TRO-1953-26-full.pdf"
    if full.pdf_pages(full_pdf) != 14:
        raise FinalizeError("combined TRO-1953-26 PDF differs")
    full_digest_receipt = STATE / "derived-full-pdf-hash.json"
    if full_digest_receipt.exists():
        full_digest = read_json(full_digest_receipt)["sha256"]
    else:
        full_digest = full.sha256_file(full_pdf)
        full.write_json(
            full_digest_receipt,
            {
                "path": str(full_pdf),
                "sha256": full_digest,
                "size_bytes": full_pdf.stat().st_size,
                "hash_rule": "first current staging hash for generated derivative",
                "recorded_at": now(),
            },
        )
    full_relative = full_pdf.relative_to(PACKAGE).as_posix()
    representations.append(
        {
            "representation_id": (
                "iss:trafo:TRO-1953-26:access:full-issue-pdf"
            ),
            "inventory": "TRO-1953-26",
            "role": "full_issue_access_pdf",
            "sequence": "",
            "relative_path": full_relative,
            "source_path": (
                "export/access/TRO-1953-26.pdf + "
                "export/access/TRO-1953-26-2-md.pdf"
            ),
            "source_sha256": "",
            "content_sha256": full_digest,
            "size_bytes": full_pdf.stat().st_size,
            "decoded_pixel_sha256": "",
            "embedding_profile": "pdfunite-no-page-recompression",
            "metadata_readback": "",
            "technical_readback": json.dumps({"pdf_pages": 14}, sort_keys=True),
        }
    )
    stored[full_relative] = full_digest
    for row in objects:
        if row["inventory"] == "TRO-1953-26":
            row["representation_count"] += 1
            break
    if len(objects) != 1548 or len(representations) != 11636:
        raise FinalizeError(
            f"catalog counts differ: {len(objects)} objects, "
            f"{len(representations)} representations"
        )
    return objects, representations, stored


def write_package_metadata(
    objects: list[dict[str, Any]],
    representations: list[dict[str, Any]],
    loss_records: list[dict[str, Any]],
) -> None:
    expected_absent = [
        PACKAGE / "CATALOG.csv",
        PACKAGE / "CATALOG.xlsx",
        PACKAGE / "README.md",
        PACKAGE / "db-restore" / "RESTORE.csv",
        PACKAGE / "db-restore" / "RESTORE.xlsx",
        PACKAGE / "db-restore" / "RESTORE.sqlite",
        PACKAGE / "db-restore" / "RESTORE.sql",
        PACKAGE / "db-restore" / "checksums.csv",
        PACKAGE / "db-restore" / "manifests" / "representations.csv",
        PACKAGE / "db-restore" / "manifests" / "accession-plan.json",
    ]
    existing = [str(path) for path in expected_absent if path.exists()]
    if existing:
        raise FinalizeError(f"final package metadata already exists: {existing}")
    object_fields = list(objects[0])
    representation_fields = list(representations[0])
    full.write_csv(PACKAGE / "CATALOG.csv", objects, object_fields)
    full.write_xlsx(
        PACKAGE / "CATALOG.xlsx",
        objects,
        object_fields,
        sheet="Trafo",
        table="TrafoCatalog",
    )
    full.write_csv(
        PACKAGE / "db-restore" / "RESTORE.csv",
        representations,
        representation_fields,
    )
    full.write_xlsx(
        PACKAGE / "db-restore" / "RESTORE.xlsx",
        representations,
        representation_fields,
        sheet="Representations",
        table="TrafoRepresentations",
    )
    full.write_csv(
        PACKAGE / "db-restore" / "manifests" / "representations.csv",
        representations,
        representation_fields,
    )
    original_plan = read_json(RUN_ROOT / "plan.json")
    full.write_json(
        PACKAGE / "db-restore" / "manifests" / "accession-plan.json",
        {
            **original_plan,
            "status": "completed-unsealed",
            "loss_resolution": {
                "resolution_id": STATE.name,
                "decision": "genuine source loss with exact issue PDF fallback",
                "loss_count": 3,
                "damaged_byte_streams_retained_in_final": False,
                "loss_ledger": LOSS_LEDGER.relative_to(PACKAGE).as_posix(),
                "records": loss_records,
            },
        },
    )
    full.create_restore_db(
        PACKAGE / "db-restore" / "RESTORE.sqlite",
        objects,
        representations,
    )
    full.write_bytes(
        PACKAGE / "README.md",
        f"""# {full.UNIT_TITLE}

Unit ID: `{full.UNIT_ID}`

Status: unsealed accession package.

This filesystem-readable unit contains 1548 newspaper objects, 10083
metadata-enriched TIFF preservation pages, four exact-PDF preservation
fallbacks, and complete access PDFs. Three TIFF source pages were confirmed as
genuine source losses during accession and are represented by their exact issue
PDFs. The damaged byte streams are not retained in this final unit. The loss
decisions and original intake identities are recorded in
`documents/source-losses.json`.

TIFF metadata readback, technical structure, decoded pixels, and the
post-enrichment hashes were verified during staging. Existing media hashes were
reused during finalization. Newly created files were hashed once, and same-
filesystem atomic publication did not trigger a media rehash.

Restore state and complete fixity information are under `db-restore/`.
""".encode("utf-8"),
    )


def write_checksums(stored_media: dict[str, str]) -> list[dict[str, Any]]:
    checksum_path = PACKAGE / "db-restore" / "checksums.csv"
    if checksum_path.exists():
        raise FinalizeError("checksum manifest unexpectedly exists")
    rows: list[dict[str, Any]] = []
    media_seen: set[str] = set()
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file():
            continue
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise FinalizeError(f"linked package file: {path}")
        relative = path.relative_to(PACKAGE).as_posix()
        if path.suffix.lower() in {".tif", ".tiff", ".pdf"}:
            if relative not in stored_media:
                raise FinalizeError(f"media lacks stored current hash: {relative}")
            digest = stored_media[relative]
            media_seen.add(relative)
        else:
            digest = full.sha256_file(path)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise FinalizeError(f"invalid stored digest: {relative}")
        rows.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": path.stat().st_size,
            }
        )
    if media_seen != set(stored_media):
        missing = sorted(set(stored_media) - media_seen)
        raise FinalizeError(f"stored media paths absent: {missing[:10]}")
    full.write_csv(
        checksum_path,
        rows,
        ["path", "sha256", "size_bytes"],
    )
    return rows


def validate_structure(root: Path) -> dict[str, Any]:
    checksum_path = root / "db-restore" / "checksums.csv"
    with checksum_path.open(encoding="utf-8", newline="") as handle:
        checksums = list(csv.DictReader(handle))
    expected = {row["path"] for row in checksums}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    errors: list[str] = []
    if actual - {"db-restore/checksums.csv"} != expected:
        errors.append("checksum path coverage differs")
    for row in checksums:
        path = root / row["path"]
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_nlink != 1
            or path.stat().st_size != int(row["size_bytes"])
        ):
            errors.append(f"file structure differs: {row['path']}")
    temporary = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.name.startswith(".") or path.suffix == ".tmp"
    ]
    if temporary:
        errors.append(f"temporary paths remain: {temporary[:10]}")
    connection = sqlite3.connect(
        f"file:{root / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        objects = connection.execute("SELECT count(*) FROM archive_object").fetchone()[0]
        representations = connection.execute(
            "SELECT count(*) FROM representation"
        ).fetchone()[0]
        loss_fallbacks = connection.execute(
            "SELECT count(*) FROM representation "
            "WHERE role='preservation_master_pdf_fallback_for_source_loss'"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok" or objects != 1548 or representations != 11636:
        errors.append("restore database counts or integrity differ")
    if loss_fallbacks != 3:
        errors.append("source-loss fallback count differs")
    counts = {
        "objects": objects,
        "representations": representations,
        "tiff_pages": sum(1 for _ in (root / "masters").glob("*/pages/*.tif")),
        "master_pdfs": sum(1 for _ in (root / "masters").glob("*/*.pdf")),
        "access_pdfs": sum(1 for _ in (root / "export/access").glob("*.pdf")),
        "page_reports": sum(
            1 for _ in (root / "db-restore/manifests/pages").glob("*/*.json")
        ),
        "checksum_rows": len(checksums),
    }
    if counts["tiff_pages"] != 10083 or counts["page_reports"] != 10083:
        errors.append("TIFF or page-report count differs")
    if counts["master_pdfs"] != 4 or counts["access_pdfs"] != 1549:
        errors.append("PDF count differs")
    for inventory in ("TRO-1964-45", "TRO-1970-021", "TRO-1970-022"):
        path = root / "masters" / inventory / f"{inventory}.pdf"
        if full.pdf_pages(path) != 7:
            errors.append(f"loss fallback PDF page count differs: {inventory}")
    if full.pdf_pages(root / "export/access/TRO-1953-26-full.pdf") != 14:
        errors.append("combined 1953 PDF page count differs")
    return {
        "format": "iss-trafo-kiss-structural-validation-v1",
        "run_id": full.RUN_ID,
        "unit_id": full.UNIT_ID,
        "validated_at": now(),
        "result": "ok" if not errors else "error",
        "errors": errors,
        "restore_sqlite_integrity": integrity,
        "source_loss_fallbacks": loss_fallbacks,
        **counts,
        "root": str(root),
        "media_rehashed": False,
    }


def snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_size,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps(dry_run(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if FINAL_VALIDATION.exists():
        value = read_json(FINAL_VALIDATION)
        if value.get("result") == "ok" and FINAL_UNIT.is_dir():
            print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        raise FinalizeError("existing final validation is not a successful checkpoint")
    if not PACKAGE.is_dir() or FINAL_STAGE.exists() or FINAL_UNIT.exists():
        raise FinalizeError("staging/final path precondition differs")
    if PACKAGE.stat().st_dev != full.FINAL_SCOPE.stat().st_dev:
        raise FinalizeError("staging and final roots are not on one filesystem")

    STATE.mkdir(parents=True, exist_ok=True)
    write_progress("preflight")
    _, grouped = load_manifest()
    errors = load_confirmed_errors()
    loss_records, loss_stored = resolve_losses(grouped, errors)
    write_progress("losses-recorded", resolved_losses=len(loss_records))

    objects, representations, media_stored = collect_catalog(grouped, loss_records)
    media_stored.update(loss_stored)
    write_progress(
        "catalog-collected",
        objects=len(objects),
        representations=len(representations),
        stored_media_hashes=len(media_stored),
    )
    write_package_metadata(objects, representations, loss_records)
    checksum_rows = write_checksums(media_stored)
    write_progress(
        "package-metadata-complete",
        checksum_rows=len(checksum_rows),
        media_rehashed=False,
    )

    staging_validation = validate_structure(PACKAGE)
    full.write_json(STATE / "staging-validation.json", staging_validation)
    if staging_validation["result"] != "ok":
        raise FinalizeError("staging structural validation failed")
    before = snapshot(PACKAGE)
    write_progress(
        "atomic-promotion",
        files=len(before),
        final_stage=str(FINAL_STAGE),
    )
    os.rename(PACKAGE, FINAL_STAGE)
    after_stage = snapshot(FINAL_STAGE)
    if after_stage != before:
        raise FinalizeError("inode/size identity changed during hidden-stage rename")
    final_stage_validation = validate_structure(FINAL_STAGE)
    if final_stage_validation["result"] != "ok":
        full.write_json(STATE / "hidden-stage-validation.json", final_stage_validation)
        raise FinalizeError("hidden final-stage structural validation failed")
    os.rename(FINAL_STAGE, FINAL_UNIT)
    after_final = snapshot(FINAL_UNIT)
    if after_final != before:
        raise FinalizeError("inode/size identity changed during final rename")
    validation = validate_structure(FINAL_UNIT)
    validation.update(
        {
            "atomic_same_filesystem_promotion": True,
            "inode_size_identity_preserved": True,
            "source_loss_count": 3,
            "damaged_byte_streams_retained_in_final": False,
            "staging_media_hashes_reused": len(media_stored),
            "publication_path": str(FINAL_UNIT),
        }
    )
    if validation["result"] != "ok":
        raise FinalizeError("published unit structural validation failed")
    full.write_json(FINAL_VALIDATION, validation)
    full.write_json(
        STATE / "resolution-complete.json",
        {
            "result": "ok",
            "completed_at": now(),
            "losses_resolved": 3,
            "resolution": "exact issue PDF preservation fallback",
            "final_unit": str(FINAL_UNIT),
            "final_validation": str(FINAL_VALIDATION),
            "media_rehashed_during_finalization": False,
        },
    )
    write_progress(
        "complete",
        result="ok",
        final_unit=str(FINAL_UNIT),
        media_rehashed=False,
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FinalizeError,
        full.AccessionError,
        OSError,
        sqlite3.Error,
    ) as error:
        raise SystemExit(f"error: {error}") from error
