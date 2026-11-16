#!/usr/bin/env python3
"""Build a resumable visual-hash CSV for institution MD display images.

This is a rebuildable comparison index, not archival metadata.  It reads the
completed 11,170-record museum-digital capture and downloads the main display
image for every image-bearing institution record.  For reconciled records the
display image is secondary visual evidence for crops and tonal derivatives;
the selected TIFF remains the single Master.  Errors are recorded and
processing continues.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image, ImageFile, ImageOps
from scipy.fft import dctn


VERSION = "1.1.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
CAPTURE_ROOT = WORK_ROOT / "work/reconciliation/museum-digital/runs/20260814T201913Z-tfa-institution-29-clean-local-only-recheck"
OUTPUT = REVIEW_ROOT / "institution-md-visual-references.csv"
ERRORS = REVIEW_ROOT / "institution-md-visual-errors.csv"
RECEIPT = REVIEW_ROOT / "institution-md-visual-index.json"
EXPECTED_OBJECTS = 11170
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
FIELDS = (
    "md_object_id", "md_inventory", "md_title", "md_url", "image_url", "image_width_px", "image_height_px",
    "download_bytes", "ahash", "dhash", "phash", "status", "error", "indexed_at",
)


class InstitutionVisualError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def source_mount() -> dict[str, Any]:
    import subprocess

    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise InstitutionVisualError("Elements did not resolve to one mount")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise InstitutionVisualError(f"Elements read-only guard failed: {mount}")
    return mount


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.parent.resolve(strict=True) != REVIEW_ROOT.resolve(strict=True):
        raise InstitutionVisualError(f"output escaped review root: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: int(row["md_object_id"])))
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


def bits_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.astype(bool).flat:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hashes(image: Image.Image) -> tuple[str, str, str]:
    grayscale = image.convert("L")
    average = np.asarray(grayscale.resize((8, 8), Image.Resampling.LANCZOS), dtype=np.float32)
    difference = np.asarray(grayscale.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    frequency = np.asarray(grayscale.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    low = dctn(frequency, type=2, norm="ortho")[:8, :8]
    return (
        bits_hex(average > average.mean()),
        bits_hex(difference[:, 1:] > difference[:, :-1]),
        bits_hex(low > np.median(low.flat[1:])),
    )


def image_url(item: dict[str, Any]) -> str:
    images = item.get("object_images") if isinstance(item.get("object_images"), list) else []
    images = [
        row for row in images
        if isinstance(row, dict)
        and str(row.get("type") or "image").casefold() == "image"
        and Path(clean(row.get("filename_loc"))).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
    ]
    if not images:
        return ""
    image = sorted(
        images,
        key=lambda row: (str(row.get("is_main")) != "j", int(row.get("order") or 999)),
    )[0]
    folder = clean(image.get("folder")).strip("/")
    filename = clean(image.get("filename_loc"))
    return f"https://berlin.museum-digital.de/data/berlin/{folder}/{filename}" if folder and filename else ""


def load_capture() -> dict[str, dict[str, Any]]:
    coverage = json.loads((CAPTURE_ROOT / "coverage.json").read_text(encoding="utf-8"))
    if (
        coverage.get("format") != "iss-md-export-discovery-v1"
        or int(coverage.get("captured_objects", -1)) != EXPECTED_OBJECTS
        or int(coverage.get("request_failures", -1)) != 0
    ):
        raise InstitutionVisualError("museum-digital capture coverage is incomplete")
    objects: dict[str, dict[str, Any]] = {}
    for path in sorted((CAPTURE_ROOT / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("results", {}).get("objects", {})
        iterable = values.values() if isinstance(values, dict) else values if isinstance(values, list) else []
        for item in iterable:
            md_id = clean(item.get("object_id"))
            if md_id:
                objects[md_id] = item
    if len(objects) != EXPECTED_OBJECTS:
        raise InstitutionVisualError(f"capture object count drifted: {len(objects)}")
    return objects


def selected_md_ids() -> set[str]:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT o.md_object_id FROM objects o
                JOIN reconciliation_decisions d ON d.record_key=o.record_key
                WHERE o.record_state='current_museum_digital' AND d.decision_state='selected_master'
                """
            )
        }
    finally:
        connection.close()


