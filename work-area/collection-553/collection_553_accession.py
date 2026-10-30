#!/usr/bin/env python3
"""Accession the resolved museum-digital collection 553 as ``archiv-wf``.

The implementation is intentionally direct: one unit, one Master
representation per object, one generated JPEG access derivative per object,
human-readable CSV/Markdown records, and one rebuildable SQLite catalogue.
Source files are copied without modification and deletion is never authorized.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

import collection_553_accession_preflight as preflight_module
import collection_553_workbench as workbench_module


SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-553")
RECON_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
ARCHIVE_ROOT = Path("/mnt/volume/iss-archive")
UNIT_NAME = "archiv-wf"
UNIT = ARCHIVE_ROOT / "units" / UNIT_NAME
UNIT_DIRS = {
    "00_MANIFESTS",
    "masters",
    "export",
    "metadata",
    "documents",
    "db-restore",
}
AUTHORITY = "explicit user instruction to run collection 553 accession on 2026-09-02"


CATALOG_FIELDS = [
    "archival_object_id",
    "md_object_id",
    "inventory",
    "title",
    "object_type",
    "date",
    "master_representation_id",
    "master_representation_type",
    "master_member_count",
    "master_member_paths_json",
    "access_derivative_path",
    "metadata_authority",
    "object_url",
    "deletion_authorized",
]
MASTER_FIELDS = [
    "master_representation_id",
    "archival_object_id",
    "md_object_id",
    "inventory",
    "representation_type",
    "member_count",
    "member_paths_json",
    "member_sha256_json",
    "selected_candidate_id",
    "original_source_paths_json",
    "representation_note",
    "decision_note",
    "actor",
    "decided_at",
    "deletion_authorized",
]
REPRESENTATION_FIELDS = [
    "representation_id",
    "master_representation_id",
    "archival_object_id",
    "md_object_id",
    "inventory",
    "role",
    "member_position",
    "relative_path",
    "original_source_path",
    "source_url",
    "format",
    "sha256",
    "size_bytes",
    "page_count",
    "width_px",
    "height_px",
    "derivative_of",
    "derivative_source_member",
    "provenance",
    "deletion_authorized",
]
SOURCE_FIELDS = [
    "candidate_id",
    "md_object_id",
    "inventory",
    "source_path",
    "source_kind",
    "candidate_role",
    "candidate_sha256",
    "size_bytes",
    "disposition",
    "relationship",
    "covered_by_selected_master",
    "final_archive_paths_json",
    "drain_state",
    "reason",
    "deletion_authorized",
]
DOCUMENT_FIELDS = [
    "document_id",
    "md_object_id",
    "inventory",
    "relative_path",
    "original_source_path",
    "sha256",
    "size_bytes",
    "provenance",
    "deletion_authorized",
]
PAYLOAD_FIELDS = ["relative_path", "sha256", "size_bytes"]


class AccessionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalized(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def safe_name(value: str) -> str:
    result = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return result.strip("._") or "unnamed"


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(fields),
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, object]]) -> None:
    write_exclusive(path, csv_bytes(fields, rows))


def write_json(path: Path, payload: object) -> None:
    write_exclusive(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def copy_master(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while block := incoming.read(8 * 1024 * 1024):
            digest.update(block)
            size += len(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return digest.hexdigest(), size


def decoded_pixels(path: Path) -> str:
    digest = hashlib.sha256()
    with Image.open(path) as image:
        pixels = ImageOps.exif_transpose(image).convert("RGB")
        digest.update(f"{pixels.width}x{pixels.height}:RGB".encode("ascii"))
        digest.update(pixels.tobytes())
    return digest.hexdigest()


def generate_derivative(source: Path, destination: Path) -> tuple[int, int]:
    if source.suffix.casefold() == ".pdf":
        prefix = destination.with_suffix("")
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-singlefile",
                "-jpeg",
                "-r",
                "150",
                str(source),
                str(prefix),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    else:
        with Image.open(source) as decoded:
            image = ImageOps.exif_transpose(decoded).convert("RGB")
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=88, optimize=True)
    if not destination.is_file():
        raise AccessionError(f"access derivative was not generated: {destination}")
    with Image.open(destination) as image:
        image.load()
        return image.width, image.height


def metadata_values(
    obj: dict[str, str], resources: list[dict[str, str]]
) -> dict[str, str]:
    rights = "; ".join(dict.fromkeys(row["rights"].strip() for row in resources if row["rights"].strip()))
    creators = "; ".join(
        dict.fromkeys(row["creator"].strip() for row in resources if row["creator"].strip())
    )
    owners = "; ".join(dict.fromkeys(row["owner"].strip() for row in resources if row["owner"].strip()))
    return {
        "title": normalized(obj["title"]),
        "description": normalized(obj["description"]),
        "identifier": normalized(obj["inventory"]),
        "source": normalized(obj["object_url"]),
        "rights": rights,
        "creator": creators,
        "credit": owners,
        "object_type": normalized(obj["object_type"]),
    }


def embed_derivative_metadata(path: Path, values: dict[str, str]) -> None:
    arguments = [
        "exiftool",
        "-overwrite_original",
        f"-XMP-dc:Title={values['title']}",
        f"-XMP-dc:Description={values['description']}",
        f"-XMP-dc:Identifier={values['identifier']}",
        f"-XMP-dc:Source={values['source']}",
        f"-IPTC:ObjectName={values['title']}",
        f"-IPTC:Caption-Abstract={values['description']}",
        f"-IPTC:Keywords={values['identifier']}",
        f"-IPTC:Keywords+={values['object_type']}",
    ]
    if values["rights"]:
        arguments.append(f"-XMP-dc:Rights={values['rights']}")
        arguments.append(f"-IPTC:CopyrightNotice={values['rights']}")
    if values["creator"]:
        arguments.append(f"-XMP-dc:Creator={values['creator']}")
        arguments.append(f"-IPTC:By-line={values['creator']}")
    if values["credit"]:
        arguments.append(f"-IPTC:Credit={values['credit']}")
    arguments.append(str(path))
    subprocess.run(arguments, check=True, capture_output=True, timeout=120)


def exiftool_values(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "exiftool",
            "-j",
            "-G1",
            "-s",
            "-XMP-dc:Title",
            "-XMP-dc:Description",
            "-XMP-dc:Identifier",
            "-XMP-dc:Source",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise AccessionError(f"unexpected ExifTool result: {path}")
    return rows[0]


def verify_derivative_metadata(path: Path, expected: dict[str, str]) -> None:
    actual = exiftool_values(path)
    wanted = {
        "XMP-dc:Title": expected["title"],
        "XMP-dc:Description": expected["description"],
        "XMP-dc:Identifier": expected["identifier"],
        "XMP-dc:Source": expected["source"],
    }
    for key, value in wanted.items():
        if normalized(actual.get(key)) != normalized(value):
            raise AccessionError(
                f"embedded metadata mismatch for {path.name}: {key}={actual.get(key)!r}"
            )


def create_database(
    path: Path,
    catalog: list[dict[str, object]],
    masters: list[dict[str, object]],
    representations: list[dict[str, object]],
    dispositions: list[dict[str, object]],
    relationships: list[dict[str, str]],
    exceptions: list[dict[str, str]],
) -> tuple[str, list[tuple[object, ...]], str]:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE objects (
            archival_object_id TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL UNIQUE,
            inventory TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            object_type TEXT NOT NULL,
            date_text TEXT NOT NULL,
            master_representation_id TEXT NOT NULL UNIQUE,
            master_representation_type TEXT NOT NULL,
            access_derivative_path TEXT NOT NULL UNIQUE,
            metadata_authority TEXT NOT NULL,
            object_url TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE master_representations (
            master_representation_id TEXT PRIMARY KEY,
            archival_object_id TEXT NOT NULL UNIQUE REFERENCES objects(archival_object_id),
            representation_type TEXT NOT NULL,
            member_count INTEGER NOT NULL CHECK (member_count > 0),
            member_paths_json TEXT NOT NULL,
            selected_candidate_id TEXT NOT NULL,
            decision_note TEXT NOT NULL,
            actor TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE representations (
            representation_id TEXT PRIMARY KEY,
            master_representation_id TEXT NOT NULL REFERENCES master_representations(master_representation_id),
            archival_object_id TEXT NOT NULL REFERENCES objects(archival_object_id),
            role TEXT NOT NULL,
            member_position INTEGER,
            relative_path TEXT NOT NULL UNIQUE,
            original_source_path TEXT NOT NULL,
            format TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            derivative_of TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE source_disposition (
            candidate_id TEXT PRIMARY KEY,
            archival_object_id TEXT NOT NULL REFERENCES objects(archival_object_id),
            source_path TEXT NOT NULL,
            disposition TEXT NOT NULL,
            relationship TEXT NOT NULL,
            covered_by_selected_master INTEGER NOT NULL,
            final_archive_paths_json TEXT NOT NULL,
            drain_state TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE object_relationships (
            left_md_object_id TEXT NOT NULL,
            right_md_object_id TEXT NOT NULL,
            relationship TEXT NOT NULL,
            evidence TEXT NOT NULL,
            decision_state TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE integrity_exceptions (
            document_id TEXT PRIMARY KEY,
            md_object_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            integrity_state TEXT NOT NULL,
            technical_error TEXT NOT NULL,
            disposition TEXT NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            archival_object_id TEXT NOT NULL REFERENCES objects(archival_object_id),
            relative_path TEXT NOT NULL UNIQUE,
            original_source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            deletion_authorized INTEGER NOT NULL CHECK (deletion_authorized=0)
        );
        """
    )
    connection.executemany(
        "INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        [
            (
                row["archival_object_id"],
                row["md_object_id"],
                row["inventory"],
                row["title"],
                row["object_type"],
                row["date"],
                row["master_representation_id"],
                row["master_representation_type"],
                row["access_derivative_path"],
                row["metadata_authority"],
                row["object_url"],
            )
            for row in catalog
        ],
    )
    connection.executemany(
        "INSERT INTO master_representations VALUES (?,?,?,?,?,?,?,?,?,0)",
        [
            (
                row["master_representation_id"],
                row["archival_object_id"],
                row["representation_type"],
                int(row["member_count"]),
                row["member_paths_json"],
                row["selected_candidate_id"],
                row["decision_note"],
                row["actor"],
                row["decided_at"],
            )
            for row in masters
        ],
    )
    connection.executemany(
        "INSERT INTO representations VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        [
            (
                row["representation_id"],
                row["master_representation_id"],
                row["archival_object_id"],
                row["role"],
                int(row["member_position"]) if row["member_position"] != "" else None,
                row["relative_path"],
                row["original_source_path"],
                row["format"],
                row["sha256"],
                int(row["size_bytes"]),
                row["derivative_of"],
            )
            for row in representations
        ],
    )
    inventory_by_md = {str(row["md_object_id"]): str(row["archival_object_id"]) for row in catalog}
    connection.executemany(
        "INSERT INTO source_disposition VALUES (?,?,?,?,?,?,?,?,0)",
        [
            (
                row["candidate_id"],
                inventory_by_md[str(row["md_object_id"])],
                row["source_path"],
                row["disposition"],
                row["relationship"],
                1 if row["covered_by_selected_master"] == "true" else 0,
                row["final_archive_paths_json"],
                row["drain_state"],
            )
            for row in dispositions
        ],
    )
    connection.executemany(
        "INSERT INTO object_relationships VALUES (?,?,?,?,?,0)",
        [
            (
                row["left_md_object_id"],
                row["right_md_object_id"],
                row["relationship"],
                row["evidence"],
                row["decision_state"],
            )
            for row in relationships
        ],
    )
    connection.executemany(
        "INSERT INTO integrity_exceptions VALUES (?,?,?,?,?,?,0)",
        [
            (
                row["document_id"],
                row["md_object_id"],
                row["source_path"],
                row["integrity_state"],
                row["technical_error"],
                row["disposition"],
            )
            for row in exceptions
        ],
    )
    connection.commit()
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    dump = "\n".join(connection.iterdump()) + "\n"
    connection.close()
    return quick_check, foreign_keys, dump


