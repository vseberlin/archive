#!/usr/bin/env python3
"""Inventory explicit Kurt Schwarz members in the shared Google Drive backup."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


FORMAT = "iss-kurt-schwarz-shared-container-v1"
SOURCE_MOUNT = Path("/media/vladimir/Elements")
DEFAULT_CONTAINER = SOURCE_MOUNT / (
    "restore/archive/WF/GoogleDocsBackup/"
    "drive-download-20220713T123037Z-001.zip"
)
MEMBER_ROOT = PurePosixPath("Kurt Schwarz Fotografie")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
FIELDS = (
    "source_container",
    "container_sha256",
    "member",
    "member_size_bytes",
    "member_compressed_bytes",
    "member_crc32",
    "member_sha256",
    "extension",
    "source_role",
    "relationship",
    "drain_state",
    "deletion_authorized",
)


class ContainerError(RuntimeError):
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
        raise ContainerError(f"path escaped allowed root: {resolved}") from error
    return resolved


def digest_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    while block := handle.read(8 * 1024 * 1024):
        digest.update(block)
    return "sha256:" + digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return digest_stream(handle)


def safe_member_path(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or MEMBER_ROOT not in member.parents:
        raise ContainerError(f"unsafe or out-of-scope member: {name}")
    return member


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_document(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def run(container: Path, output: Path) -> dict[str, Any]:
    if not root_is_read_only(SOURCE_MOUNT):
        raise ContainerError(f"USB source is not read-only: {SOURCE_MOUNT}")
    output.mkdir(parents=True, exist_ok=False)
    container_sha256 = sha256_file(container)
    rows: list[dict[str, Any]] = []
    extension_counts: Counter[str] = Counter()
    extracted_documents = 0
    with zipfile.ZipFile(container) as archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and PurePosixPath(info.filename).parts[:1] == (str(MEMBER_ROOT),)
        ]
        if len(members) != len({info.filename for info in members}):
            raise ContainerError("shared container contains duplicate member names")
        for index, info in enumerate(members, 1):
            member = safe_member_path(info.filename)
            extension = member.suffix.casefold()
            extension_counts[extension or "[none]"] += 1
            with archive.open(info) as handle:
                payload = handle.read()
            member_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
            role = (
                "project_metadata_document"
                if extension in {".xlsx", ".docx"}
                else "project_access_image"
            )
            if role == "project_metadata_document":
                relative = Path(*member.relative_to(MEMBER_ROOT).parts)
                write_document(output / "documents" / relative, payload)
                extracted_documents += 1
            rows.append(
                {
                    "source_container": str(container),
                    "container_sha256": container_sha256,
                    "member": info.filename,
                    "member_size_bytes": info.file_size,
                    "member_compressed_bytes": info.compress_size,
                    "member_crc32": f"{info.CRC:08x}",
                    "member_sha256": member_sha256,
                    "extension": extension,
                    "source_role": role,
                    "relationship": "direct_project_member_in_shared_container",
                    "drain_state": "retain_shared_container",
                    "deletion_authorized": "false",
                }
            )
            if index % 250 == 0:
                print(f"members={index}/{len(members)}", flush=True)
    rows.sort(key=lambda row: row["member"])
    atomic_csv(output / "members.csv", rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "source_container": str(container),
        "container_size_bytes": container.stat().st_size,
        "container_sha256": container_sha256,
        "source_read_only": True,
        "member_root": str(MEMBER_ROOT),
        "member_rows": len(rows),
        "member_bytes": sum(int(row["member_size_bytes"]) for row in rows),
        "unique_member_sha256": len({row["member_sha256"] for row in rows}),
        "extension_counts": dict(sorted(extension_counts.items())),
        "extracted_metadata_documents": extracted_documents,
        "shared_container_drain_state": "retain_shared_container",
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", type=Path, default=DEFAULT_CONTAINER)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(args.container.expanduser().resolve(), output)
    except (OSError, ValueError, zipfile.BadZipFile, ContainerError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
