#!/usr/bin/env python3
"""Correct collection 555 document scope and reconcile Primus metadata."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import accession_collection_555 as base  # noqa: E402


CURRENT = base.UNIT
STAGE = base.WORK_ROOT / "work/accession/collection-555-corrected-build-20260829"
INPUTS = base.WORK_ROOT / "work/accession/collection-555-correction-inputs-20260829"
PREDECESSOR = base.WORK_ROOT / "work/accession/pre-document-correction-unit-20260829"
PRIMUS_SOURCE = Path(
    "/media/vladimir/Elements/restore/archive/WF/Pressebildarchiv/Primus/Altdateien vom Praxisprojekt/Inventar Primus 1 und 2.xlsx"
)
PRIMUS_EXPORT_XLSX = Path(
    "/media/vladimir/Elements/restore/archive/WF/Archiv/Primus_fuer_ md-50429-51200-Stand-25-1-19-1.xlsx"
)
PRIMUS_EXPORT_CSV = Path(
    "/media/vladimir/Elements/restore/archive/WF/Archiv/Primus_fuer_ md-50429-51200-Stand-25-1-19-1.csv"
)
RECOVERED = INPUTS / "Inventar Primus 1 und 2_RECOVERED.xlsx"
RECOVERY_RECEIPT = INPUTS / "primus-workbook-recovery.json"
PROFILE = "iss-collection-555-md-primus-reconciled-v2"
GENERAL_DOCUMENTS = {
    "Richtlinien für die Handhabung mit Fotografien.pdf",
    "Klassifikationen  und Schlagwörter.docx",
    "Konservatorische Bedingungen_Hanna.doc",
}
EXPECTED_OBJECTS = 48
EXPECTED_IMAGE_REPRESENTATIONS = 157
EXPECTED_DRAIN = 120
EXPECTED_DOCUMENTS = 4

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class CorrectionError(RuntimeError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    atomic_bytes(path, csv_bytes(fields, rows))


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def cell_column(reference: str) -> int:
    match = re.match(r"[A-Z]+", reference)
    if not match:
        raise CorrectionError(f"invalid OOXML cell reference: {reference}")
    value = 0
    for character in match.group():
        value = value * 26 + ord(character) - 64
    return value - 1


def normalize_inventory(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def member_digests(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            payload = archive.read(name)
            result[name] = {"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def recover_workbook() -> dict[str, Any]:
    base.source_guard()
    if INPUTS.exists():
        raise CorrectionError(f"correction input directory already exists: {INPUTS}")
    INPUTS.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="primus-ooxml-parts-", dir=INPUTS) as temporary_name:
        parts = Path(temporary_name)
        result = subprocess.run(
            ["7z", "x", "-y", f"-o{parts}", str(PRIMUS_SOURCE)],
            capture_output=True, text=True, timeout=600,
        )
        files = sorted(path for path in parts.rglob("*") if path.is_file())
        if result.returncode != 0 or len(files) != 19:
            raise CorrectionError(f"7-Zip recovery failed: {(result.stdout + result.stderr).strip()}")
        source_member_hashes = {
            path.relative_to(parts).as_posix(): {
                "size_bytes": path.stat().st_size,
                "sha256": base.hash_file(path)[0],
            }
            for path in files
        }
        with zipfile.ZipFile(
            RECOVERED, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
            strict_timestamps=False,
        ) as archive:
            for path in files:
                archive.write(path, path.relative_to(parts).as_posix())
    xml_errors: list[dict[str, str]] = []
    with zipfile.ZipFile(RECOVERED) as archive:
        bad_member = archive.testzip()
        names = sorted(archive.namelist())
        for name in names:
            if name.endswith((".xml", ".rels")):
                try:
                    ET.fromstring(archive.read(name))
                except Exception as error:
                    xml_errors.append({"member": name, "error": str(error)})
    recovered_member_hashes = member_digests(RECOVERED)
    if bad_member or xml_errors or source_member_hashes != recovered_member_hashes:
        raise CorrectionError(
            f"recovered workbook member validation failed: bad={bad_member}, xml={xml_errors}, member_equality={source_member_hashes == recovered_member_hashes}"
        )
    source_sha, source_size = base.hash_file(PRIMUS_SOURCE)
    recovered_sha, recovered_size = base.hash_file(RECOVERED)
    receipt = {
        "format": "iss-primus-workbook-recovery-v1",
        "source_path": str(PRIMUS_SOURCE),
        "source_sha256": source_sha,
        "source_size_bytes": source_size,
        "observed_zip_physical_size_bytes": 981168,
        "observed_trailing_bytes": 70118,
        "recovery_method": "7-Zip extraction of all OOXML members followed by ZIP repackaging; member bytes unchanged",
        "members": len(source_member_hashes),
        "member_bytes_identical": True,
        "xml_parts_valid": True,
        "zip_testzip": None,
        "recovered_path": str(RECOVERED),
        "recovered_sha256": recovered_sha,
        "recovered_size_bytes": recovered_size,
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(RECOVERY_RECEIPT, receipt)
    return receipt


def workbook_rows(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(item.itertext()) for item in shared_root.findall("m:si", NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {item.attrib["Id"]: item.attrib["Target"] for item in rels_root.findall("p:Relationship", NS)}
        sheet = next(item for item in workbook.findall("m:sheets/m:sheet", NS) if item.attrib["name"] == "Tabelle2")
        target = rels[sheet.attrib[f"{{{NS['r']}}}id"]]
        target = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
        sheet_root = ET.fromstring(archive.read(target))
        parsed_rows: list[tuple[int, dict[int, str]]] = []
        for row in sheet_root.findall("m:sheetData/m:row", NS):
            cells: dict[int, str] = {}
            for cell in row.findall("m:c", NS):
                value = cell.find("m:v", NS)
                text = ""
                if value is not None:
                    text = shared[int(value.text)] if cell.attrib.get("t") == "s" else (value.text or "")
                inline = cell.find("m:is", NS)
                if inline is not None:
                    text = "".join(inline.itertext())
                cells[cell_column(cell.attrib["r"])] = text
            parsed_rows.append((int(row.attrib["r"]), cells))
    if not parsed_rows:
        raise CorrectionError("recovered workbook Tabelle2 is empty")
    headers = parsed_rows[0][1]
    by_inventory: dict[str, dict[str, str]] = {}
    for row_number, cells in parsed_rows[1:]:
        record = {headers.get(index, f"column_{index}"): value for index, value in cells.items() if value != ""}
        inventory = record.get("Inventarnummer", "")
        if inventory:
            record["_sheet"] = "Tabelle2"
            record["_row"] = str(row_number)
            by_inventory[normalize_inventory(inventory)] = record
    return by_inventory, {
        "sheet": "Tabelle2",
        "sheet_rows": len(parsed_rows),
        "indexed_inventory_rows": len(by_inventory),
        "shared_strings": len(shared),
    }


def unique_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def split_keywords(value: str) -> list[str]:
    return unique_values(piece.strip() for piece in re.split(r"[;,]", value or ""))


def text_norm(value: str) -> str:
    return re.sub(r"\W+", "", value.casefold(), flags=re.UNICODE)


def reconciled_record(
    md_id: str,
    object_row: dict[str, str],
    md_object: dict[str, Any],
    local: dict[str, str],
    workbook_sha: str,
) -> dict[str, Any]:
    md = base.descriptive_metadata(md_object, object_row["archival_object_id"], object_row["object_url"])
    local_description = local.get("Beschreibung", "").strip()
    local_inscription = local.get("Beschriftung", "").strip()
    local_condition = local.get("Erhaltungszustand", "").strip()
    final_title = md["title"] or local_description
    final_description = md["description"] or local_description
    embedded_parts = [final_description]
    if local_description and text_norm(local_description) not in text_norm(final_description):
        embedded_parts.append(f"Primus-Beschreibung: {local_description}")
    if local_inscription:
        embedded_parts.append(f"Beschriftung/Rückseite: {local_inscription}")
    if local_condition:
        embedded_parts.append(f"Erhaltungszustand: {local_condition}")
    final_material = md["material_technique"] or local.get("Material", "").strip()
    final_dimensions = md["dimensions"] or local.get("Masse", "").strip()
    final_object_type = md["object_type"] or local.get("Objektbezeichnung", "").strip()
    final_date = md["date"] or local.get("Datierung", "").strip()
    final_places = "; ".join(unique_values([md["places"], local.get("Verwendungsort", "")]))
    final_tags = "; ".join(unique_values(split_keywords(md["tags"]) + split_keywords(local.get("Schlagwort", ""))))
    local_payload = {key: value for key, value in local.items() if not key.startswith("_")}
    local_digest = hashlib.sha256(
        json.dumps(local_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "archival_object_id": object_row["archival_object_id"],
        "md_object_id": md_id,
        "inventory": object_row["inventory"],
        "primus_inventory": local.get("Inventarnummer", ""),
        "primus_object_id": local.get("Objekt-ID", ""),
        "primus_sheet": local["_sheet"],
        "primus_row": local["_row"],
        "primus_row_sha256": local_digest,
        "primus_workbook_sha256": workbook_sha,
        "md_title": md["title"],
        "primus_description": local_description,
        "final_title": final_title,
        "title_basis": "museum-digital current title; Primus fallback only",
        "md_description": md["description"],
        "primus_inscription": local_inscription,
        "primus_condition": local_condition,
        "final_description": final_description,
        "embedded_description": "; ".join(unique_values(embedded_parts)),
        "description_basis": "museum-digital current description with distinct Primus description, transcription, and condition retained as local evidence",
        "md_object_type": md["object_type"],
        "primus_object_type": local.get("Objektbezeichnung", ""),
        "final_object_type": final_object_type,
        "md_material_technique": md["material_technique"],
        "primus_material": local.get("Material", ""),
        "final_material_technique": final_material,
        "md_dimensions": md["dimensions"],
        "primus_dimensions": local.get("Masse", ""),
        "final_dimensions": final_dimensions,
        "md_date": md["date"],
        "primus_date": local.get("Datierung", ""),
        "final_date": final_date,
        "md_places": md["places"],
        "primus_place": local.get("Verwendungsort", ""),
        "final_places": final_places,
        "md_tags": md["tags"],
        "primus_keywords": local.get("Schlagwort", ""),
        "final_tags": final_tags,
        "creator": md["creator"],
        "rights": md["rights"],
        "rights_holder": md["rights_holder"],
        "institution": md["institution"],
        "collection": md["collection"],
        "metadata_rights": md["metadata_rights"],
        "metadata_rights_holder": md["metadata_rights_holder"],
        "primus_storage_location": local.get("Standort", ""),
        "primus_image_path_1": local.get("Bildpfad", ""),
        "primus_image_path_2": local.get("Bildpfad2", ""),
        "primus_image_count": local.get("AnzahlBilder", ""),
        "reconciliation_state": "md_current_with_local_primus_enrichment",
        "deletion_authorized": "false",
    }


RECON_FIELDS = [
    "archival_object_id", "md_object_id", "inventory", "primus_inventory", "primus_object_id", "primus_sheet", "primus_row",
    "primus_row_sha256", "primus_workbook_sha256", "md_title", "primus_description", "final_title", "title_basis",
    "md_description", "primus_inscription", "primus_condition", "final_description", "embedded_description", "description_basis",
    "md_object_type", "primus_object_type", "final_object_type", "md_material_technique", "primus_material",
    "final_material_technique", "md_dimensions", "primus_dimensions", "final_dimensions", "md_date", "primus_date",
    "final_date", "md_places", "primus_place", "final_places", "md_tags", "primus_keywords", "final_tags", "creator",
    "rights", "rights_holder", "institution", "collection", "metadata_rights", "metadata_rights_holder",
    "primus_storage_location", "primus_image_path_1", "primus_image_path_2", "primus_image_count", "reconciliation_state",
    "deletion_authorized",
]


CATALOG_FIELDS = [
    "archival_object_id", "inventory", "primus_inventory", "title", "description", "object_type", "material_technique",
    "dimensions", "date", "places", "creator", "rights", "rights_holder", "tags", "primus_inscription", "primus_condition",
    "primus_storage_location", "md_object_id", "md_object_url", "md_main_resource_id", "md_main_resource_url",
    "master_relative_path", "master_sha256", "master_size_bytes", "master_width", "master_height", "master_resolution_class",
    "original_source_path", "tiff_derivative_relative_path", "tiff_derivative_sha256", "jpeg_derivative_relative_path",
    "jpeg_derivative_sha256", "related_resource_paths", "metadata_jsonl", "metadata_reconciliation_csv", "reconciliation_state",
    "accessioned_utc", "deletion_authorized",
]


REP_FIELDS = base.REP_FIELDS
DOC_FIELDS = [
    "document_id", "filename", "role", "format", "relative_path", "original_source_path", "source_sha256", "sha256",
    "size_bytes", "integrity_state", "derivative_of", "coverage", "review_note", "deletion_authorized",
]
CHECK_FIELDS = base.CHECK_FIELDS


def reconciled_tags(
    record: dict[str, Any], source_url: str, master_relative: str, role: str, master_sha: str,
) -> dict[str, str]:
    local_reference = (
        f"{PRIMUS_SOURCE}#Tabelle2!{record['primus_row']}; sha256:{record['primus_workbook_sha256']}; "
        f"row sha256:{record['primus_row_sha256']}"
    )
    tags: dict[str, str] = {
        "XMP-dc:Identifier": record["archival_object_id"],
        "XMP-dc:Title": record["final_title"],
        "XMP-dc:Description": record["embedded_description"],
        "XMP-dc:Type": record["final_object_type"],
        "XMP-dc:Creator": record["creator"],
        "XMP-dc:Coverage": record["final_places"],
        "XMP-dc:Rights": "; ".join(unique_values([record["rights"], record["rights_holder"]])),
        "XMP-dc:Subject": record["final_tags"],
        "XMP-dc:Source": (
            f"Generated from {master_relative}; Master sha256:{master_sha}; museum-digital resource {source_url}; "
            f"local Primus evidence {local_reference}"
        ),
        "XMP-dc:Relation": (
            f"iss:unit:pressebildarchiv-wf; museum-digital:object:{record['md_object_id']}; "
            f"iss:representation:{role}; workbook-record:sha256:{record['primus_row_sha256']}"
        ),
        "XMP-xmp:Label": PROFILE,
        "XMP-iptcExt:ArtworkSourceInventoryNo": record["primus_inventory"],
        "XMP-iptcExt:ArtworkTitle": record["final_title"],
        "XMP-iptcExt:ArtworkContentDescription": record["embedded_description"],
        "XMP-iptcExt:ArtworkPhysicalDescription": "; ".join(
            unique_values([
                f"Material/Technik: {record['final_material_technique']}" if record["final_material_technique"] else "",
                f"Maße: {record['final_dimensions']}" if record["final_dimensions"] else "",
            ])
        ),
    }
    date_value = record["final_date"]
    if re.fullmatch(r"(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?", date_value):
        tags["XMP-dc:Date"] = date_value
    elif date_value:
        tags["XMP-iptcExt:ArtworkCircaDateCreated"] = date_value
    return {key: value for key, value in tags.items() if value}


def exif_read(path: Path, tags: dict[str, str]) -> dict[str, Any]:
    arguments = ["exiftool", "-j"] + [f"-{key}" for key in tags] + [str(path)]
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=120)
    return json.loads(result.stdout)[0]


def expected_exif_key(tag: str) -> str:
    return tag.split(":", 1)[1]


def verify_tags(path: Path, tags: dict[str, str]) -> None:
    observed = exif_read(path, tags)
    for tag, value in tags.items():
        key = expected_exif_key(tag)
        actual = observed.get(key)
        if actual is not None and not isinstance(actual, str):
            actual = str(actual)
        if tag == "XMP-dc:Date" and isinstance(actual, str):
            actual = actual.replace(":", "-", 2)
        if actual != value:
            raise CorrectionError(f"metadata readback differs for {path}: {tag} expected={value!r}, observed={actual!r}")


def document_row(
    *, document_id: str, source: Path, relative_path: str, role: str,
    integrity_state: str, coverage: str, review_note: str,
    target_source: Path | None = None, derivative_of: str = "",
) -> dict[str, Any]:
    target_source = target_source or source
    digest, size = base.copy_exact(target_source, STAGE / relative_path)
    source_digest, _ = base.hash_file(source)
    return {
        "document_id": document_id,
        "filename": Path(relative_path).name,
        "role": role,
        "format": Path(relative_path).suffix.lower().lstrip("."),
        "relative_path": relative_path,
        "original_source_path": str(source),
        "source_sha256": source_digest,
        "sha256": digest,
        "size_bytes": size,
        "integrity_state": integrity_state,
        "derivative_of": derivative_of,
        "coverage": coverage,
        "review_note": review_note,
        "deletion_authorized": "false",
    }


def write_sqlite(
    catalog: list[dict[str, Any]], representations: list[dict[str, Any]],
    documents: list[dict[str, Any]], source_rows: list[dict[str, str]],
) -> dict[str, int]:
    path = STAGE / "db-restore/RESTORE.sqlite"
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            CREATE TABLE objects (
                archival_object_id TEXT PRIMARY KEY, md_object_id TEXT UNIQUE NOT NULL,
                inventory TEXT UNIQUE NOT NULL, primus_inventory TEXT NOT NULL, title TEXT NOT NULL,
                master_relative_path TEXT UNIQUE NOT NULL, master_sha256 TEXT NOT NULL,
                tiff_derivative_relative_path TEXT UNIQUE NOT NULL, jpeg_derivative_relative_path TEXT UNIQUE NOT NULL,
                reconciliation_state TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE representations (
                relative_path TEXT PRIMARY KEY, archival_object_id TEXT NOT NULL REFERENCES objects(archival_object_id),
                role TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, integrity_state TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE source_disposition (
                source_path TEXT PRIMARY KEY, md_object_id TEXT, relationship TEXT NOT NULL, final_archive_path TEXT NOT NULL,
                drain_state TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            """
        )
        con.executemany(
            "INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            [(r["archival_object_id"], r["md_object_id"], r["inventory"], r["primus_inventory"], r["title"], r["master_relative_path"], r["master_sha256"], r["tiff_derivative_relative_path"], r["jpeg_derivative_relative_path"], r["reconciliation_state"]) for r in catalog],
        )
        con.executemany(
            "INSERT INTO representations VALUES (?,?,?,?,?,0)",
            [(r["relative_path"], r["archival_object_id"], r["role"], r["sha256"], int(r["size_bytes"])) for r in representations],
        )
        con.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,0)",
            [(r["document_id"], r["relative_path"], r["role"], r["sha256"], int(r["size_bytes"]), r["integrity_state"]) for r in documents],
        )
        con.executemany(
            "INSERT INTO source_disposition VALUES (?,?,?,?,?,0)",
            [(r["source_path"], r["md_object_id"], r["relationship"], r["final_archive_path"], r["drain_state"]) for r in source_rows],
        )
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("objects", "representations", "documents", "source_disposition")}
        if integrity != "ok" or foreign:
            raise CorrectionError(f"SQLite validation failed: integrity={integrity}, foreign={foreign}")
    finally:
        con.close()
    return counts


