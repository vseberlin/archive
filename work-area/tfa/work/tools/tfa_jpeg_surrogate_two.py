#!/usr/bin/env python3
"""Accession two user-approved high-resolution JPEG surrogate Masters."""

from __future__ import annotations

import csv
import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import tfa_accession as core
import tfa_recover_1958_seven as recovery


DB = core.REVIEW / "reconciliation.sqlite"
APPLIED = core.RUN / "jpeg-surrogate-two-applied.json"
RESULT = core.RUN / "jpeg-surrogate-two-completion.json"
SEARCH_RESULT = (
    core.WORK
    / "work/reconciliation/wf-full-visual-index/tmp/"
    "missing-masters-TFA-6323033-TFA-6423379-czkawka.json"
)
EXPECTED_DRAIN_BEFORE = 41720
EXPECTED_DRAIN_AFTER = 41724
TARGETS = {
    "TFA-6323033": {
        "record_key": "md:65907",
        "md_object_id": "65907",
        "candidate_id": "50682",
        "source_path": (
            "/media/vladimir/Elements/restore/archive/SCANS-fertig/"
            "04_Rosenau_dritte-Lieferung/Positive/JPG Positiv/"
            "TFA-NEG-6323033.jpg"
        ),
        "size_bytes": 2150121,
        "mtime_ns": 1578478855572515900,
        "width": 6928,
        "height": 5095,
    },
    "TFA-6423379": {
        "record_key": "md:65998",
        "md_object_id": "65998",
        "candidate_id": "51391",
        "source_path": (
            "/media/vladimir/Elements/restore/archive/SCANS-fertig/"
            "04_Rosenau_dritte-Lieferung/Positive/JPG Positiv/"
            "TFA-NEG-6423379.jpg"
        ),
        "size_bytes": 2369387,
        "mtime_ns": 1578478895195273500,
        "width": 4892,
        "height": 6730,
    },
}
NOTE = (
    "User-selected high-resolution positive JPEG as surrogate Master after the "
    "complete USB filename and perceptual TIFF search found no decodable TIFF "
    "Master; all other candidates are redundant evidence and deletion remains "
    "unauthorized."
)


