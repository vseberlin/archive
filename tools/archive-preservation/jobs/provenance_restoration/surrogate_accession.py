"""Create an exact surrogate-source copy and metadata-enriched TIFF representation."""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

import catalog_core
import tiff_accession
import tiff_metadata
from archive_core.layout import object_relative_path

if __package__:
    from .technical import verify_image_derivative
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from technical import verify_image_derivative


MEDIA_SUFFIXES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}


class SurrogateAccessionError(RuntimeError):
    """Raised when a derivative cannot become a preservation surrogate."""


def _write_tiff_representation(
    source: Path,
    candidate: Path,
    embedded: dict[str, str],
) -> None:
    tiff_accession._run(
        [
            "magick",
            str(source),
            "-strip",
            "-compress",
            "None",
            str(candidate),
        ]
    )
    arguments = ["exiftool", "-overwrite_original"]
    arguments.extend(
        f"-{tag}={value}" for tag, value in embedded.items()
    )
    arguments.append(str(candidate))
    result = tiff_accession._run(arguments)
    if "Warning:" in result.stderr:
        raise SurrogateAccessionError(
            f"ExifTool warning: {result.stderr.strip()}"
        )
    if not candidate.is_file():
        raise SurrogateAccessionError(
            "metadata-enriched surrogate TIFF was not created"
        )
    tiff_accession._fsync_file(candidate)


def _observed_mtime(path: Path) -> str:
    return datetime.fromtimestamp(
        path.stat().st_mtime,
        UTC,
    ).isoformat().replace("+00:00", "Z")


