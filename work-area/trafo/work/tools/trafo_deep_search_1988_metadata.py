#!/usr/bin/env python3
"""Read-only whole-USB search for hidden Trafo 1988 metadata."""

from __future__ import annotations

import csv
import argparse
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path("/media/vladimir/Elements")
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "TRO-1988-metadata-usb-20260727"
)
INVENTORY = OUTPUT / "usb-file-inventory.csv"
HITS = OUTPUT / "content-hits.jsonl"
ARCHIVES = OUTPUT / "archive-member-hits.jsonl"
LIGHTROOM_HITS = OUTPUT / "lightroom-catalog-hits.jsonl"
ERRORS = OUTPUT / "errors.jsonl"
SUMMARY = OUTPUT / "scan-summary.json"

PLAIN_EXTENSIONS = {
    ".csv", ".tsv", ".txt", ".xml", ".json", ".jsonl", ".html", ".htm",
    ".sql", ".log", ".md", ".xmp", ".ini", ".cfg", ".yaml", ".yml", ".rtf",
}
OFFICE_ZIP_EXTENSIONS = {".xlsx", ".xlsm", ".ods", ".docx", ".odt"}
LEGACY_OFFICE_EXTENSIONS = {".xls", ".doc"}
DATABASE_EXTENSIONS = {
    ".db", ".sqlite", ".sqlite3", ".lrcat", ".mdb", ".accdb",
    ".db-wal", ".sqlite-wal", ".sqlite3-wal", ".lrcat-wal",
}
ARCHIVE_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz",
    ".gz", ".bz2", ".xz",
}
PDF_EXTENSIONS = {".pdf"}
METADATA_EXTENSIONS = (
    PLAIN_EXTENSIONS
    | OFFICE_ZIP_EXTENSIONS
    | LEGACY_OFFICE_EXTENSIONS
    | DATABASE_EXTENSIONS
)

EXACT_RE = re.compile(
    rb"(?i)(?:TRO[\s_.-]*1988|1988[\s_.-]*TRO|"
    rb"DER[\s_.-]*TRAFO.{0,160}1988|1988.{0,160}DER[\s_.-]*TRAFO)"
)
YEAR_RE = re.compile(rb"(?<![0-9A-Fa-f])1988(?![0-9A-Fa-f])")
TRAFO_RE = re.compile(
    rb"(?i)(?:\bTRAFO\b|BETRIEBSZEITUNG|TRANSFORMATORENWERK|"
    rb"KARL[\s_.-]*LIEBKNECHT|\bTRO[\s_.-]*\d)"
)
PATH_TRAFO_RE = re.compile(
    r"(?i)(?:trafo|betriebszeitung|transformatorenwerk|karl.?liebknecht|"
    r"(?:^|[/\\])tro(?:[-_ .]|$))"
)
PATH_META_RE = re.compile(
    r"(?i)(?:meta|katalog|catalog|basis|angaben|tabelle|liste|inventar|"
    r"register|upload|export|daten|database|db|beschreibung|texte|"
    r"information|dokumentation)"
)
DATABASE_NAME_RE = re.compile(
    r"(?i)(?:trafo|lightroom|\\.lrcat|katalog|catalog|metadat|inventar|"
    r"inventory|museum|digikam|archive[_-]?index|bildarchiv|"
    r"fotoarchiv|photo.?catalog)"
)
ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz", ".tbz2", ".txz"
)


