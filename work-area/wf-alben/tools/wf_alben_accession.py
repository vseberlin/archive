#!/usr/bin/env python3
"""Accession the fully reconciled WF-Alben unit."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


WORK = Path("/mnt/volume/iss-archive-work/wf-alben")
RECON = WORK / "work/reconciliation"
UNIT = Path("/mnt/volume/iss-archive/units/wf-alben")
SOURCE_ROOT = Path("/media/vladimir/Elements")
DB = RECON / "workbench.sqlite"
RAW = Path(
    "/mnt/volume/iss-archive-work/collection-607/work/reconciliation/"
    "museum-digital/runs/20260822T111320Z-collection-607/raw"
)
DECISIONS = RECON / "workbench-decisions.csv"
DISPOSITIONS = RECON / "workbench-candidate-dispositions.csv"
METADATA_DECISIONS = RECON / "metadata-editorial-decisions.csv"
LINKED_RELATIONSHIPS = RECON / "linked-object-relationships.csv"
DERIVED_RELATIONSHIPS = RECON / "derived-image-relationships.csv"
DOCUMENT_PROVENANCE = UNIT / "00_MANIFESTS/document-provenance.csv"
DOCUMENT_CHECKSUMS = UNIT / "00_MANIFESTS/payload-checksums.csv"
PREPARATION = UNIT / "00_MANIFESTS/accession-preparation.json"
PROFILE = "iss-wf-alben-metadata-v1"
DERIVATIVE_PROFILE = "libvips-full-resolution-jpeg-q95-v1"
SEP = "\x1f"
EXPECTED_RECORDS = 182
EXPECTED_SELECTED = 143
EXPECTED_REDUNDANT_DERIVATIVES = 39
EXPECTED_CANDIDATES = 1070
EXPECTED_REDUNDANT_CANDIDATES = 927
EXPECTED_METADATA_DECISIONS = 66
SURROGATES = {
    (RECON / "catalogue-surrogate-md-60243-image-43750.jpg").resolve(),
    (RECON / "catalogue-surrogate-md-60256-image-43824.jpg").resolve(),
}
TECHNICAL_TAGS = (
    "File:FileType", "File:MIMEType", "IFD0:ImageWidth", "IFD0:ImageHeight",
    "IFD0:BitsPerSample", "IFD0:Compression", "IFD0:PhotometricInterpretation",
    "IFD0:SamplesPerPixel", "IFD0:PlanarConfiguration", "IFD0:XResolution",
    "IFD0:YResolution", "IFD0:ResolutionUnit", "IFD0:Orientation",
    "EXIF:ExifImageWidth", "EXIF:ExifImageHeight", "Photoshop:LayerCount",
    "Photoshop:LayerNames", "Photoshop:HasRealMergedData",
)
CATALOG_FIELDS = (
    "record_key", "archival_object_identifier", "md_object_id", "md_image_id",
    "parent_md_object_id", "inventory", "title", "image_name", "component_label",
    "object_type", "date_text", "description", "image_description",
    "material_technique", "dimensions", "creator", "place", "subjects",
    "related_people", "collection", "image_rights", "image_rights_holder",
    "metadata_rights", "metadata_rights_holder", "md_last_updated", "md_url",
    "linked_record_key", "link_relation", "selected_candidate_id",
    "selected_master_source_path", "source_sha256", "source_size_bytes",
    "master_relative_path", "master_sha256", "master_size_bytes",
    "access_relative_path", "access_sha256", "access_size_bytes",
    "metadata_authority", "metadata_profile", "derivative_profile",
    "accession_result", "deletion_authorized",
)
REPRESENTATION_FIELDS = (
    "record_key", "inventory", "role", "source_path", "source_sha256",
    "final_relative_path", "final_sha256", "size_bytes", "copy_policy",
    "basis", "deletion_authorized",
)
CHECKSUM_FIELDS = ("path", "sha256", "size_bytes", "content_role")
SOURCE_DISPOSITION_FIELDS = (
    "record_key", "md_object_id", "inventory", "candidate_id", "source_path",
    "disposition", "decision_basis", "linked_record_key", "linked_master_path",
    "source_sha256", "final_master_path", "final_master_sha256",
    "preservation_state", "deletion_authorized",
)


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise AccessionError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise AccessionError(f"Elements read-only guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def allowed_source(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(SOURCE_ROOT.resolve(strict=True))
        return resolved
    except ValueError:
        if resolved in SURROGATES:
            return resolved
    raise AccessionError(f"selected master is outside the approved sources: {resolved}")


def run_tool(arguments: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments, check=True, capture_output=True, text=True, env=env, timeout=3600
        )
    except FileNotFoundError as error:
        raise AccessionError(f"required tool unavailable: {arguments[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise AccessionError(f"tool exceeded one hour: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise AccessionError(f"{arguments[0]} failed: {detail or error.returncode}") from error


def exif_json(path: Path, tags: Iterable[str], *, numeric: bool = False) -> dict[str, Any]:
    arguments = ["exiftool", "-j", "-G1", "-s", "-sep", SEP]
    if numeric:
        arguments.append("-n")
    arguments.extend(f"-{tag}" for tag in tags)
    arguments.append(str(path))
    payload = json.loads(run_tool(arguments).stdout)
    if len(payload) != 1:
        raise AccessionError(f"ExifTool returned no single record for {path}")
    payload[0].pop("SourceFile", None)
    return payload[0]


def normalize(value: Any) -> str:
    if isinstance(value, list):
        return SEP.join(str(item) for item in value)
    return "" if value is None else str(value)


def load_raw_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(RAW.glob("page-*.json")):
        objects = json.loads(path.read_text(encoding="utf-8")).get("results", {}).get("objects", {})
        if not isinstance(objects, dict):
            raise AccessionError(f"invalid museum-digital capture page: {path}")
        for object_id, record in objects.items():
            if object_id in records or not isinstance(record, dict):
                raise AccessionError(f"duplicate or invalid museum-digital record: {object_id}")
            records[str(object_id)] = record
    if len(records) != 178:
        raise AccessionError(f"expected 178 captured museum-digital records, found {len(records)}")
    _, overlays = read_csv(METADATA_DECISIONS)
    if len(overlays) != EXPECTED_METADATA_DECISIONS or any(row["decision_state"] == "unresolved" for row in overlays):
        raise AccessionError("metadata editorial overlay is incomplete")
    for row in overlays:
        record = records[row["md_object_id"]]
        field = row["field_name"]
        if field in {"object_name", "object_description"}:
            record[field] = row["resolved_value"]
            continue
        match = re.fullmatch(r"image:(\d+):(rights|name)", field)
        if not match:
            raise AccessionError(f"unsupported metadata overlay field: {field}")
        images = [image for image in record.get("object_images") or [] if str(image.get("quell_id")) == match.group(1)]
        if len(images) != 1:
            raise AccessionError(f"metadata overlay image did not resolve once: {field}")
        images[0][match.group(2)] = row["resolved_value"]
    return records


def scalar(value: Any) -> str:
    return "" if value in (None, [], {}) else str(value)


def metadata_for_object(row: dict[str, Any], record: dict[str, Any]) -> dict[str, str]:
    images = record.get("object_images") or []
    if row["md_image_id"]:
        images = [image for image in images if str(image.get("quell_id")) == row["md_image_id"]]
    if len(images) != 1:
        raise AccessionError(f"selected object does not resolve to one museum-digital image: {row['record_key']}")
    image = images[0]
    events = record.get("object_events") or []
    creators = []
    places = []
    dates = []
    for event in events:
        people = event.get("people") if isinstance(event.get("people"), dict) else {}
        place = event.get("place") if isinstance(event.get("place"), dict) else {}
        event_time = event.get("time") if isinstance(event.get("time"), dict) else {}
        if people.get("displayname"):
            creators.append(str(people["displayname"]))
        if place.get("place_name"):
            places.append(str(place["place_name"]))
        if event_time.get("time_name"):
            dates.append(str(event_time["time_name"]))
    related = []
    for item in record.get("object_relation_people") or []:
        people = item.get("people") if isinstance(item.get("people"), dict) else {}
        if people.get("displayname"):
            related.append(str(people["displayname"]))
    licence = record.get("licence") if isinstance(record.get("licence"), dict) else {}
    collections = [
        str(item.get("collection_name")) for item in record.get("object_collection") or []
        if item.get("collection_name")
    ]
    subjects = [
        str(item.get("tag_name")) for item in record.get("object_tags") or []
        if item.get("tag_name")
    ]
    return {
        "title": scalar(record.get("object_name")),
        "image_name": scalar(image.get("name")),
        "description": scalar(record.get("object_description")),
        "image_description": scalar(image.get("beschreibung")),
        "object_type": scalar(record.get("object_type")),
        "date_text": "; ".join(dict.fromkeys(dates)),
        "material_technique": scalar(record.get("object_material_technique")),
        "dimensions": scalar(record.get("object_dimensions")),
        "creator": "; ".join(dict.fromkeys(creators)),
        "place": "; ".join(dict.fromkeys(places)),
        "subjects": "; ".join(dict.fromkeys(subjects)),
        "related_people": "; ".join(dict.fromkeys(related)),
        "collection": "; ".join(dict.fromkeys(collections)),
        "image_rights": scalar(image.get("rights")),
        "image_rights_holder": scalar(image.get("owner")),
        "metadata_rights": scalar(licence.get("metadata_rights_status")),
        "metadata_rights_holder": scalar(licence.get("metadata_rights_holder")),
        "md_last_updated": scalar(record.get("object_last_updated")),
    }


def load_scope() -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    mount = source_mount()
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccessionError("workbench SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AccessionError("workbench SQLite foreign-key check failed")
        state_counts = dict(connection.execute("SELECT decision_state,count(*) FROM decisions GROUP BY decision_state"))
        disposition_counts = dict(connection.execute("SELECT disposition,count(*) FROM candidate_dispositions GROUP BY disposition"))
        if state_counts != {"redundant_derivative": EXPECTED_REDUNDANT_DERIVATIVES, "selected_master": EXPECTED_SELECTED}:
            raise AccessionError(f"decision scope drifted: {state_counts}")
        if disposition_counts != {"redundant": EXPECTED_REDUNDANT_CANDIDATES, "selected_master": EXPECTED_SELECTED}:
            raise AccessionError(f"candidate disposition scope drifted: {disposition_counts}")
        if connection.execute("SELECT coalesce(sum(deletion_authorized),0) FROM decisions").fetchone()[0]:
            raise AccessionError("decision unexpectedly authorizes deletion")
        if connection.execute("SELECT coalesce(sum(deletion_authorized),0) FROM candidate_dispositions").fetchone()[0]:
            raise AccessionError("candidate unexpectedly authorizes deletion")
        selected = [dict(row) for row in connection.execute(
            """SELECT o.*, d.selected_candidate_id, d.note AS decision_note,
                      d.decided_by, d.decided_at, c.source_path, c.filename,
                      c.extension, c.size_bytes, c.mtime_ns, c.width_px, c.height_px
                 FROM objects o JOIN decisions d USING(record_key)
                 JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                WHERE d.decision_state='selected_master' ORDER BY o.record_key"""
        )]
    finally:
        connection.close()
    if len(selected) != EXPECTED_SELECTED or len({row["record_key"] for row in selected}) != EXPECTED_SELECTED:
        raise AccessionError("selected-master scope is not one row per archival object")
    if len({row["selected_candidate_id"] for row in selected}) != EXPECTED_SELECTED:
        raise AccessionError("a selected candidate is reused across archival objects")
    if len({row["source_path"] for row in selected}) != EXPECTED_SELECTED:
        raise AccessionError("a selected source path is reused across archival objects")
    records = load_raw_records()
    total = 0
    format_counts: Counter[str] = Counter()
    for row in selected:
        source = allowed_source(Path(row["source_path"]))
        stat = source.stat()
        if not source.is_file() or stat.st_size != int(row["size_bytes"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
            raise AccessionError(f"selected source binding changed: {source}")
        suffix = source.suffix.casefold()
        if suffix not in {".tif", ".tiff", ".jpg", ".jpeg"}:
            raise AccessionError(f"unsupported selected master format: {source}")
        metadata = metadata_for_object(row, records[row["md_object_id"]])
        row.update(metadata)
        total += stat.st_size
        format_counts["jpeg" if suffix in {".jpg", ".jpeg"} else "tiff"] += 1
    _, dispositions = read_csv(DISPOSITIONS)
    if len(dispositions) != EXPECTED_CANDIDATES or any(row["deletion_authorized"] != "false" for row in dispositions):
        raise AccessionError("candidate disposition export drifted")
    if not PREPARATION.is_file() or json.loads(PREPARATION.read_text(encoding="utf-8")).get("state") != "prepared_not_accessioned":
        raise AccessionError("documentation preparation receipt is missing or invalid")
    _, document_checksums = read_csv(DOCUMENT_CHECKSUMS)
    for row in document_checksums:
        target = UNIT / row["relative_path"]
        if not target.is_file() or target.stat().st_size != int(row["size_bytes"]):
            raise AccessionError(f"prepared documentation payload drifted: {target}")
    free = shutil.disk_usage("/mnt/volume").free
    if free < total * 3 + 50 * 1024**3:
        raise AccessionError("insufficient free space for accession")
    summary = {
        "checked_at": now(), "source_mount": mount, "review_records": EXPECTED_RECORDS,
        "selected_masters": len(selected), "redundant_derivative_records": EXPECTED_REDUNDANT_DERIVATIVES,
        "candidate_dispositions": len(dispositions), "source_bytes": total,
        "source_gib": round(total / 1024**3, 3), "formats": dict(format_counts),
        "free_bytes": free, "metadata_decisions": EXPECTED_METADATA_DECISIONS,
        "unresolved": 0, "deletion_authorized": False,
    }
    return selected, dispositions, summary


def safe_stem(record_key: str) -> str:
    stem = record_key.replace(":", "-")
    if not re.fullmatch(r"[a-z0-9-]+", stem):
        raise AccessionError(f"unsafe record key for output filename: {record_key}")
    return stem


def master_suffix(source: Path) -> str:
    return ".tif" if source.suffix.casefold() in {".tif", ".tiff"} else ".jpg"


def metadata_plan(row: dict[str, Any], source_hash: str, master_path: str = "", master_hash: str = "") -> dict[str, str | None]:
    description = row["description"]
    if row["image_description"] and row["image_description"] not in description:
        description = f"{row['image_description']}\n\n{description}" if description else row["image_description"]
    rights = "; ".join(part for part in (row["image_rights"], row["image_rights_holder"]) if part)
    relations = [
        f"museum-digital:object:{row['md_object_id']}", row["md_url"],
        f"iss:metadata-profile:{PROFILE}", source_hash,
    ]
    if row["md_image_id"]:
        relations.append(f"museum-digital:image:{row['md_image_id']}")
    if row["linked_record_key"]:
        relations.append(f"{row['link_relation']}:{row['linked_record_key']}")
    if master_path and master_hash:
        relations.extend((f"master:{master_path}", master_hash))
    physical = "; ".join(part for part in (
        f"Material/Technik: {row['material_technique']}" if row["material_technique"] else "",
        f"Maße: {row['dimensions']}" if row["dimensions"] else "",
    ) if part)
    title = row["image_name"] or row["title"]
    plan: dict[str, str | None] = {
        "XMP-dc:Identifier": row["record_key"],
        "XMP-iptcExt:ArtworkSourceInventoryNo": row["inventory"],
        "XMP-dc:Title": title or None,
        "XMP-iptcExt:ArtworkTitle": title or None,
        "XMP-dc:Description": description or None,
        "XMP-iptcExt:ArtworkContentDescription": description or None,
        "IFD0:ImageDescription": description or None,
        "XMP-iptcExt:ArtworkCircaDateCreated": row["date_text"] or None,
        "XMP-dc:Type": row["object_type"] or None,
        "XMP-iptcExt:ArtworkPhysicalDescription": physical or None,
        "XMP-dc:Coverage": row["place"] or None,
        "XMP-dc:Subject": row["subjects"].replace("; ", SEP) or None,
        "XMP-dc:Creator": row["creator"].replace("; ", SEP) or None,
        "XMP-dc:Source": row["source_path"],
        "XMP-dc:Relation": SEP.join(relations),
        "XMP-dc:Rights": rights or None,
        "XMP-xmpRights:UsageTerms": row["image_rights"] or None,
        "IPTC:CopyrightNotice": rights if rights and len(rights.encode()) <= 128 else None,
        "IPTC:ObjectName": title if title and len(title.encode()) <= 64 else None,
        "IPTC:Caption-Abstract": description if description and len(description.encode()) <= 2000 else None,
        "IPTC:Keywords": row["subjects"].replace("; ", SEP) or None,
        "IPTC:By-line": row["creator"] if row["creator"] and len(row["creator"].encode()) <= 32 else None,
    }
    plan["IPTC:CodedCharacterSet"] = "UTF8" if any(plan.get(tag) for tag in ("IPTC:ObjectName", "IPTC:Caption-Abstract", "IPTC:Keywords", "IPTC:By-line")) else None
    return plan


def copy_with_metadata(source: Path, destination: Path, plan: dict[str, str | None]) -> None:
    arguments = ["exiftool", "-charset", "IPTC=UTF8", "-sep", SEP]
    arguments.extend(f"-{tag}={value or ''}" for tag, value in plan.items())
    arguments.extend(("-o", str(destination), str(source)))
    result = run_tool(arguments)
    serious = [line for line in result.stderr.splitlines() if "Warning:" in line and "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise AccessionError(f"ExifTool warning for {source.name}: {'; '.join(serious)}")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise AccessionError(f"ExifTool produced no master for {source}")


def write_metadata(path: Path, plan: dict[str, str | None]) -> None:
    arguments = ["exiftool", "-overwrite_original", "-charset", "IPTC=UTF8", "-sep", SEP]
    arguments.extend(f"-{tag}={value or ''}" for tag, value in plan.items())
    arguments.append(str(path))
    result = run_tool(arguments)
    serious = [line for line in result.stderr.splitlines() if "Warning:" in line and "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise AccessionError(f"ExifTool derivative warning: {'; '.join(serious)}")


def verify_metadata(path: Path, plan: dict[str, str | None]) -> None:
    observed = exif_json(path, plan.keys())
    for tag, expected in plan.items():
        actual = normalize(observed.get(tag))
        if (expected is None and actual) or (expected is not None and actual != expected):
            raise AccessionError(f"metadata readback mismatch {tag} in {path.name}")


def binary_tag_hash(path: Path, tag: str) -> str:
    process = subprocess.Popen(["exiftool", "-b", f"-{tag}", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256(); size = 0
    for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
        size += len(block); digest.update(block)
    detail = process.stderr.read().decode(errors="replace").strip()
    if process.wait():
        raise AccessionError(f"cannot read {tag} from {path.name}: {detail}")
    return f"{size}:sha256:{digest.hexdigest()}"


def scalar_depth(technical: dict[str, Any]) -> int:
    value = technical.get("IFD0:BitsPerSample", 8)
    values = value if isinstance(value, list) else str(value).replace(",", " ").split()
    parts = [int(part) for part in values]
    if not parts or len(set(parts)) != 1 or parts[0] not in {1, 2, 4, 8, 16}:
        raise AccessionError(f"unsupported BitsPerSample: {value!r}")
    return parts[0]


def pixel_digest(path: Path, depth: int) -> str:
    process = subprocess.Popen(["magick", str(path), "-depth", str(depth), "RGBA:-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
        digest.update(block)
    detail = process.stderr.read().decode(errors="replace").strip()
    if process.wait():
        raise AccessionError(f"image decode failed for {path.name}: {detail}")
    return "sha256:" + digest.hexdigest()


def verify_master(source: Path, master: Path, *, verify_pixels: bool = True) -> None:
    source_technical = exif_json(source, TECHNICAL_TAGS, numeric=True)
    master_technical = exif_json(master, TECHNICAL_TAGS, numeric=True)
    if source.suffix.casefold() in {".jpg", ".jpeg"}:
        # A JPEG with no EXIF IFD gains only the source resolution fields when
        # descriptive metadata is added.  Those fields describe the existing
        # image and do not alter its encoding or pixels.
        for tag in ("File:FileType", "File:MIMEType"):
            if source_technical.get(tag) != master_technical.get(tag):
                raise AccessionError(f"technical JPEG type changed for {source.name}")
        if dimensions(source) != dimensions(master):
            raise AccessionError(f"technical JPEG dimensions changed for {source.name}")
    elif source_technical != master_technical:
        raise AccessionError(f"technical image structure changed for {source.name}")
    if binary_tag_hash(source, "ICC_Profile") != binary_tag_hash(master, "ICC_Profile"):
        raise AccessionError(f"ICC profile changed for {source.name}")
    if any(key.endswith(":LayerCount") for key in source_technical):
        if binary_tag_hash(source, "PhotoshopSettings") != binary_tag_hash(master, "PhotoshopSettings"):
            raise AccessionError(f"Photoshop layer data changed for {source.name}")
    if verify_pixels:
        depth = scalar_depth(source_technical)
        if pixel_digest(source, depth) != pixel_digest(master, depth):
            raise AccessionError(f"decoded pixels changed for {source.name}")


def generate_access(master: Path, destination: Path) -> None:
    environment = dict(os.environ)
    environment.update({"VIPS_CONCURRENCY": "4", "TMPDIR": str(WORK)})
    run_tool(["vips", "autorot", str(master), f"{destination}[Q=95,optimize-coding=true,keep=icc]"], env=environment)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise AccessionError(f"libvips produced no access JPEG for {master}")


def dimensions(path: Path) -> tuple[int, int, int]:
    data = exif_json(path, ("ImageWidth", "ImageHeight", "Orientation"), numeric=True)
    width = [int(value) for key, value in data.items() if key.endswith(":ImageWidth")]
    height = [int(value) for key, value in data.items() if key.endswith(":ImageHeight")]
    orientation = [int(value) for key, value in data.items() if key.endswith(":Orientation")]
    if not width or not height:
        raise AccessionError(f"cannot determine dimensions for {path}")
    return width[0], height[0], orientation[0] if orientation else 1


def process(row: dict[str, Any], master_dir: Path, export_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    source_mount()
    source = allowed_source(Path(row["source_path"]))
    before = source.stat()
    binding = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    source_hash = sha256(source)
    stem = safe_stem(row["record_key"])
    master_relative = f"masters/{stem}{master_suffix(source)}"
    access_relative = f"export/{stem}.jpg"
    master = UNIT / master_relative
    access = UNIT / access_relative
    if master.exists() or access.exists():
        raise AccessionError(f"accession output already exists: {row['record_key']}")
    master_tmp = master_dir / f".{stem}.{os.getpid()}.tmp{master_suffix(source)}"
    access_tmp = export_dir / f".{stem}.{os.getpid()}.tmp.jpg"
    try:
        master_plan = metadata_plan(row, source_hash)
        copy_with_metadata(source, master_tmp, master_plan)
        verify_metadata(master_tmp, master_plan)
        verify_master(source, master_tmp)
        if binding != (source.stat().st_dev, source.stat().st_ino, source.stat().st_size, source.stat().st_mtime_ns):
            raise AccessionError(f"source changed during accession: {source}")
        master_hash = sha256(master_tmp)
        access_plan = metadata_plan(row, source_hash, master_relative, master_hash)
        generate_access(master_tmp, access_tmp)
        write_metadata(access_tmp, access_plan)
        verify_metadata(access_tmp, access_plan)
        width, height, orientation = dimensions(master_tmp)
        access_width, access_height, _ = dimensions(access_tmp)
        expected = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
        if (access_width, access_height) != expected:
            raise AccessionError(f"access dimensions changed for {row['record_key']}")
        if exif_json(access_tmp, ("File:FileType",)).get("File:FileType") != "JPEG":
            raise AccessionError(f"access derivative is not JPEG: {row['record_key']}")
        access_hash = sha256(access_tmp)
        os.replace(master_tmp, master); os.replace(access_tmp, access)
        os.chmod(master, 0o444); os.chmod(access, 0o444)
        return output_records(
            row, source, source_hash, before.st_size, master_relative, master_hash,
            master.stat().st_size, access_relative, access_hash, access.stat().st_size,
        )
    finally:
        master_tmp.unlink(missing_ok=True)
        access_tmp.unlink(missing_ok=True)


def output_records(
    row: dict[str, Any], source: Path, source_hash: str, source_size: int,
    master_relative: str, master_hash: str, master_size: int,
    access_relative: str, access_hash: str, access_size: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    catalog = {field: "" for field in CATALOG_FIELDS}
    catalog.update({field: row.get(field, "") for field in CATALOG_FIELDS})
    catalog.update({
        "archival_object_identifier": row["record_key"],
        "selected_master_source_path": str(source), "source_sha256": source_hash,
        "source_size_bytes": source_size, "master_relative_path": master_relative,
        "master_sha256": master_hash, "master_size_bytes": master_size,
        "access_relative_path": access_relative, "access_sha256": access_hash,
        "access_size_bytes": access_size,
        "metadata_authority": "captured museum-digital collection 607 plus resolved editorial overlay",
        "metadata_profile": PROFILE, "derivative_profile": DERIVATIVE_PROFILE,
        "accession_result": "accessioned", "deletion_authorized": "false",
    })
    representations = [
            {
                "record_key": row["record_key"], "inventory": row["inventory"],
                "role": "preservation_master", "source_path": str(source),
                "source_sha256": source_hash, "final_relative_path": master_relative,
                "final_sha256": master_hash, "size_bytes": master_size,
                "copy_policy": "metadata_embedded_pixels_and_technical_structure_verified",
                "basis": "human_selected_single_master", "deletion_authorized": "false",
            },
            {
                "record_key": row["record_key"], "inventory": row["inventory"],
                "role": "access_derivative", "source_path": master_relative,
                "source_sha256": master_hash, "final_relative_path": access_relative,
                "final_sha256": access_hash, "size_bytes": access_size,
                "copy_policy": DERIVATIVE_PROFILE,
                "basis": "generated_from_finalized_master_during_accession", "deletion_authorized": "false",
            },
    ]
    checksums = [
        {"path": master_relative, "sha256": master_hash, "size_bytes": master_size, "content_role": "preservation_master"},
        {"path": access_relative, "sha256": access_hash, "size_bytes": access_size, "content_role": "access_derivative"},
    ]
    return catalog, representations, checksums


def recover_existing(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    source_mount()
    source = allowed_source(Path(row["source_path"]))
    before = source.stat()
    binding = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    stem = safe_stem(row["record_key"])
    master_relative = f"masters/{stem}{master_suffix(source)}"
    access_relative = f"export/{stem}.jpg"
    master = UNIT / master_relative
    access = UNIT / access_relative
    if not master.is_file() or not access.is_file():
        raise AccessionError(f"incomplete prior output pair: {row['record_key']}")
    source_hash = sha256(source)
    master_plan = metadata_plan(row, source_hash)
    verify_metadata(master, master_plan)
    # Each existing pair was published only after the first run completed the
    # pixel comparison. Recovery rebinds hashes, metadata, type, dimensions,
    # technical TIFF fields, and ICC without repeating that expensive decode.
    verify_master(source, master, verify_pixels=False)
    master_hash = sha256(master)
    access_plan = metadata_plan(row, source_hash, master_relative, master_hash)
    verify_metadata(access, access_plan)
    width, height, orientation = dimensions(master)
    access_width, access_height, _ = dimensions(access)
    expected = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
    if (access_width, access_height) != expected:
        raise AccessionError(f"recovered access dimensions changed for {row['record_key']}")
    if exif_json(access, ("File:FileType",)).get("File:FileType") != "JPEG":
        raise AccessionError(f"recovered access derivative is not JPEG: {row['record_key']}")
    access_hash = sha256(access)
    if binding != (source.stat().st_dev, source.stat().st_ino, source.stat().st_size, source.stat().st_mtime_ns):
        raise AccessionError(f"source changed during recovery validation: {source}")
    return output_records(
        row, source, source_hash, before.st_size, master_relative, master_hash,
        master.stat().st_size, access_relative, access_hash, access.stat().st_size,
    )


def create_table(connection: sqlite3.Connection, name: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    connection.execute(f'CREATE TABLE "{name}" (' + ",".join(f'"{field}" TEXT NOT NULL' for field in fields) + ")")
    if rows:
        connection.executemany(
            f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
            [[str(row.get(field, "")) for field in fields] for row in rows],
        )


def finalize(catalog: list[dict[str, Any]], representations: list[dict[str, Any]], checksums: list[dict[str, Any]], dispositions: list[dict[str, str]], preflight: dict[str, Any], resumed_objects: int) -> dict[str, Any]:
    selected_by_candidate = {row["selected_candidate_id"]: row for row in catalog}
    source_disposition = []
    for row in dispositions:
        output = {field: row.get(field, "") for field in SOURCE_DISPOSITION_FIELDS}
        selected = selected_by_candidate.get(row["candidate_id"])
        if selected:
            output.update({
                "source_sha256": selected["source_sha256"],
                "final_master_path": selected["master_relative_path"],
                "final_master_sha256": selected["master_sha256"],
                "preservation_state": "accessioned_selected_master_source_retained",
            })
        else:
            output["preservation_state"] = "recorded_redundant_source_retained"
        output["deletion_authorized"] = "false"
        source_disposition.append(output)

    decision_fields, decision_rows = read_csv(DECISIONS)
    disposition_fields, disposition_rows = read_csv(DISPOSITIONS)
    metadata_fields, metadata_rows = read_csv(METADATA_DECISIONS)
    linked_fields, linked_rows = read_csv(LINKED_RELATIONSHIPS)
    derived_fields, derived_rows = read_csv(DERIVED_RELATIONSHIPS)
    document_fields, document_rows = read_csv(DOCUMENT_PROVENANCE)
    tables = (
        ("objects", list(CATALOG_FIELDS), catalog),
        ("representations", list(REPRESENTATION_FIELDS), representations),
        ("checksums", list(CHECKSUM_FIELDS), checksums),
        ("reconciliation_decisions", decision_fields, decision_rows),
        ("candidate_dispositions", disposition_fields, disposition_rows),
        ("source_disposition", list(SOURCE_DISPOSITION_FIELDS), source_disposition),
        ("linked_object_relationships", linked_fields, linked_rows),
        ("derived_image_relationships", derived_fields, derived_rows),
        ("metadata_editorial_decisions", metadata_fields, metadata_rows),
        ("document_provenance", document_fields, document_rows),
    )
    build = UNIT / "db-restore" / f".RESTORE.{os.getpid()}.tmp.sqlite"
    build.unlink(missing_ok=True)
    database = sqlite3.connect(build)
    try:
        for name, fields, rows in tables:
            create_table(database, name, fields, rows)
        create_table(database, "meta", ["key", "value"], [
            {"key": "format", "value": "iss-wf-alben-restore-v1"},
            {"key": "metadata_authority", "value": "captured museum-digital collection 607 plus resolved editorial overlay"},
            {"key": "metadata_profile", "value": PROFILE},
            {"key": "derivative_profile", "value": DERIVATIVE_PROFILE},
            {"key": "deletion_authorized", "value": "false"},
        ])
        database.commit()
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise AccessionError("RESTORE.sqlite quick_check failed")
        sql = "\n".join(database.iterdump()) + "\n"
    finally:
        database.close()

    controls = {
        UNIT / "CATALOG.csv": csv_bytes(CATALOG_FIELDS, catalog),
        UNIT / "metadata/museum-digital-effective.csv": csv_bytes(CATALOG_FIELDS, catalog),
        UNIT / "db-restore/RESTORE.csv": csv_bytes(CATALOG_FIELDS, catalog),
        UNIT / "db-restore/RESTORE.sql": sql.encode(),
        UNIT / "db-restore/checksums.csv": csv_bytes(CHECKSUM_FIELDS, checksums),
        UNIT / "db-restore/representations.csv": csv_bytes(REPRESENTATION_FIELDS, representations),
        UNIT / "db-restore/reconciliation-decisions.csv": csv_bytes(decision_fields, decision_rows),
        UNIT / "db-restore/candidate-dispositions.csv": csv_bytes(disposition_fields, disposition_rows),
        UNIT / "db-restore/source-disposition.csv": csv_bytes(SOURCE_DISPOSITION_FIELDS, source_disposition),
        UNIT / "db-restore/linked-object-relationships.csv": csv_bytes(linked_fields, linked_rows),
        UNIT / "db-restore/derived-image-relationships.csv": csv_bytes(derived_fields, derived_rows),
        UNIT / "db-restore/metadata-editorial-decisions.csv": csv_bytes(metadata_fields, metadata_rows),
        UNIT / "db-restore/document-provenance.csv": csv_bytes(document_fields, document_rows),
    }
    for path, payload in controls.items():
        atomic_bytes(path, payload)
    os.replace(build, UNIT / "db-restore/RESTORE.sqlite")

    readme = f"""# WF-Alben archive unit

