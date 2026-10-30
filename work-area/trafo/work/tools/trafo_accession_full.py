#!/usr/bin/env python3
"""Build and validate the complete unsealed Trafo v3 accession unit."""

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
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_ID = "trafo-full-002"
UNIT_ID = "trafo"
UNIT_TITLE = "Betriebszeitung TRO – Der Transformator"
PROFILE = "iss-tiff-v2+trafo-page-1"

WORK_SCOPE = Path("/mnt/volume/iss-archive-work")
FINAL_SCOPE = Path("/mnt/volume/iss-archive/units")
TRAFO_WORK = WORK_SCOPE / "trafo"
RUN_ROOT = TRAFO_WORK / "work" / "accession-runs" / RUN_ID
PACKAGE = RUN_ROOT / "package"
FINAL_STAGE = FINAL_SCOPE / f".{UNIT_ID}.{RUN_ID}.tmp"
FINAL_UNIT = FINAL_SCOPE / UNIT_ID
MANIFEST = (
    TRAFO_WORK / "work" / "finalization-plan"
    / "trafo-confirmed-reconciliation-001" / "final-manifest.csv"
)
PDF_PLAN = MANIFEST.with_name("pdf-derivative-plan.csv")
MD_RUN = (
    TRAFO_WORK / "work" / "reconciliation" / "museum-digital"
    / "runs" / "20260724T165726Z-trafo"
)
MD_OBJECTS = MD_RUN / "objects.csv"
MD_RAW = MD_RUN / "raw"
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


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: Any) -> None:
    write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
    )


def replace_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


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
    write_bytes(path, payload)


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AccessionError(
            f"{arguments[0]} failed ({result.returncode}): {result.stdout.strip()}"
        )
    return result


def source_path(row: dict[str, str]) -> Path:
    path = (Path(row["storage_root"]) / row["destination_path"]).resolve(strict=True)
    try:
        path.relative_to(WORK_SCOPE)
    except ValueError as error:
        raise AccessionError(f"selected source outside work scope: {path}") from error
    if not path.is_file() or path.is_symlink():
        raise AccessionError(f"selected source is not one regular file: {path}")
    return path


def raw_objects() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            inventory = str(value.get("object_inventory_number") or "").strip()
            if inventory and value.get("object_id") is not None:
                if inventory in result:
                    raise AccessionError(f"duplicate raw MD inventory: {inventory}")
                result[inventory] = value
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in sorted(MD_RAW.glob("*.json")):
        visit(json.loads(path.read_text(encoding="utf-8")))
    return result


def first_event(raw: dict[str, Any], event_type: int) -> dict[str, Any]:
    return next(
        (
            event for event in raw.get("object_events", [])
            if int(event.get("event_type") or 0) == event_type
        ),
        {},
    )


def joined(values: list[str]) -> str:
    return "; ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def metadata_from_raw(raw: dict[str, Any]) -> dict[str, str]:
    commissioned = first_event(raw, 25)
    relation_places = [
        str(item.get("place", {}).get("place_name") or "")
        for item in raw.get("object_relation_places", [])
    ]
    relation_people = [
        str(item.get("people", {}).get("people_display_name") or "")
        for item in raw.get("object_relation_people", [])
    ]
    collections = [
        str(item.get("collection_name") or "")
        for item in raw.get("object_collection", [])
    ]
    images = raw.get("object_images") or [{}]
    main_image = next(
        (item for item in images if str(item.get("is_main")) == "j"),
        images[0],
    )
    return {
        "metadata_source": "museum-digital-raw",
        "title": str(raw.get("object_name") or ""),
        "description": str(raw.get("object_description") or ""),
        "object_type": str(raw.get("object_type") or ""),
        "material_technique": str(raw.get("object_material_technique") or ""),
        "dimensions": str(raw.get("object_dimensions") or ""),
        "date_created": str(commissioned.get("time", {}).get("time_name") or ""),
        "creator": str(
            commissioned.get("people", {}).get("people_display_name") or ""
        ),
        "coverage": joined(
            relation_places
            + [str(commissioned.get("place", {}).get("place_name") or "")]
        ),
        "contributor": joined(relation_people),
        "collection": joined(collections),
        "rights_holder": str(main_image.get("owner") or ""),
        "rights_usage_terms": str(main_image.get("rights") or ""),
        "metadata_license": str(
            raw.get("licence", {}).get("metadata_rights_status") or ""
        ),
        "credit_line": str(main_image.get("owner") or ""),
        "keywords": joined(
            [str(item.get("tag_name") or "") for item in raw.get("object_tags", [])]
        ),
    }


