#!/usr/bin/env python3
"""Recovery-safe audit of unreadable Office files and Lightroom catalogs.

The USB source is opened read-only. All generated audit artifacts are written
under the established Trafo deep-search work directory.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE = Path("/media/vladimir/Elements")
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "TRO-1988-metadata-usb-20260727"
)
INVENTORY = OUTPUT / "usb-file-inventory.csv"
ERRORS = OUTPUT / "errors.jsonl"
OFFICE_HITS = OUTPUT / "office-recovery-hits.jsonl"
OFFICE_AUDIT = OUTPUT / "office-recovery-audit.json"
LIGHTROOM_HITS = OUTPUT / "lightroom-catalog-hits.jsonl"
LIGHTROOM_AUDIT = OUTPUT / "lightroom-catalog-audit.json"

YEAR_RE = re.compile(r"(?<!\d)1988(?!\d)")
TRAFO_RE = re.compile(
    r"(?i)(?:\btrafo\b|\bder\s+trafo\b|\btro[-_ .]?1988\b|"
    r"betriebszeitung|transformatorenwerk|karl[-_ .]?liebknecht)"
)
LOCK_RE = re.compile(r"(?:^|[/\\])~[$�]")
XML_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def assert_read_only_source() -> None:
    result = subprocess.run(
        ["findmnt", "-no", "OPTIONS", "--target", str(SOURCE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    options = [line.strip().split(",") for line in result.stdout.splitlines()]
    if result.returncode or not options or not all("ro" in row for row in options):
        raise RuntimeError(f"source is not read-only: {result.stdout.strip()}")
    if SOURCE == OUTPUT or SOURCE in OUTPUT.parents:
        raise RuntimeError("output must not be under the evidence source")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def seven_zip_members(path: Path) -> tuple[list[str], str]:
    result = subprocess.run(
        ["7z", "l", "-slt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    listing = result.stdout.decode("utf-8", "replace")
    members: list[str] = []
    archive_seen = False
    for line in listing.splitlines():
        if line.startswith("----------"):
            archive_seen = True
            continue
        if archive_seen and line.startswith("Path = "):
            members.append(line[7:])
    diagnostic = result.stderr.decode("utf-8", "replace").strip()
    if not members and result.returncode:
        raise RuntimeError(diagnostic or f"7z returned {result.returncode}")
    return members, diagnostic


def seven_zip_read(path: Path, member: str) -> bytes:
    result = subprocess.run(
        ["7z", "x", "-so", str(path), member],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(diagnostic or f"7z returned {result.returncode}")
    return result.stdout


def xml_text(element: ET.Element) -> str:
    return "".join(text or "" for text in element.itertext())


def shared_strings(payload: bytes) -> list[str]:
    root = ET.fromstring(payload)
    values = []
    for item in root.findall("main:si", XML_NS):
        values.append(xml_text(item))
    return values


def sheet_rows(payload: bytes, strings: list[str]) -> list[tuple[str, list[str]]]:
    root = ET.fromstring(payload)
    rows: list[tuple[str, list[str]]] = []
    for row in root.findall(".//main:sheetData/main:row", XML_NS):
        values = []
        for cell in row.findall("main:c", XML_NS):
            kind = cell.get("t", "")
            value_node = cell.find("main:v", XML_NS)
            if kind == "inlineStr":
                inline = cell.find("main:is", XML_NS)
                value = xml_text(inline) if inline is not None else ""
            elif value_node is None:
                value = ""
            elif kind == "s":
                try:
                    value = strings[int(value_node.text or "")]
                except (ValueError, IndexError):
                    value = f"[unresolved shared string {value_node.text or ''}]"
            else:
                value = value_node.text or ""
            values.append(value)
        rows.append((row.get("r", ""), values))
    return rows


def audit_xlsx(path: Path, members: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    diagnostics: list[str] = []
    strings: list[str] = []
    shared_member = next(
        (member for member in members if member == "xl/sharedStrings.xml"), None
    )
    if shared_member:
        try:
            strings = shared_strings(seven_zip_read(path, shared_member))
        except Exception as error:
            diagnostics.append(f"{shared_member}: {error}")

    sheet_members = sorted(
        member
        for member in members
        if member.startswith("xl/worksheets/") and member.endswith(".xml")
    )
    rows_with_year: list[dict[str, Any]] = []
    workbook_has_trafo = TRAFO_RE.search(" ".join(strings)) is not None
    parsed_sheets = 0
    for member in sheet_members:
        try:
            rows = sheet_rows(seven_zip_read(path, member), strings)
            parsed_sheets += 1
        except Exception as error:
            diagnostics.append(f"{member}: {error}")
            continue
        for row_number, values in rows:
            joined = " | ".join(value.strip() for value in values if value.strip())
            if YEAR_RE.search(joined):
                rows_with_year.append(
                    {
                        "path": str(path),
                        "container_type": "xlsx",
                        "sheet_member": member,
                        "row": row_number,
                        "values": values,
                        "row_has_trafo": TRAFO_RE.search(joined) is not None,
                        "workbook_has_trafo": workbook_has_trafo,
                        "path_has_trafo": TRAFO_RE.search(str(path)) is not None,
                    }
                )
    if not parsed_sheets:
        diagnostics.append("no worksheet XML parsed")
    return rows_with_year, diagnostics


def audit_docx(path: Path, members: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    diagnostics: list[str] = []
    hits: list[dict[str, Any]] = []
    document_members = [
        member
        for member in members
        if member.startswith("word/") and member.endswith(".xml")
    ]
    for member in document_members:
        try:
            text = xml_text(ET.fromstring(seven_zip_read(path, member)))
        except Exception as error:
            diagnostics.append(f"{member}: {error}")
            continue
        for year in YEAR_RE.finditer(text):
            start = max(0, year.start() - 500)
            end = min(len(text), year.end() + 500)
            context = text[start:end]
            hits.append(
                {
                    "path": str(path),
                    "container_type": "docx",
                    "member": member,
                    "context": " ".join(context.split()),
                    "context_has_trafo": TRAFO_RE.search(context) is not None,
                    "document_has_trafo": TRAFO_RE.search(text) is not None,
                    "path_has_trafo": TRAFO_RE.search(str(path)) is not None,
                }
            )
    return hits, diagnostics


def office_error_paths() -> list[Path]:
    paths: set[str] = set()
    with ERRORS.open(encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            path = value.get("path", "")
            if (
                value.get("phase") == "content"
                and Path(path).suffix.casefold() in {".xlsx", ".docx"}
            ):
                paths.add(path)
    return [Path(path) for path in sorted(paths)]


def audit_office() -> dict[str, Any]:
    counts: Counter[str] = Counter()
    hits: list[dict[str, Any]] = []
    file_results: list[dict[str, Any]] = []
    for path in office_error_paths():
        counts["files_considered"] += 1
        if LOCK_RE.search(str(path)) or path.stat().st_size <= 1024:
            counts["lock_or_tiny_files_skipped"] += 1
            file_results.append(
                {"path": str(path), "status": "lock-or-tiny", "size_bytes": path.stat().st_size}
            )
            continue
        try:
            members, listing_diagnostic = seven_zip_members(path)
            if path.suffix.casefold() == ".xlsx":
                file_hits, diagnostics = audit_xlsx(path, members)
            else:
                file_hits, diagnostics = audit_docx(path, members)
            hits.extend(file_hits)
            counts["files_recovered_with_7z"] += 1
            counts["year_hit_rows_or_contexts"] += len(file_hits)
            if any(
                hit.get("row_has_trafo")
                or hit.get("workbook_has_trafo")
                or hit.get("path_has_trafo")
                or hit.get("context_has_trafo")
                or hit.get("document_has_trafo")
                for hit in file_hits
            ):
                counts["files_with_trafo_relevant_year_hit"] += 1
            if diagnostics:
                counts["files_with_member_diagnostics"] += 1
            file_results.append(
                {
                    "path": str(path),
                    "status": "recovered",
                    "member_count": len(members),
                    "year_hit_count": len(file_hits),
                    "listing_diagnostic": listing_diagnostic,
                    "member_diagnostics": diagnostics,
                }
            )
        except Exception as error:
            counts["files_unrecoverable_with_7z"] += 1
            file_results.append(
                {"path": str(path), "status": "unrecoverable", "error": str(error)}
            )
    write_jsonl(OFFICE_HITS, hits)
    result = {
        "completed_at": now(),
        "source_root": str(SOURCE),
        "source_read_only_required": True,
        "counts": dict(counts),
        "files": file_results,
        "hits_artifact": str(OFFICE_HITS),
    }
    atomic_json(OFFICE_AUDIT, result)
    return result


def query_lightroom(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1", uri=True, timeout=30
    )
    hits: list[dict[str, Any]] = []
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"AgLibraryFile", "Adobe_images"}.issubset(tables):
            return hits, {"status": "queried-no-required-tables"}
        has_iptc = "AgLibraryIPTC" in tables
        iptc_join = (
            "LEFT JOIN AgLibraryIPTC AS p ON p.image = i.id_local" if has_iptc else ""
        )
        iptc_fields = ", p.caption, p.copyright" if has_iptc else ", NULL, NULL"
        query = f"""
            SELECT f.id_local, f.baseName, f.originalFilename, f.extension,
                   i.id_local, i.captureTime, i.originalCaptureTime
                   {iptc_fields}
              FROM AgLibraryFile AS f
              LEFT JOIN Adobe_images AS i ON i.rootFile = f.id_local
              {iptc_join}
             WHERE lower(coalesce(f.baseName, '')) LIKE 'tro-1988%'
                OR lower(coalesce(f.originalFilename, '')) LIKE 'tro-1988%'
        """
        for row in connection.execute(query):
            hits.append(
                {
                    "catalog": str(path),
                    "match_type": "tro-1988-filename",
                    "file_id": row[0],
                    "base_name": row[1],
                    "original_filename": row[2],
                    "extension": row[3],
                    "image_id": row[4],
                    "capture_time": row[5],
                    "original_capture_time": row[6],
                    "caption": row[7],
                    "copyright": row[8],
                }
            )
        if has_iptc:
            for row in connection.execute(
                """
                SELECT id_local, image, caption, copyright
                  FROM AgLibraryIPTC
                 WHERE lower(coalesce(caption, '')) LIKE '%trafo%'
                   AND coalesce(caption, '') LIKE '%1988%'
                """
            ):
                hits.append(
                    {
                        "catalog": str(path),
                        "match_type": "caption-trafo-1988",
                        "iptc_id": row[0],
                        "image_id": row[1],
                        "caption": row[2],
                        "copyright": row[3],
                    }
                )
        return hits, {"status": "queried", "has_iptc": has_iptc}
    finally:
        connection.close()


def audit_lightroom() -> dict[str, Any]:
    catalogs: list[Path] = []
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if Path(row["path"]).suffix.casefold() == ".lrcat":
                catalogs.append(Path(row["path"]))
    hits: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    counts: Counter[str] = Counter(catalogs_found=len(catalogs))
    for path in catalogs:
        try:
            catalog_hits, detail = query_lightroom(path)
            hits.extend(catalog_hits)
            counts[detail["status"].replace("-", "_")] += 1
            counts["rows_found"] += len(catalog_hits)
            files.append({"path": str(path), "hit_count": len(catalog_hits), **detail})
        except Exception as error:
            counts["errors"] += 1
            files.append({"path": str(path), "status": "error", "error": str(error)})
    write_jsonl(LIGHTROOM_HITS, hits)
    result = {
        "completed_at": now(),
        "source_root": str(SOURCE),
        "source_read_only_required": True,
        "counts": dict(counts),
        "files": files,
        "hits_artifact": str(LIGHTROOM_HITS),
    }
    atomic_json(LIGHTROOM_AUDIT, result)
    return result


def main() -> int:
    assert_read_only_source()
    office = audit_office()
    lightroom = audit_lightroom()
    print(
        json.dumps(
            {
                "office_counts": office["counts"],
                "lightroom_counts": lightroom["counts"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