def prior_rows() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for path in (OUTPUT, ERRORS):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rows[str(row["md_object_id"])] = dict(row)
    return rows


def index_one(base: dict[str, Any]) -> dict[str, Any]:
    url = str(base["image_url"])
    if not url:
        return {**base, "status": "error", "error": "museum-digital record has no display image", "indexed_at": utc_now()}
    error = ""
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "ISS-TFA-visual-reconciliation/2.0"})
            with urlopen(request, timeout=90) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise InstitutionVisualError("display image exceeds 100 MiB bound")
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with Image.open(BytesIO(data)) as opened:
                image = ImageOps.exif_transpose(opened)
                width, height = int(image.width), int(image.height)
                ahash, dhash, phash = hashes(image)
            return {
                **base,
                "image_width_px": width,
                "image_height_px": height,
                "download_bytes": len(data),
                "ahash": ahash,
                "dhash": dhash,
                "phash": phash,
                "status": "indexed",
                "error": "",
                "indexed_at": utc_now(),
            }
        except Exception as exception:
            error = clean(f"{type(exception).__name__}: {exception}")[:1000]
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return {**base, "status": "error", "error": error, "indexed_at": utc_now()}


def run(workers: int) -> dict[str, Any]:
    mount = source_mount()
    objects = load_capture()
    selected = selected_md_ids()
    existing = prior_rows()
    requested: list[dict[str, Any]] = []
    for md_id, item in objects.items():
        url = image_url(item)
        prior = existing.get(md_id)
        if prior and prior.get("status") == "indexed" and prior.get("image_url") == url:
            continue
        requested.append({
            "md_object_id": md_id,
            "md_inventory": clean(item.get("object_inventory_number")),
            "md_title": clean(item.get("object_name")),
            "md_url": f"https://berlin.museum-digital.de/object/{md_id}",
            "image_url": url,
            "image_width_px": "",
            "image_height_px": "",
            "download_bytes": "",
            "ahash": "",
            "dhash": "",
            "phash": "",
            "status": "pending",
            "error": "",
            "indexed_at": "",
        })
    print(
        f"institution-visual: objects={len(objects)} selected_tiff_included={len(selected)} "
        f"cached={len(existing)} remaining={len(requested)}",
        flush=True,
    )
    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_rows = {executor.submit(index_one, row): row for row in requested}
        for future in as_completed(future_rows):
            row = future.result()
            existing[str(row["md_object_id"])] = row
            completed_count += 1
            if completed_count % 100 == 0 or completed_count == len(requested):
                good = sum(value.get("status") == "indexed" for value in existing.values())
                errors = sum(value.get("status") == "error" for value in existing.values())
                atomic_csv(OUTPUT, [value for value in existing.values() if value.get("status") == "indexed"])
                atomic_csv(ERRORS, [value for value in existing.values() if value.get("status") != "indexed"])
                print(
                    f"institution-visual: {completed_count}/{len(requested)} indexed={good} errors={errors}",
                    flush=True,
                )
    indexed = [value for value in existing.values() if value.get("status") == "indexed"]
    errors = [value for value in existing.values() if value.get("status") != "indexed"]
    atomic_csv(OUTPUT, indexed)
    atomic_csv(ERRORS, errors)
    receipt = {
        "format": "iss-tfa-institution-visual-index-v1",
        "version": VERSION,
        "result": "ok",
        "completed_at": utc_now(),
        "institution_objects": len(objects),
        "selected_tiff_records_included": len(selected),
        "display_images_indexed": len(indexed),
        "display_image_errors": len(errors),
        "metadata_used_for_visual_matching": False,
        "source_mount": mount,
        "source_read_only": True,
        "source_files_modified": False,
        "visual_hashes_are_rebuildable_index": True,
        "output": str(OUTPUT),
        "errors": str(ERRORS),
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        raise SystemExit("workers must be between 1 and 16")
    print(json.dumps(run(args.workers), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