def metadata_from_local(row: dict[str, str]) -> dict[str, str]:
    return {
        "metadata_source": "local-catalog",
        "title": row["object_title"],
        "description": row["object_description"],
        "object_type": row["object_type"],
        "material_technique": row["object_material_technique"],
        "dimensions": row["object_dimensions"],
        "date_created": row["commissioned_when1"],
        "creator": row["commissioned_who1"],
        "coverage": joined([row["related_place1"], row["commissioned_where1"]]),
        "contributor": row["related_actor1"],
        "collection": row["collection_name1"],
        "rights_holder": row["rights_object"],
        "rights_usage_terms": row["rights_of_use"],
        "metadata_license": row["rights_metadata_license"],
        "credit_line": row["image_owner1"],
        "keywords": joined(
            [row.get(f"tag{index}", "") for index in range(1, 7)]
        ),
    }


def load_inputs() -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, dict[str, str]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, str]],
]:
    with MANIFEST.open(encoding="utf-8-sig", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))
    with MD_OBJECTS.open(encoding="utf-8-sig", newline="") as handle:
        md_rows = list(csv.DictReader(handle))
    with LOCAL_METADATA.open(encoding="cp1252", newline="") as handle:
        local_rows = list(csv.DictReader(handle, delimiter=";"))
    by_inventory: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in manifest_rows:
        by_inventory[row["inventory"]].append(row)
    md = {row["inventory"]: row for row in md_rows}
    local = {row["inventory_number"]: row for row in local_rows}
    raw = raw_objects()
    if len(manifest_rows) != 11634 or len(by_inventory) != 1548:
        raise AccessionError("final manifest count differs from accepted plan")
    if set(by_inventory) != set(md) or set(by_inventory) != set(raw):
        raise AccessionError("manifest, MD table, and raw MD inventory sets differ")
    if len(local) != len(local_rows):
        raise AccessionError("duplicate local metadata inventory")
    return dict(by_inventory), md, raw, local


