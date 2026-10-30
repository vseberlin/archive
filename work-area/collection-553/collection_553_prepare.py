#!/usr/bin/env python3
"""Prepare unresolved reconciliation evidence for museum-digital collection 553.

The script writes only below the collection-553 work root.  It downloads the
current museum-digital resources as reference evidence, queries the existing
document and visual indexes read-only, and initializes human decision tables.
It never selects a master, changes a source file, creates an accession, or
authorizes deletion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps


COLLECTION_ID = 553
EXPECTED_OBJECTS = 54
EXPECTED_RESOURCES = 66
EXPECTED_PDFS = 62
EXPECTED_IMAGES = 4
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-553")
SOURCE_ROOT = Path("/media/vladimir/Elements")
DOCUMENT_INDEX = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
VISUAL_TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
DEFAULT_OUTPUT = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
USER_AGENT = "Industriesalon-Archive-Reconciliation/1.0 (collection 553 proposal)"
MIN_REQUEST_DELAY = 0.65
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
RETRYABLE = {429, 500, 502, 503, 504}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


class PrepareError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_guard() -> dict[str, str]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    rows = json.loads(result.stdout).get("filesystems") or []
    if len(rows) != 1:
        raise PrepareError("Elements did not resolve to exactly one filesystem")
    row = rows[0]
    options = set(str(row.get("options") or "").split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise PrepareError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key) or "") for key in ("target", "source", "fstype", "options")}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PrepareError(f"refusing to replace evidence: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def csv_payload(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(fields),
        extrasaction="ignore",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    atomic_bytes(path, csv_payload(fields, rows))


def atomic_replace_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_capture(capture: Path) -> dict[str, dict[str, Any]]:
    coverage = json.loads((capture / "coverage.json").read_text(encoding="utf-8"))
    if (
        coverage.get("query") != "collection:553"
        or coverage.get("reported_total") != EXPECTED_OBJECTS
        or coverage.get("captured_objects") != EXPECTED_OBJECTS
        or coverage.get("request_failures") != 0
        or coverage.get("duplicate_object_ids") != 0
    ):
        raise PrepareError(f"capture does not prove exact collection-553 scope: {coverage}")
    objects: dict[str, dict[str, Any]] = {}
    for path in sorted((capture / "raw").glob("page-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_objects = (payload.get("results") or {}).get("objects") or {}
        values = raw_objects.values() if isinstance(raw_objects, dict) else raw_objects
        for item in values:
            object_id = str(item.get("object_id") or "")
            if not object_id or object_id in objects:
                raise PrepareError(f"missing or duplicate object id in raw capture: {object_id}")
            objects[object_id] = item
    if len(objects) != EXPECTED_OBJECTS:
        raise PrepareError(f"expected {EXPECTED_OBJECTS} objects, found {len(objects)}")
    resources = [resource for item in objects.values() for resource in item.get("object_images") or []]
    if len(resources) != EXPECTED_RESOURCES:
        raise PrepareError(f"expected {EXPECTED_RESOURCES} resources, found {len(resources)}")
    types = Counter(str(row.get("type") or "").casefold() for row in resources)
    if types != Counter({"text": EXPECTED_PDFS, "image": EXPECTED_IMAGES}):
        raise PrepareError(f"unexpected resource types: {dict(types)}")
    for object_id, item in objects.items():
        collection_ids = [str(row.get("collection_id") or "") for row in item.get("object_collection") or []]
        if collection_ids != [str(COLLECTION_ID)]:
            raise PrepareError(f"MD {object_id} has unexpected collection membership: {collection_ids}")
        main_count = sum(str(row.get("is_main") or "") == "j" for row in item.get("object_images") or [])
        if main_count != 1:
            raise PrepareError(f"MD {object_id} has {main_count} main resources")
    return objects


def resource_url(resource: dict[str, Any]) -> str:
    filename = str(resource.get("filename_loc") or "")
    if filename.startswith(("https://", "http://")):
        return filename
    folder = str(resource.get("folder") or "").strip("/")
    if not folder or not filename or Path(filename).name != filename or ".." in Path(folder).parts:
        raise PrepareError(f"unsafe museum-digital resource path: {resource}")
    relative = "/".join((*Path(folder).parts, filename))
    return "https://asset.museum-digital.org/berlin/" + urllib.parse.quote(relative)


def resource_suffix(resource: dict[str, Any]) -> str:
    source = urllib.parse.urlparse(str(resource.get("filename_loc") or "")).path
    suffix = Path(source).suffix.casefold()
    if str(resource.get("type") or "").casefold() == "text":
        return suffix if suffix == ".pdf" else ".pdf"
    return suffix if suffix in IMAGE_EXTENSIONS else ".jpg"


def retry_after(error: urllib.error.HTTPError, fallback: float) -> float:
    value = str(error.headers.get("Retry-After") or "").strip() if error.headers else ""
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


def download(url: str, target: Path, last_request: float) -> float:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    for attempt in range(4):
        delay = MIN_REQUEST_DELAY - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                last_request = time.monotonic()
                announced = int(response.headers.get("Content-Length") or 0)
                if announced > MAX_DOWNLOAD_BYTES:
                    raise PrepareError(f"resource exceeds 512 MiB limit: {url}")
                size = 0
                with temporary.open("xb") as handle:
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise PrepareError(f"resource exceeds 512 MiB limit: {url}")
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            if size == 0:
                raise PrepareError(f"empty resource: {url}")
            os.replace(temporary, target)
            return last_request
        except urllib.error.HTTPError as error:
            last_request = time.monotonic()
            if error.code not in RETRYABLE or attempt == 3:
                raise PrepareError(f"download failed for {url}: HTTP {error.code}") from error
            time.sleep(min(retry_after(error, 2**attempt), 30))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_request = time.monotonic()
            if attempt == 3:
                raise PrepareError(f"download failed for {url}: {error}") from error
            time.sleep(min(2**attempt, 20))
        finally:
            temporary.unlink(missing_ok=True)
    raise PrepareError(f"download retries exhausted: {url}")


def pdf_technical(path: Path) -> tuple[str, str, str]:
    try:
        signature = path.open("rb").read(5)
    except OSError as error:
        return "unreadable", "", str(error)
    if signature != b"%PDF-":
        return "invalid_signature", "", "missing PDF signature"
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return "unreadable", "", str(error)
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())[:500]
        return "unreadable", "", detail
    match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    return "readable", match.group(1) if match else "", ""


def image_technical(path: Path) -> tuple[str, str, str]:
    try:
        with Image.open(path) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        return str(width), str(height), ""
    except (OSError, ValueError) as error:
        return "", "", str(error)


def date_values(item: dict[str, Any]) -> str:
    values: list[str] = []
    for event in item.get("object_events") or []:
        value = str((event.get("time") or {}).get("time_name") or "").strip()
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def inventory_tokens(inventory: str) -> list[str]:
    normalized = re.sub(r"(?i)^archiv[\s_-]*", "", inventory.strip())
    normalized = re.sub(r"\s+", "-", normalized)
    match = re.search(r"(\d{5,}(?:[-_]\d+)?)", normalized)
    if not match:
        return []
    exact = match.group(1)
    variants = {exact, exact.replace("-", "_"), exact.replace("_", "-")}
    return sorted(value.casefold() for value in variants)


def document_index_state(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    latest = connection.execute(
        "SELECT run_id,status,scan_path,files_seen,document_files,errors,completed_at "
        "FROM scan_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    meta = dict(connection.execute("SELECT key,value FROM meta"))
    return {"latest_scan": dict(latest) if latest else {}, "meta": meta}


def local_document_candidates(
    objects: dict[str, dict[str, Any]],
    resource_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{DOCUMENT_INDEX}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    state = document_index_state(connection)
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    resource_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in resource_rows:
        if resource["resource_type"] == "text":
            resource_by_sha[str(resource["sha256"])].append(resource)

    def unit_links(document_id: str) -> str:
        rows = connection.execute(
            "SELECT unit,relation_state,reconciliation_needed FROM unit_links "
            "WHERE document_id=? ORDER BY unit",
            (document_id,),
        ).fetchall()
        return "; ".join(
            f"{row['unit']}:{row['relation_state']}:reconcile={row['reconciliation_needed']}"
            for row in rows
        )

    for object_id, item in objects.items():
        inventory = str(item.get("object_inventory_number") or "")
        tokens = inventory_tokens(inventory)
        found: dict[str, sqlite3.Row] = {}
        if tokens:
            clauses = " OR ".join("lower(filename) LIKE ?" for _ in tokens)
            params = [f"%{token}%" for token in tokens]
            for row in connection.execute(
                "SELECT * FROM documents WHERE observed_state='present' AND extension='.pdf' AND ("
                + clauses
                + ") ORDER BY source_path",
                params,
            ):
                found[str(row["document_id"])] = row
        for sha, resources in resource_by_sha.items():
            if any(str(resource["md_object_id"]) == object_id for resource in resources):
                for row in connection.execute(
                    "SELECT * FROM documents WHERE observed_state='present' AND extension='.pdf' "
                    "AND content_sha256=? ORDER BY source_path",
                    (sha,),
                ):
                    found[str(row["document_id"])] = row
        for row in found.values():
            document_id = str(row["document_id"])
            path = Path(str(row["source_path"]))
            sha = str(row["content_sha256"] or "")
            matching_resources = [
                resource for resource in resource_by_sha.get(sha, [])
                if str(resource["md_object_id"]) == object_id
            ]
            basis = "exact_md_resource_sha256" if matching_resources else "inventory_filename"
            current_state = "missing"
            page_count = ""
            error = "indexed path is not currently a file"
            if path.is_file():
                if path.stat().st_size != int(row["size_bytes"]):
                    current_state, error = "changed_since_index", "current size differs from document index"
                else:
                    current_state, page_count, error = pdf_technical(path)
            key = (object_id, document_id)
            rows_by_key[key] = {
                "md_object_id": object_id,
                "inventory": inventory,
                "document_id": document_id,
                "source_path": str(path),
                "filename": row["filename"],
                "extension": row["extension"],
                "size_bytes": row["size_bytes"],
                "indexed_sha256": sha,
                "match_basis": basis,
                "matching_md_resource_ids": "; ".join(str(resource["resource_id"]) for resource in matching_resources),
                "pdf_state": current_state,
                "page_count": page_count,
                "technical_error": error,
                "extraction_state": row["extraction_state"],
                "unit_links": unit_links(document_id),
                "candidate_role": "local_pdf_master_candidate" if current_state == "readable" else "integrity_exception_not_candidate",
                "decision_state": "unresolved" if current_state == "readable" else "excluded_pending_integrity_review",
                "deletion_authorized": "false",
            }

    title_leads: list[dict[str, Any]] = []
    for object_id, item in objects.items():
        if any(key[0] == object_id for key in rows_by_key):
            continue
        title = " ".join(str(item.get("object_name") or "").split())
        words = re.findall(r"[^\W\d_]{4,}", title.casefold(), flags=re.UNICODE)
        if not words:
            continue
        query = " AND ".join(f'"{word}"' for word in words[:8])
        try:
            hits = connection.execute(
                "SELECT d.document_id,d.source_path,d.filename,d.extension,d.size_bytes,d.content_sha256,"
                "d.extraction_state FROM document_fts f JOIN documents d ON d.document_id=f.document_id "
                "WHERE document_fts MATCH ? AND d.observed_state='present' AND d.source_path LIKE ? LIMIT 20",
                (query, "%/restore/archive/WF/%"),
            ).fetchall()
        except sqlite3.OperationalError:
            hits = []
        for row in hits:
            title_leads.append(
                {
                    "md_object_id": object_id,
                    "inventory": item.get("object_inventory_number", ""),
                    "title": title,
                    "document_id": row["document_id"],
                    "source_path": row["source_path"],
                    "filename": row["filename"],
                    "extension": row["extension"],
                    "size_bytes": row["size_bytes"],
                    "indexed_sha256": row["content_sha256"],
                    "extraction_state": row["extraction_state"],
                    "evidence_state": "exact_title_fts_lead_only",
                    "decision_state": "not_a_master_candidate",
                    "deletion_authorized": "false",
                }
            )
    connection.close()
    return (
        sorted(rows_by_key.values(), key=lambda row: (int(row["md_object_id"]), row["source_path"].casefold())),
        sorted(title_leads, key=lambda row: (int(row["md_object_id"]), row["source_path"].casefold())),
        state,
    )


def visual_leads(resource_rows: list[dict[str, Any]], cache: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if str(VISUAL_TOOLS) not in sys.path:
        sys.path.insert(0, str(VISUAL_TOOLS))
    import visual_index  # type: ignore

    connection = sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    latest = connection.execute(
        "SELECT run_id,status,completed_at,images_seen,indexed,errors FROM index_runs "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assets = connection.execute(
        """SELECT a.asset_id,r.root_path,a.relative_path,a.original_filename,a.extension,
                  a.size_bytes,a.width_px,a.height_px,
                  max(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex END) ahash,
                  max(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex END) dhash,
                  max(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex END) phash
           FROM visual_assets a JOIN visual_roots r USING(root_id)
           JOIN visual_features f USING(asset_id)
           WHERE a.status='indexed' AND r.root_path=?
             AND f.extractor_id IN ('ahash-64-v2','dhash-64-v2','phash-dct-64-v2')
           GROUP BY a.asset_id HAVING count(DISTINCT f.extractor_id)=3""",
        (str(SOURCE_ROOT),),
    ).fetchall()
    connection.close()
    rows: list[dict[str, Any]] = []
    for resource in resource_rows:
        if resource["resource_type"] != "image":
            continue
        preview, _elapsed = visual_index.normalized_preview(Path(resource["work_path"]), cache, 45)
        hashes = visual_index.feature_hashes(preview)
        ranked: list[tuple[int, int, int, int, sqlite3.Row]] = []
        for asset in assets:
            ahash = visual_index.hamming_distance(hashes["ahash_64"], asset["ahash"])
            dhash = visual_index.hamming_distance(hashes["dhash_64"], asset["dhash"])
            phash = visual_index.hamming_distance(hashes["phash_dct_64"], asset["phash"])
            ranked.append((ahash + dhash + phash, phash, dhash, ahash, asset))
        ranked.sort(key=lambda value: (value[0], value[1], value[2], value[3], value[4]["relative_path"]))
        for rank, (total, phash, dhash, ahash, asset) in enumerate(ranked[:10], 1):
            evidence = "strong_visual_candidate" if total <= 15 else "visual_lead_only"
            rows.append(
                {
                    "md_object_id": resource["md_object_id"],
                    "inventory": resource["inventory"],
                    "resource_id": resource["resource_id"],
                    "rank": rank,
                    "source_path": str(Path(asset["root_path"]) / asset["relative_path"]),
                    "filename": asset["original_filename"],
                    "extension": asset["extension"],
                    "size_bytes": asset["size_bytes"],
                    "width_px": asset["width_px"],
                    "height_px": asset["height_px"],
                    "distance_total": total,
                    "distance_ahash": ahash,
                    "distance_dhash": dhash,
                    "distance_phash": phash,
                    "evidence_state": evidence,
                    "decision_state": "unresolved" if evidence == "strong_visual_candidate" else "lead_only",
                    "deletion_authorized": "false",
                }
            )
    state = {"latest_run": dict(latest) if latest else {}, "usb_assets_compared": len(assets)}
    return rows, state


def relationship_rows(
    objects: dict[str, dict[str, Any]],
    resource_rows: list[dict[str, Any]],
    local_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    inventories = {
        object_id: inventory_tokens(str(item.get("object_inventory_number") or ""))
        for object_id, item in objects.items()
    }
    exact = {object_id: tokens[0] for object_id, tokens in inventories.items() if tokens}
    for parent_id, parent in exact.items():
        if re.search(r"[-_]\d+$", parent):
            continue
        for child_id, child in exact.items():
            if child_id != parent_id and re.fullmatch(re.escape(parent) + r"[-_]\d+", child):
                rows.append(
                    {
                        "left_md_object_id": parent_id,
                        "right_md_object_id": child_id,
                        "relationship": "inventory_parent_component_overlap",
                        "evidence": f"{parent} -> {child}",
                        "decision_state": "pending_identity_review",
                        "deletion_authorized": "false",
                    }
                )
    resources_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for resource in resource_rows:
        resources_by_sha[str(resource["sha256"])].append(resource)
    for sha, matches in resources_by_sha.items():
        object_ids = sorted({str(row["md_object_id"]) for row in matches}, key=int)
        for index, left in enumerate(object_ids):
            for right in object_ids[index + 1 :]:
                rows.append(
                    {
                        "left_md_object_id": left,
                        "right_md_object_id": right,
                        "relationship": "shared_md_resource_sha256",
                        "evidence": sha,
                        "decision_state": "pending_identity_review",
                        "deletion_authorized": "false",
                    }
                )
    local_by_sha: dict[str, set[str]] = defaultdict(set)
    for row in local_rows:
        if row["indexed_sha256"]:
            local_by_sha[str(row["indexed_sha256"])].add(str(row["md_object_id"]))
    for sha, ids in local_by_sha.items():
        ordered = sorted(ids, key=int)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                rows.append(
                    {
                        "left_md_object_id": left,
                        "right_md_object_id": right,
                        "relationship": "shared_local_candidate_sha256",
                        "evidence": sha,
                        "decision_state": "pending_identity_review",
                        "deletion_authorized": "false",
                    }
                )
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["left_md_object_id"], row["right_md_object_id"], row["relationship"], row["evidence"])
        unique[key] = row
    return sorted(unique.values(), key=lambda row: (int(row["left_md_object_id"]), int(row["right_md_object_id"]), row["relationship"]))


def prepare(capture: Path, output: Path) -> dict[str, Any]:
    mount = source_guard()
    objects = load_capture(capture)
    if output.exists():
        raise PrepareError(f"refusing to replace reconciliation output: {output}")
    stage = output.with_name(f".{output.name}.partial-{uuid.uuid4().hex}")
    stage.mkdir(parents=True)
    reference_root = stage / "md-reference"
    reference_root.mkdir()
    resource_rows: list[dict[str, Any]] = []
    last_request = 0.0
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        for position, resource in enumerate(item.get("object_images") or [], 1):
            resource_id = str(resource.get("quell_id") or "")
            suffix = resource_suffix(resource)
            target = reference_root / f"md-{object_id}-resource-{resource_id}{suffix}"
            url = resource_url(resource)
            last_request = download(url, target, last_request)
            if suffix == ".pdf":
                state, pages, error = pdf_technical(target)
                width = height = ""
                if state != "readable":
                    raise PrepareError(f"downloaded MD PDF failed validation: {target}: {error}")
            else:
                width, height, error = image_technical(target)
                pages = ""
                state = "readable" if width and height else "unreadable"
                if state != "readable":
                    raise PrepareError(f"downloaded MD image failed validation: {target}: {error}")
            resource_rows.append(
                {
                    "md_object_id": object_id,
                    "inventory": item.get("object_inventory_number", ""),
                    "resource_position": position,
                    "resource_id": resource_id,
                    "resource_type": str(resource.get("type") or "").casefold(),
                    "is_main": "true" if str(resource.get("is_main") or "") == "j" else "false",
                    "name": resource.get("name", ""),
                    "description": resource.get("beschreibung", ""),
                    "creator": resource.get("creator", ""),
                    "owner": resource.get("owner", ""),
                    "rights": resource.get("rights", ""),
                    "source_url": url,
                    "work_path": str(target),
                    "format": suffix.lstrip("."),
                    "size_bytes": target.stat().st_size,
                    "page_count": pages,
                    "width_px": width,
                    "height_px": height,
                    "sha256": sha256_file(target),
                    "technical_state": state,
                    "representation_role": "md_main_projection" if str(resource.get("is_main") or "") == "j" else "md_related_component_or_view",
                    "deletion_authorized": "false",
                }
            )
            print(f"md-resources={len(resource_rows)}/{EXPECTED_RESOURCES}", flush=True)

    local_rows, title_rows, document_state = local_document_candidates(objects, resource_rows)
    visual_rows, visual_state = visual_leads(resource_rows, stage / ".visual-cache")
    for resource in resource_rows:
        resource["work_path"] = str(output / "md-reference" / Path(resource["work_path"]).name)
    relationship_rows_value = relationship_rows(objects, resource_rows, local_rows)

    local_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in local_rows:
        local_by_object[str(row["md_object_id"])].append(row)
    visual_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in visual_rows:
        visual_by_object[str(row["md_object_id"])].append(row)

    master_candidates: list[dict[str, Any]] = []
    for resource in resource_rows:
        if resource["is_main"] != "true":
            continue
        object_id = str(resource["md_object_id"])
        resource_count = sum(str(row["md_object_id"]) == object_id for row in resource_rows)
        master_candidates.append(
            {
                "candidate_id": "",
                "md_object_id": object_id,
                "inventory": resource["inventory"],
                "candidate_path": resource["work_path"],
                "source_kind": "museum-digital_reference",
                "candidate_role": "preservation_surrogate_candidate" if resource_count == 1 else "main_projection_not_sufficient_until_multipart_review",
                "match_basis": "captured_md_main_resource",
                "format": resource["format"],
                "size_bytes": resource["size_bytes"],
                "page_count": resource["page_count"],
                "width_px": resource["width_px"],
                "height_px": resource["height_px"],
                "known_sha256": resource["sha256"],
                "unit_links": "",
                "decision_state": "unresolved",
                "deletion_authorized": "false",
            }
        )
    for row in local_rows:
        if row["pdf_state"] != "readable":
            continue
        master_candidates.append(
            {
                "candidate_id": "",
                "md_object_id": row["md_object_id"],
                "inventory": row["inventory"],
                "candidate_path": row["source_path"],
                "source_kind": "read_only_local_document_index",
                "candidate_role": "local_pdf_master_candidate",
                "match_basis": row["match_basis"],
                "format": "pdf",
                "size_bytes": row["size_bytes"],
                "page_count": row["page_count"],
                "width_px": "",
                "height_px": "",
                "known_sha256": row["indexed_sha256"],
                "unit_links": row["unit_links"],
                "decision_state": "unresolved",
                "deletion_authorized": "false",
            }
        )
    for row in visual_rows:
        if row["evidence_state"] != "strong_visual_candidate":
            continue
        master_candidates.append(
            {
                "candidate_id": "",
                "md_object_id": row["md_object_id"],
                "inventory": row["inventory"],
                "candidate_path": row["source_path"],
                "source_kind": "read_only_usb_visual_index",
                "candidate_role": "local_image_master_candidate",
                "match_basis": f"visual_hash_total={row['distance_total']}",
                "format": row["extension"],
                "size_bytes": row["size_bytes"],
                "page_count": "",
                "width_px": row["width_px"],
                "height_px": row["height_px"],
                "known_sha256": "",
                "unit_links": "",
                "decision_state": "unresolved",
                "deletion_authorized": "false",
            }
        )
    master_candidates.sort(key=lambda row: (int(row["md_object_id"]), row["source_kind"], row["candidate_path"].casefold()))
    counts: dict[str, int] = defaultdict(int)
    for row in master_candidates:
        counts[str(row["md_object_id"])] += 1
        row["candidate_id"] = f"md-{row['md_object_id']}-candidate-{counts[str(row['md_object_id'])]:03d}"

    object_rows: list[dict[str, Any]] = []
    scenario_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        resources = [row for row in resource_rows if str(row["md_object_id"]) == object_id]
        local = local_by_object[object_id]
        images = [row for row in resources if row["resource_type"] == "image"]
        strong_visual = [row for row in visual_by_object[object_id] if row["evidence_state"] == "strong_visual_candidate"]
        viable_local = [row for row in local if row["pdf_state"] == "readable"]
        local_hashes = {str(row["indexed_sha256"]) for row in viable_local if row["indexed_sha256"]}
        md_total_pages = sum(int(row["page_count"] or 0) for row in resources if row["resource_type"] == "text")
        whole_candidates = sorted({int(row["page_count"]) for row in viable_local if row["page_count"] and int(row["page_count"]) == md_total_pages})
        if object_id == "64484":
            scenario = "incomplete_documentary_aggregate_review"
            basis = "MD exposes one image while its metadata describes a set of documents"
        elif images and strong_visual:
            scenario = "local_image_original_review"
            basis = f"{len(strong_visual)} strong USB visual candidate occurrence(s)"
        elif images:
            scenario = "md_image_surrogate_review"
            basis = "no strong local visual candidate in the current USB index"
        elif len(resources) > 1 and whole_candidates:
            scenario = "multipart_whole_carrier_review"
            basis = f"local readable PDF page count equals MD resource-page sum {md_total_pages}; verify sequence/content"
        elif len(resources) > 1:
            scenario = "multipart_identity_review"
            basis = "multiple MD resources; do not select one incomplete part automatically"
        elif viable_local and len(local_hashes) > 1:
            scenario = "pdf_variant_master_review"
            basis = f"{len(local_hashes)} distinct readable local PDF hashes"
        elif viable_local:
            scenario = "pdf_local_master_review"
            basis = f"{len(viable_local)} readable local PDF occurrence(s)"
        else:
            scenario = "md_pdf_surrogate_review"
            basis = "no inventory-bound readable local PDF candidate in the current document index"
        object_candidates = [row for row in master_candidates if str(row["md_object_id"]) == object_id]
        object_rows.append(
            {
                "md_object_id": object_id,
                "inventory": item.get("object_inventory_number", ""),
                "title": item.get("object_name", ""),
                "object_type": item.get("object_type", ""),
                "description": item.get("object_description", ""),
                "material_technique": item.get("object_material_technique", ""),
                "dimensions": item.get("object_dimensions", ""),
                "date": date_values(item),
                "object_url": f"https://berlin.museum-digital.de/object/{object_id}",
                "resource_count": len(resources),
                "local_document_occurrences": len(local),
                "local_document_hash_variants": len(local_hashes),
                "master_candidate_count": len(object_candidates),
                "preservation_scenario": scenario,
                "master_decision_state": "unresolved",
                "deletion_authorized": "false",
            }
        )
        scenario_rows.append(
            {
                "md_object_id": object_id,
                "inventory": item.get("object_inventory_number", ""),
                "scenario": scenario,
                "basis": basis,
                "required_human_decision": "select one complete master or leave unresolved",
                "decision_state": "pending_human_review",
                "deletion_authorized": "false",
            }
        )
        decision_rows.append(
            {
                "md_object_id": object_id,
                "inventory": item.get("object_inventory_number", ""),
                "decision_state": "unresolved",
                "selected_candidate_id": "",
                "representation_note": "",
                "decision_note": "",
                "actor": "",
                "decided_at": "",
                "deletion_authorized": "false",
            }
        )
        for candidate in object_candidates:
            disposition_rows.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "md_object_id": object_id,
                    "candidate_path": candidate["candidate_path"],
                    "disposition": "pending",
                    "reason": "awaiting human master decision",
                    "deletion_authorized": "false",
                }
            )

    exception_rows = [
        {
            "md_object_id": row["md_object_id"],
            "inventory": row["inventory"],
            "document_id": row["document_id"],
            "source_path": row["source_path"],
            "indexed_sha256": row["indexed_sha256"],
            "integrity_state": row["pdf_state"],
            "technical_error": row["technical_error"],
            "disposition": "hold_outside_master_and_drain_selection",
            "deletion_authorized": "false",
        }
        for row in local_rows
        if row["pdf_state"] != "readable"
    ]
    documentation_rows: list[dict[str, Any]] = []

    atomic_csv(stage / "objects.csv", list(object_rows[0]), object_rows)
    atomic_csv(stage / "md-resources.csv", list(resource_rows[0]), resource_rows)
    atomic_csv(stage / "local-document-candidates.csv", list(local_rows[0]), local_rows)
    atomic_csv(
        stage / "title-leads.csv",
        list(title_rows[0]) if title_rows else [
            "md_object_id", "inventory", "title", "document_id", "source_path", "filename",
            "extension", "size_bytes", "indexed_sha256", "extraction_state", "evidence_state",
            "decision_state", "deletion_authorized",
        ],
        title_rows,
    )
    atomic_csv(stage / "visual-leads.csv", list(visual_rows[0]), visual_rows)
    atomic_csv(stage / "master-candidates.csv", list(master_candidates[0]), master_candidates)
    atomic_csv(stage / "master-decisions.csv", list(decision_rows[0]), decision_rows)
    atomic_csv(stage / "candidate-dispositions.csv", list(disposition_rows[0]), disposition_rows)
    atomic_csv(stage / "preservation-scenarios.csv", list(scenario_rows[0]), scenario_rows)
    atomic_csv(
        stage / "object-relationships.csv",
        list(relationship_rows_value[0]) if relationship_rows_value else [
            "left_md_object_id", "right_md_object_id", "relationship", "evidence",
            "decision_state", "deletion_authorized",
        ],
        relationship_rows_value,
    )
    atomic_csv(
        stage / "integrity-exceptions.csv",
        list(exception_rows[0]) if exception_rows else [
            "md_object_id", "inventory", "document_id", "source_path", "indexed_sha256",
            "integrity_state", "technical_error", "disposition", "deletion_authorized",
        ],
        exception_rows,
    )
    atomic_csv(
        stage / "documentation-candidates.csv",
        ["candidate_path", "binding_basis", "decision_state", "note", "deletion_authorized"],
        documentation_rows,
    )

    scenario_counts = Counter(row["scenario"] for row in scenario_rows)
    summary = {
        "format": "iss-collection-553-reconciliation-preparation-v1",
        "created_at": now(),
        "result": "ok",
        "museum_digital_collection": COLLECTION_ID,
        "objects": len(object_rows),
        "resources": len(resource_rows),
        "pdf_resources": sum(row["resource_type"] == "text" for row in resource_rows),
        "image_resources": sum(row["resource_type"] == "image" for row in resource_rows),
        "local_document_occurrences": len(local_rows),
        "local_document_hash_variants": len({row["indexed_sha256"] for row in local_rows if row["indexed_sha256"]}),
        "objects_with_local_document_candidates": len({row["md_object_id"] for row in local_rows}),
        "title_leads_only": len(title_rows),
        "visual_leads": len(visual_rows),
        "strong_visual_leads": sum(row["evidence_state"] == "strong_visual_candidate" for row in visual_rows),
        "master_candidates": len(master_candidates),
        "pending_master_decisions": len(decision_rows),
        "integrity_exceptions": len(exception_rows),
        "relationship_rows": len(relationship_rows_value),
        "documentation_candidates": 0,
        "preservation_scenarios": dict(sorted(scenario_counts.items())),
        "source_mount": mount,
        "document_index": {"path": str(DOCUMENT_INDEX), **document_state},
        "visual_index": {"path": str(VISUAL_INDEX), **visual_state},
        "inputs": {"museum_digital_capture": str(capture)},
        "source_files_modified": 0,
        "published_unit_writes": 0,
        "master_selections_made": 0,
        "derivatives_generated": 0,
        "accession_authorized": False,
        "deletion_authorized": False,
    }
    atomic_json(stage / "preparation-summary.json", summary)
    atomic_bytes(
        stage / "00_README.md",
        (
            "# Archiv WF collection 553 reconciliation preparation\n\n"
            "Work-only evidence for the current museum-digital collection. Exactly 54 object "
            "records and 66 linked resources were captured. No master has been selected, no "
            "accession has been authorized, and deletion remains unauthorized.\n\n"
            "- `objects.csv`: MD object metadata and unresolved scenario assignment.\n"
            "- `md-resources.csv`: all downloaded MD references with technical metadata and SHA-256.\n"
            "- `local-document-candidates.csv`: inventory-bound or exact-hash PDF occurrences from the read-only document index.\n"
            "- `title-leads.csv`: title-only OCR/path leads; these are not master candidates.\n"
            "- `visual-leads.csv`: top ten USB visual-index comparisons for each MD image.\n"
            "- `master-candidates.csv`: viable MD fallbacks and local candidates, all unresolved.\n"
            "- `preservation-scenarios.csv`: format/identity scenario for human review.\n"
            "- `object-relationships.csv`: parent/component or shared-file identity warnings.\n"
            "- `integrity-exceptions.csv`: unreadable or changed local PDFs held outside master and drain selection.\n"
            "- `documentation-candidates.csv`: intentionally empty; no collection-bound supporting document was proven.\n"
            "- `master-decisions.csv` and `candidate-dispositions.csv`: unresolved decision templates.\n\n"
            "Catalogued PDFs and scans are object payloads, not supporting documentation. The protected "
            "Elements source was verified as `/dev/sdf1`, `ntfs3`, read-only. USB files were not changed.\n"
        ).encode("utf-8"),
    )
    os.replace(stage, output)
    return summary


def repair_work_paths(output: Path) -> dict[str, Any]:
    """Repair the initial run's staging-path projection without touching payloads."""

    source_guard()
    resource_csv = output / "md-resources.csv"
    candidate_csv = output / "master-candidates.csv"
    receipt = output / "path-repair-receipt.json"
    if receipt.exists():
        raise PrepareError(f"path repair was already recorded: {receipt}")
    if not resource_csv.is_file() or not candidate_csv.is_file():
        raise PrepareError("reconciliation CSVs required for path repair are missing")

    def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), [dict(row) for row in reader]

    resource_fields, resources = read_rows(resource_csv)
    candidate_fields, candidates = read_rows(candidate_csv)
    if len(resources) != EXPECTED_RESOURCES:
        raise PrepareError(f"expected {EXPECTED_RESOURCES} resource rows, found {len(resources)}")
    mapping: dict[str, str] = {}
    for row in resources:
        old = str(row.get("work_path") or "")
        old_path = Path(old)
        if ".unit-reconciliation-v1.partial-" not in old:
            raise PrepareError(f"resource does not contain the known staging-path defect: {old}")
        new_path = output / "md-reference" / old_path.name
        if not new_path.is_file() or new_path.stat().st_size != int(row["size_bytes"]):
            raise PrepareError(f"published reference identity does not match CSV: {new_path}")
        mapping[old] = str(new_path)
        row["work_path"] = str(new_path)
    repaired_candidates = 0
    for row in candidates:
        old = str(row.get("candidate_path") or "")
        if old in mapping:
            row["candidate_path"] = mapping[old]
            repaired_candidates += 1
    if repaired_candidates != EXPECTED_OBJECTS:
        raise PrepareError(
            f"expected {EXPECTED_OBJECTS} MD master-candidate paths, repaired {repaired_candidates}"
        )
    atomic_replace_bytes(resource_csv, csv_payload(resource_fields, resources))
    atomic_replace_bytes(candidate_csv, csv_payload(candidate_fields, candidates))
    result = {
        "format": "iss-collection-553-work-path-repair-v1",
        "created_at": now(),
        "result": "ok",
        "reason": "atomic staging directory was renamed after CSV paths were projected",
        "resource_paths_repaired": len(resources),
        "master_candidate_paths_repaired": repaired_candidates,
        "payload_files_changed": 0,
        "source_files_modified": 0,
        "master_selections_made": 0,
        "deletion_authorized": False,
    }
    atomic_json(receipt, result)
    return result