def source_is_protected(path: Path) -> bool:
    return path.is_absolute() and path.is_relative_to(SOURCE_ROOT)


def run_accession() -> dict[str, object]:
    started_at = utc_now()
    preflight = preflight_module.run_preflight(UNIT_NAME)
    if not preflight["accession_ready"]:
        raise AccessionError("accession preflight is blocked: " + "; ".join(preflight["blockers"]))
    if UNIT.exists():
        raise AccessionError(f"refusing to replace existing unit: {UNIT}")
    mount_before = workbench_module.source_guard()

    instance = preflight_module.projection_workbench()
    decisions = instance.decisions()
    objects_by_id = instance.objects_by_id
    resources_by_object: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in instance.resources:
        resources_by_object[row["md_object_id"]].append(row)
    for rows in resources_by_object.values():
        rows.sort(key=lambda row: int(row["resource_position"]))
    disposition_by_id = {
        row["candidate_id"]: row
        for row in workbench_module.read_csv(workbench_module.DISPOSITIONS_CSV)
    }

    input_paths = {
        Path(str(member["path"]))
        for member in preflight_module.selected_members(instance, decisions)
    }
    control_paths = {
        workbench_module.SUMMARY_JSON,
        workbench_module.OBJECTS_CSV,
        workbench_module.CANDIDATES_CSV,
        workbench_module.RESOURCES_CSV,
        workbench_module.SCENARIOS_CSV,
        workbench_module.RELATIONSHIPS_CSV,
        workbench_module.EXCEPTIONS_CSV,
        workbench_module.DECISIONS_CSV,
        workbench_module.DISPOSITIONS_CSV,
    }
    snapshots = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in input_paths | control_paths
    }

    plans: dict[str, list[dict[str, object]]] = {}
    all_relative_paths: set[str] = set()
    for object_id, decision in decisions.items():
        inventory_name = safe_name(decision["inventory"])
        members: list[dict[str, object]] = []
        if decision["master_representation_type"] == "ordered_md_resource_set":
            resource_ids = workbench_module.parsed_resource_ids(decision)
            if not resource_ids:
                raise AccessionError(f"compound Master lacks members: {object_id}")
            for position, resource_id in enumerate(resource_ids, 1):
                resource = instance.resources_by_id[resource_id]
                source = Path(resource["work_path"])
                suffix = source.suffix.casefold()
                relative = (
                    f"masters/{inventory_name}__md-{object_id}__part-{position:02d}-{resource_id}{suffix}"
                )
                members.append(
                    {
                        "member_id": f"md-resource:{resource_id}",
                        "position": position,
                        "source": source,
                        "relative_path": relative,
                        "format": resource["format"].casefold(),
                        "expected_hash": resource["sha256"].casefold(),
                        "expected_size": int(resource["size_bytes"]),
                        "page_count": resource["page_count"],
                        "width_px": resource["width_px"],
                        "height_px": resource["height_px"],
                        "source_url": resource["source_url"],
                        "resource_id": resource_id,
                    }
                )
        else:
            candidate = instance.candidates_by_id[decision["selected_candidate_id"]]
            source = Path(candidate["candidate_path"])
            suffix = source.suffix.casefold()
            relative = f"masters/{inventory_name}__md-{object_id}{suffix}"
            matching_resource = next(
                (
                    resource
                    for resource in resources_by_object[object_id]
                    if resource["work_path"] == str(source)
                    or resource["sha256"].casefold() == candidate["known_sha256"].casefold()
                ),
                None,
            )
            members.append(
                {
                    "member_id": candidate["candidate_id"],
                    "position": 1,
                    "source": source,
                    "relative_path": relative,
                    "format": candidate["format"].casefold(),
                    "expected_hash": candidate["known_sha256"].casefold(),
                    "expected_size": int(candidate["size_bytes"]),
                    "page_count": candidate["page_count"],
                    "width_px": candidate["width_px"],
                    "height_px": candidate["height_px"],
                    "source_url": matching_resource["source_url"] if matching_resource else "",
                    "resource_id": matching_resource["resource_id"] if matching_resource else "",
                }
            )
        plans[object_id] = members
        for member in members:
            relative = str(member["relative_path"])
            if relative in all_relative_paths:
                raise AccessionError(f"duplicate planned Master path: {relative}")
            all_relative_paths.add(relative)
        derivative = f"export/{inventory_name}__md-{object_id}__access.jpg"
        if derivative in all_relative_paths:
            raise AccessionError(f"duplicate planned derivative path: {derivative}")
        all_relative_paths.add(derivative)

    UNIT.mkdir()
    for directory in sorted(UNIT_DIRS):
        (UNIT / directory).mkdir()

    catalog_rows: list[dict[str, object]] = []
    master_rows: list[dict[str, object]] = []
    representation_rows: list[dict[str, object]] = []
    final_hashes: dict[str, str] = {}
    final_sizes: dict[str, int] = {}
    final_paths_by_object_hash: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for obj in instance.objects:
        object_id = obj["md_object_id"]
        decision = decisions[object_id]
        archival_object_id = obj["inventory"]
        master_id = f"master:{archival_object_id}"
        members = plans[object_id]
        member_paths: list[str] = []
        member_hashes: list[str] = []
        member_sources: list[str] = []
        main_member_representation = ""
        for member in members:
            source = Path(str(member["source"]))
            relative = str(member["relative_path"])
            destination = UNIT / relative
            source_digest, size = copy_master(source, destination)
            if source_digest != member["expected_hash"] or size != member["expected_size"]:
                raise AccessionError(f"selected source changed while copying: {source}")
            digest, output_size = hash_file(destination)
            if digest != source_digest or output_size != size:
                raise AccessionError(f"copied Master verification failed: {destination}")
            final_hashes[relative] = digest
            final_sizes[relative] = size
            member_paths.append(relative)
            member_hashes.append(digest)
            member_sources.append(str(source))
            final_paths_by_object_hash[object_id][digest].append(relative)
            representation_id = f"{master_id}:member:{int(member['position']):02d}"
            resource = (
                instance.resources_by_id.get(str(member["resource_id"]))
                if member["resource_id"]
                else None
            )
            if resource and resource["is_main"] == "true":
                main_member_representation = representation_id
            representation_rows.append(
                {
                    "representation_id": representation_id,
                    "master_representation_id": master_id,
                    "archival_object_id": archival_object_id,
                    "md_object_id": object_id,
                    "inventory": obj["inventory"],
                    "role": "selected_master_member",
                    "member_position": member["position"],
                    "relative_path": relative,
                    "original_source_path": str(source),
                    "source_url": member["source_url"],
                    "format": member["format"],
                    "sha256": digest,
                    "size_bytes": size,
                    "page_count": member["page_count"],
                    "width_px": member["width_px"],
                    "height_px": member["height_px"],
                    "derivative_of": "",
                    "derivative_source_member": "",
                    "provenance": "human-selected Master member copied unchanged during accession",
                    "deletion_authorized": "false",
                }
            )
        if not main_member_representation:
            main_member_representation = f"{master_id}:member:01"
        main_position = int(main_member_representation.rsplit(":", 1)[1])
        derivative_source = UNIT / member_paths[main_position - 1]
        derivative_relative = (
            f"export/{safe_name(obj['inventory'])}__md-{object_id}__access.jpg"
        )
        derivative_path = UNIT / derivative_relative
        width, height = generate_derivative(derivative_source, derivative_path)
        pixel_digest = decoded_pixels(derivative_path)
        values = metadata_values(obj, resources_by_object[object_id])
        embed_derivative_metadata(derivative_path, values)
        if decoded_pixels(derivative_path) != pixel_digest:
            raise AccessionError(f"metadata embedding changed derivative pixels: {derivative_path}")
        verify_derivative_metadata(derivative_path, values)
        derivative_hash, derivative_size = hash_file(derivative_path)
        final_hashes[derivative_relative] = derivative_hash
        final_sizes[derivative_relative] = derivative_size
        representation_rows.append(
            {
                "representation_id": f"access:{archival_object_id}",
                "master_representation_id": master_id,
                "archival_object_id": archival_object_id,
                "md_object_id": object_id,
                "inventory": obj["inventory"],
                "role": "generated_access_derivative",
                "member_position": "",
                "relative_path": derivative_relative,
                "original_source_path": str(derivative_source),
                "source_url": obj["object_url"],
                "format": "jpeg",
                "sha256": derivative_hash,
                "size_bytes": derivative_size,
                "page_count": "",
                "width_px": width,
                "height_px": height,
                "derivative_of": master_id,
                "derivative_source_member": main_member_representation,
                "provenance": "generated during accession from the selected Master representation",
                "deletion_authorized": "false",
            }
        )
        master_rows.append(
            {
                "master_representation_id": master_id,
                "archival_object_id": archival_object_id,
                "md_object_id": object_id,
                "inventory": obj["inventory"],
                "representation_type": decision["master_representation_type"],
                "member_count": len(member_paths),
                "member_paths_json": json.dumps(member_paths, ensure_ascii=False, separators=(",", ":")),
                "member_sha256_json": json.dumps(member_hashes, separators=(",", ":")),
                "selected_candidate_id": decision["selected_candidate_id"],
                "original_source_paths_json": json.dumps(
                    member_sources, ensure_ascii=False, separators=(",", ":")
                ),
                "representation_note": decision["representation_note"],
                "decision_note": decision["decision_note"],
                "actor": decision["actor"],
                "decided_at": decision["decided_at"],
                "deletion_authorized": "false",
            }
        )
        catalog_rows.append(
            {
                "archival_object_id": archival_object_id,
                "md_object_id": object_id,
                "inventory": obj["inventory"],
                "title": obj["title"],
                "object_type": obj["object_type"],
                "date": obj["date"],
                "master_representation_id": master_id,
                "master_representation_type": decision["master_representation_type"],
                "master_member_count": len(member_paths),
                "master_member_paths_json": json.dumps(
                    member_paths, ensure_ascii=False, separators=(",", ":")
                ),
                "access_derivative_path": derivative_relative,
                "metadata_authority": "museum-digital descriptive object records",
                "object_url": obj["object_url"],
                "deletion_authorized": "false",
            }
        )

    source_rows: list[dict[str, object]] = []
    for candidate in instance.candidates:
        object_id = candidate["md_object_id"]
        disposition = disposition_by_id[candidate["candidate_id"]]
        matching = final_paths_by_object_hash[object_id].get(
            candidate["known_sha256"].casefold(), []
        )
        selected_anchor = candidate["candidate_id"] == decisions[object_id]["selected_candidate_id"]
        if selected_anchor:
            relationship = "selected_master_representation_anchor"
            drain_state = "retain_selected_master_source"
        elif matching:
            relationship = "redundant_exact_copy_of_selected_master_member"
            drain_state = "proposed_redundant_after_validated_accession"
        else:
            relationship = "redundant_candidate_after_master_selection"
            drain_state = "proposed_redundant_after_validated_accession"
        source_rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "md_object_id": object_id,
                "inventory": candidate["inventory"],
                "source_path": candidate["candidate_path"],
                "source_kind": candidate["source_kind"],
                "candidate_role": candidate["candidate_role"],
                "candidate_sha256": candidate["known_sha256"],
                "size_bytes": candidate["size_bytes"],
                "disposition": disposition["disposition"],
                "relationship": relationship,
                "covered_by_selected_master": "true" if matching else "false",
                "final_archive_paths_json": json.dumps(
                    matching, ensure_ascii=False, separators=(",", ":")
                ),
                "drain_state": drain_state,
                "reason": disposition["reason"],
                "deletion_authorized": "false",
            }
        )
    drain_rows = [
        row
        for row in source_rows
        if row["disposition"] == "redundant"
        and source_is_protected(Path(str(row["source_path"])))
    ]
    exception_paths = {row["source_path"] for row in instance.exceptions}
    selected_source_paths = {
        str(member["source"]) for members in plans.values() for member in members
    }
    if exception_paths & {str(row["source_path"]) for row in drain_rows}:
        raise AccessionError("an integrity exception entered the drain list")
    if selected_source_paths & {str(row["source_path"]) for row in drain_rows}:
        raise AccessionError("a selected Master source entered the drain list")
    if any(row["deletion_authorized"] != "false" for row in source_rows + drain_rows):
        raise AccessionError("source disposition unexpectedly authorizes deletion")

    write_csv(UNIT / "CATALOG.csv", CATALOG_FIELDS, catalog_rows)
    write_csv(UNIT / "00_MANIFESTS/master-representations.csv", MASTER_FIELDS, master_rows)
    write_csv(
        UNIT / "00_MANIFESTS/representations.csv",
        REPRESENTATION_FIELDS,
        representation_rows,
    )
    write_csv(UNIT / "00_MANIFESTS/source-disposition.csv", SOURCE_FIELDS, source_rows)
    write_csv(UNIT / "00_MANIFESTS/drain-list.csv", SOURCE_FIELDS, drain_rows)
    write_csv(UNIT / "00_MANIFESTS/documents.csv", DOCUMENT_FIELDS, [])
    shutil.copyfile(
        workbench_module.RELATIONSHIPS_CSV,
        UNIT / "00_MANIFESTS/object-relationships.csv",
    )
    shutil.copyfile(
        workbench_module.EXCEPTIONS_CSV,
        UNIT / "00_MANIFESTS/integrity-exceptions.csv",
    )
    shutil.copyfile(workbench_module.OBJECTS_CSV, UNIT / "metadata/museum-digital-objects.csv")
    shutil.copyfile(
        workbench_module.RESOURCES_CSV,
        UNIT / "metadata/museum-digital-resources.csv",
    )
    shutil.copyfile(
        workbench_module.DECISIONS_CSV,
        UNIT / "metadata/master-decisions.csv",
    )
    shutil.copyfile(
        workbench_module.SCENARIOS_CSV,
        UNIT / "metadata/preservation-scenarios.csv",
    )

    database_path = UNIT / "db-restore/RESTORE.sqlite"
    quick_check, foreign_keys, dump = create_database(
        database_path,
        catalog_rows,
        master_rows,
        representation_rows,
        source_rows,
        instance.relationships,
        instance.exceptions,
    )
    if quick_check != "ok" or foreign_keys:
        raise AccessionError(
            f"SQLite validation failed: quick_check={quick_check}, foreign_keys={foreign_keys}"
        )
    write_exclusive(UNIT / "db-restore/RESTORE.sql", dump.encode("utf-8"))
    write_csv(UNIT / "db-restore/RESTORE.csv", CATALOG_FIELDS, catalog_rows)

    readme = f"""# Archiv WF — museum-digital collection 553

This unit is the human-readable archival authority for the 54 reconciled
objects in museum-digital collection 553. Each object has exactly one selected
Master representation. Forty-five are single files; nine are ordered compound
representations whose flat member paths and order are recorded in `CATALOG.csv`
and `00_MANIFESTS/master-representations.csv`.

The `masters/` files are byte-for-byte copies of the selected inputs. One JPEG
access derivative per object was generated during accession in `export/`; for a
compound representation it derives from the museum-digital main member.
Descriptive metadata is in `CATALOG.csv`, `metadata/`, SQLite, and embedded in
the generated JPEGs. SQLite is rebuildable support, not the sole authority.

Candidate relationships and provenance are in
`00_MANIFESTS/source-disposition.csv`. `drain-list.csv` contains only redundant
protected-source candidates, excludes selected Masters and integrity holds,
and has `deletion_authorized=false` for every row. No source file was modified
or deleted during accession.
"""
    write_exclusive(UNIT / "00_README.md", readme.encode("utf-8"))

    changed_inputs = [
        str(path)
        for path, before in snapshots.items()
        if not path.is_file() or (path.stat().st_size, path.stat().st_mtime_ns) != before
    ]
    if changed_inputs:
        raise AccessionError("accession input changed during run: " + "; ".join(changed_inputs))
    mount_after = workbench_module.source_guard()
    if mount_after != mount_before:
        raise AccessionError("protected source mount changed during accession")

    actual_dirs = {path.name for path in UNIT.iterdir() if path.is_dir()}
    if actual_dirs != UNIT_DIRS:
        raise AccessionError(f"unit directory structure changed: {sorted(actual_dirs)}")
    if len(catalog_rows) != 54 or len(master_rows) != 54:
        raise AccessionError("unit does not contain exactly 54 object/Master records")
    if Counter(row["role"] for row in representation_rows) != {
        "selected_master_member": 65,
        "generated_access_derivative": 54,
    }:
        raise AccessionError("unexpected final representation counts")
    if any(row["deletion_authorized"] != "false" for row in catalog_rows + master_rows + representation_rows):
        raise AccessionError("final unit unexpectedly carries deletion authority")

    excluded = {
        "00_MANIFESTS/payload-checksums.csv",
        "00_MANIFESTS/accession-completion.json",
    }
    actual_files = {
        path.relative_to(UNIT).as_posix()
        for path in UNIT.rglob("*")
        if path.is_file() and path.relative_to(UNIT).as_posix() not in excluded
    }
    payload_rows: list[dict[str, object]] = []
    for relative in sorted(actual_files):
        path = UNIT / relative
        if relative in final_hashes:
            digest = final_hashes[relative]
            size = final_sizes[relative]
            if path.stat().st_size != size:
                raise AccessionError(f"finalized media size changed before manifest: {relative}")
        else:
            digest, size = hash_file(path)
            final_hashes[relative] = digest
            final_sizes[relative] = size
        payload_rows.append(
            {"relative_path": relative, "sha256": digest, "size_bytes": size}
        )
    write_csv(
        UNIT / "00_MANIFESTS/payload-checksums.csv",
        PAYLOAD_FIELDS,
        payload_rows,
    )
    checksum_manifest_hash, checksum_manifest_size = hash_file(
        UNIT / "00_MANIFESTS/payload-checksums.csv"
    )

    completed_at = utc_now()
    completion = {
        "format": "iss-collection-553-accession-completion-v1",
        "result": "ok",
        "started_at": started_at,
        "completed_at": completed_at,
        "collection_id": 553,
        "unit": str(UNIT),
        "accession_authority": AUTHORITY,
        "objects": len(catalog_rows),
        "master_representations": len(master_rows),
        "master_representation_types": dict(
            sorted(Counter(row["representation_type"] for row in master_rows).items())
        ),
        "master_members": 65,
        "generated_access_derivatives": 54,
        "metadata_embedded_derivatives": 54,
        "source_disposition_rows": len(source_rows),
        "drain_rows": len(drain_rows),
        "integrity_exceptions": len(instance.exceptions),
        "object_relationships": len(instance.relationships),
        "payload_files": len(payload_rows),
        "payload_bytes": sum(int(row["size_bytes"]) for row in payload_rows),
        "payload_checksum_manifest_sha256": checksum_manifest_hash,
        "payload_checksum_manifest_size_bytes": checksum_manifest_size,
        "sqlite_quick_check": quick_check,
        "sqlite_foreign_key_violations": len(foreign_keys),
        "metadata_authority": "museum-digital descriptive object records",
        "source_mount": mount_after,
        "source_files_modified": 0,
        "source_files_deleted": 0,
        "deletion_authorized": False,
    }
    write_json(UNIT / "00_MANIFESTS/accession-completion.json", completion)
    return completion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="run preflight only")
    mode.add_argument("--run", action="store_true", help="create the accession unit")
    arguments = parser.parse_args()
    try:
        if arguments.check:
            result = preflight_module.run_preflight(UNIT_NAME)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["accession_ready"] else 1
        result = run_accession()
    except (
        AccessionError,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
        subprocess.SubprocessError,
        workbench_module.WorkbenchError,
        preflight_module.PreflightError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
