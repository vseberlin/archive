#!/usr/bin/env python3
"""Read-only visual-feature benchmark for missing-master candidate search."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image
from scipy.fft import dctn


TOOL_VERSION = "0.2.0"
REPORT_FORMAT = "iss-visual-feature-benchmark-v1"
IMAGE_SUFFIXES = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".webp"}
TIFF_SUFFIXES = {".tif", ".tiff"}
PROTECTED_SOURCE_ROOTS = (
    Path("/mnt/volume/archive"),
    Path("/mnt/volume/archive-intake"),
    Path("/media/vladimir"),
)
OUTPUT_ROOTS = (
    Path("/mnt/volume/archive-pilot-work/minimal-core-v2/work"),
    Path("/home/vladimir/archive-core-v2-preservation"),
)


class VisualIndexError(RuntimeError):
    """Raised when the visual benchmark cannot preserve its boundaries."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _under(path: Path, roots: Iterable[Path]) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    for root in roots:
        candidate = root.expanduser().resolve()
        try:
            resolved.relative_to(candidate)
        except ValueError:
            continue
        if resolved != candidate and resolved.is_file():
            return resolved
        if resolved.is_dir():
            return resolved
    raise VisualIndexError(f"path is outside the allowed roots: {resolved}")


def protected_source(path: Path) -> Path:
    return _under(path, PROTECTED_SOURCE_ROOTS)


def output_path(path: Path) -> Path:
    expanded = path.expanduser()
    parent = expanded.parent.resolve(strict=True)
    _under(parent, OUTPUT_ROOTS)
    destination = parent / expanded.name
    if destination.exists():
        raise VisualIndexError(
            f"refusing to replace visual benchmark output: {destination}"
        )
    return destination


