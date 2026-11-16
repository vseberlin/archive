#!/usr/bin/env python3
"""Prepare the current Trafo source-remnant review list without media I/O.

The input is the frozen final-human-overrides source plan. Candidate files are
checked with stat only; no candidate is opened, hashed, copied, moved, or
deleted. Output is confined to the Trafo work root.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PLAN_ROOT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/source-plan/"
    "plan-20260725T-final-human-overrides"
)
SOURCE_DRAIN = PLAN_ROOT / "drain-candidates.csv"
SOURCE_SELECTED = PLAN_ROOT / "selected-sources-proposal.csv"
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v1"
)

GROUPS = [
    {
        "order": 1,
        "id": "TRO-1957-037-02",
        "label": "TRO-1957-037-02",
        "interpretation": "possible distinct issue or issue-part",
        "next_action": "visual comparison with accepted related issue records",
    },
    {
        "order": 2,
        "id": "TRO-1989-Deckblatt",
        "label": "TRO-1989-Deckblatt",
        "interpretation": "cover or bound-volume front matter",
        "next_action": "review whether this belongs to the unit as collection-level material",
    },
    {
        "order": 3,
        "id": "handbook-pdfs",
        "label": "Handbook PDFs",
        "interpretation": "related reference material, not a Trafo issue",
        "next_action": "decide unit boundary or route to another unit",
    },
    {
        "order": 4,
        "id": "alternate-1953-special-pdfs",
        "label": "Alternate 1953 special PDFs",
        "interpretation": "possible alternates for reviewed 1953 special issues",
        "next_action": "compare visually with accepted 1953 special issues",
    },
    {
        "order": 5,
        "id": "probable-alias",
        "label": "Probable aliases",
        "interpretation": "filename normalization or source-label anomalies",
        "next_action": "review in bounded alias families against accepted objects",
    },
    {
        "order": 6,
        "id": "unresolved-1951-special",
        "label": "Unresolved 1951 specials",
        "interpretation": "possible special issues not resolved to accepted records",
        "next_action": "visual and bibliographic review",
    },
]
GROUP_BY_ID = {row["id"]: row for row in GROUPS}

OUTPUT_FIELDS = [
    "review_order",
    "review_group",
    "group_label",
    "normalized_inventory",
    "path",
    "size_bytes",
    "candidate_class",
    "source_reason",
    "path_exists",
    "size_matches_frozen_plan",
    "review_status",
    "accession_authorized",
    "move_authorized",
    "deletion_authorized",
    "next_action",
]


class DrainListError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(
    path: Path, rows: list[dict[str, Any]], fields: list[str]
) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def source_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def group_for(inventory: str) -> str:
    if inventory in {"TRO-1951-SN", "TRO-1951-SN-02"}:
        return "unresolved-1951-special"
    if inventory.startswith("HANDBUCH-"):
        return "handbook-pdfs"
    if inventory in {"TRO-1953-01-1", "TRO-1953-02-1", "TRO-1953-03-1"}:
        return "alternate-1953-special-pdfs"
    if inventory == "TRO-1989-DECKBLATT":
        return "TRO-1989-Deckblatt"
    if inventory in {"TRO-1957-037-02", "TRO-1957-037-020001"}:
        return "TRO-1957-037-02"
    return "probable-alias"


def selected_paths(rows: list[dict[str, str]]) -> set[str]:
    result: set[str] = set()
    for row in rows:
        pdf = row.get("local_pdf_path", "").strip()
        if pdf:
            result.add(pdf)
        for path in row.get("local_tiff_paths", "").split(" | "):
            if path.strip():
                result.add(path.strip())
    return result


def prepare() -> dict[str, Any]:
    allowed = Path("/mnt/volume/iss-archive-work/trafo")
    if allowed not in OUTPUT.parents:
        raise DrainListError("output is outside the authorized work root")
    if OUTPUT.exists():
        raise DrainListError(f"refusing existing output: {OUTPUT}")
    for path in (SOURCE_DRAIN, SOURCE_SELECTED):
        if not path.is_file() or path.is_symlink():
            raise DrainListError(f"frozen source-plan input unavailable: {path}")

    selected = read_csv(SOURCE_SELECTED)
    drain = read_csv(SOURCE_DRAIN)
    selected_inventories = {row["normalized_inventory"] for row in selected}
    retained_paths = selected_paths(selected)

    outside_md = [
        row
        for row in drain
        if row["normalized_inventory"] not in selected_inventories
    ]
    accessioned_1988 = [
        row
        for row in outside_md
        if row["normalized_inventory"].startswith("TRO-1988-")
    ]
    remaining = [
        row
        for row in outside_md
        if not row["normalized_inventory"].startswith("TRO-1988-")
    ]

    if len(accessioned_1988) != 294:
        raise DrainListError("expected 294 accessioned 1988 TIFF candidates")
    if len(remaining) != 89:
        raise DrainListError(f"expected 89 remaining paths, got {len(remaining)}")
    if len({row["path"] for row in remaining}) != len(remaining):
        raise DrainListError("remaining candidate paths are not unique")
    overlaps = sorted({row["path"] for row in remaining} & retained_paths)
    if overlaps:
        raise DrainListError(f"drain candidates overlap selected paths: {overlaps}")

    output_rows: list[dict[str, Any]] = []
    for row in remaining:
        source = Path(row["path"])
        exists = source.is_file()
        observed_size = source.stat().st_size if exists else None
        frozen_size = int(row["size_bytes"])
        group_id = group_for(row["normalized_inventory"])
        group = GROUP_BY_ID[group_id]
        output_rows.append(
            {
                "review_order": group["order"],
                "review_group": group_id,
                "group_label": group["label"],
                "normalized_inventory": row["normalized_inventory"],
                "path": row["path"],
                "size_bytes": frozen_size,
                "candidate_class": row["candidate_class"],
                "source_reason": row["reason"],
                "path_exists": "yes" if exists else "no",
                "size_matches_frozen_plan": (
                    "yes" if exists and observed_size == frozen_size else "no"
                ),
                "review_status": "unreviewed",
                "accession_authorized": "no",
                "move_authorized": "no",
                "deletion_authorized": "no",
                "next_action": group["next_action"],
            }
        )

    output_rows.sort(
        key=lambda row: (
            int(row["review_order"]),
            str(row["normalized_inventory"]),
            str(row["path"]),
        )
    )
    if any(
        row["path_exists"] != "yes"
        or row["size_matches_frozen_plan"] != "yes"
        for row in output_rows
    ):
        raise DrainListError("one or more candidate identities changed")

    counts = Counter(row["review_group"] for row in output_rows)
    bytes_by_group: Counter[str] = Counter()
    for row in output_rows:
        bytes_by_group[row["review_group"]] += int(row["size_bytes"])
    expected_counts = {
        "TRO-1957-037-02": 8,
        "TRO-1989-Deckblatt": 6,
        "handbook-pdfs": 4,
        "alternate-1953-special-pdfs": 6,
        "probable-alias": 63,
        "unresolved-1951-special": 2,
    }
    if dict(counts) != expected_counts:
        raise DrainListError(f"group counts differ: {dict(counts)}")
    total_bytes = sum(int(row["size_bytes"]) for row in output_rows)
    if total_bytes != 962_148_272:
        raise DrainListError(f"expected 962148272 bytes, got {total_bytes}")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    write_csv(OUTPUT / "drain-list.csv", output_rows, OUTPUT_FIELDS)
    group_rows = [
        {
            "review_order": group["order"],
            "review_group": group["id"],
            "group_label": group["label"],
            "path_count": counts[group["id"]],
            "size_bytes": bytes_by_group[group["id"]],
            "initial_interpretation": group["interpretation"],
            "next_action": group["next_action"],
            "accession_authorized": "no",
            "move_authorized": "no",
            "deletion_authorized": "no",
        }
        for group in GROUPS
    ]
    write_csv(
        OUTPUT / "group-summary.csv",
        group_rows,
        [
            "review_order",
            "review_group",
            "group_label",
            "path_count",
            "size_bytes",
            "initial_interpretation",
            "next_action",
            "accession_authorized",
            "move_authorized",
            "deletion_authorized",
        ],
    )
    summary = {
        "format": "iss-trafo-source-remnant-review-v1",
        "created_at": utc_now(),
        "status": "prepared-not-authorized",
        "scope_statement": (
            "Recognized Trafo candidate paths outside the 1548-object frozen "
            "museum-digital source plan, excluding the subsequently accessioned "
            "294 TIFF source paths for TRO-1988-001 through TRO-1988-042."
        ),
        "source_plan": str(PLAN_ROOT),
        "source_identities": [
            source_identity(SOURCE_DRAIN),
            source_identity(SOURCE_SELECTED),
        ],
        "source_drain_rows": len(drain),
        "outside_frozen_md_scope_before_1988_exclusion": len(outside_md),
        "accessioned_1988_paths_excluded": len(accessioned_1988),
        "remaining_paths": len(output_rows),
        "remaining_bytes": total_bytes,
        "all_paths_present": True,
        "all_sizes_match_frozen_plan": True,
        "selected_path_overlap": 0,
        "candidate_files_opened": 0,
        "candidate_files_hashed": 0,
        "candidate_files_copied": 0,
        "candidate_files_moved": 0,
        "candidate_files_deleted": 0,
        "authorization": {
            "accession": False,
            "move": False,
            "deletion": False,
        },
        "groups": group_rows,
    }
    write_json(OUTPUT / "summary.json", summary)
    write_text(
        OUTPUT / "README.md",
        """# Trafo source-remnant drain review

Status: **prepared for review; no action authorized**

This package lists the 89 currently recognized Trafo candidate paths that
remain outside the frozen 1548-object museum-digital source plan after the 294
TIFF source paths for the accessioned 1988 sequence were removed from the
queue.

The list is a review queue, not a deletion or retirement plan. A filename or
normalization mismatch does not prove that a file is redundant, unrelated, or
a separate object. Each bounded group requires its own visual and
bibliographic decision.

Preparation used the frozen source-plan CSVs and file metadata only. Every
listed path currently exists and has the recorded size. Candidate contents
were not opened, copied, moved, deleted, or hashed. The source USB remained
read-only.

Files:

- `drain-list.csv`: all 89 exact source paths and explicit authorization flags;
- `group-summary.csv`: the six bounded review groups in proposed order;
- `summary.json`: scope, counts, source identities, and no-action receipt.
""",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2, sort_keys=True))
