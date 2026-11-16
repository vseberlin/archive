#!/usr/bin/env python3
"""Consolidate Kurt Schwarz catalog evidence and package historical drafts.

The published unit and all source media are evidence-only inputs.  This tool
writes one timestamped run below the approved Kurt Schwarz work root.  It does
not alter, move, rename, or delete any source or published-unit file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

import paragon_document_bundle
from kurt_schwarz_discovery import flatten_md_object, load_md_objects
from kurt_schwarz_shared_workbooks import read_xlsx


FORMAT = "iss-kurt-schwarz-documentation-consolidation-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
RECONCILIATION_DB = WORK_ROOT / (
    "work/reconciliation/unit-reconciliation-v1/reconciliation.sqlite"
)
CURRENT_MD_RUN = WORK_ROOT / (
    "work/reconciliation/museum-digital/runs/"
    "20260803T084925Z-kurt-schwarz-current-collection-783"
)
IDENTITY_RUN = Path(
    "/mnt/volume/iss-archive-work/identity-provenance/"
    "kurt-schwarz-pressebildarchiv/runs/"
    "20260803T090600Z-kurt-schwarz-pressebildarchiv-identity"
)
BATCH_RECORDS = WORK_ROOT / (
    "work/reconciliation/batch-catalogs/"
    "20260729T234000Z-kurt-schwarz-batch-catalogs/batch-records.csv"
)
SHARED_WORKBOOK_RECORDS = WORK_ROOT / (
    "work/reconciliation/shared-workbooks/"
    "20260730T093000Z-google-drive-kurt-schwarz-workbooks/"
    "shared-workbook-records.csv"
)
LATEST_LOCAL_EXPORT = UNIT / (
    "documents/usb/archive/WF/Kurt Schwarz/"
    "Kurt-Schwarz_Metadatenexport_2025-01-06.xlsx"
)

INVENTORY_RE = re.compile(
    r"(?i)KS-[5-9]-(?:BZ|NB|WF|ZP)[_-]\d+(?:[-_][A-Z0-9]+)*"
)
CATALOG_NAME_RE = re.compile(
    r"(?i)(gesamtverzeichnis|metadatenexport|mdexport|mdexcel|lightroom|"
    r"findbuch|ausstellungsbilder|berlinerzeitung|bz nachtragen|"
    r"originaltitel|ks-alle-bilder|inventar primus|vergleich_a\+s|"
    r"reproduktionsrechte|upload.*(?:csv|xml)|csv_xml|lieblingsbilder)"
)
DRAFT_NAME_RE = re.compile(
    r"(?i)(^~\$|\bkopie\b|\bvorlage\b|\bmuster\b|between|"
    r"(?:^|[_ .-])(?:0[1-9]|1[0-1])(?:[_ .-]|$)|"
    r"_s\d*x?|_fl\b|weiter-unten|angepasst|überarbeitet|korr|"
    r"ohne[-_ ]?(?:bilder|bildchen)|for[-_ ]?lr)"
)
OFFICE_XML_SUFFIXES = {".docx", ".odt", ".pptx"}
SPREADSHEET_SUFFIXES = {".csv", ".xlsx", ".xls", ".ods"}
FINAL_CATALOG_FIELDS = (
    "iss_id",
    "canonical_record_key",
    "alias_record_keys",
    "museum_digital_ids",
    "inventory",
    "inventory_aliases",
    "title",
    "description",
    "tags",
    "places",
    "date_text",
    "material",
    "dimensions",
    "title_basis",
    "description_basis",
    "tags_basis",
    "places_basis",
    "date_text_basis",
    "material_basis",
    "dimensions_basis",
    "selected_master_filenames",
    "selected_master_paths",
    "selected_master_sha256",
    "museum_digital_url",
    "kurt_collection_ids",
    "kurt_collections",
    "shared_unit_ids",
    "catalog_evidence_count",
    "catalog_evidence_sources",
    "match_status",
    "reconciliation_source_fingerprint",
)
DOCUMENT_REVIEW_FIELDS = (
    "final_path",
    "filename",
    "extension",
    "size_bytes",
    "sha256",
    "source_path",
    "existing_role",
    "existing_relationship",
    "content_family",
    "proposed_relationship",
    "proposed_disposition",
    "historical_package_member",
    "spreadsheet_rows",
    "spreadsheet_sheets",
    "inventory_references",
    "text_evidence",
    "review_basis",
    "access_restriction",
    "deletion_authorized",
)


class ConsolidationError(RuntimeError):
    """A safety, input, or validation failure."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def joined(values: Iterable[str]) -> str:
    return "; ".join(sorted({clean(value) for value in values if clean(value)}))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def csv_bytes(rows: list[dict[str, Any]], fields: Iterable[str]) -> bytes:
    fields = tuple(fields)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def require_inside(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ConsolidationError(f"path escaped allowed root: {resolved}") from error
    return resolved


def atomic_write(path: Path, payload: bytes) -> None:
    require_inside(path, WORK_ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        temporary.unlink()
        raise ConsolidationError(f"refusing to overwrite: {path}")
    os.replace(temporary, path)


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def latest_rows(
    connection: sqlite3.Connection, table: str, keys: tuple[str, ...]
) -> dict[tuple[str, ...], sqlite3.Row]:
    key_sql = ", ".join(keys)
    rows = connection.execute(
        f"""SELECT item.* FROM {table} AS item
             JOIN (
               SELECT {key_sql}, MAX(decision_id) AS decision_id
               FROM {table} GROUP BY {key_sql}
             ) AS latest ON latest.decision_id=item.decision_id"""
    )
    return {tuple(row[key] for key in keys): row for row in rows}


def effective_value(field: sqlite3.Row, decision: sqlite3.Row | None) -> tuple[str, str]:
    if decision is not None and decision["action"] != "defer":
        accepted = clean(decision["accepted_value"])
        if accepted:
            return accepted, decision["action"]
    md_value = clean(field["md_value"])
    local_value = clean(field["local_value"])
    if decision is not None and decision["action"] == "defer":
        return md_value or local_value, "defer"
    return md_value or local_value, field["comparison_state"]


def load_current_md() -> dict[str, dict[str, str]]:
    return {
        row["md_object_id"]: row
        for row in map(flatten_md_object, load_md_objects(CURRENT_MD_RUN))
    }


def validate_current_md(connection: sqlite3.Connection) -> dict[str, int]:
    current = load_current_md()
    field_map = {
        "inventory": "inventory",
        "title": "title",
        "description": "description",
        "tags": "tags",
        "places": "places",
        "date_text": "date_text",
        "material": "material",
        "dimensions": "dimensions",
    }
    drift: Counter[str] = Counter()
    for item in connection.execute("SELECT * FROM objects WHERE md_object_id<>''"):
        source = current.get(item["md_object_id"])
        if source is None:
            drift["historical_or_missing_current_md"] += 1
            continue
        drift["matched_current_md"] += 1
        for field in connection.execute(
            "SELECT * FROM metadata_fields WHERE record_key=?", (item["record_key"],)
        ):
            if clean(field["md_value"]) != clean(source[field_map[field["field_key"]]]):
                drift[f"drift_{field['field_key']}"] += 1
    if any(key.startswith("drift_") for key in drift):
        raise ConsolidationError(f"current museum-digital drift: {dict(drift)}")
    return dict(drift)


def identity_indices() -> tuple[
    dict[str, str], dict[str, list[dict[str, str]]], dict[str, list[str]]
]:
    entities = read_csv(IDENTITY_RUN / "entities.csv")
    entity_by_record = {row["canonical_record_key"]: row["entity_id"] for row in entities}
    identifiers: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(IDENTITY_RUN / "identifiers.csv"):
        identifiers[row["entity_id"]].append(row)
    units: dict[str, list[str]] = defaultdict(list)
    for row in read_csv(IDENTITY_RUN / "unit-memberships.csv"):
        if row["status"] == "accepted":
            units[row["entity_id"]].append(row["unit_id"])
    return entity_by_record, identifiers, units


def catalog_evidence() -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(BATCH_RECORDS):
        object_id = row["mapped_md_object_id"]
        if object_id:
            evidence[f"md:{object_id}"].add(row["source_path"])
    for row in read_csv(SHARED_WORKBOOK_RECORDS):
        for record_key in row["mapped_record_keys"].split("; "):
            if record_key:
                evidence[record_key].add(row["source_document"])
    for _, _, row in read_xlsx(LATEST_LOCAL_EXPORT):
        object_id = clean(row.get("md:number"))
        if object_id:
            evidence[f"md:{object_id}"].add(str(LATEST_LOCAL_EXPORT))
    return evidence


def build_final_catalog(connection: sqlite3.Connection) -> list[dict[str, str]]:
    aliases = latest_rows(connection, "object_alias_decisions", ("alias_record_key",))
    active_aliases = {
        key[0]: row["canonical_record_key"]
        for key, row in aliases.items()
        if row["action"] == "same_object_alias"
    }
    alias_by_canonical: dict[str, list[str]] = defaultdict(list)
    for alias, canonical in active_aliases.items():
        alias_by_canonical[canonical].append(alias)
    metadata = latest_rows(connection, "metadata_decisions", ("record_key", "field_key"))
    media = latest_rows(connection, "media_decisions", ("record_key", "candidate_id"))
    candidates = {
        row["candidate_id"]: row
        for row in connection.execute("SELECT * FROM media_candidates")
    }
    entity_by_record, identifiers, unit_memberships = identity_indices()
    evidence = catalog_evidence()
    source_fingerprint = connection.execute(
        "SELECT value FROM meta WHERE key='source_fingerprint'"
    ).fetchone()[0]
    current_md = load_current_md()
    rows: list[dict[str, str]] = []
    for item in connection.execute("SELECT * FROM objects ORDER BY record_key"):
        record_key = item["record_key"]
        if record_key in active_aliases:
            continue
        related_keys = [record_key, *sorted(alias_by_canonical.get(record_key, []))]
        entity_id = entity_by_record.get(record_key, "")
        if not entity_id:
            raise ConsolidationError(f"canonical object lacks ISS identity: {record_key}")
        fields = {
            row["field_key"]: row
            for row in connection.execute(
                "SELECT * FROM metadata_fields WHERE record_key=?", (record_key,)
            )
        }
        values: dict[str, str] = {}
        bases: dict[str, str] = {}
        for field_key, field in fields.items():
            value, basis = effective_value(
                field, metadata.get((record_key, field_key))
            )
            values[field_key] = value
            bases[field_key] = basis
        selected = []
        for related_key in related_keys:
            selected.extend(
                candidates[candidate_id]
                for (candidate_record, candidate_id), decision in media.items()
                if candidate_record == related_key
                and decision["disposition"] == "selected_master"
            )
        selected = list({row["candidate_id"]: row for row in selected}.values())
        if not selected:
            raise ConsolidationError(f"canonical object lacks selected master: {record_key}")
        all_identifiers = identifiers[entity_id]
        md_ids = [row["value"] for row in all_identifiers if row["namespace"] == "museum-digital"]
        inventories = [row["value"] for row in all_identifiers if row["namespace"] == "inventory"]
        md_row = next((current_md[value] for value in md_ids if value in current_md), None)
        subdivisions = [] if md_row is None else [
            pair for pair in zip(
                md_row["subdivision_ids"].split("; "),
                md_row["subdivisions"].split("; "),
            ) if pair[0] and pair[0] != "555"
        ]
        source_paths = set()
        for related_key in related_keys:
            source_paths.update(evidence.get(related_key, set()))
        rows.append(
            {
                "iss_id": entity_id,
                "canonical_record_key": record_key,
                "alias_record_keys": joined(alias_by_canonical.get(record_key, [])),
                "museum_digital_ids": joined(md_ids),
                "inventory": values.get("inventory", ""),
                "inventory_aliases": joined(
                    value for value in inventories if value != values.get("inventory", "")
                ),
                "title": values.get("title", ""),
                "description": values.get("description", ""),
                "tags": values.get("tags", ""),
                "places": values.get("places", ""),
                "date_text": values.get("date_text", ""),
                "material": values.get("material", ""),
                "dimensions": values.get("dimensions", ""),
                "title_basis": bases.get("title", ""),
                "description_basis": bases.get("description", ""),
                "tags_basis": bases.get("tags", ""),
                "places_basis": bases.get("places", ""),
                "date_text_basis": bases.get("date_text", ""),
                "material_basis": bases.get("material", ""),
                "dimensions_basis": bases.get("dimensions", ""),
                "selected_master_filenames": joined(row["filename"] for row in selected),
                "selected_master_paths": joined(row["final_path"] for row in selected),
                "selected_master_sha256": joined(row["content_sha256"] for row in selected),
                "museum_digital_url": joined(
                    f"https://berlin.museum-digital.de/object/{value}" for value in md_ids
                ),
                "kurt_collection_ids": joined(pair[0] for pair in subdivisions),
                "kurt_collections": joined(pair[1] for pair in subdivisions),
                "shared_unit_ids": joined(
                    unit for unit in unit_memberships[entity_id] if unit != "kurt-schwarz"
                ),
                "catalog_evidence_count": str(len(source_paths)),
                "catalog_evidence_sources": joined(source_paths),
                "match_status": "matched" if md_ids else "no_match_found",
                "reconciliation_source_fingerprint": source_fingerprint,
            }
        )
    if len(rows) != 1668:
        raise ConsolidationError(f"expected 1668 canonical catalog rows, found {len(rows)}")
    return sorted(rows, key=lambda row: (row["inventory"].casefold(), row["iss_id"]))


def office_xml_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [
            name for name in archive.namelist()
            if name.endswith(".xml")
            and (
                name.startswith("word/")
                or name.startswith("content.xml")
                or name.startswith("ppt/slides/")
            )
        ]
        text = []
        for name in names:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            text.extend(node.text or "" for node in root.iter() if node.text)
        return clean(" ".join(text))


def spreadsheet_evidence(path: Path) -> tuple[int, int, int, str]:
    suffix = path.suffix.casefold()
    if suffix == ".xlsx":
        rows = read_xlsx(path)
        inventories = set()
        sheets = set()
        snippets = []
        for sheet, _, row in rows:
            sheets.add(sheet)
            for value in row.values():
                inventories.update(match.group(0) for match in INVENTORY_RE.finditer(clean(value)))
            if len(snippets) < 3:
                snippets.extend(clean(value)[:180] for value in row.values() if clean(value))
        return len(rows), len(sheets), len(inventories), clean(" | ".join(snippets))[:500]
    if suffix == ".csv":
        payload = path.read_bytes()
        text = ""
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
        if not text:
            return 0, 0, 0, "unreadable CSV encoding"
        first = text.splitlines()[0] if text.splitlines() else ""
        delimiter = ";" if first.count(";") >= first.count(",") else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        inventories = set(INVENTORY_RE.findall(text))
        return max(0, len(rows) - 1), 1, len(inventories), clean(" | ".join(first.split(delimiter)))[:500]
    if suffix == ".xls":
        completed = subprocess.run(
            ["catdoc", str(path)], capture_output=True, timeout=120, check=False
        )
        text = completed.stdout.decode("utf-8", errors="replace")
        inventories = set(INVENTORY_RE.findall(text))
        return len(text.splitlines()), 1, len(inventories), clean(text)[:500]
    return 0, 0, 0, "spreadsheet format retained without row extraction"


def is_historical_draft_path(path: str) -> bool:
    """Detect an internal archive/version marker without matching evidence roots."""
    lower = path.casefold()
    for prefix in (
        "documents/usb/archive/",
        "documents/contextual/archive/",
        "documents/restricted/archive/",
        "documents/restricted/usb/archive/",
    ):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    return "/archiv/" in f"/{lower}" or bool(DRAFT_NAME_RE.search(lower))


def classify_document(path: str, existing_relationship: str, restriction: str) -> tuple[str, str, str, bool, str]:
    lower = path.casefold()
    name = Path(path).name
    suffix = Path(path).suffix.casefold()
    if name.startswith("~$"):
        return "office_lock_file", "excluded", "package_historical", True, "temporary Office lock file"
    if "/catalog/10.0pro/" in lower:
        return "legacy_catalog_component", "direct", "package_historical", True, "one legacy catalog component set"
    if re.search(r"(?i)(^|/)(wf-sender/|wfs-)", path):
        return "cross_unit_wf_sender", "mixed", "cross_unit_reference", False, "WF-Sender owns issue; Kurt relation requires explicit evidence"
    if suffix == ".mp3":
        return "audio_recording", "direct", "needs_visual_review", False, "audio is not ordinary documentation"
    if suffix in SPREADSHEET_SUFFIXES and CATALOG_NAME_RE.search(path):
        return "catalog_or_description_source", "direct", "package_historical", True, "superseded by consolidated final catalog"
    if "/antragshilfe-vorlagen/" in lower:
        return "generic_project_template", "excluded", "package_historical", True, "generic application template"
    if "/restricted/" in lower or restriction == "restricted":
        draft = is_historical_draft_path(path)
        return "restricted_project_history", "direct", ("package_historical" if draft else "needs_visual_review"), draft, "project administration; restricted access"
    if "/contextual/" in lower:
        return "cross_project_description_source", "contextual", "retain_contextual", False, "previous substantive Kurt Schwarz context review"
    if "/google-drive/" in lower:
        draft = is_historical_draft_path(path)
        return "shared_container_project_record", "direct", ("package_historical" if draft else "retain_direct"), draft, "direct member of reviewed Kurt Schwarz shared container"
    if suffix == ".zip":
        return "source_container", "mixed", "needs_visual_review", False, "container requires member-level relevance review"
    if suffix == ".url" or "/links " in lower:
        return "research_reference", "contextual", "retain_contextual", False, "research link retained as contextual evidence"
    if "/scans/" in lower:
        return "research_or_correspondence_scan", "contextual", "needs_visual_review", False, "scan needs content review"
    if "ausstellung" in lower or "vita " in lower or "kurzbio" in lower:
        draft = is_historical_draft_path(path)
        return "exhibition_or_biographical_document", "contextual", ("package_historical" if draft else "needs_visual_review"), draft, "exhibition/biographical relation"
    if is_historical_draft_path(path):
        return "project_document_draft", "direct", "package_historical", True, "versioned, copied, template, or archive path"
    if existing_relationship in {"cross_project_context_document", "direct_project_member_in_shared_container", "restricted_project_history"}:
        relationship = {
            "cross_project_context_document": "contextual",
            "direct_project_member_in_shared_container": "direct",
            "restricted_project_history": "direct",
        }[existing_relationship]
        return "previously_reviewed_project_record", relationship, ("retain_contextual" if relationship == "contextual" else "retain_direct"), False, "previous explicit relationship review"
    return "unreviewed_project_record", "needs_review", "needs_visual_review", False, "bounded source location is not relevance proof"


def review_documents() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    manifest = read_csv(UNIT / "db-restore/manifests/representations.csv")
    rows: list[dict[str, str]] = []
    historical: list[dict[str, str]] = []
    for source in manifest:
        final_path = source["final_relative_path"]
        if not final_path.startswith("documents/") or final_path == "documents/CONTENTS.md":
            continue
        path = UNIT / final_path
        if not path.is_file():
            raise ConsolidationError(f"document is missing: {path}")
        family, relationship, disposition, packaged, basis = classify_document(
            final_path, source["relationship"], source["access_restriction"]
        )
        sheet_rows = sheet_count = inventory_count = 0
        text_evidence = ""
        try:
            if path.suffix.casefold() in SPREADSHEET_SUFFIXES:
                sheet_rows, sheet_count, inventory_count, text_evidence = spreadsheet_evidence(path)
            elif path.suffix.casefold() in OFFICE_XML_SUFFIXES:
                text = office_xml_text(path)
                inventory_count = len(set(INVENTORY_RE.findall(text)))
                text_evidence = text[:500]
            elif path.suffix.casefold() == ".doc":
                completed = subprocess.run(
                    ["catdoc", str(path)], capture_output=True, timeout=60, check=False
                )
                text = completed.stdout.decode("utf-8", errors="replace")
                inventory_count = len(set(INVENTORY_RE.findall(text)))
                text_evidence = clean(text)[:500]
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError, ValueError) as error:
            text_evidence = f"extraction_error:{type(error).__name__}:{error}"
            if family == "catalog_or_description_source":
                disposition = "package_historical"
                packaged = True
                basis += "; damaged historical catalog retained with condition note"
        row = {
            "final_path": final_path,
            "filename": path.name,
            "extension": path.suffix.casefold(),
            "size_bytes": source["size_bytes"],
            "sha256": source["final_sha256"],
            "source_path": source["source_path"] + (
                f"::{source['source_member']}" if source["source_member"] else ""
            ),
            "existing_role": source["role"],
            "existing_relationship": source["relationship"],
            "content_family": family,
            "proposed_relationship": relationship,
            "proposed_disposition": disposition,
            "historical_package_member": final_path if packaged else "",
            "spreadsheet_rows": str(sheet_rows),
            "spreadsheet_sheets": str(sheet_count),
            "inventory_references": str(inventory_count),
            "text_evidence": text_evidence,
            "review_basis": basis,
            "access_restriction": source["access_restriction"],
            "deletion_authorized": "false",
        }
        rows.append(row)
        if packaged:
            historical.append(row)
    if len(rows) != 484:
        raise ConsolidationError(f"expected 484 document rows, found {len(rows)}")
    return rows, historical


def historical_readme(rows: list[dict[str, str]]) -> bytes:
    return (
        "# Kurt Schwarz historical catalog and document drafts\n\n"
        "This ZIP preserves exact bytes of superseded catalog generations and clear "
        "document drafts selected by the documentation consolidation review. Member "
        "paths retain the published evidence paths. The package is historical evidence, "
        "not the current catalog and never deletion authority. See MANIFEST.csv for "
        "source paths and stored SHA-256 values.\n\n"
        f"Members: {len(rows)}\n"
    ).encode("utf-8")


def write_historical_zip(path: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    require_inside(path, WORK_ROOT)
    if path.exists():
        raise ConsolidationError(f"refusing to overwrite: {path}")
    manifest_fields = (
        "historical_package_member", "source_path", "size_bytes", "sha256",
        "content_family", "proposed_relationship", "review_basis", "text_evidence",
        "deletion_authorized"
    )
    manifest = csv_bytes(rows, manifest_fields)
    readme = historical_readme(rows)
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("README.md", readme)
        archive.writestr("MANIFEST.csv", manifest)
        for row in sorted(rows, key=lambda item: item["historical_package_member"]):
            source = UNIT / row["historical_package_member"]
            archive.write(source, row["historical_package_member"])
    with zipfile.ZipFile(path) as archive:
        error = archive.testzip()
        if error:
            raise ConsolidationError(f"historical ZIP CRC failure: {error}")
        infos = archive.infolist()
    return {
        "payload_members": len(rows),
        "zip_members": len(infos),
        "payload_bytes": sum(int(row["size_bytes"]) for row in rows),
        "zip_bytes": path.stat().st_size,
        "manifest_sha256": sha256_bytes(manifest),
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    output = require_inside(args.output, WORK_ROOT)
    if output.exists():
        raise ConsolidationError(f"output already exists: {output}")
    output.mkdir(parents=True)
    connection = sqlite3.connect(f"file:{RECONCILIATION_DB}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != "ok" or foreign_keys:
            raise ConsolidationError("reconciliation database failed integrity checks")
        md_validation = validate_current_md(connection)
        catalog = build_final_catalog(connection)
    finally:
        connection.close()
    reviews, historical = review_documents()
    review_payload = csv_bytes(reviews, DOCUMENT_REVIEW_FIELDS)
    catalog_payload = csv_bytes(catalog, FINAL_CATALOG_FIELDS)
    xlsx_payload = paragon_document_bundle.table_xlsx_bytes(
        FINAL_CATALOG_FIELDS,
        [{field: row.get(field, "") for field in FINAL_CATALOG_FIELDS} for row in catalog],
        sheet_name="Final Catalog",
        table_name="FinalCatalog",
    )
    atomic_write(output / "document-review.csv", review_payload)
    atomic_write(output / "FINAL-CATALOG.csv", catalog_payload)
    atomic_write(output / "FINAL-CATALOG.xlsx", xlsx_payload)
    zip_result = write_historical_zip(
        output / "historical-catalog-and-drafts.zip", historical
    )
    summary = {
        "format": FORMAT,
        "created_at": now(),
        "published_unit": str(UNIT),
        "reconciliation_database": str(RECONCILIATION_DB),
        "current_md_run": str(CURRENT_MD_RUN),
        "identity_run": str(IDENTITY_RUN),
        "document_rows": len(reviews),
        "document_family_counts": dict(Counter(row["content_family"] for row in reviews)),
        "proposed_disposition_counts": dict(Counter(row["proposed_disposition"] for row in reviews)),
        "final_catalog_rows": len(catalog),
        "catalog_rows_with_md": sum(bool(row["museum_digital_ids"]) for row in catalog),
        "catalog_local_only_rows": sum(not row["museum_digital_ids"] for row in catalog),
        "catalog_rows_with_multiple_selected_masters": sum(
            "; " in row["selected_master_paths"] for row in catalog
        ),
        "current_md_validation": md_validation,
        "historical_package": zip_result,
        "source_files_modified": 0,
        "published_files_modified": 0,
        "deletion_authorized": False,
    }
    validation = {
        "result": "ok",
        "sqlite_integrity": integrity,
        "foreign_key_violations": len(foreign_keys),
        "document_rows": len(reviews),
        "unique_document_paths": len({row["final_path"] for row in reviews}),
        "final_catalog_rows": len(catalog),
        "unique_iss_ids": len({row["iss_id"] for row in catalog}),
        "unique_canonical_record_keys": len({row["canonical_record_key"] for row in catalog}),
        "historical_zip_crc": "ok",
        "collection_555_as_kurt_subdivision_rows": sum(
            "555" in row["kurt_collection_ids"].split("; ") for row in catalog
        ),
        "deletion_authorized": False,
    }
    if validation["collection_555_as_kurt_subdivision_rows"]:
        raise ConsolidationError("collection 555 leaked into Kurt subdivision fields")
    atomic_write(
        output / "summary.json",
        (json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    atomic_write(
        output / "validation.json",
        (json.dumps(validation, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return {"result": "ok", "output": str(output), **summary}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    try:
        print(json.dumps(execute(parser().parse_args()), ensure_ascii=False, indent=2))
        return 0
    except (
        ConsolidationError,
        OSError,
        csv.Error,
        json.JSONDecodeError,
        sqlite3.Error,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
