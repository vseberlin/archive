#!/usr/bin/env python3
"""Publish the Kurt Schwarz subdivision catalogue correction without media changes."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))
import paragon_document_bundle  # noqa: E402


UNIT_ROOT = Path("/mnt/volume/iss-archive/units")
CURRENT = UNIT_ROOT / "kurt-schwarz"
CANDIDATE = UNIT_ROOT / "kurt-schwarz-subdivisions-candidate"
PREDECESSOR = UNIT_ROOT / "kurt-schwarz-pre-subdivisions"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
RUN_ROOT = (
    WORK_ROOT
    / "work/subdivision-finalization/kurt-schwarz-subdivisions-001"
)
PACKAGE = RUN_ROOT / "package"
PREVIOUS_EVIDENCE = (
    UNIT_ROOT
    / "kurt-schwarz-md-final-refresh/db-restore/manifests/evidence"
)
SOURCE_DEVICE = "/dev/sdf1"
KURT_SUBDIVISION_IDS = {str(value) for value in range(846, 857)}
MD_COLLECTION_IDS = KURT_SUBDIVISION_IDS | {"555", "783"}

REGISTER_FIELDS = (
    "scheme_id",
    "subdivision_id",
    "parent_subdivision_id",
    "label",
    "subdivision_type",
    "source_path",
    "assigned_object_count",
    "assignment_state",
    "notes",
)
MEMBERSHIP_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "scheme_id",
    "subdivision_id",
    "label",
    "membership_basis",
)
CATALOG_ADDITIONS = ("md_subdivision_ids", "md_subdivisions")


class BuildError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_csv(path: Path) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = tuple(str(value or "") for value in (reader.fieldnames or ()))
        return fields, [dict(row) for row in reader]


def csv_bytes(rows: list[dict[str, str]], fields: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fields,
        extrasaction="raise",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


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
            raise BuildError(f"source device is not read-only: {mount}")
    return line


def split_pairs(ids: str, names: str) -> list[tuple[str, str]]:
    id_values = [value.strip() for value in ids.split(";") if value.strip()]
    name_values = [value.strip() for value in names.split(";") if value.strip()]
    if len(id_values) != len(name_values):
        raise BuildError(f"subdivision ID/name mismatch: {ids!r} / {names!r}")
    return list(zip(id_values, name_values))


def normalized_subdivisions(ids: str, names: str) -> tuple[str, str]:
    pairs = [pair for pair in split_pairs(ids, names) if pair[0] in KURT_SUBDIVISION_IDS]
    return "; ".join(pair[0] for pair in pairs), "; ".join(pair[1] for pair in pairs)


def link_flat_directory(source: Path, target: Path) -> int:
    target.mkdir(parents=True)
    count = 0
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"unexpected payload entry: {path}")
        os.link(path, target / path.name)
        count += 1
    return count


def copy_flat_directory(source: Path, target: Path) -> int:
    target.mkdir(parents=True)
    count = 0
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"unexpected metadata entry: {path}")
        shutil.copy2(path, target / path.name)
        count += 1
    return count


def create_table(
    connection: sqlite3.Connection,
    name: str,
    rows: list[dict[str, str]],
    fields: tuple[str, ...],
) -> None:
    connection.execute(
        f'CREATE TABLE "{name}" ('
        + ",".join(f'"{field}" TEXT NOT NULL' for field in fields)
        + ")"
    )
    connection.executemany(
        f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
        [[row[field] for field in fields] for row in rows],
    )


def validate_unit(root: Path, *, expect_subdivisions: bool) -> dict[str, int]:
    database = root / "db-restore/RESTORE.sqlite"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BuildError(f"SQLite quick_check failed: {root}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BuildError(f"SQLite foreign keys failed: {root}")
        expected = {
            "objects": 1665,
            "representations": 3322,
            "checksums": 3322,
            "source_disposition": 17077,
            "drain_list": 2339,
            "reconciliation_decisions": 2366,
            "object_aliases": 3,
            "access_derivative_gaps": 8,
        }
        for table, count in expected.items():
            actual = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if actual != count:
                raise BuildError(f"{root}: {table} count {actual} != {count}")
        object_rows = connection.execute(
            "SELECT record_key,subdivision_ids,subdivisions,master_relative_path "
            "FROM objects"
        ).fetchall()
        masters = {row["master_relative_path"] for row in object_rows}
        if len(masters) != 1665 or "" in masters:
            raise BuildError(f"{root}: one-Master invariant failed")
        if expect_subdivisions:
            table_counts = {
                "subdivision_register": 13,
                "subdivision_memberships": 2405,
            }
            for table, count in table_counts.items():
                actual = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if actual != count:
                    raise BuildError(f"{root}: {table} count {actual} != {count}")
            if connection.execute(
                "SELECT COUNT(*) FROM objects WHERE ';' || replace(subdivision_ids,' ','') || ';' "
                "LIKE '%;555;%'"
            ).fetchone()[0]:
                raise BuildError("collection 555 remains classified as a Kurt subdivision")
            row_555 = connection.execute(
                "SELECT parent_subdivision_id,subdivision_type FROM subdivision_register "
                "WHERE subdivision_id='md-collection:555'"
            ).fetchone()
            if tuple(row_555 or ()) != ("", "related_collection"):
                raise BuildError("collection 555 register classification is incorrect")
    finally:
        connection.close()

    representation_fields, representations = read_csv(
        root / "db-restore/manifests/representations.csv"
    )
    del representation_fields
    checksum_fields, checksums = read_csv(root / "db-restore/checksums.csv")
    del checksum_fields
    if {row["final_relative_path"] for row in representations} != {
        row["path"] for row in checksums
    }:
        raise BuildError(f"{root}: representations and checksums disagree")
    missing = [row["final_relative_path"] for row in representations if not (root / row["final_relative_path"]).is_file()]
    if missing:
        raise BuildError(f"{root}: missing representation paths: {missing[:3]}")
    if any(
        (root / row["path"]).stat().st_size != int(row["size_bytes"])
        for row in checksums
    ):
        raise BuildError(f"{root}: representation size mismatch")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise BuildError(f"{root}: symlink found")
    return {
        "objects": 1665,
        "masters": 1665,
        "access_derivatives": 1657,
        "representations": 3322,
        "checksums": 3322,
    }


def main() -> None:
    for path in (CANDIDATE, PREDECESSOR, RUN_ROOT):
        if path.exists():
            raise BuildError(f"refusing existing path: {path}")
    if not CURRENT.is_dir() or not PREVIOUS_EVIDENCE.is_dir():
        raise BuildError("current unit or frozen subdivision evidence is missing")
    if CURRENT.stat().st_dev != UNIT_ROOT.stat().st_dev:
        raise BuildError("published unit and unit root are on different filesystems")
    initial_source_state = source_state()
    validate_unit(CURRENT, expect_subdivisions=False)

    RUN_ROOT.mkdir(parents=True)
    PACKAGE.mkdir()
    masters = link_flat_directory(CURRENT / "masters", PACKAGE / "masters")
    access = link_flat_directory(CURRENT / "export/access", PACKAGE / "export/access")
    link_flat_directory(CURRENT / "export/md", PACKAGE / "export/md")
    copy_flat_directory(CURRENT / "documents", PACKAGE / "documents")
    copy_flat_directory(
        CURRENT / "db-restore/manifests",
        PACKAGE / "db-restore/manifests",
    )
    shutil.copy2(CURRENT / "db-restore/checksums.csv", PACKAGE / "db-restore/checksums.csv")

    register_fields, register_source = read_csv(
        PREVIOUS_EVIDENCE / "subdivision-register.csv"
    )
    membership_fields, membership_source = read_csv(
        PREVIOUS_EVIDENCE / "subdivision-memberships.csv"
    )
    if register_fields != REGISTER_FIELDS or membership_fields != MEMBERSHIP_FIELDS:
        raise BuildError("frozen subdivision manifest schema drifted")

    register_rows = []
    for source_row in register_source:
        if source_row["scheme_id"] != "museum-digital-collection":
            continue
        raw_id = source_row["subdivision_id"].removeprefix("md-collection:")
        if raw_id not in MD_COLLECTION_IDS:
            continue
        row = {field: source_row[field] for field in REGISTER_FIELDS}
        if raw_id == "555":
            row.update(
                {
                    "parent_subdivision_id": "",
                    "subdivision_type": "related_collection",
                    "notes": (
                        "Valid museum-digital co-membership in Pressebildarchiv WF; "
                        "preserved as a related collection and not classified as a Kurt subdivision."
                    ),
                }
            )
        register_rows.append(row)
    register_rows.sort(key=lambda row: int(row["subdivision_id"].split(":")[-1]))
    if len(register_rows) != 13:
        raise BuildError(f"expected 13 MD collection register rows, got {len(register_rows)}")

    membership_rows = [
        {field: row[field] for field in MEMBERSHIP_FIELDS}
        for row in membership_source
        if row["scheme_id"] == "museum-digital-collection"
        and row["subdivision_id"].removeprefix("md-collection:") in MD_COLLECTION_IDS
    ]
    membership_rows.sort(
        key=lambda row: (
            row["record_key"],
            int(row["subdivision_id"].split(":")[-1]),
        )
    )
    if len(membership_rows) != 2405:
        raise BuildError(f"expected 2405 MD collection memberships, got {len(membership_rows)}")
    membership_counts = Counter(
        row["subdivision_id"].removeprefix("md-collection:")
        for row in membership_rows
    )
    if sum(membership_counts[value] for value in KURT_SUBDIVISION_IDS) != 744:
        raise BuildError("Kurt subdivision membership count drifted")

    database_source = sqlite3.connect(
        f"file:{CURRENT / 'db-restore/RESTORE.sqlite'}?mode=ro",
        uri=True,
    )
    database_build_path = RUN_ROOT / "RESTORE.building.sqlite"
    database_build = sqlite3.connect(database_build_path)
    database_source.backup(database_build)
    database_source.close()
    database_build.row_factory = sqlite3.Row

    object_fields = tuple(
        row[1] for row in database_build.execute("PRAGMA table_info(objects)")
    )
    object_rows = [dict(row) for row in database_build.execute("SELECT * FROM objects")]
    subdivision_by_record: dict[str, tuple[str, str]] = {}
    for row in object_rows:
        ids, names = normalized_subdivisions(row["subdivision_ids"], row["subdivisions"])
        subdivision_by_record[row["record_key"]] = (ids, names)
        database_build.execute(
            "UPDATE objects SET subdivision_ids=?,subdivisions=? WHERE record_key=?",
            (ids, names, row["record_key"]),
        )
    create_table(database_build, "subdivision_register", register_rows, REGISTER_FIELDS)
    create_table(
        database_build,
        "subdivision_memberships",
        membership_rows,
        MEMBERSHIP_FIELDS,
    )
    database_build.commit()

    if database_build.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise BuildError("candidate restore SQLite integrity check failed")
    if database_build.execute("PRAGMA foreign_key_check").fetchall():
        raise BuildError("candidate restore SQLite foreign-key check failed")
    object_rows = [dict(row) for row in database_build.execute("SELECT * FROM objects")]
    populated = [row for row in object_rows if row["subdivision_ids"]]
    memberships = sum(
        len(split_pairs(row["subdivision_ids"], row["subdivisions"]))
        for row in object_rows
    )
    multiple = sum(
        len(split_pairs(row["subdivision_ids"], row["subdivisions"])) > 1
        for row in object_rows
    )
    if (len(populated), memberships, multiple) != (637, 744, 94):
        raise BuildError(
            "object subdivision projection drifted: "
            f"{len(populated)}, {memberships}, {multiple}"
        )
    sql_dump = "\n".join(database_build.iterdump()) + "\n"
    database_build.close()
    database_build_path.replace(PACKAGE / "db-restore/RESTORE.sqlite")
    write_new(PACKAGE / "db-restore/RESTORE.sql", sql_dump.encode("utf-8"))

    catalog_fields, catalog_rows = read_csv(CURRENT / "CATALOG.csv")
    if any(field in catalog_fields for field in CATALOG_ADDITIONS):
        raise BuildError("catalog already contains subdivision projection columns")
    catalog_fields = catalog_fields + CATALOG_ADDITIONS
    for row in catalog_rows:
        try:
            ids, names = subdivision_by_record[row["record_key"]]
        except KeyError as error:
            raise BuildError(f"catalog record lacks object row: {row['record_key']}") from error
        row["md_subdivision_ids"] = ids
        row["md_subdivisions"] = names
    catalog_payload = csv_bytes(catalog_rows, catalog_fields)
    write_new(PACKAGE / "CATALOG.csv", catalog_payload)
    write_new(PACKAGE / "db-restore/RESTORE.csv", catalog_payload)
    write_new(
        PACKAGE / "CATALOG.xlsx",
        paragon_document_bundle.table_xlsx_bytes(
            list(catalog_fields),
            catalog_rows,
            sheet_name="Catalog",
            table_name="Catalog",
        ),
    )
    write_new(
        PACKAGE / "db-restore/RESTORE.xlsx",
        paragon_document_bundle.table_xlsx_bytes(
            list(catalog_fields),
            catalog_rows,
            sheet_name="Restore",
            table_name="Restore",
        ),
    )
    write_new(
        PACKAGE / "db-restore/manifests/subdivision-register.csv",
        csv_bytes(register_rows, REGISTER_FIELDS),
    )
    write_new(
        PACKAGE / "db-restore/manifests/subdivision-memberships.csv",
        csv_bytes(membership_rows, MEMBERSHIP_FIELDS),
    )

    readme = """# Kurt Schwarz MD-reconciled archive unit

