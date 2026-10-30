#!/usr/bin/env python3
"""Build the proposal-only TFA reconciliation projection from existing evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-tfa-reconciliation-proposal-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive/WF")
FINAL_UNIT = Path("/mnt/volume/iss-archive/units/tfa")
ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
RESTORE_INVENTORY = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore/restore-inventory.csv"
)
PUBLISHED_UNITS = ("kurt-schwarz", "paragon", "trafo", "wf-sender")
EXPECTED_COLLECTION_IDS = {"554", "611", "767", "768", "769", "771", "772", "773", "857"}
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg", ".psd", ".png"}
METADATA_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xml", ".json", ".lido"}
INVENTORY_RE = re.compile(
    r"(?i)(TFA(?:-(?:FOT|NEG|KB))?[-_ ]?[0-9][0-9A-Za-z._-]*)"
)

SUBDIVISION_FIELDS = (
    "scheme_id",
    "subdivision_id",
    "parent_subdivision_id",
    "label",
    "subdivision_type",
    "source_path",
    "assigned_object_count",
    "assignment_state",
    "notes",
)
MEMBERSHIP_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "scheme_id",
    "subdivision_id",
    "label",
    "membership_basis",
)
OBJECT_FIELDS = (
    "record_key",
    "md_object_id",
    "inventory",
    "title",
    "description",
    "date_text",
    "object_type",
    "material",
    "dimensions",
    "places",
    "tags",
    "creator",
    "md_url",
    "collection_ids",
    "collections",
    "subdivision_ids",
    "subdivisions",
    "last_updated",
    "record_state",
    "decision_state",
)
CANDIDATE_FIELDS = (
    "candidate_id",
    "record_key",
    "inventory",
    "source_path",
    "source_kind",
    "original_index_path",
    "basename",
    "extension",
    "size_bytes",
    "mtime_ns",
    "file_kind",
    "proposed_role",
    "match_basis",
    "reused_source_sha256",
    "prior_unit",
    "prior_disposition",
)
DISPOSITION_FIELDS = (
    "source_path",
    "source_kind",
    "unit",
    "inventory",
    "source_role",
    "final_path",
    "preservation_status",
    "preservation_basis",
    "source_sha256",
    "final_sha256",
    "source_size_bytes",
    "exact_copy_proven",
    "drain_state",
    "deletion_authorized",
    "notes",
)


class PrepareError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise PrepareError(f"path escaped approved root: {resolved}") from error
    return resolved


def write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    if path.exists():
        raise PrepareError(f"refusing to replace existing evidence: {path}")
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise PrepareError(f"refusing to replace existing evidence: {path}")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def mount_evidence() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    filesystems = payload.get("filesystems") or []
    if len(filesystems) != 1:
        raise PrepareError("source mount did not resolve to exactly one filesystem")
    row = filesystems[0]
    options = set(clean(row.get("options")).split(","))
    if row.get("source") != "/dev/sdf1" or row.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"source mount guard failed: {row}")
    return row


def normalize_inventory(value: str) -> str:
    value = clean(value).upper()
    value = re.sub(r"\.(?:TIF|TIFF|JPE?G|PSD|PNG)$", "", value)
    value = value.replace("_", "-").replace(" ", "")
    value = re.sub(r"^TFA-(?:FOT|NEG|KB)-?", "TFA-", value)
    value = re.sub(r"^TFA(?=\d)", "TFA-", value)
    return value.rstrip(".-")


def inventory_from_basename(value: str) -> str:
    match = INVENTORY_RE.search(value)
    return normalize_inventory(match.group(1)) if match else ""


def named_values(rows: Any, key: str) -> str:
    if not isinstance(rows, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = clean(row.get(key))
        folded = value.casefold()
        if value and folded not in seen:
            values.append(value)
            seen.add(folded)
    return "; ".join(values)


def event_values(rows: Any, family: str, key: str) -> str:
    if not isinstance(rows, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        child = row.get(family)
        if not isinstance(child, dict):
            continue
        value = clean(child.get(key))
        folded = value.casefold()
        if value and folded not in seen:
            values.append(value)
            seen.add(folded)
    return "; ".join(values)


def load_md(md_run: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    coverage = json.loads((md_run / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("request_failures") != 0:
        raise PrepareError("museum-digital capture is not complete and error-free")
    if coverage.get("reported_total") != 5282 or coverage.get("captured_objects") != 5282:
        raise PrepareError("museum-digital collection count differs from reviewed total 5282")
    objects: list[dict[str, str]] = []
    memberships: list[dict[str, str]] = []
    collection_labels: dict[str, str] = {}
    collection_counts: Counter[str] = Counter()
    seen_ids: set[str] = set()
    seen_inventories: set[str] = set()
    for raw_path in sorted((md_run / "raw").glob("page-*.json")):
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        page_objects = (payload.get("results") or {}).get("objects") or {}
        values = page_objects.values() if isinstance(page_objects, dict) else page_objects
        for item in values:
            if not isinstance(item, dict):
                continue
            md_id = clean(item.get("object_id"))
            inventory = clean(item.get("object_inventory_number"))
            normalized = normalize_inventory(inventory)
            institution = item.get("object_institution") or {}
            if clean(institution.get("institution_id")) != "29":
                raise PrepareError(f"unexpected institution for MD object {md_id}")
            if md_id in seen_ids or normalized in seen_inventories:
                raise PrepareError(f"duplicate MD identity: {md_id} / {inventory}")
            seen_ids.add(md_id)
            seen_inventories.add(normalized)
            collection_rows = [row for row in item.get("object_collection", []) if isinstance(row, dict)]
            collection_ids = [clean(row.get("collection_id")) for row in collection_rows]
            if "857" not in collection_ids:
                raise PrepareError(f"MD object {md_id} lacks parent collection 857")
            for row in collection_rows:
                collection_id = clean(row.get("collection_id"))
                label = clean(row.get("collection_name"))
                collection_labels[collection_id] = label
                collection_counts[collection_id] += 1
                memberships.append(
                    {
                        "record_key": f"md:{md_id}",
                        "md_object_id": md_id,
                        "inventory": inventory,
                        "scheme_id": "museum-digital-collection",
                        "subdivision_id": f"md-collection:{collection_id}",
                        "label": label,
                        "membership_basis": "current museum-digital collection membership",
                    }
                )
            subdivisions = [row for row in collection_rows if clean(row.get("collection_id")) != "857"]
            events = [row for row in item.get("object_events", []) if isinstance(row, dict)]
            relation_places = [row for row in item.get("object_relation_places", []) if isinstance(row, dict)]
            creators = event_values(events, "people", "people_display_name")
            objects.append(
                {
                    "record_key": f"md:{md_id}",
                    "md_object_id": md_id,
                    "inventory": inventory,
                    "title": clean(item.get("object_name")),
                    "description": clean(item.get("object_description")),
                    "date_text": event_values(events, "time", "time_name"),
                    "object_type": clean(item.get("object_type")),
                    "material": clean(item.get("object_material_technique")),
                    "dimensions": clean(item.get("object_dimensions")),
                    "places": "; ".join(
                        value
                        for value in (
                            event_values(events, "place", "place_name"),
                            event_values(relation_places, "place", "place_name"),
                        )
                        if value
                    ),
                    "tags": named_values(item.get("object_tags"), "tag_name"),
                    "creator": creators,
                    "md_url": f"https://berlin.museum-digital.de/object/{md_id}",
                    "collection_ids": "; ".join(collection_ids),
                    "collections": "; ".join(clean(row.get("collection_name")) for row in collection_rows),
                    "subdivision_ids": "; ".join(clean(row.get("collection_id")) for row in subdivisions),
                    "subdivisions": "; ".join(clean(row.get("collection_name")) for row in subdivisions),
                    "last_updated": clean(item.get("object_last_updated")),
                    "record_state": "current_museum_digital",
                    "decision_state": "unresolved",
                }
            )
    if len(objects) != 5282 or set(collection_labels) != EXPECTED_COLLECTION_IDS:
        raise PrepareError(
            f"unexpected MD collection projection: objects={len(objects)} ids={sorted(collection_labels)}"
        )
    registers = []
    for collection_id in sorted(collection_labels, key=int):
        registers.append(
            {
                "scheme_id": "museum-digital-collection",
                "subdivision_id": f"md-collection:{collection_id}",
                "parent_subdivision_id": "" if collection_id == "857" else "md-collection:857",
                "label": collection_labels[collection_id],
                "subdivision_type": "collection_root" if collection_id == "857" else "collection_subdivision",
                "source_path": str(md_run / "objects.csv"),
                "assigned_object_count": str(collection_counts[collection_id]),
                "assignment_state": "object_memberships_explicit",
                "notes": "Current museum-digital relationship; memberships may be many-to-many.",
            }
        )
    memberships.sort(key=lambda row: (int(row["md_object_id"]), row["subdivision_id"]))
    objects.sort(key=lambda row: (row["inventory"].casefold(), int(row["md_object_id"])))
    return objects, registers, memberships


def load_prior_dispositions() -> tuple[dict[str, dict[str, str]], list[Path]]:
    rows: dict[str, dict[str, str]] = {}
    paths: list[Path] = []
    for unit in PUBLISHED_UNITS:
        path = Path("/mnt/volume/iss-archive/units") / unit / "db-restore/manifests/source-disposition.csv"
        if not path.is_file():
            raise PrepareError(f"required current unit manifest is absent: {path}")
        paths.append(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("deletion_authorized", "").casefold() != "false":
                    raise PrepareError(f"published manifest contains deletion authority: {path}")
                source_path = row.get("source_path", "")
                existing = rows.get(source_path)
                if existing and existing["unit"] != unit:
                    existing["prior_unit"] += f"; {unit}"
                    existing["prior_disposition"] += f"; {row.get('drain_state', '')}"
                    if not existing["source_sha256"]:
                        existing["source_sha256"] = row.get("source_sha256", "")
                    continue
                rows[source_path] = {
                    "unit": unit,
                    "prior_unit": unit,
                    "prior_disposition": row.get("drain_state", ""),
                    "source_sha256": row.get("source_sha256", ""),
                }
    return rows, paths


def proposed_role(path: str, extension: str) -> str:
    folded = path.casefold()
    ext = extension.casefold()
    if ext in METADATA_EXTENSIONS:
        return "metadata_evidence"
    if ext in {".tif", ".tiff", ".psd"}:
        return "master_candidate"
    if ext in {".jpg", ".jpeg", ".png"}:
        derivative_hints = ("400px", "800px", "freistell", "thumbnail", "preview", "web", "minimized")
        return "derivative_candidate" if any(hint in folded for hint in derivative_hints) else "image_candidate"
    if ext in {".pdf", ".doc", ".docx", ".odt", ".txt"}:
        return "document_evidence"
    return "uncertain_candidate"


def load_local_objects(connection: sqlite3.Connection, current: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    local_rows: list[dict[str, str]] = []
    for row in connection.execute(
        "SELECT inventory_number, collection, object_type, title, description, md_id, preferred_source "
        "FROM objects WHERE inventory_number LIKE 'TFA%' ORDER BY inventory_number"
    ):
        inventory, collection, object_type, title, description, md_id, preferred_source = row
        normalized = normalize_inventory(inventory)
        if normalized in current:
            continue
        local_rows.append(
            {
                "record_key": f"local:{normalized}",
                "md_object_id": clean(md_id),
                "inventory": clean(inventory),
                "title": clean(title),
                "description": clean(description),
                "date_text": "",
                "object_type": clean(object_type),
                "material": "",
                "dimensions": "",
                "places": "",
                "tags": "",
                "creator": "",
                "md_url": f"https://berlin.museum-digital.de/object/{clean(md_id)}" if clean(md_id).isdigit() else "",
                "collection_ids": "",
                "collections": clean(collection),
                "subdivision_ids": "",
                "subdivisions": "",
                "last_updated": "",
                "record_state": "local_tfa_review",
                "decision_state": "unresolved",
                "_preferred_source": clean(preferred_source),
            }
        )
    return local_rows


def build_candidates(
    objects: list[dict[str, str]],
    prior: dict[str, dict[str, str]],
    connection: sqlite3.Connection,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
    by_inventory = {normalize_inventory(row["inventory"]): row for row in objects}
    by_record_key = {row["record_key"]: row for row in objects}
    index_targets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in connection.execute(
        "SELECT l.inventory_number, l.link_type, l.source, f.path, f.rel_path, f.basename, "
        "f.extension, f.size, f.mtime_ns, f.kind "
        "FROM file_object_links l JOIN files f ON f.id=l.file_id "
        "WHERE l.inventory_number LIKE 'TFA%' ORDER BY f.path, l.inventory_number"
    ):
        inventory, link_type, source, path, rel_path, basename, extension, size, mtime_ns, kind = row
        normalized = normalize_inventory(inventory)
        obj = by_inventory.get(normalized)
        if not obj:
            continue
        relative = "archive/WF/" + str(rel_path).lstrip("/")
        index_targets[relative].append(
            {
                "record_key": obj["record_key"],
                "inventory": obj["inventory"],
                "match_basis": f"archive_index:{link_type}:{source}",
                "original_index_path": str(path),
                "basename": str(basename),
                "extension": str(extension),
                "size_bytes": str(size),
                "mtime_ns": str(mtime_ns),
                "file_kind": str(kind),
            }
        )

    candidate_map: dict[tuple[str, str], dict[str, str]] = {}
    disposition_map: dict[str, dict[str, str]] = {}
    stats = Counter()

    def add_candidate(
        obj: dict[str, str],
        inventory_row: dict[str, str],
        basis: str,
        original_index_path: str = "",
        file_kind: str = "",
    ) -> None:
        source_path = inventory_row["source_path"]
        key = (obj["record_key"], source_path)
        prior_row = prior.get(source_path, {})
        role = proposed_role(source_path, inventory_row.get("extension", ""))
        existing = candidate_map.get(key)
        if existing:
            bases = existing["match_basis"].split("; ")
            if basis not in bases:
                existing["match_basis"] += "; " + basis
            if original_index_path and not existing["original_index_path"]:
                existing["original_index_path"] = original_index_path
            return
        candidate_map[key] = {
            "candidate_id": "",
            "record_key": obj["record_key"],
            "inventory": obj["inventory"],
            "source_path": source_path,
            "source_kind": "read_only_usb",
            "original_index_path": original_index_path,
            "basename": Path(source_path).name,
            "extension": inventory_row.get("extension", "").casefold(),
            "size_bytes": inventory_row.get("size_bytes", ""),
            "mtime_ns": inventory_row.get("mtime_ns", ""),
            "file_kind": file_kind or inventory_row.get("category", ""),
            "proposed_role": role,
            "match_basis": basis,
            "reused_source_sha256": prior_row.get("source_sha256", ""),
            "prior_unit": prior_row.get("prior_unit", ""),
            "prior_disposition": prior_row.get("prior_disposition", ""),
        }
        stats[f"candidate_role:{role}"] += 1

    def add_disposition(
        inventory_row: dict[str, str],
        inventory: str,
        basis: str,
    ) -> None:
        source_path = inventory_row["source_path"]
        prior_row = prior.get(source_path, {})
        role = proposed_role(source_path, inventory_row.get("extension", ""))
        existing = disposition_map.get(source_path)
        if existing:
            if inventory and inventory not in existing["inventory"].split("; "):
                existing["inventory"] = "; ".join(filter(None, (existing["inventory"], inventory)))
            if basis not in existing["notes"]:
                existing["notes"] += f"; {basis}"
            return
        if prior_row:
            preservation_status = "retained_existing_unit_evidence"
            preservation_basis = "published_unit_source_disposition"
            drain_state = "retain_existing_unit_scope"
            note = (
                f"Existing published-unit evidence: {prior_row.get('prior_unit', '')} / "
                f"{prior_row.get('prior_disposition', '')}; {basis}"
            )
        elif role in {"metadata_evidence", "document_evidence"}:
            preservation_status = "not_assessed"
            preservation_basis = "tfa_scope_evidence"
            drain_state = "retain_pending_document_review"
            note = f"Project/catalogue evidence; {basis}"
        else:
            preservation_status = "not_assessed"
            preservation_basis = "requires_human_master_selection"
            drain_state = "needs_reconciliation"
            note = f"Proposal only; {basis}"
        disposition_map[source_path] = {
            "source_path": source_path,
            "source_kind": "file",
            "unit": "tfa",
            "inventory": inventory,
            "source_role": role,
            "final_path": "",
            "preservation_status": preservation_status,
            "preservation_basis": preservation_basis,
            "source_sha256": prior_row.get("source_sha256", ""),
            "final_sha256": "",
            "source_size_bytes": inventory_row.get("size_bytes", ""),
            "exact_copy_proven": "false",
            "drain_state": drain_state,
            "deletion_authorized": "false",
            "notes": note,
        }
        stats[f"disposition_state:{drain_state}"] += 1

    with RESTORE_INVENTORY.open(encoding="utf-8-sig", newline="") as handle:
        for inventory_row in csv.DictReader(handle):
            source_path = inventory_row["source_path"]
            relative = inventory_row["relative_path"]
            basename = Path(source_path).name
            exact_inventory = inventory_from_basename(basename)
            in_wf = relative.startswith("archive/WF/")
            tfa_named_in_wf = in_wf and "TFA" in source_path.upper()
            direct_obj = by_inventory.get(exact_inventory) if exact_inventory else None
            linked = index_targets.get(relative, [])
            if not tfa_named_in_wf and not direct_obj and not linked:
                continue
            scope_basis = []
            if tfa_named_in_wf:
                scope_basis.append("TFA path/name below restore/archive/WF")
            if direct_obj:
                scope_basis.append("exact TFA inventory token")
            if linked:
                scope_basis.append("existing archive-index object link")
            inventory_label = direct_obj["inventory"] if direct_obj else ""
            add_disposition(inventory_row, inventory_label, "; ".join(scope_basis))
            if direct_obj:
                add_candidate(
                    direct_obj,
                    inventory_row,
                    "exact_inventory_filename",
                )
            for linked_row in linked:
                obj = by_record_key[linked_row["record_key"]]
                add_candidate(
                    obj,
                    inventory_row,
                    linked_row["match_basis"],
                    linked_row["original_index_path"],
                    linked_row["file_kind"],
                )

    # Retain archive-index-only paths as protected references, never USB drain rows.
    usb_relative_paths = {
        row["source_path"].split("/media/vladimir/Elements/restore/", 1)[1]
        for row in disposition_map.values()
        if row["source_path"].startswith("/media/vladimir/Elements/restore/")
    }
    for relative, linked_rows in index_targets.items():
        if relative in usb_relative_paths:
            continue
        for linked_row in linked_rows:
            source_path = linked_row["original_index_path"]
            obj = by_record_key[linked_row["record_key"]]
            key = (obj["record_key"], source_path)
            if key in candidate_map:
                continue
            candidate_map[key] = {
                "candidate_id": "",
                "record_key": obj["record_key"],
                "inventory": obj["inventory"],
                "source_path": source_path,
                "source_kind": "protected_archive_reference",
                "original_index_path": source_path,
                "basename": linked_row["basename"],
                "extension": linked_row["extension"].casefold(),
                "size_bytes": linked_row["size_bytes"],
                "mtime_ns": linked_row["mtime_ns"],
                "file_kind": linked_row["file_kind"],
                "proposed_role": proposed_role(source_path, linked_row["extension"]),
                "match_basis": linked_row["match_basis"],
                "reused_source_sha256": "",
                "prior_unit": "",
                "prior_disposition": "retain_protected_archive_reference",
            }
            stats["candidate_source:protected_archive_reference"] += 1

    candidates = sorted(candidate_map.values(), key=lambda row: (row["record_key"], row["source_path"]))
    for index, row in enumerate(candidates, 1):
        row["candidate_id"] = str(index)
    dispositions = sorted(disposition_map.values(), key=lambda row: row["source_path"])
    stats["candidates"] = len(candidates)
    stats["source_disposition_rows"] = len(dispositions)
    stats["usb_candidate_rows"] = sum(row["source_kind"] == "read_only_usb" for row in candidates)
    stats["protected_archive_candidate_rows"] = sum(
        row["source_kind"] == "protected_archive_reference" for row in candidates
    )
    return candidates, dispositions, dict(stats)


def build_sqlite(
    path: Path,
    objects: list[dict[str, str]],
    subdivisions: list[dict[str, str]],
    memberships: list[dict[str, str]],
    candidates: list[dict[str, str]],
    dispositions: list[dict[str, str]],
    meta: dict[str, str],
) -> None:
    if path.exists():
        raise PrepareError(f"refusing to replace existing database: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE objects (
                record_key TEXT PRIMARY KEY,
                md_object_id TEXT NOT NULL,
                inventory TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                date_text TEXT NOT NULL,
                object_type TEXT NOT NULL,
                material TEXT NOT NULL,
                dimensions TEXT NOT NULL,
                places TEXT NOT NULL,
                tags TEXT NOT NULL,
                creator TEXT NOT NULL,
                md_url TEXT NOT NULL,
                collection_ids TEXT NOT NULL,
                collections TEXT NOT NULL,
                subdivision_ids TEXT NOT NULL,
                subdivisions TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                record_state TEXT NOT NULL,
                decision_state TEXT NOT NULL CHECK(decision_state IN ('unresolved','resolved')),
                UNIQUE(inventory)
            );
            CREATE TABLE subdivisions (
                subdivision_id TEXT PRIMARY KEY,
                scheme_id TEXT NOT NULL,
                parent_subdivision_id TEXT,
                label TEXT NOT NULL,
                subdivision_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                assigned_object_count INTEGER NOT NULL,
                assignment_state TEXT NOT NULL,
                notes TEXT NOT NULL,
                FOREIGN KEY(parent_subdivision_id) REFERENCES subdivisions(subdivision_id)
            );
            CREATE TABLE object_subdivisions (
                record_key TEXT NOT NULL,
                subdivision_id TEXT NOT NULL,
                membership_basis TEXT NOT NULL,
                PRIMARY KEY(record_key, subdivision_id),
                FOREIGN KEY(record_key) REFERENCES objects(record_key),
                FOREIGN KEY(subdivision_id) REFERENCES subdivisions(subdivision_id)
            );
            CREATE TABLE candidates (
                candidate_id INTEGER PRIMARY KEY,
                record_key TEXT NOT NULL,
                inventory TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                original_index_path TEXT NOT NULL,
                basename TEXT NOT NULL,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                file_kind TEXT NOT NULL,
                proposed_role TEXT NOT NULL,
                match_basis TEXT NOT NULL,
                reused_source_sha256 TEXT NOT NULL,
                prior_unit TEXT NOT NULL,
                prior_disposition TEXT NOT NULL,
                UNIQUE(record_key, source_path),
                FOREIGN KEY(record_key) REFERENCES objects(record_key)
            );
            CREATE TABLE reconciliation_decisions (
                record_key TEXT PRIMARY KEY,
                selected_candidate_id INTEGER,
                decision_state TEXT NOT NULL CHECK(decision_state IN ('selected_master','unresolved')),
                note TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                FOREIGN KEY(record_key) REFERENCES objects(record_key),
                FOREIGN KEY(selected_candidate_id) REFERENCES candidates(candidate_id)
            );
            CREATE TABLE source_disposition_proposal (
                source_path TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                unit TEXT NOT NULL,
                inventory TEXT NOT NULL,
                source_role TEXT NOT NULL,
                final_path TEXT NOT NULL,
                preservation_status TEXT NOT NULL,
                preservation_basis TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                final_sha256 TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL,
                exact_copy_proven TEXT NOT NULL CHECK(exact_copy_proven='false'),
                drain_state TEXT NOT NULL,
                deletion_authorized TEXT NOT NULL CHECK(deletion_authorized='false'),
                notes TEXT NOT NULL
            );
            CREATE TABLE drain_list_proposal AS SELECT * FROM source_disposition_proposal WHERE 0;
            CREATE UNIQUE INDEX drain_list_proposal_source_path ON drain_list_proposal(source_path);
            """
        )
        connection.executemany("INSERT INTO meta(key,value) VALUES(?,?)", sorted(meta.items()))
        connection.executemany(
            "INSERT INTO objects VALUES (" + ",".join("?" for _ in OBJECT_FIELDS) + ")",
            [[row.get(field, "") for field in OBJECT_FIELDS] for row in objects],
        )
        # Insert parent before children to satisfy the self-reference.
        ordered_subdivisions = sorted(subdivisions, key=lambda row: bool(row["parent_subdivision_id"]))
        connection.executemany(
            "INSERT INTO subdivisions VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["subdivision_id"],
                    row["scheme_id"],
                    row["parent_subdivision_id"] or None,
                    row["label"],
                    row["subdivision_type"],
                    row["source_path"],
                    int(row["assigned_object_count"]),
                    row["assignment_state"],
                    row["notes"],
                )
                for row in ordered_subdivisions
            ],
        )
        connection.executemany(
            "INSERT INTO object_subdivisions VALUES (?,?,?)",
            [(row["record_key"], row["subdivision_id"], row["membership_basis"]) for row in memberships],
        )
        connection.executemany(
            "INSERT INTO candidates VALUES (" + ",".join("?" for _ in CANDIDATE_FIELDS) + ")",
            [[row[field] for field in CANDIDATE_FIELDS] for row in candidates],
        )
        connection.executemany(
            "INSERT INTO source_disposition_proposal VALUES (" + ",".join("?" for _ in DISPOSITION_FIELDS) + ")",
            [[row[field] for field in DISPOSITION_FIELDS] for row in dispositions],
        )
        connection.executemany(
            "INSERT INTO drain_list_proposal VALUES (" + ",".join("?" for _ in DISPOSITION_FIELDS) + ")",
            [[row[field] for field in DISPOSITION_FIELDS] for row in dispositions],
        )
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PrepareError("reconciliation database quick_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise PrepareError("reconciliation database foreign_key_check failed")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    md_run = inside(args.md_run, WORK_ROOT)
    output = inside(args.output, WORK_ROOT)
    if output.exists():
        raise PrepareError(f"output already exists: {output}")
    if FINAL_UNIT.exists():
        raise PrepareError(f"final TFA unit unexpectedly exists: {FINAL_UNIT}")
    mount = mount_evidence()
    if not ARCHIVE_INDEX.is_file() or not RESTORE_INVENTORY.is_file():
        raise PrepareError("required existing index evidence is absent")

    objects, subdivisions, memberships = load_md(md_run)
    current = {normalize_inventory(row["inventory"]): row for row in objects}
    source_connection = sqlite3.connect(f"file:{ARCHIVE_INDEX}?mode=ro", uri=True)
    try:
        if source_connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PrepareError("existing archive index quick_check failed")
        local_objects = load_local_objects(source_connection, current)
        for row in local_objects:
            row.pop("_preferred_source", None)
        objects.extend(local_objects)
        objects.sort(key=lambda row: (row["inventory"].casefold(), row["record_key"]))
        prior, prior_paths = load_prior_dispositions()
        candidates, dispositions, candidate_stats = build_candidates(objects, prior, source_connection)
    finally:
        source_connection.close()

    input_paths = [
        md_run / "coverage.json",
        md_run / "objects.csv",
        md_run / "request-manifest.csv",
        ARCHIVE_INDEX,
        RESTORE_INVENTORY,
        *prior_paths,
    ]
    wal = ARCHIVE_INDEX.with_name(ARCHIVE_INDEX.name + "-wal")
    if wal.is_file() and wal.stat().st_size:
        input_paths.append(wal)
    input_fingerprints = {str(path): sha256_file(path) for path in input_paths}
    meta = {
        "format": FORMAT,
        "created_at": utc_now(),
        "source_mount": json.dumps(mount, ensure_ascii=False, sort_keys=True),
        "md_run": str(md_run),
        "md_capture_fingerprint": input_fingerprints[str(md_run / "coverage.json")],
        "archive_index_fingerprint": input_fingerprints[str(ARCHIVE_INDEX)],
        "restore_inventory_fingerprint": input_fingerprints[str(RESTORE_INVENTORY)],
        "deletion_authorized": "false",
        "decisions_written": "0",
    }

    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "objects.csv", OBJECT_FIELDS, objects)
    write_csv(output / "subdivision-register.csv", SUBDIVISION_FIELDS, subdivisions)
    write_csv(output / "subdivision-memberships.csv", MEMBERSHIP_FIELDS, memberships)
    write_csv(output / "candidates.csv", CANDIDATE_FIELDS, candidates)
    write_csv(output / "source-disposition-proposal.csv", DISPOSITION_FIELDS, dispositions)
    write_csv(output / "drain-list-proposal.csv", DISPOSITION_FIELDS, dispositions)
    build_sqlite(
        output / "reconciliation.sqlite",
        objects,
        subdivisions,
        memberships,
        candidates,
        dispositions,
        meta,
    )
    readme = (
        "# TFA reconciliation proposal\n\n"
        "This is proposal-only work evidence for museum-digital collection 857, "
        "organisation 29. It contains no Master decisions and authorizes no deletion.\n\n"
        "Subdivisions are preserved as many-to-many catalogue relationships, not as "
        "Master folders. The drain-list proposal is an exact-path review register; "
        "every row remains unresolved or retained and has `deletion_authorized=false`.\n"
    )
    readme_path = output / "README.md"
    with readme_path.open("x", encoding="utf-8") as handle:
        handle.write(readme)
        handle.flush()
        os.fsync(handle.fileno())

    output_files = sorted(path for path in output.iterdir() if path.name != "summary.json")
    output_fingerprints = {path.name: sha256_file(path) for path in output_files}
    state_counts = Counter(row["record_state"] for row in objects)
    summary = {
        "format": FORMAT,
        "created_at": meta["created_at"],
        "result": "ok",
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": 0,
        "final_unit_created": False,
        "decisions_written": 0,
        "deletion_authorized": False,
        "md_objects": state_counts["current_museum_digital"],
        "local_tfa_review_objects": state_counts["local_tfa_review"],
        "objects_total": len(objects),
        "subdivision_register_rows": len(subdivisions),
        "subdivision_membership_rows": len(memberships),
        "candidate_stats": candidate_stats,
        "input_fingerprints": input_fingerprints,
        "output_fingerprints": output_fingerprints,
    }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
