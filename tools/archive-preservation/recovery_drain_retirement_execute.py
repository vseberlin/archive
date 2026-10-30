#!/usr/bin/env python3
"""Execute one explicitly approved, digest-bound retirement path plan."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


class RetirementExecutionError(RuntimeError):
    """Raised when an approved retirement cannot execute safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_stable(path: Path, expected_size: int) -> str:
    before = path.stat(follow_symlinks=False)
    if not path.is_file() or path.is_symlink() or before.st_size != expected_size:
        raise RetirementExecutionError(f"unsafe file or size mismatch: {path}")
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
        raise RetirementExecutionError(f"file changed while hashing: {path}")
    return f"sha256:{digest.hexdigest()}"


def verify_plan_checksums(plan_dir: Path) -> None:
    checksum_path = plan_dir / "checksums.sha256"
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        path = plan_dir / name
        if digest_file(path) != expected:
            raise RetirementExecutionError(f"plan checksum mismatch: {name}")


def load_plan(plan_dir: Path, approved_digest: str) -> tuple[list[dict], dict]:
    verify_plan_checksums(plan_dir)
    plan_path = plan_dir / "retirement-path-plan.csv"
    actual_digest = digest_file(plan_path)
    if actual_digest != approved_digest:
        raise RetirementExecutionError(
            f"approved plan digest mismatch: {actual_digest}"
        )
    with plan_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    with (plan_dir / "summary.json").open(encoding="utf-8") as handle:
        summary = json.load(handle)
    if summary.get("status") != "ready_for_explicit_authorization":
        raise RetirementExecutionError("plan is not ready for authorization")
    if summary.get("source_census_plan_sha256") is None:
        raise RetirementExecutionError("plan lacks source census binding")
    if len(rows) != summary.get("candidate_paths"):
        raise RetirementExecutionError("plan row count differs from summary")
    paths = [row["candidate_path"] for row in rows]
    if len(paths) != len(set(paths)):
        raise RetirementExecutionError("plan contains duplicate candidate paths")
    if any(
        row["planned_action"] != "unlink_exact_file_only"
        or row["deletion_authorized"] != "false"
        for row in rows
    ):
        raise RetirementExecutionError("plan contains an unexpected action or state")
    return rows, summary


def _mount_options(path: Path) -> tuple[str, frozenset[str]]:
    resolved = path.resolve()
    best: tuple[int, str, frozenset[str]] | None = None
    with Path("/proc/self/mountinfo").open(encoding="utf-8") as handle:
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
            candidate = (
                len(mountpoint.parts),
                str(mountpoint),
                frozenset(fields[5].split(",")),
            )
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        raise RetirementExecutionError(f"cannot resolve mount for {path}")
    return best[1], best[2]


def validate_candidate(row: dict, approved_roots: set[Path]) -> Path:
    root = Path(row["candidate_root"]).resolve()
    if root not in approved_roots:
        raise RetirementExecutionError(f"unapproved candidate root: {root}")
    path = Path(row["candidate_path"])
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise RetirementExecutionError(
            f"candidate missing or outside approved root: {path}"
        ) from error
    if resolved != path.absolute() or path.is_symlink():
        raise RetirementExecutionError(f"candidate traverses a symlink: {path}")
    return resolved


def verify_authoritative_rows(rows: list[dict]) -> None:
    checked: set[tuple[str, str, int, str]] = set()
    for row in rows:
        size_bytes = int(row["authoritative_size_bytes"])
        expected = row["authoritative_sha256"]
        for label in ("authoritative_primary_path", "authoritative_independent_path"):
            path = Path(row[label])
            key = (label, str(path), size_bytes, expected)
            if key in checked:
                continue
            if sha256_file_stable(path, size_bytes) != expected:
                raise RetirementExecutionError(
                    f"authoritative fixity mismatch: {path}"
                )
            checked.add(key)


