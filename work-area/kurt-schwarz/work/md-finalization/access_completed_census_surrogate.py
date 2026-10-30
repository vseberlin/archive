#!/usr/bin/env python3
"""Apply the one remaining Kurt Schwarz surrogate-Master census decision."""

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SOURCE = Path("/media/vladimir/Elements")
RECONCILIATION = WORK / "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
SCRIPT_DIR = WORK / "work/md-finalization"
TARGET_RECORD = "md:95019"
TARGET_CANDIDATE = "KSC-cc87203305645d2d0677"
TARGET_DECISION = 1199
OLD_MASTER_RELATIVE = "masters/KS-7-WF_0056.jpg"
NEW_MASTER_RELATIVE = "masters/KS-7-WF_0056.psd"
ACCESS_RELATIVE = "export/access/KS-7-WF_0056.jpg"
EXPECTED_SOURCE_HASH = "sha256:9ddea3a9ff5df059c428ce1c7c9ef3fb49f36f90545aab7243bd984d7cdfa226"
EXPECTED_SOURCE_SIZE = 21_497_337
EXPECTED_OBJECTS = 1935
EXPECTED_REPRESENTATIONS = 3870

MANIFEST = SCRIPT_DIR / "completed-object-census-surrogate-master.json"
COMPLETION = SCRIPT_DIR / "completed-object-census-surrogate-master-completion.json"
BEFORE_DB = SCRIPT_DIR / "completed-object-census-surrogate-before.sqlite"
OLD_MASTER_BACKUP = SCRIPT_DIR / "completed-object-census-surrogate-before-master.jpg"
OLD_ACCESS_BACKUP = SCRIPT_DIR / "completed-object-census-surrogate-before-access.jpg"

TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))
import kurt_schwarz_metadata_derivatives as metadata_tools  # noqa: E402
import paragon_document_bundle  # noqa: E402


class AccessionError(RuntimeError):
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
            raise AccessionError(f"existing artifact differs: {path}")
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
    first = output.splitlines()[0]
    fields = first.split(maxsplit=3)
    options = set(fields[3].split(",")) if len(fields) == 4 else set()
    if fields[:3] != ["/dev/sdf1", str(SOURCE), "ntfs3"] or "ro" not in options or "rw" in options:
        raise AccessionError(f"Elements is not safely read-only: {output}")
    return first


