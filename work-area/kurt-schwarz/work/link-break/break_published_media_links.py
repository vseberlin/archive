#!/usr/bin/env python3
"""Copy published Kurt Schwarz media onto independent inodes."""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
PREDECESSOR = Path("/mnt/volume/iss-archive/units/kurt-schwarz-pre-subdivisions")
CHECKSUMS = UNIT / "db-restore/checksums.csv"
RUN_ROOT = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/link-break/"
    "kurt-schwarz-independent-media-001"
)
RECEIPT = RUN_ROOT / "receipt.json"
FAILURE_LOG = RUN_ROOT / "copy-failures.jsonl"
COMPLETION_SIGNAL = RUN_ROOT / "completion-signal.json"
SOURCE_DEVICE = "/dev/sdf1"
BUFFER_SIZE = 16 * 1024 * 1024
EXPECTED_FILES = 3322
EXPECTED_BYTES = 402735046021
MINIMUM_FREE_BYTES = 10 * 1024**3
FATAL_STORAGE_ERRNOS = {
    errno.EIO,
    errno.ENOSPC,
    errno.EDQUOT,
    errno.EROFS,
    errno.ENODEV,
    errno.ESTALE,
}


class LinkBreakError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def source_state() -> str:
    result = subprocess.run(
        ["findmnt", "-rn", "-S", SOURCE_DEVICE, "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"],
        capture_output=True,
        text=True,
        check=False,
    )
    line = result.stdout.strip()
    if not line:
        return f"{SOURCE_DEVICE} unmounted"
    for mount in line.splitlines():
        options = set(mount.split(None, 3)[3].split(","))
        if "ro" not in options or "rw" in options:
            raise LinkBreakError(f"source device is not read-only: {mount}")
    return line


def read_checksums() -> list[dict[str, str]]:
    with CHECKSUMS.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != EXPECTED_FILES:
        raise LinkBreakError(f"checksum row count {len(rows)} != {EXPECTED_FILES}")
    if sum(int(row["size_bytes"]) for row in rows) != EXPECTED_BYTES:
        raise LinkBreakError("checksum byte total drifted")
    if len({row["path"] for row in rows}) != len(rows):
        raise LinkBreakError("checksum paths are not unique")
    return rows


def temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.independent-copy.tmp")


def free_bytes() -> int:
    filesystem = os.statvfs(UNIT)
    return filesystem.f_bavail * filesystem.f_frsize


