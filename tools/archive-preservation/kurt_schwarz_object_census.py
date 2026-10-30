#!/usr/bin/env python3
"""Build a bounded visual-review census for Kurt Schwarz source images.

The census is diagnostic review evidence.  It reads the published source
disposition and spreadsheet indexes, writes only below iss-archive-work, and
never authorizes source deletion or accession.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from kurt_schwarz_shared_workbooks import read_xlsx


csv.field_size_limit(sys.maxsize)


FORMAT = "iss-kurt-schwarz-object-census-v1"
RECONCILED_FORMAT = "iss-kurt-schwarz-object-census-v2"
SOURCE_PREFIX = "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
AVANTMEDIA_PATH_FRAGMENT = "/SCANS/01_Avantmedia_Dias/"
MASTER_EXTENSIONS = {".tif", ".tiff", ".psd"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
HIGHRES_LONG_EDGE = 2000
HIGHRES_PIXELS = 4_000_000
QUEUE_CLASSES = (
    "master_with_metadata",
    "master_without_metadata",
    "hires_jpeg_without_master",
)
FIELDS = (
    "candidate_id",
    "queue_class",
    "content_sha256",
    "size_bytes",
    "filename",
    "extension",
    "width_px",
    "height_px",
    "final_path",
    "primary_source_path",
    "source_paths_json",
    "metadata_matches_json",
    "metadata_match_count",
    "metadata_summary",
    "review_basis",
    "deletion_authorized",
)
RECONCILED_FIELDS = FIELDS + (
    "target_record_key",
    "target_inventory",
    "target_media_candidate_id",
    "related_candidate_id",
    "selected_metadata_json",
    "selected_metadata_basis",
    "metadata_enrichment_json",
    "visual_match_json",
    "auto_decision_reason",
)
RECONCILED_QUEUE_CLASSES = (
    "surrogate_master_review",
    "new_local_review",
    "existing_object_reconciled",
    "new_local_companion_reconciled",
)
SELECTED_METADATA_FIELDS = (
    "inventory",
    "title",
    "description",
    "tags",
    "date_text",
    "places",
    "material",
    "dimensions",
    "scan_signature",
    "bag_label",
)
CANONICAL_ENRICHMENT_FIELDS = {
    "inventory", "title", "description", "tags", "date_text", "places",
    "material", "dimensions",
}


class CensusError(RuntimeError):
    """Raised when census evidence violates the read-only review contract."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def normalized_stem(value: str) -> str:
    name = Path(value.strip()).name
    stem = Path(name).stem.casefold()
    return re.sub(r"[^a-z0-9]+", "", stem)


def candidate_id(content_sha256: str) -> str:
    return "KSC-" + hashlib.sha256(content_sha256.encode()).hexdigest()[:20]


def jpeg_dimensions(path: Path) -> tuple[int, int]:
    """Read JPEG SOF dimensions without decoding the image."""

    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise CensusError(f"not a JPEG stream: {path}")
        while True:
            marker_start = handle.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker:
                break
            marker_value = marker[0]
            if marker_value in {0xD8, 0xD9} or 0xD0 <= marker_value <= 0xD7:
                continue
            length_bytes = handle.read(2)
            if len(length_bytes) != 2:
                break
            length = int.from_bytes(length_bytes, "big")
            if length < 2:
                break
            if marker_value in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                payload = handle.read(5)
                if len(payload) != 5:
                    break
                return (
                    int.from_bytes(payload[3:5], "big"),
                    int.from_bytes(payload[1:3], "big"),
                )
            handle.seek(length - 2, os.SEEK_CUR)
    raise CensusError(f"JPEG dimensions unavailable: {path}")


def split_candidate_paths(value: str) -> Iterable[str]:
    for item in value.split(";"):
        item = item.strip()
        if item:
            yield item


def compact_batch_metadata(row: dict[str, str]) -> dict[str, Any]:
    return {
        "evidence_type": "batch_catalog_row",
        "source_document": row["source_path"],
        "sheet_name": "",
        "row_number": row["source_row"],
        "inventory": row["source_inventory"],
        "mapped_md_object_ids": row["mapped_md_object_id"],
        "mapped_record_keys": (
            f"md:{row['mapped_md_object_id']}"
            if row["mapped_md_object_id"]
            else ""
        ),
        "mapping_state": row["mapping_basis"],
        "title": row["title"],
        "description": row["description"],
        "tags": row["tags"],
        "date_text": row["date_text"],
        "places": row["places"],
        "material": row["material"],
        "dimensions": row["dimensions"],
        "scan_signature": row["scan_signature"],
        "bag_label": row["bag_label"],
        "match_basis": "",
    }


def compact_shared_metadata(row: dict[str, str]) -> dict[str, Any]:
    try:
        record = json.loads(row["record_json"] or "{}")
    except json.JSONDecodeError:
        record = {"raw_record": row["record_json"]}
    tags = "; ".join(
        str(record[key]).strip()
        for key in sorted(record)
        if key.casefold().startswith("tag") and str(record[key]).strip()
    )
    return {
        "evidence_type": "shared_workbook_row",
        "source_document": row["source_document"],
        "source_member": row["source_member"],
        "sheet_name": row["sheet_name"],
        "row_number": row["row_number"],
        "inventory": row["inventory"],
        "mapped_md_object_ids": row["mapped_md_object_ids"],
        "mapped_record_keys": row["mapped_record_keys"],
        "mapping_state": row["mapping_state"],
        "title": str(record.get("object_title", "")),
        "description": str(record.get("object_description", "")),
        "tags": tags,
        "date_text": str(record.get("object_date", "")),
        "places": str(record.get("object_place", "")),
        "material": str(record.get("object_material_technique", "")),
        "dimensions": str(record.get("object_dimensions", "")),
        "scan_signature": row["scan_signatures"],
        "bag_label": str(record.get("remarks_long", "")),
        "remarks_short": str(record.get("remarks_short", "")),
        "match_basis": "",
    }


