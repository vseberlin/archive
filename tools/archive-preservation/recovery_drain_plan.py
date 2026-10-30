#!/usr/bin/env python3
"""Build a non-destructive exact-byte drain plan for reviewed recovery masters."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class DrainPlanError(RuntimeError):
    """Raised when the retained set or scan boundary is unsafe."""


@dataclass(frozen=True)
class SelectedMaster:
    inventory: str
    entity_id: str
    evidence_id: str
    digest: str
    size_bytes: int
    selected_source_path: str
    preservation_role: str
    preservation_digest: str
    preservation_size_bytes: int
    preservation_relative_path: str
    primary_preservation_path: str
    independent_preservation_path: str


@dataclass(frozen=True)
class Occurrence:
    root: str
    path: str
    size_bytes: int
    digest: str


CSV_FIELDS = (
    "inventory",
    "entity_id",
    "selected_evidence_id",
    "selected_sha256",
    "selected_size_bytes",
    "selected_source_path",
    "preservation_role",
    "preservation_sha256",
    "preservation_size_bytes",
    "primary_preservation_path",
    "independent_preservation_path",
    "occurrence_root",
    "occurrence_path",
    "occurrence_size_bytes",
    "occurrence_sha256",
    "occurrence_is_selected_source",
    "occurrence_class",
    "proposed_disposition",
    "deletion_authorized",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file_stable(path: Path, expected_size: int) -> str:
    before = path.stat()
    if not path.is_file() or before.st_size != expected_size:
        raise DrainPlanError(f"file size changed before hashing: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise DrainPlanError(f"file changed while hashing: {path}")
    return f"sha256:{digest.hexdigest()}"


def _latest_selected_reviews(catalog: sqlite3.Connection, unit_id: str) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    rows = catalog.execute(
        """
        SELECT at_utc, log_id, after_json
        FROM logbook
        WHERE unit_id = ? AND action = 'review_source_candidate'
        ORDER BY at_utc DESC, log_id DESC
        """,
        (unit_id,),
    )
    for row in rows:
        try:
            value = json.loads(row["after_json"] or "{}")
        except json.JSONDecodeError as error:
            raise DrainPlanError(f"invalid source-review JSON in {row['log_id']}") from error
        entity_id = str(value.get("entity_id") or "")
        if (
            entity_id
            and value.get("state") == "selected_master"
            and entity_id not in selected
        ):
            selected[entity_id] = value
    return selected


def load_selected_masters(
    catalog_path: Path,
    reconciliation_path: Path,
    primary_root: Path,
    independent_root: Path,
    unit_id: str,
) -> list[SelectedMaster]:
    catalog = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    reconciliation = sqlite3.connect(
        f"file:{reconciliation_path}?mode=ro", uri=True
    )
    catalog.row_factory = sqlite3.Row
    reconciliation.row_factory = sqlite3.Row
    try:
        reviews = _latest_selected_reviews(catalog, unit_id)
        active = catalog.execute(
            """
            SELECT
                ao.archival_label AS inventory,
                ef.entity_id,
                ef.role,
                fo.relative_path,
                fo.content_digest,
                cb.size_bytes
            FROM entity_files ef
            JOIN archive_objects ao ON ao.entity_id = ef.entity_id
            JOIN file_occurrences fo ON fo.occurrence_id = ef.occurrence_id
            JOIN content_blobs cb ON cb.content_digest = fo.content_digest
            WHERE
                ef.status = 'active'
                AND fo.status = 'verified'
                AND fo.unit_id = ?
                AND ef.role IN (
                    'preservation_master',
                    'preservation_surrogate'
                )
            ORDER BY ao.archival_label, ef.entity_id
            """,
            (unit_id,),
        ).fetchall()
        records: list[SelectedMaster] = []
        for row in active:
            entity_id = row["entity_id"]
            review = reviews.get(entity_id)
            if not review:
                raise DrainPlanError(
                    f"active preservation source has no selected review: {entity_id}"
                )
            evidence_id = str(review.get("evidence_id") or "")
            evidence = reconciliation.execute(
                """
                SELECT ve.source_locator, ve.content_digest, ve.status
                FROM visual_evidence ve
                JOIN reconciliation_records rr ON rr.record_id = ve.record_id
                WHERE ve.evidence_id = ? AND rr.entity_id = ?
                """,
                (evidence_id, entity_id),
            ).fetchone()
            if not evidence or evidence["status"] != "available":
                raise DrainPlanError(
                    f"selected visual evidence is unavailable for {entity_id}"
                )
            selected_digest = str(review.get("content_digest") or "")
            if evidence["content_digest"] != selected_digest:
                raise DrainPlanError(
                    f"selected review and visual digests differ for {entity_id}"
                )
            registration_action = (
                "register_preservation_master"
                if row["role"] == "preservation_master"
                else "register_preservation_surrogate"
            )
            registrations = catalog.execute(
                    """
                    SELECT log_id, after_json
                    FROM logbook
                    WHERE entity_id = ? AND action = ?
                    ORDER BY at_utc DESC, log_id DESC
                    """,
                    (entity_id, registration_action),
            )
            linked = False
            for registration in registrations:
                try:
                    value = json.loads(registration["after_json"] or "{}")
                except json.JSONDecodeError as error:
                    raise DrainPlanError(
                        f"invalid preservation registration JSON in "
                        f"{registration['log_id']}"
                    ) from error
                if value.get("content_digest") == row["content_digest"]:
                    linked = value.get("source_digest") == selected_digest
                    break
            if not linked:
                raise DrainPlanError(
                    f"preservation representation lacks selected-source linkage "
                    f"for {entity_id}"
                )
            if row["role"] == "preservation_surrogate":
                access = catalog.execute(
                    """
                    SELECT fo.content_digest
                    FROM entity_files ef
                    JOIN file_occurrences fo
                      ON fo.occurrence_id=ef.occurrence_id
                    WHERE ef.entity_id=?
                      AND ef.role='access_derivative'
                      AND ef.status='active'
                      AND fo.status='verified'
                    """,
                    (entity_id,),
                ).fetchall()
                if (
                    len(access) != 1
                    or access[0]["content_digest"] != selected_digest
                ):
                    raise DrainPlanError(
                        f"surrogate access derivative is not the selected source "
                        f"for {entity_id}"
                    )
            selected_source_path = evidence["source_locator"]
            try:
                selected_size = Path(selected_source_path).stat().st_size
            except OSError as error:
                raise DrainPlanError(
                    f"cannot stat selected source for {entity_id}: "
                    f"{selected_source_path}"
                ) from error
            relative = row["relative_path"]
            records.append(
                SelectedMaster(
                    inventory=row["inventory"],
                    entity_id=entity_id,
                    evidence_id=evidence_id,
                    digest=selected_digest,
                    size_bytes=selected_size,
                    selected_source_path=selected_source_path,
                    preservation_role=row["role"],
                    preservation_digest=row["content_digest"],
                    preservation_size_bytes=int(row["size_bytes"]),
                    preservation_relative_path=relative,
                    primary_preservation_path=str(primary_root / relative),
                    independent_preservation_path=str(independent_root / relative),
                )
            )
        if len({record.entity_id for record in records}) != len(records):
            raise DrainPlanError("multiple active preservation sources for one entity")
        if len({record.digest for record in records}) != len(records):
            raise DrainPlanError("selected retained digests are not unique")
        return records
    finally:
        catalog.close()
        reconciliation.close()


def verify_retained_copies(records: Iterable[SelectedMaster]) -> None:
    for record in records:
        for label, path_text, size_bytes, digest in (
            (
                "selected source",
                record.selected_source_path,
                record.size_bytes,
                record.digest,
            ),
            (
                "primary preservation",
                record.primary_preservation_path,
                record.preservation_size_bytes,
                record.preservation_digest,
            ),
            (
                "independent preservation",
                record.independent_preservation_path,
                record.preservation_size_bytes,
                record.preservation_digest,
            ),
        ):
            path = Path(path_text)
            actual = sha256_file_stable(path, size_bytes)
            if actual != digest:
                raise DrainPlanError(
                    f"{label} digest mismatch for {record.inventory}: {path}"
                )


def _mount_for(path: Path) -> tuple[str, frozenset[str]]:
    resolved = path.resolve()
    best: tuple[int, str, frozenset[str]] | None = None
    with Path("/proc/self/mountinfo").open("r", encoding="utf-8") as handle:
        for line in handle:
            left, _, right = line.rstrip("\n").partition(" - ")
            fields = left.split()
            if len(fields) < 6 or not right:
                continue
            mountpoint = Path(
                fields[4].replace("\\040", " ").replace("\\134", "\\")
            )
            try:
                resolved.relative_to(mountpoint)
            except ValueError:
                continue
            options = frozenset(fields[5].split(","))
            candidate = (len(mountpoint.parts), str(mountpoint), options)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        raise DrainPlanError(f"cannot resolve mount for {path}")
    return best[1], best[2]


def validate_scan_roots(
    roots: Iterable[Path], required_read_only: Iterable[Path]
) -> list[Path]:
    resolved = [root.expanduser().resolve() for root in roots]
    if not resolved:
        raise DrainPlanError("at least one scan root is required")
    for root in resolved:
        if not root.is_dir():
            raise DrainPlanError(f"scan root is not a directory: {root}")
    for index, root in enumerate(resolved):
        for other in resolved[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise DrainPlanError(f"scan roots overlap: {root} and {other}")
    resolved_set = set(resolved)
    for root in required_read_only:
        candidate = root.expanduser().resolve()
        if candidate not in resolved_set:
            raise DrainPlanError(
                f"required read-only root is not a scan root: {candidate}"
            )
        mountpoint, options = _mount_for(candidate)
        if "ro" not in options:
            raise DrainPlanError(
                f"required source is not mounted read-only: {candidate} "
                f"(mount {mountpoint})"
            )
    return resolved


def validate_output_dir(output_dir: Path, roots: Iterable[Path]) -> Path:
    resolved = output_dir.expanduser().resolve()
    for root in roots:
        if resolved == root or root in resolved.parents:
            raise DrainPlanError(
                f"report output cannot be inside a scanned evidence root: {resolved}"
            )
    return resolved


def scan_occurrences(
    records: Iterable[SelectedMaster], roots: Iterable[Path]
) -> tuple[list[Occurrence], list[dict], dict]:
    records = list(records)
    digests_by_size: dict[int, set[str]] = defaultdict(set)
    for record in records:
        digests_by_size[record.size_bytes].add(record.digest)
    occurrences: list[Occurrence] = []
    errors: list[dict] = []
    counters = {
        "files_seen": 0,
        "size_candidates": 0,
        "files_hashed": 0,
        "exact_occurrences": 0,
    }

    def walk_error(error: OSError) -> None:
        errors.append(
            {
                "stage": "walk",
                "path": str(error.filename or ""),
                "error": str(error),
            }
        )

    for root in roots:
        for directory, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=walk_error
        ):
            dirnames.sort()
            filenames.sort()
            directory_path = Path(directory)
            for filename in filenames:
                path = directory_path / filename
                counters["files_seen"] += 1
                try:
                    stat = path.stat(follow_symlinks=False)
                    if not path.is_file() or path.is_symlink():
                        continue
                    expected = digests_by_size.get(stat.st_size)
                    if not expected:
                        continue
                    counters["size_candidates"] += 1
                    digest = sha256_file_stable(path, stat.st_size)
                    counters["files_hashed"] += 1
                    if digest in expected:
                        occurrences.append(
                            Occurrence(
                                root=str(root),
                                path=str(path),
                                size_bytes=stat.st_size,
                                digest=digest,
                            )
                        )
                except (OSError, DrainPlanError) as error:
                    errors.append(
                        {"stage": "file", "path": str(path), "error": str(error)}
                    )
    occurrences.sort(key=lambda item: (item.digest, item.root, item.path))
    counters["exact_occurrences"] = len(occurrences)
    return occurrences, errors, counters


def build_rows(
    records: Iterable[SelectedMaster], occurrences: Iterable[Occurrence]
) -> list[dict]:
    by_digest: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_digest[occurrence.digest].append(occurrence)
    rows: list[dict] = []
    for record in records:
        matches = by_digest.get(record.digest, [])
        if not matches:
            matches = [Occurrence("", "", 0, "")]
        for occurrence in matches:
            is_source = (
                bool(occurrence.path)
                and Path(occurrence.path) == Path(record.selected_source_path)
            )
            rows.append(
                {
                    "inventory": record.inventory,
                    "entity_id": record.entity_id,
                    "selected_evidence_id": record.evidence_id,
                    "selected_sha256": record.digest,
                    "selected_size_bytes": record.size_bytes,
                    "selected_source_path": record.selected_source_path,
                    "preservation_role": record.preservation_role,
                    "preservation_sha256": record.preservation_digest,
                    "preservation_size_bytes": record.preservation_size_bytes,
                    "primary_preservation_path": record.primary_preservation_path,
                    "independent_preservation_path": record.independent_preservation_path,
                    "occurrence_root": occurrence.root,
                    "occurrence_path": occurrence.path,
                    "occurrence_size_bytes": occurrence.size_bytes or "",
                    "occurrence_sha256": occurrence.digest,
                    "occurrence_is_selected_source": str(is_source).lower(),
                    "occurrence_class": (
                        "selected_provenance_source"
                        if is_source
                        else (
                            "exact_duplicate_occurrence"
                            if occurrence.path
                            else "no_scanned_occurrence"
                        )
                    ),
                    "proposed_disposition": (
                        "retain selected provenance source"
                        if is_source
                        else (
                            "review exact duplicate; no action authorized"
                            if occurrence.path
                            else "investigate missing scanned source"
                        )
                    ),
                    "deletion_authorized": "false",
                }
            )
    return rows


def write_report(
    output_dir: Path,
    records: list[SelectedMaster],
    occurrences: list[Occurrence],
    errors: list[dict],
    counters: dict,
    roots: list[Path],
    started_at: str,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    rows = build_rows(records, occurrences)
    with (output_dir / "drain-plan.csv").open(
        "x", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with (output_dir / "errors.csv").open(
        "x", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=("stage", "path", "error"))
        writer.writeheader()
        writer.writerows(errors)
    found_digests = {occurrence.digest for occurrence in occurrences}
    summary = {
        "schema": "iss-recovery-drain-plan-v1",
        "status": "complete" if not errors else "incomplete_scan_errors",
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "scan_roots": [str(root) for root in roots],
        "selected_records": len(records),
        "selected_unique_digests": len({record.digest for record in records}),
        "selected_digests_found": len(found_digests),
        "selected_digests_not_found": sorted(
            {record.digest for record in records} - found_digests
        ),
        "manifest_rows": len(rows),
        "scan_errors": len(errors),
        "deletion_authorized": False,
        **counters,
    }
    with (output_dir / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with (output_dir / "selected-masters.json").open(
        "x", encoding="utf-8"
    ) as handle:
        json.dump(
            [asdict(record) for record in records],
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--reconciliation", required=True, type=Path)
    parser.add_argument("--primary-root", required=True, type=Path)
    parser.add_argument("--independent-root", required=True, type=Path)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--scan-root", action="append", required=True, type=Path)
    parser.add_argument("--require-read-only-root", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--skip-retained-verification",
        action="store_true",
        help="Skip source/primary/independent fixity checks (not recommended).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    started_at = utc_now()
    roots = validate_scan_roots(args.scan_root, args.require_read_only_root)
    output_dir = validate_output_dir(args.output_dir, roots)
    records = load_selected_masters(
        args.catalog,
        args.reconciliation,
        args.primary_root.resolve(),
        args.independent_root.resolve(),
        args.unit_id,
    )
    if not records:
        raise DrainPlanError("no active reviewed preservation masters found")
    if not args.skip_retained_verification:
        verify_retained_copies(records)
    occurrences, errors, counters = scan_occurrences(records, roots)
    summary = write_report(
        output_dir,
        records,
        occurrences,
        errors,
        counters,
        roots,
        started_at,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DrainPlanError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
