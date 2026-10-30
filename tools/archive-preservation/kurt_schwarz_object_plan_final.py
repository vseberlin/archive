#!/usr/bin/env python3
"""Merge all reviewed local/shared evidence into the final Kurt Schwarz object plan."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-object-plan-final-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_OBJECT_PLAN = WORK_ROOT / (
    "work/reconciliation/object-plans/"
    "20260730T001000Z-kurt-schwarz-object-plan/object-plan.csv"
)
DEFAULT_SHARED_REVIEW = WORK_ROOT / (
    "work/reconciliation/shared-image-review/"
    "20260730T103000Z-google-drive-kurt-schwarz-image-review"
)
DEFAULT_WORKBOOKS = WORK_ROOT / (
    "work/reconciliation/shared-workbooks/"
    "20260730T093000Z-google-drive-kurt-schwarz-workbooks"
)
DEFAULT_VISUAL_REVIEW = WORK_ROOT / (
    "work/reconciliation/visual-review/"
    "20260730T090000Z-kurt-schwarz-visual-review"
)
EXTRA_FIELDS = (
    "additional_alias_inventories",
    "shared_container_members",
    "shared_workbook_record_count",
    "reviewed_visual_state",
    "object_metadata_state",
)


class FinalPlanError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise FinalPlanError(f"path escaped allowed root: {resolved}") from error
    return resolved


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def joined(values: list[str] | set[str]) -> str:
    return "; ".join(sorted({value for value in values if value}))


def atomic_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]
) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(
    object_plan: Path,
    shared_review: Path,
    workbook_run: Path,
    visual_review: Path,
    output: Path,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    base_rows = read_csv(object_plan)
    if not base_rows:
        raise FinalPlanError("base object plan is empty")
    base_fields = tuple(base_rows[0])
    by_key = {row["record_key"]: row for row in base_rows}
    if len(by_key) != len(base_rows):
        raise FinalPlanError("base object plan contains duplicate record keys")

    aliases: dict[str, set[str]] = defaultdict(set)
    members: dict[str, set[str]] = defaultdict(set)
    reviewed_states: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(shared_review / "reviewed-shared-images.csv"):
        key = row["accepted_record_key"]
        aliases[key].add(row["historical_inventory"])
        members[key].add(f"{row['source_container']}::{row['member']}")
        reviewed_states[key].add(row["reviewed_mapping_state"])
    workbook_counts: Counter[str] = Counter()
    for row in read_csv(workbook_run / "shared-workbook-records.csv"):
        for key in row["mapped_record_keys"].split("; "):
            if key:
                workbook_counts[key] += 1
                if row["inventory"]:
                    aliases[key].add(row["inventory"])
    md_visual = {
        row["md_object_id"]: row["reviewed_visual_state"]
        for row in read_csv(visual_review / "reviewed-visual-exceptions.csv")
    }

    additions = read_csv(shared_review / "local-only-object-additions.csv")
    for addition in additions:
        key = addition["record_key"]
        if key in by_key:
            raise FinalPlanError(f"local-only addition collides with base plan: {key}")
        row = {field: "" for field in base_fields}
        row.update(
            {
                "record_key": key,
                "record_state": addition["record_state"],
                "historical_inventory": addition["historical_inventory"],
                "historical_title": addition["embedded_title"],
                "historical_description": addition["embedded_description"],
                "historical_material": addition["material"],
                "scan_signature": addition["scan_signature"],
                "original_title_filenames": addition["embedded_title"],
                "selected_source_path": addition["selected_tiff_path"],
                "selected_source_format": "tiff",
                "selection_basis": "reviewed_local_only_16_bit_tiff",
                "selection_state": "reviewed_local_only_tiff_master",
                "source_scope": "usb_exclusive_scope_and_shared_container",
                "tiff_candidate_count": "1",
                "jpeg_candidate_count": "1",
                "all_candidate_paths": (
                    addition["selected_tiff_path"]
                    + "; "
                    + next(iter(members[key]))
                ),
            }
        )
        by_key[key] = row

    rows: list[dict[str, str]] = []
    for key, row in by_key.items():
        current_aliases = {
            row["current_inventory"].casefold(),
            row["historical_inventory"].casefold(),
            *(
                value.casefold()
                for value in row["batch_alias_inventories"].split("; ")
                if value
            ),
        }
        extra_aliases = {
            value for value in aliases[key] if value.casefold() not in current_aliases
        }
        visual_state = md_visual.get(row["md_object_id"], "")
        if not visual_state and reviewed_states[key]:
            visual_state = joined(reviewed_states[key])
        result = dict(row)
        result.update(
            {
                "additional_alias_inventories": joined(extra_aliases),
                "shared_container_members": joined(members[key]),
                "shared_workbook_record_count": str(workbook_counts[key]),
                "reviewed_visual_state": visual_state,
                "object_metadata_state": (
                    "current_and_historical_provenance_kept_separate"
                    if row["md_object_id"]
                    else "local_only_metadata_preserved"
                ),
            }
        )
        rows.append(result)
    rows.sort(
        key=lambda row: (
            row["current_inventory"]
            or row["historical_inventory"]
            or row["record_key"]
        ).casefold()
    )
    selected = [row["selected_source_path"] for row in rows if row["selected_source_path"]]
    if len(selected) != len(set(selected)):
        raise FinalPlanError("final object plan contains duplicate selected sources")
    fields = base_fields + EXTRA_FIELDS
    atomic_csv(output / "object-plan-final.csv", fields, rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "base_object_plan": str(object_plan),
        "object_records": len(rows),
        "record_state_counts": dict(Counter(row["record_state"] for row in rows)),
        "selected_sources": len(selected),
        "local_only_shared_container_objects": len(additions),
        "shared_member_links": sum(bool(row["shared_container_members"]) for row in rows),
        "shared_workbook_linked_objects": sum(
            bool(int(row["shared_workbook_record_count"])) for row in rows
        ),
        "unresolved_rows": 0,
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-plan", type=Path, default=DEFAULT_OBJECT_PLAN)
    parser.add_argument("--shared-review", type=Path, default=DEFAULT_SHARED_REVIEW)
    parser.add_argument("--workbooks", type=Path, default=DEFAULT_WORKBOOKS)
    parser.add_argument("--visual-review", type=Path, default=DEFAULT_VISUAL_REVIEW)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(
            args.object_plan.expanduser().resolve(),
            args.shared_review.expanduser().resolve(),
            args.workbooks.expanduser().resolve(),
            args.visual_review.expanduser().resolve(),
            output,
        )
    except (OSError, csv.Error, ValueError, FinalPlanError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