def build_corrected() -> dict[str, Any]:
    started = base.now()
    mount = base.source_guard()
    base.validate_unit()
    if STAGE.exists() or PREDECESSOR.exists():
        raise CorrectionError("correction stage or predecessor already exists")
    if INPUTS.exists():
        if not RECOVERED.is_file() or not RECOVERY_RECEIPT.is_file():
            raise CorrectionError("existing correction inputs are incomplete")
        recovery = json.loads(RECOVERY_RECEIPT.read_text(encoding="utf-8"))
        if member_digests(RECOVERED) is None or not recovery.get("member_bytes_identical"):
            raise CorrectionError("existing recovered workbook input is not validated")
    else:
        recovery = recover_workbook()
    local_by_inventory, workbook_summary = workbook_rows(RECOVERED)
    old_catalog = read_csv(CURRENT / "catalog.csv")
    old_reps = read_csv(CURRENT / "00_MANIFESTS/representations.csv")
    old_drain = read_csv(CURRENT / "00_MANIFESTS/drain-list.csv")
    objects = {row["md_object_id"]: row for row in read_csv(base.OBJECTS)}
    md_objects = base.md_objects()
    resources = read_csv(base.RESOURCES)
    resource_by_md: dict[str, list[dict[str, str]]] = {}
    for row in resources:
        resource_by_md.setdefault(row["md_object_id"], []).append(row)
    current_rep_by_path = {row["relative_path"]: row for row in old_reps}
    workbook_sha = recovery["source_sha256"]

    for name in ("00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"):
        (STAGE / name).mkdir(parents=True, exist_ok=False)

    catalog: list[dict[str, Any]] = []
    reps: list[dict[str, Any]] = []
    reconciliation: list[dict[str, Any]] = []
    metadata_jsonl: list[str] = []
    accessioned = base.now()
    for old in sorted(old_catalog, key=lambda row: int(row["md_object_id"])):
        md_id = old["md_object_id"]
        lookup_inventory = "IS50895" if old["inventory"] == "KS-8-WF_IS50895" else old["inventory"]
        local = local_by_inventory.get(normalize_inventory(lookup_inventory))
        if local is None:
            raise CorrectionError(f"Primus evidence missing for {old['inventory']}")
        record = reconciled_record(md_id, objects[md_id], md_objects[md_id], local, workbook_sha)
        reconciliation.append(record)
        master_rel = old["master_relative_path"]
        tiff_rel = old["tiff_derivative_relative_path"]
        jpeg_rel = old["jpeg_derivative_relative_path"]
        master_sha, master_size = base.copy_exact(CURRENT / master_rel, STAGE / master_rel)
        if master_sha != old["master_sha256"]:
            raise CorrectionError(f"current Master hash differs for {old['inventory']}")
        decoded, width, height = base.decoded_digest(STAGE / master_rel)
        main_resource = next(row for row in resource_by_md[md_id] if row["is_main"] == "true")
        tiff_tags = reconciled_tags(record, main_resource["resource_url"], master_rel, "full_resolution_tiff_derivative", master_sha)
        jpeg_tags = reconciled_tags(record, main_resource["resource_url"], master_rel, "full_resolution_jpeg_derivative", master_sha)
        tiff_info, jpeg_info, verified_decoded = base.generate_derivatives(
            STAGE / master_rel, STAGE / tiff_rel, STAGE / jpeg_rel, tiff_tags, jpeg_tags,
        )
        if decoded != verified_decoded:
            raise CorrectionError(f"decoded Master digest changed for {old['inventory']}")
        verify_tags(STAGE / tiff_rel, tiff_tags)
        verify_tags(STAGE / jpeg_rel, jpeg_tags)
        original_master_rep = current_rep_by_path[master_rel]
        reps.extend([
            {**original_master_rep, "sha256": master_sha, "size_bytes": master_size, "width": width, "height": height, "metadata_embedding_profile": "unchanged_source_bytes; reconciled metadata in generated derivatives and metadata records"},
            {**current_rep_by_path[tiff_rel], "sha256": tiff_info["sha256"], "size_bytes": tiff_info["size_bytes"], "width": width, "height": height, "decoded_pixel_sha256": decoded, "metadata_embedding_profile": PROFILE, "provenance": "accession correction generated from selected Master; MD and local Primus metadata reconciled; decoded pixels verified equal"},
            {**current_rep_by_path[jpeg_rel], "sha256": jpeg_info["sha256"], "size_bytes": jpeg_info["size_bytes"], "width": width, "height": height, "metadata_embedding_profile": PROFILE, "provenance": "accession correction generated at Master pixel dimensions; MD and local Primus metadata reconciled"},
        ])
        related_paths: list[str] = []
        for related in sorted((r for r in old_reps if r["md_object_id"] == md_id and r["role"] == "related_catalogue_image_original"), key=lambda row: row["relative_path"]):
            digest, size = base.copy_exact(CURRENT / related["relative_path"], STAGE / related["relative_path"])
            if digest != related["sha256"]:
                raise CorrectionError(f"related catalogue image hash differs: {related['relative_path']}")
            reps.append({**related, "sha256": digest, "size_bytes": size})
            related_paths.append(related["relative_path"])
        md_record = dict(md_objects[md_id])
        md_record["local_primus_evidence"] = {key: value for key, value in local.items() if not key.startswith("_")}
        md_record["metadata_reconciliation"] = record
        md_record["archive_accession"] = {
            "unit": "pressebildarchiv-wf", "selected_master": master_rel,
            "generated_derivatives": [tiff_rel, jpeg_rel], "related_resource_paths": related_paths,
            "metadata_profile": PROFILE, "accessioned_utc": accessioned, "deletion_authorized": False,
        }
        metadata_jsonl.append(json.dumps(md_record, ensure_ascii=False, sort_keys=True))
        catalog.append({
            "archival_object_id": record["archival_object_id"], "inventory": old["inventory"], "primus_inventory": record["primus_inventory"],
            "title": record["final_title"], "description": record["final_description"], "object_type": record["final_object_type"],
            "material_technique": record["final_material_technique"], "dimensions": record["final_dimensions"], "date": record["final_date"],
            "places": record["final_places"], "creator": record["creator"], "rights": record["rights"], "rights_holder": record["rights_holder"],
            "tags": record["final_tags"], "primus_inscription": record["primus_inscription"], "primus_condition": record["primus_condition"],
            "primus_storage_location": record["primus_storage_location"], "md_object_id": md_id, "md_object_url": objects[md_id]["object_url"],
            "md_main_resource_id": main_resource["resource_id"], "md_main_resource_url": main_resource["resource_url"],
            "master_relative_path": master_rel, "master_sha256": master_sha, "master_size_bytes": master_size, "master_width": width,
            "master_height": height, "master_resolution_class": old["master_resolution_class"], "original_source_path": old["original_source_path"],
            "tiff_derivative_relative_path": tiff_rel, "tiff_derivative_sha256": tiff_info["sha256"],
            "jpeg_derivative_relative_path": jpeg_rel, "jpeg_derivative_sha256": jpeg_info["sha256"],
            "related_resource_paths": "; ".join(related_paths), "metadata_jsonl": "metadata/objects.jsonl",
            "metadata_reconciliation_csv": "metadata/metadata-reconciliation.csv", "reconciliation_state": record["reconciliation_state"],
            "accessioned_utc": accessioned, "deletion_authorized": "false",
        })
        print(json.dumps({"reconciled_object": md_id, "inventory": old["inventory"], "objects_complete": len(catalog)}), flush=True)

    atomic_bytes(STAGE / "metadata/objects.jsonl", ("\n".join(metadata_jsonl) + "\n").encode("utf-8"))
    atomic_csv(STAGE / "metadata/metadata-reconciliation.csv", RECON_FIELDS, reconciliation)
    atomic_json(STAGE / "metadata/primus-workbook-recovery.json", recovery)

    documents = [
        document_row(
            document_id="DOC-F4D329FD08D1799CCF52F4E7595D5A5F", source=PRIMUS_SOURCE,
            relative_path="documents/Inventar Primus 1 und 2.xlsx", role="local_metadata_source_workbook",
            integrity_state="recoverable_container_with_trailing_bytes", coverage="48 of 48 objects through Tabelle2 inventory rows",
            review_note="Original source bytes retained unchanged; all 19 OOXML members are recoverable.",
        ),
        document_row(
            document_id="PBA-DOC-RECOVERED-001", source=PRIMUS_SOURCE, target_source=RECOVERED,
            relative_path="export/Inventar Primus 1 und 2_RECOVERED.xlsx", role="recovered_metadata_workbook_derivative",
            integrity_state="zip_and_xml_validated", derivative_of="documents/Inventar Primus 1 und 2.xlsx",
            coverage="48 of 48 objects through Tabelle2 inventory rows",
            review_note="Generated by extracting and repackaging all 19 OOXML members without changing member bytes.",
        ),
        document_row(
            document_id="DOC-F977DAF2ED35BB9B4A0611ADD301431A", source=PRIMUS_EXPORT_XLSX,
            relative_path=f"documents/{PRIMUS_EXPORT_XLSX.name}", role="local_metadata_export_workbook",
            integrity_state="readable", coverage="40 of 48 objects by exact current inventory number",
            review_note="Structured Primus export with a corresponding CSV representation.",
        ),
        document_row(
            document_id="DOC-1391F6378993B131B4A1678AB5B9CA0E", source=PRIMUS_EXPORT_CSV,
            relative_path=f"documents/{PRIMUS_EXPORT_CSV.name}", role="local_metadata_export_csv",
            integrity_state="readable", coverage="40 of 48 objects by exact current inventory number",
            review_note="726 rows and 30 structured fields; human-readable export evidence.",
        ),
    ]

    source_rows = [
        dict(row) for row in old_drain
        if not (
            row["source_kind"] == "document"
            and Path(row["source_path"]).name in GENERAL_DOCUMENTS
        )
    ]
    if len(source_rows) != len(old_drain) - 3:
        raise CorrectionError("general document drain removal count differs")
    original_final = "documents/Inventar Primus 1 und 2.xlsx"
    for row in source_rows:
        if row["source_path"] == str(PRIMUS_SOURCE):
            row["final_archive_path"] = original_final
            row["relationship"] = "local_metadata_evidence_input"
            row["evidence_state"] = "recoverable_container_with_trailing_bytes"
            row["note"] = "Original Primus workbook retained unchanged; validated recovered derivative is in export/."
    for source, final, evidence, note in (
        (PRIMUS_EXPORT_XLSX, f"documents/{PRIMUS_EXPORT_XLSX.name}", "readable", "Structured Primus metadata export; covers 40 current objects."),
        (PRIMUS_EXPORT_CSV, f"documents/{PRIMUS_EXPORT_CSV.name}", "readable", "Human-readable 726-row, 30-field Primus metadata export; covers 40 current objects."),
    ):
        digest, size = base.hash_file(source)
        source_rows.append({
            "md_object_id": "", "inventory": "", "source_path": str(source), "source_class": "protected_usb_source",
            "source_kind": "document", "relationship": "local_metadata_evidence_input", "evidence_state": evidence,
            "source_sha256": f"sha256:{digest}", "size_bytes": str(size), "selected_master_candidate_id": "",
            "selected_master_path": "", "covered_by_unit": "true", "coverage_state": "validated_accession_coverage",
            "final_archive_path": final, "drain_state": "covered_by_validated_accession", "deletion_authorized": "false", "note": note,
        })
    source_rows.sort(key=lambda row: (row["source_class"], row["source_kind"], row["source_path"]))
    if len(source_rows) != EXPECTED_DRAIN or len({row["source_path"] for row in source_rows}) != EXPECTED_DRAIN:
        raise CorrectionError(f"corrected source/drain count differs: {len(source_rows)}")

    atomic_csv(STAGE / "catalog.csv", CATALOG_FIELDS, catalog)
    atomic_csv(STAGE / "00_MANIFESTS/representations.csv", REP_FIELDS, reps)
    atomic_csv(STAGE / "00_MANIFESTS/documents.csv", DOC_FIELDS, documents)
    atomic_csv(STAGE / "00_MANIFESTS/source-disposition.csv", list(source_rows[0]), source_rows)
    atomic_csv(STAGE / "00_MANIFESTS/drain-list.csv", list(source_rows[0]), source_rows)
    shutil.copyfile(CURRENT / "00_MANIFESTS/master-decisions.csv", STAGE / "00_MANIFESTS/master-decisions.csv")
    shutil.copyfile(CURRENT / "00_MANIFESTS/cross-unit-overlap.csv", STAGE / "00_MANIFESTS/cross-unit-overlap.csv")
    sqlite_counts = write_sqlite(catalog, reps, documents, source_rows)

    readme = f"""# Pressebildarchiv WF

This unit is the authoritative archive representation for the 48 reconciled objects in
museum-digital collection 555. Museum-digital is a projection of this unit.

- Archival objects and explicitly selected Masters: 48
- Accession-generated metadata-bearing TIFF derivatives: 48
- Accession-generated metadata-bearing JPEG derivatives: 48
- Retained additional museum-digital image resources: 13
- Primus metadata source/export documents: 3
- Recovered Primus workbook derivative: 1
- Comprehensive source/drain rows: {len(source_rows)}
- Deletion authorized: false

`masters/` contains exactly one unchanged selected Master per object. `export/` contains
the image derivatives generated from those Masters, the additional museum-digital images,
and the recovered Primus workbook. The TIFF derivatives have decoded pixels equal to their
Masters. The generated image derivatives embed current museum-digital metadata reconciled
with the local Primus record for every object using `{PROFILE}`.

The original `documents/Inventar Primus 1 und 2.xlsx` remains byte-for-byte unchanged. Its
ZIP has 70,118 trailing bytes, but all 19 OOXML members were extracted successfully and are
preserved byte-identically in `export/Inventar Primus 1 und 2_RECOVERED.xlsx`. Recovery
details are in `metadata/primus-workbook-recovery.json`.

The two `Primus_fuer_ md-50429-51200-Stand-25-1-19-1` files are local structured exports
found under `/WF/Archiv/`; the CSV contains 726 rows and 30 fields. Per-object local evidence,
field selection, source row, and row digest are recorded in
`metadata/metadata-reconciliation.csv` and `metadata/objects.jsonl`.

General photographic handling, conservation, and classification working rules are not part
of this unit and are not covered by its drain list. Their protected USB sources remain
unchanged. Drain coverage does not authorize deletion; USB cleanup remains a separate future
decision. SQLite is rebuildable from the CSV, JSONL, Markdown, and filesystem records.
"""
    atomic_bytes(STAGE / "00_README.md", readme.encode("utf-8"))

    excluded = {"00_MANIFESTS/checksums.csv", "00_MANIFESTS/accession-completion.json"}
    checksum_rows: list[dict[str, Any]] = []
    for path in sorted(item for item in STAGE.rglob("*") if item.is_file()):
        relative = path.relative_to(STAGE).as_posix()
        if relative in excluded:
            continue
        digest, size = base.hash_file(path)
        payload_type = "unit_record"
        if relative.startswith("masters/"):
            payload_type = "selected_master"
        elif relative.startswith("export/"):
            payload_type = "export_representation"
        elif relative.startswith("documents/"):
            payload_type = "local_metadata_document"
        checksum_rows.append({"relative_path": relative, "size_bytes": size, "sha256": digest, "payload_type": payload_type})
    atomic_csv(STAGE / "00_MANIFESTS/checksums.csv", CHECK_FIELDS, checksum_rows)
    checksum_sha, checksum_size = base.hash_file(STAGE / "00_MANIFESTS/checksums.csv")
    completion = {
        "format": "iss-pressebildarchiv-wf-accession-completion-v2",
        "started_utc": started, "completed_utc": base.now(), "target_unit": str(CURRENT), "source_mount": mount,
        "objects": 48, "selected_masters": 48, "full_resolution_tiff_derivatives": 48,
        "full_resolution_jpeg_derivatives": 48, "related_catalogue_images": 13,
        "local_metadata_source_documents": 3, "recovered_metadata_workbook_derivatives": 1,
        "general_archive_work_rule_documents": 0, "metadata_reconciliation_rows": 48,
        "metadata_embedding_profile": PROFILE, "metadata_readback_verified": True,
        "tiff_decoded_pixel_equality_verified": True, "primus_objects_covered": 48,
        "primus_recovery_member_bytes_identical": True, "drain_rows": len(source_rows),
        "drain_deletion_authorized": False, "sqlite_integrity": "ok", "sqlite_counts": sqlite_counts,
        "checksum_rows": len(checksum_rows), "checksum_manifest_sha256": checksum_sha,
        "checksum_manifest_size_bytes": checksum_size, "accession_verified": True, "deletion_authorized": False,
    }
    atomic_json(STAGE / "00_MANIFESTS/accession-completion.json", completion)
    validate_root(STAGE, expect_published=False)
    os.sync()
    os.replace(CURRENT, PREDECESSOR)
    os.replace(STAGE, CURRENT)
    os.sync()
    validate_root(CURRENT, expect_published=True)
    print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return completion


