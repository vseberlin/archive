#!/usr/bin/env python3
"""Bootstrap and enrich one bounded semi-real provenance recovery unit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
import catalog_core
import reconciliation
from workflows import editor_reconcile, reconcile


FORMAT = "iss-bounded-unit-recovery-v1"
SUPPLEMENTAL_FORMAT = "iss-bounded-unit-recovery-supplement-v1"
TOOL_VERSION = "bounded-unit-recovery-0.2.0"
PROTECTED_ROOTS = (Path("/mnt/volume/archive"), Path("/mnt/volume/archive-intake"))


class UnitRecoveryError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def protected_source(value: Path) -> Path:
    source = value.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise UnitRecoveryError(f"source scope is not a directory: {source}")
    if not any(source != root.resolve() and source.is_relative_to(root.resolve()) for root in PROTECTED_ROOTS):
        raise UnitRecoveryError("source scope must be inside a protected evidence root")
    return source


def recovery_dir(root: Path, unit: str) -> Path:
    resolved = catalog_core.assert_safe_work_root(root.expanduser().resolve(strict=True))
    catalog_core.require_core_mode(resolved)
    if not unit or Path(unit).name != unit:
        raise UnitRecoveryError("unit identifier is unsafe")
    return resolved / "work" / "recovery" / unit


def _supplement_path(destination: Path, source: Path) -> Path:
    token = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return destination / "supplemental-sources" / f"source-{token}.json"


def capture_supplemental_source(
    *,
    root: Path,
    unit: str,
    source_root: Path,
) -> dict[str, Any]:
    """Freeze one additive read-only TIFF source without replacing base scope."""

    destination = recovery_dir(root, unit)
    scope = json.loads((destination / "scope.json").read_text(encoding="utf-8"))
    if scope.get("format") != FORMAT:
        raise UnitRecoveryError("bounded recovery scope is invalid")
    source = protected_source(source_root)
    manifest_path = _supplement_path(destination, source)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("format") != SUPPLEMENTAL_FORMAT
            or manifest.get("source", {}).get("root") != str(source)
        ):
            raise UnitRecoveryError("supplemental recovery source is invalid")
        return {
            "result": "ok",
            "state": "unchanged",
            "manifest": str(manifest_path),
            "files": len(manifest.get("files") or []),
        }
    before = source.stat()
    inventories = set(str(value) for value in scope.get("inventories") or [])
    files: list[dict[str, Any]] = []
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".tif", ".tiff"}:
            continue
        stat = path.stat()
        candidate = path.stem
        files.append(
            {
                "relative_path": path.relative_to(source).as_posix(),
                "inventory_candidate": candidate,
                "linked_inventory": candidate if candidate in inventories else "",
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256_file(path),
            }
        )
    after = source.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise UnitRecoveryError("supplemental source changed during capture")
    manifest = {
        "format": SUPPLEMENTAL_FORMAT,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "unit_id": unit,
        "source": {
            "root": str(source),
            "device": before.st_dev,
            "inode": before.st_ino,
            "size_bytes": before.st_size,
            "mtime_ns": before.st_mtime_ns,
        },
        "summary": {
            "files": len(files),
            "bytes": sum(int(item["size_bytes"]) for item in files),
            "linked": sum(bool(item["linked_inventory"]) for item in files),
            "unlinked": sum(not item["linked_inventory"] for item in files),
        },
        "files": files,
    }
    _atomic_new(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return {
        "result": "ok",
        "state": "captured",
        "manifest": str(manifest_path),
        **manifest["summary"],
    }


def supplemental_sources(destination: Path) -> list[dict[str, Any]]:
    """Read and verify every frozen additive source manifest."""

    manifests: list[dict[str, Any]] = []
    directory = destination / "supplemental-sources"
    if not directory.is_dir():
        return manifests
    for path in sorted(directory.glob("source-*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("format") != SUPPLEMENTAL_FORMAT:
            raise UnitRecoveryError(f"unknown supplemental source format: {path}")
        source = protected_source(Path(str(manifest["source"]["root"])))
        stat = source.stat()
        expected = manifest["source"]
        # st_dev is a runtime mount identifier and may change after a clean
        # remount. Bind the persistent directory identity and every captured
        # TIFF fingerprint instead of treating a remount as evidence loss.
        if (
            stat.st_ino != int(expected["inode"])
            or stat.st_size != int(expected["size_bytes"])
            or stat.st_mtime_ns != int(expected["mtime_ns"])
        ):
            raise UnitRecoveryError(
                f"supplemental source root changed after capture: {source}"
            )
        expected_files = {
            str(item["relative_path"]): item for item in manifest["files"]
        }
        observed_files = {
            path.relative_to(source).as_posix(): path
            for path in source.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() in {".tif", ".tiff"}
        }
        if set(observed_files) != set(expected_files):
            raise UnitRecoveryError(
                f"supplemental source file set changed after capture: {source}"
            )
        for relative, file_path in observed_files.items():
            file_stat = file_path.stat()
            file_expected = expected_files[relative]
            if (
                file_stat.st_size != int(file_expected["size_bytes"])
                or file_stat.st_mtime_ns != int(file_expected["mtime_ns"])
            ):
                raise UnitRecoveryError(
                    "supplemental source file changed after capture: "
                    f"{file_path}"
                )
        manifests.append(manifest)
    return manifests


def _atomic_new(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise UnitRecoveryError(f"refusing to replace recovery evidence: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Replace one rebuildable campaign projection atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _existing_entity(catalog: sqlite3.Connection, inventory: str) -> str:
    matches = {
        str(row["entity_id"])
        for row in catalog.execute(
            """SELECT entity_id FROM entity_identifiers
               WHERE namespace = 'inventory' AND normalized_value = ?
                 AND status = 'active'""",
            (inventory.casefold(),),
        )
    }
    matches.update(
        str(row["entity_id"])
        for row in catalog.execute(
            """SELECT entity_id FROM archive_objects
               WHERE archival_label = ? AND status = 'active'""",
            (inventory,),
        )
    )
    if len(matches) > 1:
        raise UnitRecoveryError(
            f"inventory resolves to multiple core entities: {inventory}"
        )
    return next(iter(matches), "")


def _ensure_entity(
    catalog: sqlite3.Connection,
    *,
    unit: str,
    inventory: str,
    actor: str,
) -> tuple[str, bool]:
    entity_id = _existing_entity(catalog, inventory)
    if entity_id:
        return entity_id, False
    created = catalog_core.create_entity(
        catalog,
        kind="archive_object",
        canonical_name=inventory,
        unit_id=unit,
        membership_role="primary",
        actor=actor,
        assignment_year=datetime.now(UTC).year,
        domain={
            "archival_label": inventory,
            "publication_status": "blocked",
            "note": (
                "Provisional provenance-recovery identity; legacy and "
                "museum-digital metadata remain unaccepted candidates."
            ),
        },
        note="Bounded recovery bootstrap; no descriptive metadata accepted.",
    )
    entity_id = str(created["entity_id"])
    catalog_core.add_alias_identifier(
        catalog,
        entity_id=entity_id,
        namespace="inventory",
        value=inventory,
        normalized_value=inventory.casefold(),
        role="legacy",
        actor=actor,
        note="Exact inventory identity from the bounded retained-index scope.",
    )
    return entity_id, True


def _md_tag_values(md: dict[str, Any]) -> list[str]:
    tags = md.get("object_tags")
    if not isinstance(tags, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in tags:
        value = str(
            item.get("tag_name") if isinstance(item, dict) else item
        ).strip()
        folded = value.casefold()
        if value and folded not in seen:
            seen.add(folded)
            values.append(value)
    return values


def _md_tags(md: dict[str, Any]) -> str:
    return "; ".join(_md_tag_values(md))


def prepare_reconciliation(
    *,
    root: Path,
    unit: str,
    actor: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Prepare one writable campaign without promoting candidate metadata.

    Protected source files and the retained index are opened read-only. The
    generated comparison run and captured museum-digital assets live below the
    writable recovery root and are replaceable operational projections.
    """

    actor = actor.strip()
    if not actor:
        raise UnitRecoveryError("named actor is required")
    destination = recovery_dir(root, unit)
    scope_path = destination / "scope.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    if scope.get("format") != FORMAT or scope.get("unit_id") != unit:
        raise UnitRecoveryError("bounded recovery scope is invalid")
    source_root = protected_source(Path(str(scope["source"]["root"])))
    retained = Path(str(scope["retained_index"]["path"])).resolve(strict=True)
    if (
        retained.stat().st_size != int(scope["retained_index"]["size_bytes"])
        or retained.stat().st_mtime_ns != int(scope["retained_index"]["mtime_ns"])
    ):
        raise UnitRecoveryError("retained metadata index changed since scope capture")
    scope_files = {
        str(item["relative_path"]): item for item in scope["files"]
    }
    supplements = supplemental_sources(destination)
    supplemental_by_inventory: dict[str, list[dict[str, Any]]] = {}
    for manifest in supplements:
        supplemental_root = Path(str(manifest["source"]["root"])).resolve(strict=True)
        for item in manifest["files"]:
            enriched = {**item, "source_root": str(supplemental_root)}
            linked = str(item.get("linked_inventory") or "")
            if linked:
                supplemental_by_inventory.setdefault(linked, []).append(enriched)
    run = destination / "reconciliation-run"
    visuals_dir = run / "visual"
    raw_dir = run / "raw"
    md_images_dir = run / "visual" / "museum-digital-evidence"
    visuals_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    md_images_dir.mkdir(parents=True, exist_ok=True)

    retained_db = sqlite3.connect(
        f"file:{retained.as_posix()}?mode=ro&immutable=1", uri=True
    )
    retained_db.row_factory = sqlite3.Row
    catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    created_entities = 0
    captured_images = 0
    reused_images = 0
    preview_failures = 0
    candidate_rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    visuals: list[dict[str, Any]] = []
    supplemental_visuals: list[dict[str, Any]] = []
    entity_by_inventory: dict[str, str] = {}
    try:
        for inventory in scope["inventories"]:
            entity_id, created = _ensure_entity(
                catalog,
                unit=unit,
                inventory=str(inventory),
                actor=actor,
            )
            entity_by_inventory[str(inventory)] = entity_id
            created_entities += int(created)

        links_by_path: dict[str, list[sqlite3.Row]] = {}
        for row in retained_db.execute(
            """SELECT f.path, f.basename, f.size, f.mtime_ns, f.extension,
                      l.inventory_number, l.link_type, l.confidence
               FROM files f LEFT JOIN file_object_links l ON l.file_id = f.id
               WHERE f.path LIKE ? ORDER BY f.path, l.inventory_number""",
            (str(source_root) + "/%",),
        ):
            links_by_path.setdefault(str(row["path"]), []).append(row)
        for relative, item in sorted(scope_files.items()):
            absolute = str(source_root / relative)
            links = links_by_path.get(absolute) or [None]
            for link in links:
                candidate_rows.append(
                    {
                        "inventory": str(link["inventory_number"] or "") if link else "",
                        "relative_path": relative,
                        "size_bytes": item["size_bytes"],
                        "mtime_ns": item["mtime_ns"],
                        "sha256": item["sha256"],
                        "extension": Path(relative).suffix.casefold().lstrip("."),
                        "link_type": str(link["link_type"] or "") if link else "",
                        "confidence": str(link["confidence"] or "") if link else "",
                        "candidate_state": "unreviewed",
                    }
                )
        for manifest in supplements:
            supplemental_root = Path(str(manifest["source"]["root"])).resolve(
                strict=True
            )
            for item in manifest["files"]:
                candidate_rows.append(
                    {
                        "inventory": str(item.get("linked_inventory") or ""),
                        "relative_path": (
                            f"supplemental:{supplemental_root.name}/"
                            f"{item['relative_path']}"
                        ),
                        "size_bytes": item["size_bytes"],
                        "mtime_ns": item["mtime_ns"],
                        "sha256": item["sha256"],
                        "extension": Path(str(item["relative_path"])).suffix.casefold().lstrip("."),
                        "link_type": (
                            "exact_filename_inventory"
                            if item.get("linked_inventory")
                            else "unresolved_filename_inventory"
                        ),
                        "confidence": "100" if item.get("linked_inventory") else "",
                        "candidate_state": "unreviewed",
                    }
                )

        for inventory in scope["inventories"]:
            inventory = str(inventory)
            entity_id = entity_by_inventory[inventory]
            local_row = retained_db.execute(
                "SELECT * FROM objects WHERE inventory_number = ?",
                (inventory,),
            ).fetchone()
            local = dict(local_row) if local_row else {}
            terminal_path = (
                destination
                / "museum-digital"
                / "objects"
                / f"{urllib.parse.quote(inventory, safe='-_.')}.json"
            )
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            md = (
                terminal.get("exact_objects", [{}])[0]
                if terminal.get("state") == "found"
                else {}
            )
            md_id = str(md.get("object_id") or "")
            if md_id:
                raw_path = raw_dir / f"{md_id}.json"
                raw_payload = json.dumps(
                    md, ensure_ascii=False, sort_keys=True, indent=2
                ) + "\n"
                if not raw_path.exists():
                    _atomic_new(raw_path, raw_payload)
                elif raw_path.read_text(encoding="utf-8") != raw_payload:
                    raise UnitRecoveryError(
                        f"captured MD object changed for {inventory}"
                    )
            records.append(
                {
                    "local_archival_id": entity_id,
                    "local_archival_label": inventory,
                    "local_inventory": inventory,
                    "museum_digital_id": md_id,
                    "museum_digital_url": (
                        reconcile.MUSEUM_DIGITAL_OBJECT_URL.format(object_id=md_id)
                        if md_id
                        else ""
                    ),
                    "match_status": "exact_inventory" if md_id else "not_found",
                    "source_record_status": "exact_inventory",
                    "source_record_file": Path(str(local.get("preferred_source") or "")).name,
                    "source_record_sheet": "",
                    "source_record_row": "",
                    "source_title": str(local.get("title") or ""),
                    "museum_digital_title": str(md.get("object_name") or ""),
                    "source_description": str(local.get("description") or ""),
                    "museum_digital_description": str(md.get("object_description") or ""),
                    "source_material_technique": "",
                    "museum_digital_material_technique": str(md.get("object_material_technique") or ""),
                    "source_dimensions": "",
                    "museum_digital_dimensions": str(md.get("object_dimensions") or ""),
                    "local_object_type": str(local.get("object_type") or ""),
                    "source_object_type": str(local.get("object_type") or ""),
                    "museum_digital_object_type": str(md.get("object_type") or ""),
                    "source_date": "",
                    "museum_digital_event_times": reconcile.museum_digital_event_summary(
                        md.get("object_events")
                    ),
                    "source_image_rights": "",
                    "museum_digital_primary_image_rights": str(
                        (reconcile.museum_digital_main_image(md) or {}).get("rights") or ""
                    ),
                    "museum_digital_tags": _md_tags(md),
                }
            )
            local_files = retained_db.execute(
                """SELECT f.path, f.basename, f.size, f.extension
                   FROM file_object_links l JOIN files f ON f.id = l.file_id
                   WHERE l.inventory_number = ? AND f.path LIKE ?
                     AND lower(f.extension) IN ('jpg','jpeg','png','webp','tif','tiff')
                   ORDER BY CASE lower(f.extension)
                              WHEN 'tif' THEN 0 WHEN 'tiff' THEN 0 ELSE 1 END,
                            f.path""",
                (inventory, str(source_root) + "/%"),
            ).fetchall()
            for index, file_row in enumerate(local_files, start=1):
                source = Path(str(file_row["path"])).resolve(strict=True)
                if not source.is_relative_to(source_root):
                    raise UnitRecoveryError("local visual escaped protected scope")
                relative = source.relative_to(source_root).as_posix()
                scoped = scope_files.get(relative)
                if not scoped or source.stat().st_size != int(scoped["size_bytes"]):
                    raise UnitRecoveryError(f"local candidate changed: {relative}")
                if sha256_file(source) != str(scoped["sha256"]):
                    raise UnitRecoveryError(f"local candidate digest changed: {relative}")
                preview = visuals_dir / f"{entity_id}-local-{index:02d}.jpg"
                if not preview.exists():
                    ok, detail = reconcile.create_visual_preview(source, preview)
                else:
                    ok, detail = True, ""
                preview_failures += int(not ok)
                width, height = reconcile.image_dimensions(source)
                visuals.append(
                    {
                        "image_id": reconcile.visual_image_id(entity_id, "local", relative),
                        "local_archival_id": entity_id,
                        "role": "source_record_image",
                        "label": f"Local candidate: {file_row['basename']}",
                        "original_path": str(source),
                        "snapshot_relpath": preview.relative_to(run).as_posix() if ok else "",
                        "evidence_relpath": "",
                        "sha256": scoped["sha256"],
                        "width": width,
                        "height": height,
                        "status": "available" if ok else "preview_unavailable",
                        "detail": detail,
                    }
                )
            supplemental_start = len(local_files) + 1
            for offset, item in enumerate(
                supplemental_by_inventory.get(inventory, []),
                start=supplemental_start,
            ):
                supplemental_root = Path(str(item["source_root"])).resolve(strict=True)
                source = (
                    supplemental_root / str(item["relative_path"])
                ).resolve(strict=True)
                if not source.is_relative_to(supplemental_root):
                    raise UnitRecoveryError("supplemental visual escaped protected scope")
                stat = source.stat()
                if (
                    stat.st_size != int(item["size_bytes"])
                    or stat.st_mtime_ns != int(item["mtime_ns"])
                    or sha256_file(source) != str(item["sha256"])
                ):
                    raise UnitRecoveryError(
                        f"supplemental candidate changed: {item['relative_path']}"
                    )
                preview = visuals_dir / f"{entity_id}-supplemental-{offset:02d}.jpg"
                if not preview.exists():
                    ok, detail = reconcile.create_visual_preview(source, preview)
                else:
                    ok, detail = True, ""
                preview_failures += int(not ok)
                width, height = reconcile.image_dimensions(source)
                supplemental_visual = {
                        "image_id": reconcile.visual_image_id(
                            entity_id,
                            "supplemental",
                            str(source),
                        ),
                        "local_archival_id": entity_id,
                        "role": "supplemental_source_image",
                        "label": f"Supplemental TIFF candidate: {source.name}",
                        "original_path": str(source),
                        "snapshot_relpath": (
                            preview.relative_to(run).as_posix() if ok else ""
                        ),
                        "evidence_relpath": "",
                        "sha256": item["sha256"],
                        "width": width,
                        "height": height,
                        "status": "available" if ok else "preview_unavailable",
                        "detail": detail,
                    }
                visuals.append(supplemental_visual)
                supplemental_visuals.append(supplemental_visual)
            md_image = reconcile.museum_digital_main_image(md)
            if md_id and md_image:
                filename = Path(str(md_image.get("filename_loc") or "image.jpg")).name
                suffix = Path(filename).suffix.casefold()
                if suffix not in reconcile.VISUAL_IMAGE_SUFFIXES:
                    suffix = ".bin"
                quell_id = str(md_image.get("quell_id") or "main")
                evidence = md_images_dir / f"{inventory}-{quell_id}{suffix}"
                if evidence.exists():
                    fetched, detail = True, "captured previously"
                    reused_images += 1
                else:
                    fetched, detail = reconcile.fetch_external_image(
                        md_image, evidence, timeout
                    )
                    captured_images += int(fetched)
                width, height = reconcile.image_dimensions(evidence) if fetched else ("", "")
                visuals.append(
                    {
                        "image_id": reconcile.visual_image_id(entity_id, "museum-digital", md_id, quell_id),
                        "local_archival_id": entity_id,
                        "role": "museum_digital_snapshot",
                        "label": str(md_image.get("name") or filename),
                        "original_path": detail if fetched else "",
                        "snapshot_relpath": evidence.relative_to(run).as_posix() if fetched else "",
                        "evidence_relpath": "",
                        "sha256": sha256_file(evidence) if fetched else "",
                        "width": width,
                        "height": height,
                        "status": "available" if fetched else "missing",
                        "detail": "" if fetched else detail,
                    }
                )
            elif md_id:
                visuals.append(
                    {
                        "image_id": reconcile.visual_image_id(entity_id, "museum-digital", "missing"),
                        "local_archival_id": entity_id,
                        "role": "museum_digital_snapshot",
                        "label": "Museum-digital image",
                        "original_path": "",
                        "snapshot_relpath": "",
                        "evidence_relpath": "",
                        "sha256": "",
                        "width": "",
                        "height": "",
                        "status": "missing",
                        "detail": "Captured MD record has no usable main image.",
                    }
                )
    finally:
        catalog.close()
        retained_db.close()

    record_fields = tuple(records[0]) if records else ()
    visual_fields = tuple(visuals[0]) if visuals else ()
    _write_csv(run / "records.csv", record_fields, records)
    _write_csv(run / "visual-comparison.csv", visual_fields, visuals)
    candidate_fields = tuple(candidate_rows[0]) if candidate_rows else ()
    _write_csv(destination / "source-candidates.csv", candidate_fields, candidate_rows)

    campaign_id = f"MD-RECOVERY-{unit}"
    try:
        campaign = reconciliation.connect_campaign(root, read_only=True)
    except reconciliation.ReconciliationError:
        campaign_exists = False
    else:
        try:
            campaign_exists = bool(
                campaign.execute(
                    "SELECT 1 FROM campaigns WHERE campaign_id = ?", (campaign_id,)
                ).fetchone()
            )
        finally:
            campaign.close()
    if campaign_exists:
        merge = reconciliation.merge_visual_evidence(
            root,
            run,
            campaign_id=campaign_id,
            visuals=supplemental_visuals,
        )
        campaign_state = (
            "enriched" if int(merge["inserted"]) else "unchanged"
        )
    else:
        reconciliation.import_museum_digital_run(
            root,
            run,
            campaign_id=campaign_id,
            title=f"{unit} provenance restoration",
        )
        campaign_state = "imported"
    return {
        "result": "ok",
        "unit_id": unit,
        "entities": len(entity_by_inventory),
        "created_entities": created_entities,
        "source_candidates": len(candidate_rows),
        "records": len(records),
        "visuals": len(visuals),
        "captured_md_images": captured_images,
        "reused_md_images": reused_images,
        "preview_failures": preview_failures,
        "campaign_id": campaign_id,
        "campaign_state": campaign_state,
        "supplemental_sources": len(supplements),
        "supplemental_linked_files": sum(
            len(values) for values in supplemental_by_inventory.values()
        ),
        "run": str(run),
    }


def _prior_editor_metadata_choices(
    catalog: sqlite3.Connection, entity_id: str
) -> tuple[set[str], bool]:
    """Return fields and tag choice already decided through the workbench."""

    fields: set[str] = set()
    tags_decided = False
    for row in catalog.execute(
        """SELECT after_json FROM logbook
           WHERE action = 'editor_reconcile_metadata' AND entity_id = ?
           ORDER BY at_utc, log_id""",
        (entity_id,),
    ):
        try:
            payload = json.loads(str(row["after_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        choices = payload.get("fields")
        if isinstance(choices, dict):
            fields.update(str(key) for key in choices)
        if payload.get("tags") is not None:
            tags_decided = True
    return fields, tags_decided


def _backup_catalog_before_md_defaults(root: Path, unit: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        root / "work" / "backups" / f"{stamp}-PRE-MD-DEFAULTS-{unit}.sqlite"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
        backup.commit()
    finally:
        backup.close()
        source.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro&immutable=1", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UnitRecoveryError("pre-MD-default catalog backup failed integrity")
    finally:
        check.close()
    return destination


def accept_md_metadata_defaults(
    *,
    root: Path,
    unit: str,
    actor: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Fill undecided object metadata from exact captured MD records.

    This operation deliberately does not resolve visual identity, select a
    representation, complete a review, accession files, or publish a
    generation. Existing catalog values and prior editor choices always win.
    """

    actor = actor.strip()
    if not actor:
        raise UnitRecoveryError("named actor is required")
    destination = recovery_dir(root, unit)
    scope = json.loads((destination / "scope.json").read_text(encoding="utf-8"))
    if scope.get("format") != FORMAT or scope.get("unit_id") != unit:
        raise UnitRecoveryError("bounded recovery scope is invalid")
    campaign_id = f"MD-RECOVERY-{unit}"
    catalog_path = root / "work" / "catalog.sqlite"
    campaign = reconciliation.connect_campaign(root, read_only=True)
    catalog = catalog_core.connect_catalog(catalog_path, read_only=not apply)
    plans: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_without_exact_md = 0
    try:
        records = campaign.execute(
            """SELECT * FROM reconciliation_records
               WHERE campaign_id = ? ORDER BY inventory_label""",
            (campaign_id,),
        ).fetchall()
        for record in records:
            inventory = str(record["inventory_label"] or "")
            entity_id = str(record["entity_id"] or "")
            if (
                record["match_status"] != "exact_inventory"
                or not entity_id
                or not record["external_id"]
                or not record["snapshot_digest"]
            ):
                skipped_without_exact_md += 1
                continue
            terminal_path = (
                destination
                / "museum-digital"
                / "objects"
                / f"{urllib.parse.quote(inventory, safe='-_.')}.json"
            )
            terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            exact = terminal.get("exact_objects")
            if (
                terminal.get("state") != "found"
                or not isinstance(exact, list)
                or len(exact) != 1
                or not isinstance(exact[0], dict)
            ):
                raise UnitRecoveryError(
                    f"captured MD evidence is not one exact record: {inventory}"
                )
            md = exact[0]
            if (
                str(md.get("object_id") or "") != str(record["external_id"])
                or reconcile.normalize_museum_digital_inventory(
                    str(md.get("object_inventory_number") or "")
                )
                != reconcile.normalize_museum_digital_inventory(inventory)
            ):
                raise UnitRecoveryError(
                    f"captured MD identity disagrees with campaign: {inventory}"
                )
            archive = catalog.execute(
                """SELECT descriptive_title, description,
                          material_technique, dimensions
                   FROM archive_objects WHERE entity_id = ?""",
                (entity_id,),
            ).fetchone()
            if not archive:
                raise UnitRecoveryError(f"campaign entity is missing: {inventory}")
            prior_campaign_fields = {
                str(row["field_key"])
                for row in campaign.execute(
                    """SELECT DISTINCT field_key FROM field_decisions
                       WHERE record_id = ?""",
                    (record["record_id"],),
                )
            }
            prior_editor_fields, tags_decided = _prior_editor_metadata_choices(
                catalog, entity_id
            )
            field_specs = (
                ("title", "archive_descriptive_title", "descriptive_title", "object_name"),
                ("description", "archive_description", "description", "object_description"),
                ("material", "archive_material_technique", "material_technique", "object_material_technique"),
                ("dimensions", "archive_dimensions", "dimensions", "object_dimensions"),
            )
            fields: dict[str, dict[str, str]] = {}
            for editor_key, campaign_key, column, md_key in field_specs:
                value = str(md.get(md_key) or "").strip()
                if (
                    value
                    and not str(archive[column] or "").strip()
                    and campaign_key not in prior_campaign_fields
                    and editor_key not in prior_editor_fields
                ):
                    fields[editor_key] = {
                        "source": "museum_digital",
                        "value": value,
                    }
            accepted_extra = editor_reconcile.accepted_metadata(catalog, entity_id)
            object_type = str(md.get("object_type") or "").strip()
            if (
                object_type
                and not accepted_extra["object_type"]
                and "object_type" not in prior_editor_fields
            ):
                fields["object_type"] = {
                    "source": "museum_digital",
                    "value": object_type,
                }
            md_tags = _md_tag_values(md)
            tag_decision = None
            if md_tags and not accepted_extra["tags"] and not tags_decided:
                tag_decision = {
                    "source": "museum_digital",
                    "values": md_tags,
                }
            accepted_tag_keys = {
                str(value).strip().casefold() for value in accepted_extra["tags"]
            }
            archive_keywords = [
                keyword
                for keyword in editor_reconcile.UNIT_DEFAULT_KEYWORDS.get(unit, ())
                if keyword.casefold() not in accepted_tag_keys
            ]
            if not fields and tag_decision is None and not archive_keywords:
                skipped_existing += 1
                continue
            plans.append(
                {
                    "inventory": inventory,
                    "entity_id": entity_id,
                    "external_id": str(record["external_id"]),
                    "snapshot_digest": str(record["snapshot_digest"]),
                    "fields": fields,
                    "tag_decision": tag_decision,
                    "archive_keywords": archive_keywords,
                }
            )

        result: dict[str, Any] = {
            "result": "ok",
            "state": "planned" if not apply else "applied",
            "unit_id": unit,
            "campaign_id": campaign_id,
            "records_considered": len(records),
            "records_planned": len(plans),
            "fields_planned": sum(len(plan["fields"]) for plan in plans),
            "tags_planned": sum(
                len(plan["tag_decision"]["values"])
                for plan in plans
                if plan["tag_decision"] is not None
            ),
            "archive_keywords_planned": sum(
                len(plan["archive_keywords"]) for plan in plans
            ),
            "skipped_existing_or_decided": skipped_existing,
            "skipped_without_exact_md": skipped_without_exact_md,
            "catalog_backup": "",
        }
        if not apply or not plans:
            return result
        backup = _backup_catalog_before_md_defaults(root, unit)
        applied = 0
        for plan in plans:
            if plan["fields"] or plan["tag_decision"] is not None:
                editor_reconcile.reconcile_metadata(
                    catalog,
                    unit_id=unit,
                    entity_id=plan["entity_id"],
                    field_decisions=plan["fields"],
                    tag_decision=plan["tag_decision"],
                    actor=actor,
                    reason="",
                    evidence=plan["snapshot_digest"],
                    external_id=plan["external_id"],
                )
            for keyword in plan["archive_keywords"]:
                editor_reconcile.add_archive_keyword(
                    catalog,
                    unit_id=unit,
                    entity_id=plan["entity_id"],
                    value=keyword,
                    actor=actor,
                    evidence=f"unit:{unit}",
                )
            applied += 1
        catalog_core.assert_catalog_integrity(catalog)
        result["records_applied"] = applied
        result["catalog_backup"] = str(backup)
        result["catalog_revision"] = int(
            catalog_core.catalog_state(catalog)["catalog_revision"]
        )
        return result
    finally:
        catalog.close()
        campaign.close()


def bootstrap(*, root: Path, unit: str, source_root: Path, retained_index: Path) -> dict[str, Any]:
    destination = recovery_dir(root, unit)
    manifest_path = destination / "scope.json"
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            raise UnitRecoveryError("existing recovery scope has an unknown format")
        return {"result": "ok", "state": "unchanged", "manifest": str(manifest_path), "files": data["summary"]["files"]}
    source = protected_source(source_root)
    retained = retained_index.expanduser().resolve(strict=True)
    before = source.stat()
    files: list[dict[str, Any]] = []
    for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(source)).casefold()):
        stat = path.stat()
        files.append({
            "relative_path": path.relative_to(source).as_posix(),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(path),
        })
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise UnitRecoveryError("protected source root changed during scope capture")
    connection = sqlite3.connect(f"file:{retained}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        prefix = str(source) + "/%"
        rows = connection.execute(
            """SELECT DISTINCT l.inventory_number FROM file_object_links l
               JOIN files f ON f.id = l.file_id WHERE f.path LIKE ?
               ORDER BY l.inventory_number""", (prefix,)
        ).fetchall()
    finally:
        connection.close()
    inventories = [str(row["inventory_number"]) for row in rows]
    extensions = Counter(Path(item["relative_path"]).suffix.casefold() or "<none>" for item in files)
    manifest = {
        "format": FORMAT,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "unit_id": unit,
        "state": "candidate_scope_needs_review",
        "source": {
            "root": str(source), "device": before.st_dev, "inode": before.st_ino,
            "size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
        },
        "retained_index": {"path": str(retained), "size_bytes": retained.stat().st_size, "mtime_ns": retained.stat().st_mtime_ns},
        "summary": {"files": len(files), "bytes": sum(item["size_bytes"] for item in files), "inventories": len(inventories), "extensions": dict(sorted(extensions.items()))},
        "historical_scope_reference": {"files": 490, "bytes": 18154044008, "status": "unreconciled_reference_includes_material_outside_declared_source"},
        "inventories": inventories,
        "files": files,
    }
    _atomic_new(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    csv_path = destination / "scope.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("relative_path", "size_bytes", "mtime_ns", "sha256"), quoting=csv.QUOTE_ALL)
        writer.writeheader(); writer.writerows(files)
    return {"result": "ok", "state": "captured", "manifest": str(manifest_path), "files": len(files), "bytes": manifest["summary"]["bytes"], "inventories": len(inventories)}


def capture_md(*, root: Path, unit: str, timeout: float = 30.0, delay: float = 0.75, fetcher: Callable[[str, float], tuple[list[dict[str, Any]], str]] = reconcile.fetch_museum_digital_query) -> dict[str, Any]:
    destination = recovery_dir(root, unit)
    manifest_path = destination / "scope.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    objects_dir = destination / "museum-digital" / "objects"
    failures_dir = destination / "museum-digital" / "failures"
    counts: Counter[str] = Counter(); skipped = 0
    for inventory in manifest["inventories"]:
        token = urllib.parse.quote(inventory, safe="-_.")
        terminal = objects_dir / f"{token}.json"
        if terminal.exists():
            skipped += 1; continue
        candidates, error = fetcher(inventory, timeout)
        captured_at = utc_now()
        if error:
            if "HTTP Error 404" in error:
                evidence = {
                    "format": "iss-museum-digital-unit-capture-v1",
                    "captured_at": captured_at, "inventory": inventory,
                    "state": "not_found", "query": inventory,
                    "requested_url": reconcile.MUSEUM_DIGITAL_EXPORT_URL.format(query=urllib.parse.quote(inventory, safe=":,")),
                    "returned_count": 0, "exact_count": 0,
                    "exact_objects": [], "returned_objects": [],
                    "response_evidence": error,
                }
                _atomic_new(terminal, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                counts["not_found"] += 1
                time.sleep(max(0.0, delay)); continue
            failure = failures_dir / f"{token}-{captured_at.replace(':', '')}.json"
            _atomic_new(failure, json.dumps({"inventory": inventory, "state": "request_failed", "captured_at": captured_at, "error": error}, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            counts["request_failed"] += 1
            time.sleep(max(0.0, delay)); continue
        normalized = reconcile.normalize_museum_digital_inventory(inventory)
        exact = [item for item in candidates if reconcile.normalize_museum_digital_inventory(str(item.get("object_inventory_number") or "")) == normalized]
        state = "found" if len(exact) == 1 else "not_found" if not exact else "ambiguous"
        evidence = {
            "format": "iss-museum-digital-unit-capture-v1", "captured_at": captured_at,
            "inventory": inventory, "state": state, "query": inventory,
            "requested_url": reconcile.MUSEUM_DIGITAL_EXPORT_URL.format(query=urllib.parse.quote(inventory, safe=":,")),
            "returned_count": len(candidates), "exact_count": len(exact),
            "exact_objects": exact, "returned_objects": candidates,
        }
        _atomic_new(terminal, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        counts[state] += 1
        time.sleep(max(0.0, delay))
    run = {"format": "iss-museum-digital-unit-capture-run-v1", "created_at": utc_now(), "unit_id": unit, "counts": dict(sorted(counts.items())), "skipped_terminal": skipped}
    run_path = destination / "museum-digital" / "runs" / f"run-{run['created_at'].replace(':', '')}.json"
    _atomic_new(run_path, json.dumps(run, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"result": "ok", **run, "run": str(run_path)}


def capture_md_batch(*, root: Path, unit: str, query: str, timeout: float = 60.0) -> dict[str, Any]:
    destination = recovery_dir(root, unit)
    manifest = json.loads((destination / "scope.json").read_text(encoding="utf-8"))
    batch_id = utc_now().replace(":", "")
    pages_dir = destination / "museum-digital" / "batches" / batch_id
    all_objects: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0
    while total is None or offset < total:
        encoded = urllib.parse.quote(query, safe=":,")
        url = reconcile.MUSEUM_DIGITAL_EXPORT_URL.format(query=encoded) + f"&offset={offset}"
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": reconcile.MUSEUM_DIGITAL_USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        results = payload.get("results", {})
        objects = results.get("objects", {})
        page = list(objects.values()) if isinstance(objects, dict) else list(objects)
        total = int(results.get("total") or len(page))
        _atomic_new(pages_dir / f"offset-{offset:06d}.json", json.dumps({"requested_url": url, "captured_at": utc_now(), "response": payload}, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        all_objects.extend(item for item in page if isinstance(item, dict))
        offset += 100
    lookup: dict[str, list[dict[str, Any]]] = {}
    for item in all_objects:
        key = reconcile.normalize_museum_digital_inventory(str(item.get("object_inventory_number") or ""))
        if key:
            lookup.setdefault(key, []).append(item)
    objects_dir = destination / "museum-digital" / "objects"
    conflicts_dir = destination / "museum-digital" / "conflicts"
    counts: Counter[str] = Counter()
    for inventory in manifest["inventories"]:
        token = urllib.parse.quote(inventory, safe="-_.")
        terminal = objects_dir / f"{token}.json"
        exact = lookup.get(reconcile.normalize_museum_digital_inventory(inventory), [])
        state = "found" if len(exact) == 1 else "not_found" if not exact else "ambiguous"
        if terminal.exists():
            prior = json.loads(terminal.read_text(encoding="utf-8"))
            if prior.get("state") != state:
                _atomic_new(conflicts_dir / f"{token}-{batch_id}.json", json.dumps({"inventory": inventory, "prior_state": prior.get("state"), "batch_state": state, "batch_objects": exact}, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                counts["conflict"] += 1
            else:
                counts["already_terminal"] += 1
            continue
        evidence = {"format": "iss-museum-digital-unit-capture-v1", "captured_at": utc_now(), "inventory": inventory, "state": state, "query": query, "batch_id": batch_id, "exact_count": len(exact), "exact_objects": exact}
        _atomic_new(terminal, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        counts[state] += 1
    run = {"format": "iss-museum-digital-unit-batch-run-v1", "created_at": utc_now(), "unit_id": unit, "query": query, "batch_id": batch_id, "returned_objects": len(all_objects), "reported_total": total, "pages": (offset // 100), "counts": dict(sorted(counts.items()))}
    _atomic_new(destination / "museum-digital" / "runs" / f"batch-{batch_id}.json", json.dumps(run, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"result": "ok", **run}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap"); boot.add_argument("--root", type=Path, required=True); boot.add_argument("--unit", required=True); boot.add_argument("--source-root", type=Path, required=True); boot.add_argument("--retained-index", type=Path, required=True)
    supplement = sub.add_parser("capture-supplemental-source"); supplement.add_argument("--root", type=Path, required=True); supplement.add_argument("--unit", required=True); supplement.add_argument("--source-root", type=Path, required=True)
    md = sub.add_parser("capture-md"); md.add_argument("--root", type=Path, required=True); md.add_argument("--unit", required=True); md.add_argument("--timeout", type=float, default=30.0); md.add_argument("--delay", type=float, default=0.75)
    batch = sub.add_parser("capture-md-batch"); batch.add_argument("--root", type=Path, required=True); batch.add_argument("--unit", required=True); batch.add_argument("--query", required=True); batch.add_argument("--timeout", type=float, default=60.0)
    prepare = sub.add_parser("prepare-reconciliation"); prepare.add_argument("--root", type=Path, required=True); prepare.add_argument("--unit", required=True); prepare.add_argument("--actor", required=True); prepare.add_argument("--timeout", type=float, default=30.0)
    defaults = sub.add_parser("accept-md-defaults"); defaults.add_argument("--root", type=Path, required=True); defaults.add_argument("--unit", required=True); defaults.add_argument("--actor", required=True); defaults.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap": result = bootstrap(root=args.root, unit=args.unit, source_root=args.source_root, retained_index=args.retained_index)
    elif args.command == "capture-supplemental-source": result = capture_supplemental_source(root=args.root, unit=args.unit, source_root=args.source_root)
    elif args.command == "capture-md": result = capture_md(root=args.root, unit=args.unit, timeout=args.timeout, delay=args.delay)
    elif args.command == "capture-md-batch": result = capture_md_batch(root=args.root, unit=args.unit, query=args.query, timeout=args.timeout)
    elif args.command == "prepare-reconciliation": result = prepare_reconciliation(root=args.root, unit=args.unit, actor=args.actor, timeout=args.timeout)
    else: result = accept_md_metadata_defaults(root=args.root, unit=args.unit, actor=args.actor, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
