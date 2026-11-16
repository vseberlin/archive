#!/usr/bin/env python3
"""OCR the first page of the 63 probable Trafo alias paths, read-only.

Rendered first pages and OCR text are written below the Trafo work root. Source
PDFs and the accepted archive are never written or hashed.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


INPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "source-remnants-20260727-v2/drain-list.csv"
)
FINAL = Path("/mnt/volume/iss-archive/units/trafo")
DATABASE = FINAL / "db-restore/RESTORE.sqlite"
OUTPUT = Path(
    "/mnt/volume/iss-archive-work/trafo/work/drain/trafo/"
    "ocr-alias-review-20260727-v3"
)
FIRST_PAGES = OUTPUT / "first-pages"
OCR_TEXT = OUTPUT / "ocr-text"

EXPLICIT_MAP = {
    "TRO-1952-2": "TRO-1954-002",
    "TRO-1952-SA-02-JANUAR-02": "TRO-1952-SA-02-Januar",
    "TRO-1952-SA-04-JUNI": "TRO-1952-SA-06-Juni",
    "TRO-1954-SA": "TRO-1954-SA-Frauentag",
    "TRO-1954-SN": "TRO-1954-SN-Jugend",
    "TRO-1974-1": "TRO-1974-051",
    "TRO-1980-32": "TRO-1980-005",
}
FIELDS = [
    "review_id",
    "normalized_inventory",
    "source_path",
    "size_bytes",
    "pdf_pages",
    "expected_accepted_inventory",
    "accepted_record_exists",
    "accepted_page_count",
    "embedded_text_chars",
    "ocr_text_chars",
    "ocr_source",
    "ocr_year_tokens",
    "ocr_number_tokens",
    "text_similarity_to_accepted",
    "classification",
    "review_status",
    "first_page_path",
    "ocr_text_path",
    "source_hashed",
]


class OcrReviewError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def run(command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def normalize_text(value: str) -> str:
    return re.sub(r"\W+", "", value.casefold())


def pdf_text(path: Path, first_page_only: bool = False) -> str:
    command = ["pdftotext"]
    if first_page_only:
        command.extend(["-f", "1", "-l", "1"])
    command.extend(["-layout", str(path), "-"])
    result = run(command)
    if result.returncode:
        raise OcrReviewError(
            f"pdftotext failed for {path}: {result.stderr.strip()}"
        )
    return result.stdout


def page_count(path: Path) -> int:
    result = run(["pdfinfo", str(path)])
    if result.returncode:
        raise OcrReviewError(
            f"pdfinfo failed for {path}: {result.stderr.strip()}"
        )
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise OcrReviewError(f"PDF page count unavailable: {path}")
    return int(match.group(1))


def expected_inventory(row: dict[str, str]) -> str:
    normalized = row["normalized_inventory"]
    if normalized in EXPLICIT_MAP:
        return EXPLICIT_MAP[normalized]
    stem = Path(row["path"]).stem
    if stem.endswith("0001") and (
        stem.startswith("TRO-1952-") or stem.startswith("TRO-1957-")
    ):
        return stem[:-4]
    raise OcrReviewError(f"no reviewed alias mapping rule for {stem}")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDS,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def accepted_records() -> dict[str, dict[str, Any]]:
    database = sqlite3.connect(
        f"file:{DATABASE}?mode=ro&immutable=1", uri=True
    )
    database.row_factory = sqlite3.Row
    try:
        return {
            row["inventory"]: dict(row)
            for row in database.execute(
                "SELECT inventory, title, date_created, page_count "
                "FROM archive_object"
            )
        }
    finally:
        database.close()


def inspect_one(
    index: int,
    row: dict[str, str],
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    review_id = f"alias-{index:03d}"
    source = Path(row["path"])
    if not source.is_file() or source.stat().st_size != int(row["size_bytes"]):
        raise OcrReviewError(f"source identity changed: {source}")
    expected = expected_inventory(row)
    record = records.get(expected)
    if not record:
        raise OcrReviewError(f"expected accepted record is absent: {expected}")
    accepted_pdf = FINAL / "export/access" / f"{expected}.pdf"
    if not accepted_pdf.is_file():
        raise OcrReviewError(f"accepted access PDF is absent: {accepted_pdf}")

    first_page_text = pdf_text(source, first_page_only=True)
    ocr_text = first_page_text
    ocr_source = "embedded-ocr-text-layer"
    first_page_value = ""
    if len(normalize_text(first_page_text)) < 80:
        first_page_base = FIRST_PAGES / review_id
        render = run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-r",
                "100",
                "-gray",
                "-jpeg",
                "-jpegopt",
                "quality=75",
                str(source),
                str(first_page_base),
            ]
        )
        first_page = FIRST_PAGES / f"{review_id}.jpg"
        if render.returncode or not first_page.is_file():
            raise OcrReviewError(
                f"first-page render failed for {source}: {render.stderr.strip()}"
            )
        ocr = run(
            [
                "tesseract",
                str(first_page),
                "stdout",
                "-l",
                "deu",
                "--psm",
                "11",
            ],
            timeout=90,
        )
        if ocr.returncode:
            raise OcrReviewError(
                f"OCR fallback failed for {source}: {ocr.stderr.strip()}"
            )
        ocr_text = ocr.stdout
        ocr_source = "fresh-tesseract-fallback"
        first_page_value = str(first_page)
    ocr_path = OCR_TEXT / f"{review_id}.txt"
    write_text(ocr_path, ocr_text)

    candidate_text = pdf_text(source)
    accepted_text = pdf_text(accepted_pdf)
    candidate_normalized = normalize_text(candidate_text)
    accepted_normalized = normalize_text(accepted_text)
    similarity = SequenceMatcher(
        None, candidate_normalized, accepted_normalized, autojunk=True
    ).ratio()
    if candidate_normalized == accepted_normalized:
        classification = "already-represented-exact-text"
        review_status = "resolved"
    elif similarity >= 0.90:
        classification = "already-represented-text-match"
        review_status = "resolved"
    else:
        classification = "manual-review-required"
        review_status = "unresolved"

    ocr_folded = ocr_text.casefold()
    years = sorted(set(re.findall(r"\b(?:19|20)\d{2}\b", ocr_folded)))
    numbers = sorted(
        set(
            match.group(1)
            for match in re.finditer(
                r"\b(?:nr\.?|nummer|ausgabe)\s*[:.]?\s*(\d{1,3})\b",
                ocr_folded,
            )
        ),
        key=lambda value: int(value),
    )
    return {
        "review_id": review_id,
        "normalized_inventory": row["normalized_inventory"],
        "source_path": str(source),
        "size_bytes": source.stat().st_size,
        "pdf_pages": page_count(source),
        "expected_accepted_inventory": expected,
        "accepted_record_exists": "yes",
        "accepted_page_count": record["page_count"],
        "embedded_text_chars": len(candidate_text.strip()),
        "ocr_text_chars": len(ocr_text.strip()),
        "ocr_source": ocr_source,
        "ocr_year_tokens": " | ".join(years),
        "ocr_number_tokens": " | ".join(numbers),
        "text_similarity_to_accepted": f"{similarity:.6f}",
        "classification": classification,
        "review_status": review_status,
        "first_page_path": first_page_value,
        "ocr_text_path": str(ocr_path),
        "source_hashed": "no",
    }


def review() -> dict[str, Any]:
    allowed = Path("/mnt/volume/iss-archive-work/trafo")
    if allowed not in OUTPUT.parents:
        raise OcrReviewError("output is outside the Trafo work root")
    if OUTPUT.exists():
        raise OcrReviewError(f"refusing existing output: {OUTPUT}")
    with INPUT.open(encoding="utf-8", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row["review_group"] == "probable-alias"
        ]
    if len(rows) != 63:
        raise OcrReviewError(f"expected 63 alias paths, got {len(rows)}")

    OUTPUT.mkdir(parents=True, exist_ok=False)
    FIRST_PAGES.mkdir()
    OCR_TEXT.mkdir()
    records = accepted_records()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(inspect_one, index, row, records): (index, row)
            for index, row in enumerate(rows, 1)
        }
        for future in as_completed(futures):
            index, row = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                errors.append(
                    {
                        "review_id": f"alias-{index:03d}",
                        "source_path": row["path"],
                        "error": str(error),
                    }
                )
    results.sort(key=lambda row: row["review_id"])
    write_csv(OUTPUT / "ocr-results.csv", results)
    write_json(OUTPUT / "errors.json", errors)
    counts: dict[str, int] = {}
    for row in results:
        key = str(row["classification"])
        counts[key] = counts.get(key, 0) + 1
    summary = {
        "format": "iss-trafo-alias-ocr-review-v1",
        "created_at": now(),
        "status": "ok" if not errors and len(results) == 63 else "incomplete",
        "input": str(INPUT),
        "candidate_paths": len(rows),
        "successful_ocr": len(results),
        "errors": len(errors),
        "classification_counts": counts,
        "ocr_scope": (
            "existing first-page OCR text layer; fresh German Tesseract at "
            "100 dpi only when the embedded layer has fewer than 80 normalized "
            "characters"
        ),
        "fresh_tesseract_fallbacks": sum(
            row["ocr_source"] == "fresh-tesseract-fallback" for row in results
        ),
        "comparison_scope": (
            "full embedded text compared with the explicitly mapped accepted "
            "access PDF"
        ),
        "candidate_files_hashed": 0,
        "candidate_files_copied": 0,
        "candidate_files_moved": 0,
        "candidate_files_deleted": 0,
        "accepted_files_hashed": 0,
        "authorization": {
            "accession": False,
            "move": False,
            "deletion": False,
        },
    }
    write_json(OUTPUT / "summary.json", summary)
    write_text(
        OUTPUT / "README.md",
        """# Trafo probable-alias OCR review

This bounded run extracts the existing first-page OCR text from each of the 63
probable alias PDFs. Fresh German Tesseract OCR is used only when that embedded
text is insufficient. It also compares the full embedded text with the
explicitly mapped accepted access PDF. OCR text and any necessary fallback
first-page images are retained for review.

No source or accepted file was hashed, copied, moved, deleted, or rewritten.
No result authorizes accession or deletion.
""",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(review(), ensure_ascii=False, indent=2, sort_keys=True))