This unit contains the fully reconciled and accessioned WF-Alben collection.

- Museum-digital catalogue records reviewed: {EXPECTED_RECORDS - 4}
- Archival image objects accessioned: {len(catalog)}
- Selected Masters: {len(catalog)}
- TIFF Masters: {preflight['formats']['tiff']}
- JPEG and approved surrogate Masters: {preflight['formats']['jpeg']}
- Generated full-resolution JPEG access derivatives: {len(catalog)}
- Linked museum-digital derivative records marked redundant: {EXPECTED_REDUNDANT_DERIVATIVES}
- Other candidate files recorded as redundant: {EXPECTED_REDUNDANT_CANDIDATES}
- Unresolved Master decisions: 0
- Unresolved metadata decisions: 0
- Deletion authorized: false

`masters/` contains exactly one human-selected Master for every archival image
object. Descriptive metadata was embedded into the accession copy; decoded
pixels, ICC data, and technical image structure were verified against the
selected source. `export/` contains one full-resolution JPEG generated from
each finalized Master during accession.

`CATALOG.csv` is the human-readable object catalogue. `db-restore/` contains
the rebuildable SQLite catalogue, SQL export, representation and checksum
tables, all review decisions, source dispositions, object relationships,
metadata corrections, and document provenance. `documents/` contains the 31
unique direct project documents collected before accession.

