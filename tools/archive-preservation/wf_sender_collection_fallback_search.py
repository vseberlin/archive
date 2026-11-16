#!/usr/bin/env python3
"""Search bounded WF-Sender fallback roots for unresolved collection-552 rows.

The primary reconciliation database is opened read-only. Only museum-digital
rows marked ``primary_searched_fallback_not_searched`` are searched. Source
files are observed by path/size/mtime only; ZIPs are inspected by member name
without extraction.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-sender")
DEFAULT_PRIMARY_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/collection-552/runs/"
    "20260728T062423Z-collection-552-primary-match"
)
DEFAULT_ARCHIVE_ROOT = Path("/media/vladimir/Elements/restore/archive")
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
FORMAT = "iss-wf-sender-fallback-search-v1"

DOCUMENTED_ALIAS_OVERRIDES = {
    "WFS-1954_SA-01": (
        "wfs-1951-54-sa-01",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-01",
    ),
    "WFS-1955_SA-02": (
        "wfs-1951-54-sa-02",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-02",
    ),
    "WFS-1955_SA-03-1": (
        "wfs-1951-54-sa-03-1",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-03-1",
    ),
    "WFS-1955_SA-03-2": (
        "wfs-1951-54-sa-03-2",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-03-2",
    ),
    "WFS-1954_SA-04": (
        "wfs-1951-54-sa-04",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-04",
    ),
    "WFS-1954_SA-05": (
        "wfs-1951-54-sa-05",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-05",
    ),
    "WFS-1954_SA-06": (
        "wfs-1951-54-sa-06",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-06",
    ),
    "WFS-1954_SA-07": (
        "wfs-1951-54-sa-07",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-07",
    ),
    "WFS-1954_SA-08": (
        "wfs-1951-54-sa-08",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-08",
    ),
    "WFS-1954_SA-09": (
        "wfs-1951-54-sa-09",
        "saved upload workbook records the original inventory "
        "WFS-1951-54_SA-09",
    ),
    "WFS-1990-SN-24": (
        "wfs-1989-24",
        "museum-digital title, date, and first-page masthead identify "
        "Nr. 24/1989",
    ),
}

FALLBACK_ROOTS = (
    Path("WF/WF-Sender Projekt/Scans"),
    Path("WF/WF-Sender Projekt/Scans_WF-Sender_MIK"),
    Path("WF/WF-Sender Projekt/Uploads"),
    Path("WF/WF-Sender Projekt/Uploads_alle-pdf-geordnet"),
    Path("WF/WF-Sender Projekt/WFS_1989-90"),
)
CONTAINER_ROOT = Path("WF/GoogleDocsBackup")


class FallbackError(RuntimeError):
    """A failed fallback-search precondition."""


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FallbackError(f"refusing to replace existing artifact: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def csv_bytes(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    atomic_write(path, csv_bytes(rows, fields))


def ensure_read_only_source(archive_root: Path) -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(archive_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if len(fields) != 4 or "ro" not in fields[3].split(","):
        raise FallbackError(
            f"source mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def alias_pattern(comparison_key: str) -> re.Pattern[str]:
    tokens = [token for token in comparison_key.casefold().split("-") if token]
    expressions: list[str] = []
    for token in tokens:
        if token == "sa":
            expressions.append(r"(?:sa|sonderausgabe)")
        elif token == "sn":
            expressions.append(r"(?:sn|sondernummer)")
        else:
            expressions.append(re.escape(token))
    separator = r"[^a-z0-9]*"
    return re.compile(
        r"(?<![a-z0-9])" + separator.join(expressions) + r"(?![a-z0-9])",
        re.IGNORECASE,
    )


def documented_alias_overrides(inventory_raw: str) -> list[dict[str, Any]]:
    override = DOCUMENTED_ALIAS_OVERRIDES.get(inventory_raw)
    if override is None:
        return []
    comparison_key, basis = override
    return [
        {
            "comparison_key": comparison_key,
            "method": "documented_crosswalk",
            "basis": basis,
            "pattern": alias_pattern(comparison_key),
        }
    ]


def load_targets(primary_run: Path) -> list[dict[str, Any]]:
    database = primary_run / "reconciliation.sqlite"
    if not database.is_file():
        raise FallbackError(f"primary reconciliation database missing: {database}")
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """
            SELECT md_object_id, inventory_raw, title, object_url
            FROM md_objects
            WHERE local_search_state = 'primary_searched_fallback_not_searched'
            ORDER BY inventory_raw COLLATE NOCASE, md_object_id
            """
        ).fetchall()
        targets: list[dict[str, Any]] = []
        for object_id, inventory, title, object_url in rows:
            aliases = connection.execute(
                """
                SELECT comparison_key, method, basis
                FROM md_aliases
                WHERE md_object_id = ?
                ORDER BY method, comparison_key
                """,
                (object_id,),
            ).fetchall()
            target_aliases = [
                {
                    "comparison_key": alias[0],
                    "method": alias[1],
                    "basis": alias[2],
                    "pattern": alias_pattern(alias[0]),
                }
                for alias in aliases
            ]
            existing_keys = {
                str(alias["comparison_key"]) for alias in target_aliases
            }
            target_aliases.extend(
                alias
                for alias in documented_alias_overrides(inventory)
                if alias["comparison_key"] not in existing_keys
            )
            targets.append(
                {
                    "md_object_id": object_id,
                    "inventory_raw": inventory,
                    "title": title,
                    "object_url": object_url,
                    "aliases": target_aliases,
                }
            )
        return targets
    finally:
        connection.close()


def match_targets(value: str, targets: list[dict[str, Any]]) -> list[dict[str, str]]:
    matches: dict[str, dict[str, str]] = {}
    for target in targets:
        for alias in target["aliases"]:
            if alias["pattern"].search(value):
                matches[target["md_object_id"]] = {
                    "md_object_id": target["md_object_id"],
                    "inventory_raw": target["inventory_raw"],
                    "comparison_key": alias["comparison_key"],
                    "alias_method": alias["method"],
                    "match_basis": f"path/member contains alias; {alias['basis']}",
                }
                break
    return sorted(matches.values(), key=lambda row: row["md_object_id"])


def search_filesystem(
    archive_root: Path,
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    root_counts: dict[str, int] = {}
    for relative_root in FALLBACK_ROOTS:
        root = archive_root / relative_root
        if not root.is_dir():
            errors.append(
                {
                    "stage": "filesystem_search",
                    "path": str(root),
                    "error": "configured fallback root is unavailable",
                }
            )
            continue
        files_seen = 0
        for directory, _, filenames in os.walk(root):
            for filename in filenames:
                files_seen += 1
                path = Path(directory, filename)
                relative = path.relative_to(archive_root).as_posix()
                matches = match_targets(relative, targets)
                if not matches:
                    continue
                try:
                    stat = path.stat()
                except OSError as error:
                    errors.append(
                        {
                            "stage": "filesystem_search",
                            "path": str(path),
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    continue
                for match in matches:
                    rows.append(
                        {
                            **match,
                            "fallback_root": relative_root.as_posix(),
                            "source_path": str(path),
                            "source_relative_path": relative,
                            "filename": filename,
                            "extension": path.suffix.casefold(),
                            "size_bytes": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "review_state": "needs_review",
                            "note": (
                                "Fallback path candidate only; no content or "
                                "representation decision made."
                            ),
                        }
                    )
        root_counts[relative_root.as_posix()] = files_seen
    rows.sort(
        key=lambda row: (
            row["inventory_raw"].casefold(),
            row["md_object_id"],
            row["source_relative_path"].casefold(),
        )
    )
    return rows, errors, root_counts


def search_containers(
    archive_root: Path,
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    members_seen = 0
    root = archive_root / CONTAINER_ROOT
    if not root.is_dir():
        return (
            rows,
            [
                {
                    "stage": "container_search",
                    "path": str(root),
                    "error": "configured container root is unavailable",
                }
            ],
            members_seen,
        )
    for path in sorted(root.rglob("*.zip"), key=lambda item: item.as_posix().casefold()):
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    members_seen += 1
                    matches = match_targets(info.filename, targets)
                    for match in matches:
                        rows.append(
                            {
                                **match,
                                "container_path": str(path),
                                "container_relative_path": path.relative_to(
                                    archive_root
                                ).as_posix(),
                                "member_path": info.filename,
                                "member_size_bytes": info.file_size,
                                "member_crc32": f"{info.CRC:08x}",
                                "review_state": "needs_review",
                                "note": (
                                    "ZIP directory candidate only; member was "
                                    "not extracted."
                                ),
                            }
                        )
        except (OSError, zipfile.BadZipFile) as error:
            errors.append(
                {
                    "stage": "container_search",
                    "path": str(path),
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    rows.sort(
        key=lambda row: (
            row["inventory_raw"].casefold(),
            row["md_object_id"],
            row["container_relative_path"].casefold(),
            row["member_path"].casefold(),
        )
    )
    return rows, errors, members_seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--primary-run", type=Path, default=DEFAULT_PRIMARY_RUN)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--run-id")
    args = parser.parse_args()

    work_root = args.work_root.expanduser().resolve()
    primary_run = args.primary_run.expanduser().resolve()
    archive_root = args.archive_root.expanduser().resolve()
    if not work_root.is_relative_to(SAFE_WORK_ROOT):
        raise FallbackError(f"work root outside safe boundary: {work_root}")
    if not archive_root.is_dir():
        raise FallbackError(f"archive root unavailable: {archive_root}")
    mount_state = ensure_read_only_source(archive_root)
    targets = load_targets(primary_run)
    if not targets:
        raise FallbackError("primary run has no unresolved fallback targets")

    run_id = args.run_id or (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-collection-552-fallback-search"
    )
    run_dir = (
        work_root
        / "work"
        / "reconciliation"
        / "collection-552"
        / "fallback-search"
        / "runs"
        / run_id
    )
    if run_dir.exists():
        raise FallbackError(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    filesystem_rows, filesystem_errors, root_counts = search_filesystem(
        archive_root, targets
    )
    container_rows, container_errors, members_seen = search_containers(
        archive_root, targets
    )
    errors = filesystem_errors + container_errors
    target_ids_with_candidates = {
        row["md_object_id"] for row in filesystem_rows + container_rows
    }
    target_rows = [
        {
            "md_object_id": target["md_object_id"],
            "inventory_raw": target["inventory_raw"],
            "title": target["title"],
            "object_url": target["object_url"],
            "fallback_candidate_count": sum(
                row["md_object_id"] == target["md_object_id"]
                for row in filesystem_rows + container_rows
            ),
            "fallback_search_state": (
                "fallback_candidate_found"
                if target["md_object_id"] in target_ids_with_candidates
                else "fallback_searched_no_path_or_member_candidate"
            ),
            "review_state": "needs_review",
        }
        for target in targets
    ]

    write_csv(
        run_dir / "target-objects.csv",
        target_rows,
        [
            "md_object_id", "inventory_raw", "title", "object_url",
            "fallback_candidate_count", "fallback_search_state", "review_state",
        ],
    )
    write_csv(
        run_dir / "filesystem-candidates.csv",
        filesystem_rows,
        [
            "md_object_id", "inventory_raw", "comparison_key", "alias_method",
            "match_basis", "fallback_root", "source_path",
            "source_relative_path", "filename", "extension", "size_bytes",
            "mtime_ns", "review_state", "note",
        ],
    )
    write_csv(
        run_dir / "container-members.csv",
        container_rows,
        [
            "md_object_id", "inventory_raw", "comparison_key", "alias_method",
            "match_basis", "container_path", "container_relative_path",
            "member_path", "member_size_bytes", "member_crc32", "review_state",
            "note",
        ],
    )
    write_csv(run_dir / "errors.csv", errors, ["stage", "path", "error"])

    per_target = Counter(row["md_object_id"] for row in filesystem_rows + container_rows)
    summary = {
        "format": FORMAT,
        "run_id": run_id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "primary_run": str(primary_run),
        "archive_root": str(archive_root),
        "source_mount": mount_state,
        "source_policy": "read-only; no hash; no copy; no extraction; no mutation",
        "targets": len(targets),
        "targets_with_candidates": len(target_ids_with_candidates),
        "targets_without_candidates": len(targets) - len(target_ids_with_candidates),
        "filesystem_candidates": len(filesystem_rows),
        "container_member_candidates": len(container_rows),
        "files_seen_by_root": dict(sorted(root_counts.items())),
        "container_members_seen": members_seen,
        "candidate_counts_by_target": dict(sorted(per_target.items())),
        "errors": len(errors),
        "decisions": 0,
        "decision_boundary": (
            "Path/member candidates only. No content conclusion, no_match_found, "
            "download, hash, copy, extraction, staging, source move, or deletion."
        ),
    }
    atomic_write(
        run_dir / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    readme = f"""# WF-Sender collection 552 fallback search

Run: `{run_id}`

This run searches only the 31 museum-digital rows unresolved after the primary
MIK PDF/TIFF reconciliation.

- Configured WF-Sender scan/upload roots were traversed once.
- Google Docs backup ZIPs were inspected by member name without extraction.
- All matches remain `needs_review`.
- A target without a path/member candidate is not yet `no_match_found`; content
  or metadata-workbook reconciliation may still be appropriate.
- The six local-only objects from the primary run remain archive candidates and
  are not changed by this search.
- No source file was opened for content, hashed, copied, extracted, staged, or
  modified.
"""
    atomic_write(run_dir / "README.md", readme.encode("utf-8"))
    print(json.dumps({"run_dir": str(run_dir), **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
