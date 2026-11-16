#!/usr/bin/env python3
"""Build a read-only collection-552 WF-Sender reconciliation work database.

The command imports an immutable museum-digital capture and inventories the
primary local MIK PDF/TIFF trees once. It emits candidate relationships only:
no source hashing, copying, downloading, staging, accession, or decisions.

Local issue groups without a museum-digital candidate remain explicit
``local_only_archive_candidate`` rows for curator review.
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
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_WORK_ROOT = Path("/mnt/volume/iss-archive-work/wf-sender")
DEFAULT_CAPTURE_RUN = DEFAULT_WORK_ROOT / (
    "work/reconciliation/museum-digital/runs/20260728T060935Z-wf-sender"
)
DEFAULT_SOURCE_ROOT = Path("/media/vladimir/Elements/restore/archive/MIK-Daten")
SAFE_WORK_ROOT = Path("/mnt/volume/iss-archive-work")
FORMAT = "iss-wf-sender-collection-reconciliation-v1"

MATCH_RANK = {
    "exact_inventory": 0,
    "normalized_number": 1,
    "documented_alias": 2,
}


class ReconciliationError(RuntimeError):
    """A failed reconciliation precondition."""


@dataclass(frozen=True)
class Alias:
    comparison_key: str
    method: str
    basis: str


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


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ReconciliationError(f"refusing to replace existing artifact: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    atomic_write(path, csv_bytes(rows, fields))


def normalized_token(value: str) -> tuple[str, bool, list[str]]:
    """Normalize syntax while retaining whether a documented alias was used."""
    original = value.strip()
    normalized = original.upper().replace("_", "-")
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    reasons: list[str] = []
    documented = False

    if normalized.startswith("WSF-"):
        normalized = "WFS-" + normalized[4:]
        reasons.append("correct WSF/WFS transposition")
        documented = True
    if "SONDERAUSGABE" in normalized:
        normalized = normalized.replace("SONDERAUSGABE", "SA")
        reasons.append("Sonderausgabe to SA")
        documented = True
    if "SONDERNUMMER" in normalized:
        normalized = normalized.replace("SONDERNUMMER", "SN")
        reasons.append("Sondernummer to SN")
        documented = True

    parts = normalized.split("-")
    year_index = -1
    if len(parts) >= 3 and parts[0] == "WFS":
        year_index = 2 if parts[1] == "FB" else 1
    if year_index >= 0 and year_index < len(parts):
        for index in range(year_index + 1, len(parts)):
            if parts[index].isdigit() and len(parts[index]) == 1:
                parts[index] = parts[index].zfill(2)
                reasons.append("zero-pad numeric component")
    return "-".join(parts), documented, reasons


def inventory_aliases(value: str) -> list[Alias]:
    """Return auditable comparison aliases without changing the source value."""
    raw = value.strip()
    aliases: dict[str, Alias] = {}
    if not raw:
        return []

    exact_key = raw.casefold()
    aliases[exact_key] = Alias(exact_key, "exact_inventory", "exact source string")

    normalized, documented, reasons = normalized_token(raw)
    normalized_key = normalized.casefold()
    if normalized_key != exact_key:
        method = "documented_alias" if documented else "normalized_number"
        aliases[normalized_key] = Alias(
            normalized_key,
            method,
            "; ".join(reasons) or "normalize separators and case",
        )

    return sorted(
        aliases.values(),
        key=lambda row: (MATCH_RANK[row.method], row.comparison_key),
    )


def combine_match(md_alias: Alias, local_alias: Alias) -> tuple[str, str]:
    method = max(
        (md_alias.method, local_alias.method),
        key=lambda value: MATCH_RANK[value],
    )
    basis_parts = [
        f"md:{md_alias.basis}",
        f"local:{local_alias.basis}",
    ]
    return method, "; ".join(basis_parts)


def ensure_read_only_source(source_root: Path) -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(source_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if len(fields) != 4 or "ro" not in fields[3].split(","):
        raise ReconciliationError(
            f"source mount is not read-only: {result.stdout.strip()}"
        )
    return result.stdout.strip()


def ensure_paths(capture_run: Path, source_root: Path, work_root: Path) -> None:
    capture_run = capture_run.resolve()
    source_root = source_root.resolve()
    work_root = work_root.resolve()
    if not (capture_run / "objects.csv").is_file():
        raise ReconciliationError(f"capture objects.csv unavailable: {capture_run}")
    if not (capture_run / "coverage.json").is_file():
        raise ReconciliationError(f"capture coverage.json unavailable: {capture_run}")
    if not source_root.is_dir():
        raise ReconciliationError(f"MIK source root unavailable: {source_root}")
    if not work_root.is_relative_to(SAFE_WORK_ROOT):
        raise ReconciliationError(f"work root outside safe boundary: {work_root}")
    if work_root.is_relative_to(source_root):
        raise ReconciliationError("work root must not be inside source root")


def load_md_rows(capture_run: Path) -> list[dict[str, str]]:
    with (capture_run / "objects.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = [row.get("object_id", "") for row in rows]
    if not rows or any(not value for value in ids):
        raise ReconciliationError("capture contains empty or no object IDs")
    if len(ids) != len(set(ids)):
        raise ReconciliationError("capture contains duplicate object IDs")
    return rows


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;

        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            format TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            capture_run TEXT NOT NULL,
            source_root TEXT NOT NULL,
            source_mount TEXT NOT NULL,
            source_policy TEXT NOT NULL
        );

        CREATE TABLE md_objects (
            md_object_id TEXT PRIMARY KEY,
            inventory_raw TEXT NOT NULL,
            title TEXT NOT NULL,
            object_type TEXT NOT NULL,
            date_text TEXT NOT NULL,
            object_url TEXT NOT NULL,
            duplicate_inventory INTEGER NOT NULL CHECK (duplicate_inventory IN (0, 1)),
            local_candidate_count INTEGER NOT NULL DEFAULT 0,
            local_search_state TEXT NOT NULL DEFAULT 'primary_not_searched',
            review_state TEXT NOT NULL DEFAULT 'needs_review'
        );

        CREATE INDEX md_objects_inventory_idx ON md_objects(inventory_raw);

        CREATE TABLE md_resources (
            md_object_id TEXT NOT NULL REFERENCES md_objects(md_object_id),
            resource_id TEXT NOT NULL,
            resource_url TEXT NOT NULL,
            resource_role TEXT NOT NULL,
            PRIMARY KEY (md_object_id, resource_id, resource_url)
        );

        CREATE TABLE md_aliases (
            md_object_id TEXT NOT NULL REFERENCES md_objects(md_object_id),
            comparison_key TEXT NOT NULL,
            method TEXT NOT NULL,
            basis TEXT NOT NULL,
            PRIMARY KEY (md_object_id, comparison_key)
        );

        CREATE TABLE local_objects (
            local_object_id INTEGER PRIMARY KEY,
            local_object_key TEXT NOT NULL UNIQUE,
            representative_inventory TEXT NOT NULL,
            group_count INTEGER NOT NULL DEFAULT 0,
            md_candidate_count INTEGER NOT NULL DEFAULT 0,
            local_only_archive_candidate INTEGER NOT NULL DEFAULT 0
                CHECK (local_only_archive_candidate IN (0, 1)),
            review_state TEXT NOT NULL DEFAULT 'needs_review'
        );

        CREATE TABLE local_groups (
            local_group_id INTEGER PRIMARY KEY,
            local_object_id INTEGER NOT NULL REFERENCES local_objects(local_object_id),
            source_family TEXT NOT NULL,
            issue_key_raw TEXT NOT NULL,
            year_directory TEXT NOT NULL,
            source_directory TEXT NOT NULL UNIQUE,
            source_relative_directory TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            total_size_bytes INTEGER NOT NULL,
            md_candidate_count INTEGER NOT NULL DEFAULT 0,
            local_only_archive_candidate INTEGER NOT NULL DEFAULT 0
                CHECK (local_only_archive_candidate IN (0, 1)),
            review_state TEXT NOT NULL DEFAULT 'needs_review'
        );

        CREATE INDEX local_groups_key_idx ON local_groups(issue_key_raw);

        CREATE TABLE local_files (
            local_file_id INTEGER PRIMARY KEY,
            local_group_id INTEGER NOT NULL REFERENCES local_groups(local_group_id),
            source_path TEXT NOT NULL UNIQUE,
            source_relative_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            file_role TEXT NOT NULL,
            page_sequence INTEGER
        );

        CREATE TABLE local_aliases (
            local_group_id INTEGER NOT NULL REFERENCES local_groups(local_group_id),
            comparison_key TEXT NOT NULL,
            method TEXT NOT NULL,
            basis TEXT NOT NULL,
            PRIMARY KEY (local_group_id, comparison_key)
        );

        CREATE TABLE candidate_matches (
            md_object_id TEXT NOT NULL REFERENCES md_objects(md_object_id),
            local_group_id INTEGER NOT NULL REFERENCES local_groups(local_group_id),
            match_method TEXT NOT NULL,
            comparison_key TEXT NOT NULL,
            match_basis TEXT NOT NULL,
            review_state TEXT NOT NULL DEFAULT 'needs_review',
            PRIMARY KEY (md_object_id, local_group_id)
        );

        CREATE TABLE exceptions (
            exception_id INTEGER PRIMARY KEY,
            exception_type TEXT NOT NULL,
            md_object_id TEXT,
            local_object_id INTEGER,
            local_group_id INTEGER,
            inventory_or_key TEXT NOT NULL,
            detail TEXT NOT NULL,
            review_state TEXT NOT NULL DEFAULT 'needs_review',
            FOREIGN KEY (md_object_id) REFERENCES md_objects(md_object_id),
            FOREIGN KEY (local_object_id) REFERENCES local_objects(local_object_id),
            FOREIGN KEY (local_group_id) REFERENCES local_groups(local_group_id)
        );

        CREATE TABLE decisions (
            decision_id INTEGER PRIMARY KEY,
            decided_utc TEXT NOT NULL,
            actor TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            basis TEXT NOT NULL
        );
        """
    )