def repair_disposition_paths(output: Path) -> dict[str, Any]:
    """Align pending disposition paths with the already repaired candidates."""

    source_guard()
    candidate_csv = output / "master-candidates.csv"
    disposition_csv = output / "candidate-dispositions.csv"
    receipt = output / "candidate-disposition-path-repair-receipt.json"
    if receipt.exists():
        raise PrepareError(f"candidate disposition repair was already recorded: {receipt}")

    def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), [dict(row) for row in reader]

    _candidate_fields, candidates = read_rows(candidate_csv)
    disposition_fields, dispositions = read_rows(disposition_csv)
    candidate_paths = {row["candidate_id"]: row["candidate_path"] for row in candidates}
    if len(candidates) != len(dispositions) or len(candidate_paths) != len(candidates):
        raise PrepareError("candidate and disposition projections do not have one-to-one identity")
    repaired = 0
    for row in dispositions:
        expected = candidate_paths.get(row["candidate_id"])
        if expected is None:
            raise PrepareError(f"foreign disposition candidate: {row['candidate_id']}")
        if row["candidate_path"] != expected:
            if ".unit-reconciliation-v1.partial-" not in row["candidate_path"]:
                raise PrepareError(f"unexpected disposition path disagreement: {row['candidate_id']}")
            row["candidate_path"] = expected
            repaired += 1
    if repaired != EXPECTED_OBJECTS:
        raise PrepareError(f"expected {EXPECTED_OBJECTS} staging paths, repaired {repaired}")
    atomic_replace_bytes(disposition_csv, csv_payload(disposition_fields, dispositions))
    result = {
        "format": "iss-collection-553-candidate-disposition-path-repair-v1",
        "created_at": now(),
        "result": "ok",
        "reason": "pending disposition paths retained the pre-publication staging name",
        "candidate_disposition_paths_repaired": repaired,
        "decision_values_changed": 0,
        "payload_files_changed": 0,
        "source_files_modified": 0,
        "master_selections_made": 0,
        "deletion_authorized": False,
    }
    atomic_json(receipt, result)
    return result


