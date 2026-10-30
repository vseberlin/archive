#!/usr/bin/env python3
"""Read-only USB-wide search for Trafo documentation and metadata sources."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path("/media/vladimir/Elements")
INVENTORY = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "TRO-1988-metadata-usb-20260727/usb-file-inventory.csv"
)
PRIOR_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "trafo-documentation-usb-20260727-001"
)

PLAIN_EXTENSIONS = {
    ".csv", ".tsv", ".txt", ".xml", ".json", ".jsonl", ".html", ".htm",
    ".sql", ".log", ".md", ".xmp", ".ini", ".cfg", ".yaml", ".yml", ".rtf",
}
OFFICE_EXTENSIONS = {".xlsx", ".xlsm", ".ods", ".docx", ".odt"}
LEGACY_EXTENSIONS = {".xls", ".doc"}
DATABASE_EXTENSIONS = {
    ".db", ".sqlite", ".sqlite3", ".lrcat", ".mdb", ".accdb",
    ".db-wal", ".sqlite-wal", ".sqlite3-wal", ".lrcat-wal",
}
ARCHIVE_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz",
    ".tar.gz", ".tar.bz2", ".tar.xz", ".gz", ".bz2", ".xz",
}
PDF_EXTENSIONS = {".pdf"}
DOCUMENT_EXTENSIONS = (
    PLAIN_EXTENSIONS
    | OFFICE_EXTENSIONS
    | LEGACY_EXTENSIONS
    | DATABASE_EXTENSIONS
    | ARCHIVE_EXTENSIONS
    | PDF_EXTENSIONS
)

STRONG_TERMS: list[tuple[str, re.Pattern[bytes]]] = [
    ("tro-inventory", re.compile(rb"(?i)\bTRO[\s_.-]*\d{4}")),
    ("der-trafo", re.compile(rb"(?i)\bDER[\s_.-]+TRAFO\b")),
    ("betriebszeitung", re.compile(rb"(?i)\bBETRIEBSZEITUNG\b")),
    (
        "transformatorenwerk",
        re.compile(rb"(?i)\bTRANSFORMATORENWERK(?:ES)?\b"),
    ),
    (
        "karl-liebknecht",
        re.compile(rb"(?i)\bKARL[\s_.-]+LIEBKNECHT\b"),
    ),
]
WEAK_TRAFO = re.compile(rb"(?i)(?<![A-Z0-9])TRAFO(?![A-Z0-9])")
PATH_TRAFO = re.compile(
    r"(?i)(?:trafo|betriebszeitung|transformatorenwerk|karl.?liebknecht|"
    r"(?:^|[/_. -])tro(?:[/_. -]|$))"
)
DOC_WORD = re.compile(
    r"(?i)(?:basis|angaben|bestand|meta|katalog|catalog|tabelle|liste|"
    r"inventar|register|export|daten|beschreibung|doku|document|bericht|"
    r"sachbericht|projekt|upload|abrechnung|flugblatt|source|quelle)"
)
ISSUE_PDF = re.compile(
    r"(?i)^TRO[-_ .]?\d{4}[-_ .](?:SN|SA[-_ .])?[A-Z0-9-]+\.pdf$"
)
DATABASE_NAME = re.compile(
    r"(?i)(?:trafo|lightroom|\.lrcat|katalog|catalog|metadat|inventar|"
    r"inventory|museum|digikam|archive.?index|bildarchiv|fotoarchiv)"
)
TEXT_MEMBER_EXTENSIONS = (
    PLAIN_EXTENSIONS | OFFICE_EXTENSIONS | LEGACY_EXTENSIONS
)


class SearchError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def assert_read_only() -> list[str]:
    result = subprocess.run(
        ["findmnt", "-no", "TARGET,SOURCE,FSTYPE,OPTIONS", "--target", str(ROOT)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise SearchError(result.stderr.strip())
    lines = result.stdout.strip().splitlines()
    for line in lines:
        fields = line.split(maxsplit=3)
        if (
            len(fields) != 4
            or "ro" not in fields[3].split(",")
            or "rw" in fields[3].split(",")
        ):
            raise SearchError(f"USB source is not read-only: {line}")
    return lines


def extension(name: str) -> str:
    lowered = name.casefold()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lowered.endswith(suffix):
            return suffix
    return Path(name).suffix.casefold()


def normalize(payload: bytes) -> bytes:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")) or (
        len(payload) > 4
        and payload[1:2] == b"\x00"
        and payload[3:4] == b"\x00"
    ):
        try:
            return payload.decode("utf-16", "replace").encode("utf-8")
        except UnicodeError:
            return payload
    return payload


def payload_match(payload: bytes) -> tuple[list[str], str] | None:
    payload = normalize(payload)
    matches: list[tuple[str, re.Match[bytes]]] = []
    for label, pattern in STRONG_TERMS:
        found = pattern.search(payload)
        if found:
            matches.append((label, found))
    weak = WEAK_TRAFO.search(payload)
    if weak:
        matches.append(("trafo-word", weak))
    if not matches:
        return None
    labels = sorted({label for label, _ in matches})
    first = min((found for _, found in matches), key=lambda found: found.start())
    start = max(0, first.start() - 300)
    end = min(len(payload), first.end() + 700)
    snippet = " ".join(
        payload[start:end].decode("utf-8", "replace").split()
    )
    return labels, snippet[:3000]


def stream_match(path: Path) -> tuple[list[str], str] | None:
    overlap = b""
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            payload = overlap + block
            match = payload_match(payload)
            if match:
                return match
            overlap = payload[-8192:]
    return None


def office_payload(path: Path) -> bytes:
    chunks: list[bytes] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if (
                info.file_size <= 100 * 1024 * 1024
                and Path(info.filename).suffix.casefold()
                in {".xml", ".txt", ".csv", ".rels"}
            ):
                chunks.append(archive.read(info))
    return b"\n".join(chunks)


def office_member_payload(payload: bytes) -> bytes:
    chunks: list[bytes] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            if (
                info.file_size <= 100 * 1024 * 1024
                and Path(info.filename).suffix.casefold()
                in {".xml", ".txt", ".csv", ".rels"}
            ):
                chunks.append(archive.read(info))
    return b"\n".join(chunks)


def legacy_payload(path: Path) -> bytes:
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
    result = subprocess.run(
        ["strings", "-a", "-el", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    return result.stdout


def pdf_payload(path: Path) -> bytes:
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


def add_candidate(
    candidates: dict[str, dict[str, Any]],
    row: dict[str, str],
    reason: str,
    terms: list[str] | None = None,
    snippet: str = "",
) -> None:
    path = row["path"]
    current = candidates.setdefault(
        path,
        {
            "path": path,
            "size_bytes": int(row["size_bytes"]),
            "mtime_ns": int(row["mtime_ns"]),
            "extension": row["extension"],
            "category": row["category"],
            "in_usb_recycle_bin": "/$RECYCLE.BIN/" in path,
            "reasons": set(),
            "terms": set(),
            "snippet": "",
        },
    )
    current["reasons"].add(reason)
    current["terms"].update(terms or [])
    if snippet and not current["snippet"]:
        current["snippet"] = snippet


def load_inventory() -> list[dict[str, str]]:
    with INVENTORY.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def prior_index_seeds() -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{PRIOR_INDEX}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT path, rel_path, source_type, size, mtime_iso, status,
                   record_count, note
            FROM metadata_sources
            WHERE lower(path) LIKE '%trafo%'
               OR lower(path) LIKE '%tro-%'
               OR lower(path) LIKE '%basisangaben%'
            ORDER BY path
            """
        ).fetchall()
    finally:
        connection.close()
    output = []
    for row in rows:
        item = dict(row)
        usb_path = ROOT / "restore/archive/WF" / item["rel_path"]
        item["usb_path"] = str(usb_path)
        item["usb_exists"] = usb_path.is_file()
        item["usb_size_matches"] = (
            usb_path.is_file() and usb_path.stat().st_size == item["size"]
        )
        output.append(item)
    return output


