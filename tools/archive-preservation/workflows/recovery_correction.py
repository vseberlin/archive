"""Controlled replacement of one reviewed recovery representation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import catalog_core
import reconciliation
import tiff_accession
import tiff_metadata
from archive_core.catalog import write_unit_catalog
from archive_core.layout import object_relative_path
from jobs.provenance_restoration import surrogate_accession
from jobs.provenance_restoration.technical import verify_image_derivative
from workflows import recovery_accession, recovery_metadata


CAMPAIGN_ID = "MD-RECOVERY-WF-PARAGON"
CORRECTION_TOOL_VERSION = "recovery-correction-1.0.0"


class RecoveryCorrectionError(RuntimeError):
    """Raised when a corrective re-accession cannot pass every guard."""


def _digest(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def _selection_digest(payload: dict[str, Any]) -> str:
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
            raise RecoveryCorrectionError(
                f"database backup failed integrity check: {destination}"
            )
    finally:
        check.close()


def _selected_source(
    root: Path,
    independent: Path,
    *,
    evidence: sqlite3.Row,
    catalog: sqlite3.Connection,
) -> Path:
    """Resolve one reviewed source, including a retired-path package fallback."""

    expected = str(evidence["content_digest"])
    historical = Path(str(evidence["source_locator"] or ""))
    if historical.is_file() and _digest(historical) == expected:
        return historical.resolve()
    rows = catalog.execute(
        """SELECT DISTINCT fo.relative_path
           FROM file_occurrences fo
           WHERE fo.content_digest=? AND fo.status='verified'
           ORDER BY fo.relative_path""",
        (expected,),
    ).fetchall()
    for row in rows:
        for base in (root, independent):
            candidate = base / str(row["relative_path"])
            if candidate.is_file() and _digest(candidate) == expected:
                return candidate.resolve()
    raise RecoveryCorrectionError(
        "reviewed source bytes are unavailable in evidence or package storage"
    )


def _main_md_image(record: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(str(record["external_json"] or "{}"))
    except (KeyError, IndexError, json.JSONDecodeError):
        payload = {}
    images = payload.get("object_images") or []
    return next(
        (
            image
            for image in images
            if isinstance(image, dict) and str(image.get("is_main") or "") == "j"
        ),
        images[0] if images and isinstance(images[0], dict) else {},
    )


def _active_rows(
    catalog: sqlite3.Connection,
    entity_id: str,
) -> list[sqlite3.Row]:
    return catalog.execute(
        """SELECT ef.role, fo.occurrence_id, fo.content_digest,
                  fo.relative_path, fo.original_filename
           FROM entity_files ef
           JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
           WHERE ef.entity_id=? AND ef.status='active'
             AND ef.role IN (
                 'preservation_master', 'preservation_surrogate',
                 'preservation_surrogate_source', 'access_derivative'
             )
             AND fo.status='verified'
           ORDER BY ef.role, fo.occurrence_id""",
        (entity_id,),
    ).fetchall()


def _registered_source_digest(
    catalog: sqlite3.Connection,
    entity_id: str,
) -> str:
    row = catalog.execute(
        """SELECT after_json FROM logbook
           WHERE entity_id=?
             AND action IN (
                 'register_preservation_master',
                 'register_preservation_surrogate'
             )
           ORDER BY at_utc DESC, log_id DESC LIMIT 1""",
        (entity_id,),
    ).fetchone()
    if not row:
        return ""
    try:
        return str(json.loads(str(row["after_json"] or "{}")).get("source_digest") or "")
    except json.JSONDecodeError:
        return ""


def _copy_backup(
    source: Path,
    destination: Path,
    expected: str,
) -> None:
    if not source.is_file() or _digest(source) != expected:
        raise RecoveryCorrectionError(f"active representation failed fixity: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _digest(destination) != expected:
        raise RecoveryCorrectionError(f"representation backup failed: {destination}")


def _atomic_install(source: Path, destination: Path, expected: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        if _digest(temporary) != expected:
            raise RecoveryCorrectionError(
                f"staged replacement failed fixity: {destination}"
            )
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
        tiff_accession._mark_read_only(destination)
        with destination.open("rb") as handle:
            os.fsync(handle.fileno())
    finally:
        temporary.unlink(missing_ok=True)


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
    row = catalog.execute(
        "SELECT size_bytes FROM content_blobs WHERE content_digest=?",
        (digest,),
    ).fetchone()
    if row:
        if int(row["size_bytes"]) != int(size_bytes):
            raise RecoveryCorrectionError("replacement blob conflicts with catalog")
        return
    catalog.execute(
        """INSERT INTO content_blobs (
               content_digest, size_bytes, media_type, width_px, height_px,
               bit_depth, color_space, compression, page_count, duration_ms,
               created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)""",
        (
            digest,
            int(size_bytes),
            media_type,
            int(width_px),
            int(height_px),
            bit_depth,
            color_space,
            compression,
            now,
        ),
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
    occurrence_id = f"OCC-{uuid.uuid4().hex.upper()}"
    name = Path(relative_path).name
    catalog.execute(
        """INSERT INTO file_occurrences (
               occurrence_id, unit_id, batch_id, content_digest,
               occurrence_kind, source_root_id, relative_path,
               original_filename, observed_mtime_utc, observed_at_utc,
               status, note
           ) VALUES (?, ?, NULL, ?, 'archive', '', ?, ?, ?, ?,
                     'verified', ?)""",
        (
            occurrence_id,
            unit_id,
            digest,
            relative_path,
            name,
            observed_mtime,
            now,
            note,
        ),
    )
    catalog.execute(
        """INSERT INTO entity_files (
               entity_id, occurrence_id, role, status, created_at
           ) VALUES (?, ?, ?, 'active', ?)""",
        (entity_id, occurrence_id, role, now),
    )
    return occurrence_id


def _candidate_reviews(
    catalog: sqlite3.Connection,
    campaign: sqlite3.Connection,
    record_id: str,
    entity_id: str,
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    reviews = catalog_core.source_candidate_reviews(catalog, entity_id=entity_id)
    evidence = list(
        campaign.execute(
            """SELECT * FROM visual_evidence
               WHERE record_id=? AND role<>'museum_digital_snapshot'""",
            (record_id,),
        )
    )
    masters = [
        row
        for row in evidence
        if str(reviews.get(str(row["evidence_id"]), {}).get("state") or "")
        == "selected_master"
    ]
    derivatives = [
        row
        for row in evidence
        if str(reviews.get(str(row["evidence_id"]), {}).get("state") or "")
        == "selected_derivative"
    ]
    if len(masters) != 1:
        raise RecoveryCorrectionError(
            "corrective accession requires exactly one selected master"
        )
    if len(derivatives) > 1:
        raise RecoveryCorrectionError(
            "corrective accession has multiple selected derivatives"
        )
    return masters[0], derivatives


def plan(
    root: Path,
    *,
    inventory: str,
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    config = catalog_core.require_core_mode(root)
    independent = catalog_core.assert_safe_work_root(
        Path(str(config["independent_generation_root"])).resolve(strict=True)
    )
    catalog_path = root / "work" / "catalog.sqlite"
    campaign_path = root / "work" / "reconciliation" / "museum-digital.sqlite"
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        entity = catalog.execute(
            """SELECT entity_id FROM archive_objects
               WHERE archival_label=? AND status='active'""",
            (inventory,),
        ).fetchone()
        if not entity:
            raise RecoveryCorrectionError("inventory is not an active archive object")
        entity_id = str(entity["entity_id"])
        correction = catalog_core.recovery_correction_states(catalog).get(
            entity_id, {}
        )
        if correction.get("state") != "ready":
            raise RecoveryCorrectionError(
                f"{inventory} is not ready for corrective re-accession"
            )
        active = _active_rows(catalog, entity_id)
        registered_source = _registered_source_digest(catalog, entity_id)
        with closing(reconciliation.connect_campaign(root, read_only=True)) as campaign:
            record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE campaign_id=? AND inventory_label=?""",
                (CAMPAIGN_ID, inventory),
            ).fetchone()
            if not record or record["identity_status"] != "resolved":
                raise RecoveryCorrectionError("visual identity is not resolved")
            master, derivatives = _candidate_reviews(
                catalog, campaign, str(record["record_id"]), entity_id
            )
        source = _selected_source(
            root,
            independent,
            evidence=master,
            catalog=catalog,
        )
    role = (
        "preservation_master"
        if source.suffix.casefold() in {".tif", ".tiff"}
        else "preservation_surrogate"
    )
    payload = {
        "inventory": inventory,
        "entity_id": entity_id,
        "master_evidence_id": str(master["evidence_id"]),
        "source_digest": str(master["content_digest"]),
        "source_path": str(source),
        "representation_role": role,
        "selected_derivatives": [str(row["evidence_id"]) for row in derivatives],
        "active": [
            {
                "role": str(row["role"]),
                "digest": str(row["content_digest"]),
                "relative_path": str(row["relative_path"]),
            }
            for row in active
        ],
        "registered_source_digest": registered_source,
        "embedding_metadata": recovery_metadata.require_embedding_metadata(
            inventory
        ),
        "embedding_mapping": tiff_metadata.V2_MAPPING_VERSION,
    }
    payload["plan_digest"] = _selection_digest(payload)
    return payload


