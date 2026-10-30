#!/usr/bin/env python3
"""Reconcile Kurt Schwarz embedded metadata and full-resolution JPEG access files.

The worker is deliberately serial.  It creates and validates both candidates in
the writable work root, then atomically replaces the two published paths.  It
never edits a published media file directly and never touches protected source
media.  Final SHA-256 values are calculated once, after each candidate is final.
"""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
WORK = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
SCRIPT_DIR = WORK / "work/md-finalization"
RUN = SCRIPT_DIR / "metadata-derivatives-001"
CANDIDATES = RUN / "candidates"
PILOT = RUN / "pilot"
RECEIPTS = RUN / "receipts"
STATUS = RUN / "status.json"
ERRORS = RUN / "errors.jsonl"
CRITICAL = RUN / "critical.json"
COMPLETION = RUN / "completion.json"
LOCK = RUN / "worker.lock"
PROFILE = "iss-kurt-schwarz-metadata-v1"
SEP = "\x1f"
EXPECTED_MASTERS = 1665
EXPECTED_ACCESS = 1665
EXPECTED_REPRESENTATIONS = 3330
TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
sys.path.insert(0, str(TOOL_ROOT))


class WorkerError(RuntimeError):
    pass


class NonCriticalError(WorkerError):
    """The current source stays untouched and processing may continue."""


