#!/usr/bin/env python3
"""Prepare proposal-only reconciliation evidence for MD collection 549.

The tool captures no new catalogue authority and makes no Master decisions. It
downloads the 18 current museum-digital resources into the approved work root,
compares them with a bounded set of read-only archive paths plus an exhaustive
prior filename inventory, and writes human-review CSV/JSON/Markdown records.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageOps


COLLECTION_ID = 549
EXPECTED_OBJECTS = 10
EXPECTED_RESOURCES = 18
WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-549")
SOURCE_ROOT = Path("/media/vladimir/Elements")
ARCHIVE_ROOT = SOURCE_ROOT / "restore/archive"
INVENTORY = Path(
    "/mnt/volume/iss-archive-work/wf-sender/work/full-restore-audit/runs/"
    "20260729T094604Z-wf-sender-full-restore/restore-inventory.csv"
)
DEFAULT_OUTPUT = WORK_ROOT / "work/reconciliation/unit-reconciliation-v1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
BOUNDED_IMAGE_ROOTS = (
    ARCHIVE_ROOT / "WF/Archiv",
    ARCHIVE_ROOT / "WF/Industriesalon",
    ARCHIVE_ROOT / "alter Rechner-Siemens/Foto-Archiv Industriesalon",
    ARCHIVE_ROOT / "alter Rechner-Siemens/2010-08-03 Wettersonden",
    ARCHIVE_ROOT / "WF/Forschungs- und Entwicklungsberichte",
    ARCHIVE_ROOT / "Findbücher",
    ARCHIVE_ROOT / "WF/Findbücher",
)
PATH_TERMS = {
    "49944": ("leningrad",),
    "57596": ("campingleuchte",),
    "53133": ("findbuch 83-90", "findbuch v", "15125234170"),
    "51893": ("lv3n", "lv3"),
    "49945": ("findbuch",),
    "117742": ("radiosonde", "wettersonde"),
    "51892": ("srs 4451", "srs4451"),
    "117744": ("blitzzusatz", "bzg 1"),
    "57594": ("colorskop",),
    "117745": ("gebrauchsanleitung", "bzg 1", "bgz 1"),
}
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
USER_AGENT = "Industriesalon-archive-reconciliation/1.0 (collection 549 proposal)"


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
    options = set(str(row.get("options", "")).split(","))
    if (
        row.get("target") != str(SOURCE_ROOT)
        or row.get("source") != "/dev/sdf1"
        or row.get("fstype") != "ntfs3"
        or "ro" not in options
    ):
        raise PrepareError(f"protected Elements mount guard failed: {row}")
    return {key: str(row.get(key, "")) for key in ("target", "source", "fstype", "options")}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def csv_bytes(fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> bytes:
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


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    atomic_bytes(path, csv_bytes(fields, rows))


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


def load_capture(capture: Path) -> dict[str, dict[str, Any]]:
    coverage = json.loads((capture / "coverage.json").read_text(encoding="utf-8"))
    if (
        coverage.get("query") != "collection:549"
        or coverage.get("reported_total") != EXPECTED_OBJECTS
        or coverage.get("captured_objects") != EXPECTED_OBJECTS
        or coverage.get("request_failures") != 0
    ):
        raise PrepareError(f"capture does not prove exact collection-549 scope: {coverage}")
    objects: dict[str, dict[str, Any]] = {}
    for path in sorted((capture / "raw").glob("page-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (payload.get("results", {}).get("objects", {}) or {}).items():
            objects[str(key)] = value
    if len(objects) != EXPECTED_OBJECTS or len(set(objects)) != EXPECTED_OBJECTS:
        raise PrepareError("raw capture is not exactly 10 unique objects")
    resources = sum(len(item.get("object_images") or []) for item in objects.values())
    if resources != EXPECTED_RESOURCES:
        raise PrepareError(f"expected 18 resources, found {resources}")
    if any(
        [row.get("collection_id") for row in item.get("object_collection") or []]
        != [COLLECTION_ID]
        for item in objects.values()
    ):
        raise PrepareError("an object has unexpected collection membership in this capture")
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
    filename = urllib.parse.urlparse(str(resource.get("filename_loc") or "")).path
    suffix = Path(filename).suffix.casefold()
    if str(resource.get("type") or "").casefold() == "text":
        return suffix if suffix else ".pdf"
    return suffix if suffix in IMAGE_EXTENSIONS else ".jpg"


def download(url: str, target: Path, last_request: float) -> float:
    temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
    for attempt in range(4):
        wait = 0.65 - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            response_context = urllib.request.urlopen(request, timeout=45)
        except urllib.error.HTTPError as error:
            last_request = time.monotonic()
            if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise PrepareError(f"resource download failed for {url}: HTTP {error.code}") from error
            time.sleep(min(2**attempt, 20))
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_request = time.monotonic()
            if attempt == 3:
                raise PrepareError(f"resource download failed for {url}: {error}") from error
            time.sleep(min(2**attempt, 20))
            continue
        try:
            with response_context as response:
                last_request = time.monotonic()
                announced = int(response.headers.get("Content-Length") or 0)
                if announced > MAX_DOWNLOAD_BYTES:
                    raise PrepareError(f"resource exceeds 256 MiB limit: {url}")
                size = 0
                with temporary.open("xb") as handle:
                    while block := response.read(1024 * 1024):
                        size += len(block)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise PrepareError(f"resource exceeds 256 MiB limit: {url}")
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            if size == 0:
                raise PrepareError(f"empty resource download: {url}")
            os.replace(temporary, target)
            return last_request
        finally:
            temporary.unlink(missing_ok=True)
    raise PrepareError(f"download retries exhausted: {url}")


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
        values = coefficients[:8, :8].flatten()[1:]
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


def visual_state(phash_distance: int, dhash_distance: int) -> str:
    if phash_distance <= 8 and dhash_distance <= 12:
        return "confirmed_visual_match"
    if phash_distance <= 14 and dhash_distance <= 20:
        return "probable_visual_match_review"
    return "visual_lead_only"


def image_technical(path: Path) -> tuple[str, str]:
    try:
        with Image.open(path) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        return str(width), str(height)
    except (OSError, ValueError):
        return "", ""


def normalized_path(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def inventory_rows() -> list[dict[str, str]]:
    if not INVENTORY.is_file():
        raise PrepareError(f"whole-restore inventory is missing: {INVENTORY}")
    with INVENTORY.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if len(rows) < 400_000:
        raise PrepareError("whole-restore inventory is unexpectedly small")
    return rows


def inside_bounded_root(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in BOUNDED_IMAGE_ROOTS)


def metadata_review_rows(objects: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(object_id: str, field: str, proposed: str, note: str) -> None:
        item = objects[object_id]
        current = {
            "object_name": item.get("object_name", ""),
            "object_type": item.get("object_type", ""),
            "object_description": item.get("object_description", ""),
            "object_dimensions": item.get("object_dimensions", ""),
            "event_type": (item.get("object_events") or [{}])[0].get("event_type_name", ""),
            "inventory": item.get("object_inventory_number", ""),
        }[field]
        rows.append(
            {
                "md_object_id": object_id,
                "inventory": str(item.get("object_inventory_number") or ""),
                "field": field,
                "current_value": str(current),
                "proposed_value": proposed,
                "proposal_basis": note,
                "decision": "pending_human_review",
                "final_value": "",
                "note": "",
                "deletion_authorized": "false",
            }
        )

    add("117745", "object_name", "Gebrauchsanleitung BZG 1", "BGZ conflicts with the related BZG 1 object and the description")
    add("51893", "object_type", "Elektronenröhre", "probable spelling correction")
    add("51893", "object_description", str(objects["51893"].get("object_description", "")).replace("hergestelltcvon", "hergestellt von"), "probable transcription correction")
    add("57596", "object_description", str(objects["57596"].get("object_description", "")).replace("uund", "und"), "probable transcription correction")
    add("49944", "object_description", str(objects["49944"].get("object_description", "")).replace("früher 50er", "frühen 1950er").replace("Reparationsleistungsleistung", "Reparationsleistung"), "probable language and transcription corrections")
    add("57594", "object_type", "Farbbildwiedergaberöhre", "probable spelling correction")
    add("57594", "object_description", str(objects["57594"].get("object_description", "")).replace("Fabbildröhren", "Farbbildröhren").replace("als Produktion aufgenommen", "in Produktion aufgenommen"), "probable transcription and grammar corrections")
    add("51892", "object_dimensions", "46 x 89 mm", "trailing x appears erroneous; confirm against the physical object")
    add("49945", "event_type", "Verfasst", "finding aid is documentary; compare with volume-V event before accepting")
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        if not str(item.get("object_dimensions") or "").strip():
            add(object_id, "object_dimensions", "", "missing dimension requires local or physical-object evidence")
    for object_id in ("57596", "117742", "117744", "117745"):
        add(object_id, "inventory", str(objects[object_id].get("object_inventory_number") or ""), "retain Obj_/Object_ prefix pending local inventory confirmation")
    return rows


def prepare(capture: Path, output: Path) -> dict[str, Any]:
    mount = source_guard()
    if output.exists():
        raise PrepareError(f"refusing to replace reconciliation output: {output}")
    output.mkdir(parents=True)
    references = output / "md-reference"
    references.mkdir()
    objects = load_capture(capture)

    resource_rows: list[dict[str, Any]] = []
    reference_paths: dict[str, Path] = {}
    last_request = 0.0
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        resources = list(item.get("object_images") or [])
        main_count = sum(str(row.get("is_main")) == "j" for row in resources)
        if main_count != 1:
            raise PrepareError(f"MD {object_id} does not have exactly one main resource")
        for position, resource in enumerate(resources, 1):
            resource_id = str(resource.get("quell_id") or "")
            suffix = resource_suffix(resource)
            target = references / f"md-{object_id}-resource-{resource_id}{suffix}"
            url = resource_url(resource)
            last_request = download(url, target, last_request)
            if suffix == ".pdf":
                with target.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        raise PrepareError(f"downloaded PDF signature failed: {target}")
                width, height = "", ""
            else:
                width, height = image_technical(target)
                if not width or not height:
                    raise PrepareError(f"downloaded image did not decode: {target}")
            is_main = str(resource.get("is_main")) == "j"
            reference_paths[resource_id] = target
            resource_rows.append(
                {
                    "md_object_id": object_id,
                    "inventory": item.get("object_inventory_number", ""),
                    "resource_position": position,
                    "resource_id": resource_id,
                    "resource_type": resource.get("type", ""),
                    "is_main": "true" if is_main else "false",
                    "description": resource.get("beschreibung", ""),
                    "creator": resource.get("creator", ""),
                    "rights": resource.get("rights", ""),
                    "owner": resource.get("owner", ""),
                    "source_url": url,
                    "work_path": str(target),
                    "format": suffix.lstrip("."),
                    "size_bytes": target.stat().st_size,
                    "width_px": width,
                    "height_px": height,
                    "sha256": sha256_file(target),
                    "resource_role": "master_fallback_candidate" if is_main else "related_catalogue_resource",
                    "deletion_authorized": "false",
                }
            )
            print(f"resources={len(resource_rows)}/{EXPECTED_RESOURCES}", flush=True)

    rows = inventory_rows()
    rows_by_size: dict[int, list[dict[str, str]]] = defaultdict(list)
    bounded_images: list[tuple[Path, int]] = []
    keyword_hits: list[dict[str, str]] = []
    seen_keyword: set[tuple[str, str]] = set()
    for row in rows:
        source_text = str(row.get("source_path") or "")
        if not source_text.startswith(str(ARCHIVE_ROOT) + "/"):
            continue
        try:
            size = int(row.get("size_bytes") or 0)
        except ValueError:
            continue
        if size > 0:
            rows_by_size[size].append(row)
        path = Path(source_text)
        extension = path.suffix.casefold()
        if extension in IMAGE_EXTENSIONS and inside_bounded_root(path):
            bounded_images.append((path, size))
        normalized = normalized_path(str(row.get("relative_path") or path.name))
        for object_id, terms in PATH_TERMS.items():
            for term in terms:
                if normalized_path(term) in normalized:
                    key = (object_id, source_text)
                    if key not in seen_keyword:
                        keyword_hits.append(
                            {
                                "md_object_id": object_id,
                                "inventory": str(objects[object_id].get("object_inventory_number") or ""),
                                "candidate_path": source_text,
                                "matched_term": term,
                                "format": extension.lstrip("."),
                                "size_bytes": str(size),
                                "evidence_state": "filename_or_path_lead_only",
                                "deletion_authorized": "false",
                            }
                        )
                        seen_keyword.add(key)
                    break

    exact_rows: list[dict[str, Any]] = []
    for resource in resource_rows:
        reference = Path(resource["work_path"])
        for row in rows_by_size.get(int(resource["size_bytes"]), []):
            path = Path(row["source_path"])
            if not path.is_file() or path.stat().st_size != int(resource["size_bytes"]):
                continue
            if sha256_file(path) == resource["sha256"]:
                exact_rows.append(
                    {
                        "md_object_id": resource["md_object_id"],
                        "inventory": resource["inventory"],
                        "resource_id": resource["resource_id"],
                        "is_main": resource["is_main"],
                        "candidate_path": str(path),
                        "format": path.suffix.casefold().lstrip("."),
                        "size_bytes": path.stat().st_size,
                        "sha256": resource["sha256"],
                        "evidence_state": "exact_byte_match_to_md_resource",
                        "deletion_authorized": "false",
                    }
                )

    reference_hashes = {
        row["resource_id"]: image_hashes(Path(row["work_path"]))
        for row in resource_rows
        if row["resource_type"] != "text"
    }
    local_hashes: list[tuple[Path, int, int, int]] = []
    decode_failures = 0
    for index, (path, size) in enumerate(bounded_images, 1):
        if not path.is_file() or path.stat().st_size != size:
            continue
        try:
            phash, dhash = image_hashes(path)
        except (OSError, ValueError):
            decode_failures += 1
            continue
        local_hashes.append((path, size, phash, dhash))
        if index % 500 == 0:
            print(f"bounded-images={index}/{len(bounded_images)} decoded={len(local_hashes)}", flush=True)

    visual_rows: list[dict[str, Any]] = []
    resource_by_id = {str(row["resource_id"]): row for row in resource_rows}
    for resource_id, (reference_phash, reference_dhash) in reference_hashes.items():
        ranked: list[tuple[int, int, int, Path, int]] = []
        for path, size, phash, dhash in local_hashes:
            pdistance = hamming(reference_phash, phash)
            ddistance = hamming(reference_dhash, dhash)
            ranked.append((pdistance + ddistance, pdistance, ddistance, path, size))
        resource = resource_by_id[resource_id]
        for _total, pdistance, ddistance, path, size in sorted(ranked, key=lambda row: row[:3])[:10]:
            visual_rows.append(
                {
                    "md_object_id": resource["md_object_id"],
                    "inventory": resource["inventory"],
                    "resource_id": resource_id,
                    "is_main": resource["is_main"],
                    "candidate_path": str(path),
                    "format": path.suffix.casefold().lstrip("."),
                    "size_bytes": size,
                    "phash_distance": pdistance,
                    "dhash_distance": ddistance,
                    "evidence_state": visual_state(pdistance, ddistance),
                    "deletion_authorized": "false",
                }
            )

    candidate_rows: list[dict[str, Any]] = []
    candidate_keys: set[tuple[str, str]] = set()

    def add_candidate(row: dict[str, Any]) -> None:
        key = (str(row["md_object_id"]), str(row["candidate_path"]))
        if key in candidate_keys:
            return
        candidate_keys.add(key)
        candidate_rows.append(row)

    for resource in resource_rows:
        if resource["is_main"] != "true":
            continue
        add_candidate(
            {
                "md_object_id": resource["md_object_id"],
                "inventory": resource["inventory"],
                "candidate_path": resource["work_path"],
                "candidate_source": "retained museum-digital main resource",
                "candidate_role": "preservation_surrogate_candidate",
                "resource_id": resource["resource_id"],
                "evidence_state": "md_main_resource_fallback",
                "evidence_detail": resource["source_url"],
                "format": resource["format"],
                "size_bytes": resource["size_bytes"],
                "width_px": resource["width_px"],
                "height_px": resource["height_px"],
                "known_sha256": resource["sha256"],
                "deletion_authorized": "false",
            }
        )
    for exact in exact_rows:
        if exact["is_main"] != "true":
            continue
        width, height = image_technical(Path(exact["candidate_path"]))
        add_candidate(
            {
                "md_object_id": exact["md_object_id"],
                "inventory": exact["inventory"],
                "candidate_path": exact["candidate_path"],
                "candidate_source": "read-only Elements archive",
                "candidate_role": "exact_local_counterpart_candidate",
                "resource_id": exact["resource_id"],
                "evidence_state": exact["evidence_state"],
                "evidence_detail": "SHA-256 and size equal to captured MD main resource",
                "format": exact["format"],
                "size_bytes": exact["size_bytes"],
                "width_px": width,
                "height_px": height,
                "known_sha256": exact["sha256"],
                "deletion_authorized": "false",
            }
        )
    for visual in visual_rows:
        if visual["is_main"] != "true" or visual["evidence_state"] not in {
            "confirmed_visual_match",
            "probable_visual_match_review",
        }:
            continue
        path = Path(visual["candidate_path"])
        width, height = image_technical(path)
        add_candidate(
            {
                "md_object_id": visual["md_object_id"],
                "inventory": visual["inventory"],
                "candidate_path": str(path),
                "candidate_source": "read-only Elements bounded visual search",
                "candidate_role": "local_visual_master_candidate",
                "resource_id": visual["resource_id"],
                "evidence_state": visual["evidence_state"],
                "evidence_detail": f"pHash={visual['phash_distance']}; dHash={visual['dhash_distance']}",
                "format": visual["format"],
                "size_bytes": visual["size_bytes"],
                "width_px": width,
                "height_px": height,
                "known_sha256": "",
                "deletion_authorized": "false",
            }
        )

    candidate_rows.sort(key=lambda row: (int(row["md_object_id"]), row["candidate_source"], row["candidate_path"].casefold()))
    for sequence_by_object in [defaultdict(int)]:
        for row in candidate_rows:
            sequence_by_object[row["md_object_id"]] += 1
            row["candidate_id"] = f"md-{row['md_object_id']}-candidate-{sequence_by_object[row['md_object_id']]:02d}"

    object_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    for object_id, item in sorted(objects.items(), key=lambda pair: int(pair[0])):
        object_candidates = [row for row in candidate_rows if row["md_object_id"] == object_id]
        object_resources = [row for row in resource_rows if row["md_object_id"] == object_id]
        events = item.get("object_events") or []
        dates = "; ".join(
            str((event.get("time") or {}).get("time_name") or "") for event in events
            if str((event.get("time") or {}).get("time_name") or "")
        )
        object_rows.append(
            {
                "md_object_id": object_id,
                "inventory": item.get("object_inventory_number", ""),
                "title": item.get("object_name", ""),
                "description": item.get("object_description", ""),
                "object_type": item.get("object_type", ""),
                "material_technique": item.get("object_material_technique", ""),
                "dimensions": item.get("object_dimensions", ""),
                "date": dates,
                "object_url": f"https://berlin.museum-digital.de/object/{object_id}",
                "resource_count": len(object_resources),
                "candidate_count": len(object_candidates),
                "metadata_review_count": sum(row["md_object_id"] == object_id for row in metadata_review_rows(objects)),
                "master_decision_state": "unresolved",
                "deletion_authorized": "false",
            }
        )
        decision_rows.append(
            {
                "md_object_id": object_id,
                "inventory": item.get("object_inventory_number", ""),
                "decision_state": "unresolved",
                "selected_candidate_id": "",
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
                    "reason": "awaiting human Master decision",
                    "deletion_authorized": "false",
                }
            )

    metadata_rows = metadata_review_rows(objects)
    atomic_csv(output / "objects.csv", list(object_rows[0]), object_rows)
    atomic_csv(output / "related-md-resources.csv", list(resource_rows[0]), resource_rows)
    atomic_csv(output / "path-leads.csv", list(keyword_hits[0]), sorted(keyword_hits, key=lambda row: (int(row["md_object_id"]), row["candidate_path"].casefold())))
    atomic_csv(output / "exact-resource-matches.csv", list(exact_rows[0]) if exact_rows else ["md_object_id", "inventory", "resource_id", "is_main", "candidate_path", "format", "size_bytes", "sha256", "evidence_state", "deletion_authorized"], exact_rows)
    atomic_csv(output / "visual-leads.csv", list(visual_rows[0]), visual_rows)
    atomic_csv(output / "master-candidates.csv", list(candidate_rows[0]), candidate_rows)
    atomic_csv(output / "master-decisions.csv", list(decision_rows[0]), decision_rows)
    atomic_csv(output / "candidate-dispositions.csv", list(disposition_rows[0]), disposition_rows)
    atomic_csv(output / "metadata-review.csv", list(metadata_rows[0]), metadata_rows)

    state_counts = Counter(row["evidence_state"] for row in visual_rows)
    summary = {
        "format": "iss-collection-549-reconciliation-preparation-v1",
        "created_at": now(),
        "result": "ok",
        "museum_digital_collection": COLLECTION_ID,
        "objects": len(object_rows),
        "resources": len(resource_rows),
        "master_candidates": len(candidate_rows),
        "pending_master_decisions": len(decision_rows),
        "metadata_review_rows": len(metadata_rows),
        "whole_restore_inventory_rows": len(rows),
        "bounded_image_rows": len(bounded_images),
        "bounded_images_decoded": len(local_hashes),
        "bounded_image_decode_failures": decode_failures,
        "path_leads": len(keyword_hits),
        "exact_resource_matches": len(exact_rows),
        "visual_leads": len(visual_rows),
        "visual_state_counts": dict(sorted(state_counts.items())),
        "source_mount": mount,
        "source_files_modified": 0,
        "published_unit_writes": 0,
        "master_selections_made": 0,
        "derivatives_generated": 0,
        "accession_authorized": False,
        "deletion_authorized": False,
        "inputs": {
            "museum_digital_capture": str(capture),
            "whole_restore_inventory": str(INVENTORY),
            "bounded_image_roots": [str(path) for path in BOUNDED_IMAGE_ROOTS],
        },
    }
    atomic_json(output / "preparation-summary.json", summary)
    atomic_bytes(
        output / "00_README.md",
        (
            "# Objektsammlung collection 549 reconciliation preparation\n\n"
            "Proposal-only evidence for the ten current museum-digital records. "
            "No Master has been selected and no accession has been authorized.\n\n"
            "- `objects.csv`: exact ten-object scope.\n"
            "- `master-candidates.csv`: only main-resource fallback and bounded local visual/exact candidates.\n"
            "- `related-md-resources.csv`: all 18 current MD resources; additional views do not compete as Masters.\n"
            "- `path-leads.csv`: filename/path leads requiring human assessment.\n"
            "- `exact-resource-matches.csv`: exhaustive byte-identical matches from the prior restore inventory.\n"
            "- `visual-leads.csv`: top bounded perceptual comparisons for every image resource.\n"
            "- `metadata-review.csv`: collection-specific proposed corrections; all remain pending.\n"
            "- `master-decisions.csv` and `candidate-dispositions.csv`: initialized unresolved/pending with deletion authorization false.\n\n"
            "The protected Elements source was verified as `/dev/sdf1`, `ntfs3`, read-only. "
            "The published archive was not changed.\n"
        ).encode("utf-8"),
    )
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--capture", type=Path, required=True)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return result


def main() -> int:
    args = parser().parse_args()
    capture = args.capture.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if not output.is_relative_to(WORK_ROOT.resolve()):
        raise PrepareError("output must remain inside collection-549 work root")
    summary = prepare(capture, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
