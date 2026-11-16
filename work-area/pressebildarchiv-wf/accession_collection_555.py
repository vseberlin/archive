#!/usr/bin/env python3
"""Accession the reconciled museum-digital collection 555 unit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


WORK_ROOT = Path("/mnt/volume/iss-archive-work/pressebildarchiv-wf")
INIT = WORK_ROOT / "work/accession/collection-555-init-20260829"
CAPTURE = WORK_ROOT / "work/reconciliation/museum-digital/runs/20260803T085022Z-pressebildarchiv-wf-collection-555"
MD_REFERENCE = WORK_ROOT / "work/usb-master-search/runs/20260803T094421Z-pressebildarchiv-wf-usb-master-search/md-reference"
BUILD = WORK_ROOT / "work/accession/collection-555-build-20260829"
UNIT = Path("/mnt/volume/iss-archive/units/pressebildarchiv-wf")
SOURCE_ROOT = Path("/media/vladimir/Elements")
DECISIONS = INIT / "master-decisions.csv"
CANDIDATES = INIT / "candidate-dispositions.csv"
OBJECTS = INIT / "accession-objects.csv"
RESOURCES = INIT / "related-md-resources.csv"
DOCUMENTS = INIT / "document-source-disposition.csv"
RAW_MD = CAPTURE / "raw/page-0001-offset-000000.json"
REBUILD_DRAIN = WORK_ROOT / "rebuild_drain_list.py"
OVERLAP = WORK_ROOT / "work/collection-555-cross-unit-overlap-20260829.csv"
EXPECTED_OBJECTS = 48
EXPECTED_RELATED = 13
BLOCK = 8 * 1024 * 1024
USER_AGENT = "Industriesalon-Schoeneweide-Archive/1.0 (collection 555 accession; sequential resource fetch)"
EMBED_PROFILE = "iss-collection-555-derivative-v1"


class AccessionError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    atomic_bytes(path, csv_bytes(fields, rows))


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def copy_exact(source: Path, target: Path) -> tuple[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(BLOCK), b""):
            digest.update(block)
            outgoing.write(block)
            size += len(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    final_digest, final_size = hash_file(target)
    if digest.hexdigest() != final_digest or size != final_size:
        raise AccessionError(f"exact-copy verification failed: {source} -> {target}")
    return final_digest, final_size


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise AccessionError("Elements does not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != "/media/vladimir/Elements"
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise AccessionError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return value or "unnamed"


def md_objects() -> dict[str, dict[str, Any]]:
    payload = json.loads(RAW_MD.read_text(encoding="utf-8"))
    objects = payload.get("results", {}).get("objects", {})
    if not isinstance(objects, dict) or len(objects) != EXPECTED_OBJECTS:
        raise AccessionError(f"captured MD object count differs: {len(objects) if isinstance(objects, dict) else 'invalid'}")
    return {str(key): value for key, value in objects.items()}


def values_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def descriptive_metadata(obj: dict[str, Any], archival_id: str, object_url: str) -> dict[str, str]:
    images = obj.get("object_images") or []
    main = next((item for item in images if item.get("is_main") == "j"), images[0] if images else {})
    dates: list[str] = []
    places: list[str] = []
    people: list[str] = []
    for event in (obj.get("object_events") or []) + (obj.get("object_relation_places") or []) + (obj.get("object_relation_times") or []) + (obj.get("object_relation_people") or []):
        time_value = event.get("time") or {}
        place_value = event.get("place") or {}
        people_value = event.get("people") or {}
        if isinstance(time_value, dict):
            dates.extend([time_value.get("time_name", "")])
        if isinstance(place_value, dict):
            places.extend([place_value.get("place_name", "")])
        if isinstance(people_value, dict):
            people.extend([people_value.get("people_display_name") or people_value.get("displayname") or people_value.get("people_name") or ""])
    tags = values_unique(item.get("tag_name", "") for item in obj.get("object_tags") or [])
    creator = str(main.get("creator") or "").strip()
    rights = str(main.get("rights") or "").strip()
    owner = str(main.get("owner") or "").strip()
    inventory = str(obj.get("object_inventory_number") or "").strip()
    return {
        "archival_object_id": archival_id,
        "inventory": inventory,
        "title": str(obj.get("object_name") or "").strip(),
        "description": str(obj.get("object_description") or "").strip(),
        "object_type": str(obj.get("object_type") or "").strip(),
        "material_technique": str(obj.get("object_material_technique") or "").strip(),
        "dimensions": str(obj.get("object_dimensions") or "").strip(),
        "date": "; ".join(values_unique(dates)),
        "places": "; ".join(values_unique(places)),
        "related_people_organizations": "; ".join(values_unique(people)),
        "creator": creator,
        "rights": rights,
        "rights_holder": owner,
        "tags": "; ".join(tags),
        "institution": str((obj.get("object_institution") or {}).get("institution_name") or "").strip(),
        "collection": "; ".join(values_unique(item.get("collection_name", "") for item in obj.get("object_collection") or [])),
        "metadata_rights": str((obj.get("licence") or {}).get("metadata_rights_status") or "").strip(),
        "metadata_rights_holder": str((obj.get("licence") or {}).get("metadata_rights_holder") or "").strip(),
        "object_url": object_url,
    }


def xmp_tags(meta: dict[str, str], md_id: str, source_url: str, master_relative: str, role: str, master_sha: str) -> dict[str, str]:
    relation = f"iss:unit:pressebildarchiv-wf; museum-digital:object:{md_id}; {meta['object_url']}; iss:representation:{role}"
    source = f"Generated from {master_relative}; Master sha256:{master_sha}; museum-digital resource {source_url}"
    tags = {
        "XMP-dc:Identifier": meta["archival_object_id"],
        "XMP-dc:Title": meta["title"],
        "XMP-dc:Description": meta["description"],
        "XMP-dc:Type": meta["object_type"],
        "XMP-dc:Creator": meta["creator"],
        "XMP-dc:Coverage": meta["places"],
        "XMP-dc:Rights": "; ".join(values_unique([meta["rights"], meta["rights_holder"]])),
        "XMP-dc:Subject": meta["tags"],
        "XMP-dc:Source": source,
        "XMP-dc:Relation": relation,
        "XMP-xmp:Label": EMBED_PROFILE,
    }
    if re.fullmatch(r"(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?", meta["date"]):
        tags["XMP-dc:Date"] = meta["date"]
    elif meta["date"]:
        tags["XMP-iptcExt:ArtworkCircaDateCreated"] = meta["date"]
    return {key: value for key, value in tags.items() if value}


def embed_and_verify(path: Path, tags: dict[str, str]) -> None:
    arguments = ["exiftool", "-overwrite_original", "-P"]
    arguments.extend(f"-{key}={value}" for key, value in tags.items())
    arguments.append(str(path))
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or "Warning:" in result.stdout + result.stderr:
        raise AccessionError(f"ExifTool embedding failed for {path}: {(result.stdout + result.stderr).strip()}")
    probe = subprocess.run(
        ["exiftool", "-j", "-XMP-dc:Identifier", "-XMP-dc:Title", "-XMP-xmp:Label", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    observed = json.loads(probe.stdout)[0]
    expected = {
        "Identifier": tags["XMP-dc:Identifier"],
        "Title": tags["XMP-dc:Title"],
        "Label": tags["XMP-xmp:Label"],
    }
    if any(observed.get(key) != value for key, value in expected.items()):
        raise AccessionError(f"embedded metadata readback differs for {path}: expected={expected}, observed={observed}")


def decoded_digest(path: Path) -> tuple[str, int, int]:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        return digest, rgb.width, rgb.height


def generate_derivatives(source: Path, tiff: Path, jpeg: Path, tags_tiff: dict[str, str], tags_jpeg: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], str]:
    source_decoded, width, height = decoded_digest(source)
    with Image.open(source) as image:
        rgb = image.convert("RGB")
        rgb.save(tiff, format="TIFF", compression="tiff_adobe_deflate")
        rgb.save(jpeg, format="JPEG", quality=95, subsampling=0, optimize=True)
    embed_and_verify(tiff, tags_tiff)
    embed_and_verify(jpeg, tags_jpeg)
    tiff_decoded, tw, th = decoded_digest(tiff)
    _, jw, jh = decoded_digest(jpeg)
    if (tw, th) != (width, height) or (jw, jh) != (width, height):
        raise AccessionError(f"derivative dimensions differ for {source}")
    if tiff_decoded != source_decoded:
        raise AccessionError(f"decoded TIFF pixels differ from selected Master: {source}")
    tiff_sha, tiff_size = hash_file(tiff)
    jpeg_sha, jpeg_size = hash_file(jpeg)
    return (
        {"sha256": tiff_sha, "size_bytes": tiff_size, "width": width, "height": height},
        {"sha256": jpeg_sha, "size_bytes": jpeg_size, "width": width, "height": height},
        source_decoded,
    )


def fetch_related_resources(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    fetched = 0
    fields = list(rows[0]) if rows else []
    for row in rows:
        if row["is_main"] == "true" or row["retained_work_path"]:
            continue
        target = MD_REFERENCE / f"md-{row['md_object_id']}-resource-{row['resource_position']}-{row['resource_id']}.jpg"
        if target.exists():
            with Image.open(target) as image:
                image.verify()
        else:
            last_error: Exception | None = None
            for attempt in range(1, 4):
                temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
                try:
                    request = urllib.request.Request(row["resource_url"], headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/*;q=0.9"})
                    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as output:
                        if response.status != 200:
                            raise AccessionError(f"HTTP {response.status} for {row['resource_url']}")
                        shutil.copyfileobj(response, output, BLOCK)
                        output.flush()
                        os.fsync(output.fileno())
                    with Image.open(temporary) as image:
                        image.verify()
                    os.replace(temporary, target)
                    last_error = None
                    break
                except (OSError, urllib.error.URLError, AccessionError) as error:
                    temporary.unlink(missing_ok=True)
                    last_error = error
                    if attempt < 3:
                        time.sleep(attempt * 2)
            if last_error is not None:
                raise AccessionError(f"failed to fetch related MD resource {row['resource_id']}: {last_error}")
            time.sleep(1)
        row["retained_work_path"] = str(target)
        row["accession_state"] = "available_in_work"
        fetched += 1
    if sum(row["is_main"] != "true" for row in rows) != EXPECTED_RELATED:
        raise AccessionError("related MD resource count differs")
    if any(not row["retained_work_path"] for row in rows):
        raise AccessionError("an MD resource has no retained work path")
    atomic_csv(RESOURCES, fields, rows)
    print(json.dumps({"related_resources_ready": EXPECTED_RELATED, "newly_bound": fetched}), flush=True)
    return rows


def load_plan(require_absent: bool = True) -> dict[str, Any]:
    mount = source_guard()
    if require_absent and (UNIT.exists() or BUILD.exists()):
        raise AccessionError(f"destination or build directory already exists: {UNIT} / {BUILD}")
    for path in [DECISIONS, CANDIDATES, OBJECTS, RESOURCES, DOCUMENTS, RAW_MD, REBUILD_DRAIN, OVERLAP]:
        if not path.is_file():
            raise AccessionError(f"required accession input missing: {path}")
    decisions = read_csv(DECISIONS)
    candidates = read_csv(CANDIDATES)
    objects = read_csv(OBJECTS)
    if len(decisions) != EXPECTED_OBJECTS or len(objects) != EXPECTED_OBJECTS:
        raise AccessionError("object/decision count differs from 48")
    if any(row["decision_state"] != "selected_master" or not row["selected_candidate_id"] for row in decisions):
        raise AccessionError("not every object has one selected Master")
    if len({row["md_object_id"] for row in decisions}) != EXPECTED_OBJECTS:
        raise AccessionError("Master decisions are not unique by object")
    candidate_by_id = {row["candidate_id"]: row for row in candidates}
    object_by_md = {row["md_object_id"]: row for row in objects}
    selected: dict[str, dict[str, str]] = {}
    for decision in decisions:
        candidate = candidate_by_id.get(decision["selected_candidate_id"])
        if candidate is None or candidate["md_object_id"] != decision["md_object_id"]:
            raise AccessionError(f"selected candidate binding differs for {decision['md_object_id']}")
        if candidate["disposition"] != "selected_master" or candidate["candidate_source"] != "museum-digital retained main image":
            raise AccessionError(f"selected Master is not the confirmed MD main image for {decision['md_object_id']}")
        path = Path(candidate["candidate_path"])
        if not path.is_file() or not str(path).startswith(str(WORK_ROOT) + "/"):
            raise AccessionError(f"selected Master input missing or outside work root: {path}")
        selected[decision["md_object_id"]] = candidate
    if any(row["deletion_authorized"].lower() != "false" for row in decisions + candidates + objects):
        raise AccessionError("reconciliation inputs carry deletion authority")
    docs = [row for row in read_csv(DOCUMENTS) if row["disposition"].startswith("retain_project_document")]
    if len(docs) != 4:
        raise AccessionError(f"expected four retained project documents, found {len(docs)}")
    for row in docs:
        source = Path(row["source_path"])
        digest, size = hash_file(source)
        if row["sha256"] != f"sha256:{digest}" or int(row["size_bytes"]) != size:
            raise AccessionError(f"project document source binding differs: {source}")
    return {
        "mount": mount,
        "decisions": decisions,
        "candidates": candidates,
        "objects": object_by_md,
        "selected": selected,
        "documents": docs,
        "md": md_objects(),
    }


CATALOG_FIELDS = [
    "archival_object_id", "inventory", "title", "description", "object_type", "material_technique", "dimensions",
    "date", "places", "related_people_organizations", "creator", "rights", "rights_holder", "tags", "institution",
    "collection", "metadata_rights", "metadata_rights_holder", "md_object_id", "md_object_url", "md_main_resource_id",
    "md_main_resource_url", "master_relative_path", "master_sha256", "master_size_bytes", "master_width", "master_height",
    "master_resolution_class", "original_source_path", "tiff_derivative_relative_path", "tiff_derivative_sha256",
    "jpeg_derivative_relative_path", "jpeg_derivative_sha256", "related_resource_paths", "metadata_jsonl", "selection_note",
    "accessioned_utc", "deletion_authorized",
]
REP_FIELDS = [
    "archival_object_id", "md_object_id", "inventory", "role", "format", "relative_path", "original_source_path",
    "source_url", "source_sha256", "sha256", "size_bytes", "width", "height", "derivative_of", "decoded_pixel_sha256",
    "metadata_embedding_profile", "provenance", "deletion_authorized",
]
DOC_FIELDS = [
    "document_id", "filename", "role", "format", "relative_path", "original_source_path", "source_sha256", "sha256",
    "size_bytes", "integrity_state", "review_note", "deletion_authorized",
]
CHECK_FIELDS = ["relative_path", "size_bytes", "sha256", "payload_type"]


def write_sqlite(catalog: list[dict[str, Any]], reps: list[dict[str, Any]], documents: list[dict[str, Any]], source_rows: list[dict[str, str]]) -> dict[str, int]:
    path = BUILD / "db-restore/RESTORE.sqlite"
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            CREATE TABLE objects (
                archival_object_id TEXT PRIMARY KEY, md_object_id TEXT UNIQUE NOT NULL,
                inventory TEXT UNIQUE NOT NULL, title TEXT NOT NULL, master_relative_path TEXT UNIQUE NOT NULL,
                master_sha256 TEXT NOT NULL, tiff_derivative_relative_path TEXT UNIQUE NOT NULL,
                jpeg_derivative_relative_path TEXT UNIQUE NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE representations (
                relative_path TEXT PRIMARY KEY, archival_object_id TEXT NOT NULL REFERENCES objects(archival_object_id),
                role TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, integrity_state TEXT NOT NULL,
                deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            CREATE TABLE source_disposition (
                source_path TEXT PRIMARY KEY, md_object_id TEXT, relationship TEXT NOT NULL, final_archive_path TEXT NOT NULL,
                drain_state TEXT NOT NULL, deletion_authorized INTEGER NOT NULL CHECK(deletion_authorized=0)
            );
            """
        )
        con.executemany(
            "INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,0)",
            [(r["archival_object_id"], r["md_object_id"], r["inventory"], r["title"], r["master_relative_path"], r["master_sha256"], r["tiff_derivative_relative_path"], r["jpeg_derivative_relative_path"]) for r in catalog],
        )
        con.executemany(
            "INSERT INTO representations VALUES (?,?,?,?,?,0)",
            [(r["relative_path"], r["archival_object_id"], r["role"], r["sha256"], int(r["size_bytes"])) for r in reps],
        )
        con.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,0)",
            [(r["document_id"], r["relative_path"], r["sha256"], int(r["size_bytes"]), r["integrity_state"]) for r in documents],
        )
        con.executemany(
            "INSERT INTO source_disposition VALUES (?,?,?,?,?,0)",
            [(r["source_path"], r["md_object_id"], r["relationship"], r["final_archive_path"], r["drain_state"]) for r in source_rows],
        )
        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("objects", "representations", "documents", "source_disposition")}
        if integrity != "ok" or foreign:
            raise AccessionError(f"SQLite validation failed: integrity={integrity}, foreign={foreign}")
    finally:
        con.close()
    return counts


