#!/usr/bin/env python3
"""Read-only whole-restore discovery and disposition audit for WF-Sender."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


FORMAT = "iss-wf-sender-restore-audit-v1"
SOURCE_ROOT = Path("/media/vladimir/Elements/restore")
FINAL_UNIT = Path("/mnt/volume/iss-archive/units/wf-sender")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-sender")
DISPOSITION = FINAL_UNIT / "db-restore/manifests/source-disposition.csv"
CHECKSUMS = FINAL_UNIT / "db-restore/checksums.csv"

PATH_PATTERNS = (
    ("wf_sender", re.compile(r"(?i)wf[\s_.-]*sender")),
    ("wfs_inventory", re.compile(r"(?i)(?:^|[/\\])wfs[\s_.-]*(?:fb[\s_.-]*)?\d{4}")),
    ("hfs_inventory", re.compile(r"(?i)(?:^|[/\\])hfs[\s_.-]*\d{4}")),
    ("hf_sender", re.compile(r"(?i)hf[\s_.-]*sender")),
    ("betriebszeitung", re.compile(r"(?i)betriebszeitung")),
)
CONTENT_PATTERNS = (
    ("wf_sender", re.compile(rb"(?i)wf[\s_.-]{0,4}sender")),
    (
        "wfs_inventory",
        re.compile(rb"(?i)(?:^|[^a-z0-9])wfs[\s_.-]{0,4}(?:fb[\s_.-]{0,4})?\d{4}"),
    ),
    (
        "hfs_inventory",
        re.compile(rb"(?i)(?:^|[^a-z0-9])hfs[\s_.-]{0,4}\d{4}"),
    ),
    ("hf_sender", re.compile(rb"(?i)hf[\s_.-]{0,4}sender")),
    (
        "betriebszeitung_wf",
        re.compile(rb"(?is)betriebszeitung.{0,160}(?:wf|werk\s+fuer\s+fernmeldewesen)"),
    ),
)
PLAIN_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".txt",
    ".xml",
    ".json",
    ".jsonl",
    ".html",
    ".htm",
    ".sql",
    ".log",
    ".md",
    ".xmp",
    ".ini",
    ".cfg",
    ".yaml",
    ".yml",
    ".rtf",
}
OFFICE_ZIP_EXTENSIONS = {".xlsx", ".xlsm", ".ods", ".docx", ".odt", ".pptx"}
LEGACY_OFFICE_EXTENSIONS = {".xls", ".doc", ".ppt"}
DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".lrcat", ".mdb", ".accdb"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".tgz", ".tbz", ".tbz2", ".txz"}
PDF_EXTENSIONS = {".pdf"}
SYSTEM_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}
TEMP_PREFIXES = ("~$", ".~lock.")
INVENTORY_FIELDS = (
    "source_path",
    "relative_path",
    "size_bytes",
    "mtime_ns",
    "extension",
    "category",
    "known_disposition",
    "known_preservation_status",
    "known_final_path",
    "path_match",
    "path_match_reasons",
    "final_size_match_count",
    "noise_hint",
)


class AuditError(RuntimeError):
    """Raised when the protected-source audit contract is not satisfied."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_is_read_only(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    return bool(os.statvfs(path).f_flag & readonly_flag)


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.expanduser().resolve())
    except ValueError as error:
        raise AuditError(f"path escaped allowed root: {resolved}") from error
    return resolved


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


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


def path_matches(path: str) -> list[str]:
    return [label for label, pattern in PATH_PATTERNS if pattern.search(path)]


def noise_hint(path: Path) -> str:
    lowered = path.name.casefold()
    if lowered in SYSTEM_NAMES:
        return "system_metadata"
    if lowered.startswith(TEMP_PREFIXES):
        return "temporary_or_lock_file"
    if path.suffix.casefold() in {".css", ".html", ".htm"}:
        return "generated_access_context"
    if path.suffix.casefold() in {".tmp", ".temp", ".bak"}:
        return "temporary_or_backup_hint"
    return ""


