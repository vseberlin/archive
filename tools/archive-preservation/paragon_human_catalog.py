#!/usr/bin/env python3
"""Create a human-readable Paragon unit catalog from the canonical export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from paragon_document_bundle import table_xlsx_bytes
from recovery_workbook_backfill import workbook_rows


FORMAT = "iss-paragon-human-catalog-v1"
HUMAN_COLUMNS = [
    "iss_id",
    "archival_label",
    "canonical_name",
    "descriptive_title",
    "description",
    "date_year",
    "object_type",
    "material_technique",
    "dimensions",
    "identifiers",
    "rights_status",
    "record_state",
    "representation_summary",
    "master_file",
    "access_file",
    "document_file",
    "notes_for_review",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {key: str(value or "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def workbook_index(workbook: Path) -> dict[str, dict[str, str]]:
    _sheet, rows = workbook_rows(workbook)
    result: dict[str, dict[str, str]] = {}
    for inventory, payload in rows.items():
        values = {
            key: str(value or "").strip()
            for key, value in payload["values"].items()
        }
        result[inventory] = values
    return result


def date_year_for(inventory: str, workbook: dict[str, dict[str, str]]) -> str:
    candidates = [inventory]
    if inventory == "VLL-1013":
        candidates.append("VLV-1013")
    for candidate in candidates:
        values = workbook.get(candidate)
        if not values:
            continue
        for key in ("related_time1", "printed_when_1", "printed_when1"):
            value = str(values.get(key) or "").strip()
            if value:
                return value
    return ""


def object_type_for(inventory: str, workbook: dict[str, dict[str, str]]) -> str:
    candidates = [inventory]
    if inventory == "VLL-1013":
        candidates.append("VLV-1013")
    for candidate in candidates:
        value = str(workbook.get(candidate, {}).get("object_type") or "").strip()
        if value:
            return value
    return ""


def summarize_representations(rows: list[dict[str, str]]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        role = row.get("representation_role", "")
        if role:
            counts[role] += 1
    if not counts:
        return "no representation"
    return "; ".join(f"{role}: {counts[role]}" for role in sorted(counts))


def first_path(rows: list[dict[str, str]], role: str) -> str:
    for row in rows:
        if row.get("representation_role") == role and row.get("relative_path"):
            return row["relative_path"]
    return ""


def notes_for(rows: list[dict[str, str]], inventory: str) -> str:
    first = rows[0]
    notes: list[str] = []
    if first.get("record_state") == "no_representation":
        notes.append("no active representation")
    if first.get("publication_status") == "blocked":
        notes.append("publication blocked")
    if inventory == "VLL-1013":
        notes.append("metadata accepted from corrected VLV-1013 workbook row")
    if inventory == "VLV-1127":
        notes.append("source TIFF/JPG exists; no exact workbook or final MD row")
    return "; ".join(notes)


def human_rows(canonical_rows: list[dict[str, str]], workbook: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in canonical_rows:
        grouped[row["iss_id"]].append(row)
    output: list[dict[str, str]] = []
    for iss_id in sorted(grouped, key=lambda key: grouped[key][0]["archival_label"]):
        rows = grouped[iss_id]
        first = rows[0]
        inventory = first["archival_label"]
        output.append(
            {
                "iss_id": iss_id,
                "archival_label": inventory,
                "canonical_name": first.get("canonical_name", ""),
                "descriptive_title": first.get("descriptive_title", ""),
                "description": first.get("description", ""),
                "date_year": date_year_for(inventory, workbook),
                "object_type": object_type_for(inventory, workbook),
                "material_technique": first.get("material_technique", ""),
                "dimensions": first.get("dimensions", ""),
                "identifiers": first.get("identifiers", ""),
                "rights_status": first.get("rights_status", ""),
                "record_state": first.get("record_state", ""),
                "representation_summary": summarize_representations(rows),
                "master_file": first_path(rows, "preservation_master")
                or first_path(rows, "preservation_surrogate"),
                "access_file": first_path(rows, "access_derivative"),
                "document_file": first_path(rows, "preservation_document")
                or first_path(rows, "preservation_document_surrogate"),
                "notes_for_review": notes_for(rows, inventory),
            }
        )
    return output


def write_csv(path: Path, rows: list[dict[str, str]]) -> bytes:
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=HUMAN_COLUMNS,
        quoting=csv.QUOTE_ALL,
        lineterminator="\r\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    payload = output.getvalue().encode("utf-8-sig")
    path.write_bytes(payload)
    return payload


def build(catalog_csv: Path, catalog_xlsx: Path, workbook: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    restore_csv = output_dir / "RESTORE.csv"
    restore_xlsx = output_dir / "RESTORE.xlsx"
    human_csv = output_dir / "CATALOG-HUMAN.csv"
    human_xlsx = output_dir / "CATALOG.xlsx"
    shutil.copy2(catalog_csv, restore_csv)
    shutil.copy2(catalog_xlsx, restore_xlsx)
    canonical_rows = read_rows(catalog_csv)
    rows = human_rows(canonical_rows, workbook_index(workbook))
    csv_payload = write_csv(human_csv, rows)
    xlsx_payload = table_xlsx_bytes(
        HUMAN_COLUMNS,
        rows,
        sheet_name="Catalog",
        table_name="HumanCatalog",
    )
    human_xlsx.write_bytes(xlsx_payload)
    summary = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_catalog_csv": str(catalog_csv),
        "source_catalog_xlsx": str(catalog_xlsx),
        "workbook": str(workbook),
        "rows": len(rows),
        "outputs": {
            "restore_csv": {
                "path": str(restore_csv),
                "sha256": "sha256:" + sha256_file(restore_csv),
            },
            "restore_xlsx": {
                "path": str(restore_xlsx),
                "sha256": "sha256:" + sha256_file(restore_xlsx),
            },
            "human_csv": {
                "path": str(human_csv),
                "sha256": "sha256:" + hashlib.sha256(csv_payload).hexdigest(),
            },
            "human_xlsx": {
                "path": str(human_xlsx),
                "sha256": "sha256:" + hashlib.sha256(xlsx_payload).hexdigest(),
            },
        },
    }
    (output_dir / "CATALOG-README.md").write_text(
        "# Paragon Catalog Exports\n\n"
        "`CATALOG.xlsx` is the human-readable review catalog, one row per archive object.\n"
        "`CATALOG-HUMAN.csv` is the CSV version of the same human-readable view.\n"
        "`RESTORE.csv` and `RESTORE.xlsx` are the full canonical machine/audit exports.\n",
        encoding="utf-8",
    )
    (output_dir / "catalog-export-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-csv", required=True, type=Path)
    parser.add_argument("--catalog-xlsx", required=True, type=Path)
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(
        json.dumps(
            build(
                args.catalog_csv.resolve(strict=True),
                args.catalog_xlsx.resolve(strict=True),
                args.workbook.resolve(strict=True),
                args.output_dir.resolve(),
            ),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
