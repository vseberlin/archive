#!/usr/bin/env python3
"""Parse Kurt Schwarz workbooks recovered from the shared Google Drive backup."""

from __future__ import annotations

import argparse
import csv
import json
import os
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


FORMAT = "iss-kurt-schwarz-shared-workbooks-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_CONTAINER_RUN = WORK_ROOT / (
    "work/reconciliation/shared-containers/"
    "20260730T091000Z-google-drive-kurt-schwarz"
)
DEFAULT_OBJECT_PLAN = WORK_ROOT / (
    "work/reconciliation/object-plans/"
    "20260730T001000Z-kurt-schwarz-object-plan/object-plan.csv"
)
MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
INVENTORY = re.compile(r"(?i)^KS-[5-9]-(?:BZ|NB|WF|ZP)[_-]\d+(?:[-_][A-Z0-9]+)*$")
SCAN = re.compile(r"(?i)K\d{2}_A\d{2}_T\d{2,3}")
FIELDS = (
    "source_document",
    "source_member",
    "source_member_sha256",
    "sheet_name",
    "row_number",
    "record_kind",
    "inventory",
    "scan_signatures",
    "mapped_record_keys",
    "mapped_md_object_ids",
    "mapping_state",
    "record_json",
    "deletion_authorized",
)


class WorkbookError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise WorkbookError(f"path escaped allowed root: {resolved}") from error
    return resolved


def xlsx_column(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    number = 0
    for character in letters.upper():
        number = number * 26 + ord(character) - 64
    return number - 1


def cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(MAIN_NS + "t")).strip()
    node = cell.find(MAIN_NS + "v")
    value = "" if node is None else (node.text or "")
    if kind == "s" and value:
        value = shared[int(value)]
    return " ".join(value.split())


def unique_headers(values: list[str]) -> list[str]:
    counts: Counter[str] = Counter()
    result: list[str] = []
    for index, value in enumerate(values):
        base = " ".join(value.split()) or f"column_{index + 1}"
        counts[base] += 1
        result.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return result


def read_xlsx(path: Path) -> list[tuple[str, int, dict[str, str]]]:
    result: list[tuple[str, int, dict[str, str]]] = []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(text.text or "" for text in item.iter(MAIN_NS + "t"))
                for item in root.findall(MAIN_NS + "si")
            ]
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        targets = {
            item.attrib["Id"]: item.attrib["Target"] for item in relationships
        }
        sheets = workbook.find(MAIN_NS + "sheets")
        if sheets is None:
            raise WorkbookError(f"workbook has no sheets: {path}")
        for sheet_record in sheets:
            sheet_name = sheet_record.attrib["name"]
            target = targets[sheet_record.attrib[REL_NS + "id"]]
            target = (
                target[1:]
                if target.startswith("/")
                else posixpath.normpath("xl/" + target)
            )
            sheet = ElementTree.fromstring(archive.read(target))
            raw_rows: list[tuple[int, dict[int, str]]] = []
            for row in sheet.iter(MAIN_NS + "row"):
                values = {
                    xlsx_column(cell.attrib.get("r", "A1")): cell_text(cell, shared)
                    for cell in row.findall(MAIN_NS + "c")
                }
                if any(values.values()):
                    raw_rows.append((int(row.attrib.get("r", "0")), values))
            if not raw_rows:
                continue
            maximum = max(max(values, default=0) for _, values in raw_rows)
            first_values = raw_rows[0][1]
            headers = unique_headers(
                [first_values.get(index, "") for index in range(maximum + 1)]
            )
            for row_number, values in raw_rows[1:]:
                record = {
                    headers[index]: values.get(index, "")
                    for index in range(maximum + 1)
                    if values.get(index, "")
                }
                if record:
                    result.append((sheet_name, row_number, record))
    return result


def record_kind(path: Path) -> str:
    name = path.name.casefold()
    if "gesamtverzeichnis" in name:
        return "finding_aid"
    if "berlinerzeitung" in name:
        return "newspaper_research"
    if "bz nachtragen" in name:
        return "newspaper_followup"
    if "arbeitspakete" in name or "zeitplanung" in name:
        return "project_planning"
    return "upload_metadata"


def find_inventory(record: dict[str, str]) -> str:
    preferred = (
        "inventory_number",
        "Inventarnummer",
        "Signatur",
        "SIGNATUR",
    )
    for key in preferred:
        value = record.get(key, "").strip()
        if INVENTORY.fullmatch(value):
            return value.replace("-", "-", 1)
    for value in record.values():
        value = value.strip()
        if INVENTORY.fullmatch(value):
            return value
    return ""


def normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = re.sub(
        r"\b(?:sw|farb)[ -]?foto.*?kurt schwarz\.?$", "", value
    )
    return " ".join(re.findall(r"[a-z0-9]+", value))