class SearchError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def assert_read_only_source() -> None:
    if OUTPUT == ROOT or ROOT in OUTPUT.parents:
        raise SearchError("output must remain outside the source root")
    result = subprocess.run(
        ["findmnt", "-no", "TARGET,FSTYPE,OPTIONS", "--target", str(ROOT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SearchError(f"cannot resolve source mount: {result.stderr.strip()}")
    lines = [line.split(maxsplit=2) for line in result.stdout.splitlines()]
    if not lines or not all(
        len(line) == 3 and "ro" in line[2].split(",") for line in lines
    ):
        raise SearchError(f"source mount is not read-only: {result.stdout.strip()}")


def replace_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def extension(path: Path) -> str:
    lowered = path.name.casefold()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return path.suffix.casefold()


def category(path: Path) -> str:
    suffix = extension(path)
    if suffix in PLAIN_EXTENSIONS:
        return "plain"
    if suffix in OFFICE_ZIP_EXTENSIONS:
        return "office_zip"
    if suffix in LEGACY_OFFICE_EXTENSIONS:
        return "legacy_office"
    if suffix in DATABASE_EXTENSIONS:
        return "database"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in ARCHIVE_EXTENSIONS or suffix in ARCHIVE_SUFFIXES:
        return "archive"
    return "other"


def inventory() -> list[dict[str, str]]:
    if INVENTORY.exists():
        with INVENTORY.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    temporary = INVENTORY.with_name(".usb-file-inventory.csv.tmp")
    rows: list[dict[str, str]] = []
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("path", "size_bytes", "mtime_ns", "extension", "category"),
        )
        writer.writeheader()
        for base, directories, files in os.walk(ROOT, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if not (Path(base) / name).is_symlink()
            ]
            for name in files:
                path = Path(base) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                    row = {
                        "path": str(path),
                        "size_bytes": str(stat.st_size),
                        "mtime_ns": str(stat.st_mtime_ns),
                        "extension": extension(path),
                        "category": category(path),
                    }
                    writer.writerow(row)
                    rows.append(row)
                except OSError as error:
                    append_jsonl(
                        ERRORS,
                        {"phase": "inventory", "path": str(path), "error": str(error)},
                    )
            if len(rows) and len(rows) % 100000 == 0:
                print(f"inventory {len(rows)} files", flush=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, INVENTORY)
    return rows


def normalized_bytes(payload: bytes) -> bytes:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        len(payload) > 4 and payload[1:2] == b"\x00" and payload[3:4] == b"\x00"
    ):
        try:
            return payload.decode("utf-16", "replace").encode("utf-8")
        except UnicodeError:
            pass
    return payload


def match_payload(payload: bytes, path: str) -> tuple[str, str] | None:
    payload = normalized_bytes(payload)
    exact = EXACT_RE.search(payload)
    if exact:
        start = max(0, exact.start() - 180)
        end = min(len(payload), exact.end() + 240)
        snippet = payload[start:end].decode("utf-8", "replace")
        return "exact-trafo-1988-context", " ".join(snippet.split())
    for year in YEAR_RE.finditer(payload):
        start = max(0, year.start() - 2000)
        end = min(len(payload), year.end() + 2000)
        context = payload[start:end]
        if TRAFO_RE.search(context) or PATH_TRAFO_RE.search(path):
            snippet_start = max(0, year.start() - 300)
            snippet_end = min(len(payload), year.end() + 500)
            snippet = payload[snippet_start:snippet_end].decode(
                "utf-8", "replace"
            )
            return "year-plus-trafo-context", " ".join(snippet.split())
    return None


def match_file_stream(path: Path) -> tuple[str, str] | None:
    overlap = b""
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            payload = overlap + block
            match = match_payload(payload, str(path))
            if match:
                return match
            overlap = payload[-8192:]
    return None


def scan_lightroom_catalog(path: Path) -> int:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1",
        uri=True,
        timeout=30,
    )
    hits = 0
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"AgLibraryFile", "Adobe_images"}
        if not required.issubset(tables):
            return 0
        iptc_join = (
            "LEFT JOIN AgLibraryIPTC AS p ON p.image = i.id_local"
            if "AgLibraryIPTC" in tables
            else ""
        )
        iptc_fields = (
            ", p.caption, p.copyright"
            if "AgLibraryIPTC" in tables
            else ", NULL AS caption, NULL AS copyright"
        )
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
            append_jsonl(
                LIGHTROOM_HITS,
                {
                    "catalog": str(path),
                    "file_id": row[0],
                    "base_name": row[1],
                    "original_filename": row[2],
                    "extension": row[3],
                    "image_id": row[4],
                    "capture_time": row[5],
                    "original_capture_time": row[6],
                    "caption": row[7],
                    "copyright": row[8],
                },
            )
            hits += 1
        if "AgLibraryIPTC" in tables:
            for row in connection.execute(
                """
                SELECT p.id_local, p.image, p.caption, p.copyright
                FROM AgLibraryIPTC AS p
                WHERE lower(coalesce(p.caption, '')) LIKE '%trafo%'
                  AND coalesce(p.caption, '') LIKE '%1988%'
                """
            ):
                append_jsonl(
                    LIGHTROOM_HITS,
                    {
                        "catalog": str(path),
                        "iptc_id": row[0],
                        "image_id": row[1],
                        "caption": row[2],
                        "copyright": row[3],
                        "match_type": "caption-trafo-1988",
                    },
                )
                hits += 1
    finally:
        connection.close()
    return hits


