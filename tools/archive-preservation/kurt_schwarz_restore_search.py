#!/usr/bin/env python3
"""Search reused USB inventory for Kurt-Schwarz records outside bounded roots.

The source USB is read-only.  This tool does not copy, move, rename, or delete
source files.  It searches filenames, extractable document text, database
strings, and archive members and writes an immutable evidence run below the
Kurt-Schwarz work root.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-kurt-schwarz-restore-search-v1"
SOURCE_MOUNT = Path("/media/vladimir/Elements")
SOURCE_ROOT = SOURCE_MOUNT / "restore"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_INVENTORY = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore/restore-inventory.csv"
)

EXCLUSIVE_ROOTS = (
    "archive/WF/Kurt Schwarz/",
    "archive/WF/Ausstellung_Kurt-Schwarz/",
    "archive/Schwarz, Kurt/",
    "archive/WF/Canon-SCANS-freierOrdner/Kurt_Schwarz_Albert/",
    "archive/WF/Freistellen/Schwarz, Kurt/",
)
SHARED_ROOTS = (
    "archive/WF/Lightroom-Katalog_nur-WF/",
    "archive/WF/Daten/Lightroom-Kataloge-live/",
    "archive/WF/WF-Sender Projekt/Webseite/",
    "archive/WF/Kurt Schwarz/Backup_Lightroom_KS/",
    (
        "archive/WF/Ausstellung_Kurt-Schwarz/Reihenfolge_Ausstellung-KS/"
        "Ausstellung_Reihenfolge/"
    ),
)

TEXT_PATTERNS = (
    ("kurt_schwarz", re.compile(r"(?i)kurt[\s_,.;:/\\-]{0,12}schwarz")),
    ("schwarz_kurt", re.compile(r"(?i)schwarz[\s_,.;:/\\-]{0,12}kurt")),
    (
        "ks_inventory",
        re.compile(r"(?i)(?:^|[^a-z0-9])KS-[5-9]-(?:BZ|NB|WF|ZP)[_-]\d{2,}"),
    ),
)
PATH_PATTERNS = tuple(
    (label, re.compile(pattern.pattern, pattern.flags))
    for label, pattern in TEXT_PATTERNS
)

PLAIN_EXTENSIONS = {
    ".csv", ".tsv", ".txt", ".xml", ".json", ".jsonl", ".html", ".htm",
    ".sql", ".log", ".md", ".xmp", ".ini", ".cfg", ".yaml", ".yml", ".rtf",
}
OFFICE_ZIP_EXTENSIONS = {".xlsx", ".xlsm", ".ods", ".docx", ".odt", ".pptx"}
LEGACY_OFFICE_EXTENSIONS = {".xls", ".doc", ".ppt"}
DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".lrcat", ".mdb", ".accdb"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz"}
PDF_EXTENSIONS = {".pdf"}
SEARCHABLE_MEMBER_EXTENSIONS = (
    PLAIN_EXTENSIONS | OFFICE_ZIP_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS | PDF_EXTENSIONS
)


class SearchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_is_read_only(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    return bool(os.statvfs(path).f_flag & readonly_flag)


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SearchError(f"path escaped allowed root: {resolved}") from error
    return resolved


def path_reasons(value: str) -> list[str]:
    return [label for label, pattern in PATH_PATTERNS if pattern.search(value)]


def known_scope(relative_path: str) -> str:
    for root in SHARED_ROOTS:
        if relative_path.startswith(root):
            return "shared_source"
    for root in EXCLUSIVE_ROOTS:
        if relative_path.startswith(root):
            return "exclusive_project_root"
    return ""


def category(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in PLAIN_EXTENSIONS:
        return "plain"
    if suffix in OFFICE_ZIP_EXTENSIONS:
        return "office_zip"
    if suffix in LEGACY_OFFICE_EXTENSIONS:
        return "legacy_office"
    if suffix in DATABASE_EXTENSIONS:
        return "database"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    return "other"


def normalized_text(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", "replace")
    text = payload.decode("utf-8", "replace")
    if text.count("\x00") > max(8, len(text) // 10):
        utf16 = payload.decode("utf-16-le", "replace")
        if utf16.count("\ufffd") < text.count("\ufffd") + text.count("\x00"):
            return utf16
    return text.replace("\x00", " ")


def first_match(text: str) -> tuple[str, str] | None:
    for label, pattern in TEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 240)
            end = min(len(text), match.end() + 500)
            snippet = " ".join(text[start:end].split())
            return label, snippet[:2500]
    return None


def office_payload(payload: bytes) -> str:
    chunks: list[bytes] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            if Path(member.filename).suffix.casefold() not in {".xml", ".txt", ".csv", ".rels"}:
                continue
            if member.file_size > 64 * 1024 * 1024:
                continue
            total += member.file_size
            if total > 512 * 1024 * 1024:
                raise SearchError("Office XML exceeds 512 MiB extraction limit")
            chunks.append(archive.read(member))
    return normalized_text(b"\n".join(chunks))


def run_text_command(arguments: list[str], timeout: int) -> str:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SearchError((detail or f"{arguments[0]} exited {result.returncode}")[:2000])
    return normalized_text(result.stdout)


def search_database(path: Path) -> tuple[str, str] | None:
    for encoding in ("-a", "-el"):
        process = subprocess.Popen(
            ["strings", encoding, "-n", "4", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            for raw in process.stdout:
                match = first_match(normalized_text(raw))
                if match:
                    process.terminate()
                    process.wait(timeout=10)
                    return match
            _, error = process.communicate(timeout=900)
            if process.returncode:
                raise SearchError(error.decode("utf-8", "replace")[:2000])
        except BaseException:
            process.kill()
            process.wait()
            raise
    return None


def searchable_text(path: Path, kind: str) -> str:
    if kind == "plain":
        if path.stat().st_size > 512 * 1024 * 1024:
            raise SearchError("plain file exceeds 512 MiB extraction limit")
        return normalized_text(path.read_bytes())
    if kind == "office_zip":
        return office_payload(path.read_bytes())
    if kind == "legacy_office":
        tool = "xls2csv" if path.suffix.casefold() == ".xls" else "catdoc"
        return run_text_command([tool, str(path)], 180)
    if kind == "pdf":
        return run_text_command(
            ["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(path), "-"], 300
        )
    raise SearchError(f"unsupported category: {kind}")


def search_zip(path: Path) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            reasons = path_reasons(member.filename)
            if reasons:
                hits.append({
                    "member": member.filename,
                    "match_reason": "member_name:" + "|".join(reasons),
                    "snippet": "",
                })
            suffix = Path(member.filename).suffix.casefold()
            if suffix not in SEARCHABLE_MEMBER_EXTENSIONS or member.file_size > 64 * 1024 * 1024:
                continue
            payload = archive.read(member)
            try:
                text = office_payload(payload) if suffix in OFFICE_ZIP_EXTENSIONS else normalized_text(payload)
                match = first_match(text)
            except (OSError, SearchError, zipfile.BadZipFile):
                match = None
            if match:
                hits.append({
                    "member": member.filename,
                    "match_reason": "member_content:" + match[0],
                    "snippet": match[1],
                })
    return hits


def search_nonzip_archive(path: Path) -> list[dict[str, str]]:
    listing = run_text_command(["7z", "l", "-slt", str(path)], 300)
    hits: list[dict[str, str]] = []
    for line in listing.splitlines():
        if not line.startswith("Path = "):
            continue
        member = line[7:]
        reasons = path_reasons(member)
        if reasons:
            hits.append({
                "member": member,
                "match_reason": "member_name:" + "|".join(reasons),
                "snippet": "",
            })
    return hits


def search_one(row: dict[str, str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    path = Path(row["source_path"])
    kind = category(path)
    hits: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        if kind == "database":
            match = search_database(path)
        elif kind in {"plain", "office_zip", "legacy_office", "pdf"}:
            match = first_match(searchable_text(path, kind))
        elif kind == "archive":
            archive_hits = (
                search_zip(path)
                if path.suffix.casefold() == ".zip"
                else search_nonzip_archive(path)
            )
            for hit in archive_hits:
                hits.append({
                    "source_path": str(path),
                    "relative_path": row["relative_path"],
                    "source_scope": known_scope(row["relative_path"]),
                    "category": kind,
                    **hit,
                })
            match = None
        else:
            match = None
        if match:
            hits.append({
                "source_path": str(path),
                "relative_path": row["relative_path"],
                "source_scope": known_scope(row["relative_path"]),
                "category": kind,
                "member": "",
                "match_reason": "content:" + match[0],
                "snippet": match[1],
            })
    except (
        OSError,
        SearchError,
        subprocess.TimeoutExpired,
        zipfile.BadZipFile,
        RuntimeError,
    ) as error:
        errors.append({
            "source_path": str(path),
            "relative_path": row["relative_path"],
            "category": kind,
            "error": f"{type(error).__name__}: {str(error)[:2000]}",
        })
    return hits, errors


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    if path.exists():
        raise SearchError(f"refusing to overwrite evidence: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore", quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise SearchError(f"refusing to overwrite evidence: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run(inventory: Path, output: Path, workers: int) -> dict[str, Any]:
    if not SOURCE_ROOT.is_dir() or not root_is_read_only(SOURCE_MOUNT):
        raise SearchError(f"USB source is unavailable or not read-only: {SOURCE_MOUNT}")
    output.mkdir(parents=True, exist_ok=False)

    path_hits: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []
    inventory_rows = 0
    with inventory.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            inventory_rows += 1
            relative = row["relative_path"]
            scope = known_scope(relative)
            reasons = path_reasons(relative)
            if reasons and scope != "exclusive_project_root":
                path_hits.append({
                    "source_path": row["source_path"],
                    "relative_path": relative,
                    "source_scope": scope,
                    "category": category(Path(relative)),
                    "match_reason": "|".join(reasons),
                    "size_bytes": row["size_bytes"],
                    "deletion_authorized": "false",
                })
            kind = category(Path(relative))
            if scope != "exclusive_project_root" and kind in {
                "plain", "office_zip", "legacy_office", "database", "pdf", "archive"
            }:
                candidates.append(row)

    hits: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(search_one, row): row for row in candidates}
        for index, future in enumerate(as_completed(futures), 1):
            row_hits, row_errors = future.result()
            hits.extend(row_hits)
            errors.extend(row_errors)
            counts[f"searched:{category(Path(futures[future]['relative_path']))}"] += 1
            if index % 1000 == 0:
                print(
                    f"searched={index}/{len(candidates)} "
                    f"content_or_member_hits={len(hits)} errors={len(errors)}",
                    flush=True,
                )

    path_hits.sort(key=lambda row: row["relative_path"])
    hits.sort(key=lambda row: (row["relative_path"], row["member"], row["match_reason"]))
    errors.sort(key=lambda row: row["relative_path"])
    hit_fields = (
        "source_path", "relative_path", "source_scope", "category", "member",
        "match_reason", "snippet",
    )
    atomic_csv(
        output / "path-hits.csv",
        (
            "source_path", "relative_path", "source_scope", "category",
            "match_reason", "size_bytes", "deletion_authorized",
        ),
        path_hits,
    )
    atomic_csv(output / "content-and-archive-hits.csv", hit_fields, hits)
    atomic_csv(
        output / "search-errors.csv",
        ("source_path", "relative_path", "category", "error"),
        errors,
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "source_root": str(SOURCE_ROOT),
        "source_read_only": True,
        "reused_inventory": str(inventory),
        "inventory_rows": inventory_rows,
        "search_candidates": len(candidates),
        "path_hits": len(path_hits),
        "content_and_archive_hits": len(hits),
        "errors": len(errors),
        "counts": dict(sorted(counts.items())),
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(args.inventory.expanduser().resolve(), output, args.workers)
    except (SearchError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
