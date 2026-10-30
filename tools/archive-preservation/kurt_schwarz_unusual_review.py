#!/usr/bin/env python3
"""Freeze reviewed Kurt-Schwarz findings outside exclusive project roots."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-unusual-review-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
FIELDS = (
    "source_path",
    "relative_path",
    "size_bytes",
    "sha256",
    "review_state",
    "record_key",
    "canonical_source_path",
    "preservation_role",
    "source_scope",
    "drain_state",
    "deletion_authorized",
    "review_basis",
)


class ReviewError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ReviewError(f"path escaped allowed root: {resolved}") from error
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(16 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def classify(relative_path: str) -> tuple[str, str, str]:
    path = Path(relative_path)
    if path.name.casefold() == "ks-6-bz_0588-3.tif":
        return (
            "preserve_local_only_image",
            "local-shared:KS-6-BZ_0588-3",
            "local_object_master",
        )
    if path.suffix.casefold() == ".pdf" and "Buchprojket_Kurt-Schwarz" in path.name:
        return (
            "preserve_contextual_project_document",
            "",
            "project_document",
        )
    raise ReviewError(f"unreviewed unusual candidate: {relative_path}")


def build(discovery: Path, output: Path) -> dict[str, Any]:
    with (discovery / "usb-shared-and-unusual-paths.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        unusual = [
            row
            for row in csv.DictReader(handle)
            if row["scope_state"] == "unusual_path_candidate"
        ]
    rows: list[dict[str, str]] = []
    for source in unusual:
        state, record_key, role = classify(source["relative_path"])
        digest = sha256_file(Path(source["source_path"]))
        rows.append(
            {
                "source_path": source["source_path"],
                "relative_path": source["relative_path"],
                "size_bytes": source["size_bytes"],
                "sha256": digest,
                "review_state": state,
                "record_key": record_key,
                "canonical_source_path": "",
                "preservation_role": role,
                "source_scope": "cross_project_shared_source",
                "drain_state": "shared_source_hold",
                "deletion_authorized": "false",
                "review_basis": (
                    "distinct fourth frame confirmed by visual sequence review"
                    if record_key
                    else "direct Kurt-Schwarz book-project document"
                ),
            }
        )

    by_hash: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_hash[row["sha256"]].append(row)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        canonical = min(
            group,
            key=lambda row: (
                "/Archiv/" in row["source_path"],
                len(row["source_path"]),
                row["source_path"],
            ),
        )
        canonical["canonical_source_path"] = canonical["source_path"]
        for row in group:
            if row is canonical:
                continue
            row["review_state"] = "exact_duplicate_shared_source"
            row["canonical_source_path"] = canonical["source_path"]
            row["preservation_role"] = "duplicate_occurrence"
            row["review_basis"] = "exact SHA-256 duplicate of canonical shared source"

    rows.sort(key=lambda row: row["relative_path"])
    output.mkdir(parents=True, exist_ok=False)
    with (output / "unusual-reviewed.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "reviewed_rows": len(rows),
        "unique_payloads": len(by_hash),
        "review_state_counts": {
            state: sum(row["review_state"] == state for row in rows)
            for state in sorted({row["review_state"] for row in rows})
        },
        "shared_source_hold_rows": len(rows),
        "deletion_authorized_rows": 0,
    }
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(args.discovery.expanduser().resolve(), output)
    except (ReviewError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
