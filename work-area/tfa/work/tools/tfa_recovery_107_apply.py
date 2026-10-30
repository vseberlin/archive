#!/usr/bin/env python3
"""Apply the approved 107-object TFA recovery selection to accession inputs."""

from __future__ import annotations

import csv
import fcntl
import json
import os
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import tfa_accession as core
import tfa_recover_1958_seven as prior_recovery


DB = core.REVIEW / "reconciliation.sqlite"
OBJECTS = core.REVIEW / "master-recovery-107-objects.csv"
CANDIDATES = core.REVIEW / "master-recovery-107-candidates.csv"
INVALID = core.REVIEW / "master-recovery-107-invalid-candidates.csv"
PREPARATION = core.REVIEW / "master-recovery-107-preparation.json"
APPLIED = core.RUN / "recovery-107-applied.json"
EXPECTED_DRAIN_BEFORE = 41702
EXPECTED_DRAIN_AFTER = 41720
NOTE_SOLE = (
    "User-approved 107-object recovery batch: sole exact-inventory SCANS-fertig TIFF "
    "passed full decode and was selected as Master; failed prior candidate rejected."
)
NOTE_POSITIVE = (
    "User-confirmed positive TIFF selected as Master in the visual recovery queue; "
    "candidate passed full decode; failed prior candidate and all alternatives are redundant evidence."
)


def service_inactive(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"],
        check=True, capture_output=True, text=True,
    )
    if result.stdout.strip() not in {"inactive", "failed"}:
        raise core.IntegrityError(f"service must be inactive for bounded recovery: {unit}")


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_evidence() -> tuple[list[dict[str, str]], dict[str, dict[str, str]], dict[str, Any]]:
    receipt = json.loads(PREPARATION.read_text(encoding="utf-8"))
    if (
        receipt.get("result") != "ok"
        or int(receipt.get("recovery_target_objects", -1)) != 107
        or int(receipt.get("sole_valid_candidate_objects", -1)) != 64
        or int(receipt.get("confirmed_positive_objects", -1)) != 43
        or int(receipt.get("no_valid_candidate_objects", -1)) != 2
        or int(receipt.get("full_decode_valid_candidates", -1)) != 157
        or int(receipt.get("invalid_candidates", -1)) != 18
        or receipt.get("source_read_only") is not True
        or receipt.get("deletion_authorized") is not False
    ):
        raise core.IntegrityError("107-object recovery preparation is incomplete")
    for path in (OBJECTS, CANDIDATES, INVALID):
        if receipt.get("fingerprints", {}).get(path.name) != core.sha256(path):
            raise core.IntegrityError(f"107-object recovery evidence changed: {path.name}")
    object_rows = read_rows(OBJECTS)
    candidate_rows = read_rows(CANDIDATES)
    selected = [row for row in candidate_rows if row["selection_role"]]
    if (
        len(object_rows) != 109
        or len(candidate_rows) != 157
        or len(read_rows(INVALID)) != 18
        or len(selected) != 107
        or Counter(row["selection_role"] for row in selected)
        != {"selected_sole_valid_candidate": 64, "selected_confirmed_positive": 43}
    ):
        raise core.IntegrityError("107-object recovery evidence row counts drifted")
    selected_by_inventory = {row["inventory"]: row for row in selected}
    if len(selected_by_inventory) != 107:
        raise core.IntegrityError("recovery selection is not one Master per object")
    object_by_inventory = {row["inventory"]: row for row in object_rows}
    for inventory, selected_row in selected_by_inventory.items():
        obj = object_by_inventory.get(inventory)
        if obj is None or obj["record_key"] != selected_row["record_key"]:
            raise core.IntegrityError(f"recovery object binding failed: {inventory}")
        if selected_row["decode_status"] != "full_decode_ok" or selected_row["deletion_authorized"] != "false":
            raise core.IntegrityError(f"recovery candidate validation failed: {inventory}")
        if (
            selected_row["selection_role"] == "selected_confirmed_positive"
            and "positive" not in selected_row["variant_label"].casefold()
        ):
            raise core.IntegrityError(f"confirmed visual selection is not positive: {inventory}")
        path = core.inside(Path(selected_row["source_path"]), core.SOURCE_ROOT, exists=True)
        stat = path.stat()
        if stat.st_size != int(selected_row["size_bytes"]) or stat.st_mtime_ns != int(selected_row["mtime_ns"]):
            raise core.IntegrityError(f"selected recovery candidate changed: {path}")
    return selected, object_by_inventory, receipt


