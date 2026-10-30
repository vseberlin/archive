#!/usr/bin/env python3
"""Execute one reviewed WF-PARAGON one-master/one-derivative cleanup plan."""

from __future__ import annotations

import argparse
import csv
import fcntl
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

import catalog_core
import tiff_accession
from archive_core.catalog import write_unit_catalog


class CleanupExecutionError(RuntimeError):
    """Raised when the reviewed cleanup plan cannot execute safely."""


def digest(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def verify_checksums(plan_dir: Path) -> None:
    for line in (plan_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        actual = digest(plan_dir / name).removeprefix("sha256:")
        if actual != expected:
            raise CleanupExecutionError(f"plan checksum failed: {name}")


def backup_catalog(catalog_path: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(catalog_path)
    target = sqlite3.connect(destination)
    try:
        source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(destination)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CleanupExecutionError("catalog backup failed integrity check")
    finally:
        check.close()
    return {
        "path": str(destination),
        "sha256": digest(destination),
        "size_bytes": destination.stat().st_size,
    }


def atomic_replace_verified(source: Path, destination: Path, expected: str) -> None:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if digest(temporary) != expected:
            raise CleanupExecutionError(f"staged replacement failed fixity: {destination}")
        os.replace(temporary, destination)
        tiff_accession._mark_read_only(destination)
    finally:
        temporary.unlink(missing_ok=True)


def assert_catalog_state(connection: sqlite3.Connection, summary: dict) -> None:
    state = connection.execute(
        "SELECT catalog_revision, published_revision, dirty_since FROM catalog_state"
    ).fetchone()
    expected = summary["catalog_state"]
    if (
        int(state["catalog_revision"]) != int(expected["catalog_revision"])
        or int(state["published_revision"]) != int(expected["published_revision"])
        or state["dirty_since"] is not None
    ):
        raise CleanupExecutionError("catalog changed after the reviewed plan")
    generation = connection.execute(
        "SELECT generation_id, manifest_digest FROM filesystem_generations "
        "WHERE status='current'"
    ).fetchone()
    if (
        not generation
        or generation["generation_id"] != expected["generation_id"]
        or generation["manifest_digest"] != expected["generation_manifest_digest"]
    ):
        raise CleanupExecutionError("current generation changed after the reviewed plan")


def migrate_catalog(
    connection: sqlite3.Connection,
    rows: list[dict[str, str]],
    *,
    unit_id: str,
    actor: str,
    plan_digest: str,
) -> dict:
    new_occurrences: dict[str, str] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            if row["phase"] != "migration":
                continue
            if row["operation"] == "supersede_active_link":
                result = connection.execute(
                    """
                    UPDATE entity_files SET status='superseded'
                    WHERE entity_id=? AND occurrence_id=? AND role=?
                      AND status='active'
                    """,
                    (
                        row["entity_id"],
                        row["old_occurrence_id"],
                        row["old_role"],
                    ),
                )
                if result.rowcount != 1:
                    raise CleanupExecutionError(
                        f"active link changed: {row['old_occurrence_id']}"
                    )
                continue
            if row["operation"] != "register_verified_access_derivative":
                raise CleanupExecutionError(
                    f"unexpected migration operation: {row['operation']}"
                )
            old = connection.execute(
                """
                SELECT fo.content_digest, fo.observed_mtime_utc
                FROM file_occurrences fo
                JOIN entity_files ef ON ef.occurrence_id=fo.occurrence_id
                WHERE fo.occurrence_id=? AND fo.unit_id=?
                  AND ef.entity_id=? AND ef.role='preservation_surrogate_source'
                  AND ef.status='superseded' AND fo.status='verified'
                """,
                (row["old_occurrence_id"], unit_id, row["entity_id"]),
            ).fetchone()
            if not old or old["content_digest"] != row["sha256"]:
                raise CleanupExecutionError(
                    f"surrogate source changed: {row['old_occurrence_id']}"
                )
            occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
            relative = row["new_relative_path"]
            connection.execute(
                """
                INSERT INTO file_occurrences (
                    occurrence_id, unit_id, batch_id, content_digest,
                    occurrence_kind, source_root_id, relative_path,
                    original_filename, observed_mtime_utc, observed_at_utc,
                    status, note
                ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                          'verified', ?)
                """,
                (
                    occurrence_id,
                    unit_id,
                    row["sha256"],
                    relative,
                    Path(relative).name,
                    old["observed_mtime_utc"],
                    catalog_core.utc_now(),
                    "exact selected JPEG retained as the sole access derivative; "
                    "source-of-surrogate provenance remains in the logbook",
                ),
            )
            connection.execute(
                """
                INSERT INTO entity_files (
                    entity_id, occurrence_id, role, status, created_at
                ) VALUES (?, ?, 'access_derivative', 'active', ?)
                """,
                (row["entity_id"], occurrence_id, catalog_core.utc_now()),
            )
            catalog_core._append_log(
                connection,
                actor,
                "normalize_image_representation_contract",
                "entity_file",
                row["entity_id"],
                unit_id,
                {
                    "new_occurrence_id": occurrence_id,
                    "new_relative_path": relative,
                    "new_role": "access_derivative",
                    "old_occurrence_id": row["old_occurrence_id"],
                    "old_role": "preservation_surrogate_source",
                    "plan_digest": plan_digest,
                },
                evidence=row["sha256"],
                note="One authoritative TIFF representation and one JPEG access "
                "derivative; no source evidence root changed.",
            )
            new_occurrences[row["entity_id"]] = occurrence_id
        counts = {
            row["role"]: int(row["n"])
            for row in connection.execute(
                """
                SELECT ef.role, count(*) AS n
                FROM entity_files ef
                JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
                WHERE fo.unit_id=? AND ef.status='active'
                GROUP BY ef.role
                """,
                (unit_id,),
            )
        }
        expected = {
            "access_derivative": 213,
            "preservation_master": 19,
            "preservation_surrogate": 194,
        }
        if counts != expected:
            raise CleanupExecutionError(f"post-migration roles differ: {counts}")
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.commit()
        return {"catalog_revision": revision, "new_occurrences": new_occurrences}
    except Exception:
        connection.rollback()
        raise


def finalize_catalog(
    connection: sqlite3.Connection,
    rows: list[dict[str, str]],
    *,
    actor: str,
    unit_id: str,
    plan_digest: str,
) -> int:
    try:
        connection.execute("BEGIN IMMEDIATE")
        count = 0
        for row in rows:
            if row["phase"] != "post_retirement":
                continue
            result = connection.execute(
                """
                UPDATE file_occurrences
                SET status='superseded',
                    note=CASE
                        WHEN note='' THEN ?
                        ELSE note || '; ' || ?
                    END
                WHERE occurrence_id=? AND status='verified'
                  AND content_digest=?
                """,
                (
                    "physical path retired after verified role normalization",
                    "physical path retired after verified role normalization",
                    row["old_occurrence_id"],
                    row["sha256"],
                ),
            )
            if result.rowcount != 1:
                raise CleanupExecutionError(
                    f"retired occurrence changed: {row['old_occurrence_id']}"
                )
            count += 1
        catalog_core._append_log(
            connection,
            actor,
            "finalize_image_layout_cleanup",
            "unit",
            unit_id,
            unit_id,
            {
                "file_occurrences_superseded": count,
                "plan_digest": plan_digest,
                "target": {
                    "masters": 213,
                    "derivatives": 213,
                    "documents": 0,
                },
            },
            evidence=plan_digest,
            note="Exact reviewed retirement paths removed on primary and "
            "independent storage after replacement fixity.",
        )
        revision = int(catalog_core.catalog_state(connection)["catalog_revision"])
        connection.commit()
        return revision
    except Exception:
        connection.rollback()
        raise


def write_human_catalogs(root: Path, independent: Path, unit_id: str) -> dict:
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as catalog:
        primary = write_unit_catalog(catalog, unit_id, root / "units" / unit_id)
        copy = write_unit_catalog(catalog, unit_id, independent / "units" / unit_id)
    if (
        primary["csv_sha256"] != copy["csv_sha256"]
        or primary["xlsx_sha256"] != copy["xlsx_sha256"]
    ):
        raise CleanupExecutionError("independent human-readable catalog differs")
    return {"primary": primary, "independent": copy}


def verify_active_contract(
    root: Path, independent: Path, unit_id: str
) -> dict:
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as connection:
        rows = connection.execute(
            """
            SELECT ef.role, fo.relative_path, fo.content_digest
            FROM entity_files ef
            JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
            WHERE fo.unit_id=? AND ef.status='active' AND fo.status='verified'
              AND ef.role IN (
                  'preservation_master',
                  'preservation_surrogate',
                  'preservation_surrogate_source',
                  'access_derivative'
              )
            ORDER BY ef.entity_id, ef.role
            """,
            (unit_id,),
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["role"]] = counts.get(row["role"], 0) + 1
        for base in (root, independent):
            path = base / row["relative_path"]
            if not path.is_file() or digest(path) != row["content_digest"]:
                raise CleanupExecutionError(f"active representation failed: {path}")
    expected = {
        "access_derivative": 213,
        "preservation_master": 19,
        "preservation_surrogate": 194,
    }
    if counts != expected:
        raise CleanupExecutionError(f"active representation counts differ: {counts}")
    physical = {}
    for base in (root, independent):
        data = base / "units" / unit_id / "data"
        values = {}
        for name in ("masters", "derivatives", "documents", "objects", "surrogates"):
            directory = data / name
            values[name] = (
                sum(1 for path in directory.rglob("*") if path.is_file())
                if directory.exists()
                else 0
            )
        physical[str(base)] = values
    return {"active_roles": counts, "physical_files": physical}


def remove_empty_legacy_directories(root: Path, unit_id: str) -> list[str]:
    data = root / "units" / unit_id / "data"
    removed: list[str] = []
    candidates: list[Path] = []
    for name in ("masters", "derivatives", "objects", "surrogates"):
        directory = data / name
        if not directory.exists():
            continue
        candidates.extend(
            sorted(
                (path for path in directory.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
        )
    candidates.extend([data / "objects", data / "surrogates"])
    for directory in candidates:
        if directory in {data / "masters", data / "derivatives"}:
            continue
        if directory.is_dir():
            directory.rmdir()
            removed.append(str(directory))
    return removed


def execute(args: argparse.Namespace) -> dict:
    plan_dir = args.plan_dir.resolve()
    if (plan_dir / "DO_NOT_EXECUTE.md").exists():
        raise CleanupExecutionError("plan is explicitly marked non-executable")
    verify_checksums(plan_dir)
    summary = json.loads((plan_dir / "summary.json").read_text(encoding="utf-8"))
    if summary["plan_digest"] != args.plan_digest:
        raise CleanupExecutionError("typed plan digest differs")
    if summary["writes_authorized"] or summary["deletion_authorized"]:
        raise CleanupExecutionError("review plan authorization fields changed")
    copy_rows = read_csv(plan_dir / "copy-plan.csv")
    catalog_rows = read_csv(plan_dir / "catalog-plan.csv")
    retirement_rows = read_csv(plan_dir / "retirement-plan.csv")
    if not (
        len(copy_rows) == 388
        and len(catalog_rows) == 606
        and len(retirement_rows) == 432
    ):
        raise CleanupExecutionError("review plan row counts differ")
    root, independent = [Path(value) for value in summary["storage_roots"]]
    unit_id = summary["unit_id"]
    execution_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    execution_root = plan_dir.parent / f"execution-{execution_id}"
    execution_root.mkdir(parents=True, exist_ok=False)
    lock_path = root / "work" / ".recovery-accession.lock"
    report: dict = {
        "schema": "iss-recovery-layout-cleanup-execution-v1",
        "started_at_utc": catalog_core.utc_now(),
        "plan_dir": str(plan_dir),
        "plan_digest": args.plan_digest,
        "actor": args.actor,
        "state": "started",
    }
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            with closing(
                catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
            ) as connection:
                assert_catalog_state(connection, summary)
            report["catalog_backup"] = backup_catalog(
                root / "work" / "catalog.sqlite",
                execution_root / "backup" / "catalog.sqlite",
            )
            rollback = execution_root / "rollback"
            copied = 0
            replaced = 0
            for row in copy_rows:
                source = Path(row["source_path"])
                destination = Path(row["destination_path"])
                if digest(source) != row["sha256"]:
                    raise CleanupExecutionError(f"copy source changed: {source}")
                if row["destination_collision"] == "true":
                    backup = rollback / row["storage_root"].strip("/").replace("/", "_") / destination.name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    tiff_accession._atomic_copy_verified(
                        destination,
                        backup,
                        row["collision_sha256"],
                    )
                    atomic_replace_verified(source, destination, row["sha256"])
                    replaced += 1
                else:
                    tiff_accession._atomic_copy_verified(
                        source, destination, row["sha256"]
                    )
                    tiff_accession._mark_read_only(destination)
                    copied += 1
            report["physical_migration"] = {
                "copied": copied,
                "replaced_with_rollback": replaced,
            }
            with closing(
                catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
            ) as connection:
                migration = migrate_catalog(
                    connection,
                    catalog_rows,
                    unit_id=unit_id,
                    actor=args.actor,
                    plan_digest=args.plan_digest,
                )
            report["catalog_migration"] = migration
            report["pre_retirement_verification"] = verify_active_contract(
                root, independent, unit_id
            )
            report["migration_publication"] = catalog_core.publish_generation(
                root,
                independent,
                tool_version="recovery-layout-cleanup-1.0",
                actor=args.actor,
            )
            report["migration_unit_catalog"] = write_human_catalogs(
                root, independent, unit_id
            )
            removed_files = 0
            for row in retirement_rows:
                path = Path(row["path"])
                if digest(path) != row["sha256"]:
                    raise CleanupExecutionError(f"retirement path changed: {path}")
                path.unlink()
                removed_files += 1
            removed_directories = []
            for base in (root, independent):
                removed_directories.extend(
                    remove_empty_legacy_directories(base, unit_id)
                )
            report["physical_retirement"] = {
                "files_removed": removed_files,
                "directories_removed": removed_directories,
            }
            with closing(
                catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
            ) as connection:
                report["final_catalog_revision"] = finalize_catalog(
                    connection,
                    catalog_rows,
                    actor=args.actor,
                    unit_id=unit_id,
                    plan_digest=args.plan_digest,
                )
            report["final_publication"] = catalog_core.publish_generation(
                root,
                independent,
                tool_version="recovery-layout-cleanup-1.0",
                actor=args.actor,
            )
            report["final_unit_catalog"] = write_human_catalogs(
                root, independent, unit_id
            )
            report["final_verification"] = verify_active_contract(
                root, independent, unit_id
            )
            report["state"] = "complete"
            report["completed_at_utc"] = catalog_core.utc_now()
        except Exception as error:
            report["state"] = "failed"
            report["error"] = str(error)
            report["failed_at_utc"] = catalog_core.utc_now()
            raise
        finally:
            (execution_root / "execution-report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required")
    return args


def main(argv: list[str] | None = None) -> int:
    report = execute(parse_args(argv if argv is not None else sys.argv[1:]))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CleanupExecutionError, catalog_core.CoreCatalogError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
