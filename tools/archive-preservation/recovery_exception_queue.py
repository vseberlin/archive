#!/usr/bin/env python3
"""Add audited workbook/TIFF exceptions to a bounded recovery queue."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import reconciliation
from jobs.provenance_restoration import unit_recovery
from workflows import reconcile


FORMAT = "iss-recovery-exceptions-v1"
CAMPAIGN_ID = "MD-RECOVERY-WF-PARAGON"
WORKBOOK_ONLY = {
    "VLV-1004-1",
    "VLV-1013-2",
    "VLV-1050-1",
    "VLV-1053-1",
    "VLV-1092-2",
    "VLV-1100",
    "VLV-1130-5",
    "VLV-1150",
    "VLV-1161",
    "VLV-1162",
    "VLV-1163",
}
CORRECTIONS = {
    "VLV-1112": "A retained lossless TIFF is better than the accessioned JPEG-derived surrogate.",
    "VLV-1004-2": "Embedded preservation metadata omits the accepted 597x280 mm dimensions.",
    "VLV-1131": "The workbook supplies the carrier-level dimension DIN A2, absent from catalog and master.",
    "VLV-1051": "Identity collision: its selected source filename is VLV-1050-1 although the workbook has distinct rows.",
}


class ExceptionQueueError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24].upper()}"


def backup_database(path: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    work_root = path.parent.parent if path.parent.name == "reconciliation" else path.parent
    target = work_root / "backups" / f"{stamp}-{label}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(path)
    backup = sqlite3.connect(target)
    try:
        source.backup(backup)
        backup.commit()
    finally:
        backup.close()
        source.close()
    check = sqlite3.connect(f"file:{target}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExceptionQueueError(f"backup integrity failed: {target}")
    finally:
        check.close()
    return target


def existing_candidates(inventory: str, tiff_rows: dict[str, dict[str, str]]) -> list[Path]:
    candidates: list[Path] = []
    if inventory in tiff_rows:
        candidates.append(Path(tiff_rows[inventory]["intake_path"]))
    roots = (
        Path("/mnt/volume/archive/WF/Paragon fertig für md"),
        Path("/mnt/volume/archive-intake/elements-restore-md-candidates/source_restore_archive/Paragon"),
        Path("/media/vladimir/Elements/restore/archive/WF/Paragon fertig für md"),
        Path("/media/vladimir/Elements/restore/archive/Paragon"),
    )
    names = {
        f"{inventory}.tif",
        f"{inventory}.tiff",
        f"{inventory}.jpg",
        f"{inventory}.jpeg",
        f"{inventory}.png",
    }
    for root in roots:
        if not root.is_dir():
            continue
        for name in names:
            candidates.extend(root.glob(f"**/{name}"))
    unique: dict[str, Path] = {}
    for path in candidates:
        resolved = path.resolve()
        if resolved.is_file():
            unique.setdefault(sha256_file(resolved), resolved)
    return list(unique.values())


def load_rows(audit_dir: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    with (audit_dir / "metadata-crosscheck.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        metadata = {row["inventory"]: row for row in csv.DictReader(handle)}
    with (audit_dir / "remaining-tiff-crosscheck.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        tiffs = {
            row["inventory_candidate"]: row
            for row in csv.DictReader(handle)
        }
    return metadata, tiffs


def build_manifest(audit_dir: Path) -> dict[str, Any]:
    metadata, tiffs = load_rows(audit_dir)
    tiff_only = {
        inventory
        for inventory, row in tiffs.items()
        if row["workbook_record"] == "false"
    }
    inventories = sorted(WORKBOOK_ONLY | tiff_only | set(CORRECTIONS))
    records = []
    for inventory in inventories:
        row = metadata.get(inventory, {})
        categories = []
        if inventory in WORKBOOK_ONLY:
            categories.append("workbook_without_active_representation")
        if inventory in tiff_only:
            categories.append("tiff_without_workbook_record")
        if inventory in CORRECTIONS:
            categories.append("represented_record_correction")
        files = []
        for source in existing_candidates(inventory, tiffs):
            files.append(
                {
                    "path": str(source),
                    "size_bytes": source.stat().st_size,
                    "sha256": sha256_file(source),
                }
            )
        records.append(
            {
                "inventory": inventory,
                "categories": categories,
                "correction_required": inventory in CORRECTIONS,
                "reason": CORRECTIONS.get(
                    inventory,
                    "Audited local workbook or TIFF evidence is not represented in the active package.",
                ),
                "local": {
                    "title": row.get("workbook_title", ""),
                    "description": row.get("workbook_description", ""),
                    "material_technique": row.get("workbook_material", ""),
                    "dimensions": row.get("workbook_dimensions", ""),
                    "object_type": "",
                },
                "files": files,
            }
        )
    return {
        "format": FORMAT,
        "created_at": catalog_core.utc_now(),
        "audit_dir": str(audit_dir),
        "records": records,
        "summary": {
            "records": len(records),
            "workbook_only": len(WORKBOOK_ONLY),
            "tiff_only": len(tiff_only),
            "corrections": len(CORRECTIONS),
        },
    }


def add_campaign_record(
    root: Path,
    campaign: sqlite3.Connection,
    entity_id: str,
    item: dict[str, Any],
    manifest_path: Path,
) -> bool:
    inventory = str(item["inventory"])
    if campaign.execute(
        """SELECT 1 FROM reconciliation_records
           WHERE campaign_id=? AND inventory_label=?""",
        (CAMPAIGN_ID, inventory),
    ).fetchone():
        return False
    now = catalog_core.utc_now()
    record_id = stable_id("REC", CAMPAIGN_ID, entity_id)
    campaign.execute(
        """INSERT INTO reconciliation_records (
               record_id, campaign_id, source_record_key, entity_id,
               local_archival_id, inventory_label, external_system,
               external_id, external_url, source_record_status, match_status,
               identity_status, field_status, queue_status, captured_at,
               snapshot_digest, snapshot_relpath, source_record_locator,
               note, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'museum-digital', '', '',
                     ?, 'not_found', 'blocked', 'blocked', 'pending', ?,
                     '', '', ?, ?, ?, ?)""",
        (
            record_id,
            CAMPAIGN_ID,
            entity_id,
            entity_id,
            inventory,
            inventory,
            "exact_inventory" if item["local"].get("title") else "not_supplied",
            now,
            str(manifest_path),
            str(item["reason"]),
            now,
            now,
        ),
    )
    local = item["local"]
    fields = (
        (
            "archive_descriptive_title",
            "Descriptive title",
            "descriptive_title",
            "title",
        ),
        (
            "archive_description",
            "Description",
            "description",
            "description",
        ),
        (
            "archive_material_technique",
            "Material / technique",
            "material_technique",
            "material_technique",
        ),
        (
            "archive_dimensions",
            "Dimensions",
            "dimensions",
            "dimensions",
        ),
    )
    catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    try:
        archive = catalog.execute(
            "SELECT * FROM archive_objects WHERE entity_id=?", (entity_id,)
        ).fetchone()
        for field_key, label, catalog_key, local_key in fields:
            for side, value, locator in (
                ("catalog", str(archive[catalog_key] or ""), ""),
                ("local", str(local.get(local_key) or ""), str(manifest_path)),
                ("external", "", ""),
            ):
                campaign.execute(
                    """INSERT INTO field_candidates (
                           candidate_id, record_id, field_key, field_label,
                           source_side, value_text, source_locator,
                           snapshot_digest, status
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)""",
                    (
                        stable_id("CAND", record_id, field_key, side),
                        record_id,
                        field_key,
                        label,
                        side,
                        value,
                        locator,
                        "available" if value else "missing",
                    ),
                )
    finally:
        catalog.close()
    for source_index, source_item in enumerate(item.get("files") or [], start=1):
        source = Path(source_item["path"]).resolve(strict=True)
        evidence_id = stable_id("VIS", record_id, str(source))
        cache_dir = (
            root
            / "work"
            / "reconciliation"
            / "cache"
            / CAMPAIGN_ID
            / record_id
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        preview = cache_dir / f"{source_index:02d}-{source.stem}.jpg"
        ok, detail = reconcile.create_visual_preview(source, preview)
        width, height = reconcile.image_dimensions(source)
        campaign.execute(
            """INSERT INTO visual_evidence (
                   evidence_id, record_id, role, label, cache_relpath,
                   source_locator, content_digest, media_type, width_px,
                   height_px, status, detail
               ) VALUES (?, ?, 'source_record_image', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id,
                record_id,
                f"Audited local candidate: {source.name}",
                preview.relative_to(root).as_posix() if ok else "",
                str(source),
                "sha256:" + str(source_item["sha256"]),
                mimetypes.guess_type(source.name)[0] or "",
                int(width) if str(width).isdigit() else None,
                int(height) if str(height).isdigit() else None,
                "available" if ok else "preview_unavailable",
                detail,
            ),
        )
    return True