def apply() -> dict[str, Any]:
    if core.EXPECTED_DRAIN != EXPECTED_DRAIN_AFTER:
        raise core.IntegrityError("accession drain guard is not set for the recovery delta")
    mount = core.source_mount()
    selected, object_by_inventory, evidence_receipt = verify_evidence()
    selected_by_inventory = {row["inventory"]: row for row in selected}
    catalog_fields, catalog_rows, _ = core.load_scope()
    catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
    decision_fields, decision_rows = core.read_csv(core.DECISIONS)
    decision_by_inventory = {row["inventory"]: row for row in decision_rows}
    source_fields, source_rows = core.read_csv(core.SOURCE_DISPOSITION)
    source_by_path = {row["source_path"]: row for row in source_rows}
    drain_fields, drain_rows = core.read_csv(core.DRAIN)
    if len(drain_rows) != EXPECTED_DRAIN_BEFORE:
        raise core.IntegrityError(f"pre-recovery drain count drifted: {len(drain_rows)}")
    drain_by_path = {row["source_path"]: row for row in drain_rows}
    if len(drain_by_path) != len(drain_rows):
        raise core.IntegrityError("pre-recovery drain paths are not unique")
    error_rows = [json.loads(line) for line in core.ERRORS.read_text(encoding="utf-8").splitlines()]
    error_by_inventory = {row["inventory"]: row for row in error_rows}
    if len(error_rows) != 109 or len(error_by_inventory) != 109:
        raise core.IntegrityError("expected 109 unique noncritical holds before recovery")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("reconciliation SQLite pre-write check failed")
        for inventory, selected_row in selected_by_inventory.items():
            candidate = db.execute(
                "SELECT candidate_id,record_key,inventory,source_path,size_bytes,mtime_ns,extension FROM candidates WHERE candidate_id=?",
                (int(selected_row["candidate_id"]),),
            ).fetchone()
            if (
                candidate is None
                or candidate["record_key"] != selected_row["record_key"]
                or candidate["inventory"] != inventory
                or candidate["source_path"] != selected_row["source_path"]
                or int(candidate["size_bytes"]) != int(selected_row["size_bytes"])
                or int(candidate["mtime_ns"]) != int(selected_row["mtime_ns"])
                or str(candidate["extension"]).casefold() not in {".tif", ".tiff"}
            ):
                raise core.IntegrityError(f"recovery candidate DB binding failed: {inventory}")
    finally:
        db.close()

    decided_at = core.now()
    prefix = "pre-recovery-107-" + decided_at.replace(":", "").replace("-", "")
    backups = prior_recovery.backup_current_state(prefix)

    selected_paths: set[str] = set()
    rejected_paths: set[str] = set()
    drain_removed = 0
    drain_added = 0
    for inventory, selected_row in selected_by_inventory.items():
        catalog = catalog_by_inventory.get(inventory)
        decision = decision_by_inventory.get(inventory)
        error = error_by_inventory.get(inventory)
        obj = object_by_inventory[inventory]
        if catalog is None or decision is None or error is None:
            raise core.IntegrityError(f"recovery catalogue/decision/error binding absent: {inventory}")
        if (
            catalog["record_key"] != selected_row["record_key"]
            or decision["record_key"] != selected_row["record_key"]
            or error["record_key"] != selected_row["record_key"]
        ):
            raise core.IntegrityError(f"recovery record binding drifted: {inventory}")
        old_path = catalog["selected_master_path"]
        new_path = selected_row["source_path"]
        if old_path == new_path:
            raise core.IntegrityError(f"recovery delta unexpectedly reuses failed path: {inventory}")
        if old_path not in source_by_path or new_path not in source_by_path:
            raise core.IntegrityError(f"recovery source disposition path absent: {inventory}")
        if new_path in selected_paths or old_path in rejected_paths:
            raise core.IntegrityError("recovery source paths are not unique")
        selected_paths.add(new_path); rejected_paths.add(old_path)
        note = NOTE_SOLE if selected_row["selection_role"] == "selected_sole_valid_candidate" else NOTE_POSITIVE
        catalog.update({
            "selected_candidate_id": selected_row["candidate_id"],
            "selected_master_path": new_path,
            "selected_master_size_bytes": selected_row["size_bytes"],
            "master_decision_state": "selected_master",
            "master_decided_by": "vladimir",
            "master_decided_at": decided_at,
            "master_decision_note": note,
            "provenance_chain": f"museum-digital {catalog['record_key']} | read-only recovery source {new_path}",
            "accession_state": "ready_for_accession",
            "accession_hold_reason": "",
        })
        decision.update({
            "decision_state": "selected_master",
            "selected_candidate_id": selected_row["candidate_id"],
            "selected_source_path": new_path,
            "note": note,
            "decided_by": "vladimir",
            "decided_at": decided_at,
        })
        selected_disposition = source_by_path[new_path]
        selected_disposition.update({
            "source_role": "selected_master",
            "final_path": f"masters/{inventory}.tif",
            "preservation_status": "selected_for_accession",
            "preservation_basis": obj["selection_basis"],
            "source_sha256": "",
            "final_sha256": "",
            "exact_copy_proven": "false",
            "drain_state": "retain_selected_master",
            "deletion_authorized": "false",
            "notes": note,
        })
        rejected = source_by_path[old_path]
        rejected.update({
            "source_role": "rejected_master_candidate",
            "final_path": "",
            "preservation_status": "redundant",
            "preservation_basis": "invalid_tiff_replaced_by_valid_inventory_match",
            "source_sha256": "",
            "final_sha256": "",
            "exact_copy_proven": "false",
            "drain_state": "proposed_redundant",
            "deletion_authorized": "false",
            "notes": "Failed TIFF replaced by the user-approved full-decode exact-inventory Master; source retained unchanged; deletion not authorized.",
        })
        if new_path in drain_by_path:
            del drain_by_path[new_path]
            drain_removed += 1
        if old_path not in drain_by_path:
            drain_by_path[old_path] = dict(rejected)
            drain_added += 1

    drain_rows = list(drain_by_path.values())
    if (
        drain_removed != 89
        or drain_added != 107
        or len(drain_rows) != EXPECTED_DRAIN_AFTER
        or selected_paths & set(drain_by_path)
        or not rejected_paths <= set(drain_by_path)
    ):
        raise core.IntegrityError(
            f"recovery drain transition failed: removed={drain_removed} added={drain_added} rows={len(drain_rows)}"
        )
    remaining_errors = [row for row in error_rows if row["inventory"] not in selected_by_inventory]
    if len(remaining_errors) != 2 or {row["inventory"] for row in remaining_errors} != {"TFA-6323033", "TFA-6423379"}:
        raise core.IntegrityError("recovery did not leave exactly the two no-valid-Master holds")

    core.atomic_bytes(core.CATALOG_SOURCE, core.csv_bytes(catalog_fields, catalog_rows))
    core.atomic_bytes(core.DECISIONS, core.csv_bytes(decision_fields, decision_rows))
    core.atomic_bytes(core.SOURCE_DISPOSITION, core.csv_bytes(source_fields, source_rows))
    core.atomic_bytes(core.DRAIN, core.csv_bytes(drain_fields, drain_rows))
    core.atomic_bytes(
        core.ERRORS,
        b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode() for row in remaining_errors),
    )

    db = sqlite3.connect(DB)
    try:
        db.execute("BEGIN IMMEDIATE")
        for inventory, selected_row in selected_by_inventory.items():
            note = NOTE_SOLE if selected_row["selection_role"] == "selected_sole_valid_candidate" else NOTE_POSITIVE
            if db.execute(
                "UPDATE reconciliation_decisions SET selected_candidate_id=?,decision_state='selected_master',note=?,decided_by='vladimir',decided_at=? WHERE record_key=?",
                (int(selected_row["candidate_id"]), note, decided_at, selected_row["record_key"]),
            ).rowcount != 1:
                raise core.IntegrityError(f"reconciliation decision update failed: {inventory}")
            if db.execute(
                "UPDATE objects SET decision_state='resolved' WHERE record_key=?",
                (selected_row["record_key"],),
            ).rowcount != 1:
                raise core.IntegrityError(f"object state update failed: {inventory}")
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
            raise core.IntegrityError("reconciliation SQLite post-write check failed")
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

    md_preparation = json.loads(core.PREPARATION.read_text(encoding="utf-8"))
    md_preparation["prepared_at"] = decided_at
    md_preparation["fingerprints"][core.CATALOG_SOURCE.name] = core.sha256(core.CATALOG_SOURCE)
    md_preparation["sqlite_backup"] = backups["reconciliation.sqlite"]
    core.atomic_json(core.PREPARATION, md_preparation)
    for path in (core.COMPLETION, core.PUBLICATION, core.UNIT / "db-restore/accession-completion.json"):
        path.unlink(missing_ok=True)
    core.atomic_json(
        core.UNIT / "metadata/accession-state.json",
        {
            "format": "iss-tfa-accession-state-v1",
            "updated_at": core.now(),
            "run": str(core.RUN),
            "state": "running_bounded_107_recovery",
        },
    )
    payload = {
        "format": "iss-tfa-recovery-107-application-v1",
        "applied_at": decided_at,
        "result": "ok",
        "target_objects": 107,
        "sole_valid_candidate_objects": 64,
        "confirmed_positive_objects": 43,
        "remaining_noncritical_holds": 2,
        "selected_tiff_bytes": sum(int(row["size_bytes"]) for row in selected),
        "drain_rows_before": EXPECTED_DRAIN_BEFORE,
        "drain_rows_removed": drain_removed,
        "drain_rows_added": drain_added - drain_removed,
        "drain_rows_after": len(drain_rows),
        "evidence_preparation": str(PREPARATION),
        "evidence_fingerprint": core.sha256(PREPARATION),
        "backups": backups,
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "deletion_authorized": False,
    }
    core.atomic_json(APPLIED, payload)
    return payload


def main() -> int:
    for unit in (
        "iss-tfa-accession-worker.service",
        "iss-tfa-accession-shutdown-watchdog.service",
        "iss-archive-tfa-accession-watchdog.service",
        "iss-archive-tfa-workbench.service",
    ):
        service_inactive(unit)
    core.RUN.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(core.LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise core.IntegrityError("another TFA accession process holds the run lock") from error
        if APPLIED.exists():
            raise core.IntegrityError(f"107-object recovery was already applied: {APPLIED}")
        payload = apply()
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
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
                "operation": "bounded_107_recovery_application",
                "error": str(error),
                "data_integrity_suspected": True,
            },
        )
        raise