def root_fingerprint(root: Path) -> dict[str, Any]:
    stat = root.stat()
    return {
        "path": str(root),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def walk_images(root: Path) -> tuple[list[dict[str, Any]], float, int]:
    """Walk once using scandir and retain only inexpensive stat evidence."""

    started = time.monotonic()
    images: list[dict[str, Any]] = []
    files_seen = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files_seen += 1
                            suffix = Path(entry.name).suffix.casefold()
                            if suffix in IMAGE_SUFFIXES:
                                stat = entry.stat(follow_symlinks=False)
                                images.append(
                                    {
                                        "path": entry.path,
                                        "relative_path": str(
                                            Path(entry.path).relative_to(root)
                                        ),
                                        "extension": suffix.lstrip("."),
                                        "size_bytes": stat.st_size,
                                        "mtime_ns": stat.st_mtime_ns,
                                    }
                                )
                    except OSError:
                        continue
        except OSError:
            continue
    return images, time.monotonic() - started, files_seen


def _spread(values: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count <= 0 or not values:
        return []
    ordered = sorted(
        values,
        key=lambda item: (int(item["size_bytes"]), str(item["relative_path"])),
    )
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indices = {
        round(index * (len(ordered) - 1) / (count - 1))
        for index in range(count)
    }
    return [ordered[index] for index in sorted(indices)]


def stratified_sample(
    images: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        raise VisualIndexError("benchmark limit must be positive")
    tiffs = [
        item
        for item in images
        if f".{item['extension']}" in TIFF_SUFFIXES
    ]
    others = [
        item
        for item in images
        if f".{item['extension']}" not in TIFF_SUFFIXES
    ]
    tiff_count = min(len(tiffs), max(1, limit // 2)) if tiffs else 0
    other_count = min(len(others), limit - tiff_count)
    if tiff_count + other_count < limit:
        tiff_count = min(len(tiffs), limit - other_count)
    selected = _spread(tiffs, tiff_count) + _spread(others, other_count)
    return sorted(selected, key=lambda item: str(item["relative_path"]))


def _bits_hex(bits: np.ndarray) -> str:
    value = 0
    for bit in bits.astype(bool).flat:
        value = (value << 1) | int(bit)
    width = (bits.size + 3) // 4
    return f"{value:0{width}x}"


def hamming_distance(first: str, second: str) -> int:
    if len(first) != len(second):
        raise VisualIndexError("perceptual hashes must have equal widths")
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError as error:
        raise VisualIndexError("perceptual hashes must be hexadecimal") from error


def feature_hashes(image: Image.Image) -> dict[str, str]:
    grayscale = image.convert("L")
    average = np.asarray(
        grayscale.resize((8, 8), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    ahash = _bits_hex(average > average.mean())
    difference = np.asarray(
        grayscale.resize((9, 8), Image.Resampling.LANCZOS),
        dtype=np.int16,
    )
    dhash = _bits_hex(difference[:, 1:] > difference[:, :-1])
    frequency = np.asarray(
        grayscale.resize((32, 32), Image.Resampling.LANCZOS),
        dtype=np.float32,
    )
    low = dctn(frequency, type=2, norm="ortho")[:8, :8]
    median = np.median(low.flat[1:])
    phash = _bits_hex(low > median)
    return {
        "ahash_64": ahash,
        "dhash_64": dhash,
        "phash_dct_64": phash,
    }


def normalized_preview(
    source: Path,
    cache: Path,
    timeout: float,
) -> tuple[Image.Image, float]:
    """Decode the first image into a bounded preview with libvips."""

    cache.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["TMPDIR"] = str(cache)
    output = cache / f"preview-{uuid.uuid4().hex}.pgm"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                "vipsthumbnail",
                str(source),
                "--size",
                "64x64",
                "--output",
                str(output),
                "--vips-concurrency=1",
                "--vips-disc-threshold=104857600",
                "--vips-cache-max=100",
                "--vips-cache-max-memory=268435456",
                "--vips-cache-max-files=32",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError as error:
        raise VisualIndexError("vipsthumbnail is required for visual indexing") from error
    except subprocess.TimeoutExpired as error:
        raise VisualIndexError(
            f"image normalization exceeded {timeout:.0f} seconds"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise VisualIndexError(
            f"image normalization failed: {detail or error.returncode}"
        ) from error
    elapsed = time.monotonic() - started
    try:
        if not output.is_file():
            raise VisualIndexError(
                "libvips did not create a normalized preview"
            )
        with Image.open(output) as decoded:
            image = decoded.convert("L")
            image.load()
    except Exception as error:
        raise VisualIndexError(
            "libvips returned an unreadable normalized preview"
        ) from error
    finally:
        output.unlink(missing_ok=True)
    return image, elapsed


def benchmark(
    source_root: Path,
    output: Path,
    *,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    root = protected_source(source_root)
    destination = output_path(output)
    before = root_fingerprint(root)
    images, walk_seconds, files_seen = walk_images(root)
    sample = stratified_sample(images, limit)
    cache = destination.parent / (
        f".{destination.stem}-{uuid.uuid4().hex}-magick-cache"
    )
    results: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(sample, 1):
            source = protected_source(Path(str(item["path"])))
            started = time.monotonic()
            try:
                preview, decode_seconds = normalized_preview(
                    source,
                    cache,
                    timeout,
                )
                hash_started = time.monotonic()
                hashes = feature_hashes(preview)
                hash_seconds = time.monotonic() - hash_started
                state = "ok"
                error = ""
            except (OSError, VisualIndexError) as exception:
                decode_seconds = 0.0
                hash_seconds = 0.0
                hashes = {}
                state = "error"
                error = str(exception)
            elapsed = time.monotonic() - started
            observed = source.stat()
            if (
                observed.st_size != int(item["size_bytes"])
                or observed.st_mtime_ns != int(item["mtime_ns"])
            ):
                raise VisualIndexError(
                    f"source changed during benchmark: {source}"
                )
            results.append(
                {
                    **item,
                    "state": state,
                    "error": error,
                    "decode_seconds": round(decode_seconds, 6),
                    "hash_seconds": round(hash_seconds, 6),
                    "total_seconds": round(elapsed, 6),
                    "features": hashes,
                }
            )
            print(
                f"visual-index: {index}/{len(sample)} "
                f"{item['extension']} {item['size_bytes']} "
                f"{elapsed:.3f}s {state}",
                flush=True,
            )
    finally:
        if cache.is_dir():
            for child in cache.iterdir():
                child.unlink(missing_ok=True)
            cache.rmdir()
    after = root_fingerprint(root)
    if after != before:
        raise VisualIndexError(
            "source-root fingerprint changed during visual benchmark"
        )
    successful = [item for item in results if item["state"] == "ok"]
    by_extension: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for item in images:
        extension = str(item["extension"])
        counts[extension] = counts.get(extension, 0) + 1
    projected_seconds = 0.0
    for extension, count in sorted(counts.items()):
        timings = [
            float(item["total_seconds"])
            for item in successful
            if item["extension"] == extension
        ]
        if not timings:
            continue
        mean = statistics.fmean(timings)
        projected_seconds += mean * count
        by_extension[extension] = {
            "inventory_count": count,
            "sample_count": len(timings),
            "mean_seconds": round(mean, 6),
            "median_seconds": round(statistics.median(timings), 6),
            "projected_serial_seconds": round(mean * count, 3),
        }
    report = {
        "format": REPORT_FORMAT,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now(),
        "source_root": before,
        "protections": {
            "source_access": "read_only_by_application_contract",
            "source_root_unchanged": True,
            "output": str(destination),
        },
        "walk": {
            "files_seen": files_seen,
            "images_seen": len(images),
            "seconds": round(walk_seconds, 6),
        },
        "sample": {
            "requested": limit,
            "selected": len(sample),
            "successful": len(successful),
            "errors": len(results) - len(successful),
            "by_extension": by_extension,
            "projected_serial_seconds": round(projected_seconds, 3),
        },
        "extractor": {
            "normalization": (
                "libvips-8.16-first-image-auto-orient-max-64x64-"
                "single-thread"
            ),
            "features": [
                "ahash-64-v2",
                "dhash-64-v2",
                "phash-dct-64-v2",
            ],
            "timeout_seconds": timeout,
        },
        "results": results,
    }
    encoded = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    except FileExistsError as error:
        raise VisualIndexError(
            f"refusing to replace visual benchmark output: {destination}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)
    report["report_sha256"] = hashlib.sha256(encoded).hexdigest()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = benchmark(
            args.source_root,
            args.output,
            limit=args.limit,
            timeout=args.timeout,
        )
    except (OSError, VisualIndexError) as error:
        raise SystemExit(f"error: {error}") from error
    print(
        json.dumps(
            {
                "result": "ok",
                "output": result["protections"]["output"],
                "walk": result["walk"],
                "sample": result["sample"],
                "report_sha256": result["report_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
