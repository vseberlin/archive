#!/usr/bin/env python3
"""Accession VLV-1097 as one two-view JPEG-derived surrogate object."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOLS))

import catalog_core
import tiff_accession
import tiff_metadata
from archive_core.catalog import (
    unit_catalog_csv_bytes,
    unit_catalog_rows,
    unit_catalog_xlsx_bytes,
)
from jobs.provenance_restoration import surrogate_accession, unit_recovery
from paragon_human_catalog import build as build_human_catalog
from workflows import editor_reconcile, recovery_metadata

ROOT = Path("/mnt/volume/iss-archive-work/paragon")
CATALOG = ROOT / "work/catalog.sqlite"
FINAL = Path("/mnt/volume/iss-archive/units/paragon")
RUN = Path(__file__).resolve().parent
STAGE_MEDIA = RUN / "stage-media"
STAGE_CONTROL = RUN / "stage-control"
BEFORE_CONTROL = RUN / "before-final-control"
BACKUP = RUN / "before-catalog.sqlite"
WORKBOOK = Path(
    "/media/vladimir/Elements/restore/archive/Paragon/"
    "Bildtexte Paragon stand 05.02.2020.xlsx"
)
UNIT = "WF-PARAGON"
INVENTORY = "VLV-1097"
MD_ID = "57113"
ACTOR = "Vladimir"
SOURCE_FRONT = Path(
    "/media/vladimir/Elements/restore/archive/Paragon/"
    "fertig für md/Fotos.jpg/VLV-1097.jpg"
)
SOURCE_VERSO = Path(
    "/media/vladimir/Elements/restore/archive/Paragon/"
    "fertig für md/Fotos.jpg/VLV-1097-1.jpg"
)
API_URL = (
    "https://berlin.museum-digital.de/index.php?"
    "t=objekt&oges=57113&navlang=de&output=json"
)
OBJECT_URL = "https://berlin.museum-digital.de/object/57113"
EXPECTED_SOURCE_DIGESTS = {
    "front": "sha256:028c7a33baa3486175ab7676a83d291217e515459ff6138ed62435d0a299e8f0",
    "verso": "sha256:e9789e6cc426e782a6c77fe384e4140ad8596f1d9d2cb76d4c659097ba19bf2c",
}
RIGHTS = {
    "image_statement": "CC BY-NC-SA 4.0",
    "image_uri": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "rights_holder": "www.industriesalon.de",
    "metadata_statement": "CC0 1.0",
    "metadata_uri": "https://creativecommons.org/publicdomain/zero/1.0/",
    "provider_isil": "DE-MUS-056528",
}
TOOL_VERSION = "paragon-vlv-1097-accession-1.0.0"

CONTROL_PATHS = (
    "CATALOG-HUMAN.csv",
    "CATALOG.xlsx",
    "README.md",
    "catalog-export-summary.json",
    "db-restore/RESTORE.csv",
    "db-restore/RESTORE.sql",
    "db-restore/RESTORE.sqlite",
    "db-restore/RESTORE.xlsx",
    "db-restore/export-summary.json",
    "db-restore/manifests/representations.csv",
    "documents/catalog.csv",
    "documents/catalog.xlsx",
    "db-restore/checksums.csv",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(encoded)


def write_stage_bytes(relative: str, payload: bytes) -> Path:
    path = STAGE_CONTROL / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_stage_text(relative: str, payload: str) -> Path:
    return write_stage_bytes(relative, payload.encode("utf-8"))


def write_stage_json(relative: str, payload: dict) -> Path:
    return write_stage_text(
        relative,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def backup_sqlite(source_path: Path, destination_path: Path) -> None:
    if destination_path.exists():
        raise RuntimeError(f"backup already exists: {destination_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        source.close()
        destination.close()
    check = sqlite3.connect(f"file:{destination_path}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite backup failed integrity check")
    finally:
        check.close()


def restore_catalog() -> None:
    if not BACKUP.exists():
        return
    source = sqlite3.connect(f"file:{BACKUP}?mode=ro", uri=True)
    destination = sqlite3.connect(CATALOG)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        source.close()
        destination.close()


def backup_controls() -> None:
    for relative in CONTROL_PATHS:
        source = FINAL / relative
        if not source.is_file():
            raise RuntimeError(f"missing final control: {source}")
        destination = BEFORE_CONTROL / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def restore_controls() -> None:
    for relative in CONTROL_PATHS:
        source = BEFORE_CONTROL / relative
        if not source.is_file():
            continue
        destination = FINAL / relative
        temporary = destination.with_name(f".{destination.name}.rollback")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)


def safe_copy(source: Path, destination: Path, expected: str) -> None:
    if destination.exists():
        raise RuntimeError(f"refusing existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if sha256(temporary) != expected:
            raise RuntimeError(f"copy digest mismatch: {destination}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_snapshot() -> tuple[dict, bytes, str]:
    with urllib.request.urlopen(API_URL, timeout=30) as response:
        raw = response.read()
    payload = json.loads(raw)
    if (
        str(payload.get("object_id")) != MD_ID
        or payload.get("object_inventory_number") != INVENTORY
    ):
        raise RuntimeError("museum-digital identity changed")
    snapshot_digest = digest_bytes(raw)
    (RUN / "museum-digital-57113.json").write_bytes(raw)
    return payload, raw, snapshot_digest


def inspect_source(path: Path, part: str) -> dict:
    resolved = path.resolve(strict=True)
    actual = sha256(resolved)
    if actual != EXPECTED_SOURCE_DIGESTS[part]:
        raise RuntimeError(f"{part} source digest changed")
    technical = surrogate_accession.verify_image_derivative(resolved)
    stat = resolved.stat()
    return {
        "part": part,
        "path": str(resolved),
        "sha256": actual,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "width": int(technical["width"]),
        "height": int(technical["height"]),
        "media_type": "image/jpeg",
    }


def ensure_metadata(md: dict, snapshot_digest: str) -> str:
    connection = catalog_core.connect_catalog(CATALOG)
    try:
        state = dict(catalog_core.catalog_state(connection))
        if state != {
            "catalog_revision": 9920,
            "published_revision": 9920,
            "dirty_since": None,
        }:
            raise RuntimeError(f"unexpected starting catalog state: {state}")
        entity_id, created = unit_recovery._ensure_entity(
            connection,
            unit=UNIT,
            inventory=INVENTORY,
            actor=ACTOR,
        )
    finally:
        connection.close()
    if not created:
        raise RuntimeError("VLV-1097 unexpectedly already exists")

    tags = [
        str(item.get("tag_name") or "").strip()
        for item in md.get("object_tags", [])
        if str(item.get("tag_name") or "").strip()
    ]
    connection = catalog_core.connect_catalog(CATALOG)
    try:
        editor_reconcile.reconcile_metadata(
            connection,
            unit_id=UNIT,
            entity_id=entity_id,
            field_decisions={
                "title": {
                    "source": "museum_digital",
                    "value": str(md["object_name"]),
                },
                "description": {
                    "source": "museum_digital",
                    "value": str(md["object_description"]),
                },
                "object_type": {
                    "source": "museum_digital",
                    "value": str(md["object_type"]),
                },
                "material": {
                    "source": "museum_digital",
                    "value": str(md["object_material_technique"]),
                },
                "dimensions": {
                    "source": "museum_digital",
                    "value": str(md["object_dimensions"]),
                },
            },
            tag_decision={"source": "museum_digital", "values": tags},
            actor=ACTOR,
            reason=(
                "Accept current museum-digital descriptive metadata for the "
                "visually verified VLV-1097 front/verso object."
            ),
            evidence=f"{OBJECT_URL}; {snapshot_digest}",
            external_id=MD_ID,
        )
        editor_reconcile.add_archive_keyword(
            connection,
            unit_id=UNIT,
            entity_id=entity_id,
            value="Paragon",
            actor=ACTOR,
            reason="Unit-level discovery context.",
            evidence=f"unit:{UNIT}",
        )
        catalog_core.review_external_identity(
            connection,
            decision_id="MD-IDENTITY-VLV-1097-57113-20260727",
            entity_id=entity_id,
            external_system="museum-digital",
            external_id=MD_ID,
            outcome="same_entity",
            actor=ACTOR,
            reason=(
                "Inventory, description, dimensions, and front image pixels "
                "agree; local VLV-1097-1 is the verso of the same photograph."
            ),
            captured_at=utc_now(),
            snapshot_digest=snapshot_digest,
            relative_path=(
                "work/accessions/20260727T182817Z-vlv-1097/"
                "museum-digital-57113.json"
            ),
            citation=OBJECT_URL,
        )
    finally:
        connection.close()

    connection = catalog_core.connect_catalog(CATALOG)
    try:
        timestamp = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        source_id = f"SOURCE-{uuid.uuid4().hex.upper()}"
        connection.execute(
            """INSERT INTO evidence_sources (
                   source_id, source_type, system, external_id, captured_at,
                   snapshot_digest, source_root_id, relative_path,
                   local_absolute_path, citation, note
               ) VALUES (?, 'external_snapshot', 'museum-digital', ?, ?, ?,
                         '', ?, '', ?, ?)""",
            (
                source_id,
                MD_ID,
                timestamp,
                snapshot_digest,
                (
                    "work/accessions/20260727T182817Z-vlv-1097/"
                    "museum-digital-57113.json"
                ),
                OBJECT_URL,
                "Rights, metadata licence, and provider identity evidence.",
            ),
        )
        rights_assertions = {
            "archive_rights_statement": RIGHTS["image_statement"],
            "archive_rights_web_statement": RIGHTS["image_uri"],
            "archive_rights_holder": RIGHTS["rights_holder"],
            "archive_metadata_license": RIGHTS["metadata_statement"],
            "archive_metadata_license_uri": RIGHTS["metadata_uri"],
            "archive_provider_isil": RIGHTS["provider_isil"],
        }
        for predicate, value in rights_assertions.items():
            connection.execute(
                """INSERT OR IGNORE INTO assertion_predicates (
                       predicate, label, value_kind, cardinality, status
                   ) VALUES (?, ?, 'text', 'one', 'active')""",
                (predicate, predicate.replace("_", " ").title()),
            )
            connection.execute(
                """INSERT INTO evidence_assertions (
                       assertion_id, subject_entity_id, predicate, value_text,
                       object_entity_id, status, confidence, source_id, actor,
                       asserted_at, supersedes_assertion_id, note
                   ) VALUES (?, ?, ?, ?, NULL, 'accepted', 'certain', ?, ?, ?,
                             NULL, ?)""",
                (
                    f"ASSERT-{uuid.uuid4().hex.upper()}",
                    entity_id,
                    predicate,
                    value,
                    source_id,
                    ACTOR,
                    timestamp,
                    "Current museum-digital rights plus approved ISS provider ISIL.",
                ),
            )
        note = (
            "One physical photograph represented by front and verso scans. "
            "Best surviving files are low-resolution JPEGs; preservation TIFFs "
            "are explicitly manufactured pixel-equivalent surrogates. Image "
            "rights CC BY-NC-SA 4.0; metadata CC0 1.0; provider ISIL "
            "DE-MUS-056528."
        )
        connection.execute(
            "UPDATE archive_objects SET note=?, updated_at=? WHERE entity_id=?",
            (note, timestamp, entity_id),
        )
        catalog_core._append_log(
            connection,
            ACTOR,
            "record_rights_and_provider",
            "archive_object",
            entity_id,
            UNIT,
            {
                **RIGHTS,
                "museum_digital_id": MD_ID,
                "deletion_authorized": False,
            },
            evidence=snapshot_digest,
            note="Rights and provider data assigned like the published MD record.",
            at_utc=timestamp,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    connection = catalog_core.connect_catalog(CATALOG)
    try:
        connection.execute("BEGIN IMMEDIATE")
        timestamp = utc_now()
        catalog_core._append_log(
            connection,
            ACTOR,
            "prepare_simplified_surrogate_accession",
            "archive_object",
            entity_id,
            UNIT,
            {
                "views": ["front", "verso"],
                "source_format": "image/jpeg",
                "target_role": "preservation_surrogate",
            },
            evidence=snapshot_digest,
            note="Publish accepted metadata for deterministic TIFF embedding.",
            at_utc=timestamp,
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.execute(
            "UPDATE catalog_state SET published_revision=?, dirty_since=NULL WHERE singleton=1",
            (revision,),
        )
        connection.commit()
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return entity_id


def create_surrogates(
    entity_id: str,
    sources: list[dict],
    reviewed_digest: str,
) -> list[dict]:
    STAGE_MEDIA.mkdir(parents=True)
    connection = catalog_core.connect_catalog(CATALOG, read_only=True)
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        metadata = tiff_accession._catalog_metadata(
            connection, entity_id, tiff_metadata.PROFILE_V2
        )
    finally:
        connection.close()
    workbook_metadata = recovery_metadata.require_embedding_metadata(INVENTORY)
    results = []
    for source in sources:
        part = source["part"]
        source_path = Path(source["path"])
        suffix = "" if part == "front" else "-1"
        master_name = f"{INVENTORY}{suffix}__{entity_id}__surrogate.tif"
        access_name = f"{INVENTORY}{suffix}__{entity_id}__access.jpg"
        master_stage = STAGE_MEDIA / master_name
        access_stage = STAGE_MEDIA / access_name
        provenance = dict(workbook_metadata)
        contributor = str(provenance.get("contributor") or "")
        provider = "Industriesalon Schöneweide (ISIL DE-MUS-056528)"
        provenance.update(
            {
                "case_id": f"PARAGON-{INVENTORY}-{part.upper()}",
                "source_file_id": str(source_path),
                "source_digest": source["sha256"],
                "reviewed_bundle_digest": reviewed_digest,
                "museum_digital_id": MD_ID,
                "representation_role": "preservation_surrogate",
                "provenance_gaps": [
                    "missing_local_tiff_master",
                    "low_resolution_jpeg_source",
                ],
                "rights_statement": RIGHTS["image_statement"],
                "rights_holder": RIGHTS["rights_holder"],
                "rights_usage_terms": RIGHTS["image_statement"],
                "rights_web_statement": RIGHTS["image_uri"],
                "credit_line": "Industriesalon Schöneweide",
                "contributor": (
                    f"{contributor}; {provider}" if contributor else provider
                ),
                "image_reference": (
                    f"{provenance.get('image_reference', '')}; "
                    f"{part} view"
                ).strip("; "),
            }
        )
        embedded = tiff_metadata.expected_tags(
            tiff_metadata.PROFILE_V2, metadata, provenance
        )
        surrogate_accession._write_tiff_representation(
            source_path, master_stage, embedded
        )
        shutil.copy2(source_path, access_stage)
        if sha256(access_stage) != source["sha256"]:
            raise RuntimeError(f"{part} staged JPEG differs from source")
        readback = tiff_accession._exif_values(master_stage, tuple(embedded))
        if readback != embedded:
            raise RuntimeError(f"{part} TIFF metadata readback differs")
        technical = tiff_accession._exif_values(
            master_stage, tiff_accession.TECHNICAL_TAGS
        )
        bit_depth = tiff_accession._scalar_sample_depth(
            technical["IFD0:BitsPerSample"]
        )
        decoded_source = tiff_accession._decoded_pixel_digest(
            source_path, bit_depth
        )
        decoded_tiff = tiff_accession._decoded_pixel_digest(
            master_stage, bit_depth
        )
        if decoded_source != decoded_tiff:
            raise RuntimeError(f"{part} surrogate changed decoded pixels")
        if tiff_accession._page_count(master_stage) != 1:
            raise RuntimeError(f"{part} surrogate is not one TIFF image")
        results.append(
            {
                **source,
                "entity_id": entity_id,
                "catalog_revision_at_embedding": revision,
                "master_name": master_name,
                "access_name": access_name,
                "master_stage": str(master_stage),
                "access_stage": str(access_stage),
                "master_sha256": sha256(master_stage),
                "master_size_bytes": master_stage.stat().st_size,
                "decoded_pixel_sha256": decoded_tiff,
                "bit_depth": bit_depth,
                "color_space": str(technical["IFD0:PhotometricInterpretation"]),
                "compression": str(technical["IFD0:Compression"]),
                "embedded_metadata": embedded,
                "provenance": provenance,
            }
        )
    return results


def media_destinations(entity_id: str, item: dict) -> dict[str, Path]:
    return {
        "work_master": (
            ROOT / f"units/{UNIT}/data/masters/{item['master_name']}"
        ),
        "work_access": (
            ROOT / f"units/{UNIT}/data/derivatives/{item['access_name']}"
        ),
        "final_master": FINAL / "masters" / item["master_name"],
        "final_access": FINAL / "export/access" / item["access_name"],
    }


def copy_and_register(
    entity_id: str, items: list[dict], created: list[Path]
) -> None:
    for item in items:
        destinations = media_destinations(entity_id, item)
        safe_copy(
            Path(item["master_stage"]),
            destinations["work_master"],
            item["master_sha256"],
        )
        created.append(destinations["work_master"])
        safe_copy(
            Path(item["access_stage"]),
            destinations["work_access"],
            item["sha256"],
        )
        created.append(destinations["work_access"])
        safe_copy(
            Path(item["master_stage"]),
            destinations["final_master"],
            item["master_sha256"],
        )
        created.append(destinations["final_master"])
        safe_copy(
            Path(item["access_stage"]),
            destinations["final_access"],
            item["sha256"],
        )
        created.append(destinations["final_access"])

    connection = catalog_core.connect_catalog(CATALOG)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = catalog_core.catalog_state(connection)
        expected = {item["catalog_revision_at_embedding"] for item in items}
        if (
            len(expected) != 1
            or int(state["catalog_revision"]) not in expected
            or int(state["published_revision"]) not in expected
        ):
            raise RuntimeError("catalog changed after TIFF embedding")
        timestamp = utc_now()
        registrations = []
        for item in items:
            destinations = media_destinations(entity_id, item)
            blobs = (
                (
                    item["master_sha256"],
                    item["master_size_bytes"],
                    "image/tiff",
                    item["width"],
                    item["height"],
                    item["bit_depth"],
                    item["color_space"],
                    item["compression"],
                ),
                (
                    item["sha256"],
                    item["size_bytes"],
                    "image/jpeg",
                    item["width"],
                    item["height"],
                    8,
                    "sRGB",
                    "JPEG",
                ),
            )
            for digest, size, media, width, height, depth, color, compression in blobs:
                row = connection.execute(
                    "SELECT size_bytes FROM content_blobs WHERE content_digest=?",
                    (digest,),
                ).fetchone()
                if row and int(row["size_bytes"]) != int(size):
                    raise RuntimeError("content digest size conflict")
                if not row:
                    connection.execute(
                        """INSERT INTO content_blobs (
                               content_digest, size_bytes, media_type, width_px,
                               height_px, bit_depth, color_space, compression,
                               page_count, duration_ms, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)""",
                        (
                            digest,
                            size,
                            media,
                            width,
                            height,
                            depth,
                            color,
                            compression,
                            timestamp,
                        ),
                    )
            master_occurrence = f"OCC-{uuid.uuid4().hex.upper()}"
            access_occurrence = f"OCC-{uuid.uuid4().hex.upper()}"
            master_relative = destinations["work_master"].relative_to(ROOT).as_posix()
            access_relative = destinations["work_access"].relative_to(ROOT).as_posix()
            connection.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc, observed_at_utc,
                       status, note
                   ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                             'verified', ?)""",
                (
                    master_occurrence,
                    UNIT,
                    item["master_sha256"],
                    master_relative,
                    item["master_name"],
                    timestamp,
                    timestamp,
                    (
                        "Manufactured metadata-enriched preservation surrogate; "
                        f"profile {tiff_metadata.PROFILE_V2}; "
                        f"representation_part={item['part']}; "
                        f"JPEG source {item['sha256']}; decoded pixels "
                        f"{item['decoded_pixel_sha256']}; source path "
                        f"{item['path']}"
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO file_occurrences (
                       occurrence_id, unit_id, batch_id, content_digest,
                       occurrence_kind, source_root_id, relative_path,
                       original_filename, observed_mtime_utc, observed_at_utc,
                       status, note
                   ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                             'verified', ?)""",
                (
                    access_occurrence,
                    UNIT,
                    item["sha256"],
                    access_relative,
                    item["access_name"],
                    timestamp,
                    timestamp,
                    json.dumps(
                        {
                            "derivation": "exact_low_resolution_jpeg_source_copy",
                            "museum_digital_id": MD_ID,
                            "museum_digital_url": OBJECT_URL,
                            "representation_part": item["part"],
                            "source_path": item["path"],
                            "surrogate_master_digest": item["master_sha256"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, ?, 'preservation_surrogate', 'active', ?)""",
                (entity_id, master_occurrence, timestamp),
            )
            connection.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, ?, 'access_derivative', 'active', ?)""",
                (entity_id, access_occurrence, timestamp),
            )
            registrations.append(
                {
                    "part": item["part"],
                    "master_occurrence_id": master_occurrence,
                    "access_occurrence_id": access_occurrence,
                    "master_digest": item["master_sha256"],
                    "access_digest": item["sha256"],
                }
            )
        catalog_core._append_log(
            connection,
            ACTOR,
            "register_multiview_preservation_surrogates",
            "archive_object",
            entity_id,
            UNIT,
            {
                "registrations": registrations,
                "museum_digital_id": MD_ID,
                "source_deleted": False,
                "deletion_authorized": False,
            },
            evidence=canonical_digest(registrations),
            note=(
                "One physical photograph accessioned with front and verso; "
                "both TIFFs are manufactured pixel-equivalent surrogates."
            ),
            at_utc=timestamp,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    connection = catalog_core.connect_catalog(CATALOG)
    try:
        connection.execute("BEGIN IMMEDIATE")
        timestamp = utc_now()
        catalog_core._append_log(
            connection,
            ACTOR,
            "publish_simplified_unit_accession",
            "unit",
            UNIT,
            UNIT,
            {
                "entity_id": entity_id,
                "inventory": INVENTORY,
                "media_files_added": 4,
                "media_rewritten_after_accession": False,
            },
            evidence=canonical_digest(items),
            note="Publish VLV-1097 object and two surrogate/access pairs.",
            at_utc=timestamp,
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.execute(
            "UPDATE catalog_state SET published_revision=?, dirty_since=NULL WHERE singleton=1",
            (revision,),
        )
        connection.commit()
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
def stage_controls(entity_id: str, items: list[dict]) -> dict:
    STAGE_CONTROL.mkdir(parents=True)
    connection = catalog_core.connect_catalog(CATALOG, read_only=True)
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        state = dict(catalog_core.catalog_state(connection))
        rows = unit_catalog_rows(connection, UNIT)
    finally:
        connection.close()
    restore_csv = write_stage_bytes(
        "db-restore/RESTORE.csv", unit_catalog_csv_bytes(rows)
    )
    restore_xlsx = write_stage_bytes(
        "db-restore/RESTORE.xlsx", unit_catalog_xlsx_bytes(rows)
    )
    (STAGE_CONTROL / "documents").mkdir(parents=True)
    shutil.copy2(restore_csv, STAGE_CONTROL / "documents/catalog.csv")
    shutil.copy2(restore_xlsx, STAGE_CONTROL / "documents/catalog.xlsx")
    restore_sqlite = STAGE_CONTROL / "db-restore/RESTORE.sqlite"
    backup_sqlite(CATALOG, restore_sqlite)
    restore = sqlite3.connect(f"file:{restore_sqlite}?mode=ro", uri=True)
    try:
        write_stage_text(
            "db-restore/RESTORE.sql",
            "\n".join(restore.iterdump()) + "\n",
        )
    finally:
        restore.close()

    human_dir = RUN / "human-build"
    human = build_human_catalog(
        restore_csv, restore_xlsx, WORKBOOK, human_dir
    )
    shutil.copy2(
        human_dir / "CATALOG-HUMAN.csv",
        STAGE_CONTROL / "CATALOG-HUMAN.csv",
    )
    shutil.copy2(
        human_dir / "CATALOG.xlsx",
        STAGE_CONTROL / "CATALOG.xlsx",
    )
    human["source_catalog_csv"] = str(FINAL / "db-restore/RESTORE.csv")
    human["source_catalog_xlsx"] = str(FINAL / "db-restore/RESTORE.xlsx")
    for key, relative in (
        ("restore_csv", "db-restore/RESTORE.csv"),
        ("restore_xlsx", "db-restore/RESTORE.xlsx"),
        ("human_csv", "CATALOG-HUMAN.csv"),
        ("human_xlsx", "CATALOG.xlsx"),
    ):
        human["outputs"][key]["path"] = str(FINAL / relative)
        human["outputs"][key]["sha256"] = sha256(STAGE_CONTROL / relative)
    write_stage_json("catalog-export-summary.json", human)

    manifest_path = FINAL / "db-restore/manifests/representations.csv"
    with manifest_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        representations = list(reader)
    if any(row["iss_id"] == entity_id for row in representations):
        raise RuntimeError("VLV-1097 manifest rows already exist")
    generated_at = utc_now()
    for item in items:
        destinations = media_destinations(entity_id, item)
        representations.append(
            {
                "iss_id": entity_id,
                "inventory": INVENTORY,
                "source_role": "preservation_surrogate",
                "master_path": destinations["final_master"].relative_to(FINAL).as_posix(),
                "master_sha256": item["master_sha256"],
                "access_path": destinations["final_access"].relative_to(FINAL).as_posix(),
                "access_sha256": item["sha256"],
                "access_source": destinations["work_access"].relative_to(ROOT).as_posix(),
                "derivation_status": (
                    f"pixel_equivalent_tiff_surrogate_from_low_resolution_"
                    f"jpeg_{item['part']}"
                ),
                "target_system": "local_access",
                "width_px": str(item["width"]),
                "height_px": str(item["height"]),
                "generated_by": TOOL_VERSION,
                "generated_at": generated_at,
            }
        )
    representations.sort(
        key=lambda row: (
            row["inventory"].casefold(),
            row["iss_id"],
            row["master_path"],
        )
    )
    staged_manifest = STAGE_CONTROL / "db-restore/manifests/representations.csv"
    staged_manifest.parent.mkdir(parents=True, exist_ok=True)
    with staged_manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(representations)

    objects = len({row["iss_id"] for row in rows})
    summary = json.loads(
        (FINAL / "db-restore/export-summary.json").read_text(encoding="utf-8")
    )
    summary.update(
        {
            "created_at": utc_now(),
            "source_root": str(ROOT),
            "output": str(FINAL),
            "catalog_state": state,
            "objects": objects,
            "masters": len(representations),
            "access": len(representations),
        }
    )
    summary["restore"] = {
        "sqlite": str(FINAL / "db-restore/RESTORE.sqlite"),
        "sql": str(FINAL / "db-restore/RESTORE.sql"),
        "csv": str(FINAL / "db-restore/RESTORE.csv"),
        "xlsx": str(FINAL / "db-restore/RESTORE.xlsx"),
        "rows": len(rows),
    }
    write_stage_json("db-restore/export-summary.json", summary)
    write_stage_text(
        "README.md",
        "# WF-PARAGON\n\n"
        "Simplified archive workflow export.\n\n"
        "- `masters/`: preservation master/surrogate TIFFs.\n"
        "- `export/access/`: local access JPEGs.\n"
        "- `documents/`: source and historical export evidence.\n"
        "- `db-restore/`: SQLite, SQL dump, full restore exports, checksums, "
        "and manifests.\n"
        "- `CATALOG.xlsx`: human-readable object catalog.\n\n"
        f"Updated: {summary['created_at']}\n"
        f"Catalog revision: {state['catalog_revision']}\n"
        f"Active objects: {objects}\n"
        f"Preservation representations: {len(representations)}\n\n"
        "Corrections and additions:\n\n"
        "- `VLV-1095-1` is retired as a false second object; its TIFF/JPEG "
        "remain the open-interior view of `VLV-1095`.\n"
        "- `VLV-1097` is preserved as one photograph with front and verso. "
        "The exact low-resolution JPEGs remain access/source copies; both "
        "TIFFs are documented pixel-equivalent manufactured surrogates.\n\n"
        "This unit is not ready for sealing until the mandatory "
        "`db-restore/manifests/source-disposition.csv` is complete.\n",
    )
    return {
        "state": state,
        "restore_rows": len(rows),
        "objects": objects,
        "representation_manifest_rows": len(representations),
    }


def stage_checksums(entity_id: str, items: list[dict]) -> None:
    source = FINAL / "db-restore/checksums.csv"
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    by_path = {row["path"]: row for row in rows}
    changed_controls = set(CONTROL_PATHS) - {"db-restore/checksums.csv"}
    for relative in changed_controls:
        path = STAGE_CONTROL / relative
        if relative not in by_path:
            raise RuntimeError(f"missing control checksum row: {relative}")
        by_path[relative]["sha256"] = sha256(path)
        by_path[relative]["size_bytes"] = str(path.stat().st_size)
    for item in items:
        destinations = media_destinations(entity_id, item)
        for key, digest_key in (
            ("final_master", "master_sha256"),
            ("final_access", "sha256"),
        ):
            path = destinations[key]
            relative = path.relative_to(FINAL).as_posix()
            if relative in by_path:
                raise RuntimeError(f"new media checksum row already exists: {relative}")
            rows.append(
                {
                    "path": relative,
                    "sha256": item[digest_key],
                    "size_bytes": str(path.stat().st_size),
                }
            )
    rows.sort(key=lambda row: row["path"])
    destination = STAGE_CONTROL / "db-restore/checksums.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def promote_controls() -> None:
    for relative in CONTROL_PATHS:
        if not (STAGE_CONTROL / relative).is_file():
            raise RuntimeError(f"staged control missing: {relative}")
    for relative in CONTROL_PATHS:
        os.replace(STAGE_CONTROL / relative, FINAL / relative)


def validate_final(entity_id: str, items: list[dict], source_before: list[dict]) -> dict:
    for source in source_before:
        path = Path(source["path"])
        if (
            sha256(path) != source["sha256"]
            or path.stat().st_size != source["size_bytes"]
            or path.stat().st_mtime_ns != source["mtime_ns"]
        ):
            raise RuntimeError(f"source changed: {path}")
    with (FINAL / "db-restore/checksums.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        checksum_rows = list(csv.DictReader(handle))
    checksum_index = {row["path"]: row for row in checksum_rows}
    for relative in set(CONTROL_PATHS) - {"db-restore/checksums.csv"}:
        path = FINAL / relative
        if sha256(path) != checksum_index[relative]["sha256"]:
            raise RuntimeError(f"control checksum mismatch: {relative}")
    for item in items:
        for destination in media_destinations(entity_id, item).values():
            expected = (
                item["master_sha256"]
                if destination.suffix.casefold() in {".tif", ".tiff"}
                else item["sha256"]
            )
            if sha256(destination) != expected:
                raise RuntimeError(f"media checksum mismatch: {destination}")
        final_master = media_destinations(entity_id, item)["final_master"]
        technical = tiff_accession._exif_values(
            final_master, tiff_accession.TECHNICAL_TAGS
        )
        if (
            technical.get("File:FileType") != "TIFF"
            or str(technical.get("IFD0:Compression")) != "Uncompressed"
        ):
            raise RuntimeError(f"surrogate technical profile failed: {final_master}")

    restore_path = FINAL / "db-restore/RESTORE.sqlite"
    restore = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
    restore.row_factory = sqlite3.Row
    try:
        if restore.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("RESTORE.sqlite integrity failed")
        if list(restore.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("RESTORE.sqlite foreign key check failed")
        state = dict(restore.execute("SELECT * FROM catalog_state").fetchone())
        entity = restore.execute(
            """SELECT e.status, a.archival_label, a.descriptive_title
               FROM entities e JOIN archive_objects a USING(entity_id)
               WHERE e.entity_id=?""",
            (entity_id,),
        ).fetchone()
        identifiers = {
            (row["namespace"], row["value"])
            for row in restore.execute(
                """SELECT namespace,value FROM entity_identifiers
                   WHERE entity_id=? AND status='active'""",
                (entity_id,),
            )
        }
        roles = {
            row["role"]: row["count"]
            for row in restore.execute(
                """SELECT role,count(*) AS count FROM entity_files
                   WHERE entity_id=? AND status='active' GROUP BY role""",
                (entity_id,),
            )
        }
        rights = {
            row["predicate"]: row["value_text"]
            for row in restore.execute(
                """SELECT predicate,value_text FROM evidence_assertions
                   WHERE subject_entity_id=? AND status='accepted'
                     AND predicate LIKE 'archive_%'""",
                (entity_id,),
            )
        }
        table_counts = {
            row["name"]: restore.execute(
                f'SELECT count(*) FROM "{row["name"]}"'
            ).fetchone()[0]
            for row in restore.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
            )
        }
    finally:
        restore.close()
    if (
        not entity
        or entity["status"] != "active"
        or entity["archival_label"] != INVENTORY
        or ("museum-digital", MD_ID) not in identifiers
        or roles != {"access_derivative": 2, "preservation_surrogate": 2}
        or rights.get("archive_provider_isil") != RIGHTS["provider_isil"]
        or rights.get("archive_rights_web_statement") != RIGHTS["image_uri"]
        or state["catalog_revision"] != state["published_revision"]
    ):
        raise RuntimeError("catalog accession state failed")

    replay_path = RUN / "sql-replay.sqlite"
    replay = sqlite3.connect(replay_path)
    try:
        replay.executescript(
            (FINAL / "db-restore/RESTORE.sql").read_text(encoding="utf-8")
        )
        if replay.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("RESTORE.sql replay integrity failed")
        replay_counts = {
            name: replay.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in table_counts
        }
        if replay_counts != table_counts:
            raise RuntimeError("RESTORE.sql replay table counts differ")
    finally:
        replay.close()

    with (FINAL / "db-restore/manifests/representations.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        manifest = list(csv.DictReader(handle))
    entity_rows = [row for row in manifest if row["iss_id"] == entity_id]
    if len(entity_rows) != 2:
        raise RuntimeError("representation manifest does not contain two views")
    return {
        "catalog_revision": state["catalog_revision"],
        "published_revision": state["published_revision"],
        "entity_id": entity_id,
        "inventory": entity["archival_label"],
        "title": entity["descriptive_title"],
        "active_roles": roles,
        "manifest_rows": len(entity_rows),
        "restore_integrity": "ok",
        "sql_replay": "ok",
        "source_unchanged": True,
        "source_deleted": False,
        "deletion_authorized": False,
    }


def main() -> int:
    if any(path.exists() for path in (STAGE_MEDIA, STAGE_CONTROL, BEFORE_CONTROL, BACKUP)):
        raise RuntimeError("accession run already contains execution state")
    backup_sqlite(CATALOG, BACKUP)
    backup_controls()
    created_media: list[Path] = []
    controls_promoted = False
    try:
        md, raw, snapshot_digest = fetch_snapshot()
        sources = [
            inspect_source(SOURCE_FRONT, "front"),
            inspect_source(SOURCE_VERSO, "verso"),
        ]
        plan = {
            "format": "iss-paragon-multiview-surrogate-plan-v1",
            "unit": UNIT,
            "inventory": INVENTORY,
            "museum_digital_id": MD_ID,
            "museum_digital_snapshot_sha256": snapshot_digest,
            "sources": sources,
            "rights": RIGHTS,
            "decision": (
                "One object with front and verso; create documented "
                "pixel-equivalent TIFF surrogates and retain exact JPEGs."
            ),
        }
        reviewed_digest = canonical_digest(plan)
        plan["reviewed_bundle_digest"] = reviewed_digest
        (RUN / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        entity_id = ensure_metadata(md, snapshot_digest)
        items = create_surrogates(entity_id, sources, reviewed_digest)
        copy_and_register(entity_id, items, created_media)
        staged = stage_controls(entity_id, items)
        stage_checksums(entity_id, items)
        controls_promoted = True
        promote_controls()
        validation = validate_final(entity_id, items, sources)
        receipt = {
            "format": "iss-paragon-multiview-surrogate-accession-receipt-v1",
            "completed_at": utc_now(),
            "unit": UNIT,
            "inventory": INVENTORY,
            "entity_id": entity_id,
            "museum_digital_id": MD_ID,
            "museum_digital_url": OBJECT_URL,
            "museum_digital_snapshot_sha256": snapshot_digest,
            "reviewed_bundle_digest": reviewed_digest,
            "sources": sources,
            "representations": items,
            "staged": staged,
            "validation": validation,
            "catalog_backup": str(BACKUP),
            "final_control_backup": str(BEFORE_CONTROL),
            "source_deleted": False,
            "deletion_authorized": False,
        }
        (RUN / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "result": "ok",
                    "entity_id": entity_id,
                    "catalog_revision": validation["catalog_revision"],
                    "sources": [
                        {
                            "part": item["part"],
                            "path": item["path"],
                            "sha256": item["sha256"],
                            "width": item["width"],
                            "height": item["height"],
                        }
                        for item in items
                    ],
                    "surrogates": [
                        {
                            "part": item["part"],
                            "name": item["master_name"],
                            "sha256": item["master_sha256"],
                            "decoded_pixel_sha256": item["decoded_pixel_sha256"],
                        }
                        for item in items
                    ],
                    "validation": validation,
                    "receipt": str(RUN / "receipt.json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    except Exception as error:
        if controls_promoted:
            restore_controls()
        for path in reversed(created_media):
            path.unlink(missing_ok=True)
        restore_catalog()
        (RUN / "failure.json").write_text(
            json.dumps(
                {
                    "failed_at": utc_now(),
                    "error": f"{type(error).__name__}: {error}",
                    "catalog_restored": BACKUP.exists(),
                    "controls_restored": controls_promoted,
                    "created_media_removed": len(created_media),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
