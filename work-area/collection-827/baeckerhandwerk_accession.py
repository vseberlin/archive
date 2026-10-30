#!/usr/bin/env python3
"""Accession the prepared museum-digital collection 827 Bäckerhandwerk unit."""

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
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


ARCHIVE_TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
if str(ARCHIVE_TOOLS) not in sys.path:
    sys.path.insert(0, str(ARCHIVE_TOOLS))
import tiff_accession  # noqa: E402
import tiff_metadata  # noqa: E402


WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-827")
PREPARED = WORK_ROOT / "work/accession"
UNIT = Path("/mnt/volume/iss-archive/units/baeckerhandwerk")
RECOVERY_UNIT = WORK_ROOT / "work/pre-metadata-unit-20260828"
SOURCE_ROOT = Path("/media/vladimir/Elements")
BAECKER_SOURCE_ROOT = SOURCE_ROOT / "restore/archive/WF/Baecker"
DECISIONS = PREPARED / "accession-decisions.csv"
MD_CSV = PREPARED / "museum-digital-objects.csv"
MD_JSON = PREPARED / "museum-digital-objects.json"
SOURCE_DISPOSITION = PREPARED / "source-disposition.csv"
DRAIN_LIST = PREPARED / "drain-list.csv"
DOCUMENT_DISPOSITION = PREPARED / "document-source-disposition.csv"
DRAIN_SUMMARY = PREPARED / "drain-review-summary.json"
PREPARATION = PREPARED / "accession-preparation.json"
EXPECTED_OBJECTS = 76
EXPECTED_SOURCE_ROWS = 92
EXPECTED_SOURCE_BYTES = 23_892_232_954
BLOCK = 8 * 1024 * 1024
MASTER_PROFILE = "iss-tiff-v2+baeckerhandwerk-1"
ACCESS_PROFILE = "iss-jpeg-v1+baeckerhandwerk-1"
PROVENANCE_CASE = "collection-827-baeckerhandwerk-001"
DRAIN_FIELDS = [
    "source_path", "source_kind", "unit", "inventory", "source_role", "final_path",
    "preservation_status", "preservation_basis", "source_sha256", "final_sha256",
    "source_size_bytes", "exact_copy_proven", "drain_state", "deletion_authorized", "notes",
]
DOCUMENT_DISPOSITION_FIELDS = [
    "source_path", "filename", "format", "size_bytes", "sha256", "disposition",
    "basis", "selected_document_id", "deletion_authorized",
]
DOCUMENT_SPECS = (
    {
        "document_id": "BAECKER-DOC-001",
        "title": "101 x Bäckerhandwerk in Berlin - final publication",
        "role": "publication_master",
        "format": "pdf",
        "source_path": "/media/vladimir/Elements/restore/archive/WF/Baecker/P220240_Chronik_Bäckerinnung_165x240_241014.pdf",
        "final_relative_path": "documents/final-publication-2024.pdf",
        "condition": "readable",
        "selection_basis": "content_review_confirmed_final_2024_bebra_publication",
    },
    {
        "document_id": "BAECKER-DOC-002",
        "title": "101 x Bäckerhandwerk in Berlin - final illustrated manuscript",
        "role": "editorial_manuscript_master",
        "format": "docx",
        "source_path": "/media/vladimir/Elements/restore/archive/WF/Baecker/101xBaeckerhandwerk__mit-Abb._final_03.03.2024.docx",
        "final_relative_path": "documents/final-illustrated-manuscript-2024.docx",
        "condition": "readable",
        "selection_basis": "content_review_confirmed_complete_illustrated_editorial_source",
    },
)


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def copy_exact(source: Path, target: Path) -> tuple[str, str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(BLOCK), b""):
            source_digest.update(block)
            outgoing.write(block)
            size += len(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    final_digest, final_size = hash_file(target)
    source_hex = source_digest.hexdigest()
    if source_hex != final_digest or size != final_size:
        raise AccessionError(f"copy verification failed: {source} -> {target}")
    return source_hex, final_digest, final_size


def event_metadata(row: dict[str, str]) -> dict[str, str]:
    events = json.loads(row.get("events_json") or "[]")
    creators: list[str] = []
    places: list[str] = []
    dates: list[str] = []
    for event in events:
        people = event.get("people") or {}
        creator = str(
            people.get("people_display_name")
            or people.get("displayname")
            or people.get("people_name")
            or ""
        ).strip()
        place = str((event.get("place") or {}).get("place_name") or "").strip()
        date = str((event.get("time") or {}).get("time_name") or "").strip()
        if creator and creator not in creators:
            creators.append(creator)
        if place and place not in places:
            places.append(place)
        if date and date not in dates:
            dates.append(date)
    coverage = list(places)
    if dates:
        coverage.append(f"Datierung laut Katalog: {'; '.join(dates)}")
    exact_date = dates[0] if len(dates) == 1 and re.fullmatch(r"(?:19|20)\d{2}", dates[0]) else ""
    return {
        "creator": "; ".join(creators),
        "coverage": "; ".join(coverage),
        "date_created": exact_date,
        "artwork_date": "; ".join(dates),
    }


def master_metadata_tags(
    row: dict[str, str],
    source_sha256: str,
    reviewed_bundle_sha256: str,
) -> dict[str, str]:
    event = event_metadata(row)
    metadata = {
        "entity_id": row["object_key"],
        "archival_label": row["inventory"],
        "descriptive_title": row["title"],
        "description": row["description"],
        "object_type": row["object_type"],
        "material_technique": row["material_technique"],
        "dimensions": row["dimensions"],
        "creator": event["creator"],
        "date_created": event["date_created"],
        "coverage": event["coverage"],
        "rights_statement": row["md_image_rights"],
        "rights_holder": row["md_image_owner"],
        "rights_usage_terms": row["md_image_rights"],
        "credit_line": row["md_image_owner"],
        "keywords": row["tags"],
        "source_collection": "Bäckerhandwerk in Berlin",
        "image_reference": row["md_image_filename"],
    }
    provenance = {
        "case_id": PROVENANCE_CASE,
        "source_file_id": Path(row["selected_master_source_path"]).name,
        "source_digest": f"sha256:{source_sha256}",
        "reviewed_bundle_digest": f"sha256:{reviewed_bundle_sha256}",
        "museum_digital_id": row["md_object_id"],
        "representation_role": "preservation_master",
        "provenance_gaps": [],
    }
    tags = tiff_metadata.expected_tags(tiff_metadata.PROFILE_V2, metadata, provenance)
    tags["XMP-xmp:Label"] = MASTER_PROFILE
    tags["XMP-dc:Relation"] += (
        f"; {row['md_object_url']}; museum-digital:image-resource:{row['md_image_resource_id']}"
        "; iss:collection:827"
    )
    if event["artwork_date"]:
        tags["XMP-iptcExt:ArtworkCircaDateCreated"] = event["artwork_date"]
    return tags


def metadata_readback(path: Path, tags: dict[str, str]) -> dict[str, Any]:
    return tiff_accession._exif_values(path, tuple(tags))


def expected_metadata_readback(tags: dict[str, str]) -> dict[str, str]:
    expected = dict(tags)
    date = expected.get("XMP-dc:Date", "")
    if re.fullmatch(r"(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?", date):
        expected["XMP-dc:Date"] = date.replace("-", ":")
    return expected


def write_metadata_master(
    source: Path,
    target: Path,
    row: dict[str, str],
    reviewed_bundle_sha256: str,
) -> tuple[str, str, int, int, int, str, dict[str, str]]:
    source_sha256, source_size = hash_file(source)
    tags = master_metadata_tags(row, source_sha256, reviewed_bundle_sha256)
    technical_source = tiff_accession._exif_values(source, tiff_accession.TECHNICAL_TAGS)
    bit_depth = tiff_accession._scalar_sample_depth(technical_source.get("IFD0:BitsPerSample"))
    decoded_source = tiff_accession._decoded_pixel_digest(source, bit_depth)
    target.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["exiftool", "-P"]
    arguments.extend(f"-{key}={value}" for key, value in tags.items())
    arguments.extend(["-o", str(target), str(source)])
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=600)
    if "Warning:" in (result.stdout + result.stderr):
        raise AccessionError(f"ExifTool warning while embedding {source}: {(result.stdout + result.stderr).strip()}")
    observed = metadata_readback(target, tags)
    expected = expected_metadata_readback(tags)
    if observed != expected:
        raise AccessionError(f"TIFF metadata readback differs for {row['inventory']}: expected={expected}, observed={observed}")
    technical_target = tiff_accession._exif_values(target, tiff_accession.TECHNICAL_TAGS)
    if technical_target != technical_source:
        raise AccessionError(f"TIFF technical metadata changed for {row['inventory']}")
    decoded_target = tiff_accession._decoded_pixel_digest(target, bit_depth)
    if decoded_target != decoded_source:
        raise AccessionError(f"decoded TIFF pixels changed for {row['inventory']}")
    final_sha256, final_size = hash_file(target)
    if source.stat().st_size != source_size:
        raise AccessionError(f"source TIFF size changed during embedding: {source}")
    return (
        source_sha256,
        final_sha256,
        final_size,
        int(technical_target["IFD0:ImageWidth"]),
        int(technical_target["IFD0:ImageHeight"]),
        decoded_target.removeprefix("sha256:"),
        tags,
    )


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise AccessionError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise AccessionError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def build_plan(*, require_target_absent: bool) -> dict[str, Any]:
    mount = source_guard()
    required = [
        DECISIONS, MD_CSV, MD_JSON, SOURCE_DISPOSITION, PREPARATION,
        DRAIN_LIST, DOCUMENT_DISPOSITION, DRAIN_SUMMARY,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise AccessionError(f"preparation files are missing: {missing}")
    if require_target_absent and UNIT.exists():
        raise AccessionError(f"target unit already exists: {UNIT}")

    preparation = json.loads(PREPARATION.read_text(encoding="utf-8"))
    decision_fields, decisions = read_csv(DECISIONS)
    _, md_rows = read_csv(MD_CSV)
    source_fields, source_rows = read_csv(SOURCE_DISPOSITION)
    drain_fields, drain_rows = read_csv(DRAIN_LIST)
    document_disposition_fields, document_disposition_rows = read_csv(DOCUMENT_DISPOSITION)
    raw_md = json.loads(MD_JSON.read_text(encoding="utf-8"))

    if not preparation.get("accession_ready"):
        raise AccessionError("preparation is not accession-ready")
    if preparation.get("accession_executed") and not preparation.get("metadata_correction_approved"):
        raise AccessionError("completed preparation lacks explicit metadata-correction approval")
    if preparation.get("target_unit") != str(UNIT):
        raise AccessionError("prepared target does not match accession target")
    if len(decisions) != EXPECTED_OBJECTS or len({row["object_key"] for row in decisions}) != EXPECTED_OBJECTS:
        raise AccessionError("expected 76 unique accession decisions")
    if len({row["inventory"] for row in decisions}) != EXPECTED_OBJECTS:
        raise AccessionError("inventory values are missing or duplicated")
    if any(row["outcome"] != "selected_master" for row in decisions):
        raise AccessionError("every object must have one confirmed selected Master")
    if any(row["deletion_authorized"].casefold() != "false" for row in decisions):
        raise AccessionError("accession decisions cannot authorize deletion")
    if Counter(row["presentation_rotation_cw"] for row in decisions) != {"0": 60, "90": 16}:
        raise AccessionError("unexpected presentation-rotation decisions")
    if len(md_rows) != EXPECTED_OBJECTS or len(raw_md) != EXPECTED_OBJECTS:
        raise AccessionError("museum-digital metadata snapshot is incomplete")
    if {row["md_object_id"] for row in md_rows} != {row["md_object_id"] for row in decisions}:
        raise AccessionError("museum-digital metadata does not align with decisions")
    if len(source_rows) != EXPECTED_SOURCE_ROWS:
        raise AccessionError("bounded source disposition must contain 92 rows")
    if Counter(row["disposition"] for row in source_rows) != {
        "selected_master": 76,
        "out_of_scope_collection_827": 16,
    }:
        raise AccessionError("source-disposition counts are wrong")
    if any(row["deletion_authorized"].casefold() != "false" for row in source_rows):
        raise AccessionError("source disposition cannot authorize deletion")
    if drain_fields != DRAIN_FIELDS or document_disposition_fields != DOCUMENT_DISPOSITION_FIELDS:
        raise AccessionError("drain review fields do not match the approved manifests")
    if len({row["source_path"] for row in drain_rows}) != len(drain_rows):
        raise AccessionError("drain review contains duplicated source paths")
    if any(row["deletion_authorized"].casefold() != "false" for row in drain_rows):
        raise AccessionError("drain review cannot authorize deletion")
    if any(row["deletion_authorized"].casefold() != "false" for row in document_disposition_rows):
        raise AccessionError("document source disposition cannot authorize deletion")

    selected_bytes = 0
    output_paths: set[str] = set()
    for row in decisions:
        source = Path(row["selected_master_source_path"]).resolve(strict=True)
        if not source.is_file() or SOURCE_ROOT.resolve(strict=True) not in source.parents:
            raise AccessionError(f"selected Master is outside protected source root: {source}")
        if source.name != f"{row['inventory']}.tif":
            raise AccessionError(f"selected Master filename mismatch: {source}")
        if source.stat().st_size != int(row["selected_master_size_bytes"]):
            raise AccessionError(f"selected Master size changed: {source}")
        selected_bytes += source.stat().st_size
        for field in ("master_final_relative_path", "access_final_relative_path"):
            relative = row[field]
            if relative in output_paths or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise AccessionError(f"unsafe or duplicated output path: {relative}")
            output_paths.add(relative)
    if selected_bytes != EXPECTED_SOURCE_BYTES or preparation.get("required_source_bytes") != selected_bytes:
        raise AccessionError("selected source-byte total is wrong")

    free = shutil.disk_usage(UNIT.parent).free
    document_bytes = 0
    document_paths: set[str] = set()
    for spec in DOCUMENT_SPECS:
        source = Path(spec["source_path"]).resolve(strict=True)
        if not source.is_file() or SOURCE_ROOT.resolve(strict=True) not in source.parents:
            raise AccessionError(f"project document is outside protected source root: {source}")
        relative = spec["final_relative_path"]
        if relative in output_paths or relative in document_paths or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise AccessionError(f"unsafe or duplicated document output path: {relative}")
        document_paths.add(relative)
        document_bytes += source.stat().st_size

    protected_paths = {
        Path(row["selected_master_source_path"]).resolve(strict=True) for row in decisions
    } | {
        Path(spec["source_path"]).resolve(strict=True) for spec in DOCUMENT_SPECS
    }
    for row in drain_rows:
        source = Path(row["source_path"]).resolve(strict=True)
        if not source.is_file() or BAECKER_SOURCE_ROOT.resolve(strict=True) not in source.parents:
            raise AccessionError(f"drain candidate is outside the bounded source tree: {source}")
        if source in protected_paths:
            raise AccessionError(f"selected Master/document appears in drain review: {source}")
        if source.stat().st_size != int(row["source_size_bytes"]):
            raise AccessionError(f"drain candidate size changed: {source}")
        if row["drain_state"] != "proposed_redundant":
            raise AccessionError(f"unexpected drain state for {source}")
    if len({row["source_path"] for row in document_disposition_rows}) != len(document_disposition_rows):
        raise AccessionError("document source disposition contains duplicated paths")
    for row in document_disposition_rows:
        source = Path(row["source_path"]).resolve(strict=True)
        if not source.is_file() or BAECKER_SOURCE_ROOT.resolve(strict=True) not in source.parents:
            raise AccessionError(f"document disposition path is outside the bounded source tree: {source}")
        if source.stat().st_size != int(row["size_bytes"]):
            raise AccessionError(f"document disposition size changed: {source}")

    minimum_free = selected_bytes + document_bytes + 10_000_000_000
    if free < minimum_free:
        raise AccessionError(f"insufficient destination space: free={free}, required={minimum_free}")
    return {
        "mount": mount,
        "preparation": preparation,
        "decision_fields": decision_fields,
        "decisions": decisions,
        "md_rows": md_rows,
        "source_fields": source_fields,
        "source_rows": source_rows,
        "drain_rows": drain_rows,
        "document_disposition_rows": document_disposition_rows,
        "raw_md": raw_md,
        "selected_bytes": selected_bytes,
        "document_bytes": document_bytes,
        "reviewed_bundle_sha256": hash_file(DECISIONS)[0],
        "free_bytes": free,
    }


def generate_access_jpeg(
    master: Path,
    target: Path,
    row: dict[str, str],
    master_sha256: str,
    master_tags: dict[str, str],
) -> tuple[str, int, int, int, dict[str, str]]:
    rotation = int(row["presentation_rotation_cw"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(master) as image:
        icc_profile = image.info.get("icc_profile")
        output = image.convert("RGB")
        if rotation:
            output = output.rotate(-rotation, expand=True)
        save_options: dict[str, Any] = {"format": "JPEG", "quality": 95, "subsampling": 0}
        if icc_profile:
            save_options["icc_profile"] = icc_profile
        output.save(target, **save_options)
        width, height = output.size
    access_tags = dict(master_tags)
    access_tags["XMP-xmp:Label"] = ACCESS_PROFILE
    access_tags["XMP-dc:Source"] = (
        f"ISS Bäckerhandwerk accession; generated from {row['master_final_relative_path']}; "
        f"master sha256:{master_sha256}; original source {row['selected_master_source_path']}"
    )
    access_tags["XMP-dc:Relation"] = (
        f"{master_tags['XMP-dc:Relation']}; iss:representation:access_derivative; "
        f"master={row['master_final_relative_path']}; sha256:{master_sha256}"
    )
    arguments = ["exiftool", "-overwrite_original", "-P", "-Orientation#=1"]
    arguments.extend(f"-{key}={value}" for key, value in access_tags.items())
    arguments.append(str(target))
    result = subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=600)
    if "Warning:" in (result.stdout + result.stderr):
        raise AccessionError(f"ExifTool warning while embedding derivative {row['inventory']}")
    observed = metadata_readback(target, access_tags)
    expected = expected_metadata_readback(access_tags)
    if observed != expected:
        raise AccessionError(f"JPEG metadata readback differs for {row['inventory']}: expected={expected}, observed={observed}")
    digest, size = hash_file(target)
    return digest, size, width, height, access_tags


CATALOG_FIELDS = [
    "object_key", "inventory", "title", "description", "object_type", "material_technique",
    "dimensions", "tags", "md_object_id", "md_object_url", "metadata_authority",
    "master_relative_path", "master_sha256", "master_size_bytes", "original_source_path",
    "source_sha256", "master_embedding_profile", "decoded_pixel_sha256",
    "access_relative_path", "access_sha256", "access_size_bytes", "access_embedding_profile",
    "presentation_rotation_cw", "selection_basis", "accession_state", "accessioned_utc",
    "deletion_authorized",
]
REPRESENTATION_FIELDS = [
    "object_key", "inventory", "role", "format", "final_relative_path", "original_source_path",
    "source_sha256", "final_sha256", "size_bytes", "width", "height", "derivative_of",
    "generation_profile", "presentation_rotation_cw", "provenance", "deletion_authorized",
    "decoded_pixel_sha256", "embedding_profile",
]
CHECKSUM_FIELDS = ["relative_path", "size_bytes", "sha256", "payload_type"]
DOCUMENT_FIELDS = [
    "document_id", "title", "role", "format", "final_relative_path",
    "original_source_path", "source_sha256", "final_sha256", "size_bytes",
    "condition", "selection_basis", "deletion_authorized",
]


def write_sqlite(
    catalog: list[dict[str, Any]],
    representations: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    source_rows: list[dict[str, str]],
    decisions: list[dict[str, str]],
    drain_rows: list[dict[str, Any]],
    document_disposition_rows: list[dict[str, str]],
) -> dict[str, int]:
    path = UNIT / "db-restore/RESTORE.sqlite"
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE catalog (
                object_key TEXT PRIMARY KEY, inventory TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, object_type TEXT NOT NULL, material_technique TEXT NOT NULL,
                dimensions TEXT NOT NULL, tags TEXT NOT NULL, md_object_id TEXT UNIQUE NOT NULL,
                md_object_url TEXT NOT NULL, metadata_authority TEXT NOT NULL,
                master_relative_path TEXT UNIQUE NOT NULL, master_sha256 TEXT NOT NULL,
                master_size_bytes INTEGER NOT NULL, original_source_path TEXT UNIQUE NOT NULL,
                source_sha256 TEXT NOT NULL, master_embedding_profile TEXT NOT NULL,
                decoded_pixel_sha256 TEXT NOT NULL, access_relative_path TEXT UNIQUE NOT NULL,
                access_sha256 TEXT NOT NULL, access_size_bytes INTEGER NOT NULL,
                access_embedding_profile TEXT NOT NULL,
                presentation_rotation_cw INTEGER NOT NULL, selection_basis TEXT NOT NULL,
                accession_state TEXT NOT NULL, accessioned_utc TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE representations (
                object_key TEXT NOT NULL REFERENCES catalog(object_key), inventory TEXT NOT NULL,
                role TEXT NOT NULL, format TEXT NOT NULL, final_relative_path TEXT PRIMARY KEY,
                original_source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                final_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, derivative_of TEXT NOT NULL, generation_profile TEXT NOT NULL,
                presentation_rotation_cw INTEGER NOT NULL, provenance TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0),
                decoded_pixel_sha256 TEXT NOT NULL, embedding_profile TEXT NOT NULL,
                UNIQUE(object_key, role)
            );
            CREATE TABLE source_disposition (
                source_path TEXT PRIMARY KEY, filename TEXT NOT NULL, object_key TEXT NOT NULL,
                disposition TEXT NOT NULL, basis TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE accession_decisions (
                object_key TEXT PRIMARY KEY, inventory TEXT UNIQUE NOT NULL, outcome TEXT NOT NULL,
                selected_master_source_path TEXT UNIQUE NOT NULL, selection_basis TEXT NOT NULL,
                human_confirmation TEXT NOT NULL, accession_state TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, title TEXT NOT NULL, role TEXT NOT NULL,
                format TEXT NOT NULL, final_relative_path TEXT UNIQUE NOT NULL,
                original_source_path TEXT UNIQUE NOT NULL, source_sha256 TEXT NOT NULL,
                final_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                condition TEXT NOT NULL, selection_basis TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE drain_list (
                source_path TEXT PRIMARY KEY, source_kind TEXT NOT NULL, unit TEXT NOT NULL,
                inventory TEXT NOT NULL, source_role TEXT NOT NULL, final_path TEXT NOT NULL,
                preservation_status TEXT NOT NULL, preservation_basis TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, final_sha256 TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL, exact_copy_proven TEXT NOT NULL,
                drain_state TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0),
                notes TEXT NOT NULL
            );
            CREATE TABLE document_source_disposition (
                source_path TEXT PRIMARY KEY, filename TEXT NOT NULL, format TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, disposition TEXT NOT NULL,
                basis TEXT NOT NULL, selected_document_id TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            """
        )
        con.executemany(
            f"INSERT INTO catalog ({','.join(CATALOG_FIELDS)}) VALUES ({','.join('?' for _ in CATALOG_FIELDS)})",
            [[row[field] for field in CATALOG_FIELDS] for row in catalog],
        )
        con.executemany(
            f"INSERT INTO representations ({','.join(REPRESENTATION_FIELDS)}) VALUES ({','.join('?' for _ in REPRESENTATION_FIELDS)})",
            [[row[field] for field in REPRESENTATION_FIELDS] for row in representations],
        )
        con.executemany(
            "INSERT INTO source_disposition VALUES (?,?,?,?,?,?)",
            [
                [row[field] for field in ("source_path", "filename", "object_key", "disposition", "basis")]
                + [0]
                for row in source_rows
            ],
        )
        con.executemany(
            "INSERT INTO accession_decisions VALUES (?,?,?,?,?,?,?,?)",
            [
                [
                    row[field]
                    for field in (
                        "object_key", "inventory", "outcome", "selected_master_source_path",
                        "selection_basis", "human_confirmation", "accession_state",
                    )
                ]
                + [0]
                for row in decisions
            ],
        )
        con.executemany(
            f"INSERT INTO documents ({','.join(DOCUMENT_FIELDS)}) VALUES ({','.join('?' for _ in DOCUMENT_FIELDS)})",
            [[row[field] for field in DOCUMENT_FIELDS] for row in documents],
        )
        con.executemany(
            f"INSERT INTO drain_list ({','.join(DRAIN_FIELDS)}) VALUES ({','.join('?' for _ in DRAIN_FIELDS)})",
            [
                [0 if field == "deletion_authorized" else row[field] for field in DRAIN_FIELDS]
                for row in drain_rows
            ],
        )
        con.executemany(
            f"INSERT INTO document_source_disposition ({','.join(DOCUMENT_DISPOSITION_FIELDS)}) "
            f"VALUES ({','.join('?' for _ in DOCUMENT_DISPOSITION_FIELDS)})",
            [
                [0 if field == "deletion_authorized" else row[field] for field in DOCUMENT_DISPOSITION_FIELDS]
                for row in document_disposition_rows
            ],
        )
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign:
            raise AccessionError(f"SQLite validation failed: integrity={integrity}, foreign_keys={foreign}")
        return {
            "catalog": con.execute("SELECT COUNT(*) FROM catalog").fetchone()[0],
            "representations": con.execute("SELECT COUNT(*) FROM representations").fetchone()[0],
            "source_disposition": con.execute("SELECT COUNT(*) FROM source_disposition").fetchone()[0],
            "accession_decisions": con.execute("SELECT COUNT(*) FROM accession_decisions").fetchone()[0],
            "documents": con.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "drain_list": con.execute("SELECT COUNT(*) FROM drain_list").fetchone()[0],
            "document_source_disposition": con.execute(
                "SELECT COUNT(*) FROM document_source_disposition"
            ).fetchone()[0],
        }
    finally:
        con.close()


def accession() -> dict[str, Any]:
    plan = build_plan(require_target_absent=True)
    started = now()
    UNIT.mkdir(parents=False)
    for relative in ("00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"):
        (UNIT / relative).mkdir()
    (UNIT / "export/access").mkdir()

    catalog: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    final_hashes: dict[str, tuple[str, int, str]] = {}
    final_decisions: list[dict[str, str]] = []
    for index, row in enumerate(plan["decisions"], start=1):
        source = Path(row["selected_master_source_path"])
        master_relative = row["master_final_relative_path"]
        access_relative = row["access_final_relative_path"]
        master = UNIT / master_relative
        access = UNIT / access_relative
        (
            source_sha,
            master_sha,
            master_size,
            master_width,
            master_height,
            decoded_pixel_sha256,
            embedded_master_tags,
        ) = write_metadata_master(source, master, row, plan["reviewed_bundle_sha256"])
        access_sha, access_size, access_width, access_height, _ = generate_access_jpeg(
            master, access, row, master_sha, embedded_master_tags
        )
        accessioned = now()
        catalog.append(
            {
                "object_key": row["object_key"],
                "inventory": row["inventory"],
                "title": row["title"],
                "description": row["description"],
                "object_type": row["object_type"],
                "material_technique": row["material_technique"],
                "dimensions": row["dimensions"],
                "tags": row["tags"],
                "md_object_id": row["md_object_id"],
                "md_object_url": row["md_object_url"],
                "metadata_authority": row["metadata_authority"],
                "master_relative_path": master_relative,
                "master_sha256": master_sha,
                "master_size_bytes": master_size,
                "original_source_path": str(source),
                "source_sha256": source_sha,
                "master_embedding_profile": MASTER_PROFILE,
                "decoded_pixel_sha256": decoded_pixel_sha256,
                "access_relative_path": access_relative,
                "access_sha256": access_sha,
                "access_size_bytes": access_size,
                "access_embedding_profile": ACCESS_PROFILE,
                "presentation_rotation_cw": int(row["presentation_rotation_cw"]),
                "selection_basis": row["selection_basis"],
                "accession_state": "accessioned_verified",
                "accessioned_utc": accessioned,
                "deletion_authorized": 0,
            }
        )
        representations.extend(
            [
                {
                    "object_key": row["object_key"], "inventory": row["inventory"],
                    "role": "preservation_master", "format": "tiff",
                    "final_relative_path": master_relative, "original_source_path": str(source),
                    "source_sha256": source_sha, "final_sha256": master_sha,
                    "size_bytes": master_size, "width": master_width, "height": master_height,
                    "derivative_of": "", "generation_profile": MASTER_PROFILE,
                    "presentation_rotation_cw": int(row["presentation_rotation_cw"]),
                    "provenance": "protected_local_tiff_exact_inventory_match_metadata_enriched",
                    "deletion_authorized": 0,
                    "decoded_pixel_sha256": decoded_pixel_sha256,
                    "embedding_profile": MASTER_PROFILE,
                },
                {
                    "object_key": row["object_key"], "inventory": row["inventory"],
                    "role": "access_derivative", "format": "jpeg",
                    "final_relative_path": access_relative, "original_source_path": str(source),
                    "source_sha256": source_sha, "final_sha256": access_sha,
                    "size_bytes": access_size, "width": access_width, "height": access_height,
                    "derivative_of": master_relative,
                    "generation_profile": "full_resolution_jpeg_q95_subsampling_0_md_metadata",
                    "presentation_rotation_cw": int(row["presentation_rotation_cw"]),
                    "provenance": "generated_during_accession_from_selected_master",
                    "deletion_authorized": 0,
                    "decoded_pixel_sha256": "",
                    "embedding_profile": ACCESS_PROFILE,
                },
            ]
        )
        final_row = dict(row)
        final_row["source_hash_state"] = f"sha256:{source_sha}"
        final_row["final_hash_state"] = f"sha256:{master_sha}"
        final_row["accession_state"] = "accessioned_verified"
        final_decisions.append(final_row)
        final_hashes[master_relative] = (master_sha, master_size, "preservation_master")
        final_hashes[access_relative] = (access_sha, access_size, "access_derivative")
        print(f"[{index:02d}/{EXPECTED_OBJECTS}] {row['inventory']} copied and derived", flush=True)

    for spec in DOCUMENT_SPECS:
        source = Path(spec["source_path"])
        final_relative = spec["final_relative_path"]
        source_sha, final_sha, size = copy_exact(source, UNIT / final_relative)
        document = {
            **spec,
            "original_source_path": str(source),
            "source_sha256": source_sha,
            "final_sha256": final_sha,
            "size_bytes": size,
            "deletion_authorized": False,
        }
        document.pop("source_path")
        documents.append(document)
        final_hashes[final_relative] = (final_sha, size, "project_document_master")
        print(f"[document] {spec['document_id']} copied exactly", flush=True)

    catalog_by_inventory = {row["inventory"]: row for row in catalog}
    documents_by_source_hash = {f"sha256:{row['source_sha256']}": row for row in documents}
    final_drain_rows: list[dict[str, Any]] = []
    for prepared_row in plan["drain_rows"]:
        row: dict[str, Any] = dict(prepared_row)
        inventory = row["inventory"]
        if inventory:
            final = catalog_by_inventory.get(inventory)
            if final is None:
                raise AccessionError(f"drain row refers to unknown inventory: {inventory}")
            row["final_path"] = final["master_relative_path"]
            row["final_sha256"] = f"sha256:{final['master_sha256']}"
        else:
            final_document = documents_by_source_hash.get(row["source_sha256"])
            if final_document:
                row["final_path"] = final_document["final_relative_path"]
                row["final_sha256"] = f"sha256:{final_document['final_sha256']}"
        row["deletion_authorized"] = "false"
        final_drain_rows.append(row)

    drain_summary = json.loads(DRAIN_SUMMARY.read_text(encoding="utf-8"))
    drain_summary.update(
        {
            "target_unit": str(UNIT),
            "drain_rows": len(final_drain_rows),
            "document_rows": len(plan["document_disposition_rows"]),
            "final_accession_hashes_linked": sum(bool(row["final_sha256"]) for row in final_drain_rows),
            "deletion_authorized": False,
        }
    )

    atomic_bytes(UNIT / "CATALOG.csv", csv_bytes(CATALOG_FIELDS, catalog))
    atomic_bytes(
        UNIT / "00_MANIFESTS/representations.csv",
        csv_bytes(REPRESENTATION_FIELDS, representations),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/source-disposition.csv",
        csv_bytes(plan["source_fields"], plan["source_rows"]),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/documents.csv",
        csv_bytes(DOCUMENT_FIELDS, documents),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/drain-list.csv",
        csv_bytes(DRAIN_FIELDS, final_drain_rows),
    )
    atomic_bytes(
        UNIT / "00_MANIFESTS/document-source-disposition.csv",
        csv_bytes(DOCUMENT_DISPOSITION_FIELDS, plan["document_disposition_rows"]),
    )
    atomic_json(UNIT / "00_MANIFESTS/drain-review-summary.json", drain_summary)
    atomic_bytes(
        UNIT / "db-restore/accession-decisions.csv",
        csv_bytes(plan["decision_fields"], final_decisions),
    )
    shutil.copyfile(MD_CSV, UNIT / "metadata/museum-digital-objects.csv")
    shutil.copyfile(MD_JSON, UNIT / "metadata/museum-digital-objects.json")
    shutil.copyfile(PREPARATION, UNIT / "00_MANIFESTS/accession-preparation.json")
    shutil.copyfile(UNIT / "CATALOG.csv", UNIT / "db-restore/RESTORE.csv")
    shutil.copyfile(UNIT / "00_MANIFESTS/drain-list.csv", UNIT / "db-restore/drain-list.csv")
    shutil.copyfile(
        UNIT / "00_MANIFESTS/document-source-disposition.csv",
        UNIT / "db-restore/document-source-disposition.csv",
    )

    sqlite_counts = write_sqlite(
        catalog,
        representations,
        documents,
        plan["source_rows"],
        final_decisions,
        final_drain_rows,
        plan["document_disposition_rows"],
    )
    con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        sql = "\n".join(con.iterdump()) + "\n"
    finally:
        con.close()
    atomic_bytes(UNIT / "db-restore/RESTORE.sql", sql.encode("utf-8"))

    readme = f"""# Bäckerhandwerk in Berlin archive unit

This unit contains museum-digital collection 827 with one explicitly confirmed TIFF
Master and one accession-generated JPEG access derivative per archival object.

- Archival objects: 76
- Selected TIFF Masters: 76
- Generated JPEG access derivatives: 76
- Project-document Masters: 2
- Proposed redundant/noise drain entries: {len(final_drain_rows)}
- Related-document source dispositions: {len(plan["document_disposition_rows"])}
- Derivatives with 90-degree clockwise presentation rotation: 16
- Source files outside collection 827: 16 (recorded as out of scope)
- Metadata authority: live museum-digital capture
- Deletion authorized: false

`masters/` contains metadata-enriched preservation TIFFs created from the selected protected
sources with the `{MASTER_PROFILE}` profile. Exact metadata readback, TIFF technical-tag
equality, and decoded-pixel equality were verified; source TIFFs were not changed.
`export/access/` contains the full-resolution JPEG derivatives generated during accession,
with the same descriptive metadata and explicit Master provenance under `{ACCESS_PROFILE}`.
`documents/` contains exact
copies of the final publication and complete illustrated editorial manuscript. `CATALOG.csv`
and `00_MANIFESTS/` preserve object identity, museum-digital metadata, provenance,
representations, dispositions, and hashes without requiring SQLite. `db-restore/`
contains the rebuildable catalogue and the final explicit Master decisions.
`00_MANIFESTS/drain-list.csv` is an exact-path review list for matched image derivatives,
exact TIFF copies, superseded production documents, and technical noise. The companion
document-source disposition lists every related document. Neither record authorizes deletion.

The 16 non-collection files in the bounded source folder remain unchanged and outside
this unit. Out of scope and redundant never imply deletion authorization.
"""
    atomic_bytes(UNIT / "00_README.md", readme.encode("utf-8"))

    document_contents = """# Bäckerhandwerk project documents

Two content-reviewed project-document Masters are retained as exact source bytes.
The earlier publication draft, separate text/caption components, numbered image-selection
workbooks, reference scans, and administrative files remain unchanged at source. Their
content-reviewed dispositions are recorded in `../00_MANIFESTS/document-source-disposition.csv`;
only explicit redundant/noise proposals also appear in `../00_MANIFESTS/drain-list.csv`.
No deletion is authorized.

| ID | Final path | Role | Source | Condition |
| --- | --- | --- | --- | --- |
| BAECKER-DOC-001 | `final-publication-2024.pdf` | Final BeBra publication | `P220240_Chronik_Bäckerinnung_165x240_241014.pdf` | readable |
| BAECKER-DOC-002 | `final-illustrated-manuscript-2024.docx` | Complete illustrated editorial source | `101xBaeckerhandwerk__mit-Abb._final_03.03.2024.docx` | readable |

Exact source paths, provenance, sizes, and final SHA-256 values are recorded in
`../00_MANIFESTS/documents.csv` and mirrored in `../db-restore/RESTORE.sqlite`.
"""
    atomic_bytes(UNIT / "documents/CONTENTS.md", document_contents.encode("utf-8"))

    excluded = {"00_MANIFESTS/payload-checksums.csv", "00_MANIFESTS/accession-completion.json"}
    checksum_rows: list[dict[str, Any]] = []
    for path in sorted(item for item in UNIT.rglob("*") if item.is_file()):
        relative = path.relative_to(UNIT).as_posix()
        if relative in excluded:
            continue
        if relative in final_hashes:
            digest, size, payload_type = final_hashes[relative]
        else:
            digest, size = hash_file(path)
            payload_type = "unit_record"
        checksum_rows.append(
            {"relative_path": relative, "size_bytes": size, "sha256": digest, "payload_type": payload_type}
        )
    atomic_bytes(
        UNIT / "00_MANIFESTS/payload-checksums.csv",
        csv_bytes(CHECKSUM_FIELDS, checksum_rows),
    )
    checksum_sha, checksum_size = hash_file(UNIT / "00_MANIFESTS/payload-checksums.csv")

    actual_paths = {
        path.relative_to(UNIT).as_posix()
        for path in UNIT.rglob("*")
        if path.is_file() and path.relative_to(UNIT).as_posix() not in excluded
    }
    listed_paths = {row["relative_path"] for row in checksum_rows}
    expected_sqlite = {
        "catalog": 76,
        "representations": 152,
        "source_disposition": 92,
        "accession_decisions": 76,
        "documents": 2,
        "drain_list": len(final_drain_rows),
        "document_source_disposition": len(plan["document_disposition_rows"]),
    }
    if (
        Counter(row["role"] for row in representations)
        != {"preservation_master": 76, "access_derivative": 76}
        or sqlite_counts != expected_sqlite
        or actual_paths != listed_paths
    ):
        raise AccessionError("final structural validation failed")

    completed = now()
    completion = {
        "format": "iss-baeckerhandwerk-accession-completion-v1",
        "started_utc": started,
        "completed_utc": completed,
        "target_unit": str(UNIT),
        "source_mount": source_guard(),
        "objects": len(catalog),
        "selected_master_representations": 76,
        "generated_access_derivatives": 76,
        "project_document_masters": len(documents),
        "master_embedding_profile": MASTER_PROFILE,
        "access_embedding_profile": ACCESS_PROFILE,
        "metadata_readback_verified": True,
        "decoded_pixel_equality_verified": True,
        "presentation_rotation_cw": dict(Counter(str(row["presentation_rotation_cw"]) for row in catalog)),
        "source_disposition_rows": len(plan["source_rows"]),
        "drain_rows": len(final_drain_rows),
        "document_source_disposition_rows": len(plan["document_disposition_rows"]),
        "drain_deletion_authorized": False,
        "out_of_scope_source_files": 16,
        "payload_checksum_rows": len(checksum_rows),
        "payload_checksum_manifest_sha256": checksum_sha,
        "payload_checksum_manifest_size_bytes": checksum_size,
        "sqlite_integrity": "ok",
        "sqlite_counts": sqlite_counts,
        "filesystem_matches_checksum_manifest": actual_paths == listed_paths,
        "accession_verified": True,
        "deletion_authorized": False,
    }
    atomic_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    os.sync()
    print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return completion


def metadata_pilot() -> dict[str, Any]:
    mount = source_guard()
    _, decisions = read_csv(DECISIONS)
    if not decisions:
        raise AccessionError("no accession decisions available for metadata pilot")
    row = decisions[0]
    source = Path(row["selected_master_source_path"])
    master = PREPARED / "metadata-pilot-master.tif"
    access = PREPARED / "metadata-pilot-access.jpg"
    if master.exists() or access.exists():
        raise AccessionError("metadata pilot outputs already exist")
    try:
        (
            source_sha,
            master_sha,
            master_size,
            width,
            height,
            decoded_pixel_sha256,
            tags,
        ) = write_metadata_master(source, master, row, hash_file(DECISIONS)[0])
        access_sha, access_size, access_width, access_height, access_tags = generate_access_jpeg(
            master, access, row, master_sha, tags
        )
        return {
            "ok": True,
            "inventory": row["inventory"],
            "source_mount": mount,
            "source_sha256": source_sha,
            "master_sha256": master_sha,
            "master_size_bytes": master_size,
            "master_dimensions": [width, height],
            "decoded_pixel_sha256": decoded_pixel_sha256,
            "master_embedding_profile": MASTER_PROFILE,
            "master_embedded_fields": len(tags),
            "access_sha256": access_sha,
            "access_size_bytes": access_size,
            "access_dimensions": [access_width, access_height],
            "access_embedding_profile": ACCESS_PROFILE,
            "access_embedded_fields": len(access_tags),
            "source_unchanged": hash_file(source)[0] == source_sha,
            "deletion_authorized": False,
        }
    finally:
        master.unlink(missing_ok=True)
        access.unlink(missing_ok=True)


def repair_accession() -> dict[str, Any]:
    if not UNIT.is_dir():
        raise AccessionError(f"unit to correct is absent: {UNIT}")
    if RECOVERY_UNIT.exists():
        raise AccessionError(f"correction recovery target already exists: {RECOVERY_UNIT}")
    source_guard()
    os.replace(UNIT, RECOVERY_UNIT)
    try:
        completion = accession()
    except Exception as error:
        raise AccessionError(
            f"metadata correction failed: {error}; predecessor preserved at {RECOVERY_UNIT} "
            "and partial target retained for diagnosis"
        ) from error
    completion["predecessor_unit"] = str(RECOVERY_UNIT)
    atomic_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    return completion


def validate_existing() -> dict[str, Any]:
    if not UNIT.is_dir():
        raise AccessionError(f"unit is absent: {UNIT}")
    completion = json.loads((UNIT / "00_MANIFESTS/accession-completion.json").read_text())
    _, catalog = read_csv(UNIT / "CATALOG.csv")
    _, representations = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    _, dispositions = read_csv(UNIT / "00_MANIFESTS/source-disposition.csv")
    _, documents = read_csv(UNIT / "00_MANIFESTS/documents.csv")
    drain_fields, drain_rows = read_csv(UNIT / "00_MANIFESTS/drain-list.csv")
    document_disposition_fields, document_disposition_rows = read_csv(
        UNIT / "00_MANIFESTS/document-source-disposition.csv"
    )
    _, checksums = read_csv(UNIT / "00_MANIFESTS/payload-checksums.csv")
    con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        document_count = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        drain_count = con.execute("SELECT COUNT(*) FROM drain_list").fetchone()[0]
        document_disposition_count = con.execute(
            "SELECT COUNT(*) FROM document_source_disposition"
        ).fetchone()[0]
    finally:
        con.close()
    excluded = {"00_MANIFESTS/payload-checksums.csv", "00_MANIFESTS/accession-completion.json"}
    actual = {
        path.relative_to(UNIT).as_posix()
        for path in UNIT.rglob("*")
        if path.is_file() and path.relative_to(UNIT).as_posix() not in excluded
    }
    listed = {row["relative_path"] for row in checksums}
    result = {
        "ok": bool(
            completion.get("accession_verified")
            and completion.get("deletion_authorized") is False
            and len(catalog) == 76
            and Counter(row["role"] for row in representations)
            == {"preservation_master": 76, "access_derivative": 76}
            and all(row["master_embedding_profile"] == MASTER_PROFILE for row in catalog)
            and all(row["access_embedding_profile"] == ACCESS_PROFILE for row in catalog)
            and all(row["decoded_pixel_sha256"] for row in catalog)
            and Counter(row["embedding_profile"] for row in representations)
            == {MASTER_PROFILE: 76, ACCESS_PROFILE: 76}
            and len(dispositions) == 92
            and len(documents) == 2
            and document_count == 2
            and drain_fields == DRAIN_FIELDS
            and document_disposition_fields == DOCUMENT_DISPOSITION_FIELDS
            and drain_count == len(drain_rows)
            and document_disposition_count == len(document_disposition_rows)
            and all(row["deletion_authorized"].casefold() == "false" for row in drain_rows)
            and all(
                row["deletion_authorized"].casefold() == "false"
                for row in document_disposition_rows
            )
            and all(row["deletion_authorized"].casefold() == "false" for row in documents)
            and all(row["deletion_authorized"].casefold() == "false" for row in dispositions)
            and actual == listed
            and integrity == "ok"
            and not foreign
        ),
        "objects": len(catalog),
        "representations": len(representations),
        "master_embedding_profile": MASTER_PROFILE,
        "access_embedding_profile": ACCESS_PROFILE,
        "source_disposition_rows": len(dispositions),
        "project_document_masters": len(documents),
        "drain_rows": len(drain_rows),
        "document_source_disposition_rows": len(document_disposition_rows),
        "payload_checksum_rows": len(checksums),
        "filesystem_matches_checksum_manifest": actual == listed,
        "sqlite_integrity": integrity,
        "foreign_key_violations": len(foreign),
        "source_mount": source_guard(),
        "deletion_authorized_true": sum(
            row["deletion_authorized"].casefold() == "true"
            for row in dispositions + drain_rows + document_disposition_rows
        ),
    }
    if not result["ok"]:
        raise AccessionError(f"existing unit validation failed: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="read-only accession preflight")
    group.add_argument("--run", action="store_true", help="perform explicitly approved accession")
    group.add_argument("--validate", action="store_true", help="validate a completed unit")
    group.add_argument("--metadata-pilot", action="store_true", help="run one unpublished metadata/pixel pilot")
    group.add_argument("--repair-metadata", action="store_true", help="preserve predecessor and rebuild metadata-enriched unit")
    args = parser.parse_args()
    try:
        if args.check:
            plan = build_plan(require_target_absent=True)
            result = {
                "ok": True,
                "source_mount": plan["mount"],
                "objects": len(plan["decisions"]),
                "selected_masters": 76,
                "generated_access_derivatives": 76,
                "project_document_masters": len(DOCUMENT_SPECS),
                "project_document_source_bytes": plan["document_bytes"],
                "presentation_rotation_cw": {"0": 60, "90": 16},
                "selected_source_bytes": plan["selected_bytes"],
                "destination_free_bytes": plan["free_bytes"],
                "target_absent": not UNIT.exists(),
                "metadata_snapshot_rows": len(plan["md_rows"]),
                "source_disposition_rows": len(plan["source_rows"]),
                "deletion_authorized": False,
            }
        elif args.run:
            result = accession()
        elif args.metadata_pilot:
            result = metadata_pilot()
        elif args.repair_metadata:
            result = repair_accession()
        else:
            result = validate_existing()
        if not args.run and not args.repair_metadata:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (
        AccessionError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
