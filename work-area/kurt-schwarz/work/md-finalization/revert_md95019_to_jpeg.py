#!/usr/bin/env python3
"""Revert the mistaken md:95019 PSD Master selection to the prior JPEG."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SOURCE = Path("/media/vladimir/Elements")
REVIEW_DIR = WORK / "work/reconciliation/unit-reconciliation-v1"
RECONCILIATION = REVIEW_DIR / "reconciliation.sqlite"
SCRIPT_DIR = WORK / "work/md-finalization"
PUBLISHED_DB = UNIT / "db-restore/RESTORE.sqlite"
BEFORE_PUBLISHED_DB = SCRIPT_DIR / "completed-object-census-surrogate-before.sqlite"

TARGET_RECORD = "md:95019"
TARGET_CANDIDATE = "KSC-cc87203305645d2d0677"
JPEG_SOURCE = Path(
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
    "Alle-KS-Bilder_5000px/KS-7-WF_0056.jpg"
)
PSD_SOURCE = Path(
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
    "KS-CD_WF-Doku_70er_80er_alle-Bereiche/WF Diode 213_0003.psd"
)
JPEG_MASTER_RELATIVE = "masters/KS-7-WF_0056.jpg"
PSD_MASTER_RELATIVE = "masters/KS-7-WF_0056.psd"
ACCESS_RELATIVE = "export/access/KS-7-WF_0056.jpg"
JPEG_MASTER_HASH = "sha256:43357bd1e9bfc6f6860acd87e69199e85f741fa4bf52ca94f6b9d9c9bd62d145"
JPEG_ACCESS_HASH = "sha256:d78fb6b140eca5b153ed891212301d62ea7ebd2e3538bd3f0b3cc2845b651c3d"
PSD_MASTER_HASH = "sha256:9ddea3a9ff5df059c428ce1c7c9ef3fb49f36f90545aab7243bd984d7cdfa226"
PSD_ACCESS_HASH = "sha256:1547d394e4546e156faa2cb8c8d93070d0aca1350cb94f1c3a6d24db2cd1acbc"

PREDECESSOR_MASTER = SCRIPT_DIR / "completed-object-census-surrogate-before-master.jpg"
PREDECESSOR_ACCESS = SCRIPT_DIR / "completed-object-census-surrogate-before-access.jpg"
MISTAKEN_PSD_BACKUP = SCRIPT_DIR / "md95019-mistaken-psd-master.psd"
MISTAKEN_ACCESS_BACKUP = SCRIPT_DIR / "md95019-mistaken-psd-access.jpg"
PUBLISHED_DB_BACKUP = SCRIPT_DIR / "md95019-jpeg-revert-before-published.sqlite"
RECONCILIATION_BACKUP = REVIEW_DIR / "reconciliation.pre-md95019-jpeg-revert-20260814.sqlite"
UNIT_CENSUS = UNIT / "db-restore/manifests/object-census-decisions.csv"
WORK_CENSUS = SCRIPT_DIR / "completed-object-census-decisions.csv"
BEFORE_UNIT_CENSUS = SCRIPT_DIR / "md95019-jpeg-revert-before-unit-object-census-decisions.csv"
BEFORE_WORK_CENSUS = SCRIPT_DIR / "md95019-jpeg-revert-before-work-object-census-decisions.csv"
BEFORE_CENSUS_COMPLETION = SCRIPT_DIR / "md95019-jpeg-revert-before-census-completion.json"
CURRENT_CENSUS_COMPLETION = SCRIPT_DIR / "completed-object-census-decisions-completion.json"
CORRECTION_CENSUS = SCRIPT_DIR / "md95019-jpeg-revert-object-census-decisions.csv"
COMPLETION = SCRIPT_DIR / "md95019-jpeg-revert-completion.json"

TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))
import paragon_document_bundle  # noqa: E402


class RevertError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise RevertError(f"existing artifact differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def source_mount() -> str:
    output = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS", "--target", str(SOURCE)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fields = output.splitlines()[0].split(maxsplit=3)
    options = set(fields[3].split(",")) if len(fields) == 4 else set()
    if fields[:3] != ["/dev/sdf1", str(SOURCE), "ntfs3"] or "ro" not in options or "rw" in options:
        raise RevertError(f"Elements is not safely read-only: {output}")
    return output.splitlines()[0]


def table_fields(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def table_rows(connection: sqlite3.Connection, table: str, order: str) -> list[dict[str, str]]:
    fields = table_fields(connection, table)
    return [
        {field: str(value) for field, value in zip(fields, row)}
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
    ]


def csv_bytes(fields: list[str], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def sqlite_backup(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RevertError(f"backup already exists: {destination}")
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        source_connection.close()
        destination_connection.close()
    fsync_file(destination)
    check = sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RevertError(f"backup failed quick_check: {destination}")
        if check.execute("PRAGMA foreign_key_check").fetchall():
            raise RevertError(f"backup failed foreign keys: {destination}")
    finally:
        check.close()


def validate_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RevertError(f"database failed quick_check: {path}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RevertError(f"database failed foreign keys: {path}")
    finally:
        connection.close()


def preflight() -> dict[str, Any]:
    mount = source_mount()
    expected_hashes = {
        UNIT / PSD_MASTER_RELATIVE: PSD_MASTER_HASH,
        UNIT / ACCESS_RELATIVE: PSD_ACCESS_HASH,
        PREDECESSOR_MASTER: JPEG_MASTER_HASH,
        PREDECESSOR_ACCESS: JPEG_ACCESS_HASH,
        JPEG_SOURCE: "sha256:d09e642acb80ac2517091d725b19f24abecb21b31f357bcd871e4f75005fa0d0",
        PSD_SOURCE: PSD_MASTER_HASH,
    }
    for path, expected in expected_hashes.items():
        if not path.is_file() or sha256_file(path) != expected:
            raise RevertError(f"required evidence drifted: {path}")
    if (UNIT / JPEG_MASTER_RELATIVE).exists():
        raise RevertError("prior JPEG Master destination already exists")
    for path in (
        MISTAKEN_PSD_BACKUP,
        MISTAKEN_ACCESS_BACKUP,
        PUBLISHED_DB_BACKUP,
        RECONCILIATION_BACKUP,
        BEFORE_UNIT_CENSUS,
        BEFORE_WORK_CENSUS,
        BEFORE_CENSUS_COMPLETION,
        CORRECTION_CENSUS,
        COMPLETION,
    ):
        if path.exists():
            raise RevertError(f"reversal artifact already exists: {path}")
    validate_database(PUBLISHED_DB)
    validate_database(BEFORE_PUBLISHED_DB)
    validate_database(RECONCILIATION)

    current = sqlite3.connect(f"file:{PUBLISHED_DB}?mode=ro&immutable=1", uri=True)
    before = sqlite3.connect(f"file:{BEFORE_PUBLISHED_DB}?mode=ro&immutable=1", uri=True)
    current.row_factory = before.row_factory = sqlite3.Row
    try:
        counts = {
            table: current.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("objects", "representations", "checksums", "source_disposition")
        }
        if counts != {"objects": 1935, "representations": 3870, "checksums": 3870, "source_disposition": 17077}:
            raise RevertError(f"published counts drifted: {counts}")
        target = current.execute("SELECT * FROM objects WHERE record_key=?", (TARGET_RECORD,)).fetchone()
        old_target = before.execute("SELECT * FROM objects WHERE record_key=?", (TARGET_RECORD,)).fetchone()
        if target is None or old_target is None:
            raise RevertError("target object is absent")
        if target["master_relative_path"] != PSD_MASTER_RELATIVE or target["master_sha256"] != PSD_MASTER_HASH:
            raise RevertError("published PSD Master binding drifted")
        if old_target["master_relative_path"] != JPEG_MASTER_RELATIVE or old_target["master_sha256"] != JPEG_MASTER_HASH:
            raise RevertError("predecessor JPEG binding drifted")
        changed_tables: dict[str, tuple[int, int]] = {}
        current.execute("ATTACH DATABASE ? AS predecessor", (str(BEFORE_PUBLISHED_DB),))
        for table in [
            row[0]
            for row in current.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]:
            columns = table_fields(current, table)
            before_columns = [
                str(row[1]) for row in current.execute(f'PRAGMA predecessor.table_info("{table}")')
            ]
            if columns != before_columns:
                raise RevertError(f"published/predecessor schema differs: {table}")
            names = ",".join('"' + value.replace('"', '""') + '"' for value in columns)
            current_only = current.execute(
                f'SELECT COUNT(*) FROM (SELECT {names} FROM main."{table}" EXCEPT '
                f'SELECT {names} FROM predecessor."{table}")'
            ).fetchone()[0]
            before_only = current.execute(
                f'SELECT COUNT(*) FROM (SELECT {names} FROM predecessor."{table}" EXCEPT '
                f'SELECT {names} FROM main."{table}")'
            ).fetchone()[0]
            if current_only or before_only:
                changed_tables[table] = (current_only, before_only)
        expected_differences = {
            "checksums": (2, 2),
            "objects": (1, 1),
            "reconciliation_decisions": (2, 0),
            "representations": (2, 2),
            "source_disposition": (2, 2),
        }
        if changed_tables != expected_differences:
            raise RevertError(f"published change scope drifted: {changed_tables}")
    finally:
        current.close()
        before.close()

    reconciliation = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    reconciliation.row_factory = sqlite3.Row
    try:
        latest = reconciliation.execute(
            """SELECT * FROM object_census_decisions WHERE decision_id=(
                   SELECT MAX(decision_id) FROM object_census_decisions WHERE candidate_id=?)""",
            (TARGET_CANDIDATE,),
        ).fetchone()
        if latest is None or latest["disposition"] != "surrogate_master" or latest["target_record_key"] != TARGET_RECORD:
            raise RevertError("latest census decision is not the mistaken PSD selection")
        next_decision = int(reconciliation.execute("SELECT MAX(decision_id) FROM object_census_decisions").fetchone()[0]) + 1
    finally:
        reconciliation.close()
    return {
        "result": "ok",
        "record_key": TARGET_RECORD,
        "candidate_id": TARGET_CANDIDATE,
        "next_census_decision_id": next_decision,
        "source_mount": mount,
        "published_counts": counts,
        "published_change_scope": changed_tables,
        "deletion_authorized": False,
    }


def replace_exact_row(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    table: str,
    where: str,
    parameters: tuple[Any, ...],
) -> None:
    fields = table_fields(target, table)
    if fields != table_fields(source, table):
        raise RevertError(f"schema mismatch while restoring {table}")
    rows = list(source.execute(f'SELECT * FROM "{table}" WHERE {where}', parameters))
    if len(rows) != 1:
        raise RevertError(f"predecessor row count is not one for {table}")
    target.execute(f'DELETE FROM "{table}" WHERE {where}', parameters)
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise RevertError(f"current row count is not one for {table}")
    quoted = ",".join('"' + value.replace('"', '""') + '"' for value in fields)
    target.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in fields)})',
        tuple(rows[0]),
    )


def build_reconciliation_candidate(decided_at: str) -> tuple[Path, int]:
    temporary = REVIEW_DIR / f".md95019-correction-{os.getpid()}.sqlite"
    if temporary.exists():
        raise RevertError(f"unexpected reconciliation temporary: {temporary}")
    source = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    finally:
        source.close()
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("BEGIN IMMEDIATE")
    target.execute(
        """INSERT INTO object_census_decisions (
               candidate_id,disposition,target_record_key,actor,reason,decided_at,
               curated_title,curated_date_text,curated_description,curated_tags,cross_unit_hold_json
           ) VALUES (?, 'redundant', NULL, 'vladimir', ?, ?, NULL, NULL, NULL, NULL, NULL)""",
        (
            TARGET_CANDIDATE,
            "Curator correction: the PSD Master selection was a mistake; retain the prior JPEG as the single Master. The PSD remains redundant and deletion is not authorized.",
            decided_at,
        ),
    )
    decision_id = int(target.execute("SELECT last_insert_rowid()").fetchone()[0])
    target.commit()
    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
        target.close()
        temporary.unlink(missing_ok=True)
        raise RevertError("corrected reconciliation candidate failed integrity")
    target.close()
    fsync_file(temporary)
    return temporary, decision_id


def build_published_controls(decided_at: str) -> tuple[dict[Path, bytes], Path]:
    temporary_db = SCRIPT_DIR / f".md95019-published-{os.getpid()}.sqlite"
    if temporary_db.exists():
        raise RevertError(f"unexpected published temporary: {temporary_db}")
    source = sqlite3.connect(f"file:{PUBLISHED_DB}?mode=ro", uri=True)
    target = sqlite3.connect(temporary_db)
    try:
        source.backup(target)
    finally:
        source.close()
    target.row_factory = sqlite3.Row
    predecessor = sqlite3.connect(f"file:{BEFORE_PUBLISHED_DB}?mode=ro&immutable=1", uri=True)
    predecessor.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("BEGIN IMMEDIATE")
    try:
        replace_exact_row(target, predecessor, "objects", "record_key=?", (TARGET_RECORD,))
        target.execute("DELETE FROM representations WHERE record_keys=?", (TARGET_RECORD,))
        if target.execute("SELECT changes()").fetchone()[0] != 2:
            raise RevertError("current target representations are not exactly two")
        representation_fields = table_fields(target, "representations")
        quoted = ",".join('"' + value.replace('"', '""') + '"' for value in representation_fields)
        representation_rows = list(
            predecessor.execute("SELECT * FROM representations WHERE record_keys=? ORDER BY role", (TARGET_RECORD,))
        )
        if len(representation_rows) != 2:
            raise RevertError("predecessor target representations are not exactly two")
        target.executemany(
            f'INSERT INTO representations ({quoted}) VALUES ({",".join("?" for _ in representation_fields)})',
            [tuple(row) for row in representation_rows],
        )
        target.execute(
            "DELETE FROM checksums WHERE path IN (?,?)", (PSD_MASTER_RELATIVE, ACCESS_RELATIVE)
        )
        if target.execute("SELECT changes()").fetchone()[0] != 2:
            raise RevertError("current target checksums are not exactly two")
        checksum_fields = table_fields(target, "checksums")
        quoted = ",".join('"' + value.replace('"', '""') + '"' for value in checksum_fields)
        checksum_rows = list(
            predecessor.execute(
                "SELECT * FROM checksums WHERE path IN (?,?) ORDER BY path",
                (JPEG_MASTER_RELATIVE, ACCESS_RELATIVE),
            )
        )
        if len(checksum_rows) != 2:
            raise RevertError("predecessor target checksums are not exactly two")
        target.executemany(
            f'INSERT INTO checksums ({quoted}) VALUES ({",".join("?" for _ in checksum_fields)})',
            [tuple(row) for row in checksum_rows],
        )
        replace_exact_row(target, predecessor, "source_disposition", "source_path=?", (str(JPEG_SOURCE),))
        psd_before = predecessor.execute(
            "SELECT * FROM source_disposition WHERE source_path=?", (str(PSD_SOURCE),)
        ).fetchone()
        if psd_before is None:
            raise RevertError("predecessor PSD disposition is absent")
        psd = row_dict(psd_before)
        psd.update(
            {
                "source_role": "redundant_surrogate",
                "final_path": JPEG_MASTER_RELATIVE,
                "preservation_status": "retained_source_redundant_after_master_selection",
                "preservation_basis": "curator_corrected_master_selection_to_prior_jpeg",
                "final_sha256": JPEG_MASTER_HASH,
                "exact_copy_proven": "false",
                "drain_state": "retain_reviewed_redundant",
                "deletion_authorized": "false",
                "notes": "Curator correction: PSD selection was a mistake; retain prior JPEG as Master. Source remains protected and no deletion is authorized.",
            }
        )
        fields = table_fields(target, "source_disposition")
        target.execute("DELETE FROM source_disposition WHERE source_path=?", (str(PSD_SOURCE),))
        if target.execute("SELECT changes()").fetchone()[0] != 1:
            raise RevertError("current PSD source disposition is not exactly one")
        quoted = ",".join('"' + value.replace('"', '""') + '"' for value in fields)
        target.execute(
            f'INSERT INTO source_disposition ({quoted}) VALUES ({",".join("?" for _ in fields)})',
            [psd[field] for field in fields],
        )
        decision_fields = table_fields(target, "reconciliation_decisions")
        note = "Curator correction: PSD selection was a mistake; prior JPEG restored as the single Master. No deletion is authorized."
        for path, disposition in ((str(PSD_SOURCE), "redundant"), (str(JPEG_SOURCE), "selected_master")):
            values = {field: "" for field in decision_fields}
            values.update(
                {
                    "source_path": path,
                    "group_id": f"object-census-correction:{TARGET_CANDIDATE}",
                    "disposition": disposition,
                    "master_path": str(JPEG_SOURCE),
                    "note": note,
                    "actor": "vladimir",
                    "decided_at": decided_at,
                    "deletion_authorized": "false",
                }
            )
            target.execute(
                "INSERT INTO reconciliation_decisions VALUES ("
                + ",".join("?" for _ in decision_fields)
                + ")",
                [values[field] for field in decision_fields],
            )
        target.commit()
    except Exception:
        target.rollback()
        target.close()
        predecessor.close()
        temporary_db.unlink(missing_ok=True)
        raise
    predecessor.close()
    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
        target.close()
        temporary_db.unlink(missing_ok=True)
        raise RevertError("reverted published database failed integrity")
    if target.execute("SELECT COUNT(*) FROM source_disposition WHERE deletion_authorized<>'false'").fetchone()[0]:
        target.close()
        temporary_db.unlink(missing_ok=True)
        raise RevertError("reverted published database authorizes deletion")
    obj = target.execute(
        "SELECT master_relative_path,master_sha256,selected_source_path FROM objects WHERE record_key=?",
        (TARGET_RECORD,),
    ).fetchone()
    if tuple(obj) != (JPEG_MASTER_RELATIVE, JPEG_MASTER_HASH, str(JPEG_SOURCE)):
        target.close()
        temporary_db.unlink(missing_ok=True)
        raise RevertError("reverted object does not bind the prior JPEG")

    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    matching = [row for row in catalog if row["record_key"] == TARGET_RECORD]
    if len(matching) != 2 or {row["file_role"] for row in matching} != {"preservation_master", "access_derivative"}:
        raise RevertError("catalog target rows drifted")
    old_object = row_dict(
        target.execute("SELECT * FROM objects WHERE record_key=?", (TARGET_RECORD,)).fetchone()
    )
    old_representations = {
        str(row["role"]): row_dict(row)
        for row in target.execute("SELECT * FROM representations WHERE record_keys=?", (TARGET_RECORD,))
    }
    for row in matching:
        representation = old_representations[row["file_role"]]
        row["master_relative_path"] = JPEG_MASTER_RELATIVE
        row["original_source_path"] = str(JPEG_SOURCE)
        row["access_derivative_status"] = str(old_object["access_derivative_status"])
        row["final_relative_path"] = str(representation["final_relative_path"])
        row["filename"] = Path(str(representation["final_relative_path"])).name
        row["file_type"] = Path(str(representation["final_relative_path"])).suffix.lstrip(".").casefold()
        row["size_bytes"] = str(representation["size_bytes"])
        row["final_sha256"] = str(representation["final_sha256"])

    controls: dict[Path, bytes] = {
        UNIT / "CATALOG.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "db-restore/RESTORE.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "CATALOG.xlsx": paragon_document_bundle.table_xlsx_bytes(
            catalog_fields, catalog, sheet_name="Catalog", table_name="Catalog"
        ),
        UNIT / "db-restore/RESTORE.xlsx": paragon_document_bundle.table_xlsx_bytes(
            catalog_fields, catalog, sheet_name="Restore", table_name="Restore"
        ),
    }
    for table, path, order in (
        ("checksums", UNIT / "db-restore/checksums.csv", "rowid"),
        ("representations", UNIT / "db-restore/manifests/representations.csv", 'CAST("index" AS INTEGER)'),
        ("source_disposition", UNIT / "db-restore/manifests/source-disposition.csv", "rowid"),
    ):
        fields = table_fields(target, table)
        controls[path] = csv_bytes(fields, table_rows(target, table, order))
    sql_stream = io.StringIO()
    for line in target.iterdump():
        sql_stream.write(line + "\n")
    controls[UNIT / "db-restore/RESTORE.sql"] = sql_stream.getvalue().encode("utf-8")
    target.close()
    fsync_file(temporary_db)
    controls[PUBLISHED_DB] = temporary_db.read_bytes()
    return controls, temporary_db


CENSUS_FIELDS = (
    "candidate_id", "filename", "primary_source_path", "source_paths_json",
    "content_sha256", "size_bytes", "queue_class", "decision_id", "disposition",
    "target_record_key", "retained_candidate_id", "published_record_key",
    "selected_master_path", "accession_state", "actor", "reason", "decided_at",
    "curated_title", "curated_date_text", "curated_description", "curated_tags",
    "cross_unit_hold_json", "deletion_authorized",
)


def build_census_manifest(reconciliation_path: Path, published_path: Path) -> tuple[bytes, dict[str, int]]:
    reconciliation = sqlite3.connect(f"file:{reconciliation_path}?mode=ro", uri=True)
    published = sqlite3.connect(f"file:{published_path}?mode=ro", uri=True)
    reconciliation.row_factory = published.row_factory = sqlite3.Row
    try:
        candidates = {
            str(row["candidate_id"]): row_dict(row)
            for row in reconciliation.execute("SELECT * FROM object_census_candidates")
        }
        decisions = {
            str(row["candidate_id"]): row_dict(row)
            for row in reconciliation.execute(
                """SELECT * FROM object_census_decisions d WHERE decision_id=(
                       SELECT MAX(d2.decision_id) FROM object_census_decisions d2
                        WHERE d2.candidate_id=d.candidate_id)"""
            )
        }
        if len(candidates) != 1032 or set(candidates) != set(decisions):
            raise RevertError("corrected census candidate/decision binding drifted")
        dispositions = Counter(str(row["disposition"]) for row in decisions.values())
        expected = Counter(
            {
                "context_not_object": 4,
                "cross_unit_hold": 389,
                "existing_object": 163,
                "new_local_metadata_right": 270,
                "new_local_metadata_wrong": 4,
                "redundant": 45,
                "same_new_candidate": 126,
                "surrogate_keep": 4,
                "surrogate_master": 27,
            }
        )
        if dispositions != expected:
            raise RevertError(f"corrected census dispositions drifted: {dispositions}")
        similarity = [
            row_dict(row)
            for row in reconciliation.execute(
                """SELECT * FROM object_census_similarity_decisions d WHERE decision_id=(
                       SELECT MAX(d2.decision_id) FROM object_census_similarity_decisions d2
                        WHERE d2.pair_id=d.pair_id) AND outcome='same_photo'"""
            )
        ]
        retained_by_companion = {
            str(row["companion_candidate_id"]): str(row["primary_candidate_id"])
            for row in similarity
            if row["companion_candidate_id"] and row["primary_candidate_id"]
        }
        objects = {
            str(row["record_key"]): row_dict(row)
            for row in published.execute("SELECT * FROM objects")
        }
        master_source = {
            str(row["record_keys"]): str(row["source_path"])
            for row in published.execute(
                "SELECT record_keys,source_path FROM representations WHERE role='preservation_master'"
            )
        }
        rows: list[dict[str, Any]] = []
        for candidate_id in sorted(candidates):
            candidate = candidates[candidate_id]
            decision = decisions[candidate_id]
            disposition = str(decision["disposition"])
            target = str(decision["target_record_key"] or "")
            retained = retained_by_companion.get(candidate_id, "")
            published_key = ""
            master_path = ""
            state = "not_accessioned_by_decision"
            if disposition == "new_local_metadata_right":
                published_key, state = candidate_id, "accessioned_new_local_master"
            elif disposition == "surrogate_master":
                published_key, state = target, "accessioned_selected_master"
            elif disposition == "surrogate_keep":
                published_key, state = target, "retained_existing_master"
            elif disposition == "existing_object":
                published_key, state = target, "redundant_to_existing_object"
            elif disposition in {"same_new_candidate", "redundant"}:
                published_key = retained if retained in objects else ""
                state = "reviewed_redundant"
            elif disposition == "new_local_metadata_wrong":
                state = "not_accessioned_metadata_unresolved"
            elif disposition == "cross_unit_hold":
                state = "not_accessioned_cross_unit_hold"
            elif disposition == "context_not_object":
                state = "not_accessioned_context"
            if published_key:
                if published_key not in objects:
                    raise RevertError(f"decision target is absent: {candidate_id}")
                master_path = str(objects[published_key]["master_relative_path"])
            if disposition == "surrogate_master" and master_source.get(published_key) != candidate["primary_source_path"]:
                raise RevertError(f"surrogate Master is not reflected: {candidate_id}")
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "filename": candidate["filename"],
                    "primary_source_path": candidate["primary_source_path"],
                    "source_paths_json": candidate["source_paths_json"],
                    "content_sha256": candidate["content_sha256"],
                    "size_bytes": candidate["size_bytes"],
                    "queue_class": candidate["queue_class"],
                    "decision_id": decision["decision_id"],
                    "disposition": disposition,
                    "target_record_key": target,
                    "retained_candidate_id": retained,
                    "published_record_key": published_key,
                    "selected_master_path": master_path,
                    "accession_state": state,
                    "actor": decision["actor"],
                    "reason": decision["reason"] or "",
                    "decided_at": decision["decided_at"],
                    "curated_title": decision["curated_title"] or "",
                    "curated_date_text": decision["curated_date_text"] or "",
                    "curated_description": decision["curated_description"] or "",
                    "curated_tags": decision["curated_tags"] or "",
                    "cross_unit_hold_json": decision["cross_unit_hold_json"] or "",
                    "deletion_authorized": "false",
                }
            )
        return csv_bytes(list(CENSUS_FIELDS), rows), dict(sorted(dispositions.items()))
    finally:
        reconciliation.close()
        published.close()


def stage_media() -> tuple[Path, Path]:
    master = SCRIPT_DIR / f".md95019-jpeg-master-{os.getpid()}.tmp"
    access = SCRIPT_DIR / f".md95019-jpeg-access-{os.getpid()}.tmp"
    for source, destination, expected in (
        (PREDECESSOR_MASTER, master, JPEG_MASTER_HASH),
        (PREDECESSOR_ACCESS, access, JPEG_ACCESS_HASH),
    ):
        if destination.exists():
            raise RevertError(f"unexpected media temporary: {destination}")
        shutil.copyfile(source, destination)
        fsync_file(destination)
        if sha256_file(destination) != expected:
            raise RevertError(f"staged media hash mismatch: {destination}")
    predecessor = sqlite3.connect(f"file:{BEFORE_PUBLISHED_DB}?mode=ro&immutable=1", uri=True)
    predecessor.row_factory = sqlite3.Row
    try:
        mtimes = {
            str(row["role"]): int(row["mtime_ns"])
            for row in predecessor.execute(
                "SELECT role,mtime_ns FROM representations WHERE record_keys=?", (TARGET_RECORD,)
            )
        }
    finally:
        predecessor.close()
    os.utime(master, ns=(mtimes["preservation_master"], mtimes["preservation_master"]))
    os.utime(access, ns=(mtimes["access_derivative"], mtimes["access_derivative"]))
    return master, access


def publish_controls(controls: dict[Path, bytes]) -> None:
    staged: list[tuple[Path, Path]] = []
    for destination, payload in controls.items():
        if destination == PUBLISHED_DB:
            continue
        temporary = SCRIPT_DIR / f".md95019-control-{len(staged):02d}-{os.getpid()}.tmp"
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged.append((temporary, destination))
    for temporary, destination in staged:
        os.replace(temporary, destination)
        fsync_dir(destination.parent)


def apply() -> dict[str, Any]:
    if COMPLETION.exists():
        return json.loads(COMPLETION.read_text(encoding="utf-8"))
    preflight_result = preflight()
    decided_at = now()
    reconciliation_candidate, decision_id = build_reconciliation_candidate(decided_at)
    controls, published_candidate = build_published_controls(decided_at)
    staged_master, staged_access = stage_media()
    census_payload, dispositions = build_census_manifest(reconciliation_candidate, published_candidate)

    sqlite_backup(PUBLISHED_DB, PUBLISHED_DB_BACKUP)
    sqlite_backup(RECONCILIATION, RECONCILIATION_BACKUP)
    write_once(BEFORE_UNIT_CENSUS, UNIT_CENSUS.read_bytes())
    write_once(BEFORE_WORK_CENSUS, WORK_CENSUS.read_bytes())
    write_once(BEFORE_CENSUS_COMPLETION, CURRENT_CENSUS_COMPLETION.read_bytes())

    os.replace(UNIT / PSD_MASTER_RELATIVE, MISTAKEN_PSD_BACKUP)
    fsync_dir((UNIT / PSD_MASTER_RELATIVE).parent)
    os.replace(staged_master, UNIT / JPEG_MASTER_RELATIVE)
    fsync_dir((UNIT / JPEG_MASTER_RELATIVE).parent)
    os.replace(UNIT / ACCESS_RELATIVE, MISTAKEN_ACCESS_BACKUP)
    fsync_dir((UNIT / ACCESS_RELATIVE).parent)
    os.replace(staged_access, UNIT / ACCESS_RELATIVE)
    fsync_dir((UNIT / ACCESS_RELATIVE).parent)
    os.replace(published_candidate, PUBLISHED_DB)
    fsync_dir(PUBLISHED_DB.parent)
    publish_controls(controls)
    os.replace(reconciliation_candidate, RECONCILIATION)
    fsync_dir(RECONCILIATION.parent)

    for path in (UNIT_CENSUS, WORK_CENSUS, CORRECTION_CENSUS):
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        with temporary.open("xb") as handle:
            handle.write(census_payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    manifest_hash = "sha256:" + hashlib.sha256(census_payload).hexdigest()
    census_completion = {
        "format": "iss-kurt-schwarz-completed-object-census-manifest-v1",
        "completed_at": decided_at,
        "result": "ok",
        "candidates": 1032,
        "latest_dispositions": dispositions,
        "manifest_sha256": manifest_hash,
        "work_manifest": str(WORK_CENSUS),
        "unit_manifest": str(UNIT_CENSUS),
        "correction": "md:95019 PSD selection reverted to prior JPEG Master",
        "deletion_authorized": False,
    }
    completion_payload = (json.dumps(census_completion, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    temporary = CURRENT_CENSUS_COMPLETION.with_name(f".{CURRENT_CENSUS_COMPLETION.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(completion_payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, CURRENT_CENSUS_COMPLETION)
    fsync_dir(CURRENT_CENSUS_COMPLETION.parent)

    validate_database(PUBLISHED_DB)
    validate_database(RECONCILIATION)
    if sha256_file(UNIT / JPEG_MASTER_RELATIVE) != JPEG_MASTER_HASH:
        raise RevertError("published JPEG Master hash mismatch")
    if sha256_file(UNIT / ACCESS_RELATIVE) != JPEG_ACCESS_HASH:
        raise RevertError("published JPEG access hash mismatch")
    if sha256_file(MISTAKEN_PSD_BACKUP) != PSD_MASTER_HASH:
        raise RevertError("preserved mistaken PSD hash mismatch")
    if sha256_file(MISTAKEN_ACCESS_BACKUP) != PSD_ACCESS_HASH:
        raise RevertError("preserved mistaken PSD access hash mismatch")
    if (UNIT / PSD_MASTER_RELATIVE).exists():
        raise RevertError("mistaken PSD still occupies the Master directory")
    if sum(path.is_file() for path in (UNIT / "masters").iterdir()) != 1935:
        raise RevertError("published Master count is wrong")
    if sum(path.is_file() for path in (UNIT / "export/access").iterdir()) != 1935:
        raise RevertError("published access count is wrong")
    for path in (UNIT / "CATALOG.xlsx", UNIT / "db-restore/RESTORE.xlsx"):
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise RevertError(f"invalid XLSX: {path}")
    rebuilt = sqlite3.connect(":memory:")
    try:
        rebuilt.executescript((UNIT / "db-restore/RESTORE.sql").read_text(encoding="utf-8"))
        if rebuilt.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RevertError("RESTORE.sql rebuild failed")
    finally:
        rebuilt.close()
    mount = source_mount()
    result = {
        "format": "iss-kurt-schwarz-md95019-jpeg-master-correction-v1",
        "completed_at": now(),
        "result": "ok",
        "record_key": TARGET_RECORD,
        "candidate_id": TARGET_CANDIDATE,
        "correction_decision_id": decision_id,
        "master_relative_path": JPEG_MASTER_RELATIVE,
        "master_sha256": JPEG_MASTER_HASH,
        "access_relative_path": ACCESS_RELATIVE,
        "access_sha256": JPEG_ACCESS_HASH,
        "redundant_source": str(PSD_SOURCE),
        "mistaken_psd_preserved": str(MISTAKEN_PSD_BACKUP),
        "mistaken_access_preserved": str(MISTAKEN_ACCESS_BACKUP),
        "published_database_backup": str(PUBLISHED_DB_BACKUP),
        "reconciliation_database_backup": str(RECONCILIATION_BACKUP),
        "object_census_manifest_sha256": manifest_hash,
        "source_mount": mount,
        "source_read_only": True,
        "deletion_authorized": False,
        "preflight": preflight_result,
    }
    write_once(COMPLETION, (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = apply() if args.apply else preflight()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str))
        return 0
    except (RevertError, OSError, ValueError, csv.Error, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
