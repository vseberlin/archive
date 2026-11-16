#!/usr/bin/env python3
"""Search read-only Elements evidence for collection-555 master matches.

This is a bounded evidence audit, not accession.  It combines the existing
whole-USB filename inventory with perceptual comparison against the selected
representatives already decoded for the Kurt Schwarz audit.  Cross-unit visual
matches remain provenance leads for the separate Pressebildarchiv WF unit.
Nothing is copied from, renamed on, or written to the USB source.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kurt_schwarz_reconciliation import KurtSchwarzReview
from kurt_schwarz_visual_verify import hamming, image_hashes, visual_state


FORMAT = "iss-pressebildarchiv-wf-usb-master-search-v1"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/pressebildarchiv-wf")
SOURCE_ROOT = Path("/media/vladimir/Elements")
KURT_UNIT = Path("/mnt/volume/iss-archive/units/kurt-schwarz")
KURT_REVIEW = Path(
    "/mnt/volume/iss-archive-work/kurt-schwarz/work/reconciliation/"
    "unit-reconciliation-v1"
)
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".psd",
    ".dng",
    ".nef",
    ".cr2",
    ".cr3",
    ".arw",
}
MASTER_EXTENSIONS = {
    ".tif",
    ".tiff",
    ".psd",
    ".dng",
    ".nef",
    ".cr2",
    ".cr3",
    ".arw",
}
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024


class SearchError(RuntimeError):
    """Raised when the bounded audit contract is violated."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def root_is_read_only(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    return bool(os.statvfs(path).f_flag & readonly_flag)


def inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise SearchError(f"output escaped the bounded work root: {resolved}")
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def load_objects(run_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    if summary.get("format") != "iss-kurt-schwarz-identity-provenance-v1":
        raise SearchError("identity-provenance run format is unsupported")
    with (run_root / "pressebildarchiv-wf-objects.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 48 or len({row["md_object_id"] for row in rows}) != 48:
        raise SearchError("collection-555 scope is not exactly 48 unique objects")
    capture = Path(summary["inputs"]["capture_555"]["path"]).resolve(strict=True)
    md_objects: dict[str, Any] = {}
    for path in sorted((capture / "raw").glob("page-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, item in payload.get("results", {}).get("objects", {}).items():
            md_objects[str(key)] = item
    if set(md_objects) != {row["md_object_id"] for row in rows}:
        raise SearchError("captured collection-555 object IDs changed")
    for row in rows:
        row["md"] = md_objects[row["md_object_id"]]
    return rows, summary


def asset_url(item: dict[str, Any]) -> str:
    images = item.get("object_images") or []
    image = next((row for row in images if row.get("is_main") == "j"), None)
    image = image or (images[0] if images else None)
    folder = str((image or {}).get("folder") or "").strip("/")
    filename = str((image or {}).get("filename_loc") or "")
    folder_path = Path(folder)
    if (
        not folder
        or folder_path.is_absolute()
        or ".." in folder_path.parts
        or not filename
        or Path(filename).name != filename
    ):
        raise SearchError("captured museum-digital image path is invalid")
    relative = "/".join((*folder_path.parts, filename))
    return "https://asset.museum-digital.org/berlin/" + urllib.parse.quote(
        relative
    )


def download_reference(url: str, destination: Path, last_request: float) -> float:
    if destination.is_file() and destination.stat().st_size > 4:
        return last_request
    temporary = destination.with_suffix(f".{os.getpid()}.part")
    for attempt in range(4):
        delay = 0.6 - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Industriesalon-archive-reconciliation/1.0",
                "Accept": "image/jpeg",
            },
        )
        temporary.unlink(missing_ok=True)
        try:
            response_context = urllib.request.urlopen(request, timeout=45)
        except urllib.error.HTTPError as error:
            last_request = time.monotonic()
            if error.code != 429 and not 500 <= error.code < 600:
                raise SearchError(f"reference download failed: HTTP {error.code}")
            if attempt == 3:
                raise SearchError(
                    f"reference download failed after retries: HTTP {error.code}"
                ) from error
            retry_after = error.headers.get("Retry-After", "")
            try:
                backoff = max(float(retry_after), 2**attempt)
            except ValueError:
                backoff = 2**attempt
            time.sleep(min(backoff, 30))
            continue
        except urllib.error.URLError as error:
            last_request = time.monotonic()
            if attempt == 3:
                raise SearchError("reference download failed after retries") from error
            time.sleep(2**attempt)
            continue
        try:
            with response_context as response:
                last_request = time.monotonic()
                content_type = response.headers.get_content_type()
                announced = int(response.headers.get("Content-Length") or 0)
                if content_type not in {"image/jpeg", "image/jpg"}:
                    raise SearchError(f"reference is not JPEG: {content_type}")
                if announced > MAX_DOWNLOAD_BYTES:
                    raise SearchError("reference image exceeds 32 MiB")
                total = 0
                with temporary.open("xb") as handle:
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise SearchError("reference image exceeds 32 MiB")
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            with temporary.open("rb") as handle:
                if handle.read(2) != b"\xff\xd8":
                    raise SearchError("reference lacks JPEG signature")
            os.replace(temporary, destination)
            return last_request
        finally:
            temporary.unlink(missing_ok=True)
    raise SearchError("reference download retries exhausted")


def filename_candidates(
    objects: list[dict[str, Any]], inventory_path: Path
) -> dict[str, list[dict[str, str]]]:
    by_number = {
        re.sub(r"\D", "", row["inventory"]): row["md_object_id"]
        for row in objects
        if re.sub(r"\D", "", row["inventory"])
    }
    output: dict[str, list[dict[str, str]]] = defaultdict(list)
    with inventory_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            extension = row["extension"].casefold()
            if extension not in IMAGE_EXTENSIONS:
                continue
            stem = Path(row["relative_path"]).stem
            compact = re.sub(r"(?i)^IS[\s_-]*", "", stem)
            match = re.match(r"^(\d{5})(?:\D|$)", compact)
            if match and match.group(1) in by_number:
                output[by_number[match.group(1)]].append(row)
    for rows in output.values():
        rows.sort(key=lambda row: row["relative_path"])
    return output


def safe_usb_path(row: dict[str, str]) -> Path:
    path = Path(row["source_path"]).resolve(strict=True)
    if path == SOURCE_ROOT or not path.is_relative_to(SOURCE_ROOT):
        raise SearchError(f"inventory path escaped the USB root: {path}")
    if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
        raise SearchError(f"USB inventory binding changed: {path}")
    return path


def compare_filename_candidates(
    objects: list[dict[str, Any]],
    references: dict[str, Path],
    candidates: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    output = []
    for item in objects:
        object_id = item["md_object_id"]
        reference_hash = image_hashes(references[object_id])
        for row in candidates.get(object_id, []):
            path = safe_usb_path(row)
            result = {
                "md_object_id": object_id,
                "inventory": item["inventory"],
                "candidate_kind": "whole_usb_filename",
                "candidate_path": str(path),
                "extension": row["extension"].casefold(),
                "size_bytes": row["size_bytes"],
                "phash_distance": "",
                "dhash_distance": "",
                "visual_state": "decode_failed",
                "candidate_role": (
                    "master_format_candidate"
                    if row["extension"].casefold() in MASTER_EXTENSIONS
                    else "derivative_format_candidate"
                ),
                "cross_unit_record_key": "",
                "cross_unit_inventory": "",
            }
            try:
                candidate_hash = image_hashes(path)
                pdistance = hamming(reference_hash[0], candidate_hash[0])
                ddistance = hamming(reference_hash[1], candidate_hash[1])
                state, _ = visual_state(pdistance, ddistance)
                result.update(
                    {
                        "phash_distance": pdistance,
                        "dhash_distance": ddistance,
                        "visual_state": state,
                    }
                )
            except (OSError, ValueError):
                pass
            output.append(result)
    return output


def compare_kurt_representatives(
    objects: list[dict[str, Any]], references: dict[str, Path]
) -> list[dict[str, Any]]:
    review = KurtSchwarzReview(KURT_UNIT, KURT_REVIEW)
    representatives = []
    records = review.records()
    for index, record in enumerate(records, 1):
        candidate_id = record["thumbnail_candidate_id"]
        if not candidate_id:
            continue
        preview = review.preview_cache_path(candidate_id)
        if not preview.is_file():
            continue
        try:
            candidate_hash = image_hashes(preview)
        except (OSError, ValueError):
            continue
        candidate = review.candidate(candidate_id)
        source_paths = []
        occurrences = json.loads(candidate["source_occurrences_json"])
        for occurrence in occurrences:
            source = Path(occurrence["source_path"]).resolve()
            if (
                source.is_file()
                and source.is_relative_to(SOURCE_ROOT)
                and source.stat().st_size
                == int(occurrence["source_size_bytes"])
            ):
                source_paths.append(str(source))
        representatives.append(
            {
                "hash": candidate_hash,
                "record_key": record["record_key"],
                "inventory": record["display_inventory"],
                "candidate": candidate,
                "source_paths": source_paths,
            }
        )
        if index % 250 == 0:
            print(
                f"representative-previews={index}/{len(records)} "
                f"usable={len(representatives)}",
                flush=True,
            )
    output = []
    for index, item in enumerate(objects, 1):
        object_id = item["md_object_id"]
        reference_hash = image_hashes(references[object_id])
        ranked = []
        for candidate in representatives:
            pdistance = hamming(reference_hash[0], candidate["hash"][0])
            ddistance = hamming(reference_hash[1], candidate["hash"][1])
            ranked.append((pdistance + ddistance, pdistance, ddistance, candidate))
        for _, pdistance, ddistance, candidate in sorted(
            ranked, key=lambda row: row[:3]
        )[:5]:
            state, _ = visual_state(pdistance, ddistance)
            media = candidate["candidate"]
            output.append(
                {
                    "md_object_id": object_id,
                    "inventory": item["inventory"],
                    "candidate_kind": "cross_unit_visual_lead",
                    "candidate_path": " | ".join(candidate["source_paths"]),
                    "extension": media["extension"],
                    "size_bytes": media["size_bytes"],
                    "phash_distance": pdistance,
                    "dhash_distance": ddistance,
                    "visual_state": state,
                    "candidate_role": media["content_role"],
                    "cross_unit_record_key": candidate["record_key"],
                    "cross_unit_inventory": candidate["inventory"],
                }
            )
        print(f"objects-compared={index}/{len(objects)}", flush=True)
    return output


def build(
    run_root: Path,
    inventory_path: Path,
    output: Path,
    reference_cache: Path | None = None,
) -> dict[str, Any]:
    if not root_is_read_only(SOURCE_ROOT):
        raise SearchError(f"USB source is not read-only: {SOURCE_ROOT}")
    if output.exists() and any(
        (output / name).exists()
        for name in ("summary.json", "objects.csv", "candidates.csv")
    ):
        raise SearchError(f"refusing completed or partially written output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    objects, provenance_summary = load_objects(run_root)
    references = {}
    if reference_cache is not None:
        reference_cache = inside(reference_cache, WORK_ROOT)
        for item in objects:
            object_id = item["md_object_id"]
            path = reference_cache / f"md-{object_id}.jpg"
            if not path.is_file() or path.stat().st_size < 5:
                raise SearchError(f"reference cache is incomplete: {path}")
            references[object_id] = path
        print(f"references=reused/{len(references)}", flush=True)
    else:
        references_root = output / "md-reference"
        references_root.mkdir(exist_ok=True)
        last_request = 0.0
        for index, item in enumerate(objects, 1):
            object_id = item["md_object_id"]
            destination = references_root / f"md-{object_id}.jpg"
            last_request = download_reference(
                asset_url(item["md"]), destination, last_request
            )
            references[object_id] = destination
            print(f"references={index}/{len(objects)}", flush=True)

    filename_index = filename_candidates(objects, inventory_path)
    filename_rows = compare_filename_candidates(
        objects, references, filename_index
    )
    cross_rows = compare_kurt_representatives(objects, references)
    candidate_rows = filename_rows + cross_rows
    candidate_fields = (
        "md_object_id",
        "inventory",
        "candidate_kind",
        "candidate_path",
        "extension",
        "size_bytes",
        "phash_distance",
        "dhash_distance",
        "visual_state",
        "candidate_role",
        "cross_unit_record_key",
        "cross_unit_inventory",
    )
    write_csv(output / "candidates.csv", candidate_fields, candidate_rows)

    object_rows = []
    for item in objects:
        object_id = item["md_object_id"]
        rows = [row for row in candidate_rows if row["md_object_id"] == object_id]
        confirmed_master = [
            row
            for row in rows
            if row["visual_state"] == "confirmed_visual_match"
            and row["candidate_path"]
            and row["extension"] in MASTER_EXTENSIONS
        ]
        probable_master = [
            row
            for row in rows
            if row["visual_state"] == "probable_visual_match_review"
            and row["candidate_path"]
            and row["extension"] in MASTER_EXTENSIONS
        ]
        derivative = [
            row
            for row in rows
            if row["visual_state"]
            in {"confirmed_visual_match", "probable_visual_match_review"}
            and row["candidate_path"]
        ]
        if confirmed_master:
            state = "confirmed_master_match"
            best = sorted(
                confirmed_master,
                key=lambda row: (
                    int(row["phash_distance"]),
                    int(row["dhash_distance"]),
                ),
            )[0]
        elif probable_master:
            state = "probable_master_match_review"
            best = sorted(
                probable_master,
                key=lambda row: (
                    int(row["phash_distance"]),
                    int(row["dhash_distance"]),
                ),
            )[0]
        elif derivative:
            state = "derivative_match_only"
            best = sorted(
                derivative,
                key=lambda row: (
                    int(row["phash_distance"]),
                    int(row["dhash_distance"]),
                ),
            )[0]
        else:
            state = "no_bounded_match"
            best = {}
        object_rows.append(
            {
                "entity_id": item["entity_id"],
                "md_object_id": object_id,
                "inventory": item["inventory"],
                "title": item["title"],
                "audit_state": state,
                "filename_candidate_count": len(filename_index.get(object_id, [])),
                "best_candidate_path": best.get("candidate_path", ""),
                "best_candidate_extension": best.get("extension", ""),
                "best_candidate_role": best.get("candidate_role", ""),
                "best_phash_distance": best.get("phash_distance", ""),
                "best_dhash_distance": best.get("dhash_distance", ""),
                "cross_unit_record_key": best.get("cross_unit_record_key", ""),
                "large_file_action": "none",
            }
        )
    object_fields = tuple(object_rows[0])
    write_csv(output / "objects.csv", object_fields, object_rows)
    state_counts = Counter(row["audit_state"] for row in object_rows)
    summary = {
        "format": FORMAT,
        "created_at": utc_now(),
        "accepted_identity_provenance_run": str(run_root),
        "accepted_identity_provenance_summary_sha256": sha256_file(
            run_root / "summary.json"
        ),
        "whole_usb_inventory": str(inventory_path),
        "reference_cache_reused_from": (
            str(reference_cache) if reference_cache is not None else ""
        ),
        "source_root": str(SOURCE_ROOT),
        "source_read_only": True,
        "objects": len(object_rows),
        "filename_candidate_rows": len(filename_rows),
        "cross_unit_visual_lead_rows": len(cross_rows),
        "state_counts": dict(sorted(state_counts.items())),
        "large_files_moved": 0,
        "source_files_modified": 0,
        "deletion_authorized": False,
        "scope_note": (
            "The whole USB filename index is exhaustive. Content matching is "
            "bounded to exact filename candidates and the selected Kurt audit "
            "representatives with existing decoded previews; no broad 196k-image "
            "USB decode was performed. Cross-unit matches do not create Kurt "
            "membership or a subdivision."
        ),
    }
    with (output / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    with (output / "checksums.csv").open(
        "x", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(("path", "sha256"))
        for path in (output / "candidates.csv", output / "objects.csv", output / "summary.json"):
            writer.writerow((path.name, sha256_file(path)))
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-provenance-run", type=Path, required=True)
    parser.add_argument("--whole-usb-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reference-cache",
        type=Path,
        help="reuse a complete prior md-reference directory without copying it",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output = inside(args.output, WORK_ROOT)
        summary = build(
            args.identity_provenance_run.expanduser().resolve(strict=True),
            args.whole_usb_inventory.expanduser().resolve(strict=True),
            output,
            (
                args.reference_cache.expanduser().resolve(strict=True)
                if args.reference_cache is not None
                else None
            ),
        )
    except (
        SearchError,
        OSError,
        ValueError,
        KeyError,
        csv.Error,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
