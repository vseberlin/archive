#!/usr/bin/env python3
"""Reconcile Kurt Schwarz JPEG members found only in a shared ZIP container."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
import kurt_schwarz_visual_verify as visual  # noqa: E402


FORMAT = "iss-kurt-schwarz-shared-image-reconciliation-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_CONTAINER_RUN = WORK_ROOT / (
    "work/reconciliation/shared-containers/"
    "20260730T091000Z-google-drive-kurt-schwarz"
)
DEFAULT_WORKBOOK_RUN = WORK_ROOT / (
    "work/reconciliation/shared-workbooks/"
    "20260730T093000Z-google-drive-kurt-schwarz-workbooks"
)
DEFAULT_OBJECT_PLAN = WORK_ROOT / (
    "work/reconciliation/object-plans/"
    "20260730T001000Z-kurt-schwarz-object-plan/object-plan.csv"
)
DEFAULT_BATCH_RUN = WORK_ROOT / (
    "work/reconciliation/batch-catalogs/"
    "20260729T234000Z-kurt-schwarz-batch-catalogs"
)
DEFAULT_DISCOVERY = WORK_ROOT / (
    "work/discovery/runs/20260729T233000Z-kurt-schwarz-discovery"
)
DEFAULT_PREVIEWS = WORK_ROOT / (
    "work/visual-verification/runs/"
    "20260729T235500Z-current-md-vs-curated-usb-static-assets/previews"
)
FIELDS = (
    "source_container",
    "member",
    "member_sha256",
    "historical_inventory",
    "mapped_record_key",
    "mapped_md_object_id",
    "mapped_current_inventory",
    "mapping_state",
    "mapping_basis",
    "phash_distance",
    "dhash_distance",
    "visual_candidates_json",
    "extracted_review_path",
    "deletion_authorized",
)


class ReconcileError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ReconcileError(f"path escaped allowed root: {resolved}") from error
    return resolved


def inventory_tail(value: str) -> str:
    match = re.fullmatch(
        r"(?i)KS-[5-9]-((?:BZ|NB|WF|ZP)[_-]\d+(?:[-_][A-Z0-9]+)*)",
        value.strip(),
    )
    return match.group(1).casefold() if match else ""


def unique_rows(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["record_key"]: row for row in rows}


def build_indices(
    object_plan: Path, workbook_run: Path, batch_run: Path
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    exact_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    tail_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    with object_plan.open(encoding="utf-8-sig", newline="") as handle:
        objects = list(csv.DictReader(handle))
    by_key = {row["record_key"]: row for row in objects}
    by_md_id = {
        row["md_object_id"]: row for row in objects if row["md_object_id"]
    }
    for row in objects:
        values = (
            row["current_inventory"],
            row["historical_inventory"],
            *row["batch_alias_inventories"].split("; "),
        )
        for value in values:
            if not value:
                continue
            exact_rows[value.casefold()].append(row)
            tail = inventory_tail(value)
            if tail:
                tail_rows[tail].append(row)
    with (workbook_run / "shared-workbook-records.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for record in csv.DictReader(handle):
            if not record["inventory"] or not record["mapped_record_keys"]:
                continue
            for key in record["mapped_record_keys"].split("; "):
                row = by_key[key]
                exact_rows[record["inventory"].casefold()].append(row)
                tail = inventory_tail(record["inventory"])
                if tail:
                    tail_rows[tail].append(row)
    with (batch_run / "batch-records.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        for record in csv.DictReader(handle):
            object_id = record["mapped_md_object_id"]
            image_name = record["image_name"]
            if not object_id or not image_name or object_id not in by_md_id:
                continue
            alias = Path(image_name).stem
            row = by_md_id[object_id]
            exact_rows[alias.casefold()].append(row)
            tail = inventory_tail(alias)
            if tail:
                tail_rows[tail].append(row)
    exact = {
        key: next(iter(values.values()))
        for key, rows in exact_rows.items()
        if len(values := unique_rows(rows)) == 1
    }
    tails = {
        key: next(iter(values.values()))
        for key, rows in tail_rows.items()
        if len(values := unique_rows(rows)) == 1
    }
    return exact, tails


def extract_member(
    archive: zipfile.ZipFile, member: str, target: Path
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with archive.open(member) as source, temporary.open("xb") as destination:
        while block := source.read(8 * 1024 * 1024):
            destination.write(block)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, target)


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


def run(
    container_run: Path,
    workbook_run: Path,
    object_plan: Path,
    batch_run: Path,
    discovery: Path,
    previews: Path,
    output: Path,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    review_images = output / "review-images"
    exact, tails = build_indices(object_plan, workbook_run, batch_run)
    with object_plan.open(encoding="utf-8-sig", newline="") as handle:
        object_rows = {
            row["md_object_id"]: row
            for row in csv.DictReader(handle)
            if row["md_object_id"]
        }
    with (discovery / "md-objects.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        md_rows = {
            row["md_object_id"]: row for row in csv.DictReader(handle)
        }
    md_hashes: dict[str, tuple[int, int]] = {}
    for object_id in md_rows:
        preview = previews / f"md-{object_id}.jpg"
        if preview.is_file():
            md_hashes[object_id] = visual.image_hashes(preview)
    with (container_run / "members.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        members = [
            row for row in csv.DictReader(handle) if row["extension"] == ".jpg"
        ]
    container = Path(members[0]["source_container"])
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(container) as archive:
        for index, member in enumerate(members, 1):
            inventory = Path(member["member"]).stem
            mapped = exact.get(inventory.casefold())
            basis = "exact_known_inventory"
            if mapped is None:
                mapped = tails.get(inventory_tail(inventory))
                basis = "unique_inventory_tail_alias"
            state = "mapped_alias" if mapped else "visual_review_required"
            phash_distance = ""
            dhash_distance = ""
            candidates_json = ""
            extracted = ""
            if mapped is None:
                target = review_images / f"{index:04d}-{Path(member['member']).name}"
                extract_member(archive, member["member"], target)
                extracted = str(target)
                source_hashes = visual.image_hashes(target)
                candidates = []
                for object_id, hashes in md_hashes.items():
                    pdistance = visual.hamming(source_hashes[0], hashes[0])
                    ddistance = visual.hamming(source_hashes[1], hashes[1])
                    candidates.append(
                        {
                            "md_object_id": object_id,
                            "current_inventory": md_rows[object_id]["inventory"],
                            "record_key": object_rows[object_id]["record_key"],
                            "phash_distance": pdistance,
                            "dhash_distance": ddistance,
                            "combined_distance": pdistance + ddistance,
                        }
                    )
                candidates.sort(
                    key=lambda row: (
                        row["combined_distance"],
                        row["phash_distance"],
                        int(row["md_object_id"]),
                    )
                )
                best = candidates[0]
                second = candidates[1]
                phash_distance = str(best["phash_distance"])
                dhash_distance = str(best["dhash_distance"])
                candidates_json = json.dumps(
                    candidates[:5], ensure_ascii=False, sort_keys=True
                )
                if (
                    best["phash_distance"] <= 8
                    and best["dhash_distance"] <= 12
                    and second["combined_distance"] - best["combined_distance"] >= 4
                ):
                    mapped = object_rows[best["md_object_id"]]
                    state = "probable_visual_alias_review"
                    basis = "unique_bounded_perceptual_hash_candidate"
                else:
                    state = "unresolved_visual_review"
                    basis = "no_unique_bounded_perceptual_hash_candidate"
            rows.append(
                {
                    "source_container": str(container),
                    "member": member["member"],
                    "member_sha256": member["member_sha256"],
                    "historical_inventory": inventory,
                    "mapped_record_key": mapped["record_key"] if mapped else "",
                    "mapped_md_object_id": mapped["md_object_id"] if mapped else "",
                    "mapped_current_inventory": (
                        mapped["current_inventory"] if mapped else ""
                    ),
                    "mapping_state": state,
                    "mapping_basis": basis,
                    "phash_distance": phash_distance,
                    "dhash_distance": dhash_distance,
                    "visual_candidates_json": candidates_json,
                    "extracted_review_path": extracted,
                    "deletion_authorized": "false",
                }
            )
            if index % 250 == 0:
                print(f"members={index}/{len(members)}", flush=True)
    rows.sort(key=lambda row: row["member"])
    atomic_csv(output / "shared-image-reconciliation.csv", rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "shared_container_run": str(container_run),
        "member_images": len(rows),
        "mapping_state_counts": dict(Counter(row["mapping_state"] for row in rows)),
        "review_image_rows": sum(bool(row["extracted_review_path"]) for row in rows),
        "unresolved_rows": sum(
            row["mapping_state"] == "unresolved_visual_review" for row in rows
        ),
        "deletion_authorized_rows": 0,
    }
    atomic_json(output / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-run", type=Path, default=DEFAULT_CONTAINER_RUN)
    parser.add_argument("--workbook-run", type=Path, default=DEFAULT_WORKBOOK_RUN)
    parser.add_argument("--object-plan", type=Path, default=DEFAULT_OBJECT_PLAN)
    parser.add_argument("--batch-run", type=Path, default=DEFAULT_BATCH_RUN)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--previews", type=Path, default=DEFAULT_PREVIEWS)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = run(
            args.container_run.expanduser().resolve(),
            args.workbook_run.expanduser().resolve(),
            args.object_plan.expanduser().resolve(),
            args.batch_run.expanduser().resolve(),
            args.discovery.expanduser().resolve(),
            args.previews.expanduser().resolve(),
            output,
        )
    except (
        OSError,
        csv.Error,
        ValueError,
        zipfile.BadZipFile,
        ReconcileError,
    ) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
