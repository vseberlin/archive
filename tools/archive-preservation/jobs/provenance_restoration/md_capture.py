#!/usr/bin/env python3
"""Capture one embedded museum-digital image into a new enriched draft."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

if __package__:
    from . import bundle as bundle_contract
    from .evidence import file_digest, file_fingerprint
    from .review import ProvenanceReviewError, verify_evidence
    from .safety import ProtectedEvidenceError, require_safe_output
    from .technical import TechnicalVerificationError, verify_image_derivative
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bundle as bundle_contract
    from evidence import file_digest, file_fingerprint
    from review import ProvenanceReviewError, verify_evidence
    from safety import ProtectedEvidenceError, require_safe_output
    from technical import TechnicalVerificationError, verify_image_derivative


ASSET_ORIGIN = "https://asset.museum-digital.org"
ASSET_PREFIX = "/berlin/"
USER_AGENT = "ISSProvenanceRestoration/1 (+one-way evidence capture)"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
ALLOWED_MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAIN_VALUES = {"1", "j", "ja", "true", "y", "yes"}


class MuseumDigitalCaptureError(RuntimeError):
    """Raised when MD evidence cannot be captured truthfully."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _safe_asset_url(image: dict[str, Any]) -> str:
    folder_text = str(image.get("folder") or "").strip("/")
    filename = str(image.get("filename_loc") or "").strip()
    folder = PurePosixPath(folder_text)
    if (
        not folder_text
        or folder.is_absolute()
        or ".." in folder.parts
        or not filename
        or PurePosixPath(filename).name != filename
    ):
        raise MuseumDigitalCaptureError(
            "cached MD image row has an unsafe or incomplete locator"
        )
    encoded_folder = "/".join(
        urllib.parse.quote(part, safe="")
        for part in folder.parts
    )
    encoded_filename = urllib.parse.quote(filename, safe="")
    return (
        f"{ASSET_ORIGIN}{ASSET_PREFIX}"
        f"{encoded_folder}/{encoded_filename}"
    )


