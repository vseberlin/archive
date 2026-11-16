#!/usr/bin/env python3
"""Complete the interrupted institution:29 museum-digital capture in place."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


USER_AGENT = "ISS archive discovery; contact: Industriesalon Schoeneweide"
BASE = Path("/mnt/volume/iss-archive-work/tfa/work/reconciliation/museum-digital/runs")
TOTAL = 11170
QUERY = "institution:29"
PAGE_LIMIT = 100


class CaptureError(RuntimeError):
    pass


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def utc_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")


def result_objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    objects = (payload.get("results") or {}).get("objects") or {}
    values = list(objects.values()) if isinstance(objects, dict) else list(objects)
    return [value for value in values if isinstance(value, dict)]


def reported_total(payload: dict[str, Any]) -> int:
    return int((payload.get("results") or {}).get("total") or 0)


def fetch_json(offset: int) -> tuple[bytes, dict[str, Any], int, int]:
    last_error = ""
    total_attempts = 0
    for limit in (PAGE_LIMIT, 99, 50, 25, 10, 1):
        url = (
            "https://berlin.museum-digital.de/export/json/"
            f"{QUERY}?limit={limit}&offset={offset}"
        )
        for attempt in range(1, 3):
            total_attempts += 1
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
                    content_type = str(response.headers.get("Content-Type") or "")
                    if response.status != 200 or "json" not in content_type.casefold():
                        raise CaptureError(
                            f"HTTP/content-type mismatch: {response.status} {content_type}"
                        )
                    payload = json.loads(raw.decode("utf-8"))
                    if reported_total(payload) != TOTAL:
                        raise CaptureError(
                            f"reported total drifted: {reported_total(payload)} != {TOTAL}"
                        )
                    return raw, payload, total_attempts, limit
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt < 2:
                    time.sleep(2.0)
        time.sleep(1.0)
    raise CaptureError(f"offset {offset} failed after retries: {last_error}")


def atomic_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        raise CaptureError(f"refusing to replace: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_text(path: Path, text: str) -> None:
    atomic_bytes(path, text.encode("utf-8"))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    if path.exists():
        raise CaptureError(f"refusing to replace: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def named_values(rows: Any, key: str) -> str:
    if not isinstance(rows, list):
        return ""
    return "; ".join(
        str(row.get(key) or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get(key) or "").strip()
    )


def event_times(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(((row.get("time") or {}).get("time_name") or "")).strip()
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def object_row(item: dict[str, Any]) -> dict[str, Any]:
    institution = item.get("object_institution") or {}
    images = item.get("object_images") if isinstance(item.get("object_images"), list) else []
    return {
        "object_id": item.get("object_id", ""),
        "inventory": item.get("object_inventory_number", ""),
        "title": item.get("object_name", ""),
        "object_type": item.get("object_type", ""),
        "date": event_times(item.get("object_events")),
        "collections": named_values(item.get("object_collection"), "collection_name"),
        "institution_id": institution.get("institution_id", ""),
        "institution_name": institution.get("institution_name", ""),
        "pdf_count": 0,
        "pdf_resource_ids": "",
        "pdf_urls": "",
        "image_count": len(images),
        "last_updated": item.get("object_last_updated", ""),
        "metadata_rights": ((item.get("licence") or {}).get("metadata_rights_status", "")),
        "object_url": f"https://berlin.museum-digital.de/object/{item.get('object_id')}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.expanduser().resolve()
    try:
        run.relative_to(BASE.resolve())
    except ValueError as error:
        raise CaptureError("run escaped the TFA museum-digital runs root") from error
    raw_dir = run / "raw"
    if not raw_dir.is_dir() or (run / "coverage.json").exists():
        raise CaptureError("run is absent or already finalized")

    preserved_errors: list[Path] = []
    for invalid in sorted(raw_dir.glob("page-*-offset-*.json")):
        try:
            json.loads(invalid.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            preserved_error = invalid.with_suffix(".http-error.html")
            if preserved_error.exists():
                raise CaptureError(f"preserved error already exists: {preserved_error}")
            os.replace(invalid, preserved_error)
            preserved_errors.append(preserved_error)
    preserved_errors.extend(sorted(raw_dir.glob("page-*-offset-*.http-error.html")))
    preserved_errors = sorted(set(preserved_errors))

    request_rows: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    initial = sorted(raw_dir.glob("page-*-offset-*.json"))
    for path in initial:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if reported_total(payload) != TOTAL:
            raise CaptureError(f"total drift in {path.name}")
        page_objects = result_objects(payload)
        if not 1 <= len(page_objects) <= PAGE_LIMIT:
            raise CaptureError(
                f"non-clean page size in {path.name}: {len(page_objects)}"
            )
        offset = int(path.stem.rsplit("offset-", 1)[1])
        limit = PAGE_LIMIT
        objects.extend(page_objects)
        request_rows.append(
            {
                "page": len(request_rows) + 1,
                "offset": offset,
                "limit": limit,
                "url": f"https://berlin.museum-digital.de/export/json/{QUERY}?limit={limit}&offset={offset}",
                "captured_at": utc_from_mtime(path),
                "attempts": 1,
                "http_status": 200,
                "reported_total": TOTAL,
                "returned_objects": len(page_objects),
                "raw_path": path.relative_to(run).as_posix(),
                "sha256": sha256_bytes(raw),
                "error": "",
            }
        )

    request_rows.sort(key=lambda row: int(row["offset"]))
    expected_offset = 0
    for row in request_rows:
        if int(row["offset"]) != expected_offset:
            raise CaptureError(
                f"capture has a gap: expected offset {expected_offset}, got {row['offset']}"
            )
        expected_offset += int(row["returned_objects"])
    offset = max(
        int(row["offset"]) + int(row["returned_objects"])
        for row in request_rows
    )
    while offset < TOTAL:
        raw, payload, attempts, limit = fetch_json(offset)
        page_index = len(request_rows) + 1
        path = raw_dir / f"page-{page_index:04d}-offset-{offset:06d}.json"
        atomic_bytes(path, raw)
        page_objects = result_objects(payload)
        objects.extend(page_objects)
        request_rows.append(
            {
                "page": page_index,
                "offset": offset,
                "limit": limit,
                "url": f"https://berlin.museum-digital.de/export/json/{QUERY}?limit={limit}&offset={offset}",
                "captured_at": utc_from_mtime(path),
                "attempts": attempts,
                "http_status": 200,
                "reported_total": TOTAL,
                "returned_objects": len(page_objects),
                "raw_path": path.relative_to(run).as_posix(),
                "sha256": sha256_bytes(raw),
                "error": "",
            }
        )
        if not page_objects:
            raise CaptureError(f"offset {offset} returned no objects before total {TOTAL}")
        offset += len(page_objects)
        time.sleep(0.8)

    by_id: dict[str, dict[str, Any]] = {}
    for item in objects:
        object_id = str(item.get("object_id") or "")
        if not object_id:
            raise CaptureError("captured object lacks ID")
        existing = by_id.get(object_id)
        if existing is not None and existing != item:
            raise CaptureError(f"conflicting duplicate object ID: {object_id}")
        by_id[object_id] = item
    if len(by_id) != TOTAL:
        raise CaptureError(f"capture incomplete: {len(by_id)} != {TOTAL}")
    if any(str((item.get("object_institution") or {}).get("institution_id")) != "29" for item in by_id.values()):
        raise CaptureError("capture contains an object outside institution 29")

    rows = [object_row(item) for item in sorted(by_id.values(), key=lambda item: int(item["object_id"]))]
    inventories = [str(row["inventory"]) for row in rows if row["inventory"]]
    collections = Counter()
    for row in rows:
        for value in str(row["collections"]).split("; "):
            if value:
                collections[value] += 1
    request_rows.sort(key=lambda row: int(row["offset"]))
    for index, row in enumerate(request_rows, 1):
        row["page"] = index
    write_csv(
        run / "request-manifest.csv",
        [
            "page", "offset", "limit", "url", "captured_at", "attempts",
            "http_status", "reported_total", "returned_objects", "raw_path",
            "sha256", "error",
        ],
        request_rows,
    )
    write_csv(
        run / "objects.csv",
        [
            "object_id", "inventory", "title", "object_type", "date",
            "collections", "institution_id", "institution_name", "pdf_count",
            "pdf_resource_ids", "pdf_urls", "image_count", "last_updated",
            "metadata_rights", "object_url",
        ],
        rows,
    )
    coverage = {
        "format": "iss-md-export-discovery-v1",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "unit": "tfa-institution-29-clean-local-only-recheck",
        "query": QUERY,
        "run_dir": str(run),
        "reported_total": TOTAL,
        "captured_objects": len(rows),
        "pages": len(request_rows),
        "request_failures": 0,
        "maximum_page_limit": PAGE_LIMIT,
        "recovered_non_json_responses": len(preserved_errors),
        "preserved_error_responses": [str(path) for path in preserved_errors],
        "duplicate_object_ids": 0,
        "duplicate_inventories": len(inventories) - len(set(inventories)),
        "collections": dict(collections.most_common()),
    }
    atomic_text(run / "coverage.json", json.dumps(coverage, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    atomic_text(
        run / "README.md",
        "# museum-digital institution 29 capture\n\n"
        "Read-only capture for the TFA local-only duplicate recheck. Every request "
        "uses at most 100 objects; temporary HTML error responses are retained beside "
        "successfully retried JSON pages.\n",
    )
    print(json.dumps({"result": "ok", **coverage}, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