def _atomic_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                report,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_preservation_surrogate(
    root: Path,
    source: Path,
    source_media_type: str,
    entity_id: str,
    actor: str,
    *,
    provenance_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Preserve selected bytes and one pixel-equivalent V2 TIFF surrogate."""

    root = catalog_core.assert_safe_work_root(root)
    config = catalog_core.require_core_mode(root)
    entity_id = catalog_core.normalize_entity_id(entity_id)
    source = source.expanduser().resolve(strict=True)
    suffixes = MEDIA_SUFFIXES.get(source_media_type)
    if (
        not source.is_file()
        or not suffixes
        or source.suffix.casefold() not in suffixes
    ):
        raise SurrogateAccessionError(
            "surrogate source must be one JPEG, PNG, or WebP matching its "
            "declared media type"
        )
    source_before = tiff_accession._source_fingerprint(source)
    provenance = dict(provenance_metadata)
    declared_digest = str(provenance.get("source_digest") or "")
    if declared_digest and declared_digest != source_before["digest"]:
        raise SurrogateAccessionError(
            "surrogate provenance digest differs from selected source"
        )
    provenance["source_digest"] = source_before["digest"]
    try:
        source_technical = verify_image_derivative(source)
    except Exception as error:
        raise SurrogateAccessionError(str(error)) from error

    exiftool_version = tiff_accession._tool_version("exiftool")
    imagemagick_version = tiff_accession._tool_version("magick")
    catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    try:
        catalog_core.assert_catalog_integrity(catalog)
        catalog_core.require_filesystem_current(catalog)
        state = catalog_core.catalog_state(catalog)
        expected_revision = int(state["catalog_revision"])
        metadata = tiff_accession._catalog_metadata(
            catalog,
            entity_id,
            tiff_metadata.PROFILE_V2,
        )
        existing = catalog.execute(
            """SELECT ef.role FROM entity_files ef
               WHERE ef.entity_id = ?
                 AND ef.role IN (
                     'preservation_master',
                     'preservation_surrogate',
                     'preservation_surrogate_source'
                 )
                 AND ef.status = 'active'""",
            (entity_id,),
        ).fetchone()
        if existing:
            raise SurrogateAccessionError(
                "entity already has an active preservation representation"
            )
    finally:
        catalog.close()

    embedded = tiff_accession._expected_embedded(
        metadata,
        tiff_metadata.PROFILE_V2,
        provenance,
    )
    unit_id = str(metadata["unit_id"])
    inventory = str(metadata["archival_label"])
    representation_relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=inventory,
        entity_id=entity_id,
        role="preservation_surrogate",
        suffix=".tif",
    ).parts)
    selected_relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=inventory,
        entity_id=entity_id,
        role="access_derivative",
        suffix=source.suffix,
    ).parts)
    representation_name = representation_relative.name
    selected_name = selected_relative.name
    representation = root / representation_relative
    selected_copy = root / selected_relative

    preservation_text = str(
        config.get("independent_generation_root") or ""
    )
    if not preservation_text:
        raise SurrogateAccessionError(
            "independent preservation root is not configured"
        )
    preservation_root = catalog_core.assert_safe_work_root(
        Path(preservation_text)
    )
    preservation_root.mkdir(parents=True, exist_ok=True)
    catalog_core.assert_independent_device(root, preservation_root)
    independent_representation = (
        preservation_root / representation_relative
    )
    independent_selected = preservation_root / selected_relative
    destinations = (
        representation,
        selected_copy,
        independent_representation,
        independent_selected,
    )
    if any(path.exists() for path in destinations):
        raise SurrogateAccessionError(
            "preservation-surrogate destination already exists; replacement "
            "requires a separate accession decision"
        )

    operation_id = (
        "SURROGATE-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    staging = root / "work" / "accession-staging" / operation_id
    candidate = staging / representation_name
    staging.mkdir(parents=True)
    created: list[Path] = []
    catalog_registered = False
    backup_path: Path | None = None
    try:
        _write_tiff_representation(source, candidate, embedded)
        readback = tiff_accession._exif_values(
            candidate,
            tuple(embedded),
        )
        if readback != embedded:
            raise SurrogateAccessionError(
                "surrogate TIFF metadata read-back differs"
            )
        technical = tiff_accession._exif_values(
            candidate,
            tiff_accession.TECHNICAL_TAGS,
        )
        if (
            technical.get("File:FileType") != "TIFF"
            or tiff_accession._page_count(candidate) != 1
        ):
            raise SurrogateAccessionError(
                "surrogate representation is not one valid TIFF image"
            )
        try:
            bit_depth = tiff_accession._scalar_sample_depth(
                technical.get("IFD0:BitsPerSample")
            )
        except tiff_accession.TiffAccessionError as error:
            raise SurrogateAccessionError(str(error)) from error
        decoded_source = tiff_accession._decoded_pixel_digest(
            source,
            bit_depth,
        )
        decoded_representation = tiff_accession._decoded_pixel_digest(
            candidate,
            bit_depth,
        )
        if decoded_source != decoded_representation:
            raise SurrogateAccessionError(
                "decoded pixels changed while creating surrogate TIFF"
            )
        if (
            tiff_accession._source_fingerprint(source)
            != source_before
        ):
            raise SurrogateAccessionError(
                "selected derivative changed during accession"
            )

        representation_digest = tiff_accession._sha256(candidate)
        representation.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, representation)
        created.append(representation)
        tiff_accession._fsync_file(representation)
        tiff_accession._atomic_copy_verified(
            source,
            selected_copy,
            str(source_before["digest"]),
        )
        created.append(selected_copy)
        tiff_accession._atomic_copy_verified(
            representation,
            independent_representation,
            representation_digest,
        )
        created.append(independent_representation)
        tiff_accession._atomic_copy_verified(
            selected_copy,
            independent_selected,
            str(source_before["digest"]),
        )
        created.append(independent_selected)
        protections = [
            {
                "path": str(path),
                "control": tiff_accession._mark_read_only(path),
            }
            for path in created
        ]
        backup_path = tiff_accession._backup_catalog(
            root,
            entity_id,
            "SURROGATE",
        )
        catalog = catalog_core.connect_catalog(
            root / "work" / "catalog.sqlite"
        )
        try:
            result = catalog_core.register_preservation_surrogate(
                catalog,
                entity_id=entity_id,
                unit_id=unit_id,
                content_digest=representation_digest,
                size_bytes=representation.stat().st_size,
                relative_path=representation_relative.as_posix(),
                original_filename=representation_name,
                observed_mtime_utc=_observed_mtime(representation),
                width_px=int(technical["IFD0:ImageWidth"]),
                height_px=int(technical["IFD0:ImageHeight"]),
                bit_depth=bit_depth,
                color_space=str(
                    technical["IFD0:PhotometricInterpretation"]
                ),
                compression=str(technical["IFD0:Compression"]),
                page_count=1,
                source_digest=str(source_before["digest"]),
                decoded_pixel_digest=decoded_representation,
                embedding_profile=tiff_metadata.PROFILE_V2,
                exiftool_version=exiftool_version,
                imagemagick_version=imagemagick_version,
                expected_catalog_revision=expected_revision,
                archival_label=inventory,
                descriptive_title=str(metadata["descriptive_title"]),
                description=str(metadata["description"]),
                material_technique=str(metadata["material_technique"]),
                dimensions=str(metadata["dimensions"]),
                actor=actor,
                surrogate_source={
                    "content_digest": str(source_before["digest"]),
                    "size_bytes": int(source_before["size"]),
                    "relative_path": selected_relative.as_posix(),
                    "original_filename": selected_name,
                    "observed_mtime_utc": _observed_mtime(selected_copy),
                    "media_type": source_media_type,
                },
            )
            catalog_registered = True
        finally:
            catalog.close()
        report = {
            **result,
            "backup": str(backup_path),
            "decoded_pixel_digest": decoded_representation,
            "embedding_profile": tiff_metadata.PROFILE_V2,
            "embedded_metadata": embedded,
            "entity_id": entity_id,
            "exiftool_version": exiftool_version,
            "independent_selected_source": str(independent_selected),
            "independent_surrogate": str(independent_representation),
            "protections": protections,
            "provenance_gaps": ["missing_local_master"],
            "provenance_metadata": provenance,
            "selected_source": str(source),
            "selected_source_copy": str(selected_copy),
            "selected_source_digest": source_before["digest"],
            "selected_source_technical": source_technical,
            "surrogate": str(representation),
            "surrogate_digest": representation_digest,
            "technical_metadata": technical,
        }
        report_path = (
            root
            / "work"
            / "accession-reports"
            / f"{operation_id}-{entity_id}.json"
        )
        _atomic_report(report_path, report)
        report["report"] = str(report_path)
        return report
    except Exception:
        if not catalog_registered:
            for path in reversed(created):
                try:
                    os.chmod(path, 0o644)
                except OSError:
                    pass
                path.unlink(missing_ok=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass
