#!/usr/bin/env python3
"""Freeze the reviewed Kurt Schwarz visual exceptions without source mutation."""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-kurt-schwarz-visual-review-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_INPUT = WORK_ROOT / (
    "work/visual-verification/runs/"
    "20260729T235500Z-current-md-vs-curated-usb-static-assets/"
    "visual-verification.csv"
)
REVIEW_ROOT = WORK_ROOT / "work/visual-review/visual-exceptions-20260730"
FOUR_PAIR_MONTAGE = REVIEW_ROOT / "md-vs-usb-exceptions.jpg"
PREFIX_MONTAGE = REVIEW_ROOT / "md-103603-vs-old-prefix-0555.jpg"
MD_RECOVERY = WORK_ROOT / "work/md-image-recovery/51888/md-51888.jpg"

DECISIONS = {
    "103958": (
        "confirmed_visual_match",
        "same_image_crop_and_tonal_rendering_difference",
        "",
        FOUR_PAIR_MONTAGE,
    ),
    "109416": (
        "confirmed_visual_match",
        "same_image_crop_and_border_difference",
        "",
        FOUR_PAIR_MONTAGE,
    ),
    "98451": (
        "confirmed_visual_match",
        "same_image_crop_and_tonal_rendering_difference",
        "",
        FOUR_PAIR_MONTAGE,
    ),
    "98390": (
        "confirmed_visual_match",
        "same_image_crop_difference",
        "",
        FOUR_PAIR_MONTAGE,
    ),
    "103603": (
        "confirmed_visual_alias",
        "same_image_old_local_prefix_KS-6-BZ_0555_maps_to_current_KS-7-BZ_0555",
        (
            "/media/vladimir/Elements/restore/archive/WF/Kurt Schwarz/"
            "Alle-KS-Bilder_5000px/KS-6-BZ_0555.jpg"
        ),
        PREFIX_MONTAGE,
    ),
    "51888": (
        "resolved_md_surrogate",
        "no_local_curated_image_full_museum_digital_image_recovered",
        str(MD_RECOVERY),
        MD_RECOVERY,
    ),
}

FIELDS = (
    "md_object_id",
    "current_inventory",
    "initial_visual_state",
    "reviewed_visual_state",
    "accepted_local_or_recovery_path",
    "review_basis",
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


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
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


def run(input_path: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with input_path.open(encoding="utf-8-sig", newline="") as handle:
        input_rows = list(csv.DictReader(handle))
    by_id = {row["md_object_id"]: row for row in input_rows}
    if len(by_id) != len(input_rows):
        raise ReviewError("visual input contains duplicate museum-digital IDs")
    missing = sorted(set(DECISIONS) - set(by_id))
    if missing:
        raise ReviewError(f"review decision IDs absent from input: {missing}")

    rows: list[dict[str, str]] = []
    for object_id, (state, basis, accepted_path, evidence) in DECISIONS.items():
        source = by_id[object_id]
        if source["visual_state"] == "confirmed_visual_match":
            raise ReviewError(f"{object_id} was not an exception in the input")
        rows.append(
            {
                "md_object_id": object_id,
                "current_inventory": source["current_inventory"],
                "initial_visual_state": source["visual_state"],
                "reviewed_visual_state": state,
                "accepted_local_or_recovery_path": accepted_path
                or source["local_jpeg_path"],
                "review_basis": basis,
                "review_evidence_path": str(evidence),
                "deletion_authorized": "false",
            }
        )
    rows.sort(key=lambda row: int(row["md_object_id"]))
    atomic_csv(output / "reviewed-visual-exceptions.csv", rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "visual_verification_input": str(input_path),
        "reviewed_rows": len(rows),
        "reviewed_state_counts": dict(
            Counter(row["reviewed_visual_state"] for row in rows)
        ),
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
    except (OSError, csv.Error, ValueError, ReviewError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
