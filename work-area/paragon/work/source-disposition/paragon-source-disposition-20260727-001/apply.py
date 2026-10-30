#!/usr/bin/env python3
"""Install the reviewed Paragon source-disposition register transactionally."""

from __future__ import annotations

import csv
import filecmp
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path


RUN = Path(__file__).resolve().parent
FINAL = Path("/mnt/volume/iss-archive/units/paragon")
PLAN = RUN / "source-disposition.planned.csv"
PLAN_SUMMARY = RUN / "classification-summary.json"
USB_INVENTORY = RUN / "usb-inventory.csv"
TARGET = FINAL / "db-restore/manifests/source-disposition.csv"
CHECKSUMS = FINAL / "db-restore/checksums.csv"
README = FINAL / "README.md"
RESTORE = FINAL / "db-restore/RESTORE.sqlite"
BEFORE = RUN / "before-apply"
STAGE = RUN / "stage-apply"
RECEIPT = RUN / "receipt.json"
FIELDS = [
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDS and path in {PLAN, TARGET}:
            raise RuntimeError(f"wrong source-disposition header: {reader.fieldnames}")
        return list(reader)


def write_csv(
    path: Path, rows: list[dict[str, str]], fields: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def ensure_usb_read_only() -> str:
    output = subprocess.run(
        [
            "findmnt",
            "-rn",
            "-T",
            "/media/vladimir/Elements/restore/archive/Paragon",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    line = output.splitlines()[0] if output else ""
    options = line.split(None, 3)[-1].split(",") if line else []
    if "ro" not in options or "rw" in options:
        raise RuntimeError(f"USB is not read-only: {line}")
    return line


def validate_plan() -> tuple[list[dict[str, str]], dict]:
    rows = read_csv(PLAN)
    inventory_rows = {}
    with USB_INVENTORY.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            inventory_rows[row["source_path"]] = row
    if len(rows) != 1413 or len(inventory_rows) != 1413:
        raise RuntimeError("planned source scope is incomplete")
    if len({row["source_path"] for row in rows}) != len(rows):
        raise RuntimeError("source-disposition contains duplicate paths")
    if {row["source_path"] for row in rows} != set(inventory_rows):
        raise RuntimeError("source-disposition path coverage differs from USB inventory")
    for row in rows:
        source = Path(row["source_path"])
        inventory = inventory_rows[row["source_path"]]
        stat = source.stat()
        if (
            row["deletion_authorized"] != "false"
            or row["source_kind"] != "file"
            or row["unit"] != "paragon"
        ):
            raise RuntimeError(f"unsafe disposition row: {row['source_path']}")
        if (
            row["source_sha256"] != inventory["sha256"]
            or row["source_size_bytes"] != inventory["size_bytes"]
            or stat.st_size != int(inventory["size_bytes"])
            or stat.st_mtime_ns != int(inventory["mtime_ns"])
        ):
            raise RuntimeError(f"source state changed: {source}")
        if row["drain_state"] == "candidate_after_separate_review":
            if not row["final_path"] or not row["final_sha256"]:
                raise RuntimeError(f"candidate lacks final evidence: {source}")
        elif row["drain_state"] != "retain_not_preserved":
            raise RuntimeError(f"unexpected drain state: {row['drain_state']}")
    summary = json.loads(PLAN_SUMMARY.read_text(encoding="utf-8"))
    if (
        summary["rows"] != len(rows)
        or summary["candidate_rows"]
        + summary["retain_rows"]
        != len(rows)
        or summary["deletion_authorized_true"] != 0
    ):
        raise RuntimeError("classification summary differs from plan")
    return rows, summary


def updated_readme(summary: dict) -> str:
    current = README.read_text(encoding="utf-8")
    old = (
        "This unit is not ready for sealing until the mandatory "
        "`db-restore/manifests/source-disposition.csv` is complete."
    )
    if old not in current:
        raise RuntimeError("README sealing statement changed unexpectedly")
    new = (
        "Source disposition:\n\n"
        f"- `db-restore/manifests/source-disposition.csv` covers all "
        f"{summary['rows']:,} exact paths in the seven reviewed Paragon-named "
        "trees on the authoritative read-only Elements USB.\n"
        f"- {summary['candidate_rows']:,} preserved sources are candidates only "
        "after a separate future review; "
        f"{summary['retain_rows']:,} unproven, intermediate, or explicitly "
        "rejected paths must be retained.\n"
        "- Every row has `deletion_authorized=false`; this register and sealing "
        "never authorize deletion. `archive-intake` was not used as source "
        "authority.\n\n"
        "The source-disposition requirement is complete, but the unit is not "
        "ready for sealing: nine active catalog objects still have no "
        "preservation representation."
    )
    return current.replace(old, new)


def backup() -> None:
    if BEFORE.exists() or STAGE.exists() or RECEIPT.exists() or TARGET.exists():
        raise RuntimeError("apply state already exists")
    for source in (README, CHECKSUMS):
        destination = BEFORE / source.relative_to(FINAL)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def stage(summary: dict) -> None:
    target = STAGE / TARGET.relative_to(FINAL)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLAN, target)
    if not filecmp.cmp(PLAN, target, shallow=False):
        raise RuntimeError("staged disposition differs from reviewed plan")

    staged_readme = STAGE / "README.md"
    staged_readme.parent.mkdir(parents=True, exist_ok=True)
    staged_readme.write_text(updated_readme(summary), encoding="utf-8")

    with CHECKSUMS.open(encoding="utf-8", newline="") as handle:
        checksum_rows = list(csv.DictReader(handle))
    checksum_rows = [
        row
        for row in checksum_rows
        if row["path"]
        not in {"README.md", "db-restore/manifests/source-disposition.csv"}
    ]
    for relative, path in (
        ("README.md", staged_readme),
        ("db-restore/manifests/source-disposition.csv", target),
    ):
        checksum_rows.append(
            {
                "path": relative,
                "sha256": digest(path),
                "size_bytes": str(path.stat().st_size),
            }
        )
    checksum_rows.sort(key=lambda row: row["path"])
    if len({row["path"] for row in checksum_rows}) != len(checksum_rows):
        raise RuntimeError("duplicate checksum paths")
    write_csv(
        STAGE / "db-restore/checksums.csv",
        checksum_rows,
        ["path", "sha256", "size_bytes"],
    )


def install_file(staged: Path, final: Path) -> None:
    temporary = final.with_name(final.name + ".source-disposition-new")
    if temporary.exists():
        raise RuntimeError(f"temporary path exists: {temporary}")
    shutil.copy2(staged, temporary)
    os.replace(temporary, final)


def rollback() -> None:
    if TARGET.exists():
        TARGET.unlink()
    for relative in ("README.md", "db-restore/checksums.csv"):
        source = BEFORE / relative
        if source.is_file():
            shutil.copy2(source, FINAL / relative)


def promote() -> None:
    try:
        install_file(STAGE / TARGET.relative_to(FINAL), TARGET)
        install_file(STAGE / "README.md", README)
        install_file(STAGE / "db-restore/checksums.csv", CHECKSUMS)
    except Exception:
        rollback()
        raise


def validate_final(
    planned_rows: list[dict[str, str]], summary: dict
) -> dict:
    if not filecmp.cmp(PLAN, TARGET, shallow=False):
        raise RuntimeError("final source-disposition differs from reviewed plan")
    rows = read_csv(TARGET)
    if rows != planned_rows:
        raise RuntimeError("final source-disposition row content changed")

    with CHECKSUMS.open(encoding="utf-8", newline="") as handle:
        checksum_map = {
            row["path"]: row for row in csv.DictReader(handle)
        }
    for relative in ("README.md", "db-restore/manifests/source-disposition.csv"):
        path = FINAL / relative
        row = checksum_map.get(relative)
        if (
            not row
            or row["sha256"] != digest(path)
            or int(row["size_bytes"]) != path.stat().st_size
        ):
            raise RuntimeError(f"checksum control mismatch: {relative}")

    connection = sqlite3.connect(f"file:{RESTORE}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        state = connection.execute(
            "SELECT catalog_revision,published_revision,dirty_since "
            "FROM catalog_state WHERE singleton=1"
        ).fetchone()
        unrepresented = connection.execute(
            """SELECT count(*) FROM archive_objects a
               JOIN entities e USING(entity_id)
               WHERE a.status='active' AND e.status='active'
                 AND NOT EXISTS (
                     SELECT 1 FROM entity_files ef
                     WHERE ef.entity_id=a.entity_id AND ef.status='active'
                       AND ef.role IN (
                           'preservation_master','preservation_surrogate'
                       )
                 )"""
        ).fetchone()[0]
    finally:
        connection.close()
    if (
        integrity != "ok"
        or foreign_keys
        or state != (9976, 9976, None)
        or unrepresented != 9
    ):
        raise RuntimeError("catalog readiness state changed")
    mount = ensure_usb_read_only()
    return {
        "rows": len(rows),
        "candidate_rows": summary["candidate_rows"],
        "retain_rows": summary["retain_rows"],
        "deletion_authorized_true": 0,
        "exact_path_coverage": True,
        "catalog_state": list(state),
        "restore_integrity": integrity,
        "restore_foreign_key_violations": foreign_keys,
        "active_objects_without_preservation_representation": unrepresented,
        "usb_mount": mount,
        "source_deleted": False,
    }


def main() -> int:
    mount = ensure_usb_read_only()
    rows, summary = validate_plan()
    backup()
    stage(summary)
    promote()
    try:
        validation = validate_final(rows, summary)
    except Exception:
        rollback()
        raise
    receipt = {
        "format": "iss-paragon-source-disposition-apply-receipt-v1",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "authority": "/media/vladimir/Elements",
        "excluded_authority": "archive-intake",
        "reviewed_roots": json.loads(
            (RUN / "usb-inventory-summary.json").read_text(encoding="utf-8")
        )["roots"],
        "plan_sha256": digest(PLAN),
        "summary": summary,
        "validation": validation,
        "pre_apply_controls": str(BEFORE),
        "usb_mount_at_start": mount,
        "deletion_authorized": False,
    }
    RECEIPT.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(validation, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