class IntegrityError(WorkerError):
    """Continuing could conceal corruption or an unsafe state."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_roots() -> None:
    for path in (UNIT, WORK, SCRIPT_DIR):
        if not path.is_dir():
            raise IntegrityError(f"required configured root is absent: {path}")
    unit_real = UNIT.resolve(strict=True)
    work_real = WORK.resolve(strict=True)
    if unit_real == work_real or unit_real.is_relative_to(work_real) or work_real.is_relative_to(unit_real):
        raise IntegrityError("archive and work roots are not independent directories")
    if UNIT.stat().st_dev != WORK.stat().st_dev:
        raise IntegrityError("atomic media replacement requires archive and work on one filesystem")


def fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_tool(arguments: list[str], *, binary: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=not binary,
            env=env,
        )
    except FileNotFoundError as error:
        raise IntegrityError(f"required tool unavailable: {arguments[0]}") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode("utf-8", "replace")
        stdout = error.stdout if isinstance(error.stdout, str) else (error.stdout or b"").decode("utf-8", "replace")
        raise NonCriticalError(f"{arguments[0]} failed: {(stderr or stdout).strip() or error.returncode}") from error


def sha256_once(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    fields = list(fields)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def safe_media_path(relative: str, expected_parent: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2 or rel.parts[0] != expected_parent:
        raise IntegrityError(f"unsafe media path in catalog: {relative}")
    result = UNIT / rel
    if not result.is_file() or result.resolve().parent != (UNIT / expected_parent).resolve():
        raise IntegrityError(f"catalog media path is absent or escaped: {relative}")
    return result


def load_master_rows() -> list[dict[str, str]]:
    fields, rows = read_csv(UNIT / "CATALOG.csv")
    required = {
        "record_key", "inventory", "file_role", "final_relative_path", "title",
        "description", "date_text", "material", "dimensions", "places", "tags",
        "creator", "md_object_id", "md_url", "original_source_path",
    }
    if not required.issubset(fields):
        raise IntegrityError(f"CATALOG.csv lacks required fields: {sorted(required - set(fields))}")
    masters = [row for row in rows if row["file_role"] == "preservation_master"]
    access = [row for row in rows if row["file_role"] == "access_derivative"]
    if len(masters) != EXPECTED_MASTERS or len(access) != EXPECTED_ACCESS or len(rows) != EXPECTED_REPRESENTATIONS:
        raise IntegrityError(f"catalog scope drifted: masters={len(masters)} access={len(access)} rows={len(rows)}")
    if len({row["record_key"] for row in masters}) != EXPECTED_MASTERS:
        raise IntegrityError("catalog no longer has exactly one Master per object")
    for row in masters:
        safe_media_path(row["final_relative_path"], "masters")
    return sorted(masters, key=lambda row: row["final_relative_path"])


def documented_gap_records() -> set[str]:
    _, rows = read_csv(UNIT / "db-restore/manifests/access-derivative-gaps.csv")
    records = {row["record_key"] for row in rows}
    if len(records) != EXPECTED_MASTERS - EXPECTED_ACCESS:
        raise IntegrityError(f"documented derivative-gap scope drifted: {len(records)}")
    return records


def exif_json(path: Path, tags: Iterable[str], *, numeric: bool = False) -> dict[str, Any]:
    arguments = ["exiftool", "-j", "-G1", "-s", "-sep", SEP]
    if numeric:
        arguments.append("-n")
    arguments.extend(f"-{tag}" for tag in tags)
    arguments.append(str(path))
    result = run_tool(arguments)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise NonCriticalError(f"ExifTool returned invalid JSON for {path.name}") from error
    if len(payload) != 1:
        raise NonCriticalError(f"ExifTool returned no single record for {path.name}")
    payload[0].pop("SourceFile", None)
    return payload[0]


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


def split_terms(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def metadata_plan(row: dict[str, str], master_hash: str | None = None) -> dict[str, str | None]:
    physical = "; ".join(
        part for part in (
            f"Material/Technik: {row['material'].strip()}" if row["material"].strip() else "",
            f"Maße: {row['dimensions'].strip()}" if row["dimensions"].strip() else "",
        ) if part
    )
    relations = [f"iss:metadata-profile:{PROFILE}"]
    if row["md_object_id"].strip():
        relations.append(f"museum-digital:object:{row['md_object_id'].strip()}")
    if row["md_url"].strip():
        relations.append(row["md_url"].strip())
    if master_hash is not None:
        relations.extend((f"master:{row['final_relative_path']}", f"master-sha256:{master_hash.removeprefix('sha256:')}"))
    title = row["title"].strip()
    description = row["description"].strip()
    creator = row["creator"].strip()
    subjects = SEP.join(split_terms(row["tags"]))
    date_text = row["date_text"].strip()
    strict_date: str | None = None
    if re.fullmatch(r"\d{4}", date_text):
        strict_date = date_text
    elif match := re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", date_text):
        strict_date = f"{match.group(3)}:{match.group(2)}:{match.group(1)}"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        strict_date = date_text.replace("-", ":")
    plan: dict[str, str | None] = {
        "XMP-dc:Identifier": row["record_key"].strip(),
        "XMP-iptcExt:ArtworkSourceInventoryNo": row["inventory"].strip(),
        "XMP-dc:Title": title or None,
        "XMP-iptcExt:ArtworkTitle": title or None,
        "XMP-dc:Description": description or None,
        "XMP-iptcExt:ArtworkContentDescription": description or None,
        "IFD0:ImageDescription": description or None,
        "XMP-dc:Date": strict_date,
        "XMP-iptcExt:ArtworkCircaDateCreated": date_text or None,
        "XMP-dc:Type": row["material"].strip() or None,
        "XMP-iptcExt:ArtworkPhysicalDescription": physical or None,
        "XMP-dc:Coverage": row["places"].strip() or None,
        "XMP-dc:Subject": subjects or None,
        "XMP-dc:Creator": creator or None,
        "XMP-dc:Source": row["original_source_path"].strip() or None,
        "XMP-dc:Relation": SEP.join(relations),
        "IPTC:ObjectName": title if title and len(title.encode("utf-8")) <= 64 else None,
        "IPTC:Caption-Abstract": description if description and len(description.encode("utf-8")) <= 2000 else None,
        "IPTC:Keywords": subjects or None,
        "IPTC:By-line": creator if creator and len(creator.encode("utf-8")) <= 32 else None,
    }
    if any(plan[key] is not None for key in ("IPTC:ObjectName", "IPTC:Caption-Abstract", "IPTC:Keywords", "IPTC:By-line")):
        plan["IPTC:CodedCharacterSet"] = "UTF8"
    else:
        plan["IPTC:CodedCharacterSet"] = None
    return plan


def copy_with_metadata(source: Path, destination: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    if destination.exists():
        raise NonCriticalError(f"preserved candidate already exists: {destination}")
    if source.name == "KS-8-WF_0101.jpg":
        shutil.copyfile(source, destination)
        fsync_file(destination)
        result = run_tool(["exiftool", "-overwrite_original", "-XMP:All=", str(destination)])
        warnings = [line for line in result.stderr.splitlines() if "Warning:" in line]
        serious = [line for line in warnings if "Ignored non-standard extended XMP" not in line]
        if serious:
            raise NonCriticalError(f"ExifTool warning while clearing malformed XMP in {destination.name}: {result.stderr.strip()}")
        write_metadata_in_place(destination, plan, rights)
        return
    arguments = ["exiftool", "-charset", "IPTC=UTF8", "-sep", SEP]
    merged: dict[str, str | None] = dict(plan)
    for tag in RIGHTS_TAGS:
        value = rights.get(tag)
        if isinstance(value, list):
            value = SEP.join(str(part) for part in value)
        if value not in (None, ""):
            merged[tag] = str(value)
    for tag, value in merged.items():
        arguments.append(f"-{tag}={value or ''}")
    arguments.extend(("-o", str(destination), str(source)))
    result = run_tool(arguments)
    warnings = [line for line in result.stderr.splitlines() if "Warning:" in line]
    serious = [line for line in warnings if "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise NonCriticalError(f"ExifTool warning while creating {destination.name}: {result.stderr.strip()}")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise NonCriticalError(f"ExifTool did not create a valid candidate: {destination}")
    fsync_file(destination)


def write_metadata_in_place(path: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    arguments = ["exiftool", "-overwrite_original", "-charset", "IPTC=UTF8", "-sep", SEP]
    merged: dict[str, str | None] = dict(plan)
    for tag in RIGHTS_TAGS:
        value = rights.get(tag)
        if isinstance(value, list):
            value = SEP.join(str(part) for part in value)
        if value not in (None, ""):
            merged[tag] = str(value)
    for tag, value in merged.items():
        arguments.append(f"-{tag}={value or ''}")
    arguments.append(str(path))
    result = run_tool(arguments)
    warnings = [line for line in result.stderr.splitlines() if "Warning:" in line]
    serious = [line for line in warnings if "[minor] Ignored empty rdf:Seq list" not in line]
    if serious:
        raise NonCriticalError(f"ExifTool warning while writing {path.name}: {result.stderr.strip()}")
    fsync_file(path)


def normalize_value(value: Any) -> str:
    if isinstance(value, list):
        return SEP.join(str(part) for part in value)
    return "" if value is None else str(value)


def verify_metadata(path: Path, plan: dict[str, str | None], rights: dict[str, Any]) -> None:
    tags = list(plan) + list(RIGHTS_TAGS)
    observed = exif_json(path, tags)
    for tag, expected in plan.items():
        actual = normalize_value(observed.get(tag))
        if expected is None:
            if actual:
                raise NonCriticalError(f"metadata clear failed for {tag} in {path.name}: {actual!r}")
        elif actual != expected:
            raise NonCriticalError(f"metadata readback mismatch for {tag} in {path.name}: {actual!r} != {expected!r}")
    for tag in RIGHTS_TAGS:
        if tag in rights and normalize_value(observed.get(tag)) != normalize_value(rights[tag]):
            raise NonCriticalError(f"preserved rights readback mismatch for {tag} in {path.name}")


def binary_tag_hash(path: Path, tag: str) -> str:
    try:
        process = subprocess.Popen(["exiftool", "-b", f"-{tag}", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise IntegrityError("required tool unavailable: exiftool") from error
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    size = 0
    while block := process.stdout.read(8 * 1024 * 1024):
        size += len(block)
        digest.update(block)
    stderr = process.stderr.read().decode("utf-8", "replace").strip()
    code = process.wait()
    if code:
        raise NonCriticalError(f"ExifTool could not read {tag} from {path.name}: {stderr or code}")
    return f"{size}:sha256:{digest.hexdigest()}"


def decoded_pixel_digest(path: Path, depth: int) -> str:
    if path.name in {"KS-7-BZ_0590-2.tif", "KS-7-BZ_0590-2.master.tif"}:
        try:
            process = subprocess.Popen(
                ["vips", "rawsave", str(path), "/dev/stdout"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise IntegrityError("required tool unavailable: vips") from error
        assert process.stdout is not None and process.stderr is not None
        digest = hashlib.sha256()
        while block := process.stdout.read(8 * 1024 * 1024):
            digest.update(block)
        stderr = process.stderr.read().decode("utf-8", "replace").strip()
        code = process.wait()
        if code:
            raise NonCriticalError(f"libvips could not decode {path.name}: {stderr or code}")
        return "sha256:" + digest.hexdigest()
    try:
        process = subprocess.Popen(
            ["magick", str(path), "-depth", str(depth), "RGBA:-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise IntegrityError("required tool unavailable: magick") from error
    assert process.stdout is not None and process.stderr is not None
    digest = hashlib.sha256()
    while block := process.stdout.read(8 * 1024 * 1024):
        digest.update(block)
    stderr = process.stderr.read().decode("utf-8", "replace").strip()
    code = process.wait()
    if code:
        raise NonCriticalError(f"source image does not decode safely: {path.name}: {stderr or code}")
    return "sha256:" + digest.hexdigest()


def scalar_depth(technical: dict[str, Any]) -> int:
    value = technical.get("IFD0:BitsPerSample", 8)
    if isinstance(value, list):
        parts = [int(part) for part in value]
    else:
        try:
            parts = [int(part) for part in str(value).replace(",", " ").split()]
        except ValueError as error:
            raise NonCriticalError(f"unsupported BitsPerSample: {value!r}") from error
    if not parts:
        return 8
    if len(set(parts)) != 1 or parts[0] not in (1, 2, 4, 8, 16):
        raise NonCriticalError(f"unsupported BitsPerSample: {value!r}")
    return parts[0]


def image_dimensions(path: Path) -> tuple[int, int, int]:
    observed = exif_json(path, ("ImageWidth", "ImageHeight", "Orientation"), numeric=True)
    try:
        width = int(observed["File:ImageWidth"] if "File:ImageWidth" in observed else observed["EXIF:ImageWidth"])
        height = int(observed["File:ImageHeight"] if "File:ImageHeight" in observed else observed["EXIF:ImageHeight"])
    except (KeyError, TypeError, ValueError) as error:
        values = list(observed.items())
        width_values = [int(v) for k, v in values if k.endswith(":ImageWidth")]
        height_values = [int(v) for k, v in values if k.endswith(":ImageHeight")]
        if not width_values or not height_values:
            raise NonCriticalError(f"cannot determine image dimensions for {path.name}") from error
        width, height = width_values[0], height_values[0]
    orientation_values = [v for k, v in observed.items() if k.endswith(":Orientation")]
    orientation = int(orientation_values[0]) if orientation_values else 1
    return width, height, orientation


def verify_structure(source: Path, candidate: Path) -> tuple[dict[str, Any], int, str]:
    source_technical = exif_json(source, TECHNICAL_TAGS, numeric=True)
    candidate_technical = exif_json(candidate, TECHNICAL_TAGS, numeric=True)
    if source_technical != candidate_technical:
        raise IntegrityError(f"technical image structure changed for {source.name}")
    depth = scalar_depth(source_technical)
    layer_values = [v for k, v in source_technical.items() if k.endswith(":LayerCount")]
    if layer_values:
        source_block = binary_tag_hash(source, "PhotoshopSettings")
        candidate_block = binary_tag_hash(candidate, "PhotoshopSettings")
        if source_block != candidate_block:
            raise IntegrityError(f"Photoshop layer block changed for {source.name}")
    source_icc = binary_tag_hash(source, "ICC_Profile")
    candidate_icc = binary_tag_hash(candidate, "ICC_Profile")
    if source_icc != candidate_icc:
        raise IntegrityError(f"ICC profile changed for {source.name}")
    source_pixels = decoded_pixel_digest(source, depth)
    candidate_pixels = decoded_pixel_digest(candidate, depth)
    if source_pixels != candidate_pixels:
        raise IntegrityError(f"decoded pixels changed for {source.name}")
    return source_technical, depth, source_pixels


def generate_access(master: Path, destination: Path) -> None:
    if destination.exists():
        raise NonCriticalError(f"preserved candidate already exists: {destination}")
    env = dict(os.environ)
    env["VIPS_CONCURRENCY"] = "5"
    run_tool([
        "vips", "autorot", str(master),
        f"{destination}[Q=95,optimize-coding=true,keep=icc]",
    ], env=env)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise NonCriticalError(f"libvips did not create access candidate: {destination}")
    fsync_file(destination)


def process_one(row: dict[str, str], *, publish: bool, output_root: Path) -> dict[str, Any]:
    inventory = row["inventory"].strip()
    if not inventory or Path(inventory).name != inventory or "/" in inventory or "\\" in inventory:
        raise IntegrityError(f"unsafe inventory: {inventory!r}")
    master = safe_media_path(row["final_relative_path"], "masters")
    master_stat = master.stat()
    if master_stat.st_nlink != 1:
        raise IntegrityError(f"Master is hard-linked; refusing mutation: {master}")
    usage = shutil.disk_usage(WORK)
    if usage.free < max(20 * 1024**3, master_stat.st_size * 3):
        raise IntegrityError(f"insufficient safe free space before {inventory}: {usage.free} bytes")
    suffix = master.suffix.lower()
    if suffix not in {".tif", ".tiff", ".jpg", ".jpeg"}:
        raise NonCriticalError(f"unsupported Master format: {suffix}")
    output_root.mkdir(parents=True, exist_ok=True)
    master_candidate = output_root / f"{inventory}.master{suffix}"
    access_candidate = output_root / f"{inventory}.access.jpg"
    rights = exif_json(master, RIGHTS_TAGS)
    plan_master = metadata_plan(row)
    before = (master_stat.st_dev, master_stat.st_ino, master_stat.st_size, master_stat.st_mtime_ns, master_stat.st_nlink)
    copy_with_metadata(master, master_candidate, plan_master, rights)
    verify_metadata(master_candidate, plan_master, rights)
    technical, depth, pixel_digest = verify_structure(master, master_candidate)
    after_stat = master.stat()
    after = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_nlink)
    if after != before:
        raise IntegrityError(f"published Master changed during candidate preparation: {master}")
    master_hash = sha256_once(master_candidate)
    plan_access = metadata_plan(row, master_hash)
    generate_access(master_candidate, access_candidate)
    write_metadata_in_place(access_candidate, plan_access, rights)
    verify_metadata(access_candidate, plan_access, rights)
    source_width, source_height, orientation = image_dimensions(master_candidate)
    access_width, access_height, _ = image_dimensions(access_candidate)
    expected_dimensions = (source_height, source_width) if orientation in {5, 6, 7, 8} else (source_width, source_height)
    if (access_width, access_height) != expected_dimensions:
        raise IntegrityError(
            f"access dimensions changed for {inventory}: {(access_width, access_height)} != {expected_dimensions}"
        )
    file_type = exif_json(access_candidate, ("File:FileType",)).get("File:FileType")
    if file_type != "JPEG":
        raise IntegrityError(f"access candidate is not JPEG: {inventory}")
    access_hash = sha256_once(access_candidate)
    receipt: dict[str, Any] = {
        "format": "iss-kurt-schwarz-metadata-derivative-receipt-v1",
        "completed_at": now(),
        "record_key": row["record_key"],
        "inventory": inventory,
        "master_relative_path": row["final_relative_path"],
        "master_sha256": master_hash,
        "master_size_bytes": master_candidate.stat().st_size,
        "master_pixel_digest": pixel_digest,
        "master_depth": depth,
        "master_technical": technical,
        "access_relative_path": f"export/access/{inventory}.jpg",
        "access_sha256": access_hash,
        "access_size_bytes": access_candidate.stat().st_size,
        "access_width": access_width,
        "access_height": access_height,
        "metadata_profile": PROFILE,
        "master_metadata": plan_master,
        "access_metadata": plan_access,
        "rights_preserved": rights,
        "published": publish,
    }
    if not publish:
        atomic_json(output_root / f"{inventory}.pilot.json", receipt)
        return receipt
    access = UNIT / receipt["access_relative_path"]
    if access.exists() and access.stat().st_nlink != 1:
        raise IntegrityError(f"access file is hard-linked; refusing mutation: {access}")
    os.replace(master_candidate, master)
    fsync_dir(master.parent)
    published_master = master.stat()
    if published_master.st_size != receipt["master_size_bytes"] or published_master.st_nlink != 1:
        raise IntegrityError(f"published Master verification failed after replacement: {master}")
    receipt["master_inode"] = published_master.st_ino
    receipt["master_mtime_ns"] = published_master.st_mtime_ns
    try:
        os.replace(access_candidate, access)
        fsync_dir(access.parent)
    except OSError as error:
        raise IntegrityError(f"access replacement failed after Master publication: {access}: {error}") from error
    published_access = access.stat()
    if published_access.st_size != receipt["access_size_bytes"] or published_access.st_nlink != 1:
        raise IntegrityError(f"published access verification failed after replacement: {access}")
    receipt["access_inode"] = published_access.st_ino
    receipt["access_mtime_ns"] = published_access.st_mtime_ns
    atomic_json(RECEIPTS / f"{inventory}.json", receipt)
    return receipt


def load_receipts() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not RECEIPTS.exists():
        return result
    for path in sorted(RECEIPTS.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IntegrityError(f"invalid receipt {path}: {error}") from error
        inventory = str(receipt.get("inventory") or "")
        if not inventory or inventory in result or not receipt.get("published"):
            raise IntegrityError(f"invalid or duplicate receipt: {path}")
        result[inventory] = receipt
    return result


def atomic_replace_bytes(path: Path, payload: bytes) -> None:
    temporary = RUN / f"finalize-{path.name}.tmp-{os.getpid()}"
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_dir(path.parent)


def table_rows(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[dict[str, str]]]:
    fields = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    rows = [dict(zip(fields, map(str, row))) for row in connection.execute(f'SELECT * FROM "{table}"')]
    return fields, rows


def create_table(connection: sqlite3.Connection, name: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    connection.execute(f'CREATE TABLE "{name}" (' + ",".join(f'"{field}" TEXT NOT NULL' for field in fields) + ")")
    if rows:
        connection.executemany(
            f'INSERT INTO "{name}" VALUES (' + ",".join("?" for _ in fields) + ")",
            [[str(row.get(field, "")) for field in fields] for row in rows],
        )


def finalize(master_rows: list[dict[str, str]], errors: int) -> dict[str, Any]:
    receipts = load_receipts()
    receipt_by_record = {receipt["record_key"]: receipt for receipt in receipts.values()}
    catalog_fields, catalog = read_csv(UNIT / "CATALOG.csv")
    rep_fields, representations = read_csv(UNIT / "db-restore/manifests/representations.csv")
    checksum_fields, checksums = read_csv(UNIT / "db-restore/checksums.csv")
    source_fields, source_rows = read_csv(UNIT / "db-restore/manifests/source-disposition.csv")
    drain_fields, drain_rows = read_csv(UNIT / "db-restore/manifests/drain-list.csv")
    old_db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    old_db.row_factory = sqlite3.Row
    try:
        table_names = [row[0] for row in old_db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {name: table_rows(old_db, name) for name in table_names}
    finally:
        old_db.close()
    by_path: dict[str, dict[str, Any]] = {}
    for receipt in receipts.values():
        by_path[receipt["master_relative_path"]] = {
            "sha256": receipt["master_sha256"], "size": str(receipt["master_size_bytes"]),
            "mtime_ns": str(receipt["master_mtime_ns"]), "role": "preservation_master",
            "record_key": receipt["record_key"],
        }
        by_path[receipt["access_relative_path"]] = {
            "sha256": receipt["access_sha256"], "size": str(receipt["access_size_bytes"]),
            "mtime_ns": str(receipt["access_mtime_ns"]), "role": "access_derivative",
            "record_key": receipt["record_key"], "master_sha256": receipt["master_sha256"],
        }
    for row in catalog:
        update = by_path.get(row["final_relative_path"])
        if update:
            row["final_sha256"] = update["sha256"]
            row["size_bytes"] = update["size"]
            row["access_derivative_status"] = "generated_full_resolution_jpeg_q95"
    for row in representations:
        update = by_path.get(row["final_relative_path"])
        if not update:
            if row["role"] == "preservation_master" and row.get("copy_policy") == "same_filesystem_hardlink_of_validated_archive_bytes":
                row["copy_policy"] = "verified_independent_copy_metadata_deferred"
            continue
        row["completed_at"] = now()
        row["representation_key"] = update["sha256"]
        row["final_sha256"] = update["sha256"]
        row["size_bytes"] = update["size"]
        row["mtime_ns"] = update["mtime_ns"]
        if update["role"] == "preservation_master":
            row["copy_policy"] = "verified_metadata_rewrite_atomic_replace"
            row["basis"] = "catalog_metadata_reconciled_pixels_and_structure_unchanged"
        else:
            row["source_sha256"] = update["master_sha256"]
            row["copy_policy"] = "generated_full_resolution_jpeg_q95"
            row["basis"] = "libvips_full_native_dimensions_metadata_embedded"
            row["relationship"] = f"access_derivative_of:{row['source_member']}"
    for row in checksums:
        update = by_path.get(row["path"])
        if update:
            row["sha256"] = update["sha256"]
            row["size_bytes"] = update["size"]
    for rows in (source_rows, drain_rows):
        for row in rows:
            update = by_path.get(row.get("final_path", ""))
            if update and update["role"] == "preservation_master":
                row["final_sha256"] = update["sha256"]
                row["exact_copy_proven"] = "false"
                if row.get("source_role") == "selected_master":
                    row["preservation_status"] = "preserved_metadata_enriched_master"
                    row["notes"] = "Selected source retained unchanged; published Master has reconciled embedded metadata. No deletion is authorized."
            elif row.get("source_role") == "selected_master" and row.get("preservation_status") == "preserved_exact_archive_hardlink":
                row["preservation_status"] = "preserved_independent_master_metadata_deferred"
                row["notes"] = "Selected source retained unchanged; published Master is an independent copy, with metadata rewrite deferred because safe decoding failed. No deletion is authorized."
    if "representations" in tables:
        tables["representations"] = (rep_fields, representations)
    if "checksums" in tables:
        tables["checksums"] = (checksum_fields, checksums)
    if "source_disposition" in tables:
        tables["source_disposition"] = (source_fields, source_rows)
    if "drain_list" in tables:
        tables["drain_list"] = (drain_fields, drain_rows)
    if "objects" in tables:
        object_fields, objects = tables["objects"]
        for row in objects:
            receipt = receipt_by_record.get(row["record_key"])
            if receipt:
                row["master_sha256"] = receipt["master_sha256"]
                row["access_relative_path"] = receipt["access_relative_path"]
                row["access_derivative_status"] = "generated_full_resolution_jpeg_q95"
        tables["objects"] = (object_fields, objects)
    if "access_derivative_gaps" in tables:
        gap_fields, gaps = tables["access_derivative_gaps"]
        for row in gaps:
            receipt = receipt_by_record.get(row["record_key"])
            if receipt:
                raise IntegrityError(f"a documented decode gap was unexpectedly published: {row['record_key']}")
        tables["access_derivative_gaps"] = (gap_fields, gaps)
    build_db = RUN / f"RESTORE.building-{os.getpid()}.sqlite"
    connection = sqlite3.connect(build_db)
    try:
        for name in table_names:
            fields, rows = tables[name]
            create_table(connection, name, fields, rows)
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IntegrityError("rebuilt RESTORE.sqlite integrity check failed")
        sql = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()
    sys.path.insert(0, str(TOOL_ROOT))
    import paragon_document_bundle
    catalog_xlsx = paragon_document_bundle.table_xlsx_bytes(catalog_fields, catalog, sheet_name="Catalog", table_name="Catalog")
    restore_xlsx = paragon_document_bundle.table_xlsx_bytes(catalog_fields, catalog, sheet_name="Restore", table_name="Restore")
    updates = {
        UNIT / "CATALOG.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "CATALOG.xlsx": catalog_xlsx,
        UNIT / "db-restore/RESTORE.csv": csv_bytes(catalog_fields, catalog),
        UNIT / "db-restore/RESTORE.xlsx": restore_xlsx,
        UNIT / "db-restore/checksums.csv": csv_bytes(checksum_fields, sorted(checksums, key=lambda row: row["path"])),
        UNIT / "db-restore/manifests/representations.csv": csv_bytes(rep_fields, representations),
        UNIT / "db-restore/manifests/source-disposition.csv": csv_bytes(source_fields, source_rows),
        UNIT / "db-restore/manifests/drain-list.csv": csv_bytes(drain_fields, drain_rows),
        UNIT / "db-restore/RESTORE.sql": sql.encode("utf-8"),
    }
    readme_path = UNIT / "00_README.md"
    readme = readme_path.read_text(encoding="utf-8")
    marker = "## Embedded metadata and full-resolution access derivatives"
    if marker not in readme:
        readme += (
            f"\n{marker}\n\n"
            "Published Masters carry the reconciled catalog metadata while preserving their original image encoding, decoded pixels, ICC profile, and Photoshop layer data where present; the documented decode-gap Masters remain unchanged with metadata correction deferred. "
            "`export/access/` JPEGs are generated at the Master's native displayed dimensions with JPEG quality 95, carry the same descriptive metadata, and record the relative Master path and finalized Master SHA-256. "
            "Final checksums are recorded in `db-restore/checksums.csv`; protected source files and the predecessor unit remain unchanged.\n"
        )
        updates[readme_path] = readme.encode("utf-8")
    for path, payload in updates.items():
        atomic_replace_bytes(path, payload)
    os.replace(build_db, UNIT / "db-restore/RESTORE.sqlite")
    fsync_dir(UNIT / "db-restore")
    db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise IntegrityError("published RESTORE.sqlite quick_check failed")
        for table, expected in (("objects", EXPECTED_MASTERS), ("representations", EXPECTED_REPRESENTATIONS), ("checksums", EXPECTED_REPRESENTATIONS)):
            actual = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if actual != expected:
                raise IntegrityError(f"published {table} count {actual} != {expected}")
        for table in ("source_disposition", "drain_list", "reconciliation_decisions"):
            if db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE deletion_authorized != "false"').fetchone()[0]:
                raise IntegrityError(f"deletion authorization detected in {table}")
    finally:
        db.close()
    for receipt in receipts.values():
        for path_key, size_key, inode_key in (
            ("master_relative_path", "master_size_bytes", "master_inode"),
            ("access_relative_path", "access_size_bytes", "access_inode"),
        ):
            path = UNIT / receipt[path_key]
            stat = path.stat()
            if stat.st_size != receipt[size_key] or stat.st_ino != receipt[inode_key] or stat.st_nlink != 1:
                raise IntegrityError(f"published receipt binding drifted: {path}")
    completion = {
        "format": "iss-kurt-schwarz-metadata-derivatives-completion-v1",
        "completed_at": now(),
        "result": "ok",
        "metadata_profile": PROFILE,
        "objects_total": len(master_rows),
        "objects_updated": len(receipts),
        "noncritical_errors": errors,
        "data_integrity_suspected": False,
        "normalization_deferred": errors > 0,
        "final_hash_policy": "one SHA-256 per finalized Master and derivative candidate; catalog rebuild reused receipts",
        "deletion_authorized": False,
        "source_or_predecessor_mutated": False,
        "sqlite_quick_check": "ok",
    }
    atomic_json(COMPLETION, completion)
    return completion


def run_pilot(inventories: list[str]) -> int:
    ensure_roots()
    rows = load_master_rows()
    by_inventory = {row["inventory"]: row for row in rows}
    results = []
    for inventory in inventories:
        row = by_inventory.get(inventory)
        if row is None:
            raise SystemExit(f"unknown pilot inventory: {inventory}")
        try:
            results.append(process_one(row, publish=False, output_root=PILOT))
        except WorkerError as error:
            atomic_json(RUN / "pilot-failure.json", {"failed_at": now(), "inventory": inventory, "error": str(error), "integrity": isinstance(error, IntegrityError)})
            raise
    atomic_json(RUN / "pilot-validation.json", {
        "validated_at": now(), "result": "ok", "published": False,
        "inventories": inventories, "count": len(results), "metadata_profile": PROFILE,
    })
    print(json.dumps({"result": "ok", "pilot": inventories}, indent=2))
    return 0


def run_all() -> int:
    ensure_roots()
    RUN.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IntegrityError("another metadata worker holds the run lock") from error
        rows = load_master_rows()
        gap_records = documented_gap_records()
        receipts = load_receipts()
        errors = sum(1 for _ in ERRORS.open(encoding="utf-8")) if ERRORS.exists() else 0
        atomic_json(STATUS, {"started_at": now(), "state": "running", "total": len(rows), "completed": len(receipts), "errors": errors})
        for index, row in enumerate(rows, 1):
            inventory = row["inventory"]
            if inventory in receipts:
                continue
            if row["record_key"] in gap_records:
                errors += 1
                append_jsonl(ERRORS, {"failed_at": now(), "index": index, "record_key": row["record_key"], "inventory": inventory, "error": "documented Master decode gap; metadata rewrite and new derivative safely deferred", "critical": False})
                atomic_json(STATUS, {"updated_at": now(), "state": "running", "index": index, "total": len(rows), "completed": len(receipts), "errors": errors, "last_inventory": inventory})
                continue
            try:
                receipt = process_one(row, publish=True, output_root=CANDIDATES)
                receipts[inventory] = receipt
            except IntegrityError as error:
                payload = {"failed_at": now(), "state": "stopped_integrity", "index": index, "inventory": inventory, "error": str(error), "data_integrity_suspected": True}
                atomic_json(CRITICAL, payload)
                atomic_json(STATUS, {**payload, "total": len(rows), "completed": len(receipts), "errors": errors})
                raise
            except NonCriticalError as error:
                errors += 1
                append_jsonl(ERRORS, {"failed_at": now(), "index": index, "record_key": row["record_key"], "inventory": inventory, "error": str(error), "critical": False})
            atomic_json(STATUS, {"updated_at": now(), "state": "running", "index": index, "total": len(rows), "completed": len(receipts), "errors": errors, "last_inventory": inventory})
        atomic_json(STATUS, {"updated_at": now(), "state": "finalizing", "total": len(rows), "completed": len(receipts), "errors": errors})
        completion = finalize(rows, errors)
        atomic_json(STATUS, {**completion, "state": "complete"})
        print(json.dumps(completion, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    finally:
        os.close(descriptor)


def watchdog(worker_unit: str, interval: int) -> int:
    ensure_roots()
    log = RUN / "watchdog.jsonl"
    append_jsonl(log, {"at": now(), "event": "armed", "worker_unit": worker_unit})
    while True:
        if CRITICAL.exists():
            append_jsonl(log, {"at": now(), "event": "refused_shutdown", "reason": "critical integrity stop present"})
            return 2
        if COMPLETION.exists():
            try:
                completion = json.loads(COMPLETION.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                append_jsonl(log, {"at": now(), "event": "refused_shutdown", "reason": f"invalid completion: {error}"})
                return 2
            if completion.get("result") != "ok" or completion.get("data_integrity_suspected") is not False or completion.get("deletion_authorized") is not False:
                append_jsonl(log, {"at": now(), "event": "refused_shutdown", "reason": "completion gates failed"})
                return 2
            state = subprocess.run(["systemctl", "show", worker_unit, "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"], capture_output=True, text=True)
            values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
            if values.get("ActiveState") in {"active", "activating"}:
                time.sleep(interval)
                continue
            if values.get("Result") not in {"success", ""} or values.get("ExecMainStatus") not in {"0", ""}:
                append_jsonl(log, {"at": now(), "event": "refused_shutdown", "reason": "worker did not exit successfully", "unit": values})
                return 2
            db = sqlite3.connect(f"file:{UNIT / 'db-restore/RESTORE.sqlite'}?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise IntegrityError("watchdog SQLite quick_check failed")
                if db.execute('SELECT COUNT(*) FROM checksums').fetchone()[0] != EXPECTED_REPRESENTATIONS:
                    raise IntegrityError("watchdog checksum count failed")
            finally:
                db.close()
            subprocess.run(["sync"], check=True)
            append_jsonl(log, {"at": now(), "event": "shutdown_requested", "completion": str(COMPLETION)})
            result = subprocess.run(["sudo", "-n", "systemctl", "poweroff"], capture_output=True, text=True)
            if result.returncode:
                append_jsonl(log, {"at": now(), "event": "shutdown_failed", "returncode": result.returncode, "stderr": result.stderr.strip()})
                return result.returncode
            return 0
        state = subprocess.run(["systemctl", "show", worker_unit, "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"], capture_output=True, text=True)
        values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if values.get("ActiveState") not in {"active", "activating"}:
            append_jsonl(log, {"at": now(), "event": "refused_shutdown", "reason": "worker ended without valid completion", "unit": values})
            return 2
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pilot = sub.add_parser("pilot")
    pilot.add_argument("inventory", nargs="+")
    sub.add_parser("run")
    watch = sub.add_parser("watchdog")
    watch.add_argument("--worker-unit", required=True)
    watch.add_argument("--interval", type=int, default=30)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "pilot":
            return run_pilot(arguments.inventory)
        if arguments.command == "run":
            return run_all()
        return watchdog(arguments.worker_unit, arguments.interval)
    except WorkerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