def iter_files(root: Path) -> Iterable[Path]:
    for base, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(base) / name).is_symlink()
        ]
        for filename in filenames:
            path = Path(base) / filename
            if not path.is_symlink():
                yield path


def inventory(run_dir: Path) -> dict[str, Any]:
    if not SOURCE_ROOT.is_dir():
        raise AuditError(f"source root unavailable: {SOURCE_ROOT}")
    if not root_is_read_only(SOURCE_ROOT):
        raise AuditError(f"source root is not read-only: {SOURCE_ROOT}")
    if not FINAL_UNIT.is_dir():
        raise AuditError(f"final unit unavailable: {FINAL_UNIT}")
    disposition_rows = load_csv(DISPOSITION)
    disposition_by_path = {row["source_path"]: row for row in disposition_rows}
    checksum_rows = load_csv(CHECKSUMS)
    checksums_by_size: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in checksum_rows:
        checksums_by_size[int(row["size_bytes"])].append(row)

    output = run_dir / "restore-inventory.csv"
    temporary = run_dir / ".restore-inventory.csv.tmp"
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    top_directories: Counter[str] = Counter()
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=INVENTORY_FIELDS, quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        for path in iter_files(SOURCE_ROOT):
            counts["paths_seen"] += 1
            try:
                stat = path.stat()
            except OSError as error:
                errors.append({"source_path": str(path), "error": str(error)})
                counts["stat_errors"] += 1
                continue
            relative = path.relative_to(SOURCE_ROOT).as_posix()
            top_directories[relative.split("/", 1)[0]] += 1
            known = disposition_by_path.get(str(path), {})
            matches = path_matches(relative)
            hint = noise_hint(path)
            if known:
                counts["known_disposition_paths"] += 1
            if matches:
                counts["path_matches"] += 1
                if not known:
                    counts["new_path_matches"] += 1
            if hint:
                counts[f"noise_hint:{hint}"] += 1
            size_matches = checksums_by_size.get(stat.st_size, [])
            if size_matches:
                counts["final_size_match_paths"] += 1
                if not known:
                    counts["new_final_size_match_paths"] += 1
                    counts["new_final_size_match_bytes"] += stat.st_size
            counts[f"category:{category(path)}"] += 1
            counts["bytes_seen"] += stat.st_size
            writer.writerow(
                {
                    "source_path": str(path),
                    "relative_path": relative,
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "extension": path.suffix.casefold(),
                    "category": category(path),
                    "known_disposition": "true" if known else "false",
                    "known_preservation_status": known.get("preservation_status", ""),
                    "known_final_path": known.get("final_path", ""),
                    "path_match": "true" if matches else "false",
                    "path_match_reasons": "|".join(matches),
                    "final_size_match_count": len(size_matches),
                    "noise_hint": hint,
                }
            )
            if counts["paths_seen"] % 50000 == 0:
                print(
                    f"inventory paths={counts['paths_seen']} "
                    f"new_path_matches={counts['new_path_matches']}",
                    flush=True,
                )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    with (run_dir / "inventory-errors.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("source_path", "error"), quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(errors)
    summary = {
        "format": FORMAT,
        "phase": "inventory",
        "started_source_root": str(SOURCE_ROOT),
        "source_read_only_observed": True,
        "final_unit": str(FINAL_UNIT),
        "existing_source_disposition_rows": len(disposition_rows),
        "final_checksum_rows": len(checksum_rows),
        "counts": dict(sorted(counts.items())),
        "top_directories": dict(top_directories.most_common()),
        "completed_at": utc_now(),
        "artifacts": {
            "inventory": str(output),
            "errors": str(run_dir / "inventory-errors.csv"),
        },
    }
    atomic_json(run_dir / "inventory-summary.json", summary)
    return summary


def normalized_payload(payload: bytes) -> bytes:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return payload.decode("utf-16", "replace").encode("utf-8")
        except UnicodeError:
            pass
    return payload


def first_content_match(payload: bytes) -> tuple[str, str] | None:
    payload = normalized_payload(payload)
    for label, pattern in CONTENT_PATTERNS:
        match = pattern.search(payload)
        if match:
            start = max(0, match.start() - 180)
            end = min(len(payload), match.end() + 300)
            snippet = " ".join(
                payload[start:end].decode("utf-8", "replace").split()
            )
            return label, snippet[:2000]
    return None


def run_command(arguments: list[str], timeout: int) -> bytes:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise AuditError(detail or f"{arguments[0]} exited {result.returncode}")
    return result.stdout


def read_direct(path: Path, kind: str) -> bytes:
    if kind == "plain":
        if path.stat().st_size > 256 * 1024 * 1024:
            raise AuditError("plain file exceeds 256 MiB extraction limit")
        return path.read_bytes()
    if kind == "office_zip":
        chunks: list[bytes] = []
        total = 0
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                suffix = Path(member.filename).suffix.casefold()
                if suffix not in {".xml", ".txt", ".csv", ".rels"}:
                    continue
                if member.file_size > 64 * 1024 * 1024:
                    continue
                total += member.file_size
                if total > 512 * 1024 * 1024:
                    raise AuditError("Office XML exceeds 512 MiB extraction limit")
                chunks.append(archive.read(member))
        return b"\n".join(chunks)
    if kind == "legacy_office":
        tool = "xls2csv" if path.suffix.casefold() == ".xls" else "catdoc"
        return run_command([tool, str(path)], 180)
    if kind == "pdf":
        return run_command(
            ["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(path), "-"], 300
        )
    if kind == "database":
        ascii_strings = run_command(["strings", "-a", "-n", "4", str(path)], 900)
        utf16_strings = run_command(
            ["strings", "-a", "-el", "-n", "4", str(path)], 900
        )
        return ascii_strings + b"\n" + utf16_strings
    raise AuditError(f"unsupported direct extraction category: {kind}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def search(run_dir: Path) -> dict[str, Any]:
    if not root_is_read_only(SOURCE_ROOT):
        raise AuditError(f"source root is not read-only: {SOURCE_ROOT}")
    inventory_path = run_dir / "restore-inventory.csv"
    if not inventory_path.is_file():
        raise AuditError(f"inventory missing: {inventory_path}")
    checksum_rows = load_csv(CHECKSUMS)
    checksums_by_size: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in checksum_rows:
        checksums_by_size[int(row["size_bytes"])].append(row)
    checksum_by_digest = {row["sha256"]: row for row in checksum_rows}

    path_hits: list[dict[str, str]] = []
    content_hits: list[dict[str, str]] = []
    exact_matches: list[dict[str, str]] = []
    archive_hits: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    with inventory_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    for index, row in enumerate(rows, 1):
        path = Path(row["source_path"])
        known = row["known_disposition"] == "true"
        if row["path_match"] == "true" and not known:
            path_hits.append(row)
        if known:
            continue
        kind = row["category"]
        size = int(row["size_bytes"])
        if int(row["final_size_match_count"]) and size >= 4096:
            counts["hash_candidates"] += 1
            counts["hash_candidate_bytes"] += size
            try:
                digest = sha256_file(path)
                final = checksum_by_digest.get(digest)
                if final:
                    exact_matches.append(
                        {
                            "source_path": str(path),
                            "size_bytes": str(size),
                            "source_sha256": digest,
                            "final_path": final["path"],
                            "final_sha256": final["sha256"],
                            "basis": "exact_sha256_and_size",
                        }
                    )
                    counts["exact_final_matches"] += 1
            except OSError as error:
                errors.append(
                    {
                        "phase": "hash",
                        "source_path": str(path),
                        "error": str(error),
                    }
                )
        if kind in {"plain", "office_zip", "legacy_office", "database", "pdf"}:
            counts[f"content_candidates:{kind}"] += 1
            try:
                match = first_content_match(read_direct(path, kind))
                if match:
                    label, snippet = match
                    content_hits.append(
                        {
                            "source_path": str(path),
                            "category": kind,
                            "match_reason": label,
                            "snippet": snippet,
                        }
                    )
                    counts[f"content_hits:{kind}"] += 1
            except (
                OSError,
                AuditError,
                subprocess.TimeoutExpired,
                zipfile.BadZipFile,
                RuntimeError,
            ) as error:
                errors.append(
                    {
                        "phase": "content",
                        "source_path": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        elif kind == "archive":
            counts["archive_candidates"] += 1
            try:
                if path.suffix.casefold() != ".zip":
                    listing = run_command(["7z", "l", "-ba", str(path)], 300)
                    match = first_content_match(listing)
                    if match:
                        archive_hits.append(
                            {
                                "source_path": str(path),
                                "member": "",
                                "match_reason": f"archive_listing:{match[0]}",
                                "snippet": match[1],
                            }
                        )
                    continue
                with zipfile.ZipFile(path) as archive:
                    for member in archive.infolist():
                        name_match = path_matches(member.filename)
                        if name_match:
                            archive_hits.append(
                                {
                                    "source_path": str(path),
                                    "member": member.filename,
                                    "match_reason": "member_name:" + "|".join(name_match),
                                    "snippet": "",
                                }
                            )
                        suffix = Path(member.filename).suffix.casefold()
                        if (
                            suffix
                            not in PLAIN_EXTENSIONS
                            | OFFICE_ZIP_EXTENSIONS
                            | LEGACY_OFFICE_EXTENSIONS
                            or member.file_size > 64 * 1024 * 1024
                        ):
                            continue
                        payload = archive.read(member)
                        match = first_content_match(payload)
                        if match:
                            archive_hits.append(
                                {
                                    "source_path": str(path),
                                    "member": member.filename,
                                    "match_reason": f"member_content:{match[0]}",
                                    "snippet": match[1],
                                }
                            )
            except (
                OSError,
                AuditError,
                subprocess.TimeoutExpired,
                zipfile.BadZipFile,
                RuntimeError,
            ) as error:
                errors.append(
                    {
                        "phase": "archive",
                        "source_path": str(path),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        if index % 5000 == 0:
            print(
                f"search paths={index}/{len(rows)} "
                f"path_hits={len(path_hits)} content_hits={len(content_hits)} "
                f"exact_matches={len(exact_matches)}",
                flush=True,
            )

    def write_csv(
        name: str, fields: tuple[str, ...], values: list[dict[str, str]]
    ) -> None:
        with (run_dir / name).open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fields,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            writer.writerows(values)

    write_csv("path-hits.csv", INVENTORY_FIELDS, path_hits)
    write_csv(
        "content-hits.csv",
        ("source_path", "category", "match_reason", "snippet"),
        content_hits,
    )
    write_csv(
        "exact-final-matches.csv",
        (
            "source_path",
            "size_bytes",
            "source_sha256",
            "final_path",
            "final_sha256",
            "basis",
        ),
        exact_matches,
    )
    write_csv(
        "archive-member-hits.csv",
        ("source_path", "member", "match_reason", "snippet"),
        archive_hits,
    )
    write_csv("search-errors.csv", ("phase", "source_path", "error"), errors)
    summary = {
        "format": FORMAT,
        "phase": "search",
        "source_root": str(SOURCE_ROOT),
        "source_read_only_observed": True,
        "final_unit": str(FINAL_UNIT),
        "inventory_rows": len(rows),
        "counts": {
            **dict(sorted(counts.items())),
            "new_path_hits": len(path_hits),
            "new_content_hits": len(content_hits),
            "new_exact_final_matches": len(exact_matches),
            "archive_member_hits": len(archive_hits),
            "errors": len(errors),
        },
        "completed_at": utc_now(),
    }
    atomic_json(run_dir / "search-summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("inventory", "search"), required=True)
    parser.add_argument("--run-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = inside(args.run_dir, WORK_ROOT)
    run_dir.mkdir(parents=True, exist_ok=args.phase == "search")
    try:
        result = inventory(run_dir) if args.phase == "inventory" else search(run_dir)
    except (AuditError, OSError, ValueError, csv.Error) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
