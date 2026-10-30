#!/usr/bin/env python3
"""Assign canonical ISS IDs and build work-only provenance projections.

The operation reads the published Kurt-Schwarz unit, its append-only alias
decision, immutable museum-digital captures for collections 783 and 555, and
the existing archive index. It writes only a new review run below
``/mnt/volume/iss-archive-work``. No media file is opened, moved, renamed, or
rewritten.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
import catalog_core  # noqa: E402


FORMAT = "iss-kurt-schwarz-identity-provenance-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work")
FINAL_UNITS_ROOT = Path("/mnt/volume/iss-archive/units")
ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
KURT_UNIT_ID = "kurt-schwarz"
PRESSE_UNIT_ID = "pressebildarchiv-wf"
CANONICAL_ID_RE = re.compile(
    rb"ISS-[0-9]{4}-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}-"
    rb"[0-9ABCDEFGHJKMNPQRSTVWXYZ]"
)
KURT_SUBDIVISION_IDS = {str(value) for value in range(846, 857)}


class IdentityProvenanceError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def require_under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise IdentityProvenanceError(
            f"{label} escaped allowed root {root}: {resolved}"
        ) from error
    return resolved


def require_file(path: Path, root: Path, *, label: str) -> Path:
    resolved = require_under(path, root, label=label)
    if not resolved.is_file():
        raise IdentityProvenanceError(f"{label} does not exist: {resolved}")
    return resolved


def require_capture(path: Path) -> Path:
    resolved = require_under(path, WORK_ROOT, label="museum-digital capture")
    for name in ("coverage.json", "objects.csv", "request-manifest.csv"):
        if not (resolved / name).is_file():
            raise IdentityProvenanceError(
                f"museum-digital capture lacks {name}: {resolved}"
            )
    if not (resolved / "raw").is_dir():
        raise IdentityProvenanceError(
            f"museum-digital capture lacks raw pages: {resolved}"
        )
    return resolved


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_capture(path: Path, expected_query: str) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    coverage = json.loads((path / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("format") != "iss-md-export-discovery-v1":
        raise IdentityProvenanceError(f"unsupported capture format: {path}")
    if coverage.get("query") != expected_query:
        raise IdentityProvenanceError(
            f"capture query is not {expected_query}: {path}"
        )
    if int(coverage.get("request_failures") or 0):
        raise IdentityProvenanceError(f"capture has request failures: {path}")
    rows = read_csv(path / "objects.csv")
    ids = [row["object_id"] for row in rows]
    if len(rows) != int(coverage.get("captured_objects") or -1):
        raise IdentityProvenanceError(f"capture row count differs: {path}")
    if len(ids) != len(set(ids)):
        raise IdentityProvenanceError(f"capture has duplicate object IDs: {path}")
    objects: list[dict[str, Any]] = []
    for raw_path in sorted((path / "raw").glob("*.json")):
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        result = payload.get("results") if isinstance(payload, dict) else None
        raw_objects = result.get("objects") if isinstance(result, dict) else None
        if isinstance(raw_objects, dict):
            objects.extend(
                row for row in raw_objects.values() if isinstance(row, dict)
            )
        elif isinstance(raw_objects, list):
            objects.extend(row for row in raw_objects if isinstance(row, dict))
    raw_ids = [clean(row.get("object_id")) for row in objects]
    if raw_ids != ids:
        if set(raw_ids) != set(ids) or len(raw_ids) != len(ids):
            raise IdentityProvenanceError(
                f"raw capture and objects.csv IDs differ: {path}"
            )
    return coverage, rows, objects


def canonical_record_map(
    record_keys: Iterable[str],
    alias_rows: Iterable[dict[str, str]],
) -> dict[str, str]:
    keys = set(record_keys)
    direct: dict[str, str] = {}
    for row in alias_rows:
        if row["action"] != "same_object_alias":
            continue
        alias = row["alias_record_key"]
        canonical = row["canonical_record_key"]
        if alias not in keys or canonical not in keys:
            raise IdentityProvenanceError(
                f"alias decision references unknown record: {alias} -> {canonical}"
            )
        if alias in direct and direct[alias] != canonical:
            raise IdentityProvenanceError(f"conflicting alias decisions: {alias}")
        direct[alias] = canonical
    result: dict[str, str] = {}
    for key in keys:
        seen: set[str] = set()
        current = key
        while current in direct:
            if current in seen:
                raise IdentityProvenanceError(f"alias cycle contains {current}")
            seen.add(current)
            current = direct[current]
        if len(seen) > 1:
            raise IdentityProvenanceError(
                f"alias chains are not allowed: {key} -> {current}"
            )
        result[key] = current
    return result


def existing_entity_ids() -> set[str]:
    paths: list[Path] = []
    if FINAL_UNITS_ROOT.is_dir():
        for unit in FINAL_UNITS_ROOT.iterdir():
            if not unit.is_dir():
                continue
            for relative in (
                "README.md",
                "CATALOG.csv",
                "db-restore/RESTORE.csv",
            ):
                path = unit / relative
                if path.is_file():
                    paths.append(path)
    found: set[str] = set()
    for path in paths:
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                found.update(match.decode("ascii") for match in CANONICAL_ID_RE.findall(chunk))
    return found


def allocate_entity_ids(
    canonical_keys: Iterable[str],
    *,
    year: int,
    reserved: set[str],
) -> dict[str, str]:
    assigned: dict[str, str] = {}
    occupied = set(reserved)
    for key in sorted(set(canonical_keys)):
        for _ in range(1000):
            body = "".join(secrets.choice(catalog_core.CROCKFORD) for _ in range(4))
            check = catalog_core.entity_id_check_symbol(year, body)
            candidate = f"ISS-{year:04d}-{body}-{check}"
            if candidate not in occupied:
                occupied.add(candidate)
                assigned[key] = candidate
                break
        else:
            raise IdentityProvenanceError("could not allocate a unique entity ID")
    return assigned


def read_reusable_entity_ids(
    path: Path,
    canonical_keys: set[str],
) -> dict[str, str]:
    rows = read_csv(path)
    mapping = {
        row["canonical_record_key"]: row["entity_id"]
        for row in rows
    }
    if set(mapping) != canonical_keys or len(mapping) != len(rows):
        raise IdentityProvenanceError(
            "reusable entity map does not match the current canonical key set"
        )
    if len(set(mapping.values())) != len(mapping):
        raise IdentityProvenanceError("reusable entity map contains duplicate IDs")
    if not all(catalog_core.validate_entity_id(value) for value in mapping.values()):
        raise IdentityProvenanceError("reusable entity map contains an invalid ID")
    return mapping


def split_values(value: str) -> list[str]:
    return [clean(part) for part in value.split("; ") if clean(part)]


def filename_from_path(value: str) -> str:
    return PurePosixPath(value).name if value else ""


def write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
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


def collection_role(collection_id: str) -> tuple[str, str]:
    if collection_id == "783":
        return "kurt_schwarz_unit_scope", ""
    if collection_id in KURT_SUBDIVISION_IDS:
        return "kurt_schwarz_subdivision", "783"
    if collection_id == "555":
        return "pressebildarchiv_wf_unit_scope", ""
    return "external_collection_membership", ""


def build(args: argparse.Namespace) -> dict[str, Any]:
    output = require_under(args.output, WORK_ROOT, label="output")
    if output.exists():
        raise IdentityProvenanceError(f"refusing to replace output: {output}")
    kurt_unit = require_under(args.kurt_unit, FINAL_UNITS_ROOT, label="Kurt unit")
    kurt_catalog_path = require_file(
        kurt_unit / "CATALOG.csv", FINAL_UNITS_ROOT, label="Kurt catalog"
    )
    representations_path = require_file(
        kurt_unit / "db-restore/manifests/representations.csv",
        FINAL_UNITS_ROOT,
        label="Kurt representations",
    )
    reconciliation = require_file(
        args.reconciliation,
        WORK_ROOT,
        label="Kurt reconciliation database",
    )
    capture_783 = require_capture(args.capture_783)
    capture_555 = require_capture(args.capture_555)
    if args.archive_index.expanduser().resolve() != ARCHIVE_INDEX.resolve():
        raise IdentityProvenanceError(
            f"archive index must be the approved read-only index: {ARCHIVE_INDEX}"
        )
    if not ARCHIVE_INDEX.is_file():
        raise IdentityProvenanceError(f"archive index does not exist: {ARCHIVE_INDEX}")

    coverage_783, capture_rows_783, raw_783 = read_capture(
        capture_783, "collection:783"
    )
    coverage_555, capture_rows_555, raw_555 = read_capture(
        capture_555, "collection:555"
    )
    if len(capture_rows_555) != 48:
        raise IdentityProvenanceError(
            f"collection 555 expected 48 current objects, found {len(capture_rows_555)}"
        )

    kurt_rows = read_csv(kurt_catalog_path)
    if len(kurt_rows) != 1669:
        raise IdentityProvenanceError(
            f"published Kurt catalog expected 1669 rows, found {len(kurt_rows)}"
        )
    kurt_by_key = {row["record_key"]: row for row in kurt_rows}
    if len(kurt_by_key) != len(kurt_rows):
        raise IdentityProvenanceError("published Kurt catalog has duplicate record keys")

    connection = sqlite3.connect(f"file:{reconciliation}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IdentityProvenanceError("Kurt reconciliation database is not intact")
        alias_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM object_alias_decisions ORDER BY decision_id"
            )
        ]
    finally:
        connection.close()
    record_to_canonical = canonical_record_map(kurt_by_key, alias_rows)
    for row in capture_rows_555:
        key = f"md:{row['object_id']}"
        record_to_canonical.setdefault(key, key)

    canonical_keys = set(record_to_canonical.values())
    reserved_ids = existing_entity_ids()
    reuse_entity_map = None
    if args.reuse_entity_map:
        reuse_entity_map = require_file(
            args.reuse_entity_map,
            WORK_ROOT,
            label="reusable entity map",
        )
        entity_ids = read_reusable_entity_ids(reuse_entity_map, canonical_keys)
    else:
        entity_ids = allocate_entity_ids(
            canonical_keys,
            year=args.assignment_year,
            reserved=reserved_ids,
        )
    record_to_entity = {
        key: entity_ids[canonical]
        for key, canonical in record_to_canonical.items()
    }
    if len(entity_ids) != 1715:
        raise IdentityProvenanceError(
            f"expected 1715 canonical entities, found {len(entity_ids)}"
        )
    if not all(catalog_core.validate_entity_id(value) for value in entity_ids.values()):
        raise IdentityProvenanceError("generated entity ID failed validation")

    capture_555_by_id = {row["object_id"]: row for row in capture_rows_555}
    current_md_ids = {row["object_id"] for row in capture_rows_783} | set(
        capture_555_by_id
    )
    created_at = utc_now()
    entities: list[dict[str, str]] = []
    identifiers: list[dict[str, str]] = []
    identifier_seen: set[tuple[str, str, str]] = set()

    canonical_sources: dict[str, list[dict[str, str]]] = defaultdict(list)
    for key, row in kurt_by_key.items():
        canonical_sources[record_to_canonical[key]].append(row)
    for md_id, row in capture_555_by_id.items():
        key = f"md:{md_id}"
        if key not in kurt_by_key:
            canonical_sources[key].append(
                {
                    "record_key": key,
                    "record_state": "current_md_metadata_only_collection_555",
                    "md_object_id": md_id,
                    "current_inventory": row["inventory"],
                    "historical_inventory": "",
                    "current_title": row["title"],
                    "historical_title": "",
                }
            )

    for canonical_key in sorted(canonical_keys):
        source_rows = canonical_sources[canonical_key]
        label = next(
            (
                clean(row.get(field))
                for field in (
                    "current_inventory",
                    "historical_inventory",
                    "current_title",
                    "historical_title",
                    "record_key",
                )
                for row in source_rows
                if clean(row.get(field))
            ),
            canonical_key,
        )
        entity_id = entity_ids[canonical_key]
        units = [KURT_UNIT_ID] if canonical_key in canonical_sources and any(
            row.get("record_key") in kurt_by_key for row in source_rows
        ) else []
        if any(row.get("md_object_id") in capture_555_by_id for row in source_rows):
            units.append(PRESSE_UNIT_ID)
        entities.append(
            {
                "entity_id": entity_id,
                "kind": "archive_object",
                "status": "active",
                "canonical_record_key": canonical_key,
                "archival_label": label,
                "assignment_year": str(args.assignment_year),
                "unit_ids": "; ".join(sorted(set(units))),
                "created_at": created_at,
                "identity_basis": "guarded assignment after current MD capture and curator alias resolution",
            }
        )
        for row in source_rows:
            record_key = clean(row.get("record_key"))
            values: list[tuple[str, str, str, str]] = []
            if record_key:
                values.append(("record-key", record_key, "legacy", "active"))
            md_id = clean(row.get("md_object_id"))
            if md_id:
                values.append(
                    (
                        "museum-digital",
                        md_id,
                        "external",
                        "active" if md_id in current_md_ids else "deprecated",
                    )
                )
            for value in split_values(clean(row.get("current_inventory"))):
                values.append(("inventory", value, "accession", "active"))
            for value in split_values(clean(row.get("historical_inventory"))):
                values.append(("inventory", value, "legacy", "deprecated"))
            for namespace, value, role, status in values:
                normalized = value.casefold()
                key = (entity_id, namespace, normalized)
                if key in identifier_seen:
                    continue
                identifier_seen.add(key)
                identifiers.append(
                    {
                        "entity_id": entity_id,
                        "namespace": namespace,
                        "value": value,
                        "normalized_value": normalized,
                        "role": role,
                        "status": status,
                        "source_record_key": record_key,
                    }
                )

    inventory_entities: dict[str, set[str]] = defaultdict(set)
    for row in identifiers:
        if row["namespace"] == "inventory" and row["status"] == "active":
            inventory_entities[row["normalized_value"]].add(row["entity_id"])
    disputed_inventories = {
        value for value, ids in inventory_entities.items() if len(ids) > 1
    }
    for row in identifiers:
        if (
            row["namespace"] == "inventory"
            and row["normalized_value"] in disputed_inventories
        ):
            row["status"] = "disputed"

    unit_memberships: list[dict[str, str]] = []
    kurt_entities = {
        record_to_entity[key] for key in kurt_by_key
    }
    for entity_id in sorted(kurt_entities):
        unit_memberships.append(
            {
                "entity_id": entity_id,
                "unit_id": KURT_UNIT_ID,
                "role": "archival_object",
                "status": "accepted",
                "basis": "published Kurt Schwarz unit object or resolved alias",
            }
        )
    for row in capture_rows_555:
        unit_memberships.append(
            {
                "entity_id": record_to_entity[f"md:{row['object_id']}"],
                "unit_id": PRESSE_UNIT_ID,
                "role": "collection_scope_object",
                "status": "accepted",
                "basis": "current museum-digital collection 555 membership",
            }
        )

    collection_observations: list[dict[str, str]] = []
    collection_register: dict[str, dict[str, str]] = {}
    for capture_path, coverage, raw_objects in (
        (capture_783, coverage_783, raw_783),
        (capture_555, coverage_555, raw_555),
    ):
        source_ref = str(capture_path / "request-manifest.csv")
        observed_at = clean(coverage.get("created_at"))
        for obj in raw_objects:
            md_id = clean(obj.get("object_id"))
            entity_id = record_to_entity.get(f"md:{md_id}")
            if not entity_id:
                raise IdentityProvenanceError(
                    f"capture object lacks assigned entity: {md_id}"
                )
            for collection in obj.get("object_collection", []):
                if not isinstance(collection, dict):
                    continue
                collection_id = clean(collection.get("collection_id"))
                label = clean(collection.get("collection_name"))
                role, parent = collection_role(collection_id)
                collection_register.setdefault(
                    collection_id,
                    {
                        "source_scheme": "museum-digital-collection",
                        "source_identifier": collection_id,
                        "source_label": label,
                        "source_role": role,
                        "parent_source_identifier": parent,
                    },
                )
                assertion_key = "\0".join(
                    (entity_id, md_id, collection_id, source_ref)
                ).encode("utf-8")
                collection_observations.append(
                    {
                        "assertion_id": "COL-" + hashlib.sha256(assertion_key).hexdigest()[:20].upper(),
                        "entity_id": entity_id,
                        "source_object_id": md_id,
                        "relationship": "member_of_external_collection",
                        "source_scheme": "museum-digital-collection",
                        "source_identifier": collection_id,
                        "source_label": label,
                        "source_role": role,
                        "status": "current",
                        "source_ref": source_ref,
                        "observed_at": observed_at,
                        "basis": "explicit current museum-digital object_collection relationship",
                    }
                )
    observed_kurt_subdivisions = {
        row["source_identifier"]
        for row in collection_observations
        if row["source_role"] == "kurt_schwarz_subdivision"
    }
    if observed_kurt_subdivisions != KURT_SUBDIVISION_IDS:
        raise IdentityProvenanceError(
            "current Kurt subdivision IDs differ from expected 846 through 856"
        )

    file_provenance: list[dict[str, str]] = []
    representations = read_csv(representations_path)
    unit_evidence_representations = 0
    representation_entities: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in representations:
        record_keys = split_values(row["record_keys"])
        if not record_keys:
            unit_evidence_representations += 1
            continue
        entity_keys: dict[str, str] = {}
        for record_key in record_keys:
            if record_key not in record_to_entity:
                raise IdentityProvenanceError(
                    f"representation references unknown record: {record_key}"
                )
            entity_keys[record_to_entity[record_key]] = record_key
        for entity_id, record_key in sorted(entity_keys.items()):
            final_path = row["final_relative_path"]
            item = {
                "entity_id": entity_id,
                "unit_id": KURT_UNIT_ID,
                "source_record_key": record_key,
                "representation_key": row["representation_key"],
                "role": row["role"],
                "historical_filename": filename_from_path(final_path),
                "final_relative_path": final_path,
                "source_path": row["source_path"],
                "source_member": row["source_member"],
                "source_filename": filename_from_path(
                    row["source_member"] or row["source_path"]
                ),
                "final_sha256": row["final_sha256"],
                "size_bytes": row["size_bytes"],
                "linkage_basis": "existing published representation record; filename unchanged",
            }
            file_provenance.append(item)
            representation_entities[entity_id].append(item)

    index = sqlite3.connect(
        f"file:{ARCHIVE_INDEX}?mode=ro&immutable=1", uri=True
    )
    index.row_factory = sqlite3.Row
    local_candidates: list[dict[str, str]] = []
    coverage_rows: list[dict[str, str]] = []
    try:
        for row in capture_rows_555:
            md_id = row["object_id"]
            inventory = row["inventory"]
            entity_id = record_to_entity[f"md:{md_id}"]
            preserved = [
                item
                for item in representation_entities.get(entity_id, [])
                if item["role"] in {
                    "preservation_master",
                    "preservation_source_tiff",
                    "preservation_source_variant",
                }
            ]
            candidates = index.execute(
                """SELECT path, basename, stem, extension, size
                   FROM files
                   WHERE stem GLOB ?
                   ORDER BY path""",
                (inventory + "*",),
            ).fetchall()
            exact_count = 0
            prefix_count = 0
            for candidate in candidates:
                state = (
                    "exact_inventory_stem_candidate"
                    if candidate["stem"] == inventory
                    else "inventory_prefix_candidate"
                )
                if state == "exact_inventory_stem_candidate":
                    exact_count += 1
                else:
                    prefix_count += 1
                local_candidates.append(
                    {
                        "entity_id": entity_id,
                        "md_object_id": md_id,
                        "inventory": inventory,
                        "candidate_state": state,
                        "candidate_path": candidate["path"],
                        "historical_filename": candidate["basename"],
                        "extension": candidate["extension"],
                        "size_bytes": str(candidate["size"]),
                        "evidence_basis": "read-only existing archive index filename search; not accepted membership",
                    }
                )
            if preserved:
                coverage_state = "preserved_shared_kurt_schwarz_representation"
                basis = "same canonical entity already has a published Kurt preservation representation"
            elif exact_count:
                coverage_state = "local_exact_filename_candidate_review_required"
                basis = "existing archive index has an exact inventory-stem candidate; identity and role unreviewed"
            elif prefix_count:
                coverage_state = "local_prefix_candidate_review_required"
                basis = "existing archive index has only inventory-prefix candidates; identity and role unreviewed"
            else:
                coverage_state = "no_local_candidate_in_existing_index"
                basis = "no exact or prefix inventory filename was found in the existing archive index"
            coverage_rows.append(
                {
                    "entity_id": entity_id,
                    "md_object_id": md_id,
                    "inventory": inventory,
                    "title": row["title"],
                    "coverage_state": coverage_state,
                    "preserved_representation_count": str(len(preserved)),
                    "exact_candidate_count": str(exact_count),
                    "prefix_candidate_count": str(prefix_count),
                    "basis": basis,
                    "large_file_action": "none",
                }
            )
    finally:
        index.close()

    output.mkdir(parents=True)
    fields = {
        "entities.csv": (
            "entity_id", "kind", "status", "canonical_record_key",
            "archival_label", "assignment_year", "unit_ids", "created_at",
            "identity_basis",
        ),
        "identifiers.csv": (
            "entity_id", "namespace", "value", "normalized_value", "role",
            "status", "source_record_key",
        ),
        "unit-memberships.csv": (
            "entity_id", "unit_id", "role", "status", "basis",
        ),
        "collection-register.csv": (
            "source_scheme", "source_identifier", "source_label", "source_role",
            "parent_source_identifier",
        ),
        "collection-memberships.csv": (
            "assertion_id", "entity_id", "source_object_id", "relationship",
            "source_scheme", "source_identifier", "source_label", "source_role",
            "status", "source_ref", "observed_at", "basis",
        ),
        "file-provenance.csv": (
            "entity_id", "unit_id", "source_record_key", "representation_key",
            "role", "historical_filename", "final_relative_path", "source_path",
            "source_member", "source_filename", "final_sha256", "size_bytes",
            "linkage_basis",
        ),
        "pressebildarchiv-wf-objects.csv": (
            "entity_id", "md_object_id", "inventory", "title", "coverage_state",
            "preserved_representation_count", "exact_candidate_count",
            "prefix_candidate_count", "basis", "large_file_action",
        ),
        "pressebildarchiv-wf-local-candidates.csv": (
            "entity_id", "md_object_id", "inventory", "candidate_state",
            "candidate_path", "historical_filename", "extension", "size_bytes",
            "evidence_basis",
        ),
    }
    data = {
        "entities.csv": sorted(entities, key=lambda row: row["entity_id"]),
        "identifiers.csv": sorted(
            identifiers,
            key=lambda row: (row["entity_id"], row["namespace"], row["normalized_value"]),
        ),
        "unit-memberships.csv": sorted(
            unit_memberships, key=lambda row: (row["unit_id"], row["entity_id"])
        ),
        "collection-register.csv": sorted(
            collection_register.values(), key=lambda row: int(row["source_identifier"])
        ),
        "collection-memberships.csv": sorted(
            collection_observations,
            key=lambda row: (row["entity_id"], int(row["source_identifier"]), row["source_ref"]),
        ),
        "file-provenance.csv": sorted(
            file_provenance,
            key=lambda row: (row["entity_id"], row["final_relative_path"], row["role"]),
        ),
        "pressebildarchiv-wf-objects.csv": sorted(
            coverage_rows, key=lambda row: int(row["md_object_id"])
        ),
        "pressebildarchiv-wf-local-candidates.csv": sorted(
            local_candidates,
            key=lambda row: (int(row["md_object_id"]), row["candidate_path"]),
        ),
    }
    for filename, rows in data.items():
        write_csv(output / filename, fields[filename], rows)

    database_path = output / "identity-provenance.sqlite"
    database = sqlite3.connect(database_path)
    try:
        for filename, table in (
            ("entities.csv", "entities"),
            ("identifiers.csv", "identifiers"),
            ("unit-memberships.csv", "unit_memberships"),
            ("collection-register.csv", "collection_register"),
            ("collection-memberships.csv", "collection_memberships"),
            ("file-provenance.csv", "file_provenance"),
            ("pressebildarchiv-wf-objects.csv", "pressebildarchiv_wf_objects"),
            ("pressebildarchiv-wf-local-candidates.csv", "pressebildarchiv_wf_local_candidates"),
        ):
            columns = fields[filename]
            database.execute(
                f'CREATE TABLE "{table}" ('
                + ", ".join(f'"{column}" TEXT NOT NULL' for column in columns)
                + ")"
            )
            placeholders = ",".join("?" for _ in columns)
            database.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})',
                ([str(row.get(column, "")) for column in columns] for row in data[filename]),
            )
        database.execute("CREATE UNIQUE INDEX entities_id_unique ON entities(entity_id)")
        database.execute(
            "CREATE UNIQUE INDEX collection_assertion_unique ON collection_memberships(assertion_id)"
        )
        database.execute(
            "CREATE INDEX collection_filter ON collection_memberships(source_identifier, status, entity_id)"
        )
        database.execute(
            "CREATE INDEX identifier_lookup ON identifiers(namespace, normalized_value, status)"
        )
        database.commit()
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IdentityProvenanceError("generated SQLite integrity check failed")
    finally:
        database.close()

    coverage_counts = Counter(row["coverage_state"] for row in coverage_rows)
    summary = {
        "format": FORMAT,
        "created_at": created_at,
        "assignment_year": args.assignment_year,
        "canonical_entities": len(entities),
        "kurt_published_rows": len(kurt_rows),
        "kurt_canonical_entities": len(kurt_entities),
        "alias_decisions_applied": len(alias_rows),
        "collection_783_objects": len(capture_rows_783),
        "collection_555_objects": len(capture_rows_555),
        "shared_entities_between_units": len(
            kurt_entities
            & {
                record_to_entity[f"md:{row['object_id']}"]
                for row in capture_rows_555
            }
        ),
        "kurt_subdivision_count": len(observed_kurt_subdivisions),
        "file_provenance_rows": len(file_provenance),
        "unit_evidence_representations_not_object_linked": unit_evidence_representations,
        "collection_555_coverage": dict(sorted(coverage_counts.items())),
        "collection_555_local_candidate_rows": len(local_candidates),
        "disputed_active_inventory_values": len(disputed_inventories),
        "existing_reserved_entity_ids": len(reserved_ids),
        "identity_reused_from": str(reuse_entity_map or ""),
        "large_files_moved": 0,
        "media_files_opened": 0,
        "media_files_rewritten": 0,
        "final_unit_writes": 0,
        "inputs": {
            "kurt_catalog": {
                "path": str(kurt_catalog_path),
                "sha256": sha256_file(kurt_catalog_path),
            },
            "kurt_representations": {
                "path": str(representations_path),
                "sha256": sha256_file(representations_path),
            },
            "kurt_reconciliation": {
                "path": str(reconciliation),
                "sha256": sha256_file(reconciliation),
            },
            "capture_783": {
                "path": str(capture_783),
                "coverage_sha256": sha256_file(capture_783 / "coverage.json"),
                "request_manifest_sha256": sha256_file(capture_783 / "request-manifest.csv"),
            },
            "capture_555": {
                "path": str(capture_555),
                "coverage_sha256": sha256_file(capture_555 / "coverage.json"),
                "request_manifest_sha256": sha256_file(capture_555 / "request-manifest.csv"),
            },
            "archive_index": {
                "path": str(ARCHIVE_INDEX),
                "mode": "read_only_immutable",
                "sha256": "not_rehashed_large_existing_index",
            },
        },
    }
    if reuse_entity_map:
        summary["inputs"]["reusable_entity_map"] = {
            "path": str(reuse_entity_map),
            "sha256": sha256_file(reuse_entity_map),
        }
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    readme = f"""# Kurt Schwarz and Pressebildarchiv WF identity/provenance review

