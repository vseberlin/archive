#!/usr/bin/env python3
"""Copy a small Trafo selected-source pilot into work with copy/hash evidence."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import copyhash_runner


FORMAT = "iss-trafo-copy-pilot-v1"
TOOL_ROOT = Path(__file__).resolve().parent
COPYHASH = TOOL_ROOT / "bin" / "archive-copyhash"


class CopyPilotError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_name(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip(".-")
    return cleaned or "unnamed"


def copyhash(
    source: Path,
    destination: Path,
    expected_digest: str = "",
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise CopyPilotError(f"refusing to replace existing file: {destination}")
    try:
        return copyhash_runner.copy_file(
            COPYHASH,
            source,
            destination,
            expected_digest,
        )
    except copyhash_runner.CopyHashError as error:
        raise CopyPilotError(str(error)) from error


def pdfinfo(path: Path) -> dict[str, str]:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=60,
    )
    info = {"pdfinfo_status": str(result.returncode), "pdf_pages": "", "pdf_error": ""}
    if result.returncode != 0:
        info["pdf_error"] = result.stdout.strip()
        return info
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            info["pdf_pages"] = line.split(":", 1)[1].strip()
            break
    return info


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def selected_records(
    plan: Path,
    inventories: list[str] | None,
    all_records: bool,
) -> list[dict[str, str]]:
    if bool(inventories) == all_records:
        raise CopyPilotError("choose either --all-records or one/more --inventory values")
    if all_records:
        with plan.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    assert inventories is not None
    wanted = set(inventories)
    rows = []
    with plan.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["inventory"] in wanted:
                rows.append(row)
    found = {row["inventory"] for row in rows}
    missing = sorted(wanted - found)
    if missing:
        raise CopyPilotError("missing inventories in plan: " + ", ".join(missing))
    return sorted(rows, key=lambda row: inventories.index(row["inventory"]))


def build(args: argparse.Namespace) -> dict[str, Any]:
    if not copyhash_runner.helper_available(COPYHASH):
        raise CopyPilotError(f"copy helper is unavailable: {COPYHASH}")
    work_root = args.work_root.expanduser().resolve()
    if not str(work_root).startswith("/mnt/volume/iss-archive-work/"):
        raise CopyPilotError("work root must be below /mnt/volume/iss-archive-work")
    output = work_root / "work" / "selected-copy" / args.pilot_id
    output.mkdir(parents=True, exist_ok=False)
    records = selected_records(args.plan, args.inventory, args.all_records)

    copy_rows: list[dict[str, Any]] = []
    checksum_rows: list[dict[str, Any]] = []
    for record in records:
        inventory = record["inventory"]
        issue_dir = output / "masters" / safe_name(inventory)
        selected_paths = [
            Path(value.strip())
            for value in record["selected_paths"].split(" | ")
            if value.strip()
        ]
        if not selected_paths:
            raise CopyPilotError(f"record has no selected paths: {inventory}")
        for index, source in enumerate(selected_paths, start=1):
            if not source.is_file():
                raise CopyPilotError(f"selected source is missing: {source}")
            if record["selected_role"] == "preservation_tiff_pages":
                destination = issue_dir / "pages" / source.name
                role = "tiff_page"
            else:
                destination = issue_dir / "local.pdf"
                role = "local_pdf_fallback"
            digest = copyhash(source, destination)
            relative = destination.relative_to(output).as_posix()
            row = {
                "inventory": inventory,
                "md_object_id": record["md_object_id"],
                "role": role,
                "source_path": str(source),
                "destination_path": relative,
                "sha256": digest,
                "size_bytes": destination.stat().st_size,
                "sequence": index,
                "selected_role": record["selected_role"],
                "md_pdf_url": record["md_pdf_url"],
            }
            copy_rows.append(row)
            checksum_rows.append(
                {
                    "path": relative,
                    "sha256": digest,
                    "size_bytes": destination.stat().st_size,
                }
            )
        local_pdf = Path(record["selected_local_pdf"]) if record["selected_local_pdf"] else None
        if (
            record["selected_role"] == "preservation_tiff_pages"
            and local_pdf
            and local_pdf.is_file()
        ):
            destination = issue_dir / "local.pdf"
            digest = copyhash(local_pdf, destination)
            info = pdfinfo(destination)
            relative = destination.relative_to(output).as_posix()
            row = {
                "inventory": inventory,
                "md_object_id": record["md_object_id"],
                "role": "local_pdf",
                "source_path": str(local_pdf),
                "destination_path": relative,
                "sha256": digest,
                "size_bytes": destination.stat().st_size,
                "sequence": "",
                "selected_role": record["selected_role"],
                "md_pdf_url": record["md_pdf_url"],
                **info,
            }
            copy_rows.append(row)
            checksum_rows.append(
                {
                    "path": relative,
                    "sha256": digest,
                    "size_bytes": destination.stat().st_size,
                }
            )
        elif record["selected_role"] == "local_pdf_fallback":
            info = pdfinfo(output / "masters" / safe_name(inventory) / "local.pdf")
            copy_rows[-1].update(info)

    write_csv(
        output / "manifests" / "selected-copy.csv",
        [
            "inventory",
            "md_object_id",
            "role",
            "source_path",
            "destination_path",
            "sha256",
            "size_bytes",
            "sequence",
            "selected_role",
            "md_pdf_url",
            "pdfinfo_status",
            "pdf_pages",
            "pdf_error",
        ],
        copy_rows,
    )
    write_csv(
        output / "manifests" / "checksums.csv",
        ["path", "sha256", "size_bytes"],
        checksum_rows,
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "pilot_id": args.pilot_id,
        "output": str(output),
        "records": len(records),
        "copied_files": len(copy_rows),
        "bytes": sum(int(row["size_bytes"]) for row in copy_rows),
        "inventories": [row["inventory"] for row in records],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"result": "ok", **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--pilot-id", default="pilot-001")
    parser.add_argument("--inventory", action="append")
    parser.add_argument("--all-records", action="store_true")
    return parser


def main() -> int:
    result = build(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
