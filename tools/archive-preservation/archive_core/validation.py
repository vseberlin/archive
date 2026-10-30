"""Package and recovery validation independent of user interfaces."""

from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from archive_core import ArchiveError
from archive_core.catalog import LOGGED_TABLES, PROJECTED_TABLES
from archive_core.filesystem import sha256_file
from archive_core.identity import validate_archival_id
from archive_core.metadata import SHA256_RE


def read_catalog_projection(path: Path) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {table: [] for table in PROJECTED_TABLES}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["record_type", "record_id", "unit_id", "payload_json"]:
            raise ArchiveError("catalog.csv header is invalid")
        for row in reader:
            record_type = row["record_type"]
            if record_type not in records:
                raise ArchiveError(f"unknown catalog record type: {record_type}")
            payload = json.loads(row["payload_json"])
            if payload.get("unit_id") != row["unit_id"]:
                raise ArchiveError("catalog row unit mismatch")
            if str(payload.get(LOGGED_TABLES[record_type], "")) != row["record_id"]:
                raise ArchiveError("catalog row identity mismatch")
            records[record_type].append(payload)
    return records

def manifest_digest(path: Path) -> str:
    return sha256_file(path)

def declared_payload_paths(bag: Path) -> set[str]:
    manifest = bag / "manifest-sha256.txt"
    if not manifest.is_file():
        raise ArchiveError(f"payload manifest is missing: {manifest}")
    result: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if len(line) < 67 or not re.fullmatch(r"[0-9a-f]{64}", line[:64]) or line[64:66] not in ("  ", " *"):
            raise ArchiveError(f"invalid payload manifest line: {line!r}")
        result.add(line[66:])
    return result

def actual_payload_paths(bag: Path) -> set[str]:
    data = bag / "data"
    if not data.is_dir():
        return set()
    return {path.relative_to(bag).as_posix() for path in data.rglob("*") if path.is_file()}

def run_checksum_validation(bag: Path) -> None:
    for manifest in ("manifest-sha256.txt", "tagmanifest-sha256.txt"):
        result = subprocess.run(
            ["sha256sum", "-c", manifest],
            cwd=bag,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode != 0:
            raise ArchiveError(f"{manifest} validation failed:\n{result.stdout}")

def run_bagit_validation(bag: Path, validator_python: str) -> None:
    result = subprocess.run(
        [validator_python, "-m", "bagit", "--validate", str(bag)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise ArchiveError(f"BagIt-Python validation failed:\n{result.stdout}")

def validate_internal(bag: Path) -> dict[str, Any]:
    required = (
        "bagit.txt",
        "bag-info.txt",
        "manifest-sha256.txt",
        "tagmanifest-sha256.txt",
        "README.md",
        "data/catalog.csv",
        "data/identifiers.csv",
        "data/logbook.jsonl",
    )
    for relative in required:
        if not (bag / relative).is_file():
            raise ArchiveError(f"required bag file is missing: {relative}")
    records = read_catalog_projection(bag / "data" / "catalog.csv")
    units = records["units"]
    bag_info: dict[str, str] = {}
    for line in (bag / "bag-info.txt").read_text(encoding="utf-8").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            bag_info[key] = value
    if len(units) != 1 or units[0]["unit_id"] != bag_info.get("External-Identifier"):
        raise ArchiveError("bag must contain exactly one unit matching External-Identifier")
    entities: dict[str, set[str]] = {
        "object": {row["object_id"] for row in records["objects"]},
        "event": {row["event_id"] for row in records["events"]},
        "testimony": {row["testimony_id"] for row in records["testimonies"]},
        "source_file": {row["source_file_id"] for row in records["source_files"]},
    }
    for object_id in entities["object"]:
        if not validate_archival_id(object_id) or not object_id.startswith("ISS-A-"):
            raise ArchiveError(f"invalid object ID: {object_id}")
    for event_id in entities["event"]:
        if not validate_archival_id(event_id) or not event_id.startswith("ISS-E-"):
            raise ArchiveError(f"invalid event ID: {event_id}")
    referenced_payloads: set[str] = set()
    file_ids: dict[str, list[str]] = {}
    for row in records["source_files"]:
        file_id = row["file_id"]
        archive_path = row["archive_path"]
        if not SHA256_RE.fullmatch(file_id):
            raise ArchiveError(f"invalid file ID: {file_id}")
        if not archive_path.startswith(("data/masters/", "data/sources/")):
            raise ArchiveError(f"invalid archive path: {archive_path}")
        payload = bag / Path(*PurePosixPath(archive_path).parts)
        if not payload.is_file() or payload.is_symlink():
            raise ArchiveError(f"catalog payload is missing or unsafe: {archive_path}")
        digest = sha256_file(payload)
        if digest != file_id.removeprefix("sha256:") or payload.stat().st_size != row["size_bytes"]:
            raise ArchiveError(f"catalog payload fixity mismatch: {archive_path}")
        referenced_payloads.add(archive_path)
        file_ids.setdefault(file_id, []).append(archive_path)
    actual_evidence = {
        path.relative_to(bag).as_posix()
        for folder in (bag / "data" / "masters", bag / "data" / "sources")
        if folder.exists()
        for path in folder.rglob("*")
        if path.is_file()
    }
    if actual_evidence != referenced_payloads:
        raise ArchiveError(
            f"catalog/payload mismatch: missing={sorted(actual_evidence - referenced_payloads)} "
            f"unexpected={sorted(referenced_payloads - actual_evidence)}"
        )
    for relationship in records["relationships"]:
        if relationship["subject_id"] not in entities[relationship["subject_type"]]:
            raise ArchiveError(f"relationship subject does not resolve: {relationship['relationship_id']}")
        if relationship["object_id"] not in entities[relationship["object_type"]]:
            raise ArchiveError(f"relationship object does not resolve: {relationship['relationship_id']}")
    with (bag / "data" / "identifiers.csv").open("r", encoding="utf-8", newline="") as handle:
        identifiers = list(csv.DictReader(handle))
    for identifier in identifiers:
        if identifier["entity_id"] not in entities[identifier["entity_type"]]:
            raise ArchiveError(f"identifier entity does not resolve: {identifier['identifier_id']}")
    duplicate_occurrences = sum(max(0, len(paths) - 1) for paths in file_ids.values())
    return {
        "unit_id": units[0]["unit_id"],
        "objects": len(records["objects"]),
        "events": len(records["events"]),
        "testimonies": len(records["testimonies"]),
        "source_files": len(records["source_files"]),
        "duplicate_occurrences": duplicate_occurrences,
        "payload_bytes": sum((bag / Path(path)).stat().st_size for path in referenced_payloads),
    }

def validate_bag(bag: Path, validator_python: str) -> dict[str, Any]:
    bag = bag.resolve()
    if not bag.is_dir():
        raise ArchiveError(f"bag does not exist: {bag}")
    run_checksum_validation(bag)
    run_bagit_validation(bag, validator_python)
    result = validate_internal(bag)
    result.update(
        {
            "result": "pass",
            "bag": str(bag),
            "manifest_sha256": manifest_digest(bag / "manifest-sha256.txt"),
            "tagmanifest_sha256": manifest_digest(bag / "tagmanifest-sha256.txt"),
        }
    )
    return result