Work-only identity assignment and provenance projection. No master, derivative,
source file, historical filename, or final unit was moved, renamed, opened, or
rewritten by this run.

- Canonical entities: {len(entities)}
- Kurt Schwarz canonical entities: {len(kurt_entities)} from 1,669 published rows
- Applied alias decisions: {len(alias_rows)}
- museum-digital collection 783 objects: {len(capture_rows_783)}
- museum-digital collection 555 objects: {len(capture_rows_555)}
- Shared canonical entities: {summary['shared_entities_between_units']}
- Kurt museum-digital subdivisions: {len(observed_kurt_subdivisions)} (846-856)

`entities.csv` contains the canonical `ISS-YYYY-XXXX-C` identifiers.
`identifiers.csv` keeps museum-digital IDs, inventories, and legacy record keys
as aliases. `file-provenance.csv` preserves the existing filenames and paths;
canonical IDs do not replace filenames. `collection-memberships.csv` is the
queryable provenance chain used for filtering, not a local taxonomy.

Collection 555 is represented as the separate unit `{PRESSE_UNIT_ID}`. Existing
archive-index filename matches are candidates only. They are not accepted
representations and did not authorize a copy or move. Objects without local
coverage remain explicit gaps for later bounded review.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")

    checksum_rows: list[dict[str, str]] = []
    for path in sorted(item for item in output.iterdir() if item.is_file()):
        if path.name == "checksums.csv":
            continue
        checksum_rows.append(
            {
                "filename": path.name,
                "sha256": sha256_file(path),
                "size_bytes": str(path.stat().st_size),
            }
        )
    write_csv(
        output / "checksums.csv",
        ("filename", "sha256", "size_bytes"),
        checksum_rows,
    )
    return {"result": "ok", "output": str(output), **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kurt-unit",
        type=Path,
        default=FINAL_UNITS_ROOT / KURT_UNIT_ID,
    )
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--capture-783", type=Path, required=True)
    parser.add_argument("--capture-555", type=Path, required=True)
    parser.add_argument("--archive-index", type=Path, default=ARCHIVE_INDEX)
    parser.add_argument("--assignment-year", type=int, default=2026)
    parser.add_argument("--reuse-entity-map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    result = build(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
