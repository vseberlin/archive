#!/usr/bin/env python3
"""Side-by-side simplified archive package exporter and validator."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import copyhash_runner
from archive_core.catalog import unit_catalog_csv_bytes, unit_catalog_rows, unit_catalog_xlsx_bytes
from paragon_human_catalog import build as build_human_catalog


FORMAT = "iss-archive-v3-export-v1"
TOOL_VERSION = "archive-v3-0.1.0"
IMAGE_ROLES = {"preservation_master", "preservation_surrogate"}
COPYHASH = TOOL_ROOT / "bin" / "archive-copyhash"


class ArchiveV3Error(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def copyhash_available() -> bool:
    return copyhash_runner.helper_available(COPYHASH)


def helper_copy(source: Path, destination: Path, expected_digest: str = "") -> str:
    try:
        return copyhash_runner.copy_file(
            COPYHASH,
            source,
            destination,
            expected_digest,
        )
    except copyhash_runner.CopyHashError as error:
        raise ArchiveV3Error(str(error)) from error


def safe_copy(source: Path, destination: Path, expected_digest: str = "") -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ArchiveV3Error(f"refusing to replace existing file: {destination}")
    if copyhash_available():
        return helper_copy(source, destination, expected_digest)
    shutil.copy2(source, destination)
    source_digest = "sha256:" + sha256_file(source)
    if expected_digest and source_digest != expected_digest:
        raise ArchiveV3Error(f"source digest differs from expected: {source}")
    if source_digest != "sha256:" + sha256_file(destination):
        raise ArchiveV3Error(f"copy digest mismatch: {source} -> {destination}")
    return source_digest


def ensure_empty_output(path: Path) -> None:
    if path.exists():
        raise ArchiveV3Error(f"output path already exists: {path}")
    path.mkdir(parents=True)


def filename_component(value: str, fallback: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip(".-")
    return normalized or fallback


def v3_access_filename(inventory: str, entity_id: str) -> str:
    return (
        f"{filename_component(inventory, 'unlabelled')}__"
        f"{filename_component(entity_id, 'unknown-entity')}__access.jpg"
    )


def copied_name(relative_path: str) -> str:
    return Path(relative_path).name


def active_representations(connection: sqlite3.Connection, unit: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT a.archival_label, a.descriptive_title, e.entity_id,
                  ef.role, fo.relative_path, fo.original_filename,
                  fo.content_digest, fo.observed_mtime_utc, cb.size_bytes,
                  cb.media_type, cb.width_px, cb.height_px
           FROM archive_objects a
           JOIN entities e ON e.entity_id = a.entity_id
           JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
           JOIN entity_files ef ON ef.entity_id = e.entity_id
           JOIN file_occurrences fo ON fo.occurrence_id = ef.occurrence_id
           JOIN content_blobs cb ON cb.content_digest = fo.content_digest
           WHERE m.unit_id = ?
             AND m.role = 'primary'
             AND e.status = 'active'
             AND a.status = 'active'
             AND ef.status = 'active'
             AND fo.status = 'verified'
           ORDER BY a.archival_label, e.entity_id, ef.role, fo.relative_path""",
        (unit,),
    ).fetchall()
    return [dict(row) for row in rows]


