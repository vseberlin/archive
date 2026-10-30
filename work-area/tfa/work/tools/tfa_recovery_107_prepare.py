#!/usr/bin/env python3
"""Prepare the exact 107-object SCANS-fertig TFA recovery projection."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


WORK = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW = WORK / "work/reconciliation/unit-reconciliation"
RUN = WORK / "work/accession-runs/tfa-md-001"
DB = REVIEW / "reconciliation.sqlite"
ERRORS = RUN / "errors.jsonl"
OBJECTS = REVIEW / "master-recovery-107-objects.csv"
CANDIDATES = REVIEW / "master-recovery-107-candidates.csv"
INVALID = REVIEW / "master-recovery-107-invalid-candidates.csv"
RECEIPT = REVIEW / "master-recovery-107-preparation.json"
SOURCE_ROOT = Path("/media/vladimir/Elements")
TMP = RUN / "tmp"
OBJECT_FIELDS = (
    "record_key", "md_object_id", "inventory", "title", "failed_candidate_id",
    "failed_source_path", "accession_error", "valid_candidate_count",
    "valid_candidate_ids", "recovery_class", "selection_basis",
)
CANDIDATE_FIELDS = (
    "record_key", "md_object_id", "inventory", "candidate_id", "source_path",
    "basename", "size_bytes", "mtime_ns", "variant_label", "decode_status",
    "pixel_digest_8bit_rgba", "selection_role", "validation_basis",
    "deletion_authorized",
)
INVALID_FIELDS = (
    "record_key", "md_object_id", "inventory", "candidate_id", "source_path",
    "basename", "size_bytes", "mtime_ns", "validation_stage", "error",
    "deletion_authorized",
)


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
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


def csv_bytes(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, quoting=csv.QUOTE_ALL)
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode()


def atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def decode_digest(path: Path, environment: dict[str, str]) -> tuple[str, str]:
    process = subprocess.Popen(
        ["magick", str(path), "-depth", "8", "RGBA:-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
        digest.update(block)
    detail = process.stderr.read().decode(errors="replace").strip().replace("\n", "; ")
    code = process.wait()
    if code:
        return "", detail or f"magick exited {code}"
    return "sha256:" + digest.hexdigest(), ""


def main() -> int:
    mount = source_mount()
    if not TMP.is_dir():
        raise PreparationError(f"approved temporary directory is absent: {TMP}")
    error_rows = [json.loads(line) for line in ERRORS.read_text(encoding="utf-8").splitlines()]
    if len(error_rows) != 109 or any(row.get("critical") is not False for row in error_rows):
        raise PreparationError("expected 109 noncritical accession holds")
    error_by_inventory = {row["inventory"]: row for row in error_rows}
    if len(error_by_inventory) != 109:
        raise PreparationError("accession holds are not unique by inventory")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PreparationError("reconciliation SQLite quick_check failed")
        indexed = [
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
        decisions = {
            row["record_key"]: dict(row)
            for row in db.execute(
                """
                SELECT d.record_key,d.selected_candidate_id,d.decision_state,c.source_path
                FROM reconciliation_decisions d
                LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                """
            )
        }
        object_meta = {
            row["record_key"]: dict(row)
            for row in db.execute(
                "SELECT record_key,md_object_id,inventory,title FROM objects"
            )
        }
    finally:
        db.close()
    if len(indexed) != 175:
        raise PreparationError(f"indexed recovery candidate count drifted: {len(indexed)}")

    environment = dict(os.environ)
    environment.update({"MAGICK_TEMPORARY_PATH": str(TMP), "TMPDIR": str(TMP)})
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for index, row in enumerate(indexed, 1):
        path = inside_source(Path(row["source_path"]))
        stat = path.stat()
        if (
            stat.st_size != int(row["size_bytes"])
            or stat.st_mtime_ns != int(row["mtime_ns"])
            or row["match_basis"] != "exact_inventory_filename"
        ):
            raise PreparationError(f"candidate binding drifted: {path}")
        identified = subprocess.run(
            ["magick", "identify", "-quiet", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment, timeout=180,
        )
        if identified.returncode:
            invalid_rows.append({
                **{field: row[field] for field in ("record_key", "md_object_id", "inventory", "candidate_id", "source_path", "basename", "size_bytes", "mtime_ns")},
                "validation_stage": "identify",
                "error": identified.stderr.decode(errors="replace").strip().replace("\n", "; "),
                "deletion_authorized": "false",
            })
        else:
            pixel_digest, error = decode_digest(path, environment)
            if error:
                invalid_rows.append({
                    **{field: row[field] for field in ("record_key", "md_object_id", "inventory", "candidate_id", "source_path", "basename", "size_bytes", "mtime_ns")},
                    "validation_stage": "full_decode",
                    "error": error,
                    "deletion_authorized": "false",
                })
            else:
                valid_rows.append({
                    **{field: row[field] for field in ("record_key", "md_object_id", "inventory", "candidate_id", "source_path", "basename", "size_bytes", "mtime_ns")},
                    "variant_label": variant(row["source_path"]),
                    "decode_status": "full_decode_ok",
                    "pixel_digest_8bit_rgba": pixel_digest,
                    "selection_role": "",
                    "validation_basis": "exact_inventory_filename_and_full_imagemagick_rgba_decode",
                    "deletion_authorized": "false",
                })
        if index % 10 == 0 or index == len(indexed):
            print(
                f"validated {index}/{len(indexed)} valid={len(valid_rows)} invalid={len(invalid_rows)} elapsed={time.monotonic()-started:.1f}s",
                flush=True,
            )

    by_inventory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        by_inventory[row["inventory"]].append(row)
    distribution = Counter(len(by_inventory.get(inventory, ())) for inventory in error_by_inventory)
    if len(valid_rows) != 157 or len(invalid_rows) != 18 or distribution != {0: 2, 1: 64, 2: 36, 3: 7}:
        raise PreparationError(
            f"validated recovery scope drifted: valid={len(valid_rows)} invalid={len(invalid_rows)} distribution={dict(distribution)}"
        )

    object_rows = []
    target_count = 0
    for inventory in sorted(error_by_inventory):
        rows = by_inventory.get(inventory, [])
        error = error_by_inventory[inventory]
        record_key = error["record_key"]
        if any(row["record_key"] != record_key for row in rows):
            raise PreparationError(f"candidate object binding drifted: {inventory}")
        decision = decisions[record_key]
        if len(rows) == 0:
            recovery_class = "no_valid_candidate"
            selection_basis = "hold_for_master_search"
        elif len(rows) == 1:
            rows[0]["selection_role"] = "selected_sole_valid_candidate"
            recovery_class = "sole_valid_candidate"
            selection_basis = "user_approved_sole_full_decode_exact_inventory_candidate"
            target_count += 1
        else:
            selected = next(
                (row for row in rows if int(row["candidate_id"]) == int(decision["selected_candidate_id"] or 0)),
                None,
            )
            if (
                decision["decision_state"] != "selected_master"
                or selected is None
                or "positive" not in selected["variant_label"].casefold()
            ):
                raise PreparationError(f"ambiguous object lacks confirmed positive selection: {inventory}")
            selected["selection_role"] = "selected_confirmed_positive"
            recovery_class = "confirmed_positive_from_visual_queue"
            selection_basis = "user_confirmed_positive_full_decode_candidate"
            target_count += 1
        failed_path = str(decision.get("source_path") or "")
        object_rows.append({
            "record_key": record_key,
            "md_object_id": object_meta[record_key]["md_object_id"],
            "inventory": inventory,
            "title": object_meta[record_key]["title"],
            "failed_candidate_id": decision.get("selected_candidate_id") or "",
            "failed_source_path": failed_path,
            "accession_error": error["error"],
            "valid_candidate_count": len(rows),
            "valid_candidate_ids": ";".join(str(row["candidate_id"]) for row in rows),
            "recovery_class": recovery_class,
            "selection_basis": selection_basis,
        })
    if target_count != 107:
        raise PreparationError(f"recovery target count drifted: {target_count}")

    atomic_bytes(OBJECTS, csv_bytes(OBJECT_FIELDS, object_rows))
    atomic_bytes(CANDIDATES, csv_bytes(CANDIDATE_FIELDS, valid_rows))
    atomic_bytes(INVALID, csv_bytes(INVALID_FIELDS, invalid_rows))
    receipt = {
        "format": "iss-tfa-recovery-107-preparation-v1",
        "prepared_at": now(),
        "result": "ok",
        "source_folder": "/media/vladimir/Elements/restore/archive/SCANS-fertig/",
        "remaining_accession_holds": 109,
        "indexed_exact_inventory_candidates": len(indexed),
        "full_decode_valid_candidates": len(valid_rows),
        "invalid_candidates": len(invalid_rows),
        "sole_valid_candidate_objects": distribution[1],
        "confirmed_positive_objects": distribution[2] + distribution[3],
        "recovery_target_objects": target_count,
        "no_valid_candidate_objects": distribution[0],
        "fingerprints": {
            path.name: sha256(path) for path in (OBJECTS, CANDIDATES, INVALID)
        },
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
