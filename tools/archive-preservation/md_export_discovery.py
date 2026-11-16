#!/usr/bin/env python3
"""Capture a museum-digital export query as work-only discovery evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


FORMAT = "iss-md-export-discovery-v1"
USER_AGENT = "ISS archive discovery; contact: Industriesalon Schoeneweide"
EXPORT_URL = "https://berlin.museum-digital.de/export/json/{query}?limit={limit}&offset={offset}"
WORK_ROOT = Path("/mnt/volume/iss-archive-work")
MAX_PAGE_LIMIT = 100
MIN_PAGE_DELAY_SECONDS = 0.6
RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


class DiscoveryError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_polite_args(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_PAGE_LIMIT:
        raise DiscoveryError(
            f"museum-digital page limit must be between 1 and {MAX_PAGE_LIMIT}"
        )
    if args.delay < MIN_PAGE_DELAY_SECONDS:
        raise DiscoveryError(
            "museum-digital page delay must be at least "
            f"{MIN_PAGE_DELAY_SECONDS:.1f} seconds"
        )
    if args.timeout <= 0:
        raise DiscoveryError("request timeout must be positive")
    if args.retries < 0:
        raise DiscoveryError("request retries cannot be negative")
    if args.retry_delay <= 0:
        raise DiscoveryError("request retry delay must be positive")


def retry_after_seconds(headers: Any, fallback: float) -> float:
    value = str(headers.get("Retry-After") or "").strip() if headers else ""
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return fallback


def fetch_page(
    url: str,
    *,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> tuple[int, bytes, int]:
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read(), attempt + 1
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS or attempt >= retries:
                raise
            wait = retry_after_seconds(
                error.headers,
                retry_delay * (2**attempt),
            )
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
            wait = retry_delay * (2**attempt)
        time.sleep(wait)
    raise DiscoveryError("request retry loop ended unexpectedly")


def safe_component(value: str) -> str:
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip(".-")
    return cleaned or "query"


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DiscoveryError(f"refusing to replace existing evidence: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DiscoveryError(f"refusing to replace existing evidence: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def result_objects(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    objects = results.get("objects")
    if isinstance(objects, dict):
        return [value for value in objects.values() if isinstance(value, dict)]
    if isinstance(objects, list):
        return [value for value in objects if isinstance(value, dict)]
    return []


def reported_total(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    results = payload.get("results")
    if not isinstance(results, dict):
        return 0
    try:
        return int(results.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def named_values(items: Any, key: str) -> str:
    if not isinstance(items, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value = " ".join(str(item.get(key) or "").split())
        folded = value.casefold()
        if value and folded not in seen:
            values.append(value)
            seen.add(folded)
    return "; ".join(values)


def event_times(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        period = item.get("time") if isinstance(item.get("time"), dict) else {}
        value = " ".join(str(period.get("time_name") or "").split())
        folded = value.casefold()
        if value and folded not in seen:
            values.append(value)
            seen.add(folded)
    return "; ".join(values)


def pdf_resources(item: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    images = item.get("object_images")
    if not isinstance(images, list):
        return resources
    for image in images:
        if not isinstance(image, dict):
            continue
        candidates = [
            str(image.get("filename_loc") or ""),
            str(image.get("name") or ""),
            str(image.get("filename") or ""),
        ]
        url = next((value for value in candidates if value.lower().endswith(".pdf")), "")
        if str(image.get("type") or "").casefold() == "text" or url:
            resources.append(
                {
                    "resource_id": str(image.get("quell_id") or ""),
                    "url": url,
                    "rights": str(image.get("rights") or ""),
                    "owner": str(image.get("owner") or ""),
                    "is_main": str(image.get("is_main") or ""),
                }
            )
    return resources


def object_row(item: dict[str, Any]) -> dict[str, Any]:
    pdfs = pdf_resources(item)
    institution = item.get("object_institution") if isinstance(item.get("object_institution"), dict) else {}
    return {
        "object_id": item.get("object_id", ""),
        "inventory": item.get("object_inventory_number", ""),
        "title": item.get("object_name", ""),
        "object_type": item.get("object_type", ""),
        "date": event_times(item.get("object_events")),
        "collections": named_values(item.get("object_collection"), "collection_name"),
        "institution_id": institution.get("institution_id", ""),
        "institution_name": institution.get("institution_name", ""),
        "pdf_count": len(pdfs),
        "pdf_resource_ids": "; ".join(row["resource_id"] for row in pdfs if row["resource_id"]),
        "pdf_urls": "; ".join(row["url"] for row in pdfs if row["url"]),
        "image_count": len(item.get("object_images") if isinstance(item.get("object_images"), list) else []),
        "last_updated": item.get("object_last_updated", ""),
        "metadata_rights": (item.get("licence") or {}).get("metadata_rights_status", "")
        if isinstance(item.get("licence"), dict)
        else "",
        "object_url": f"https://berlin.museum-digital.de/object/{item.get('object_id')}",
    }


def capture(args: argparse.Namespace) -> dict[str, Any]:
    validate_polite_args(args)
    work_root = args.work_root.expanduser().resolve()
    try:
        work_root.relative_to(WORK_ROOT)
    except ValueError as error:
        raise DiscoveryError("work root must be below /mnt/volume/iss-archive-work")
    base = work_root / "work" / "reconciliation" / "museum-digital"
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / "runs" / f"{run_id}-{safe_component(args.unit)}"
    if run_dir.exists():
        raise DiscoveryError(f"run directory already exists: {run_dir}")
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True)

    request_rows: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0
    page_index = 1
    encoded_query = urllib.parse.quote(args.query, safe=":,")
    while total is None or offset < total:
        url = EXPORT_URL.format(query=encoded_query, limit=args.limit, offset=offset)
        captured_at = utc_now()
        status = 0
        error = ""
        raw = b""
        attempts = 0
        try:
            status, raw, attempts = fetch_page(
                url,
                timeout=args.timeout,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
            digest = sha256_bytes(raw)
            raw_path = raw_dir / f"page-{page_index:04d}-offset-{offset:06d}.json"
            atomic_write_bytes(raw_path, raw)
            payload = json.loads(raw.decode("utf-8"))
            page = result_objects(payload)
            page_total = reported_total(payload)
            if total is None:
                total = page_total
            elif page_total and page_total != total:
                raise DiscoveryError(
                    f"reported total changed from {total} to {page_total}"
                )
            objects.extend(page)
            request_rows.append(
                {
                    "page": page_index,
                    "offset": offset,
                    "limit": args.limit,
                    "url": url,
                    "captured_at": captured_at,
                    "attempts": attempts,
                    "http_status": status,
                    "reported_total": page_total,
                    "returned_objects": len(page),
                    "raw_path": raw_path.relative_to(run_dir).as_posix(),
                    "sha256": digest,
                    "error": "",
                }
            )
            if not page:
                break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            request_rows.append(
                {
                    "page": page_index,
                    "offset": offset,
                    "limit": args.limit,
                    "url": url,
                    "captured_at": captured_at,
                    "attempts": attempts or args.retries + 1,
                    "http_status": status,
                    "reported_total": total or "",
                    "returned_objects": 0,
                    "raw_path": "",
                    "sha256": sha256_bytes(raw) if raw else "",
                    "error": error,
                }
            )
            if not args.keep_going:
                raise DiscoveryError(error) from exc
        offset += args.limit
        page_index += 1
        if args.max_pages and page_index > args.max_pages:
            break
        if total is None or offset < total:
            time.sleep(args.delay)

    rows = [object_row(item) for item in objects]
    ids = [str(row["object_id"]) for row in rows if row["object_id"]]
    inventories = [str(row["inventory"]) for row in rows if row["inventory"]]
    pdf_counts = Counter(str(row["pdf_count"]) for row in rows)
    collections = Counter()
    for row in rows:
        for collection in str(row["collections"]).split("; "):
            if collection:
                collections[collection] += 1
    coverage = {
        "format": FORMAT,
        "created_at": utc_now(),
        "unit": args.unit,
        "query": args.query,
        "run_dir": str(run_dir),
        "reported_total": total,
        "captured_objects": len(rows),
        "pages": len([row for row in request_rows if not row["error"]]),
        "request_failures": len([row for row in request_rows if row["error"]]),
        "duplicate_object_ids": len(ids) - len(set(ids)),
        "duplicate_inventories": len(inventories) - len(set(inventories)),
        "pdf_count_distribution": dict(sorted(pdf_counts.items())),
        "objects_with_pdf": sum(int(row["pdf_count"]) > 0 for row in rows),
        "objects_without_pdf": sum(int(row["pdf_count"]) == 0 for row in rows),
        "collections": dict(collections.most_common()),
    }
    write_csv(
        run_dir / "request-manifest.csv",
        [
            "page",
            "offset",
            "limit",
            "url",
            "captured_at",
            "attempts",
            "http_status",
            "reported_total",
            "returned_objects",
            "raw_path",
            "sha256",
            "error",
        ],
        request_rows,
    )
    write_csv(
        run_dir / "objects.csv",
        [
            "object_id",
            "inventory",
            "title",
            "object_type",
            "date",
            "collections",
            "institution_id",
            "institution_name",
            "pdf_count",
            "pdf_resource_ids",
            "pdf_urls",
            "image_count",
            "last_updated",
            "metadata_rights",
            "object_url",
        ],
        rows,
    )
    atomic_write_text(
        run_dir / "coverage.json",
        json.dumps(coverage, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    atomic_write_text(
        run_dir / "README.md",
        "# museum-digital export discovery\n\n"
        "Read-only capture of museum-digital export API pages. This run does not "
        "download PDFs and does not write to the final archive package.\n\n"
        f"Query: `{args.query}`\n"
        f"Unit: `{args.unit}`\n",
    )
    return {"result": "ok", **coverage}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=MAX_PAGE_LIMIT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=MIN_PAGE_DELAY_SECONDS)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--keep-going", action="store_true")
    return parser


def main() -> int:
    result = capture(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
