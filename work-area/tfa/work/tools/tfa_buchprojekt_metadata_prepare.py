#!/usr/bin/env python3
"""Prepare book-bound metadata candidates for selected local TFA objects.

The tool reads the final Buchprojekt caption workbook, the 189 numbered book
TIFFs, the final PDF text layer, the existing visual index, and the current
selected local Masters.  It writes only a human-review CSV and receipt inside
the existing TFA reconciliation work root.  It never applies metadata or
changes object, Master, source, archive, or deletion state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np


VERSION = "1.0.0"
SOURCE_ROOT = Path("/media/vladimir/Elements")
BOOK_ROOT = SOURCE_ROOT / "restore/archive/WF/Buchprojekt_Industriesalon-Schöneweide"
BOOK_IMAGES = BOOK_ROOT / "Bilddaten"
CAPTIONS_XLSX = BOOK_ROOT / "Bildtexte_Buchprojekt_WordPress_FINALe.xlsx"
FINAL_PDF = BOOK_ROOT / "Röhren aus Schöneweide, TXT 03226 Schoeneweide_FINAL.pdf"
WORK_ROOT = Path("/mnt/volume/iss-archive-work/tfa")
REVIEW_ROOT = WORK_ROOT / "work/reconciliation/unit-reconciliation"
DATABASE = REVIEW_ROOT / "reconciliation.sqlite"
LOCAL_ACCESSION = REVIEW_ROOT / "reference-photo-accession-candidates.csv"
VISUAL_INDEX = Path("/home/vladimir/archive-visual-index/visual-index.sqlite")
CANDIDATES = REVIEW_ROOT / "local-object-metadata-review-candidates.csv"
DECISIONS = REVIEW_ROOT / "local-object-metadata-review-decisions.csv"
RECEIPT = REVIEW_ROOT / "local-object-metadata-review-preparation.json"

MAX_PHASH_DISTANCE = 14
MAX_MEAN_DISTANCE = 16.0
MAX_ASPECT_DELTA = 0.05

BASE_CANDIDATE_FIELDS = (
    "candidate_id", "record_key", "inventory", "selected_master_path",
    "source_path", "source_sha256", "source_size_bytes", "source_mtime_ns",
    "sheet_name", "row_number", "matched_cell_references",
    "inventory_tokens_in_row", "row_scope", "header_row_number",
    "row_inventory", "identity_review_state", "md_record_key", "md_object_id",
    "md_inventory", "md_title", "md_date_text", "md_selected_master_path",
    "labelled_cells_json", "raw_cells_json", "suggested_title",
    "suggested_description", "suggested_date_text", "suggested_object_type",
    "suggested_material", "suggested_dimensions", "candidate_basis",
    "deletion_authorized",
)
BOOK_FIELDS = (
    "book_image_key", "book_page", "book_caption", "book_image_path",
    "book_image_size_bytes", "book_image_mtime_ns", "book_visual_asset_id",
    "visual_ahash_distance", "visual_dhash_distance", "visual_phash_distance",
    "visual_mean_distance", "visual_aspect_ratio_delta", "pdf_caption_verified",
    "pdf_caption_support_score",
    "current_title", "current_description", "current_date_text",
    "current_object_type", "current_material", "current_dimensions",
)
DECISION_FIELDS = (
    "record_key", "decision", "candidate_id", "title", "description",
    "date_text", "object_type", "material", "dimensions", "note",
    "decided_by", "decided_at", "source_path", "source_sha256",
    "sheet_name", "row_number", "deletion_authorized",
)


class PrepareError(RuntimeError):
    pass


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def atomic_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(fields), quoting=csv.QUOTE_ALL,
                extrasaction="ignore",
            )
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


def source_mount() -> dict[str, Any]:
    completed = subprocess.run(
        ["findmnt", "-J", "-T", str(SOURCE_ROOT), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    filesystems = json.loads(completed.stdout).get("filesystems") or []
    if len(filesystems) != 1:
        raise PrepareError("Elements did not resolve to one mounted filesystem")
    mount = filesystems[0]
    options = set(str(mount.get("options", "")).split(","))
    if mount.get("source") != "/dev/sdf1" or mount.get("fstype") != "ntfs3" or "ro" not in options:
        raise PrepareError(f"Elements read-only mount guard failed: {mount}")
    return mount


def xlsx_rows(path: Path) -> Iterable[tuple[str, int, dict[int, str]]]:
    parser_path = Path(__file__).with_name("tfa_local_metadata_review_prepare.py")
    specification = importlib.util.spec_from_file_location("tfa_xlsx_parser", parser_path)
    if specification is None or specification.loader is None:
        raise PrepareError("cannot load the existing bounded XLSX parser")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    yield from module.xlsx_rows(path)


def book_inventory(value: str) -> str:
    rest = re.sub(r"^\d{3}(?:-\d+)?[_ ]+|^\d{3}[-_ ]+", "", value).strip()
    match = re.search(r"(?i)^(\d{5,8}(?:[-_.]\d{1,2})?)(?![A-Z0-9_-])", rest)
    if match is None:
        return ""
    return "TFA-" + re.sub(r"[_ .]+", "-", match.group(1)).strip("-")


def book_slot(value: str) -> str:
    match = re.match(r"^(\d{3}(?:-\d+)?)(?=_)|^(\d{3})(?=[-_ ])", value)
    if match is None:
        raise PrepareError(f"book image has no bounded sequence number: {value}")
    return match.group(1) or match.group(2)


def canonical_rows() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    canonical: dict[str, dict[str, Any]] = {}
    detail: dict[str, dict[str, Any]] = {}
    for sheet, row_number, values in xlsx_rows(CAPTIONS_XLSX):
        if sheet == "alle Bilder Buch" and row_number > 1:
            key = clean(values.get(3, ""))
            if re.match(r"^\d{3}(?:-|_)", key):
                if key in canonical:
                    raise PrepareError(f"duplicate canonical book image key: {key}")
                canonical[key] = {
                    "sheet": sheet, "row_number": row_number,
                    "page": clean(values.get(1, "")),
                    "key": key, "caption": clean(values.get(5, "")),
                    "values": values,
                }
        elif sheet in {"50er", "60er", "70er-80er"} and row_number > 1:
            key = clean(values.get(2, ""))
            if re.match(r"^\d{3}(?:-|_)", key):
                detail[key] = {"sheet": sheet, "row_number": row_number, "values": values}
    if len(canonical) != 189:
        raise PrepareError(f"canonical Buchprojekt caption scope drifted: {len(canonical)} != 189")
    return canonical, detail


def book_files(canonical: dict[str, dict[str, Any]]) -> dict[str, Path]:
    files_by_slot: dict[str, Path] = {}
    for path in BOOK_IMAGES.iterdir():
        if not path.is_file():
            continue
        slot = book_slot(path.stem)
        if slot in files_by_slot:
            raise PrepareError(f"duplicate book TIFF sequence: {slot}")
        files_by_slot[slot] = path
    canonical_by_slot = {book_slot(key): key for key in canonical}
    if len(files_by_slot) != 189 or set(files_by_slot) != set(canonical_by_slot):
        missing = sorted(set(canonical_by_slot) - set(files_by_slot))
        extra = sorted(set(files_by_slot) - set(canonical_by_slot))
        raise PrepareError(f"book TIFF/caption sequence drifted: missing={missing[:3]} extra={extra[:3]}")
    return {key: files_by_slot[slot] for slot, key in canonical_by_slot.items()}


def pdf_text() -> str:
    completed = subprocess.run(
        ["pdftotext", "-layout", str(FINAL_PDF), "-"],
        check=True, capture_output=True, text=True, timeout=120,
    )
    if len(completed.stdout.split()) < 5000:
        raise PrepareError("final book PDF text layer is unexpectedly sparse")
    return clean(completed.stdout).casefold()


PDF_STOPWORDS = frozenset({
    "eine", "einer", "eines", "einem", "einen", "der", "die", "das", "dem",
    "den", "des", "und", "oder", "bei", "mit", "von", "vom", "zum", "zur",
    "im", "in", "an", "am", "auf", "aus", "für", "als", "wurde", "foto",
    "fotos", "bild", "bilder", "wahrscheinlich",
})


def pdf_caption_support(caption: str, text: str) -> float:
    words = [
        word for word in re.findall(r"(?u)\b\w{3,}\b", caption.casefold())
        if word not in PDF_STOPWORDS
    ]
    if not words:
        return 0.0
    text_words = set(re.findall(r"(?u)\b\w{3,}\b", text))
    return round(sum(word in text_words for word in words) / len(words), 6)


def local_targets() -> dict[str, dict[str, str]]:
    rows = {
        row["record_key"]: row for row in read_csv(LOCAL_ACCESSION)
        if row.get("metadata_state") == "metadata_deferred_next_pass"
    }
    if len(rows) != 2405:
        raise PrepareError(f"deferred local metadata scope drifted: {len(rows)} != 2405")
    return rows


def visual_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with closing(sqlite3.connect(f"file:{VISUAL_INDEX}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        root = connection.execute(
            "SELECT root_id FROM visual_roots WHERE root_path=?", (str(SOURCE_ROOT),)
        ).fetchone()
        if root is None:
            raise PrepareError("visual index is not bound to Elements")
        books = [dict(row) for row in connection.execute(
            """
            SELECT a.asset_id,a.relative_path,a.width_px,a.height_px,a.size_bytes,a.mtime_ns,
              MAX(CASE WHEN f.extractor_id='ahash-64-v2' THEN f.feature_hex ELSE '' END) ahash,
              MAX(CASE WHEN f.extractor_id='dhash-64-v2' THEN f.feature_hex ELSE '' END) dhash,
              MAX(CASE WHEN f.extractor_id='phash-dct-64-v2' THEN f.feature_hex ELSE '' END) phash
            FROM visual_assets a JOIN visual_features f ON f.asset_id=a.asset_id
            WHERE a.root_id=? AND a.status='indexed'
              AND a.relative_path LIKE 'restore/archive/WF/Buchprojekt_Industriesalon-Schöneweide/Bilddaten/%'
            GROUP BY a.asset_id ORDER BY a.relative_path
            """,
            (root[0],),
        )]
    with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        selected = [dict(row) for row in connection.execute(
            """
            SELECT d.record_key,c.inventory,c.source_path,c.candidate_id,
              h.ahash_64 ahash,h.dhash_64 dhash,h.phash_64 phash,
              t.width_px,t.height_px
            FROM local_object_decisions d
            JOIN candidates c ON c.candidate_id=d.selected_candidate_id
            JOIN local_visual_hashes h ON h.candidate_id=c.candidate_id
            JOIN local_candidate_technical t ON t.candidate_id=c.candidate_id
            WHERE d.outcome='new_local_object' AND h.decode_status='ok'
            ORDER BY d.record_key
            """
        )]
    if len(books) != 189 or len(selected) != 2415:
        raise PrepareError(f"visual scope drifted: book={len(books)} local={len(selected)}")
    return books, selected


def relationships(
    books: list[dict[str, Any]], selected: list[dict[str, Any]],
    targets: dict[str, dict[str, str]],
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    local_a = np.array([int(row["ahash"], 16) for row in selected], dtype=np.uint64)
    local_d = np.array([int(row["dhash"], 16) for row in selected], dtype=np.uint64)
    local_p = np.array([int(row["phash"], 16) for row in selected], dtype=np.uint64)
    local_ratio = np.array([
        int(row["width_px"]) / int(row["height_px"]) for row in selected
    ], dtype=np.float64)
    result: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for book in books:
        ah = np.bitwise_count(local_a ^ np.uint64(int(book["ahash"], 16)))
        dh = np.bitwise_count(local_d ^ np.uint64(int(book["dhash"], 16)))
        ph = np.bitwise_count(local_p ^ np.uint64(int(book["phash"], 16)))
        mean = (ah + dh + ph) / 3
        ratio = int(book["width_px"]) / int(book["height_px"])
        aspect = np.abs(local_ratio - ratio) / ratio
        indexes = np.where(
            (ph <= MAX_PHASH_DISTANCE)
            & (mean <= MAX_MEAN_DISTANCE)
            & (aspect <= MAX_ASPECT_DELTA)
        )[0]
        for index in indexes:
            local = selected[int(index)]
            if local["record_key"] not in targets:
                continue
            result.append((book, local, {
                "ahash_distance": int(ah[index]),
                "dhash_distance": int(dh[index]),
                "phash_distance": int(ph[index]),
                "mean_distance": round(float(mean[index]), 6),
                "aspect_delta": round(float(aspect[index]), 8),
            }))
    if len(result) != 8 or len({local["record_key"] for _, local, _ in result}) != 8:
        raise PrepareError(f"bounded book/local relationship scope drifted: {len(result)}")
    return result


def proposal(
    canonical: dict[str, Any], detail: dict[str, Any] | None,
    current: dict[str, str],
) -> dict[str, str]:
    values = detail["values"] if detail else {}
    caption = canonical["caption"]
    title = clean(values.get(6, "")) or current.get("title", "") or caption
    date_text = clean(values.get(5, ""))
    material = clean(values.get(7, "")) if clean(values.get(7, "")) in {
        "Schwarzweißfotografie", "Farbfotografie",
    } else ""
    return {
        "title": title,
        "description": caption,
        "date_text": date_text,
        "object_type": current.get("object_type", ""),
        "material": material or current.get("material", ""),
        "dimensions": current.get("dimensions", ""),
    }


def prepare() -> dict[str, Any]:
    mount = source_mount()
    for path in (CANDIDATES, RECEIPT):
        if path.exists():
            raise PrepareError(f"refusing to overwrite local metadata review output: {path}")
    canonical, details = canonical_rows()
    files = book_files(canonical)
    text = pdf_text()
    targets = local_targets()
    books, selected = visual_rows()
    matched = relationships(books, selected, targets)
    workbook_hash = sha256_file(CAPTIONS_XLSX)
    candidates: list[dict[str, Any]] = []
    canonical_by_slot = {book_slot(key): key for key in canonical}
    for book, local, distance in matched:
        key = canonical_by_slot[book_slot(Path(book["relative_path"]).stem)]
        row = canonical[key]
        detail = details.get(key)
        target = targets[local["record_key"]]
        fields = proposal(row, detail, target)
        inventory = book_inventory(key)
        master_name = Path(local["source_path"]).name
        master_inventory_match = inventory and inventory.removeprefix("TFA-") in master_name
        state = (
            "book_inventory_and_visual_match"
            if master_inventory_match else "book_visual_match_related_object"
        )
        book_path = files[key]
        stat = book_path.stat()
        pdf_support = pdf_caption_support(row["caption"], text)
        pdf_verified = pdf_support >= 0.75
        labelled = [
            {"cell": f"A{row['row_number']}", "header": "Seite", "value": row["page"]},
            {"cell": f"C{row['row_number']}", "header": "Laufnummer_Buch", "value": key},
            {"cell": f"E{row['row_number']}", "header": "Bildtexte-fertig", "value": row["caption"]},
        ]
        if detail:
            for column, header in ((4, "description"), (5, "date_text"), (6, "object_title"), (7, "tag1")):
                if clean(detail["values"].get(column, "")):
                    labelled.append({
                        "cell": f"{chr(64 + column)}{detail['row_number']}",
                        "header": header,
                        "value": clean(detail["values"].get(column, "")),
                    })
        seed = f"{book['asset_id']}|{local['record_key']}|{workbook_hash}"
        candidates.append({
            "candidate_id": "BOOKMETA-" + hashlib.sha256(seed.encode()).hexdigest()[:20].upper(),
            "record_key": local["record_key"],
            "inventory": target["inventory"],
            "selected_master_path": local["source_path"],
            "source_path": str(CAPTIONS_XLSX),
            "source_sha256": workbook_hash,
            "source_size_bytes": str(CAPTIONS_XLSX.stat().st_size),
            "source_mtime_ns": str(CAPTIONS_XLSX.stat().st_mtime_ns),
            "sheet_name": row["sheet"],
            "row_number": str(row["row_number"]),
            "matched_cell_references": f"C{row['row_number']};E{row['row_number']}",
            "inventory_tokens_in_row": inventory,
            "row_scope": "single_book_image",
            "header_row_number": "1",
            "row_inventory": inventory,
            "identity_review_state": state,
            "md_record_key": "", "md_object_id": "", "md_inventory": "",
            "md_title": "", "md_date_text": "", "md_selected_master_path": "",
            "labelled_cells_json": json.dumps(labelled, ensure_ascii=False, separators=(",", ":")),
            "raw_cells_json": json.dumps(labelled, ensure_ascii=False, separators=(",", ":")),
            "suggested_title": fields["title"],
            "suggested_description": fields["description"],
            "suggested_date_text": fields["date_text"],
            "suggested_object_type": fields["object_type"],
            "suggested_material": fields["material"],
            "suggested_dimensions": fields["dimensions"],
            "candidate_basis": (
                "human review: selected Master visually matches numbered Buchprojekt image; "
                "metadata comes from final caption workbook and PDF text verification"
            ),
            "deletion_authorized": "false",
            "book_image_key": key,
            "book_page": row["page"],
            "book_caption": row["caption"],
            "book_image_path": str(book_path),
            "book_image_size_bytes": str(stat.st_size),
            "book_image_mtime_ns": str(stat.st_mtime_ns),
            "book_visual_asset_id": book["asset_id"],
            "visual_ahash_distance": str(distance["ahash_distance"]),
            "visual_dhash_distance": str(distance["dhash_distance"]),
            "visual_phash_distance": str(distance["phash_distance"]),
            "visual_mean_distance": str(distance["mean_distance"]),
            "visual_aspect_ratio_delta": str(distance["aspect_delta"]),
            "pdf_caption_verified": str(pdf_verified).lower(),
            "pdf_caption_support_score": str(pdf_support),
            "current_title": target.get("title", ""),
            "current_description": target.get("description", ""),
            "current_date_text": target.get("date_text", ""),
            "current_object_type": target.get("object_type", ""),
            "current_material": target.get("material", ""),
            "current_dimensions": target.get("dimensions", ""),
        })
    candidates.sort(key=lambda item: (item["book_image_key"], item["inventory"]))
    if not all(row["pdf_caption_verified"] == "true" for row in candidates):
        raise PrepareError("a proposed Buchprojekt caption was not verified in the final PDF text layer")
    atomic_csv(CANDIDATES, BASE_CANDIDATE_FIELDS + BOOK_FIELDS, candidates)
    if not DECISIONS.exists():
        atomic_csv(DECISIONS, DECISION_FIELDS, ())
    rejected_filename_conflicts = []
    by_path = {Path(row["selected_master_path"]).name: row for row in targets.values()}
    for key in ("040_5817530", "048_5511140"):
        token = book_inventory(key)
        master = next((name for name in by_path if token.removeprefix("TFA-") in name), "")
        rejected_filename_conflicts.append({
            "book_image_key": key,
            "book_inventory": token,
            "selected_master_filename": master,
            "reason": "filename_or_inventory_only_but_visual_content_conflicts",
        })
    receipt = {
        "format": "iss-tfa-buchprojekt-local-metadata-review-v1",
        "version": VERSION,
        "prepared_at": now(),
        "result": "ok",
        "source_mount": mount,
        "source_read_only": True,
        "source_modified": False,
        "published_tfa_unit_changed": False,
        "automatic_metadata_application": False,
        "deletion_authorized": False,
        "canonical_book_images": len(canonical),
        "canonical_caption_rows": len(canonical),
        "final_pdf_text_words_minimum": 5000,
        "selected_local_scope": len(selected),
        "structured_candidate_target_count": len({row["record_key"] for row in candidates}),
        "structured_candidate_rows": len(candidates),
        "visual_thresholds": {
            "maximum_phash_distance": MAX_PHASH_DISTANCE,
            "maximum_mean_distance": MAX_MEAN_DISTANCE,
            "maximum_aspect_ratio_delta": MAX_ASPECT_DELTA,
        },
        "identity_review_state_counts": {
            state: sum(row["identity_review_state"] == state for row in candidates)
            for state in sorted({row["identity_review_state"] for row in candidates})
        },
        "book_image_candidate_counts": {
            key: sum(row["book_image_key"] == key for row in candidates)
            for key in sorted({row["book_image_key"] for row in candidates})
        },
        "rejected_filename_identity_conflicts": rejected_filename_conflicts,
        "inputs": {
            CAPTIONS_XLSX.name: "sha256:" + workbook_hash,
            FINAL_PDF.name: "sha256:" + sha256_file(FINAL_PDF),
            LOCAL_ACCESSION.name: "sha256:" + sha256_file(LOCAL_ACCESSION),
        },
        "outputs": {"candidates": str(CANDIDATES), "decisions": str(DECISIONS)},
    }
    atomic_json(RECEIPT, receipt)
    return receipt


def check() -> dict[str, Any]:
    mount = source_mount()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    candidates = read_csv(CANDIDATES)
    decisions = read_csv(DECISIONS)
    if (
        receipt.get("format") != "iss-tfa-buchprojekt-local-metadata-review-v1"
        or receipt.get("result") != "ok"
        or receipt.get("source_read_only") is not True
        or receipt.get("source_modified") is not False
        or receipt.get("published_tfa_unit_changed") is not False
        or receipt.get("automatic_metadata_application") is not False
        or receipt.get("deletion_authorized") is not False
        or len(candidates) != int(receipt.get("structured_candidate_rows", -1))
        or any(row.get("deletion_authorized") != "false" for row in candidates + decisions)
    ):
        raise PrepareError("Buchprojekt local metadata review projection is not safe")
    return {
        "result": "ok", "candidate_objects": len({row["record_key"] for row in candidates}),
        "candidate_rows": len(candidates), "decision_rows": len(decisions),
        "source_mount": mount, "deletion_authorized": False,
    }


def self_test() -> None:
    assert book_inventory("030_5410023") == "TFA-5410023"
    assert book_inventory("067-1_6018903") == "TFA-6018903"
    assert book_inventory("029-5511011-26") == "TFA-5511011-26"
    assert book_inventory("123_IMG_2020012") == ""
    assert book_slot("067-1_6018903") == "067-1"
    assert book_slot("029-5511011-26") == "029"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("self-test: ok")
        return 0
    payload = prepare() if args.apply else check()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
