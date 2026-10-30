#!/usr/bin/env python3
"""Stat-only live drift check for the guarded TFA source projection."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path("/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation")
SOURCE_PREFIX = "/media/vladimir/Elements/restore/"


def mount_evidence() -> dict[str, str]:
    payload = json.loads(
        subprocess.run(
            [
                "findmnt",
                "-J",
                "-T",
                "/media/vladimir/Elements/restore/archive/WF",
                "-o",
                "TARGET,SOURCE,FSTYPE,OPTIONS",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["filesystems"][0]
    if (
        payload["source"] != "/dev/sdf1"
        or payload["fstype"] != "ntfs3"
        or "ro" not in set(payload["options"].split(","))
    ):
        raise RuntimeError(f"source mount guard failed: {payload}")
    return payload


def main() -> int:
    receipt = ROOT / "source-drift-validation.json"
    if receipt.exists():
        raise RuntimeError(f"refusing to replace drift receipt: {receipt}")
    start_mount = mount_evidence()
    candidate_mtimes: dict[str, int] = {}
    with (ROOT / "candidates.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["source_kind"] != "read_only_usb":
                continue
            source_path = row["source_path"]
            expected = int(row["mtime_ns"])
            previous = candidate_mtimes.setdefault(source_path, expected)
            if previous != expected:
                raise RuntimeError(f"conflicting expected mtimes: {source_path}")

    missing: list[str] = []
    size_mismatches: list[dict[str, int | str]] = []
    mtime_mismatches: list[dict[str, int | str]] = []
    digest = hashlib.sha256()
    checked = 0
    with (ROOT / "source-disposition-proposal.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            source_path = row["source_path"]
            if not source_path.startswith(SOURCE_PREFIX):
                raise RuntimeError(f"unexpected drain-source root: {source_path}")
            try:
                stat = Path(source_path).stat()
            except FileNotFoundError:
                missing.append(source_path)
                continue
            expected_size = int(row["source_size_bytes"])
            if stat.st_size != expected_size:
                size_mismatches.append(
                    {
                        "source_path": source_path,
                        "expected": expected_size,
                        "actual": stat.st_size,
                    }
                )
            expected_mtime = candidate_mtimes.get(source_path)
            if expected_mtime is not None and stat.st_mtime_ns != expected_mtime:
                mtime_mismatches.append(
                    {
                        "source_path": source_path,
                        "expected": expected_mtime,
                        "actual": stat.st_mtime_ns,
                    }
                )
            digest.update(source_path.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(b"\n")
            checked += 1
            if checked % 5000 == 0:
                print(f"checked {checked} source paths", flush=True)

    end_mount = mount_evidence()
    result = "ok" if not missing and not size_mismatches and not mtime_mismatches else "failed"
    payload = {
        "format": "iss-tfa-source-drift-validation-v1",
        "validated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "result": result,
        "source_mount_start": start_mount,
        "source_mount_end": end_mount,
        "source_read_only": True,
        "source_paths_expected": checked + len(missing),
        "source_paths_checked": checked,
        "candidate_mtimes_checked": len(candidate_mtimes),
        "missing_count": len(missing),
        "size_mismatch_count": len(size_mismatches),
        "mtime_mismatch_count": len(mtime_mismatches),
        "missing_sample": missing[:20],
        "size_mismatch_sample": size_mismatches[:20],
        "mtime_mismatch_sample": mtime_mismatches[:20],
        "source_stat_fingerprint": "sha256:" + digest.hexdigest(),
        "source_files_modified": 0,
    }
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
