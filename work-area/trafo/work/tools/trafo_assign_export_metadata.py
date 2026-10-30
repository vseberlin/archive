#!/usr/bin/env python3
"""Assign approved Trafo rights/provider metadata without touching media.

The operation updates only current catalog/restore metadata, the 44 unmatched
accepted-metadata sidecars, the unit README, the export profile, and their
checksum-ledger rows. Existing TIFF and PDF representations are neither opened
nor rehashed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


RUN_ID = "trafo-metadata-export-readiness-001"
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
WORK = Path("/mnt/volume/iss-archive-work/trafo")
RUN_ROOT = WORK / "work/metadata-runs" / RUN_ID
STAGE = RUN_ROOT / "stage"
ROLLBACK = RUN_ROOT / "rollback"
PLAN = RUN_ROOT / "plan.json"
VALIDATION = RUN_ROOT / "validation.json"
PUBLICATION = RUN_ROOT / "publication.json"
EXPORT_PROFILE = "export/md/export-profile.json"
TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))
import paragon_document_bundle  # noqa: E402


RIGHTS = "CC BY-SA"
METADATA_LICENSE = "CC0"
RIGHTS_HOLDER = "www.industriesalon.de"
PROVIDER_NAME = "Industriesalon Schöneweide"
PROVIDER_ISIL = "DE-MUS-056528"
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
EXPECTED_LOCAL = (
    [f"TRO-1988-{number:03d}" for number in range(1, 43)]
    + ["TRO-1957-037-02", "TRO-1989-Deckblatt"]
)
CORE_REPLACEMENTS = [
    "CATALOG.csv",
    "CATALOG.xlsx",
    "README.md",
    "db-restore/RESTORE.sql",
    "db-restore/RESTORE.sqlite",
]
UNCHANGED_RESTORE = [
    "db-restore/RESTORE.csv",
    "db-restore/RESTORE.xlsx",
    "db-restore/manifests/representations.csv",
    "db-restore/manifests/source-disposition.csv",
]


class MetadataError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def local_sidecar(inventory: str) -> str:
    return f"documents/metadata-sources/{inventory}.json"


def changed_payloads() -> list[str]:
    return sorted(
        CORE_REPLACEMENTS
        + [local_sidecar(inventory) for inventory in EXPECTED_LOCAL]
        + [EXPORT_PROFILE]
    )


def replacements() -> list[str]:
    return changed_payloads() + ["db-restore/checksums.csv"]


def assert_baseline() -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    if not FINAL.is_dir() or FINAL.is_symlink():
        raise MetadataError("Trafo unit is unavailable or is a symlink")
    catalog = read_csv(FINAL / "CATALOG.csv")
    if len(catalog) != 1592:
        raise MetadataError(f"catalog count differs: {len(catalog)}")
    local = sorted(
        row["inventory"]
        for row in catalog
        if not row["museum_digital_object_id"]
    )
    if local != sorted(EXPECTED_LOCAL):
        raise MetadataError(f"local-only scope differs: {local}")
    unresolved = sorted(
        row["inventory"]
        for row in catalog
        if row["rights_usage_terms"] == "unknown"
        or row["metadata_license"] == "not supplied"
    )
    if unresolved != sorted(EXPECTED_LOCAL):
        raise MetadataError(f"unresolved-rights scope differs: {unresolved}")
    for relative in CORE_REPLACEMENTS + [
        local_sidecar(inventory) for inventory in EXPECTED_LOCAL
    ]:
        path = FINAL / relative
        if not path.is_file() or path.is_symlink():
            raise MetadataError(f"required baseline file unavailable: {relative}")
    if (FINAL / EXPORT_PROFILE).exists():
        raise MetadataError("export profile already exists; refusing overwrite")
    watched = {
        relative: identity(FINAL / relative)
        for relative in CORE_REPLACEMENTS
        + UNCHANGED_RESTORE
        + ["db-restore/checksums.csv"]
        + [local_sidecar(inventory) for inventory in EXPECTED_LOCAL]
    }
    return catalog, watched


def update_sidecar(
    inventory: str, catalog_row: dict[str, str]
) -> dict[str, Any]:
    path = FINAL / local_sidecar(inventory)
    value = json.loads(path.read_text(encoding="utf-8"))
    accepted = value["accepted_metadata"]
    accepted["inventory"] = inventory
    accepted["title"] = catalog_row["title"]
    accepted["description"] = catalog_row["description"]
    accepted["date_created"] = catalog_row["date_created"]
    accepted["object_type"] = catalog_row["object_type"]
    accepted["material_technique"] = catalog_row["material_technique"]
    accepted["dimensions"] = catalog_row["dimensions"]
    accepted["collection"] = catalog_row["collection_name"]
    accepted.pop("collection_name", None)
    accepted["rights_usage_terms"] = RIGHTS
    accepted["metadata_license"] = METADATA_LICENSE
    accepted["rights_holder"] = RIGHTS_HOLDER
    accepted["credit_line"] = RIGHTS_HOLDER
    accepted["metadata_source"] = catalog_row["metadata_source"]
    gaps = [
        gap
        for gap in value.get("provenance_gaps", [])
        if gap
        not in {
            "rights-not-supplied",
            "metadata-license-not-supplied",
            "exact-publication-date-not-reconciled",
        }
    ]
    value["provenance_gaps"] = gaps
    evidence = value.setdefault("evidence", {})
    if inventory.startswith("TRO-1988-"):
        evidence["exact_issue_date"] = catalog_row["date_created"]
    evidence["rights_and_provider_decision"] = {
        "decision_date": "2026-07-27",
        "decision_basis": (
            "Curator assignment consistent with the accepted "
            "museum-digital-backed Trafo records."
        ),
        "metadata_license": METADATA_LICENSE,
        "object_rights": RIGHTS,
        "provider_isil": PROVIDER_ISIL,
        "provider_name": PROVIDER_NAME,
        "rights_holder": RIGHTS_HOLDER,
    }
    return value


def export_profile() -> dict[str, Any]:
    return {
        "format": "iss-md-ddb-export-profile-v1",
        "unit": "trafo",
        "provider": {
            "identifier": PROVIDER_ISIL,
            "identifier_type": "ISIL",
            "name": PROVIDER_NAME,
        },
        "default_record_values": {
            "language": "de",
            "media_type": "TEXT",
            "metadata_license": {
                "label": METADATA_LICENSE,
                "uri": "https://creativecommons.org/publicdomain/zero/1.0/",
            },
            "object_rights": {
                "label": RIGHTS,
                "uri": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "rights_holder": RIGHTS_HOLDER,
        },
        "resource_policy": {
            "museum_digital_backed_records": (
                "Use the accepted museum-digital object and resource URLs."
            ),
            "local_only_records": (
                "Submit the archived access PDF to museum-digital; use the "
                "resulting persistent public object/resource URL for DDB."
            ),
        },
        "object_type_policy": {
            "Zeitung": (
                "Reuse the established museum-digital Trafo object-type mapping."
            ),
            "Sammlungsbegleitmaterial": (
                "Confirm the controlled museum-digital object type during "
                "pilot import before DDB export."
            ),
        },
        "decision": {
            "date": "2026-07-27",
            "basis": (
                "Curator assignment: use the same rights and metadata licence "
                "as the accepted museum-digital Trafo records; Industriesalon "
                "Schöneweide provider ISIL confirmed as DE-MUS-056528."
            ),
        },
    }


def updated_readme() -> bytes:
    text = (FINAL / "README.md").read_text(encoding="utf-8")
    text = text.replace(
        "Rights and metadata licence remain explicitly unresolved.",
        (
            "Object rights are `CC BY-SA` and the metadata licence is `CC0`, "
            "assigned consistently with the museum-digital-backed Trafo records."
        ),
    )
    text = text.replace(
        "Neither object has a matching\nmuseum-digital record. Rights and "
        "metadata licence remain unresolved.",
        (
            "Neither object has a matching\nmuseum-digital record. Object "
            "rights are `CC BY-SA` and the metadata licence is `CC0`, assigned "
            "consistently with the museum-digital-backed Trafo records."
        ),
    )
    marker = "Restore state and complete fixity information are under `db-restore/`."
    insertion = (
        marker
        + "\n\n## Export metadata\n\n"
        "Industriesalon Schöneweide is identified by ISIL `DE-MUS-056528`. "
        "`export/md/export-profile.json` records the approved rights, metadata "
        "licence, provider identity, and default DDB/museum-digital export "
        "values. The 44 records without museum-digital IDs require publication "
        "in museum-digital before their persistent public URLs can be supplied "
        "to DDB."
    )
    if marker not in text:
        raise MetadataError("README insertion marker unavailable")
    return text.replace(marker, insertion).encode("utf-8")


def build_stage() -> dict[str, Any]:
    catalog, watched = assert_baseline()
    if RUN_ROOT.exists():
        raise MetadataError(f"run root already exists: {RUN_ROOT}")
    STAGE.mkdir(parents=True)
    catalog_by_inventory = {row["inventory"]: row for row in catalog}
    for inventory in EXPECTED_LOCAL:
        row = catalog_by_inventory[inventory]
        row["rights_usage_terms"] = RIGHTS
        row["metadata_license"] = METADATA_LICENSE
        write_json(
            STAGE / local_sidecar(inventory),
            update_sidecar(inventory, row),
        )
    database_path = STAGE / "db-restore/RESTORE.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FINAL / "db-restore/RESTORE.sqlite", database_path)
    connection = sqlite3.connect(database_path)
    try:
        placeholders = ",".join("?" for _ in EXPECTED_LOCAL)
        connection.execute(
            f"UPDATE archive_object SET rights_usage_terms=?, "
            f"metadata_license=? WHERE inventory IN ({placeholders})",
            [RIGHTS, METADATA_LICENSE, *EXPECTED_LOCAL],
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MetadataError("staged SQLite integrity check failed")
        rows = connection.execute(
            "SELECT * FROM archive_object ORDER BY inventory"
        ).fetchall()
        fields = [column[0] for column in connection.execute(
            "SELECT * FROM archive_object LIMIT 0"
        ).description]
        sql_dump = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    staged_catalog = [dict(zip(fields, row, strict=True)) for row in rows]
    write_csv(STAGE / "CATALOG.csv", staged_catalog, CATALOG_FIELDS)
    (STAGE / "CATALOG.xlsx").write_bytes(
        paragon_document_bundle.table_xlsx_bytes(
            CATALOG_FIELDS,
            [
                {field: str(row.get(field, "")) for field in CATALOG_FIELDS}
                for row in staged_catalog
            ],
            sheet_name="Trafo",
            table_name="TrafoCatalog",
        )
    )
    (STAGE / "README.md").write_bytes(updated_readme())
    (STAGE / "db-restore/RESTORE.sql").write_text(sql_dump, encoding="utf-8")
    write_json(STAGE / EXPORT_PROFILE, export_profile())
    old_checksums = read_csv(FINAL / "db-restore/checksums.csv")
    checksums = {row["path"]: row for row in old_checksums}
    if len(checksums) != len(old_checksums):
        raise MetadataError("baseline checksum paths are not unique")
    changed_hashes: dict[str, str] = {}
    for relative in changed_payloads():
        staged = STAGE / relative
        digest = sha256_file(staged)
        changed_hashes[relative] = digest
        checksums[relative] = {
            "path": relative,
            "sha256": digest,
            "size_bytes": str(staged.stat().st_size),
        }
    write_csv(
        STAGE / "db-restore/checksums.csv",
        [checksums[path] for path in sorted(checksums)],
        ["path", "sha256", "size_bytes"],
    )
    plan = {
        "format": "iss-trafo-metadata-correction-v1",
        "run_id": RUN_ID,
        "created_at": now(),
        "status": "staged-not-published",
        "scope": EXPECTED_LOCAL,
        "provider_isil": PROVIDER_ISIL,
        "rights": RIGHTS,
        "metadata_license": METADATA_LICENSE,
        "changed_payloads": changed_payloads(),
        "replacements": replacements(),
        "baseline_identity": watched,
        "changed_hashes": changed_hashes,
        "media_files_opened": 0,
        "media_files_hashed": 0,
        "media_files_rewritten": 0,
    }
    write_json(PLAN, plan)
    return validate_root(STAGE, expected_checksum_rows=len(old_checksums) + 1)


def validate_root(root: Path, *, expected_checksum_rows: int | None = None) -> dict[str, Any]:
    catalog = read_csv(root / "CATALOG.csv")
    if len(catalog) != 1592:
        raise MetadataError("validated catalog count differs")
    if any(
        row["rights_usage_terms"] != RIGHTS
        or row["metadata_license"] != METADATA_LICENSE
        for row in catalog
    ):
        raise MetadataError("catalog rights/licence coverage differs")
    catalog_by_inventory = {row["inventory"]: row for row in catalog}
    for inventory in EXPECTED_LOCAL:
        value = json.loads(
            (root / local_sidecar(inventory)).read_text(encoding="utf-8")
        )
        accepted = value["accepted_metadata"]
        row = catalog_by_inventory[inventory]
        for field in ("title", "description", "date_created"):
            if accepted[field] != row[field]:
                raise MetadataError(
                    f"sidecar/catalog mismatch: {inventory} {field}"
                )
        if (
            accepted["rights_usage_terms"] != RIGHTS
            or accepted["metadata_license"] != METADATA_LICENSE
            or accepted["rights_holder"] != RIGHTS_HOLDER
        ):
            raise MetadataError(f"sidecar rights differ: {inventory}")
        if {
            "rights-not-supplied",
            "metadata-license-not-supplied",
            "exact-publication-date-not-reconciled",
        } & set(value.get("provenance_gaps", [])):
            raise MetadataError(f"resolved gap remains: {inventory}")
    profile = json.loads((root / EXPORT_PROFILE).read_text(encoding="utf-8"))
    if profile["provider"]["identifier"] != PROVIDER_ISIL:
        raise MetadataError("export profile provider ISIL differs")
    connection = sqlite3.connect(
        f"file:{root / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        db_count = connection.execute(
            "SELECT count(*) FROM archive_object"
        ).fetchone()[0]
        unresolved = connection.execute(
            "SELECT count(*) FROM archive_object "
            "WHERE rights_usage_terms != ? OR metadata_license != ?",
            (RIGHTS, METADATA_LICENSE),
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok" or db_count != 1592 or unresolved != 0:
        raise MetadataError("restore SQLite validation differs")
    replay = sqlite3.connect(":memory:")
    try:
        replay.executescript(
            (root / "db-restore/RESTORE.sql").read_text(encoding="utf-8")
        )
        replay_counts = (
            replay.execute("SELECT count(*) FROM archive_object").fetchone()[0],
            replay.execute(
                "SELECT count(*) FROM archive_object "
                "WHERE rights_usage_terms != ? OR metadata_license != ?",
                (RIGHTS, METADATA_LICENSE),
            ).fetchone()[0],
        )
    finally:
        replay.close()
    if replay_counts != (1592, 0):
        raise MetadataError("restore SQL replay differs")
    checksum_rows = read_csv(root / "db-restore/checksums.csv")
    if expected_checksum_rows is not None and len(checksum_rows) != expected_checksum_rows:
        raise MetadataError(
            f"checksum row count differs: {len(checksum_rows)}"
        )
    by_path = {row["path"]: row for row in checksum_rows}
    for relative in changed_payloads():
        path = root / relative
        if by_path.get(relative, {}).get("sha256") != sha256_file(path):
            raise MetadataError(f"checksum differs: {relative}")
        if int(by_path[relative]["size_bytes"]) != path.stat().st_size:
            raise MetadataError(f"checksum size differs: {relative}")
    return {
        "status": "ok",
        "root": str(root),
        "objects": len(catalog),
        "local_only_records_updated": len(EXPECTED_LOCAL),
        "provider_isil": PROVIDER_ISIL,
        "rights": RIGHTS,
        "metadata_license": METADATA_LICENSE,
        "sqlite_integrity": integrity,
        "sql_replay": "ok",
        "checksum_rows": len(checksum_rows),
        "media_files_opened": 0,
        "media_files_hashed": 0,
        "media_files_rewritten": 0,
    }


def assert_identity_unchanged(baseline: dict[str, dict[str, Any]]) -> None:
    for relative, expected in baseline.items():
        observed = identity(FINAL / relative)
        for field in ("device", "inode", "size_bytes", "mtime_ns"):
            if observed[field] != expected[field]:
                raise MetadataError(
                    f"baseline changed before publication: {relative} {field}"
                )


def publish() -> dict[str, Any]:
    if not PLAN.is_file():
        raise MetadataError("staged plan unavailable")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    assert_identity_unchanged(plan["baseline_identity"])
    ROLLBACK.mkdir(parents=True, exist_ok=False)
    for relative in replacements():
        source = FINAL / relative
        if source.exists():
            destination = ROLLBACK / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    write_json(
        PUBLICATION,
        {"run_id": RUN_ID, "status": "publishing", "started_at": now()},
    )
    published: list[str] = []
    try:
        for relative in replacements():
            source = STAGE / relative
            destination = FINAL / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            published.append(relative)
        os.sync()
        result = validate_root(FINAL)
        for relative in UNCHANGED_RESTORE:
            if identity(FINAL / relative) != plan["baseline_identity"][relative]:
                raise MetadataError(f"unchanged restore file changed: {relative}")
        result.update(
            {
                "run_id": RUN_ID,
                "status": "published",
                "published_at": now(),
                "metadata_files_replaced": len(replacements()),
                "rollback_root": str(ROLLBACK),
            }
        )
        write_json(VALIDATION, result)
        write_json(PUBLICATION, result)
        return result
    except Exception as error:
        for relative in reversed(published):
            backup = ROLLBACK / relative
            destination = FINAL / relative
            if backup.exists():
                os.replace(backup, destination)
            elif destination.exists():
                destination.unlink()
        os.sync()
        write_json(
            PUBLICATION,
            {
                "run_id": RUN_ID,
                "status": "rolled-back",
                "rolled_back_at": now(),
                "reason": str(error),
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("stage", "publish", "validate"))
    args = parser.parse_args()
    if args.command == "stage":
        result = build_stage()
        write_json(VALIDATION, result)
    elif args.command == "publish":
        result = publish()
    else:
        result = validate_root(FINAL)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