def should_scan_pdf(row: dict[str, str]) -> bool:
    if row["category"] != "pdf":
        return False
    path = Path(row["path"])
    if int(row["size_bytes"]) > 100 * 1024 * 1024:
        return False
    path_text = str(path)
    if not PATH_TRAFO.search(path_text):
        return False
    if ISSUE_PDF.match(path.name) and not DOC_WORD.search(path.name):
        return False
    return bool(DOC_WORD.search(path_text) or not ISSUE_PDF.match(path.name))


def scan_direct(
    rows: list[dict[str, str]],
    candidates: dict[str, dict[str, Any]],
    errors: list[dict[str, str]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    scan_rows = [
        row
        for row in rows
        if row["category"] in {"plain", "office_zip", "legacy_office"}
        or should_scan_pdf(row)
    ]
    for index, row in enumerate(scan_rows, 1):
        path = Path(row["path"])
        size = int(row["size_bytes"])
        try:
            if row["category"] == "plain":
                match = (
                    stream_match(path)
                    if size > 256 * 1024 * 1024
                    else payload_match(path.read_bytes())
                )
            elif row["category"] == "office_zip":
                match = payload_match(office_payload(path))
            elif row["category"] == "legacy_office":
                match = payload_match(legacy_payload(path))
            else:
                match = payload_match(pdf_payload(path))
            counts[f"scanned_{row['category']}"] += 1
            if match:
                terms, snippet = match
                add_candidate(
                    candidates, row, "content-match", terms, snippet
                )
                counts[f"hit_{row['category']}"] += 1
        except Exception as error:
            errors.append(
                {
                    "phase": "direct-content",
                    "path": str(path),
                    "error": str(error),
                }
            )
            counts[f"error_{row['category']}"] += 1
        if index % 500 == 0:
            print(f"direct {index}/{len(scan_rows)}", flush=True)
    return counts


def scan_lightroom(
    row: dict[str, str],
    candidates: dict[str, dict[str, Any]],
) -> int:
    path = Path(row["path"])
    connection = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1", uri=True, timeout=30
    )
    hits = 0
    try:
        tables = {
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"AgLibraryFile", "Adobe_images"}.issubset(tables):
            return 0
        query = """
            SELECT f.baseName, f.originalFilename, f.extension,
                   i.captureTime, i.originalCaptureTime
            FROM AgLibraryFile AS f
            LEFT JOIN Adobe_images AS i ON i.rootFile = f.id_local
            WHERE lower(coalesce(f.baseName, '')) LIKE 'tro-%'
               OR lower(coalesce(f.originalFilename, '')) LIKE 'tro-%'
               OR lower(coalesce(f.baseName, '')) LIKE '%trafo%'
               OR lower(coalesce(f.originalFilename, '')) LIKE '%trafo%'
        """
        samples = connection.execute(query).fetchmany(20)
        total = connection.execute(
            f"SELECT count(*) FROM ({query})"
        ).fetchone()[0]
        if total:
            add_candidate(
                candidates,
                row,
                "lightroom-filename-reference",
                ["lightroom-trafo-reference"],
                json.dumps(samples, ensure_ascii=False),
            )
            hits = total
    finally:
        connection.close()
    return hits


def scan_databases(
    rows: list[dict[str, str]],
    candidates: dict[str, dict[str, Any]],
    errors: list[dict[str, str]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    database_rows = [
        row
        for row in rows
        if row["category"] == "database"
        and DATABASE_NAME.search(row["path"])
    ]
    for index, row in enumerate(database_rows, 1):
        path = Path(row["path"])
        try:
            if path.suffix.casefold() == ".lrcat":
                hits = scan_lightroom(row, candidates)
                counts["lightroom_references"] += hits
                counts["lightroom_catalogs_queried"] += 1
            elif int(row["size_bytes"]) <= 512 * 1024 * 1024:
                match = stream_match(path)
                counts["database_streams_scanned"] += 1
                if match:
                    terms, snippet = match
                    add_candidate(
                        candidates,
                        row,
                        "database-content-match",
                        terms,
                        snippet,
                    )
                    counts["database_content_hits"] += 1
            else:
                counts["large_database_skipped"] += 1
        except Exception as error:
            errors.append(
                {
                    "phase": "database",
                    "path": str(path),
                    "error": str(error),
                }
            )
            counts["database_errors"] += 1
        if index % 25 == 0:
            print(f"databases {index}/{len(database_rows)}", flush=True)
    return counts


def archive_members(path: Path) -> list[tuple[str, int]]:
    lowered = path.name.casefold()
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return [
                (info.filename, info.file_size)
                for info in archive.infolist()
            ]
    if lowered.endswith(
        (
            ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2",
            ".tar.xz", ".txz",
        )
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
            raise SearchError(
                result.stderr.decode("utf-8", "replace").strip()
            )
        return [
            (line.decode("utf-8", "replace"), 0)
            for line in result.stdout.splitlines()
        ]
    return []


def scan_archives(
    rows: list[dict[str, str]],
    candidates: dict[str, dict[str, Any]],
    archive_hits: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    archive_rows = [row for row in rows if row["category"] == "archive"]
    for index, row in enumerate(archive_rows, 1):
        path = Path(row["path"])
        handle: zipfile.ZipFile | None = None
        try:
            if path.name.casefold().endswith(".zip"):
                handle = zipfile.ZipFile(path)
                members = [
                    (info.filename, info.file_size)
                    for info in handle.infolist()
                ]
            else:
                members = archive_members(path)
            counts["archives_scanned"] += 1
            archive_matched = False
            for member, member_size in members:
                member_ext = extension(member)
                member_path = f"{path}!/{member}"
                if (
                    member_ext in DOCUMENT_EXTENSIONS
                    and PATH_TRAFO.search(member_path)
                ):
                    archive_hits.append(
                        {
                            "archive": str(path),
                            "member": member,
                            "member_size_bytes": member_size,
                            "match_type": "member-name",
                            "terms": "path-trafo",
                            "snippet": "",
                        }
                    )
                    counts["member_name_hits"] += 1
                    archive_matched = True
                if (
                    handle is not None
                    and member_ext in TEXT_MEMBER_EXTENSIONS
                    and 0 <= member_size <= 100 * 1024 * 1024
                ):
                    try:
                        payload = handle.read(member)
                        if member_ext in OFFICE_EXTENSIONS:
                            payload = office_member_payload(payload)
                        match = payload_match(payload)
                        counts["document_members_scanned"] += 1
                        if match:
                            terms, snippet = match
                            archive_hits.append(
                                {
                                    "archive": str(path),
                                    "member": member,
                                    "member_size_bytes": member_size,
                                    "match_type": "member-content",
                                    "terms": ";".join(terms),
                                    "snippet": snippet,
                                }
                            )
                            counts["member_content_hits"] += 1
                            archive_matched = True
                    except Exception as error:
                        errors.append(
                            {
                                "phase": "archive-member",
                                "path": member_path,
                                "error": str(error),
                            }
                        )
                        counts["member_content_errors"] += 1
            if archive_matched:
                add_candidate(candidates, row, "archive-member-match")
        except Exception as error:
            errors.append(
                {
                    "phase": "archive-list",
                    "path": str(path),
                    "error": str(error),
                }
            )
            counts["archive_errors"] += 1
        finally:
            if handle is not None:
                handle.close()
        if index % 25 == 0:
            print(f"archives {index}/{len(archive_rows)}", flush=True)
    return counts


def windows_recycle_search() -> dict[str, Any]:
    root = ROOT / "$RECYCLE.BIN"
    terms = re.compile(
        r"(?i)(sqlite|archive.?index|usb.?index|file.?index|inventory|"
        r"inventar|trafo|metadata|metadaten|TRO[-_ .]?\d{4})"
    )
    hits = []
    parsed = 0
    for path in root.rglob("$I*") if root.exists() else []:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        payload = path.read_bytes()
        parsed += 1
        candidates = []
        for offset in (24, 28):
            text = payload[offset:].decode("utf-16le", "ignore")
            text = text.split("\x00", 1)[0].strip()
            if text:
                candidates.append(text)
        original = max(candidates, key=len, default="")
        if terms.search(original):
            restored = path.with_name("$R" + path.name[2:])
            hits.append(
                {
                    "metadata_path": str(path),
                    "original_path": original,
                    "payload_path": str(restored),
                    "payload_exists": restored.exists(),
                    "payload_size": (
                        restored.stat().st_size
                        if restored.is_file()
                        else None
                    ),
                }
            )
    return {"metadata_records_parsed": parsed, "hits": hits}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    started = time.monotonic()
    mounts = assert_read_only()
    if OUTPUT.exists():
        raise SearchError(f"output run already exists: {OUTPUT}")
    OUTPUT.mkdir(parents=True)
    rows = load_inventory()
    candidates: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    archive_hits: list[dict[str, Any]] = []

    for row in rows:
        path = row["path"]
        if (
            row["extension"] in DOCUMENT_EXTENSIONS
            and PATH_TRAFO.search(path)
            and (
                row["category"] != "pdf"
                or should_scan_pdf(row)
            )
        ):
            add_candidate(candidates, row, "path-match")

    seeds = prior_index_seeds()
    by_path = {row["path"]: row for row in rows}
    for seed in seeds:
        row = by_path.get(seed["usb_path"])
        if row:
            add_candidate(candidates, row, "prior-index-seed")

    print(
        f"inventory {len(rows)}; path/seed candidates {len(candidates)}",
        flush=True,
    )
    direct = scan_direct(rows, candidates, errors)
    databases = scan_databases(rows, candidates, errors)
    archives = scan_archives(
        rows, candidates, archive_hits, errors
    )
    recycle = windows_recycle_search()
    final_mounts = assert_read_only()

    candidate_rows = []
    identity_errors = 0
    for item in sorted(candidates.values(), key=lambda row: row["path"]):
        path = Path(item["path"])
        identity_ok = False
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            identity_ok = (
                stat.st_size == item["size_bytes"]
                and stat.st_mtime_ns == item["mtime_ns"]
            )
        if not identity_ok:
            identity_errors += 1
        candidate_rows.append(
            {
                **item,
                "reasons": ";".join(sorted(item["reasons"])),
                "terms": ";".join(sorted(item["terms"])),
                "identity_matches_inventory": "yes" if identity_ok else "no",
            }
        )
    write_csv(
        OUTPUT / "candidate-files.csv",
        candidate_rows,
        [
            "path", "size_bytes", "mtime_ns", "extension", "category",
            "in_usb_recycle_bin", "reasons", "terms",
            "identity_matches_inventory", "snippet",
        ],
    )
    write_csv(
        OUTPUT / "archive-member-hits.csv",
        sorted(
            archive_hits,
            key=lambda row: (row["archive"], row["member"], row["match_type"]),
        ),
        [
            "archive", "member", "member_size_bytes", "match_type",
            "terms", "snippet",
        ],
    )
    write_csv(
        OUTPUT / "prior-index-seeds.csv",
        seeds,
        [
            "path", "rel_path", "source_type", "size", "mtime_iso", "status",
            "record_count", "note", "usb_path", "usb_exists",
            "usb_size_matches",
        ],
    )
    write_json(OUTPUT / "recycle-bin-audit.json", recycle)
    write_json(OUTPUT / "errors.json", errors)
    summary = {
        "run_id": OUTPUT.name,
        "completed_at": now(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "source_root": str(ROOT),
        "source_mount_before": mounts,
        "source_mount_after": final_mounts,
        "source_read_only": True,
        "source_files_hashed": 0,
        "source_files_changed": 0,
        "inventory_source": str(INVENTORY),
        "inventory_files": len(rows),
        "prior_index": str(PRIOR_INDEX),
        "prior_index_seed_rows": len(seeds),
        "prior_index_seeds_present_on_usb": sum(
            bool(seed["usb_exists"]) for seed in seeds
        ),
        "candidate_files": len(candidate_rows),
        "candidate_identity_errors": identity_errors,
        "archive_member_hits": len(archive_hits),
        "direct_scan": dict(direct),
        "database_scan": dict(databases),
        "archive_scan": dict(archives),
        "recycle_bin": {
            "metadata_records_parsed": recycle["metadata_records_parsed"],
            "relevant_hits": len(recycle["hits"]),
        },
        "errors": len(errors),
        "artifacts": {
            "candidate_files": str(OUTPUT / "candidate-files.csv"),
            "archive_member_hits": str(
                OUTPUT / "archive-member-hits.csv"
            ),
            "prior_index_seeds": str(OUTPUT / "prior-index-seeds.csv"),
            "recycle_bin_audit": str(OUTPUT / "recycle-bin-audit.json"),
            "errors": str(OUTPUT / "errors.json"),
        },
    }
    write_json(OUTPUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
