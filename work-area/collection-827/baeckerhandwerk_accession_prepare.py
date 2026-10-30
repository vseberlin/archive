#!/usr/bin/env python3
"""Prepare the bounded accession plan for museum-digital collection 827.

This tool does not copy, hash, modify, or delete source/archive payloads. It
captures the live museum-digital metadata, verifies the exact-name local TIFF
set, records the user's explicit Master confirmation, and writes preparation
manifests below the approved collection-827 work root.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

COLLECTION_ID = 827
EXPECTED_OBJECTS = 76
EXPECTED_LOCAL_FILES = 92
EXPECTED_SELECTED_TIFFS = 76
EXPECTED_OUT_OF_SCOPE = 16
SOURCE_MOUNT = Path("/media/vladimir/Elements")
SOURCE_DIR = SOURCE_MOUNT / "restore/archive/WF/Baecker/01_Glasplatten_Ersatzoriginale"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-827")
OUTPUT_DIR = WORK_ROOT / "work/accession"
TARGET_UNIT = Path("/mnt/volume/iss-archive/units/baeckerhandwerk")
MD_EXPORT = (
    "https://berlin.museum-digital.de/export/json/collection:827?limit=200&offset=0"
)
MD_COLLECTION_URL = "https://berlin.museum-digital.de/objects?s=collection:827"
HUMAN_CONFIRMATION = "user_confirmed_exact_inventory_mapping_2026-08-28"
MD_PRESENTATION_ROTATION_CW = {
    "EBK_03",
    "EBK_04_49",
    "EBK_05",
    "EBK_06_62",
    "EBK_07",
    "EBK_08",
    "EBK_11",
    "EBK_12",
    "EBK_15",
    "EBK_27",
    "EBK_54",
    "FVB_02",
    "FVB_03",
    "FVB_04_3",
    "FVB_07_1",
    "FVB_07_2",
}


class PreparationError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_MOUNT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise PreparationError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise PreparationError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def fetch_bytes(url: str, *, retries: int = 3, retry_delay: float = 2.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ISSArchiveAccession/1.0 (+local collection-827 preparation)",
            "Accept": "application/json",
        },
    )
    failure: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            failure = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failure = exc
            if attempt >= retries:
                break
        time.sleep(retry_delay * (attempt + 1))
    raise PreparationError(f"could not fetch {url}: {failure}")


def fetch_md_objects() -> list[dict[str, Any]]:
    payload = json.loads(fetch_bytes(MD_EXPORT))
    results = payload.get("results") or {}
    objects = results.get("objects") or {}
    if results.get("total") != EXPECTED_OBJECTS or len(objects) != EXPECTED_OBJECTS:
        raise PreparationError(f"expected {EXPECTED_OBJECTS} MD objects, found {len(objects)}")
    rows = list(objects.values())
    inventories = [str(row.get("object_inventory_number", "")) for row in rows]
    if len(set(inventories)) != EXPECTED_OBJECTS or any(not value for value in inventories):
        raise PreparationError("museum-digital inventories are missing or duplicated")
    if any(len(row.get("object_images") or []) != 1 for row in rows):
        raise PreparationError("every collection-827 object must have exactly one MD image")
    if any(row.get("object_type") != "SW-Foto" for row in rows):
        raise PreparationError("unexpected museum-digital object type")
    return sorted(rows, key=lambda row: str(row["object_inventory_number"]))


def technical_metadata(paths: list[Path]) -> dict[str, dict[str, Any]]:
    fields = [
        "FileName",
        "FileSize#",
        "ImageWidth",
        "ImageHeight",
        "BitsPerSample",
        "Compression",
        "PhotometricInterpretation",
        "Make",
        "Model",
        "Software",
        "CreateDate",
        "Orientation",
        "XResolution",
        "YResolution",
        "ResolutionUnit",
        "ProfileDescription",
    ]
    command = ["exiftool", "-fast2", "-json", *[f"-{field}" for field in fields], *map(str, paths)]
    result = subprocess.run(command, check=True, capture_output=True, timeout=120)
    rows = json.loads(result.stdout)
    if len(rows) != len(paths):
        raise PreparationError("technical metadata count does not match TIFF count")
    return {str(row["FileName"]): row for row in rows}


def md_preview_url(row: dict[str, Any]) -> str:
    image = row["object_images"][0]
    return f"https://assets.museum-digital.org/berlin/{image['folder']}/{image['preview']}"

def csv_payload(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def prepare() -> dict[str, Any]:
    mount = source_guard()
    if TARGET_UNIT.exists():
        raise PreparationError(f"target unit already exists: {TARGET_UNIT}")
    if not SOURCE_DIR.is_dir():
        raise PreparationError(f"source directory is absent: {SOURCE_DIR}")

    md_rows = fetch_md_objects()
    inventories = {str(row["object_inventory_number"]) for row in md_rows}
    local_files = sorted(path for path in SOURCE_DIR.iterdir() if path.is_file())
    if len(local_files) != EXPECTED_LOCAL_FILES:
        raise PreparationError(f"expected {EXPECTED_LOCAL_FILES} files in bounded source, found {len(local_files)}")
    selected = [SOURCE_DIR / f"{inventory}.tif" for inventory in sorted(inventories)]
    if len(selected) != EXPECTED_SELECTED_TIFFS or any(not path.is_file() for path in selected):
        raise PreparationError("exact-name TIFF coverage is incomplete")
    extra = [path for path in local_files if path not in set(selected)]
    if len(extra) != EXPECTED_OUT_OF_SCOPE:
        raise PreparationError("unexpected out-of-scope source-file count")

    tech = technical_metadata(selected)
    required_technical = {
        "Make": "Phase One",
        "Model": "IQ3 100MP",
        "Software": "Capture One Pro 11.1.1 Windows",
        "BitsPerSample": "16 16 16",
        "Compression": "Uncompressed",
        "PhotometricInterpretation": "RGB",
        "ProfileDescription": "Adobe RGB (1998)",
    }
    for filename, metadata in tech.items():
        for field, expected in required_technical.items():
            if str(metadata.get(field)) != expected:
                raise PreparationError(f"unexpected {field} for {filename}: {metadata.get(field)!r}")

    decision_rows: list[dict[str, Any]] = []
    md_csv_rows: list[dict[str, Any]] = []
    for row in md_rows:
        object_id = str(row["object_id"])
        inventory = str(row["object_inventory_number"])
        source = SOURCE_DIR / f"{inventory}.tif"
        image = row["object_images"][0]
        technical = tech[source.name]
        tags = "; ".join(str(tag.get("tag_name", "")) for tag in row.get("object_tags") or [])
        events = json.dumps(row.get("object_events") or [], ensure_ascii=False, sort_keys=True)
        collections = json.dumps(row.get("object_collection") or [], ensure_ascii=False, sort_keys=True)
        common = {
            "object_key": f"md:{object_id}",
            "md_object_id": object_id,
            "inventory": inventory,
            "title": row.get("object_name", ""),
            "description": row.get("object_description", ""),
            "object_type": row.get("object_type", ""),
            "material_technique": row.get("object_material_technique", ""),
            "dimensions": row.get("object_dimensions", ""),
            "tags": tags,
            "events_json": events,
            "collections_json": collections,
            "md_last_updated": row.get("object_last_updated", ""),
            "md_object_url": f"https://berlin.museum-digital.de/object/{object_id}",
            "md_image_resource_id": image.get("quell_id", ""),
            "md_image_filename": image.get("filename_loc", ""),
            "md_image_rights": image.get("rights", ""),
            "md_image_owner": image.get("owner", ""),
            "md_preview_url": md_preview_url(row),
        }
        md_csv_rows.append(common)
        decision_rows.append(
            {
                **common,
                "outcome": "selected_master",
                "selected_master_source_path": str(source),
                "selected_master_format": "tiff",
                "selected_master_size_bytes": technical.get("FileSize", source.stat().st_size),
                "selected_master_width": technical.get("ImageWidth", ""),
                "selected_master_height": technical.get("ImageHeight", ""),
                "selected_master_bits_per_sample": technical.get("BitsPerSample", ""),
                "selected_master_orientation": technical.get("Orientation", ""),
                "master_final_relative_path": f"masters/{inventory}.tif",
                "access_final_relative_path": f"export/access/{inventory}.jpg",
                "presentation_rotation_cw": 90 if inventory in MD_PRESENTATION_ROTATION_CW else 0,
                "orientation_basis": "bounded_md_preview_comparison_2026-08-28",
                "metadata_authority": "live_museum_digital_capture",
                "selection_basis": "exact_inventory_tiff_and_visual_identity",
                "human_confirmation": HUMAN_CONFIRMATION,
                "source_hash_state": "deferred_until_accession_copy",
                "final_hash_state": "deferred_until_finalized_accession_output",
                "accession_state": "prepared_not_accessioned",
                "deletion_authorized": "false",
            }
        )

    disposition_rows: list[dict[str, Any]] = []
    selected_set = set(selected)
    for path in local_files:
        if path in selected_set:
            disposition = "selected_master"
            basis = "collection_827_exact_inventory_tiff_user_confirmed"
            object_key = f"md:{next(row['object_id'] for row in md_rows if row['object_inventory_number'] == path.stem)}"
        else:
            disposition = "out_of_scope_collection_827"
            basis = "filename_inventory_absent_from_live_collection_827"
            object_key = ""
        disposition_rows.append(
            {
                "source_path": str(path),
                "filename": path.name,
                "object_key": object_key,
                "disposition": disposition,
                "basis": basis,
                "deletion_authorized": "false",
            }
        )

    decision_fields = list(decision_rows[0])
    md_fields = list(md_csv_rows[0])
    disposition_fields = list(disposition_rows[0])
    started = now()
    preparation = {
        "format": "iss-baeckerhandwerk-accession-preparation-v1",
        "prepared_utc": started,
        "collection_id": COLLECTION_ID,
        "collection_url": MD_COLLECTION_URL,
        "metadata_authority": "live_museum_digital_capture",
        "human_confirmation": HUMAN_CONFIRMATION,
        "objects": len(decision_rows),
        "selected_masters": Counter(row["outcome"] for row in decision_rows)["selected_master"],
        "master_formats": dict(Counter(row["selected_master_format"] for row in decision_rows)),
        "presentation_rotation_cw": dict(Counter(str(row["presentation_rotation_cw"]) for row in decision_rows)),
        "required_source_bytes": sum(path.stat().st_size for path in selected),
        "bounded_source_files": len(local_files),
        "out_of_scope_source_files": len(extra),
        "source_hash_state": "deferred_until_accession_copy",
        "final_hash_state": "deferred_until_finalized_accession_output",
        "source_mount": mount,
        "source_directory": str(SOURCE_DIR),
        "target_unit": str(TARGET_UNIT),
        "target_unit_exists": False,
        "accession_ready": True,
        "accession_executed": False,
        "deletion_authorized": False,
    }

    readme = f"""# Bäckerhandwerk collection 827 accession preparation