def check(capture: Path) -> dict[str, Any]:
    mount = source_guard()
    objects = load_capture(capture)
    missing = [str(path) for path in (DOCUMENT_INDEX, VISUAL_INDEX) if not path.is_file()]
    if missing:
        raise PrepareError(f"required indexes missing: {missing}")
    if subprocess.run(["pdfinfo", "-v"], capture_output=True).returncode not in {0, 99}:
        raise PrepareError("pdfinfo is not usable")
    return {
        "result": "ok",
        "objects": len(objects),
        "resources": sum(len(item.get("object_images") or []) for item in objects.values()),
        "source_mount": mount,
        "output_exists": DEFAULT_OUTPUT.exists(),
        "writes_performed": 0,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--capture", type=Path, required=True)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--check", action="store_true")
    result.add_argument("--repair-work-paths", action="store_true")
    result.add_argument("--repair-disposition-paths", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    capture = args.capture.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if not output.is_relative_to(WORK_ROOT.resolve()):
        raise PrepareError("output must remain inside collection-553 work root")
    actions = sum((args.check, args.repair_work_paths, args.repair_disposition_paths))
    if actions > 1:
        raise PrepareError("choose only one check or repair action")
    if args.check:
        result = check(capture)
    elif args.repair_work_paths:
        result = repair_work_paths(output)
    elif args.repair_disposition_paths:
        result = repair_disposition_paths(output)
    else:
        result = prepare(capture, output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
