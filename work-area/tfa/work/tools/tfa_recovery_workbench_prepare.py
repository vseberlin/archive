#!/usr/bin/env python3
"""Prepare the bounded TFA ambiguous-master recovery workbench projection."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


WORK = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW = WORK / "work/reconciliation/unit-reconciliation"
RUN = WORK / "work/accession-runs/tfa-md-001"
DB = REVIEW / "reconciliation.sqlite"
ERRORS = RUN / "errors.jsonl"
QUEUE = REVIEW / "master-recovery-ambiguous.csv"
VALIDATION = REVIEW / "master-recovery-candidates.csv"
RECEIPT = REVIEW / "master-recovery-workbench.json"
SOURCE_ROOT = Path("/media/vladimir/Elements")
TMP = RUN / "tmp"
QUEUE_FIELDS = (
    "record_key", "md_object_id", "inventory", "title", "failed_candidate_id",
    "failed_source_path", "accession_error", "valid_candidate_count",
    "valid_candidate_ids", "validation_basis",
)
VALIDATION_FIELDS = (
    "record_key", "md_object_id", "inventory", "candidate_id", "source_path",
    "basename", "size_bytes", "mtime_ns", "variant_label", "decode_status",
    "validation_basis", "deletion_authorized",
)


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise PreparationError("source mount did not resolve exactly once")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise PreparationError(f"source mount is not protected read-only: {row}")
    return row


def inside_source(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(SOURCE_ROOT.resolve(strict=True))
    except ValueError as error:
        raise PreparationError(f"candidate escaped protected source root: {resolved}") from error
    return resolved


def csv_bytes(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def variant(path: str) -> str:
    lowered = path.casefold()
    if "/korrektur_2_tranche/positiv/tiff/" in lowered:
        return "corrected positive TIFF"
    if "/korrektur_2_tranche/positiv/jpg/" in lowered:
        return "corrected positive TIFF in JPG folder"
    if "/tiff_postive_banderole/" in lowered:
        return "positive banderole TIFF"
    if "/tiff positive/" in lowered or "/tiff_positive/" in lowered:
        return "positive TIFF"
    if "/tiff negative/" in lowered or "/tiff_negative/" in lowered:
        return "negative TIFF"
    return "TIFF alternative"


def main() -> int:
    mount = source_mount()
    if not TMP.is_dir():
        raise PreparationError(f"approved temporary directory is absent: {TMP}")
    error_rows = [json.loads(line) for line in ERRORS.read_text(encoding="utf-8").splitlines()]
    if len(error_rows) != 109:
        raise PreparationError(f"remaining accession hold count drifted: {len(error_rows)}")
    error_by_inventory = {row["inventory"]: row for row in error_rows}
    if len(error_by_inventory) != len(error_rows):
        raise PreparationError("accession holds are not unique by inventory")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PreparationError("reconciliation SQLite quick_check failed")
        candidates = [
            dict(row)
            for row in db.execute(
                """
                SELECT c.inventory,c.candidate_id,c.record_key,c.source_path,c.basename,
                       c.size_bytes,c.mtime_ns,c.match_basis,o.md_object_id,o.title
                FROM candidates c JOIN objects o ON o.record_key=c.record_key
                WHERE c.source_path LIKE '/media/vladimir/Elements/restore/archive/SCANS-fertig/%'
                  AND lower(c.extension) IN ('.tif','.tiff')
                ORDER BY c.inventory,c.source_path
                """
            )
            if row["inventory"] in error_by_inventory
        ]
        selected = {
            row["record_key"]: (int(row["selected_candidate_id"]), str(row["source_path"]))
            for row in db.execute(
                """
                SELECT d.record_key,d.selected_candidate_id,c.source_path
                FROM reconciliation_decisions d JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.record_key IN (SELECT record_key FROM objects)
                """
            )
        }
    finally:
        db.close()

    by_inventory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    environment = dict(os.environ)
    environment.update({"MAGICK_TEMPORARY_PATH": str(TMP), "TMPDIR": str(TMP)})
    identified = []
    for row in candidates:
        path = inside_source(Path(row["source_path"]))
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
            raise PreparationError(f"candidate stat binding drifted: {path}")
        if row["match_basis"] != "exact_inventory_filename":
            raise PreparationError(f"candidate is not exact-inventory evidence: {path}")
        completed = subprocess.run(
            ["magick", "identify", "-quiet", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=120,
        )
        if completed.returncode == 0:
            identified.append(row)
            by_inventory[row["inventory"]].append(row)

    ambiguous = {inventory: rows for inventory, rows in by_inventory.items() if len(rows) > 1}
    if len(identified) != 157 or len(ambiguous) != 43:
        raise PreparationError(
            f"candidate scope drifted: identified={len(identified)} ambiguous={len(ambiguous)}"
        )

    validation_rows = []
    started = time.monotonic()
    total = sum(len(rows) for rows in ambiguous.values())
    completed_count = 0
    for inventory in sorted(ambiguous):
        for row in ambiguous[inventory]:
            path = Path(row["source_path"])
            completed = subprocess.run(
                ["magick", str(path), "-depth", "8", "RGBA:-"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=900,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode(errors="replace").strip().replace("\n", "; ")
                raise PreparationError(f"full decode failed after identify for {path}: {detail}")
            completed_count += 1
            if completed_count % 10 == 0 or completed_count == total:
                print(
                    f"validated {completed_count}/{total} elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
            validation_rows.append(
                {
                    "record_key": row["record_key"],
                    "md_object_id": row["md_object_id"],
                    "inventory": inventory,
                    "candidate_id": row["candidate_id"],
                    "source_path": row["source_path"],
                    "basename": row["basename"],
                    "size_bytes": row["size_bytes"],
                    "mtime_ns": row["mtime_ns"],
                    "variant_label": variant(row["source_path"]),
                    "decode_status": "full_decode_ok",
                    "validation_basis": "exact_inventory_filename_and_full_imagemagick_rgba_decode",
                    "deletion_authorized": "false",
                }
            )

    queue_rows = []
    validation_by_inventory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in validation_rows:
        validation_by_inventory[row["inventory"]].append(row)
    for inventory in sorted(validation_by_inventory):
        rows = validation_by_inventory[inventory]
        first = rows[0]
        failed_id, failed_path = selected[first["record_key"]]
        error = error_by_inventory[inventory]
        if (
            error["record_key"] != first["record_key"]
            or Path(failed_path).name not in error["error"]
        ):
            raise PreparationError(f"failed selected-master binding drifted: {inventory}")
        queue_rows.append(
            {
                "record_key": first["record_key"],
                "md_object_id": first["md_object_id"],
                "inventory": inventory,
                "title": next(row["title"] for row in candidates if row["inventory"] == inventory),
                "failed_candidate_id": failed_id,
                "failed_source_path": failed_path,
                "accession_error": error["error"],
                "valid_candidate_count": len(rows),
                "valid_candidate_ids": ";".join(str(row["candidate_id"]) for row in rows),
                "validation_basis": "exact inventory plus full decode; human visual choice required",
            }
        )

    atomic_bytes(VALIDATION, csv_bytes(VALIDATION_FIELDS, validation_rows))
    atomic_bytes(QUEUE, csv_bytes(QUEUE_FIELDS, queue_rows))
    receipt = {
        "format": "iss-tfa-master-recovery-workbench-v1",
        "prepared_at": now(),
        "result": "ok",
        "source_folder": "/media/vladimir/Elements/restore/archive/SCANS-fertig/",
        "remaining_accession_holds": 109,
        "indexed_exact_inventory_candidates": len(candidates),
        "full_decode_valid_candidates": 157,
        "ambiguous_objects": len(queue_rows),
        "ambiguous_valid_candidates": len(validation_rows),
        "single_valid_candidate_objects": 64,
        "no_valid_candidate_objects": 2,
        "queue_csv": str(QUEUE),
        "candidate_csv": str(VALIDATION),
        "fingerprints": {QUEUE.name: sha256(QUEUE), VALIDATION.name: sha256(VALIDATION)},
        "source_mount": mount,
        "source_read_only": True,
        "decisions_written": 0,
        "deletion_authorized": False,
    }
    atomic_json(RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
