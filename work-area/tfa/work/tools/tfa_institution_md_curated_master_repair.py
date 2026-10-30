#!/usr/bin/env python3
"""Apply the three proven institution-MD curated-master exclusions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
SOURCE_CSV = REVIEW_ROOT / "source-disposition-proposal.csv"
DRAIN_CSV = REVIEW_ROOT / "drain-list-proposal.csv"
LOCAL_DECISIONS_CSV = REVIEW_ROOT / "local-object-decisions.csv"
LOCAL_DISPOSITIONS_CSV = REVIEW_ROOT / "local-object-candidate-dispositions.csv"
REDUNDANT_CSV = REVIEW_ROOT / "full-wf-md-redundant-images.csv"
LOCAL_PREFLIGHT = REVIEW_ROOT / "local-object-workbench-preflight.json"
WORKBENCH_PREFLIGHT = REVIEW_ROOT / "workbench-preflight.json"
RECEIPT = REVIEW_ROOT / "institution-md-curated-master-repair.json"
SELECTION_TOKEN = "/Alle-Tiff-Positive/"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"}
TIFF_EXTENSIONS = {".tif", ".tiff"}

TARGETS = {
    "local:TFA-549030": {"md_object_id": "93608", "master_candidate_id": 11},
    "local:TFA-549031": {"md_object_id": "93609", "master_candidate_id": 17},
    "local:TFA-5713365": {"md_object_id": "85823", "master_candidate_id": 199},
}
EXPECTED_BEFORE = {
    "local_object_decisions": 295,
    "duplicate_md_decisions": 294,
    "source_disposition_proposal": 63377,
    "drain_list_proposal": 63377,
    "proposed_redundant": 48384,
    "redundant_evidence": 48384,
}


class RepairError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=60)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(DATABASE, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise RepairError("Elements did not resolve to one mounted filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options or "rw" in options:
        raise RepairError(f"Elements read-only mount guard failed: {row}")
    return row


def workbench_is_listening() -> bool:
    with socket.socket() as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", 8768)) == 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def assert_work_path(path: Path) -> None:
    try:
        path.resolve().relative_to(WORK_ROOT.resolve())
    except ValueError as error:
        raise RepairError(f"write escaped approved TFA work root: {path}") from error


def backup(path: Path, stamp: str) -> str:
    assert_work_path(path)
    destination = path.with_name(f"{path.stem}.pre-institution-md-curated-master-{stamp}{path.suffix}")
    if destination.exists():
        raise RepairError(f"backup already exists: {destination}")
    shutil.copy2(path, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return str(destination)


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def csv_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def current_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "local_object_decisions": int(connection.execute("SELECT COUNT(*) FROM local_object_decisions").fetchone()[0]),
        "duplicate_md_decisions": int(
            connection.execute("SELECT COUNT(*) FROM local_object_decisions WHERE outcome='duplicate_md'").fetchone()[0]
        ),
        "source_disposition_proposal": int(
            connection.execute("SELECT COUNT(*) FROM source_disposition_proposal").fetchone()[0]
        ),
        "drain_list_proposal": int(connection.execute("SELECT COUNT(*) FROM drain_list_proposal").fetchone()[0]),
        "proposed_redundant": int(
            connection.execute("SELECT COUNT(*) FROM drain_list_proposal WHERE drain_state='proposed_redundant'").fetchone()[0]
        ),
        "redundant_evidence": csv_count(REDUNDANT_CSV),
    }


def projection(connection: sqlite3.Connection) -> dict[str, Any]:
    before = current_counts(connection)
    if before != EXPECTED_BEFORE:
        raise RepairError(f"pre-repair scope drifted: {before}")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise RepairError("SQLite quick_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RepairError("SQLite foreign-key check failed")

    master_rows = []
    redundant_rows = []
    hashes: dict[str, str] = {}
    for record_key, target in TARGETS.items():
        decision = connection.execute(
            "SELECT 1 FROM local_object_decisions WHERE record_key=?", (record_key,)
        ).fetchone()
        if decision is not None:
            raise RepairError(f"target already has a local decision: {record_key}")
        match = connection.execute(
            """
            SELECT md_object_id,md_inventory,md_title,match_basis
            FROM local_md_match_candidates
            WHERE record_key=? AND md_object_id=?
              AND (instr(match_basis,'exact_inventory')>0 OR instr(match_basis,'live_historical_md_id')>0)
            """,
            (record_key, target["md_object_id"]),
        ).fetchone()
        if match is None:
            raise RepairError(f"exact institution-MD binding disappeared: {record_key}")
        selected_in_collection = connection.execute(
            "SELECT 1 FROM reconciliation_decisions WHERE record_key='md:'||? AND decision_state='selected_master'",
            (target["md_object_id"],),
        ).fetchone()
        if selected_in_collection is not None:
            raise RepairError(f"target unexpectedly entered collection-selected scope: {record_key}")
        candidates = [
            dict(row) for row in connection.execute(
                """
                SELECT c.*,t.width_px,t.height_px,t.accession_eligible
                FROM candidates c JOIN local_candidate_technical t ON t.candidate_id=c.candidate_id
                WHERE c.record_key=? AND lower(c.extension) IN ('.tif','.tiff','.jpg','.jpeg','.png','.webp','.psd')
                ORDER BY c.candidate_id
                """,
                (record_key,),
            )
        ]
        curated = [
            row for row in candidates
            if int(row["accession_eligible"]) == 1
            and str(row["extension"]).casefold() in TIFF_EXTENSIONS
            and SELECTION_TOKEN in str(row["source_path"])
        ]
        if len(curated) != 1 or int(curated[0]["candidate_id"]) != target["master_candidate_id"]:
            raise RepairError(f"unique curated Master guard failed: {record_key}")
        master = curated[0]
        tiffs = [row for row in candidates if str(row["extension"]).casefold() in TIFF_EXTENSIONS]
        tiff_hashes = []
        for row in tiffs:
            path = Path(str(row["source_path"]))
            if path.stat().st_size != int(row["size_bytes"]):
                raise RepairError(f"source size drifted: {path}")
            digest = sha256_file(path)
            hashes[str(path)] = digest
            tiff_hashes.append(digest)
        if len(set(tiff_hashes)) != 1:
            raise RepairError(f"TIFF candidates are not byte-identical: {record_key}")
        master["md_object_id"] = target["md_object_id"]
        master["md_inventory"] = match["md_inventory"]
        master["md_title"] = match["md_title"]
        master["sha256"] = tiff_hashes[0]
        master_rows.append(master)
        for row in candidates:
            if int(row["candidate_id"]) == target["master_candidate_id"]:
                continue
            row["md_object_id"] = target["md_object_id"]
            row["md_inventory"] = match["md_inventory"]
            row["md_title"] = match["md_title"]
            row["selected_master_path"] = master["source_path"]
            row["selected_master_sha256"] = master["sha256"]
            redundant_rows.append(row)
    if len(master_rows) != 3 or len(redundant_rows) != 17:
        raise RepairError(
            f"bounded target scope failed: masters={len(master_rows)}, redundant={len(redundant_rows)}"
        )
    return {
        "counts_before": before,
        "masters": master_rows,
        "redundant": redundant_rows,
        "hashes": hashes,
    }


def write_disposition(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    master: bool,
) -> None:
    path = str(candidate["source_path"])
    row = connection.execute(
        "SELECT * FROM source_disposition_proposal WHERE source_path=?", (path,)
    ).fetchone()
    if row is None:
        raise RepairError(f"source disposition row is missing: {path}")
    value = dict(row)
    if master:
        note = (
            f"Retained authoritative Alle-Tiff-Positive TIFF Master for institution "
            f"md:{candidate['md_object_id']}; SHA-256 {candidate['sha256']}; deletion not authorized"
        )
        value.update({
            "preservation_status": "selected_master",
            "preservation_basis": "authoritative_alle_tiff_positive_exact_institution_md",
            "drain_state": "retain_selected_master",
            "deletion_authorized": "false",
        })
    else:
        note = (
            f"Redundant to retained authoritative TIFF Master {candidate['selected_master_path']} "
            f"for institution md:{candidate['md_object_id']}; deletion not authorized"
        )
        value.update({
            "preservation_status": "redundant",
            "preservation_basis": "exact_institution_md_with_authoritative_curated_master",
            "drain_state": "proposed_redundant",
            "deletion_authorized": "false",
        })
    old_note = clean(value.get("notes"))
    value["notes"] = f"{old_note}; {note}" if old_note else note
    fields = [str(info["name"]) for info in connection.execute("PRAGMA table_info(source_disposition_proposal)")]
    values = [value[field] for field in fields]
    connection.execute(
        "INSERT OR REPLACE INTO source_disposition_proposal VALUES ("
        + ",".join("?" for _ in fields) + ")",
        values,
    )
    connection.execute("DELETE FROM drain_list_proposal WHERE source_path=?", (path,))
    connection.execute(
        "INSERT INTO drain_list_proposal VALUES (" + ",".join("?" for _ in fields) + ")",
        values,
    )


def export_local(connection: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions = [
        dict(row) for row in connection.execute(
            """
            SELECT d.*,o.inventory,COALESCE(c.source_path,'') AS selected_source_path
            FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
            LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            ORDER BY o.inventory COLLATE NOCASE,d.record_key
            """
        )
    ]
    dispositions = []
    for row in connection.execute(
        """
        SELECT d.record_key,o.inventory,d.outcome,d.selected_candidate_id,d.matched_md_object_id,
               c.candidate_id,c.source_path,c.extension,c.size_bytes,
               COALESCE((
                 SELECT sm.source_path FROM reconciliation_decisions rd
                 JOIN candidates sm ON sm.candidate_id=rd.selected_candidate_id
                 WHERE rd.record_key='md:'||d.matched_md_object_id
                   AND rd.decision_state='selected_master'
                   AND lower(sm.extension) IN ('.tif','.tiff')
               ),'') AS existing_md_selected_master_path,
               (
                 SELECT COUNT(*) FROM candidates cc
                 JOIN local_candidate_technical ct ON ct.candidate_id=cc.candidate_id
                 WHERE cc.record_key=d.record_key AND ct.accession_eligible=1
                   AND lower(cc.extension) IN ('.tif','.tiff')
                   AND cc.source_path LIKE '%/Alle-Tiff-Positive/%'
               ) AS curated_master_count
        FROM local_object_decisions d JOIN objects o ON o.record_key=d.record_key
        JOIN candidates c ON c.record_key=d.record_key
        ORDER BY o.inventory COLLATE NOCASE,c.source_path
        """
    ):
        extension = str(row["extension"]).casefold()
        if extension not in IMAGE_EXTENSIONS:
            disposition = "retain_metadata_evidence"
            basis = "non-image evidence remains attached to the object"
        elif row["outcome"] == "new_local_object":
            disposition = "selected_master" if row["candidate_id"] == row["selected_candidate_id"] else "redundant"
            basis = "human selected one local Master; all other image candidates are redundant"
        elif row["outcome"] == "duplicate_md":
            selected_path = str(row["existing_md_selected_master_path"])
            curated_master = (
                not selected_path and int(row["curated_master_count"]) == 1
                and extension in TIFF_EXTENSIONS and SELECTION_TOKEN in str(row["source_path"])
            )
            if (selected_path and row["source_path"] == selected_path) or curated_master:
                disposition = "existing_md_selected_master"
                basis = f"retained selected TIFF Master evidence for md:{row['matched_md_object_id']}"
            else:
                disposition = "redundant_to_existing_md_object"
                basis = f"user-directed exact match to md:{row['matched_md_object_id']}"
        else:
            disposition = "unresolved"
            basis = "human left object unresolved"
        dispositions.append({
            "record_key": row["record_key"], "inventory": row["inventory"],
            "outcome": row["outcome"], "matched_md_object_id": row["matched_md_object_id"] or "",
            "candidate_id": row["candidate_id"], "source_path": row["source_path"],
            "extension": row["extension"], "size_bytes": row["size_bytes"],
            "disposition": disposition, "disposition_basis": basis,
            "deletion_authorized": "false",
        })
    return decisions, dispositions


def database_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, Any]]]:
    fields = [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY source_path")]
    return fields, rows


def run(*, apply: bool) -> dict[str, Any]:
    mount = source_mount()
    required = (
        DATABASE, SOURCE_CSV, DRAIN_CSV, LOCAL_DECISIONS_CSV, LOCAL_DISPOSITIONS_CSV,
        REDUNDANT_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT,
    )
    if not all(path.is_file() for path in required):
        raise RepairError("required queue or readable evidence is missing")
    if apply and RECEIPT.exists():
        raise RepairError(f"repair receipt already exists: {RECEIPT}")
    with closing(connect(read_only=True)) as connection:
        projected = projection(connection)
    summary = {
        "result": "ok",
        "mode": "apply" if apply else "check",
        "institution_md_records": len(projected["masters"]),
        "retained_curated_tiff_masters": len(projected["masters"]),
        "redundant_image_paths_added": len(projected["redundant"]),
        "all_additional_tiffs_byte_identical": True,
        "deletion_authorized": False,
    }
    if not apply:
        return summary
    if workbench_is_listening():
        raise RepairError("port 8768 must be stopped before applying the repair")

    applied_at = utc_now()
    stamp = applied_at.replace("-", "").replace(":", "")
    backups = [
        backup(path, stamp) for path in (
            DATABASE, SOURCE_CSV, DRAIN_CSV, LOCAL_DECISIONS_CSV,
            LOCAL_DISPOSITIONS_CSV, REDUNDANT_CSV, LOCAL_PREFLIGHT, WORKBENCH_PREFLIGHT,
        )
    ]
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if current_counts(connection) != projected["counts_before"]:
            raise RepairError("queue changed after repair preflight")
        for master in projected["masters"]:
            record_key = str(master["record_key"])
            metadata = connection.execute(
                "SELECT * FROM local_object_metadata WHERE record_key=?", (record_key,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO local_object_decisions
                  (record_key,outcome,selected_candidate_id,matched_md_object_id,
                   title,description,date_text,object_type,material,dimensions,
                   note,decided_by,decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record_key, "duplicate_md", None, master["md_object_id"],
                    metadata["title"], metadata["description"], metadata["date_text"],
                    metadata["object_type"], metadata["material"], metadata["dimensions"],
                    (
                        f"User confirmed institution md:{master['md_object_id']} and authoritative "
                        f"Alle-Tiff-Positive TIFF candidate {master['candidate_id']}; other images redundant; "
                        "deletion not authorized."
                    ),
                    "codex:user-confirmed-institution-md-master", applied_at,
                ),
            )
            write_disposition(connection, master, master=True)
        for row in projected["redundant"]:
            write_disposition(connection, row, master=False)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_institution_md_curated_master_repair',?)",
            (str(RECEIPT),),
        )
        connection.commit()

    with closing(connect(read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        unauthorized = int(
            connection.execute("SELECT COUNT(*) FROM drain_list_proposal WHERE deletion_authorized!='false'").fetchone()[0]
        )
        after = current_counts(connection)
        source_fields, source_rows = database_rows(connection, "source_disposition_proposal")
        drain_fields, drain_rows = database_rows(connection, "drain_list_proposal")
        local_decisions, local_dispositions = export_local(connection)
    expected_after = {
        "local_object_decisions": 298,
        "duplicate_md_decisions": 297,
        "source_disposition_proposal": 63377,
        "drain_list_proposal": 63377,
        "proposed_redundant": 48401,
        "redundant_evidence": 48384,
    }
    if quick != "ok" or foreign or unauthorized or after != expected_after:
        raise RepairError(
            f"post-repair validation failed: quick={quick}, foreign={len(foreign)}, "
            f"unauthorized={unauthorized}, counts={after}"
        )

    existing_redundant = []
    with REDUNDANT_CSV.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        redundant_fields = list(reader.fieldnames or ())
        existing_redundant = list(reader)
    known_paths = {row["source_path"] for row in existing_redundant}
    for row in projected["redundant"]:
        if row["source_path"] in known_paths:
            raise RepairError(f"repair redundant path already existed in evidence: {row['source_path']}")
        existing_redundant.append({
            "source_path": row["source_path"],
            "extension": row["extension"],
            "size_bytes": row["size_bytes"],
            "normalized_inventory": row["inventory"],
            "matched_md_object_ids": row["md_object_id"],
            "matched_md_inventories": row["md_inventory"],
            "selected_master_paths": row["selected_master_path"],
            "disposition": "redundant_to_existing_md_selected_master",
            "disposition_basis": "exact_institution_md_with_authoritative_curated_master",
            "drain_state": "proposed_redundant",
            "deletion_authorized": "false",
        })
    existing_redundant.sort(key=lambda row: row["source_path"])

    atomic_csv(SOURCE_CSV, source_fields, source_rows)
    atomic_csv(DRAIN_CSV, drain_fields, drain_rows)
    atomic_csv(
        LOCAL_DECISIONS_CSV,
        (
            "record_key", "outcome", "selected_candidate_id", "matched_md_object_id", "title", "description",
            "date_text", "object_type", "material", "dimensions", "note", "decided_by", "decided_at",
            "inventory", "selected_source_path",
        ),
        local_decisions,
    )
    atomic_csv(
        LOCAL_DISPOSITIONS_CSV,
        (
            "record_key", "inventory", "outcome", "matched_md_object_id", "candidate_id", "source_path",
            "extension", "size_bytes", "disposition", "disposition_basis", "deletion_authorized",
        ),
        local_dispositions,
    )
    atomic_csv(REDUNDANT_CSV, redundant_fields, existing_redundant)

    local_preflight = json.loads(LOCAL_PREFLIGHT.read_text(encoding="utf-8"))
    local_preflight.update({
        "prepared_at": applied_at,
        "existing_local_decisions_preserved": 298,
        "institution_md_curated_master_repair": str(RECEIPT),
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
    })
    atomic_json(LOCAL_PREFLIGHT, local_preflight)
    workbench_preflight = json.loads(WORKBENCH_PREFLIGHT.read_text(encoding="utf-8"))
    workbench_preflight["institution_md_curated_master_repair"] = {
        "applied_at": applied_at,
        "receipt": str(RECEIPT),
        "retained_master_paths": [row["source_path"] for row in projected["masters"]],
        "redundant_image_paths": len(projected["redundant"]),
        "deletion_authorized": False,
    }
    atomic_json(WORKBENCH_PREFLIGHT, workbench_preflight)

    receipt = {
        "format": "iss-tfa-institution-md-curated-master-repair-v1",
        "version": VERSION,
        "applied_at": applied_at,
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "backups": backups,
        "targets": [
            {
                "record_key": row["record_key"],
                "md_object_id": row["md_object_id"],
                "selected_candidate_id": row["candidate_id"],
                "selected_master_path": row["source_path"],
                "selected_master_sha256": row["sha256"],
            }
            for row in projected["masters"]
        ],
        "counts_after": {
            **after,
            "redundant_evidence": len(existing_redundant),
            "local_candidate_dispositions": len(local_dispositions),
        },
        "sqlite_quick_check": quick,
        "sqlite_foreign_key_violations": len(foreign),
        **summary,
        "mode": "apply",
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = run(apply=args.apply)
    except Exception as error:
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
