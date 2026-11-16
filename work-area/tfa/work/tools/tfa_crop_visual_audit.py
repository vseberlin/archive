#!/usr/bin/env python3
"""Verify cropped local TIFF derivatives against their MD display images.

Filename numbers only route plausible comparisons.  A path is accepted as
redundant only when crop-aware pHash and dHash evidence uniquely identifies a
museum-digital record that already has one selected TIFF Master.  Source files
remain read-only and every output stays in the existing TFA review root.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageFile, ImageOps
from scipy.fft import dctn


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
REVIEW_ROOT = Path("/mnt/volume/iss-archive-work/tfa/work/reconciliation/unit-reconciliation")
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
NEW_TIFF_CSV = REVIEW_ROOT / "visual-only-new-tiff-candidates.csv"
MD_VISUAL_CSV = REVIEW_ROOT / "institution-md-visual-references.csv"
INSTITUTION_BINDINGS_CSV = REVIEW_ROOT / "visual-only-institution-master-bindings.csv"
OUTPUT = REVIEW_ROOT / "visual-only-crop-verified-redundant.csv"
RECEIPT = REVIEW_ROOT / "visual-only-crop-audit.json"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
PHASH_LIMIT = 11
DHASH_LIMIT = 12
FIELDS = (
    "source_path", "visual_group_id", "asset_id", "filename", "extension", "size_bytes", "mtime_ns",
    "width_px", "height_px", "matched_md_object_id", "matched_md_inventory", "selected_master_path",
    "md_display_url", "crop_phash_distance", "crop_dhash_distance", "crop_distance_total",
    "crop_width_fraction", "crop_height_fraction", "crop_horizontal", "crop_vertical",
    "disposition_basis", "candidate_routing", "deletion_authorized",
)


class CropAuditError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def source_mount() -> dict[str, Any]:
    import subprocess

    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    values = json.loads(result.stdout).get("filesystems") or []
    if len(values) != 1:
        raise CropAuditError("Elements did not resolve to one mount")
    mount = values[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise CropAuditError(f"Elements read-only guard failed: {mount}")
    return mount


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.parent.resolve(strict=True) != REVIEW_ROOT.resolve(strict=True):
        raise CropAuditError(f"output escaped review root: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: row["source_path"]))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def numeric_tokens(value: str) -> set[str]:
    """Return full object tokens for routing, preserving sequence suffixes."""

    stem = Path(value).stem.casefold()
    return {
        match.group(0).replace("_", "-")
        for match in re.finditer(r"\d{5,}(?:[-_]?[a-z])?(?:[-_]\d+)?", stem)
    }


def image_hashes(image: Image.Image) -> tuple[int, int]:
    grayscale = ImageOps.autocontrast(image.convert("L"))
    frequency = np.asarray(grayscale.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    low = dctn(frequency, type=2, norm="ortho")[:8, :8].flatten()[1:]
    phash = 0
    for bit in low >= np.median(low):
        phash = (phash << 1) | int(bit)
    difference = np.asarray(grayscale.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    dhash = 0
    for bit in (difference[:, 1:] > difference[:, :-1]).flat:
        dhash = (dhash << 1) | int(bit)
    return phash, dhash


def crop_distance(query_phash: int, query_dhash: int, image: Image.Image) -> dict[str, Any]:
    master = ImageOps.exif_transpose(image).convert("L")
    width, height = master.size
    best: tuple[int, int, int, float, float, float, float] | None = None
    for width_fraction in (1.0, 0.9, 0.8, 0.7):
        for height_fraction in (1.0, 0.9, 0.8, 0.7):
            crop_width = round(width * width_fraction)
            crop_height = round(height * height_fraction)
            for horizontal in (0.0, 0.5, 1.0):
                for vertical in (0.0, 0.5, 1.0):
                    left = round((width - crop_width) * horizontal)
                    top = round((height - crop_height) * vertical)
                    phash, dhash = image_hashes(master.crop((left, top, left + crop_width, top + crop_height)))
                    pd = (query_phash ^ phash).bit_count()
                    dd = (query_dhash ^ dhash).bit_count()
                    value = (pd + dd, pd, dd, width_fraction, height_fraction, horizontal, vertical)
                    if best is None or value < best:
                        best = value
    assert best is not None
    return {
        "crop_distance_total": best[0], "crop_phash_distance": best[1], "crop_dhash_distance": best[2],
        "crop_width_fraction": best[3], "crop_height_fraction": best[4],
        "crop_horizontal": best[5], "crop_vertical": best[6],
    }


def selected_records() -> list[dict[str, str]]:
    visuals = {row["md_object_id"]: row for row in read_csv(MD_VISUAL_CSV)}
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row) for row in connection.execute(
                """SELECT o.md_object_id,o.inventory,c.source_path AS selected_master_path
                   FROM reconciliation_decisions d
                   JOIN objects o ON o.record_key=d.record_key
                   JOIN candidates c ON c.candidate_id=d.selected_candidate_id
                   WHERE d.decision_state='selected_master'"""
            )
        ]
    finally:
        connection.close()
    for binding in read_csv(INSTITUTION_BINDINGS_CSV):
        rows.append({
            "md_object_id": binding["md_object_id"], "inventory": binding["md_inventory"],
            "selected_master_path": binding["selected_master_path"],
        })
    result = []
    for row in rows:
        visual = visuals.get(str(row["md_object_id"]))
        if visual is not None:
            result.append({**row, "md_display_url": visual["image_url"]})
    return result


def download_image(url: str) -> Image.Image:
    error = ""
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "ISS-TFA-crop-visual-audit/1.0"})
            with urlopen(request, timeout=90) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise CropAuditError("MD display image exceeds 100 MiB")
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(BytesIO(data)) as opened:
                return ImageOps.exif_transpose(opened).copy()
        except Exception as exception:
            error = f"{type(exception).__name__}: {exception}"
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise CropAuditError(error)


def evaluate_group(task: tuple[list[dict[str, str]], list[dict[str, str]]]) -> tuple[list[dict[str, Any]], str]:
    members, references = task
    representative = members[0]
    query_phash = int(representative["phash"], 16) & ((1 << 63) - 1)
    query_dhash = int(representative["dhash"], 16)
    results = []
    for reference in references:
        image = download_image(reference["md_display_url"])
        distance = crop_distance(query_phash, query_dhash, image)
        results.append((distance["crop_distance_total"], distance["crop_phash_distance"], distance["crop_dhash_distance"], reference, distance))
    qualified = [value for value in results if value[1] <= PHASH_LIMIT and value[2] <= DHASH_LIMIT]
    if not qualified:
        return [], "no_crop_match"
    qualified.sort(key=lambda value: (value[0], value[1], value[2], int(value[3]["md_object_id"])))
    best = qualified[0]
    tied_ids = {value[3]["md_object_id"] for value in qualified if value[:3] == best[:3]}
    if len(tied_ids) != 1:
        return [], "ambiguous_crop_match"
    _, _, _, reference, distance = best
    output = []
    for member in members:
        output.append({
            **member,
            "matched_md_object_id": reference["md_object_id"],
            "matched_md_inventory": reference["inventory"],
            "selected_master_path": reference["selected_master_path"],
            "md_display_url": reference["md_display_url"],
            **distance,
            "disposition_basis": "unique_crop_visual_match_to_selected_md_tiff_master",
            "candidate_routing": "filename_numeric_token_only; not matching evidence",
            "deletion_authorized": "false",
        })
    return output, "matched"


def run(workers: int) -> dict[str, Any]:
    mount = source_mount()
    new_rows = read_csv(NEW_TIFF_CSV)
    prior = read_csv(OUTPUT) if OUTPUT.is_file() else []
    selected = selected_records()
    token_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in selected:
        for token in numeric_tokens(row["inventory"] + " " + Path(row["selected_master_path"]).stem):
            token_index[token].append(row)
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in new_rows:
        groups[row["visual_group_id"]].append(row)
    tasks = []
    routed_paths = set()
    for members in groups.values():
        references = {}
        for member in members:
            for token in numeric_tokens(member["filename"]):
                for reference in token_index.get(token, []):
                    references[reference["md_object_id"]] = reference
        if references:
            tasks.append((members, list(references.values())))
            routed_paths.update(member["source_path"] for member in members)
    retained_prior = [row for row in prior if row["source_path"] not in {item["source_path"] for item in new_rows}]
    matched = list(retained_prior)
    outcomes: defaultdict[str, int] = defaultdict(int)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(evaluate_group, task): task for task in tasks}
        for future in as_completed(futures):
            rows, outcome = future.result()
            matched.extend(rows)
            outcomes[outcome] += 1
            completed += 1
            if completed % 100 == 0 or completed == len(tasks):
                print(f"crop-audit: {completed}/{len(tasks)} matched_paths={len(matched)}", flush=True)
    atomic_csv(OUTPUT, matched)
    receipt = {
        "format": "iss-tfa-crop-visual-audit-v1", "version": VERSION, "result": "ok",
        "completed_at": utc_now(), "source_mount": mount, "source_read_only": True,
        "source_files_modified": False, "deletion_authorized": False,
        "candidate_paths_considered": len(new_rows), "candidate_groups_routed": len(tasks),
        "candidate_paths_routed": len(routed_paths), "crop_verified_redundant_paths": len(matched),
        "outcomes": dict(outcomes), "phash_limit": PHASH_LIMIT, "dhash_limit": DHASH_LIMIT,
        "filename_used_for_candidate_routing": True, "filename_used_as_matching_evidence": False,
        "metadata_used_for_matching": False, "output": str(OUTPUT),
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def refilter_existing() -> dict[str, Any]:
    source_mount()
    if not OUTPUT.is_file() or not RECEIPT.is_file():
        raise CropAuditError("completed crop evidence is required for refiltering")
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if receipt.get("result") != "ok":
        raise CropAuditError("crop evidence receipt is incomplete")
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_csv(OUTPUT):
        groups[row["visual_group_id"]].append(row)
    retained = []
    for members in groups.values():
        reference = members[0]
        reference_tokens = numeric_tokens(reference["matched_md_inventory"]) | numeric_tokens(
            reference["selected_master_path"]
        )
        group_tokens = set().union(*(numeric_tokens(row["filename"]) for row in members))
        if not reference_tokens & group_tokens:
            continue
        retained.extend(
            row for row in members
            if int(row["crop_phash_distance"]) <= PHASH_LIMIT
            and int(row["crop_dhash_distance"]) <= DHASH_LIMIT
        )
    retained_groups = len({row["visual_group_id"] for row in retained})
    prior_outcomes = receipt.get("outcomes") or {}
    prior_matched_groups = int(prior_outcomes.get("matched", retained_groups))
    prior_no_match_groups = int(prior_outcomes.get("no_crop_match", 0))
    threshold_rejected_groups = max(0, prior_matched_groups - retained_groups)
    atomic_csv(OUTPUT, retained)
    receipt.update({
        "completed_at": utc_now(),
        "crop_verified_redundant_paths": len(retained),
        "phash_limit": PHASH_LIMIT,
        "dhash_limit": DHASH_LIMIT,
        "full_object_token_required_for_candidate_routing": True,
        "outcomes": {
            "matched": retained_groups,
            "no_crop_match": prior_no_match_groups + threshold_rejected_groups,
        },
        "threshold_refiltered_from_completed_visual_evidence": True,
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
    })
    atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--refilter-existing", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        raise SystemExit("workers must be between 1 and 16")
    result = refilter_existing() if args.refilter_existing else run(args.workers)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