This is the published shallow Kurt Schwarz unit. It retains one selected Master
per accepted object and records reviewed Redundant and Noise files without
authorizing source deletion.

- Retained objects and Masters: 1665
- Generated access derivatives: 1657
- Explicit derivative gaps: 8
- Duplicate catalogue-record aliases: 3
- Museum-digital record relationships represented: 1668
- Kurt subdivision memberships: 744 across 637 objects
- Objects in multiple Kurt subdivisions: 94
- Deletion authorized: false

## Layout

`masters/` is flat and contains exactly one authoritative file per retained object.
`export/access/` contains the JPEG derivative generated from that Master when it
decoded. Eight Masters remain unchanged without an access derivative; see
`db-restore/manifests/access-derivative-gaps.csv`.

## Museum-digital subdivisions

Subdivisions are catalogue relationships, not filesystem folders. The collection
root and its subdivisions are documented in
`db-restore/manifests/subdivision-register.csv`; object memberships are in
`db-restore/manifests/subdivision-memberships.csv`. IDs 846 through 856 are Kurt
subdivisions. Collection 555, Pressebildarchiv WF, is retained as a related
museum-digital collection and is not classified as a Kurt subdivision.

`CATALOG.csv` and the paired XLSX/restore files expose each object's Kurt
subdivision IDs and names directly. Multiple values are separated by semicolon.

