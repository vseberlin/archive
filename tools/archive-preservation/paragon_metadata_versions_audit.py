#!/usr/bin/env python3
"""Audit overlapping Paragon metadata/export files without changing sources."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from recovery_workbook_backfill import workbook_rows


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN, "r": OFFICE_REL}
FORMAT = "iss-paragon-metadata-version-audit-v1"
ROW_FIELDS = (
    "object_publication",
    "collection_name1",
    "collection",
    "object_type",
    "object_title",
    "object_description",
    "object_material_technique",
    "object_dimensions",
    "owner_previous",
    "related_place1",
    "related_actor1",
    "related_actor2",
    "printed_who1",
    "printed_when1",
    "printed_when_1",
    "production_who1",
    "production_who",
    "production_where1",
    "related_time1",
    "image_name1",
    "tag1",
    "tag2",
    "tag3",
    "tag4",
    "tag5",
    "tag6",
    "tag7",
    "tag8",
    "tag9",
    "tag10",
)
FINAL_EXPORT_CLASSES = {"museum_digital_final_export", "museum_digital_xml_export"}


class AuditError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def row_digest(row: dict[str, str]) -> str:
    encoded = json.dumps(
        {key: value for key, value in sorted(row.items()) if value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def column_number(reference: str) -> int:
    result = 0
    for value in reference:
        if not value.isalpha():
            break
        result = result * 26 + ord(value.upper()) - 64
    return result


def normalize_header(header: str) -> str:
    header = header.strip().replace(" ", "_")
    if header.startswith("inventory_number"):
        return "inventory_number"
    return header


def parse_xlsx_generic(path: Path) -> tuple[str, dict[str, dict[str, str]], list[str]]:
    with ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared.append(
                    "".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
                )
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}
        sheet = workbook.find("m:sheets/m:sheet", NS)
        if sheet is None:
            raise AuditError(f"workbook contains no worksheet: {path}")
        sheet_name = str(sheet.attrib["name"])
        target = targets[sheet.attrib[f"{{{OFFICE_REL}}}id"]]
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        worksheet = ET.fromstring(archive.read(target))
        parsed: list[dict[int, str]] = []
        for row in worksheet.findall(".//m:sheetData/m:row", NS):
            values: dict[int, str] = {}
            for cell in row.findall("m:c", NS):
                cell_type = cell.attrib.get("t", "")
                raw = cell.find("m:v", NS)
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or "" for node in cell.iter(f"{{{MAIN}}}t")
                    )
                elif raw is None:
                    value = ""
                elif cell_type == "s":
                    value = shared[int(str(raw.text))]
                else:
                    value = str(raw.text or "")
                values[column_number(cell.attrib.get("r", ""))] = value.strip()
            parsed.append(values)
    if not parsed:
        return sheet_name, {}, []
    headers = {
        index: normalize_header(value)
        for index, value in parsed[0].items()
        if normalize_header(value)
    }
    rows: dict[str, dict[str, str]] = {}
    for values in parsed[1:]:
        inventory = values.get(1, "").strip()
        if not inventory:
            continue
        row = {
            header: values.get(index, "").strip()
            for index, header in sorted(headers.items())
        }
        row["inventory_number"] = inventory
        rows[inventory] = row
    return sheet_name, rows, list(headers.values())


def parse_xlsx(path: Path) -> tuple[str, dict[str, dict[str, str]], list[str], str]:
    try:
        sheet, rows = workbook_rows(path)
        headers = list(next(iter(rows.values()))["values"].keys()) if rows else []
        normalized = {
            inventory: {
                normalize_header(key): str(value or "").strip()
                for key, value in payload["values"].items()
            }
            for inventory, payload in rows.items()
        }
        for inventory, row in normalized.items():
            row["inventory_number"] = inventory
        return sheet, normalized, [normalize_header(item) for item in headers], ""
    except Exception as error:
        sheet, rows, headers = parse_xlsx_generic(path)
        return sheet, rows, headers, f"generic_xlsx_parse_after:{error}"


def sniff_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:4096]
    return ";" if sample.count(";") > sample.count(",") else ","


def parse_csv(path: Path) -> tuple[dict[str, dict[str, str]], list[str], str]:
    delimiter = sniff_delimiter(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = [normalize_header(item or "") for item in (reader.fieldnames or [])]
        rows: dict[str, dict[str, str]] = {}
        for raw in reader:
            row = {
                normalize_header(key or ""): str(value or "").strip()
                for key, value in raw.items()
            }
            inventory = row.get("inventory_number", "").strip()
            if inventory:
                rows[inventory] = row
    return rows, headers, f"delimiter={delimiter}"


def parse_xml_zip(path: Path) -> tuple[dict[str, dict[str, str]], list[str], str]:
    rows: dict[str, dict[str, str]] = {}
    headers: set[str] = set()
    bad: list[str] = []
    with ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.casefold().endswith(".xml"):
                continue
            try:
                root = ET.fromstring(archive.read(name))
            except ET.ParseError as error:
                bad.append(f"{name}:{error}")
                continue
            row = {
                normalize_header(child.tag): (child.text or "").strip()
                for child in root
            }
            inventory = row.get("inventory_number", "").strip()
            if inventory:
                row["_zip_entry"] = name
                rows[inventory] = row
                headers.update(row)
    note = f"xml_entries={len(rows)}"
    if bad:
        note += f"; bad_xml={len(bad)}"
    return rows, sorted(headers), note


def classify(path: Path, digest: str, duplicates: dict[str, list[Path]]) -> str:
    name = path.name.casefold()
    if len(duplicates[digest]) > 1 and path != sorted(duplicates[digest], key=str)[0]:
        return "duplicate_copy"
    if "stand 05.02.2020" in name:
        return "candidate_archive_metadata_authority"
    if "final" in name:
        return "museum_digital_final_export"
    if "csv_xml.zip" == name:
        return "museum_digital_xml_export"
    if "_02" in name or "_03" in name or "_04" in name or "_05" in name or "_06" in name:
        return "museum_digital_intermediate_export"
    if "fertig für md" in name:
        return "museum_digital_export_source"
    return "metadata_evidence"


def collect_sources(roots: list[Path]) -> list[Path]:
    suffixes = {".csv", ".xls", ".xlsx", ".xml", ".zip"}
    paths: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in suffixes:
                paths.append(path)
    return sorted(paths, key=lambda item: str(item))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def first_source_id(file_rows: list[dict[str, Any]], classification: str) -> str:
    for row in file_rows:
        if row["classification"] == classification:
            return str(row["source_id"])
    return ""


def source_policy_rows(file_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    policies = {
        "candidate_archive_metadata_authority": (
            "use_as_metadata_authority_candidate",
            "broadest current row set; source workbook for archive metadata",
        ),
        "museum_digital_export_source": (
            "retain_as_export_provenance",
            "earlier museum-digital preparation export",
        ),
        "duplicate_copy": (
            "retain_as_duplicate_evidence",
            "byte-identical duplicate; do not use as independent authority",
        ),
        "museum_digital_intermediate_export": (
            "retain_as_version_evidence",
            "intermediate museum-digital preparation version",
        ),
        "museum_digital_final_export": (
            "retain_as_final_md_export_evidence",
            "final museum-digital CSV/XLSX export evidence",
        ),
        "museum_digital_xml_export": (
            "retain_as_final_md_xml_evidence",
            "XML export evidence; not LIDO",
        ),
    }
    rows: list[dict[str, str]] = []
    for row in file_rows:
        decision, reason = policies.get(
            str(row["classification"]),
            ("retain_as_metadata_evidence", "unclassified metadata evidence"),
        )
        rows.append(
            {
                "source_id": str(row["source_id"]),
                "decision": decision,
                "reason": reason,
                "classification": str(row["classification"]),
                "filename": str(row["filename"]),
                "path": str(row["path"]),
                "sha256": str(row["sha256"]),
            }
        )
    return rows


def authority_gap_rows(
    parsed: dict[str, dict[str, dict[str, str]]],
    file_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    authority_id = first_source_id(file_rows, "candidate_archive_metadata_authority")
    final_ids = {
        str(row["source_id"])
        for row in file_rows
        if row["classification"] in FINAL_EXPORT_CLASSES
    }
    if not authority_id:
        return []
    authority_rows = parsed.get(authority_id, {})
    rows: list[dict[str, str]] = []
    for inventory in sorted(authority_rows):
        present_final = sorted(
            source_id
            for source_id in final_ids
            if inventory in parsed.get(source_id, {})
        )
        if present_final:
            continue
        source_row = authority_rows[inventory]
        image_name = source_row.get("image_name1", "")
        image_suffix = Path(image_name).suffix.casefold()
        if image_suffix in {".pdf", ".doc", ".docx"}:
            action = "ignore_document_noise"
            reason = "authority_row_points_to_ignored_pdf_or_docx"
            media_scope = "ignored_document_noise"
        elif image_name:
            action = "review_for_archive_completion"
            reason = "present_in_authority_workbook_not_in_final_md_export_or_xml"
            media_scope = "image_candidate"
        else:
            action = "review_metadata_only_gap"
            reason = "authority_row_missing_final_md_export_or_xml_and_image_hint"
            media_scope = "metadata_only"
        rows.append(
            {
                "inventory": inventory,
                "action": action,
                "reason": reason,
                "media_scope": media_scope,
                "authority_source": authority_id,
                "missing_from_final_sources": ";".join(sorted(final_ids)),
                "object_title": source_row.get("object_title", ""),
                "object_description": source_row.get("object_description", ""),
                "image_name1": image_name,
                "date_candidate": source_row.get("date_created", "")
                or source_row.get("printed_when_1", "")
                or source_row.get("printed_when1", ""),
            }
        )
    return rows


def audit(roots: list[Path], output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=False)
    paths = collect_sources(roots)
    digests = {path: sha256_file(path) for path in paths}
    duplicates: dict[str, list[Path]] = defaultdict(list)
    for path, digest in digests.items():
        duplicates[digest].append(path)

    parsed: dict[str, dict[str, dict[str, str]]] = {}
    file_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for index, path in enumerate(paths, start=1):
        source_id = f"S{index:02d}"
        suffix = path.suffix.casefold()
        rows: dict[str, dict[str, str]] = {}
        headers: list[str] = []
        note = ""
        try:
            if suffix == ".xlsx":
                sheet, rows, headers, note = parse_xlsx(path)
            elif suffix == ".csv":
                rows, headers, note = parse_csv(path)
                sheet = ""
            elif suffix == ".zip":
                rows, headers, note = parse_xml_zip(path)
                sheet = ""
            elif suffix == ".xml":
                root = ET.fromstring(path.read_text(encoding="utf-8"))
                row = {
                    normalize_header(child.tag): (child.text or "").strip()
                    for child in root
                }
                inventory = row.get("inventory_number", "").strip()
                rows = {inventory: row} if inventory else {}
                headers = sorted(row)
                sheet = ""
            else:
                sheet = ""
                note = "unsupported_metadata_file"
        except Exception as error:
            sheet = ""
            note = f"parse_error:{error}"
            errors.append({"source_id": source_id, "path": str(path), "error": str(error)})
        parsed[source_id] = rows
        stat = path.stat()
        file_rows.append(
            {
                "source_id": source_id,
                "classification": classify(path, digests[path], duplicates),
                "path": str(path),
                "filename": path.name,
                "suffix": suffix,
                "size_bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "sha256": "sha256:" + digests[path],
                "duplicate_count": len(duplicates[digests[path]]),
                "row_count": len(rows),
                "header_count": len(headers),
                "sheet": sheet,
                "note": note,
            }
        )

    all_inventories = sorted({inventory for rows in parsed.values() for inventory in rows})
    source_ids = [row["source_id"] for row in file_rows]
    presence_rows: list[dict[str, Any]] = []
    for inventory in all_inventories:
        present = [source_id for source_id in source_ids if inventory in parsed[source_id]]
        digests_for_inventory = {
            row_digest(parsed[source_id][inventory])
            for source_id in present
        }
        presence_rows.append(
            {
                "inventory": inventory,
                "present_count": len(present),
                "row_digest_count": len(digests_for_inventory),
                "present_in": ";".join(present),
                "missing_from": ";".join(
                    source_id for source_id in source_ids if source_id not in present
                ),
            }
        )

    conflict_rows: list[dict[str, Any]] = []
    for inventory in all_inventories:
        for field in ROW_FIELDS:
            values: dict[str, list[str]] = defaultdict(list)
            for source_id in source_ids:
                value = parsed[source_id].get(inventory, {}).get(field, "")
                if value:
                    values[value].append(source_id)
            if len(values) > 1:
                conflict_rows.append(
                    {
                        "inventory": inventory,
                        "field": field,
                        "distinct_values": len(values),
                        "values_by_source": json.dumps(
                            {
                                value: sources
                                for value, sources in sorted(values.items())
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    }
                )

    policy_rows = source_policy_rows(file_rows)
    gap_rows = authority_gap_rows(parsed, file_rows)

    write_csv(
        output_root / "file-manifest.csv",
        file_rows,
        [
            "source_id",
            "classification",
            "path",
            "filename",
            "suffix",
            "size_bytes",
            "mtime_utc",
            "sha256",
            "duplicate_count",
            "row_count",
            "header_count",
            "sheet",
            "note",
        ],
    )
    write_csv(
        output_root / "row-presence.csv",
        presence_rows,
        ["inventory", "present_count", "row_digest_count", "present_in", "missing_from"],
    )
    write_csv(
        output_root / "field-conflicts.csv",
        conflict_rows,
        ["inventory", "field", "distinct_values", "values_by_source"],
    )
    write_csv(
        output_root / "source-policy.csv",
        policy_rows,
        [
            "source_id",
            "decision",
            "reason",
            "classification",
            "filename",
            "path",
            "sha256",
        ],
    )
    write_csv(
        output_root / "authority-gap-queue.csv",
        gap_rows,
        [
            "inventory",
            "action",
            "reason",
            "media_scope",
            "authority_source",
            "missing_from_final_sources",
            "object_title",
            "object_description",
            "image_name1",
            "date_candidate",
        ],
    )
    (output_root / "parsed-rows.json").write_text(
        json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "format": FORMAT,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "roots": [str(root) for root in roots],
        "output": str(output_root),
        "files": len(file_rows),
        "inventories": len(all_inventories),
        "field_conflicts": len(conflict_rows),
        "authority_gap_queue_rows": len(gap_rows),
        "parse_errors": errors,
        "classifications": {
            label: sum(1 for row in file_rows if row["classification"] == label)
            for label in sorted({row["classification"] for row in file_rows})
        },
        "recommended_policy": {
            "archive_metadata_authority": "Bildtexte Paragon stand 05.02.2020.xlsx",
            "museum_digital_export_evidence": "Paragon_Bildtexte-fertig-für-md_final.csv/.xlsx and csv_xml.zip",
            "intermediate_versions": "retain as provenance/version evidence unless unique rows or fields are needed",
            "editing_rule": "do not edit source spreadsheets; apply decisions through logged archive operations",
        },
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_root / "README.md").write_text(
        "# Paragon Metadata Version Audit\n\n"
        f"Generated: {summary['created_at']}\n\n"
        "This is a read-only reconciliation of Paragon CSV/XLSX/XML/ZIP metadata "
        "exports. Standalone PDF/DOCX files are intentionally excluded.\n\n"
        "## Outputs\n\n"
        "- `file-manifest.csv`: file-level digest, row-count, parser, and classification.\n"
        "- `row-presence.csv`: inventory presence/missing matrix across sources.\n"
        "- `field-conflicts.csv`: non-empty field differences across sources.\n"
        "- `source-policy.csv`: recommended handling for each metadata/export file.\n"
        "- `authority-gap-queue.csv`: authority workbook rows absent from final MD export/XML.\n"
        "- `parsed-rows.json`: normalized row content keyed by source id.\n"
        "- `summary.json`: machine-readable summary and recommended policy.\n\n"
        "## Policy Recommendation\n\n"
        "Treat `Bildtexte Paragon stand 05.02.2020.xlsx` as the current archive "
        "metadata authority candidate because it has the broadest row set. Treat "
        "the `fertig für md` CSV/XLSX/XML files as museum-digital export evidence "
        "and version history, not as a replacement for the archive workbook.\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--root", action="append", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = [root.resolve(strict=True) for root in args.root]
    summary = audit(roots, args.output.resolve())
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
