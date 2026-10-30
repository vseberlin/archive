#!/usr/bin/env python3
"""Prepare the bounded local-only TFA reconciliation queue.

This tool reads Elements, the existing archive index, and one completed
museum-digital export capture.  It adds only the small catalogue projections
needed by the existing TFA workbench.  It never selects a Master or declares a
museum-digital duplicate.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.1"
SOURCE_ROOT = Path("/media/vladimir/Elements")
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
ARCHIVE_INDEX = Path("/home/vladimir/Archive-restoration/archive_index.sqlite")
DEFAULT_MD_RUN = WORK_ROOT / "work/reconciliation/museum-digital/runs/20260814T201913Z-tfa-institution-29-clean-local-only-recheck"
METADATA_CSV = REVIEW_ROOT / "local-object-metadata.csv"
TECHNICAL_CSV = REVIEW_ROOT / "local-object-accession-candidates.csv"
MATCHES_CSV = REVIEW_ROOT / "local-object-md-match-candidates.csv"
DECISIONS_CSV = REVIEW_ROOT / "local-object-decisions.csv"
RECEIPT = REVIEW_ROOT / "local-object-workbench-preflight.json"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".jpg", ".jpeg"}
TIFF_EXTENSIONS = {".tif", ".tiff"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
JPEG_MIN_LONG_EDGE = 2000
JPEG_MIN_PIXELS = 4_000_000


class PrepareError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)
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


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def normalize_inventory(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def source_mount() -> dict[str, Any]:
    result = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True,
        capture_output=True,
        text=True,
    )
    filesystems = json.loads(result.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise PrepareError("Elements did not resolve to one mounted filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements read-only mount guard failed: {mount}")
    return mount


def inside_source(path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(SOURCE_ROOT.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def raw_cells(value: str) -> list[str]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return []
    cells = payload.get("cells") if isinstance(payload, dict) else None
    return [clean(item) for item in cells] if isinstance(cells, list) else []


def evidence_score(row: dict[str, Any]) -> tuple[int, int, str, int]:
    joined = " ".join((clean(row.get("title")), clean(row.get("raw_json"))))
    path = clean(row.get("source_path"))
    score = 0
    score += 80 if row.get("source_type") == "xlsx" else 0
    score += 35 if "FINALE" in path.upper() else 0
    score += 20 if clean(row.get("title")) else 0
    score += 15 if clean(row.get("md_id")) else 0
    score += min(25, len(raw_cells(clean(row.get("raw_json")))))
    score -= 200 if "�" in joined else 0
    score -= 80 if "####" in joined else 0
    return (score, len(joined), path, int(row.get("row_number") or 0))


def parsed_metadata(obj: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(sources, key=evidence_score, reverse=True)
    best = ordered[0] if ordered else {}
    cells = raw_cells(clean(best.get("raw_json")))
    title = clean(best.get("title")) or clean(obj.get("title"))
    description = clean(obj.get("description"))
    date_text = ""
    object_type = clean(obj.get("object_type"))
    material = ""
    dimensions = ""
    collection_text = clean(obj.get("collection"))
    if cells:
        if clean(cells[0]).upper().startswith("TFA-") and len(cells) >= 8:
            if len(cells) > 3 and cells[2].casefold() in {"foto", "fotografie", "negativ", "dias", "dia"}:
                collection_text = cells[1] or collection_text
                object_type = cells[2] or object_type
                title = cells[3] or title
                description = cells[4] or description
                material = cells[5]
                dimensions = cells[6]
                date_text = cells[22] if len(cells) > 22 else ""
            elif len(cells) > 4:
                title = cells[1] or title
                description = cells[2] or description
                material = cells[3]
                dimensions = cells[4]
        elif re.fullmatch(r"\d{5,8}", cells[0]) and len(cells) >= 6:
            date_text = cells[1]
            title = cells[2] or title
            description = cells[3] or description
            material = cells[4]
            dimensions = cells[5]
    if "�" in title and clean(obj.get("title")) and "�" not in clean(obj.get("title")):
        title = clean(obj.get("title"))
    hints: set[str] = set()
    for source in sources:
        for cell in raw_cells(clean(source.get("raw_json"))):
            match = re.fullmatch(r"(?:TFA-FOT-)?(\d{6,8})", cell, flags=re.IGNORECASE)
            if match:
                hints.add("TFA-" + match.group(1))
    source_rows = [
        {
            "source_path": clean(row.get("source_path")),
            "source_type": clean(row.get("source_type")),
            "row_number": int(row.get("row_number") or 0),
            "md_id": clean(row.get("md_id")),
            "title": clean(row.get("title")),
            "image_name": clean(row.get("image_name")),
            "master_name": clean(row.get("master_name")),
            "raw_json": clean(row.get("raw_json")),
        }
        for row in ordered
    ]
    state = "source_evidence" if ordered else "catalogue_only"
    return {
        "record_key": "local:" + normalize_inventory(obj["inventory_number"]),
        "inventory": clean(obj["inventory_number"]),
        "title": title,
        "description": description,
        "date_text": date_text,
        "object_type": object_type,
        "material": material,
        "dimensions": dimensions,
        "collection_text": collection_text,
        "historical_md_id": clean(obj.get("md_id")),
        "preferred_source": clean(best.get("source_path")) or clean(obj.get("preferred_source")),
        "preferred_row": int(best.get("row_number") or 0),
        "source_count": len(sources),
        "inventory_hints": "; ".join(sorted(hints)),
        "source_rows_json": json.dumps(source_rows, ensure_ascii=False, separators=(",", ":")),
        "metadata_state": state,
    }


def load_local_metadata(local_objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    inventories = [row["inventory"] for row in local_objects]
    by_inventory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with closing(connect(ARCHIVE_INDEX, read_only=True)) as connection:
        for start in range(0, len(inventories), 300):
            group = inventories[start : start + 300]
            placeholders = ",".join("?" for _ in group)
            for row in connection.execute(
                f"SELECT * FROM object_sources WHERE inventory_number IN ({placeholders})", group
            ):
                by_inventory[str(row["inventory_number"])].append(dict(row))
        archive_objects = {}
        for start in range(0, len(inventories), 300):
            group = inventories[start : start + 300]
            placeholders = ",".join("?" for _ in group)
            for row in connection.execute(
                f"SELECT * FROM objects WHERE inventory_number IN ({placeholders})", group
            ):
                archive_objects[str(row["inventory_number"])] = dict(row)
    result = []
    for local in local_objects:
        inventory = str(local["inventory"])
        source_obj = archive_objects.get(inventory, {
            "inventory_number": inventory,
            "collection": local.get("collections", ""),
            "object_type": local.get("object_type", ""),
            "title": local.get("title", ""),
            "description": local.get("description", ""),
            "md_id": local.get("md_object_id", ""),
            "preferred_source": "",
        })
        parsed = parsed_metadata(source_obj, by_inventory.get(inventory, []))
        # The existing reconciliation database owns the record-key spelling;
        # do not recreate it from inventory punctuation or case.
        parsed["record_key"] = str(local["record_key"])
        result.append(parsed)
    return sorted(result, key=lambda row: (normalize_inventory(row["inventory"]), row["record_key"]))


def technical_batch(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not shutil.which("exiftool"):
        raise PrepareError("exiftool is required for bounded technical metadata reads")
    by_path = {str(row["source_path"]): row for row in candidates}
    extracted: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for start in range(0, len(candidates), 100):
        group = candidates[start : start + 100]
        command = [
            "exiftool", "-j", "-n", "-ImageWidth", "-ImageHeight", "-FileType", "-MIMEType",
            "-BitsPerSample", "-ColorSpace", "-Compression", "-Orientation", "-Software", "-Artist",
            "-Copyright", "-CreateDate", "-DateTimeOriginal", "--",
            *[str(row["source_path"]) for row in group],
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=180)
        try:
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            payload = []
        for item in payload if isinstance(payload, list) else []:
            source_file = clean(item.get("SourceFile"))
            if source_file:
                extracted[source_file] = item
        if completed.returncode:
            message = clean(completed.stderr)[-1000:]
            for row in group:
                if str(row["source_path"]) not in extracted:
                    errors[str(row["source_path"])] = message or f"exiftool exit {completed.returncode}"
        print(f"technical metadata: {min(start + 100, len(candidates))}/{len(candidates)}", flush=True)
    rows = []
    for path, candidate in by_path.items():
        item = extracted.get(path, {})
        width = int(float(item.get("ImageWidth") or 0))
        height = int(float(item.get("ImageHeight") or 0))
        extension = str(candidate["extension"]).casefold()
        readable = width > 0 and height > 0
        if extension in TIFF_EXTENSIONS:
            eligible = readable
            basis = "TIFF with readable image dimensions" if readable else "TIFF header unreadable"
        else:
            eligible = readable and max(width, height) >= JPEG_MIN_LONG_EDGE and width * height >= JPEG_MIN_PIXELS
            basis = (
                f"JPEG long edge >= {JPEG_MIN_LONG_EDGE}px and pixels >= {JPEG_MIN_PIXELS}"
                if eligible else
                f"JPEG below HiRes threshold ({JPEG_MIN_LONG_EDGE}px long edge and {JPEG_MIN_PIXELS} pixels)"
                if readable else "JPEG header unreadable"
            )
        embedded = {key: value for key, value in item.items() if key != "SourceFile"}
        rows.append({
            "candidate_id": int(candidate["candidate_id"]),
            "record_key": candidate["record_key"],
            "source_path": path,
            "extension": extension,
            "width_px": width,
            "height_px": height,
            "file_type": clean(item.get("FileType")),
            "mime_type": clean(item.get("MIMEType")),
            "header_status": "read" if readable else "unreadable",
            "accession_eligible": 1 if eligible else 0,
            "eligibility_basis": basis,
            "embedded_metadata_json": json.dumps(embedded, ensure_ascii=False, separators=(",", ":")),
            "technical_error": errors.get(path, ""),
        })
    return sorted(rows, key=lambda row: (row["record_key"], -row["accession_eligible"], row["source_path"]))


def md_image_url(item: dict[str, Any], *, preview: bool = False) -> str:
    images = item.get("object_images") if isinstance(item.get("object_images"), list) else []
    if not images:
        return ""
    image = sorted(images, key=lambda row: (str(row.get("is_main")) != "j", int(row.get("order") or 999)))[0]
    folder = clean(image.get("folder")).strip("/")
    filename = clean(image.get("preview") if preview else image.get("filename_loc"))
    return f"https://berlin.museum-digital.de/data/berlin/{folder}/{filename}" if folder and filename else ""


def load_md_objects(run: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    coverage_path = run / "coverage.json"
    if not coverage_path.is_file():
        raise PrepareError(f"museum-digital coverage is missing: {coverage_path}")
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    if coverage.get("format") != "iss-md-export-discovery-v1" or int(coverage.get("captured_objects", -1)) != 11170:
        raise PrepareError(f"museum-digital capture is not complete: {coverage}")
    if int(coverage.get("maximum_page_limit", 101)) > 100 or int(coverage.get("request_failures", -1)) != 0:
        raise PrepareError("museum-digital page-size/failure guard failed")
    objects: dict[str, dict[str, Any]] = {}
    for path in sorted((run / "raw").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("results", {}).get("objects", {})
        if isinstance(values, dict):
            iterable = values.values()
        elif isinstance(values, list):
            iterable = values
        else:
            iterable = []
        for item in iterable:
            md_id = clean(item.get("object_id"))
            if md_id:
                objects[md_id] = item
    if len(objects) != 11170:
        raise PrepareError(f"museum-digital JSON coverage mismatch: {len(objects)} != 11170")
    return objects, coverage


def hash_image(path: Path) -> tuple[str, str, str, str]:
    try:
        from PIL import Image
        from scipy.fft import dctn
        import numpy as np
        with Image.open(path) as image:
            image.draft("L", (64, 64))
            grayscale = image.convert("L")
            average = np.asarray(grayscale.resize((8, 8), Image.Resampling.LANCZOS), dtype=np.float32)
            difference = np.asarray(grayscale.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
            frequency = np.asarray(grayscale.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
            low = dctn(frequency, type=2, norm="ortho")[:8, :8]
            def bits_hex(bits: Any) -> str:
                value = 0
                for bit in bits.astype(bool).flat:
                    value = (value << 1) | int(bit)
                return f"{value:016x}"
            ahash = bits_hex(average > average.mean())
            dhash = bits_hex(difference[:, 1:] > difference[:, :-1])
            phash = bits_hex(low > np.median(low.flat[1:]))
        return ahash, dhash, phash, "ok"
    except Exception as error:  # decoder failures are retained as evidence
        return "", "", "", clean(f"{type(error).__name__}: {error}")[:500]


def hamming(first: str, second: str) -> int | None:
    if not first or not second or len(first) != len(second):
        return None
    return (int(first, 16) ^ int(second, 16)).bit_count()


def choose_visual_representatives(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = [dict(row) for row in connection.execute(
        """
        SELECT c.*, o.record_state FROM candidates c JOIN objects o ON o.record_key=c.record_key
        WHERE lower(c.extension) IN ('.jpg','.jpeg','.tif','.tiff')
          AND o.record_state IN ('current_museum_digital','local_tfa_review')
        ORDER BY c.record_key,
          CASE WHEN lower(c.extension) IN ('.jpg','.jpeg') THEN 0 ELSE 1 END,
          CASE WHEN lower(c.source_path) LIKE '%800px%' THEN 0 ELSE 1 END,
          c.size_bytes, c.source_path
        """
    )]
    current: dict[str, dict[str, Any]] = {}
    local: dict[str, dict[str, Any]] = {}
    for row in rows:
        target = local if row["record_state"] == "local_tfa_review" else current
        target.setdefault(str(row["record_key"]), row)
    return current, local


def prepare_visual_hashes(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    current_reps, local_reps = choose_visual_representatives(connection)
    existing = {
        int(row["candidate_id"]): dict(row)
        for row in connection.execute("SELECT * FROM local_visual_hashes")
    }
    computed = 0
    all_reps = list(current_reps.values()) + list(local_reps.values())
    for index, candidate in enumerate(all_reps, 1):
        candidate_id = int(candidate["candidate_id"])
        cached = existing.get(candidate_id)
        if cached and int(cached["size_bytes"]) == int(candidate["size_bytes"]) and int(cached["mtime_ns"]) == int(candidate["mtime_ns"]):
            continue
        path = Path(str(candidate["source_path"]))
        if not inside_source(path):
            raise PrepareError(f"visual candidate escaped Elements: {path}")
        ahash, dhash, phash, status = hash_image(path)
        connection.execute(
            """
            INSERT INTO local_visual_hashes
              (candidate_id,size_bytes,mtime_ns,ahash_64,dhash_64,phash_64,decode_status)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
              size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
              ahash_64=excluded.ahash_64,dhash_64=excluded.dhash_64,
              phash_64=excluded.phash_64,decode_status=excluded.decode_status
            """,
            (candidate_id, candidate["size_bytes"], candidate["mtime_ns"], ahash, dhash, phash, status),
        )
        existing[candidate_id] = {
            "candidate_id": candidate_id, "size_bytes": candidate["size_bytes"], "mtime_ns": candidate["mtime_ns"],
            "ahash_64": ahash, "dhash_64": dhash, "phash_64": phash, "decode_status": status,
        }
        computed += 1
        if index % 250 == 0 or index == len(all_reps):
            print(f"visual hashes: {index}/{len(all_reps)} ({computed} newly computed)", flush=True)
    current = {key: existing[int(row["candidate_id"])] for key, row in current_reps.items()}
    local = {key: existing[int(row["candidate_id"])] for key, row in local_reps.items()}
    return current, local, computed


def md_projection(md_id: str, item: dict[str, Any]) -> dict[str, Any]:
    events = item.get("object_events") if isinstance(item.get("object_events"), list) else []
    dates = []
    for event in events:
        if not isinstance(event, dict):
            continue
        time_value = event.get("time")
        if isinstance(time_value, dict):
            dates.append(clean(time_value.get("time_name")))
    collections = item.get("object_collection") if isinstance(item.get("object_collection"), list) else []
    return {
        "md_object_id": md_id,
        "md_inventory": clean(item.get("object_inventory_number")),
        "md_title": clean(item.get("object_name")),
        "md_description": clean(item.get("object_description")),
        "md_date": "; ".join(value for value in dates if value),
        "md_type": clean(item.get("object_type")),
        "md_material": clean(item.get("object_material_technique")),
        "md_dimensions": clean(item.get("object_dimensions")),
        "md_collections": "; ".join(clean(row.get("collection_name")) for row in collections if isinstance(row, dict)),
        "md_image_url": md_image_url(item),
        "md_preview_url": md_image_url(item, preview=True),
        "md_url": f"https://berlin.museum-digital.de/object/{md_id}",
    }


def build_matches(
    metadata: list[dict[str, Any]], md_objects: dict[str, dict[str, Any]],
    current_hashes: dict[str, dict[str, Any]], local_hashes: dict[str, dict[str, Any]],
    current_record_to_md: dict[str, str],
) -> list[dict[str, Any]]:
    by_inventory: dict[str, set[str]] = defaultdict(set)
    by_title: dict[str, set[str]] = defaultdict(set)
    md_projected: dict[str, dict[str, Any]] = {}
    for md_id, item in md_objects.items():
        projected = md_projection(md_id, item)
        md_projected[md_id] = projected
        if normalize_inventory(projected["md_inventory"]):
            by_inventory[normalize_inventory(projected["md_inventory"])].add(md_id)
        if normalize(projected["md_title"]):
            by_title[normalize(projected["md_title"])].add(md_id)
    hash_by_md = {
        current_record_to_md[key]: value
        for key, value in current_hashes.items()
        if key in current_record_to_md and current_record_to_md[key]
    }
    results = []
    for number, local in enumerate(metadata, 1):
        evidence: dict[str, set[str]] = defaultdict(set)
        historical = clean(local["historical_md_id"])
        if historical in md_objects:
            evidence[historical].add("live_historical_md_id")
        for md_id in by_inventory.get(normalize_inventory(local["inventory"]), set()):
            evidence[md_id].add("exact_inventory")
        for hint in clean(local["inventory_hints"]).split("; "):
            if hint:
                for md_id in by_inventory.get(normalize_inventory(hint), set()):
                    evidence[md_id].add("metadata_inventory_hint")
        local_title = normalize(local["title"])
        for md_id in by_title.get(local_title, set()) if local_title else set():
            evidence[md_id].add("exact_title")
        if local_title:
            for md_id, projected in md_projected.items():
                other = normalize(projected["md_title"])
                if other and abs(len(other) - len(local_title)) <= 8 and SequenceMatcher(None, local_title, other).ratio() >= 0.965:
                    evidence[md_id].add("near_exact_title")
        local_hash = local_hashes.get(str(local["record_key"]), {})
        visual_scores: dict[str, tuple[int, int, int, float]] = {}
        if local_hash.get("decode_status") == "ok":
            for md_id, other in hash_by_md.items():
                if other.get("decode_status") != "ok":
                    continue
                ah = hamming(str(local_hash.get("ahash_64", "")), str(other.get("ahash_64", "")))
                dh = hamming(str(local_hash.get("dhash_64", "")), str(other.get("dhash_64", "")))
                ph = hamming(str(local_hash.get("phash_64", "")), str(other.get("phash_64", "")))
                if ah is None or dh is None or ph is None:
                    continue
                score = (ah + dh + ph) / 3
                visual_scores[md_id] = (ah, dh, ph, score)
            strongest = sorted(visual_scores, key=lambda value: (visual_scores[value][3], visual_scores[value][2], int(value)))[:5]
            for md_id in strongest:
                ah, dh, ph, score = visual_scores[md_id]
                if score <= 10 and ph <= 14:
                    evidence[md_id].add("visual_near_duplicate")
        for md_id, bases in evidence.items():
            projected = md_projected[md_id]
            ah, dh, ph, visual_score = visual_scores.get(md_id, (None, None, None, None))
            title_score = SequenceMatcher(None, local_title, normalize(projected["md_title"])).ratio() if local_title else 0.0
            confidence = (
                0 if "live_historical_md_id" in bases else
                1 if "exact_inventory" in bases or "metadata_inventory_hint" in bases else
                2 if "exact_title" in bases else
                3 if "near_exact_title" in bases else 4
            )
            results.append({
                "record_key": local["record_key"], **projected,
                "match_basis": "; ".join(sorted(bases)),
                "title_score": f"{title_score:.6f}",
                "ahash_distance": "" if ah is None else ah,
                "dhash_distance": "" if dh is None else dh,
                "phash_distance": "" if ph is None else ph,
                "visual_score": "" if visual_score is None else f"{visual_score:.3f}",
                "confidence_order": confidence,
            })
        if number % 50 == 0 or number == len(metadata):
            print(f"MD comparisons: {number}/{len(metadata)}", flush=True)
    results.sort(key=lambda row: (
        row["record_key"], int(row["confidence_order"]),
        float(row["visual_score"]) if row["visual_score"] != "" else 999.0,
        -float(row["title_score"]), int(row["md_object_id"]),
    ))
    ranks: dict[str, int] = defaultdict(int)
    for row in results:
        ranks[row["record_key"]] += 1
        row["rank"] = ranks[row["record_key"]]
    return results


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS local_object_metadata (
          record_key TEXT PRIMARY KEY REFERENCES objects(record_key),
          inventory TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
          date_text TEXT NOT NULL, object_type TEXT NOT NULL, material TEXT NOT NULL,
          dimensions TEXT NOT NULL, collection_text TEXT NOT NULL,
          historical_md_id TEXT NOT NULL, preferred_source TEXT NOT NULL,
          preferred_row INTEGER NOT NULL, source_count INTEGER NOT NULL,
          inventory_hints TEXT NOT NULL, source_rows_json TEXT NOT NULL,
          metadata_state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_candidate_technical (
          candidate_id INTEGER PRIMARY KEY REFERENCES candidates(candidate_id),
          record_key TEXT NOT NULL REFERENCES objects(record_key),
          source_path TEXT NOT NULL, extension TEXT NOT NULL,
          width_px INTEGER NOT NULL, height_px INTEGER NOT NULL,
          file_type TEXT NOT NULL, mime_type TEXT NOT NULL,
          header_status TEXT NOT NULL, accession_eligible INTEGER NOT NULL CHECK(accession_eligible IN (0,1)),
          eligibility_basis TEXT NOT NULL, embedded_metadata_json TEXT NOT NULL,
          technical_error TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_visual_hashes (
          candidate_id INTEGER PRIMARY KEY REFERENCES candidates(candidate_id),
          size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          ahash_64 TEXT NOT NULL, dhash_64 TEXT NOT NULL, phash_64 TEXT NOT NULL,
          decode_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_md_match_candidates (
          record_key TEXT NOT NULL REFERENCES objects(record_key),
          md_object_id TEXT NOT NULL, md_inventory TEXT NOT NULL, md_title TEXT NOT NULL,
          md_description TEXT NOT NULL, md_date TEXT NOT NULL, md_type TEXT NOT NULL,
          md_material TEXT NOT NULL, md_dimensions TEXT NOT NULL, md_collections TEXT NOT NULL,
          md_image_url TEXT NOT NULL, md_preview_url TEXT NOT NULL, md_url TEXT NOT NULL,
          match_basis TEXT NOT NULL, title_score REAL NOT NULL,
          ahash_distance INTEGER, dhash_distance INTEGER, phash_distance INTEGER, visual_score REAL,
          confidence_order INTEGER NOT NULL, rank INTEGER NOT NULL,
          PRIMARY KEY(record_key, md_object_id)
        );
        CREATE TABLE IF NOT EXISTS local_object_decisions (
          record_key TEXT PRIMARY KEY REFERENCES objects(record_key),
          outcome TEXT NOT NULL CHECK(outcome IN ('duplicate_md','new_local_object','unresolved')),
          selected_candidate_id INTEGER REFERENCES candidates(candidate_id),
          matched_md_object_id TEXT,
          title TEXT NOT NULL, description TEXT NOT NULL, date_text TEXT NOT NULL,
          object_type TEXT NOT NULL, material TEXT NOT NULL, dimensions TEXT NOT NULL,
          note TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
          CHECK((outcome='duplicate_md' AND selected_candidate_id IS NULL AND matched_md_object_id IS NOT NULL)
             OR (outcome='new_local_object' AND selected_candidate_id IS NOT NULL AND matched_md_object_id IS NULL)
             OR (outcome='unresolved' AND selected_candidate_id IS NULL AND matched_md_object_id IS NULL))
        );
        """
    )