Recovery begins with the human-readable CSV/XLSX/Markdown records or
`db-restore/RESTORE.sqlite` and `RESTORE.sql`. The SQLite catalogue is rebuildable
from the archive records. `drain-list.csv` is review evidence only and never
authorizes deletion.
"""
    write_new(PACKAGE / "00_README.md", readme.encode("utf-8"))

    unchanged_files = (
        "db-restore/checksums.csv",
        "db-restore/manifests/access-derivative-gaps.csv",
        "db-restore/manifests/drain-list.csv",
        "db-restore/manifests/object-aliases.csv",
        "db-restore/manifests/reconciliation-decisions.csv",
        "db-restore/manifests/representations.csv",
        "db-restore/manifests/source-disposition.csv",
        "documents/CONTENTS.md",
    )
    for relative in unchanged_files:
        if (CURRENT / relative).read_bytes() != (PACKAGE / relative).read_bytes():
            raise BuildError(f"unchanged control file drifted: {relative}")

    if masters != 1665 or access != 1657:
        raise BuildError(f"payload count drifted: {masters} Masters, {access} access")
    inode_matches = 0
    for relative_directory in ("masters", "export/access", "export/md"):
        for candidate_path in (PACKAGE / relative_directory).iterdir():
            current_path = CURRENT / relative_directory / candidate_path.name
            if candidate_path.stat().st_ino != current_path.stat().st_ino:
                raise BuildError(f"media is not an unchanged hard link: {candidate_path}")
            inode_matches += 1
    if inode_matches != 3322:
        raise BuildError(f"expected 3322 unchanged media hard links, got {inode_matches}")

    validate_unit(PACKAGE, expect_subdivisions=True)
    final_source_state = source_state()
    if final_source_state != initial_source_state:
        raise BuildError("source-device state changed during candidate construction")
    if PACKAGE.stat().st_dev != UNIT_ROOT.stat().st_dev:
        raise BuildError("candidate and published unit root are on different filesystems")
    os.rename(PACKAGE, CANDIDATE)

    try:
        os.rename(CURRENT, PREDECESSOR)
        try:
            os.rename(CANDIDATE, CURRENT)
        except Exception:
            os.rename(PREDECESSOR, CURRENT)
            raise
    except Exception as error:
        raise BuildError(f"guarded cutover failed: {error}") from error

    published = validate_unit(CURRENT, expect_subdivisions=True)
    predecessor = validate_unit(PREDECESSOR, expect_subdivisions=False)
    if source_state() != initial_source_state:
        raise BuildError("source-device state changed during cutover")
    if CANDIDATE.exists() or not CURRENT.is_dir() or not PREDECESSOR.is_dir():
        raise BuildError("post-cutover path state is invalid")

    receipt = {
        "format": "iss-kurt-schwarz-subdivision-cutover-v1",
        "result": "ok",
        "cutover_at": now(),
        "published_unit": str(CURRENT),
        "predecessor_unit": str(PREDECESSOR),
        "predecessor_preserved": True,
        "source_state": initial_source_state,
        "source_bytes_changed": False,
        "media_bytes_changed": False,
        "media_payload_hardlinks_verified": inode_matches,
        "objects": published["objects"],
        "masters": published["masters"],
        "access_derivatives": published["access_derivatives"],
        "representations": published["representations"],
        "checksums": published["checksums"],
        "md_collection_register_rows": 13,
        "md_collection_memberships": 2405,
        "kurt_subdivisions": 11,
        "objects_with_kurt_subdivisions": 637,
        "kurt_subdivision_memberships": 744,
        "objects_with_multiple_kurt_subdivisions": 94,
        "related_collection_555_memberships": membership_counts["555"],
        "deletion_authorized": False,
        "predecessor_objects": predecessor["objects"],
        "predecessor_representations": predecessor["representations"],
    }
    write_new(
        RUN_ROOT / "receipt.json",
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (BuildError, OSError, csv.Error, sqlite3.Error, ValueError) as error:
        raise SystemExit(f"ERROR: {error}")
