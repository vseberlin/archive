#!/usr/bin/env python3
"""Prepare a human-only metadata review for selected local TFA objects.

The projection reads the protected Elements source, the existing document
index, and current TFA work CSVs.  It writes only a candidate CSV, an empty
decision CSV, and a receipt inside the TFA work root.  It never changes local
object decisions, the published TFA unit, source files, or deletion state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


VERSION = "1.1.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DOCUMENT_INDEX = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
PUBLISHED_DATABASE = Path("/mnt/volume/iss-archive/units/tfa/db-restore/RESTORE.sqlite")
LOCAL_ACCESSION = REVIEW_ROOT / "reference-photo-accession-candidates.csv"
METADATA_PLAN = REVIEW_ROOT / "tfa-metadata-accession-plan.scope-reviewed.csv"
CANDIDATES = REVIEW_ROOT / "local-object-metadata-review-candidates.csv"
DECISIONS = REVIEW_ROOT / "local-object-metadata-review-decisions.csv"
RECEIPT = REVIEW_ROOT / "local-object-metadata-review-preparation.json"

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS}

# Do not let TFA-549412 match TFA-549412a or TFA-549412-a.  A filename
# extension after an otherwise exact token is permitted.
TFA_RE = re.compile(
    r"(?i)(?:^|[^A-Z0-9])TFA(?:-(?:FOT|NEG|KB))?[-_ ]?"
    r"(\d{5,8}(?:[-_.]\d{1,2})?)(?![A-Z0-9_-])"
)
SUPPORTED_EXTENSIONS = {".xlsx", ".csv"}
REQUIRED_TARGET_FIELDS = (
    "record_key", "inventory", "selected_master_path", "metadata_state",
)
CANDIDATE_FIELDS = (
    "candidate_id", "record_key", "inventory", "selected_master_path",
    "source_path", "source_sha256", "source_size_bytes", "source_mtime_ns",
    "sheet_name", "row_number", "matched_cell_references",
    "inventory_tokens_in_row", "row_scope", "header_row_number",
    "row_inventory", "identity_review_state", "md_record_key", "md_object_id",
    "md_inventory", "md_title", "md_date_text", "md_selected_master_path",
    "labelled_cells_json", "raw_cells_json", "suggested_title",
    "suggested_description", "suggested_date_text", "suggested_object_type",
    "suggested_material", "suggested_dimensions", "candidate_basis",
    "deletion_authorized",
)
DECISION_FIELDS = (
    "record_key", "decision", "candidate_id", "title", "description",
    "date_text", "object_type", "material", "dimensions", "note",
    "decided_by", "decided_at", "source_path", "source_sha256",
    "sheet_name", "row_number", "deletion_authorized",
)


class PrepareError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalized_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return "_".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def canonical_inventory(value: str) -> str:
    suffix = re.sub(r"[_ .]+", "-", value.upper()).strip("-")
    if suffix.endswith("-0"):
        suffix = suffix[:-2]
    return "TFA-" + suffix


def inventories(value: Any) -> set[str]:
    return {canonical_inventory(match) for match in TFA_RE.findall(str(value or ""))}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    assert_work_path(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def assert_work_path(path: Path) -> None:
    try:
        path.parent.resolve(strict=True).relative_to(WORK_ROOT.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PrepareError(f"write path escaped TFA work root: {path}") from error


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise PrepareError("Elements did not resolve to one mounted filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements read-only mount guard failed: {mount}")
    return mount


def inside_source(path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def column_number(reference: str) -> int:
    result = 0
    for character in reference:
        if not character.isalpha():
            break
        result = result * 26 + ord(character.upper()) - 64
    return result


def column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def xlsx_rows(path: Path) -> Iterable[tuple[str, int, dict[int, str]]]:
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{{{MAIN_NS}}}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        }
        for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
            relation_id = sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]
            target = targets[relation_id].lstrip("/")
            worksheet_path = target if target.startswith("xl/") else "xl/" + target
            worksheet = ET.fromstring(archive.read(worksheet_path))
            sheet_name = clean(sheet.attrib.get("name", "Sheet"))
            for xml_row in worksheet.findall(".//m:sheetData/m:row", NS):
                values: dict[int, str] = {}
                for cell in xml_row.findall("m:c", NS):
                    number = column_number(cell.attrib.get("r", "A1"))
                    if number <= 0 or number > 512:
                        continue
                    value_node = cell.find("m:v", NS)
                    cell_type = cell.attrib.get("t", "")
                    if cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
                    elif value_node is None:
                        value = ""
                    elif cell_type == "s":
                        index = int(value_node.text or "0")
                        value = shared[index] if 0 <= index < len(shared) else ""
                    else:
                        value = value_node.text or ""
                    value = clean(value)
                    if value:
                        values[number] = value
                if values:
                    yield sheet_name, int(xml_row.attrib.get("r", "0") or 0), values


def csv_rows(path: Path) -> Iterable[tuple[str, int, dict[int, str]]]:
    decoded = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            decoded = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise PrepareError(f"cannot decode CSV: {path}")
    sample = decoded[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    for row_number, row in enumerate(csv.reader(decoded.splitlines(), dialect=dialect), start=1):
        values = {index: clean(value) for index, value in enumerate(row, start=1) if clean(value)}
        if values:
            yield "CSV", row_number, values


HEADER_TERMS = frozenset({
    "bildnr", "bildnummer", "inventar", "inventarnummer", "inventory", "signatur",
    "datum", "datum_als_text", "jahr", "titel", "title", "beschreibung",
    "description", "bildtext", "text", "objekttyp", "objektart", "material",
    "technik", "masse", "format", "abmessungen", "weitere_infos",
})


def header_score(values: dict[int, str]) -> int:
    return sum(normalized_label(value) in HEADER_TERMS for value in values.values())


FIELD_ALIASES = {
    "title": {"titel", "title", "bildtitel", "objektbezeichnung", "bezeichnung"},
    "description": {"beschreibung", "description", "bildtext", "text", "erlaeuterungen"},
    "date_text": {"datum_als", "datum_als_text", "datum", "date", "jahr"},
    "object_type": {"objekttyp", "objektart", "object_type", "typ"},
    "material": {"material", "technik", "material_technik"},
    "dimensions": {"masse", "format", "abmessungen", "dimensions"},
}


def suggestions(headers: dict[int, str], values: dict[int, str]) -> dict[str, str]:
    result = {key: "" for key in FIELD_ALIASES}
    for number, header in headers.items():
        label = normalized_label(header)
        for field, aliases in FIELD_ALIASES.items():
            if label in aliases and not result[field]:
                result[field] = clean(values.get(number, ""))
    return result


INVENTORY_HEADER_ALIASES = frozenset({
    "bildnr", "bildnummer", "inventory_number", "inventarnummer", "inventar",
})


def row_inventories(headers: dict[int, str], values: dict[int, str]) -> set[str]:
    result: set[str] = set()
    for number, header in headers.items():
        if normalized_label(header) not in INVENTORY_HEADER_ALIASES:
            continue
        value = clean(values.get(number, ""))
        strict = inventories(value)
        if strict:
            result.update(strict)
            continue
        match = re.fullmatch(r"(\d{5,8}(?:[-_.]\d{1,2})?)", value)
        if match:
            result.add(canonical_inventory(match.group(1)))
    return result


def published_md() -> dict[str, dict[str, str]]:
    connection = sqlite3.connect(f"file:{PUBLISHED_DATABASE}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        result: dict[str, dict[str, str]] = {}
        for row in connection.execute(
            """
            SELECT record_key,md_object_id,inventory,title,date_text,selected_master_path
            FROM objects WHERE inventory!=''
            """
        ):
            inventory = clean(row["inventory"])
            if inventory in result:
                raise PrepareError(f"published TFA inventory is not unique: {inventory}")
            result[inventory] = {key: clean(row[key]) for key in row.keys()}
        return result
    finally:
        connection.close()


def selected_targets() -> dict[str, dict[str, str]]:
    rows = read_csv(LOCAL_ACCESSION)
    targets: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("metadata_state") != "metadata_deferred_next_pass":
            continue
        inventory = row.get("inventory", "")
        if inventory.startswith("TFA-VISUAL-"):
            continue
        if not re.fullmatch(r"TFA-\d+(?:-\d+)?", inventory):
            raise PrepareError(f"unexpected local inventory spelling: {inventory}")
        if inventory in targets:
            raise PrepareError(f"duplicate selected local inventory: {inventory}")
        if any(not row.get(field, "") for field in REQUIRED_TARGET_FIELDS):
            raise PrepareError(f"incomplete local target row: {inventory}")
        targets[inventory] = row
    return targets


def relevant_sources(targets: set[str]) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    plan = [
        row for row in read_csv(METADATA_PLAN)
        if row.get("category") == "tfa_working_metadata"
        and row.get("proposed_decision") == "proposed_accession"
        and Path(row.get("source_path", "")).suffix.casefold() in SUPPORTED_EXTENSIONS
    ]
    by_path: dict[str, dict[str, str]] = {}
    matches: dict[str, set[str]] = defaultdict(set)
    connection = sqlite3.connect(f"file:{DOCUMENT_INDEX}?mode=ro", uri=True, timeout=60)
    try:
        for row in plan:
            cached = connection.execute(
                "SELECT extracted_text FROM content_cache WHERE content_sha256=?",
                (row["source_sha256"],),
            ).fetchone()
            if cached is None:
                continue
            found = inventories(cached[0]) & targets
            if not found:
                continue
            path = row["source_path"]
            if path in by_path and by_path[path]["source_sha256"] != row["source_sha256"]:
                raise PrepareError(f"metadata plan path has conflicting hashes: {path}")
            by_path[path] = row
            matches[path].update(found)
    finally:
        connection.close()
    return by_path, matches


def candidate_rows(
    targets: dict[str, dict[str, str]],
    sources: dict[str, dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, set[str]]]:
    candidates: list[dict[str, str]] = []
    structured_matches: dict[str, set[str]] = defaultdict(set)
    md_by_inventory = published_md()
    for source_path in sorted(sources):
        plan = sources[source_path]
        path = Path(source_path)
        if not inside_source(path):
            raise PrepareError(f"metadata source escaped protected Elements root: {path}")
        stat = path.stat()
        if stat.st_size != int(plan["size_bytes"]):
            raise PrepareError(f"metadata source size drifted: {path}")
        actual_hash = sha256_file(path)
        if actual_hash != plan["source_sha256"]:
            raise PrepareError(f"metadata source hash drifted: {path}")
        parsed = list(xlsx_rows(path) if path.suffix.casefold() == ".xlsx" else csv_rows(path))
        by_sheet: dict[str, list[tuple[int, dict[int, str]]]] = defaultdict(list)
        for sheet, row_number, values in parsed:
            by_sheet[sheet].append((row_number, values))
        for sheet, rows in sorted(by_sheet.items()):
            header_candidates: list[tuple[int, int, dict[int, str]]] = []
            for row_number, values in rows:
                score = header_score(values)
                if score:
                    header_candidates.append((row_number, score, values))
                found: set[str] = set()
                matched_cells: dict[str, list[str]] = defaultdict(list)
                all_tokens: set[str] = set()
                for number, value in values.items():
                    tokens = inventories(value)
                    all_tokens.update(tokens)
                    for inventory in tokens & targets.keys():
                        found.add(inventory)
                        matched_cells[inventory].append(f"{column_name(number)}{row_number}")
                if not found:
                    continue
                eligible_headers = [item for item in header_candidates if item[0] < row_number and row_number - item[0] <= 100]
                header_number = 0
                headers: dict[int, str] = {}
                if eligible_headers:
                    header_number, _, headers = max(eligible_headers, key=lambda item: (item[1], item[0]))
                proposed = suggestions(headers, values)
                row_inventory_values = row_inventories(headers, values)
                row_inventory = ";".join(sorted(row_inventory_values))
                labelled = [
                    {
                        "cell": f"{column_name(number)}{row_number}",
                        "header": clean(headers.get(number, "")),
                        "value": value,
                    }
                    for number, value in sorted(values.items())
                ]
                raw = [
                    {"cell": f"{column_name(number)}{row_number}", "value": value}
                    for number, value in sorted(values.items())
                ]
                row_scope = "single_inventory_row" if len(all_tokens) == 1 else "multi_inventory_reference_row"
                mapped = sum(bool(value) for value in proposed.values())
                basis = (
                    "exact structured inventory row with labelled descriptive fields"
                    if row_scope == "single_inventory_row" and mapped
                    else "exact structured inventory row; descriptive fields require human mapping"
                    if row_scope == "single_inventory_row"
                    else "row contains multiple inventory tokens; use only as comparison evidence"
                )
                for inventory in sorted(found):
                    target = targets[inventory]
                    md_matches = [md_by_inventory[value] for value in sorted(row_inventory_values) if value in md_by_inventory]
                    md = md_matches[0] if len(md_matches) == 1 else {}
                    if row_scope != "single_inventory_row":
                        identity_state = "comparison_only_multi_inventory_row"
                    elif len(row_inventory_values) != 1:
                        identity_state = "row_inventory_missing_or_ambiguous"
                    elif md:
                        identity_state = "existing_md_row_inventory_requires_review"
                    elif row_inventory == inventory:
                        identity_state = "exact_local_inventory_row"
                    else:
                        identity_state = "alternate_row_inventory_requires_review"
                    seed = f"{actual_hash}|{sheet}|{row_number}|{inventory}"
                    candidate_id = "LMETA-" + hashlib.sha256(seed.encode()).hexdigest()[:20].upper()
                    candidates.append({
                        "candidate_id": candidate_id,
                        "record_key": target["record_key"],
                        "inventory": inventory,
                        "selected_master_path": target["selected_master_path"],
                        "source_path": str(path),
                        "source_sha256": actual_hash,
                        "source_size_bytes": str(stat.st_size),
                        "source_mtime_ns": str(stat.st_mtime_ns),
                        "sheet_name": sheet,
                        "row_number": str(row_number),
                        "matched_cell_references": ";".join(matched_cells[inventory]),
                        "inventory_tokens_in_row": ";".join(sorted(all_tokens)),
                        "row_scope": row_scope,
                        "header_row_number": str(header_number),
                        "row_inventory": row_inventory,
                        "identity_review_state": identity_state,
                        "md_record_key": md.get("record_key", ""),
                        "md_object_id": md.get("md_object_id", ""),
                        "md_inventory": md.get("inventory", ""),
                        "md_title": md.get("title", ""),
                        "md_date_text": md.get("date_text", ""),
                        "md_selected_master_path": md.get("selected_master_path", ""),
                        "labelled_cells_json": json.dumps(labelled, ensure_ascii=False, separators=(",", ":")),
                        "raw_cells_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                        "suggested_title": proposed["title"],
                        "suggested_description": proposed["description"],
                        "suggested_date_text": proposed["date_text"],
                        "suggested_object_type": proposed["object_type"],
                        "suggested_material": proposed["material"],
                        "suggested_dimensions": proposed["dimensions"],
                        "candidate_basis": basis,
                        "deletion_authorized": "false",
                    })
                    structured_matches[inventory].add(str(path))
    candidates.sort(key=lambda row: (
        row["inventory"], row["source_path"], row["sheet_name"], int(row["row_number"])
    ))
    ids = [row["candidate_id"] for row in candidates]
    if len(ids) != len(set(ids)):
        raise PrepareError("local metadata candidate identifiers are not unique")
    return candidates, structured_matches


def prepare() -> dict[str, Any]:
    mount = source_mount()
    for path in (CANDIDATES, RECEIPT):
        if path.exists():
            raise PrepareError(f"refusing to overwrite existing local metadata review output: {path}")
    targets = selected_targets()
    sources, prefilter = relevant_sources(set(targets))
    candidates, structured = candidate_rows(targets, sources)
    prefilter_targets = set().union(*prefilter.values()) if prefilter else set()
    candidate_targets = {row["inventory"] for row in candidates}
    if not candidates:
        raise PrepareError("structured metadata scan produced no review candidates")
    atomic_csv(CANDIDATES, CANDIDATE_FIELDS, candidates)
    if not DECISIONS.exists():
        atomic_csv(DECISIONS, DECISION_FIELDS, ())
    receipt = {
        "format": "iss-tfa-local-object-metadata-review-preparation-v2",
        "version": VERSION,
        "prepared_at": now(),
        "result": "ok",
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "published_tfa_unit_changed": False,
        "automatic_metadata_application": False,
        "deletion_authorized": False,
        "selected_local_metadata_deferred": len(targets),
        "prefilter_target_count": len(prefilter_targets),
        "structured_candidate_target_count": len(candidate_targets),
        "structured_candidate_rows": len(candidates),
        "source_files_scanned": len(sources),
        "single_inventory_candidate_rows": sum(row["row_scope"] == "single_inventory_row" for row in candidates),
        "multi_inventory_reference_rows": sum(row["row_scope"] != "single_inventory_row" for row in candidates),
        "prefilter_without_structured_row": sorted(prefilter_targets - candidate_targets),
        "candidate_inventory_counts": dict(sorted(Counter(row["inventory"] for row in candidates).items())),
        "identity_review_state_counts": dict(sorted(Counter(row["identity_review_state"] for row in candidates).items())),
        "inputs": {
            LOCAL_ACCESSION.name: "sha256:" + sha256_file(LOCAL_ACCESSION),
            METADATA_PLAN.name: "sha256:" + sha256_file(METADATA_PLAN),
            PUBLISHED_DATABASE.name: "sha256:" + sha256_file(PUBLISHED_DATABASE),
        },
        "outputs": {
            "candidates": str(CANDIDATES),
            "decisions": str(DECISIONS),
        },
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def check() -> dict[str, Any]:
    mount = source_mount()
    if not CANDIDATES.is_file() or not DECISIONS.is_file() or not RECEIPT.is_file():
        raise PrepareError("local metadata review outputs are incomplete")
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if (
        receipt.get("result") != "ok"
        or receipt.get("source_read_only") is not True
        or receipt.get("source_modified") is not False
        or receipt.get("published_tfa_unit_changed") is not False
        or receipt.get("automatic_metadata_application") is not False
        or receipt.get("deletion_authorized") is not False
    ):
        raise PrepareError("local metadata review receipt is not safe")
    candidates = read_csv(CANDIDATES)
    by_id = {row["candidate_id"]: row for row in candidates}
    if len(by_id) != len(candidates) or len(candidates) != int(receipt["structured_candidate_rows"]):
        raise PrepareError("local metadata candidate count or identifiers drifted")
    if any(row.get("deletion_authorized") != "false" for row in candidates):
        raise PrepareError("candidate row authorized deletion")
    decisions = read_csv(DECISIONS)
    seen: set[str] = set()
    for row in decisions:
        candidate = by_id.get(row.get("candidate_id", "")) if row.get("candidate_id") else None
        if row.get("record_key") in seen:
            raise PrepareError(f"duplicate local metadata decision: {row.get('record_key')}")
        if row.get("decision") not in {
            "apply_metadata", "apply_edited_metadata", "reject_noise",
            "dismiss_object", "defer",
        }:
            raise PrepareError(f"unsupported local metadata decision: {row.get('decision')}")
        if row.get("decision") in {"apply_metadata", "apply_edited_metadata", "reject_noise"}:
            if (
                candidate is None
                or candidate["record_key"] != row.get("record_key")
                or row.get("source_path") != candidate["source_path"]
                or row.get("source_sha256") != candidate["source_sha256"]
                or row.get("sheet_name") != candidate["sheet_name"]
                or row.get("row_number") != candidate["row_number"]
            ):
                raise PrepareError(f"invalid source-bound local metadata decision: {row.get('record_key')}")
        if row.get("decision") == "apply_metadata" and any(
            row.get(field, "") != candidate.get("suggested_" + field, "")
            for field in ("title", "description", "date_text", "object_type", "material", "dimensions")
        ):
            raise PrepareError(f"applied proposed metadata was edited: {row.get('record_key')}")
        if row.get("decision") in {"apply_metadata", "apply_edited_metadata"} and not row.get("title"):
            raise PrepareError(f"applied local metadata has no title: {row.get('record_key')}")
        if row.get("decision") in {"dismiss_object", "defer"} and row.get("candidate_id"):
            raise PrepareError(f"object-level metadata decision bound a source row: {row.get('record_key')}")
        if row.get("deletion_authorized") != "false":
            raise PrepareError("local metadata decision authorized deletion")
        seen.add(row["record_key"])
    return {
        "result": "ok",
        "source_mount": mount,
        "candidate_rows": len(candidates),
        "candidate_objects": len({row["record_key"] for row in candidates}),
        "decision_rows": len(decisions),
        "open_objects": len({row["record_key"] for row in candidates}) - len(decisions),
        "deletion_authorized": False,
    }


def self_test() -> None:
    assert inventories("TFA-FOT-549412") == {"TFA-549412"}
    assert inventories("TFA-FOT-549412.tif") == {"TFA-549412"}
    assert inventories("TFA-FOT-549412a") == set()
    assert inventories("TFA-FOT-549412-a") == set()
    assert inventories("TFA-FOT-5511011-21") == {"TFA-5511011-21"}
    assert suggestions({1: "Titel", 2: "Beschreibung"}, {1: "A", 2: "B"}) == {
        "title": "A", "description": "B", "date_text": "", "object_type": "",
        "material": "", "dimensions": "",
    }
    assert suggestions({1: "Datum als", 2: "Weitere Infos"}, {1: "1954", 2: "context"}) == {
        "title": "", "description": "", "date_text": "1954", "object_type": "",
        "material": "", "dimensions": "",
    }
    assert row_inventories({1: "Bildnr."}, {1: "538070"}) == {"TFA-538070"}
    assert row_inventories({1: "Bildnr."}, {1: "TFA-FOT-5511011-21"}) == {"TFA-5511011-21"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="write the bounded candidate projection")
    mode.add_argument("--check", action="store_true", help="validate an existing projection")
    mode.add_argument("--self-test", action="store_true", help="run pure parser assertions")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("self-test: ok")
        return 0
    payload = prepare() if args.apply else check()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
