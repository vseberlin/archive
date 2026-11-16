"""Read-only technical checks for proposed preservation masters."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class TechnicalVerificationError(RuntimeError):
    """Raised when a candidate cannot be verified without modification."""


def _run(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise TechnicalVerificationError(
            f"required verifier is unavailable: {arguments[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        raise TechnicalVerificationError(
            f"{arguments[0]} verification failed: "
            f"{detail or error.returncode}"
        ) from error
    return result.stdout


def verify_tiff_candidate(path: Path) -> dict[str, Any]:
    """Verify one TIFF structurally using read-only external probes."""

    path = path.expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise TechnicalVerificationError(
            "TIFF candidate must be one existing non-symlink file"
        )
    if path.suffix.casefold() not in {".tif", ".tiff"}:
        raise TechnicalVerificationError(
            "local master candidate must have a TIFF extension"
        )

    try:
        exif_rows = json.loads(
            _run(
                [
                    "exiftool",
                    "-j",
                    "-FileType",
                    "-ImageWidth",
                    "-ImageHeight",
                    "-BitsPerSample",
                    str(path),
                ]
            )
        )
    except json.JSONDecodeError as error:
        raise TechnicalVerificationError(
            "ExifTool returned invalid JSON"
        ) from error
    if len(exif_rows) != 1:
        raise TechnicalVerificationError(
            "ExifTool did not return one TIFF record"
        )
    exif = exif_rows[0]
    if exif.get("FileType") != "TIFF":
        raise TechnicalVerificationError(
            "candidate is not structurally identified as TIFF"
        )
    width = exif.get("ImageWidth")
    height = exif.get("ImageHeight")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
    ):
        raise TechnicalVerificationError(
            "TIFF dimensions are missing or invalid"
        )

    identify = _run(
        [
            "magick",
            "identify",
            "-regard-warnings",
            "-format",
            "%m|%w|%h|%n",
            str(path),
        ]
    ).strip()
    parts = identify.split("|")
    if len(parts) != 4 or parts[0] != "TIFF":
        raise TechnicalVerificationError(
            "ImageMagick did not identify one valid TIFF"
        )
    try:
        magick_width = int(parts[1])
        magick_height = int(parts[2])
        image_directories = int(parts[3])
    except ValueError as error:
        raise TechnicalVerificationError(
            "ImageMagick returned invalid TIFF dimensions"
        ) from error
    if (magick_width, magick_height) != (width, height):
        raise TechnicalVerificationError(
            "ExifTool and ImageMagick TIFF dimensions disagree"
        )
    if image_directories != 1:
        raise TechnicalVerificationError(
            "bounded master review requires one TIFF image directory"
        )
    return {
        "format": "TIFF",
        "width": width,
        "height": height,
        "bits_per_sample": exif.get("BitsPerSample"),
        "image_directories": image_directories,
        "probes": ["exiftool", "imagemagick_identify"],
    }


def verify_image_derivative(path: Path) -> dict[str, Any]:
    """Verify one captured comparison image without modifying it."""

    path = path.expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise TechnicalVerificationError(
            "captured derivative must be one existing non-symlink file"
        )
    identify = _run(
        [
            "magick",
            "identify",
            "-regard-warnings",
            "-format",
            "%m|%w|%h|%n",
            str(path),
        ]
    ).strip()
    parts = identify.split("|")
    if len(parts) != 4 or parts[0] not in {"JPEG", "PNG", "WEBP"}:
        raise TechnicalVerificationError(
            "captured derivative is not an accepted comparison image"
        )
    try:
        width = int(parts[1])
        height = int(parts[2])
        image_count = int(parts[3])
    except ValueError as error:
        raise TechnicalVerificationError(
            "ImageMagick returned invalid derivative dimensions"
        ) from error
    if width <= 0 or height <= 0 or image_count != 1:
        raise TechnicalVerificationError(
            "captured derivative dimensions or image count are invalid"
        )
    return {
        "format": parts[0],
        "width": width,
        "height": height,
        "image_count": image_count,
        "probe": "imagemagick_identify",
    }
