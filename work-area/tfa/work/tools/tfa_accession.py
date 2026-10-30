#!/usr/bin/env python3
"""Guarded, resumable accession of the museum-digital TFA scope."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


WORK = Path("/mnt/volume/iss-archive-work/tfa")
UNIT = Path("/mnt/volume/iss-archive/units/tfa")
REVIEW = WORK / "work/reconciliation/unit-reconciliation"
RUN = WORK / "work/accession-runs/tfa-md-001"
PILOT = RUN / "pilot"
CANDIDATES = RUN / "candidates"
RECEIPTS = RUN / "receipts"
STATUS = RUN / "status.json"
ERRORS = RUN / "errors.jsonl"
CRITICAL = RUN / "critical.json"
COMPLETION = RUN / "completion.json"
PUBLICATION = RUN / "publication.json"
LOCK = RUN / "worker.lock"
LOG = RUN / "watchdog.jsonl"
CATALOG_SOURCE = REVIEW / "md-accession-catalog.csv"
PREPARATION = REVIEW / "md-accession-preparation.json"
SUBDIVISIONS = REVIEW / "subdivision-register.csv"
MEMBERSHIPS = REVIEW / "subdivision-memberships.csv"
DECISIONS = REVIEW / "workbench-decisions.csv"
SOURCE_DISPOSITION = REVIEW / "source-disposition-proposal.csv"
DRAIN = REVIEW / "drain-list-proposal.csv"
SOURCE_ROOT = Path("/media/vladimir/Elements")
PROFILE = "iss-tfa-md-metadata-v1"
DERIVATIVE_PROFILE = "libvips-full-resolution-jpeg-q95-v1"
EXPECTED_MD = 5282
EXPECTED_READY = 5281
EXPECTED_MEMBERSHIPS = 10723
EXPECTED_DRAIN = 41724
JPEG_SURROGATE_INVENTORIES = frozenset({"TFA-6323033", "TFA-6423379"})
SEP = "\x1f"
RIGHTS_TAGS = (
    "XMP-dc:Rights", "IPTC:CopyrightNotice", "XMP-xmpRights:UsageTerms",
    "XMP-xmpRights:WebStatement",
)
TECHNICAL_TAGS = (
    "File:FileType", "File:MIMEType", "IFD0:ImageWidth", "IFD0:ImageHeight",
    "IFD0:BitsPerSample", "IFD0:Compression", "IFD0:PhotometricInterpretation",
    "IFD0:SamplesPerPixel", "IFD0:PlanarConfiguration", "IFD0:XResolution",
    "IFD0:YResolution", "IFD0:ResolutionUnit", "IFD0:Orientation",
    "EXIF:ExifImageWidth", "EXIF:ExifImageHeight", "Photoshop:LayerCount",
    "Photoshop:LayerNames", "Photoshop:HasRealMergedData",
)
CATALOG_EXTRA_FIELDS = (
    "master_relative_path", "master_sha256", "source_sha256",
    "access_relative_path", "access_sha256", "access_size_bytes",
    "derivative_profile", "accession_result", "accession_error",
)
REPRESENTATION_FIELDS = (
    "record_key", "inventory", "role", "source_path", "source_sha256",
    "final_relative_path", "final_sha256", "size_bytes", "completed_at",
    "copy_policy", "basis", "deletion_authorized",
)
CHECKSUM_FIELDS = ("path", "sha256", "size_bytes", "content_role")
HOLD_FIELDS = (
    "record_key", "md_object_id", "inventory", "accession_state",
    "accession_hold_reason", "related_record_key", "selected_master_path",
    "evidence_path",
)


class AccessionError(RuntimeError):
    pass


class NonCriticalError(AccessionError):
    pass


class IntegrityError(AccessionError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    fields = list(fields)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise IntegrityError("source mount did not resolve exactly once")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise IntegrityError(f"source mount is not protected read-only: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def inside(path: Path, root: Path, *, exists: bool = False) -> Path:
    resolved = path.resolve(strict=exists)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise IntegrityError(f"path escaped approved root: {resolved}") from error
    return resolved


def run_tool(arguments: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments, check=True, capture_output=True, text=True, env=env, timeout=3600
        )
    except FileNotFoundError as error:
        raise IntegrityError(f"required tool unavailable: {arguments[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise NonCriticalError(f"{arguments[0]} exceeded one hour") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise NonCriticalError(
            f"{arguments[0]} failed: {detail or error.returncode}"
        ) from error


def exif_json(path: Path, tags: Iterable[str], *, numeric: bool = False) -> dict[str, Any]:
    arguments = ["exiftool", "-j", "-G1", "-s", "-sep", SEP]
    if numeric:
        arguments.append("-n")
    arguments.extend(f"-{tag}" for tag in tags)
    arguments.append(str(path))
    try:
        payload = json.loads(run_tool(arguments).stdout)
    except json.JSONDecodeError as error:
        raise NonCriticalError(f"ExifTool returned invalid JSON for {path.name}") from error
    if len(payload) != 1:
        raise NonCriticalError(f"ExifTool returned no single record for {path.name}")
    payload[0].pop("SourceFile", None)
    return payload[0]


def normalize(value: Any) -> str:
    if isinstance(value, list):
        return SEP.join(str(part) for part in value)
    return "" if value is None else str(value)


def split_terms(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def master_suffix(row: dict[str, str]) -> str:
    source_suffix = Path(row["selected_master_path"]).suffix.casefold()
    if source_suffix in {".tif", ".tiff"}:
        return ".tif"
    if row["inventory"] in JPEG_SURROGATE_INVENTORIES and source_suffix in {".jpg", ".jpeg"}:
        return ".jpg"
    raise IntegrityError(
        f"unsupported selected Master format for {row['inventory']}: {source_suffix}"
    )


def metadata_plan(
    row: dict[str, str],
    source_hash: str,
    master_hash: str = "",
    master_relative_path: str = "",
) -> dict[str, str | None]:
    title = row["title"].strip()
    description = row["description"].strip()
    creator = row["creator"].strip()
    subjects = SEP.join(split_terms(row["tags"]))
    date_text = row["date_text"].strip()
    strict_date: str | None = date_text if re.fullmatch(r"\d{4}", date_text) else None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        strict_date = date_text.replace("-", ":")
    physical = "; ".join(
        part for part in (
            f"Material/Technik: {row['material'].strip()}" if row["material"].strip() else "",
            f"Maße: {row['dimensions'].strip()}" if row["dimensions"].strip() else "",
        ) if part
    )
    relations = [
        f"iss:metadata-profile:{PROFILE}", f"museum-digital:object:{row['md_object_id']}",
        row["md_url"], source_hash,
    ]
    if master_hash:
        if not master_relative_path:
            raise IntegrityError("master hash supplied without a master path")
        relations.extend((f"master:{master_relative_path}", master_hash))
    plan: dict[str, str | None] = {
        "XMP-dc:Identifier": row["record_key"],
        "XMP-iptcExt:ArtworkSourceInventoryNo": row["inventory"],
        "XMP-dc:Title": title or None,
        "XMP-iptcExt:ArtworkTitle": title or None,
        "XMP-dc:Description": description or None,
        "XMP-iptcExt:ArtworkContentDescription": description or None,
        "IFD0:ImageDescription": description or None,
        "XMP-dc:Date": strict_date,
        "XMP-iptcExt:ArtworkCircaDateCreated": date_text or None,
        "XMP-dc:Type": row["object_type"].strip() or None,
        "XMP-iptcExt:ArtworkPhysicalDescription": physical or None,
        "XMP-dc:Coverage": row["places"].strip() or None,
        "XMP-dc:Subject": subjects or None,
        "XMP-dc:Creator": creator or None,
        "XMP-dc:Source": row["selected_master_path"],
        "XMP-dc:Relation": SEP.join(relations),
        "IPTC:ObjectName": title if title and len(title.encode()) <= 64 else None,
        "IPTC:Caption-Abstract": description if description and len(description.encode()) <= 2000 else None,
        "IPTC:Keywords": subjects or None,
        "IPTC:By-line": creator if creator and len(creator.encode()) <= 32 else None,
    }
    plan["IPTC:CodedCharacterSet"] = (
        "UTF8" if any(plan[key] for key in ("IPTC:ObjectName", "IPTC:Caption-Abstract", "IPTC:Keywords", "IPTC:By-line")) else None
    )
    return plan


def copy_with_metadata(source: Path, destination: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    if destination.exists():
        raise IntegrityError(f"candidate collision: {destination}")
    arguments = ["exiftool", "-charset", "IPTC=UTF8", "-sep", SEP]
    merged: dict[str, str | None] = dict(plan)
    for tag in RIGHTS_TAGS:
        value = rights.get(tag)
        if value not in (None, ""):
            merged[tag] = normalize(value)
    arguments.extend(f"-{tag}={value or ''}" for tag, value in merged.items())
    arguments.extend(("-o", str(destination), str(source)))
    result = run_tool(arguments)
    warnings = [line for line in result.stderr.splitlines() if "Warning:" in line]
    serious = [line for line in warnings if "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise NonCriticalError(f"ExifTool warning for {source.name}: {'; '.join(serious)}")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise NonCriticalError(f"ExifTool produced no valid selected Master for {source.name}")
    fsync_file(destination)


def write_metadata(path: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    arguments = ["exiftool", "-overwrite_original", "-charset", "IPTC=UTF8", "-sep", SEP]
    merged: dict[str, str | None] = dict(plan)
    for tag in RIGHTS_TAGS:
        value = rights.get(tag)
        if value not in (None, ""):
            merged[tag] = normalize(value)
    arguments.extend(f"-{tag}={value or ''}" for tag, value in merged.items())
    arguments.append(str(path))
    result = run_tool(arguments)
    warnings = [line for line in result.stderr.splitlines() if "Warning:" in line]
    serious = [line for line in warnings if "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise NonCriticalError(f"ExifTool derivative warning: {'; '.join(serious)}")
    fsync_file(path)


def verify_metadata(path: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    observed = exif_json(path, tuple(plan) + RIGHTS_TAGS)
    for tag, expected in plan.items():
        actual = normalize(observed.get(tag))
        if (expected is None and actual) or (expected is not None and actual != expected):
            raise NonCriticalError(f"metadata readback mismatch {tag} in {path.name}")
    for tag, expected in rights.items():
        if expected not in (None, "") and normalize(observed.get(tag)) != normalize(expected):
            raise NonCriticalError(f"rights readback mismatch {tag} in {path.name}")


def binary_tag_hash(path: Path, tag: str) -> str:
    process = subprocess.Popen(
        ["exiftool", "-b", f"-{tag}", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256(); size = 0
    for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
        size += len(block); digest.update(block)
    detail = process.stderr.read().decode(errors="replace").strip()
    if process.wait():
        raise NonCriticalError(f"cannot read {tag} from {path.name}: {detail}")
    return f"{size}:sha256:{digest.hexdigest()}"


def scalar_depth(technical: dict[str, Any]) -> int:
    value = technical.get("IFD0:BitsPerSample", 8)
    try:
        parts = [int(part) for part in value] if isinstance(value, list) else [int(part) for part in str(value).replace(",", " ").split()]
    except ValueError as error:
        raise NonCriticalError(f"unsupported BitsPerSample: {value!r}") from error
    if not parts or len(set(parts)) != 1 or parts[0] not in (1, 2, 4, 8, 16):
        raise NonCriticalError(f"unsupported BitsPerSample: {value!r}")
    return parts[0]


def pixel_digest(path: Path, depth: int) -> str:
    try:
        process = subprocess.Popen(
            ["magick", str(path), "-depth", str(depth), "RGBA:-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise IntegrityError("required tool unavailable: magick") from error
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    for block in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
        digest.update(block)
    detail = process.stderr.read().decode(errors="replace").strip()
    if process.wait():
        raise NonCriticalError(f"TIFF decode failed for {path.name}: {detail}")
    return "sha256:" + digest.hexdigest()


def verify_structure(source: Path, candidate: Path) -> tuple[dict[str, Any], str]:
    source_technical = exif_json(source, TECHNICAL_TAGS, numeric=True)
    candidate_technical = exif_json(candidate, TECHNICAL_TAGS, numeric=True)
    if source_technical != candidate_technical:
        raise IntegrityError(f"technical image structure changed for {source.name}")
    if any(key.endswith(":LayerCount") for key in source_technical):
        if binary_tag_hash(source, "PhotoshopSettings") != binary_tag_hash(candidate, "PhotoshopSettings"):
            raise IntegrityError(f"Photoshop layer data changed for {source.name}")
    if binary_tag_hash(source, "ICC_Profile") != binary_tag_hash(candidate, "ICC_Profile"):
        raise IntegrityError(f"ICC profile changed for {source.name}")
    depth = scalar_depth(source_technical)
    source_pixels = pixel_digest(source, depth)
    if pixel_digest(candidate, depth) != source_pixels:
        raise IntegrityError(f"decoded pixels changed for {source.name}")
    return source_technical, source_pixels


def dimensions(path: Path) -> tuple[int, int, int]:
    observed = exif_json(path, ("ImageWidth", "ImageHeight", "Orientation"), numeric=True)
    width = [int(v) for key, v in observed.items() if key.endswith(":ImageWidth")]
    height = [int(v) for key, v in observed.items() if key.endswith(":ImageHeight")]
    orientation = [int(v) for key, v in observed.items() if key.endswith(":Orientation")]
    if not width or not height:
        raise NonCriticalError(f"cannot determine dimensions for {path.name}")
    return width[0], height[0], orientation[0] if orientation else 1


def generate_access(master: Path, destination: Path) -> None:
    environment = dict(os.environ)
    environment.update({"VIPS_CONCURRENCY": "4", "TMPDIR": str(RUN / "tmp")})
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    run_tool(
        ["vips", "autorot", str(master), f"{destination}[Q=95,optimize-coding=true,keep=icc]"],
        env=environment,
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise NonCriticalError(f"libvips produced no valid JPEG for {master.name}")
    fsync_file(destination)


def load_scope() -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    receipt = json.loads(PREPARATION.read_text(encoding="utf-8"))
    if (
        receipt.get("result") != "ok" or receipt.get("metadata_authority") != "museum-digital"
        or int(receipt.get("md_objects", -1)) != EXPECTED_MD
        or int(receipt.get("ready_for_accession", -1)) != EXPECTED_READY
        or receipt.get("deletion_authorized") is not False
    ):
        raise IntegrityError("MD accession preparation is not valid")
    if receipt["fingerprints"].get(CATALOG_SOURCE.name) != sha256(CATALOG_SOURCE):
        raise IntegrityError("MD accession catalogue fingerprint changed")
    fields, rows = read_csv(CATALOG_SOURCE)
    if len(rows) != EXPECTED_MD or len({row["record_key"] for row in rows}) != EXPECTED_MD:
        raise IntegrityError("MD accession scope is not one row per record")
    ready = [row for row in rows if row["accession_state"] == "ready_for_accession"]
    if len(ready) != EXPECTED_READY or len({row["selected_master_path"] for row in ready}) != EXPECTED_READY:
        raise IntegrityError("ready scope is not one distinct Master per object")
    return fields, rows, receipt


def preflight(*, allow_unit: bool = False) -> dict[str, Any]:
    mount = source_mount()
    for path in (WORK, REVIEW, CATALOG_SOURCE, PREPARATION, SUBDIVISIONS, MEMBERSHIPS, DECISIONS, SOURCE_DISPOSITION, DRAIN):
        inside(path, WORK, exists=True)
    inside(UNIT, Path("/mnt/volume/iss-archive"), exists=False)
    if UNIT.exists() and not allow_unit:
        raise IntegrityError(f"final TFA unit already exists: {UNIT}")
    fields, rows, preparation = load_scope()
    ready = [row for row in rows if row["accession_state"] == "ready_for_accession"]
    selected = {row["selected_master_path"] for row in ready}
    _, drain_rows = read_csv(DRAIN)
    if len(drain_rows) != EXPECTED_DRAIN or selected & {row["source_path"] for row in drain_rows}:
        raise IntegrityError("selected Master intersects the drain list or drain count drifted")
    total = 0
    for row in ready:
        source = inside(Path(row["selected_master_path"]), SOURCE_ROOT, exists=True)
        stat = source.stat()
        if not source.is_file():
            raise IntegrityError(f"selected Master is not a file: {source}")
        master_suffix(row)
        if stat.st_size != int(row["selected_master_size_bytes"]):
            raise IntegrityError(f"selected Master size changed: {source}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", row["inventory"]):
            raise IntegrityError(f"unsafe inventory filename: {row['inventory']}")
        total += stat.st_size
    free = shutil.disk_usage(Path("/mnt/volume")).free
    required = total * 2 + 200 * 1024**3
    if free < required:
        raise IntegrityError(f"insufficient free space: {free} < {required}")
    return {
        "result": "ok", "checked_at": now(), "md_objects": len(rows),
        "ready_objects": len(ready), "selected_master_bytes": total,
        "selected_master_gib": round(total / 1024**3, 3), "free_bytes": free,
        "required_free_bytes_guard": required, "source_mount": mount,
        "metadata_authority": preparation["metadata_authority"],
        "deletion_authorized": False, "catalog_fields": len(fields),
    }


def publish_unit_dirs() -> None:
    if UNIT.exists():
        marker = UNIT / "metadata/accession-state.json"
        if not marker.is_file():
            raise IntegrityError("existing TFA unit has no resumable accession marker")
        state = json.loads(marker.read_text(encoding="utf-8"))
        if state.get("format") != "iss-tfa-accession-state-v1" or state.get("run") != str(RUN):
            raise IntegrityError("existing TFA unit belongs to another accession")
        return
    for path in (
        UNIT / "masters", UNIT / "export/access", UNIT / "metadata",
        UNIT / "db-restore/manifests",
    ):
        path.mkdir(parents=True, exist_ok=False)
    atomic_json(
        UNIT / "metadata/accession-state.json",
        {"format": "iss-tfa-accession-state-v1", "created_at": now(), "run": str(RUN), "state": "running"},
    )


def process_one(row: dict[str, str], *, publish: bool) -> dict[str, Any]:
    source_mount()
    source = inside(Path(row["selected_master_path"]), SOURCE_ROOT, exists=True)
    before_stat = source.stat()
    before = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)
    if before_stat.st_size != int(row["selected_master_size_bytes"]):
        raise IntegrityError(f"source size changed before accession: {source}")
    if shutil.disk_usage(Path("/mnt/volume")).free < max(100 * 1024**3, before_stat.st_size * 4):
        raise IntegrityError("free-space safety floor reached")
    output = CANDIDATES if publish else PILOT
    output.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    suffix = master_suffix(row)
    master_relative_path = f"masters/{row['inventory']}{suffix}"
    master_candidate = output / f".{row['inventory']}.{token}.master{suffix}"
    access_candidate = output / f".{row['inventory']}.{token}.access.jpg"
    started = time.monotonic()
    try:
        source_hash = sha256(source)
        rights = exif_json(source, RIGHTS_TAGS)
        plan_master = metadata_plan(row, source_hash)
        copy_with_metadata(source, master_candidate, plan_master, rights)
        verify_metadata(master_candidate, plan_master, rights)
        technical, pixels = verify_structure(source, master_candidate)
        after_stat = source.stat()
        after = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns)
        if before != after:
            raise IntegrityError(f"source changed during accession: {source}")
        master_hash = sha256(master_candidate)
        plan_access = metadata_plan(
            row, source_hash, master_hash, master_relative_path
        )
        generate_access(master_candidate, access_candidate)
        write_metadata(access_candidate, plan_access, rights)
        verify_metadata(access_candidate, plan_access, rights)
        width, height, orientation = dimensions(master_candidate)
        access_width, access_height, _ = dimensions(access_candidate)
        expected = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
        if (access_width, access_height) != expected:
            raise IntegrityError(f"access dimensions changed: {(access_width, access_height)} != {expected}")
        if exif_json(access_candidate, ("File:FileType",)).get("File:FileType") != "JPEG":
            raise IntegrityError("access derivative is not JPEG")
        access_hash = sha256(access_candidate)
        receipt: dict[str, Any] = {
            "format": "iss-tfa-accession-object-v1", "completed_at": now(),
            "record_key": row["record_key"], "md_object_id": row["md_object_id"],
            "inventory": row["inventory"], "source_path": str(source),
            "source_sha256": source_hash, "source_size_bytes": before_stat.st_size,
            "master_relative_path": master_relative_path,
            "master_sha256": master_hash, "master_size_bytes": master_candidate.stat().st_size,
            "master_pixel_digest": pixels, "master_technical": technical,
            "access_relative_path": f"export/access/{row['inventory']}.jpg",
            "access_sha256": access_hash, "access_size_bytes": access_candidate.stat().st_size,
            "access_width": access_width, "access_height": access_height,
            "metadata_profile": PROFILE, "derivative_profile": DERIVATIVE_PROFILE,
            "elapsed_seconds": round(time.monotonic() - started, 3), "published": publish,
            "source_modified": False, "deletion_authorized": False,
        }
        if not publish:
            destination = PILOT / f"{row['inventory']}.json"
            atomic_json(destination, receipt)
            return receipt
        master = UNIT / receipt["master_relative_path"]
        access = UNIT / receipt["access_relative_path"]
        if master.exists() or access.exists():
            raise IntegrityError(f"unreceipted accession destination exists: {row['inventory']}")
        os.replace(master_candidate, master); fsync_dir(master.parent)
        os.replace(access_candidate, access); fsync_dir(access.parent)
        os.chmod(master, 0o444); os.chmod(access, 0o444)
        for path, size_key, inode_key, mtime_key in (
            (master, "master_size_bytes", "master_inode", "master_mtime_ns"),
            (access, "access_size_bytes", "access_inode", "access_mtime_ns"),
        ):
            stat = path.stat()
            if stat.st_size != receipt[size_key] or stat.st_nlink != 1:
                raise IntegrityError(f"published file binding failed: {path}")
            receipt[inode_key] = stat.st_ino; receipt[mtime_key] = stat.st_mtime_ns
        atomic_json(RECEIPTS / f"{row['inventory']}.json", receipt)
        return receipt
    finally:
        master_candidate.unlink(missing_ok=True)
        access_candidate.unlink(missing_ok=True)


def load_receipts() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not RECEIPTS.exists():
        return found
    for path in sorted(RECEIPTS.glob("*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        inventory = str(receipt.get("inventory") or "")
        if not inventory or inventory in found or receipt.get("published") is not True:
            raise IntegrityError(f"invalid receipt: {path}")
        for prefix in ("master", "access"):
            target = UNIT / receipt[f"{prefix}_relative_path"]
            stat = target.stat()
            if stat.st_size != int(receipt[f"{prefix}_size_bytes"]) or stat.st_ino != int(receipt[f"{prefix}_inode"]) or stat.st_nlink != 1:
                raise IntegrityError(f"receipt/file binding drifted: {target}")
        found[inventory] = receipt
    return found


def load_errors() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not ERRORS.exists():
        return found
    for line in ERRORS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        inventory = str(row.get("inventory") or "")
        if not inventory or inventory in found:
            raise IntegrityError("invalid or duplicate noncritical error")
        found[inventory] = row
    return found


def create_table(db: sqlite3.Connection, name: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    db.execute(f'CREATE TABLE "{name}" (' + ",".join(f'"{field}" TEXT NOT NULL' for field in fields) + ")")
    if rows:
        db.executemany(
            f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
            [[str(row.get(field, "")) for field in fields] for row in rows],
        )


def finalize(all_fields: list[str], all_rows: list[dict[str, str]]) -> dict[str, Any]:
    receipts = load_receipts(); errors = load_errors()
    ready = [row for row in all_rows if row["accession_state"] == "ready_for_accession"]
    if len(receipts) + len(errors) != len(ready):
        raise IntegrityError("ready objects are neither accessioned nor error-logged")
    by_inventory = {row["inventory"]: row for row in all_rows}
    if set(receipts) | set(errors) != {row["inventory"] for row in ready}:
        raise IntegrityError("receipt/error scope differs from ready accession scope")
    catalog = []
    representations = []
    checksums = []
    holds = []
    for row in all_rows:
        output = dict(row)
        output.update({field: "" for field in CATALOG_EXTRA_FIELDS})
        receipt = receipts.get(row["inventory"])
        error = errors.get(row["inventory"])
        if receipt:
            output.update({
                "master_relative_path": receipt["master_relative_path"],
                "master_sha256": receipt["master_sha256"],
                "source_sha256": receipt["source_sha256"],
                "access_relative_path": receipt["access_relative_path"],
                "access_sha256": receipt["access_sha256"],
                "access_size_bytes": receipt["access_size_bytes"],
                "derivative_profile": DERIVATIVE_PROFILE,
                "accession_result": "accessioned",
            })
            representations.extend((
                {
                    "record_key": row["record_key"], "inventory": row["inventory"],
                    "role": "preservation_master", "source_path": receipt["source_path"],
                    "source_sha256": receipt["source_sha256"],
                    "final_relative_path": receipt["master_relative_path"],
                    "final_sha256": receipt["master_sha256"],
                    "size_bytes": receipt["master_size_bytes"], "completed_at": receipt["completed_at"],
                    "copy_policy": "verified_metadata_rewrite_atomic_publication",
                    "basis": "museum_digital_metadata_embedded_pixels_and_structure_unchanged",
                    "deletion_authorized": "false",
                },
                {
                    "record_key": row["record_key"], "inventory": row["inventory"],
                    "role": "access_derivative", "source_path": receipt["master_relative_path"],
                    "source_sha256": receipt["master_sha256"],
                    "final_relative_path": receipt["access_relative_path"],
                    "final_sha256": receipt["access_sha256"],
                    "size_bytes": receipt["access_size_bytes"], "completed_at": receipt["completed_at"],
                    "copy_policy": DERIVATIVE_PROFILE,
                    "basis": "generated_from_finalized_metadata_embedded_master",
                    "deletion_authorized": "false",
                },
            ))
            checksums.extend((
                {"path": receipt["master_relative_path"], "sha256": receipt["master_sha256"], "size_bytes": receipt["master_size_bytes"], "content_role": "preservation_master"},
                {"path": receipt["access_relative_path"], "sha256": receipt["access_sha256"], "size_bytes": receipt["access_size_bytes"], "content_role": "access_derivative"},
            ))
        elif error:
            output.update({"accession_result": "noncritical_error_hold", "accession_error": error["error"]})
            holds.append({
                "record_key": row["record_key"], "md_object_id": row["md_object_id"],
                "inventory": row["inventory"], "accession_state": "noncritical_error_hold",
                "accession_hold_reason": error["error"], "related_record_key": "",
                "selected_master_path": row["selected_master_path"], "evidence_path": str(ERRORS),
            })
        else:
            output["accession_result"] = "catalogue_duplicate_hold"
            holds.append({
                "record_key": row["record_key"], "md_object_id": row["md_object_id"],
                "inventory": row["inventory"], "accession_state": row["accession_state"],
                "accession_hold_reason": row["accession_hold_reason"],
                "related_record_key": row["archival_object_identifier"],
                "selected_master_path": row["selected_master_path"],
                "evidence_path": str(REVIEW / "md-55436-md-85642-provenance-correction.json"),
            })
        catalog.append(output)
    source_fields, source_rows = read_csv(SOURCE_DISPOSITION)
    ready_by_source = {receipt["source_path"]: receipt for receipt in receipts.values()}
    for row in source_rows:
        receipt = ready_by_source.get(row["source_path"])
        if receipt:
            row.update({
                "inventory": receipt["inventory"], "source_role": "selected_master",
                "final_path": receipt["master_relative_path"],
                "preservation_status": "preserved_metadata_enriched_master",
                "preservation_basis": "human_selected_master_museum_digital_metadata_embedded",
                "source_sha256": receipt["source_sha256"], "final_sha256": receipt["master_sha256"],
                "exact_copy_proven": "false", "drain_state": "retain_selected_master",
                "deletion_authorized": "false",
                "notes": "Read-only source retained unchanged; accessioned Master preserves decoded pixels and technical structure.",
            })
    subdivision_fields, subdivisions = read_csv(SUBDIVISIONS)
    membership_fields, memberships = read_csv(MEMBERSHIPS)
    decision_fields, decisions = read_csv(DECISIONS)
    drain_fields, drain_rows = read_csv(DRAIN)
    if len(memberships) != EXPECTED_MEMBERSHIPS or len(drain_rows) != EXPECTED_DRAIN:
        raise IntegrityError("facet or drain scope drifted during finalization")
    catalog_fields = all_fields + list(CATALOG_EXTRA_FIELDS)
    build = RUN / f"RESTORE.building-{os.getpid()}.sqlite"
    build.unlink(missing_ok=True)
    db = sqlite3.connect(build)
    try:
        tables = (
            ("objects", catalog_fields, catalog),
            ("representations", list(REPRESENTATION_FIELDS), representations),
            ("checksums", list(CHECKSUM_FIELDS), checksums),
            ("subdivisions", subdivision_fields, subdivisions),
            ("object_subdivisions", membership_fields, memberships),
            ("reconciliation_decisions", decision_fields, decisions),
            ("source_disposition", source_fields, source_rows),
            ("drain_list", drain_fields, drain_rows),
            ("accession_holds", list(HOLD_FIELDS), holds),
        )
        for name, fields, rows in tables:
            create_table(db, name, fields, rows)
        create_table(db, "meta", ["key", "value"], [
            {"key": "format", "value": "iss-tfa-restore-v1"},
            {"key": "metadata_authority", "value": "museum-digital"},
            {"key": "metadata_profile", "value": PROFILE},
            {"key": "derivative_profile", "value": DERIVATIVE_PROFILE},
            {"key": "deletion_authorized", "value": "false"},
        ])
        db.commit()
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise IntegrityError("RESTORE.sqlite quick_check failed")
        sql = "\n".join(db.iterdump()) + "\n"
    finally:
        db.close()
    sys.path.insert(0, "/home/vladimir/wp/tools/archive-preservation")
    import paragon_document_bundle  # noqa: PLC0415
    xlsx = paragon_document_bundle.table_xlsx_bytes(
        catalog_fields, catalog, sheet_name="Catalog", table_name="Catalog"
    )
    controls = {
        UNIT / "CATALOG.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "CATALOG.xlsx": xlsx,
        UNIT / "metadata/museum-digital-catalog.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "metadata/subdivision-register.csv": csv_bytes(subdivision_fields, subdivisions),
        UNIT / "metadata/subdivision-memberships.csv": csv_bytes(membership_fields, memberships),
        UNIT / "db-restore/RESTORE.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "db-restore/RESTORE.xlsx": xlsx,
        UNIT / "db-restore/RESTORE.sql": sql.encode(),
        UNIT / "db-restore/checksums.csv": csv_bytes(CHECKSUM_FIELDS, checksums),
        UNIT / "db-restore/manifests/representations.csv": csv_bytes(REPRESENTATION_FIELDS, representations),
        UNIT / "db-restore/manifests/subdivision-register.csv": csv_bytes(subdivision_fields, subdivisions),
        UNIT / "db-restore/manifests/subdivision-memberships.csv": csv_bytes(membership_fields, memberships),
        UNIT / "db-restore/manifests/reconciliation-decisions.csv": csv_bytes(decision_fields, decisions),
        UNIT / "db-restore/manifests/source-disposition.csv": csv_bytes(source_fields, source_rows),
        UNIT / "db-restore/manifests/drain-list.csv": csv_bytes(drain_fields, drain_rows),
        UNIT / "db-restore/manifests/accession-holds.csv": csv_bytes(HOLD_FIELDS, holds),
    }
    for path, payload in controls.items():
        atomic_bytes(path, payload)
    os.replace(build, UNIT / "db-restore/RESTORE.sqlite"); fsync_dir(UNIT / "db-restore")
    jpeg_surrogates = sum(
        receipt["master_relative_path"].casefold().endswith((".jpg", ".jpeg"))
        for receipt in receipts.values()
    )
    readme = f"""# Technisches Fotoarchiv (TFA) des WF

