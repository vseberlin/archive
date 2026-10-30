#!/usr/bin/env python3
"""Raw read-only Trafo 1988 search across every database-like USB file."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SOURCE = Path("/media/vladimir/Elements")
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/deep-search/"
    "TRO-1988-metadata-usb-20260727"
)
INVENTORY = OUTPUT / "usb-file-inventory.csv"
HITS = OUTPUT / "database-raw-hits.jsonl"
AUDIT = OUTPUT / "database-raw-audit.json"

EXACT_PATTERNS = (
    b"tro-1988",
    b"tro_1988",
    b"tro.1988",
    b"tro 1988",
    b"1988-tro",
    b"1988_tro",
    b"1988.tro",
    b"1988 tro",
)
BLOCK_SIZE = 8 * 1024 * 1024
OVERLAP_SIZE = 16 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def assert_read_only_source() -> None:
    result = subprocess.run(
        ["findmnt", "-no", "OPTIONS", "--target", str(SOURCE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    options = [line.strip().split(",") for line in result.stdout.splitlines()]
    if result.returncode or not options or not all("ro" in row for row in options):
        raise RuntimeError(f"source is not read-only: {result.stdout.strip()}")
    if SOURCE == OUTPUT or SOURCE in OUTPUT.parents:
        raise RuntimeError("output must not be under the evidence source")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def database_rows() -> list[dict[str, str]]:
    with INVENTORY.open(encoding="utf-8", newline="") as handle:
        return [
            row for row in csv.DictReader(handle) if row["category"] == "database"
        ]


def find_match(payload: bytes) -> tuple[int, int] | None:
    lowered = payload.lower()
    positions = [
        position
        for pattern in EXACT_PATTERNS
        if (position := lowered.find(pattern)) >= 0
    ]
    if positions:
        position = min(positions)
        return position, position + 8

    position = lowered.find(b"trafo")
    while position >= 0:
        start = max(0, position - 4096)
        end = min(len(lowered), position + 4096)
        year = lowered.find(b"1988", start, end)
        der = lowered.rfind(b"der", max(0, position - 64), position)
        if year >= 0 and der >= 0:
            return min(der, year), max(position + 5, year + 4)
        position = lowered.find(b"trafo", position + 5)
    return None


def scan(path: Path) -> tuple[int, dict[str, Any] | None]:
    read_bytes = 0
    overlap = b""
    with path.open("rb") as handle:
        while block := handle.read(BLOCK_SIZE):
            read_bytes += len(block)
            payload = overlap + block
            match = find_match(payload)
            if match:
                match_start, match_end = match
                start = max(0, match_start - 300)
                end = min(len(payload), match_end + 700)
                return read_bytes, {
                    "path": str(path),
                    "match_type": "raw-exact-or-nearby",
                    "approximate_offset": max(
                        0, read_bytes - len(payload) + match_start
                    ),
                    "snippet": " ".join(
                        payload[start:end].decode("utf-8", "replace").split()
                    ),
                }
            overlap = payload[-OVERLAP_SIZE:]
    return read_bytes, None


def main() -> int:
    assert_read_only_source()
    rows = database_rows()
    hits: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    bytes_read = 0
    for index, row in enumerate(rows, 1):
        path = Path(row["path"])
        try:
            current_bytes, hit = scan(path)
            bytes_read += current_bytes
            if hit:
                hit["size_bytes"] = int(row["size_bytes"])
                hits.append(hit)
        except Exception as error:
            errors.append({"path": str(path), "error": str(error)})
        if index % 50 == 0:
            print(f"database files {index}/{len(rows)}", flush=True)
    write_jsonl(HITS, hits)
    result = {
        "completed_at": now(),
        "source_root": str(SOURCE),
        "source_read_only_required": True,
        "database_files_scanned": len(rows),
        "inventory_bytes": sum(int(row["size_bytes"]) for row in rows),
        "bytes_read_until_match_or_eof": bytes_read,
        "hit_count": len(hits),
        "error_count": len(errors),
        "errors": errors,
        "hits_artifact": str(HITS),
    }
    atomic_json(AUDIT, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
