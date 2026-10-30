#!/usr/bin/env python3
"""Flag selected TIFF byte streams that differ from normalized preservation TIFFs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


FIELDS = (
    "flag",
    "inventory",
    "entity_id",
    "selected_source_path",
    "selected_source_sha256",
    "selected_source_size_bytes",
    "primary_preservation_path",
    "independent_preservation_path",
    "preservation_sha256",
    "preservation_size_bytes",
    "source_vs_preservation_sha_match",
    "exact_source_in_active_package",
    "usb_exact_occurrence_count",
    "usb_exact_occurrence_paths",
    "required_disposition",
    "deletion_authorized",
    "reason",
)


def build_flags(selected: list[dict], drain_rows: list[dict]) -> list[dict]:
    usb_by_digest: dict[str, list[str]] = defaultdict(list)
    for row in drain_rows:
        if row["occurrence_root"] == "/media/vladimir/Elements":
            usb_by_digest[row["selected_sha256"]].append(row["occurrence_path"])
    flags = []
    for row in selected:
        source = Path(row["selected_source_path"])
        if source.suffix.casefold() not in {".tif", ".tiff"}:
            continue
        if row["digest"] == row["preservation_digest"]:
            continue
        usb_paths = sorted(usb_by_digest[row["digest"]])
        flags.append(
            {
                "flag": "RETAIN_EXACT_ORIGINAL_TIFF",
                "inventory": row["inventory"],
                "entity_id": row["entity_id"],
                "selected_source_path": row["selected_source_path"],
                "selected_source_sha256": row["digest"],
                "selected_source_size_bytes": row["size_bytes"],
                "primary_preservation_path": row["primary_preservation_path"],
                "independent_preservation_path": row[
                    "independent_preservation_path"
                ],
                "preservation_sha256": row["preservation_digest"],
                "preservation_size_bytes": row["preservation_size_bytes"],
                "source_vs_preservation_sha_match": "false",
                "exact_source_in_active_package": "false",
                "usb_exact_occurrence_count": len(usb_paths),
                "usb_exact_occurrence_paths": " | ".join(usb_paths),
                "required_disposition": (
                    "retain one exact original TIFF occurrence; USB duplicates "
                    "may be retired only after the retained path is reverified"
                ),
                "deletion_authorized": "false",
                "reason": (
                    "accession created a normalized metadata-embedded "
                    "preservation TIFF with a different byte-stream SHA-256"
                ),
            }
        )
    return sorted(flags, key=lambda row: (row["inventory"], row["entity_id"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selected-masters", required=True, type=Path)
    parser.add_argument("--drain-plan", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    parser.add_argument("--output-summary", required=True, type=Path)
    args = parser.parse_args()
    selected = json.loads(args.selected_masters.read_text(encoding="utf-8"))
    with args.drain_plan.open(newline="", encoding="utf-8") as handle:
        drain_rows = list(csv.DictReader(handle))
    flags = build_flags(selected, drain_rows)
    if len(flags) != 19:
        raise SystemExit(f"refusing unexpected TIFF mismatch count: {len(flags)}")
    if args.output_csv.exists() or args.output_summary.exists():
        raise SystemExit("refusing to replace an existing mismatch report")
    with args.output_csv.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(flags)
    summary = {
        "schema": "iss-tiff-source-preservation-mismatch-v1",
        "flag": "RETAIN_EXACT_ORIGINAL_TIFF",
        "records": len(flags),
        "all_source_vs_preservation_sha_match": False,
        "all_exact_sources_present_in_active_package": False,
        "all_deletion_authorized": False,
        "selected_source_roots": sorted(
            {str(Path(row["selected_source_path"]).parent) for row in flags}
        ),
        "usb_exact_occurrences": sum(
            int(row["usb_exact_occurrence_count"]) for row in flags
        ),
        "required_disposition": (
            "Retain one exact original TIFF occurrence for every flagged record."
        ),
    }
    args.output_summary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
