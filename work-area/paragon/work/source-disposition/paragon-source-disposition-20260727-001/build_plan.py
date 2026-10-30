#!/usr/bin/env python3
"""Build the reviewed Paragon source-disposition plan from the USB inventory."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


RUN = Path(__file__).resolve().parent
INVENTORY = RUN / "usb-inventory.csv"
PLAN = RUN / "source-disposition.planned.csv"
RETAIN = RUN / "retain-review.csv"
SUMMARY = RUN / "classification-summary.json"
FINAL = Path("/mnt/volume/iss-archive/units/paragon")
CATALOG = Path("/mnt/volume/iss-archive-work/paragon/work/catalog.sqlite")
CHECKSUMS = FINAL / "db-restore/checksums.csv"
REPRESENTATIONS = FINAL / "db-restore/manifests/representations.csv"
DOCUMENTS = FINAL / "db-restore/manifests/documents.csv"
FIELDS = [
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
]
SOURCE_DIGEST = re.compile(r"(?:source|Source) sha256:([0-9a-f]{64})")
INVENTORY_FROM_NAME = re.compile(r"(VL[LV]-\d{4}(?:-\d+)?)", re.IGNORECASE)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def joined(values: set[str]) -> str:
    return " | ".join(sorted(value for value in values if value))


def exact_final_records() -> dict[str, list[dict[str, str]]]:
    checksum_rows = read_csv(CHECKSUMS)
    checksum_map = {row["path"]: row for row in checksum_rows}
    records: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(REPRESENTATIONS):
        for path_key, digest_key, role in (
            ("master_path", "master_sha256", "preservation_representation"),
            ("access_path", "access_sha256", "access_representation"),
        ):
            records[row[digest_key]].append(
                {
                    "inventory": row["inventory"],
                    "final_path": row[path_key],
                    "final_sha256": row[digest_key],
                    "role": role,
                }
            )
    for row in read_csv(DOCUMENTS):
        records[row["final_sha256"]].append(
            {
                "inventory": "",
                "final_path": row["relative_path"],
                "final_sha256": row["final_sha256"],
                "role": row["category"],
            }
        )
    represented_paths = {
        item["final_path"] for values in records.values() for item in values
    }
    for path, row in checksum_map.items():
        if path in represented_paths:
            continue
        records[row["sha256"]].append(
            {
                "inventory": "",
                "final_path": path,
                "final_sha256": row["sha256"],
                "role": "package_record",
            }
        )
    return records


def active_source_records() -> dict[str, list[dict[str, str]]]:
    representation_rows = read_csv(REPRESENTATIONS)
    master_by_digest = {
        row["master_sha256"]: row["master_path"] for row in representation_rows
    }
    connection = sqlite3.connect(f"file:{CATALOG}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                """SELECT a.archival_label, ef.role, o.relative_path,
                          o.content_digest, o.note
                   FROM entity_files ef
                   JOIN file_occurrences o USING(occurrence_id)
                   JOIN archive_objects a ON a.entity_id=ef.entity_id
                   JOIN entities e ON e.entity_id=ef.entity_id
                   WHERE ef.status='active' AND o.status='verified'
                     AND ef.role IN (
                         'preservation_master','preservation_surrogate'
                     )
                     AND e.status='active'"""
            )
        )
    finally:
        connection.close()
    records: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        match = SOURCE_DIGEST.search(row["note"] or "")
        if not match:
            continue
        final_path = master_by_digest.get(row["content_digest"])
        if not final_path:
            raise RuntimeError(
                f"active master absent from final manifest: {row['content_digest']}"
            )
        records["sha256:" + match.group(1)].append(
            {
                "inventory": row["archival_label"],
                "final_path": final_path,
                "final_sha256": row["content_digest"],
                "role": row["role"],
            }
        )
    return records


def rejected_paths() -> dict[str, str]:
    connection = sqlite3.connect(f"file:{CATALOG}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                """SELECT after_json,note FROM logbook
                   WHERE action='review_surrogate_swap_candidate'"""
            )
        )
    finally:
        connection.close()
    rejected = {}
    for row in rows:
        payload = json.loads(row["after_json"])
        if payload.get("state") == "rejected":
            rejected[payload["candidate_path"]] = row["note"] or (
                "Explicitly rejected during surrogate-swap review."
            )
    return rejected


def source_role(row: dict[str, str], exact: list[dict[str, str]]) -> str:
    extension = row["extension"]
    path = row["source_path"]
    if extension == ".tif":
        return "source_tiff_image"
    if extension == ".jpg":
        return "source_jpeg_image"
    if extension == ".pdf":
        return "project_history_document"
    if extension == ".docx":
        return "research_note"
    if extension in {".xlsx", ".csv"}:
        if exact:
            return "metadata_source"
        return "intermediate_metadata_file"
    if extension == ".zip":
        return "museum_digital_import_xml"
    return "source_file"


def inventory_value(
    row: dict[str, str],
    exact: list[dict[str, str]],
    active: list[dict[str, str]],
) -> str:
    mapped = {item["inventory"] for item in exact + active if item["inventory"]}
    if mapped:
        return joined(mapped)
    match = INVENTORY_FROM_NAME.search(Path(row["source_path"]).stem)
    return match.group(1).upper() if match else ""


def classify(
    row: dict[str, str],
    exact_map: dict[str, list[dict[str, str]]],
    active_map: dict[str, list[dict[str, str]]],
    rejected: dict[str, str],
    primary_source_path: dict[str, str],
) -> dict[str, str]:
    exact = exact_map.get(row["sha256"], [])
    active = active_map.get(row["sha256"], [])
    final_records = exact + active
    final_paths = {item["final_path"] for item in final_records}
    final_digests = {item["final_sha256"] for item in final_records}
    path = row["source_path"]
    role = source_role(row, exact)
    inventory = inventory_value(row, exact, active)

    if path in rejected:
        status = "reviewed_not_selected"
        basis = (
            "Explicit logged surrogate-swap rejection; candidate identity or "
            "replacement value was not accepted."
        )
        drain_state = "retain_not_preserved"
        exact_proven = "false"
        final_paths = set()
        final_digests = set()
        note = rejected[path] + " Retain this exact USB path."
    elif exact:
        duplicate = path != primary_source_path[row["sha256"]]
        status = (
            "preserved_exact_duplicate" if duplicate else "preserved_exact_copy"
        )
        basis = (
            "Fresh USB SHA-256 equals the checksum of the accepted final "
            "file; final size/checksum control is current."
        )
        if active:
            basis += (
                " The active preservation TIFF also records this source "
                "digest and decoded-pixel preservation."
            )
        drain_state = "candidate_after_separate_review"
        exact_proven = "true"
        note = "Evidence only; no deletion is authorized."
    elif active:
        status = "preserved_as_metadata_enriched_master"
        basis = (
            "The active preservation TIFF records this exact source SHA-256 "
            "and an accession-verified decoded-pixel digest; TIFF bytes differ "
            "because archive metadata was embedded."
        )
        drain_state = "candidate_after_separate_review"
        exact_proven = "false"
        note = "Evidence only; no deletion is authorized."
    elif row["extension"] in {".xlsx", ".csv"}:
        status = "not_preserved_intermediate_metadata"
        basis = (
            "Reviewed as an intermediate upload version; the full pre-upload "
            "workbook and final CSV/XLSX are archived, but this exact version "
            "is not preserved."
        )
        drain_state = "retain_not_preserved"
        exact_proven = "false"
        note = "Retain on USB unless a later appraisal explicitly preserves or rejects it."
    else:
        status = "preservation_unresolved"
        basis = (
            "No checksum-bound link to an active final representation was "
            "proven. Filename, directory, or visual-family similarity alone "
            "is insufficient."
        )
        drain_state = "retain_not_preserved"
        exact_proven = "false"
        note = "Retain this exact USB path; it is outside any later automatic drain."

    return {
        "source_path": path,
        "source_kind": "file",
        "unit": "paragon",
        "inventory": inventory,
        "source_role": role,
        "final_path": joined(final_paths),
        "preservation_status": status,
        "preservation_basis": basis,
        "source_sha256": row["sha256"],
        "final_sha256": joined(final_digests),
        "source_size_bytes": row["size_bytes"],
        "exact_copy_proven": exact_proven,
        "drain_state": drain_state,
        "deletion_authorized": "false",
        "notes": note,
    }


def main() -> int:
    if PLAN.exists() or RETAIN.exists() or SUMMARY.exists():
        raise RuntimeError("classification plan already exists")
    inventory = read_csv(INVENTORY)
    if len(inventory) != 1413:
        raise RuntimeError(f"unexpected USB inventory count: {len(inventory)}")
    inventory_paths = {row["source_path"] for row in inventory}
    if len(inventory_paths) != len(inventory):
        raise RuntimeError("USB inventory contains duplicate paths")
    for row in inventory:
        path = Path(row["source_path"])
        stat = path.stat()
        if (
            stat.st_size != int(row["size_bytes"])
            or stat.st_mtime_ns != int(row["mtime_ns"])
        ):
            raise RuntimeError(f"USB source state changed: {path}")

    exact_map = exact_final_records()
    active_map = active_source_records()
    rejected = rejected_paths()
    missing_rejected = set(rejected) - inventory_paths
    if missing_rejected:
        raise RuntimeError(
            f"reviewed rejected paths absent from USB inventory: {missing_rejected}"
        )
    source_paths_by_digest: dict[str, list[str]] = defaultdict(list)
    for row in inventory:
        source_paths_by_digest[row["sha256"]].append(row["source_path"])
    primary_source_path = {
        source_digest: min(paths)
        for source_digest, paths in source_paths_by_digest.items()
    }
    rows = [
        classify(row, exact_map, active_map, rejected, primary_source_path)
        for row in sorted(inventory, key=lambda item: item["source_path"])
    ]
    if any(row["deletion_authorized"] != "false" for row in rows):
        raise RuntimeError("disposition plan authorizes deletion")
    if len({row["source_path"] for row in rows}) != len(rows):
        raise RuntimeError("disposition plan contains duplicate paths")
    write_csv(PLAN, rows)
    retain_rows = [
        row for row in rows if row["drain_state"] != "candidate_after_separate_review"
    ]
    write_csv(RETAIN, retain_rows)
    summary = {
        "format": "iss-paragon-source-disposition-plan-summary-v1",
        "rows": len(rows),
        "source_bytes": sum(int(row["source_size_bytes"]) for row in rows),
        "candidate_rows": sum(
            row["drain_state"] == "candidate_after_separate_review"
            for row in rows
        ),
        "retain_rows": len(retain_rows),
        "preservation_status": dict(
            sorted(Counter(row["preservation_status"] for row in rows).items())
        ),
        "drain_state": dict(
            sorted(Counter(row["drain_state"] for row in rows).items())
        ),
        "source_role": dict(
            sorted(Counter(row["source_role"] for row in rows).items())
        ),
        "rejected_paths": len(rejected),
        "active_source_digests": len(active_map),
        "active_source_digests_present_on_usb": sum(
            source_digest in source_paths_by_digest for source_digest in active_map
        ),
        "exact_final_digests_present_on_usb": sum(
            source_digest in source_paths_by_digest for source_digest in exact_map
        ),
        "fresh_source_sha256": len(rows),
        "deletion_authorized_true": 0,
    }
    SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
