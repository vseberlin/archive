#!/usr/bin/env python3
"""Prepare a read-only reconciliation projection for museum-digital collection 861.

The program reads the frozen museum-digital capture plus the existing visual and
document indexes.  It records candidate relationships only.  It does not copy,
download, rename, rewrite, delete, accession, or select a master.
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable


WORK_ROOT = Path("/mnt/volume/iss-archive-work/collection-861")
RECONCILIATION_ROOT = WORK_ROOT / "work/reconciliation"
CAPTURE_RUN = RECONCILIATION_ROOT / (
    "museum-digital/runs/20260823T093033Z-collection-861"
)
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
DOCUMENT_INDEX = Path(
    "/mnt/volume/iss-archive-work/document-index/document-index.sqlite"
)
DOCUMENT_SNAPSHOT = Path(
    "/mnt/volume/iss-archive-work/document-index/document-index.pre-extraction.sqlite"
)
SOURCE_ROOT = Path("/media/vladimir/Elements")
PAGE_MATCHES = RECONCILIATION_ROOT / "brigadebuecher-page-matches.csv"
OBJECT_DECISIONS = RECONCILIATION_ROOT / "brigadebuecher-md-object-decisions.csv"
PROJECT_PREFIX = (
    "/media/vladimir/Elements/restore/archive/WF/BRIGADEBUCH-Projekt/"
)
VISUAL_TOOL_ROOT = Path("/home/vladimir/wp/tools/archive-preservation")
FORMAT = "iss-brigadebuecher-reconciliation-v1"
LOW_DISTANCE_TOTAL_MAX = 8
LOW_DISTANCE_PHASH_MAX = 4
LOW_DISTANCE_RUNNER_MARGIN_MIN = 20


class ReconciliationError(RuntimeError):
    pass


def ro_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def csv_payload(rows: Iterable[dict[str, Any]], fields: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def atomic_write(path: Path, payload: bytes) -> None:
    if path.exists():
        raise ReconciliationError(f"refusing to replace existing output: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    atomic_write(path, csv_payload(rows, fields))


def replace_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    """Replace one rebuildable reconciliation index inside the approved work root."""
    path.resolve().relative_to(WORK_ROOT.resolve())
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(csv_payload(rows, fields))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def ensure_source_guard() -> str:
    result = subprocess.run(
        [
            "findmnt",
            "-n",
            "-o",
            "SOURCE,TARGET,FSTYPE,OPTIONS",
            "--target",
            str(SOURCE_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fields = result.stdout.strip().split(maxsplit=3)
    if len(fields) != 4:
        raise ReconciliationError(f"unparseable source mount: {result.stdout!r}")
    source, target, fstype, options = fields
    if source != "/dev/sdf1" or target != str(SOURCE_ROOT) or fstype != "ntfs3":
        raise ReconciliationError(f"unexpected source mount: {result.stdout.strip()}")
    if "ro" not in options.split(","):
        raise ReconciliationError(f"source mount is not read-only: {result.stdout.strip()}")
    return result.stdout.strip()


def base_inventory(value: str) -> str:
    return re.sub(r"_\d+$", "", value.strip().upper())


def part_number(value: str) -> int | None:
    match = re.search(r"_(\d+)$", value.strip())
    return int(match.group(1)) if match else None


def page_sequence(filename: str) -> int | None:
    match = re.search(r"(?:BB|BX)-\d+[_-](\d+)(?=\.[^.]+$)", filename, re.I)
    return int(match.group(1)) if match else None


def description_page_range(description: str) -> tuple[int | None, int | None]:
    patterns = (
        r"PDF\s*,\s*S\.\s*(\d+)\s*(?:bis|[-–])\s*S?\.?\s*(\d+)",
        r"PDF\s*\(S\.\s*(\d+)\s*(?:bis|[-–])\s*S?\.?\s*(\d+)\)",
        r"PDF\s*,\s*S\.\s*(\d+)\s*bis\s*S\.\s*(\d+)",
    )
    compact = re.sub(r"\s+", " ", description)
    for pattern in patterns:
        match = re.search(pattern, compact, re.I)
        if match:
            return int(match.group(1)), int(match.group(2))
    count_match = re.search(
        r"(?:Digitalisat\s*\(PDF[^)]*|PDF[^)]*)[, ]\s*(\d+)\s+Seiten",
        compact,
        re.I,
    )
    if count_match:
        return 1, int(count_match.group(1))
    return None, None


def load_md() -> list[dict[str, Any]]:
    coverage = json.loads((CAPTURE_RUN / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("captured_objects") != 73 or coverage.get("request_failures") != 0:
        raise ReconciliationError(f"unexpected capture coverage: {coverage}")
    raw_files = sorted((CAPTURE_RUN / "raw").glob("*.json"))
    if len(raw_files) != 1:
        raise ReconciliationError(f"expected one raw capture page, found {len(raw_files)}")
    payload = json.loads(raw_files[0].read_text(encoding="utf-8"))
    raw_objects = payload["results"]["objects"]
    with (CAPTURE_RUN / "objects.csv").open(encoding="utf-8", newline="") as handle:
        compact_rows = list(csv.DictReader(handle))
    if len(raw_objects) != 73 or len(compact_rows) != 73:
        raise ReconciliationError("museum-digital capture is not the expected 73 objects")
    objects: list[dict[str, Any]] = []
    for compact in compact_rows:
        object_id = compact["object_id"]
        raw = raw_objects[object_id]
        inventory = compact["inventory"].strip()
        start, end = description_page_range(raw.get("object_description", ""))
        objects.append(
            {
                **compact,
                "base_inventory": base_inventory(inventory),
                "part_number": part_number(inventory) or "",
                "description": raw.get("object_description", ""),
                "material_technique": raw.get("object_material_technique", ""),
                "dimensions": raw.get("object_dimensions", ""),
                "description_page_start": start or "",
                "description_page_end": end or "",
            }
        )
    if len({row["object_id"] for row in objects}) != 73:
        raise ReconciliationError("duplicate museum-digital object IDs")
    if len({row["inventory"] for row in objects}) != 73:
        raise ReconciliationError("duplicate museum-digital inventories")
    if any(row["pdf_count"] != "1" for row in objects):
        raise ReconciliationError("not every museum-digital object has exactly one PDF")
    return objects


def load_documents() -> list[dict[str, Any]]:
    snapshot = ro_connection(DOCUMENT_SNAPSHOT)
    candidates = snapshot.execute(
        "SELECT * FROM documents WHERE source_path LIKE ? ORDER BY source_path",
        (PROJECT_PREFIX + "%",),
    ).fetchall()
    snapshot.close()
    ids = [row["document_id"] for row in candidates]
    current_rows: dict[str, sqlite3.Row] = {}
    current = ro_connection(DOCUMENT_INDEX)
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        for row in current.execute(
            f"SELECT * FROM documents WHERE document_id IN ({placeholders})", batch
        ):
            current_rows[row["document_id"]] = row
    current.close()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = current_rows.get(candidate["document_id"], candidate)
        rows.append(dict(row))
    return rows


def load_visual_assets() -> tuple[list[dict[str, Any]], str]:
    connection = ro_connection(VISUAL_INDEX)
    root = connection.execute(
        "SELECT root_id, root_path FROM visual_roots WHERE root_path = ?",
        (str(SOURCE_ROOT),),
    ).fetchone()
    if root is None:
        raise ReconciliationError("Elements root absent from visual index")
    rows = connection.execute(
        """
        SELECT a.asset_id, a.relative_path, a.original_filename, a.extension,
               a.size_bytes, a.width_px, a.height_px, a.status, a.error,
               max(CASE WHEN f.extractor_id = 'ahash-64-v2'
                        THEN f.feature_hex END) AS ahash,
               max(CASE WHEN f.extractor_id = 'dhash-64-v2'
                        THEN f.feature_hex END) AS dhash,
               max(CASE WHEN f.extractor_id = 'phash-dct-64-v2'
                        THEN f.feature_hex END) AS phash
          FROM visual_assets a
          LEFT JOIN visual_features f USING (asset_id)
         WHERE a.root_id = ?
           AND (upper(a.original_filename) LIKE 'BB-%'
                OR upper(a.original_filename) LIKE 'BX-%')
         GROUP BY a.asset_id
         ORDER BY a.relative_path
        """,
        (root["root_id"],),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows], root["root_id"]


def classify_visual(row: dict[str, Any]) -> str:
    path = row["relative_path"]
    extension = row["extension"].lower()
    if extension in {"jpg", "jpeg"}:
        return "jpeg_derivative"
    if "/SCANS-Canon/" in "/" + path and extension in {"tif", "tiff"}:
        return "canon_tiff_source"
    if path.startswith("restore/archive/Bibliocopy/") and extension in {"tif", "tiff"}:
        return "bibliocopy_tiff_source"
    if "/SCANS-Bibliocopy/" in "/" + path and extension in {"tif", "tiff"}:
        return "project_tiff_copy"
    return "uncertain_visual_candidate"


def visual_base(row: dict[str, Any]) -> str:
    match = re.match(r"((?:BB|BX)-\d+)", row["original_filename"], re.I)
    return match.group(1).upper() if match else ""


def pdf_pages(path: str) -> tuple[int | None, str]:
    try:
        result = subprocess.run(
            ["pdfinfo", path], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    match = re.search(r"^Pages:\s*(\d+)", result.stdout, re.M)
    if result.returncode or not match:
        return None, (result.stderr or result.stdout).strip()[:500]
    return int(match.group(1)), ""


def select_pdf_occurrence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(row: dict[str, Any]) -> tuple[int, str]:
        path = row["source_path"]
        if "/Metadaten/UPLOADS/PDFs_upload/" in path:
            return 0, path.casefold()
        if "/Metadaten/UPLOADS/PDFs/" in path:
            return 1, path.casefold()
        if "/PDFs_neu/" in path:
            return 2, path.casefold()
        return 3, path.casefold()
    return sorted(rows, key=rank)[0]


def build_pdf_rows(
    md_rows: list[dict[str, Any]], documents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_filename: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in documents:
        if row["extension"].lower() == ".pdf":
            by_filename[row["filename"].casefold()].append(row)
    occurrences: list[dict[str, Any]] = []
    preferred: dict[str, dict[str, Any]] = {}
    md_inventories = {row["inventory"] for row in md_rows}
    md_bases = {row["base_inventory"] for row in md_rows}
    for md in md_rows:
        matches = by_filename[(md["inventory"] + ".pdf").casefold()]
        if not matches:
            continue
        hashes = {row["content_sha256"] for row in matches}
        selected = select_pdf_occurrence(matches)
        count, error = pdf_pages(selected["source_path"])
        preferred[md["inventory"]] = {
            **selected,
            "page_count": count,
            "pdfinfo_error": error,
            "occurrence_count": len(matches),
            "unique_hash_count": len(hashes),
        }
        for row in matches:
            occurrences.append(
                {
                    "inventory": md["inventory"],
                    "md_object_id": md["object_id"],
                    "source_path": row["source_path"],
                    "content_sha256": row["content_sha256"],
                    "size_bytes": row["size_bytes"],
                    "page_count": count,
                    "relationship": (
                        "preferred_occurrence_for_review"
                        if row["document_id"] == selected["document_id"]
                        else "exact_duplicate_occurrence"
                    ),
                    "decision_state": "candidate_only",
                }
            )
    other_pdfs: list[dict[str, Any]] = []
    direct_paths = {row["source_path"] for row in occurrences}
    for row in documents:
        if row["extension"].lower() != ".pdf" or row["source_path"] in direct_paths:
            continue
        stem = Path(row["filename"]).stem.upper()
        if stem in md_bases and stem not in md_inventories:
            relationship = "whole_book_pdf_related_to_split_md_objects"
        elif re.fullmatch(r"(?:BB|BX)-\d+(?:_\d+)?", stem):
            relationship = "local_only_or_noncollection_pdf_candidate"
        else:
            relationship = "project_document_pdf"
        other_pdfs.append({**row, "relationship": relationship})
    return occurrences, preferred, other_pdfs


def build_visual_rows(
    assets: list[dict[str, Any]], md_bases: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in assets:
        base = visual_base(row)
        if not base:
            continue
        role = classify_visual(row)
        family = str(Path(row["relative_path"]).parent)
        enriched = {
            **row,
            "base_inventory": base,
            "sequence": page_sequence(row["original_filename"]) or "",
            "candidate_role": role,
            "source_path": str(SOURCE_ROOT / row["relative_path"]),
            "md_base_present": int(base in md_bases),
        }
        details.append(enriched)
        groups[(base, family, role)].append(enriched)
    families: list[dict[str, Any]] = []
    for (base, family, role), rows in sorted(groups.items()):
        sequences = sorted(
            {int(row["sequence"]) for row in rows if row["sequence"] != ""}
        )
        missing: list[int] = []
        if sequences:
            present = set(sequences)
            missing = [value for value in range(sequences[0], sequences[-1] + 1) if value not in present]
        families.append(
            {
                "base_inventory": base,
                "family_path": str(SOURCE_ROOT / family),
                "candidate_role": role,
                "file_count": len(rows),
                "indexed_count": sum(row["status"] == "indexed" for row in rows),
                "error_count": sum(row["status"] != "indexed" for row in rows),
                "sequence_min": sequences[0] if sequences else "",
                "sequence_max": sequences[-1] if sequences else "",
                "missing_sequences": ";".join(map(str, missing)),
                "extensions": ";".join(sorted({row["extension"] for row in rows})),
                "md_base_present": int(base in md_bases),
                "decision_state": "candidate_only",
            }
        )
    return details, families


def hamming(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def monotonic_page_alignment(
    ranked_pages: list[list[tuple[int, int, int, int, int, dict[str, Any]]]],
    gap_cost: int = 20,
) -> list[int | None]:
    """Align ordered PDF pages and TIFF sequences without repeated suggestions."""
    if not ranked_pages:
        return []
    sequences = sorted({item[4] for item in ranked_pages[0]})
    costs = [
        {item[4]: item[0] for item in ranked}
        for ranked in ranked_pages
    ]
    page_count = len(ranked_pages)
    sequence_count = len(sequences)
    infinity = 10**12
    scores = [
        [infinity] * (sequence_count + 1)
        for _ in range(page_count + 1)
    ]
    previous: list[list[tuple[int, int, str] | None]] = [
        [None] * (sequence_count + 1)
        for _ in range(page_count + 1)
    ]
    scores[0][0] = 0
    for page_index in range(page_count + 1):
        for sequence_index in range(sequence_count + 1):
            score = scores[page_index][sequence_index]
            if page_index < page_count and sequence_index < sequence_count:
                matched = score + costs[page_index][sequences[sequence_index]]
                if matched < scores[page_index + 1][sequence_index + 1]:
                    scores[page_index + 1][sequence_index + 1] = matched
                    previous[page_index + 1][sequence_index + 1] = (
                        page_index, sequence_index, "match"
                    )
            if page_index < page_count and score + gap_cost < scores[page_index + 1][sequence_index]:
                scores[page_index + 1][sequence_index] = score + gap_cost
                previous[page_index + 1][sequence_index] = (
                    page_index, sequence_index, "skip_pdf"
                )
            if sequence_index < sequence_count and score + gap_cost < scores[page_index][sequence_index + 1]:
                scores[page_index][sequence_index + 1] = score + gap_cost
                previous[page_index][sequence_index + 1] = (
                    page_index, sequence_index, "skip_tiff"
                )
    alignment: list[int | None] = [None] * page_count
    page_index = page_count
    sequence_index = sequence_count
    while page_index or sequence_index:
        step = previous[page_index][sequence_index]
        if step is None:
            raise ReconciliationError("PDF/TIFF sequence alignment failed")
        prior_page, prior_sequence, action = step
        if action == "match":
            alignment[page_index - 1] = sequences[sequence_index - 1]
        page_index, sequence_index = prior_page, prior_sequence
    return alignment


def classify_match_state(row: dict[str, Any]) -> str:
    """Accept decisive visual identity without treating it as a Master decision."""
    total = int(row["distance_total"])
    phash = int(row["distance_phash"])
    runner = row.get("runner_up_total", "")
    decisive_low_distance = (
        total <= LOW_DISTANCE_TOTAL_MAX
        and phash <= LOW_DISTANCE_PHASH_MAX
        and (runner == "" or int(runner) - total >= LOW_DISTANCE_RUNNER_MARGIN_MIN)
    )
    offset_consistent = str(row.get("offset_consistent", "0")) == "1"
    if offset_consistent and total <= 30 and phash <= 16:
        return "strong_consistent"
    if decisive_low_distance:
        return "strong_visual_identity"
    return "needs_review"


def resolved_inventories(md_rows: list[dict[str, Any]]) -> set[str]:
    """Return inventories whose human TIFF Master selection must remain frozen."""
    if not OBJECT_DECISIONS.is_file():
        return set()
    by_object_id = {row["object_id"]: row["inventory"] for row in md_rows}
    with OBJECT_DECISIONS.open(encoding="utf-8-sig", newline="") as handle:
        return {
            by_object_id[row["md_object_id"]]
            for row in csv.DictReader(handle)
            if row.get("outcome") == "selected_master"
            and row.get("md_object_id") in by_object_id
        }


def pdf_page_rotations(pdf_path: str, page_count: int) -> dict[int, int]:
    """Return the displayed clockwise rotation for every PDF page."""
    result = subprocess.run(
        ["pdfinfo", "-f", "1", "-l", str(page_count), pdf_path],
        capture_output=True,
        text=True,
        timeout=max(60, page_count * 2),
    )
    if result.returncode:
        raise ReconciliationError(f"pdfinfo failed: {result.stderr[:500]}")
    rotations = {
        int(match.group(1)): int(match.group(2)) % 360
        for match in re.finditer(
            r"^Page\s+(\d+)\s+rot:\s+(-?\d+)\s*$",
            result.stdout,
            re.MULTILINE,
        )
    }
    if set(rotations) != set(range(1, page_count + 1)):
        raise ReconciliationError(
            f"PDF rotation coverage is {len(rotations)} of {page_count} pages"
        )
    if any(rotation not in {0, 90, 180, 270} for rotation in rotations.values()):
        raise ReconciliationError("unsupported PDF page rotation")
    return rotations


def embedded_scan_hashes(
    pdf_path: str,
    page_count: int,
    visual_index: Any,
    Image: Any,
    rotations: dict[int, int] | None = None,
) -> list[dict[str, str]]:
    """Hash only the scan rasters embedded in PDF pages, excluding PDF furniture."""
    prefix = RECONCILIATION_ROOT / f".bb-scan-{os.getpid()}-{uuid.uuid4().hex}"
    extracted: list[Path] = []
    try:
        rotations = rotations or pdf_page_rotations(pdf_path, page_count)
        listing = subprocess.run(
            ["pdfimages", "-list", pdf_path],
            capture_output=True,
            text=True,
            timeout=max(60, page_count * 2),
        )
        if listing.returncode:
            raise ReconciliationError(f"pdfimages list failed: {listing.stderr[:500]}")
        layout: dict[int, list[dict[str, int]]] = collections.defaultdict(list)
        for line in listing.stdout.splitlines():
            fields = line.split()
            if len(fields) < 16 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            if fields[2] != "image":
                continue
            layout[int(fields[0])].append({
                "number": int(fields[1]),
                "width": int(fields[3]),
                "height": int(fields[4]),
            })
        if set(layout) != set(range(1, page_count + 1)):
            raise ReconciliationError(
                f"embedded scan coverage is {len(layout)} of {page_count} PDF pages"
            )
        extraction = subprocess.run(
            ["pdfimages", "-j", pdf_path, str(prefix)],
            capture_output=True,
            text=True,
            timeout=max(60, page_count * 4),
        )
        if extraction.returncode:
            raise ReconciliationError(f"pdfimages extraction failed: {extraction.stderr[:500]}")
        extracted = sorted(
            RECONCILIATION_ROOT.glob(prefix.name + "-*"),
            key=lambda path: int(re.search(r"-(\d+)\.[^.]+$", path.name).group(1)),
        )
        by_number: dict[int, Path] = {}
        for path in extracted:
            match = re.search(r"-(\d+)\.[^.]+$", path.name)
            if match:
                by_number[int(match.group(1))] = path
        expected_numbers = {
            image["number"]
            for images in layout.values()
            for image in images
        }
        if set(by_number) != expected_numbers:
            raise ReconciliationError(
                f"extracted {len(by_number)} of {len(expected_numbers)} embedded PDF images"
            )

        hashes: list[dict[str, str]] = []
        for page_number in range(1, page_count + 1):
            groups: dict[int, list[dict[str, int]]] = collections.defaultdict(list)
            for image in layout[page_number]:
                groups[image["width"]].append(image)
            pieces = max(
                groups.values(),
                key=lambda group: sum(item["width"] * item["height"] for item in group),
            )
            pieces.sort(key=lambda item: item["number"])
            width = pieces[0]["width"]
            height = sum(item["height"] for item in pieces)
            scan = Image.new("L", (width, height), 255)
            top = 0
            for piece in pieces:
                with Image.open(by_number[piece["number"]]) as image:
                    grayscale = image.convert("L")
                    if grayscale.size != (piece["width"], piece["height"]):
                        raise ReconciliationError("embedded PDF image dimensions changed during extraction")
                    scan.paste(grayscale, (0, top))
                top += piece["height"]
            displayed_scan = (
                scan.rotate(-rotations[page_number], expand=True)
                if rotations[page_number]
                else scan
            )
            try:
                hashes.append(visual_index.feature_hashes(displayed_scan))
            finally:
                if displayed_scan is not scan:
                    displayed_scan.close()
                scan.close()
        return hashes
    finally:
        for path in extracted:
            path.unlink(missing_ok=True)


def match_pdf_pages(
    md_rows: list[dict[str, Any]],
    preferred_pdfs: dict[str, dict[str, Any]],
    visual_rows: list[dict[str, Any]],
    skip: bool,
) -> list[dict[str, Any]]:
    if skip:
        return []
    sys.path.insert(0, str(VISUAL_TOOL_ROOT))
    import visual_index  # type: ignore
    from PIL import Image

    by_base: dict[str, dict[int, list[dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for row in visual_rows:
        if (
            row["sequence"] != ""
            and row["candidate_role"] != "jpeg_derivative"
            and row["status"] == "indexed"
            and row.get("ahash")
            and row.get("dhash")
            and row.get("phash")
        ):
            by_base[row["base_inventory"]][int(row["sequence"])].append(row)

    output: list[dict[str, Any]] = []
    ordered_md_rows = sorted(md_rows, key=lambda row: row["inventory"])
    for object_index, md in enumerate(ordered_md_rows, 1):
        pdf = preferred_pdfs.get(md["inventory"])
        if not pdf or not pdf.get("page_count"):
            print(f"[{object_index}/{len(ordered_md_rows)}] {md['inventory']}: no readable local PDF", flush=True)
            continue
        base_candidates = by_base.get(md["base_inventory"], {})
        if not base_candidates:
            print(f"[{object_index}/{len(ordered_md_rows)}] {md['inventory']}: no TIFF candidates", flush=True)
            continue
        try:
            rotations = pdf_page_rotations(
                pdf["source_path"], int(pdf["page_count"])
            )
            page_hashes = embedded_scan_hashes(
                pdf["source_path"], int(pdf["page_count"]), visual_index, Image,
                rotations,
            )
            raw_matches: list[dict[str, Any]] = []
            for page_number, hashes in enumerate(page_hashes, 1):
                ranked: list[tuple[int, int, int, int, int, dict[str, Any]]] = []
                for sequence, occurrences in base_candidates.items():
                    best: tuple[int, int, int, int, dict[str, Any]] | None = None
                    for candidate in occurrences:
                        da = hamming(hashes["ahash_64"], candidate["ahash"])
                        dd = hamming(hashes["dhash_64"], candidate["dhash"])
                        dp = hamming(hashes["phash_dct_64"], candidate["phash"])
                        score = (da + dd + dp, dp, dd, da, candidate)
                        if best is None or score[:4] < best[:4]:
                            best = score
                    assert best is not None
                    ranked.append((best[0], best[1], best[2], best[3], sequence, best[4]))
                ranked.sort(key=lambda item: item[:5])
                winner = ranked[0]
                runner = ranked[1] if len(ranked) > 1 else None
                raw_matches.append(
                    {
                        "inventory": md["inventory"],
                        "md_object_id": md["object_id"],
                        "pdf_page": page_number,
                        "matched_scan_sequence": winner[4],
                        "distance_total": winner[0],
                        "distance_phash": winner[1],
                        "distance_dhash": winner[2],
                        "distance_ahash": winner[3],
                        "runner_up_sequence": runner[4] if runner else "",
                        "runner_up_total": runner[0] if runner else "",
                        "best_source_path": winner[5]["source_path"],
                        "_ranked": ranked,
                    }
                )
            use_monotonic_alignment = any(rotations.values())
            if use_monotonic_alignment:
                alignment = monotonic_page_alignment(
                    [row["_ranked"] for row in raw_matches]
                )
                for row, aligned_sequence in zip(raw_matches, alignment):
                    ranked = row["_ranked"]
                    global_winner = ranked[0]
                    row["global_best_scan_sequence"] = global_winner[4]
                    row["global_best_total"] = global_winner[0]
                    if aligned_sequence is None:
                        row.update({
                            "matched_scan_sequence": "",
                            "distance_total": "",
                            "distance_phash": "",
                            "distance_dhash": "",
                            "distance_ahash": "",
                            "runner_up_sequence": global_winner[4],
                            "runner_up_total": global_winner[0],
                            "best_source_path": "",
                            "selection_basis": "embedded_scan_monotonic_sequence_gap",
                        })
                        continue
                    aligned = next(
                        item for item in ranked if item[4] == aligned_sequence
                    )
                    runner = (
                        global_winner
                        if global_winner[4] != aligned_sequence
                        else ranked[1]
                        if len(ranked) > 1
                        else None
                    )
                    row.update({
                        "matched_scan_sequence": aligned[4],
                        "distance_total": aligned[0],
                        "distance_phash": aligned[1],
                        "distance_dhash": aligned[2],
                        "distance_ahash": aligned[3],
                        "runner_up_sequence": runner[4] if runner else "",
                        "runner_up_total": runner[0] if runner else "",
                        "best_source_path": aligned[5]["source_path"],
                        "selection_basis": "embedded_scan_monotonic_sequence_alignment",
                    })
            strong_offsets = collections.Counter(
                row["matched_scan_sequence"] - row["pdf_page"]
                for row in raw_matches
                if row["matched_scan_sequence"] != ""
                and row["distance_total"] <= 30
                and row["distance_phash"] <= 16
            )
            dominant_offset = ""
            if strong_offsets:
                dominant_offset = strong_offsets.most_common(1)[0][0]
            for row in raw_matches:
                ranked = row.pop("_ranked")
                global_winner = ranked[0]
                contextual = None
                if not use_monotonic_alignment and dominant_offset != "":
                    local = [
                        item
                        for item in ranked
                        if abs((item[4] - row["pdf_page"]) - dominant_offset) <= 2
                    ]
                    contextual = min(local, key=lambda item: item[:5]) if local else None
                use_context = bool(
                    contextual
                    and contextual[4] != global_winner[4]
                    and contextual[0] <= global_winner[0] + 10
                )
                if use_context:
                    assert contextual is not None
                    row.update({
                        "matched_scan_sequence": contextual[4],
                        "distance_total": contextual[0],
                        "distance_phash": contextual[1],
                        "distance_dhash": contextual[2],
                        "distance_ahash": contextual[3],
                        "runner_up_sequence": global_winner[4],
                        "runner_up_total": global_winner[0],
                        "best_source_path": contextual[5]["source_path"],
                    })
                    row["selection_basis"] = "embedded_scan_sequence_context_within_10_hash_bits"
                elif not use_monotonic_alignment:
                    row["selection_basis"] = "embedded_scan_perceptual_hash"
                row.setdefault("global_best_scan_sequence", global_winner[4])
                row.setdefault("global_best_total", global_winner[0])
                row["dominant_offset"] = dominant_offset
                if not row["best_source_path"]:
                    row["offset_consistent"] = 0
                    row["match_state"] = "missing_visual_match"
                    continue
                row["offset_consistent"] = int(
                    dominant_offset != ""
                    and row["matched_scan_sequence"] - row["pdf_page"] == dominant_offset
                )
                row["match_state"] = classify_match_state(row)
            output.extend(raw_matches)
            consistent = sum(row["offset_consistent"] for row in raw_matches)
            print(
                f"[{object_index}/{len(ordered_md_rows)}] {md['inventory']}: {len(raw_matches)} pages, "
                f"offset={dominant_offset}, consistent={consistent}",
                flush=True,
            )
        except ReconciliationError as error:
            raise ReconciliationError(f"{md['inventory']}: {error}") from error
    return output


def documentation_rows(
    documents: list[dict[str, Any]], md_bases: set[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in documents:
        extension = row["extension"].lower()
        path = row["source_path"]
        name_upper = row["filename"].upper()
        mentioned = sorted(base for base in md_bases if base in name_upper)
        if extension in {".xlsx", ".xls", ".csv", ".zip"} and (
            "/Metadaten/" in path or "/LZA/" in path
        ):
            role = "metadata_evidence"
        elif any(
            token in name_upper
            for token in (
                "PROJEKTANTRAG",
                "PROJEKTABSCHLUSSBERICHT",
                "BRIGADEBUECHER_MD-EXPORT",
                "BRIGADEBÜCHER_MD-EXPORT",
            )
        ):
            role = "direct_project_documentation"
        else:
            role = "contextual_project_document"
        if extension == ".pdf" and re.fullmatch(
            r"(?:BB|BX)-\d+(?:_\d+)?\.PDF", name_upper
        ):
            continue
        rows.append(
            {
                **row,
                "document_role": role,
                "mentioned_md_bases": ";".join(mentioned),
                "decision_state": "candidate_only",
            }
        )
    hashes = collections.Counter(row["content_sha256"] for row in rows)
    for row in rows:
        row["hash_occurrence_count"] = hashes[row["content_sha256"]]
    return rows


def object_summaries(
    md_rows: list[dict[str, Any]],
    preferred_pdfs: dict[str, dict[str, Any]],
    visual_families: list[dict[str, Any]],
    page_matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    families_by_base: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for family in visual_families:
        families_by_base[family["base_inventory"]].append(family)
    matches_by_inventory: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for match in page_matches:
        matches_by_inventory[match["inventory"]].append(match)

    summaries: list[dict[str, Any]] = []
    obvious: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for md in sorted(md_rows, key=lambda row: row["inventory"]):
        pdf = preferred_pdfs.get(md["inventory"], {})
        page_count = pdf.get("page_count")
        expected = ""
        if md["description_page_start"] != "" and md["description_page_end"] != "":
            expected = int(md["description_page_end"]) - int(md["description_page_start"]) + 1
        families = families_by_base.get(md["base_inventory"], [])
        source_tiff = [
            row
            for row in families
            if row["candidate_role"]
            in {"bibliocopy_tiff_source", "canon_tiff_source"}
        ]
        project_tiff = [row for row in families if row["candidate_role"] == "project_tiff_copy"]
        jpg = [row for row in families if row["candidate_role"] == "jpeg_derivative"]
        matches = matches_by_inventory.get(md["inventory"], [])
        consistent = sum(row["offset_consistent"] for row in matches)
        strong = sum(row["match_state"].startswith("strong_") for row in matches)
        issues: list[str] = []
        if not pdf:
            issues.append("no_exact_local_pdf")
        elif pdf.get("unique_hash_count") != 1:
            issues.append("multiple_local_pdf_hashes")
        if expected != "" and page_count != expected:
            issues.append("md_description_pdf_page_count_mismatch")
        if not source_tiff:
            issues.append("no_original_tiff_family")
        if page_count and not matches:
            issues.append("no_pdf_to_tiff_visual_mapping")
        elif matches and consistent != page_count:
            issues.append("noncontiguous_or_ambiguous_pdf_to_tiff_mapping")
        if source_tiff and any(int(row["error_count"]) for row in source_tiff):
            issues.append("source_tiff_decode_error_present")
        suggestion = (
            "confirm_original_tiff_page_set_as_master_and_pdf_as_access"
            if source_tiff and matches and consistent == page_count
            else "confirm_pdf_as_master_if_no_complete_usable_original_tiff_set"
        )
        summary = {
            "inventory": md["inventory"],
            "md_object_id": md["object_id"],
            "title": md["title"],
            "base_inventory": md["base_inventory"],
            "part_number": md["part_number"],
            "md_pdf_url": md["pdf_urls"],
            "local_pdf_occurrences": pdf.get("occurrence_count", 0),
            "local_pdf_unique_hashes": pdf.get("unique_hash_count", 0),
            "local_pdf_page_count": page_count or "",
            "description_page_start": md["description_page_start"],
            "description_page_end": md["description_page_end"],
            "description_expected_pages": expected,
            "source_tiff_family_count": len(source_tiff),
            "project_tiff_copy_family_count": len(project_tiff),
            "jpeg_derivative_family_count": len(jpg),
            "visually_mapped_pdf_pages": len(matches),
            "offset_consistent_pages": consistent,
            "strong_consistent_pages": strong,
            "suggested_relationship": suggestion,
            "issue_flags": ";".join(issues),
            "decision_state": "awaiting_human_master_confirmation",
            "selected_master": "",
            "note": "",
        }
        summaries.append(summary)
        if issues:
            review.append(summary)
        else:
            obvious.append(summary)
    return summaries, obvious, review


def local_only_rows(
    visual_families: list[dict[str, Any]],
    other_pdfs: list[dict[str, Any]],
    md_bases: set[str],
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for family in visual_families:
        base = family["base_inventory"]
        if base in md_bases:
            continue
        row = candidates.setdefault(
            base,
            {
                "base_inventory": base,
                "visual_family_count": 0,
                "visual_file_count": 0,
                "local_pdf_count": 0,
                "evidence_paths": [],
                "relationship_state": "local_only_candidate",
                "decision_state": "needs_human_review",
            },
        )
        row["visual_family_count"] += 1
        row["visual_file_count"] += int(family["file_count"])
        row["evidence_paths"].append(family["family_path"])
    for pdf in other_pdfs:
        stem = base_inventory(Path(pdf["filename"]).stem)
        if stem in md_bases or not re.fullmatch(r"(?:BB|BX)-\d+", stem):
            continue
        row = candidates.setdefault(
            stem,
            {
                "base_inventory": stem,
                "visual_family_count": 0,
                "visual_file_count": 0,
                "local_pdf_count": 0,
                "evidence_paths": [],
                "relationship_state": "local_only_candidate",
                "decision_state": "needs_human_review",
            },
        )
        row["local_pdf_count"] += 1
        row["evidence_paths"].append(pdf["source_path"])
    output = []
    for row in sorted(candidates.values(), key=lambda value: value["base_inventory"]):
        row["evidence_paths"] = ";".join(sorted(set(row["evidence_paths"])))
        output.append(row)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-page-matching",
        action="store_true",
        help="omit PDF-page to TIFF visual matching",
    )
    parser.add_argument(
        "--refresh-page-matches-only",
        action="store_true",
        help="replace only the rebuildable PDF-page similarity index; preserve every human decision",
    )
    parser.add_argument(
        "--preserve-resolved-matches",
        action="store_true",
        help="during a page-match refresh, retain the match rows behind resolved human Master decisions",
    )
    args = parser.parse_args()
    if args.preserve_resolved_matches and not args.refresh_page_matches_only:
        raise ReconciliationError("resolved matches can be preserved only during a page-match refresh")
    for required in (
        CAPTURE_RUN / "coverage.json",
        CAPTURE_RUN / "objects.csv",
        VISUAL_INDEX,
        DOCUMENT_INDEX,
        DOCUMENT_SNAPSHOT,
    ):
        if not required.exists():
            raise ReconciliationError(f"required input unavailable: {required}")
    if not RECONCILIATION_ROOT.is_dir():
        raise ReconciliationError(f"approved reconciliation root absent: {RECONCILIATION_ROOT}")
    mount = ensure_source_guard()
    md_rows = load_md()
    documents = load_documents()
    assets, visual_root_id = load_visual_assets()
    md_bases = {row["base_inventory"] for row in md_rows}
    pdf_occurrences, preferred_pdfs, other_pdfs = build_pdf_rows(md_rows, documents)
    visual_rows, visual_families = build_visual_rows(assets, md_bases)
    initially_resolved = resolved_inventories(md_rows) if args.preserve_resolved_matches else set()
    page_matches = match_pdf_pages(
        [row for row in md_rows if row["inventory"] not in initially_resolved],
        preferred_pdfs,
        visual_rows,
        args.skip_page_matching,
    )
    page_match_fields = [
        "inventory", "md_object_id", "pdf_page", "matched_scan_sequence",
        "dominant_offset", "offset_consistent", "distance_total",
        "distance_phash", "distance_dhash", "distance_ahash",
        "runner_up_sequence", "runner_up_total", "best_source_path",
        "match_state", "selection_basis", "global_best_scan_sequence",
        "global_best_total",
    ]
    if args.refresh_page_matches_only:
        if args.skip_page_matching:
            raise ReconciliationError("cannot refresh page matches while skipping page matching")
        preserved_rows: list[dict[str, Any]] = []
        if args.preserve_resolved_matches:
            currently_resolved = resolved_inventories(md_rows)
            with PAGE_MATCHES.open(encoding="utf-8-sig", newline="") as handle:
                preserved_rows = [
                    row
                    for row in csv.DictReader(handle)
                    if row["inventory"] in currently_resolved
                ]
            page_matches = [
                row for row in page_matches if row["inventory"] not in currently_resolved
            ] + preserved_rows
            inventory_order = {
                row["inventory"]: index for index, row in enumerate(md_rows)
            }
            page_matches.sort(
                key=lambda row: (inventory_order[row["inventory"]], int(row["pdf_page"]))
            )
        replace_csv(
            PAGE_MATCHES,
            page_matches,
            page_match_fields,
        )
        print(json.dumps({
            "page_matches_replaced": len(page_matches),
            "resolved_inventories_preserved": len(initially_resolved),
            "resolved_page_matches_preserved": len(preserved_rows),
            "human_decisions_changed": 0,
            "source_files_changed": 0,
        }, indent=2))
        return 0
    documentation = documentation_rows(documents, md_bases)
    summaries, obvious, review = object_summaries(
        md_rows, preferred_pdfs, visual_families, page_matches
    )
    local_only = local_only_rows(visual_families, other_pdfs, md_bases)

    outputs: list[tuple[str, list[dict[str, Any]], list[str]]] = [
        (
            "brigadebuecher-md-objects.csv",
            md_rows,
            [
                "object_id", "inventory", "base_inventory", "part_number", "title",
                "object_type", "date", "description", "material_technique",
                "dimensions", "description_page_start", "description_page_end",
                "pdf_urls", "object_url", "metadata_rights", "last_updated",
            ],
        ),
        (
            "brigadebuecher-pdf-occurrences.csv",
            pdf_occurrences,
            [
                "inventory", "md_object_id", "source_path", "content_sha256",
                "size_bytes", "page_count", "relationship", "decision_state",
            ],
        ),
        (
            "brigadebuecher-scan-families.csv",
            visual_families,
            [
                "base_inventory", "family_path", "candidate_role", "file_count",
                "indexed_count", "error_count", "sequence_min", "sequence_max",
                "missing_sequences", "extensions", "md_base_present", "decision_state",
            ],
        ),
        (
            "brigadebuecher-page-matches.csv",
            page_matches,
            page_match_fields,
        ),
        (
            "brigadebuecher-other-pdfs.csv",
            other_pdfs,
            [
                "document_id", "filename", "source_path", "content_sha256",
                "size_bytes", "extraction_state", "relationship",
            ],
        ),
        (
            "brigadebuecher-document-candidates.csv",
            documentation,
            [
                "document_id", "filename", "extension", "source_path",
                "content_sha256", "size_bytes", "extraction_state", "parser",
                "header_text", "extraction_error", "document_role",
                "mentioned_md_bases", "hash_occurrence_count", "decision_state",
            ],
        ),
        (
            "brigadebuecher-object-summary.csv",
            summaries,
            list(summaries[0]),
        ),
        (
            "brigadebuecher-obvious-ready.csv",
            obvious,
            list(summaries[0]),
        ),
        (
            "brigadebuecher-review-queue.csv",
            review,
            list(summaries[0]),
        ),
        (
            "brigadebuecher-local-only.csv",
            local_only,
            [
                "base_inventory", "visual_family_count", "visual_file_count",
                "local_pdf_count", "evidence_paths", "relationship_state",
                "decision_state",
            ],
        ),
    ]
    for filename, rows, fields in outputs:
        write_csv(RECONCILIATION_ROOT / filename, rows, fields)

    summary = {
        "format": FORMAT,
        "source_mount": mount,
        "source_policy": "read_only_no_source_changes",
        "museum_digital_capture": str(CAPTURE_RUN),
        "museum_digital_objects": len(md_rows),
        "museum_digital_base_carriers": len(md_bases),
        "direct_local_pdf_occurrences": len(pdf_occurrences),
        "direct_local_pdf_unique_hashes": len(
            {row["content_sha256"] for row in pdf_occurrences}
        ),
        "indexed_visual_assets_considered": len(visual_rows),
        "visual_families": len(visual_families),
        "pdf_pages_visually_compared": len(page_matches),
        "obvious_ready_for_batch_confirmation": len(obvious),
        "ambiguous_md_objects": len(review),
        "local_only_base_candidates": len(local_only),
        "document_candidates": len(documentation),
        "master_selection_policy": "human_confirmation_required",
        "selected_masters": 0,
        "source_files_changed": 0,
    }
    atomic_write(
        RECONCILIATION_ROOT / "brigadebuecher-summary.json",
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    readme = "# Brigadebücher collection 861 reconciliation\n\n"
    readme += "This is a read-only candidate projection. No master has been selected, "
    readme += "no museum-digital media was downloaded, and no source file was changed.\n\n"
    readme += f"- museum-digital objects: {len(md_rows)}\n"
    readme += f"- local PDF occurrences linked by exact inventory filename: {len(pdf_occurrences)}\n"
    readme += f"- unique linked local PDF hashes: {summary['direct_local_pdf_unique_hashes']}\n"
    readme += f"- indexed local visual candidates considered: {len(visual_rows)}\n"
    readme += f"- PDF pages visually compared with indexed TIFF candidates: {len(page_matches)}\n"
    readme += f"- obvious objects ready for one human batch confirmation: {len(obvious)}\n"
    readme += f"- ambiguous museum-digital objects requiring individual review: {len(review)}\n"
    readme += f"- local-only base candidates requiring review: {len(local_only)}\n\n"
    readme += "The proposed normal relationship follows the existing Trafo and WF-Sender units: "
    readme += "an ordered original TIFF page set is the preservation Master representation and the "
    readme += "matching PDF is an access representation. A PDF is proposed as preservation Master only "
    readme += "when no complete usable original TIFF set can be established. Every proposal remains "
    readme += "unselected until a person confirms it. JPGs and duplicate project copies remain recorded "
    readme += "as derivatives or redundant occurrences; this never authorizes deletion.\n"
    atomic_write(RECONCILIATION_ROOT / "BRIGADEBUECHER_README.md", readme.encode("utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
