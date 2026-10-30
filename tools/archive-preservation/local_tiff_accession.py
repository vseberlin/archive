#!/usr/bin/env python3
"""Accession explicit local Paragon TIFF masters with workbook metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import tiff_accession
import tiff_metadata
from archive_core.catalog import write_unit_catalog
from archive_core.layout import object_relative_path
from jobs.provenance_restoration import unit_recovery
from jobs.provenance_restoration.technical import verify_image_derivative
from recovery_workbook_backfill import sha256_file, workbook_rows
from workflows import editor_reconcile, recovery_accession, recovery_metadata


FORMAT = "iss-local-tiff-accession-plan-v1"
RESULT_FORMAT = "iss-local-tiff-accession-result-v1"
TOOL_VERSION = "local-tiff-accession-1.0.0"
UNIT_ID = "WF-PARAGON"


class LocalTiffAccessionError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def _payload_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _inventory_from_source(path: Path) -> str:
    stem = path.stem.strip().upper()
    if not stem.startswith("VLV-"):
        raise LocalTiffAccessionError(
            f"source filename does not look like a VLV inventory: {path.name}"
        )
    return stem


def _workbook_values(row: dict[str, Any]) -> dict[str, Any]:
    values = row["values"]
    tags = [
        str(values.get(f"tag{index}", "") or "").strip()
        for index in range(1, 11)
        if str(values.get(f"tag{index}", "") or "").strip()
    ]
    if "Paragon" not in {tag.casefold(): tag for tag in tags}:
        tags.append("Paragon")
    return {
        "title": str(values.get("object_title") or "").strip(),
        "description": str(values.get("object_description") or "").strip(),
        "material": str(values.get("object_material_technique") or "").strip(),
        "dimensions": str(values.get("object_dimensions") or "").strip(),
        "object_type": str(values.get("object_type") or "").strip(),
        "tags": tags,
    }


def _embedding_metadata(inventory: str) -> dict[str, str]:
    metadata = dict(recovery_metadata.require_embedding_metadata(inventory))
    date_created = metadata.get("date_created", "")
    if date_created and not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", date_created):
        metadata.pop("date_created", None)
        certainty = metadata.get("certainty_context", "")
        note = f"date_created_not_embedded:{date_created}"
        metadata["certainty_context"] = (
            f"{certainty}, {note}" if certainty else note
        )
    return metadata


def _backup_database(source: Path, destination: Path) -> None:
    old = sqlite3.connect(source)
    backup = sqlite3.connect(destination)
    try:
        old.backup(backup)
        backup.commit()
    finally:
        backup.close()
        old.close()
    check = sqlite3.connect(
        f"file:{destination}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LocalTiffAccessionError(
                f"database backup failed integrity check: {destination}"
            )
    finally:
        check.close()


def _active_roles(catalog: sqlite3.Connection, entity_id: str) -> list[str]:
    return [
        str(row["role"])
        for row in catalog.execute(
            """SELECT ef.role
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               WHERE ef.entity_id=? AND ef.status='active'
                 AND fo.status='verified'
               ORDER BY ef.role""",
            (entity_id,),
        )
    ]


def build_plan(
    root: Path,
    workbook: Path,
    sources: list[Path],
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    workbook = workbook.resolve(strict=True)
    workbook_digest = sha256_file(workbook)
    sheet, rows = workbook_rows(workbook)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as catalog:
        catalog_core.assert_catalog_integrity(catalog)
        for raw_source in sources:
            source = raw_source.resolve(strict=True)
            if not source.is_file() or source.suffix.casefold() not in {".tif", ".tiff"}:
                raise LocalTiffAccessionError(f"source is not a TIFF file: {source}")
            inventory = _inventory_from_source(source)
            if inventory in seen:
                raise LocalTiffAccessionError(f"duplicate inventory: {inventory}")
            seen.add(inventory)
            row = rows.get(inventory)
            if not row:
                raise LocalTiffAccessionError(
                    f"workbook row is missing for {inventory}"
                )
            values = _workbook_values(row)
            missing = [
                key for key in ("title", "description", "material", "dimensions")
                if not values[key]
            ]
            if missing:
                raise LocalTiffAccessionError(
                    f"workbook metadata is incomplete for {inventory}: "
                    + ", ".join(missing)
                )
            entity = catalog.execute(
                """SELECT entity_id FROM archive_objects
                   WHERE archival_label=? AND status='active'""",
                (inventory,),
            ).fetchone()
            active_roles: list[str] = []
            entity_id = ""
            if entity:
                entity_id = str(entity["entity_id"])
                active_roles = _active_roles(catalog, entity_id)
                if active_roles:
                    raise LocalTiffAccessionError(
                        f"{inventory} already has active representations: "
                        + ", ".join(active_roles)
                    )
            technical = tiff_accession._exif_values(
                source,
                tiff_accession.TECHNICAL_TAGS,
            )
            if technical.get("File:FileType") != "TIFF":
                raise LocalTiffAccessionError(
                    f"source is not structurally TIFF: {source}"
                )
            item = {
                "inventory": inventory,
                "entity_id": entity_id,
                "source": str(source),
                "source_digest": _digest(source),
                "source_size_bytes": source.stat().st_size,
                "source_width_px": int(technical["IFD0:ImageWidth"]),
                "source_height_px": int(technical["IFD0:ImageHeight"]),
                "workbook": str(workbook),
                "workbook_sha256": "sha256:" + workbook_digest,
                "workbook_sheet": sheet,
                "workbook_row": int(row["row"]),
                "metadata": values,
                "embedding_metadata": _embedding_metadata(inventory),
                "embedding_profile": tiff_metadata.PROFILE_V2,
                "embedding_mapping": tiff_metadata.V2_MAPPING_VERSION,
            }
            item["item_digest"] = _payload_digest(item)
            items.append(item)
    plan = {
        "format": FORMAT,
        "root": str(root),
        "workbook": str(workbook),
        "count": len(items),
        "items": items,
    }
    plan["plan_digest"] = _payload_digest(items)
    return plan


def _ensure_metadata(
    root: Path,
    item: dict[str, Any],
    *,
    actor: str,
    reason: str,
) -> str:
    inventory = str(item["inventory"])
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite")) as catalog:
        entity_id, _created = unit_recovery._ensure_entity(
            catalog,
            unit=UNIT_ID,
            inventory=inventory,
            actor=actor,
        )
    metadata = item["metadata"]
    field_decisions = {
        "title": {"source": "local", "value": metadata["title"]},
        "description": {"source": "local", "value": metadata["description"]},
        "material": {"source": "local", "value": metadata["material"]},
        "dimensions": {"source": "local", "value": metadata["dimensions"]},
        "object_type": {"source": "local", "value": metadata["object_type"]},
    }
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite")) as catalog:
        editor_reconcile.reconcile_metadata(
            catalog,
            unit_id=UNIT_ID,
            entity_id=entity_id,
            field_decisions=field_decisions,
            tag_decision={"source": "local", "values": metadata["tags"]},
            actor=actor,
            reason=reason,
            evidence=(
                f"{item['workbook']}#{item['workbook_sheet']}!"
                f"row={item['workbook_row']}; {item['workbook_sha256']}"
            ),
            external_id="",
        )
    return entity_id


def _observed_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace(
        "+00:00",
        "Z",
    )


def _register_generated_derivative(
    root: Path,
    independent: Path,
    *,
    inventory: str,
    entity_id: str,
    source: Path,
    master_digest: str,
    width_px: int,
    height_px: int,
    actor: str,
) -> dict[str, Any]:
    relative = Path(*object_relative_path(
        unit_id=UNIT_ID,
        inventory=inventory,
        entity_id=entity_id,
        role="access_derivative",
        suffix=".jpg",
    ).parts)
    primary_copy = root / relative
    independent_copy = independent / relative
    generated_digest, technical = recovery_accession._ensure_generated_access_jpeg(
        source,
        primary_copy,
        width_px=width_px,
        height_px=height_px,
        scratch=root / "work" / "accession-staging" / entity_id,
    )
    recovery_accession._ensure_exact_copy(
        primary_copy,
        independent_copy,
        generated_digest,
    )
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite")) as catalog:
        return catalog_core.register_access_derivative(
            catalog,
            entity_id=entity_id,
            unit_id=UNIT_ID,
            content_digest=generated_digest,
            size_bytes=primary_copy.stat().st_size,
            relative_path=relative.as_posix(),
            original_filename=relative.name,
            observed_mtime_utc=_observed_mtime(primary_copy),
            media_type=mimetypes.guess_type(relative.name)[0] or "image/jpeg",
            width_px=int(technical["width"]),
            height_px=int(technical["height"]),
            master_digest=master_digest,
            museum_digital_id="",
            museum_digital_filename="",
            museum_digital_url="",
            actor=actor,
        )


def apply_item(
    root: Path,
    item: dict[str, Any],
    *,
    actor: str,
    reason: str,
    expected_item_digest: str,
) -> dict[str, Any]:
    if item["item_digest"] != expected_item_digest:
        raise LocalTiffAccessionError(f"{item['inventory']} plan changed")
    config = catalog_core.require_core_mode(root)
    independent = catalog_core.assert_safe_work_root(
        Path(str(config["independent_generation_root"])).resolve(strict=True)
    )
    catalog_core.assert_independent_device(root, independent)
    source = Path(str(item["source"])).resolve(strict=True)
    if _digest(source) != item["source_digest"]:
        raise LocalTiffAccessionError(f"{item['inventory']} source digest changed")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / "work" / "backups" / (
        f"{stamp}-PRE-LOCAL-TIFF-ACCESSION-{item['inventory']}"
    )
    backup_dir.mkdir(parents=True)
    _backup_database(root / "work" / "catalog.sqlite", backup_dir / "catalog.sqlite")
    campaign_path = root / "work" / "reconciliation" / "museum-digital.sqlite"
    if campaign_path.is_file():
        _backup_database(campaign_path, backup_dir / "museum-digital.sqlite")
    (backup_dir / "plan-item.json").write_text(
        json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    entity_id = _ensure_metadata(root, item, actor=actor, reason=reason)
    publication_before = catalog_core.publish_generation(
        root,
        independent,
        tool_version=TOOL_VERSION,
        actor=actor,
    )
    provenance = {
        "case_id": f"LOCAL-TIFF-WF-PARAGON-{item['inventory']}",
        "source_file_id": str(source),
        "source_digest": item["source_digest"],
        "reviewed_bundle_digest": item["item_digest"],
        "museum_digital_id": "",
        "representation_role": "preservation_master",
        "provenance_gaps": [],
    }
    provenance.update(_embedding_metadata(item["inventory"]))
    master = tiff_accession.create_preservation_master(
        root,
        source,
        entity_id,
        actor,
        embedding_profile=tiff_metadata.PROFILE_V2,
        provenance_metadata=provenance,
    )
    derivative = _register_generated_derivative(
        root,
        independent,
        inventory=item["inventory"],
        entity_id=entity_id,
        source=source,
        master_digest=str(master["content_digest"]),
        width_px=int(item["source_width_px"]),
        height_px=int(item["source_height_px"]),
        actor=actor,
    )
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite")) as catalog:
        catalog_core.record_recovery_accession_provenance(
            catalog,
            entity_id=entity_id,
            unit_id=UNIT_ID,
            selection_digest=item["item_digest"],
            master_digest=str(master["content_digest"]),
            derivative_digest=str(derivative["content_digest"]),
            museum_digital_id="",
            actor=actor,
            reason=reason,
        )
    publication_after = catalog_core.publish_generation(
        root,
        independent,
        tool_version=TOOL_VERSION,
        actor=actor,
    )
    with closing(catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)) as catalog:
        metadata = tiff_accession._catalog_metadata(
            catalog,
            entity_id,
            tiff_metadata.PROFILE_V2,
        )
        expected = tiff_accession._expected_embedded(
            metadata,
            tiff_metadata.PROFILE_V2,
            provenance,
        )
        if (
            tiff_accession._exif_values(Path(str(master["master"])), tuple(expected))
            != expected
        ):
            raise LocalTiffAccessionError(
                f"{item['inventory']} embedded metadata read-back differs"
            )
        primary_catalog = write_unit_catalog(
            catalog,
            UNIT_ID,
            root / "units" / UNIT_ID,
        )
        independent_catalog = write_unit_catalog(
            catalog,
            UNIT_ID,
            independent / "units" / UNIT_ID,
        )
    if (
        primary_catalog["csv_sha256"] != independent_catalog["csv_sha256"]
        or primary_catalog["xlsx_sha256"] != independent_catalog["xlsx_sha256"]
    ):
        raise LocalTiffAccessionError(
            f"{item['inventory']} unit catalogs differ after accession"
        )
    return {
        "inventory": item["inventory"],
        "entity_id": entity_id,
        "backup": str(backup_dir),
        "metadata_generation": publication_before["generation_id"],
        "generation_id": publication_after["generation_id"],
        "master_digest": master["content_digest"],
        "derivative_digest": derivative["content_digest"],
    }


def run(
    root: Path,
    workbook: Path,
    sources: list[Path],
    *,
    actor: str,
    reason: str,
    expected_plan_digest: str,
) -> dict[str, Any]:
    plan = build_plan(root, workbook, sources)
    if plan["plan_digest"] != expected_plan_digest:
        raise LocalTiffAccessionError(
            "local TIFF accession plan changed; review a new dry-run"
        )
    root = Path(plan["root"])
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for item in plan["items"]:
        try:
            result = apply_item(
                root,
                item,
                actor=actor,
                reason=reason,
                expected_item_digest=str(item["item_digest"]),
            )
            successes.append(result)
            print(
                f"accessioned {item['inventory']} as {result['generation_id']}",
                flush=True,
            )
        except (
            LocalTiffAccessionError,
            catalog_core.CoreCatalogError,
            tiff_accession.TiffAccessionError,
            recovery_accession.RecoveryAccessionError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as error:
            failures.append({"inventory": str(item["inventory"]), "error": str(error)})
            print(f"failed {item['inventory']}: {error}", flush=True)
            break
    return {
        "format": RESULT_FORMAT,
        "plan_digest": plan["plan_digest"],
        "planned": plan["count"],
        "succeeded": successes,
        "failed": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--source", action="append", required=True, type=Path)
    parser.add_argument("--actor", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--plan-digest", default="")
    parser.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.dry_run:
            plan = build_plan(arguments.root, arguments.workbook, arguments.source)
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if not arguments.plan_digest:
            raise LocalTiffAccessionError(
                "--run requires --plan-digest from a reviewed dry-run"
            )
        report = run(
            arguments.root,
            arguments.workbook,
            arguments.source,
            actor=arguments.actor,
            reason=arguments.reason
            or f"Bound local TIFF accession {arguments.plan_digest}",
            expected_plan_digest=arguments.plan_digest,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if report["failed"] else 0
    except (
        LocalTiffAccessionError,
        catalog_core.CoreCatalogError,
        tiff_accession.TiffAccessionError,
        recovery_accession.RecoveryAccessionError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    raise SystemExit(main())
