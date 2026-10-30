#!/usr/bin/env python3
"""Create and verify one metadata-enriched V2 preservation-master TIFF."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import catalog_core
import tiff_metadata
from archive_core.layout import object_relative_path


EMBEDDING_PROFILE = tiff_metadata.PROFILE_V1
TECHNICAL_TAGS = (
    "File:FileType",
    "IFD0:ImageWidth",
    "IFD0:ImageHeight",
    "IFD0:BitsPerSample",
    "IFD0:Compression",
    "IFD0:PhotometricInterpretation",
    "IFD0:SamplesPerPixel",
    "IFD0:PlanarConfiguration",
    "IFD0:XResolution",
    "IFD0:YResolution",
    "IFD0:ResolutionUnit",
    "IFD0:Orientation",
)
EMBEDDED_TAGS = (
    "XMP-dc:Identifier",
    "XMP-dc:Title",
    "XMP-dc:Description",
    "IPTC:CodedCharacterSet",
    "IPTC:ObjectName",
    "IPTC:Caption-Abstract",
    "IFD0:ImageDescription",
    "XMP-iptcExt:ArtworkSourceInventoryNo",
    "XMP-iptcExt:ArtworkTitle",
    "XMP-iptcExt:ArtworkContentDescription",
    "XMP-iptcExt:ArtworkPhysicalDescription",
)


class TiffAccessionError(RuntimeError):
    """Raised when TIFF creation or verification fails closed."""


def _sha256(path: Path) -> str:
    return f"sha256:{catalog_core.sha256_file(path)}"


def _source_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "digest": _sha256(path),
    }


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise TiffAccessionError(f"required tool is unavailable: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise TiffAccessionError(
            f"{arguments[0]} failed: {detail or error.returncode}"
        ) from error


def _tool_version(executable: str) -> str:
    if executable == "exiftool":
        return _run([executable, "-ver"]).stdout.strip()
    first = _run([executable, "-version"]).stdout.splitlines()
    return first[0].strip() if first else ""


def _exif_values(path: Path, tags: tuple[str, ...]) -> dict[str, Any]:
    arguments = ["exiftool", "-j", "-G1", "-s"]
    arguments.extend(f"-{tag}" for tag in tags)
    arguments.append(str(path))
    payload = json.loads(_run(arguments).stdout)
    if len(payload) != 1:
        raise TiffAccessionError(f"ExifTool returned no single record for {path}")
    payload[0].pop("SourceFile", None)
    return {
        key: (
            str(value)
            if value is not None and not isinstance(value, str)
            else value
        )
        for key, value in payload[0].items()
    }


def _decoded_pixel_digest(path: Path, bit_depth: int) -> str:
    try:
        process = subprocess.Popen(
            [
                "magick",
                str(path),
                "-depth",
                str(bit_depth),
                "RGBA:-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise TiffAccessionError("required tool is unavailable: magick") from error
    assert process.stdout is not None
    assert process.stderr is not None
    digest = hashlib.sha256()
    try:
        while chunk := process.stdout.read(8 * 1024 * 1024):
            digest.update(chunk)
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        returncode = process.wait()
    finally:
        process.stdout.close()
        process.stderr.close()
    if returncode:
        raise TiffAccessionError(
            f"ImageMagick pixel decoding failed: {stderr or returncode}"
        )
    return f"sha256:{digest.hexdigest()}"


def _page_count(path: Path) -> int:
    result = _run(["magick", "identify", "-format", "%p\n", str(path)])
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _scalar_sample_depth(value: Any) -> int:
    """Return one uniform sample depth from grayscale or multi-channel TIFF."""

    if isinstance(value, int):
        parts = [value]
    elif isinstance(value, str):
        try:
            parts = [
                int(part)
                for part in value.replace(",", " ").split()
                if part
            ]
        except ValueError as error:
            raise TiffAccessionError(
                "TIFF BitsPerSample is not numeric"
            ) from error
    elif isinstance(value, list) and all(
        isinstance(part, int) for part in value
    ):
        parts = value
    else:
        parts = []
    if (
        not parts
        or len(set(parts)) != 1
        or parts[0] not in (1, 2, 4, 8, 16)
    ):
        raise TiffAccessionError(
            "bounded TIFF accession requires one uniform scalar sample depth"
        )
    return parts[0]


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _atomic_copy_verified(source: Path, destination: Path, digest: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise TiffAccessionError(f"refusing to replace existing file: {destination}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        _fsync_file(temporary)
        if _sha256(temporary) != digest:
            raise TiffAccessionError(
                f"independent preservation copy failed fixity: {destination}"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _mark_read_only(path: Path) -> str:
    os.chmod(path, 0o444)
    if path.stat().st_mode & 0o222 == 0:
        return "posix-permissions"
    try:
        attributes = int.from_bytes(
            os.getxattr(path, "system.ntfs_attrib"),
            "little",
        )
        os.setxattr(
            path,
            "system.ntfs_attrib",
            (attributes | 0x1).to_bytes(4, "little"),
        )
        verified = int.from_bytes(
            os.getxattr(path, "system.ntfs_attrib"),
            "little",
        )
    except OSError as error:
        raise TiffAccessionError(
            f"filesystem cannot mark preservation master read-only: {path}"
        ) from error
    if verified & 0x1 == 0:
        raise TiffAccessionError(
            f"NTFS read-only attribute did not persist: {path}"
        )
    return "ntfs-read-only-attribute-with-fixity-sweep"


def _backup_catalog(
    root: Path,
    entity_id: str,
    representation: str = "MASTER",
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        root
        / "work"
        / "backups"
        / f"{stamp}-PRE-TIFF-{representation}-{entity_id}.sqlite"
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
            raise TiffAccessionError("pre-accession catalog backup failed integrity")
    finally:
        check.close()
    return destination


def _catalog_metadata(
    connection: sqlite3.Connection,
    entity_id: str,
    embedding_profile: str = EMBEDDING_PROFILE,
) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT e.entity_id, e.kind, e.status,
                  a.archival_label, a.descriptive_title, a.description,
                  a.material_technique, a.dimensions, a.publication_status,
                  m.unit_id, m.role AS membership_role, u.status AS unit_status
           FROM entities e
           JOIN archive_objects a ON a.entity_id = e.entity_id
           JOIN entity_unit_memberships m ON m.entity_id = e.entity_id
           JOIN units u ON u.unit_id = m.unit_id
           WHERE e.entity_id = ? AND m.role = 'primary'""",
        (entity_id,),
    ).fetchall()
    if len(rows) != 1:
        raise TiffAccessionError(
            "preservation master requires exactly one primary unit membership"
        )
    row = dict(rows[0])
    row["object_type"] = ""
    row["keywords"] = ""
    optional = connection.execute(
        """SELECT predicate, value_text
           FROM evidence_assertions
           WHERE subject_entity_id = ?
             AND predicate IN ('archive_object_type', 'archive_keyword')
             AND status = 'accepted'
           ORDER BY predicate, lower(value_text), value_text""",
        (entity_id,),
    ).fetchall()
    keywords = []
    for item in optional:
        if item["predicate"] == "archive_object_type":
            row["object_type"] = str(item["value_text"] or "")
        elif str(item["value_text"] or "").strip():
            keywords.append(str(item["value_text"]).strip())
    row["keywords"] = ", ".join(keywords)
    if (
        row["kind"] != "archive_object"
        or row["status"] != "active"
        or row["unit_status"] != "open"
    ):
        raise TiffAccessionError(
            "preservation master requires an active archive object in an open unit"
        )
    required = (
        (
            "archival_label",
            "descriptive_title",
            "description",
            "material_technique",
            "dimensions",
        )
        if embedding_profile == tiff_metadata.PROFILE_V1
        else ("archival_label",)
    )
    missing = [key for key in required if not str(row[key]).strip()]
    if missing:
        raise TiffAccessionError(
            f"canonical metadata is incomplete: {', '.join(missing)}"
        )
    return row


