#!/usr/bin/env python3
"""Prepare the reconciled museum-digital TFA scope for later accession.

This creates human-readable work projections and records the museum-digital
metadata authority in the existing reconciliation SQLite catalogue. It does
not copy a master, generate a derivative, accession an object, or modify the
read-only source archive.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-tfa-md-accession-preparation-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
SOURCE_ROOT = Path("/media/vladimir/Elements")
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
OBJECTS_CSV = REVIEW_ROOT / "objects.csv"
SUBDIVISIONS_CSV = REVIEW_ROOT / "subdivision-register.csv"
MEMBERSHIPS_CSV = REVIEW_ROOT / "subdivision-memberships.csv"
CATALOG_CSV = REVIEW_ROOT / "md-accession-catalog.csv"
HOLDS_CSV = REVIEW_ROOT / "md-accession-holds.csv"
RECEIPT_JSON = REVIEW_ROOT / "md-accession-preparation.json"
COLLECTION_CAPTURE = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/museum-digital/runs/"
    "20260810T071711Z-tfa-collection-857"
)
INSTITUTION_CAPTURE = Path(
    "/mnt/volume/iss-archive-work/tfa/work/reconciliation/museum-digital/runs/"
    "20260814T201913Z-tfa-institution-29-clean-local-only-recheck"
)
PROVENANCE_CORRECTION = REVIEW_ROOT / "md-55436-md-85642-provenance-correction.json"
EXPECTED_SCOPE = 5282
EXPECTED_MEMBERSHIPS = 10723
EXPECTED_SUBDIVISIONS = 9
WORKBENCH_UNIT = "iss-archive-tfa-workbench.service"

CATALOG_FIELDS = (
    "record_key",
    "archival_object_identifier",
    "md_object_id",
    "inventory",
    "metadata_authority",
    "metadata_reconciled",
    "metadata_reconciliation_basis",
    "title",
    "description",
    "date_text",
    "object_type",
    "material",
    "dimensions",
    "places",
    "tags",
    "creator",
    "md_url",
    "collection_ids",
    "collections",
    "subdivision_ids",
    "subdivisions",
    "last_updated",
    "selected_candidate_id",
    "selected_master_path",
    "selected_master_size_bytes",
    "master_decision_state",
    "master_decided_by",
    "master_decided_at",
    "master_decision_note",
    "provenance_chain",
    "accession_state",
    "accession_hold_reason",
)
HOLD_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "accession_state",
    "accession_hold_reason",
    "related_record_key",
    "selected_master_path",
    "evidence_path",
)
MD_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "title",
    "description",
    "date_text",
    "object_type",
    "material",
    "dimensions",
    "places",
    "tags",
    "creator",
    "md_url",
    "collection_ids",
    "collections",
    "subdivision_ids",
    "subdivisions",
    "last_updated",
    "record_state",
)


class PreparationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise PreparationError(f"path escaped approved root: {resolved}") from error
    return resolved


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    inside(path, WORK_ROOT)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    inside(path, WORK_ROOT)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def mount_evidence() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise PreparationError("source mount did not resolve exactly once")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise PreparationError(f"source mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def require_workbench_stopped() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", WORKBENCH_UNIT],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() == "active":
        raise PreparationError(
            f"stop {WORKBENCH_UNIT} before replacing projections or updating SQLite"
        )


def connect(*, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def compare_current_captures() -> dict[str, Any]:
    sys.path.insert(0, str(Path(__file__).parent))
    import tfa_prepare  # noqa: PLC0415

    collection_rows, _, _ = tfa_prepare.load_md(COLLECTION_CAPTURE)
    wanted = {row["md_object_id"]: row for row in collection_rows}
    institution_rows: dict[str, dict[str, str]] = {}
    for raw_path in sorted((INSTITUTION_CAPTURE / "raw").glob("page-*.json")):
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        page_objects = (payload.get("results") or {}).get("objects") or {}
        values = page_objects.values() if isinstance(page_objects, dict) else page_objects
        for item in values:
            if not isinstance(item, dict):
                continue
            md_id = clean(item.get("object_id"))
            if md_id not in wanted:
                continue
            collection_items = [row for row in item.get("object_collection", []) if isinstance(row, dict)]
            collection_ids = [clean(row.get("collection_id")) for row in collection_items]
            if "857" not in collection_ids:
                raise PreparationError(f"institution capture lost TFA membership: {md_id}")
            events = [row for row in item.get("object_events", []) if isinstance(row, dict)]
            relation_places = [row for row in item.get("object_relation_places", []) if isinstance(row, dict)]
            subdivisions = [row for row in collection_items if clean(row.get("collection_id")) != "857"]
            institution_rows[md_id] = {
                "record_key": f"md:{md_id}",
                "md_object_id": md_id,
                "inventory": clean(item.get("object_inventory_number")),
                "title": clean(item.get("object_name")),
                "description": clean(item.get("object_description")),
                "date_text": tfa_prepare.event_values(events, "time", "time_name"),
                "object_type": clean(item.get("object_type")),
                "material": clean(item.get("object_material_technique")),
                "dimensions": clean(item.get("object_dimensions")),
                "places": "; ".join(
                    value
                    for value in (
                        tfa_prepare.event_values(events, "place", "place_name"),
                        tfa_prepare.event_values(relation_places, "place", "place_name"),
                    )
                    if value
                ),
                "tags": tfa_prepare.named_values(item.get("object_tags"), "tag_name"),
                "creator": tfa_prepare.event_values(events, "people", "people_display_name"),
                "md_url": f"https://berlin.museum-digital.de/object/{md_id}",
                "collection_ids": "; ".join(collection_ids),
                "collections": "; ".join(clean(row.get("collection_name")) for row in collection_items),
                "subdivision_ids": "; ".join(clean(row.get("collection_id")) for row in subdivisions),
                "subdivisions": "; ".join(clean(row.get("collection_name")) for row in subdivisions),
                "last_updated": clean(item.get("object_last_updated")),
                "record_state": "current_museum_digital",
                "decision_state": "unresolved",
            }
    if len(institution_rows) != EXPECTED_SCOPE or set(institution_rows) != set(wanted):
        raise PreparationError(
            f"newest institution capture TFA scope drifted: {len(institution_rows)} != {EXPECTED_SCOPE}"
        )
    differences = []
    for md_id, old in wanted.items():
        current = institution_rows[md_id]
        fields = [field for field in MD_FIELDS if old[field] != current[field]]
        if fields:
            differences.append({"md_object_id": md_id, "fields": fields})
    if differences:
        raise PreparationError(
            f"museum-digital metadata changed in newest capture; reproject before accession: {differences[:5]}"
        )
    return {
        "collection_capture": str(COLLECTION_CAPTURE),
        "institution_capture": str(INSTITUTION_CAPTURE),
        "scope_objects_compared": len(wanted),
        "object_metadata_differences": 0,
    }


def verify_human_and_sqlite_metadata() -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    object_csv = {
        row["record_key"]: row
        for row in read_csv(OBJECTS_CSV)
        if row["record_state"] == "current_museum_digital"
    }
    membership_csv = read_csv(MEMBERSHIPS_CSV)
    subdivision_csv = read_csv(SUBDIVISIONS_CSV)
    if len(object_csv) != EXPECTED_SCOPE:
        raise PreparationError(f"human-readable MD object scope drifted: {len(object_csv)}")
    if len(membership_csv) != EXPECTED_MEMBERSHIPS:
        raise PreparationError(f"human-readable membership scope drifted: {len(membership_csv)}")
    if len(subdivision_csv) != EXPECTED_SUBDIVISIONS:
        raise PreparationError(f"human-readable subdivision scope drifted: {len(subdivision_csv)}")

    with closing(connect(read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign:
            raise PreparationError(f"SQLite integrity failed: quick={quick}, foreign_keys={len(foreign)}")
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT o.*, d.selected_candidate_id,
                       d.decision_state AS master_decision_state,
                       d.note AS master_decision_note,
                       d.decided_by AS master_decided_by,
                       d.decided_at AS master_decided_at,
                       c.source_path AS selected_master_path,
                       c.extension AS selected_master_extension,
                       c.size_bytes AS selected_master_size_bytes
                FROM objects o
                JOIN reconciliation_decisions d ON d.record_key=o.record_key
                JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE o.record_state='current_museum_digital'
                ORDER BY CAST(o.md_object_id AS INTEGER), o.record_key
                """
            )
        ]
        db_memberships = {
            (row["record_key"], row["subdivision_id"], row["membership_basis"])
            for row in connection.execute(
                "SELECT record_key, subdivision_id, membership_basis FROM object_subdivisions"
            )
        }
        db_subdivisions = {
            row["subdivision_id"]: dict(row)
            for row in connection.execute("SELECT * FROM subdivisions")
        }
    if len(rows) != EXPECTED_SCOPE:
        raise PreparationError(f"SQLite MD decision scope drifted: {len(rows)}")
    for row in rows:
        source = object_csv.get(row["record_key"])
        if source is None:
            raise PreparationError(f"SQLite object absent from objects.csv: {row['record_key']}")
        differing = [field for field in MD_FIELDS if str(row[field]) != source[field]]
        if differing:
            raise PreparationError(f"SQLite/CSV metadata disagreement for {row['record_key']}: {differing}")
        if row["decision_state"] != "resolved" or row["master_decision_state"] != "selected_master":
            raise PreparationError(f"unresolved master decision: {row['record_key']}")
        if str(row["selected_master_extension"]).casefold() not in {".tif", ".tiff"}:
            raise PreparationError(f"selected master is not TIFF: {row['record_key']}")
        master = inside(Path(row["selected_master_path"]), SOURCE_ROOT)
        if not master.is_file() or master.stat().st_size != int(row["selected_master_size_bytes"]):
            raise PreparationError(f"selected master path/size guard failed: {row['record_key']}")

    human_memberships = {
        (row["record_key"], row["subdivision_id"], row["membership_basis"])
        for row in membership_csv
    }
    if db_memberships != human_memberships:
        raise PreparationError("SQLite and human-readable subdivision memberships disagree")
    for row in subdivision_csv:
        db = db_subdivisions.get(row["subdivision_id"])
        if db is None:
            raise PreparationError(f"SQLite subdivision missing: {row['subdivision_id']}")
        for field in (
            "scheme_id", "parent_subdivision_id", "label", "subdivision_type",
            "source_path", "assigned_object_count", "assignment_state", "notes",
        ):
            if clean(db[field]) != clean(row[field]):
                raise PreparationError(
                    f"SQLite/CSV subdivision disagreement: {row['subdivision_id']} / {field}"
                )
    return rows, subdivision_csv, membership_csv


def build_catalog(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    correction = json.loads(PROVENANCE_CORRECTION.read_text(encoding="utf-8"))
    canonical = correction["canonical_object"]["record_key"]
    duplicate = correction["duplicate_catalogue_record"]["record_key"]
    related = {canonical: duplicate, duplicate: canonical}
    by_path: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_path[str(row["selected_master_path"])].append(str(row["record_key"]))
    shared = {path: keys for path, keys in by_path.items() if len(keys) > 1}
    if shared != {correction["master"]["read_only_source_path"]: sorted((canonical, duplicate))}:
        normalized = {path: sorted(keys) for path, keys in shared.items()}
        expected = {correction["master"]["read_only_source_path"]: sorted((canonical, duplicate))}
        if normalized != expected:
            raise PreparationError(f"unexpected selected-master sharing: {normalized}")

    catalog = []
    holds = []
    for row in rows:
        is_duplicate = row["record_key"] == duplicate
        state = "hold_catalogue_duplicate" if is_duplicate else "ready_for_accession"
        reason = (
            f"Duplicate museum-digital catalogue record; physical TIFF is canonically bound to {canonical}."
            if is_duplicate
            else ""
        )
        catalog.append(
            {
                "record_key": row["record_key"],
                "archival_object_identifier": canonical if is_duplicate else row["record_key"],
                "md_object_id": row["md_object_id"],
                "inventory": row["inventory"],
                "metadata_authority": "museum-digital",
                "metadata_reconciled": "true",
                "metadata_reconciliation_basis": "complete current MD scope; newest institution capture identical",
                "title": row["title"],
                "description": row["description"],
                "date_text": row["date_text"],
                "object_type": row["object_type"],
                "material": row["material"],
                "dimensions": row["dimensions"],
                "places": row["places"],
                "tags": row["tags"],
                "creator": row["creator"],
                "md_url": row["md_url"],
                "collection_ids": row["collection_ids"],
                "collections": row["collections"],
                "subdivision_ids": row["subdivision_ids"],
                "subdivisions": row["subdivisions"],
                "last_updated": row["last_updated"],
                "selected_candidate_id": row["selected_candidate_id"],
                "selected_master_path": row["selected_master_path"],
                "selected_master_size_bytes": row["selected_master_size_bytes"],
                "master_decision_state": row["master_decision_state"],
                "master_decided_by": row["master_decided_by"],
                "master_decided_at": row["master_decided_at"],
                "master_decision_note": row["master_decision_note"],
                "provenance_chain": f"museum-digital md:{row['md_object_id']} | read-only source {row['selected_master_path']}",
                "accession_state": state,
                "accession_hold_reason": reason,
            }
        )
        if is_duplicate:
            holds.append(
                {
                    "record_key": row["record_key"],
                    "md_object_id": row["md_object_id"],
                    "inventory": row["inventory"],
                    "accession_state": state,
                    "accession_hold_reason": reason,
                    "related_record_key": related[row["record_key"]],
                    "selected_master_path": row["selected_master_path"],
                    "evidence_path": str(PROVENANCE_CORRECTION),
                }
            )
    return catalog, holds


def backup_database(stamp: str) -> Path:
    backup = REVIEW_ROOT / f"reconciliation.pre-md-authority-{stamp}.sqlite"
    if backup.exists():
        raise PreparationError(f"database backup already exists: {backup}")
    with closing(connect(read_only=True)) as source, closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
    with backup.open("rb") as handle:
        os.fsync(handle.fileno())
    return backup


def main() -> int:
    prepared_at = utc_now()
    stamp = stamp_now()
    require_workbench_stopped()
    mount = mount_evidence()
    capture_check = compare_current_captures()
    rows, subdivisions, memberships = verify_human_and_sqlite_metadata()
    catalog, holds = build_catalog(rows)
    missing_counts = {
        field: sum(not clean(row[field]) for row in rows)
        for field in ("date_text", "material", "dimensions", "places", "tags", "creator")
    }
    subdivision_counts = {
        row["label"]: int(row["assigned_object_count"])
        for row in subdivisions
        if row["subdivision_type"] == "collection_subdivision"
    }

    atomic_csv(CATALOG_CSV, CATALOG_FIELDS, catalog)
    atomic_csv(HOLDS_CSV, HOLD_FIELDS, holds)
    backup = backup_database(stamp)
    receipt = {
        "format": FORMAT,
        "prepared_at": prepared_at,
        "result": "ok",
        "scope": "all current museum-digital collection 857 records",
        "md_objects": len(catalog),
        "metadata_authority": "museum-digital",
        "metadata_reconciled_objects": len(catalog),
        "metadata_missing_value_counts": missing_counts,
        "master_decisions": Counter(row["master_decision_state"] for row in rows),
        "selected_tiff_masters": len(rows),
        "distinct_selected_master_paths": len({row["selected_master_path"] for row in rows}),
        "ready_for_accession": sum(row["accession_state"] == "ready_for_accession" for row in catalog),
        "accession_holds": len(holds),
        "hold_policy": "retained duplicate MD catalogue record is not accessioned as a second archival object",
        "subdivision_facets": subdivision_counts,
        "subdivision_memberships": len(memberships),
        "capture_validation": capture_check,
        "human_readable_outputs": {
            "accession_catalog": str(CATALOG_CSV),
            "accession_holds": str(HOLDS_CSV),
            "subdivision_register": str(SUBDIVISIONS_CSV),
            "subdivision_memberships": str(MEMBERSHIPS_CSV),
        },
        "fingerprints": {
            path.name: sha256_file(path)
            for path in (CATALOG_CSV, HOLDS_CSV, OBJECTS_CSV, SUBDIVISIONS_CSV, MEMBERSHIPS_CSV)
        },
        "sqlite_database": str(DATABASE),
        "sqlite_backup": str(backup),
        "source_mount": mount,
        "source_read_only": True,
        "masters_copied": 0,
        "derivatives_generated": 0,
        "accessions_performed": 0,
        "source_modified": False,
        "deletion_authorized": False,
    }
    atomic_json(RECEIPT_JSON, receipt)
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        values = {
            "md_metadata_authority": "museum-digital",
            "md_metadata_scope": "collection:857",
            "md_metadata_reconciled_objects": str(len(catalog)),
            "md_metadata_prepared_at": prepared_at,
            "md_accession_catalog": str(CATALOG_CSV),
            "md_accession_preparation_receipt": str(RECEIPT_JSON),
            "md_ready_for_accession": str(receipt["ready_for_accession"]),
            "md_accession_holds": str(receipt["accession_holds"]),
        }
        connection.executemany(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", values.items()
        )
        connection.commit()
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