def preflight(
    by_inventory: dict[str, list[dict[str, str]]],
    md: dict[str, dict[str, str]],
    raw: dict[str, dict[str, Any]],
    local: dict[str, dict[str, str]],
) -> dict[str, Any]:
    selected_paths: set[Path] = set()
    output_paths: set[str] = set()
    total_bytes = 0
    tiffs = 0
    pdfs = 0
    snapshots: dict[str, tuple[int, int, int]] = {}
    for inventory, rows in by_inventory.items():
        if str(raw[inventory].get("object_id")) != md[inventory]["object_id"]:
            raise AccessionError(f"MD object mismatch: {inventory}")
        pages = sorted(
            [row for row in rows if row["role"] == "tiff_page"],
            key=lambda row: int(row["sequence"]),
        )
        issue_output_names: set[str] = set()
        for row in rows:
            path = source_path(row)
            if path in selected_paths:
                raise AccessionError(f"duplicate selected physical path: {path}")
            selected_paths.add(path)
            stat = path.stat()
            if stat.st_size != int(row["size_bytes"]):
                raise AccessionError(f"selected source size changed: {path}")
            snapshots[str(path)] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            total_bytes += stat.st_size
            if row["role"] == "tiff_page":
                tiffs += 1
                name = path.name
                if name in issue_output_names:
                    raise AccessionError(f"duplicate page filename: {inventory}/{name}")
                issue_output_names.add(name)
                output = f"masters/{inventory}/pages/{name}"
            else:
                pdfs += 1
                output = (
                    f"masters/{inventory}/{inventory}.pdf"
                    if row["role"] == "local_pdf_fallback"
                    else f"export/access/{inventory}.pdf"
                )
            if output in output_paths:
                raise AccessionError(f"duplicate package path: {output}")
            output_paths.add(output)
        if inventory == "TRO-1976-005":
            if pages or [row["role"] for row in rows] != ["local_pdf_fallback"]:
                raise AccessionError("sole fallback shape differs")
        elif not pages or len([r for r in rows if r["role"] == "local_pdf"]) != 1:
            raise AccessionError(f"unexpected issue representation shape: {inventory}")
    if tiffs != 10086 or pdfs != 1548:
        raise AccessionError("accepted representation counts differ")
    if RUN_ROOT.exists() or FINAL_STAGE.exists() or FINAL_UNIT.exists():
        raise AccessionError("full accession requires fresh work, staging, and final paths")
    if not copyhash_runner.helper_available(COPYHASH):
        raise AccessionError(f"copy/hash helper unavailable: {COPYHASH}")
    for tool in ("exiftool", "magick", "pdfinfo", "pdfunite"):
        if shutil.which(tool) is None:
            raise AccessionError(f"required tool unavailable: {tool}")
    return {
        "inventories": len(by_inventory),
        "manifest_rows": sum(len(rows) for rows in by_inventory.values()),
        "tiff_pages": tiffs,
        "pdf_sources": pdfs,
        "source_bytes": total_bytes,
        "local_metadata_rows_used": sum(inventory in local for inventory in by_inventory),
        "raw_md_fallback_rows": sum(inventory not in local for inventory in by_inventory),
        "source_snapshots": snapshots,
    }


def page_tags(
    inventory: str,
    page_number: int,
    page_count: int,
    source_row: dict[str, str],
    metadata: dict[str, str],
    md_object_id: str,
    plan_digest: str,
) -> dict[str, str]:
    year_match = re.search(r"(?:19|20)\d{2}", inventory)
    title = f"{metadata['title']}; Seite {page_number} von {page_count}"
    description = metadata["description"]
    if description:
        description += " "
    description += f"Digitale Seitenrepräsentation: Seite {page_number} von {page_count}."
    tags = tiff_metadata.expected_tags(
        tiff_metadata.PROFILE_V2,
        {
            "entity_id": f"iss:trafo:{inventory}:page:{page_number:03d}",
            "archival_label": inventory,
            "descriptive_title": title,
            "description": description,
            "object_type": "Zeitungsseite",
            "material_technique": metadata["material_technique"],
            "dimensions": metadata["dimensions"],
            "creator": metadata["creator"],
            "date_created": year_match.group(0) if year_match else "",
            "rights_statement": metadata["rights_usage_terms"],
            "rights_holder": metadata["rights_holder"],
            "rights_usage_terms": metadata["rights_usage_terms"],
            "credit_line": metadata["credit_line"],
            "keywords": metadata["keywords"],
            "coverage": metadata["coverage"],
            "contributor": metadata["contributor"],
            "source_collection": metadata["collection"],
            "image_reference": (
                f"{Path(source_row['destination_path']).name}; "
                f"page {page_number} of {page_count}"
            ),
        },
        {
            "case_id": RUN_ID,
            "source_file_id": Path(source_row["destination_path"]).name,
            "source_digest": source_row["sha256"],
            "reviewed_bundle_digest": plan_digest,
            "museum_digital_id": md_object_id,
            "representation_role": "preservation_master",
            "provenance_gaps": [],
        },
    )
    tags["XMP-dc:Relation"] += (
        f"; iss:carrier:{inventory}; "
        f"iss:page:{page_number:03d}-of-{page_count:03d}; "
        f"iss:publication-date:{metadata['date_created']}"
    )
    tags["XMP-xmp:Label"] = PROFILE
    return tags


