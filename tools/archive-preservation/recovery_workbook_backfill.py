#!/usr/bin/env python3
"""Backfill complete exact-inventory workbook rows into recovery working state."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import reconciliation
from recovery_exception_queue import backup_database, stable_id


class WorkbookBackfillError(RuntimeError):
    pass


MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
NS = {"m": MAIN, "r": OFFICE_REL}
CORE_FIELDS = {
    "archive_descriptive_title": "object_title",
    "archive_description": "object_description",
    "archive_material_technique": "object_material_technique",
    "archive_dimensions": "object_dimensions",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def column_number(reference: str) -> int:
    result = 0
    for value in reference:
        if not value.isalpha():
            break
        result = result * 26 + ord(value.upper()) - 64
    return result


def workbook_rows(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    with ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", NS):
                shared.append(
                    "".join(
                        node.text or ""
                        for node in item.iter(f"{{{MAIN}}}t")
                    )
                )
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships
        }
        sheet = workbook.find("m:sheets/m:sheet", NS)
        if sheet is None:
            raise WorkbookBackfillError("workbook contains no worksheet")
        sheet_name = str(sheet.attrib["name"])
        target = targets[sheet.attrib[f"{{{OFFICE_REL}}}id"]]
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        worksheet = ET.fromstring(archive.read(target))
        parsed: list[tuple[int, dict[int, str]]] = []
        for row in worksheet.findall(".//m:sheetData/m:row", NS):
            values: dict[int, str] = {}
            for cell in row.findall("m:c", NS):
                cell_type = cell.attrib.get("t", "")
                raw = cell.find("m:v", NS)
                if cell_type == "inlineStr":
                    value = "".join(
                        node.text or ""
                        for node in cell.iter(f"{{{MAIN}}}t")
                    )
                elif raw is None:
                    value = ""
                elif cell_type == "s":
                    value = shared[int(str(raw.text))]
                else:
                    value = str(raw.text or "")
                values[column_number(cell.attrib.get("r", ""))] = value.strip()
            parsed.append((int(row.attrib["r"]), values))
    if not parsed:
        raise WorkbookBackfillError("worksheet contains no rows")
    headers = parsed[0][1]
    if headers.get(1) != "inventory_number":
        raise WorkbookBackfillError(
            "first workbook column is not inventory_number"
        )
    rows: dict[str, dict[str, Any]] = {}
    for row_number, values in parsed[1:]:
        inventory = values.get(1, "").strip()
        if not inventory:
            continue
        if inventory.casefold() in {key.casefold() for key in rows}:
            raise WorkbookBackfillError(
                f"duplicate inventory row in workbook: {inventory}"
            )
        rows[inventory] = {
            "row": row_number,
            "values": {
                header: values.get(index, "")
                for index, header in sorted(headers.items())
                if header
            },
        }
    return sheet_name, rows


def local_values(
    *,
    workbook: Path,
    digest: str,
    sheet: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    values = row["values"]
    tags = [
        str(values.get(f"tag{index}", "") or "").strip()
        for index in range(1, 11)
        if str(values.get(f"tag{index}", "") or "").strip()
    ]
    return {
        "title": str(values.get("object_title", "") or "").strip(),
        "description": str(values.get("object_description", "") or "").strip(),
        "material_technique": str(
            values.get("object_material_technique", "") or ""
        ).strip(),
        "dimensions": str(values.get("object_dimensions", "") or "").strip(),
        "object_type": str(values.get("object_type", "") or "").strip(),
        "date": str(
            values.get("related_time1")
            or values.get("printed_when_1")
            or ""
        ).strip(),
        "tags": tags,
        "workbook": {
            "path": str(workbook),
            "sha256": digest,
            "sheet": sheet,
            "row": int(row["row"]),
            "values": values,
        },
    }


def build_plan(
    root: Path,
    workbook: Path,
    inventories: list[str],
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    workbook = workbook.resolve(strict=True)
    digest = sha256_file(workbook)
    sheet, rows = workbook_rows(workbook)
    manifest_path = (
        root
        / "work"
        / "recovery"
        / "WF-PARAGON"
        / "reconciliation-exceptions.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_records = {
        str(item["inventory"]): item
        for item in manifest.get("records") or []
    }
    requested = inventories or sorted(manifest_records)
    missing_manifest = [
        inventory for inventory in requested if inventory not in manifest_records
    ]
    if missing_manifest:
        raise WorkbookBackfillError(
            "inventory absent from recovery exception manifest: "
            + ", ".join(missing_manifest)
        )
    matched = [
        {
            "inventory": inventory,
            "row": int(rows[inventory]["row"]),
            "local": local_values(
                workbook=workbook,
                digest=digest,
                sheet=sheet,
                row=rows[inventory],
            ),
        }
        for inventory in requested
        if inventory in rows
    ]
    return {
        "format": "iss-recovery-workbook-backfill-v1",
        "root": str(root),
        "workbook": str(workbook),
        "workbook_sha256": digest,
        "sheet": sheet,
        "requested": requested,
        "matched": matched,
        "not_found": [
            inventory for inventory in requested if inventory not in rows
        ],
        "manifest": str(manifest_path),
    }


def apply_plan(plan: dict[str, Any], actor: str) -> dict[str, Any]:
    root = Path(plan["root"])
    manifest_path = Path(plan["manifest"])
    campaign_path = (
        root / "work" / "reconciliation" / "museum-digital.sqlite"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / "work" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest_backup = (
        backup_dir / f"{stamp}-PRE-WORKBOOK-BACKFILL-exceptions.json"
    )
    shutil.copy2(manifest_path, manifest_backup)
    database_backup = backup_database(
        campaign_path,
        "PRE-WORKBOOK-BACKFILL-CAMPAIGN",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_inventory = {
        str(item["inventory"]): item for item in manifest["records"]
    }
    for match in plan["matched"]:
        item = by_inventory[match["inventory"]]
        item["local"] = match["local"]
        item["reason"] = (
            str(item.get("reason") or "")
            + " Complete exact-inventory metadata restored from "
            + f"{plan['workbook']}#{plan['sheet']}!{match['row']}."
        ).strip()
    temporary = manifest_path.with_name(
        f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)

    campaign = reconciliation.connect_campaign(root)
    try:
        campaign.execute("BEGIN IMMEDIATE")
        for match in plan["matched"]:
            inventory = str(match["inventory"])
            local = match["local"]
            locator = (
                f"{plan['workbook']}#{plan['sheet']}!row={match['row']}"
            )
            record = campaign.execute(
                """SELECT record_id FROM reconciliation_records
                   WHERE campaign_id='MD-RECOVERY-WF-PARAGON'
                     AND inventory_label=?""",
                (inventory,),
            ).fetchone()
            if not record:
                raise WorkbookBackfillError(
                    f"campaign record is missing: {inventory}"
                )
            record_id = str(record["record_id"])
            campaign.execute(
                """UPDATE reconciliation_records
                   SET local_archival_id=?, source_record_status='exact_inventory',
                       source_record_locator=?, updated_at=?
                   WHERE record_id=?""",
                (inventory, locator, catalog_core.utc_now(), record_id),
            )
            for field_key, local_key in CORE_FIELDS.items():
                value = str(local[{
                    "object_title": "title",
                    "object_description": "description",
                    "object_material_technique": "material_technique",
                    "object_dimensions": "dimensions",
                }[local_key]] or "").strip()
                campaign.execute(
                    """UPDATE field_candidates
                       SET value_text=?, source_locator=?, status=?
                       WHERE record_id=? AND field_key=?
                         AND source_side='local'""",
                    (
                        value,
                        locator,
                        "available" if value else "missing",
                        record_id,
                        field_key,
                    ),
                )
            standard_context = {
                "object_type": str(local["object_type"]),
                "date": str(local["date"]),
            }
            for key, value in standard_context.items():
                campaign.execute(
                    """INSERT INTO context_values (
                           context_id, record_id, context_key, context_label,
                           source_side, value_text
                       ) VALUES (?, ?, ?, ?, 'local', ?)
                       ON CONFLICT(record_id, context_key, source_side)
                       DO UPDATE SET value_text=excluded.value_text""",
                    (
                        stable_id("CTX", record_id, key, "local"),
                        record_id,
                        key,
                        key.replace("_", " ").title(),
                        value,
                    ),
                )
            for header, value in local["workbook"]["values"].items():
                if not str(value).strip():
                    continue
                key = "workbook_" + header
                campaign.execute(
                    """INSERT INTO context_values (
                           context_id, record_id, context_key, context_label,
                           source_side, value_text
                       ) VALUES (?, ?, ?, ?, 'local', ?)
                       ON CONFLICT(record_id, context_key, source_side)
                       DO UPDATE SET value_text=excluded.value_text""",
                    (
                        stable_id("CTX", record_id, key, "local"),
                        record_id,
                        key,
                        header,
                        str(value),
                    ),
                )
        if campaign.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise WorkbookBackfillError("campaign integrity check failed")
        campaign.commit()
    except Exception:
        campaign.rollback()
        raise
    finally:
        campaign.close()

    audit_dir = (
        root
        / "work"
        / "audits"
        / "WF-PARAGON"
        / f"workbook-backfill-{stamp}"
    )
    audit_dir.mkdir(parents=True, exist_ok=False)
    receipt = {
        **plan,
        "applied_at": catalog_core.utc_now(),
        "actor": actor,
        "manifest_backup": str(manifest_backup),
        "campaign_backup": str(database_backup),
        "accepted_catalog_values_changed": False,
    }
    (audit_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return {
        "result": "ok",
        "matched": len(plan["matched"]),
        "not_found": plan["not_found"],
        "audit": str(audit_dir),
        "manifest_backup": str(manifest_backup),
        "campaign_backup": str(database_backup),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--inventory", action="append", default=[])
    parser.add_argument("--actor", default="")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    plan = build_plan(
        arguments.root,
        arguments.workbook,
        arguments.inventory,
    )
    if not arguments.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not arguments.actor.strip():
        raise WorkbookBackfillError("--actor is required with --apply")
    print(
        json.dumps(
            apply_plan(plan, arguments.actor.strip()),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
