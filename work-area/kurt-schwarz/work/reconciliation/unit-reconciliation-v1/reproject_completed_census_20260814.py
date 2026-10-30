#!/usr/bin/env python3
"""Reproject the corrected 1,935-object Kurt Schwarz unit into the workbench."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/unit-reconciliation-v1")
UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
LIVE = ROOT / "reconciliation.sqlite"
CANDIDATE = ROOT / ".reconciliation.completed-census-20260814.sqlite"
BACKUP = ROOT / "reconciliation.pre-completed-census-reprojection-20260814.sqlite"
RECEIPT = ROOT / "completed-census-reprojection-20260814.json"
ACCESSION_MANIFEST = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/md-finalization/"
    "completed-object-census-accession.csv"
)
TARGET_RECORD = "md:95019"
TARGET_CANDIDATE = "KSC-cc87203305645d2d0677"
JPEG_MASTER = "masters/KS-7-WF_0056.jpg"
JPEG_MASTER_HASH = "sha256:43357bd1e9bfc6f6860acd87e69199e85f741fa4bf52ca94f6b9d9c9bd62d145"

sys.path.insert(0, "/home/vladimir/wp/tools/archive-preservation")
from kurt_schwarz_reconciliation import KurtSchwarzReview  # noqa: E402


class ReprojectionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = list(connection.execute(f'PRAGMA table_info("{table}")'))
    return [str(row[1]) for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]


def fetch_map(
    connection: sqlite3.Connection, table: str, keys: tuple[str, ...] | list[str]
) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    columns = table_columns(connection, table)
    key_indexes = [columns.index(key) for key in keys]
    return {
        tuple(row[index] for index in key_indexes): tuple(row)
        for row in connection.execute(f'SELECT * FROM "{table}"')
    }


def validate_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReprojectionError(f"database failed quick_check: {path}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ReprojectionError(f"database failed foreign keys: {path}")
    finally:
        connection.close()


def databases_logically_equal(left: Path, right: Path) -> bool:
    first = sqlite3.connect(f"file:{left}?mode=ro&immutable=1", uri=True)
    second = sqlite3.connect(f"file:{right}?mode=ro&immutable=1", uri=True)
    try:
        first_tables = list(
            first.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        second_tables = list(
            second.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        if first_tables != second_tables:
            return False
        for table, _sql in first_tables:
            quoted = '"' + str(table).replace('"', '""') + '"'
            if list(first.execute(f"SELECT * FROM {quoted} ORDER BY rowid")) != list(
                second.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
            ):
                return False
        return True
    finally:
        first.close()
        second.close()


def accession_ids() -> set[str]:
    with ACCESSION_MANIFEST.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    identifiers = {str(row["candidate_id"]) for row in rows}
    if len(rows) != 84 or len(identifiers) != 84:
        raise ReprojectionError("84-object accession manifest drifted")
    if {row["deletion_authorized"] for row in rows} != {"false"}:
        raise ReprojectionError("accession manifest deletion guard failed")
    return identifiers


def build_fresh_projection() -> str:
    if CANDIDATE.exists():
        raise ReprojectionError(f"candidate database already exists: {CANDIDATE}")
    review = object.__new__(KurtSchwarzReview)
    review.unit_root = UNIT.resolve(strict=True)
    review.review_root = ROOT.resolve(strict=True)
    review.catalog_path = review.unit_root / "CATALOG.csv"
    review.representations_path = review.unit_root / "db-restore/manifests/representations.csv"
    review.disposition_path = review.unit_root / "db-restore/manifests/source-disposition.csv"
    review.restore_database_path = review.unit_root / "db-restore/RESTORE.sqlite"
    review.database = CANDIDATE
    review.cache_root = ROOT / "cache"
    review._analysis_lock = threading.Lock()
    review._preview_lock = threading.Semaphore(2)
    review.source_fingerprint = review._source_fingerprint()
    review._build_projection()
    validate_database(CANDIDATE)
    return review.source_fingerprint


def inspect_scope() -> dict[str, Any]:
    validate_database(LIVE)
    published = UNIT / "db-restore/RESTORE.sqlite"
    validate_database(published)
    manifest_ids = accession_ids()
    with closing(sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)) as old, closing(
        sqlite3.connect(f"file:{published}?mode=ro", uri=True)
    ) as current:
        old_keys = {str(row[0]) for row in old.execute("SELECT record_key FROM objects")}
        current_keys = {str(row[0]) for row in current.execute("SELECT record_key FROM objects")}
        added = current_keys - old_keys
        removed = old_keys - current_keys
        if len(old_keys) != 1851 or len(current_keys) != 1935:
            raise ReprojectionError(
                f"object counts drifted: old={len(old_keys)} current={len(current_keys)}"
            )
        if added != manifest_ids or removed:
            raise ReprojectionError(
                f"published delta does not match the approved 84-object manifest: added={len(added)} removed={len(removed)}"
            )
        object_row = current.execute(
            "SELECT master_relative_path,master_sha256,selected_source_format FROM objects WHERE record_key=?",
            (TARGET_RECORD,),
        ).fetchone()
        if object_row != (JPEG_MASTER, JPEG_MASTER_HASH, "jpeg"):
            raise ReprojectionError("md:95019 is not corrected to the JPEG Master")
        latest = old.execute(
            """SELECT decision_id,disposition,target_record_key FROM object_census_decisions
               WHERE decision_id=(SELECT MAX(decision_id) FROM object_census_decisions WHERE candidate_id=?)""",
            (TARGET_CANDIDATE,),
        ).fetchone()
        if latest is None or latest[1] != "redundant" or latest[2] is not None:
            raise ReprojectionError("md:95019 correction decision is not current")
        counts = {
            table: old.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "objects",
                "media_candidates",
                "metadata_fields",
                "media_decisions",
                "metadata_decisions",
                "object_census_decisions",
            )
        }
    return {
        "old_objects": 1851,
        "current_objects": 1935,
        "appended_objects": 84,
        "removed_objects": 0,
        "manifest_matches_delta": True,
        "md95019_master": JPEG_MASTER,
        "md95019_correction_decision_id": int(latest[0]),
        "live_counts": counts,
        "deletion_authorized": False,
    }


def backup_live() -> str:
    if BACKUP.exists():
        raise ReprojectionError(f"backup already exists: {BACKUP}")
    source = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    destination = sqlite3.connect(BACKUP)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    fsync_file(BACKUP)
    validate_database(BACKUP)
    return sha256_file(BACKUP)


def merge_projection(source_fingerprint: str, applied_at: str) -> dict[str, Any]:
    manifest_ids = accession_ids()
    base_keys = {
        "objects": ("record_key",),
        "media_candidates": ("candidate_id",),
        "metadata_fields": ("record_key", "field_key"),
    }
    copy_order = (
        "object_alias_decisions",
        "media_candidate_links",
        "document_candidates",
        "document_decisions",
        "object_census_candidates",
        "object_census_decisions",
        "object_census_similarity_decisions",
        "media_decisions",
        "metadata_decisions",
        "technical_analysis",
    )
    reserved_meta = {"format", "created_at", "source_fingerprint", "unit_root"}
    with closing(sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)) as old, closing(
        sqlite3.connect(CANDIDATE)
    ) as new:
        old.execute("PRAGMA query_only=ON")
        new.execute("PRAGMA foreign_keys=ON")
        old_meta = dict(old.execute("SELECT key,value FROM meta"))
        fresh_meta = dict(new.execute("SELECT key,value FROM meta"))
        if fresh_meta["source_fingerprint"] != source_fingerprint:
            raise ReprojectionError("fresh projection fingerprint drifted")
        old_keys = {str(row[0]) for row in old.execute("SELECT record_key FROM objects")}
        new_keys = {str(row[0]) for row in new.execute("SELECT record_key FROM objects")}
        if new_keys - old_keys != manifest_ids or old_keys - new_keys:
            raise ReprojectionError("fresh projection does not match the 84-object manifest")

        common_report: dict[str, dict[str, int]] = {}
        corrected_provenance_rows = 0
        for table, keys in base_keys.items():
            old_rows = fetch_map(old, table, keys)
            new_rows = fetch_map(new, table, keys)
            changed = [
                key
                for key in old_rows.keys() & new_rows.keys()
                if old_rows[key] != new_rows[key]
            ]
            removed = old_rows.keys() - new_rows.keys()
            if table == "media_candidates" and changed == [("MEDIA-CE7B4E0A2C7103CCC68A9852",)] and not removed:
                candidate_key = changed[0]
                columns = table_columns(old, table)
                old_row = dict(zip(columns, old_rows[candidate_key]))
                new_row = dict(zip(columns, new_rows[candidate_key]))
                for column in columns:
                    if column != "source_occurrences_json" and old_row[column] != new_row[column]:
                        raise ReprojectionError(
                            f"corrected md:95019 candidate changed outside provenance: {column}"
                        )
                old_occurrences = {
                    json.dumps(value, sort_keys=True)
                    for value in json.loads(old_row["source_occurrences_json"])
                }
                new_occurrences = {
                    json.dumps(value, sort_keys=True)
                    for value in json.loads(new_row["source_occurrences_json"])
                }
                added_occurrences = [
                    json.loads(value) for value in new_occurrences - old_occurrences
                ]
                if old_occurrences - new_occurrences or added_occurrences != [
                    {
                        "deletion_authorized": "false",
                        "drain_state": "retain_reviewed_redundant",
                        "preservation_status": "retained_source_redundant_after_master_selection",
                        "source_path": "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/KS-CD_WF-Doku_70er_80er_alle-Bereiche/WF Diode 213_0003.psd",
                        "source_sha256": "sha256:9ddea3a9ff5df059c428ce1c7c9ef3fb49f36f90545aab7243bd984d7cdfa226",
                        "source_size_bytes": "21497337",
                    }
                ]:
                    raise ReprojectionError(
                        "md:95019 provenance change is not the expected redundant PSD occurrence"
                    )
                corrected_provenance_rows = 1
            elif changed or removed:
                raise ReprojectionError(
                    f"{table} is not append-only: changed={len(changed)} removed={len(removed)} samples={(changed + list(removed))[:5]}"
                )
            common_report[table] = {
                "preserved": len(old_rows) - len(changed),
                "corrected": len(changed),
                "appended": len(new_rows) - len(old_rows),
            }
        if common_report != {
            "objects": {"preserved": 1851, "corrected": 0, "appended": 84},
            "media_candidates": {"preserved": 3701, "corrected": 1, "appended": 168},
            "metadata_fields": {"preserved": 14808, "corrected": 0, "appended": 672},
        }:
            raise ReprojectionError(f"unexpected append counts: {common_report}")

        for table in ("meta", *base_keys, *copy_order):
            if table_columns(old, table) != table_columns(new, table):
                raise ReprojectionError(f"schema mismatch: {table}")
        old_counts = {
            table: old.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in copy_order
        }
        old_rows_by_table = {
            table: fetch_map(old, table, primary_key_columns(old, table))
            for table in copy_order
        }
        new.execute("BEGIN IMMEDIATE")
        try:
            for table in copy_order:
                columns = table_columns(old, table)
                placeholders = ",".join("?" for _ in columns)
                quoted = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
                rows = list(old.execute(f'SELECT * FROM "{table}"'))
                if rows:
                    new.executemany(
                        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', rows
                    )
            for key, value in old_meta.items():
                if key not in reserved_meta:
                    new.execute(
                        "INSERT INTO meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
            meta_updates = {
                "prior_source_fingerprint": old_meta["source_fingerprint"],
                "reprojected_at": applied_at,
                "reprojection_scope": "published_append_84_objects_after_md95019_jpeg_correction_v1",
                "reprojection_source_database": str(BACKUP),
                "candidate_binding_rewrites": "0",
                "appended_published_objects": "84",
            }
            new.executemany(
                "INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                sorted(meta_updates.items()),
            )
            new_media = list(
                new.execute(
                    "SELECT record_key,candidate_id,content_role FROM media_candidates "
                    "WHERE record_key IN ("
                    + ",".join("?" for _ in manifest_ids)
                    + ") ORDER BY record_key,content_role,candidate_id",
                    tuple(sorted(manifest_ids)),
                )
            )
            if len(new_media) != 168:
                raise ReprojectionError(f"expected 168 appended media bindings, got {len(new_media)}")
            decisions = []
            for record_key, candidate_id, role in new_media:
                if role == "preservation_master":
                    disposition = "selected_master"
                elif role == "access_derivative":
                    disposition = "access_derivative"
                else:
                    raise ReprojectionError(f"unexpected appended media role: {role}")
                decisions.append(
                    (
                        record_key,
                        candidate_id,
                        disposition,
                        "vladimir",
                        "Mirrors the validated published role after the approved 84-object census accession; does not reopen Master selection.",
                        applied_at,
                    )
                )
            new.executemany(
                "INSERT INTO media_decisions(record_key,candidate_id,disposition,actor,reason,decided_at) "
                "VALUES (?,?,?,?,?,?)",
                decisions,
            )
            new.commit()
        except Exception:
            new.rollback()
            raise

        if new.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ReprojectionError("replacement database failed quick_check")
        violations = new.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ReprojectionError(f"replacement database has {len(violations)} foreign-key violations")
        for table, old_rows in old_rows_by_table.items():
            new_rows = fetch_map(new, table, primary_key_columns(new, table))
            missing = [
                key
                for key, row in old_rows.items()
                if key not in new_rows or new_rows[key] != row
            ]
            if missing:
                raise ReprojectionError(f"existing rows were not preserved in {table}: {missing[:5]}")
        master_errors = list(
            new.execute(
                """WITH latest AS (
                       SELECT d.* FROM media_decisions d JOIN (
                         SELECT candidate_id,MAX(decision_id) decision_id
                           FROM media_decisions GROUP BY candidate_id
                       ) x ON x.decision_id=d.decision_id
                   )
                   SELECT o.record_key,
                          SUM(CASE WHEN l.disposition='selected_master' THEN 1 ELSE 0 END) selected_count
                     FROM objects o
                     LEFT JOIN media_candidates m ON m.record_key=o.record_key
                     LEFT JOIN latest l ON l.candidate_id=m.candidate_id
                    GROUP BY o.record_key HAVING selected_count<>1"""
            )
        )
        if master_errors:
            raise ReprojectionError(
                f"exactly-one-Master invariant failed for {len(master_errors)} objects"
            )
        selected_role_errors = new.execute(
            """WITH latest AS (
                   SELECT d.* FROM media_decisions d JOIN (
                     SELECT candidate_id,MAX(decision_id) decision_id
                       FROM media_decisions GROUP BY candidate_id
                   ) x ON x.decision_id=d.decision_id
               )
               SELECT COUNT(*) FROM latest l
                 JOIN media_candidates m ON m.candidate_id=l.candidate_id
                WHERE l.disposition='selected_master'
                  AND m.content_role<>'preservation_master'"""
        ).fetchone()[0]
        if selected_role_errors:
            raise ReprojectionError(
                f"{selected_role_errors} selected Masters do not match published roles"
            )
        md95019 = new.execute(
            """WITH latest AS (
                   SELECT d.* FROM media_decisions d JOIN (
                     SELECT candidate_id,MAX(decision_id) decision_id
                       FROM media_decisions GROUP BY candidate_id
                   ) x ON x.decision_id=d.decision_id
               )
               SELECT m.final_path,m.content_sha256
                 FROM media_candidates m JOIN latest l ON l.candidate_id=m.candidate_id
                WHERE m.record_key=? AND l.disposition='selected_master'""",
            (TARGET_RECORD,),
        ).fetchone()
        if md95019 != (JPEG_MASTER, JPEG_MASTER_HASH):
            raise ReprojectionError("reprojected md:95019 does not select the JPEG Master")
        correction = new.execute(
            """SELECT decision_id,disposition FROM object_census_decisions
               WHERE decision_id=(SELECT MAX(decision_id) FROM object_census_decisions WHERE candidate_id=?)""",
            (TARGET_CANDIDATE,),
        ).fetchone()
        if correction is None or correction[1] != "redundant":
            raise ReprojectionError("reprojected correction decision is absent")
        final_counts = {
            table: new.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("objects", "media_candidates", "metadata_fields", *copy_order)
        }
    fsync_file(CANDIDATE)
    return {
        "common_projection": common_report,
        "preserved_table_counts": old_counts,
        "final_table_counts": final_counts,
        "appended_media_role_decisions": 168,
        "corrected_provenance_rows": corrected_provenance_rows,
        "md95019_correction_decision_id": int(correction[0]),
    }


def apply() -> dict[str, Any]:
    if RECEIPT.exists():
        return json.loads(RECEIPT.read_text(encoding="utf-8"))
    if CANDIDATE.exists():
        raise ReprojectionError("reprojection candidate already exists")
    if BACKUP.exists():
        validate_database(BACKUP)
        if not databases_logically_equal(BACKUP, LIVE):
            raise ReprojectionError("existing boundary backup differs from the unchanged live database")
        backup_hash = sha256_file(BACKUP)
    else:
        backup_hash = ""
    scope = inspect_scope()
    source_fingerprint = build_fresh_projection()
    applied_at = now()
    if not backup_hash:
        backup_hash = backup_live()
    merged = merge_projection(source_fingerprint, applied_at)
    candidate_hash = sha256_file(CANDIDATE)
    os.replace(CANDIDATE, LIVE)
    fsync_dir(LIVE.parent)
    validate_database(LIVE)
    live_hash = sha256_file(LIVE)
    review = KurtSchwarzReview(UNIT, ROOT)
    if review.source_fingerprint != source_fingerprint:
        raise ReprojectionError("live workbench fingerprint validation drifted")
    result = {
        "format": "iss-kurt-schwarz-completed-census-reprojection-v1",
        "applied_at": applied_at,
        "result": "ok",
        "live_database": str(LIVE),
        "live_database_sha256": live_hash,
        "candidate_database_sha256_before_replace": candidate_hash,
        "backup_database": str(BACKUP),
        "backup_database_sha256": backup_hash,
        "current_source_fingerprint": source_fingerprint,
        "scope": scope,
        **merged,
        "exactly_one_selected_master_per_object": True,
        "quick_check": "ok",
        "foreign_key_violations": 0,
        "source_modified": False,
        "archive_unit_modified": False,
        "deletion_authorized": False,
    }
    payload = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    temporary = RECEIPT.with_name(f".{RECEIPT.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, RECEIPT)
    fsync_dir(RECEIPT.parent)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = apply() if args.apply else inspect_scope()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ReprojectionError, OSError, ValueError, csv.Error, sqlite3.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        CANDIDATE.unlink(missing_ok=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