def append_event(handle, event: dict) -> None:
    handle.write(json.dumps(event, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def execute(
    rows: list[dict],
    summary: dict,
    execution_dir: Path,
    approved_digest: str,
    approved_by: str,
) -> dict:
    approved_roots = {Path(root).resolve() for root in summary["approved_roots"]}
    for root in approved_roots:
        mountpoint, options = _mount_options(root)
        if "rw" not in options:
            raise RetirementExecutionError(
                f"retirement root is not writable: {root} (mount {mountpoint})"
            )
    verify_authoritative_rows(rows)
    candidates: list[tuple[dict, Path]] = []
    for row in sorted(rows, key=lambda value: int(value["sequence"])):
        path = validate_candidate(row, approved_roots)
        expected = row["candidate_sha256"]
        if sha256_file_stable(path, int(row["candidate_size_bytes"])) != expected:
            raise RetirementExecutionError(f"candidate fixity mismatch: {path}")
        candidates.append((row, path))

    execution_dir.mkdir(parents=True, exist_ok=False)
    start = {
        "schema": "iss-recovery-retirement-execution-v1",
        "status": "started",
        "started_at_utc": utc_now(),
        "approved_by": approved_by,
        "approved_plan_sha256": approved_digest,
        "candidate_paths": len(candidates),
        "candidate_bytes": sum(
            int(row["candidate_size_bytes"]) for row, _ in candidates
        ),
        "rollback": (
            "No byte-level rollback is promised. The preservation package and "
            "its independent copy are the retained authority."
        ),
    }
    start_path = execution_dir / "execution-start.json"
    start_path.write_text(
        json.dumps(start, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    events_path = execution_dir / "unlink-events.jsonl"
    completed = 0
    completed_bytes = 0
    with events_path.open("x", encoding="utf-8") as events:
        for row, path in candidates:
            size_bytes = int(row["candidate_size_bytes"])
            expected = row["candidate_sha256"]
            actual = sha256_file_stable(path, size_bytes)
            if actual != expected:
                raise RetirementExecutionError(
                    f"pre-unlink digest mismatch: {path}"
                )
            append_event(
                events,
                {
                    "event": "unlink_intent",
                    "at_utc": utc_now(),
                    "sequence": int(row["sequence"]),
                    "path": str(path),
                    "size_bytes": size_bytes,
                    "sha256": expected,
                    "retirement_basis": row["retirement_basis"],
                },
            )
            path.unlink()
            if path.exists() or path.is_symlink():
                raise RetirementExecutionError(f"unlink did not remove path: {path}")
            completed += 1
            completed_bytes += size_bytes
            append_event(
                events,
                {
                    "event": "unlinked",
                    "at_utc": utc_now(),
                    "sequence": int(row["sequence"]),
                    "path": str(path),
                    "size_bytes": size_bytes,
                    "sha256": expected,
                },
            )

    for _, path in candidates:
        if path.exists() or path.is_symlink():
            raise RetirementExecutionError(f"retired path remains: {path}")
    report = {
        **start,
        "status": "complete",
        "completed_at_utc": utc_now(),
        "unlinked_paths": completed,
        "unlinked_bytes": completed_bytes,
        "directory_deletions": 0,
        "authoritative_primary_and_independent_fixity_verified": True,
        "all_planned_paths_absent": True,
    }
    report_path = execution_dir / "execution-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksums = []
    for path in (start_path, events_path, report_path):
        checksums.append(f"{digest_file(path)}  {path.name}")
    (execution_dir / "checksums.sha256").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--approved-plan-sha256", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--execution-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.apply:
        raise RetirementExecutionError("refusing without --apply")
    if len(args.approved_plan_sha256) != 64:
        raise RetirementExecutionError("approved plan SHA-256 must be 64 hex digits")
    plan_dir = args.plan_dir.resolve()
    execution_dir = args.execution_dir.resolve()
    if execution_dir.exists():
        raise RetirementExecutionError(
            f"execution directory already exists: {execution_dir}"
        )
    rows, summary = load_plan(plan_dir, args.approved_plan_sha256.lower())
    report = execute(
        rows,
        summary,
        execution_dir,
        args.approved_plan_sha256.lower(),
        args.approved_by,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RetirementExecutionError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