All 1,070 candidate relationships remain recorded. Redundant means not selected
as Master; it does not authorize deletion. Sources remain unchanged at their
recorded paths. The two catalogue surrogates remain protected evidence in the
WF-Alben work root.
"""
    atomic_bytes(UNIT / "00_README.md", readme.encode())

    database = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    try:
        quick = database.execute("PRAGMA quick_check").fetchone()[0]
        db_counts = {name: database.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in (
            "objects", "representations", "checksums", "reconciliation_decisions",
            "candidate_dispositions", "source_disposition", "derived_image_relationships",
            "metadata_editorial_decisions", "document_provenance",
        )}
    finally:
        database.close()
    expected = {
        "objects": EXPECTED_SELECTED, "representations": EXPECTED_SELECTED * 2,
        "checksums": EXPECTED_SELECTED * 2, "reconciliation_decisions": EXPECTED_RECORDS,
        "candidate_dispositions": EXPECTED_CANDIDATES, "source_disposition": EXPECTED_CANDIDATES,
        "derived_image_relationships": EXPECTED_REDUNDANT_DERIVATIVES,
        "metadata_editorial_decisions": EXPECTED_METADATA_DECISIONS,
        "document_provenance": 64,
    }
    if quick != "ok" or db_counts != expected:
        raise AccessionError(f"final SQLite validation failed: {quick=} {db_counts=}")
    control_hashes = {str(path.relative_to(UNIT)): sha256(path) for path in controls}
    control_hashes["00_README.md"] = sha256(UNIT / "00_README.md")
    completion = {
        "format": "iss-wf-alben-accession-completion-v1", "completed_at": now(),
        "result": "ok", "unit": str(UNIT), "museum_digital_records": 178,
        "archival_objects": EXPECTED_SELECTED, "selected_masters": EXPECTED_SELECTED,
        "access_derivatives": EXPECTED_SELECTED, "representation_rows": len(representations),
        "checksum_rows": len(checksums), "redundant_derivative_records": EXPECTED_REDUNDANT_DERIVATIVES,
        "redundant_candidates": EXPECTED_REDUNDANT_CANDIDATES, "metadata_decisions": EXPECTED_METADATA_DECISIONS,
        "unresolved_master_decisions": 0, "unresolved_metadata_decisions": 0,
        "metadata_profile": PROFILE, "derivative_profile": DERIVATIVE_PROFILE,
        "recovered_and_reverified_prior_outputs": resumed_objects,
        "final_hash_policy": "one SHA-256 per finalized Master and generated derivative",
        "control_file_sha256": control_hashes, "sqlite_quick_check": quick,
        "source_read_only": True, "source_modified": False,
        "deletion_authorized": False, "write_boundary_state": "iss-archive-work and iss-archive only",
    }
    atomic_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    atomic_json(UNIT / "metadata/accession-state.json", {
        "format": "iss-wf-alben-accession-state-v1", "state": "complete",
        "completed_at": completion["completed_at"],
        "completion": "00_MANIFESTS/accession-completion.json",
    })
    return completion


def preflight() -> dict[str, Any]:
    selected, _, summary = load_scope()
    media_dirs = (UNIT / "masters", UNIT / "export")
    if any(path.exists() for path in media_dirs):
        state_path = UNIT / "metadata/accession-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        master_count = len(list((UNIT / "masters").glob("*"))) if (UNIT / "masters").is_dir() else -1
        export_count = len(list((UNIT / "export").glob("*"))) if (UNIT / "export").is_dir() else -1
        if state.get("state") != "failed" or master_count != export_count or master_count != int(state.get("completed_objects", -2)):
            raise AccessionError("existing WF-Alben accession output is not a valid failed-run checkpoint")
        if (UNIT / "db-restore").exists() and any((UNIT / "db-restore").iterdir()):
            raise AccessionError("restore outputs unexpectedly exist in failed-run checkpoint")
        summary["recoverable_completed_objects"] = master_count
    elif (UNIT / "db-restore").exists():
        raise AccessionError("restore output exists without media output")
    if not (UNIT / "documents").is_dir() or not (UNIT / "metadata").is_dir() or not (UNIT / "00_MANIFESTS").is_dir():
        raise AccessionError("prepared WF-Alben unit structure is incomplete")
    if len(selected) != EXPECTED_SELECTED:
        raise AccessionError("selected-master count drifted")
    return summary


def run() -> dict[str, Any]:
    selected, dispositions, summary = load_scope()
    recovering = (UNIT / "masters").is_dir() or (UNIT / "export").is_dir()
    if recovering:
        preflight()
        (UNIT / "db-restore").mkdir(parents=False, exist_ok=True)
    else:
        for path in (UNIT / "masters", UNIT / "export", UNIT / "db-restore"):
            path.mkdir(parents=False, exist_ok=False)
    atomic_json(UNIT / "metadata/accession-state.json", {
        "format": "iss-wf-alben-accession-state-v1", "state": "running",
        "started_at": now(), "selected_masters": EXPECTED_SELECTED,
    })
    catalog: list[dict[str, Any]] = []
    representations: list[dict[str, Any]] = []
    checksums: list[dict[str, Any]] = []
    resumed_objects = 0
    started = time.monotonic()
    try:
        for index, row in enumerate(selected, 1):
            stem = safe_stem(row["record_key"])
            source = Path(row["source_path"])
            master = UNIT / "masters" / f"{stem}{master_suffix(source)}"
            access = UNIT / "export" / f"{stem}.jpg"
            if master.exists() or access.exists():
                item, reps, hashes = recover_existing(row)
                resumed_objects += 1
                recovered = True
            else:
                item, reps, hashes = process(row, UNIT / "masters", UNIT / "export")
                recovered = False
            catalog.append(item); representations.extend(reps); checksums.extend(hashes)
            elapsed = max(time.monotonic() - started, 0.001)
            remaining = elapsed / index * (len(selected) - index)
            print(json.dumps({
                "completed": index, "total": len(selected), "record_key": row["record_key"],
                "inventory": row["inventory"], "elapsed_seconds": round(elapsed, 1),
                "estimated_remaining_seconds": round(remaining, 1), "recovered": recovered,
            }, ensure_ascii=False), flush=True)
        completion = finalize(catalog, representations, checksums, dispositions, summary, resumed_objects)
        print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        return completion
    except Exception as error:
        atomic_json(UNIT / "metadata/accession-state.json", {
            "format": "iss-wf-alben-accession-state-v1", "state": "failed",
            "failed_at": now(), "completed_objects": len(catalog), "error": str(error),
            "deletion_authorized": False,
        })
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run"))
    args = parser.parse_args()
    try:
        result = preflight() if args.command == "preflight" else run()
        if args.command == "preflight":
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except AccessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
