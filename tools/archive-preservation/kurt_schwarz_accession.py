#!/usr/bin/env python3
"""Resumable exact-byte accession of the reconciled Kurt Schwarz project."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
import copyhash_runner  # noqa: E402
import paragon_document_bundle  # noqa: E402


FORMAT = "iss-kurt-schwarz-accession-v1"
PUBLICATION_FORMAT = "iss-kurt-schwarz-publication-v1"
RUN_ID = "kurt-schwarz-full-001"
UNIT_ID = "kurt-schwarz"
WORK_BASE = Path("/mnt/volume/iss-archive-work")
FINAL_BASE = Path("/mnt/volume/iss-archive")
SOURCE_MEDIA = Path("/media/vladimir/Elements")
UNIT_WORK = WORK_BASE / UNIT_ID
RUN_ROOT = UNIT_WORK / "work/accession-runs" / RUN_ID
PACKAGE = RUN_ROOT / "package"
FINAL_UNIT = FINAL_BASE / "units" / UNIT_ID
PLAN_ROOT = UNIT_WORK / (
    "work/accession-plan/runs/20260730T110000Z-kurt-schwarz-accession-plan"
)
DISCOVERY_ROOT = UNIT_WORK / (
    "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
)
SUBDIVISION_ROOT = UNIT_WORK / (
    "work/reconciliation/subdivisions/"
    "20260730T002000Z-kurt-schwarz-subdivisions"
)
SHARED_WORKBOOK_ROOT = UNIT_WORK / (
    "work/reconciliation/shared-workbooks/"
    "20260730T093000Z-google-drive-kurt-schwarz-workbooks"
)
DEEP_REVIEW_ROOT = UNIT_WORK / (
    "work/reconciliation/deep-review/"
    "20260730T100000Z-kurt-schwarz-deep-review-with-metadata"
)
SHARED_CONTAINER_ROOT = UNIT_WORK / (
    "work/reconciliation/shared-containers/"
    "20260730T091000Z-google-drive-kurt-schwarz"
)
COPYHASH = RUN_ROOT / "tools/archive-copyhash"

CHECKSUM_FIELDS = ("path", "sha256", "size_bytes", "content_role")
DRAIN_FIELDS = (
    "source_path",
    "source_sha256",
    "source_size_bytes",
    "classification",
    "preservation_status",
    "final_path",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
)
ERROR_FIELDS = (
    "representation_key",
    "source_kind",
    "source_path",
    "source_member",
    "final_relative_path",
    "error_type",
    "error",
    "logged_at",
    "deletion_authorized",
)


class AccessionError(RuntimeError):
    """A fatal safety or publication error."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(
    rows: list[dict[str, Any]], fields: Iterable[str]
) -> bytes:
    fields = tuple(fields)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def xlsx_bytes(
    rows: list[dict[str, Any]],
    fields: Iterable[str],
    *,
    sheet: str,
    table: str,
) -> bytes:
    fields = tuple(fields)
    return paragon_document_bundle.table_xlsx_bytes(
        fields,
        [{field: str(row.get(field, "")) for field in fields} for row in rows],
        sheet_name=sheet,
        table_name=table,
    )


def require_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise AccessionError(f"path escaped allowed root: {resolved}") from error
    return resolved


def mkdir(path: Path) -> None:
    require_inside(path, WORK_BASE if str(path).startswith(str(WORK_BASE)) else FINAL_BASE)
    path.mkdir(parents=True, exist_ok=True)


def fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, payload: bytes, *, replace: bool = False) -> None:
    require_inside(path, PACKAGE if str(path).startswith(str(PACKAGE)) else RUN_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise AccessionError(f"unexpected temporary file exists: {temporary}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists() and not replace:
        temporary.unlink()
        raise AccessionError(f"refusing to overwrite existing file: {path}")
    os.replace(temporary, path)
    fsync_parent(path)


def write_json(path: Path, payload: Any, *, replace: bool = False) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        replace=replace,
    )


def append_jsonl(path: Path, payload: Any) -> None:
    require_inside(path, RUN_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def command(arguments: list[str], *, timeout: int = 900) -> str:
    try:
        result = subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = getattr(error, "stderr", "") or getattr(error, "stdout", "")
        raise AccessionError(
            f"{arguments[0]} failed: {str(detail).strip() or error}"
        ) from error
    return result.stdout.strip()


def mount_line(path: Path) -> str:
    return command(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(path)]
    )


def source_is_read_only() -> tuple[bool, str]:
    line = mount_line(SOURCE_MEDIA)
    fields = line.split(None, 3)
    return (
        len(fields) == 4 and "ro" in set(fields[3].split(",")),
        line,
    )


def ensure_boundaries(*, allow_published: bool = False) -> dict[str, str]:
    read_only, source_mount = source_is_read_only()
    if not read_only:
        raise AccessionError(f"source media is not read-only: {source_mount}")
    if WORK_BASE.stat().st_dev != FINAL_BASE.stat().st_dev:
        raise AccessionError("work and final archive roots are not on one filesystem")
    if FINAL_UNIT.exists() and not allow_published:
        raise AccessionError(f"final unit already exists: {FINAL_UNIT}")
    return {
        "source_mount": source_mount,
        "work_mount": mount_line(WORK_BASE),
        "final_mount": mount_line(FINAL_BASE),
    }


def compile_copyhash() -> None:
    if COPYHASH.is_file() and os.access(COPYHASH, os.X_OK):
        return
    source = TOOL_ROOT / "copyhash.c"
    if not source.is_file():
        raise AccessionError(f"copy/hash source is absent: {source}")
    tool_dir = COPYHASH.parent
    mkdir(tool_dir)
    temporary = tool_dir / f".archive-copyhash.tmp-{os.getpid()}"
    if temporary.exists() or COPYHASH.exists():
        raise AccessionError("unexpected copy/hash helper path exists")
    command(
        [
            "cc",
            "-O2",
            "-std=c11",
            "-Wall",
            "-Wextra",
            str(source),
            "-lcrypto",
            "-o",
            str(temporary),
        ]
    )
    temporary.chmod(0o755)
    os.rename(temporary, COPYHASH)
    fsync_parent(COPYHASH)


def plan_digest() -> str:
    digest = hashlib.sha256()
    for name in ("objects.csv", "representations.csv", "source-disposition.csv"):
        path = PLAN_ROOT / name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def load_inputs() -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    summary = json.loads((PLAN_ROOT / "summary.json").read_text(encoding="utf-8"))
    if summary.get("object_rows") != 1669:
        raise AccessionError("object-plan count drifted")
    if summary.get("deletion_authorized_rows") != 0:
        raise AccessionError("source plan authorizes deletion")
    objects = read_csv(PLAN_ROOT / "objects.csv")
    representations = read_csv(PLAN_ROOT / "representations.csv")
    dispositions = read_csv(PLAN_ROOT / "source-disposition.csv")
    if len(objects) != 1669:
        raise AccessionError("object count drifted")
    if len(representations) != summary.get("representation_rows"):
        raise AccessionError("representation count drifted")
    if len(dispositions) != summary.get("source_disposition_rows"):
        raise AccessionError("source-disposition count drifted")
    if any(row["deletion_authorized"] != "false" for row in dispositions):
        raise AccessionError("source disposition authorizes deletion")
    final_paths = [row["final_relative_path"] for row in representations]
    if len(final_paths) != len(set(final_paths)):
        raise AccessionError("source plan contains duplicate final paths")
    return objects, representations, dispositions


def source_for(row: dict[str, str]) -> Path:
    kind = row["source_kind"]
    if kind in {"file", "work_file"}:
        return Path(row["source_path"]).resolve(strict=True)
    if kind == "zip_member":
        parts = row["source_member"].split("/")
        if len(parts) < 2:
            raise AccessionError(f"invalid ZIP member path: {row['source_member']}")
        relative = Path(*parts[1:])
        source = require_inside(
            SHARED_CONTAINER_ROOT / "documents" / relative,
            SHARED_CONTAINER_ROOT / "documents",
        )
        return source.resolve(strict=True)
    raise AccessionError(f"unsupported source kind: {kind}")


def receipt_path(index: int) -> Path:
    return RUN_ROOT / "status/representations" / f"{index:05d}.json"


def validate_receipt(root: Path, receipt: dict[str, Any]) -> None:
    target = require_inside(root / receipt["final_relative_path"], root)
    if not target.is_file() or target.is_symlink():
        raise AccessionError(f"completed target is absent or unsafe: {target}")
    stat = target.stat()
    if (
        stat.st_size != int(receipt["size_bytes"])
        or stat.st_mtime_ns != int(receipt["mtime_ns"])
    ):
        raise AccessionError(f"completed target observation changed: {target}")


def copy_representation(
    row: dict[str, str], index: int
) -> dict[str, Any]:
    saved = receipt_path(index)
    if saved.is_file():
        receipt = json.loads(saved.read_text(encoding="utf-8"))
        validate_receipt(PACKAGE, receipt)
        return receipt
    source = source_for(row)
    if str(source).startswith(str(SOURCE_MEDIA) + "/"):
        try:
            read_only_flag = os.statvfs(source).f_flag & os.ST_RDONLY
        except OSError:
            raise
        if not read_only_flag:
            raise AccessionError(f"source filesystem is no longer read-only: {source}")
    if source.is_symlink() or not source.is_file():
        raise OSError(f"source is not one regular file: {source}")
    stat = source.stat()
    if stat.st_size != int(row["source_size_bytes"]):
        raise OSError(
            f"source size changed: {stat.st_size} != {row['source_size_bytes']}"
        )
    target = require_inside(PACKAGE / row["final_relative_path"], PACKAGE)
    if target.exists():
        raise AccessionError(f"unreceipted destination exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest = copyhash_runner.copy_file(
            COPYHASH,
            source,
            target,
            row["source_sha256"],
            timeout=3600,
        )
    except copyhash_runner.CopyHashError as error:
        if "destination already exists" in str(error).casefold():
            raise AccessionError(
                f"destination collision during guarded copy: {target}"
            ) from error
        raise
    target_stat = target.stat()
    receipt = {
        "index": index,
        "completed_at": now(),
        "representation_key": row["representation_key"],
        "record_keys": row["record_keys"],
        "inventories": row["inventories"],
        "role": row["role"],
        "source_kind": row["source_kind"],
        "source_path": row["source_path"],
        "source_member": row["source_member"],
        "source_sha256": digest,
        "final_sha256": digest,
        "final_relative_path": row["final_relative_path"],
        "size_bytes": target_stat.st_size,
        "mtime_ns": target_stat.st_mtime_ns,
        "copy_policy": "exact_source_bytes",
        "basis": row["basis"],
        "relationship": row["relationship"],
        "access_restriction": row["access_restriction"],
    }
    write_json(saved, receipt)
    return receipt


def load_errors() -> dict[str, dict[str, Any]]:
    path = RUN_ROOT / "errors.jsonl"
    if not path.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                result[row["representation_key"]] = row
    return result


def process_representations(
    representations: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    receipts: list[dict[str, Any]] = []
    errors = load_errors()
    for index, row in enumerate(representations, 1):
        if row["representation_key"] in errors:
            continue
        try:
            receipt = copy_representation(row, index)
        except AccessionError:
            raise
        except (OSError, ValueError, copyhash_runner.CopyHashError) as error:
            logged = {
                "representation_key": row["representation_key"],
                "source_kind": row["source_kind"],
                "source_path": row["source_path"],
                "source_member": row["source_member"],
                "final_relative_path": row["final_relative_path"],
                "error_type": type(error).__name__,
                "error": str(error),
                "logged_at": now(),
                "deletion_authorized": "false",
            }
            append_jsonl(RUN_ROOT / "errors.jsonl", logged)
            errors[row["representation_key"]] = logged
            continue
        receipts.append(receipt)
        if index % 100 == 0:
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(representations)}",
                        "completed": len(receipts),
                        "non_destructive_errors": len(errors),
                    }
                ),
                flush=True,
            )
    receipt_keys = {row["representation_key"] for row in receipts}
    for index, row in enumerate(representations, 1):
        if row["representation_key"] in receipt_keys or row["representation_key"] in errors:
            continue
        saved = receipt_path(index)
        if saved.is_file():
            receipt = json.loads(saved.read_text(encoding="utf-8"))
            validate_receipt(PACKAGE, receipt)
            receipts.append(receipt)
    receipts.sort(key=lambda row: row["final_relative_path"])
    return receipts, list(errors.values())