def backup(path: Path, stamp: str) -> str:
    if not path.exists():
        return ""
    destination = path.with_name(f"{path.stem}.pre-local-object-workbench-{stamp}{path.suffix}")
    if destination.exists():
        raise PrepareError(f"backup already exists: {destination}")
    shutil.copy2(path, destination)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return str(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md-run", type=Path, default=DEFAULT_MD_RUN)
    parser.add_argument("--actor", default="vladimir")
    args = parser.parse_args()
    prepared_at = utc_now()
    stamp = prepared_at.replace("-", "").replace(":", "")
    mount = source_mount()
    if not DATABASE.is_file() or not ARCHIVE_INDEX.is_file():
        raise PrepareError("required reconciliation or archive-index SQLite is missing")
    md_objects, coverage = load_md_objects(args.md_run)
    with closing(connect(DATABASE, read_only=True)) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        local_objects = [dict(row) for row in connection.execute(
            "SELECT * FROM objects WHERE record_state='local_tfa_review' ORDER BY inventory,record_key"
        )]
        prior_decisions = connection.execute("SELECT COUNT(*) FROM reconciliation_decisions").fetchone()[0]
        prior_selected = connection.execute(
            "SELECT COUNT(*) FROM reconciliation_decisions WHERE decision_state='selected_master'"
        ).fetchone()[0]
        candidates = [dict(row) for row in connection.execute(
            """
            SELECT c.* FROM candidates c JOIN objects o ON o.record_key=c.record_key
            WHERE o.record_state='local_tfa_review' AND lower(c.extension) IN ('.tif','.tiff','.jpg','.jpeg')
            ORDER BY c.record_key,c.source_path
            """
        )]
        current_record_to_md = {
            str(row["record_key"]): clean(row["md_object_id"])
            for row in connection.execute("SELECT record_key,md_object_id FROM objects WHERE record_state='current_museum_digital'")
        }
    if quick != "ok" or len(local_objects) != 436 or prior_selected != 5282 or prior_decisions != 5282:
        raise PrepareError(
            f"scope guard failed: quick={quick}, local={len(local_objects)}, decisions={prior_decisions}, selected={prior_selected}"
        )
    for candidate in candidates:
        path = Path(str(candidate["source_path"]))
        if not inside_source(path):
            raise PrepareError(f"candidate escaped read-only Elements: {path}")
        stat = path.stat()
        if stat.st_size != int(candidate["size_bytes"]) or stat.st_mtime_ns != int(candidate["mtime_ns"]):
            raise PrepareError(f"candidate path/size/mtime guard failed: {path}")
    metadata = load_local_metadata(local_objects)
    technical = technical_batch(candidates)
    backups = [backup(path, stamp) for path in (DATABASE, METADATA_CSV, TECHNICAL_CSV, MATCHES_CSV, DECISIONS_CSV, RECEIPT)]
    backups = [value for value in backups if value]
    with closing(connect(DATABASE)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        initialize_schema(connection)
        existing_local_decisions = connection.execute("SELECT COUNT(*) FROM local_object_decisions").fetchone()[0]
        connection.execute("DELETE FROM local_object_metadata")
        connection.execute("DELETE FROM local_candidate_technical")
        connection.executemany(
            """INSERT INTO local_object_metadata VALUES
            (:record_key,:inventory,:title,:description,:date_text,:object_type,:material,:dimensions,
             :collection_text,:historical_md_id,:preferred_source,:preferred_row,:source_count,
             :inventory_hints,:source_rows_json,:metadata_state)""", metadata,
        )
        connection.executemany(
            """INSERT INTO local_candidate_technical VALUES
            (:candidate_id,:record_key,:source_path,:extension,:width_px,:height_px,:file_type,:mime_type,
             :header_status,:accession_eligible,:eligibility_basis,:embedded_metadata_json,:technical_error)""", technical,
        )
        current_hashes, local_hashes, computed_hashes = prepare_visual_hashes(connection)
        matches = build_matches(metadata, md_objects, current_hashes, local_hashes, current_record_to_md)
        connection.execute("DELETE FROM local_md_match_candidates")
        connection.executemany(
            """INSERT INTO local_md_match_candidates VALUES
            (:record_key,:md_object_id,:md_inventory,:md_title,:md_description,:md_date,:md_type,
             :md_material,:md_dimensions,:md_collections,:md_image_url,:md_preview_url,:md_url,
             :match_basis,:title_score,:ahash_distance,:dhash_distance,:phash_distance,:visual_score,
             :confidence_order,:rank)""", matches,
        )
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_workbench_version',?)", (VERSION,))
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_md_capture',?)", (str(args.md_run),))
        connection.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('tfa_local_candidate_technical_count',?)",
            (str(len(technical)),),
        )
        after_local_decisions = connection.execute("SELECT COUNT(*) FROM local_object_decisions").fetchone()[0]
        if after_local_decisions != existing_local_decisions:
            raise PrepareError("local decision preservation guard failed")
        connection.commit()
    with closing(connect(DATABASE, read_only=True)) as connection:
        decision_rows = [dict(row) for row in connection.execute(
            """SELECT d.*, COALESCE(c.source_path,'') AS selected_source_path
               FROM local_object_decisions d LEFT JOIN candidates c ON c.candidate_id=d.selected_candidate_id
               ORDER BY d.record_key"""
        )]
        quick_after = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_after = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    atomic_csv(METADATA_CSV, metadata[0].keys(), metadata)
    atomic_csv(TECHNICAL_CSV, technical[0].keys(), technical)
    atomic_csv(MATCHES_CSV, matches[0].keys() if matches else (
        "record_key","md_object_id","md_inventory","md_title","md_description","md_date","md_type",
        "md_material","md_dimensions","md_collections","md_image_url","md_preview_url","md_url",
        "match_basis","title_score","ahash_distance","dhash_distance","phash_distance","visual_score",
        "confidence_order","rank",
    ), matches)
    decision_fields = (
        "record_key","outcome","selected_candidate_id","matched_md_object_id","title","description",
        "date_text","object_type","material","dimensions","note","decided_by","decided_at","selected_source_path",
    )
    atomic_csv(DECISIONS_CSV, decision_fields, decision_rows)
    eligible_tiff = sum(row["accession_eligible"] and row["extension"] in TIFF_EXTENSIONS for row in technical)
    eligible_jpeg = sum(row["accession_eligible"] and row["extension"] in JPEG_EXTENSIONS for row in technical)
    records_with_eligible = len({row["record_key"] for row in technical if row["accession_eligible"]})
    match_records = len({row["record_key"] for row in matches})
    receipt = {
        "format": "iss-tfa-local-object-workbench-preflight-v1",
        "prepared_at": prepared_at,
        "result": "ok" if quick_after == "ok" and foreign_after == 0 else "failed",
        "version": VERSION,
        "actor": args.actor,
        "database": str(DATABASE),
        "backups": backups,
        "source_mount": mount,
        "source_read_only": True,
        "local_objects": len(local_objects),
        "local_image_candidates": len(technical),
        "eligible_tiff_candidates": eligible_tiff,
        "eligible_hires_jpeg_candidates": eligible_jpeg,
        "records_with_eligible_candidate": records_with_eligible,
        "records_without_eligible_candidate": len(local_objects) - records_with_eligible,
        "records_with_md_suggestions": match_records,
        "md_match_suggestions": len(matches),
        "human_duplicate_decisions_written": 0,
        "existing_local_decisions_preserved": len(decision_rows),
        "visual_hashes_computed_this_run": computed_hashes,
        "visual_hashes_are_rebuildable_index": True,
        "jpeg_hires_policy": {"minimum_long_edge_px": JPEG_MIN_LONG_EDGE, "minimum_pixel_count": JPEG_MIN_PIXELS},
        "museum_digital_capture": str(args.md_run),
        "museum_digital_capture_sha256": sha256_file(args.md_run / "coverage.json"),
        "museum_digital_objects": len(md_objects),
        "museum_digital_pages": int(coverage.get("page_count", coverage.get("pages", 0)) or 0),
        "museum_digital_maximum_page_limit": int(coverage.get("maximum_page_limit", 0)),
        "museum_digital_request_failures": int(coverage.get("request_failures", 0)),
        "sqlite_quick_check": quick_after,
        "sqlite_foreign_key_violations": foreign_after,
        "derivatives_generated": False,
        "accession_performed": False,
        "deletion_authorized": False,
    }
    atomic_json(RECEIPT, receipt)
    if receipt["result"] != "ok":
        raise PrepareError(f"post-write validation failed: {receipt}")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PrepareError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
