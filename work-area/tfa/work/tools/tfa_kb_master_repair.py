#!/usr/bin/env python3
"""Repair omitted TFA-KB candidates and select unique curated TIFF Masters."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tfa_prepare import inventory_from_basename, normalize_inventory


REVIEW_ROOT = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation"
)
SOURCE_ROOT = Path("/media/vladimir/Elements")
MASTER_FOLDER = SOURCE_ROOT / "restore/archive/Alle-Tiff-Positive"
RESTORE_INVENTORY = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore/restore-inventory.csv"
)
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
CANDIDATES_CSV = REVIEW_ROOT / "candidates.csv"
SOURCE_DISPOSITIONS_CSV = REVIEW_ROOT / "source-disposition-proposal.csv"
DRAIN_DISPOSITIONS_CSV = REVIEW_ROOT / "drain-list-proposal.csv"
EXCEPTIONS_CSV = REVIEW_ROOT / "selected-master-exceptions.csv"
WORKBENCH_RECEIPT = REVIEW_ROOT / "workbench-preflight.json"
BACKUP_STAMP = "20260811T101631Z"
EXPECTED_EXCEPTION_COUNT = 152
EXPECTED_REPAIR_COUNT = 149
EXPECTED_UNCERTAIN = {"TFA-538075", "TFA-6826811", "TFA-6220899"}


class RepairError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise RepairError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_csv_temp(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> Path:
    temporary = path.with_name(f".{path.name}.tfa-kb-repair.tmp")
    if temporary.exists():
        raise RepairError(f"temporary output already exists: {temporary}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tfa-kb-repair.tmp")
    if temporary.exists():
        raise RepairError(f"temporary output already exists: {temporary}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def verify_boundaries() -> dict[str, str]:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "iss-archive-tfa-workbench.service"],
        capture_output=True,
        text=True,
    )
    if active.stdout.strip() == "active":
        raise RepairError("TFA workbench must be stopped before repair")
    mount = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(mount.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise RepairError("source mount did not resolve exactly once")
    row = filesystems[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise RepairError(f"source mount guard failed: {row}")
    required_backups = (
        REVIEW_ROOT / f"reconciliation.pre-tfa-kb-repair-{BACKUP_STAMP}.sqlite",
        REVIEW_ROOT / f"candidates.pre-tfa-kb-repair-{BACKUP_STAMP}.csv",
        REVIEW_ROOT / f"source-disposition-proposal.pre-tfa-kb-repair-{BACKUP_STAMP}.csv",
        REVIEW_ROOT / f"drain-list-proposal.pre-tfa-kb-repair-{BACKUP_STAMP}.csv",
        REVIEW_ROOT / f"workbench-decisions.pre-tfa-kb-repair-{BACKUP_STAMP}.csv",
        REVIEW_ROOT / f"workbench-candidate-dispositions.pre-tfa-kb-repair-{BACKUP_STAMP}.csv",
        REVIEW_ROOT / f"workbench-preflight.pre-tfa-kb-repair-{BACKUP_STAMP}.json",
    )
    missing = [str(path) for path in required_backups if not path.is_file()]
    if missing:
        raise RepairError(f"required pre-repair backups are absent: {missing}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def main() -> int:
    mount = verify_boundaries()
    repaired_at = utc_now()

    receipt = json.loads(WORKBENCH_RECEIPT.read_text(encoding="utf-8"))
    old_fingerprint = receipt["evidence_fingerprints"][CANDIDATES_CSV.name]
    backup_candidates = REVIEW_ROOT / f"candidates.pre-tfa-kb-repair-{BACKUP_STAMP}.csv"
    if sha256_file(CANDIDATES_CSV) != old_fingerprint:
        raise RepairError("current candidates do not match the recorded workbench fingerprint")
    if sha256_file(backup_candidates) != old_fingerprint:
        raise RepairError("pre-repair candidate backup does not match the recorded fingerprint")

    candidate_fields, candidates = read_csv(CANDIDATES_CSV)
    disposition_fields, source_dispositions = read_csv(SOURCE_DISPOSITIONS_CSV)
    drain_fields, drain_dispositions = read_csv(DRAIN_DISPOSITIONS_CSV)
    if disposition_fields != drain_fields or source_dispositions != drain_dispositions:
        raise RepairError("source and drain proposal projections disagree before repair")
    _, exceptions = read_csv(EXCEPTIONS_CSV)
    no_tiff = [row for row in exceptions if row["exception_type"] == "no_tiff_candidate"]
    if len(no_tiff) != EXPECTED_EXCEPTION_COUNT:
        raise RepairError(f"no-TIFF scope drifted: {len(no_tiff)}")

    _, restore_rows = read_csv(RESTORE_INVENTORY)
    restore_by_path = {row["source_path"]: row for row in restore_rows}
    source_by_inventory: dict[str, list[Path]] = {}
    for path in sorted(MASTER_FOLDER.iterdir()):
        if not path.is_file() or path.suffix.casefold() not in {".tif", ".tiff"}:
            continue
        inventory = inventory_from_basename(path.name)
        if inventory:
            source_by_inventory.setdefault(inventory, []).append(path)

    repair_scope: list[tuple[dict[str, str], Path]] = []
    uncertain: set[str] = set()
    for row in no_tiff:
        matches = source_by_inventory.get(normalize_inventory(row["inventory"]), [])
        if len(matches) == 1 and matches[0].name.upper().startswith("TFA-KB-"):
            repair_scope.append((row, matches[0]))
        else:
            uncertain.add(row["inventory"])
    if len(repair_scope) != EXPECTED_REPAIR_COUNT or uncertain != EXPECTED_UNCERTAIN:
        raise RepairError(
            f"repair scope drifted: repair={len(repair_scope)}, uncertain={sorted(uncertain)}"
        )

    candidate_paths = {row["source_path"] for row in candidates}
    disposition_paths = {row["source_path"] for row in source_dispositions}
    if any(str(path) in candidate_paths or str(path) in disposition_paths for _, path in repair_scope):
        raise RepairError("one or more repair paths already exist in the projection")

    with closing(sqlite3.connect(DATABASE, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RepairError("SQLite pre-repair quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RepairError("SQLite pre-repair foreign-key check failed")
        next_candidate_id = int(connection.execute("SELECT MAX(candidate_id) FROM candidates").fetchone()[0]) + 1
        new_candidates: list[dict[str, Any]] = []
        new_dispositions: list[dict[str, Any]] = []
        for offset, (exception, path) in enumerate(repair_scope):
            source_path = str(path)
            restore = restore_by_path.get(source_path)
            if restore is None:
                raise RepairError(f"source is absent from the fixed restore inventory: {source_path}")
            stat = path.stat()
            if stat.st_size <= 0 or stat.st_size != int(restore["size_bytes"]) or stat.st_mtime_ns != int(restore["mtime_ns"]):
                raise RepairError(f"source size/mtime guard failed: {source_path}")
            obj = connection.execute(
                "SELECT record_key, inventory FROM objects WHERE record_key=?",
                (exception["record_key"],),
            ).fetchone()
            if obj is None or normalize_inventory(obj["inventory"]) != inventory_from_basename(path.name):
                raise RepairError(f"object/source binding failed: {exception['record_key']} / {source_path}")
            current = connection.execute(
                "SELECT decision_state, selected_candidate_id FROM reconciliation_decisions WHERE record_key=?",
                (exception["record_key"],),
            ).fetchone()
            if current is not None and (current["decision_state"] != "unresolved" or current["selected_candidate_id"] is not None):
                raise RepairError(f"refusing to replace a non-placeholder decision: {exception['record_key']}")
            candidate_id = next_candidate_id + offset
            new_candidates.append(
                {
                    "candidate_id": candidate_id,
                    "record_key": exception["record_key"],
                    "inventory": exception["inventory"],
                    "source_path": source_path,
                    "source_kind": "read_only_usb",
                    "original_index_path": "",
                    "basename": path.name,
                    "extension": restore["extension"].casefold(),
                    "size_bytes": restore["size_bytes"],
                    "mtime_ns": restore["mtime_ns"],
                    "file_kind": restore["category"],
                    "proposed_role": "master_candidate",
                    "match_basis": "exact_inventory_filename; approved_TFA-KB_prefix_repair_2026-08-11",
                    "reused_source_sha256": "",
                    "prior_unit": "",
                    "prior_disposition": "",
                }
            )
            new_dispositions.append(
                {
                    "source_path": source_path,
                    "source_kind": "file",
                    "unit": "tfa",
                    "inventory": exception["inventory"],
                    "source_role": "master_candidate",
                    "final_path": "",
                    "preservation_status": "not_assessed",
                    "preservation_basis": "requires_human_master_selection",
                    "source_sha256": "",
                    "final_sha256": "",
                    "source_size_bytes": restore["size_bytes"],
                    "exact_copy_proven": "false",
                    "drain_state": "needs_reconciliation",
                    "deletion_authorized": "false",
                    "notes": "Proposal projection repaired for TFA-KB prefix; exact TFA inventory token",
                }
            )

        candidate_rows = candidates + new_candidates
        disposition_rows = sorted(source_dispositions + new_dispositions, key=lambda row: row["source_path"])
        candidate_tmp = write_csv_temp(CANDIDATES_CSV, candidate_fields, candidate_rows)
        source_tmp = write_csv_temp(SOURCE_DISPOSITIONS_CSV, disposition_fields, disposition_rows)
        drain_tmp = write_csv_temp(DRAIN_DISPOSITIONS_CSV, drain_fields, disposition_rows)

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                "INSERT INTO candidates VALUES (" + ",".join("?" for _ in candidate_fields) + ")",
                [[row[field] for field in candidate_fields] for row in new_candidates],
            )
            connection.executemany(
                "INSERT INTO source_disposition_proposal VALUES (" + ",".join("?" for _ in disposition_fields) + ")",
                [[row[field] for field in disposition_fields] for row in new_dispositions],
            )
            connection.executemany(
                "INSERT INTO drain_list_proposal VALUES (" + ",".join("?" for _ in disposition_fields) + ")",
                [[row[field] for field in disposition_fields] for row in new_dispositions],
            )
            note = (
                "Automatically selected under explicit user authorization: unique exact inventory match "
                "after correcting the TFA-KB filename-prefix omission in the curated read-only "
                "Alle-Tiff-Positive folder. Source not modified; deletion not authorized."
            )
            connection.executemany(
                """
                INSERT INTO reconciliation_decisions
                  (record_key, selected_candidate_id, decision_state, note, decided_by, decided_at)
                VALUES (?, ?, 'selected_master', ?, 'vladimir', ?)
                ON CONFLICT(record_key) DO UPDATE SET
                  selected_candidate_id=excluded.selected_candidate_id,
                  decision_state=excluded.decision_state,
                  note=excluded.note,
                  decided_by=excluded.decided_by,
                  decided_at=excluded.decided_at
                """,
                [(row["record_key"], row["candidate_id"], note, repaired_at) for row in new_candidates],
            )
            connection.executemany(
                "UPDATE objects SET decision_state='resolved' WHERE record_key=?",
                [(row["record_key"],) for row in new_candidates],
            )
            repaired_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM reconciliation_decisions d
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.record_key IN (SELECT record_key FROM candidates WHERE candidate_id>=?)
                  AND d.decision_state='selected_master'
                  AND d.record_key=c.record_key
                  AND lower(c.extension) IN ('.tif','.tiff')
                """,
                (next_candidate_id,),
            ).fetchone()[0]
            if repaired_count != EXPECTED_REPAIR_COUNT:
                raise RepairError(f"one-Master verification failed: {repaired_count}")
            connection.commit()
        except Exception:
            connection.rollback()
            for temporary in (candidate_tmp, source_tmp, drain_tmp):
                temporary.unlink(missing_ok=True)
            raise

    os.replace(candidate_tmp, CANDIDATES_CSV)
    os.replace(source_tmp, SOURCE_DISPOSITIONS_CSV)
    os.replace(drain_tmp, DRAIN_DISPOSITIONS_CSV)

    receipt["evidence_fingerprints"][CANDIDATES_CSV.name] = sha256_file(CANDIDATES_CSV)
    receipt.update(
        {
            "tfa_kb_prefix_repair_at": repaired_at,
            "tfa_kb_prefix_repair_added_candidates": EXPECTED_REPAIR_COUNT,
            "tfa_kb_prefix_repair_selected_masters": EXPECTED_REPAIR_COUNT,
            "tfa_kb_prefix_repair_uncertain_records": sorted(EXPECTED_UNCERTAIN),
            "tfa_kb_prefix_repair_previous_candidates_fingerprint": old_fingerprint,
            "tfa_kb_prefix_repair_user_authorized": True,
            "source_mount": mount,
            "source_read_only": True,
            "deletion_authorized": False,
        }
    )
    atomic_json(WORKBENCH_RECEIPT, receipt)
    print(
        json.dumps(
            {
                "result": "ok",
                "repaired_at": repaired_at,
                "candidates_added": EXPECTED_REPAIR_COUNT,
                "masters_selected": EXPECTED_REPAIR_COUNT,
                "uncertain": sorted(EXPECTED_UNCERTAIN),
                "media_hashed": False,
                "media_copied": False,
                "source_modified": False,
                "deletion_authorized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
