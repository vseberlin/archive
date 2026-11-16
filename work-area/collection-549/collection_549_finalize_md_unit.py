#!/usr/bin/env python3
"""Finalize Objektsammlung as ten MD objects with one derivative each.

The correction is deliberately narrow:

* keep the ten accepted Masters;
* keep only their ten generated access derivatives in ``export``;
* embed the existing museum-digital object metadata in both representations;
* retain drain/source-disposition rows only for accepted Master sources;
* never write to the protected Elements source.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image


SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-549")
RECON = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
UNIT = Path("/mnt/volume/iss-archive/units/objektsammlung")
PROFILE = "iss-objektsammlung-metadata-v1"
ALLOWED_DIRS = {
    "00_MANIFESTS",
    "masters",
    "export",
    "metadata",
    "documents",
    "db-restore",
}
RETAINED_DRAIN_RELATIONSHIPS = {"selected_master_source", "redundant_local_copy"}
COPY_TO_DERIVATIVE = (
    "XMP-dc:Creator",
    "XMP-dc:Rights",
    "XMP-dc:Subject",
    "IPTC:By-line",
    "IPTC:CopyrightNotice",
    "IPTC:Credit",
    "IPTC:Keywords",
    "IFD0:Copyright",
)


class CorrectionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fields,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def normalized(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else "; ".join(str(item) for item in value)
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def guarded_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise CorrectionError("Elements did not resolve to one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise CorrectionError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def sqlite_state(path: Path) -> tuple[dict[str, int], str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        counts = {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in ("objects", "representations", "documents", "source_disposition")
        }
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    return counts, quick_check, foreign_keys


def exif_values(path: Path, tags: list[str] | tuple[str, ...]) -> dict[str, object]:
    result = subprocess.run(
        ["exiftool", "-j", "-G1", "-s", *[f"-{tag}" for tag in tags], str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise CorrectionError(f"ExifTool returned an unexpected result for {path}")
    return rows[0]


def existing_description(path: Path) -> str:
    return normalized(exif_values(path, ["XMP-dc:Description"]).get("XMP-dc:Description"))


def merged_description(existing: str, canonical: str) -> str:
    existing = normalized(existing).strip()
    canonical = normalized(canonical).strip()
    if not existing:
        return canonical
    if not canonical or canonical in existing:
        return existing
    return f"{existing}\n-----\n{canonical}"


def metadata_tags(
    metadata: dict[str, str],
    role: str,
    description: str,
    master_relative: str,
) -> dict[str, str]:
    md_id = metadata["md_object_id"]
    inventory = metadata["inventory"]
    object_url = metadata["object_url"]
    relation = (
        f"iss:unit:objektsammlung; museum-digital:object:{md_id}; "
        f"{object_url}; iss:representation:{role}"
    )
    source = f"museum-digital:berlin object {md_id}; {object_url}"
    if role == "generated_access_derivative":
        source = f"Generated from {master_relative}; metadata from {object_url}"
    physical = "; ".join(
        value
        for value in (
            f"Material/Technik: {metadata['material_technique']}"
            if metadata["material_technique"]
            else "",
            f"Maße: {metadata['dimensions']}" if metadata["dimensions"] else "",
        )
        if value
    )
    tags = {
        "XMP-dc:Identifier": inventory,
        "XMP-dc:Title": metadata["title"],
        "XMP-dc:Description": description,
        "XMP-dc:Type": metadata["object_type"],
        "XMP-dc:Source": source,
        "XMP-dc:Relation": relation,
        "XMP-xmp:Label": PROFILE,
        "XMP-iptcExt:ArtworkSourceInventoryNo": inventory,
        "XMP-iptcExt:ArtworkTitle": metadata["title"],
        "XMP-iptcExt:ArtworkContentDescription": normalized(metadata["description"]).strip(),
        "XMP-iptcExt:ArtworkPhysicalDescription": physical,
        "XMP-iptcExt:ArtworkCircaDateCreated": metadata["date"],
    }
    if len(metadata["title"].encode("utf-8")) <= 64:
        tags["IPTC:CodedCharacterSet"] = "UTF8"
        tags["IPTC:ObjectName"] = metadata["title"]
    if description and len(description.encode("utf-8")) <= 2000:
        tags["IPTC:CodedCharacterSet"] = "UTF8"
        tags["IPTC:Caption-Abstract"] = description
    tags["IPTC:Source"] = "museum-digital:berlin"
    return {key: value for key, value in tags.items() if value}


def pdf_info_tags(metadata: dict[str, str]) -> dict[str, str]:
    keywords = "; ".join(
        value
        for value in (
            metadata["inventory"],
            f"museum-digital object {metadata['md_object_id']}",
            metadata["object_type"],
            metadata["date"],
        )
        if value
    )
    return {
        "PDF:Title": metadata["title"],
        "PDF:Subject": normalized(metadata["description"]).strip(),
        "PDF:Keywords": keywords,
    }


def pdf_supported_tags(tags: dict[str, str]) -> dict[str, str]:
    """Keep the XMP/PDF mapping and omit JPEG-only legacy IPTC fields."""

    return {key: value for key, value in tags.items() if not key.startswith("IPTC:")}


def embed(path: Path, tags: dict[str, str], copy_from: Path | None = None) -> None:
    arguments = ["exiftool", "-overwrite_original", "-P"]
    if copy_from is not None:
        arguments.extend(["-TagsFromFile", str(copy_from)])
        arguments.extend(f"-{tag}" for tag in COPY_TO_DERIVATIVE)
    arguments.extend(f"-{tag}={value}" for tag, value in tags.items())
    if path.suffix.casefold() in {".jpg", ".jpeg"}:
        # Keep legacy IPTC and XMP synchronized when source JPEGs carry an
        # older Photoshop IPTC digest.
        arguments.append("-IPTCDigest=new")
    arguments.append(str(path))
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=600)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or "Warning:" in output:
        raise CorrectionError(f"ExifTool embedding failed for {path}: {output}")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def verify_tags(path: Path, tags: dict[str, str]) -> None:
    observed = exif_values(path, list(tags))
    for tag, expected in tags.items():
        actual = normalized(observed.get(tag))
        if actual != normalized(expected):
            raise CorrectionError(
                f"metadata readback differs for {path}: {tag} "
                f"expected={expected!r}, observed={actual!r}"
            )


def decoded_image(path: Path) -> tuple[str, int, int]:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        return hashlib.sha256(rgb.tobytes()).hexdigest(), rgb.width, rgb.height


def pdf_pages(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)], check=True, capture_output=True, text=True, timeout=60
    )
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise CorrectionError(f"cannot read PDF page count: {path}")
    return int(match.group(1))


def pdf_first_page_digest(path: Path) -> str:
    prefix = RECON / f".objektsammlung-pdf-render-{uuid.uuid4().hex}"
    rendered = prefix.with_suffix(".ppm")
    try:
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-r",
                "72",
                str(path),
                str(prefix),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return sha256(rendered)
    finally:
        rendered.unlink(missing_ok=True)


def payload_files() -> set[str]:
    return {
        str(path.relative_to(UNIT))
        for directory in ("masters", "export", "metadata", "documents")
        for path in (UNIT / directory).iterdir()
        if path.is_file()
    }


def snapshot_sources(rows: list[dict[str, str]]) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for row in rows:
        path = Path(row["source_path"])
        if not path.is_relative_to(SOURCE_ROOT):
            raise CorrectionError(f"drain row is outside protected Elements: {path}")
        stat = path.stat()
        if stat.st_size != int(row["size_bytes"]):
            raise CorrectionError(f"protected source size changed: {path}")
        snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def verify_source_snapshot(snapshot: dict[str, tuple[int, int]]) -> None:
    for source, before in snapshot.items():
        stat = Path(source).stat()
        if (stat.st_size, stat.st_mtime_ns) != before:
            raise CorrectionError(f"protected source changed during correction: {source}")


def current_preflight() -> dict[str, object]:
    mount = guarded_mount()
    current_dirs = {path.name for path in UNIT.iterdir() if path.is_dir()}
    if current_dirs != ALLOWED_DIRS:
        raise CorrectionError(f"unexpected unit directories: {sorted(current_dirs)}")
    if any((UNIT / "documents").iterdir()):
        raise CorrectionError("documents directory is not empty")

    _, catalogue = read_csv(UNIT / "CATALOG.csv")
    if len(catalogue) != 10:
        raise CorrectionError("catalogue is not exactly ten accepted objects")
    master_paths = {row["selected_master_path"] for row in catalogue}
    derivative_paths = {row["access_derivative_path"] for row in catalogue}
    if len(master_paths) != 10 or len(derivative_paths) != 10:
        raise CorrectionError("catalogue does not identify ten unique Master/derivative pairs")
    actual_masters = {
        str(path.relative_to(UNIT)) for path in (UNIT / "masters").iterdir() if path.is_file()
    }
    actual_exports = {
        str(path.relative_to(UNIT)) for path in (UNIT / "export").iterdir() if path.is_file()
    }
    if actual_masters != master_paths or len(actual_exports) != 52:
        raise CorrectionError("current Master/export census differs from reviewed state")
    if not derivative_paths.issubset(actual_exports):
        raise CorrectionError("a catalogue-listed derivative is missing")

    rep_fields, representations = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    roles = Counter(row["role"] for row in representations)
    if roles != {
        "selected_master": 10,
        "generated_access_derivative": 10,
        "related_museum_digital_projection_resource": 9,
        "retained_unique_local_related_image": 33,
    }:
        raise CorrectionError(f"unexpected current representation roles: {dict(roles)}")
    retained_representations = [
        row
        for row in representations
        if row["role"] in {"selected_master", "generated_access_derivative"}
    ]
    removed_representations = [row for row in representations if row not in retained_representations]
    if {row["relative_path"] for row in retained_representations} != master_paths | derivative_paths:
        raise CorrectionError("catalogue and retained representations disagree")
    extra_exports = actual_exports - derivative_paths
    if len(extra_exports) != 42 or extra_exports != {
        row["relative_path"] for row in removed_representations
    }:
        raise CorrectionError("the 42 export removals are not exactly represented in the manifest")

    payload_fields, payload = read_csv(UNIT / "00_MANIFESTS/payload-checksums.csv")
    if len(payload) != 65 or payload_files() != {row["relative_path"] for row in payload}:
        raise CorrectionError("current payload census differs from its checksum manifest")
    for row in payload:
        path = UNIT / row["relative_path"]
        if path.stat().st_size != int(row["size_bytes"]) or sha256(path) != row["sha256"]:
            raise CorrectionError(f"current payload checksum mismatch: {row['relative_path']}")

    drain_fields, drain = read_csv(UNIT / "00_MANIFESTS/drain-list.csv")
    disposition_fields, disposition = read_csv(UNIT / "00_MANIFESTS/source-disposition.csv")
    work_drain_fields, work_drain = read_csv(RECON / "drain-list.csv")
    if drain != disposition or drain != work_drain or len(drain) != 43:
        raise CorrectionError("published and work-root drain/source rows disagree")
    if any(row["deletion_authorized"] != "false" for row in drain):
        raise CorrectionError("a drain row unexpectedly authorizes deletion")
    retained_drain = [
        row for row in drain if row["relationship"] in RETAINED_DRAIN_RELATIONSHIPS
    ]
    removed_drain = [row for row in drain if row not in retained_drain]
    if len(retained_drain) != 2 or len(removed_drain) != 41:
        raise CorrectionError("drain cleanup is not exactly 2 retained and 41 removed rows")
    if any(row["final_archive_path"] not in master_paths for row in retained_drain):
        raise CorrectionError("a retained drain row is not tied to an accepted Master")
    source_snapshot = snapshot_sources(drain)

    counts, quick_check, foreign_keys = sqlite_state(UNIT / "db-restore/RESTORE.sqlite")
    if counts != {
        "objects": 10,
        "representations": 62,
        "documents": 0,
        "source_disposition": 43,
    } or quick_check != "ok" or foreign_keys:
        raise CorrectionError(
            f"current SQLite state is invalid: {counts}, {quick_check}, {foreign_keys}"
        )

    _, metadata = read_csv(UNIT / "metadata/museum-digital-objects.csv")
    metadata_by_id = {row["md_object_id"]: row for row in metadata}
    if len(metadata_by_id) != 10 or set(metadata_by_id) != {row["md_object_id"] for row in catalogue}:
        raise CorrectionError("museum-digital metadata does not cover the ten accepted objects")
    required = (
        "inventory",
        "title",
        "description",
        "object_type",
        "date",
        "object_url",
    )
    for row in metadata:
        if any(not row[field].strip() for field in required):
            raise CorrectionError(f"required museum-digital metadata is missing for {row['md_object_id']}")

    return {
        "mount": mount,
        "catalogue": catalogue,
        "metadata_by_id": metadata_by_id,
        "rep_fields": rep_fields,
        "retained_representations": retained_representations,
        "extra_exports": extra_exports,
        "payload_fields": payload_fields,
        "drain_fields": drain_fields,
        "disposition_fields": disposition_fields,
        "work_drain_fields": work_drain_fields,
        "retained_drain": retained_drain,
        "source_snapshot": source_snapshot,
    }


def stage_embedded(
    source: Path,
    tags: dict[str, str],
    copy_from: Path | None = None,
) -> Path:
    temporary = source.with_name(
        f".{source.stem}.{uuid.uuid4().hex}.metadata-tmp{source.suffix}"
    )
    try:
        shutil.copy2(source, temporary)
        if source.suffix.casefold() == ".pdf":
            pages_before = pdf_pages(source)
            render_before = pdf_first_page_digest(source)
            embed(temporary, tags, copy_from)
            if pdf_pages(temporary) != pages_before or pdf_first_page_digest(temporary) != render_before:
                raise CorrectionError(f"PDF page content changed during metadata embedding: {source}")
        else:
            decoded_before = decoded_image(source)
            embed(temporary, tags, copy_from)
            if decoded_image(temporary) != decoded_before:
                raise CorrectionError(f"decoded pixels changed during metadata embedding: {source}")
        verify_tags(temporary, tags)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def final_validation(expected_hashes: dict[str, str] | None = None) -> dict[str, object]:
    mount = guarded_mount()
    _, catalogue = read_csv(UNIT / "CATALOG.csv")
    masters = {row["selected_master_path"] for row in catalogue}
    derivatives = {row["access_derivative_path"] for row in catalogue}
    actual_masters = {
        str(path.relative_to(UNIT)) for path in (UNIT / "masters").iterdir() if path.is_file()
    }
    actual_exports = {
        str(path.relative_to(UNIT)) for path in (UNIT / "export").iterdir() if path.is_file()
    }
    if actual_masters != masters or actual_exports != derivatives:
        raise CorrectionError("final Master/export census disagrees with the catalogue")

    _, metadata = read_csv(UNIT / "metadata/museum-digital-objects.csv")
    metadata_by_id = {row["md_object_id"]: row for row in metadata}
    for row in catalogue:
        meta = metadata_by_id[row["md_object_id"]]
        master = UNIT / row["selected_master_path"]
        derivative = UNIT / row["access_derivative_path"]
        master_tags = metadata_tags(
            meta,
            "selected_master",
            merged_description(existing_description(master), meta["description"]),
            row["selected_master_path"],
        )
        if master.suffix.casefold() == ".pdf":
            master_tags = pdf_supported_tags(master_tags)
            master_tags.update(pdf_info_tags(meta))
        derivative_tags = metadata_tags(
            meta,
            "generated_access_derivative",
            meta["description"],
            row["selected_master_path"],
        )
        verify_tags(master, master_tags)
        verify_tags(derivative, derivative_tags)

    _, representations = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    roles = Counter(row["role"] for row in representations)
    if roles != {"selected_master": 10, "generated_access_derivative": 10}:
        raise CorrectionError(f"unexpected final representation roles: {dict(roles)}")
    if {row["relative_path"] for row in representations} != masters | derivatives:
        raise CorrectionError("final representation paths disagree with catalogue")

    _, payload = read_csv(UNIT / "00_MANIFESTS/payload-checksums.csv")
    if len(payload) != 23 or payload_files() != {row["relative_path"] for row in payload}:
        raise CorrectionError("final payload is not exactly 23 files")
    for row in payload:
        path = UNIT / row["relative_path"]
        if path.stat().st_size != int(row["size_bytes"]):
            raise CorrectionError(f"final payload size differs: {row['relative_path']}")
        if expected_hashes is not None and expected_hashes[row["relative_path"]] != row["sha256"]:
            raise CorrectionError(f"final payload hash projection differs: {row['relative_path']}")

    _, drain = read_csv(UNIT / "00_MANIFESTS/drain-list.csv")
    _, disposition = read_csv(UNIT / "00_MANIFESTS/source-disposition.csv")
    _, work_drain = read_csv(RECON / "drain-list.csv")
    if drain != disposition or drain != work_drain or len(drain) != 2:
        raise CorrectionError("final published/work drain rows disagree or are not exactly two")
    if {row["relationship"] for row in drain} != RETAINED_DRAIN_RELATIONSHIPS:
        raise CorrectionError("final drain rows are not the accepted Master-source relationships")
    if any(row["deletion_authorized"] != "false" for row in drain):
        raise CorrectionError("a final drain row unexpectedly authorizes deletion")

    counts, quick_check, foreign_keys = sqlite_state(UNIT / "db-restore/RESTORE.sqlite")
    if counts != {
        "objects": 10,
        "representations": 20,
        "documents": 0,
        "source_disposition": 2,
    } or quick_check != "ok" or foreign_keys:
        raise CorrectionError(
            f"final SQLite state is invalid: {counts}, {quick_check}, {foreign_keys}"
        )
    connection = sqlite3.connect(
        f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro&immutable=1", uri=True
    )
    try:
        database_representations = {
            row[0]: (row[1], int(row[2]))
            for row in connection.execute(
                "SELECT relative_path, sha256, size_bytes FROM representations"
            )
        }
    finally:
        connection.close()
    csv_representations = {
        row["relative_path"]: (row["sha256"], int(row["size_bytes"]))
        for row in representations
    }
    if database_representations != csv_representations:
        raise CorrectionError("SQLite and human-readable representation records disagree")

    completion = json.loads(
        (UNIT / "00_MANIFESTS/accession-completion.json").read_text(encoding="utf-8")
    )
    expected_completion = {
        "objects": 10,
        "selected_masters": 10,
        "generated_access_derivatives": 10,
        "related_museum_digital_resources": 0,
        "retained_unique_local_related_images": 0,
        "retained_unique_documents": 0,
        "source_disposition_rows": 2,
        "payload_files": 23,
        "metadata_embedded_files": 20,
        "unit_export_files_removed": 42,
        "drain_rows_removed": 41,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
        "result": "ok",
    }
    for key, value in expected_completion.items():
        if completion.get(key) != value:
            raise CorrectionError(
                f"completion receipt differs: {key}={completion.get(key)!r}, expected {value!r}"
            )
    return {
        "result": "ok",
        "objects": 10,
        "selected_masters": 10,
        "generated_access_derivatives": 10,
        "export_files": 10,
        "payload_files": 23,
        "drain_rows": 2,
        "sqlite_counts": counts,
        "sqlite_quick_check": quick_check,
        "sqlite_foreign_key_violations": len(foreign_keys),
        "metadata_profile": PROFILE,
        "source_mount": mount,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
    }


def apply_correction(state: dict[str, object]) -> dict[str, object]:
    corrected_at = now()
    catalogue = state["catalogue"]
    metadata_by_id = state["metadata_by_id"]
    staged: dict[str, Path] = {}
    try:
        for row in catalogue:
            meta = metadata_by_id[row["md_object_id"]]
            master_relative = row["selected_master_path"]
            master = UNIT / master_relative
            description = merged_description(existing_description(master), meta["description"])
            tags = metadata_tags(meta, "selected_master", description, master_relative)
            if master.suffix.casefold() == ".pdf":
                tags = pdf_supported_tags(tags)
                tags.update(pdf_info_tags(meta))
            staged[master_relative] = stage_embedded(master, tags)

        for row in catalogue:
            meta = metadata_by_id[row["md_object_id"]]
            master_relative = row["selected_master_path"]
            derivative_relative = row["access_derivative_path"]
            derivative = UNIT / derivative_relative
            tags = metadata_tags(
                meta,
                "generated_access_derivative",
                meta["description"],
                master_relative,
            )
            copy_from = (
                staged[master_relative]
                if Path(master_relative).suffix.casefold() != ".pdf"
                else None
            )
            staged[derivative_relative] = stage_embedded(
                derivative, tags, copy_from
            )

        for relative, temporary in staged.items():
            os.replace(temporary, UNIT / relative)
        staged.clear()

        for relative in sorted(state["extra_exports"]):
            target = UNIT / relative
            if target.parent != UNIT / "export" or not target.is_file():
                raise CorrectionError(f"unsafe or missing reviewed export target: {relative}")
            target.unlink()

        final_representations = [dict(row) for row in state["retained_representations"]]
        final_hashes: dict[str, str] = {}
        for row in final_representations:
            path = UNIT / row["relative_path"]
            digest = sha256(path)
            final_hashes[row["relative_path"]] = digest
            row["sha256"] = digest
            row["size_bytes"] = str(path.stat().st_size)

        final_drain = [dict(row) for row in state["retained_drain"]]
        for row in final_drain:
            row["covered_by_unit"] = "true"
            row["coverage_state"] = "selected_master_content_preserved_with_embedded_metadata"
            row["drain_state"] = "proposed_redundant_after_validated_accession"
            row["deletion_authorized"] = "false"
            row["note"] = (
                "Accepted Master content is preserved in the Objektsammlung unit with embedded "
                "museum-digital metadata; the USB source remains protected and deletion is not authorized."
            )

        payload: list[dict[str, object]] = []
        for directory in ("masters", "export", "metadata", "documents"):
            for path in sorted((UNIT / directory).iterdir(), key=lambda item: item.name.casefold()):
                if not path.is_file():
                    continue
                relative = str(path.relative_to(UNIT))
                digest = final_hashes.get(relative)
                if digest is None:
                    digest = sha256(path)
                    final_hashes[relative] = digest
                payload.append(
                    {
                        "relative_path": relative,
                        "sha256": digest,
                        "size_bytes": path.stat().st_size,
                    }
                )
        if len(payload) != 23:
            raise CorrectionError(f"corrected payload is not exactly 23 files: {len(payload)}")

        database = UNIT / "db-restore/RESTORE.sqlite"
        temporary_db = database.with_name(f".RESTORE.{uuid.uuid4().hex}.metadata-tmp.sqlite")
        shutil.copy2(database, temporary_db)
        try:
            connection = sqlite3.connect(temporary_db)
            connection.execute("PRAGMA foreign_keys=ON")
            retained_paths = {row["relative_path"] for row in final_representations}
            placeholders = ",".join("?" for _ in retained_paths)
            connection.execute(
                f"DELETE FROM representations WHERE relative_path NOT IN ({placeholders})",
                tuple(sorted(retained_paths)),
            )
            for row in final_representations:
                connection.execute(
                    "UPDATE representations SET sha256=?, size_bytes=? WHERE relative_path=?",
                    (row["sha256"], int(row["size_bytes"]), row["relative_path"]),
                )
            connection.execute("DELETE FROM source_disposition")
            connection.executemany(
                "INSERT INTO source_disposition VALUES (?,?,?,?,?,0)",
                [
                    (
                        row["source_path"],
                        row["md_object_id"],
                        row["relationship"],
                        row["final_archive_path"],
                        row["drain_state"],
                    )
                    for row in final_drain
                ],
            )
            connection.commit()
            counts = {
                table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in ("objects", "representations", "documents", "source_disposition")
            }
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            dump = "\n".join(connection.iterdump()) + "\n"
            connection.close()
            if counts != {
                "objects": 10,
                "representations": 20,
                "documents": 0,
                "source_disposition": 2,
            } or quick_check != "ok" or foreign_keys:
                raise CorrectionError(
                    f"corrected SQLite validation failed: {counts}, {quick_check}, {foreign_keys}"
                )
            os.replace(temporary_db, database)
        finally:
            temporary_db.unlink(missing_ok=True)

        readme = (
            "# Objektsammlung\n\n"
            "This unit is the human-readable archival authority for museum-digital collection 549. "
            "Museum-digital is retained as an external projection and provenance source.\n\n"
            "Each of the ten accepted objects has exactly one selected Master in `masters/`. "
            "The `export/` directory contains exactly one access derivative generated from each "
            "accepted Master. Museum-digital object metadata is embedded in both the Master and "
            "its generated derivative; the CSV files in `metadata/` remain supporting evidence. "
            "No related image resources or local documentation are accessioned in this unit. "
            "Essential decisions are in `CATALOG.csv` and `00_MANIFESTS/`; SQLite is rebuildable "
            "catalogue support only.\n\n"
            "Source paths remain read-only. Drain rows cover only accepted Master sources, are "
            "proposals, and carry `deletion_authorized=false`.\n"
        )

        old_completion = json.loads(
            (UNIT / "00_MANIFESTS/accession-completion.json").read_text(encoding="utf-8")
        )
        completion = dict(old_completion)
        completion.update(
            {
                "completed_at": corrected_at,
                "export_cleanup_applied_at": corrected_at,
                "metadata_embedding_applied_at": corrected_at,
                "correction_scope": (
                    "remove non-Objektsammlung documents; restrict export to one generated derivative "
                    "per accepted Master; embed museum-digital metadata; restrict drain rows to accepted "
                    "Master sources"
                ),
                "related_museum_digital_resources": 0,
                "retained_unique_local_related_images": 0,
                "retained_unique_documents": 0,
                "source_disposition_rows": len(final_drain),
                "payload_files": len(payload),
                "payload_bytes": sum(int(row["size_bytes"]) for row in payload),
                "metadata_profile": PROFILE,
                "metadata_embedded_files": 20,
                "unit_export_files_removed": 42,
                "drain_rows_removed": 41,
                "source_files_modified": 0,
                "source_files_deleted": 0,
                "deletion_authorized": False,
                "sqlite_quick_check": quick_check,
                "sqlite_foreign_key_violations": len(foreign_keys),
                "source_mount": guarded_mount(),
                "result": "ok",
            }
        )

        atomic_csv(
            UNIT / "00_MANIFESTS/representations.csv",
            state["rep_fields"],
            final_representations,
        )
        atomic_csv(UNIT / "00_MANIFESTS/drain-list.csv", state["drain_fields"], final_drain)
        atomic_csv(
            UNIT / "00_MANIFESTS/source-disposition.csv",
            state["disposition_fields"],
            final_drain,
        )
        atomic_csv(RECON / "drain-list.csv", state["work_drain_fields"], final_drain)
        atomic_csv(
            UNIT / "00_MANIFESTS/payload-checksums.csv",
            state["payload_fields"],
            payload,
        )
        atomic_text(UNIT / "db-restore/RESTORE.sql", dump)
        atomic_text(UNIT / "00_README.md", readme)
        atomic_text(
            UNIT / "00_MANIFESTS/accession-completion.json",
            json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

        verify_source_snapshot(state["source_snapshot"])
        if guarded_mount() != state["mount"]:
            raise CorrectionError("protected source mount changed during correction")
        result = final_validation(final_hashes)
        result.update(
            {
                "unit_export_files_removed": 42,
                "drain_rows_removed": 41,
                "metadata_embedded_files": 20,
                "payload_bytes": completion["payload_bytes"],
            }
        )
        return result
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--verify"]:
        print(json.dumps(final_validation(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if arguments not in ([], ["--check"]):
        raise CorrectionError(
            "usage: collection_549_finalize_md_unit.py [--check|--verify]"
        )
    state = current_preflight()
    if arguments == ["--check"]:
        verify_source_snapshot(state["source_snapshot"])
        if guarded_mount() != state["mount"]:
            raise CorrectionError("protected source mount changed during preflight")
        print(
            json.dumps(
                {
                    "result": "ok",
                    "mode": "check-only",
                    "accepted_objects": 10,
                    "masters_to_embed": 10,
                    "derivatives_to_embed": 10,
                    "unit_export_files_to_remove": len(state["extra_exports"]),
                    "drain_rows_to_remove": 41,
                    "drain_rows_to_retain": 2,
                    "final_export_files": 10,
                    "final_payload_files": 23,
                    "source_mount": state["mount"],
                    "source_files_modified": 0,
                    "source_files_deleted": 0,
                    "deletion_authorized": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps(apply_correction(state), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