def append_failure(event: dict[str, object]) -> None:
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    with FAILURE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def preserve_temporary(
    temporary: Path,
    row: dict[str, str],
    index: int,
    reason: str,
) -> str | None:
    if not temporary.exists():
        return None
    token = hashlib.sha256(row["path"].encode("utf-8")).hexdigest()[:12]
    destination = RUN_ROOT / (
        f"failed-copy-{index:04d}-{token}-{time.time_ns()}.partial"
    )
    os.replace(temporary, destination)
    directory_fd = os.open(RUN_ROOT, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return str(destination)


def original_is_unchanged_and_preserved(
    path: Path,
    predecessor: Path,
    row: dict[str, str],
    initial_source_state: str,
) -> bool:
    try:
        if source_state() != initial_source_state:
            return False
        path_stat = path.stat()
        predecessor_stat = predecessor.stat()
    except (OSError, LinkBreakError):
        return False
    expected_size = int(row["size_bytes"])
    return (
        path.is_file()
        and predecessor.is_file()
        and path_stat.st_size == expected_size
        and predecessor_stat.st_size == expected_size
        and path_stat.st_dev == predecessor_stat.st_dev
        and path_stat.st_ino == predecessor_stat.st_ino
    )


def copy_independent(path: Path, row: dict[str, str]) -> int:
    predecessor = PREDECESSOR / row["path"]
    if not predecessor.is_file():
        raise LinkBreakError(f"predecessor path is missing: {predecessor}")
    source_stat = path.stat()
    predecessor_stat = predecessor.stat()
    if source_stat.st_size != int(row["size_bytes"]):
        raise LinkBreakError(f"published size mismatch: {path}")
    if (
        source_stat.st_dev != predecessor_stat.st_dev
        or source_stat.st_ino != predecessor_stat.st_ino
    ):
        if source_stat.st_nlink == 1:
            return 0
        raise LinkBreakError(f"linked file is not bound to the immediate predecessor: {path}")

    temporary = temporary_path(path)
    if temporary.exists():
        raise LinkBreakError(f"temporary file already exists: {temporary}")
    if free_bytes() < source_stat.st_size + MINIMUM_FREE_BYTES:
        raise LinkBreakError(f"unsafe free-space floor reached before copying: {path}")

    digest = hashlib.sha256()
    copied = 0
    try:
        with path.open("rb", buffering=0) as source, temporary.open(
            "xb", buffering=0
        ) as target:
            os.fchmod(target.fileno(), source_stat.st_mode & 0o7777)
            while True:
                block = source.read(BUFFER_SIZE)
                if not block:
                    break
                target.write(block)
                digest.update(block)
                copied += len(block)
            target.flush()
            os.fsync(target.fileno())

        expected_digest = row["sha256"].removeprefix("sha256:").casefold()
        if copied != source_stat.st_size:
            raise LinkBreakError(f"copy byte count mismatch: {path}")
        if digest.hexdigest().casefold() != expected_digest:
            raise LinkBreakError(f"stored checksum mismatch while copying: {path}")
        temporary_stat = temporary.stat()
        if temporary_stat.st_nlink != 1 or temporary_stat.st_ino == source_stat.st_ino:
            raise LinkBreakError(f"temporary copy is not independent: {temporary}")

        os.utime(temporary, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        os.replace(temporary, path)
        replaced_stat = path.stat()
        if replaced_stat.st_nlink != 1 or replaced_stat.st_ino == predecessor_stat.st_ino:
            raise LinkBreakError(f"published file did not become independent: {path}")
        if replaced_stat.st_size != source_stat.st_size:
            raise LinkBreakError(f"published replacement size mismatch: {path}")
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # If replacement happened, the original hard link is no longer at `path`.
        # The caller's preservation check will therefore fail closed.
        raise
    return copied


def validate_database() -> None:
    database = UNIT / "db-restore/RESTORE.sqlite"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LinkBreakError("RESTORE.sqlite quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise LinkBreakError("RESTORE.sqlite foreign-key check failed")
        for table in ("source_disposition", "drain_list", "reconciliation_decisions"):
            values = connection.execute(
                f'SELECT DISTINCT deletion_authorized FROM "{table}"'
            ).fetchall()
            if values != [("false",)]:
                raise LinkBreakError(f"deletion authorization changed in {table}")
    finally:
        connection.close()


def main() -> None:
    if RECEIPT.exists():
        raise LinkBreakError(f"completion receipt already exists: {RECEIPT}")
    if not UNIT.is_dir() or not PREDECESSOR.is_dir():
        raise LinkBreakError("published unit or immediate predecessor is missing")
    rows = read_checksums()
    initial_source_state = source_state()
    RUN_ROOT.mkdir(parents=True, exist_ok=True)

    # An interruption can leave only our partial temporary copy. Preserve it in
    # the work root, but only after proving the published original and immediate
    # predecessor still point at the same expected inode.
    for index, row in enumerate(rows, 1):
        path = UNIT / row["path"]
        temporary = temporary_path(path)
        if not temporary.exists():
            continue
        predecessor = PREDECESSOR / row["path"]
        if not original_is_unchanged_and_preserved(
            path, predecessor, row, initial_source_state
        ):
            raise LinkBreakError(
                f"temporary copy exists but original preservation cannot be proven: {path}"
            )
        preserved = preserve_temporary(
            temporary, row, index, "interrupted temporary copy"
        )
        append_failure(
            {
                "at": now(),
                "event": "interrupted_temporary_preserved",
                "path": row["path"],
                "temporary_preserved_as": preserved,
                "published_original_unchanged": True,
                "predecessor_preserved": True,
            }
        )

    available_bytes = free_bytes()
    linked_bytes = sum(
        int(row["size_bytes"])
        for row in rows
        if (UNIT / row["path"]).stat().st_nlink > 1
    )
    if available_bytes < linked_bytes + MINIMUM_FREE_BYTES:
        raise LinkBreakError(
            f"insufficient free space: {available_bytes} available, {linked_bytes} required"
        )

    started_at = now()
    start = time.monotonic()
    copied_files = 0
    copied_bytes = 0
    skipped_independent = 0
    recoverable_failures = 0
    for index, row in enumerate(rows, 1):
        path = UNIT / row["path"]
        try:
            copied = copy_independent(path, row)
        except Exception as error:
            predecessor = PREDECESSOR / row["path"]
            fatal_storage_error = (
                isinstance(error, OSError) and error.errno in FATAL_STORAGE_ERRNOS
            )
            preserved = original_is_unchanged_and_preserved(
                path, predecessor, row, initial_source_state
            )
            if fatal_storage_error or not preserved:
                raise LinkBreakError(
                    "copy halted because safe continuation cannot be proven for "
                    f"{path}: {type(error).__name__}: {error}"
                ) from error
            partial = preserve_temporary(
                temporary_path(path), row, index, str(error)
            )
            append_failure(
                {
                    "at": now(),
                    "event": "recoverable_copy_failure",
                    "path": row["path"],
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "partial_copy_preserved_as": partial,
                    "published_original_unchanged": True,
                    "predecessor_preserved": True,
                    "continued": True,
                }
            )
            recoverable_failures += 1
            print(
                f"recoverable_failure index={index} path={row['path']!r} "
                f"error={type(error).__name__}: {error}",
                flush=True,
            )
            continue
        if copied:
            copied_files += 1
            copied_bytes += copied
        else:
            skipped_independent += 1
        if index == 1 or index % 25 == 0 or index == len(rows):
            elapsed = max(time.monotonic() - start, 0.001)
            rate = copied_bytes / elapsed
            remaining = max(EXPECTED_BYTES - copied_bytes, 0)
            eta = int(remaining / rate) if rate else 0
            print(
                f"checked={index}/{len(rows)} copied={copied_files} "
                f"bytes={copied_bytes}/{EXPECTED_BYTES} "
                f"rate_mib_s={rate / 1024**2:.1f} eta_s={eta}",
                flush=True,
            )

    final_source_state = source_state()
    if final_source_state != initial_source_state:
        raise LinkBreakError("source-device state changed during copying")

    linked_remaining = []
    missing = []
    size_mismatches = []
    for row in rows:
        path = UNIT / row["path"]
        predecessor = PREDECESSOR / row["path"]
        if not path.is_file() or not predecessor.is_file():
            missing.append(row["path"])
            continue
        path_stat = path.stat()
        predecessor_stat = predecessor.stat()
        if path_stat.st_size != int(row["size_bytes"]):
            size_mismatches.append(row["path"])
        if path_stat.st_nlink != 1 or (
            path_stat.st_dev == predecessor_stat.st_dev
            and path_stat.st_ino == predecessor_stat.st_ino
        ):
            linked_remaining.append(row["path"])
    if missing or size_mismatches:
        raise LinkBreakError(
            "final safety check failed: "
            f"missing={len(missing)} size={len(size_mismatches)}"
        )
    if any(temporary_path(UNIT / row["path"]).exists() for row in rows):
        raise LinkBreakError("temporary files remain after completion")
    validate_database()

    receipt = {
        "format": "iss-kurt-schwarz-independent-media-v1",
        "result": "ok" if not linked_remaining else "completed_with_failures",
        "started_at": started_at,
        "completed_at": now(),
        "published_unit": str(UNIT),
        "immediate_predecessor": str(PREDECESSOR),
        "predecessor_preserved": True,
        "source_state": initial_source_state,
        "source_files_changed": False,
        "predecessor_files_changed": False,
        "catalogue_files_changed": False,
        "checksum_manifest_changed": False,
        "published_media_files": EXPECTED_FILES,
        "published_media_bytes": EXPECTED_BYTES,
        "files_copied_to_independent_inodes": copied_files,
        "already_independent_files": skipped_independent,
        "stored_checksums_verified_while_copying": copied_files,
        "recoverable_copy_failures_this_run": recoverable_failures,
        "failure_log": str(FAILURE_LOG) if FAILURE_LOG.exists() else None,
        "remaining_linked_published_files": len(linked_remaining),
        "remaining_linked_paths": linked_remaining,
        "deletion_authorized": False,
    }
    receipt_payload = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    with RECEIPT.open("x", encoding="utf-8") as handle:
        handle.write(receipt_payload)
        handle.flush()
        os.fsync(handle.fileno())
    print(receipt_payload, end="", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (LinkBreakError, OSError, csv.Error, sqlite3.Error, ValueError) as error:
        raise SystemExit(f"ERROR: {error}")
