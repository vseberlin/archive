#!/usr/bin/env python3
"""Visually compare current museum-digital objects with curated USB JPEGs.

The tool downloads bounded 1200-pixel IIIF previews into the writable work
root, computes perceptual hashes, and records review candidates.  It never
modifies the read-only USB and does not publish or delete anything.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
from PIL import Image, ImageOps


FORMAT = "iss-kurt-schwarz-visual-verification-v1"
SOURCE_MOUNT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/kurt-schwarz")
DEFAULT_DISCOVERY = WORK_ROOT / "work/discovery/runs/20260729T190000Z-kurt-schwarz-discovery"
CURATED_ROOT = (
    SOURCE_MOUNT / "restore/archive/WF/Kurt Schwarz/Alle-KS-Bilder_5000px"
)
FIELDS = (
    "md_object_id",
    "current_inventory",
    "historical_inventory",
    "md_preview_path",
    "md_preview_sha256",
    "local_jpeg_path",
    "local_jpeg_sha256",
    "phash_distance",
    "dhash_distance",
    "visual_state",
    "review_reason",
)


class VerifyError(RuntimeError):
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
        raise VerifyError(f"path escaped allowed root: {resolved}") from error
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def dct_matrix(size: int) -> np.ndarray:
    x = np.arange(size)
    u = np.arange(size)[:, None]
    matrix = np.cos((2 * x + 1) * u * np.pi / (2 * size))
    matrix[0, :] *= 1 / np.sqrt(2)
    return matrix * np.sqrt(2 / size)


DCT_32 = dct_matrix(32)


def image_hashes(path: Path) -> tuple[int, int]:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("L")
        phash_image = image.resize((32, 32), Image.Resampling.LANCZOS)
        pixels = np.asarray(phash_image, dtype=np.float64)
        coefficients = DCT_32 @ pixels @ DCT_32.T
        low = coefficients[:8, :8].copy()
        values = low.flatten()[1:]
        median = float(np.median(values))
        phash = 0
        for bit in values >= median:
            phash = (phash << 1) | int(bit)

        dhash_image = image.resize((9, 8), Image.Resampling.LANCZOS)
        dhash_pixels = np.asarray(dhash_image, dtype=np.int16)
        dhash = 0
        for bit in (dhash_pixels[:, 1:] > dhash_pixels[:, :-1]).flatten():
            dhash = (dhash << 1) | int(bit)
    return phash, dhash


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def visual_state(phash_distance: int, dhash_distance: int) -> tuple[str, str]:
    if phash_distance <= 8 and dhash_distance <= 12:
        return "confirmed_visual_match", "bounded_perceptual_hash_agreement"
    if phash_distance <= 14 and dhash_distance <= 20:
        return "probable_visual_match_review", "moderate_perceptual_hash_distance"
    return "visual_mismatch_review", "large_perceptual_hash_distance"


def download_preview(url: str, target: Path) -> None:
    if target.is_file() and target.stat().st_size:
        return
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    payload = b""
    last_error = ""
    for attempt in range(10):
        response = requests.get(
            url,
            headers={"User-Agent": "Industriesalon-archive-reconciliation/1.0"},
            timeout=(20, 180),
        )
        if response.status_code in {429, 502, 503, 504}:
            last_error = f"HTTP {response.status_code}"
            retry_after = response.headers.get("Retry-After", "")
            delay = (
                int(retry_after)
                if retry_after.isdigit()
                else min(30, 2 + attempt * 2)
            )
            time.sleep(delay)
            continue
        response.raise_for_status()
        payload = response.content
        time.sleep(0.4)
        break
    if not payload:
        raise VerifyError(f"IIIF preview retries exhausted ({last_error}): {url}")
    if not payload.startswith(b"\xff\xd8"):
        raise VerifyError(f"IIIF response is not JPEG: {url}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def load_rows(discovery: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    with (discovery / "md-objects.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        md = list(csv.DictReader(handle))
    with (discovery / "local-catalog.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        local = {
            row["historical_md_object_id"]: row["inventory"]
            for row in csv.DictReader(handle)
        }
    return md, local


def build(discovery: Path, output: Path, workers: int) -> dict[str, Any]:
    if not root_is_read_only(SOURCE_MOUNT):
        raise VerifyError(f"USB source is not read-only: {SOURCE_MOUNT}")
    output.mkdir(parents=True, exist_ok=True)
    previews = output / "previews"
    previews.mkdir(exist_ok=True)
    result_path = output / "visual-verification.csv"
    if result_path.exists():
        raise VerifyError(f"refusing to overwrite completed evidence: {result_path}")

    md_rows, historical_inventory = load_rows(discovery)
    download_errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {}
        for row in md_rows:
            target = previews / f"md-{row['md_object_id']}.jpg"
            preview_url = (
                row.get("image_asset_preview_url")
                or row["image_preview_url"]
            )
            futures[
                executor.submit(download_preview, preview_url, target)
            ] = (row, target, preview_url)
        for index, future in enumerate(as_completed(futures), 1):
            row, _, preview_url = futures[future]
            try:
                future.result()
            except (OSError, requests.RequestException, VerifyError) as error:
                download_errors.append(
                    {
                        "md_object_id": row["md_object_id"],
                        "url": preview_url,
                        "error": f"{type(error).__name__}: {str(error)[:2000]}",
                    }
                )
            if index % 200 == 0:
                print(
                    f"previews={index}/{len(md_rows)} errors={len(download_errors)}",
                    flush=True,
                )

    curated = {
        path.stem.casefold(): path
        for path in CURATED_ROOT.iterdir()
        if path.is_file() and path.suffix.casefold() in {".jpg", ".jpeg"}
    }
    rows: list[dict[str, str]] = []
    for index, md in enumerate(md_rows, 1):
        object_id = md["md_object_id"]
        current = md["inventory"]
        historical = historical_inventory.get(object_id, "")
        local = curated.get(current.casefold()) or curated.get(historical.casefold())
        preview = previews / f"md-{object_id}.jpg"
        base = {
            "md_object_id": object_id,
            "current_inventory": current,
            "historical_inventory": historical,
            "md_preview_path": str(preview) if preview.is_file() else "",
            "md_preview_sha256": sha256_file(preview) if preview.is_file() else "",
            "local_jpeg_path": str(local) if local else "",
            "local_jpeg_sha256": sha256_file(local) if local else "",
            "phash_distance": "",
            "dhash_distance": "",
            "visual_state": "",
            "review_reason": "",
        }
        if not preview.is_file():
            base["visual_state"] = "preview_download_failed"
            base["review_reason"] = "no_md_preview"
        elif not local:
            base["visual_state"] = "no_curated_local_jpeg"
            base["review_reason"] = "md_object_without_curated_usb_jpeg"
        else:
            md_phash, md_dhash = image_hashes(preview)
            local_phash, local_dhash = image_hashes(local)
            pdistance = hamming(md_phash, local_phash)
            ddistance = hamming(md_dhash, local_dhash)
            state, reason = visual_state(pdistance, ddistance)
            base["phash_distance"] = str(pdistance)
            base["dhash_distance"] = str(ddistance)
            base["visual_state"] = state
            base["review_reason"] = reason
        rows.append(base)
        if index % 200 == 0:
            print(f"compared={index}/{len(md_rows)}", flush=True)

    with result_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    with (output / "download-errors.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("md_object_id", "url", "error"),
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(download_errors)
        handle.flush()
        os.fsync(handle.fileno())

    state_counts: dict[str, int] = {}
    for row in rows:
        state_counts[row["visual_state"]] = state_counts.get(row["visual_state"], 0) + 1
    unmatched_local = sorted(
        path for stem, path in curated.items()
        if stem not in {
            row["inventory"].casefold() for row in md_rows
        }
        and stem not in {
            value.casefold() for value in historical_inventory.values()
        }
    )
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "discovery_run": str(discovery),
        "md_objects": len(md_rows),
        "curated_local_jpegs": len(curated),
        "state_counts": dict(sorted(state_counts.items())),
        "download_errors": len(download_errors),
        "unmatched_curated_local_jpegs": [str(path) for path in unmatched_local],
        "source_read_only": True,
        "deletion_authorized_rows": 0,
    }
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(args.discovery.expanduser().resolve(), output, args.workers)
    except (VerifyError, OSError, csv.Error, ValueError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
