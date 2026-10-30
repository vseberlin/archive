#!/usr/bin/env python3
"""Prepare a guarded reconciliation workbench for museum-digital collection 551.

The script writes only below the collection-551 work root. It captures all live
museum-digital resources and metadata, queries the protected local indexes and
source read-only, records advisory Master proposals, and creates a no-delete
drain projection. It never confirms a Master, accessions a unit, edits source files,
or authorizes deletion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps


COLLECTION_ID = 551
EXPECTED_OBJECTS = 79
EXPECTED_RESOURCES = 112
EXPECTED_PDFS = 106
EXPECTED_IMAGES = 6
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-551")
SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_SOURCE = SOURCE_ROOT / "restore/archive"
DOCUMENT_INDEX = Path("/mnt/volume/iss-archive-work/document-index/document-index.sqlite")
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
VISUAL_TOOLS = Path("/home/vladimir/wp/tools/archive-preservation")
DEFAULT_OUTPUT = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
QUERY_URL = "https://berlin.museum-digital.de/export/json/collection:551?limit=100&offset=0"
USER_AGENT = "Industriesalon-Archive-Reconciliation/1.0 (collection 551 proposal; industriesalon.de)"
MIN_REQUEST_DELAY = 0.65
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
RETRYABLE = {429, 500, 502, 503, 504}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
COLLECTION_SOURCE_DIRS = (
    ARCHIVE_SOURCE / "WF/Forschungs- und Entwicklungsberichte",
    ARCHIVE_SOURCE / "WF/Namenssuche+WF-Sender/Forschungs- und Entwicklungsberichte",
)
COMPOUND_MD_SELECTION = "__ordered_md_resource_set__"


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


def csv_payload(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PrepareError(f"refusing to replace evidence: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    atomic_bytes(path, csv_payload(fields, rows))


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


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


def request_bytes(url: str, accept: str, last_request: float) -> tuple[bytes, float, str]:
    for attempt in range(4):
        delay = MIN_REQUEST_DELAY - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                last_request = time.monotonic()
                announced = int(response.headers.get("Content-Length") or 0)
                if announced > MAX_DOWNLOAD_BYTES:
                    raise PrepareError(f"resource exceeds 512 MiB limit: {url}")
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
                if len(payload) > MAX_DOWNLOAD_BYTES:
                    raise PrepareError(f"resource exceeds 512 MiB limit: {url}")
                if not payload:
                    raise PrepareError(f"empty response: {url}")
                return payload, last_request, str(response.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as error:
            last_request = time.monotonic()
            if error.code not in RETRYABLE or attempt == 3:
                raise PrepareError(f"request failed for {url}: HTTP {error.code}") from error
            time.sleep(min(retry_after(error, 2**attempt), 30))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_request = time.monotonic()
            if attempt == 3:
                raise PrepareError(f"request failed for {url}: {error}") from error
            time.sleep(min(2**attempt, 20))
    raise PrepareError(f"request retries exhausted: {url}")


def live_objects() -> tuple[dict[str, dict[str, Any]], bytes, float]:
    payload, last_request, _content_type = request_bytes(QUERY_URL, "application/json", 0.0)
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise PrepareError("museum-digital export did not return JSON") from error
    results = decoded.get("results") or {}
    raw_objects = results.get("objects") or {}
    values = raw_objects.values() if isinstance(raw_objects, dict) else raw_objects
    objects: dict[str, dict[str, Any]] = {}
    for item in values:
        object_id = str(item.get("object_id") or "")
        if not object_id or object_id in objects:
            raise PrepareError(f"missing or duplicate museum-digital object id: {object_id}")
        objects[object_id] = item
    resources = [row for item in objects.values() for row in item.get("object_images") or []]
    types = Counter(str(row.get("type") or "").casefold() for row in resources)
    inventories = [str(item.get("object_inventory_number") or "") for item in objects.values()]
    if results.get("total") != EXPECTED_OBJECTS or len(objects) != EXPECTED_OBJECTS:
        raise PrepareError(f"live collection scope changed: total={results.get('total')} captured={len(objects)}")
    if len(resources) != EXPECTED_RESOURCES or types != Counter({"text": EXPECTED_PDFS, "image": EXPECTED_IMAGES}):
        raise PrepareError(f"live resource scope changed: count={len(resources)} types={dict(types)}")
    if len(set(inventories)) != EXPECTED_OBJECTS or any(not value for value in inventories):
        raise PrepareError("collection does not have 79 unique non-empty inventory numbers")
    for object_id, item in objects.items():
        collections = [str(row.get("collection_id") or "") for row in item.get("object_collection") or []]
        if collections != [str(COLLECTION_ID)]:
            raise PrepareError(f"MD {object_id} has unexpected collection membership: {collections}")
        resources_for_object = item.get("object_images") or []
        if sum(str(row.get("is_main") or "") == "j" for row in resources_for_object) != 1:
            raise PrepareError(f"MD {object_id} does not have exactly one main resource")
    return objects, payload, last_request


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
    suffix = Path(urllib.parse.urlparse(str(resource.get("filename_loc") or "")).path).suffix.casefold()
    if str(resource.get("type") or "").casefold() == "text":
        return ".pdf"
    return suffix if suffix in IMAGE_SUFFIXES else ".jpg"


def pdf_technical(path: Path) -> tuple[str, str, str]:
    if path.open("rb").read(5) != b"%PDF-":
        return "invalid_signature", "", "missing PDF signature"
    try:
        result = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=45)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return "unreadable", "", str(error)
    if result.returncode:
        return "unreadable", "", " ".join((result.stderr or result.stdout).split())[:500]
    match = re.search(r"^Pages:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    return "readable", match.group(1) if match else "", ""


def image_technical(path: Path) -> tuple[str, str, str]:
    try:
        with Image.open(path) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        return str(width), str(height), ""
    except (OSError, ValueError) as error:
        return "", "", str(error)


def inventory_key(value: str) -> tuple[Any, ...]:
    normalized = value.upper()
    match = re.search(r"FEB[-_ ]*0*(\d+)[-_ ]*0*(\d+)(?!\d)", normalized)
    if match:
        return ("numeric", int(match.group(1)), int(match.group(2)))
    return ("text", re.sub(r"[^A-Z0-9]", "", normalized))


def filename_keys(value: str) -> set[tuple[Any, ...]]:
    normalized = value.upper()
    keys: set[tuple[Any, ...]] = {("text", re.sub(r"[^A-Z0-9]", "", normalized))}
    for match in re.finditer(r"FEB[-_ ]*0*(\d+)[-_ ]*0*(\d+)(?!\d)", normalized):
        keys.add(("numeric", int(match.group(1)), int(match.group(2))))
    return keys


def compact(value: Any, limit: int = 4000) -> str:
    return " ".join(str(value or "").split())[:limit]


def date_values(item: dict[str, Any]) -> str:
    values: list[str] = []
    for event in item.get("object_events") or []:
        value = compact((event.get("time") or {}).get("time_name"))
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def tag_values(item: dict[str, Any]) -> str:
    values: list[str] = []
    for tag in item.get("object_tags") or []:
        value = compact(tag.get("tag_name") if isinstance(tag, dict) else tag)
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def drain_class(path: Path) -> tuple[str, str] | None:
    text = str(path).casefold()
    name = path.name.casefold()
    if name.startswith("~$") or name in {".ds_store", "thumbs.db", "desktop.ini"} or path.suffix.casefold() in {".tmp", ".temp", ".part", ".crdownload"}:
        return "transient_local_file", "temporary or application-generated filename"
    derivative_directories = (
        "/durchsuchbar/", "/dsb-wz/", "/bilddateien für md/", "/bilddateien fuer md/"
    )
    if any(value in text for value in derivative_directories):
        return "local_derivative", "known derivative directory"
    staging_directories = ("/in md importiert/", "/fertig für md-import/", "/fertig fuer md-import/")
    if any(value in text for value in staging_directories):
        return "projection_staging_file", "known museum-digital import staging directory"
    if re.search(r"md[-_ ]*import", name):
        return "projection_staging_file", "museum-digital import staging filename"
    if name in {"bilddateien für md.zip", "bilddateien fuer md.zip"}:
        return "local_derivative", "bundled museum-digital image derivatives"
    stem = path.stem.casefold()
    if re.search(r"(?:^|[-_])(dsb(?:[-_]wz)?|ocr|opt|optimized|optimiert)(?:[-_]|$)", stem):
        return "local_derivative", "derivative marker in filename"
    return None


def document_index_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{DOCUMENT_INDEX}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    latest = connection.execute(
        "SELECT run_id,status,scan_path,files_seen,document_files,errors,completed_at "
        "FROM scan_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    ids = [str(row[0]) for row in connection.execute(
        "SELECT document_id FROM document_fts WHERE document_fts MATCH 'filename:FEB'"
    )]
    path_ids = [str(row[0]) for row in connection.execute(
        "SELECT document_id FROM document_fts WHERE document_fts MATCH ?",
        ('relative_path:"Forschungs und Entwicklungsberichte"',),
    )]
    all_ids = sorted(set(ids) | set(path_ids))
    rows: list[dict[str, Any]] = []
    for index in range(0, len(all_ids), 400):
        batch = all_ids[index:index + 400]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(dict(row) for row in connection.execute(
            f"SELECT document_id,source_path,filename,extension,size_bytes,content_sha256,"
            f"observed_state,extraction_state,parser,header_text,extraction_error "
            f"FROM documents WHERE document_id IN ({placeholders})", batch
        ))
    connection.close()
    return rows, dict(latest) if latest else {}


def local_pdf_rows(
    objects: dict[str, dict[str, Any]], resources: list[dict[str, Any]], index_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    objects_by_key = {inventory_key(str(item["object_inventory_number"])): object_id for object_id, item in objects.items()}
    resources_by_object_sha: dict[tuple[str, str], list[str]] = defaultdict(list)
    for resource in resources:
        if resource["resource_type"] == "text":
            resources_by_object_sha[(resource["md_object_id"], resource["sha256"])].append(resource["resource_id"])
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in index_rows:
        if row.get("observed_state") != "present" or str(row.get("extension") or "").casefold() != ".pdf":
            continue
        path = Path(str(row["source_path"]))
        if not str(path).startswith(str(SOURCE_ROOT) + "/"):
            continue
        object_ids: set[str] = set()
        for key in filename_keys(str(row.get("filename") or "")):
            if key[0] == "numeric" and key in objects_by_key:
                object_ids.add(objects_by_key[key])
            elif key[0] == "text":
                for object_key, object_id in objects_by_key.items():
                    if object_key[0] == "text" and object_key[1] and object_key[1] in key[1]:
                        object_ids.add(object_id)
        digest = str(row.get("content_sha256") or "")
        for (object_id, sha), _resource_ids in resources_by_object_sha.items():
            if digest and digest == sha:
                object_ids.add(object_id)
        for object_id in object_ids:
            inventory = str(objects[object_id]["object_inventory_number"])
            current_state = "missing"
            page_count = ""
            technical_error = "indexed source path is missing"
            if path.is_file():
                if path.stat().st_size != int(row["size_bytes"]):
                    current_state, technical_error = "changed_since_index", "current size differs from document index"
                else:
                    current_state, page_count, technical_error = pdf_technical(path)
            matching = resources_by_object_sha.get((object_id, digest), [])
            classified = drain_class(path)
            if classified:
                role = "local_derivative_or_transient"
                eligible = "false"
            elif matching:
                role = "exact_md_resource_local_copy"
                eligible = "true"
            else:
                role = "potential_local_binary_enrichment"
                eligible = "true"
            if current_state != "readable":
                eligible = "false"
            result[(object_id, str(path))] = {
                "md_object_id": object_id,
                "inventory": inventory,
                "document_id": row["document_id"],
                "source_path": str(path),
                "filename": row["filename"],
                "size_bytes": row["size_bytes"],
                "sha256": digest,
                "match_basis": "exact_md_resource_sha256" if matching else "inventory_filename",
                "matching_md_resource_ids": json.dumps(matching),
                "pdf_state": current_state,
                "page_count": page_count,
                "extraction_state": row.get("extraction_state") or "",
                "candidate_role": role,
                "master_eligible": eligible,
                "technical_error": technical_error,
                "deletion_authorized": "false",
            }
    return sorted(result.values(), key=lambda row: (int(row["md_object_id"]), row["source_path"].casefold()))


def visual_leads(
    objects: dict[str, dict[str, Any]],
    resources: list[dict[str, Any]],
    reference_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    image_resources = [row for row in resources if row["resource_type"] == "image"]
    if not image_resources:
        return [], {}
    if str(VISUAL_TOOLS) not in sys.path:
        sys.path.insert(0, str(VISUAL_TOOLS))
    import visual_index  # type: ignore

    connection = sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    latest = connection.execute(
        "SELECT run_id,status,completed_at,images_seen,indexed,errors FROM index_runs ORDER BY started_at DESC LIMIT 1"
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
    output: list[dict[str, Any]] = []
    for resource in image_resources:
        with Image.open(reference_root / Path(resource["work_path"]).name) as opened:
            hashes = visual_index.feature_hashes(ImageOps.exif_transpose(opened))
        ranked = []
        for asset in assets:
            ahash = visual_index.hamming_distance(hashes["ahash_64"], asset["ahash"])
            dhash = visual_index.hamming_distance(hashes["dhash_64"], asset["dhash"])
            phash = visual_index.hamming_distance(hashes["phash_dct_64"], asset["phash"])
            ranked.append((ahash + dhash + phash, phash, dhash, ahash, asset))
        ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]["relative_path"]))
        for rank, (total, phash, dhash, ahash, asset) in enumerate(ranked[:10], 1):
            source_path = str(Path(asset["root_path"]) / asset["relative_path"])
            output.append({
                "md_object_id": resource["md_object_id"],
                "inventory": resource["inventory"],
                "resource_id": resource["resource_id"],
                "rank": rank,
                "source_path": source_path,
                "filename": asset["original_filename"],
                "extension": asset["extension"],
                "size_bytes": asset["size_bytes"],
                "width_px": asset["width_px"],
                "height_px": asset["height_px"],
                "distance_total": total,
                "distance_ahash": ahash,
                "distance_dhash": dhash,
                "distance_phash": phash,
                "evidence_state": "strong_visual_candidate" if total <= 15 else "visual_lead_only",
                "deletion_authorized": "false",
            })
    return output, {"latest_run": dict(latest) if latest else {}, "usb_assets_compared": len(assets)}


SHEET_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def column_number(reference: str) -> int:
    value = 0
    for character in re.match(r"[A-Z]+", reference).group():
        value = value * 26 + ord(character) - 64
    return value - 1


def normalize_header(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return re.sub(r"[^a-z0-9]+", "_", "".join(ch for ch in decomposed if not unicodedata.combining(ch))).strip("_")


def xlsx_sheets(path: Path) -> Iterable[tuple[str, list[dict[int, str]]]]:
    with ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in row.findall(".//m:t", SHEET_NS)) for row in root.findall("m:si", SHEET_NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {row.attrib["Id"]: row.attrib["Target"] for row in rel_root}
        for sheet in workbook.find("m:sheets", SHEET_NS):
            relation = sheet.attrib["{" + SHEET_NS["r"] + "}id"]
            target = relationships[relation]
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            rows: list[dict[int, str]] = []
            with archive.open(target) as stream:
                for _event, row in ET.iterparse(stream, events=("end",)):
                    if not row.tag.endswith("}row"):
                        continue
                    excel_row = int(row.attrib.get("r") or 0)
                    # Several legacy sheets carry formatting to Excel's final row.
                    # The collection data is in the leading table; stop before
                    # expanding those empty/formatted tails.
                    if excel_row > 10_000:
                        row.clear()
                        break
                    values: dict[int, str] = {-1: str(excel_row)}
                    for cell in row.findall("m:c", SHEET_NS):
                        index = column_number(cell.attrib["r"])
                        kind = cell.attrib.get("t")
                        value_node = cell.find("m:v", SHEET_NS)
                        value = ""
                        if kind == "inlineStr":
                            value = "".join(node.text or "" for node in cell.findall(".//m:t", SHEET_NS))
                        elif value_node is not None:
                            value = value_node.text or ""
                            if kind == "s" and value.isdigit() and int(value) < len(shared):
                                value = shared[int(value)]
                        if value:
                            values[index] = compact(value)
                    if len(values) > 1:
                        rows.append(values)
                    row.clear()
            yield sheet.attrib["name"], rows


def csv_rows(path: Path) -> Iterable[tuple[str, list[dict[int, str]]]]:
    payload = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    rows = [{index: compact(value) for index, value in enumerate(row)} for row in csv.reader(io.StringIO(text), dialect)]
    yield "CSV", rows


def metadata_documents(index_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    permitted = {".xlsx", ".csv"}
    rows = []
    for row in index_rows:
        path = Path(str(row["source_path"]))
        if (
            str(row.get("extension") or "").casefold() in permitted
            and any(str(path).startswith(str(root) + "/") for root in COLLECTION_SOURCE_DIRS)
            and path.is_file()
            and not path.name.startswith("~$")
        ):
            rows.append(row)
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_hash[str(row.get("content_sha256") or row["source_path"])].append(row)
    selected = []
    for digest, variants in by_hash.items():
        variants.sort(key=lambda row: ("Namenssuche+WF-Sender" in str(row["source_path"]), len(str(row["source_path"])), str(row["source_path"])))
        selected.append(variants[0])
    return sorted(selected, key=lambda row: str(row["source_path"]).casefold())


def canonical_metadata(headers: dict[int, str], values: dict[int, str]) -> dict[str, str]:
    gathered: dict[str, list[str]] = defaultdict(list)
    for index, value in values.items():
        if not value:
            continue
        header = normalize_header(headers.get(index, ""))
        if header in {"inventory_number", "inventarnummer"}:
            key = "inventory"
        elif header in {"object_title", "dokumentname", "objektname", "titel"}:
            key = "title"
        elif header in {"object_description", "beschreibung"}:
            key = "description"
        elif header in {"object_type", "objektart", "objektart1"}:
            key = "object_type"
        elif header in {"object_material_technique", "material"}:
            key = "material_technique"
        elif header in {"object_dimensions", "masse"}:
            key = "dimensions"
        elif header in {"autor", "author"}:
            key = "author"
        elif header in {"seiten", "page_count"}:
            key = "page_count"
        elif header in {"abode_regular", "verbleib_eigentlicher_standort"}:
            key = "storage_location"
        elif header.startswith("tag") or header.startswith("schlagwort"):
            key = "tags"
        elif header in {"commissioned_when1", "ereignis_wann", "date"}:
            key = "date"
        elif header in {"rights_annotation", "rights"}:
            key = "rights"
        else:
            continue
        if value not in gathered[key]:
            gathered[key].append(value)
    return {key: "; ".join(items) for key, items in gathered.items()}


def metadata_evidence(
    objects: dict[str, dict[str, Any]], index_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    object_by_inventory = {str(item["object_inventory_number"]): object_id for object_id, item in objects.items()}
    evidence: list[dict[str, Any]] = []
    documents = metadata_documents(index_rows)
    for document in documents:
        path = Path(str(document["source_path"]))
        try:
            sources = xlsx_sheets(path) if path.suffix.casefold() == ".xlsx" else csv_rows(path)
            for sheet_name, rows in sources:
                header_index = next((idx for idx, row in enumerate(rows[:12]) if any(normalize_header(value) in {"inventory_number", "inventarnummer"} for value in row.values())), None)
                if header_index is None:
                    continue
                headers = rows[header_index]
                inventory_columns = [idx for idx, value in headers.items() if normalize_header(value) in {"inventory_number", "inventarnummer"}]
                for position, row in enumerate(rows[header_index + 1:], header_index + 2):
                    inventories = [compact(row.get(index)) for index in inventory_columns]
                    inventory = next((value for value in inventories if value in object_by_inventory), "")
                    if not inventory:
                        continue
                    fields = canonical_metadata(headers, row)
                    evidence.append({
                        "md_object_id": object_by_inventory[inventory],
                        "inventory": inventory,
                        "source_path": str(path),
                        "source_sha256": document.get("content_sha256") or "",
                        "sheet": sheet_name,
                        "row_number": row.get(-1, str(position)),
                        "local_title": fields.get("title", ""),
                        "local_description": fields.get("description", ""),
                        "local_object_type": fields.get("object_type", ""),
                        "local_material_technique": fields.get("material_technique", ""),
                        "local_dimensions": fields.get("dimensions", ""),
                        "local_author": fields.get("author", ""),
                        "local_page_count": fields.get("page_count", ""),
                        "local_storage_location": fields.get("storage_location", ""),
                        "local_date": fields.get("date", ""),
                        "local_tags": fields.get("tags", ""),
                        "local_rights": fields.get("rights", ""),
                        "metadata_fields_json": json.dumps(fields, ensure_ascii=False, sort_keys=True),
                        "decision_state": "metadata_evidence_pending_field_review",
                        "deletion_authorized": "false",
                    })
        except (BadZipFile, ET.ParseError, KeyError, OSError, csv.Error) as error:
            evidence.append({
                "md_object_id": "", "inventory": "", "source_path": str(path),
                "source_sha256": document.get("content_sha256") or "", "sheet": "", "row_number": "",
                "local_title": "", "local_description": "", "local_object_type": "",
                "local_material_technique": "", "local_dimensions": "", "local_author": "",
                "local_page_count": "", "local_storage_location": "", "local_date": "",
                "local_tags": "", "local_rights": "", "metadata_fields_json": "{}",
                "decision_state": f"metadata_source_parse_error:{type(error).__name__}",
                "deletion_authorized": "false",
            })
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        if row["md_object_id"]:
            by_object[row["md_object_id"]].append(row)
    comparisons = []
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        rows = by_object[object_id]
        local_values: dict[str, list[str]] = defaultdict(list)
        field_map = {
            "title": "local_title", "description": "local_description", "object_type": "local_object_type",
            "material_technique": "local_material_technique", "dimensions": "local_dimensions",
            "author": "local_author", "page_count": "local_page_count", "storage_location": "local_storage_location",
            "date": "local_date", "tags": "local_tags", "rights": "local_rights",
        }
        for row in rows:
            for field, column in field_map.items():
                value = compact(row.get(column))
                if value and value not in local_values[field]:
                    local_values[field].append(value)
        md_values = {
            "title": compact(item.get("object_name")), "description": compact(item.get("object_description")),
            "object_type": compact(item.get("object_type")), "material_technique": compact(item.get("object_material_technique")),
            "dimensions": compact(item.get("object_dimensions")), "date": date_values(item), "tags": tag_values(item),
        }
        enrichment = []
        for field, values in local_values.items():
            normalized_md = normalize_header(md_values.get(field, ""))
            if field in {"author", "page_count", "storage_location", "rights"} or any(normalize_header(value) != normalized_md for value in values):
                enrichment.append(field)
        comparisons.append({
            "md_object_id": object_id,
            "inventory": item["object_inventory_number"],
            "metadata_evidence_rows": len(rows),
            "metadata_source_files": len({row["source_path"] for row in rows}),
            "md_values_json": json.dumps(md_values, ensure_ascii=False, sort_keys=True),
            "local_values_json": json.dumps(local_values, ensure_ascii=False, sort_keys=True),
            "local_enrichment_fields": "; ".join(sorted(set(enrichment))),
            "proposal": "merge_local_enrichment_with_md_seed" if enrichment else "accept_md_metadata_as_archive_seed",
            "decision_state": "pending_human_metadata_review" if enrichment else "md_seed_ready_for_confirmation",
            "deletion_authorized": "false",
        })
    document_rows = [{
        "source_path": str(row["source_path"]), "source_sha256": str(row.get("content_sha256") or ""),
        "size_bytes": str(row.get("size_bytes") or ""), "format": str(row.get("extension") or "").lstrip("."),
        "role": "local_metadata_evidence", "decision_state": "retain_pending_metadata_reconciliation",
        "deletion_authorized": "false",
    } for row in documents]
    return evidence, comparisons, document_rows


def prepare(output: Path) -> dict[str, Any]:
    mount = source_guard()
    objects, raw_payload, last_request = live_objects()
    if output.exists():
        raise PrepareError(f"refusing to replace reconciliation output: {output}")
    stage = output.with_name(f".{output.name}.partial-{uuid.uuid4().hex}")
    stage.mkdir(parents=True)
    try:
        reference_stage = stage / "md-reference"
        reference_stage.mkdir()
        resource_rows: list[dict[str, Any]] = []
        for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
            raw_resources = sorted(item.get("object_images") or [], key=lambda row: (int(row.get("order") or 0), int(row.get("quell_id") or 0)))
            for position, resource in enumerate(raw_resources, 1):
                resource_id = str(resource.get("quell_id") or "")
                suffix = resource_suffix(resource)
                filename = f"md-{object_id}-resource-{resource_id}{suffix}"
                stage_target = reference_stage / filename
                final_target = output / "md-reference" / filename
                url = resource_url(resource)
                payload, last_request, content_type = request_bytes(url, "application/pdf,image/*,*/*;q=0.1", last_request)
                stage_target.write_bytes(payload)
                with stage_target.open("rb") as handle:
                    os.fsync(handle.fileno())
                if suffix == ".pdf":
                    state, page_count, error = pdf_technical(stage_target)
                    width = height = ""
                    if state != "readable":
                        raise PrepareError(f"downloaded MD PDF failed validation: {url}: {error}")
                else:
                    width, height, error = image_technical(stage_target)
                    state = "readable" if width and height else "unreadable"
                    page_count = ""
                    if state != "readable":
                        raise PrepareError(f"downloaded MD image failed validation: {url}: {error}")
                resource_rows.append({
                    "md_object_id": object_id,
                    "inventory": item["object_inventory_number"],
                    "resource_position": position,
                    "resource_order": resource.get("order") or position,
                    "resource_id": resource_id,
                    "resource_type": str(resource.get("type") or "").casefold(),
                    "is_main": "true" if str(resource.get("is_main") or "") == "j" else "false",
                    "name": compact(resource.get("name")),
                    "description": compact(resource.get("beschreibung")),
                    "creator": compact(resource.get("creator")),
                    "owner": compact(resource.get("owner")),
                    "rights": compact(resource.get("rights")),
                    "source_url": url,
                    "work_path": str(final_target),
                    "format": suffix.lstrip("."),
                    "content_type": content_type,
                    "size_bytes": len(payload),
                    "page_count": page_count,
                    "width_px": width,
                    "height_px": height,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "technical_state": state,
                    "representation_role": "md_projection_resource_candidate",
                    "deletion_authorized": "false",
                })
                print(f"md-resources={len(resource_rows)}/{EXPECTED_RESOURCES}", flush=True)

        index_rows, index_state = document_index_rows()
        local_rows = local_pdf_rows(objects, resource_rows, index_rows)
        visual_rows, visual_state = visual_leads(objects, resource_rows, reference_stage)
        metadata_rows, metadata_comparisons, metadata_documents_rows = metadata_evidence(objects, index_rows)

        local_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
        visual_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
        resources_by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in local_rows:
            local_by_object[row["md_object_id"]].append(row)
        for row in visual_rows:
            visual_by_object[row["md_object_id"]].append(row)
        for row in resource_rows:
            resources_by_object[row["md_object_id"]].append(row)

        master_candidates: list[dict[str, Any]] = []
        for object_id, resources in resources_by_object.items():
            multipart = len(resources) > 1
            for resource in resources:
                master_candidates.append({
                    "candidate_id": "",
                    "md_object_id": object_id,
                    "inventory": resource["inventory"],
                    "candidate_path": resource["work_path"],
                    "source_kind": "museum_digital_capture",
                    "candidate_role": "md_compound_member" if multipart else "md_origin_master_candidate",
                    "match_basis": "captured_live_md_resource",
                    "format": resource["format"],
                    "size_bytes": resource["size_bytes"],
                    "page_count": resource["page_count"],
                    "width_px": resource["width_px"],
                    "height_px": resource["height_px"],
                    "known_sha256": resource["sha256"],
                    "resource_id": resource["resource_id"],
                    "master_eligible": "false" if multipart else "true",
                    "decision_state": "unresolved",
                    "deletion_authorized": "false",
                })
        for row in local_rows:
            master_candidates.append({
                "candidate_id": "", "md_object_id": row["md_object_id"], "inventory": row["inventory"],
                "candidate_path": row["source_path"], "source_kind": "protected_local_document_index",
                "candidate_role": row["candidate_role"], "match_basis": row["match_basis"], "format": "pdf",
                "size_bytes": row["size_bytes"], "page_count": row["page_count"], "width_px": "", "height_px": "",
                "known_sha256": row["sha256"], "resource_id": "", "master_eligible": row["master_eligible"],
                "decision_state": "unresolved" if row["master_eligible"] == "true" else "excluded_derivative_or_integrity",
                "deletion_authorized": "false",
            })
        for row in visual_rows:
            if row["evidence_state"] != "strong_visual_candidate":
                continue
            master_candidates.append({
                "candidate_id": "", "md_object_id": row["md_object_id"], "inventory": row["inventory"],
                "candidate_path": row["source_path"], "source_kind": "protected_local_visual_index",
                "candidate_role": "potential_local_binary_enrichment", "match_basis": f"visual_hash_total={row['distance_total']}",
                "format": str(row["extension"]).lstrip("."), "size_bytes": row["size_bytes"], "page_count": "",
                "width_px": row["width_px"], "height_px": row["height_px"], "known_sha256": "", "resource_id": row["resource_id"],
                "master_eligible": "true", "decision_state": "unresolved", "deletion_authorized": "false",
            })
        master_candidates.sort(key=lambda row: (int(row["md_object_id"]), row["source_kind"], row["candidate_path"].casefold()))
        candidate_counts: dict[str, int] = defaultdict(int)
        for row in master_candidates:
            candidate_counts[row["md_object_id"]] += 1
            row["candidate_id"] = f"md-{row['md_object_id']}-candidate-{candidate_counts[row['md_object_id']]:03d}"

        proposals = []
        object_rows = []
        decision_rows = []
        disposition_rows = []
        comparison_by_object = {row["md_object_id"]: row for row in metadata_comparisons}
        for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
            resources = resources_by_object[object_id]
            candidates = [row for row in master_candidates if row["md_object_id"] == object_id]
            enrichment = [row for row in candidates if row["candidate_role"] == "potential_local_binary_enrichment" and row["master_eligible"] == "true"]
            if enrichment:
                proposal_state = "local_binary_enrichment_review_required"
                proposed_candidate = ""
                proposed_type = ""
                proposed_resources = []
                proposal_basis = f"{len(enrichment)} materially different local candidate(s) require human comparison"
            elif len(resources) > 1:
                proposal_state = "md_origin_compound_master_proposed"
                proposed_candidate = COMPOUND_MD_SELECTION
                proposed_type = "ordered_md_resource_set"
                proposed_resources = [row["resource_id"] for row in resources]
                proposal_basis = "no materially different eligible local binary candidate; retain every ordered MD resource"
            else:
                md_candidate = next(row for row in candidates if row["source_kind"] == "museum_digital_capture")
                proposal_state = "md_origin_single_master_proposed"
                proposed_candidate = md_candidate["candidate_id"]
                proposed_type = "single_file"
                proposed_resources = [resources[0]["resource_id"]]
                proposal_basis = "no materially different eligible local binary candidate"
            proposal = {
                "md_object_id": object_id, "inventory": item["object_inventory_number"],
                "proposal_state": proposal_state, "proposed_candidate_id": proposed_candidate,
                "proposed_representation_type": proposed_type,
                "proposed_resource_ids": json.dumps(proposed_resources),
                "proposal_basis": proposal_basis,
                "metadata_proposal": comparison_by_object[object_id]["proposal"],
                "human_confirmation_required": "true", "deletion_authorized": "false",
            }
            proposals.append(proposal)
            object_rows.append({
                "md_object_id": object_id, "inventory": item["object_inventory_number"],
                "title": compact(item.get("object_name")), "object_type": compact(item.get("object_type")),
                "description": compact(item.get("object_description")),
                "material_technique": compact(item.get("object_material_technique")),
                "dimensions": compact(item.get("object_dimensions")), "date": date_values(item), "tags": tag_values(item),
                "object_url": f"https://berlin.museum-digital.de/object/{object_id}",
                "resource_count": len(resources), "local_pdf_occurrences": len(local_by_object[object_id]),
                "local_binary_enrichment_candidates": len(enrichment),
                "metadata_evidence_rows": comparison_by_object[object_id]["metadata_evidence_rows"],
                "metadata_proposal": comparison_by_object[object_id]["proposal"],
                "master_proposal_state": proposal_state, "master_decision_state": "unresolved",
                "deletion_authorized": "false",
            })
            decision_rows.append({
                "md_object_id": object_id, "inventory": item["object_inventory_number"], "decision_state": "unresolved",
                "selected_candidate_id": "", "master_representation_type": "", "selected_resource_ids": "[]",
                "metadata_authority": "archive_catalog_after_accession", "representation_note": "", "decision_note": "",
                "actor": "", "decided_at": "", "deletion_authorized": "false",
            })
            for candidate in candidates:
                disposition_rows.append({
                    "candidate_id": candidate["candidate_id"], "md_object_id": object_id,
                    "candidate_path": candidate["candidate_path"], "disposition": "pending",
                    "reason": "awaiting explicit human Master decision", "deletion_authorized": "false",
                })

        indexed_by_path = {str(row["source_path"]): row for row in index_rows}
        drain_by_path: dict[str, dict[str, Any]] = {}
        object_id_by_key = {inventory_key(str(item["object_inventory_number"])): object_id for object_id, item in objects.items()}

        def add_drain(path: Path, source_class: str, classification: str, evidence: str, *, known_sha: str = "", known_size: Any = "", object_id: str = "", inventory: str = "") -> None:
            path_text = str(path)
            if path_text in drain_by_path:
                return
            if source_class == "protected_usb_source" and not path.is_file():
                raise PrepareError(f"drain source disappeared: {path}")
            size = int(known_size or path.stat().st_size)
            digest = known_sha or str(indexed_by_path.get(path_text, {}).get("content_sha256") or "")
            if not digest:
                digest = sha256_file(path)
            if not object_id:
                for key in filename_keys(path.name):
                    if key in object_id_by_key:
                        object_id = object_id_by_key[key]
                        inventory = str(objects[object_id]["object_inventory_number"])
                        break
            drain_by_path[path_text] = {
                "md_object_id": object_id, "inventory": inventory, "source_path": path_text,
                "source_class": source_class, "source_kind": path.suffix.casefold().lstrip(".") or "file",
                "classification": classification, "relationship": "derivative_or_transient_cleanup_candidate",
                "evidence": evidence, "sha256": digest, "size_bytes": size,
                "selected_master_candidate_id": "", "covered_by_selected_master": "false",
                "coverage_state": "not_yet_accessioned",
                "drain_state": "blocked_until_human_decisions_and_validated_accession",
                "deletion_authorized": "false",
                "note": "Drain listing records cleanup intent only; it never authorizes deletion.",
            }

        for root in COLLECTION_SOURCE_DIRS:
            if not root.is_dir():
                raise PrepareError(f"expected collection source directory missing: {root}")
            for directory, _subdirs, filenames in os.walk(root):
                for filename in filenames:
                    path = Path(directory) / filename
                    classified = drain_class(path)
                    if classified:
                        add_drain(path, "protected_usb_source", classified[0], classified[1])
        for row in local_rows:
            path = Path(row["source_path"])
            classified = drain_class(path)
            if classified:
                add_drain(path, "protected_usb_source", classified[0], classified[1], known_sha=row["sha256"], known_size=row["size_bytes"], object_id=row["md_object_id"], inventory=row["inventory"])
        for resource in resource_rows:
            add_drain(Path(resource["work_path"]), "work_capture", "transient_work_capture", "museum-digital reconciliation capture", known_sha=resource["sha256"], known_size=resource["size_bytes"], object_id=resource["md_object_id"], inventory=resource["inventory"])
        drain_rows = sorted(drain_by_path.values(), key=lambda row: (row["source_class"], row["source_path"].casefold()))

        relationships = []
        candidates_by_hash: dict[str, set[str]] = defaultdict(set)
        for row in master_candidates:
            if row["known_sha256"]:
                candidates_by_hash[row["known_sha256"]].add(row["md_object_id"])
        for digest, ids in candidates_by_hash.items():
            ordered = sorted(ids, key=int)
            for left_index, left in enumerate(ordered):
                for right in ordered[left_index + 1:]:
                    relationships.append({
                        "left_md_object_id": left, "right_md_object_id": right,
                        "relationship": "shared_candidate_sha256", "evidence": digest,
                        "decision_state": "pending_identity_review", "deletion_authorized": "false",
                    })

        raw_target = stage / "museum-digital-collection-551.json"
        raw_target.write_bytes(raw_payload)
        with raw_target.open("rb") as handle:
            os.fsync(handle.fileno())
        jsonl = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for _, item in sorted(objects.items(), key=lambda pair: int(pair[0])))
        atomic_bytes(stage / "md-objects.jsonl", jsonl.encode())
        atomic_csv(stage / "objects.csv", list(object_rows[0]), object_rows)
        atomic_csv(stage / "md-resources.csv", list(resource_rows[0]), resource_rows)
        atomic_csv(stage / "local-document-candidates.csv", list(local_rows[0]) if local_rows else ["md_object_id"], local_rows)
        atomic_csv(stage / "visual-leads.csv", list(visual_rows[0]) if visual_rows else ["md_object_id"], visual_rows)
        atomic_csv(stage / "metadata-source-documents.csv", list(metadata_documents_rows[0]) if metadata_documents_rows else ["source_path"], metadata_documents_rows)
        atomic_csv(stage / "metadata-evidence.csv", list(metadata_rows[0]) if metadata_rows else ["md_object_id"], metadata_rows)
        atomic_csv(stage / "metadata-comparison.csv", list(metadata_comparisons[0]), metadata_comparisons)
        atomic_csv(stage / "master-candidates.csv", list(master_candidates[0]), master_candidates)
        atomic_csv(stage / "master-proposals.csv", list(proposals[0]), proposals)
        atomic_csv(stage / "master-decisions.csv", list(decision_rows[0]), decision_rows)
        atomic_csv(stage / "candidate-dispositions.csv", list(disposition_rows[0]), disposition_rows)
        atomic_csv(stage / "object-relationships.csv", list(relationships[0]) if relationships else ["left_md_object_id", "right_md_object_id", "relationship", "evidence", "decision_state", "deletion_authorized"], relationships)
        atomic_csv(stage / "drain-list.csv", list(drain_rows[0]), drain_rows)

        summary = {
            "format": "iss-collection-551-reconciliation-preparation-v1",
            "created_at": now(), "result": "ok", "museum_digital_collection": COLLECTION_ID,
            "query": "collection:551", "objects": len(object_rows), "resources": len(resource_rows),
            "pdf_resources": sum(row["resource_type"] == "text" for row in resource_rows),
            "image_resources": sum(row["resource_type"] == "image" for row in resource_rows),
            "single_resource_objects": sum(int(row["resource_count"]) == 1 for row in object_rows),
            "multi_resource_objects": sum(int(row["resource_count"]) > 1 for row in object_rows),
            "local_pdf_occurrences": len(local_rows),
            "objects_with_local_pdf_candidates": len({row["md_object_id"] for row in local_rows}),
            "exact_md_resource_local_occurrences": sum(row["match_basis"] == "exact_md_resource_sha256" for row in local_rows),
            "potential_local_binary_enrichment_candidates": sum(row["candidate_role"] == "potential_local_binary_enrichment" for row in local_rows) + sum(row["evidence_state"] == "strong_visual_candidate" for row in visual_rows),
            "md_origin_master_proposals": sum(row["proposal_state"].startswith("md_origin_") for row in proposals),
            "local_binary_enrichment_reviews": sum(row["proposal_state"] == "local_binary_enrichment_review_required" for row in proposals),
            "metadata_source_documents": len(metadata_documents_rows), "metadata_evidence_rows": len(metadata_rows),
            "objects_with_local_metadata_evidence": sum(int(row["metadata_evidence_rows"]) > 0 for row in metadata_comparisons),
            "objects_with_local_metadata_enrichment": sum(bool(row["local_enrichment_fields"]) for row in metadata_comparisons),
            "master_candidates": len(master_candidates), "pending_master_decisions": len(decision_rows),
            "drain_rows": len(drain_rows),
            "drain_protected_source_rows": sum(row["source_class"] == "protected_usb_source" for row in drain_rows),
            "drain_work_capture_rows": sum(row["source_class"] == "work_capture" for row in drain_rows),
            "drain_classifications": dict(sorted(Counter(row["classification"] for row in drain_rows).items())),
            "source_mount": mount, "document_index": {"path": str(DOCUMENT_INDEX), "latest_scan": index_state},
            "visual_index": {"path": str(VISUAL_INDEX), **visual_state},
            "source_files_modified": 0, "published_unit_writes": 0, "master_selections_made": 0,
            "derivatives_generated": 0, "accession_authorized": False, "deletion_authorized": False,
        }
        atomic_json(stage / "preparation-summary.json", summary)
        atomic_bytes(stage / "00_README.md", (
            "# Collection 551 reconciliation preparation\n\n"
            "Proposal-only evidence for the 79 live museum-digital objects. Museum-digital files are the default Master proposal when no materially different eligible local binary candidate was found. Every Master decision remains unresolved until a person confirms it.\n\n"
            "The live MD metadata is captured in `museum-digital-collection-551.json`, `md-objects.jsonl`, and `objects.csv`. Local spreadsheet and CSV evidence is in `metadata-source-documents.csv`, `metadata-evidence.csv`, and `metadata-comparison.csv`; the eventual archive catalogue becomes authoritative after reconciliation and accession.\n\n"
            "`drain-list.csv` includes deterministic local derivatives, projection-staging/transient files, and every MD work capture. All rows are blocked until human decisions and a separately authorized, independently validated accession. Every row has `deletion_authorized=false`.\n\n"
            "No source file was changed, no Master was selected, no derivative was generated, no accession was performed, and no deletion was authorized.\n"
        ).encode())
        os.replace(stage, output)
        return summary
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def check(output: Path) -> dict[str, Any]:
    mount = source_guard()
    objects, _payload, _last = live_objects()
    missing = [str(path) for path in (DOCUMENT_INDEX, VISUAL_INDEX) if not path.is_file()]
    if missing:
        raise PrepareError(f"required indexes missing: {missing}")
    if any(not path.is_dir() for path in COLLECTION_SOURCE_DIRS):
        raise PrepareError("one or more bounded FEB source directories are missing")
    if subprocess.run(["pdfinfo", "-v"], capture_output=True).returncode not in {0, 99}:
        raise PrepareError("pdfinfo is not usable")
    return {
        "result": "ok", "objects": len(objects),
        "resources": sum(len(item.get("object_images") or []) for item in objects.values()),
        "source_mount": mount, "output_exists": output.exists(), "writes_performed": 0,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--check", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    output = args.output.expanduser().resolve()
    if not output.is_relative_to(WORK_ROOT.resolve()):
        raise PrepareError("output must remain inside the collection-551 work root")
    result = check(output) if args.check else prepare(output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
