#!/usr/bin/env python3
"""Create a resumable SHA-256 inventory for exclusive Kurt-Schwarz USB scope."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-source-hashes-v1"
SOURCE_MOUNT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
FIELDS = (
    "source_path",
    "relative_path",
    "size_bytes",
    "mtime_ns",
    "sha256",
    "hash_state",
    "error",
    "deletion_authorized",
)


class HashError(RuntimeError):
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
        raise HashError(f"path escaped allowed root: {resolved}") from error
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(16 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def load_completed(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    completed: dict[str, dict[str, str]] = {}
    for row in rows:
        source = row["source_path"]
        if source in completed:
            raise HashError(f"duplicate resumable hash row: {source}")
        completed[source] = row
    return completed


def run(discovery: Path, output: Path) -> dict[str, Any]:
    if not root_is_read_only(SOURCE_MOUNT):
        raise HashError(f"USB source is not read-only: {SOURCE_MOUNT}")
    output.mkdir(parents=True, exist_ok=True)
    summary_path = output / "summary.json"
    if summary_path.exists():
        raise HashError(f"hash run already completed: {summary_path}")
    hashes_path = output / "source-hashes.csv"
    completed = load_completed(hashes_path)
    with (discovery / "usb-exclusive-candidates.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        candidates = list(csv.DictReader(handle))
    if len(candidates) != len({row["source_path"] for row in candidates}):
        raise HashError("discovery contains duplicate source paths")

    mode = "a" if hashes_path.exists() else "x"
    processed_bytes = sum(
        int(row["size_bytes"])
        for row in completed.values()
        if row["hash_state"] == "hashed"
    )
    with hashes_path.open(mode, encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        if mode == "x":
            writer.writeheader()
            handle.flush()
            os.fsync(handle.fileno())
        for index, candidate in enumerate(candidates, 1):
            source = candidate["source_path"]
            if source in completed:
                continue
            path = Path(source)
            row = {
                "source_path": source,
                "relative_path": candidate["relative_path"],
                "size_bytes": candidate["size_bytes"],
                "mtime_ns": candidate["mtime_ns"],
                "sha256": "",
                "hash_state": "",
                "error": "",
                "deletion_authorized": "false",
            }
            try:
                stat = path.stat()
                if stat.st_size != int(candidate["size_bytes"]):
                    raise HashError("size changed since reused inventory")
                if stat.st_mtime_ns != int(candidate["mtime_ns"]):
                    raise HashError("mtime changed since reused inventory")
                row["sha256"] = sha256_file(path)
                row["hash_state"] = "hashed"
                processed_bytes += stat.st_size
            except (OSError, HashError) as error:
                row["hash_state"] = "error"
                row["error"] = f"{type(error).__name__}: {error}"
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
            if index % 100 == 0:
                print(
                    f"scope_index={index}/{len(candidates)} "
                    f"completed={len(completed) + index} "
                    f"hashed_gib={processed_bytes / 2**30:.1f}",
                    flush=True,
                )

    completed_rows = load_completed(hashes_path)
    states = Counter(row["hash_state"] for row in completed_rows.values())
    if len(completed_rows) != len(candidates):
        raise HashError(
            f"incomplete hash run: {len(completed_rows)}/{len(candidates)}"
        )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "source_read_only": True,
        "scope_rows": len(candidates),
        "scope_bytes": sum(int(row["size_bytes"]) for row in candidates),
        "hash_state_counts": dict(states),
        "unique_sha256": len(
            {
                row["sha256"]
                for row in completed_rows.values()
                if row["sha256"]
            }
        ),
        "deletion_authorized_rows": 0,
    }
    temporary = summary_path.with_name(f".{summary_path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, summary_path)
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
        summary = run(args.discovery.expanduser().resolve(), output)
    except (HashError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