def inventory_tail(value: str) -> str:
    match = re.fullmatch(
        r"(?i)KS-[5-9]-((?:BZ|NB|WF|ZP)[_-]\d+(?:[-_][A-Z0-9]+)*)",
        value.strip(),
    )
    return match.group(1).casefold() if match else ""


def build_object_indices(
    path: Path,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
]:
    inventory_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    title_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    tail_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        values = (
            row["current_inventory"],
            row["historical_inventory"],
            *row["batch_alias_inventories"].split("; "),
        )
        for value in values:
            if value:
                inventory_index[value.casefold()].append(row)
                tail = inventory_tail(value)
                if tail:
                    tail_index[tail].append(row)
        for value in (row["current_title"], row["historical_title"]):
            title = normalized_title(value)
            if title:
                title_index[title].append(row)
    return inventory_index, title_index, tail_index


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
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


def run(container_run: Path, object_plan: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with (container_run / "members.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        members = list(csv.DictReader(handle))
    member_by_tail = {
        str(Path(*row["member"].split("/")[1:])): row
        for row in members
        if row["extension"] == ".xlsx"
    }
    object_index, title_index, tail_index = build_object_indices(object_plan)
    rows: list[dict[str, str]] = []
    for path in sorted((container_run / "documents").rglob("*.xlsx")):
        relative = str(path.relative_to(container_run / "documents"))
        member = member_by_tail.get(relative)
        if member is None:
            raise WorkbookError(f"workbook lacks shared-container provenance: {path}")
        kind = record_kind(path)
        for sheet_name, row_number, record in read_xlsx(path):
            inventory = find_inventory(record)
            objects = (
                object_index.get(inventory.casefold(), []) if inventory else []
            )
            unique_objects = {
                row["record_key"]: row for row in objects
            }
            mapping_state = ""
            if inventory and len(unique_objects) == 1:
                mapping_state = "mapped_inventory"
            elif inventory and len(unique_objects) > 1:
                mapping_state = "ambiguous_inventory"
            elif inventory:
                title = normalized_title(
                    record.get("object_title", "") or record.get("Titel", "")
                )
                title_objects = {
                    row["record_key"]: row for row in title_index.get(title, [])
                }
                tail_objects = {
                    row["record_key"]: row
                    for row in tail_index.get(inventory_tail(inventory), [])
                }
                if title and len(title_objects) == 1:
                    unique_objects = title_objects
                    mapping_state = "mapped_title_alias"
                elif len(tail_objects) == 1:
                    unique_objects = tail_objects
                    mapping_state = "mapped_inventory_tail_alias"
                else:
                    mapping_state = "local_inventory_not_in_object_plan"
            else:
                mapping_state = "contextual_record_without_inventory"
            scan_signatures = sorted(
                {
                    value.upper()
                    for cell in record.values()
                    for value in SCAN.findall(cell)
                }
            )
            rows.append(
                {
                    "source_document": str(path),
                    "source_member": member["member"],
                    "source_member_sha256": member["member_sha256"],
                    "sheet_name": sheet_name,
                    "row_number": str(row_number),
                    "record_kind": kind,
                    "inventory": inventory,
                    "scan_signatures": "; ".join(scan_signatures),
                    "mapped_record_keys": "; ".join(sorted(unique_objects)),
                    "mapped_md_object_ids": "; ".join(
                        sorted(
                            {
                                row["md_object_id"]
                                for row in unique_objects.values()
                                if row["md_object_id"]
                            },
                            key=int,
                        )
                    ),
                    "mapping_state": mapping_state,
                    "record_json": json.dumps(
                        record, ensure_ascii=False, sort_keys=True
                    ),
                    "deletion_authorized": "false",
                }
            )
    rows.sort(
        key=lambda row: (
            row["source_member"],
            row["sheet_name"],
            int(row["row_number"]),
        )
    )
    atomic_csv(output / "shared-workbook-records.csv", rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "shared_container_run": str(container_run),
        "object_plan": str(object_plan),
        "workbooks": len({row["source_document"] for row in rows}),
        "record_rows": len(rows),
        "record_kind_counts": dict(Counter(row["record_kind"] for row in rows)),
        "mapping_state_counts": dict(
            Counter(row["mapping_state"] for row in rows)
        ),
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-run", type=Path, default=DEFAULT_CONTAINER_RUN)
    parser.add_argument("--object-plan", type=Path, default=DEFAULT_OBJECT_PLAN)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(
            args.container_run.expanduser().resolve(),
            args.object_plan.expanduser().resolve(),
            output,
        )
    except (OSError, csv.Error, ValueError, zipfile.BadZipFile, WorkbookError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