def table_fields(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


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


def load_decision() -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{RECONCILIATION}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AccessionError("reconciliation quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AccessionError("reconciliation foreign keys failed")
        row = connection.execute(
            """SELECT c.*,d.decision_id,d.disposition,d.target_record_key,
                      d.actor,d.reason,d.decided_at
                 FROM object_census_candidates c
                 JOIN object_census_decisions d ON d.decision_id=(
                      SELECT MAX(d2.decision_id) FROM object_census_decisions d2
                       WHERE d2.candidate_id=c.candidate_id)
                WHERE c.candidate_id=?""",
            (TARGET_CANDIDATE,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AccessionError("surrogate candidate is absent")
    decision = dict(row)
    if (
        int(decision["decision_id"]) != TARGET_DECISION
        or decision["disposition"] != "surrogate_master"
        or decision["target_record_key"] != TARGET_RECORD
        or decision["content_sha256"] != EXPECTED_SOURCE_HASH
        or int(decision["size_bytes"]) != EXPECTED_SOURCE_SIZE
    ):
        raise AccessionError("surrogate decision binding drifted")
    return decision


def preflight(decision: dict[str, Any]) -> dict[str, Any]:
    mount = source_mount()
    source = Path(decision["primary_source_path"])
    if not source.is_file() or not source.resolve().is_relative_to(SOURCE.resolve()):
        raise AccessionError("selected surrogate Master is absent or outside Elements")
    if source.stat().st_size != EXPECTED_SOURCE_SIZE or sha256_file(source) != EXPECTED_SOURCE_HASH:
        raise AccessionError("selected surrogate Master bytes drifted")
    old_master = UNIT / OLD_MASTER_RELATIVE
    old_access = UNIT / ACCESS_RELATIVE
    new_master = UNIT / NEW_MASTER_RELATIVE
    for path in (old_master, old_access):
        if not path.is_file():
            raise AccessionError(f"published predecessor is absent: {path}")
    if new_master.exists():
        raise AccessionError(f"new Master destination already exists: {new_master}")
    if any(path.exists() for path in (BEFORE_DB, OLD_MASTER_BACKUP, OLD_ACCESS_BACKUP, COMPLETION)):
        raise AccessionError("surrogate accession artifact already exists without a completion receipt")

    connection = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AccessionError("published database quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AccessionError("published database foreign keys failed")
        counts = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("objects", "representations", "checksums", "source_disposition")
        }
        obj = connection.execute("SELECT * FROM objects WHERE record_key=?", (TARGET_RECORD,)).fetchone()
        master = connection.execute(
            "SELECT * FROM representations WHERE role='preservation_master' AND record_keys=?",
            (TARGET_RECORD,),
        ).fetchone()
        access = connection.execute(
            "SELECT * FROM representations WHERE role='access_derivative' AND record_keys=?",
            (TARGET_RECORD,),
        ).fetchone()
    finally:
        connection.close()
    if counts != {
        "objects": EXPECTED_OBJECTS,
        "representations": EXPECTED_REPRESENTATIONS,
        "checksums": EXPECTED_REPRESENTATIONS,
        "source_disposition": 17077,
    }:
        raise AccessionError(f"published counts drifted: {counts}")
    if obj is None or master is None or access is None:
        raise AccessionError("target object or representation is absent")
    if obj["master_relative_path"] != OLD_MASTER_RELATIVE or master["final_relative_path"] != OLD_MASTER_RELATIVE:
        raise AccessionError("published predecessor Master binding drifted")
    if master["source_path"] == decision["primary_source_path"]:
        raise AccessionError("surrogate Master decision is already represented")
    return {
        "result": "ok",
        "source_mount": mount,
        "record_key": TARGET_RECORD,
        "candidate_id": TARGET_CANDIDATE,
        "decision_id": TARGET_DECISION,
        "old_master_relative_path": OLD_MASTER_RELATIVE,
        "new_master_relative_path": NEW_MASTER_RELATIVE,
        "access_relative_path": ACCESS_RELATIVE,
        "selected_master_bytes": EXPECTED_SOURCE_SIZE,
        "deletion_authorized": False,
    }


def build_controls(
    decision: dict[str, Any], master_stat: os.stat_result, access_hash: str, access_stat: os.stat_result
) -> dict[Path, bytes]:
    restore_path = UNIT / "db-restore/RESTORE.sqlite"
    source = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    untouched_before = [
        tuple(row)
        for row in source.execute("SELECT * FROM objects WHERE record_key<>? ORDER BY record_key", (TARGET_RECORD,))
    ]
    temporary_db = SCRIPT_DIR / f".surrogate-RESTORE.tmp-{os.getpid()}.sqlite"
    if temporary_db.exists():
        raise AccessionError(f"unexpected temporary database: {temporary_db}")
    target = sqlite3.connect(temporary_db)
    source.backup(target)
    source.close()
    target.row_factory = sqlite3.Row

    old_master_source = target.execute(
        "SELECT source_path FROM representations WHERE role='preservation_master' AND record_keys=?",
        (TARGET_RECORD,),
    ).fetchone()[0]
    completed_at = now()
    target.execute(
        """UPDATE objects SET selected_source_path=?,selected_source_format='psd',
                  selection_basis='object_census_curator_decision',
                  selection_state='curator_selected_master',
                  master_relative_path=?,master_sha256=?,access_relative_path=?,
                  access_derivative_status='generated_full_resolution_jpeg_q95'
             WHERE record_key=?""",
        (decision["primary_source_path"], NEW_MASTER_RELATIVE, EXPECTED_SOURCE_HASH, ACCESS_RELATIVE, TARGET_RECORD),
    )
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise AccessionError("target object update did not bind exactly one row")
    target.execute(
        """UPDATE representations SET completed_at=?,representation_key=?,source_kind='file',
                  source_path=?,source_member='',source_sha256=?,final_sha256=?,
                  final_relative_path=?,size_bytes=?,mtime_ns=?,copy_policy='verified_exact_copy',
                  basis='curator_confirmed_higher_resolution_master',relationship='approved_master_replacement'
             WHERE role='preservation_master' AND record_keys=?""",
        (completed_at, EXPECTED_SOURCE_HASH, decision["primary_source_path"], EXPECTED_SOURCE_HASH,
         EXPECTED_SOURCE_HASH, NEW_MASTER_RELATIVE, str(master_stat.st_size), str(master_stat.st_mtime_ns), TARGET_RECORD),
    )
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise AccessionError("Master representation update did not bind exactly one row")
    target.execute(
        """UPDATE representations SET completed_at=?,representation_key=?,source_kind='generated_from_master',
                  source_path=?,source_member=?,source_sha256=?,final_sha256=?,
                  final_relative_path=?,size_bytes=?,mtime_ns=?,
                  copy_policy='generated_full_resolution_jpeg_q95',
                  basis='libvips_full_native_dimensions_metadata_embedded',relationship=?
             WHERE role='access_derivative' AND record_keys=?""",
        (completed_at, access_hash, decision["primary_source_path"], NEW_MASTER_RELATIVE,
         EXPECTED_SOURCE_HASH, access_hash, ACCESS_RELATIVE, str(access_stat.st_size),
         str(access_stat.st_mtime_ns), f"access_derivative_of:{NEW_MASTER_RELATIVE}", TARGET_RECORD),
    )
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise AccessionError("access representation update did not bind exactly one row")
    target.execute(
        "UPDATE checksums SET path=?,sha256=?,size_bytes=?,content_role='preservation_master' WHERE path=?",
        (NEW_MASTER_RELATIVE, EXPECTED_SOURCE_HASH, str(master_stat.st_size), OLD_MASTER_RELATIVE),
    )
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise AccessionError("Master checksum update did not bind exactly one row")
    target.execute(
        "UPDATE checksums SET sha256=?,size_bytes=?,content_role='access_derivative' WHERE path=?",
        (access_hash, str(access_stat.st_size), ACCESS_RELATIVE),
    )
    if target.execute("SELECT changes()").fetchone()[0] != 1:
        raise AccessionError("access checksum update did not bind exactly one row")

    for path, role, status, exact in (
        (decision["primary_source_path"], "preservation_master", "preserved_exact_selected_surrogate_master", "true"),
        (old_master_source, "redundant_surrogate", "retained_source_redundant_after_master_selection", "false"),
    ):
        target.execute(
            """UPDATE source_disposition SET source_role=?,final_path=?,preservation_status=?,
                      preservation_basis='curator_confirmed_higher_resolution_master',final_sha256=?,
                      exact_copy_proven=?,drain_state='retain_reviewed_redundant',
                      deletion_authorized='false',notes=? WHERE source_path=?""",
            (role, NEW_MASTER_RELATIVE, status, EXPECTED_SOURCE_HASH, exact,
             "Completed object-census Master decision; source remains protected and no deletion is authorized.", path),
        )
        if target.execute("SELECT changes()").fetchone()[0] != 1:
            raise AccessionError(f"source disposition did not bind exactly one row: {path}")

    decision_fields = table_fields(target, "reconciliation_decisions")
    for path, disposition, master_path in (
        (decision["primary_source_path"], "selected_master", decision["primary_source_path"]),
        (old_master_source, "redundant", decision["primary_source_path"]),
    ):
        values = {field: "" for field in decision_fields}
        values.update(
            {
                "source_path": path,
                "group_id": f"object-census:{TARGET_CANDIDATE}",
                "disposition": disposition,
                "master_path": master_path,
                "note": decision["reason"] or "Curator-confirmed surrogate Master selection",
                "actor": decision["actor"],
                "decided_at": decision["decided_at"],
                "deletion_authorized": "false",
            }
        )
        target.execute(
            "INSERT INTO reconciliation_decisions VALUES (" + ",".join("?" for _ in decision_fields) + ")",
            [values[field] for field in decision_fields],
        )

    target.commit()
    if target.execute("PRAGMA quick_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
        raise AccessionError("generated database integrity failed")
    untouched_after = [
        tuple(row)
        for row in target.execute("SELECT * FROM objects WHERE record_key<>? ORDER BY record_key", (TARGET_RECORD,))
    ]
    if untouched_after != untouched_before:
        raise AccessionError("an unrelated object row changed")
    for table, expected in (("objects", EXPECTED_OBJECTS), ("representations", EXPECTED_REPRESENTATIONS),
                            ("checksums", EXPECTED_REPRESENTATIONS), ("source_disposition", 17077)):
        if target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != expected:
            raise AccessionError(f"generated count drifted: {table}")
    if target.execute("SELECT COUNT(*) FROM source_disposition WHERE deletion_authorized<>'false'").fetchone()[0]:
        raise AccessionError("generated database authorizes source deletion")
    target.close()
    fsync_file(temporary_db)

    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    matching = [row for row in catalog if row["record_key"] == TARGET_RECORD]
    if len(matching) != 2 or {row["file_role"] for row in matching} != {"preservation_master", "access_derivative"}:
        raise AccessionError("published catalog target rows drifted")
    for row in matching:
        row["master_relative_path"] = NEW_MASTER_RELATIVE
        row["original_source_path"] = decision["primary_source_path"]
        row["access_derivative_status"] = "generated_full_resolution_jpeg_q95"
        if row["file_role"] == "preservation_master":
            row.update({"final_relative_path": NEW_MASTER_RELATIVE, "filename": Path(NEW_MASTER_RELATIVE).name,
                        "file_type": "psd", "size_bytes": str(master_stat.st_size), "final_sha256": EXPECTED_SOURCE_HASH})
        else:
            row.update({"final_relative_path": ACCESS_RELATIVE, "filename": Path(ACCESS_RELATIVE).name,
                        "file_type": "jpg", "size_bytes": str(access_stat.st_size), "final_sha256": access_hash})

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
    generated = sqlite3.connect(f"file:{temporary_db}?mode=ro", uri=True)
    generated.row_factory = sqlite3.Row
    for table, path, order in (
        ("checksums", UNIT / "db-restore/checksums.csv", "rowid"),
        ("representations", UNIT / "db-restore/manifests/representations.csv", 'CAST("index" AS INTEGER)'),
        ("source_disposition", UNIT / "db-restore/manifests/source-disposition.csv", "rowid"),
    ):
        fields = table_fields(generated, table)
        controls[path] = csv_bytes(fields, table_rows(generated, table, order))
    sql_stream = io.StringIO()
    for line in generated.iterdump():
        sql_stream.write(line + "\n")
    generated.close()
    controls[UNIT / "db-restore/RESTORE.sql"] = sql_stream.getvalue().encode("utf-8")
    controls[restore_path] = temporary_db.read_bytes()
    temporary_db.unlink()
    return controls


def publish_controls(controls: dict[Path, bytes]) -> None:
    staged: list[tuple[Path, Path]] = []
    for destination, payload in controls.items():
        temporary = SCRIPT_DIR / f".surrogate-control-{len(staged):02d}-{os.getpid()}.tmp"
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged.append((temporary, destination))
    for temporary, destination in staged:
        os.replace(temporary, destination)
        fsync_dir(destination.parent)


def apply(decision: dict[str, Any]) -> dict[str, Any]:
    if COMPLETION.exists():
        return json.loads(COMPLETION.read_text(encoding="utf-8"))
    preflight_result = preflight(decision)
    write_once(MANIFEST, (json.dumps({**preflight_result, "decision": decision}, ensure_ascii=False,
                                     sort_keys=True, indent=2, default=str) + "\n").encode("utf-8"))

    source = Path(decision["primary_source_path"])
    staged_master = SCRIPT_DIR / f".surrogate-master-{os.getpid()}.psd"
    staged_access = SCRIPT_DIR / f".surrogate-access-{os.getpid()}.jpg"
    if staged_master.exists() or staged_access.exists():
        raise AccessionError("unexpected staged media exists")
    shutil.copyfile(source, staged_master)
    fsync_file(staged_master)
    if staged_master.stat().st_size != EXPECTED_SOURCE_SIZE or sha256_file(staged_master) != EXPECTED_SOURCE_HASH:
        raise AccessionError("staged Master exact-copy validation failed")

    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    master_catalog = next(
        row for row in catalog if row["record_key"] == TARGET_RECORD and row["file_role"] == "preservation_master"
    )
    master_catalog.update(
        {"master_relative_path": NEW_MASTER_RELATIVE, "final_relative_path": NEW_MASTER_RELATIVE,
         "filename": Path(NEW_MASTER_RELATIVE).name, "file_type": "psd",
         "size_bytes": str(EXPECTED_SOURCE_SIZE), "final_sha256": EXPECTED_SOURCE_HASH,
         "original_source_path": decision["primary_source_path"]}
    )
    rights = metadata_tools.exif_json(UNIT / OLD_MASTER_RELATIVE, metadata_tools.RIGHTS_TAGS)
    plan = metadata_tools.metadata_plan(master_catalog, EXPECTED_SOURCE_HASH)
    metadata_tools.generate_access(staged_master, staged_access)
    metadata_tools.write_metadata_in_place(staged_access, plan, rights)
    metadata_tools.verify_metadata(staged_access, plan, rights)
    source_width, source_height, orientation = metadata_tools.image_dimensions(staged_master)
    access_width, access_height, _ = metadata_tools.image_dimensions(staged_access)
    expected_dimensions = (source_height, source_width) if orientation in {5, 6, 7, 8} else (source_width, source_height)
    if (access_width, access_height) != expected_dimensions:
        raise AccessionError("generated access derivative dimensions changed")
    access_hash = sha256_file(staged_access)
    fsync_file(staged_access)
    controls = build_controls(decision, staged_master.stat(), access_hash, staged_access.stat())

    current_db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    backup_db = sqlite3.connect(BEFORE_DB)
    current_db.backup(backup_db)
    current_db.close()
    backup_db.close()
    fsync_file(BEFORE_DB)
    os.replace(UNIT / OLD_MASTER_RELATIVE, OLD_MASTER_BACKUP)
    fsync_dir((UNIT / OLD_MASTER_RELATIVE).parent)
    os.replace(staged_master, UNIT / NEW_MASTER_RELATIVE)
    fsync_dir((UNIT / NEW_MASTER_RELATIVE).parent)
    os.replace(UNIT / ACCESS_RELATIVE, OLD_ACCESS_BACKUP)
    fsync_dir((UNIT / ACCESS_RELATIVE).parent)
    os.replace(staged_access, UNIT / ACCESS_RELATIVE)
    fsync_dir((UNIT / ACCESS_RELATIVE).parent)
    publish_controls(controls)

    connection = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AccessionError("published database integrity failed")
        obj = connection.execute(
            "SELECT master_relative_path,master_sha256,selected_source_path FROM objects WHERE record_key=?",
            (TARGET_RECORD,),
        ).fetchone()
        if obj != (NEW_MASTER_RELATIVE, EXPECTED_SOURCE_HASH, decision["primary_source_path"]):
            raise AccessionError("published object does not reflect selected surrogate Master")
        if connection.execute("SELECT COUNT(*) FROM source_disposition WHERE deletion_authorized<>'false'").fetchone()[0]:
            raise AccessionError("published database authorizes source deletion")
    finally:
        connection.close()
    if sha256_file(UNIT / NEW_MASTER_RELATIVE) != EXPECTED_SOURCE_HASH:
        raise AccessionError("published Master hash mismatch")
    if sha256_file(UNIT / ACCESS_RELATIVE) != access_hash:
        raise AccessionError("published access hash mismatch")
    if sum(path.is_file() for path in (UNIT / "masters").iterdir()) != EXPECTED_OBJECTS:
        raise AccessionError("published Master count is wrong")
    if sum(path.is_file() for path in (UNIT / "export/access").iterdir()) != EXPECTED_OBJECTS:
        raise AccessionError("published access count is wrong")
    mount = source_mount()
    completion = {
        "format": "iss-kurt-schwarz-completed-census-surrogate-accession-v1",
        "completed_at": now(),
        "result": "ok",
        "record_key": TARGET_RECORD,
        "candidate_id": TARGET_CANDIDATE,
        "decision_id": TARGET_DECISION,
        "master_relative_path": NEW_MASTER_RELATIVE,
        "master_sha256": EXPECTED_SOURCE_HASH,
        "access_relative_path": ACCESS_RELATIVE,
        "access_sha256": access_hash,
        "source_mount": mount,
        "source_read_only": True,
        "predecessor_master_preserved": str(OLD_MASTER_BACKUP),
        "predecessor_access_preserved": str(OLD_ACCESS_BACKUP),
        "deletion_authorized": False,
    }
    write_once(COMPLETION, (json.dumps(completion, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    return completion


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        decision = load_decision()
        result = apply(decision) if args.apply else preflight(decision)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (AccessionError, OSError, ValueError, csv.Error, sqlite3.Error,
            subprocess.CalledProcessError, metadata_tools.WorkerError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
