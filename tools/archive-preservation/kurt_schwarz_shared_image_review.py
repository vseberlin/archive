#!/usr/bin/env python3
"""Freeze shared-container image aliases and two reviewed local-only objects."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-shared-image-review-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_INPUT = WORK_ROOT / (
    "work/reconciliation/shared-images/"
    "20260730T102000Z-google-drive-kurt-schwarz-images"
)
LOCAL_ONLY = {
    "KS-6-BZ_0629-1": Path(
        "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
        "UPLOADS/5.Upload/1636947_KB/KS-6-BZ_0629-1.tif"
    ),
    "KS-6-BZ_0629-2": Path(
        "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
        "UPLOADS/5.Upload/1636947_KB/KS-6-BZ_0629-2.tif"
    ),
}
MONTAGE = WORK_ROOT / (
    "work/visual-review/shared-container-local-only-20260730/"
    "KS-6-BZ_0629-pair.jpg"
)
REVIEW_FIELDS = (
    "source_container",
    "member",
    "member_sha256",
    "historical_inventory",
    "accepted_record_key",
    "accepted_md_object_id",
    "accepted_current_inventory",
    "initial_mapping_state",
    "reviewed_mapping_state",
    "review_basis",
    "phash_distance",
    "dhash_distance",
    "extracted_review_path",
    "review_evidence_path",
    "deletion_authorized",
)
ADDITION_FIELDS = (
    "record_key",
    "record_state",
    "historical_inventory",
    "embedded_title",
    "embedded_description",
    "scan_signature",
    "creator",
    "material",
    "selected_tiff_path",
    "shared_jpeg_member",
    "shared_jpeg_sha256",
    "review_evidence_path",
    "deletion_authorized",
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


def reviewed_state(row: dict[str, str]) -> tuple[str, str, str]:
    state = row["mapping_state"]
    if state == "mapped_alias":
        return (
            "confirmed_alias",
            row["mapped_record_key"],
            "documented inventory, workbook, or batch alias",
        )
    if state == "probable_visual_alias_review":
        return (
            "confirmed_visual_alias",
            row["mapped_record_key"],
            "unique bounded perceptual match to current museum-digital image",
        )
    if (
        state == "unresolved_visual_review"
        and row["historical_inventory"] in LOCAL_ONLY
    ):
        return (
            "reviewed_local_only_object",
            "local-shared-container:" + row["historical_inventory"],
            "distinct local frame with exclusive 16-bit TIFF and no current MD match",
        )
    raise ReviewError(
        f"unreviewed shared image state: {row['historical_inventory']} {state}"
    )


def exif_metadata(paths: list[Path]) -> dict[str, dict[str, Any]]:
    result = subprocess.run(
        [
            "exiftool",
            "-json",
            "-Artist",
            "-Creator",
            "-Description",
            "-ImageDescription",
            "-Title",
            "-Keywords",
            "-Subject",
            *map(str, paths),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    rows = json.loads(result.stdout)
    return {row["SourceFile"]: row for row in rows}


def atomic_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]
) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
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


def run(input_run: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with (input_run / "shared-image-reconciliation.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        source_rows = list(csv.DictReader(handle))
    metadata = exif_metadata(list(LOCAL_ONLY.values()))
    review_rows: list[dict[str, str]] = []
    additions: list[dict[str, str]] = []
    for row in source_rows:
        state, record_key, basis = reviewed_state(row)
        review_rows.append(
            {
                "source_container": row["source_container"],
                "member": row["member"],
                "member_sha256": row["member_sha256"],
                "historical_inventory": row["historical_inventory"],
                "accepted_record_key": record_key,
                "accepted_md_object_id": row["mapped_md_object_id"],
                "accepted_current_inventory": row["mapped_current_inventory"],
                "initial_mapping_state": row["mapping_state"],
                "reviewed_mapping_state": state,
                "review_basis": basis,
                "phash_distance": row["phash_distance"],
                "dhash_distance": row["dhash_distance"],
                "extracted_review_path": row["extracted_review_path"],
                "review_evidence_path": (
                    str(MONTAGE)
                    if state == "reviewed_local_only_object"
                    else row["extracted_review_path"]
                ),
                "deletion_authorized": "false",
            }
        )
        if state != "reviewed_local_only_object":
            continue
        inventory = row["historical_inventory"]
        source = LOCAL_ONLY[inventory]
        exif = metadata[str(source)]
        embedded_title = str(exif.get("Title", ""))
        additions.append(
            {
                "record_key": record_key,
                "record_state": "local_only_shared_container",
                "historical_inventory": inventory,
                "embedded_title": embedded_title,
                "embedded_description": str(
                    exif.get("Description") or exif.get("ImageDescription") or ""
                ),
                "scan_signature": Path(embedded_title).stem,
                "creator": str(exif.get("Creator") or exif.get("Artist") or ""),
                "material": str(exif.get("Keywords") or exif.get("Subject") or ""),
                "selected_tiff_path": str(source),
                "shared_jpeg_member": row["member"],
                "shared_jpeg_sha256": row["member_sha256"],
                "review_evidence_path": str(MONTAGE),
                "deletion_authorized": "false",
            }
        )
    if {row["historical_inventory"] for row in additions} != set(LOCAL_ONLY):
        raise ReviewError("local-only shared image review is incomplete")
    review_rows.sort(key=lambda row: row["member"])
    additions.sort(key=lambda row: row["historical_inventory"])
    atomic_csv(output / "reviewed-shared-images.csv", REVIEW_FIELDS, review_rows)
    atomic_csv(output / "local-only-object-additions.csv", ADDITION_FIELDS, additions)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "shared_image_reconciliation": str(input_run),
        "reviewed_member_images": len(review_rows),
        "reviewed_state_counts": dict(
            Counter(row["reviewed_mapping_state"] for row in review_rows)
        ),
        "local_only_object_additions": len(additions),
        "unresolved_rows": 0,
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(args.input.expanduser().resolve(), output)
    except (
        OSError,
        csv.Error,
        json.JSONDecodeError,
        ValueError,
        subprocess.SubprocessError,
        ReviewError,
    ) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
