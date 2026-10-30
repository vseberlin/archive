#!/usr/bin/env python3
"""Shared, resumable document inventory for archive-unit reconciliation."""

from __future__ import annotations

import argparse
import csv
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


VERSION = "1.1.0"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/document-index")
DATABASE = WORK_ROOT / "document-index.sqlite"
SUMMARY = WORK_ROOT / "document-index-summary.json"
KNOWN_DOCUMENTS = WORK_ROOT / "known-unit-documents.csv"
SCAN_RUNS_CSV = WORK_ROOT / "scan-runs.csv"
EXTRACTION_LOG = WORK_ROOT / "document-extraction.log"
EXTRACTION_ERRORS = WORK_ROOT / "document-extraction-errors.csv"
EXTRACTION_PROGRESS = WORK_ROOT / "document-extraction-progress.json"
EXTRACTION_LOCK = WORK_ROOT / ".document-extraction.lock"
SOURCE_ROOT = Path("/media/vladimir/Elements")
OLD_ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
WHOLE_USB_VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
UNITS_ROOT = Path("/mnt/volume/iss-archive/units")
DOCUMENT_SUFFIXES = {
    ".csv", ".doc", ".docx", ".json", ".jsonl", ".md", ".ods", ".odt",
    ".pdf", ".ppt", ".pptx", ".rtf", ".tsv", ".txt", ".xls", ".xlsm",
    ".xlsx", ".xml", ".xmp", ".zip",
}
IGNORED_DIRECTORIES = {"$RECYCLE.BIN", "System Volume Information", ".Trash-1000"}
TEXT_LIMIT = 4_000_000
PLAIN_TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".tsv", ".txt", ".xml", ".xmp"}
OOXML_SUFFIXES = {".docx", ".xlsx", ".xlsm", ".pptx"}
ODF_SUFFIXES = {".ods", ".odt"}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
  scan_path TEXT NOT NULL,
  files_seen INTEGER NOT NULL DEFAULT 0,
  document_files INTEGER NOT NULL DEFAULT 0,
  new_rows INTEGER NOT NULL DEFAULT 0,
  changed_rows INTEGER NOT NULL DEFAULT 0,
  unchanged_rows INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  source_root TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  source_path TEXT NOT NULL UNIQUE,
  filename TEXT NOT NULL,
  extension TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  content_sha256 TEXT NOT NULL DEFAULT '',
  discovery_basis TEXT NOT NULL,
  observed_state TEXT NOT NULL CHECK(observed_state IN ('seeded_unverified','present','missing')),
  extraction_state TEXT NOT NULL CHECK(extraction_state IN (
    'pending','skipped_known_unit','header_extracted','text_extracted','needs_ocr','error'
  )),
  parser TEXT NOT NULL DEFAULT '',
  header_text TEXT NOT NULL DEFAULT '',
  extraction_error TEXT NOT NULL DEFAULT '',
  last_observed_at TEXT NOT NULL,
  UNIQUE(source_root, relative_path)
);
CREATE INDEX IF NOT EXISTS documents_extension_idx ON documents(extension);
CREATE INDEX IF NOT EXISTS documents_sha_idx ON documents(content_sha256);
CREATE INDEX IF NOT EXISTS documents_state_idx ON documents(extraction_state);
CREATE TABLE IF NOT EXISTS content_cache (
  content_sha256 TEXT PRIMARY KEY,
  extension TEXT NOT NULL,
  parser TEXT NOT NULL,
  extraction_state TEXT NOT NULL CHECK(extraction_state IN (
    'header_extracted','text_extracted','needs_ocr','error'
  )),
  extracted_text TEXT NOT NULL DEFAULT '',
  extraction_error TEXT NOT NULL DEFAULT '',
  extracted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_links (
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  unit TEXT NOT NULL,
  relation_state TEXT NOT NULL CHECK(relation_state IN (
    'accessioned','reconciled_redundant','reviewed_not_accessioned',
    'contextual_open','cross_unit_open'
  )),
  reconciliation_needed INTEGER NOT NULL CHECK(reconciliation_needed IN (0,1)),
  source_manifest TEXT NOT NULL,
  basis TEXT NOT NULL,
  PRIMARY KEY(document_id, unit)
);
CREATE INDEX IF NOT EXISTS unit_links_state_idx
ON unit_links(unit, reconciliation_needed, relation_state);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
  document_id UNINDEXED, filename, relative_path, header_text
);
"""


class IndexError(RuntimeError):
    pass


class SafetyError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(value: str) -> str:
    return "DOC-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32].upper()


def connect() -> sqlite3.Connection:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','1')")
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('tool_version',?)", (VERSION,))
    connection.commit()
    return connection


def source_mount() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise IndexError("Elements mount cannot be resolved")
    filesystems = json.loads(result.stdout).get("filesystems", [])
    if len(filesystems) != 1:
        raise IndexError("Elements mount resolution is ambiguous")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise IndexError(f"Elements is not protected ntfs3 read-only: {mount}")
    return {
        "source": str(mount.get("source", "")),
        "target": str(mount.get("target", "")),
        "fstype": str(mount.get("fstype", "")),
        "options": str(mount.get("options", "")),
    }


def require_safe_outputs() -> None:
    root = WORK_ROOT.resolve(strict=True)
    for path in (
        DATABASE, SUMMARY, KNOWN_DOCUMENTS, SCAN_RUNS_CSV, EXTRACTION_LOG,
        EXTRACTION_ERRORS, EXTRACTION_PROGRESS, EXTRACTION_LOCK,
    ):
        if path.parent.resolve(strict=True) != root:
            raise SafetyError(f"output escapes approved work root: {path}")
    usage = shutil.disk_usage(root)
    if usage.free < 2 * 1024**3:
        raise SafetyError(f"less than 2 GiB free in approved work root: {usage.free}")


def log_extract(message: str) -> None:
    line = f"{now()} {message}\n"
    with EXTRACTION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def decode_text(data: bytes) -> str:
    encodings = ["utf-8-sig"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(("cp1252", "latin-1"))
    for encoding in encodings:
        try:
            return data.decode(encoding)[:TEXT_LIMIT]
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace")[:TEXT_LIMIT]


def normalized_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value.strip()[:TEXT_LIMIT]


def run_text_command(command: list[str], timeout: int) -> str:
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    if result.returncode != 0:
        detail = decode_text(result.stderr).strip().replace("\n", " ")[:500]
        raise RuntimeError(f"command failed ({result.returncode}): {detail}")
    return normalized_text(decode_text(result.stdout))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def stat_signature(path: Path) -> tuple[int, int]:
    try:
        value = path.stat()
    except OSError as error:
        if error.errno in {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ESTALE}:
            raise SafetyError(f"source device/read integrity error at {path}: {error}") from error
        raise
    return value.st_size, value.st_mtime_ns


def checked_source_path(raw_path: str) -> Path:
    path = Path(raw_path).resolve(strict=True)
    try:
        path.relative_to(SOURCE_ROOT.resolve(strict=True))
    except ValueError as error:
        raise SafetyError(f"document resolves outside read-only Elements: {path}") from error
    return path


def xml_zip_text(path: Path, extension: str) -> str:
    selected: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if extension == ".docx":
            wanted = [name for name in names if name == "word/document.xml" or name.startswith("word/header")]
        elif extension in {".xlsx", ".xlsm"}:
            wanted = [name for name in names if name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/sheet")]
        elif extension == ".pptx":
            wanted = [name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
        else:
            wanted = [name for name in names if name == "content.xml"]
        for name in wanted:
            if sum(len(item) for item in selected) >= TEXT_LIMIT:
                break
            with archive.open(name) as member:
                data = member.read(TEXT_LIMIT * 2)
            text = re.sub(r"<[^>]+>", " ", decode_text(data))
            selected.append(text)
    return normalized_text("\n".join(selected))


def zip_listing(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        lines: list[str] = []
        length = 0
        for item in archive.infolist():
            line = f"{item.filename}\t{item.file_size}\t{item.compress_size}"
            lines.append(line)
            length += len(line) + 1
            if length >= TEXT_LIMIT:
                break
    return normalized_text("\n".join(lines))


def binary_strings(path: Path) -> str:
    with path.open("rb") as handle:
        data = handle.read(64 * 1024 * 1024)
    ascii_parts = [item.decode("cp1252", errors="replace") for item in re.findall(rb"[\x20-\x7e\x80-\xff]{4,}", data)]
    utf16_parts = [item.decode("utf-16le", errors="replace") for item in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", data)]
    return normalized_text("\n".join(ascii_parts + utf16_parts))


def pdf_page_count(path: Path) -> int:
    output = run_text_command(["pdfinfo", str(path)], timeout=60)
    match = re.search(r"^Pages:\s+(\d+)", output, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("PDF page count unavailable")
    return int(match.group(1))


def ocr_pdf_headers(path: Path, pages: int) -> str:
    texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ocr-", dir=WORK_ROOT) as temporary:
        temp = Path(temporary)
        for page in range(1, pages + 1):
            output = temp / f"page-{page}"
            render = subprocess.run(
                [
                    "pdftocairo", "-f", str(page), "-l", str(page),
                    "-singlefile", "-r", "200", "-gray", "-png",
                    str(path), str(output),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=120, check=False,
            )
            image = output.with_suffix(".png")
            if render.returncode != 0 or not image.is_file():
                detail = decode_text(render.stderr).strip().replace("\n", " ")[:500]
                raise RuntimeError(f"PDF rendering failed on page {page}: {detail}")
            text = run_text_command(
                ["tesseract", str(image), "stdout", "-l", "deu+eng"],
                timeout=180,
            )
            texts.append(f"[page {page}]\n{text}")
    return normalized_text("\n\n".join(texts))


def extract_content(path: Path, extension: str, ocr_pages: int) -> tuple[str, str, str]:
    if extension in PLAIN_TEXT_SUFFIXES:
        with path.open("rb") as handle:
            return "text_extracted", "plain-text", normalized_text(decode_text(handle.read(TEXT_LIMIT * 2)))
    if extension == ".pdf":
        pages = min(ocr_pages, pdf_page_count(path))
        text = run_text_command(
            ["pdftotext", "-f", "1", "-l", str(pages), "-layout", str(path), "-"],
            timeout=180,
        )
        if len(text) >= 80:
            return "header_extracted", f"pdftotext:first-{pages}", text
        ocr = ocr_pdf_headers(path, pages)
        if ocr:
            return "header_extracted", f"tesseract-deu+eng:first-{pages}", ocr
        return "needs_ocr", f"tesseract-deu+eng:first-{pages}", ""
    if extension in OOXML_SUFFIXES | ODF_SUFFIXES:
        return "text_extracted", "office-xml", xml_zip_text(path, extension)
    if extension == ".zip":
        return "header_extracted", "zip-member-list", zip_listing(path)
    if extension == ".doc":
        if shutil.which("antiword"):
            return "text_extracted", "antiword", run_text_command(["antiword", str(path)], timeout=120)
        return "header_extracted", "binary-strings", binary_strings(path)
    if extension in {".xls", ".ods"}:
        return "text_extracted", "xls2csv", run_text_command(["xls2csv", str(path)], timeout=120)
    if extension in {".ppt", ".rtf"}:
        return "header_extracted", "binary-strings", binary_strings(path)
    raise RuntimeError(f"no approved extractor for {extension}")


def normalize_source_path(raw: str) -> tuple[str, str, str]:
    value = str(raw).strip()
    elements_prefix = str(SOURCE_ROOT) + "/"
    archive_prefix = "/mnt/volume/archive/"
    if value.startswith(elements_prefix):
        relative = value[len(elements_prefix):]
        return "elements", relative, value
    if value.startswith(archive_prefix):
        relative = "restore/archive/" + value[len(archive_prefix):]
        return "elements", relative, str(SOURCE_ROOT / relative)
    return "external-evidence", value, value


def normalize_sha(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else ""


def upsert_document(
    connection: sqlite3.Connection,
    *,
    source_root: str,
    relative_path: str,
    source_path: str,
    filename: str,
    extension: str,
    size_bytes: int,
    mtime_ns: int,
    content_sha256: str,
    discovery_basis: str,
    observed_state: str,
) -> str:
    document_id = stable_id(f"{source_root}\x1f{relative_path}")
    connection.execute(
        """
        INSERT INTO documents (
          document_id,source_root,relative_path,source_path,filename,extension,
          size_bytes,mtime_ns,content_sha256,discovery_basis,observed_state,
          extraction_state,last_observed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)
        ON CONFLICT(document_id) DO UPDATE SET
          source_path=excluded.source_path,filename=excluded.filename,
          extension=excluded.extension,
          size_bytes=CASE
            WHEN documents.observed_state='present' AND excluded.observed_state!='present'
              THEN documents.size_bytes ELSE excluded.size_bytes END,
          mtime_ns=CASE
            WHEN documents.observed_state='present' AND excluded.observed_state!='present'
              THEN documents.mtime_ns
            WHEN excluded.mtime_ns!=0 THEN excluded.mtime_ns ELSE documents.mtime_ns END,
          content_sha256=CASE WHEN excluded.content_sha256!='' THEN excluded.content_sha256 ELSE documents.content_sha256 END,
          discovery_basis=CASE
            WHEN instr(documents.discovery_basis,excluded.discovery_basis)>0 THEN documents.discovery_basis
            ELSE documents.discovery_basis||';'||excluded.discovery_basis END,
          observed_state=CASE
            WHEN excluded.observed_state='present' THEN 'present' ELSE documents.observed_state END,
          last_observed_at=CASE
            WHEN documents.observed_state='present' AND excluded.observed_state!='present'
              THEN documents.last_observed_at ELSE excluded.last_observed_at END
        """,
        (
            document_id, source_root, relative_path, source_path, filename,
            extension, max(0, int(size_bytes)), max(0, int(mtime_ns)),
            normalize_sha(content_sha256), discovery_basis, observed_state, now(),
        ),
    )
    return document_id


def seed_archive_index(connection: sqlite3.Connection) -> int:
    if not OLD_ARCHIVE_INDEX.is_file():
        raise IndexError(f"missing prior archive index: {OLD_ARCHIVE_INDEX}")
    source = sqlite3.connect(f"file:{OLD_ARCHIVE_INDEX}?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    suffixes = tuple(sorted(suffix.lstrip(".") for suffix in DOCUMENT_SUFFIXES))
    placeholders = ",".join("?" for _ in suffixes)
    count = 0
    for row in source.execute(
        f"SELECT path,basename,extension,size,mtime_ns FROM files WHERE extension IN ({placeholders})",
        suffixes,
    ):
        root_id, relative, current_path = normalize_source_path(row["path"])
        upsert_document(
            connection,
            source_root=root_id,
            relative_path=relative,
            source_path=current_path,
            filename=row["basename"],
            extension="." + str(row["extension"]).casefold().lstrip("."),
            size_bytes=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            content_sha256="",
            discovery_basis="archive-restoration-index-20260709",
            observed_state="seeded_unverified",
        )
        count += 1
        if count % 1000 == 0:
            connection.commit()
    source.close()
    connection.commit()
    return count


def relation_for(unit: str, row: dict[str, str]) -> tuple[str, int]:
    status = row.get("preservation_status", "")
    if unit == "wf-sender":
        if status.startswith("preserved_"):
            return "accessioned", 0
        if status in {
            "covered_carrier_derivative_candidate",
            "reviewed_superseded_or_derivative",
            "reviewed_workflow_noise",
        }:
            return "reconciled_redundant", 0
        if status == "excluded_cross_unit_trafo":
            return "cross_unit_open", 1
        return "contextual_open", 1
    if unit == "trafo":
        if status.startswith("preserved_"):
            return "accessioned", 0
        return "contextual_open", 1
    if unit == "kurt-schwarz":
        if status == "accessioned_documentation":
            return "accessioned", 0
        if status == "reviewed_not_accessioned":
            return "reviewed_not_accessioned", 0
        return "contextual_open", 1
    raise IndexError(f"unsupported completed unit: {unit}")


def seed_unit_manifest(connection: sqlite3.Connection, unit: str) -> int:
    manifest = UNITS_ROOT / unit / "db-restore/manifests/source-disposition.csv"
    if not manifest.is_file():
        raise IndexError(f"missing published source disposition: {manifest}")
    count = 0
    with manifest.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            raw_path = row.get("source_path", "")
            suffix = Path(raw_path).suffix.casefold()
            if suffix not in DOCUMENT_SUFFIXES:
                continue
            root_id, relative, current_path = normalize_source_path(raw_path)
            document_id = upsert_document(
                connection,
                source_root=root_id,
                relative_path=relative,
                source_path=current_path,
                filename=Path(raw_path).name,
                extension=suffix,
                size_bytes=int(row.get("source_size_bytes") or 0),
                mtime_ns=0,
                content_sha256=row.get("source_sha256", ""),
                discovery_basis=f"published-{unit}-source-disposition",
                observed_state="seeded_unverified",
            )
            relation_state, reconciliation_needed = relation_for(unit, row)
            connection.execute(
                """
                INSERT INTO unit_links (
                  document_id,unit,relation_state,reconciliation_needed,source_manifest,basis
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(document_id,unit) DO UPDATE SET
                  relation_state=excluded.relation_state,
                  reconciliation_needed=excluded.reconciliation_needed,
                  source_manifest=excluded.source_manifest,basis=excluded.basis
                """,
                (
                    document_id, unit, relation_state, reconciliation_needed,
                    str(manifest),
                    f"{row.get('preservation_status','')}; {row.get('preservation_basis','')}",
                ),
            )
            count += 1
            if count % 1000 == 0:
                connection.commit()
    connection.execute(
        """
        UPDATE documents SET extraction_state='skipped_known_unit'
        WHERE document_id IN (
          SELECT document_id FROM unit_links
          GROUP BY document_id
          HAVING MIN(reconciliation_needed)=0 AND MAX(reconciliation_needed)=0
        ) AND extraction_state='pending'
        """
    )
    connection.commit()
    return count


def refresh_fts(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM document_fts")
    connection.execute(
        """
        INSERT INTO document_fts(document_id,filename,relative_path,header_text)
        SELECT document_id,filename,relative_path,header_text FROM documents
        """
    )
    connection.commit()


def write_outputs(connection: sqlite3.Connection, mount: dict[str, str]) -> dict[str, object]:
    by_state = {
        row["extraction_state"]: row["n"]
        for row in connection.execute(
            "SELECT extraction_state,COUNT(*) n FROM documents GROUP BY extraction_state"
        )
    }
    by_extension = {
        row["extension"]: row["n"]
        for row in connection.execute(
            "SELECT extension,COUNT(*) n FROM documents GROUP BY extension ORDER BY COUNT(*) DESC"
        )
    }
    by_unit = {
        row["unit"]: {
            "links": row["links"],
            "excluded_from_reconciliation": row["excluded"],
            "open_for_other_unit_evidence": row["open"],
        }
        for row in connection.execute(
            """
            SELECT unit,COUNT(*) links,
                   SUM(CASE WHEN reconciliation_needed=0 THEN 1 ELSE 0 END) excluded,
                   SUM(CASE WHEN reconciliation_needed=1 THEN 1 ELSE 0 END) open
            FROM unit_links GROUP BY unit ORDER BY unit
            """
        )
    }
    summary = {
        "format": "iss-shared-document-index-summary-v1",
        "generated_at": now(),
        "database": str(DATABASE),
        "result": "ok",
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "known_content_hashes": connection.execute(
            "SELECT COUNT(*) FROM documents WHERE content_sha256!=''"
        ).fetchone()[0],
        "extraction_states": by_state,
        "extensions": by_extension,
        "completed_unit_links": by_unit,
        "reconciliation_queue": connection.execute(
            """
            SELECT COUNT(*) FROM documents d
            WHERE d.extraction_state!='skipped_known_unit'
            """
        ).fetchone()[0],
        "reuse_policy": "unchanged path+size+mtime skips inventory work; matching SHA-256 reuses content extraction and OCR",
        "deletion_authorized": False,
    }
    temp_summary = SUMMARY.with_suffix(".json.tmp")
    temp_summary.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp_summary.replace(SUMMARY)
    temp_csv = KNOWN_DOCUMENTS.with_suffix(".csv.tmp")
    with temp_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "document_id","source_path","extension","size_bytes","content_sha256",
            "unit","relation_state","reconciliation_needed","source_manifest","basis",
            "deletion_authorized",
        ))
        for row in connection.execute(
            """
            SELECT d.document_id,d.source_path,d.extension,d.size_bytes,d.content_sha256,
                   u.unit,u.relation_state,u.reconciliation_needed,u.source_manifest,u.basis
            FROM documents d JOIN unit_links u ON u.document_id=d.document_id
            ORDER BY u.unit,d.source_path
            """
        ):
            writer.writerow(tuple(row) + ("false",))
    temp_csv.replace(KNOWN_DOCUMENTS)
    temp_runs = SCAN_RUNS_CSV.with_suffix(".csv.tmp")
    with temp_runs.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "run_id","started_at","completed_at","status","scan_path","files_seen",
            "document_files","new_rows","changed_rows","unchanged_rows","errors","note",
        ))
        for row in connection.execute("SELECT * FROM scan_runs ORDER BY started_at"):
            writer.writerow(tuple(row))
    temp_runs.replace(SCAN_RUNS_CSV)
    return summary


def write_extraction_errors(connection: sqlite3.Connection) -> None:
    temporary = EXTRACTION_ERRORS.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow((
            "document_id", "source_path", "extension", "size_bytes",
            "content_sha256", "parser", "extraction_error", "deletion_authorized",
        ))
        for row in connection.execute(
            """
            SELECT document_id,source_path,extension,size_bytes,content_sha256,
                   parser,extraction_error
            FROM documents WHERE extraction_state='error'
            ORDER BY source_path
            """
        ):
            writer.writerow(tuple(row) + ("false",))
    temporary.replace(EXTRACTION_ERRORS)


def write_extraction_progress(values: dict[str, object]) -> None:
    temporary = EXTRACTION_PROGRESS.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(values, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(EXTRACTION_PROGRESS)


def update_document_extraction(
    connection: sqlite3.Connection,
    document_id: str,
    digest: str,
    state: str,
    parser: str,
    text: str,
    error: str,
) -> None:
    connection.execute(
        """
        UPDATE documents SET content_sha256=?,extraction_state=?,parser=?,
          header_text=?,extraction_error=? WHERE document_id=?
        """,
        (digest, state, parser, text, error, document_id),
    )
    connection.execute("DELETE FROM document_fts WHERE document_id=?", (document_id,))
    connection.execute(
        """
        INSERT INTO document_fts(document_id,filename,relative_path,header_text)
        SELECT document_id,filename,relative_path,header_text
        FROM documents WHERE document_id=?
        """,
        (document_id,),
    )


def extraction_queue(connection: sqlite3.Connection, retry_errors: bool) -> list[sqlite3.Row]:
    states = ("pending", "error") if retry_errors else ("pending",)
    placeholders = ",".join("?" for _ in states)
    return list(connection.execute(
        f"""
        SELECT d.document_id,d.source_path,d.extension,d.size_bytes,d.mtime_ns
        FROM documents d
        WHERE d.observed_state='present'
          AND d.extraction_state IN ({placeholders})
        ORDER BY CASE d.extension
          WHEN '.txt' THEN 0 WHEN '.csv' THEN 0 WHEN '.json' THEN 0
          WHEN '.xml' THEN 1 WHEN '.xmp' THEN 1
          WHEN '.docx' THEN 2 WHEN '.xlsx' THEN 2 WHEN '.pptx' THEN 2
          WHEN '.pdf' THEN 3 WHEN '.zip' THEN 4 ELSE 5 END,
          d.size_bytes,d.source_path
        """,
        states,
    ))


def extract(actor: str, limit: int, ocr_pages: int, retry_errors: bool) -> dict[str, object]:
    require_safe_outputs()
    mount = source_mount()
    if ocr_pages < 1 or ocr_pages > 10:
        raise IndexError("--ocr-pages must be between 1 and 10")
    started = now()
    counts = Counter({
        "selected": 0,
        "processed": 0,
        "bytes_hashed": 0,
        "cache_reused": 0,
        "known_unit_duplicates": 0,
        "header_extracted": 0,
        "text_extracted": 0,
        "needs_ocr": 0,
        "errors": 0,
    })
    with EXTRACTION_LOCK.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IndexError("another document extraction process is already active") from error
        with closing(connect()) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if quick != "ok" or foreign:
                raise SafetyError(f"catalogue integrity failed before extraction: quick={quick}, foreign={len(foreign)}")
            queue = extraction_queue(connection, retry_errors)
            if limit:
                queue = queue[:limit]
            counts["selected"] = len(queue)
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_status','running')")
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_started_at',?)", (started,))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_actor',?)", (actor,))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_ocr_scope',?)", (f"first-{ocr_pages}-pages",))
            connection.commit()
            log_extract(
                f"START actor={actor} selected={len(queue)} ocr_scope=first-{ocr_pages}-pages "
                f"source={mount['source']} options={mount['options']}"
            )
            progress: dict[str, object] = {
                "format": "iss-document-extraction-progress-v1",
                "status": "running",
                "started_at": started,
                "updated_at": started,
                "actor": actor,
                "ocr_scope": f"first-{ocr_pages}-pages",
                "source_read_only": True,
                "deletion_authorized": False,
                **counts,
            }
            write_extraction_progress(progress)
            try:
                for row in queue:
                    document_id = row["document_id"]
                    raw_path = row["source_path"]
                    digest = ""
                    try:
                        if counts["processed"] % 25 == 0:
                            try:
                                source_mount()
                            except Exception as error:
                                raise SafetyError(f"source mount safety check failed: {error}") from error
                            require_safe_outputs()
                        if counts["processed"] and counts["processed"] % 250 == 0:
                            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
                            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
                            if quick != "ok" or foreign:
                                raise SafetyError(
                                    f"catalogue integrity changed during extraction: quick={quick}, foreign={len(foreign)}"
                                )
                        path = checked_source_path(raw_path)
                        before = stat_signature(path)
                        expected = (int(row["size_bytes"]), int(row["mtime_ns"]))
                        if before != expected:
                            raise SafetyError(
                                f"source changed since inventory: {path}; expected={expected}; observed={before}"
                            )
                        digest = hash_file(path)
                        after = stat_signature(path)
                        if after != before:
                            raise SafetyError(f"source changed while hashing: {path}; before={before}; after={after}")
                        counts["bytes_hashed"] += before[0]
                        cached = connection.execute(
                            "SELECT * FROM content_cache WHERE content_sha256=?", (digest,)
                        ).fetchone()
                        if cached is not None and retry_errors and cached["extraction_state"] == "error":
                            cached = None
                        if cached is not None:
                            update_document_extraction(
                                connection, document_id, digest, cached["extraction_state"],
                                f"cache:{cached['parser']}", cached["extracted_text"],
                                cached["extraction_error"],
                            )
                            counts["cache_reused"] += 1
                            counts[cached["extraction_state"]] += 1
                        else:
                            known = connection.execute(
                                """
                                SELECT 1 FROM documents
                                WHERE document_id!=? AND content_sha256=?
                                  AND extraction_state='skipped_known_unit'
                                LIMIT 1
                                """,
                                (document_id, digest),
                            ).fetchone()
                            if known is not None:
                                connection.execute(
                                    """
                                    UPDATE documents SET content_sha256=?,
                                      extraction_state='skipped_known_unit',
                                      parser='sha256-known-unit-duplicate',header_text='',
                                      extraction_error='' WHERE document_id=?
                                    """,
                                    (digest, document_id),
                                )
                                counts["known_unit_duplicates"] += 1
                            else:
                                state, parser, text = extract_content(path, row["extension"], ocr_pages)
                                if stat_signature(path) != before:
                                    raise SafetyError(f"source changed while extracting: {path}")
                                connection.execute(
                                    """
                                    INSERT INTO content_cache(
                                      content_sha256,extension,parser,extraction_state,
                                      extracted_text,extraction_error,extracted_at
                                    ) VALUES (?,?,?,?,?,'',?)
                                    ON CONFLICT(content_sha256) DO UPDATE SET
                                      extension=excluded.extension,parser=excluded.parser,
                                      extraction_state=excluded.extraction_state,
                                      extracted_text=excluded.extracted_text,
                                      extraction_error='',extracted_at=excluded.extracted_at
                                    """,
                                    (digest, row["extension"], parser, state, text, now()),
                                )
                                update_document_extraction(
                                    connection, document_id, digest, state, parser, text, ""
                                )
                                counts[state] += 1
                    except SafetyError:
                        raise
                    except OSError as error:
                        if error.errno in {errno.EIO, errno.ENODEV, errno.ENXIO, errno.ESTALE}:
                            raise SafetyError(f"source device/read integrity error at {raw_path}: {error}") from error
                        message = f"{type(error).__name__}: {error}"[:2000]
                        update_document_extraction(
                            connection, document_id, "", "error", "", "", message
                        )
                        counts["errors"] += 1
                        log_extract(f"ERROR document_id={document_id} path={json.dumps(raw_path)} error={json.dumps(message)}")
                    except Exception as error:
                        message = f"{type(error).__name__}: {error}"[:2000]
                        update_document_extraction(
                            connection, document_id, digest, "error", "", "", message
                        )
                        if digest:
                            connection.execute(
                                """
                                INSERT OR REPLACE INTO content_cache(
                                  content_sha256,extension,parser,extraction_state,
                                  extracted_text,extraction_error,extracted_at
                                ) VALUES (?,?,?,'error','',?,?)
                                """,
                                (digest, row["extension"], "", message, now()),
                            )
                        counts["errors"] += 1
                        log_extract(f"ERROR document_id={document_id} path={json.dumps(raw_path)} error={json.dumps(message)}")
                    counts["processed"] += 1
                    connection.commit()
                    if counts["processed"] % 25 == 0 or counts["processed"] == len(queue):
                        progress.update({**counts, "updated_at": now(), "current_path": raw_path})
                        write_extraction_progress(progress)
                        write_extraction_errors(connection)
                        line = (
                            f"PROGRESS processed={counts['processed']}/{len(queue)} "
                            f"cache={counts['cache_reused']} known_unit_duplicates={counts['known_unit_duplicates']} "
                            f"errors={counts['errors']}"
                        )
                        log_extract(line)
                        print(line, flush=True)
                quick = connection.execute("PRAGMA quick_check").fetchone()[0]
                foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
                if quick != "ok" or foreign:
                    raise SafetyError(f"catalogue integrity failed after extraction: quick={quick}, foreign={len(foreign)}")
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_status','complete')")
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_completed_at',?)", (now(),))
                connection.commit()
                write_extraction_errors(connection)
                summary = write_outputs(connection, source_mount())
                progress.update({
                    **counts, "status": "complete", "updated_at": now(),
                    "completed_at": now(), "current_path": "",
                    "remaining_pending": summary["extraction_states"].get("pending", 0),
                })
                write_extraction_progress(progress)
                log_extract(
                    f"COMPLETE processed={counts['processed']} cache={counts['cache_reused']} "
                    f"known_unit_duplicates={counts['known_unit_duplicates']} errors={counts['errors']}"
                )
                return {"result": "ok", **progress}
            except SafetyError as error:
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_status','stopped_safety')")
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('extraction_safety_error',?)", (str(error),))
                connection.commit()
                progress.update({**counts, "status": "stopped_safety", "updated_at": now(), "safety_error": str(error)})
                write_extraction_progress(progress)
                write_extraction_errors(connection)
                log_extract(f"STOP_SAFETY error={json.dumps(str(error))}")
                raise


def seed() -> dict[str, object]:
    mount = source_mount()
    with closing(connect()) as connection:
        archive_rows = seed_archive_index(connection)
        unit_rows = {
            unit: seed_unit_manifest(connection, unit)
            for unit in ("wf-sender", "trafo", "kurt-schwarz")
        }
        connection.execute(
            "INSERT OR REPLACE INTO meta VALUES ('whole_usb_visual_index',?)",
            (str(WHOLE_USB_VISUAL_INDEX),),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta VALUES ('prior_archive_index',?)",
            (str(OLD_ARCHIVE_INDEX),),
        )
        connection.commit()
        refresh_fts(connection)
        summary = write_outputs(connection, mount)
        summary["seeded_archive_document_rows"] = archive_rows
        summary["seeded_unit_manifest_rows"] = unit_rows
        return summary


def iter_documents(scan_path: Path) -> Iterable[Path]:
    for directory, names, files in os.walk(scan_path):
        names[:] = [
            name for name in names
            if name not in IGNORED_DIRECTORIES and not name.casefold().endswith(".lrdata")
        ]
        base = Path(directory)
        for name in files:
            path = base / name
            if path.suffix.casefold() in DOCUMENT_SUFFIXES and not name.startswith("~$"):
                yield path


def inventory(scan_path: Path, actor: str) -> dict[str, object]:
    mount = source_mount()
    scan_path = scan_path.resolve(strict=True)
    try:
        scan_path.relative_to(SOURCE_ROOT.resolve(strict=True))
    except ValueError as error:
        raise IndexError("inventory path must stay inside read-only Elements") from error
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha256(str(scan_path).encode()).hexdigest()[:8]
    started = now()
    counts = Counter({
        "files_seen": 0,
        "document_files": 0,
        "new_rows": 0,
        "changed_rows": 0,
        "unchanged_rows": 0,
        "errors": 0,
    })
    with closing(connect()) as connection:
        connection.execute(
            """
            UPDATE scan_runs SET completed_at=?,status='failed',errors=errors+1,
              note=note||'; interrupted before a later inventory resumed'
            WHERE status='running'
            """,
            (now(),),
        )
        connection.execute(
            "INSERT INTO scan_runs(run_id,started_at,status,scan_path,note) VALUES (?,?,'running',?,?)",
            (run_id, started, str(scan_path), f"actor={actor}; inventory only; no content extraction"),
        )
        connection.commit()
        try:
            for path in iter_documents(scan_path):
                counts["document_files"] += 1
                counts["files_seen"] += 1
                relative = path.relative_to(SOURCE_ROOT).as_posix()
                stat = path.stat()
                document_id = stable_id(f"elements\x1f{relative}")
                existing = connection.execute(
                    "SELECT size_bytes,mtime_ns,observed_state FROM documents WHERE document_id=?",
                    (document_id,),
                ).fetchone()
                if existing is None:
                    counts["new_rows"] += 1
                elif int(existing["size_bytes"]) == stat.st_size and int(existing["mtime_ns"]) == stat.st_mtime_ns:
                    counts["unchanged_rows"] += 1
                    if existing["observed_state"] != "present":
                        connection.execute(
                            "UPDATE documents SET observed_state='present',last_observed_at=? WHERE document_id=?",
                            (now(), document_id),
                        )
                    if counts["document_files"] % 500 == 0:
                        connection.commit()
                    continue
                else:
                    counts["changed_rows"] += 1
                    connection.execute(
                        """
                        UPDATE documents SET
                          content_sha256=CASE WHEN EXISTS (
                            SELECT 1 FROM unit_links u
                            WHERE u.document_id=documents.document_id
                              AND u.reconciliation_needed=0
                          ) THEN content_sha256 ELSE '' END,
                          header_text='',parser='',
                          extraction_error='',
                          extraction_state=CASE WHEN extraction_state='skipped_known_unit'
                            THEN extraction_state ELSE 'pending' END
                        WHERE document_id=?
                        """,
                        (document_id,),
                    )
                upsert_document(
                    connection,
                    source_root="elements",
                    relative_path=relative,
                    source_path=str(path),
                    filename=path.name,
                    extension=path.suffix.casefold(),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    content_sha256="",
                    discovery_basis=f"live-inventory:{run_id}",
                    observed_state="present",
                )
                if counts["document_files"] % 500 == 0:
                    connection.commit()
            connection.execute(
                """
                UPDATE scan_runs SET completed_at=?,status='complete',files_seen=?,
                  document_files=?,new_rows=?,changed_rows=?,unchanged_rows=?,errors=0
                WHERE run_id=?
                """,
                (
                    now(), counts["files_seen"], counts["document_files"],
                    counts["new_rows"], counts["changed_rows"], counts["unchanged_rows"], run_id,
                ),
            )
            connection.commit()
            refresh_fts(connection)
            summary = write_outputs(connection, mount)
        except Exception as error:
            connection.execute(
                "UPDATE scan_runs SET completed_at=?,status='failed',errors=1,note=note||? WHERE run_id=?",
                (now(), f"; {type(error).__name__}: {error}", run_id),
            )
            connection.commit()
            raise
    return {"result": "ok", "run_id": run_id, **counts, "summary": summary}


def status() -> dict[str, object]:
    mount = source_mount()
    if not DATABASE.is_file():
        raise IndexError("document index has not been seeded")
    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign:
            raise IndexError(f"SQLite validation failed: quick={quick}, foreign={len(foreign)}")
        extraction_counts = {
            row["extraction_state"]: row["n"]
            for row in connection.execute(
                "SELECT extraction_state,COUNT(*) n FROM documents GROUP BY extraction_state"
            )
        }
        extraction_meta = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key,value FROM meta WHERE key LIKE 'extraction_%'"
            )
        }
        return {
            "result": "ok",
            "database": str(DATABASE),
            "quick_check": quick,
            "foreign_key_violations": len(foreign),
            "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "unit_links": connection.execute("SELECT COUNT(*) FROM unit_links").fetchone()[0],
            "excluded_from_reconciliation": connection.execute(
                "SELECT COUNT(DISTINCT document_id) FROM unit_links WHERE reconciliation_needed=0"
            ).fetchone()[0],
            "open_contextual_links": connection.execute(
                "SELECT COUNT(*) FROM unit_links WHERE reconciliation_needed=1"
            ).fetchone()[0],
            "extraction_counts": extraction_counts,
            "extraction_meta": extraction_meta,
            "scan_runs": [dict(row) for row in connection.execute("SELECT * FROM scan_runs ORDER BY started_at")],
            "source_mount": mount,
            "source_read_only": True,
            "deletion_authorized": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--path", type=Path, default=SOURCE_ROOT)
    inventory_parser.add_argument("--actor", default="vladimir")
    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("--actor", default="vladimir")
    extract_parser.add_argument("--limit", type=int, default=0)
    extract_parser.add_argument("--ocr-pages", type=int, default=3)
    extract_parser.add_argument("--retry-errors", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "seed":
        result = seed()
    elif args.command == "inventory":
        result = inventory(args.path, args.actor)
    elif args.command == "extract":
        result = extract(args.actor, args.limit, args.ocr_pages, args.retry_errors)
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