def apply(root: Path, audit_dir: Path, actor: str) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    catalog_core.require_core_mode(root)
    manifest = build_manifest(audit_dir.resolve(strict=True))
    recovery_dir = root / "work" / "recovery" / "WF-PARAGON"
    manifest_path = recovery_dir / "reconciliation-exceptions.json"
    if manifest_path.exists():
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
        current.pop("created_at", None)
        comparison = dict(manifest)
        comparison.pop("created_at", None)
        if current != comparison:
            raise ExceptionQueueError("existing exception manifest differs")
    catalog_path = root / "work" / "catalog.sqlite"
    campaign_path = root / "work" / "reconciliation" / "museum-digital.sqlite"
    backups = [
        str(backup_database(catalog_path, "PRE-RECOVERY-EXCEPTIONS-CATALOG")),
        str(backup_database(campaign_path, "PRE-RECOVERY-EXCEPTIONS-CAMPAIGN")),
    ]
    catalog = catalog_core.connect_catalog(catalog_path)
    created_entities = 0
    entity_by_inventory: dict[str, str] = {}
    try:
        for item in manifest["records"]:
            entity_id, created = unit_recovery._ensure_entity(
                catalog,
                unit="WF-PARAGON",
                inventory=str(item["inventory"]),
                actor=actor,
            )
            entity_by_inventory[str(item["inventory"])] = entity_id
            created_entities += int(created)
        catalog_core.assert_catalog_integrity(catalog)
    finally:
        catalog.close()
    campaign = reconciliation.connect_campaign(root)
    inserted_records = 0
    try:
        campaign.execute("BEGIN IMMEDIATE")
        for item in manifest["records"]:
            inserted_records += int(
                add_campaign_record(
                    root,
                    campaign,
                    entity_by_inventory[str(item["inventory"])],
                    item,
                    manifest_path,
                )
            )
        campaign.commit()
        if campaign.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExceptionQueueError("campaign integrity check failed")
    except Exception:
        campaign.rollback()
        raise
    finally:
        campaign.close()
    temporary = manifest_path.with_name(
        f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return {
        "result": "ok",
        "manifest": str(manifest_path),
        "records": len(manifest["records"]),
        "created_entities": created_entities,
        "inserted_campaign_records": inserted_records,
        "backups": backups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps(build_manifest(args.audit_dir.resolve(strict=True)), indent=2))
        return 0
    print(json.dumps(apply(args.root, args.audit_dir, args.actor), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