def metadata_indexes(
    batch_records: Path, shared_records: Path
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    exact_paths: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exact_keys: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(batch_records):
        evidence = compact_batch_metadata(row)
        for path in split_candidate_paths(row["image_candidate_paths"]):
            exact_paths[path].append(evidence)
        for value in (
            row["source_inventory"],
            row["normalized_inventory"],
            row["image_name"],
            row["scan_signature"],
        ):
            key = normalized_stem(value)
            if len(key) >= 7:
                exact_keys[key].append(evidence)
    for row in read_csv(shared_records):
        if row["deletion_authorized"].casefold() != "false":
            raise CensusError(
                "shared workbook evidence unexpectedly authorizes deletion"
            )
        evidence = compact_shared_metadata(row)
        values = [row["inventory"], *row["scan_signatures"].split(";")]
        remarks = evidence.get("remarks_short", "")
        values.extend(
            re.findall(r"[^/;|]+\.(?:tif|tiff|psd|jpe?g)", remarks, re.I)
        )
        for value in values:
            key = normalized_stem(value)
            if len(key) >= 7:
                exact_keys[key].append(evidence)
    return exact_paths, exact_keys


def candidate_keys_in_value(
    value: str, target_keys: set[str]
) -> set[str]:
    """Return exact filename-like identifiers mentioned in a spreadsheet cell."""

    found = set()
    value = str(value).strip()
    if not value:
        return found
    # Check filename-looking tokens, including filenames containing spaces.
    for match in re.finditer(
        r"(?i)[a-z0-9][a-z0-9_.() -]{1,120}\.(?:tif|tiff|psd|jpe?g)",
        value,
    ):
        words = match.group(0).strip().split()
        for start in range(len(words)):
            key = normalized_stem(" ".join(words[start:]))
            if key in target_keys:
                found.add(key)
    # Many finding aids store a scan signature without an extension.
    if len(value) <= 140:
        for part in re.split(r"[;,|/]", value):
            key = normalized_stem(part)
            if key in target_keys:
                found.add(key)
    return found


def row_metadata(
    *,
    source: Path,
    sheet: str,
    row_number: int,
    record: dict[str, str],
    match_basis: str,
) -> dict[str, Any]:
    def first(*names: str) -> str:
        wanted = {name.casefold() for name in names}
        for key, value in record.items():
            if key.strip().casefold() in wanted and str(value).strip():
                return str(value).strip()
        return ""

    tags = "; ".join(
        str(value).strip()
        for key, value in record.items()
        if key.strip().casefold().startswith(("tag", "schlagwort"))
        and str(value).strip()
    )
    compact_record = {
        str(key)[:160]: str(value)[:2000]
        for key, value in record.items()
        if str(value).strip()
    }
    return {
        "evidence_type": "usb_spreadsheet_row",
        "source_document": str(source),
        "sheet_name": sheet,
        "row_number": str(row_number),
        "inventory": first(
            "inventory_number", "inventarnummer", "signatur", "inventory"
        ),
        "mapped_md_object_ids": first(
            "object_id", "md_object_id", "museum-digital id"
        ),
        "mapped_record_keys": "",
        "mapping_state": "direct_file_or_scan_reference",
        "title": first("object_title", "objekttitel", "title", "titel"),
        "description": first(
            "object_description", "objektbeschreibung", "description", "beschreibung"
        ),
        "tags": tags,
        "date_text": first(
            "taken_when1", "object_date", "datierung", "date", "datum"
        ),
        "places": first(
            "taken_where1", "object_place", "ort", "place"
        ),
        "material": first(
            "object_material_technique", "material", "material/technik"
        ),
        "dimensions": first("object_dimensions", "maße", "dimensions"),
        "scan_signature": first(
            "remarks_short", "scan_signature", "scan-signatur", "bildname"
        ),
        "bag_label": first("remarks_long", "taschenbeschriftung", "bemerkung"),
        "match_basis": match_basis,
        "raw_record": compact_record,
    }


def read_delimited_rows(
    text: str,
) -> list[tuple[str, int, dict[str, str]]]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return []
    headers = []
    used: Counter[str] = Counter()
    for index, value in enumerate(rows[0]):
        base = value.strip() or f"column_{index + 1}"
        used[base] += 1
        headers.append(base if used[base] == 1 else f"{base}_{used[base]}")
    result = []
    for row_number, values in enumerate(rows[1:], 2):
        record = {
            headers[index]: value
            for index, value in enumerate(values[: len(headers)])
            if value.strip()
        }
        if record:
            result.append(("", row_number, record))
    return result


def usb_spreadsheet_mentions(
    spreadsheet_root: Path,
    target_keys: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Index direct candidate filename/scan references across USB spreadsheets."""

    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    files = sorted(
        (
            path
            for path in spreadsheet_root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in {".csv", ".xls", ".xlsx"}
        ),
        key=lambda path: str(path).casefold(),
    )
    failures = []
    parsed_rows = 0
    for path in files:
        suffix = path.suffix.casefold()
        try:
            if suffix == ".xlsx":
                rows = read_xlsx(path)
            elif suffix == ".xls":
                completed = subprocess.run(
                    ["xls2csv", str(path)],
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
                if completed.returncode:
                    raise CensusError(
                        completed.stderr.decode("utf-8", errors="replace")[:500]
                    )
                rows = read_delimited_rows(
                    completed.stdout.decode("utf-8", errors="replace")
                )
            else:
                payload = path.read_bytes()
                text = ""
                for encoding in ("utf-8-sig", "cp1252", "utf-16"):
                    try:
                        text = payload.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                if not text:
                    raise CensusError("unsupported CSV encoding")
                rows = read_delimited_rows(text)
        except Exception as error:  # retain a bounded audit of damaged files
            failures.append({"path": str(path), "error": str(error)[:500]})
            continue
        parsed_rows += len(rows)
        for sheet, row_number, record in rows:
            keys = set()
            for value in record.values():
                keys.update(candidate_keys_in_value(str(value), target_keys))
            for key in keys:
                matches[key].append(
                    row_metadata(
                        source=path,
                        sheet=sheet,
                        row_number=row_number,
                        record=record,
                        match_basis="direct_usb_spreadsheet_filename_or_scan_reference",
                    )
                )
    return matches, {
        "spreadsheet_files_considered": len(files),
        "spreadsheet_files_failed": len(failures),
        "spreadsheet_rows_parsed": parsed_rows,
        "spreadsheet_failures": failures,
    }


def dedupe_metadata(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        identity = (
            item.get("source_document", ""),
            item.get("sheet_name", ""),
            item.get("row_number", ""),
            item.get("inventory", ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def find_metadata(
    occurrences: list[dict[str, str]],
    exact_paths: dict[str, list[dict[str, Any]]],
    exact_keys: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    matches = []
    for row in occurrences:
        for raw in exact_paths.get(row["source_path"], []):
            item = dict(raw)
            item["match_basis"] = "exact_image_candidate_path"
            matches.append(item)
    if matches:
        return dedupe_metadata(matches)
    keys = {normalized_stem(row["source_path"]) for row in occurrences}
    for key in keys:
        possible = dedupe_metadata(exact_keys.get(key, []))
        # A filename/identifier is confirmation only when it resolves to one
        # logical inventory, even if that row occurs in several exports.
        inventories = {
            str(item.get("inventory", "")).strip().casefold()
            for item in possible
            if str(item.get("inventory", "")).strip()
        }
        if possible and len(inventories) == 1:
            for raw in possible:
                item = dict(raw)
                item["match_basis"] = "unique_inventory_or_scan_identifier"
                matches.append(item)
    return dedupe_metadata(matches)


def metadata_summary(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "No confirmed row match in the validated spreadsheet indexes."
    inventories = sorted(
        {item.get("inventory", "") for item in matches if item.get("inventory")}
    )
    titles = [item.get("title", "") for item in matches if item.get("title")]
    result = "; ".join(inventories) or "Spreadsheet row matched"
    if titles:
        result += " — " + str(titles[0])
    return result


def clean_inventory(value: str) -> str:
    value = str(value or "").strip()
    if Path(value).suffix.casefold() in MASTER_EXTENSIONS | JPEG_EXTENSIONS:
        value = Path(value).stem
    return value.strip(" _")


def choose_selected_metadata(
    matches: list[dict[str, Any]],
    *,
    filename: str,
    existing_inventories: set[str],
) -> tuple[dict[str, str], str]:
    """Choose one conservative metadata interpretation for a local object.

    Spreadsheet rows pointing at an existing Kurt inventory are excluded here:
    a visually different image must not inherit metadata from a stale lookup
    or ``found`` column.  Raw matches remain preserved in the census evidence.
    """

    filename_inventory = clean_inventory(filename)
    filename_key = normalized_stem(filename_inventory)
    by_inventory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for match in matches:
        inventory = clean_inventory(str(match.get("inventory", "")))
        if not inventory:
            continue
        key = normalized_stem(inventory)
        if not key or key in existing_inventories:
            continue
        by_inventory[key].append(match)
        labels.setdefault(key, inventory)

    selected_key = ""
    basis = ""
    if filename_key in by_inventory:
        selected_key = filename_key
        basis = "filename_and_spreadsheet_inventory_agree"
    elif by_inventory:
        ranked = sorted(
            by_inventory.items(), key=lambda item: (-len(item[1]), item[0])
        )
        best_count = len(ranked[0][1])
        next_count = len(ranked[1][1]) if len(ranked) > 1 else 0
        if best_count >= 2 and best_count > next_count:
            selected_key = ranked[0][0]
            basis = "dominant_non_md_spreadsheet_inventory"

    if not selected_key:
        if re.fullmatch(r"(?i)KS[-_ ].+", filename_inventory):
            return {"inventory": filename_inventory}, "filename_only"
        return {}, "no_reliable_metadata_selected"

    evidence = by_inventory[selected_key]
    selected: dict[str, str] = {"inventory": labels[selected_key]}
    for field in SELECTED_METADATA_FIELDS[1:]:
        values = Counter(
            str(item.get(field, "")).strip()
            for item in evidence
            if str(item.get(field, "")).strip()
        )
        if values:
            selected[field] = sorted(
                values.items(), key=lambda item: (-item[1], item[0].casefold())
            )[0][0]
    return selected, basis


def metadata_for_inventory(
    matches: list[dict[str, Any]], target_inventory: str
) -> dict[str, str]:
    """Select only untied values from rows naming one visually proven object."""

    target_key = normalized_stem(clean_inventory(target_inventory))
    evidence = [
        item
        for item in matches
        if normalized_stem(clean_inventory(str(item.get("inventory", ""))))
        == target_key
    ]
    selected: dict[str, str] = {}
    for field in SELECTED_METADATA_FIELDS[1:]:
        values = Counter(
            str(item.get(field, "")).strip()
            for item in evidence
            if str(item.get(field, "")).strip()
        )
        if not values:
            continue
        ranked = sorted(
            values.items(), key=lambda item: (-item[1], item[0].casefold())
        )
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            selected[field] = ranked[0][0]
    return selected


def avantmedia_candidate_identity(
    row: dict[str, Any],
) -> tuple[int, str] | None:
    """Return the numbered Avantmedia slide and representation role.

    The source layout is the provenance here: ``TIFF/Dia 00001.tif`` is the
    preservation scan and ``Nutzungsderivate/*_00001.jpg`` is its edited access
    derivative.  Descriptive JPEG prefixes are not independent object IDs.
    """

    try:
        paths = json.loads(str(row.get("source_paths_json", "[]")))
    except json.JSONDecodeError:
        paths = []
    if not isinstance(paths, list):
        paths = []
    primary = str(row.get("primary_source_path", ""))
    if primary and primary not in paths:
        paths.append(primary)
    identities: set[tuple[int, str]] = set()
    for raw_path in paths:
        path = str(raw_path).replace("\\", "/")
        if AVANTMEDIA_PATH_FRAGMENT not in path:
            continue
        name = Path(path).name
        master = re.fullmatch(r"(?i)Dia (\d{5})\.tiff?", name)
        derivative = re.fullmatch(r"(?i).+_(\d{5})\.jpe?g", name)
        if master and "/TIFF/" in path:
            identities.add((int(master.group(1)), "tiff"))
        elif derivative and "/Nutzungsderivate/" in path:
            identities.add((int(derivative.group(1)), "derivative"))
    return next(iter(identities)) if len(identities) == 1 else None


def apply_avantmedia_tiff_primary_policy(
    reconciled: list[dict[str, Any]],
    *,
    record_context: dict[str, dict[str, Any]],
    existing_inventories: set[str],
) -> dict[str, Any]:
    """Collapse numbered JPEG derivatives beneath their TIFF source scan.

    A derivative may remain an independent review candidate only when its
    numbered TIFF is absent.  Existing-object evidence found through a JPEG is
    transferred to the TIFF; the JPEG byte state remains preserved as an
    automatically reconciled companion.
    """

    groups: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"tiff": [], "derivative": []}
    )
    for row in reconciled:
        identity = avantmedia_candidate_identity(row)
        if identity is not None:
            sequence, role = identity
            groups[sequence][role].append(row)

    summary: Counter[str] = Counter()
    duplicate_derivative_sequences = 0
    missing_tiff_sequences = []
    ambiguous_target_sequences = []
    for sequence, representations in sorted(groups.items()):
        tiffs = representations["tiff"]
        derivatives = representations["derivative"]
        summary["sequences"] += 1
        summary["tiff_candidates"] += len(tiffs)
        summary["derivative_candidates"] += len(derivatives)
        if len(derivatives) > 1:
            duplicate_derivative_sequences += 1
        if not derivatives:
            continue
        if len(tiffs) != 1:
            if not tiffs:
                missing_tiff_sequences.append(sequence)
            continue

        primary = tiffs[0]
        all_rows = [primary, *derivatives]
        raw_matches = dedupe_metadata(
            match
            for item in all_rows
            for match in json.loads(item["metadata_matches_json"])
        )
        primary["metadata_matches_json"] = json.dumps(
            raw_matches, ensure_ascii=False, sort_keys=True
        )
        primary["metadata_match_count"] = len(raw_matches)

        targets = {
            item["target_record_key"]
            for item in all_rows
            if item["queue_class"] in {
                "existing_object_reconciled", "surrogate_master_review"
            }
            and item["target_record_key"]
        }
        derivative_audits = [
            {
                "candidate_id": item["candidate_id"],
                "filename": item["filename"],
                "target_record_key": item["target_record_key"],
                "target_inventory": item["target_inventory"],
                "visual_match": json.loads(item["visual_match_json"]),
            }
            for item in derivatives
            if item["target_record_key"]
        ]
        primary_audit = json.loads(primary["visual_match_json"])
        primary_audit["avantmedia_numbered_derivatives"] = {
            "sequence": f"{sequence:05d}",
            "candidate_ids": [item["candidate_id"] for item in derivatives],
            "existing_object_evidence": derivative_audits,
        }
        primary["visual_match_json"] = json.dumps(
            primary_audit, ensure_ascii=False, sort_keys=True
        )

        if len(targets) == 1:
            target_key = next(iter(targets))
            target = record_context[target_key]
            target_row = next(
                item for item in all_rows
                if item["target_record_key"] == target_key
            )
            selected_metadata = {
                key: str(value)
                for key, value in target["effective_metadata"].items()
                if str(value).strip()
            }
            selected_metadata["inventory"] = target["inventory"]
            derivative_filenames = sorted(
                {item["filename"] for item in derivatives}
            )
            existing_bag_label = selected_metadata.get("bag_label", "")
            if len(derivative_filenames) == 1 and (
                not existing_bag_label
                or existing_bag_label.casefold().startswith(
                    "dia aus multivision, eigener dateiname:"
                )
            ):
                selected_metadata["bag_label"] = (
                    "Dia aus Multivision, eigener Dateiname: "
                    f"{derivative_filenames[0]}"
                )
            local_metadata = metadata_for_inventory(
                raw_matches, target["inventory"]
            )
            metadata_enrichment = {
                key: value
                for key, value in local_metadata.items()
                if key in CANONICAL_ENRICHMENT_FIELDS
                and not str(target["effective_metadata"].get(key, "")).strip()
            }
            selected_metadata.update(metadata_enrichment)
            primary.update(
                {
                    "queue_class": (
                        "surrogate_master_review"
                        if target["no_tiff_master"]
                        else "existing_object_reconciled"
                    ),
                    "target_record_key": target_key,
                    "target_inventory": target["inventory"],
                    "target_media_candidate_id": target_row[
                        "target_media_candidate_id"
                    ],
                    "selected_metadata_json": json.dumps(
                        selected_metadata, ensure_ascii=False, sort_keys=True
                    ),
                    "selected_metadata_basis": (
                        "existing_kurt_metadata_via_numbered_avantmedia_derivative"
                    ),
                    "metadata_enrichment_json": json.dumps(
                        metadata_enrichment, ensure_ascii=False, sort_keys=True
                    ),
                    "metadata_summary": (
                        selected_metadata.get("title")
                        or target["inventory"]
                    ),
                    "review_basis": (
                        "Numbered Avantmedia TIFF source scan for a JPEG "
                        "derivative uniquely reconciled to this Kurt object."
                    ),
                    "auto_decision_reason": (
                        "The numbered Avantmedia JPEG derivative uniquely "
                        "matches this existing Kurt object; preserve its TIFF "
                        "source scan as the object representation."
                    ),
                }
            )
            summary["existing_bindings_transferred_to_tiff"] += 1
        elif len(targets) > 1:
            ambiguous_target_sequences.append(sequence)
            primary.update(
                {
                    "queue_class": "new_local_review",
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "metadata_enrichment_json": "{}",
                    "selected_metadata_json": "{}",
                    "selected_metadata_basis": (
                        "conflicting_avantmedia_derivative_targets"
                    ),
                    "metadata_summary": "Conflicting existing-object evidence",
                    "review_basis": (
                        "Numbered derivatives point to more than one existing "
                        "Kurt object; review the TIFF once."
                    ),
                    "auto_decision_reason": "",
                }
            )
        elif raw_matches:
            selected, basis = choose_selected_metadata(
                raw_matches,
                filename=primary["filename"],
                existing_inventories=existing_inventories,
            )
            primary["selected_metadata_json"] = json.dumps(
                selected, ensure_ascii=False, sort_keys=True
            )
            primary["selected_metadata_basis"] = basis
            primary["metadata_summary"] = (
                selected.get("title")
                or selected.get("inventory")
                or "No reliable metadata selected"
            )

        for derivative in derivatives:
            derivative.update(
                {
                    "queue_class": "new_local_companion_reconciled",
                    "target_record_key": "",
                    "target_inventory": "",
                    "target_media_candidate_id": "",
                    "related_candidate_id": primary["candidate_id"],
                    "metadata_enrichment_json": "{}",
                    "review_basis": (
                        "Numbered access derivative of the bound Avantmedia "
                        "TIFF source scan."
                    ),
                    "auto_decision_reason": (
                        f"Avantmedia Nutzungsderivat sequence {sequence:05d}; "
                        "retain the JPEG byte state as a companion and review "
                        "only its TIFF source scan."
                    ),
                }
            )
            summary["derivatives_reconciled_as_companions"] += 1

    return {
        **dict(summary),
        "duplicate_derivative_sequences": duplicate_derivative_sequences,
        "missing_tiff_sequences": missing_tiff_sequences,
        "ambiguous_target_sequences": ambiguous_target_sequences,
        "derivative_surfaces_only_without_numbered_tiff": True,
    }


def visual_match(
    candidate_hashes: tuple[int, int],
    representatives: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return one unique bounded visual match and its audit summary."""

    from kurt_schwarz_visual_verify import hamming

    matches = []
    for representative in representatives:
        phash_distance = hamming(candidate_hashes[0], representative["phash"])
        dhash_distance = hamming(candidate_hashes[1], representative["dhash"])
        if phash_distance <= 8 and dhash_distance <= 12:
            matches.append(
                {
                    **representative,
                    "phash_distance": phash_distance,
                    "dhash_distance": dhash_distance,
                }
            )
    matches.sort(
        key=lambda item: (
            item["phash_distance"] + item["dhash_distance"],
            item["phash_distance"],
            item["dhash_distance"],
            item["record_key"],
            item.get("matched_media_candidate_id", item["media_candidate_id"]),
        )
    )
    matches_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in matches:
        matches_by_record[item["record_key"]].append(item)
    object_matches = [items[0] for items in matches_by_record.values()]
    object_matches.sort(
        key=lambda item: (
            item["phash_distance"] + item["dhash_distance"],
            item["phash_distance"],
            item["dhash_distance"],
            item["record_key"],
        )
    )
    audit = {
        "bounded_match_count": len(object_matches),
        "bounded_representation_match_count": len(matches),
        "matches": [
            {
                "record_key": item["record_key"],
                "inventory": item["inventory"],
                "target_media_candidate_id": item["media_candidate_id"],
                "matched_media_candidate_id": item.get(
                    "matched_media_candidate_id", item["media_candidate_id"]
                ),
                "matched_disposition": item.get("matched_disposition", ""),
                "phash_distance": item["phash_distance"],
                "dhash_distance": item["dhash_distance"],
            }
            for item in object_matches[:10]
        ],
    }
    return (object_matches[0] if len(object_matches) == 1 else None), audit


def reconcile_existing_census(
    *,
    unit_root: Path,
    review_root: Path,
    census_csv: Path,
    output_dir: Path,
    workers: int = 2,
) -> dict[str, Any]:
    """Reclassify the byte census around the already reconciled Kurt objects."""

    if workers not in {1, 2}:
        raise CensusError("preview workers must be one or two")
    from kurt_schwarz_reconciliation import KurtSchwarzReview
    from kurt_schwarz_visual_verify import image_hashes

    rows = read_csv(census_csv)
    if not rows or len(rows) != len({row["candidate_id"] for row in rows}):
        raise CensusError("existing census must contain unique candidates")
    review = KurtSchwarzReview(unit_root, review_root)
    imported = {row["candidate_id"] for row in review.object_census_candidates()}
    supplied = {row["candidate_id"] for row in rows}
    if imported != supplied:
        raise CensusError("existing census candidates drifted from the live projection")

    records = [row for row in review.records() if not row.get("alias_of")]
    records_by_key = {row["record_key"]: row for row in records}
    record_context: dict[str, dict[str, Any]] = {}
    for record in records:
        try:
            catalog_metadata = json.loads(record.get("catalog_json", "{}"))
        except json.JSONDecodeError:
            catalog_metadata = {}
        effective_metadata = dict(record["effective_metadata"])
        for supplemental in ("scan_signature", "bag_label"):
            value = str(catalog_metadata.get(supplemental, "")).strip()
            if value:
                effective_metadata[supplemental] = value
        record_context[record["record_key"]] = {
            "record_key": record["record_key"],
            "inventory": record["display_inventory"],
            "media_candidate_id": record["thumbnail_candidate_id"],
            "no_tiff_master": bool(record["no_tiff_master"]),
            "effective_metadata": effective_metadata,
        }

    with review.connect(read_only=True) as connection:
        decided_media = [
            dict(row)
            for row in connection.execute(
                """SELECT media.candidate_id, media.record_key,
                          media.content_role, decision.disposition
                   FROM media_candidates AS media
                   JOIN media_decisions AS decision
                     ON decision.decision_id = (
                       SELECT MAX(latest.decision_id)
                       FROM media_decisions AS latest
                       WHERE latest.candidate_id=media.candidate_id
                     )
                   WHERE decision.disposition IN ('selected_master', 'redundant')
                   ORDER BY CASE decision.disposition
                     WHEN 'selected_master' THEN 0 ELSE 1 END,
                     media.record_key, media.candidate_id"""
            )
            if row["record_key"] in records_by_key
        ]
    covered_records = {row["record_key"] for row in decided_media}
    for record in records:
        candidate = record.get("thumbnail_candidate_id", "")
        if candidate and record["record_key"] not in covered_records:
            decided_media.append(
                {
                    "candidate_id": candidate,
                    "record_key": record["record_key"],
                    "content_role": record.get("thumbnail_role", "fallback"),
                    "disposition": "fallback_representative",
                }
            )

    representative_errors = []
    representatives_raw = []

    def hash_representative(row: dict[str, str]) -> dict[str, Any]:
        phash, dhash = image_hashes(review.preview(row["candidate_id"]))
        return {
            **record_context[row["record_key"]],
            "matched_media_candidate_id": row["candidate_id"],
            "matched_disposition": row["disposition"],
            "matched_content_role": row["content_role"],
            "phash": phash,
            "dhash": dhash,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(hash_representative, row): row for row in decided_media}
        for index, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                representatives_raw.append(future.result())
            except Exception as error:
                representative_errors.append(
                    {
                        "record_key": row["record_key"],
                        "media_candidate_id": row["candidate_id"],
                        "error": f"{type(error).__name__}: {str(error)[:500]}",
                    }
                )
            if index % 500 == 0:
                print(
                    f"decided-representation previews={index}/{len(decided_media)} "
                    f"errors={len(representative_errors)}",
                    flush=True,
                )
    representatives_by_visual: dict[
        tuple[str, int, int], dict[str, Any]
    ] = {}
    for representative in representatives_raw:
        key = (
            representative["record_key"],
            representative["phash"],
            representative["dhash"],
        )
        representatives_by_visual.setdefault(key, representative)
    representatives = list(representatives_by_visual.values())
    existing_inventories = {
        normalized_stem(record["display_inventory"])
        for record in records
        if record["display_inventory"]
    }

    preview_errors = []
    candidate_hashes: dict[str, tuple[int, int]] = {}
    candidate_preview_sha256: dict[str, str] = {}

    def hash_candidate(
        row: dict[str, str]
    ) -> tuple[str, tuple[int, int], str]:
        preview = review.object_census_preview(row["candidate_id"])
        return row["candidate_id"], image_hashes(preview), sha256_file(preview)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(hash_candidate, row): row for row in rows}
        for index, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                candidate, hashes, preview_digest = future.result()
                candidate_hashes[candidate] = hashes
                candidate_preview_sha256[candidate] = preview_digest
            except Exception as error:  # retain the candidate for manual review
                preview_errors.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "filename": row["filename"],
                        "error": f"{type(error).__name__}: {str(error)[:500]}",
                    }
                )
            if index % 100 == 0:
                print(
                    f"object-census reconciliation previews={index}/{len(rows)} "
                    f"errors={len(preview_errors)}",
                    flush=True,
                )

    reconciled = []
    for row in rows:
        raw_matches = json.loads(row["metadata_matches_json"])
        hashes = candidate_hashes.get(row["candidate_id"])
        target, audit = (
            visual_match(hashes, representatives)
            if hashes is not None
            else (None, {"bounded_match_count": 0, "matches": [], "preview_error": True})
        )
        selected_metadata: dict[str, str]
        selected_basis: str
        target_record_key = ""
        target_inventory = ""
        target_media_candidate_id = ""
        auto_reason = ""
        metadata_enrichment: dict[str, str] = {}
        if target is not None:
            target_record_key = target["record_key"]
            target_inventory = target["inventory"]
            target_media_candidate_id = target["media_candidate_id"]
            selected_metadata = {
                key: str(value)
                for key, value in target["effective_metadata"].items()
                if str(value).strip()
            }
            selected_metadata["inventory"] = target_inventory
            local_metadata = metadata_for_inventory(raw_matches, target_inventory)
            metadata_enrichment = {
                key: value
                for key, value in local_metadata.items()
                if key in CANONICAL_ENRICHMENT_FIELDS
                and not str(target["effective_metadata"].get(key, "")).strip()
            }
            selected_metadata.update(metadata_enrichment)
            selected_basis = (
                "existing_kurt_effective_metadata_with_missing_local_enrichment"
                if metadata_enrichment
                else "existing_kurt_effective_metadata"
            )
            is_master = row["extension"].casefold() in MASTER_EXTENSIONS
            if target["no_tiff_master"] and is_master:
                queue = "surrogate_master_review"
            else:
                queue = "existing_object_reconciled"
                auto_reason = (
                    "Unique bounded visual match to the existing Kurt object; "
                    "raw spreadsheet mentions remain audit evidence only."
                )
        else:
            queue = "new_local_review"
            selected_metadata, selected_basis = choose_selected_metadata(
                raw_matches,
                filename=row["filename"],
                existing_inventories=existing_inventories,
            )
        summary_text = (
            selected_metadata.get("title")
            or selected_metadata.get("inventory")
            or "No reliable metadata selected"
        )
        reconciled.append(
            {
                **row,
                "queue_class": queue,
                "metadata_summary": summary_text,
                "review_basis": (
                    "Compare the preservation candidate with the current "
                    "surrogate before changing the selected Master."
                    if queue == "surrogate_master_review"
                    else "Unique bounded visual match to an existing Kurt object."
                    if queue == "existing_object_reconciled"
                    else "No unique visual match in the reconciled Kurt/MD object set."
                ),
                "target_record_key": target_record_key,
                "target_inventory": target_inventory,
                "target_media_candidate_id": target_media_candidate_id,
                "related_candidate_id": "",
                "selected_metadata_json": json.dumps(
                    selected_metadata, ensure_ascii=False, sort_keys=True
                ),
                "selected_metadata_basis": selected_basis,
                "metadata_enrichment_json": json.dumps(
                    metadata_enrichment, ensure_ascii=False, sort_keys=True
                ),
                "visual_match_json": json.dumps(
                    audit, ensure_ascii=False, sort_keys=True
                ),
                "auto_decision_reason": auto_reason,
            }
        )

    avantmedia_summary = apply_avantmedia_tiff_primary_policy(
        reconciled,
        record_context=record_context,
        existing_inventories=existing_inventories,
    )

    exact_preview_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reconciled:
        if row["queue_class"] != "new_local_review":
            continue
        digest = candidate_preview_sha256.get(row["candidate_id"], "")
        if digest:
            exact_preview_groups[digest].append(row)
    companion_count = 0
    for group in exact_preview_groups.values():
        if len(group) < 2:
            continue
        group.sort(
            key=lambda item: (
                -len(json.loads(item["selected_metadata_json"])),
                0 if item["extension"].casefold() in MASTER_EXTENSIONS else 1,
                -int(item["size_bytes"]),
                item["filename"].casefold(),
                item["candidate_id"],
            )
        )
        primary = group[0]
        for companion in group[1:]:
            companion["queue_class"] = "new_local_companion_reconciled"
            companion["related_candidate_id"] = primary["candidate_id"]
            companion["auto_decision_reason"] = (
                "Exact derived-preview bytes match the primary new-local "
                "candidate; retain this byte state as a companion representation."
            )
            companion["review_basis"] = (
                "Exact visual duplicate of the bound primary new-local candidate."
            )
            companion_count += 1

    counts = Counter(item["queue_class"] for item in reconciled)
    enrichment_counts = Counter(
        item["selected_metadata_basis"] for item in reconciled
    )
    enriched_fields = Counter(
        key
        for item in reconciled
        for key in json.loads(item["metadata_enrichment_json"])
    )

    reconciled.sort(
        key=lambda item: (
            RECONCILED_QUEUE_CLASSES.index(item["queue_class"]),
            item["filename"].casefold(),
            item["candidate_id"],
        )
    )
    summary = {
        "format": RECONCILED_FORMAT,
        "generated_at": utc_now(),
        "source_census": str(census_csv),
        "source_census_sha256": sha256_file(census_csv),
        "unit_root": str(unit_root),
        "review_root": str(review_root),
        "candidate_count": len(reconciled),
        "queue_counts": {key: counts[key] for key in RECONCILED_QUEUE_CLASSES},
        "selected_metadata_basis_counts": dict(sorted(enrichment_counts.items())),
        "metadata_enrichment_field_counts": dict(sorted(enriched_fields.items())),
        "exact_preview_companions": companion_count,
        "avantmedia_tiff_primary_policy": avantmedia_summary,
        "objects_compared": len(records),
        "decided_representations_loaded": len(decided_media),
        "representatives_compared": len(representatives),
        "representative_preview_errors": representative_errors,
        "preview_errors": preview_errors,
        "visual_policy": {
            "phash_maximum": 8,
            "dhash_maximum": 12,
            "unique_bounded_match_required": True,
        },
        "deletion_authorized": False,
    }
    return write_outputs(
        output_dir,
        reconciled,
        summary,
        fields=RECONCILED_FIELDS,
    )


def build_census(
    *,
    unit_root: Path,
    batch_records: Path,
    shared_records: Path,
    spreadsheet_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    disposition_path = unit_root / "db-restore/manifests/source-disposition.csv"
    if not disposition_path.is_file():
        raise CensusError(f"missing source disposition: {disposition_path}")
    for path in (batch_records, shared_records):
        if not path.is_file():
            raise CensusError(f"missing spreadsheet index: {path}")

    disposition = read_csv(disposition_path)
    source_rows = [
        row
        for row in disposition
        if row["source_path"].startswith(SOURCE_PREFIX)
    ]
    if any(row["deletion_authorized"].casefold() != "false" for row in source_rows):
        raise CensusError("source disposition unexpectedly authorizes deletion")

    master_stems = {
        normalized_stem(row["source_path"])
        for row in source_rows
        if Path(row["source_path"]).suffix.casefold() in MASTER_EXTENSIONS
    }
    exact_paths, exact_keys = metadata_indexes(batch_records, shared_records)

    master_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    jpeg_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in source_rows:
        suffix = Path(row["source_path"]).suffix.casefold()
        if (
            suffix in MASTER_EXTENSIONS
            and row["final_path"].startswith("masters/unassigned/")
        ):
            master_groups[row["source_sha256"]].append(row)
        elif (
            suffix in JPEG_EXTENSIONS
            and row["final_path"].startswith("export/access/unassigned/")
            and "listviewimgs" not in row["source_path"].casefold()
        ):
            jpeg_groups[row["source_sha256"]].append(row)

    target_keys = {
        normalized_stem(row["source_path"])
        for occurrences in (*master_groups.values(), *jpeg_groups.values())
        for row in occurrences
        if len(normalized_stem(row["source_path"])) >= 7
    }
    usb_mentions, spreadsheet_audit = usb_spreadsheet_mentions(
        spreadsheet_root, target_keys
    )

    candidates: list[dict[str, Any]] = []
    queue_counts: Counter[str] = Counter()
    excluded = Counter()

    for digest, occurrences in sorted(master_groups.items()):
        row = sorted(occurrences, key=lambda item: item["source_path"])[0]
        matches = find_metadata(occurrences, exact_paths, exact_keys)
        for key in {normalized_stem(item["source_path"]) for item in occurrences}:
            matches.extend(usb_mentions.get(key, []))
        matches = dedupe_metadata(matches)
        queue = (
            "master_with_metadata" if matches else "master_without_metadata"
        )
        queue_counts[queue] += 1
        candidates.append(
            {
                "candidate_id": candidate_id(digest),
                "queue_class": queue,
                "content_sha256": digest,
                "size_bytes": int(row["source_size_bytes"]),
                "filename": Path(row["source_path"]).name,
                "extension": Path(row["source_path"]).suffix.casefold(),
                "width_px": "",
                "height_px": "",
                "final_path": row["final_path"],
                "primary_source_path": row["source_path"],
                "source_paths_json": json.dumps(
                    sorted({item["source_path"] for item in occurrences}),
                    ensure_ascii=False,
                ),
                "metadata_matches_json": json.dumps(
                    matches, ensure_ascii=False, sort_keys=True
                ),
                "metadata_match_count": len(matches),
                "metadata_summary": metadata_summary(matches),
                "review_basis": (
                    "Unique TIFF/PSD bytes in masters/unassigned; spreadsheet "
                    "metadata confirmed by exact path or unique structured "
                    "inventory/scan identifier."
                    if matches
                    else "Unique TIFF/PSD bytes in masters/unassigned; no "
                    "confirmed match in the validated spreadsheet indexes."
                ),
                "deletion_authorized": "false",
            }
        )

    for digest, occurrences in sorted(jpeg_groups.items()):
        row = sorted(occurrences, key=lambda item: item["source_path"])[0]
        stem = normalized_stem(row["source_path"])
        if stem in master_stems:
            excluded["jpeg_matching_master_stem"] += 1
            continue
        preserved = (unit_root / row["final_path"]).resolve()
        try:
            preserved.relative_to(unit_root.resolve())
        except ValueError as error:
            raise CensusError("preserved JPEG path escaped unit root") from error
        width, height = jpeg_dimensions(preserved)
        if max(width, height) < HIGHRES_LONG_EDGE or width * height < HIGHRES_PIXELS:
            excluded["low_resolution_jpeg"] += 1
            continue
        queue = "hires_jpeg_without_master"
        queue_counts[queue] += 1
        matches = find_metadata(occurrences, exact_paths, exact_keys)
        for key in {normalized_stem(item["source_path"]) for item in occurrences}:
            matches.extend(usb_mentions.get(key, []))
        matches = dedupe_metadata(matches)
        candidates.append(
            {
                "candidate_id": candidate_id(digest),
                "queue_class": queue,
                "content_sha256": digest,
                "size_bytes": int(row["source_size_bytes"]),
                "filename": Path(row["source_path"]).name,
                "extension": Path(row["source_path"]).suffix.casefold(),
                "width_px": width,
                "height_px": height,
                "final_path": row["final_path"],
                "primary_source_path": row["source_path"],
                "source_paths_json": json.dumps(
                    sorted({item["source_path"] for item in occurrences}),
                    ensure_ascii=False,
                ),
                "metadata_matches_json": json.dumps(
                    matches, ensure_ascii=False, sort_keys=True
                ),
                "metadata_match_count": len(matches),
                "metadata_summary": metadata_summary(matches),
                "review_basis": (
                    f"Unique JPEG bytes are at least {HIGHRES_LONG_EDGE}px on "
                    f"the long edge and {HIGHRES_PIXELS:,} pixels; no TIFF/PSD "
                    "with the same normalized filename stem is manifested."
                ),
                "deletion_authorized": "false",
            }
        )

    candidates.sort(
        key=lambda item: (
            QUEUE_CLASSES.index(item["queue_class"]),
            item["filename"].casefold(),
            item["candidate_id"],
        )
    )
    if len(candidates) != len({item["candidate_id"] for item in candidates}):
        raise CensusError("candidate IDs are not unique")
    summary = {
        "format": FORMAT,
        "generated_at": utc_now(),
        "unit_root": str(unit_root),
        "source_prefix": SOURCE_PREFIX,
        "source_disposition": str(disposition_path),
        "source_disposition_sha256": sha256_file(disposition_path),
        "batch_records": str(batch_records),
        "batch_records_sha256": sha256_file(batch_records),
        "shared_records": str(shared_records),
        "shared_records_sha256": sha256_file(shared_records),
        "spreadsheet_root": str(spreadsheet_root),
        "spreadsheet_audit": spreadsheet_audit,
        "candidate_count": len(candidates),
        "queue_counts": {key: queue_counts[key] for key in QUEUE_CLASSES},
        "master_source_paths": sum(map(len, master_groups.values())),
        "master_unique_digests": len(master_groups),
        "jpeg_unique_digests_considered": len(jpeg_groups),
        "excluded": dict(sorted(excluded.items())),
        "highres_policy": {
            "long_edge_minimum_px": HIGHRES_LONG_EDGE,
            "pixel_count_minimum": HIGHRES_PIXELS,
        },
        "low_resolution_included": False,
        "reviewed_redundant_tiffs_included": False,
        "deletion_authorized": False,
    }
    return candidates, summary


def write_outputs(
    output_dir: Path,
    candidates: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    fields: tuple[str, ...] = FIELDS,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    csv_path = output_dir / "object-census.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(candidates)
        handle.flush()
        os.fsync(handle.fileno())
    summary = {
        **summary,
        "census_csv": str(csv_path),
        "census_sha256": sha256_file(csv_path),
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {**summary, "summary_path": str(summary_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-root", type=Path, required=True)
    parser.add_argument("--batch-records", type=Path)
    parser.add_argument("--shared-records", type=Path)
    parser.add_argument("--spreadsheet-root", type=Path)
    parser.add_argument(
        "--reconcile-census",
        type=Path,
        help="reclassify an already imported raw census around existing objects",
    )
    parser.add_argument(
        "--review-root",
        type=Path,
        help="live reconciliation root required by --reconcile-census",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.reconcile_census is not None:
        if arguments.review_root is None:
            parser.error("--review-root is required with --reconcile-census")
        result = reconcile_existing_census(
            unit_root=arguments.unit_root.resolve(strict=True),
            review_root=arguments.review_root.resolve(strict=True),
            census_csv=arguments.reconcile_census.resolve(strict=True),
            output_dir=arguments.output_dir.resolve(),
            workers=arguments.workers,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not all(
        (arguments.batch_records, arguments.shared_records, arguments.spreadsheet_root)
    ):
        parser.error(
            "--batch-records, --shared-records, and --spreadsheet-root are "
            "required for a raw census"
        )
    candidates, summary = build_census(
        unit_root=arguments.unit_root.resolve(strict=True),
        batch_records=arguments.batch_records.resolve(strict=True),  # type: ignore[union-attr]
        shared_records=arguments.shared_records.resolve(strict=True),  # type: ignore[union-attr]
        spreadsheet_root=arguments.spreadsheet_root.resolve(strict=True),  # type: ignore[union-attr]
    )
    result = write_outputs(arguments.output_dir.resolve(), candidates, summary)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