def _expected_embedded(
    metadata: dict[str, Any],
    embedding_profile: str = EMBEDDING_PROFILE,
    provenance_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    try:
        return tiff_metadata.expected_tags(
            embedding_profile,
            metadata,
            provenance_metadata,
        )
    except tiff_metadata.TiffMetadataError as error:
        raise TiffAccessionError(str(error)) from error


def _write_candidate(
    source: Path,
    candidate: Path,
    expected: dict[str, str],
) -> None:
    arguments = ["exiftool"]
    for tag, value in expected.items():
        arguments.append(f"-{tag}={value}")
    arguments.extend(["-o", str(candidate), str(source)])
    result = _run(arguments)
    if "Warning:" in result.stderr:
        raise TiffAccessionError(f"ExifTool warning: {result.stderr.strip()}")
    if not candidate.is_file():
        raise TiffAccessionError("ExifTool did not create the TIFF candidate")
    _fsync_file(candidate)


def create_preservation_master(
    root: Path,
    source: Path,
    entity_id: str,
    actor: str,
    *,
    embedding_profile: str = EMBEDDING_PROFILE,
    provenance_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = catalog_core.assert_safe_work_root(root)
    config = catalog_core.require_core_mode(root)
    entity_id = catalog_core.normalize_entity_id(entity_id)
    source = source.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.casefold() not in {".tif", ".tiff"}:
        raise TiffAccessionError("source must be one existing TIFF file")
    source_before = _source_fingerprint(source)
    provenance = dict(provenance_metadata or {})
    if embedding_profile == tiff_metadata.PROFILE_V2:
        declared_source_digest = str(
            provenance.get("source_digest") or ""
        )
        if (
            declared_source_digest
            and declared_source_digest != source_before["digest"]
        ):
            raise TiffAccessionError(
                "V2 provenance source digest differs from the selected TIFF"
            )
        provenance["source_digest"] = source_before["digest"]
    elif embedding_profile != tiff_metadata.PROFILE_V1:
        raise TiffAccessionError(
            f"unsupported TIFF embedding profile: {embedding_profile}"
        )
    exiftool_version = _tool_version("exiftool")
    imagemagick_version = _tool_version("magick")
    catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
    try:
        catalog_core.assert_catalog_integrity(catalog)
        catalog_core.require_filesystem_current(catalog)
        state = catalog_core.catalog_state(catalog)
        expected_revision = int(state["catalog_revision"])
        metadata = _catalog_metadata(
            catalog,
            entity_id,
            embedding_profile,
        )
        if catalog.execute(
            """SELECT 1 FROM entity_files
               WHERE entity_id = ? AND role = 'preservation_master'
                 AND status = 'active'""",
            (entity_id,),
        ).fetchone():
            raise TiffAccessionError(
                f"entity already has an active preservation master: {entity_id}"
            )
    finally:
        catalog.close()
    technical_source = _exif_values(source, TECHNICAL_TAGS)
    if technical_source.get("File:FileType") != "TIFF":
        raise TiffAccessionError("source is not structurally identified as TIFF")
    bit_depth = _scalar_sample_depth(
        technical_source.get("IFD0:BitsPerSample")
    )
    if _page_count(source) != 1:
        raise TiffAccessionError(
            "bounded TIFF accession currently requires one image directory"
        )
    expected_embedded = _expected_embedded(
        metadata,
        embedding_profile,
        provenance,
    )
    unit_id = str(metadata["unit_id"])
    relative = Path(*object_relative_path(
        unit_id=unit_id,
        inventory=str(metadata["archival_label"]),
        entity_id=entity_id,
        role="preservation_master",
        suffix=".tif",
    ).parts)
    output_name = relative.name
    final = root / relative
    preservation_text = str(config.get("independent_generation_root") or "")
    if not preservation_text:
        raise TiffAccessionError("independent preservation root is not configured")
    preservation_root = catalog_core.assert_safe_work_root(Path(preservation_text))
    preservation_root.mkdir(parents=True, exist_ok=True)
    catalog_core.assert_independent_device(root, preservation_root)
    independent = preservation_root / relative
    if final.exists() or independent.exists():
        raise TiffAccessionError(
            "preservation-master destination already exists; replacement "
            "requires a separate accession decision"
        )
    operation_id = f"TIFF-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
    staging = root / "work" / "accession-staging" / operation_id
    candidate = staging / output_name
    staging.mkdir(parents=True)
    primary_created = False
    independent_created = False
    catalog_registered = False
    backup_path: Path | None = None
    try:
        _write_candidate(source, candidate, expected_embedded)
        embedded = _exif_values(candidate, tuple(expected_embedded))
        if embedded != expected_embedded:
            raise TiffAccessionError(
                "embedded metadata read-back differs from the canonical catalog"
            )
        technical_candidate = _exif_values(candidate, TECHNICAL_TAGS)
        if technical_candidate != technical_source:
            raise TiffAccessionError(
                "TIFF technical structure changed during metadata embedding"
            )
        if _page_count(candidate) != 1:
            raise TiffAccessionError(
                "TIFF image-directory count changed during metadata embedding"
            )
        decoded_source = _decoded_pixel_digest(source, bit_depth)
        decoded_candidate = _decoded_pixel_digest(candidate, bit_depth)
        if decoded_candidate != decoded_source:
            raise TiffAccessionError(
                "decoded pixels changed during metadata embedding"
            )
        source_after = _source_fingerprint(source)
        if source_after != source_before:
            raise TiffAccessionError("source TIFF changed during accession")
        result_digest = _sha256(candidate)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, final)
        primary_created = True
        _fsync_file(final)
        _atomic_copy_verified(final, independent, result_digest)
        independent_created = True
        primary_protection = _mark_read_only(final)
        independent_protection = _mark_read_only(independent)
        backup_path = _backup_catalog(root, entity_id)
        final_stat = final.stat()
        observed_mtime = datetime.fromtimestamp(
            final_stat.st_mtime,
            UTC,
        ).isoformat().replace("+00:00", "Z")
        catalog = catalog_core.connect_catalog(root / "work" / "catalog.sqlite")
        try:
            result = catalog_core.register_preservation_master(
                catalog,
                entity_id=entity_id,
                unit_id=unit_id,
                content_digest=result_digest,
                size_bytes=final_stat.st_size,
                relative_path=relative.as_posix(),
                original_filename=output_name,
                observed_mtime_utc=observed_mtime,
                width_px=int(technical_candidate["IFD0:ImageWidth"]),
                height_px=int(technical_candidate["IFD0:ImageHeight"]),
                bit_depth=bit_depth,
                color_space=str(
                    technical_candidate["IFD0:PhotometricInterpretation"]
                ),
                compression=str(technical_candidate["IFD0:Compression"]),
                page_count=1,
                source_digest=str(source_before["digest"]),
                decoded_pixel_digest=decoded_candidate,
                embedding_profile=embedding_profile,
                exiftool_version=exiftool_version,
                imagemagick_version=imagemagick_version,
                expected_catalog_revision=expected_revision,
                archival_label=str(metadata["archival_label"]),
                descriptive_title=str(metadata["descriptive_title"]),
                description=str(metadata["description"]),
                material_technique=str(metadata["material_technique"]),
                dimensions=str(metadata["dimensions"]),
                actor=actor,
            )
            catalog_registered = True
        finally:
            catalog.close()
        report = {
            **result,
            "backup": str(backup_path),
            "decoded_pixel_digest": decoded_candidate,
            "embedding_profile": embedding_profile,
            "embedded_metadata": expected_embedded,
            "entity_id": entity_id,
            "exiftool_version": exiftool_version,
            "independent_copy": str(independent),
            "independent_protection": independent_protection,
            "master": str(final),
            "primary_protection": primary_protection,
            "source": str(source),
            "source_digest": source_before["digest"],
            "provenance_metadata": provenance,
            "technical_metadata": technical_candidate,
        }
        report_path = (
            root
            / "work"
            / "accession-reports"
            / f"{operation_id}-{entity_id}.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_report = report_path.with_suffix(".json.tmp")
        temporary_report.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _fsync_file(temporary_report)
        os.replace(temporary_report, report_path)
        report["report"] = str(report_path)
        return report
    except Exception:
        if independent_created and not catalog_registered:
            independent.unlink(missing_ok=True)
        if primary_created and not catalog_registered:
            final.unlink(missing_ok=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)
        try:
            staging.rmdir()
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument(
        "--profile",
        choices=tiff_metadata.SUPPORTED_PROFILES,
        default=EMBEDDING_PROFILE,
    )
    parser.add_argument(
        "--provenance-json",
        type=Path,
        help="JSON object required by iss-tiff-v2",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        provenance = (
            json.loads(
                arguments.provenance_json.read_text(encoding="utf-8")
            )
            if arguments.provenance_json
            else {}
        )
        if not isinstance(provenance, dict):
            raise TiffAccessionError(
                "provenance JSON must contain one object"
            )
        result = create_preservation_master(
            Path(arguments.root),
            Path(arguments.source),
            arguments.entity,
            arguments.actor,
            embedding_profile=arguments.profile,
            provenance_metadata=provenance,
        )
    except (
        catalog_core.CoreCatalogError,
        json.JSONDecodeError,
        OSError,
        TiffAccessionError,
    ) as error:
        raise SystemExit(f"error: {error}") from error
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
