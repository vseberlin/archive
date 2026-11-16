#!/usr/bin/env python3
"""Plan and run reviewed Paragon surrogate-to-master swaps serially."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parent
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import reconciliation
import tiff_accession
import tiff_metadata
from archive_core.catalog import write_unit_catalog
from archive_core.layout import object_relative_path
from jobs.provenance_restoration.technical import verify_image_derivative
from workflows import recovery_accession, recovery_correction, recovery_metadata


FORMAT = "iss-surrogate-swap-batch-plan-v1"
RESULT_FORMAT = "iss-surrogate-swap-batch-result-v1"
TOOL_VERSION = "surrogate-swap-batch-1.0.0"
DEFAULT_EXPECTED_COUNT = 11


class SurrogateSwapBatchError(RuntimeError):
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
            raise SurrogateSwapBatchError(
                f"database backup failed integrity check: {destination}"
            )
    finally:
        check.close()


def _active_rows(
    catalog: sqlite3.Connection,
    entity_id: str,
) -> list[sqlite3.Row]:
    return recovery_correction._active_rows(catalog, entity_id)


def _active_surrogate(
    catalog: sqlite3.Connection,
    entity_id: str,
    active_digest: str,
) -> sqlite3.Row:
    rows = [
        row
        for row in _active_rows(catalog, entity_id)
        if row["role"] == "preservation_surrogate"
        and str(row["content_digest"]) == active_digest
    ]
    if len(rows) != 1:
        raise SurrogateSwapBatchError(
            "swap review is not bound to exactly one active surrogate"
        )
    return rows[0]


def _completed_swap(
    catalog: sqlite3.Connection,
    *,
    entity_id: str,
    source_digest: str,
) -> bool:
    row = catalog.execute(
        """SELECT 1
           FROM entity_files ef
           JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
           WHERE ef.entity_id=? AND ef.status='active'
             AND ef.role='preservation_master'
             AND fo.status='verified'""",
        (entity_id,),
    ).fetchone()
    if not row:
        return False
    latest = catalog.execute(
        """SELECT after_json FROM logbook
           WHERE entity_id=? AND action='replace_surrogate_with_master'
           ORDER BY at_utc DESC, log_id DESC LIMIT 1""",
        (entity_id,),
    ).fetchone()
    if not latest:
        return False
    try:
        payload = json.loads(str(latest["after_json"] or "{}"))
    except json.JSONDecodeError:
        return False
    return str(payload.get("source_digest") or "") == source_digest


def _marked_reviews(
    catalog: sqlite3.Connection,
) -> list[dict[str, Any]]:
    reviews = catalog_core.surrogate_swap_reviews(catalog)
    marked = [
        dict(review)
        for review in reviews.values()
        if review.get("state") == "marked_for_swap"
    ]
    marked.sort(key=lambda item: str(item["candidate_path"]))
    return marked


def _source_from_review(review: dict[str, Any]) -> Path:
    source = Path(str(review["candidate_path"])).resolve(strict=True)
    if not source.is_file() or source.suffix.casefold() not in {".tif", ".tiff"}:
        raise SurrogateSwapBatchError("marked swap source is not one TIFF file")
    stat = source.stat()
    if int(stat.st_size) != int(review["candidate_size_bytes"]):
        raise SurrogateSwapBatchError("marked swap source size changed")
    return source


def _candidate_technical(source: Path, review: dict[str, Any]) -> dict[str, str]:
    technical = tiff_accession._exif_values(source, tiff_accession.TECHNICAL_TAGS)
    if technical.get("File:FileType") != "TIFF":
        raise SurrogateSwapBatchError("marked swap source is not structurally TIFF")
    if tiff_accession._page_count(source) != 1:
        raise SurrogateSwapBatchError("marked swap source is not single-page TIFF")
    if (
        int(technical["IFD0:ImageWidth"]) != int(review["candidate_width_px"])
        or int(technical["IFD0:ImageHeight"])
        != int(review["candidate_height_px"])
    ):
        raise SurrogateSwapBatchError("marked swap source dimensions changed")
    return technical


def build_plan(
    root: Path,
    *,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    config = catalog_core.require_core_mode(root)
    independent = catalog_core.assert_safe_work_root(
        Path(str(config["independent_generation_root"])).resolve(strict=True)
    )
    items: list[dict[str, Any]] = []
    with closing(
        catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite",
            read_only=True,
        )
    ) as catalog:
        catalog_core.assert_catalog_integrity(catalog)
        for review in _marked_reviews(catalog):
            entity_id = str(review["entity_id"])
            row = catalog.execute(
                """SELECT archival_label FROM archive_objects
                   WHERE entity_id=? AND status='active'""",
                (entity_id,),
            ).fetchone()
            if not row:
                raise SurrogateSwapBatchError(
                    f"marked swap entity is not active: {entity_id}"
                )
            inventory = str(row["archival_label"])
            source = _source_from_review(review)
            technical = _candidate_technical(source, review)
            source_digest = _digest(source)
            try:
                active = _active_surrogate(
                    catalog,
                    entity_id,
                    str(review["active_digest"]),
                )
            except SurrogateSwapBatchError:
                if _completed_swap(
                    catalog,
                    entity_id=entity_id,
                    source_digest=source_digest,
                ):
                    continue
                raise
            metadata = tiff_accession._catalog_metadata(
                catalog,
                entity_id,
                tiff_metadata.PROFILE_V2,
            )
            provenance = {
                "case_id": f"SURROGATE-SWAP-WF-PARAGON-{inventory}",
                "source_file_id": str(review["candidate_key"]),
                "source_digest": source_digest,
                "reviewed_bundle_digest": "",
                "museum_digital_id": "",
                "representation_role": "preservation_master",
                "provenance_gaps": [],
            }
            provenance.update(recovery_metadata.require_embedding_metadata(inventory))
            item_without_digest = {
                "inventory": inventory,
                "entity_id": entity_id,
                "candidate_key": str(review["candidate_key"]),
                "candidate_path": str(source),
                "source_digest": source_digest,
                "source_size_bytes": int(source.stat().st_size),
                "source_width_px": int(technical["IFD0:ImageWidth"]),
                "source_height_px": int(technical["IFD0:ImageHeight"]),
                "active_surrogate_digest": str(active["content_digest"]),
                "active_surrogate_relative_path": str(active["relative_path"]),
                "reviewed_at": str(review["reviewed_at"]),
                "reviewer": str(review["actor"]),
                "embedding_metadata": recovery_metadata.require_embedding_metadata(
                    inventory
                ),
                "embedding_mapping": tiff_metadata.V2_MAPPING_VERSION,
            }
            provenance["reviewed_bundle_digest"] = _payload_digest(
                item_without_digest
            )
            expected = tiff_accession._expected_embedded(
                metadata,
                tiff_metadata.PROFILE_V2,
                provenance,
            )
            item = {
                **item_without_digest,
                "reviewed_bundle_digest": provenance["reviewed_bundle_digest"],
                "expected_embedded_tag_count": len(expected),
            }
            item["item_digest"] = _payload_digest(item)
            items.append(item)
    if expected_count and len(items) != expected_count:
        raise SurrogateSwapBatchError(
            f"marked swap count is {len(items)}, expected {expected_count}"
        )
    encoded = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "format": FORMAT,
        "root": str(root),
        "independent_root": str(independent),
        "count": len(items),
        "items": items,
        "plan_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _copy_backup(source: Path, destination: Path, expected: str) -> None:
    if not source.is_file() or _digest(source) != expected:
        raise SurrogateSwapBatchError(f"active representation failed fixity: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _digest(destination) != expected:
        raise SurrogateSwapBatchError(f"representation backup failed: {destination}")


def _atomic_install(source: Path, destination: Path, expected: str) -> None:
    recovery_correction._atomic_install(source, destination, expected)


def _ensure_blob(
    catalog: sqlite3.Connection,
    *,
    digest: str,
    size_bytes: int,
    media_type: str,
    width_px: int,
    height_px: int,
    bit_depth: int | None,
    color_space: str,
    compression: str,
    now: str,
) -> None:
    recovery_correction._ensure_blob(
        catalog,
        digest=digest,
        size_bytes=size_bytes,
        media_type=media_type,
        width_px=width_px,
        height_px=height_px,
        bit_depth=bit_depth,
        color_space=color_space,
        compression=compression,
        now=now,
    )


def _insert_occurrence(
    catalog: sqlite3.Connection,
    *,
    entity_id: str,
    unit_id: str,
    role: str,
    digest: str,
    relative_path: str,
    observed_mtime: str,
    note: str,
    now: str,
) -> str:
    return recovery_correction._insert_occurrence(
        catalog,
        entity_id=entity_id,
        unit_id=unit_id,
        role=role,
        digest=digest,
        relative_path=relative_path,
        observed_mtime=observed_mtime,
        note=note,
        now=now,
    )


def apply_one(
    root: Path,
    independent: Path,
    *,
    inventory: str,
    actor: str,
    reason: str,
    expected_item_digest: str,
) -> dict[str, Any]:
    reviewed = next(
        (
            item
            for item in build_plan(root, expected_count=0)["items"]
            if item["inventory"] == inventory
        ),
        None,
    )
    if not reviewed:
        raise SurrogateSwapBatchError(f"{inventory} is not marked for swap")
    if reviewed["item_digest"] != expected_item_digest:
        raise SurrogateSwapBatchError(f"{inventory} swap plan changed")
    catalog_path = root / "work" / "catalog.sqlite"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / "work" / "backups" / f"{stamp}-PRE-SURROGATE-SWAP-{inventory}"
    backup_dir.mkdir(parents=True)
    _backup_database(catalog_path, backup_dir / "catalog.sqlite")
    (backup_dir / "plan-item.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    entity_id = str(reviewed["entity_id"])
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        old_rows = _active_rows(catalog, entity_id)
        for row in old_rows:
            expected = str(row["content_digest"])
            relative = Path(str(row["relative_path"]))
            for label, base in (("primary", root), ("independent", independent)):
                _copy_backup(
                    base / relative,
                    backup_dir / label / relative,
                    expected,
                )
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        state = catalog_core.catalog_state(catalog)
        clean = (
            state["catalog_revision"] == state["published_revision"]
            and state["dirty_since"] is None
        )
    if not clean:
        catalog_core.publish_generation(
            root,
            independent,
            tool_version=TOOL_VERSION,
            actor=actor,
        )
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        state = catalog_core.catalog_state(catalog)
        if state["catalog_revision"] != state["published_revision"]:
            raise SurrogateSwapBatchError("catalog is not clean before swap")
        expected_revision = int(state["catalog_revision"])
        metadata = tiff_accession._catalog_metadata(
            catalog,
            entity_id,
            tiff_metadata.PROFILE_V2,
        )
        active_rows = _active_rows(catalog, entity_id)
    source = Path(str(reviewed["candidate_path"])).resolve(strict=True)
    if _digest(source) != str(reviewed["source_digest"]):
        raise SurrogateSwapBatchError("marked swap source digest changed")
    technical_source = _candidate_technical(
        source,
        {
            "candidate_width_px": reviewed["source_width_px"],
            "candidate_height_px": reviewed["source_height_px"],
        },
    )
    bit_depth = tiff_accession._scalar_sample_depth(
        technical_source.get("IFD0:BitsPerSample")
    )
    provenance = {
        "case_id": f"SURROGATE-SWAP-WF-PARAGON-{inventory}",
        "source_file_id": str(reviewed["candidate_key"]),
        "source_digest": str(reviewed["source_digest"]),
        "reviewed_bundle_digest": str(reviewed["reviewed_bundle_digest"]),
        "museum_digital_id": "",
        "representation_role": "preservation_master",
        "provenance_gaps": [],
    }
    provenance.update(recovery_metadata.require_embedding_metadata(inventory))
    expected_tags = tiff_accession._expected_embedded(
        metadata,
        tiff_metadata.PROFILE_V2,
        provenance,
    )
    operation_id = f"SURROGATE-SWAP-{stamp}-{inventory}"
    staging = root / "work" / "accession-staging" / operation_id
    staging.mkdir(parents=True)
    staged_master = staging / "master.tif"
    tiff_accession._write_candidate(source, staged_master, expected_tags)
    if tiff_accession._exif_values(staged_master, tuple(expected_tags)) != expected_tags:
        raise SurrogateSwapBatchError("replacement TIFF metadata read-back differs")
    technical = tiff_accession._exif_values(
        staged_master,
        tiff_accession.TECHNICAL_TAGS,
    )
    if technical != technical_source:
        raise SurrogateSwapBatchError("TIFF technical structure changed during swap")
    decoded_source = tiff_accession._decoded_pixel_digest(source, bit_depth)
    decoded_master = tiff_accession._decoded_pixel_digest(staged_master, bit_depth)
    if decoded_source != decoded_master:
        raise SurrogateSwapBatchError("decoded pixels changed during swap")
    master_digest = _digest(staged_master)
    staged_derivative = staging / "access.jpg"
    derivative_digest, derivative_technical = (
        recovery_accession._ensure_generated_access_jpeg(
            source,
            staged_derivative,
            width_px=int(technical["IFD0:ImageWidth"]),
            height_px=int(technical["IFD0:ImageHeight"]),
            scratch=staging / "scratch",
        )
    )
    if _digest(staged_derivative) != derivative_digest:
        raise SurrogateSwapBatchError("replacement derivative failed fixity")
    unit_id = str(metadata["unit_id"])
    master_relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=inventory,
        entity_id=entity_id,
        role="preservation_master",
        suffix=".tif",
    ).parts)
    derivative_relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=inventory,
        entity_id=entity_id,
        role="access_derivative",
        suffix=".jpg",
    ).parts)
    for base in (root, independent):
        _atomic_install(staged_master, base / master_relative, master_digest)
        _atomic_install(staged_derivative, base / derivative_relative, derivative_digest)
    now = catalog_core.utc_now()
    observed_master = datetime.fromtimestamp(
        (root / master_relative).stat().st_mtime,
        UTC,
    ).isoformat().replace("+00:00", "Z")
    observed_derivative = datetime.fromtimestamp(
        (root / derivative_relative).stat().st_mtime,
        UTC,
    ).isoformat().replace("+00:00", "Z")
    catalog = catalog_core.connect_catalog(catalog_path)
    try:
        catalog.execute("BEGIN IMMEDIATE")
        state = catalog_core.catalog_state(catalog)
        if (
            int(state["catalog_revision"]) != expected_revision
            or int(state["published_revision"]) != expected_revision
        ):
            raise SurrogateSwapBatchError(
                "catalog changed after swap embedding began"
            )
        current = _active_rows(catalog, entity_id)
        if {str(row["occurrence_id"]) for row in current} != {
            str(row["occurrence_id"]) for row in active_rows
        }:
            raise SurrogateSwapBatchError("active representations changed")
        catalog.execute(
            """UPDATE entity_files SET status='superseded'
               WHERE entity_id=? AND status='active'
                 AND role IN ('preservation_surrogate', 'access_derivative')""",
            (entity_id,),
        )
        _ensure_blob(
            catalog,
            digest=master_digest,
            size_bytes=(root / master_relative).stat().st_size,
            media_type="image/tiff",
            width_px=int(technical["IFD0:ImageWidth"]),
            height_px=int(technical["IFD0:ImageHeight"]),
            bit_depth=bit_depth,
            color_space=str(technical["IFD0:PhotometricInterpretation"]),
            compression=str(technical["IFD0:Compression"]),
            now=now,
        )
        _ensure_blob(
            catalog,
            digest=derivative_digest,
            size_bytes=(root / derivative_relative).stat().st_size,
            media_type="image/jpeg",
            width_px=int(derivative_technical["width"]),
            height_px=int(derivative_technical["height"]),
            bit_depth=None,
            color_space="",
            compression="",
            now=now,
        )
        master_occurrence = _insert_occurrence(
            catalog,
            entity_id=entity_id,
            unit_id=unit_id,
            role="preservation_master",
            digest=master_digest,
            relative_path=master_relative.as_posix(),
            observed_mtime=observed_master,
            note=(
                f"controlled surrogate-to-master swap; profile "
                f"{tiff_metadata.PROFILE_V2}; source {reviewed['source_digest']}; "
                f"decoded pixels {decoded_master}"
            ),
            now=now,
        )
        derivative_occurrence = _insert_occurrence(
            catalog,
            entity_id=entity_id,
            unit_id=unit_id,
            role="access_derivative",
            digest=derivative_digest,
            relative_path=derivative_relative.as_posix(),
            observed_mtime=observed_derivative,
            note=json.dumps(
                {
                    "derivation": "generated_from_lossless_master",
                    "master_digest": master_digest,
                    "swap_candidate_key": str(reviewed["candidate_key"]),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            now=now,
        )
        registration = {
            "content_digest": master_digest,
            "decoded_pixel_digest": decoded_master,
            "embedding_profile": tiff_metadata.PROFILE_V2,
            "exiftool_version": tiff_accession._tool_version("exiftool"),
            "height_px": int(technical["IFD0:ImageHeight"]),
            "imagemagick_version": tiff_accession._tool_version("magick"),
            "occurrence_id": master_occurrence,
            "relative_path": master_relative.as_posix(),
            "source_digest": str(reviewed["source_digest"]),
            "width_px": int(technical["IFD0:ImageWidth"]),
        }
        catalog_core._append_log(
            catalog,
            actor,
            "register_preservation_master",
            "entity_file",
            entity_id,
            unit_id,
            registration,
            evidence=master_digest,
            note="controlled surrogate-to-master swap",
            at_utc=now,
        )
        catalog_core._append_log(
            catalog,
            actor,
            "replace_surrogate_with_master",
            "entity_file",
            entity_id,
            unit_id,
            {
                "inventory": inventory,
                "master_digest": master_digest,
                "master_occurrence_id": master_occurrence,
                "derivative_digest": derivative_digest,
                "derivative_occurrence_id": derivative_occurrence,
                "item_digest": reviewed["item_digest"],
                "reviewed_bundle_digest": reviewed["reviewed_bundle_digest"],
                "source_digest": reviewed["source_digest"],
                "superseded_occurrence_ids": [
                    str(row["occurrence_id"]) for row in active_rows
                    if row["role"] in ("preservation_surrogate", "access_derivative")
                ],
            },
            before={
                "active": [
                    {
                        "role": str(row["role"]),
                        "digest": str(row["content_digest"]),
                        "relative_path": str(row["relative_path"]),
                    }
                    for row in active_rows
                ]
            },
            evidence=str(reviewed["item_digest"]),
            note=reason,
            at_utc=now,
        )
        catalog.commit()
    except Exception:
        catalog.rollback()
        raise
    finally:
        catalog.close()
    publication = catalog_core.publish_generation(
        root,
        independent,
        tool_version=TOOL_VERSION,
        actor=actor,
    )
    for label, base in (("primary", root), ("independent", independent)):
        if _digest(base / master_relative) != master_digest:
            raise SurrogateSwapBatchError(f"{label} master fixity failed")
        if _digest(base / derivative_relative) != derivative_digest:
            raise SurrogateSwapBatchError(f"{label} derivative fixity failed")
    if (
        tiff_accession._exif_values(root / master_relative, tuple(expected_tags))
        != expected_tags
    ):
        raise SurrogateSwapBatchError("installed TIFF metadata read-back differs")
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        catalog_core.assert_catalog_integrity(catalog)
        primary_catalog = write_unit_catalog(catalog, unit_id, root / "units" / unit_id)
        independent_catalog = write_unit_catalog(
            catalog,
            unit_id,
            independent / "units" / unit_id,
        )
    if (
        primary_catalog["csv_sha256"] != independent_catalog["csv_sha256"]
        or primary_catalog["xlsx_sha256"] != independent_catalog["xlsx_sha256"]
    ):
        raise SurrogateSwapBatchError("unit catalogs differ after swap")
    return {
        "result": "ok",
        "inventory": inventory,
        "operation": "replaced_surrogate_with_master",
        "master_digest": master_digest,
        "derivative_digest": derivative_digest,
        "backup": str(backup_dir),
        "publication": publication,
    }


def run_plan(
    root: Path,
    *,
    actor: str,
    reason: str,
    plan_digest: str,
    expected_count: int,
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    config = catalog_core.require_core_mode(root)
    independent = catalog_core.assert_safe_work_root(
        Path(str(config["independent_generation_root"])).resolve(strict=True)
    )
    catalog_core.assert_independent_device(root, independent)
    lock_path = root / "work" / ".surrogate-swap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SurrogateSwapBatchError(
                "another surrogate swap batch is already running"
            ) from error
        plan = build_plan(root, expected_count=expected_count)
        if plan["plan_digest"] != plan_digest:
            raise SurrogateSwapBatchError(
                "surrogate swap batch plan changed; review a new dry-run"
            )
        successes: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for item in plan["items"]:
            inventory = str(item["inventory"])
            try:
                result = apply_one(
                    root,
                    independent,
                    inventory=inventory,
                    actor=actor,
                    reason=reason,
                    expected_item_digest=str(item["item_digest"]),
                )
                successes.append(
                    {
                        "inventory": inventory,
                        "generation_id": str(result["publication"]["generation_id"]),
                        "master_digest": str(result["master_digest"]),
                    }
                )
                print(
                    f"swapped {inventory} as "
                    f"{result['publication']['generation_id']}",
                    flush=True,
                )
            except (
                SurrogateSwapBatchError,
                catalog_core.CoreCatalogError,
                reconciliation.ReconciliationError,
                tiff_accession.TiffAccessionError,
                sqlite3.Error,
                OSError,
                ValueError,
            ) as error:
                failures.append({"inventory": inventory, "error": str(error)})
                print(f"failed {inventory}: {error}", flush=True)
                break
        return {
            "format": RESULT_FORMAT,
            "plan_digest": plan["plan_digest"],
            "planned": int(plan["count"]),
            "succeeded": successes,
            "failed": failures,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--plan-digest", default="")
    parser.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.expected_count < 0:
        raise SystemExit("--expected-count must not be negative")
    try:
        if arguments.dry_run:
            plan = build_plan(
                arguments.root,
                expected_count=arguments.expected_count,
            )
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        if not arguments.plan_digest:
            raise SurrogateSwapBatchError(
                "--run requires --plan-digest from a reviewed dry-run"
            )
        report = run_plan(
            arguments.root,
            actor=arguments.actor,
            reason=arguments.reason
            or f"Bound surrogate swap batch {arguments.plan_digest}",
            plan_digest=arguments.plan_digest,
            expected_count=arguments.expected_count,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if report["failed"] else 0
    except (
        SurrogateSwapBatchError,
        catalog_core.CoreCatalogError,
        reconciliation.ReconciliationError,
        tiff_accession.TiffAccessionError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    raise SystemExit(main())
