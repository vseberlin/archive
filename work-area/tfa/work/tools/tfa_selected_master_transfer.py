#!/usr/bin/env python3
"""Prepare and run the guarded TFA selected-Master transfer.

The prior curated selection is the read-only ``Alle-Tiff-Positive`` folder.
Only records with exactly one TIFF in that selection are transferable.  This
tool does not hash, decode, edit, or delete image files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


RECONCILIATION_ROOT = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation"
)
DATABASE = RECONCILIATION_ROOT / "reconciliation.sqlite"
SOURCE_DIRECTORY = Path(
    "/media/vladimir/Elements/restore/archive/Alle-Tiff-Positive"
)
DESTINATION_DIRECTORY = Path("/mnt/volume/iss-archive-work/tfa/work/masters")
SELECTED_MANIFEST = RECONCILIATION_ROOT / "selected-master-transfer.csv"
EXCEPTIONS_MANIFEST = RECONCILIATION_ROOT / "selected-master-exceptions.csv"
REDUNDANT_MANIFEST = RECONCILIATION_ROOT / "selected-master-redundant-candidates.csv"
FILES0 = RECONCILIATION_ROOT / "selected-master-transfer.files0"
PREFLIGHT_RECEIPT = RECONCILIATION_ROOT / "selected-master-transfer-preflight.json"
COPY_RECEIPT = RECONCILIATION_ROOT / "selected-master-transfer-completion.json"
COPY_STATE = RECONCILIATION_ROOT / "selected-master-transfer-state.csv"
COPY_ERRORS = RECONCILIATION_ROOT / "selected-master-transfer-errors.csv"

SELECTION_TOKEN = "/Alle-Tiff-Positive/"
EXPECTED_SOURCE_DEVICE = "/dev/sdf1"
EXPECTED_SOURCE_FSTYPE = "ntfs3"


class TransferError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    write_bytes_exclusive(path, json_bytes(payload))


def write_csv_exclusive(
    path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]
) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        [
            "findmnt",
            "-J",
            "-T",
            str(SOURCE_DIRECTORY),
            "-o",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise TransferError("source directory did not resolve to one filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if (
        mount.get("source") != EXPECTED_SOURCE_DEVICE
        or mount.get("fstype") != EXPECTED_SOURCE_FSTYPE
        or "ro" not in options
    ):
        raise TransferError(f"read-only source mount guard failed: {mount}")
    return mount


def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def prepare() -> int:
    outputs = (
        SELECTED_MANIFEST,
        EXCEPTIONS_MANIFEST,
        REDUNDANT_MANIFEST,
        FILES0,
        PREFLIGHT_RECEIPT,
    )
    existing = [str(path) for path in outputs if path.exists()]
    if existing:
        raise TransferError(f"refusing to replace transfer evidence: {existing}")
    if COPY_RECEIPT.exists() or COPY_STATE.exists():
        raise TransferError("copy evidence exists before preflight")

    mount = source_mount()
    connection = open_database()
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick_check != "ok" or foreign_keys:
            raise TransferError(
                f"SQLite guard failed: quick_check={quick_check}, foreign_keys={len(foreign_keys)}"
            )

        objects = list(
            connection.execute(
                """
                SELECT o.record_key, o.md_object_id, o.inventory,
                       SUM(CASE WHEN lower(c.extension) IN ('.tif', '.tiff')
                                     AND instr(c.source_path, ?) > 0
                                THEN 1 ELSE 0 END) AS prior_selected_count,
                       SUM(CASE WHEN lower(c.extension) IN ('.tif', '.tiff')
                                THEN 1 ELSE 0 END) AS tiff_candidate_count
                FROM objects o
                LEFT JOIN candidates c ON c.record_key = o.record_key
                WHERE o.record_key LIKE 'md:%'
                GROUP BY o.record_key, o.md_object_id, o.inventory
                ORDER BY CAST(o.md_object_id AS INTEGER), o.record_key
                """,
                (SELECTION_TOKEN,),
            )
        )

        selected_rows: list[dict[str, Any]] = []
        exception_rows: list[dict[str, Any]] = []
        redundant_rows: list[dict[str, Any]] = []

        for obj in objects:
            prior_count = int(obj["prior_selected_count"] or 0)
            tiff_count = int(obj["tiff_candidate_count"] or 0)
            if prior_count != 1:
                if tiff_count == 0:
                    exception_type = "no_tiff_candidate"
                elif prior_count == 0:
                    exception_type = "no_prior_selected_tiff"
                else:
                    exception_type = "multiple_prior_selected_tiffs"
                paths = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT source_path FROM candidates
                        WHERE record_key = ? AND lower(extension) IN ('.tif', '.tiff')
                        ORDER BY source_path
                        """,
                        (obj["record_key"],),
                    )
                ]
                exception_rows.append(
                    {
                        "record_key": obj["record_key"],
                        "md_object_id": obj["md_object_id"],
                        "inventory": obj["inventory"],
                        "exception_type": exception_type,
                        "prior_selected_count": prior_count,
                        "tiff_candidate_count": tiff_count,
                        "tiff_candidate_paths_json": json.dumps(
                            paths, ensure_ascii=False, separators=(",", ":")
                        ),
                        "decision_state": "unresolved",
                    }
                )
                continue

            selected = connection.execute(
                """
                SELECT candidate_id, source_path, basename, size_bytes, mtime_ns,
                       extension
                FROM candidates
                WHERE record_key = ?
                  AND lower(extension) IN ('.tif', '.tiff')
                  AND instr(source_path, ?) > 0
                """,
                (obj["record_key"], SELECTION_TOKEN),
            ).fetchone()
            if selected is None:
                raise TransferError(f"selection query drift for {obj['record_key']}")
            source = Path(selected["source_path"])
            if source.parent != SOURCE_DIRECTORY:
                raise TransferError(f"selected source escaped selection directory: {source}")
            stat = source.stat()
            if stat.st_size != selected["size_bytes"] or stat.st_mtime_ns != selected["mtime_ns"]:
                raise TransferError(f"source stat guard changed: {source}")

            selected_rows.append(
                {
                    "record_key": obj["record_key"],
                    "md_object_id": obj["md_object_id"],
                    "inventory": obj["inventory"],
                    "candidate_id": selected["candidate_id"],
                    "source_path": selected["source_path"],
                    "destination_path": str(DESTINATION_DIRECTORY / selected["basename"]),
                    "basename": selected["basename"],
                    "extension": selected["extension"],
                    "size_bytes": selected["size_bytes"],
                    "mtime_ns": selected["mtime_ns"],
                    "selection_basis": "prior_curated_Alle-Tiff-Positive_confirmed_by_user",
                    "path_size_mtime_guard": "passed",
                    "copy_state": "pending",
                }
            )

            for candidate in connection.execute(
                """
                SELECT candidate_id, source_path, basename, extension, size_bytes,
                       proposed_role
                FROM candidates
                WHERE record_key = ? AND candidate_id != ?
                ORDER BY source_path
                """,
                (obj["record_key"], selected["candidate_id"]),
            ):
                extension = str(candidate["extension"]).casefold()
                disposition = (
                    "redundant_rejected_master_candidate"
                    if extension in {".tif", ".tiff"}
                    else "redundant_derivative_or_copy"
                )
                redundant_rows.append(
                    {
                        "record_key": obj["record_key"],
                        "md_object_id": obj["md_object_id"],
                        "inventory": obj["inventory"],
                        "selected_candidate_id": selected["candidate_id"],
                        "candidate_id": candidate["candidate_id"],
                        "source_path": candidate["source_path"],
                        "basename": candidate["basename"],
                        "extension": candidate["extension"],
                        "size_bytes": candidate["size_bytes"],
                        "prior_role": candidate["proposed_role"],
                        "disposition": disposition,
                        "disposition_basis": "excluded_by_confirmed_prior_curated_selection",
                        "drain_state": "proposed_redundant",
                        "deletion_authorized": "false",
                    }
                )
    finally:
        connection.close()

    expected_selected = 5092
    if len(selected_rows) != expected_selected:
        raise TransferError(
            f"selected scope drifted: {len(selected_rows)} != {expected_selected}"
        )
    if len({row["record_key"] for row in selected_rows}) != len(selected_rows):
        raise TransferError("selected records are not unique")
    if len({row["source_path"] for row in selected_rows}) != len(selected_rows):
        raise TransferError("selected source paths are not unique")
    if len({row["basename"].casefold() for row in selected_rows}) != len(selected_rows):
        raise TransferError("case-insensitive selected basenames are not unique")
    if len({row["inventory"].casefold() for row in selected_rows}) != len(selected_rows):
        raise TransferError("selected inventories are not unique")

    selected_fields = (
        "record_key",
        "md_object_id",
        "inventory",
        "candidate_id",
        "source_path",
        "destination_path",
        "basename",
        "extension",
        "size_bytes",
        "mtime_ns",
        "selection_basis",
        "path_size_mtime_guard",
        "copy_state",
    )
    exception_fields = (
        "record_key",
        "md_object_id",
        "inventory",
        "exception_type",
        "prior_selected_count",
        "tiff_candidate_count",
        "tiff_candidate_paths_json",
        "decision_state",
    )
    redundant_fields = (
        "record_key",
        "md_object_id",
        "inventory",
        "selected_candidate_id",
        "candidate_id",
        "source_path",
        "basename",
        "extension",
        "size_bytes",
        "prior_role",
        "disposition",
        "disposition_basis",
        "drain_state",
        "deletion_authorized",
    )
    write_csv_exclusive(SELECTED_MANIFEST, selected_fields, selected_rows)
    write_csv_exclusive(EXCEPTIONS_MANIFEST, exception_fields, exception_rows)
    write_csv_exclusive(REDUNDANT_MANIFEST, redundant_fields, redundant_rows)
    write_bytes_exclusive(
        FILES0,
        b"".join(os.fsencode(row["basename"]) + b"\0" for row in selected_rows),
    )
    fingerprints = {
        path.name: sha256_file(path)
        for path in (
            SELECTED_MANIFEST,
            EXCEPTIONS_MANIFEST,
            REDUNDANT_MANIFEST,
            FILES0,
        )
    }
    counts: dict[str, int] = {}
    for row in exception_rows:
        key = row["exception_type"]
        counts[key] = counts.get(key, 0) + 1
    receipt = {
        "format": "iss-tfa-selected-master-transfer-preflight-v1",
        "prepared_at": utc_now(),
        "result": "ok",
        "selection_authority": str(SOURCE_DIRECTORY),
        "selection_basis": "prior_curated_selection_confirmed_by_user_2026-08-10",
        "source_mount": mount,
        "source_read_only": True,
        "selected_files": len(selected_rows),
        "selected_bytes": sum(int(row["size_bytes"]) for row in selected_rows),
        "redundant_candidate_rows": len(redundant_rows),
        "exception_counts": counts,
        "destination": str(DESTINATION_DIRECTORY),
        "hashing_performed": False,
        "decoding_performed": False,
        "metadata_modified": False,
        "deletion_authorized": False,
        "fingerprints": fingerprints,
    }
    write_json_exclusive(PREFLIGHT_RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def load_preflight() -> dict[str, Any]:
    payload = json.loads(PREFLIGHT_RECEIPT.read_text(encoding="utf-8"))
    if payload.get("result") != "ok":
        raise TransferError("preflight result is not ok")
    for name, expected in payload.get("fingerprints", {}).items():
        path = RECONCILIATION_ROOT / name
        actual = sha256_file(path)
        if actual != expected:
            raise TransferError(f"preflight evidence changed: {path}")
    return payload


def read_selected_manifest() -> list[dict[str, str]]:
    with SELECTED_MANIFEST.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_selected_sources(rows: list[dict[str, str]]) -> None:
    for row in rows:
        source = Path(row["source_path"])
        if source.parent != SOURCE_DIRECTORY or source.name != row["basename"]:
            raise TransferError(f"source binding failed: {source}")
        stat = source.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise TransferError(f"source size guard failed: {source}")
        if stat.st_mtime_ns != int(row["mtime_ns"]):
            raise TransferError(f"source mtime guard failed: {source}")


def copy() -> int:
    if COPY_RECEIPT.exists() or COPY_STATE.exists() or COPY_ERRORS.exists():
        raise TransferError("refusing to replace existing copy evidence")
    preflight = load_preflight()
    mount_start = source_mount()
    rows = read_selected_manifest()
    if len(rows) != int(preflight["selected_files"]):
        raise TransferError("selected manifest count disagrees with preflight")
    validate_selected_sources(rows)

    DESTINATION_DIRECTORY.mkdir(parents=True, exist_ok=True)
    expected_by_name = {row["basename"]: int(row["size_bytes"]) for row in rows}
    for existing in DESTINATION_DIRECTORY.iterdir():
        if not existing.is_file() or existing.name not in expected_by_name:
            raise TransferError(f"unexpected destination entry: {existing}")
        if existing.stat().st_size != expected_by_name[existing.name]:
            raise TransferError(f"incomplete or changed destination file: {existing}")
    free_bytes = shutil.disk_usage(DESTINATION_DIRECTORY.parent).free
    required_bytes = int(preflight["selected_bytes"])
    if free_bytes < required_bytes + 20 * 1024**3:
        raise TransferError(
            f"insufficient destination space: free={free_bytes}, required={required_bytes}"
        )

    started_at = utc_now()
    command = [
        "rsync",
        "--archive",
        "--no-owner",
        "--no-group",
        "--from0",
        f"--files-from={FILES0}",
        "--human-readable",
        "--info=progress2,stats2",
        "--itemize-changes",
        "--protect-args",
        str(SOURCE_DIRECTORY) + "/",
        str(DESTINATION_DIRECTORY) + "/",
    ]
    print(f"started_at={started_at}", flush=True)
    print("command=" + " ".join(command), flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode not in (0, 23):
        raise TransferError(
            f"rsync stopped with critical or non-recoverable exit code "
            f"{completed.returncode}"
        )

    validate_selected_sources(rows)
    mount_end = source_mount()
    state_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    for row in rows:
        destination = Path(row["destination_path"])
        expected_size = int(row["size_bytes"])
        try:
            stat = destination.stat()
        except FileNotFoundError:
            destination_size: int | str = ""
            destination_mtime: int | str = ""
            copy_state = "copy_error_missing_destination"
            error_rows.append(
                {
                    "record_key": row["record_key"],
                    "inventory": row["inventory"],
                    "source_path": row["source_path"],
                    "destination_path": str(destination),
                    "expected_size_bytes": expected_size,
                    "observed_size_bytes": "",
                    "error_type": "missing_destination",
                    "rsync_exit_code": completed.returncode,
                    "action": "retained_source_retry_or_review",
                }
            )
        else:
            destination_size = stat.st_size
            destination_mtime = stat.st_mtime_ns
            if stat.st_size != expected_size:
                copy_state = "copy_error_size_mismatch"
                error_rows.append(
                    {
                        "record_key": row["record_key"],
                        "inventory": row["inventory"],
                        "source_path": row["source_path"],
                        "destination_path": str(destination),
                        "expected_size_bytes": expected_size,
                        "observed_size_bytes": stat.st_size,
                        "error_type": "destination_size_mismatch",
                        "rsync_exit_code": completed.returncode,
                        "action": "retained_source_retry_or_review",
                    }
                )
            else:
                copy_state = "copied_size_validated"
        state_rows.append(
            {
                "record_key": row["record_key"],
                "inventory": row["inventory"],
                "source_path": row["source_path"],
                "destination_path": str(destination),
                "expected_size_bytes": expected_size,
                "destination_size_bytes": destination_size,
                "source_mtime_ns": row["mtime_ns"],
                "destination_mtime_ns": destination_mtime,
                "copy_state": copy_state,
                "sha256": "",
                "metadata_embedded": "false",
                "decoded": "false",
            }
        )
    state_fields = (
        "record_key",
        "inventory",
        "source_path",
        "destination_path",
        "expected_size_bytes",
        "destination_size_bytes",
        "source_mtime_ns",
        "destination_mtime_ns",
        "copy_state",
        "sha256",
        "metadata_embedded",
        "decoded",
    )
    write_csv_exclusive(COPY_STATE, state_fields, state_rows)
    error_fields = (
        "record_key",
        "inventory",
        "source_path",
        "destination_path",
        "expected_size_bytes",
        "observed_size_bytes",
        "error_type",
        "rsync_exit_code",
        "action",
    )
    if completed.returncode == 23 and not error_rows:
        error_rows.append(
            {
                "record_key": "",
                "inventory": "",
                "source_path": "",
                "destination_path": "",
                "expected_size_bytes": "",
                "observed_size_bytes": "",
                "error_type": "rsync_partial_error_see_transfer_log",
                "rsync_exit_code": completed.returncode,
                "action": "review_log_before_accession",
            }
        )
    write_csv_exclusive(COPY_ERRORS, error_fields, error_rows)
    valid_rows = [row for row in state_rows if row["copy_state"] == "copied_size_validated"]
    result = "ok" if completed.returncode == 0 and not error_rows else "partial"
    receipt = {
        "format": "iss-tfa-selected-master-transfer-completion-v1",
        "started_at": started_at,
        "completed_at": utc_now(),
        "result": result,
        "source_mount_start": mount_start,
        "source_mount_end": mount_end,
        "source_read_only": True,
        "selected_manifest_sha256": sha256_file(SELECTED_MANIFEST),
        "expected_files": len(state_rows),
        "copied_files": len(valid_rows),
        "copy_error_rows": len(error_rows),
        "rsync_exit_code": completed.returncode,
        "copied_bytes": sum(int(row["destination_size_bytes"]) for row in valid_rows),
        "destination": str(DESTINATION_DIRECTORY),
        "destination_size_validation": "passed" if not error_rows else "partial",
        "source_guards_rechecked": True,
        "hashing_performed": False,
        "decoding_performed": False,
        "metadata_modified": False,
        "deletion_authorized": False,
        "copy_state_sha256": sha256_file(COPY_STATE),
        "copy_errors_sha256": sha256_file(COPY_ERRORS),
    }
    write_json_exclusive(COPY_RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "copy"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return prepare() if args.action == "prepare" else copy()
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TransferError) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
