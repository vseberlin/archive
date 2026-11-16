#!/usr/bin/env python3
"""Duplicate-aware metadata-source registry for the ISS archive workbench."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from xml.etree import ElementTree


REGISTRY_VERSION = "1"
TEXT_LIMIT = 2 * 1024 * 1024
ZIP_MEMBER_LIMIT = 16 * 1024 * 1024
MAX_TABULAR_COLUMNS = 512
DEFAULT_MAX_HASH_BYTES = 1024 * 1024 * 1024
METADATA_SUFFIXES = {
    ".csv",
    ".db",
    ".doc",
    ".docx",
    ".json",
    ".jsonl",
    ".lrcat",
    ".md",
    ".ods",
    ".odt",
    ".pdf",
    ".rtf",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".xmp",
    ".zip",
}
TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx"}
PLAIN_TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".rtf", ".sql", ".txt"}
IDENTIFIER_FIELDS = (
    "inventory_number",
    "inventarnummer",
    "inventory",
    "signatur",
    "object_inventory_number",
    "identifier",
)
ASSERTION_FIELDS = {
    "collection",
    "collection_name1",
    "creator",
    "description",
    "image_name1",
    "image_name_1",
    "image_rights1",
    "inventory_number",
    "material",
    "object_description",
    "object_dimensions",
    "object_material_technique",
    "object_name",
    "object_title",
    "object_type",
    "owner_previous",
    "printed_when1",
    "printed_when_1",
    "printed_who1",
    "production_where1",
    "production_who1",
    "related_actor1",
    "related_actor2",
    "related_place1",
    "related_time1",
    "title",
}
INVENTORY_RE = re.compile(r"\b[A-ZÄÖÜ]{2,12}[-_][A-Z0-9][A-Z0-9._/-]{1,48}\b")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_registry_runs (
    run_id TEXT PRIMARY KEY,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    roots_json TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    candidates_seen INTEGER NOT NULL DEFAULT 0,
    occurrences_recorded INTEGER NOT NULL DEFAULT 0,
    unique_blobs INTEGER NOT NULL DEFAULT 0,
    records_extracted INTEGER NOT NULL DEFAULT 0,
    assertions_extracted INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_registry_roots (
    root_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    root_kind TEXT NOT NULL CHECK (root_kind IN ('usb', 'archive', 'intake', 'other')),
    read_only_observed INTEGER NOT NULL CHECK (read_only_observed IN (0, 1)),
    first_observed_at_utc TEXT NOT NULL,
    last_observed_at_utc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_registry_blobs (
    blob_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    extension TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    extraction_status TEXT NOT NULL CHECK (extraction_status IN ('pending', 'extracted', 'identified', 'needs_ocr', 'error')),
    parser TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    extraction_error TEXT NOT NULL DEFAULT '',
    first_observed_at_utc TEXT NOT NULL,
    last_extracted_at_utc TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_registry_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES source_registry_roots(root_id),
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    blob_id TEXT REFERENCES source_registry_blobs(blob_id),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    observed_mtime_utc TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    hash_status TEXT NOT NULL CHECK (hash_status IN ('complete', 'deferred', 'error')),
    status TEXT NOT NULL CHECK (status IN ('present', 'missing')) DEFAULT 'present',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (root_id, relative_path)
);
CREATE TABLE IF NOT EXISTS source_registry_records (
    record_id TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL REFERENCES source_registry_blobs(blob_id),
    locator TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    entity_hint TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    extraction_status TEXT NOT NULL CHECK (extraction_status IN ('extracted', 'identified', 'needs_ocr', 'error')),
    UNIQUE (blob_id, locator)
);
CREATE TABLE IF NOT EXISTS source_registry_assertions (
    assertion_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES source_registry_records(record_id),
    entity_hint TEXT NOT NULL,
    field_name TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('observed', 'accepted', 'rejected', 'unresolved')) DEFAULT 'observed',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (record_id, field_name, raw_value)
);
CREATE TABLE IF NOT EXISTS source_registry_lineages (
    lineage_id TEXT PRIMARY KEY,
    subject_blob_id TEXT NOT NULL REFERENCES source_registry_blobs(blob_id),
    predicate TEXT NOT NULL CHECK (predicate IN ('derived_from', 'version_of', 'same_export_family')),
    object_blob_id TEXT NOT NULL REFERENCES source_registry_blobs(blob_id),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'unresolved')) DEFAULT 'unresolved',
    confidence TEXT NOT NULL CHECK (confidence IN ('certain', 'probable', 'possible', 'unknown')) DEFAULT 'unknown',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (subject_blob_id, predicate, object_blob_id)
);
CREATE INDEX IF NOT EXISTS source_registry_occurrences_blob_idx ON source_registry_occurrences(blob_id);
CREATE INDEX IF NOT EXISTS source_registry_occurrences_filename_idx ON source_registry_occurrences(original_filename);
CREATE INDEX IF NOT EXISTS source_registry_records_entity_idx ON source_registry_records(entity_hint);
CREATE INDEX IF NOT EXISTS source_registry_assertions_entity_field_idx ON source_registry_assertions(entity_hint, field_name);
CREATE INDEX IF NOT EXISTS source_registry_assertions_claim_idx ON source_registry_assertions(claim_key);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24].upper()
    return f"{prefix}-{digest}"


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())).strip("_")


def normalize_claim(value: str) -> str:
    return normalize_space(value).casefold()


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT OR REPLACE INTO source_registry_meta (key, value) VALUES ('schema_version', ?)",
        (REGISTRY_VERSION,),
    )


def clear_derived_registry(connection: sqlite3.Connection) -> None:
    """Clear only the rebuildable registry projection, never source material."""
    secure_delete = connection.execute("PRAGMA secure_delete").fetchone()[0]
    connection.execute("PRAGMA secure_delete=OFF")
    try:
        ensure_schema(connection)
        if table_exists(connection, "source_registry_fts"):
            connection.execute("DROP TABLE source_registry_fts")
        for table in (
            "source_registry_lineages",
            "source_registry_assertions",
            "source_registry_records",
            "source_registry_occurrences",
            "source_registry_blobs",
            "source_registry_roots",
            "source_registry_runs",
        ):
            connection.execute(f'DROP TABLE "{table}"')
        ensure_schema(connection)
        connection.commit()
    finally:
        connection.execute(f"PRAGMA secure_delete={int(secure_delete)}")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')", (table,)
        ).fetchone()
    )


def parse_root_spec(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError("source root must be LABEL=/absolute/path")
    root_id, raw_path = value.split("=", 1)
    root_id = normalize_header(root_id).replace("_", "-")
    path = Path(raw_path).expanduser().resolve()
    if not root_id or not path.is_dir():
        raise ValueError(f"invalid source root: {value}")
    return root_id, path


def root_kind(root_id: str, path: Path) -> str:
    text = f"{root_id} {path}".casefold()
    if "usb" in text or "/media/" in str(path):
        return "usb"
    if "intake" in text:
        return "intake"
    if "archive" in text:
        return "archive"
    return "other"


def root_is_read_only(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    try:
        return bool(os.statvfs(path).f_flag & readonly_flag)
    except OSError:
        return False


def metadata_candidate(path: Path) -> bool:
    suffix = path.suffix.casefold()
    if suffix not in METADATA_SUFFIXES:
        return False
    if path.name.startswith("~$") or path.name in {"Thumbs.db", ".DS_Store"}:
        return False
    if suffix != ".zip":
        return True
    if any(token in path.name.casefold() for token in ("csv", "xml", "metadata", "export", "catalog", "daten")):
        return True
    try:
        with zipfile.ZipFile(path) as archive:
            return any(Path(name).suffix.casefold() in METADATA_SUFFIXES - {".zip"} for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return True


def iter_candidates(root: Path) -> Iterable[Path]:
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in {"$RECYCLE.BIN", "System Volume Information", ".Trash-1000"}]
        base = Path(directory)
        for filename in files:
            path = base / filename
            if metadata_candidate(path):
                yield path


def xlsx_column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        return 0
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - 64
    return index - 1


def row_entity_hint(row: dict[str, str]) -> str:
    for field in IDENTIFIER_FIELDS:
        value = normalize_space(str(row.get(field, "")))
        if value:
            return value
    joined = " ".join(str(value) for value in row.values())
    match = INVENTORY_RE.search(joined)
    return match.group(0) if match else ""


def row_record(locator: str, kind: str, row: dict[str, str]) -> dict[str, Any]:
    cleaned = {normalize_header(str(key)): normalize_space(str(value or "")) for key, value in row.items() if key}
    entity_hint = row_entity_hint(cleaned)
    raw = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assertions = [
        (field, value)
        for field, value in cleaned.items()
        if entity_hint and field in ASSERTION_FIELDS and value
    ]
    return {
        "locator": locator,
        "record_kind": kind,
        "entity_hint": entity_hint,
        "raw_text": raw,
        "normalized_text": normalize_space(" ".join(cleaned.values())),
        "assertions": assertions,
    }


def read_csv_records(path: Path) -> list[dict[str, Any]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            handle = path.open("r", encoding=encoding, newline="")
            sample = handle.read(16384)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel_tab if path.suffix.casefold() == ".tsv" else csv.excel
            records = [
                row_record(f"row:{number}", "tabular_row", dict(row))
                for number, row in enumerate(csv.DictReader(handle, dialect=dialect), start=2)
            ]
            handle.close()
            return records
        except UnicodeDecodeError:
            try:
                handle.close()
            except UnboundLocalError:
                pass
    raise ValueError(f"cannot decode CSV: {path}")


def read_xlsx_records(path: Path) -> list[dict[str, Any]]:
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    document_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    namespace = {"m": spreadsheet_ns}
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{spreadsheet_ns}}}si"):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{spreadsheet_ns}}}t")))
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{package_rel_ns}}}Relationship")
        }
        for sheet in workbook.findall(f".//{{{spreadsheet_ns}}}sheet"):
            target = targets[sheet.attrib[f"{{{document_rel_ns}}}id"]].lstrip("/")
            worksheet_path = target if target.startswith("xl/") else f"xl/{target}"
            worksheet = ElementTree.fromstring(archive.read(worksheet_path))
            rows: list[tuple[int, list[str]]] = []
            for xml_row in worksheet.findall(".//m:sheetData/m:row", namespace):
                values: list[str] = []
                for cell in xml_row.findall("m:c", namespace):
                    index = xlsx_column_index(cell.attrib.get("r", "A1"))
                    if index >= MAX_TABULAR_COLUMNS:
                        continue
                    while len(values) <= index:
                        values.append("")
                    value_node = cell.find("m:v", namespace)
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(f"{{{spreadsheet_ns}}}t"))
                    elif value_node is None:
                        value = ""
                    elif cell_type == "s":
                        value = shared_strings[int(value_node.text or "0")]
                    else:
                        value = value_node.text or ""
                    values[index] = value
                rows.append((int(xml_row.attrib.get("r", len(rows) + 1)), values))
            if not rows:
                continue
            headers = [normalize_header(value) or f"column_{index + 1}" for index, value in enumerate(rows[0][1])]
            sheet_name = sheet.attrib.get("name", "Sheet")
            for row_number, values in rows[1:]:
                row = {header: values[index] if index < len(values) else "" for index, header in enumerate(headers)}
                if any(str(value).strip() for value in row.values()):
                    records.append(row_record(f"sheet:{sheet_name}/row:{row_number}", "tabular_row", row))
    return records


def xml_records_from_bytes(content: bytes, locator_prefix: str = "document") -> list[dict[str, Any]]:
    root = ElementTree.fromstring(content)
    children = list(root)
    candidates = children if children and all(list(child) for child in children[: min(5, len(children))]) else [root]
    records: list[dict[str, Any]] = []
    for index, element in enumerate(candidates, start=1):
        row = {normalize_header(child.tag.split("}")[-1]): normalize_space(" ".join(child.itertext())) for child in list(element)}
        if row:
            records.append(row_record(f"{locator_prefix}/record:{index}", "xml_record", row))
        else:
            text = normalize_space(" ".join(element.itertext()))
            if text:
                hint = INVENTORY_RE.search(text)
                records.append({
                    "locator": f"{locator_prefix}/record:{index}",
                    "record_kind": "xml_text",
                    "entity_hint": hint.group(0) if hint else "",
                    "raw_text": text,
                    "normalized_text": text,
                    "assertions": [],
                })
    return records


def json_records_from_bytes(content: bytes, locator_prefix: str = "document") -> list[dict[str, Any]]:
    payload = json.loads(content.decode("utf-8-sig"))
    values: list[Any]
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        values = payload["results"]
    else:
        values = [payload]
    records = []
    for index, value in enumerate(values, start=1):
        if isinstance(value, dict):
            records.append(row_record(f"{locator_prefix}/record:{index}", "json_record", {str(k): str(v) if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False, sort_keys=True) for k, v in value.items()}))
        else:
            text = normalize_space(str(value))
            records.append({"locator": f"{locator_prefix}/record:{index}", "record_kind": "json_value", "entity_hint": "", "raw_text": text, "normalized_text": text, "assertions": []})
    return records


def text_record(text: str, locator: str, kind: str) -> list[dict[str, Any]]:
    text = normalize_space(text[:TEXT_LIMIT])
    if not text:
        return []
    hint = INVENTORY_RE.search(text)
    return [{
        "locator": locator,
        "record_kind": kind,
        "entity_hint": hint.group(0) if hint else "",
        "raw_text": text,
        "normalized_text": text,
        "assertions": [],
    }]


def zip_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            suffix = Path(member.filename).suffix.casefold()
            if member.is_dir() or suffix not in {".csv", ".json", ".jsonl", ".md", ".txt", ".xml", ".xmp"} or member.file_size > ZIP_MEMBER_LIMIT:
                continue
            content = archive.read(member)
            prefix = f"zip:{PurePosixPath(member.filename)}"
            try:
                if suffix in {".xml", ".xmp"}:
                    records.extend(xml_records_from_bytes(content, prefix))
                elif suffix in {".json", ".jsonl"}:
                    records.extend(json_records_from_bytes(content, prefix))
                else:
                    records.extend(text_record(content.decode("utf-8-sig", errors="replace"), prefix, "zip_text"))
            except (ElementTree.ParseError, json.JSONDecodeError, UnicodeDecodeError):
                records.extend(text_record(content.decode("utf-8", errors="replace"), prefix, "zip_text"))
    return records


def extract_records(path: Path, extract_pdf: bool = False) -> tuple[str, str, list[dict[str, Any]], str]:
    suffix = path.suffix.casefold()
    try:
        if suffix in {".csv", ".tsv"}:
            return "extracted", "python-csv", read_csv_records(path), ""
        if suffix == ".xlsx":
            return "extracted", "python-xlsx-xml", read_xlsx_records(path), ""
        if suffix in {".xml", ".xmp"}:
            return "extracted", "python-xml", xml_records_from_bytes(path.read_bytes()), ""
        if suffix == ".json":
            return "extracted", "python-json", json_records_from_bytes(path.read_bytes()), ""
        if suffix == ".jsonl":
            records: list[dict[str, Any]] = []
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                lines = handle.read(TEXT_LIMIT).splitlines()
            for line_number, line in enumerate(lines, start=1):
                if line.strip():
                    records.extend(json_records_from_bytes(line.encode("utf-8"), f"line:{line_number}"))
            return "extracted", "python-jsonl", records, ""
        if suffix in PLAIN_TEXT_SUFFIXES:
            with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                text = handle.read(TEXT_LIMIT)
            return "extracted", "plain-text", text_record(text, "document", "text"), ""
        if suffix in {".docx", ".odt"}:
            member = "word/document.xml" if suffix == ".docx" else "content.xml"
            with zipfile.ZipFile(path) as archive:
                root = ElementTree.fromstring(archive.read(member))
            return "extracted", "office-xml", text_record(" ".join(root.itertext()), "document", "office_text"), ""
        if suffix == ".zip":
            return "extracted", "zip-container", zip_records(path), ""
        if suffix == ".pdf":
            if not extract_pdf:
                return "identified", "candidate-only", [], "PDF text extraction deferred"
            result = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, timeout=60, check=False)
            records = text_record(result.stdout.decode("utf-8", errors="replace"), "document", "pdf_text")
            return ("extracted", "pdftotext", records, "") if records else ("needs_ocr", "pdftotext", [], "no embedded text")
        return "identified", "candidate-only", [], "parser not yet implemented"
    except (OSError, ValueError, LookupError, UnicodeDecodeError, zipfile.BadZipFile, ElementTree.ParseError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        return "error", "", [], f"{type(error).__name__}: {error}"


def observed_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def upsert_record(connection: sqlite3.Connection, blob_id: str, record: dict[str, Any]) -> tuple[int, int]:
    raw_text = str(record["raw_text"])
    record_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    record_id = stable_id("REC", blob_id, str(record["locator"]))
    connection.execute(
        """INSERT INTO source_registry_records (
               record_id, blob_id, locator, record_kind, entity_hint, raw_text,
               normalized_text, record_sha256, extraction_status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extracted')
           ON CONFLICT(blob_id, locator) DO UPDATE SET
               record_kind=excluded.record_kind, entity_hint=excluded.entity_hint,
               raw_text=excluded.raw_text, normalized_text=excluded.normalized_text,
               record_sha256=excluded.record_sha256, extraction_status='extracted'""",
        (record_id, blob_id, record["locator"], record["record_kind"], record["entity_hint"], raw_text, record["normalized_text"], record_hash),
    )
    assertions = 0
    for field_name, raw_value in record.get("assertions", []):
        normalized = normalize_claim(raw_value)
        claim_key = hashlib.sha256(f"{normalize_claim(record['entity_hint'])}\x1f{field_name}\x1f{normalized}".encode("utf-8")).hexdigest()
        assertion_id = stable_id("AST", record_id, field_name, raw_value)
        connection.execute(
            """INSERT INTO source_registry_assertions (
                   assertion_id, record_id, entity_hint, field_name, raw_value,
                   normalized_value, claim_key, status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'observed')
               ON CONFLICT(record_id, field_name, raw_value) DO NOTHING""",
            (assertion_id, record_id, record["entity_hint"], field_name, raw_value, normalized, claim_key),
        )
        assertions += 1
    return 1, assertions


def scan_registry(
    connection: sqlite3.Connection,
    root_specs: list[tuple[str, Path]],
    actor: str,
    note: str = "",
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    defer_hash_suffixes: set[str] | None = None,
    extract_pdf: bool = False,
    output_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    ensure_schema(connection)
    deferred_suffixes = {
        suffix.casefold() if suffix.startswith(".") else f".{suffix.casefold()}"
        for suffix in (defer_hash_suffixes or set())
    }
    started = utc_now()
    connection.execute(
        """UPDATE source_registry_runs
           SET completed_at_utc=?, status='failed',
               note=CASE WHEN note='' THEN 'interrupted before a later scan'
                         ELSE note || ' | interrupted before a later scan' END
           WHERE status='running'""",
        (started,),
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + hashlib.sha256("|".join(str(path) for _, path in root_specs).encode()).hexdigest()[:8].upper()
    roots_payload = [{"root_id": root_id, "path": str(path)} for root_id, path in root_specs]
    connection.execute(
        "INSERT INTO source_registry_runs (run_id, started_at_utc, actor, status, roots_json, note) VALUES (?, ?, ?, 'running', ?, ?)",
        (run_id, started, actor, json.dumps(roots_payload, ensure_ascii=False, sort_keys=True), note),
    )
    connection.commit()
    counts = {"files_seen": 0, "candidates_seen": 0, "occurrences_recorded": 0, "records_extracted": 0, "assertions_extracted": 0, "errors": 0}
    errors: list[dict[str, str]] = []
    for root_id, source_root in root_specs:
        if progress:
            progress(f"source-registry: scanning {root_id} ({source_root})")
        now = utc_now()
        connection.execute(
            """INSERT INTO source_registry_roots (
                   root_id, root_path, root_kind, read_only_observed,
                   first_observed_at_utc, last_observed_at_utc, note
               ) VALUES (?, ?, ?, ?, ?, ?, '')
               ON CONFLICT(root_id) DO UPDATE SET root_path=excluded.root_path,
                   root_kind=excluded.root_kind, read_only_observed=excluded.read_only_observed,
                   last_observed_at_utc=excluded.last_observed_at_utc""",
            (root_id, str(source_root), root_kind(root_id, source_root), int(root_is_read_only(source_root)), now, now),
        )
        for path in iter_candidates(source_root):
            counts["files_seen"] += 1
            counts["candidates_seen"] += 1
            relative = path.relative_to(source_root).as_posix()
            if progress and (counts["candidates_seen"] == 1 or counts["candidates_seen"] % 25 == 0):
                progress(
                    f"source-registry: candidate {counts['candidates_seen']} "
                    f"in {root_id}: {relative}"
                )
            occurrence_id = stable_id("OCC", root_id, relative)
            try:
                stat = path.stat()
                defer_reason = ""
                if path.suffix.casefold() in deferred_suffixes:
                    defer_reason = f"hash deferred by suffix {path.suffix.casefold()}"
                elif stat.st_size > max_hash_bytes:
                    defer_reason = f"larger than hash limit {max_hash_bytes}"
                if defer_reason:
                    connection.execute(
                        """INSERT INTO source_registry_occurrences (
                               occurrence_id, root_id, relative_path, original_filename,
                               blob_id, size_bytes, observed_mtime_utc, observed_at_utc,
                               hash_status, status, note
                           ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'deferred', 'present', ?)
                           ON CONFLICT(root_id, relative_path) DO UPDATE SET
                               blob_id=NULL, size_bytes=excluded.size_bytes,
                               observed_mtime_utc=excluded.observed_mtime_utc,
                               observed_at_utc=excluded.observed_at_utc, hash_status='deferred',
                               status='present', note=excluded.note""",
                        (occurrence_id, root_id, relative, path.name, stat.st_size, observed_mtime(path), utc_now(), defer_reason),
                    )
                    counts["occurrences_recorded"] += 1
                    if counts["candidates_seen"] % 100 == 0:
                        connection.commit()
                    continue
                digest = sha256_file(path)
                blob_id = "sha256:" + digest
                existing = connection.execute("SELECT extraction_status FROM source_registry_blobs WHERE blob_id = ?", (blob_id,)).fetchone()
                connection.execute(
                    """INSERT INTO source_registry_blobs (
                           blob_id, sha256, size_bytes, extension, source_kind,
                           extraction_status, first_observed_at_utc
                       ) VALUES (?, ?, ?, ?, 'metadata', 'pending', ?)
                       ON CONFLICT(blob_id) DO NOTHING""",
                    (blob_id, digest, stat.st_size, path.suffix.casefold(), utc_now()),
                )
                connection.execute(
                    """INSERT INTO source_registry_occurrences (
                           occurrence_id, root_id, relative_path, original_filename,
                           blob_id, size_bytes, observed_mtime_utc, observed_at_utc,
                           hash_status, status, note
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'complete', 'present', '')
                       ON CONFLICT(root_id, relative_path) DO UPDATE SET
                           blob_id=excluded.blob_id, size_bytes=excluded.size_bytes,
                           observed_mtime_utc=excluded.observed_mtime_utc,
                           observed_at_utc=excluded.observed_at_utc, hash_status='complete',
                           status='present', note=''""",
                    (occurrence_id, root_id, relative, path.name, blob_id, stat.st_size, observed_mtime(path), utc_now()),
                )
                counts["occurrences_recorded"] += 1
                should_extract = (
                    not existing
                    or existing["extraction_status"] in {"pending", "error"}
                    or (
                        extract_pdf
                        and path.suffix.casefold() == ".pdf"
                        and existing["extraction_status"] == "identified"
                    )
                )
                if should_extract:
                    status, parser, records, error = extract_records(path, extract_pdf=extract_pdf)
                    connection.execute(
                        """UPDATE source_registry_blobs SET extraction_status=?, parser=?,
                               parser_version=?, extraction_error=?, last_extracted_at_utc=?
                           WHERE blob_id=?""",
                        (status, parser, REGISTRY_VERSION, error, utc_now(), blob_id),
                    )
                    if status == "error":
                        counts["errors"] += 1
                        errors.append({"root_id": root_id, "relative_path": relative, "error": error})
                    for record in records:
                        record_count, assertion_count = upsert_record(connection, blob_id, record)
                        counts["records_extracted"] += record_count
                        counts["assertions_extracted"] += assertion_count
                if should_extract or counts["candidates_seen"] % 100 == 0:
                    connection.commit()
            except (OSError, sqlite3.Error, ValueError) as error:
                counts["errors"] += 1
                errors.append({"root_id": root_id, "relative_path": relative, "error": f"{type(error).__name__}: {error}"})
        connection.commit()
    unique_blobs = connection.execute("SELECT COUNT(*) FROM source_registry_blobs").fetchone()[0]
    duplicate_groups = connection.execute(
        "SELECT COUNT(*) FROM (SELECT blob_id FROM source_registry_occurrences WHERE blob_id IS NOT NULL GROUP BY blob_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    conflict_groups = connection.execute(
        """SELECT COUNT(*) FROM (
               SELECT entity_hint, field_name FROM source_registry_assertions
               WHERE entity_hint != '' GROUP BY entity_hint, field_name
               HAVING COUNT(DISTINCT normalized_value) > 1
           )"""
    ).fetchone()[0]
    hashes_deferred = connection.execute(
        "SELECT COUNT(*) FROM source_registry_occurrences WHERE hash_status = 'deferred'"
    ).fetchone()[0]
    completed = utc_now()
    connection.execute(
        """UPDATE source_registry_runs SET completed_at_utc=?, status='complete',
               files_seen=?, candidates_seen=?, occurrences_recorded=?, unique_blobs=?,
               records_extracted=?, assertions_extracted=?, errors=? WHERE run_id=?""",
        (completed, counts["files_seen"], counts["candidates_seen"], counts["occurrences_recorded"], unique_blobs, counts["records_extracted"], counts["assertions_extracted"], counts["errors"], run_id),
    )
    connection.commit()
    result = {
        "result": "pass",
        "run_id": run_id,
        **counts,
        "unique_blobs": unique_blobs,
        "duplicate_groups": duplicate_groups,
        "conflict_groups": conflict_groups,
        "hashes_deferred": hashes_deferred,
        "roots": roots_payload,
    }
    if output_dir:
        run_dir = output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with (run_dir / "errors.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("root_id", "relative_path", "error"))
            writer.writeheader()
            writer.writerows(errors)
        with (run_dir / "duplicate-groups.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("blob_id", "size_bytes", "occurrences", "paths"))
            for row in connection.execute(
                """SELECT b.blob_id, b.size_bytes, COUNT(*) occurrences,
                          GROUP_CONCAT(o.root_id || ':' || o.relative_path, ' | ') paths
                   FROM source_registry_blobs b JOIN source_registry_occurrences o ON o.blob_id=b.blob_id
                   GROUP BY b.blob_id HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, b.blob_id"""
            ):
                writer.writerow(tuple(row))
        result["output"] = str(run_dir)
    return result
