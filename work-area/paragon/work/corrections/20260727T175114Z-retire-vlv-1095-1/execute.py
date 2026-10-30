#!/usr/bin/env python3
"""Retire the false VLV-1095-1 object split without rewriting media."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

REPO_TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(REPO_TOOLS))

import catalog_core
from archive_core.catalog import (
    unit_catalog_csv_bytes,
    unit_catalog_rows,
    unit_catalog_xlsx_bytes,
)
from paragon_human_catalog import build as build_human_catalog

WORK_ROOT = Path("/mnt/volume/iss-archive-work/paragon")
CATALOG = WORK_ROOT / "work/catalog.sqlite"
FINAL = Path("/mnt/volume/iss-archive/units/paragon")
WORKBOOK = Path(
    "/media/vladimir/Elements/restore/archive/Paragon/"
    "Bildtexte Paragon stand 05.02.2020.xlsx"
)
RUN = Path(__file__).resolve().parent
STAGE = RUN / "stage"
BEFORE = RUN / "before-final"
BACKUP = RUN / "before-catalog.sqlite"
UNIT = "WF-PARAGON"
CANONICAL = "ISS-2026-YM10-B"
FORMER = "ISS-2026-H5TK-G"
ACTOR = "Vladimir"
REASON = (
    "Visual and pixel comparison proves VLV-1095 and VLV-1095-1 are cover "
    "and open-interior scans of one folding invitation; retire the false "
    "second object while retaining both representations."
)

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


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def write_bytes(relative: str, payload: bytes) -> Path:
    path = STAGE / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_text(relative: str, payload: str) -> Path:
    return write_bytes(relative, payload.encode("utf-8"))


def write_json(relative: str, payload: dict) -> Path:
    return write_text(
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
            raise RuntimeError("catalog backup failed integrity check")
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
            raise RuntimeError(f"final control file is missing: {source}")
        destination = BEFORE / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def restore_controls() -> None:
    for relative in CONTROL_PATHS:
        source = BEFORE / relative
        if not source.is_file():
            continue
        destination = FINAL / relative
        temporary = destination.with_name(f".{destination.name}.rollback")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)


def occurrence_rows(connection: sqlite3.Connection, entity_id: str) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            """SELECT ef.entity_id, ef.occurrence_id, ef.role, ef.status,
                      fo.relative_path, fo.content_digest, fo.note
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               WHERE ef.entity_id=?
               ORDER BY ef.role, fo.relative_path""",
            (entity_id,),
        )
    ]


def annotate_occurrence(
    connection: sqlite3.Connection,
    occurrence_id: str,
    part: str,
    warning: str = "",
) -> None:
    row = connection.execute(
        "SELECT note FROM file_occurrences WHERE occurrence_id=?",
        (occurrence_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"occurrence disappeared: {occurrence_id}")
    existing = str(row["note"])
    try:
        payload = json.loads(existing)
    except (TypeError, json.JSONDecodeError):
        suffix = f"; representation_part={part}"
        if warning:
            suffix += f"; {warning}"
        updated = existing + suffix
    else:
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected occurrence note: {occurrence_id}")
        payload["representation_part"] = part
        if warning:
            payload["catalog_correction"] = warning
        updated = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    connection.execute(
        "UPDATE file_occurrences SET note=? WHERE occurrence_id=?",
        (updated, occurrence_id),
    )


def mutate_catalog() -> dict:
    connection = catalog_core.connect_catalog(CATALOG)
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        state_before = dict(catalog_core.catalog_state(connection))
        if int(state_before["catalog_revision"]) != 9907:
            raise RuntimeError(f"unexpected starting revision: {state_before}")
        former = connection.execute(
            "SELECT status FROM entities WHERE entity_id=?", (FORMER,)
        ).fetchone()
        canonical = connection.execute(
            "SELECT status FROM entities WHERE entity_id=?", (CANONICAL,)
        ).fetchone()
        if not former or former["status"] != "active":
            raise RuntimeError("former entity is not active")
        if not canonical or canonical["status"] != "active":
            raise RuntimeError("canonical entity is not active")
        canonical_files = occurrence_rows(connection, CANONICAL)
        former_files = occurrence_rows(connection, FORMER)
        if len(canonical_files) != 2 or len(former_files) != 2:
            raise RuntimeError(
                f"unexpected representation counts: "
                f"{len(canonical_files)} canonical, {len(former_files)} former"
            )
        if {row["role"] for row in former_files} != {
            "access_derivative",
            "preservation_master",
        }:
            raise RuntimeError("former representation roles changed")

        timestamp = now()
        connection.execute("BEGIN IMMEDIATE")
        for row in former_files:
            connection.execute(
                """INSERT INTO entity_files (
                       entity_id, occurrence_id, role, status, created_at
                   ) VALUES (?, ?, ?, 'active', ?)""",
                (CANONICAL, row["occurrence_id"], row["role"], timestamp),
            )
        connection.execute(
            """UPDATE entity_files SET status='superseded'
               WHERE entity_id=? AND status='active'""",
            (FORMER,),
        )
        retired = connection.execute(
            """UPDATE entity_identifiers
               SET status='retired', retired_at=?
               WHERE entity_id=? AND namespace='inventory'
                 AND value='VLV-1095-1' AND status='active'""",
            (timestamp, FORMER),
        )
        if retired.rowcount != 1:
            raise RuntimeError("former inventory identifier changed")
        connection.execute(
            """INSERT INTO entity_identifiers (
                   identifier_id, entity_id, namespace, value,
                   normalized_value, scheme_version, role, status,
                   created_at, retired_at
               ) VALUES (?, ?, 'inventory', 'VLV-1095-1', 'vlv-1095-1',
                         '', 'former', 'active', ?, NULL)""",
            (f"ID-{uuid.uuid4().hex.upper()}", CANONICAL, timestamp),
        )

        all_rows = canonical_files + former_files
        for row in all_rows:
            if row["role"] == "preservation_master":
                part = (
                    "cover"
                    if row["entity_id"] == CANONICAL
                    else "open_interior"
                )
                warning = (
                    ""
                    if row["entity_id"] == CANONICAL
                    else "embedded legacy 1962 metadata retained as source error"
                )
            else:
                part = (
                    "cover_access"
                    if row["entity_id"] == CANONICAL
                    else "open_interior_access"
                )
                warning = (
                    ""
                    if row["entity_id"] == CANONICAL
                    else "VLV-1095-1 retired as false object split"
                )
            annotate_occurrence(
                connection, row["occurrence_id"], part, warning
            )

        object_row = connection.execute(
            "SELECT note FROM archive_objects WHERE entity_id=?",
            (CANONICAL,),
        ).fetchone()
        note = str(object_row["note"]).rstrip()
        correction = (
            " Catalog correction 2026-07-27: one folding invitation with "
            "cover and open-interior representations; VLV-1095-1 is a "
            "retired scan-part identifier, not a second object."
        )
        if correction.strip() not in note:
            connection.execute(
                """UPDATE archive_objects
                   SET note=?, updated_at=?
                   WHERE entity_id=?""",
                (note + correction, timestamp, CANONICAL),
            )
        catalog_core._append_log(
            connection,
            ACTOR,
            "reassign_false_split_representations",
            "archive_object",
            CANONICAL,
            UNIT,
            {
                "former_entity_id": FORMER,
                "reassigned_occurrence_ids": [
                    row["occurrence_id"] for row in former_files
                ],
                "former_identifier": "VLV-1095-1",
                "deletion_authorized": False,
            },
            before={
                "canonical_active_representations": 2,
                "former_active_representations": 2,
            },
            evidence="pixel-identity-and-visible-folding-invitation",
            note=REASON,
            at_utc=timestamp,
        )
        connection.commit()

        merge_result = catalog_core.merge_entities(
            connection,
            former_entity_id=FORMER,
            canonical_entity_id=CANONICAL,
            actor=ACTOR,
            reason=REASON,
        )

        connection.execute("BEGIN IMMEDIATE")
        publication_time = now()
        catalog_core._append_log(
            connection,
            ACTOR,
            "publish_simplified_unit_correction",
            "unit",
            UNIT,
            UNIT,
            {
                "final_unit": str(FINAL),
                "canonical_entity_id": CANONICAL,
                "retired_entity_id": FORMER,
                "media_rewritten": False,
                "media_rehashed": False,
            },
            evidence="control-projection-rebuild",
            note="Publish corrected object identity and representation mapping.",
            at_utc=publication_time,
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.execute(
            """UPDATE catalog_state
               SET published_revision=?, dirty_since=NULL
               WHERE singleton=1""",
            (revision,),
        )
        connection.commit()
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        return {
            "state_before": state_before,
            "merge_result": merge_result,
            "catalog_revision": revision,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def stage_restore_and_catalogs() -> dict:
    connection = catalog_core.connect_catalog(CATALOG, read_only=True)
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        state = dict(catalog_core.catalog_state(connection))
        rows = unit_catalog_rows(connection, UNIT)
        restore_csv = write_bytes(
            "db-restore/RESTORE.csv", unit_catalog_csv_bytes(rows)
        )
        restore_xlsx = write_bytes(
            "db-restore/RESTORE.xlsx", unit_catalog_xlsx_bytes(rows)
        )
        (STAGE / "documents").mkdir(parents=True, exist_ok=True)
        shutil.copy2(restore_csv, STAGE / "documents/catalog.csv")
        shutil.copy2(restore_xlsx, STAGE / "documents/catalog.xlsx")
    finally:
        connection.close()

    restore_sqlite = STAGE / "db-restore/RESTORE.sqlite"
    backup_sqlite(CATALOG, restore_sqlite)
    restore = sqlite3.connect(f"file:{restore_sqlite}?mode=ro", uri=True)
    try:
        write_text(
            "db-restore/RESTORE.sql",
            "\n".join(restore.iterdump()) + "\n",
        )
    finally:
        restore.close()

    human_dir = RUN / "human-build"
    human = build_human_catalog(
        restore_csv,
        restore_xlsx,
        WORKBOOK,
        human_dir,
    )
    shutil.copy2(human_dir / "CATALOG-HUMAN.csv", STAGE / "CATALOG-HUMAN.csv")
    shutil.copy2(human_dir / "CATALOG.xlsx", STAGE / "CATALOG.xlsx")
    human["source_catalog_csv"] = str(FINAL / "db-restore/RESTORE.csv")
    human["source_catalog_xlsx"] = str(FINAL / "db-restore/RESTORE.xlsx")
    for key, relative in (
        ("restore_csv", "db-restore/RESTORE.csv"),
        ("restore_xlsx", "db-restore/RESTORE.xlsx"),
        ("human_csv", "CATALOG-HUMAN.csv"),
        ("human_xlsx", "CATALOG.xlsx"),
    ):
        human["outputs"][key]["path"] = str(FINAL / relative)
        human["outputs"][key]["sha256"] = sha256(STAGE / relative)
    write_json("catalog-export-summary.json", human)

    with (FINAL / "db-restore/manifests/representations.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        representations = list(reader)
    changed = 0
    for row in representations:
        if row["iss_id"] == FORMER:
            row["iss_id"] = CANONICAL
            row["inventory"] = "VLV-1095"
            changed += 1
    if changed != 1:
        raise RuntimeError(f"unexpected former representation rows: {changed}")
    rep_path = STAGE / "db-restore/manifests/representations.csv"
    rep_path.parent.mkdir(parents=True, exist_ok=True)
    with rep_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, quoting=csv.QUOTE_MINIMAL
        )
        writer.writeheader()
        writer.writerows(representations)

    object_count = len(
        {
            row["iss_id"]
            for row in rows
            if row["object_status"] == "active"
        }
    )
    summary = json.loads(
        (FINAL / "db-restore/export-summary.json").read_text(encoding="utf-8")
    )
    summary.update(
        {
            "created_at": now(),
            "output": str(FINAL),
            "source_root": str(WORK_ROOT),
            "catalog_state": state,
            "objects": object_count,
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
    write_json("db-restore/export-summary.json", summary)
    write_text(
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
        f"Active objects: {object_count}\n"
        f"Preservation representations: {len(representations)}\n\n"
        "Catalog correction: `VLV-1095-1` was retired as a false second "
        "object. Its preserved TIFF and access JPEG remain attached to "
        "`VLV-1095` as the open-interior view; no media bytes were rewritten.\n",
    )
    return {
        "catalog_state": state,
        "restore_rows": len(rows),
        "objects": object_count,
        "representations": len(representations),
    }


def stage_checksums() -> None:
    source = FINAL / "db-restore/checksums.csv"
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    by_path = {row["path"]: row for row in rows}
    changed = set(CONTROL_PATHS) - {"db-restore/checksums.csv"}
    missing = changed - set(by_path)
    if missing:
        raise RuntimeError(f"checksum rows missing: {sorted(missing)}")
    for relative in changed:
        path = STAGE / relative
        by_path[relative]["sha256"] = sha256(path)
        by_path[relative]["size_bytes"] = str(path.stat().st_size)
    destination = STAGE / "db-restore/checksums.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def promote_controls() -> None:
    for relative in CONTROL_PATHS:
        staged = STAGE / relative
        if not staged.is_file():
            raise RuntimeError(f"staged control is missing: {staged}")
    for relative in CONTROL_PATHS:
        os.replace(STAGE / relative, FINAL / relative)


def validate_final() -> dict:
    with (FINAL / "db-restore/checksums.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        checksum_rows = list(csv.DictReader(handle))
    checksum_index = {row["path"]: row for row in checksum_rows}
    for relative in set(CONTROL_PATHS) - {"db-restore/checksums.csv"}:
        row = checksum_index[relative]
        path = FINAL / relative
        if sha256(path) != row["sha256"]:
            raise RuntimeError(f"changed control checksum failed: {relative}")
        if path.stat().st_size != int(row["size_bytes"]):
            raise RuntimeError(f"changed control size failed: {relative}")

    restore_path = FINAL / "db-restore/RESTORE.sqlite"
    restore = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
    restore.row_factory = sqlite3.Row
    try:
        if restore.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("RESTORE.sqlite integrity failed")
        if list(restore.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("RESTORE.sqlite foreign keys failed")
        former_status = restore.execute(
            "SELECT status FROM entities WHERE entity_id=?", (FORMER,)
        ).fetchone()["status"]
        redirect = restore.execute(
            """SELECT canonical_entity_id FROM entity_redirects
               WHERE former_entity_id=?""",
            (FORMER,),
        ).fetchone()["canonical_entity_id"]
        canonical_active = restore.execute(
            """SELECT count(*) FROM entity_files
               WHERE entity_id=? AND status='active'""",
            (CANONICAL,),
        ).fetchone()[0]
        former_active = restore.execute(
            """SELECT count(*) FROM entity_files
               WHERE entity_id=? AND status='active'""",
            (FORMER,),
        ).fetchone()[0]
        state = dict(restore.execute("SELECT * FROM catalog_state").fetchone())
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
        former_status != "merged"
        or redirect != CANONICAL
        or canonical_active != 4
        or former_active != 0
    ):
        raise RuntimeError("retirement state failed validation")
    if state["catalog_revision"] != state["published_revision"]:
        raise RuntimeError("restore catalog is not current")

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
        representations = list(csv.DictReader(handle))
    canonical_rows = [row for row in representations if row["iss_id"] == CANONICAL]
    former_rows = [row for row in representations if row["iss_id"] == FORMER]
    if len(canonical_rows) != 2 or former_rows:
        raise RuntimeError("representation manifest retirement mapping failed")
    for row in canonical_rows:
        if not (FINAL / row["master_path"]).is_file():
            raise RuntimeError(f"preserved master missing: {row['master_path']}")
        if not (FINAL / row["access_path"]).is_file():
            raise RuntimeError(f"access image missing: {row['access_path']}")

    return {
        "catalog_revision": state["catalog_revision"],
        "published_revision": state["published_revision"],
        "former_status": former_status,
        "redirect": redirect,
        "canonical_active_representations": canonical_active,
        "former_active_representations": former_active,
        "manifest_rows_for_canonical": len(canonical_rows),
        "manifest_rows_for_former": len(former_rows),
        "restore_integrity": "ok",
        "sql_replay": "ok",
        "media_rehashed": False,
    }


def main() -> int:
    if STAGE.exists() or BEFORE.exists() or BACKUP.exists():
        raise RuntimeError("correction run already contains execution state")
    STAGE.mkdir(parents=True)
    backup_sqlite(CATALOG, BACKUP)
    backup_controls()
    promoted = False
    try:
        mutation = mutate_catalog()
        staged = stage_restore_and_catalogs()
        stage_checksums()
        promoted = True
        promote_controls()
        validation = validate_final()
        receipt = {
            "format": "iss-paragon-object-retirement-receipt-v1",
            "completed_at": now(),
            "unit": UNIT,
            "canonical_entity_id": CANONICAL,
            "retired_entity_id": FORMER,
            "reason": REASON,
            "mutation": mutation,
            "staged": staged,
            "validation": validation,
            "catalog_backup": str(BACKUP),
            "final_control_backup": str(BEFORE),
            "media_rewritten": False,
            "source_deleted": False,
            "deletion_authorized": False,
        }
        (RUN / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        if promoted:
            restore_controls()
        restore_catalog()
        failure = {
            "failed_at": now(),
            "error": f"{type(error).__name__}: {error}",
            "catalog_restored": BACKUP.exists(),
            "final_controls_restored": promoted,
        }
        (RUN / "failure.json").write_text(
            json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