def import_md(
    connection: sqlite3.Connection,
    rows: list[dict[str, str]],
) -> None:
    inventory_counts = Counter(row["inventory"] for row in rows)
    for row in rows:
        object_id = row["object_id"]
        inventory = row["inventory"]
        connection.execute(
            """
            INSERT INTO md_objects (
                md_object_id, inventory_raw, title, object_type, date_text,
                object_url, duplicate_inventory
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                object_id,
                inventory,
                row.get("title", ""),
                row.get("object_type", ""),
                row.get("date", ""),
                row.get("object_url", ""),
                int(inventory_counts[inventory] > 1),
            ),
        )
        resource_ids = [
            value.strip()
            for value in row.get("pdf_resource_ids", "").split(";")
            if value.strip()
        ]
        resource_urls = [
            value.strip()
            for value in row.get("pdf_urls", "").split(";")
            if value.strip()
        ]
        width = max(len(resource_ids), len(resource_urls), 1)
        for index in range(width):
            resource_id = (
                resource_ids[index] if index < len(resource_ids) else str(index + 1)
            )
            resource_url = (
                resource_urls[index] if index < len(resource_urls) else ""
            )
            connection.execute(
                """
                INSERT INTO md_resources (
                    md_object_id, resource_id, resource_url, resource_role
                ) VALUES (?, ?, ?, 'public_pdf_reference')
                """,
                (object_id, resource_id, resource_url),
            )
        for alias in inventory_aliases(inventory):
            connection.execute(
                """
                INSERT INTO md_aliases (
                    md_object_id, comparison_key, method, basis
                ) VALUES (?, ?, ?, ?)
                """,
                (object_id, alias.comparison_key, alias.method, alias.basis),
            )

    for inventory, count in inventory_counts.items():
        if count <= 1:
            continue
        for row in (item for item in rows if item["inventory"] == inventory):
            connection.execute(
                """
                INSERT INTO exceptions (
                    exception_type, md_object_id, inventory_or_key, detail
                ) VALUES ('duplicate_md_inventory', ?, ?, ?)
                """,
                (
                    row["object_id"],
                    inventory,
                    f"{count} museum-digital objects share this inventory string",
                ),
            )


def local_file_role(family: str, suffix: str) -> str:
    if family == "TIF":
        return "preservation_page_candidate"
    return {
        ".pdf": "access_pdf_candidate",
        ".htm": "access_html_context",
        ".html": "access_html_context",
        ".css": "access_css_context",
    }.get(suffix, "supporting_file_candidate")


def page_sequence(filename: str) -> int | None:
    match = re.search(r"_(\d{1,4})\.(?:tif|tiff)$", filename, re.IGNORECASE)
    return int(match.group(1)) if match else None


def inventory_local(
    connection: sqlite3.Connection,
    source_root: Path,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    local_object_ids: dict[str, int] = {}
    for family in ("PDF", "TIF"):
        family_root = source_root / family
        if not family_root.is_dir():
            raise ReconciliationError(f"required source family unavailable: {family_root}")
        for year_directory in sorted(
            (path for path in family_root.iterdir() if path.is_dir()),
            key=lambda path: path.name.casefold(),
        ):
            for issue_directory in sorted(
                (path for path in year_directory.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            ):
                files: list[tuple[Path, os.stat_result]] = []
                for path in sorted(
                    (item for item in issue_directory.rglob("*") if item.is_file()),
                    key=lambda item: item.as_posix().casefold(),
                ):
                    try:
                        files.append((path, path.stat()))
                    except OSError as error:
                        errors.append(
                            {
                                "stage": "local_inventory",
                                "path": str(path),
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
                if not files:
                    errors.append(
                        {
                            "stage": "local_inventory",
                            "path": str(issue_directory),
                            "error": "empty issue directory",
                        }
                    )
                    continue
                normalized_key, _, _ = normalized_token(issue_directory.name)
                local_object_key = normalized_key.casefold()
                if local_object_key not in local_object_ids:
                    cursor = connection.execute(
                        """
                        INSERT INTO local_objects (
                            local_object_key, representative_inventory
                        ) VALUES (?, ?)
                        """,
                        (local_object_key, issue_directory.name),
                    )
                    local_object_ids[local_object_key] = int(cursor.lastrowid)
                local_object_id = local_object_ids[local_object_key]
                relative_directory = issue_directory.relative_to(source_root).as_posix()
                cursor = connection.execute(
                    """
                    INSERT INTO local_groups (
                        local_object_id, source_family, issue_key_raw, year_directory,
                        source_directory, source_relative_directory, file_count,
                        total_size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_object_id,
                        family,
                        issue_directory.name,
                        year_directory.name,
                        str(issue_directory),
                        relative_directory,
                        len(files),
                        sum(stat.st_size for _, stat in files),
                    ),
                )
                group_id = int(cursor.lastrowid)
                for path, stat in files:
                    suffix = path.suffix.casefold()
                    connection.execute(
                        """
                        INSERT INTO local_files (
                            local_group_id, source_path, source_relative_path,
                            filename, extension, size_bytes, mtime_ns, file_role,
                            page_sequence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            str(path),
                            path.relative_to(source_root).as_posix(),
                            path.name,
                            suffix,
                            stat.st_size,
                            stat.st_mtime_ns,
                            local_file_role(family, suffix),
                            page_sequence(path.name),
                        ),
                    )
                for alias in inventory_aliases(issue_directory.name):
                    connection.execute(
                        """
                        INSERT INTO local_aliases (
                            local_group_id, comparison_key, method, basis
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (group_id, alias.comparison_key, alias.method, alias.basis),
                    )
    return errors


def generate_candidates(connection: sqlite3.Connection) -> None:
    md_index: dict[str, list[tuple[str, Alias]]] = defaultdict(list)
    local_index: dict[str, list[tuple[int, Alias]]] = defaultdict(list)
    for object_id, key, method, basis in connection.execute(
        "SELECT md_object_id, comparison_key, method, basis FROM md_aliases"
    ):
        md_index[key].append((object_id, Alias(key, method, basis)))
    for group_id, key, method, basis in connection.execute(
        "SELECT local_group_id, comparison_key, method, basis FROM local_aliases"
    ):
        local_index[key].append((int(group_id), Alias(key, method, basis)))

    best: dict[tuple[str, int], tuple[int, str, str, str]] = {}
    for key in sorted(set(md_index) & set(local_index)):
        for object_id, md_alias in md_index[key]:
            for group_id, local_alias in local_index[key]:
                method, basis = combine_match(md_alias, local_alias)
                candidate = (MATCH_RANK[method], method, key, basis)
                pair = (object_id, group_id)
                if pair not in best or candidate < best[pair]:
                    best[pair] = candidate

    for (object_id, group_id), (_, method, key, basis) in sorted(best.items()):
        connection.execute(
            """
            INSERT INTO candidate_matches (
                md_object_id, local_group_id, match_method, comparison_key,
                match_basis
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (object_id, group_id, method, key, basis),
        )

    connection.execute(
        """
        UPDATE md_objects
        SET local_candidate_count = (
                SELECT COUNT(*) FROM candidate_matches AS candidate
                WHERE candidate.md_object_id = md_objects.md_object_id
            ),
            local_search_state = CASE
                WHEN EXISTS (
                    SELECT 1 FROM candidate_matches AS candidate
                    WHERE candidate.md_object_id = md_objects.md_object_id
                ) THEN 'primary_candidate_found'
                ELSE 'primary_searched_fallback_not_searched'
            END
        """
    )
    connection.execute(
        """
        UPDATE local_groups
        SET md_candidate_count = (
                SELECT COUNT(*) FROM candidate_matches AS candidate
                WHERE candidate.local_group_id = local_groups.local_group_id
            ),
            local_only_archive_candidate = CASE
                WHEN EXISTS (
                    SELECT 1 FROM candidate_matches AS candidate
                    WHERE candidate.local_group_id = local_groups.local_group_id
                ) THEN 0 ELSE 1 END
        """
    )
    connection.execute(
        """
        UPDATE local_objects
        SET group_count = (
                SELECT COUNT(*) FROM local_groups AS local
                WHERE local.local_object_id = local_objects.local_object_id
            ),
            md_candidate_count = (
                SELECT COUNT(DISTINCT candidate.md_object_id)
                FROM local_groups AS local
                JOIN candidate_matches AS candidate USING (local_group_id)
                WHERE local.local_object_id = local_objects.local_object_id
            ),
            local_only_archive_candidate = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM local_groups AS local
                    JOIN candidate_matches AS candidate USING (local_group_id)
                    WHERE local.local_object_id = local_objects.local_object_id
                ) THEN 0 ELSE 1 END
        """
    )
    connection.execute(
        """
        UPDATE local_groups
        SET local_only_archive_candidate = (
            SELECT local.local_only_archive_candidate
            FROM local_objects AS local
            WHERE local.local_object_id = local_groups.local_object_id
        )
        """
    )

    connection.execute(
        """
        INSERT INTO exceptions (
            exception_type, md_object_id, inventory_or_key, detail
        )
        SELECT
            'md_without_primary_local_candidate',
            md_object_id,
            inventory_raw,
            'MIK PDF/TIF searched; fallback WF-Sender roots not yet searched'
        FROM md_objects
        WHERE local_candidate_count = 0
        """
    )
    connection.execute(
        """
        INSERT INTO exceptions (
            exception_type, local_object_id, inventory_or_key, detail
        )
        SELECT
            'local_only_archive_candidate',
            local_object_id,
            representative_inventory,
            'No museum-digital candidate in current alias pass; retain as archive candidate'
        FROM local_objects
        WHERE local_only_archive_candidate = 1
        """
    )

    for object_id, family, count in connection.execute(
        """
        SELECT candidate.md_object_id, local.source_family, COUNT(*)
        FROM candidate_matches AS candidate
        JOIN local_groups AS local USING (local_group_id)
        GROUP BY candidate.md_object_id, local.source_family
        HAVING COUNT(*) > 1
        """
    ):
        inventory = connection.execute(
            "SELECT inventory_raw FROM md_objects WHERE md_object_id = ?",
            (object_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO exceptions (
                exception_type, md_object_id, inventory_or_key, detail
            ) VALUES ('ambiguous_multiple_local_groups', ?, ?, ?)
            """,
            (
                object_id,
                inventory,
                f"{count} candidate groups in source family {family}",
            ),
        )