def service_inactive(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() not in {"inactive", "failed"}:
        raise core.IntegrityError(f"service must be inactive: {unit}")


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def validate() -> tuple[
    list[str],
    list[dict[str, str]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    if core.EXPECTED_DRAIN != EXPECTED_DRAIN_AFTER:
        raise core.IntegrityError("accession drain guard is not set for JPEG surrogates")
    mount = core.source_mount()
    if not SEARCH_RESULT.is_file() or json.loads(SEARCH_RESULT.read_text()) != []:
        raise core.IntegrityError("whole-archive TIFF search is absent or has matches")
    completion = json.loads(core.COMPLETION.read_text(encoding="utf-8"))
    if (
        completion.get("result") != "ok"
        or int(completion.get("objects_accessioned", -1)) != 5279
        or int(completion.get("noncritical_error_holds", -1)) != 2
        or completion.get("source_read_only") is not True
        or completion.get("data_integrity_suspected") is not False
    ):
        raise core.IntegrityError("pre-surrogate accession completion drifted")

    catalog_fields, catalog_rows, _ = core.load_scope()
    catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
    errors = [
        json.loads(line)
        for line in core.ERRORS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if {row.get("inventory") for row in errors} != set(TARGETS) or len(errors) != 2:
        raise core.IntegrityError("the two expected noncritical holds changed")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    evidence: list[dict[str, Any]] = []
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("reconciliation SQLite quick_check failed")
        for inventory, expected in TARGETS.items():
            row = catalog_by_inventory.get(inventory)
            if row is None or row["record_key"] != expected["record_key"]:
                raise core.IntegrityError(f"catalogue binding failed: {inventory}")
            candidate = db.execute(
                "SELECT * FROM candidates WHERE candidate_id=?",
                (int(expected["candidate_id"]),),
            ).fetchone()
            if (
                candidate is None
                or candidate["record_key"] != expected["record_key"]
                or candidate["inventory"] != inventory
                or candidate["source_path"] != expected["source_path"]
                or int(candidate["size_bytes"]) != expected["size_bytes"]
                or int(candidate["mtime_ns"]) != expected["mtime_ns"]
                or str(candidate["extension"]).casefold() != ".jpg"
            ):
                raise core.IntegrityError(f"JPEG candidate binding failed: {inventory}")
            source = core.inside(Path(expected["source_path"]), core.SOURCE_ROOT, exists=True)
            before = source.stat()
            if before.st_size != expected["size_bytes"] or before.st_mtime_ns != expected["mtime_ns"]:
                raise core.IntegrityError(f"JPEG source changed: {source}")
            width, height, _orientation = core.dimensions(source)
            if (width, height) != (expected["width"], expected["height"]):
                raise core.IntegrityError(f"JPEG dimensions changed: {source}")
            pixel_digest = core.pixel_digest(source, 8)
            after = source.stat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise core.IntegrityError(f"JPEG source changed during validation: {source}")
            evidence.append(
                {
                    "inventory": inventory,
                    "record_key": expected["record_key"],
                    "candidate_id": expected["candidate_id"],
                    "source_path": str(source),
                    "size_bytes": before.st_size,
                    "mtime_ns": before.st_mtime_ns,
                    "width": width,
                    "height": height,
                    "pixel_digest": pixel_digest,
                }
            )
    finally:
        db.close()

    for item in evidence:
        row = dict(catalog_by_inventory[item["inventory"]])
        row.update(
            {
                "selected_candidate_id": item["candidate_id"],
                "selected_master_path": item["source_path"],
                "selected_master_size_bytes": str(item["size_bytes"]),
            }
        )
        receipt = core.process_one(row, publish=False)
        if (
            receipt["source_path"] != item["source_path"]
            or not receipt["master_relative_path"].endswith(".jpg")
            or (receipt["access_width"], receipt["access_height"])
            != (item["width"], item["height"])
        ):
            raise core.IntegrityError(f"JPEG surrogate pilot failed: {item['inventory']}")

    return catalog_fields, catalog_rows, errors, {
        "mount": mount,
        "completion": completion,
        "evidence": evidence,
    }


def apply_decisions(
    catalog_fields: list[str],
    catalog_rows: list[dict[str, str]],
    validation: dict[str, Any],
) -> dict[str, Any]:
    decided_at = core.now()
    prefix = "pre-jpeg-surrogate-two-" + decided_at.replace(":", "").replace("-", "")
    backups = recovery.backup_current_state(prefix)

    catalog_by_inventory = {row["inventory"]: row for row in catalog_rows}
    decision_fields, decisions = read_rows(core.DECISIONS)
    decision_by_inventory = {row["inventory"]: row for row in decisions}
    source_fields, source_rows = read_rows(core.SOURCE_DISPOSITION)
    source_by_path = {row["source_path"]: row for row in source_rows}
    if len(source_by_path) != len(source_rows):
        raise core.IntegrityError("source disposition paths are not unique")
    drain_fields, drain_rows = read_rows(core.DRAIN)
    if len(drain_rows) != EXPECTED_DRAIN_BEFORE:
        raise core.IntegrityError(f"pre-surrogate drain count drifted: {len(drain_rows)}")
    drain_by_path = {row["source_path"]: row for row in drain_rows}
    if len(drain_by_path) != len(drain_rows):
        raise core.IntegrityError("drain paths are not unique")

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        candidates_by_inventory = {
            inventory: [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM candidates WHERE inventory=? ORDER BY candidate_id",
                    (inventory,),
                )
            ]
            for inventory in TARGETS
        }
    finally:
        db.close()
    if any(len(rows) != 6 for rows in candidates_by_inventory.values()):
        raise core.IntegrityError("expected six candidates for each JPEG surrogate object")

    removed = 0
    added = 0
    for inventory, expected in TARGETS.items():
        catalog = catalog_by_inventory[inventory]
        decision = decision_by_inventory[inventory]
        old_path = catalog["selected_master_path"]
        new_path = expected["source_path"]
        catalog.update(
            {
                "selected_candidate_id": expected["candidate_id"],
                "selected_master_path": new_path,
                "selected_master_size_bytes": str(expected["size_bytes"]),
                "master_decision_state": "selected_master",
                "master_decided_by": "vladimir",
                "master_decided_at": decided_at,
                "master_decision_note": NOTE,
                "provenance_chain": (
                    f"museum-digital {expected['record_key']} | read-only JPEG "
                    f"surrogate source {new_path}"
                ),
                "accession_state": "ready_for_accession",
                "accession_hold_reason": "",
            }
        )
        decision.update(
            {
                "decision_state": "selected_master",
                "selected_candidate_id": expected["candidate_id"],
                "selected_source_path": new_path,
                "note": NOTE,
                "decided_by": "vladimir",
                "decided_at": decided_at,
            }
        )
        for candidate in candidates_by_inventory[inventory]:
            path = candidate["source_path"]
            disposition = source_by_path.get(path)
            if disposition is None:
                raise core.IntegrityError(f"source disposition missing: {path}")
            if path == new_path:
                disposition.update(
                    {
                        "source_role": "selected_master",
                        "final_path": f"masters/{inventory}.jpg",
                        "preservation_status": "selected_for_accession",
                        "preservation_basis": (
                            "user_selected_hires_jpeg_surrogate_after_whole_archive_search"
                        ),
                        "source_sha256": "",
                        "final_sha256": "",
                        "exact_copy_proven": "false",
                        "drain_state": "retain_selected_master",
                        "deletion_authorized": "false",
                        "notes": NOTE,
                    }
                )
                if path not in drain_by_path:
                    raise core.IntegrityError(f"selected JPEG was not in prior drain: {path}")
                del drain_by_path[path]
                removed += 1
                continue
            disposition.update(
                {
                    "source_role": (
                        "rejected_master_candidate"
                        if Path(path).suffix.casefold() in {".tif", ".tiff"}
                        else disposition["source_role"]
                    ),
                    "final_path": "",
                    "preservation_status": "redundant",
                    "preservation_basis": "redundant_to_user_selected_hires_jpeg_surrogate",
                    "source_sha256": "",
                    "final_sha256": "",
                    "exact_copy_proven": "false",
                    "drain_state": "proposed_redundant",
                    "deletion_authorized": "false",
                    "notes": (
                        "Redundant after explicit selection of the high-resolution JPEG "
                        "surrogate Master; source retained unchanged; deletion not authorized."
                    ),
                }
            )
            if path not in drain_by_path:
                drain_by_path[path] = dict(disposition)
                added += 1
        if old_path == new_path:
            raise core.IntegrityError(f"surrogate delta did not replace prior TIFF: {inventory}")

    drain_rows = list(drain_by_path.values())
    if removed != 2 or added != 6 or len(drain_rows) != EXPECTED_DRAIN_AFTER:
        raise core.IntegrityError(
            f"drain transition failed: removed={removed} added={added} rows={len(drain_rows)}"
        )
    selected_paths = {target["source_path"] for target in TARGETS.values()}
    if selected_paths & set(drain_by_path):
        raise core.IntegrityError("selected JPEG intersects final drain")

    core.atomic_bytes(core.CATALOG_SOURCE, core.csv_bytes(catalog_fields, catalog_rows))
    core.atomic_bytes(core.DECISIONS, core.csv_bytes(decision_fields, decisions))
    core.atomic_bytes(core.SOURCE_DISPOSITION, core.csv_bytes(source_fields, source_rows))
    core.atomic_bytes(core.DRAIN, core.csv_bytes(drain_fields, drain_rows))
    core.atomic_bytes(core.ERRORS, b"")

    db = sqlite3.connect(DB)
    try:
        db.execute("BEGIN IMMEDIATE")
        for inventory, expected in TARGETS.items():
            if db.execute(
                """UPDATE reconciliation_decisions
                   SET selected_candidate_id=?,decision_state='selected_master',
                       note=?,decided_by='vladimir',decided_at=?
                   WHERE record_key=?""",
                (int(expected["candidate_id"]), NOTE, decided_at, expected["record_key"]),
            ).rowcount != 1:
                raise core.IntegrityError(f"decision update failed: {inventory}")
            if db.execute(
                "UPDATE objects SET decision_state='resolved' WHERE record_key=?",
                (expected["record_key"],),
            ).rowcount != 1:
                raise core.IntegrityError(f"object update failed: {inventory}")
        db.execute("DELETE FROM source_disposition_proposal")
        db.executemany(
            "INSERT INTO source_disposition_proposal VALUES ("
            + ",".join("?" for _ in source_fields)
            + ")",
            [[row[field] for field in source_fields] for row in source_rows],
        )
        db.execute("DELETE FROM drain_list_proposal")
        db.executemany(
            "INSERT INTO drain_list_proposal VALUES ("
            + ",".join("?" for _ in drain_fields)
            + ")",
            [[row[field] for field in drain_fields] for row in drain_rows],
        )
        db.commit()
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("reconciliation SQLite post-write check failed")
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
            "state": "running_bounded_jpeg_surrogate_two",
        },
    )
    payload = {
        "format": "iss-tfa-jpeg-surrogate-two-application-v1",
        "applied_at": decided_at,
        "result": "ok",
        "inventories": list(TARGETS),
        "drain_rows_before": EXPECTED_DRAIN_BEFORE,
        "drain_rows_removed": removed,
        "drain_rows_added": added,
        "drain_rows_after": len(drain_rows),
        "evidence": validation["evidence"],
        "backups": backups,
        "source_mount": validation["mount"],
        "source_read_only": True,
        "source_modified": False,
        "deletion_authorized": False,
    }
    core.atomic_json(APPLIED, payload)
    return payload


def verify_final(completion: dict[str, Any]) -> dict[str, Any]:
    if (
        completion.get("result") != "ok"
        or int(completion.get("objects_accessioned", -1)) != 5281
        or int(completion.get("noncritical_error_holds", -1)) != 0
        or int(completion.get("catalogue_duplicate_holds", -1)) != 1
        or int(completion.get("representation_rows", -1)) != 10562
        or int(completion.get("checksum_rows", -1)) != 10562
        or int(completion.get("drain_rows", -1)) != EXPECTED_DRAIN_AFTER
        or completion.get("sqlite_quick_check") != "ok"
        or completion.get("source_read_only") is not True
        or completion.get("data_integrity_suspected") is not False
    ):
        raise core.IntegrityError(f"unexpected final completion: {completion}")
    db_path = core.UNIT / "db-restore/RESTORE.sqlite"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    verified = []
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise core.IntegrityError("final RESTORE SQLite check failed")
        for inventory in TARGETS:
            obj = db.execute(
                "SELECT * FROM objects WHERE inventory=?", (inventory,)
            ).fetchone()
            reps = db.execute(
                "SELECT * FROM representations WHERE inventory=? ORDER BY role",
                (inventory,),
            ).fetchall()
            holds = db.execute(
                "SELECT COUNT(*) FROM accession_holds WHERE inventory=?", (inventory,)
            ).fetchone()[0]
            if (
                obj is None
                or obj["accession_result"] != "accessioned"
                or not obj["master_relative_path"].endswith(".jpg")
                or {row["role"] for row in reps}
                != {"preservation_master", "access_derivative"}
                or len(reps) != 2
                or holds != 0
            ):
                raise core.IntegrityError(f"final surrogate binding failed: {inventory}")
            master = core.UNIT / obj["master_relative_path"]
            access = core.UNIT / obj["access_relative_path"]
            if (
                core.sha256(master) != obj["master_sha256"]
                or core.sha256(access) != obj["access_sha256"]
            ):
                raise core.IntegrityError(f"final surrogate hash failed: {inventory}")
            verified.append(
                {
                    "inventory": inventory,
                    "master": obj["master_relative_path"],
                    "master_sha256": obj["master_sha256"],
                    "access": obj["access_relative_path"],
                    "access_sha256": obj["access_sha256"],
                }
            )
    finally:
        db.close()
    core.source_mount()
    return {"verified": verified, "sqlite_quick_check": "ok"}


def main() -> int:
    for unit in (
        "iss-tfa-accession-worker.service",
        "iss-tfa-accession-shutdown-watchdog.service",
        "iss-archive-tfa-accession-watchdog.service",
        "iss-archive-tfa-workbench.service",
    ):
        service_inactive(unit)
    if APPLIED.exists() or RESULT.exists():
        raise core.IntegrityError("bounded JPEG surrogate operation already exists")
    descriptor = os.open(core.LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise core.IntegrityError("another TFA accession process holds the run lock") from error
        fields, rows, _errors, validation = validate()
        applied = apply_decisions(fields, rows, validation)
        preflight = core.preflight(allow_unit=True)
        fields, rows, _ = core.load_scope()
        by_inventory = {row["inventory"]: row for row in rows}
        receipts = core.load_receipts()
        for inventory in TARGETS:
            if inventory in receipts:
                raise core.IntegrityError(f"unexpected pre-existing receipt: {inventory}")
            receipt = core.process_one(by_inventory[inventory], publish=True)
            if not receipt["master_relative_path"].endswith(".jpg"):
                raise core.IntegrityError(f"published Master is not JPEG: {inventory}")
            receipts[inventory] = receipt
        completion = core.finalize(fields, rows)
        final = verify_final(completion)
        core.atomic_json(core.STATUS, {**completion, "state": "complete"})
        payload = {
            "format": "iss-tfa-jpeg-surrogate-two-completion-v1",
            "completed_at": core.now(),
            "result": "ok",
            "inventories": list(TARGETS),
            "objects_accessioned": 2,
            "total_objects_accessioned": completion["objects_accessioned"],
            "remaining_noncritical_holds": completion["noncritical_error_holds"],
            "preflight": preflight,
            "application": str(APPLIED),
            "backups": applied["backups"],
            "completion": str(core.COMPLETION),
            "final": final,
            "source_read_only": True,
            "source_modified": False,
            "data_integrity_suspected": False,
            "deletion_authorized": False,
        }
        core.atomic_json(RESULT, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except core.AccessionError as error:
        core.atomic_json(
            core.CRITICAL,
            {
                "failed_at": core.now(),
                "state": "stopped_integrity",
                "operation": "bounded_jpeg_surrogate_two",
                "error": str(error),
                "data_integrity_suspected": True,
            },
        )
        raise