def select_image(
    draft: dict[str, Any],
    quell_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    md_sources = [
        source
        for source in draft["sources"]
        if source["kind"] == "museum_digital_snapshot"
    ]
    if len(md_sources) != 1:
        raise MuseumDigitalCaptureError(
            "capture requires exactly one cached MD object snapshot"
        )
    images = md_sources[0].get("snapshot", {}).get("images", [])
    if not isinstance(images, list):
        raise MuseumDigitalCaptureError(
            "cached MD image snapshot is invalid"
        )
    candidates = [
        image
        for image in images
        if isinstance(image, dict)
        and (
            not quell_id
            or str(image.get("quell_id") or "") == quell_id
        )
    ]
    if quell_id:
        if len(candidates) != 1:
            raise MuseumDigitalCaptureError(
                f"expected one cached MD image for quell_id={quell_id}"
            )
        return md_sources[0], candidates[0]
    mains = [
        image
        for image in candidates
        if str(image.get("is_main") or "").strip().casefold()
        in MAIN_VALUES
    ]
    if len(mains) == 1:
        return md_sources[0], mains[0]
    if not mains and len(candidates) == 1:
        return md_sources[0], candidates[0]
    raise MuseumDigitalCaptureError(
        "cached MD image selection is missing or ambiguous"
    )


def fetch_asset(url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/jpeg,image/png,image/webp",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            final_url = response.geturl()
            parsed = urllib.parse.urlsplit(final_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "asset.museum-digital.org"
                or not parsed.path.startswith(ASSET_PREFIX)
            ):
                raise MuseumDigitalCaptureError(
                    "MD image redirected outside the trusted asset boundary"
                )
            if status != 200:
                raise MuseumDigitalCaptureError(
                    f"MD image returned HTTP {status}"
                )
            content_type = (
                response.headers.get_content_type().casefold()
            )
            if content_type not in ALLOWED_MEDIA_TYPES:
                raise MuseumDigitalCaptureError(
                    f"MD image returned unsupported media type: {content_type}"
                )
            declared_length = int(
                response.headers.get("Content-Length", "0") or 0
            )
            if declared_length > MAX_IMAGE_BYTES:
                raise MuseumDigitalCaptureError(
                    "MD image exceeds the capture size limit"
                )
            body = bytearray()
            while chunk := response.read(1024 * 1024):
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    raise MuseumDigitalCaptureError(
                        "MD image exceeds the capture size limit"
                    )
            if not body:
                raise MuseumDigitalCaptureError(
                    "MD image response is empty"
                )
            return {
                "body": bytes(body),
                "http_status": status,
                "requested_url": url,
                "final_url": final_url,
                "media_type": content_type,
                "content_length_header": declared_length,
                "last_modified": str(
                    response.headers.get("Last-Modified", "")
                ),
                "etag": str(response.headers.get("ETag", "")),
            }
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as error:
        raise MuseumDigitalCaptureError(
            f"MD image request failed: {error}"
        ) from error


def _write_verified_file(
    path: Path,
    body: bytes,
    verifier: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    path = require_safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        technical = verifier(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise MuseumDigitalCaptureError(
                f"refusing to replace captured evidence: {path}"
            ) from error
    finally:
        if temporary.exists():
            temporary.unlink()
    return technical


def _stable_id(prefix: str, value: Any) -> str:
    body = re.sub(
        r"[^A-Z0-9._-]+",
        "-",
        str(value or "").upper(),
    ).strip("-")
    if not body:
        raise MuseumDigitalCaptureError(
            "MD image row cannot form a stable evidence ID"
        )
    return f"{prefix}-{body}"


def capture_derivative(
    draft_path: Path,
    evidence_output: Path,
    bundle_output: Path,
    *,
    quell_id: str = "",
    timeout: float = 30.0,
    captured_at: str | None = None,
    fetcher: Callable[[str, float], dict[str, Any]] = fetch_asset,
    image_verifier: Callable[[Path], dict[str, Any]] = verify_image_derivative,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Capture one MD derivative and create a new derived draft."""

    evidence_output = require_safe_output(evidence_output)
    bundle_output = require_safe_output(bundle_output)
    draft_path = draft_path.expanduser().resolve()
    if evidence_output == bundle_output:
        raise MuseumDigitalCaptureError(
            "evidence and bundle outputs must be different"
        )
    if evidence_output.exists() or bundle_output.exists():
        raise MuseumDigitalCaptureError(
            "capture outputs already exist; replacement is forbidden"
        )
    draft_before = file_fingerprint(draft_path)
    draft = bundle_contract.read_bundle(draft_path)
    if draft["lineage"]["state"] != "draft":
        raise MuseumDigitalCaptureError(
            "MD capture input must be a decision-free draft"
        )
    if any(
        file["role"] == "museum_digital_derivative"
        for file in draft["files"]
    ):
        raise MuseumDigitalCaptureError(
            "draft already contains an MD derivative"
        )
    evidence_before = verify_evidence(draft)
    md_source, image = select_image(draft, quell_id)
    requested_url = _safe_asset_url(image)
    response = fetcher(requested_url, timeout)
    body = response.get("body")
    if not isinstance(body, bytes):
        raise MuseumDigitalCaptureError(
            "MD fetcher did not return byte evidence"
        )
    media_type = str(response.get("media_type") or "").casefold()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise MuseumDigitalCaptureError(
            f"unsupported captured media type: {media_type}"
        )
    expected_suffix = ALLOWED_MEDIA_TYPES[media_type]
    if evidence_output.suffix.casefold() not in {
        expected_suffix,
        ".jpeg" if expected_suffix == ".jpg" else expected_suffix,
    }:
        raise MuseumDigitalCaptureError(
            "evidence output suffix does not match captured media type"
        )
    if media_type == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
        raise MuseumDigitalCaptureError(
            "captured response does not have a JPEG signature"
        )
    response_status = int(response.get("http_status") or 0)
    if response_status != 200:
        raise MuseumDigitalCaptureError(
            "captured MD response does not claim HTTP 200"
        )
    final_url = str(response.get("final_url") or "")
    final = urllib.parse.urlsplit(final_url)
    if (
        final.scheme != "https"
        or final.hostname != "asset.museum-digital.org"
        or not final.path.startswith(ASSET_PREFIX)
    ):
        raise MuseumDigitalCaptureError(
            "captured final URL is outside the trusted asset boundary"
        )
    captured_at = captured_at or utc_now()
    technical = _write_verified_file(
        evidence_output,
        body,
        image_verifier,
    )
    digest = file_digest(evidence_output)
    capture = {
        "captured_at": captured_at,
        "requested_url": requested_url,
        "final_url": final_url,
        "http_status": response_status,
        "media_type": media_type,
        "content_length_header": int(
            response.get("content_length_header") or 0
        ),
        "size_bytes": evidence_output.stat().st_size,
        "content_digest": digest,
        "last_modified": str(response.get("last_modified") or ""),
        "etag": str(response.get("etag") or ""),
        "md_id": str(image.get("md_id") or ""),
        "quell_id": str(image.get("quell_id") or ""),
        "cached_image_row_digest": bundle_contract.snapshot_digest(image),
        "technical": technical,
    }
    enriched = json.loads(json.dumps(draft))
    parent_digest = bundle_contract.bundle_digest(draft)
    source_id = _stable_id(
        "SOURCE-MD-IMAGE",
        image.get("quell_id") or image.get("id"),
    )
    file_id = _stable_id(
        "FILE-MD-IMAGE",
        image.get("quell_id") or image.get("id"),
    )
    enriched["created_at"] = captured_at
    enriched["derivation"] = {
        "kind": "md_derivative_capture",
        "parent_bundle_digest": parent_digest,
        "operation_snapshot": capture,
        "operation_digest": bundle_contract.snapshot_digest(capture),
    }
    enriched["sources"].append(
        {
            "source_id": source_id,
            "kind": "museum_digital_derivative_capture",
            "locator": requested_url,
            "captured_at": captured_at,
            "snapshot": capture,
            "snapshot_digest": bundle_contract.snapshot_digest(capture),
            "note": (
                "One-way museum-digital comparison capture; "
                "bytes stored outside protected evidence roots."
            ),
        }
    )
    enriched["files"].append(
        {
            "file_id": file_id,
            "role": "museum_digital_derivative",
            "source_id": source_id,
            "locator": str(evidence_output),
            "content_digest": digest,
            "size_bytes": evidence_output.stat().st_size,
            "media_type": media_type,
            "note": (
                f"Cached MD image row {image.get('id')}; "
                f"quell_id={image.get('quell_id')}"
            ),
        }
    )
    enriched["identity"]["evidence_source_ids"].append(source_id)
    bundle_contract.validate_bundle(enriched)
    bundle_digest = bundle_contract.write_bundle(
        bundle_output,
        enriched,
    )
    if bundle_contract.read_bundle(bundle_output) != enriched:
        raise MuseumDigitalCaptureError(
            "enriched draft read-back did not match export"
        )
    verify_evidence(enriched)
    if file_fingerprint(draft_path) != draft_before:
        raise MuseumDigitalCaptureError(
            "parent draft changed during MD capture"
        )
    if verify_evidence(draft) != evidence_before:
        raise MuseumDigitalCaptureError(
            "protected source evidence changed during MD capture"
        )
    return enriched, bundle_digest, capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--bundle-output", type=Path, required=True)
    parser.add_argument("--quell-id", default="")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        enriched, digest, capture = capture_derivative(
            arguments.draft,
            arguments.evidence_output,
            arguments.bundle_output,
            quell_id=arguments.quell_id,
            timeout=arguments.timeout,
        )
    except (
        MuseumDigitalCaptureError,
        ProvenanceReviewError,
        ProtectedEvidenceError,
        TechnicalVerificationError,
        bundle_contract.ProvenanceBundleError,
        OSError,
    ) as error:
        print(
            json.dumps(
                {"result": "fail", "error": str(error)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "result": "ok",
                "case_id": enriched["case_id"],
                "bundle": str(arguments.bundle_output),
                "bundle_digest": digest,
                "evidence": str(arguments.evidence_output),
                "capture": capture,
                "readiness": enriched["readiness"],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
