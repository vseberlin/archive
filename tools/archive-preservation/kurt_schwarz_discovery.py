#!/usr/bin/env python3
"""Build the bounded Kurt-Schwarz discovery register from reused evidence.

This tool does not copy, modify, move, or delete USB files. It reuses the
completed full-restore inventory and an immutable museum-digital capture.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree


FORMAT = "iss-kurt-schwarz-discovery-v1"
SOURCE_MOUNT = Path("/media/vladimir/Elements")
SOURCE_ROOT = SOURCE_MOUNT / "restore"
SOURCE_UUID = "6CAAB316AAB2DC30"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_INVENTORY = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore/restore-inventory.csv"
)
DEFAULT_MD_RUN = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/"
    "museum-digital/runs/20260729T175740Z-kurt-schwarz"
)
DEFAULT_LOCAL_CATALOG = (
    SOURCE_ROOT
    / "archive/WF/Kurt Schwarz/"
    "mdExport_Gesamtverzeichnis_Kurt-Schwarz_4-LR.csv"
)
DEFAULT_ORIGINAL_TITLE_MAP = (
    SOURCE_ROOT
    / "archive/WF/Kurt Schwarz/KS-alle-Bilder_für-Originaltitel.xlsx"
)

EXCLUSIVE_ROOTS = (
    "archive/WF/Kurt Schwarz/",
    "archive/WF/Ausstellung_Kurt-Schwarz/",
    "archive/Schwarz, Kurt/",
    "archive/WF/Canon-SCANS-freierOrdner/Kurt_Schwarz_Albert/",
    "archive/WF/Freistellen/Schwarz, Kurt/",
)
SHARED_ROOTS = (
    "archive/WF/Lightroom-Katalog_nur-WF/",
    "archive/WF/Daten/Lightroom-Kataloge-live/",
    "archive/WF/WF-Sender Projekt/Webseite/",
    "archive/WF/Kurt Schwarz/Backup_Lightroom_KS/",
    (
        "archive/WF/Ausstellung_Kurt-Schwarz/Reihenfolge_Ausstellung-KS/"
        "Ausstellung_Reihenfolge/"
    ),
)
NAME_PATTERNS = (
    ("kurt_schwarz", re.compile(r"(?i)kurt[\s_.-]*schwarz")),
    ("schwarz_kurt", re.compile(r"(?i)schwarz[\s_,.-]*kurt")),
    (
        "ks_inventory",
        re.compile(r"(?i)(?:^|[/\\])KS-[5-9]-(?:BZ|NB|WF|ZP)[_-]"),
    ),
)
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp", ".psd"}
DOCUMENT_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".txt",
    ".xml",
    ".json",
    ".jsonl",
    ".html",
    ".htm",
    ".pdf",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".ods",
    ".docx",
    ".doc",
    ".odt",
    ".pptx",
    ".ppt",
    ".zip",
    ".7z",
    ".rar",
    ".lrcat",
    ".db",
    ".sqlite",
    ".sqlite3",
}

SCOPE_FIELDS = [
    "source_path",
    "relative_path",
    "size_bytes",
    "mtime_ns",
    "extension",
    "category",
    "scope_state",
    "scope_basis",
    "scope_root",
    "deletion_authorized",
]
OBJECT_FIELDS = [
    "md_object_id",
    "inventory",
    "title",
    "description",
    "object_type",
    "material",
    "dimensions",
    "date_text",
    "creator",
    "places",
    "tags",
    "collection_ids",
    "collections",
    "subdivision_ids",
    "subdivisions",
    "image_resource_id",
    "image_filename",
    "image_preview_url",
    "image_full_url",
    "image_asset_preview_url",
    "image_asset_full_url",
    "image_rights",
    "image_owner",
    "last_updated",
    "object_url",
]
LOCAL_FIELDS = [
    "inventory",
    "title",
    "description",
    "tags",
    "historical_md_object_id",
    "scan_signature",
    "bag_label",
    "places",
    "date_text",
    "material",
    "dimensions",
]
ORIGINAL_TITLE_FIELDS = [
    "sequence",
    "source_filename",
    "mapped_md_object_id",
    "mapping_basis",
    "current_inventory",
    "title",
    "description_and_bag",
    "tags_and_batch",
    "scan_and_dimensions",
    "places",
    "date_text",
]
DRIFT_FIELDS = [
    "state",
    "md_object_id",
    "current_inventory",
    "local_inventory",
    "current_title",
    "local_title",
    "basis",
]
MATCH_FIELDS = [
    "record_state",
    "md_object_id",
    "inventory",
    "historical_inventory",
    "scan_signature",
    "original_title_filename",
    "current_inventory_matches",
    "historical_inventory_matches",
    "scan_signature_matches",
    "original_title_filename_matches",
    "variant_matches",
    "tiff_matches",
    "jpeg_matches",
    "candidate_paths",
    "match_state",
]


class DiscoveryError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_is_read_only(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    return bool(os.statvfs(path).f_flag & readonly_flag)


def verify_source() -> None:
    if not SOURCE_ROOT.is_dir():
        raise DiscoveryError(f"USB restore root unavailable: {SOURCE_ROOT}")
    if not root_is_read_only(SOURCE_MOUNT):
        raise DiscoveryError(f"USB source is not read-only: {SOURCE_MOUNT}")
    device = os.stat(SOURCE_MOUNT).st_dev
    if device == os.stat(WORK_ROOT.parent).st_dev:
        raise DiscoveryError("USB and work root unexpectedly share a device")


def ensure_work_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(WORK_ROOT.resolve())
    except ValueError as error:
        raise DiscoveryError(f"output escaped Kurt-Schwarz work root: {resolved}") from error
    return resolved


def read_csv(path: Path, *, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=delimiter))


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DiscoveryError(f"refusing to overwrite discovery evidence: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
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


def write_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise DiscoveryError(f"refusing to overwrite discovery evidence: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def result_objects(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    objects = results.get("objects")
    if isinstance(objects, dict):
        return [row for row in objects.values() if isinstance(row, dict)]
    if isinstance(objects, list):
        return [row for row in objects if isinstance(row, dict)]
    return []


def load_md_objects(run: Path) -> list[dict[str, Any]]:
    coverage = json.loads((run / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("format") != "iss-md-export-discovery-v1":
        raise DiscoveryError("unsupported museum-digital capture")
    if coverage.get("query") != "collection:783":
        raise DiscoveryError("museum-digital capture is not collection 783")
    if coverage.get("request_failures") != 0:
        raise DiscoveryError("museum-digital capture has request failures")
    objects: list[dict[str, Any]] = []
    for path in sorted((run / "raw").glob("*.json")):
        objects.extend(result_objects(json.loads(path.read_text(encoding="utf-8"))))
    ids = [str(row.get("object_id") or "") for row in objects]
    if len(objects) != int(coverage.get("captured_objects") or 0):
        raise DiscoveryError("museum-digital object count differs from coverage")
    if len(ids) != len(set(ids)):
        raise DiscoveryError("museum-digital capture has duplicate object IDs")
    return objects


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def joined_named(rows: Any, key: str) -> str:
    if not isinstance(rows, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = clean(row.get(key))
        if value and value.casefold() not in seen:
            values.append(value)
            seen.add(value.casefold())
    return "; ".join(values)


def event_values(rows: Any, family: str) -> str:
    if not isinstance(rows, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        child = row.get(family)
        if not isinstance(child, dict):
            continue
        key = {
            "time": "time_name",
            "people": "people_display_name",
            "place": "place_name",
        }[family]
        value = clean(child.get(key))
        if value and value.casefold() not in seen:
            values.append(value)
            seen.add(value.casefold())
    return "; ".join(values)


def iiif_urls(resource_id: str) -> tuple[str, str]:
    base = f"https://berlin.museum-digital.de/apis/iiif-image/{resource_id}"
    return f"{base}/full/1200,/0/default.jpg", f"{base}/full/full/0/default.jpg"


def flatten_md_object(item: dict[str, Any]) -> dict[str, str]:
    collections = [
        row for row in item.get("object_collection", []) if isinstance(row, dict)
    ]
    collection_ids = [str(row.get("collection_id") or "") for row in collections]
    collection_names = [clean(row.get("collection_name")) for row in collections]
    subdivisions = [
        (cid, name)
        for cid, name in zip(collection_ids, collection_names)
        if cid != "783"
    ]
    images = [row for row in item.get("object_images", []) if isinstance(row, dict)]
    image = next(
        (row for row in images if clean(row.get("is_main")).casefold() == "j"),
        images[0] if images else {},
    )
    resource_id = clean(image.get("quell_id"))
    preview_url, full_url = iiif_urls(resource_id) if resource_id else ("", "")
    image_folder = clean(image.get("folder")).strip("/")
    image_filename = clean(image.get("filename_loc"))
    asset_base = (
        f"https://asset.museum-digital.org/berlin/{image_folder}"
        if image_folder
        else ""
    )
    events = [row for row in item.get("object_events", []) if isinstance(row, dict)]
    relation_places = [
        row for row in item.get("object_relation_places", []) if isinstance(row, dict)
    ]
    creators = event_values(events, "people")
    if not creators:
        creators = clean(image.get("creator")) or clean(image.get("owner"))
    dates = event_values(events, "time")
    places = "; ".join(
        value
        for value in (event_values(events, "place"), event_values(relation_places, "place"))
        if value
    )
    return {
        "md_object_id": clean(item.get("object_id")),
        "inventory": clean(item.get("object_inventory_number")),
        "title": clean(item.get("object_name")),
        "description": clean(item.get("object_description")),
        "object_type": clean(item.get("object_type")),
        "material": clean(item.get("object_material_technique")),
        "dimensions": clean(item.get("object_dimensions")),
        "date_text": dates,
        "creator": creators,
        "places": places,
        "tags": joined_named(item.get("object_tags"), "tag_name"),
        "collection_ids": "; ".join(collection_ids),
        "collections": "; ".join(collection_names),
        "subdivision_ids": "; ".join(row[0] for row in subdivisions),
        "subdivisions": "; ".join(row[1] for row in subdivisions),
        "image_resource_id": resource_id,
        "image_filename": image_filename,
        "image_preview_url": preview_url,
        "image_full_url": full_url,
        "image_asset_preview_url": (
            f"{asset_base}/500w_{image_filename}"
            if asset_base and image_filename
            else ""
        ),
        "image_asset_full_url": (
            f"{asset_base}/{image_filename}"
            if asset_base and image_filename
            else ""
        ),
        "image_rights": clean(image.get("rights")),
        "image_owner": clean(image.get("owner")),
        "last_updated": clean(item.get("object_last_updated")),
        "object_url": (
            "https://berlin.museum-digital.de/object/"
            + clean(item.get("object_id"))
        ),
    }


def normalize_inventory(value: str) -> str:
    value = clean(value)
    value = re.sub(r"(?i)\.(?:tif|tiff|jpe?g)$", "", value)
    return value


def scan_signature(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    value = re.split(r"\s*//|\s*;|\s+", value, maxsplit=1)[0]
    return normalize_inventory(value)


def load_local_catalog(path: Path) -> list[dict[str, str]]:
    source = read_csv(path, delimiter=";")
    rows = []
    for row in source:
        rows.append(
            {
                "inventory": normalize_inventory(row.get("Inventarnummer", "")),
                "title": clean(row.get("Objektname")),
                "description": clean(row.get("Beschreibung")),
                "tags": clean(row.get("Schlagwort")),
                "historical_md_object_id": clean(row.get("md:number")),
                "scan_signature": scan_signature(row.get("Scansignatur", "")),
                "bag_label": clean(row.get("Taschenbeschriftung")),
                "places": clean(row.get("Ort")),
                "date_text": clean(row.get("Zeit")),
                "material": clean(row.get("Material / Technik")),
                "dimensions": clean(row.get("Maße")),
            }
        )
    ids = [row["historical_md_object_id"] for row in rows]
    if not ids or len(ids) != len(set(ids)):
        raise DiscoveryError("local catalog has missing or duplicate MD IDs")
    return rows


def xlsx_column(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    number = 0
    for character in letters.upper():
        number = number * 26 + ord(character) - 64
    return number - 1


def load_original_title_map(
    path: Path,
    md_rows: list[dict[str, str]],
    local_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    inventory_to_ids: dict[str, list[str]] = defaultdict(list)
    current_inventory = {row["md_object_id"]: row["inventory"] for row in md_rows}
    for row in md_rows:
        inventory_to_ids[row["inventory"].casefold()].append(row["md_object_id"])
    for row in local_rows:
        inventory_to_ids[row["inventory"].casefold()].append(
            row["historical_md_object_id"]
        )

    with zipfile.ZipFile(path) as archive:
        shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(text.text or "" for text in item.iter(namespace + "t"))
            for item in shared_root.findall(namespace + "si")
        ]
        sheet = ElementTree.fromstring(
            archive.read("xl/worksheets/sheet1.xml")
        )

    result: list[dict[str, str]] = []
    for row in sheet.iter(namespace + "row"):
        values: dict[int, str] = {}
        for cell in row.findall(namespace + "c"):
            value_node = cell.find(namespace + "v")
            value = "" if value_node is None else (value_node.text or "")
            if cell.attrib.get("t") == "s" and value:
                value = shared[int(value)]
            values[xlsx_column(cell.attrib.get("r", "A"))] = clean(value)
        source_filename = values.get(2, "")
        if not source_filename or source_filename.casefold() == "dateiname":
            continue
        explicit_id = values.get(3, "")
        if explicit_id.isdigit():
            object_id = explicit_id
            basis = "explicit_md_object_id"
        else:
            inventory = normalize_inventory(Path(source_filename).stem).casefold()
            ids = sorted(set(inventory_to_ids.get(inventory, [])), key=int)
            if len(ids) > 1:
                raise DiscoveryError(
                    "original-title workbook filename did not resolve uniquely: "
                    + source_filename
                )
            object_id = ids[0] if ids else ""
            basis = (
                "exact_inventory_filename"
                if object_id
                else "local_only_unmatched_inventory"
            )
        if object_id and object_id not in {
            row["md_object_id"] for row in md_rows
        } | {
            row["historical_md_object_id"] for row in local_rows
        }:
            raise DiscoveryError(
                f"original-title workbook references unknown object ID: {object_id}"
            )
        result.append(
            {
                "sequence": values.get(0, ""),
                "source_filename": source_filename,
                "mapped_md_object_id": object_id,
                "mapping_basis": basis,
                "current_inventory": current_inventory.get(object_id, ""),
                "title": values.get(4, ""),
                "description_and_bag": values.get(5, ""),
                "tags_and_batch": values.get(6, ""),
                "scan_and_dimensions": values.get(7, ""),
                "places": values.get(8, ""),
                "date_text": values.get(9, ""),
            }
        )
    return result


def path_match_reasons(relative_path: str) -> list[str]:
    return [
        name for name, pattern in NAME_PATTERNS if pattern.search(relative_path)
    ]


def classify_path(relative_path: str) -> tuple[str, str, str]:
    for root in SHARED_ROOTS:
        if relative_path.startswith(root) and path_match_reasons(relative_path):
            return "shared_source_separate_review", "mixed_project_source", root.rstrip("/")
    for root in EXCLUSIVE_ROOTS:
        if relative_path.startswith(root):
            return "exclusive_candidate", "dedicated_kurt_schwarz_root", root.rstrip("/")
    reasons = path_match_reasons(relative_path)
    if reasons:
        return "unusual_path_candidate", "|".join(reasons), ""
    return "", "", ""


def scope_rows(inventory: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    candidates: list[dict[str, str]] = []
    outside: list[dict[str, str]] = []
    with inventory.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            state, basis, root = classify_path(row["relative_path"])
            if not state:
                continue
            result = {
                **{field: row.get(field, "") for field in SCOPE_FIELDS},
                "scope_state": state,
                "scope_basis": basis,
                "scope_root": root,
                "deletion_authorized": "false",
            }
            if state == "exclusive_candidate":
                candidates.append(result)
            else:
                outside.append(result)
    return candidates, outside


def build_drift(
    md_rows: list[dict[str, str]], local_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    md = {row["md_object_id"]: row for row in md_rows}
    local = {row["historical_md_object_id"]: row for row in local_rows}
    drift: list[dict[str, str]] = []
    for object_id in sorted(md.keys() - local.keys(), key=int):
        row = md[object_id]
        drift.append(
            {
                "state": "current_md_only",
                "md_object_id": object_id,
                "current_inventory": row["inventory"],
                "local_inventory": "",
                "current_title": row["title"],
                "local_title": "",
                "basis": "present_in_current_collection_783_not_latest_usb_export",
            }
        )
    for object_id in sorted(local.keys() - md.keys(), key=int):
        row = local[object_id]
        drift.append(
            {
                "state": "historical_local_only",
                "md_object_id": object_id,
                "current_inventory": "",
                "local_inventory": row["inventory"],
                "current_title": "",
                "local_title": row["title"],
                "basis": "latest_usb_export_record_absent_from_current_collection_783",
            }
        )
    for object_id in sorted(md.keys() & local.keys(), key=int):
        current = md[object_id]
        historical = local[object_id]
        changes = []
        if current["inventory"] != historical["inventory"]:
            changes.append("inventory")
        if current["title"] != historical["title"]:
            changes.append("title")
        if changes:
            drift.append(
                {
                    "state": "metadata_changed",
                    "md_object_id": object_id,
                    "current_inventory": current["inventory"],
                    "local_inventory": historical["inventory"],
                    "current_title": current["title"],
                    "local_title": historical["title"],
                    "basis": "current_md_differs_from_latest_usb_export:" + "|".join(changes),
                }
            )
    return drift


def filename_index(
    candidates: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        if row["extension"].casefold() not in IMAGE_EXTENSIONS:
            continue
        result[Path(row["relative_path"]).stem.casefold()].append(row)
    return result


def variant_matches(
    index: dict[str, list[dict[str, str]]],
    base_values: Iterable[str],
) -> list[dict[str, str]]:
    suffix = re.compile(r"(?i)^(?:_or|_b(?:[-_]?\d+)*(?:_b)*|b(?:-\d+)?)$")
    matches: list[dict[str, str]] = []
    for value in base_values:
        base = value.casefold()
        if not base:
            continue
        for stem, candidates in index.items():
            if stem.startswith(base) and suffix.fullmatch(stem[len(base):]):
                matches.extend(candidates)
    return matches


def filter_variant_ownership(
    candidates: Iterable[dict[str, str]],
    object_id: str,
    ownership: dict[str, set[str]],
) -> list[dict[str, str]]:
    result = []
    for candidate in candidates:
        stem = Path(candidate["relative_path"]).stem.casefold()
        owners = ownership.get(stem, set())
        if owners and object_id not in owners:
            continue
        result.append(candidate)
    return result


def build_matches(
    md_rows: list[dict[str, str]],
    local_rows: list[dict[str, str]],
    original_title_rows: list[dict[str, str]],
    candidates: list[dict[str, str]],
) -> list[dict[str, str]]:
    index = filename_index(candidates)
    historical = {row["historical_md_object_id"]: row for row in local_rows}
    original_titles: dict[str, list[dict[str, str]]] = defaultdict(list)
    original_stem_ownership: dict[str, set[str]] = defaultdict(set)
    for row in original_title_rows:
        if row["mapped_md_object_id"]:
            original_titles[row["mapped_md_object_id"]].append(row)
            original_stem_ownership[
                Path(row["source_filename"]).stem.casefold()
            ].add(row["mapped_md_object_id"])
    rows: list[dict[str, str]] = []
    all_ids = sorted(
        {row["md_object_id"] for row in md_rows}
        | {row["historical_md_object_id"] for row in local_rows},
        key=int,
    )
    current = {row["md_object_id"]: row for row in md_rows}
    for object_id in all_ids:
        md = current.get(object_id, {})
        local = historical.get(object_id, {})
        inventory = md.get("inventory", "") or local.get("inventory", "")
        old_inventory = local.get("inventory", "")
        scan = local.get("scan_signature", "")
        original_filenames = [
            row["source_filename"] for row in original_titles.get(object_id, [])
        ]
        groups: dict[str, list[dict[str, str]]] = {}
        for label, value in (
            ("current", inventory),
            ("historical", old_inventory),
            ("scan", scan),
        ):
            groups[label] = index.get(value.casefold(), []) if value else []
        groups["original"] = [
            candidate
            for filename in original_filenames
            for candidate in index.get(
                normalize_inventory(Path(filename).stem).casefold(), []
            )
        ]
        groups["variants"] = filter_variant_ownership(
            variant_matches(
                index,
                [
                    inventory,
                    old_inventory,
                    scan,
                    *[
                        normalize_inventory(Path(filename).stem)
                        for filename in original_filenames
                    ],
                ],
            ),
            object_id,
            original_stem_ownership,
        )
        unique = {
            row["source_path"]: row
            for values in groups.values()
            for row in values
        }
        tiffs = [
            row for row in unique.values()
            if row["extension"].casefold() in {".tif", ".tiff"}
        ]
        jpegs = [
            row for row in unique.values()
            if row["extension"].casefold() in {".jpg", ".jpeg"}
        ]
        state = "matched_tiff" if tiffs else "matched_jpeg" if jpegs else "unresolved"
        rows.append(
            {
                "record_state": (
                    "current_md"
                    if object_id in current
                    else "historical_local_only"
                ),
                "md_object_id": object_id,
                "inventory": inventory,
                "historical_inventory": old_inventory,
                "scan_signature": scan,
                "original_title_filename": "; ".join(original_filenames),
                "current_inventory_matches": str(len(groups["current"])),
                "historical_inventory_matches": str(len(groups["historical"])),
                "scan_signature_matches": str(len(groups["scan"])),
                "original_title_filename_matches": str(len(groups["original"])),
                "variant_matches": str(len(groups["variants"])),
                "tiff_matches": str(len(tiffs)),
                "jpeg_matches": str(len(jpegs)),
                "candidate_paths": "; ".join(sorted(unique)),
                "match_state": state,
            }
        )
    return rows


def execute(args: argparse.Namespace) -> dict[str, Any]:
    verify_source()
    run = ensure_work_path(args.output)
    if run.exists():
        raise DiscoveryError(f"run already exists: {run}")
    run.mkdir(parents=True)
    md_objects = load_md_objects(args.md_run.resolve())
    md_rows = [flatten_md_object(row) for row in md_objects]
    local_rows = load_local_catalog(args.local_catalog.resolve())
    original_title_rows = load_original_title_map(
        args.original_title_map.resolve(), md_rows, local_rows
    )
    candidates, outside = scope_rows(args.inventory.resolve())
    drift = build_drift(md_rows, local_rows)
    matches = build_matches(md_rows, local_rows, original_title_rows, candidates)

    write_csv(run / "md-objects.csv", OBJECT_FIELDS, md_rows)
    write_csv(run / "local-catalog.csv", LOCAL_FIELDS, local_rows)
    write_csv(
        run / "original-title-map.csv",
        ORIGINAL_TITLE_FIELDS,
        original_title_rows,
    )
    write_csv(run / "usb-exclusive-candidates.csv", SCOPE_FIELDS, candidates)
    write_csv(run / "usb-shared-and-unusual-paths.csv", SCOPE_FIELDS, outside)
    write_csv(run / "catalog-drift.csv", DRIFT_FIELDS, drift)
    write_csv(run / "filename-reconciliation.csv", MATCH_FIELDS, matches)

    root_counts = Counter(row["scope_root"] for row in candidates)
    state_counts = Counter(row["scope_state"] for row in outside)
    match_counts = Counter(row["match_state"] for row in matches)
    subdivision_counts = Counter()
    for row in md_rows:
        for subdivision in row["subdivisions"].split("; "):
            if subdivision:
                subdivision_counts[subdivision] += 1
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "source_mount": str(SOURCE_MOUNT),
        "source_uuid": SOURCE_UUID,
        "source_read_only": True,
        "reused_inventory": str(args.inventory.resolve()),
        "museum_digital_run": str(args.md_run.resolve()),
        "local_catalog": str(args.local_catalog.resolve()),
        "original_title_map": str(args.original_title_map.resolve()),
        "current_md_objects": len(md_rows),
        "local_catalog_records": len(local_rows),
        "original_title_records": len(original_title_rows),
        "original_title_local_only_records": sum(
            not row["mapped_md_object_id"] for row in original_title_rows
        ),
        "usb_exclusive_candidate_rows": len(candidates),
        "usb_exclusive_candidate_bytes": sum(
            int(row["size_bytes"]) for row in candidates
        ),
        "usb_shared_and_unusual_rows": len(outside),
        "root_counts": dict(root_counts),
        "outside_state_counts": dict(state_counts),
        "catalog_drift_counts": dict(Counter(row["state"] for row in drift)),
        "filename_match_counts": dict(match_counts),
        "subdivision_counts": dict(subdivision_counts.most_common()),
        "deletion_authorized_rows": 0,
    }
    write_json(run / "summary.json", summary)
    write_json(
        run / "validation.json",
        {
            "result": "ok",
            "source_read_only": True,
            "current_md_objects": len(md_rows),
            "unique_md_object_ids": len({row["md_object_id"] for row in md_rows}),
            "local_catalog_records": len(local_rows),
            "original_title_records": len(original_title_rows),
            "unique_original_title_md_ids": len(
                {
                    row["mapped_md_object_id"]
                    for row in original_title_rows
                    if row["mapped_md_object_id"]
                }
            ),
            "unique_local_md_ids": len(
                {row["historical_md_object_id"] for row in local_rows}
            ),
            "duplicate_exclusive_paths": len(candidates)
            - len({row["source_path"] for row in candidates}),
            "deletion_authorized_rows": 0,
        },
    )
    return {"result": "ok", "run": str(run), **summary}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    result.add_argument("--md-run", type=Path, default=DEFAULT_MD_RUN)
    result.add_argument("--local-catalog", type=Path, default=DEFAULT_LOCAL_CATALOG)
    result.add_argument(
        "--original-title-map",
        type=Path,
        default=DEFAULT_ORIGINAL_TITLE_MAP,
    )
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    try:
        print(json.dumps(execute(parser().parse_args()), ensure_ascii=False, indent=2))
        return 0
    except (DiscoveryError, OSError, csv.Error, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