def embed_in_place(path: Path, tags: dict[str, str]) -> None:
    arguments = ["exiftool", "-overwrite_original"]
    arguments.extend(f"-{tag}={value}" for tag, value in tags.items())
    arguments.append(str(path))
    result = run(arguments)
    if "Warning:" in result.stdout:
        raise AccessionError(f"ExifTool warning for {path}: {result.stdout.strip()}")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def pdf_pages(path: Path) -> int:
    output = run(["pdfinfo", str(path)]).stdout
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise AccessionError(f"cannot read PDF page count: {path}")
    return int(match.group(1))


def progress(done: int, total: int, inventory: str, phase: str) -> None:
    replace_json(
        RUN_ROOT / "progress.json",
        {
            "run_id": RUN_ID,
            "updated_at": now(),
            "phase": phase,
            "completed_manifest_rows": done,
            "total_manifest_rows": total,
            "current_inventory": inventory,
        },
    )


def build(
    by_inventory: dict[str, list[dict[str, str]]],
    md: dict[str, dict[str, str]],
    raw: dict[str, dict[str, Any]],
    local: dict[str, dict[str, str]],
    plan_digest: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    representations: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    done = 0
    total = sum(len(rows) for rows in by_inventory.values())
    for inventory in sorted(by_inventory):
        rows = by_inventory[inventory]
        metadata = (
            metadata_from_local(local[inventory])
            if inventory in local
            else metadata_from_raw(raw[inventory])
        )
        pages = sorted(
            [row for row in rows if row["role"] == "tiff_page"],
            key=lambda row: int(row["sequence"]),
        )
        issue_representations: list[dict[str, Any]] = []
        for page_number, source_row in enumerate(pages, 1):
            source = source_path(source_row)
            target = PACKAGE / "masters" / inventory / "pages" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            technical_source = tiff_accession._exif_values(
                source, tiff_accession.TECHNICAL_TAGS
            )
            bit_depth = tiff_accession._scalar_sample_depth(
                technical_source.get("IFD0:BitsPerSample")
            )
            if tiff_accession._page_count(source) != 1:
                raise AccessionError(f"TIFF is not one image directory: {source}")
            source_pixel = tiff_accession._decoded_pixel_digest(source, bit_depth)
            copied = copyhash_runner.copy_file(
                COPYHASH, source, target, source_row["sha256"]
            )
            if copied != source_row["sha256"]:
                raise AccessionError(f"copy digest differs: {source}")
            tags = page_tags(
                inventory,
                page_number,
                len(pages),
                source_row,
                metadata,
                md[inventory]["object_id"],
                plan_digest,
            )
            embed_in_place(target, tags)
            if tiff_accession._exif_values(target, tuple(tags)) != tags:
                raise AccessionError(f"embedded metadata readback differs: {target}")
            technical_target = tiff_accession._exif_values(
                target, tiff_accession.TECHNICAL_TAGS
            )
            if technical_target != technical_source:
                raise AccessionError(f"TIFF technical metadata changed: {target}")
            target_pixel = tiff_accession._decoded_pixel_digest(target, bit_depth)
            if target_pixel != source_pixel:
                raise AccessionError(f"decoded TIFF pixels changed: {target}")
            relative = target.relative_to(PACKAGE).as_posix()
            report = {
                "inventory": inventory,
                "page_number": page_number,
                "page_count": len(pages),
                "source": str(source),
                "source_sha256": source_row["sha256"],
                "target": relative,
                "target_sha256": sha256_file(target),
                "size_bytes": target.stat().st_size,
                "decoded_pixel_sha256": target_pixel,
                "embedding_profile": PROFILE,
                "embedded_metadata": tags,
                "technical_metadata": technical_target,
            }
            write_json(
                PACKAGE / "db-restore" / "manifests" / "pages"
                / inventory / f"page-{page_number:03d}.json",
                report,
            )
            issue_representations.append(
                {
                    "representation_id": f"iss:trafo:{inventory}:page:{page_number:03d}",
                    "inventory": inventory,
                    "role": "preservation_master_page",
                    "sequence": page_number,
                    "relative_path": relative,
                    "source_path": str(source),
                    "source_sha256": source_row["sha256"],
                    "content_sha256": report["target_sha256"],
                    "size_bytes": report["size_bytes"],
                    "decoded_pixel_sha256": target_pixel,
                    "embedding_profile": PROFILE,
                    "metadata_readback": json.dumps(tags, ensure_ascii=False, sort_keys=True),
                    "technical_readback": json.dumps(
                        technical_target, ensure_ascii=False, sort_keys=True
                    ),
                }
            )
            done += 1
            progress(done, total, inventory, "embedding-tiffs")

        pdf_row = next(row for row in rows if row["role"] != "tiff_page")
        source_pdf = source_path(pdf_row)
        if pdf_row["role"] == "local_pdf_fallback":
            master_pdf = PACKAGE / "masters" / inventory / f"{inventory}.pdf"
            master_pdf.parent.mkdir(parents=True, exist_ok=True)
            copyhash_runner.copy_file(
                COPYHASH, source_pdf, master_pdf, pdf_row["sha256"]
            )
            if pdf_pages(master_pdf) != int(pdf_row["pdf_pages"]):
                raise AccessionError(f"fallback PDF page count differs: {inventory}")
            issue_representations.append(
                {
                    "representation_id": f"iss:trafo:{inventory}:master:pdf",
                    "inventory": inventory,
                    "role": "preservation_master_pdf_fallback",
                    "sequence": "",
                    "relative_path": master_pdf.relative_to(PACKAGE).as_posix(),
                    "source_path": str(source_pdf),
                    "source_sha256": pdf_row["sha256"],
                    "content_sha256": pdf_row["sha256"],
                    "size_bytes": master_pdf.stat().st_size,
                    "decoded_pixel_sha256": "",
                    "embedding_profile": "none-exact-source-bytes",
                    "metadata_readback": "",
                    "technical_readback": json.dumps(
                        {"pdf_pages": int(pdf_row["pdf_pages"])}, sort_keys=True
                    ),
                }
            )
            access_pdf = PACKAGE / "export" / "access" / f"{inventory}.pdf"
            access_pdf.parent.mkdir(parents=True, exist_ok=True)
            copyhash_runner.copy_file(
                COPYHASH, master_pdf, access_pdf, pdf_row["sha256"]
            )
            access_role = "retained_access_pdf_from_fallback_master"
        else:
            access_name = (
                "TRO-1953-26-2-md.pdf"
                if inventory == "TRO-1953-26-2"
                else f"{inventory}.pdf"
            )
            access_pdf = PACKAGE / "export" / "access" / access_name
            access_pdf.parent.mkdir(parents=True, exist_ok=True)
            copyhash_runner.copy_file(
                COPYHASH, source_pdf, access_pdf, pdf_row["sha256"]
            )
            access_role = "retained_access_pdf"
        expected_pages = int(pdf_row["pdf_pages"])
        if pdf_pages(access_pdf) != expected_pages:
            raise AccessionError(f"access PDF page count differs: {inventory}")
        issue_representations.append(
            {
                "representation_id": f"iss:trafo:{inventory}:access:pdf",
                "inventory": inventory,
                "role": access_role,
                "sequence": "",
                "relative_path": access_pdf.relative_to(PACKAGE).as_posix(),
                "source_path": str(source_pdf),
                "source_sha256": pdf_row["sha256"],
                "content_sha256": pdf_row["sha256"],
                "size_bytes": access_pdf.stat().st_size,
                "decoded_pixel_sha256": "",
                "embedding_profile": "none-exact-source-bytes",
                "metadata_readback": "",
                "technical_readback": json.dumps(
                    {"pdf_pages": expected_pages}, sort_keys=True
                ),
            }
        )
        done += 1
        progress(done, total, inventory, "copying-access-pdfs")
        snapshot = {
            "inventory": inventory,
            "metadata_source": metadata["metadata_source"],
            "accepted_metadata": metadata,
            "museum_digital": raw[inventory],
            "local_catalog": local.get(inventory),
        }
        write_json(
            PACKAGE / "documents" / "metadata-sources" / f"{inventory}.json",
            snapshot,
        )
        write_json(
            PACKAGE / "export" / "md" / "metadata" / f"{inventory}.json",
            raw[inventory],
        )
        representations.extend(issue_representations)
        objects.append(
            {
                "inventory": inventory,
                "museum_digital_object_id": md[inventory]["object_id"],
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
                "representation_count": len(issue_representations),
                "status": "unsealed",
            }
        )

    six = PACKAGE / "export" / "access" / "TRO-1953-26.pdf"
    eight = PACKAGE / "export" / "access" / "TRO-1953-26-2-md.pdf"
    full = PACKAGE / "export" / "access" / "TRO-1953-26-full.pdf"
    run(["pdfunite", str(six), str(eight), str(full)])
    if pdf_pages(full) != 14:
        raise AccessionError("combined TRO-1953-26 full issue is not 14 pages")
    full_representation = {
        "representation_id": "iss:trafo:TRO-1953-26:access:full-issue-pdf",
        "inventory": "TRO-1953-26",
        "role": "full_issue_access_pdf",
        "sequence": "",
        "relative_path": full.relative_to(PACKAGE).as_posix(),
        "source_path": f"{six.relative_to(PACKAGE)} + {eight.relative_to(PACKAGE)}",
        "source_sha256": "",
        "content_sha256": sha256_file(full),
        "size_bytes": full.stat().st_size,
        "decoded_pixel_sha256": "",
        "embedding_profile": "pdfunite-no-page-recompression",
        "metadata_readback": "",
        "technical_readback": json.dumps({"pdf_pages": 14}, sort_keys=True),
    }
    representations.append(full_representation)
    for row in objects:
        if row["inventory"] == "TRO-1953-26":
            row["representation_count"] += 1
            break
    return objects, representations


def create_restore_db(
    path: Path,
    objects: list[dict[str, Any]],
    representations: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            CREATE TABLE unit (
                unit_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE archive_object (
                inventory TEXT PRIMARY KEY, museum_digital_object_id TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL,
                object_type TEXT NOT NULL, material_technique TEXT NOT NULL,
                dimensions TEXT NOT NULL, date_created TEXT NOT NULL,
                collection_name TEXT NOT NULL, rights_usage_terms TEXT NOT NULL,
                metadata_license TEXT NOT NULL, metadata_source TEXT NOT NULL,
                page_count INTEGER NOT NULL, representation_count INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE representation (
                representation_id TEXT PRIMARY KEY,
                inventory TEXT NOT NULL REFERENCES archive_object(inventory),
                role TEXT NOT NULL, sequence INTEGER, relative_path TEXT NOT NULL UNIQUE,
                source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                decoded_pixel_sha256 TEXT NOT NULL, embedding_profile TEXT NOT NULL,
                metadata_readback TEXT NOT NULL, technical_readback TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO unit VALUES (?, ?, 'unsealed', ?)", (UNIT_ID, UNIT_TITLE, now())
        )
        object_fields = list(objects[0])
        placeholders = ", ".join(f":{field}" for field in object_fields)
        connection.executemany(
            f"INSERT INTO archive_object ({', '.join(object_fields)}) VALUES ({placeholders})",
            objects,
        )
        representation_fields = list(representations[0])
        placeholders = ", ".join(f":{field}" for field in representation_fields)
        connection.executemany(
            f"INSERT INTO representation ({', '.join(representation_fields)}) "
            f"VALUES ({placeholders})",
            representations,
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("restore database integrity failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    write_bytes(path.with_suffix(".sql"), sql.encode())


def finish_package(
    objects: list[dict[str, Any]],
    representations: list[dict[str, Any]],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    object_fields = list(objects[0])
    representation_fields = list(representations[0])
    write_csv(PACKAGE / "CATALOG.csv", objects, object_fields)
    write_xlsx(
        PACKAGE / "CATALOG.xlsx", objects, object_fields,
        sheet="Trafo", table="TrafoCatalog",
    )
    write_csv(
        PACKAGE / "db-restore" / "RESTORE.csv",
        representations,
        representation_fields,
    )
    write_xlsx(
        PACKAGE / "db-restore" / "RESTORE.xlsx",
        representations,
        representation_fields,
        sheet="Representations",
        table="TrafoRepresentations",
    )
    write_csv(
        PACKAGE / "db-restore" / "manifests" / "representations.csv",
        representations,
        representation_fields,
    )
    write_json(PACKAGE / "db-restore" / "manifests" / "accession-plan.json", plan)
    create_restore_db(PACKAGE / "db-restore" / "RESTORE.sqlite", objects, representations)
    write_bytes(
        PACKAGE / "README.md",
        f"""# {UNIT_TITLE}

Unit ID: `{UNIT_ID}`

Status: unsealed, accession completed and fixity validated.

This filesystem-readable unit contains {len(objects)} newspaper objects.
TIFF preservation pages are metadata-enriched copies whose decoded pixels were
verified against the selected source. Access PDFs retain exact source bytes,
except `TRO-1953-26-full.pdf`, which concatenates the validated six-page and
eight-page components without page-image recompression. `TRO-1976-005` remains
the sole explicitly documented PDF preservation fallback.

Restore state and complete fixity information are under `db-restore/`.
""".encode(),
    )
    checksum_rows = []
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file():
            checksum_rows.append(
                {
                    "path": path.relative_to(PACKAGE).as_posix(),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    write_csv(
        PACKAGE / "db-restore" / "checksums.csv",
        checksum_rows,
        ["path", "sha256", "size_bytes"],
    )
    return checksum_rows


def copy_to_final(checksums: list[dict[str, Any]]) -> None:
    FINAL_STAGE.mkdir(parents=True, exist_ok=False)
    expected = {row["path"]: row["sha256"] for row in checksums}
    for source in sorted(path for path in PACKAGE.rglob("*") if path.is_file()):
        relative = source.relative_to(PACKAGE)
        destination = FINAL_STAGE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = expected.get(relative.as_posix(), sha256_file(source))
        copyhash_runner.copy_file(COPYHASH, source, destination, digest)


def validate(root: Path) -> dict[str, Any]:
    checksum_path = root / "db-restore" / "checksums.csv"
    with checksum_path.open(encoding="utf-8", newline="") as handle:
        checksum_rows = list(csv.DictReader(handle))
    errors: list[str] = []
    bytes_checked = 0
    expected_paths = {row["path"] for row in checksum_rows}
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths - {"db-restore/checksums.csv"} != expected_paths:
        errors.append("checksum coverage differs from package files")
    for row in checksum_rows:
        path = root / row["path"]
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing or linked file: {row['path']}")
            continue
        if path.stat().st_nlink != 1:
            errors.append(f"hard-link violation: {row['path']}")
        if path.stat().st_size != int(row["size_bytes"]):
            errors.append(f"size mismatch: {row['path']}")
            continue
        bytes_checked += path.stat().st_size
        if sha256_file(path) != row["sha256"]:
            errors.append(f"digest mismatch: {row['path']}")
    connection = sqlite3.connect(
        f"file:{root / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1", uri=True
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        objects = connection.execute("SELECT count(*) FROM archive_object").fetchone()[0]
        representations = connection.execute(
            "SELECT count(*) FROM representation"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok" or objects != 1548 or representations != 11636:
        errors.append("restore database validation differs")
    if pdf_pages(root / "export/access/TRO-1953-26-2-md.pdf") != 8:
        errors.append("MD-matching 1953 PDF page count differs")
    if pdf_pages(root / "export/access/TRO-1953-26-full.pdf") != 14:
        errors.append("full 1953 PDF page count differs")
    return {
        "format": "iss-trafo-full-accession-validation-v1",
        "run_id": RUN_ID,
        "unit_id": UNIT_ID,
        "validated_at": now(),
        "result": "ok" if not errors else "error",
        "errors": errors,
        "checksum_rows": len(checksum_rows),
        "bytes_checked": bytes_checked,
        "objects": objects,
        "representations": representations,
        "restore_sqlite_integrity": integrity,
        "root": str(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        root = FINAL_UNIT if FINAL_UNIT.exists() else FINAL_STAGE
        result = validate(root)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["result"] == "ok" else 1
    by_inventory, md, raw, local = load_inputs()
    summary = preflight(by_inventory, md, raw, local)
    snapshots = summary.pop("source_snapshots")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "result": "ok",
                    "dry_run": True,
                    "run_id": RUN_ID,
                    "unit_id": UNIT_ID,
                    "unit_title": UNIT_TITLE,
                    "work_output": str(RUN_ROOT),
                    "final_stage": str(FINAL_STAGE),
                    "final_output": str(FINAL_UNIT),
                    **summary,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    RUN_ROOT.mkdir(parents=True, exist_ok=False)
    plan = {
        "format": "iss-trafo-full-accession-plan-v1",
        "run_id": RUN_ID,
        "unit_id": UNIT_ID,
        "unit_title": UNIT_TITLE,
        "status": "running-unsealed",
        "created_at": now(),
        "source_manifest": str(MANIFEST),
        "source_manifest_sha256": sha256_file(MANIFEST),
        "pdf_derivative_plan": str(PDF_PLAN),
        "pdf_derivative_plan_sha256": sha256_file(PDF_PLAN),
        "md_objects": str(MD_OBJECTS),
        "md_objects_sha256": sha256_file(MD_OBJECTS),
        "local_metadata": str(LOCAL_METADATA),
        "local_metadata_sha256": sha256_file(LOCAL_METADATA),
        "embedding_profile": PROFILE,
        **summary,
    }
    plan_digest = "sha256:" + hashlib.sha256(canonical_json(plan)).hexdigest()
    write_json(RUN_ROOT / "plan.json", {**plan, "plan_digest": plan_digest})
    objects, representations = build(by_inventory, md, raw, local, plan_digest)
    checksums = finish_package(objects, representations, {**plan, "plan_digest": plan_digest})
    copy_to_final(checksums)
    validation = validate(FINAL_STAGE)
    if validation["result"] != "ok":
        write_json(RUN_ROOT / "final-validation.json", validation)
        raise AccessionError("final-stage validation failed")
    for path_string, before in snapshots.items():
        stat = Path(path_string).stat()
        if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != before:
            raise AccessionError(f"selected source changed during run: {path_string}")
    os.rename(FINAL_STAGE, FINAL_UNIT)
    validation["root"] = str(FINAL_UNIT)
    validation["sources_unchanged"] = True
    write_json(RUN_ROOT / "final-validation.json", validation)
    print(json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AccessionError,
        OSError,
        copyhash_runner.CopyHashError,
        tiff_accession.TiffAccessionError,
        tiff_metadata.TiffMetadataError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
