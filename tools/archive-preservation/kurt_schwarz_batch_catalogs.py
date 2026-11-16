#!/usr/bin/env python3
"""Reconcile Kurt-Schwarz USB batch CSV catalogs with current object records."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-batch-catalog-reconciliation-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"}
FIELDS = (
    "source_path",
    "source_relative_path",
    "source_row",
    "source_inventory",
    "normalized_inventory",
    "mapped_md_object_id",
    "current_inventory",
    "mapping_basis",
    "title",
    "description",
    "object_type",
    "material",
    "dimensions",
    "tags",
    "date_text",
    "places",
    "scan_signature",
    "bag_label",
    "image_name",
    "image_candidate_paths",
)


class BatchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise BatchError(f"path escaped allowed root: {resolved}") from error
    return resolved


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_inventory(value: str) -> str:
    value = clean(value)
    value = re.sub(r"(?i)\.(?:tif|tiff|jpe?g|psd|png)$", "", value)
    value = re.sub(
        r"(?i)^(KS-[5-9]-WF)-(?=\d)",
        r"\1_",
        value,
    )
    return value


def normalized_title(value: str) -> str:
    value = clean(value).casefold()
    value = value.replace("..", ".")
    return value.strip(" .")


def title_core(value: str) -> str:
    value = normalized_title(value)
    value = re.split(
        r"(?i)(?:sw-foto|farbfoto|foto)\s*(?:,|©)",
        value,
        maxsplit=1,
    )[0]
    return value.strip(" .,:;")


def first(row: dict[str, str], *fields: str) -> str:
    for field in fields:
        value = clean(row.get(field, ""))
        if value:
            return value
    return ""


def tags(row: dict[str, str]) -> str:
    explicit = first(row, "Schlagwort", "KEYWORDS")
    if explicit:
        return explicit
    values = [
        clean(value)
        for key, value in row.items()
        if re.fullmatch(r"tag\d+", key, re.IGNORECASE) and clean(value)
    ]
    return "; ".join(values)


def scan_signature(row: dict[str, str]) -> str:
    value = first(
        row,
        "Scansignatur",
        "remarks_short",
        "remarks_short4",
        "Kurznotiz/Scannummern",
        "Kurznotiz",
    )
    if not value:
        return ""
    value = value.split("//", 1)[0].split(";", 1)[0].strip()
    return Path(value).stem


def read_source_csv(path: Path) -> list[dict[str, str]]:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise BatchError(f"unsupported CSV encoding: {path}")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def image_index(discovery: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    with (discovery / "usb-exclusive-candidates.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row["extension"].casefold() not in IMAGE_EXTENSIONS:
                continue
            result[Path(row["relative_path"]).stem.casefold()].append(
                row["source_path"]
            )
    return result


def candidate_images(
    index: dict[str, list[str]],
    inventory: str,
    scan: str,
    image_name: str,
) -> list[str]:
    keys = {
        normalize_inventory(value).casefold()
        for value in (inventory, scan, image_name)
        if value
    }
    return sorted({path for key in keys for path in index.get(key, [])})


def choose_local_only(rows: list[dict[str, str]]) -> dict[str, str]:
    def score(row: dict[str, str]) -> tuple[int, int, int, str]:
        path = row["source_relative_path"].casefold()
        return (
            1 if "final" in path and "for-lr" not in path else 0,
            1 if "final" in path else 0,
            sum(bool(row[field]) for field in FIELDS),
            row["source_relative_path"],
        )

    return max(rows, key=score)


def build(discovery: Path, output: Path) -> dict[str, Any]:
    with (discovery / "md-objects.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        md_rows = list(csv.DictReader(handle))
    with (discovery / "local-catalog.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        local_rows = list(csv.DictReader(handle))
    current_by_id = {row["md_object_id"]: row for row in md_rows}
    by_inventory: dict[str, set[str]] = defaultdict(set)
    by_title: dict[str, set[str]] = defaultdict(set)
    title_cores: dict[str, set[str]] = defaultdict(set)
    by_scan: dict[str, set[str]] = defaultdict(set)
    by_wf_serial: dict[str, set[str]] = defaultdict(set)
    for row in md_rows:
        by_inventory[normalize_inventory(row["inventory"]).casefold()].add(
            row["md_object_id"]
        )
        wf_match = re.match(r"(?i)^KS-[5-9]-WF_(.+)$", row["inventory"])
        if wf_match:
            by_wf_serial[wf_match.group(1).casefold()].add(row["md_object_id"])
        if normalized_title(row["title"]):
            by_title[normalized_title(row["title"])].add(row["md_object_id"])
        if title_core(row["title"]):
            title_cores[title_core(row["title"])].add(row["md_object_id"])
    for row in local_rows:
        object_id = row["historical_md_object_id"]
        by_inventory[normalize_inventory(row["inventory"]).casefold()].add(object_id)
        wf_match = re.match(r"(?i)^KS-[5-9]-WF_(.+)$", row["inventory"])
        if wf_match:
            by_wf_serial[wf_match.group(1).casefold()].add(object_id)
        if normalized_title(row["title"]):
            by_title[normalized_title(row["title"])].add(object_id)
        if title_core(row["title"]):
            title_cores[title_core(row["title"])].add(object_id)
        if row["scan_signature"]:
            by_scan[row["scan_signature"].casefold()].add(object_id)

    index = image_index(discovery)
    csv_paths: list[tuple[str, Path]] = []
    with (discovery / "usb-exclusive-candidates.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            if row["extension"].casefold() == ".csv":
                csv_paths.append((row["relative_path"], Path(row["source_path"])))

    result: list[dict[str, str]] = []
    source_errors: list[dict[str, str]] = []
    for relative, path in sorted(csv_paths):
        try:
            source_rows = read_source_csv(path)
        except (OSError, csv.Error, BatchError) as error:
            source_errors.append(
                {"source_path": str(path), "error": f"{type(error).__name__}: {error}"}
            )
            continue
        if not source_rows:
            continue
        fields = source_rows[0].keys()
        inventory_field = next(
            (field for field in ("inventory_number", "Inventarnummer") if field in fields),
            "",
        )
        if not inventory_field:
            continue
        for row_number, source in enumerate(source_rows, 2):
            source_inventory = normalize_inventory(source.get(inventory_field, ""))
            if not re.match(
                r"(?i)^KS-[5-9]-(?:BZ|NB|WF|ZP)[_-]",
                source_inventory,
            ):
                continue
            title = first(
                source,
                "object_title",
                "Objektname",
                "object_titleSOURCE",
            )
            scan = scan_signature(source)
            explicit_id = first(source, "md:number", "md_object_id")
            mapping_basis = ""
            mapped = ""
            if explicit_id.isdigit() and explicit_id in current_by_id:
                mapped = explicit_id
                mapping_basis = "explicit_current_md_object_id"
            if not mapped:
                ids = by_inventory.get(source_inventory.casefold(), set())
                if len(ids) == 1:
                    mapped = next(iter(ids))
                    mapping_basis = "exact_or_normalized_inventory"
            if not mapped:
                wf_match = re.match(
                    r"(?i)^KS-[5-9]-WF_(.+)$", source_inventory
                )
                ids = (
                    by_wf_serial.get(wf_match.group(1).casefold(), set())
                    if wf_match
                    else set()
                )
                if len(ids) == 1:
                    mapped = next(iter(ids))
                    mapping_basis = "unique_wf_serial"
            if not mapped and title:
                ids = by_title.get(normalized_title(title), set())
                if len(ids) == 1:
                    mapped = next(iter(ids))
                    mapping_basis = "unique_normalized_title"
            if not mapped and title_core(title):
                source_core = title_core(title)
                ids = {
                    object_id
                    for current_core, object_ids in title_cores.items()
                    if source_core in current_core or current_core in source_core
                    for object_id in object_ids
                }
                if len(ids) == 1:
                    mapped = next(iter(ids))
                    mapping_basis = "unique_title_core_containment"
            if not mapped and scan:
                ids = by_scan.get(scan.casefold(), set())
                if len(ids) == 1:
                    mapped = next(iter(ids))
                    mapping_basis = "unique_scan_signature"

            image_name = first(source, "image_name1", "Abbildung")
            candidates = candidate_images(
                index, source_inventory, scan, image_name
            )
            result.append(
                {
                    "source_path": str(path),
                    "source_relative_path": relative,
                    "source_row": str(row_number),
                    "source_inventory": first(source, inventory_field),
                    "normalized_inventory": source_inventory,
                    "mapped_md_object_id": mapped,
                    "current_inventory": current_by_id.get(mapped, {}).get(
                        "inventory", ""
                    ),
                    "mapping_basis": mapping_basis or "unmapped_local_record",
                    "title": title,
                    "description": first(
                        source,
                        "object_description",
                        "Beschreibung",
                        "object_descriptionSOURCE",
                    ),
                    "object_type": first(source, "object_type", "Object type"),
                    "material": first(
                        source,
                        "object_material_technique",
                        "Material / Technik",
                    ),
                    "dimensions": first(
                        source, "object_dimensions", "Maße"
                    ),
                    "tags": tags(source),
                    "date_text": first(
                        source, "taken_when1", "Zeit", "Datum"
                    ),
                    "places": "; ".join(
                        value
                        for value in (
                            first(source, "taken_where1", "Ort"),
                            first(source, "related_place1"),
                            first(source, "related_place2"),
                        )
                        if value
                    ),
                    "scan_signature": scan,
                    "bag_label": first(
                        source,
                        "remarks_long",
                        "remarks long",
                        "Archivsignatur; Beschriftung Themengruppe | Negativtasche",
                        "Taschenbeschriftung",
                        "Notiz/Negativtaschen-Beschriftung",
                    ),
                    "image_name": image_name,
                    "image_candidate_paths": "; ".join(candidates),
                }
            )

    result.sort(
        key=lambda row: (
            row["normalized_inventory"].casefold(),
            row["source_relative_path"],
            int(row["source_row"]),
        )
    )
    local_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in result:
        if not row["mapped_md_object_id"]:
            local_groups[row["normalized_inventory"].casefold()].append(row)
    local_only = [
        choose_local_only(rows)
        for _, rows in sorted(local_groups.items())
    ]

    output.mkdir(parents=True, exist_ok=False)
    for filename, rows in (
        ("batch-records.csv", result),
        ("local-only-records.csv", local_only),
    ):
        with (output / filename).open(
            "x", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
    with (output / "errors.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("source_path", "error"), quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(source_errors)
        handle.flush()
        os.fsync(handle.fileno())

    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "csv_sources_considered": len(csv_paths),
        "batch_record_rows": len(result),
        "unique_batch_inventories": len(
            {row["normalized_inventory"].casefold() for row in result}
        ),
        "mapping_basis_counts": dict(
            Counter(row["mapping_basis"] for row in result)
        ),
        "local_only_records": len(local_only),
        "local_only_inventories": [
            row["normalized_inventory"] for row in local_only
        ],
        "errors": len(source_errors),
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
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(args.discovery.expanduser().resolve(), output)
    except (BatchError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
