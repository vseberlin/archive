#!/usr/bin/env python3
"""Hash the bounded Paragon evidence roots on the authoritative Elements USB."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


RUN = Path(__file__).resolve().parent
USB = Path("/media/vladimir/Elements")
OUTPUT = RUN / "usb-inventory.csv"
SUMMARY = RUN / "usb-inventory-summary.json"
ROOTS = (
    ("paragon-main", USB / "restore/archive/Paragon"),
    ("paragon-md", USB / "restore/archive/WF/Paragon fertig für md"),
    (
        "paragon-history",
        USB / "restore/archive/Findbücher/Andere Scans vom Ricoah/Paragon",
    ),
    (
        "paragon-history-duplicate",
        USB / "restore/archive/WF/Findbücher/Andere Scans vom Ricoah/Paragon",
    ),
    ("paragon-scans-finished", USB / "restore/archive/SCANS-fertig/Paragon"),
    ("paragon-cropped", USB / "restore/archive/WF/Freistellen/Paragon"),
    (
        "paragon-angle-corrected",
        USB / "restore/archive/WF/Freistellen/Paragon_Winkel-korrigiert",
    ),
)
FIELDS = [
    "source_path",
    "source_root",
    "relative_path",
    "extension",
    "size_bytes",
    "mtime_ns",
    "mtime_utc",
    "sha256",
]


def ensure_read_only() -> str:
    output = subprocess.run(
        [
            "findmnt",
            "-rn",
            "-T",
            str(ROOTS[0][1]),
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    line = output.splitlines()[0] if output else ""
    options = line.split(None, 3)[-1].split(",") if line else []
    if "ro" not in options or "rw" in options:
        raise RuntimeError(f"USB is not read-only: {line}")
    return line


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def main() -> int:
    if OUTPUT.exists() or SUMMARY.exists():
        raise RuntimeError("USB inventory already exists")
    mount = ensure_read_only()
    entries: list[tuple[str, Path, Path]] = []
    seen: set[Path] = set()
    root_counts = {}
    for name, root in ROOTS:
        if not root.is_dir():
            raise RuntimeError(f"missing source root: {root}")
        paths = sorted(path for path in root.rglob("*") if path.is_file())
        unique = [path for path in paths if path not in seen]
        seen.update(unique)
        root_counts[name] = len(unique)
        entries.extend((name, root, path) for path in unique)
    total_bytes = sum(path.stat().st_size for _, _, path in entries)
    print(
        f"Hashing {len(entries)} files ({total_bytes} bytes) from read-only USB",
        flush=True,
    )
    rows = []
    hashed_bytes = 0
    started = time.monotonic()
    last_report = started
    for index, (name, root, path) in enumerate(entries, 1):
        before = path.stat()
        file_digest = digest(path)
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError(f"source changed while hashing: {path}")
        hashed_bytes += after.st_size
        rows.append(
            {
                "source_path": str(path),
                "source_root": name,
                "relative_path": path.relative_to(root).as_posix(),
                "extension": path.suffix.casefold(),
                "size_bytes": after.st_size,
                "mtime_ns": after.st_mtime_ns,
                "mtime_utc": datetime.fromtimestamp(
                    after.st_mtime, UTC
                ).isoformat().replace("+00:00", "Z"),
                "sha256": file_digest,
            }
        )
        now = time.monotonic()
        if index == len(entries) or now - last_report >= 20:
            elapsed = max(now - started, 0.001)
            rate = hashed_bytes / elapsed
            remaining = total_bytes - hashed_bytes
            eta = remaining / rate if rate else 0
            print(
                f"{index}/{len(entries)} files; {hashed_bytes}/{total_bytes} "
                f"bytes; {rate / 1024 / 1024:.1f} MiB/s; ETA {eta:.0f}s",
                flush=True,
            )
            last_report = now
    temporary = OUTPUT.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, OUTPUT)
    summary = {
        "format": "iss-paragon-authoritative-usb-inventory-v1",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mount": mount,
        "roots": [{"name": name, "path": str(path)} for name, path in ROOTS],
        "root_file_counts": root_counts,
        "files": len(rows),
        "bytes": total_bytes,
        "unique_sha256": len({row["sha256"] for row in rows}),
    }
    SUMMARY.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