This preparation records the user's explicit confirmation of one exact-name TIFF
Master for each of the {EXPECTED_OBJECTS} museum-digital objects in collection 827.
No separate reconciliation workbench is required because the inventory mapping is
complete and the local TIFFs visually correspond to all museum-digital previews.

- Selected TIFF Masters: {len(selected)}
- Proposed generated JPEG access derivatives: {len(selected)}
- Presentation rotation 90 degrees clockwise: {sum(row['presentation_rotation_cw'] == 90 for row in decision_rows)}
- Source files outside collection 827: {len(extra)}
- Source mount: `/dev/sdf1` as `ntfs3 ro`
- Deletion authorized: false
- Accession executed: no

The 16 non-collection files remain out of scope, are not redundant decisions, and
remain unchanged. Source TIFF bytes will be copied unchanged during accession.
Hashes are intentionally deferred until the accession output is finalized.
"""

    atomic_bytes(OUTPUT_DIR / "accession-decisions.csv", csv_payload(decision_fields, decision_rows))
    atomic_bytes(OUTPUT_DIR / "museum-digital-objects.csv", csv_payload(md_fields, md_csv_rows))
    atomic_json(OUTPUT_DIR / "museum-digital-objects.json", md_rows)
    atomic_bytes(OUTPUT_DIR / "source-disposition.csv", csv_payload(disposition_fields, disposition_rows))
    atomic_json(OUTPUT_DIR / "accession-preparation.json", preparation)
    atomic_bytes(OUTPUT_DIR / "README.md", readme.encode())
    return preparation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true", help="write bounded preparation manifests")
    args = parser.parse_args()
    if not args.prepare:
        parser.error("use --prepare")
    try:
        result = prepare()
    except (PreparationError, subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
