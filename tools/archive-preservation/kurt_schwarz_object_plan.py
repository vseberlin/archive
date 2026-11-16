#!/usr/bin/env python3
"""Build a reconciliation and provisional master-selection plan for Kurt Schwarz.

The plan keeps current museum-digital metadata and historical USB catalog
metadata in separate, provenance-labelled columns.  It never overwrites either
source and performs no source or final-unit mutation.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-object-plan-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
DEFAULT_BATCH_RUN = (
    WORK_ROOT
    / "work/reconciliation/batch-catalogs/"
    "20260729T234000Z-kurt-schwarz-batch-catalogs"
)
DEFAULT_UNUSUAL_RUN = (
    WORK_ROOT
    / "work/reconciliation/unusual-review/"
    "20260730T000000Z-kurt-schwarz-unusual-review"
)
MD_RECOVERY_ROOT = WORK_ROOT / "work/md-image-recovery"

FIELDS = (
    "record_key",
    "record_state",
    "md_object_id",
    "current_inventory",
    "historical_inventory",
    "current_title",
    "historical_title",
    "current_description",
    "historical_description",
    "current_tags",
    "historical_tags",
    "current_places",
    "historical_places",
    "current_date_text",
    "historical_date_text",
    "current_material",
    "historical_material",
    "current_dimensions",
    "historical_dimensions",
    "scan_signature",
    "original_title_filenames",
    "batch_alias_inventories",
    "bag_label",
    "subdivision_ids",
    "subdivisions",
    "md_image_resource_id",
    "md_image_full_url",
    "selected_source_path",
    "selected_source_format",
    "selection_basis",
    "selection_state",
    "source_scope",
    "tiff_candidate_count",
    "jpeg_candidate_count",
    "all_candidate_paths",
    "metadata_difference_fields",
)


class PlanError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise PlanError(f"path escaped allowed root: {resolved}") from error
    return resolved


def load_csv(path: Path, key: str) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    keyed = {row[key]: row for row in rows}
    if len(keyed) != len(rows):
        raise PlanError(f"missing or duplicate {key} in {path}")
    return keyed


def normalized_stem(path: str) -> str:
    return Path(path).stem.casefold().strip()


def scan_from_original_title(value: str) -> str:
    return Path(value.split("//", 1)[0].strip()).stem


def select_source(
    *,
    current_inventory: str,
    historical_inventory: str,
    scan_signature: str,
    original_title_filenames: str,
    alternate_inventories: str,
    candidates: list[str],
) -> tuple[str, str, str, str]:
    tiffs = sorted(
        path for path in candidates if Path(path).suffix.casefold() in {".tif", ".tiff"}
    )
    jpegs = sorted(
        path for path in candidates if Path(path).suffix.casefold() in {".jpg", ".jpeg"}
    )

    original_names = [
        Path(value).stem
        for value in original_title_filenames.split("; ")
        if value
    ]
    original_master_names = [
        value for value in original_names if value.casefold().endswith("_or")
    ]
    source_bases = [
        value.casefold()
        for value in (
            current_inventory,
            historical_inventory,
            scan_signature,
            *original_names,
            *[
                value
                for value in alternate_inventories.split("; ")
                if value
            ],
        )
        if value
    ]
    variant_originals = [
        path
        for path in tiffs
        if any(
            normalized_stem(path) == base + "_or"
            for base in source_bases
        )
    ]
    if variant_originals:
        selected = max(
            variant_originals,
            key=lambda path: (Path(path).stat().st_size, path),
        )
        return (
            selected,
            "tiff",
            "explicit_original_variant_tiff",
            "provisional_multiple_tiff_review",
        )
    preferred_names = [
        *[
            ("original_title_original_tiff", value)
            for value in original_master_names
        ],
        ("current_inventory_tiff", current_inventory),
        ("historical_inventory_tiff", historical_inventory),
        ("scan_signature_tiff", scan_signature),
    ]
    for basis, name in preferred_names:
        if not name:
            continue
        exact = [path for path in tiffs if normalized_stem(path) == name.casefold()]
        if exact:
            selected = max(exact, key=lambda path: (Path(path).stat().st_size, path))
            state = "provisional_multiple_tiff_review" if len(tiffs) > 1 else "provisional"
            return selected, "tiff", basis, state

    if tiffs:
        selected = max(tiffs, key=lambda path: (Path(path).stat().st_size, path))
        state = "provisional_multiple_tiff_review" if len(tiffs) > 1 else "provisional"
        return selected, "tiff", "only_or_largest_tiff_candidate", state

    if jpegs:
        preferred = [
            path for path in jpegs if "/Alle-KS-Bilder_5000px/" in path
        ]
        pool = preferred or jpegs
        selected = max(pool, key=lambda path: (Path(path).stat().st_size, path))
        basis = "curated_5000px_jpeg" if preferred else "largest_jpeg_candidate"
        return selected, "jpeg", basis, "provisional_jpeg_only"

    return "", "", "museum_digital_image_required", "unresolved_no_local_image"


def differences(md: dict[str, str], local: dict[str, str]) -> list[str]:
    pairs = (
        ("inventory", "inventory"),
        ("title", "title"),
        ("description", "description"),
        ("tags", "tags"),
        ("places", "places"),
        ("date_text", "date_text"),
        ("material", "material"),
        ("dimensions", "dimensions"),
    )
    return [
        left
        for left, right in pairs
        if " ".join(md.get(left, "").split()) != " ".join(local.get(right, "").split())
    ]


def build(
    discovery: Path,
    batch_run: Path,
    unusual_run: Path,
    output: Path,
) -> dict[str, Any]:
    md = load_csv(discovery / "md-objects.csv", "md_object_id")
    local = load_csv(discovery / "local-catalog.csv", "historical_md_object_id")
    matches = load_csv(discovery / "filename-reconciliation.csv", "md_object_id")
    with (batch_run / "batch-records.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        batch_rows = list(csv.DictReader(handle))
    batch_by_id: dict[str, list[dict[str, str]]] = {}
    for row in batch_rows:
        if row["mapped_md_object_id"]:
            batch_by_id.setdefault(row["mapped_md_object_id"], []).append(row)
    with (batch_run / "local-only-records.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        batch_local_only = list(csv.DictReader(handle))
    with (discovery / "original-title-map.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        original_title_rows = list(csv.DictReader(handle))
    image_index: dict[str, list[str]] = {}
    with (discovery / "usb-exclusive-candidates.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for candidate in csv.DictReader(handle):
            if candidate["extension"].casefold() not in {
                ".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"
            }:
                continue
            image_index.setdefault(
                Path(candidate["relative_path"]).stem.casefold(), []
            ).append(candidate["source_path"])
    object_ids = sorted(md.keys() | local.keys(), key=int)
    rows: list[dict[str, str]] = []

    for object_id in object_ids:
        current = md.get(object_id, {})
        historical = local.get(object_id, {})
        match = matches.get(object_id, {})
        object_batch_rows = batch_by_id.get(object_id, [])
        batch_aliases = sorted(
            {
                row["normalized_inventory"]
                for row in object_batch_rows
                if row["normalized_inventory"]
                and row["normalized_inventory"].casefold()
                not in {
                    current.get("inventory", "").casefold(),
                    historical.get("inventory", "").casefold(),
                }
            }
        )
        candidates = [
            value for value in match.get("candidate_paths", "").split("; ") if value
        ]
        candidates.extend(
            value
            for row in object_batch_rows
            for value in row["image_candidate_paths"].split("; ")
            if value
        )
        candidates = sorted(set(candidates))
        selected, source_format, basis, state = select_source(
            current_inventory=current.get("inventory", ""),
            historical_inventory=historical.get("inventory", ""),
            scan_signature=historical.get("scan_signature", ""),
            original_title_filenames=match.get("original_title_filename", ""),
            alternate_inventories="; ".join(batch_aliases),
            candidates=candidates,
        )
        recovered_md_image = (
            MD_RECOVERY_ROOT / object_id / f"md-{object_id}.jpg"
        )
        if (
            state == "unresolved_no_local_image"
            and current
            and recovered_md_image.is_file()
        ):
            selected = str(recovered_md_image)
            source_format = "jpeg"
            basis = "museum_digital_full_image_recovery"
            state = "resolved_md_surrogate"
            candidates.append(str(recovered_md_image))
        record_state = (
            "current_md_and_local"
            if current and historical
            else "current_md_only"
            if current
            else "historical_md_deleted"
        )
        rows.append(
            {
                "record_key": f"md:{object_id}",
                "record_state": record_state,
                "md_object_id": object_id,
                "current_inventory": current.get("inventory", ""),
                "historical_inventory": historical.get("inventory", ""),
                "current_title": current.get("title", ""),
                "historical_title": historical.get("title", ""),
                "current_description": current.get("description", ""),
                "historical_description": historical.get("description", ""),
                "current_tags": current.get("tags", ""),
                "historical_tags": historical.get("tags", ""),
                "current_places": current.get("places", ""),
                "historical_places": historical.get("places", ""),
                "current_date_text": current.get("date_text", ""),
                "historical_date_text": historical.get("date_text", ""),
                "current_material": current.get("material", ""),
                "historical_material": historical.get("material", ""),
                "current_dimensions": current.get("dimensions", ""),
                "historical_dimensions": historical.get("dimensions", ""),
                "scan_signature": historical.get("scan_signature", ""),
                "original_title_filenames": match.get(
                    "original_title_filename", ""
                ),
                "batch_alias_inventories": "; ".join(batch_aliases),
                "bag_label": historical.get("bag_label", ""),
                "subdivision_ids": current.get("subdivision_ids", ""),
                "subdivisions": current.get("subdivisions", ""),
                "md_image_resource_id": current.get("image_resource_id", ""),
                "md_image_full_url": current.get("image_full_url", ""),
                "selected_source_path": selected,
                "selected_source_format": source_format,
                "selection_basis": basis,
                "selection_state": state,
                "source_scope": (
                    "museum_digital_recovery"
                    if basis == "museum_digital_full_image_recovery"
                    else "usb_exclusive_scope"
                ),
                "tiff_candidate_count": str(
                    sum(
                        Path(path).suffix.casefold() in {".tif", ".tiff"}
                        for path in candidates
                    )
                ),
                "jpeg_candidate_count": str(
                    sum(
                        Path(path).suffix.casefold() in {".jpg", ".jpeg"}
                        for path in candidates
                    )
                ),
                "all_candidate_paths": "; ".join(candidates),
                "metadata_difference_fields": "|".join(
                    differences(current, historical) if current and historical else []
                ),
            }
        )

    original_local_by_inventory = {
        Path(row["source_filename"]).stem.casefold(): row
        for row in original_title_rows
        if not row["mapped_md_object_id"]
    }
    for local_only in batch_local_only:
        inventory = local_only["normalized_inventory"]
        scan = local_only["scan_signature"]
        candidates = [
            value
            for value in local_only["image_candidate_paths"].split("; ")
            if value
        ]
        original_local = original_local_by_inventory.get(inventory.casefold(), {})
        selected, source_format, basis, state = select_source(
            current_inventory="",
            historical_inventory=inventory,
            scan_signature=scan,
            original_title_filenames=original_local.get("source_filename", ""),
            alternate_inventories="",
            candidates=candidates,
        )
        rows.append(
            {
                "record_key": f"local:{inventory}",
                "record_state": "local_only_batch_catalog",
                "md_object_id": "",
                "current_inventory": "",
                "historical_inventory": inventory,
                "current_title": "",
                "historical_title": local_only["title"],
                "current_description": "",
                "historical_description": local_only["description"],
                "current_tags": "",
                "historical_tags": local_only["tags"],
                "current_places": "",
                "historical_places": local_only["places"],
                "current_date_text": "",
                "historical_date_text": local_only["date_text"],
                "current_material": "",
                "historical_material": local_only["material"],
                "current_dimensions": "",
                "historical_dimensions": local_only["dimensions"],
                "scan_signature": scan,
                "original_title_filenames": original_local.get(
                    "source_filename", ""
                ),
                "batch_alias_inventories": "",
                "bag_label": local_only["bag_label"],
                "subdivision_ids": "",
                "subdivisions": "",
                "md_image_resource_id": "",
                "md_image_full_url": "",
                "selected_source_path": selected,
                "selected_source_format": source_format,
                "selection_basis": basis,
                "selection_state": state,
                "source_scope": "usb_exclusive_scope",
                "tiff_candidate_count": str(
                    sum(
                        Path(path).suffix.casefold() in {".tif", ".tiff"}
                        for path in candidates
                    )
                ),
                "jpeg_candidate_count": str(
                    sum(
                        Path(path).suffix.casefold() in {".jpg", ".jpeg"}
                        for path in candidates
                    )
                ),
                "all_candidate_paths": "; ".join(candidates),
                "metadata_difference_fields": "",
            }
        )

    with (unusual_run / "unusual-reviewed.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        unusual_rows = [
            row for row in csv.DictReader(handle) if row["record_key"]
        ]
    for unusual in unusual_rows:
        inventory = unusual["record_key"].split(":", 1)[1]
        rows.append(
            {
                "record_key": unusual["record_key"],
                "record_state": "local_only_shared_source_review",
                "md_object_id": "",
                "current_inventory": "",
                "historical_inventory": inventory,
                "current_title": "",
                "historical_title": (
                    f"Unidentified local photograph {inventory}"
                ),
                "current_description": "",
                "historical_description": unusual["review_basis"],
                "current_tags": "",
                "historical_tags": "",
                "current_places": "",
                "historical_places": "",
                "current_date_text": "",
                "historical_date_text": "",
                "current_material": "",
                "historical_material": "TIFF",
                "current_dimensions": "",
                "historical_dimensions": "",
                "scan_signature": "",
                "original_title_filenames": Path(
                    unusual["source_path"]
                ).name,
                "batch_alias_inventories": "",
                "bag_label": "",
                "subdivision_ids": "",
                "subdivisions": "",
                "md_image_resource_id": "",
                "md_image_full_url": "",
                "selected_source_path": unusual["source_path"],
                "selected_source_format": "tiff",
                "selection_basis": unusual["review_basis"],
                "selection_state": "reviewed_local_shared_master",
                "source_scope": unusual["source_scope"],
                "tiff_candidate_count": "1",
                "jpeg_candidate_count": "0",
                "all_candidate_paths": unusual["source_path"],
                "metadata_difference_fields": "",
            }
        )

    output.mkdir(parents=True, exist_ok=False)
    with (output / "object-plan.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())

    states: dict[str, int] = {}
    selections: dict[str, int] = {}
    for row in rows:
        states[row["record_state"]] = states.get(row["record_state"], 0) + 1
        selections[row["selection_state"]] = selections.get(row["selection_state"], 0) + 1
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "batch_catalog_run": str(batch_run),
        "unusual_review_run": str(unusual_run),
        "object_records": len(rows),
        "record_state_counts": dict(sorted(states.items())),
        "selection_state_counts": dict(sorted(selections.items())),
        "metadata_sources_kept_separate": True,
        "selection_is_provisional": True,
        "deletion_authorized_rows": 0,
    }
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--batch-run", type=Path, default=DEFAULT_BATCH_RUN)
    parser.add_argument("--unusual-run", type=Path, default=DEFAULT_UNUSUAL_RUN)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(
            args.discovery.expanduser().resolve(),
            args.batch_run.expanduser().resolve(),
            args.unusual_run.expanduser().resolve(),
            output,
        )
    except (PlanError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