def query_dicts(
    connection: sqlite3.Connection,
    query: str,
) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    fields = [column[0] for column in cursor.description]
    return [dict(zip(fields, row, strict=True)) for row in cursor.fetchall()]


def export_artifacts(
    connection: sqlite3.Connection,
    run_dir: Path,
    errors: list[dict[str, str]],
    context: dict[str, Any],
) -> dict[str, Any]:
    object_rows = query_dicts(
        connection,
        """
        SELECT
            md_object_id, inventory_raw, title, object_type, date_text,
            object_url, duplicate_inventory, local_candidate_count,
            local_search_state, review_state
        FROM md_objects
        ORDER BY inventory_raw COLLATE NOCASE, md_object_id
        """,
    )
    group_rows = query_dicts(
        connection,
        """
        SELECT
            local_group_id, local_object_id, source_family, issue_key_raw, year_directory,
            source_directory, source_relative_directory, file_count,
            total_size_bytes, md_candidate_count,
            local_only_archive_candidate, review_state
        FROM local_groups
        ORDER BY source_family, source_relative_directory COLLATE NOCASE
        """,
    )
    local_object_rows = query_dicts(
        connection,
        """
        SELECT
            local_object_id, local_object_key, representative_inventory,
            group_count, md_candidate_count, local_only_archive_candidate,
            review_state
        FROM local_objects
        ORDER BY local_object_key
        """,
    )
    file_rows = query_dicts(
        connection,
        """
        SELECT
            local_file_id, local_group_id, source_path, source_relative_path,
            filename, extension, size_bytes, mtime_ns, file_role, page_sequence
        FROM local_files
        ORDER BY source_relative_path COLLATE NOCASE
        """,
    )
    candidate_rows = query_dicts(
        connection,
        """
        SELECT
            candidate.md_object_id, object.inventory_raw, object.title,
            candidate.local_group_id, local.source_family, local.issue_key_raw,
            local.source_directory, candidate.match_method,
            candidate.comparison_key, candidate.match_basis,
            candidate.review_state
        FROM candidate_matches AS candidate
        JOIN md_objects AS object USING (md_object_id)
        JOIN local_groups AS local USING (local_group_id)
        ORDER BY object.inventory_raw COLLATE NOCASE, candidate.md_object_id,
                 local.source_family, local.source_relative_directory COLLATE NOCASE
        """,
    )
    exception_rows = query_dicts(
        connection,
        """
        SELECT
            exception_id, exception_type, md_object_id, local_object_id,
            local_group_id,
            inventory_or_key, detail, review_state
        FROM exceptions
        ORDER BY exception_type, inventory_or_key COLLATE NOCASE, exception_id
        """,
    )

    write_csv(run_dir / "objects.csv", object_rows, list(object_rows[0]))
    write_csv(
        run_dir / "local-objects.csv",
        local_object_rows,
        list(local_object_rows[0]),
    )
    write_csv(run_dir / "local-groups.csv", group_rows, list(group_rows[0]))
    write_csv(run_dir / "local-files.csv", file_rows, list(file_rows[0]))
    write_csv(
        run_dir / "candidate-matches.csv",
        candidate_rows,
        list(candidate_rows[0]),
    )
    write_csv(run_dir / "exceptions.csv", exception_rows, list(exception_rows[0]))
    write_csv(run_dir / "errors.csv", errors, ["stage", "path", "error"])

    match_counts = Counter(row["match_method"] for row in candidate_rows)
    exception_counts = Counter(row["exception_type"] for row in exception_rows)
    family_group_counts = Counter(row["source_family"] for row in group_rows)
    family_by_group = {
        row["local_group_id"]: row["source_family"] for row in group_rows
    }
    family_file_counts = Counter()
    for row in file_rows:
        family_file_counts[family_by_group[row["local_group_id"]]] += 1
    summary = {
        **context,
        "md_objects": len(object_rows),
        "md_duplicate_inventory_rows": sum(
            int(row["duplicate_inventory"]) for row in object_rows
        ),
        "local_groups": len(group_rows),
        "local_groups_by_family": dict(sorted(family_group_counts.items())),
        "local_objects": len(local_object_rows),
        "local_files": len(file_rows),
        "local_files_by_family": dict(sorted(family_file_counts.items())),
        "candidate_matches": len(candidate_rows),
        "candidate_matches_by_method": dict(sorted(match_counts.items())),
        "md_objects_with_primary_candidate": sum(
            int(row["local_candidate_count"]) > 0 for row in object_rows
        ),
        "md_objects_without_primary_candidate": sum(
            int(row["local_candidate_count"]) == 0 for row in object_rows
        ),
        "local_objects_without_md_match_archive_candidates": sum(
            int(row["local_only_archive_candidate"]) for row in local_object_rows
        ),
        "exceptions": len(exception_rows),
        "exceptions_by_type": dict(sorted(exception_counts.items())),
        "errors": len(errors),
        "decisions": 0,
        "decision_boundary": (
            "All relationships and local-only rows are candidates requiring "
            "review. No accession, download, copy, hash, staging, source move, "
            "or deletion was performed."
        ),
    }
    atomic_write(
        run_dir / "summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    readme = f"""# WF-Sender collection 552 reconciliation

Run: `{context['run_id']}`

This is a read-only first-pass reconciliation of the immutable
museum-digital collection 552 capture against `MIK-Daten/PDF` and
`MIK-Daten/TIF`.

## Decision boundary

- Museum-digital object ID is the object-row identity; inventory is not assumed
  unique.
- Exact, normalized, and documented-alias relationships are candidates only.
- Every local group without a museum-digital candidate remains a
  `local_only_archive_candidate` for curator review.
- A museum-digital row without a primary-source candidate is
  `primary_searched_fallback_not_searched`, not `no_match_found`.
- No source was hashed, copied, downloaded, renamed, staged, or modified.
- No curator decision, accession, source move, drain, or deletion occurred.

## Outputs

- `reconciliation.sqlite`: logged work database and empty decision table.
- `objects.csv`: one row per museum-digital object ID.
- `local-objects.csv`: one row per normalized local issue object.
- `local-groups.csv`: one row per MIK issue directory.
- `local-files.csv`: source provenance and candidate file roles.
- `candidate-matches.csv`: candidate relationships and auditable match basis.
- `exceptions.csv`: duplicates, ambiguous candidates, local-only archive
  candidates, and MD rows still needing fallback search.
- `errors.csv`: source inventory errors.
- `summary.json`: verified run counts and protection boundary.
"""
    atomic_write(run_dir / "README.md", readme.encode("utf-8"))
    return summary


def build_run(
    capture_run: Path,
    source_root: Path,
    run_dir: Path,
    run_id: str,
    source_mount: str,
) -> dict[str, Any]:
    if run_dir.exists():
        raise ReconciliationError(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    database = run_dir / "reconciliation.sqlite"
    rows = load_md_rows(capture_run)
    created_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    connection = sqlite3.connect(database)
    try:
        create_schema(connection)
        connection.execute(
            """
            INSERT INTO runs (
                run_id, format, created_utc, capture_run, source_root,
                source_mount, source_policy
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                FORMAT,
                created_utc,
                str(capture_run),
                str(source_root),
                source_mount,
                "read-only; no hash; no copy; no download; no mutation",
            ),
        )
        import_md(connection, rows)
        errors = inventory_local(connection, source_root)
        generate_candidates(connection)
        connection.commit()
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ReconciliationError(f"SQLite integrity check failed: {result}")
        summary = export_artifacts(
            connection,
            run_dir,
            errors,
            {
                "format": FORMAT,
                "run_id": run_id,
                "created_utc": created_utc,
                "capture_run": str(capture_run),
                "source_root": str(source_root),
                "source_mount": source_mount,
                "source_policy": (
                    "read-only; no hash; no copy; no download; no mutation"
                ),
                "database_integrity": result,
            },
        )
        connection.commit()
        return summary
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--capture-run", type=Path, default=DEFAULT_CAPTURE_RUN)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--run-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    work_root = args.work_root.expanduser().resolve()
    capture_run = args.capture_run.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    ensure_paths(capture_run, source_root, work_root)
    mount_state = ensure_read_only_source(source_root)
    run_id = args.run_id or (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-collection-552-primary-match"
    )
    run_dir = (
        work_root
        / "work"
        / "reconciliation"
        / "collection-552"
        / "runs"
        / run_id
    )
    summary = build_run(
        capture_run,
        source_root,
        run_dir,
        run_id,
        mount_state,
    )
    print(json.dumps({"run_dir": str(run_dir), **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
