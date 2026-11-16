#!/usr/bin/env python3
"""Finalize and atomically publish the validated Kurt Schwarz MD candidate."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CURRENT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
CANDIDATE = Path(
    "/mnt/volume/iss-archive/units/kurt-schwarz-md-final-refresh"
)
RUN_ROOT = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/md-finalization/"
    "kurt-schwarz-md-final-refresh-001"
)
SOURCE_ROOT = Path(
    "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz"
)
CUTOVER_RECEIPT = RUN_ROOT / "cutover.json"
FINAL_UNIT_ID = "kurt-schwarz"
TEMPORARY_PREDECESSOR = CURRENT.parent / ".kurt-schwarz-cutover-predecessor"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def source_is_read_only() -> str:
    line = subprocess.run(
        [
            "findmnt", "-n", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target", str(SOURCE_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    options = set(line.split(None, 3)[3].split(","))
    if "ro" not in options or "rw" in options:
        raise RuntimeError(f"source is not read-only: {line}")
    return line


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def csv_payload(rows: list[dict[str, str]], fields: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fields,
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise RuntimeError(f"temporary path already exists: {temporary}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def validate_candidate(root: Path, *, final_labels: bool) -> dict[str, int]:
    representations = read_csv(
        root / "db-restore/manifests/representations.csv"
    )
    source_disposition = read_csv(
        root / "db-restore/manifests/source-disposition.csv"
    )
    drain = read_csv(root / "db-restore/manifests/drain-list.csv")
    decisions = read_csv(
        root / "db-restore/manifests/reconciliation-decisions.csv"
    )
    aliases = read_csv(root / "db-restore/manifests/object-aliases.csv")
    gaps = read_csv(
        root / "db-restore/manifests/access-derivative-gaps.csv"
    )
    checksums = read_csv(root / "db-restore/checksums.csv")
    catalog = read_csv(root / "CATALOG.csv")
    masters = [
        row for row in representations
        if row["role"] == "preservation_master"
    ]
    access = [
        row for row in representations
        if row["role"] == "access_derivative"
    ]
    if not (
        len(masters) == len({row["record_keys"] for row in masters}) == 1665
        and len(access) == 1657
        and len(gaps) == 8
        and len(representations) == len(checksums) == len(catalog) == 3322
        and len(source_disposition) == 17077
        and len({row["source_path"] for row in source_disposition}) == 17077
        and len(drain) == 2339
        and len(decisions) == 2366
        and len(aliases) == 3
    ):
        raise RuntimeError("candidate manifest counts drifted")
    if Counter(row["disposition"] for row in decisions) != Counter(
        {"redundant": 2331, "noise": 8, "selected_master": 27}
    ):
        raise RuntimeError("candidate decision counts drifted")
    if any(
        row["deletion_authorized"] != "false"
        for row in source_disposition + drain + decisions
    ):
        raise RuntimeError("deletion authorization detected")
    if set(row["record_keys"] for row in masters).intersection(
        row["alias_record_key"] for row in aliases
    ):
        raise RuntimeError("canonical and alias record keys overlap")
    if len(
        set(row["record_keys"] for row in masters)
        | set(row["alias_record_key"] for row in aliases)
    ) != 1668:
        raise RuntimeError("1,668 record relationships are not accounted for")
    if len(list((root / "masters").iterdir())) != 1665:
        raise RuntimeError("flat Master payload count drifted")
    if any(path.is_dir() for path in (root / "masters").iterdir()):
        raise RuntimeError("Master payload is not flat")
    if not (root / "00_README.md").is_file() or (root / "README.md").exists():
        raise RuntimeError("approved unit README layout is missing")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise RuntimeError("candidate contains a symbolic link")
    if {row["final_relative_path"] for row in representations} != {
        row["path"] for row in checksums
    }:
        raise RuntimeError("representation/checksum paths disagree")
    if any(
        (root / row["path"]).stat().st_size != int(row["size_bytes"])
        for row in checksums
    ):
        raise RuntimeError("candidate file size binding failed")
    expected_unit = {FINAL_UNIT_ID} if final_labels else {
        "kurt-schwarz-md-final-refresh"
    }
    if {row["unit"] for row in source_disposition} != expected_unit:
        raise RuntimeError("source-disposition unit label drifted")
    if {row["unit"] for row in drain} != expected_unit:
        raise RuntimeError("drain unit label drifted")
    database = sqlite3.connect(
        f"file:{root / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True
    )
    try:
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("restore SQLite quick_check failed")
        expected = {
            "objects": 1665,
            "representations": 3322,
            "source_disposition": 17077,
            "drain_list": 2339,
            "reconciliation_decisions": 2366,
            "object_aliases": 3,
            "access_derivative_gaps": 8,
            "checksums": 3322,
        }
        for table, count in expected.items():
            actual = database.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            if actual != count:
                raise RuntimeError(f"{table} count {actual} != {count}")
        if {
            row[0] for row in database.execute(
                "SELECT DISTINCT unit FROM objects"
            )
        } != expected_unit:
            raise RuntimeError("restore object unit label drifted")
        if {
            row[0] for row in database.execute(
                "SELECT DISTINCT unit FROM source_disposition"
            )
        } != expected_unit:
            raise RuntimeError("restore source unit label drifted")
    finally:
        database.close()
    return {
        "objects": len(masters),
        "masters": len(masters),
        "access_derivatives": len(access),
        "access_derivative_gaps": len(gaps),
        "representations": len(representations),
        "record_relationships": len(masters) + len(aliases),
        "drain_rows": len(drain),
    }


def finalize_unit_labels() -> None:
    for relative in (
        "db-restore/manifests/source-disposition.csv",
        "db-restore/manifests/drain-list.csv",
    ):
        path = CANDIDATE / relative
        rows = read_csv(path)
        fields = list(rows[0])
        for row in rows:
            row["unit"] = FINAL_UNIT_ID
        atomic_replace(path, csv_payload(rows, fields))

    database_path = CANDIDATE / "db-restore/RESTORE.sqlite"
    database = sqlite3.connect(database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute("UPDATE objects SET unit=?", (FINAL_UNIT_ID,))
        database.execute(
            "UPDATE source_disposition SET unit=?", (FINAL_UNIT_ID,)
        )
        database.execute("UPDATE drain_list SET unit=?", (FINAL_UNIT_ID,))
        database.commit()
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("restore SQLite integrity failed after relabel")
        sql = "\n".join(database.iterdump()) + "\n"
    finally:
        database.close()
    atomic_replace(
        CANDIDATE / "db-restore/RESTORE.sql", sql.encode("utf-8")
    )

    readme_path = CANDIDATE / "00_README.md"
    readme = readme_path.read_text(encoding="utf-8")
    old = (
        "The previous published `kurt-schwarz` unit and all protected source "
        "files were left\nunchanged. This candidate must not replace the prior "
        "unit until its controlled\ncutover is explicitly approved.\n"
    )
    new = (
        "This is the controlled published `kurt-schwarz` replacement. The prior "
        "unit remains\npreserved at the predecessor path recorded in the cutover "
        "receipt. Protected source\nfiles remain unchanged and deletion remains "
        "unauthorized.\n"
    )
    if old not in readme:
        raise RuntimeError("candidate README cutover boundary drifted")
    atomic_replace(readme_path, readme.replace(old, new).encode("utf-8"))


def guarded_three_rename(left: Path, right: Path) -> None:
    if TEMPORARY_PREDECESSOR.exists():
        raise RuntimeError(
            f"temporary predecessor path already exists: {TEMPORARY_PREDECESSOR}"
        )
    left.rename(TEMPORARY_PREDECESSOR)
    right.rename(left)
    TEMPORARY_PREDECESSOR.rename(right)


def reverse_three_rename(left: Path, right: Path) -> None:
    if TEMPORARY_PREDECESSOR.exists():
        raise RuntimeError(
            f"temporary predecessor path already exists: {TEMPORARY_PREDECESSOR}"
        )
    left.rename(TEMPORARY_PREDECESSOR)
    right.rename(left)
    TEMPORARY_PREDECESSOR.rename(right)


if CUTOVER_RECEIPT.exists():
    raise SystemExit(f"cutover receipt already exists: {CUTOVER_RECEIPT}")
if not CURRENT.is_dir() or not CANDIDATE.is_dir():
    raise SystemExit("current or candidate unit is missing")
if CURRENT.stat().st_dev != CANDIDATE.stat().st_dev:
    raise SystemExit("cutover roots are not on the same filesystem")

validation = json.loads((RUN_ROOT / "validation.json").read_text())
publication = json.loads((RUN_ROOT / "publication.json").read_text())
if not (
    validation["result"] == publication["result"] == "ok"
    and publication["candidate_only"] is True
    and publication["replaces_prior_unit"] is False
    and publication["confirmed_master_replacements"] == 27
    and publication["canonical_record_relationships"] == 1668
):
    raise SystemExit("candidate validation/publication receipt is not eligible")

source_mount_before = source_is_read_only()
try:
    candidate_before = validate_candidate(CANDIDATE, final_labels=False)
except RuntimeError as error:
    if "unit label drifted" not in str(error):
        raise
    candidate_before = validate_candidate(CANDIDATE, final_labels=True)
else:
    finalize_unit_labels()
candidate_ready = validate_candidate(CANDIDATE, final_labels=True)
if candidate_ready != candidate_before:
    raise RuntimeError("candidate content counts changed during final relabel")

os.sync()
cutover_at = now()
guarded_three_rename(CURRENT, CANDIDATE)
os.sync()

try:
    final_counts = validate_candidate(CURRENT, final_labels=True)
    predecessor_representations = read_csv(
        CANDIDATE / "db-restore/manifests/representations.csv"
    )
    if not (
        len(predecessor_representations) == 8623
        and sum(
            row["role"] == "preservation_master"
            for row in predecessor_representations
        ) == 1668
    ):
        raise RuntimeError("predecessor unit binding failed after exchange")
    source_mount_after = source_is_read_only()
except Exception:
    reverse_three_rename(CURRENT, CANDIDATE)
    os.sync()
    raise

receipt = {
    "format": "iss-kurt-schwarz-md-final-cutover-v1",
    "result": "ok",
    "cutover_at": cutover_at,
    "validated_at": now(),
    "promotion": "same-filesystem guarded three-rename cutover",
    "published_unit": str(CURRENT),
    "predecessor_unit": str(CANDIDATE),
    "predecessor_preserved": True,
    "replaces_prior_unit": True,
    "source_mount_before": source_mount_before,
    "source_mount_after": source_mount_after,
    "source_read_only": True,
    "deletion_authorized": False,
    "confirmed_master_replacements": 27,
    "retained_master_comparisons": 2,
    "duplicate_master_corrections": 3,
    **final_counts,
}
atomic_replace(
    CUTOVER_RECEIPT,
    (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
)
print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
