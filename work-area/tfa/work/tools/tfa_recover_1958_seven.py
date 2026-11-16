#!/usr/bin/env python3
"""Recover and accession seven TFA 1958 masters from validated source copies."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import tfa_accession as core


DB = core.REVIEW / "reconciliation.sqlite"
PREPARED = core.RUN / "seven-recovery-prepared.json"
RESULT = core.RUN / "seven-recovery-completion.json"
TARGETS = {
    "TFA-5815133": ("md:60199", "60199", "5815133", "30141", "30143"),
    "TFA-5815134": ("md:60200", "60200", "5815134", "30153", "30155"),
    "TFA-5815136": ("md:60201", "60201", "5815136", "30165", "30167"),
    "TFA-5815139": ("md:60202", "60202", "5815139", "30178", "30180"),
    "TFA-5815140": ("md:60203", "60203", "5815140", "30190", "30192"),
    "TFA-5815141": ("md:60204", "60204", "5815141", "30202", "30204"),
    "TFA-5815142": ("md:60205", "60205", "5815142", "30214", "30216"),
}
NOTE = (
    "User-approved replacement after accession hold; exact inventory TIFF in /restore/archive/1958 "
    "passed ExifTool validation and full pixel decoding; corrupted Alle-Tiff-Positive candidate rejected."
)


def paths(inventory: str) -> tuple[Path, Path]:
    number = TARGETS[inventory][2]
    return (
        Path(f"/media/vladimir/Elements/restore/archive/1958/TFA-FOT-{number}.tif"),
        Path(f"/media/vladimir/Elements/restore/archive/Alle-Tiff-Positive/TFA-FOT-{number}.tif"),
    )


def service_inactive(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() not in {"inactive", "failed"}:
        raise core.IntegrityError(f"service must be inactive for bounded recovery: {unit}")


def backup_file(path: Path, prefix: str, label: str) -> str:
    destination = core.RUN / f"{prefix}-{label}"
    if destination.exists():
        raise core.IntegrityError(f"backup collision: {destination}")
    shutil.copy2(path, destination)
    core.fsync_file(destination)
    core.fsync_dir(destination.parent)
    return str(destination)


def backup_sqlite(path: Path, prefix: str, label: str) -> str:
    destination = core.RUN / f"{prefix}-{label}"
    if destination.exists():
        raise core.IntegrityError(f"backup collision: {destination}")
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError(f"SQLite backup check failed: {destination}")
    finally:
        target.close()
        source.close()
    core.fsync_file(destination)
    core.fsync_dir(destination.parent)
    return str(destination)


def backup_current_state(prefix: str) -> dict[str, str]:
    files = {
        "md-accession-catalog.csv": core.CATALOG_SOURCE,
        "md-accession-preparation.json": core.PREPARATION,
        "workbench-decisions.csv": core.DECISIONS,
        "source-disposition-proposal.csv": core.SOURCE_DISPOSITION,
        "drain-list-proposal.csv": core.DRAIN,
        "errors.jsonl": core.ERRORS,
        "completion.json": core.COMPLETION,
        "publication.json": core.PUBLICATION,
        "status.json": core.STATUS,
        "unit-accession-completion.json": core.UNIT / "db-restore/accession-completion.json",
        "unit-accession-state.json": core.UNIT / "metadata/accession-state.json",
    }
    backups = {
        label: backup_file(path, prefix, label)
        for label, path in files.items()
        if path.is_file()
    }
    backups["reconciliation.sqlite"] = backup_sqlite(DB, prefix, "reconciliation.sqlite")
    backups["RESTORE.sqlite"] = backup_sqlite(
        core.UNIT / "db-restore/RESTORE.sqlite", prefix, "RESTORE.sqlite"
    )
    return backups


def validate_sources(
    catalog_by_inventory: dict[str, dict[str, str]],
    error_by_inventory: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence = []
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("reconciliation SQLite quick_check failed")
        for inventory, (record_key, md_id, _number, new_id, old_id) in TARGETS.items():
            candidate, rejected = paths(inventory)
            candidate = core.inside(candidate, core.SOURCE_ROOT, exists=True)
            rejected = core.inside(rejected, core.SOURCE_ROOT, exists=True)
            row = catalog_by_inventory[inventory]
            if row["record_key"] != record_key or row["md_object_id"] != md_id:
                raise core.IntegrityError(f"catalogue binding drifted: {inventory}")
            if candidate.stat().st_size != int(row["selected_master_size_bytes"]):
                raise core.IntegrityError(f"replacement size drifted: {candidate}")
            candidate_row = db.execute(
                "SELECT candidate_id,record_key,inventory,size_bytes,match_basis FROM candidates WHERE source_path=?",
                (str(candidate),),
            ).fetchone()
            expected = (int(new_id), record_key, inventory, candidate.stat().st_size, "exact_inventory_filename")
            if candidate_row != expected:
                raise core.IntegrityError(
                    f"replacement candidate binding drifted: {candidate_row!r} != {expected!r}"
                )
            rejected_row = db.execute(
                "SELECT candidate_id,record_key,inventory FROM candidates WHERE source_path=?",
                (str(rejected),),
            ).fetchone()
            if rejected_row != (int(old_id), record_key, inventory):
                raise core.IntegrityError(f"rejected candidate binding drifted: {inventory}")
            if row["selected_master_path"] == str(rejected):
                if inventory not in error_by_inventory:
                    raise core.IntegrityError(f"original accession hold missing: {inventory}")
            elif row["selected_master_path"] != str(candidate):
                raise core.IntegrityError(f"unexpected selected path: {inventory}")
            technical = core.exif_json(candidate, core.TECHNICAL_TAGS, numeric=True)
            depth = core.scalar_depth(technical)
            pixel_digest = core.pixel_digest(candidate, depth)
            evidence.append(
                {
                    "inventory": inventory,
                    "record_key": record_key,
                    "candidate_id": int(new_id),
                    "replacement_path": str(candidate),
                    "rejected_path": str(rejected),
                    "size_bytes": candidate.stat().st_size,
                    "pixel_digest": pixel_digest,
                    "full_decode": "ok",
                }
            )
    finally:
        db.close()
    return evidence


def replace_decisions(
    catalog_fields: list[str],
    catalog_rows: list[dict[str, str]],
    error_rows: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    decided_at = core.now()
    prefix = "pre-seven-recovery-" + decided_at.replace(":", "").replace("-", "")
    backups = backup_current_state(prefix)

    catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
    for inventory, (_record_key, _md_id, _number, new_id, _old_id) in TARGETS.items():
        candidate, _rejected = paths(inventory)
        row = catalog_by_inventory[inventory]
        row.update(
            {
                "selected_candidate_id": new_id,
                "selected_master_path": str(candidate),
                "selected_master_size_bytes": str(candidate.stat().st_size),
                "master_decision_state": "selected_master",
                "master_decided_by": "vladimir",
                "master_decided_at": decided_at,
                "master_decision_note": NOTE,
                "provenance_chain": f"museum-digital {row['record_key']} | read-only recovery source {candidate}",
                "accession_state": "ready_for_accession",
                "accession_hold_reason": "",
            }
        )

    decision_fields, decision_rows = core.read_csv(core.DECISIONS)
    decision_by_inventory = {row["inventory"]: row for row in decision_rows}
    for inventory, (_record_key, _md_id, _number, new_id, _old_id) in TARGETS.items():
        candidate, _rejected = paths(inventory)
        decision_by_inventory[inventory].update(
            {
                "decision_state": "selected_master",
                "selected_candidate_id": new_id,
                "selected_source_path": str(candidate),
                "note": NOTE,
                "decided_by": "vladimir",
                "decided_at": decided_at,
            }
        )

    source_fields, source_rows = core.read_csv(core.SOURCE_DISPOSITION)
    source_by_path = {row["source_path"]: row for row in source_rows}
    old_drain_rows = []
    selected_paths = set()
    for inventory in TARGETS:
        candidate, rejected = paths(inventory)
        selected_paths.add(str(candidate))
        selected_row = source_by_path[str(candidate)]
        selected_row.update(
            {
                "source_role": "selected_master",
                "final_path": f"masters/{inventory}.tif",
                "preservation_status": "selected_for_accession",
                "preservation_basis": "user_selected_valid_recovery_master_from_1958",
                "source_sha256": "",
                "final_sha256": "",
                "exact_copy_proven": "false",
                "drain_state": "retain_selected_master",
                "deletion_authorized": "false",
                "notes": NOTE,
            }
        )
        rejected_row = source_by_path[str(rejected)]
        rejected_row.update(
            {
                "source_role": "rejected_master_candidate",
                "final_path": "",
                "preservation_status": "redundant",
                "preservation_basis": "invalid_tiff_replaced_by_valid_inventory_match",
                "source_sha256": "",
                "final_sha256": "",
                "exact_copy_proven": "false",
                "drain_state": "proposed_redundant",
                "deletion_authorized": "false",
                "notes": (
                    "Rejected TIFF failed accession parsing or decoding; valid exact-inventory /1958 "
                    "replacement selected by user; source retained unchanged; deletion not authorized."
                ),
            }
        )
        old_drain_rows.append(dict(rejected_row))

    drain_fields, drain_rows = core.read_csv(core.DRAIN)
    drain_rows = [row for row in drain_rows if row["source_path"] not in selected_paths]
    drain_rows.extend(old_drain_rows)
    if len(drain_rows) != core.EXPECTED_DRAIN:
        raise core.IntegrityError(f"drain swap changed row count: {len(drain_rows)}")
    if len({row["source_path"] for row in drain_rows}) != len(drain_rows):
        raise core.IntegrityError("drain swap created duplicate paths")

    remaining_errors = [row for row in error_rows if row["inventory"] not in TARGETS]
    if len(remaining_errors) != 109:
        raise core.IntegrityError(f"unexpected remaining error count: {len(remaining_errors)}")

    core.atomic_bytes(core.CATALOG_SOURCE, core.csv_bytes(catalog_fields, catalog_rows))
    core.atomic_bytes(core.DECISIONS, core.csv_bytes(decision_fields, decision_rows))
    core.atomic_bytes(core.SOURCE_DISPOSITION, core.csv_bytes(source_fields, source_rows))
    core.atomic_bytes(core.DRAIN, core.csv_bytes(drain_fields, drain_rows))
    core.atomic_bytes(
        core.ERRORS,
        b"".join(
            (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
            for row in remaining_errors
        ),
    )

    db = sqlite3.connect(DB)
    try:
        db.execute("BEGIN IMMEDIATE")
        for inventory, (record_key, _md_id, _number, new_id, _old_id) in TARGETS.items():
            changed = db.execute(
                "UPDATE reconciliation_decisions SET selected_candidate_id=?,decision_state='selected_master',note=?,decided_by='vladimir',decided_at=? WHERE record_key=?",
                (new_id, NOTE, decided_at, record_key),
            ).rowcount
            if changed != 1:
                raise core.IntegrityError(f"SQLite decision update failed: {inventory}")
        db.execute("DELETE FROM source_disposition_proposal")
        db.executemany(
            "INSERT INTO source_disposition_proposal VALUES (" + ",".join("?" for _ in source_fields) + ")",
            [[row[field] for field in source_fields] for row in source_rows],
        )
        db.execute("DELETE FROM drain_list_proposal")
        db.executemany(
            "INSERT INTO drain_list_proposal VALUES (" + ",".join("?" for _ in drain_fields) + ")",
            [[row[field] for field in drain_fields] for row in drain_rows],
        )
        db.commit()
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("reconciliation SQLite check failed after replacement")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    preparation = json.loads(core.PREPARATION.read_text(encoding="utf-8"))
    preparation["prepared_at"] = decided_at
    preparation["fingerprints"][core.CATALOG_SOURCE.name] = core.sha256(core.CATALOG_SOURCE)
    preparation["sqlite_backup"] = backups["reconciliation.sqlite"]
    core.atomic_json(core.PREPARATION, preparation)

    for path in (
        core.COMPLETION,
        core.PUBLICATION,
        core.UNIT / "db-restore/accession-completion.json",
    ):
        path.unlink(missing_ok=True)
    core.atomic_json(
        core.UNIT / "metadata/accession-state.json",
        {
            "format": "iss-tfa-accession-state-v1",
            "updated_at": core.now(),
            "run": str(core.RUN),
            "state": "running_bounded_seven_recovery",
        },
    )
    payload = {
        "format": "iss-tfa-seven-recovery-preparation-v1",
        "prepared_at": decided_at,
        "result": "ok",
        "inventories": list(TARGETS),
        "replacement_count": len(TARGETS),
        "remaining_noncritical_holds": len(remaining_errors),
        "evidence": evidence,
        "backups": backups,
        "source_read_only": True,
        "deletion_authorized": False,
    }
    core.atomic_json(PREPARED, payload)
    return payload


def main() -> int:
    service_inactive("iss-tfa-accession-worker.service")
    service_inactive("iss-tfa-accession-shutdown-watchdog.service")
    core.source_mount()
    core.RUN.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(core.LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise core.IntegrityError("another TFA accession process holds the run lock") from error

        catalog_fields, catalog_rows, _preparation = core.load_scope()
        catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
        error_rows = [json.loads(line) for line in core.ERRORS.read_text(encoding="utf-8").splitlines()]
        error_by_inventory = {row["inventory"]: row for row in error_rows}
        evidence = validate_sources(catalog_by_inventory, error_by_inventory)
        states = {catalog_by_inventory[inventory]["selected_master_path"] == str(paths(inventory)[0]) for inventory in TARGETS}
        if len(states) != 1:
            raise core.IntegrityError("seven recovery decisions are in a mixed state")
        if states == {False}:
            prepared = replace_decisions(catalog_fields, catalog_rows, error_rows, evidence)
        else:
            if not PREPARED.is_file():
                raise core.IntegrityError("prepared decisions lack their recovery receipt")
            prepared = json.loads(PREPARED.read_text(encoding="utf-8"))

        preflight = core.preflight(allow_unit=True)
        catalog_fields, catalog_rows, _ = core.load_scope()
        catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
        receipts = core.load_receipts()
        for inventory in TARGETS:
            if inventory in receipts:
                if receipts[inventory]["source_path"] != str(paths(inventory)[0]):
                    raise core.IntegrityError(f"recovery receipt source mismatch: {inventory}")
                continue
            receipt = core.process_one(catalog_by_inventory[inventory], publish=True)
            if receipt["source_path"] != str(paths(inventory)[0]):
                raise core.IntegrityError(f"published recovery source mismatch: {inventory}")
            receipts[inventory] = receipt

        completion = core.finalize(catalog_fields, catalog_rows)
        if (
            completion["objects_accessioned"] != 5172
            or completion["noncritical_error_holds"] != 109
            or completion["representation_rows"] != 10344
            or completion["checksum_rows"] != 10344
            or completion["result"] != "ok"
        ):
            raise core.IntegrityError(f"unexpected recovery completion: {completion}")
        core.atomic_json(core.STATUS, {**completion, "state": "complete"})
        result = {
            "format": "iss-tfa-seven-recovery-completion-v1",
            "completed_at": core.now(),
            "result": "ok",
            "inventories": list(TARGETS),
            "objects_accessioned": 7,
            "total_objects_accessioned": completion["objects_accessioned"],
            "remaining_noncritical_holds": completion["noncritical_error_holds"],
            "source_read_only": True,
            "source_modified": False,
            "deletion_authorized": False,
            "preflight": preflight,
            "preparation": str(PREPARED),
            "backups": prepared["backups"],
            "completion": str(core.COMPLETION),
        }
        core.atomic_json(RESULT, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except core.IntegrityError as error:
        core.atomic_json(
            core.CRITICAL,
            {
                "failed_at": core.now(),
                "state": "stopped_integrity",
                "operation": "bounded_seven_recovery",
                "error": str(error),
                "data_integrity_suspected": True,
            },
        )
        raise