def safe_component(value: str) -> str:
    result = re.sub(r"[\x00-\x1f/\\]+", "_", value).strip(" .")
    return result or "unidentified"


def object_directories(objects: list[dict[str, str]]) -> dict[str, str]:
    proposed: dict[str, list[str]] = defaultdict(list)
    by_key = {row["record_key"]: row for row in objects}
    for row in objects:
        label = row["current_inventory"] or row["historical_inventory"] or row["record_key"]
        proposed[safe_component(label)].append(row["record_key"])
    result: dict[str, str] = {}
    for label, keys in proposed.items():
        if len(keys) == 1:
            result[keys[0]] = label
            continue
        for key in sorted(keys):
            suffix = hashlib.sha256(key.encode()).hexdigest()[:8]
            result[key] = f"{label}--{suffix}"
    if set(result) != set(by_key):
        raise AccessionError("object directory mapping is incomplete")
    return result


def representation_links(
    receipts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        link = {
            "path": receipt["final_relative_path"],
            "role": receipt["role"],
            "sha256": receipt["final_sha256"],
            "size_bytes": receipt["size_bytes"],
            "basis": receipt["basis"],
            "relationship": receipt["relationship"],
        }
        for key in receipt["record_keys"].split("; "):
            if key:
                result[key].append(link)
    return result


def build_object_metadata(
    objects: list[dict[str, str]],
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    directories = object_directories(objects)
    links = representation_links(receipts)
    membership_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for membership in read_csv(
        SUBDIVISION_ROOT / "subdivision-memberships.csv"
    ):
        membership_index[membership["record_key"]].append(membership)
    workbook_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for workbook_row in read_csv(
        SHARED_WORKBOOK_ROOT / "shared-workbook-records.csv"
    ):
        for record_key in workbook_row["mapped_record_keys"].split("; "):
            if record_key:
                workbook_index[record_key].append(workbook_row)
    catalog: list[dict[str, str]] = []
    checksums: list[dict[str, Any]] = []
    generated_at = now()
    for row in objects:
        key = row["record_key"]
        relative = f"masters/objects/{directories[key]}/meta/metadata.json"
        path = PACKAGE / relative
        payload = {
            "format": "iss-kurt-schwarz-object-metadata-v1",
            "unit": UNIT_ID,
            "generated_at": generated_at,
            "record": row,
            "representations": sorted(links.get(key, []), key=lambda value: value["path"]),
            "subdivision_memberships": membership_index.get(key, []),
            "shared_workbook_records": workbook_index.get(key, []),
            "metadata_policy": (
                "Current and historical museum-digital fields remain separate; "
                "USB, workbook, website, and Lightroom evidence is provenance "
                "or enrichment and does not silently replace object identity."
            ),
            "deletion_authorized": False,
        }
        data = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode()
        atomic_write(path, data, replace=True)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        checksums.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": len(data),
                "content_role": "object_metadata",
            }
        )
        item = dict(row)
        item.update(
            {
                "unit": UNIT_ID,
                "object_directory": directories[key],
                "object_metadata_path": relative,
                "representation_count": str(len(links.get(key, []))),
            }
        )
        catalog.append(item)
    return catalog, checksums


