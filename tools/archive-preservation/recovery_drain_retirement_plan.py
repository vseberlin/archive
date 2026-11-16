#!/usr/bin/env python3
"""Create an exact, non-executing retirement path plan from a drain census."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class RetirementPlanError(RuntimeError):
    """Raised when retirement evidence is incomplete or unsafe."""


PLAN_FIELDS = (
    "sequence",
    "inventory",
    "entity_id",
    "candidate_root",
    "candidate_path",
    "candidate_size_bytes",
    "candidate_sha256",
    "candidate_class",
    "retirement_basis",
    "authoritative_role",
    "authoritative_primary_path",
    "authoritative_independent_path",
    "authoritative_size_bytes",
    "authoritative_sha256",
    "exact_candidate_bytes_retained_in_package",
    "verified_metadata_transformation",
    "planned_action",
    "deletion_authorized",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file_stable(path: Path, expected_size: int) -> str:
    before = path.stat(follow_symlinks=False)
    if not path.is_file() or path.is_symlink() or before.st_size != expected_size:
        raise RetirementPlanError(f"unsafe file or size mismatch: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
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
        raise RetirementPlanError(f"file changed while hashing: {path}")
    return f"sha256:{digest.hexdigest()}"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict | list:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_census(census_dir: Path) -> tuple[list[dict], dict, list[dict]]:
    summary_path = census_dir / "summary.json"
    plan_path = census_dir / "drain-plan.csv"
    selected_path = census_dir / "selected-masters.json"
    summary = _load_json(summary_path)
    selected = _load_json(selected_path)
    if not isinstance(summary, dict) or not isinstance(selected, list):
        raise RetirementPlanError("invalid census JSON")
    if summary.get("status") != "complete" or summary.get("scan_errors") != 0:
        raise RetirementPlanError("census is incomplete or contains scan errors")
    if summary.get("deletion_authorized") is not False:
        raise RetirementPlanError("input census unexpectedly authorizes deletion")
    with plan_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != summary.get("manifest_rows"):
        raise RetirementPlanError("census row count differs from summary")
    if len(selected) != summary.get("selected_records"):
        raise RetirementPlanError("selected-master count differs from summary")
    if any(not row.get("occurrence_path") for row in rows):
        raise RetirementPlanError("census contains an empty occurrence path")
    paths = [row["occurrence_path"] for row in rows]
    if len(paths) != len(set(paths)):
        raise RetirementPlanError("census contains duplicate occurrence paths")
    return rows, summary, selected


def validate_candidate_path(path_text: str, root_text: str, roots: set[Path]) -> Path:
    root = Path(root_text).resolve()
    if root not in roots:
        raise RetirementPlanError(f"candidate root is not approved: {root}")
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise RetirementPlanError(
            f"candidate is missing or outside its approved root: {path}"
        ) from error
    if resolved != path.absolute():
        raise RetirementPlanError(f"candidate path traverses a symlink: {path}")
    return resolved


def load_active_representations(
    catalog_path: Path, unit_id: str
) -> tuple[dict[str, dict[str, dict]], dict]:
    catalog = sqlite3.connect(f"file:{catalog_path}?mode=ro", uri=True)
    catalog.row_factory = sqlite3.Row
    try:
        state = catalog.execute(
            """
            SELECT catalog_revision, published_revision, dirty_since
            FROM catalog_state WHERE singleton=1
            """
        ).fetchone()
        if (
            not state
            or state["catalog_revision"] != state["published_revision"]
            or state["dirty_since"] is not None
        ):
            raise RetirementPlanError("catalog is not clean and published")
        generation = catalog.execute(
            """
            SELECT generation_id, catalog_revision, manifest_digest
            FROM filesystem_generations
            WHERE status='current'
            """
        ).fetchall()
        if (
            len(generation) != 1
            or generation[0]["catalog_revision"] != state["catalog_revision"]
        ):
            raise RetirementPlanError("current filesystem generation is invalid")
        rows = catalog.execute(
            """
            SELECT
                ef.entity_id,
                ef.role,
                fo.relative_path,
                fo.content_digest,
                cb.size_bytes
            FROM entity_files ef
            JOIN file_occurrences fo ON fo.occurrence_id=ef.occurrence_id
            JOIN content_blobs cb ON cb.content_digest=fo.content_digest
            WHERE ef.status='active'
              AND fo.status='verified'
              AND fo.unit_id=?
              AND ef.role IN (
                'preservation_master',
                'preservation_surrogate',
                'access_derivative'
              )
            """,
            (unit_id,),
        ).fetchall()
        by_entity: dict[str, dict[str, dict]] = {}
        for row in rows:
            roles = by_entity.setdefault(row["entity_id"], {})
            if row["role"] in roles:
                raise RetirementPlanError(
                    f"multiple active {row['role']} files for {row['entity_id']}"
                )
            roles[row["role"]] = dict(row)
        generation_row = dict(generation[0])
        generation_row["catalog_revision"] = state["catalog_revision"]
        return by_entity, generation_row
    finally:
        catalog.close()


def load_tiff_accessions(report_dir: Path) -> list[dict]:
    reports: list[dict] = []
    for path in sorted(report_dir.glob("TIFF-*.json")):
        value = _load_json(path)
        if isinstance(value, dict) and value.get("result") == "ok":
            value["_report_path"] = str(path)
            reports.append(value)
    return reports


def find_verified_tiff_accession(
    reports: Iterable[dict], entity_id: str, source_digest: str, result_digest: str
) -> dict:
    matches = [
        report
        for report in reports
        if report.get("entity_id") == entity_id
        and report.get("source_digest") == source_digest
        and report.get("content_digest") == result_digest
        and report.get("decoded_pixel_digest")
    ]
    if not matches:
        raise RetirementPlanError(
            f"no verified TIFF transformation report for {entity_id}"
        )
    return matches[-1]


def verify_authoritative_file(
    root: Path, relative_path: str, size_bytes: int, digest: str
) -> Path:
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise RetirementPlanError(
            f"authoritative path escapes preservation root: {path}"
        ) from error
    if sha256_file_stable(path, size_bytes) != digest:
        raise RetirementPlanError(f"authoritative fixity mismatch: {path}")
    return path


def build_plan(
    census_rows: list[dict],
    selected: list[dict],
    representations: dict[str, dict[str, dict]],
    tiff_reports: list[dict],
    approved_roots: set[Path],
    primary_root: Path,
    independent_root: Path,
) -> tuple[list[dict], dict]:
    selected_by_entity = {row["entity_id"]: row for row in selected}
    if len(selected_by_entity) != len(selected):
        raise RetirementPlanError("selected-master entities are not unique")

    authoritative: dict[str, dict] = {}
    transformation_reports: dict[str, str] = {}
    for entity_id, record in selected_by_entity.items():
        roles = representations.get(entity_id, {})
        source_role = record["preservation_role"]
        source_representation = roles.get(source_role)
        derivative = roles.get("access_derivative")
        if not source_representation or not derivative:
            raise RetirementPlanError(
                f"active master/surrogate and derivative required for {entity_id}"
            )
        for representation in (source_representation, derivative):
            verify_authoritative_file(
                primary_root,
                representation["relative_path"],
                int(representation["size_bytes"]),
                representation["content_digest"],
            )
            verify_authoritative_file(
                independent_root,
                representation["relative_path"],
                int(representation["size_bytes"]),
                representation["content_digest"],
            )
        if source_role == "preservation_surrogate":
            if derivative["content_digest"] != record["digest"]:
                raise RetirementPlanError(
                    f"selected JPEG is not the access derivative for {entity_id}"
                )
            authoritative[entity_id] = derivative
        elif source_role == "preservation_master":
            if source_representation["content_digest"] == record["digest"]:
                raise RetirementPlanError(
                    f"TIFF retirement expected a transformed master for {entity_id}"
                )
            report = find_verified_tiff_accession(
                tiff_reports,
                entity_id,
                record["digest"],
                source_representation["content_digest"],
            )
            transformation_reports[entity_id] = report["_report_path"]
            authoritative[entity_id] = source_representation
        else:
            raise RetirementPlanError(f"unsupported preservation role: {source_role}")

    rows: list[dict] = []
    verified_candidate_bytes = 0
    for sequence, census_row in enumerate(
        sorted(census_rows, key=lambda row: row["occurrence_path"]), start=1
    ):
        entity_id = census_row["entity_id"]
        record = selected_by_entity.get(entity_id)
        representation = authoritative.get(entity_id)
        if not record or not representation:
            raise RetirementPlanError(f"unknown census entity: {entity_id}")
        candidate = validate_candidate_path(
            census_row["occurrence_path"],
            census_row["occurrence_root"],
            approved_roots,
        )
        size_bytes = int(census_row["occurrence_size_bytes"])
        expected_digest = census_row["occurrence_sha256"]
        if expected_digest != record["digest"]:
            raise RetirementPlanError(
                f"candidate/source digest mismatch for {candidate}"
            )
        if sha256_file_stable(candidate, size_bytes) != expected_digest:
            raise RetirementPlanError(f"candidate fixity mismatch: {candidate}")
        verified_candidate_bytes += size_bytes
        transformed = record["preservation_role"] == "preservation_master"
        rows.append(
            {
                "sequence": sequence,
                "inventory": census_row["inventory"],
                "entity_id": entity_id,
                "candidate_root": census_row["occurrence_root"],
                "candidate_path": str(candidate),
                "candidate_size_bytes": size_bytes,
                "candidate_sha256": expected_digest,
                "candidate_class": census_row["occurrence_class"],
                "retirement_basis": (
                    "verified_metadata_transformation_to_preservation_master"
                    if transformed
                    else "exact_bytes_retained_as_access_derivative"
                ),
                "authoritative_role": representation["role"],
                "authoritative_primary_path": str(
                    primary_root / representation["relative_path"]
                ),
                "authoritative_independent_path": str(
                    independent_root / representation["relative_path"]
                ),
                "authoritative_size_bytes": representation["size_bytes"],
                "authoritative_sha256": representation["content_digest"],
                "exact_candidate_bytes_retained_in_package": str(
                    not transformed
                ).lower(),
                "verified_metadata_transformation": str(transformed).lower(),
                "planned_action": "unlink_exact_file_only",
                "deletion_authorized": "false",
            }
        )
    details = {
        "verified_candidate_bytes": verified_candidate_bytes,
        "transformation_reports": transformation_reports,
    }
    return rows, details


def write_plan(
    output_dir: Path,
    rows: list[dict],
    details: dict,
    census_dir: Path,
    generation: dict,
    approved_roots: set[Path],
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    plan_path = output_dir / "retirement-path-plan.csv"
    with plan_path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    root_counts = Counter(row["candidate_root"] for row in rows)
    basis_counts = Counter(row["retirement_basis"] for row in rows)
    summary = {
        "schema": "iss-recovery-retirement-path-plan-v1",
        "status": "ready_for_explicit_authorization",
        "created_at_utc": utc_now(),
        "source_census": str(census_dir),
        "source_census_plan_sha256": digest_file(census_dir / "drain-plan.csv"),
        "catalog_revision": generation["catalog_revision"],
        "filesystem_generation": generation["generation_id"],
        "filesystem_manifest_digest": generation["manifest_digest"],
        "candidate_paths": len(rows),
        "candidate_bytes": details["verified_candidate_bytes"],
        "candidate_roots": dict(sorted(root_counts.items())),
        "retirement_bases": dict(sorted(basis_counts.items())),
        "selected_entities": len({row["entity_id"] for row in rows}),
        "approved_roots": sorted(str(root) for root in approved_roots),
        "authoritative_primary_and_independent_fixity_verified": True,
        "candidate_fixity_verified_during_planning": True,
        "tiff_transformation_reports": details["transformation_reports"],
        "planned_action": "unlink_exact_file_only",
        "directory_deletion_planned": False,
        "usb_requires_read_write_remount_for_execution": any(
            str(root).startswith("/media/") for root in approved_roots
        ),
        "pre_unlink_sha256_revalidation_required": True,
        "deletion_authorized": False,
        "rollback": (
            "No byte-level rollback is promised. The preservation package and "
            "its independent copy are the retained authority."
        ),
    }
    summary_path = output_dir / "summary.json"
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    notice_path = output_dir / "PLAN_ONLY.md"
    notice_path.write_text(
        "# Plan only\n\n"
        "This directory contains an exact retirement path plan. It does not "
        "authorize or execute deletion.\n\n"
        "Execution must revalidate every candidate SHA-256 immediately before "
        "unlinking it, must not delete directories, and must emit a separate "
        "execution report.\n",
        encoding="utf-8",
    )
    checksums_path = output_dir / "checksums.sha256"
    checksum_lines = []
    for path in (plan_path, summary_path, notice_path):
        checksum_lines.append(f"{digest_file(path)}  {path.name}")
    checksums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census-dir", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--accession-report-dir", required=True, type=Path)
    parser.add_argument("--primary-root", required=True, type=Path)
    parser.add_argument("--independent-root", required=True, type=Path)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--approved-root", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    census_dir = args.census_dir.resolve()
    output_dir = args.output_dir.resolve()
    approved_roots = {root.resolve() for root in args.approved_root}
    if output_dir.exists():
        raise RetirementPlanError(f"output directory already exists: {output_dir}")
    if any(output_dir == root or root in output_dir.parents for root in approved_roots):
        raise RetirementPlanError("output directory is inside a retirement root")
    census_rows, _, selected = load_census(census_dir)
    representations, generation = load_active_representations(
        args.catalog.resolve(), args.unit_id
    )
    reports = load_tiff_accessions(args.accession_report_dir.resolve())
    rows, details = build_plan(
        census_rows,
        selected,
        representations,
        reports,
        approved_roots,
        args.primary_root.resolve(),
        args.independent_root.resolve(),
    )
    summary = write_plan(
        output_dir,
        rows,
        details,
        census_dir,
        generation,
        approved_roots,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RetirementPlanError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