This unit contains the accessioned museum-digital collection 857 scope. Museum-digital is the authoritative descriptive metadata source.

- Museum-digital records: {len(all_rows)}
- Accessioned archival objects: {len(receipts)}
- Selected Masters: {len(receipts)}
- High-resolution JPEG surrogate Masters: {jpeg_surrogates}
- Generated full-resolution JPEG access derivatives: {len(receipts)}
- Catalogue duplicate and non-critical accession holds: {len(holds)}
- Subdivision memberships: {len(memberships)}
- Redundant source paths in the drain list: {len(drain_rows)}
- Deletion authorized: false

`masters/` contains one metadata-embedded selected Master per accessioned object, preserving the selected source format. `export/access/` contains one full-resolution JPEG generated from that finalized Master at quality 95. Current museum-digital subdivision relationships remain catalogue facets, not filesystem folders. Source files remain unchanged and read-only. Final media hashes are in `db-restore/checksums.csv`; human-readable provenance and dispositions are under `db-restore/manifests/`.
"""
    atomic_bytes(UNIT / "00_README.md", readme.encode())
    db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    try:
        quick = db.execute("PRAGMA quick_check").fetchone()[0]
        counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in ("objects", "representations", "checksums", "object_subdivisions", "drain_list", "accession_holds")}
    finally:
        db.close()
    expected_representations = 2 * len(receipts)
    if quick != "ok" or counts != {
        "objects": EXPECTED_MD, "representations": expected_representations,
        "checksums": expected_representations, "object_subdivisions": EXPECTED_MEMBERSHIPS,
        "drain_list": EXPECTED_DRAIN, "accession_holds": len(holds),
    }:
        raise IntegrityError(f"final SQLite validation failed: quick={quick} counts={counts}")
    for receipt in receipts.values():
        for prefix in ("master", "access"):
            path = UNIT / receipt[f"{prefix}_relative_path"]
            stat = path.stat()
            if stat.st_size != receipt[f"{prefix}_size_bytes"] or stat.st_ino != receipt[f"{prefix}_inode"] or stat.st_nlink != 1:
                raise IntegrityError(f"final receipt binding changed: {path}")
    control_hashes = {str(path.relative_to(UNIT)): sha256(path) for path in controls}
    control_hashes["00_README.md"] = sha256(UNIT / "00_README.md")
    completion = {
        "format": "iss-tfa-accession-completion-v1", "completed_at": now(),
        "result": "ok", "museum_digital_records": EXPECTED_MD,
        "ready_scope": EXPECTED_READY, "objects_accessioned": len(receipts),
        "noncritical_error_holds": len(errors), "catalogue_duplicate_holds": 1,
        "representation_rows": expected_representations, "checksum_rows": len(checksums),
        "subdivision_memberships": len(memberships), "drain_rows": len(drain_rows),
        "metadata_authority": "museum-digital", "metadata_profile": PROFILE,
        "derivative_profile": DERIVATIVE_PROFILE,
        "final_hash_policy": "one SHA-256 per finalized Master and derivative; receipts reused during finalization",
        "control_file_sha256": control_hashes, "sqlite_quick_check": quick,
        "source_read_only": True, "source_modified": False,
        "data_integrity_suspected": False, "deletion_authorized": False,
        "write_boundary_state": "confined_to_iss_archive_work_and_iss_archive",
        "unit": str(UNIT), "run": str(RUN),
    }
    atomic_json(COMPLETION, completion)
    atomic_json(PUBLICATION, completion)
    atomic_json(UNIT / "db-restore/accession-completion.json", completion)
    atomic_json(
        UNIT / "metadata/accession-state.json",
        {"format": "iss-tfa-accession-state-v1", "updated_at": now(), "run": str(RUN), "state": "complete", "completion": str(COMPLETION)},
    )
    return completion


def run_pilot(inventories: list[str]) -> int:
    preflight()
    _, rows, _ = load_scope(); by_inventory = {row["inventory"]: row for row in rows}
    results = []
    for inventory in inventories:
        if inventory not in by_inventory or by_inventory[inventory]["accession_state"] != "ready_for_accession":
            raise IntegrityError(f"unknown or held pilot inventory: {inventory}")
        results.append(process_one(by_inventory[inventory], publish=False))
    total_bytes = sum(int(by_inventory[item]["selected_master_size_bytes"]) for item in inventories)
    elapsed = sum(float(item["elapsed_seconds"]) for item in results)
    estimate = elapsed / total_bytes * sum(
        int(row["selected_master_size_bytes"]) for row in rows if row["accession_state"] == "ready_for_accession"
    ) if total_bytes else 0
    payload = {
        "format": "iss-tfa-accession-pilot-v1", "completed_at": now(), "result": "ok",
        "inventories": inventories, "pilot_bytes": total_bytes,
        "pilot_elapsed_seconds": elapsed, "estimated_batch_seconds_by_bytes": estimate,
        "estimated_batch_hours_by_bytes": round(estimate / 3600, 2),
        "source_read_only": True, "published": False, "deletion_authorized": False,
    }
    atomic_json(RUN / "pilot-validation.json", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_all() -> int:
    preflight_result = preflight(allow_unit=True)
    RUN.mkdir(parents=True, exist_ok=True)
    atomic_json(RUN / "preflight.json", preflight_result)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IntegrityError("another TFA accession worker holds the run lock") from error
        publish_unit_dirs()
        fields, rows, _ = load_scope()
        ready = [row for row in rows if row["accession_state"] == "ready_for_accession"]
        receipts = load_receipts(); errors = load_errors(); started = time.monotonic()
        for index, row in enumerate(ready, 1):
            inventory = row["inventory"]
            if inventory in receipts or inventory in errors:
                continue
            try:
                receipt = process_one(row, publish=True)
                receipts[inventory] = receipt
            except IntegrityError as error:
                payload = {
                    "failed_at": now(), "state": "stopped_integrity", "index": index,
                    "inventory": inventory, "error": str(error), "data_integrity_suspected": True,
                }
                atomic_json(CRITICAL, payload); atomic_json(STATUS, payload)
                raise
            except (NonCriticalError, OSError) as error:
                if isinstance(error, OSError) and getattr(error, "errno", None) in {5, 30}:
                    payload = {"failed_at": now(), "state": "stopped_integrity", "index": index, "inventory": inventory, "error": str(error), "data_integrity_suspected": True}
                    atomic_json(CRITICAL, payload); atomic_json(STATUS, payload)
                    raise IntegrityError(str(error)) from error
                payload = {
                    "failed_at": now(), "index": index, "record_key": row["record_key"],
                    "inventory": inventory, "error": str(error), "critical": False,
                }
                append_jsonl(ERRORS, payload); errors[inventory] = payload
            elapsed = max(time.monotonic() - started, 0.001)
            processed = len(receipts) + len(errors)
            rate = processed / elapsed
            eta = (len(ready) - processed) / rate if rate else 0
            atomic_json(STATUS, {
                "updated_at": now(), "state": "running", "index": index,
                "total": len(ready), "completed": len(receipts), "errors": len(errors),
                "processed": processed, "last_inventory": inventory,
                "elapsed_seconds": round(elapsed, 1), "estimated_remaining_seconds": round(eta, 1),
            })
        atomic_json(STATUS, {"updated_at": now(), "state": "finalizing", "total": len(ready), "completed": len(receipts), "errors": len(errors)})
        completion = finalize(fields, rows)
        atomic_json(STATUS, {**completion, "state": "complete"})
        print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        os.close(descriptor)


def watchdog(worker_unit: str, interval: int) -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    append_jsonl(LOG, {"at": now(), "event": "armed", "worker_unit": worker_unit})
    while True:
        if CRITICAL.exists():
            append_jsonl(LOG, {"at": now(), "event": "refused_shutdown", "reason": "critical integrity record exists"})
            return 2
        state = subprocess.run(
            ["systemctl", "--user", "show", worker_unit, "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True,
        )
        values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if COMPLETION.exists():
            completion = json.loads(COMPLETION.read_text(encoding="utf-8"))
            gates = (
                completion.get("result") == "ok"
                and completion.get("data_integrity_suspected") is False
                and completion.get("deletion_authorized") is False
                and completion.get("source_read_only") is True
                and completion.get("write_boundary_state") == "confined_to_iss_archive_work_and_iss_archive"
            )
            if not gates:
                append_jsonl(LOG, {"at": now(), "event": "refused_shutdown", "reason": "completion gates failed"})
                return 2
            if values.get("ActiveState") in {"active", "activating"}:
                time.sleep(interval); continue
            if values.get("Result") not in {"success", ""} or values.get("ExecMainStatus") not in {"0", ""}:
                append_jsonl(LOG, {"at": now(), "event": "refused_shutdown", "reason": "worker exit failed", "unit": values})
                return 2
            db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise IntegrityError("watchdog SQLite check failed")
                if db.execute("SELECT COUNT(*) FROM checksums").fetchone()[0] != int(completion["checksum_rows"]):
                    raise IntegrityError("watchdog checksum count failed")
            finally:
                db.close()
            source_mount()
            scan = subprocess.run(["pgrep", "-f", "czkawka_cli.*restore/archive/WF"], capture_output=True)
            if scan.returncode == 0:
                append_jsonl(LOG, {"at": now(), "event": "waiting", "reason": "supplementary Czkawka scan still running"})
                time.sleep(interval); continue
            subprocess.run(["sync", "-f", str(UNIT)], check=True)
            subprocess.run(["sync", "-f", str(WORK)], check=True)
            append_jsonl(LOG, {"at": now(), "event": "shutdown_requested", "completion": str(COMPLETION)})
            result = subprocess.run(["sudo", "-n", "systemctl", "poweroff"], capture_output=True, text=True)
            if result.returncode:
                append_jsonl(LOG, {"at": now(), "event": "shutdown_failed", "returncode": result.returncode, "stderr": result.stderr.strip()})
            return result.returncode
        if values.get("ActiveState") not in {"active", "activating"}:
            append_jsonl(LOG, {"at": now(), "event": "refused_shutdown", "reason": "worker ended without valid completion", "unit": values})
            return 2
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    pilot = commands.add_parser("pilot"); pilot.add_argument("inventory", nargs="+")
    commands.add_parser("run")
    watch = commands.add_parser("watchdog"); watch.add_argument("--worker-unit", required=True); watch.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            print(json.dumps(preflight(), indent=2, sort_keys=True)); return 0
        if args.command == "pilot":
            return run_pilot(args.inventory)
        if args.command == "run":
            return run_all()
        return watchdog(args.worker_unit, args.interval)
    except AccessionError as error:
        print(f"error: {error}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