def final_dispositions(
    planned: list[dict[str, str]],
    receipts: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> list[dict[str, str]]:
    receipt_by_final = {row["final_relative_path"]: row for row in receipts}
    error_by_final = {row["final_relative_path"]: row for row in errors}
    result: list[dict[str, str]] = []
    for source in planned:
        row = dict(source)
        final = row["final_path"]
        receipt = receipt_by_final.get(final)
        error = error_by_final.get(final)
        if receipt:
            row.update(
                {
                    "preservation_status": "preserved_exact_copy",
                    "final_sha256": receipt["final_sha256"],
                    "exact_copy_proven": "true",
                    "drain_state": (
                        "candidate_after_separate_review"
                        if row["source_kind"] == "file"
                        and row["source_path"].startswith(str(SOURCE_MEDIA) + "/")
                        else "retain_shared_or_work_source"
                    ),
                    "notes": (
                        "Exact source bytes preserved and checksum verified; "
                        "no deletion is authorized."
                    ),
                }
            )
        elif error:
            row.update(
                {
                    "preservation_status": "source_error_logged",
                    "final_sha256": "",
                    "exact_copy_proven": "false",
                    "drain_state": "retain_source_copy_error",
                    "notes": (
                        "Non-destructive copy or validation error logged; source "
                        "must remain untouched."
                    ),
                }
            )
        row["deletion_authorized"] = "false"
        result.append(row)
    return result


def build_drain_list(
    dispositions: list[dict[str, str]],
) -> list[dict[str, str]]:
    exclusive = {
        row["source_path"]
        for row in read_csv(DISCOVERY_ROOT / "usb-exclusive-candidates.csv")
    }
    disposition_by_path = {row["source_path"]: row for row in dispositions}
    if not exclusive.issubset(disposition_by_path):
        missing = sorted(exclusive - set(disposition_by_path))
        raise AccessionError(f"exclusive drain scope missing rows: {missing[:3]}")
    result: list[dict[str, str]] = []
    for path in sorted(exclusive):
        source = disposition_by_path[path]
        if source["preservation_status"] == "preserved_exact_copy":
            state = "candidate_after_separate_review"
        elif source["preservation_status"] == "reviewed_noise_not_accessioned":
            state = "reviewed_noise_pending_explicit_drain_decision"
        else:
            state = "retain_not_ready"
        result.append(
            {
                "source_path": path,
                "source_sha256": source["source_sha256"],
                "source_size_bytes": source["source_size_bytes"],
                "classification": source["source_role"],
                "preservation_status": source["preservation_status"],
                "final_path": source["final_path"],
                "exact_copy_proven": source["exact_copy_proven"],
                "drain_state": state,
                "deletion_authorized": "false",
                "notes": source["notes"],
            }
        )
    return result


def sqlite_table(
    connection: sqlite3.Connection,
    name: str,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        raise AccessionError(f"cannot build empty required table: {name}")
    fields = tuple(rows[0])
    connection.execute(
        f'CREATE TABLE "{name}" ('
        + ",".join(f'"{field}" TEXT NOT NULL' for field in fields)
        + ")"
    )
    connection.executemany(
        f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
        [[str(row.get(field, "")) for field in fields] for row in rows],
    )


def build_restore(
    catalog: list[dict[str, str]],
    receipts: list[dict[str, Any]],
    dispositions: list[dict[str, str]],
    drain: list[dict[str, str]],
    errors: list[dict[str, Any]],
    metadata_checksums: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    manifests = PACKAGE / "db-restore/manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    catalog_fields = tuple(catalog[0])
    atomic_write(
        PACKAGE / "CATALOG.csv",
        csv_bytes(catalog, catalog_fields),
        replace=True,
    )
    atomic_write(
        PACKAGE / "CATALOG.xlsx",
        xlsx_bytes(catalog, catalog_fields, sheet="Catalog", table="Catalog"),
        replace=True,
    )
    atomic_write(
        PACKAGE / "db-restore/RESTORE.csv",
        csv_bytes(catalog, catalog_fields),
        replace=True,
    )
    atomic_write(
        PACKAGE / "db-restore/RESTORE.xlsx",
        xlsx_bytes(catalog, catalog_fields, sheet="Restore", table="Restore"),
        replace=True,
    )
    atomic_write(
        manifests / "representations.csv",
        csv_bytes(receipts, tuple(receipts[0])) if receipts else csv_bytes([], ()),
        replace=True,
    )
    atomic_write(
        manifests / "source-disposition.csv",
        csv_bytes(dispositions, tuple(dispositions[0])),
        replace=True,
    )
    atomic_write(
        manifests / "drain-list.csv",
        csv_bytes(drain, DRAIN_FIELDS),
        replace=True,
    )
    atomic_write(
        manifests / "copy-errors.csv",
        csv_bytes(errors, ERROR_FIELDS),
        replace=True,
    )

    source_tables = {
        "subdivision_register": read_csv(
            SUBDIVISION_ROOT / "subdivision-register.csv"
        ),
        "subdivision_memberships": read_csv(
            SUBDIVISION_ROOT / "subdivision-memberships.csv"
        ),
        "shared_workbook_records": read_csv(
            SHARED_WORKBOOK_ROOT / "shared-workbook-records.csv"
        ),
        "deep_hit_review": read_csv(DEEP_REVIEW_ROOT / "deep-hit-review.csv"),
    }
    checksums = [
        {
            "path": row["final_relative_path"],
            "sha256": row["final_sha256"],
            "size_bytes": row["size_bytes"],
            "content_role": row["role"],
        }
        for row in receipts
    ] + metadata_checksums
    checksums.sort(key=lambda row: row["path"])
    atomic_write(
        PACKAGE / "db-restore/checksums.csv",
        csv_bytes(checksums, CHECKSUM_FIELDS),
        replace=True,
    )

    building = RUN_ROOT / "restore-building.sqlite"
    if building.exists():
        building.unlink()
    connection = sqlite3.connect(building)
    try:
        sqlite_table(connection, "objects", catalog)
        if receipts:
            sqlite_table(connection, "representations", receipts)
        else:
            connection.execute(
                "CREATE TABLE representations (final_relative_path TEXT NOT NULL)"
            )
        sqlite_table(connection, "source_disposition", dispositions)
        sqlite_table(connection, "drain_list", drain)
        for name, rows in source_tables.items():
            sqlite_table(connection, name, rows)
        if errors:
            sqlite_table(connection, "copy_errors", errors)
        else:
            connection.execute(
                "CREATE TABLE copy_errors (error TEXT NOT NULL)"
            )
        sqlite_table(connection, "checksums", checksums)
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("restore database integrity check failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    destination = PACKAGE / "db-restore/RESTORE.sqlite"
    os.replace(building, destination)
    fsync_parent(destination)
    atomic_write(
        PACKAGE / "db-restore/RESTORE.sql",
        sql.encode("utf-8"),
        replace=True,
    )
    return checksums


def write_human_docs(
    catalog: list[dict[str, str]],
    receipts: list[dict[str, Any]],
    dispositions: list[dict[str, str]],
    drain: list[dict[str, str]],
    errors: list[dict[str, Any]],
) -> None:
    document_rows = [
        row for row in receipts if row["final_relative_path"].startswith("documents/")
    ]
    lines = [
        "# Kurt Schwarz project documentation",
        "",
        "These files are exact-byte project records selected by the bounded USB,",
        "shared-container, and cross-project review. Website and Lightroom source",
        "trees were not changed or absorbed; useful reviewed metadata is represented",
        "only in object metadata and reconciliation evidence.",
        "",
    ]
    for row in document_rows:
        lines.extend(
            [
                f"## {Path(row['final_relative_path']).name}",
                "",
                f"- Unit path: `{row['final_relative_path']}`",
                f"- Source: `{row['source_path']}`",
                f"- SHA-256: `{row['final_sha256']}`",
                f"- Relationship: {row['relationship']}",
                "",
            ]
        )
    atomic_write(
        PACKAGE / "documents/CONTENTS.md",
        ("\n".join(lines) + "\n").encode(),
        replace=True,
    )
    state_counts = Counter(row["record_state"] for row in catalog)
    scheme_count = len(
        {
            row["scheme_id"]
            for row in read_csv(SUBDIVISION_ROOT / "subdivision-register.csv")
        }
    )
    readme = f"""# Kurt Schwarz archive unit

This is the single archival source of truth for the Kurt Schwarz project.
It reconciles current and historical museum-digital records with the bounded
USB catalogs, spreadsheets, scans, JPG/TIFF images, and project records.

- Objects: {len(catalog)}
- Archived exact-byte representations: {len(receipts)}
- Non-destructive source/copy errors: {len(errors)}
- USB drain-list rows: {len(drain)}
- Source-disposition rows: {len(dispositions)}
- Independent subdivision schemes: {scheme_count}
- Current MD and local objects: {state_counts['current_md_and_local']}
- Current MD-only objects: {state_counts['current_md_only']}
- Historical deleted MD objects: {state_counts['historical_md_deleted']}
- Local-only objects: {sum(value for key, value in state_counts.items() if key.startswith('local_only'))}

The subdivision register preserves museum-digital collections, Kurt Schwarz
finding-aid groupings (including Menschen and Objekte), and exhibition series
as independent schemes. It does not flatten them into one hierarchy.

Website and Lightroom sources remain untouched and outside this unit's drain
scope. Reviewed metadata from those boundaries may enrich object metadata, but
does not transfer source ownership.

Recovery begins with `db-restore/RESTORE.sqlite` or the paired SQL, CSV, and
XLSX exports. `db-restore/checksums.csv` covers every archived representation
and generated object metadata sidecar. `db-restore/manifests/drain-list.csv`
is exhaustive for the bounded USB-exclusive Kurt Schwarz scope. Every row has
`deletion_authorized=false`; this run performs no deletions.
"""
    atomic_write(PACKAGE / "README.md", readme.encode(), replace=True)


def validate_package(
    catalog: list[dict[str, str]],
    representations: list[dict[str, str]],
    receipts: list[dict[str, Any]],
    dispositions: list[dict[str, str]],
    drain: list[dict[str, str]],
    errors: list[dict[str, Any]],
    checksums: list[dict[str, Any]],
) -> dict[str, Any]:
    read_only, source_mount = source_is_read_only()
    if not read_only:
        raise AccessionError(f"source read-only state was lost: {source_mount}")
    if len(catalog) != 1669:
        raise AccessionError("final object count drifted")
    if len(receipts) + len(errors) != len(representations):
        raise AccessionError("representations are neither copied nor error-logged")
    if len(drain) != 14688:
        raise AccessionError("USB drain scope count drifted")
    if any(row["deletion_authorized"] != "false" for row in dispositions + drain):
        raise AccessionError("final controls authorize deletion")
    for receipt in receipts:
        validate_receipt(PACKAGE, receipt)
    metadata_paths = [row["object_metadata_path"] for row in catalog]
    if len(metadata_paths) != len(set(metadata_paths)):
        raise AccessionError("object metadata paths collide")
    if any(not (PACKAGE / path).is_file() for path in metadata_paths):
        raise AccessionError("object metadata sidecars are incomplete")
    if len(checksums) != len(receipts) + len(catalog):
        raise AccessionError("checksum inventory count drifted")
    connection = sqlite3.connect(
        f"file:{PACKAGE / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("published restore database integrity failed")
        expected = {
            "objects": 1669,
            "representations": len(receipts),
            "source_disposition": len(dispositions),
            "drain_list": 14688,
            "subdivision_register": 47,
            "subdivision_memberships": 2405,
            "shared_workbook_records": 6603,
        }
        for table, count in expected.items():
            actual = connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            if actual != count:
                raise AccessionError(
                    f"restore table count drifted for {table}: {actual} != {count}"
                )
    finally:
        connection.close()
    unwanted = [
        path
        for path in PACKAGE.rglob("*")
        if path.is_symlink() or ".tmp-" in path.name or ".copyhash." in path.name
    ]
    if unwanted:
        raise AccessionError(f"package contains temporary or symlink paths: {unwanted[:3]}")
    return {
        "format": PUBLICATION_FORMAT,
        "validated_at": now(),
        "result": "ok",
        "final_unit": str(FINAL_UNIT),
        "source_read_only": True,
        "source_mount": source_mount,
        "source_safety_state": "safe",
        "write_boundary_state": "confined",
        "fatal_safety_errors": 0,
        "deletion_authorized_rows": 0,
        "object_rows": len(catalog),
        "representation_rows": len(receipts),
        "planned_representation_rows": len(representations),
        "checksum_rows": len(checksums),
        "source_disposition_rows": len(dispositions),
        "drain_list_rows": len(drain),
        "non_destructive_errors": len(errors),
    }


def publish(validation: dict[str, Any]) -> None:
    if FINAL_UNIT.exists():
        raise AccessionError(f"final unit already exists: {FINAL_UNIT}")
    if PACKAGE.stat().st_dev != FINAL_UNIT.parent.stat().st_dev:
        raise AccessionError("final promotion would cross filesystems")
    os.rename(PACKAGE, FINAL_UNIT)
    fsync_parent(FINAL_UNIT)
    publication = dict(validation)
    publication.update(
        {
            "published_at": now(),
            "promotion": "same-filesystem atomic rename",
            "media_rehashed_after_promotion": False,
        }
    )
    write_json(RUN_ROOT / "publication.json", publication)


def execute() -> dict[str, Any]:
    if (RUN_ROOT / "publication.json").is_file():
        publication = json.loads(
            (RUN_ROOT / "publication.json").read_text(encoding="utf-8")
        )
        ensure_boundaries(allow_published=True)
        if Path(publication["final_unit"]) != FINAL_UNIT or not FINAL_UNIT.is_dir():
            raise AccessionError("publication exists without the expected final unit")
        return publication
    mounts = ensure_boundaries()
    objects, representations, planned_dispositions = load_inputs()
    digest = plan_digest()
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    compile_copyhash()
    plan_path = RUN_ROOT / "plan.json"
    plan = {
        "format": FORMAT,
        "created_at": now(),
        "run_id": RUN_ID,
        "unit_id": UNIT_ID,
        "plan_root": str(PLAN_ROOT),
        "plan_digest": digest,
        "write_roots": [str(WORK_BASE), str(FINAL_BASE)],
        "script_root": str(TOOL_ROOT),
        "source_policy": "USB source read-only; website and Lightroom untouched",
        "mounts": mounts,
        "objects": len(objects),
        "representations": len(representations),
        "error_policy": (
            "continue and log non-destructive object errors; stop immediately "
            "on source-write, boundary, collision, or outside-root damage risk"
        ),
        "deletion_authorized": False,
    }
    if plan_path.is_file():
        prior = json.loads(plan_path.read_text(encoding="utf-8"))
        if prior["plan_digest"] != digest:
            raise AccessionError("saved accession plan digest differs")
    else:
        write_json(plan_path, plan)
    PACKAGE.mkdir(parents=True, exist_ok=True)
    receipts, errors = process_representations(representations)
    catalog, metadata_checksums = build_object_metadata(objects, receipts)
    dispositions = final_dispositions(planned_dispositions, receipts, errors)
    drain = build_drain_list(dispositions)
    checksums = build_restore(
        catalog,
        receipts,
        dispositions,
        drain,
        errors,
        metadata_checksums,
    )
    write_human_docs(catalog, receipts, dispositions, drain, errors)
    validation = validate_package(
        catalog,
        representations,
        receipts,
        dispositions,
        drain,
        errors,
        checksums,
    )
    write_json(RUN_ROOT / "validation.json", validation, replace=True)
    publish(validation)
    return json.loads((RUN_ROOT / "publication.json").read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="store_true",
        help="print current receipts/errors/publication without changing state",
    )
    return parser.parse_args()


def status() -> dict[str, Any]:
    receipts = list((RUN_ROOT / "status/representations").glob("*.json"))
    errors = load_errors()
    publication_path = RUN_ROOT / "publication.json"
    return {
        "run_root": str(RUN_ROOT),
        "receipts": len(receipts),
        "non_destructive_errors": len(errors),
        "publication": (
            json.loads(publication_path.read_text(encoding="utf-8"))
            if publication_path.is_file()
            else None
        ),
        "final_unit_exists": FINAL_UNIT.is_dir(),
    }


def main() -> int:
    args = parse_args()
    try:
        result = status() if args.status else execute()
    except (
        AccessionError,
        OSError,
        csv.Error,
        json.JSONDecodeError,
        sqlite3.Error,
        ValueError,
    ) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
