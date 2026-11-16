#!/usr/bin/env python3
"""Validate a Trafo final manifest across declared storage roots."""

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


FORMAT = "iss-trafo-final-manifest-validation-v1"
WORK_ROOT_PREFIX = "/mnt/volume/iss-archive-work/"


class ValidationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not str(value).strip():
        raise ValidationError(f"unsafe relative path: {value!r}")
    return path


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build(args: argparse.Namespace) -> dict[str, Any]:
    manifest = args.manifest.expanduser().resolve(strict=True)
    if not str(manifest).startswith(WORK_ROOT_PREFIX):
        raise ValidationError(f"manifest must be below {WORK_ROOT_PREFIX}")
    manifest_rows = read_csv(manifest)
    selected_storage_roots = set(args.storage_root_id or [])
    rows = [
        row
        for row in manifest_rows
        if not selected_storage_roots
        or row["storage_root_id"] in selected_storage_roots
    ]
    if selected_storage_roots:
        found_storage_roots = {row["storage_root_id"] for row in rows}
        missing_storage_roots = sorted(
            selected_storage_roots - found_storage_roots
        )
        if missing_storage_roots:
            raise ValidationError(
                "manifest contains no rows for storage roots: "
                + ", ".join(missing_storage_roots)
            )
    seen_destinations: set[str] = set()
    errors: list[dict[str, str]] = []
    role_counts: Counter[str] = Counter()
    component_counts: Counter[str] = Counter()
    storage_counts: Counter[str] = Counter()
    bytes_checked = 0

    for index, row in enumerate(rows, start=1):
        role_counts[row["role"]] += 1
        component_counts[row["final_component"]] += 1
        storage_counts[row["storage_root_id"]] += 1
        destination_path = row["destination_path"]
        try:
            relative = safe_relative(destination_path)
        except ValidationError as error:
            errors.append({"row": str(index), "path": destination_path, "error": str(error)})
            continue
        if destination_path in seen_destinations:
            errors.append(
                {
                    "row": str(index),
                    "path": destination_path,
                    "error": "duplicate destination_path in final manifest",
                }
            )
        seen_destinations.add(destination_path)

        storage_root = Path(row["storage_root"]).expanduser().resolve()
        if not str(storage_root).startswith(WORK_ROOT_PREFIX):
            errors.append(
                {
                    "row": str(index),
                    "path": str(storage_root),
                    "error": f"storage_root must be below {WORK_ROOT_PREFIX}",
                }
            )
            continue
        file_path = storage_root / relative
        try:
            resolved = file_path.resolve(strict=True)
        except FileNotFoundError:
            errors.append(
                {"row": str(index), "path": str(file_path), "error": "referenced file missing"}
            )
            continue
        if storage_root not in resolved.parents:
            errors.append(
                {"row": str(index), "path": str(resolved), "error": "path escapes storage root"}
            )
            continue
        actual_size = resolved.stat().st_size
        expected_size = int(row["size_bytes"])
        if actual_size != expected_size:
            errors.append(
                {
                    "row": str(index),
                    "path": str(resolved),
                    "error": f"size mismatch expected {expected_size} got {actual_size}",
                }
            )
            continue
        actual_digest = digest_file(resolved)
        if actual_digest != row["sha256"]:
            errors.append(
                {
                    "row": str(index),
                    "path": str(resolved),
                    "error": f"sha256 mismatch expected {row['sha256']} got {actual_digest}",
                }
            )
            continue
        bytes_checked += actual_size
        if args.progress_every and index % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "progress_rows": index,
                        "total_rows": len(rows),
                        "bytes_checked": bytes_checked,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    result = {
        "format": FORMAT,
        "created_at": utc_now(),
        "manifest": str(manifest),
        "manifest_rows": len(manifest_rows),
        "rows": len(rows),
        "selected_storage_root_ids": sorted(selected_storage_roots),
        "bytes_checked": bytes_checked,
        "role_counts": dict(sorted(role_counts.items())),
        "component_counts": dict(sorted(component_counts.items())),
        "storage_root_counts": dict(sorted(storage_counts.items())),
        "errors": errors[: args.max_errors],
        "error_count": len(errors),
        "result": "ok" if not errors else "error",
    }
    if args.output:
        write_json(args.output.expanduser().resolve(), result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--max-errors", type=int, default=50)
    parser.add_argument("--storage-root-id", action="append")
    return parser


def main() -> int:
    result = build(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["result"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