def apply(
    root: Path,
    *,
    inventory: str,
    actor: str,
    reason: str,
    expected_plan_digest: str = "",
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root.resolve(strict=True))
    config = catalog_core.require_core_mode(root)
    independent = catalog_core.assert_safe_work_root(
        Path(str(config["independent_generation_root"])).resolve(strict=True)
    )
    catalog_core.assert_independent_device(root, independent)
    lock_path = root / "work" / ".recovery-correction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryCorrectionError(
                "another corrective accession is already running"
            ) from error
        try:
            return _apply_locked(
                root,
                independent,
                inventory=inventory,
                actor=actor,
                reason=reason,
                expected_plan_digest=expected_plan_digest,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _apply_locked(
    root: Path,
    independent: Path,
    *,
    inventory: str,
    actor: str,
    reason: str,
    expected_plan_digest: str,
) -> dict[str, Any]:
    reviewed = plan(root, inventory=inventory)
    if expected_plan_digest and reviewed["plan_digest"] != expected_plan_digest:
        raise RecoveryCorrectionError("corrective selection changed after review")
    entity_id = str(reviewed["entity_id"])
    catalog_path = root / "work" / "catalog.sqlite"
    campaign_path = root / "work" / "reconciliation" / "museum-digital.sqlite"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = root / "work" / "backups" / (
        f"{stamp}-PRE-CORRECTIVE-{inventory}"
    )
    backup_dir.mkdir(parents=True)
    _backup_database(catalog_path, backup_dir / "catalog.sqlite")
    _backup_database(campaign_path, backup_dir / "museum-digital.sqlite")
    exception_manifest = (
        root / "work" / "recovery" / "WF-PARAGON"
        / "reconciliation-exceptions.json"
    )
    if exception_manifest.is_file():
        shutil.copy2(exception_manifest, backup_dir / exception_manifest.name)

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
    (backup_dir / "plan.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Candidate embedding starts from a published catalog revision. Avoid a
    # duplicate generation when the preceding serial correction already left
    # the catalog clean.
    with closing(
        catalog_core.connect_catalog(catalog_path, read_only=True)
    ) as catalog:
        state = catalog_core.catalog_state(catalog)
        catalog_is_clean = (
            state["catalog_revision"] == state["published_revision"]
            and state["dirty_since"] is None
        )
    if not catalog_is_clean:
        catalog_core.publish_generation(
            root,
            independent,
            tool_version=CORRECTION_TOOL_VERSION,
            actor=actor,
        )
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        state = catalog_core.catalog_state(catalog)
        if state["catalog_revision"] != state["published_revision"]:
            raise RecoveryCorrectionError("catalog is not clean before correction")
        expected_revision = int(state["catalog_revision"])
        metadata = tiff_accession._catalog_metadata(
            catalog, entity_id, tiff_metadata.PROFILE_V2
        )
        active_rows = _active_rows(catalog, entity_id)
        registered_source = _registered_source_digest(catalog, entity_id)
        with closing(reconciliation.connect_campaign(root, read_only=True)) as campaign:
            record = campaign.execute(
                """SELECT * FROM reconciliation_records
                   WHERE campaign_id=? AND inventory_label=?""",
                (CAMPAIGN_ID, inventory),
            ).fetchone()
            master, _derivatives = _candidate_reviews(
                catalog, campaign, str(record["record_id"]), entity_id
            )
        source = _selected_source(
            root, independent, evidence=master, catalog=catalog
        )
    source_digest = str(master["content_digest"])
    if _digest(source) != source_digest:
        raise RecoveryCorrectionError("selected source changed before correction")
    is_tiff = source.suffix.casefold() in {".tif", ".tiff"}
    role = "preservation_master" if is_tiff else "preservation_surrogate"
    md_id = str(record["external_id"] or "")
    main_image = _main_md_image(record)
    provenance = {
        "case_id": f"RECOVERY-WF-PARAGON-{inventory}",
        "source_file_id": str(master["evidence_id"]),
        "source_digest": source_digest,
        "reviewed_bundle_digest": str(reviewed["plan_digest"]),
        "museum_digital_id": md_id,
        "representation_role": role,
        "provenance_gaps": [] if is_tiff else ["missing_local_tiff_master"],
    }
    provenance.update(recovery_metadata.require_embedding_metadata(inventory))
    expected_tags = tiff_accession._expected_embedded(
        metadata, tiff_metadata.PROFILE_V2, provenance
    )
    old_master = next(
        (
            row for row in active_rows
            if row["role"] in ("preservation_master", "preservation_surrogate")
        ),
        None,
    )
    old_derivative = next(
        (row for row in active_rows if row["role"] == "access_derivative"),
        None,
    )
    if not old_master or not old_derivative:
        raise RecoveryCorrectionError(
            "corrective accession requires active master and derivative"
        )
    old_master_path = root / str(old_master["relative_path"])
    readback = tiff_accession._exif_values(old_master_path, tuple(expected_tags))
    no_op = (
        str(old_master["role"]) == role
        and registered_source == source_digest
        and readback == expected_tags
        and (
            not is_tiff
            and str(old_derivative["content_digest"]) == source_digest
        )
    )
    if no_op:
        with closing(catalog_core.connect_catalog(catalog_path)) as catalog:
            catalog_core.set_recovery_correction_status(
                catalog,
                entity_id=entity_id,
                state="complete",
                actor=actor,
                reason=reason,
            )
            catalog_core.set_reconciliation_review_status(
                catalog,
                entity_id=entity_id,
                status="reconciled",
                actor=actor,
                reason=reason,
            )
        publication = catalog_core.publish_generation(
            root,
            independent,
            tool_version=CORRECTION_TOOL_VERSION,
            actor=actor,
        )
        return {
            "result": "ok",
            "inventory": inventory,
            "operation": "validated_no_rewrite",
            "backup": str(backup_dir),
            "publication": publication,
        }

    operation_id = f"CORRECTION-{stamp}-{inventory}"
    staging = root / "work" / "accession-staging" / operation_id
    staging.mkdir(parents=True)
    staged_master = staging / "master.tif"
    if is_tiff:
        source_technical = tiff_accession._exif_values(
            source, tiff_accession.TECHNICAL_TAGS
        )
        bit_depth = tiff_accession._scalar_sample_depth(
            source_technical.get("IFD0:BitsPerSample")
        )
        tiff_accession._write_candidate(source, staged_master, expected_tags)
        if tiff_accession._page_count(source) != 1:
            raise RecoveryCorrectionError("selected TIFF is not single-page")
    else:
        source_technical = verify_image_derivative(source)
        surrogate_accession._write_tiff_representation(
            source, staged_master, expected_tags
        )
        staged_technical = tiff_accession._exif_values(
            staged_master, tiff_accession.TECHNICAL_TAGS
        )
        bit_depth = tiff_accession._scalar_sample_depth(
            staged_technical.get("IFD0:BitsPerSample")
        )
    if (
        tiff_accession._exif_values(staged_master, tuple(expected_tags))
        != expected_tags
    ):
        raise RecoveryCorrectionError("replacement TIFF metadata read-back differs")
    technical = tiff_accession._exif_values(
        staged_master, tiff_accession.TECHNICAL_TAGS
    )
    decoded_source = tiff_accession._decoded_pixel_digest(source, bit_depth)
    decoded_master = tiff_accession._decoded_pixel_digest(staged_master, bit_depth)
    if decoded_source != decoded_master:
        raise RecoveryCorrectionError("decoded pixels changed during correction")
    master_digest = _digest(staged_master)

    staged_derivative = staging / "access.jpg"
    if is_tiff:
        derivative_digest, derivative_technical = (
            recovery_accession._ensure_generated_access_jpeg(
                source,
                staged_derivative,
                width_px=int(technical["IFD0:ImageWidth"]),
                height_px=int(technical["IFD0:ImageHeight"]),
                scratch=staging / "scratch",
            )
        )
    else:
        shutil.copy2(source, staged_derivative)
        derivative_digest = source_digest
        derivative_technical = verify_image_derivative(staged_derivative)
    if _digest(staged_derivative) != derivative_digest:
        raise RecoveryCorrectionError("replacement derivative failed fixity")

    unit_id = str(metadata["unit_id"])
    master_relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=inventory,
        entity_id=entity_id,
        role=role,
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
        _atomic_install(
            staged_derivative,
            base / derivative_relative,
            derivative_digest,
        )

    now = catalog_core.utc_now()
    observed_master = datetime.fromtimestamp(
        (root / master_relative).stat().st_mtime, UTC
    ).isoformat().replace("+00:00", "Z")
    observed_derivative = datetime.fromtimestamp(
        (root / derivative_relative).stat().st_mtime, UTC
    ).isoformat().replace("+00:00", "Z")
    catalog = catalog_core.connect_catalog(catalog_path)
    try:
        catalog.execute("BEGIN IMMEDIATE")
        state = catalog_core.catalog_state(catalog)
        if (
            int(state["catalog_revision"]) != expected_revision
            or int(state["published_revision"]) != expected_revision
        ):
            raise RecoveryCorrectionError(
                "catalog changed after replacement embedding began"
            )
        current = _active_rows(catalog, entity_id)
        if {
            str(row["occurrence_id"]) for row in current
        } != {
            str(row["occurrence_id"]) for row in active_rows
        }:
            raise RecoveryCorrectionError("active representations changed")
        catalog.execute(
            """UPDATE entity_files SET status='superseded'
               WHERE entity_id=? AND status='active'
                 AND role IN (
                     'preservation_master', 'preservation_surrogate',
                     'preservation_surrogate_source', 'access_derivative'
                 )""",
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
            role=role,
            digest=master_digest,
            relative_path=master_relative.as_posix(),
            observed_mtime=observed_master,
            note=(
                f"controlled corrective {role}; profile "
                f"{tiff_metadata.PROFILE_V2}; source {source_digest}; "
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
                    "derivation": (
                        "generated_from_lossless_master"
                        if is_tiff else "exact_selected_jpeg_source"
                    ),
                    "master_digest": master_digest,
                    "museum_digital_id": md_id,
                    "museum_digital_filename": str(
                        main_image.get("filename_loc") or ""
                    ),
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
            "source_digest": source_digest,
            "width_px": int(technical["IFD0:ImageWidth"]),
        }
        catalog_core._append_log(
            catalog,
            actor,
            (
                "register_preservation_master"
                if is_tiff else "register_preservation_surrogate"
            ),
            "entity_file",
            entity_id,
            unit_id,
            registration,
            evidence=master_digest,
            note="controlled corrective re-accession",
            at_utc=now,
        )
        catalog_core._append_log(
            catalog,
            actor,
            "replace_recovery_representation",
            "entity_file",
            entity_id,
            unit_id,
            {
                "inventory": inventory,
                "master_digest": master_digest,
                "master_occurrence_id": master_occurrence,
                "derivative_digest": derivative_digest,
                "derivative_occurrence_id": derivative_occurrence,
                "plan_digest": reviewed["plan_digest"],
                "source_digest": source_digest,
                "superseded_occurrence_ids": [
                    str(row["occurrence_id"]) for row in active_rows
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
            evidence=str(reviewed["plan_digest"]),
            note=reason,
            at_utc=now,
        )
        catalog.commit()
    except Exception:
        catalog.rollback()
        raise
    finally:
        catalog.close()

    # Old role paths are removed only after both new copies and catalog links exist.
    keep = {master_relative.as_posix(), derivative_relative.as_posix()}
    for row in active_rows:
        relative = str(row["relative_path"])
        if relative in keep:
            continue
        for base in (root, independent):
            path = base / relative
            if path.exists():
                os.chmod(path, 0o644)
                path.unlink()

    with closing(catalog_core.connect_catalog(catalog_path)) as catalog:
        catalog_core.set_recovery_correction_status(
            catalog,
            entity_id=entity_id,
            state="complete",
            actor=actor,
            reason=reason,
        )
        catalog_core.set_reconciliation_review_status(
            catalog,
            entity_id=entity_id,
            status="reconciled",
            actor=actor,
            reason=reason,
        )
    publication = catalog_core.publish_generation(
        root,
        independent,
        tool_version=CORRECTION_TOOL_VERSION,
        actor=actor,
    )
    with closing(catalog_core.connect_catalog(catalog_path, read_only=True)) as catalog:
        catalog_core.assert_catalog_integrity(catalog)
        fixity = catalog_core.verify_preservation_masters(
            catalog, root, independent
        )
        primary_catalog = write_unit_catalog(
            catalog, unit_id, root / "units" / unit_id
        )
        independent_catalog = write_unit_catalog(
            catalog, unit_id, independent / "units" / unit_id
        )
    if (
        primary_catalog["csv_sha256"] != independent_catalog["csv_sha256"]
        or primary_catalog["xlsx_sha256"] != independent_catalog["xlsx_sha256"]
    ):
        raise RecoveryCorrectionError("unit catalogs differ after correction")
    return {
        "result": "ok",
        "inventory": inventory,
        "operation": "replaced",
        "representation_role": role,
        "master_digest": master_digest,
        "derivative_digest": derivative_digest,
        "backup": str(backup_dir),
        "publication": publication,
        "fixity": fixity,
    }