def validate_root(root: Path, *, expect_published: bool) -> dict[str, Any]:
    mount = base.source_guard()
    if not root.is_dir():
        raise CorrectionError(f"unit root missing: {root}")
    required_dirs = {"00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"}
    if {path.name for path in root.iterdir() if path.is_dir()} != required_dirs:
        raise CorrectionError("approved unit directory structure differs")
    catalog = read_csv(root / "catalog.csv")
    reps = read_csv(root / "00_MANIFESTS/representations.csv")
    documents = read_csv(root / "00_MANIFESTS/documents.csv")
    drain = read_csv(root / "00_MANIFESTS/drain-list.csv")
    source = read_csv(root / "00_MANIFESTS/source-disposition.csv")
    recon = read_csv(root / "metadata/metadata-reconciliation.csv")
    checksums = read_csv(root / "00_MANIFESTS/checksums.csv")
    if len(catalog) != 48 or len(recon) != 48 or len({row["primus_inventory"] for row in recon}) != 48:
        raise CorrectionError("object or Primus reconciliation coverage differs")
    roles = Counter(row["role"] for row in reps)
    expected_roles = Counter({"selected_master": 48, "full_resolution_tiff_derivative": 48, "full_resolution_jpeg_derivative": 48, "related_catalogue_image_original": 13})
    if roles != expected_roles or len(reps) != EXPECTED_IMAGE_REPRESENTATIONS:
        raise CorrectionError(f"representation roles differ: {roles}")
    expected_document_roles = Counter({"local_metadata_source_workbook": 1, "recovered_metadata_workbook_derivative": 1, "local_metadata_export_workbook": 1, "local_metadata_export_csv": 1})
    if len(documents) != EXPECTED_DOCUMENTS or Counter(row["role"] for row in documents) != expected_document_roles:
        raise CorrectionError("corrected document roles differ")
    if any(name in {Path(row["relative_path"]).name for row in documents} for name in GENERAL_DOCUMENTS):
        raise CorrectionError("a general work-rule document remains in the unit")
    if len(drain) != EXPECTED_DRAIN or source != drain:
        raise CorrectionError("source/drain manifests differ")
    if any(Path(row["source_path"]).name in GENERAL_DOCUMENTS for row in drain):
        raise CorrectionError("a general work-rule source remains in drain coverage")
    if any(row["deletion_authorized"] != "false" for row in catalog + reps + documents + drain + recon):
        raise CorrectionError("a corrected record carries deletion authority")
    excluded = {"00_MANIFESTS/checksums.csv", "00_MANIFESTS/accession-completion.json"}
    actual_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path.relative_to(root).as_posix() not in excluded}
    listed_files = {row["relative_path"] for row in checksums}
    if actual_files != listed_files:
        raise CorrectionError(f"checksum paths differ: missing={actual_files-listed_files}, extra={listed_files-actual_files}")
    for row in checksums:
        digest, size = base.hash_file(root / row["relative_path"])
        if digest != row["sha256"] or size != int(row["size_bytes"]):
            raise CorrectionError(f"checksum differs: {row['relative_path']}")
    recon_by_id = {row["archival_object_id"]: row for row in recon}
    for row in catalog:
        master = root / row["master_relative_path"]
        tiff = root / row["tiff_derivative_relative_path"]
        jpeg = root / row["jpeg_derivative_relative_path"]
        master_decoded, mw, mh = base.decoded_digest(master)
        tiff_decoded, tw, th = base.decoded_digest(tiff)
        _, jw, jh = base.decoded_digest(jpeg)
        if master_decoded != tiff_decoded or (mw, mh) != (tw, th) or (mw, mh) != (jw, jh):
            raise CorrectionError(f"derivative pixel/dimension validation differs: {row['inventory']}")
        evidence = recon_by_id[row["archival_object_id"]]
        for derivative in (tiff, jpeg):
            tags = {
                "XMP-dc:Identifier": row["archival_object_id"],
                "XMP-dc:Title": row["title"],
                "XMP-dc:Description": evidence["embedded_description"],
                "XMP-xmp:Label": PROFILE,
                "XMP-iptcExt:ArtworkSourceInventoryNo": row["primus_inventory"],
                "XMP-iptcExt:ArtworkPhysicalDescription": "; ".join(unique_values([
                    f"Material/Technik: {row['material_technique']}" if row["material_technique"] else "",
                    f"Maße: {row['dimensions']}" if row["dimensions"] else "",
                ])),
            }
            verify_tags(derivative, {key: value for key, value in tags.items() if value})
    with zipfile.ZipFile(root / "export/Inventar Primus 1 und 2_RECOVERED.xlsx") as archive:
        if archive.testzip() or len(archive.namelist()) != 19:
            raise CorrectionError("recovered workbook ZIP validation differs")
    recovery = json.loads((root / "metadata/primus-workbook-recovery.json").read_text(encoding="utf-8"))
    if not recovery.get("member_bytes_identical") or recovery.get("source_modified"):
        raise CorrectionError("workbook recovery receipt differs")
    con = sqlite3.connect(root / "db-restore/RESTORE.sqlite")
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        sqlite_counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("objects", "representations", "documents", "source_disposition")}
    finally:
        con.close()
    expected_counts = {"objects": 48, "representations": 157, "documents": 4, "source_disposition": 120}
    if integrity != "ok" or foreign or sqlite_counts != expected_counts:
        raise CorrectionError(f"SQLite validation differs: integrity={integrity}, foreign={foreign}, counts={sqlite_counts}")
    completion = json.loads((root / "00_MANIFESTS/accession-completion.json").read_text(encoding="utf-8"))
    if completion.get("format") != "iss-pressebildarchiv-wf-accession-completion-v2" or not completion.get("accession_verified") or completion.get("deletion_authorized"):
        raise CorrectionError("corrected completion receipt differs")
    result = {
        "result": "ok", "root": str(root), "published": expect_published, "source_mount": mount,
        "objects": 48, "representations": dict(roles), "documents": dict(expected_document_roles),
        "metadata_reconciliation_rows": 48, "drain_rows": 120, "deletion_authorized_rows": 0,
        "metadata_profile": PROFILE, "metadata_readback_verified": True,
        "tiff_decoded_pixel_equality_verified": True, "recovered_workbook_members": 19,
        "sqlite_integrity": "ok", "sqlite_counts": sqlite_counts, "checksum_rows": len(checksums),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--correct", action="store_true")
    group.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        if args.correct:
            build_corrected()
        else:
            validate_root(CURRENT, expect_published=True)
        return 0
    except Exception as error:
        print(json.dumps({"result": "error", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