def build_unit() -> dict[str, Any]:
    started = now()
    plan = load_plan(require_absent=True)
    resources = fetch_related_resources(read_csv(RESOURCES))
    subprocess.run([str(REBUILD_DRAIN)], check=True, timeout=3600)
    drain = read_csv(INIT / "drain-list.csv")
    if len(drain) != 121:
        raise AccessionError(f"expected comprehensive 121-row drain list after resource fetch, found {len(drain)}")
    if any(row["deletion_authorized"] != "false" for row in drain):
        raise AccessionError("drain list carries deletion authority")
    for name in ("00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"):
        (BUILD / name).mkdir(parents=True, exist_ok=False)

    resource_by_md: dict[str, list[dict[str, str]]] = {}
    for row in resources:
        resource_by_md.setdefault(row["md_object_id"], []).append(row)
    catalog: list[dict[str, Any]] = []
    reps: list[dict[str, Any]] = []
    metadata_jsonl: list[str] = []
    final_by_md: dict[str, str] = {}
    related_final_by_source: dict[str, str] = {}
    accessioned = now()

    for md_id in sorted(plan["selected"], key=int):
        selected = plan["selected"][md_id]
        object_row = plan["objects"][md_id]
        obj = plan["md"][md_id]
        meta = descriptive_metadata(obj, object_row["archival_object_id"], object_row["object_url"])
        base = f"{slug(meta['inventory'])}__md-{md_id}"
        master_rel = f"masters/{base}.jpg"
        tiff_rel = f"export/{base}__metadata.tif"
        jpeg_rel = f"export/{base}__metadata.jpg"
        master_source = Path(selected["candidate_path"])
        master = BUILD / master_rel
        master_sha, master_size = copy_exact(master_source, master)
        expected_sha = selected["known_sha256"].removeprefix("sha256:")
        if expected_sha and master_sha != expected_sha:
            raise AccessionError(f"selected Master hash differs for {md_id}")
        decoded, width, height = decoded_digest(master)
        main_resource = next(row for row in resource_by_md[md_id] if row["is_main"] == "true")
        tiff_tags = xmp_tags(meta, md_id, main_resource["resource_url"], master_rel, "full_resolution_tiff_derivative", master_sha)
        jpeg_tags = xmp_tags(meta, md_id, main_resource["resource_url"], master_rel, "full_resolution_jpeg_derivative", master_sha)
        tiff_info, jpeg_info, verified_decoded = generate_derivatives(master, BUILD / tiff_rel, BUILD / jpeg_rel, tiff_tags, jpeg_tags)
        if decoded != verified_decoded:
            raise AccessionError(f"decoded Master changed during derivative generation: {master}")
        final_by_md[md_id] = master_rel
        reps.extend([
            {"archival_object_id": meta["archival_object_id"], "md_object_id": md_id, "inventory": meta["inventory"], "role": "selected_master", "format": "JPEG", "relative_path": master_rel, "original_source_path": str(master_source), "source_url": main_resource["resource_url"], "source_sha256": master_sha, "sha256": master_sha, "size_bytes": master_size, "width": width, "height": height, "derivative_of": "", "decoded_pixel_sha256": decoded, "metadata_embedding_profile": "unchanged_source_bytes; metadata in JSONL and generated derivatives", "provenance": "human-selected retained museum-digital main image", "deletion_authorized": "false"},
            {"archival_object_id": meta["archival_object_id"], "md_object_id": md_id, "inventory": meta["inventory"], "role": "full_resolution_tiff_derivative", "format": "TIFF", "relative_path": tiff_rel, "original_source_path": str(master_source), "source_url": main_resource["resource_url"], "source_sha256": master_sha, "sha256": tiff_info["sha256"], "size_bytes": tiff_info["size_bytes"], "width": width, "height": height, "derivative_of": master_rel, "decoded_pixel_sha256": decoded, "metadata_embedding_profile": EMBED_PROFILE, "provenance": "accession-generated from selected Master; decoded pixels verified equal", "deletion_authorized": "false"},
            {"archival_object_id": meta["archival_object_id"], "md_object_id": md_id, "inventory": meta["inventory"], "role": "full_resolution_jpeg_derivative", "format": "JPEG", "relative_path": jpeg_rel, "original_source_path": str(master_source), "source_url": main_resource["resource_url"], "source_sha256": master_sha, "sha256": jpeg_info["sha256"], "size_bytes": jpeg_info["size_bytes"], "width": width, "height": height, "derivative_of": master_rel, "decoded_pixel_sha256": "", "metadata_embedding_profile": EMBED_PROFILE, "provenance": "accession-generated at original pixel dimensions, JPEG quality 95 and 4:4:4", "deletion_authorized": "false"},
        ])
        related_paths: list[str] = []
        for resource in sorted(resource_by_md[md_id], key=lambda row: int(row["resource_position"])):
            if resource["is_main"] == "true":
                continue
            source = Path(resource["retained_work_path"])
            rel = f"export/{base}__related-{int(resource['resource_position']):02d}__md-resource-{resource['resource_id']}.jpg"
            digest, size = copy_exact(source, BUILD / rel)
            _, rw, rh = decoded_digest(BUILD / rel)
            related_paths.append(rel)
            related_final_by_source[str(source)] = rel
            reps.append({"archival_object_id": meta["archival_object_id"], "md_object_id": md_id, "inventory": meta["inventory"], "role": "related_catalogue_image_original", "format": "JPEG", "relative_path": rel, "original_source_path": str(source), "source_url": resource["resource_url"], "source_sha256": digest, "sha256": digest, "size_bytes": size, "width": rw, "height": rh, "derivative_of": "", "decoded_pixel_sha256": "", "metadata_embedding_profile": "unchanged_source_bytes; metadata in objects.jsonl", "provenance": resource["description"] or "additional museum-digital image resource", "deletion_authorized": "false"})
        record = dict(obj)
        record["archive_accession"] = {"archival_object_id": meta["archival_object_id"], "unit": "pressebildarchiv-wf", "selected_master": master_rel, "selected_master_sha256": master_sha, "generated_derivatives": [tiff_rel, jpeg_rel], "related_resource_paths": related_paths, "accessioned_utc": accessioned, "deletion_authorized": False}
        metadata_jsonl.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        catalog.append({**meta, "md_object_id": md_id, "md_object_url": meta["object_url"], "md_main_resource_id": main_resource["resource_id"], "md_main_resource_url": main_resource["resource_url"], "master_relative_path": master_rel, "master_sha256": master_sha, "master_size_bytes": master_size, "master_width": width, "master_height": height, "master_resolution_class": "high_resolution" if max(width, height) >= 1000 else "low_resolution_human_selected_surrogate", "original_source_path": str(master_source), "tiff_derivative_relative_path": tiff_rel, "tiff_derivative_sha256": tiff_info["sha256"], "jpeg_derivative_relative_path": jpeg_rel, "jpeg_derivative_sha256": jpeg_info["sha256"], "related_resource_paths": "; ".join(related_paths), "metadata_jsonl": "metadata/objects.jsonl", "selection_note": next(row["decision_note"] for row in plan["decisions"] if row["md_object_id"] == md_id), "accessioned_utc": accessioned, "deletion_authorized": "false"})
        print(json.dumps({"accessioned_object": md_id, "inventory": meta["inventory"], "objects_complete": len(catalog)}), flush=True)

    atomic_bytes(BUILD / "metadata/objects.jsonl", ("\n".join(metadata_jsonl) + "\n").encode("utf-8"))
    documents: list[dict[str, Any]] = []
    doc_final_by_source: dict[str, str] = {}
    for row in plan["documents"]:
        rel = f"documents/{row['filename']}"
        digest, size = copy_exact(Path(row["source_path"]), BUILD / rel)
        if digest != row["sha256"].removeprefix("sha256:"):
            raise AccessionError(f"document final hash differs: {row['filename']}")
        documents.append({"document_id": row["document_id"], "filename": row["filename"], "role": row["document_kind"], "format": row["format"], "relative_path": rel, "original_source_path": row["source_path"], "source_sha256": digest, "sha256": digest, "size_bytes": size, "integrity_state": row["integrity_state"], "review_note": row["review_note"], "deletion_authorized": "false"})
        doc_final_by_source[row["source_path"]] = rel

    final_source_rows: list[dict[str, str]] = []
    for row in drain:
        final = related_final_by_source.get(row["source_path"])
        if not final and row["source_kind"] == "document":
            final = doc_final_by_source.get(row["source_path"])
        if not final:
            final = final_by_md.get(row["md_object_id"])
        if not final:
            raise AccessionError(f"drain row has no final archive coverage: {row['source_path']}")
        updated = dict(row)
        updated["coverage_state"] = "validated_accession_coverage"
        updated["final_archive_path"] = final
        updated["drain_state"] = "covered_by_validated_accession"
        updated["deletion_authorized"] = "false"
        final_source_rows.append(updated)

    atomic_csv(BUILD / "catalog.csv", CATALOG_FIELDS, catalog)
    atomic_csv(BUILD / "00_MANIFESTS/representations.csv", REP_FIELDS, reps)
    atomic_csv(BUILD / "00_MANIFESTS/documents.csv", DOC_FIELDS, documents)
    atomic_csv(BUILD / "00_MANIFESTS/source-disposition.csv", list(final_source_rows[0]), final_source_rows)
    atomic_csv(BUILD / "00_MANIFESTS/drain-list.csv", list(final_source_rows[0]), final_source_rows)
    shutil.copyfile(DECISIONS, BUILD / "00_MANIFESTS/master-decisions.csv")
    shutil.copyfile(OVERLAP, BUILD / "00_MANIFESTS/cross-unit-overlap.csv")
    sqlite_counts = write_sqlite(catalog, reps, documents, final_source_rows)

    readme = f"""# Pressebildarchiv WF

This unit is the authoritative archive representation for the 48 reconciled objects in
museum-digital collection 555. Museum-digital is a projection of this unit.

- Archival objects and explicitly selected Masters: {len(catalog)}
- Unchanged museum-digital JPEG Master inputs: {len(catalog)}
- Accession-generated metadata-bearing full-resolution TIFF derivatives: {len(catalog)}
- Accession-generated metadata-bearing full-resolution JPEG derivatives: {len(catalog)}
- Retained additional museum-digital image resources: {EXPECTED_RELATED}
- Retained project documents: {len(documents)}
- Comprehensive source/drain rows: {len(final_source_rows)}
- Deletion authorized: false

`masters/` contains exactly one unchanged selected Master per object. `export/` contains
the derivatives generated during accession plus exact-byte additional catalogue images.
The TIFF derivatives have decoded pixels equal to their selected Masters. Descriptive,
rights, catalogue, and provenance metadata is embedded in both generated derivative types
using `{EMBED_PROFILE}` and is also preserved in `catalog.csv` and
`metadata/objects.jsonl`.

`00_MANIFESTS/drain-list.csv` covers selected inputs, local duplicates and earlier
derivatives, inventory-linked noise, related catalogue images, and retained project
documents. Coverage confirms that the validated unit represents those source occurrences;
it does not authorize deletion. USB cleanup remains a separate future decision.

`documents/Inventar Primus 1 und 2.xlsx` is retained unchanged. Its stable source hash is
recorded, but ZIP tools reported overlapping components and 70,118 trailing bytes. The
warning is preserved in `00_MANIFESTS/documents.csv`.

`db-restore/RESTORE.sqlite` is a rebuildable catalogue projection. The CSV, JSONL, Markdown,
and filesystem layout remain sufficient to understand the unit without SQLite.
"""
    atomic_bytes(BUILD / "00_README.md", readme.encode("utf-8"))

    excluded = {"00_MANIFESTS/checksums.csv", "00_MANIFESTS/accession-completion.json"}
    checksum_rows: list[dict[str, Any]] = []
    for path in sorted(item for item in BUILD.rglob("*") if item.is_file()):
        rel = path.relative_to(BUILD).as_posix()
        if rel in excluded:
            continue
        digest, size = hash_file(path)
        payload_type = "unit_record"
        if rel.startswith("masters/"):
            payload_type = "selected_master"
        elif rel.startswith("export/"):
            payload_type = "export_representation"
        elif rel.startswith("documents/"):
            payload_type = "project_document"
        atomic_row = {"relative_path": rel, "size_bytes": size, "sha256": digest, "payload_type": payload_type}
        checksum_rows.append(atomic_row)
    atomic_csv(BUILD / "00_MANIFESTS/checksums.csv", CHECK_FIELDS, checksum_rows)
    checksum_sha, checksum_size = hash_file(BUILD / "00_MANIFESTS/checksums.csv")
    completion = {
        "format": "iss-pressebildarchiv-wf-accession-completion-v1",
        "started_utc": started,
        "completed_utc": now(),
        "target_unit": str(UNIT),
        "source_mount": source_guard(),
        "objects": len(catalog),
        "selected_masters": sum(r["role"] == "selected_master" for r in reps),
        "full_resolution_tiff_derivatives": sum(r["role"] == "full_resolution_tiff_derivative" for r in reps),
        "full_resolution_jpeg_derivatives": sum(r["role"] == "full_resolution_jpeg_derivative" for r in reps),
        "related_catalogue_images": sum(r["role"] == "related_catalogue_image_original" for r in reps),
        "project_documents": len(documents),
        "drain_rows": len(final_source_rows),
        "drain_deletion_authorized": False,
        "low_resolution_human_selected_surrogates": sum(r["master_resolution_class"].startswith("low_resolution") for r in catalog),
        "metadata_embedding_profile": EMBED_PROFILE,
        "metadata_readback_verified": True,
        "tiff_decoded_pixel_equality_verified": True,
        "sqlite_integrity": "ok",
        "sqlite_counts": sqlite_counts,
        "checksum_rows": len(checksum_rows),
        "checksum_manifest_sha256": checksum_sha,
        "checksum_manifest_size_bytes": checksum_size,
        "accession_verified": True,
        "deletion_authorized": False,
    }
    atomic_json(BUILD / "00_MANIFESTS/accession-completion.json", completion)
    os.sync()
    os.replace(BUILD, UNIT)
    os.sync()
    print(json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return completion


def validate_unit() -> dict[str, Any]:
    mount = source_guard()
    if not UNIT.is_dir() or BUILD.exists():
        raise AccessionError("published unit is absent or a build directory remains")
    required_dirs = {"00_MANIFESTS", "masters", "export", "metadata", "documents", "db-restore"}
    actual_dirs = {path.name for path in UNIT.iterdir() if path.is_dir()}
    if actual_dirs != required_dirs:
        raise AccessionError(f"unit directory structure differs: {actual_dirs}")
    catalog = read_csv(UNIT / "catalog.csv")
    reps = read_csv(UNIT / "00_MANIFESTS/representations.csv")
    docs = read_csv(UNIT / "00_MANIFESTS/documents.csv")
    drain = read_csv(UNIT / "00_MANIFESTS/drain-list.csv")
    source = read_csv(UNIT / "00_MANIFESTS/source-disposition.csv")
    checksums = read_csv(UNIT / "00_MANIFESTS/checksums.csv")
    if len(catalog) != 48 or len({r["archival_object_id"] for r in catalog}) != 48:
        raise AccessionError("catalogue does not contain 48 unique objects")
    roles = Counter(row["role"] for row in reps)
    expected_roles = {"selected_master": 48, "full_resolution_tiff_derivative": 48, "full_resolution_jpeg_derivative": 48, "related_catalogue_image_original": 13}
    if roles != expected_roles:
        raise AccessionError(f"representation roles differ: {roles}")
    master_counts = Counter(row["archival_object_id"] for row in reps if row["role"] == "selected_master")
    if any(master_counts[row["archival_object_id"]] != 1 for row in catalog):
        raise AccessionError("an object does not have exactly one Master")
    if len(docs) != 4 or len(drain) != 121 or source != drain:
        raise AccessionError("document or source/drain manifest count differs")
    if any(row["deletion_authorized"] != "false" for row in catalog + reps + docs + drain):
        raise AccessionError("published records carry deletion authority")
    excluded = {"00_MANIFESTS/checksums.csv", "00_MANIFESTS/accession-completion.json"}
    actual_files = {p.relative_to(UNIT).as_posix() for p in UNIT.rglob("*") if p.is_file() and p.relative_to(UNIT).as_posix() not in excluded}
    listed_files = {row["relative_path"] for row in checksums}
    if actual_files != listed_files:
        raise AccessionError(f"checksum manifest paths differ: missing={actual_files-listed_files}, extra={listed_files-actual_files}")
    for row in checksums:
        digest, size = hash_file(UNIT / row["relative_path"])
        if digest != row["sha256"] or size != int(row["size_bytes"]):
            raise AccessionError(f"checksum differs: {row['relative_path']}")
    for row in catalog:
        master = UNIT / row["master_relative_path"]
        tiff = UNIT / row["tiff_derivative_relative_path"]
        jpeg = UNIT / row["jpeg_derivative_relative_path"]
        master_decoded, mw, mh = decoded_digest(master)
        tiff_decoded, tw, th = decoded_digest(tiff)
        _, jw, jh = decoded_digest(jpeg)
        if master_decoded != tiff_decoded or (mw, mh) != (tw, th) or (mw, mh) != (jw, jh):
            raise AccessionError(f"derivative pixel/dimension validation differs: {row['inventory']}")
        for derivative in (tiff, jpeg):
            probe = subprocess.run(["exiftool", "-j", "-XMP-dc:Identifier", "-XMP-xmp:Label", str(derivative)], check=True, capture_output=True, text=True)
            observed = json.loads(probe.stdout)[0]
            if observed.get("Identifier") != row["archival_object_id"] or observed.get("Label") != EMBED_PROFILE:
                raise AccessionError(f"embedded metadata differs: {derivative}")
    con = sqlite3.connect(UNIT / "db-restore/RESTORE.sqlite")
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchall()
        sqlite_counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("objects", "representations", "documents", "source_disposition")}
    finally:
        con.close()
    expected_counts = {"objects": 48, "representations": 157, "documents": 4, "source_disposition": 121}
    if integrity != "ok" or foreign or sqlite_counts != expected_counts:
        raise AccessionError(f"SQLite validation differs: integrity={integrity}, foreign={foreign}, counts={sqlite_counts}")
    completion = json.loads((UNIT / "00_MANIFESTS/accession-completion.json").read_text(encoding="utf-8"))
    if not completion.get("accession_verified") or completion.get("deletion_authorized"):
        raise AccessionError("completion receipt is not a successful non-authorizing accession")
    result = {"result": "ok", "unit": str(UNIT), "source_mount": mount, "objects": 48, "representations": dict(roles), "documents": 4, "drain_rows": 121, "deletion_authorized_rows": 0, "sqlite_integrity": "ok", "sqlite_counts": sqlite_counts, "checksum_rows": len(checksums), "metadata_readback_verified": True, "tiff_decoded_pixel_equality_verified": True}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--accession", action="store_true")
    group.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        if args.accession:
            build_unit()
        else:
            validate_unit()
        return 0
    except Exception as error:
        print(json.dumps({"result": "error", "error": str(error)}, ensure_ascii=False, indent=2), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