def active_objects(connection: sqlite3.Connection, unit: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT a.archival_label, a.descriptive_title, a.description,
                  a.publication_status, a.status AS object_status, e.entity_id
           FROM archive_objects a
           JOIN entities e ON e.entity_id = a.entity_id
           JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
           WHERE m.unit_id = ?
             AND m.role = 'primary'
             AND e.status = 'active'
             AND a.status = 'active'
           ORDER BY a.archival_label, e.entity_id""",
        (unit,),
    ).fetchall()
    return [dict(row) for row in rows]


def export_restore_db(connection: sqlite3.Connection, root: Path, unit: str, output: Path) -> dict[str, Any]:
    db_restore = output / "db-restore"
    db_restore.mkdir(parents=True)
    sqlite_path = db_restore / "RESTORE.sqlite"
    sql_path = db_restore / "RESTORE.sql"
    csv_path = db_restore / "RESTORE.csv"
    xlsx_path = db_restore / "RESTORE.xlsx"

    source_db = root / "work" / "catalog.sqlite"
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    restore = sqlite3.connect(sqlite_path)
    try:
        source.backup(restore)
        restore.execute("PRAGMA journal_mode=DELETE")
        restore.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        restore.commit()
    finally:
        source.close()
        restore.close()
    restore = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        sql_path.write_text("\n".join(restore.iterdump()) + "\n", encoding="utf-8")
    finally:
        restore.close()
    for sidecar in (
        sqlite_path.with_name(sqlite_path.name + "-wal"),
        sqlite_path.with_name(sqlite_path.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()

    rows = unit_catalog_rows(connection, unit)
    csv_path.write_bytes(unit_catalog_csv_bytes(rows))
    xlsx_path.write_bytes(unit_catalog_xlsx_bytes(rows))
    return {
        "sqlite": str(sqlite_path),
        "sql": str(sql_path),
        "csv": str(csv_path),
        "xlsx": str(xlsx_path),
        "rows": len(rows),
    }


def generate_access(master: Path, destination: Path) -> None:
    command = [
        "magick",
        str(master),
        "-auto-orient",
        "-resize",
        "2000x2000>",
        "-strip",
        "-quality",
        "85",
        str(destination),
    ]
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise ArchiveV3Error(
            f"access generation failed for {master}: {result.stdout.strip()}"
        )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise ArchiveV3Error(f"access generation produced no file: {destination}")


def image_dimensions(path: Path) -> tuple[str, str]:
    result = subprocess.run(
        ["magick", "identify", "-format", "%w %h", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return "", ""
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def export_files(root: Path, unit: str, output: Path, representations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_entity: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in representations:
        by_entity[str(row["entity_id"])][str(row["role"])].append(row)

    manifest: list[dict[str, Any]] = []
    masters_dir = output / "masters"
    access_dir = output / "export" / "access"
    for entity_id, roles in by_entity.items():
        image_rows = roles.get("preservation_master", []) + roles.get("preservation_surrogate", [])
        for image_row in image_rows:
            source = root / str(image_row["relative_path"])
            destination = masters_dir / copied_name(str(image_row["relative_path"]))
            source_digest = str(image_row["content_digest"])
            copied_digest = safe_copy(source, destination, source_digest)
            access_rows = roles.get("access_derivative", [])
            access_destination = access_dir / v3_access_filename(
                str(image_row["archival_label"]),
                entity_id,
            )
            derivation_status = "generated_from_master"
            access_source = ""
            access_digest = ""
            if access_rows:
                access_row = access_rows[0]
                access_source_path = root / str(access_row["relative_path"])
                access_digest = safe_copy(
                    access_source_path,
                    access_destination,
                    str(access_row["content_digest"]),
                )
                access_source = str(access_row["relative_path"])
                derivation_status = "copied_existing_access"
            else:
                generate_access(source, access_destination)
                access_digest = "sha256:" + sha256_file(access_destination)
            width, height = image_dimensions(access_destination)
            manifest.append(
                {
                    "iss_id": entity_id,
                    "inventory": image_row["archival_label"],
                    "source_role": image_row["role"],
                    "master_path": str(destination.relative_to(output)),
                    "master_sha256": copied_digest,
                    "access_path": str(access_destination.relative_to(output)),
                    "access_sha256": access_digest,
                    "access_source": access_source,
                    "derivation_status": derivation_status,
                    "target_system": "local_access",
                    "width_px": width,
                    "height_px": height,
                    "generated_by": TOOL_VERSION,
                    "generated_at": utc_now(),
                }
            )
    return manifest


def copy_documents(root: Path, unit: str, output: Path) -> list[dict[str, Any]]:
    source = root / "units" / unit / "data" / "documents"
    if not source.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    destination = output / "documents"
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        relative = path.relative_to(source)
        target = destination / relative
        digest = safe_copy(path, target)
        rows.append(
            {
                "path": str(target.relative_to(output)),
                "sha256": digest,
                "size_bytes": target.stat().st_size,
            }
        )
    return rows


def copy_md_metadata(output: Path) -> list[dict[str, Any]]:
    source = output / "documents"
    metadata = output / "export" / "md" / "metadata"
    images = output / "export" / "md" / "images"
    images.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for name in ("md-export.csv", "md-export.xlsx", "md-import-xml.zip"):
        path = source / name
        if not path.is_file():
            continue
        destination = metadata / name
        digest = safe_copy(path, destination)
        rows.append(
            {
                "path": str(destination.relative_to(output)),
                "sha256": digest,
                "size_bytes": destination.stat().st_size,
                "role": "museum_digital_metadata",
            }
        )
    return rows


def write_checksums(output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    checksums_path = output / "db-restore" / "checksums.csv"
    for path in sorted(p for p in output.rglob("*") if p.is_file() and p != checksums_path):
        rows.append(
            {
                "path": path.relative_to(output).as_posix(),
                "sha256": "sha256:" + sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    write_csv(checksums_path, rows, ["path", "sha256", "size_bytes"])
    rows.append(
        {
            "path": checksums_path.relative_to(output).as_posix(),
            "sha256": "sha256:" + sha256_file(checksums_path),
            "size_bytes": checksums_path.stat().st_size,
        }
    )
    return rows


def write_readme(output: Path, unit: str, summary: dict[str, Any]) -> None:
    (output / "README.md").write_text(
        f"# {unit}\n\n"
        "Simplified archive workflow v3 export.\n\n"
        "- `masters/`: preservation master/surrogate TIFFs.\n"
        "- `export/access/`: local access JPEGs, copied or generated from masters.\n"
        "- `documents/`: source and historical export evidence.\n"
        "- `db-restore/`: SQLite, SQL dump, full restore exports, checksums, manifests.\n"
        "- `CATALOG.xlsx`: human-readable object catalog.\n\n"
        f"Created: {summary['created_at']}\n"
        f"Catalog revision: {summary['catalog_state']['catalog_revision']}\n",
        encoding="utf-8",
    )


def export_unit(args: argparse.Namespace) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(Path(args.root).resolve(strict=True))
    catalog_core.require_core_mode(root)
    output = Path(args.output).resolve()
    ensure_empty_output(output)
    connection = catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    try:
        catalog_core.assert_catalog_integrity(connection)
        catalog_core.require_filesystem_current(connection)
        state = dict(catalog_core.catalog_state(connection))
        objects = active_objects(connection, args.unit)
        representations = active_representations(connection, args.unit)
        restore = export_restore_db(connection, root, args.unit, output)
    finally:
        connection.close()

    image_manifest = export_files(root, args.unit, output, representations)
    documents = copy_documents(root, args.unit, output)
    md_metadata = copy_md_metadata(output)
    manifests = output / "db-restore" / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    write_csv(
        manifests / "representations.csv",
        image_manifest,
        [
            "iss_id",
            "inventory",
            "source_role",
            "master_path",
            "master_sha256",
            "access_path",
            "access_sha256",
            "access_source",
            "derivation_status",
            "target_system",
            "width_px",
            "height_px",
            "generated_by",
            "generated_at",
        ],
    )
    write_csv(
        manifests / "documents.csv",
        documents,
        ["path", "sha256", "size_bytes"],
    )
    write_csv(
        manifests / "md-export.csv",
        md_metadata,
        ["path", "sha256", "size_bytes", "role"],
    )
    build_human_catalog(
        output / "db-restore" / "RESTORE.csv",
        output / "db-restore" / "RESTORE.xlsx",
        Path(args.workbook).resolve(strict=True),
        output,
    )
    for generated_restore_duplicate in (output / "RESTORE.csv", output / "RESTORE.xlsx"):
        if generated_restore_duplicate.exists():
            generated_restore_duplicate.unlink()
    checksums = write_checksums(output)
    summary = {
        "format": FORMAT,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "unit": args.unit,
        "source_root": str(root),
        "output": str(output),
        "catalog_state": state,
        "objects": len(objects),
        "masters": len(image_manifest),
        "access": len(image_manifest),
        "generated_access": sum(
            1 for row in image_manifest if row["derivation_status"] == "generated_from_master"
        ),
        "copied_access": sum(
            1 for row in image_manifest if row["derivation_status"] == "copied_existing_access"
        ),
        "documents": len(documents),
        "md_metadata": len(md_metadata),
        "restore": restore,
        "checksums": len(checksums),
    }
    write_readme(output, args.unit, summary)
    (output / "db-restore" / "export-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def validate_unit(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.path).resolve(strict=True)
    required = [
        root / "masters",
        root / "export" / "access",
        root / "documents",
        root / "db-restore",
        root / "CATALOG.xlsx",
        root / "db-restore" / "RESTORE.sqlite",
        root / "db-restore" / "RESTORE.sql",
        root / "db-restore" / "RESTORE.csv",
        root / "db-restore" / "RESTORE.xlsx",
        root / "db-restore" / "checksums.csv",
        root / "db-restore" / "manifests" / "representations.csv",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ArchiveV3Error("missing v3 paths: " + ", ".join(missing))
    with (root / "db-restore" / "checksums.csv").open(encoding="utf-8", newline="") as handle:
        checksum_rows = list(csv.DictReader(handle))
    mismatches: list[str] = []
    for row in checksum_rows:
        path = root / str(row["path"])
        if not path.is_file():
            mismatches.append(f"missing file: {row['path']}")
            continue
        if "sha256:" + sha256_file(path) != row["sha256"]:
            mismatches.append(f"digest mismatch: {row['path']}")
    with (root / "db-restore" / "manifests" / "representations.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        reps = list(csv.DictReader(handle))
    missing_access = [
        row["inventory"]
        for row in reps
        if not (root / row["access_path"]).is_file()
    ]
    if mismatches or missing_access:
        raise ArchiveV3Error(
            json.dumps(
                {"checksum_errors": mismatches, "missing_access": missing_access},
                ensure_ascii=False,
                indent=2,
            )
        )
    return {
        "result": "ok",
        "path": str(root),
        "representations": len(reps),
        "checksums": len(checksum_rows),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-unit")
    export.add_argument("--root", required=True)
    export.add_argument("--unit", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--workbook", required=True)
    export.set_defaults(handler=export_unit)
    validate = sub.add_parser("validate-unit")
    validate.add_argument("--path", required=True)
    validate.set_defaults(handler=validate_unit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
