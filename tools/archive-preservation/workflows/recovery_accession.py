"""Guarded, resumable accession for one reviewed recovery record."""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import subprocess
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import catalog_core
import tiff_accession
import tiff_metadata
from archive_core.catalog import write_unit_catalog
from archive_core.layout import object_relative_path
from jobs.provenance_restoration import surrogate_accession
from jobs.provenance_restoration.technical import verify_image_derivative
from workflows import editor_reconcile, recovery_metadata


class RecoveryAccessionError(RuntimeError):
    """Raised when a recovery record cannot pass the accession gates."""


GENERATED_ACCESS_JPEG_PROFILE = "libvips-full-resolution-jpeg-q92-v1"


def _digest(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def _observed_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _selection_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _assert_source(evidence: dict[str, Any]) -> Path:
    path = Path(str(evidence.get("source_locator") or "")).resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise RecoveryAccessionError("selected recovery source must be a regular file")
    expected = str(evidence.get("content_digest") or "")
    if _digest(path) != expected:
        raise RecoveryAccessionError(
            f"selected recovery source changed after review: {path.name}"
        )
    return path


def _ensure_exact_copy(source: Path, destination: Path, digest: str) -> None:
    if destination.exists():
        if not destination.is_file() or _digest(destination) != digest:
            raise RecoveryAccessionError(
                f"existing accession destination differs: {destination}"
            )
        return
    tiff_accession._atomic_copy_verified(source, destination, digest)
    tiff_accession._mark_read_only(destination)


def _ensure_generated_access_jpeg(
    source: Path,
    destination: Path,
    *,
    width_px: int,
    height_px: int,
    scratch: Path,
) -> tuple[str, dict[str, Any]]:
    """Create one full-resolution access JPEG without changing the TIFF."""
    if destination.exists():
        if not destination.is_file():
            raise RecoveryAccessionError(
                f"generated derivative destination is not a file: {destination}"
            )
        technical = verify_image_derivative(destination)
        return _digest(destination), technical
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp.jpg"
    environment = dict(os.environ)
    environment["TMPDIR"] = str(scratch)
    try:
        result = subprocess.run(
            [
                "vipsthumbnail",
                str(source),
                "--size",
                f"{width_px}x{height_px}",
                "--output",
                f"{temporary}[Q=92,optimize_coding=true,strip=false]",
                "--vips-concurrency=1",
                "--vips-disc-threshold=104857600",
                "--vips-cache-max=100",
                "--vips-cache-max-memory=268435456",
                "--vips-cache-max-files=32",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
            env=environment,
        )
    except FileNotFoundError as error:
        raise RecoveryAccessionError(
            "vipsthumbnail is required to generate the access JPEG"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RecoveryAccessionError(
            "access JPEG generation exceeded 30 minutes"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise RecoveryAccessionError(
            f"access JPEG generation failed: {detail or error.returncode}"
        ) from error
    try:
        if not temporary.is_file():
            raise RecoveryAccessionError(
                "libvips did not create the access JPEG"
            )
        technical = verify_image_derivative(temporary)
        if int(technical["width"]) < 1 or int(technical["height"]) < 1:
            raise RecoveryAccessionError(
                "generated access JPEG has invalid dimensions"
            )
        os.replace(temporary, destination)
        tiff_accession._mark_read_only(destination)
        return _digest(destination), technical
    finally:
        temporary.unlink(missing_ok=True)


def _existing_master_digest(
    root: Path,
    independent: Path,
    entity_id: str,
) -> str:
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as catalog:
        rows = catalog.execute(
            """SELECT ef.role, fo.relative_path, fo.content_digest
               FROM entity_files ef
               JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
               WHERE ef.entity_id=?
                 AND ef.role IN ('preservation_master',
                                 'preservation_surrogate',
                                 'access_derivative')
                 AND ef.status='active' AND fo.status='verified'""",
            (entity_id,),
        ).fetchall()
        registration = catalog.execute(
            """SELECT after_json
               FROM logbook
               WHERE entity_id=?
                 AND action IN (
                     'register_preservation_master',
                     'register_preservation_surrogate'
                 )
               ORDER BY at_utc DESC, log_id DESC
               LIMIT 1""",
            (entity_id,),
        ).fetchone()
    if not rows:
        return ""
    for row in rows:
        expected = str(row["content_digest"])
        for base in (root, independent):
            path = base / str(row["relative_path"])
            if not path.is_file() or _digest(path) != expected:
                raise RecoveryAccessionError(
                    "an existing preservation representation failed two-copy fixity"
                )
    if registration:
        payload = json.loads(str(registration["after_json"]) or "{}")
        source_digest = str(payload.get("source_digest") or "")
        if source_digest:
            return source_digest
    return str(rows[0]["content_digest"])


def accession_and_publish(
    root: Path,
    independent: Path,
    *,
    unit_id: str,
    entity_id: str,
    inventory: str,
    master_evidence: dict[str, Any],
    derivative_evidence: dict[str, Any] | None,
    museum_digital: dict[str, Any],
    actor: str,
    tool_version: str,
    reason: str = "",
) -> dict[str, Any]:
    """Accession one record while excluding every other process at this root."""

    root = catalog_core.assert_safe_work_root(root)
    lock_path = root / "work" / ".recovery-accession.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_handle:
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            raise RecoveryAccessionError(
                "another recovery accession is already running; wait for it to finish"
            ) from error
        try:
            return _accession_and_publish_locked(
                root,
                independent,
                unit_id=unit_id,
                entity_id=entity_id,
                inventory=inventory,
                master_evidence=master_evidence,
                derivative_evidence=derivative_evidence,
                museum_digital=museum_digital,
                actor=actor,
                tool_version=tool_version,
                reason=reason,
            )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _accession_and_publish_locked(
    root: Path,
    independent: Path,
    *,
    unit_id: str,
    entity_id: str,
    inventory: str,
    master_evidence: dict[str, Any],
    derivative_evidence: dict[str, Any] | None,
    museum_digital: dict[str, Any],
    actor: str,
    tool_version: str,
    reason: str = "",
) -> dict[str, Any]:
    """Run the already locked accession and publication lifecycle."""

    independent = catalog_core.assert_safe_work_root(independent)
    master = _assert_source(master_evidence)
    derivative = _assert_source(derivative_evidence) if derivative_evidence else None
    if derivative and derivative == master:
        raise RecoveryAccessionError("master and derivative selections must differ")
    if derivative and master.suffix.casefold() in {".tif", ".tiff"}:
        raise RecoveryAccessionError(
            "a TIFF master requires the uniformly generated archive derivative"
        )
    images = museum_digital.get("object_images") or []
    main_image = next(
        (
            image for image in images
            if isinstance(image, dict) and str(image.get("is_main") or "") == "j"
        ),
        images[0] if images and isinstance(images[0], dict) else {},
    )
    md_id = str(museum_digital.get("object_id") or "")
    selection = {
        "entity_id": entity_id,
        "inventory": inventory,
        "master_digest": str(master_evidence["content_digest"]),
        "master_evidence_id": str(master_evidence["evidence_id"]),
        "derivative_digest": (
            str(derivative_evidence["content_digest"]) if derivative_evidence else ""
        ),
        "derivative_evidence_id": (
            str(derivative_evidence["evidence_id"]) if derivative_evidence else ""
        ),
        "derivative_generation_profile": (
            GENERATED_ACCESS_JPEG_PROFILE
            if derivative_evidence is None
            and master.suffix.casefold() in {".tif", ".tiff"}
            else ""
        ),
        "museum_digital_id": md_id,
        "museum_digital_filename": str(
            main_image.get("filename_loc")
            or museum_digital.get("primary_image")
            or museum_digital.get("image")
            or museum_digital.get("filename")
            or ""
        ),
        "museum_digital_url": str(
            museum_digital.get("object_url")
            or museum_digital.get("url")
            or (f"https://berlin.museum-digital.de/object/{md_id}" if md_id else "")
            or ""
        ),
    }
    reviewed_digest = _selection_digest(selection)

    # Unit-wide archive keywords are local catalog assertions, not MD tags.
    # Ensure them before publishing and TIFF embedding so future accessions
    # cannot silently omit required discovery context.
    if md_id:
        with closing(
            catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        ) as catalog:
            for keyword in editor_reconcile.UNIT_DEFAULT_KEYWORDS.get(unit_id, ()):
                editor_reconcile.add_archive_keyword(
                    catalog,
                    unit_id=unit_id,
                    entity_id=entity_id,
                    value=keyword,
                    actor=actor,
                    evidence=f"unit:{unit_id}",
                )

    # Metadata embedding starts only from a published catalog revision.
    catalog_core.publish_generation(
        root,
        independent,
        tool_version=tool_version,
        actor=actor,
    )
    master_digest = _existing_master_digest(root, independent, entity_id)
    if master_digest and master_digest != str(master_evidence["content_digest"]):
        raise RecoveryAccessionError(
            "existing preservation representation does not match selected master"
        )
    if not master_digest:
        provenance = {
            "case_id": f"RECOVERY-{unit_id}-{inventory}",
            "source_file_id": str(master_evidence["evidence_id"]),
            "source_digest": str(master_evidence["content_digest"]),
            "reviewed_bundle_digest": reviewed_digest,
            "museum_digital_id": selection["museum_digital_id"],
            "representation_role": (
                "preservation_master"
                if master.suffix.casefold() in {".tif", ".tiff"}
                else "preservation_surrogate"
            ),
            "provenance_gaps": (
                []
                if master.suffix.casefold() in {".tif", ".tiff"}
                else ["missing_local_tiff_master"]
            ),
        }
        provenance.update(
            recovery_metadata.require_embedding_metadata(inventory)
        )
        if master.suffix.casefold() in {".tif", ".tiff"}:
            accession = tiff_accession.create_preservation_master(
                root,
                master,
                entity_id,
                actor,
                embedding_profile=tiff_metadata.PROFILE_V2,
                provenance_metadata=provenance,
            )
        else:
            media_type = mimetypes.guess_type(master.name)[0] or ""
            accession = surrogate_accession.create_preservation_surrogate(
                root,
                master,
                media_type,
                entity_id,
                actor,
                provenance_metadata=provenance,
            )
        master_digest = str(
            accession.get("selected_source_digest")
            or accession.get("content_digest")
            or master_evidence["content_digest"]
        )

    derivative_result: dict[str, Any] | None = None
    actual_derivative_digest = ""
    if master.suffix.casefold() not in {".tif", ".tiff"}:
        # The exact selected JPEG is already registered by surrogate accession
        # as the sole access derivative. A smaller competing derivative remains
        # evidence, not a second active payload representation.
        actual_derivative_digest = str(master_evidence["content_digest"])
        derivative_result = {
            "result": "ok",
            "representation_role": "access_derivative",
            "content_digest": actual_derivative_digest,
            "state": "registered_with_surrogate",
        }
    elif derivative and derivative_evidence:
        media_type = mimetypes.guess_type(derivative.name)[0] or ""
        technical = verify_image_derivative(derivative)
        relative = Path(*object_relative_path(
            unit_id=unit_id,
            inventory=inventory,
            entity_id=entity_id,
            role="access_derivative",
            suffix=derivative.suffix,
        ).parts)
        primary_copy = root / relative
        independent_copy = independent / relative
        derivative_digest = str(derivative_evidence["content_digest"])
        actual_derivative_digest = derivative_digest
        _ensure_exact_copy(derivative, primary_copy, derivative_digest)
        _ensure_exact_copy(derivative, independent_copy, derivative_digest)
        with closing(
            catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        ) as catalog:
            derivative_result = catalog_core.register_access_derivative(
                catalog,
                entity_id=entity_id,
                unit_id=unit_id,
                content_digest=derivative_digest,
                size_bytes=derivative.stat().st_size,
                relative_path=relative.as_posix(),
                original_filename=relative.name,
                observed_mtime_utc=_observed_mtime(primary_copy),
                media_type=media_type,
                width_px=int(technical["width"]),
                height_px=int(technical["height"]),
                master_digest=master_digest,
                museum_digital_id=selection["museum_digital_id"],
                museum_digital_filename=selection["museum_digital_filename"],
                museum_digital_url=selection["museum_digital_url"],
                actor=actor,
            )
    elif master.suffix.casefold() in {".tif", ".tiff"}:
        relative = Path(*object_relative_path(
            unit_id=unit_id,
            inventory=inventory,
            entity_id=entity_id,
            role="access_derivative",
            suffix=".jpg",
        ).parts)
        primary_copy = root / relative
        independent_copy = independent / relative
        generated_digest, technical = _ensure_generated_access_jpeg(
            master,
            primary_copy,
            width_px=int(master_evidence.get("width_px") or 100000),
            height_px=int(master_evidence.get("height_px") or 100000),
            scratch=root / "work" / "accession-staging" / entity_id,
        )
        actual_derivative_digest = generated_digest
        _ensure_exact_copy(primary_copy, independent_copy, generated_digest)
        with closing(
            catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        ) as catalog:
            derivative_result = catalog_core.register_access_derivative(
                catalog,
                entity_id=entity_id,
                unit_id=unit_id,
                content_digest=generated_digest,
                size_bytes=primary_copy.stat().st_size,
                relative_path=relative.as_posix(),
                original_filename=relative.name,
                observed_mtime_utc=_observed_mtime(primary_copy),
                media_type="image/jpeg",
                width_px=int(technical["width"]),
                height_px=int(technical["height"]),
                master_digest=master_digest,
                museum_digital_id=selection["museum_digital_id"],
                museum_digital_filename=selection["museum_digital_filename"],
                museum_digital_url=selection["museum_digital_url"],
                actor=actor,
            )

    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    ) as catalog:
        catalog_core.record_recovery_accession_provenance(
            catalog,
            entity_id=entity_id,
            unit_id=unit_id,
            selection_digest=reviewed_digest,
            master_digest=master_digest,
            derivative_digest=actual_derivative_digest,
            museum_digital_id=selection["museum_digital_id"],
            actor=actor,
            reason=reason,
        )
    publication = catalog_core.publish_generation(
        root,
        independent,
        tool_version=tool_version,
        actor=actor,
    )
    with closing(
        catalog_core.connect_catalog(root / "work" / "catalog.sqlite", read_only=True)
    ) as catalog:
        primary_catalog = write_unit_catalog(catalog, unit_id, root / "units" / unit_id)
        independent_catalog = write_unit_catalog(
            catalog, unit_id, independent / "units" / unit_id
        )
    if (
        primary_catalog["csv_sha256"] != independent_catalog["csv_sha256"]
        or primary_catalog["xlsx_sha256"] != independent_catalog["xlsx_sha256"]
    ):
        raise RecoveryAccessionError("independent human-readable catalog differs")
    return {
        "result": "ok",
        "entity_id": entity_id,
        "inventory": inventory,
        "master_digest": master_digest,
        "derivative": derivative_result,
        "publication": publication,
        "unit_catalog": primary_catalog,
        "independent_unit_catalog": independent_catalog,
        "reviewed_selection_digest": reviewed_digest,
    }