def read_plain(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read()


def read_office_zip(path: Path) -> bytes:
    chunks = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            suffix = Path(info.filename).suffix.casefold()
            if (
                info.file_size <= 100 * 1024 * 1024
                and suffix in {".xml", ".txt", ".csv", ".rels"}
            ):
                chunks.append(archive.read(info))
    return b"\n".join(chunks)


def read_legacy(path: Path) -> bytes:
    tool = "xls2csv" if path.suffix.casefold() == ".xls" else "catdoc"
    result = subprocess.run(
        [tool, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    fallback = subprocess.run(
        ["strings", "-a", "-el", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    return fallback.stdout


def read_pdf(path: Path) -> bytes:
    result = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(path), "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise SearchError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def scan_direct(rows: list[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    processed = 0
    for row in rows:
        path = Path(row["path"])
        kind = row["category"]
        size = int(row["size_bytes"])
        should_scan_pdf = (
            kind == "pdf"
            and size <= 100 * 1024 * 1024
            and (
                "1988" in str(path)
                or (
                    PATH_TRAFO_RE.search(str(path))
                    and PATH_META_RE.search(str(path))
                )
            )
        )
        should_scan_database = kind == "database" and bool(
            DATABASE_NAME_RE.search(str(path))
        )
        if kind not in {"plain", "office_zip", "legacy_office", "database"}:
            if not should_scan_pdf:
                continue
        if kind == "database" and not should_scan_database:
            counts["skipped_generic_database"] += 1
            continue
        try:
            match = None
            if kind == "plain" and size > 256 * 1024 * 1024:
                match = match_file_stream(path)
            elif kind == "plain":
                payload = read_plain(path)
            elif kind == "office_zip":
                payload = read_office_zip(path)
            elif kind == "legacy_office":
                payload = read_legacy(path)
            elif kind == "database" and path.suffix.casefold() == ".lrcat":
                catalog_hits = scan_lightroom_catalog(path)
                counts["lightroom_catalogs_queried"] += 1
                counts["lightroom_rows_found"] += catalog_hits
                match = None
            elif kind == "database" and size <= 256 * 1024 * 1024:
                match = match_file_stream(path)
            elif kind == "database":
                counts["skipped_large_non_lightroom_database"] += 1
                match = None
            else:
                payload = read_pdf(path)
            if match is None and kind != "database" and not (
                kind == "plain" and size > 256 * 1024 * 1024
            ):
                match = match_payload(payload, str(path))
            counts[f"scanned_{kind}"] += 1
            if match:
                match_type, snippet = match
                append_jsonl(
                    HITS,
                    {
                        "path": str(path),
                        "size_bytes": size,
                        "container_type": kind,
                        "match_type": match_type,
                        "snippet": snippet[:3000],
                    },
                )
                counts[f"hit_{kind}"] += 1
        except Exception as error:
            append_jsonl(
                ERRORS,
                {"phase": "content", "path": str(path), "error": str(error)},
            )
            counts[f"error_{kind}"] += 1
        processed += 1
        if processed % 250 == 0:
            print(f"content containers {processed}", flush=True)
    return counts


def archive_names(path: Path) -> list[tuple[str, int]]:
    lowered = path.name.casefold()
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return [(info.filename, info.file_size) for info in archive.infolist()]
    if lowered.endswith(
        (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz")
    ):
        with tarfile.open(path, "r:*") as archive:
            return [
                (member.name, member.size)
                for member in archive.getmembers()
                if member.isfile()
            ]
    if lowered.endswith((".7z", ".rar")):
        result = subprocess.run(
            ["7z", "l", "-ba", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise SearchError(result.stderr.decode("utf-8", "replace").strip())
        return [
            (line.decode("utf-8", "replace"), 0)
            for line in result.stdout.splitlines()
        ]
    return []


def zip_member_payload(archive: zipfile.ZipFile, member: str) -> bytes:
    payload = archive.read(member)
    suffix = Path(member).suffix.casefold()
    if suffix in OFFICE_ZIP_EXTENSIONS:
        with zipfile.ZipFile(io.BytesIO(payload)) as nested:
            chunks = [
                nested.read(info)
                for info in nested.infolist()
                if info.file_size <= 100 * 1024 * 1024
                and Path(info.filename).suffix.casefold()
                in {".xml", ".txt", ".csv", ".rels"}
            ]
        return b"\n".join(chunks)
    return payload


def scan_archives(rows: list[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    archive_rows = [row for row in rows if row["category"] == "archive"]
    for index, row in enumerate(archive_rows, 1):
        path = Path(row["path"])
        zip_handle: zipfile.ZipFile | None = None
        try:
            if path.name.casefold().endswith(".zip"):
                zip_handle = zipfile.ZipFile(path)
                members = [
                    (info.filename, info.file_size)
                    for info in zip_handle.infolist()
                ]
            else:
                members = archive_names(path)
            counts["archives_scanned"] += 1
            for member, member_size in members:
                member_path = f"{path}!/{member}"
                member_suffix = Path(member).suffix.casefold()
                name_hit = bool(
                    "1988" in member
                    and (
                        PATH_TRAFO_RE.search(member)
                        or PATH_TRAFO_RE.search(str(path))
                        or member_suffix in METADATA_EXTENSIONS
                    )
                )
                metadata_candidate = bool(
                    member_suffix in METADATA_EXTENSIONS
                    and (
                        PATH_TRAFO_RE.search(member_path)
                        or "1988" in member
                        or (
                            PATH_TRAFO_RE.search(str(path))
                            and PATH_META_RE.search(member)
                        )
                    )
                )
                if name_hit:
                    append_jsonl(
                        ARCHIVES,
                        {
                            "archive": str(path),
                            "member": member,
                            "member_size_bytes": member_size,
                            "match_type": "member-name",
                        },
                    )
                    counts["archive_member_name_hits"] += 1
                if (
                    zip_handle is not None
                    and metadata_candidate
                    and 0 <= member_size <= 100 * 1024 * 1024
                ):
                    try:
                        payload = zip_member_payload(zip_handle, member)
                        match = match_payload(payload, member_path)
                        counts["archive_metadata_members_scanned"] += 1
                        if match:
                            match_type, snippet = match
                            append_jsonl(
                                ARCHIVES,
                                {
                                    "archive": str(path),
                                    "member": member,
                                    "member_size_bytes": member_size,
                                    "match_type": match_type,
                                    "snippet": snippet[:3000],
                                },
                            )
                            counts["archive_member_content_hits"] += 1
                    except Exception as error:
                        append_jsonl(
                            ERRORS,
                            {
                                "phase": "archive-member-content",
                                "path": member_path,
                                "error": str(error),
                            },
                        )
                        counts["archive_member_content_errors"] += 1
        except Exception as error:
            append_jsonl(
                ERRORS,
                {"phase": "archive-list", "path": str(path), "error": str(error)},
            )
            counts["archive_errors"] += 1
        finally:
            if zip_handle is not None:
                zip_handle.close()
        if index % 50 == 0:
            print(f"archives {index}/{len(archive_rows)}", flush=True)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("all", "direct", "archives"),
        default="all",
    )
    arguments = parser.parse_args()
    assert_read_only_source()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    reset_paths = {
        "all": (HITS, ARCHIVES, LIGHTROOM_HITS, ERRORS),
        "direct": (HITS, LIGHTROOM_HITS, ERRORS),
        "archives": (ARCHIVES,),
    }[arguments.mode]
    for path in reset_paths:
        if path.exists():
            path.unlink()
    rows = inventory()
    categories = Counter(row["category"] for row in rows)
    print(f"inventory complete: {len(rows)} files", flush=True)
    direct = scan_direct(rows) if arguments.mode in {"all", "direct"} else Counter()
    archive = (
        scan_archives(rows) if arguments.mode in {"all", "archives"} else Counter()
    )
    summary = {
        "run_id": "TRO-1988-metadata-usb-20260727",
        "mode": arguments.mode,
        "completed_at": now(),
        "source_root": str(ROOT),
        "source_read_only_required": True,
        "files_in_inventory": len(rows),
        "categories": dict(categories),
        "direct_scan": dict(direct),
        "archive_scan": dict(archive),
        "artifacts": {
            "inventory": str(INVENTORY),
            "content_hits": str(HITS),
            "archive_member_hits": str(ARCHIVES),
            "lightroom_catalog_hits": str(LIGHTROOM_HITS),
            "errors": str(ERRORS),
        },
    }
    replace_json(SUMMARY, summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
